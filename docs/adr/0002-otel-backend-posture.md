# ADR-0002: Observability backend posture (single-replica Jaeger, sample-all)

- **Status:** accepted
- **Context:** OTel Phases 1–3 shipped RED metrics, W3C ingress, deploy
  identity, and a smoke check that spans land. Remaining open questions:
  is the single-replica Jaeger backend production-grade, is head sampling
  at 1.0 sustainable, and do metrics need exemplar links into traces.
- **Decision:** Jaeger stays single-replica debug-grade (traces are 14-day
  debug data, not records); head sampling stays 1.0 until the revisit
  trigger below fires; Prometheus exemplars stay deferred — the
  `request_id` JSON-log pivot links metrics to traces.
- **Consequences / rules:**
  - No Jaeger HA work: badger is single-writer local disk on an RWO block
    PVC, so a second replica cannot share the store — HA would mean
    replacing the backend, not scaling it. Outage cost is bounded to
    recent-trace visibility (export is fail-open; requests never block).
  - UI stays port-forward + cluster RBAC; no Route, no extra auth layer.
    No ServiceMonitor on Jaeger itself — product alerting rides the agent
    RED instruments, not backend self-telemetry.
  - Retention math (re-check against Jaeger storage metrics if volume
    changes): 10Gi PVC with a 336h span TTL sustains ~730MB/day. At a
    conservative ~2KB/span upper bound (query text capped at
    `query_max_chars=2000`) that is ~480k spans/day ≈ ~60k answer-traces
    (~8 spans) per day at ratio 1.0. Revisit trigger: sustained volume
    above ~50k requests/day — then lower `otel_sample_ratio`, not the TTL.
  - Exemplars: the OTel Python Prometheus exporter has no exemplar path,
    so there is nothing to wire; `request_id` is present on every log line,
    span, and metric-adjacent record, which is the supported pivot.
  - Superseding any of these (backend replacement, default sample-ratio
    change, exemplar support) requires a new ADR; a sample-ratio default
    change additionally splits into its own PR per repo rule.
