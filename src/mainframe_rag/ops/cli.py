"""Read-only operations CLI for the deployed agent API (issue #172, ADR-0003).

A separate HTTP consumer of exactly three routes: ``GET /healthz``,
``POST /v1/search`` and ``POST /v1/answer``. It never calls the Zowe/MCP
bridge, exposes no live-state tool, adds no config-disclosure endpoint, reads
no Qdrant, and never falls back to a local embedder or model. Search never
calls an LLM; the CLI is only a client of that contract.

Contract summary (owner: docs/agent.md, "Operations CLI"):

* Base URL is explicit (``--base-url`` or ``MAINFRAME_RAG_URL``); URLs with
  userinfo, query or fragment are refused. TLS verification cannot be turned
  off: ``--ca-file`` supplies a complete trust bundle, otherwise the HTTP
  client's default applies (including ``SSL_CERT_FILE``).
* The optional bearer key comes from ``MAINFRAME_RAG_API_KEY`` or
  ``--api-key-file`` only (never an argument, so it never reaches shell
  history), is sent only over https or to a loopback host, and is never
  printed.
* Errors carry fixed codes and fixed messages; server/upstream text, request
  text and exception text are never echoed. No retries; each request has a
  timeout and a response-size cap.
* An answer is reported as successful (exit 0) only when the server's
  ``verification_state`` is ``accepted`` with a non-blank answer. Streamed
  answers (``--stream``) print nothing unless the terminal ``final`` event
  arrives: EOF, an ``error`` event, a malformed frame or Ctrl-C never print a
  completed answer.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import ssl
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2

from mainframe_rag.config import bearer_auth_headers

ENV_URL = "MAINFRAME_RAG_URL"
ENV_API_KEY = "MAINFRAME_RAG_API_KEY"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_ACCEPTED = 3
EXIT_UNAUTHORIZED = 4
EXIT_UNAVAILABLE = 5
EXIT_BAD_RESPONSE = 6
EXIT_REJECTED = 7
EXIT_TLS = 8
EXIT_CANCELLED = 130

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_KEY_CHARS = 4096
CONNECT_TIMEOUT_CAP_S = 10.0
DEFAULT_TIMEOUTS_S = {"health": 10.0, "search": 60.0, "answer": 180.0}
MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 600.0

VERIFICATION_STATES = frozenset(
    {"accepted", "insufficient_evidence", "unverified_draft", "generation_incomplete"}
)
# Server error codes (docs/agent.md section 2) that may be echoed verbatim.
# Anything else is dropped: the CLI's own fixed code carries the outcome.
KNOWN_SERVER_CODES = frozenset(
    {
        "upstream_error",
        "internal",
        "not_configured",
        "qdrant_unready",
        "representation_unavailable",
        "invalid_request",
        "prompt_budget_exceeded",
        "metrics_unavailable",
        "not_found",
        "method_not_allowed",
        "http_error",
    }
)

# code -> (exit code, fixed message)
ERRORS: dict[str, tuple[int, str]] = {
    "usage": (EXIT_USAGE, "invalid command-line usage or configuration"),
    "answer_not_accepted": (EXIT_NOT_ACCEPTED, "the answer is not a verified accepted answer"),
    "unauthorized": (EXIT_UNAUTHORIZED, "the service rejected the credentials"),
    "not_ready": (EXIT_UNAVAILABLE, "the service is not ready"),
    "unavailable": (EXIT_UNAVAILABLE, "the service is unavailable"),
    "timeout": (EXIT_UNAVAILABLE, "the request timed out"),
    "server_error": (EXIT_UNAVAILABLE, "the service reported an error"),
    "malformed_response": (EXIT_BAD_RESPONSE, "the response was malformed or truncated"),
    "unexpected_response": (EXIT_BAD_RESPONSE, "the response status was unexpected"),
    "empty_answer": (EXIT_BAD_RESPONSE, "the service returned an accepted answer with no text"),
    "stream_incomplete": (EXIT_BAD_RESPONSE, "the answer stream ended without a final event"),
    "stream_failed": (EXIT_BAD_RESPONSE, "the answer stream reported a failure"),
    "request_rejected": (EXIT_REJECTED, "the service rejected the request"),
    "tls_error": (EXIT_TLS, "TLS verification of the service failed"),
    "cancelled": (EXIT_CANCELLED, "the request was cancelled"),
}


class CliError(Exception):
    """Fixed-shape failure. Never carries upstream, request or exception text."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        server_code: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.server_code = server_code if server_code in KNOWN_SERVER_CODES else None
        self.data = data

    @property
    def exit_code(self) -> int:
        return ERRORS[self.code][0]

    @property
    def message(self) -> str:
        return ERRORS[self.code][1]


@dataclass(frozen=True)
class Target:
    base_url: str
    headers: dict[str, str]
    verify: ssl.SSLContext | bool
    timeout_s: float


# ---------------------------------------------------------------- configuration


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_base_url(raw: str) -> str:
    try:
        parts = urlsplit(raw.strip())
        _ = parts.port  # raises on a malformed port
    except ValueError:
        raise CliError("usage") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CliError("usage")
    if parts.username is not None or parts.password is not None:
        raise CliError("usage")
    if parts.query or parts.fragment:
        raise CliError("usage")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def load_api_key(key_file: str | None, environ: dict[str, str]) -> str | None:
    if key_file is not None:
        try:
            with open(key_file, encoding="ascii") as handle:
                raw = handle.read(MAX_KEY_CHARS + 2)
        except OSError, UnicodeDecodeError:
            raise CliError("usage") from None
    else:
        raw = environ.get(ENV_API_KEY, "")
    key = raw.strip()
    if not key:
        return None
    if len(key) > MAX_KEY_CHARS or not re.fullmatch(r"[\x21-\x7e]+", key):
        raise CliError("usage")
    return key


def build_target(args: argparse.Namespace, environ: dict[str, str], command: str) -> Target:
    raw_url = args.base_url or environ.get(ENV_URL, "")
    if not raw_url.strip():
        raise CliError("usage")
    base_url = normalize_base_url(raw_url)
    key = load_api_key(args.api_key_file, environ)
    parts = urlsplit(base_url)
    if key is not None and parts.scheme != "https" and not _is_loopback(parts.hostname or ""):
        raise CliError("usage")  # never send a credential over cleartext
    timeout_s = args.timeout if args.timeout is not None else DEFAULT_TIMEOUTS_S[command]
    if not MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S:
        raise CliError("usage")
    verify: ssl.SSLContext | bool = True
    if args.ca_file:
        try:
            verify = ssl.create_default_context(cafile=args.ca_file)
        except OSError, ssl.SSLError:
            raise CliError("usage") from None
    return Target(
        base_url=base_url,
        headers=bearer_auth_headers(key),
        verify=verify,
        timeout_s=timeout_s,
    )


# ------------------------------------------------------------------- transport


def _chain_has_ssl_error(exc: BaseException | None) -> bool:
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, ssl.SSLError):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _status_error(status: int, body: Any) -> CliError:
    server_code = body.get("code") if isinstance(body, dict) else None
    server_code = server_code if isinstance(server_code, str) else None
    if status in (401, 403):
        return CliError("unauthorized", status=status, server_code=server_code)
    if status == 503:
        return CliError("unavailable", status=status, server_code=server_code)
    if status >= 500:
        return CliError("server_error", status=status, server_code=server_code)
    if 400 <= status < 500:
        return CliError("request_rejected", status=status, server_code=server_code)
    return CliError("unexpected_response", status=status)


def _bounded_chunks(response: httpx2.Response, deadline: float, limit: int) -> Iterator[bytes]:
    total = 0
    for chunk in response.iter_bytes():
        if time.monotonic() > deadline:
            raise CliError("timeout")
        total += len(chunk)
        if total > limit:
            raise CliError("malformed_response")
        yield chunk


def _read_body(response: httpx2.Response, deadline: float) -> bytes:
    return b"".join(_bounded_chunks(response, deadline, MAX_BODY_BYTES))


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, ValueError, RecursionError:
        raise CliError("malformed_response") from None


class _Session:
    """One request over a real httpx2 client with fixed failure mapping."""

    def __init__(self, target: Target, *, transport: httpx2.BaseTransport | None = None) -> None:
        self.target = target
        try:
            self.client = httpx2.Client(
                verify=target.verify,
                timeout=httpx2.Timeout(
                    target.timeout_s, connect=min(target.timeout_s, CONNECT_TIMEOUT_CAP_S)
                ),
                follow_redirects=False,
                transport=transport,
                headers={"User-Agent": "mainframe-rag-ops/1"},
            )
        except OSError, ssl.SSLError:
            raise CliError("usage") from None

    def close(self) -> None:
        self.client.close()

    def json_call(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        """Return (status, parsed JSON) for 200 and for server error envelopes."""
        deadline = time.monotonic() + self.target.timeout_s
        try:
            with self.client.stream(
                method,
                self.target.base_url + path,
                json=body,
                headers={**self.target.headers, "Accept": "application/json"},
            ) as response:
                raw = _read_body(response, deadline)
                status = response.status_code
        except CliError:
            raise
        except httpx2.TimeoutException:
            raise CliError("timeout") from None
        except httpx2.ProtocolError:
            raise CliError("malformed_response") from None
        except (httpx2.TransportError, OSError) as exc:
            raise self._transport_error(exc) from None
        parsed = None
        if raw:
            try:
                parsed = _parse_json(raw)
            except CliError:
                if status == 200:
                    raise
        if status != 200:
            if (
                status == 503
                and path == "/healthz"
                and isinstance(parsed, dict)
                and "status" in parsed  # a HealthzResponse, not an error envelope
            ):
                return status, parsed
            raise _status_error(status, parsed)
        return status, parsed

    def sse_call(self, path: str, body: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield (event, data) frames. Raises CliError on any non-clean path."""
        deadline = time.monotonic() + self.target.timeout_s
        try:
            with self.client.stream(
                "POST",
                self.target.base_url + path,
                json=body,
                headers={**self.target.headers, "Accept": "text/event-stream"},
            ) as response:
                if response.status_code != 200:
                    raw = _read_body(response, deadline)
                    parsed = None
                    try:
                        parsed = _parse_json(raw) if raw else None
                    except CliError:
                        parsed = None
                    raise _status_error(response.status_code, parsed)
                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype != "text/event-stream":
                    raise CliError("malformed_response")
                yield from _frames(_bounded_chunks(response, deadline, MAX_BODY_BYTES))
        except CliError:
            raise
        except httpx2.TimeoutException:
            raise CliError("timeout") from None
        except httpx2.ProtocolError:
            raise CliError("stream_incomplete") from None
        except (httpx2.TransportError, OSError) as exc:
            raise self._transport_error(exc) from None

    @staticmethod
    def _transport_error(exc: BaseException) -> CliError:
        if _chain_has_ssl_error(exc):
            return CliError("tls_error")
        return CliError("unavailable")


def _frames(chunks: Iterator[bytes]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse SSE frames from byte chunks (event/data/comment lines, blank-line
    terminated). A trailing unterminated frame is discarded: EOF mid-frame is
    a truncated stream, never a frame."""
    buffer = b""
    event = "message"
    data: list[str] = []
    for chunk in chunks:
        buffer += chunk
        if len(buffer) > MAX_FRAME_BYTES:
            raise CliError("malformed_response")
        while (nl := buffer.find(b"\n")) != -1:
            line, buffer = buffer[:nl], buffer[nl + 1 :]
            try:
                text = line.rstrip(b"\r").decode("utf-8")
            except UnicodeDecodeError:
                raise CliError("malformed_response") from None
            if text == "":
                if data:
                    payload = _parse_json("\n".join(data).encode("utf-8"))
                    if not isinstance(payload, dict):
                        raise CliError("malformed_response")
                    yield event, payload
                event, data = "message", []
            elif text.startswith(":"):
                continue
            else:
                name, _, value = text.partition(":")
                value = value.removeprefix(" ")
                if name == "event":
                    event = value
                elif name == "data":
                    data.append(value)


# ----------------------------------------------------------------- validation


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


_HIT_STR_FIELDS = (
    "chunk_id",
    "cite",
    "heading",
    "text",
    "doc_id",
    "title",
    "page_label",
    "chunk_type",
)


def validate_hit(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) for k in _HIT_STR_FIELDS):
        raise CliError("malformed_response")
    message_ids = raw.get("message_ids")
    if not _str_list(message_ids) or not _is_number(raw.get("score")):
        raise CliError("malformed_response")
    hit: dict[str, Any] = {k: raw[k] for k in _HIT_STR_FIELDS}
    hit["message_ids"] = message_ids
    hit["score"] = raw["score"]
    for key in ("product", "version"):
        value = raw.get(key)
        hit[key] = value if isinstance(value, str) else None
    rerank = raw.get("rerank_score")
    hit["rerank_score"] = rerank if _is_number(rerank) else None
    for key in ("page_start", "page_end"):
        value = raw.get(key)
        hit[key] = (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )
    return hit


def validate_health(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("status"), str):
        raise CliError("malformed_response")
    if not isinstance(raw.get("qdrant"), bool):
        raise CliError("malformed_response")
    out: dict[str, Any] = {"status": raw["status"], "qdrant": raw["qdrant"]}
    for key in ("embed", "rerank"):
        value = raw.get(key)
        if value is not None and not isinstance(value, bool):
            raise CliError("malformed_response")
        out[key] = value
    representation = raw.get("representation")
    if representation is not None and not isinstance(representation, str):
        raise CliError("malformed_response")
    out["representation"] = representation
    return out


def validate_search(raw: Any) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("request_id"), str)
        or not isinstance(raw.get("query_kind"), str)
        or not isinstance(raw.get("hits"), list)
    ):
        raise CliError("malformed_response")
    return {
        "request_id": raw["request_id"],
        "query_kind": raw["query_kind"],
        "hits": [validate_hit(h) for h in raw["hits"]],
    }


def validate_answer(raw: Any, *, final: bool) -> dict[str, Any]:
    """Shared by the JSON response and the SSE ``final`` event; ``final`` adds
    the stream-only fields (finish_reason, query_kind, hits, ttft_ms, usage)."""
    if not isinstance(raw, dict):
        raise CliError("malformed_response")
    inferred_indices = raw.get("inferred_indices")
    ok = (
        isinstance(raw.get("request_id"), str)
        and isinstance(raw.get("answer"), str)
        and _str_list(raw.get("citations"))
        and isinstance(raw.get("citations_inferred"), bool)
        and isinstance(inferred_indices, list)
        and all(_is_int(i) for i in inferred_indices)
        and (raw.get("script") is None or isinstance(raw.get("script"), str))
        and (raw.get("script_lang") is None or isinstance(raw.get("script_lang"), str))
        and raw.get("verification_state") in VERIFICATION_STATES
        and isinstance(raw.get("script_review_required"), bool)
    )
    if not ok:
        raise CliError("malformed_response")
    out: dict[str, Any] = {
        k: raw[k]
        for k in (
            "request_id",
            "answer",
            "citations",
            "citations_inferred",
            "inferred_indices",
            "script",
            "script_lang",
            "verification_state",
            "script_review_required",
        )
    }
    if final:
        finish = raw.get("finish_reason")
        if not isinstance(finish, str) or not isinstance(raw.get("query_kind"), str):
            raise CliError("malformed_response")
        if not isinstance(raw.get("hits"), list):
            raise CliError("malformed_response")
        out["finish_reason"] = finish
        out["query_kind"] = raw["query_kind"]
        out["hits"] = [validate_hit(h) for h in raw["hits"]]
        ttft = raw.get("ttft_ms")
        out["ttft_ms"] = ttft if _is_int(ttft) else None
        usage = raw.get("usage")
        out["usage"] = (
            {k: v for k, v in usage.items() if isinstance(k, str) and _is_int(v)}
            if isinstance(usage, dict)
            else None
        )
    return out


# -------------------------------------------------------------------- commands


def _filters(args: argparse.Namespace) -> dict[str, Any]:
    body: dict[str, Any] = {"query": args.query}
    if args.product:
        body["product"] = args.product
    if args.version:
        body["version"] = args.version
    return body


def run_health(session: _Session, args: argparse.Namespace) -> dict[str, Any]:
    status, raw = session.json_call("GET", "/healthz", None)
    data = validate_health(raw)
    if status != 200 or data["status"] != "ok":
        raise CliError("not_ready", status=status, data=data)
    return data


def run_search(session: _Session, args: argparse.Namespace) -> dict[str, Any]:
    body = _filters(args)
    body["limit"] = args.limit
    _, raw = session.json_call("POST", "/v1/search", body)
    return validate_search(raw)


def _final_event(session: _Session, body: dict[str, Any]) -> dict[str, Any]:
    final: dict[str, Any] | None = None
    for event, payload in session.sse_call("/v1/answer", {**body, "stream": True}):
        if final is not None:
            raise CliError("malformed_response")  # nothing may follow the terminal frame
        if event == "token":
            continue  # provisional; never shown
        if event == "error":
            raise CliError("stream_failed", data={"verification_state": "generation_incomplete"})
        if event == "final":
            final = validate_answer(payload, final=True)
            continue
        raise CliError("malformed_response")
    if final is None:
        raise CliError("stream_incomplete", data={"verification_state": "generation_incomplete"})
    return final


def run_answer(session: _Session, args: argparse.Namespace) -> dict[str, Any]:
    body = _filters(args)
    if args.temperature is not None:
        body["temperature"] = args.temperature
    if args.stream:
        data = _final_event(session, body)
    else:
        _, raw = session.json_call("POST", "/v1/answer", body)
        data = validate_answer(raw, final=False)
    state = data["verification_state"]
    if state == "accepted" and not data["answer"].strip():
        raise CliError("empty_answer")
    if state != "accepted" or data.get("finish_reason", "stop") != "stop":
        raise CliError("answer_not_accepted", data=data)
    return data


COMMANDS = {"health": run_health, "search": run_search, "answer": run_answer}


# ---------------------------------------------------------------------- output

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _safe(text: str) -> str:
    """Neutralize terminal control sequences in server-supplied text."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def _render_hit(index: int, hit: dict[str, Any]) -> list[str]:
    lines = [f"#{index} [{hit['score']:.4f}] {_safe(hit['cite'])}"]
    meta = [
        f"doc={_safe(hit['doc_id'])}",
        f"type={hit['chunk_type']}",
        f"page={_safe(hit['page_label'])}",
    ]
    if hit["page_start"] is not None:
        end = hit["page_end"] if hit["page_end"] is not None else hit["page_start"]
        meta.append(f"pdf_pages={hit['page_start'] + 1}-{end + 1}")
    if hit["product"] or hit["version"]:
        meta.append(
            f"product={_safe(hit['product'] or 'unknown')} {_safe(hit['version'] or '')}".rstrip()
        )
    if hit["message_ids"]:
        meta.append("messages=" + ",".join(_safe(m) for m in hit["message_ids"]))
    lines.append("   " + " | ".join(meta))
    lines.extend("   | " + _safe(t) for t in hit["text"].strip().splitlines())
    return lines


def render_text(command: str, data: dict[str, Any]) -> str:
    lines: list[str] = []
    if command == "health":
        for key in ("status", "qdrant", "embed", "representation", "rerank"):
            value = data[key]
            lines.append(
                f"{key}: {'null' if value is None else str(value).lower() if isinstance(value, bool) else _safe(str(value))}"
            )
    elif command == "search":
        lines.append(f"request_id: {_safe(data['request_id'])}")
        lines.append(f"query_kind: {_safe(data['query_kind'])}")
        lines.append(f"hits: {len(data['hits'])}")
        for i, hit in enumerate(data["hits"], 1):
            lines.extend(_render_hit(i, hit))
    else:
        lines.append(f"request_id: {_safe(data['request_id'])}")
        lines.append(f"verification_state: {data['verification_state']}")
        if "finish_reason" in data:
            lines.append(f"finish_reason: {_safe(data['finish_reason'])}")
        if data["citations_inferred"]:
            lines.append("citations_inferred: true (mapped from bare [n] markers; not grounding)")
        if data["script_review_required"]:
            lines.append("script_review_required: true (scripts are unvalidated drafts)")
        lines.append("")
        lines.append(_safe(data["answer"]))
        if data["citations"]:
            lines.append("")
            lines.append("Citations:")
            lines.extend(f"  - {_safe(c)}" for c in data["citations"])
        if data["script"]:
            lines.append("")
            lang = _safe(data["script_lang"] or "unknown")
            lines.append(f"Script ({lang}) - REVIEW REQUIRED, NOT VALIDATED:")
            lines.append(_safe(data["script"]))
    return "\n".join(lines) + "\n"


def emit_success(command: str, data: dict[str, Any], fmt: str, out: Any) -> None:
    if fmt == "json":
        out.write(json.dumps({"ok": True, "command": command, "exit_code": 0, "data": data}) + "\n")
    else:
        out.write(render_text(command, data))


def emit_error(command: str, err: CliError, fmt: str, out: Any, errout: Any) -> None:
    if fmt == "json":
        error: dict[str, Any] = {"code": err.code, "message": err.message}
        if err.status is not None:
            error["status"] = err.status
        if err.server_code is not None:
            error["server_code"] = err.server_code
        envelope: dict[str, Any] = {
            "ok": False,
            "command": command,
            "exit_code": err.exit_code,
            "error": error,
        }
        if err.data is not None:
            envelope["data"] = err.data
        out.write(json.dumps(envelope) + "\n")
        return
    suffix = f" (server code: {err.server_code})" if err.server_code else ""
    status = f" [HTTP {err.status}]" if err.status is not None else ""
    errout.write(f"error: {err.code}: {err.message}{status}{suffix}\n")
    if err.data is not None and command == "health":
        out.write(render_text("health", err.data))
    elif err.data is not None and "answer" in err.data:
        out.write(render_text("answer", err.data))
    elif err.data is not None:
        state = err.data.get("verification_state")
        if state:
            errout.write(f"verification_state: {state}\n")


# ------------------------------------------------------------------------ main


def _query_text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("query must not be blank")
    return value


def _limit(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer") from None
    if not 1 <= n <= 40:
        raise argparse.ArgumentTypeError("limit must be 1-40")
    return n


def _temperature(value: str) -> float:
    try:
        t = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("temperature must be a number") from None
    if not 0.0 <= t <= 2.0:  # NaN fails both comparisons
        raise argparse.ArgumentTypeError("temperature must be 0.0-2.0")
    return t


def _timeout(value: str) -> float:
    try:
        t = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a number") from None
    if not MIN_TIMEOUT_S <= t <= MAX_TIMEOUT_S:
        raise argparse.ArgumentTypeError(f"timeout must be {MIN_TIMEOUT_S:g}-{MAX_TIMEOUT_S:g}")
    return t


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # fixed text: never echo argument values
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: invalid arguments ({message.split(':')[0]})\n")
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    common.add_argument("--base-url", help=f"Agent base URL (or ${ENV_URL}); required, no default.")
    common.add_argument(
        "--api-key-file",
        help=f"File holding a bearer key (or ${ENV_API_KEY}); never passed as an argument.",
    )
    common.add_argument(
        "--ca-file",
        help="PEM trust bundle (complete; replaces default roots). TLS verification "
        "cannot be disabled. Without it the client default applies, incl. SSL_CERT_FILE.",
    )
    common.add_argument(
        "--timeout",
        type=_timeout,
        help="Per-request timeout in seconds, 1-600 (default: health 10, search 60, answer 180).",
    )
    common.add_argument(
        "--format",
        choices=("json", "text"),
        default="text",
        help="json: one machine-readable object on stdout; text: human-readable.",
    )

    parser = _Parser(
        prog="mainframe-rag-ops",
        description="Read-only operations client for the Mainframe RAG agent HTTP API.",
        epilog="Exit codes: 0 ok; 2 usage/config; 3 answer not verified; 4 unauthorized; "
        "5 unavailable/not ready/timeout; 6 malformed or incomplete response; "
        "7 request rejected; 8 TLS verification failed; 130 cancelled.",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sub.add_parser("health", parents=[common], help="GET /healthz readiness.")

    for name, help_text in (
        ("search", "POST /v1/search (retrieval only; no LLM)."),
        ("answer", "POST /v1/answer (cited answer; verification state decides the exit)."),
    ):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("query", type=_query_text)
        p.add_argument("--product")
        p.add_argument("--version")
        if name == "search":
            p.add_argument("--limit", type=_limit, default=8, help="Hits, 1-40 (default 8).")
        else:
            p.add_argument("--temperature", type=_temperature)
            p.add_argument(
                "--stream",
                action="store_true",
                help="Use the SSE route: prints only a complete terminal final event.",
            )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    stdout: Any = None,
    stderr: Any = None,
    transport: httpx2.BaseTransport | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    errout = stderr if stderr is not None else sys.stderr
    env = dict(os.environ) if environ is None else environ
    args = build_parser().parse_args(argv)
    command: str = args.command
    fmt: str = args.format
    session: _Session | None = None
    try:
        target = build_target(args, env, command)
        session = _Session(target, transport=transport)
        data = COMMANDS[command](session, args)
    except CliError as err:
        emit_error(command, err, fmt, out, errout)
        return err.exit_code
    except KeyboardInterrupt:
        emit_error(command, CliError("cancelled"), fmt, out, errout)
        return EXIT_CANCELLED
    finally:
        if session is not None:
            session.close()
    emit_success(command, data, fmt, out)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
