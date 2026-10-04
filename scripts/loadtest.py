#!/usr/bin/env python3
"""Concurrent load generator for the agent endpoints (loopback only).

Drives real HTTP against a running agent with a deterministic query set and
prints one JSON result object on stdout (human table goes to stderr, so the
stdout stays pipeable). Used by scripts/benchmark.py and harness L3; also
runnable standalone:

    python scripts/loadtest.py --url http://127.0.0.1:8080 \
        --endpoint search --concurrency 8 --duration 30 --query "IEA500I operator message"

Exports per-stage p50/p95 latency (embed_ms, qdrant_ms, llm_ms, ttft_ms) and
VRAM footprint into the baseline JSON via --baseline / --update-baseline.

Every request is real; nothing is monkeypatched. The /v1/answer endpoint is
only as honest as the model behind it — under the benchmark harness that is
the deterministic mock (no real model exists), and any report must say so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# Preserve the supported uninstalled-checkout script entry.
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from mainframe_rag.eval.load import DEFAULT_QUERIES, query_gpu_name, run_load


def _set_nested(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def export_to_baseline(
    baseline_path: Path,
    endpoint: str,
    result: dict[str, Any],
    env: dict[str, Any] | None = None,
) -> None:
    """Export or update load test metrics (latencies, per-stage percentiles,
    and VRAM footprint) into the baseline JSON file preserving nested shape.
    Refuses to write to benchmarks/baseline.json to prevent polluting CI benchmarks.
    Refuses to record runs with errors or missing Server-Timing headers."""
    resolved = baseline_path.resolve()
    ci_bench = (REPO / "benchmarks" / "baseline.json").resolve()
    if resolved == ci_bench or (baseline_path.name == "baseline.json" and "benchmarks" in str(baseline_path)):
        raise ValueError(
            f"Refusing to export L3 metrics to {baseline_path}: benchmarks/baseline.json is reserved "
            "for the CI benchmark gate. L3 metrics must be exported to dedicated L3 baseline files "
            "(e.g. benchmarks/harness-l3*.json)."
        )

    errors = result.get("errors", 0)
    missing_timings = result.get("missing_timings", 0)
    if errors > 0 or missing_timings > 0:
        raise ValueError(
            f"Refusing to update baseline: {endpoint} run had faults (errors={errors}, "
            f"missing_timings={missing_timings}). A broken run must not become the pin."
        )

    baseline: dict[str, Any] = {}
    if baseline_path.exists():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            baseline = {}

    if "_meta" not in baseline:
        baseline["_meta"] = {
            "note": "Re-baseline via `sh scripts/tools/run-task.sh eval:harness:l3-baseline`; dedicated PR (AGENTS.md).",
            "updated": time.strftime("%Y-%m-%d"),
        }
    else:
        baseline["_meta"]["updated"] = time.strftime("%Y-%m-%d")

    if env:
        existing_env = baseline["_meta"].get("env", {})
        existing_env.update(env)
        baseline["_meta"]["env"] = existing_env

    lat = result.get("latency_ms", {})
    if "p50" in lat:
        _set_nested(baseline, f"agent.{endpoint}.latency_ms.p50", lat["p50"])
    if "p95" in lat:
        _set_nested(baseline, f"agent.{endpoint}.latency_ms.p95", lat["p95"])

    stages = result.get("stages", {})
    for stage_name, metrics in stages.items():
        if "p50" in metrics:
            _set_nested(baseline, f"agent.{endpoint}.stages.{stage_name}.p50", metrics["p50"])
        if "p95" in metrics:
            _set_nested(baseline, f"agent.{endpoint}.stages.{stage_name}.p95", metrics["p95"])

    vram = result.get("vram")
    if isinstance(vram, dict) and "used_mb" in vram:
        _set_nested(baseline, "vram.used_mb", vram["used_mb"])
        if "total_mb" in vram:
            _set_nested(baseline, "vram.total_mb", vram["total_mb"])

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="agent base URL")
    parser.add_argument("--endpoint", choices=("search", "answer", "all"), default="search")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--duration", type=float, default=30.0, help="seconds of load")
    parser.add_argument(
        "--request-timeout", type=float, default=30.0,
        help="per-request client timeout in seconds (reasoning answers under load can exceed 30s)",
    )
    parser.add_argument(
        "--query", action="append", default=None,
        help="query to send (repeatable); defaults to a fixed mixed set",
    )
    parser.add_argument(
        "--baseline", "--update-baseline", "--export-baseline",
        dest="baseline",
        type=Path,
        default=None,
        help="export measured stage percentiles and VRAM into this dedicated L3 baseline JSON file (refuses benchmarks/baseline.json)",
    )
    args = parser.parse_args(argv)

    if args.baseline:
        ci_bench = (REPO / "benchmarks" / "baseline.json").resolve()
        if args.baseline.resolve() == ci_bench or (args.baseline.name == "baseline.json" and "benchmarks" in str(args.baseline)):
            print(
                f"ERROR: Refusing to export L3 metrics to {args.baseline}: benchmarks/baseline.json is reserved "
                "for the CI benchmark gate. Specify a dedicated L3 baseline file (e.g. benchmarks/harness-l3.json).",
                file=sys.stderr,
            )
            return 1

    queries = args.query or DEFAULT_QUERIES
    endpoints = ["search", "answer"] if args.endpoint == "all" else [args.endpoint]
    results: dict[str, Any] = {}

    for ep in endpoints:
        res = run_load(args.url, ep, queries, args.concurrency, args.duration,
                       request_timeout_s=args.request_timeout)
        results[ep] = res
        lat = res["latency_ms"]
        print(
            f"load[{ep}] requests={res['requests']} errors={res['errors']} "
            f"missing_timings={res.get('missing_timings', 0)} "
            f"rps={res['rps']} p50={lat['p50']}ms p95={lat['p95']}ms p99={lat['p99']}ms",
            file=sys.stderr,
        )
        if res.get("stages"):
            for sname, smetrics in res["stages"].items():
                print(
                    f"  stage[{sname}] p50={smetrics['p50']}ms p95={smetrics['p95']}ms max={smetrics['max']}ms",
                    file=sys.stderr,
                )
        if res.get("vram"):
            print(
                f"  vram used={res['vram']['used_mb']}MB total={res['vram']['total_mb']}MB",
                file=sys.stderr,
            )
        if args.baseline:
            if res.get("errors", 0) > 0 or res.get("missing_timings", 0) > 0:
                print(
                    f"ERROR: refusing to update baseline: {ep} run had faults "
                    f"(errors={res.get('errors', 0)}, missing_timings={res.get('missing_timings', 0)}). "
                    "A broken run must not become the pin.",
                    file=sys.stderr,
                )
                return 1
            env = {"cpu_count": os.cpu_count(), "gpu_name": query_gpu_name()}
            export_to_baseline(args.baseline, ep, res, env=env)
            print(f"exported {ep} metrics to baseline {args.baseline}", file=sys.stderr)

    output = results if args.endpoint == "all" else results[args.endpoint]
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
