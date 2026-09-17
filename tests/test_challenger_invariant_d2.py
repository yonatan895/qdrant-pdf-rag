"""Adversarial stress test suite challenging Invariant D2 (Publication Gate Metadata Validation).

Author: challenger_m1_2
Focus: Boundary conditions for verify_all_complete, _run_publish, and alias cutover safety.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.make_synthetic_pdf import build as make_pdf

from mainframe_rag.ingest import run_ingest
from mainframe_rag.ingest.completion import (
    completion_collection_name,
)
from mainframe_rag.ingest.identity import source_rev_key
from mainframe_rag.ingest.inventory import InventoryRecord
from mainframe_rag.ingest.publish import (
    verify_all_complete,
)
from mainframe_rag.ingest.representation import (
    STATE_COMMITTED,
    STATE_PENDING,
    build_manifest,
    manifest_point_id,
    read_manifest_record,
    write_manifest,
)
from mainframe_rag.ingest.rules_version import extraction_rules_version
from tests.test_ingest_publish import (
    ALIAS,
    PublishFake,
    _publish_env,
    _run_main,
    _settings,
)


def _build_doc_with_id(directory: Path, stem: str, doc_id: str = "SA22-0000-00") -> Path:
    out = directory / f"{stem}.pdf"
    make_pdf(out, doc_id=doc_id)
    return out


# ============================================================================
# Group 1: Manifest point exists but payload is unparseable JSON / corrupt / missing fields
# ============================================================================

class TestUnparseableOrCorruptManifest:
    """Stress test boundary conditions where the manifest point payload is missing or invalid."""

    def test_empty_payload_dict(self):
        """Manifest point payload is completely empty {}."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-empty-payload")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        fake.collections[completions] = [
            SimpleNamespace(id=manifest_point_id(completions), payload={})
        ]

        record = read_manifest_record(fake, completions)
        assert record is None

        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert "stg-empty-payload: missing or unreadable metadata manifest" in problems

    def test_wrong_record_type(self):
        """Manifest point payload has record_type != 'representation-manifest'."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-wrong-type")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        fake.collections[completions] = [
            SimpleNamespace(
                id=manifest_point_id(completions),
                payload={
                    "record_type": "completion-marker",  # wrong type
                    "manifest": build_manifest(staging, rules_v).model_dump(mode="json"),
                    "state": STATE_COMMITTED,
                },
            )
        ]

        assert read_manifest_record(fake, completions) is None
        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert "stg-wrong-type: missing or unreadable metadata manifest" in problems

    @pytest.mark.parametrize("bad_manifest", [
        "not a dict",
        12345,
        None,
        ["a", "b"],
        {"bad": "schema"},  # missing required fields like extraction_rules, embed_mode
    ])
    def test_invalid_manifest_payload(self, bad_manifest):
        """Manifest payload is non-dict or missing required Pydantic fields."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-bad-manifest")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        fake.collections[completions] = [
            SimpleNamespace(
                id=manifest_point_id(completions),
                payload={
                    "record_type": "representation-manifest",
                    "manifest": bad_manifest,
                    "state": STATE_COMMITTED,
                },
            )
        ]

        assert read_manifest_record(fake, completions) is None
        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert "stg-bad-manifest: missing or unreadable metadata manifest" in problems

    @pytest.mark.parametrize("bad_state", [
        123,
        {},
        [],
    ])
    def test_non_string_state(self, bad_state):
        """Manifest payload has a non-string state field (corrupt)."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-bad-state")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        fake.collections[completions] = [
            SimpleNamespace(
                id=manifest_point_id(completions),
                payload={
                    "record_type": "representation-manifest",
                    "manifest": build_manifest(staging, rules_v).model_dump(mode="json"),
                    "state": bad_state,
                },
            )
        ]

        # Non-string state reads as None (unparseable)
        assert read_manifest_record(fake, completions) is None
        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert "stg-bad-state: missing or unreadable metadata manifest" in problems

    def test_store_retrieve_exception_handled_as_unreadable(self):
        """Store retrieve raises an unexpected Exception (network error, timeout)."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-store-err")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)
        fake.collections[completions] = []

        def _exploding_retrieve(*args, **kwargs):
            raise RuntimeError("Database connection timed out")

        fake.retrieve = _exploding_retrieve

        assert read_manifest_record(fake, completions) is None
        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert "stg-store-err: missing or unreadable metadata manifest" in problems


# ============================================================================
# Group 2: Manifest point has non-committed states
# ============================================================================

class TestNonCommittedManifestStates:
    """Stress test manifest states other than STATE_COMMITTED ('committed')."""

    @pytest.mark.parametrize("state", [
        "pending",
        "aborted",
        "unknown",
        "failed",
        "in_progress",
        "COMMITTED",  # uppercase must not bypass
        "",            # empty string
        "partial",
    ])
    def test_non_committed_states_blocked(self, state):
        """Any state != 'committed' must block publication gate."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-states")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        write_manifest(fake, completions, staging, rules_v, state=state)

        record = read_manifest_record(fake, completions)
        assert record is not None
        assert record.state == state

        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert f"stg-states: contract {state!r}" in problems

    def test_non_committed_state_blocks_even_with_empty_walked(self):
        """Even if walked is empty, a non-committed contract must be reported as a problem."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-empty-pending")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        write_manifest(fake, completions, staging, rules_v, state="pending")

        problems = verify_all_complete(fake, staging, [], {}, rules_v, "||")
        assert problems == ["stg-empty-pending: contract 'pending'"]


# ============================================================================
# Group 3: Manifest has drift on rules_v or embedding dimension/model
# ============================================================================

class TestManifestRepresentationDrift:
    """Stress test drift detection across extraction rules and embedding coordinates."""

    def test_drift_on_extraction_rules(self):
        """Manifest extraction_rules version differs from current."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-drift-rules")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        # Write manifest with outdated rules version
        write_manifest(fake, completions, staging, "old_rules_v_1234", state=STATE_COMMITTED)

        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert any("stg-drift-rules: representation drift on extraction_rules" in p for p in problems)

    def test_drift_on_embed_model(self):
        """Manifest embed_model differs from staging settings."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-drift-model", embed_model="model-alpha")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        drift_settings = staging.model_copy(update={"embed_model": "model-beta"})
        write_manifest(fake, completions, drift_settings, rules_v, state=STATE_COMMITTED)

        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert any("stg-drift-model: representation drift on embed_model" in p for p in problems)

    def test_drift_on_dense_dim(self):
        """Manifest dense dimension differs (under vllm mode where dense_dim is configurable)."""
        fake = PublishFake()
        staging = _settings(
            qdrant_collection="stg-drift-dim",
            embed_mode="vllm",
            dense_dim=768,
            embed_model="model-x",
            embed_model_revision="rev1",
        )
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        drift_settings = staging.model_copy(update={"dense_dim": 1024})
        write_manifest(fake, completions, drift_settings, rules_v, state=STATE_COMMITTED)

        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert any("stg-drift-dim: representation drift on dense_dim" in p for p in problems)

    def test_drift_on_sparse_model_and_weights(self):
        """Manifest sparse model and weights revision differ."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-drift-sparse", bm25_model="bm25_v1", bm25_weights_revision="rev1")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        drift_settings = staging.model_copy(update={"bm25_model": "bm25_v2", "bm25_weights_revision": "rev2"})
        write_manifest(fake, completions, drift_settings, rules_v, state=STATE_COMMITTED)

        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert any("representation drift on sparse_model, sparse_weights_revision" in p for p in problems)

    def test_drift_on_multiple_dimensions_simultaneously(self):
        """Drift on extraction_rules, embed_model, and dense_dim at the same time under vllm mode."""
        fake = PublishFake()
        staging = _settings(
            qdrant_collection="stg-multi-drift",
            embed_mode="vllm",
            embed_model="model-a",
            embed_model_revision="rev1",
            dense_dim=512,
        )
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        drift_settings = staging.model_copy(update={"embed_model": "model-b", "dense_dim": 768})
        write_manifest(fake, completions, drift_settings, "other_rules_v", state=STATE_COMMITTED)

        problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
        assert any(
            "stg-multi-drift: representation drift on extraction_rules, embed_model, dense_dim" in p
            for p in problems
        )

    def test_record_only_drift_does_not_block_publication(self):
        """dense_query_prefix is a record-only drift and MUST NOT block publication gate."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-record-only", dense_query_prefix="PREFIX_A: ")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        # Write manifest with different prefix
        drift_settings = staging.model_copy(update={"dense_query_prefix": "PREFIX_B: "})
        write_manifest(fake, completions, drift_settings, rules_v, state=STATE_COMMITTED)

        # verify_all_complete should accept RECORD_ONLY_DRIFT without appending to problems
        problems = verify_all_complete(fake, staging, [], {}, rules_v, "||")
        assert problems == []


# ============================================================================
# Group 4: Corpus sizing boundaries (empty vs single vs multi-document)
# ============================================================================

class TestCorpusSizingBoundaries:
    """Stress test verify_all_complete and _run_publish under varying corpus sizes."""

    def test_empty_corpus_verify_all_complete_allows_bootstrap_if_no_manifest(self):
        """walked == [] allows missing manifest for initial bootstrap."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-bootstrap")
        rules_v = extraction_rules_version()

        problems = verify_all_complete(fake, staging, [], {}, rules_v, "||")
        assert problems == []

    def test_empty_corpus_run_publish_refuses_fail_closed(self, tmp_path, monkeypatch):
        """_run_publish refuses empty corpus at invocation boundary with explicit message."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
        empty_corpus = tmp_path / "empty_corpus"
        empty_corpus.mkdir()

        with pytest.raises(RuntimeError, match="refuses an empty corpus"):
            _run_main(monkeypatch, empty_corpus, tmp_path / "inv.jsonl")

        assert fake.aliases == {}, "empty corpus run must never create an alias"

    def test_single_document_corpus_fails_gate_on_uncommitted_manifest(self, tmp_path, monkeypatch):
        """Single document corpus with uncommitted manifest blocks publication."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        # Monkeypatch commit_manifest to leave manifest in 'pending' state
        def _fake_commit_as_pending(client, completions_collection, settings, rules_v):
            return write_manifest(client, completions_collection, settings, rules_v, state=STATE_PENDING)

        monkeypatch.setattr(run_ingest, "commit_manifest", _fake_commit_as_pending)

        with pytest.raises(RuntimeError, match="contract 'pending'"):
            _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")

        assert fake.aliases == {}, "Alias cutover must not occur on pending manifest"

    def test_multi_document_corpus_manifest_and_doc_errors_coexist(self):
        """Multi-document corpus reports both manifest problem AND incomplete document problems."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-multi")
        rules_v = extraction_rules_version()

        # Do NOT write manifest
        walked = [
            ("doc1.pdf", "1" * 64),
            ("doc2.pdf", "2" * 64),
            ("doc3.pdf", "3" * 64),
        ]
        inv = {
            "doc1.pdf": InventoryRecord(
                path="doc1.pdf", sha256="1" * 64, doc_id="D1",
                status="upserted", rules_version=rules_v,
                source_rev=source_rev_key("v", "p", "1", "1" * 64),
            ),
            # doc2 is missing from inventory
            # doc3 has wrong sha
            "doc3.pdf": InventoryRecord(
                path="doc3.pdf", sha256="wrong_sha", doc_id="D3",
                status="upserted", rules_version=rules_v,
                source_rev=source_rev_key("v", "p", "1", "3" * 64),
            ),
        }

        problems = verify_all_complete(fake, staging, walked, inv, rules_v, "||")
        assert "stg-multi: missing or unreadable metadata manifest" in problems
        assert "doc2.pdf" in problems
        assert "doc3.pdf" in problems
        assert len(problems) == 4  # manifest + doc1 (not in store) + doc2 (not in inv) + doc3 (wrong sha)


# ============================================================================
# Group 5: Bypass and failure resistance in alias cutover
# ============================================================================

class TestAliasCutoverBypassResistance:
    """Stress test all potential bypasses or failure paths before swap_alias_to."""

    def test_cutover_impossible_when_verify_all_complete_fails(self, tmp_path, monkeypatch):
        """Any problem returned by verify_all_complete blocks swap_alias_to."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        # Inject injected problem into verify_all_complete
        monkeypatch.setattr(
            run_ingest,
            "verify_all_complete",
            lambda *a, **k: ["synthetic-failure-item"],
        )

        with pytest.raises(RuntimeError, match="alias untouched"):
            _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")

        assert fake.aliases == {}

    def test_cutover_impossible_when_run_impl_returns_error(self, tmp_path, monkeypatch):
        """If _run_impl fails (returns rc != 0), cutover never executes."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        monkeypatch.setattr(run_ingest, "_run_impl", lambda *a, **k: 2)

        rc = _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")
        assert rc == 2
        assert fake.aliases == {}

    def test_cutover_with_limit_flag_refused_fail_closed(self, tmp_path, monkeypatch):
        """Passing --limit is rejected immediately without touching aliases."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        with pytest.raises(RuntimeError, match="INGEST_ALIAS_PUBLISH refuses --limit"):
            _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl", "--limit", "1")

        assert fake.aliases == {}

    def test_cutover_with_stem_collision_fails_closed_before_staging_created(self, tmp_path, monkeypatch):
        """Planned entry collision fails closed at _gate_planned_entries before staging is cloned."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "SA22-0000-00", "SA22-0000-00")
        # Same stem in subdirectory
        sub = corpus / "sub"
        sub.mkdir()
        _build_doc_with_id(sub, "SA22-0000-00", "SA22-0000-00")

        with pytest.raises(RuntimeError, match="revision collision"):
            _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")

        assert fake.aliases == {}
        assert not any("__gen" in c for c in fake.collections)

    def test_atomic_swap_failure_leaves_previous_live_serving(self, tmp_path, monkeypatch):
        """If swap_alias_to fails during Qdrant update_collection_aliases, error is raised."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        # First publish succeeds
        assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
        gen1 = fake.aliases[ALIAS]
        assert gen1 in fake.collections

        # Second publish with distinct doc_id injects swap failure
        _build_doc_with_id(corpus, "DOC2", "SA22-0000-01")
        fake.fail_swap = "raise"

        with pytest.raises(RuntimeError, match="injected swap failure"):
            _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")

        # Verify alias still points to gen1
        assert fake.aliases[ALIAS] == gen1

    def test_steady_state_fails_if_manifest_dropped(self, tmp_path, monkeypatch):
        """Steady-state re-verification fails-closed if live generation's manifest is deleted."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        # First publish succeeds
        assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
        live = fake.aliases[ALIAS]

        # Delete manifest point from completions collection
        completions = f"{live}__completions"
        mp_id = manifest_point_id(completions)
        fake.collections[completions] = [
            p for p in fake.collections.get(completions, []) if str(p.id) != mp_id
        ]

        # Rerun in steady state (same inputs -> live == staging)
        with pytest.raises(RuntimeError, match="predates the representation manifest"):
            _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")

    def test_steady_state_fails_if_manifest_marked_pending(self, tmp_path, monkeypatch):
        """Steady-state re-verification fails-closed if live manifest is in pending state."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        # First publish succeeds
        assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
        live = fake.aliases[ALIAS]

        # Set manifest to pending
        staging_settings = _settings(qdrant_collection=live)
        rules_v = extraction_rules_version()
        write_manifest(fake, f"{live}__completions", staging_settings, rules_v, state=STATE_PENDING)

        # Rerun in steady state
        with pytest.raises(RuntimeError, match="unfinished representation migration"):
            _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")

    def test_force_reingest_fails_if_contract_corrupt(self, tmp_path, monkeypatch):
        """Forced in-place reconverge refuses when live carries no readable contract."""
        _publish_env(monkeypatch)
        fake = PublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        # First publish succeeds
        assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
        live = fake.aliases[ALIAS]

        # Corrupt manifest
        completions = f"{live}__completions"
        mp_id = manifest_point_id(completions)
        fake.collections[completions] = [
            p for p in fake.collections.get(completions, []) if str(p.id) != mp_id
        ]

        # Rerun with --reingest
        with pytest.raises(RuntimeError, match="refusing to reconverge.*no readable contract"):
            _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl", "--reingest")


# ============================================================================
# Group 6: Collection Index Creation and Payload Schema Alignment at Publication Gate
# ============================================================================

class IndexTrackingPublishFake(PublishFake):
    """PublishFake extension tracking create_payload_index calls per collection
    and supporting index failure injection."""

    def __init__(self, dim: int = 256):
        super().__init__(dim=dim)
        self.created_indexes: dict[str, list[tuple[str, object]]] = {}
        self.fail_index_fields: set[str] = set()

    def create_payload_index(self, collection_name, field_name, field_schema, **kwargs):
        if field_name in self.fail_index_fields:
            raise RuntimeError(f"injected index creation failure for {field_name}")
        self.created_indexes.setdefault(collection_name, []).append((field_name, field_schema))
        return SimpleNamespace()


class TestCollectionIndexCreationAndPublicationGate:
    """Stress test the publication gate against the updated indexed field set (including 'source')."""

    def test_publish_creates_all_11_payload_indexes_on_fresh_staging(self, tmp_path, monkeypatch):
        """Fresh corpus publication creates all 11 payload indexes (10 keywords + page_start)
        on the staging collection before alias swap."""
        from qdrant_client import models

        from mainframe_rag.ingest.qdrant_io import _KEYWORD_INDEXES

        _publish_env(monkeypatch)
        fake = IndexTrackingPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        rc = _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")
        assert rc == 0

        # Physical staging collection that was swapped to alias
        live_gen = fake.aliases[ALIAS]
        assert live_gen in fake.created_indexes

        indexes = dict(fake.created_indexes[live_gen])
        # Verify all 10 keywords are indexed
        for kw in _KEYWORD_INDEXES:
            assert kw in indexes, f"keyword index '{kw}' missing on {live_gen}"
            assert indexes[kw] == models.PayloadSchemaType.KEYWORD
        # Verify 'source' is specifically indexed
        assert "source" in indexes
        assert indexes["source"] == models.PayloadSchemaType.KEYWORD
        # Verify 'page_start' integer index
        assert indexes["page_start"] == models.PayloadSchemaType.INTEGER
        # Total index count on staging collection must be exactly 11
        assert len(indexes) == 11

    def test_publish_aborts_and_keeps_alias_untouched_on_index_creation_failure(self, tmp_path, monkeypatch):
        """If payload index creation fails on staging (e.g. for 'source'),
        the publication gate fails closed and the alias remains untouched."""
        _publish_env(monkeypatch)
        fake = IndexTrackingPublishFake()
        fake.fail_index_fields.add("source")
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        with pytest.raises(RuntimeError, match="injected index creation failure for source"):
            _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")

        assert fake.aliases == {}, "Alias cutover must not occur if payload index creation fails"

    def test_cloned_staging_heals_missing_source_index_from_legacy_live(self, tmp_path, monkeypatch):
        """When staging is cloned from a live collection that lacked the 'source' index,
        ensure_collection on staging ensures the missing 'source' index is created before cutover."""
        from qdrant_client import models

        _publish_env(monkeypatch)
        fake = IndexTrackingPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        # Initial publication succeeds
        assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
        gen1 = fake.aliases[ALIAS]

        # Simulate that gen1 was created before 'source' was in _KEYWORD_INDEXES by removing 'source'
        fake.created_indexes[gen1] = [
            (k, v) for k, v in fake.created_indexes[gen1] if k != "source"
        ]
        assert not any(k == "source" for k, _ in fake.created_indexes[gen1])

        # Publish second document (cloning gen1 -> gen2)
        _build_doc_with_id(corpus, "DOC2", "SA22-0000-01")
        assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
        gen2 = fake.aliases[ALIAS]
        assert gen2 != gen1

        # Gen2 staging must have 'source' index created
        gen2_indexes = dict(fake.created_indexes[gen2])
        assert "source" in gen2_indexes
        assert gen2_indexes["source"] == models.PayloadSchemaType.KEYWORD
        assert len(gen2_indexes) == 11

    def test_verify_all_complete_validates_chunks_with_source_payload(self, tmp_path, monkeypatch):
        """verify_all_complete succeeds when staging points carry the new 'source' payload key."""
        _publish_env(monkeypatch)
        fake = IndexTrackingPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc_with_id(corpus, "DOC1", "SA22-0000-00")

        inv_path = tmp_path / "inv.jsonl"
        assert _run_main(monkeypatch, corpus, inv_path, "--vendor", "ibm") == 0

        live_gen = fake.aliases[ALIAS]
        points = fake.collections[live_gen]
        assert len(points) > 0

        # Verify every chunk point payload carries 'source' and 'vendor' matching the specified vendor
        for p in points:
            payload = p.payload or {}
            assert payload.get("source") == "ibm"
            assert payload.get("vendor") == "ibm"
            assert payload.get("source") == payload.get("vendor")


    def test_verify_all_complete_fails_gate_on_mismatched_rules_or_sha(self):
        """verify_all_complete rejects document if point payload sha256 or rules_v does not match."""
        fake = PublishFake()
        staging = _settings(qdrant_collection="stg-corrupt-point")
        rules_v = extraction_rules_version()
        completions = completion_collection_name(staging)

        write_manifest(fake, completions, staging, rules_v, state=STATE_COMMITTED)

        src_rev = source_rev_key("ibm", "z/OS", "3.1", "1" * 64)
        # Point with mismatched sha256
        fake.collections[staging.qdrant_collection] = [
            SimpleNamespace(
                id="c1",
                payload={
                    "sha256": "wrong_sha" + "0" * 55,
                    "rules_v": rules_v,
                    "source_rev": src_rev,
                    "source": "ibm",
                    "vendor": "ibm",
                    "text": "sample",
                    "doc_id": "DOC1",
                },
            )
        ]

        inv = {
            "doc1.pdf": InventoryRecord(
                path="doc1.pdf",
                sha256="1" * 64,
                doc_id="DOC1",
                status="upserted",
                rules_version=rules_v,
                source_rev=src_rev,
            )
        }

        problems = verify_all_complete(
            fake, staging, [("doc1.pdf", "1" * 64)], inv, rules_v, "vendor=ibm||product=z/OS||version=3.1"
        )
        assert "doc1.pdf" in problems

    def test_ensure_collection_recovers_missing_indexes_on_existing_staging(self):
        """ensure_collection on an existing collection ensures all 11 indexes are registered."""
        from qdrant_client import models

        from mainframe_rag.ingest.qdrant_io import ensure_collection

        fake = IndexTrackingPublishFake(dim=256)
        collection = "existing_col"
        fake.collections[collection] = []

        staging_settings = _settings(qdrant_collection=collection, dense_dim=256)
        ensure_collection(fake, staging_settings)

        indexes = dict(fake.created_indexes[collection])
        assert len(indexes) == 11
        assert "source" in indexes
        assert indexes["source"] == models.PayloadSchemaType.KEYWORD
        assert indexes["page_start"] == models.PayloadSchemaType.INTEGER

    def test_cross_layer_all_retrieve_filter_keys_are_indexed_in_qdrant_io(self):
        """Cross-layer contract assertion: all query filter keys extracted from
        retrieve/filters.py must exist in ingest/qdrant_io._KEYWORD_INDEXES."""
        import inspect
        import re

        from mainframe_rag.ingest import qdrant_io
        from mainframe_rag.retrieve import filters

        source = inspect.getsource(filters)
        filter_keys = set(re.findall(r'key="(\w+)"', source))
        assert "source" in filter_keys, "'source' must be directly used as a filter key"

        indexed_keywords = set(qdrant_io._KEYWORD_INDEXES)
        assert "source" in indexed_keywords, "'source' must be in _KEYWORD_INDEXES"

        missing = filter_keys - indexed_keywords
        assert not missing, f"Filter keys not indexed: {missing}"


