"""Hybrid retrieval: dense + BM25 prefetch with filters, fused with local RRF.

Why local RRF instead of Qdrant's FusionQuery.RRF: per-prefetch weights are
required ([1,3] when identifiers are present, else [1,1]) and k=2. Qdrant's
server-side RRF does not expose weights. Two filtered prefetch queries, fused
here, preserve the "filters in prefetch" contract. architecture.md 4.5.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from qdrant_client import models

if TYPE_CHECKING:
    from mainframe_rag.config import Settings

from mainframe_rag.ports import AsyncQdrantPoints, Embedder, QdrantPoints, Reranker
from mainframe_rag.retrieve.filters import build_filter, parse_query, query_kind

PREFETCH_LIMIT = 40
RRF_K = 2
RRF_WEIGHTS_IDENTIFIER = (1.0, 3.0)  # (dense, bm25): identifiers favor exact terms
RRF_WEIGHTS_NL = (1.0, 1.0)


RETRIEVE_PAYLOAD_FIELDS: tuple[str, ...] = (
    "doc_id",
    "title",
    "heading_path",
    "page_label",
    "chunk_type",
    "product",
    "version",
    "message_ids",
    "text",
)


def format_citation(doc_id: str, title: str, heading_path: str, page_label: str) -> str:
    """SA22-7592-05 z/OS MVS Init..., IEASYSxx > LFAREA, p. 1-17

    The citation shape contract; cites.CITATION_LINE_RE validates this shape
    on LLM output."""
    parts = [f"{doc_id} {title}".strip(), heading_path]
    cite = ", ".join(p for p in parts if p)
    if page_label:
        cite += f", p. {page_label}"
    return cite


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    score: float
    cite: str
    heading: str
    text: str
    doc_id: str
    title: str
    page_label: str
    chunk_type: str
    message_ids: tuple[str, ...]
    product: str | None = None
    version: str | None = None
    rerank_score: float | None = None


def _to_hit(point: models.ScoredPoint, score: float) -> SearchHit:
    payload = point.payload or {}
    doc_id = str(payload.get("doc_id") or "")
    title = str(payload.get("title") or "")
    heading = str(payload.get("heading_path") or "")
    page_label = str(payload.get("page_label") or "")
    return SearchHit(
        chunk_id=str(point.id),
        score=score,
        cite=format_citation(doc_id, title, heading, page_label),
        heading=heading,
        text=str(payload.get("text") or ""),
        doc_id=doc_id,
        title=title,
        page_label=page_label,
        chunk_type=str(payload.get("chunk_type") or "narrative"),
        product=payload.get("product"),
        version=payload.get("version"),
        message_ids=tuple(payload.get("message_ids") or []),
    )


def _prefetch_one(
    client: QdrantPoints,
    collection: str,
    query_vec,
    using: str,
    flt: models.Filter | None,
    limit: int,
) -> list[models.ScoredPoint]:
    """Single-vector query against one named vector/sparse space; payload
    restricted to required citation and ranking fields."""
    result = client.query_points(
        collection,
        query=query_vec,
        using=using,
        limit=limit,
        query_filter=flt,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )
    return result.points


def rrf_fuse(
    dense: list[models.ScoredPoint],
    sparse: list[models.ScoredPoint],
    weights: tuple[float, float],
    k: int = RRF_K,
    limit: int = 8,
) -> list[SearchHit]:
    by_id: dict[str, models.ScoredPoint] = {}
    scores: dict[str, float] = defaultdict(float)
    for weight, points in zip(weights, (dense, sparse)):
        for rank, point in enumerate(points):
            key = str(point.id)
            by_id[key] = point
            scores[key] += weight / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [_to_hit(by_id[key], score) for key, score in ranked]


def diversify_hits(
    hits: list[SearchHit],
    limit: int = 8,
    max_per_page: int = 1,
    max_per_doc: int = 3,
) -> list[SearchHit]:
    """Ensures search results provide diverse coverage across documents and pages
    so near-duplicate consecutive chunks do not crowd out relevant companion
    manuals or distinct sections."""
    selected: list[SearchHit] = []
    seen_pages: dict[tuple[str, str], int] = {}
    seen_docs: dict[str, int] = {}
    remaining: list[SearchHit] = []

    # Phase 1: select candidates respecting both per-page and per-doc caps
    for h in hits:
        p_key = (h.doc_id, h.page_label)
        d_key = h.doc_id
        if seen_pages.get(p_key, 0) < max_per_page and seen_docs.get(d_key, 0) < max_per_doc:
            seen_pages[p_key] = seen_pages.get(p_key, 0) + 1
            seen_docs[d_key] = seen_docs.get(d_key, 0) + 1
            selected.append(h)
        else:
            remaining.append(h)
        if len(selected) >= limit:
            return selected

    # Phase 2: backfill without violating max_per_page (relax per-doc cap first)
    still_remaining: list[SearchHit] = []
    for h in remaining:
        p_key = (h.doc_id, h.page_label)
        if seen_pages.get(p_key, 0) < max_per_page:
            seen_pages[p_key] = seen_pages.get(p_key, 0) + 1
            selected.append(h)
            if len(selected) >= limit:
                return selected
        else:
            still_remaining.append(h)

    still_remaining.sort(
        key=lambda h: (
            seen_pages.get((h.doc_id, h.page_label), 0),
            -(h.rerank_score if h.rerank_score is not None else h.score),
        )
    )
    for h in still_remaining:
        p_key = (h.doc_id, h.page_label)
        seen_pages[p_key] = seen_pages.get(p_key, 0) + 1
        selected.append(h)
        if len(selected) >= limit:
            break

    return selected


_memoized_reranker: tuple[int, Reranker | None] | None = None


def search(
    client: QdrantPoints,
    embedder: Embedder,
    collection: str,
    query: str,
    product: str | None = None,
    version: str | None = None,
    limit: int = 8,
    settings: Settings | None = None,
    reranker: Reranker | None = None,
) -> tuple[list[SearchHit], str, dict[str, int]]:
    """Returns (hits, query_kind, timing_ms). Filters applied inside prefetch.

    Dense and sparse prefetch queries execute concurrently in a single HTTP
    batch call via query_batch_points (falling back to query_points if unsupported).
    When reranking is enabled, fused candidates (top-50) are scored by the cross-encoder."""
    identifiers = parse_query(query)
    flt = build_filter(identifiers, product=product, version=version)

    active_reranker = reranker
    if active_reranker is None and settings and settings.rerank_enabled:
        global _memoized_reranker
        sid = id(settings)
        if _memoized_reranker is None or _memoized_reranker[0] != sid:
            from mainframe_rag.retrieve.rerank import build_reranker

            _memoized_reranker = (sid, build_reranker(settings))
        active_reranker = _memoized_reranker[1]
    rerank_active = active_reranker is not None

    prefetch_limit = settings.rerank_candidates if (settings and rerank_active) else PREFETCH_LIMIT

    timings: dict[str, int] = {}

    t0 = time.monotonic()
    dense_vec = embedder.dense_query([query])[0]
    sparse_idx, sparse_val = embedder.sparse([query])[0]
    timings["embed_ms"] = int((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    dense_req = models.QueryRequest(
        query=dense_vec,
        using="dense",
        limit=prefetch_limit,
        filter=flt,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )
    sparse_req = models.QueryRequest(
        query=models.SparseVector(indices=sparse_idx, values=sparse_val),
        using="bm25",
        limit=prefetch_limit,
        filter=flt,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )

    if hasattr(client, "query_batch_points"):
        responses = client.query_batch_points(collection, requests=[dense_req, sparse_req])
        dense_points = responses[0].points
        sparse_points = responses[1].points
    else:
        dense_points = _prefetch_one(client, collection, dense_vec, "dense", flt, prefetch_limit)
        sparse_points = _prefetch_one(
            client,
            collection,
            models.SparseVector(indices=sparse_idx, values=sparse_val),
            "bm25",
            flt,
            prefetch_limit,
        )
    timings["qdrant_ms"] = int((time.monotonic() - t0) * 1000)

    if settings:
        weights = (
            (settings.rrf_weight_dense_identifier, settings.rrf_weight_sparse_identifier)
            if identifiers.has_identifiers
            else (settings.rrf_weight_dense_nl, settings.rrf_weight_sparse_nl)
        )
        k = settings.rrf_k
        max_per_page = settings.retrieve_max_chunks_per_page
        max_per_doc = settings.retrieve_max_chunks_per_doc
    else:
        weights = RRF_WEIGHTS_IDENTIFIER if identifiers.has_identifiers else RRF_WEIGHTS_NL
        k = RRF_K
        max_per_page = 1
        max_per_doc = 3

    if rerank_active and active_reranker is not None:
        from mainframe_rag.retrieve.rerank import rerank_candidates

        rrf_limit = settings.rerank_candidates if settings else 50
        candidates = rrf_fuse(dense_points, sparse_points, weights, k=k, limit=rrf_limit)
        t_rr = time.monotonic()
        reranked = rerank_candidates(query, candidates, active_reranker)
        timings["rerank_ms"] = int((time.monotonic() - t_rr) * 1000)
        hits = diversify_hits(reranked, limit=limit, max_per_page=max_per_page, max_per_doc=max_per_doc)
    else:
        candidates = rrf_fuse(dense_points, sparse_points, weights, k=k, limit=max(limit * 3, 24))
        hits = diversify_hits(candidates, limit=limit, max_per_page=max_per_page, max_per_doc=max_per_doc)

    return hits, query_kind(identifiers), timings


async def _async_prefetch_one(
    client: AsyncQdrantPoints | QdrantPoints,
    collection: str,
    vec: list[float] | models.SparseVector,
    using: str,
    flt: models.Filter | None,
    limit: int,
) -> list[models.ScoredPoint]:
    res = client.query_points(
        collection,
        query=vec,
        using=using,
        limit=limit,
        query_filter=flt,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )
    resp = await res if inspect.isawaitable(res) else res
    return resp.points


async def async_search(
    client: AsyncQdrantPoints | QdrantPoints,
    embedder: Embedder,
    collection: str,
    query: str,
    product: str | None = None,
    version: str | None = None,
    limit: int = 8,
    settings: Settings | None = None,
    reranker: Reranker | None = None,
) -> tuple[list[SearchHit], str, dict[str, int]]:
    """Async: returns (hits, query_kind, timing_ms). Filters applied inside prefetch.

    Dense and sparse prefetch queries execute concurrently in a single HTTP
    batch call via query_batch_points (falling back to query_points if unsupported).
    When reranking is enabled, fused candidates (top-50) are scored by the cross-encoder."""
    identifiers = parse_query(query)
    flt = build_filter(identifiers, product=product, version=version)

    active_reranker = reranker
    if active_reranker is None and settings and settings.rerank_enabled:
        global _memoized_reranker
        sid = id(settings)
        if _memoized_reranker is None or _memoized_reranker[0] != sid:
            from mainframe_rag.retrieve.rerank import build_reranker

            _memoized_reranker = (sid, build_reranker(settings))
        active_reranker = _memoized_reranker[1]
    rerank_active = active_reranker is not None

    prefetch_limit = settings.rerank_candidates if (settings and rerank_active) else PREFETCH_LIMIT

    timings: dict[str, int] = {}

    # dense_query is a sync HTTP POST to the embed server and sparse is
    # CPU-bound FastEmbed/BM25; both are sync by protocol. Offload to a worker
    # thread — running them on the event loop would block every in-flight
    # request for the duration of the embed call (review S1).
    t0 = time.monotonic()
    dense_vec = (await asyncio.to_thread(embedder.dense_query, [query]))[0]
    sparse_idx, sparse_val = (await asyncio.to_thread(embedder.sparse, [query]))[0]
    timings["embed_ms"] = int((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    dense_req = models.QueryRequest(
        query=dense_vec,
        using="dense",
        limit=prefetch_limit,
        filter=flt,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )
    sparse_req = models.QueryRequest(
        query=models.SparseVector(indices=sparse_idx, values=sparse_val),
        using="bm25",
        limit=prefetch_limit,
        filter=flt,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )

    if hasattr(client, "query_batch_points"):
        res = client.query_batch_points(collection, requests=[dense_req, sparse_req])
        responses = await res if inspect.isawaitable(res) else res
        dense_points = responses[0].points
        sparse_points = responses[1].points
    else:
        dense_points = await _async_prefetch_one(client, collection, dense_vec, "dense", flt, prefetch_limit)
        sparse_points = await _async_prefetch_one(
            client,
            collection,
            models.SparseVector(indices=sparse_idx, values=sparse_val),
            "bm25",
            flt,
            prefetch_limit,
        )
    timings["qdrant_ms"] = int((time.monotonic() - t0) * 1000)

    if settings:
        weights = (
            (settings.rrf_weight_dense_identifier, settings.rrf_weight_sparse_identifier)
            if identifiers.has_identifiers
            else (settings.rrf_weight_dense_nl, settings.rrf_weight_sparse_nl)
        )
        k = settings.rrf_k
        max_per_page = settings.retrieve_max_chunks_per_page
        max_per_doc = settings.retrieve_max_chunks_per_doc
    else:
        weights = RRF_WEIGHTS_IDENTIFIER if identifiers.has_identifiers else RRF_WEIGHTS_NL
        k = RRF_K
        max_per_page = 1
        max_per_doc = 3

    if rerank_active and active_reranker is not None:
        from mainframe_rag.retrieve.rerank import rerank_candidates

        rrf_limit = settings.rerank_candidates if settings else 50
        candidates = rrf_fuse(dense_points, sparse_points, weights, k=k, limit=rrf_limit)
        t_rr = time.monotonic()
        # Cross-encoder scoring is sync HTTP (batches of settings.rerank_batch_size);
        # offload like the embed leg above (review S1).
        reranked = await asyncio.to_thread(rerank_candidates, query, candidates, active_reranker)
        timings["rerank_ms"] = int((time.monotonic() - t_rr) * 1000)
        hits = diversify_hits(reranked, limit=limit, max_per_page=max_per_page, max_per_doc=max_per_doc)
    else:
        candidates = rrf_fuse(dense_points, sparse_points, weights, k=k, limit=max(limit * 3, 24))
        hits = diversify_hits(candidates, limit=limit, max_per_page=max_per_page, max_per_doc=max_per_doc)

    return hits, query_kind(identifiers), timings

