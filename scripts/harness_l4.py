#!/usr/bin/env python3
"""Harness L4 — answer-quality gate: L2 measurements + relevance, repeated.

Where it sits
    L2 (harness_l2.py) measures the answer tier and gates only structural
    fails; its rates are trend data because reasoning-model sampling is not
    run-deterministic. L4 turns those rates into an RC gate without gating
    noise: it runs the same deterministic stratified sample K times
    (`--repeats`, default 3) through the L2 runner (one judging path, plus
    the answer-relevance leg), and compares the per-metric mean against a
    committed reference (``evals/harness-l4-thresholds.json``) with a
    tolerance band:

      * at or better than the reference  -> pass
      * inside the band short of it      -> hold + human-review queue
      * outside the band                 -> fail
      * structural fails, request errors, and judge infra errors fail in
        any repeat, unconditionally.

    The deterministic PR gate (``gate-l1``, harness L1) is untouched: L4 is
    an RC-only tier, never a PR gate. The judge is the local reasoning
    model over the existing stack — fully offline.

Venue (issue #268)
    Dev defaults to ``evals/golden.jsonl`` only; the frozen holdout and the
    ``real_manuals`` collection require ``VENUE=rc`` (``scripts/venue.py``).
    The reference file is checked against the live venue/embed/reasoning
    model before any GPU spend — a reference from a different judge tier is
    not comparable and fails closed.

Exit codes: 0 pass; 1 hold (borderline, queue written) or fail; 2 the
reference cannot be applied (missing/malformed file or env mismatch) —
a skip is not a pass. Recording (`--update-thresholds`) refuses broken
measurement (request/judge errors, uncomputed metrics) but records through
product structural debt: the structural count lands in `_meta` and still
gates every run on its own.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from harness_l2 import RELEVANCE_LABELS, run_l2
from venue import VenueError, require_rc_for_collection, resolve_golden_paths

DEFAULT_THRESHOLDS = REPO / "evals" / "harness-l4-thresholds.json"

# Sampling-noise band for the rate reference. With the default N=24 the
# judged rate denominators are ~15-25 rows; a single run's binomial sigma
# is ~0.10 at p=0.5 and ~0.065 for a 3-repeat mean, so a 0.05 band flags
# noise as regressions (measured: two metrics flipped on re-run of the same
# tier). 0.15 is ~2.3 sigma — honest for this sample size; raise N to
# tighten it. The reference stores the live value in _meta.tolerance.
DEFAULT_TOLERANCE = 0.15

# (metric path in the L2 metrics dict, gate direction). Every key must be
# present in the reference: a missing key would silently stop gating it.
GATED_METRICS: tuple[tuple[str, str], ...] = (
    ("grounded_rate", "min"),
    ("citation_precision", "min"),
    ("citation_recall", "min"),
    ("truncation_rate", "max"),
    ("syntax_compliance", "min"),
    ("faithfulness.entailed", "min"),
    ("faithfulness.contradiction", "max"),
    ("relevance.relevant", "min"),
    ("relevance.irrelevant", "max"),
)


class ThresholdError(RuntimeError):
    """The L4 reference is missing, malformed, or from another tier."""


def get_nested(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def load_thresholds(path: Path) -> dict[str, Any]:
    """Load + validate the reference. Fail closed on anything malformed —
    a gate that cannot name its reference must not score."""
    if not path.exists():
        raise ThresholdError(
            f"no L4 reference at {path}; record one on the RC host with `make harness-l4-record`"
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ThresholdError(f"unreadable L4 reference {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ThresholdError(f"L4 reference {path}: top level must be an object")
    meta = doc.get("_meta")
    if not isinstance(meta, dict):
        raise ThresholdError(f"L4 reference {path}: missing _meta")
    tolerance = meta.get("tolerance")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 1:
        raise ThresholdError(f"L4 reference {path}: _meta.tolerance must be a number in [0, 1]")
    for key in ("venue", "embed_mode", "llm_model_reasoning"):
        if not isinstance(meta.get(key), str) or not meta[key]:
            raise ThresholdError(f"L4 reference {path}: _meta.{key} is required")
    metrics = doc.get("metrics")
    if not isinstance(metrics, dict):
        raise ThresholdError(f"L4 reference {path}: missing metrics object")
    expected = {name for name, _ in GATED_METRICS}
    if set(metrics) != expected:
        missing = sorted(expected - set(metrics))
        extra = sorted(set(metrics) - expected)
        raise ThresholdError(
            f"L4 reference {path}: metric keys must match exactly (missing {missing}, unknown {extra})"
        )
    for name, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ThresholdError(f"L4 reference {path}: metric {name!r} must be a rate in [0, 1]")
    return doc


def mean_metric(runs: list[dict[str, Any]], dotted: str) -> float | None:
    """Mean of one metric across repeats. None when any repeat lacks it —
    a rate that only half the repeats computed cannot be gated."""
    values: list[float] = []
    for run in runs:
        value = get_nested(run["metrics"], dotted)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        values.append(float(value))
    if not values:
        return None
    return sum(values) / len(values)


def summarize_l4(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate repeats: structural counts are sums (any occurrence gates),
    rates are means (sampling noise is the reason for repeats)."""
    if not runs:
        raise ValueError("L4 needs at least one run")
    return {
        "repeats": len(runs),
        "queries_per_run": runs[0]["metrics"].get("queries", 0),
        "structural_fails": sum(r["metrics"]["structural_fails"] for r in runs),
        "errors": sum(r["metrics"]["errors"] for r in runs),
        "judge_errors": sum(
            r["metrics"]["faithfulness"]["judge_errors"] + r["metrics"]["relevance"]["judge_errors"]
            for r in runs
        ),
        "metrics": {name: mean_metric(runs, name) for name, _ in GATED_METRICS},
        "per_run": [r["metrics"] for r in runs],
    }


def classify(value: float, reference: float, tolerance: float, direction: str) -> str:
    """pass / borderline / fail for one metric against its reference band."""
    if direction == "min":
        if value >= reference:
            return "pass"
        if value >= reference - tolerance:
            return "borderline"
        return "fail"
    if value <= reference:
        return "pass"
    if value <= reference + tolerance:
        return "borderline"
    return "fail"


def gate_l4(summary: dict[str, Any], thresholds: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """Verdict from the repeat summary. Fail-closed: structural faults fail
    even when every rate is healthy; an uncomputed metric fails rather than
    vanishing from the gate."""
    tolerance = float(thresholds["_meta"]["tolerance"])
    refs = thresholds["metrics"]
    failures: list[str] = []
    borderline: list[str] = []
    if summary["structural_fails"]:
        failures.append(f"{summary['structural_fails']} structural failure(s)")
    if summary["errors"]:
        failures.append(f"{summary['errors']} request error(s)")
    if summary["judge_errors"]:
        failures.append(f"{summary['judge_errors']} judge infra error(s)")
    for name, direction in GATED_METRICS:
        value = summary["metrics"].get(name)
        if value is None:
            failures.append(f"{name}: not computed in every repeat")
            continue
        verdict = classify(float(value), float(refs[name]), tolerance, direction)
        if verdict == "fail":
            failures.append(
                f"{name} {value:.4f} outside the {direction} band of reference "
                f"{refs[name]:.4f} (tolerance {tolerance})"
            )
        elif verdict == "borderline":
            borderline.append(name)
    if failures:
        return "fail", failures, borderline
    if borderline:
        return "hold", [f"borderline metric(s): {', '.join(borderline)}"], borderline
    return "pass", [], borderline


_FAITHFULNESS_IDEAL = ("entailed",)
_RELEVANCE_IDEAL = ("relevant",)


def build_review_queue(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows that are not clean in every repeat: failures, non-ideal judge
    labels, or truncation. The queue is where a human adjudicates a hold —
    per-repeat verdicts/labels/citations, never manual text."""
    by_id: dict[str, dict[str, Any]] = {}
    for k, run in enumerate(runs, 1):
        for row in run["rows"]:
            rec = by_id.setdefault(
                row["id"],
                {
                    "id": row["id"],
                    "query": row.get("query"),
                    "query_class": row.get("query_class"),
                    "expected_behavior": row.get("expected_behavior"),
                    "verdicts": [],
                    "faithfulness": [],
                    "relevance": [],
                    "truncated": [],
                    "citation_precision": [],
                    "citation_recall": [],
                    "citations": row.get("citations"),
                    "failures": [],
                },
            )
            rec["verdicts"].append(row.get("verdict"))
            if row.get("judge_label") in ("entailed", "neutral", "contradiction"):
                rec["faithfulness"].append(row["judge_label"])
            if row.get("relevance_label") in RELEVANCE_LABELS:
                rec["relevance"].append(row["relevance_label"])
            if row.get("truncated") is not None:
                rec["truncated"].append(row["truncated"])
            for key in ("citation_precision", "citation_recall"):
                if row.get(key) is not None:
                    rec[key].append(row[key])
            for failure in row.get("failures") or []:
                rec["failures"].append(f"run {k}: {failure}")
    queue = []
    for rec in sorted(by_id.values(), key=lambda r: r["id"]):
        weak = (
            any(v in ("fail", "error") for v in rec["verdicts"])
            or any(lbl not in _FAITHFULNESS_IDEAL for lbl in rec["faithfulness"])
            or any(lbl not in _RELEVANCE_IDEAL for lbl in rec["relevance"])
            or any(t is True for t in rec["truncated"])
            or bool(rec["failures"])
        )
        if weak:
            queue.append(rec)
    return queue


def write_summary(
    path: Path,
    summary: dict[str, Any],
    verdict: str,
    reasons: list[str],
    thresholds: dict[str, Any],
    queue_path: Path,
    queue_n: int,
) -> None:
    tolerance = thresholds["_meta"]["tolerance"]
    refs = thresholds["metrics"]
    lines: list[str] = [
        "# Harness L4 — answer-quality gate",
        "",
        f"- repeats: {summary['repeats']} × {summary['queries_per_run']} queries",
        (
            f"- structural fails: {summary['structural_fails']}, errors: {summary['errors']}, "
            f"judge errors: {summary['judge_errors']}"
        ),
        (
            f"- reference: {thresholds['_meta'].get('updated')} "
            f"(venue {thresholds['_meta'].get('venue')}, tolerance {tolerance})"
        ),
        f"- human-review queue: {queue_n} row(s) -> {queue_path.name}",
        f"- VERDICT: {verdict}",
        "",
        "| metric | direction | reference | mean | verdict |",
        "|---|---|---|---|---|",
    ]
    for name, direction in GATED_METRICS:
        value = summary["metrics"].get(name)
        shown = f"{value:.4f}" if value is not None else "—"
        if value is None:
            cell = "fail (uncomputed)"
        else:
            cell = classify(float(value), float(refs[name]), tolerance, direction)
        lines.append(f"| {name} | {direction} | {refs[name]:.4f} | {shown} | {cell} |")
    if reasons:
        lines.append("")
        lines.append("## Reasons")
        lines.extend(f"- {r}" for r in reasons)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_l4(entries: list[dict[str, Any]], max_queries: int | None, repeats: int) -> list[dict[str, Any]]:
    """K live passes of the same deterministic sample through the L2 runner
    with the relevance leg on."""
    runs: list[dict[str, Any]] = []
    for k in range(1, repeats + 1):
        print(f"[*] L4 repeat {k}/{repeats}", file=sys.stderr)
        rows, metrics = run_l2(entries, max_queries, judge_enabled=True, relevance_enabled=True)
        runs.append({"rows": rows, "metrics": metrics})
    return runs


def record_blockers(summary: dict[str, Any]) -> list[str]:
    """Why a run must not become the rate reference. Product structural
    debt is deliberately not a blocker: it gates every L4 run on its own."""
    blockers: list[str] = []
    if summary["errors"]:
        blockers.append(f"{summary['errors']} request error(s)")
    if summary["judge_errors"]:
        blockers.append(f"{summary['judge_errors']} judge infra error(s)")
    uncomputed = [name for name, _ in GATED_METRICS if summary["metrics"].get(name) is None]
    if uncomputed:
        blockers.append(f"uncomputed metric(s): {', '.join(uncomputed)}")
    return blockers


def save_thresholds(path: Path, summary: dict[str, Any], settings: Any, repeats: int) -> dict[str, Any]:
    doc = {
        "_meta": {
            "note": (
                "L4 answer-quality reference rates; record with `make harness-l4-record` "
                "(dedicated PR, AGENTS.md). Gate compares repeat means against these with "
                "_meta.tolerance (default 0.15 = ~2.3 sigma of the 3-repeat mean at N=24; "
                "raise N to tighten). An uncomputed metric fails. Structural fails gate "
                "independently of the rate reference and are stored for context; grounding "
                "counts explicit citations only (#269)."
            ),
            "updated": time.strftime("%Y-%m-%d", time.gmtime()),
            "venue": settings.qdrant_collection,
            "embed_mode": settings.embed_mode,
            "llm_model_reasoning": settings.llm_model_reasoning,
            "tolerance": DEFAULT_TOLERANCE,
            "repeats": repeats,
            "queries_per_run": summary["queries_per_run"],
            "structural_fails": summary["structural_fails"],
        },
        "metrics": {name: round(float(value), 4) for name, value in summary["metrics"].items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harness L4: repeated answer-quality gate (RC only)")
    parser.add_argument("--golden", type=Path, action="append", default=None,
                        help="golden JSONL path (repeatable; default: dev golden, +holdout under VENUE=rc)")
    parser.add_argument("--max-queries", type=int, default=24,
                        help="deterministic stratified sample size per repeat (default 24)")
    parser.add_argument("--all", action="store_true", help="run every golden entry (slow)")
    parser.add_argument("--repeats", type=int, default=3, help="live passes of the sample (default 3)")
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS,
                        help="reference file (default: evals/harness-l4-thresholds.json)")
    parser.add_argument("--update-thresholds", action="store_true",
                        help="record the measured means as the reference (dedicated PR)")
    parser.add_argument("--out", type=Path, default=None, help="JSON report path")
    parser.add_argument("--summary", type=Path, default=None, help="markdown summary path")
    parser.add_argument("--queue", type=Path, default=None, help="human-review queue JSON path")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")

    from mainframe_rag.config import load_settings

    settings = load_settings()
    try:
        golden_paths = resolve_golden_paths(args.golden)
        require_rc_for_collection(settings.qdrant_collection)
    except VenueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    entries: list[dict[str, Any]] = []
    for p in golden_paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(json.loads(line))
    if len({e["id"] for e in entries}) != len(entries):
        print("L4 FAILED: duplicate entry ids across golden files", file=sys.stderr)
        return 1

    thresholds: dict[str, Any] | None = None
    if not args.update_thresholds:
        try:
            thresholds = load_thresholds(args.thresholds)
        except ThresholdError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 2
        meta = thresholds["_meta"]
        mismatches = [
            f"reference {key}={meta.get(key)!r} != live {live!r}"
            for key, live in (
                ("venue", settings.qdrant_collection),
                ("embed_mode", settings.embed_mode),
                ("llm_model_reasoning", settings.llm_model_reasoning),
            )
            if meta.get(key) != live
        ]
        if mismatches:
            print("FAIL: L4 reference recorded in a different tier:", file=sys.stderr)
            for m in mismatches:
                print(f"  - {m}", file=sys.stderr)
            print("Re-record on this tier: make harness-l4-record", file=sys.stderr)
            return 2

    t0 = time.monotonic()
    runs = run_l4(entries, None if args.all else args.max_queries, args.repeats)
    summary = summarize_l4(runs)
    summary["wall_s"] = round(time.monotonic() - t0, 1)

    if args.update_thresholds:
        blockers = record_blockers(summary)
        if blockers:
            print(
                "ERROR: refusing to record the L4 reference: broken measurement "
                f"({'; '.join(blockers)}). A broken run must not become the reference.",
                file=sys.stderr,
            )
            return 1
        if summary["structural_fails"]:
            # Product debt is a finding, not a broken measurement: the rate
            # reference records through it (the structural count is stored in
            # _meta and still gates every L4 run).
            print(
                f"[!] recording through {summary['structural_fails']} structural failure(s) "
                "— they keep gating L4 independently of the rate reference",
                file=sys.stderr,
            )
        recorded_ref = save_thresholds(args.thresholds, summary, settings, args.repeats)
        print(f"[*] L4 reference recorded: {args.thresholds}", file=sys.stderr)
        print(json.dumps(recorded_ref["metrics"], indent=1), file=sys.stderr)
        verdict, reasons = "baseline", ["reference recorded"]
    else:
        assert thresholds is not None
        verdict, reasons, _borderline = gate_l4(summary, thresholds)

    queue = build_review_queue(runs)
    queue_path = args.queue or Path("bundles/harness-l4-review-queue.json")
    queue_doc = {
        "_meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "verdict": verdict,
            "repeats": args.repeats,
            "reasons": reasons,
        },
        "rows": queue,
    }
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(json.dumps(queue_doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    report = {"summary": summary, "verdict": verdict, "reasons": reasons, "queue_n": len(queue),
              "runs": runs}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        reference = thresholds if thresholds is not None else recorded_ref
        write_summary(args.summary, summary, verdict, reasons, reference, queue_path, len(queue))

    try:
        from mainframe_rag.manifest import write_run_manifest

        manifest = write_run_manifest("harness_l4", settings, summary)
        print(f"run manifest appended ({manifest['git_sha'][:8]})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — manifest is observability, never the gate
        print(f"warn: failed to append run manifest: {exc}", file=sys.stderr)

    print(f"[*] L4 VERDICT: {verdict}", file=sys.stderr)
    for reason in reasons:
        print(f"    - {reason}", file=sys.stderr)
    print(f"[*] review queue: {len(queue)} row(s) -> {queue_path}", file=sys.stderr)
    return 0 if verdict in ("pass", "baseline") else 1


if __name__ == "__main__":
    sys.exit(main())
