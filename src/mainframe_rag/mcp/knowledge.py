"""Read-only knowledge MCP adapter over the agent's exact-evidence HTTP service
(issue #405 MCP1).

Thin on purpose. It maps two tools onto the agent's existing routes and nothing
else:

- `knowledge_search`  -> POST /v1/search   (no LLM; hits carry `reference`)
- `evidence_read`     -> GET  /v1/evidence/{reference}

There is no Qdrant client, no embedder, no entitlement or reference logic, no
database or query surface and no write capability here: retrieval, reference
verification, access decisions and size bounds all live in the shared service
behind that HTTP boundary, so success/denial/unknown/refusal outcomes are the
service's own. Incoming MCP request headers and tokens are never read or
forwarded; the adapter speaks to the service as its own deployment identity
(trusted caller boundary = network reachability of the configured base URL,
whose approval is a rollout input, #373).

Protocol: hand-rolled JSON-RPC 2.0 framing, like the FTP bridge in server.py;
there is no third-party MCP SDK, hence no new dependency to pin. Methods:
initialize, notifications/initialized, ping, tools/list, tools/call.
Everything awaits real I/O (httpx2.AsyncClient), so a cancelled tool call
cancels the in-flight upstream request; nothing runs in a worker thread.
Failures surface as MCP `isError` results carrying only the service's fixed
{code, message} envelope or a fixed adapter code, never upstream bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Protocol
from urllib.parse import quote

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER_INFO = {"name": "mainframe-knowledge", "version": "0.1.0"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

MAX_RESPONSE_BYTES = 1_100_000  # one read is capped at 1 MiB by the service


class KnowledgeBackend(Protocol):
    """(HTTP-style status, JSON body) for each shared-service operation."""

    async def search(
        self, query: str, product: str | None, version: str | None, limit: int
    ) -> tuple[int, dict]: ...

    async def read(
        self, reference: str, max_bytes: int | None, product: str | None, version: str | None
    ) -> tuple[int, dict]: ...


class HttpKnowledgeBackend:
    """The shared service over its additive HTTP surface. The client is
    injectable so tests can drive the real ASGI app without a socket."""

    def __init__(self, base_url: str, timeout_s: float = 15.0, client: Any = None) -> None:
        self._client = client or httpx2.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def _json(self, response: Any) -> tuple[int, dict]:
        if len(response.content) > MAX_RESPONSE_BYTES:
            return 502, {"code": "upstream_error", "message": "evidence read failed"}
        try:
            body = response.json()
        except ValueError:
            return 502, {"code": "upstream_error", "message": "evidence read failed"}
        return response.status_code, body if isinstance(body, dict) else {}

    async def search(self, query, product, version, limit):
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if product is not None:
            payload["product"] = product
        if version is not None:
            payload["version"] = version
        return await self._json(await self._client.post("/v1/search", json=payload))

    async def read(self, reference, max_bytes, product, version):
        params = {k: v for k, v in
                  (("max_bytes", max_bytes), ("product", product), ("version", version))
                  if v is not None}
        url = "/v1/evidence/" + quote(reference, safe="")
        return await self._json(await self._client.get(url, params=params))

    async def aclose(self) -> None:
        await self._client.aclose()


TOOL_SCHEMAS: dict[str, dict] = {
    "knowledge_search": {
        "description": "Search the indexed manuals (no LLM). Each hit carries its stored "
        "text, citation and, when an exact read can be promised, an opaque `reference`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "product": {"type": "string"},
                "version": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
            },
            "required": ["query"],
        },
    },
    "evidence_read": {
        "description": "Read the exact stored evidence for a `reference` from knowledge_search: "
        "complete chunk text, source revision, document/page identity, chunk identity and "
        "atomic-unit byte ranges. Refuses explicitly (unavailable, over budget, ...); never "
        "returns a prefix or another build's text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576},
                "product": {"type": "string"},
                "version": {"type": "string"},
            },
            "required": ["reference"],
        },
    },
}


def _error(id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def _ok(id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _opt_str(args: dict, name: str) -> str | None:
    value = args.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _int(args: dict, name: str, lo: int, hi: int, default: int | None) -> int | None:
    value = args.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer in [{lo}, {hi}]")
    return value


def _result(status: int, body: dict) -> dict:
    """One MCP result. Success carries the service body verbatim; every refusal
    carries only the service's fixed {code, message} envelope."""
    if 200 <= status < 300:
        return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}
    code, message = body.get("code"), body.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        code, message = "upstream_error", "evidence read failed"
    envelope = {"code": code, "message": message, "status": status}
    return {"isError": True, "content": [{"type": "text", "text": json.dumps(envelope)}]}


async def run_tool(name: str, args: dict, backend: KnowledgeBackend) -> dict:
    """Dispatch one tools/call. Bad arguments raise ValueError (-32602)."""
    if name == "knowledge_search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query (required) must be a non-empty string")
        limit = _int(args, "limit", 1, 40, 8)
        assert limit is not None
        product, version = _opt_str(args, "product"), _opt_str(args, "version")
        call = backend.search(query, product, version, limit)
    elif name == "evidence_read":
        reference = args.get("reference")
        if not isinstance(reference, str) or not reference:
            raise ValueError("reference (required) must be a string")
        max_bytes = _int(args, "max_bytes", 1, 1048576, None)
        product, version = _opt_str(args, "product"), _opt_str(args, "version")
        call = backend.read(reference, max_bytes, product, version)
    else:
        raise KeyError(name)
    try:
        status, body = await call
    except (httpx2.HTTPError, OSError):
        return _result(502, {})
    return _result(status, body)


async def handle_request(message: Any, backend: KnowledgeBackend) -> dict | None:
    """One decoded JSON-RPC message; None for notifications."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "request must be an object")
    method, msg_id = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if not isinstance(method, str):
        return _error(msg_id, INVALID_REQUEST, "missing method")
    if not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "params must be an object")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        requested = params.get("protocolVersion", PROTOCOL_VERSION)
        negotiated = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        return _ok(msg_id, {"protocolVersion": negotiated, "capabilities": {"tools": {}},
                            "serverInfo": SERVER_INFO})
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": [{"name": n, **s} for n, s in TOOL_SCHEMAS.items()]})
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or name not in TOOL_SCHEMAS:
            return _error(msg_id, INVALID_PARAMS, "unknown tool")
        if not isinstance(arguments, dict):
            return _error(msg_id, INVALID_PARAMS, "arguments must be an object")
        print(f"mcp tools/call name={name}", file=sys.stderr, flush=True)  # name only
        try:
            return _ok(msg_id, await run_tool(name, arguments, backend))
        except ValueError as exc:
            return _error(msg_id, INVALID_PARAMS, f"invalid tool call: {exc}")
    return _error(msg_id, METHOD_NOT_FOUND, f"unsupported method: {method}")


async def serve_stdio(backend: KnowledgeBackend) -> None:
    """Newline-delimited JSON-RPC; stdout carries replies only. Each message is
    handled as its own task so a slow read never blocks ping/other calls, and
    stdin EOF cancels whatever is still in flight."""
    loop = asyncio.get_running_loop()
    tasks: set[asyncio.Task] = set()

    async def one(raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            reply: dict | None = _error(None, PARSE_ERROR, "invalid JSON")
        else:
            try:
                reply = await handle_request(message, backend)
            except Exception as exc:  # noqa: BLE001 — framing must never die
                print(f"mcp knowledge error: {type(exc).__name__}", file=sys.stderr)
                reply = _error(message.get("id") if isinstance(message, dict) else None,
                               -32000, "internal error")
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()

    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            if line.strip():
                task = asyncio.create_task(one(line.strip()))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_app(backend: KnowledgeBackend) -> FastAPI:
    """Unary JSON-RPC POSTs at /mcp. A client disconnect cancels the handler
    task and with it the in-flight upstream request."""
    app = FastAPI(title="mainframe-knowledge", version=SERVER_INFO["version"])

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> JSONResponse:
        try:
            message = await request.json()
        except ValueError:
            return JSONResponse(_error(None, PARSE_ERROR, "invalid JSON"), status_code=400)
        try:
            reply = await handle_request(message, backend)
        except Exception:  # noqa: BLE001
            reply = _error(message.get("id") if isinstance(message, dict) else None,
                           -32000, "internal error")
        return JSONResponse({}, status_code=202) if reply is None else JSONResponse(reply)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args(argv)
    base_url = os.environ.get("KNOWLEDGE_API_BASE_URL", "").strip()
    if not base_url:
        print("mcp knowledge: KNOWLEDGE_API_BASE_URL must be set", file=sys.stderr)
        return 2
    try:
        timeout = float(os.environ.get("KNOWLEDGE_API_TIMEOUT_S", "15"))
    except ValueError:
        timeout = 15.0
    backend = HttpKnowledgeBackend(base_url, timeout)
    if args.transport == "stdio":
        asyncio.run(serve_stdio(backend))
        return 0
    import uvicorn

    uvicorn.run(create_app(backend), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
