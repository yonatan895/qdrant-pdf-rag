# ADR-0006: OpenObserve OSS as the traces and metrics backend

- **Status:** accepted (maintainer, 2026-10-23; #782). Supersedes the backend
  and retention rules of [ADR-0002](0002-otel-backend-posture.md) for traces and
  metrics. Amended 2026-10-23 (#956): OpenObserve is the only trace backend,
  in dev, CI and the local stack too, pinned at v1.1.2.
- **Context:** ADR-0002 keeps single-replica, debug-grade Jaeger and requires a
  new ADR to replace it. #782 qualified single-node OpenObserve OSS v1.0.4 (local
  mode) against Jaeger + user-workload monitoring (UWM) + Splunk. Basis: the
  [2026-10-23 record](../records/2026-10-23-openobserve-oss-qualification.md),
  with criteria approved 2026-10-18 (D1-D10).
  All 7 gates passed (data boundary with 0 canary hits in the scanned `default` streams and container logs; `_meta` not scanned, security, air-gap
  packaging, kill -9 durability, 13/13 trace-operation parity, 1 GiB cap with no
  OOM, fail-open); scored S1, S4, S6 and S8 passed (peak 519 MiB sampled, about
  90 bytes per span, about 42 MiB for 480,975 spans, exporter overhead within
  noise); S7 disk-full failed on visibility; S3 (logs) not run by design;
  Phase B (Kind admission) not run. The maintainer accepted the G2 and G3
  readings below and the AGPL-3.0 licence (D7).
- **Decision:** the supported deployment uses single-node OpenObserve OSS as its
  traces and metrics backend, replacing Jaeger and the UWM metrics path.
  Logs stay in the platform's Splunk (D2/D3). The OpenTelemetry
  trace exporter and the `/metrics` exposition are unchanged. ADR-0005 holds:
  no question, answer, excerpt or comment text in any telemetry backend.
- **Amendment, 2026-10-23 (#956, maintainer):** OpenObserve replaces Jaeger
  everywhere, including development, CI, the Kind lanes and the local stack; there
  is no `tracing.backend` selector. The pin is v1.1.2
  (`sha256:4a154849432f75c4fb024de32fe34163c02406c905dab22bf8d213d2e3386392`,
  index), subject to local re-qualification before merge (G2, G3, G4, G5, G6, S6,
  S7 and a 1.0.4 to 1.1.2 upgrade of a copy of the trial volume). Any regression
  falls the pin back to v1.0.4. Reason: v1.1.1 made the metrics index and WAL
  packing always on and removed `ZO_METRICS_INDEX_ENABLED` and
  `ZO_FEATURE_WAL_PACK_ENABLED`, which changes the write path the S7 and
  durability evidence covered. The trial figures below are v1.0.4.
- **Amendment, 2026-10-23 (#956): v1.1.2 re-qualified, adopted.** The local
  re-run ([#956 comment](https://github.com/yonatan895/qdrant-pdf-rag/issues/956#issuecomment-6373675369))
  passed G2 (identical auth behaviour), G3 (0 egress attempts with the three
  `ZO_*` settings; the new `ZO_USAGE_REPORTING_URL` and `ZO_TELEMETRY_URL` made
  none), G4, G5 (13/13 operations, 100/100 lookups, J2 70=70), G6 (peak 163 MiB)
  and a 1.0.4 to 1.1.2 upgrade of a copy of the trial volume (identical query
  battery, schema migrated 77 to 97, old traces 100/100, new traces 30/30). S6
  was inconclusive at that scale. The pin stays v1.1.2; no fall-back to v1.0.4.
  - **Upgrade gap closed:** the volume upgrade is no longer untested; it is the
    1.0.4 to 1.1.2 run above. A later release still needs its own pin-bump run.
  - **kill -9 trade-off:** the 107 of 107 spans present before the kill survive,
    but spans from roughly the last 6 s before the kill were not visible (4 of
    119). Traces accept losing about the last 6 s on a hard kill.
  - **S7 is unchanged:** 400 acknowledged, 159 stored on v1.1.2 (161 on v1.0.4),
    still silent loss with HTTP 200. Mitigation: headroom plus a required
    site-side alert (finding below).
  - **Disk-full signal finding (2026-10-23):** OpenObserve OSS has alerting
    (`/api/{org}/{stream}/alerts`, SQL or PromQL over ingested streams) and its
    `/metrics` exposes `zo_node_disk_total` and `zo_node_disk_usage`. Run
    against the v1.1.2 binary with the data directory on a 128 MiB tmpfs and 60
    MiB written there, both series stayed at the host root filesystem figures
    and did not move: they do not measure the data volume. The built-in
    `ZO_DISK_CIRCUIT_BREAKER_ENABLED=true` was also tested (same method;
    threshold 50 with the tmpfs at 68%, and thresholds 30 and 0.3 with the root
    filesystem near 41%): JSON ingest kept returning HTTP 200 and no request
    was refused, so it gives no visible refusal. Decision (maintainer): no
    PrometheusRule and no in-app signal. S7 stays silent loss at the
    product level, mitigated by headroom and a required site-side alert on the
    platform's kubelet volume metrics for `openobserve-data`
    ([deploy](../deploy.md#trace-backend)).
- **Required configuration (the supported deployment must set all of these):**
  - `ZO_TELEMETRY=false`, `ZO_MODEL_PRICING_ENABLED=false` and
    `ZO_MMDB_DISABLE_DOWNLOAD=true`. With `ZO_TELEMETRY=false` alone, the default
    configuration still attempted outbound fetches (`geoip.zinclabs.dev`,
    `raw.githubusercontent.com`) within 45 s. This is the accepted G3 reading.
  - Image pinned by digest (trial: v1.0.4,
    `sha256:d4a878fac1f6c56003764f7f2a1625668917388f167e222c8c810de3f54c56ba`,
    amd64); no tag references, no runtime downloads.
  - No external egress: the three `ZO_*` settings above, plus an egress-deny
    NetworkPolicy (maintainer decision, [#956 comment](https://github.com/yonatan895/qdrant-pdf-rag/issues/956#issuecomment-6369025102)) so the pod cannot reach a public destination even if a setting
    is missed. The names of the two egress settings were found in the binary, not
    vendor documentation; verify them when the pin is made.
  - Credentials from a Secret; the console, query and ingest paths all require
    them. Four endpoints answer without credentials (`/healthz`, `/config`, the
    UI static files, `/metrics`; accepted G2 reading). `ZO_PROMETHEUS_ENABLED=false`
    empties `/metrics` and is optional.
  - Single writer on an RWO block volume (replicas 1, Recreate), as for Jaeger.
    Local mode only; HA mode (object storage, PostgreSQL, NATS) is out of scope.
- **Consequences / rules:**
  - ADR-0002's Jaeger, no-HA and retention-math rules are replaced for traces
    and metrics. Still in force: head sampling at 1.0 until its revisit trigger,
    no Prometheus exemplars (`request_id` is the pivot), the 14-day trace
    retention target, fail-open export, and ADR-0002's UI-access rule, applied
    to the OpenObserve console: ClusterIP only, reached by port-forward with
    cluster RBAC; no Route and no extra auth layer.
  - Volume sizing: about 90 bytes per span puts 14 days at the ADR-0002 volume
    bound near 0.6 GB (6% of 10Gi; 30% with a 5x margin for less repetitive
    real spans). Re-measure on the site.
  - Metrics need a scraper or remote write into OpenObserve (the trial used
    otelcol-contrib 0.160.0, stateless); the agent has no OTLP metrics exporter.
    The scraping or forwarding path is a site decision.
  - **Known failure, S7 disk full:** writes return HTTP 200 while records are
    silently dropped (161 of 400 stored); no corruption, health stays 200, and
    ingest and queries recover after space is freed and a restart. The deployment
    needs a mitigation: volume-usage alerting and headroom well below the
    retention-sized budget.
  - **Not run:** Phase B (`kind load` failed with "failed to detect containerd
    snapshotter"), the upgrade of a volume by a release later than v1.1.2, and
    logs (S3). The 1.0.4 to 1.1.2 upgrade ran and closed that gap (amendment
    above).
    Not tested: SSO, RBAC and audit gaps in OSS.
  - AGPL-3.0 is accepted for the unmodified upstream binary as a separate
    service ([licensing](../licensing.md)). No modification of Enterprise
    behaviour or limits.
  - **Open site checks before production use:** `restricted-v2` SCC and Pod
    Security admission; real namespace quota; platform forwarding of metrics and
    traces; the kubelet-volume-stats alert on `openobserve-data` above 70% used (#807); a 14-day soak; Enterprise offline licence answers (issue, renewal,
    50 GB/day enforcement, expiry behaviour); disk-full behaviour on the site volume.
  - Dashboards and alerts are later work under #529, using the M1-M6 PromQL
    (ran unmodified except M2, not numerically verified) and the J1-J3 queries
    in [metric-contract](../metric-contract.md). The disk-full alert follows the
    re-qualification, because S7 behaviour may differ on v1.1.2.
  - Revisit trigger: any open site check failing, or Enterprise licensing
    constraints that block the supported deployment.
