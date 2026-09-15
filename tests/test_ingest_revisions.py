"""Issue #361 step 2: revision-keyed selectors, locks, completions, chunk ids.

Two source revisions sharing a printed doc_id coexist; refresh replaces by
inventory lineage; crash residue is swept; unattributable legacy residue
fails closed. Every destructive test asserts the untouched sibling, not
just the changed revision.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk, make_chunk_id
from mainframe_rag.ingest.completion import (
    completion_collection_name,
    completion_point_id,
    doc_generation_id,
    is_doc_complete,
    is_revision_committed,
    legacy_markers,
    read_completion,
)
from mainframe_rag.ingest.ibm_pdf import ParsedDoc
from mainframe_rag.ingest.identity import AmbiguousRevisionError, source_rev_key
from mainframe_rag.ingest.qdrant_io import delete_by_revision, stored_doc_revisions
from mainframe_rag.ingest.rules_version import extraction_rules_version
from tests.test_run_ingest import _filter_doc_id, _filter_match_value


def _settings(**overrides):
    kw = {"embed_mode": "hash", "_env_file": None, "batch_size": 16}
    kw.update(overrides)
    return Settings(**kw)


def _rev(vendor, product, version, sha) -> str:
    return source_rev_key(vendor, product, version, sha)


def _parsed(doc_id="reference", sha="a" * 64, vendor="vendor-a",
            product="product-x", version="1.0") -> ParsedDoc:
    return ParsedDoc(
        path=Path("d.pdf"), sha256=sha, doc_id=doc_id, title="t",
        product=product, version=version, vendor=vendor, page_count=1,
    )


def _chunks(doc_id="reference", rev="rev", n=3, tag="body") -> list[Chunk]:
    return [
        Chunk(
            chunk_id=make_chunk_id(rev, "H", i, 0), doc_id=doc_id, heading_path="H",
            page_start=i, page_label=str(i + 1), chunk_type="narrative",
            text=f"{tag} chunk {i}", message_ids=[], members=[], ordinal=i,
        )
        for i in range(n)
    ]


def _vectors(n):
    return [([0.1] * 4, ([1], [1.0])) for _ in range(n)]


class RevisionFake:
    """Collection-keyed point store honoring doc_id + source_rev filters
    and both delete shapes (FilterSelector, PointIdsList)."""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self._points: dict[str, list] = {}
        self.upserts = 0
        self.deletes = 0

    def collection_exists(self, name):
        return True

    def get_collection(self, name):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors={"dense": SimpleNamespace(size=self.dim)})
            )
        )

    def create_collection(self, name, **kwargs):
        return True

    def create_payload_index(self, *a, **k):
        return SimpleNamespace()

    def update_collection(self, name, *, optimizer_config=None):
        return True

    def scroll(self, name, *, scroll_filter=None, limit=10, with_payload=None, offset=None):
        doc_id = _filter_doc_id(scroll_filter)
        rev = _filter_match_value(scroll_filter, "source_rev")
        stored = self._points.get(name, [])
        if doc_id is not None:
            stored = [p for p in stored if (p.payload or {}).get("doc_id") == doc_id]
        if rev is not None:
            stored = [p for p in stored if (p.payload or {}).get("source_rev") == rev]
        start = offset if isinstance(offset, int) else 0
        page = stored[start:start + limit]
        nxt = start + limit if start + limit < len(stored) else None
        return page, nxt

    def retrieve(self, name, ids, *, with_payload=True, with_vectors=False):
        wanted = {str(i) for i in ids}
        return [
            SimpleNamespace(id=p.id, payload=p.payload)
            for p in self._points.get(name, [])
            if str(getattr(p, "id", None)) in wanted
        ]

    def upsert(self, name, *, points, wait=True):
        self.upserts += 1
        # Production upsert overwrites same-id points (manifest recommit);
        # a duplicated manifest id would read back as a stale first.
        stored = self._points.setdefault(name, [])
        ids = {str(p.id) for p in points}
        stored[:] = [p for p in stored if str(p.id) not in ids]
        stored.extend(points)
        return SimpleNamespace()

    def delete(self, name, *, points_selector, wait=True):
        self.deletes += 1
        ids = getattr(points_selector, "points", None)
        if ids is not None:
            wanted = {str(i) for i in ids}
            self._points[name] = [
                p for p in self._points.get(name, [])
                if str(getattr(p, "id", None)) not in wanted
            ]
            return SimpleNamespace()
        doc_id = _filter_doc_id(points_selector)
        rev = _filter_match_value(points_selector, "source_rev")
        self._points[name] = [
            p for p in self._points.get(name, [])
            if not (
                (doc_id is None or (p.payload or {}).get("doc_id") == doc_id)
                and (rev is None or (p.payload or {}).get("source_rev") == rev)
            )
        ]
        return SimpleNamespace()

    def main_ids(self, collection, doc_id):
        return {
            str(p.id)
            for p in self._points.get(collection, [])
            if (p.payload or {}).get("doc_id") == doc_id
        }

    def main_revs(self, collection, doc_id):
        return {
            (p.payload or {}).get("source_rev")
            for p in self._points.get(collection, [])
            if (p.payload or {}).get("doc_id") == doc_id
        }


def _upsert_one(monkeypatch, fake, parsed, chunks, settings=None,
                src_labels="||", lineage_rev=None):
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.run_ingest import _DocLocks, _upsert_one

    settings = settings or _settings()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    return _upsert_one(parsed, chunks, _vectors(len(chunks)), settings, _DocLocks(),
                       None, False, src_labels=src_labels, lineage_rev=lineage_rev)


SHA_A1 = "a1" * 32
SHA_B1 = "b1" * 32
REV_A = _rev("vendor-a", "product-x", "1.0", SHA_A1)
REV_B = _rev("vendor-b", "product-y", "2.0", SHA_B1)


def _complete(settings, fake, doc_id, sha, rev, labels="||"):
    return is_doc_complete(
        fake, settings, doc_id, sha256=sha,
        rules_v=extraction_rules_version(), source_labels=labels, source_rev=rev,
    )


def fake_point_rev(fake, collection, pid):
    for p in fake._points.get(collection, []):
        if str(p.id) == pid:
            return (p.payload or {}).get("source_rev")
    raise AssertionError(f"point {pid} missing")


@pytest.mark.parametrize("first", ["a", "b"])
def test_two_vendors_coexist_either_order(monkeypatch, first):
    """Acceptance: distinct revisions never delete each other merely because
    the printed doc_id matches — in either ingest order, with no lineage
    (separate runs / fresh inventory)."""
    settings = _settings()
    fake = RevisionFake()
    order = ["a", "b"] if first == "a" else ["b", "a"]
    specs = {
        "a": _parsed(sha=SHA_A1, vendor="vendor-a", product="product-x", version="1.0"),
        "b": _parsed(sha=SHA_B1, vendor="vendor-b", product="product-y", version="2.0"),
    }
    revs = {"a": REV_A, "b": REV_B}
    for key in order:
        status, _ = _upsert_one(
            monkeypatch, fake, specs[key],
            _chunks(rev=revs[key], tag=f"edition-{key}"), settings,
        )
        assert status == "upserted"
    coll = settings.qdrant_collection
    ids_a = {p for p in fake.main_ids(coll, "reference")
             if (fake_point_rev(fake, coll, p) == REV_A)}
    ids_b = {p for p in fake.main_ids(coll, "reference")
             if (fake_point_rev(fake, coll, p) == REV_B)}
    assert len(ids_a) == 3 and len(ids_b) == 3
    assert ids_a.isdisjoint(ids_b), "revisions must mint distinct point ids"
    assert fake.main_revs(coll, "reference") == {REV_A, REV_B}
    # Same printed family key on both (citations / family search intact).
    for p in fake._points[coll]:
        assert (p.payload or {})["doc_id"] == "reference"
    # Independent verification per revision.
    assert _complete(settings, fake, "reference", SHA_A1, REV_A)
    assert _complete(settings, fake, "reference", SHA_B1, REV_B)
    # Independent markers with distinct point ids.
    name = completion_collection_name(settings)
    markers = fake._points.get(name, [])
    assert len(markers) == 2
    assert len({str(p.id) for p in markers}) == 2


def test_update_one_revision_leaves_other_untouched(monkeypatch):
    """Acceptance: refreshing A by lineage replaces exactly A's points and
    markers; B's ids, content, and completion are byte-identical."""
    settings = _settings()
    fake = RevisionFake()
    coll = settings.qdrant_collection
    _upsert_one(monkeypatch, fake, _parsed(sha=SHA_A1, vendor="vendor-a",
                product="product-x", version="1.0"),
                _chunks(rev=REV_A, tag="A-v1"), settings)
    _upsert_one(monkeypatch, fake, _parsed(sha=SHA_B1, vendor="vendor-b",
                product="product-y", version="2.0"),
                _chunks(rev=REV_B, tag="B-v1"), settings)
    before_b = {
        str(p.id): dict(p.payload or {})
        for p in fake._points[coll]
        if (p.payload or {}).get("source_rev") == REV_B
    }
    marker_b_before = read_completion(
        fake, settings, "reference", source_rev=REV_B,
        generation_id=_gen_id(settings, SHA_B1),
    )
    assert marker_b_before is not None

    sha_a2 = "a2" * 32
    rev_a2 = _rev("vendor-a", "product-x", "1.0", sha_a2)
    status, _ = _upsert_one(
        monkeypatch, fake,
        _parsed(sha=sha_a2, vendor="vendor-a", product="product-x", version="1.0"),
        _chunks(rev=rev_a2, tag="A-v2"), settings, lineage_rev=REV_A,
    )
    assert status == "upserted"
    after_b = {
        str(p.id): dict(p.payload or {})
        for p in fake._points[coll]
        if (p.payload or {}).get("source_rev") == REV_B
    }
    assert after_b == before_b, "sibling revision must be byte-identical"
    assert _complete(settings, fake, "reference", SHA_B1, REV_B)
    assert _complete(settings, fake, "reference", sha_a2, rev_a2)
    assert read_completion(
        fake, settings, "reference", source_rev=REV_B,
        generation_id=_gen_id(settings, SHA_B1),
    ).generation_id == marker_b_before.generation_id
    assert fake.main_revs(coll, "reference") == {rev_a2, REV_B}


def _gen_id(settings, sha, labels="||"):
    return doc_generation_id(settings, sha, extraction_rules_version(), labels)


def test_refresh_without_lineage_adds_and_leaves_committed(monkeypatch):
    """The lineage rule, documented: without inventory lineage a committed
    sibling is coexistence (left alone) — replacement needs lineage."""
    settings = _settings()
    fake = RevisionFake()
    _upsert_one(monkeypatch, fake, _parsed(sha=SHA_A1, vendor="vendor-a",
                product="product-x", version="1.0"),
                _chunks(rev=REV_A, tag="A-v1"), settings)
    sha_a2 = "a2" * 32
    rev_a2 = _rev("vendor-a", "product-x", "1.0", sha_a2)
    status, _ = _upsert_one(
        monkeypatch, fake,
        _parsed(sha=sha_a2, vendor="vendor-a", product="product-x", version="1.0"),
        _chunks(rev=rev_a2, tag="A-v2"), settings,
    )
    assert status == "upserted"
    coll = settings.qdrant_collection
    assert fake.main_revs(coll, "reference") == {REV_A, rev_a2}
    assert _complete(settings, fake, "reference", SHA_A1, REV_A)
    assert _complete(settings, fake, "reference", sha_a2, rev_a2)


def test_crash_residue_without_completion_is_swept(monkeypatch):
    """Points without a marker are residue, never a revision: a later
    refresh deletes them without lineage."""
    from mainframe_rag.ingest.qdrant_io import upsert_chunks

    settings = _settings()
    fake = RevisionFake()
    parsed_old = _parsed(sha=SHA_A1, vendor="vendor-a", product="product-x", version="1.0")
    upsert_chunks(fake, settings, parsed_old, _chunks(rev=REV_A, tag="A-old"), _vectors(3))
    assert not is_revision_committed(fake, settings, "reference", REV_A)
    status, _ = _upsert_one(
        monkeypatch, fake,
        _parsed(sha=SHA_B1, vendor="vendor-b", product="product-y", version="2.0"),
        _chunks(rev=REV_B, tag="B-v1"), settings,
    )
    assert status == "upserted"
    assert fake.main_revs(settings.qdrant_collection, "reference") == {REV_B}


def _legacy_points(doc_id, sha, rules_v, n=3, tag="legacy"):
    return [
        models.PointStruct(
            id=f"legacy-{i}",
            vector={"dense": [0.1] * 4, "bm25": models.SparseVector(indices=[1], values=[1.0])},
            payload={"doc_id": doc_id, "sha256": sha, "rules_v": rules_v, "text": f"{tag} {i}"},
        )
        for i in range(n)
    ]


def test_legacy_sole_history_migrates_on_refresh(monkeypatch):
    """Pre-361B residue (sourceless points + sourceless marker) is the sole
    history: a changed refresh deletes it by doc_id and stamps the new
    revision — the lazy in-place migration, no flags."""
    settings = _settings()
    fake = RevisionFake()
    coll = settings.qdrant_collection
    rules_v = extraction_rules_version()
    fake.upsert(coll, points=_legacy_points("reference", SHA_A1, rules_v))
    assert stored_doc_revisions(fake, settings, "reference") == {None}
    status, _ = _upsert_one(
        monkeypatch, fake,
        _parsed(sha=SHA_B1, vendor="vendor-b", product="product-y", version="2.0"),
        _chunks(rev=REV_B, tag="B-v1"), settings,
    )
    assert status == "upserted"
    assert fake.main_revs(coll, "reference") == {REV_B}
    assert "legacy-0" not in fake.main_ids(coll, "reference")
    assert _complete(settings, fake, "reference", SHA_B1, REV_B)
    assert legacy_markers(fake, settings, "reference") == []


def test_legacy_unchanged_doc_skips_without_reingest(monkeypatch):
    """Lazy upgrade: an unchanged legacy doc (sourceless points + matching
    sourceless marker) skips — no mass re-ingest on upgrade."""
    from mainframe_rag.ingest.completion import expected_digests

    settings = _settings()
    fake = RevisionFake()
    coll = settings.qdrant_collection
    rules_v = extraction_rules_version()
    chunks = _chunks(rev=REV_A, n=3, tag="A-v1")
    ids = sorted(c.chunk_id for c in chunks)
    points = [
        models.PointStruct(
            id=cid,
            vector={"dense": [0.1] * 4, "bm25": models.SparseVector(indices=[1], values=[1.0])},
            payload={
                "doc_id": "reference", "sha256": SHA_A1, "rules_v": rules_v,
                "text": next(c.text for c in chunks if c.chunk_id == cid),
            },
        )
        for cid in ids
    ]
    fake.upsert(coll, points=points)
    n, ids_d, content_d = expected_digests(chunks)
    gen = _gen_id(settings, SHA_A1)
    fake.upsert(
        completion_collection_name(settings),
        points=[
            models.PointStruct(
                id="legacy-marker",
                vector={"dense": [0.0] * 256,
                        "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={
                    "doc_id": "reference", "sha256": SHA_A1, "rules_v": rules_v,
                    "target_collection": coll, "generation_id": gen,
                    "expected_chunks": n, "chunk_ids_digest": ids_d,
                    "content_digest": content_d, "embed_mode": "hash",
                },
            )
        ],
    )
    upserts_before = fake.upserts
    deletes_before = fake.deletes
    status, _ = _upsert_one(
        monkeypatch, fake,
        _parsed(sha=SHA_A1, vendor="vendor-a", product="product-x", version="1.0"),
        _chunks(rev=REV_A, n=3, tag="A-v1"), settings,
    )
    assert status == "skipped"
    assert fake.upserts == upserts_before and fake.deletes == deletes_before


def test_sourceless_residue_amid_named_raises(monkeypatch):
    """Unattributable legacy residue beside a live revision fails closed:
    deleting by doc_id could wipe the live revision."""
    settings = _settings()
    fake = RevisionFake()
    coll = settings.qdrant_collection
    rules_v = extraction_rules_version()
    _upsert_one(monkeypatch, fake, _parsed(sha=SHA_B1, vendor="vendor-b",
                product="product-y", version="2.0"),
                _chunks(rev=REV_B, tag="B-v1"), settings)
    fake.upsert(coll, points=_legacy_points("reference", "ff" * 32, rules_v, tag="stray"))
    deletes_before = fake.deletes
    upserts_before = fake.upserts
    with pytest.raises(AmbiguousRevisionError, match="reference"):
        _upsert_one(
            monkeypatch, fake,
            _parsed(sha="cc" * 32, vendor="vendor-c", product="p", version="3"),
            _chunks(rev=_rev("vendor-c", "p", "3", "cc" * 32), tag="C-v1"), settings,
        )
    assert fake.deletes == deletes_before and fake.upserts == upserts_before
    try:
        _upsert_one(
            monkeypatch, fake,
            _parsed(sha="cc" * 32, vendor="vendor-c", product="p", version="3"),
            _chunks(rev=_rev("vendor-c", "p", "3", "cc" * 32), tag="C-v1"), settings,
        )
        raised = None
    except AmbiguousRevisionError as exc:
        raised = str(exc)
    assert raised is not None
    assert "reference" in raised and "original --progress file" in raised
    assert "/tmp" not in raised and "stray 0" not in raised


def test_completion_ids_scope_per_revision(monkeypatch):
    settings = _settings()
    fake = RevisionFake()
    _upsert_one(monkeypatch, fake, _parsed(sha=SHA_A1, vendor="vendor-a",
                product="product-x", version="1.0"),
                _chunks(rev=REV_A, tag="A"), settings)
    _upsert_one(monkeypatch, fake, _parsed(sha=SHA_B1, vendor="vendor-b",
                product="product-y", version="2.0"),
                _chunks(rev=REV_B, tag="B"), settings)
    coll = settings.qdrant_collection
    id_a = completion_point_id(coll, "reference", REV_A, _gen_id(settings, SHA_A1))
    id_b = completion_point_id(coll, "reference", REV_B, _gen_id(settings, SHA_B1))
    assert id_a != id_b
    assert read_completion(fake, settings, "reference", source_rev=REV_A,
                           generation_id=_gen_id(settings, SHA_A1)) is not None
    assert read_completion(fake, settings, "reference", source_rev=REV_A,
                           generation_id=_gen_id(settings, SHA_B1)) is None


def test_locks_keyed_by_revision():
    from mainframe_rag.ingest.run_ingest import _DocLocks

    locks = _DocLocks()
    assert locks.get(REV_A) is locks.get(REV_A)
    assert locks.get(REV_B) is not locks.get(REV_A)


def test_stored_doc_revisions_and_delete_by_revision(monkeypatch):
    settings = _settings()
    fake = RevisionFake()
    coll = settings.qdrant_collection
    assert stored_doc_revisions(fake, settings, "reference") == set()
    _upsert_one(monkeypatch, fake, _parsed(sha=SHA_A1, vendor="vendor-a",
                product="product-x", version="1.0"),
                _chunks(rev=REV_A, tag="A"), settings)
    fake.upsert(coll, points=_legacy_points("reference", "ff" * 32,
                                            extraction_rules_version(), tag="stray"))
    assert stored_doc_revisions(fake, settings, "reference") == {REV_A, None}
    delete_by_revision(fake, settings, REV_A)
    remaining = fake.main_ids(coll, "reference")
    assert remaining == {"legacy-0", "legacy-1", "legacy-2"}
    assert _complete(settings, fake, "reference", SHA_A1, REV_A) is False


def test_plan_refresh_deletes_matrix(monkeypatch):
    from mainframe_rag.ingest.completion import plan_refresh_deletes

    settings = _settings()
    fake = RevisionFake()
    # Empty store: nothing to delete, no legacy sweep.
    assert plan_refresh_deletes(fake, settings, "reference", REV_A, None) == (set(), False)
    _upsert_one(monkeypatch, fake, _parsed(sha=SHA_A1, vendor="vendor-a",
                product="product-x", version="1.0"),
                _chunks(rev=REV_A, tag="A"), settings)
    # Stale current revision always replaced, even without lineage.
    assert plan_refresh_deletes(fake, settings, "reference", REV_A, None) == ({REV_A}, False)
    # Lineage replaces precisely.
    rev_a2 = _rev("vendor-a", "product-x", "1.0", "a2" * 32)
    assert plan_refresh_deletes(fake, settings, "reference", rev_a2, REV_A) == ({REV_A}, False)


def test_triple_variant_refresh_retires_old_marker(monkeypatch):
    """Same bytes+labels under another CLI triple: the same revision
    re-certifies (points overwritten idempotently — same chunk ids), and
    the refresh retires the previous triple's marker, so exactly one
    marker certifies the revision at any time."""
    from mainframe_rag.ingest.completion import source_labels

    settings = _settings()
    fake = RevisionFake()
    coll = settings.qdrant_collection
    parsed = _parsed(doc_id="DOC1")
    rev = _rev("v", "p", "1", "a" * 64)
    assert _upsert_one(monkeypatch, fake, parsed, _chunks(doc_id="DOC1", rev=rev, tag="t1"),
                       settings, src_labels=source_labels(None, None, None))[0] == "upserted"
    assert _upsert_one(monkeypatch, fake, parsed, _chunks(doc_id="DOC1", rev=rev, tag="t1"),
                       settings, src_labels=source_labels(None, "Solaris", None))[0] == "upserted"
    name = completion_collection_name(settings)
    markers = [p for p in fake._points.get(name, [])]
    assert len(markers) == 1, "refresh retires the previous triple's marker"
    assert len(fake.main_ids(coll, "DOC1")) == 3, "same chunk ids overwritten, never duplicated"
    assert _upsert_one(monkeypatch, fake, parsed, _chunks(doc_id="DOC1", rev=rev, tag="t1"),
                       settings, src_labels=source_labels("IBM", None, None),
                       lineage_rev=rev)[0] == "upserted"
    markers = [p for p in fake._points.get(name, [])]
    assert len(markers) == 1
    assert markers[0].payload["generation_id"].endswith(source_labels("IBM", None, None))


def test_marker_listing_paginates_past_old_cap():
    """Review fix: marker reads paginate to exhaustion — 150 markers under
    one doc_id are all visible with a 100-point page, and a revision delete
    removes exactly its own marker (a fixed 100-cap silently dropped both
    reads and invalidations)."""
    from mainframe_rag.ingest.completion import (
        _doc_markers,
        delete_completion,
        write_completion,
    )

    settings = _settings(ingest_scan_page_size=100)
    fake = RevisionFake()
    revs = [_rev("v", "p", "1", f"{i:064x}") for i in range(150)]
    for i, rev in enumerate(revs):
        write_completion(
            fake, settings, doc_id="D", sha256=f"{i:064x}",
            rules_v="r" * 16, source_labels="cli", source_rev=rev,
            expected_chunks=1, chunk_ids_digest="c" * 16, content_digest="d" * 16,
        )
    assert len(_doc_markers(fake, settings, "D")) == 150
    delete_completion(fake, settings, "D", source_rev=revs[0])
    remaining = _doc_markers(fake, settings, "D")
    assert len(remaining) == 149
    assert {m.source_rev for m in remaining} == set(revs[1:])
