# OpenObserve OSS qualification trial (record)

Status: dated record for issue #782 (design and decisions D1-D10 in the issue). Run 2026-10-23 on base `65ffeb0`.
Read-only for the repository: no image pin, chart, values, `images.txt`, Taskfile or deployment change.
Counts, bytes, percentiles and times only: no raw telemetry, host paths or credentials. Evidence stays outside git.

## Verdict

**GO, conditional** (recommend a superseding ADR for ADR-0002 and the pin PR; no deployment change from #782).

- All seven gates passed, two of them on readings the maintainer accepted on 2026-10-23 (G2: four unauthenticated non-data
  endpoints; G3: the default config tries to reach two hosts and the trial disabled both by environment settings).
  If either reading is rejected, the verdict is keep-baseline.
- Scored items required for a go (S1, S4, S6, S8) passed; S9 holds (net new stateful services +0, collector stateless).
- S7 (disk-full behaviour) did not pass; it is not a go criterion but is a real operating risk (below).
- Phase B (Kind admission) is NOT RUN (tooling failure, below). The open site checks stay open.
- Any ADR must keep ADR-0005 (no content in telemetry); the trial found no canary anywhere.

## Identity

- Repo base `65ffeb0` (main). Run 2026-10-23 15:59 to 17:07 IDT (68 min, scale 1.0; the 2 to 2.5 h design estimate was long).
- Images, by digest, pulled on the connected dev host only (D4): OpenObserve v1.0.4
  `sha256:d4a878fac1f6c56003764f7f2a1625668917388f167e222c8c810de3f54c56ba` (amd64, built 2026-09-24, 29 days old; the
  registries `public.ecr.aws/zinclabs` and `docker.io/openobserve` returned the same digest); otelcol-contrib 0.160.0
  `sha256:799dc6cf12c96192af37b5bdba804da8c10b3bc563b43cb90c3f3c58d9572ad6` (built 2026-09-02). Jaeger comparison: the
  `images.txt` digest `46a88626...`.
- Host: 16 GB WSL; at start MemAvailable 10.4 GB, PSI memory and io near 0, 601 GB disk free; minimum MemAvailable during
  the run 9.4 GB; no watchdog abort. Heavy lock held for the whole run.
- Workload: Qdrant sim (1 GiB cap), `mock_vllm` (TTFT 300 ms, 20 ms/token, jitter 50 ms, seed 782, 2% error rate; mock
  embeddings, no hash mode, D10), 4 synthetic PDFs (12 points), the real agent unchanged (traces direct to O2, `/metrics`
  scraped by the bridge). 480,975 spans and 26,440 metric samples (25 series names) were stored.

## Deviations from the approved design

1. **Logs excluded** (D2/D3): S3 NOT RUN; G1 covers the `default` org's trace, metrics and log streams and the O2 and bridge container logs; the `_meta` org was listed but its streams were not scanned.
2. **Host cannot reach an `--internal` Docker network** on this host (the daemon is not in the WSL network namespace), so
   the design assumption failed. A 128 MiB relay container (python on the `kindest/node` image, on the default bridge and the
   internal network) publishes O2 on host loopback; the bridge is also attached to both networks. O2 itself stays on the internal
   network with no route out.
3. **Search load was paced** at 20 rps (10 rps per concurrency unit; 80 rps in the burst). The closed-loop `loadtest.py` drives the
   agent to about 190 rps (about 1.3k spans/s), far above the 100k-span target. Answers use `loadtest.py` (c=2, burst c=8). P1 stored
   178,677 spans (target at least 100k).
4. **O2 needed a password with a special character**, and two non-default settings, `ZO_MODEL_PRICING_ENABLED=false` and
   `ZO_MMDB_DISABLE_DOWNLOAD=true`, to stop outbound fetches (G3). The names were found in the binary; the vendor documentation was
   not checked (no network lookups in the trial).
5. **Collector needed no workaround for scraping** once the agent bound `0.0.0.0` (the collector container reaches it through the
   Docker host gateway). The first attempt, with a host-network collector, could not reach the agent.
6. **J1-J3 text**: the shipped Jaeger URL templates are no longer in `tests/test_metric_contract.py` (retained only in git history,
   `40f25bf`); they were rebuilt from `docs/metric-contract.md` (by `http.request_id`; `v1.answer` slower than 500 ms; `otel.status_code=ERROR`).
7. **Phase B NOT RUN.** `kind load docker-image` and `kind load image-archive` both failed with "failed to detect containerd
   snapshotter" against `kindest/node:v1.31.14` on this Docker (the images are stored in the containerd image store). Two attempts
   (the allowed one rerun); no workaround was improvised. The cluster `o2trial782` was created and deleted both times.
8. The Jaeger comparison ran the image's in-memory store capped at 20,000 traces, not the production Badger store, so its footprint is a lower bound.
9. A maintainer-requested 15-minute UI viewing window was blocked by the permission classifier for the agent; the maintainer
   copied the stopped data volume into the unlabelled volume `o2view-data` after the last measurement and runs any viewer after
   teardown. That window is outside the measured trial and does not affect any result. `o2view-data` was not removed.

## Gates

| ID | Observed | Result |
|---|---|---|
| G1 data boundary | 20 canary requests (query, header, upstream-exception body, doc id; 16 x 200, 4 x 502). The `default` org's streams (1 traces, 25 metrics, 1 logs; 3 `rag_*` metric names returned "stream not found") and the O2 and bridge container logs were downloaded and scanned locally: 0 hits. The `_meta` org was listed but not scanned (ADR-0005 coverage is for the scanned streams). Negative control stream: all 6 tokens found. O2 and collector container logs: 0 hits. Data volume files: 0 files contain a token. Server-side `match_all` cross-check on traces returned an HTTP error (not counted) | PASS |
| G2 security | Ingest (traces, metrics, json), search, streams, Prometheus query, orgs, users, settings: 401 without credentials; wrong credentials 401. Unauthenticated 200: `/healthz`, `/config` (build info and a telemetry flag), `/web/` static UI, `/metrics` (26 process-level series, no stream or org names; empty body with `ZO_PROMETHEUS_ENABLED=false`). No published ports. Ran, ingested and restarted as UID 1000770000 GID 0, read-only rootfs, `--cap-drop ALL`, no-new-privileges, `/tmp` tmpfs, writable data volume only (fsGroup stand-in) | PASS, with the four named public endpoints |
| G3 air-gap | Digest-pinned amd64, 29 days old. Static UI: no external `src` or `href` or CSS `url()` (JS bundles contain literal documentation and vendor URLs, never fetched). Default config with `ZO_TELEMETRY=false` still tried `geoip.zinclabs.dev` and `raw.githubusercontent.com` within 45 s; with the two settings in deviation 4, the O2 log shows 0 outbound attempts for the whole run. Kernel-level counters were not observable (the container namespace is not visible); evidence is the log plus the internal network, which blocks all egress | PASS, with the extra settings |
| G4 durability | `kill -9` under load (search 40 rps plus a tracker): restart ready in 2.3 s, no manual repair, 224 of 224 pre-kill tracked requests present (unflushed window 0 s), next ordinary request visible 6.1 s after sending | PASS |
| G5 trace parity | 13 of 13 operations present service-wide in both backends. 100 of 100 request ids returned exactly one trace in both; spans per trace identical (search 6 or 7, answer 8 or 9; same distribution in Jaeger and O2). Steady-state export errors in agent logs: 0 (errors only in the G7 stop window and the G4 kill window) | PASS |
| G6 memory cap | cgroup peak after the last restart 206 MiB; highest sampled 519 MiB (during the `g5_s2` trace-query phase); 1 GiB cap, OOMKilled false throughout | PASS |
| G7 fail-open | O2 stopped 100 s under load, then restarted (ready in 2.6 s). Answer p95 1790 / 1828 / 1795 ms (before / during / after), search p95 11.96 / 11.59 / 11.97 ms; 0 request errors; baseline noise (two off runs) 36 ms and 0.5 ms. Traces resumed 2.1 s after the restart, no agent restart | PASS |

## Scored items

| ID | Observed | Result |
|---|---|---|
| S1 metrics and PromQL | Collector scrape of `/metrics` into O2, names and label keys identical (`rag_requests_total`, `_bucket`, `_sum`, `_count`). M1, M3 (p50, p95, p99), M4, M5, M6 ran unmodified; relative error versus the agent's own exposition at most 0.4% (mid-P1 and end-of-P1). M2 ran unmodified but returned an empty vector: no non-`ok` outcome occurred in the 5-minute window (empty numerator, same result as Prometheus), so it is not numerically verified | PASS, M2 unverified |
| S2 trace queries | J1: 100 of 100. J2: 70 slow answers in O2 and by client (Jaeger 70). J3: executed and returned ERROR-status spans; equality with ground truth not verified (no 5xx in the sampled window). Latency at 481k spans: 58 ms per request-id lookup, J2 14 ms, J3 11 ms (limit 3 s) | PASS (J3 partial) |
| S3 logs correlation | Excluded by D2/D3 (logs stay in Splunk) | NOT RUN |
| S4 footprint | Idle 94 MiB (max 125), P1 peak 273, burst 294, highest 519 (restart window); startup 2.6 s; steady CPU about 0.4% of one core (max 120% for seconds around a restart). Jaeger (memory store): idle 17, peak 66 MiB, startup 0.5 s. Bridge 44 MiB | PASS |
| S5 quota fit | Computed from peaks, no cluster: O2 peak 519 MiB (request 512Mi, limit 1Gi proposed), bridge 44 MiB (request 128Mi), data about 42 MiB; the whole host stack stayed under 1.2 GiB against the 4 GiB dev envelope (D6). Real site quota unverified | PASS (computed) |
| S6 retention and storage | All in: 42.3 MB allocated for 480,975 spans and 26,440 samples, about 90 bytes per span (traces parquet 18.9 MB). 14 days at the ADR-0002 bound (480k spans/day) is about 0.6 GB, 6% of 10Gi (mock content is repetitive, so compression is optimistic; a 5x margin is still 30%). Backdated traces and logs (3 days) were deleted by the compaction job without restart about 90 s after a 1-day per-stream retention was set (HTTP 200 for traces and logs; metrics type not tested) | PASS |
| S7 disk full | 128 MiB tmpfs volume, 400 posts of 0.8 MB: every post returned HTTP 200, `failed: 0`, but only 161 records were stored (earlier run showed `WalError` in the server log): silent loss on the JSON path. An OTLP trace post while full got no HTTP response (client error). Health stayed 200; after files were removed and a restart, ingest and query worked, no corruption | FAIL on visibility of failures; no corruption |
| S8 agent overhead | p95 versus tracing off (mean of two runs): search 11.1 ms off, 12.1 Jaeger, 12.2 O2 (+1.1 ms, within 10 ms); answer 1787 off, 1782 Jaeger, 1745 O2 | PASS |
| S9 operational burden | Net new stateful services +0 (O2 replaces Jaeger; the collector is stateless but a new image). Images: O2 and collector in, Jaeger out. Settings: 2 secret values and 8 other env settings. Restore: copy of the stopped volume opened by a second instance, 480,975 of 480,975 spans, ready in 2.7 s. Tools per metric spike to trace hop: 2 (O2, Splunk) versus 3 (console, Jaeger, Splunk), by design reasoning, not a hands-on count. Upgrade of the volume by v1.1.1 NOT RUN (no third image pull authorized) | PASS, upgrade NOT RUN |

Baseline comparison: marginal peak (O2 plus bridge minus Jaeger) is about +0.5 GiB; UWM and Splunk are platform-owned and unchanged. What O2 adds: one pane for traces and metrics.

## Open site checks

Real `restricted-v2` SCC and Pod Security admission (Phase B not run; the Docker-level equivalent flags passed), the real quota, platform forwarding of metrics and traces into O2 (bridge or remote write), a 14-day soak, Enterprise offline licence answers, silent-loss behaviour at disk-full on the site volume (S7), retiring the two Jaeger-specific checks (`smoke.sh`, `check_live.py`), and vendor-documented names for the two egress settings. AGPL-3.0 was accepted (D7). Not tested: SSO, RBAC and audit gaps in OSS.

## Teardown proof

Before and after object lists (containers, volumes, networks, scopes, Kind clusters) were captured by the driver. Everything labelled `trial=782` was removed:
containers (O2, bridge, relay, Qdrant, Jaeger and the throwaway instances), networks and volumes; Kind shows only `rag-kind`; no trial ports listen. One
anonymous volume left by the Jaeger container was found by the comparison and removed by hand. Not removed: the maintainer's `o2view-data` copy and any later viewer.
`rag-kind`, `rag-colab-706-4096` and the other users' containers were not touched.

## Evidence

`~/.local/share/mainframe-rag/o2trial782/` (outside git): driver and phase scripts, per-run `results.json`, logs, `evidence.sha256`.
