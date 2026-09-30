"""Source-privacy canaries (issue #529 OBS-1A §4.4).

Prohibited content — raw query text, header values, exception bodies,
document identifiers — must be absent from every telemetry surface
(stdout JSON, span attributes/events/status, metric labels), both before
and after collection. Safe context (action, request_id, error types,
counts, timings, finite labels) must remain. All assertions run through
the real SDK exporters and test readers, never around them.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from mainframe_rag import tracing as tracing_mod
from mainframe_rag.agent import answer_core as answer_core_mod
from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent import metrics as metrics_mod
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.logs import JsonFormatter, error_type
from mainframe_rag.ports import ChatResult, TokenUsage
from tests.test_metrics import _points

CANARY_QUERY = "canary-query-ZK7xq Kostenstelle"
CANARY_HEADER = "canary-header-QQ9vv"
CANARY_EXC = "canary-exc-XJ4zz"
CANARY_DOC = "CANARY-DOC-11"
CANARIES = (CANARY_QUERY, CANARY_HEADER, CANARY_EXC, CANARY_DOC)

FINITE_METRIC_LABELS = {
    "endpoint",
    "query_class",
    "outcome",
    "verification_state",
    "model",
}


def _provider():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _hit(doc_id="SA22-0000-00"):
    from mainframe_rag.retrieve.query import SearchHit

    return SearchHit(
        chunk_id="abc123",
        score=0.42,
        cite=f"{doc_id} Synthetic Reference, Chapter 2 > IEA500I, p. 1-6",
        heading="Chapter 2 > IEA500I",
        text="IEA500I synthetic text",
        doc_id=doc_id,
        title="Synthetic Reference",
        page_label="1-6",
        chunk_type="message",
        product="z/OS",
        version="9.9",
        message_ids=("IEA500I",),
    )


class MagicSearch:
    def __init__(self, exc=None, hits=None):
        self.exc = exc
        self.hits = hits if hits is not None else [_hit()]

    def __call__(self, *args, **kwargs):
        if self.exc:
            raise self.exc
        return list(self.hits), "identifier", {"embed_ms": 1, "qdrant_ms": 2}


class FakeLLM:
    def chat(self, messages, reasoning_effort=None, temperature=None):
        return ChatResult(
            content=(
                "Answer text.\n\n"
                "Citations:\n"
                "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            ),
            finish_reason="stop",
            usage=TokenUsage(),
        )


@pytest.fixture(autouse=True)
def _reset_provider(monkeypatch):
    monkeypatch.setattr(tracing_mod, "_provider", None)
    monkeypatch.setattr(trace, "set_tracer_provider", lambda _p: None)


@pytest.fixture
def client(monkeypatch, servable_representation_gate):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    provider, exporter = _provider()
    monkeypatch.setattr(app_mod, "retrieve_search", MagicSearch())
    with TestClient(app_mod.app) as c:
        monkeypatch.setattr(app_mod, "llm", FakeLLM())
        monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
        monkeypatch.setattr(app_mod, "tracer", provider.get_tracer("test"))
        monkeypatch.setattr(answer_core_mod, "tracer", provider.get_tracer("test"))
        yield c, exporter


@pytest.fixture
def metric_reader(monkeypatch):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        metrics_mod,
        "_instruments",
        metrics_mod.create_instruments(provider.get_meter("test")),
    )
    return reader


def _span_text(spans):
    """Complete SDK records, including attributes, events and status text."""
    return "\n".join(span.to_json() for span in spans)


def _metric_text(reader):
    texts = []
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    for key, value in dict(point.attributes or {}).items():
                        texts.append(key)
                        texts.append(str(value))
    return "\n".join(texts)


def _metric_label_keys(reader):
    keys = set()
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    keys.update(dict(point.attributes or {}).keys())
    return keys


def _formatted_logs(caplog):
    formatter = JsonFormatter()
    return [formatter.format(record) for record in caplog.records]


def test_error_type_never_exports_bodies():
    assert error_type(ValueError(CANARY_EXC)) == "ValueError"
    assert error_type(None) == "Error"
    assert CANARY_EXC not in error_type(ValueError(CANARY_EXC))


def test_success_hides_canaries_from_logs_and_spans(client, caplog, monkeypatch):
    c, exporter = client
    # The hit itself carries the identifier canary: proves doc ids stay
    # out of spans and logs while counts remain.
    monkeypatch.setattr(
        app_mod, "retrieve_search", MagicSearch(hits=[_hit(doc_id=CANARY_DOC)])
    )
    with caplog.at_level(logging.INFO):
        resp = c.post(
            "/v1/search",
            json={"query": CANARY_QUERY},
            headers={"X-Canary-Probe": CANARY_HEADER},
        )
        resp = c.post(
            "/v1/search",
            json={"query": CANARY_QUERY},
            headers={"X-Canary-Probe": CANARY_HEADER},
        )
    assert resp.status_code == 200
    logs_blob = "\n".join(_formatted_logs(caplog))
    assert CANARY_QUERY not in logs_blob
    assert CANARY_HEADER not in logs_blob
    spans_blob = _span_text(exporter.get_finished_spans())
    assert exporter.get_finished_spans(), "success must render spans"
    assert CANARY_QUERY not in spans_blob
    assert CANARY_HEADER not in spans_blob
    assert CANARY_DOC not in spans_blob
    # Safe context remains: request identity, kind, counts.
    assert resp.json()["request_id"] in logs_blob
    root = next(s for s in exporter.get_finished_spans() if s.name == "v1.search")
    assert root.attributes["rag.query_kind"] == "identifier"
    assert root.attributes["rag.hits"] == 1


def test_failure_hides_canaries_but_keeps_error_type(client, caplog, monkeypatch):
    c, exporter = client
    monkeypatch.setattr(
        app_mod, "retrieve_search", MagicSearch(exc=RuntimeError(CANARY_EXC))
    )
    with caplog.at_level(logging.INFO):
        resp = c.post(
            "/v1/search",
            json={"query": CANARY_QUERY},
            headers={"X-Canary-Probe": CANARY_HEADER},
        )
    assert resp.status_code == 502
    logs_blob = "\n".join(_formatted_logs(caplog))
    for canary in CANARIES:
        assert canary not in logs_blob, f"{canary} leaked into stdout JSON"
    assert "RuntimeError" in logs_blob
    spans = exporter.get_finished_spans()
    assert spans, "failure must still render spans"
    spans_blob = _span_text(spans)
    for canary in CANARIES:
        assert canary not in spans_blob, f"{canary} leaked into spans"
    root = next(s for s in spans if s.name == "v1.search")
    assert root.status.status_code == trace.StatusCode.ERROR
    assert root.status.description == "RuntimeError"
    assert any(
        e.name == "exception"
        and e.attributes.get("exception.type") == "RuntimeError"
        for e in root.events
    )


_ATTACHMENT_ROUTES = [
    ("/v1/answer", False, "answer"),
    ("/v1/answer", True, "answer"),
    ("/v1/answer?stream=true", False, "answer"),
    ("/v1/chat", False, "chat"),
    ("/v1/chat", True, "chat"),
    ("/v1/chat/completions", False, "chat"),
    ("/v1/chat/completions", True, "chat"),
    ("/ui/chat", False, "console"),
    ("/ui/chat/stream", True, "console"),
]


@pytest.mark.parametrize(
    ("path", "stream", "endpoint", "failure"),
    [
        (*route, failure)
        for route in _ATTACHMENT_ROUTES
        for failure in (
            "sync_retrieval", "awaited_retrieval", "sync_condensation", "awaited_condensation",
        )
        if route[2] != "answer" or failure.endswith("retrieval")
    ],
)
def test_attachment_failures_hide_canaries(
    client, metric_reader, caplog, monkeypatch, path, stream, endpoint, failure,
):
    """Issue #563: escaping failures must be private before SDK export."""
    connection, exporter = client
    calls = []

    def fail_sync(*args, **kwargs):
        calls.append(failure)
        raise RuntimeError(" / ".join(CANARIES))

    async def fail_awaited(*args, **kwargs):
        await asyncio.sleep(0)
        fail_sync(*args, **kwargs)

    fail = fail_awaited if failure.startswith("awaited") else fail_sync
    condensation = failure.endswith("condensation")
    if condensation:
        monkeypatch.setattr(answer_core_mod, "condense_query", fail)
        monkeypatch.setattr(app_mod.settings, "chat_condense_enabled", True)
    else:
        monkeypatch.setattr(app_mod, "retrieve_search", fail)
    monkeypatch.setattr(app_mod.settings, "ui_enabled", True)
    messages = [
        {"role": "user", "content": "Original synthetic question"},
        {"role": "assistant", "content": "Original synthetic answer"},
        {"role": "user", "content": CANARY_QUERY},
    ]
    headers = {
        "X-Canary-Probe": CANARY_HEADER,
        "traceparent": "00-1234567890abcdef1234567890abcdef-abcdef1234567890-01",
    }
    with caplog.at_level(logging.INFO):
        if path == "/ui/chat":
            response = connection.post(path, headers=headers, data={
                "message": CANARY_QUERY, "messages": json.dumps(messages[:-1]),
            })
        else:
            payload = {} if endpoint == "console" else {"stream": stream}
            payload.update({"query": CANARY_QUERY} if endpoint == "answer" else {"messages": messages})
            response = connection.post(path, headers=headers, json=payload)
    assert calls == [failure]
    if path == "/ui/chat/stream":
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = response.text.strip().split("\n")
        assert frames[0] == "event: error"
        assert json.loads(frames[1].removeprefix("data: ")) == {
            "type": "error", "code": "upstream_error", "message": "stream failed",
            "verification_state": "generation_incomplete",
        }
        assert "event: final" not in response.text
    elif path == "/ui/chat":
        assert response.status_code == 502
        assert "The reasoning agent could not complete this request." in response.text
    else:
        assert response.status_code == 502
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"code": "upstream_error", "message": "retrieval failed"}

    spans = exporter.get_finished_spans()
    assert spans, "failure must still render spans"
    logs_blob = "\n".join(_formatted_logs(caplog))
    spans_blob = _span_text(spans)
    metrics_blob = _metric_text(metric_reader)
    for canary in CANARIES:
        assert canary not in spans_blob, f"{canary} leaked into spans"
        assert canary not in logs_blob, f"{canary} leaked into stdout JSON"
        assert canary not in metrics_blob, f"{canary} leaked into metric labels"
    assert _metric_label_keys(metric_reader) <= FINITE_METRIC_LABELS
    root_name = "ui.chat" if endpoint == "console" else f"v1.{endpoint}"
    roots = [span for span in spans if span.name == root_name]
    assert len(roots) == 1
    root = roots[0]
    expected_type = "RetrievalError" if endpoint == "console" and not condensation else "RuntimeError"
    assert root.status.status_code == trace.StatusCode.ERROR
    assert root.status.description == expected_type
    assert [dict(event.attributes) for event in root.events if event.name == "exception"] == [
        {"exception.type": expected_type},
    ]
    assert root.kind == trace.SpanKind.SERVER
    assert root.context.trace_id == 0x1234567890ABCDEF1234567890ABCDEF
    assert root.parent.span_id == 0xABCDEF1234567890
    assert len(root.attributes["http.request_id"]) == 12
    if endpoint != "console":
        assert root.attributes["http.request_id"] in logs_blob
    if condensation:
        children = [span for span in spans if span.name == "chat.condense"]
        assert len(children) == 1
        assert children[0].parent.span_id == root.context.span_id
    counts = _points(metric_reader, "rag.requests.total")
    assert len(counts) == 1 and counts[0].value == 1
    assert dict(counts[0].attributes) == {
        "endpoint": endpoint, "outcome": "upstream_error", "query_class": "unknown",
        **({"verification_state": "generation_incomplete"} if path == "/ui/chat/stream" else {}),
    }
    assert sum(point.count for point in _points(metric_reader, "rag.request.duration")) == 1


@pytest.mark.parametrize(
    "failure",
    ["sync_retrieval", "awaited_retrieval", "sync_condensation", "awaited_condensation"],
)
def test_attachment_privacy_negative_control(client, metric_reader, caplog, monkeypatch, failure):
    """Restoring SDK attachment defaults must trip the endpoint canary."""
    monkeypatch.setattr(app_mod, "use_span", trace.use_span)
    endpoint = "chat" if failure.endswith("condensation") else "answer"
    with pytest.raises(AssertionError, match="leaked into spans"):
        test_attachment_failures_hide_canaries(
            client, metric_reader, caplog, monkeypatch, f"/v1/{endpoint}", False, endpoint, failure,
        )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("end_on_exit", [False, True])
@pytest.mark.parametrize("awaited", [False, True])
def test_safe_attachment_preserves_parent_and_lifetime(enabled, end_on_exit, awaited):
    provider, exporter = _provider()
    span_tracer = (
        provider.get_tracer("attachment") if enabled else trace.NoOpTracerProvider().get_tracer("off")
    )
    root = tracing_mod.start_span(span_tracer, "root")
    ambient = trace.get_current_span()

    async def escape():
        with tracing_mod.use_span(root, end_on_exit=end_on_exit):
            assert trace.get_current_span() is root
            with tracing_mod.start_as_current_span(span_tracer, "child"):
                pass
            if awaited:
                await asyncio.sleep(0)
            raise RuntimeError(CANARY_EXC)

    try:
        with pytest.raises(RuntimeError, match=CANARY_EXC):
            asyncio.run(escape())
        assert trace.get_current_span() is ambient
        assert root.is_recording() == (enabled and not end_on_exit)
        if not end_on_exit:
            app_mod._span_error(root, RuntimeError(CANARY_EXC))
            root.end()
        spans = exporter.get_finished_spans()
        assert CANARY_EXC not in _span_text(spans)
        if enabled:
            assert {span.name for span in spans} == {"root", "child"}
            child = next(span for span in spans if span.name == "child")
            assert child.parent.span_id == root.get_span_context().span_id
            finished_root = next(span for span in spans if span.name == "root")
            assert finished_root.status.status_code == (
                trace.StatusCode.UNSET if end_on_exit else trace.StatusCode.ERROR
            )
            assert [dict(event.attributes) for event in finished_root.events] == (
                [] if end_on_exit else [{"exception.type": "RuntimeError"}]
            )
        else:
            assert not spans
    finally:
        provider.shutdown()


def test_metric_labels_hide_canaries_and_stay_finite(
    client, metric_reader, monkeypatch
):
    c, _exporter = client
    monkeypatch.setattr(
        app_mod, "retrieve_search", MagicSearch(exc=RuntimeError(CANARY_EXC))
    )
    c.post("/v1/search", json={"query": CANARY_QUERY})
    monkeypatch.setattr(app_mod, "retrieve_search", MagicSearch())
    c.post("/v1/search", json={"query": CANARY_QUERY})
    blob = _metric_text(metric_reader)
    for canary in CANARIES:
        assert canary not in blob, f"{canary} leaked into metric labels"
    assert _metric_label_keys(metric_reader) <= FINITE_METRIC_LABELS


def test_exception_frames_carry_no_message_or_source():
    try:
        raise ValueError(CANARY_EXC)
    except ValueError:
        import sys

        line = JsonFormatter().format(
            logging.makeLogRecord(
                {"msg": "failed", "levelname": "ERROR", "name": "t", "exc_info": sys.exc_info()}
            )
        )
    payload = json.loads(line)
    assert payload["error_type"] == "ValueError"
    assert CANARY_EXC not in line
    assert "test_telemetry_privacy.py" in payload["trace"]


def test_success_and_failure_traces_join_logs_to_spans(client, caplog):
    """One synthetic success and one failure end to end through the real
    SDK exporter: log lines join their spans, terminal states are exact."""
    c, exporter = client
    with caplog.at_level(logging.INFO):
        ok_resp = c.post("/v1/search", json={"query": CANARY_QUERY})
    assert ok_resp.status_code == 200
    ok_spans = [s for s in exporter.get_finished_spans() if s.name == "v1.search"]
    assert len(ok_spans) == 1
    assert ok_spans[0].status.status_code == trace.StatusCode.UNSET
    joined = [
        line
        for line in _formatted_logs(caplog)
        if ok_resp.json()["request_id"] in line
    ]
    assert joined, "request_id must join logs to their trace"
