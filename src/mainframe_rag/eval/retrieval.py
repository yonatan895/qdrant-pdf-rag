"""Retrieval scoring and L1 aggregation (issue #508 C2).

Canonical owner for the retrieval-eval machinery (same ``retrieve_search``,
same golden schema, same must_not sibling allowance) plus the harness L1
metric set. Mechanical move from ``scripts/eval_retrieval.py`` pure helpers
and ``scripts/harness_l1.py`` scoring/aggregation: no metric, default, gate
or label change.

Per-class/trap/paired-query semantics stay explicit; there is exactly one
relevance implementation (``is_relevant_hit``), one sibling allowance
(``is_sibling_exception``), one graded nDCG (``ndcg_at_k``).

Pure module: no live imports, no I/O, no ``sys.path`` mutation, no global
environment changes at import. Product hit types are referenced under
``TYPE_CHECKING`` only; ``find_message_ids`` (shared identifier regexes) is
the single product import and is itself pure. Live execution
(``evaluate``/``collect_rows``/``label_draft``/``main``) stays in the script
delegates until its family moves.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from mainframe_rag.eval.datasets import RC_ONLY_COLLECTIONS, GoldenEntry
from mainframe_rag.regexes import find_message_ids

if TYPE_CHECKING:
    from mainframe_rag.retrieve.query import SearchHit

SEARCH_LIMIT = 8  # headroom for recall@5
MUST_NOT_WINDOW = 5  # must_not violations are gated inside the top-5

L1_LIMIT = 8  # recall@8 headroom; matches the answer path's retrieval depth
L1_KEYS = ("recall@5", "recall@8", "mrr", "ndcg@8")


def is_sibling_exception(query_ids: set[str], doc_message_ids) -> bool:
    """Sibling-precision allowance, shared by must_not_violations (retrieval
    eval + harness L1) and verify_golden: a chunk/doc that carries a bait
    message id ALONGSIDE the query's own id is one page documenting adjacent
    messages (e.g. IOS207I and IOS208I share a page) — not a wrong-sibling
    answer. Only sibling-only payloads violate."""
    return bool(set(query_ids) & set(doc_message_ids or ()))


def is_relevant_hit(hit_doc_id: str, hit_heading: str, entry: GoldenEntry) -> bool:
    """Doc-level relevance, shared by score_entry and harness L1: doc_id in
    the expected set AND heading substring (if given)."""
    if hit_doc_id not in set(entry.expected_doc_ids):
        return False
    heading = (entry.expected_heading or "").lower()
    return not heading or heading in hit_heading.lower()


def must_not_violations(hits: list[SearchHit], entry: GoldenEntry) -> list[dict]:  # type: ignore[valid-type]
    """Collect must_not violations inside the top-5 window.

    Sibling-precision allowance (same rule as the corpus builder's trap
    assertion): a chunk that carries a bait message id ALONGSIDE the query's
    own id is one page documenting adjacent messages (e.g. IOS207I and
    IOS208I share a page in both editions of System Messages Vol 8) — not a
    wrong-sibling answer. Only sibling-only chunks violate. Without this,
    the chunk-level gate is stricter than the trap's documented intent and
    every adjacent-message page in a multi-edition corpus trips it. Shared
    by the retrieval eval and the harness L1 so the allowance cannot
    diverge between the two gates."""
    query_ids = set(find_message_ids(entry.query))
    violations: list[dict] = []
    for rank, hit in enumerate(hits[:MUST_NOT_WINDOW], 1):
        if hit.doc_id in set(entry.must_not_retrieve):
            violations.append({"type": "doc_id", "value": hit.doc_id, "rank": rank})
        hit_msgs = set(hit.message_ids or ()) & set(entry.must_not_message_ids)
        if hit_msgs and not is_sibling_exception(query_ids, hit.message_ids):
            violations.append(
                {"type": "message_id", "value": sorted(hit_msgs), "rank": rank, "doc_id": hit.doc_id}
            )
    return violations


def gain(hit_doc_id: str, hit_heading: str, hit_page: str, entry: GoldenEntry) -> int:
    """Graded gain for one hit: 1 for a doc hit, +1 heading, +1 page."""
    if hit_doc_id not in set(entry.expected_doc_ids):
        return 0
    g = 1
    heading = (entry.expected_heading or "").lower()
    if heading and heading in (hit_heading or "").lower():
        g += 1
    page = entry.expected_page or ""
    if page and hit_page == page:
        g += 1
    return g


def ndcg_at_k(hits: Sequence[Any], entry: GoldenEntry, k: int = SEARCH_LIMIT) -> float | None:
    """Doc-level nDCG@k against the entry's own ideal (every expected doc at
    max gain). The hit list is DEDUPLICATED per doc_id (best-ranked chunk
    wins) — otherwise N chunks of one expected doc each contribute gain and
    DCG can exceed IDCG (nDCG > 1), which is meaningless. Doc-level ranking
    matches the doc-level gold. None when the entry has no expected docs
    (abstain rows)."""
    expected_n = len(entry.expected_doc_ids)
    if expected_n == 0:
        return None
    max_gain = 1
    if entry.expected_heading:
        max_gain += 1
    if entry.expected_page:
        max_gain += 1
    seen: set[str] = set()
    dcg = 0.0
    rank = 0
    for hit in hits:
        doc_id = getattr(hit, "doc_id", "")
        heading = getattr(hit, "heading", "")
        page_label = getattr(hit, "page_label", "") or ""
        if doc_id in seen:
            continue
        seen.add(doc_id)
        rank += 1
        if rank > k:
            break
        g = gain(doc_id, heading, page_label, entry)
        dcg += g / math.log2(rank + 1)
    ideal_gains = [max_gain] + [1] * (expected_n - 1)
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal_gains[:k], start=1))
    if idcg == 0.0:
        return None
    return dcg / idcg


def score_entry(hits: list[SearchHit], entry: GoldenEntry) -> dict:  # type: ignore[valid-type]
    """Score one entry against retrieved hits.

    Relevance = doc_id in expected set AND heading substring (if given).
    Abstain entries produce no recall/MRR keys (excluded from the
    denominators) and record their top scores for calibration. must_not
    violations are collected inside the MUST_NOT_WINDOW window for both
    doc IDs and message-ID payloads, with the sibling-precision allowance:
    a chunk co-carrying the query's own message id is the same documented
    page, never a wrong-sibling answer. expected_page feeds the page_hit@5
    diagnostic (doc-restricted; never a hard gate)."""
    expected = set(entry.expected_doc_ids)
    abstain = entry.expected_behavior == "abstain"

    def relevant(hit: SearchHit) -> bool:  # type: ignore[valid-type]
        return is_relevant_hit(hit.doc_id, hit.heading, entry)

    row: dict = {
        "query": entry.query,
        "id": entry.id,
        "query_class": entry.query_class,
        "expected_behavior": entry.expected_behavior,
        "hit_doc_ids": [h.doc_id for h in hits[:3]],
    }

    if abstain:
        row["top_scores"] = [round(h.score, 4) for h in hits[:5]]
    else:
        reciprocal_rank = 0.0
        for rank, hit in enumerate(hits, 1):
            if relevant(hit):
                reciprocal_rank = 1.0 / rank
                break
        row["recall@1"] = 1.0 if hits[:1] and relevant(hits[0]) else 0.0
        row["recall@3"] = 1.0 if any(relevant(h) for h in hits[:3]) else 0.0
        row["recall@5"] = 1.0 if any(relevant(h) for h in hits[:5]) else 0.0
        row["recall@8"] = 1.0 if any(relevant(h) for h in hits[:8]) else 0.0
        row["mrr"] = reciprocal_rank
        ndcg = ndcg_at_k(hits, entry, k=8)
        if ndcg is not None:
            row["ndcg@8"] = round(ndcg, 4)

    violations = must_not_violations(hits, entry)
    if violations:
        row["violations"] = violations

    if entry.expected_page and expected:
        page_hit = any(
            h.doc_id in expected and h.page_label == entry.expected_page for h in hits[:5]
        )
        row["page_hit@5"] = 1.0 if page_hit else 0.0

    return row


def summarize(
    rows: list[dict],
    *,
    failures: int,
    elapsed_s: float,
    embed_mode: str,
    collection: str,
) -> dict:
    """Pure aggregation over scored rows (unit-testable; no I/O).

    Answer rows carry recall/mrr keys; abstain rows carry top_scores and are
    excluded from the recall/MRR denominators by construction."""
    scored = [r for r in rows if "recall@1" in r]

    def mean_over(sub: list[dict], key: str) -> float | None:
        values = [r[key] for r in sub if key in r]
        return round(sum(values) / len(values), 3) if values else None

    def mean(key: str) -> float:
        value = mean_over(scored, key)
        return 0.0 if value is None else value

    def mean_by_kind(key: str, kind: str) -> float | None:
        return mean_over([r for r in scored if r.get("kind") == kind], key)

    classes: dict[str, dict] = {}
    for cls in sorted({r["query_class"] for r in rows if r.get("query_class")}):
        sub = [r for r in rows if r.get("query_class") == cls]
        classes[cls] = {"n": len(sub), "scored": sum(1 for r in sub if "recall@1" in r)}
        for key in ("recall@1", "recall@3", "recall@5", "recall@8", "mrr", "ndcg@8"):
            classes[cls][key] = mean_over(sub, key)

    abstain_rows = [r for r in rows if r.get("expected_behavior") == "abstain" and "top_scores" in r]
    top_scores = [r["top_scores"][0] for r in abstain_rows if r["top_scores"]]
    abstain_summary = {
        "n": len(abstain_rows),
        "top_score_mean": round(sum(top_scores) / len(top_scores), 4) if top_scores else None,
        "top_score_max": round(max(top_scores), 4) if top_scores else None,
    }

    # The zero gate applies corpus-wide: every scored row is checked.
    violations = [v for r in rows for v in r.get("violations", [])]
    must_not_summary = {
        "checked": len(rows),
        "violations": len(violations),
        "rate": round(len(violations) / len(rows), 4) if rows else 0.0,
    }

    page_rows = [r for r in rows if "page_hit@5" in r]
    page_summary = {
        "checked": len(page_rows),
        "hit@5": mean_over(page_rows, "page_hit@5"),
    }

    return {
        "n": len(rows),
        "scored": len(scored),
        "failures": failures,
        "elapsed_s": elapsed_s,
        "embed_mode": embed_mode,
        "collection": collection,
        "recall@1": mean("recall@1"),
        "recall@3": mean("recall@3"),
        "recall@5": mean("recall@5"),
        "recall@8": mean("recall@8"),
        "mrr": mean("mrr"),
        "ndcg@8": mean("ndcg@8"),
        "identifier": {k: mean_by_kind(k, "identifier") for k in ("recall@1", "recall@5", "recall@8", "mrr", "ndcg@8")},
        "nl": {k: mean_by_kind(k, "nl") for k in ("recall@1", "recall@5", "recall@8", "mrr", "ndcg@8")},
        "classes": classes,
        "abstain": abstain_summary,
        "must_not": must_not_summary,
        "page": page_summary,
        "rows": rows,
    }


EVAL_GATED_METRICS = {
    # dotted path into the report -> minimum allowed ratio vs baseline (1.0 = no drop, 0.95 = 5% margin)
    "recall@1": 0.90,
    "recall@5": 0.95,
    "recall@8": 0.95,
    "mrr": 0.95,
    "ndcg@8": 0.95,
}

# Absolute floors, independent of the recorded baseline: identifier and
# message-ID lookups are safety-critical, so 1.0 means 1.0 (AGENTS.md).
# The 1.0 floor encodes the synthetic venue's saturation; a real-corpus
# baseline (venue.RC_ONLY_COLLECTIONS) is itself below 1.0 by construction,
# so there the same dotted metrics gate as "no drop vs the recorded value"
# (issue #286) — an always-red gate would mask the real signal.
EVAL_ABSOLUTE_GATED_METRICS = {
    "identifier.recall@1": 1.0,
    "classes.message_id.recall@1": 1.0,
}

# Absolute invariants: must be exactly zero whenever baseline checking runs,
# regardless of the recorded baseline (a wrong doc or sibling message ID
# surfacing in the top-5 is a failure even if it happened at baseline time).
EVAL_ZERO_GATED_METRICS = (
    "must_not.violations",
)


def _get(result: dict, dotted: str):
    node: object = result
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(target: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _absolute_floors_apply(baseline: dict) -> bool:
    """Strict 1.0 floors apply to the synthetic instrument only (issue #286).

    The real-corpus holdout baseline records identifier recall below 1.0
    (real manuals, real queries); applying the saturated floor there made
    every gated holdout run red at its own baseline. Baselines without a
    venue meta keep the strict floors (dev/CI and ad-hoc callers).
    """
    meta = baseline.get("_meta")
    collection = meta.get("collection") if isinstance(meta, dict) else None
    return collection not in RC_ONLY_COLLECTIONS


def _finite_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_baseline(report: dict, baseline: dict | None) -> list[str]:
    if baseline is None:
        return []
    regressions: list[str] = []

    def require_number(dotted: str, *, source: dict = report, label: str = "run") -> bool:
        if not _finite_number(_get(source, dotted)):
            regressions.append(f"{dotted}: {label} requires a finite numeric value")
            return False
        return True

    for key in ("n", "scored"):
        if require_number(key) and report[key] <= 0:
            regressions.append(f"{key}: gate requires nonempty eligible scoring")
    if require_number("failures") and report["failures"] > 0:
        regressions.append(f"failures: {report['failures']} > 0 query errors occurred during evaluation")
    for dotted in EVAL_ZERO_GATED_METRICS:
        if require_number(dotted) and _get(report, dotted) != 0:
            regressions.append(f"{dotted}: {_get(report, dotted)} != 0 (absolute gate: must_not hits in the top-{MUST_NOT_WINDOW})")

    # Baseline classes define coverage, not new per-class quality thresholds.
    # Negative/abstain classes can intentionally have zero scored recall rows.
    for cls, expected in baseline.get("classes", {}).items():
        prefix = f"classes.{cls}"
        for key in ("n", "scored"):
            value = expected.get(key)
            if value is not None:
                dotted = f"{prefix}.{key}"
                if (
                    require_number(dotted, source=baseline, label="baseline")
                    and value > 0
                    and require_number(dotted)
                    and _get(report, dotted) <= 0
                ):
                    regressions.append(f"{dotted}: required class has no eligible observations")
        for key in (*EVAL_GATED_METRICS, "recall@3"):
            if expected.get(key) is not None:
                dotted = f"{prefix}.{key}"
                require_number(dotted, source=baseline, label="baseline")
                require_number(dotted)

    absolute_floors = _absolute_floors_apply(baseline)
    for dotted, floor in EVAL_ABSOLUTE_GATED_METRICS.items():
        current, base_val = _get(report, dotted), _get(baseline, dotted)
        # A class absent from both instruments is deliberately unscored.
        if current is None and base_val is None:
            continue
        valid = require_number(dotted)
        if base_val is not None:
            valid = require_number(dotted, source=baseline, label="baseline") and valid
        if not valid:
            continue
        if not absolute_floors:
            if base_val is None:
                regressions.append(f"{dotted}: baseline required for real-corpus no-drop gate")
            elif current < base_val:
                regressions.append(f"{dotted}: {current} < baseline {base_val} (real-corpus gate: identifier lookups must not drop)")
        elif current < floor:
            regressions.append(f"{dotted}: {current} < {floor} (absolute gate: identifier lookups must not drop)")
    for dotted, min_ratio in EVAL_GATED_METRICS.items():
        base_val = _get(baseline, dotted)
        if base_val is None:
            continue
        valid = require_number(dotted, source=baseline, label="baseline")
        valid = require_number(dotted) and valid
        if not valid:
            continue
        current = _get(report, dotted)
        threshold = round(base_val * min_ratio, 3)
        if current < threshold:
            regressions.append(f"{dotted}: {current} < baseline {base_val} (min allowed {threshold} with ratio {min_ratio})")
    return regressions


def update_baseline(report: dict, baseline_path: Path) -> None:
    payload: dict = {
        "_meta": {
            "note": "Re-baseline via `sh scripts/tools/run-task.sh eval:baseline`; dedicated PR (AGENTS.md). Tolerances in mainframe_rag.eval.retrieval.",
            "n": report.get("n", 0),
            "collection": report.get("collection", "local-corpus"),
            "embed_mode": report.get("embed_mode", "hash"),
            "updated": time.strftime("%Y-%m-%d"),
        },
        "recall@1": report.get("recall@1"),
        "recall@3": report.get("recall@3"),
        "recall@5": report.get("recall@5"),
        "recall@8": report.get("recall@8"),
        "mrr": report.get("mrr"),
        "ndcg@8": report.get("ndcg@8"),
        "identifier": report.get("identifier", {}),
        "nl": report.get("nl", {}),
        "classes": report.get("classes", {}),
        "abstain": report.get("abstain", {}),
        "page": report.get("page", {}),
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def summary_markdown(report: dict, baseline: dict | None = None) -> str:
    lines = [
        "## Retrieval eval",
        "",
        (
            f"n={report['n']} failures={report['failures']} mode={report['embed_mode']} "
            f"collection={report['collection']} ({report['elapsed_s']}s)"
        ),
        "",
        "| metric | all | identifier | nl | baseline | gate |",
        "|---|---|---|---|---|---|",
    ]
    for key in ("recall@1", "recall@3", "recall@5", "recall@8", "mrr", "ndcg@8"):
        base_val = _get(baseline, key) if baseline else None
        min_ratio = EVAL_GATED_METRICS.get(key)
        gate = f">= {round(base_val * min_ratio, 3)}" if (_finite_number(base_val) and min_ratio is not None) else "n/a"
        lines.append(
            f"| {key} | {report.get(key)} | {report.get('identifier', {}).get(key)} | {report.get('nl', {}).get(key)} | {base_val} | {gate} |"
        )

    classes = report.get("classes") or {}
    if classes:
        lines += ["", "### Per golden query class", "", "| class | n | scored | r@1 | r@3 | r@5 | r@8 | mrr | ndcg@8 |", "|---|---|---|---|---|---|---|---|---|"]
        for cls, stats in classes.items():
            cells = " | ".join("-" if stats.get(k) is None else str(stats.get(k)) for k in ("recall@1", "recall@3", "recall@5", "recall@8", "mrr", "ndcg@8"))
            lines.append(f"| {cls} | {stats.get('n')} | {stats.get('scored')} | {cells} |")

    abstain = report.get("abstain") or {}
    if abstain.get("n"):
        lines += [
            "",
            (
                f"abstain entries: n={abstain['n']} "
                f"top_score_mean={abstain.get('top_score_mean')} top_score_max={abstain.get('top_score_max')} "
                "(recorded for score-floor calibration; excluded from recall/MRR)"
            ),
        ]
    must_not = report.get("must_not") or {}
    lines.append(
        f"must_not violations: {must_not.get('violations', 0)} (gate: 0 within top-{MUST_NOT_WINDOW}; checked {must_not.get('checked', 0)} rows)"
    )
    page = report.get("page") or {}
    if page.get("checked"):
        lines.append(f"page_hit@5 (diagnostic): {page.get('hit@5')} over {page['checked']} entries with expected_page")

    lines += ["", "| query | class | kind | r@1 | r@5 | mrr | viol | top hit doc_ids |", "|---|---|---|---|---|---|---|---|"]
    for row in report["rows"]:
        if "error" in row:
            lines.append(f"| {row['query'][:60]} | {row.get('query_class') or '-'} | error | - | - | - | - | {row['error'][:40]} |")
        else:
            cells = " | ".join(
                "-" if row.get(k) is None else f"{row[k]:.0f}" if k.startswith("recall") else f"{row[k]:.2f}"
                for k in ("recall@1", "recall@5", "mrr")
            )
            viol = len(row.get("violations", []))
            lines.append(
                f"| {row['query'][:60]} | {row.get('query_class') or '-'} | {row.get('kind', '')} | {cells} | {viol} "
                f"| {', '.join(row['hit_doc_ids'])[:60]} |"
            )
    return "\n".join(lines) + "\n"


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
    ndcg = ndcg_at_k(hits, entry)
    if ndcg is not None:
        row["ndcg@8"] = round(ndcg, 4)
    return row


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

    classes: dict[Any, list[dict[str, Any]]] = {}
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
