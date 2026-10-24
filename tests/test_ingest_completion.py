"""Verified-completion regressions (issue #359, revision-scoped by #361).

Deterministic UUID5s permit replay; they do not prove completeness. Each
test below fails against the inspected behavior (single-point sampling,
inventory-only planner skip, zip-truncation in upsert_chunks) and passes
only when the claimed path — completion write after verification,
verified skip, explicit empty policy — is forced.

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
    doc_generation_id,
    ensure_completion_collection,
    is_doc_complete,
    read_completion,
    release_run_lock,
)
from mainframe_rag.ingest.ibm_pdf import ParsedDoc
from mainframe_rag.ingest.identity import source_rev_key
from mainframe_rag.ingest.inventory import InventoryRecord, append_record, should_skip
from mainframe_rag.ingest.qdrant_io import CollectionPolicyMismatchError, upsert_chunks
from mainframe_rag.ingest.rules_version import extraction_rules_version
from tests.helpers_builder import staging_builder  # noqa: F401 — fixture
from tests.test_run_ingest import _filter_doc_id

# Real runs publish (in-place ingest retired, #360); these tests cover the
# builder against a fixed collection through tests/helpers_builder.py.
pytestmark = pytest.mark.usefixtures("staging_builder")



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


def _rev(sha="a" * 64) -> str:
    """Source revision for the _parsed() labels (vendor v / product p /
    version 1): the revision every helper below certifies unless told."""
    return source_rev_key("v", "p", "1", sha)


def _gen(settings, sha="a" * 64, labels="||") -> str:
    return doc_generation_id(settings, sha, extraction_rules_version(), labels)


def _read(settings, fake, doc_id="DOC1", sha="a" * 64, labels="||"):
    """Scoped completion read mirroring the skip gate (revision + generation)."""
    return read_completion(
        fake, settings, doc_id, source_rev=_rev(sha), generation_id=_gen(settings, sha, labels)
    )


def _complete(settings, fake, doc_id="DOC1", sha="a" * 64, labels="||"):
    return is_doc_complete(
        fake, settings, doc_id, sha256=sha,
        rules_v=extraction_rules_version(), source_labels=labels,
        source_rev=_rev(sha),
    )


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
    def get_aliases(self):
        from types import SimpleNamespace

        return SimpleNamespace(aliases=[])

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
        from tests.test_run_ingest import _filter_match_value

        doc_id = _filter_doc_id(scroll_filter)
        rev = _filter_match_value(scroll_filter, "source_rev")
        stored = self._points.get(collection_name, [])
        if doc_id is not None:
            stored = [p for p in stored if (p.payload or {}).get("doc_id") == doc_id]
        if rev is not None:
            stored = [p for p in stored if (p.payload or {}).get("source_rev") == rev]
        return stored[:limit], None

    def retrieve(self, collection_name, ids, *, with_payload=True, with_vectors=False):
        from types import SimpleNamespace

        wanted = {str(i) for i in ids}
        return [
            SimpleNamespace(id=p.id, payload=p.payload)
            for p in self._points.get(collection_name, [])
            if str(p.id) in wanted
        ]

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

        from tests.test_run_ingest import _filter_match_value

        if self.fail_delete is not None:
            raise self.fail_delete
        self.deletes += 1
        ids = getattr(points_selector, "points", None)
        if ids is not None:
            # PointIdsList (precise completion invalidation, issue #361).
            wanted = {str(i) for i in ids}
            self._points[collection_name] = [
                p for p in self._points.get(collection_name, [])
                if str(getattr(p, "id", None)) not in wanted
            ]
            return SimpleNamespace()
        doc_id = _filter_doc_id(points_selector)
        rev = _filter_match_value(points_selector, "source_rev")
        if doc_id is None and rev is None:
            self._points[collection_name] = []
        else:
            self._points[collection_name] = [
                p for p in self._points.get(collection_name, [])
                if not (
                    (doc_id is None or (p.payload or {}).get("doc_id") == doc_id)
                    and (rev is None or (p.payload or {}).get("source_rev") == rev)
                )
            ]
        return SimpleNamespace()

    def main_points(self, collection, doc_id):
        return [p for p in self._points.get(collection, [])
                if (p.payload or {}).get("doc_id") == doc_id]


def _upsert_one(monkeypatch, fake, parsed, chunks, settings=None, force_reingest=False,
                src_labels="||", lineage_rev=None):
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.run_ingest import _DocLocks, _upsert_one

    settings = settings or _settings(batch_size=16)
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    return _upsert_one(parsed, chunks, _vectors(len(chunks)), settings, _DocLocks(),
                       None, force_reingest, src_labels=src_labels, lineage_rev=lineage_rev)


def test_mismatched_pairs_raise_before_any_write(monkeypatch):
    """Neither upsert_chunks nor the stage-2 seam may silently truncate a
    mismatched pair list: both raise before any Qdrant call."""
    from types import SimpleNamespace

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.run_ingest import _DocLocks, _upsert_one

    seen: list = []

    class Rec:
        def upsert(self, collection, *, points, wait=True):
            seen.append(points)
            return SimpleNamespace()

    with pytest.raises(ValueError, match="length mismatch"):
        upsert_chunks(Rec(), _settings(), _parsed(), _chunks(n=2), _vectors(1))
    assert seen == []

    fake = FailingFakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    with pytest.raises(ValueError, match="length mismatch"):
        _upsert_one(_parsed(), _chunks(n=3), _vectors(1),
                    _settings(), _DocLocks(), None, False, src_labels="||")
    assert fake.main_upserted_points == 0
    assert _read(_settings(), fake) is None


@pytest.mark.parametrize(("failed_batch", "survivors", "n"), [(2, 16, 40), (1, 0, 10)])
def test_failed_batch_never_skips_and_retry_publishes_exact(monkeypatch, failed_batch, survivors, n):
    """Fail the 2nd of 3 batches, or the very first: no completion is
    written, and the retry re-publishes every chunk with exact content and no
    duplicates, never skipping."""
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    fake.fail_main_calls = {failed_batch}
    parsed = _parsed()
    chunks = _chunks(n=n, tag="exact")
    with pytest.raises(RuntimeError, match="injected main-batch"):
        _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    assert len(fake.main_points(settings.qdrant_collection, "DOC1")) == survivors
    assert _read(settings, fake) is None

    fake.fail_main_calls = set()
    status, _ = _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    assert status == "upserted", "partial residue must re-publish, never skip"
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == n
    assert len({str(p.id) for p in points}) == n, "no duplicates"
    by_id = {str(p.id): (p.payload or {}).get("text") for p in points}
    assert by_id == {c.chunk_id: c.text for c in chunks}, "content must match exactly"
    completion = _read(settings, fake)
    assert completion is not None and completion.expected_chunks == n


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
    assert _read(settings, fake) is None

    fake.fail_completion = False
    status, _ = _upsert_one(monkeypatch, fake, parsed, chunks, settings)
    assert status == "upserted"
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == 33
    assert len({str(p.id) for p in points}) == 33
    assert _read(settings, fake) is not None


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
                payload={"doc_id": "DOC1", "sha256": "a" * 64, "rules_v": rules_v,
                         "source_rev": _rev("a" * 64), "text": "residue"},
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


def test_surplus_point_under_the_same_revision_never_verifies(monkeypatch):
    """#1037: one expected chunk set plus one extra point under the same doc and
    revision is not complete: the scroll must be able to observe the surplus."""
    from qdrant_client import models

    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    status, _ = _upsert_one(monkeypatch, fake, _parsed(), _chunks(n=3), settings)
    assert status == "upserted"
    assert _complete(settings, fake) is True
    template = fake.main_points(settings.qdrant_collection, "DOC1")[0]
    fake.upsert(settings.qdrant_collection, points=[models.PointStruct(
        id="surplus-point", vector=template.vector,
        payload={**template.payload, "text": "surplus"})])
    assert len(fake.main_points(settings.qdrant_collection, "DOC1")) == 4
    assert _complete(settings, fake) is False


@pytest.mark.parametrize("field", ["sha256", "rules_v"])
def test_one_point_with_a_foreign_sha_or_rules_version_never_verifies(monkeypatch, field):
    """#1038: exact point count and matching digests are not enough; each point
    must carry the marker's sha256 and rules version."""
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    status, _ = _upsert_one(monkeypatch, fake, _parsed(), _chunks(n=3), settings)
    assert status == "upserted"
    assert _complete(settings, fake) is True
    victim = fake.main_points(settings.qdrant_collection, "DOC1")[0]
    victim.payload[field] = "f" * 64 if field == "sha256" else "0" * 16
    assert _complete(settings, fake) is False


def test_zero_chunk_is_explicit_empty_never_complete(monkeypatch):
    fake = FailingFakeQdrant()
    status, _ = _upsert_one(monkeypatch, fake, _parsed(), [], _settings())
    assert status == "empty"
    assert fake.main_points(_settings().qdrant_collection, "DOC1") == []
    assert _read(_settings(), fake) is None
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
    assert is_doc_complete(fake, settings_b, "DOC1", sha256="a" * 64,
                           rules_v=extraction_rules_version(), source_labels="||",
                           source_rev=_rev()) is False
    # Same target, wrong source hash: also incomplete.
    assert is_doc_complete(fake, settings_a, "DOC1", sha256="b" * 64,
                           rules_v=extraction_rules_version(), source_labels="||",
                           source_rev=_rev("b" * 64)) is False


class _RecordingCollectionClient:
    """Minimal create/exists recorder for the completion constructor."""

    def __init__(self, exists=False, live_params=None):
        self.exists_flag = exists
        self.live_params = live_params
        self.created = None

    def collection_exists(self, _name):
        return self.exists_flag

    def get_collection(self, _name):
        from types import SimpleNamespace

        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(**(self.live_params or {}))
            )
        )

    def create_collection(self, name, **kwargs):
        self.created = (name, kwargs)

    def create_payload_index(self, *a, **k):
        return None


def test_completion_collection_shares_corpus_policy_verbatim():
    """Control state must survive the same node loss as the data it gates
    (issue #360): the selected policy applies here exactly as in the corpus
    constructor, unset policy sends no distribution keys, and an existing
    mismatching collection fails closed without mutation."""
    unset = _RecordingCollectionClient(exists=False)
    ensure_completion_collection(unset, _settings())
    for key in ("shard_number", "replication_factor", "write_consistency_factor"):
        assert key not in unset.created[1]

    policy = {
        "qdrant_shard_number": 6,
        "qdrant_replication_factor": 2,
        "qdrant_write_consistency_factor": 1,
    }
    client = _RecordingCollectionClient(exists=False)
    settings = _settings(**policy)
    name = ensure_completion_collection(client, settings)
    assert name == completion_collection_name(settings)
    assert client.created[1]["shard_number"] == 6
    assert client.created[1]["replication_factor"] == 2
    assert client.created[1]["write_consistency_factor"] == 1

    live = {"shard_number": 1, "replication_factor": 1, "write_consistency_factor": 1}
    mismatched = _RecordingCollectionClient(exists=True, live_params=live)
    with pytest.raises(CollectionPolicyMismatchError, match="snapshot-gated"):
        ensure_completion_collection(mismatched, settings)
    assert mismatched.created is None


def test_refresh_failure_leaves_no_stale_completion(monkeypatch):
    """Refresh deletes the old generation first; a mid-refresh crash must
    leave NO valid completion (safe retry), never the old marker over
    partial data. Lineage (the v1 revision from inventory) scopes the
    replacement — production always passes it."""
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    v1 = _parsed(sha="1" * 64)
    status, _ = _upsert_one(monkeypatch, fake, v1, _chunks(n=3, tag="v1"), settings)
    assert status == "upserted"

    v2 = _parsed(sha="2" * 64)
    lineage = _rev("1" * 64)
    fake.main_upsert_calls = 0  # injection counter is cumulative; re-arm for the refresh
    fake.fail_main_calls = {1}  # fail after old points were deleted
    with pytest.raises(RuntimeError, match="injected main-batch"):
        _upsert_one(monkeypatch, fake, v2, _chunks(n=4, tag="v2"), settings,
                    lineage_rev=lineage)
    assert _read(settings, fake, sha="2" * 64) is None
    assert _read(settings, fake, sha="1" * 64) is None, "lineage markers go with the refresh"
    assert fake.main_points(settings.qdrant_collection, "DOC1") == []

    fake.fail_main_calls = set()
    status, _ = _upsert_one(monkeypatch, fake, v2, _chunks(n=4, tag="v2"), settings,
                            lineage_rev=lineage)
    assert status == "upserted"
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == 4
    texts = {(p.payload or {}).get("text") for p in points}
    assert all(t.startswith("v2") for t in texts)
    completion = _read(settings, fake, sha="2" * 64)
    assert completion is not None and completion.sha256 == "2" * 64


def test_unscoped_or_malformed_completions_never_gate_a_skip(monkeypatch):
    """A completion point predating the schema never gates a skip and
    re-ingests explicitly; a sourceless pre-manifest marker fails validation
    and never certifies either."""
    from qdrant_client import models

    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    vector = {"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])}
    fake.upsert(
        completion_collection_name(settings),
        points=[
            models.PointStruct(
                id="legacy-marker", vector=vector,
                payload={"doc_id": "DOC1", "sha256": "a" * 64},
            )
        ],
    )
    assert _read(settings, fake) is None
    assert _complete(settings, fake) is False
    status, _ = _upsert_one(monkeypatch, fake, _parsed(), _chunks(n=3), settings)
    assert status == "upserted"

    other = _settings(batch_size=16, qdrant_collection="pre_manifest")
    fresh = FailingFakeQdrant()
    fresh.upsert(
        completion_collection_name(other),
        points=[
            models.PointStruct(
                id="legacy-marker-valid", vector=vector,
                payload={
                    "doc_id": "DOC1",
                    "sha256": "a" * 64,
                    "rules_v": extraction_rules_version(),
                    "target_collection": other.qdrant_collection,
                    "generation_id": "gen1",
                    "source_labels": "||",
                    "expected_chunks": 3,
                    "chunk_ids_digest": "i" * 64,
                    "content_digest": "c" * 64,
                },
            )
        ],
    )
    assert _read(other, fresh) is None, "sourceless markers never satisfy a scoped read"
    assert _complete(other, fresh) is False


def test_delete_completion_real_error_propagates(monkeypatch):
    """A real Qdrant failure invalidating the marker must fail the doc —
    never be swallowed leaving a stale marker next to a new one. The
    refresh never starts, so a clean retry publishes exactly."""
    settings = _settings(batch_size=16)
    fake = FailingFakeQdrant()
    status, _ = _upsert_one(monkeypatch, fake, _parsed(sha="1" * 64),
                            _chunks(n=3, tag="v1"), settings)
    assert status == "upserted"

    lineage = _rev("1" * 64)
    fake.fail_delete = RuntimeError("connection reset")
    with pytest.raises(RuntimeError, match="connection reset"):
        _upsert_one(monkeypatch, fake, _parsed(sha="2" * 64),
                    _chunks(n=4, tag="v2"), settings, lineage_rev=lineage)
    # Refresh never started: v1 points and the v1 marker are untouched, and
    # no v2 completion exists.
    assert len(fake.main_points(settings.qdrant_collection, "DOC1")) == 3
    v1 = _read(settings, fake, sha="1" * 64)
    assert v1 is not None and v1.sha256 == "1" * 64

    fake.fail_delete = None
    status, _ = _upsert_one(monkeypatch, fake, _parsed(sha="2" * 64),
                            _chunks(n=4, tag="v2"), settings, lineage_rev=lineage)
    assert status == "upserted"
    points = fake.main_points(settings.qdrant_collection, "DOC1")
    assert len(points) == 4
    v2 = _read(settings, fake, sha="2" * 64)
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
    assert _read(settings, fake) is not None


def test_product_override_rerun_reingests_end_to_end(tmp_path, synthetic_pdf, monkeypatch):
    """Through the real planner path: a second run with --product set must
    re-ingest (new chunks_upserted), never report a verified skip."""
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
    assert run_ingest.main([*args, "--product", "Solaris"]) == 0
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert records[-1]["status"] == "upserted"
    assert records[-1]["chunks"] > 0, "changed CLI triple must re-ingest, never skip"


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


def test_stale_inventory_never_causes_a_false_skip(tmp_path, synthetic_pdf, monkeypatch):
    """The inventory outlives its storage: wiped points (with or without the
    surviving completion) and pre-#359 lines without a generation binding must
    all re-ingest through the real planner, never report a verified skip."""
    import json

    from mainframe_rag.config import Settings as _Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.ibm_pdf import sha256_file
    from tests.test_run_ingest import _FakeQdrant

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = ["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"]

    def last_record():
        records = [json.loads(line) for line in progress.read_text().splitlines() if line.strip()]
        return records[-1]

    assert run_ingest.main(args) == 0
    first = last_record()
    assert first["status"] == "upserted"
    assert first["generation_id"], "upserted lines must carry the generation binding"

    fake._points.clear()  # collection deletion/recreation; inventory survives
    assert run_ingest.main(args) == 0
    assert last_record()["status"] == "upserted"
    assert last_record()["chunks"] > 0, "wiped collection must re-ingest, never skip"

    # Completion without points is equally incomplete: verify fails.
    del fake._points[_Settings(_env_file=None).qdrant_collection]
    assert run_ingest.main(args) == 0
    assert last_record()["status"] == "upserted"
    assert last_record()["chunks"] > 0

    # Pre-#359 inventory lines (no generation binding) over an empty store.
    fake._points.clear()
    progress.unlink()
    append_record(
        progress,
        InventoryRecord(
            path=str(synthetic_pdf), sha256=sha256_file(synthetic_pdf), doc_id="SA22-0000-00",
            pages=8, chunks=7, status="upserted", rules_version=extraction_rules_version(),
        ),
    )
    assert run_ingest.main(args) == 0
    assert last_record()["status"] == "upserted"
    assert last_record()["chunks"] > 0, "legacy line over empty Qdrant must re-ingest"
    assert last_record().get("generation_id"), "new lines carry the binding"


def test_no_completion_collection_means_no_marker_and_not_complete():
    """Before any document is certified the completion collection does not
    exist: reads answer None/False rather than failing."""
    class _NoCompletionCollection(FailingFakeQdrant):
        def collection_exists(self, collection_name):
            return False

        def scroll(self, *a, **k):
            raise RuntimeError("a missing collection must not be scrolled (404 upstream)")

    fake = _NoCompletionCollection()
    settings = _settings()
    assert _read(settings, fake) is None
    assert _complete(settings, fake) is False
