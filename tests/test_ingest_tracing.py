"""Ingest tracing (issue #83): parent-process spans only.

Hermetic: setup_tracing/shutdown_tracing and the ingest body are faked — no
Qdrant, no PDFs, no collector. Spans are asserted on name and status; the
worker-side untraced contract is documented in AGENTS.md and architecture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mainframe_rag.ingest import run_ingest as ri


@pytest.fixture
def traced(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    seen: dict = {}

    def fake_setup(endpoint, sample_ratio=1.0, export_queue_size=2048,
                   export_timeout_ms=5000, service_name=None):
        seen["endpoint"] = endpoint
        seen["service_name"] = service_name
        if not endpoint or not endpoint.strip():
            from opentelemetry import trace as otel_trace

            return otel_trace.NoOpTracerProvider().get_tracer("test")
        return provider.get_tracer("test")

    monkeypatch.setattr(ri, "setup_tracing", fake_setup)
    monkeypatch.setattr(ri, "shutdown_tracing", lambda: seen.__setitem__("shutdown", True))
    monkeypatch.setattr(ri, "_run_impl", lambda *a, **k: 0)
    return exporter, seen


@pytest.mark.parametrize(
    ("env_name", "expected"),
    [(None, "mainframe-rag-ingest"), ("custom-svc", "custom-svc")],
)
def test_ingest_roots_and_service_name(monkeypatch, traced, env_name, expected):
    """OTEL_SERVICE_NAME wins; the fallback is the ingest service, never the
    agent default (one Jaeger must not merge the two)."""
    exporter, seen = traced
    if env_name is None:
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    else:
        monkeypatch.setenv("OTEL_SERVICE_NAME", env_name)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    rc = ri.run(Path("/corpus"), Path("/tmp/progress.jsonl"), None, None, False)
    assert rc == 0
    assert seen["service_name"] == expected
    assert seen["shutdown"] is True
    assert [s.name for s in exporter.get_finished_spans()] == ["ingest.run"]


def test_ingest_error_marks_root_span(monkeypatch, traced):
    exporter, seen = traced

    def boom(*a, **k):
        raise RuntimeError("bad corpus")

    monkeypatch.setattr(ri, "_run_impl", boom)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    with pytest.raises(RuntimeError):
        ri.run(Path("/corpus"), Path("/tmp/p.jsonl"), None, None, False)
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["ingest.error_type"] == "RuntimeError"
    assert seen["shutdown"] is True


def test_ingest_run_is_tracing_noop_when_disabled(monkeypatch, traced):
    # No endpoint: setup receives None, the wrapper still roots a (non-
    # recording) span, and nothing is exported.
    exporter, seen = traced
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    rc = ri.run(Path("/corpus"), Path("/tmp/p.jsonl"), None, None, False)
    assert rc == 0
    assert seen["endpoint"] is None
    assert not exporter.get_finished_spans()
