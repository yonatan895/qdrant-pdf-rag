#!/usr/bin/env python3
"""End-to-end performance harness for the simulation tier.

Measures, against a real Qdrant server (docker, the images.txt pin) and a
real agent (uvicorn, hash embedder):

- ingest: wall time, docs/s, chunks, peak single-process RSS (the max RSS
  across reaped children/descendants — a floor for the tree total)
- Qdrant: container memory/CPU (docker stats), storage disk footprint
- agent endpoints: latency percentiles under concurrent load. /v1/answer is
  measured against the DETERMINISTIC MOCK LLM (no real model exists) — every
  report says so; treat answer numbers as plumbing benchmarks, not model
  benchmarks.

One JSON result object on --out / stdout; markdown table on --summary
(GITHUB_STEP_SUMMARY in CI). Regression gate:

    python scripts/benchmark.py --check benchmarks/baseline.json --summary out.md

fails when a gated metric exceeds its baseline tolerance (RSS/disk 1.5x,
latency p95 3x — shared-runner wall time is noisy, resource footprints less
so; improvements never fail). Missing baseline warns and passes.

    python scripts/benchmark.py --update-baseline benchmarks/baseline.json

re-records the baseline (re-baselining is a dedicated PR, see AGENTS.md).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from loadtest import DEFAULT_QUERIES, run_load
from qdrant_sim import QdrantSim, start_simulator

REPO_ROOT = Path(__file__).resolve().parents[1]
GATED_METRICS = {
    # dotted path into the result -> regression tolerance multiplier
    "ingest.peak_rss_mb": 1.5,
    "qdrant.mem_mb": 1.5,
    "qdrant.disk_mb": 1.5,
    "agent.search.latency_ms.p95": 3.0,
    "agent.answer.latency_ms.p95": 3.0,
}


def _get(result: dict, dotted: str):
    node: object = result
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(target: dict, dotted: str, value) -> None:
    """Mirror of _get: writes the nested shape the gate reads. Baselines and
    results MUST share one shape — update_baseline emits nested records so
    check_baseline's _get walk finds them (a flat dotted-key baseline would
    gate nothing, forever)."""
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _meminfo_total_mb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def _import_scripts_mod(name: str):
    """Import a sibling script module in both contexts: repo-root on sys.path
    (tests, make) and scripts/ itself on sys.path (standalone run)."""
    import importlib

    try:
        return importlib.import_module(f"scripts.{name}")
    except ImportError:
        return importlib.import_module(name)


def env_snapshot() -> dict:
    try:
        pin = _import_scripts_mod("qdrant_pin").qdrant_image_pin(REPO_ROOT / "images.txt")
    except (ImportError, ValueError, OSError):
        pin = "unavailable"
    return {
        "cpu_count": os.cpu_count(),
        "mem_total_mb": _meminfo_total_mb(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "qdrant_image": pin,
        "docs": int(os.environ.get("BENCH_DOCS", "30")),
    }


def generate_corpus(root: Path, docs: int) -> dict:
    """docs IBM-shaped documents with genuinely distinct bodies (unique
    message ids — identical bodies tie in RRF, see AGENTS.md) + 1 plain doc."""
    pdf_gen = _import_scripts_mod("make_synthetic_pdf")
    # Self-consistent runs: a leftover corpus from a smaller BENCH_DOCS run
    # would otherwise be walked and ingested alongside the current one.
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    for i in range(docs):
        if i == 0:
            pdf_gen.build(root / "SA22-0000-00.pdf")
        else:
            # DOCNO_RE: [A-Z]{2,4}\d{2}-\d{4}(-\d{2})?; MSG_RE: [A-Z]{3}\d{2,5}[A-Z]
            pdf_gen.build(
                root / f"SA22-7{i:03d}-01.pdf",
                doc_id=f"SA22-7{i:03d}-01",
                title=f"Synthetic Reference Volume {i}",
                message_id=f"IEA5{i:02d}I",
            )
    pdf_gen.build_plain(root / "widget-guide.pdf")
    return {"docs": docs + 1, "root": root}


def run_ingest(corpus: Path, qdrant_url: str, collection: str) -> dict:
    progress = corpus.parent / "bench-inventory.jsonl"
    progress.unlink(missing_ok=True)  # the inventory is append-only; a stale
    # one from an earlier run would inflate the docs/chunks being reported
    env = {
        **os.environ,
        "QDRANT_URL": qdrant_url,
        "QDRANT_COLLECTION": collection,
        "EMBED_MODE": "hash",
    }
    cmd = [sys.executable, "-m", "mainframe_rag.ingest.run_ingest",
           "--src", str(corpus), "--progress", str(progress)]
    started = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600, check=False)
    wall = time.perf_counter() - started
    if proc.returncode != 0:
        raise RuntimeError(f"ingest failed rc={proc.returncode}: {proc.stderr[-400:]}")
    # RUSAGE_CHILDREN.ru_maxrss is the max single-process RSS across all
    # reaped children and their descendants (monotonic high-water, KB on
    # Linux) — a floor for the tree total, and it also covers docker client
    # children spawned before the ingest.
    peak_rss_mb = round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1)
    records = [json.loads(line) for line in progress.read_text().splitlines() if line.strip()]
    docs_upserted = sum(1 for r in records if r["status"] == "upserted")
    chunks = sum(r["chunks"] for r in records if r["status"] == "upserted")
    return {
        "wall_s": round(wall, 2),
        "docs_per_s": round(docs_upserted / wall, 2) if wall else 0.0,
        "docs": docs_upserted,
        "chunks": chunks,
        "peak_rss_mb": peak_rss_mb,
    }


def qdrant_stats(sim: QdrantSim, collection: str) -> dict:
    stats: dict = {}
    try:
        info = httpx.get(f"{sim.url}/collections/{collection}", timeout=10.0).json()["result"]
        stats["points"] = info.get("points_count")
        stats["indexed_vectors"] = info.get("indexed_vectors_count")
    except (httpx.HTTPError, KeyError, ValueError):
        stats["points"] = None
    if sim.container_id:
        raw = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", sim.container_id],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.strip().splitlines()
        if raw:
            row = json.loads(raw[-1])
            mem_used = str(row.get("MemUsage", "")).split("/")[0].strip()
            stats["mem_mb"] = _parse_size_mb(mem_used)
            stats["cpu_pct"] = float(str(row.get("CPUPerc", "0%")).rstrip("%") or 0)
        du = subprocess.run(
            ["docker", "exec", sim.container_id, "sh", "-c",
             "du -sB1 /qdrant/storage; du -sb /qdrant/storage"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if du.returncode == 0:
            lines = du.stdout.splitlines()
            # Real allocated blocks vs apparent size: Qdrant pre-allocates
            # large sparse mmap segments (~400MB apparent for an empty
            # collection), so apparent size tracks reservations, not usage.
            real = lines[0].split()[0] if lines else "0"
            stats["disk_mb"] = round(int(real) / (1024 * 1024), 1)
            if len(lines) > 1:
                stats["disk_apparent_mb"] = round(int(lines[1].split()[0]) / (1024 * 1024), 1)
    try:
        metrics = httpx.get(f"{sim.url}/metrics", timeout=10.0).text
        stats["metrics_available"] = True
        stats["memory_metric_names"] = sorted(
            line.split("{")[0] for line in metrics.splitlines()
            if line.startswith("qdrant_") and "memory" in line and not line.startswith("#")
        )[:10]
    except httpx.HTTPError:
        stats["metrics_available"] = False
    return stats


def _parse_size_mb(text: str) -> float | None:
    """docker stats MemUsage fragments like '123.4MiB', '1.2GiB', '512kB', '0B'."""
    text = text.strip()
    for suffix, factor in (("GiB", 1024.0), ("MiB", 1.0), ("kB", 1 / 1024.0), ("B", 1 / (1024 * 1024))):
        if text.endswith(suffix):
            try:
                return round(float(text[: -len(suffix)].strip()) * factor, 1)
            except ValueError:
                return None
    return None


class _MockLLM:
    """The deterministic model stand-in (same module the sim tests use)."""

    def __init__(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "mock_vllm_bench", REPO_ROOT / "scripts" / "mock_vllm.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def run_agent_phase(qdrant_url: str, collection: str, concurrency: int,
                    search_s: float, answer_s: float) -> dict:
    with _MockLLM() as mock_url:
        env = {
            **os.environ,
            "QDRANT_URL": qdrant_url,
            "QDRANT_COLLECTION": collection,
            "EMBED_MODE": "hash",
            "ALLOW_HASH_MODE": "true",
            "LLM_BASE_URL": f"{mock_url}/v1",
            "LLM_MODEL_REASONING": "mock-reasoning",
        }
        with tempfile.TemporaryFile(mode="w+") as log:
            agent = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "mainframe_rag.agent.app:app",
                 "--host", "127.0.0.1", "--port", "0", "--log-level", "info"],
                env=env, stdout=log, stderr=log,
            )
            try:
                base_url = _wait_agent(agent, log)
                return {
                    "model_note": (
                        "/v1/answer is measured against the deterministic mock LLM "
                        "(no real model): plumbing benchmark, not model benchmark."
                    ),
                    "search": run_load(base_url, "search", DEFAULT_QUERIES, concurrency, search_s),
                    "answer": run_load(base_url, "answer", DEFAULT_QUERIES, concurrency, answer_s),
                }
            finally:
                agent.terminate()
                try:
                    agent.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    agent.kill()


def _wait_agent(agent: subprocess.Popen, log, timeout_s: float = 30.0) -> str:
    """Uvicorn announces its ephemeral port on stderr (file-backed, so
    polling never blocks the way a PIPE readline would)."""
    deadline = time.monotonic() + timeout_s
    port = None
    while time.monotonic() < deadline and port is None:
        if agent.poll() is not None:
            break
        log.seek(0)
        m = re.search(r"Uvicorn running on http://127\.0\.0\.1:(\d+)", log.read())
        if m:
            port = m.group(1)
        time.sleep(0.1)
    if port is None:
        log.seek(0)
        raise RuntimeError(f"agent did not announce its port in time; log: {log.read()[-400:]}")
    base_url = f"http://127.0.0.1:{port}"
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/healthz", timeout=5.0).status_code == 200:
                return base_url
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError("agent /healthz never became ready")


def check_baseline(result: dict, baseline: dict | None) -> list[str]:
    if baseline is None:
        return []
    regressions = []
    for dotted, tolerance in GATED_METRICS.items():
        current = _get(result, dotted)
        if current is None:
            # Metric could not be measured this run (e.g. qdrant mem/disk on
            # the QDRANT_SIM_URL reuse path) — warn and skip, not regress.
            print(f"warn: {dotted} not measured this run; not gated", file=sys.stderr)
            continue
        allowed = _get(baseline, dotted)
        if allowed is None:
            print(f"warn: baseline has no {dotted}; not gated", file=sys.stderr)
            continue
        limit = allowed * tolerance
        if current > limit:
            regressions.append(
                f"{dotted}: {current} > {allowed} x{tolerance} (limit {round(limit, 2)})"
            )
    for endpoint in ("search", "answer"):
        errors = _get(result, f"agent.{endpoint}.errors")
        if errors:
            regressions.append(
                f"agent.{endpoint}.errors: {errors} > 0 — the agent failed under load"
            )
    return regressions


def update_baseline(result: dict, baseline_path: Path) -> None:
    payload: dict = {
        "_meta": {
            "note": "Re-baseline via `make bench-baseline`; dedicated PR (AGENTS.md). Tolerances in scripts/benchmark.py GATED_METRICS.",
            "env": result["env"],
            "updated": time.strftime("%Y-%m-%d"),
        },
    }
    for dotted in GATED_METRICS:
        _set(payload, dotted, _get(result, dotted))
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, indent=2) + "\n")


def summary_markdown(result: dict, baseline: dict | None) -> str:
    lines = [
        "## Benchmark results",
        "",
        (
            f"env: {result['env']['cpu_count']} cpus, {result['env']['mem_total_mb']} MB RAM, "
            f"{result['env']['qdrant_image']}"
        ),
        "",
        "| metric | current | baseline | gate |",
        "|---|---|---|---|",
    ]
    for dotted, tolerance in GATED_METRICS.items():
        current = _get(result, dotted)
        allowed = _get(baseline, dotted) if baseline else None
        gate = f"<= {round(allowed * tolerance, 2)}" if allowed is not None else "n/a"
        lines.append(f"| {dotted} | {current} | {allowed} | {gate} |")
    for endpoint in ("search", "answer"):
        errors = _get(result, f"agent.{endpoint}.errors")
        lines.append(f"| agent.{endpoint}.errors | {errors} | 0 | == 0 |")
    lines += [
        "",
        (
            f"ingest: {result['ingest']['docs']} docs / {result['ingest']['chunks']} chunks "
            f"in {result['ingest']['wall_s']}s ({result['ingest']['docs_per_s']} docs/s)"
        ),
        (
            f"agent search: {result['agent']['search']['rps']} rps, "
            f"p50 {result['agent']['search']['latency_ms']['p50']}ms, "
            f"p99 {result['agent']['search']['latency_ms']['p99']}ms"
        ),
        (
            f"agent answer: {result['agent']['answer']['rps']} rps, "
            f"p50 {result['agent']['answer']['latency_ms']['p50']}ms, "
            f"p99 {result['agent']['answer']['latency_ms']['p99']}ms"
        ),
        "",
        f"_{result['agent']['model_note']}_",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None, help="write the full JSON result here")
    parser.add_argument("--summary", type=Path, default=None, help="write a markdown table here")
    parser.add_argument("--check", type=Path, default=None, help="fail on regressions vs this baseline")
    parser.add_argument("--update-baseline", type=Path, default=None, help="record a new baseline here")
    parser.add_argument("--collection", default="bench")
    args = parser.parse_args(argv)
    if args.check and args.update_baseline:
        parser.error("--check and --update-baseline are mutually exclusive")

    concurrency = int(os.environ.get("BENCH_CONCURRENCY", "8"))
    search_s = float(os.environ.get("BENCH_SEARCH_SECONDS", "120"))
    answer_s = float(os.environ.get("BENCH_ANSWER_SECONDS", "60"))

    sim: QdrantSim | None = None
    try:
        sim = start_simulator(REPO_ROOT, os.environ.get("QDRANT_SIM_URL"))
        # Sessions must be independent: a stale bench collection from an
        # earlier run would inflate the disk/RAM footprint being measured.
        httpx.delete(f"{sim.url}/collections/{args.collection}", timeout=10.0)
        corpus_env = os.environ.get("BENCH_CORPUS_DIR")
        corpus_root = Path(corpus_env) if corpus_env else None
        corpus_info = generate_corpus(
            corpus_root
            or Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
            / "bench-corpus",
            int(os.environ.get("BENCH_DOCS", "30")),
        )
        ingest = run_ingest(corpus_info["root"], sim.url, args.collection)
        qdrant = qdrant_stats(sim, args.collection)
        agent = run_agent_phase(sim.url, args.collection, concurrency, search_s, answer_s)
    finally:
        if sim is not None:
            sim.stop()

    result = {
        "env": env_snapshot(),
        "corpus": {"docs": corpus_info["docs"]},
        "ingest": ingest,
        "qdrant": qdrant,
        "agent": agent,
    }

    baseline = None
    regressions: list[str] = []
    if args.check:
        if not args.check.exists():
            print(f"warn: baseline {args.check} missing; nothing gated", file=sys.stderr)
        else:
            baseline = json.loads(args.check.read_text())
            regressions = check_baseline(result, baseline)
    if args.update_baseline:
        update_baseline(result, args.update_baseline)
        print(f"baseline written to {args.update_baseline}", file=sys.stderr)

    summary = summary_markdown(result, baseline)
    print(summary, file=sys.stderr)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(summary)
    payload = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n")
    else:
        print(payload)

    if regressions:
        print("REGRESSIONS:", file=sys.stderr)
        for r in regressions:
            print(f"  {r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
