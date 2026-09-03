"""Load tier (marker ``integration``, run via ``make loadtest-mock``).

Same composition locally and in CI: real PDFs -> real ingest (hash mode)
into docker Qdrant (``images.txt`` pin, or ``QDRANT_SIM_URL``) -> a real
uvicorn agent plus the deterministic mock LLM over loopback HTTP. No
retrieval/LLM code is monkeypatched; ``scripts/mock_vllm.py`` is the only
stand-in.

Unlike ``make sim`` (correctness) this tier asserts ABSOLUTE contracts under
concurrency — never cross-environment comparisons (harness invariant #1):

- zero request errors and zero missing ``Server-Timing`` headers on
  ``/v1/search`` and ``/v1/answer`` under threaded load
  (``scripts/loadtest.py`` drives; this module judges);
- per-stream SSE integrity on ``/v1/answer?stream=true``: token deltas, then
  exactly one ``final`` (``finish_reason`` stop, >= 1 citation, ``ttft_ms``
  set) and no ``error`` event;
- citation parity stream-vs-search and stream-vs-JSON;
- fixed error shapes (``code`` + ``message`` only) with no leaked internals
  under concurrent bad requests;
- within-run determinism after load.

Fail-closed: ``QdrantSimError``, startup timeouts, and zero-request phases
raise — nothing here skips. The mock-abort chaos phase and the TTFT-bound
phase need the mock realism knobs and arrive separately; this composition
already isolates the model behind a URL so those phases only add env plus
assertions.

Knobs (CI-sane defaults, override locally): ``LOAD_SEARCH_CONCURRENCY``,
``LOAD_SEARCH_DURATION_S``, ``LOAD_ANSWER_CONCURRENCY``,
``LOAD_ANSWER_DURATION_S``, ``LOAD_STREAMS``, ``LOAD_STREAM_WORKERS``.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTION = "load-hash"
MOCK_DIM = 32  # mock-side only; the tier embeds with hash mode

# Queries proven to hit the load corpus (identifier + nl): every answer in
# every phase must carry >= 1 validated citation, so hitless queries have
# no place in this tier.
QUERIES = ["IEA500I operator message", "widget torque buffer"]

READY_TIMEOUT_S = 90.0

# Fixed error shapes under load: exact (status, code, message) plus raw-text
# markers that must never appear in a client body (error contract: fixed
# message + stable code client-side; tracebacks stay in server logs).
ERROR_PROBES = [
    ("GET", "/v1/nope", None, 404, "not_found", "not found"),
    ("GET", "/v1/search", None, 405, "method_not_allowed", "method not allowed"),
    ("POST", "/v1/search", {"query": 123}, 422, "invalid_request", "request body failed validation"),
]
LEAK_MARKERS = ("Traceback", 'File "', ".py\"", "httpx", "uvicorn", "starlette", "pydantic", "anyio")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.5, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def qdrant_url():
    """Fail-closed docker Qdrant: start failures raise, never skip. Shares
    scripts/qdrant_sim.py with the sim tier and the benchmark harness."""
    from scripts.qdrant_sim import start_simulator

    sim = start_simulator(REPO_ROOT, os.environ.get("QDRANT_SIM_URL"))
    yield sim.url
    sim.stop()


@pytest.fixture(scope="session")
def mock_url():
    """The model stand-in over real loopback HTTP (sim-tier pattern)."""
    import importlib.util

    old_dim = os.environ.get("MOCK_DIM")
    os.environ["MOCK_DIM"] = str(MOCK_DIM)
    try:
        spec = importlib.util.spec_from_file_location("mock_vllm_load", REPO_ROOT / "scripts" / "mock_vllm.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old_dim is None:
            os.environ.pop("MOCK_DIM", None)
        else:
            os.environ["MOCK_DIM"] = old_dim
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build, build_plain

    root = tmp_path_factory.mktemp("load-corpus")
    build(root / "SA22-0000-00.pdf")
    # Distinct body text, not just metadata: equal-text chunks tie in RRF
    # and flip top-1 between runs.
    build(
        root / "SA22-7777-01.pdf",
        doc_id="SA22-7777-01",
        title="Synthetic Initialization and Tuning Reference",
        message_id="IEB700I",
    )
    build_plain(root / "widget-guide.pdf")
    return root


@pytest.fixture(scope="session")
def ingested(qdrant_url, corpus, tmp_path_factory) -> str:
    """Real ingest into the load collection. Synchronous in-process call
    with a saved/restored environ (session scope cannot use monkeypatch);
    worker globals reset exactly like the sim tier so a warm session running
    both tiers cannot leak a stale client in."""
    from mainframe_rag.ingest import run_ingest

    progress = tmp_path_factory.mktemp("load-progress") / "inv.jsonl"
    try:
        httpx2.delete(f"{qdrant_url}/collections/{COLLECTION}", timeout=10.0)
    except httpx2.HTTPError:
        pass
    saved = dict(os.environ)
    os.environ.update(
        {
            "QDRANT_URL": qdrant_url,
            "QDRANT_COLLECTION": COLLECTION,
            "EMBED_MODE": "hash",
            "ALLOW_HASH_MODE": "true",
        }
    )
    try:
        previous_qdrant = run_ingest._worker_qdrant
        if previous_qdrant is not None:
            previous_qdrant.close()
        run_ingest._worker_qdrant = None
        run_ingest._worker_embedder = None
        rc = run_ingest.main(["--src", str(corpus), "--progress", str(progress), "--workers", "1"])
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert rc == 0, "load-tier ingest failed"
    records = [json.loads(line) for line in progress.read_text().splitlines() if line.strip()]
    assert [r["status"] for r in records] == ["upserted"] * 3
    assert all(r["chunks"] > 0 for r in records)
    return COLLECTION


@pytest.fixture(scope="session")
def agent_url(qdrant_url, mock_url, ingested, tmp_path_factory):
    """A real uvicorn agent on loopback (LLM_STREAM=true, the L3 venue):
    threaded load exercises the true HTTP stack, not TestClient."""
    port = _free_port()
    log_dir = tmp_path_factory.mktemp("load-agent")
    env = {
        **os.environ,
        "QDRANT_URL": qdrant_url,
        "QDRANT_COLLECTION": ingested,
        "EMBED_MODE": "hash",
        "ALLOW_HASH_MODE": "true",
        "LLM_BASE_URL": f"{mock_url}/v1",
        "LLM_MODEL_REASONING": "mock-reasoning",
        "LLM_STREAM": "true",
        "PYTHONUNBUFFERED": "1",
    }
    print(f"\n[load-tier] mock={mock_url} qdrant={qdrant_url} agent-port={port}", flush=True)
    # Agent stdout goes to a file, never a pipe: under threaded load the
    # agent logs thousands of JSON lines, and an unread pipe would fill and
    # wedge every request (every load request timing out at once). The child
    # keeps its own fd after spawn, so our handle closes right away.
    with open(log_dir / "agent-stdout.log", "w") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mainframe_rag.agent.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=env,
            cwd=REPO_ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    print(f"[load-tier] spawned pid={proc.pid}", flush=True)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + READY_TIMEOUT_S
    ready = False
    last: str = "no attempt"
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        if proc.poll() is not None:
            log_file.flush()
            out = (log_dir / "agent-stdout.log").read_text()[-2000:]
            raise RuntimeError(f"agent exited during startup: {out}")
        try:
            r = httpx2.get(f"{url}/healthz", timeout=2.0)
            if r.status_code == 200 and r.json().get("qdrant") is True:
                ready = True
                break
            last = f"status={r.status_code} body={r.text[:120]}"
        except httpx2.HTTPError as exc:
            last = f"{type(exc).__name__}"
        if attempts % 10 == 0:
            print(f"[load-tier] still waiting ({attempts}s): {last}", flush=True)
        time.sleep(1.0)
    if not ready:
        proc.terminate()
        out = (log_dir / "agent-stdout.log").read_text()[-2000:]
        raise RuntimeError(f"agent not ready within {READY_TIMEOUT_S:.0f}s (last: {last}): {out}")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=15.0)
    except subprocess.TimeoutExpired:
        proc.kill()


def _parse_app_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    current_event = "message"
    current_data: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            if current_data:
                events.append((current_event, json.loads("\n".join(current_data))))
                current_event, current_data = "message", []
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            current_data.append(line[5:].strip())
    if current_data:
        events.append((current_event, json.loads("\n".join(current_data))))
    return events


def _read_stream(base_url: str, query: str) -> tuple[int, dict[str, str], str]:
    """One raw ?stream=true read: status, response headers, raw SSE text."""
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=60)
    try:
        conn.request(
            "POST",
            "/v1/answer?stream=true",
            body=json.dumps({"query": query}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, resp.read().decode()
    finally:
        conn.close()


def test_search_load_has_zero_errors_and_full_timings(agent_url):
    from scripts.loadtest import run_load

    conc = _env_int("LOAD_SEARCH_CONCURRENCY", 8)
    dur = _env_float("LOAD_SEARCH_DURATION_S", 10.0)
    res = run_load(agent_url, "search", QUERIES, conc, dur)
    assert res["requests"] > 0, "the search phase sent no requests"
    assert res["errors"] == 0, f"search errors under load: {res['errors']}"
    assert res["missing_timings"] == 0, "search responses missing Server-Timing"
    assert "embed_ms" in res["stages"] and "qdrant_ms" in res["stages"]


def test_answer_load_has_zero_errors_and_ttft(agent_url):
    from scripts.loadtest import run_load

    conc = _env_int("LOAD_ANSWER_CONCURRENCY", 4)
    dur = _env_float("LOAD_ANSWER_DURATION_S", 10.0)
    res = run_load(agent_url, "answer", QUERIES, conc, dur)
    assert res["requests"] > 0, "the answer phase sent no requests"
    assert res["errors"] == 0, f"answer errors under load: {res['errors']}"
    assert res["missing_timings"] == 0, "answer responses missing Server-Timing"
    assert "ttft_ms" in res["stages"], "LLM_STREAM=true server must report ttft"


def test_answer_streams_keep_integrity_under_concurrency(agent_url):
    """Per-stream contract under concurrency: token deltas, then exactly one
    final (stop, >= 1 citation, ttft set) and no error event — the shape the
    truncation fix (PR #107) protects."""
    from scripts.loadtest import parse_server_timing

    n_streams = _env_int("LOAD_STREAMS", 16)
    workers = _env_int("LOAD_STREAM_WORKERS", 4)

    def one_stream(i: int) -> None:
        query = QUERIES[i % len(QUERIES)]
        status, headers, text = _read_stream(agent_url, query)
        assert status == 200, f"stream {i}: status {status}: {text[:200]}"
        # Response headers carry only the stages known before the LLM runs
        # (the LLM streams after headers flush); llm/ttft surface in the
        # final payload instead.
        timings = parse_server_timing(headers.get("server-timing"))
        assert "embed_ms" in timings and "qdrant_ms" in timings, (
            f"stream {i}: Server-Timing without retrieval stages: {headers}"
        )
        events = _parse_app_sse(text)
        kinds = [kind for kind, _ in events]
        assert "token" in kinds, f"stream {i}: no token deltas"
        assert "error" not in kinds, f"stream {i}: unexpected error event"
        finals = [data for kind, data in events if kind == "final"]
        assert len(finals) == 1, f"stream {i}: expected exactly one final, got {len(finals)}"
        final = finals[0]
        assert final["finish_reason"] == "stop", f"stream {i}: {final['finish_reason']}"
        assert len(final["citations"]) >= 1, f"stream {i}: final without citations"
        assert final["ttft_ms"] is not None, f"stream {i}: final without ttft_ms"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one_stream, range(n_streams)))


def test_citation_parity_stream_vs_search_vs_json(agent_url):
    """Citations must agree across all three answer shapes on the loaded
    server: no cross-talk between concurrent requests."""
    for query in QUERIES:
        hits = httpx2.post(f"{agent_url}/v1/search", json={"query": query}, timeout=30.0).json()["hits"]
        hit_cites = {h["cite"] for h in hits}
        assert hit_cites, f"query {query!r} must hit the load corpus"

        _, _, text = _read_stream(agent_url, query)
        finals = [data for kind, data in _parse_app_sse(text) if kind == "final"]
        assert len(finals) == 1
        stream_cites = finals[0]["citations"]
        assert stream_cites, "stream final must carry citations"
        assert set(stream_cites) <= hit_cites, "stream citations must come from the hit set"

        body = httpx2.post(f"{agent_url}/v1/answer", json={"query": query}, timeout=60.0).json()
        assert body["citations"] == stream_cites, "JSON and stream citations must agree"


def test_error_shapes_hold_under_load_without_leaks(agent_url):
    """Concurrent bad requests keep the fixed error shapes: exact status +
    code + message, and no marker of internals anywhere in the bodies."""

    def one_probe(i: int) -> None:
        method, path, payload, status, code, message = ERROR_PROBES[i % len(ERROR_PROBES)]
        parts = urlsplit(agent_url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
        try:
            body = json.dumps(payload) if payload is not None else None
            conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read().decode()
        finally:
            conn.close()
        assert resp.status == status, f"probe {i}: status {resp.status}: {raw[:200]}"
        envelope = json.loads(raw)
        assert envelope == {"code": code, "message": message}, f"probe {i}: {raw[:200]}"
        for marker in LEAK_MARKERS:
            assert marker not in raw, f"probe {i}: leaked marker {marker!r}"

    n = 3 * _env_int("LOAD_STREAM_WORKERS", 4)
    with ThreadPoolExecutor(max_workers=_env_int("LOAD_STREAM_WORKERS", 4)) as pool:
        list(pool.map(one_probe, range(n)))


def test_determinism_after_load(agent_url):
    """Same query twice around the load phases: identical modulo request_id."""
    first = httpx2.post(f"{agent_url}/v1/search", json={"query": QUERIES[0]}, timeout=30.0).json()
    second = httpx2.post(f"{agent_url}/v1/search", json={"query": QUERIES[0]}, timeout=30.0).json()
    assert first["hits"], "the load corpus must hit"
    assert {k: v for k, v in first.items() if k != "request_id"} == {
        k: v for k, v in second.items() if k != "request_id"
    }
