"""Operations CLI (issue #172): `mainframe-rag-ops health|search|answer`.

Hermetic: the real httpx2 client talks to a loopback stdlib HTTP(S) server that
replays scripted responses (healthy, empty, scoped, unauthorized, unavailable,
malformed, truncated, stalled, cancelled). Payloads are built from the agent's
own response models/SSE builders so the CLI is pinned to the server contract,
not to a hand-written lookalike. After every failure class a healthy request
must still succeed against the same server (next ordinary operation).
"""

from __future__ import annotations

import io
import json
import shutil
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from mainframe_rag.agent import sse
from mainframe_rag.agent.app import AnswerResponse, HealthzResponse, SearchResponse
from mainframe_rag.ops import cli
from mainframe_rag.ports import TokenUsage
from mainframe_rag.retrieve.query import SearchHit

KEY = "sk-test-ops-secret-0123456789"
REPO = Path(__file__).resolve().parents[1]


def hit(n: int = 1, **kw: Any) -> SearchHit:
    base: dict[str, Any] = {
        "chunk_id": f"c{n}",
        "score": 0.5,
        "cite": f"SA22-7601 p.{n}",
        "heading": "IEA500I",
        "text": "IEA500I COMMAND REJECTED\nsecond line",
        "doc_id": "SA22-7601",
        "title": "Messages",
        "page_label": str(n),
        "chunk_type": "message",
        "message_ids": ("IEA500I",),
        "product": "z/OS",
        "version": "3.1",
        "page_start": n - 1,
        "page_end": n,
    }
    base.update(kw)
    return SearchHit(**base)


def answer_json(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "request_id": "abc123abc123",
        "answer": "Use the console.",
        "citations": ["SA22-7601 p.1"],
        "citations_inferred": False,
        "inferred_indices": [],
        "script": None,
        "verification_state": "accepted",
    }
    base.update(kw)
    return AnswerResponse(**base).model_dump()


def final_event(**kw: Any) -> str:
    args: dict[str, Any] = {
        "request_id": "abc123abc123",
        "answer": "Use the console.",
        "citations": ["SA22-7601 p.1"],
        "citations_inferred": False,
        "script": None,
        "query_kind": "message",
        "hits": [hit()],
        "finish_reason": "stop",
        "ttft_ms": 12,
        "usage": TokenUsage(
            prompt_tokens=1, completion_tokens=2, reasoning_tokens=0, total_tokens=3
        ),
        "verification_state": "accepted",
    }
    args.update(kw)
    return sse.format_sse_event("final", sse.final_payload(**args))


def token_event(text: str = "Use") -> str:
    return sse.format_sse_event("token", {"type": "token", "delta": text})


@dataclass
class Reply:
    status: int = 200
    body: bytes | str | dict = b""
    ctype: str = "application/json"
    chunks: list[bytes] | None = None  # sent as-is, then connection closes
    declared_length: int | None = None  # lie about Content-Length (truncation)
    delay_s: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


class FakeAgent:
    def __init__(self, tls: ssl.SSLContext | None = None) -> None:
        self.routes: dict[tuple[str, str], list[Reply]] = {}
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "json": json.loads(raw) if raw else None,
                    }
                )
                queue = owner.routes.get((self.command, self.path.split("?")[0]))
                reply = (queue.pop(0) if len(queue) > 1 else queue[0]) if queue else Reply(404)
                if reply.delay_s:
                    time.sleep(reply.delay_s)
                body = reply.body
                if isinstance(body, dict):
                    body = json.dumps(body)
                if isinstance(body, str):
                    body = body.encode()
                self.send_response(reply.status)
                self.send_header("Content-Type", reply.ctype)
                for k, v in reply.headers.items():
                    self.send_header(k, v)
                if reply.chunks is not None:
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in reply.chunks:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    self.close_connection = True
                    return
                self.send_header("Content-Length", str(reply.declared_length or len(body)))
                if reply.declared_length:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                if reply.declared_length:
                    self.close_connection = True

            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if tls is not None:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.scheme = "https" if tls is not None else "http"
        self.url = f"{self.scheme}://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def on(self, method: str, path: str, *replies: Reply) -> None:
        self.routes[(method, path)] = list(replies)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def agent():
    srv = FakeAgent()
    yield srv
    srv.close()


def run(argv: list[str], env: dict[str, str] | None = None, **kw: Any):
    out, err = io.StringIO(), io.StringIO()
    try:
        code = cli.main(argv, environ=env or {}, stdout=out, stderr=err, **kw)
    except SystemExit as exc:  # argparse usage errors exit before main() returns
        code = exc.code
    return code, out.getvalue(), err.getvalue()


def jrun(agent: FakeAgent, *argv: str, env: dict[str, str] | None = None):
    code, out, err = run([*argv, "--base-url", agent.url, "--format", "json"], env)
    return code, (json.loads(out) if out.strip() else None), err


def healthy(agent: FakeAgent) -> None:
    agent.on(
        "GET",
        "/healthz",
        Reply(
            body=HealthzResponse(qdrant=True, embed=True, representation="compatible").model_dump()
        ),
    )
    code, doc, _ = jrun(agent, "health")
    assert code == 0 and doc["data"]["status"] == "ok"


# ------------------------------------------------------------------- health


def test_health_ok_json_and_text(agent):
    agent.on(
        "GET",
        "/healthz",
        Reply(
            body=HealthzResponse(
                qdrant=True, embed=True, representation="compatible", rerank=None
            ).model_dump()
        ),
    )
    code, doc, _ = jrun(agent, "health")
    assert code == 0
    assert doc == {
        "ok": True,
        "command": "health",
        "exit_code": 0,
        "data": {
            "status": "ok",
            "qdrant": True,
            "embed": True,
            "representation": "compatible",
            "rerank": None,
        },
    }
    code, out, _ = run(["health", "--base-url", agent.url])
    assert code == 0 and "status: ok" in out and "rerank: null" in out


def test_health_degraded_is_not_ready_with_state_shown(agent):
    agent.on(
        "GET",
        "/healthz",
        Reply(
            503,
            HealthzResponse(
                status="degraded",
                qdrant=True,
                embed=True,
                representation="compatible",
                rerank=False,
            ).model_dump(),
        ),
    )
    code, doc, _ = jrun(agent, "health")
    assert code == cli.EXIT_UNAVAILABLE
    assert doc["ok"] is False and doc["error"]["code"] == "not_ready"
    assert doc["data"]["rerank"] is False and doc["error"]["status"] == 503
    healthy(agent)


def test_health_qdrant_unready_envelope_is_unavailable_with_server_code(agent):
    agent.on(
        "GET", "/healthz", Reply(503, {"code": "qdrant_unready", "message": "SECRET-UPSTREAM"})
    )
    code, out, err = run(["health", "--base-url", agent.url, "--format", "json"])
    doc = json.loads(out)
    assert code == 5 and doc["error"] == {
        "code": "unavailable",
        "message": cli.ERRORS["unavailable"][1],
        "status": 503,
        "server_code": "qdrant_unready",
    }
    assert "SECRET-UPSTREAM" not in out + err


# ------------------------------------------------------------------- search


def test_search_scoped_request_and_full_structured_hits(agent):
    agent.on(
        "POST",
        "/v1/search",
        Reply(
            body=SearchResponse(
                request_id="r1", query_kind="message", hits=[h.model_dump() for h in (hit(1), hit(2, rerank_score=0.9))]
            ).model_dump(mode="json")
        ),
    )
    code, doc, _ = jrun(
        agent, "search", "IEA500I", "--product", "z/OS", "--version", "3.1", "--limit", "5"
    )
    assert code == 0
    assert agent.requests[-1]["json"] == {
        "query": "IEA500I",
        "product": "z/OS",
        "version": "3.1",
        "limit": 5,
    }
    h = doc["data"]["hits"]
    assert [x["cite"] for x in h] == ["SA22-7601 p.1", "SA22-7601 p.2"]
    assert h[0]["page_start"] == 0 and h[0]["page_end"] == 1 and h[0]["message_ids"] == ["IEA500I"]
    assert h[1]["rerank_score"] == 0.9 and "units" not in h[0]
    assert h[0]["text"] == "IEA500I COMMAND REJECTED\nsecond line"
    code, out, _ = run(["search", "IEA500I", "--base-url", agent.url])
    assert code == 0 and "#1 [0.5000] SA22-7601 p.1" in out and "pdf_pages=1-2" in out
    assert "   | second line" in out


def test_search_empty_is_success_and_unscoped_omits_filters(agent):
    agent.on(
        "POST", "/v1/search", Reply(body={"request_id": "r", "query_kind": "prose", "hits": []})
    )
    code, doc, _ = jrun(agent, "search", "nothing here")
    assert code == 0 and doc["data"]["hits"] == []
    assert agent.requests[-1]["json"] == {"query": "nothing here", "limit": 8}
    code, out, _ = run(["search", "nothing", "--base-url", agent.url])
    assert code == 0 and "hits: 0" in out


# ------------------------------------------------------------ answer (JSON)


def test_answer_accepted_json_preserves_provenance_fields(agent):
    agent.on(
        "POST",
        "/v1/answer",
        Reply(
            body=answer_json(
                script="//JOB",
                script_lang="jcl",
                script_review_required=True,
                citations_inferred=False,
            )
        ),
    )
    code, doc, _ = jrun(agent, "answer", "how?", "--temperature", "0")
    assert code == 0
    d = doc["data"]
    assert d["verification_state"] == "accepted" and d["script_review_required"] is True
    assert d["script"] == "//JOB" and d["script_lang"] == "jcl"
    assert agent.requests[-1]["json"] == {"query": "how?", "temperature": 0.0}
    code, out, _ = run(["answer", "how?", "--base-url", agent.url])
    assert code == 0
    assert "REVIEW REQUIRED, NOT VALIDATED" in out and "script_review_required: true" in out
    assert "verification_state: accepted" in out and "  - SA22-7601 p.1" in out


@pytest.mark.parametrize(
    "state", ["insufficient_evidence", "unverified_draft", "generation_incomplete"]
)
def test_answer_non_accepted_states_exit_3_but_are_labelled(agent, state):
    agent.on("POST", "/v1/answer", Reply(body=answer_json(verification_state=state, citations=[])))
    code, doc, _ = jrun(agent, "answer", "q")
    assert code == cli.EXIT_NOT_ACCEPTED
    assert doc["ok"] is False and doc["error"]["code"] == "answer_not_accepted"
    assert doc["data"]["verification_state"] == state
    code, out, err = run(["answer", "q", "--base-url", agent.url])
    assert code == 3 and f"verification_state: {state}" in out and "answer_not_accepted" in err


def test_answer_inferred_citations_are_flagged_not_grounding(agent):
    agent.on(
        "POST",
        "/v1/answer",
        Reply(body=answer_json(citations_inferred=True, inferred_indices=[1, 2])),
    )
    code, doc, _ = jrun(agent, "answer", "q")
    assert code == 0 and doc["data"]["citations_inferred"] is True
    assert doc["data"]["inferred_indices"] == [1, 2]
    _, out, _ = run(["answer", "q", "--base-url", agent.url])
    assert "citations_inferred: true" in out and "not grounding" in out


@pytest.mark.parametrize("text", ["", "   \n"])
def test_accepted_with_blank_answer_is_never_a_success(agent, text):
    agent.on("POST", "/v1/answer", Reply(body=answer_json(answer=text)))
    code, doc, _ = jrun(agent, "answer", "q")
    assert code == cli.EXIT_BAD_RESPONSE and doc["error"]["code"] == "empty_answer"
    assert "data" not in doc


# ------------------------------------------------------------- answer (SSE)


def test_stream_complete_prints_only_the_final(agent):
    agent.on(
        "POST",
        "/v1/answer",
        Reply(
            ctype="text/event-stream",
            chunks=[
                b": keepalive\n\n",
                token_event("Use").encode(),
                token_event(" it").encode(),
                final_event().encode(),
            ],
        ),
    )
    code, out, _ = run(["answer", "q", "--stream", "--base-url", agent.url, "--format", "json"])
    doc = json.loads(out)
    assert code == 0 and doc["data"]["answer"] == "Use the console."
    assert doc["data"]["finish_reason"] == "stop" and doc["data"]["query_kind"] == "message"
    assert doc["data"]["hits"][0]["cite"] == "SA22-7601 p.1"
    assert doc["data"]["ttft_ms"] == 12 and doc["data"]["usage"]["total_tokens"] == 3
    assert agent.requests[-1]["json"] == {"query": "q", "stream": True}
    code, out, _ = run(["answer", "q", "--stream", "--base-url", agent.url])
    assert code == 0 and "finish_reason: stop" in out and "Use it" not in out


def _stream_failure(agent, chunks):
    agent.on("POST", "/v1/answer", Reply(ctype="text/event-stream", chunks=chunks))
    code, out, err = run(["answer", "q", "--stream", "--base-url", agent.url, "--format", "json"])
    return code, json.loads(out), out + err


@pytest.mark.parametrize(
    "name,chunks,error_code",
    [
        ("eof_after_tokens", [token_event("PARTIAL-ANSWER").encode()], "stream_incomplete"),
        ("empty_stream", [b""], "stream_incomplete"),
        (
            "error_event",
            [
                token_event("PARTIAL-ANSWER").encode(),
                sse.format_sse_event("error", sse.error_payload()).encode(),
            ],
            "stream_failed",
        ),
        ("final_truncated_mid_frame", [final_event()[:-30].encode()], "stream_incomplete"),
        ("final_without_blank_line", [final_event().rstrip("\n").encode()], "stream_incomplete"),
    ],
)
def test_stream_never_prints_a_completed_answer_without_final(agent, name, chunks, error_code):
    code, doc, raw = _stream_failure(agent, chunks)
    assert code == cli.EXIT_BAD_RESPONSE and doc["ok"] is False
    assert doc["error"]["code"] == error_code
    assert doc["data"] == {"verification_state": "generation_incomplete"}
    assert "PARTIAL-ANSWER" not in raw and "Use the console" not in raw
    healthy(agent)  # next ordinary request after the failure


@pytest.mark.parametrize(
    "chunks",
    [
        [b"event: final\ndata: {not json}\n\n"],
        [b"event: final\ndata: [1]\n\n"],
        [b"event: surprise\ndata: {}\n\n"],
        [b'event: final\ndata: {"type": "final"}\n\n'],
        [b"\xff\xfe\n\n"],
        [final_event().encode(), token_event("late").encode()],
    ],
)
def test_stream_malformed_frames_fail_closed(agent, chunks):
    code, doc, raw = _stream_failure(agent, chunks)
    assert code == cli.EXIT_BAD_RESPONSE and doc["error"]["code"] == "malformed_response"
    assert "Use the console" not in raw


def test_stream_final_with_non_stop_finish_or_state_is_not_accepted(agent):
    agent.on(
        "POST",
        "/v1/answer",
        Reply(
            ctype="text/event-stream",
            chunks=[
                final_event(
                    finish_reason="length", verification_state="generation_incomplete"
                ).encode()
            ],
        ),
    )
    code, doc, _ = jrun(agent, "answer", "q", "--stream")
    assert code == 3 and doc["data"]["finish_reason"] == "length"
    agent.on(
        "POST",
        "/v1/answer",
        Reply(ctype="text/event-stream", chunks=[final_event(finish_reason="length").encode()]),
    )  # state lies: accepted + length
    code, doc, _ = jrun(agent, "answer", "q", "--stream")
    assert code == 3 and doc["error"]["code"] == "answer_not_accepted"


def test_stream_wrong_content_type_and_pre_stream_refusal(agent):
    agent.on("POST", "/v1/answer", Reply(body="hello", ctype="text/html"))
    code, doc, _ = jrun(agent, "answer", "q", "--stream")
    assert code == 6 and doc["error"]["code"] == "malformed_response"
    agent.on(
        "POST", "/v1/answer", Reply(503, {"code": "representation_unavailable", "message": "x"})
    )
    code, doc, _ = jrun(agent, "answer", "q", "--stream")
    assert code == 5 and doc["error"]["server_code"] == "representation_unavailable"


def test_stream_cancellation_prints_nothing_and_next_request_is_healthy(agent, monkeypatch):
    agent.on(
        "POST",
        "/v1/answer",
        Reply(
            ctype="text/event-stream",
            chunks=[token_event("PARTIAL-ANSWER").encode(), final_event().encode()],
        ),
    )
    real = cli._frames

    def interrupted(chunks):
        for i, frame in enumerate(real(chunks)):
            yield frame
            if i == 0:
                raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_frames", interrupted)
    code, out, err = run(["answer", "q", "--stream", "--base-url", agent.url, "--format", "json"])
    doc = json.loads(out)
    assert code == cli.EXIT_CANCELLED and doc["error"]["code"] == "cancelled"
    assert "PARTIAL-ANSWER" not in out + err and "Use the console" not in out + err
    monkeypatch.undo()
    healthy(agent)


# --------------------------------------------------------- failure matrix


@pytest.mark.parametrize(
    "status,code_expected,error",
    [
        (401, 4, "unauthorized"),
        (403, 4, "unauthorized"),
        (422, 7, "request_rejected"),
        (404, 7, "request_rejected"),
        (405, 7, "request_rejected"),
        (500, 5, "server_error"),
        (502, 5, "server_error"),
        (503, 5, "unavailable"),
        (302, 6, "unexpected_response"),
    ],
)
def test_status_mapping_and_no_server_text_echo(agent, status, code_expected, error):
    agent.on(
        "POST",
        "/v1/search",
        Reply(
            status,
            {"code": "invalid_request", "message": "UPSTREAM-SECRET-TEXT"},
            headers={"Location": "http://elsewhere/"} if status == 302 else {},
        ),
    )
    code, out, err = run(["search", "q", "--base-url", agent.url, "--format", "json"])
    doc = json.loads(out)
    assert code == code_expected and doc["error"]["code"] == error
    assert "UPSTREAM-SECRET-TEXT" not in out + err and "elsewhere" not in out + err
    assert doc["error"]["message"] == cli.ERRORS[error][1]
    if status == 302:
        assert len(agent.requests) == 1  # redirects are never followed
    agent.on("POST", "/v1/search", Reply(body={"request_id": "r", "query_kind": "x", "hits": []}))
    assert jrun(agent, "search", "q")[0] == 0


def test_unknown_server_code_is_dropped(agent):
    agent.on("POST", "/v1/search", Reply(500, {"code": "Bearer sk-leak", "message": "m"}))
    _, doc, _ = jrun(agent, "search", "q")
    assert "server_code" not in doc["error"]


@pytest.mark.parametrize(
    "reply",
    [
        Reply(body="<html>not json</html>"),
        Reply(body="[]"),
        Reply(body={"request_id": 1, "query_kind": "x", "hits": []}),
        Reply(body={"request_id": "r", "query_kind": "x", "hits": [{"cite": "only"}]}),
        Reply(body={"request_id": "r", "query_kind": "x", "hits": "nope"}),
        Reply(body=b""),
        Reply(body=b'{"request_id": "r", "query_kind"', declared_length=500),  # truncated body
    ],
)
def test_malformed_and_truncated_search_responses(agent, reply):
    agent.on("POST", "/v1/search", reply)
    code, doc, _ = jrun(agent, "search", "q")
    assert code == cli.EXIT_BAD_RESPONSE and doc["error"]["code"] == "malformed_response"
    agent.on("POST", "/v1/search", Reply(body={"request_id": "r", "query_kind": "x", "hits": []}))
    assert jrun(agent, "search", "q")[0] == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"verification_state": "great"},
        {"verification_state": None},
        {"citations": "x"},
        {"citations_inferred": "no"},
        {"inferred_indices": ["1"]},
        {"script_review_required": 1},
        {"answer": None},
    ],
)
def test_malformed_answer_fields_fail_closed(agent, patch):
    body = answer_json()
    body.update(patch)
    agent.on("POST", "/v1/answer", Reply(body=body))
    code, doc, _ = jrun(agent, "answer", "q")
    assert code == 6 and doc["error"]["code"] == "malformed_response"


def test_oversize_body_is_refused(agent, monkeypatch):
    monkeypatch.setattr(cli, "MAX_BODY_BYTES", 64)
    agent.on("POST", "/v1/search", Reply(body=b"x" * 500))
    code, doc, _ = jrun(agent, "search", "q")
    assert code == 6 and doc["error"]["code"] == "malformed_response"


def test_connection_refused_is_unavailable_then_recovers(agent):
    dead = FakeAgent()
    dead_url = dead.url
    dead.close()
    code, out, err = run(["health", "--base-url", dead_url, "--format", "json"])
    assert code == 5 and json.loads(out)["error"]["code"] == "unavailable"
    assert "127.0.0.1" not in out + err
    healthy(agent)


def test_timeout_exit_and_recovery(agent):
    agent.on("GET", "/healthz", Reply(body={}, delay_s=3.0))
    started = time.monotonic()
    code, doc, _ = jrun(agent, "health", "--timeout", "1")
    assert code == 5 and doc["error"]["code"] == "timeout"
    assert time.monotonic() - started < 2.8
    healthy(agent)


# ------------------------------------------------- credentials, TLS, config


def test_api_key_header_and_never_printed(agent, tmp_path):
    keyfile = tmp_path / "key"
    keyfile.write_text(f"{KEY}\n")
    for status in (200, 401, 500):
        agent.on(
            "POST",
            "/v1/search",
            Reply(status, {"request_id": "r", "query_kind": "x", "hits": [], "message": KEY}),
        )
        for argv, env in ((["--api-key-file", str(keyfile)], {}), ([], {cli.ENV_API_KEY: KEY})):
            code, out, err = run(
                ["search", "q", "--base-url", agent.url, "--format", "json", *argv], env
            )
            assert KEY not in out + err and "sk-test" not in out + err
            assert agent.requests[-1]["headers"]["authorization"] == f"Bearer {KEY}"
            assert code == {200: 0, 401: 4, 500: 5}[status]


def test_no_key_means_no_authorization_header_and_flag_does_not_exist(agent):
    agent.on("POST", "/v1/search", Reply(body={"request_id": "r", "query_kind": "x", "hits": []}))
    assert run(["search", "q", "--base-url", agent.url], {cli.ENV_API_KEY: "  "})[0] == 0
    assert "authorization" not in agent.requests[-1]["headers"]
    code, _, err = run(["search", "q", "--base-url", agent.url, "--api-key", KEY])
    assert code == 2 and KEY not in err
    assert agent.requests[-1]["path"] == "/v1/search" and len(agent.requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pw@127.0.0.1:1",
        "http://127.0.0.1:1/?x=1",
        "http://127.0.0.1:1/#f",
        "ftp://127.0.0.1",
        "127.0.0.1:8080",
        "http://",
        "",
        "http://127.0.0.1:99999",
    ],
)
def test_bad_base_url_is_usage_error_without_echo(url):
    code, out, err = run(["health", "--base-url", url, "--format", "json"])
    assert code == 2 and json.loads(out)["error"]["code"] == "usage"
    assert "pw" not in out + err and url.strip("/") not in out + err or url == ""


def test_missing_base_url_is_usage_error_and_env_url_works(agent):
    assert run(["health"], {})[0] == 2
    agent.on("GET", "/healthz", Reply(body=HealthzResponse(qdrant=True).model_dump()))
    assert run(["health"], {cli.ENV_URL: agent.url})[0] == 0
    assert run(["health", "--base-url", agent.url + "/"], {})[0] == 0
    assert agent.requests[-1]["path"] == "/healthz"


def test_key_refused_over_cleartext_non_loopback_and_bad_keys(tmp_path):
    code, out, err = run(
        ["health", "--base-url", "http://agent.example:8080", "--format", "json"],
        {cli.ENV_API_KEY: KEY},
    )
    assert code == 2 and KEY not in out + err
    for bad in ("two words", "tab\tkey", "k" * 5000, "café"):
        code, out, err = run(
            ["health", "--base-url", "https://agent.example", "--format", "json"],
            {cli.ENV_API_KEY: bad},
        )
        assert code == 2 and bad not in out + err
    code, _, _ = run(
        [
            "health",
            "--base-url",
            "https://agent.example",
            "--api-key-file",
            str(tmp_path / "missing"),
        ]
    )
    assert code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["search", " "],
        ["search", "q", "--limit", "0"],
        ["search", "q", "--limit", "41"],
        ["answer", "q", "--temperature", "nan"],
        ["answer", "q", "--temperature", "2.5"],
        ["health", "--timeout", "0"],
        ["health", "--timeout", "601"],
        ["bogus"],
        [],
        ["search", "q", "--format", "xml"],
    ],
)
def test_argument_errors_exit_2_before_any_request(agent, argv):
    code, _, _ = run([*argv, "--base-url", agent.url])
    assert code == 2 and agent.requests == []


def test_usage_errors_do_not_echo_argument_values():
    code, out, err = run(["search", "SECRET-QUERY", "--limit", "SECRET-LIMIT"])
    assert code == 2 and "SECRET" not in out + err


def test_cli_only_reaches_the_three_supported_routes(agent):
    agent.on("GET", "/healthz", Reply(body=HealthzResponse(qdrant=True).model_dump()))
    agent.on("POST", "/v1/search", Reply(body={"request_id": "r", "query_kind": "x", "hits": []}))
    agent.on("POST", "/v1/answer", Reply(body=answer_json()))
    for argv in (["health"], ["search", "q"], ["answer", "q"]):
        assert jrun(agent, *argv)[0] == 0
    assert [(r["method"], r["path"]) for r in agent.requests] == [
        ("GET", "/healthz"),
        ("POST", "/v1/search"),
        ("POST", "/v1/answer"),
    ]
    sub = next(a for a in cli.build_parser()._actions if a.dest == "command")
    assert set(sub.choices) == {"health", "search", "answer"}
    import ast

    tree = ast.parse(Path(cli.__file__).read_text())
    modules = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    for mod in modules:  # no bridge, live-state, Qdrant, retrieval or agent-internals import
        assert (
            mod.split(".")[0]
            in {
                "__future__",
                "argparse",
                "ipaddress",
                "json",
                "os",
                "re",
                "ssl",
                "sys",
                "time",
                "collections",
                "dataclasses",
                "typing",
                "urllib",
                "httpx2",
            }
            or mod == "mainframe_rag.config"
        ), mod
    paths = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value.startswith("/")
        and len(n.value) > 1
    }
    assert paths == {"/healthz", "/v1/search", "/v1/answer"}


def test_terminal_control_sequences_in_server_text_are_neutralized(agent):
    evil = "\x1b]0;pwned\x07\x1b[31m red \x9b"
    agent.on("POST", "/v1/answer", Reply(body=answer_json(answer=f"ok {evil}", citations=[evil])))
    code, out, _ = run(["answer", "q", "--base-url", agent.url])
    assert code == 0 and "\x1b" not in out and "\x07" not in out and "\x9b" not in out
    assert "\\x1b" in out


def test_tls_verification_is_enforced_and_ca_file_is_honoured(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl CLI unavailable")
    crt, key = tmp_path / "ca.crt", tmp_path / "ca.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv = FakeAgent(tls=ctx)
    try:
        srv.on("GET", "/healthz", Reply(body=HealthzResponse(qdrant=True).model_dump()))
        # Pin the failing case to an empty trust store the OS cannot widen.
        other = tmp_path / "other.crt"
        other.write_text(
            subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(tmp_path / "o.key"),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=other",
                ],
                check=True,
                capture_output=True,
                timeout=60,
                text=True,
            ).stdout
        )
        code, out, err = run(
            ["health", "--base-url", srv.url, "--ca-file", str(other), "--format", "json"],
            {cli.ENV_API_KEY: KEY},
        )
        assert code == cli.EXIT_TLS and json.loads(out)["error"]["code"] == "tls_error"
        assert srv.requests == [] and KEY not in out + err
        code, out, _ = run(
            ["health", "--base-url", srv.url, "--ca-file", str(crt), "--format", "json"],
            {cli.ENV_API_KEY: KEY},
        )
        assert code == 0 and srv.requests[-1]["headers"]["authorization"] == f"Bearer {KEY}"
        code, _, _ = run(["health", "--base-url", srv.url, "--ca-file", str(tmp_path / "nope")])
        assert code == 2
        assert not any(
            "insecure" in a or "verify" in a
            for a in (o for act in cli.build_parser()._actions for o in act.option_strings)
        )
    finally:
        srv.close()


# ---------------------------------------------------------------- packaging


def test_entry_point_is_declared_and_module_runs():
    pyproject = (REPO / "pyproject.toml").read_text()
    assert '[project.scripts]\nmainframe-rag-ops = "mainframe_rag.ops.cli:main"' in pyproject
    proc = subprocess.run(
        [sys.executable, "-m", "mainframe_rag.ops", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(cli.__file__).resolve().parents[2])},
    )
    assert proc.returncode == 0 and "Exit codes" in proc.stdout


def test_no_new_runtime_dependency():
    imports = {
        line.split()[1].split(".")[0]
        for line in Path(cli.__file__).read_text().splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    }
    assert imports <= {
        "argparse",
        "ipaddress",
        "json",
        "os",
        "re",
        "ssl",
        "sys",
        "time",
        "collections",
        "dataclasses",
        "typing",
        "urllib",
        "httpx2",
        "mainframe_rag",
    }


@pytest.mark.parametrize("field,value", [
    ("script", "missing"), ("script_lang", "missing"),
    ("verification_state", []), ("verification_state", {"secret": "upstream"}),
])
@pytest.mark.parametrize("stream", [False, True])
def test_cli_process_malformed_nullable_and_enum_fields_have_fixed_error(agent, field, value, stream):
    body = answer_json()
    if value == "missing":
        body.pop(field)
    else:
        body[field] = value
    if stream:
        # Real SSE final shape, altered only in the field under test.
        final = json.loads(final_event().split("data: ", 1)[1].strip())
        if value == "missing":
            final.pop(field)
        else:
            final[field] = value
        agent.on("POST", "/v1/answer", Reply(ctype="text/event-stream", chunks=[sse.format_sse_event("final", final).encode()]))
    else:
        agent.on("POST", "/v1/answer", Reply(body=body))
    proc = subprocess.run(
        [sys.executable, "-m", "mainframe_rag.ops", "answer", "q", "--base-url", agent.url,
         "--format", "json", *(["--stream"] if stream else [])],
        capture_output=True, text=True, timeout=30, check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(cli.__file__).resolve().parents[2])},
    )
    assert proc.returncode == cli.EXIT_BAD_RESPONSE
    assert json.loads(proc.stdout)["error"]["code"] == "malformed_response"
    assert "Traceback" not in proc.stderr and "upstream" not in proc.stdout
    agent.on("POST", "/v1/answer", Reply(body=answer_json()))
    assert jrun(agent, "answer", "q")[0] == 0


@pytest.mark.parametrize("reference", ["ep1.exact:/ whitespace\tµ", None, "absent"])
def test_search_cli_process_preserves_optional_exact_reference(agent, reference):
    body = SearchResponse(request_id="request", query_kind="identifier", hits=[hit().model_dump()]).model_dump()
    if reference == "absent":
        body["hits"][0].pop("reference")
    else:
        body["hits"][0]["reference"] = reference
    agent.on("POST", "/v1/search", Reply(body=body))
    proc = subprocess.run(
        [sys.executable, "-m", "mainframe_rag.ops", "search", "q", "--base-url", agent.url, "--format", "json"],
        capture_output=True, text=True, timeout=30, check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(cli.__file__).resolve().parents[2])},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["data"]["hits"][0]["reference"] == (None if reference == "absent" else reference)
    assert json.loads(proc.stdout)["data"]["hits"][0]["text"] == hit().text


def test_cli_rejects_non_scalar_reference(agent):
    body = SearchResponse(request_id="request", query_kind="identifier", hits=[hit().model_dump()]).model_dump()
    body["hits"][0]["reference"] = ["invalid"]
    agent.on("POST", "/v1/search", Reply(body=body))
    code, doc, _ = jrun(agent, "search", "q")
    assert code == cli.EXIT_BAD_RESPONSE and doc["error"]["code"] == "malformed_response"
