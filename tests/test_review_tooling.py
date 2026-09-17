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
import pathlib
import subprocess
import sys
import tempfile
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
            "Makefile",
        ]
        decision = classify_paths(paths)
        self.assertEqual(decision.profile, ProfileName.OFFLINE)
        self.assertEqual(decision.services, [])
        self.assertFalse(decision.needs_qdrant)
        self.assertFalse(decision.needs_vllm)
        self.assertFalse(decision.needs_jaeger)
        self.assertFalse(decision.needs_agent)

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
            "scripts/airgap/deploy.sh",
            "deploy/kustomize/base/agent.yaml",
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
        for path in ("tests/test_config.py", "tests/conftest.py", "tests/test_airgap_deploy_sh.py"):
            decision = classify_paths([path])
            if path.startswith("tests/test_airgap_"):
                self.assertEqual(decision.profile, ProfileName.DEPLOY)
            else:
                self.assertEqual(decision.profile, ProfileName.OFFLINE)
                self.assertIn("tests", decision.matched_categories)

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

    def test_producerless_lanes_are_unselected_and_do_not_block(self):
        # agent_probes/eval_retrieval have no CI producer; they must never
        # block purely by being absent.
        manifest = self._sample_manifest(profile="http")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "unit_tests": "success",
            "simulation": "success",
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses, review=self._ready_review())
        self.assertTrue(summary.all_prerequisites_met)
        for name in ("agent_probes", "eval_retrieval"):
            lane = next(l for l in summary.lanes if l.name == name)
            self.assertFalse(lane.required)
            self.assertEqual(lane.state, LaneState.UNSELECTED)

    def test_all_selected_lanes_passed_produces_ready(self):
        manifest = self._sample_manifest(profile="http")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "unit_tests": "success",
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
                "docs/testing.md",
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

            gh_text = gh_out_file.read_text()
            self.assertIn("profile=offline", gh_text)
            self.assertIn("needs_qdrant=false", gh_text)

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
        payload = self._payload("1" * 40, "2" * 40, "3" * 40)
        payload["evidence"] = "all good"
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


if __name__ == "__main__":
    unittest.main()
