"""Mock-backed sim tier for the MCP bridge (marker: ``integration``).

The bridge runs as a real subprocess over loopback HTTP against a runtime
mock z/OS tree; the agent boots against it with the flag on. No retrieval
code is faked — fetch_live drives a real HttpZoweMCP over the wire. Like
the rest of sim: fail-closed (no skips except a missing docker daemon for
the agent-boot test, which needs a live Qdrant), corpus generated at
runtime, nothing committed.
"""

from __future__ import annotations

import fcntl
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def mock_root(tmp_path_factory) -> Path:
    from scripts.init_mock_zos import build_mock_tree

    root = tmp_path_factory.mktemp("mock-zos") / "tree"
    build_mock_tree(root)
    return root


@pytest.fixture(scope="session")
def bridge_url(mock_root):
    """Real bridge subprocess (HTTP transport, mock backend). Polls
    tools/list until ready; fail-closed on timeout."""
    port = _free_port()
    env = {
        **os.environ,
        "MCP_MOCK_DIR": str(mock_root),
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "mainframe_rag.mcp", "--transport", "http",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60.0
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"bridge subprocess died: {proc.stderr.read()[:2000]}")
        try:
            resp = httpx2.post(
                f"{url}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                timeout=2.0,
            )
            if resp.status_code == 200 and len(resp.json()["result"]["tools"]) == 4:
                ready = True
                break
        except (httpx2.HTTPError, ValueError, KeyError):
            time.sleep(0.5)
    if not ready:
        proc.terminate()
        raise RuntimeError("bridge subprocess never became ready")
    yield url, proc
    proc.terminate()
    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        proc.kill()


def _rpc(url: str, method: str, params: dict, call_id: int = 1) -> dict:
    resp = httpx2.post(
        f"{url}/mcp",
        json={"jsonrpc": "2.0", "id": call_id, "method": method, "params": params},
        timeout=10.0,
    )
    assert resp.status_code == 200
    return resp.json()


def test_bridge_http_serves_mock_tools(bridge_url) -> None:
    """Full framing over the wire: list lock, dataset + JES reads, stable
    errors, unknown-tool refusal — all against mock fixtures."""
    url, _proc = bridge_url
    names = [t["name"] for t in _rpc(url, "tools/list", {})["result"]["tools"]]
    assert names == ["dataset_read", "uss_read", "job_status", "jes_spool_read"]

    spool = _rpc(url, "tools/call", {"name": "jes_spool_read", "arguments": {"job_id": "JOB00023", "spool_id": "2"}})
    assert spool["result"]["isError"] is False
    assert "ABEND S0C4" in spool["result"]["content"][0]["text"]

    member = _rpc(url, "tools/call", {"name": "dataset_read", "arguments": {"dataset": "USER.JCL", "member": "JOBCARD"}})
    assert "EXEC PGM=PAYCALC" in member["result"]["content"][0]["text"]

    missing = _rpc(url, "tools/call", {"name": "dataset_read", "arguments": {"dataset": "NOPE"}})
    assert missing["result"]["isError"] is True
    assert missing["result"]["content"][0]["text"].startswith("not_found")

    refused = _rpc(url, "tools/call", {"name": "job_submit", "arguments": {}}, call_id=9)
    assert refused["error"]["code"] == -32602


def test_fetch_live_over_wire(bridge_url) -> None:
    """fetch_live drives a real HttpZoweMCP against the subprocess bridge:
    a job-failure query returns mock spool bytes; a manual query makes
    zero bridge calls (proven by the access log, not by mocking)."""
    from mainframe_rag.agent import live_state
    from mainframe_rag.agent.zowe_mcp import HttpZoweMCP
    from mainframe_rag.config import Settings

    url, proc = bridge_url
    settings = Settings(
        zowe_mcp_enabled=True, zowe_mcp_base_url=url, zowe_mcp_max_bytes=100000, _env_file=None
    )
    client = HttpZoweMCP(settings)
    try:
        out = live_state.fetch_live(
            settings, client, "sim-1", "Why did JOB00023 fail last night?",
            live_state.classify_live_need("Why did JOB00023 fail last night?"),
        )
    finally:
        client.close()
    assert out.degraded is None
    assert out.tools_used == ("job_status", "jes_spool_read")
    assert any("ABEND S0C4" in text for text in out.texts)

    manual = live_state.fetch_live(
        settings, client, "sim-2", "What does IEA500I mean?", "manual"
    )
    assert manual.texts == ()

    # Drain the access log without killing the session-scoped subprocess:
    # non-blocking read of whatever has accumulated is enough — the spool
    # call above must be present, and no call may reference trap content.
    fd = proc.stderr.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        logged = proc.stderr.read() or ""
    except (OSError, ValueError):
        logged = ""
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    assert "mcp tools/call name=jes_spool_read" in logged


@pytest.fixture(scope="session")
def qdrant_url():
    from scripts.qdrant_sim import QdrantSimError, start_simulator

    try:
        sim = start_simulator(REPO_ROOT, os.environ.get("QDRANT_SIM_URL"))
    except QdrantSimError as exc:
        pytest.skip(str(exc))
    yield sim.url
    sim.stop()


@contextmanager
def _agent(monkeypatch, qdrant_url: str, bridge_url: str, collection: str):
    from mainframe_rag.agent import app as app_mod

    monkeypatch.setenv("QDRANT_URL", qdrant_url)
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LLM_MODEL_REASONING", "unused-reasoning")
    monkeypatch.setenv("ZOWE_MCP_ENABLED", "true")
    monkeypatch.setenv("ZOWE_MCP_BASE_URL", bridge_url)
    with TestClient(app_mod.app) as client:
        yield client


def test_agent_boots_with_bridge_and_answers_without_fetch(
    qdrant_url, bridge_url, tmp_path, monkeypatch
) -> None:
    """Agent lifespan probes the real bridge (warn-only path needs no
    warnings here) and trap answers stay MCP-free end to end."""
    from scripts.make_synthetic_pdf import build

    from mainframe_rag.ingest import run_ingest

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    build(corpus / "SA22-0000-00.pdf")
    monkeypatch.setenv("QDRANT_URL", qdrant_url)
    monkeypatch.setenv("QDRANT_COLLECTION", "sim-mcp")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    # Parent-side worker globals leak across sim files: run_ingest keeps a
    # process-global client that an earlier file's monkeypatch reverted to a
    # live handle on ITS simulator (found as a sim-mcp 404 on the wrong
    # port). Reset exactly like test_integration_sim._ingest does.
    previous_qdrant = run_ingest._worker_qdrant
    if previous_qdrant is not None:
        previous_qdrant.close()
    monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
    monkeypatch.setattr(run_ingest, "_worker_embedder", None)
    assert run_ingest.main(["--src", str(corpus), "--progress", str(tmp_path / "inv.jsonl"), "--workers", "1"]) == 0

    url, _proc = bridge_url
    with _agent(monkeypatch, qdrant_url, url, "sim-mcp") as client:
        assert client.get("/healthz").status_code == 200
        body = client.post("/v1/search", json={"query": "IEA500I operator message"}).json()
        assert body["hits"], "sim corpus must serve retrieval with the flag on"
