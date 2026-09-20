# Helm live-deployment audit — 20 September 2026

Status: in progress. This record distinguishes current observations from the
historical 14 September CRC/Kind evidence. It is not production acceptance.

Scope: documentation sweep and teardown/redeployment of the complete local stack
using Helm, the preserved real corpus and real reasoning/embedding models.
Baseline: `cbcc74b0aa0c21a611f8542fb4135b3df32a7434` (merged Helm migration).
The user explicitly selected real corpus/models and authorized skipping CRC if
memory is insufficient. Existing corpus, snapshots, private configuration and
volumes must survive. Production settings and serving safety gates stay intact.

## Baseline and evidence

- Published-main bundle is available from [run 35523273373](https://github.com/yonatan895/qdrant-pdf-rag/actions/runs/35523273373).
- Preserved Kind Qdrant is green with 435,057 points in `real_manuals` and alias
  `mainframe_manuals`. The independent 4,312,394,240-byte backup matches its
  recorded SHA256 and has 1,024-dimensional dense vectors.
- Two rehearsal clusters were stopped without deleting their containers or
  volumes. The preserved real-corpus cluster was restarted for inventory.
- Windows available memory before the transition was 10,216,848 KiB, below the
  CRC 14 GiB prerequisite for a 12 GiB VM. CRC has not been started for this audit.
- Both pinned model revisions are cached; no model revision change is intended.

## Gap register

| Gap | Current evidence | Resolution / required proof |
|---|---|---|
| Recovery recipe uses serving credentials for snapshot restore and alias writes | `local-real-corpus.md` executes mutations inside the agent using its read-only key | Removed the unsafe recipe; current recovery uses supported full-source migration. New agent snapshot writes are checked independently; administrative recovery requires writer references |
| Historical snapshot lacks current representation/control collections | Preserved Qdrant lists only `real_manuals`; current serving validates the physical generation's completion contract | Determine supported migration/re-ingest requirements; never invent completion evidence or relax the serving gate |
| Current real-model stack is stopped | Model GPU usage is absent; gateway containers are absent and TLS front is stopped | Restore pinned model/gateway launchers, verify TLS, keys, dimensions and real streaming |
| Documentation mixes historical and current acceptance | CRC record refers to candidate `89e10d3`, while the current release uses Helm | Preserve dated evidence; link current results and explicit limits from current operating instructions |
| Task drops `SERVED_NAME` CLI input | Real reasoning launch served directory basename instead of gateway model ID | Forwarded for all three model tasks; exact CLI/environment/literal round-trip regression passed |
| Docker/WSL restart leaves stale bind mappings and gateway DNS | TLS containers refused mount startup; gateway DNS referenced the old address | Recreated containers preserving recorded hardened configuration and data; reattached Kind networks and refreshed CoreDNS |
| README starts a third model beside the default 8 GiB pair | Quickstart launched reranker without selecting a compatible pack | Default quickstart now uses reasoning/embedding with reranking explicitly off |
| Bastion prerequisites still recommend Helm 3.12+ | The current deployment launcher requires Helm 4 and the verified local run uses 4.3.0 | Corrected the prerequisite to the pinned 4.3.0 client and documented host standard-library Python for deployment rendering |
| CRC and internal site qualification | Insufficient CRC startup headroom; no authorized internal GitLab/Quay/namespace supplied | Record NOT RUN and exact environment/acceptance requirements; Kind cannot close OpenShift or site controls |

Private logs, snapshots, configuration and model checksum manifests stay outside
Git. Final evidence must name the deployed source/image identity, commands,
results, persistence/recovery observations and every unexecuted acceptance check.

## Redeployment observations (migration still running)

The exact published bundle passed the outer checksum and bootstrap with the
existing independently retained signing public key. All five images loaded into
the authenticated TLS registry. Live `airgap:validate` passed. The published
main pipeline passed build, packaging, bootstrap, Kind pipeline, gateway-fault
and lifecycle lanes; hosted OpenShift remained skipped.

Old agent/Jaeger workloads and the Qdrant release were removed by name. All
three existing data/snapshot/Jaeger claims survived. Helm then installed separate
Qdrant and application releases from the verified checkout. The pre-teardown
search returned three hits; its exact Jaeger trace remained readable after the
rebuild. Both real served model IDs, dimension 1024, TLS/auth, reasoning finish
and streaming `[DONE]` passed from the new candidate's application pod.

The first `airgap:deploy` exited 201 through Task after the unchanged 300-second
agent rollout wait: readiness is correctly `503 representation=legacy`. This is
not a successful full deployment. An explicit `INGEST_ALIAS_PUBLISH=true` /
`INGEST_REINGEST=true` migration is running with all 452 hash-matched originals,
one worker and real Qwen embeddings under the recorded immutable revision. The
old physical collection remains the alias target until verified publication.
The selected operator wait is 86400 seconds, matching the existing Job deadline;
no product timeout/default was changed.

WSL swap reached its configured 2 GiB capacity during startup/migration; do not
claim a paging-free run. Windows free memory was later 6,652,880 KiB. Sustained
workload, final publication, next ordinary ingest and full application checks
remain pending. Model/gateway sessions and the ingest Job must not be restarted
just because a polling window expires.

## Remaining environment acceptance

| Environment | Missing proof | Closure requirement |
|---|---|---|
| CRC/OpenShift | Current candidate SCC, Route/OAuth, Service CA, network-policy and storage acceptance | A host meeting the documented 14 GiB available-memory startup gate for the 12 GiB CRC VM; run the same bundle through the CRC mock-computation lane with the real gateway and record all selected controls |
| Internal site | Actual GitLab import/checkout, Quay push/pull and target-namespace identity | Authorized internal endpoints, namespace/deployer, trusted CA/pull credentials and suitable RWO storage; verify the exact bundle/images and scoped permissions, then execute deploy/ingest/smoke and site controls |
| Production topology | Independent-worker 6/3/2 placement and failure tolerance | The intended multi-worker OpenShift environment and platform model endpoints; local single-node Kind and CI software HA do not establish site resilience |

These rows are **NOT RUN**, not waived production passes. No production transfer
or promotion is performed by this audit.

## Transport correction and verification checkpoint

The first migration attempt encountered an embedding HTTP 502. Nginx recorded
`upstream prematurely closed connection` on its IPv6 `host.docker.internal:4000`
hop; model, gateway and proxy containers had zero restarts and no OOM flag. The
failed attempt was stopped deliberately, preserving its private log and staging
state. Its obsolete host wait was terminated only after the Job was deleted.

The TLS front now joins the gateway's existing Docker network and routes to the
gateway container by name. Certificates, authentication, gateway keys, model
revisions and deadlines are unchanged. Nginx configuration validation and reload
passed. Ten real embedding batches (128 vectors each, dimension 1024) then passed
from the candidate agent. Full migration resumed through the same operator
command and shared progress storage; this is not yet a publication pass.

An independent check inside the actual ingest container verified all 452 source
hashes and a kernel read-only corpus mount. Serving snapshot writes returned HTTP
403, and original Qdrant/Jaeger PVC identities were retained.

Host-tool candidate `67ad7ebde01731b83c5afaf2bc68f21c93746a2f` passed
`sh scripts/tools/run-task.sh qa:check`: exit 0, 2,426 tests passed, 42 deselected,
two warnings; Ruff and mypy (54 source files) passed. The focused launcher suites
passed 80 tests. Later documentation-only corrections distinguish the proposed
Zowe sidecar from the deployed chart and describe the verified direct proxy route.
The application remains the published `cbcc74b` release throughout.

Private evidence directory: `/tmp/helm-live-audit`; bootstrap/load/validate,
original and resumed migration logs, source-hash/mount proof, gateway probes,
resource samples and rollback inventories are separate files. They are not
committed or attached publicly. Live completion and ordinary-operation evidence
will supersede this in-progress checkpoint.

The verified release and private operator inputs have also been retained under
`~/.config/mainframe-rag/helm-live-audit/runtime`, with Helm 4.3.0 and a scoped
`run-airgap.sh` wrapper. Its `airgap:validate` passed (exit 0). The active ingest
continues from its original workspace; this copy does not authorize overlapping
writers. After ingestion completes, the persistent wrapper is available for the
ordinary repeat and subsequent deployment operations.
