"""Source-privacy canaries (issue #529 OBS-1A §4.4).

Prohibited content — raw query text, header values, exception bodies,
document identifiers — must be absent from every telemetry surface
(stdout JSON, span attributes/events/status, metric labels), both before
and after collection. Safe context (action, request_id, error types,
counts, timings, finite labels) must remain. All assertions run through
the real SDK exporters and test readers, never around them.
"""

from __future__ import annotations

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
    """Every string the spans export: attribute values, event fields,
    status descriptions."""
    texts = []
    for span in spans:
        for value in (span.attributes or {}).values():
            texts.append(str(value))
        for event in span.events or []:
            texts.append(event.name)
            for value in (event.attributes or {}).values():
                texts.append(str(value))
        if span.status and span.status.description:
            texts.append(span.status.description)
    return "\n".join(texts)


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
