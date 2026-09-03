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
- within-run determinism after load;
- chaos survival (``MOCK_ERROR_RATE`` abort storm): every stream classifies
  as complete XOR aborted, aborted streams carry ``event: error`` with no
  ``final``, and each aborted stream leaves exactly one ``stream_truncated``
  ``answer_alert`` joined by ``request_id`` — the live proof of the
  truncation fix;
- TTFT floor under a paced mock (``MOCK_TTFT_MS``): agent ``ttft_ms`` never
  precedes the model's first byte.

Fail-closed: ``QdrantSimError``, startup timeouts, and zero-request phases
raise — nothing here skips.

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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTION = "load-hash"
MOCK_DIM = 32  # mock-side only; the tier embeds with hash mode

# Chaos leg: every second chat request aborts mid-stream. Mixed-shape
# assertions need both outcomes in one run: at p=0.5 over 16 streams the
# all-same chance is 2^-15, so both classes are near-certain without any
# seed dependence (MOCK_SEED is still pinned for hygiene).
CHAOS_ERROR_RATE = "0.5"
CHAOS_SEED = "11"
# TTFT leg: the mock holds first-byte 500ms; the agent's first-token
# arrival cannot precede it. The 400ms floor keeps 100ms of timer
# slack — scheduling only ever delays arrivals, so the floor is
# deterministic-safe while still decisive (unpaced TTFT is ~ms).
TTFT_MS = "500"
TTFT_SEED = "7"
TTFT_FLOOR_MS = 400

# Agent log files by served URL: the chaos phase joins answer_alert lines
# to its aborted streams through its own agent's log.
_AGENT_LOG: dict[str, Path] = {}

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


def _start_mock(tag: str, extra_env: dict[str, str]) -> tuple[str, Callable[[], None]]:
    """One mock server with the given MOCK_* knobs over real loopback HTTP
    (sim-tier pattern). Env is saved/restored; the server stops via the
    returned thunk."""
    import importlib.util
    from http.server import ThreadingHTTPServer

    watched = ("MOCK_DIM", *extra_env)
    saved = {key: os.environ.get(key) for key in watched}
    os.environ["MOCK_DIM"] = str(MOCK_DIM)
    os.environ.update(extra_env)
    try:
        spec = importlib.util.spec_from_file_location(f"mock_vllm_load_{tag}", REPO_ROOT / "scripts" / "mock_vllm.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    def _stop() -> None:
        server.shutdown()
        server.server_close()

    return url, _stop


@pytest.fixture(scope="session")
def mock_url():
    """Clean model stand-in: instant, infallible, deterministic."""
    url, stop = _start_mock("clean", {})
    yield url
    stop()


@pytest.fixture(scope="session")
def mock_url_chaos():
    """Abort storm: half the chat requests die mid-stream (first chunk,
    clean close, no [DONE]) — the wire shape the truncation fix must
    survive, live."""
    url, stop = _start_mock("chaos", {"MOCK_ERROR_RATE": CHAOS_ERROR_RATE, "MOCK_SEED": CHAOS_SEED})
    yield url
    stop()


@pytest.fixture(scope="session")
def mock_url_ttft():
    """Paced model: 500ms first-byte latency on every chat request."""
    url, stop = _start_mock("ttft", {"MOCK_TTFT_MS": TTFT_MS, "MOCK_SEED": TTFT_SEED})
    yield url
    stop()


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


def _spawn_agent(
    tag: str,
    qdrant_url: str,
    mock_url: str,
    collection: str,
    tmp_path_factory,
) -> tuple[str, subprocess.Popen, Path]:
    """A real uvicorn agent on loopback (LLM_STREAM=true, the L3 venue):
    threaded load exercises the true HTTP stack, not TestClient."""
    port = _free_port()
    log_dir = tmp_path_factory.mktemp(f"load-agent-{tag}")
    log_path = log_dir / "agent-stdout.log"
    env = {
        **os.environ,
        "QDRANT_URL": qdrant_url,
        "QDRANT_COLLECTION": collection,
        "EMBED_MODE": "hash",
        "ALLOW_HASH_MODE": "true",
        "LLM_BASE_URL": f"{mock_url}/v1",
        "LLM_MODEL_REASONING": "mock-reasoning",
        "LLM_STREAM": "true",
        "PYTHONUNBUFFERED": "1",
    }
    print(f"\n[load-tier:{tag}] mock={mock_url} qdrant={qdrant_url} agent-port={port}", flush=True)
    # Agent stdout goes to a file, never a pipe: under threaded load the
    # agent logs thousands of JSON lines, and an unread pipe would fill and
    # wedge every request (every load request timing out at once). The child
    # keeps its own fd after spawn, so our handle closes right away.
    with open(log_path, "w") as log_file:
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
    print(f"[load-tier:{tag}] spawned pid={proc.pid}", flush=True)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + READY_TIMEOUT_S
    ready = False
    last: str = "no attempt"
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        if proc.poll() is not None:
            out = log_path.read_text()[-2000:]
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
            print(f"[load-tier:{tag}] still waiting ({attempts}s): {last}", flush=True)
        time.sleep(1.0)
    if not ready:
        proc.terminate()
        out = log_path.read_text()[-2000:]
        raise RuntimeError(f"agent not ready within {READY_TIMEOUT_S:.0f}s (last: {last}): {out}")
    _AGENT_LOG[url] = log_path
    return url, proc, log_path


def _stop_agent(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15.0)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def agent_url(qdrant_url, mock_url, ingested, tmp_path_factory):
    url, proc, _ = _spawn_agent("clean", qdrant_url, mock_url, ingested, tmp_path_factory)
    yield url
    _stop_agent(proc)


@pytest.fixture(scope="session")
def agent_url_chaos(qdrant_url, mock_url_chaos, ingested, tmp_path_factory):
    """Agent facing the abort storm. Shares the load collection (read-only
    workload); the chaos comes only from its model URL."""
    url, proc, _ = _spawn_agent("chaos", qdrant_url, mock_url_chaos, ingested, tmp_path_factory)
    yield url
    _stop_agent(proc)


@pytest.fixture(scope="session")
def agent_url_ttft(qdrant_url, mock_url_ttft, ingested, tmp_path_factory):
    """Agent facing the paced model."""
    url, proc, _ = _spawn_agent("ttft", qdrant_url, mock_url_ttft, ingested, tmp_path_factory)
    yield url
    _stop_agent(proc)


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


def test_aborted_streams_surface_error_without_final(agent_url_chaos):
    """Chaos survival, live: under the abort storm every stream classifies
    as complete XOR aborted. Aborted streams carry token deltas, then
    event: error with no final (the truncation-fix contract); completed
    streams keep the full integrity shape. Both classes must appear —
    all-complete would prove nothing about the abort path."""
    n_streams = _env_int("LOAD_STREAMS", 16)
    workers = _env_int("LOAD_STREAM_WORKERS", 4)
    classes: dict[int, str] = {}
    lock = threading.Lock()

    def one_stream(i: int) -> None:
        query = QUERIES[i % len(QUERIES)]
        status, _, text = _read_stream(agent_url_chaos, query)
        assert status == 200, f"chaos stream {i}: status {status}: {text[:200]}"
        events = _parse_app_sse(text)
        kinds = [kind for kind, _ in events]
        finals = [data for kind, data in events if kind == "final"]
        errors = [data for kind, data in events if kind == "error"]
        if errors:
            assert "token" in kinds, f"chaos stream {i}: error without any token"
            assert finals == [], f"chaos stream {i}: aborted stream must not emit final"
            assert len(errors) == 1, f"chaos stream {i}: exactly one error event"
            assert errors[0]["code"] == "upstream_error", f"chaos stream {i}: {errors[0]}"
            cls = "aborted"
        else:
            assert len(finals) == 1, f"chaos stream {i}: expected exactly one final"
            assert finals[0]["finish_reason"] == "stop"
            assert len(finals[0]["citations"]) >= 1
            cls = "complete"
        with lock:
            classes[i] = cls

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one_stream, range(n_streams)))
    assert len(classes) == n_streams, "every chaos stream must classify"
    assert set(classes.values()) == {"complete", "aborted"}, (
        f"the storm must produce both shapes, got {sorted(set(classes.values()))}"
    )


def test_aborted_streams_leave_truncation_alerts(agent_url_chaos):
    """Observability join: every aborted stream leaves exactly one
    stream_truncated answer_alert, and the alert carries counts only —
    never response text (log contract). Joined by request_id presence;
    windowed to this phase via the log offset."""
    log_path = _AGENT_LOG[agent_url_chaos]
    start_offset = log_path.stat().st_size
    n_streams = _env_int("LOAD_STREAMS", 16)

    aborted = 0
    for i in range(n_streams):
        _, _, text = _read_stream(agent_url_chaos, QUERIES[i % len(QUERIES)])
        if any(kind == "error" for kind, _ in _parse_app_sse(text)):
            aborted += 1
    assert aborted > 0, "the storm must abort at least one stream in the join window"

    with open(log_path) as handle:
        handle.seek(start_offset)
        appended = handle.read()
    alerts = [
        json.loads(line)
        for line in appended.splitlines()
        if '"answer_alert"' in line and '"stream_truncated"' in line
    ]
    assert len(alerts) == aborted, (
        f"{aborted} aborted streams must leave {aborted} alerts, got {len(alerts)}"
    )
    for alert in alerts:
        assert alert.get("request_id"), "truncation alert without request_id undercounts the join"
        assert "Partial" not in json.dumps(alert), "alert must not carry response text"
        assert "[DONE]" in alert.get("detail", ""), "alert must name the missing terminator"


def test_ttft_floor_holds_under_paced_mock(agent_url_ttft):
    """TTFT bound, live: with the model holding first-byte 500ms, the
    agent's first-token arrival cannot precede it. The 400ms floor keeps
    timer slack; scheduling only delays arrivals, so it is
    deterministic-safe while decisive (unpaced TTFT is single-digit ms).
    Streams still complete cleanly under pacing."""
    n_streams = _env_int("LOAD_STREAMS", 16)
    workers = _env_int("LOAD_STREAM_WORKERS", 4)

    def one_stream(i: int) -> None:
        _, _, text = _read_stream(agent_url_ttft, QUERIES[i % len(QUERIES)])
        events = _parse_app_sse(text)
        finals = [data for kind, data in events if kind == "final"]
        assert len(finals) == 1, f"paced stream {i}: expected exactly one final"
        final = finals[0]
        assert final["finish_reason"] == "stop"
        assert len(final["citations"]) >= 1
        assert final["ttft_ms"] is not None, f"paced stream {i}: final without ttft_ms"
        assert final["ttft_ms"] >= TTFT_FLOOR_MS, (
            f"paced stream {i}: ttft {final['ttft_ms']}ms precedes the 500ms mock pace"
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one_stream, range(n_streams)))
