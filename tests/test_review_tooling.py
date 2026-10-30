"""Unit tests for review tooling (Increment B of Issue #411).

Tests:
- Profile path classifier & service selector (R1, §5.1)
- Candidate runtime manifest generator (R1, §5.1)
- Review result schema version 1 validator & fail-closed logic (R3, §5.3)
- Git candidate attribution and anti-forging overrides (R3, §5.3)
- 5-state lane taxonomy and anti-skip enforcement (R4, §5.4)
- Maintainer merge authority preservation (R4, §5.4)
- CLI subcommands (profile, validate-review, summarize-acceptance)
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
import warnings
import xml.etree.ElementTree as ET
import zipfile
from typing import ClassVar
from unittest.mock import patch

import yaml
from scripts import ci_evidence
from scripts.acceptance import (
    VERIFICATION_INPUTS,
    GitHub,
    candidate_identity,
    changed_pr_paths,
    collect_acceptance,
    collect_native,
    main,
    post_review_template,
    publish_acceptance,
    recheck_current,
    require_current_base,
    review_template,
    review_template_comment,
    verification_inputs,
)
from scripts.acceptance_evidence import (
    PRODUCERS,
    artifact_members,
    normalize_native,
    paginate,
    unit_producers,
)
from scripts.ci_evidence import junit_bytes, junit_counts
from scripts.review_tooling import (
    CandidateCurrentness,
    CodeAssessment,
    LaneState,
    MergeReadiness,
    ProfileName,
    VerificationStatus,
    build_acceptance_summary,
    changed_paths,
    classify_paths,
    evaluate_lane,
    extract_review_json,
    required_lanes,
    validate_review_payload,
)
from scripts.unit_durations import durations
from scripts.unit_evidence import (
    INPUTS,
    assign_balanced,
    input_hashes,
    parse_durations,
    rule_record,
    run_shard,
    select_nodes,
    validate,
    validate_union,
)
from scripts.verifier_approval import (
    VerifierApprovalRequired,
    allowed_anchor_change,
    approved_catalogue,
    approved_inputs,
    comment_request,
    latest_decision,
    parse_command,
    record_decision,
    validated_decision,
)

from mainframe_rag.ingest.rules_version import _RULE_MODULES
from tests.helpers_tooling_local import CI_UNIT_SOURCE, approved_repository, pack_archive


class TestOpencodeWorkflowContract(unittest.TestCase):
    """The /oc comment agent runs only for trusted commenters (#588)."""

    def test_comment_trigger_requires_a_trusted_author_association(self):

        path = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/opencode.yml"
        condition = " ".join(yaml.safe_load(path.read_text())["jobs"]["gate"]["if"].split())
        # The pull_request branch is the only one allowed to skip the check.
        comment_branch = condition.split("github.event_name == 'pull_request' ||", 1)[1]
        self.assertIn("github.event.comment.author_association", comment_branch)
        self.assertIn("""fromJSON('["OWNER","MEMBER","COLLABORATOR"]')""", comment_branch)
        for untrusted in ("CONTRIBUTOR", "NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"):
            self.assertNotIn(untrusted, condition)
        # The check sits inside the same && chain as the /oc match, before it.
        self.assertLess(comment_branch.index("author_association"), comment_branch.index("'/oc'"))


class TestProfileClassification(unittest.TestCase):
    """Risk-based profile selection and service mapping (§5.1)."""

    def _required(self, paths):

        decision = classify_paths(paths)
        return decision, required_lanes({"profile": decision.profile.value,
                                         "matched_categories": decision.matched_categories,
                                         "changed_paths": paths})

    def test_changed_paths_select_profile_and_services(self):
        offline, deploy = ProfileName.OFFLINE, ProfileName.DEPLOY
        cases = [
            (["README.md", "docs/testing.md", "ROADMAP.md", "docs/agent-workflow.md"], offline, []),
            (["scripts/check_agent_context.py", "scripts/review_tooling.py", "tests/test_agent_context.py",
              ".github/workflows/ci.yml", "Taskfile.yml", "taskfiles/quality.yml"], offline, []),
            (["scripts/gate_l1.py", "benchmarks/harness.json", ".gitlab-ci.yml"], offline, []),
            (["tests/test_config.py"], offline, []),
            (["Dockerfile", "images/Containerfile.agent", "pyproject.toml", "requirements.lock.txt",
              "scripts/airgap/deploy.sh", "charts/mainframe-rag/templates/agent-deployment.yaml",
              "docker-compose.ci.yml", "airgap.env.example", "tests/test_airgap_deploy_sh.py"], deploy, []),
            (["taskfiles/airgap.yml", "scripts/tools/run-task.sh", "scripts/tools/task-pin.txt"], deploy, []),
            (["tests/helpers_helm.py", "tests/helpers_image_inventory.py"], deploy, []),
            (["docs/testing.md", "Dockerfile"], deploy, []),
            (["src/mainframe_rag/ingest/publish.py", "scripts/qdrant_pin.py"], ProfileName.STORAGE, ["qdrant"]),
            (["src/mainframe_rag/retrieve/query.py"], ProfileName.STORAGE, ["qdrant"]),
            (["docs/testing.md", "src/mainframe_rag/ingest/publish.py"], ProfileName.STORAGE, ["qdrant"]),
            (["src/mainframe_rag/agent/app.py", "scripts/mock_vllm.py"], ProfileName.HTTP,
             ["agent", "qdrant", "vllm"]),
            (["src/mainframe_rag/tracing.py", "scripts/run_local_openobserve.sh"], ProfileName.TRACING, ["openobserve"]),
            (["src/mainframe_rag/ingest/publish.py", "src/mainframe_rag/agent/app.py"], ProfileName.FULL,
             ["agent", "qdrant", "vllm"]),
            (["src/mainframe_rag/ingest/publish.py", "src/mainframe_rag/agent/app.py",
              "src/mainframe_rag/tracing.py"], ProfileName.FULL, ["agent", "openobserve", "qdrant", "vllm"]),
            (["pyproject.toml", "src/mainframe_rag/ingest/publish.py"], ProfileName.FULL, ["qdrant"]),
        ]
        for paths, profile, services in cases:
            with self.subTest(paths=paths):
                decision = classify_paths(paths)
                self.assertEqual(decision.profile, profile)
                self.assertEqual(sorted(decision.services), services)
                self.assertEqual(decision.needs_qdrant, "qdrant" in services)
                self.assertEqual(decision.needs_openobserve, "openobserve" in services)

    def test_unmapped_unreadable_or_shared_changes_fail_closed_to_full(self):
        every_service = {"agent", "openobserve", "qdrant", "vllm"}
        # An empty change set means the diff could not be read (bad SHA, shallow
        # checkout); it must not silently downgrade to a docs review.
        for paths, category in (([], "unclassified_empty"), (["mystery.bin"], "unclassified"),
                                (["src/mainframe_rag/new_module.py"], None),
                                (["docs/generated.json"], None), (["docs/example.sh"], None)):
            with self.subTest(paths=paths):
                decision = classify_paths(paths)
                self.assertEqual(decision.profile, ProfileName.FULL)
                self.assertEqual(set(decision.services), every_service)
                if category:
                    self.assertIn(category, decision.matched_categories)
        for path in ("tests/conftest.py", "tests/fakes.py", "tests/ci_shard.py"):
            with self.subTest(path=path):
                decision = classify_paths([path])
                self.assertEqual(decision.profile, ProfileName.FULL)
                self.assertTrue({"deploy", "storage", "http", "tests"} <= set(decision.matched_categories))
        for manifest in ({}, {"profile": {}}, {"profile": "unknown"}):
            with self.subTest(manifest=manifest):

                self.assertTrue({"simulation", "packaging", "load", "ha", "agent_probes",
                                 "eval_retrieval", "unit_tests", "hazards"} <= required_lanes(manifest))

    def test_markdown_is_prose_unless_it_is_an_instruction_or_under_tests(self):
        # The prose resource boundary is strictly offline even under a service-owned tree.
        for path in ("evals/README.md", "src/mainframe_rag/ingest/README.md", "docs/explanation.md"):
            decision = classify_paths([path])
            self.assertEqual((decision.profile, decision.services, decision.matched_categories),
                             (ProfileName.OFFLINE, [], ["prose"]))
        decision = classify_paths(["tests/fixtures/notes.md"])
        self.assertEqual((decision.profile, decision.services), (ProfileName.OFFLINE, []))
        self.assertIn("tests", decision.matched_categories)
        for path in ("AGENTS.md", "src/AGENTS.override.md", ".agents/skills/example/SKILL.md", "docs/testing.md"):
            with self.subTest(path=path):
                decision = classify_paths([path])
                self.assertIn("tooling", decision.matched_categories)
                self.assertEqual(decision.services, [])

    def test_every_rule_module_change_selects_the_retrieval_eval_lane(self):
        """The lane list is derived from `_RULE_MODULES`, not hard-coded (#588)."""

        self.assertGreaterEqual(len(_RULE_MODULES), 8)
        for rel in _RULE_MODULES:
            path = f"src/mainframe_rag/{rel}"
            with self.subTest(path=path):
                self.assertTrue((pathlib.Path(__file__).resolve().parents[1] / path).is_file())
                _, lanes = self._required([path])
                self.assertIn("eval_retrieval", lanes)

    def test_simulation_only_test_helpers_select_the_simulation_lane(self):
        """#1033: helpers spawned/imported by the sim tier must select `simulation`."""
        for path in ("tests/helpers_ingest_crash_worker.py",
                     "tests/helpers_publication_lifecycle.py",
                     "tests/helpers_builder.py"):
            with self.subTest(path=path):
                _, lanes = self._required([path])
                self.assertIn("simulation", lanes)
        # A helper with no simulation consumer stays offline.
        self.assertNotIn("simulation", self._required(["tests/helpers_helm.py"])[1])

    def test_real_nginx_proxy_inputs_select_the_simulation_lane(self):
        """#1118 A5: the real-nginx battery runs only in the sim lane, so its own file and the
        config it runs must select `simulation` even though a chart change alone is `deploy`."""
        for path in ("tests/test_openwebui_proxy_nginx.py",
                     "charts/mainframe-rag/files/openwebui/proxy.nginx.conf"):
            with self.subTest(path=path):
                self.assertTrue((pathlib.Path(__file__).resolve().parents[1] / path).is_file())
                _, lanes = self._required([path])
                self.assertIn("simulation", lanes)
        # Another chart helper keeps the deploy profile without the sim lane.
        decision, lanes = self._required(["charts/mainframe-rag/files/openwebui/ca_trust.py"])
        self.assertEqual(decision.profile, ProfileName.DEPLOY)
        self.assertNotIn("simulation", lanes)

    def test_site_only_changes_are_docs_like_and_offline(self):
        for paths in (["site/index.html"], ["site/js/app.js", "site/data/x.json", "site/css/a.css"]):
            with self.subTest(paths=paths):
                decision, lanes = self._required(paths)
                self.assertEqual((decision.profile, decision.services), (ProfileName.OFFLINE, []))
                self.assertEqual(decision.matched_categories, ["prose"])
                self.assertNotIn("unclassified", decision.matched_categories)
                self.assertFalse({"simulation", "load", "ha", "packaging", "eval_retrieval"} & lanes)
        # A sibling that merely shares the prefix stays fail-closed.
        self.assertEqual(self._required(["sitemap/x.py"])[0].profile, ProfileName.FULL)

    def test_evaluation_package_and_delegates_select_executable_tooling_lanes(self):

        expected = {"context_check", "lint_and_types", "unit_tests", "hazards",
                    "simulation", "gate_l1"}
        for path in ("src/mainframe_rag/eval/__init__.py",
                     "src/mainframe_rag/eval/datasets.py",
                     "src/mainframe_rag/eval/retrieval.py",
                     "scripts/eval_retrieval.py"):
            with self.subTest(path=path):
                decision = classify_paths([path])
                self.assertEqual(decision.profile, ProfileName.OFFLINE)
                self.assertEqual(decision.services, [])
                manifest = {"profile": decision.profile.value,
                            "matched_categories": decision.matched_categories,
                            "changed_paths": [path]}
                self.assertEqual(required_lanes(manifest), expected)
                statuses = dict.fromkeys(expected, "success")
                self.assertTrue(build_acceptance_summary(manifest, statuses).all_prerequisites_met)
                for lane in expected:
                    for status in (None, "failure", "skipped", "cancelled", "unverified"):
                        missing = {key: value for key, value in statuses.items() if key != lane}
                        if status is not None:
                            missing[lane] = status
                        self.assertFalse(build_acceptance_summary(manifest, missing).all_prerequisites_met,
                                         (path, lane, status))

    def test_moved_evaluation_and_relocated_contracts_keep_every_obligation(self):
        # Both rename names are supplied by changed_pr_paths; retain the old
        # instrument and new package obligations in the same decision.
        moved = ["scripts/eval_retrieval.py", "src/mainframe_rag/eval/retrieval.py",
                 "src/mainframe_rag/eval/datasets.py", "tests/test_eval_scoring.py"]
        for extra, required in (
            ([], {"simulation", "gate_l1", "unit_tests", "hazards"}),
            (["src/mainframe_rag/retrieve/query.py"], {"eval_retrieval", "simulation", "gate_l1"}),
            (["images/Containerfile.agent"], {"packaging", "simulation", "gate_l1"}),
            (["src/mainframe_rag/agent/app.py"], {"agent_probes", "simulation", "gate_l1"}),
            (["src/mainframe_rag/new_module.py"], {"eval_retrieval", "packaging", "agent_probes"}),
        ):
            with self.subTest(extra=extra):
                decision, lanes = self._required(moved + extra)
                self.assertTrue(required <= lanes)
                if not extra:
                    self.assertNotIn("eval_retrieval", lanes)
                    self.assertNotIn("unclassified", decision.matched_categories)
        # Evaluation documentation never selects service lanes.
        decision, lanes = self._required(["src/mainframe_rag/eval/README.md"])
        self.assertEqual(decision.profile, ProfileName.OFFLINE)
        self.assertFalse({"simulation", "gate_l1", "eval_retrieval"} & lanes)
        for path in ("charts/qdrant-openshift.values.yaml", "scripts/airgap/collection-policy.env"):
            with self.subTest(path=path):
                decision, lanes = self._required([path])
                self.assertTrue({"ha", "packaging"} <= lanes)
                self.assertNotIn("unclassified", decision.matched_categories)

    def test_real_git_rename_retains_both_paths_and_literal_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            def git(*args):
                return subprocess.check_output(["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                                                "-c", "commit.gpgsign=false", *args], cwd=root, text=True).strip()
            git("init", "-q")
            old = "src/mainframe_rag/ingest/old name\r\n.py"
            new = "docs/moved name\r\n.md"
            (root / old).parent.mkdir(parents=True)
            (root / old).write_text("unchanged synthetic contents\n")
            git("add", "--", old)
            git("commit", "-qm", "original")
            base = git("rev-parse", "HEAD")
            (root / new).parent.mkdir(parents=True)
            git("mv", "--", old, new)
            git("commit", "-qm", "rename")
            self.assertEqual(changed_paths(base, cwd=root), [old, new])
            self.assertIn("storage", classify_paths(changed_paths(base, cwd=root)).matched_categories)
            self.assertEqual(changed_paths("nonexistent-ref", cwd=root), [])


def _review_payload(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "head_sha": "1" * 40,
        "base_sha": "2" * 40,
        "execution_sha": "3" * 40,
        "code_assessment": "acceptable",
        "verification": "complete",
        "candidate_currentness": "current",
        "merge_readiness": "ready_for_maintainer",
        "material_findings": [{"id": "FINDING-1", "disposition": "fixed-and-verified",
                               "description": "Clean up temporary file descriptor"}],
        "evidence": {"notes": "All unit tests and doctor checks pass."},
    }
    payload.update(overrides)
    return payload


def _review_manifest(profile: str, categories: list[str], dirty: bool = False) -> dict:
    return {"schema_version": 1, "head_sha": "1" * 40, "base_sha": "2" * 40,
            "execution_sha": "3" * 40, "profile": profile, "dirty": dirty,
            "matched_categories": categories}


def _ready_review():
    return validate_review_payload(
        _review_payload(material_findings=[], evidence={"notes": "ok"}),
        expected_head="1" * 40, expected_base="2" * 40, expected_execution="3" * 40)


def _not_ready_review():
    return validate_review_payload(
        _review_payload(code_assessment="changes_required", verification="incomplete",
                        merge_readiness="not_ready",
                        material_findings=[{"id": "F1", "disposition": "unresolved", "description": "open"}],
                        evidence={"notes": "blocked"}),
        expected_head="1" * 40, expected_base="2" * 40, expected_execution="3" * 40)


class TestReviewResultValidator(unittest.TestCase):
    """Schema v1 review result validation and fail-closed handling (§5.3)."""

    def _sample_valid_payload(self) -> dict:
        return _review_payload()

    def test_valid_payload_is_ready_in_every_accepted_embedding(self):
        payload = self._sample_valid_payload()
        result = validate_review_payload(
            payload, expected_head="1" * 40, expected_base="2" * 40, expected_execution="3" * 40)
        self.assertEqual(result.merge_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)
        self.assertEqual(result.code_assessment, CodeAssessment.ACCEPTABLE.value)
        self.assertEqual(result.verification, VerificationStatus.COMPLETE.value)
        self.assertEqual(result.candidate_currentness, CandidateCurrentness.CURRENT.value)
        self.assertEqual(len(result.validation_errors), 0)
        body = json.dumps(payload, indent=2)
        for text in (f"# Review\n\n<!-- review-result:start -->\n{body}\n<!-- review-result:end -->\n\nEnd.",
                     f"Here is the review record:\n\n```json\n{body}\n```\n"):
            extracted = extract_review_json(text)
            self.assertEqual(extracted["head_sha"], "1" * 40)
            self.assertEqual(validate_review_payload(extracted).merge_readiness,
                             MergeReadiness.READY_FOR_MAINTAINER.value)
        # No findings, or only resolved ones, is a valid outcome.
        for findings in ([], [{"id": "F1", "disposition": "fixed-and-verified"},
                              {"id": "F2", "disposition": "disproven-with-evidence"},
                              {"id": "F3", "disposition": "accepted-by-authorized-owner"}]):
            self.assertEqual(validate_review_payload(_review_payload(material_findings=findings)).merge_readiness,
                             MergeReadiness.READY_FOR_MAINTAINER.value)

    def test_missing_malformed_or_forged_payloads_fail_closed(self):
        missing = validate_review_payload(None)
        self.assertEqual(missing.code_assessment, CodeAssessment.INCOMPLETE.value)
        self.assertEqual(missing.verification, VerificationStatus.INCOMPLETE.value)
        self.assertEqual(missing.candidate_currentness, CandidateCurrentness.UNVERIFIED.value)
        self.assertEqual(missing.merge_readiness, MergeReadiness.NOT_READY.value)
        self.assertTrue(missing.validation_errors)

        parsed = extract_review_json("Not valid JSON: { broken ")
        self.assertIsNone(parsed)
        result = validate_review_payload(parsed, parse_error="Malformed JSON syntax")
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
        self.assertIn("Malformed JSON syntax", result.validation_errors)

        for field in ("head_sha", "material_findings", "evidence"):
            payload = self._sample_valid_payload()
            del payload[field]
            result = validate_review_payload(payload)
            self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value, field)
            self.assertTrue(any(field in err for err in result.validation_errors), field)
        for bad in ("all good", ["claim one proven"]):
            result = validate_review_payload(_review_payload(evidence=bad))
            self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
            self.assertTrue(any("evidence" in e for e in result.validation_errors))

        result = validate_review_payload(_review_payload(code_assessment="looks_good", verification="passed"))
        self.assertEqual(result.code_assessment, CodeAssessment.INCOMPLETE.value)
        self.assertEqual(result.verification, VerificationStatus.INCOMPLETE.value)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)

        # Claiming ready_for_maintainer while the assessment is changes_required.
        result = validate_review_payload(_review_payload(code_assessment="changes_required"))
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
        self.assertTrue(any("Overriding" in err for err in result.validation_errors))

    def test_unresolved_or_author_declined_findings_block_readiness(self):
        # Implementer declining is NOT acceptance per AGENTS.md / agent-workflow.md.
        for disposition in ("unresolved", "declined-by-author"):
            with self.subTest(disposition=disposition):
                payload = self._sample_valid_payload()
                payload["material_findings"].append(
                    {"id": "FINDING-2", "disposition": disposition, "description": "open"})
                self.assertEqual(validate_review_payload(payload).merge_readiness,
                                 MergeReadiness.NOT_READY.value)

    def test_obsolete_head_or_dirty_tree_is_not_current(self):
        stale = {"head_sha": "9" * 40, "base_sha": "2" * 40, "execution_sha": "3" * 40}
        result = validate_review_payload(self._sample_valid_payload(), manifest=stale)
        self.assertEqual(result.candidate_currentness, CandidateCurrentness.STALE.value)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
        dirty = {"head_sha": "1" * 40, "base_sha": "2" * 40, "execution_sha": "3" * 40, "dirty": True}
        result = validate_review_payload(self._sample_valid_payload(), manifest=dirty)
        self.assertEqual(result.candidate_currentness, CandidateCurrentness.UNVERIFIED.value)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)


class TestCandidateAcceptanceSummary(unittest.TestCase):
    """5-state lane taxonomy, anti-skip enforcement, and maintainer merge authority (§5.4)."""

    def _sample_manifest(self, profile: str = "offline", dirty: bool = False) -> dict:
        return _review_manifest(profile, ["prose"] if profile == "offline" else [profile], dirty)

    def test_lane_evaluation_states(self):
        cases = (("simulation", False, None, LaneState.UNSELECTED, False),
                 ("context_check", True, "success", LaneState.SELECTED_PASSED, False),
                 ("context_check", True, "failure", LaneState.SELECTED_FAILED, True),
                 ("simulation", True, "skipped", LaneState.SELECTED_SKIPPED, True),
                 ("reviewer", True, None, LaneState.SELECTED_MISSING, True))
        for name, required, status, state, blocks in cases:
            lane = evaluate_lane(name, required=required, reported_status=status)
            self.assertEqual((lane.state, lane.blocks_readiness), (state, blocks), (name, status))

    def test_selected_lane_that_did_not_pass_blocks_acceptance(self):
        storage_ok = {"context_check": "success", "lint_and_types": "success", "unit_tests": "success",
                      "simulation": "success", "gate_l1": "success", "reviewer": "success"}
        deploy_ok = {"context_check": "success", "lint_and_types": "success", "unit_tests": "success",
                     "packaging": "success", "reviewer": "success"}
        http_ok = {"context_check": "success", "lint_and_types": "success", "unit_tests": "success",
                   "hazards": "success", "load": "success", "simulation": "success",
                   "agent_probes": "success", "reviewer": "success"}
        for profile, ok, lane, status, state in (
                ("storage", storage_ok, "simulation", "skipped", LaneState.SELECTED_SKIPPED),
                ("storage", storage_ok, "gate_l1", None, LaneState.SELECTED_MISSING),
                ("deploy", deploy_ok, "packaging", None, LaneState.SELECTED_MISSING),
                ("deploy", deploy_ok, "packaging", "skipped", LaneState.SELECTED_SKIPPED),
                ("offline", {"context_check": "success", "lint_and_types": "success",
                             "reviewer": "success"}, "context_check", "failure", LaneState.SELECTED_FAILED),
                # The HTTP contract requires real agent probes even when no native
                # producer reports them; an authorized human must supply the evidence.
                ("http", http_ok, "agent_probes", None, LaneState.SELECTED_MISSING)):
            with self.subTest(profile=profile, lane=lane, status=status):
                manifest = self._sample_manifest(profile=profile)
                statuses = {k: v for k, v in ok.items() if k != lane}
                if status is not None:
                    statuses[lane] = status
                summary = build_acceptance_summary(manifest, statuses, review=_ready_review())
                self.assertFalse(summary.all_prerequisites_met)
                self.assertEqual(summary.verification_status, "incomplete")
                self.assertEqual(next(l for l in summary.lanes if l.name == lane).state, state)
        passing = build_acceptance_summary(self._sample_manifest(profile="http"), http_ok,
                                           review=_ready_review())
        self.assertTrue(passing.all_prerequisites_met)
        self.assertEqual(passing.verification_status, "passed")
        self.assertFalse(next(l for l in passing.lanes if l.name == "eval_retrieval").required)
        # A full-profile union still owes packaging even without a deploy category.
        full = build_acceptance_summary(self._sample_manifest(profile="full"), http_ok,
                                        review=_ready_review())
        self.assertTrue(next(l for l in full.lanes if l.name == "packaging").required)
        self.assertFalse(full.all_prerequisites_met)

        tooling = self._sample_manifest(profile="offline")
        tooling["matched_categories"] = ["tooling"]
        summary = build_acceptance_summary(tooling, {"context_check": "success", "reviewer": "success"},
                                           review=_ready_review())
        self.assertFalse(summary.all_prerequisites_met)
        for name in ("lint_and_types", "unit_tests"):
            self.assertEqual(next(l for l in summary.lanes if l.name == name).state, LaneState.SELECTED_MISSING)

    def test_unselected_lane_never_blocks_a_docs_only_acceptance(self):
        # ci.yml ignores markdown-only PRs, so lint/unit lanes have no producer for
        # prose; requiring them would make docs-only acceptance unreachable.
        manifest = self._sample_manifest(profile="offline")
        summary = build_acceptance_summary(
            manifest, {"context_check": "success", "lint_and_types": "success", "reviewer": "success",
                       "simulation": "skipped", "gate_l1": "skipped"}, review=_ready_review())
        self.assertTrue(summary.all_prerequisites_met)
        self.assertEqual(summary.verification_status, "passed")
        summary = build_acceptance_summary(manifest, {"context_check": "success", "reviewer": "success"},
                                           review=_ready_review())
        self.assertTrue(summary.all_prerequisites_met)
        for name in ("lint_and_types", "unit_tests"):
            self.assertEqual(next(l for l in summary.lanes if l.name == name).state, LaneState.UNSELECTED)
        md = summary.markdown_report
        self.assertIn("Prerequisite Obligations**: `ALL_MET`", md)
        self.assertIn("Maintainer Merge Decision**: `pending`", md)
        self.assertIn("Agents never merge pull requests or modify repository access rules", md)


class TestReviewToolingCLI(unittest.TestCase):
    """CLI invocations and end-to-end command exits."""

    def _run(self, *args, cwd=None):
        return subprocess.run([sys.executable, "scripts/review_tooling.py", *args],
                              capture_output=True, text=True, check=False, cwd=cwd)

    def test_cli_profile_writes_manifest_lanes_and_workflow_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = pathlib.Path(tmpdir) / "manifest.json"
            gh_out_file = pathlib.Path(tmpdir) / "github_output.txt"
            res = self._run("profile", "--files", "docs/explanation.md", "README.md",
                            "--out", str(out_file), "--github-output", str(gh_out_file))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stdout.strip(), "offline")
            manifest = json.loads(out_file.read_text())
            self.assertEqual((manifest["profile"], manifest["services"]), ("offline", []))
            gh_text = gh_out_file.read_text()
            self.assertIn("profile=offline", gh_text)
            self.assertIn("needs_qdrant=false", gh_text)

            res = self._run("profile", "--files", "scripts/eval_retrieval.py",
                            "src/mainframe_rag/eval/retrieval.py", "src/mainframe_rag/eval/datasets.py",
                            "tests/test_eval_scoring.py", "--out", str(out_file),
                            "--github-output", str(gh_out_file))
            self.assertEqual(res.returncode, 0, res.stderr)
            manifest = json.loads(out_file.read_text())
            outputs = dict(line.split("=", 1) for line in gh_out_file.read_text().splitlines() if "=" in line)
            expected = ["context_check", "gate_l1", "hazards", "lint_and_types", "simulation", "unit_tests"]
            self.assertEqual(manifest["required_lanes"], expected)
            self.assertEqual(json.loads(outputs["lanes"]), expected)

    def test_cli_validate_review_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            def review(name, text):
                path = pathlib.Path(tmpdir) / name
                path.write_text(text if isinstance(text, str) else json.dumps(text))
                return str(path)
            valid = _review_payload(head_sha="a" * 40, base_sha="b" * 40, execution_sha="c" * 40,
                                    material_findings=[], evidence={})
            changes = {**valid, "code_assessment": "changes_required"}
            findings = {**changes, "verification": "incomplete", "merge_readiness": "not_ready",
                        "material_findings": [{"id": "F1", "disposition": "unresolved",
                                               "description": "Open finding"}]}
            # --check turns a not-ready candidate into a failing exit.
            self.assertEqual(self._run("validate-review", "--review", review("v.json", valid),
                                       "--check").returncode, 0)
            self.assertEqual(self._run("validate-review", "--review", review("c.json", changes),
                                       "--check").returncode, 1)
            # --require-payload: a valid changes_required verdict is a successful review
            # execution; a missing, unparseable or forged payload fails it.
            self.assertEqual(self._run("validate-review", "--review", review("f.json", findings),
                                       "--require-payload").returncode, 0)
            for target in (str(pathlib.Path(tmpdir) / "absent.json"),
                           review("m.md", "Review prose with no machine-readable block."),
                           review("forged.json", changes)):
                self.assertEqual(self._run("validate-review", "--review", target,
                                           "--require-payload").returncode, 2, target)

    def test_cli_summarize_acceptance_check_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_file = pathlib.Path(tmpdir) / "manifest.json"
            manifest_file.write_text(json.dumps({
                "schema_version": 1, "head_sha": "1" * 40, "base_sha": "2" * 40, "execution_sha": "3" * 40,
                "profile": "offline", "dirty": False, "matched_categories": ["prose"]}))
            review_file = pathlib.Path(tmpdir) / "review.json"
            review_file.write_text(json.dumps(_review_payload(material_findings=[], evidence={"notes": "ok"})))
            # Technical obligations alone determine this command exit.
            res = self._run("summarize-acceptance", "--manifest", str(manifest_file),
                            "--review", str(review_file), "--lane", "context_check:success",
                            "--lane", "lint_and_types:success", "--lane", "reviewer:success", "--check")
            self.assertEqual(res.returncode, 0)
            # Failed technical evidence exits 1 without a review schema.
            res = self._run("summarize-acceptance", "--manifest", str(manifest_file),
                            "--lane", "context_check:failure", "--check")
            self.assertEqual(res.returncode, 1)


class TestExecutionAttribution(unittest.TestCase):
    """F3: a commit existing is not proof it was executed (pinned worktree)."""

    def _make_two_commit_repo(self):
        tmp = tempfile.TemporaryDirectory()
        repo = pathlib.Path(tmp.name)
        def git(*args):
            res = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, msg=f"git {' '.join(args)} failed: {res.stderr}")
            return res
        git("init", "-q")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        (repo / "file.txt").write_text("base\n")
        git("add", "file.txt")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD").stdout.strip()
        (repo / "file.txt").write_text("candidate\n")
        git("add", "file.txt")
        git("commit", "-qm", "candidate")
        head = git("rev-parse", "HEAD").stdout.strip()
        return tmp, repo, base, head

    def _payload(self, head, base, execution):
        return _review_payload(head_sha=head, base_sha=base, execution_sha=execution,
                               material_findings=[], evidence={"notes": "ok"})

    def test_wrong_clean_checkout_is_unverified_in_process_and_through_the_cli(self):
        tmp, repo, base, head = self._make_two_commit_repo()
        try:
            # Claim the newer candidate/execution while checked out at base.
            subprocess.run(["git", "checkout", "-q", base], cwd=repo, check=True)
            payload = self._payload(head, base, head)
            result = validate_review_payload(payload, check_git=True, cwd=repo)
            self.assertEqual(result.candidate_currentness, CandidateCurrentness.UNVERIFIED.value)
            self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
            self.assertTrue(any("matches neither" in e and "HEAD" in e for e in result.validation_errors))
            review_file = repo / "review.json"
            review_file.write_text(json.dumps(payload))
            script = str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "review_tooling.py")
            res = subprocess.run(
                [sys.executable, script, "validate-review", "--review", str(review_file),
                 "--check-git", "--require-payload"],
                capture_output=True, text=True, check=False, cwd=repo,
            )
            self.assertEqual(res.returncode, 2)
            normalized = json.loads(res.stdout) if res.stdout.strip() else {}
            self.assertEqual(normalized.get("merge_readiness"), MergeReadiness.NOT_READY.value)
            self.assertEqual(normalized.get("candidate_currentness"), CandidateCurrentness.UNVERIFIED.value)
        finally:
            tmp.cleanup()

    def test_head_checkout_or_head_checkout_with_test_merge_execution_passes(self):
        tmp, repo, base, head = self._make_two_commit_repo()
        try:
            subprocess.run(["git", "checkout", "-q", head], cwd=repo, check=True)
            result = validate_review_payload(self._payload(head, base, head), check_git=True, cwd=repo)
            self.assertEqual(result.merge_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)
            self.assertEqual(len(result.validation_errors), 0)
            # The pinned reviewer CLI checks out the PR head before the model runs
            # while tests execute on the test-merge commit; both are legitimate
            # candidate identities, an unrelated clean checkout is not.
            (repo / "merge.txt").write_text("test-merge\n")
            subprocess.run(["git", "add", "merge.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "test-merge"], cwd=repo, check=True)
            execution = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertNotEqual(execution, head)
            subprocess.run(["git", "checkout", "-q", head], cwd=repo, check=True)
            result = validate_review_payload(self._payload(head, base, execution), check_git=True, cwd=repo)
            self.assertEqual(result.merge_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)
            self.assertEqual(result.validation_errors, [])
        finally:
            tmp.cleanup()

    def test_head_must_be_ancestor_of_execution(self):
        tmp, repo, base, head = self._make_two_commit_repo()
        try:
            # Execution older than head: head cannot be contained in execution.
            subprocess.run(["git", "checkout", "-q", base], cwd=repo, check=True)
            result = validate_review_payload(self._payload(head, base, base), check_git=True, cwd=repo)
            self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
            self.assertTrue(any("ancestor" in e for e in result.validation_errors))
        finally:
            tmp.cleanup()


class TestAcceptanceUnionAndReviewerAuthority(unittest.TestCase):
    """F5/F6: unions of risk never reduce verification; job success is not approval."""

    def _manifest(self, profile, categories):
        return _review_manifest(profile, categories)

    def test_human_review_neither_changes_nor_replaces_technical_verification(self):
        manifest = self._manifest("offline", ["prose"])
        for review in (None, _not_ready_review(), _ready_review()):
            for reviewer_status in ("success", "failure", "skipped"):
                with self.subTest(review=review, status=reviewer_status):
                    summary = build_acceptance_summary(
                        manifest, {"context_check": "success", "reviewer": reviewer_status}, review=review)
                    self.assertTrue(summary.all_prerequisites_met)
                    self.assertEqual(summary.verification_status, "passed")
                    self.assertNotIn("reviewer", {lane.name for lane in summary.lanes})
                    self.assertEqual(summary.to_dict()["maintainer_authority"]["maintainer_decision"], "pending")
                    self.assertNotIn("recommended_readiness", summary.to_dict())
        summary = build_acceptance_summary(manifest, {"reviewer": "success"}, review=_ready_review())
        self.assertFalse(summary.all_prerequisites_met)
        context = next(lane for lane in summary.lanes if lane.name == "context_check")
        self.assertEqual(context.state, LaneState.SELECTED_MISSING)


class TestCiUnitPartition(unittest.TestCase):
    """Exercise real pytest collection, filtering and exit status in isolation."""

    def coverage_tree(self, root, source, *, filename="test_sample.py"):
        repository = pathlib.Path(__file__).resolve().parents[1]
        for package in ("tests", "scripts"):
            (root / package).mkdir()
            (root / package / "__init__.py").write_text("")
        for relative in ("tests/ci_shard.py", "scripts/unit_evidence.py", "requirements.dev.lock.txt",
                         "locks/cp314-linux-x86_64.json", "scripts/prepare_python.py",
                         "scripts/dependency_lock.py", "scripts/agent_doctor.py"):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes((repository / relative).read_bytes())
        (root / "tests/conftest.py").write_text("")
        (root / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
            'addopts = "-q -m \'not integration\'"\n'
            'markers = ["integration: intentionally excluded"]\n')
        (root / "tests" / filename).write_text(source)

    def test_native_coverage_refuses_canary_narrowing_and_collection_hooks(self):
        source = ("import pytest\n" + "\n".join(
            f"def test_failure_{i}():\n    assert False\n" for i in range(6)) +
            "def test_canary_one():\n    pass\ndef test_canary_two():\n    pass\n")
        for variant in ("ordinary", "config-canary", "environment-canary", "drop-hook", "duplicate-hook"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                self.coverage_tree(root, source)
                if variant == "config-canary":
                    p = root / "pyproject.toml"
                    p.write_text(p.read_text().replace("not integration'", "not integration' -k canary"))
                if variant.endswith("hook"):
                    operation = "items[:] = [i for i in items if 'canary' in i.nodeid]" if variant == "drop-hook" else "items.append(items[0])"
                    (root / "tests/conftest.py").write_text("def pytest_collection_modifyitems(items):\n    " + operation + "\n")
                with patch.dict(os.environ, {"PYTEST_ADDOPTS": "-k canary" if variant == "environment-canary" else ""}):
                    for shard in (1, 2):
                        out = root / f"out-{shard}"
                        out.mkdir()
                        if variant == "environment-canary":
                            with self.assertRaises(ValueError):
                                run_shard(root, sys.executable, shard, out / "result.xml", out)
                        elif variant == "config-canary":
                            with self.assertRaises(OSError):  # configuration rejected before any receipt
                                run_shard(root, sys.executable, shard, out / "result.xml", out)
                        else:
                            code, proof = run_shard(root, sys.executable, shard, out / "result.xml", out)
                            self.assertNotEqual(code, 0)
                            if variant == "ordinary":
                                self.assertEqual(code, 1)
                                self.assertEqual(len(proof["collect"]["eligible"]), 8)
                                self.assertEqual(len(proof["execute"]["executed"]), 4)
                                self.assertEqual(len(list(ET.parse(out / "result.xml").iter("failure"))), 3)
                            else:
                                self.assertFalse(proof["collect"]["passed"])


    def test_shards_cover_each_selected_case_once_and_preserve_failure(self):

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "pytest.ini").write_text(
                "[pytest]\naddopts = -m 'not integration'\nmarkers =\n    integration: excluded tier\n"
            )
            (root / "test_sample.py").write_text(textwrap.dedent('''\
                import pytest

                @pytest.mark.parametrize("value", [0, 1, 2, 3], ids=["a b", "c::d", "λ", "[x]"])
                def test_case(value):
                    assert value != 2

                def test_other():
                    pass

                @pytest.mark.integration
                def test_integration():
                    raise AssertionError("integration must remain deselected")
            '''))
            env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                   "PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[1])}
            env.pop("PYTEST_ADDOPTS", None)
            base = [sys.executable, "-m", "pytest", "-p", "tests.ci_shard", "-q"]
            for selection in ([], ["-k", "test_case"]):
                cases, results = [], []
                for shard in (None, 1, 2):
                    report = root / "report.xml"
                    args = [] if shard is None else [f"--unit-shard={shard}"]
                    proc = subprocess.run(base + selection + args + [f"--junitxml={report}"],
                                          cwd=root, env=env, capture_output=True,
                                          text=True, check=False, timeout=20)
                    self.assertIn(proc.returncode, (0, 1), proc.stdout + proc.stderr)
                    results.append(proc.returncode)
                    names = [case.attrib["name"] for case in ET.parse(report).iter("testcase")]
                    self.assertEqual(len(names), len(set(names)))
                    cases.append(set(names))
                self.assertEqual(len(cases[0]), 4 if selection else 5)
                self.assertFalse(cases[1] & cases[2])
                self.assertEqual(cases[1] | cases[2], cases[0])
                self.assertLessEqual(abs(len(cases[1]) - len(cases[2])), 1)
                self.assertEqual(results[0], 1)
                self.assertEqual(sorted(results[1:]), [0, 1])

            for value in ("0", "3", "bad"):
                proc = subprocess.run(base + [f"--unit-shard={value}"], cwd=root,
                                      env=env, capture_output=True, check=False, timeout=20)
                self.assertEqual(proc.returncode, 4)
            proc = subprocess.run(base + ["--unit-shard=1", "-k", "absent_case"],
                                  cwd=root, env=env, capture_output=True, check=False, timeout=20)
            self.assertEqual(proc.returncode, 5)

    def test_balanced_assignment_is_a_deterministic_exact_partition(self):

        nodes = [f"tests/t.py::t[{index:02d}]" for index in range(40)]
        ms = {nodes[0]: 9000, nodes[1]: 4000, nodes[2]: 4000, nodes[39]: 10}
        text = json.dumps({"schema_version": 1, "default_ms": 5, "ms": ms})
        default_ms, parsed = parse_durations(text)
        bins = assign_balanced(sorted(nodes), 4, default_ms, parsed)
        self.assertEqual(sorted(node for group in bins for node in group), sorted(nodes))
        self.assertEqual(bins, assign_balanced(sorted(nodes), 4, default_ms, parsed))
        self.assertEqual([select_nodes(nodes, shard, 4, text) for shard in range(1, 5)], bins)
        # Heaviest first onto the lightest shard, ties by lowest shard index.
        self.assertIn(nodes[0], bins[0])
        self.assertIn(nodes[1], bins[1])
        self.assertIn(nodes[2], bins[2])
        # Unknown new tests still land in exactly one shard, sharing the default weight.
        grown = sorted(nodes + ["tests/t.py::new_case"])
        self.assertEqual(sorted(node for group in assign_balanced(grown, 4, default_ms, parsed) for node in group), grown)
        # A skewed load beats round-robin's worst shard.
        heavy = {node: 1000 if index % 4 == 0 else 1 for index, node in enumerate(sorted(nodes))}
        def weights(group):
            return sum(heavy[node] for node in group)

        balanced = max(map(weights, assign_balanced(sorted(nodes), 4, 5, heavy)))
        self.assertLess(balanced, max(weights(sorted(nodes)[index::4]) for index in range(4)))
        for invalid in ("[]", "{}", json.dumps({"schema_version": 2, "default_ms": 5, "ms": {}}),
                        json.dumps({"schema_version": 1, "default_ms": 0, "ms": {}}),
                        json.dumps({"schema_version": 1, "default_ms": 5, "ms": {"a": -1}}),
                        json.dumps({"schema_version": 1, "default_ms": 5, "ms": {"a": 1.5}}),
                        json.dumps({"schema_version": 1, "default_ms": 5, "ms": {"a": True}}),
                        json.dumps({"schema_version": 1, "default_ms": 5, "ms": {}, "x": 1}),
                        '{"schema_version": 1, "default_ms": 5, "ms": {"a": 1, "a": 2}}'):
            with self.assertRaises(ValueError):
                parse_durations(invalid)

    def test_native_shard_layouts_preserve_exact_union_and_reject_forged_coverage(self):
        source = ("import pytest\n"
                  "@pytest.mark.parametrize('value', range(9), ids=[f'case {index}::λ' for index in range(9)])\n"
                  "def test_case(value):\n    assert value >= 0\n"
                  "@pytest.mark.integration\ndef test_excluded():\n    assert False\n")
        for balanced in (False, True):
            with self.subTest(balanced=balanced), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                filename = "test_sample.py" if balanced else "test_ \t\n four_λ.py"
                self.coverage_tree(root, source + ("def test_unlisted():\n    pass\n" if balanced else ""),
                                   filename=filename)
                listed = {f"tests/{filename}::test_case[case {index}::\\u03bb]": 1000 if index < 3 else 10
                          for index in range(9)}
                if balanced:
                    (root / "tests/unit-durations.json").write_text(
                        json.dumps({"schema_version": 1, "default_ms": 10, "ms": listed}, sort_keys=True))
                records, proofs = [], []
                for shard in range(1, 5):
                    out = root / f"out-{shard}"
                    out.mkdir()
                    xml = out / "result.xml"
                    options = ({"durations": pathlib.Path("tests/unit-durations.json")} if balanced else {})
                    code, proof = run_shard(root, sys.executable, shard, xml, out, shards=4, **options)
                    self.assertEqual(code, 0)
                    record = validate(proof, shard, xml.read_bytes(), input_hashes(root), shards=4)
                    if balanced:
                        self.assertEqual(proof["collect"]["rule"]["name"], "lpt-durations-v1")
                    else:
                        self.assertEqual(record['executed'],
                                         [f"tests/{filename}::test_case[case {index}::\\u03bb]"
                                          for index in range(shard - 1, 9, 4)])
                        with self.assertRaises(ValueError):
                            validate(proof, shard, xml.read_bytes(), input_hashes(root), shards=2)
                    records.append(record)
                    proofs.append((proof, xml.read_bytes()))
                validate_union(list(reversed(records)), shards=4)
                if not balanced:
                    for invalid in (records[:3], records[:2], records + records[:1],
                                    records[:3] + records[:1]):
                        with self.assertRaises(ValueError):
                            validate_union(invalid, shards=4)
                    for field in ('all', 'eligible', 'executed'):
                        forged = copy.deepcopy(records)
                        forged[-1][field] = forged[-1][field][:-1]
                        with self.assertRaises(ValueError):
                            validate_union(forged, shards=4)
                    with self.assertRaises(ValueError):
                        validate_union(records)
                    continue
                # The three heavy cases are on three different shards, not
                # round-robin neighbours of each other.
                heavy_homes = [next(r["shard"] for r in records if node in r["executed"])
                               for node in list(listed)[:3]]
                self.assertEqual(sorted(heavy_homes), [1, 2, 3])
                self.assertTrue(any(r["executed"] != r["eligible"][r["shard"] - 1::4] for r in records))
                executed = sorted(node for r in records for node in r["executed"])
                self.assertEqual(executed, records[0]["eligible"])
                self.assertIn(f"tests/{filename}::test_unlisted", executed)
                self.assertEqual(len({r["rule_sha256"] for r in records}), 1)
                mixed = copy.deepcopy(records)
                mixed[0]["rule_sha256"] = None
                with self.assertRaises(ValueError):
                    validate_union(mixed, shards=4)
                proof, xml = copy.deepcopy(proofs[0][0]), proofs[0][1]
                round_robin = copy.deepcopy(proof)
                for phase in ("collect", "execute"):
                    round_robin[phase].pop("rule")
                with self.assertRaises(ValueError):  # balanced results are not a round-robin shard
                    validate(round_robin, 1, xml, input_hashes(root), shards=4)
                for tamper in ("hash", "text", "name", "missing-phase", "extra"):
                    forged = copy.deepcopy(proof)
                    if tamper == "hash":
                        forged["collect"]["rule"]["durations_sha256"] = "0" * 64
                    elif tamper == "text":  # a consistent hash for different durations still changes the assignment
                        text = json.dumps({"schema_version": 1, "default_ms": 1, "ms": {}})
                        for phase in ("collect", "execute"):
                            forged[phase]["rule"].update(durations_text=text, durations_sha256=hashlib.sha256(text.encode()).hexdigest())
                    elif tamper == "name":
                        forged["collect"]["rule"]["name"] = "other"
                    elif tamper == "missing-phase":
                        forged["execute"].pop("rule")
                    else:
                        forged["collect"]["rule"]["extra"] = 1
                        forged["execute"]["rule"]["extra"] = 1
                    with self.subTest(tamper=tamper), self.assertRaises(ValueError):
                        validate(forged, 1, xml, input_hashes(root), shards=4)
                out = root / "out-absent"
                out.mkdir()
                with self.assertRaises(OSError):  # an unreadable durations file never degrades to round-robin
                    run_shard(root, sys.executable, 1, out / "result.xml", out, shards=4,
                              durations=pathlib.Path("tests/absent.json"))


class TestTaskCiConsumers(unittest.TestCase):
    """CI selection and shell guards; runner semantics live in Task contract tests."""

    root = pathlib.Path(__file__).resolve().parents[1]

    def test_ci_runs_on_pull_request_and_nightly_main_not_on_push(self):
        document = yaml.safe_load((self.root / ".github/workflows/ci.yml").read_text())
        triggers = document.get("on", document.get(True))
        # Candidate acceptance reads only pull_request-event ci runs, so a push
        # run on main is redundant; the nightly is the post-merge signal.
        self.assertEqual(set(triggers), {"pull_request", "schedule", "workflow_dispatch"})
        self.assertIsNone(triggers["pull_request"])
        self.assertEqual(len(triggers["schedule"]), 1)
        self.assertRegex(triggers["schedule"][0]["cron"], r"^(?!0 )\d+ \d+ \* \* \*$")
        concurrency = document["concurrency"]
        self.assertIn("'ci-main'", concurrency["group"])
        self.assertIn("format('ci-{0}', github.ref)", concurrency["group"])
        self.assertEqual(concurrency["cancel-in-progress"], "${{ github.event_name == 'pull_request' }}")

    def test_task_edits_trigger_context_and_load_on_push_and_pull_request(self):

        changes = ("Taskfile.yml", "taskfiles/quality.yml", "taskfiles/airgap.yml",
                   "scripts/tools/run-task.sh", "scripts/tools/task-pin.txt")
        # The pre-merge gate is unchanged for both. Post-merge, the light context
        # lane stays on push; load runs nightly on main plus dispatch (maintainer
        # decision 2026-10-23), so a main SHA is not individually load-tested.
        expected = {"agent-context.yml": ("pull_request", "push"), "load.yml": ("pull_request", "schedule")}
        for workflow, events in expected.items():
            document = yaml.safe_load((self.root / ".github/workflows" / workflow).read_text())
            triggers = document.get("on", document.get(True))
            self.assertNotIn("push", triggers) if workflow == "load.yml" else None
            for event in events:
                self.assertIn(event, triggers)
                if isinstance(triggers[event], dict):
                    self.assertFalse(triggers[event].get("paths"))
                    self.assertFalse(triggers[event].get("paths-ignore"))
        for path in changes:
            decision = classify_paths([path])
            lanes = required_lanes({"profile": decision.profile.value,
                                    "matched_categories": decision.matched_categories,
                                    "changed_paths": [path]})
            self.assertTrue({"context_check", "load", "ha", "simulation", "packaging"}.issubset(lanes), path)


    def test_committed_unit_durations_parse_and_regenerate_from_junit_shape(self):
        default_ms, ms = parse_durations((self.root / "tests/unit-durations.json").read_text())
        self.assertGreaterEqual(default_ms, 1)
        self.assertTrue(ms and all(node.startswith("tests/") for node in ms))
        with tempfile.TemporaryDirectory() as directory:
            case = ('<testcase time="%s"><properties><property name="native_nodeid" value="%s"/>'
                    '</properties></testcase>')
            def node(text):
                return base64.b64encode(text.encode()).decode()

            (pathlib.Path(directory) / "evidence-unit-1-attempt-1").mkdir()
            (pathlib.Path(directory) / "evidence-unit-1-attempt-1/tests.xml").write_text(
                "<testsuite>" + case % ("1.2345", node("tests/a.py::slow[x y]")) +
                case % ("0.004", node("tests/a.py::fast")) + "</testsuite>")
            self.assertEqual(durations([pathlib.Path(directory)]),
                             {"schema_version": 1, "default_ms": 5,
                              "ms": {"tests/a.py::slow[x y]": 1234}})

    def test_unit_durations_dedupes_reruns_to_the_latest_attempt(self):
        def junit(text, seconds):
            value = base64.b64encode(text.encode()).decode()
            return (f'<testsuite><testcase time="{seconds}"><properties><property name="native_nodeid" '
                    f'value="{value}"/></properties></testcase></testsuite>')

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name, seconds in (("evidence-unit-1-attempt-1", "0.5"), ("evidence-unit-1-attempt-2", "0.9"),
                                  ("evidence-unit-2-attempt-1", "0.3")):
                (root / name).mkdir()
                node = "tests/a.py::one" if name.startswith("evidence-unit-1") else "tests/b.py::two"
                (root / name / "tests.xml").write_text(junit(node, seconds))
            self.assertEqual(durations([root])["ms"], {"tests/a.py::one": 900, "tests/b.py::two": 300})

    def test_unit_durations_malformed_input_exits_with_a_fixed_message(self):

        valid = ('<testsuite><testcase time="%s"><properties><property name="native_nodeid" '
                 'value="%s"/></properties></testcase></testsuite>')
        cases = {
            "not xml": "<testsuite",
            "bad base64": valid % ("1", "***"),
            "bad time": valid % ("fast", "dGVzdHMvYS5weTo6eA=="),
            "no nodeid": '<testsuite><testcase time="1"/></testsuite>',
        }
        for label, text in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                (pathlib.Path(directory) / "tests.xml").write_text(text)
                run = subprocess.run([sys.executable, str(self.root / "scripts/unit_durations.py"), directory],
                                     capture_output=True, text=True, cwd=self.root, check=False)
                self.assertEqual(run.returncode, 1)
                self.assertNotIn("Traceback", run.stderr)
                self.assertEqual(len(run.stderr.strip().splitlines()), 1)

    def test_four_unit_vms_preserve_a_fail_closed_test_status(self):

        jobs = yaml.safe_load((self.root / ".github/workflows/ci.yml").read_text())["jobs"]
        self.assertEqual(jobs["unit"]["strategy"]["matrix"], {"shard": [1, 2, 3, 4, 5, 6, 7, 8]})
        self.assertIs(jobs["unit"]["strategy"]["fail-fast"], False)
        command = next(s["run"] for s in jobs["unit"]["steps"] if s.get("name") == "Run unit shard")
        self.assertIn("--unit-shard=${{ matrix.shard }}", command)
        self.assertIn("--unit-shards=8", command)
        self.assertIn("--unit-durations tests/unit-durations.json", command)
        self.assertIn("scripts/ci_evidence.py --lane unit_tests", command)
        self.assertNotIn("-- python -m pytest", command)
        gate = jobs["test"]
        self.assertEqual(gate["needs"], ["select", "unit"])
        self.assertEqual(gate["if"], "always()")
        step = gate["steps"][0]
        self.assertEqual(step["env"]["UNIT_RESULT"], "${{ needs.unit.result }}")
        for result in ("success", "failure", "cancelled", "skipped", ""):
            proc = subprocess.run(["sh", "-eu", "-c", step["run"]],
                                  env={"SELECT_RESULT": "success", "UNIT_REQUIRED": "true", "UNIT_RESULT": result}, check=False)
            self.assertEqual(proc.returncode == 0, result == "success")

    def test_load_step_preserves_failure_skip_and_no_tests_guards(self):

        workflow = yaml.safe_load((self.root / ".github/workflows/load.yml").read_text())
        script = next(step["run"] for step in workflow["jobs"]["load"]["steps"]
                      if step.get("name", "").startswith("run the load tier "))
        for output, status, success in (("2 passed", 0, True), ("2 passed", 7, False),
                                        ("2 passed, 1 skipped", 0, False),
                                        ("no tests ran", 0, False)):
            with self.subTest(output=output, status=status), tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                tool = root / "scripts/tools/run-task.sh"
                tool.parent.mkdir(parents=True)
                tool.write_text('printf "%s\\n" "$*" > calls\nprintf "%s\\n" "$OUTPUT"\nexit "$STATUS"\n')
                # Exercise the real wrapper with a controlled identity and child Task.
                # The separate provenance tests cover Git/event identity validation.
                launcher = root / ".venv/bin/python"
                launcher.parent.mkdir(parents=True)
                launcher.write_text(
                    "#!" + sys.executable + "\nimport sys\nfrom pathlib import Path\n"
                    + "sys.path.insert(0, " + repr(str(self.root)) + ")\n"
                    + "from scripts import ci_evidence\n"
                    + "ci_evidence.ROOT = Path.cwd()\n"
                    + "ci_evidence.identity = lambda: {'execution_sha': 'a' * 40}\n"
                    + "sys.argv = sys.argv[1:]\nraise SystemExit(ci_evidence.main())\n")
                launcher.chmod(0o755)
                tool.write_text(tool.read_text().replace(
                    'exit "$STATUS"',
                    "printf '%s' '<testsuite><testcase name=\"task\"/></testsuite>' > \"$RUNNER_TEMP/load.xml\"\nexit \"$STATUS\""))
                proc = subprocess.run(
                    ["bash", "-eu", "-c", script], cwd=root,
                    env={**os.environ, "OUTPUT": output, "STATUS": str(status), "RUNNER_TEMP": str(root)},
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(proc.returncode == 0, success, proc.stdout + proc.stderr)
                self.assertEqual((root / "calls").read_text(), "qa:load\n")

    def test_offline_unit_job_requires_local_archive_before_running_tests(self):

        job = yaml.safe_load((self.root / ".gitlab-ci.yml").read_text())["test"]
        self.assertEqual(job["variables"]["TASK_CONTRACTS_REQUIRE_RUNNER"], "1")
        commands = job["script"]
        # The JUnit skip/xfail gate runs after pytest; tests/test_ci_gitlab_parity.py
        # owns its behavior. Here only the prerequisite ordering is exercised.
        gate = next(i for i, c in enumerate(commands) if "check_junit_clean.py" in c)
        script = "\n".join(commands[:gate])
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            tool = root / "scripts/tools/install-task.sh"
            tool.parent.mkdir(parents=True)
            tool.write_text('printf "%s\\n" "$@" > archive-args\nexit "${INSTALL_STATUS:-0}"\n')
            helm_tool = root / "scripts/tools/install-helm.sh"
            helm_tool.write_text('printf "%s\\n" "$@" > helm-args\nexit "${HELM_INSTALL_STATUS:-0}"\n')
            doctor = root / "python"
            doctor.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > doctor-args\nexit "${DOCTOR_STATUS:-0}"\n')
            doctor.chmod(0o755)
            test = root / "pytest"
            test.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > tests-ran\n')
            test.chmod(0o755)
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}"}
            env.pop("CI_TASK_ARCHIVE", None)
            missing = subprocess.run(["sh", "-eu", "-c", script], cwd=root, env=env,
                                     capture_output=True, text=True, check=False)
            self.assertNotEqual(missing.returncode, 0)
            self.assertFalse((root / "archive-args").exists())
            self.assertFalse((root / "tests-ran").exists())
            archive = str(root / "offline archive with spaces.tar.gz")
            helm_archive = str(root / "offline helm with spaces.tar.gz")
            missing_helm = subprocess.run(
                ["sh", "-eu", "-c", script], cwd=root,
                env={**env, "CI_TASK_ARCHIVE": archive},
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(missing_helm.returncode, 0)
            self.assertFalse((root / "helm-args").exists())
            self.assertFalse((root / "tests-ran").exists())
            for task_status, helm_status, doctor_status in ((9, 0, 0), (0, 7, 0), (0, 0, 2), (0, 0, 0)):
                for marker in ("archive-args", "helm-args", "doctor-args", "tests-ran"):
                    (root / marker).unlink(missing_ok=True)
                proc = subprocess.run(
                    ["sh", "-eu", "-c", script], cwd=root,
                    env={**env, "CI_TASK_ARCHIVE": archive, "CI_HELM_ARCHIVE": helm_archive,
                         "CI_PROJECT_DIR": str(root), "INSTALL_STATUS": str(task_status),
                         "HELM_INSTALL_STATUS": str(helm_status), "DOCTOR_STATUS": str(doctor_status)},
                    capture_output=True, text=True, check=False,
                )
                expected = task_status or helm_status or doctor_status
                self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
                self.assertEqual((root / "archive-args").read_text(), f"--archive\n{archive}\n")
                self.assertEqual((root / "tests-ran").exists(), expected == 0)
                self.assertEqual((root / "doctor-args").exists(), task_status == helm_status == 0)
                if task_status == 0:
                    self.assertEqual((root / "helm-args").read_text(), f"--archive\n{helm_archive}\n")
            self.assertEqual((root / "tests-ran").read_text(), "-q --junitxml=unit-junit.xml\n")
            self.assertEqual((root / "doctor-args").read_text(),
                             f"scripts/agent_doctor.py\n--python\n{root / 'python'}\n")
            self.assertEqual(shlex.split(commands[1]),
                             ["sh", "scripts/tools/install-task.sh", "--archive", "$CI_TASK_ARCHIVE"])



class TestNativeExecutionEvidence(unittest.TestCase):
    def test_junit_counts_actual_records_not_declared_totals(self):

        with tempfile.TemporaryDirectory() as directory:
            report = pathlib.Path(directory) / "tests.xml"
            report.write_text('<testsuite tests="999"><testcase name="one"/><testcase name="two"><skipped/></testcase></testsuite>')
            counts = junit_counts(report)
            self.assertEqual(counts["executed"], 2)
            self.assertEqual(counts["skipped"], 1)
            report.write_text('<testsuite tests="999"/>')
            with self.assertRaisesRegex(ValueError, "no testcases"):
                junit_counts(report)
            report.write_text('<!DOCTYPE testsuite><testsuite><testcase/></testsuite>')
            with self.assertRaisesRegex(ValueError, "unsupported"):
                junit_counts(report)

    def test_real_pytest_xfail_and_skip_are_not_passes(self):
        """Real pytest/JUnit semantics: xfail renders as <skipped>, so it cannot read as green."""
        cases = {
            "xfail": "import pytest\n@pytest.mark.xfail(reason='known')\ndef test_known():\n    assert False\n",
            "skip": "import pytest\n@pytest.mark.skip(reason='later')\ndef test_later():\n    pass\n",
            "xpass-strict": ("import pytest\n@pytest.mark.xfail(strict=True, reason='r')\n"
                             "def test_unexpected():\n    pass\n"),
            "clean": "def test_ok():\n    pass\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name, source in cases.items():
                (root / name).mkdir()
                (root / name / f"test_{name.replace('-', '_')}.py").write_text(source + "\ndef test_companion():\n    pass\n")
            for name, (expected_skipped, expected_exit) in {
                    "xfail": (1, 1), "skip": (1, 1), "xpass-strict": (0, 1), "clean": (0, 0)}.items():
                with self.subTest(name=name):
                    junit = root / (name + ".xml")
                    output = root / ("out-" + name)
                    argv = ["ci_evidence.py", "--lane", "simulation", "--job-name", "sim",
                            "--output", str(output), "--junit", str(junit), "--",
                            sys.executable, "-m", "pytest", "-o", "addopts=", "-p", "no:cacheprovider", "-q",
                            "--rootdir", str(root / name), str(root / name), "--junitxml", str(junit)]
                    env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
                    with patch.object(ci_evidence, "ROOT", root), \
                            patch.object(ci_evidence, "identity", return_value={"execution_sha": "a" * 40}), \
                            patch.dict(os.environ, env), patch.object(sys, "argv", argv):
                        self.assertEqual(ci_evidence.main(), expected_exit)
                    evidence = json.loads((output / "evidence.json").read_text())
                    self.assertEqual(evidence["tests"]["skipped"], expected_skipped)
                    self.assertEqual(evidence["passed"], expected_exit == 0)
                    self.assertEqual(evidence["tests"]["executed"], 2)

    def test_unittest_expected_failure_and_skip_are_not_passes(self):
        sources = {
            "expected": ("import unittest\nclass T(unittest.TestCase):\n"
                         "    def test_ok(self):\n        pass\n"
                         "    @unittest.expectedFailure\n    def test_known(self):\n        self.fail()\n"),
            "skipped": ("import unittest\nclass T(unittest.TestCase):\n"
                        "    def test_ok(self):\n        pass\n"
                        "    @unittest.skip('later')\n    def test_later(self):\n        pass\n"),
            "clean": "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            sys.path.insert(0, directory)
            try:
                for name, source in sources.items():
                    module = f"ctx_{name}_{uuid.uuid4().hex}"
                    (root / (module + ".py")).write_text(source)
                    importlib.invalidate_caches()
                    with self.subTest(name=name):
                        with contextlib.redirect_stderr(io.StringIO()):
                            code, counts = ci_evidence.run_unittest([module])
                        self.assertEqual(code, 0 if name == "clean" else 1, (name, counts))
                        self.assertEqual(counts["skipped"], 0 if name == "clean" else 1)
                        self.assertEqual(counts["errors"], 0)
            finally:
                sys.path.remove(directory)

    def test_native_command_requires_nonzero_fresh_passing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name, xml, exit_code, expected in (
                ("pass", '<testsuite><testcase name="actual"/></testsuite>', 0, 0),
                ("zero", '<testsuite tests="99"/>', 0, 1),
                ("skip", '<testsuite><testcase><skipped/></testcase></testsuite>', 0, 1),
                ("error", '<testsuite><testcase><error/></testcase></testsuite>', 0, 1),
                ("failed-command", '<testsuite><testcase/></testsuite>', 1, 1),
            ):
                with self.subTest(name=name):
                    junit = root / (name + '.xml')
                    output = root / name
                    command = 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2]); sys.exit(int(sys.argv[3]))'
                    argv = ['ci_evidence.py', '--lane', 'simulation', '--job-name', 'sim',
                            '--output', str(output), '--junit', str(junit), '--', sys.executable,
                            '-c', command, str(junit), xml, str(exit_code)]
                    with patch.object(ci_evidence, 'ROOT', root), patch.object(ci_evidence, 'identity', return_value={'execution_sha': 'a' * 40}), patch.object(sys, 'argv', argv):
                        self.assertEqual(ci_evidence.main(), expected)
                        evidence = json.loads((output / 'evidence.json').read_text())
                        self.assertEqual(evidence['passed'], expected == 0)
                        if expected == 0:
                            self.assertEqual((output / 'tests.xml').read_text(), xml)
                        # Reusing the existing report is refused before command execution.
                        argv[argv.index('--output') + 1] = str(root / (name + '-retry'))
                        self.assertEqual(ci_evidence.main(), 2)
                        self.assertFalse((root / (name + '-retry')).exists())

    def test_identity_binds_real_merge_parents_event_and_clean_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True,
                                               stderr=subprocess.DEVNULL).strip()
            git("init", "-b", "main")
            git("config", "user.name", "Synthetic Test")
            git("config", "user.email", "synthetic@example.invalid")
            (root / "scripts").mkdir()
            policy = root / "scripts/review_tooling.py"
            policy.write_text("# synthetic policy\n")
            git("add", ".")
            git("commit", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("checkout", "-b", "candidate")
            (root / "candidate").write_text("synthetic change")
            git("add", ".")
            git("commit", "-m", "candidate")
            head = git("rev-parse", "HEAD")
            git("checkout", "main")
            git("merge", "--no-ff", "candidate", "-m", "test merge")
            merge = git("rev-parse", "HEAD")
            event = root / "event.json"
            payload = {"pull_request": {"number": 3, "head": {"sha": head}, "base": {"sha": base}}}
            event.write_text(json.dumps(payload))
            env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "synthetic/repository",
                   "GITHUB_REPOSITORY_ID": "42", "GITHUB_EVENT_NAME": "pull_request",
                   "GITHUB_WORKFLOW_REF": "synthetic/repository/.github/workflows/ci.yml@refs/pull/3/merge",
                   "GITHUB_WORKFLOW_SHA": merge, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                   "GITHUB_JOB": "unit", "GITHUB_ACTOR_ID": "7", "GITHUB_TRIGGERING_ACTOR": "synthetic"}
            with patch.object(ci_evidence, "ROOT", root), patch.dict(os.environ, env):
                receipt = ci_evidence.identity()
                self.assertEqual((receipt["head_sha"], receipt["base_sha"], receipt["execution_sha"]),
                                 (head, base, merge))
                self.assertEqual(receipt["execution_parents"], [base, head])
                self.assertEqual(receipt["event_base_sha"], base)
                self.assertEqual((receipt["run_id"], receipt["run_attempt"]), (123, 2))
                # An updated head cannot reuse an earlier test merge.
                payload["pull_request"]["head"]["sha"] = "f" * 40
                event.write_text(json.dumps(payload))
                with self.assertRaisesRegex(ValueError, "does not bind"):
                    ci_evidence.identity()
                payload["pull_request"]["head"]["sha"] = head
                # A payload base that disagrees with the merge is recorded, not
                # substituted: the receipt names the base the tree was built on.
                payload["pull_request"]["base"]["sha"] = "f" * 40
                event.write_text(json.dumps(payload))
                moved = ci_evidence.identity()
                self.assertEqual((moved["base_sha"], moved["event_base_sha"], moved["execution_parents"]),
                                 (base, "f" * 40, [base, head]))
                payload["pull_request"]["base"]["sha"] = base
                event.write_text(json.dumps(payload))
                policy.write_text("# dirty tracked policy\n")
                with self.assertRaisesRegex(ValueError, "dirty"):
                    ci_evidence.identity()
                git("restore", "scripts/review_tooling.py")
                git("checkout", "candidate")
                self.assertEqual(ci_evidence.identity()["execution_sha"], head)

    def test_identity_receipt_cannot_claim_results_and_identity_movement_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            existing = root / "existing.json"
            existing.write_text('{"passed": true}')
            for extra in (["--identity-only", "--junit", str(root / "new.xml")],
                          ["--identity-only", "--result-json", str(existing)],
                          ["--result-json", str(existing), "--", sys.executable, "-c", "pass"]):
                with self.subTest(extra=extra):
                    argv = ["ci_evidence.py", "--lane", "packaging", "--job-name", "build",
                            "--output", str(root / "out"), *extra]
                    with patch.object(sys, "argv", argv), patch.object(ci_evidence, "identity", return_value={}):
                        self.assertEqual(ci_evidence.main(), 2)
                    self.assertFalse((root / "out").exists())
            # The execution identity moving during the job cannot pass.
            argv = ['ci_evidence.py', '--lane', 'lint_and_types', '--job-name', 'lint', '--output', str(root / 'out'),
                    '--', sys.executable, '-c', 'pass']
            with patch.object(ci_evidence, 'ROOT', root), patch.object(sys, 'argv', argv), patch.object(
                    ci_evidence, 'identity', side_effect=[{'execution_sha': 'a' * 40}, {'execution_sha': 'b' * 40}]):
                self.assertEqual(ci_evidence.main(), 1)
                self.assertFalse(json.loads((root / 'out/evidence.json').read_text())['passed'])

class TestNativeEvidenceConsumer(unittest.TestCase):
    def test_four_native_job_receipts_require_the_complete_raw_junit_union(self):
        for balanced in (False, True):
            with self.subTest(balanced=balanced):
                self.check_native_union(balanced)

    def check_native_union(self, balanced):
        source = b'    name: unit (${{ matrix.shard }}/4)\n        shard: [1, 2, 3, 4]\n'
        producers = unit_producers(source, hashlib.sha256(source).hexdigest())
        eligible = [f'tests/test_exact \t\n λ.py::test_case[{index}:: a b]' for index in range(9)]
        text = json.dumps({'schema_version': 1, 'default_ms': 5,
                           'ms': {eligible[3]: 900, eligible[4]: 800, eligible[0]: 700}})
        durations = text if balanced else None
        records = []
        for shard, producer in enumerate(producers, 1):
            args, receipt, _ = self.fixture()
            args.update(producer=producer, workflow_source=source,
                        workflow_digest=hashlib.sha256(source).hexdigest())
            args['job'].update(name=producer.job, id=456 + shard)
            args['artifact']['name'] = f'evidence-unit-{shard}-attempt-2'
            selected = select_nodes(eligible, shard, 4, durations)
            xml = ('<testsuite>' + ''.join(
                '<testcase><properties><property name="native_nodeid" value="' +
                base64.b64encode(node.encode()).decode() + '"/></properties></testcase>'
                for node in selected) + '</testsuite>').encode()
            coverage = receipt['unit_coverage']
            coverage['shard'] = shard
            for phase in ('collect', 'execute'):
                if balanced:
                    coverage[phase]['rule'] = rule_record(text)
                coverage[phase].update(all=eligible, eligible=eligible,
                                       selected=eligible if phase == 'collect' else selected,
                                       executed=[] if phase == 'collect' else selected)
            receipt.update(job_name=producer.job, tests=junit_bytes(xml))
            archive, digest = self.packed(receipt, xml)
            args['artifact']['digest'] = digest
            result = normalize_native(**args, archive=archive)
            records.append(result['unit_coverage'])
            receipt['unit_coverage']['execute']['executed'] = eligible[:1]
            archive, args['artifact']['digest'] = self.packed(receipt, xml)
            if selected != eligible[:1]:
                with self.assertRaises(ValueError):
                    normalize_native(**args, archive=archive)
        validate_union(records, shards=4)
        for invalid in (records[:3], records + records[:1], records[:3] + records[:1]):
            with self.assertRaises(ValueError):
                validate_union(copy.deepcopy(invalid), shards=4)

    def test_unit_layout_comes_from_approved_workflow_not_receipts(self):
        for shards in (2, 4, 8):
            source = (f'    name: unit (${{{{ matrix.shard }}}}/{shards})\n'
                      '        shard: [' + ', '.join(map(str, range(1, shards + 1))) + ']\n').encode()
            producers = unit_producers(source, hashlib.sha256(source).hexdigest())
            self.assertEqual([producer.job for producer in producers],
                             [f'unit ({shard}/{shards})' for shard in range(1, shards + 1)])
            self.assertEqual([producer.artifact for producer in producers],
                             [f'unit-{shard}' for shard in range(1, shards + 1)])
            for invalid in (source + source, source.replace(b'shard: [1, 2', b'shard: [1, 1'),
                            source.replace(f'/{shards})'.encode(), b'/3)'), b'unknown'):
                with self.assertRaises(ValueError):
                    unit_producers(invalid, hashlib.sha256(invalid).hexdigest())
            with self.assertRaises(ValueError):
                unit_producers(source, '0' * 64)

    def fixture(self):
        producer = next(p for p in PRODUCERS if p.job == "unit (1/2)")
        candidate = {"repository": "synthetic/repository", "repository_id": 42, "number": 3,
                     "head_sha": "a" * 40, "base_sha": "b" * 40, "execution_sha": "c" * 40,
                     "head_repository_id": 84}
        run = {"id": 123, "run_attempt": 2, "event": "pull_request", "path": ".github/workflows/ci.yml",
               "head_sha": "a" * 40, "status": "completed", "repository": {"id": 42, "full_name": "synthetic/repository"},
               "head_repository": {"id": 84}, "actor": {"id": 7}, "triggering_actor": {"login": "synthetic"},
               "created_at": "2026-10-26T12:00:05Z", "run_started_at": "2026-10-26T12:00:05Z"}
        job = {"id": 456, "run_id": 123, "run_attempt": 2, "head_sha": "a" * 40,
               "name": "unit (1/2)", "status": "completed", "conclusion": "success"}
        artifact = {"id": 789, "expired": False, "name": "evidence-unit-1-attempt-2",
                    "workflow_run": {"id": 123, "repository_id": 42, "head_repository_id": 84, "head_sha": "a" * 40}}
        commit = {"sha": "c" * 40, "parents": [{"sha": "b" * 40}, {"sha": "a" * 40}],
                  "commit": {"tree": {"sha": "1" * 40},
                             "committer": {"date": "2026-10-26T12:00:00Z"}}}
        identity = base64.b64encode(b'tests/test_example.py::actual').decode()
        xml = ('<testsuite tests="999"><testcase name="actual"><properties>'
               f'<property name="native_nodeid" value="{identity}"/>'
               '</properties></testcase></testsuite>').encode()
        unit_policy = {path: hashlib.sha256(b"approved verifier input").hexdigest() for path in
                       ("pyproject.toml", "tests/conftest.py", "tests/ci_shard.py", "scripts/unit_evidence.py",
                        "requirements.dev.lock.txt", "locks/cp314-linux-x86_64.json",
                        "scripts/prepare_python.py", "scripts/dependency_lock.py", "scripts/agent_doctor.py")}
        eligible = ["tests/test_example.py::actual", "tests/test_example.py::other"]
        collection = {"phase": "collect", "passed": True, "inputs": unit_policy,
                      "versions": {"pytest": "synthetic", "pluggy": "synthetic", "anyio": "synthetic"},
                      "policy": {"testpaths": ["tests"], "python_files": ["test_*.py", "*_test.py"],
                                 "python_classes": ["Test"], "python_functions": ["test"],
                                 "addopts": ["-q", "-m", "not integration"]},
                      "plugins": ["_pytest", "anyio.pytest_plugin", "tests/ci_shard.py", "tests/conftest.py"],
                      "all": eligible, "eligible": eligible, "integration": [],
                      "selected": eligible, "executed": []}
        execution = {**collection, "phase": "execute", "selected": eligible[:1], "executed": eligible[:1]}
        coverage = {"schema_version": 1, "shard": 1, "collect": collection, "execute": execution}
        receipt = {"schema_version": 1, "repository": "synthetic/repository", "repository_id": 42,
                   "pull_request": 3, "head_sha": "a" * 40, "base_sha": "b" * 40,
                   "execution_sha": "c" * 40, "execution_parents": ["b" * 40, "a" * 40],
                   "event": "pull_request", "run_id": 123, "run_attempt": 2, "job_key": "unit",
                   "job_name": "unit (1/2)", "lane": "unit_tests", "actor_id": 7,
                   "triggering_actor": "synthetic", "workflow_sha": "c" * 40,
                   "workflow_ref": "synthetic/repository/.github/workflows/ci.yml@refs/pull/3/merge",
                   "policy_sha256": "d" * 64, "producer_sha256": "e" * 64,
                   "evidence_kind": "execution", "passed": True, "exit_code": 0, "tests": junit_bytes(xml),
                   "unit_coverage": coverage}
        return {"candidate": candidate, "producer": producer, "run": run, "job": job, "unit_policy": unit_policy,
                "artifact": artifact, "execution_commit": commit,
                "candidate_commit": json.loads(json.dumps(commit)), "policy_digest": "d" * 64,
                "producer_digest": "e" * 64, "workflow_source": b"approved workflow",
                "workflow_digest": hashlib.sha256(b"approved workflow").hexdigest()}, receipt, xml

    def packed(self, receipt, xml):
        return pack_archive(receipt, **{"tests.xml": xml})

    def test_collector_requires_all_native_shards_and_never_reuses_an_older_green_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            base = approved_repository(root)
            args, receipt, xml = self.fixture()
            args["candidate"]["base_sha"] = receipt["base_sha"] = base
            receipt["execution_parents"][0] = base
            args["execution_commit"]["parents"][0]["sha"] = base
            args["candidate_commit"]["parents"][0]["sha"] = base
            receipt["policy_sha256"] = hashlib.sha256(b"approved policy").hexdigest()
            receipt["producer_sha256"] = hashlib.sha256(b"approved producer").hexdigest()
            archive, digest = self.packed(receipt, xml)
            args["artifact"].update(digest=digest, size_in_bytes=len(archive))
            pr = {"number": 3, "user": {"id": 99}, "state": "open", "mergeable": True,
                  "merge_commit_sha": "c" * 40,
                  "head": {"sha": "a" * 40, "repo": {"id": 84}},
                  "base": {"sha": base, "ref": "main",
                           "repo": {"id": 42, "full_name": "synthetic/repository", "default_branch": "main"}}}
            class API:
                repository = "synthetic/repository"
                prefix = "repos/synthetic/repository/"
                def __init__(self):
                    self.newer = False
                    self.live_base = base
                    self.seen = []
                def get(self, endpoint):
                    self.seen.append(endpoint)
                    if endpoint.endswith("git/ref/heads/main"):
                        return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.live_base}}
                    if "actions/workflows/verifier-update.yml/runs?" in endpoint:
                        return {"total_count": 0, "workflow_runs": []}
                    if "actions/runs?" in endpoint:
                        runs = [args["run"]]
                        if self.newer:
                            runs.append({**args["run"], "id": 124, "status": "in_progress"})
                        return {"total_count": len(runs), "workflow_runs": runs}
                    if endpoint.endswith("/jobs?per_page=100&page=1"):
                        return {"total_count": 1, "jobs": [args["job"]]}
                    if endpoint.endswith("/artifacts?per_page=100&page=1"):
                        return {"total_count": 1, "artifacts": [args["artifact"]]}
                    if "/commits/" in endpoint:
                        if endpoint.endswith(args["candidate_commit"]["sha"]):
                            return args["candidate_commit"]
                        if endpoint.endswith(args["execution_commit"]["sha"]):
                            return args["execution_commit"]
                        raise AssertionError("unexpected commit lookup")
                    if endpoint.endswith("/124"):
                        return {**args["run"], "id": 124, "status": "in_progress"}
                    return args["run"]
                def raw(self, endpoint):
                    return archive
                def blob(self, sha, path):
                    if path == "scripts/ci_evidence.py":
                        return b"candidate producer" if getattr(self, "forged_producer", False) else b"approved producer"
                    if path == "scripts/review_tooling.py":
                        return b"approved policy"
                    if path in VERIFICATION_INPUTS or path.startswith("taskfiles/"):
                        return b"candidate dispatch" if getattr(self, "changed_input", None) == path else (root / path).read_bytes()
                    return (root / path).read_bytes()
            api = API()
            api.live_base = "d" * 40
            with self.assertRaises(ValueError):
                collect_native(api, pr, root)
            api.live_base = base
            result = collect_native(api, pr, root)
            self.assertEqual(next(r for r in result["native"] if r["job"] == "unit (1/2)")["status"], "success")
            self.assertNotIn("unit_tests", result["lane_statuses"])
            # GitHub regenerates only the merge metadata: resolve the tested
            # commit from its receipt and independently compare both API trees.
            pr["merge_commit_sha"] = args["candidate_commit"]["sha"] = "f" * 40
            for tree, expected in (("1" * 40, "success"), ("2" * 40, "unverified"), ("1" * 40, "success")):
                args["execution_commit"]["commit"]["tree"]["sha"] = tree
                result = collect_native(api, pr, root)
                record = next(r for r in result["native"] if r["job"] == "unit (1/2)")
                self.assertEqual(record["status"], expected)
                if expected == "success":
                    self.assertEqual(record["execution_sha"], "c" * 40)
                    self.assertEqual(result["candidate"]["execution_sha"], "f" * 40)
            self.assertIn(api.prefix + "commits/" + "c" * 40, api.seen)
            self.assertIn(api.prefix + "commits/" + "f" * 40, api.seen)
            api.newer = True
            result = collect_native(api, pr, root)
            self.assertEqual(result["runs"]["ci.yml"]["run_id"], 124)
            self.assertTrue(all(r["status"] != "success" for r in result["native"]))
            self.assertIn("repos/synthetic/repository/actions/runs/124/attempts/2/jobs?per_page=100&page=1", api.seen)
            api.newer = False
            api.forged_producer = True
            # The receipt still claims the approved producer digest. Actual
            # candidate source bytes must independently contradict that claim.
            with self.assertRaises(ValueError):
                collect_native(api, pr, root)
            api.forged_producer = False
            for path in ("pyproject.toml", "tests/conftest.py", "tests/ci_shard.py", "scripts/unit_evidence.py",
                         "requirements.dev.lock.txt", "locks/cp314-linux-x86_64.json",
                         "scripts/prepare_python.py", "scripts/dependency_lock.py", "scripts/agent_doctor.py",
                         "Taskfile.yml", "taskfiles/quality.yml", "scripts/tools/run-task.sh",
                         "tests/hazards/critical.json", "scripts/check_hazard_sensitivity.py"):
                with self.subTest(path=path):
                    api.changed_input = path
                    with self.assertRaises(ValueError):
                        collect_native(api, pr, root)

    def test_unit_receipt_rejects_incomplete_selection_and_same_count_substitutions(self):

        for variant in ("missing", "wrong-shard", "narrowed-execution", "duplicate-call", "altered-input",
                        "unexpected-plugin", "changed-integration", "same-count-xml", "duplicate-property"):
            with self.subTest(variant=variant):
                args, receipt, xml = self.fixture()
                proof = copy.deepcopy(receipt['unit_coverage'])
                if variant == "missing":
                    receipt.pop('unit_coverage')
                elif variant == "wrong-shard":
                    proof['shard'] = 2
                elif variant == "narrowed-execution":
                    proof['execute']['eligible'] = proof['execute']['selected']
                elif variant == "duplicate-call":
                    proof['execute']['executed'] *= 2
                elif variant == "altered-input":
                    proof['execute']['inputs']['tests/ci_shard.py'] = 'f' * 64
                elif variant == "unexpected-plugin":
                    proof['collect']['plugins'].append('test-hook')
                elif variant == "changed-integration":
                    proof['collect']['integration'] = ['tests/test_example.py::other']
                elif variant == "same-count-xml":
                    xml = xml.replace(base64.b64encode(b'tests/test_example.py::actual'),
                                      base64.b64encode(b'tests/test_example.py::other'))
                else:
                    xml = xml.replace(b'</properties>', b'<property name="native_nodeid" value=""/></properties>')
                if variant != "missing":
                    receipt['unit_coverage'] = proof
                receipt['tests'] = junit_bytes(xml)  # raw XML digest/counts still agree
                args['archive'], args['artifact']['digest'] = self.packed(receipt, xml)
                with self.assertRaises((ValueError, KeyError)):
                    normalize_native(**args)

    def test_native_agent_probes_require_all_named_transport_witnesses(self):

        names = [
            "test_live_agent_contract_and_fresh_trace",
            "test_live_agent_fixed_overlong_envelope",
            "test_live_agent_stream_final_matches_buffered",
            "test_live_citation_only_generation_is_not_accepted",
            "test_live_agent_disconnect_closes_upstream_then_next_request",
        ]
        for variant in ("complete", "missing", "missing-citation-only", "duplicate", "wrong-module",
                        "unrelated", "parametrized", "parametrized-duplicate", "mixed-duplicate"):
            with self.subTest(variant=variant):
                args, receipt, _ = self.fixture()
                args["producer"] = next(p for p in PRODUCERS if p.lane == "agent_probes")
                # Probe receipts have no unit coverage proof or unit policy.
                receipt.pop("unit_coverage")
                args["unit_policy"] = None
                args["run"]["path"] = ".github/workflows/agent-probes.yml"
                args["job"]["name"] = "agent-probes"
                args["artifact"]["name"] = "evidence-agent-probes-attempt-2"
                receipt.update(job_key="agent-probes", job_name="agent-probes", lane="agent_probes",
                               workflow_ref="synthetic/repository/.github/workflows/agent-probes.yml@refs/pull/3/merge")
                selected = names.copy()
                module = "tests.live_agent_probes"
                if variant == "missing":
                    selected.pop()
                elif variant == "missing-citation-only":
                    selected.remove("test_live_citation_only_generation_is_not_accepted")
                elif variant == "duplicate":
                    selected.append(names[0])
                elif variant == "wrong-module":
                    module = "tests.unrelated"
                elif variant == "unrelated":
                    selected[-1] = "test_unrelated"
                elif variant == "parametrized":
                    selected[-1:] = [f"{names[-1]}[{param}]" for param in (
                        "answer-/v1/answer-v1.answer",
                        "chat-/v1/chat-v1.chat",
                        "console-/ui/chat/stream-ui.chat",
                    )]
                elif variant == "parametrized-duplicate":
                    selected[-1:] = [f"{names[-1]}[answer]", f"{names[-1]}[answer]"]
                elif variant == "mixed-duplicate":
                    selected.append(f"{names[-1]}[answer]")
                xml = ("<testsuite>" + "".join(
                    f'<testcase classname="{module}" name="{name}"/>' for name in selected
                ) + "</testsuite>").encode()
                receipt["tests"] = junit_bytes(xml)
                args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
                expected = {"complete": 5, "parametrized": 7}.get(variant)
                if expected is not None:
                    self.assertEqual(normalize_native(**args)["tests"]["executed"], expected)
                else:
                    with self.assertRaises(ValueError):
                        normalize_native(**args)


    def test_rejects_wrong_actor_run_attempt_workflow_candidate_and_failed_execution(self):

        for field, value in (("actor_id", 9), ("triggering_actor", "wrong"), ("run_id", 124),
                             ("run_attempt", 1), ("job_key", "other"), ("job_name", "unit (2/2)"),
                             ("head_sha", "f" * 40), ("base_sha", "f" * 40),
                             ("workflow_sha", "a" * 40), ("workflow_ref", "other"),
                             ("policy_sha256", "f" * 64), ("producer_sha256", "f" * 64),
                             ("passed", False), ("passed", 1), ("exit_code", 3), ("exit_code", False)):
            with self.subTest(field=field, value=value):
                args, receipt, xml = self.fixture()
                receipt[field] = value
                args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
                with self.assertRaises(ValueError):
                    normalize_native(**args)
        for field, value in (("run_attempt", 1), ("conclusion", "cancelled"), ("conclusion", "skipped"),
                             ("conclusion", "failure"), ("status", "in_progress"), ("run_id", 124)):
            with self.subTest(native_field=field, value=value):
                args, receipt, xml = self.fixture()
                args["job"][field] = value
                args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
                with self.assertRaises(ValueError):
                    normalize_native(**args)

    def test_missing_zero_skipped_or_different_raw_test_evidence_cannot_pass(self):
        # A green receipt must not cover different actual XML, even when both
        # sides report the same number of cases, and an honest skip count is
        # still not a pass.
        for xml, honest in ((b'<testsuite tests="100"/>', False),
                            (b'<testsuite><testcase><skipped/></testcase></testsuite>', False),
                            (b'<testsuite><testcase name="different"/></testsuite>', False),
                            (b'<testsuite><testcase><skipped/></testcase></testsuite>', True)):
            args, receipt, _ = self.fixture()
            if honest:
                receipt["tests"] = junit_bytes(xml)
            args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
            with self.assertRaises(ValueError):
                normalize_native(**args)

    def test_xfail_shard_report_cannot_pass_even_with_an_honest_or_forged_receipt(self):

        args, receipt, xml = self.fixture()
        args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
        self.assertEqual(normalize_native(**args)["status"], "success")  # negative control: unmodified shard passes
        xfail = xml.replace(b"</testcase>", b'<skipped type="pytest.xfail" message="known"/></testcase>')
        self.assertNotEqual(xfail, xml)
        for claimed in (junit_bytes(xfail), junit_bytes(xml)):  # honest counts, then forged zero-skip counts
            args, receipt, _ = self.fixture()
            receipt["tests"] = claimed
            args["archive"], args["artifact"]["digest"] = self.packed(receipt, xfail)
            with self.assertRaises(ValueError):
                normalize_native(**args)

    def test_candidate_cannot_substitute_workflow_or_a_different_merge_with_same_parents(self):

        args, receipt, xml = self.fixture()
        args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
        args["workflow_source"] = b"candidate workflow that fabricates results"
        with self.assertRaises(ValueError):
            normalize_native(**args)
        args, receipt, xml = self.fixture()
        receipt["execution_sha"] = receipt["workflow_sha"] = "f" * 40
        args["execution_commit"]["sha"] = "f" * 40
        args["execution_commit"]["commit"]["tree"]["sha"] = "2" * 40
        args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
        with self.assertRaises(ValueError):
            normalize_native(**args)

    def test_test_merge_on_a_base_that_moved_after_the_push_round_trips_and_stays_bound(self):
        """#1118 A1: GitHub's test merge and the payload base.sha can disagree.

        The producer records the base the merge was actually built on, and the
        consumer accepts that receipt only while the merge is current.
        """
        from unittest.mock import patch

        from scripts import ci_evidence
        from scripts.acceptance_evidence import normalize_native

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True,
                                               stderr=subprocess.DEVNULL).strip()

            def commit(message, path):
                (root / path).write_text(message + "\n")
                git("add", path)
                git("commit", "-m", message)
                return git("rev-parse", "HEAD")

            def merge_on(base):
                git("checkout", "--detach", base)
                git("merge", "--no-ff", "--no-edit", head)
                return git("rev-parse", "HEAD")

            def record(sha):
                parents = git("show", "-s", "--format=%P", sha).split()
                return {"sha": sha, "parents": [{"sha": p} for p in parents],
                        "commit": {"tree": {"sha": git("show", "-s", "--format=%T", sha)},
                                   "committer": {"date": "2026-10-26T12:00:00Z"}}}

            git("init", "-b", "main")
            git("config", "user.name", "Synthetic Test")
            git("config", "user.email", "synthetic@example.invalid")
            (root / "scripts").mkdir()
            base0 = commit("synthetic policy", "scripts/review_tooling.py")
            git("checkout", "-b", "candidate")
            head = commit("candidate change", "candidate")
            git("checkout", "main")
            base1 = commit("base moved after the push", "moved")
            base2 = commit("base moved again after the run", "again")

            def produce(event_base, merge):
                git("checkout", "--detach", merge)
                event = root / "event.json"  # untracked: the clean-checkout rule ignores it
                event.write_text(json.dumps({"pull_request": {
                    "number": 3, "head": {"sha": head}, "base": {"sha": event_base}}}))
                env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "synthetic/repository",
                       "GITHUB_REPOSITORY_ID": "42", "GITHUB_EVENT_NAME": "pull_request",
                       "GITHUB_WORKFLOW_REF": "synthetic/repository/.github/workflows/ci.yml@refs/pull/3/merge",
                       "GITHUB_WORKFLOW_SHA": merge, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                       "GITHUB_JOB": "unit", "GITHUB_ACTOR_ID": "7", "GITHUB_TRIGGERING_ACTOR": "synthetic"}
                with patch.object(ci_evidence, "ROOT", root), patch.dict(os.environ, env):
                    return ci_evidence.identity()

            def consume(identity, live_base, current_merge):
                args, receipt, xml = self.fixture()
                args["candidate"].update(head_sha=head, base_sha=live_base, execution_sha=current_merge)
                for holder in (args["run"], args["job"], args["artifact"]["workflow_run"]):
                    holder["head_sha"] = head
                receipt.update({key: identity[key] for key in
                                ("head_sha", "base_sha", "event_base_sha", "execution_sha", "execution_parents")})
                receipt["workflow_sha"] = identity["workflow_sha"]
                args["execution_commit"] = record(identity["execution_sha"])
                args["candidate_commit"] = record(current_merge)
                archive, args["artifact"]["digest"] = self.packed(receipt, xml)
                return normalize_native(**args, archive=archive)

            merge1 = merge_on(base1)
            # The base moved after the push: the payload still names base0, but
            # GitHub built the test merge on base1.
            receipt = produce(base0, merge1)
            self.assertEqual((receipt["base_sha"], receipt["event_base_sha"]), (base1, base0))
            self.assertEqual(receipt["execution_parents"], [base1, head])
            self.assertEqual(receipt["execution_sha"], merge1)
            self.assertEqual(consume(receipt, base1, merge1)["status"], "success")
            # The base moves again after the run: that receipt is stale, never current.
            merge2 = merge_on(base2)
            with self.assertRaises(ValueError):
                consume(receipt, base2, merge2)
            # A merge GitHub built on an older base than the payload names is
            # also refused against the newer live base.
            merge0 = merge_on(base0)
            lagging = produce(base1, merge0)
            self.assertEqual((lagging["base_sha"], lagging["event_base_sha"]), (base0, base1))
            with self.assertRaises(ValueError):
                consume(lagging, base1, merge1)
            # Negative control: a current merge still binds exactly.
            self.assertEqual(consume(produce(base2, merge2), base2, merge2)["status"], "success")

    def test_regenerated_merge_requires_api_tree_and_exact_parents_retaining_tested_sha(self):

        for fault in (None, "changed-tree", "missing-tree", "empty-tree", "invalid-tree",
                      "stale-base", "stale-head", "reversed-parents", "extra-parent",
                      "current-parents", "wrong-current-sha", "wrong-tested-sha", "wrong-workflow-sha"):
            with self.subTest(fault=fault):
                args, receipt, xml = self.fixture()
                args["candidate"]["execution_sha"] = "f" * 40
                args["candidate_commit"]["sha"] = "f" * 40
                tested = args["execution_commit"]
                current = args["candidate_commit"]
                if fault == "changed-tree":
                    tested["commit"]["tree"]["sha"] = "2" * 40
                elif fault == "missing-tree":
                    del tested["commit"]["tree"]
                elif fault in {"empty-tree", "invalid-tree"}:
                    tested["commit"]["tree"]["sha"] = current["commit"]["tree"]["sha"] = (
                        "" if fault == "empty-tree" else "not-a-tree-sha")
                elif fault in {"stale-base", "stale-head"}:
                    tested["parents"][int(fault == "stale-head")]["sha"] = "0" * 40
                elif fault == "reversed-parents":
                    tested["parents"].reverse()
                elif fault == "extra-parent":
                    tested["parents"].append({"sha": "0" * 40})
                elif fault == "current-parents":
                    current["parents"][0]["sha"] = "0" * 40
                elif fault == "wrong-current-sha":
                    current["sha"] = "0" * 40
                elif fault == "wrong-tested-sha":
                    tested["sha"] = "0" * 40
                elif fault == "wrong-workflow-sha":
                    receipt["workflow_sha"] = "f" * 40
                args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
                if fault is None:
                    result = normalize_native(**args)
                    self.assertEqual(result["status"], "success")
                    self.assertEqual(result["execution_sha"], "c" * 40)
                else:
                    with self.assertRaises((ValueError, KeyError)):
                        normalize_native(**args)

    def test_hazard_receipts_require_each_approved_behavioral_kill(self):
        hazard = {"id": "retained-state", "contract": "Preserve retained state", "target": "src/state.py",
                  "test": "tests/test_state.py::test_retained", "assertion": "assert retained == expected",
                  "before": "retained = expected", "after": "retained = None", "occurrences": 1,
                  "target_role": "production"}
        catalogue = json.dumps({"schema_version": 1, "hazards": [hazard]}).encode()
        policy = {"catalogue": catalogue, "runner_sha256": "f" * 64}
        result = {"id": "retained-state", "contract": "Preserve retained state", "target": "src/state.py",
                  "expected_test": "tests/test_state.py::test_retained",
                  "expected_assertion": "assert retained == expected",
                  "replacement": {"before": "retained = expected", "after": "retained = None",
                                  "occurrences": 1, "target_role": "production"},
                  "baseline": {"status": "baseline_pass", "exit_code": 0},
                  "mutation": {"status": "killed_by_behavior", "exit_code": 1,
                               "cause": "assert retained == expected"}}
        report = {"schema_version": 1, "candidate_sha": "c" * 40,
                  "catalogue_sha256": hashlib.sha256(catalogue).hexdigest(), "runner_sha256": "f" * 64,
                  "complete_catalogue": True, "passed": True, "results": [result]}

        def normalize(value, *, regenerated=False):
            args, receipt, _ = self.fixture()
            if regenerated:
                args["candidate"]["execution_sha"] = args["candidate_commit"]["sha"] = "e" * 40
            args["producer"] = next(p for p in PRODUCERS if p.lane == "hazards")
            args["job"]["name"] = "hazards"
            args["artifact"]["name"] = "evidence-hazards-attempt-2"
            args["hazard_policy"] = policy
            raw = json.dumps(value).encode()
            receipt.update(job_key="hazards", job_name="hazards", lane="hazards", tests=None,
                           result_sha256=hashlib.sha256(raw).hexdigest())
            args["archive"], args["artifact"]["digest"] = pack_archive(receipt, **{"results.json": raw})
            return normalize_native(**args)

        self.assertEqual(normalize(report)["status"], "success")
        regenerated = normalize(report, regenerated=True)
        self.assertEqual(regenerated["execution_sha"], "c" * 40)
        self.assertEqual(regenerated["results"]["candidate_sha"], "c" * 40)
        with self.assertRaises(ValueError):
            normalize({**report, "candidate_sha": "e" * 40}, regenerated=True)
        mutations = [({}, "empty report")]
        for field, value in (("results", []), ("results", [result, result]), ("complete_catalogue", False),
                             ("complete_catalogue", 1), ("passed", False), ("candidate_sha", "a" * 40),
                             ("runner_sha256", "0" * 64), ("catalogue_sha256", "0" * 64)):
            mutations.append(({**report, field: value}, field))
        for field, value in (("id", "unapproved"), ("expected_assertion", "assert True"),
                             ("target", "src/unrelated.py"),
                             ("replacement", {**result["replacement"], "occurrences": True}),
                             ("baseline", {"status": "baseline_failed", "exit_code": 1}),
                             ("mutation", {"status": "survived", "exit_code": 0}),
                             ("mutation", {"status": "killed_by_behavior", "exit_code": True,
                                           "cause": "assert retained == expected"}),
                             ("mutation", {"status": "killed_by_behavior", "exit_code": 1,
                                           "cause": "unrelated assertion"})):
            altered = copy.deepcopy(report)
            altered["results"][0][field] = value
            mutations.append((altered, field))
        for altered, label in mutations:
            with self.subTest(case=label, value=altered), self.assertRaises((ValueError, KeyError)):
                normalize(altered)


    def test_native_pagination_reads_later_pages_and_refuses_inconsistent_results(self):

        calls = []
        def get(endpoint):
            calls.append(endpoint)
            page = 1 if endpoint.endswith("&page=1") else 2
            rows = [{"id": number} for number in range(100)] if page == 1 else [{"id": 100}]
            return {"total_count": 101, "jobs": rows}
        result = paginate(get, "actions/runs/123/attempts/2/jobs", "jobs")
        self.assertEqual(result[-1], {"id": 100})
        self.assertEqual(calls, ["actions/runs/123/attempts/2/jobs?per_page=100&page=1",
                                 "actions/runs/123/attempts/2/jobs?per_page=100&page=2"])
        for response in ({"total_count": 101, "jobs": [{"id": 1}]},
                         {"total_count": 2, "jobs": [{"id": 1}, {"id": 1}]}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                paginate(lambda endpoint, response=response: response, "jobs", "jobs")

    def test_artifact_tampering_paths_duplicates_and_symlinks_are_rejected(self):
        for name, symlink, duplicate in (("../evidence.json", False, False), ("evidence.json", True, False),
                                        ("evidence.json", False, True)):
            with self.subTest(name=name, symlink=symlink, duplicate=duplicate):
                buffer = io.BytesIO()
                with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w") as archive:
                    warnings.simplefilter("ignore", UserWarning)
                    info = zipfile.ZipInfo(name)
                    if symlink:
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, "{}")
                    if duplicate:
                        archive.writestr(name, "{}")
                raw = buffer.getvalue()
                with self.assertRaises(ValueError):
                    artifact_members(raw, "sha256:" + hashlib.sha256(raw).hexdigest())
        _args, receipt, xml = self.fixture()
        raw, digest = self.packed(receipt, xml)
        with self.assertRaises(ValueError):
            artifact_members(raw + b"tampered", digest)


class TestAcceptanceSnapshot(unittest.TestCase):

    def test_paginated_pr_files_preserve_rename_bytes_and_refuse_truncation(self):

        class API:
            prefix = "repos/synthetic/repository/"
            def get(self, endpoint):
                if endpoint.endswith("page=1"):
                    return [{"filename": f"docs/page-{i}.md", "status": "modified"} for i in range(100)]
                return [{"filename": "docs/new \r\nname.md", "previous_filename": "src/old \tname.py",
                         "status": "renamed"}]
        api = API()
        paths = changed_pr_paths(api, {"number": 3, "changed_files": 101})
        self.assertEqual(paths[-2:], ["docs/new \r\nname.md", "src/old \tname.py"])
        with self.assertRaises(ValueError):
            changed_pr_paths(api, {"number": 3, "changed_files": 102})

    def test_recheck_refuses_candidate_and_attempt_movement(self):
        pr = {"number": 3, "user": {"id": 99}, "state": "open", "mergeable": True, "draft": False,
              "merge_commit_sha": "c" * 40, "head": {"sha": "a" * 40, "repo": {"id": 84}},
              "base": {"sha": "b" * 40, "ref": "main", "repo": {
                  "id": 42, "full_name": "synthetic/repository", "default_branch": "main"}}}
        class API:
            repository = "synthetic/repository"
            prefix = "repos/synthetic/repository/"
            def __init__(self):
                self.pr = copy.deepcopy(pr)
                self.base_ref = "b" * 40
                self.run = {"id": 123, "path": ".github/workflows/ci.yml",
                            "run_attempt": 1, "status": "completed"}
            def get(self, endpoint):
                if "/reviews?" in endpoint or "/comments?" in endpoint or "/permission" in endpoint:
                    raise AssertionError("Technical recheck must not fetch human votes")
                if endpoint.endswith("git/ref/heads/main"):
                    return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.base_ref}}
                if "actions/runs?" in endpoint:
                    return {"total_count": 1, "workflow_runs": [self.run]}
                return self.pr
        result = {"candidate": candidate_identity(pr, API.repository), "draft": False,
                  "review_identity": {"id": 10}, "all_prerequisites_met": False,
                  "runs": {"ci.yml": {"run_id": 123, "run_attempt": 1, "status": "completed"}}}
        recheck_current(API(), result)
        moved_draft = API()
        moved_draft.pr["draft"] = True
        recheck_current(moved_draft, result)
        for mutation in ("head", "base", "merge", "attempt", "run", "live_base"):
            with self.subTest(mutation=mutation):
                api = API()
                if mutation in {"head", "base"}:
                    api.pr[mutation]["sha"] = "d" * 40
                elif mutation == "merge":
                    api.pr["merge_commit_sha"] = "d" * 40
                elif mutation == "attempt":
                    api.run["run_attempt"] = 2
                elif mutation == "live_base":
                    api.base_ref = "d" * 40
                elif mutation == "run":
                    api.run["id"] = 124
                with self.assertRaises(ValueError):
                    recheck_current(api, result)


    def test_bulk_publisher_reports_candidate_failures_without_failing_reconciliation(self):
        with patch('scripts.acceptance.GitHub'), patch('scripts.acceptance.paginate', return_value=[{'number': 3}]), \
             patch('scripts.acceptance.publish_acceptance') as publish, contextlib.redirect_stdout(io.StringIO()):
            publish.return_value = {'all_prerequisites_met': False, 'check_run_id': 17}
            args = ['--repository', 'synthetic/repository', '--all-open', '--publish']
            self.assertEqual(main(args), 0)
            self.assertEqual(main(['--repository', 'synthetic/repository', '--pr', '3', '--publish']), 1)
            publish.side_effect = ValueError('API write failed')
            self.assertEqual(main(args), 1)

    def test_head_sha_selects_only_candidates_at_that_exact_head(self):
        def pull(number, sha):
            return {'number': number, 'head': {'sha': sha * 40}}

        open_prs = [pull(3, 'a'), pull(4, 'b'), pull(5, 'a')]
        base = ['--repository', 'synthetic/repository', '--publish']
        with patch('scripts.acceptance.GitHub'), patch('scripts.acceptance.paginate', return_value=open_prs), \
             patch('scripts.acceptance.publish_acceptance') as publish, contextlib.redirect_stdout(io.StringIO()) as out:
            publish.return_value = {'all_prerequisites_met': False, 'check_run_id': 17}
            self.assertEqual(main([*base, '--head-sha', 'a' * 40]), 0)
            self.assertEqual([call.args[1] for call in publish.call_args_list], [3, 5])
            publish.reset_mock()
            # A superseded head selects nothing and publishes nothing.
            self.assertEqual(main([*base, '--head-sha', 'c' * 40]), 0)
            publish.assert_not_called()
            self.assertEqual(out.getvalue().splitlines()[-1], '[]')
            for bad in ('A' * 40, 'a' * 39, 'a' * 40 + ' --all-open'):
                with self.assertRaises(SystemExit):
                    main([*base, '--head-sha', bad])

    def test_read_only_cli_never_emits_success_after_recheck_failure(self):
        result = {"all_prerequisites_met": True}
        with patch("scripts.acceptance.collect_acceptance", return_value=result), \
             patch("scripts.acceptance.recheck_current") as recheck:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--repository", "synthetic/repository", "--pr", "3"]), 0)
            self.assertTrue(json.loads(output.getvalue())["all_prerequisites_met"])
            recheck.side_effect = ValueError("sensitive upstream detail")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--repository", "synthetic/repository", "--pr", "3"]), 1)
            self.assertFalse(json.loads(output.getvalue())["all_prerequisites_met"])
            self.assertNotIn("sensitive", output.getvalue())

    def test_native_summary_ignores_human_comments_and_review_state(self):
        pr = {"number": 3, "user": {"id": 99}, "state": "open", "mergeable": True, "draft": False, "changed_files": 1,
              "merge_commit_sha": "c" * 40, "head": {"sha": "a" * 40, "repo": {"id": 84}},
              "base": {"sha": "b" * 40, "ref": "main", "repo": {
                  "id": 42, "full_name": "synthetic/repository", "default_branch": "main"}}}
        candidate = candidate_identity(pr, "synthetic/repository")
        payload = {"schema_version": 1, "head_sha": "a" * 40, "base_sha": "b" * 40,
                   "execution_sha": "c" * 40, "code_assessment": "acceptable",
                   "verification": "complete", "candidate_currentness": "current",
                   "merge_readiness": "ready_for_maintainer", "material_findings": [], "evidence": {}}
        class API:
            repository = "synthetic/repository"
            prefix = "repos/synthetic/repository/"
            def __init__(self):
                self.pr = copy.deepcopy(pr)
                self.payload = copy.deepcopy(payload)
            def get(self, endpoint):
                if "/files?" in endpoint:
                    return [{"filename": "docs/explanation.md", "status": "modified"}]
                if "/reviews?" in endpoint:
                    raise AssertionError("Technical CI must not fetch reviews")
                if "/comments?" in endpoint or "/permission" in endpoint:
                    raise AssertionError("Technical CI must not fetch comment votes")
                return self.pr
        native = {"candidate": candidate, "lane_statuses": {"context_check": "success"},
                  "native": [], "runs": {}, "policy_sha256": "d" * 64, "policy_inputs": {}}
        with patch("scripts.acceptance.collect_native", return_value=native):
            api = API()
            result = collect_acceptance(api, 3, pathlib.Path.cwd())
            self.assertTrue(result["all_prerequisites_met"])
            self.assertEqual({lane["name"] for lane in result["lanes"] if lane["required"]},
                             {"context_check"})
            native["native"] = [{"status": "success", "lane": "context_check", "execution_sha": "f" * 40}]
            regenerated = collect_acceptance(api, 3, pathlib.Path.cwd())
            self.assertIn("`" + "f" * 40 + "`", regenerated["markdown_report"])
            self.assertIn("identical file trees", regenerated["markdown_report"])
            self.assertEqual(regenerated["candidate"]["execution_sha"], "c" * 40)
            # Draft movement is candidate identity, owned by the recheck test.
            api.payload["code_assessment"] = "changes_required"
            self.assertTrue(collect_acceptance(api, 3, pathlib.Path.cwd())["all_prerequisites_met"])
            api.payload = payload
            native["lane_statuses"]["context_check"] = "skipped"
            self.assertFalse(collect_acceptance(api, 3, pathlib.Path.cwd())["all_prerequisites_met"])

    def test_publisher_pending_precedes_collection_and_stale_result_is_failure(self):
        events = []
        class API:
            prefix = "repos/synthetic/repository/"
            def get(self, endpoint):
                return {"head": {"sha": "a" * 40}}
            def write(self, endpoint, payload, *, method):
                events.append((method, copy.deepcopy(payload)))
                return {"id": 17}
        def collect(*args):
            self.assertEqual(events[0][1]["status"], "in_progress")
            return {"candidate": {"head_sha": "a" * 40}, "all_prerequisites_met": True,
                    "markdown_report": "Verified summary"}
        with patch("scripts.acceptance.collect_acceptance", side_effect=collect), \
             patch("scripts.acceptance.recheck_current") as recheck:
            result = publish_acceptance(API(), 3, pathlib.Path.cwd())
            self.assertTrue(result["all_prerequisites_met"])
            self.assertEqual(events[-1][1]["conclusion"], "success")
            events.clear()
            recheck.side_effect = ValueError("upstream detail")
            result = publish_acceptance(API(), 3, pathlib.Path.cwd())
            self.assertFalse(result["all_prerequisites_met"])
            self.assertEqual(events[-1][1]["conclusion"], "failure")
            self.assertNotIn("upstream detail", json.dumps(events))

    def test_publisher_workflow_keeps_candidate_data_out_of_privileged_execution(self):

        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/acceptance.yml').read_text())
        events = workflow.get('on', workflow.get(True))
        self.assertTrue({'pull_request_target', 'workflow_run', 'push', 'schedule'} <= events.keys())
        for name, job in workflow['jobs'].items():
            self.assertIn("github.ref == 'refs/heads/main'", job['if'])
            checkout = next(step for step in job['steps'] if 'actions/checkout@' in step.get('uses', ''))
            self.assertEqual(checkout['with']['ref'], 'main')
            self.assertFalse(checkout['with']['persist-credentials'])
            commands = [step['run'] for step in job['steps'] if 'run' in step]
            self.assertEqual(len(commands), 1)
            self.assertIn('python -m scripts.acceptance', commands[0])
            self.assertNotIn('github.event.', commands[0])
            if name == 'trusted-app':
                self.assertIn("ACCEPTANCE_APP_ENABLED == 'true'", job['if'])
                self.assertEqual(job['environment'], 'acceptance-publisher')
                token = next(step for step in job['steps'] if step.get('id') == 'app')
                self.assertEqual(token['with']['permission-checks'], 'write')
                self.assertNotIn('permission-issues', token['with'])
                self.assertEqual(token['with']['permission-pull-requests'], 'read')
                self.assertTrue(all(value == 'read' for value in job['permissions'].values()))
                self.assertEqual(token['with']['private-key'], '${{ secrets.ACCEPTANCE_APP_PRIVATE_KEY }}')
            else:
                self.assertNotIn('environment', job)
                self.assertNotIn('issues', job['permissions'])
                self.assertEqual(job['permissions']['pull-requests'], 'read')
                self.assertEqual(job['permissions']['contents'], 'read')
        # Candidate events serialize per head SHA; everything else keeps the
        # single global group so base pushes and the sweep stay ordered.
        group = workflow['concurrency']['group']
        self.assertIn('github.event.pull_request.head.sha', group)
        self.assertIn("github.event.workflow_run.event == 'pull_request'", group)
        self.assertIs(workflow['concurrency']['cancel-in-progress'], False)
        for job in workflow['jobs'].values():
            step = next(step for step in job['steps'] if 'run' in step)
            self.assertIn('--all-open', step['run'])
            self.assertIn('--head-sha $ACCEPTANCE_HEAD_SHA', step['run'])
            self.assertIn("github.event.workflow_run.event == 'pull_request'", step['env']['ACCEPTANCE_HEAD_SHA'])
        self.assertNotIn('issue_comment', events)
        self.assertNotIn('acceptance-review-signal', events['workflow_run']['workflows'])
        self.assertFalse((root / '.github/workflows/acceptance-review-signal.yml').exists())

    def test_gitlab_l1_note_appends_literal_body_without_claiming_marker_ownership(self):

        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.gitlab-ci.yml').read_text())
        job = next(value for value in workflow.values() if isinstance(value, dict)
                   and any('NOTE_BODY=' in step for step in value.get('script', []) if isinstance(step, str)))
        script = next(step for step in job['script'] if 'NOTE_BODY=' in step)
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            capture = temp / 'curl-args.json'
            stub = temp / 'curl'
            stub.write_text('#!' + sys.executable + '\nimport json, os, sys\n'
                            'open(os.environ["CURL_CAPTURE"], "w").write(json.dumps(sys.argv[1:]))\n')
            stub.chmod(0o755)
            (temp / 'eval-delta.md').write_text('@literal;type=text/plain\n<!-- gate-l1-report -->')
            env = {**os.environ, 'PATH': str(temp) + os.pathsep + os.environ['PATH'],
                   'CURL_CAPTURE': str(capture), 'CI_MERGE_REQUEST_IID': '3', 'GITLAB_TOKEN': 'synthetic',
                   'CI_COMMIT_SHA': 'a' * 40, 'CI_PIPELINE_ID': '123', 'CI_JOB_URL': 'https://example.invalid/jobs/9',
                   'CI_API_V4_URL': 'https://example.invalid/api/v4', 'CI_PROJECT_ID': '7'}
            for key in ('CI_MERGE_REQUEST_SOURCE_BRANCH_SHA', 'CI_MERGE_REQUEST_TARGET_BRANCH_SHA',
                        'CI_MERGE_REQUEST_DIFF_BASE_SHA'):
                env.pop(key, None)
            subprocess.run(['sh', '-c', script], cwd=temp, env=env, check=True)
            args = json.loads(capture.read_text())
            self.assertEqual(args[args.index('-X') + 1], 'POST')
            self.assertEqual(args[args.index('-X') + 2], 'https://example.invalid/api/v4/projects/7/merge_requests/3/notes')
            body = args[args.index('--form-string') + 1]
            self.assertIn('Head: ' + 'a' * 40, body)
            self.assertIn('Target: unavailable', body)
            self.assertIn('@literal;type=text/plain', body)
            self.assertIn('Job: https://example.invalid/jobs/9', body)


class TestAcceptancePendingPublication(unittest.TestCase):
    """In-progress current runs leave the check in progress; stale runs fail."""

    def approved_base(self, root):
        return approved_repository(root)

    def context_archive(self, base, execution_sha="c" * 40):
        """A valid context_check receipt; this producer emits no JUnit file."""
        _args, receipt, _xml = TestNativeEvidenceConsumer().fixture()
        receipt.update(
            base_sha=base, execution_sha=execution_sha, workflow_sha=execution_sha,
            execution_parents=[base, "a" * 40],
            job_key="check-context", job_name="check-context", lane="context_check",
            workflow_ref=("synthetic/repository/.github/workflows/agent-context.yml"
                          "@refs/pull/3/merge"),
            policy_sha256=hashlib.sha256(b"approved policy").hexdigest(),
            producer_sha256=hashlib.sha256(b"approved producer").hexdigest())
        receipt.pop("unit_coverage")
        raw, digest = pack_archive(receipt)
        return raw, digest, len(raw)

    def scenario(self, *, run_status, started="2026-10-26T12:00:05Z", head_sha="a" * 40,
                 job_conclusion=None, success=False, corrupt=False, twin=False,
                 extra_active_run=False, pull_request_number=None):

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            base = self.approved_base(root)
            repository = "synthetic/repository"
            run = {"id": 123, "run_attempt": 2, "event": "pull_request",
                   "path": ".github/workflows/agent-context.yml", "head_sha": head_sha,
                   "status": run_status, "created_at": started, "run_started_at": started,
                   "repository": {"id": 42, "full_name": repository},
                   "head_repository": {"id": 84},
                   "actor": {"id": 7}, "triggering_actor": {"login": "synthetic"}}
            if pull_request_number is not None:
                run["pull_requests"] = [{"number": pull_request_number}]
            runs = [run]
            if extra_active_run:
                runs.append({**run, "id": 124, "path": ".github/workflows/ci.yml",
                             "status": "in_progress"})
            jobs = [] if job_conclusion is None else [
                {"id": 456, "run_id": 123, "run_attempt": 2, "head_sha": "a" * 40,
                 "name": "check-context", "status": "completed", "conclusion": job_conclusion}]
            artifacts = []
            archive = None
            if success:
                tested_sha = "d" * 40 if twin else "c" * 40
                archive, digest, size = self.context_archive(base, tested_sha)
                if corrupt:
                    archive += b"tampered"
                jobs = [{"id": 456, "run_id": 123, "run_attempt": 2, "head_sha": "a" * 40,
                         "name": "check-context", "status": "completed", "conclusion": "success"}]
                artifacts = [{"id": 789, "expired": False, "size_in_bytes": size, "digest": digest,
                              "name": "evidence-context-attempt-2",
                              "workflow_run": {"id": 123, "repository_id": 42,
                                               "head_repository_id": 84, "head_sha": "a" * 40}}]
            pr = {"number": 3, "state": "open", "mergeable": True, "draft": False,
                  "changed_files": 1, "merge_commit_sha": "c" * 40,
                  "head": {"sha": "a" * 40, "repo": {"id": 84}},
                  "base": {"sha": base, "ref": "main", "repo": {
                      "id": 42, "full_name": repository, "default_branch": "main"}}}
            execution = {"sha": "c" * 40, "parents": [{"sha": base}, {"sha": "a" * 40}],
                         "commit": {"tree": {"sha": "1" * 40},
                                    "committer": {"date": "2026-10-26T12:00:00Z"}}}
            twin_commit = {"sha": "d" * 40, "parents": [{"sha": base}, {"sha": "a" * 40}],
                           "commit": {"tree": {"sha": "1" * 40},
                                      "committer": {"date": "2026-10-26T11:00:00Z"}}}

            class API:
                def __init__(self):
                    self.writes = []

                def get(self, endpoint):
                    if endpoint.endswith("git/ref/heads/main"):
                        return {"ref": "refs/heads/main",
                                "object": {"type": "commit", "sha": base}}
                    if "actions/workflows/verifier-update.yml/runs?" in endpoint:
                        return {"total_count": 0, "workflow_runs": []}
                    if "actions/runs?event=pull_request" in endpoint:
                        return {"total_count": len(runs), "workflow_runs": runs}
                    if endpoint.endswith("/pulls/3"):
                        return pr
                    if endpoint.endswith("/pulls/3/files?per_page=100&page=1"):
                        return [{"filename": "docs/pending.md", "status": "modified"}]
                    if "/commits/" in endpoint:
                        if endpoint.endswith("c" * 40):
                            return execution
                        if endpoint.endswith("d" * 40):
                            return twin_commit
                        raise AssertionError(endpoint)
                    if endpoint.endswith("/123"):
                        return run
                    if endpoint.endswith("/124"):
                        return runs[1]
                    if endpoint.endswith("/123/attempts/2/jobs?per_page=100&page=1"):
                        return {"total_count": len(jobs), "jobs": jobs}
                    if endpoint.endswith("/123/artifacts?per_page=100&page=1"):
                        return {"total_count": len(artifacts), "artifacts": artifacts}
                    if endpoint.endswith("/124/attempts/2/jobs?per_page=100&page=1"):
                        return {"total_count": 0, "jobs": []}
                    if endpoint.endswith("/124/artifacts?per_page=100&page=1"):
                        return {"total_count": 0, "artifacts": []}
                    raise AssertionError(endpoint)

                def raw(self, endpoint):
                    assert endpoint.endswith("/artifacts/789/zip") and archive is not None
                    return archive

                def blob(self, sha, path):
                    assert sha == "c" * 40
                    if path == "scripts/review_tooling.py":
                        return b"approved policy"
                    if path == "scripts/ci_evidence.py":
                        return b"approved producer"
                    return (root / path).read_bytes()

                def write(self, endpoint, payload, *, method):
                    self.writes.append({"endpoint": endpoint, "method": method, **payload})
                    return {"id": 17}

            API.repository = repository
            API.prefix = "repos/" + repository + "/"
            api = API()
            result = publish_acceptance(api, 3, root)
            return result, api.writes

    def patch_payloads(self, writes):
        return [write for write in writes if write["method"] == "PATCH"]

    def assert_pending_patch(self, writes):
        patch = self.patch_payloads(writes)[0]
        self.assertEqual(patch["status"], "in_progress")
        self.assertNotIn("conclusion", patch)
        return patch

    def assert_failure_patch(self, writes):
        patches = self.patch_payloads(writes)
        self.assertEqual([patch["status"] for patch in patches], ["completed"])
        self.assertEqual([patch["conclusion"] for patch in patches], ["failure"])

    def lane(self, result):
        return next(item for item in result["native"] if item["lane"] == "context_check")

    def test_in_progress_current_run_leaves_check_in_progress(self):
        for pull_request_number in (None, 3):
            with self.subTest(pull_request_number=pull_request_number):
                result, writes = self.scenario(run_status="in_progress",
                                               pull_request_number=pull_request_number)
                self.assertFalse(result["all_prerequisites_met"])
                self.assertTrue(result["pending"])
                self.assertIn("context_check", result["pending_lanes"])
                self.assertEqual(writes[0]["status"], "in_progress")
                patch = self.assert_pending_patch(writes)
                self.assertIn("still queued or in progress", patch["output"]["summary"])

    def test_valid_active_evidence_stays_pending(self):
        result, writes = self.scenario(run_status="in_progress", success=True)
        self.assertTrue(result["pending"])
        lane = self.lane(result)
        self.assertEqual(lane["status"], "pending")
        self.assert_pending_patch(writes)

    def test_corrupt_active_artifact_fails_unverified(self):
        result, writes = self.scenario(run_status="in_progress", success=True, corrupt=True)
        self.assertFalse(result["pending"])
        lane = self.lane(result)
        self.assertEqual(lane["status"], "unverified")
        self.assert_failure_patch(writes)

    def test_completed_run_with_missing_lane_publishes_failure(self):
        result, writes = self.scenario(run_status="completed")
        self.assertFalse(result["pending"])
        self.assertEqual(result["pending_lanes"], [])
        self.assert_failure_patch(writes)

    def test_completed_job_without_artifact_fails_while_run_is_active_or_complete(self):
        for run_status in ("in_progress", "completed"):
            with self.subTest(run_status=run_status):
                result, writes = self.scenario(run_status=run_status, job_conclusion="success")
                self.assertFalse(result["pending"])
                self.assertEqual(self.lane(result)["status"], "missing")
                self.assert_failure_patch(writes)

    def test_completed_success_publishes_success(self):
        result, writes = self.scenario(run_status="completed", success=True)
        self.assertTrue(result["all_prerequisites_met"])
        self.assertFalse(result["pending"])
        lane = self.lane(result)
        self.assertEqual(lane["status"], "success")
        self.assertEqual(lane["execution_sha"], "c" * 40)
        patches = self.patch_payloads(writes)
        self.assertEqual([patch["status"] for patch in patches], ["completed"])
        self.assertEqual([patch["conclusion"] for patch in patches], ["success"])

    def test_completed_stale_run_claiming_current_execution_fails(self):
        result, writes = self.scenario(run_status="completed", success=True,
                                       started="2026-10-26T11:59:00Z")
        self.assertFalse(result["all_prerequisites_met"])
        self.assertFalse(result["pending"])
        lane = self.lane(result)
        self.assertEqual(lane["status"], "stale")
        self.assert_failure_patch(writes)

    def test_completed_stale_run_content_twin_succeeds(self):
        result, writes = self.scenario(run_status="completed", success=True, twin=True,
                                       started="2026-10-26T11:59:00Z")
        self.assertTrue(result["all_prerequisites_met"])
        lane = self.lane(result)
        self.assertEqual(lane["status"], "success")
        self.assertEqual(lane["execution_sha"], "d" * 40)
        patches = self.patch_payloads(writes)
        self.assertEqual([patch["status"] for patch in patches], ["completed"])
        self.assertEqual([patch["conclusion"] for patch in patches], ["success"])

    def test_completed_missing_lane_blocks_while_another_run_is_active(self):
        result, writes = self.scenario(run_status="completed", extra_active_run=True)
        self.assertFalse(result["pending"])
        self.assertNotIn("context_check", result["pending_lanes"])
        self.assertIn("lint_and_types", result["pending_lanes"])
        self.assert_failure_patch(writes)

    def test_failed_lane_publishes_failure(self):
        result, writes = self.scenario(run_status="completed", job_conclusion="failure")
        self.assertFalse(result["pending"])
        lane = self.lane(result)
        self.assertEqual(lane["status"], "failure")
        self.assert_failure_patch(writes)

    def test_completed_non_success_conclusions_fail(self):
        for conclusion in ("neutral", "action_required"):
            with self.subTest(conclusion=conclusion):
                result, writes = self.scenario(run_status="completed", job_conclusion=conclusion)
                self.assertFalse(result["pending"])
                lane = self.lane(result)
                self.assertEqual(lane["status"], "unverified")
                self.assert_failure_patch(writes)

    def test_run_that_is_not_current_never_leaves_the_check_pending(self):
        for label, scenario, stale in (
            ("started before the test merge", {"started": "2026-10-26T11:59:00Z"}, True),
            ("another head", {"head_sha": "d" * 40}, False),
            ("another pull request", {"pull_request_number": 4}, True),
        ):
            with self.subTest(label=label):
                result, writes = self.scenario(run_status="in_progress", **scenario)
                self.assertFalse(result["pending"])
                self.assertEqual(result["pending_lanes"], [])
                if stale:
                    self.assertEqual([run["run_id"] for run in result["stale_runs"]], [123])
                self.assert_failure_patch(writes)


class ReviewTemplateTests(unittest.TestCase):
    def test_template_binds_current_merge_without_approving_or_writing(self):
        pr = {'number': 3, 'state': 'open', 'mergeable': True, 'draft': False,
              'head': {'sha': 'a' * 40, 'repo': {'id': 84}},
              'base': {'sha': 'b' * 40, 'ref': 'main', 'repo': {
                  'id': 42, 'full_name': 'synthetic/repository', 'default_branch': 'main'}},
              'merge_commit_sha': 'c' * 40}

        class API:
            repository = 'synthetic/repository'
            prefix = 'repos/synthetic/repository/'

            def __init__(self, move=False, wrong_parents=False, base_refs=None):
                self.base_refs = iter(base_refs or ["b" * 40, "b" * 40])
                self.reads = 0
                self.move = move
                self.wrong_parents = wrong_parents

            def get(self, endpoint):
                if endpoint.endswith('git/ref/heads/main'):
                    return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': next(self.base_refs)}}
                if endpoint.endswith('pulls/3'):
                    self.reads += 1
                    result = copy.deepcopy(pr)
                    if self.move and self.reads == 2:
                        result['head']['sha'] = 'd' * 40
                    return result
                assert endpoint.endswith('commits/' + 'c' * 40)
                return {'sha': 'c' * 40, 'parents': [
                    {'sha': ('d' if self.wrong_parents else 'b') * 40}, {'sha': 'a' * 40}]}

        result = review_template(API(), 3)
        self.assertEqual([result[k] for k in ('head_sha', 'base_sha', 'execution_sha')],
                         ['a' * 40, 'b' * 40, 'c' * 40])
        self.assertEqual(validate_review_payload(result).merge_readiness, 'not_ready')
        for api in (API(move=True), API(wrong_parents=True),
                    API(base_refs=["d" * 40]), API(base_refs=["b" * 40, "d" * 40])):
            with self.subTest(api=api), self.assertRaises(ValueError):
                review_template(api, 3)

    def test_live_base_requires_exact_commit_ref_and_encodes_branch(self):
        branch = 'release/topic#雪'
        pr = {'base': {'ref': branch}}
        candidate = {'base_sha': 'b' * 40}
        valid = {'ref': 'refs/heads/' + branch, 'object': {'type': 'commit', 'sha': 'b' * 40}}

        class API:
            prefix = 'repos/synthetic/repository/'

            def __init__(self, record):
                self.record = record

            def get(self, endpoint):
                self.endpoint = endpoint
                return self.record

        api = API(valid)
        require_current_base(api, pr, candidate)
        self.assertEqual(api.endpoint, 'repos/synthetic/repository/git/ref/heads/'
                         'release/topic%23%E9%9B%AA')
        for key, value in [('ref', 'refs/heads/other'), ('type', 'tag'), ('sha', 'd' * 40)]:
            wrong = copy.deepcopy(valid)
            target = wrong if key == 'ref' else wrong['object']
            target[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                require_current_base(API(wrong), pr, candidate)
        with self.assertRaises(KeyError):
            require_current_base(API({}), pr, candidate)

    def test_template_cli_refuses_publication_and_bulk_selection(self):
        for extra in (['--pr', '3', '--publish'], ['--all-open']):
            with self.subTest(extra=extra), patch('scripts.acceptance.GitHub') as api, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(['--repository', 'synthetic/repository', '--review-template', *extra])
            self.assertEqual(error.exception.code, 2)
            api.assert_not_called()

    def test_failed_acceptance_still_exports_identity_template_and_check_link(self):
        class API:
            repository = 'synthetic/repository'
            prefix = 'repos/synthetic/repository/'

            def __init__(self):
                self.writes = []

            def get(self, endpoint):
                return {'head': {'sha': 'a' * 40}}

            def write(self, endpoint, payload, *, method):
                self.writes.append((method, payload))
                return {'id': 17}

        template = {'head_sha': 'a' * 40, 'base_sha': 'b' * 40, 'execution_sha': 'c' * 40,
                    'code_assessment': '<human input>'}
        with tempfile.TemporaryDirectory() as directory:
            api = API()
            with patch('scripts.acceptance.review_template', return_value=template), \
                 patch('scripts.acceptance.post_review_template') as post, \
                 patch('scripts.acceptance.collect_acceptance', side_effect=ValueError('private diagnostic')):
                result = publish_acceptance(api, 3, pathlib.Path.cwd(),
                                            template_directory=pathlib.Path(directory), publisher_run_id=23,
                                            post_templates=True)
            post.assert_called_once_with(api, 3, template)
            self.assertFalse(result['all_prerequisites_met'])
            self.assertEqual(json.loads((pathlib.Path(directory) / ('pr-3-' + 'a' * 40 + '.json')).read_text()),
                             template)
            summary = api.writes[-1][1]['output']['summary']
            self.assertIn('https://github.com/synthetic/repository/actions/runs/23', summary)
            self.assertIn('review-templates', summary)
            self.assertNotIn('private diagnostic', summary)
            self.assertEqual(api.writes[-1][1]['conclusion'], 'failure')

    def test_moved_template_is_not_exported_for_old_check_head(self):
        class API:
            prefix = 'repos/synthetic/repository/'

            def __init__(self):
                self.last = None

            def get(self, endpoint):
                return {'head': {'sha': 'a' * 40}}

            def write(self, endpoint, payload, *, method):
                self.last = payload
                return {'id': 17}

        with tempfile.TemporaryDirectory() as directory:
            api = API()
            with patch('scripts.acceptance.review_template', return_value={'head_sha': 'd' * 40}), \
                 patch('scripts.acceptance.collect_acceptance', side_effect=ValueError):
                publish_acceptance(api, 3, pathlib.Path.cwd(),
                                   template_directory=pathlib.Path(directory), publisher_run_id=23)
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])
            self.assertIn('Review template unavailable', api.last['output']['summary'])





    def test_template_comments_append_deduplicate_and_preserve_human_text(self):
        template = {'head_sha': 'a' * 40, 'base_sha': 'b' * 40, 'execution_sha': 'c' * 40,
                    'code_assessment': '<human input>'}

        class API:
            prefix = 'repos/synthetic/repository/'

            def __init__(self):
                self.comments = [{'id': 1, 'body': '<!-- generated-human-review-template --> Human finding'}]
                self.writes = []

            def get(self, endpoint):
                return copy.deepcopy(self.comments)

            def write(self, endpoint, payload, *, method):
                self.writes.append((endpoint, method))
                self.comments.append({'id': len(self.comments) + 1, **payload})
                return self.comments[-1]

        api = API()
        original = copy.deepcopy(api.comments[0])
        post_review_template(api, 3, template)
        post_review_template(api, 3, template)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.comments[0], original)
        self.assertEqual(api.comments[1]['body'], review_template_comment(template))
        template['execution_sha'] = 'd' * 40
        post_review_template(api, 3, template)
        self.assertEqual(api.writes, [('repos/synthetic/repository/issues/3/comments', 'POST')] * 2)
        self.assertEqual(api.comments[0], original)

    def test_comment_write_boundary_cannot_edit_reviews_or_other_repositories(self):
        api = GitHub('synthetic/repository')
        for endpoint, method in [('repos/synthetic/repository/issues/3/comments', 'PATCH'),
                                 ('repos/synthetic/repository/issues/comments/17', 'PATCH'),
                                 ('repos/synthetic/repository/pulls/3/reviews', 'POST'),
                                 ('repos/other/repository/issues/3/comments', 'POST')]:
            with self.subTest(endpoint=endpoint), patch('scripts.acceptance.subprocess.run') as run, \
                 self.assertRaises(ValueError):
                api.write(endpoint, {'body': 'template'}, method=method)
            run.assert_not_called()

class TestVerifierUpdateDecision(unittest.TestCase):
    """Trust changes must bind real native data, without relaxing test outcomes."""

    def fixture(self):

        candidate = {"repository": "synthetic/repository", "repository_id": 42, "number": 3,
                     "head_sha": "a" * 40, "base_sha": "b" * 40, "execution_sha": "c" * 40,
                     "head_repository_id": 42}
        class API:
            repository = "synthetic/repository"
            prefix = "repos/synthetic/repository/"
            def __init__(self):
                self.candidate = copy.deepcopy(candidate)
                self.sources = {"scripts/check_hazard_sensitivity.py": b"changed runner"}
                self.run = {"id": 900, "run_attempt": 1, "display_title": "Verifier PR 3: approve",
                            "created_at": "2026-09-25T12:00:01Z",
                            "event": "workflow_dispatch", "path": ".github/workflows/verifier-update.yml",
                            "head_sha": "b" * 40, "head_branch": "main", "status": "completed",
                            "conclusion": "success", "repository": {"id": 42, "full_name": self.repository},
                            "head_repository": {"id": 42}, "actor": {"login": "yonatan895", "type": "User"},
                            "triggering_actor": {"login": "yonatan895", "type": "User"}}
                self.runs = [self.run]
                self.job = {"id": 901, "name": "record-decision", "conclusion": "success", "status": "completed",
                            "run_id": 900, "run_attempt": 1, "head_sha": "b" * 40}
                self.artifact = {"id": 902, "expired": False, "name": "verifier-decision-1",
                                 "workflow_run": {"id": 900, "repository_id": 42,
                                                  "head_repository_id": 42, "head_sha": "b" * 40}}
                self.record = {"schema_version": 1, "candidate": copy.deepcopy(candidate), "decision": "approve",
                               "pr_updated_at": "2026-09-25T12:00:00Z",
                               "run_id": 900, "run_attempt": 1,
                               "inputs": {k: hashlib.sha256(v).hexdigest() for k, v in self.sources.items()}}
                self.commits = {}
                self.pack()
                self.writes = []
            def pack(self):
                self.archive, digest = pack_archive(self.record)
                self.artifact.update(size_in_bytes=len(self.archive), digest=digest)
            def get(self, endpoint):
                if "actions/workflows/verifier-update.yml/runs?" in endpoint:
                    return {"total_count": len(self.runs), "workflow_runs": self.runs}
                if endpoint.endswith(("/900", "/910")):
                    return self.run
                if "/jobs?" in endpoint:
                    return {"total_count": 1, "jobs": [self.job]}
                if "/artifacts?" in endpoint:
                    return {"total_count": 1, "artifacts": [self.artifact]}
                if "/pulls/3" in endpoint:
                    c = self.candidate
                    return {"number": 3, "state": "open", "mergeable": True, "draft": True,
                            "merge_commit_sha": c["execution_sha"], "updated_at": "2026-09-25T12:00:00Z",
                            "head": {"sha": c["head_sha"], "repo": {"id": c["head_repository_id"]}},
                            "base": {"sha": c["base_sha"], "ref": "main", "repo": {
                                "id": 42, "full_name": self.repository, "default_branch": "main"}}}
                if endpoint.endswith("git/ref/heads/main"):
                    return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.candidate["base_sha"]}}
                if "/commits/" in endpoint:
                    sha = endpoint.rsplit("/", 1)[1]
                    if sha in self.commits:
                        return copy.deepcopy(self.commits[sha])
                    return {"sha": self.candidate["execution_sha"],
                            "commit": {"tree": {"sha": "1" * 40},
                                       "committer": {"date": "2026-09-25T12:00:00Z"}},
                            "parents": [{"sha": self.candidate["base_sha"]},
                                        {"sha": self.candidate["head_sha"]}]}
                if "actions/runs?" in endpoint:
                    return {"total_count": 0, "workflow_runs": []}
                raise AssertionError(endpoint)
            def blob(self, sha, path):
                assert sha == self.candidate["execution_sha"]
                return self.sources[path]
            def raw(self, endpoint):
                assert endpoint == self.prefix + "actions/artifacts/902/zip"
                return self.archive
            def write(self, endpoint, payload, *, method):
                self.writes.append(copy.deepcopy(payload))
                return {"id": 17}
        return API()

    def test_approved_exact_bytes_and_ordinary_unchanged_input(self):
        api = self.fixture()
        inputs = api.record["inputs"]
        with patch.object(api, 'get', side_effect=AssertionError('Unchanged verifier needs no decision')):
            self.assertEqual(approved_inputs(api, api.candidate, inputs), (inputs, None))
        old = {k: hashlib.sha256(b'old runner').hexdigest() for k in inputs}
        accepted, identity = approved_inputs(api, api.candidate, old)
        self.assertEqual(accepted, inputs)
        self.assertEqual(identity, {"run_id": 900, "run_attempt": 1, "inputs": inputs})
        api.runs = []
        with self.assertRaises(VerifierApprovalRequired):
            approved_inputs(api, api.candidate, old)

    def test_wrong_or_stale_native_decision_cannot_grant_trust(self):

        mutations = [('candidate', key, 'd' * 40) for key in ('head_sha', 'base_sha', 'execution_sha')]
        mutations += [('run', key, value) for key, value in (
            ('head_branch', 'candidate'), ('head_sha', 'd' * 40), ('event', 'pull_request'),
            ('path', '.github/workflows/ci.yml'), ('status', 'in_progress'), ('conclusion', 'failure'),
            ('conclusion', 'cancelled'), ('run_attempt', 2), ('head_repository', {'id': 84}),
            ('actor', {'login': 'other', 'type': 'User'}),
            ('triggering_actor', {'login': 'other', 'type': 'User'}),
            ('actor', {'login': 'yonatan895', 'type': 'Bot'}), ('display_title', 'Verifier PR 3: revoke'))]
        mutations += [('job', 'conclusion', 'skipped'), ('job', 'run_attempt', 2),
                      ('artifact', 'expired', True), ('artifact', 'name', 'verifier-decision-2'),
                      ('artifact', 'digest', 'sha256:' + '0' * 64)]
        mutations += [('record', 'decision', 'revoke'), ('record', 'inputs', {}),
                      ('record', 'run_id', 800), ('record', 'run_attempt', 2),
                      ('record', 'pr_updated_at', '2026-09-25T12:00:01Z'),
                      ('record', 'pr_updated_at', '2026-09-25T12:00:02Z')]
        for target, key, value in mutations:
            with self.subTest(target=target, key=key, value=value):
                api = self.fixture()
                getattr(api, target)[key] = value
                if target == 'record':
                    api.pack()  # Correct archive digest does not prove valid content.
                with self.assertRaises(ValueError):
                    validated_decision(api, api.candidate, {k: v for k, v in self.fixture().record['inputs'].items()})

    def test_decision_survives_regenerated_merge_only_with_identical_tree_and_parents(self):
        # #753: the decision recorded merge c; GitHub regenerated the same content as f.
        def regenerated(fault=None):
            api = self.fixture()
            api.candidate["execution_sha"] = "f" * 40
            recorded = {"sha": "c" * 40, "commit": {"tree": {"sha": "1" * 40}},
                        "parents": [{"sha": "b" * 40}, {"sha": "a" * 40}]}
            if fault == "changed-tree":
                recorded["commit"]["tree"]["sha"] = "2" * 40
            elif fault == "old-base":
                recorded["parents"][0]["sha"] = "e" * 40
            elif fault == "reordered-parents":
                recorded["parents"].reverse()
            elif fault == "extra-parent":
                recorded["parents"].append({"sha": "e" * 40})
            elif fault == "invalid-tree":
                # Equal but invalid trees are not proof of identical content.
                api.commits["f" * 40] = copy.deepcopy(recorded) | {"sha": "f" * 40}
                for commit in (recorded, api.commits["f" * 40]):
                    commit["commit"]["tree"]["sha"] = "not-a-tree"
            elif fault == "unknown-commit":
                recorded["sha"] = "d" * 40
            elif fault == "malformed-sha":
                api.record["candidate"]["execution_sha"] = "../pulls/3"
                api.pack()
            elif fault == "new-head":
                api.candidate["head_sha"] = "e" * 40
            api.commits["c" * 40] = recorded
            return api

        api = regenerated()
        inputs = api.record["inputs"]
        self.assertEqual(validated_decision(api, api.candidate, inputs),
                         {"run_id": 900, "run_attempt": 1, "inputs": inputs})
        accepted, identity = approved_inputs(api, api.candidate, {k: "0" * 64 for k in inputs})
        self.assertEqual((accepted, identity["run_id"]), (inputs, 900))
        for fault in ("changed-tree", "old-base", "reordered-parents", "extra-parent", "invalid-tree",
                      "unknown-commit", "malformed-sha", "new-head"):
            with self.subTest(fault=fault):
                api = regenerated(fault)
                with self.assertRaises(ValueError):
                    validated_decision(api, api.candidate, inputs)
                with self.assertRaises(VerifierApprovalRequired):
                    approved_inputs(api, api.candidate, {k: "0" * 64 for k in inputs})

    def test_latest_revoke_or_pending_decision_blocks_old_approval_and_recheck(self):
        for status, conclusion, decision in [('completed', 'success', 'revoke'),
                                             ('in_progress', None, 'approve'),
                                             ('completed', 'cancelled', 'approve')]:
            with self.subTest(status=status, decision=decision):
                api = self.fixture()
                identity = validated_decision(api, api.candidate, api.record['inputs'])
                result = {'candidate': api.candidate, 'runs': {}, 'verifier_approval': identity}
                recheck_current(api, result)
                old = copy.deepcopy(api.run)
                api.run.update(id=910, status=status, conclusion=conclusion,
                               display_title='Verifier PR 3: ' + decision)
                api.runs = [old, api.run]
                with self.assertRaises(ValueError):
                    recheck_current(api, result)

    def test_selector_requires_exact_approval_and_catalogue_remains_excluded(self):
        for path in ('scripts/review_tooling.py', 'tests/hazards/critical.json'):
            api = self.fixture()
            api.sources = {path: b'reduced obligations'}
            with self.assertRaises(VerifierApprovalRequired):
                approved_inputs(api, api.candidate, {path: '0' * 64})
            api.record['inputs'] = {path: hashlib.sha256(api.sources[path]).hexdigest()}
            api.pack()
            if path == 'tests/hazards/critical.json':
                with self.assertRaises(VerifierApprovalRequired) as rejected:
                    approved_inputs(api, api.candidate, {path: '0' * 64})
                self.assertFalse(rejected.exception.approval_allowed)
            else:
                accepted, _ = approved_inputs(api, api.candidate, {path: '0' * 64})
                self.assertEqual(accepted, api.record['inputs'])
                for defect in ('changed bytes', 'revoked', 'new head'):
                    with self.subTest(defect=defect):
                        original = api.sources[path]
                        api.sources[path] = b'other selector' if defect == 'changed bytes' else original
                        api.record['decision'] = 'revoke' if defect == 'revoked' else 'approve'
                        api.record['candidate']['head_sha'] = 'e' * 40 if defect == 'new head' else 'a' * 40
                        api.pack()
                        with self.assertRaises(VerifierApprovalRequired):
                            approved_inputs(api, api.candidate, {path: '0' * 64})
                        api.sources[path] = original

    def test_producer_artifact_round_trip_and_no_failed_evidence_waiver(self):
        api = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            base = approved_repository(root, policy=b'approved original', producer=b'approved original',
                                       verifier_input=b'approved original')
            api.candidate['base_sha'] = api.run['head_sha'] = api.job['head_sha'] = base
            api.artifact['workflow_run']['head_sha'] = base
            api.sources = {p: (root / p).read_bytes() for p in verification_inputs(root)}
            api.sources['scripts/check_hazard_sensitivity.py'] = b'changed runner'
            api.sources['scripts/ci_evidence.py'] = b'changed producer'
            # A trusted receipt may identify these bytes, but the consumer
            # must never execute them or use them to choose its obligations.
            api.sources['scripts/review_tooling.py'] = b'raise AssertionError("candidate policy executed")'
            api.sources['tests/ci_shard.py'] = b'changed collector'
            api.sources['.github/workflows/ci.yml'] = (
                b'    name: unit (${{ matrix.shard }}/2)\n        shard: [1, 2]\n')
            # Use actual producer output, not a separately hand-built positive record.
            api.record = record_decision(api, 3, 'approve', root, 900, 1)
            api.pack()
            identity = validated_decision(api, api.candidate, api.record['inputs'])
            self.assertEqual(identity['inputs']['scripts/check_hazard_sensitivity.py'],
                             hashlib.sha256(b'changed runner').hexdigest())
            result = collect_native(api, api.get(api.prefix + 'pulls/3'), root)
            self.assertEqual(result['verifier_approval'], identity)
            self.assertEqual(result['lane_statuses'], {})  # No native evidence is not a pass.
            self.assertEqual(result['producer_sha256'], hashlib.sha256(b'changed producer').hexdigest())
            self.assertEqual(result['policy_sha256'], hashlib.sha256(b'approved original').hexdigest())
            # Approval also cannot suppress a normalizer's failed raw-test check.
            fixture, receipt, xml = TestNativeEvidenceConsumer().fixture()
            fixture['producer_digest'] = receipt['producer_sha256'] = result['producer_sha256']
            receipt['tests']['failed'] = 1
            archive, digest = TestNativeEvidenceConsumer().packed(receipt, xml)
            fixture['artifact']['digest'] = digest
            with self.assertRaises(ValueError):
                normalize_native(**fixture, archive=archive)
            # The same approval flows through the real collector and normalizer
            # for both shards; failed/skipped/cancelled native jobs still block.
            native, receipt, xml = TestNativeEvidenceConsumer().fixture()
            native['run']['head_repository']['id'] = 42
            receipt.update(base_sha=base, execution_parents=[base, 'a' * 40],
                           policy_sha256=hashlib.sha256(api.sources['scripts/review_tooling.py']).hexdigest(),
                           producer_sha256=result['producer_sha256'])
            jobs, artifacts, archives = [], [], {}
            for shard in (1, 2):
                job = {**native['job'], 'id': 456 + shard, 'name': f'unit ({shard}/2)'}


                report = copy.deepcopy(receipt)
                report['job_name'] = job['name']
                proof = report['unit_coverage']
                proof['shard'] = shard
                selected = ['tests/test_example.py::actual'] if shard == 1 else ['tests/test_example.py::other']
                for phase in ('collect', 'execute'):
                    proof[phase]['inputs'] = {path: hashlib.sha256(api.sources[path]).hexdigest() for path in INPUTS}
                proof['execute'].update(selected=selected, executed=selected)
                node = base64.b64encode(selected[0].encode()).decode()
                shard_xml = ('<testsuite><testcase name="literal"><properties>'
                             f'<property name="native_nodeid" value="{node}"/>'
                             '</properties></testcase></testsuite>').encode()
                report['tests'] = junit_bytes(shard_xml)
                packed, digest = TestNativeEvidenceConsumer().packed(report, shard_xml)
                artifact = copy.deepcopy(native['artifact'])
                artifact.update(id=789 + shard, name=f'evidence-unit-{shard}-attempt-2',
                                digest=digest, size_in_bytes=len(packed))
                artifact['workflow_run']['head_repository_id'] = 42
                jobs.append(job)
                artifacts.append(artifact)
                archives[api.prefix + f"actions/artifacts/{artifact['id']}/zip"] = packed
            original_get, original_raw = api.get, api.raw
            def with_native_jobs(endpoint):
                if endpoint.endswith('/pulls/3'):
                    return {**original_get(endpoint), 'changed_files': 1}
                if '/pulls/3/files?' in endpoint:
                    return [{'filename': 'src/mainframe_rag/retrieve/query.py', 'status': 'modified'}]
                if 'actions/runs?event=pull_request' in endpoint:
                    return {'total_count': 1, 'workflow_runs': [native['run']]}
                if endpoint.endswith('/123'):
                    return native['run']
                if '/123/attempts/2/jobs?' in endpoint:
                    return {'total_count': 2, 'jobs': jobs}
                if '/123/artifacts?' in endpoint:
                    return {'total_count': 2, 'artifacts': artifacts}
                return original_get(endpoint)
            def with_native_artifacts(endpoint):
                return archives[endpoint] if endpoint in archives else original_raw(endpoint)
            with patch.object(api, 'get', side_effect=with_native_jobs), \
                 patch.object(api, 'raw', side_effect=with_native_artifacts):
                pr = original_get(api.prefix + 'pulls/3')
                accepted = collect_native(api, pr, root)
                self.assertEqual(accepted['lane_statuses']['unit_tests'], 'success')
                summary = collect_acceptance(api, 3, root)
                required = {lane['name'] for lane in summary['lanes'] if lane['required']}
                self.assertTrue({'eval_retrieval', 'gate_l1', 'simulation', 'ha', 'load'} <= required)
                self.assertFalse(summary['all_prerequisites_met'])
                original_artifact = copy.deepcopy(artifacts[1])
                original_archive = archives[api.prefix + 'actions/artifacts/791/zip']
                for stale in (hashlib.sha256(b'approved original').hexdigest(), 'f' * 64):
                    bad = copy.deepcopy(report)
                    bad['policy_sha256'] = stale
                    packed, digest = TestNativeEvidenceConsumer().packed(bad, shard_xml)
                    artifacts[1].update(digest=digest, size_in_bytes=len(packed))
                    archives[api.prefix + 'actions/artifacts/791/zip'] = packed
                    rejected = collect_native(api, pr, root)
                    self.assertEqual(rejected['lane_statuses']['unit_tests'], 'unverified')
                artifacts[1] = original_artifact
                archives[api.prefix + 'actions/artifacts/791/zip'] = original_archive
                for conclusion in ('failure', 'skipped', 'cancelled'):
                    with self.subTest(conclusion=conclusion):
                        jobs[1]['conclusion'] = conclusion
                        rejected = collect_native(api, pr, root)
                        self.assertNotEqual(rejected['lane_statuses']['unit_tests'], 'success')
                jobs[1]['conclusion'] = 'success'
                for defect in ('missing coverage', 'different collection'):
                    with self.subTest(defect=defect):
                        bad = copy.deepcopy(report)  # second shard's otherwise valid receipt
                        if defect == 'missing coverage':
                            del bad['unit_coverage']
                        else:
                            # Each shard is locally self-consistent, but their
                            # independently collected universes must also agree.
                            for phase in ('collect', 'execute'):
                                bad['unit_coverage'][phase]['all'] = [
                                    'tests/test_example.py::different', 'tests/test_example.py::other']
                                bad['unit_coverage'][phase]['eligible'] = bad['unit_coverage'][phase]['all']
                            bad['unit_coverage']['collect']['selected'] = bad['unit_coverage']['collect']['all']
                        packed, digest = TestNativeEvidenceConsumer().packed(bad, shard_xml)
                        artifacts[1].update(digest=digest, size_in_bytes=len(packed))
                        archives[api.prefix + 'actions/artifacts/791/zip'] = packed
                        rejected = collect_native(api, pr, root)
                        self.assertEqual(rejected['lane_statuses']['unit_tests'], 'unverified')
            original_get = api.get
            def move_during_snapshot(endpoint):
                value = original_get(endpoint)
                if '/commits/' in endpoint:
                    api.candidate['head_sha'] = 'e' * 40
                return value
            with patch.object(api, 'get', side_effect=move_during_snapshot), self.assertRaises(ValueError):
                record_decision(api, 3, 'approve', root, 900, 1)

    def test_workflow_runs_only_approved_main_with_read_only_token(self):

        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/verifier-update.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertEqual(set(triggers), {'workflow_dispatch', 'issue_comment'})
        self.assertEqual(triggers['issue_comment'], {'types': ['created']})
        self.assertEqual(workflow['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        job = workflow['jobs']['record-decision']
        self.assertNotIn('permissions', job)
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        self.assertIn("github.actor == 'yonatan895'", job['if'])
        self.assertIn("github.triggering_actor == 'yonatan895'", job['if'])
        for guard in ("github.event.comment.user.login == 'yonatan895'",
                      "github.event.comment.author_association == 'OWNER'",
                      "github.event.action == 'created'", 'github.event.issue.pull_request'):
            self.assertIn(guard, job['if'])
        self.assertEqual(job['steps'][0]['with']['ref'], '${{ github.sha }}')
        self.assertFalse(job['steps'][0]['with']['persist-credentials'])
        commands = '\n'.join(step.get('run', '') for step in job['steps'])
        self.assertNotIn('${{', commands)
        self.assertNotIn('git checkout', commands)
        self.assertNotIn('secrets.', json.dumps(workflow))

    def test_comment_workflow_never_expands_comment_text_and_acknowledges_with_least_privilege(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        text = (root / '.github/workflows/verifier-update.yml').read_text()
        workflow = yaml.safe_load(text)
        # The comment body may appear only in expressions (if/run-name), never in env or a script.
        for job in workflow['jobs'].values():
            for step in job['steps']:
                self.assertNotIn('comment.body', json.dumps(step))
        self.assertNotIn('pull_request.head', text)
        self.assertNotIn('issue.title', text)
        # validated_decision accepts a run with exactly one successful record-decision
        # job; any second job (even skipped) voids every decision, dispatch included.
        self.assertEqual(list(workflow['jobs']), ['record-decision'])
        feedback_text = (root / '.github/workflows/verifier-feedback.yml').read_text()
        feedback = yaml.safe_load(feedback_text)
        self.assertEqual(feedback[True if True in feedback else 'on'],
                         {'workflow_run': {'workflows': ['Verifier update decision'], 'types': ['completed']}})
        self.assertEqual(feedback['permissions'], {})
        ack = feedback['jobs']['feedback']
        self.assertEqual(ack['permissions'], {'actions': 'read', 'issues': 'write', 'pull-requests': 'write'})
        self.assertIn("github.event.workflow_run.event == 'issue_comment'", ack['if'])
        self.assertIn("github.event.workflow_run.head_branch == 'main'", ack['if'])
        self.assertNotIn('actions/checkout', feedback_text)
        self.assertNotIn('comment.body', feedback_text)
        self.assertTrue(all('${{' not in step.get('run', '') for step in ack['steps']))
        # Third-party actions stay on the pins already used in this repository.
        self.assertTrue(all(re.search(r'@[0-9a-f]{40}$', step['uses'])
                            for step in workflow['jobs']['record-decision']['steps'] if 'uses' in step))
        self.assertEqual(workflow['name'], 'Verifier update decision')

    def comment_fixture(self, body='/verifier approve ' + 'a' * 12):

        api = self.fixture()
        api.run.update(event='issue_comment', display_title='Verifier PR 3: approve',
                       created_at='2026-09-25T12:00:05Z')
        api.record['comment'] = {'id': 77, 'created_at': '2026-09-25T12:00:04Z', 'head_sha': 'a' * 40}
        api.record['pr_updated_at'] = '2026-09-25T12:00:04Z'
        api.pack()
        api.live_comment = {'id': 77, 'body': body, 'created_at': '2026-09-25T12:00:04Z',
                            'updated_at': '2026-09-25T12:00:04Z', 'author_association': 'OWNER',
                            'user': {'login': 'yonatan895', 'type': 'User'},
                            'issue_url': 'https://api.github.com/repos/synthetic/repository/issues/3'}
        original = api.get
        def get(endpoint):
            if endpoint.endswith('issues/comments/77'):
                return copy.deepcopy(api.live_comment)
            if '/issues/comments/' in endpoint:
                raise OSError('404 Not Found')
            return original(endpoint)
        api.get = get
        return api

    def test_comment_approval_binds_the_named_head_and_the_live_comment(self):

        api = self.comment_fixture()
        inputs = api.record['inputs']
        self.assertEqual(validated_decision(api, api.candidate, inputs),
                         {'run_id': 900, 'run_attempt': 1, 'inputs': inputs})
        # Every mutation of the comment, run or record must fail closed.
        cases = [('live_comment', 'body', '/verifier approve ' + 'd' * 12),
                 ('live_comment', 'body', '/verifier approve ' + 'a' * 12 + ' catalogue-anchors'),
                 ('live_comment', 'body', '/verifier revoke'),
                 ('live_comment', 'body', '/verifier approve'),
                 ('live_comment', 'updated_at', '2026-09-25T12:09:00Z'),
                 ('live_comment', 'author_association', 'CONTRIBUTOR'),
                 ('live_comment', 'user', {'login': 'other', 'type': 'User'}),
                 ('live_comment', 'issue_url', 'https://api.github.com/repos/synthetic/repository/issues/4'),
                 ('live_comment', 'created_at', '2026-09-25T12:00:03Z'),
                 ('run', 'created_at', '2026-09-25T12:00:03Z'),
                 ('run', 'actor', {'login': 'other', 'type': 'User'}),
                 ('run', 'triggering_actor', {'login': 'other', 'type': 'User'}),
                 ('candidate', 'head_sha', 'd' * 40)]
        for target, key, value in cases:
            with self.subTest(target=target, key=key, value=value):
                api = self.comment_fixture()
                getattr(api, target)[key] = value
                with self.assertRaises((ValueError, KeyError)):
                    validated_decision(api, api.candidate, api.record['inputs'])
        for mutate in (lambda r: r.pop('comment'), lambda r: r['comment'].update(head_sha='d' * 40),
                       lambda r: r['comment'].update(id=78), lambda r: r['comment'].update(extra=1)):
            api = self.comment_fixture()
            mutate(api.record)
            api.pack()
            with self.assertRaises((ValueError, KeyError, TypeError, OSError)):
                validated_decision(api, api.candidate, api.record['inputs'])
        # A dispatch-shaped record must not carry a comment binding.
        api = self.fixture()
        api.record['comment'] = {'id': 77, 'created_at': '2026-09-25T12:00:04Z', 'head_sha': 'a' * 40}
        api.pack()
        with self.assertRaises(ValueError):
            validated_decision(api, api.candidate, api.record['inputs'])

    def test_other_users_comment_runs_cannot_shadow_or_replace_the_maintainers_decision(self):

        api = self.comment_fixture()
        api.runs = [api.run, {**api.run, 'id': 950, 'display_title': 'Verifier PR 3: revoke',
                              'actor': {'login': 'stranger', 'type': 'User'}, 'conclusion': 'skipped'}]
        self.assertEqual(latest_decision(api, api.candidate)['id'], 900)
        api.runs[1]['actor'] = {'login': 'yonatan895', 'type': 'User'}
        original_get = api.get
        api.get = lambda e: api.runs[1] if e.endswith('/950') else original_get(e)
        self.assertEqual(latest_decision(api, api.candidate)['id'], 950)

    def test_comment_grammar_is_strict(self):

        sha = 'ab12cd3'
        self.assertEqual(parse_command(f'/verifier approve {sha}\n'), ('approve', 'implementation', sha))
        self.assertEqual(parse_command(f'/verifier approve {sha} catalogue-anchors'),
                         ('approve', 'catalogue-anchors', sha))
        self.assertEqual(parse_command('/verifier revoke'), ('revoke', 'implementation', None))
        self.assertEqual(parse_command('/verifier revoke catalogue-anchors'), ('revoke', 'catalogue-anchors', None))
        for body in ('/verifier approve', '/verifier approve ab12cd', '/verifier approve AB12CD3',
                     f'/verifier approve {sha}; id', f'/verifier approve {sha}\nextra', f'x /verifier approve {sha}',
                     f'/verifier  approve {sha}', f'/verifier approve {sha} other', '/verifier revoke ' + sha,
                     '/verifier approve ' + 'a' * 41, '$(id)', '', None):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_command(body)

    def test_comment_event_authorization_and_recording(self):
        event = {'action': 'created', 'sender': {'login': 'yonatan895'}, 'issue': {'number': 3, 'pull_request': {}},
                 'comment': {'id': 77, 'body': '/verifier approve ' + 'a' * 12, 'author_association': 'OWNER',
                             'user': {'login': 'yonatan895', 'type': 'User'}}}
        api = self.comment_fixture()
        self.assertEqual(comment_request(api, event),
                         (3, 'approve', 'implementation', 'a' * 12,
                          {'id': 77, 'created_at': '2026-09-25T12:00:04Z'}))
        mutations = [lambda e: e.update(action='edited'), lambda e: e['issue'].pop('pull_request'),
                     lambda e: e['comment']['user'].update(login='other'),
                     lambda e: e['comment'].update(author_association='MEMBER'),
                     lambda e: e['comment']['user'].update(type='Bot'),
                     lambda e: e['sender'].update(login='other'),
                     lambda e: e['comment'].update(body='/verifier approve'),
                     lambda e: e['comment'].update(body='/verifier approve ' + 'b' * 12),
                     lambda e: e['issue'].update(number=4)]
        for mutate in mutations:
            changed = copy.deepcopy(event)
            mutate(changed)
            with self.assertRaises(ValueError):
                comment_request(api, changed)

    def test_comment_recording_refuses_a_head_pushed_after_the_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'f').write_text('x')
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t', 'add', 'f'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                            'commit', '-qm', 'x'], check=True)
            head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
            api = self.comment_fixture()
            api.candidate['base_sha'] = head
            with patch('scripts.acceptance.verification_inputs', return_value=['scripts/check_hazard_sensitivity.py']):
                comment = {'id': 77, 'created_at': '2026-09-25T12:00:04Z'}
                record = record_decision(api, 3, 'approve', root, 900, 1, comment=copy.deepcopy(comment),
                                         head_prefix='a' * 12)
                self.assertEqual(record['comment'], {**comment, 'head_sha': 'a' * 40})
                with self.assertRaises(ValueError):
                    record_decision(api, 3, 'approve', root, 900, 1, comment=copy.deepcopy(comment),
                                    head_prefix='b' * 12)
                with self.assertRaises(ValueError):
                    record_decision(api, 3, 'approve', root, 900, 1, comment=copy.deepcopy(comment),
                                    head_prefix=None)


    def test_publisher_explains_required_decision_without_recommending_a_policy_bypass(self):
        for path, code in [('scripts/check_hazard_sensitivity.py', 'verifier_update_requires_maintainer_decision'),
                           ('scripts/review_tooling.py', 'verifier_update_requires_maintainer_decision'),
                           ('tests/hazards/critical.json', 'verifier_policy_change_not_supported')]:
            with self.subTest(path=path):
                api = self.fixture()
                with patch('scripts.acceptance.collect_acceptance', side_effect=VerifierApprovalRequired([path])):
                    result = publish_acceptance(api, 3, pathlib.Path.cwd())
                self.assertEqual(result['error'], code)
                self.assertFalse(result['all_prerequisites_met'])
                self.assertEqual(api.writes[-1]['conclusion'], 'failure')
                self.assertIn(path, api.writes[-1]['output']['summary'])
                if path == 'tests/hazards/critical.json':
                    self.assertNotIn('/actions/workflows/verifier-update.yml', api.writes[-1]['output']['summary'])
                else:
                    self.assertIn('/actions/workflows/verifier-update.yml', api.writes[-1]['output']['summary'])


class TestCatalogueAnchorDecision(unittest.TestCase):
    """A human anchor decision preserves the complete native challenge oracle."""

    def catalogues(self):

        root = pathlib.Path(__file__).resolve().parents[1]
        original = json.loads((root / 'tests/hazards/critical.json').read_bytes())
        self.assertEqual([hazard['id'] for hazard in original['hazards']], [
            'vector-projection', 'same-id-overwrite', 'unmarked-residue', 'reader-repair-overlap',
            'scope-losing-fallback', 'missing-terminal-finish', 'final-order-miscount',
            'latest-user-mismatch', 'dropped-operator-value', 'mixed-release-identity',
        ])
        # Retain the complete current oracle, but specify both historical #718
        # anchor states independently of whether the repository adopted them.
        original_hazard = original['hazards'][6]
        self.assertEqual(original_hazard['occurrences'], 2)
        original_hazard['before'] = '            used = tokenizer.count_messages(messages)'
        original_hazard['after'] = (
            '            used = tokenizer.count_messages(messages[:-1] + '
            '[ChatMessage(role="user", content=_user_content(_assemble_blocks('
            'context_entries, question_text, packed, tail_part), "retrieval"))])'
        )
        approved = json.dumps(original).encode()
        adopted = copy.deepcopy(original)
        hazard = adopted['hazards'][6]
        # These are the original #718 changes, independently specified rather
        # than copied from an approval implementation's transformed output.
        hazard['before'] = '            used = tokenizer.count_messages(messages'
        hazard['after'] = (
            '            used = tokenizer.count_messages(messages[:-1] + '
            '[ChatMessage(role="user", content=_user_content(_assemble_blocks('
            'context_entries, question_text, packed, tail_part), "retrieval"))]'
        )
        return approved, json.dumps(adopted).encode()

    def hazard_report(self, catalogue):

        return {
            'schema_version': 1, 'candidate_sha': 'c' * 40,
            'catalogue_sha256': hashlib.sha256(catalogue).hexdigest(), 'runner_sha256': 'f' * 64,
            'complete_catalogue': True, 'passed': True,
            'results': [{
                'id': hazard['id'], 'contract': hazard['contract'], 'target': hazard['target'],
                'expected_test': hazard['test'], 'expected_assertion': hazard['assertion'],
                'replacement': {key: hazard[key] for key in ('before', 'after', 'occurrences', 'target_role')},
                'baseline': {'status': 'baseline_pass', 'exit_code': 0},
                'mutation': {'status': 'killed_by_behavior', 'exit_code': 1, 'cause': hazard['assertion']},
            } for hazard in json.loads(catalogue)['hazards']],
        }

    def fixture(self):

        approved, adopted = self.catalogues()
        api = TestVerifierUpdateDecision().fixture()
        api.sources = {'tests/hazards/critical.json': adopted}
        api.base_sources = {'tests/hazards/critical.json': approved}
        api.run['display_title'] = 'Catalogue anchors PR 3: approve'
        api.artifact['name'] = 'catalogue-anchor-decision-1'
        api.record.update(schema_version=2, kind='catalogue-anchors', catalogue={
            'base_sha256': hashlib.sha256(approved).hexdigest(),
            'candidate_sha256': hashlib.sha256(adopted).hexdigest(),
            'changed_ids': ['final-order-miscount'],
        }, inputs={'tests/hazards/critical.json': hashlib.sha256(adopted).hexdigest()})
        api.pack()
        def blob(sha, path):
            if sha == api.candidate['base_sha']:
                return api.base_sources[path]
            self.assertEqual(sha, api.candidate['execution_sha'])
            return api.sources[path]
        api.blob = blob
        identity = {'run_id': 900, 'run_attempt': 1, 'inputs': copy.deepcopy(api.record['inputs']),
                    'kind': 'catalogue-anchors', 'catalogue': copy.deepcopy(api.record['catalogue'])}
        return api, approved, adopted, identity

    def collector_fixture(self, root, *, mixed=False):
        api, approved, adopted, _ = self.fixture()
        base = approved_repository(
            root, policy=b'approved original\n', producer=b'approved original\n',
            verifier_input=b'approved original\n',
            extras={'tests/hazards/critical.json': approved,
                    '.github/workflows/ci.yml': CI_UNIT_SOURCE})
        api.candidate['base_sha'] = api.run['head_sha'] = api.job['head_sha'] = base
        api.artifact['workflow_run']['head_sha'] = base
        api.sources = {relative: (root / relative).read_bytes() for relative in verification_inputs(root)}
        api.base_sources = copy.deepcopy(api.sources)
        api.sources['tests/hazards/critical.json'] = adopted
        if mixed:
            api.sources['scripts/check_hazard_sensitivity.py'] = b'changed runner\n'
        api.record = record_decision(api, 3, 'approve', root, 900, 1, kind='catalogue-anchors')
        api.pack()
        api.implementation = None
        api.native_reads = 0
        api.revoke_on_recheck = False
        native, receipt, _ = TestNativeEvidenceConsumer().fixture()
        api.native_run = native['run']
        api.native_run['head_repository']['id'] = 42
        api.native_job = {**native['job'], 'name': 'hazards'}
        api.native_artifact = copy.deepcopy(native['artifact'])
        api.native_artifact['name'] = 'evidence-hazards-attempt-2'
        api.native_artifact['workflow_run']['head_repository_id'] = 42
        api.native_report = self.hazard_report(adopted)
        api.native_report['runner_sha256'] = hashlib.sha256(api.sources['scripts/check_hazard_sensitivity.py']).hexdigest()
        api.native_receipt = receipt
        api.native_receipt.pop('unit_coverage')
        api.native_receipt.update(base_sha=base, execution_parents=[base, 'a' * 40],
                                  policy_sha256=hashlib.sha256(api.sources['scripts/review_tooling.py']).hexdigest(),
                                  producer_sha256=hashlib.sha256(api.sources['scripts/ci_evidence.py']).hexdigest(),
                                  job_key='hazards', job_name='hazards', lane='hazards', tests=None)
        def pack_native():
            raw = json.dumps(api.native_report).encode()
            api.native_receipt['result_sha256'] = hashlib.sha256(raw).hexdigest()
            api.native_archive, digest = pack_archive(api.native_receipt, **{'results.json': raw})
            api.native_artifact.update(size_in_bytes=len(api.native_archive), digest=digest)
        api.pack_native = pack_native
        pack_native()
        original_get, original_raw = api.get, api.raw
        def get(endpoint):
            if '/pulls/3/files?' in endpoint:
                return [{'filename': 'tests/hazards/critical.json', 'status': 'modified'}]
            if endpoint.endswith('/pulls/3'):
                return {**original_get(endpoint), 'changed_files': 1}
            if 'actions/runs?event=pull_request' in endpoint:
                api.native_reads += 1
                if api.revoke_on_recheck and api.native_reads == 2:
                    old = copy.deepcopy(api.run)
                    api.run = {**api.run, 'id': 910, 'display_title': 'Catalogue anchors PR 3: revoke'}
                    api.runs = [old, api.run]
                return {'total_count': 1, 'workflow_runs': [api.native_run]}
            if endpoint.endswith('/123'):
                return api.native_run
            if '/123/attempts/2/jobs?' in endpoint:
                return {'total_count': 1, 'jobs': [api.native_job]}
            if '/123/artifacts?' in endpoint:
                return {'total_count': 1, 'artifacts': [api.native_artifact]}
            if api.implementation is not None:
                if endpoint.endswith('/920'):
                    return api.implementation.run
                if '/920/attempts/1/jobs?' in endpoint:
                    return {'total_count': 1, 'jobs': [api.implementation.job]}
                if '/920/artifacts?' in endpoint:
                    return {'total_count': 1, 'artifacts': [api.implementation.artifact]}
            return original_get(endpoint)
        def raw(endpoint):
            if endpoint == api.prefix + 'actions/artifacts/789/zip':
                return api.native_archive
            if api.implementation is not None and endpoint == api.prefix + 'actions/artifacts/922/zip':
                return api.implementation.archive
            return original_raw(endpoint)
        api.get, api.raw = get, raw
        return api

    def add_implementation_decision(self, api, root):
        implementation = TestVerifierUpdateDecision().fixture()
        implementation.candidate = copy.deepcopy(api.candidate)
        implementation.run.update(id=920, head_sha=api.candidate['base_sha'])
        implementation.job.update(id=921, run_id=920, head_sha=api.candidate['base_sha'])
        implementation.artifact.update(id=922, workflow_run={
            'id': 920, 'repository_id': 42, 'head_repository_id': 42, 'head_sha': api.candidate['base_sha']})
        implementation.record = record_decision(api, 3, 'approve', root, 920, 1)
        implementation.pack()
        api.implementation = implementation
        api.runs = [api.run, implementation.run]
        return implementation

    def test_actual_anchor_record_round_trips_through_collector_and_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            api = self.collector_fixture(root)
            self.assertEqual(api.record['schema_version'], 2)
            self.assertEqual(api.record['kind'], 'catalogue-anchors')
            self.assertEqual(api.record['catalogue']['changed_ids'], ['final-order-miscount'])
            self.assertEqual(api.record['inputs'], {
                'tests/hazards/critical.json': hashlib.sha256(api.sources['tests/hazards/critical.json']).hexdigest()})
            self.assertEqual((root / 'tests/hazards/critical.json').read_bytes(), self.catalogues()[0])
            result = collect_native(api, api.get(api.prefix + 'pulls/3'), root)
            self.assertEqual(result['lane_statuses']['hazards'], 'success')
            self.assertEqual(result['catalogue_approval']['run_id'], 900)
            self.assertIsNone(result['verifier_approval'])
            hazard = next(item for item in result['native'] if item['lane'] == 'hazards')
            self.assertEqual(len(hazard['results']['results']), 10)
            self.assertEqual(hazard['results']['results'][6]['replacement']['occurrences'], 2)
            summary = collect_acceptance(api, 3, root)
            self.assertEqual(summary['catalogue_approval'], result['catalogue_approval'])
            self.assertFalse(summary['all_prerequisites_met'])  # Other native jobs are absent.
            recheck_current(api, summary)
            api.native_report = self.hazard_report(self.catalogues()[0])
            api.native_report['runner_sha256'] = hashlib.sha256(api.sources['scripts/check_hazard_sensitivity.py']).hexdigest()
            api.pack_native()
            rejected = collect_native(api, api.get(api.prefix + 'pulls/3'), root)
            self.assertEqual(rejected['lane_statuses']['hazards'], 'unverified')

    def test_mixed_catalogue_and_implementation_changes_need_both_distinct_decisions(self):

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            api = self.collector_fixture(root, mixed=True)
            with self.assertRaises(VerifierApprovalRequired):
                collect_native(api, api.get(api.prefix + 'pulls/3'), root)
            implementation = self.add_implementation_decision(api, root)
            both = collect_native(api, api.get(api.prefix + 'pulls/3'), root)
            self.assertEqual(both['lane_statuses']['hazards'], 'success')
            self.assertEqual(both['catalogue_approval']['run_id'], 900)
            self.assertEqual(both['verifier_approval']['run_id'], 920)
            self.assertEqual(implementation.record['schema_version'], 1)
            self.assertNotIn('kind', implementation.record)
            self.assertEqual(implementation.record['inputs']['tests/hazards/critical.json'],
                             api.record['inputs']['tests/hazards/critical.json'])
            api.runs = [implementation.run]
            with self.assertRaises(VerifierApprovalRequired):
                collect_native(api, api.get(api.prefix + 'pulls/3'), root)
            api.runs = [api.run]
            with self.assertRaises(VerifierApprovalRequired):
                collect_native(api, api.get(api.prefix + 'pulls/3'), root)

    def test_catalogue_only_collection_refuses_a_complete_ordinary_decision(self):

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            api = self.collector_fixture(root)
            ordinary = self.add_implementation_decision(api, root)
            api.runs = [ordinary.run]
            with self.assertRaises(VerifierApprovalRequired):
                collect_native(api, api.get(api.prefix + 'pulls/3'), root)

    def test_actual_publisher_refuses_anchor_revoked_between_collection_and_final_write(self):

        with tempfile.TemporaryDirectory() as directory:
            api = self.collector_fixture(pathlib.Path(directory))
            api.revoke_on_recheck = True
            result = publish_acceptance(api, 3, pathlib.Path(directory))
            self.assertEqual(api.native_reads, 2)
            self.assertEqual(result['error'], 'hazard_catalogue_requires_maintainer_decision')
            self.assertFalse(result['all_prerequisites_met'])
            self.assertEqual(api.writes[-1]['conclusion'], 'failure')
            self.assertEqual(api.run['display_title'], 'Catalogue anchors PR 3: revoke')


    def test_historical_approval_and_native_oracle_survive_repository_anchor_adoption(self):
        catalogue_path = pathlib.Path(__file__).resolve().parents[1] / 'tests/hazards/critical.json'
        repository = json.loads(catalogue_path.read_bytes())
        repository['hazards'][6]['before'] = '            used = tokenizer.count_messages(messages'
        repository['hazards'][6]['after'] = (
            '            used = tokenizer.count_messages(messages[:-1] + '
            '[ChatMessage(role="user", content=_user_content(_assemble_blocks('
            'context_entries, question_text, packed, tail_part), "retrieval"))]'
        )
        adopted_repository = json.dumps(repository).encode()
        original_read = pathlib.Path.read_bytes

        def read_adopted_repository(path):
            return adopted_repository if path == catalogue_path else original_read(path)

        with patch.object(pathlib.Path, 'read_bytes', autospec=True, side_effect=read_adopted_repository):
            api, historical, candidate, identity = self.fixture()
            old_hazard = json.loads(historical)['hazards'][6]
            self.assertEqual(old_hazard['before'], '            used = tokenizer.count_messages(messages)')
            self.assertEqual(old_hazard['after'], (
                '            used = tokenizer.count_messages(messages[:-1] + '
                '[ChatMessage(role="user", content=_user_content(_assemble_blocks('
                'context_entries, question_text, packed, tail_part), "retrieval"))])'
            ))
            self.assertNotEqual(historical, adopted_repository)
            self.assertEqual(candidate, adopted_repository)
            self.assertEqual(approved_catalogue(api, api.candidate, historical), (candidate, identity))
            accepted = self.normalize_hazards(candidate, self.hazard_report(candidate))
            self.assertEqual(accepted['status'], 'success')
            self.assertEqual(len(accepted['results']['results']), 10)
            self.assertEqual(accepted['results']['results'][6]['replacement']['occurrences'], 2)
            weakened = json.loads(candidate)
            weakened['hazards'][6]['assertion'] = 'assert True'
            weakened_bytes = json.dumps(weakened).encode()
            with self.assertRaises(ValueError):
                self.normalize_hazards(weakened_bytes, self.hazard_report(weakened_bytes))

    def test_anchor_decision_refuses_structural_edits_even_when_python_values_compare_equal(self):
        approved, adopted = self.catalogues()
        document = json.loads(adopted)
        mutations = []
        for path, value in [
            (('schema_version',), True), (('schema_version',), 1.0),
            (('owner',), 'different owner'),
            (('deferred', 0, 'id'), 'different future challenge'),
            (('deferred', 0, 'owner'), '#999'), (('deferred', 0, 'reason'), 'weakened future scope'),
            (('hazards', 0, 'id'), 'different challenge'),
            (('hazards', 0, 'contract'), 'different guarantee'),
            (('hazards', 0, 'target'), 'src/unrelated.py'),
            (('hazards', 0, 'test'), 'tests/test_unrelated.py::test_pass'),
            (('hazards', 0, 'assertion'), 'assert True'),
            (('hazards', 0, 'occurrences'), True), (('hazards', 0, 'occurrences'), 1.0),
            (('hazards', 0, 'occurrences'), 2), (('hazards', 0, 'target_role'), 'weaker_role'),
        ]:
            altered = copy.deepcopy(document)
            parent = altered
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = value
            mutations.append((str(path) + '=' + repr(value), altered))
        for path in ((), ('deferred', 0), ('hazards', 0)):
            for operation in ('add', 'remove'):
                altered = copy.deepcopy(document)
                parent = altered
                for key in path:
                    parent = parent[key]
                if operation == 'add':
                    parent['extra_policy'] = 'unapproved'
                else:
                    del parent['owner' if path == () or path[0] == 'deferred' else 'target']
                mutations.append((str(path) + ':' + operation + ' field', altered))
        for operation in ('remove', 'add', 'duplicate-id', 'reorder'):
            altered = copy.deepcopy(document)
            hazards = altered['hazards']
            if operation == 'remove':
                hazards.pop()
            elif operation == 'add':
                hazards.append(copy.deepcopy(hazards[0]))
                hazards[-1]['id'] = 'unapproved-extra-challenge'
            elif operation == 'duplicate-id':
                hazards[-1]['id'] = hazards[0]['id']
            else:
                hazards[0], hazards[1] = hazards[1], hazards[0]
            mutations.append((operation + ' challenge', altered))
        for value in ([], document['deferred'] * 2):
            mutations.append(('changed deferred membership', {**document, 'deferred': value}))
        for field in ('before', 'after'):
            for value in (None, True, 1, 1.0, [], {}, ''):
                altered = copy.deepcopy(document)
                altered['hazards'][6][field] = value
                mutations.append((field + '=' + repr(value), altered))
            altered = copy.deepcopy(document)
            del altered['hazards'][6][field]
            mutations.append(('missing ' + field, altered))
        for label, altered in mutations:
            with self.subTest(case=label), self.assertRaises(ValueError):
                allowed_anchor_change(approved, json.dumps(altered).encode())
        duplicate_key = adopted.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1', 1)
        for raw in (approved, json.dumps(json.loads(approved), indent=3).encode(), duplicate_key,
                    b'{malformed', b'[]'):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                allowed_anchor_change(approved, raw)

    def test_distinct_anchor_decision_approves_exact_bytes_but_ordinary_approval_stays_excluded(self):

        api, approved, adopted, identity = self.fixture()
        self.assertEqual(approved_catalogue(api, api.candidate, approved), (adopted, identity))
        with self.assertRaises(VerifierApprovalRequired) as rejected:
            approved_inputs(api, api.candidate, {'tests/hazards/critical.json': '0' * 64})
        self.assertFalse(rejected.exception.approval_allowed)
        api.run['display_title'] = 'Verifier PR 3: approve'
        api.artifact['name'] = 'verifier-decision-1'
        api.record['schema_version'] = 1
        del api.record['kind']
        del api.record['catalogue']
        api.pack()
        with self.assertRaises(ValueError):
            approved_catalogue(api, api.candidate, approved)
        api.sources['tests/hazards/critical.json'] = approved
        api.runs = []
        self.assertEqual(approved_catalogue(api, api.candidate, approved), (approved, None))

    def test_anchor_decision_rejects_stale_malformed_or_mixed_provenance(self):

        mutations = [
            ('run', 'head_sha', 'd' * 40), ('run', 'head_branch', 'candidate'),
            ('run', 'event', 'pull_request'), ('run', 'path', '.github/workflows/ci.yml'),
            ('run', 'status', 'in_progress'), ('run', 'conclusion', 'failure'),
            ('run', 'actor', {'login': 'other', 'type': 'User'}),
            ('run', 'triggering_actor', {'login': 'yonatan895', 'type': 'Bot'}),
            ('job', 'run_attempt', 2), ('job', 'conclusion', 'skipped'),
            ('artifact', 'expired', True), ('artifact', 'name', 'verifier-decision-1'),
            ('artifact', 'digest', 'sha256:' + '0' * 64),
            ('record', 'kind', 'implementation'), ('record', 'schema_version', 1),
            ('record', 'schema_version', 2.0), ('record', 'decision', 'revoke'),
            ('record', 'inputs', {}), ('record', 'run_id', 800),
            ('record', 'pr_updated_at', '2026-09-25T12:00:01Z'),
            ('record', 'pr_updated_at', '2026-09-25T12:00:02Z'),
        ]
        mutations += [('record-candidate', field, 'd' * 40)
                      for field in ('head_sha', 'base_sha', 'execution_sha')]
        for target, key, value in mutations:
            with self.subTest(target=target, key=key, value=value):
                api, approved, _, _ = self.fixture()
                holder = api.record['candidate'] if target == 'record-candidate' else getattr(api, target)
                holder[key] = value
                if target.startswith('record'):
                    api.pack()
                with self.assertRaises(ValueError):
                    approved_catalogue(api, api.candidate, approved)
        for field, value in (('base_sha256', '0' * 64), ('candidate_sha256', '0' * 64),
                             ('changed_ids', []), ('changed_ids', ['vector-projection'])):
            with self.subTest(field=field):
                api, approved, _, _ = self.fixture()
                api.record['catalogue'][field] = value
                api.pack()
                with self.assertRaises(ValueError):
                    approved_catalogue(api, api.candidate, approved)
        api, approved, adopted, _ = self.fixture()
        api.sources['tests/hazards/critical.json'] = adopted + b'\n'
        with self.assertRaises(ValueError):
            approved_catalogue(api, api.candidate, approved)

    def test_new_anchor_revoke_pending_or_failure_after_collection_blocks_publication(self):
        for status, conclusion, decision in (
            ('completed', 'success', 'revoke'), ('in_progress', None, 'approve'),
            ('completed', 'failure', 'approve'), ('completed', 'cancelled', 'approve'),
        ):
            with self.subTest(status=status, decision=decision):
                api, approved, _, _ = self.fixture()
                _, identity = approved_catalogue(api, api.candidate, approved)
                result = {'candidate': api.candidate, 'runs': {}, 'verifier_approval': None,
                          'catalogue_approval': identity}
                recheck_current(api, result)
                old = copy.deepcopy(api.run)
                api.run.update(id=910, status=status, conclusion=conclusion,
                               display_title='Catalogue anchors PR 3: ' + decision)
                api.runs = [old, api.run]
                with self.assertRaises(ValueError):
                    recheck_current(api, result)

    def normalize_hazards(self, catalogue, report, *, include_results=True):
        args, receipt, _ = TestNativeEvidenceConsumer().fixture()
        args['producer'] = next(producer for producer in PRODUCERS if producer.lane == 'hazards')
        args['job']['name'] = 'hazards'
        args['artifact']['name'] = 'evidence-hazards-attempt-2'
        args['hazard_policy'] = {'catalogue': catalogue, 'approved_catalogue': self.catalogues()[0],
                                 'runner_sha256': 'f' * 64}
        raw = json.dumps(report).encode()
        receipt.update(job_key='hazards', job_name='hazards', lane='hazards', tests=None,
                       result_sha256=hashlib.sha256(raw).hexdigest())
        args['archive'], args['artifact']['digest'] = pack_archive(
            receipt, **({'results.json': raw} if include_results else {}))
        return normalize_native(**args)

    def test_native_receipt_binds_exact_adopted_catalogue_and_all_ten_obligations(self):

        original, adopted = self.catalogues()
        report = self.hazard_report(adopted)
        accepted = self.normalize_hazards(adopted, report)
        self.assertEqual(accepted['status'], 'success')
        self.assertEqual(accepted['results']['catalogue_sha256'], hashlib.sha256(adopted).hexdigest())
        self.assertEqual(len(accepted['results']['results']), 10)
        self.assertEqual(accepted['results']['results'][6]['replacement'], {
            'before': '            used = tokenizer.count_messages(messages',
            'after': ('            used = tokenizer.count_messages(messages[:-1] + '
                      '[ChatMessage(role="user", content=_user_content(_assemble_blocks('
                      'context_entries, question_text, packed, tail_part), "retrieval"))]'),
            'occurrences': 2, 'target_role': 'production',
        })
        mutants = [('old catalogue and otherwise passing native report', self.hazard_report(original))]
        mixed = copy.deepcopy(report)
        mixed['results'][6] = self.hazard_report(original)['results'][6]
        mutants.append(('approved hash with original anchor receipt', mixed))
        missing = copy.deepcopy(report)
        missing['results'].pop()
        mutants.append(('missing challenge', missing))
        duplicate = copy.deepcopy(report)
        duplicate['results'][-1] = copy.deepcopy(duplicate['results'][0])
        mutants.append(('duplicate challenge replaces another', duplicate))
        altered_catalogue = json.loads(adopted)
        altered_catalogue['hazards'][6]['before'] += ', different_effort=True)'
        altered = json.dumps(altered_catalogue).encode()
        self_declared = self.hazard_report(altered)
        self_declared['catalogue'] = altered_catalogue
        mutants.append(('self-consistent artifact chooses its own catalogue', copy.deepcopy(self_declared)))
        self_declared['catalogue_sha256'] = hashlib.sha256(adopted).hexdigest()
        mutants.append(('approved hash does not excuse different anchor contents', copy.deepcopy(self_declared)))
        for label, mutant in mutants:
            with self.subTest(case=label), self.assertRaises((ValueError, KeyError)):
                self.normalize_hazards(adopted, mutant)
        with self.assertRaises((ValueError, KeyError)):
            self.normalize_hazards(adopted, report, include_results=False)

    def test_native_policy_cannot_replace_approved_obligations_with_self_consistent_candidate(self):

        _, adopted = self.catalogues()
        original = json.loads(adopted)
        for defect in ('omitted hazard', 'weaker assertion', 'changed occurrences', 'bool occurrence'):
            with self.subTest(defect=defect):
                altered = copy.deepcopy(original)
                if defect == 'omitted hazard':
                    altered['hazards'].pop()
                elif defect == 'weaker assertion':
                    altered['hazards'][0]['assertion'] = 'assert True'
                elif defect == 'changed occurrences':
                    altered['hazards'][0]['occurrences'] = 2
                else:
                    altered['hazards'][0]['occurrences'] = True
                catalogue = json.dumps(altered).encode()
                report = self.hazard_report(catalogue)
                # Hash, raw results, expected assertions and replacement all
                # agree with the candidate. Approved-main remains the oracle.
                with self.assertRaises(ValueError):
                    self.normalize_hazards(catalogue, report)


class TestEvalRetrievalProducer(unittest.TestCase):
    """The eval_retrieval lane has a native CI producer (ci.yml eval-retrieval
    job running scripts/gate_l1.py). These tests pin the receipt binding and
    the collection into lane statuses."""

    def _eval_receipt(self, candidate, policy_sha256, producer_sha256, results):

        raw = json.dumps(results).encode()
        return {
            "schema_version": 1, "repository": candidate["repository"],
            "repository_id": candidate["repository_id"], "pull_request": candidate["number"],
            "head_sha": candidate["head_sha"], "base_sha": candidate["base_sha"],
            "execution_sha": candidate["execution_sha"],
            "execution_parents": [candidate["base_sha"], candidate["head_sha"]],
            "event": "pull_request", "run_id": 123, "run_attempt": 2,
            "job_key": "eval-retrieval", "job_name": "eval-retrieval", "lane": "eval_retrieval",
            "actor_id": 7, "triggering_actor": "synthetic",
            "workflow_ref": "synthetic/repository/.github/workflows/ci.yml@refs/pull/3/merge",
            "workflow_sha": candidate["execution_sha"],
            "policy_sha256": policy_sha256, "producer_sha256": producer_sha256,
            "evidence_kind": "execution", "passed": True, "exit_code": 0, "tests": None,
            "result_sha256": hashlib.sha256(raw).hexdigest(),
        }, raw

    def test_eval_retrieval_receipt_normalizes_to_success(self):
        args, _, _ = TestNativeEvidenceConsumer().fixture()
        producer = next(p for p in PRODUCERS if p.lane == "eval_retrieval")
        self.assertEqual((producer.workflow, producer.job, producer.artifact, producer.kind),
                         ("ci.yml", "eval-retrieval", "eval-retrieval", "execution"))
        self.assertTrue(producer.structured)
        args["producer"] = producer
        args["job"]["name"] = "eval-retrieval"
        args["artifact"]["name"] = "evidence-eval-retrieval-attempt-2"
        candidate = args["candidate"]
        report = {"n": 121, "scored": 108, "failures": 0, "recall@5": 1.0, "mrr": 0.995}
        receipt, raw = self._eval_receipt(candidate, args["policy_digest"], args["producer_digest"], report)
        args["archive"], args["artifact"]["digest"] = pack_archive(receipt, **{"results.json": raw})
        result = normalize_native(**args)
        self.assertEqual((result["lane"], result["status"]), ("eval_retrieval", "success"))
        self.assertEqual(result["results"]["recall@5"], 1.0)
        # A receipt mislabeled to another lane must not satisfy this producer.
        tampered = dict(receipt, lane="gate_l1")
        args["archive"], args["artifact"]["digest"] = pack_archive(tampered, **{"results.json": raw})
        with self.assertRaises(ValueError):
            normalize_native(**args)

    def test_collect_native_maps_eval_retrieval_job_to_lane_success(self):
        fixture_args, _, _ = TestNativeEvidenceConsumer().fixture()
        candidate, run, commit = fixture_args["candidate"], fixture_args["run"], fixture_args["execution_commit"]
        candidate["base_sha"], commit["parents"][0]["sha"] = None, None
        jobs = [{"id": 456 + i, "run_id": 123, "run_attempt": 2, "head_sha": "a" * 40,
                 "name": name, "status": "completed", "conclusion": "success"}
                for i, name in enumerate(("gate-l1", "eval-retrieval"))]
        lanes = {"gate-l1": "gate_l1", "eval-retrieval": "eval_retrieval"}
        results = {"gate_l1": {"verdict": "pass"}, "eval_retrieval": {"n": 121, "failures": 0}}
        archives = {}
        artifacts = []
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            base = approved_repository(root)
            candidate["base_sha"] = base
            commit["parents"][0]["sha"] = base
            policy_sha256 = hashlib.sha256(b"approved policy").hexdigest()
            producer_sha256 = hashlib.sha256(b"approved producer").hexdigest()
            aid = 789
            for job in jobs:
                lane = lanes[job["name"]]
                receipt, raw = self._eval_receipt(
                    {**candidate, "number": 3}, policy_sha256, producer_sha256, results[lane])
                receipt.update(job_key=job["name"], job_name=job["name"], lane=lane)
                blob, digest = pack_archive(receipt, **{"results.json": raw})
                archives[aid] = blob
                artifacts.append({"id": aid, "expired": False,
                                  "name": f"evidence-{job['name']}-attempt-2",
                                  "size_in_bytes": len(blob), "digest": digest,
                                  "workflow_run": {"id": 123, "repository_id": 42,
                                                   "head_repository_id": 84, "head_sha": "a" * 40}})
                aid += 1
            pr = {"number": 3, "user": {"id": 99}, "state": "open", "mergeable": True,
                  "merge_commit_sha": "c" * 40,
                  "head": {"sha": "a" * 40, "repo": {"id": 84}},
                  "base": {"sha": base, "ref": "main",
                           "repo": {"id": 42, "full_name": "synthetic/repository", "default_branch": "main"}}}
            class API:
                repository = "synthetic/repository"
                prefix = "repos/synthetic/repository/"
                def get(self, endpoint):
                    if endpoint.endswith("git/ref/heads/main"):
                        return {"ref": "refs/heads/main",
                                "object": {"type": "commit", "sha": base}}
                    if "actions/workflows/verifier-update.yml/runs?" in endpoint:
                        return {"total_count": 0, "workflow_runs": []}
                    if "actions/runs?" in endpoint:
                        return {"total_count": 1, "workflow_runs": [run]}
                    if endpoint.endswith("/jobs?per_page=100&page=1"):
                        return {"total_count": len(jobs), "jobs": jobs}
                    if endpoint.endswith("/artifacts?per_page=100&page=1"):
                        return {"total_count": len(artifacts), "artifacts": artifacts}
                    if "/commits/" in endpoint:
                        return commit
                    return run
                def raw(self, endpoint):
                    aid = int(endpoint.rstrip("/").rsplit("/", 2)[-2])
                    return archives[aid]
                def blob(self, sha, path):
                    if path == "scripts/ci_evidence.py":
                        return b"approved producer"
                    if path == "scripts/review_tooling.py":
                        return b"approved policy"
                    if path in VERIFICATION_INPUTS or path.startswith("taskfiles/"):
                        return (root / path).read_bytes()
                    return (root / path).read_bytes()
            result = collect_native(API(), pr, root)
            self.assertEqual(result["lane_statuses"].get("gate_l1"), "success")
            self.assertEqual(result["lane_statuses"].get("eval_retrieval"), "success")


class TestRerunAttemptEvidence(unittest.TestCase):
    """A failed-jobs rerun keeps earlier successes; each job binds its own attempt (#1118).

    GitHub lists every job in a rerun attempt (observed on run 40115760188):
    a job that was not rerun appears under a new id and the new attempt
    number with its original timestamps, and its evidence artifact carries the
    attempt that actually ran it.
    """

    LANES: ClassVar[dict[str, str]] = {"gate-l1": "gate_l1", "eval-retrieval": "eval_retrieval"}
    RESULTS: ClassVar[dict[str, dict]] = {"gate_l1": {"verdict": "pass"}, "eval_retrieval": {"n": 121, "failures": 0}}

    def scenario(self, root):
        base = approved_repository(root)
        candidate = {"repository": "synthetic/repository", "repository_id": 42, "number": 3,
                     "head_sha": "a" * 40, "base_sha": base, "execution_sha": "c" * 40,
                     "head_repository_id": 84}
        identity = {"event": "pull_request", "path": ".github/workflows/ci.yml", "head_sha": "a" * 40,
                    "repository": {"id": 42, "full_name": "synthetic/repository"},
                    "head_repository": {"id": 84}, "actor": {"id": 7}, "created_at": "2026-10-26T12:00:05Z"}
        runs = {1: {**identity, "id": 123, "run_attempt": 1, "status": "completed",
                    "triggering_actor": {"login": "first"}, "run_started_at": "2026-10-26T12:00:05Z"},
                2: {**identity, "id": 123, "run_attempt": 2, "status": "completed",
                    "triggering_actor": {"login": "rerunner"}, "run_started_at": "2026-10-26T13:00:00Z"}}

        def job(job_id, attempt, name, conclusion, started, completed):
            return {"id": job_id, "run_id": 123, "run_attempt": attempt, "head_sha": "a" * 40, "name": name,
                    "status": "completed", "conclusion": conclusion,
                    "started_at": started, "completed_at": completed}
        # Attempt 1: gate-l1 passed, eval-retrieval failed. Attempt 2 ("re-run
        # failed jobs"): gate-l1 is a carried copy, eval-retrieval ran again.
        jobs = {1: [job(501, 1, "gate-l1", "success", "2026-10-26T12:01:00Z", "2026-10-26T12:05:00Z"),
                    job(502, 1, "eval-retrieval", "failure", "2026-10-26T12:01:00Z", "2026-10-26T12:06:00Z")],
                2: [job(601, 2, "gate-l1", "success", "2026-10-26T12:01:00Z", "2026-10-26T12:05:00Z"),
                    job(602, 2, "eval-retrieval", "success", "2026-10-26T13:00:10Z", "2026-10-26T13:04:00Z")]}
        commit = {"sha": "c" * 40, "parents": [{"sha": base}, {"sha": "a" * 40}],
                  "commit": {"tree": {"sha": "1" * 40}, "committer": {"date": "2026-10-26T12:00:00Z"}}}
        pr = {"number": 3, "user": {"id": 99}, "state": "open", "mergeable": True, "draft": True,
              "merge_commit_sha": "c" * 40, "changed_files": 1,
              "head": {"sha": "a" * 40, "repo": {"id": 84}},
              "base": {"sha": base, "ref": "main",
                       "repo": {"id": 42, "full_name": "synthetic/repository", "default_branch": "main"}}}
        test = self

        class API:
            repository = "synthetic/repository"
            prefix = "repos/synthetic/repository/"

            def __init__(self):
                self.runs, self.jobs, self.commit, self.pr = runs, jobs, commit, pr
                self.live = base
                self.artifacts, self.archives, self.seen, self.writes = [], {}, [], []

            def evidence(self, name, attempt, **receipt_changes):
                """Upload one job's receipt under the artifact name of `attempt`."""
                lane = test.LANES[name]
                receipt, raw = TestEvalRetrievalProducer()._eval_receipt(
                    candidate, hashlib.sha256(b"approved policy").hexdigest(),
                    hashlib.sha256(b"approved producer").hexdigest(), test.RESULTS[lane])
                receipt.update(job_key=name, job_name=name, lane=lane, run_attempt=attempt,
                               triggering_actor=self.runs[attempt]["triggering_actor"]["login"])
                receipt.update(receipt_changes)
                blob, digest = pack_archive(receipt, **{"results.json": raw})
                artifact_id = 700 + len(self.artifacts)
                self.archives[artifact_id] = blob
                self.artifacts.append({"id": artifact_id, "expired": False,
                                       "name": f"evidence-{name}-attempt-{attempt}",
                                       "size_in_bytes": len(blob), "digest": digest,
                                       "workflow_run": {"id": 123, "repository_id": 42,
                                                        "head_repository_id": 84, "head_sha": "a" * 40}})

            def get(self, endpoint):
                self.seen.append(endpoint)
                latest = max(self.runs)
                if endpoint.endswith("git/ref/heads/main"):
                    return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.live}}
                if endpoint.endswith("pulls/3"):
                    return self.pr
                if "/pulls/3/files?" in endpoint:
                    return [{"filename": "src/mainframe_rag/retrieve/query.py", "status": "modified"}]
                if "actions/workflows/verifier-update.yml/runs?" in endpoint:
                    return {"total_count": 0, "workflow_runs": []}
                if "actions/runs?" in endpoint:
                    return {"total_count": 1, "workflow_runs": [self.runs[latest]]}
                for attempt in self.runs:
                    if f"actions/runs/123/attempts/{attempt}/jobs?" in endpoint:
                        return {"total_count": len(self.jobs[attempt]), "jobs": self.jobs[attempt]}
                    if endpoint.endswith(f"actions/runs/123/attempts/{attempt}"):
                        return self.runs[attempt]
                if "actions/runs/123/artifacts?" in endpoint:
                    return {"total_count": len(self.artifacts), "artifacts": self.artifacts}
                if endpoint.endswith("actions/runs/123"):
                    return self.runs[latest]
                if "/commits/" in endpoint:
                    return self.commit
                raise AssertionError("unexpected endpoint " + endpoint)

            def raw(self, endpoint):
                return self.archives[int(endpoint.rstrip("/").rsplit("/", 2)[-2])]

            def blob(self, sha, path):
                if path == "scripts/ci_evidence.py":
                    return b"approved producer"
                if path == "scripts/review_tooling.py":
                    return b"approved policy"
                return (root / path).read_bytes()

            def write(self, endpoint, payload, *, method):
                self.writes.append((endpoint, method, payload))
                return {"id": 55}

        return API(), pr

    def records(self, result):
        return {record["job"]: record for record in result["native"] if record["job"] in self.LANES}

    def test_rerun_of_one_failed_job_keeps_earlier_successes(self):
        with tempfile.TemporaryDirectory() as directory:
            api, pr = self.scenario(pathlib.Path(directory))
            api.evidence("gate-l1", 1)
            api.evidence("eval-retrieval", 2)
            result = collect_native(api, pr, pathlib.Path(directory))
            self.assertEqual((result["lane_statuses"].get("gate_l1"), result["lane_statuses"].get("eval_retrieval")),
                             ("success", "success"))
            records = self.records(result)
            # Each job is bound to the attempt that ran it: its job id, its
            # attempt and its own artifact, never the carried copy's.
            self.assertEqual((records["gate-l1"]["run_attempt"], records["gate-l1"]["job_id"],
                              records["gate-l1"]["artifact_id"]), (1, 501, 700))
            self.assertEqual((records["eval-retrieval"]["run_attempt"], records["eval-retrieval"]["job_id"],
                              records["eval-retrieval"]["artifact_id"]), (2, 602, 701))
            self.assertEqual(result["runs"]["ci.yml"], {"run_id": 123, "run_attempt": 2, "status": "completed"})
            # The recheck still pins the run's latest attempt.
            recheck_current(api, {"candidate": result["candidate"], "runs": result["runs"]})
            # While the rerun attempt is still active nothing passes yet.
            api.runs[2] = {**api.runs[2], "status": "in_progress"}
            pending = collect_native(api, pr, pathlib.Path(directory))
            self.assertEqual({r["status"] for r in self.records(pending).values()}, {"pending"})
            self.assertNotIn("gate_l1", pending["lane_statuses"])

    def test_job_whose_latest_record_failed_is_refused(self):
        for case in ("not rerun", "rerun failed again"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                api, pr = self.scenario(pathlib.Path(directory))
                api.evidence("gate-l1", 1)
                api.evidence("eval-retrieval", 2)
                if case == "not rerun":
                    # Only eval-retrieval was rerun; gate-l1 failed in attempt 1 and
                    # its carried copy still says so. A valid older receipt does not help.
                    for attempt in (1, 2):
                        api.jobs[attempt][0]["conclusion"] = "failure"
                else:
                    # gate-l1 passed in attempt 1, was rerun in attempt 2 and failed.
                    api.jobs[2][0].update(conclusion="failure", started_at="2026-10-26T13:00:10Z",
                                          completed_at="2026-10-26T13:02:00Z")
                result = collect_native(api, pr, pathlib.Path(directory))
                self.assertEqual(result["lane_statuses"].get("gate_l1"), "failure")
                self.assertEqual(result["lane_statuses"].get("eval_retrieval"), "success")

    def test_artifact_from_another_attempt_than_the_job_is_refused(self):
        cases = {
            # The rerun job's artifact is missing; an earlier attempt's is present.
            "rerun job, older artifact": (("gate-l1", 1, {}), ("eval-retrieval", 1, {})),
            # The carried job ran in attempt 1, but only an attempt-2 artifact exists.
            "carried job, newer artifact": (("gate-l1", 2, {}), ("eval-retrieval", 2, {})),
            # The artifact name matches the job's attempt, its receipt names another.
            "renamed receipt": (("gate-l1", 1, {"run_attempt": 2}), ("eval-retrieval", 2, {"run_attempt": 1})),
        }
        expected = {"rerun job, older artifact": {"gate-l1": "success", "eval-retrieval": "missing"},
                    "carried job, newer artifact": {"gate-l1": "missing", "eval-retrieval": "success"},
                    "renamed receipt": {"gate-l1": "unverified", "eval-retrieval": "unverified"}}
        for case, uploads in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                api, pr = self.scenario(pathlib.Path(directory))
                for name, attempt, changes in uploads:
                    api.evidence(name, attempt, **changes)
                result = collect_native(api, pr, pathlib.Path(directory))
                self.assertEqual({job: record["status"] for job, record in self.records(result).items()},
                                 expected[case])

    def test_attempt_from_another_run_or_commit_is_refused(self):
        def attempt_run(api, **changes):
            api.runs[1] = {**api.runs[1], **changes}

        def earlier_job(api, **changes):
            api.jobs[1][0].update(changes)

        faults = {
            "attempt names another run": lambda api: attempt_run(api, id=124),
            "attempt is another attempt": lambda api: attempt_run(api, run_attempt=3),
            "attempt for another head": lambda api: attempt_run(api, head_sha="e" * 40),
            "attempt from another workflow": lambda api: attempt_run(api, path=".github/workflows/e2e.yml"),
            "attempt from a fork": lambda api: attempt_run(api, head_repository={"id": 85}),
            "attempt still running": lambda api: attempt_run(api, status="in_progress"),
            "earlier job in another run": lambda api: earlier_job(api, run_id=124),
            "earlier job for another head": lambda api: earlier_job(api, head_sha="e" * 40),
            "copy differs from the earlier job": lambda api: earlier_job(api, completed_at="2026-10-26T12:04:00Z"),
            "copy has no earlier job": lambda api: earlier_job(api, name="renamed"),
        }
        for fault, apply in faults.items():
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                api, pr = self.scenario(pathlib.Path(directory))
                api.evidence("gate-l1", 1)
                api.evidence("eval-retrieval", 2)
                apply(api)
                result = collect_native(api, pr, pathlib.Path(directory))
                self.assertEqual(self.records(result)["gate-l1"]["status"], "unverified")
                self.assertEqual(result["lane_statuses"].get("gate_l1"), "unverified")
        receipts = {"receipt from another run": {"run_id": 124},
                    "receipt for another execution": {"execution_sha": "f" * 40},
                    "receipt for another head": {"head_sha": "e" * 40}}
        for fault, changes in receipts.items():
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                api, pr = self.scenario(pathlib.Path(directory))
                api.evidence("gate-l1", 1, **changes)
                api.evidence("eval-retrieval", 2)
                result = collect_native(api, pr, pathlib.Path(directory))
                self.assertEqual(result["lane_statuses"].get("gate_l1"), "unverified")

    def test_stale_base_names_the_base_the_live_tip_and_the_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            api, pr = self.scenario(pathlib.Path(directory))
            api.evidence("gate-l1", 1)
            api.evidence("eval-retrieval", 2)
            api.live = "d" * 40
            result = publish_acceptance(api, 3, pathlib.Path(directory))
            self.assertEqual(result["error"], "candidate_base_not_current")
            endpoint, method, payload = api.writes[-1]
            self.assertEqual((endpoint, method, payload["conclusion"]),
                             (api.prefix + "check-runs/55", "PATCH", "failure"))
            summary = payload["output"]["summary"]
            self.assertIn(f"`{pr['base']['sha']}`", summary)
            self.assertIn("`" + "d" * 40 + "`", summary)
            self.assertIn("update the branch", summary)
            self.assertNotIn("unavailable or changed", summary)
            with patch("scripts.acceptance.GitHub", return_value=api), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["--repository", "synthetic/repository", "--pr", "3",
                                       "--approved-root", directory]), 1)
            printed = json.loads(out.getvalue())
            self.assertEqual((printed["error"], printed["base_sha"], printed["live_base_sha"]),
                             ("candidate_base_not_current", pr["base"]["sha"], "d" * 40))
            # Any other refusal keeps the generic fixed message.
            api.live = pr["base"]["sha"]
            api.pr = {**pr, "mergeable": False}
            result = publish_acceptance(api, 3, pathlib.Path(directory))
            self.assertEqual(result["error"], "acceptance_evidence_unavailable_or_changed")


if __name__ == "__main__":
    unittest.main()
