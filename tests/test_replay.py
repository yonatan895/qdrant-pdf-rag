"""Recorded-pool schema for local ranking replay (``scripts/capture_pool.py``).

A prod RC run captures prefetch pools as ids/ranks/chunk_type plus
cross-encoder scores — ids, counts, and scores only, never PDF text, per the
log contract. ``replay_pool`` rebuilds prefetch legs from such a recording and
must fail closed on a corrupt capture rather than silently rank it. The
ranking chain itself (RRF, boosts, alpha, diversity) is owned by
``test_query_filters.py`` / ``test_rerank.py`` and swept end to end in
``test_replay_sweep.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts.capture_pool import replay_pool


def _row(pid: str, dense_rank: int | None = None, sparse_rank: int | None = None, ce: Any = 0.5) -> dict[str, Any]:
    return {"id": pid, "doc_id": pid, "page": "1", "dense_rank": dense_rank, "sparse_rank": sparse_rank, "ce": ce}


def test_replay_pool_orders_legs_by_rank_and_defaults_optional_fields():
    rows = [
        {"id": "b", "doc_id": "B", "page": "2", "dense_rank": 1, "sparse_rank": 0},  # no ce, no chunk_type
        {"id": "a", "doc_id": "A", "page": "1", "dense_rank": 0, "ce": 0.5},
        {"id": "c", "doc_id": "C", "page": "3", "sparse_rank": 1, "ce": 0.1},  # sparse-only
    ]
    dense, sparse, ce_by_id = replay_pool(rows)
    assert [p.id for p in dense] == ["a", "b"]
    assert [p.id for p in sparse] == ["b", "c"]
    assert ce_by_id == {"b": None, "a": 0.5, "c": 0.1}  # CE-less rows are kept, marked unscored
    assert all(p.payload["chunk_type"] == "narrative" for p in dense)


@pytest.mark.parametrize(
    "rows",
    [
        "not-a-list",
        ["just-a-string"],
        [{"no": "id"}],
        [_row("a", dense_rank=0), _row("a", sparse_rank=0)],  # duplicate id
        [_row("a", dense_rank=0), _row("b", dense_rank=0)],  # duplicate rank in a leg
        [_row("lonely")],  # no rank on either leg
        [_row("flag", dense_rank=True)],
        [_row("nance", dense_rank=0, ce=float("nan"))],
        [{**_row("badtype", dense_rank=0), "chunk_type": 7}],
        [{**_row("baddoc", dense_rank=0), "doc_id": ""}],
        [{**_row("badpage", dense_rank=0), "page": ""}],
    ],
)
def test_replay_schema_rejects_corrupt_capture(rows):
    with pytest.raises((ValueError, TypeError)):
        replay_pool(rows)  # type: ignore[arg-type]
