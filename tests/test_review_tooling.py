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

    def test_classify_empty_paths(self):
        decision = classify_paths([])
        self.assertEqual(decision.profile, ProfileName.OFFLINE)
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
        summary = build_acceptance_summary(manifest, lane_statuses)
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
        summary = build_acceptance_summary(manifest, lane_statuses)
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
        summary = build_acceptance_summary(manifest, lane_statuses)
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
        summary = build_acceptance_summary(manifest, lane_statuses)
        self.assertFalse(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.NOT_READY.value)

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
        summary = build_acceptance_summary(manifest, lane_statuses)
        self.assertTrue(summary.all_prerequisites_met)
        self.assertEqual(summary.recommended_readiness, MergeReadiness.READY_FOR_MAINTAINER.value)

    def test_maintainer_authority_preservation_in_markdown(self):
        manifest = self._sample_manifest(profile="offline")
        lane_statuses = {
            "context_check": "success",
            "lint_and_types": "success",
            "reviewer": "success",
        }
        summary = build_acceptance_summary(manifest, lane_statuses)
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

            # All obligations met exits 0
            res = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "summarize-acceptance",
                    "--manifest",
                    str(manifest_file),
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

            # Missing obligation exits 1
            res_fail = subprocess.run(
                [
                    sys.executable,
                    "scripts/review_tooling.py",
                    "summarize-acceptance",
                    "--manifest",
                    str(manifest_file),
                    "--lane",
                    "context_check:success",
                    # lint_and_types omitted
                    "--lane",
                    "reviewer:success",
                    "--check",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_fail.returncode, 1)


if __name__ == "__main__":
    unittest.main()
