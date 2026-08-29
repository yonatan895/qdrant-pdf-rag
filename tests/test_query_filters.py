"""Query filter + hybrid search tests with a mocked Qdrant client."""

from types import SimpleNamespace

import pytest
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ports import Embedder
from mainframe_rag.retrieve.filters import build_filter, parse_query
from mainframe_rag.retrieve.query import format_citation, rrf_fuse, search


def _settings(dim: int | None = 768) -> Settings:
    return Settings(
        qdrant_url="http://localhost:6333",
        qdrant_collection="mainframe_manuals",
        dense_dim=dim,
        embed_base_url="http://localhost:8000/v1",
        embed_model="test-embed",
        bm25_model="Qdrant/bm25",
    )


def test_parse_query_identifiers():
    ids = parse_query("what does IEA500I in SA22-7592-05 about IEASYSxx mean")
    assert ids.message_ids == ["IEA500I"]
    assert ids.doc_ids == ["SA22-7592-05"]
    assert "IEASYSxx" in ids.members
    assert ids.has_identifiers


def test_parse_query_nl():
    ids = parse_query("how should I size the lookaside facility")
    assert not ids.has_identifiers


def test_build_filter_includes_all_context():
    ids = parse_query("IEA500I")
    flt = build_filter(ids, product="z/OS", version="3.1")
    keys = {c.key for c in flt.must}
    assert keys == {"message_ids", "product", "version"}


def test_build_filter_none_when_empty():
    assert build_filter(parse_query("nothing here")) is None


def test_format_citation():
    cite = format_citation("SA22-7592-05", "z/OS MVS Init", "IEASYSxx > LFAREA", "1-17")
    assert cite == "SA22-7592-05 z/OS MVS Init, IEASYSxx > LFAREA, p. 1-17"


def test_format_citation_round_trips_through_citation_line_re():
    """The citation shape is one contract shared by retrieve.format_citation
    (producer) and agent.cites.CITATION_LINE_RE (validator of LLM output); a
    drift between the two would make every valid citation unvalidatable."""
    from mainframe_rag.agent.cites import CITATION_LINE_RE

    cite = format_citation("SA22-7592-05", "z/OS MVS Init", "IEASYSxx > LFAREA", "1-17")
    m = CITATION_LINE_RE.match(cite)
    assert m is not None
    assert m.group("doc_id") == "SA22-7592-05"
    assert m.group("page") == "1-17"


def _point(pid: str, score: float = 1.0) -> models.ScoredPoint:
    return models.ScoredPoint(
        id=pid,
        version=1,
        score=score,
        payload={
            "doc_id": "SA22-0000-00",
            "title": "Synthetic Reference",
            "heading_path": "Chapter 2 > IEA500I",
            "page_label": "1-6",
            "page_start": 5,
            "chunk_type": "message",
            "product": "z/OS",
            "version": "9.9",
            "message_ids": ["IEA500I"],
            "text": "IEA500I synthetic text",
        },
    )


class FakeQdrant:
    def __init__(self, dense, sparse, support_batch: bool = True):
        self._dense, self._sparse = dense, sparse
        self.support_batch = support_batch
        self.queries = []
        self.batch_requests = []

    def query_points(self, collection, query, using, limit, query_filter, with_payload, **_):
        self.queries.append({"using": using, "filter": query_filter, "with_payload": with_payload})
        points = self._dense if using == "dense" else self._sparse
        return SimpleNamespace(points=list(points))

    def query_batch_points(self, collection, requests, **_):
        self.batch_requests.extend(requests)
        results = []
        for req in requests:
            self.queries.append({"using": req.using, "filter": req.filter, "with_payload": req.with_payload})
            points = self._dense if req.using == "dense" else self._sparse
            results.append(SimpleNamespace(points=list(points)))
        return results


class LegacyFakeQdrant:
    """Client double lacking query_batch_points to test graceful fallback."""
    def __init__(self, dense, sparse):
        self._dense, self._sparse = dense, sparse
        self.queries = []

    def query_points(self, collection, query, using, limit, query_filter, with_payload, **_):
        self.queries.append({"using": using, "filter": query_filter, "with_payload": with_payload})
        points = self._dense if using == "dense" else self._sparse
        return SimpleNamespace(points=list(points))


class FakeEmbedder:
    """Embedder protocol double: deterministic vectors, no network."""

    def dense(self, texts):
        return [[0.1] * 4 for _ in texts]

    def sparse(self, texts):
        return [([3], [1.0]) for _ in texts]


@pytest.fixture
def embedder() -> Embedder:
    return FakeEmbedder()


def test_search_applies_message_ids_filter_in_prefetch(embedder):
    fake = FakeQdrant(dense=[_point("a")], sparse=[_point("b")])
    hits, kind, timings = search(fake, embedder, "mainframe_manuals", "IEA500I rejected", limit=5)
    assert kind == "identifier"
    for q in fake.queries:
        assert q["filter"] is not None
        keys = {c.key for c in q["filter"].must}
        assert "message_ids" in keys
    assert len(fake.queries[0]["filter"].must) == 1
    assert {q["using"] for q in fake.queries} == {"dense", "bm25"}
    assert timings["embed_ms"] >= 0 and timings["qdrant_ms"] >= 0
    assert {h.chunk_id for h in hits} == {"a", "b"}


def test_search_uses_query_batch_points_with_field_include_list(embedder):
    """search() must execute dense + sparse prefetches in a single batch call
    with restricted payload fields (not full payload)."""
    from mainframe_rag.retrieve.query import RETRIEVE_PAYLOAD_FIELDS

    fake = FakeQdrant(dense=[_point("d1")], sparse=[_point("s1")])
    hits, _kind, _timings = search(fake, embedder, "mainframe_manuals", "sample query", limit=5)
    assert len(fake.batch_requests) == 2
    assert fake.batch_requests[0].using == "dense"
    assert fake.batch_requests[1].using == "bm25"
    for req in fake.batch_requests:
        assert req.with_payload == list(RETRIEVE_PAYLOAD_FIELDS)
        assert "embed_text" not in req.with_payload
    assert len(hits) == 2


def test_search_falls_back_to_query_points_when_batch_unsupported(embedder):
    """search() must fall back to sequential query_points if the client lacks query_batch_points."""
    from mainframe_rag.retrieve.query import RETRIEVE_PAYLOAD_FIELDS

    fake = LegacyFakeQdrant(dense=[_point("d1")], sparse=[_point("s1")])
    hits, _kind, _timings = search(fake, embedder, "mainframe_manuals", "sample query", limit=5)
    assert len(fake.queries) == 2
    for q in fake.queries:
        assert q["with_payload"] == list(RETRIEVE_PAYLOAD_FIELDS)
    assert len(hits) == 2


def test_search_identifier_weights_favor_bm25(embedder):
    fake = FakeQdrant(dense=[_point("dense-only")], sparse=[_point("sparse-only")])
    hits, kind, _ = search(fake, embedder, "mainframe_manuals", "IEA500I", limit=5)
    assert kind == "identifier"
    assert hits[0].chunk_id == "sparse-only"


def test_search_nl_weights_equal(embedder):
    fake = FakeQdrant(dense=[_point("dense-only")], sparse=[_point("sparse-only")])
    hits, kind, _ = search(fake, embedder, "mainframe_manuals", "sizing lookaside", limit=5)
    assert kind == "nl"
    # Equal weights, equal ranks -> tie; dense list order wins stably.
    assert [h.chunk_id for h in hits] == ["dense-only", "sparse-only"]


def test_rrf_fuse_scores():
    dense = [_point("a", 0.9), _point("b", 0.8)]
    sparse = [_point("b", 0.9)]
    hits = rrf_fuse(dense, sparse, weights=(1.0, 3.0), k=2, limit=8)
    scores = {h.chunk_id: h.score for h in hits}
    # b ranks first in bm25 with weight 3 and second in dense with weight 1
    assert scores["b"] > scores["a"]
    assert hits[0].chunk_id == "b"


def test_rrf_hits_carry_citation_fields():
    hits = rrf_fuse([_point("a")], [], weights=(1.0, 3.0))
    assert hits[0].cite == "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
    assert hits[0].text == "IEA500I synthetic text"

