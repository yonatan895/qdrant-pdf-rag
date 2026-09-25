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
  `--help` usage; unknown arguments fail before any pipeline stage runs.
- Ingest runs only when `CORPUS_PVC` is non-empty and `--skip-ingest` is
  absent; otherwise the stage reports skipped.
- After deployment, the pipeline runs `scripts/probe_gateway.py` inside the
  agent pod before ingestion. Embeddings are required; reasoning and rerank
  are checked when configured. A failed leg stops the pipeline. This uses
  the pod's actual endpoints and Secret-backed keys, not bastion connectivity.
- The final banner differs: `OPERATIONAL & ACCEPTED` when ingestion ran,
  `READY (Awaiting Corpus Ingest)` when it did not.

Standalone `sh scripts/tools/run-task.sh airgap:deploy` only waits for workload readiness. The agent
`/healthz` check covers Qdrant and embedding connectivity **and** the served
generation's representation contract (HTTP 503 for any non-servable state);
`/livez` is the process-only liveness probe. It does not prove that reasoning
works. Run the gateway probe before ingesting when using the
modular commands. `sh scripts/tools/run-task.sh airgap:smoke` checks retrieval and tracing; use the
console/answer checks in the operator runbook to verify the user experience.

Production transfer additionally requires [the manual CRC release gate](crc-release-verification.md)
and its [completed evidence record](crc-release-record.md). The existing scripts
do not enforce that record; their success banner is pipeline acceptance only.
Missing or failed CRC checks block transfer of the candidate bundle.

## 2. Environment precedence

`common.sh` is sourced by every air-gap script and implements one rule:
**explicit non-empty environment wins over the env file.** It snapshots all
documented operator keys (`OPERATOR_ENV_KEYS`) before sourcing, then
restores the snapshot over whatever the file assigned; empty stays unset
(matching the `${VAR:-default}` idiom everywhere).

- File selection: exported `AIRGAP_ENV` path wins, else local
  `./airgap.env` when present, else no file. An explicitly selected
  `AIRGAP_ENV` must be a readable regular file or the launcher fails
  closed before any mutation (issue #478) — a missing selection never
  silently becomes an environment/default-only run. Relative selections
  resolve from the repository root; paths with spaces load as data.
- Alias resolution: `INTERNAL_REGISTRY` falls back to `REGISTRY_INTERNAL`,
  `NAMESPACE` to `OPENSHIFT_NAMESPACE` (default `mainframe-rag`),
  `QDRANT_RELEASE` defaults to `qdrant`, empty `IMAGE_SHA` resolves from
  `git rev-parse HEAD`, and `EMBED_BASE_URL` derives from the vLLM URL with
  trailing slashes and a trailing `/v1` stripped.
- `require_env` collects **all** missing keys before failing, so one run
  tells the operator everything to fill in. `EMBED_MODEL_REVISION` is a
  required key on every air-gap launch path (vllm mode refuses a blank
  attestation); `validate.sh` additionally rejects whitespace-only values.
- Product rules: `EMBED_MODE=hash` dies (case-sensitive match on that exact
  string); storage classes containing `nfs` (any case) die — but only
  `STORAGE_CLASS` is checked, not snapshot/corpus classes.
- **Maintenance warning:** a new `.example` key that is not added to
  `OPERATOR_ENV_KEYS` silently regresses to file-wins. The list and the
  example must change together.
- Task discovery never loads private env files. Air-gap tasks bridge only
  caller-set operator values to `common.sh`; CLI assignments win over the
  caller environment before that script applies its precedence rule.
  `sh scripts/tools/run-task.sh airgap:dryrun` executes the pipeline with
  fixed stand-ins for its declared dry-run inputs. Other operator keys still
  follow `common.sh`; inspect that owner before supplying private config.
  Task `--dry` only previews commands and is not rendering evidence. Render
  custom values through `airgap:pipeline AIRGAP_DRYRUN=1` or the scripts with
  explicit environment.

## 3. Render pipeline

Qdrant deploys as its separate release from `charts/qdrant-1.19.0.tgz`;
`mainframe-rag` deploys the first-party chart from the exact verified source
checkout. Use the checksum-pinned Helm 4.3.0 client from
[the workflow](../.github/workflows/e2e.yml). Neither chart needs a remote
chart repository in the air gap.
`airgap.env` remains the only operator configuration owner. After `common.sh`
resolves precedence and validates inputs, `map_app_values` exports the declared
non-secret inputs to the deterministic serializer in `map_values.py`.

`airgap:deploy` writes `dist/mainframe-rag-release-values.yaml`, runs Helm
schema/lint/template checks, checks the rendered read-only serving credential,
and verifies selected Secret keys and resource ownership before either release
is mutated. It then upgrades Qdrant and the application separately. The app
release never owns an ingest Job, corpus PVC or Qdrant resource. Generated
values/manifests are ignored evidence, not a manually editable configuration
surface. All Kubernetes env scalar values are strings, including all-digit
SHAs and revisions containing YAML-significant characters. The schema preserves
positive operator dimensions and worker counts without introducing the shadow
chart's former 4096-dimension or 32-worker ceilings.

`airgap:ingest` writes `dist/mainframe-rag-ingest-values.yaml` and explicitly
renders the Job and external ingest-work PVC using `--show-only`. The scratch
claim is created only if absent; failed reads stop the operation. The existing
Job is deliberately deleted/replaced, waited for, and diagnosed by shell.
Ingest has no Helm hook and no automatic upgrade execution. Existing worker,
publication, retirement validation and `/work/inventory.jsonl` lock semantics
remain unchanged. Route configuration is not needed to render an ingest Job.

Compatibility and lifecycle decisions under #448:

- `check_app_ownership.py` accepts only the fixed first-party resource inventory
  in the selected namespace. Existing unmanaged Kustomize resources can be
  adopted with Helm's `--take-ownership`; another release, namespace, deployment
  manager or controller owner fails preflight. Operators must serialize
  deployment operations in that namespace; this read-before-write check is
  not a distributed lock. Adoption changes management metadata, not authorization.
  Application installs/upgrades explicitly use `--server-side=false` to preserve
  client-side apply during migration. Helm 4's default server-side apply can
  adopt unchanged legacy objects but then conflict with `kubectl-client-side-apply`
  on the next image update. Use the same flag for application rollbacks; do not
  force conflicts or replace resources to bypass ownership checks.
- The first adoption also inventories disabled legacy optional objects using
  cluster API discovery. Discovery/read/ownership failures stop before either
  release changes. Once selected workloads are ready, deployment deletes only
  the checked disabled Jaeger workloads/config, console Route/ServiceAccount
  and ServiceMonitor. This covers objects absent from the first Helm release's
  history. The cleanup inventory never permits a PVC.
- Disabled optional workloads, Routes and monitors are removed on upgrades of
  the Helm release. The Jaeger PVC has `helm.sh/resource-policy: keep`: disabling
  tracing or removing the app release retains its data. Re-enabling tracing
  reuses that claim; neither application rollback nor uninstall rolls back data.
- `PULL_SECRET` and `GATEWAY_API_KEY_SECRET` remain DNS-subdomain names and
  Secret references. An absent pull Secret renders `imagePullSecrets: []` for
  first-party pods and `imagePullSecrets=null` for Qdrant. An absent gateway
  Secret omits key env entries. Selected refs require nonempty keys before
  mutation: agent `llm/embed/rerank-api-key`, ingest
  `embed/context-llm-api-key`, pull `.dockerconfigjson`, OAuth `cookie-secret`.
  No key values enter generated files or logs. Plaintext gateway key settings
  remain rejected by `enforce_product_rules`.
- `GATEWAY_CA_CONFIGMAP` remains a reference to `ca-bundle.crt`, mounted only
  in application containers. `AGENT_ROUTE=true` requires the recorded OAuth
  image digest and cookie Secret. Deploy reads the public namespace Service CA
  into generated values and Helm reconciles the reencrypt Route, OAuth port,
  sidecar and ServiceAccount, including an existing Route's CA/timeout.
  Route-on deployment therefore requires its namespace and operator Secrets
  to exist first. Dry-run uses an explicit `ROUTE_DESTINATION_CA_FILE`.
- `UI_ENABLED=false` renders the API-only POC profile (issue #479): the
  Deployment carries `UI_ENABLED="false"` and the application serves its
  stable 404 envelope on every `/ui` path with zero model/retrieval calls.
  Unset keeps the default `/ui` served. UI selection is independent of
  `AGENT_ROUTE`: disabling the console never removes OAuth/TLS/access
  controls, and Route-off is not a UI-off claim. Invalid nonempty values
  fail before mutation (`UI_ENABLED must be true/false`).
- Qdrant's tag still strips `-unprivileged` before the upstream chart appends it.
  Snapshot class falls back to `STORAGE_CLASS`. Rehearsal-only sizing inputs
  (`QDRANT_STORAGE_SIZE`, `QDRANT_EXTRA_VALUES`, `QDRANT_TAG`, `INGEST_WORK_SIZE`,
  `INGEST_EXTRA_PATCH`) retain their existing precedence and never change the
  production defaults. The last remains a client-side CI-only Job patch.
- Rollout waits remain Qdrant 600s, agent 300s and Jaeger 120s, with bounded
  diagnostics on failure. Explicit ingest keeps its existing wait/diagnostics.

- The Qdrant service URL is derived as plaintext
  `http://<QDRANT_RELEASE>:6333` (in-cluster DNS). The `<release>-apikey`
  secret name follows `QDRANT_RELEASE` — renaming the release without a
  reinstall orphans the agent/ingest key references (and reinstalls
  rotate the key: roll the agent afterward).
- Qdrant credential separation (issue #366): the chart stores two keys in
  `<release>-apikey` — full-access `api-key` and read-only
  `read-only-api-key`. The serving agent wires `QDRANT_API_KEY` to the
  read-only data key; ingest keeps the full-access one. Serving reads
  (query/search, collection info/exists, snapshot listing, `/readyz`)
  all succeed under the read-only key; mutations 403 and unknown keys
  401 (pinned by `tests/test_qdrant_auth.py` against the vendored image).
  Deploy and ingest preflight fail closed when the rendered manifests
  reference the wrong key (`check_agent_qdrant_key` /
  `check_ingest_qdrant_key` in `common.sh`); `sh scripts/tools/run-task.sh airgap:validate`
  pins the same contract on the chart template sources. Key values never appear
  in manifests, logs, or test output — only Secret names and data keys.
- Qdrant key rotation (chart-native): the chart generates both keys with
  `randAlphaNum 32` and reuses the existing Secret across `helm upgrade`
  while it exists. To rotate: `kubectl -n $NAMESPACE delete secret
  <release>-apikey`, re-run the Helm release step (`sh scripts/tools/run-task.sh airgap:deploy`
  regenerates both keys on upgrade), then `rollout restart` the agent
  Deployment and re-run ingest consumers so every pod picks up the new
  Secret revision. Verify with a real search (smoke) plus a revoked-key
  probe: the old key must 401 while the corpus (PVC-backed points) is
  untouched. Do not render with `helm template --dry-run` and apply the
  result: without cluster access the chart's Secret lookup misses and
  every render mints fresh random keys.
The published-bundle Kind lifecycle lane uses the same chart and values
mapper for A/A/B/failed rollout/recovery A/B/redeploy A. It checks admitted
fault injection, serving smoke and PVC identities. Independent rendered behavior
is checked by `tests/test_helm_chart_contracts.py`; producer round trips and
preflight/lifecycle behavior remain in the mapper and air-gap suites. The
retired parity oracle's coverage mapping is in [testing](testing.md#helm-coverage).
Helm rendering and Kind do not prove OpenShift or actual internal-site
qualification. Record unexecuted checks as not run in the candidate review;
they do not authorize production promotion.

## 4. Signing and provenance

The connected host packs one tarball: git bundle, Qdrant + Jaeger + ingest
+ agent image archives (plus the oauth-proxy sidecar image once its
`images.txt` digest is recorded), vendored chart, bootstrap script,
`MANIFEST.txt`, `PACKING_RECORD.txt`, digest enumeration, offline signature,
and member checksums. Verify the tarball digest **before** unpacking, member
checksums **after**.

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
  against the manifest, then all four base image digests (plus the
  oauth-proxy digest when the bundle carries it) — and pushes under fixed
  names: `qdrant/qdrant:v1.19.0-unprivileged`,
  `jaegertracing/jaeger:v2.20.0` (retag note: upstream tag `2.20.0`),
  `qdrant-pdf-rag-ingest:<SHA>`, `qdrant-pdf-rag-agent:<SHA>`, and
  `openshift4/ose-oauth-proxy:v4.14` when bundled. Full SHA only, never
  `latest` or short SHAs. `INSECURE_REGISTRY=true` disables TLS
  verify on the pack-pull side *or* the load-push side depending on which
  script reads it.
- `bootstrap.sh` cannot source `common.sh` (no clone exists yet), so it
  carries an inline twin of the trust check (bundle signature honoring
  `SNEAKERNET_TRUSTED_PUB`, then `SHA256SUMS`). It clones into
  `AIRGAP_WORKSPACE` (default `qdrant-pdf-rag`), skips the clone when a
  repo already exists, copies (not links) the archives into `dist/`, and
  seeds `airgap.env` from the example only when absent — never overwriting
  operator edits. Artifact discovery searches `dist/` then the parent dir.
  The copy list includes `oauth-proxy-image.tar` when the bundle contains it;
  no manual sidecar-image copy is needed before `airgap:load`.

## 5. Sizing and security

Model servers (reasoning, dense embed, reranker) live in a
platform-team-owned pool with its own resources — this repo neither
deploys nor sizes them, so model VRAM/RAM/CPU is out of scope here.
Everything below sizes this repo's workloads only (Qdrant, agent,
ingest, Jaeger). Where this guide says prod has ≥10× local, that means
CPU/RAM/disk for those workloads, not model GPU/VRAM.

Prod values assume a real OpenShift cluster; the single-node Kind path
shrinks them via overrides that must never reach prod (a 3×16Gi Qdrant
cannot schedule on one node — proven).

- Prod Qdrant: 3 replicas, 500Gi data + 500Gi snapshots on RWO block,
  4 CPU/16Gi requests, 8 CPU/32Gi limits, project-assigned UID/GID and
  volume group from `restricted-v2`. Project-range
  admission and volume writes must pass the CRC gate; fix demonstrated
  incompatibilities in this production configuration, never by granting
  `anyuid` or hiding security changes in a local sizing override.
  `readOnlyApiKey`, PDB maxUnavailable 1,
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
- Jaeger is on by default (unset `OTEL_EXPORTER_OTLP_ENDPOINT` resolves to
  `http://jaeger:4318`; the off sentinel disables both tracing and this
  deployment): 1 replica,
  project-assigned UID and volume group from `restricted-v2` for Badger, 10Gi volume with 14-day span TTL, OTLP/HTTP 4318 only (the configured exporter uses HTTP;
  the Qdrant client independently brings a transitive `grpcio` wheel), UI on
  port-forward only, no archive store (debug data, not records).
- Validate is read-only pre-flight: required keys, non-blank
  `EMBED_MODEL_REVISION` (the vllm attestation ingest/serving refuse when
  blank), `DENSE_DIM` positive
  integer, `http(s)` vLLM URL, `http(s)` scheme on any set optional model
  URL (`EMBED/LLM/RERANK/CONTEXT_LLM_BASE_URL`), `RERANK_ENDPOINT_ORDER`
  limited to `score_first`/`rerank_first`, `GATEWAY_API_KEY_SECRET`
  charset gate, plaintext `*_API_KEY` refusal in the env file (pack, load,
  deploy, ingest, and validate — not smoke), `IMAGE_SHA` not empty/`HEAD`, tool presence
  (even for dry-run), manifest cross-check (missing manifest is a notice,
  not a failure), storage-class existence, gateway key Secret existence
  (notice when the namespace does not exist yet), and OpenShift-detected SCC
  advice. It probes no inference endpoint — a bad vLLM URL passes
  validation and fails later.
  The current validator still contains legacy `anyuid` advice in its
  no-extra-values branch. Do not follow that advice: the owning production
  values remove fixed IDs and actual `restricted-v2` admission is required.
  The CRC sizing-override path does not exercise that advisory branch.
- Smoke needs only a namespace: it execs into the agent pod (no Route or
  port-forward required), fails closed on degraded `/healthz`, treats empty
  search results as SKIP (infrastructure ready, corpus not ingested) rather
  than failure, and never touches `/v1/answer` (needs a reasoning model).
  With tracing on (the default) it also fails closed unless a `v1.search`
  span lands in `JAEGER_QUERY_URL` within `TRACE_TIMEOUT`; the `off`
  sentinel skips that assertion.

<a id="collection-policy"></a>
### Collection distribution and placement (issue #360)

**Production default (checked in, non-secret):** 6 logical shards,
replication factor 3, write consistency 2, across the three Qdrant peers,
applied to both the corpus collection and its paired completion/control
collection. `overlays/openshift/collection-policy.env` carries the tuple;
`scripts/airgap/common.sh` loads it with explicit caller > operator file >
preset precedence, and `airgap.env.example` documents concrete values. This
is the **production default**, distinct from the verifier's stricter
`--production` claim. The loader validates the complete *effective* tuple
after merging: all three keys present, positive integers, W <= RF. It does
not force every caller to exactly 6/3/2 — a partial override inherits the
remaining preset values, and local, CI and one-node lanes select 1/1/1
explicitly (the explicit operator environment in `.github/workflows/e2e.yml`) as
a deliberate non-HA profile, never inferred from the available node count.
Generic `QDRANT_*` Settings stay optional for local compatibility, but
unset values cannot qualify the production path.

**Failure contract:** one unavailable peer at a time (planned or
unplanned). RF=3/W=2 keeps acknowledged writes available with one peer
gone; two surviving copies are *degraded*, not healthy, and a new
generation must not cut over until every shard has three ACTIVE copies
again. Do not change RF/W automatically during failure. RF=3 is not
tolerance of simultaneous failures, shared-storage loss, or loss of the
platform model/gateway tier. Capacity follows: RF=3 stores three physical
copies of the logical generation plus snapshots and staging, so a peer is
never sized as one third of the corpus.

**Placement is a hard requirement:** `overlays/openshift/values.yaml` sets
required pod anti-affinity across `kubernetes.io/hostname`. A capacity
squeeze leaves a peer Pending with a diagnostic instead of silently
colocating. The PDB (`maxUnavailable: 1`) limits voluntary disruption only
and is not a copy count; maintenance waits for full shard recovery before
the next eviction/restart. The chart's selector is release-independent
(`app.kubernetes.io/name: qdrant`), so **one Qdrant Helm release per
namespace** is the architecture: generations are collections inside that
one release, never a second release deployed as a "repair generation". A
release rename or blue/green release replacement needs a deliberate
maintenance plan; it does not behave like a rolling StatefulSet update.
Hostname separation is not yet proof of independent hypervisor/rack/storage.

**Verified state:** `scripts/verify_placement.py` is read-only and checks
actual placement for every durable collection of the active generation —
the alias-resolved physical corpus collection plus its paired control
collection — not pod count or configured values. `QDRANT_URL` is the
**entry endpoint** (often a load-balanced Service): it supplies inventory,
the alias binding and configured-policy reads, and is never counted as a
peer identity. Every `--peer-url` is an authoritative **direct peer**
endpoint; each one must report its own peer id, the same cluster
membership and a working consensus thread, and only those endpoints'
local-shard reports count as copies. The entry endpoint's own cluster view
must agree with the peers as well, so a Service routed to a different
cluster cannot certify it. An alternating Service therefore cannot become
an extra replica, and three addresses that all reach one peer cannot
satisfy RF3. The alias -> physical-generation binding is captured
with the inventory and re-read across every reachable endpoint after
observation; a generation that moves mid-inspection is retried and then
refused, never certified stale.

```sh
# Production qualification: owner decision, every expected peer direct.
QDRANT_URL=http://qdrant:6333 QDRANT_SHARD_NUMBER=6 \
QDRANT_REPLICATION_FACTOR=3 QDRANT_WRITE_CONSISTENCY_FACTOR=2 \
python3 scripts/verify_placement.py --production \
    --peer-url http://qdrant-0.qdrant-headless:6333 \
    --peer-url http://qdrant-1.qdrant-headless:6333 \
    --peer-url http://qdrant-2.qdrant-headless:6333

# Explicit single-node profile (1/1/1, labeled non-HA).
QDRANT_URL=http://127.0.0.1:6333 \
python3 scripts/verify_placement.py --expect-single-node
```

Run it from the cluster network (an agent/ingest pod or the bastion with
access to each peer). In the ingest image the script ships at
`/app/scripts/verify_placement.py` while the image entrypoint runs
ingestion, so a read-only diagnostic Job must override the command
explicitly (run that image's `python3 /app/scripts/verify_placement.py
--production --peer-url ...` with the same `QDRANT_*` environment instead
of the ingest entrypoint); do not assume the agent image or a bastion has
the same script/dependency layout. The operator runbook for inspection,
mutation, abort, retry, and recovery ownership is
[install and operations](install_and_ops.md#5-day-2-operations--maintenance).

It examines cluster membership/consensus on every direct peer, the
configured S/RF/W per required collection on every reachable peer, every
logical shard's distinct ACTIVE copies, transfers/recovery states, and
refuses to count a replica merely because another peer reports it.
Observations deduplicate by `(collection, shard_id, peer_id)`; observed
peer ids outside the reported member set, contradictory identities and
shard ids outside the declared shard set are refusals, not ignored data.
Unknown/unreadable metadata is unverifiable, never green. Outcomes:
healthy, degraded (a positively established loss of exactly one known
member), recovering, unverifiable, unservable, and the explicit non-HA
profile. Exit 0 verifies healthy or the declared non-HA profile; exit 1
refuses everything else. `--allow-degraded` (never combined with
`--production`) turns only that single-member-loss state into exit 0 for
operational continuation; missing membership, stopped/unknown consensus,
duplicate/anonymous identities, missing collections and incomplete
observations stay nonzero. The verifier observes placement only — it
performs no read or write request, so `healthy` is not read availability,
write acknowledgement, or RPO/RTO evidence. Exit 2 is a usage or
contradictory/incomplete claim. The final `VERDICT:` line is the
machine-readable outcome. Moving replicas is the later migration slice;
this command never mutates. Publication eligibility is enforced in process
as well as by this command: the cutover gate requires RF ACTIVE copies per
shard on the staging pair through direct peer endpoints (`QDRANT_PEER_URLS`,
[publication contract](ingest.md#publication-contract)) and refuses while
degraded; operator plumbing of that setting through the air-gap render path
is a follow-up slice.

**Disposable local proof:** `scripts/qdrant_cluster.py` starts the pinned
image as three loopback peers and `tests/test_ha_cluster.py`
(`sh scripts/tools/run-task.sh qa:ha`) asserts real 6/3/2 placement, a
false-HA (RF1) corpus+control refusal, degraded reads and healthy rejoin
after stopping one peer, exact corpus **and seeded control-record**
ids/payloads through survivors and again on every peer after rejoin, and
the publication cutover gate over real staging pairs (healthy 6/3/2
passes; RF1 and control-only-mismatched staging refused) and the in-process
ACTIVE-copy gate (refuses while one peer is down, passes again after
rejoin). Each
scenario creates its own corpus+control pair, so it runs alone or in any
order, and rejoin waits for every shard to be ACTIVE on three distinct
peers before re-qualifying (membership count alone is not catch-up). Three
containers on one host prove distributed software behavior, not
independent-worker or site tolerance. Existing one-node CRC/Kind lanes and
the three-worker lifecycle lane are not distributed acceptance. The lane
further demonstrates W=2 acknowledged writes under one-peer loss (exact
corpus/control read-back via survivors and on every peer after rejoin),
identical-ID retry convergence, and 2+1 partition behavior (minority writes
never acknowledge; majority writes stay exact; healed peers converge to one
exact state, so retries must reuse identical IDs and identical content).

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
  `sh scripts/tools/run-task.sh artifacts:chart-fetch` pulls latest unpinned — a drift risk if re-run without
  a `--version` pin; the committed tgz is the contract.
- New runtime deps require a connected-host wheelhouse refresh before an
  air-gap cut: a `requirements.lock.txt` bump (e.g. `jinja2` +
  `python-multipart` for ADR-0004) means `sh scripts/tools/run-task.sh artifacts:wheelhouse artifacts:bm25` plus
  a connected image rebuild/push and a fresh pack. The air-gap images
  install only from the baked wheelhouse (`--no-index`).
- The oauth-proxy digest is recorded in `images.txt`. Connected packaging
  requires `skopeo login registry.redhat.io`; GitHub's `airgap-package` job
  requires `REDHAT_REGISTRY_USER` and `REDHAT_REGISTRY_PASSWORD` secrets
  belonging to a registry service account and fails closed if either is absent
  or login fails. Personal CRC pull secrets stay local. The OAuth copy uses
  `--remove-signatures` because Docker archives cannot store Red Hat's upstream
  signature attachments. Source digest verification, archive digests, and the
  offline bundle signature still apply; upstream signature attachments are not
  part of the handoff. The explicit PENDING state still skips the member and
  makes `AGENT_ROUTE=true` fail closed. A tag bump for `ose-oauth-proxy`
  must change all three sites: `images.txt`, `deploy.sh`, `load.sh`.
- `METRICS_ENABLED=true` additionally renders/applies
  the first-party chart ServiceMonitor so the OpenShift UWM stack scrapes
  `/metrics` (prerequisite and sizing in `docs/install_and_ops.md`).
- The `oc-mirror` config still uses tag form and is otherwise unreferenced
  (optional path) — reconcile to digests before relying on it.

## 7. CI inventory

GitHub runs unit, sim, gates, bench, load, the path-filtered multi-peer HA
lane (issue #360: three-peer placement/peer-loss fixture), connected E2E,
and the dry-run gate; air-gap GitLab runs hygiene + pytest + gate-l1 only
(no e2e, load, deploy, multi-peer docker, GHCR, or PDFs/tokens/hostnames in
file). Job meaning stays aligned across the two files; only e2e-scale jobs
live in `.github/workflows/e2e.yml`.

- GitHub product `ci.yml`/`e2e.yml` ignore markdown-only changes. The narrow
  `agent-context.yml` checks relevant instructions/docs/templates and its own tools,
  including root-only AGENTS changes, without model/image/deployment work.
  Vendored-only changes do not enter that context lane. Mixed changes retain
  existing product CI/E2E behavior. GitLab hygiene runs the same offline checker.
- GitHub unit/context/review lanes explicitly install the pinned host Task into
  `.tools/bin` and dispatch through `sh scripts/tools/run-task.sh`. Runner
  contract lanes set `TASK_CONTRACTS_REQUIRE_RUNNER=1`; absence is a failure,
  never a skipped pass. Offline GitLab runners mount the approved
  `task_linux_amd64.tar.gz` at `CI_TASK_ARCHIVE`; the unit job verifies/installs
  it with `install-task.sh --archive`, with no public download. Prepared Python
  jobs retain their interpreter and do not run `dev:setup`. Python preparation
  uses [complete target hash locks](dependencies.md) and verifies the installed
  inventory. Runtime images check approved wheel bytes before offline installation;
  packaging reconciles actual image layers with the Python SBOM. Unit jobs also
  explicitly prepare the existing pinned Helm binary (offline GitLab input
  `CI_HELM_ARCHIVE`) and run `agent_doctor.py --python` before pytest collection.
  Both Task and the selected Helm executable are checksum-verified; the doctor
  never downloads or repairs. See [tool preparation](task-runner.md#installation).
- Taskfile/modules participate in tooling review/path selection; air-gap and
  artifact modules plus `scripts/tools/` select deployment checks. Existing
  direct pipeline calls retain their explicit `--dry-run` / `--skip-load`
  contract. Neither the runner nor path classification authorizes new network
  access or cluster operations.
- `ci.yml`: hygiene (refuse committed PDFs), pytest (integration
  deselected, split across two runner VMs with an aggregate `test` status
  requiring both shards), sim (docker Qdrant, fail-closed on skips/zero-pass), gate-l1
  with PR delta comment. Least-privilege permissions, timeouts, and
  concurrency groups on every job; third-party actions SHA-pinned.
- `load.yml`: path-allowlisted to agent/retrieve/ingest/mock/sim/loadtest
  surface and shared dependency/Task inputs — other paths run nothing. `ha.yml` is path-allowlisted to the
  collection-policy/placement surface and runs the three-peer fixture
  (`qa:ha`) fail-closed (no skips, at least one pass); unrelated changes
  do not pay for three containers.
- `bench.yml`: push-to-main + nightly + dispatch (baseline update with
  repeats); never a PR gate.
- `e2e.yml build`: local images retagged to full-SHA GHCR refs; push only
  on `main`/dispatch (PRs build, never push — fork-safe).
  `airgap-package` (main/dispatch): pack + 90-day bundle artifact (PRs
  skip). `airgap-acceptance` (main/dispatch): black-box handoff in a fresh
  dir — digest verify, unpack, bootstrap, manifest/SHA assertions, dry-run
  pipeline with standin env passed explicitly, both pull-secret branches.
  `kind-live-rehearsal` (main/dispatch) is a three-lane matrix described below.
  The lab OpenShift rehearsal remains secret-gated, and PRs never touch the lab cluster.
  `airgap-rehearsal` downloads and bootstraps the published bundle; it does not
  independently repack. It retains both outline-message and generic widget
  search checks, Qdrant/agent readiness and explicit ingestion formerly covered
  by the redundant direct-manifest hash-only lab lane. A skipped lab job supplies
  no OpenShift verification.
- Pinned third-party versions live in-repo (kubectl + sha256, helm, kind,
  node image); the local-path provisioner manifest is pinned to a source commit and
  sha256-verified before applying it. The opencode
  reviewer workflow is GitHub-only: when enabled, it runs on pull requests
  plus `/oc` comments, never mirrored to GitLab. To pause quota consumption
  without deleting its file, use `gh workflow disable opencode.yml` and
  cancel any already-running reviews explicitly. Restore it later with
  `gh workflow enable opencode.yml`; workflow state lives in GitHub, not git.
- `run_local_vllm.sh` resolves (never probes) launch flags from the
  `serve` Budget `LOCAL_RT_8GB` profile: pinned `v0.28.0` image (which
  removed `--task`, hence `--runner pooling --convert embed`), reasoning
  0.64 / embed 0.33 GPU fractions at 4096 tokens both, eager embed,
  prefix-cache on reasoning only, explicit env always winning, fail-closed
  resolve with `--check-pack` preflight. Model names containing `embed`
  (either case) select the embed role; secrets pass via environment, never
  argv.

### Release rehearsal lanes and fixes

All acceptance lanes consume `sneakernet-bundle-<full-sha>` from the same factory
run, verify its outer checksum, extract into a fresh directory and bootstrap its
repository. Each Kind lane has its own runner and disposable cluster. Diagnostics
include actual runner CPU, RAM and disk capacity; cleanup runs even after failure.

| Job / lane | Real services and assertions |
|---|---|
| `airgap-acceptance` | Fresh bundle integrity/bootstrap, manifest SHA and both pull-secret render branches |
| `kind-pipeline` | Authenticated registry, real product containers, real LiteLLM/PostgreSQL, synthetic ingest, search and application streams |
| `kind-gateway-faults` | TLS/auth, both model legs, malformed/upstream/dimension/timeout/truncated-stream failures through LiteLLM, followed by healthy recovery |
| `kind-lifecycle` | Three-worker Kind; synthetic snapshot recovery, PVC identity, Qdrant/agent/Jaeger replacement, old trace persistence and repeat pipeline |
| Manual Windows CRC | Actual SCC, Service CA, OAuth, Routes, node trust/pulls and runtime egress; record the fit outcome and fallback mode separately |

Only computation behind LiteLLM is deterministic in the Kind lanes. The existing
`mock_vllm.py` uses explicit model IDs, 1024-dimensional embeddings, an explicit
`EMBED_MODEL_REVISION` stand-in (`mock-embed@ci`), synthetic
citation output and controlled faults. Mock citations establish interface
behavior, not answer quality. The mock, CI deployment helpers and gateway module
are excluded from application images and production manifests.

The rehearsal now configures **both** embedding and reasoning URLs through the
real test gateway. Service existence is awaited before readiness checks. Fault
transitions wait for the requested mock state through the actual upstream Service,
so a rollout's success cannot leave a test hitting the old backend state.
The gateway's configuration and strict-finish module share a projected volume;
there is no nested read-only `subPath` mount to fail during container creation.

`probe_gateway.py --require-reasoning --stream` fails when reasoning is absent,
the stream reports an error, no successful finish arrives, or `[DONE]` is missing.
The local/CI `strict_openai` provider rejects missing provider finish state before
LiteLLM can turn it into a successful finish. This is the approved gateway-only
LiteLLM import exception. Served model IDs and application contracts remain the
same; production's platform gateway must provide equivalent protection.

The production SCC fixes remove fixed Qdrant/Jaeger identities and make the
application's required cache/work paths group-0 writable. Jaeger uses `Recreate`
for its single RWO volume, avoiding concurrent-writer locks during redeployment.
The dedicated OAuth pin and registry service-account authentication make the
sidecar part of the signed candidate, rather than an unresolved release input.

### Image identity across archive and registry formats

Record the requested upstream pin, platform/archive manifest digest, image config
digest, loaded registry manifest digest and actual pod `imageID`. Docker archives
can contain uncompressed layers while the registry stores compressed layers;
those manifest digests can differ. Verify the archive signature/checksums and
its manifest against the bundle first, then verify identical image config/rootfs
diffIDs across the load, and the running digest against the loaded registry.
Do not compare an archive digest blindly to a registry digest or accept a tag
alone. Capture all five images, including every OAuth sidecar.

<a id="deployment-policy"></a>
## Deployment maintenance policy

Hard boundaries from the root guide remain binding. CPython is 3.14 GIL;
Qdrant server/client/chart track 1.19.0 and the unprivileged image. The connected
main factory is the only product image builder. App references use one exact
`ghcr.io/<owner-lowercase>/qdrant-pdf-rag-{ingest,agent}:<full-git-sha>` across tag,
push, render, pack and load (retag local `mainframe-rag/*` images before push).
Never short SHAs, latest product tags, `helm repo add` or builds in the gap.
Tests explicitly supply pending/recorded OAuth pins; do not assume the production
pin is pending. Lockfile/pin bumps are dedicated changes; refresh wheelhouse/BM25 on CPython 3.14
and rebuild connected images. Do not add unused dependencies or unpublished
extras such as `types-httpx2`; LiteLLM remains outside product dependencies.

The platform owns production models/gateway/Splunk/GPU operators. Local model
and gateway launchers stay out of product images/Helm/air-gap paths. The sole
LiteLLM import exception is `scripts/gateway/strict_finish.py` inside the pinned
local/CI gateway image: observe upstream finish, close the stream, fail closed
when state is unavailable, retain clean-EOF fault checks on pin bumps. Production
protection remains the platform's responsibility.

Preserve separate CI/prod Qdrant values and sizing. CI synthetic hash jobs never become
prod ingest; production has no EMBED_MODE key, RWO block data/work storage,
read-only caller corpus and two agent replicas. Qdrant chart IDs are explicitly
null; Jaeger uses project-assigned IDs and `Recreate` for its single Badger writer.
App source is read-only; required caches/work paths are group-0 writable. Never
repair admission with `anyuid`. Verify an old trace survives replacement.

Keep placeholders in git, render fails closed on leftover tokens, quote scalar
environment values, and wire PULL_SECRET to Qdrant **and** agent/ingest. Deploy
and ingest arguments reject unknown flags before operations. Snapshot administration
runs in a temporary maintenance Job using the ingest image and write-key Secret;
serving remains read-only. Jobs are deleted before re-apply because they are
immutable. TLS bundles retain every required trust root and mount only into the
application containers; missing ConfigMap/key fails closed. Gateway secrets use
per-leg Secret references, never plaintext keys in airgap.env.

Install/bootstrap, pipeline, migration and recovery recipes stay in their existing
owners: [install](install_and_ops.md), [ingest](ingest.md#publication-contract),
[local recovery](local-real-corpus.md), [CRC gate](crc-release-verification.md).
Qdrant 1.19 snapshot file URIs must be under the configured snapshot directory;
require completed recovery, provenance/count/dimension and vector compatibility
before an alias. Never mix synthetic cleanup with preserved real-corpus resources.
Transfer the identical tested bundle; missing/failed manual CRC checks block
promotion. Sizing-only overrides do not establish SCC, residency or distributed HA.

<a id="configuration-contract"></a>
## Operator configuration propagation contract

**Status:** implemented paths with shell/runtime test evidence; live deployment
acceptance remains separately required. **Authority:** #245, #246–#248, #362/#391,
and the current model-ownership agreement. **Decision owners:** `common.sh`
override/require/normalization helpers, `validate.sh`, deploy/ingest renderers and
`config.Settings` / `representation.require_attested_revision`.

**Producer → state → consumers:** operator setting → `airgap.env.example` and
explicit environment → `OPERATOR_ENV_KEYS` snapshot/restore around file loading
→ `require_env` plus blank/preflight validation → generated Helm values → schema validation and chart rendering
→ both agent and ingest environments → Settings and operation-specific interpretation. Trace every step
for a newly required input; an example alone or agent-only render is insufficient.
Maintenance modes (issue #391 current packet) follow the same path:
`INGEST_ALIAS_PUBLISH`, `INGEST_REINGEST` and `INGEST_RETIRE_DOCS` are
`OPERATOR_ENV_KEYS` validated by `scripts/airgap/ingest.sh` and rendered into
the ingest Job args/env (`INGEST_RETIRE_DOCS` accepts comma- or newline-separated
`DOCID[@SOURCEREV]` entries and preserves interior whitespace in product/version labels);
the example documents them commented out, no default
flips, and the shared ingest-work progress path is fixed so one authorized
publisher at a time is enforceable (host-local target lock, no distributed
lock claim). Replacing the immutable Job waits for foreground deletion of its
prior pods before applying the replacement; a deletion failure aborts the
launcher. The scratch PVC and its durable progress remain intact. The log
overlay follows successive retry pods and stops its owned stream when the Job
wait returns; Job completion, not log activity, determines success. SIGTERM or
SIGINT to the launcher terminates and reaps its local wait, poll and log children
and exits with status 143 or 130. Cancellation does not delete the cluster Job:
`job/ingest` in the selected namespace may still be running. Inspect/reconcile
that Job explicitly; the supported replacement launcher still waits for its
foreground deletion before starting another writer. CI uses
explicit synthetic values, never an attestation bypass. No
real registry, URL, token or private environment file enters git or the transfer
artifact.

**Collection distribution policy (issue #360):** `QDRANT_SHARD_NUMBER`,
`QDRANT_REPLICATION_FACTOR` and `QDRANT_WRITE_CONSISTENCY_FACTOR` follow the
same operator path — concrete example values, `OPERATOR_ENV_KEYS`
snapshot/restore, Task `TASK_*` bridges (pinned equal by
`test_bridge_set_matches_operator_keys`), effective-tuple validation in
`validate.sh`/`ingest.sh`, rendered only into the ingest Job (the agent
never creates collections, so the agent render deliberately excludes them).
Precedence is explicit caller > operator file > checked-in production default
(`overlays/openshift/collection-policy.env`, 6/3/2); partial overrides
inherit the remaining preset values (explicit 1/1/1 is the supported
one-node profile, never inferred). Values reach
`Settings` and both collection constructors verbatim; shard defaults are not
reconstructed from pod count or an assumed server default. Live placement
verification exists (`scripts/verify_placement.py`,
[collection policy](#collection-policy)) and the cutover gate enforces
ACTIVE copies in process (`publish.verify_staging_placement`, fed by the
`QDRANT_PEER_URLS` operator setting);
snapshot-gated migration of existing collections and the recorded
node-loss/site qualification remain later slices.

Set `QDRANT_PEER_URLS` to a comma-separated list of non-secret direct REST
endpoints reachable from the ingest Job. The shell and Task launchers preserve
caller-over-file precedence and the exact string through mapper
`ingest.peerUrls` to the Job environment. The runtime parser trims each entry;
the existing placement gate requires distinct, reachable peers with the
required ACTIVE copies on both collections. No peer endpoints are inferred
from the Service or replica count. An unset list remains unconfigured and
cannot certify RF>1 placement. Peer clients use the existing ingest credential;
the serving Deployment does not receive this writer-side setting.

**Absent/blank semantics:** `common.sh` currently gives non-empty explicit env
precedence and otherwise permits file/default resolution; required attestation
rejects whitespace. Runtime bearer auth intentionally omits a header for an
absent/empty/whitespace key. These are different owners/meanings: do not globally
normalize blanks without tracing both consumers. `resolve_otel_endpoint` owns the
separate unset=local-Jaeger and off/none/false/0=disabled policy across stages.

**Preconditions/failures/lifetime:** platform-supplied model/dimension/revision and
per-leg credentials must match the deployment. Gateway aliases are mutable and
cannot attest weights. Secrets reach pods via Secret refs; rotations require the
runbook's restart/verification. Local `GATEWAY_ENV_FILE` is launcher-owned and
provides gateway URLs/model IDs/keys plus a local simulation revision label.
Explicit CLI/Task/env model or endpoint overrides must apply or fail; ambiguous
model discovery fails closed. No new production value is invented here.

**Evidence:** `tests/test_airgap_validate_sh.py`, `test_airgap_deploy_sh.py`,
`test_airgap_ingest_sh.py`, `test_airgap_pipeline_sh.py`, `test_config.py` and
`test_representation_gate.py`. Render tests prove wiring under their fixtures;
real trust/identity/model readiness still needs the application-pod gateway probe
and release acceptance. `/healthz` plus search does not prove reasoning readiness.
Known metadata/publication gaps remain #391; broader CI enforcement remains #370.

<a id="ci-policy"></a>
## CI and reviewer maintenance policy

Keep root `.gitlab-ci.yml` and GitHub `ci.yml`; shared product hygiene/test meaning
changes together. GitLab uses mirrored CI_PYTHON_IMAGE/CI_RUNNER_TAG and internal
PIP_INDEX_URL or PIP_FIND_LINKS, failing closed without a package source. No
public network, GHCR, deploy/pack/image-build or opencode reviewer in GitLab.
Issue #397 authorizes the offline context check in hygiene and a small GitHub
context workflow; general lint/type/coverage/ruleset changes remain #370.

Third-party actions use full SHA pins; document runtime downloads outside that
pin. Runtime artifacts need pinned versions and in-repo SHA256 verification;
never unpinned curl-to-shell in secret/id-token jobs. Invoke pinned tools by
absolute path. Policy requires every job to declare least-privilege permissions,
bounded timeouts, concurrency with a run-ID fallback, secret/fork guards where relevant,
and third-party sharing off. Existing enforcement gaps are not permission to
claim compliance. Reviewer install steps remain inline: local composite actions
can re-resolve a moved checkout during post-processing.

Rehearsal lanes bootstrap the same downloaded bundle in fresh directories; never
repack. Bracket every injected fault with healthy control/recovery. Retire the
old mock pod and verify requested state through its Service from the gateway pod;
readiness alone cannot prove which fault is served. Project gateway config/modules
through one volume, without nested read-only subPath mounts. Wait for an
authenticated certificate-verified Service read before key creation; only transient
connection startup is retried within the setup deadline, never key/model requests.
Streams need content, successful finish and DONE; faults/truncation fail.

Connected E2E stays in `e2e.yml`, with ephemeral namespaces and always-run cleanup.
The opencode inline prompts are repository-controlled review entry points;
they must follow [the review contract](agent-workflow.md#review-handoff), inspect
affected unchanged callers and classify evidence gaps honestly. Their actual
loader/environment acceptance remains an explicit audit item under #397.


### Local follow-up and tokenizer acceptance

`CHAT_CONDENSE_ENABLED` is an explicit boolean operator input mapped to Helm
`models.reasoning.condenseEnabled` and the agent environment. It preserves the
application's false default. Enable it deliberately when qualifying pronoun-only
console follow-ups; require a grounded answer after the extra reasoning call.
The local gateway's authenticated `/tokenize` passthrough enforces the per-leg
key's model allowlist. Use `probe_gateway.py --require-tokenizer --stream
--require-reasoning` and the [strict local check](local-real-corpus.md#4-verify-and-retain-the-complete-live-stack)
for local full-stack acceptance. Platform-owned gateways may instead support
the documented estimator fallback; that is not exact prompt-budget evidence.

The pipeline's final banner reports only the stages it executed. Dry runs say
live acceptance **NOT RUN**; a skipped ingest stays **NOT RUN**. Successful
smoke stages do not certify grounded answers, console follow-ups or an exact
production release. Complete the applicable live-stack acceptance separately.
