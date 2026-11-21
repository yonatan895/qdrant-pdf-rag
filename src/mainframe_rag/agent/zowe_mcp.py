"""Agent-side MCP client for the read-only FTP bridge (ADR-0003, ADR-0012).

Minimal Streamable-HTTP JSON-RPC over httpx2 — initialize, tools/list,
tools/call only. No new dependency. Sync by protocol (callers offload
with asyncio.to_thread, like the reranker leg).

First egress W3C injection in the codebase: traceparent headers ride the
bridge POST so tools.call joins the request trace. Scoped to this client
only — embed/LLM/Qdrant calls are other teams' services and stay as-is.
Fail-open: injection failure logs at debug, the request proceeds untraced.

Security invariants (#1302, ADR-0012):
- `ZoweMCPError` carries fixed, allowlisted messages and stable codes only —
  never raw upstream `isError` text or exception bodies.
- When `zowe_mcp_auth_token` is configured, sends `Authorization: Bearer ...`
  matching the bridge's caller authentication middleware (ADR-0012 D4 point 2).
"""

from __future__ import annotations

import itertools
import logging
from typing import TYPE_CHECKING

import httpx2
from opentelemetry import propagate

from mainframe_rag.config import new_http_client
from mainframe_rag.logs import error_type

if TYPE_CHECKING:
    from mainframe_rag.config import Settings

log = logging.getLogger("agent")

# Closed allowlist, mirrored from the bridge dispatch table (ADR-0003: adding
# a tool is a new ADR, never a flag flip). Startup asserts the server's
# tools/list is a subset — a server registering more refuses to serve.
ALLOWLIST = ("dataset_read", "uss_read", "job_status", "jes_spool_read")

_MCP_PATH = "/mcp"

# Closed mapping from bridge error-code prefixes to fixed client-safe messages.
# Upstream text after `:` (or any unrecognized payload) is NEVER echoed (#1302).
_BRIDGE_ERROR_MAP: dict[str, tuple[str, str]] = {
    "not_found": ("not_found", "requested z/OS target was not found"),
    "denied": ("denied", "z/OS target is not permitted"),
    "logon_failed": ("logon_failed", "z/OS FTP authentication failed"),
    "tls_error": ("tls_error", "z/OS FTPS verification failed"),
    "timeout": ("timeout", "z/OS bridge request timed out"),
    "jes_unavailable": ("jes_unavailable", "z/OS JES interface is unavailable"),
    "invalid_params": ("invalid_request", "z/OS bridge request parameters were invalid"),
    "not_configured": ("not_configured", "z/OS bridge is not configured"),
    "upstream_error": ("upstream_error", "z/OS bridge request failed"),
}


class ZoweMCPError(Exception):
    """Stable client-visible failure for one tool call (code + fixed message only;
    upstream bodies and exception text never enter this exception)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _classify_tool_error(content: list) -> tuple[str, str]:
    """Map bridge `isError` content to a fixed (code, message) pair.

    Reads only the leading code prefix before `:` on the first text item and
    matches against `_BRIDGE_ERROR_MAP`. Never echoes arbitrary upstream text.
    """
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            prefix = item["text"].split(":", 1)[0].strip()
            mapped = _BRIDGE_ERROR_MAP.get(prefix)
            if mapped is not None:
                return mapped
            break
    return "tool_error", "z/OS bridge tool call failed"


class HttpZoweMCP:
    """Production client: JSON-RPC POSTs to the FTP bridge."""

    def __init__(
        self,
        settings: Settings,
        client: httpx2.Client | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = (settings.zowe_mcp_base_url or "").rstrip("/")
        self._auth_token = (settings.zowe_mcp_auth_token or "").strip() or None
        self._timeout = settings.zowe_mcp_timeout_s
        self._client = client
        self._ids = itertools.count(1)

    def _http(self) -> httpx2.Client:
        if self._client is None:
            self._client = new_http_client(self._timeout, self._settings.http_connect_retries)
        return self._client

    def _post(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._auth_token is not None:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        try:
            propagate.inject(headers)
        except Exception:  # noqa: BLE001 — fail-open, request proceeds untraced
            log.debug("otel egress inject failed; continuing untraced")
        try:
            resp = self._http().post(
                f"{self._base_url}{_MCP_PATH}", json=payload, headers=headers, timeout=self._timeout
            )
            if resp.status_code in (401, 403):
                raise ZoweMCPError("unauthorized", "live state authentication failed")
            resp.raise_for_status()
            data = resp.json()
        except ZoweMCPError:
            raise
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
        raise ZoweMCPError with fixed messages — never leaking upstream text."""
        if name not in ALLOWLIST:
            raise ZoweMCPError("invalid_request", "tool is not allowlisted")
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
            code, message = _classify_tool_error(result["content"])
            raise ZoweMCPError(code, message)
        return result

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()


def build_zowe_mcp(settings: Settings, client: httpx2.Client | None = None) -> HttpZoweMCP | None:
    """Single dispatch point (never branch on zowe flags elsewhere): off ->
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
        return error_type(exc)
    extras = [n for n in names if n not in ALLOWLIST]
    if extras:
        return f"bridge registers non-allowlisted tools: {sorted(extras)}"
    if not names:
        return "bridge registered zero tools"
    return None
