"""OpenTelemetry metrics setup (issue #187) + RED/domain instruments (Phase 1).

One module owns meter wiring so the enable decision has exactly one
implementation. Design rules (mirroring tracing.py):

- Default OFF: `GET /metrics` 404s unless `metrics_enabled` is set. With it
  on, the endpoint exposes process/runtime defaults plus the RED/domain
  instruments below, proving the UWM scrape pipeline end to end.
- Pull model: a process-global `PrometheusMetricReader` serves scrapes from
  the prometheus_client REGISTRY. No exporter lifecycle to flush, so
  lifespan has no shutdown step — the provider lives for the process.
  Setup is idempotent (second call returns the existing provider) so
  repeated lifespans in one process can never double-register collectors.
- Single uvicorn worker only: the OTel Prometheus exporter documents no
  multiprocessing support; parallel scrape writers would corrupt counters.
- Fail-open: record_request never raises — telemetry must not break the
  request path it observes.

Cardinality law (standing): instrument labels come from bounded enums
only (endpoint, query_class, outcome, model). Never doc_id, query text,
headings, or any other unbounded value as a label.

Instruments (all `rag.` prefixed; Prometheus renders dots as underscores):
- rag.requests.total (counter): endpoint x query_class x outcome. The
  countable signal for alerts (replaces worker-unsafe in-process counters).
- rag.request.duration (histogram, seconds): endpoint x query_class x
  outcome. Buckets cover the 300s answer tail (`answer_timeout_s`).
- rag.retrieval.hits (histogram, count): endpoint x query_class.
- rag.llm.ttft (histogram, milliseconds): model only. Recorded only when
  the leg measured it (None means unmeasured, not zero).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.metrics import Counter, Histogram, Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from prometheus_client import GCCollector, PlatformCollector, ProcessCollector

log = logging.getLogger("otel.metrics")

# Bucket boundaries are instrument-shape constants (not timeouts/limits):
# duration spans embed-ms search to the 300s answer tail; TTFT spans
# in-GPU first tokens to cold-start outliers; hits spans empty to the
# 40-hit endpoint cap.
DURATION_BOUNDARIES_S: tuple[float, ...] = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
)
TTFT_BOUNDARIES_MS: tuple[float, ...] = (
    10.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0, 30000.0, 60000.0,
)
HITS_BOUNDARIES: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 40.0)

# The only endpoints RED instruments observe. Anything else (scrapes,
# healthz, unknown paths) must not pollute request series.
_METRIC_ENDPOINTS = {"search": "/v1/search", "answer": "/v1/answer"}

_provider: MeterProvider | None = None
_platform_collectors_registered = False


@dataclass(frozen=True)
class Instruments:
    """The RED/domain instrument set. Built once per provider; tests build
    their own against an InMemoryMetricReader without touching globals."""

    requests: Counter
    duration: Histogram
    hits: Histogram
    ttft: Histogram


_instruments: Instruments | None = None


def _views() -> list[View]:
    return [
        View(
            instrument_name="rag.request.duration",
            aggregation=ExplicitBucketHistogramAggregation(DURATION_BOUNDARIES_S),
        ),
        View(
            instrument_name="rag.llm.ttft",
            aggregation=ExplicitBucketHistogramAggregation(TTFT_BOUNDARIES_MS),
        ),
        View(
            instrument_name="rag.retrieval.hits",
            aggregation=ExplicitBucketHistogramAggregation(HITS_BOUNDARIES),
        ),
    ]


def create_instruments(meter: Meter | None = None) -> Instruments:
    """Build the instrument set on the given meter (the global one by
    default). One constructor so production and tests register the exact
    same names, units, and label keys."""
    meter = meter if meter is not None else metrics.get_meter("mainframe-rag")
    return Instruments(
        requests=meter.create_counter(
            "rag.requests.total",
            description="Agent requests by endpoint, query class, and outcome",
            unit="1",
        ),
        duration=meter.create_histogram(
            "rag.request.duration",
            description="Agent request latency in seconds",
            unit="s",
        ),
        hits=meter.create_histogram(
            "rag.retrieval.hits",
            description="Retrieved hits per request",
            unit="1",
        ),
        ttft=meter.create_histogram(
            "rag.llm.ttft",
            description="Reasoning-model time to first token in milliseconds",
            unit="ms",
        ),
    )


def endpoint_for_path(path: str) -> str | None:
    """Map a request path to its RED endpoint label, or None when the path
    is not a product endpoint (scrapes, probes, and unknown routes stay out
    of request series). One mapping, shared by every error handler."""
    clean = path.rstrip("/") or "/"
    for endpoint, route in _METRIC_ENDPOINTS.items():
        if clean == route:
            return endpoint
    return None


def record_request(
    endpoint: str,
    outcome: str,
    *,
    query_class: str = "unknown",
    elapsed_s: float = 0.0,
    hits: int | None = None,
    ttft_ms: int | None = None,
    llm_model: str | None = None,
) -> None:
    """Record one finished request. No-op unless setup_metrics enabled the
    provider. hits/ttft are recorded only when measured (None means the leg
    never ran — never record a zero for unmeasured work). Fail-open: never
    raises, so a metrics fault cannot break the request it observes."""
    instruments = _instruments
    if instruments is None or endpoint not in _METRIC_ENDPOINTS:
        return
    try:
        base = {"endpoint": endpoint, "query_class": query_class, "outcome": outcome}
        instruments.requests.add(1, base)
        instruments.duration.record(max(elapsed_s, 0.0), base)
        if hits is not None:
            instruments.hits.record(
                max(hits, 0), {"endpoint": endpoint, "query_class": query_class}
            )
        if ttft_ms is not None and llm_model is not None:
            instruments.ttft.record(max(ttft_ms, 0), {"model": llm_model})
    except Exception as exc:  # noqa: BLE001
        log.debug("otel record_request dropped: %s", exc)


def setup_metrics(enabled: bool) -> MeterProvider | None:
    """Build the process-global meter provider. Idempotent: the second call
    returns the existing provider (re-registering the reader's collector
    would raise, and WS3 instruments must survive lifespan re-entry in
    tests). Returns None when disabled — the /metrics route 404s."""
    global _provider, _platform_collectors_registered, _instruments
    if not enabled:
        return None
    if _provider is not None:
        return _provider
    provider = MeterProvider(metric_readers=[PrometheusMetricReader()], views=_views())
    try:
        metrics.set_meter_provider(provider)
    except Exception as exc:  # noqa: BLE001
        log.warning("otel global meter provider already set: %s", exc)
    if not _platform_collectors_registered:
        # Runtime defaults for the scrape: GC, interpreter, and process
        # stats. Bounded, no labels — no cardinality risk. Tolerant of
        # pre-registration: recent prometheus_client versions auto-register
        # these on import, and re-registering raises.
        for collector in (GCCollector, PlatformCollector, ProcessCollector):
            try:
                collector()
            except ValueError:
                log.debug("otel platform collector already registered: %s", collector.__name__)
        _platform_collectors_registered = True
    _provider = provider
    _instruments = create_instruments(provider.get_meter("mainframe-rag"))
    log.info("otel metrics enabled: prometheus exposition on /metrics")
    return provider
