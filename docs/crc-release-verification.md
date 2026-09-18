# Local OpenShift release verification

Owner: this file. Deployment commands and signing contracts remain in
[deploy.md](deploy.md) and [install_and_ops.md](install_and_ops.md#4-standard-deployment-architecture-air-gap-production--local-cluster-testing).

Every published-main release must pass this manual CRC gate before its bundle
is transferred into production. A missing, skipped, or failed required check
blocks transfer. The pipeline's `OPERATIONAL & ACCEPTED` banner alone does not
pass this gate. Copy [the record template](crc-release-record.md) outside Git
for each candidate; initialize every result as `NOT RUN`.

**Implementation status:** candidate `89e10d3926b2e7f6a2e6ad0c1450c68c60e1b5f8`
passed its required CI lanes and a 32-minute fully live CRC workload, but the
12GiB cold start failed the 2GiB Windows headroom gate (1.62GiB after successful
streams). At 10752MiB, the required one-worker ingest remained unschedulable:
existing requests were 10396Mi against 10233200Ki allocatable. Simultaneous
operation is therefore unreliable under the agreed constraints. The [complementary
CRC/mock and disposable Kind/real-model lanes](local-release-fallback.md) both passed
against the same signed bundle, alongside all required CI checks. The local
verification mode is **complementary fallback**; transfer still needs operator
sign-off on the recorded bytes. Do not treat the workload result alone as a
fully live CRC release pass. No laptop runner or new deployment framework is
needed.

CRC runs actual single-node OpenShift. It has no supported in-place OpenShift
upgrade path and differs from production in storage, networking, and enabled
operators. Keep production compatibility and capacity acceptance separate.
[CRC limitations](https://crc.dev/docs/introducing/).

## Recorded candidate: 14 September 2026

The tested original tarball is
`qdrant-pdf-rag-89e10d3926b2e7f6a2e6ad0c1450c68c60e1b5f8.tar`, SHA256
`a715c531e7163de7316893ce63921345b9669ad718abcc295748e742f97febda`.
The protected release record retains command logs, UTC boundaries, configuration
hashes, model revisions, archive/registry image reconciliation and observations.
No credentials, PDFs, snapshots or browser captures belong in this document.

| Check | Measured result |
|---|---|
| Published bundle and required CI | PASS: [factory, bootstrap and three Kind lanes](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/34786949435); hosted OpenShift jobs skipped and excluded |
| Fully live mixed workload | PASS for this workload only: 1933.948 seconds, 44 HTTP requests, four fresh one-worker ingests, no request failures or unexpected restarts; 129 samples |
| Workload headroom minima | Windows 2.829GiB, CRC 3.843GiB, VRAM 627MiB; no WSL swap-out or Windows page-out during this window; about 68MiB WSL swap-in |
| 12288MiB CRC formal cold start | FAIL: functional checks and all 24 operators passed, but Windows headroom settled below 2GiB; final 1.621GiB |
| 10752MiB CRC attempt | FAIL: ingest Pending/Insufficient memory; node requests 10396Mi versus 10233200Ki allocatable |
| CRC complementary lane | PASS: real OpenShift/LiteLLM/PostgreSQL with mock computation; two full pipelines, browser OAuth and streams, TLS/auth, SCC, egress, snapshot/PVC/trace recovery, eight separate synthetic points |
| Real-model Kind complementary lane | PASS: two full pipelines, 11 successful application requests in 471.26 seconds, cited follow-ups/all streams, agent/ingest TLS controls, snapshot/PVC/prior-trace recovery, unchanged eight point IDs and credential fingerprints |

The unsuccessful first cold start stopped the 12GiB fit qualification; a second
cold start was not counted as passed or required to repeat an already failed
configuration. The smaller supported allocation then failed scheduling. This is
an observed reliability failure under the agreed constraints, not a proof that
32GiB can never host such a stack.

Setup archive/registry work caused additional memory pressure and historical
swap usage outside the formal workload window. Record those separately; do not
claim zero paging across the entire session. Jaeger's auxiliary query-service
self-telemetry warned about localhost:4317 while application OTLP HTTP on 4318,
trace queries and persistent trace recovery passed. Keep that observability
limitation explicit for the site owner.

The Kind application window had 31 observations: minimum Windows headroom
12.434GiB and free VRAM 819MiB, no container OOM/restarts or Windows page-out;
WSL recorded about 37.6MiB swap-in and 72.8MiB swap-out. The real-model CRC and Kind rehearsals each accepted a
3994-token embedding input with dimension 1024. An additional gateway diagnostic
run concurrently with chat hit its probe timeout; the unchanged idle probe then
passed. Retain both logs and run single-sequence readiness probes before the
interactive workload.

Production transfer still uses operator sign-off on these exact bytes. Production
OpenShift/storage/identity/network/capacity compatibility and actual platform
model acceptance remain site-specific; no production deployment or transfer is
claimed by the local results.

## 1. Credentials and host preflight

1. Create or sign in with a [Red Hat account](https://developers.redhat.com/register).
   Verify the email and complete the required account/terms prompts.
2. Open the [OpenShift Local download page](https://console.redhat.com/openshift/create/local),
   choose **Download pull secret**, and save the JSON outside Git, for example
   `C:\Users\<user>\.crc-secrets\pull-secret.json`. Restrict the folder to the
   Windows account that runs CRC; do not put it in a shared or synced folder.
   Use its actual filename in `--pull-secret-file`; Windows may append `.txt`
   while hiding the extension. CRC can read valid JSON with that extension.
3. In WSL, authenticate the connected loader and prove access to the exact
   OAuth image. Skopeo prompts for credentials; never put passwords in argv:

   ```sh
   umask 077
   mkdir -p "$HOME/.config/containers"
   skopeo login --authfile "$HOME/.config/containers/redhat-auth.json" registry.redhat.io
   skopeo inspect --authfile "$HOME/.config/containers/redhat-auth.json" \
     --no-tags --format '{{.Digest}}' \
     docker://registry.redhat.io/openshift4/ose-oauth-proxy:v4.14
   ```

   A login without successful image inspection is insufficient. For an account
   using organizational SSO, obtain registry service-account credentials through
   the organization's registry administrator if password login fails.
   [Red Hat registry authentication](https://access.redhat.com/articles/RegistryAuthentication).
   If login immediately reports `reading username: EOF`, inspect `command -v
   skopeo`: a Docker wrapper must forward stdin (`docker run -i`) and allocate
   a terminal (`-it`) for interactive login. Use `-i` without `-t` for piped
   `--password-stdin`; the auth-file directory must also be mounted persistently.
4. In Windows PowerShell, inspect resources before changing the host:

   ```powershell
   Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,FreePhysicalMemory
   Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory,NumberOfLogicalProcessors,HypervisorPresent
   Get-Volume -DriveLetter C | Select-Object Size,SizeRemaining
   ```

   Require a supported Windows edition, virtualization, administrator access for
   installation/setup, and sufficient **available Windows memory** for the selected 12 GiB or 10.5 GiB CRC allocation
   plus at least 2 GiB host headroom while WSL serves both models and the gateway. A WSL
   `free` result is not Windows free RAM. Reserve disk for the 50 GiB VM,
   installer/cache, bundle extraction, registry, and existing WSL virtual disk
   growth. Do not assume sparse virtual disks reserve their maximum size.
   [CRC Windows requirements](https://crc.dev/docs/installing/).
5. Inventory the existing Kind node containers, Docker mounts, PVCs, collections,
   aliases, and backup locations. Confirm snapshot checksums and successful
   isolated restore evidence (counts, vector configuration, and representative
   retrieval). Preserve all existing volumes and the full corpus.
6. Once the credentials and backup checks pass, stop only the inventoried Kind
   node containers with `docker stop <node-name>` in WSL. Keep vLLM, LiteLLM,
   and its database running. Never use `kind delete cluster`, volume prune,
   or an uncoordinated `wsl --shutdown` for this transition. Repeat Windows memory preflight;
   insufficient memory stops CRC startup. Record exact container names for
   recovery.

## 1.1 Bounded simultaneous-fit attempt and complementary verification

On the 32 GiB Windows/WSL host, try the complete live configuration before
choosing complementary verification. Keep current Gemma reasoning and Qwen
embedding weights/revisions, model IDs, 1024-dimensional vectors, 4096-token
windows, and reranking disabled. Use opt-in `BUDGET_PROFILE=LOCAL_CRC_32GB`
for both existing model launch targets; reasoning starts first. The profile
requests eager execution, one sequence per model, text-only reasoning with
zero multimodal processor cache, and explicit embedding prefix-cache and
chunked-prefill disables. Initial GPU limits are 0.54/0.43. Record actual
startup flags and RAM/VRAM: limits and Budget estimates do not establish fit.

Prepare `%UserProfile%\.wslconfig` with the following settings, preserving a
backup and any unrelated existing options. Apply during a controlled WSL
restart with the development environment saved; a ceiling is not a reservation.

```ini
[wsl2]
memory=12GB
processors=8
swap=2GB
pageReporting=true
[experimental]
autoMemoryReclaim=gradual
```

Keep CRC at 8 CPUs / 50 GiB disk. With CRC stopped, start each model, prove its
health through LiteLLM, and measure its settled footprint. Boot CRC at 12288
MiB only when Windows has at least 14 GiB available; try 10752 MiB if needed,
requiring 12.5 GiB available first. Stop/restart CRC for memory changes. Never
reduce below the documented minimum. [CRC configuration](https://crc.dev/docs/configuring/).

Require two cold starts and a 30-minute mixed synthetic ingest/query/chat run:
no OOM, unexpected restart, failed request, sustained paging, or degraded
operator; at least 2 GiB steady Windows headroom, 1 GiB available inside CRC,
and 512 MiB free VRAM. Keep existing request deadlines. Include long embedding
inputs and representative RAG prompts. Missing measurements remain NOT RUN.

Before loading another release, inventory old archives and their provenance.
Retain the current and last successful candidates. Remove only artifacts
confirmed reproducible; preserve corpus, snapshots, credentials, CRC instance
files, and the original Kind volumes. If deletion does not return Windows
space, arrange offline VHD compaction during the controlled restart; never
compact a mounted VHD.

If neither supported allocation passes, record simultaneous operation as
**unreliable under the agreed constraints**, with evidence. Do not claim
mathematical impossibility or keep lowering limits. Require all these lanes
against one original signed-bundle checksum:

| Lane | Required real components and coverage |
|---|---|
| Windows CRC, GPU models stopped | OpenShift, product images, registry/TLS, Qdrant, ingest, two agents, OAuth, LiteLLM/PostgreSQL, Jaeger; SCC, Service CA, Route, volumes and network enforcement |
| New disposable local Kind, CRC stopped | Same product images and pipeline, both real GPU models, authenticated TLS gateway, Qdrant, ingest, Jaeger; citations, follow-up chat and all streams; private access without the OpenShift Route |
| Required GitHub jobs | Fresh bootstrap of the published bundle, real application containers, registry, gateway/database and Kubernetes; deterministic computation and failure contracts |

In mock lanes replace only computation behind LiteLLM, using the existing mock
with explicit model IDs and 1024 dimensions. Keep real and mock collections
separate. The disposable Kind cluster must not reuse or delete the preserved
Kind cluster. Every lane must pass; skipped lab jobs are not OpenShift evidence.
The combined OpenShift/real-model test remains an explicit coverage gap.

GitHub's standard public Linux runners provide 4 CPUs/16 GB each; split lanes
across runners and record observed capacity. CRC is not a hosted-runner gate:
nested virtualization is unsupported. [Runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[virtualization limits](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners).

Extract repeatable CRC steps into a shared command only after this manual
procedure passes. Keep the existing deployment framework and avoid a laptop
runner. A changed image or configuration requires a new candidate and new
acceptance; never edit a signed bundle or repack separately per lane.

## 2. Install the pinned Windows CRC release

Initial version selection: **CRC 2.63.0, bundled OpenShift 4.22.7**, from the
[versioned release](https://github.com/crc-org/crc/releases/tag/v2.63.0).
This is a local verification pin, not a production-version decision.
Download its Windows installer from the release's Red Hat download link;
retain the installer, published checksum, and source URL outside Git. Verify
the downloaded bytes against the published checksum before running the guided
installer. If a checksum cannot be obtained, leave installation blocked.
Install on the local Windows C: drive, not inside WSL or on a network share.

The verified Windows archive is `crc-windows-installer.zip`, SHA256
`8556cca5d30f76190c4331e93b001d59a4edede2dd3e2fa65ecb3df8c0efd739`, from
[the release checksum file](https://developers.redhat.com/content-gateway/file/pub/cgw/crc/2.63.0/sha256sum.txt).
Its `crc-windows-amd64.msi` has a valid Windows Authenticode signature. Installer
exit code `3010` means installation succeeded and Windows must be rebooted;
do not start CRC until that reboot completes and resource preflight passes.

After the installer completes (and any required reboot), open a fresh Windows
PowerShell as the regular account that will own CRC. Use elevation only when
setup requests it. Configure before creating the instance:

```powershell
crc version
crc config set preset openshift
crc config set cpus 8
crc config set memory 12288
crc config set disk-size 50
crc config set consent-telemetry no
crc config set host-network-access true
crc setup
crc start --pull-secret-file "$env:USERPROFILE\.crc-secrets\pull-secret.json"
crc status
crc oc-env | Invoke-Expression
```

Check each command's exit status before continuing. Do not bypass CRC startup
checks. Record the actual `crc version`, bundled OpenShift version, installer
checksum, and configuration. Memory is specified in MiB.
[CRC configuration](https://crc.dev/docs/configuring/).

Log in using CRC's locally displayed cluster credentials; do not capture
passwords or tokens in the release record. Confirm one Ready node and record
`oc get clusterversion` and cluster-operator conditions. Account explicitly
for CRC's disabled operators instead of claiming full production parity.

## 3. WSL gateway and cluster connectivity

For the concrete host, TLS registry, Windows-client, Secret and generator commands,
follow [local-crc-environment.md](local-crc-environment.md).

Keep model serving in WSL behind the existing authenticated LiteLLM gateway
([local-stack ownership](live-stack.md#full-local-simulation)).
CRC host access exposes Windows services through `host.crc.testing`; it does
not establish access to an arbitrary WSL listener. First prove a Windows
client can reach the gateway, then prove the same connection from a CRC pod.
[CRC host access](https://crc.dev/docs/networking/).

Use the Windows-to-WSL connection appropriate to the actual WSL networking
mode. If forwarding is needed, forward only the gateway port, restrict the
Windows/Hyper-V firewall to the CRC path, and record the rule and removal
command. WSL NAT addresses can change after restart. Do not forward backend
model ports or assume Windows localhost forwarding works from CRC.
[WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking).

Use a TLS gateway endpoint with a certificate valid for the hostname used by
the pods. Preserve Bearer authentication and certificate verification. If
using a private CA, deliver its public trust bundle to both agent and ingest
HTTP clients using optional `GATEWAY_CA_CONFIGMAP` (key `ca-bundle.crt`,
mounted read-only, `SSL_CERT_FILE` set for agent and ingest). Supply a complete
bundle preserving every required root; trusting the CA on
Windows or the nodes alone does not make application clients trust it. Verify the reviewed candidate supports this input, and prove correct CA, wrong
CA, and hostname-mismatch behavior from the application pods. Never use `verify=False`,
`curl -k`, or an HTTP downgrade to claim a pass.

Use a dedicated WSL kubeconfig for CRC, leaving the Kind context intact.
Verify the CRC API's hostname and certificate. If the API is Windows-loopback
only, use the Windows-native `oc`/Helm adapters in
[the local guide](local-crc-environment.md#6-windows-api-access-from-the-wsl-pipeline)
with a dedicated Windows kubeconfig; a Linux client is not required. Use `KC=oc` for the existing scripts and
verify `oc whoami --show-server` before each deployment. Do not work around
WSL API/DNS failures by disabling TLS verification.

## 4. Authenticated local registry

Provision a separate local registry reachable by the WSL loader and CRC node;
preserve the Kind registry. A Distribution registry with persistent local
storage, TLS, and bcrypt `htpasswd` authentication supports the loader's
nested repository layout. Pin and record the registry image digest before
launch. Keep its certificate key, auth database, and data outside Git. Use
interactive `htpasswd -B` password entry and bind only the intended interface.
[Distribution deployment](https://distribution.github.io/distribution/about/deploying/).

Use one registry authority and optional prefix in `INTERNAL_REGISTRY` for
load, deploy, and ingest. Configure local DNS/forwarding so that the same
hostname resolves from the loader and the node; its TLS certificate must cover
that hostname. Keep these repository suffixes used by
[load.sh](../scripts/airgap/load.sh):

- `qdrant/qdrant:v1.19.0-unprivileged`
- `jaegertracing/jaeger:v2.20.0`
- `qdrant-pdf-rag-agent:<full-main-sha>`
- `qdrant-pdf-rag-ingest:<full-main-sha>`
- `openshift4/ose-oauth-proxy:v4.14`

Install the registry CA in the loader's container trust store and OpenShift's
`image.config.openshift.io/cluster.spec.additionalTrustedCA` ConfigMap in
`openshift-config`. Preserve existing CA entries; a registry port uses `..`
in the ConfigMap key, for example `registry.example.test..5443`. Confirm the
node has reconciled the trust change before testing pulls.
[OpenShift registry CA configuration](https://docs.redhat.com/en/documentation/openshift_container_platform/4.14/html/registry/configuring-registry-operator).

Authenticate Skopeo with an explicit local auth file and export
`REGISTRY_AUTH_FILE` to that path in the loader shell. Create the namespace's
`kubernetes.io/dockerconfigjson` pull Secret from that file, set `PULL_SECRET`
to its name, and confirm it reaches Qdrant, agent, Jaeger, ingest, and the
synthetic corpus generator. Use `INSECURE_REGISTRY=false`. Require an
unauthenticated `/v2/` request to be denied, an authenticated request to
succeed with TLS verification, and an actual uncached CRC node pull to pass.
Do not substitute the Kind HTTP registry or CRC's integrated registry without
first proving the required multi-component repository paths work.

## 5. Release prerequisites and local sizing

Verify an authenticated pull of the OAuth digest recorded in
[images.txt](../images.txt), using section 1. Future pin changes, including
resolving `sha256:PENDING` in an older checkout, require a **dedicated pin
change** through normal review and a new published-main bundle containing
the OAuth archive. Editing a downloaded bundle invalidates its signature;
do not bypass the pending-pin guard.

Use [the production overlays](../overlays/openshift/values.yaml) and existing
rehearsal override hooks. Keep local files outside the bootstrapped checkout.
Local values for a tiny generated corpus (never production defaults):

| Setting | CRC starting value |
|---|---|
| Qdrant Helm override | 1 replica; requests 200m CPU / 256Mi; limits 2 CPU / 2Gi |
| `QDRANT_STORAGE_SIZE` | `1Gi` for each data and snapshot PVC |
| `INGEST_WORK_SIZE` | `1Gi` |
| `INGEST_WORKERS` | `1` |
| Ingest resource patch | requests 500m CPU / 1Gi; limits 2 CPU / 2Gi |
| Corpus PVC | `1Gi`, generated PDFs only |
| Agent / Jaeger | Existing replica counts and resource settings |

The 256Mi Qdrant request was explicitly approved after the initial 512Mi request
left the 12GiB node 169Mi short in scheduler reservations despite actual free RAM.
The 2Gi limit, two agents, OAuth and other acceptance criteria stayed in place.

Use `QDRANT_EXTRA_VALUES` for Qdrant sizing and `INGEST_EXTRA_PATCH` for the
ingest Job's resources; the latter is a strategic merge patch for `Job/ingest`,
container `ingest`. Record the exact files and hashes. Select the actual CRC
storage class after inspecting its provisioner and RWO behavior; do not assume
it is named `standard`. CRC hostpath-backed storage is a local difference,
not proof of production block-storage parity. Watch actual VM disk usage,
including Jaeger's existing 10Gi claim, image layers, and snapshot duplication.

Local overrides change only sizing and environment coordinates. Keep
`AGENT_ROUTE=true`, Secret-backed gateway keys, and tracing enabled in CRC.
Fully live mode requires both current real models. Complementary mode follows
section 1.1 and records mock computation explicitly; never use hash mode or
claim that deterministic mock citations measure answer quality.

**SCC prerequisite:** inspect admission with the unmodified security policy.
Production Qdrant values remove the chart's fixed UID, GID and fsGroup;
[Jaeger](../deploy/kustomize/jaeger/deployment.yaml) also leaves identities to
admission. Verify the resulting project-assigned ranges. A denied pod or
unwritable volume blocks the run. Fix demonstrated incompatibilities in the
owning production configuration with `sh scripts/tools/run-task.sh qa:check` and `sh scripts/tools/run-task.sh airgap:dryrun`,
then obtain and retest a new main bundle. Never grant `anyuid`, disable SCC,
or hide a security fix in a CRC-only values file. Validate every workload,
including the OAuth container and completed Jobs.

## 6. Fresh bundle rehearsal

1. Select a green, published-main SHA with all required image archives. Record
   its CI evidence. For deployment changes, require `sh scripts/tools/run-task.sh qa:check` and
   `sh scripts/tools/run-task.sh airgap:dryrun` to pass; documentation edits require cited-path checks
   under [the change-class ladder](live-stack.md#0-which-rungs-you-owe-no-more-no-less).
2. Obtain the signed bundle, its checksum, and the release signing public key
   from the approved out-of-band source. Require a custodied release key;
   an ephemeral rehearsal key cannot authorize production transfer.
3. In a fresh directory outside the development checkout, verify the tarball
   checksum **before** extraction, then bootstrap with
   `SNEAKERNET_TRUSTED_PUB` pointing to that public key. Follow
   [the existing handoff](install_and_ops.md#42-transfer--automated-bootstrap).
   Confirm bootstrap verifies both signature and member checksums. Work only
   from this fresh clone; verify its HEAD equals the manifest SHA.
4. Configure a local `AIRGAP_ENV` file with the registry, namespace, storage,
   sizing hooks, model IDs/dimension/operator revision, HTTPS gateway URLs, and Secret names.
   Populate the gateway and OAuth-cookie Secrets from local protected files.
   Do not put key values into the env file. Keep the exact bundle unmodified.
5. Run `sh scripts/tools/run-task.sh airgap:validate`, then `sh scripts/tools/run-task.sh airgap:load`. Verify archive identity
   against the signed manifest, then reconcile registry compression/config and
   runtime digests using [the image-identity procedure](deploy.md#image-identity-across-archive-and-registry-formats).
6. Before product deployment, run a temporary pod using the loaded agent
   image, the intended gateway env/Secret references and TLS trust, under
   `restricted-v2`. Run `python3 /app/scripts/probe_gateway.py --require-reasoning --stream` in
   it. Require all configured legs, dimension, authentication, and streaming
   checks to pass. Remove only this temporary pod after recording results.
7. Create the synthetic corpus PVC and generator Job using the
   [existing generator recipe](local-crc-environment.md#72-configure-and-deploy-the-same-candidate).
   Adapt only registry, storage class, namespace, pull Secret, and SCC-safe
   security context. Keep the original generated content and the loaded
   release ingest image. Require generator completion; set `CORPUS_PVC`.
8. Run `sh scripts/tools/run-task.sh airgap:pipeline` without skip flags. This repeats load, deploys
   the production overlays including the real OAuth sidecar, probes the
   configured gateway from the agent pod, ingests, and checks smoke/tracing.
   Save stage exit statuses. Empty search or skipped ingest/tracing is a
   failed release check even if the script exits successfully.

## 7. Live acceptance

Record evidence before replacing pods so their original admission and image
IDs remain attributable. Use explicit pod/container names for `oc exec`.

| Check | Required evidence |
|---|---|
| SCC and identities | Each product/generator pod's `openshift.io/scc` annotation is exactly `restricted-v2`; namespace UID/group ranges, admitted security contexts, actual process IDs, and no `anyuid` grants. Capture completed ingest/generator pods too. |
| Images | Requested image refs and each running container's `imageID`, including OAuth, match loaded release artifacts. Account for manifest-list/platform and archive/registry compression differences; verify image configs/rootfs identity and actual registry digests. |
| Storage writes | Bound PVCs, actual provisioner/access modes, successful ingest scratch/data writes, Qdrant snapshot creation, and Jaeger span persistence. |
| Pod replacement | Replace one Qdrant pod and one agent pod, recording old/new UIDs. PVC identity, exact point count, representative hit IDs/citations, and health survive. Restart Jaeger and verify an earlier trace remains queryable. |
| Repeat deployment | Repeat `sh scripts/tools/run-task.sh airgap:pipeline` on the same bundle/env; require rollout, new ingest Job completion, unchanged synthetic point count/IDs, healthy search, and tracing. Detect API-key rotation or immutable-resource failures. |
| Snapshot recovery | Snapshot only the CRC synthetic collection, export and checksum it outside Git, then restore into an isolated empty collection/instance using the same Qdrant version and vector config. Verify exact count, sampled payload/point IDs, and equivalent queries; retain the original collection. Record snapshot and restore evidence, never commit snapshot bytes. |
| OAuth Route | A fresh unauthenticated `GET /ui` may show the provider chooser (403); `/oauth/start` redirects to OpenShift OAuth. Browser login returns to `/ui`; a fresh session still requires authentication. No accidental direct public API/Jaeger/Qdrant Route. |
| Certificates | Browser/CLI verifies Route hostname and ingress chain without bypass. Route is `reencrypt`, targets OAuth 8443, and trusts the Service CA; serving Secret is populated and matches its service identity. Existing Routes need inspection because deploy does not replace them. |
| Cited answer | Ask `What does IEA500I mean?`; verify the answer and non-inferred citation against the generated PDF, with no error/degraded response. A transport-only success is insufficient. |
| Follow-up | Send a contextual follow-up with browser history and verify it completes coherently with a valid synthetic citation. Record current condensation setting; do not flip its default. |
| Streams | `/v1/answer`: exactly one successful terminal `final`, no `error`. `/v1/chat` and `/v1/chat/completions`: successful finish chunk plus `[DONE]`, no error frame. `/ui`: terminal completion and cleared busy state. A connection closing alone is insufficient. |
| Tracing | Search and answer/chat traces appear in Jaeger with retrieval and reasoning children where applicable; browser assets load locally. |

Inspect actual admission, for example (substitute the dedicated namespace):

```sh
oc -n "$CRC_NAMESPACE" get pods -o custom-columns='NAME:.metadata.name,SCC:.metadata.annotations.openshift\.io/scc,UID:.spec.securityContext.runAsUser,GROUP:.spec.securityContext.fsGroup'
oc get namespace "$CRC_NAMESPACE" -o jsonpath='{.metadata.annotations.openshift\.io/sa\.scc\.uid-range}{"\n"}{.metadata.annotations.openshift\.io/sa\.scc\.supplemental-groups}{"\n"}'
oc -n "$CRC_NAMESPACE" get pvc
```

Pod-level fields may be empty when a container has its own context; inspect
both and run `id` where the image provides it. Save only sanitized resource
metadata. Never archive Secret YAML, login commands with tokens, or browser
cookies/HAR credentials.

## 8. Runtime egress isolation

Apply a namespace-wide egress default-deny NetworkPolicy to all application
pods, including ingest and OAuth. Add narrowly scoped allow rules for the
actual DNS service (UDP/TCP), Qdrant HTTP/gRPC and peer traffic, Jaeger OTLP
and query traffic, OpenShift OAuth/API dependencies, and the gateway's
observed destination/port. Inspect the active CNI, service translation, and
host-network behavior before choosing selectors or IP blocks. Avoid broad
`0.0.0.0/0` exceptions. Keep local policies and a sanitized rendered copy with
the record. [OpenShift network policy](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/network_security/network-policy).

Before enforcement, prove a public TCP destination is reachable from each
distinct application policy context; record the resolved address. After
enforcement, require a bounded direct-IP connection to that same address to
fail (not just DNS), while internal DNS and required gateway calls succeed.
An unresolved hostname, invalid certificate, missing probe utility, or public
server already down is not evidence of egress blocking. Account for IPv6 if
enabled. Repeat ingest, gateway streaming probe, retrieval, tracing, OAuth
login, cited answers, follow-up, and stream termination under the policy.

Node image pulls do not originate in application pods. Test them separately:
allow the trusted local registry, enforce the intended node/upstream pull
restriction, and prove an uncached public image pull fails while an uncached
local pull succeeds. Record the image digests, isolation mechanism, events,
and any CRC control-plane exceptions. A cached image proves neither result.
Missing node-isolation evidence blocks this gate; it must not be inferred
from NetworkPolicy success.

## 9. Recovery and promotion

For routine recovery, use Windows `crc stop`, retain its VM/data and local
registry storage, then restart the previously recorded Kind containers from
WSL. Check the original Kind context, PVCs, counts, aliases, and gateway
readiness. Do not delete or recreate Kind. If changing CRC versions, export
needed evidence/synthetic snapshots first and follow the version's recreation
procedure; repeat the full gate after any recreation or configuration fix.

Promotion requires every required row in [the release record](crc-release-record.md)
to be `PASS`, evidence attributable to this bundle, and named operator sign-off.
`NOT RUN`, `BLOCKED`, `FAIL`, missing artifacts, or stale results prohibit
transfer. This is an operator-enforced gate until the proven manual procedure
is automated; existing pack/load scripts do not read this record.

Transfer **the identical tested tarball and checksum**, plus its verification
record through the approved channel. Do not rebuild, repack, edit, or substitute
an image. Recheck the checksum on arrival, then use the existing bootstrap,
validation, deployment pipeline, and production acceptance commands. A bundle
or security/configuration fix requires a new candidate and a fresh gate.

Before claiming production compatibility, obtain the production OpenShift
version, storage driver/topology, SCC/RBAC policy, identity integration,
registry trust, and network restrictions. Record each difference and its
acceptance owner. CRC success establishes neither production capacity nor
multi-node resilience, enterprise identity behavior, or general answer quality.
