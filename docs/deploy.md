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
- `GATEWAY_PROBE_MAX_TOKENS` sets only the readiness completion budget:
  decimal 1–4096, default 16; the Colab lab selects 512 explicitly. The pipeline
  passes it as `--max-tokens` into the pod and rejects invalid values before
  any stage. Task CLI values override ambient/file values as usual. A larger
  budget still requires non-empty final content, `finish_reason=stop`, and
  (with `--stream`) a successful terminal SSE event; reasoning-only and
  length-finished output fail.
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

Production transfer additionally requires [the manual release gate](release-verification.md)
and its [completed evidence record](release-record.md). The existing scripts
do not enforce that record; their success banner is pipeline acceptance only.
Missing or failed gate checks block transfer of the candidate bundle. OpenShift
SCC, OAuth Route, Service CA and isolation checks have no pre-transfer lane; they
block go-live under [site OpenShift acceptance](install_and_ops.md#site-openshift-acceptance).

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
- Defaults: `NAMESPACE` defaults to `mainframe-rag`, `QDRANT_RELEASE` to
  `qdrant`, and an empty `IMAGE_SHA` resolves from `git rev-parse HEAD`.
  Explicit model-operation URLs win over `GATEWAY_BASE_URL`, a shared API
  base including `/v1`. The removed aliases `REGISTRY_INTERNAL`,
  `OPENSHIFT_NAMESPACE` and the `VLLM_BASE_URL` embedding-origin fallback are
  not read: set `INTERNAL_REGISTRY`, `NAMESPACE` and `EMBED_BASE_URL` (or
  `GATEWAY_BASE_URL`).
- `require_env` collects **all** missing keys before failing, so one run
  tells the operator everything to fill in. `EMBED_MODEL_REVISION` is a
  required key on every air-gap launch path (vllm mode refuses a blank
  attestation); `validate.sh` additionally rejects whitespace-only values.
- Product rules: `EMBED_MODE=hash` dies (case-sensitive match on that exact
  string); storage classes containing `nfs` (any case) die — both
  `STORAGE_CLASS` and `SNAPSHOT_STORAGE_CLASS` are checked (the latter
  defaults to the former); the corpus PVC is not.
- Operator booleans `UI_ENABLED`, `CONTEXTUAL_EMBED_ENABLED`,
  `INGEST_ALIAS_PUBLISH`, `INGEST_REINGEST`, `CHAT_CONDENSE_ENABLED`, `LLM_STREAM`
  `FEEDBACK_ENABLED` and `AGENT_NETWORK_POLICY` (default **true**, unlike the others)
  accept only `true/1/yes` or `false/0/no` (any case; unset keeps the
  default). `INGEST_ALIAS_PUBLISH` may only be unset or true: in-place ingest
  was retired (#360) and an explicit false fails closed.
  `validate.sh` rejects any other value before the pipeline pushes
  images; the diagnostic names the key, never the value. One parser
  (`model_config.parse_strict_bool`) serves preflight and `map_values.py`.
- `validate.sh` and `deploy.sh` need only a namespace-admin identity (#678,
  #680). The validate reachability probe is
  API discovery (`get --raw /api`): an anonymous or invalid identity fails, and
  a namespace-scoped one passes. It does not use `cluster-info` (kube-system
  Services) or `/version` (anonymous).
- `validate.sh` probes each distinct data/snapshot StorageClass; unset/empty
  snapshots share the data class. `Forbidden` means existence unverified;
  `NotFound` means absent and fails; other client errors fail. A denied
  Namespace read still checks its Secret; denied Secret reads fail.
  SCC read success means OpenShift, a missing resource type means standard
  Kubernetes; denied/failed reads leave cluster type undetermined.
  Successful class reads prove existence only; NFS-name rejection cannot
  prove backend semantics. The platform owner qualifies RWO block data
  and snapshot suitability at the site.
- `scripts/airgap/model_config.py` owns pure validation of the resolved model
  inputs for both preflight and the values mapper. A reasoning alias requires
  a resolved reasoning/shared URL; enabled contextual embedding requires its
  URL and model. Invalid endpoint schemes or required model/URL pairs fail
  before the pipeline reaches image loading. Diagnostics name the field, not
  its value. Python 3 is required on the bastion for this check as well as
  mapping. URL resolution and caller/file precedence remain `common.sh`-owned;
  rerank's existing embedding-URL/model fallback is unchanged.
- Total request deadline (#843 D6, handoff owned by #374): the chart value
  `requestDeadlineSeconds` defaults to 270 and renders `REQUEST_DEADLINE_S=270`
  on the agent pod, so every air-gap render carries it (the mapper emits no
  override). `Settings` keeps 0 for local/CI. 270 s is strictly below the route
  `timeoutSeconds`, client and `answer_timeout_s` (300 each); a test pins this. Probes are never admitted or limited.
  Operator keys `REQUEST_DEADLINE_S` (0..3600; nonzero must be below the 300 s
  route timeout), `REQUEST_MAX_CONCURRENT`, `REQUEST_QUEUE_MAX` (0..1000) and
  `REQUEST_QUEUE_WAIT_S` (0..300) override or select the chart values
  `requestDeadlineSeconds`, `requestMaxConcurrent`, `requestQueueMax` and
  `requestQueueWaitSeconds` via `model_config.request_overrides` (preflight and
  mapper share it; diagnostics name the key, not the value). Empty means no
  override: the deadline stays 270 and admission is not rendered, so no limit
  applies until the D4 stepped run approves numbers; `0` is an explicit
  selection. Limits are per agent replica.
- Optional application budgets (`LLM_MAX_MODEL_LEN`, `LLM_RESERVED_OUTPUT_TOKENS`,
  `LLM_THINKING_RESERVE_TOKENS_SIMPLE` / `COMPLEX`), reasoning efforts
  (`LLM_REASONING_EFFORT_SIMPLE` / `COMPLEX`) and `LLM_STREAM` follow that same
  precedence/export path into `models.reasoning` and the rendered agent pod.
  `model_config.reasoning_overrides` enforces the existing Settings integer bounds,
  the `low/medium/high/xhigh` effort domain, `LLM_REASONING_EFFORTS` membership
  of both efforts (explicit or default, #748) and strict streaming booleans; chart
  schema also enforces these types and ranges for direct Helm callers, whose pod
  then refuses non-member efforts at Settings startup. The shipped effort is
  `medium` on both routes (#917, maintainer decision 2026-10-23): it is the
  `Settings` default, the `model_config` mirror and the explicit chart values
  `reasoningEffortSimple/Complex`, so a rendered pod always carries it unless the
  operator selects another. `xhigh`
  needs an application build with #707 support. Unset/empty operator values omit
  the new pod env entries, preserving application defaults; explicit zero
  thinking reserves and streaming false remain present. These are application
  settings, not an inference-server reconfiguration: the operator must align
  them with the served model window. Existing UI, condensation and rerank
  selections keep their previous defaults and interpretation. Render/precedence
  evidence lives in `test_map_values.py`, `test_helm_chart_contracts.py` and
  `test_airgap_deploy_sh.py`; live model/deployment acceptance stays separate.
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

Qdrant deploys separately from `charts/qdrant-1.19.0.tgz`; the first-party
application chart uses the verified checkout. Both render offline with
checksum-pinned Helm 4.3.0. The packaging-selected PR job in
[the workflow](../.github/workflows/e2e.yml) executes `artifacts:helm-lint` and
`artifacts:helm-render` with `charts/qdrant-openshift.values.yaml`, the same
production-values owner deploy uses. Generated Secret data stays in
runner-temporary render output, outside logs/artifacts. First-party render
checks remain separate; Qdrant dry-run only prints upgrade argv.
Deploy reads every deployed registry image before mutation, including
`--skip-load` and standalone paths (see [image identity](#image-identity-across-archive-and-registry-formats)).
Deploy and pack require exactly one `charts/qdrant-*.tgz`. For live deploy
with a reachable packed `MANIFEST.txt`, its archive sha256 must match
`chart_sha256`; mismatch/missing fields fail closed before release commands.
Dry-run or no-MANIFEST runs report "not release-verified".
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
  cluster user; deploy prints a notice; the opt-in SAR cohort check is deferred
  under #373) and, for the unauthenticated 8080 Service port, whether the
  agent ingress NetworkPolicy below is actually enforced and not widened by
  site policies.

<a id="agent-networkpolicy"></a>
- Agent ingress NetworkPolicy (issue #373, **default on**, a maintainer-approved
  default change). The chart renders `NetworkPolicy/rag-agent-ingress` unless
  `AGENT_NETWORK_POLICY=false` (strict boolean; `networkPolicy.enabled`). It
  selects only `app: rag-agent` pods and only `Ingress` (Qdrant, OpenObserve, the
  ingest Job and all egress are untouched; the OpenObserve egress-deny policy
  is a separate object that this toggle does not disable). Rules: pods of the release
  namespace reach 8080 (and 8443 when `AGENT_ROUTE=true`); the OpenShift
  router (`network.openshift.io/policy-group: ingress`) reaches 8443 only;
  with `METRICS_ENABLED=true` the monitoring namespaces
  (`policy-group: monitoring` or namespace
  `openshift-user-workload-monitoring`) reach 8080, whether or not the chart
  ServiceMonitor is deployed; each namespace in
  `AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES` (comma-separated DNS-1123 labels,
  no spaces or repeats, empty by default, matched by
  `kubernetes.io/metadata.name`) reaches 8080 only. Anything else, including
  another namespace calling `rag-agent:8080` such as an out-of-namespace MCP
  server, is denied until listed or the policy is disabled. A malformed
  selection, or a list with the policy disabled, fails before any mutation.
  Kubelet probes and the sidecar's loopback hop are expected to be unaffected
  (pod readiness after the policy is site acceptance). Missing platform labels
  show as the router's 503 on the Route or failed scrapes while pods stay Ready;
  `AGENT_NETWORK_POLICY=false` is the opt-out. The policy is
  a member of the fixed ownership inventory (`check_app_ownership.py`), so the
  next ordinary deploy adopts its own policy; disabling it makes Helm remove the
  owned object with the release upgrade (no extra cleanup entry). Not proven by
  this repository, and site-gated under #447: CNI enforcement, the platform
  labels on the router/monitoring namespaces, denial from another namespace
  (service and pod IP), and coexistence with site policies. NetworkPolicies
  are additive, so a site allow-all or broad allow policy that also selects the
  agent pods voids the restriction; a site default-deny is not satisfied by
  this policy for Qdrant or OpenObserve.

<a id="feedback-service"></a>
- Answer-feedback service (issue #804, ADR-0005, **default off**). With
  `FEEDBACK_ENABLED=true` the application release renders `StatefulSet/rag-feedback`
  (one replica, the agent image started as
  `python3 -m uvicorn --factory mainframe_rag.feedback.service:app_from_env`, one
  worker; no Qdrant, model, gateway or OTLP settings and no ServiceAccount token),
  the standalone claim `PersistentVolumeClaim/rag-feedback-data`
  (`FEEDBACK_STORAGE_SIZE`, default 10Gi, RWO on `STORAGE_CLASS`, never NFS,
  `helm.sh/resource-policy: keep`), a ClusterIP `Service/rag-feedback` (never a
  Route: the Route exposure preflight treats it as protected whether or not the
  feature is on) and `NetworkPolicy/rag-feedback-ingress`. The agent Deployment
  then gains `FEEDBACK_ENABLED=true`, `FEEDBACK_URL=http://rag-feedback:8080` and
  `FEEDBACK_API_KEY`. When off, nothing is rendered and the default render is
  unchanged byte for byte.
  - **Secret.** `FEEDBACK_SECRET` names an operator-created Secret with data key
    `service-token`, the bearer token the service reads as
    `FEEDBACK_SERVICE_TOKEN` and each agent pod as `FEEDBACK_API_KEY` (never a
    chart value; the chart renders no Secret). `validate.sh` and `deploy.sh` check
    that the key exists, without reading it, before any mutation. Rotation:
    replace the Secret value, then restart the StatefulSet and the agent
    Deployment; a mismatch shows as agent `feedback_record status=401` warnings
    and `rag.feedback.records{outcome="failed"}`, never as an answer failure.
  - **Network.** Policies are additive and a pod selected by none is open to the
    namespace network, so the content store has its own policy. Data peer: pods
    labelled `app: rag-agent` of this namespace on 8080 only (not the ingest Job,
    debug pods or `AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES`). With
    `METRICS_ENABLED=true` the monitoring namespaces (same two selectors as the
    agent policy) may also reach 8080; `/metrics` and every data route still
    require the bearer token, and the chart ServiceMonitor `rag-feedback`
    (same decision as the agent's) scrapes with that token. Because the bearer
    token would otherwise be the only control, `FEEDBACK_ENABLED=true` with
    `AGENT_NETWORK_POLICY=false` is refused before any mutation (and by the chart
    at render). Egress is untouched: a site that default-denies egress must allow
    agent to `rag-feedback:8080`.
  - **Purge.** The service purges its own retention window
    (`FEEDBACK_RETENTION_DAYS`, default 180, 1..3650): 30 s after start, then
    every 24 h, with `wal_truncated: false` and errors retried at 60/300/900 s
    then hourly ([agent.md](agent.md#answer-feedback)). Outcomes are on the
    bearer-protected `/metrics`; the suggested alert
    (`time() - rag_feedback_purge_last_success_timestamp_seconds > 2 * 86400`)
    is platform-owned and not shipped.
  - **Lifecycle.** The pod runs the same image as the agent, so every release
    restarts it; records sent meanwhile are dropped and counted and ratings
    return `feedback_unavailable` (ADR-0005). The deploy waits for
    `statefulset/rag-feedback` and, on the release path, binds that StatefulSet
    to the verified agent digest after rollout. All five objects
    (`StatefulSet`, `Service`, `PersistentVolumeClaim`, `NetworkPolicy`,
    `ServiceMonitor`) are in `check_app_ownership.ALLOWED`, so the next ordinary
    deploy adopts its own objects. **Disabling** removes the workload, Service,
    policy and monitor with the release upgrade (no cleanup entry) but keeps the
    claim: **nothing purges while the service is off, so question and answer
    text stays on the volume past the retention window until the operator
    deletes the claim** (`oc delete pvc rag-feedback-data`, an explicit action;
    no deploy path ever deletes it, and retiring the feature for good means
    deleting it). **Re-enabling** adopts the retained claim and the first
    scheduled purge, 30 s after start, removes whatever aged out meanwhile.
  - **Backups.** A CSI snapshot of the claim, or SQLite's online backup API run
    inside the pod and copied out, contains question and answer text: store it
    with the claim's protection and expire it no later than the retention
    window, otherwise purged text survives in the backup. A restore can bring
    back records and ratings made after the backup; the startup purge re-expires
    anything past the window. Portable export/import is #805.
  - Sizing is unmeasured: 10Gi, 50m/64Mi requests and 250m/256Mi limits are the
    maintainer's accepted starting defaults (D8, #804); measure real bytes per
    record before relying on them. Not proven by this repository, and site-gated
    under #447: CNI enforcement of the feedback policy and denial from another
    pod, RWO claim binding and restricted-v2 group write on the site storage
    class, expansion and reclaim policy, snapshots, the bearer-authenticated
    scrape and monitoring-namespace labels, and the site egress policy.
  - Rater attribution is deferred (D5): ratings are stored anonymously.

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
  the checked legacy Jaeger leftovers (retired in #956), console Route/ServiceAccount
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
  the Helm release. The OpenObserve PVC has `helm.sh/resource-policy: keep`: disabling
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
- Rollout waits remain Qdrant 600s, agent 300s and, with the bundled backend, OpenObserve and otelcol 120s each, with bounded
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

The connected host packs one tarball: git bundle, Qdrant + OpenObserve
+ otelcol-contrib + ingest + agent image archives (plus the oauth-proxy sidecar image once its
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
  against the manifest, then all six base image digests (plus the
  oauth-proxy digest when the bundle carries it) — and pushes under fixed
  names: `qdrant/qdrant:v1.19.0-unprivileged`,
  `openobserve/openobserve:v1.1.2`,
  `otel/opentelemetry-collector-contrib:0.160.0`,
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
ingest, OpenObserve, otelcol). Where this guide says prod has ≥10× local, that means
CPU/RAM/disk for those workloads, not model GPU/VRAM.

Prod values assume a real OpenShift cluster; the single-node Kind path
shrinks them via overrides that must never reach prod (a 3×16Gi Qdrant
cannot schedule on one node — proven).

- Prod Qdrant: 3 replicas, 500Gi data + 500Gi snapshots on RWO block,
  4 CPU/16Gi requests, 8 CPU/32Gi limits, project-assigned UID/GID and
  volume group from `restricted-v2`. Project-range
  admission and volume writes must pass site OpenShift acceptance; fix demonstrated
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
- First-party pods (agent, the oauth-proxy sidecar, the ingest Job, OpenObserve, otelcol)
  declare their own hardening so it does not depend on `restricted-v2`
  alone (issue #585): pod `runAsNonRoot: true` and
  `seccompProfile: RuntimeDefault`; every container
  `allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]`. The chart
  never sets `runAsUser`, `runAsGroup` or `fsGroup`; OpenShift assigns them.
  The agent and ingest images declare numeric `USER 1000`.
  Agent and ingest containers additionally set `readOnlyRootFilesystem: true`.
  Each mounts a disk-backed `emptyDir` at `/tmp`; ingest reuses its existing
  `/work` PVC for inventory, writer locks, publication state and the default
  contextual cache. Image `PYTHONDONTWRITEBYTECODE=1` prevents import cache
  writes. Baked `/opt/bm25` and `HF_HOME=/opt/bm25/hf` remain read-only: the
  pinned `Qdrant/bm25` implementation reads its existing snapshot and uses
  text stopwords/stemming, without an ONNX model/session. No writable source,
  weight or home overlay is provided. The snapshot-maintenance Job inherits
  ingest's security settings and has its own `/tmp` emptyDir, without corpus
  or work PVC attachments. Render tests check these declarations;
  current-candidate image/pipeline/lifecycle execution must establish write-path
  coverage. The pinned OAuth sidecar retains the four original controls;
  its read-only-root/write-path qualification and actual `restricted-v2`
  admission remain separate acceptance under #585/#373/#447.
- OpenObserve is the bundled trace backend (see [Trace and metrics backend](#trace-backend)):
  1 replica, project-assigned UID and volume group from `restricted-v2`, 10Gi RWO volume, OTLP/HTTP
  on 5080 only, UI on port-forward only. Tracing off disables it.
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
- Smoke needs only a namespace: it execs into the agent pod (no Route or
  port-forward required), fails closed on degraded `/healthz`, treats empty
  search results as SKIP (infrastructure ready, corpus not ingested) rather
  than failure, and never touches `/v1/answer` (needs a reasoning model).
  With tracing on (the default) it also fails closed unless a `v1.search`
  span lands in OpenObserve (`TRACE_QUERY_URL`, default the in-cluster service; credentials from the
  `openobserve-credentials` Secret or `TRACE_QUERY_USER`/`TRACE_QUERY_PASSWORD`) within `TRACE_TIMEOUT` (default 60s); the `off`
  sentinel skips that assertion. With an external `OTEL_EXPORTER_OTLP_ENDPOINT` there is no bundled
  OpenObserve: `TRACE_QUERY_URL` (and user/password) must be given explicitly, otherwise smoke
  refuses with a fixed message.

<a id="collection-policy"></a>
### Capacity arithmetic (issue #272)

Provisioned claims by default (`charts/qdrant-openshift.values.yaml`, chart
values; the three Qdrant peers each get their own claims):

| Claim | Per unit | Units | Total |
|---|---|---|---|
| Qdrant data (RWO block) | 500Gi | 3 peers | 1500Gi |
| Qdrant snapshots (RWO block, node-local) | 500Gi | 3 peers | 1500Gi |
| Ingest scratch `ingest-work` (`INGEST_WORK_SIZE`) | 100Gi | 1 | 100Gi |
| OpenObserve data (when tracing is on) | 10Gi | 1 | 10Gi |
| **Provisioned PVC capacity** | | | **3110Gi = 3.04TiB** |

The optional feedback claim (`FEEDBACK_STORAGE_SIZE`, default 10Gi, unmeasured,
only with `FEEDBACK_ENABLED`) is not in this total.

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
independent-worker or site tolerance. Existing one-node Kind lanes and
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
staging pair's distribution and placement. A physical collection squatting
on the alias name (the pre-publication layout) is unsupported and refused,
never migrated. Supported recovery from lost distributed storage is
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
  or login fails. Personal pull secrets stay local. The OAuth copy uses
  `--remove-signatures` because Docker archives cannot store Red Hat's upstream
  signature attachments. Source digest verification, archive digests, and the
  offline bundle signature still apply; upstream signature attachments are not
  part of the handoff. The explicit PENDING state still skips the member and
  makes `AGENT_ROUTE=true` fail closed. A tag bump for `ose-oauth-proxy`
  must change all three sites: `images.txt`, `deploy.sh`, `load.sh`.
- OpenObserve OSS v1.0.4 (`docker.io/openobserve/openobserve`,
  `sha256:d4a878fa...54ba`, amd64 manifest list) and otelcol-contrib 0.160.0
  (`docker.io/otel/opentelemetry-collector-contrib`, `sha256:799dc6cf...2ad6`)
  are pinned and bundled by [ADR-0006](adr/0006-openobserve-traces-metrics-backend.md)
  (#956); the [qualification record](records/2026-10-23-openobserve-oss-qualification.md)
  names both digests. Neither pin changes what deploys until the chart backend
  selection (#957) lands. OpenObserve is AGPL-3.0, otelcol-contrib Apache-2.0
  ([licensing](licensing.md)). A bump of either changes `images.txt`, the
  `licenses/inventory.json` bind, `load.sh` and `image_identity.sh` together.
- `METRICS_ENABLED=true` additionally renders/applies
  the first-party chart ServiceMonitor so the OpenShift UWM stack scrapes
  `/metrics` (prerequisite and sizing in `docs/install_and_ops.md`).
- Application transfer uses the signed bundle and verified `skopeo` loading
  path above; this repository ships no alternate mirroring configuration.

<a id="quay-image-roles"></a>
### Quay image roles and platform compatibility (issue #808)

Application images are built once in the connected factory and promoted as the
tested bytes through the signed bundle. The air-gapped GitLab never builds,
rebuilds or tags an application image; it only runs the gates. Every path below
is relative to an operator registry path and contains no hostname; operators set
the real values in `airgap.env` (`INTERNAL_REGISTRY`, written by `load.sh`) and in
protected GitLab CI/CD variables (`CI_APPROVED_REGISTRY`, read only by
`.gitlab-ci.yml`). The two may name different Quay organizations: runtime images
and CI tooling have different owners and pull credentials.

| Role | Path under the registry | Identity | Written by | Read by |
|---|---|---|---|---|
| Agent | `qdrant-pdf-rag-agent:<IMAGE_SHA>` | packed config digest, then deployed by registry manifest digest | `load.sh` | cluster pods |
| Ingest | `qdrant-pdf-rag-ingest:<IMAGE_SHA>` | as the agent | `load.sh` | ingest Job |
| Qdrant | `qdrant/qdrant:v1.19.0-unprivileged` | tag (the vendored chart cannot render a digest); running `imageID` compared | `load.sh` | cluster pods |
| OpenObserve OSS (ADR-0006) | `openobserve/openobserve:v1.1.2` | as the agent; the chart references it by digest only (`images.openobserve.digest`) | `load.sh` | cluster pods when tracing is on with an empty `tracing.endpoint` |
| otelcol-contrib (ADR-0006) | `otel/opentelemetry-collector-contrib:0.160.0` | as the agent | `load.sh` | cluster pod (`Deployment/otelcol`, container `otelcol`) beside OpenObserve |
| OAuth sidecar (optional) | `openshift4/ose-oauth-proxy:v4.14` | as the agent; absent while `images.txt` says `sha256:PENDING` | `load.sh` | cluster pods |
| CI job image | `ci/python:3.14`, or a complete `CI_PYTHON_IMAGE` | mirrored UBI Python 3.14 (GIL) with git, pip, tar, sha256sum | platform or factory mirror, not this repository | GitLab runner |
| CI Qdrant service | `qdrant/qdrant@sha256:<images.txt digest>` | the `images.txt` manifest digest, so a tag is never enough | registry-to-registry copy that preserves the digest | GitLab runner (`sim`, `gate-l1`) |

The CI Qdrant service is a different role from the runtime Qdrant image. `load.sh`
pushes from a Docker archive, whose registry manifest digest can differ from the
upstream pin (see above), so a digest reference only resolves for a copy made
registry to registry. Prove it with `skopeo inspect` against the `images.txt`
digest before relying on it; a repository shared by both roles must satisfy both
forms. Registry-side tag immutability, retention and scanning remain Quay policy,
not repository behavior.

Release pipelines run from a protected tag: the tag pipeline runs the same jobs as
a merge-request pipeline, and `hygiene` fails a tag pipeline whose ref is not
protected. Protect the release tag pattern and limit creation to release owners in
GitLab; the repository cannot enforce that setting. `CI_APPROVED_REGISTRY` has no
default, so with it unset the job images do not resolve and every job fails to
start. The tag name pattern is an operator choice.

Platform compatibility profile. These are requirements to verify, not recorded
site facts. The site records the installed value, the person who supplied it and
the date in its protected release record; unknown rows block only their dependent
lane, and an unknown executor is never assumed to provide Docker, privileged
containers, a writable host or cluster access.

| Area | Requirement | Used by |
|---|---|---|
| GitLab | Release version recorded; protected tags; `CI_COMMIT_TAG` and `CI_COMMIT_REF_PROTECTED`; variables expanded in `image`, `services` and `tags` | tag pipelines |
| Runner | Tag `CI_RUNNER_TAG` (default `airgap`); linux-amd64; an executor that starts job and service images from the approved registry with a service alias; reads `CI_TASK_ARCHIVE`, `CI_HELM_ARCHIVE` and `CI_BM25_CACHE_DIR` from the runner; no route to PyPI, Docker Hub or GitHub | all GitLab jobs |
| Python packages | `PIP_FIND_LINKS` wheelhouse or `PIP_INDEX_URL` internal index, never a public index | `.py-base` |
| Quay | Release version recorded; Docker v2 API reachable by `skopeo`; a push identity for the five runtime paths; pull access for cluster and runner; organization layout may differ from upstream paths | `load.sh`, runner |
| OpenShift | Namespace-scoped deployer; `restricted-v2` SCC with no `anyuid`; RWO block storage for Qdrant (NFS only for the read-only corpus); ClusterIP-only Qdrant; Route only with OAuth | deploy, ingest |
| Models | Platform-owned HTTP gateway and per-leg Secret references; no models hosted here | agent, ingest |

Site qualification of the actual versions and permissions is #446 packets C/D and
is not claimed by this table.

### Shared LiteLLM configuration

`GATEWAY_BASE_URL=https://sample-api/v1` illustrates the shared API-base
convention; replace the example URL with the platform endpoint. The operator
loader resolves unset `EMBED_BASE_URL`, `LLM_BASE_URL`, `RERANK_BASE_URL` and
`CONTEXT_LLM_BASE_URL` to it before rendering. Explicit operation-specific
URLs take precedence. A caller value beats the operator file for the same
setting. With no shared setting, the explicit per-operation URLs apply
(`EMBED_BASE_URL` is required). Clients append operation
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
Direct Helm users set `gateway.apiKeySecretKey`; an empty value keeps the
default per-leg data keys. The API key value never enters Helm values or `airgap.env`.

## 7. CI inventory

GitHub runs unit, sim, gates, bench, load, the path-filtered multi-peer HA
lane (issue #360: three-peer placement/peer-loss fixture), connected E2E,
and the dry-run gate; air-gap GitLab runs hygiene, lint/types, the full
pytest suite, hazards, the sim tier and gate-l1 only (no e2e, load, deploy,
multi-peer docker, GHCR, or PDFs/tokens/hostnames in file). GitLab unit/sim
reports reject any skipped or xfailed test (`scripts/check_junit_clean.py`);
`tests/test_ci_gitlab_parity.py` pins the correspondence and failure propagation. Job meaning stays aligned across the two files; only e2e-scale jobs
live in `.github/workflows/e2e.yml`.

- GitHub product `ci.yml` runs on every PR and nightly on `main` (`schedule`,
  plus `workflow_dispatch`), not on push to `main`; `e2e.yml` runs its light jobs
  on every PR/push, while the heavy jobs (`airgap-package`, `airgap-acceptance`,
  the `kind-live-rehearsal` matrix, `airgap-rehearsal`) run only nightly on `main`
  HEAD (`schedule`) or by `workflow_dispatch` (use it on a branch for Kind-lane
  PRs). Neither is gated by a markdown-only
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
  a top-level concurrency group (cancel-in-progress on PRs; main schedule/dispatch
  runs share `ci-main` and never cancel); third-party actions SHA-pinned.
  Hazard pairs run in four isolated processes; baseline, mutation and cleanup
  remain ordered within each pair. Connected Python acquisition explicitly
  uses eight bounded download workers across GitHub workflows.
- Post-merge cadence (maintainer decision 2026-10-23): `ci.yml`, `load.yml`, `ha.yml`,
  `agent-probes.yml` and `browser.yml` run on `pull_request` (the merge gate,
  unchanged) and nightly on `main` by `schedule` (staggered UTC, off the hour)
  plus `workflow_dispatch`; they no longer run on every push to `main`, so an
  individual main SHA is covered by its PR run and the next nightly, not by its
  own post-merge run. A red nightly is visible as a failed scheduled run in the
  Actions list (GitHub notifies the account that last edited the cron); it is
  not a PR check. Candidate acceptance reads only `pull_request`-event runs, so
  it is unaffected. Pull-request runs of `ha`/`load` cancel superseded runs of
  the same PR. Their `select` job runs only on pull requests; schedule and
  dispatch run the lane unconditionally. `ci.yml` keeps its `select` job on every
  event; with no base SHA on schedule/dispatch the profile is `full`, so every lane runs.
- `load.yml`: path-allowlisted to agent/retrieve/ingest/mock/sim/loadtest
  surface and shared dependency/Task inputs — other paths run nothing. `ha.yml` is path-allowlisted to the
  collection-policy/placement surface and runs the three-peer fixture
  (`qa:ha`) fail-closed (no skips, at least one pass); unrelated changes
  do not pay for three containers.
- `browser.yml`: path-allowlisted to the console and dependency locks; runs
  the browser contracts against a digest-pinned Chrome for Testing, fail-closed on skips.
- `coverage.yml`: nightly + dispatch + PRs touching only its own file; runs
  `qa:coverage` over the full unit selection and uploads the reports. Advisory:
  never a required check or an acceptance producer.
- `bench.yml`: nightly + dispatch (no longer on push to main; baseline update with
  repeats); never a PR gate.
  Measurement passes and search/answer load phases stay sequential on their
  dedicated runner: overlapping them would change contention and invalidate
  the approved performance baseline. Signing/packaging likewise waits for
  both complete images; fault and lifecycle transitions remain ordered inside
  each isolated rehearsal case. No GPU work is added.
- `e2e.yml build`: local images retagged to full-SHA GHCR refs; push only
  on `main` push/schedule/dispatch (PRs build, never push — fork-safe).
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
  pipeline with explicit shared-gateway reasoning and embedding-only
  (reasoning explicitly disabled) configurations, both pull-secret branches.
  `kind-live-rehearsal` (main/dispatch) is a twelve-lane matrix described below;
  its operator flow runs behind an [egress boundary](#kind-egress-boundary).
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
| `airgap-acceptance` | Static dry-run (no cluster, no egress block): fresh bundle integrity/bootstrap, manifest SHA, explicit shared-reasoning and embedding-only configurations, and both pull-secret render branches |
| `kind-pipeline` | Authenticated registry, real product containers, real LiteLLM/PostgreSQL, synthetic ingest, search and application streams |
| `kind-gateway-chat-{upstream,malformed,truncated}` | Three isolated clusters, one reasoning fault each through real LiteLLM, application failure contracts and healthy recovery |
| `kind-gateway-embed-{upstream,malformed,dimension}` | Three isolated clusters, one embedding fault each through real LiteLLM and healthy recovery |
| `kind-gateway-auth` | Wrong/missing credentials, untrusted CA, hostname mismatch and reasoning deadline, followed by healthy gateway/application recovery |
| `kind-lifecycle-chart` | Three-worker Kind; first-party chart fresh/repeat/update/failure/rollback/redeploy sequence with PVC guards, then the next ordinary pipeline, all behind the egress boundary |
| `kind-lifecycle` | Independent three-worker Kind; synthetic snapshot recovery, PVC identity, Qdrant/agent/OpenObserve replacement, old trace persistence and repeat pipeline |
| `kind-shared-gateway` | Same live pipeline/probe/contracts/smoke but through the shared `GATEWAY_BASE_URL` fallback and the single `api-key` Secret data-key (issue #551); explicit per-operation URLs stay unset so fallback is exercised |
| `kind-rerank` | Same live pipeline behind the egress boundary with `RERANK_ENABLED=true` through the test gateway: the minted per-leg `rerank-api-key` reaches the pod through its Secret reference, the gateway refuses a wrong, absent or other-leg key on both rerank routes, one scoped search is one stand-in call, the score route being unavailable is served by the rerank route, a scoped search with rerank on differs from rerank off only by the stand-in's scores (gateway call count the causal check), and with every rerank route down non-identifier search fails closed with the fixed `retrieval failed` message while `/healthz` is 503 with the rerank leg unready, then recovers (issue #815) |
| Manual release Kind lane | Both real models through the authenticated TLS gateway on the same signed bundle ([release gate](release-verification.md)); SCC, Service CA, OAuth, Routes, node pulls and egress are verified on the site |

<a id="kind-rerank-lane"></a>
#### Rerank rehearsal (`kind-rerank`, issue #815)

Owners: `scripts/ci/test-gateway.yaml` (the `mock-rerank` model and the
`/v1/score` pass-through, shaped like `scripts/run_local_gateway.sh`),
`scripts/ci/mint_gateway_keys.py` (a real key scoped to `mock-rerank`),
`scripts/mock_vllm.py` (the stand-in and its request counters) and
`scripts/ci/rerank_rehearsal.sh`. Only this lane sets `RERANK_ENABLED=true`
(per-leg keys, `RERANK_BASE_URL` the test gateway, `RERANK_MODEL=mock-rerank`);
every other lane keeps rerank off and its expected results.

The stand-in scores candidate *i* of *n* as *(i+1)/n*, so reranking exactly
reverses the order the agent hands over.

What the lane proves: the Deployment carries a distinct per-leg rerank key from
its Secret reference; the real gateway accepts it on `/v1/score` and
`/v1/rerank` and refuses a wrong, absent or other-leg key before the stand-in
sees a request; with `score_first` one scoped search is one `/v1/score` call and
a score route that is unavailable is served by `/v1/rerank` with the same
scores; a scoped `/v1/search` with rerank on is the exact reversal of the same
search with rerank off (same candidates, fusion scores unchanged, stand-in call
count 1 versus 0); with every rerank route unavailable, non-identifier search
returns the fixed `502 upstream_error "retrieval failed"`, identifier lookups
still serve (they bypass rerank), and `/healthz` is `503` with `rerank: false`;
the leg and the next search recover when the routes return. Both endpoint
orders and their fallbacks are pinned in `tests/test_rerank.py`.

What stays site-only: the real rerank endpoint, its key and TLS, its model and
latency, and ranking quality (golden-set evaluation owns that; the stand-in
carries no relevance signal). Fail-closed on exhaustion is the current contract
([serving contract](agent.md#serving-contract)); the lane asserts it and does not
change it. The lane's assertions run only in a `workflow_dispatch` or `main`
Kind run; the same scripts' logic is covered hermetically by
`tests/test_ci_gateway.py`.

<a id="kind-egress-boundary"></a>
#### Egress boundary in the Kind lanes (issue #809)

Every Kind lane bootstraps the signed bundle and runs the operator flow (image
import, validate, deploy, ingest, smoke, application contracts, fault and
lifecycle sequences) with public egress blocked by the kernel packet filter,
not by mocking. `scripts/ci/egress_gate.sh` owns the rules and
`scripts/ci/egress_probe.py` owns the probe.

- **Before the boundary** (public network allowed): tool installs, the Kind
  node image, the registry image, the provisioner and the platform stand-ins'
  third-party images (real LiteLLM and PostgreSQL; the provisioner's helper
  image is pre-pulled on every node). These stand in for what the platform
  already provides in the gap. The helper scripts come from a separate
  checkout, never from the operator workspace.
- **Behind the boundary**: bundle verification, extraction and `bootstrap.sh`
  (a fresh workspace from the bundle only), every later operator step, and all
  pods. Host processes run with a dedicated primary group whose `iptables`
  OUTPUT owner-match accepts only loopback and the Kind network and REJECTs
  everything else, DNS included. A `DOCKER-USER` rule REJECTs traffic leaving
  the Kind network, which covers node image pulls and pod egress. Only bundle
  images, loaded into the lane's authenticated TLS registry, can be pulled.
- **Not blocked**: the GitHub runner agent, the Docker daemon and its embedded
  resolver (a pod can resolve a public name but cannot connect), the setup
  steps, and the post-mortem and teardown steps. IPv6 is gated only when
  `ip6tables` exists (a warning otherwise). This is a rehearsal boundary, not a
  security control; gated commands run with `no_new_privs` so they cannot lift it.
- **Proof in each run**: a public-reachability control before the gate; a probe
  that must fail for public addresses and names, and a failed public download,
  behind it; the registry and API controls that must still succeed; the same
  blocked probe from the mock-vLLM pod and the application pod, with an
  in-cluster control; and non-zero REJECT packet counters for both rule
  classes. `egress_gate.sh run` refuses to start when the rules are absent.
  Pod egress is also denied in the cluster: the boundary step applies a default-deny
  egress NetworkPolicy (kindnet, the Kind CNI, enforces egress) to the lane namespace
  allowing only cluster DNS, pods of that namespace and the API server (a
  CI-only manifest; the product chart has no egress option). The host rules alone
  were seen to miss a pod connect to a public address.
  Because that policy stops pod connects before the host, a probe from each Kind
  node's own network namespace (outside NetworkPolicy) exercises the host rule, so
  `require-hits nodes` proves the host and node-forward rules; the pod-level
  NetworkPolicy is proven by the pod probes only (no counter). The
  `kind-lifecycle-chart` lane's `$NS-chart` namespace has no egress policy and
  relies on the host rules alone.
  The counters and rules are retained as `diagnostics/egress-gate-*.txt`.
- **Maintenance**: a step added after the boundary must use the gated `shell:`
  or be listed as post-mortem in `tests/test_egress_gate.py`. A new public
  dependency of the operator flow fails the lane by design; supply it from the
  bundle or the platform stand-ins, never by loosening the rules.

The `kind-lifecycle-chart` lane behind the boundary is the single A to repeat A
to B to failed B to rollback sequence: the injected failed B fails at the image
pull against the internal registry, rollback restores A, B is redeployed, A is
redeployed explicitly, and the next ordinary `pipeline.sh` deploy then succeeds.
A and B differ by one rollout mark on the same signed images and chart;
two separately signed releases and the hotfix import loop remain #446 D work.

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

The production SCC fixes remove fixed Qdrant/OpenObserve identities and make the
application's required work paths group-0 writable. OpenObserve uses `Recreate`
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
3. The first-party images (agent, ingest, OpenObserve, otelcol, oauth-proxy) are then rendered
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
null; OpenObserve uses project-assigned IDs and `Recreate` for its single local-mode writer.
Agent and ingest image roots (source and baked BM25 snapshot included) are
read-only; only `/tmp` and ingest's existing `/work` mount accept writes.
Never repair admission with `anyuid`. Verify an old trace survives replacement.

The agent's unauthenticated HTTP port is namespace-scoped by default through
the chart's ingress NetworkPolicy (rules, escape hatches and site-gated proof:
[compatibility decisions](#agent-networkpolicy)).
Never replace it with a Qdrant or agent Route, `anyuid` or a cluster-scoped
grant; a site that cannot use it sets `AGENT_NETWORK_POLICY=false` and records
its own restriction.

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
[local recovery](local-real-corpus.md), [release gate](release-verification.md).
Qdrant 1.19 snapshot file URIs must be under the configured snapshot directory;
require completed recovery, provenance/count/dimension and vector compatibility
before an alias. Never mix synthetic cleanup with preserved real-corpus resources.
Transfer the identical tested bundle; missing/failed release-gate checks block
promotion. Sizing-only overrides do not establish SCC, residency or distributed HA.

<a id="trace-backend"></a>
## Trace and metrics backend: OpenObserve (ADR-0006, #956)

OpenObserve OSS is the only bundled backend; Jaeger is gone from the chart (an approved
default change, #956). With `tracing.enabled=true` and an empty `tracing.endpoint` the chart
renders a single-node Deployment (local mode, replicas 1, `Recreate`, RWO PVC
`openobserve-data`, `openobserve.storage.size` default 10Gi, kept on uninstall), a ClusterIP
Service on 5080 (no Route or Ingress) and an egress-deny NetworkPolicy. The pod is restricted-v2
(non-root, no fixed IDs, drop ALL, seccomp RuntimeDefault, read-only root) and sets
`ZO_TELEMETRY=false`, `ZO_MODEL_PRICING_ENABLED=false`, `ZO_MMDB_DISABLE_DOWNLOAD=true`.
Egress is denied (DNS included) by the policy as well as by those settings; the policy needs an
enforcing CNI. The console is reached by port-forward only.

Settings (tracing off needs none of them):

- `images.openobserve` (`v1.1.2`) and `images.otelcol` (`0.160.0`): repository plus tag, like the
  other images; the digest pin lives in `images.txt` and is enforced at pack/load (#960).
- `openobserve.runAsId` (operator key `OPENOBSERVE_RUN_AS_ID`, default unset): on plain Kubernetes such as the Kind lanes the image runs as root, so set one non-root id used as runAsUser/runAsGroup/fsGroup; leave unset on OpenShift, where restricted-v2 assigns the identity.
- `openobserve.credentialsSecretName` (default `openobserve-credentials`; an emptied name fails the
  render): the Secret must exist at deploy time (#960/`deploy.sh` checks it) with keys
  `root-user-email`, `root-user-password`, `otlp-headers` (`Authorization=Basic <b64>`, read by the
  agent and ingest Job as `OTEL_EXPORTER_OTLP_HEADERS`) and `metrics-auth` (`Basic <b64>`, read by
  the collector). No credential value is in values or the ConfigMap. `deploy.sh` refuses a weak password before any mutation (`E_OPENOBSERVE_WEAK_PASSWORD`, value never printed). OpenObserve v1.1.2 refuses to start with a weak root password: `root-user-password` must be 8-128 characters with a lowercase letter, an uppercase letter, a digit and a special character.

`tracing.endpoint` set is the generic OTLP/HTTP escape hatch: the agent exports there, and no
OpenObserve or collector renders. Export stays fail-open.

**Metrics collector.** With `metrics.enabled=true` (and the bundled OpenObserve) the chart adds a
stateless `otelcol-contrib` Deployment (no PVC, no Service, same hardening). It scrapes
`rag-agent:8080/metrics` every 15s and forwards over OTLP/HTTP to
`http://openobserve:5080/api/default` with `Authorization` from `metrics-auth`.
`NetworkPolicy/otelcol-egress` allows only OpenObserve 5080, the agent 8080 and cluster DNS
(`openshift-dns`, `kube-system`), never the internet. `metrics.collector.enabled=false` skips it
(platform forwarding); `true` without metrics and the bundled backend fails the render.
The disk-full alert and the smoke trace check are later steps of #956.

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
`INGEST_REINGEST` and `INGEST_RETIRE_DOCS` are `OPERATOR_ENV_KEYS` validated by
`scripts/airgap/ingest.sh` and rendered into the ingest Job args (`INGEST_RETIRE_DOCS` accepts comma- or newline-separated
`DOCID[@SOURCEREV]` entries and preserves interior whitespace in product/version labels);
alias publication is the only ingest mode (the Job carries no mode switch, the
chart schema refuses `ingest.aliasPublish`, and the values mapper refuses
`INGEST_ALIAS_PUBLISH=false`), and the shared ingest-work progress path is fixed so one authorized
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

**Agent ingress policy (issue #373):** `AGENT_NETWORK_POLICY` (default true) and
`AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES` (default empty) follow the same path:
`airgap.env.example`, `OPERATOR_ENV_KEYS` snapshot/restore and the Task
`TASK_*` bridge, validation by the one owner (`model_config.network_policy`,
used by `validate.sh` and `map_values.py`), generated `networkPolicy.enabled` /
`networkPolicy.allowedNamespaces`, schema validation, and the chart template.
It renders only into the application release (the ingest Job render does not
depend on it). The default-on change is documented under the console Route
bullet in the compatibility decisions above, which owns the rules.

**Feedback service (issue #804):** `FEEDBACK_ENABLED` (default false),
`FEEDBACK_SECRET`, `FEEDBACK_STORAGE_SIZE` (default 10Gi) and
`FEEDBACK_RETENTION_DAYS` (default 180) follow the same path:
`airgap.env.example`, `OPERATOR_ENV_KEYS` and the Task `TASK_*` bridge (pinned
equal by `test_bridge_set_matches_operator_keys`), the one validation owner
(`model_config.feedback_config`, used by `validate.sh` and `map_values.py`),
generated `feedback.*` values (emitted only when enabled, with the protected
`resources.feedback`), schema validation and the chart templates. Storage class
reuses `STORAGE_CLASS`; the purge cadence and retry ladder are protected defaults
in the service, not keys. Rules and lifecycle:
[feedback service](#feedback-service).

**Absent/blank semantics:** `common.sh` currently gives non-empty explicit env
precedence and otherwise permits file/default resolution; required attestation
rejects whitespace. Runtime bearer auth intentionally omits a header for an
absent/empty/whitespace key. These are different owners/meanings: do not globally
normalize blanks without tracing both consumers. `map_values.py observability()`
is the one owner of the observability decisions across stages: the
unset=bundled-OpenObserve, http(s)=external collector and off/none/false/0=disabled tracing policy,
whether the bundled OpenObserve and otelcol deploy (`OTEL_BUNDLED`: tracing on and an empty endpoint)
and whether the ServiceMonitor deploys (explicit flag, else the parent leg;
a backend for a disabled leg fails closed). The shell stages `eval` its
`--bundle-decisions` (`common.sh resolve_observability`) and the release renders
the same booleans, so deploy/smoke/validate gates and the chart cannot disagree.
Any explicit `http(s)` URL is an intentional platform-owned collector: the chart renders
neither OpenObserve nor otelcol then, and deploy skips their Secret, identity, render and
rollout steps.

**Preconditions/failures/lifetime:** platform-supplied model/dimension/revision and
per-leg credentials must match the deployment. Gateway aliases are mutable and
cannot attest weights. Secrets reach pods via Secret refs; rotations require the
runbook's restart/verification. Local `GATEWAY_ENV_FILE` is launcher-owned and
provides gateway URLs/model IDs/keys plus `EMBED_MODEL_REVISION`. The local gateway
accepts `GATEWAY_EMBED_MODEL_REVISION` as an opaque, caller-attested immutable
weight identity and shell-quotes it in that private handoff. The local stack
sources the exported value unchanged for both ingest and serving. Unset or empty
input retains the existing `local:<model-id>` simulation label; that label does
not attest immutable weights. Set the matching `DENSE_DIM` explicitly for another
embedding model; the gateway does not infer dimensions or alter collections.
Explicit CLI/Task/env model or endpoint overrides must apply or fail; ambiguous
model discovery fails closed. No new production value is invented here.

**Evidence:** `tests/test_airgap_validate_sh.py`, `test_airgap_deploy_sh.py`,
`test_airgap_ingest_sh.py`, `test_airgap_pipeline_sh.py`, `test_config.py` and
`test_representation_gate.py`. `test_local_gateway_sh.py` also exercises the real
gateway handoff through local-stack ingest and serving process launches, including
exact opaque revision round-trips across shell-significant text and whitespace.
Render tests prove wiring under their fixtures;
real trust/identity/model readiness still needs the application-pod gateway probe
and release acceptance. `/healthz` plus search does not prove reasoning readiness.
Known metadata/publication gaps remain #391; broader CI enforcement remains #370.

<a id="ci-policy"></a>
## CI and reviewer maintenance policy

Keep root `.gitlab-ci.yml` and GitHub `ci.yml`; shared product hygiene/test meaning
changes together. GitLab runs merge-request, branch and protected-tag pipelines with
the same jobs, uses images from the operator-set CI_APPROVED_REGISTRY (never the
GitLab registry; CI_PYTHON_IMAGE overrides the job image), CI_RUNNER_TAG and internal
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

Rehearsal lanes bootstrap the same downloaded bundle in fresh directories behind the
[egress boundary](#kind-egress-boundary); never
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
