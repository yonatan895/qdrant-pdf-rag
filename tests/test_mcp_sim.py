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


_SIM_AUTH_TOKEN = "sim-bridge-test-token"


@pytest.fixture(scope="session")
def bridge_url(mock_root):
    """Real bridge subprocess (HTTP transport, mock backend, ADR-0012 pilot
    allowlists + bearer auth). Polls tools/list until ready; fail-closed on timeout."""
    port = _free_port()
    env = {
        **os.environ,
        "MCP_MOCK_DIR": str(mock_root),
        "MCP_ENABLED_TOOLS": "dataset_read,job_status,jes_spool_read",
        "MCP_DATASET_ALLOWLIST": "USER.PARMLIB,USER.JCL(JOBCARD)",
        "MCP_JES_JOBNAME_PREFIXES": "PAY,BACK",
        "MCP_JES_OWNER_PREFIXES": "READER",
        "MCP_BRIDGE_AUTH_TOKEN": _SIM_AUTH_TOKEN,
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
                headers={"Authorization": f"Bearer {_SIM_AUTH_TOKEN}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                timeout=2.0,
            )
            if resp.status_code == 200 and len(resp.json()["result"]["tools"]) == 3:
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
    bearer auth is enforced, allowlisted dataset_read & DD-scoped jes_spool_read
    succeed, forbidden DD (JESJCL) and off-allowlist datasets are denied."""
    from mainframe_rag.agent import live_state
    from mainframe_rag.agent.zowe_mcp import HttpZoweMCP, ZoweMCPError
    from mainframe_rag.config import Settings

    url, _proc = bridge_url
    # Unauthenticated request is rejected 401
    unauth = httpx2.post(
        f"{url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        timeout=2.0,
    )
    assert unauth.status_code == 401

    settings = Settings(
        zowe_mcp_enabled=True,
        zowe_mcp_base_url=url,
        zowe_mcp_auth_token=_SIM_AUTH_TOKEN,
        zowe_mcp_max_bytes=100000,
        _env_file=None,
    )
    client = HttpZoweMCP(settings)
    try:
        out = live_state.fetch_live(
            settings, client, "sim-1", "Why did JOB00023 fail last night?",
            live_state.classify_live_need("Why did JOB00023 fail last night?"),
        )
        assert out.degraded is None
        assert out.tools_used == ("job_status", "jes_spool_read")
        assert any("ABEND S0C4" in text for text in out.texts)

        # Allowlisted dataset_read succeeds; off-allowlist and JESJCL spool are denied
        ds = client.call_tool("dataset_read", {"dataset": "USER.PARMLIB"})
        assert "LFAREA=(1G,64M)" in ds["content"][0]["text"]
        with pytest.raises(ZoweMCPError) as denied_ds:
            client.call_tool("dataset_read", {"dataset": "SYS1.SECRET"})
        assert denied_ds.value.code == "denied"
        with pytest.raises(ZoweMCPError) as denied_jcl:
            client.call_tool("jes_spool_read", {"job_id": "JOB00023", "spool_id": "1"})
        assert denied_jcl.value.code == "denied"

        manual = live_state.fetch_live(
            settings, client, "sim-2", "What does IEA500I mean?", "manual"
        )
        assert manual.texts == ()
    finally:
        client.close()

