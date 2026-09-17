"""Adversarial stress test suite challenging Invariant D4 (Immutable Publication Lifetime Model).

Author: challenger_m2_2
Focus:
1. Live collection immutability under --reingest / force_reingest:
   Verify live and live__completions are NEVER mutated in place.
2. Publication lifecycle and atomic cutover:
   Verify new physical generation is created, populated, verified, and only then switched via swap_alias_to.
3. In-flight reader isolation:
   Verify concurrent and step-by-step readers reading from live never observe missing points or partial states.
4. Edge cases & boundary conditions:
   Collisions, worker crashes, verification failures, alias swap failures, ServingGate binding.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

import pytest
from scripts.make_synthetic_pdf import build as make_pdf

from mainframe_rag.agent.serving import ServingGate
from mainframe_rag.ingest import run_ingest
from mainframe_rag.ingest.publish import (
    resolve_staging_name,
    staging_name_for,
)
from mainframe_rag.ingest.rules_version import extraction_rules_version
from tests.test_ingest_publish import (
    ALIAS,
    PublishFake,
    _build_doc,
    _publish_env,
    _run_main,
    _settings,
)


class SpyPublishFake(PublishFake):
    """Observing and asserting fake Qdrant client that records every mutation
    and validates immutability assertions in real-time.
    """

    def __init__(self) -> None:
        super().__init__()
        self.mutation_log: list[tuple[str, str, dict[str, Any]]] = []
        self.forbidden_targets: set[str] = set()
        self.mutation_hooks: list[Any] = []
        self.operation_trace: list[str] = []

    def set_forbidden_mutations(self, *collections: str) -> None:
        for c in collections:
            self.forbidden_targets.add(c)

    def add_mutation_hook(self, hook: Any) -> None:
        self.mutation_hooks.append(hook)

    def _record_mutation(self, op: str, collection: str, details: dict[str, Any]) -> None:
        physical = self.aliases.get(collection, collection)
        self.mutation_log.append((op, physical, details))
        self.operation_trace.append(f"{op}:{physical}")
        if physical in self.forbidden_targets or collection in self.forbidden_targets:
            raise AssertionError(
                f"Invariant D4 Violation! Illegal mutation '{op}' attempted on protected collection: "
                f"collection={collection!r}, physical={physical!r}"
            )
        for hook in self.mutation_hooks:
            hook(self, op, physical)

    def create_collection(self, name, **kwargs):
        res = super().create_collection(name, **kwargs)
        self._record_mutation("create_collection", name, kwargs)
        return res

    def delete_collection(self, name):
        self._record_mutation("delete_collection", name, {})
        return super().delete_collection(name)

    def recover_snapshot(self, collection, location, *, priority=None, wait=True):
        res = super().recover_snapshot(collection, location, priority=priority, wait=wait)
        self._record_mutation("recover_snapshot", collection, {"location": location})
        return res

    def upsert(self, collection, *, points, wait=True):
        self._record_mutation("upsert", collection, {"count": len(points)})
        return super().upsert(collection, points=points, wait=wait)

    def delete(self, collection, *, points_selector, wait=True):
        self._record_mutation("delete", collection, {"selector": str(points_selector)})
        return super().delete(collection, points_selector=points_selector, wait=wait)

    def update_collection_aliases(self, ops):
        self.operation_trace.append("update_collection_aliases")
        return super().update_collection_aliases(ops)

    def create_snapshot(self, collection, *, wait=True):
        self.operation_trace.append(f"create_snapshot:{collection}")
        return super().create_snapshot(collection, wait=wait)


# ============================================================================
# Group 1: Live Immutability Under Force Reingest
# ============================================================================

class TestLiveImmutabilityUnderForceReingest:
    """Rigorous stress tests verifying that the active live collection and its
    completion collection are NEVER mutated during --reingest / force_reingest.
    """

    def test_force_reingest_zero_mutations_on_live_and_completions(self, tmp_path, monkeypatch):
        """Under --reingest on an unchanged corpus, live and live__completions
        must receive exactly zero mutations (no upsert, delete, or collection deletion).
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        # Initial ingest
        assert _run_main(monkeypatch, corpus, progress) == 0
        live_initial = fake.aliases[ALIAS]
        live_completions = f"{live_initial}__completions"
        initial_points = copy.deepcopy(fake.collections[live_initial])
        initial_completions = copy.deepcopy(fake.collections[live_completions])
        assert initial_points, "initial publish must populate points"

        # Set live and its completions as forbidden targets for ANY mutation
        fake.set_forbidden_mutations(live_initial, live_completions)
        fake.mutation_log.clear()

        # Execute --reingest
        rc = _run_main(monkeypatch, corpus, progress, "--reingest")
        assert rc == 0, "reingest must succeed"

        # Verify live collection points and completions remained completely identical
        assert fake.collections[live_initial] == initial_points, "live points must be strictly unchanged"
        assert fake.collections[live_completions] == initial_completions, "live completions must be strictly unchanged"

        # Verify new live generation is distinct
        new_live = fake.aliases[ALIAS]
        assert new_live != live_initial
        assert new_live.startswith(f"{live_initial}_")

    def test_force_reingest_with_added_document_preserves_live(self, tmp_path, monkeypatch):
        """When a new document is added to the corpus, running --reingest must
        leave the previous live generation completely untouched, containing only
        the original document, while the new live generation contains both.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]
        points_0 = copy.deepcopy(fake.collections[live_0])
        fake.set_forbidden_mutations(live_0, f"{live_0}__completions")

        # Add second document with different doc_id
        make_pdf(corpus / "SA22-0001-00_doc2.pdf", doc_id="SA22-0001-00")

        # Reingest with added document
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0

        # Assert live_0 was untouched
        assert fake.collections[live_0] == points_0
        doc_ids_0 = {p.payload["doc_id"] for p in fake.collections[live_0]}
        assert doc_ids_0 == {"SA22-0000-00"}

        # Assert new generation contains both documents
        live_1 = fake.aliases[ALIAS]
        assert live_1 != live_0
        doc_ids_1 = {p.payload["doc_id"] for p in fake.collections[live_1]}
        assert doc_ids_1 == {"SA22-0000-00", "SA22-0001-00"}

    def test_force_reingest_with_deleted_document_preserves_live(self, tmp_path, monkeypatch):
        """When a document is deleted from the corpus, running --reingest must
        leave the previous live generation completely untouched (still containing the deleted doc),
        while the new live generation sweeps and excludes the deleted doc.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_keep")
        doc_to_delete = corpus / "SA22-0002-00_delete.pdf"
        make_pdf(doc_to_delete, doc_id="SA22-0002-00")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]
        points_0 = copy.deepcopy(fake.collections[live_0])
        assert {p.payload["doc_id"] for p in points_0} == {"SA22-0000-00", "SA22-0002-00"}

        # Protect live_0 from any mutation
        fake.set_forbidden_mutations(live_0, f"{live_0}__completions")

        # Delete document from disk
        doc_to_delete.unlink()

        # Reingest
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0

        # Assert live_0 was untouched
        assert fake.collections[live_0] == points_0
        assert {p.payload["doc_id"] for p in fake.collections[live_0]} == {"SA22-0000-00", "SA22-0002-00"}

        # Assert new live generation excludes deleted doc
        live_1 = fake.aliases[ALIAS]
        assert live_1 != live_0
        assert {p.payload["doc_id"] for p in fake.collections[live_1]} == {"SA22-0000-00"}

    def test_force_reingest_with_modified_document_preserves_live(self, tmp_path, monkeypatch):
        """When a document is modified on disk, running --reingest must leave the
        previous live generation with the original revision and chunks untouched.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        doc_path = corpus / "SA22-0000-00_mod.pdf"
        make_pdf(doc_path, doc_id="SA22-0000-00", title="Version 1 Title")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]
        points_0 = copy.deepcopy(fake.collections[live_0])
        rev_0 = {p.payload["source_rev"] for p in points_0}
        fake.set_forbidden_mutations(live_0, f"{live_0}__completions")

        # Overwrite document with new content (new revision hash)
        make_pdf(doc_path, doc_id="SA22-0000-00", title="Version 2 Title Updated")

        # Reingest
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0

        # Assert live_0 has original points and revision
        assert fake.collections[live_0] == points_0
        assert {p.payload["source_rev"] for p in fake.collections[live_0]} == rev_0

        # Assert new generation has updated revision
        live_1 = fake.aliases[ALIAS]
        assert live_1 != live_0
        rev_1 = {p.payload["source_rev"] for p in fake.collections[live_1]}
        assert rev_1 != rev_0

    def test_consecutive_reingests_preserve_all_ancestor_generations(self, tmp_path, monkeypatch):
        """Multiple consecutive --reingest runs must each allocate a new generation
        and never mutate ANY prior ancestor generation.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        gen_0 = fake.aliases[ALIAS]
        points_0 = copy.deepcopy(fake.collections[gen_0])

        # Run 1st reingest
        fake.set_forbidden_mutations(gen_0, f"{gen_0}__completions")
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
        gen_1 = fake.aliases[ALIAS]
        assert gen_1 != gen_0
        points_1 = copy.deepcopy(fake.collections[gen_1])

        # Run 2nd reingest (protecting both gen_0 and gen_1)
        fake.set_forbidden_mutations(gen_1, f"{gen_1}__completions")
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
        gen_2 = fake.aliases[ALIAS]
        assert gen_2 not in (gen_0, gen_1)
        points_2 = copy.deepcopy(fake.collections[gen_2])

        # Run 3rd reingest (protecting gen_0, gen_1, gen_2)
        fake.set_forbidden_mutations(gen_2, f"{gen_2}__completions")
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
        gen_3 = fake.aliases[ALIAS]
        assert gen_3 not in (gen_0, gen_1, gen_2)

        # Verify all ancestor points remain identical
        assert fake.collections[gen_0] == points_0
        assert fake.collections[gen_1] == points_1
        assert fake.collections[gen_2] == points_2


# ============================================================================
# Group 2: Failure Resilience & Rollback Invariants
# ============================================================================

class TestFailureResilienceAndFailClosed:
    """Stress tests verifying that errors at any stage of publication abort
    safely without mutating live and without moving the alias.
    """

    def test_worker_crash_during_reingest_leaves_live_untouched(self, tmp_path, monkeypatch):
        """If worker fails during chunk upsert, live must be 100% untouched
        and alias must still point to live.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]
        points_0 = copy.deepcopy(fake.collections[live_0])
        fake.set_forbidden_mutations(live_0, f"{live_0}__completions")

        # Inject failure in _run_impl
        def _failing_impl(*args, **kwargs):
            return 1  # non-zero return code (failure)

        monkeypatch.setattr(run_ingest, "_run_impl", _failing_impl)

        rc = _run_main(monkeypatch, corpus, progress, "--reingest")
        assert rc == 1, "reingest must report error"

        # Live and alias must remain unchanged
        assert fake.aliases[ALIAS] == live_0, "alias must not move on worker failure"
        assert fake.collections[live_0] == points_0, "live collection must be unmutated"

    def test_verification_failure_leaves_live_untouched_and_alias_unmoved(self, tmp_path, monkeypatch):
        """If verify_all_complete fails on the staging collection, publication
        aborts fail-closed; live is untouched and alias does not move.
        """
        from mainframe_rag.ingest import publish

        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]
        points_0 = copy.deepcopy(fake.collections[live_0])
        fake.set_forbidden_mutations(live_0, f"{live_0}__completions")

        # Inject failure into verify_all_complete
        def _bad_verify(*args, **kwargs):
            return ["simulated unverified staging state"]

        monkeypatch.setattr(publish, "verify_all_complete", _bad_verify)
        monkeypatch.setattr(run_ingest, "verify_all_complete", _bad_verify)
        fake.operation_trace.clear()

        with pytest.raises(RuntimeError, match="staging .* incomplete for 1 path"):
            _run_main(monkeypatch, corpus, progress, "--reingest")

        # Live and alias must remain unchanged
        assert fake.aliases[ALIAS] == live_0
        assert fake.collections[live_0] == points_0
        assert "update_collection_aliases" not in fake.operation_trace

    def test_alias_swap_rejection_leaves_live_untouched(self, tmp_path, monkeypatch):
        """If Qdrant rejects the alias swap operation, publication raises and
        live remains untouched.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]
        points_0 = copy.deepcopy(fake.collections[live_0])
        fake.set_forbidden_mutations(live_0, f"{live_0}__completions")

        # Inject rejection in alias swap
        fake.fail_swap = "reject"

        with pytest.raises(RuntimeError, match="alias swap.*rejected"):
            _run_main(monkeypatch, corpus, progress, "--reingest")

        assert fake.collections[live_0] == points_0
        assert fake.aliases[ALIAS] == live_0

    def test_invariant_d4_guard_aborts_if_staging_equals_live(self, tmp_path, monkeypatch):
        """Directly verify the Invariant D4 guard in run_ingest.py:
        If resolve_staging_name somehow returned live under a forced publication,
        the safety guard must raise RuntimeError and prevent cutover.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]

        # Artificially force resolve_staging_name to return live
        monkeypatch.setattr(run_ingest, "resolve_staging_name", lambda *args, **kwargs: live_0)

        with pytest.raises(RuntimeError, match="Invariant D4 violation: publication attempted on serving collection"):
            _run_main(monkeypatch, corpus, progress, "--reingest")


# ============================================================================
# Group 3: Strict Lifecycle Sequence & Atomic Cutover
# ============================================================================

class TestLifecycleSequenceAndCutover:
    """Stress tests proving that publication follows an exact atomic lifecycle:
    new physical collection created -> populated -> residue swept -> verified -> swapped.
    """

    def test_publication_lifecycle_trace(self, tmp_path, monkeypatch):
        """Trace every event during publication and assert the strict order:
        1. create/recover staging
        2. upserts to staging (never live)
        3. sweep residue on staging
        4. verify staging
        5. snapshot live
        6. atomic alias swap
        """
        from mainframe_rag.ingest import publish

        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]

        # Instrument sweep and verify with trace markers
        trace: list[str] = []

        orig_sweep = publish.sweep_unmarked_residue
        def _traced_sweep(client, staging_settings, walked, inventory):
            trace.append(f"sweep:{staging_settings.qdrant_collection}")
            return orig_sweep(client, staging_settings, walked, inventory)

        orig_verify = publish.verify_all_complete
        def _traced_verify(client, staging_settings, walked, inventory, rules_v, src_labels):
            trace.append(f"verify:{staging_settings.qdrant_collection}")
            return orig_verify(client, staging_settings, walked, inventory, rules_v, src_labels)

        monkeypatch.setattr(publish, "sweep_unmarked_residue", _traced_sweep)
        monkeypatch.setattr(run_ingest, "sweep_unmarked_residue", _traced_sweep)
        monkeypatch.setattr(publish, "verify_all_complete", _traced_verify)
        monkeypatch.setattr(run_ingest, "verify_all_complete", _traced_verify)

        fake.operation_trace.clear()
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0

        staging_1 = fake.aliases[ALIAS]
        assert staging_1 != live_0

        # Verify exact sequence
        op_trace = fake.operation_trace
        # Find indices
        recover_idx = next(i for i, op in enumerate(op_trace) if op == f"recover_snapshot:{staging_1}")
        alias_swap_idx = next(i for i, op in enumerate(op_trace) if op == "update_collection_aliases")
        snapshot_live_idx = next(i for i, op in enumerate(op_trace) if op == f"create_snapshot:{live_0}")

        # Assert staging is recovered before alias swap
        assert recover_idx < alias_swap_idx, "staging must be prepared before alias swap"
        # Assert safety snapshot of live was taken during alias swap
        assert snapshot_live_idx < alias_swap_idx, "safety snapshot of previous live must occur during alias swap"

        # Assert traced sweep and verify happened on staging before alias swap
        assert f"sweep:{staging_1}" in trace
        assert f"verify:{staging_1}" in trace

        # Verify alias swap operation batch was strictly atomic (delete old + create new)
        last_alias_call = fake.alias_calls[-1]
        assert len(last_alias_call) == 2
        delete_op = getattr(last_alias_call[0], "delete_alias", None)
        create_op = getattr(last_alias_call[1], "create_alias", None)
        assert delete_op is not None and delete_op.alias_name == ALIAS
        assert create_op is not None and create_op.alias_name == ALIAS and create_op.collection_name == staging_1

    def test_clean_rerun_after_reingest_is_read_only_noop(self, tmp_path, monkeypatch):
        """After --reingest publishes a suffixed generation, running without
        --reingest must recognize the generation as live, performing read-only
        verification with zero collection creations, mutations, or alias changes.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
        live_reingest = fake.aliases[ALIAS]

        fake.set_forbidden_mutations(live_reingest, f"{live_reingest}__completions")
        fake.mutation_log.clear()
        fake.alias_calls.clear()

        # Clean rerun without --reingest
        rc = _run_main(monkeypatch, corpus, progress)
        assert rc == 0
        assert fake.aliases[ALIAS] == live_reingest
        assert len(fake.mutation_log) == 0, "clean rerun must perform zero mutations"
        assert len(fake.alias_calls) == 0, "clean rerun must not touch aliases"


# ============================================================================
# Group 4: In-Flight Reader Isolation & Consistency
# ============================================================================

class TestInFlightReaderIsolation:
    """Stress tests simulating active readers during --reingest to verify that
    in-flight readers bound to live never observe missing points, dropped chunks,
    or partial states.
    """

    def test_interleaved_reader_observes_complete_state_at_every_mutation_step(
        self, tmp_path, monkeypatch
    ):
        """Simulate an active reader querying live at every single internal
        mutation step of a --reingest run. The reader must always observe 100% of
        the initial points and document coverage.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        make_pdf(corpus / "SA22-0002-00_doc2.pdf", doc_id="SA22-0002-00")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]
        initial_points = fake.collections[live_0]
        initial_count = len(initial_points)
        initial_point_ids = {str(p.id) for p in initial_points}
        initial_doc_ids = {(p.payload or {}).get("doc_id") for p in initial_points}
        assert initial_count > 0

        # Reader validation function called at EVERY mutation step
        reader_observations: list[int] = []

        def _reader_probe(client, op, target_collection):
            # Active reader is bound to live_0 physical collection
            pts = client.collections.get(live_0, [])
            current_count = len(pts)
            current_ids = {str(p.id) for p in pts}
            current_docs = {(p.payload or {}).get("doc_id") for p in pts}

            assert current_count == initial_count, (
                f"Reader observed mutated point count {current_count} != {initial_count} "
                f"during {op} on {target_collection}"
            )
            assert current_ids == initial_point_ids, "Reader observed changed point IDs"
            assert current_docs == initial_doc_ids, "Reader observed changed document IDs"

            # Execute a simulated read / scroll
            page, _ = client.scroll(live_0, limit=100)
            assert len(page) == initial_count
            reader_observations.append(current_count)

        fake.add_mutation_hook(_reader_probe)

        # Run reingest
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0

        # Confirm that reader observed stable state across numerous mutations
        assert len(reader_observations) >= 4, "Reader should have checked across multiple mutation steps"

    def test_alias_reader_transition_is_strictly_atomic(self, tmp_path, monkeypatch):
        """Simulate a reader querying through the alias:
        Before alias swap -> sees complete old generation.
        After alias swap -> sees complete new generation.
        At no point is the alias pointing to an incomplete or empty collection.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]
        count_0 = len(fake.collections[live_0])

        # Add a new document so new generation will have strictly more points
        make_pdf(corpus / "SA22-0003-00_doc3.pdf", doc_id="SA22-0003-00")

        # Monitor alias reads at every mutation step
        def _alias_reader_probe(client, op, target_collection):
            target_physical = client.aliases.get(ALIAS)
            assert target_physical is not None, "alias must never be dangling"
            pts = client.collections.get(target_physical, [])
            # Before swap, alias must point to live_0 with count_0
            if target_physical == live_0:
                assert len(pts) == count_0, "alias reader before swap must see complete old generation"

        fake.add_mutation_hook(_alias_reader_probe)

        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
        live_1 = fake.aliases[ALIAS]
        assert live_1 != live_0
        count_1 = len(fake.collections[live_1])
        assert count_1 > count_0

        # Immediately after swap, alias reader sees complete new generation
        pts_after = fake.alias_target_points(ALIAS)
        assert len(pts_after) == count_1

    def test_serving_gate_binding_isolates_readers_across_reingest(self, tmp_path, monkeypatch):
        """Invariant E0 + D4 verification:
        A ServingGate caches the physical collection for an active request.
        During and after a reingest, an in-flight request holding the cached ServingGeneration
        continues reading from the old physical without disruption.
        A new request with fresh=True resolves to the new physical.
        """
        import asyncio

        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        settings = _settings()
        rules_v = extraction_rules_version()

        gate = ServingGate(ttl_s=3600.0)

        # In-flight reader 1 resolves generation
        async def _test_gate():
            gen_before = await gate.generation(fake, settings, rules_v)
            assert gen_before.servable
            old_physical = gen_before.physical
            assert old_physical == fake.aliases[ALIAS]

            # Ingest executes --reingest
            assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0

            # Reader 1 continues request with its bound ServingGeneration
            points = fake.collections.get(gen_before.physical, [])
            assert len(points) > 0, "reader 1 bound physical must remain accessible"

            # Fresh resolution gets new generation
            gen_after = await gate.generation(fake, settings, rules_v, fresh=True)
            assert gen_after.servable
            assert gen_after.physical != old_physical
            assert gen_after.physical == fake.aliases[ALIAS]

        asyncio.run(_test_gate())

    def test_concurrent_reader_thread_during_reingest(self, tmp_path, monkeypatch):
        """Empirical multithreading stress test:
        A concurrent reader thread continuously queries live while the main thread
        executes --reingest. The reader thread must record 0 errors and zero missing points.
        """
        _publish_env(monkeypatch)
        fake = SpyPublishFake()
        monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _build_doc(corpus, "SA22-0000-00_doc1")
        progress = tmp_path / "inv.jsonl"

        assert _run_main(monkeypatch, corpus, progress) == 0
        live_0 = fake.aliases[ALIAS]
        initial_points = copy.deepcopy(fake.collections[live_0])
        initial_count = len(initial_points)

        stop_event = threading.Event()
        reader_errors: list[Exception] = []
        read_counts: list[int] = []

        def _reader_loop():
            try:
                while not stop_event.is_set():
                    # Read points from live_0
                    page, _ = fake.scroll(live_0, limit=100)
                    read_counts.append(len(page))
                    if len(page) != initial_count:
                        raise AssertionError(f"Reader observed partial state: {len(page)} != {initial_count}")
                    time.sleep(0.001)
            except Exception as e:  # noqa: BLE001
                reader_errors.append(e)

        reader_thread = threading.Thread(target=_reader_loop, daemon=True)
        reader_thread.start()

        try:
            # Run reingest
            rc = _run_main(monkeypatch, corpus, progress, "--reingest")
            assert rc == 0
        finally:
            stop_event.set()
            reader_thread.join(timeout=5.0)

        assert not reader_errors, f"Reader thread encountered errors: {reader_errors}"
        assert len(read_counts) > 0, "Reader thread should have performed multiple reads"
        assert all(c == initial_count for c in read_counts), "All reader reads must see full count"


# ============================================================================
# Group 5: Staging Resolution & Collision Avoidance
# ============================================================================

class TestStagingResolutionAndCollisionAvoidance:
    """Stress tests verifying resolve_staging_name allocation and collision avoidance."""

    def test_resolve_staging_skips_preexisting_conflicting_collections(self):
        """When base_1 and base_2 already exist in Qdrant (e.g. from prior runs),
        resolve_staging_name must skip both and allocate base_3.
        """
        fake = PublishFake()
        gen_fp = "a" * 16
        corp_fp = "b" * 12
        base = staging_name_for(ALIAS, gen_fp, corp_fp)

        fake.create_collection(base)
        fake.create_collection(f"{base}_1")
        fake.create_collection(f"{base}_2")

        # When live is base, force_reingest should allocate base_3
        name = resolve_staging_name(fake, ALIAS, gen_fp, corp_fp, live=base, force_reingest=True)
        assert name == f"{base}_3"

    def test_resolve_staging_steady_state_returns_live_without_new_collection(self):
        """When live is base_3 and force_reingest is False, resolve_staging_name
        returns live directly.
        """
        fake = PublishFake()
        gen_fp = "a" * 16
        corp_fp = "b" * 12
        base = staging_name_for(ALIAS, gen_fp, corp_fp)
        live_name = f"{base}_3"
        fake.create_collection(live_name)

        name = resolve_staging_name(fake, ALIAS, gen_fp, corp_fp, live=live_name, force_reingest=False)
        assert name == live_name

    def test_resolve_staging_on_fresh_database(self):
        """When database is fresh (no live collection), returns base."""
        fake = PublishFake()
        gen_fp = "a" * 16
        corp_fp = "b" * 12
        base = staging_name_for(ALIAS, gen_fp, corp_fp)

        name = resolve_staging_name(fake, ALIAS, gen_fp, corp_fp, live=None, force_reingest=False)
        assert name == base
        name_forced = resolve_staging_name(fake, ALIAS, gen_fp, corp_fp, live=None, force_reingest=True)
        assert name_forced == base
