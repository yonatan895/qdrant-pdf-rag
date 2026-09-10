"""Record-replay for agent-side ranking (local tuning without prod models).

A prod RC run captures prefetch pools as ids/ranks/chunk_type plus
cross-encoder scores — ids, counts, and scores only, never PDF text, per
the log contract. This module rebuilds equivalent prefetch legs from such
a recording and replays the real chain
(``rrf_fuse`` → ``rerank_candidates`` → ``diversify_hits``) hermetically,
so type-boosts, fusion alpha, and diversity caps can be tuned locally.

The builder stays local (not in ``tests/fakes.py``): the recorded-pool
schema is specific to this module, like other per-file shapes.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from qdrant_client.http import models

from mainframe_rag.retrieve.query import diversify_hits, rrf_fuse
from mainframe_rag.retrieve.rerank import format_rerank_text, rerank_candidates
from tests.conftest import MockReranker

# Production truncation mirrored here: the non-rerank path keeps
# max(limit*3, 24) fused candidates; the rerank path fuses the top-50 pool.
_RERANK_POOL = 50


def _require_rank(value: Any, row_id: str, leg: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"replay row {row_id!r}: {leg}_rank must be a non-negative int")
    return value


def replay_pool(rows: list[dict[str, Any]]) -> tuple[list, list, dict[str, float]]:
    """Validate a recorded pool and rebuild (dense, sparse, ce_by_id) legs.

    Leg order follows recorded rank order, like real prefetch results.
    Fail-closed: corrupt captures raise (TypeError for wrong shapes,
    ValueError for bad values), never silently rank.
    """
    if not isinstance(rows, list):
        raise TypeError("replay pool must be a list of row dicts")
    seen_ids: set[str] = set()
    seen_ranks: dict[str, set[int]] = {"dense": set(), "sparse": set()}
    dense_ranked: list[tuple[int, dict[str, Any]]] = []
    sparse_ranked: list[tuple[int, dict[str, Any]]] = []
    ce_by_id: dict[str, float] = {}
    for pos, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TypeError(f"replay row {pos}: must be a dict")
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise ValueError(f"replay row {pos}: 'id' must be a non-empty string")
        if row_id in seen_ids:
            raise ValueError(f"replay row {row_id!r}: duplicate id")
        seen_ids.add(row_id)
        dense_rank = _require_rank(row.get("dense_rank"), row_id, "dense")
        sparse_rank = _require_rank(row.get("sparse_rank"), row_id, "sparse")
        if dense_rank is None and sparse_rank is None:
            raise ValueError(f"replay row {row_id!r}: needs a rank on at least one leg")
        for leg, rank in (("dense", dense_rank), ("sparse", sparse_rank)):
            if rank is not None:
                if rank in seen_ranks[leg]:
                    raise ValueError(f"replay row {row_id!r}: duplicate {leg}_rank {rank}")
                seen_ranks[leg].add(rank)
        ce = row.get("ce")
        if isinstance(ce, bool) or not isinstance(ce, (int, float)) or not math.isfinite(ce):
            raise ValueError(f"replay row {row_id!r}: 'ce' must be a finite number")
        chunk_type = row.get("chunk_type", "narrative")
        if chunk_type is None:
            chunk_type = "narrative"  # mirrors _to_hit defaulting
        if not isinstance(chunk_type, str):
            raise TypeError(f"replay row {row_id!r}: 'chunk_type' must be a string")
        doc_id = row.get("doc_id", row_id)
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError(f"replay row {row_id!r}: 'doc_id' must be a non-empty string")
        page = row.get("page", "1")
        if not isinstance(page, str) or not page:
            raise ValueError(f"replay row {row_id!r}: 'page' must be a non-empty string")
        point = models.ScoredPoint(
            id=row_id,
            version=1,
            score=1.0,
            payload={
                "doc_id": doc_id,
                "title": f"Replay {doc_id}",
                "heading_path": f"Replay > {row_id}",
                "page_label": page,
                "chunk_type": chunk_type,
                "message_ids": [],
                "text": "",
            },
        )
        if dense_rank is not None:
            dense_ranked.append((dense_rank, {"point": point}))
        if sparse_rank is not None:
            sparse_ranked.append((sparse_rank, {"point": point}))
        ce_by_id[row_id] = float(ce)
    dense = [entry["point"] for _, entry in sorted(dense_ranked, key=lambda t: t[0])]
    sparse = [entry["point"] for _, entry in sorted(sparse_ranked, key=lambda t: t[0])]
    return dense, sparse, ce_by_id


def replay_rank(
    rows: list[dict[str, Any]],
    *,
    weights: tuple[float, float] = (1.0, 1.0),
    k: int = 2,
    type_boosts: dict[str, float] | None = None,
    alpha: float = 1.0,
    limit: int = 8,
    max_per_page: int = 1,
    max_per_doc: int = 3,
    rerank: bool = True,
) -> list:
    """Replay one recorded pool through the production ranking chain."""
    dense, sparse, ce_by_id = replay_pool(rows)
    if not rerank:
        fused = rrf_fuse(dense, sparse, weights=weights, k=k, limit=max(limit * 3, 24), type_boosts=type_boosts)
        return diversify_hits(fused, limit=limit, max_per_page=max_per_page, max_per_doc=max_per_doc)
    fused = rrf_fuse(dense, sparse, weights=weights, k=k, limit=_RERANK_POOL, type_boosts=type_boosts)
    if not fused:
        return []
    score_map = {format_rerank_text(hit): ce_by_id[hit.chunk_id] for hit in fused}
    reranked = rerank_candidates("replay query", fused, MockReranker(score_map), alpha=alpha, top_k=limit)
    return diversify_hits(reranked, limit=limit, max_per_page=max_per_page, max_per_doc=max_per_doc)


def _row(
    pid: str,
    dense_rank: int | None = None,
    sparse_rank: int | None = None,
    ce: float = 0.5,
    chunk_type: str = "narrative",
    doc_id: str | None = None,
    page: str = "1",
) -> dict[str, Any]:
    return {
        "id": pid,
        "doc_id": doc_id or pid,
        "page": page,
        "chunk_type": chunk_type,
        "dense_rank": dense_rank,
        "sparse_rank": sparse_rank,
        "ce": ce,
    }


def _boost_pool() -> list[dict[str, Any]]:
    # narr leads both legs plainly; the table hit only ranks on sparse.
    # Table x3 must flip rank 1 without moving the dense-only syntax hit
    # (dense-leg contributions are never boosted).
    return [
        _row("narr", dense_rank=0, sparse_rank=1, ce=0.5),
        _row("tbl", sparse_rank=0, ce=0.5, chunk_type="table"),
        _row("dsonly", dense_rank=1, ce=0.5, chunk_type="syntax"),
    ]


def test_replay_is_deterministic():
    rows = _boost_pool()
    first = [(h.chunk_id, h.score) for h in replay_rank(rows)]
    second = [(h.chunk_id, h.score) for h in replay_rank(rows)]
    assert first == second
    plain_first = [(h.chunk_id, h.score) for h in replay_rank(rows, rerank=False)]
    assert plain_first == [(h.chunk_id, h.score) for h in replay_rank(rows, rerank=False)]


def test_replay_type_boost_flips_table_without_touching_dense_leg():
    plain = replay_rank(_boost_pool(), rerank=False)
    assert [h.chunk_id for h in plain] == ["narr", "tbl", "dsonly"]
    boosted = replay_rank(_boost_pool(), rerank=False, type_boosts={"table": 3.0})
    assert boosted[0].chunk_id == "tbl"
    plain_by_id = {h.chunk_id: h.score for h in plain}
    boosted_by_id = {h.chunk_id: h.score for h in boosted}
    assert boosted_by_id["dsonly"] == plain_by_id["dsonly"]
    assert boosted_by_id["narr"] == plain_by_id["narr"]


def test_replay_alpha_zero_keeps_rrf_order_while_one_follows_ce():
    rows = [
        _row("c1", dense_rank=0, sparse_rank=0, ce=0.0, doc_id="D1", page="1"),
        _row("c2", dense_rank=1, sparse_rank=1, ce=0.7, doc_id="D2", page="2"),
        _row("c3", dense_rank=2, sparse_rank=2, ce=1.0, doc_id="D3", page="3"),
    ]
    kept = replay_rank(rows, alpha=0.0, limit=3)
    assert [h.chunk_id for h in kept] == ["c1", "c2", "c3"]
    assert [h.rerank_score for h in kept] == [0.0, 0.7, 1.0]
    legacy = replay_rank(rows, alpha=1.0, limit=3)
    assert [h.chunk_id for h in legacy] == ["c3", "c2", "c1"]


def test_replay_diversify_caps_hold_on_deep_pool():
    rows = [
        _row("a1", dense_rank=0, doc_id="A", page="1"),
        _row("a2", dense_rank=1, doc_id="A", page="1"),
        _row("a3", dense_rank=2, doc_id="A", page="1"),
        _row("b1", dense_rank=3, doc_id="B", page="1"),
        _row("c1", dense_rank=4, doc_id="C", page="1"),
        _row("d1", dense_rank=5, doc_id="D", page="1"),
    ]
    hits = replay_rank(rows, rerank=False, limit=4)
    assert [h.chunk_id for h in hits] == ["a1", "b1", "c1", "d1"]
    page_counts: dict[tuple[str, str], int] = {}
    for hit in hits:
        key = (hit.doc_id, hit.page_label)
        page_counts[key] = page_counts.get(key, 0) + 1
    assert all(count <= 1 for count in page_counts.values())


def test_replay_empty_pool_returns_empty():
    assert replay_rank([]) == []
    assert replay_rank([], rerank=False) == []


def test_replay_missing_chunk_type_defaults_narrative():
    rows = [
        {"id": "x1", "doc_id": "X", "page": "7", "dense_rank": 0, "sparse_rank": 0, "ce": 0.5},
    ]
    hits = replay_rank(rows, rerank=False, limit=1)
    assert hits[0].chunk_type == "narrative"


@pytest.mark.parametrize(
    "rows",
    [
        "not-a-list",
        [{"no": "id"}],
        [{"id": "", "dense_rank": 0, "ce": 0.5}],
        [_row("a", dense_rank=0, ce=0.5), _row("a", sparse_rank=0, ce=0.5)],
        [_row("a", dense_rank=0, ce=0.5), _row("b", dense_rank=0, ce=0.5)],
        [_row("a", sparse_rank=0, ce=0.5), _row("b", sparse_rank=0, ce=0.5)],
        [_row("lonely", ce=0.5)],
        [_row("neg", dense_rank=-1, ce=0.5)],
        [_row("flag", dense_rank=True, ce=0.5)],
        [_row("frac", dense_rank=0.5, ce=0.5)],
        [{"id": "noce", "doc_id": "N", "page": "1", "dense_rank": 0}],
        [_row("badce", dense_rank=0, ce="high")],
        [_row("nance", dense_rank=0, ce=float("nan"))],
        [_row("infce", dense_rank=0, ce=float("inf"))],
        [_row("boolce", dense_rank=0, ce=True)],
        [{**_row("badtype", dense_rank=0, ce=0.5), "chunk_type": 7}],
        [{**_row("baddoc", dense_rank=0, ce=0.5), "doc_id": ""}],
        [{**_row("badpage", dense_rank=0, ce=0.5), "page": ""}],
        ["just-a-string"],
    ],
)
def test_replay_schema_rejects_corrupt_capture(rows):
    with pytest.raises((ValueError, TypeError)):
        replay_pool(rows)  # type: ignore[arg-type]
