"""Adversarial stress test suite challenging Invariant D3 (Unmarked Residue Exclusion & Sweep).

Author: teamwork_preview_challenger (challenger_m2_1)
Focus:
- sweep_unmarked_residue: purging unknown doc_id, wrong source_rev, orphaned completions, pagination
- audit_unmarked_residue: fail-closed detection of unpurged residue, wrong rules_v, missing payload
- cutover safety: live alias points strictly to verified coverage with zero residue
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client import models

from mainframe_rag.ingest import run_ingest
from mainframe_rag.ingest.identity import source_rev_key
from mainframe_rag.ingest.inventory import InventoryRecord
from mainframe_rag.ingest.publish import (
    audit_unmarked_residue,
    sweep_unmarked_residue,
    verify_all_complete,
)
from mainframe_rag.ingest.representation import (
    STATE_COMMITTED,
    manifest_point_id,
    write_manifest,
)
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.ingest.run_ingest import _DocLocks, _upsert_one
from tests.test_ingest_completion import _chunks, _vectors
from tests.test_ingest_publish import (
    ALIAS,
    PublishFake,
    _build_doc,
    _parsed_doc,
    _publish_env,
    _run_main,
    _settings,
)

# ============================================================================
# Group 1: sweep_unmarked_residue — Unknown doc_id, Wrong source_rev, Missing Payload
# ============================================================================


class TestSweepUnknownDocAndStaleRevision:
    """Stress-test active sweeping of unknown doc_id, stale revision, and corrupted points."""

    def test_sweep_purges_unknown_doc_id_with_source_rev(self, monkeypatch):
        """Points with unknown doc_id and source_rev must be swept and deleted."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-sweep-unk-doc", batch_size=16)
        write_manifest(fake, "stg-sweep-unk-doc__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-sweep-unk-doc"] = []

        # Valid Doc A
        chunks_a = _chunks(doc_id="DOC_A", n=2)
        rev_a = source_rev_key("v", "p", "1", "a" * 64)
        _upsert_one(_parsed_doc("DOC_A", "a" * 64), chunks_a, _vectors(2),
                    staging, _DocLocks(), None, False, src_labels="||")

        # Unknown Doc X (injected residue)
        rev_x = source_rev_key("v", "p", "1", "x" * 64)
        fake.collections["stg-sweep-unk-doc"].append(
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={"doc_id": "DOC_X", "source_rev": rev_x, "sha256": "x" * 64, "rules_v": rules_v, "text": "unk"},
            )
        )

        inv = {
            "a.pdf": InventoryRecord(
                path="a.pdf", sha256="a" * 64, doc_id="DOC_A",
                status="upserted", rules_version=rules_v, source_rev=rev_a,
            ),
        }
        walked = [("a.pdf", "a" * 64)]

        # Before sweep: audit flags it
        problems = audit_unmarked_residue(fake, staging, walked, inv, rules_v)
        assert len(problems) == 1
        assert "unmarked residue detected" in problems[0]

        # Execute sweep
        swept = sweep_unmarked_residue(fake, staging, walked, inv)
        assert swept == 1

        # After sweep: unknown doc is purged, valid Doc A preserved
        points = fake.collections["stg-sweep-unk-doc"]
        assert len(points) == 2
        assert all((p.payload or {}).get("doc_id") == "DOC_A" for p in points)

        # Audit passes cleanly
        assert audit_unmarked_residue(fake, staging, walked, inv, rules_v) == []

    def test_sweep_purges_unknown_doc_id_without_source_rev(self, monkeypatch):
        """Points with unknown doc_id and missing source_rev must be swept via delete_by_doc."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-sweep-nosrc", batch_size=16)
        write_manifest(fake, "stg-sweep-nosrc__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-sweep-nosrc"] = []

        # Valid Doc A
        chunks_a = _chunks(doc_id="DOC_A", n=2)
        rev_a = source_rev_key("v", "p", "1", "a" * 64)
        _upsert_one(_parsed_doc("DOC_A", "a" * 64), chunks_a, _vectors(2),
                    staging, _DocLocks(), None, False, src_labels="||")

        # Unknown Doc Y without source_rev
        fake.collections["stg-sweep-nosrc"].append(
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={"doc_id": "DOC_Y", "sha256": "y" * 64, "rules_v": rules_v, "text": "nosrc"},
            )
        )

        inv = {
            "a.pdf": InventoryRecord(
                path="a.pdf", sha256="a" * 64, doc_id="DOC_A",
                status="upserted", rules_version=rules_v, source_rev=rev_a,
            ),
        }
        walked = [("a.pdf", "a" * 64)]

        swept = sweep_unmarked_residue(fake, staging, walked, inv)
        assert swept == 1

        points = fake.collections["stg-sweep-nosrc"]
        assert len(points) == 2
        assert all((p.payload or {}).get("doc_id") == "DOC_A" for p in points)
        assert audit_unmarked_residue(fake, staging, walked, inv, rules_v) == []

    def test_sweep_purges_orphan_points_with_no_payload_or_no_doc_id(self, monkeypatch):
        """Points with empty payload or missing doc_id must be swept via PointIdsList."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-sweep-orphan-payload", batch_size=16)
        write_manifest(fake, "stg-sweep-orphan-payload__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-sweep-orphan-payload"] = []

        chunks_a = _chunks(doc_id="DOC_A", n=2)
        rev_a = source_rev_key("v", "p", "1", "a" * 64)
        _upsert_one(_parsed_doc("DOC_A", "a" * 64), chunks_a, _vectors(2),
                    staging, _DocLocks(), None, False, src_labels="||")

        # Injected point with empty payload
        orphan_id1 = str(uuid.uuid4())
        fake.collections["stg-sweep-orphan-payload"].append(
            models.PointStruct(
                id=orphan_id1,
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={},
            )
        )
        # Injected point with payload but no doc_id
        orphan_id2 = str(uuid.uuid4())
        fake.collections["stg-sweep-orphan-payload"].append(
            models.PointStruct(
                id=orphan_id2,
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={"text": "no doc id", "source_rev": "some_rev"},
            )
        )

        inv = {
            "a.pdf": InventoryRecord(
                path="a.pdf", sha256="a" * 64, doc_id="DOC_A",
                status="upserted", rules_version=rules_v, source_rev=rev_a,
            ),
        }
        walked = [("a.pdf", "a" * 64)]

        swept = sweep_unmarked_residue(fake, staging, walked, inv)
        assert swept == 2

        points = fake.collections["stg-sweep-orphan-payload"]
        assert len(points) == 2
        assert all((p.payload or {}).get("doc_id") == "DOC_A" for p in points)
        assert audit_unmarked_residue(fake, staging, walked, inv, rules_v) == []

    def test_sweep_purges_stale_source_rev_while_preserving_active_rev(self, monkeypatch):
        """When Doc A has points under both stale rev_old and active rev_new,
        sweep_unmarked_residue purges rev_old points and preserves rev_new."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-sweep-multi-rev", batch_size=16)
        write_manifest(fake, "stg-sweep-multi-rev__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-sweep-multi-rev"] = []

        rev_new = source_rev_key("v", "p", "1", "a_new" * 16)
        rev_old = source_rev_key("v", "p", "1", "a_old" * 16)

        # Upsert 2 points under active rev_new
        chunks_new = _chunks(doc_id="DOC_A", n=2)
        _upsert_one(_parsed_doc("DOC_A", "a_new" * 16), chunks_new, _vectors(2),
                    staging, _DocLocks(), None, False, src_labels="||")

        # Inject 3 points under stale rev_old for the same doc_id
        for i in range(3):
            fake.collections["stg-sweep-multi-rev"].append(
                models.PointStruct(
                    id=str(uuid.uuid4()),
                    vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                    payload={"doc_id": "DOC_A", "source_rev": rev_old, "sha256": "a_old" * 16, "rules_v": rules_v, "text": f"old_{i}"},
                )
            )

        assert len(fake.collections["stg-sweep-multi-rev"]) == 5

        inv = {
            "a.pdf": InventoryRecord(
                path="a.pdf", sha256="a_new" * 16, doc_id="DOC_A",
                status="upserted", rules_version=rules_v, source_rev=rev_new,
            ),
        }
        walked = [("a.pdf", "a_new" * 16)]

        # Before sweep: audit fails due to 3 stale rev points
        problems = audit_unmarked_residue(fake, staging, walked, inv, rules_v)
        assert len(problems) == 1
        assert "3 point(s)" in problems[0]

        # Execute sweep
        swept = sweep_unmarked_residue(fake, staging, walked, inv)
        assert swept == 3

        # After sweep: strictly rev_new points survive
        points = fake.collections["stg-sweep-multi-rev"]
        assert len(points) == 2
        for p in points:
            assert p.payload["doc_id"] == "DOC_A"
            assert p.payload["source_rev"] == rev_new

        assert audit_unmarked_residue(fake, staging, walked, inv, rules_v) == []

    def test_sweep_purges_points_for_failed_inventory_records(self, monkeypatch):
        """If a document has inventory status 'failed', its points must be swept as residue."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-sweep-failed-doc", batch_size=16)
        write_manifest(fake, "stg-sweep-failed-doc__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-sweep-failed-doc"] = []

        rev_fail = source_rev_key("v", "p", "1", "f" * 64)
        fake.collections["stg-sweep-failed-doc"].append(
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={"doc_id": "DOC_FAIL", "source_rev": rev_fail, "sha256": "f" * 64, "rules_v": rules_v, "text": "fail"},
            )
        )

        inv = {
            "f.pdf": InventoryRecord(
                path="f.pdf", sha256="f" * 64, doc_id="DOC_FAIL",
                status="failed", rules_version=rules_v, source_rev=rev_fail,
            ),
        }
        walked = [("f.pdf", "f" * 64)]

        swept = sweep_unmarked_residue(fake, staging, walked, inv)
        assert swept == 1
        assert fake.collections["stg-sweep-failed-doc"] == []


# ============================================================================
# Group 2: sweep_unmarked_residue — Orphaned Completion Markers
# ============================================================================


class TestSweepCompletionMarkers:
    """Stress-test sweeping of completion markers while preserving manifest point."""

    def test_sweep_purges_stale_completion_markers_and_preserves_manifest(self, monkeypatch):
        """Completion markers for deleted doc_id or stale source_rev are purged; manifest point is retained."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-comp-sweep", batch_size=16)
        comp_col = "stg-comp-sweep__completions"
        write_manifest(fake, comp_col, staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-comp-sweep"] = []

        # Valid Doc A
        rev_a = source_rev_key("v", "p", "1", "a" * 64)
        marker_a_id = str(uuid.uuid4())
        fake.collections[comp_col].append(
            models.PointStruct(
                id=marker_a_id,
                vector={"dense": [0.0] * 256},
                payload={"doc_id": "DOC_A", "source_rev": rev_a, "sha256": "a" * 64, "rules_v": rules_v},
            )
        )

        # Stale marker for Doc A with old rev
        rev_a_old = source_rev_key("v", "p", "0", "a_old" * 16)
        marker_a_old_id = str(uuid.uuid4())
        fake.collections[comp_col].append(
            models.PointStruct(
                id=marker_a_old_id,
                vector={"dense": [0.0] * 256},
                payload={"doc_id": "DOC_A", "source_rev": rev_a_old, "sha256": "a_old" * 16, "rules_v": rules_v},
            )
        )

        # Deleted Doc B marker
        marker_b_id = str(uuid.uuid4())
        fake.collections[comp_col].append(
            models.PointStruct(
                id=marker_b_id,
                vector={"dense": [0.0] * 256},
                payload={"doc_id": "DOC_B", "source_rev": "rev_b", "sha256": "b" * 64, "rules_v": rules_v},
            )
        )

        # Sourceless marker for deleted Doc C
        marker_c_id = str(uuid.uuid4())
        fake.collections[comp_col].append(
            models.PointStruct(
                id=marker_c_id,
                vector={"dense": [0.0] * 256},
                payload={"doc_id": "DOC_C", "sha256": "c" * 64, "rules_v": rules_v},
            )
        )

        inv = {
            "a.pdf": InventoryRecord(
                path="a.pdf", sha256="a" * 64, doc_id="DOC_A",
                status="upserted", rules_version=rules_v, source_rev=rev_a,
            ),
        }
        walked = [("a.pdf", "a" * 64)]

        sweep_unmarked_residue(fake, staging, walked, inv)

        # Completions collection must now retain ONLY:
        # 1. Manifest point (no doc_id)
        # 2. Valid Doc A marker (rev_a)
        surviving = fake.collections[comp_col]
        surviving_ids = {str(p.id) for p in surviving}
        mp_id = manifest_point_id(comp_col)

        assert mp_id in surviving_ids, "Manifest point must be preserved"
        assert marker_a_id in surviving_ids, "Active Doc A marker must be preserved"
        assert marker_a_old_id not in surviving_ids, "Stale Doc A marker must be purged"
        assert marker_b_id not in surviving_ids, "Deleted Doc B marker must be purged"
        assert marker_c_id not in surviving_ids, "Deleted Doc C marker must be purged"


# ============================================================================
# Group 3: audit_unmarked_residue & verify_all_complete Fail-Closed Verification
# ============================================================================


class TestAuditUnmarkedResidueFailClosed:
    """Stress-test audit_unmarked_residue and verify_all_complete gate defenses."""

    def test_audit_flags_point_with_mismatched_rules_version(self, monkeypatch):
        """Points with mismatched rules_v must be flagged as unmarked residue."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-wrong-rules-audit", batch_size=16)
        write_manifest(fake, "stg-wrong-rules-audit__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-wrong-rules-audit"] = []

        rev = source_rev_key("v", "p", "1", "a" * 64)
        fake.collections["stg-wrong-rules-audit"].append(
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={"doc_id": "DOC_A", "source_rev": rev, "sha256": "a" * 64, "rules_v": "old_v0", "text": "wrong rules"},
            )
        )

        inv = {
            "a.pdf": InventoryRecord(
                path="a.pdf", sha256="a" * 64, doc_id="DOC_A",
                status="upserted", rules_version=rules_v, source_rev=rev,
            ),
        }
        walked = [("a.pdf", "a" * 64)]

        problems = audit_unmarked_residue(fake, staging, walked, inv, rules_v)
        assert len(problems) == 1
        assert "unmarked residue detected (1 point(s)" in problems[0]

    def test_audit_flags_sourceless_point_under_valid_doc_id(self, monkeypatch):
        """Points under a valid doc_id that lack source_rev must be flagged as unmarked residue."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-sourceless-audit", batch_size=16)
        write_manifest(fake, "stg-sourceless-audit__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-sourceless-audit"] = []

        rev = source_rev_key("v", "p", "1", "a" * 64)
        # Point has valid doc_id and rules_v, but source_rev is None
        bad_id = str(uuid.uuid4())
        fake.collections["stg-sourceless-audit"].append(
            models.PointStruct(
                id=bad_id,
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={"doc_id": "DOC_A", "sha256": "a" * 64, "rules_v": rules_v, "text": "no rev"},
            )
        )

        inv = {
            "a.pdf": InventoryRecord(
                path="a.pdf", sha256="a" * 64, doc_id="DOC_A",
                status="upserted", rules_version=rules_v, source_rev=rev,
            ),
        }
        walked = [("a.pdf", "a" * 64)]

        problems = audit_unmarked_residue(fake, staging, walked, inv, rules_v)
        assert len(problems) == 1
        assert bad_id in problems[0]

    def test_audit_flags_point_when_inventory_record_is_missing(self, monkeypatch):
        """If walked has (path, sha) but inventory lacks the record, points are flagged as residue."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-missing-inv-audit", batch_size=16)
        write_manifest(fake, "stg-missing-inv-audit__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-missing-inv-audit"] = []

        fake.collections["stg-missing-inv-audit"].append(
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={"doc_id": "DOC_A", "source_rev": "rev", "sha256": "a" * 64, "rules_v": rules_v, "text": "t"},
            )
        )

        # Inventory is empty
        problems = audit_unmarked_residue(fake, staging, [("a.pdf", "a" * 64)], {}, rules_v)
        assert len(problems) == 1
        assert "unmarked residue detected" in problems[0]

    def test_audit_handles_nonexistent_collection_gracefully(self, monkeypatch):
        """If collection does not exist, audit returns [] without raising."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-nonexistent", batch_size=16)
        problems = audit_unmarked_residue(fake, staging, [], {}, "v1")
        assert problems == []


# ============================================================================
# Group 4: Pagination & High-Volume Residue Stress Test
# ============================================================================


class TestPaginationAndScaleResidue:
    """Stress-test scroll pagination during residue sweep with small page_size."""

    def test_sweep_paginates_and_purges_many_residue_points(self, monkeypatch):
        """When residue points span multiple scroll pages, sweep_unmarked_residue
        must exhaustively paginate and delete all residue points."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        # Set scan page size to 100 (Settings.ingest_scan_page_size ge=100)
        staging = _settings(
            qdrant_collection="stg-scale-sweep",
            batch_size=16,
            ingest_scan_page_size=100,
        )
        write_manifest(fake, "stg-scale-sweep__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-scale-sweep"] = []

        # Valid Doc A: 4 chunks
        chunks_a = _chunks(doc_id="DOC_A", n=4)
        rev_a = source_rev_key("v", "p", "1", "a" * 64)
        _upsert_one(_parsed_doc("DOC_A", "a" * 64), chunks_a, _vectors(4),
                    staging, _DocLocks(), None, False, src_labels="||")

        # Inject 250 residue points spanning 10 different unknown doc_ids (3 scroll pages: 100, 100, 50)
        for doc_num in range(10):
            doc_id = f"RESIDUE_DOC_{doc_num}"
            rev = f"stale_rev_{doc_num}"
            for pt_num in range(25):
                fake.collections["stg-scale-sweep"].append(
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                        payload={"doc_id": doc_id, "source_rev": rev, "sha256": "x" * 64, "rules_v": rules_v, "text": f"{doc_id}_{pt_num}"},
                    )
                )

        assert len(fake.collections["stg-scale-sweep"]) == 254  # 4 valid + 250 residue

        inv = {
            "a.pdf": InventoryRecord(
                path="a.pdf", sha256="a" * 64, doc_id="DOC_A",
                status="upserted", rules_version=rules_v, source_rev=rev_a,
            ),
        }
        walked = [("a.pdf", "a" * 64)]

        # Audit flags 250 points
        problems = audit_unmarked_residue(fake, staging, walked, inv, rules_v)
        assert len(problems) == 1
        assert "250 point(s)" in problems[0]

        # Execute sweep with page_size=100 (requires 3 scroll pages)
        swept = sweep_unmarked_residue(fake, staging, walked, inv)
        assert swept == 250

        # All 250 residue points are purged, exactly 4 valid Doc A points survive
        surviving = fake.collections["stg-scale-sweep"]
        assert len(surviving) == 4
        assert all(p.payload["doc_id"] == "DOC_A" for p in surviving)
        assert audit_unmarked_residue(fake, staging, walked, inv, rules_v) == []


# ============================================================================
# Group 5: End-to-End Adversarial Walkthrough (Cutover & Zero Residue Certification)
# ============================================================================


class TestEndToEndCutoverZeroResidue:
    """End-to-end integration tests verifying zero residue after cutover and fail-closed safety."""

    def test_multidoc_deletion_and_update_leaves_zero_residue_in_active_alias(self, tmp_path, monkeypatch):
        """Adversarial walkthrough:
        1. Ingest 3 documents (Doc A, Doc B, Doc C) -> publish Gen 1.
        2. Delete Doc B, update Doc C to new content.
        3. Re-publish -> cuts over to Gen 2.
        4. Assert: Live alias target contains strictly Doc A and updated Doc C, ZERO residue from Doc B or old Doc C.
        5. Assert: Gen 1 remains completely untouched with all 3 historical documents.
        """
        from scripts.make_synthetic_pdf import build as make_pdf

        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_a")
        doc_b = _build_doc(corpus, "SA22-0000-01_b")
        doc_c = _build_doc(corpus, "SA22-0000-02_c")
        progress = tmp_path / "inv.jsonl"

        # Phase 1: Publish initial state
        assert _run_main(monkeypatch, corpus, progress) == 0
        gen1 = fake.aliases[ALIAS]
        gen1_docs = {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)}
        assert gen1_docs == {"SA22-0000-00", "SA22-0000-01", "SA22-0000-02"}
        gen1_points_count = len(fake.alias_target_points(ALIAS))

        # Phase 2: Delete Doc B and rebuild Doc C with different title/content
        doc_b.unlink()
        make_pdf(doc_c, doc_id="SA22-0000-02", title="Updated Doc C Reference")

        # Phase 3: Publish update
        assert _run_main(monkeypatch, corpus, progress) == 0
        gen2 = fake.aliases[ALIAS]
        assert gen2 != gen1, "Must cut over to new generation"

        # Phase 4: Active coverage verification
        active_points = fake.alias_target_points(ALIAS)
        active_doc_ids = {p.payload["doc_id"] for p in active_points}

        # Zero residue from deleted Doc B
        assert "SA22-0000-01" not in active_doc_ids, "Deleted Doc B must be absent from active coverage"
        assert active_doc_ids == {"SA22-0000-00", "SA22-0000-02"}

        # Gen 2 completion collection has no marker for deleted Doc B
        gen2_comp = fake.collections[f"{gen2}__completions"]
        comp_docs = {(p.payload or {}).get("doc_id") for p in gen2_comp}
        assert "SA22-0000-01" not in comp_docs

        # Phase 5: Gen 1 immutability verification
        assert len(fake.collections[gen1]) == gen1_points_count
        gen1_preserved_docs = {p.payload["doc_id"] for p in fake.collections[gen1]}
        assert gen1_preserved_docs == {"SA22-0000-00", "SA22-0000-01", "SA22-0000-02"}

    def test_unpurged_residue_blocks_cutover_and_preserves_serving_alias(self, tmp_path, monkeypatch):
        """If unpurged residue exists in staging after sweep, publication aborts fail-closed,
        and the serving alias remains strictly unchanged."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_a")
        progress = tmp_path / "inv.jsonl"

        # Initial clean publication
        assert _run_main(monkeypatch, corpus, progress) == 0
        serving_gen = fake.aliases[ALIAS]
        serving_points_count = len(fake.alias_target_points(ALIAS))

        # Adversarially hook sweep_unmarked_residue to inject an unpurged rogue point
        orig_sweep = run_ingest.sweep_unmarked_residue

        def _sweep_and_inject_malicious_residue(client, staging_settings, walked, inventory):
            res = orig_sweep(client, staging_settings, walked, inventory)
            stg = staging_settings.qdrant_collection
            fake.collections[stg].append(
                models.PointStruct(
                    id="deadbeef-0000-0000-0000-000000000001",
                    vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                    payload={
                        "doc_id": "INJECTED_ATTACK_RESIDUE",
                        "source_rev": "unauthorized_rev",
                        "sha256": "bad" * 21 + "b",
                        "rules_v": extraction_rules_version(),
                        "text": "malicious point",
                    },
                )
            )
            return res

        monkeypatch.setattr(run_ingest, "sweep_unmarked_residue", _sweep_and_inject_malicious_residue)

        # Force reingest must FAIL at publication gate
        with pytest.raises(RuntimeError, match="unmarked residue detected.*deadbeef-0000-0000-0000-000000000001"):
            _run_main(monkeypatch, corpus, progress, "--reingest")

        # Serving alias was NOT cut over
        assert fake.aliases[ALIAS] == serving_gen, "Serving alias must remain untouched on residue detection"
        assert len(fake.alias_target_points(ALIAS)) == serving_points_count
        assert all(p.payload["doc_id"] != "INJECTED_ATTACK_RESIDUE" for p in fake.alias_target_points(ALIAS))


# ============================================================================
# Group 6: Edge Cases & Attribution Boundaries
# ============================================================================


class TestEdgeCasesAndAttributionBoundary:
    """Stress-test edge cases, empty walked bootstrap, extra chunks under valid documents,
    and multi-document mass deletions."""

    def test_extra_chunk_injected_under_valid_doc_fails_publication_gate(self, monkeypatch):
        """If an attacker or corrupted pipeline injects an extra chunk under a valid
        (doc_id, source_rev, rules_v), verify_all_complete catches it via is_doc_complete."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-extra-chunk", batch_size=16)
        write_manifest(fake, "stg-extra-chunk__completions", staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-extra-chunk"] = []

        chunks = _chunks(doc_id="DOC_A", n=2)
        rev = source_rev_key("v", "p", "1", "a" * 64)
        _upsert_one(_parsed_doc("DOC_A", "a" * 64), chunks, _vectors(2),
                    staging, _DocLocks(), None, False, src_labels="||")

        # Now inject an EXTRA 3rd chunk under the exact same doc_id and source_rev
        fake.collections["stg-extra-chunk"].append(
            models.PointStruct(
                id="00000000-0000-0000-0000-000000000099",
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={
                    "doc_id": "DOC_A",
                    "source_rev": rev,
                    "sha256": "a" * 64,
                    "rules_v": rules_v,
                    "text": "extra unauthorized chunk",
                },
            )
        )

        inv = {
            "a.pdf": InventoryRecord(
                path="a.pdf", sha256="a" * 64, doc_id="DOC_A",
                status="upserted", rules_version=rules_v, source_rev=rev,
            ),
        }
        walked = [("a.pdf", "a" * 64)]

        problems = verify_all_complete(fake, staging, walked, inv, rules_v, "||")
        assert "a.pdf" in problems, "Extra chunk under valid doc must cause is_doc_complete verification failure"

    def test_audit_flags_all_points_when_walked_is_empty(self, monkeypatch):
        """When walked corpus is empty, any points in staging are flagged as unmarked residue."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-empty-walked", batch_size=16)
        fake.collections["stg-empty-walked"] = [
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector={"dense": [0.0] * 256},
                payload={"doc_id": "DOC_STRAY", "source_rev": "rev", "sha256": "x" * 64, "rules_v": "v1"},
            ),
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector={"dense": [0.0] * 256},
                payload={"doc_id": "DOC_STRAY2", "source_rev": "rev2", "sha256": "y" * 64, "rules_v": "v1"},
            ),
        ]

        problems = audit_unmarked_residue(fake, staging, [], {}, "v1")
        assert len(problems) == 1
        assert "unmarked residue detected (2 point(s)" in problems[0]

    def test_sweep_purges_multiple_deleted_documents_and_preserves_survivors(self, monkeypatch):
        """When 3 out of 6 documents are deleted from the corpus, sweep_unmarked_residue
        purges all points and completion markers for the deleted docs, leaving only surviving docs."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        rules_v = extraction_rules_version()
        staging = _settings(qdrant_collection="stg-mass-deletion", batch_size=16)
        comp_col = "stg-mass-deletion__completions"
        write_manifest(fake, comp_col, staging, rules_v, state=STATE_COMMITTED)
        fake.collections["stg-mass-deletion"] = []

        all_doc_ids = [f"DOC_{i}" for i in range(1, 7)]
        walked_doc_ids = {"DOC_1", "DOC_3", "DOC_5"}
        deleted_doc_ids = set(all_doc_ids) - walked_doc_ids

        inv: dict[str, InventoryRecord] = {}
        walked: list[tuple[str, str]] = []

        for d_id in all_doc_ids:
            sha = f"{d_id.lower()}_sha".ljust(64, "0")
            rev = source_rev_key("v", "p", "1", sha)
            chunks = _chunks(doc_id=d_id, n=2)
            _upsert_one(_parsed_doc(d_id, sha), chunks, _vectors(2),
                        staging, _DocLocks(), None, False, src_labels="||")

            path = f"{d_id}.pdf"
            inv[path] = InventoryRecord(
                path=path, sha256=sha, doc_id=d_id,
                status="upserted", rules_version=rules_v, source_rev=rev,
            )
            if d_id in walked_doc_ids:
                walked.append((path, sha))

        assert len(fake.collections["stg-mass-deletion"]) == 12  # 6 docs * 2 chunks
        assert len(fake.collections[comp_col]) == 7  # 6 markers + 1 manifest point

        swept = sweep_unmarked_residue(fake, staging, walked, inv)
        assert swept == 6  # 3 deleted docs * 2 chunks

        surviving_points = fake.collections["stg-mass-deletion"]
        assert len(surviving_points) == 6
        surviving_doc_ids = {(p.payload or {}).get("doc_id") for p in surviving_points}
        assert surviving_doc_ids == walked_doc_ids
        assert surviving_doc_ids.isdisjoint(deleted_doc_ids)

        comp_points = fake.collections[comp_col]
        surviving_comp_docs = {(p.payload or {}).get("doc_id") for p in comp_points}
        assert "DOC_2" not in surviving_comp_docs
        assert "DOC_4" not in surviving_comp_docs
        assert "DOC_6" not in surviving_comp_docs

        assert audit_unmarked_residue(fake, staging, walked, inv, rules_v) == []
        problems = verify_all_complete(fake, staging, walked, inv, rules_v, "||")
        assert problems == []

