# Rebuild the local Helm stack with the preserved real corpus

Use the exact published release with real reasoning and embedding models behind
the authenticated TLS gateway. Preserve the original PDFs, verified snapshots,
private configuration, credentials and persistent volumes. A successful historical
restore is not acceptance of a newer application or representation contract.
See the [dated Helm audit](deployment-audit-2026-09-20.md) for current observations;
the [14 September record](crc-release-verification.md#recorded-candidate-14-september-2026)
remains historical evidence for its own candidate.

This is a single-node local operating profile, not production 6/3/2 acceptance.
Keep CRC and unrelated rehearsal clusters stopped while both real models run.
Never query a hash collection with real embeddings. Reranking stays explicitly
disabled for the two-model 8 GiB GPU pack.

## 1. Establish the candidate, data and resource budget

Follow [the Kind setup](local-release-fallback.md#3-disposable-kind-with-both-real-models)
for checksum-pinned tools, the trusted signed release, registry/node trust,
gateway DNS and Secret/CA references. Execute application operations from the
verified bundle checkout. Do not rebuild, relabel old images as the new SHA,
or substitute current development files inside that checkout.

Record the full source SHA, tar checksum, trusted signing-key provenance,
archive digests and running imageIDs. Keep protected state outside Git. Use an
explicit kubeconfig and namespace for every operation; inspect the current
context before teardown. Record PVC UIDs, releases, resources and Secret backups
privately, plus collection counts, aliases, vector configuration and source hashes.
Do not print keys, manual text or vectors into shared evidence.

Verify the backup checksum and an isolated restore before destroying any only
copy. The preserved real corpus used by the September audit has 435,057 points
and 452 distinct original PDF hashes. Those counts describe that backup, not a
universal success threshold. Match **every** source hash before a full migration;
a smaller convenient corpus cannot qualify the restored collection.

Use the recorded immutable Gemma/Qwen revisions and `LOCAL_CRC_32GB` serving
budget from [the model runbook](local-crc-environment.md#3-pin-and-start-the-two-model-servers-sequentially).
Pass `SERVED_NAME` when `MODEL` is a local directory. Start reasoning, verify its
served ID and health, then start embedding. Restore the same gateway keys and
PostgreSQL volume. Measure Windows/WSL RAM, swap, GPU headroom and disk before
and during startup, ingest and application use. Do not lower context windows,
change models or relax readiness to make the run pass.

For the existing local corpus, the measured Qdrant override is:

```yaml
replicaCount: 1
podSecurityContext: {fsGroup: 3000}
resources:
  requests: {cpu: 200m, memory: 1Gi}
  limits: {cpu: "2", memory: 2Gi}
```

This fixed group is Kind-only; OpenShift keeps project-assigned identities.
Use explicit local `QDRANT_SHARD_NUMBER=1`, `QDRANT_REPLICATION_FACTOR=1`,
`QDRANT_WRITE_CONSISTENCY_FACTOR=1`, `AGENT_ROUTE=false`, two agent replicas,
real-model dimension 1024 and the verified immutable `EMBED_MODEL_REVISION`.
The recorded claims are 20Gi each for Qdrant data/snapshots and 10Gi for Jaeger.
Include old generation, staging, safety snapshots and optimizer scratch in disk
planning. Local-path claim capacity does not reserve or enforce physical disk
space. Production sizing and 6/3/2 policy are unchanged.

## 2. Recover host services and tear down workloads deliberately

After a Docker/WSL restart, inspect container bind sources before `docker start`.
A stale Docker Desktop bind mapping can report a file/directory mount mismatch
even when the original file exists. Preserve the stopped container's inspection
record and recreate only the affected service from its recorded image digest,
entrypoint, ports, bind mounts, environment, user, memory limit, read-only root,
tmpfs, capabilities, security options and restart policy. Retain the certificates,
registry data and credentials; never prune volumes or regenerate keys as a fix.

Reconnect the registry and TLS gateway to the Kind network as described in
[the TLS service setup](local-release-fallback.md#32-reuse-authenticated-tls-services-without-changing-host-ports).
Restart the TLS proxy after recreating the LiteLLM container: nginx can retain
the old upstream address even when Docker DNS already resolves the new one.
Container addresses may change. Refresh only the selected cluster's existing
`host.crc.testing` CoreDNS entry to the actual gateway address and wait for DNS
convergence. The node's registry DNS and the pod's gateway DNS are distinct
paths. Verify TLS/auth from both before attributing failures to model serving.

For an explicitly authorized workload rebuild, first record a successful search
and its Jaeger trace ID. Drain/stop agent and Jaeger Deployments, then uninstall
the selected Helm releases. Legacy unmanaged agent/Jaeger Deployment, Service
and ConfigMap resources need explicit removal by name. Inspect Helm manifests
before uninstalling: retain Jaeger Badger and Qdrant data/snapshot PVCs, and keep
corpus/scratch claims outside the application release. Do not delete the namespace
or cluster to accomplish workload teardown. Preserve unrelated resources.

A fresh cluster may instead recover a verified snapshot under its original
physical name using an isolated maintenance workload with the **writer** Secret
key, followed by the compatibility checks below. Snapshot and alias writes are
never performed using the serving agent's read-only key. Mount local recovery
files read-only beneath `/qdrant/snapshots` in Qdrant; its canonical-path policy
rejects files elsewhere. Require synchronous recovery completion, not merely
`wait=false` scheduling. Preserve the original snapshot and collection until the
replacement is verified.

## 3. Distinguish data recovery from serving compatibility

A current generation includes its data **and** its physical
`<generation>__completions` representation/completion/publication records.
Restore consistent records under their original physical identities; do not
invent, rename or copy a current manifest onto unattributed old vectors.
Aliases must resolve to the generation whose own contract is validated.
See [publication and migration](ingest.md#publication-contract).

The historical real-manual snapshot contains only data. Counts, dimension
matching and three similar fresh vectors do not supply the missing generation
contract. The current agent correctly reports `representation=legacy` and refuses
serving. The supported remedy is a complete real-source re-ingest under the
attested embedding revision. Keep the old physical collection and snapshot for
rollback; never manufacture completion markers or weaken the gate.

Prepare a read-only corpus PVC containing the complete hash-matched originals.
For Kind, a separately inventoried node directory and retained static corpus PV
are suitable; no private PDFs enter Git or application images. The ingest pod
must see every expected file and retain filename-stem identity. Keep shared
`ingest-work` scratch and a single authorized publisher. Set `INGEST_WORKERS`
explicitly in the protected operator inputs and select a local resource override
from measured peak usage with both models and Qdrant running. A container limit
does not reserve host RAM. The [dated audit](deployment-audit-2026-09-20.md)
records the four-worker recovery and failed six-worker memory trial; neither
historical one-worker sizing nor a brief healthy sample establishes full-run fit.

From the exact release checkout with protected `AIRGAP_ENV`, explicit kubeconfig
and Helm 4 on PATH:

```sh
sh scripts/tools/run-task.sh airgap:validate
sh scripts/tools/run-task.sh airgap:load
sh scripts/tools/run-task.sh airgap:deploy
```

On a legacy corpus, the application release can install successfully while the
bounded agent readiness wait fails. Record that nonzero deployment result. This
is not permission to disable probes or declare readiness; Qdrant must be ready
before proceeding with the explicit migration:

```sh
INGEST_ALIAS_PUBLISH=true INGEST_REINGEST=true INGEST_TIMEOUT=86400 \
  sh scripts/tools/run-task.sh airgap:ingest
```

These are deliberate migration inputs, not new defaults. The Job has a 24-hour
active deadline; choose a suitable operator wait within that boundary. Preserve
logs and publication state on failure, inspect the actual Job before retrying,
and resume through the same supported operation. Do not start overlapping writers.
Verify the candidate's checkpoint behavior before claiming that a forced retry
retains work: the historical published `cbcc74b` path redid every document, while
the recovery candidate in the dated audit verifies same-build document checkpoints.
Inventory counts alone do not establish that the actual target can skip work.
Alias publication retains the old generation and promotes only after full coverage
and representation checks. A failed or still-running Job is not acceptance.

After successful migration, perform the next ordinary operation without forcing
another re-embed:

```sh
INGEST_ALIAS_PUBLISH=true sh scripts/tools/run-task.sh airgap:ingest
sh scripts/tools/run-task.sh airgap:deploy
sh scripts/tools/run-task.sh airgap:smoke
```

Require all walked files complete, no failures, a committed physical contract,
correct alias, stable point membership on the ordinary repeat, and compatible
agent readiness. Changed chunk counts after a documented identity migration need
source/coverage explanation; historical vector counts alone are not the oracle.

### Explicit recovery of duplicate legacy staging points

A completed replacement can coexist with obsolete sourceless points inherited
from the old snapshot. Coverage must still refuse that mixture. Do not clear
publication state, mark the manifest committed, retire the whole document, or
turn off the coverage gate to get past it.

The candidate's `mainframe_rag.ingest.repair` module handles only this narrow
case. Stop writers and use the same writer credentials, configuration and shared
`/work/inventory.jsonl` as the recorded build. The publisher and run locks are
cooperative filesystem locks; they do not exclude an unrelated admin writing
through another progress mount. Maintenance requires one authorized writer.
Use an ingest image that actually contains this module and record its imageID;
older published images do not acquire the command by changing their tag.

For a local Kind maintenance Job, retain the rendered ingest Job's environment,
Secret references, volumes and security settings. The existing
`INGEST_EXTRA_PATCH` local rehearsal input can replace the `ingest` container's
command with `["python3", "-m", "mainframe_rag.ingest.repair"]` and its args with
the following **plan** arguments. Use a new directory on the retained work PVC:

```text
plan --progress /work/inventory.jsonl --directory /work/repair-<run-id> --max-points <reviewed-upper-bound>
```

Run it through `airgap:ingest` with the same recorded candidate/configuration.
A successful plan is read-only in Qdrant and prints a plan SHA256. Privately
inspect `plan.json`, `points.json`, the inventory and publication-state backups;
copy them off the work PVC and verify checksums. They contain corpus data and
must never enter Git. Replace the maintenance args with:

```text
apply --progress /work/inventory.jsonl --directory /work/repair-<run-id> --approve-plan <reviewed-sha256>
```

Each phase locks and rechecks the actual pending manifest, alias, inventory,
replacement completion and stored replacement content. Every exported point's
payload **and vectors** must also match its retained live copy. Apply refuses
changed inputs or newly discovered residue and deletes only the approved IDs
from the unserved staging collection. An interrupted apply can retry the same
plan: already deleted IDs are allowed, changed survivors are not. It neither
edits completion records nor publishes. A local process with the actual shared
progress mount can invoke the same operation through
`sh scripts/tools/run-task.sh local:repair-staging -- ...`.

Remove the maintenance command override before resuming the ordinary ingest
Job. Resume the same forced build if it is pending, then run the unforced
operation in section 3. Require unchanged live rollback content, full source
coverage, a committed replacement, alias cutover and stable membership on that
next ordinary operation. A repair receipt alone is not publication acceptance.

## 4. Verify and retain the complete live stack

Run the actual candidate's `probe_gateway.py --require-reasoning --require-tokenizer --stream` from
an application pod before interactive traffic. Require correct served IDs,
1024-dimensional real embeddings, TLS/auth, and explicit successful finish plus
`[DONE]`. The local gateway exposes authenticated `POST /tokenize` to the
reasoning backend and enforces the virtual key's model scope on that route.
If a prior tokenizer failure pinned an agent process to estimation, replace
that process after repairing the route; inspect `budget_verified` on real
requests. Do not overlap probes with single-sequence interactive reasoning.

For pronoun-only follow-ups such as "What is the purpose of that parameter?",
explicitly set `CHAT_CONDENSE_ENABLED=true` in this local profile's protected
operator inputs and redeploy. Helm maps it to
`models.reasoning.condenseEnabled`. The application/chart default remains false;
with it off, retrieval uses the latest question literally. Enabling it adds a
reasoning call and needs its own latency/quality acceptance; a fallback to the
raw question can still produce insufficient evidence.

Verify search, grounded answer and follow-up, streaming completion, long-input
refusal, liveness/readiness and the enabled console. Keep manual answers/browser
captures private; share counts and outcomes. Replace Qdrant/agent/Jaeger pods,
then verify collection/control state, next ordinary search/answer and the exact
**pre-replacement trace ID**. Stable PVC UIDs alone do not prove retained content.

```sh
kubectl -n "$KIND_NAMESPACE" port-forward --address 127.0.0.1 svc/rag-agent 8087:8080
# Separate terminal:
kubectl -n "$KIND_NAMESPACE" port-forward --address 127.0.0.1 svc/jaeger 16686:16686
```

With both port-forwards running, execute the strict check from the candidate
source checkout (requires the prepared development environment):

```sh
sh scripts/tools/run-task.sh local:check -- \
  --agent http://127.0.0.1:8087 --jaeger http://127.0.0.1:16686 \
  --query 'What should the LFAREA parameter be set to in IEASYSxx?' \
  --followup 'What is the purpose of that parameter? Please cite the manual.' \
  --report "$STATE/live-check-<run-id>.json"
```

`STATE` is an existing private directory outside Git. Choose a new report name
on each run. This is an actual model/corpus check, not a documentation check.
It exits nonzero on incomplete/uncited answers, insufficient-evidence follow-ups,
SSE errors or duplicate/missing final events, incorrect long-input refusal, or
missing fresh search trace. An HTTP 200 or an old trace is insufficient.
Keep the source SHA, chart revision, deployed imageIDs, gateway launcher SHA,
model revisions and private configuration inventory beside the report. Mixed
candidate components are explicit diagnostic evidence, not exact-release
acceptance. The check does not replace gateway/TLS, browser or persistence tests.

Open **http://localhost:8087/ui** and **http://localhost:16686**. On Windows/WSL,
check listeners on both hosts and verify these URLs from the Windows side;
a successful WSL curl does not prove the browser reaches the same listener.
The September audit used 8087 because an unrelated Windows `AgentService`
already occupied 8080. Leave unrelated services alone and select a free local
port; the Kubernetes service remains on 8080. Re-establish
port-forwards after pod or cluster replacement. Keep the pinned model/gateway
launcher sessions, release workspace, protected restart configuration and backups
available for ongoing use. Run the foreground model/gateway launchers and
port-forwards in retained terminal sessions (for example named tmux sessions),
with private logs under `STATE`, rather than relying on a temporary agent tool
session. Keep a protected restart file containing the original gateway keys,
PostgreSQL password/volume and explicit container names. Source it without shell
tracing before `local:gateway:up`; a missing file is a prerequisite failure, not
permission to mint replacement keys. Record each service's launcher command,
source SHA, image digest, model view/revision, port and session owner. Stop only
those recorded services and the selected cluster's workloads. After a restart,
repeat readiness, TLS DNS, strict acceptance and Windows listener checks. Preserve the old generation until explicitly retired.

Kind does not prove OpenShift SCC, OAuth/Route, Service CA or site network/storage
controls. CRC skipped for insufficient memory remains **NOT RUN**, as does any
unavailable internal GitLab/Quay/namespace qualification. Record the exact missing
environment and closure checks in the dated evidence; never promote from a local
search or the pipeline banner alone.

The `local:check` trap requires `insufficient_evidence`, no citations and an answer
that satisfies the shared answer abstention predicate. An uncited
`unverified_draft` is not a refusal. Console acceptance examines every data-bearing
SSE event, including unnamed (`message`) events, and permits only token events
followed by exactly one terminal final; comment heartbeats are allowed.
