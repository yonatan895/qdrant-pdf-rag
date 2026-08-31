"""Hybrid retrieval: dense + BM25 prefetch with filters, fused with local RRF.

Why local RRF instead of Qdrant's FusionQuery.RRF: per-prefetch weights are
required ([1,3] when identifiers are present, else [1,1]) and k=2. Qdrant's
server-side RRF does not expose weights. Two filtered prefetch queries, fused
here, preserve the "filters in prefetch" contract. architecture.md 4.5.
"""

from __future__ import annotations

import time
from collections import defaultdict

from pydantic import BaseModel, ConfigDict
from qdrant_client import models

from mainframe_rag.ports import Embedder, QdrantPoints
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


def search(
    client: QdrantPoints,
    embedder: Embedder,
    collection: str,
    query: str,
    product: str | None = None,
    version: str | None = None,
    limit: int = 8,
) -> tuple[list[SearchHit], str, dict[str, int]]:
    """Returns (hits, query_kind, timing_ms). Filters applied inside prefetch.

    Dense and sparse prefetch queries execute concurrently in a single HTTP
    batch call via query_batch_points (falling back to query_points if unsupported)."""
    identifiers = parse_query(query)
    flt = build_filter(identifiers, product=product, version=version)

    timings: dict[str, int] = {}

    t0 = time.monotonic()
    dense_vec = embedder.dense([query])[0]
    sparse_idx, sparse_val = embedder.sparse([query])[0]
    timings["embed_ms"] = int((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    dense_req = models.QueryRequest(
        query=dense_vec,
        using="dense",
        limit=PREFETCH_LIMIT,
        filter=flt,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )
    sparse_req = models.QueryRequest(
        query=models.SparseVector(indices=sparse_idx, values=sparse_val),
        using="bm25",
        limit=PREFETCH_LIMIT,
        filter=flt,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )

    if hasattr(client, "query_batch_points"):
        responses = client.query_batch_points(collection, requests=[dense_req, sparse_req])
        dense_points = responses[0].points
        sparse_points = responses[1].points
    else:
        dense_points = _prefetch_one(client, collection, dense_vec, "dense", flt, PREFETCH_LIMIT)
        sparse_points = _prefetch_one(
            client,
            collection,
            models.SparseVector(indices=sparse_idx, values=sparse_val),
            "bm25",
            flt,
            PREFETCH_LIMIT,
        )
    timings["qdrant_ms"] = int((time.monotonic() - t0) * 1000)

    weights = RRF_WEIGHTS_IDENTIFIER if identifiers.has_identifiers else RRF_WEIGHTS_NL
    hits = rrf_fuse(dense_points, sparse_points, weights, limit=limit)
    return hits, query_kind(identifiers), timings

