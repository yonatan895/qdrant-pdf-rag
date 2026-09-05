# Air-gap deployment internals reference

Owner: this file. Operator runbook: `docs/install_and_ops.md` §4. CI job
reference: this file §7. Design overview: `docs/architecture.md` §3.

> One fact, one owner — this file owns deploy internals. Code is named by
> module and script, never by line number.

## 1. Pipeline stages

`pipeline.sh` orchestrates five stages in order — validate → load → deploy
→ ingest → smoke — with `set -eu`, so a stage failure stops the run with
no rollback:

- `--skip-load` / `--skip-ingest` skip their stage; `--dry-run` exports
  `AIRGAP_DRYRUN=1` (every script prints instead of executing);
  `--help` usage; unknown arguments are silently ignored.
- Ingest runs only when `CORPUS_PVC` is non-empty and `--skip-ingest` is
  absent; otherwise the stage reports skipped.
- The final banner differs: `OPERATIONAL & ACCEPTED` when ingestion ran,
  `READY (Awaiting Corpus Ingest)` when it did not.

## 2. Environment precedence

`common.sh` is sourced by every air-gap script and implements one rule:
**explicit non-empty environment wins over the env file.** It snapshots all
documented operator keys (`OPERATOR_ENV_KEYS`) before sourcing, then
restores the snapshot over whatever the file assigned; empty stays unset
(matching the `${VAR:-default}` idiom everywhere).

- File selection: exported `AIRGAP_ENV` path wins, else local
  `./airgap.env` when present, else no file.
- Alias resolution: `INTERNAL_REGISTRY` falls back to `REGISTRY_INTERNAL`,
  `NAMESPACE` to `OPENSHIFT_NAMESPACE` (default `mainframe-rag`),
  `QDRANT_RELEASE` defaults to `qdrant`, empty `IMAGE_SHA` resolves from
  `git rev-parse HEAD`, and `EMBED_BASE_URL` derives from the vLLM URL with
  trailing slashes and a trailing `/v1` stripped.
- `require_env` collects **all** missing keys before failing, so one run
  tells the operator everything to fill in.
- Product rules: `EMBED_MODE=hash` dies (case-sensitive match on that exact
  string); storage classes containing `nfs` (any case) die — but only
  `STORAGE_CLASS` is checked, not snapshot/corpus classes.
- **Maintenance warning:** a new `.example` key that is not added to
  `OPERATOR_ENV_KEYS` silently regresses to file-wins. The list and the
  example must change together.
- `make airgap-dryrun` deliberately does **not** include the file at the
  make level, and its recipe carries fixed test parameters — so it can
  never render an operator's `airgap.env`. Render custom values by invoking
  the scripts (or pipeline) directly with explicit environment.

## 3. Render pipeline

Qdrant deploys from the vendored chart (`charts/qdrant-1.19.0.tgz` — never
`helm repo add` in the gap); the agent, ingest Job, and Jaeger deploy from
kustomize overlays. Manifests use `__TOKEN__` placeholders that fail closed
(`fail_on_placeholders`) when any `__[A-Z][A-Z0-9_]*__` survives rendering.

- Deploy defaults the Qdrant image tag by stripping the registry path and
  the `-unprivileged` suffix from `QDRANT_IMAGE`, because the chart
  re-appends that suffix when `useUnprivilegedImage` is set — passing the
  suffix through would double-append. The tag is always set explicitly so
  deploy matches what load pushed.
- The agent render unquotes `"__TOKEN__"` first (so integer/boolean env
  vars like `DENSE_DIM` and `RERANK_ENABLED` render unquoted), then
  substitutes each key; `RERANK_ENABLED` defaults to `false` and the rerank
  model default is baked in, while LLM/rerank-base/OTEL values default to
  empty.
- `wire_pull_secret` reuses the matched line's indent when replacing
  `imagePullSecrets: []` — every overlay nests it inside the pod spec, and
  a fixed-indent insert breaks out of the mapping (kubectl rejects the
  manifest). `PULL_SECRET` must be a DNS-subdomain name (no sed-active
  characters); unset renders `imagePullSecrets: []` (kustomize) and
  `imagePullSecrets=null` (Helm, so the chart's placeholder name never
  reaches the cluster).
- The Qdrant service URL is derived as plaintext
  `http://<QDRANT_RELEASE>:6333` (in-cluster DNS). The `<release>-apikey`
  secret name follows `QDRANT_RELEASE` — renaming the release without a
  reinstall orphans the agent/ingest key references (and reinstalls
  rotate the key: roll the agent afterward).
- Snapshot storage class falls back to `STORAGE_CLASS`. `QDRANT_STORAGE_SIZE`
  and `QDRANT_EXTRA_VALUES` empty means git prod values untouched (missing
  override file dies); both are rehearsal-only knobs that must never reach
  prod.
- Rollout waits: Qdrant StatefulSet 600s, agent Deployment 300s, Jaeger
  120s — on failure the script prints pods, warning events, and container
  logs before dying. `AGENT_ROUTE=true` creates an edge Route via `oc`
  (OpenShift only — it shells to `oc` even when `KC=kubectl`), default
  `false` keeps ClusterIP only.

## 4. Signing and provenance

The connected host packs one tarball: git bundle, Qdrant + Jaeger + ingest
+ agent image archives, vendored chart, bootstrap script, `MANIFEST.txt`,
`PACKING_RECORD.txt`, digest enumeration, offline signature, and member
checksums. Verify the tarball digest **before** unpacking, member checksums
**after**.

- Pack pulls (never builds) the app images from the registry tags of the
  checked-out SHA and fails closed on missing tags — pack only works on a
  green `main` SHA whose CI images exist. The owner is inferred lowercase
  from the git remote (GHCR 404s on uppercase). The git bundle pins HEAD
  explicitly so the air-gap clone lands on the packed SHA.
- `MANIFEST *_digest` lines are post-copy archive digests (the manifest
  *list* resolves to one arch manifest when written, by construction) —
  the ref line carries the requested pin, the digest line carries the
  bundled bytes that load re-verifies. Confusing the two is the classic
  digest-mismatch false alarm.
- `signed: true` appears only with a custodied key (`SNEAKERNET_KEY_TRUSTED`);
  rehearsal keys record `ephemeral`. The label is honesty, never a trust
  root. Trust roots, strongest first: a `SNEAKERNET_TRUSTED_PUB` obtained
  out of band (load/bootstrap refuse mismatches byte-for-byte), the
  published key fingerprint in `PACKING_RECORD.txt`, HTTPS download of the
  tarball. Without a pinned pubkey, verification is TOFU: it binds members
  together but proves nothing about *which* key signed.
- Load re-verifies the signature, then checksums, then the `IMAGE_SHA`
  against the manifest, then all four image digests — and pushes under
  fixed names: `qdrant/qdrant:v1.19.0-unprivileged`,
  `jaegertracing/jaeger:v2.20.0` (retag note: upstream tag `2.20.0`),
  `qdrant-pdf-rag-ingest:<SHA>`, `qdrant-pdf-rag-agent:<SHA>`. Full SHA
  only, never `latest` or short SHAs. `INSECURE_REGISTRY=true` disables TLS
  verify on the pack-pull side *or* the load-push side depending on which
  script reads it.
- `bootstrap.sh` cannot source `common.sh` (no clone exists yet), so it
  carries an inline twin of the trust check. It clones into
  `AIRGAP_WORKSPACE` (default `qdrant-pdf-rag`), skips the clone when a
  repo already exists, copies (not links) the archives into `dist/`, and
  seeds `airgap.env` from the example only when absent — never overwriting
  operator edits. Artifact discovery searches `dist/` then the parent dir.

## 5. Sizing and security

Prod values assume a real OpenShift cluster; the single-node Kind path
shrinks them via overrides that must never reach prod (a 3×16Gi Qdrant
cannot schedule on one node — proven).

- Prod Qdrant: 3 replicas, 500Gi data + 500Gi snapshots on RWO block,
  4 CPU/16Gi requests, 8 CPU/32Gi limits, `restricted-v2` UIDs
  (`runAsUser 1000`, static — clusters with allocated UID ranges need the
  `anyuid` grant or a `QDRANT_EXTRA_VALUES` override, which also silences
  the validate-time SCC advice), `readOnlyApiKey`, PDB maxUnavailable 1,
  hostname spread. Inter-node gossip is plaintext on the CNI
  (`enable_tls: false`) — mounting no cert avoids startup crashloops.
  No public Route, ever.
- The agent stays 2 tiny replicas (production patch changes only replica
  count, env, and pull secrets — FastAPI needs no GPU). The ingest Job is
  one-shot with a 24h deadline: caller corpus PVC read-only plus an
  auto-created `ingest-work` RWO scratch PVC (sized by `INGEST_WORK_SIZE`,
  default 100Gi, never deleted); previous runs are deleted before re-apply
  (Jobs are immutable); completion waits up to `INGEST_TIMEOUT` (default
  1h) while a background tailer streams pod logs, dumping logs and events
  on timeout.
- Jaeger is opt-in only (`OTEL_EXPORTER_OTLP_ENDPOINT` set): 1 replica,
  `fsGroup 10001` (upstream container user, group-writable RWO for
  Badger), 10Gi volume with 14-day span TTL, OTLP/HTTP 4318 only (no gRPC —
  `grpcio` is not in the wheelhouse, so 4317 stays closed), UI on
  port-forward only, no archive store (debug data, not records).
- Validate is read-only pre-flight: required keys, `DENSE_DIM` positive
  integer, `http(s)` vLLM URL, `IMAGE_SHA` not empty/`HEAD`, tool presence
  (even for dry-run), manifest cross-check (missing manifest is a notice,
  not a failure), storage-class existence, and OpenShift-detected SCC
  advice. It probes no inference endpoint — a bad vLLM URL passes
  validation and fails later.
- Smoke needs only a namespace: it execs into the agent pod (no Route or
  port-forward required), fails closed on degraded `/healthz`, treats empty
  search results as SKIP (infrastructure ready, corpus not ingested) rather
  than failure, and never touches `/v1/answer` (needs a reasoning model).

## 6. Images and pins

`images.txt` is the digest contract: the cluster must run exactly these
bytes. Combined tag+digest refs are invalid — digest-only form is the pin.

- Base is UBI 9 `python-314-minimal` (CPython 3.14 GIL, digest-pinned);
  builds use `--no-index` from the baked wheelhouse (never PyPI) with BM25
  weights baked to `/opt/bm25`; `mock_vllm.py` is excluded via
  `.dockerignore` so the CI mock never ships; images run as UID 1000
  (non-root), agent serving uvicorn on 8080, ingest entrypointing
  `run_ingest`.
- `qdrant-client` in the lockfile must track the 1.19 server and chart.
  `make pull-chart` pulls latest unpinned — a drift risk if re-run without
  a `--version` pin; the committed tgz is the contract.
- The `oc-mirror` config still uses tag form and is otherwise unreferenced
  (optional path) — reconcile to digests before relying on it.

## 7. CI inventory

GitHub runs unit, sim, gates, bench, load, connected E2E, and the dry-run
gate; air-gap GitLab runs hygiene + pytest + gate-l1 only (no e2e, load,
deploys, GHCR, or PDFs/tokens/hostnames in file). Job meaning stays
aligned across the two files; only e2e-scale jobs live in
`.github/workflows/e2e.yml`.

- Markdown-only changes (outside vendored docs) run no GitHub checks;
  mixed changes run CI + E2E.
- `ci.yml`: hygiene (refuse committed PDFs), pytest (integration
  deselected), sim (docker Qdrant, fail-closed on skips/zero-pass), gate-l1
  with PR delta comment. Least-privilege permissions, timeouts, and
  concurrency groups on every job; third-party actions SHA-pinned.
- `load.yml`: path-allowlisted to agent/retrieve/ingest/mock/sim/loadtest
  surface — other paths run nothing.
- `bench.yml`: push-to-main + nightly + dispatch (baseline update with
  repeats); never a PR gate.
- `e2e.yml build`: local images retagged to full-SHA GHCR refs; push only
  on `main`/dispatch (PRs build, never push — fork-safe).
  `airgap-package` (main/dispatch): pack + 90-day bundle artifact (PRs
  skip). `airgap-acceptance` (main/dispatch): black-box handoff in a fresh
  dir — digest verify, unpack, bootstrap, manifest/SHA assertions, dry-run
  pipeline with standin env passed explicitly, both pull-secret branches.
  `kind-live-rehearsal` (main/dispatch): the same handoff live against
  ephemeral Kind + local registry (pre-loaded, `--skip-load`), mock vLLM,
  generated corpus, search assertions; always tears down. Lab OpenShift
  jobs are secret-gated and PRs never touch the lab cluster.
- Pinned third-party versions live in-repo (kubectl + sha256, helm, kind,
  node image); the local-path provisioner manifest is version-pinned in URL
  only (v0.0.37, no sha256 check) — known supply-chain gap. The opencode reviewer workflow is
  dispatch-only and GitHub-only.
- `run_local_vllm.sh` resolves (never probes) launch flags from the
  `serve` Budget `LOCAL_RT_8GB` profile: pinned `v0.28.0` image (which
  removed `--task`, hence `--runner pooling --convert embed`), reasoning
  0.64 / embed 0.33 GPU fractions at 4096 tokens both, eager embed,
  prefix-cache on reasoning only, explicit env always winning, fail-closed
  resolve with `--check-pack` preflight. Model names containing `embed`
  (either case) select the embed role; secrets pass via environment, never
  argv.
