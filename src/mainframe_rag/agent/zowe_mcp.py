"""Agent-side MCP client for the read-only FTP bridge (ADR-0003, phase 2).

Minimal Streamable-HTTP JSON-RPC over httpx2 — initialize, tools/list,
tools/call only. No new dependency. Sync by protocol (callers offload
with asyncio.to_thread, like the reranker leg).

First egress W3C injection in the codebase: traceparent headers ride the
bridge POST so tools.call joins the request trace. Scoped to this client
only — embed/LLM/Qdrant calls are other teams' services and stay as-is.
Fail-open: injection failure logs at debug, the request proceeds untraced.
"""

from __future__ import annotations

import itertools
import logging
from typing import TYPE_CHECKING

import httpx2
from opentelemetry import propagate

if TYPE_CHECKING:
    from mainframe_rag.config import Settings

log = logging.getLogger("agent")

# Closed allowlist, mirrored from the bridge dispatch table (ADR-0003: adding
# a tool is a new ADR, never a flag flip). Startup asserts the server's
# tools/list is a subset — a server registering more refuses to serve.
ALLOWLIST = ("dataset_read", "uss_read", "job_status", "jes_spool_read")

_MCP_PATH = "/mcp"


class ZoweMCPError(Exception):
    """Stable client-visible failure for one tool call (code + message only;
    upstream bodies and exception text stay in logs)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HttpZoweMCP:
    """Production client: JSON-RPC POSTs to the sidecar bridge."""

    def __init__(
        self,
        settings: Settings,
        client: httpx2.Client | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = (settings.zowe_mcp_base_url or "").rstrip("/")
        self._timeout = settings.zowe_mcp_timeout_s
        self._client = client
        self._ids = itertools.count(1)

    def _http(self) -> httpx2.Client:
        if self._client is None:
            self._client = httpx2.Client(
                timeout=self._timeout,
                transport=httpx2.HTTPTransport(retries=self._settings.http_connect_retries),
            )
        return self._client

    def _post(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            propagate.inject(headers)
        except Exception:  # noqa: BLE001 — fail-open, request proceeds untraced
            log.debug("otel egress inject failed; continuing untraced")
        try:
            resp = self._http().post(
                f"{self._base_url}{_MCP_PATH}", json=payload, headers=headers, timeout=self._timeout
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx2.HTTPStatusError, httpx2.RequestError, ValueError) as exc:
            raise ZoweMCPError("upstream_error", "live state request failed") from exc
        if not isinstance(data, dict) or "result" not in data:
            raise ZoweMCPError("upstream_error", "live state request failed")
        return data["result"]

    def list_tools(self) -> list[str]:
        """Server-registered tool names (startup allowlist assertion input)."""
        result = self._post(
            {"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/list", "params": {}}
        )
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise ZoweMCPError("upstream_error", "live state request failed")
        names = [t.get("name") for t in tools if isinstance(t, dict)]
        return [n for n in names if isinstance(n, str)]

    def call_tool(self, name: str, arguments: dict[str, str]) -> dict:
        """One tool call. MCP isError results and transport failures both
        raise ZoweMCPError — the orchestrator degrades, never leaks."""
        if name not in ALLOWLIST:
            raise ZoweMCPError("invalid_request", f"tool not allowlisted: {name}")
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise ZoweMCPError("upstream_error", "live state request failed")
        if result.get("isError"):
            texts = [
                item.get("text", "")
                for item in result["content"]
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            raise ZoweMCPError("tool_error", "; ".join(texts)[:500] or "tool reported failure")
        return result

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()


def build_zowe_mcp(settings: Settings, client: httpx2.Client | None = None) -> HttpZoweMCP | None:
    """Single dispatch point (never branch on zowe flags elsewhere): off →
    None; on without a base URL raises fail-closed at lifespan."""
    if not settings.zowe_mcp_enabled:
        return None
    if not settings.zowe_mcp_base_url:
        raise RuntimeError(
            "ZOWE_MCP_ENABLED is true but ZOWE_MCP_BASE_URL is unset. "
            "Point it at the bridge sidecar, or disable the flag."
        )
    return HttpZoweMCP(settings, client)


def probe_zowe_mcp(zowe_mcp: HttpZoweMCP) -> str | None:
    """Best-effort tools/list ping for lifespan startup. Returns None when
    every registered tool is allowlisted, else a short error string for a
    loud startup warning. Warn-only by design: live state is opt-in, so a
    dead or surprising bridge must never keep the agent from listening."""
    try:
        names = zowe_mcp.list_tools()
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"[:200]
    extras = [n for n in names if n not in ALLOWLIST]
    if extras:
        return f"bridge registers non-allowlisted tools: {sorted(extras)}"
    if not names:
        return "bridge registered zero tools"
    return None
