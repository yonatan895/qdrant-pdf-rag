"""Mock-backed sim tier for the MCP bridge (marker: ``integration``).

The bridge runs as a real subprocess over loopback HTTP against a runtime
mock z/OS tree. The one guarantee only this tier proves: the agent's real
HttpZoweMCP client and the real bridge process agree on the JSON-RPC wire
(each side is otherwise tested against a fake of the other). Mock tree is
generated at runtime, nothing committed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest

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


def test_fetch_live_over_wire(bridge_url) -> None:
    """fetch_live drives a real HttpZoweMCP against the subprocess bridge:
    a job-failure query returns mock spool bytes; a manual query makes
    zero bridge calls (proven by tools_used, not by mocking). The
    access-log line itself is pinned hermetically in
    test_mcp_ftp_bridge.py, not via this subprocess pipe."""
    from mainframe_rag.agent import live_state
    from mainframe_rag.agent.zowe_mcp import HttpZoweMCP
    from mainframe_rag.config import Settings

    url, _proc = bridge_url
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

