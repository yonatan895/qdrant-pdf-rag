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
- The final banner differs: `PIPELINE STAGES COMPLETE: deployment, ingest and smoke passed`
  when ingestion ran, `PIPELINE STAGES COMPLETE: deployment and smoke passed; ingest NOT RUN`
  when it did not.

Standalone `sh scripts/tools/run-task.sh airgap:deploy` only waits for workload readiness. The agent
`/healthz` check covers Qdrant and embedding connectivity, the served
generation's representation contract, **and** the rerank leg when
`RERANK_ENABLED=true` (HTTP 503 for any non-servable state);
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
  `git rev-parse HEAD`. Explicit model-operation URLs win over
  `GATEWAY_BASE_URL`, a shared API base including `/v1`; the legacy
  `EMBED_BASE_URL` fallback derives from the vLLM URL with
  trailing slashes and a trailing `/v1` stripped.
- `require_env` collects **all** missing keys before failing, so one run
  tells the operator everything to fill in. `EMBED_MODEL_REVISION` is a
  required key on every air-gap launch path (vllm mode refuses a blank
  attestation); `validate.sh` additionally rejects whitespace-only values.
- Product rules: `EMBED_MODE=hash` dies (case-sensitive match on that exact
  string); storage classes containing `nfs` (any case) die — both
  `STORAGE_CLASS` and `SNAPSHOT_STORAGE_CLASS` are checked (the latter
  defaults to the former); the corpus PVC is not.
- Operator booleans `UI_ENABLED`, `CONTEXTUAL_EMBED_ENABLED`,
  `INGEST_ALIAS_PUBLISH`, `INGEST_REINGEST` and `CHAT_CONDENSE_ENABLED`
  accept only `true/1/yes` or `false/0/no` (any case; unset keeps the
  default). `validate.sh` rejects any other value before the pipeline pushes
  images; the diagnostic names the key, never the value. One parser
  (`model_config.parse_strict_bool`) serves preflight and `map_values.py`.
- `validate.sh` and `deploy.sh` need only a namespace-admin identity (#678,
  #680). The validate reachability probe is
  API discovery (`get --raw /api`): an anonymous or invalid identity fails, and
  a namespace-scoped one passes. It does not use `cluster-info` (kube-system
  Services) or `/version` (anonymous).
- Live cluster probes in `validate.sh` classify the client error: a
  `Forbidden` StorageClass read is a notice (existence unverified, not
  "absent"); a `Forbidden` Namespace read falls through to the Secret check
  in the namespace; a `Forbidden` Secret read fails; only `NotFound` means
  absent. `get scc` succeeding means OpenShift, a missing resource type means
  standard Kubernetes, and a denied or failed read leaves the cluster type
  undetermined (never reported as non-OpenShift).
- `scripts/airgap/model_config.py` owns pure validation of the resolved model
  inputs for both preflight and the values mapper. A reasoning alias requires
  a resolved reasoning/shared URL; enabled contextual embedding requires its
  URL and model. Invalid endpoint schemes or required model/URL pairs fail
  before the pipeline reaches image loading. Diagnostics name the field, not
  its value. Python 3 is required on the bastion for this check as well as
  mapping. URL resolution and caller/file precedence remain `common.sh`-owned;
  rerank's existing embedding-URL/model fallback is unchanged.
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
Deploy also reads back every deployed image from the registry before any
cluster change, so `pipeline.sh --skip-load` and standalone `airgap:deploy`
are verified exactly like a fresh load (see [image identity](#image-identity-across-archive-and-registry-formats)).
Deploy requires exactly one `charts/qdrant-*.tgz` (none or several is refused
before any release command). When a packed `MANIFEST.txt` is reachable and the
run is not a dry-run, that archive's sha256 must equal the MANIFEST
`chart_sha256`; a mismatch or missing field fails closed. Dry-run and
no-MANIFEST runs print a "not release-verified" notice instead. Pack refuses
a checkout with zero or several chart archives, so the MANIFEST binds one.
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

- Console Route reconciliation (issue #373). `AGENT_ROUTE` is a strict boolean
  (`AGENT_ROUTE must be true/false`). `validate.sh` and `deploy.sh` share one
  preflight (`common.sh`, `check_route_exposure.py`): with `AGENT_ROUTE=true` the
  `images.txt` oauth-proxy pin must be recorded, match the chart's
  `images.oauthProxy` repository/tag and, when a packed MANIFEST is reachable,
  equal its `oauth_proxy` member; `rag-agent-oauth-cookie` must carry a nonempty
  `cookie-secret` key (its byte length is enforced by oauth-proxy at start; the
  rollout wait surfaces a rejected value) and the namespace service CA must be a
  PEM bundle. Every Route in the namespace is listed before mutation (a deployer
  that cannot list Routes fails closed): any Route other than the owned
  `rag-agent` Route whose backend or `alternateBackends` is the agent or a
  Qdrant Service is refused in both Route-on and Route-off, and the owned Route
  with `alternateBackends` is refused when Route-on. Before release work,
  deploy classifies the owned Route against the generated OAuth contract and
  deletes a confirmed incompatible Route; a valid Route and its operator
  host/certificate fields stay. Foreign ownership is refused. After the application
  release, deploy re-reads the live Route and requires Service `rag-agent`,
  `targetPort: oauth`, `reencrypt`, `insecureEdgeTerminationPolicy: Redirect`,
  the generated destination CA and no alternates; a readable owned mismatch
  is deleted and fails. An unreadable Route or changed ownership fails with an
  unknown/unverified exposure diagnostic; this does not prove absent exposure
  and does not authorize deleting an operator object. With Route-off an
  existing owned Route is deleted before the first release mutation, not after
  the rollout wait. Not covered here (site evidence, see the issue): the OAuth
  login itself, who is authorized (`--email-domain=*` admits any authenticated
  cluster user; deploy prints a notice) and the unauthenticated 8080 Service
  port.

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
- The one exception (#680) is a disabled ServiceMonitor read that returns
  `Forbidden`. OpenShift's namespace admin role has no `monitoring.coreos.com`
  rights, so this read is a notice: a leftover ServiceMonitor is not checked or
  removed, and deploy continues. A ServiceMonitor is metrics-only and never an
  exposure path, and that identity could not remove one anyway. Any other read
  failure, and every Route, Jaeger and ServiceAccount read failure, still stops
  before mutation.
- Disabled optional workloads, Routes and monitors are removed on upgrades of
  the Helm release. The Jaeger PVC has `helm.sh/resource-policy: keep`: disabling
  tracing or removing the app release retains its data. Re-enabling tracing
  reuses that claim; moving to an external collector sets the destination in
  the same release that removes the backend, and the backend objects are
  deleted only after the rollout; neither application rollback nor uninstall rolls back data.
- `PULL_SECRET` and `GATEWAY_API_KEY_SECRET` remain DNS-subdomain names and
  Secret references. An absent pull Secret renders `imagePullSecrets: []` for
  first-party pods and `imagePullSecrets=null` for Qdrant. An absent gateway
  Secret omits key env entries. Selected refs require nonempty keys before
  mutation: by default agent `llm/embed/rerank-api-key`, ingest
  `embed/context-llm-api-key`, pull `.dockerconfigjson`, OAuth `cookie-secret`.
  With `GATEWAY_API_KEY_SECRET_KEY` set, every model leg references that one
  data-key name instead, and deploy/ingest check that selected key. It requires
  `GATEWAY_API_KEY_SECRET`; neither setting contains credential material.
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
`MANIFEST.txt`, `PACKING_RECORD.txt`, digest enumeration, `THIRD-PARTY-NOTICES.txt`
([licensing](licensing.md); pack fails closed on an unrecorded dependency, image
digest or notice change, and bootstrap requires the member), offline signature,
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
- Load then reads every pushed tag back from the registry (`skopeo inspect
  --raw`, reusing the registry-access options of `SKOPEO_ARGS`/`INSECURE_REGISTRY`)
  and refuses unless the stored manifest is a single image whose config digest
  and layer count equal the packed archive's, whose own manifest hashes to the
  MANIFEST `*_digest`. A tag that resolves to anything else (swapped, mirrored,
  not overwritten, unreadable) fails closed with a fixed message and no
  `Loaded N images` line. Each verified image prints
  `==> verified <ref>@<registry manifest digest>`; that digest is the immutable
  pin, and it differs from the MANIFEST archive digest by design (see
  [image identity](#image-identity-across-archive-and-registry-formats)).
  Dry-run reads no registry and says so ("not release-verified").
  Load also requires each `<image>_config_digest` in the MANIFEST to equal its
  archive's image config before any push. Deploy and ingest repeat the registry
  read-back and render by digest (see the image identity section below).
- Executing-checkout guard (`common.sh::check_checkout_sha`, run by load,
  deploy, ingest and validate). With a packed MANIFEST reachable:
  1. *MANIFEST authenticity.* The guard never reads a bare file. `SHA256SUMS`,
     `SHA256SUMS.sig` and `sneakernet-signing.pub` must sit next to it, the
     signature over `SHA256SUMS` must verify (`SNEAKERNET_TRUSTED_PUB` pins
     the key when set; otherwise TOFU, as in load), and `SHA256SUMS` must list
     `MANIFEST.txt` exactly once with the file's real sha256. Image archives
     stay load.sh's `sha256sum -c`; only this MANIFEST link is rechecked.
  2. *Identity.* HEAD must equal the packed SHA and no tracked file may differ
     from it, staged or unstaged (scripts, charts, Taskfile — the whole
     tracked tree).
  3. *No hidden edits.* `git diff` trusts the index, so any tracked file
     flagged assume-unchanged or skip-worktree (`git ls-files -v` tag other
     than `H`; sparse checkout sets skip-worktree) is refused, with or
     without an edit. An unreadable index is refused. Clear the flags with
     `git update-index --no-assume-unchanged/--no-skip-worktree`.
  Refusals are fixed messages naming neither files nor contents; `git diff
  HEAD` lists edits. Untracked files (`airgap.env`, `dist/`, generated
  output) stay allowed, so site values belong in the untracked `airgap.env`.
  Nothing is reset or overwritten; after the cause is fixed the next run
  passes. A claimed release also fails closed when its identity cannot be
  established: an unverifiable MANIFEST or one without a `sha:`, a checkout
  git cannot resolve (a copied tree without `.git`), or bundle evidence with
  no readable `MANIFEST.txt` — so launching load/deploy/ingest/validate
  directly, setting `IMAGE_SHA` to the packed SHA, or deleting/forging
  `MANIFEST.txt` never bypasses checkout identity; rerun `bootstrap.sh` to
  restore it. *Bundle evidence* is `dist/SHA256SUMS`, or a `../SHA256SUMS`
  that names `MANIFEST.txt` (the documented unpack-next-to-the-clone layout
  that `find_manifest` and load.sh search; every packed bundle's list names
  it). An unrelated `../SHA256SUMS` that does not name `MANIFEST.txt` is not
  evidence, so connected development in a directory beside such a file is not
  refused. Counting evidence can only make the guard refuse, never pass.
  Only dry-run and connected development (no MANIFEST and no bundle evidence)
  still run, printing "not release-verified" on stderr — never a
  release-verified result. `bootstrap.sh` applies the same tracked-change and
  index-flag refusals to an existing workspace before copying artifacts into
  `dist/`. Not covered: the guard is not tamper-proofing against an actor who
  can rewrite the whole bundle directory *and* its pubkey while
  `SNEAKERNET_TRUSTED_PUB` is unset (TOFU), nor against edits to files the
  checkout does not track (`airgap.env` is operator config by design).
- `bootstrap.sh` cannot source `common.sh` (no clone exists yet), so it
  carries an inline twin of the trust check (bundle signature honoring
  `SNEAKERNET_TRUSTED_PUB`, then `SHA256SUMS`). It clones into
  `AIRGAP_WORKSPACE` (default `qdrant-pdf-rag`), skips the clone when a
  repo already exists, copies (not links) the archives into `dist/`, and
  seeds `airgap.env` from the example only when absent — never overwriting
  operator edits. Artifact discovery searches `dist/` then the parent dir.
  Artifacts are staged in `dist/.bootstrap-staging`, verified there (signature
  and every member checksum), and only then moved into `dist/`, with
  `SHA256SUMS.sig`, `SHA256SUMS` and `MANIFEST.txt` last, so a failed copy or
  a failed verification leaves `dist/` and `airgap.env` byte-identical and
  an interruption never yields a mixed bundle that verifies. `dist/` is never
  swapped wholesale: operator files and earlier releases' archives stay. If
  the process was killed uncleanly the staging directory remains. A failure
  before promotion preserves existing `dist/` bytes; interrupted promotion
  may have replaced members before the acceptance metadata moved. The next run
  refuses the stage and warns that `dist/` may be partial. Do not use it: remove
  only `dist/.bootstrap-staging`, rerun the complete verified bundle and require
  signature/member verification before load. Bootstrap never deletes a leftover
  stage on its own initiative; operator files remain through recovery.
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
- First-party pods (agent, the oauth-proxy sidecar, the ingest Job, Jaeger)
  declare their own hardening so it does not depend on `restricted-v2`
  alone (issue #585): pod `runAsNonRoot: true` and
  `seccompProfile: RuntimeDefault`; every container
  `allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]`. The chart
  never sets `runAsUser`, `runAsGroup` or `fsGroup`; OpenShift assigns them.
  The agent and ingest images declare numeric `USER 1000`.
  `readOnlyRootFilesystem` with `/tmp` emptyDirs is not yet set: it needs a
  Kind run proving nothing else writes to the root filesystem.
- Jaeger is on by default (unset `OTEL_EXPORTER_OTLP_ENDPOINT` resolves to
  `http://jaeger:4318`; the off sentinel disables both tracing and this
  deployment; `JAEGER_ENABLED=false` skips only the deployment and is
  refused unless `OTEL_EXPORTER_OTLP_ENDPOINT` names a collector or `off`,
  in preflight, mapper and chart alike): 1 replica,
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
  span lands in `JAEGER_QUERY_URL` within `TRACE_TIMEOUT` (default 60s); the `off`
  sentinel skips that assertion. With the bundled Jaeger disabled the query is
  skipped and the report says trace arrival is NOT VERIFIED: configuring a
  collector is not proof that spans reach it.

<a id="collection-policy"></a>
### Capacity arithmetic (issue #272)

Provisioned claims by default (`charts/qdrant-openshift.values.yaml`, chart
values; the three Qdrant peers each get their own claims):

| Claim | Per unit | Units | Total |
|---|---|---|---|
| Qdrant data (RWO block) | 500Gi | 3 peers | 1500Gi |
| Qdrant snapshots (RWO block, node-local) | 500Gi | 3 peers | 1500Gi |
| Ingest scratch `ingest-work` (`INGEST_WORK_SIZE`) | 100Gi | 1 | 100Gi |
| Jaeger Badger (when enabled) | 10Gi | 1 | 10Gi |
| **Provisioned PVC capacity** | | | **3110Gi = 3.04TiB** |

Qdrant alone is `3 * (500Gi + 500Gi) = 3000Gi = 2.93TiB`, not 500Gi. The
read-only corpus PVC is caller-owned and not counted. This is provisioned
capacity, not usable logical capacity:

- With 6 shards, replication factor 3 on 3 peers, every peer stores one copy of
  every shard. The logical corpus (corpus plus `__completions` collection) is
  therefore bounded by **one** peer's data claim (500Gi), not by 1500Gi.
  Let `S` be one generation's on-disk size on a single peer.
- Alias publication keeps the retired generation until the operator removes it,
  so a refresh peaks at `S_old + S_new` plus WAL and optimizer scratch on the
  same claim: the data claim must hold at least `2 * S` with headroom, i.e. `S`
  stays well below 250Gi at the default size.
- Each node-local snapshot of a generation is about `S` on that peer's snapshot
  claim: the retained snapshots (pre-change, pre-upgrade, safety) satisfy
  `retained * S` below 500Gi. Off-cluster copies do not free the claim until the
  snapshot is deleted.
- Sizing math, not measurement: measured per-point footprint, headroom, RPO/RTO
  and replica policy belong to #374 and #360. Do not provision at exactly the
  estimate (`.agents/skills/qdrant-sizing`).
- Claim expansion needs a StorageClass with `allowVolumeExpansion: true`. The
  StatefulSet's `volumeClaimTemplates` are immutable, so a larger value in the
  chart does not resize existing claims; the procedure is in the
  [upgrade and recovery runbook](install_and_ops.md#upgrade-and-recovery-runbook-issue-272).

### Collection distribution and placement (issue #360)

**Production default (checked in, non-secret):** 6 logical shards,
replication factor 3, write consistency 2, across the three Qdrant peers,
applied to both the corpus collection and its paired completion/control
collection. `scripts/airgap/collection-policy.env` carries the tuple;
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

The deploy launcher refuses a missing or unreadable
`charts/qdrant-openshift.values.yaml` before running Helm or cluster commands.

**Placement is a hard requirement:** `charts/qdrant-openshift.values.yaml` sets
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
degraded. `QDRANT_PEER_URLS` flows operator -> mapper -> chart
`ingest.peerUrls` -> ingest Job environment (#507).

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
order. Rejoin requires ACTIVE placement on three distinct peers, then exact
corpus/control reads within the existing 60-second convergence waits before
re-qualifying; ACTIVE placement alone is not data catch-up. Three
containers on one host prove distributed software behavior, not
independent-worker or site tolerance. Existing one-node CRC/Kind lanes and
the three-worker lifecycle lane are not distributed acceptance. The lane
further demonstrates W=2 acknowledged writes under one-peer loss (exact
corpus/control read-back via survivors and on every peer after rejoin),
identical-ID retry convergence, and 2+1 partition behavior (minority writes
never acknowledge; majority writes stay exact; healed peers converge to one
exact state, so retries must reuse identical IDs and identical content).

<a id="distributed-recovery"></a>
#### Distributed recovery boundary (issue #360)

Qdrant collection snapshots are node-specific and do not carry aliases, so
a snapshot recipe cannot prove that every shard of a 6/3/2 collection was
captured or restored. Update publications therefore never use snapshots to
prepare staging: `qdrant_io.clone_collection` copies every point (id, named
vectors, payload) and completion marker through the points API into a new
collection created with the selected policy, and refuses unless the copy
holds exactly the source's point ids; the cutover gates then verify the
staging pair's distribution and placement. The legacy-layout migration in
`run_ingest` uses the same copy: a physical collection squatting on the alias
name is copied into the retained generation `<alias>__legacy_<build>` and
verified before it is deleted, so its rollback is a real collection, not a
node-local snapshot. Supported recovery from lost distributed storage is
still a fresh complete generation rebuilt from the protected originals
(accepted downtime). Node-addressed restore, replacement-peer
join and replica repair stay unqualified until site evidence exists. The
safety snapshot taken at alias swap is node-local on a distributed source:
it is not a restore point; the retained superseded physical collection is
the rollback.

<a id="site-qualification"></a>
#### Site qualification procedure (issue #360; external, not run by CI)

Run by the authorized site operator against the exact bundle/configuration
that will serve users. Evidence goes to the approved venue (never git:
credentials, hostnames, private corpus content stay out of the repository);
record the exact image digests, chart values, `QDRANT_*`/`QDRANT_PEER_URLS`
values, and the commit SHA of this repository. Any FAIL blocks a production-HA
claim; do not lower RF/W, colocate peers, or relabel a one-node setup to pass.

| Step | Action | Evidence to capture | Pass / fail |
|---|---|---|---|
| 1 Placement map | `kubectl get pods -o wide -l app.kubernetes.io/name=qdrant` and `kubectl get nodes -L topology.kubernetes.io/zone,kubernetes.io/hostname`; map peer id (from `/cluster`) -> pod -> worker -> storage volume/backing device | Table peer/pod/worker/zone/PV/storage class | PASS: 3 distinct workers; the site names the independent failure domain (hypervisor/rack/storage array) each worker sits in. FAIL: any two peers share a worker or an undeclared domain; hostname separation alone is recorded as "distinct hosts, domain unproven" |
| 2 Storage | `df`/PVC capacity and used per peer for data and snapshots; storage class access mode and semantics (RWO block, not shared NFS) | Capacity/used/headroom per peer; storage class YAML | PASS: RWO block on each peer, headroom >= sizing below with margin for staging plus retained generation. FAIL: shared storage between peers, or headroom below one extra generation |
| 3 Policy and placement | `scripts/verify_placement.py --production` with `QDRANT_URL` the Service and three direct `--peer-url`s, for the live alias and for each staging pair before cutover | Full verifier output including the `VERDICT:` line and exit code | PASS: exit 0, VERDICT healthy, 6/3/2 on corpus and control. FAIL: any other verdict (degraded/recovering/unverifiable/unservable) |
| 4 Fresh and update generations | Canonical Task/launcher ingest from the protected originals into an empty alias; re-run it unchanged (steady-state re-verify) and restart the ingest Job; then change one source document and run it again (update publication) | Job logs (`action: publish`, `staging_mode`), points count, second-run no-op log, update-run alias target | PASS: first publication succeeds, repeat re-verifies read-only, restart is a no-op, the update publishes a new 6/3/2 generation (`staging_mode: cloned`) while the superseded one is retained, counts equal across all three peers. FAIL: any snapshot/recover path reached, the update refuses, or counts differ |
| 5 Exact data | Per peer (direct URL) scroll the corpus and control collections and compare point ids, payload hashes and vector checksums against the first peer; run the golden/real-corpus retrieval check through the alias | Per-peer id/payload/vector digests, retrieval report | PASS: digests identical on all peers, retrieval within the accepted baseline. FAIL: any divergence |
| 6 Write acknowledgement | Through the entry Service, upsert a marker batch with `wait=true` (W=2), record the acknowledgement; repeat while exactly one peer is stopped by the site | Ack responses, marker ids, read-back from every peer after rejoin | PASS: acknowledged writes succeed with one peer down and every acknowledged id is present exactly on all three peers after rejoin (RPO 0 target). Unacknowledged/ambiguous writes are retried with identical ids and converge without duplicates. FAIL: any acknowledged write missing |
| 7 Peer loss | One authorized peer loss at a time (pod delete or node cordon+drain; never storage destruction): measure time to first successful search through the entry Service, then verifier with `--allow-degraded` | Timestamps, search results, degraded verdict output | PASS (targets, not SLAs until measured here): reads recover within the recorded bound (target 60 s); verdict `degraded` not `unverifiable`. FAIL: reads unavailable beyond the bound, or any verdict besides degraded |
| 8 Rejoin | Restore the peer; poll the verifier (strict) until healthy, then repeat step 5 exact-data comparison and one ordinary read plus one ordinary publication re-run | Time to healthy (target 180 s on the small fixture; record the site-sized value), step 5 digests | PASS: healthy and digests identical. ACTIVE alone is not convergence (step 5 is required). FAIL: not healthy within the agreed window, or digests differ |
| 9 Sequential maintenance | Evict peers one by one, waiting for step 8 before the next | Per-eviction verifier output | PASS: no shard ever has fewer than 2 ACTIVE copies; never two peers down. FAIL: PDB-only reliance without recovery wait |
| 10 Replacement and restore | Only if the site wants to qualify it: replacement of a peer with empty storage through the supported remove-shards/join procedure, and node-addressed restore, each with step 5 digests plus control/alias/inventory reconciliation | Procedure transcript, digests | Not required for the frozen-generation POC. Until recorded PASS, the supported recovery is a fresh rebuild with accepted downtime |
| 11 Report | Fill the venue record: topology, storage/headroom, read/write behavior, measured recovery times, objectives accepted or waived | Signed record linked from #360/#374/#272/#447 | A missing step is recorded as not run, never as pass |

Steps 6-9 are destructive-adjacent and need explicit site authorization; this
repository performs none of them.

<a id="sizing-note"></a>
#### Sizing note and HA-scope decision (issues #360, #374, #582)

Measured input: the 2026-10-02 full re-ingest (`manuals_b5c1756`, 452 docs)
holds 190,440 points of 1024-d dense vectors. The dense vector is
190,440 x 1024 x 4 B = 0.78 GB float32 (0.73 GiB) and about 0.20 GB as int8
scalar quantization (the collection's configured quantization). Dense and
sparse vectors are `on_disk`. Payload text, the BM25 sparse vector and the
HNSW graph are **not yet measured**: record the real per-peer
`data` usage from step 2 above before relying on any total.

| Quantity | Value |
|---|---|
| Logical dense vectors, float32 / int8 | 0.78 GB / 0.20 GB |
| Points per shard at 6 shards | about 31,700 |
| Copies at RF3 on 3 peers | every peer stores every shard: one full copy per peer; cluster total 3 x logical (dense 2.3 GB float32) |
| During an update publication | live plus staging coexist (about 2 x per peer) plus retained superseded generations until the operator removes them |
| Configured per peer | 500 Gi data, 500 Gi snapshots, 16 Gi RAM request / 32 Gi limit |

Even with 3 retained generations the dense vectors are a few GB per peer,
under 1 % of the configured data volume and well under the RAM request, so
the current 6/3/2 topology is not capacity constrained; the sizing margin is
large and a smaller request/limit is possible but is a capacity decision for
#374 after real per-peer usage is measured, not a change made here.

**Maintainer decision requested (HA scope; no change is made in code).**
Replicas buy availability; shards buy scale. With 3 peers and RF3, every
peer already holds every shard, so the 6 shards add no availability and no
storage spreading today: they only fix the maximum spread for a future
peer-count increase (shards can move to up to 6 distinct peers) and give
about 31,700-point units for transfer/recovery. At the measured size one
shard per peer would suffice. Options:

1. Keep 6/3/2 (current decision). Shard-count headroom is kept; a later
   change from any other shard count needs a rebuilt generation.
2. Move to fewer shards (for example 3/3/2) at the next full rebuild: smaller
   per-collection overhead and fewer recovery units, loses headroom beyond
   3 peers. A shard-layout change is a distinct physical generation, never an
   in-place edit, and never an embedding-model change.
3. Keep availability scope explicit: RF3/W2 tolerates one peer at a time;
   it does not tolerate simultaneous failures, shared-storage loss or loss of the
   platform model/gateway tier.

Recommendation: option 1 unless the corpus is expected to stay near its
present size for the lifetime of the deployment and the maintainer prefers
fewer recovery units; either way site steps 1-9 apply unchanged. No
topology default changes until the maintainer records the decision.

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
- Application transfer uses the signed bundle and verified `skopeo` loading
  path above; this repository ships no alternate mirroring configuration.

### Shared LiteLLM configuration

`GATEWAY_BASE_URL=https://sample-api/v1` illustrates the shared API-base
convention; replace the example URL with the platform endpoint. The operator
loader resolves unset `EMBED_BASE_URL`, `LLM_BASE_URL`, `RERANK_BASE_URL` and
`CONTEXT_LLM_BASE_URL` to it before rendering. Explicit operation-specific
URLs take precedence. A caller value beats the operator file for the same
setting. With no shared setting, the legacy vLLM embedding-origin fallback
and existing per-operation behavior remain available. Clients append operation
paths without adding another `/v1`.

Model settings are opaque gateway aliases: for example `EMBED_MODEL=embedding-v1`
and `LLM_MODEL_REASONING=code`. No upstream-name lookup or intent-based routing
is performed. Reranking and contextual embedding remain opt-in and require
their own configured model aliases when used. Embedding dimension and immutable
revision remain explicit; switching an alias's underlying representation still
requires the existing deliberate migration.

`GATEWAY_API_KEY_SECRET_KEY=api-key` selects one data key in the Secret named by
`GATEWAY_API_KEY_SECRET`. Helm exposes that same Secret reference through the
existing per-operation runtime variables, including contextual ingestion.
Direct Helm users set `gateway.apiKeySecretKey`; an empty value preserves legacy
per-leg data keys. The API key value never enters Helm values or `airgap.env`.

## 7. CI inventory

GitHub runs unit, sim, gates, bench, load, the path-filtered multi-peer HA
lane (issue #360: three-peer placement/peer-loss fixture), connected E2E,
and the dry-run gate; air-gap GitLab runs hygiene, lint/types, the full
pytest suite, hazards, the sim tier and gate-l1 only (no e2e, load, deploy,
multi-peer docker, GHCR, or PDFs/tokens/hostnames in file). GitLab unit/sim
reports reject any skipped or xfailed test (`scripts/check_junit_clean.py`);
`tests/test_ci_gitlab_parity.py` pins the correspondence and failure propagation. Job meaning stays aligned across the two files; only e2e-scale jobs
live in `.github/workflows/e2e.yml`.

- GitHub product `ci.yml`/`e2e.yml` run on every PR/push with no markdown-only
  ignore (only `bench.yml` carries a `paths-ignore` for docs). The
  `agent-context.yml` lane likewise runs on every PR/push with no `paths:`
  filter: it always runs `qa:context` plus the dependency-free context
  unit tests, including for root-only AGENTS changes, without model/image/deployment work.
  It also runs for vendored-only changes (no path exclusions). Mixed changes retain
  existing product CI/E2E behavior. GitLab hygiene runs the same offline checker.
- GitHub unit/context/review lanes explicitly install the pinned host Task into
  `.tools/bin` and dispatch through `sh scripts/tools/run-task.sh`. Runner
  contract lanes set `TASK_CONTRACTS_REQUIRE_RUNNER=1`; absence is a failure,
  never a skipped pass. Offline GitLab runners mount the approved
  `task_linux_amd64.tar.gz` at `CI_TASK_ARCHIVE`; the unit job verifies/installs
  it with `install-task.sh --archive`, with no public download. Prepared jobs
  retain their Python interpreter without `dev:setup`. Preparation uses
  [complete target hash locks](dependencies.md), verifies installed inventory, and
  omits unused pip caching. Runtime images check approved wheel bytes before offline installation;
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
  deselected, split across four runner VMs with an aggregate `test` status
  requiring every shard), sim (docker Qdrant, fail-closed on skips/zero-pass), gate-l1
  with PR delta comment. Least-privilege permissions, timeouts, and
  a top-level concurrency group (cancel-in-progress on PRs); third-party actions SHA-pinned.
  Hazard pairs run in four isolated processes; baseline, mutation and cleanup
  remain ordered within each pair. Connected Python acquisition explicitly
  uses eight bounded download workers across GitHub workflows.
- `load.yml`: path-allowlisted to agent/retrieve/ingest/mock/sim/loadtest
  surface and shared dependency/Task inputs — other paths run nothing. `ha.yml` is path-allowlisted to the
  collection-policy/placement surface and runs the three-peer fixture
  (`qa:ha`) fail-closed (no skips, at least one pass); unrelated changes
  do not pay for three containers.
- `bench.yml`: push-to-main + nightly + dispatch (baseline update with
  repeats); never a PR gate.
  Measurement passes and search/answer load phases stay sequential on their
  dedicated runner: overlapping them would change contention and invalidate
  the approved performance baseline. Signing/packaging likewise waits for
  both complete images; fault and lifecycle transitions remain ordered inside
  each isolated rehearsal case. No GPU work is added.
- `e2e.yml build`: local images retagged to full-SHA GHCR refs; push only
  on `main`/dispatch (PRs build, never push — fork-safe).
  Runtime wheels and BM25 weights prepare in two independent `build-inputs`
  runners; runtime acquisition needs no full dev install. Verified bytes move
  through full-SHA-named, workflow-run-scoped artifacts into two independent
  `images` runners (`ingest`, `agent`). Input names stay stable across attempts:
  failed-image retries reuse successful upstream artifacts, while producer
  retries overwrite their own same-name artifacts. `artifacts:image` re-verifies
  actual transferred wheels and BM25 before each unchanged Containerfile build. The canonical `build`
  job is an all-success join: failed, cancelled or skipped preparation/image
  jobs cannot emit successful packaging identity or image-ref outputs.
  `airgap-package` (main/dispatch): pack + 90-day bundle artifact (PRs
  skip). `airgap-acceptance` (main/dispatch): black-box handoff in a fresh
  dir — digest verify, unpack, bootstrap, manifest/SHA assertions, dry-run
  pipeline with explicit shared-gateway reasoning and legacy embedding-only
  (reasoning explicitly disabled) configurations, both pull-secret branches.
  `kind-live-rehearsal` (main/dispatch) is an eleven-lane matrix described below.
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
| `airgap-acceptance` | Fresh bundle integrity/bootstrap, manifest SHA, explicit shared-reasoning and legacy embedding-only configurations, and both pull-secret render branches |
| `kind-pipeline` | Authenticated registry, real product containers, real LiteLLM/PostgreSQL, synthetic ingest, search and application streams |
| `kind-gateway-chat-{upstream,malformed,truncated}` | Three isolated clusters, one reasoning fault each through real LiteLLM, application failure contracts and healthy recovery |
| `kind-gateway-embed-{upstream,malformed,dimension}` | Three isolated clusters, one embedding fault each through real LiteLLM and healthy recovery |
| `kind-gateway-auth` | Wrong/missing credentials, untrusted CA, hostname mismatch and reasoning deadline, followed by healthy gateway/application recovery |
| `kind-lifecycle-chart` | Three-worker Kind; first-party chart fresh/repeat/update/failure/rollback/redeploy sequence with PVC guards, then the next ordinary pipeline |
| `kind-lifecycle` | Independent three-worker Kind; synthetic snapshot recovery, PVC identity, Qdrant/agent/Jaeger replacement, old trace persistence and repeat pipeline |
| `kind-shared-gateway` | Same live pipeline/probe/contracts/smoke but through the shared `GATEWAY_BASE_URL` fallback and the single `api-key` Secret data-key (issue #551); explicit per-operation URLs stay unset so fallback is exercised |
| Manual Windows CRC | Actual SCC, Service CA, OAuth, Routes, node trust/pulls and runtime egress; record the fit outcome and fallback mode separately |

Only computation behind LiteLLM is deterministic in the Kind lanes. The existing
`mock_vllm.py` uses explicit model IDs, 1024-dimensional embeddings, an explicit
`EMBED_MODEL_REVISION` stand-in (`mock-embed@ci`), synthetic
citation output and controlled faults. Mock citations establish interface
behavior, not answer quality. The mock, CI deployment helpers and gateway module
are excluded from application images and production manifests.

The rehearsal now configures **both** embedding and reasoning URLs through the
real test gateway. The `pipeline`, gateway fault and lifecycle lanes keep
explicit per-operation URLs and per-leg Secret keys; the `shared-gateway` lane
instead sets only `GATEWAY_BASE_URL` and `GATEWAY_API_KEY_SECRET_KEY=api-key`
(one virtual key for both mock models) so `common.sh` fallback and the shared
Secret reference are exercised live. Service existence is awaited before readiness checks. Fault
transitions wait for the requested mock state through the actual upstream Service,
so a rollout's success cannot leave a test hitting the old backend state.
The gateway's configuration and strict-finish module share a projected volume;
there is no nested read-only `subPath` mount to fail during container creation.

`scripts/ci/check_gateway_rendering.py` checks the selected source mode and
parses the final rendered Kubernetes YAML through `kubectl patch --local`
with an empty merge patch. It matches each resource/container/env identity:
agent LLM/embed/rerank and ingest embed/context URLs and mandatory Secret
name/data-key references, including dormant legs. Quote formatting is not
an invariant: the rehearsal's local ingest resource patch can remove quotes
without changing a reference. Missing/duplicate consumers, wrong precedence,
wrong model aliases, optional or plaintext keys, and unresolved placeholders
fail with an attributable nonsecret message. The gate's required
`--context-model` expectation compares both the operator source and ingest
`CONTEXT_LLM_MODEL`, independently of the reasoning alias. The shared-gateway
lane explicitly expects an empty dormant context alias; configured aliases
must round-trip exactly, including delimiters and whitespace. Its retained JSON
projects only five consumer identities, sanitized endpoint locations, Secret reference
names/keys, and fixed errors; no Secret data or unrestricted env/YAML dump.
Local decoding has a 30-second timeout; diagnostic reference/host strings are
bounded to 253 characters, with credential/query/fragment and nonstandard path
data omitted from endpoint projections. These are CI diagnostic controls, not
new model-operation timeouts or production defaults.
The following reasoning/complete-stream and application-contract steps still
must run successfully on the same final candidate bundle. Local rendering or
synthetic signed-handoff tests do not supply that final-tree Kind evidence.

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
Implemented (issue #272), as one chain bound to the signed MANIFEST:

1. `pack.sh` records `<image>_config_digest` for all five images next to the
   archive `<image>_digest`. `load.sh` refuses an archive whose config differs
   from it (before any push), then reads each pushed tag back and requires the
   packed config digest and layer count; it prints `ref@registry-manifest-digest`.
2. `deploy.sh` (every entry path: after `load`, `pipeline.sh --skip-load`,
   standalone `airgap:deploy`) and `ingest.sh` read the registry tag back
   **before any cluster change** and require a single-image manifest with the
   packed config digest. Missing tag, swapped tag, manifest list, or a MANIFEST
   without `<image>_config_digest` (older bundle: repack) stop with a fixed
   message and no mutation. The check shares `load.sh`'s read-back options
   (`SKOPEO_ARGS` access options, `INSECURE_REGISTRY`) and needs `skopeo` and
   `python3`. `image_manifest.py` owns config-digest and layer structure parsing
   for pack/load/deploy/ingest; load additionally binds the archive manifest
   checksum and compares layer counts across serialization.
3. The first-party images (agent, ingest, Jaeger, oauth-proxy) are then rendered
   as `repository@sha256:<registry manifest digest>` (`IMAGE_DIGEST_<ROLE>` →
   `map_values.py` → `images.<name>.digest`; the chart keeps the SHA tag only as
   the informational release identity). The digest only ever comes from this
   verification, never from the caller's environment.
4. The vendored Qdrant chart cannot render a digest (it appends
   `-unprivileged` to the tag and semver-compares the tag), and vendored-tree
   edits are not allowed. It stays tag-referenced; its verified digest is
   compared with the running pods instead. After rollout, `check_pod_images.py`
   binds each role to the named release Deployment/StatefulSet and container,
   using controller UIDs, selectors, the current Deployment ReplicaSet revision
   or StatefulSet update revision. It requires every desired replica's regular
   and applicable init-container repository and `imageID`, including completed
   init containers; unrelated Jobs and terminating old Pods cannot supply or
   invalidate this evidence. The deployer needs namespace-scoped list access to
   Deployments, StatefulSets, ReplicaSets and Pods. Missing workload/controller/status, a wrong
   repository, digest or replica count fails the deploy
   before the legacy-resource cleanup. Residual: between the read-back and the
   Qdrant pod pull, a tag could still move; the post-rollout check detects it
   after pods started, it cannot prevent it.
5. Dry-run and runs without a reachable packed MANIFEST (connected
   development) keep tag-only references and print a notice; they are not
   release-verified.

Not covered: signature/provenance of images beyond the signed MANIFEST, and any
registry-side tag immutability (a platform control).

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
alias publication is the default (the example documents the deprecated
`INGEST_ALIAS_PUBLISH=false` opt-out commented out), and the shared ingest-work progress path is fixed so one authorized
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
(`scripts/airgap/collection-policy.env`, 6/3/2); partial overrides
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
separate unset=local-Jaeger and off/none/false/0=disabled policy across stages;
an unset endpoint with `JAEGER_ENABLED=false` fails closed (`map_values.py` and
the chart's empty `tracing.endpoint` mirror it), since the default names the
service just disabled. Any explicit `http(s)` URL, the former hostname
included, is an intentional platform-owned collector.

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
context workflow; #370 owns GitLab/GitHub gate parity, #584 the lint ruleset.
Coverage ratchets and a real internal-runner pipeline run (#446) are not part of it.

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
