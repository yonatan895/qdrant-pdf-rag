#!/usr/bin/env python3
"""Harness L1: retrieval metrics for the layered promotion gate.

Extends the retrieval-eval machinery (same retrieve_search, same golden
schema, same must_not sibling allowance via the shared helper) with the
gate's metric set:

  - recall@5 / recall@8, MRR@8, nDCG@8 — computed PER QUERY CLASS, always;
    aggregates are reported alongside and never alone.
  - trap precision: must_not violations are an absolute P0 signal — a single
    violating entry fails the gate; precision is reported for visibility
    but the gate never averages traps away.

nDCG@8 definition (graded, deterministic, doc-level): the hit list is
deduplicated per doc_id (best-ranked chunk of a doc wins) because the gold
is doc-level; per-doc gain = 1 for a doc hit, +1 when the gold carries
expected_heading and the heading matches (case-fold substring — the shared
relevance helper in the retrieval eval), +1 when the gold carries
expected_page and the page matches (doc-restricted, as in the retrieval
eval). DCG uses the standard 1/log2(rank+1) discount; IDCG gives max gain
to ONE expected doc (the heading/page gold fields are singular and can be
satisfied by at most one document) and plain doc gain to the rest — the
entry's honest ceiling, so nDCG stays within [0, 1] and is comparable
across entries with different gold richness.

Abstain rows are excluded from recall/MRR/nDCG denominators (same rule as
the retrieval eval) but their must_not traps are still checked.

Per-query metric values are returned so the gate can bootstrap PAIRED
deltas against the stored baseline — retrieval is deterministic against
the pinned snapshot, so per-entry differences measure the change, not
run-to-run noise.

Compatibility delegate (issue #508 C2): pure scoring lives in
:mod:`mainframe_rag.eval.retrieval` and dataset identity in
:mod:`mainframe_rag.eval.datasets`. This module re-exports the same
functions (not a copy); live collection stays here until its family moves.

Retirement condition: all known callers import the package directly, the
successor is documented and qualified, and the maintainer approves removing
this shim.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from mainframe_rag.eval.datasets import GoldenEntry  # noqa: E402
from mainframe_rag.eval.retrieval import (  # noqa: E402
    L1_KEYS,
    L1_LIMIT,
    aggregate,
    is_relevant_hit,
    must_not_violations,
    ndcg_at_k as _ndcg_at_k,
    score_row,
)

__all__ = [
    "GoldenEntry",
    "L1_KEYS",
    "L1_LIMIT",
    "aggregate",
    "collect_rows",
    "is_relevant_hit",
    "must_not_violations",
    "score_row",
]


def collect_rows(
    entries: list[GoldenEntry], qdrant, embedder, collection: str, settings
) -> list[dict]:
    """Retrieve (limit=8, same depth as the answer path) and score every
    entry. Live-stack tier; pure helpers above are unit-tested without it."""
    from mainframe_rag.retrieve.query import search as retrieve_search

    rows: list[dict] = []
    for entry in entries:
        hits, _kind, _timings = retrieve_search(
            qdrant, embedder, collection, entry.query, limit=L1_LIMIT, settings=settings
        )
        rows.append(score_row(hits, entry))
    return rows
