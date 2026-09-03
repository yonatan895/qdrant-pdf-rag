"""Query filter + hybrid search tests with a mocked Qdrant client."""

import asyncio
import time
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

    def dense_query(self, queries):
        return self.dense(queries)

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
    # Equal weights (1.0, 1.0), equal ranks -> tie; dense list order wins stably.
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


def test_rrf_fuse_tie_order_is_deterministic():
    """Tied RRF scores must produce stable, deterministic ordering (Timsort stability)."""
    p1 = _point("chunk-1")
    p2 = _point("chunk-2")
    # In equal weight and equal ranks across dense and sparse:
    hits = rrf_fuse([p1, p2], [p2, p1], weights=(1.0, 1.0), k=2, limit=2)
    assert len(hits) == 2
    assert hits[0].score == hits[1].score
    assert [h.chunk_id for h in hits] == ["chunk-1", "chunk-2"]


def test_diversify_hits_prevents_page_monopoly():
    from mainframe_rag.retrieve.query import SearchHit, diversify_hits

    def h(chunk_id: str, doc_id: str, page_label: str, score: float) -> SearchHit:
        return SearchHit(
            chunk_id=chunk_id,
            score=score,
            cite=f"{doc_id}, p. {page_label}",
            heading=f"Section {page_label}",
            text=f"Text {chunk_id}",
            doc_id=doc_id,
            title="Title",
            page_label=page_label,
            chunk_type="narrative",
            message_ids=(),
        )

    # 3 chunks from doc1 page 1, 1 chunk from doc1 page 2, 1 chunk from doc2 page 3
    raw = [
        h("c1", "doc1", "1", 0.9),
        h("c2", "doc1", "1", 0.8),
        h("c3", "doc1", "1", 0.7),
        h("c4", "doc1", "2", 0.6),
        h("c5", "doc2", "3", 0.5),
    ]
    # Diversified with max_per_page=1, max_per_doc=1:
    # Phase 1 selects c1 (doc1, p1) and c5 (doc2, p3).
    # Phase 2 backfills c4 (doc1, p2) which satisfies max_per_page=1 before picking duplicate page 1!
    div = diversify_hits(raw, limit=3, max_per_page=1, max_per_doc=1)
    assert [x.chunk_id for x in div] == ["c1", "c5", "c4"]


class SlowBlockingEmbedder(FakeEmbedder):
    """Embedder double whose sync calls block the thread like the real thing:
    dense_query is a sync HTTP POST to the embed server, sparse is CPU-bound
    FastEmbed/BM25 work."""

    def __init__(self, delay_s: float = 0.15):
        self.delay_s = delay_s

    def dense_query(self, queries):
        time.sleep(self.delay_s)
        return super().dense_query(queries)

    def sparse(self, texts):
        time.sleep(self.delay_s)
        return super().sparse(texts)


@pytest.mark.anyio
async def test_async_search_offloads_blocking_embed_from_event_loop():
    """Review S1: the embed leg of async_search must run via asyncio.to_thread.

    With the calls on the event loop, 4 concurrent queries each block the loop
    2x0.15s sequentially (>= 1.2s wall) and the loop cannot tick at all;
    offloaded, they overlap (~0.3s) and a heartbeat task keeps running."""
    from mainframe_rag.retrieve.query import async_search

    qdrant = FakeQdrant(dense=[_point("d1")], sparse=[_point("s1")])
    embedder = SlowBlockingEmbedder()

    heartbeats = 0

    async def heartbeat():
        nonlocal heartbeats
        while True:
            await asyncio.sleep(0.02)
            heartbeats += 1

    hb = asyncio.create_task(heartbeat())
    t0 = time.monotonic()
    await asyncio.gather(
        *(async_search(qdrant, embedder, "mainframe_manuals", f"IEA500I {i}", limit=5) for i in range(4))
    )
    elapsed = time.monotonic() - t0
    hb.cancel()
    assert elapsed < 0.9
    # The event loop stayed responsive while the sync embeds were in flight.
    assert heartbeats > 5


class RecordingReranker:
    """Deterministic cross-encoder double: score derived from the text itself."""

    def score(self, query, texts):
        return [float(len(t) % 7) + 0.5 for t in texts]


def test_async_search_matches_sync_search_identical_fakes():
    """Review S3 drift guard: async_search mirrors search() line for line, so
    any divergence between the two transports must fail here. Identical fakes
    must produce identical hits (order + every field), query_kind, timing keys
    and prefetch shapes — with and without the rerank leg, for identifier and
    natural-language queries (different RRF weight branches)."""
    from mainframe_rag.retrieve.query import async_search

    for query in ("IEA500I rejected", "sizing the lookaside facility"):
        for reranker in (None, RecordingReranker()):
            fake_sync = FakeQdrant(dense=[_point("d1", 0.9), _point("d2", 0.5)], sparse=[_point("s1", 0.8)])
            fake_async = FakeQdrant(dense=[_point("d1", 0.9), _point("d2", 0.5)], sparse=[_point("s1", 0.8)])
            embedder = FakeEmbedder()

            sync_res = search(fake_sync, embedder, "mainframe_manuals", query, limit=5, reranker=reranker)
            async_res = asyncio.run(
                async_search(fake_async, embedder, "mainframe_manuals", query, limit=5, reranker=reranker)
            )

            sync_hits, sync_kind, sync_timings = sync_res
            async_hits, async_kind, async_timings = async_res
            assert sync_kind == async_kind
            assert [h.model_dump() for h in sync_hits] == [h.model_dump() for h in async_hits]
            assert set(sync_timings) == set(async_timings)
            assert [(r.using, r.limit) for r in fake_sync.batch_requests] == [
                (r.using, r.limit) for r in fake_async.batch_requests
            ]
            rerank_expected = reranker is not None
            assert ("rerank_ms" in sync_timings) is rerank_expected
            assert (sync_hits[0].rerank_score is not None) is rerank_expected


