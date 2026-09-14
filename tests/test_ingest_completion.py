"""Verified-completion regressions (issue #359).

Deterministic UUID5s permit replay; they do not prove completeness. Each
test below fails against the inspected behavior (single-point sampling in
stored_doc_state, inventory-only planner skip, zip-truncation in
upsert_chunks) and passes only when the claimed path — completion write
after verification, verified skip, explicit empty policy — is forced.

Hermetic: faked Qdrant port, no network, no PDFs (chunks fabricated
in-memory except where main() needs a real file).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ingest.completion import (
    acquire_run_lock,
    completion_collection_name,
    is_doc_complete,
    read_completion,
    release_run_lock,
)
from mainframe_rag.ingest.ibm_pdf import ParsedDoc
from mainframe_rag.ingest.inventory import InventoryRecord, append_record, should_skip
from mainframe_rag.ingest.qdrant_io import upsert_chunks
from mainframe_rag.ingest.rules_version import extraction_rules_version
from tests.test_run_ingest import _filter_doc_id


def _settings(**overrides):
    kw = {"embed_mode": "hash", "_env_file": None}
    kw.update(overrides)
    return Settings(**kw)


def _parsed(doc_id="DOC1", sha="a" * 64) -> ParsedDoc:
    return ParsedDoc(
        path=Path("d.pdf"), sha256=sha, doc_id=doc_id, title="t",
        product="p", version="1", vendor="v", page_count=1,
    )


def _chunks(doc_id="DOC1", n=5, tag="body") -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"{doc_id}-c{i:04d}", doc_id=doc_id, heading_path="H",
            page_start=i, page_label=str(i + 1), chunk_type="narrative",
            text=f"{tag} chunk {i}", message_ids=[], members=[], ordinal=i,
        )
        for i in range(n)
    ]


def _vectors(n):
    return [([0.1] * 4, ([1], [1.0])) for _ in range(n)]


class FailingFakeQdrant:
    """Per-collection point store with batch-failure injection."""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self._points: dict[str, list] = {}
        self.main_upsert_calls = 0
        self.main_upserted_points = 0
        self.completion_upserts = 0
        self.deletes = 0
        self.fail_main_calls: set[int] = set()
        self.fail_completion = False
        self.fail_delete: BaseException | None = None

    # -- collection surface -------------------------------------------
    def collection_exists(self, collection_name):
        return True

    def get_collection(self, collection_name):
        from types import SimpleNamespace

        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors={"dense": SimpleNamespace(size=self.dim)})
            )
        )

    def create_collection(self, collection_name, **kwargs):
        return True

    def create_payload_index(self, *a, **k):
        from types import SimpleNamespace

        return SimpleNamespace()

    def update_collection(self, collection_name, *, optimizer_config=None):
        return True

    # -- points surface -----------------------------------------------
    def scroll(self, collection_name, *, scroll_filter=None, limit=10, with_payload=None, offset=None):
        doc_id = _filter_doc_id(scroll_filter)
        stored = self._points.get(collection_name, [])
        if doc_id is not None:
            stored = [p for p in stored if (p.payload or {}).get("doc_id") == doc_id]
        return stored[:limit], None

    def upsert(self, collection_name, *, points, wait=True):
        from types import SimpleNamespace

        if collection_name.endswith("__completions"):
            if self.fail_completion:
                raise RuntimeError("injected completion-write failure")
            self.completion_upserts += 1
        else:
            self.main_upsert_calls += 1
            if self.main_upsert_calls in self.fail_main_calls:
                raise RuntimeError(f"injected main-batch failure #{self.main_upsert_calls}")
            self.main_upserted_points += len(points)
        self._points.setdefault(collection_name, []).extend(points)
        return SimpleNamespace()

    def delete(self, collection_name, *, points_selector, wait=True):
        from types import SimpleNamespace

        if self.fail_delete is not None:
            raise self.fail_delete
        self.deletes += 1
        doc_id = _filter_doc_id(points_selector)
        if doc_id is not None:
            self._points[collection_name] = [
                p for p in self._points.get(collection_name, [])
                if (p.payload or {}).get("doc_id") != doc_id
            ]
        else:
            self._points[collection_name] = []
        return SimpleNamespace()

    def main_points(self, collection, doc_id):
        return [p for p in self._points.get(collection, [])
                if (p.payload or {}).get("doc_id") == doc_id]


def _upsert_one(monkeypatch, fake, parsed, chunks, settings=None, force_reingest=False):
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.run_ingest import _DocLocks, _upsert_one

    settings = settings or _settings(batch_size=16)
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    return _upsert_one(parsed, chunks, _vectors(len(chunks)), settings, _DocLocks(),
                       None, force_reingest)


def test_pair_length_mismatch_raises_without_writes():
    """upsert_chunks must never silently truncate a mismatched pair list."""
    from types import SimpleNamespace

    seen: list = []

    class Rec:
        def upsert(self, collection, *, points, wait=True):
            seen.append(points)
            return SimpleNamespace()

    parsed = _parsed()
    with pytest.raises(ValueError, match="length mismatch"):
        upsert_chunks(Rec(), _settings(), parsed, _chunks(n=2), _vectors(1))
    assert seen == []


def test_upsert_one_rejects_mismatched_pairs(monkeypatch):
    """The stage-2 seam raises on mismatched pairs before any Qdrant call."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.run_ingest import _DocLocks, _upsert_one

    fake = FailingFakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    with pytest.raises(ValueError, match="length mismatch"):
        _upsert_one(_parsed(), _chunks(n=3), _vectors(1),
                    _settings(), _DocLocks(), None, False)
    assert fake.main_upserted_points == 0
    assert read_completion(fake, _settings(), "DOC1") is None


def test_partial_first_batch_never_skips(monkeypatch):
    """Fail the 2nd of 3 batches; retry must re-publish all, never skip."""
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    fake.fail_main_calls = {2}
    parsed = _parsed()
    chunks = _chunks(n=40)
    with pytest.raises(RuntimeError, match="injected main-batch"):
        _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    # First batch survived; no completion was written.
    assert len(fake.main_points(settings.qdrant_collection, "DOC1")) == 16
    assert read_completion(fake, settings, "DOC1") is None

    fake.fail_main_calls = set()
    status, _ = _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    assert status == "upserted", "partial residue must re-publish, never skip"
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == 40
    assert len({str(p.id) for p in points}) == 40, "no duplicates"
    assert {str(p.id) for p in points} == {c.chunk_id for c in chunks}
    completion = read_completion(fake, settings, "DOC1")
    assert completion is not None and completion.expected_chunks == 40


def test_fail_before_first_batch_resumes_exact(monkeypatch):
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    fake.fail_main_calls = {1}
    parsed = _parsed()
    chunks = _chunks(n=10, tag="exact")
    with pytest.raises(RuntimeError, match="injected main-batch"):
        _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    assert fake.main_points(settings.qdrant_collection, "DOC1") == []

    fake.fail_main_calls = set()
    status, _ = _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    assert status == "upserted"
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == 10
    by_id = {str(p.id): (p.payload or {}).get("text") for p in points}
    for c in chunks:
        assert by_id[c.chunk_id] == c.text, "content must match exactly"


def test_fail_after_last_batch_before_completion_recovers(monkeypatch):
    """All batches acked but the completion write fails: retry recovers with
    exact points and a written completion (idempotent UUID5s, no dupes)."""
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    fake.fail_completion = True
    parsed = _parsed()
    chunks = _chunks(n=33)
    with pytest.raises(RuntimeError, match="post-upsert verification failed|injected completion"):
        _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    assert read_completion(fake, settings, "DOC1") is None

    fake.fail_completion = False
    status, _ = _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    assert status == "upserted"
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == 33
    assert len({str(p.id) for p in points}) == 33
    assert read_completion(fake, settings, "DOC1") is not None


def test_fail_after_completion_before_inventory_skips_without_reupsert(monkeypatch):
    """Crash between completion write and inventory append: the next attempt
    must verify and skip — zero new main upserts, zero deletes."""
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    parsed = _parsed()
    chunks = _chunks(n=7)
    status, _ = _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    assert status == "upserted"
    calls_after_publish = fake.main_upsert_calls
    deletes_after_publish = fake.deletes

    # No inventory record survived: same inputs again (fresh parse).
    status2, _ = _upsert_one(monkeypatch, fake, parsed, _chunks(n=7), settings)
    assert status2 == "skipped"
    assert fake.main_upsert_calls == calls_after_publish
    assert fake.deletes == deletes_after_publish


def test_stale_single_point_never_skips(monkeypatch):
    """One surviving point with matching sha/rules is residue, not proof."""
    from qdrant_client import models

    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    rules_v = extraction_rules_version()
    fake.upsert(
        settings.qdrant_collection,
        points=[
            models.PointStruct(
                id="residue-point",
                vector={"dense": [0.1] * 4, "bm25": models.SparseVector(indices=[1], values=[1.0])},
                payload={"doc_id": "DOC1", "sha256": "a" * 64, "rules_v": rules_v, "text": "residue"},
            )
        ],
    )
    calls_before = fake.main_upsert_calls
    status, _ = _upsert_one(monkeypatch, fake, _parsed(), _chunks(n=5), settings)
    assert status == "upserted", "single-point residue must not skip"
    assert fake.main_upsert_calls > calls_before
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == 5
    assert "residue-point" not in {str(p.id) for p in points}


def test_zero_chunk_is_explicit_empty_never_complete(monkeypatch):
    fake = FailingFakeQdrant()
    status, _ = _upsert_one(monkeypatch, fake, _parsed(), [], _settings())
    assert status == "empty"
    assert fake.main_points(_settings().qdrant_collection, "DOC1") == []
    assert read_completion(fake, _settings(), "DOC1") is None
    # An empty outcome must never satisfy a future skip.
    rec = InventoryRecord(path="d.pdf", sha256="a" * 64, status="empty")
    assert should_skip(rec, "a" * 64, rules_version=extraction_rules_version()) is False


def test_completion_tied_to_target_generation(tmp_path, monkeypatch):
    settings_a = _settings(batch_size=16, qdrant_collection="coll_a")
    settings_b = _settings(batch_size=16, qdrant_collection="coll_b")
    fake = FailingFakeQdrant()
    status, _ = _upsert_one(monkeypatch, fake, _parsed(), _chunks(n=4), settings_a)
    assert status == "upserted"
    assert completion_collection_name(settings_a) != completion_collection_name(settings_b)
    assert is_doc_complete(fake, settings_b, "DOC1",
                           sha256="a" * 64, rules_v=extraction_rules_version()) is False
    # Same target, wrong source hash: also incomplete.
    assert is_doc_complete(fake, settings_a, "DOC1",
                           sha256="b" * 64, rules_v=extraction_rules_version()) is False


def test_refresh_failure_leaves_no_stale_completion(monkeypatch):
    """Refresh deletes the old generation first; a mid-refresh crash must
    leave NO valid completion (safe retry), never the old marker over
    partial data."""
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    v1 = _parsed(sha="1" * 64)
    status, _ = _upsert_one(monkeypatch, fake, v1, _chunks(n=3, tag="v1"), settings)
    assert status == "upserted"

    v2 = _parsed(sha="2" * 64)
    fake.main_upsert_calls = 0  # injection counter is cumulative; re-arm for the refresh
    fake.fail_main_calls = {1}  # fail after old points were deleted
    with pytest.raises(RuntimeError, match="injected main-batch"):
        _upsert_one(monkeypatch, fake, v2, _chunks(n=4, tag="v2"), settings)
    assert read_completion(fake, settings, "DOC1") is None
    assert fake.main_points(settings.qdrant_collection, "DOC1") == []

    fake.fail_main_calls = set()
    status, _ = _upsert_one(monkeypatch, fake, v2, _chunks(n=4, tag="v2"), settings)
    assert status == "upserted"
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == 4
    texts = {(p.payload or {}).get("text") for p in points}
    assert all(t.startswith("v2") for t in texts)
    completion = read_completion(fake, settings, "DOC1")
    assert completion is not None and completion.sha256 == "2" * 64


def test_malformed_completion_is_incomplete(monkeypatch):
    """A completion point predating the schema (missing fields) validates to
    None and never gates a skip — legacy state re-ingests explicitly."""
    from qdrant_client import models

    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    fake.upsert(
        completion_collection_name(settings),
        points=[
            models.PointStruct(
                id="legacy-marker",
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={"doc_id": "DOC1", "sha256": "a" * 64},
            )
        ],
    )
    assert read_completion(fake, settings, "DOC1") is None
    assert is_doc_complete(fake, settings, "DOC1",
                           sha256="a" * 64, rules_v=extraction_rules_version()) is False
    status, _ = _upsert_one(monkeypatch, fake, _parsed(), _chunks(n=3), settings)
    assert status == "upserted"


def test_delete_completion_real_error_propagates(monkeypatch):
    """A real Qdrant failure invalidating the marker must fail the doc —
    never be swallowed leaving a stale marker next to a new one. The
    refresh never starts, so a clean retry publishes exactly."""
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    status, _ = _upsert_one(monkeypatch, fake, _parsed(sha="1" * 64),
                            _chunks(n=3, tag="v1"), settings)
    assert status == "upserted"

    fake.fail_delete = RuntimeError("connection reset")
    with pytest.raises(RuntimeError, match="connection reset"):
        _upsert_one(monkeypatch, fake, _parsed(sha="2" * 64),
                    _chunks(n=4, tag="v2"), settings)
    # Refresh never started: v1 points and the v1 marker are untouched, and
    # no v2 completion exists.
    assert len(fake.main_points(settings.qdrant_collection, "DOC1")) == 3
    v1 = read_completion(fake, settings, "DOC1")
    assert v1 is not None and v1.sha256 == "1" * 64

    fake.fail_delete = None
    status, _ = _upsert_one(monkeypatch, fake, _parsed(sha="2" * 64),
                            _chunks(n=4, tag="v2"), settings)
    assert status == "upserted"
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == 4
    v2 = read_completion(fake, settings, "DOC1")
    assert v2 is not None and v2.sha256 == "2" * 64


def test_delete_completion_404_race_is_tolerated(monkeypatch):
    """Collection dropped between the exists-check and the invalidation
    delete (404) is a safe no-op — the doc still publishes."""
    import httpx
    from qdrant_client.http.exceptions import UnexpectedResponse

    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    fake.fail_delete = UnexpectedResponse(404, "Not Found", b"{}", httpx.Headers())

    status, _ = _upsert_one(monkeypatch, fake, _parsed(), _chunks(n=3), settings)
    assert status == "upserted"
    assert len(fake.main_points(settings.qdrant_collection, "DOC1")) == 3
    assert read_completion(fake, settings, "DOC1") is not None


def test_concurrent_run_lock_rejects_second_writer(tmp_path):
    progress = tmp_path / "inventory.jsonl"
    handle = acquire_run_lock(progress)
    try:
        with pytest.raises(RuntimeError, match="concurrent ingest"):
            acquire_run_lock(progress)
    finally:
        release_run_lock(handle)
    release_run_lock(handle)  # idempotent, never raises
    handle2 = acquire_run_lock(progress)
    release_run_lock(handle2)


def test_inventory_outlives_collection_no_false_skip(tmp_path, synthetic_pdf, monkeypatch):
    """Keep inventory, wipe Qdrant (both collections): rerun must re-ingest,
    never report a successful skip over missing data."""
    import json

    from mainframe_rag.ingest import run_ingest
    from tests.test_run_ingest import _FakeQdrant

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = ["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"]
    assert run_ingest.main(args) == 0
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert records and records[0]["status"] == "upserted"
    assert records[0]["generation_id"], "upserted lines must carry the generation binding"

    fake._points.clear()  # collection deletion/recreation; inventory survives
    assert run_ingest.main(args) == 0
    records2 = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert records2[-1]["status"] == "upserted"
    assert records2[-1]["chunks"] > 0, "wiped collection must re-ingest, never skip"


def test_inventory_survives_main_wipe_but_completion_kept(tmp_path, synthetic_pdf, monkeypatch):
    """Completion without points is equally incomplete: verify fails and the
    doc re-ingests."""
    import json

    from mainframe_rag.config import Settings as _Settings
    from mainframe_rag.ingest import run_ingest
    from tests.test_run_ingest import _FakeQdrant

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = ["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"]
    assert run_ingest.main(args) == 0
    main_collection = _Settings(_env_file=None).qdrant_collection
    del fake._points[main_collection]  # points lost, completion survives
    assert run_ingest.main(args) == 0
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert records[-1]["status"] == "upserted"
    assert records[-1]["chunks"] > 0


def test_legacy_inventory_without_binding_reingests(tmp_path, synthetic_pdf, monkeypatch):
    """Pre-#359 inventory lines (no generation binding) against an empty
    collection must re-ingest explicitly, never skip."""
    import json

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.ibm_pdf import sha256_file
    from mainframe_rag.ingest.rules_version import extraction_rules_version
    from tests.test_run_ingest import _FakeQdrant

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    sha = sha256_file(synthetic_pdf)
    append_record(
        progress,
        InventoryRecord(
            path=str(synthetic_pdf), sha256=sha, doc_id="SA22-0000-00",
            pages=8, chunks=7, status="upserted",
            rules_version=extraction_rules_version(),
        ),
    )
    assert run_ingest.main(
        ["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"]
    ) == 0
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert records[-1]["status"] == "upserted"
    assert records[-1]["chunks"] > 0, "legacy line over empty Qdrant must re-ingest"
    assert records[-1].get("generation_id"), "new lines carry the binding"
