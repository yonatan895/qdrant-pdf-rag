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
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from eval_retrieval import GoldenEntry, is_relevant_hit, must_not_violations
from eval_retrieval import ndcg_at_k as _ndcg_at_k

L1_LIMIT = 8  # recall@8 headroom; matches the answer path's retrieval depth


def score_row(hits: Sequence[Any], entry: GoldenEntry) -> dict[str, Any]:
    """Per-entry L1 metrics. Pure function (hits in, dict out) so hermetic
    tests can fire every branch."""
    abstain = entry.expected_behavior == "abstain"
    row: dict[str, Any] = {
        "id": entry.id,
        "query_class": entry.query_class,
        "expected_behavior": entry.expected_behavior,
    }
    violations = must_not_violations(list(hits), entry)
    if violations:
        row["violations"] = violations
    if abstain:
        row["top_scores"] = [round(h.score, 4) for h in hits[:5]]
        return row
    reciprocal_rank = 0.0
    for rank, hit in enumerate(hits[:L1_LIMIT], 1):
        if is_relevant_hit(hit.doc_id, hit.heading, entry):
            reciprocal_rank = 1.0 / rank
            break
    row["recall@5"] = 1.0 if any(is_relevant_hit(h.doc_id, h.heading, entry) for h in hits[:5]) else 0.0
    row["recall@8"] = 1.0 if any(is_relevant_hit(h.doc_id, h.heading, entry) for h in hits[:L1_LIMIT]) else 0.0
    row["mrr"] = reciprocal_rank
    ndcg = _ndcg_at_k(hits, entry)
    if ndcg is not None:
        row["ndcg@8"] = round(ndcg, 4)
    return row


L1_KEYS = ("recall@5", "recall@8", "mrr", "ndcg@8")


def _mean(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if key in r]
    return round(sum(vals) / len(vals), 4) if vals else None


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-class + overall L1 summary. Aggregates are reported NEXT TO the
    per-class breakdown and per-query values (paired-delta inputs for the
    gate), never instead of them."""
    scored = [r for r in rows if "recall@5" in r]
    trap_failed = [r["id"] for r in rows if r.get("violations")]

    def block(sub: list[dict]) -> dict[str, Any]:
        return {
            "n": len(sub),
            "scored": len([r for r in sub if "recall@5" in r]),
            **{k: _mean(sub, k) for k in L1_KEYS},
        }

    classes: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda x: x["id"]):
        classes.setdefault(r["query_class"], []).append(r)
    per_query = {
        r["id"]: {k: r[k] for k in L1_KEYS if k in r}
        for r in sorted(rows, key=lambda x: x["id"])
    }
    return {
        "overall": block(scored),
        "classes": {cls: block(sub) for cls, sub in sorted(classes.items())},
        "traps": {
            "checked": len(rows),
            "failed": trap_failed,
            "precision": round(1.0 - len(trap_failed) / len(rows), 4) if rows else None,
        },
        "per_query": per_query,
    }


def collect_rows(
    entries: list[GoldenEntry], qdrant, embedder, collection: str, settings
) -> list[dict[str, Any]]:
    """Retrieve (limit=8, same depth as the answer path) and score every
    entry. Live-stack tier; pure helpers above are unit-tested without it."""
    from mainframe_rag.retrieve.query import search as retrieve_search

    rows: list[dict[str, Any]] = []
    for entry in entries:
        hits, _kind, _timings = retrieve_search(
            qdrant, embedder, collection, entry.query, limit=L1_LIMIT, settings=settings
        )
        rows.append(score_row(hits, entry))
    return rows
