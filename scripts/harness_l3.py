#!/usr/bin/env python3
"""Harness L3 — performance & latency tier: per-stage p50/p95, TTFT, VRAM.

Drives concurrent load against the running agent's /v1/search and /v1/answer
endpoints, captures per-stage latency percentiles (embed_ms, qdrant_ms,
llm_ms, ttft_ms) from Server-Timing headers, measures VRAM footprint via
nvidia-smi (trend data), and gates regressions against the baseline JSON.

Usage:
    # Run and gate vs baseline:
    python scripts/harness_l3.py --url http://127.0.0.1:8080 --gate \
        --baseline benchmarks/harness-l3-vllm.json --out bundles/harness-l3-report.json

    # Update baseline:
    python scripts/harness_l3.py --url http://127.0.0.1:8080 --update-baseline \
        --baseline benchmarks/harness-l3-vllm.json
"""

from __future__ import annotations

import argparse
import json
import os
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

if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from loadtest import export_to_baseline

from mainframe_rag.eval.datasets import VenueError, require_rc_for_collection
from mainframe_rag.eval.load import DEFAULT_QUERIES, query_gpu_name, query_vram_mb, run_load
from mainframe_rag.eval.performance import (
    default_baseline_path,
    gate_verdict_l3,
    summary_markdown_l3,
)


def env_snapshot(**run: Any) -> dict[str, Any]:
    try:
        from qdrant_pin import qdrant_image_pin

        pin = qdrant_image_pin(REPO / "images.txt")
    except Exception:  # noqa: BLE001
        pin = "unavailable"
    snapshot: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "embed_mode": os.environ.get("EMBED_MODE", "hash").lower(),
        "qdrant_image": pin,
        "gpu_name": query_gpu_name(),
    }
    # Run-shape provenance (concurrency is gated; duration/timeout document
    # the load window a p95 was recorded under).
    snapshot.update({key: value for key, value in run.items() if value is not None})
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="agent base URL")
    parser.add_argument("--concurrency", type=int, default=8, help="worker concurrency")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds of load per endpoint")
    parser.add_argument(
        "--request-timeout", type=float, default=30.0,
        help="per-request client timeout (reasoning answers under load can exceed 30s on small GPUs)",
    )
    parser.add_argument(
        "--baseline", type=Path, default=default_baseline_path(REPO, os.environ.get("EMBED_MODE", "hash")),
        help="path to dedicated L3 baseline JSON file (default: mode-keyed)",
    )
    parser.add_argument("--gate", action="store_true", help="fail nonzero on performance regressions")
    parser.add_argument("--update-baseline", action="store_true", help="update baseline JSON with measured metrics")
    parser.add_argument("--out", type=Path, default=None, help="write JSON report here")
    parser.add_argument("--summary", type=Path, default=None, help="write Markdown summary here")
    args = parser.parse_args(argv)

    from mainframe_rag.config import load_settings

    try:
        # Venue rule (issue #268): the real-corpus collection is an RC-only
        # instrument even for the perf tier.
        require_rc_for_collection(load_settings().qdrant_collection)
    except VenueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    baseline: dict[str, Any] | None = None
    if args.baseline and args.baseline.exists():
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            baseline = None

    vram_initial = query_vram_mb()
    print(f"[*] Driving load against {args.url}/v1/search (concurrency={args.concurrency}, duration={args.duration}s)...", file=sys.stderr)
    search_res = run_load(args.url, "search", DEFAULT_QUERIES, args.concurrency, args.duration,
                          request_timeout_s=args.request_timeout)

    print(f"[*] Driving load against {args.url}/v1/answer (concurrency={args.concurrency}, duration={args.duration}s)...", file=sys.stderr)
    answer_res = run_load(args.url, "answer", DEFAULT_QUERIES, args.concurrency, args.duration,
                          request_timeout_s=args.request_timeout)

    vram_final = query_vram_mb()
    vram = vram_final or vram_initial

    env = env_snapshot(
        concurrency=args.concurrency,
        duration_s=args.duration,
        request_timeout_s=args.request_timeout,
    )
    report: dict[str, Any] = {
        "env": env,
        "search": search_res,
        "answer": answer_res,
        "vram": vram,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if args.update_baseline:
        faults: list[str] = []
        for ep, res in (("search", search_res), ("answer", answer_res)):
            if res.get("errors", 0) > 0:
                faults.append(f"{ep}: {res['errors']} request error(s)")
            if res.get("missing_timings", 0) > 0:
                faults.append(f"{ep}: {res['missing_timings']} response(s) missing Server-Timing header")
        if faults:
            print(
                "ERROR: refusing to update baseline: load run had faults:\n  - "
                + "\n  - ".join(faults)
                + "\nA broken run must not become the pin.",
                file=sys.stderr,
            )
            return 1
        if args.baseline:
            export_to_baseline(args.baseline, "search", search_res, env=env)
            export_to_baseline(args.baseline, "answer", answer_res, env=env)
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
