"""MCP framing for the FTP bridge: JSON-RPC dispatch, tool schemas, transports.

Supported methods: `initialize`, `notifications/initialized` (no reply),
`ping`, `tools/list`, `tools/call`. Anything else is -32601. Tool input
failures are -32602; tool *execution* failures ride the MCP `isError`
result shape (bridge.py), never protocol errors.

Transports: newline-delimited JSON-RPC on stdio (logs go to stderr only),
and unary JSON-RPC POSTs at `/mcp` over HTTP (plain JSON replies are
spec-legal for non-streaming tools). Blocking FTP I/O in HTTP mode is
offloaded via `asyncio.to_thread` (#1302) so slow FTP calls never stall
uvicorn's event loop.

Security controls (ADR-0012):
- `tools/list` and `tools/call` expose only `config.enabled_tools` (D7).
- Pre-connect policy validation rejects off-allowlist targets before any FTP
  connection is opened (D2).
- `LogonBreaker` bounds FTP logons under concurrency: at most `threshold`
  failing logons in flight, and after an FTP 530 trip no logon reaches the
  server until cooldown elapses or an operator resets it via
  `POST /admin/reset-logon-breaker` (D9). Reads are not serialized.
- Optional/required bearer-secret caller authentication on HTTP endpoints
  (except `/healthz`), matching the feedback service pattern (D4 point 2).

Tracing: the HTTP transport joins the caller's trace (W3C traceparent
extracted per request, `tools.call` span as its child); stdio carries no
headers, so stdio-served calls are trace-discontinuous by construction
(new roots, never joined). Tracing is default-off and fail-open exactly
like the agent: no endpoint means no-op spans, a dead collector means
log-and-drop, and telemetry never gates serving.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import sys
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from mainframe_rag import tracing as tracing_mod
from mainframe_rag.mcp import bridge
from mainframe_rag.mcp.bridge import FTPConfig, LogonBreaker

ConnectFn = Callable[[FTPConfig], Any]

# Proxy tracer: no-op until setup_tracing installs the real provider (the
# sidecar sets OTEL_EXPORTER_OTLP_ENDPOINT + OTEL_SERVICE_NAME).
tracer = trace.get_tracer("mainframe-ftp-bridge")

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


def _extract_tool_args(name: str, args: dict) -> dict[str, Any]:
    """Type-validate arguments before policy check or FTP connect."""
    if name == "dataset_read":
        dataset = _str_arg(args, "dataset", required=True)
        member = _str_arg(args, "member")
        if dataset is None or member is None:
            raise ValueError("dataset (required) and member (optional) must be strings")
        return {"dataset": dataset, "member": member or None}
    if name == "uss_read":
        path = _str_arg(args, "path", required=True)
        if path is None:
            raise ValueError("path (required) must be a string")
        return {"path": path}
    if name == "job_status":
        job_name = _str_arg(args, "job_name")
        owner = _str_arg(args, "owner")
        job_id = _str_arg(args, "job_id")
        if job_name is None or owner is None or job_id is None:
            raise ValueError("job_name, owner, and job_id must be strings when provided")
        return {
            "job_name": job_name or None,
            "owner": owner or None,
            "job_id": job_id or None,
        }
    if name == "jes_spool_read":
        job_id = _str_arg(args, "job_id", required=True)
        spool_id = _str_arg(args, "spool_id", required=True)
        if job_id is None or spool_id is None:
            raise ValueError("job_id and spool_id (required) must be strings")
        return {"job_id": job_id, "spool_id": spool_id}
    raise KeyError(name)


def _run_tool(
    name: str,
    args: dict,
    config: bridge.FTPConfig,
    connect: ConnectFn,
    breaker: LogonBreaker | None = None,
) -> dict:
    """Dispatch one tools/call. Unknown tools and malformed arg types raise
    KeyError/ValueError, which handle_request maps to -32602. Policy denials
    and connect/FTP failures return MCP isError tool results with stable codes."""
    if name not in config.enabled_tools:
        raise KeyError(name)
    parsed_args = _extract_tool_args(name, args)

    # Pre-connect policy check: off-allowlist targets never open FTP or log on.
    policy_error = bridge.validate_tool_policy(name, parsed_args, config)
    if policy_error is not None:
        return policy_error

    # Only session setup (connect + logon) holds a breaker slot; the read
    # below runs outside it, so concurrent reads stay concurrent (D9).
    try:
        if breaker is None:
            ftp = connect(config)
        else:
            with breaker.logon_slot(wait_s=config.timeout_s):
                ftp = connect(config)
    except Exception as exc:  # noqa: BLE001 — mapped to stable error codes
        code, message = bridge._classify_ftp_error(exc)
        return bridge._tool_error(code, message)

    session = bridge.FTPSession(ftp, config)
    try:
        if name == "dataset_read":
            return bridge.dataset_read(session, parsed_args["dataset"], parsed_args["member"])
        if name == "uss_read":
            return bridge.uss_read(session, parsed_args["path"])
        if name == "job_status":
            return bridge.job_status(
                session,
                job_name=parsed_args["job_name"],
                owner=parsed_args["owner"],
                job_id=parsed_args["job_id"],
            )
        if name == "jes_spool_read":
            return bridge.jes_spool_read(session, parsed_args["job_id"], parsed_args["spool_id"])
    finally:
        with contextlib.suppress(Exception):  # best-effort close only
            session.ftp.quit()
    raise KeyError(name)


# Closed four-tool registry (ADR-0003); active tools are filtered by
# `config.enabled_tools` (ADR-0012 D7).
TOOL_SCHEMAS: dict[str, dict] = {
    "dataset_read": {
        "description": "Read an allowlisted z/OS sequential dataset or PDS member "
        "(TYPE A, EBCDIC converted; exact allowlist only, no PDS member listing).",
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
        "description": "Read a z/OS UNIX file (unapproved for ADR-0012 pilot).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute USS path"}},
            "required": ["path"],
        },
    },
    "job_status": {
        "description": "Query one exact batch job (exact job_id or exact job_name + owner) "
        "within the configured JES prefix allowlist.",
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
        "description": "Read one DD-scoped spool file (JESMSGLG or JESYSMSG only) of an "
        "allowlisted job (JOBID.n), verified via LIST before RETR.",
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


def _result_bytes(result: dict) -> int:
    """Count text bytes across result content (ids/counts on spans, never
    dataset/spool text)."""
    total = 0
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                total += len(item["text"].encode("utf-8", errors="replace"))
    return total


def handle_request(
    message: Any,
    config: FTPConfig,
    connect: ConnectFn,
    parent_context: Any | None = None,
    breaker: LogonBreaker | None = None,
) -> dict | None:
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
                    {"name": name, **TOOL_SCHEMAS[name]}
                    for name in config.enabled_tools
                    if name in TOOL_SCHEMAS
                ]
            },
        )
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(tool_name, str) or tool_name not in config.enabled_tools:
            return _error(msg_id, INVALID_PARAMS, "unknown or disabled tool")
        if not isinstance(arguments, dict):
            return _error(msg_id, INVALID_PARAMS, "arguments must be an object")
        # Access log: tool name only (never args, content, or credentials).
        # Tests and operators grep this to prove what the bridge served.
        print(f"mcp tools/call name={tool_name}", file=sys.stderr, flush=True)
        t0 = time.monotonic()
        with tracer.start_as_current_span(
            "tools.call",
            context=parent_context,
            attributes={"mcp.tool": tool_name},
        ) as span:
            try:
                result = _run_tool(tool_name, arguments, config, connect, breaker=breaker)
            except (KeyError, ValueError) as exc:
                return _error(msg_id, INVALID_PARAMS, f"invalid tool call: {exc}")
            span.set_attributes(
                {
                    "mcp.bytes_out": _result_bytes(result),
                    "mcp.elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "mcp.is_error": bool(result.get("isError")),
                }
            )
        return _ok(msg_id, result)
    return _error(msg_id, METHOD_NOT_FOUND, f"unsupported method: {method}")


def serve_stdio(
    config: FTPConfig,
    connect: ConnectFn,
    *,
    breaker: LogonBreaker | None = None,
) -> None:
    """Newline-delimited JSON-RPC loop. stdout carries replies only;
    everything else (including tracebacks) goes to stderr."""
    active_breaker = breaker or LogonBreaker(
        threshold=config.logon_breaker_threshold,
        cooldown_s=config.logon_breaker_cooldown_s,
    )
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
            reply = handle_request(message, config, connect, breaker=active_breaker)
        except Exception as exc:  # noqa: BLE001 — framing must never die
            print(f"mcp bridge error: {type(exc).__name__}", file=sys.stderr)
            reply = _error(
                message.get("id") if isinstance(message, dict) else None, -32000, "internal error"
            )
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def sample_ratio_from_env() -> float:
    """OTEL_SAMPLE_RATIO clamped to [0, 1]; garbage means keep-all (1.0)."""
    try:
        return max(0.0, min(1.0, float(os.environ.get("OTEL_SAMPLE_RATIO", "1.0"))))
    except ValueError:
        return 1.0


def _expected_bearer(token: str) -> bytes:
    return f"Bearer {token.strip()}".encode()


def _valid_bearer(supplied: str, expected: bytes) -> bool:
    if not supplied.startswith("Bearer "):
        return False
    token = supplied[len("Bearer ") :].strip()
    if not token:
        return False
    return hmac.compare_digest(f"Bearer {token}".encode(), expected)


def create_app(
    config: FTPConfig,
    connect: ConnectFn,
    *,
    auth_token: str | None = None,
    breaker: LogonBreaker | None = None,
) -> FastAPI:
    """Streamable-HTTP transport: unary JSON-RPC POSTs at /mcp.

    - Offloads synchronous `handle_request` (blocking ftplib I/O) to a worker
      thread via `asyncio.to_thread` (#1302).
    - Enforces constant-time bearer caller authentication when `auth_token` is
      configured (ADR-0012 D4 point 2), keeping `GET /healthz` open for probes.
    - Exposes `POST /admin/reset-logon-breaker` (bearer-protected) for D9.
    - Lifespan owns the tracer; W3C parents are extracted per request.
    """
    expected_auth: bytes | None = None
    if auth_token is not None:
        if not auth_token.strip():
            raise RuntimeError("MCP_BRIDGE_AUTH_TOKEN must not be empty")
        expected_auth = _expected_bearer(auth_token)

    active_breaker = breaker or LogonBreaker(
        threshold=config.logon_breaker_threshold,
        cooldown_s=config.logon_breaker_cooldown_s,
    )
    app = FastAPI(
        title="mainframe-ftp-bridge",
        version=SERVER_INFO["version"],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tracing_mod.setup_tracing(
            os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            sample_ratio=sample_ratio_from_env(),
        )
        yield
        tracing_mod.shutdown_tracing()

    app.router.lifespan_context = lifespan

    if expected_auth is not None:
        expected_bytes = expected_auth

        @app.middleware("http")
        async def authorize(request: Request, call_next):  # type: ignore[no-untyped-def]
            if request.url.path == "/healthz" and request.method == "GET":
                return await call_next(request)
            supplied = request.headers.get("authorization", "")
            if not _valid_bearer(supplied, expected_bytes):
                return JSONResponse(
                    {"code": "unauthorized", "message": "a valid bridge token is required"},
                    status_code=401,
                )
            return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "logon_breaker_open": active_breaker.is_open()}

    @app.post("/admin/reset-logon-breaker")
    async def reset_logon_breaker() -> dict[str, str]:
        active_breaker.reset()
        return {"status": "reset"}

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> JSONResponse:
        try:
            message = await request.json()
        except ValueError:
            return JSONResponse(_error(None, PARSE_ERROR, "invalid JSON"), status_code=400)
        parent_ctx = tracing_mod.parent_context(request.headers)
        try:
            reply = await asyncio.to_thread(
                handle_request,
                message,
                config,
                connect,
                parent_context=parent_ctx,
                breaker=active_breaker,
            )
        except Exception:  # noqa: BLE001 — framing must never die
            reply = _error(
                message.get("id") if isinstance(message, dict) else None, -32000, "internal error"
            )
        if reply is None:
            return JSONResponse({}, status_code=202)
        return JSONResponse(reply)

    return app
