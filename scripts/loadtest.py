#!/usr/bin/env python3
"""Concurrent load generator for the agent endpoints (loopback only).

Drives real HTTP against a running agent with a deterministic query set and
prints one JSON result object on stdout (human table goes to stderr, so the
stdout stays pipeable). Used by scripts/benchmark.py; also runnable standalone:

    python scripts/loadtest.py --url http://127.0.0.1:8080 \
        --endpoint search --concurrency 8 --duration 30 --query "IEA500I operator message"

Every request is real; nothing is monkeypatched. The /v1/answer endpoint is
only as honest as the model behind it — under the benchmark harness that is
the deterministic mock (no real model exists), and any report must say so.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import httpx

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


def run_load(
    base_url: str,
    endpoint: str,
    queries: list[str],
    concurrency: int,
    duration_s: float,
    limit: int = 8,
) -> dict:
    """Run the load and return the metrics dict. Thread-per-worker, each with
    its own connection pool; round-robin over the deterministic query set."""
    path = "/v1/search" if endpoint == "search" else "/v1/answer"
    url = f"{base_url.rstrip('/')}{path}"
    deadline = time.monotonic() + duration_s
    latencies: list[float] = []
    errors = 0
    lock = threading.Lock()
    query_idx = {"next": 0}

    def worker() -> None:
        nonlocal errors
        client = httpx.Client(timeout=30.0)
        try:
            while time.monotonic() < deadline:
                with lock:
                    query = queries[query_idx["next"] % len(queries)]
                    query_idx["next"] += 1
                started = time.perf_counter()
                try:
                    resp = client.post(url, json={"query": query, "limit": limit})
                    ok = resp.status_code == 200
                except httpx.HTTPError:
                    ok = False
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                with lock:
                    latencies.append(elapsed_ms)
                    if not ok:
                        errors += 1
        finally:
            client.close()

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    ordered = sorted(latencies)
    return {
        "endpoint": endpoint,
        "concurrency": concurrency,
        "duration_s": round(wall, 3),
        "requests": len(ordered),
        "errors": errors,
        "rps": round(len(ordered) / wall, 3) if wall > 0 else 0.0,
        "latency_ms": {
            "p50": round(_percentile(ordered, 50), 2),
            "p90": round(_percentile(ordered, 90), 2),
            "p95": round(_percentile(ordered, 95), 2),
            "p99": round(_percentile(ordered, 99), 2),
            "max": round(ordered[-1], 2) if ordered else 0.0,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="agent base URL")
    parser.add_argument("--endpoint", choices=("search", "answer"), default="search")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--duration", type=float, default=30.0, help="seconds of load")
    parser.add_argument(
        "--query", action="append", default=None,
        help="query to send (repeatable); defaults to a fixed mixed set",
    )
    args = parser.parse_args(argv)

    queries = args.query or DEFAULT_QUERIES
    result = run_load(args.url, args.endpoint, queries, args.concurrency, args.duration)
    lat = result["latency_ms"]
    print(
        f"load[{args.endpoint}] requests={result['requests']} errors={result['errors']} "
        f"rps={result['rps']} p50={lat['p50']}ms p95={lat['p95']}ms p99={lat['p99']}ms",
        file=sys.stderr,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
