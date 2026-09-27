"""Reusable concurrent HTTP load measurement and hardware observations.

CLI dispatch and baseline persistence remain in scripts/loadtest.py.
Importing this module performs no HTTP request or GPU probe.
"""
from __future__ import annotations

import collections
import re
import subprocess
import threading
import time
from typing import Any

import httpx2

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
    request_timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Run the load and return the metrics dict. Thread-per-worker, each with
    its own connection pool; round-robin over the deterministic query set.
    Captures overall latency, per-stage timings from Server-Timing headers,
    and VRAM footprint. ``request_timeout_s`` bounds one request: reasoning
    answers under concurrency can legitimately exceed 30s on a small GPU, and
    a client-side give-up is a recordable fault, not a latency sample."""
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
        client = httpx2.Client(timeout=request_timeout_s)
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
