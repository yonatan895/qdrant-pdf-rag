"""Unit tests for scripts/probe_gateway.py (LiteLLM cutover readiness).

Hermetic: scripts.probe_gateway.httpx2 is swapped for a URL-keyed fake —
no network. Success paths are forced with mocks; every failure asserted is
a canned server answer, never a connect error accident.
"""

import httpx2
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

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

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
        status, payload = self._match("POST", url)
        return SimpleResp(url, "POST", status, payload)

    def stream(self, method, url, json=None, timeout=None, headers=None):
        self.calls.append(("STREAM", url, headers))
        _, payload = self._match("STREAM", url)
        lines = payload if isinstance(payload, list) else []
        return StreamCtx(SimpleResp(url, method, 200, {}), lines)


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


def test_probe_models_flag_lists_ids(monkeypatch, capsys):
    _, rc = _run(monkeypatch, _healthy_routes(), argv=["--models"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "embed /models" in out and "reasoning /models" in out and "rerank /models" in out


def test_probe_stream_flag_verifies_done(monkeypatch, capsys):
    routes = [r for r in _healthy_routes() if not (r[0] == "POST" and r[1] == "/chat/completions")]
    routes.append(
        (
            "POST",
            "/chat/completions",
            200,
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
    )
    routes.append(
        (
            "STREAM",
            "/chat/completions",
            200,
            ['data: {"choices": [{"delta": {"content": "o"}}]}', "data: [DONE]"],
        )
    )
    fake, rc = _run(monkeypatch, routes, argv=["--stream"])
    assert rc == 0
    assert "[DONE] ok" in capsys.readouterr().out
    assert any(m == "STREAM" for m, _, _ in fake.calls)


def test_probe_stream_without_done_warns_but_passes(monkeypatch, capsys):
    routes = [r for r in _healthy_routes() if not (r[0] == "POST" and r[1] == "/chat/completions")]
    routes.append(
        (
            "POST",
            "/chat/completions",
            200,
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
    )
    routes.append(
        (
            "STREAM",
            "/chat/completions",
            200,
            ['data: {"choices": [{"delta": {"content": "o"}}]}'],
        )
    )
    _, rc = _run(monkeypatch, routes, argv=["--stream"])
    assert rc == 0
    assert "will not" in capsys.readouterr().out
