"""OpenTelemetry metrics setup (issue #187).

One module owns meter wiring so the enable decision has exactly one
implementation. Design rules (mirroring tracing.py):

- Default OFF: `GET /metrics` 404s unless `metrics_enabled` is set. No
  instruments are registered yet (WS3 adds RED/domain instruments); the
  endpoint initially exposes process/runtime defaults only, proving the
  UWM scrape pipeline end to end.
- Pull model: a process-global `PrometheusMetricReader` serves scrapes from
  the prometheus_client REGISTRY. No exporter lifecycle to flush, so
  lifespan has no shutdown step — the provider lives for the process.
  Setup is idempotent (second call returns the existing provider) so
  repeated lifespans in one process can never double-register collectors.
- Single uvicorn worker only: the OTel Prometheus exporter documents no
  multiprocessing support; parallel scrape writers would corrupt counters.

Cardinality law (standing): instrument labels come from bounded enums
only (endpoint, query_class, outcome, model). Never doc_id, query text,
headings, or any other unbounded value as a label.
"""

from __future__ import annotations

import logging

from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import GCCollector, PlatformCollector, ProcessCollector

log = logging.getLogger("otel.metrics")

_provider: MeterProvider | None = None
_platform_collectors_registered = False


def setup_metrics(enabled: bool) -> MeterProvider | None:
    """Build the process-global meter provider. Idempotent: the second call
    returns the existing provider (re-registering the reader's collector
    would raise, and WS3 instruments must survive lifespan re-entry in
    tests). Returns None when disabled — the /metrics route 404s."""
    global _provider, _platform_collectors_registered
    if not enabled:
        return None
    if _provider is not None:
        return _provider
    provider = MeterProvider(metric_readers=[PrometheusMetricReader()])
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
    log.info("otel metrics enabled: prometheus exposition on /metrics")
    return provider
