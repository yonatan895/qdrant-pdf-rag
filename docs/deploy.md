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
  `AIRGAP_DRYRUN=1` (every script prints instead of executing).
  `common.sh` normalizes the flag once: `1`/`true`/`yes` (any case) preview,
  `0`/`false`/`no`/unset run live, and any other value (`on`, ` 1`, `1<newline>`)
  is refused before any stage (#1015);
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
`/livez` is the process-only liveness probe. Kubelet probes reach the pod port
directly, not the OAuth sidecar; the sidecar's only unauthenticated path is
`--skip-auth-regex=^/livez$` (no upstream work), so an anonymous Route caller
cannot trigger the `/healthz` Qdrant, embedding or serving-gate evaluation
(#1018). No agent, ingest, OpenObserve or otelcol pod automounts a
ServiceAccount token (`automountServiceAccountToken: false`); with the Route
on, a projected token is mounted only into the `oauth-proxy` container (#1029).
It does not prove that reasoning
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
  base including `/v1`. The removed `REGISTRY_INTERNAL`, `OPENSHIFT_NAMESPACE`,
  `VLLM_BASE_URL`, `JAEGER_ENABLED` and `JAEGER_QUERY_URL` are refused when
  non-empty (from the file, environment or Task CLI), before any cluster change, with a fixed migration message
  (`model_config.RETIRED_KEYS`; values are never printed): set
  `INTERNAL_REGISTRY`, `NAMESPACE` and `EMBED_BASE_URL` (or `GATEWAY_BASE_URL`),
  and remove the Jaeger keys (`OTEL_EXPORTER_OTLP_ENDPOINT`/`TRACE_QUERY_URL`
  replace them).
- `require_env` collects **all** missing keys before failing, so one run
  tells the operator everything to fill in. `EMBED_MODEL_REVISION` is a
  required key on every air-gap launch path (vllm mode refuses a blank
  attestation); `validate.sh` additionally rejects whitespace-only values.
- Product rules: `EMBED_MODE=hash` dies (case-sensitive match on that exact
  string); storage classes containing `nfs` (any case) die — both
  `STORAGE_CLASS` and `SNAPSHOT_STORAGE_CLASS` are checked (the latter
  defaults to the former); the corpus PVC is not.
- Operator booleans `UI_ENABLED`, `CONTEXTUAL_EMBED_ENABLED`,
  `INGEST_ALIAS_PUBLISH`, `INGEST_REINGEST`, `INGEST_AUTHORIZE_FROM_CATALOG`,
  `CHAT_CONDENSE_ENABLED`, `LLM_STREAM`
  `FEEDBACK_ENABLED`, `OPENWEBUI_ENABLED` and `AGENT_NETWORK_POLICY` (default **true**, unlike the others)
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
  override: the deadline stays 270 and admission ships ON at 4/4/30 (#843);
  `0` is an explicit selection (queue wait must stay above 0 while concurrency
  is above 0; the chart schema refuses it). Limits are per agent replica.
  Sizing rule: `REQUEST_MAX_CONCURRENT` = 2 x the engine's concurrent sequence
  slots; the site sets it from its own engine numbers.
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
- **Maintenance rule:** a new `.example` key must join `OPERATOR_ENV_KEYS`
  and the Task bridge; `test_every_example_key_crosses_the_task_cli_and_beats_the_file`
  derives the keys from the example and fails until both are updated.
- The plaintext gateway-key guard (`LLM_API_KEY`, `EMBED_API_KEY`,
  `RERANK_API_KEY`, `CONTEXT_LLM_API_KEY`) scans the operator file text before it
  is sourced (any `export`/`readonly`/`declare`/chained form) and rejects any
  such variable whose sourced value differs from the caller's environment, so
  no shell syntax can smuggle a key in (#1016). Process-environment keys for
  `local:ask` stay allowed.
- Task discovery never loads private env files. Air-gap tasks bridge only
  caller-set operator values to `common.sh`; CLI assignments win over the
  caller environment before that script applies its precedence rule.
  `sh scripts/tools/run-task.sh airgap:dryrun` executes the pipeline with
  fixed stand-ins for its declared dry-run inputs, including the fake Open WebUI
  site inputs (`charts/mainframe-rag/ci/`), so Open WebUI renders on. Other operator keys still
  follow `common.sh`; inspect that owner before supplying private config.
  Task `--dry` only previews commands and is not rendering evidence. Render
  custom values through `airgap:pipeline AIRGAP_DRYRUN=1` or the scripts with
  explicit environment.

## 3. Render pipeline

Qdrant deploys separately from `charts/qdrant-1.19.2.tgz`; the first-party
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
Dry-run, and a no-MANIFEST run under the connected opt-out (see the
executing-checkout guard below), report "not release-verified".
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

- Open WebUI docs front end (issue #1074, ADR-0007, ADR-0008, **default on**): `OPENWEBUI_*`
  keys, validated by `model_config.openwebui_config` before any mutation, render the
  objects, Route checks and Pipe install described in
  [Docs front end](#docs-front-end). Unset means on and, while a required site input is
  missing, the deploy and the chart render refuse; `OPENWEBUI_ENABLED=false` opts out and
  renders nothing.

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
  manager or controller owner fails preflight. The refusal names the object it
  judged (apiVersion, kind, name; never values), for example
  `refused (application resource belongs to another Helm release: v1 ConfigMap/rag-openwebui-proxy)`.
  Every object the chart renders must be in that inventory, or the deploy after
  the one that created it is refused; a test feeds the full render back as the
  live inventory (#1134). Operators must serialize
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
  `QDRANT_EXTRA_VALUES` and `INGEST_EXTRA_PATCH` are CI-rehearsal overrides:
  the scripts refuse them unless `CI_REHEARSAL=1` is in the caller's process
  environment (an `airgap.env` assignment never counts; never set it in the
  air gap). Even then the patched ingest Job may differ from the render only in
  container `resources` (no image, env/hash mode, security context, volume,
  sidecar or identity change), and the Qdrant release is rendered with
  `helm template` and refused unless it is private and hardened (no
  Ingress/Route, ClusterIP Services only, API-key Secret present, no host
  namespaces or hostPath, non-privileged read-only-root containers, the verified
  registry image) before any `helm upgrade` (#1014). The container check is an
  allowlist: no added capabilities and `drop: [ALL]`, `runAsNonRoot` with no UID 0,
  no `hostPort`, host namespaces or RBAC objects, and only the chart's own
  ServiceAccount. `CI_REHEARSAL` is captured read-only before the operator file
  is sourced, so the file cannot forge it.
- **Site sizing keys (#1070).** A quota-bound site fits the release with these
  `airgap.env` keys, not with the CI-only overlays. Unset keeps the shipped
  default, so the default render is unchanged. Qdrant:
  `QDRANT_DATA_SIZE` (`persistence.size`), `QDRANT_SNAPSHOT_SIZE`
  (`snapshotPersistence.size`), `QDRANT_CPU_REQUEST`, `QDRANT_MEMORY_REQUEST`,
  `QDRANT_CPU_LIMIT`, `QDRANT_MEMORY_LIMIT` (`resources.{requests,limits}`).
  Ingest Job (`ingest.resources`): `INGEST_CPU_REQUEST`, `INGEST_MEMORY_REQUEST`,
  `INGEST_CPU_LIMIT`, `INGEST_MEMORY_LIMIT`. Worker count and scratch size are
  `INGEST_WORKERS` and `INGEST_WORK_SIZE`. CPU is whole/decimal cores or
  millicores (`500m`); memory and sizes need a Ki/Mi/Gi/Ti unit. Each value is
  validated before any change; a limit below its request (or below the default
  request when only the limit is set) is refused, and diagnostics never echo the
  value. The specific size keys win over `QDRANT_STORAGE_SIZE`. A StatefulSet's
  volume claim templates are immutable: before any change `deploy.sh` reads the
  existing Qdrant StatefulSet (read-only) and refuses when the rendered
  data/snapshot sizes differ from it, so keep the creation-time sizes in the
  file and grow a volume with `oc patch pvc` (`check_qdrant_volumes.py`).
  `QDRANT_EXTRA_VALUES` and `INGEST_EXTRA_PATCH` set outside `CI_REHEARSAL=1`
  refuse with a message naming these keys. These keys follow the
  [precedence rule](#2-environment-precedence) and the Task bridge like every operator key.
- `deploy.sh` validates every operator string that becomes a Helm value before
  rendering (`STORAGE_CLASS`/`SNAPSHOT_STORAGE_CLASS`: DNS-1123 name,
  `QDRANT_RELEASE`: DNS-1123 label, `QDRANT_TAG`: image tag,
  `QDRANT_STORAGE_SIZE`: positive quantity with a Ki/Mi/Gi/Ti unit, `INTERNAL_REGISTRY`:
  `host[:port][/path]`) and passes them with `--set-string`, so a comma or `=`
  can never become a second chart value (#1017).
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
`images.txt` digest is recorded; [image format](#bundle-image-format)), vendored chart, bootstrap script,
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
- `MANIFEST *_digest` lines are the archives' platform manifest digests (the
  manifest *list* pin resolves to one arch manifest when written, by
  construction) — the ref line carries the requested pin, the digest line
  carries the bundled bytes that load re-verifies. Confusing the two is the
  classic digest-mismatch false alarm.
- <a id="bundle-image-format"></a>**Image format (#1066).** Image members are
  `oci-archive` tars under the unchanged member names (`qdrant-image.tar`,
  `app-<role>-<SHA>.tar`, ...), and the signed MANIFEST declares
  `image_format: oci-archive`. Pack copies with `skopeo copy --preserve-digests`,
  so each archive holds the registry's platform manifest, config and compressed
  layer blobs byte-for-byte: no conversion, no recompression (third-party layers
  keep their upstream gzip; app layers keep the gzip the factory pushed). Each
  `*_digest` therefore equals the upstream platform manifest digest (for a
  manifest-list pin, the amd64 entry of that list), and `*_config_digest` the
  upstream config. Pack, load and the SBOM inventory read each archive through
  `image_manifest.py` (`oci-layout` version, an `index.json` naming exactly one
  single-image manifest, and every manifest, config and layer blob present once
  with its descriptor's size and sha256); a missing, shadowed or altered blob,
  a second image, or another format fails closed with a fixed message before
  signing (pack) or before any push (load). Load refuses any `image_format`
  other than `oci-archive` before its `IMAGE_SHA` check, so a bundle never
  mixes formats.
- Rollback across the format change: a bundle packed before #1066 (for
  example release `d53595e`) has docker-archive members and no
  `image_format` line. This release's `load.sh` refuses it with the fixed
  message "bundle image format is not oci-archive ..."; load it as before with
  that release's own `bootstrap.sh` and `load.sh` in a separate workspace
  ([rollback](install_and_ops.md#42-transfer--automated-bootstrap)). Its
  checkout-identity guard would refuse mixing it with this checkout anyway.
- Bastion prerequisites for `oci-archive`: skopeo ignores `TMPDIR` when it
  unpacks an `oci-archive` and uses `/var/tmp` unless the global `--tmpdir`
  flag is given. Where only a work directory is writable, put
  `--tmpdir /work/tmp` in `SKOPEO_ARGS` (for example
  `SKOPEO_ARGS="--authfile /work/auth.json --tmpdir /work/tmp"`); `load.sh`
  refuses before the first push unless that directory exists, is writable and
  has room for the largest member (at least 0.3 GB). A site
  `containers-policy.json` passed with `--policy` that admits only the
  `docker-archive` transport must also admit `oci-archive` (same
  `insecureAcceptAnything` scope: bundle trust is the signed checksum list,
  not registry signatures). The internal registry must accept OCI image
  manifests (Qdrant's platform manifest is OCI; Docker registry 2.x and
  current Quay do). A registry that rejects OCI manifests makes skopeo convert
  the push to Docker v2s2: the manifest digest changes but the config blob
  does not (observed with skopeo 1.22.2 on the Qdrant member), so load's
  config read-back passes and prints the registry digest, which deploy pins;
  it differs from the MANIFEST digest. Load refuses the tag only if the
  config itself changed.
- Release-signing custody (#1000): CI holds the key only as an **environment**
  secret `SNEAKERNET_SIGNING_KEY` of the GitHub environment `release-signing`
  (maintainer setup: Settings > Environments > New; Deployment branches =
  selected branches `main`; Required reviewers = the maintainer; move the secret
  there and delete any repository-level copy, since a same-repo PR workflow can
  read repository secrets). `airgap-package` selects that environment only for a
  manual `workflow_dispatch` on `refs/heads/main` (the required reviewer
  approves it); only that run produces a signed, promotable bundle. Every
  other run, including the nightly `schedule` (#1051, so it needs no daily
  approval), uses the secret-free `branch-unsigned` environment, packs with an
  ephemeral key and records `signed: ephemeral`; such bundles are not
  promotable. `SNEAKERNET_KEY_TRUSTED=true` is exported only for that
  dispatch on `refs/heads/main`. `tests/test_airgap_pack_sh.py` pins this.
- `signed: true` appears only with a custodied key (`SNEAKERNET_KEY_TRUSTED`);
  rehearsal keys record `ephemeral`. The label is honesty, never a trust
  root. Trust roots, strongest first: a `SNEAKERNET_TRUSTED_PUB` obtained
  out of band (load/bootstrap refuse mismatches byte-for-byte), the
  published key fingerprint in `PACKING_RECORD.txt`, HTTPS download of the
  tarball. Without a pinned pubkey, verification is TOFU: it binds members
  together but proves nothing about *which* key signed. The executing-checkout
  guard below therefore requires `SNEAKERNET_TRUSTED_PUB` outside dry-run
  (#1118 F1), so load, deploy, ingest, validate and retire-legacy never run
  on TOFU alone outside the connected opt-out described there.
- Load re-verifies the signature, then checksums, then the `IMAGE_SHA`
  against the manifest, then all six base image digests (plus the
  oauth-proxy digest when the bundle carries it) — and pushes under fixed
  names: `qdrant/qdrant:v1.19.2-unprivileged`,
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
  pin. With the registry's manifest bytes kept in the archive it normally equals
  the MANIFEST archive digest, but a registry may re-serialize, so identity is
  the config digest, never digest equality (see
  [image identity](#image-identity-across-archive-and-registry-formats)).
  Dry-run reads no registry and says so ("not release-verified").
  Load also requires each `<image>_config_digest` in the MANIFEST to equal its
  archive's image config before any push. Deploy and ingest repeat the registry
  read-back and render by digest (see the image identity section below).
- Executing-checkout guard (`common.sh::check_checkout_sha`, run by load,
  deploy, ingest, validate and `retire_legacy.sh`, before any cluster or
  Qdrant contact). Outside dry-run the checkout must be a
  verified signed release (#1118 F1, ADR-0009 §1): a packed MANIFEST must be
  reachable, and:
  1. *MANIFEST authenticity.* The guard never reads a bare file. `SHA256SUMS`,
     `SHA256SUMS.sig` and `sneakernet-signing.pub` must sit next to it, the
     bundled key must equal a configured release trust anchor byte-for-byte,
     the signature over `SHA256SUMS` must verify with that anchor, and
     `SHA256SUMS` must list `MANIFEST.txt` exactly once with the file's real
     sha256. The anchor is `SNEAKERNET_TRUSTED_PUB` and it is required; the
     guard reads anchors from the variable-name list
     `RELEASE_TRUST_ANCHOR_VARS` so a second (gap) key can join it. Image
     archives stay load.sh's `sha256sum -c`; only this MANIFEST link is
     rechecked.
  2. *Identity.* HEAD must equal the packed SHA and no tracked file may differ
     from it, staged or unstaged (scripts, charts, Taskfile — the whole
     tracked tree).
  3. *No hidden edits.* `git diff` trusts the index, so any tracked file
     flagged assume-unchanged or skip-worktree (`git ls-files -v` tag other
     than `H`; sparse checkout sets skip-worktree) is refused, with or
     without an edit. An unreadable index is refused. Clear the flags with
     `git update-index --no-assume-unchanged/--no-skip-worktree`. The guard's
     git reads are pinned to the executing checkout and ignore replace refs
     and the caller's `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, object
     store, namespace and config variables (#1149 N1); the rest of the stage
     keeps its environment.
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
  evidence, so connected development (under the opt-out below) in a directory
  beside such a file is not refused. Counting evidence can only make the guard refuse, never pass.
  A missing MANIFEST and a missing anchor are refused too, each with a
  fixed message naming the fix. Only dry-run still runs,
  printing "not release-verified" on stderr — never a release-verified
  result.
  *Connected opt-out.* `AIRGAP_UNVERIFIED_CHECKOUT=1` restores the pre-F1
  rules for connected developers and the connected CI and Kind lanes: no
  MANIFEST runs with the "not release-verified" notice, and without an anchor
  the signature check is TOFU (a configured anchor still pins the key); a
  reachable MANIFEST is still checked against HEAD and the tree. It is read
  from the process environment only (an `airgap.env` assignment is ignored)
  and is refused with a fixed message unless `CI_REHEARSAL=1` is in the
  process environment too; any value other than `1` or unset is refused.
  Never use it in the air gap: gap rehearsals use signed gap releases.
  `bootstrap.sh` applies the same tracked-change and
  index-flag refusals to an existing workspace before copying artifacts into
  `dist/`. Not covered: edits to files the checkout does not track
  (`airgap.env` is operator config by design), and an anchor file that is
  itself replaced on the bastion.
- `bootstrap.sh` cannot source `common.sh` (no clone exists yet), so it
  carries an inline twin of the trust check (bundle signature honoring
  `SNEAKERNET_TRUSTED_PUB`, then `SHA256SUMS`). There the anchor stays
  optional, since bootstrap only creates the workspace; every later live
  stage requires it (guard above). It clones into
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

<a id="release-archive"></a>
### Release archive (#1118, decision D5)

The 3 newest site releases are kept as **private** GHCR OCI artifacts at
`ghcr.io/yonatan895/qdrant-pdf-rag-release-archive:<release-sha>`, as input
for the previous-release to new-release upgrade lane. Each artifact holds the
loose signed bundle members that the site received, one layer per member.
[`release-archive.txt`](../release-archive.txt) is the only record. It holds
one row per release: SHA, manifest digest, SHA-256 of the signing public key
file, and the packed MANIFEST `date:`. Consumers pull by that digest, never by
tag. `scripts/release_archive.py` writes the record. It uses ORAS
([pin](../scripts/tools/oras-pin.txt), installed offline by
`scripts/tools/install-oras.sh --archive`, binary checksum verified before
every run) and `gh` for the Packages API:

- `verify`/`push --bundle DIR --release-sha SHA --trusted-pub PUB` fails
  closed before any registry call in these cases: the bundled key differs from
  the out-of-band key, the `SHA256SUMS.sig` signature does not verify, a file
  is unsigned or missing, a member checksum does not match, or MANIFEST does
  not name `SHA`. Push refuses when the package is not private, both before and
  after the push; that state is fixed in the package settings and never by the
  tool. Push never overwrites an existing tag. A tag that already exists with
  identical layers is adopted, which recovers a push that was never recorded.
- Privacy boundary: push only with the maintainer's own `gh` credentials. Never
  push this package from a workflow with `GITHUB_TOKEN`, and never add an
  `org.opencontainers.image.source` annotation. Either one links the package to
  the public repository, so it needs a visibility decision first.
- `pull --digest D --out DIR --trusted-pub PUB` accepts only a recorded
  digest and the recorded key. It refuses a manifest that does not hash to `D`.
  It downloads each layer blob over the registry API with HTTP Range, using the
  registry bearer token, which is kept in memory and never printed. The registry
  is pinned to `https://ghcr.io` (loopback only for tests), and the `gh` token is
  sent only to a token realm on exactly that origin; a challenge naming any other
  realm is refused before credentials are sent. A dropped connection resumes from
  the partial file `.DIR.partial/<layer digest>.partial` beside `DIR`, also
  across reruns; partials of other artifacts are left in place and reported. A server that ignores Range restarts the blob from byte 0.
  The registry's redirect to blob storage is followed without the
  `Authorization` header. `--attempts` bounds consecutive tries without
  progress, with exponential backoff (`--backoff`). The whole file is then
  re-hashed against its layer digest; on a mismatch the partial is deleted and
  pull refuses. `DIR` may hold only this artifact's members, and a member that
  already matches is kept. Finally pull re-runs the full bundle verification.
- `list [--remote]` prints the rows newest first and marks which ones prune
  removes. `prune` keeps the 3 newest by packed date, and refuses `--keep`
  below 3. It deletes a dropped digest's package version, then its row. A rerun
  converges. `prune` needs the `delete:packages` gh scope.
- SIGTERM exits through cleanup, so the temporary registry config holding the
  `gh` token is removed. SIGKILL cannot be handled and may leave it in `TMPDIR`.

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
  default 100Gi, never deleted). Parse workers also put their private
  per-document PDF copies under `/work/parse-tmp` (`INGEST_PARSE_TMP_DIR`,
  issue #1058): size `INGEST_WORK_SIZE` for the existing scratch plus about
  `ingest.workers` x the largest PDF at peak (`ingest_max_pdf_bytes` defaults
  to 0, unbounded). A full volume fails only the affected document with
  `ParseScratchError`; previous runs are deleted before re-apply
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
never migrated; deploy and validate refuse it before any change, and the
upgrade from it is the maintenance-window procedure in
[install and operations](install_and_ops.md#upgrade-from-a-legacy-flat-collection-issue-1070). Supported recovery from lost distributed storage is
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
  `--remove-signatures` because OCI archives cannot store Red Hat's upstream
  signature attachments. Source digest verification, archive digests, and the
  offline bundle signature still apply; upstream signature attachments are not
  part of the handoff. The explicit PENDING state still skips the member and
  makes `AGENT_ROUTE=true` fail closed. A tag bump for `ose-oauth-proxy`
  must change all three sites: `images.txt`, `deploy.sh`, `load.sh`.
- OpenObserve OSS and otelcol-contrib are pinned and bundled by
  [ADR-0006](adr/0006-openobserve-traces-metrics-backend.md) (#956). Their
  digests live in [`images.txt`](../images.txt), the deployment constant owner;
  never copy them into prose. A bump of either changes `images.txt`, the
  `licenses/inventory.json` bind, `load.sh` and `image_identity.sh` together.
  OpenObserve is AGPL-3.0, otelcol-contrib Apache-2.0
  ([licensing](licensing.md)).
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
| Qdrant | `qdrant/qdrant:v1.19.2-unprivileged` | tag (the vendored chart cannot render a digest); running `imageID` compared | `load.sh` | cluster pods |
| OpenObserve OSS (ADR-0006) | `openobserve/openobserve:v1.1.2` | as the agent; the chart references it by digest only (`images.openobserve.digest`) | `load.sh` | cluster pods when tracing is on with an empty `tracing.endpoint` |
| otelcol-contrib (ADR-0006) | `otel/opentelemetry-collector-contrib:0.160.0` | as the agent | `load.sh` | cluster pod (`Deployment/otelcol`, container `otelcol`) beside OpenObserve |
| OAuth sidecar (optional) | `openshift4/ose-oauth-proxy:v4.14` | as the agent; absent while `images.txt` says `sha256:PENDING` | `load.sh` | cluster pods |
| Open WebUI front end (default on, ADR-0007/ADR-0008, #1074) | `qdrant-pdf-rag-openwebui:v0.12.3-slim` | as the agent; absent while `images.txt` says `sha256:PENDING` (the factory records the pushed manifest digest of the image built from `images/openwebui/Containerfile`, the recipe the `scripts/openwebui/` dev environment and acceptance lane also build); the upstream `open-webui/open-webui:v0.12.3-slim` line is factory build provenance, never pushed | `load.sh` | cluster pod (`Deployment/rag-openwebui`, container `openwebui`) when `openwebui.enabled` |
| Open WebUI allow-list proxy (ADR-0007 E1, **Verifier-owned** where the workflow builds or mirrors images) | `nginxinc/nginx-unprivileged:1.30.6-alpine` | pinned by digest in `images.txt`; bundled as `openwebui-proxy-image.tar` exactly when the Open WebUI member is | `load.sh` | cluster pod (`Deployment/rag-openwebui`, container `proxy`) when `openwebui.enabled` |
| CI job image | `ci/python:3.14`, or a complete `CI_PYTHON_IMAGE` | mirrored UBI Python 3.14 (GIL) with git, pip, tar, sha256sum | platform or factory mirror, not this repository | GitLab runner |
| Dev workspace image (ADR-0009 §8, #1118) | `qdrant-pdf-rag-devkit:<kit key>` | manifest digest in `scripts/tools/devkit-pin.txt` (`sha256:PENDING` until the factory pushes); built from `images/Containerfile.devkit` by `.github/workflows/devkit.yml`; carried by the separate dev kit, never a release-bundle member or an `images.txt` line | the dev kit install (follow-up) | bastion, dev pod and runner jobs, from the templates and runner configurations in `deploy/workspace/` (contract: `deploy/workspace/contract.json`) |
| CI Qdrant service | `qdrant/qdrant@sha256:<images.txt digest>` | the `images.txt` manifest digest, so a tag is never enough | registry-to-registry copy that preserves the digest | GitLab runner (`sim`, `gate-l1`) |

The CI Qdrant service is a different role from the runtime Qdrant image. `load.sh`
pushes the single platform image from an OCI archive: its registry manifest digest
is the platform digest, never the `images.txt` manifest-list pin (see above), so a
digest reference to the pin only resolves for a copy made registry to registry. Prove it with `skopeo inspect` against the `images.txt`
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
| GitLab | Release version recorded, 16.10 or later (`workflow:auto_cancel`); protected tags; `CI_COMMIT_TAG` and `CI_COMMIT_REF_PROTECTED`; variables expanded in `image`, `services` and `tags`; `CI_OPEN_MERGE_REQUESTS`; dynamic child pipelines (`trigger:include:artifact`) | all pipelines |
| Runner | Tags `gate` (ready merge request and tag pipelines) and `wip` (branch and draft merge request pipelines), or one tag for all from the project variable `CI_RUNNER_TAG`; linux-amd64; an executor that starts job and service images from the approved registry with a service alias; reads `CI_TASK_ARCHIVE`, `CI_HELM_ARCHIVE` and `CI_BM25_CACHE_DIR` from the runner; no route to PyPI, Docker Hub or GitHub | all GitLab jobs |
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
and the dry-run gate; air-gap GitLab runs hygiene, lint/types, the unit suite
in four shards, hazards, the sim tier and gate-l1 only (no e2e, load, deploy,
multi-peer docker, GHCR, or PDFs/tokens/hostnames in file). Merge request and
tag pipelines run every GitLab lane on the `gate` runner tag, a draft merge
request every lane on `wip` (GitLab >= 17.10); branch pipelines
run on `wip` and select lanes with the fail-closed classifier through a child
pipeline; a newer commit cancels the older pipeline on the same ref, never a
tag pipeline (ADR-0009 section 2). GitLab unit/sim
reports reject any skipped or xfailed test (`scripts/check_junit_clean.py`);
`tests/test_ci_gitlab_parity.py` pins the correspondence and failure propagation. Job meaning stays aligned across the two files; only e2e-scale jobs
live in `.github/workflows/e2e.yml`.

- `ci.yml`/`load.yml`/`ha.yml`/`agent-probes.yml`/`browser.yml` run on
  `pull_request` and nightly (`schedule`, staggered UTC) plus
  `workflow_dispatch`, never on push to `main` (maintainer decision 2026-10-23);
  candidate acceptance reads only `pull_request`-event runs. Triggers, path
  allow-lists, sharding and job wiring are owned by the workflow files.
  `agent-context.yml` has no path filter (root-only AGENTS changes still run
  `qa:context`); `bench.yml` is the only docs-path-ignore lane.
- `e2e.yml` builds images on PRs but pushes only on main push/schedule/dispatch;
  its `build` job is an all-success join, and packaging/acceptance/rehearsal
  consume only verified bundle artifacts. Heavy jobs (`airgap-package`,
  `airgap-acceptance`, `kind-live-rehearsal`, `airgap-rehearsal`) are
  nightly/dispatch only; PRs never touch the lab cluster. The lab OpenShift
  rehearsal stays secret-gated, and a skipped lab job supplies no OpenShift
  verification.
- Policy: least-privilege permissions, timeouts, a top-level concurrency group
  (cancel-in-progress on PRs; main runs share `ci-main` and never cancel) and
  SHA-pinned third-party actions. `load.yml`/`ha.yml`/`browser.yml` fail closed
  on skips (`qa:ha` requires at least one pass; the browser lane uses a
  digest-pinned Chrome for Testing). `coverage.yml` is advisory and never a
  required check. `bench.yml` is never a PR gate and keeps its measurement
  phases sequential. No GPU work is added, and neither the runner nor path
  classification authorizes new network access or cluster operations.
- CI installs the pinned host Task into `.tools/bin` and dispatches through
  `run-task.sh`, checksum-verified, with no public download; offline GitLab
  mounts the approved Task/Helm archives. See
  [tool preparation](task-runner.md#installation).
- Pinned third-party versions live in-repo (kubectl + sha256, helm, kind, node
  image); the local-path provisioner manifest is pinned to a source commit and
  sha256-verified. The opencode reviewer workflow is GitHub-only (`gh workflow
  disable/enable opencode.yml` pauses/restores it); its state lives in GitHub.
- The local vLLM launcher resolves (never probes) flags from the `serve` Budget
  profile; values are owned by `install_and_ops.md` §3.6 and
  `src/mainframe_rag/serve/`.

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
digest, loaded registry manifest digest and actual pod `imageID`. The bundle's
OCI archives keep the registry's manifest bytes, but a manifest-list pin is not
the platform digest, and a registry may re-serialize a pushed manifest, so
those manifest digests can differ. Verify the archive signature/checksums and
its manifest against the bundle first, then verify identical image config/rootfs
diffIDs across the load, and the running digest against the loaded registry.
Do not compare an archive digest blindly to a registry digest or accept a tag
alone. Capture all five images, including every OAuth sidecar.
Implemented (issue #272), as one chain bound to the signed MANIFEST:

1. `pack.sh` records `<image>_config_digest` for all five images next to the
   archive `<image>_digest`, both read from the verified OCI layout. `load.sh`
   re-reads every archive the same way and refuses one whose blobs, digest or
   config differ (before any push), then reads each pushed tag back and requires
   the packed config digest and layer count; it prints `ref@registry-manifest-digest`.
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
Qdrant server and chart track 1.19.2 (client 1.19.0; server and client agree on major.minor, the chart matches the server; maintainer, 2026-10-23) and the unprivileged image. The connected
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

**Disk-full headroom and the required site alert (S7).** A full `openobserve-data` volume makes
OpenObserve acknowledge writes (HTTP 200) while silently dropping records (400 acknowledged, 159
stored on v1.1.2). The product has no in-app signal for it: `zo_node_disk_usage` measures the
node root filesystem, not the data volume, and `ZO_DISK_CIRCUIT_BREAKER_ENABLED` refused nothing in
the v1.1.2 test (ADR-0006). Two controls are therefore required, and the chart ships no
PrometheusRule (it would depend on user-workload monitoring, which ADR-0006 replaces).
- **Headroom.** The retention budget is about 90 bytes per span: 14 days at the ADR-0002 bound is
  about 0.6 GB (6% of the 10Gi default; 30% with a 5x margin for less repetitive real spans).
  Keep `openobserve.storage.size` so the budget stays at or below 30% of the volume; re-measure on
  the site and grow the PVC before the budget passes that.
- **Alert (site-owned).** Alert on the platform's kubelet volume metrics for PVC `openobserve-data`:
  `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes` above 0.70 (the margin
  between the 30% budget and a full volume), routed to the site's on-call destination. This is a
  required item on the site-check list below the ADR-0006 open checks (#807).

<a id="docs-front-end"></a>
## Docs front end: Open WebUI (ADR-0007, #1074)

**Naming (maintainer decision 2026-10-26, #1070).** *Rosetta* is the site-facing name of the docs
assistant: the Open WebUI model users pick, the Route host (suggested example
`rosetta.apps.<cluster-domain>`, a placeholder), the AD FS client name and the user docs. Code
identifiers stay `mainframe_rag`, `mainframe-rag` and the Pipe/model ID `mainframe_rag_docs`; Open
WebUI's own branding stays unmodified (ADR-0007 L1) and the console keeps its title.

Open WebUI is the main docs platform and is **on by default** (maintainer decision 2026-10-26,
ADR-0008); the console stays the frozen fallback. It is a front end for the docs corpus and a
client of the agent: its only model is the repository docs Pipe (#1072) over `/v1/chat`. It
holds no gateway key and no model connection, and it does no retrieval of its own. The image pin
is recorded in `images.txt`; the 2026-10-24 Kind trials are interim and partial
(runs 1-2 of the #1073 qualification, not a clean pass; open residuals are listed
in the trial records), and every pin import re-runs the ADR-0007 conditions
(ADR-0008 item 2). The environment variable
names, uvicorn TLS arguments, Functions API used by the Pipe installer and chat table layout
used by the purge are those of the pinned v0.12.3 build.

**Enabling it at a site (default).** A site that does nothing gets Open WebUI, so the deploy
refuses until the site supplies every required input. Both layers refuse before any cluster change:

- `airgap.env` layer (`validate.sh`, `map_values.py`): refuses with one fixed message that names the
  missing keys (never values), from `OPENWEBUI_SECRET`, `OPENWEBUI_HOST`,
  `OPENWEBUI_OIDC_PROVIDER_URL`, `OPENWEBUI_OIDC_CLIENT_ID`, `OPENWEBUI_OIDC_ALLOWED_ROLES`,
  `OPENWEBUI_OIDC_ADMIN_ROLES`, `OPENWEBUI_IDP_EGRESS_CIDRS`, and
  names the opt-out `OPENWEBUI_ENABLED=false`. `ROUTE_DESTINATION_CA_FILE` is not an operator input on
  the live path (deploy fetches the service CA); only a dry-run render of the Route needs it.
- Chart layer (direct `helm template/upgrade`): the same eight inputs as chart values
  (`openwebui.secretName`, `host`, `oidc.providerUrl`, `oidc.clientId`, `oidc.allowedRoles`,
  `oidc.adminRoles`, `idpEgress.cidrs`, `route.destinationCA`); the render fails with a fixed message
  naming the missing values and `--set openwebui.enabled=false`. The schema also refuses an absent or null `oidc.groupClaim`, `oidc.rolesClaim` and `idpEgress.port`
  (a null port would otherwise render an egress rule open to all TCP ports). A half-configured Open WebUI is
  never rendered, and there is no placeholder host or local-login fallback.
- Opt out: `OPENWEBUI_ENABLED=false` in `airgap.env` (the mapper writes `openwebui.enabled: false`)
  or `--set openwebui.enabled=false`. The opt-out render is byte-identical to a chart without Open
  WebUI (tested).
- Test inputs: `charts/mainframe-rag/ci/openwebui-site-values.yaml` holds obviously fake values
  (`example.invalid`, TEST-NET CIDR) for lint and render tests, `artifacts:app-helm-*` and the
  `airgap:dryrun` stand-ins (with `openwebui-route-ca.pem` as the route CA). Never use them at a site.

**Upgrade consequence for existing sites.** A site upgrading from a release that predates this
default and whose `airgap.env` has no `OPENWEBUI_*` keys fails validation at the next
`airgap:validate`/`airgap:deploy`, before any cluster change, and keeps running the previous
release. Either supply the inputs and the Secret (procedure below) or set `OPENWEBUI_ENABLED=false`
to keep today's render unchanged. Scripts and CI that call `map_values.py` or `helm template`
directly need the same choice. The image pin must also be recorded and bundled (step 1 below) before
an enabled deploy can pass.

**What `OPENWEBUI_ENABLED=true` (`openwebui.enabled`) renders.** With it off, the render is
byte-identical to a release without it (tested). The chart owns the objects and their
hardening (`Deployment/rag-openwebui` with the chat-purge sidecar, `Service`, reencrypt
`Route`, `NetworkPolicy/rag-openwebui`, the Pipe ConfigMap, the optional IdP CA
ConfigMap); [ADR-0007](adr/0007-open-webui-docs-front-end.md) owns every condition those
objects must satisfy, and [openwebui-dev.md#gate-map](openwebui-dev.md#gate-map) maps each
gate to its proof. Two site-facing consequences: the ingress NetworkPolicy is the
zero-egress control (it needs an enforcing CNI, and the site provides that evidence), and
the Route-exposure preflight refuses any other Route in front of `rag-openwebui` whether
the front end is on or off.

IdP CA (optional, `OPENWEBUI_IDP_CA_FILE` / `openwebui.oidc.caBundle`): when the IdP's
TLS certificate comes from an enterprise CA (usual for AD FS), the PEM becomes
`ConfigMap/rag-openwebui-idp-ca` and a `ca-trust` init step mounts one combined bundle via
`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` for sign-in and token refresh; a changed CA rolls
the pod. Without it, sign-in fails at the server-side discovery call; a public-CA IdP needs
nothing. Mapping, the chart render and the init step each refuse an unreadable, blank,
non-certificate or corrupted bundle with fixed text without printing the value, so a bad
bundle cannot yield a Ready pod whose sign-ins fail. Token exchange stays disabled
(`ENABLE_OAUTH_TOKEN_EXCHANGE=false`), and so does the ID-token cookie
(`ENABLE_OAUTH_ID_TOKEN_COOKIE=false`, [below](#adfs-ldap-only-rule-form)).

**What the identity team registers.** One confidential OIDC client on the organisation's IdP
(AD FS, or Keycloak in front of it):
- Redirect URI: `https://<OPENWEBUI_HOST>/oauth/oidc/callback`. Scopes: `OPENWEBUI_OIDC_SCOPES`,
  default `openid email profile groups offline_access` (the IdP must permit every scope; for
  `offline_access` or its refresh-token equivalent: without a refresh token Open WebUI deletes the
  session near token expiry and can fall back to an older session's token, per the Kind trial).
  Client authentication by client secret.
- The **ID token** must carry the e-mail claim (`OPENWEBUI_OIDC_EMAIL_CLAIM`, default `email`),
  the group claim (`OPENWEBUI_OIDC_GROUP_CLAIM`, default `groups`) and the roles claim
  (`OPENWEBUI_OIDC_ROLES_CLAIM`, default `roles`). Open WebUI v0.12.3 reads them from the ID
  token. It calls userinfo only when the e-mail or username claim (`OPENWEBUI_OIDC_USERNAME_CLAIM`,
  default `name`) is missing there, and userinfo wins on a conflict. AD FS userinfo returns only
  `sub`, so AD FS issues these claims through issuance rules (below).
- Group values must be **names, not SIDs** (AD FS "Token-Groups - Unqualified Names"; in Keycloak,
  a group-membership mapper with full group path off). Groups are re-synced on every login.
- Roles claim values: `OPENWEBUI_OIDC_ALLOWED_ROLES` lists the roles whose holders become Open
  WebUI users (all employees in the pilot). `OPENWEBUI_OIDC_ADMIN_ROLES` lists the roles of the
  named owners who are Open WebUI admins. Admins can still read chats through the database or a
  Function (ADR-0007 S3), so keep that group small.
- The roles claim must be in **every** ID token. The IdP issues the baseline value `signed-in`
  to every user it authenticates, plus the group roles. Open WebUI v0.12.3 recomputes the role
  at every sign-in only when the claim is present: if the claim is missing, an existing user
  keeps the stored role, so a removed admin stays admin. Neither role list may contain
  `signed-in` or `*`; validate and the chart refuse both. An IdP that cannot issue a constant
  value uses the group claim as the roles claim instead (AD FS variant
  [below](#adfs-ldap-only-rule-form)).
- The team hands over: the discovery URL (`https://<idp>/.well-known/openid-configuration`), the
  client id, the client secret (it goes into the Secret, never `airgap.env`), and the IdP's IPv4
  CIDRs and port for the egress policy. If an enterprise CA issued the IdP's TLS certificate, they
  also hand over that CA's PEM certificate(s) for `OPENWEBUI_IDP_CA_FILE`.

**Register Open WebUI with AD FS.** AD FS 2016 or later. Placeholders: `<adfs-host>` is the
federation service host and `<host>` is `OPENWEBUI_HOST`. Statements marked *(confirm on the test
client)* are AD FS behaviour that could not be checked from a source here. The Open WebUI
statements come from the v0.12.3 source in the pinned image.
1. **Client.** Create an application group with a server application, which is a confidential
   client authenticated by a generated shared secret. Add a Web API whose identifier is the
   client id, so it can carry the scopes and rules below *(confirm on the test client)*. A
   suggested names are `rosetta` (production) and `rosetta-test` (test client); they are only suggestions. The redirect URI is exactly
   `https://<host>/oauth/oidc/callback`. The client id goes into `OPENWEBUI_OIDC_CLIENT_ID`, and
   the secret goes into the Secret as `oidc-client-secret`.
2. **Scopes.** Permit `openid`, `profile`, `email` and `allatclaims`, and set
   `OPENWEBUI_OIDC_SCOPES=openid profile email allatclaims`. AD FS puts the claims from
   issuance rules into the access token. They reach the ID token, which Open WebUI reads, only
   when `allatclaims` is requested *(confirm on the test client)*. Leave out `groups`, which is
   not an AD FS scope. Add `offline_access` only if AD FS accepts it for this client and returns a
   refresh token (see above) *(confirm on the test client)*.
3. **Claim rules** on the Web API. Each outgoing claim type is the plain name Open WebUI reads.
   AD FS then emits that name as the JWT claim name *(confirm on the test client)*.
   - E-mail: Open WebUI refuses a sign-in without the e-mail claim (HTTP 400). It reads one
     top-level claim, lowercases it and does not check its format. AD FS emits no `email` claim
     without a rule *(confirm on the test client)*. The usual choice is the UPN: map the LDAP
     attribute User-Principal-Name to `upn` and set `OPENWEBUI_OIDC_EMAIL_CLAIM=upn`.
   - Name: map Display-Name to `name`, the default `OPENWEBUI_OIDC_USERNAME_CLAIM`. When the name
     is missing, Open WebUI uses the e-mail as the display name and also calls userinfo.
   - Groups: map Token-Groups - Unqualified Names to the claim named in
     `OPENWEBUI_OIDC_GROUP_CLAIM` (`groups`). These are names, not SIDs. Several values arrive as
     a list. A single string value is split on `;`.
   - Roles, baseline first. Add a custom rule that issues the baseline to every authenticated
     user: `=> issue(Type = "roles", Value = "signed-in");`. Its `Type` is the
     `OPENWEBUI_OIDC_ROLES_CLAIM` name. Then add one "Send Group Membership as a Claim" rule per
     AD group, with the same outgoing type and a value listed in `OPENWEBUI_OIDC_ALLOWED_ROLES`
     (for example `docs-user`) or in `OPENWEBUI_OIDC_ADMIN_ROLES` (for example `docs-admin`).
     Several values arrive as a list. A single string is split on `,`. *(Confirm on the test
     client that the custom rule's type is emitted as `roles` in the ID token.)*
   - **What happens at each sign-in.** Open WebUI v0.12.3 recomputes the role from the claim at
     every sign-in (`utils/oauth.py` `get_user_role`, called from the callback for an existing
     user). The cases are pinned by a test in the pinned image:
     - An admin role is present: the user becomes `admin`.
     - Otherwise an allowed role is present: the user becomes `user`, so a removed admin is
       demoted.
     - Only `signed-in` is present: sign-in is refused. Open WebUI raises 403 internally and the
       callback answers with a redirect to `/auth?error=...` and no session cookie
       (`utils/oauth.py` `handle_callback`), so the browser shows an error on the sign-in page.
     - No claim at all: an existing user keeps the stored role, and a new user is `pending`.
       The baseline rule exists to prevent this case.
   - **Revocation window.** The IdP change takes effect at the next sign-in. A session that is
     already open stays valid until it expires (`JWT_EXPIRES_IN`, 8 hours). A refused sign-in
     does not change the stored role. That user's API keys therefore keep their old role until
     the next successful sign-in, or with no time limit for someone who never signs in again.
     To remove an admin at once, also demote or delete the account in the Open WebUI admin UI.
     That takes effect on the next request, because the role is read from the database on each
     request. If the account owns the installer's admin API key, rotate the key.
<a id="adfs-ldap-only-rule-form"></a>
**Variant: a self-service rule form that offers only LDAP-attribute rules.** Some AD FS
deployments give the application owner a form with fixed LDAP attribute queries only: no custom
rules and no group-to-constant-value rules. The `signed-in` baseline and the role-value rules of
step 3 then cannot be issued. Steps 1, 2 and 4 to 6 are unchanged; step 3 becomes:
- **Rules.** User-Principal-Name to `upn` and Display-Name to `name`, as in step 3. For groups,
  use the **domain-qualified** group-membership query (`tokenGroups(domainQualifiedName)`,
  values `DOMAIN\group`).
- **Settings.** `OPENWEBUI_OIDC_ROLES_CLAIM` equals `OPENWEBUI_OIDC_GROUP_CLAIM`: the group claim
  is the roles claim. `OPENWEBUI_OIDC_ALLOWED_ROLES` and `OPENWEBUI_OIDC_ADMIN_ROLES` are
  domain-qualified AD group names. Single-quote each list in `airgap.env`, for example
  `OPENWEBUI_OIDC_ADMIN_ROLES='DOMAIN\docs-admins'`. The file is sourced by `sh`, which removes an
  unquoted backslash, so validate and deploy refuse a role line whose backslash is outside single
  quotes (fixed message, value not shown). Names are comma-separated with no spaces. Open WebUI
  v0.12.3 compares them exactly and case-sensitively with the claim values (`utils/oauth.py`
  `get_user_role`; the configured names are trimmed, the claim values are not), so copy the case
  the token carries.
- **Why domain-qualified.** Group membership spans the forest and its trusts. A bare name such as
  `docs-admins` also matches a same-named group in any other domain whose users can sign in
  through this AD FS: that fails open, as a silent admin grant, and admins can read chats. A
  lost backslash fails closed: `DOMAIN\docs-admins` becomes `DOMAINdocs-admins`, which matches
  nobody. Bare names (the unqualified query) are an exception for a forest with one domain and
  no trusts, which the identity team must confirm in writing.
- **Revocation.** Token-Groups always includes the user's primary group (Domain Users), so the
  claim is present in every token. A user in neither docs group carries only values that match
  no configured role and is refused (redirected to `/auth?error=...` without a session), so the
  guarantee above still holds without a
  baseline. The chart's refusal of `*` and `signed-in` stays. A group literally named `*` cannot
  be configured, and a claim value `*` matches nothing (only a configured `*` is a wildcard).
- **Token size.** Every group of the user is in the ID token. By default Open WebUI v0.12.3
  also sets the raw ID token as the `oauth_id_token` browser cookie on the sign-in callback
  (`utils/oauth.py` `handle_callback`). The pod's allow-list proxy reads that response header into
  nginx's default `proxy_buffer_size` of one 4 KiB page, so a large token failed the sign-in with
  HTTP 502 (measured on the pinned nginx image and the chart's config). Nothing reads the cookie
  back, so the chart sets `ENABLE_OAUTH_ID_TOKEN_COOKIE=false` (maintainer decision). The callback
  headers are then about 800 bytes whatever the token size, and Open WebUI keeps the token in its
  database (`models/oauth_sessions.py`). One step still carries the whole token: sign-out (below)
  puts it in the IdP logout URL. AD FS runs on Windows http.sys, which by default limits the
  request line and headers to 16384 bytes (Microsoft's http.sys registry documentation; not
  verified here). The claims check warns above 8000 bytes; above that, confirm sign-out on the
  test client. No size remedy is needed for sign-in. If sign-out to the IdP fails for a very
  large token, the Rosetta session is already gone: the sign-out response deletes its cookies
  before the redirect. Do not shrink the group claim: in this form the shorter queries either
  give bare names (fail open, above) or drop the primary group, and so lose the claim that
  revocation relies on.
- **Sign-out.** Open WebUI v0.12.3 (`routers/auths.py` `signout`) deletes its session cookies and,
  for an OIDC session, sends the browser to the IdP's `end_session_endpoint` from discovery with
  `id_token_hint` taken from the server-side OAuth session (not from a cookie). Whether AD FS then
  ends its own SSO session is AD FS behaviour *(confirm on the test client)*. The chart has no
  Redis, and v0.12.3 revokes session tokens only through Redis (`utils/auth.py`
  `invalidate_token`). Sign-out therefore removes the browser's Rosetta session, but a copied
  session token stays valid until it expires (8 hours).
- **Logs.** A refused sign-in makes Open WebUI log a warning that lists the user's roles claim
  values, that is, all their group names (v0.12.3 `get_user_role`). This is identity metadata in
  the pod log, not chat content.
- **Check.** Run the [claims check](#adfs-claims-check) in role-list mode before deploying. In
  step 6, the refusal of the account in no docs group shows that the group claim arrives.

4. **CA.** If the enterprise CA issued the AD FS TLS certificate, export that CA (root and any
   intermediate) as PEM and set `OPENWEBUI_IDP_CA_FILE`. Without it, Open WebUI cannot reach the
   discovery URL.
5. **Egress.** Set `OPENWEBUI_IDP_EGRESS_CIDRS` to the IPv4 addresses that `<adfs-host>` resolves
   to from the cluster, and `OPENWEBUI_IDP_EGRESS_PORT` (443). Open WebUI calls discovery, token,
   JWKS and, when needed, userinfo there.
6. **Verify.** First fetch
   `https://<adfs-host>/adfs/.well-known/openid-configuration` from the bastion (with
   `--cacert` set to the CA file). It must return JSON whose `scopes_supported` includes
   `allatclaims`. This URL is `OPENWEBUI_OIDC_PROVIDER_URL`. After deploy, sign in once with a
   test account that is in an allowed-role group and in one docs group. The account must appear
   in the Open WebUI admin user list with its UPN and the `user` role, not `pending`. It must also
   be a member of that group; the chart does not create groups from the claim, so create the
   group first. This shows that the ID token carried the e-mail, role and group claims. Then
   prove revocation: sign in with a test account that is in no role group. It must be refused
   (a redirect to `/auth?error=...` with no session cookie, shown as an error on the sign-in
   page). That shows the baseline claim arrives. Next, remove
   a test admin from the admin group, keeping it in the user group. At its next sign-in it must
   show the `user` role.

**Site procedure.**
1. On the connected side, build the derived image from `images/openwebui/Containerfile` (upstream
   slim digest plus one ownership layer), push it, and record its manifest digest in `images.txt`. Then pack and load: the bundle carries `openwebui-image.tar` only once the pin is
   recorded, and validate/deploy refuse `OPENWEBUI_ENABLED=true` while the pin is
   `sha256:PENDING` or does not match the chart and the bundle.
2. Create the Secret (`OPENWEBUI_SECRET`) with `webui-secret-key` (`openssl rand -hex 32`) and
   `oidc-client-secret`. Deploy checks that both keys are present before any mutation.
3. Set the `OPENWEBUI_*` keys in `airgap.env` (see `airgap.env.example`) and run
   `sh scripts/tools/run-task.sh airgap:deploy`. Deploy verifies the registry image
   and then the running pod's digest (`check_pod_images`, role `openwebui`), waits for the
   rollout (schema migrations at first start), and verifies the Route.
4. First admin: a member of an admin role signs in once, creates an API key in Open WebUI, and
   adds it to the Secret as `admin-api-key`. Re-run `airgap:deploy`. The installer then runs
   inside the pod with the key on stdin. It refuses a mounted Pipe whose sha256 differs from the
   repository file, sends the key only to the pod's own serving certificate, proves it is an
   admin key on the admin-only Function listing (a refused key, including a non-admin one,
   fails there and nothing is written), creates the Function when the listing lacks it (v0.12.3
   answers an absent Function with 401, so a 401 on the Function itself never decides) or
   updates it, reads it back by hash and activates it. Until the key exists, deploy prints
   `docs Pipe NOT installed` and the front end offers no model.
5. Give all signed-in users read access to the Rosetta model in the Open WebUI admin UI (the
   default visibility is a #1073 trial item). Then sign in as an ordinary user and ask one
   question: citations show as source chips, and the console still works.

**Retention (ADR-0007 S2).** The window is `OPENWEBUI_RETENTION_DAYS` (`openwebui.retentionDays`),
default **30 days** (maintainer decision 2026-10-24). An explicit null fails the render.
- The chat-purge sidecar runs hourly. In one transaction it deletes the chats last updated before
  the window, their shared snapshots and stale snapshots, every row that references a deleted
  chat by `chat_id`, and file records with their uploads. Deletion runs with `secure_delete`;
  the sidecar then truncates the WAL and runs `VACUUM`, so the text is gone from the claim's
  files. Users may also delete their own chats.
- An unexpected table layout or a timestamp that is not epoch seconds refuses to delete
  anything. A busy database or a failed vacuum is a failed run. Each run logs one line of counts.
  Failures are retried after 5 minutes and counted in `rag-openwebui-data/.mainframe-rag-purge.json`
  (`consecutive_failures`, `total_failures`, `last_success`). Alert on `consecutive_failures`
  above 0 for more than a day.
- Backups of `rag-openwebui-data` hold chat text. They are protected records that never leave
  the enterprise, and their retention follows the same window: at most 30 days plus one backup
  cycle.

**Patch policy and advisory switch-off (ADR-0007 P1).** The maintainer owns patching. Agents
prepare a monthly import of the latest upstream release as a dedicated pin-bump PR, carrying the
base and derived digests, the licence record, the re-run of every ADR-0007 condition, and an
upgrade of a PVC copy. Upstream fixes land only on its latest release. If a published High or
Critical advisory affects the running pin and stays unpatched for **14 days**, switch the front end
off with the rollback flag below until a qualified import is deployed.

**Rollback (K1).** Set `OPENWEBUI_ENABLED=false` and re-run `airgap:deploy`. Helm removes the
Deployment, Service, Route, NetworkPolicy and ConfigMap. The claim `rag-openwebui-data` is kept
(`helm.sh/resource-policy: keep`), and nothing purges it while the front end is off. The agent
and the console are unchanged. Open WebUI chats cannot move into the console. A database that a
newer pin has migrated rolls back only through its pre-upgrade snapshot. To retire the front end
for good, delete the claim explicitly (`oc delete pvc rag-openwebui-data`); no deploy path does
that.

**Allow-list proxy (ADR-0007 E1).** Open WebUI v0.12.3 disables some features in its UI
only: `/api/v1/files` stores uploads with uploads off (Kind trial V1), the
`/api/v1/retrieval/process/*` routes check no feature flag, and knowledge and audio rest on
permissions that admins bypass. The server also decodes percent-encoding before routing, so a
raw-path Route cannot block `/api/v1/%66iles/` or `/api/v1%2Ffiles/`. The pod therefore runs
an `nginx-unprivileged` sidecar (`proxy`, pinned in `images.txt`, bundled with the Open WebUI
image as `openwebui-proxy-image.tar`) as its only listener. Open WebUI listens on
`127.0.0.1:8080`. The Service, Route and probes target the proxy on 8443; the config is
`charts/mainframe-rag/files/openwebui/proxy.nginx.conf`, shipped verbatim in
`ConfigMap/rag-openwebui-proxy`. It decides on nginx's decoded, normalised `$uri`
(`merge_slashes on`), forwards the original request line, and answers `403 denied` for
everything outside the allow-list. Access logs are off.

Site acceptance, with `$C` a signed-in user's session cookie and `$H` the Route host. Every
request must return `403` (or `400` for the null byte) and must store nothing:

```sh
for p in /api/v1/files/ /api/v1/files /API/v1/files/ //api/v1/files/ /api/v1/./files/ \
         /api/v1/%66iles/ /api/v1%2Ffiles/ /api/v1/files%2F /%61pi/v1/files/ \
         /api/v1/chats/%2e%2e/files/ /api/v1/%2566iles/ /api/v1/files%00 \
         /api/v1/retrieval/process/text /api/v1/knowledge/ /api/v1/audio/transcriptions \
         /openai/chat/completions /ollama/api/chat /api/v1/utils/code/execute; do
  printf '%s ' "$p"; curl -sk -o /dev/null -w '%{http_code}\n' -X POST -b "$C" "https://$H$p"
done
```

Then sign in and open a chat: a conversation must answer through the Rosetta model.

**Re-audit the allow-list on every Open WebUI import (ADR-0007 P1).** Each import PR must (1) list
the router prefixes and handlers of the new release (`main.py` `include_router`, each
router's feature and permission checks) and sort them into server-enforced and UI-only; (2) record
the table in the PR; (3) add nothing to the allow-list that is UI-only for a disabled feature;
(4) re-run the nginx config test (`tests/test_openwebui_proxy_nginx.py`, pinned image) and the
curl checks above; and (5) re-run a browser trace of the pilot flows (sign-in, chat, admin
pages, Pipe install) to catch a path the UI newly needs. A UI error on a denied path is the
signal to decide, not to widen the list by default.

**Admin API key (installer).** The Pipe installer needs one admin key. The chart sets
`ENABLE_API_KEYS=true`, `USER_PERMISSIONS_FEATURES_API_KEYS=false` (users cannot create keys;
admins bypass the permission) and `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS=true` with
`API_KEYS_ALLOWED_ENDPOINTS=/api/v1/functions`, so a key works only on the Functions API.
Rotate or delete the key after an install if you prefer: `deploy.sh` prints the explicit
not-done notice without it. The settings are pending #1073 run-2 qualification.

**Requirements.** The Route API (OpenShift; deploy refuses the front end without it) and an
enforcing CNI. The Kind lanes do not enable the front end yet: an e2e run with it enabled needs
the #1073 test IdP and the #1072 Pipe.

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
`INGEST_REINGEST`, `INGEST_RETIRE_DOCS` and `INGEST_AUTHORIZE_FROM_CATALOG` are `OPERATOR_ENV_KEYS` validated by
`scripts/airgap/ingest.sh` and rendered into the ingest Job args (`INGEST_RETIRE_DOCS` accepts comma- or newline-separated
`DOCID[@SOURCEREV]` entries and preserves interior whitespace in product/version labels;
`INGEST_AUTHORIZE_FROM_CATALOG=true` maps to `ingest.authorizeFromCatalog` and renders
`--authorize-from-catalog`, which lets a retirement name a revision the live build published and
committed after the local inventory was lost. It is valid only with `INGEST_RETIRE_DOCS`: the values
mapper refuses it alone with `INGEST_AUTHORIZE_FROM_CATALOG requires INGEST_RETIRE_DOCS`, and the
chart schema and template refuse it too. See
[ingest refresh lineage](ingest.md#9-orchestration-run_ingestpy-inventorypy));
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
changes together. GitLab merge-request and protected-tag pipelines run every lane
(#808); branch pipelines run the lanes `scripts/review_tooling.py` selects, as a
child pipeline. GitLab uses images from the operator-set CI_APPROVED_REGISTRY (never the
GitLab registry; CI_PYTHON_IMAGE overrides the job image), the `gate`/`wip` runner tags
(CI_RUNNER_TAG overrides both) and internal
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

<a id="adfs-claims-check"></a>
## AD FS claims check (bastion, before any Open WebUI deploy)

`scripts/openwebui/adfs_claims_check.py` proves, from the air-gapped bastion, that the AD FS
client for Open WebUI issues the claims the chart reads. It is stdlib-only (`python3 -I`),
talks only to the AD FS host, and checks `iss`, `aud`, `nonce`, `exp` and the claim
expectations. It prints claim **names**, the roles claim's JSON type and value **count**, the ID
token size in bytes, yes/no answers and, in role-list mode, the **configured** roles that
matched. It never prints tokens, the secret, other claim values or identity, and
every failure is one fixed line with a distinct exit code (3 TLS, 5 callback, 6 token, 7 ID
token, 8 claims). The ID token signature is **not** verified; this is a diagnostic only.

Claim names are flags with the chart defaults: `--email-claim` (`email`), `--username-claim`
(`name`), `--group-claim` (`groups`) and `--roles-claim` (`roles`). When a configured claim is
absent, the script names the closest claim name the token carries (a rule form may emit its rule
names or URIs as claim types). It always ends with the `OPENWEBUI_OIDC_*` lines for
`airgap.env` that match what it found, with any value holding a backslash single-quoted.

```sh
python3 -I scripts/openwebui/adfs_claims_check.py \
  --issuer https://<adfs-host>/adfs --client-id <client-id> \
  --client-secret-file <file> --ca-bundle <ca.pem> \
  --roles-claim roles --email-claim upn --username-claim name \
  --baseline-role signed-in
```

Flags also read `ADFS_ISSUER`, `ADFS_CLIENT_ID`, `ADFS_CLIENT_SECRET_FILE`, `ADFS_CA_BUNDLE`,
`ADFS_ROLES_CLAIM`, `ADFS_GROUP_CLAIM`, `ADFS_EMAIL_CLAIM`, `ADFS_USERNAME_CLAIM`,
`ADFS_BASELINE_ROLE`, `ADFS_ALLOWED_ROLES`, `ADFS_ADMIN_ROLES`,
`ADFS_SCOPES` (default `openid profile email allatclaims`) and `ADFS_CALLBACK_PORT` (8400).
The secret is never an argument: without `--client-secret-file` it is prompted for. The
script prints an authorize URL; open it in a browser that can reach AD FS and the bastion's
loopback (for example through an SSH port forward of the callback port), sign in, and the
script listens once on `http://127.0.0.1:<port>/callback` (180 s). Run it with a test account
in a role group (expect exit 0), then with `--expect-baseline-only` and an account in no role
group (the baseline must be the only value). `--decode-only <file>` analyses a saved ID token
offline with the same output rules.

**Role-list mode** ([LDAP-attribute-only rule form](#adfs-ldap-only-rule-form)). Pass the role
lists as configured; they are checked like validate and the chart check them:

```sh
python3 -I scripts/openwebui/adfs_claims_check.py --paste \
  --redirect-uri https://<openwebui-host>/oauth/oidc/callback \
  --issuer https://<adfs-host>/adfs --client-id <client-id> \
  --client-secret-file <file> --ca-bundle <ca.pem> \
  --email-claim upn --username-claim name --group-claim groups --roles-claim groups \
  --allowed-roles 'DOMAIN\docs-users' --admin-roles 'DOMAIN\docs-admins'
```

It reports whether the claim is a list or a string (split on `,`), which configured roles
matched, and the predicted Open WebUI v0.12.3 outcome: `admin`, `user` or refused (a redirect
to `/auth?error=...` with no session cookie). Run it
with a docs-group account (expect exit 0), then with `--expect-refused` and an account in
neither docs group: it passes only if the claim is present and the prediction is a refusal.
It fails when a configured role differs from a claim value only by letter case, a domain
prefix or a removed backslash, or when the configured names are bare and the token's are
domain-qualified (or the reverse); it names only the configured role. It warns on bare
configured names, and when a bare name occurs in the token under more than one domain prefix
(the count only). It warns when the ID token is above 8000 bytes (see "Token size" above).

**Paste mode (the default recommendation when the localhost redirect is not registered).**
It needs only the real redirect URI and works before Open WebUI exists:

```sh
python3 -I scripts/openwebui/adfs_claims_check.py --paste \
  --redirect-uri https://<openwebui-host>/oauth/oidc/callback  # plus the flags above
```

It prints the authorize URL and starts no listener. Sign in; AD FS then redirects to the
not-yet-deployed host and the router shows an error page. That is expected: copy the full
address from the address bar (it holds `?code=...&state=...`) and paste it at the hidden
prompt. Paste immediately: the code expires in minutes, and the whole run is bounded by
`--total-timeout` (300 s). The script requires the scheme, host and path to equal
`--redirect-uri`, checks the state, exchanges with the same URI, and never echoes the pasted
URL or code.

**Listener mode.** Without `--paste` the script sends `http://localhost:8400/callback`.
Register that second redirect URI on the **test** client only, next to
`https://<host>/oauth/oidc/callback`, and remove it when the check is done; the production
client keeps only the HTTPS callback.
Tests: `tests/test_adfs_claims_check.py` (a fake TLS AD FS, canary leak assertions).

<a id="workspace-isolation-proof"></a>
## Workspace isolation proof (bastion, ADR-0009 §2)

`scripts/devkit/isolation_proof.py` proves the F9 isolation of the production and CI
namespaces. It is standard library only, runs on the bastion as the operator (namespace
admin in both namespaces) and reaches the cluster only through `oc` (`--kubectl` picks
another client). Roles, ServiceAccounts, allowed Secrets and egress classes come from
`deploy/workspace/contract.json`; site values come from a parameter file (`KEY=VALUE`
lines, `--param` overrides): the two templates' parameters plus the runner configuration's
`PULL_SECRET` and `REGISTRY_PATH`, and `RUNNER_TOKEN_SECRET`. Probe pods run
`DEVKIT_REPOSITORY@DEVKIT_DIGEST`.

```sh
python3 -I scripts/devkit/isolation_proof.py \
  --contract deploy/workspace/contract.json --param-file <workspace.params> \
  --runner-config <gate config.toml> --runner-config <wip config.toml> \
  --probe --output <record.json>
```

The JSON record goes with the dated `gap/records/` entry for the proof.

It records:

- each F9 ServiceAccount's `can-i --list` in both namespaces against a fixed, versioned
  baseline in the script (`BASELINE_VERSION`: the Kubernetes and OpenShift self-review,
  discovery and default `system:authenticated` rules). Any other rule is a violation,
  whichever binding or group granted it; a site whose defaults differ fails closed until
  a reviewed PR changes the baseline;
- a negative matrix asked outright of each F9 ServiceAccount in both namespaces: secret
  read and write, configmap writes, pod create, patch, update, delete, exec, attach,
  port-forward, proxy and ephemeral containers, service proxy, workload creates
  (deployments, statefulsets, jobs, cronjobs, replica and daemon sets, replication
  controllers, DeploymentConfigs), NetworkPolicy writes, `runners.apps.gitlab.com`, token
  requests, impersonation, and Role and RoleBinding writes. The operator's own
  `get secrets` is the positive control;
- every pod's class, and the workspace and manager pods' Secret references, mounted and
  image-pull checked separately: an injected `<serviceaccount>-dockercfg-<suffix>` only as
  the pod's own pull Secret, and GitLab Runner's own labels and per-job pull Secrets
  (`runner-<token>-project-<id>-concurrent-<n>...`, same prefix as the pod, image pull
  only) on runner job pods only; the manager label selector must match no workspace pod
  and no runner `pod_labels`;
- the runner configuration against an allow-list: every key of the file, of each
  `[[runners]]` entry and of `[runners.kubernetes]` (with `pod_security_context`) must be
  listed and hold an allowed value, so volumes, `pod_spec` patches, annotations, node
  selectors, `*_overwrite_*` and any unknown key are refused;
- the egress policies: in the CI namespace exactly the contract's policies, each equal to
  its rendered form, and nothing else; in the production namespace every policy that
  selects the bastion (its contract labels or a live bastion pod's) must be one of its
  contract policies;
- on every egress rule that selects a workspace or manager pod: no empty `to`, no
  `0.0.0.0/0` or `::/0`, no empty `namespaceSelector`, and no ipBlock over the pod or
  Service network. The networks come from `network.config.openshift.io/cluster`; when the
  operator cannot read it, pass them with `--cluster-network CIDR`, or the check stays
  `unchecked` and the result inconclusive;
- the agent ingress policies;
- with `--probe`, TCP and DNS outcomes from probe pods carrying the labels of a live pod
  of each role (for runner jobs without a live job: the role label, the runner
  configuration's `pod_labels` and GitLab Runner's default labels), minus the keys a
  workload controller owns (`pod-template-hash`, `controller-revision-hash`,
  `statefulset.kubernetes.io/pod-name`, `apps.kubernetes.io/pod-index`, `job-name`,
  `controller-uid`, `batch.kubernetes.io/*`). Each probe pod's controller reference
  (`blockOwnerDeletion: false`) is the run's `mfr-proof-owner-<run>` ConfigMap in its
  namespace, so no ReplicaSet, StatefulSet or Job adopts it and then deletes it or the
  real pod; the operator therefore needs configmap create and delete in both namespaces.
  The run is marked by an annotation, never a label. A probe
  pod in the production namespace that no policy selects is the negative control: a
  blocked result counts only for a target the control reached. A CIDR wider than one
  address is not probed; give such a class a `--probe-host CLASS=HOST`.

The record holds verbs, resources, object names and outcomes, never a Secret value, token
or env value.

Exit 0 means proven, 1 a violation, 2 a usage or environment error, an inconclusive
result, or a run without `--probe` (verdict `static-only`, never the full proof). Run one
proof at a time: each run first deletes probe objects (`mfr-proof-*` pods and
`mfr-proof-owner-*` ConfigMaps with the annotation `isolation-proof.mainframe-rag.io/run`)
that an earlier run left, and removes its own on every exit, SIGTERM and SIGHUP included;
only a SIGKILL leaves them for the next run, and deleting a leftover owner ConfigMap also
garbage-collects its probe pods.
Tests: `tests/test_isolation_proof.py` (a stub `oc`, canary leak assertions).
