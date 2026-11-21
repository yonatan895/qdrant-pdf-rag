"""Tests for scripts/gap/allowlist.py (GH#1207 Rev 2 §3a, D2)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.gap import allowlist

ROOT = Path(__file__).resolve().parents[1]
_COPY_LINE = re.compile(r"^\s*COPY\s+(.+)$", re.IGNORECASE)


def _containerfile_copy_sources(path: Path) -> list[str]:
    sources: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _COPY_LINE.match(line)
        if not match:
            continue
        tokens = match.group(1).split()
        if any(tok.startswith("--from=") for tok in tokens):
            continue
        positional = [tok for tok in tokens if not tok.startswith("--")]
        if len(positional) >= 2:
            for src in positional[:-1]:
                sources.append(src.rstrip("/"))
    return sources


def _pack_sh_tree_inputs(pack_sh: Path) -> list[str]:
    text = pack_sh.read_text(encoding="utf-8")
    inputs: set[str] = set()
    for pattern in (
        r"charts/qdrant-\*\.tgz",
        r"scripts/tools/task-pin\.txt",
        r"scripts/airgap/reference-docs\.json",
        r"scripts/airgap/bootstrap\.sh",
        r"scripts/license_inventory\.py",
        r"scripts/image_inventory\.py",
        r"scripts/dependency_lock\.py",
        r"images\.txt",
        r"licenses/inventory\.json",
    ):
        if re.search(pattern, text):
            inputs.add(pattern.replace("\\", ""))
    archive_match = re.search(r"git archive[^\n]*HEAD -- ([^\n]+(?:\\\n[^\n]+)*)", text)
    if archive_match:
        raw = archive_match.group(1).replace("\\\n", " ")
        for tok in raw.split():
            if tok in ("||", "&&") or tok.startswith("die"):
                break
            cleaned = tok.strip("'\"")
            if cleaned.startswith(":(glob)"):
                cleaned = cleaned[len(":(glob)"):]
            inputs.add(cleaned.rstrip("/*"))
    return sorted(inputs)


def test_audit_repo_coverage_passes_on_this_checkout():
    """Rev 2 §3a: every Containerfile COPY source and pack.sh input is refused or
    carries a declared exception, and every tracked top-level entry is decided."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout.decode("utf-8", "replace")
    top_levels = {entry.split("/", 1)[0] for entry in listed.split("\x00") if entry}
    assert sorted(top_levels - allowlist.TOP_LEVEL_DECIDED) == []

    for cf_name in ("images/Containerfile.agent", "images/Containerfile.ingest"):
        for src in _containerfile_copy_sources(ROOT / cf_name):
            if "/" + src in allowlist.DECLARED_EXCEPTIONS:
                continue
            assert allowlist.classify_path(src).verdict == "refused", (cf_name, src)

    for inp in _pack_sh_tree_inputs(ROOT / "scripts/airgap/pack.sh"):
        top = inp.split("/", 1)[0]
        if "/" + inp in allowlist.DECLARED_EXCEPTIONS or "/" + top in allowlist.DECLARED_EXCEPTIONS:
            continue
        sample = inp.replace("*", "sample")
        assert allowlist.classify_path(sample).verdict == "refused", inp


@pytest.mark.parametrize("path", [
    "gap/README.md",
    "gap/patches.md",
    "gap/config/airgap.env",
    "gap/records/2026-11-21-gap-release-1.md",
    ".gitlab/CODEOWNERS",
    ".gitlab/merge_request_templates/default.md",
    "charts/mainframe-rag/values.yaml",
    "charts/mainframe-rag/templates/agent-deployment.yaml",
    "deploy/workspace/bastion.template.yaml",
    "deploy/workspace/contract.json",
    "airgap.env.example",
    "Taskfile.yml",
    "taskfiles/airgap.yml",
    "tests/test_gap_allowlist.py",
    "evals/golden.jsonl",
    "benchmarks/baseline.json",
    "docs/deploy.md",
    "docs/adr/0009-gap-fork.md",
    "README.md",
    "ROADMAP.md",
    "AGENTS.md",
    "CLAUDE.md",
    # .dockerignore-excluded scripts do not enter app images
    "scripts/mock_vllm.py",
    "scripts/ci/observability_lane.sh",
    "scripts/gateway/strict_finish.py",
])
def test_allowed_no_layer_paths(path):
    result = allowlist.classify_path(path)
    assert result.verdict == "allowed-no-layer", result


@pytest.mark.parametrize("path", [
    "src/mainframe_rag/agent/app.py",
    "src/mainframe_rag/retrieve/query.py",
    "scripts/gap/release.py",
    "scripts/airgap/deploy.sh",
    "scripts/airgap/bootstrap.sh",
    "scripts/smoke_search.py",
])
def test_layer_replacement_paths(path):
    result = allowlist.classify_path(path)
    assert result.verdict == "requires-layer-replacement", result


@pytest.mark.parametrize("path", [
    # Explicit scripts/ exclusions
    "scripts/dependency_lock.py",
    "scripts/tools/task-pin.txt",
    "scripts/tools/helm-pin.txt",
    "scripts/tools/devkit-pin.txt",
    "scripts/airgap/reference-docs.json",
    "scripts/airgap/reference-docs-openshift.paths",
    # Build inputs / image pins / vendored charts / notices / unknown top-level
    "requirements.lock.txt",
    "requirements.dev.lock.txt",
    "requirements.build.lock.txt",
    "locks/cp314-linux-x86_64.json",
    "bm25-weights.sha256",
    "images.txt",
    "images/Containerfile.agent",
    "images/Containerfile.ingest",
    "images/Containerfile.devkit",
    "images/openwebui/Containerfile",
    "charts/qdrant-1.19.2.tgz",
    "charts/other/Chart.yaml",
    "licenses/inventory.json",
    "LICENSE",
    "LICENSE.qdrant-skills",
    "NOTICE.qdrant-skills",
    "integrations/openwebui/docs_pipe.py",
    "observability/otelcol.yaml",
    "pyproject.toml",
    ".dockerignore",
    ".gitlab-ci.yml",
    ".github/workflows/ci.yml",
    ".agents/skills/qdrant-sizing/SKILL.md",
    "site/index.html",
    # Unknown / future top-level path fails closed
    "brand_new_dir/file.txt",
    "NEW_TOP_LEVEL_FILE",
    # Unsafe / malformed paths
    "",
    "/etc/passwd",
    "../outside",
    "docs/../../etc/passwd",
    "docs\\windows.md",
    "docs/file\x00name",
])
def test_refused_paths(path):
    result = allowlist.classify_path(path)
    assert result.verdict == "refused", result


def test_classify_paths_groups_verdicts_and_cli_runs_under_python_dash_i():
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts/gap/allowlist.py"),
            "--json",
            "docs/deploy.md",
            "src/a.py",
            "requirements.lock.txt",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["all_allowed"] is False
    assert data["needs_layer_replacement"] is True
    assert [x["path"] for x in data["no_layer"]] == ["docs/deploy.md"]
    assert [x["path"] for x in data["layer_replacement"]] == ["src/a.py"]
    assert [x["path"] for x in data["refused"]] == ["requirements.lock.txt"]

    ok = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts/gap/allowlist.py"), "--json", "docs/deploy.md", "gap/README.md"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0
    assert json.loads(ok.stdout)["all_allowed"] is True
