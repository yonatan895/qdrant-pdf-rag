"""Open WebUI Kind trial (issue #1073, #1088): a dummy tool server that records
what Open WebUI forwards to it. Trial-only, standard library only.

    python3 toolprobe_echo.py            # PORT (default 8000), EXCHANGE_CONFIG (optional)

It speaks just enough MCP over streamable HTTP (JSON responses, no session) at
POST /mcp and an OpenAPI tool at GET /openapi.json + POST /whoami. Every request
logs one JSON line to stdout with:

- the request header NAMES, and the values of the X-OpenWebUI-* headers only;
- the Authorization scheme, the decoded JWT header and claims of a bearer JWT,
  and a 12-hex sha256 fingerprint of the token (to match it against the token
  set Open WebUI holds, without logging the token).

The raw token, its signature segment and any other header value are never
logged. On each tool call it also tries the token exchanges listed in the
EXCHANGE_CONFIG JSON file (a run-time Secret): it posts the incoming token as
the subject token to the IdP and logs the HTTP status, the OAuth error code and
description, or the decoded header and claims of the issued token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
EXCHANGE_CONFIG = os.environ.get("EXCHANGE_CONFIG", "")
TOOL = {"name": "whoami", "description": "Trial echo: reports that the call arrived.",
        "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}}}
OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Trial echo", "version": "0.0.1"},
    "paths": {"/whoami": {"post": {
        "operationId": "whoami_openapi", "summary": "Trial echo: reports that the call arrived.",
        "requestBody": {"required": False, "content": {"application/json": {"schema": {
            "type": "object", "properties": {"note": {"type": "string"}}}}}},
        "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "object"}}}}},
    }}},
}


def emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _b64json(segment: str):
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()))


def decode_jwt(token: str) -> dict:
    """Header and claims of a JWS compact token; never the signature."""
    parts = token.split(".")
    out: dict = {"segments": len(parts), "fp": fingerprint(token)}
    if len(parts) != 3:
        return out
    try:
        out["header"] = _b64json(parts[0])
        out["claims"] = _b64json(parts[1])
    except (ValueError, UnicodeDecodeError):
        out["decode_error"] = True
    return out


def bearer(headers) -> str | None:
    value = headers.get("Authorization") or ""
    scheme, _, rest = value.partition(" ")
    return rest.strip() if scheme.lower() == "bearer" and rest.strip() else None


def exchanges(subject_token: str) -> list[dict]:
    if not EXCHANGE_CONFIG or not os.path.exists(EXCHANGE_CONFIG):
        return []
    with open(EXCHANGE_CONFIG, encoding="utf-8") as fh:
        attempts = json.load(fh)
    results = []
    for attempt in attempts:
        form = {"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": subject_token, **attempt.get("params", {})}
        req = urllib.request.Request(attempt["token_url"], data=urllib.parse.urlencode(form).encode(),
                                     method="POST")
        creds = f"{attempt['client_id']}:{attempt['client_secret']}".encode()
        req.add_header("Authorization", "Basic " + base64.b64encode(creds).decode())
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        result: dict = {"label": attempt["label"], "params": {k: v for k, v in attempt.get("params", {}).items()}}
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result["status"] = resp.status
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            result["status"] = exc.code
            try:
                body = json.loads(exc.read())
            except ValueError:
                body = {}
        except OSError as exc:
            result["status"] = None
            result["transport_error"] = type(exc).__name__
            body = {}
        if "error" in body:
            result["error"] = body.get("error")
            result["error_description"] = str(body.get("error_description", ""))[:200]
        issued = body.get("access_token")
        if issued:
            result["issued_token_type"] = body.get("issued_token_type")
            result["token_type"] = body.get("token_type")
            result["issued"] = decode_jwt(issued)
        results.append(result)
    return results


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def _record(self, extra: dict) -> str | None:
        token = bearer(self.headers)
        auth = self.headers.get("Authorization")
        record = {
            "event": "request", "method": self.command, "path": self.path.split("?")[0],
            "header_names": sorted({k.lower() for k in self.headers}),
            "forwarded": {k: v for k, v in self.headers.items() if k.lower().startswith("x-openwebui-")},
            "auth_scheme": auth.split(" ")[0] if auth else None,
            "jwt": decode_jwt(token) if token else None,
            **extra,
        }
        emit(record)
        return token

    def _send(self, status: int, body=None, content_type="application/json") -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def do_GET(self):
        if self.path.split("?")[0] == "/openapi.json":
            self._record({"kind": "openapi-spec"})
            self._send(200, OPENAPI)
            return
        if self.path == "/healthz":
            self._send(200, {"ok": True})
            return
        self._record({"kind": "get"})
        self._send(405, {"error": "method not allowed"})

    def do_DELETE(self):
        self._record({"kind": "delete"})
        self._send(405, {"error": "method not allowed"})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/whoami":
            token = self._record({"kind": "openapi-call"})
            if token:
                emit({"event": "exchange", "via": "openapi", "results": exchanges(token)})
            self._send(200, {"received_bearer": bool(token), "fp": fingerprint(token) if token else None})
            return
        if path != "/mcp" or not isinstance(body, dict):
            self._record({"kind": "unexpected"})
            self._send(404, {"error": "not found"})
            return
        method = body.get("method")
        token = self._record({"kind": "mcp", "rpc_method": method})
        if "id" not in body:
            self._send(202)
            return
        rid = body["id"]
        if method == "initialize":
            params = body.get("params") or {}
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "trial-echo", "version": "0.0.1"}}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            if token:
                emit({"event": "exchange", "via": "mcp", "results": exchanges(token)})
            text = json.dumps({"received_bearer": bool(token), "fp": fingerprint(token) if token else None})
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        elif method == "ping":
            result = {}
        else:
            self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})
            return
        self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result})


def main() -> int:
    emit({"event": "start", "port": PORT, "exchange_config": bool(EXCHANGE_CONFIG)})
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
