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
    ``real_manuals`` collection require ``VENUE=rc`` (``eval.datasets``).
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
# Preserve the supported uninstalled-checkout command; no sibling CLI imports.
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from mainframe_rag.eval.answer_tier import run_l2
from mainframe_rag.eval.datasets import (
    DatasetError,
    read_golden_text,
    require_rc_for_collection,
    resolve_golden_paths,
)

DEFAULT_THRESHOLDS = REPO / "evals" / "harness-l4-thresholds.json"

from mainframe_rag.eval.quality import (
    ThresholdError,
    build_review_queue,
    gate_l4,
    load_thresholds,
    record_blockers,
    save_thresholds,
    summarize_l4,
    write_summary,
)


def run_l4(entries: list[dict[str, Any]], max_queries: int | None, repeats: int) -> list[dict[str, Any]]:
    """K live passes of the same deterministic sample through the L2 runner
    with the relevance leg on."""
    runs: list[dict[str, Any]] = []
    for k in range(1, repeats + 1):
        print(f"[*] L4 repeat {k}/{repeats}", file=sys.stderr)
        rows, metrics = run_l2(entries, max_queries, judge_enabled=True, relevance_enabled=True)
        runs.append({"rows": rows, "metrics": metrics})
    return runs


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
        entries: list[dict[str, Any]] = []
        for p in golden_paths:
            for line in read_golden_text(p).splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append(json.loads(line))
    except DatasetError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
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
            print("Re-record on this tier: sh scripts/tools/run-task.sh eval:harness:l4-record", file=sys.stderr)
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
