#!/usr/bin/env python3
"""Harness L3 — performance & latency tier: per-stage p50/p95, TTFT, VRAM.

Drives concurrent load against the running agent's /v1/search and /v1/answer
endpoints, captures per-stage latency percentiles (embed_ms, qdrant_ms,
llm_ms, ttft_ms) from Server-Timing headers, measures VRAM footprint via
nvidia-smi, and gates regressions against the baseline JSON.

Usage:
    # Run and gate vs baseline:
    python scripts/harness_l3.py --url http://127.0.0.1:8080 --gate \
        --baseline benchmarks/baseline.json --out bundles/harness-l3-report.json

    # Update baseline:
    python scripts/harness_l3.py --url http://127.0.0.1:8080 --update-baseline \
        --baseline benchmarks/baseline.json
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from loadtest import DEFAULT_QUERIES, export_to_baseline, query_vram_mb, run_load


def _get_nested(data: dict[str, Any] | None, dotted: str) -> Any:
    if data is None:
        return None
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def gate_verdict_l3(
    report: dict[str, Any],
    baseline: dict[str, Any] | None,
    tolerance: float = 3.0,
) -> tuple[str, list[str]]:
    """Evaluate L3 performance gate: fails on request errors or p95 regressions."""
    reasons: list[str] = []

    for ep in ("search", "answer"):
        ep_data = report.get(ep, {})
        errors = ep_data.get("errors", 0)
        if errors > 0:
            reasons.append(f"{ep}: {errors} request error(s) under load")

    if baseline is None:
        return ("baseline", ["no baseline recorded"])

    # Check total latency p95
    for ep in ("search", "answer"):
        cur_p95 = _get_nested(report, f"{ep}.latency_ms.p95")
        base_p95 = _get_nested(baseline, f"agent.{ep}.latency_ms.p95")
        if cur_p95 is not None and base_p95 is not None:
            limit = base_p95 * tolerance
            if cur_p95 > limit:
                reasons.append(
                    f"{ep}.latency_ms.p95: {cur_p95}ms > {base_p95}ms x{tolerance} (limit {round(limit, 2)}ms)"
                )

        # Check stage latencies p95
        stages = report.get(ep, {}).get("stages", {})
        for sname, smetrics in stages.items():
            cur_sp95 = smetrics.get("p95")
            base_sp95 = _get_nested(baseline, f"agent.{ep}.stages.{sname}.p95")
            if cur_sp95 is not None and base_sp95 is not None:
                limit = base_sp95 * tolerance
                if cur_sp95 > limit:
                    reasons.append(
                        f"{ep}.stages.{sname}.p95: {cur_sp95}ms > {base_sp95}ms x{tolerance} (limit {round(limit, 2)}ms)"
                    )

    if reasons:
        return ("hold", reasons)
    return ("pass", [])


def summary_markdown_l3(report: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    lines = [
        "# Harness L3 — performance & latency report",
        "",
        "## Request Latencies",
        "",
        "| endpoint | rps | p50 (ms) | p95 (ms) | baseline p95 | errors |",
        "|---|---|---|---|---|---|",
    ]
    for ep in ("search", "answer"):
        ep_data = report.get(ep, {})
        lat = ep_data.get("latency_ms", {})
        cur_p95 = lat.get("p95")
        base_p95 = _get_nested(baseline, f"agent.{ep}.latency_ms.p95")
        base_str = f"{base_p95}ms" if base_p95 is not None else "n/a"
        lines.append(
            f"| {ep} | {ep_data.get('rps', 0.0)} | {lat.get('p50', 0.0)} | {cur_p95} | {base_str} | {ep_data.get('errors', 0)} |"
        )

    lines += [
        "",
        "## Per-Stage Latencies (p50 / p95)",
        "",
        "| endpoint | stage | p50 (ms) | p95 (ms) | baseline p95 | max (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for ep in ("search", "answer"):
        stages = report.get(ep, {}).get("stages", {})
        for sname, smetrics in sorted(stages.items()):
            cur_sp95 = smetrics.get("p95")
            base_sp95 = _get_nested(baseline, f"agent.{ep}.stages.{sname}.p95")
            base_str = f"{base_sp95}ms" if base_sp95 is not None else "n/a"
            lines.append(
                f"| {ep} | {sname} | {smetrics.get('p50', 0.0)} | {cur_sp95} | {base_str} | {smetrics.get('max', 0.0)} |"
            )

    vram = report.get("vram")
    if vram:
        lines += [
            "",
            "## VRAM Footprint",
            "",
            f"- used: {vram.get('used_mb')} MB",
            f"- total: {vram.get('total_mb')} MB",
        ]

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="agent base URL")
    parser.add_argument("--concurrency", type=int, default=8, help="worker concurrency")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds of load per endpoint")
    parser.add_argument(
        "--baseline", type=Path, default=REPO / "benchmarks" / "baseline.json",
        help="path to baseline JSON file",
    )
    parser.add_argument("--gate", action="store_true", help="fail nonzero on performance regressions")
    parser.add_argument("--update-baseline", action="store_true", help="update baseline JSON with measured metrics")
    parser.add_argument("--out", type=Path, default=None, help="write JSON report here")
    parser.add_argument("--summary", type=Path, default=None, help="write Markdown summary here")
    args = parser.parse_args(argv)

    baseline: dict[str, Any] | None = None
    if args.baseline and args.baseline.exists():
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            baseline = None

    vram_initial = query_vram_mb()
    print(f"[*] Driving load against {args.url}/v1/search (concurrency={args.concurrency}, duration={args.duration}s)...", file=sys.stderr)
    search_res = run_load(args.url, "search", DEFAULT_QUERIES, args.concurrency, args.duration)

    print(f"[*] Driving load against {args.url}/v1/answer (concurrency={args.concurrency}, duration={args.duration}s)...", file=sys.stderr)
    answer_res = run_load(args.url, "answer", DEFAULT_QUERIES, args.concurrency, args.duration)

    vram_final = query_vram_mb()
    vram = vram_final or vram_initial

    report: dict[str, Any] = {
        "env": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "search": search_res,
        "answer": answer_res,
        "vram": vram,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if args.update_baseline and args.baseline:
        export_to_baseline(args.baseline, "search", search_res)
        export_to_baseline(args.baseline, "answer", answer_res)
        print(f"baseline updated at {args.baseline}", file=sys.stderr)

    summary_md = summary_markdown_l3(report, baseline)
    print(summary_md, file=sys.stderr)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(summary_md, encoding="utf-8")

    try:
        from mainframe_rag.config import load_settings
        from mainframe_rag.manifest import write_run_manifest

        manifest = write_run_manifest("harness_l3", load_settings(), report)
        print(f"run manifest appended ({manifest['git_sha'][:8]})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — manifest is observability, never the gate
        print(f"warn: failed to append run manifest: {exc}", file=sys.stderr)

    if args.gate:
        verdict, reasons = gate_verdict_l3(report, baseline)
        print(f"[*] L3 VERDICT: {verdict}", file=sys.stderr)
        for r in reasons:
            print(f"    - {r}", file=sys.stderr)
        return 0 if verdict == "pass" else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
