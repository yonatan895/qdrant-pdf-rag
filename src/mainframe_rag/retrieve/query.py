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

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict
from qdrant_client import models

if TYPE_CHECKING:
    from mainframe_rag.config import Settings

from mainframe_rag.ports import AsyncQdrantPoints, Embedder, QdrantPoints, Reranker
from mainframe_rag.retrieve.filters import build_filter, parse_query, query_kind
from mainframe_rag.retrieve.rewrite import expand_query, should_rewrite
from mainframe_rag.retrieve.screen import screen_query
from mainframe_rag.retrieve.split import split_query

# Proxy tracer: no-op until a real provider is installed (issue #83 — the
# agent's lifespan installs one when OTEL_EXPORTER_OTLP_ENDPOINT is set).
tracer = trace.get_tracer("mainframe-rag.retrieve")

PREFETCH_LIMIT = 40
RRF_K = 2
RRF_WEIGHTS_IDENTIFIER = (1.0, 3.0)  # (dense, bm25): identifiers favor exact terms
RRF_WEIGHTS_NL = (1.0, 1.0)

# Second-level fusion across split retrieval paths (issue #214): comparative
# peers merge by best evidence (max_split_hits); diagnostic legs merge by
# rank sum favoring the symptom anchor (anchor trust, issue #117).
SPLIT_MERGE_DIAGNOSTIC: tuple[float, float] = (2.0, 1.0)


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


def merge_split_hits(
    lists: list[list[SearchHit]],
    weights: tuple[float, float],
    k: int = RRF_K,
    limit: int = 8,
) -> list[SearchHit]:
    """Second-level RRF over already-fused per-path rankings (issue #214).

    Same 1/(k+rank+1) shape as rrf_fuse, applied to hit ranks instead of
    point ranks; weights favor the anchor leg (diagnostic symptom-first).
    First-seen hit wins the object; its score becomes the merged score so
    downstream legs (diversify backfill, rerank RRF fusion) see one scale.
    Stable sort: full ties keep leg-1-then-leg-2 order.
    """
    by_id: dict[str, SearchHit] = {}
    scores: dict[str, float] = defaultdict(float)
    for weight, hits in zip(weights, lists):
        for rank, hit in enumerate(hits):
            key = hit.chunk_id
            if key not in by_id:
                by_id[key] = hit
            scores[key] += weight / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [by_id[key].model_copy(update={"score": score}) for key, score in ranked]


def max_split_hits(
    lists: list[list[SearchHit]],
    k: int = RRF_K,
    limit: int = 8,
) -> list[SearchHit]:
    """Best-evidence fusion over comparative peer rankings (issue #214).

    Takes each doc's MAXIMUM 1/(k+rank+1) across paths instead of the sum:
    RRF-sum lets shared-context noise ranking mid in both legs outscore an
    entity-focused rank-1 (measured: IGW-messages 0.2+0.2=0.4 past a focused
    0.33 on holdout CMP-14). Peers are equals, so no weights — first-seen
    hit wins ties by stability (leg-1 order). Score becomes the max so
    downstream legs see one scale.
    """
    by_id: dict[str, SearchHit] = {}
    scores: dict[str, float] = {}
    for hits in lists:
        for rank, hit in enumerate(hits):
            key = hit.chunk_id
            if key not in by_id:
                by_id[key] = hit
            score = 1.0 / (k + rank + 1)
            if score > scores.get(key, 0.0):
                scores[key] = score
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [by_id[key].model_copy(update={"score": score}) for key, score in ranked]


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


_memoized_reranker: tuple[tuple[object, ...], Reranker | None] | None = None


def _reranker_config_key(settings: Settings) -> tuple[object, ...]:
    """Value key over every Settings field build_reranker()/HttpReranker
    consume (issue #156). The memo used id(settings): ids can be recycled
    by the allocator after a garbage-collected Settings, silently reusing
    a reranker built for a previous configuration. Equal values are safe
    to reuse by construction, so the key is the values themselves."""
    return (
        settings.embed_mode,
        settings.rerank_base_url,
        settings.embed_base_url,
        settings.rerank_model,
        settings.rerank_batch_size,
        settings.rerank_timeout_s,
        settings.allow_hash_mode,
        settings.http_connect_retries,
    )


def _rerank_bypass_reason(query: str, has_identifiers: bool) -> str | None:
    """Shared bypass classification for both twins (one rule, one helper —
    the reason string lands on the trace and must never diverge between
    search() and async_search())."""
    if screen_query(query) == "trap":
        return "trap"
    if has_identifiers:
        return "identifier"
    return None


def _resolve_active_reranker(
    settings: Settings | None,
    reranker: Reranker | None,
    query: str,
    has_identifiers: bool,
) -> tuple[Reranker | None, bool, str | None]:
    """Reranker dispatch shared by both twins: explicit client wins, else the
    flag-built memoized one; trap/identifier queries bypass (RRF order
    stands). Returns (active_reranker_or_None, rerank_active, bypass_reason).

    Issue #113: trap-class (injection) queries bypass rerank — however
    the reranker arrived (flag-built, memoized, or explicitly passed
    by the lifespan client). RRF order stands, so the must_not
    hard-zero holds with RERANK_ENABLED=true. Prefetch also drops to
    the non-rerank limit: no 50-candidate fetch for a leg that will
    not run.
    Issue #117: identifier-kind queries bypass rerank on the same
    structural principle — rerank authority scales with anchor trust.
    The cross-encoder scores shape-compatibility, so on exact-code
    queries it prefers confident definitions of the WRONG message and
    buries the right context (DSN9022I live: CE rank 13+ over RRF
    rank 1 — no rank-fusion k can bridge that gap, k cancels out).
    RRF's lexical anchor is the trustworthy signal here; the CE
    keeps serving NL queries, where it earns its keep (PAR-10/19)."""
    active_reranker = reranker
    if active_reranker is None and settings and settings.rerank_enabled:
        global _memoized_reranker
        key = _reranker_config_key(settings)
        if _memoized_reranker is None or _memoized_reranker[0] != key:
            from mainframe_rag.retrieve.rerank import build_reranker

            _memoized_reranker = (key, build_reranker(settings))
        active_reranker = _memoized_reranker[1]
    bypass_reason = _rerank_bypass_reason(query, has_identifiers)
    if active_reranker is not None and bypass_reason is not None:
        active_reranker = None
    return active_reranker, active_reranker is not None, bypass_reason


def _effective_query(settings: Settings | None, query: str) -> str:
    """Deterministic acronym expansion (issue #82) feeds both retrieval legs
    (and rerank scoring below). Identifiers, filters, and the returned
    query_kind stay on the operator's original query."""
    if settings and settings.acronym_expansion_enabled and should_rewrite(query):
        return expand_query(query)
    return query


def _split_paths_for(settings: Settings | None, query: str) -> tuple[list[str], str]:
    """Split gate shared by both twins (one rule per concept): flags off or
    no settings → legacy single path. Otherwise split_query owns trap and
    identifier bypass, so the twins cannot diverge on gating."""
    if settings is None or (
        not settings.comparative_split_enabled and not settings.diagnostic_dualpath_enabled
    ):
        return ([query], "single")
    return split_query(
        query,
        comparative_enabled=settings.comparative_split_enabled,
        diagnostic_enabled=settings.diagnostic_dualpath_enabled,
    )


def _prefetch_limit_for(settings: Settings | None, rerank_active: bool) -> int:
    return settings.rerank_candidates if (settings and rerank_active) else PREFETCH_LIMIT


def _needs_filter_fallback(
    dense_points: list[models.ScoredPoint],
    sparse_points: list[models.ScoredPoint],
    flt: models.Filter | None,
) -> bool:
    """One rule for the empty-filtered retry (both twins share it): only when
    a filter was applied and both legs came back empty. Non-empty filtered
    results take the byte-identical legacy path — no second call."""
    return flt is not None and not dense_points and not sparse_points


def _retrieve_span_attrs(
    query: str,
    limit: int,
    rerank_active: bool,
    prefetch_limit: int,
    flt: models.Filter | None,
    bypass_reason: str | None,
    split_paths: int,
    split_mode: str,
) -> dict[str, str | bool | int | float]:
    attrs: dict[str, str | bool | int | float] = {
        "rag.query": query,
        "rag.limit": limit,
        "rag.rerank_active": rerank_active,
        "rag.prefetch_limit": prefetch_limit,
        "rag.filter_present": flt is not None,
        "rag.split_paths": split_paths,
        "rag.split_mode": split_mode,
    }
    if bypass_reason is not None:
        attrs["rag.rerank_bypass_reason"] = bypass_reason
    return attrs


def _build_prefetch_requests(
    dense_vec,
    sparse_idx,
    sparse_val,
    flt: models.Filter | None,
    prefetch_limit: int,
) -> tuple[models.QueryRequest, models.QueryRequest]:
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
    return dense_req, sparse_req


def _ranking_params(
    settings: Settings | None, has_identifiers: bool
) -> tuple[tuple[float, float], int, int, int]:
    """(weights, k, max_per_page, max_per_doc): Settings when present, else
    the module-constant fallback both twins shared before."""
    if settings:
        weights = (
            (settings.rrf_weight_dense_identifier, settings.rrf_weight_sparse_identifier)
            if has_identifiers
            else (settings.rrf_weight_dense_nl, settings.rrf_weight_sparse_nl)
        )
        k = settings.rrf_k
        max_per_page = settings.retrieve_max_chunks_per_page
        max_per_doc = settings.retrieve_max_chunks_per_doc
    else:
        weights = RRF_WEIGHTS_IDENTIFIER if has_identifiers else RRF_WEIGHTS_NL
        k = RRF_K
        max_per_page = 1
        max_per_doc = 3
    return weights, k, max_per_page, max_per_doc


def _rrf_span_attrs(weights: tuple[float, float], k: int, candidates_in: int) -> dict:
    return {
        "rag.rrf_k": k,
        "rag.rrf_weights": f"{weights[0]:g},{weights[1]:g}",
        "rag.candidates_in": candidates_in,
    }


def _fuse_with_span(
    dense_points: list[models.ScoredPoint],
    sparse_points: list[models.ScoredPoint],
    weights: tuple[float, float],
    k: int,
    limit: int,
) -> list[SearchHit]:
    with tracer.start_as_current_span(
        "retrieve.rrf",
        attributes=_rrf_span_attrs(weights, k, len(dense_points) + len(sparse_points)),
    ):
        return rrf_fuse(dense_points, sparse_points, weights, k=k, limit=limit)


def _diversify_with_span(
    fused: list[SearchHit], limit: int, max_per_page: int, max_per_doc: int
) -> list[SearchHit]:
    with tracer.start_as_current_span("retrieve.diversify") as dv_span:
        hits = diversify_hits(fused, limit=limit, max_per_page=max_per_page, max_per_doc=max_per_doc)
        dv_span.set_attributes(
            {
                "rag.candidates_in": len(fused),
                "rag.candidates_out": len(hits),
                "rag.doc_ids": ",".join(dict.fromkeys(h.doc_id for h in hits)),
            }
        )
    return hits


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

    active_reranker, rerank_active, bypass_reason = _resolve_active_reranker(
        settings, reranker, query, identifiers.has_identifiers
    )
    # Split on the operator's words (expansion never adds markers or ids);
    # each leg is expanded independently below. Single path short-circuits
    # so flags-off stays byte-identical legacy (no double expansion).
    sub_queries, split_mode = _split_paths_for(settings, query)
    query = _effective_query(settings, query)
    eff_legs = (
        [_effective_query(settings, p) for p in sub_queries] if split_mode != "single" else [query]
    )

    prefetch_limit = _prefetch_limit_for(settings, rerank_active)

    timings: dict[str, int] = {}
    span_attrs = _retrieve_span_attrs(
        query, limit, rerank_active, prefetch_limit, flt, bypass_reason, len(eff_legs), split_mode
    )

    with tracer.start_as_current_span("retrieve.search", attributes=span_attrs) as span:
        with tracer.start_as_current_span(
            "retrieve.embed", attributes={"rag.embedder": type(embedder).__name__}
        ):
            t0 = time.monotonic()
            # One embed per retrieval leg (single path: exactly today's call).
            leg_vecs: list[tuple[list[float], list[int], list[float]]] = []
            for eq in eff_legs:
                dense_vec = embedder.dense_query([eq])[0]
                sparse_idx, sparse_val = embedder.sparse([eq])[0]
                leg_vecs.append((dense_vec, sparse_idx, sparse_val))
            timings["embed_ms"] = int((time.monotonic() - t0) * 1000)

        with tracer.start_as_current_span(
            "retrieve.prefetch",
            attributes={
                "rag.batch": hasattr(client, "query_batch_points"),
                "rag.prefetch_limit": prefetch_limit,
            },
        ):
            t0 = time.monotonic()
            # Every leg shares the ORIGINAL filter: splitting changes ranking
            # text only, never the constraint allowlist (a stripped cause leg
            # must not surface must_not docs the symptom filter excluded).
            leg_dense_points: list[list[models.ScoredPoint]] = []
            leg_sparse_points: list[list[models.ScoredPoint]] = []
            filter_fallback = False
            for dense_vec, sparse_idx, sparse_val in leg_vecs:
                dense_req, sparse_req = _build_prefetch_requests(
                    dense_vec, sparse_idx, sparse_val, flt, prefetch_limit
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
                # Empty-filtered recovery: an exact doc-id stem (SC23-6858 vs
                # SC23-6858-01) or a multi-identifier AND can match zero points
                # while the unfiltered legs score. Retry once unfiltered — a
                # single retry only, so non-empty filtered results never pay it.
                leg_fallback = _needs_filter_fallback(dense_points, sparse_points, flt)
                if leg_fallback:
                    dense_req, sparse_req = _build_prefetch_requests(
                        dense_vec, sparse_idx, sparse_val, None, prefetch_limit
                    )
                    if hasattr(client, "query_batch_points"):
                        responses = client.query_batch_points(
                            collection, requests=[dense_req, sparse_req]
                        )
                        dense_points = responses[0].points
                        sparse_points = responses[1].points
                    else:
                        dense_points = _prefetch_one(
                            client, collection, dense_vec, "dense", None, prefetch_limit
                        )
                        sparse_points = _prefetch_one(
                            client,
                            collection,
                            models.SparseVector(indices=sparse_idx, values=sparse_val),
                            "bm25",
                            None,
                            prefetch_limit,
                        )
                leg_dense_points.append(dense_points)
                leg_sparse_points.append(sparse_points)
                filter_fallback = filter_fallback or leg_fallback
            timings["qdrant_ms"] = int((time.monotonic() - t0) * 1000)

        weights, k, max_per_page, max_per_doc = _ranking_params(settings, identifiers.has_identifiers)
        if split_mode == "single":
            leg_weights = [weights]
        else:
            # Per-leg ranking text, shared constraint filter: comparative
            # legs are NL; the diagnostic cause leg is identifier-stripped.
            leg_weights = [
                _ranking_params(settings, parse_query(sub).has_identifiers)[0]
                for sub in sub_queries
            ]

        if rerank_active and active_reranker is not None:
            from mainframe_rag.retrieve.rerank import rerank_candidates

            rrf_limit = settings.rerank_candidates if settings else 50
            fused_lists = [
                _fuse_with_span(dp, sp, w, k, rrf_limit)
                for (dp, sp), w in zip(zip(leg_dense_points, leg_sparse_points), leg_weights)
            ]
            if split_mode == "single":
                fused = fused_lists[0]
            elif split_mode == "comparative":
                fused = max_split_hits(fused_lists, k, rrf_limit)
            else:
                fused = merge_split_hits(fused_lists, SPLIT_MERGE_DIAGNOSTIC, k, rrf_limit)
            t_rr = time.monotonic()
            with tracer.start_as_current_span(
                "retrieve.rerank", attributes={"rag.candidates": len(fused)}
            ) as rr_span:
                fusion_alpha = settings.rerank_fusion_alpha if settings else 1.0
                reranked = rerank_candidates(query, fused, active_reranker, alpha=fusion_alpha)
                rr_span.set_attributes(
                    {
                        "rag.rerank_scores": ",".join(f"{h.score:.3f}" for h in reranked[:5]),
                        "rag.rerank_alpha": fusion_alpha,
                    }
                )
            timings["rerank_ms"] = int((time.monotonic() - t_rr) * 1000)
            fused = reranked
        else:
            fuse_limit = max(limit * 3, 24)
            fused_lists = [
                _fuse_with_span(dp, sp, w, k, fuse_limit)
                for (dp, sp), w in zip(zip(leg_dense_points, leg_sparse_points), leg_weights)
            ]
            if split_mode == "single":
                fused = fused_lists[0]
            elif split_mode == "comparative":
                fused = max_split_hits(fused_lists, k, fuse_limit)
            else:
                fused = merge_split_hits(fused_lists, SPLIT_MERGE_DIAGNOSTIC, k, fuse_limit)

        hits = _diversify_with_span(fused, limit, max_per_page, max_per_doc)
        span.set_attributes(
            {
                "rag.query_kind": query_kind(identifiers),
                "rag.hits": len(hits),
                "rag.filter_fallback": filter_fallback,
            }
        )

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

    active_reranker, rerank_active, bypass_reason = _resolve_active_reranker(
        settings, reranker, query, identifiers.has_identifiers
    )
    # Split on the operator's words (expansion never adds markers or ids);
    # each leg is expanded independently below. Single path short-circuits
    # so flags-off stays byte-identical legacy (no double expansion).
    sub_queries, split_mode = _split_paths_for(settings, query)
    query = _effective_query(settings, query)
    eff_legs = (
        [_effective_query(settings, p) for p in sub_queries] if split_mode != "single" else [query]
    )

    prefetch_limit = _prefetch_limit_for(settings, rerank_active)

    timings: dict[str, int] = {}
    span_attrs = _retrieve_span_attrs(
        query, limit, rerank_active, prefetch_limit, flt, bypass_reason, len(eff_legs), split_mode
    )

    with tracer.start_as_current_span("retrieve.search", attributes=span_attrs) as span:
        # dense_query is a sync HTTP POST to the embed server and sparse is
        # CPU-bound FastEmbed/BM25; both are sync by protocol. Offload to a worker
        # thread — running them on the event loop would block every in-flight
        # request for the duration of the embed call (review S1).
        with tracer.start_as_current_span(
            "retrieve.embed", attributes={"rag.embedder": type(embedder).__name__}
        ):
            t0 = time.monotonic()
            # One embed per retrieval leg, sequential (parity over fan-out;
            # each leg stays off the event loop via to_thread like today).
            leg_vecs: list[tuple[list[float], list[int], list[float]]] = []
            for eq in eff_legs:
                dense_vec = (await asyncio.to_thread(embedder.dense_query, [eq]))[0]
                sparse_idx, sparse_val = (await asyncio.to_thread(embedder.sparse, [eq]))[0]
                leg_vecs.append((dense_vec, sparse_idx, sparse_val))
            timings["embed_ms"] = int((time.monotonic() - t0) * 1000)

        with tracer.start_as_current_span(
            "retrieve.prefetch",
            attributes={
                "rag.batch": hasattr(client, "query_batch_points"),
                "rag.prefetch_limit": prefetch_limit,
            },
        ):
            t0 = time.monotonic()
            # Every leg shares the ORIGINAL filter: splitting changes ranking
            # text only, never the constraint allowlist (a stripped cause leg
            # must not surface must_not docs the symptom filter excluded).
            leg_dense_points: list[list[models.ScoredPoint]] = []
            leg_sparse_points: list[list[models.ScoredPoint]] = []
            filter_fallback = False
            for dense_vec, sparse_idx, sparse_val in leg_vecs:
                dense_req, sparse_req = _build_prefetch_requests(
                    dense_vec, sparse_idx, sparse_val, flt, prefetch_limit
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
                leg_fallback = _needs_filter_fallback(dense_points, sparse_points, flt)
                if leg_fallback:
                    dense_req, sparse_req = _build_prefetch_requests(
                        dense_vec, sparse_idx, sparse_val, None, prefetch_limit
                    )
                    if hasattr(client, "query_batch_points"):
                        res = client.query_batch_points(collection, requests=[dense_req, sparse_req])
                        responses = await res if inspect.isawaitable(res) else res
                        dense_points = responses[0].points
                        sparse_points = responses[1].points
                    else:
                        dense_points = await _async_prefetch_one(
                            client, collection, dense_vec, "dense", None, prefetch_limit
                        )
                        sparse_points = await _async_prefetch_one(
                            client,
                            collection,
                            models.SparseVector(indices=sparse_idx, values=sparse_val),
                            "bm25",
                            None,
                            prefetch_limit,
                        )
                leg_dense_points.append(dense_points)
                leg_sparse_points.append(sparse_points)
                filter_fallback = filter_fallback or leg_fallback
            timings["qdrant_ms"] = int((time.monotonic() - t0) * 1000)

        weights, k, max_per_page, max_per_doc = _ranking_params(settings, identifiers.has_identifiers)
        if split_mode == "single":
            leg_weights = [weights]
        else:
            # Per-leg ranking text, shared constraint filter: comparative
            # legs are NL; the diagnostic cause leg is identifier-stripped.
            leg_weights = [
                _ranking_params(settings, parse_query(sub).has_identifiers)[0]
                for sub in sub_queries
            ]

        if rerank_active and active_reranker is not None:
            from mainframe_rag.retrieve.rerank import rerank_candidates

            rrf_limit = settings.rerank_candidates if settings else 50
            fused_lists = [
                _fuse_with_span(dp, sp, w, k, rrf_limit)
                for (dp, sp), w in zip(zip(leg_dense_points, leg_sparse_points), leg_weights)
            ]
            if split_mode == "single":
                fused = fused_lists[0]
            elif split_mode == "comparative":
                fused = max_split_hits(fused_lists, k, rrf_limit)
            else:
                fused = merge_split_hits(fused_lists, SPLIT_MERGE_DIAGNOSTIC, k, rrf_limit)
            t_rr = time.monotonic()
            # Cross-encoder scoring is sync HTTP (batches of settings.rerank_batch_size);
            # offload like the embed leg above (review S1).
            with tracer.start_as_current_span(
                "retrieve.rerank", attributes={"rag.candidates": len(fused)}
            ) as rr_span:
                fusion_alpha = settings.rerank_fusion_alpha if settings else 1.0
                reranked = await asyncio.to_thread(
                    rerank_candidates, query, fused, active_reranker, alpha=fusion_alpha
                )
                rr_span.set_attributes(
                    {
                        "rag.rerank_scores": ",".join(f"{h.score:.3f}" for h in reranked[:5]),
                        "rag.rerank_alpha": fusion_alpha,
                    }
                )
            timings["rerank_ms"] = int((time.monotonic() - t_rr) * 1000)
            fused = reranked
        else:
            fuse_limit = max(limit * 3, 24)
            fused_lists = [
                _fuse_with_span(dp, sp, w, k, fuse_limit)
                for (dp, sp), w in zip(zip(leg_dense_points, leg_sparse_points), leg_weights)
            ]
            if split_mode == "single":
                fused = fused_lists[0]
            elif split_mode == "comparative":
                fused = max_split_hits(fused_lists, k, fuse_limit)
            else:
                fused = merge_split_hits(fused_lists, SPLIT_MERGE_DIAGNOSTIC, k, fuse_limit)

        hits = _diversify_with_span(fused, limit, max_per_page, max_per_doc)
        span.set_attributes(
            {
                "rag.query_kind": query_kind(identifiers),
                "rag.hits": len(hits),
                "rag.filter_fallback": filter_fallback,
            }
        )

    return hits, query_kind(identifiers), timings

