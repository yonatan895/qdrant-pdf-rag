"""Pure retrieval promotion verdicts over recorded candidate/baseline metrics.

Operational snapshot management and baseline persistence belong to the harness.
"""

from __future__ import annotations

from typing import Any

from mainframe_rag.eval.statistics import ci95_paired, ci_excludes_zero

PRIMARY_METRICS = ("recall@5", "mrr")
DEFAULT_CLASS_FLOOR = 0.05


# --------------------------------------------------------------- gate verdict
def gate_verdict(
    candidate: dict[str, Any],
    baseline: dict[str, Any] | None,
    *,
    resamples: int = 2000,
    seed: int = 0,
) -> tuple[str, list[str]]:
    """Verdict for the promotion gate: "baseline" (nothing to compare),
    "merge", or "hold" (with reasons). Pure function.

    Pairing joins per-query values by entry id; entries present in only one
    side are skipped (they still fail the per-class/P0 checks through the
    aggregates)."""
    if baseline is None:
        return "baseline", ["no baseline recorded; candidate stored as the first baseline"]

    recorded_n = (baseline.get("_meta") or {}).get("golden_entries")
    checked = (candidate.get("traps") or {}).get("checked")
    if recorded_n is not None and checked is not None and recorded_n != checked:
        return "hold", [
            (
                f"golden entry-set mismatch: baseline {recorded_n}, candidate {checked} — "
                "a truncated venue must not be scored (declare VENUE=rc for the holdout venue)"
            )
        ]

    reasons: list[str] = []

    cand_traps = candidate.get("traps", {})
    failed = cand_traps.get("failed") or []
    if failed:
        reasons.append(f"P0 trap failures: {failed}")

    floor = float(baseline.get("_meta", {}).get("class_regression_floor", DEFAULT_CLASS_FLOOR))
    base_classes = baseline.get("classes", {})
    cand_classes = candidate.get("classes", {})
    for cls in sorted(set(base_classes) & set(cand_classes)):
        for metric in ("recall@5", "mrr"):
            b = base_classes[cls].get(metric)
            c = cand_classes[cls].get(metric)
            if b is None or c is None:
                continue
            if b - c > floor:
                reasons.append(
                    f"class regression: {cls} {metric} {b} -> {c} (floor {floor})"
                )

    base_pq = baseline.get("per_query", {})
    cand_pq = candidate.get("per_query", {})
    improvements: list[str] = []
    for metric in PRIMARY_METRICS:
        pairs = [
            (cand_pq[eid][metric], base_pq[eid][metric])
            for eid in sorted(set(cand_pq) & set(base_pq))
            if metric in cand_pq.get(eid, {}) and metric in base_pq.get(eid, {})
        ]
        if not pairs:
            reasons.append(f"no paired values for {metric}; CI overlap cannot be evaluated")
            continue
        ci = ci95_paired(pairs, resamples=resamples, seed=seed)
        if ci is None:  # pragma: no cover — pairs is non-empty here
            continue
        if ci_excludes_zero(ci, improvement=True):
            improvements.append(f"{metric} {tuple(round(x, 4) for x in ci)}")
    if not improvements:
        reasons.append(
            "no primary metric improved beyond CI overlap ("
            + ", ".join(PRIMARY_METRICS)
            + ")"
        )

    if reasons:
        return "hold", reasons
    return "merge", improvements
