# Observability and data boundaries

Contract owner for what the system collects, where each signal lives, how
long it is kept and how it is analyzed. Owners: #529 (observability backend and
signals), #780 (answer feedback), #782 (backend qualification). Decisions:
[ADR-0002](adr/0002-otel-backend-posture.md) (Jaeger posture; replacing the
backend needs a superseding ADR) and
[ADR-0005](adr/0005-answer-feedback-data-boundary.md) (content only in the
feedback store). Maintainer decisions of 2026-10-17 unless noted.

## Data tiers

| Tier | Signals and store | Content | Retention | Used for |
|---|---|---|---|---|
| 1. Operational telemetry | `/metrics` (Prometheus exposition); OTLP traces; sanitized JSON logs on stdout | **Never** question, answer or excerpt text (OBS-1A, #554) | Traces 14 days (bundled Jaeger Badger TTL); logs per the platform's Splunk policy | Latency, failures, root-cause analysis |
| 2. Quality records | #780 feedback store (SQLite, protected PVC) | **The only runtime home** of question and answer text, plus ratings and comments. Each record carries its own telemetry snapshot, so quality analysis outlives trace expiry | 180 days, explicit purge | Rated-answer analysis |
| 3. Evaluation | Private golden sets and eval reports (outside git) | Manual-derived text | Manual | #367 metrics; adjudicated `bad` ratings feed it (#780 phase 3) |

**Join key.** `request_id`, the agent's server-minted 12-hex id, is the only
link between tiers. Within trace and log retention, a rated answer leads to its
trace and logs.

**Never.** Question, answer, excerpt or comment text in tier 1. That rules out
putting it into a ClickHouse-backed telemetry store "for analysis", because it
would widen access to manual text beyond the protected feedback volume.

## Display and analysis

- **Rating metrics:** `rag.feedback.ratings` (rating, reason, endpoint,
  verification state) and `rag.feedback.records` (recorded, duplicate, dropped,
  failed) on the existing `/metrics`. Bounded labels only, never ids or text.
  Shown beside the request, latency and admission panels.
- **Periodic analysis report** (#780 phase 2), from a read-only export of the
  feedback store: failure clusters, candidate golden cases, and `request_id`
  links to traces.
- **Deferred:** an admin UI over the feedback store, until the report proves
  insufficient.

## Backend status

| Component | Status |
|---|---|
| Traces | Bundled Jaeger all-in-one with Badger on an RWO PVC, 14-day TTL. Remains the development and CI backend (one small pod) whatever production chooses. |
| Metrics, dashboards, alerts | Prometheus exposition; the chart renders a `ServiceMonitor` for OpenShift user-workload monitoring when `metrics.enabled`. |
| Logs | Sanitized JSON on stdout, through the platform's log forwarding to Splunk. |
| Single-pane backend | **Under evaluation (#782): OpenObserve local mode** (single binary, local volume; AGPL-3.0 OSS edition first; no modification of Enterprise behaviour or limits). Baseline if it does not qualify: Jaeger, user-workload monitoring and Splunk, with no new stateful service. |
| SigNoz | Deprioritized: ClickHouse plus Keeper operations, version migrations, `restricted-v2` admission risk, SSO in paid editions, too heavy for CI. Reconsider only if the platform operates it. |

Retiring the bundled Jaeger in production (whatever replaces it) first requires
backend-neutral replacements for the two Jaeger-specific checks: the
trace-arrival query in `scripts/airgap/smoke.sh` and `check_live.py`'s
`fresh_search_trace`.

## Feedback store backend

SQLite in a single-replica service with its own RWO block PVC (never NFS).
The store keeps a narrow interface (`record_answer`, `add_rating`, `answer`,
`ratings`, `purge`) and portable SQL (`ON CONFLICT DO NOTHING`, JSON as text),
so a later move to the platform's Crunchy Postgres is a backend swap plus a
tested export and import. Triggers for that move: an availability requirement
for feedback (the single replica drops records best-effort during a
reschedule), multiple writers, live concurrent analytics, or a platform mandate.
