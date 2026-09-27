#!/usr/bin/env python3
"""L2 command composition; reusable measurements live in eval.answer_tier."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Preserve the supported uninstalled-checkout script entry.
_SOURCE = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from mainframe_rag.eval.answer_tier import (  # noqa: F401 — compatibility exports
    _INLINE_CITE_RE,
    _AlertCapture,
    _by_class_pr,
    _by_complexity_truncation,
    _by_failure_histogram,
    _by_why,
    _faithfulness_by_class,
    apply_l2_measurements,
    gate_l2,
    run_l2,
    summarize_l2,
    syntax_check,
    write_summary,
)
from mainframe_rag.eval.answers import (  # noqa: F401 — compatibility exports
    AnswerCapture,
    answer_completeness,
    failure_bucket,
    inferred_index_off_gold,
    run_query,
    select_sample,
    why_mode,
)
from mainframe_rag.eval.datasets import VenueError, require_rc_for_collection, resolve_golden_paths
from mainframe_rag.eval.judging import (  # noqa: F401 — compatibility exports
    CITE_PREFIX_RE,
    JSON_BLOCK_RE,
    JUDGE_LABELS,
    JUDGE_MAX_EVIDENCE_CHARS,
    JUDGE_REASONING_EFFORT,
    RELEVANCE_LABELS,
    JudgeError,
    _parse_label,
    citation_to_hit,
    cited_doc_ids,
    evidence_for_citations,
    judge_chat,
    judge_messages,
    parse_judge_label,
    parse_relevance_label,
    precision_recall,
    relevance_messages,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harness L2: answer tier on the live GPU stack")
    parser.add_argument("--golden", type=Path, action="append", default=None,
                        help="golden JSONL path (repeatable; default: dev golden, +holdout under VENUE=rc)")
    parser.add_argument("--max-queries", type=int, default=24,
                        help="deterministic stratified sample size (default 24)")
    parser.add_argument("--all", action="store_true", help="run every golden entry (slow: one reasoning call each)")
    parser.add_argument("--no-judge", action="store_true",
                        help="skip the faithfulness judge (its infra errors gate; use only to isolate failures)")
    parser.add_argument("--out", type=Path, default=None, help="JSON report path")
    parser.add_argument("--summary", type=Path, default=None, help="markdown summary path")
    args = parser.parse_args(argv)

    from mainframe_rag.config import load_settings

    try:
        golden_paths = resolve_golden_paths(args.golden)
        require_rc_for_collection(load_settings().qdrant_collection)
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
        print("L2 FAILED: duplicate entry ids across golden files", file=sys.stderr)
        return 1

    t0 = time.monotonic()
    results, metrics = run_l2(entries, None if args.all else args.max_queries, judge_enabled=not args.no_judge)
    metrics["wall_s"] = round(time.monotonic() - t0, 1)

    report = {"metrics": metrics, "results": results}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        write_summary(args.summary, results, metrics)

    try:
        from mainframe_rag.config import load_settings
        from mainframe_rag.manifest import write_run_manifest

        manifest = write_run_manifest("harness_l2", load_settings(), metrics)
        print(f"run manifest appended ({manifest['git_sha'][:8]})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — manifest is observability, never the gate
        print(f"warn: failed to append run manifest: {exc}", file=sys.stderr)

    verdict, reasons = gate_l2(metrics)
    print(f"[*] L2 VERDICT: {verdict}", file=sys.stderr)
    for r in reasons:
        print(f"    - {r}", file=sys.stderr)
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
