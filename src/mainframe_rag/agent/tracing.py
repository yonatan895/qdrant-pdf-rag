"""OpenTelemetry tracing setup (issue #83).

One module owns tracer wiring so the enable/disable decision and the exporter
lifecycle have exactly one implementation. Design rules:

- Default OFF: with no OTEL_EXPORTER_OTLP_ENDPOINT, `get_tracer` returns a
  no-op tracer (the global proxy tracer) and every span call site costs one
  context-manager call.
- Enabled: the agent's lifespan builds a TracerProvider + BatchSpanProcessor
  (bounded queue — a dead collector must not grow the heap) and lifespan
  shutdown forces a final flush. Export failures log and drop; tracing is
  never on the response path.
- OTLP/HTTP only (no grpcio in the air-gap wheelhouse). The endpoint is the
  collector origin; the exporter appends /v1/traces itself.
- Service name comes from OTEL_SERVICE_NAME when the operator sets it,
  defaulting to "mainframe-rag-agent".

Span attribute discipline mirrors logs.py: ids, counts, scores, timings.
Query text is the one deliberate exception, bounded by the caller (the
agent already caps it at query_max_chars). Never PDF/manual text, never
secrets.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

log = logging.getLogger("otel")

DEFAULT_SERVICE_NAME: Final = "mainframe-rag-agent"
DEFAULT_OTLP_TRACES_PATH: Final = "/v1/traces"

_provider: TracerProvider | None = None


def trace_enabled(endpoint: str | None) -> bool:
    """Enabled iff an endpoint is configured. One rule, shared by lifespan
    setup and tests."""
    return bool(endpoint and endpoint.strip())


def setup_tracing(endpoint: str | None, sample_ratio: float = 1.0) -> trace.Tracer:
    """Build the tracer for the agent's lifespan. Idempotent: the second call
    returns a tracer on the same provider (a redeploy of config within one
    process must not stack exporters).

    sample_ratio is the head sampler ratio — 0.0 disables span production
    entirely, 1.0 keeps every trace (the default; this service's request
    volume is tiny).
    """
    global _provider
    if not trace_enabled(endpoint):
        return trace.get_tracer("mainframe-rag")
    if _provider is not None:
        return _provider.get_tracer("mainframe-rag")

    service_name = os.environ.get("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME)
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
    )
    assert endpoint is not None
    exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + DEFAULT_OTLP_TRACES_PATH)
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            max_queue_size=2048,
            export_timeout_millis=5000,
            max_export_batch_size=512,
        )
    )
    _provider = provider
    # Register globally: every module-level proxy tracer (retrieve.query's
    # stage tracer was created at import time, before the provider existed)
    # upgrades to the real provider at its next use. Without this, only the
    # agent's own tracer spans — the retrieval stages silently stay no-ops.
    # Never called twice for one process (the API refuses a second set).
    try:
        trace.set_tracer_provider(provider)
    except Exception as exc:  # noqa: BLE001
        log.warning("otel global tracer provider already set: %s", exc)
    log.info("otel tracing enabled: endpoint=%s service=%s", endpoint, service_name)
    return provider.get_tracer("mainframe-rag")


def shutdown_tracing() -> None:
    """Lifespan shutdown: flush the batch queue. Bounded by the exporter's
    own timeout; a wedged collector cannot hang shutdown forever. Errors are
    swallowed — telemetry must not break the shutdown path."""
    global _provider
    if _provider is None:
        return
    try:
        _provider.force_flush(timeout_millis=5000)
        _provider.shutdown()
    except Exception as exc:  # noqa: BLE001
        log.warning("otel flush on shutdown failed: %s", exc)
    _provider = None
