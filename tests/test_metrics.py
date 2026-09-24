"""Unit tests for agent/metrics.py (issue #187): lifecycle and request counters.

Setup tests pin idempotency and the disabled path. Hermetic request tests
exercise cumulative Prometheus counters through the application lifespan.
"""

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent import metrics as metrics_mod
from mainframe_rag.retrieve.query import SearchHit


def test_setup_metrics_disabled_returns_none():
    # Order-independent: the disabled path returns before touching globals,
    # so earlier test files may already have enabled the provider.
    assert metrics_mod.setup_metrics(False) is None


def test_setup_metrics_idempotent():
    first = metrics_mod.setup_metrics(True)
    assert first is not None
    assert metrics_mod.setup_metrics(True) is first


# ------------------------------------------------------- endpoint mapping


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("/v1/search", "search"),
        ("/v1/search/", "search"),
        ("/v1/answer", "answer"),
        ("/v1/answer?stream=true", "answer"),
        ("/metrics", None),
        ("/healthz", None),
        ("/v1/nope", None),
        ("/", None),
    ],
)
def test_endpoint_for_path(path, want):
    from urllib.parse import urlsplit

    assert metrics_mod.endpoint_for_path(urlsplit(path).path) == want


# ------------------------------------------------------- hermetic instruments


def _hermetic_instruments(monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        metrics_mod, "_instruments",
        metrics_mod.create_instruments(provider.get_meter("test")),
    )
    return reader


def _points(reader, name):
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    return list(m.data.data_points)
    raise AssertionError(f"instrument {name} not found")


def test_record_request_counter_and_histograms(monkeypatch):
    reader = _hermetic_instruments(monkeypatch)
    metrics_mod.record_request(
        "search", "ok", query_class="identifier",
        elapsed_s=0.5, hits=3, ttft_ms=120, llm_model="m",
    )
    metrics_mod.record_request("search", "ok", query_class="identifier", elapsed_s=0.25)
    metrics_mod.record_request("answer", "upstream_error", query_class="nl", elapsed_s=1.0, hits=0)
    metrics_mod.record_request("nope", "ok", elapsed_s=9.0)  # unknown endpoint ignored

    counts = _points(reader, "rag.requests.total")
    assert sum(p.value for p in counts) == 3
    by_outcome = {(dict(p.attributes)["endpoint"], dict(p.attributes)["outcome"]): p.value for p in counts}
    assert by_outcome == {("search", "ok"): 2, ("answer", "upstream_error"): 1}

    durations = _points(reader, "rag.request.duration")
    assert sum(p.count for p in durations) == 3
    assert sum(p.sum for p in durations) == pytest.approx(1.75)

    hits = _points(reader, "rag.retrieval.hits")
    assert sum(p.count for p in hits) == 2  # the hitless record adds no point
    assert sorted(p.sum for p in hits) == [0, 3]

    ttft = _points(reader, "rag.llm.ttft")
    assert len(ttft) == 1  # only the measured leg records
    assert ttft[0].count == 1 and ttft[0].sum == 120
    assert dict(ttft[0].attributes) == {"model": "m"}


def test_record_request_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(metrics_mod, "_instruments", None)
    metrics_mod.record_request("search", "ok", query_class="identifier", elapsed_s=1.0, hits=5)


def test_record_request_never_raises(monkeypatch):
    reader = _hermetic_instruments(monkeypatch)
    metrics_mod.record_request("search", "ok", elapsed_s=-1.0, hits=-2)  # clamps, not raises
    assert sum(p.count for p in _points(reader, "rag.request.duration")) == 1


# ------------------------------------------------------- endpoint behavior


def _hit():
    return SearchHit(
        chunk_id="abc123", score=0.42,
        cite="SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6",
        heading="Chapter 2 > IEA500I", text="IEA500I synthetic text",
        doc_id="SA22-0000-00", title="Synthetic Reference", page_label="1-6",
        chunk_type="message", product="z/OS", version="9.9", message_ids=("IEA500I",),
    )


class _Search:
    def __init__(self, exc=None):
        self.exc = exc

    def __call__(self, *args, **kwargs):
        if self.exc:
            raise self.exc
        return [_hit()], "identifier", {"embed_ms": 1, "qdrant_ms": 2}


@pytest.fixture
def client(monkeypatch, servable_representation_gate):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setattr(app_mod, "retrieve_search", _Search())
    with TestClient(app_mod.app) as c:
        yield c


def _series(body, name, *, default=None, **labels):
    # Subset match: the OTel exporter appends otel_scope_* labels to every
    # series, so pin our labels as all present rather than exactly equal.
    for line in body.splitlines():
        if not line.startswith(name + "{"):
            continue
        attrs, _, value = line[len(name) + 1:].partition("} ")
        pairs = dict(p.split("=", 1) for p in attrs.split(","))
        if all(pairs.get(k) == f'"{v}"' for k, v in labels.items()):
            return float(value)
    if default is not None:
        return default
    raise AssertionError(f"series {name} with {labels} missing")


def test_search_success_records_ok_series(client):
    labels = {"endpoint": "search", "outcome": "ok", "query_class": "identifier"}
    # The provider is intentionally process-global. Earlier tests or the next
    # ordinary request can populate this series; each request must add one.
    for _ in range(2):
        before = _series(client.get("/metrics").text, "rag_requests_total", default=0.0, **labels)
        resp = client.post("/v1/search", json={"query": "IEA500I SECRETXYZ"})
        assert resp.status_code == 200
        body = client.get("/metrics").text
        assert _series(body, "rag_requests_total", **labels) == before + 1.0
        # Cardinality law: the query text that triggered the request must never
        # become a label value in the exposition.
        assert "SECRETXYZ" not in body


def test_search_failure_records_outcome_series(client, monkeypatch):
    monkeypatch.setattr(app_mod, "retrieve_search", _Search(exc=RuntimeError("qdrant down")))
    labels = {"endpoint": "search", "outcome": "upstream_error", "query_class": "unknown"}
    for _ in range(2):
        before = _series(client.get("/metrics").text, "rag_requests_total", default=0.0, **labels)
        resp = client.post("/v1/search", json={"query": "IEA500I"})
        assert resp.status_code == 502
        body = client.get("/metrics").text
        assert _series(body, "rag_requests_total", **labels) == before + 1.0


def test_overlong_query_records_invalid_request(client):
    labels = {"endpoint": "search", "outcome": "invalid_request", "query_class": "unknown"}
    for _ in range(2):
        before = _series(client.get("/metrics").text, "rag_requests_total", default=0.0, **labels)
        resp = client.post("/v1/search", json={"query": "Q" * 2001})
        assert resp.status_code == 422
        body = client.get("/metrics").text
        assert _series(body, "rag_requests_total", **labels) == before + 1.0


@pytest.mark.parametrize("path", ["/ui/chat", "/ui/chat/stream", "/ui/healthz", "/ui"])
def test_console_path_mapping(path):
    assert metrics_mod.endpoint_for_path(path) == (
        "console" if path in ("/ui/chat", "/ui/chat/stream") else None
    )


@pytest.mark.parametrize("state", ["accepted", "insufficient_evidence", "unverified_draft",
                                    "generation_incomplete", "SECRET-unknown-state"])
def test_verification_label_is_bounded(monkeypatch, state):
    reader = _hermetic_instruments(monkeypatch)
    metrics_mod.record_request("answer", "ok", verification_state=state)
    attrs = dict(_points(reader, "rag.requests.total")[0].attributes)
    assert attrs.get("verification_state") == (None if state.startswith("SECRET") else state)
    assert "SECRET" not in str(attrs)


@pytest.fixture
def outcome_client(client, monkeypatch):
    from mainframe_rag.agent.tokenizer import FallbackTokenizer
    monkeypatch.setattr(app_mod.settings, "ui_enabled", True)
    monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
    return client


class _OutcomeLLM:
    def __init__(self, state):
        self.state = state

    def chat(self, messages, **kwargs):
        from mainframe_rag.ports import ChatResult, TokenUsage
        if self.state == "error":
            raise RuntimeError("SECRET-upstream")
        content = {
            "accepted": "Synthetic response.\n\nCitations:\n- " + _hit().cite,
            "unverified_draft": "Synthetic draft.",
            "generation_incomplete": "Synthetic prefix.",
        }[self.state]
        return ChatResult(content=content, finish_reason=(
            "length" if self.state == "generation_incomplete" else "stop"
        ), usage=TokenUsage())

    async def chat_stream(self, messages, **kwargs):
        if self.state == "error":
            yield {"type": "token", "delta": "Synthetic prefix."}
            raise RuntimeError("SECRET-upstream")
        result = self.chat(messages)
        yield {"type": "token", "delta": result.content}
        yield {"type": "done", "finish_reason": result.finish_reason, "usage": result.usage}


def _post_outcome(client, path, stream):
    if path == "/ui/chat":
        return client.post(path, data={"message": "IEA500I SECRET-query"})
    payload = ({"query": "IEA500I SECRET-query"} if path == "/v1/answer" else
               {"messages": [{"role": "user", "content": "IEA500I SECRET-query"}]})
    return client.post(path, json=payload if path == "/ui/chat/stream" else {**payload, "stream": stream})


@pytest.mark.parametrize("path,stream,endpoint", [
    ("/v1/answer", False, "answer"), ("/v1/answer", True, "answer"),
    ("/v1/chat", False, "chat"), ("/v1/chat", True, "chat"),
    ("/ui/chat", False, "console"), ("/ui/chat/stream", True, "console"),
])
@pytest.mark.parametrize("state", ["accepted", "unverified_draft", "generation_incomplete", "empty", "error"])
def test_finalized_outcome_series(outcome_client, monkeypatch, path, stream, endpoint, state):
    monkeypatch.setattr(app_mod, "llm", _OutcomeLLM(state))
    if state == "empty":
        monkeypatch.setattr(app_mod, "retrieve_search", lambda *a, **kw: ([], "identifier", {}))
    reader = _hermetic_instruments(monkeypatch)
    for count in (1, 2):
        response = _post_outcome(outcome_client, path, stream)
        assert response.status_code == (502 if state == "error" and not stream else 200)
        points = _points(reader, "rag.requests.total")
        assert len(points) == 1
        assert points[0].value == count
        attrs = dict(points[0].attributes)
        assert attrs["endpoint"] == endpoint
        assert attrs["outcome"] == ("upstream_error" if state == "error" else "ok")
        expected = "insufficient_evidence" if state == "empty" else state
        if state == "error":
            expected = "generation_incomplete" if stream else None
        assert attrs.get("verification_state") == expected
        assert "SECRET" not in str(attrs)
        durations = _points(reader, "rag.request.duration")
        assert sum(p.count for p in durations) == count


@pytest.mark.anyio
@pytest.mark.parametrize("endpoint", ["answer", "chat", "console"])
@pytest.mark.parametrize("close_after", ["token", "final", "error"])
async def test_stream_close_records_once(outcome_client, monkeypatch, endpoint, close_after):
    from fastapi import Request, Response

    from mainframe_rag.webui import routes
    from tests.test_stream_truncation import _scope

    monkeypatch.setattr(app_mod, "llm", _OutcomeLLM("error" if close_after == "error" else "accepted"))
    reader = _hermetic_instruments(monkeypatch)
    path = {"answer": "/v1/answer", "chat": "/v1/chat", "console": "/ui/chat/stream"}[endpoint]
    request = Request(_scope(path))
    messages = [{"role": "user", "content": "IEA500I"}]
    if endpoint == "answer":
        response = await app_mod.v1_answer(request, app_mod.AnswerRequest(query="IEA500I"), Response(), stream=True)
    elif endpoint == "chat":
        response = await app_mod.chat_completions(app_mod.ChatRequest(messages=messages, stream=True), request, Response())
    else:
        response = await routes.ui_chat_stream(request, routes.UiChatRequest(messages=messages))
    body = response.body_iterator
    async for chunk in body:
        if close_after == "token" or (
            close_after == "error" and '"error"' in chunk
        ) or (close_after == "final" and '"verification_state"' in chunk):
            break
    else:
        pytest.fail("target frame never emitted")
    await body.aclose()
    app_mod._record_handler_error(request, "internal_error")
    points = _points(reader, "rag.requests.total")
    assert len(points) == 1 and points[0].value == 1
    attrs = dict(points[0].attributes)
    assert attrs["outcome"] == {"token": "client_disconnect", "final": "ok", "error": "upstream_error"}[close_after]
    assert attrs["verification_state"] == ("accepted" if close_after == "final" else "generation_incomplete")
    assert sum(p.count for p in _points(reader, "rag.request.duration")) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_console_validation_only_counts_when_enabled(outcome_client, monkeypatch, enabled):
    monkeypatch.setattr(app_mod.settings, "ui_enabled", enabled)
    reader = _hermetic_instruments(monkeypatch)
    response = outcome_client.post("/ui/chat/stream", json={})
    assert response.status_code == (422 if enabled else 404)
    if enabled:
        points = _points(reader, "rag.requests.total")
        assert len(points) == 1 and points[0].value == 1
        assert dict(points[0].attributes) == {
            "endpoint": "console", "outcome": "invalid_request", "query_class": "unknown",
        }
    else:
        assert reader.get_metrics_data() is None


def test_broken_metrics_instrument_does_not_break_request(outcome_client, monkeypatch):
    from types import SimpleNamespace

    class BrokenCounter:
        def add(self, *args):
            raise RuntimeError("instrument unavailable")

    monkeypatch.setattr(metrics_mod, "_instruments", SimpleNamespace(requests=BrokenCounter()))
    monkeypatch.setattr(app_mod, "llm", _OutcomeLLM("accepted"))
    response = _post_outcome(outcome_client, "/ui/chat/stream", True)
    assert response.status_code == 200
    assert '"accepted"' in response.text
