"""Unit tests for scripts/probe_gateway.py (LiteLLM cutover readiness).

Hermetic: scripts.probe_gateway.httpx2 is swapped for a URL-keyed fake —
no network. Success paths are forced with mocks; every failure asserted is
a canned server answer, never a connect error accident.
"""

import json

import httpx2
import pytest
import scripts.probe_gateway as probe_mod
from scripts.probe_gateway import main


class SimpleResp:
    def __init__(self, url, method, status, payload):
        self._resp = httpx2.Response(status, json=payload if isinstance(payload, dict) else {})
        # raise_for_status needs a request bound (MockTransport does this
        # for free; direct construction must do it explicitly).
        self._resp.request = httpx2.Request(method, url)
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        self._resp.raise_for_status()

    def json(self):
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("non-JSON body")


class StreamCtx:
    def __init__(self, resp, lines):
        self._resp = resp
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        self._resp.raise_for_status()

    def iter_lines(self):
        return iter(self._lines)


class FakeGateway:
    """httpx2 stand-in: first (method, substring) match wins ("ANY" matches
    every method). Payload is a dict (JSON body) or, for the stream leg, a
    list of SSE lines."""

    HTTPError = httpx2.HTTPError

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.bodies = []

    def _match(self, method, url):
        for route_method, suffix, status, payload in self.routes:
            if route_method in (method, "ANY") and suffix in url:
                return status, payload
        return 404, {}

    def get(self, url, timeout=None, headers=None):
        self.calls.append(("GET", url, headers))
        status, payload = self._match("GET", url)
        return SimpleResp(url, "GET", status, payload)

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append(("POST", url, headers))
        self.bodies.append((url, json))
        status, payload = self._match("POST", url)
        return SimpleResp(url, "POST", status, payload)

    def stream(self, method, url, json=None, timeout=None, headers=None):
        self.calls.append(("STREAM", url, headers))
        self.bodies.append((url, json))
        status, payload = self._match("STREAM", url)
        lines = payload if isinstance(payload, list) else []
        return StreamCtx(SimpleResp(url, method, status, {}), lines)


def _env(monkeypatch, **overrides):
    base = {
        "EMBED_BASE_URL": "http://gw:8000/v1",
        "EMBED_MODEL": "gw-embed",
        "DENSE_DIM": "4",
        "EMBED_API_KEY": "sk-embed",
        "LLM_BASE_URL": "http://gw:8001/v1",
        "LLM_MODEL_REASONING": "gw-reasoning",
        "LLM_API_KEY": "sk-llm",
        "RERANK_ENABLED": "true",
        "RERANK_BASE_URL": "http://gw:8002/v1",
        "RERANK_MODEL": "gw-rerank",
        "RERANK_API_KEY": "sk-rerank",
        "GATEWAY_PROBE_MAX_TOKENS": "16",
    }
    base.update(overrides)
    for k, v in base.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def _healthy_routes():
    vec = [0.1] * 4
    return [
        ("ANY", "/models", 200, {"data": [{"id": "m"}]}),
        ("POST", "/embeddings", 200, {"data": [{"index": 0, "embedding": vec}]}),
        (
            "POST",
            "/chat/completions",
            200,
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ),
        (
            "POST",
            "/v1/score",
            200,
            {"data": [{"index": 0, "score": 0.5}, {"index": 1, "score": 0.6}]},
        ),
        (
            "POST",
            "/v1/rerank",
            200,
            {"results": [{"index": 0, "score": 0.5}, {"index": 1, "score": 0.6}]},
        ),
        ("POST", "/tokenize", 200, {"count": 3}),
    ]


def _run(monkeypatch, routes, argv=None, **env):
    _env(monkeypatch, **env)
    fake = FakeGateway(routes)
    monkeypatch.setattr(probe_mod, "httpx2", fake)
    return fake, main(argv if argv is not None else [])


def test_probe_all_healthy_recommends_score_first(monkeypatch, capsys):
    fake, rc = _run(monkeypatch, _healthy_routes())
    assert rc == 0
    out = capsys.readouterr().out
    assert "GATEWAY PROBE PASSED" in out
    assert "recommendation: RERANK_ENDPOINT_ORDER=score_first" in out
    # Every leg carries its own virtual key.
    by_url = {url: headers for _, url, headers in fake.calls}
    assert by_url["http://gw:8000/v1/embeddings"] == {"Authorization": "Bearer sk-embed"}
    assert by_url["http://gw:8001/v1/chat/completions"] == {"Authorization": "Bearer sk-llm"}
    assert by_url["http://gw:8002/v1/score"] == {"Authorization": "Bearer sk-rerank"}


@pytest.mark.parametrize("route", ["/embeddings", "/chat/completions"])
def test_unreachable_required_gateway_leg_fails(monkeypatch, route):
    _env(monkeypatch)
    fake = FakeGateway(_healthy_routes())
    normal_post = fake.post

    def post(url, **kwargs):
        if url.endswith(route):
            raise httpx2.ConnectError("synthetic unavailable")
        return normal_post(url, **kwargs)

    monkeypatch.setattr(fake, "post", post)
    monkeypatch.setattr(probe_mod, "httpx2", fake)
    assert main(["--require-reasoning"]) == 1


def test_probe_score_dead_recommends_rerank_first(monkeypatch, capsys):
    routes = [r for r in _healthy_routes() if r[1] != "/v1/score"]
    routes.append(("POST", "/v1/score", 404, {}))
    _, rc = _run(monkeypatch, routes)
    assert rc == 0
    out = capsys.readouterr().out
    assert "recommendation: RERANK_ENDPOINT_ORDER=rerank_first" in out


def test_probe_rerank_both_dead_fails_without_recommendation(monkeypatch, capsys):
    routes = [r for r in _healthy_routes() if r[1] not in ("/v1/score", "/v1/rerank")]
    routes.extend([("POST", "/v1/score", 404, {}), ("POST", "/v1/rerank", 500, {})])
    _, rc = _run(monkeypatch, routes)
    assert rc == 1
    out = capsys.readouterr().out
    assert "GATEWAY PROBE FAILED" in out
    assert "recommendation:" not in out


def test_probe_reasoning_unset_skips_chat(monkeypatch, capsys):
    _, rc = _run(monkeypatch, _healthy_routes(), LLM_BASE_URL=None, LLM_MODEL_REASONING=None)
    assert rc == 0
    out = capsys.readouterr().out
    assert "reasoning not configured" in out


def test_probe_embed_dim_mismatch_fails(monkeypatch, capsys):
    routes = [r for r in _healthy_routes() if r[1] != "/embeddings"]
    routes.append(("POST", "/embeddings", 200, {"data": [{"index": 0, "embedding": [0.1] * 8}]}))
    _, rc = _run(monkeypatch, routes)
    assert rc == 1
    assert "DENSE_DIM" in capsys.readouterr().out


def test_probe_embed_401_names_key(monkeypatch, capsys):
    routes = [r for r in _healthy_routes() if r[1] != "/embeddings"]
    routes.append(("POST", "/embeddings", 401, {}))
    _, rc = _run(monkeypatch, routes)
    assert rc == 1
    out = capsys.readouterr().out
    assert "EMBED_API_KEY" in out


def test_probe_tokenize_absent_is_informational(monkeypatch, capsys):
    routes = [r for r in _healthy_routes() if r[1] != "/tokenize"]
    routes.append(("POST", "/tokenize", 404, {}))
    _, rc = _run(monkeypatch, routes)
    assert rc == 0
    assert "estimator fallback" in capsys.readouterr().out




def test_probe_models_flag_lists_ids_and_fails_on_a_dead_leg(monkeypatch, capsys):
    _, rc = _run(monkeypatch, _healthy_routes(), argv=["--models"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "embed /models" in out and "reasoning /models" in out and "rerank /models" in out
    dead = [("ANY", "/models", 503, {}), *_healthy_routes()]
    _, rc = _run(monkeypatch, dead, argv=["--models"])
    assert rc == 1


def test_probe_stream_flag_accepts_a_complete_stream_with_usage_frame(monkeypatch, capsys):
    routes = [
        ("STREAM", "/chat/completions", 200, [
            'data: {"choices": [{"delta": {"content": "ok"}}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
            'data: {"choices": [], "usage": {"total_tokens": 2}}',
            "data: [DONE]",
        ]),
        *_healthy_routes(),
    ]
    fake, rc = _run(monkeypatch, routes, argv=["--stream"])
    assert rc == 0
    assert "[DONE] ok" in capsys.readouterr().out
    assert any(m == "STREAM" for m, _, _ in fake.calls)


@pytest.mark.parametrize("flag,missing", [("--require-reasoning", "LLM_BASE_URL"), ("--stream", "LLM_MODEL_REASONING")])
def test_required_reasoning_cannot_skip(monkeypatch, capsys, flag, missing):
    _, rc = _run(monkeypatch, _healthy_routes(), argv=[flag], **{missing: None})
    assert rc == 1
    assert "reasoning required" in capsys.readouterr().out


def test_models_listing_cannot_bypass_required_checks():
    with pytest.raises(SystemExit) as exc:
        main(["--models", "--require-reasoning"])
    assert exc.value.code == 2


@pytest.mark.parametrize("status,lines", [
    (401, []),
    (200, ["data: [DONE]"]),
    (200, ['data: {"error": {"message": "injected"}}', "data: [DONE]"]),
    (200, ["event: error", "data: [DONE]"]),
    (200, ["data: broken", "data: [DONE]"]),
    (200, ['data: {"choices": []}', "data: [DONE]"]),
    (200, ["data: {}", "data: [DONE]"]),
    (200, ['data: {"choices": [{"delta": {"content": 7}}]}', "data: [DONE]"]),
    (200, ['data: {"choices": [{"delta": null}]}', "data: [DONE]"]),
    (200, ['data: {"choices": [{"delta": {"content": "ok"}}]}', "data: [DONE]"]),
    (200, ['data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}', "data: [DONE]"]),
    (200, ['data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "length"}]}', "data: [DONE]"]),
    (200, [
        'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}',
        'data: {"choices": [{"delta": {"content": "late"}}]}',
        "data: [DONE]",
    ]),
])
def test_stream_errors_never_pass(monkeypatch, status, lines):
    _, rc = _run(monkeypatch, [("STREAM", "/chat/completions", status, lines), *_healthy_routes()],
                 argv=["--stream"])
    assert rc == 1


def test_probe_stream_without_done_fails(monkeypatch, capsys):
    routes = [r for r in _healthy_routes() if r[1] != "/chat/completions"]
    routes += [("POST", "/chat/completions", 200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}),
               ("STREAM", "/chat/completions", 200, ['data: {"choices": [{"delta": {"content": "o"}}]}'])]
    _, rc = _run(monkeypatch, routes, argv=["--stream"])
    assert rc == 1
    assert "no [DONE]" in capsys.readouterr().out


@pytest.mark.parametrize("choices", [None, [], [{"finish_reason": "length"}],
    [{"finish_reason": "stop", "message": {"content": ""}}]])
def test_unusable_json_chat_fails(monkeypatch, choices):
    _, rc = _run(monkeypatch, [("POST", "/chat/completions", 200, {"choices": choices}), *_healthy_routes()])
    assert rc == 1


def test_probe_uses_application_simple_reasoning_effort(monkeypatch):
    fake, rc = _run(monkeypatch, _healthy_routes(), argv=['--require-reasoning'],
                    LLM_REASONING_EFFORT_SIMPLE='low')
    assert rc == 0
    assert [body['reasoning_effort'] for url, body in fake.bodies if url.endswith('/chat/completions')] == ['low']


@pytest.mark.parametrize('status,body', [(404, {}), (200, {'count': -1}),
    (200, {'count': True}), (200, {'tokens': []})])
def test_required_tokenizer_does_not_accept_estimation(monkeypatch, status, body):
    _, rc = _run(monkeypatch, [('POST', '/tokenize', status, body), *_healthy_routes()],
                 argv=['--require-tokenizer'])
    assert rc == 1


def test_required_tokenizer_counts_chat_generation_template(monkeypatch):
    fake, rc = _run(monkeypatch, [('POST', '/tokenize', 200, {'count': 12}), *_healthy_routes()],
                    argv=['--require-tokenizer'])
    assert rc == 0
    body = next(b for url, b in fake.bodies if url.endswith('/tokenize'))
    assert body['messages'] == [{'role': 'user', 'content': probe_mod.PROBE_TEXT}]
    assert body['add_generation_prompt'] is True
    assert body['add_special_tokens'] is False


def test_explicit_completion_budget_reaches_json_and_stream(monkeypatch):
    routes = [("STREAM", "/chat/completions", 200, [
        'data: {"choices": [{"delta": {"content": "ok"}}]}',
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
        "data: [DONE]",
    ]), *_healthy_routes()]
    fake, rc = _run(monkeypatch, routes, argv=["--stream", "--max-tokens", "512"])
    assert rc == 0
    assert [body["max_tokens"] for url, body in fake.bodies
            if url.endswith("/chat/completions")] == [512, 512]


@pytest.mark.parametrize("argv,env,want", [
    ([], "512", 512),
    (["--max-tokens", "4096"], "512", 4096),
])
def test_completion_budget_default_environment_and_cli_precedence(monkeypatch, argv, env, want):
    fake, rc = _run(monkeypatch, _healthy_routes(), argv=argv, GATEWAY_PROBE_MAX_TOKENS=env)
    assert rc == 0
    assert next(body["max_tokens"] for url, body in fake.bodies
                if url.endswith("/chat/completions")) == want


@pytest.mark.parametrize("bad", ["0", "4097", "1.5"])
@pytest.mark.parametrize("via_env", [False, True])
def test_invalid_completion_budget_fails_before_any_model_request(monkeypatch, bad, via_env):
    _env(monkeypatch, GATEWAY_PROBE_MAX_TOKENS=bad if via_env else "16")
    fake = FakeGateway(_healthy_routes())
    monkeypatch.setattr(probe_mod, "httpx2", fake)
    with pytest.raises(SystemExit) as exc:
        main([] if via_env else ["--max-tokens", bad])
    assert exc.value.code == 2
    assert fake.calls == []


@pytest.mark.parametrize("content,finish", [("ok", "length"), ("   ", "stop")])
def test_larger_budget_retains_strict_final_answer_gate(monkeypatch, content, finish):
    routes = [("POST", "/chat/completions", 200, {"choices": [{
        "message": {"content": content, "reasoning_content": "private reasoning"},
        "finish_reason": finish,
    }]}), *_healthy_routes()]
    _, rc = _run(monkeypatch, routes, argv=["--max-tokens", "512"])
    assert rc == 1


# --- Argument probe (issue #1193): a real loopback upstream, real httpx ---

_PRIVATE_REASONING = "PRIVATE-REASONING-TEXT step by step"
_PRIVATE_ANSWER = "PRIVATE-ANSWER-TEXT 06:10 06:55"
_PRIVATE_UPSTREAM_ERROR = "PRIVATE-UPSTREAM-ERROR unknown template argument"


class ArgumentUpstream:
    """OpenAI-compatible chat upstream on 127.0.0.1 whose behaviour depends on
    the request arguments, as a served chat template's does:

    - `chat_template_kwargs.thinking` present: HTTP 400 with an error text
      (a gateway refusing an argument it does not know);
    - thinking switched off (`enable_thinking: false`): content only;
    - otherwise reasoning deltas in a separate field, then content, with more
      reasoning at effort `high` than at `low`;
    - `buffered: true`: a JSON body despite the stream ask.

    Usage reports `completion_tokens_details.reasoning_tokens` only when
    `report_reasoning_tokens` is set (vLLM does not; some gateways do)."""

    def __init__(self, report_reasoning_tokens=True):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        upstream = self
        self.requests: list[dict] = []
        self.report_reasoning_tokens = report_reasoning_tokens

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                upstream.requests.append(
                    {"path": self.path, "body": body, "authorization": self.headers.get("Authorization")})
                kwargs = body.get("chat_template_kwargs", {})
                if "thinking" in kwargs:
                    return self._send(400, "application/json", json.dumps({"error": {"message": _PRIVATE_UPSTREAM_ERROR}}))
                effort = body.get("reasoning_effort") or kwargs.get("reasoning_effort") or "medium"
                thinks = kwargs.get("enable_thinking", True)
                steps = {"low": 1, "medium": 2, "high": 4, "xhigh": 6}[effort] if thinks else 0
                usage = {"prompt_tokens": 41, "completion_tokens": 6 + 5 * steps, "total_tokens": 47 + 5 * steps}
                if upstream.report_reasoning_tokens:
                    usage["completion_tokens_details"] = {"reasoning_tokens": 5 * steps}
                if body.get("buffered"):
                    message = {"content": _PRIVATE_ANSWER, "reasoning_content": _PRIVATE_REASONING * steps}
                    return self._send(200, "application/json", json.dumps(
                        {"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage}))
                finish = "length" if body["max_tokens"] < 6 + 5 * steps else "stop"
                frames = [{"choices": [{"delta": {"reasoning_content": _PRIVATE_REASONING}}]} for _ in range(steps)]
                frames.append({"choices": [{"delta": {"content": _PRIVATE_ANSWER}}]})
                frames.append({"choices": [{"delta": {}, "finish_reason": finish}]})
                frames.append({"choices": [], "usage": usage})
                text = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
                return self._send(200, "text/event-stream", text)

            def _send(self, status, content_type, text):
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"


def _arg_probe(monkeypatch, capsys, upstream, argv, **env):
    """Run `--probe-args` against the loopback upstream; (rc, rows, summary, raw output)."""
    _env(monkeypatch, **{
        "LLM_BASE_URL": upstream.base_url, "LLM_API_KEY": "sk-PRIVATE-KEY", "GATEWAY_PROBE_MAX_TOKENS": None,
        "LLM_EXTRA_BODY": None, "LLM_EFFORT_TRANSPORT": None, **env})
    rc = main(["--probe-args", *argv])
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    return rc, [json.loads(line) for line in lines[:-1]], lines[-1], out


_ROW_KEYS = {
    "candidate", "args", "status", "error", "finish_reason", "prompt_tokens", "completion_tokens",
    "reasoning_tokens", "reasoning_field", "reasoning_chars", "content_chars", "ttft_ms",
    "first_content_ms", "decode_tps", "elapsed_ms",
}


def test_argument_probe_reports_each_candidate_without_any_text(monkeypatch, capsys):
    """One fixed prompt under every candidate; rows show which arguments
    change the model's thinking, and carry no prompt, answer, reasoning,
    key or upstream error text."""
    with ArgumentUpstream() as upstream:
        rc, rows, summary, out = _arg_probe(monkeypatch, capsys, upstream, [])
    assert [row["candidate"] for row in rows] == [
        "baseline", "configured",
        "effort-top-low", "effort-template-low", "effort-top-medium", "effort-template-medium",
        "effort-top-xhigh", "effort-template-xhigh",
        "enable-thinking-on", "enable-thinking-off", "thinking-on", "thinking-off",
    ]
    by_name = {row["candidate"]: row for row in rows}
    assert all(set(row) == _ROW_KEYS for row in rows)
    # The same fixed prompt, bounded, streamed, authenticated, once per candidate.
    assert len(upstream.requests) == len(rows)
    assert {json.dumps(r["body"]["messages"]) for r in upstream.requests} == {
        json.dumps([{"role": "user", "content": probe_mod.ARG_PROBE_PROMPT}])}
    assert all(r["path"] == "/v1/chat/completions" and r["authorization"] == "Bearer sk-PRIVATE-KEY"
               and r["body"]["stream"] is True and r["body"]["max_tokens"] == probe_mod.ARG_PROBE_MAX_TOKENS
               for r in upstream.requests)
    # Each candidate's arguments reached the wire exactly as its row states.
    fixed = {"model", "messages", "temperature", "max_tokens", "stream", "stream_options"}
    assert [{k: v for k, v in r["body"].items() if k not in fixed} for r in upstream.requests] == [
        row["args"] for row in rows]
    assert by_name["baseline"]["args"] == {}
    assert by_name["configured"]["args"] == {"reasoning_effort": "medium"}
    # Discovery: effort and the thinking switch show up in the counts.
    low, medium = by_name["effort-template-low"], by_name["effort-top-medium"]
    assert (low["status"], low["finish_reason"], low["prompt_tokens"]) == (200, "stop", 41)
    assert (low["completion_tokens"], low["reasoning_tokens"]) == (11, 5)
    assert (medium["completion_tokens"], medium["reasoning_tokens"]) == (16, 10)
    assert low["reasoning_field"] and low["reasoning_chars"] == len(_PRIVATE_REASONING)
    assert medium["reasoning_chars"] == 2 * len(_PRIVATE_REASONING)
    off = by_name["enable-thinking-off"]
    assert (off["reasoning_field"], off["reasoning_chars"], off["reasoning_tokens"]) == (False, 0, 0)
    assert off["content_chars"] == len(_PRIVATE_ANSWER) and off["error"] is None
    for row in (low, medium, off):
        assert isinstance(row["ttft_ms"], int) and isinstance(row["first_content_ms"], int)
        assert 0 <= row["ttft_ms"] <= row["first_content_ms"] <= row["elapsed_ms"]
    # A refused argument is a finding: status and a fixed label, nothing else.
    refused = by_name["thinking-on"]
    assert (refused["status"], refused["error"], refused["finish_reason"]) == (400, "http_error", None)
    assert refused["prompt_tokens"] is None and refused["decode_tps"] is None
    assert rc == 0 and summary == f"ARGUMENT PROBE DONE: candidates={len(rows)} completed={len(rows) - 2}"
    for private in (probe_mod.ARG_PROBE_PROMPT, "PRIVATE-", "sk-PRIVATE-KEY", "06:10", "step by step"):
        assert private not in out


def test_argument_probe_builtin_candidates_follow_the_served_efforts(monkeypatch, capsys):
    with ArgumentUpstream() as upstream:
        _, rows, _, _ = _arg_probe(monkeypatch, capsys, upstream, [], LLM_REASONING_EFFORTS="low,medium,high",
                                   GATEWAY_PROBE_MAX_TOKENS="16",
                                   LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}',
                                   LLM_EFFORT_TRANSPORT="chat_template", LLM_REASONING_EFFORT_SIMPLE="high")
    assert [row["candidate"] for row in rows] == [
        "baseline", "configured",
        "effort-top-low", "effort-template-low", "effort-top-medium", "effort-template-medium",
        "effort-top-high", "effort-template-high",
        "enable-thinking-on", "enable-thinking-off", "thinking-on", "thinking-off",
    ]
    by_name = {row["candidate"]: row for row in rows}
    # `configured` is what the application sends at the simple effort.
    assert by_name["configured"]["args"] == {
        "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "high"}}
    assert by_name["configured"]["reasoning_field"] is False
    assert by_name["effort-top-high"]["args"] == {"reasoning_effort": "high"}
    assert by_name["effort-top-high"]["reasoning_tokens"] == 20
    # The readiness budget variable (16 here) does not shrink this mode's budget.
    assert {r["body"]["max_tokens"] for r in upstream.requests} == {probe_mod.ARG_PROBE_MAX_TOKENS}


def test_argument_probe_takes_operator_candidates_from_file_and_flags(monkeypatch, capsys, tmp_path):
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({
        "site-budget": {"chat_template_kwargs": {"thinking_budget": 256}, "top_k": 20},
        "thinking-off": {"seed": 7},  # a later definition replaces the built-in in place
    }), encoding="utf-8")
    with ArgumentUpstream(report_reasoning_tokens=False) as upstream:
        rc, rows, summary, _ = _arg_probe(monkeypatch, capsys, upstream, [
            "--candidates-file", str(candidates),
            "--candidate", 'short={"reasoning_effort": "high", "max_tokens": 8}',
            "--candidate", 'whole={"buffered": true}',
            "--only", "short,site-budget", "--only", "thinking-off,whole", "--max-tokens", "64", "--timeout", "5",
        ], GATEWAY_PROBE_MAX_TOKENS="16")  # the readiness budget is not this mode's
    assert [row["candidate"] for row in rows] == ["short", "site-budget", "thinking-off", "whole"]
    assert [r["body"]["max_tokens"] for r in upstream.requests] == [8, 64, 64, 64]
    assert upstream.requests[1]["body"]["chat_template_kwargs"] == {"thinking_budget": 256}
    assert upstream.requests[1]["body"]["top_k"] == 20 and upstream.requests[2]["body"]["seed"] == 7
    short, _, _, whole = rows
    # A cut generation is reported as its finish; usage without a reasoning
    # breakdown reports none rather than zero.
    assert (short["finish_reason"], short["reasoning_tokens"], short["reasoning_field"]) == ("length", None, True)
    # A buffered JSON answer still yields counts, with no first-token time.
    assert (whole["status"], whole["error"], whole["finish_reason"]) == (200, None, "stop")
    assert (whole["ttft_ms"], whole["first_content_ms"], whole["decode_tps"]) == (None, None, None)
    assert whole["content_chars"] == len(_PRIVATE_ANSWER) and whole["completion_tokens"] == 16
    assert rc == 0 and summary == "ARGUMENT PROBE DONE: candidates=4 completed=4"


def test_argument_probe_fails_only_when_the_baseline_does_not_complete(monkeypatch, capsys):
    """Unreachable gateway: every row is a transport label (class name only)
    and the mode fails because `baseline` did not complete."""
    with ArgumentUpstream() as upstream:
        dead = upstream.base_url
    _env(monkeypatch, LLM_BASE_URL=dead, GATEWAY_PROBE_MAX_TOKENS=None, LLM_EXTRA_BODY=None,
         LLM_EFFORT_TRANSPORT=None)
    rc = main(["--probe-args", "--only", "baseline,enable-thinking-off", "--timeout", "2"])
    lines = capsys.readouterr().out.strip().splitlines()
    rows = [json.loads(line) for line in lines[:-1]]
    assert rc == 1 and lines[-1] == "ARGUMENT PROBE FAILED: candidates=2 completed=0"
    assert all(row["status"] is None and row["error"] == "transport_error:ConnectError" for row in rows)
    assert "127.0.0.1" not in "".join(lines)


def test_argument_probe_requires_a_reasoning_model(monkeypatch, capsys):
    _env(monkeypatch, LLM_MODEL_REASONING=None, GATEWAY_PROBE_MAX_TOKENS=None)
    fake = FakeGateway([])
    monkeypatch.setattr(probe_mod, "httpx2", fake)
    assert main(["--probe-args"]) == 1
    assert "not configured" in capsys.readouterr().out and fake.calls == []


@pytest.mark.parametrize("argv", [
    ["--candidate", 'bad={"messages": []}'],
    ["--candidate", 'bad={"model": "PRIVATE-TEXT"}'],
    ["--candidate", 'bad={"stream": false}'],
    ["--candidate", 'bad=["PRIVATE-TEXT"]'],
    ["--candidate", "PRIVATE TEXT={}"],
    ["--candidate", "no-separator"],
    ["--candidate", "baseline={}"],
    ["--only", "PRIVATE-TEXT-undefined"],
    ["--no-builtin"],
    ["--candidates-file", "/nonexistent/PRIVATE-TEXT.json"],
    ["--models"],
    ["--stream"],
])
def test_argument_probe_refuses_bad_candidates_before_any_request(monkeypatch, capsys, argv):
    _env(monkeypatch, GATEWAY_PROBE_MAX_TOKENS=None)
    fake = FakeGateway([])
    monkeypatch.setattr(probe_mod, "httpx2", fake)
    with pytest.raises(SystemExit) as exc:
        main(["--probe-args", *argv])
    assert exc.value.code == 2 and fake.calls == []
    assert "PRIVATE" not in capsys.readouterr().err


def test_candidate_options_require_the_argument_probe(monkeypatch, capsys):
    _env(monkeypatch)
    fake = FakeGateway(_healthy_routes())
    monkeypatch.setattr(probe_mod, "httpx2", fake)
    with pytest.raises(SystemExit) as exc:
        main(["--candidate", "x={}"])
    assert exc.value.code == 2 and fake.calls == []


@pytest.mark.parametrize(("transport", "extra", "expected"), [
    (None, None, {"reasoning_effort": "medium"}),
    ("chat_template", '{"chat_template_kwargs": {"enable_thinking": true}, "top_k": 20}',
     {"chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "medium"}, "top_k": 20}),
    ("none", None, {}),
])
def test_readiness_chat_sends_the_configured_model_arguments(monkeypatch, transport, extra, expected):
    """The readiness chat mirrors the application's request (#1193): unset,
    the top-level effort it always sent, in the same key order."""
    fake, rc = _run(monkeypatch, _healthy_routes(), LLM_EFFORT_TRANSPORT=transport, LLM_EXTRA_BODY=extra)
    assert rc == 0
    body = next(b for url, b in fake.bodies if url.endswith("/chat/completions"))
    assert {k: v for k, v in body.items() if k not in ("model", "messages", "temperature", "max_tokens")} == expected
    if transport is None:
        assert list(body) == ["model", "messages", "temperature", "max_tokens", "reasoning_effort"]
