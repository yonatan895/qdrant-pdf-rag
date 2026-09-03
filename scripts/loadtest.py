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
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx2

REPO = Path(__file__).resolve().parents[1]

DEFAULT_QUERIES = [
    "IEA500I operator message",
    "SA22-0000-00 initialization parameters",
    "system initialization LFAREA parameter",
    "operator response reissue command",
]


def _percentile(sorted_ms: list[float], p: float) -> float:
    if not sorted_ms:
        return 0.0
    idx = min(len(sorted_ms) - 1, round(p / 100.0 * (len(sorted_ms) - 1)))
    return sorted_ms[idx]


def parse_server_timing(header_val: str | None) -> dict[str, float]:
    """Parse W3C Server-Timing header (e.g. 'embed;dur=12, qdrant;dur=34, llm;dur=56, ttft;dur=45').
    Returns a dict with timing values in ms, e.g. {'embed_ms': 12.0, 'qdrant_ms': 34.0, ...}."""
    if not header_val:
        return {}
    timings: dict[str, float] = {}
    for part in header_val.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([a-zA-Z0-9_-]+);dur=(\"?)([\d.]+)\2", part)
        if m:
            metric = m.group(1)
            try:
                dur = float(m.group(3))
                timings[f"{metric}_ms"] = dur
            except ValueError:
                continue
    return timings


def query_vram_mb() -> dict[str, float] | None:
    """Query NVIDIA GPU VRAM used/total in MB via nvidia-smi.
    Returns None if nvidia-smi is unavailable (e.g. CPU-only or CI)."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            line = proc.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                used = float(parts[0])
                total = float(parts[1])
                return {"used_mb": used, "total_mb": total}
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def query_gpu_name() -> str | None:
    """Query NVIDIA GPU model name via nvidia-smi. Returns None if unavailable."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[0]
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return None


def run_load(
    base_url: str,
    endpoint: str,
    queries: list[str],
    concurrency: int,
    duration_s: float,
    limit: int = 8,
) -> dict[str, Any]:
    """Run the load and return the metrics dict. Thread-per-worker, each with
    its own connection pool; round-robin over the deterministic query set.
    Captures overall latency, per-stage timings from Server-Timing headers,
    and VRAM footprint."""
    path = "/v1/search" if endpoint == "search" else "/v1/answer"
    url = f"{base_url.rstrip('/')}{path}"
    latencies: list[float] = []
    stage_latencies: dict[str, list[float]] = collections.defaultdict(list)
    errors = 0
    missing_timings = 0
    lock = threading.Lock()
    query_idx = {"next": 0}

    vram_start = query_vram_mb()

    def worker() -> None:
        nonlocal errors, missing_timings
        client = httpx2.Client(timeout=30.0)
        try:
            while time.monotonic() < deadline:
                with lock:
                    query = queries[query_idx["next"] % len(queries)]
                    query_idx["next"] += 1
                started = time.perf_counter()
                st_header: str | None = None
                try:
                    resp = client.post(url, json={"query": query, "limit": limit})
                    ok = resp.status_code == 200
                    if ok:
                        st_header = resp.headers.get("server-timing")
                except httpx2.HTTPError:
                    ok = False
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                timings = parse_server_timing(st_header) if ok else {}
                with lock:
                    latencies.append(elapsed_ms)
                    if ok:
                        if not timings:
                            missing_timings += 1
                        for metric, val in timings.items():
                            stage_latencies[metric].append(val)
                    else:
                        errors += 1
        finally:
            client.close()

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    started = time.perf_counter()
    deadline = time.monotonic() + duration_s
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    vram_end = query_vram_mb()
    vram = vram_end or vram_start

    ordered = sorted(latencies)
    stages: dict[str, dict[str, float]] = {}
    for stage_name in sorted(stage_latencies):
        ordered_stage = sorted(stage_latencies[stage_name])
        stages[stage_name] = {
            "p50": round(_percentile(ordered_stage, 50), 2),
            "p90": round(_percentile(ordered_stage, 90), 2),
            "p95": round(_percentile(ordered_stage, 95), 2),
            "p99": round(_percentile(ordered_stage, 99), 2),
            "max": round(ordered_stage[-1], 2) if ordered_stage else 0.0,
        }

    return {
        "endpoint": endpoint,
        "concurrency": concurrency,
        "duration_s": round(wall, 3),
        "requests": len(ordered),
        "errors": errors,
        "missing_timings": missing_timings,
        "rps": round(len(ordered) / wall, 3) if wall > 0 else 0.0,
        "latency_ms": {
            "p50": round(_percentile(ordered, 50), 2),
            "p90": round(_percentile(ordered, 90), 2),
            "p95": round(_percentile(ordered, 95), 2),
            "p99": round(_percentile(ordered, 99), 2),
            "max": round(ordered[-1], 2) if ordered else 0.0,
        },
        "stages": stages,
        "vram": vram,
    }


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
            "note": "Re-baseline via `make harness-l3 --update-baseline`; dedicated PR (AGENTS.md).",
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
        res = run_load(args.url, ep, queries, args.concurrency, args.duration)
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
