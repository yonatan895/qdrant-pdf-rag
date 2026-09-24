#!/usr/bin/env python3
"""Review tooling for Increment B (Issue #411).

Provides:
- Risk-based profile selection and candidate runtime manifest generation (R1, §5.1)
- Review result schema version 1 parsing, fail-closed validation, and attribution checks (R3, §5.3)
- Enforceable candidate acceptance summary with 5-state lane taxonomy and anti-skip enforcement (R4, §5.4)
"""
from __future__ import annotations

import argparse
import dataclasses
import enum
import fnmatch
import hashlib
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import sysconfig
from collections.abc import Iterable
from typing import Any

# -----------------------------------------------------------------------------
# Constants and Enums
# -----------------------------------------------------------------------------

SCHEMA_VERSION = 1

PROSE_PATTERNS = [
    "*.md",
    "*.markdown",
    "LICENSE*",
    "ROADMAP.md",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/**",
    "evals/README.md",
]

TOOLING_PATTERNS = [
    "scripts/**",
    "benchmarks/**",
    "vendor/**",
    "tests/test_agent_context.py",
    "tests/test_agent_doctor.py",
    "tests/test_review_tooling.py",
    ".github/workflows/**",
    ".gitlab-ci.yml",
    "NOTICE*",
    "Taskfile.yml",
    "taskfiles/**",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
]

# Packaging/deployment/defaults/identity surfaces (verification table row
# `packaging/deploy`). These select the lightweight `deploy` profile: no heavy
# services, but the packaging lane and unit tests are required. Ambiguity with
# a code category (storage/http/tracing) takes the union (FULL) instead.
DEPLOY_PATTERNS = [
    "deploy/**",
    "images/**",
    "overlays/**",
    "oc-mirror/**",
    "charts/**",
    "Dockerfile*",
    "Containerfile*",
    "*.dockerfile",
    ".dockerignore",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "compose*.yaml",
    "scripts/airgap/**",
    "taskfiles/airgap.yml",
    "taskfiles/artifacts.yml",
    "scripts/tools/**",
    "scripts/bootstrap.sh",
    "scripts/bootstrap_ci.py",
    "pyproject.toml",
    "requirements*.txt",
    "requirements*.in",
    "locks/**",
    "scripts/dependency_lock.py",
    "scripts/prepare_python.py",
    "scripts/image_inventory.py",
    "constraints*.txt",
    "*.lock",
    "airgap.env.example",
    "tests/test_airgap_*.py",
    "tests/helpers_airgap.py",
    "tests/helpers_helm.py",
    "tests/helpers_image_inventory.py",
    "tests/helpers_task_artifact.py",
]

TEST_PATTERNS = [
    "tests/**",
]

STORAGE_PATTERNS = [
    "src/mainframe_rag/ingest/**",
    "src/mainframe_rag/retrieve/**",
    "scripts/qdrant_*.py",
    "scripts/fetch_bm25_weights.py",
    "bm25-weights.sha256",
    "images.txt",
    "evals/**",
    "tests/test_ingest*.py",
    "tests/test_qdrant*.py",
    "tests/test_integration_sim.py",
    "tests/test_eval_retrieval.py",
    "tests/test_publish*.py",
]

HTTP_PATTERNS = [
    "src/mainframe_rag/agent/**",
    "src/mainframe_rag/webui/**",
    "src/mainframe_rag/ui/**",
    "src/mainframe_rag/mcp/**",
    "src/mainframe_rag/serve/**",
    "scripts/mock_vllm.py",
    "tests/test_agent_api.py",
    "tests/test_agent_app.py",
    "tests/test_serve*.py",
    "tests/test_webui*.py",
    "tests/test_mcp*.py",
    "tests/test_app*.py",
    "tests/test_stream*.py",
]

TRACING_PATTERNS = [
    "src/mainframe_rag/tracing.py",
    "scripts/run_local_jaeger.sh",
    "tests/test_tracing*.py",
]


# Instruction changes affect the verification contract even when Markdown.
INSTRUCTION_NAMES = {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "GEMINI.md", "CONTEXT.md", "SKILL.md"}
POLICY_DOCUMENTS = {"docs/agent-workflow.md", "docs/live-stack.md", "docs/testing.md",
                    "docs/task-runner.md", "docs/dependencies.md"}
# These shared producers feed every selected suite. Narrow helpers retain their
# actual deploy owner above instead of treating every tests/** file as global.
SHARED_TEST_INPUTS = {"tests/conftest.py", "tests/fakes.py", "tests/ci_shard.py", "tests/__init__.py"}


class ProfileName(str, enum.Enum):
    OFFLINE = "offline"
    DEPLOY = "deploy"
    STORAGE = "storage"
    HTTP = "http"
    TRACING = "tracing"
    FULL = "full"


class CodeAssessment(str, enum.Enum):
    ACCEPTABLE = "acceptable"
    CHANGES_REQUIRED = "changes_required"
    INCOMPLETE = "incomplete"


class VerificationStatus(str, enum.Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class CandidateCurrentness(str, enum.Enum):
    CURRENT = "current"
    STALE = "stale"
    UNVERIFIED = "unverified"


class MergeReadiness(str, enum.Enum):
    READY_FOR_MAINTAINER = "ready_for_maintainer"
    NOT_READY = "not_ready"


class FindingDisposition(str, enum.Enum):
    FIXED_AND_VERIFIED = "fixed-and-verified"
    DISPROVEN_WITH_EVIDENCE = "disproven-with-evidence"
    ACCEPTED_BY_AUTHORIZED_OWNER = "accepted-by-authorized-owner"
    UNRESOLVED = "unresolved"


RESOLVED_DISPOSITIONS: set[str] = {
    FindingDisposition.FIXED_AND_VERIFIED.value,
    FindingDisposition.DISPROVEN_WITH_EVIDENCE.value,
    FindingDisposition.ACCEPTED_BY_AUTHORIZED_OWNER.value,
}


class LaneState(str, enum.Enum):
    UNSELECTED = "UNSELECTED"
    SELECTED_PASSED = "SELECTED_PASSED"
    SELECTED_FAILED = "SELECTED_FAILED"
    SELECTED_SKIPPED = "SELECTED_SKIPPED"
    SELECTED_MISSING = "SELECTED_MISSING"


# -----------------------------------------------------------------------------
# Profile Selection Logic (R1, §5.1)
# -----------------------------------------------------------------------------

def _match_any(path: str, patterns: list[str]) -> bool:
    norm_path = path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(norm_path, pat):
            return True
        if "**" in pat:
            # Simple recursive glob match
            prefix, _, suffix = pat.partition("/**")
            if suffix:
                clean_suffix = suffix.lstrip("/")
                if norm_path.startswith(prefix + "/") and fnmatch.fnmatch(norm_path[len(prefix) + 1:], clean_suffix):
                    return True
            else:
                if norm_path == prefix or norm_path.startswith(prefix + "/"):
                    return True
    return False


@dataclasses.dataclass(frozen=True)
class ProfileDecision:
    profile: ProfileName
    services: list[str]
    matched_categories: list[str]

    @property
    def needs_qdrant(self) -> bool:
        return "qdrant" in self.services

    @property
    def needs_vllm(self) -> bool:
        return "vllm" in self.services

    @property
    def needs_jaeger(self) -> bool:
        return "jaeger" in self.services

    @property
    def needs_agent(self) -> bool:
        return "agent" in self.services

    @property
    def test_focus(self) -> str:
        if self.profile == ProfileName.OFFLINE:
            if "tooling" in self.matched_categories or "tests" in self.matched_categories:
                return "tests/test_agent_context.py tests/test_review_tooling.py"
            return "tests/test_agent_context.py"
        elif self.profile == ProfileName.DEPLOY:
            return "tests/test_airgap_*.py tests/test_config.py"
        elif self.profile == ProfileName.HTTP:
            return "tests/test_agent_api.py tests/test_stream_truncation.py"
        elif self.profile == ProfileName.STORAGE:
            return "tests/test_ingest_*.py tests/test_publish_*.py"
        elif self.profile == ProfileName.TRACING:
            return "tests/test_tracing*.py"
        else:
            return "tests/test_agent_api.py tests/test_ingest_publish.py"


ALL_SERVICES = ["agent", "jaeger", "qdrant", "vllm"]


def classify_paths(paths: Iterable[str]) -> ProfileDecision:
    path_list = [p for p in paths if p]
    if not path_list:
        # An empty change set cannot be classified safely: an unreadable diff,
        # a bad SHA or a shallow checkout must not silently downgrade review.
        return ProfileDecision(ProfileName.FULL, list(ALL_SERVICES), ["unclassified_empty"])

    categories: set[str] = set()
    services: set[str] = set()

    for path in path_list:
        matched = False
        if pathlib.PurePosixPath(path).name in INSTRUCTION_NAMES or path in POLICY_DOCUMENTS:
            categories.add("tooling")
            matched = True
        if path in SHARED_TEST_INPUTS:
            categories.update(("storage", "http", "tracing", "deploy", "tests"))
            services.update(ALL_SERVICES)
            matched = True
        is_markdown = path.replace("\\", "/").endswith((".md", ".markdown"))
        if _match_any(path, PROSE_PATTERNS):
            categories.add("prose")
            matched = True
        if _match_any(path, TOOLING_PATTERNS):
            categories.add("tooling")
            matched = True
        if _match_any(path, TEST_PATTERNS):
            categories.add("tests")
            matched = True
        if _match_any(path, DEPLOY_PATTERNS):
            categories.add("deploy")
            matched = True
        # Markdown never selects a live service by itself: the prose resource
        # boundary is strictly offline even when the file sits under an
        # evals/, charts/ or src/ tree. Tooling/tests/deploy still apply.
        if not is_markdown:
            if _match_any(path, STORAGE_PATTERNS):
                categories.add("storage")
                services.add("qdrant")
                matched = True
            if _match_any(path, HTTP_PATTERNS):
                categories.add("http")
                services.add("qdrant")
                services.add("vllm")
                services.add("agent")
                matched = True
            if _match_any(path, TRACING_PATTERNS):
                categories.add("tracing")
                services.add("jaeger")
                matched = True

        if not matched:
            # Fail closed: an unmapped executable/config path selects the
            # broader applicable checks instead of a silent docs-only pass.
            categories.add("unclassified")
            services.update(ALL_SERVICES)

    code_categories = categories - {"prose", "tooling", "tests", "unclassified"}
    sorted_cats = sorted(categories)

    if "unclassified" in categories:
        return ProfileDecision(ProfileName.FULL, list(ALL_SERVICES), sorted_cats)

    if not code_categories:
        # Strictly offline CPU: prose and/or tooling/tests only
        return ProfileDecision(ProfileName.OFFLINE, [], sorted_cats)

    # Determine profile name based on active code categories
    if code_categories == {"deploy"}:
        return ProfileDecision(ProfileName.DEPLOY, sorted(services), sorted_cats)
    elif code_categories == {"storage"}:
        return ProfileDecision(ProfileName.STORAGE, sorted(services), sorted_cats)
    elif code_categories == {"http"}:
        return ProfileDecision(ProfileName.HTTP, sorted(services), sorted_cats)
    elif code_categories == {"tracing"}:
        return ProfileDecision(ProfileName.TRACING, sorted(services), sorted_cats)
    else:
        # Cross-layer union or full
        return ProfileDecision(ProfileName.FULL, sorted(services), sorted_cats)


# -----------------------------------------------------------------------------
# Runtime Manifest Generation (R1, §5.1)
# -----------------------------------------------------------------------------

def probe_git_status() -> tuple[str, str, str, bool]:
    """Returns (head_sha, base_sha, execution_sha, dirty)."""
    def run_git(*args: str) -> str:
        try:
            res = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
            return res.stdout.strip()
        except OSError:
            return ""

    head_sha = run_git("rev-parse", "HEAD") or "0" * 40
    base_sha = run_git("merge-base", "origin/main", "HEAD") or run_git("rev-parse", "origin/main") or head_sha
    execution_sha = head_sha

    # Check uncommitted tracked changes
    porcelain = run_git("status", "--porcelain", "-uno")
    dirty = bool(porcelain)

    return head_sha, base_sha, execution_sha, dirty


def probe_interpreter() -> dict[str, Any]:
    gil_disabled = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    jit_enabled = bool(getattr(sys, "_jit", None) and sys._jit.is_enabled())
    version_str = ".".join(str(x) for x in sys.version_info[:3])
    return {
        "implementation": platform.python_implementation(),
        "version": version_str,
        "free_threaded": gil_disabled,
        "gil_disabled": gil_disabled,
        "jit": jit_enabled,
        "jit_enabled": jit_enabled,
    }


def probe_dependencies(root: pathlib.Path) -> dict[str, str]:
    lockfile = root / "requirements.lock.txt"
    if lockfile.exists():
        req_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    else:
        req_hash = "0" * 64
    return {
        "requirements_hash": req_hash,
        "lockfile_sha256": req_hash,
    }


def generate_candidate_manifest(
    profile_decision: ProfileDecision,
    head_sha: str | None = None,
    base_sha: str | None = None,
    execution_sha: str | None = None,
    dirty: bool | None = None,
    repo_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    root = repo_root or pathlib.Path.cwd()
    g_head, g_base, g_exec, g_dirty = probe_git_status()

    final_head = head_sha or g_head
    final_base = base_sha or g_base
    final_exec = execution_sha or g_exec
    final_dirty = g_dirty if dirty is None else dirty

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_version": SCHEMA_VERSION,
        "head_sha": final_head,
        "base_sha": final_base,
        "execution_sha": final_exec,
        "dirty": final_dirty,
        "dirty_tree": final_dirty,
        "profile": profile_decision.profile.value,
        "test_focus": profile_decision.test_focus,
        "services": profile_decision.services,
        "matched_categories": profile_decision.matched_categories,
        "interpreter": probe_interpreter(),
        "dependencies": probe_dependencies(root),
    }
    return manifest


# -----------------------------------------------------------------------------
# Review Result Parsing and Validation (R3, §5.3)
# -----------------------------------------------------------------------------

def extract_review_json(content: str) -> dict[str, Any] | None:
    content = content.strip()
    # 1. Direct JSON parse
    if content.startswith("{") and content.endswith("}"):
        try:
            val = json.loads(content)
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass

    # 2. Extract from HTML comment markers <!-- review-result:start -->...<!-- review-result:end -->
    comment_pattern = re.compile(
        r"<!--\s*review-result:start\s*-->(.*?)<!--\s*review-result:end\s*-->",
        re.DOTALL | re.IGNORECASE,
    )
    match = comment_pattern.search(content)
    if match:
        snippet = match.group(1).strip()
        try:
            val = json.loads(snippet)
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass

    # 3. Extract from markdown code fences: ```json ... ```
    fence_pattern = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
    for fence_match in fence_pattern.finditer(content):
        snippet = fence_match.group(1).strip()
        try:
            val = json.loads(snippet)
            if isinstance(val, dict) and ("schema_version" in val or "code_assessment" in val):
                return val
        except json.JSONDecodeError:
            continue

    return None


@dataclasses.dataclass
class NormalizedReviewResult:
    schema_version: int
    head_sha: str
    base_sha: str
    execution_sha: str
    code_assessment: str
    verification: str
    candidate_currentness: str
    merge_readiness: str
    material_findings: list[dict[str, Any]]
    evidence: Any
    validation_errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "execution_sha": self.execution_sha,
            "code_assessment": self.code_assessment,
            "verification": self.verification,
            "candidate_currentness": self.candidate_currentness,
            "merge_readiness": self.merge_readiness,
            "material_findings": self.material_findings,
            "evidence": self.evidence,
            "validation_errors": self.validation_errors,
        }


def _run_git(args: list[str], cwd: pathlib.Path | str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def resolve_execution_head(cwd: pathlib.Path | str | None = None) -> str:
    """Actual execution-worktree identity (pinned checkout).

    Callers must keep services/tests pinned to this immutable worktree and
    inspect other revisions via git objects or a separate worktree rather
    than switching the active execution checkout.
    """
    try:
        res = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    except OSError:
        return ""
    if res.returncode != 0:
        return ""
    return res.stdout.strip()


def is_ancestor(ancestor_sha: str, descendant_sha: str, cwd: pathlib.Path | str | None = None) -> bool:
    """Allowed head/execution relationship: execution equals head, or the
    test-merge execution commit contains the PR head as an ancestor."""
    if not ancestor_sha or not descendant_sha:
        return False
    if ancestor_sha.lower() == descendant_sha.lower():
        return True
    try:
        res = _run_git(["merge-base", "--is-ancestor", ancestor_sha, descendant_sha], cwd=cwd)
    except OSError:
        return False
    return res.returncode == 0


def evaluate_probe_response(status_code: int | None, body: str | None = None, *, require_agent_ok: bool = False) -> bool:
    """Shared readiness predicate for review-service probes.

    HTTP success alone (2xx) is not readiness for the agent: the agent
    readiness contract is `status == "ok"` with HTTP 200. A degraded 503
    body that still contains `"qdrant":true` (e.g. reembed_required) must
    not count as ready.
    """
    if status_code is None or not (200 <= status_code < 300):
        return False
    if not require_agent_ok:
        return True
    if not body:
        return False
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    return parsed.get("status") == "ok"


def validate_review_payload(
    raw_payload: dict[str, Any] | None,
    expected_head: str | None = None,
    expected_base: str | None = None,
    expected_execution: str | None = None,
    manifest: dict[str, Any] | None = None,
    check_git: bool = False,
    parse_error: str | None = None,
    cwd: pathlib.Path | str | None = None,
) -> NormalizedReviewResult:
    validation_errors: list[str] = []

    if parse_error:
        validation_errors.append(parse_error)

    if raw_payload is None:
        if not validation_errors:
            validation_errors.append("Review input payload is missing or unreadable")
        return NormalizedReviewResult(
            schema_version=SCHEMA_VERSION,
            head_sha=expected_head or "",
            base_sha=expected_base or "",
            execution_sha=expected_execution or "",
            code_assessment=CodeAssessment.INCOMPLETE.value,
            verification=VerificationStatus.INCOMPLETE.value,
            candidate_currentness=CandidateCurrentness.UNVERIFIED.value,
            merge_readiness=MergeReadiness.NOT_READY.value,
            material_findings=[],
            evidence={},
            validation_errors=validation_errors,
        )

    # Schema version check
    s_ver = raw_payload.get("schema_version")
    if s_ver != SCHEMA_VERSION:
        validation_errors.append(f"Invalid schema_version {s_ver!r}; expected {SCHEMA_VERSION}")

    # Commit SHAs
    head_sha = str(raw_payload.get("head_sha", "")).strip()
    base_sha = str(raw_payload.get("base_sha", "")).strip()
    execution_sha = str(raw_payload.get("execution_sha", "")).strip()

    if not head_sha:
        validation_errors.append("head_sha is missing or empty")
    if not base_sha:
        validation_errors.append("base_sha is missing or empty")
    if not execution_sha:
        validation_errors.append("execution_sha is missing or empty")

    # Enum validations
    code_raw = raw_payload.get("code_assessment")
    if code_raw in {e.value for e in CodeAssessment}:
        code_assessment = code_raw
    else:
        validation_errors.append(f"Invalid code_assessment {code_raw!r}")
        code_assessment = CodeAssessment.INCOMPLETE.value

    ver_raw = raw_payload.get("verification")
    if ver_raw in {e.value for e in VerificationStatus}:
        verification = ver_raw
    else:
        validation_errors.append(f"Invalid verification {ver_raw!r}")
        verification = VerificationStatus.INCOMPLETE.value

    curr_raw = raw_payload.get("candidate_currentness")
    if curr_raw in {e.value for e in CandidateCurrentness}:
        candidate_currentness = curr_raw
    else:
        validation_errors.append(f"Invalid candidate_currentness {curr_raw!r}")
        candidate_currentness = CandidateCurrentness.UNVERIFIED.value

    readiness_claimed = raw_payload.get("merge_readiness")
    if readiness_claimed in {e.value for e in MergeReadiness}:
        merge_readiness = readiness_claimed
    else:
        validation_errors.append(f"Invalid merge_readiness {readiness_claimed!r}")
        merge_readiness = MergeReadiness.NOT_READY.value

    # Findings validation
    findings_raw = raw_payload.get("material_findings")
    material_findings: list[dict[str, Any]] = []
    has_unresolved_findings = False

    if isinstance(findings_raw, list):
        for idx, f in enumerate(findings_raw):
            if not isinstance(f, dict):
                validation_errors.append(f"material_findings[{idx}] is not an object")
                has_unresolved_findings = True
                continue
            f_id = str(f.get("id", f"FINDING-{idx+1}")).strip()
            disp = str(f.get("disposition", "")).strip().lower()
            if disp not in RESOLVED_DISPOSITIONS:
                has_unresolved_findings = True
            material_findings.append({
                "id": f_id,
                "disposition": disp,
                "description": str(f.get("description", "")),
                "location": str(f.get("location", "")),
                "impact": str(f.get("impact", "")),
            })
    elif findings_raw is None:
        validation_errors.append("material_findings field is missing")
    else:
        validation_errors.append("material_findings must be a list")
        has_unresolved_findings = True

    if "evidence" not in raw_payload:
        validation_errors.append("evidence field is missing")
        evidence: Any = {}
    else:
        evidence = raw_payload.get("evidence")
        if not isinstance(evidence, dict):
            validation_errors.append("evidence must be an object")
            evidence = {}

    # Git attribution checks
    # Use manifest if provided
    exp_h = expected_head or (manifest.get("head_sha") if manifest else None)
    exp_b = expected_base or (manifest.get("base_sha") if manifest else None)
    exp_e = expected_execution or (manifest.get("execution_sha") if manifest else None)

    if exp_h and head_sha.lower() != exp_h.lower():
        candidate_currentness = CandidateCurrentness.STALE.value
        validation_errors.append(f"head_sha {head_sha} does not match expected {exp_h}")

    if exp_b and base_sha.lower() != exp_b.lower():
        candidate_currentness = CandidateCurrentness.STALE.value
        validation_errors.append(f"base_sha {base_sha} does not match expected {exp_b}")

    if exp_e and execution_sha.lower() != exp_e.lower():
        candidate_currentness = CandidateCurrentness.UNVERIFIED.value
        validation_errors.append(f"execution_sha {execution_sha} does not match expected {exp_e}")

    if manifest and (manifest.get("dirty") or manifest.get("dirty_tree")):
        candidate_currentness = CandidateCurrentness.UNVERIFIED.value
        validation_errors.append("Manifest records dirty working tree during candidate execution")

    if check_git:
        try:
            # Check commit existence in git object database. Existence alone
            # never proves execution: the pinned worktree HEAD binding below
            # is what ties the claimed SHAs to the code actually checked out.
            for sha, name in [(head_sha, "head_sha"), (base_sha, "base_sha"), (execution_sha, "execution_sha")]:
                if sha:
                    chk = _run_git(["rev-parse", "--verify", f"{sha}^{{commit}}"], cwd=cwd)
                    if chk.returncode != 0:
                        candidate_currentness = CandidateCurrentness.UNVERIFIED.value
                        validation_errors.append(f"Commit {name} ({sha}) not found in git repository")

            # Bind the candidate to the actual worktree revision. The pinned
            # reviewer CLI deliberately checks out the PR head before the model
            # runs (`Checking out local branch...`), while tests and services run
            # on the test-merge execution commit: both are legitimate candidate
            # identities. Any other clean checkout (e.g. the base) must not pass
            # merely because all claimed commits exist as git objects.
            actual_head = resolve_execution_head(cwd=cwd)
            if not actual_head:
                candidate_currentness = CandidateCurrentness.UNVERIFIED.value
                validation_errors.append("Unable to resolve execution worktree HEAD for attribution check")
            else:
                allowed_worktrees = {sha.lower() for sha in (head_sha, execution_sha) if sha}
                if actual_head.lower() not in allowed_worktrees:
                    candidate_currentness = CandidateCurrentness.UNVERIFIED.value
                    validation_errors.append(
                        f"Execution worktree HEAD ({actual_head}) matches neither the claimed "
                        f"head_sha ({head_sha}) nor execution_sha ({execution_sha}); a clean "
                        "checkout of another revision cannot attest the candidate"
                    )
                # Allowed head/execution relationship: execution equals head,
                # or execution is a test-merge commit containing head.
                if head_sha and execution_sha and not is_ancestor(head_sha, execution_sha, cwd=cwd):
                    candidate_currentness = CandidateCurrentness.UNVERIFIED.value
                    validation_errors.append(
                        f"head_sha ({head_sha}) is not an ancestor of execution_sha ({execution_sha}); "
                        "execution must equal head or be a test-merge containing head"
                    )

            # Recheck relevant dirty-tree state before accepting reviewer output.
            st = _run_git(["status", "--porcelain", "-uno"], cwd=cwd)
            if st.stdout.strip():
                candidate_currentness = CandidateCurrentness.UNVERIFIED.value
                validation_errors.append("Working tree contains uncommitted changes")
        except OSError as e:
            validation_errors.append(f"Git check failed: {e}")

    # Anti-Forging & Override Rules
    # Overrides merge_readiness to NOT_READY if any criteria fail
    must_be_not_ready = (
        code_assessment != CodeAssessment.ACCEPTABLE.value
        or verification != VerificationStatus.COMPLETE.value
        or candidate_currentness != CandidateCurrentness.CURRENT.value
        or has_unresolved_findings
        or bool(validation_errors)
    )

    if must_be_not_ready:
        if merge_readiness == MergeReadiness.READY_FOR_MAINTAINER.value:
            validation_errors.append(
                "Overriding claimed readiness 'ready_for_maintainer' to 'not_ready' due to unmet acceptance criteria"
            )
        merge_readiness = MergeReadiness.NOT_READY.value

    return NormalizedReviewResult(
        schema_version=SCHEMA_VERSION,
        head_sha=head_sha,
        base_sha=base_sha,
        execution_sha=execution_sha,
        code_assessment=code_assessment,
        verification=verification,
        candidate_currentness=candidate_currentness,
        merge_readiness=merge_readiness,
        material_findings=material_findings,
        evidence=evidence,
        validation_errors=validation_errors,
    )


# -----------------------------------------------------------------------------
# Enforceable Candidate Acceptance Summary (R4, §5.4)
# -----------------------------------------------------------------------------

LANE_REQUIREMENTS_BY_PROFILE: dict[str, set[str]] = {
    # Offline base is the minimal applicable set; tooling/tests categories add
    # lint_and_types and unit_tests in build_acceptance_summary. Prose-only
    # changes never run the path-filtered ci/pytest lanes by policy.
    ProfileName.OFFLINE.value: {"context_check", "reviewer"},
    ProfileName.DEPLOY.value: {"context_check", "lint_and_types", "unit_tests", "packaging", "reviewer"},
    ProfileName.STORAGE.value: {"context_check", "lint_and_types", "unit_tests", "simulation", "gate_l1", "reviewer"},
    ProfileName.HTTP.value: {"context_check", "lint_and_types", "unit_tests", "simulation", "reviewer"},
    ProfileName.TRACING.value: {"context_check", "lint_and_types", "unit_tests", "reviewer"},
    ProfileName.FULL.value: {"context_check", "lint_and_types", "unit_tests", "simulation", "gate_l1", "reviewer"},
}

# agent_probes and eval_retrieval have no CI producer yet: live agent probes
# stay a reviewer-side obligation (HTTP/lifecycle row) and semantic retrieval
# evaluation is mode/venue-gated. They are reported as UNSELECTED unless a
# producer supplies a status, and never block acceptance by themselves.
ALL_KNOWN_LANES = [
    "context_check",
    "lint_and_types",
    "unit_tests",
    "simulation",
    "gate_l1",
    "packaging",
    "agent_probes",
    "eval_retrieval",
    "reviewer",
]


@dataclasses.dataclass
class LaneEvaluation:
    name: str
    required: bool
    reported_status: str | None
    state: LaneState
    notes: str = ""

    @property
    def blocks_readiness(self) -> bool:
        return self.state in {
            LaneState.SELECTED_FAILED,
            LaneState.SELECTED_SKIPPED,
            LaneState.SELECTED_MISSING,
        }


def evaluate_lane(
    lane_name: str,
    required: bool,
    reported_status: str | None,
) -> LaneEvaluation:
    if not required:
        state = LaneState.UNSELECTED
        notes = "Not required for current profile"
        return LaneEvaluation(lane_name, required, reported_status, state, notes)

    if reported_status is None:
        state = LaneState.SELECTED_MISSING
        notes = "Required lane was not executed or reported (MISSING)"
        return LaneEvaluation(lane_name, required, reported_status, state, notes)

    norm_status = reported_status.strip().lower()
    if norm_status in {"success", "passed", "pass", "0"}:
        state = LaneState.SELECTED_PASSED
        notes = "Completed successfully"
    elif norm_status in {"skipped", "cancelled", "canceled", "skip"}:
        state = LaneState.SELECTED_SKIPPED
        notes = "Skipped in CI (anti-skip violation: skipped required obligations block acceptance)"
    else:
        state = LaneState.SELECTED_FAILED
        notes = f"Failed with status: {reported_status}"

    return LaneEvaluation(lane_name, required, reported_status, state, notes)


@dataclasses.dataclass
class CandidateAcceptanceSummary:
    profile: str
    head_sha: str
    base_sha: str
    execution_sha: str
    lanes: list[LaneEvaluation]
    review: NormalizedReviewResult | None
    all_prerequisites_met: bool
    recommended_readiness: str
    markdown_report: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "profile": self.profile,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "execution_sha": self.execution_sha,
            "all_prerequisites_met": self.all_prerequisites_met,
            "recommended_readiness": self.recommended_readiness,
            "lanes": [
                {
                    "name": l.name,
                    "required": l.required,
                    "reported_status": l.reported_status,
                    "state": l.state.value,
                    "notes": l.notes,
                }
                for l in self.lanes
            ],
            "review": self.review.to_dict() if self.review else None,
            "maintainer_authority": {
                "prerequisite_status": "ALL_MET" if self.all_prerequisites_met else "UNMET_OBLIGATIONS",
                "recommended_readiness": self.recommended_readiness,
                "maintainer_decision": "pending",
                "maintainer": None,
                "rule": "Agents never merge PRs or modify repository access rules; human maintainer decision required",
            },
        }


def build_acceptance_summary(
    manifest: dict[str, Any],
    lane_statuses: dict[str, str],
    review: NormalizedReviewResult | None = None,
) -> CandidateAcceptanceSummary:
    # Profile string or dict
    raw_profile = manifest.get("profile", ProfileName.OFFLINE.value)
    if isinstance(raw_profile, dict):
        profile_name = raw_profile.get("name", ProfileName.OFFLINE.value)
    else:
        profile_name = str(raw_profile)

    required_lanes = LANE_REQUIREMENTS_BY_PROFILE.get(
        profile_name,
        LANE_REQUIREMENTS_BY_PROFILE[ProfileName.FULL.value],
    )

    # Tooling/tests changes always owe the deterministic Python lanes. This is
    # additive on top of the profile sets (deploy already requires them).
    matched_cats = manifest.get("matched_categories", [])
    if "tooling" in matched_cats or "tests" in matched_cats:
        required_lanes = set(required_lanes) | {"lint_and_types", "unit_tests"}

    # Deploy always contributes the packaging obligation, even when the
    # cross-layer union selects the `full` profile (whose base set has no
    # packaging lane). A union of risks must never reduce verification.
    if any(category in matched_cats for category in ("deploy", "unclassified", "unclassified_empty")):
        required_lanes = set(required_lanes) | {"packaging"}

    head_sha = str(manifest.get("head_sha", ""))
    base_sha = str(manifest.get("base_sha", ""))
    execution_sha = str(manifest.get("execution_sha", ""))

    lanes_to_evaluate = list(ALL_KNOWN_LANES)
    for extra in lane_statuses:
        if extra not in lanes_to_evaluate:
            lanes_to_evaluate.append(extra)

    lane_evaluations: list[LaneEvaluation] = []
    blocking_lanes: list[LaneEvaluation] = []

    for name in lanes_to_evaluate:
        req = name in required_lanes
        status = lane_statuses.get(name)
        # If lane is 'reviewer', the validated candidate-bound review result
        # is authoritative. Workflow execution success alone is never code
        # approval: a successful reviewer job can return changes_required /
        # not_ready, and a missing normalized review blocks when reviewer
        # acceptance is required.
        if name == "reviewer" and req:
            if review is None:
                status = None
            elif review.merge_readiness == MergeReadiness.READY_FOR_MAINTAINER.value:
                status = "success"
            else:
                status = "failure"
        elif name == "reviewer" and review:
            if review.merge_readiness == MergeReadiness.READY_FOR_MAINTAINER.value:
                status = "success"
            else:
                status = "failure"

        evaluation = evaluate_lane(name, req, status)
        lane_evaluations.append(evaluation)
        if evaluation.blocks_readiness:
            blocking_lanes.append(evaluation)

    all_prereqs_met = len(blocking_lanes) == 0
    if review and review.merge_readiness != MergeReadiness.READY_FOR_MAINTAINER.value:
        all_prereqs_met = False

    recommended_readiness = (
        MergeReadiness.READY_FOR_MAINTAINER.value
        if all_prereqs_met
        else MergeReadiness.NOT_READY.value
    )

    # Build Markdown Summary
    md_lines: list[str] = [
        "## Candidate Acceptance Summary",
        "",
        f"- **Selected Profile**: `{profile_name}`",
        f"- **Head SHA**: `{head_sha}`",
        f"- **Base SHA**: `{base_sha}`",
        f"- **Execution SHA**: `{execution_sha}`",
        f"- **Prerequisite Obligations**: `{'ALL_MET' if all_prereqs_met else 'UNMET_OBLIGATIONS'}`",
        f"- **Recommended Readiness**: `{recommended_readiness}`",
        "",
        "### Verification Lanes",
        "| Lane | Required | Reported Status | Evaluation | Notes |",
        "|---|---|---|---|---|",
    ]

    for l in lane_evaluations:
        status_str = l.reported_status if l.reported_status is not None else "*missing*"
        md_lines.append(
            f"| `{l.name}` | {'Yes' if l.required else 'No'} | `{status_str}` | **{l.state.value}** | {l.notes} |"
        )

    md_lines.append("")
    md_lines.append("### Maintainer Merge Authority")
    md_lines.append(f"- **Prerequisite Obligations**: `{'ALL_MET' if all_prereqs_met else 'UNMET_OBLIGATIONS'}`")
    md_lines.append(f"- **Recommended Readiness**: `{recommended_readiness}`")
    md_lines.append("- **Maintainer Merge Decision**: `pending`")
    md_lines.append("- **Maintainer**: `@maintainer`")
    md_lines.append(
        "- **Rationale**: Agents never merge pull requests or modify repository access rules. "
        "The automated acceptance summary validates required verification obligations fail-closed; "
        "final merge authority strictly remains with human maintainers."
    )
    md_lines.append("")

    markdown_report = "\n".join(md_lines)

    return CandidateAcceptanceSummary(
        profile=profile_name,
        head_sha=head_sha,
        base_sha=base_sha,
        execution_sha=execution_sha,
        lanes=lane_evaluations,
        review=review,
        all_prerequisites_met=all_prereqs_met,
        recommended_readiness=recommended_readiness,
        markdown_report=markdown_report,
    )


# -----------------------------------------------------------------------------
# CLI Entrypoints
# -----------------------------------------------------------------------------

def changed_paths(base: str | None, head: str = "HEAD", *, cwd: pathlib.Path | None = None) -> list[str]:
    """Read NUL-delimited path identities, retaining both sides of a rename.

    Missing/unreadable data yields the existing full unknown-impact selection.
    Never execute a candidate diff driver or normalize whitespace in filenames.
    """
    command = ["git", "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "--find-renames"]
    if base:
        command.append(f"{base}...{head}")
    command.append("--")
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, check=False)
        if result.returncode:
            return []
        fields = result.stdout.decode("utf-8").split("\0")
        if fields[-1] != "":
            return []
        fields.pop()
        paths = []
        while fields:
            status = fields.pop(0)
            count = 2 if status.startswith(("R", "C")) else 1
            if not re.fullmatch(r"(?:[ACDMRTUXB]|[RC][0-9]+)", status) or len(fields) < count:
                return []
            for _ in range(count):
                name = fields.pop(0)
                if not name:
                    return []
                paths.append(name)
        return paths
    except (OSError, UnicodeError):
        return []


def cmd_profile(args: argparse.Namespace) -> int:
    # 1. Resolve paths
    if args.files:
        paths = args.files
    else:
        paths = changed_paths(args.base, args.head or "HEAD")

    decision = classify_paths(paths)
    manifest = generate_candidate_manifest(
        decision,
        head_sha=args.head,
        base_sha=args.base,
        execution_sha=args.execution,
    )

    out_path = pathlib.Path(args.out or "candidate-manifest.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Handle GitHub Output
    gh_output_path = args.github_output or os.environ.get("GITHUB_OUTPUT")
    if gh_output_path and os.path.exists(os.path.dirname(os.path.abspath(gh_output_path))):
        with open(gh_output_path, "a", encoding="utf-8") as f:
            f.write(f"profile={decision.profile.value}\n")
            f.write(f"needs_qdrant={str(decision.needs_qdrant).lower()}\n")
            f.write(f"needs_vllm={str(decision.needs_vllm).lower()}\n")
            f.write(f"needs_jaeger={str(decision.needs_jaeger).lower()}\n")
            f.write(f"needs_agent={str(decision.needs_agent).lower()}\n")
            f.write(f"test_focus={decision.test_focus}\n")

    if not args.quiet:
        print(decision.profile.value)

    return 0


def cmd_validate_review(args: argparse.Namespace) -> int:
    review_path = getattr(args, "review", None) or getattr(args, "input", None)
    manifest_path = getattr(args, "manifest", None)

    raw_payload: dict[str, Any] | None = None
    parse_error: str | None = None

    if review_path:
        p = pathlib.Path(review_path)
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8")
                raw_payload = extract_review_json(content)
                if raw_payload is None:
                    parse_error = f"No valid JSON review block found in {review_path}"
            except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
                parse_error = f"Error reading review file {review_path}: {e}"
        else:
            parse_error = f"Review file not found: {review_path}"
    else:
        parse_error = "No review file specified (--review)"

    manifest: dict[str, Any] | None = None
    if manifest_path:
        mp = pathlib.Path(manifest_path)
        if mp.exists():
            try:
                manifest = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
                parse_error = f"Error reading manifest {manifest_path}: {e}"
        else:
            parse_error = f"Manifest file not found: {manifest_path}"

    normalized = validate_review_payload(
        raw_payload=raw_payload,
        expected_head=args.expected_head,
        expected_base=args.expected_base,
        expected_execution=args.expected_execution,
        manifest=manifest,
        check_git=args.check_git,
        parse_error=parse_error,
    )

    out_json = json.dumps(normalized.to_dict(), indent=2)

    if args.out:
        op = pathlib.Path(args.out)
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(out_json + "\n", encoding="utf-8")

    if not args.quiet:
        print(out_json)

    if args.require_payload and (raw_payload is None or normalized.validation_errors):
        # Missing/malformed/mis-attributed reviewer output is a failed review
        # execution. A valid `changes_required` verdict is NOT a failure here.
        return 2

    if args.check:
        if normalized.merge_readiness == MergeReadiness.READY_FOR_MAINTAINER.value:
            return 0
        return 1

    return 0


def cmd_summarize_acceptance(args: argparse.Namespace) -> int:
    manifest_path = pathlib.Path(args.manifest)
    if not manifest_path.exists():
        sys.stderr.write(f"Manifest file not found: {args.manifest}\n")
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
        sys.stderr.write(f"Failed to parse manifest {args.manifest}: {e}\n")
        return 1

    # Aggregate lane statuses
    lane_statuses: dict[str, str] = {}
    if args.ci_results:
        cr_path = pathlib.Path(args.ci_results)
        if cr_path.exists():
            try:
                lane_statuses.update(json.loads(cr_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
                sys.stderr.write(f"Failed to parse ci-results {args.ci_results}: {e}\n")

    if args.lane:
        for item in args.lane:
            if ":" in item:
                k, _, v = item.partition(":")
                lane_statuses[k.strip()] = v.strip()

    # Process review if provided
    review: NormalizedReviewResult | None = None
    review_path = getattr(args, "review", None) or getattr(args, "review_result", None)
    if review_path:
        rp = pathlib.Path(review_path)
        if rp.exists():
            try:
                content = rp.read_text(encoding="utf-8")
                raw_payload = extract_review_json(content)
                review = validate_review_payload(
                    raw_payload=raw_payload,
                    manifest=manifest,
                )
            except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError):
                review = validate_review_payload(
                    raw_payload=None,
                    manifest=manifest,
                    parse_error=f"Error reading review file {review_path}",
                )
        else:
            review = validate_review_payload(
                raw_payload=None,
                manifest=manifest,
                parse_error=f"Review file not found {review_path}",
            )

    summary = build_acceptance_summary(
        manifest=manifest,
        lane_statuses=lane_statuses,
        review=review,
    )

    out_path = getattr(args, "out", None) or getattr(args, "out_json", None)
    if out_path:
        p = pathlib.Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary.to_dict(), indent=2) + "\n", encoding="utf-8")

    out_md = args.out_markdown or os.environ.get("GITHUB_STEP_SUMMARY")
    if out_md:
        try:
            mp = pathlib.Path(out_md)
            mp.parent.mkdir(parents=True, exist_ok=True)
            with open(mp, "a", encoding="utf-8") as f:
                f.write(summary.markdown_report + "\n")
        except OSError:
            pass

    if not args.quiet:
        print(summary.markdown_report)

    if args.check:
        return 0 if summary.all_prerequisites_met else 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review Tooling for Increment B (Issue #411)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. profile
    p_prof = subparsers.add_parser("profile", help="Select profile and emit candidate runtime manifest")
    p_prof.add_argument("--base", help="PR base commit SHA")
    p_prof.add_argument("--head", help="PR head commit SHA")
    p_prof.add_argument("--execution", help="CI test-merge execution commit SHA")
    p_prof.add_argument("--files", nargs="*", help="Explicit list of changed file paths to classify")
    p_prof.add_argument("--out", default="candidate-manifest.json", help="Path to write manifest JSON")
    p_prof.add_argument("--github-output", help="Path to GITHUB_OUTPUT file")
    p_prof.add_argument("--quiet", action="store_true", help="Do not print profile to stdout")
    p_prof.set_defaults(func=cmd_profile)

    # 2. validate-review
    p_val = subparsers.add_parser("validate-review", help="Validate and normalize review result against Schema v1")
    p_val.add_argument("--review", "--input", help="Path to review JSON or Markdown file")
    p_val.add_argument("--manifest", help="Path to candidate runtime manifest JSON")
    p_val.add_argument("--expected-head", help="Expected candidate HEAD commit SHA")
    p_val.add_argument("--expected-base", help="Expected base commit SHA")
    p_val.add_argument("--expected-execution", help="Expected execution SHA")
    p_val.add_argument("--check-git", action="store_true", help="Validate commits and cleanliness with git")
    p_val.add_argument("--out", help="Path to write normalized review JSON")
    p_val.add_argument(
        "--require-payload",
        action="store_true",
        help="Exit 2 unless the payload parsed and passed structural/attribution validation",
    )
    p_val.add_argument("--check", action="store_true", help="Exit 0 if ready_for_maintainer, 1 if not_ready")
    p_val.add_argument("--quiet", action="store_true", help="Do not print normalized JSON to stdout")
    p_val.set_defaults(func=cmd_validate_review)

    # 3. summarize-acceptance
    p_sum = subparsers.add_parser("summarize-acceptance", help="Evaluate PR verification obligations into summary")
    p_sum.add_argument("--manifest", required=True, help="Path to candidate-manifest.json")
    p_sum.add_argument("--review", "--review-result", help="Path to review JSON file")
    p_sum.add_argument("--ci-results", help="Path to JSON with CI lane statuses")
    p_sum.add_argument("--lane", action="append", help="Lane status in name:status format (can be repeated)")
    p_sum.add_argument("--out", "--out-json", help="Path to write acceptance summary JSON")
    p_sum.add_argument("--out-markdown", help="Path to write acceptance summary Markdown")
    p_sum.add_argument("--check", action="store_true", help="Exit 0 if all obligations met, 1 if blocked")
    p_sum.add_argument("--quiet", action="store_true", help="Do not print markdown summary to stdout")
    p_sum.set_defaults(func=cmd_summarize_acceptance)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
