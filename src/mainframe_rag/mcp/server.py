"""MCP framing for the FTP bridge: JSON-RPC dispatch, tool schemas, transports.

Supported methods: `initialize`, `notifications/initialized` (no reply),
`ping`, `tools/list`, `tools/call`. Anything else is -32601. Tool input
failures are -32602; tool *execution* failures ride the MCP `isError`
result shape (bridge.py), never protocol errors.

Transports: newline-delimited JSON-RPC on stdio (logs go to stderr only),
and unary JSON-RPC POSTs at `/mcp` over HTTP (plain JSON replies are
spec-legal for non-streaming tools).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mainframe_rag.mcp import bridge
from mainframe_rag.mcp.bridge import FTPConfig

ConnectFn = Callable[[FTPConfig], Any]

PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER_INFO = {"name": "mainframe-ftp-bridge", "version": "0.1.0"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def _error(id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def _ok(id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _str_arg(args: dict, name: str, required: bool = False, default: str = "") -> str | None:
    """Validated string arg, or None signalling an INVALID_PARAMS reply."""
    value = args.get(name, default)
    if not isinstance(value, str):
        return None
    if required and not value.strip():
        return None
    return value


def _run_tool(name: str, args: dict, config: bridge.FTPConfig, connect: ConnectFn) -> dict:
    """Dispatch one tools/call. Unknown tools and malformed args raise
    KeyError/ValueError, which handle_request maps to -32602."""
    session = bridge.FTPSession(connect(config), config)
    try:
        if name == "dataset_read":
            dataset = _str_arg(args, "dataset", required=True)
            member = _str_arg(args, "member")
            if dataset is None or member is None:
                raise ValueError("dataset (required) and member (optional) must be strings")
            return bridge.dataset_read(session, dataset, member or None)
        if name == "uss_read":
            path = _str_arg(args, "path", required=True)
            if path is None:
                raise ValueError("path (required) must be a string")
            return bridge.uss_read(session, path)
        if name == "job_status":
            return bridge.job_status(
                session,
                job_name=_str_arg(args, "job_name") or None,
                owner=_str_arg(args, "owner") or None,
                job_id=_str_arg(args, "job_id") or None,
            )
        if name == "jes_spool_read":
            job_id = _str_arg(args, "job_id", required=True)
            spool_id = _str_arg(args, "spool_id", required=True)
            if job_id is None or spool_id is None:
                raise ValueError("job_id and spool_id (required) must be strings")
            return bridge.jes_spool_read(session, job_id, spool_id)
    finally:
        try:
            session.ftp.quit()
        except Exception:  # noqa: BLE001, S110 — best-effort close only
            pass
    raise KeyError(name)


TOOL_SCHEMAS: dict[str, dict] = {
    "dataset_read": {
        "description": "Read a z/OS sequential dataset or PDS member (EBCDIC converted). "
        "A PDS name without member returns the member list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "description": "Fully qualified dataset name"},
                "member": {"type": "string", "description": "PDS member name (omit for sequential)"},
            },
            "required": ["dataset"],
        },
    },
    "uss_read": {
        "description": "Read a z/OS UNIX file (absolute path, EBCDIC converted).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute USS path"}},
            "required": ["path"],
        },
    },
    "job_status": {
        "description": "List batch jobs with status and return code, optionally filtered.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_name": {"type": "string"},
                "owner": {"type": "string"},
                "job_id": {"type": "string"},
            },
        },
    },
    "jes_spool_read": {
        "description": "Read one spool file of a job (JOBID.n; typical ids 1=JCL, "
        "2=system messages, 3+=SYSOUT — verify per site).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "spool_id": {"type": "string", "description": "Numeric spool file id"},
            },
            "required": ["job_id", "spool_id"],
        },
    },
}


def handle_request(message: Any, config: FTPConfig, connect: ConnectFn) -> dict | None:
    """Handle one decoded JSON-RPC message. Returns None for notifications
    (no reply) and for anything that must not produce output."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "request must be an object")
    method = message.get("method")
    msg_id = message.get("id")
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
        return _ok(
            msg_id,
            {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(
            msg_id,
            {
                "tools": [
                    {"name": name, **schema} for name, schema in TOOL_SCHEMAS.items()
                ]
            },
        )
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(tool_name, str) or tool_name not in TOOL_SCHEMAS:
            return _error(msg_id, INVALID_PARAMS, f"unknown tool: {tool_name!r}")
        if not isinstance(arguments, dict):
            return _error(msg_id, INVALID_PARAMS, "arguments must be an object")
        try:
            result = _run_tool(tool_name, arguments, config, connect)
        except (KeyError, ValueError) as exc:
            return _error(msg_id, INVALID_PARAMS, f"invalid tool call: {exc}")
        return _ok(msg_id, result)
    return _error(msg_id, METHOD_NOT_FOUND, f"unsupported method: {method}")


def serve_stdio(config: FTPConfig, connect: ConnectFn) -> None:
    """Newline-delimited JSON-RPC loop. stdout carries replies only;
    everything else (including tracebacks) goes to stderr."""
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps(_error(None, PARSE_ERROR, "invalid JSON")) + "\n")
            sys.stdout.flush()
            continue
        try:
            reply = handle_request(message, config, connect)
        except Exception as exc:  # noqa: BLE001 — framing must never die
            print(f"mcp bridge error: {type(exc).__name__}", file=sys.stderr)
            reply = _error(message.get("id") if isinstance(message, dict) else None, -32000, "internal error")
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def create_app(config: FTPConfig, connect: ConnectFn) -> FastAPI:
    """Streamable-HTTP transport: unary JSON-RPC POSTs at /mcp."""
    app = FastAPI(title="mainframe-ftp-bridge", version=SERVER_INFO["version"])

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> JSONResponse:
        try:
            message = await request.json()
        except ValueError:
            return JSONResponse(_error(None, PARSE_ERROR, "invalid JSON"), status_code=400)
        try:
            reply = handle_request(message, config, connect)
        except Exception:  # noqa: BLE001 — framing must never die
            reply = _error(
                message.get("id") if isinstance(message, dict) else None, -32000, "internal error"
            )
        if reply is None:
            return JSONResponse({}, status_code=202)
        return JSONResponse(reply)

    return app
