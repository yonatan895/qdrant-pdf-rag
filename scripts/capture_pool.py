#!/usr/bin/env python3
"""Record live prefetch pools for offline replay (record side of record-replay).

Reads queries from a golden jsonl file, runs the production prefetch legs
(embed → Qdrant prefetch, same filter/split/expansion path as
``retrieve/query.py`` async_search), optionally scores the pool with the
resolved cross-encoder, and writes one JSON record per line shaped for
``tests/test_replay.py`` replay (via ``record_to_rows`` below).

Records carry chunk ids, ranks, chunk_type, doc ids, page labels, and
scores only — never chunk text (log contract).

Single source of truth is ``retrieve/query.py``: the live path imports its
prefetch helpers so a rename fails loudly instead of silently diverging.

Usage:
    .venv/bin/python scripts/capture_pool.py \\
        --golden evals/golden.jsonl --out /tmp/pools.jsonl
    .venv/bin/python scripts/capture_pool.py \\
        --golden evals/golden.jsonl --out /tmp/pools.jsonl --no-ce --max-queries 10
"""

from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any

# Pure record helpers below are unit-tested in tests/test_capture_pool.py
# (precedent: tests import pure helpers from scripts/).
# Live-only imports stay inside functions so unit collection never needs
# qdrant-client, httpx2, or model weights.


def legs_to_record(
    query: str,
    query_kind: str,
    legs: list[dict[str, Any]],
    ce_by_id: dict[str, float],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Serialize prefetch legs to a replayable record (pure, no I/O).

    ``legs`` entries carry ``effective_text``, ``filter_fallback``, and
    ``dense``/``sparse`` ScoredPoint lists in rank order. Chunk table is
    first-seen wins across dense-then-sparse per leg. CE scores must be
    finite — a nan/inf score is a corrupt capture, not a tunable input.
    """
    if not isinstance(query, str) or not query:
        raise ValueError("record needs a non-empty query string")
    if not isinstance(legs, list) or not legs:
        raise ValueError("record needs at least one leg")
    chunks: dict[str, dict[str, str]] = {}
    leg_rows = []
    for pos, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise TypeError(f"leg {pos}: must be a dict")
        for key in ("dense", "sparse"):
            points = leg.get(key)
            if not isinstance(points, list):
                raise TypeError(f"leg {pos}: {key!r} must be a point list")
            for point in points:
                pid = str(point.id)
                if pid not in chunks:
                    payload = point.payload or {}
                    ctype = payload.get("chunk_type", "narrative")
                    if ctype is None:
                        ctype = "narrative"  # mirrors _to_hit defaulting
                    if not isinstance(ctype, str):
                        raise TypeError(f"chunk {pid!r}: 'chunk_type' must be a string")
                    chunks[pid] = {
                        "doc_id": str(payload.get("doc_id") or ""),
                        "page": str(payload.get("page_label") or ""),
                        "chunk_type": ctype,
                    }
        leg_rows.append(
            {
                "effective_text": str(leg.get("effective_text") or ""),
                "filter_fallback": bool(leg.get("filter_fallback", False)),
                "dense": [str(p.id) for p in leg["dense"]],
                "sparse": [str(p.id) for p in leg["sparse"]],
            }
        )
    ce: dict[str, float] = {}
    for cid, score in ce_by_id.items():
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise TypeError(f"chunk {cid!r}: CE score must be a number")
        if not math.isfinite(score):
            raise ValueError(f"chunk {cid!r}: CE score must be finite")
        ce[str(cid)] = float(score)
    return {
        "query": query,
        "query_kind": query_kind,
        "legs": leg_rows,
        "chunks": chunks,
        "ce": ce,
        "_meta": dict(meta),
    }


def record_to_rows(record: dict[str, Any], leg: int = 0) -> list[dict[str, Any]]:
    """Convert one recorded leg to ``replay_pool`` rows (pure, no I/O).

    Split recordings replay per-leg: ranks are only meaningful within the
    leg that produced them, so merging legs would corrupt the replay.
    Chunks without a recorded CE score replay with ``ce=None`` (RRF-only
    replay; the rerank leg refuses unscored pools fail-closed).
    """
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    legs = record.get("legs")
    if not isinstance(legs, list) or not legs:
        raise ValueError("record needs at least one leg")
    if isinstance(leg, bool) or not isinstance(leg, int) or not 0 <= leg < len(legs):
        raise ValueError(f"leg index {leg!r} out of range for {len(legs)} legs")
    chunks = record.get("chunks")
    if not isinstance(chunks, dict):
        raise TypeError("record 'chunks' must be a dict")
    ce = record.get("ce", {})
    if not isinstance(ce, dict):
        raise TypeError("record 'ce' must be a dict")
    leg_rec = legs[leg]
    dense_ids = leg_rec.get("dense", [])
    sparse_ids = leg_rec.get("sparse", [])
    if not isinstance(dense_ids, list) or not isinstance(sparse_ids, list):
        raise TypeError("leg 'dense'/'sparse' must be id lists")
    dense_rank = {cid: rank for rank, cid in enumerate(dense_ids)}
    sparse_rank = {cid: rank for rank, cid in enumerate(sparse_ids)}
    rows = []
    for cid in list(dense_ids) + [c for c in sparse_ids if c not in dense_rank]:
        if cid not in chunks:
            raise ValueError(f"chunk {cid!r}: missing from record 'chunks'")
        info = chunks[cid]
        score = ce.get(cid)
        rows.append(
            {
                "id": cid,
                "doc_id": info.get("doc_id") or cid,
                "page": info.get("page") or "1",
                "chunk_type": info.get("chunk_type") or "narrative",
                "dense_rank": dense_rank.get(cid),
                "sparse_rank": sparse_rank.get(cid),
                "ce": score,
            }
        )
    return rows


def capture_query(
    client, embedder, collection: str, query: str, settings, *, score_ce: bool = True, reranker=None
) -> dict:
    """Run production prefetch legs for one query and serialize the pool (live).

    Mirrors ``async_search`` prefetch: identifiers → filter → split paths →
    per-leg expansion → embed → batched prefetch (sequential fallback) →
    empty-filtered retry. Fusion/rerank/diversify deliberately do NOT run
    here — replay owns ranking. An explicit ``reranker`` wins like the
    lifespan client; trap/identifier queries still bypass (RRF order
    stands) and record CE-less pools.
    """
    from mainframe_rag.retrieve.filters import build_filter, parse_query, query_kind
    from mainframe_rag.retrieve.query import (
        _build_prefetch_requests,
        _effective_query,
        _needs_filter_fallback,
        _prefetch_limit_for,
        _resolve_active_reranker,
        _split_paths_for,
        _to_hit,
    )
    from mainframe_rag.retrieve.rerank import format_rerank_text

    identifiers = parse_query(query)
    kind = query_kind(identifiers)
    flt = build_filter(identifiers)
    active_reranker, rerank_active, bypass_reason = _resolve_active_reranker(
        settings, reranker, query, identifiers.has_identifiers
    )
    sub_queries, _split_mode = _split_paths_for(settings, query)
    eff_query = _effective_query(settings, query)
    eff_legs = (
        [_effective_query(settings, p) for p in sub_queries] if len(sub_queries) > 1 else [eff_query]
    )
    prefetch_limit = _prefetch_limit_for(settings, rerank_active)

    legs = []
    for eq in eff_legs:
        dense_vec = embedder.dense_query([eq])[0]
        sparse_idx, sparse_val = embedder.sparse([eq])[0]
        dense_req, sparse_req = _build_prefetch_requests(dense_vec, sparse_idx, sparse_val, flt, prefetch_limit)
        if hasattr(client, "query_batch_points"):
            responses = client.query_batch_points(collection, requests=[dense_req, sparse_req])
            dense_points, sparse_points = responses[0].points, responses[1].points
        else:
            dense_points = client.query_points(
                collection, query=dense_vec, using="dense", limit=prefetch_limit,
                query_filter=flt, with_payload=True,
            ).points
            from qdrant_client.http import models

            sparse_points = client.query_points(
                collection,
                query=models.SparseVector(indices=sparse_idx, values=sparse_val),
                using="bm25", limit=prefetch_limit, query_filter=flt, with_payload=True,
            ).points
        leg_fallback = _needs_filter_fallback(dense_points, sparse_points, flt)
        if leg_fallback:
            dense_req, sparse_req = _build_prefetch_requests(
                dense_vec, sparse_idx, sparse_val, None, prefetch_limit
            )
            if hasattr(client, "query_batch_points"):
                responses = client.query_batch_points(collection, requests=[dense_req, sparse_req])
                dense_points, sparse_points = responses[0].points, responses[1].points
            else:
                from qdrant_client.http import models

                dense_points = client.query_points(
                    collection, query=dense_vec, using="dense", limit=prefetch_limit,
                    query_filter=None, with_payload=True,
                ).points
                sparse_points = client.query_points(
                    collection,
                    query=models.SparseVector(indices=sparse_idx, values=sparse_val),
                    using="bm25", limit=prefetch_limit, query_filter=None, with_payload=True,
                ).points
        legs.append(
            {
                "effective_text": eq,
                "filter_fallback": leg_fallback,
                "dense": dense_points,
                "sparse": sparse_points,
            }
        )

    ce_by_id: dict[str, float] = {}
    ce_scored = False
    if score_ce and rerank_active and active_reranker is not None:
        # Score the full prefetched union: a superset of every replay
        # fusion subset, so every replayed hit has a score. Fusing here
        # would only truncate under placeholder weights; replay applies
        # the real weights/alphas. Scores attach to the acronym-expanded
        # question, matching rerank.
        seen: dict[str, Any] = {}
        for leg in legs:
            for point in list(leg["dense"]) + list(leg["sparse"]):
                seen.setdefault(str(point.id), point)
        hits = [_to_hit(point, 0.0) for point in seen.values()]
        texts = [format_rerank_text(hit) for hit in hits]
        scores = active_reranker.score(eff_query, texts)
        if len(scores) != len(hits):
            raise RuntimeError(
                f"Reranker returned {len(scores)} scores for {len(hits)} captured hits"
            )
        ce_by_id = {hit.chunk_id: float(score) for hit, score in zip(hits, scores)}
        ce_scored = True
    # Else: bypassed queries (trap/identifier) and flag-off runs record
    # CE-less pools for RRF-only replay — never fabricated scores.

    meta = {
        "collection": collection,
        "embed_mode": settings.embed_mode if settings is not None else "unknown",
        "ce_scored": ce_scored,
        "bypass_reason": bypass_reason if not rerank_active else None,
        "legs": len(legs),
    }
    try:
        meta["dense_dim"] = settings.require_dense_dim() if settings is not None else None
    except RuntimeError:
        meta["dense_dim"] = None
    return legs_to_record(query, kind, legs, ce_by_id, meta)


def _iter_queries(golden_path: str, max_queries: int | None) -> list[str]:
    queries = []
    with open(golden_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            queries.append(json.loads(line)["query"])
            if max_queries is not None and len(queries) >= max_queries:
                break
    return queries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record live prefetch pools for offline replay.")
    parser.add_argument("--golden", required=True, help="Golden jsonl file (uses each entry's query).")
    parser.add_argument("--out", required=True, help="Output JSONL path (one record per line).")
    parser.add_argument("--max-queries", type=int, default=None)
    parser.add_argument("--no-ce", action="store_true", help="Skip cross-encoder scoring (RRF-only pools).")
    args = parser.parse_args(argv)

    import httpx2
    from qdrant_client import QdrantClient

    from mainframe_rag.config import load_settings
    from mainframe_rag.ingest.embed import build_embedder

    settings = load_settings()
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
    )
    embedder = build_embedder(settings)

    queries = _iter_queries(args.golden, args.max_queries)
    failures = 0
    started = time.perf_counter()
    with open(args.out, "w", encoding="utf-8") as fh:
        for query in queries:
            try:
                record = capture_query(
                    client, embedder, settings.qdrant_collection, query, settings,
                    score_ce=not args.no_ce,
                )
                fh.write(json.dumps(record) + "\n")
            except (httpx2.HTTPError, RuntimeError, OSError, ValueError) as exc:
                failures += 1
                fh.write(json.dumps({"query": query, "error": str(exc)[:200]}) + "\n")
    elapsed = round(time.perf_counter() - started, 2)
    print(json.dumps({"queries": len(queries), "failures": failures, "elapsed_s": elapsed}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
