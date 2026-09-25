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

import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest

from scripts.review_tooling import (
    CandidateCurrentness,
    CodeAssessment,
    LaneState,
    MergeReadiness,
    ProfileDecision,
    ProfileName,
    VerificationStatus,
    build_acceptance_summary,
    changed_paths,
    classify_paths,
    evaluate_lane,
    evaluate_probe_response,
    extract_review_json,
    generate_candidate_manifest,
    validate_review_payload,
)


class TestProfileClassification(unittest.TestCase):
    """Tests for risk-based profile selection and service mapping (§5.1)."""

    def test_classify_prose_only(self):
        paths = ["README.md", "docs/testing.md", "ROADMAP.md", "docs/agent-workflow.md"]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.OFFLINE)
        self.assertEqual(decision.services, [])
        self.assertFalse(decision.needs_qdrant)
        self.assertFalse(decision.needs_vllm)
        self.assertFalse(decision.needs_jaeger)
        self.assertFalse(decision.needs_agent)

    def test_classify_tooling_only(self):
        paths = [
            "scripts/check_agent_context.py",
            "scripts/agent_doctor.py",
            "scripts/review_tooling.py",
            "tests/test_agent_context.py",
            "tests/test_review_tooling.py",
            ".github/workflows/ci.yml",
            ".github/workflows/opencode.yml",
            "Taskfile.yml",
            "taskfiles/quality.yml",
        ]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.OFFLINE)
        self.assertEqual(decision.services, [])
        self.assertFalse(decision.needs_qdrant)
        self.assertFalse(decision.needs_vllm)
        self.assertFalse(decision.needs_jaeger)
        self.assertFalse(decision.needs_agent)

    def test_task_entry_and_modules_select_owned_verification(self):
        for path in ("Taskfile.yml", "taskfiles/quality.yml", "taskfiles/dev.yml",
                     "taskfiles/eval.yml", "taskfiles/local.yml"):
            with self.subTest(path=path):
                decision = classify_paths([path])
                self.assertEqual(decision.profile, ProfileName.OFFLINE)
                self.assertIn("tooling", decision.matched_categories)
        for path in ("taskfiles/airgap.yml", "taskfiles/artifacts.yml",
                     "scripts/tools/install-task.sh", "scripts/tools/run-task.sh",
                     "scripts/tools/task-pin.txt"):
            with self.subTest(path=path):
                decision = classify_paths([path])
                self.assertEqual(decision.profile, ProfileName.DEPLOY)
                self.assertEqual(decision.services, [])
                self.assertIn("deploy", decision.matched_categories)

    def test_classify_empty_paths_fails_closed(self):
        # An empty change set means the diff could not be read (bad SHA,
        # shallow checkout). It must not silently downgrade to a docs review.
        decision = classify_paths([])
        self.assertEqual(decision.profile, ProfileName.FULL)
        self.assertEqual(set(decision.services), {"agent", "jaeger", "qdrant", "vllm"})
        self.assertIn("unclassified_empty", decision.matched_categories)

    def test_classify_deploy_and_packaging_paths(self):
        paths = [
            "Dockerfile",
            "images/Containerfile.agent",
            "pyproject.toml",
            "requirements.lock.txt",
            "locks/cp314-linux-x86_64.json",
            "scripts/dependency_lock.py",
            "scripts/prepare_python.py",
            "scripts/image_inventory.py",
            "scripts/airgap/deploy.sh",
            "charts/mainframe-rag/templates/agent-deployment.yaml",
            "airgap.env.example",
            "tests/test_airgap_deploy_sh.py",
        ]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.DEPLOY)
        self.assertEqual(decision.services, [])
        self.assertFalse(decision.needs_qdrant)
        self.assertFalse(decision.needs_vllm)
        self.assertFalse(decision.needs_jaeger)
        self.assertFalse(decision.needs_agent)

    def test_classify_charts_and_compose_as_deploy(self):
        decision = classify_paths(["charts/mainframe-rag/values.yaml", "docker-compose.ci.yml"])
        self.assertEqual(decision.profile, ProfileName.DEPLOY)
        self.assertEqual(decision.services, [])

    def test_classify_unclassified_path_fails_closed(self):
        decision = classify_paths(["mystery.bin"])
        self.assertEqual(decision.profile, ProfileName.FULL)
        self.assertEqual(set(decision.services), {"agent", "jaeger", "qdrant", "vllm"})
        self.assertIn("unclassified", decision.matched_categories)

    def test_classify_tests_only_offline_with_tests_category(self):
        for path in ("tests/test_config.py", "tests/test_airgap_deploy_sh.py"):
            decision = classify_paths([path])
            if path.startswith("tests/test_airgap_"):
                self.assertEqual(decision.profile, ProfileName.DEPLOY)
            else:
                self.assertEqual(decision.profile, ProfileName.OFFLINE)
                self.assertIn("tests", decision.matched_categories)

    def test_shared_fixtures_select_all_dependent_boundaries(self):
        for path in ("tests/conftest.py", "tests/fakes.py", "tests/ci_shard.py"):
            with self.subTest(path=path):
                decision = classify_paths([path])
                self.assertEqual(decision.profile, ProfileName.FULL)
                self.assertTrue({"deploy", "storage", "http", "tests"} <= set(decision.matched_categories))

    def test_instructions_and_executable_docs_are_not_prose_only(self):
        for path in ("AGENTS.md", "src/AGENTS.override.md", ".agents/skills/example/SKILL.md", "docs/testing.md"):
            with self.subTest(path=path):
                decision = classify_paths([path])
                self.assertIn("tooling", decision.matched_categories)
                self.assertEqual(decision.services, [])
        for path in ("docs/generated.json", "docs/example.sh"):
            self.assertEqual(classify_paths([path]).profile, ProfileName.FULL)
        self.assertEqual(classify_paths(["docs/explanation.md"]).matched_categories, ["prose"])

    def test_narrow_deployment_helpers_keep_their_owner(self):
        for path in ("tests/helpers_helm.py", "tests/helpers_image_inventory.py", "tests/helpers_task_artifact.py"):
            self.assertEqual(classify_paths([path]).profile, ProfileName.DEPLOY)
        self.assertEqual(classify_paths(["src/mainframe_rag/retrieve/query.py"]).profile, ProfileName.STORAGE)

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

    def test_classify_scripts_benchmarks_and_ci_mirror_as_tooling(self):
        for path in ("scripts/gate_l1.py", "benchmarks/harness.json", ".gitlab-ci.yml",
                     "vendor/qdrant-skills.sha"):
            decision = classify_paths([path])
            self.assertEqual(decision.profile, ProfileName.OFFLINE)
            self.assertIn("tooling", decision.matched_categories)

    def test_markdown_never_selects_live_services(self):
        # The prose resource boundary is strictly offline even when the file
        # lives under a service-owned tree.
        decision = classify_paths(["evals/README.md"])
        self.assertEqual(decision.profile, ProfileName.OFFLINE)
        self.assertEqual(decision.services, [])
        self.assertEqual(decision.matched_categories, ["prose"])

        ingest_doc = classify_paths(["src/mainframe_rag/ingest/README.md"])
        self.assertEqual(ingest_doc.profile, ProfileName.OFFLINE)
        self.assertEqual(ingest_doc.services, [])

    def test_markdown_under_tests_keeps_test_lane(self):
        decision = classify_paths(["tests/fixtures/notes.md"])
        self.assertEqual(decision.profile, ProfileName.OFFLINE)
        self.assertEqual(decision.services, [])
        self.assertIn("tests", decision.matched_categories)

    def test_classify_deploy_plus_code_takes_union_full(self):
        decision = classify_paths(["pyproject.toml", "src/mainframe_rag/ingest/publish.py"])
        self.assertEqual(decision.profile, ProfileName.FULL)
        self.assertEqual(decision.services, ["qdrant"])

    def test_classify_mixed_docs_and_deploy_stays_deploy(self):
        decision = classify_paths(["docs/testing.md", "Dockerfile"])
        self.assertEqual(decision.profile, ProfileName.DEPLOY)
        self.assertEqual(decision.services, [])

    def test_classify_storage(self):
        paths = [
            "src/mainframe_rag/ingest/publish.py",
            "src/mainframe_rag/ingest/completion.py",
            "scripts/qdrant_pin.py",
            "images.txt",
        ]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.STORAGE)
        self.assertEqual(decision.services, ["qdrant"])
        self.assertTrue(decision.needs_qdrant)
        self.assertFalse(decision.needs_vllm)
        self.assertFalse(decision.needs_jaeger)
        self.assertFalse(decision.needs_agent)

    def test_classify_http(self):
        paths = [
            "src/mainframe_rag/agent/app.py",
            "src/mainframe_rag/agent/api.py",
            "scripts/mock_vllm.py",
        ]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.HTTP)
        self.assertEqual(set(decision.services), {"agent", "qdrant", "vllm"})
        self.assertTrue(decision.needs_qdrant)
        self.assertTrue(decision.needs_vllm)
        self.assertTrue(decision.needs_agent)
        self.assertFalse(decision.needs_jaeger)

    def test_classify_tracing(self):
        paths = [
            "src/mainframe_rag/tracing.py",
            "scripts/run_local_jaeger.sh",
        ]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.TRACING)
        self.assertEqual(decision.services, ["jaeger"])
        self.assertFalse(decision.needs_qdrant)
        self.assertFalse(decision.needs_vllm)
        self.assertFalse(decision.needs_agent)
        self.assertTrue(decision.needs_jaeger)

    def test_classify_cross_layer_union(self):
        # Storage + HTTP
        paths = [
            "src/mainframe_rag/ingest/publish.py",
            "src/mainframe_rag/agent/app.py",
        ]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.FULL)
        self.assertEqual(set(decision.services), {"agent", "qdrant", "vllm"})
        self.assertTrue(decision.needs_qdrant)
        self.assertTrue(decision.needs_vllm)
        self.assertTrue(decision.needs_agent)
        self.assertFalse(decision.needs_jaeger)

    def test_classify_full_stack(self):
        # Storage + HTTP + Tracing
        paths = [
            "src/mainframe_rag/ingest/publish.py",
            "src/mainframe_rag/agent/app.py",
            "src/mainframe_rag/tracing.py",
        ]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.FULL)
        self.assertEqual(set(decision.services), {"agent", "jaeger", "qdrant", "vllm"})
        self.assertTrue(decision.needs_qdrant)
        self.assertTrue(decision.needs_vllm)
        self.assertTrue(decision.needs_agent)
        self.assertTrue(decision.needs_jaeger)

    def test_classify_unclassified_src_defaults_to_full(self):
        paths = ["src/mainframe_rag/__init__.py", "src/mainframe_rag/new_module.py"]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.FULL)
        self.assertEqual(set(decision.services), {"agent", "jaeger", "qdrant", "vllm"})

    def test_classify_mixed_docs_and_code_prioritizes_code(self):
        paths = [
            "docs/testing.md",
            "src/mainframe_rag/ingest/publish.py",
        ]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.STORAGE)
        self.assertEqual(decision.services, ["qdrant"])


class TestCandidateRuntimeManifest(unittest.TestCase):
    """Tests for candidate runtime manifest generation and attestation (§5.1)."""

    def test_manifest_structure_and_schema(self):
        decision = ProfileDecision(ProfileName.OFFLINE, [], ["prose"])
        manifest = generate_candidate_manifest(
            decision,
            head_sha="a" * 40,
            base_sha="b" * 40,
            execution_sha="c" * 40,
            dirty=False,
            repo_root=pathlib.Path.cwd(),
        )

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["head_sha"], "a" * 40)
        self.assertEqual(manifest["base_sha"], "b" * 40)
        self.assertEqual(manifest["execution_sha"], "c" * 40)
        self.assertFalse(manifest["dirty"])
        self.assertFalse(manifest["dirty_tree"])
        self.assertEqual(manifest["profile"], "offline")
        self.assertEqual(manifest["services"], [])
        self.assertEqual(manifest["test_focus"], "tests/test_agent_context.py")

        # Interpreter attestation
        interp = manifest["interpreter"]
        self.assertEqual(interp["implementation"], "CPython")
        self.assertIn("version", interp)
        self.assertIsInstance(interp["free_threaded"], bool)
        self.assertIsInstance(interp["jit"], bool)

        # Dependencies attestation
        deps = manifest["dependencies"]
        self.assertIn("requirements_hash", deps)
        self.assertEqual(len(deps["requirements_hash"]), 64)

    def test_test_focus_profile_resolution(self):
        d_prose = ProfileDecision(ProfileName.OFFLINE, [], ["prose"])
        self.assertEqual(d_prose.test_focus, "tests/test_agent_context.py")

        d_tooling = ProfileDecision(ProfileName.OFFLINE, [], ["tooling"])
        self.assertEqual(d_tooling.test_focus, "tests/test_agent_context.py tests/test_review_tooling.py")

        d_deploy = ProfileDecision(ProfileName.DEPLOY, [], ["packaging"])
        self.assertEqual(d_deploy.test_focus, "tests/test_airgap_*.py tests/test_config.py")

        d_http = ProfileDecision(ProfileName.HTTP, ["agent", "jaeger"], ["agent_code"])
        self.assertEqual(d_http.test_focus, "tests/test_agent_api.py tests/test_stream_truncation.py")

        d_storage = ProfileDecision(ProfileName.STORAGE, ["qdrant"], ["storage"])
        self.assertEqual(d_storage.test_focus, "tests/test_ingest_*.py tests/test_publish_*.py")

        d_tracing = ProfileDecision(ProfileName.TRACING, ["jaeger"], ["tracing"])
        self.assertEqual(d_tracing.test_focus, "tests/test_tracing*.py")

        d_full = ProfileDecision(ProfileName.FULL, ["agent", "jaeger", "qdrant", "vllm"], ["unclassified"])
        self.assertEqual(d_full.test_focus, "tests/test_agent_api.py tests/test_ingest_publish.py")

    def test_manifest_dirty_tree_flag(self):
        decision = ProfileDecision(ProfileName.STORAGE, ["qdrant"], ["storage"])
        manifest = generate_candidate_manifest(
            decision,
            dirty=True,
        )
        self.assertTrue(manifest["dirty"])
        self.assertTrue(manifest["dirty_tree"])


class TestReviewResultValidator(unittest.TestCase):
    """Tests for Schema v1 review result validation, fail-closed handling, and attribution checks (§5.3)."""

    def _sample_valid_payload(self) -> dict:
        return {
            "schema_version": 1,
            "head_sha": "1" * 40,
            "base_sha": "2" * 40,
            "execution_sha": "3" * 40,
            "code_assessment": "acceptable",
            "verification": "complete",
            "candidate_currentness": "current",
            "merge_readiness": "ready_for_maintainer",
            "material_findings": [
                {
                    "id": "FINDING-1",
                    "disposition": "fixed-and-verified",
                    "description": "Clean up temporary file descriptor",
                }
            ],
            "evidence": {"notes": "All unit tests and doctor checks pass."},
        }

    def test_valid_schema_v1_passes(self):
        payload = self._sample_valid_payload()
        result = validate_review_payload(
            payload,
            expected_head="1" * 40,
            expected_base="2" * 40,
            expected_execution="3" * 40,
        )
        self.assertEqual(result.merge_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)
        self.assertEqual(result.code_assessment, CodeAssessment.ACCEPTABLE.value)
        self.assertEqual(result.verification, VerificationStatus.COMPLETE.value)
        self.assertEqual(result.candidate_currentness, CandidateCurrentness.CURRENT.value)
        self.assertEqual(len(result.validation_errors), 0)

    def test_missing_payload_fails_closed(self):
        result = validate_review_payload(None)
        self.assertEqual(result.code_assessment, CodeAssessment.INCOMPLETE.value)
        self.assertEqual(result.verification, VerificationStatus.INCOMPLETE.value)
        self.assertEqual(result.candidate_currentness, CandidateCurrentness.UNVERIFIED.value)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
        self.assertTrue(len(result.validation_errors) > 0)

    def test_malformed_json_extraction_fails_closed(self):
        content = "Not valid JSON: { broken "
        parsed = extract_review_json(content)
        self.assertIsNone(parsed)
        result = validate_review_payload(parsed, parse_error="Malformed JSON syntax")
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
        self.assertIn("Malformed JSON syntax", result.validation_errors)

    def test_markdown_embedded_json_comment_extraction(self):
        payload = self._sample_valid_payload()
        md_text = f"""
# Review Summary

The changes look great.

<!-- review-result:start -->
{json.dumps(payload, indent=2)}
<!-- review-result:end -->

End of review.
"""
        extracted = extract_review_json(md_text)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted["head_sha"], "1" * 40)

        result = validate_review_payload(extracted)
        self.assertEqual(result.merge_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)

    def test_markdown_code_fence_extraction(self):
        payload = self._sample_valid_payload()
        md_text = f"""
Here is the review record:

```json
{json.dumps(payload, indent=2)}
```
"""
        extracted = extract_review_json(md_text)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted["schema_version"], 1)

    def test_missing_required_fields_fails_validation(self):
        # Missing material_findings and head_sha
        payload = {
            "schema_version": 1,
            "base_sha": "2" * 40,
            "execution_sha": "3" * 40,
            "code_assessment": "acceptable",
            "verification": "complete",
            "candidate_currentness": "current",
            "merge_readiness": "ready_for_maintainer",
        }
        result = validate_review_payload(payload)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
        self.assertTrue(any("head_sha" in err for err in result.validation_errors))
        self.assertTrue(any("material_findings" in err for err in result.validation_errors))

    def test_invalid_enum_values_normalize_to_incomplete_and_not_ready(self):
        payload = self._sample_valid_payload()
        payload["code_assessment"] = "looks_good"
        payload["verification"] = "passed"
        result = validate_review_payload(payload)
        self.assertEqual(result.code_assessment, CodeAssessment.INCOMPLETE.value)
        self.assertEqual(result.verification, VerificationStatus.INCOMPLETE.value)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)

    def test_unresolved_material_findings_block_readiness(self):
        payload = self._sample_valid_payload()
        payload["material_findings"].append({
            "id": "FINDING-2",
            "disposition": "unresolved",
            "description": "Unhandled connection pool exhaustion",
        })
        result = validate_review_payload(payload)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)

    def test_author_declined_finding_blocks_readiness(self):
        # Implementer declining is NOT acceptance per AGENTS.md / agent-workflow.md
        payload = self._sample_valid_payload()
        payload["material_findings"].append({
            "id": "FINDING-2",
            "disposition": "declined-by-author",
            "description": "Author declined to fix bug",
        })
        result = validate_review_payload(payload)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)

    def test_resolved_findings_permit_readiness(self):
        payload = self._sample_valid_payload()
        payload["material_findings"] = [
            {"id": "F1", "disposition": "fixed-and-verified"},
            {"id": "F2", "disposition": "disproven-with-evidence"},
            {"id": "F3", "disposition": "accepted-by-authorized-owner"},
        ]
        result = validate_review_payload(payload)
        self.assertEqual(result.merge_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)

    def test_empty_findings_list_permits_readiness(self):
        # "no findings is a valid outcome"
        payload = self._sample_valid_payload()
        payload["material_findings"] = []
        result = validate_review_payload(payload)
        self.assertEqual(result.merge_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)

    def test_obsolete_head_sha_evaluates_to_stale(self):
        payload = self._sample_valid_payload()
        manifest = {"head_sha": "9" * 40, "base_sha": "2" * 40, "execution_sha": "3" * 40}
        result = validate_review_payload(payload, manifest=manifest)
        self.assertEqual(result.candidate_currentness, CandidateCurrentness.STALE.value)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)

    def test_manifest_dirty_tree_evaluates_to_unverified(self):
        payload = self._sample_valid_payload()
        manifest = {
            "head_sha": "1" * 40,
            "base_sha": "2" * 40,
            "execution_sha": "3" * 40,
            "dirty": True,
        }
        result = validate_review_payload(payload, manifest=manifest)
        self.assertEqual(result.candidate_currentness, CandidateCurrentness.UNVERIFIED.value)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)

    def test_anti_forging_override(self):
        # Claiming ready_for_maintainer while code assessment is changes_required
        payload = self._sample_valid_payload()
        payload["code_assessment"] = "changes_required"
        payload["merge_readiness"] = "ready_for_maintainer"
        result = validate_review_payload(payload)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
        self.assertTrue(any("Overriding" in err for err in result.validation_errors))


class TestCandidateAcceptanceSummary(unittest.TestCase):
    """Tests for 5-state lane taxonomy, anti-skip enforcement, and maintainer merge authority (§5.4)."""

    def _sample_manifest(self, profile: str = "offline", dirty: bool = False) -> dict:
        return {
            "schema_version": 1,
            "head_sha": "1" * 40,
            "base_sha": "2" * 40,
            "execution_sha": "3" * 40,
            "profile": profile,
            "dirty": dirty,
            "matched_categories": ["prose"] if profile == "offline" else [profile],
        }

    def _ready_review(self):
        payload = {
            "schema_version": 1,
            "head_sha": "1" * 40,
            "base_sha": "2" * 40,
            "execution_sha": "3" * 40,
            "code_assessment": "acceptable",
            "verification": "complete",
            "candidate_currentness": "current",
            "merge_readiness": "ready_for_maintainer",
            "material_findings": [],
            "evidence": {"notes": "ok"},
        }
        return validate_review_payload(
            payload,
            expected_head="1" * 40,
            expected_base="2" * 40,
            expected_execution="3" * 40,
        )

    def _not_ready_review(self):
        payload = {
            "schema_version": 1,
            "head_sha": "1" * 40,
            "base_sha": "2" * 40,
            "execution_sha": "3" * 40,
            "code_assessment": "changes_required",
            "verification": "incomplete",
            "candidate_currentness": "current",
            "merge_readiness": "not_ready",
            "material_findings": [{"id": "F1", "disposition": "unresolved", "description": "open"}],
            "evidence": {"notes": "not ready"},
        }
        return validate_review_payload(
            payload,
            expected_head="1" * 40,
            expected_base="2" * 40,
            expected_execution="3" * 40,
        )

    def test_lane_evaluation_states(self):
        # 1. Unselected lane
        l_unsel = evaluate_lane("simulation", required=False, reported_status=None)
        self.assertEqual(l_unsel.state, LaneState.UNSELECTED)
        self.assertFalse(l_unsel.blocks_readiness)

        # 2. Selected passed
        l_pass = evaluate_lane("context_check", required=True, reported_status="success")
        self.assertEqual(l_pass.state, LaneState.SELECTED_PASSED)
        self.assertFalse(l_pass.blocks_readiness)

        # 3. Selected failed
        l_fail = evaluate_lane("context_check", required=True, reported_status="failure")
        self.assertEqual(l_fail.state, LaneState.SELECTED_FAILED)
        self.assertTrue(l_fail.blocks_readiness)

        # 4. Selected skipped (anti-skip violation)
        l_skip = evaluate_lane("simulation", required=True, reported_status="skipped")
        self.assertEqual(l_skip.state, LaneState.SELECTED_SKIPPED)
        self.assertTrue(l_skip.blocks_readiness)

        # 5. Selected missing
        l_missing = evaluate_lane("reviewer", required=True, reported_status=None)
        self.assertEqual(l_missing.state, LaneState.SELECTED_MISSING)
        self.assertTrue(l_missing.blocks_readiness)

    def test_unselected_lane_skipped_does_not_block(self):
        # In offline profile (prose-only), simulation is unselected.
        manifest = self._sample_manifest(profile="offline")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "reviewer": "success",
            "simulation": "skipped",  # unselected lane was skipped
            "gate_l1": "skipped",     # unselected lane was skipped
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertTrue(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)

    def test_selected_lane_skipped_blocks_acceptance(self):
        # In storage profile, simulation is required. If skipped upstream, it MUST block.
        manifest = self._sample_manifest(profile="storage")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "unit_tests": "success",
            "simulation": "skipped",  # anti-skip violation!
            "gate_l1": "success",
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertFalse(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.NOT_READY.value)
        sim_lane = next(l for l in summary.lanes if l.name == "simulation")
        self.assertEqual(sim_lane.state, LaneState.SELECTED_SKIPPED)

    def test_selected_lane_missing_blocks_acceptance(self):
        manifest = self._sample_manifest(profile="storage")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "unit_tests": "success",
            "simulation": "success",
            # gate_l1 is omitted entirely
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertFalse(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.NOT_READY.value)
        gate_lane = next(l for l in summary.lanes if l.name == "gate_l1")
        self.assertEqual(gate_lane.state, LaneState.SELECTED_MISSING)

    def test_selected_lane_failed_blocks_acceptance(self):
        manifest = self._sample_manifest(profile="offline")
        lane_statuses = {
            "context_check": "failure",
            "lint_and_types": "success",
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertFalse(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.NOT_READY.value)

    def test_deploy_profile_requires_packaging_lane(self):
        manifest = self._sample_manifest(profile="deploy")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "unit_tests": "success",
            # packaging omitted: the airgap dry-run lane never ran
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertFalse(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.NOT_READY.value)
        packaging = next(l for l in summary.lanes if l.name == "packaging")
        self.assertTrue(packaging.required)
        self.assertEqual(packaging.state, LaneState.SELECTED_MISSING)

    def test_deploy_profile_packaging_skipped_blocks(self):
        manifest = self._sample_manifest(profile="deploy")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "unit_tests": "success",
            "packaging": "skipped",
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertFalse(summary.all_prerequisites_met)
        packaging = next(l for l in summary.lanes if l.name == "packaging")
        self.assertEqual(packaging.state, LaneState.SELECTED_SKIPPED)

    def test_offline_tooling_requires_lint_and_unit_lanes(self):
        manifest = self._sample_manifest(profile="offline")
        manifest["matched_categories"] = ["tooling"]
        lane_statuses = {"context_check": "success", "reviewer": "success"}
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertFalse(summary.all_prerequisites_met)
        lint = next(l for l in summary.lanes if l.name == "lint_and_types")
        unit = next(l for l in summary.lanes if l.name == "unit_tests")
        self.assertEqual(lint.state, LaneState.SELECTED_MISSING)
        self.assertEqual(unit.state, LaneState.SELECTED_MISSING)

    def test_offline_prose_does_not_require_path_filtered_lanes(self):
        # ci.yml ignores markdown-only PRs, so lint/unit lanes have no producer
        # for prose; requiring them would make docs-only acceptance unreachable.
        manifest = self._sample_manifest(profile="offline")
        lane_statuses = {"context_check": "success", "reviewer": "success"}
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertTrue(summary.all_prerequisites_met)
        lint = next(l for l in summary.lanes if l.name == "lint_and_types")
        unit = next(l for l in summary.lanes if l.name == "unit_tests")
        self.assertEqual(lint.state, LaneState.UNSELECTED)
        self.assertEqual(unit.state, LaneState.UNSELECTED)

    def test_required_agent_probes_are_not_waived_by_a_missing_ci_producer(self):
        # The HTTP contract requires actual agent probes even when no native
        # producer reports them. An authorized human must supply the evidence.
        manifest = self._sample_manifest(profile="http")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "unit_tests": "success",
            "hazards": "success",
            "load": "success",
            "simulation": "success",
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertFalse(summary.all_prerequisites_met)
        lane = next(l for l in summary.lanes if l.name == "agent_probes")
        self.assertTrue(lane.required)
        self.assertEqual(lane.state, LaneState.SELECTED_MISSING)
        evaluation = next(l for l in summary.lanes if l.name == "eval_retrieval")
        self.assertFalse(evaluation.required)

    def test_all_selected_lanes_passed_produces_ready(self):
        manifest = self._sample_manifest(profile="http")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "unit_tests": "success",
            "hazards": "success",
            "load": "success",
            "simulation": "success",
            "agent_probes": "success",
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertTrue(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)

    def test_maintainer_authority_preservation_in_markdown(self):
        manifest = self._sample_manifest(profile="offline")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        md = summary.markdown_report

        self.assertIn("### Maintainer Merge Authority", md)
        self.assertIn("Prerequisite Obligations**: `ALL_MET`", md)
        self.assertIn("Recommended Readiness**: `ready_for_maintainer`", md)
        self.assertIn("Maintainer Merge Decision**: `pending`", md)
        self.assertIn("Agents never merge pull requests or modify repository access rules", md)


class TestReviewToolingCLI(unittest.TestCase):
    """Tests for CLI invocations and end-to-end command exits."""

    def test_cli_profile_offline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = pathlib.Path(tmpdir) / "manifest.json"
            gh_out_file = pathlib.Path(tmpdir) / "github_output.txt"
            gh_out_file.touch()

            cmd = [
                sys.executable,
                "scripts/review_tooling.py",
                "profile",
                "--files",
                "docs/explanation.md",
                "README.md",
                "--out",
                str(out_file),
                "--github-output",
                str(gh_out_file),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout.strip(), "offline")
            self.assertTrue(out_file.exists())

            manifest = json.loads(out_file.read_text())
            self.assertEqual(manifest["profile"], "offline")
            self.assertEqual(manifest["services"], [])
            self.assertEqual(manifest["test_focus"], "tests/test_agent_context.py")

            gh_text = gh_out_file.read_text()
            self.assertIn("profile=offline", gh_text)
            self.assertIn("needs_qdrant=false", gh_text)
            self.assertIn("test_focus=tests/test_agent_context.py", gh_text)

    def test_cli_validate_review_check_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            valid_file = pathlib.Path(tmpdir) / "valid_review.json"
            valid_payload = {
                "schema_version": 1,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "execution_sha": "c" * 40,
                "code_assessment": "acceptable",
                "verification": "complete",
                "candidate_currentness": "current",
                "merge_readiness": "ready_for_maintainer",
                "material_findings": [],
                "evidence": {},
            }
            valid_file.write_text(json.dumps(valid_payload))

            # Valid review with --check exits 0
            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "validate-review",
                    "--review",
                    str(valid_file),
                    "--check",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0)

            # Incomplete review with --check exits 1
            invalid_file = pathlib.Path(tmpdir) / "invalid_review.json"
            invalid_payload = dict(valid_payload)
            invalid_payload["code_assessment"] = "changes_required"
            invalid_file.write_text(json.dumps(invalid_payload))

            res_inv = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "validate-review",
                    "--review",
                    str(invalid_file),
                    "--check",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_inv.returncode, 1)

    def test_cli_validate_review_require_payload_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_payload = {
                "schema_version": 1,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "execution_sha": "c" * 40,
                "code_assessment": "acceptable",
                "verification": "complete",
                "candidate_currentness": "current",
                "merge_readiness": "ready_for_maintainer",
                "material_findings": [],
                "evidence": {},
            }

            # A valid changes_required verdict is a successful review execution
            # (structural validation passes even though the candidate is not ready).
            findings_file = pathlib.Path(tmpdir) / "findings_review.json"
            findings_payload = dict(base_payload)
            findings_payload["code_assessment"] = "changes_required"
            findings_payload["verification"] = "incomplete"
            findings_payload["merge_readiness"] = "not_ready"
            findings_payload["material_findings"] = [
                {"id": "F1", "disposition": "unresolved", "description": "Open finding"}
            ]
            findings_file.write_text(json.dumps(findings_payload))
            res_findings = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "validate-review",
                    "--review",
                    str(findings_file),
                    "--require-payload",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_findings.returncode, 0)

            # Missing payload file fails the review execution.
            missing = pathlib.Path(tmpdir) / "absent.json"
            res_missing = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "validate-review",
                    "--review",
                    str(missing),
                    "--require-payload",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_missing.returncode, 2)

            # Unparseable payload fails the review execution.
            malformed_file = pathlib.Path(tmpdir) / "malformed.md"
            malformed_file.write_text("Review prose with no machine-readable block.")
            res_malformed = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "validate-review",
                    "--review",
                    str(malformed_file),
                    "--require-payload",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_malformed.returncode, 2)

            # Forged readiness (override applied) fails structural validation.
            forged_file = pathlib.Path(tmpdir) / "forged.json"
            forged_payload = dict(base_payload)
            forged_payload["code_assessment"] = "changes_required"
            forged_file.write_text(json.dumps(forged_payload))
            res_forged = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "validate-review",
                    "--review",
                    str(forged_file),
                    "--require-payload",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_forged.returncode, 2)

    def test_cli_summarize_acceptance_check_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_file = pathlib.Path(tmpdir) / "manifest.json"
            manifest = {
                "schema_version": 1,
                "head_sha": "1" * 40,
                "base_sha": "2" * 40,
                "execution_sha": "3" * 40,
                "profile": "offline",
                "dirty": False,
                "matched_categories": ["prose"],
            }
            manifest_file.write_text(json.dumps(manifest))
            review_file = pathlib.Path(tmpdir) / "review.json"
            review_file.write_text(json.dumps({
                "schema_version": 1,
                "head_sha": "1" * 40,
                "base_sha": "2" * 40,
                "execution_sha": "3" * 40,
                "code_assessment": "acceptable",
                "verification": "complete",
                "candidate_currentness": "current",
                "merge_readiness": "ready_for_maintainer",
                "material_findings": [],
                "evidence": {"notes": "ok"},
            }))

            # All obligations met exits 0 (validated review result required;
            # reviewer lane status alone never suffices).
            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "summarize-acceptance",
                    "--manifest",
                    str(manifest_file),
                    "--review",
                    str(review_file),
                    "--lane",
                    "context_check:success",
                    "--lane",
                    "lint_and_types:success",
                    "--lane",
                    "reviewer:success",
                    "--check",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0)

            # Missing obligation exits 1 (reviewer lane absent)
            res_fail = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "summarize-acceptance",
                    "--manifest",
                    str(manifest_file),
                    "--lane",
                    "context_check:success",
                    # reviewer omitted
                    "--check",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_fail.returncode, 1)


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
        return {
            "schema_version": 1,
            "head_sha": head,
            "base_sha": base,
            "execution_sha": execution,
            "code_assessment": "acceptable",
            "verification": "complete",
            "candidate_currentness": "current",
            "merge_readiness": "ready_for_maintainer",
            "material_findings": [],
            "evidence": {"notes": "ok"},
        }

    def test_wrong_clean_checkout_is_unverified(self):
        tmp, repo, base, head = self._make_two_commit_repo()
        try:
            # Claim the newer candidate/execution while checked out at base.
            subprocess.run(["git", "checkout", "-q", base], cwd=repo, check=True)
            payload = self._payload(head, base, head)
            result = validate_review_payload(payload, check_git=True, cwd=repo)
            self.assertEqual(result.candidate_currentness, CandidateCurrentness.UNVERIFIED.value)
            self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
            self.assertTrue(any("matches neither" in e and "HEAD" in e for e in result.validation_errors))
        finally:
            tmp.cleanup()

    def test_wrong_checkout_cli_marks_not_ready(self):
        tmp, repo, base, head = self._make_two_commit_repo()
        try:
            subprocess.run(["git", "checkout", "-q", base], cwd=repo, check=True)
            review_file = repo / "review.json"
            review_file.write_text(json.dumps(self._payload(head, base, head)))
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

    def test_correct_checkout_passes_attribution(self):
        tmp, repo, base, head = self._make_two_commit_repo()
        try:
            subprocess.run(["git", "checkout", "-q", head], cwd=repo, check=True)
            payload = self._payload(head, base, head)
            result = validate_review_payload(payload, check_git=True, cwd=repo)
            self.assertEqual(result.merge_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)
            self.assertEqual(len(result.validation_errors), 0)
        finally:
            tmp.cleanup()

    def test_head_checkout_with_test_merge_execution_passes(self):
        # The pinned reviewer CLI deliberately checks out the PR head before the
        # model runs while tests execute on the test-merge commit. Both are
        # legitimate candidate identities; an unrelated clean checkout is not.
        tmp, repo, base, head = self._make_two_commit_repo()
        try:
            subprocess.run(["git", "checkout", "-q", head], cwd=repo, check=True)
            (repo / "merge.txt").write_text("test-merge\n")
            subprocess.run(["git", "add", "merge.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "test-merge"], cwd=repo, check=True)
            execution = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertNotEqual(execution, head)
            subprocess.run(["git", "checkout", "-q", head], cwd=repo, check=True)

            payload = self._payload(head, base, execution)
            result = validate_review_payload(payload, check_git=True, cwd=repo)
            self.assertEqual(result.merge_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)
            self.assertEqual(result.validation_errors, [])
        finally:
            tmp.cleanup()

    def test_head_must_be_ancestor_of_execution(self):
        tmp, repo, base, head = self._make_two_commit_repo()
        try:
            # Execution older than head: head cannot be contained in execution.
            subprocess.run(["git", "checkout", "-q", base], cwd=repo, check=True)
            payload = self._payload(head, base, base)
            result = validate_review_payload(payload, check_git=True, cwd=repo)
            self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
            self.assertTrue(any("ancestor" in e for e in result.validation_errors))
        finally:
            tmp.cleanup()

    def test_missing_evidence_field_fails(self):
        payload = self._payload("1" * 40, "2" * 40, "3" * 40)
        del payload["evidence"]
        result = validate_review_payload(payload)
        self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
        self.assertTrue(any("evidence" in e for e in result.validation_errors))

    def test_non_object_evidence_fails(self):
        for bad in ("all good", ["claim one proven", "claim two proven"]):
            with self.subTest(evidence=bad):
                payload = self._payload("1" * 40, "2" * 40, "3" * 40)
                payload["evidence"] = bad
                result = validate_review_payload(payload)
                self.assertEqual(result.merge_readiness, MergeReadiness.NOT_READY.value)
                self.assertTrue(any("evidence" in e for e in result.validation_errors))


class TestReadinessProbes(unittest.TestCase):
    """F4: an endpoint responding is not proof the service is ready."""

    def _serve(self, status_code, body):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = body.encode()
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}/healthz"

    def _fetch(self, url):
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status, resp.read().decode()
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            try:
                body = exc.read().decode() if hasattr(exc, "read") else ""
            except Exception:  # noqa: BLE001
                body = ""
            return code, body

    def test_healthy_200_ok_is_ready(self):
        server, url = self._serve(200, json.dumps({"status": "ok", "qdrant": True}))
        try:
            code, body = self._fetch(url)
            self.assertTrue(evaluate_probe_response(code, body, require_agent_ok=True))
            self.assertTrue(evaluate_probe_response(code, body))
        finally:
            server.shutdown()
            server.server_close()

    def test_503_degraded_is_not_ready(self):
        server, url = self._serve(503, json.dumps({"status": "degraded", "qdrant": True}))
        try:
            code, body = self._fetch(url)
            self.assertFalse(evaluate_probe_response(code, body, require_agent_ok=True))
            self.assertFalse(evaluate_probe_response(code, body))
        finally:
            server.shutdown()
            server.server_close()

    def test_404_is_not_ready(self):
        server, url = self._serve(404, json.dumps({"error": "not found"}))
        try:
            code, body = self._fetch(url)
            self.assertFalse(evaluate_probe_response(code, body, require_agent_ok=True))
            self.assertFalse(evaluate_probe_response(code, body))
        finally:
            server.shutdown()
            server.server_close()

    def test_malformed_body_is_not_ready(self):
        server, url = self._serve(200, "not-json{{{")
        try:
            code, body = self._fetch(url)
            self.assertFalse(evaluate_probe_response(code, body, require_agent_ok=True))
            # Generic (non-agent) probes accept any 2xx body.
            self.assertTrue(evaluate_probe_response(code, body))
        finally:
            server.shutdown()
            server.server_close()

    def test_degraded_with_qdrant_true_still_not_ready(self):
        # Counterexample from the review: degraded overall status with a
        # nested qdrant:true flag must not count as agent readiness.
        body = json.dumps({"status": "degraded", "qdrant": True, "representation": "reembed_required"})
        self.assertFalse(evaluate_probe_response(503, body, require_agent_ok=True))
        self.assertFalse(evaluate_probe_response(200, body, require_agent_ok=True))

    def test_workflow_probes_are_status_and_contract_aware(self):
        workflow = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "opencode.yml"
        text = workflow.read_text(encoding="utf-8")
        # All service probes must be HTTP-status-aware (curl --fail), and the
        # agent probe must validate the readiness contract, not one nested flag.
        self.assertIn("curl -fsS -m 2 http://127.0.0.1:6333/collections", text)
        self.assertIn("curl -fsS -m 2 http://127.0.0.1:16686/", text)
        self.assertIn("curl -fsS -m 2 http://127.0.0.1:8001/healthz", text)
        self.assertIn("curl -fsS -m 2 http://127.0.0.1:8080/healthz", text)
        self.assertIn('"status"', text)
        self.assertNotIn('| grep -q \'"qdrant":true\'', text)


class TestAcceptanceUnionAndReviewerAuthority(unittest.TestCase):
    """F5/F6: unions of risk never reduce verification; job success is not approval."""

    def _manifest(self, profile, categories):
        return {
            "schema_version": 1,
            "head_sha": "1" * 40,
            "base_sha": "2" * 40,
            "execution_sha": "3" * 40,
            "profile": profile,
            "dirty": False,
            "matched_categories": categories,
        }

    def _ready_review(self):
        payload = {
            "schema_version": 1,
            "head_sha": "1" * 40,
            "base_sha": "2" * 40,
            "execution_sha": "3" * 40,
            "code_assessment": "acceptable",
            "verification": "complete",
            "candidate_currentness": "current",
            "merge_readiness": "ready_for_maintainer",
            "material_findings": [],
            "evidence": {"notes": "ok"},
        }
        return validate_review_payload(payload, expected_head="1" * 40,
                                       expected_base="2" * 40, expected_execution="3" * 40)

    def _not_ready_review(self):
        payload = {
            "schema_version": 1,
            "head_sha": "1" * 40,
            "base_sha": "2" * 40,
            "execution_sha": "3" * 40,
            "code_assessment": "changes_required",
            "verification": "incomplete",
            "candidate_currentness": "current",
            "merge_readiness": "not_ready",
            "material_findings": [{"id": "F1", "disposition": "unresolved", "description": "open"}],
            "evidence": {"notes": "blocked"},
        }
        return validate_review_payload(payload, expected_head="1" * 40,
                                       expected_base="2" * 40, expected_execution="3" * 40)

    def test_full_profile_keeps_packaging_when_deploy_present(self):
        decision = classify_paths(["pyproject.toml", "src/mainframe_rag/ingest/publish.py"])
        self.assertEqual(decision.profile, ProfileName.FULL)
        manifest = self._manifest("full", decision.matched_categories)
        self.assertIn("deploy", manifest["matched_categories"])
        summary = build_acceptance_summary(
            manifest,
            {"context_check": "success", "lint_and_types": "success", "unit_tests": "success",
             "simulation": "success", "gate_l1": "success", "packaging": "success",
             "reviewer": "success"},
            review=self._ready_review(),
        )
        packaging = next(l for l in summary.lanes if l.name == "packaging")
        self.assertTrue(packaging.required)
        # Omitting the packaging lane must block even though profile is full.
        blocked = build_acceptance_summary(
            manifest,
            {"context_check": "success", "lint_and_types": "success", "unit_tests": "success",
             "simulation": "success", "gate_l1": "success", "reviewer": "success"},
            review=self._ready_review(),
        )
        self.assertFalse(blocked.all_prerequisites_met)
        missing = next(l for l in blocked.lanes if l.name == "packaging")
        self.assertEqual(missing.state, LaneState.SELECTED_MISSING)

    def test_reviewer_success_with_not_ready_review_blocks(self):
        manifest = self._manifest("offline", ["prose"])
        summary = build_acceptance_summary(
            manifest,
            {"context_check": "success", "reviewer": "success"},
            review=self._not_ready_review(),
        )
        self.assertFalse(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.NOT_READY.value)
        reviewer = next(l for l in summary.lanes if l.name == "reviewer")
        self.assertEqual(reviewer.state, LaneState.SELECTED_FAILED)

    def test_missing_review_blocks_despite_job_success(self):
        manifest = self._manifest("offline", ["prose"])
        summary = build_acceptance_summary(
            manifest,
            {"context_check": "success", "reviewer": "success"},
            review=None,
        )
        self.assertFalse(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.NOT_READY.value)
        reviewer = next(l for l in summary.lanes if l.name == "reviewer")
        self.assertEqual(reviewer.state, LaneState.SELECTED_MISSING)


class TestCiUnitPartition(unittest.TestCase):
    """Exercise real pytest collection, filtering and exit status in isolation."""

    def test_shards_cover_each_selected_case_once_and_preserve_failure(self):
        import xml.etree.ElementTree as ET

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


class TestTaskCiConsumers(unittest.TestCase):
    """CI selection and shell guards; runner semantics live in Task contract tests."""

    root = pathlib.Path(__file__).resolve().parents[1]

    def test_task_edits_trigger_context_and_load_on_push_and_pull_request(self):
        import yaml
        from scripts.review_tooling import classify_paths, required_lanes

        changes = ("Taskfile.yml", "taskfiles/quality.yml", "taskfiles/airgap.yml",
                   "scripts/tools/run-task.sh", "scripts/tools/task-pin.txt")
        for workflow in ("agent-context.yml", "load.yml"):
            document = yaml.safe_load((self.root / ".github/workflows" / workflow).read_text())
            triggers = document.get("on", document.get(True))
            self.assertIn("pull_request", triggers)
            self.assertIn("push", triggers)
            for event in ("pull_request", "push"):
                self.assertFalse((triggers[event] or {}).get("paths"))
                self.assertFalse((triggers[event] or {}).get("paths-ignore"))
        for path in changes:
            decision = classify_paths([path])
            lanes = required_lanes({"profile": decision.profile.value,
                                    "matched_categories": decision.matched_categories,
                                    "changed_paths": [path]})
            self.assertTrue({"context_check", "load", "ha", "simulation", "packaging"}.issubset(lanes), path)

    def test_github_unit_and_context_contract_lanes_require_the_runner(self):
        product = (self.root / ".github/workflows/ci.yml").read_text()
        unit = product.split("  unit:\n", 1)[1].split("  test:\n", 1)[0]
        self.assertIn('TASK_CONTRACTS_REQUIRE_RUNNER: "1"', unit)
        self.assertLess(unit.index("sh scripts/tools/install-task.sh"), unit.index("pytest -q"))
        context = (self.root / ".github/workflows/agent-context.yml").read_text()
        self.assertLess(context.index("sh scripts/tools/install-task.sh"),
                        context.index("sh scripts/tools/run-task.sh qa:context"))
        import yaml

        steps = yaml.safe_load(context)["jobs"]["check-context"]["steps"]
        execution = next(step for step in steps if "--unittest" in step.get("run", ""))
        self.assertEqual(execution["env"]["TASK_CONTRACTS_REQUIRE_RUNNER"], "1")
        self.assertEqual(shlex.split(execution["run"])[-3:],
                         ["tests.test_agent_context", "tests.test_agent_doctor", "tests.test_taskfile_contracts"])

    def test_two_unit_vms_preserve_a_fail_closed_test_status(self):
        import yaml

        jobs = yaml.safe_load((self.root / ".github/workflows/ci.yml").read_text())["jobs"]
        self.assertEqual(jobs["unit"]["strategy"]["matrix"], {"shard": [1, 2]})
        self.assertIs(jobs["unit"]["strategy"]["fail-fast"], False)
        command = next(s["run"] for s in jobs["unit"]["steps"] if s.get("name") == "Run unit shard")
        self.assertIn("-p tests.ci_shard --unit-shard=${{ matrix.shard }}", command)
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
        import yaml

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
        import yaml

        job = yaml.safe_load((self.root / ".gitlab-ci.yml").read_text())["test"]
        self.assertEqual(job["variables"]["TASK_CONTRACTS_REQUIRE_RUNNER"], "1")
        commands = job["script"]
        script = "\n".join(commands)
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
            self.assertEqual((root / "tests-ran").read_text(), "-q\n")
            self.assertEqual((root / "doctor-args").read_text(),
                             f"scripts/agent_doctor.py\n--python\n{root / 'python'}\n")
            self.assertEqual(shlex.split(commands[1]),
                             ["sh", "scripts/tools/install-task.sh", "--archive", "$CI_TASK_ARCHIVE"])



class TestNativeExecutionEvidence(unittest.TestCase):
    def test_junit_counts_actual_records_not_declared_totals(self):
        from scripts.ci_evidence import junit_counts

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

    def test_native_command_requires_nonzero_fresh_passing_results(self):
        from unittest.mock import patch

        from scripts import ci_evidence

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
                    argv = ['ci_evidence.py', '--lane', 'unit_tests', '--job-name', 'unit (1/2)',
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
        from unittest.mock import patch

        from scripts import ci_evidence

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
                self.assertEqual((receipt["run_id"], receipt["run_attempt"]), (123, 2))
                # An updated base or head cannot reuse an earlier test merge.
                for field in ("base", "head"):
                    original = payload["pull_request"][field]["sha"]
                    payload["pull_request"][field]["sha"] = "f" * 40
                    event.write_text(json.dumps(payload))
                    with self.assertRaisesRegex(ValueError, "does not bind"):
                        ci_evidence.identity()
                    payload["pull_request"][field]["sha"] = original
                event.write_text(json.dumps(payload))
                policy.write_text("# dirty tracked policy\n")
                with self.assertRaisesRegex(ValueError, "dirty"):
                    ci_evidence.identity()
                git("restore", "scripts/review_tooling.py")
                git("checkout", "candidate")
                self.assertEqual(ci_evidence.identity()["execution_sha"], head)

    def test_identity_receipt_cannot_claim_test_results_or_reuse_structured_output(self):
        from unittest.mock import patch

        from scripts import ci_evidence

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

    def test_execution_identity_movement_cannot_pass(self):
        from unittest.mock import patch

        from scripts import ci_evidence

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            argv = ['ci_evidence.py', '--lane', 'lint_and_types', '--job-name', 'lint', '--output', str(root / 'out'),
                    '--', sys.executable, '-c', 'pass']
            with patch.object(ci_evidence, 'ROOT', root), patch.object(sys, 'argv', argv), patch.object(
                    ci_evidence, 'identity', side_effect=[{'execution_sha': 'a' * 40}, {'execution_sha': 'b' * 40}]):
                self.assertEqual(ci_evidence.main(), 1)
                self.assertFalse(json.loads((root / 'out/evidence.json').read_text())['passed'])

class TestNativeEvidenceConsumer(unittest.TestCase):
    def fixture(self):
        import hashlib

        from scripts.acceptance_evidence import PRODUCERS
        from scripts.ci_evidence import junit_bytes

        producer = next(p for p in PRODUCERS if p.job == "unit (1/2)")
        candidate = {"repository": "synthetic/repository", "repository_id": 42, "number": 3,
                     "head_sha": "a" * 40, "base_sha": "b" * 40, "execution_sha": "c" * 40,
                     "head_repository_id": 84}
        run = {"id": 123, "run_attempt": 2, "event": "pull_request", "path": ".github/workflows/ci.yml",
               "head_sha": "a" * 40, "status": "completed", "repository": {"id": 42, "full_name": "synthetic/repository"},
               "head_repository": {"id": 84}, "actor": {"id": 7}, "triggering_actor": {"login": "synthetic"}}
        job = {"id": 456, "run_id": 123, "run_attempt": 2, "head_sha": "a" * 40,
               "name": "unit (1/2)", "status": "completed", "conclusion": "success"}
        artifact = {"id": 789, "expired": False, "name": "evidence-unit-1-attempt-2",
                    "workflow_run": {"id": 123, "repository_id": 42, "head_repository_id": 84, "head_sha": "a" * 40}}
        commit = {"sha": "c" * 40, "parents": [{"sha": "b" * 40}, {"sha": "a" * 40}]}
        xml = b'<testsuite tests="999"><testcase name="actual"/></testsuite>'
        receipt = {"schema_version": 1, "repository": "synthetic/repository", "repository_id": 42,
                   "pull_request": 3, "head_sha": "a" * 40, "base_sha": "b" * 40,
                   "execution_sha": "c" * 40, "execution_parents": ["b" * 40, "a" * 40],
                   "event": "pull_request", "run_id": 123, "run_attempt": 2, "job_key": "unit",
                   "job_name": "unit (1/2)", "lane": "unit_tests", "actor_id": 7,
                   "triggering_actor": "synthetic", "workflow_sha": "c" * 40,
                   "workflow_ref": "synthetic/repository/.github/workflows/ci.yml@refs/pull/3/merge",
                   "policy_sha256": "d" * 64, "producer_sha256": "e" * 64,
                   "evidence_kind": "execution", "passed": True, "exit_code": 0, "tests": junit_bytes(xml)}
        return {"candidate": candidate, "producer": producer, "run": run, "job": job,
                "artifact": artifact, "execution_commit": commit, "policy_digest": "d" * 64,
                "producer_digest": "e" * 64, "workflow_source": b"approved workflow",
                "workflow_digest": hashlib.sha256(b"approved workflow").hexdigest()}, receipt, xml

    def packed(self, receipt, xml):
        import hashlib
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("evidence.json", json.dumps(receipt))
            archive.writestr("tests.xml", xml)
        raw = buffer.getvalue()
        return raw, "sha256:" + hashlib.sha256(raw).hexdigest()

    def test_human_review_requires_authorized_current_api_record_and_preserves_rejection(self):
        import copy

        from scripts.acceptance import collect_review

        candidate = self.fixture()[0]["candidate"]
        payload = {"schema_version": 1, "head_sha": candidate["head_sha"], "base_sha": candidate["base_sha"],
                   "execution_sha": candidate["execution_sha"], "code_assessment": "acceptable",
                   "verification": "complete", "candidate_currentness": "current",
                   "merge_readiness": "ready_for_maintainer", "material_findings": [], "evidence": {}}
        comment = {"id": 11, "user": {"id": 7, "login": "synthetic", "type": "User"},
                   "author_association": "OWNER", "created_at": "2026-09-24T10:00:00Z",
                   "updated_at": "2026-09-24T10:00:00Z", "body": json.dumps(payload),
                   "html_url": "https://github.com/synthetic/repository/pull/3#issuecomment-11"}
        class API:
            prefix = "repos/synthetic/repository/"
            def __init__(self):
                self.comment = copy.deepcopy(comment)
                self.reviews = []
                self.older_comments = []
                self.permission = "admin"
            def get(self, endpoint):
                if "/collaborators/" in endpoint:
                    return {"permission": self.permission}
                if "/reviews?" in endpoint:
                    return self.reviews
                return [*self.older_comments, self.comment]
        api = API()
        review, manual, identity = collect_review(api, {"number": 3, "user": {"id": 99}}, candidate)
        self.assertEqual(review.merge_readiness, "ready_for_maintainer")
        self.assertEqual(identity["actor_id"], 7)
        self.assertEqual(manual, {})
        # The maintainer independently reviews agent work using the same GitHub
        # account that the agent uses to open PRs. Account equality is not a
        # substitute for the actual human-review process.
        api.comment["user"]["id"] = 99
        shared_review, _, shared_identity = collect_review(api, {"number": 3, "user": {"id": 99}}, candidate)
        self.assertEqual(shared_review.merge_readiness, "ready_for_maintainer")
        self.assertEqual(shared_identity["actor_id"], 99)
        for change in ("bot", "author-pass-table", "stranger", "read-only", "stale", "changes-required", "native-rejection"):
            with self.subTest(change=change):
                api = API()
                revised = copy.deepcopy(payload)
                if change == "bot":
                    api.comment["user"]["type"] = "Bot"
                elif change == "author-pass-table":
                    api.comment["user"]["id"] = 99
                    revised = {"result": "PASS"}
                elif change == "stranger":
                    api.comment["author_association"] = "NONE"
                elif change == "read-only":
                    api.permission = "read"
                elif change == "stale":
                    revised["base_sha"] = "f" * 40
                elif change == "changes-required":
                    revised["code_assessment"] = "changes_required"
                else:
                    api.reviews = [{**copy.deepcopy(comment), "id": 12, "body": "",
                                    "submitted_at": "2026-09-24T11:00:00Z", "state": "CHANGES_REQUESTED",
                                    "commit_id": candidate["head_sha"]}]
                api.comment["body"] = json.dumps(revised)
                review, _, _ = collect_review(api, {"number": 3, "user": {"id": 99}}, candidate)
                self.assertTrue(review is None or review.merge_readiness == "not_ready")
        api = API()
        previous = copy.deepcopy(payload)
        previous["material_findings"] = [{"id": "F1", "disposition": "unresolved"}]
        api.reviews = [{**copy.deepcopy(comment), "id": 9, "body": json.dumps(previous),
                        "submitted_at": "2026-09-24T09:00:00Z", "state": "COMMENTED",
                        "commit_id": candidate["head_sha"]}]
        review, _, _ = collect_review(api, {"number": 3, "user": {"id": 99}}, candidate)
        self.assertEqual(review.merge_readiness, "not_ready")
        self.assertIn("earlier material finding", " ".join(review.validation_errors))
        api = API()
        rejection = {**copy.deepcopy(payload), "code_assessment": "changes_required"}
        api.older_comments = [{**copy.deepcopy(comment), "id": 8,
                               "created_at": "2026-09-24T08:00:00Z",
                               "updated_at": "2026-09-24T12:00:00Z", "body": json.dumps(rejection)}]
        review, _, identity = collect_review(api, {"number": 3, "user": {"id": 99}}, candidate)
        self.assertEqual(identity["id"], 8)
        self.assertEqual(review.merge_readiness, "not_ready")

    def test_collector_requires_all_native_shards_and_never_reuses_an_older_green_run(self):
        import hashlib

        from scripts.acceptance import VERIFICATION_INPUTS, collect_native

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "scripts").mkdir()
            (root / ".github/workflows").mkdir(parents=True)
            (root / "scripts/review_tooling.py").write_bytes(b"approved policy")
            (root / "scripts/ci_evidence.py").write_bytes(b"approved producer")
            (root / ".github/workflows/ci.yml").write_bytes(b"approved workflow")
            for relative in (*VERIFICATION_INPUTS, "taskfiles/quality.yml"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_bytes(b"approved verifier input")
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True,
                                               stderr=subprocess.DEVNULL).strip()
            git("init", "-b", "main")
            git("config", "user.name", "Synthetic Test")
            git("config", "user.email", "synthetic@example.invalid")
            git("add", ".")
            git("commit", "-m", "approved base")
            base = git("rev-parse", "HEAD")
            args, receipt, xml = self.fixture()
            args["candidate"]["base_sha"] = receipt["base_sha"] = base
            receipt["execution_parents"][0] = base
            args["execution_commit"]["parents"][0]["sha"] = base
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
                    self.seen = []
                def get(self, endpoint):
                    self.seen.append(endpoint)
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
                        return args["execution_commit"]
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
                    return b"approved workflow"
            api = API()
            result = collect_native(api, pr, root)
            self.assertEqual(next(r for r in result["native"] if r["job"] == "unit (1/2)")["status"], "success")
            self.assertNotIn("unit_tests", result["lane_statuses"])
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
            for path in ("Taskfile.yml", "taskfiles/quality.yml", "scripts/tools/run-task.sh",
                         "tests/hazards/critical.json", "scripts/check_hazard_sensitivity.py"):
                with self.subTest(path=path):
                    api.changed_input = path
                    with self.assertRaises(ValueError):
                        collect_native(api, pr, root)

    def test_accepts_exact_native_job_attempt_and_actual_test_records(self):
        from scripts.acceptance_evidence import normalize_native

        args, receipt, xml = self.fixture()
        args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
        result = normalize_native(**args)
        self.assertEqual((result["run_id"], result["run_attempt"], result["job_id"], result["artifact_id"]),
                         (123, 2, 456, 789))
        self.assertEqual(result["tests"]["executed"], 1)

    def test_rejects_wrong_actor_run_attempt_workflow_candidate_and_failed_execution(self):
        from scripts.acceptance_evidence import normalize_native

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
        from scripts.acceptance_evidence import normalize_native
        from scripts.ci_evidence import junit_bytes

        for xml in (b'<testsuite tests="100"/>', b'<testsuite><testcase><skipped/></testcase></testsuite>',
                    b'<testsuite><testcase name="different"/></testsuite>'):
            args, receipt, _ = self.fixture()
            # A green receipt must not cover different actual XML, even if both have one case.
            args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
            with self.assertRaises(ValueError):
                normalize_native(**args)
        args, receipt, _ = self.fixture()
        xml = b'<testsuite><testcase><skipped/></testcase></testsuite>'
        receipt["tests"] = junit_bytes(xml)
        args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
        with self.assertRaises(ValueError):
            normalize_native(**args)

    def test_candidate_cannot_substitute_workflow_or_a_different_merge_with_same_parents(self):
        from scripts.acceptance_evidence import normalize_native

        args, receipt, xml = self.fixture()
        args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
        args["workflow_source"] = b"candidate workflow that fabricates results"
        with self.assertRaises(ValueError):
            normalize_native(**args)
        args, receipt, xml = self.fixture()
        receipt["execution_sha"] = receipt["workflow_sha"] = "f" * 40
        args["execution_commit"]["sha"] = "f" * 40
        args["archive"], args["artifact"]["digest"] = self.packed(receipt, xml)
        with self.assertRaises(ValueError):
            normalize_native(**args)

    def test_hazard_receipts_require_each_approved_behavioral_kill(self):
        import copy
        import hashlib
        import io
        import zipfile

        from scripts.acceptance_evidence import PRODUCERS, normalize_native

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

        def normalize(value):
            args, receipt, _ = self.fixture()
            args["producer"] = next(p for p in PRODUCERS if p.lane == "hazards")
            args["job"]["name"] = "hazards"
            args["artifact"]["name"] = "evidence-hazards-attempt-2"
            args["hazard_policy"] = policy
            raw = json.dumps(value).encode()
            receipt.update(job_key="hazards", job_name="hazards", lane="hazards", tests=None,
                           result_sha256=hashlib.sha256(raw).hexdigest())
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("evidence.json", json.dumps(receipt))
                archive.writestr("results.json", raw)
            args["archive"] = buffer.getvalue()
            args["artifact"]["digest"] = "sha256:" + hashlib.sha256(args["archive"]).hexdigest()
            return normalize_native(**args)

        self.assertEqual(normalize(report)["status"], "success")
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

    def test_hazard_receipt_rejects_hash_valid_empty_structured_results(self):
        import hashlib
        import io
        import zipfile

        from scripts.acceptance_evidence import PRODUCERS, normalize_native

        args, receipt, _ = self.fixture()
        producer = next(p for p in PRODUCERS if p.lane == "hazards")
        args["producer"] = producer
        args["job"]["name"] = "hazards"
        args["artifact"]["name"] = "evidence-hazards-attempt-2"
        receipt.update(job_key="hazards", job_name="hazards", lane="hazards", tests=None,
                       result_sha256=hashlib.sha256(b"{}").hexdigest())
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("evidence.json", json.dumps(receipt))
            archive.writestr("results.json", b"{}")
        args["archive"] = buffer.getvalue()
        args["artifact"]["digest"] = "sha256:" + hashlib.sha256(args["archive"]).hexdigest()
        with self.assertRaises(ValueError):
            normalize_native(**args)

    def test_native_pagination_reads_later_pages_and_refuses_inconsistent_results(self):
        from scripts.acceptance_evidence import paginate

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
        import hashlib
        import io
        import stat
        import warnings
        import zipfile

        from scripts.acceptance_evidence import artifact_members

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


if __name__ == "__main__":
    unittest.main()


class TestAcceptanceSnapshot(unittest.TestCase):
    def test_unknown_or_missing_profile_keeps_full_obligations(self):
        from scripts.review_tooling import required_lanes

        for manifest in ({}, {"profile": {}}, {"profile": "unknown"}):
            with self.subTest(manifest=manifest):
                self.assertTrue({"simulation", "packaging", "load", "ha", "agent_probes",
                                 "eval_retrieval", "unit_tests", "hazards"} <= required_lanes(manifest))

    def test_paginated_pr_files_preserve_rename_bytes_and_refuse_truncation(self):
        from scripts.acceptance import changed_pr_paths

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

    def test_recheck_refuses_candidate_review_and_attempt_movement(self):
        import copy
        from unittest.mock import patch

        from scripts.acceptance import candidate_identity, recheck_current

        pr = {"number": 3, "user": {"id": 99}, "state": "open", "mergeable": True, "draft": False,
              "merge_commit_sha": "c" * 40, "head": {"sha": "a" * 40, "repo": {"id": 84}},
              "base": {"sha": "b" * 40, "ref": "main", "repo": {
                  "id": 42, "full_name": "synthetic/repository", "default_branch": "main"}}}
        class API:
            repository = "synthetic/repository"
            prefix = "repos/synthetic/repository/"
            def __init__(self):
                self.pr = copy.deepcopy(pr)
                self.run = {"id": 123, "path": ".github/workflows/ci.yml",
                            "run_attempt": 1, "status": "completed"}
            def get(self, endpoint):
                if "actions/runs?" in endpoint:
                    return {"total_count": 1, "workflow_runs": [self.run]}
                return self.pr
        result = {"candidate": candidate_identity(pr, API.repository), "draft": False,
                  "review_identity": {"id": 10}, "all_prerequisites_met": False,
                  "runs": {"ci.yml": {"run_id": 123, "run_attempt": 1, "status": "completed"}}}
        with patch("scripts.acceptance.collect_review", return_value=(None, {}, {"id": 10})) as review:
            recheck_current(API(), result)
            for mutation in ("head", "base", "merge", "draft", "attempt", "run", "review"):
                with self.subTest(mutation=mutation):
                    api = API()
                    review.return_value = (None, {}, {"id": 10})
                    if mutation in {"head", "base"}:
                        api.pr[mutation]["sha"] = "d" * 40
                    elif mutation == "merge":
                        api.pr["merge_commit_sha"] = "d" * 40
                    elif mutation == "draft":
                        api.pr["draft"] = True
                    elif mutation == "attempt":
                        api.run["run_attempt"] = 2
                    elif mutation == "run":
                        api.run["id"] = 124
                    else:
                        review.return_value = (None, {}, {"id": 11})
                    with self.assertRaises(ValueError):
                        recheck_current(api, result)


    def test_read_only_cli_never_emits_success_after_recheck_failure(self):
        import contextlib
        import io
        from unittest.mock import patch

        from scripts.acceptance import main

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

    def test_summary_consumes_current_review_and_draft_state(self):
        import copy
        from unittest.mock import patch

        from scripts.acceptance import candidate_identity, collect_acceptance

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
                    return []
                if "/comments?" in endpoint:
                    return [{"id": 11, "user": {"id": 7, "login": "reviewer", "type": "User"},
                             "author_association": "COLLABORATOR", "created_at": "2026-09-24T10:00:00Z",
                             "body": json.dumps(self.payload), "html_url": "https://example.invalid/review"}]
                if "/permission" in endpoint:
                    return {"permission": "write"}
                return self.pr
        native = {"candidate": candidate, "lane_statuses": {"context_check": "success"},
                  "native": [], "runs": {}, "policy_sha256": "d" * 64, "policy_inputs": {}}
        with patch("scripts.acceptance.collect_native", return_value=native):
            api = API()
            result = collect_acceptance(api, 3, pathlib.Path.cwd())
            self.assertTrue(result["all_prerequisites_met"])
            self.assertEqual({lane["name"] for lane in result["lanes"] if lane["required"]},
                             {"context_check", "reviewer"})
            api.pr["draft"] = True
            self.assertFalse(collect_acceptance(api, 3, pathlib.Path.cwd())["all_prerequisites_met"])
            api.pr["draft"] = False
            api.payload["code_assessment"] = "changes_required"
            self.assertFalse(collect_acceptance(api, 3, pathlib.Path.cwd())["all_prerequisites_met"])
            api.payload = payload
            native["lane_statuses"]["context_check"] = "skipped"
            self.assertFalse(collect_acceptance(api, 3, pathlib.Path.cwd())["all_prerequisites_met"])

    def test_publisher_pending_precedes_collection_and_stale_result_is_failure(self):
        import copy
        from unittest.mock import patch

        from scripts.acceptance import publish_acceptance

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
        import yaml

        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/acceptance.yml').read_text())
        events = workflow.get('on', workflow.get(True))
        self.assertTrue({'pull_request_target', 'workflow_run', 'issue_comment', 'push', 'schedule'} <= events.keys())
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
                self.assertEqual(token['with']['permission-issues'], 'write')
                self.assertEqual(token['with']['permission-pull-requests'], 'write')
                self.assertTrue(all(value == 'read' for value in job['permissions'].values()))
                self.assertEqual(token['with']['private-key'], '${{ secrets.ACCEPTANCE_APP_PRIVATE_KEY }}')
            else:
                self.assertNotIn('environment', job)
                self.assertEqual(job['permissions']['issues'], 'write')
                self.assertEqual(job['permissions']['pull-requests'], 'write')
                self.assertEqual(job['permissions']['contents'], 'read')
        signal = yaml.safe_load((root / '.github/workflows/acceptance-review-signal.yml').read_text())
        self.assertEqual(signal['permissions'], {})
        self.assertEqual(signal['jobs']['signal']['steps'], [{'run': 'true'}])

    def test_gitlab_l1_note_appends_literal_body_without_claiming_marker_ownership(self):
        import yaml

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


class ReviewTemplateTests(unittest.TestCase):
    def test_template_binds_current_merge_without_approving_or_writing(self):
        import copy

        from scripts.acceptance import review_template
        from scripts.review_tooling import validate_review_payload

        pr = {'number': 3, 'state': 'open', 'mergeable': True, 'draft': False,
              'head': {'sha': 'a' * 40, 'repo': {'id': 84}},
              'base': {'sha': 'b' * 40, 'ref': 'main', 'repo': {
                  'id': 42, 'full_name': 'synthetic/repository', 'default_branch': 'main'}},
              'merge_commit_sha': 'c' * 40}

        class API:
            repository = 'synthetic/repository'
            prefix = 'repos/synthetic/repository/'

            def __init__(self, move=False, wrong_parents=False):
                self.reads = 0
                self.move = move
                self.wrong_parents = wrong_parents

            def get(self, endpoint):
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
        for api in (API(move=True), API(wrong_parents=True)):
            with self.subTest(api=api), self.assertRaises(ValueError):
                review_template(api, 3)

    def test_template_cli_refuses_publication_and_bulk_selection(self):
        import contextlib
        import io
        from unittest.mock import patch

        from scripts.acceptance import main

        for extra in (['--pr', '3', '--publish'], ['--all-open']):
            with self.subTest(extra=extra), patch('scripts.acceptance.GitHub') as api, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(['--repository', 'synthetic/repository', '--review-template', *extra])
            self.assertEqual(error.exception.code, 2)
            api.assert_not_called()

    def test_failed_acceptance_still_exports_identity_template_and_check_link(self):
        import tempfile
        from unittest.mock import patch

        from scripts.acceptance import publish_acceptance

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
        import tempfile
        from unittest.mock import patch

        from scripts.acceptance import publish_acceptance

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

    def test_both_publishers_upload_templates_after_unmet_acceptance(self):
        import yaml

        workflow = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] /
                                   '.github/workflows/acceptance.yml').read_text())
        for name in ('advisory', 'trusted-app'):
            with self.subTest(job=name):
                steps = workflow['jobs'][name]['steps']
                command = next(s['run'] for s in steps if 'python -m scripts.acceptance' in s.get('run', ''))
                self.assertIn('--review-templates-dir "$RUNNER_TEMP/review-templates"', command)
                self.assertIn('--publisher-run-id "$GITHUB_RUN_ID"', command)
                upload = next(s for s in steps if s.get('name') == 'Upload human review templates')
                self.assertEqual(upload['if'], 'always()')
                self.assertEqual(upload['with']['path'], '${{ runner.temp }}/review-templates/*.json')

    def test_pr_template_shell_does_not_upload_failed_generation_as_a_template(self):
        import os
        import subprocess
        import tempfile

        import yaml

        workflow = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] /
                                   '.github/workflows/agent-context.yml').read_text())
        job = workflow['jobs']['check-context']
        self.assertEqual(job['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        command = next(s['run'] for s in job['steps'] if s.get('name') == 'Generate human review template')
        upload = next(s for s in job['steps'] if s.get('name') == 'Upload human review template')
        self.assertEqual(upload['with']['path'], '${{ runner.temp }}/review-template.json')
        for failed in ('0', '1'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                stub = root / 'python'
                stub.write_text('#!/bin/sh\n'
                                'if [ "$TEMPLATE_STUB_FAIL" = 1 ]; then\n'
                                '  echo \'{"error":"unavailable"}\'; exit 1\n'
                                'fi\n'
                                'echo \'{"head_sha":"synthetic-current-head"}\'\n')
                stub.chmod(0o755)
                result = subprocess.run(['sh', '-c', command], capture_output=True, text=True, check=False,
                                        env={**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                                             'RUNNER_TEMP': directory, 'TEMPLATE_STUB_FAIL': failed,
                                             'REVIEW_REPOSITORY': 'synthetic/repository', 'REVIEW_PR': '3'})
                self.assertEqual(result.returncode, 0, result.stderr)
                artifact = root / 'review-template.json'
                self.assertEqual(artifact.exists(), failed == '0')
                if failed == '0':
                    self.assertEqual(json.loads(artifact.read_text())['head_sha'], 'synthetic-current-head')
                else:
                    self.assertIn('::warning::', result.stdout)

    def test_template_comments_append_deduplicate_and_preserve_human_text(self):
        import copy

        from scripts.acceptance import post_review_template, review_template_comment

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
        from unittest.mock import patch

        from scripts.acceptance import GitHub

        api = GitHub('synthetic/repository')
        for endpoint, method in [('repos/synthetic/repository/issues/3/comments', 'PATCH'),
                                 ('repos/synthetic/repository/issues/comments/17', 'PATCH'),
                                 ('repos/synthetic/repository/pulls/3/reviews', 'POST'),
                                 ('repos/other/repository/issues/3/comments', 'POST')]:
            with self.subTest(endpoint=endpoint), patch('scripts.acceptance.subprocess.run') as run, \
                 self.assertRaises(ValueError):
                api.write(endpoint, {'body': 'template'}, method=method)
            run.assert_not_called()

    def test_shared_account_scaffolding_is_not_review_or_finding_history(self):
        import copy

        from scripts.acceptance import collect_review

        candidate = {'head_sha': 'a' * 40, 'base_sha': 'b' * 40, 'execution_sha': 'c' * 40}
        human = {'schema_version': 1, **candidate, 'candidate_currentness': 'current',
                 'code_assessment': 'acceptable', 'verification': 'complete',
                 'merge_readiness': 'ready_for_maintainer', 'material_findings': [], 'evidence': {}}
        scaffold = {**human, 'code_assessment': '<acceptable|changes_required|incomplete>',
                    'verification': '<complete|incomplete|failed>',
                    'merge_readiness': '<ready_for_maintainer|not_ready>'}
        legacy = [{'id': '<finding ID; use [] only if none>', 'disposition': 'unresolved',
                   'description': '<finding and evidence; carry forward prior findings>'}]

        def comment(number, payload):
            return {'id': number, 'user': {'id': 7, 'login': 'synthetic', 'type': 'User'},
                    'author_association': 'OWNER', 'updated_at': f'2026-09-24T10:00:0{number}Z',
                    'body': '<!-- generated-human-review-template -->\n```json\n' + json.dumps(payload) + '\n```',
                    'html_url': f'https://github.com/synthetic/repository/pull/3#issuecomment-{number}'}

        class API:
            prefix = 'repos/synthetic/repository/'

            def __init__(self, comments):
                self.comments = comments

            def get(self, endpoint):
                if '/collaborators/' in endpoint:
                    return {'permission': 'admin'}
                if '/reviews?' in endpoint:
                    return []
                return self.comments

        for findings in ([], legacy):
            with self.subTest(findings=findings):
                template = {**scaffold, 'material_findings': findings}
                api = API([comment(1, template), comment(2, human), comment(3, template)])
                review, _, identity = collect_review(api, {'number': 3}, candidate)
                self.assertEqual(review.merge_readiness, 'ready_for_maintainer')
                self.assertEqual(identity['id'], 2)
                review, _, _ = collect_review(API([comment(1, template)]), {'number': 3}, candidate)
                self.assertIsNone(review)
        real_finding = copy.deepcopy(scaffold)
        real_finding['material_findings'] = [{'id': 'F1', 'disposition': 'unresolved', 'description': 'Real defect'}]
        review, _, _ = collect_review(API([comment(1, real_finding), comment(2, human)]), {'number': 3}, candidate)
        self.assertEqual(review.merge_readiness, 'not_ready')
        self.assertTrue(any('omits earlier material finding' in e for e in review.validation_errors))
        malformed = {**human, 'material_findings': [{'id': '[]', 'disposition': '[]', 'description': '[]'}]}
        review, _, _ = collect_review(API([comment(1, malformed)]), {'number': 3}, candidate)
        self.assertEqual(review.merge_readiness, 'not_ready')
