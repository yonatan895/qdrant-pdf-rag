# Fresh local Kind stack with real models

This connected-host procedure starts from a new clone and the published-main
bundle. It does not require a CRC installation, old containers, a retained
registry database, or a previous `airgap.env`. It uses the same Helm deployment
pipeline as production, with explicit single-node Kind overrides. Original
manuals and an optional verified checkpoint are operator inputs outside Git.

## 1. Acquire source and the published bundle

Prerequisites: Docker with NVIDIA GPU support, Python 3.14, Git, GitHub CLI,
curl, OpenSSL, Apache `htpasswd`, and tmux. Authenticate GitHub and the approved
Hugging Face account before model downloads. Keep private inputs out of shell
traces. Use at least the documented two-model GPU budget, and allow disk for
both the checkpoint and its expanded restore. Do not run competing model packs.

```sh
git clone https://github.com/yonatan895/qdrant-pdf-rag.git
cd qdrant-pdf-rag
sh scripts/tools/install-task.sh
sh scripts/tools/run-task.sh --list
sh scripts/tools/run-task.sh dev:setup --summary
sh scripts/tools/run-task.sh dev:setup PY=python3.14
umask 077
export KIND_STATE="$HOME/.config/mainframe-rag/kind-live"
export KIND_NAME=rag-kind
export KIND_NAMESPACE=rag-kind
mkdir -p "$KIND_STATE"
export IMAGE_SHA="$(git rev-parse HEAD)"
gh run list --workflow e2e.yml --branch main --limit 5
# Select the successful run whose headSha equals IMAGE_SHA, not an older green run.
export RUN_ID=<matching-run-id>
gh run download "$RUN_ID" --name "sneakernet-bundle-$IMAGE_SHA" \
  --dir "$KIND_STATE/release"
```

Follow [bundle verification and bootstrap](install_and_ops.md#42-transfer--automated-bootstrap)
with the independently trusted public signing key. Use a new extraction directory
and run application deployment commands from its bootstrapped checkout. Never
relabel an old application image or inject changed source into the bundle.
For local launcher/doc fixes record the development SHA separately from the
published application SHA. Application behavior changes require a newly published
candidate before claiming exact-candidate acceptance.

Install the checksum-pinned Kind, kubectl and Helm clients from the
[workflow](../.github/workflows/e2e.yml) into `$KIND_STATE/bin`, replacing its
`/usr/local/bin` destination with that directory. Export that directory first in
`PATH` and use `KUBECONFIG=$KIND_STATE/kubeconfig` explicitly. These tools must
not reuse CRC adapters or alter the default kubeconfig.

## 2. Preserve the authorized checkpoint before teardown

For a checkpoint rebuild, quiesce ingest/admin writers. Record the serving alias
and physical collection, take synchronous Qdrant snapshots of **both** the data
collection and its `__completions` companion, download them, and record SHA256,
collection configuration and counts. Copy the ingest-work `inventory.jsonl` and
any pending publication sidecar, plus the original corpus with its relative
filenames. A pending build is a recovery operation; do not certify it as a
committed checkpoint. Keep backups outside the cluster and outside Git.

Verify every original PDF hash against the checkpoint inventory. Restore both
snapshots into an isolated pinned Qdrant server first, under their original
physical names, and compare counts, stored content/vector samples, manifest
state/digest and completion records. A data-only historical snapshot is not a
current checkpoint and can require complete re-embedding. The current
[metadata and publication contracts](ingest.md#metadata-contract) still apply.

After successful isolated verification, remove only the enumerated old local
stack containers/clusters and their owned foreground launcher sessions. Keep
original documents, checkpoint files, and protected evidence. Do not use global
Docker prune. This clean-reset procedure deliberately replaces cluster storage;
ordinary [preserved-volume operations](local-real-corpus.md) do not.

## 3. Start named model servers and a fresh gateway

Use the exact model revisions and immutable-view procedure in
[the model runbook](local-crc-environment.md#3-pin-and-start-the-two-model-servers-sequentially).
Only that model acquisition/profile section is shared with CRC; no CRC state is
required. Retain `LOCAL_CRC_32GB` as the explicit existing profile identifier
until its owner approves a rename; a profile label is not a running CRC service.
Set `REASONING_VIEW` and `EMBED_VIEW` to newly prepared immutable local views.

In retained tmux sessions, run from the development clone, first reasoning:

```sh
sh scripts/tools/run-task.sh local:llm CONTAINER_NAME=rag-kind-reasoning \
  MODEL="$REASONING_VIEW" SERVED_NAME=google/gemma-4-E4B-it-qat-mobile-ct \
  PORT=8000 BUDGET_PROFILE=LOCAL_CRC_32GB \
  VLLM_IMAGE=vllm/vllm-openai@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14
```

Wait for `http://127.0.0.1:8000/health`, then start embedding:

```sh
sh scripts/tools/run-task.sh local:embed CONTAINER_NAME=rag-kind-embed \
  MODEL="$EMBED_VIEW" SERVED_NAME=Qwen/Qwen3-Embedding-0.6B \
  PORT=8001 BUDGET_PROFILE=LOCAL_CRC_32GB \
  VLLM_IMAGE=vllm/vllm-openai@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14
```

Wait for embedding health before gateway probes. No model/window overrides are
needed for naming. Keep reranking disabled for this two-model pack.

Generate a new protected gateway restart file using
[the gateway provisioning recipe](local-crc-environment.md#4-start-and-preserve-the-gateway),
setting `LOCAL_STATE=$KIND_STATE`, `GATEWAY_NAME=rag-kind-gateway`,
`PG_NAME=rag-kind-gateway-db`, `PG_VOLUME=rag-kind-gateway-data`,
`PG_NET=rag-kind-models`, and `GATEWAY_ENV_FILE=$KIND_STATE/gateway.env`.
Use fresh keys with this fresh database; do not import the old CRC restart file.
Run `local:gateway:up` in a retained tmux session with its output redirected to a
private log. Source its handoff only in consumer shells and then explicitly set
`RERANK_ENABLED=false`, `DENSE_DIM=1024`, and the immutable embedding revision.

## 4. TLS, registry and cluster

Use the existing [TLS/registry recipe](local-crc-environment.md#5-tls-authenticated-registry-and-windows-clients)
with this complete substitution table for a **new** identity:

| Recipe input or literal | Fresh Kind value |
|---|---|
| `LOCAL_STATE` | `$KIND_STATE` |
| CA common name | `Local Kind CA` |
| `host.crc.testing` | `rag-kind.test` (including certificate SANs) |
| `crc-loader` | `kind-loader` |
| `crc-fit-registry` | `rag-kind-registry` |
| `crc-fit-gateway-tls` | `rag-kind-gateway-tls` |
| `crc-litellm-gateway` | `rag-kind-gateway` |
| `crc-litellm-gateway-net` | `rag-kind-models` |

Keep the pinned images, non-root TLS proxy, authenticated registry, and TLS
verification. Registry host port is 5443; gateway host port is 8444. Neither
service needs the old CRC containers. Use a generated registry password supplied
to `htpasswd` on stdin; keep its protected auth JSON for the loader and pull Secret.

Set the adapter inputs explicitly:

```sh
export LOCAL_STATE="$KIND_STATE"
export KIND_BIN="$KIND_STATE/bin"
export KIND_ARTIFACT_ROOT="$KIND_STATE/candidate"
export KUBECONFIG="$KIND_STATE/kubeconfig"
export REGISTRY_AUTH_FILE="$KIND_STATE/registry-auth.json"
export PATH="$KIND_BIN:$PATH"
```

The fresh auth JSON must contain `auths["rag-kind.test:5000"]` and
`auths["rag-kind.test:5443"]`, each with the base64-encoded
`kind-loader:password` credential. Keep it mode 0600. Skip the fallback guide's
copy-from-old-auth step: there is no old registry credential to reuse. Give the
Skopeo adapter a directory containing `ca.crt` at `/certs`; when the host path
contains a colon (for example `rag-kind.test:5000`), use Docker's
`--mount type=bind,src=...,dst=/certs,readonly`, not the colon-delimited `-v` form.
Do not mount CA private keys into the loader.

Follow [Kind network and node trust](local-release-fallback.md#32-reuse-authenticated-tls-services-without-changing-host-ports)
through cluster creation using the same substitutions. Add read-only Kind node
mounts for `$KIND_STATE/corpus` at `/mnt/rag-kind-corpus` and
`$KIND_STATE/checkpoint` at `/mnt/rag-kind-checkpoint`. The corpus is a static
read-only local PV; Qdrant data/scratch and Jaeger use writable local-path claims.
After both services join the `kind` network, the registry gets Docker DNS alias
`rag-kind.test`; CoreDNS maps that same hostname to the TLS gateway address.
The node registry and pod gateway use different ports and DNS paths.

The TLS proxy may resolve the gateway dynamically through Docker DNS to survive
its recreation without a stale upstream address. In its `http` block add
`resolver 127.0.0.11 valid=10s ipv6=off;`, and replace the location's static
`proxy_pass` with:

```nginx
set $gateway_backend rag-kind-gateway:4000;
proxy_pass http://$gateway_backend;
```

Keep proxy buffering off and the existing streaming timeouts. Validate `nginx -t`
and make a certificate-verified gateway request from an application pod after
startup and after any gateway recreation.

## 5. Helm deployment and checkpoint restore

From the bootstrapped published checkout, create `rag-kind` and the file-based
Secrets `rag-kind-registry-pull`, `rag-kind-gateway-keys` plus
ConfigMap `rag-kind-gateway-ca`. Use the existing pipeline's writer/read-only
Qdrant key split; Helm creates `qdrant-apikey` on the first deployment. Do not
pre-create that chart-owned Secret or pass its writer key to serving pods.

Prepare a new `airgap.env` from its repository example. Set all operator inputs
explicitly using [the deployment recipe](local-crc-environment.md#72-configure-and-deploy-the-same-candidate):
namespace `rag-kind`, registry `rag-kind.test:5000`, storage class `standard`,
`AGENT_ROUTE=false`, the new pull/gateway Secret and CA names, model URLs
`https://rag-kind.test:8444/v1`, exact served IDs and dimension 1024, immutable
embedding revision, `RERANK_ENABLED=false`, the chart’s enabled console, and
`CHAT_CONDENSE_ENABLED=true` for pronoun follow-ups. The latter is an explicit
local choice, not a product-default change. Keep the published `IMAGE_SHA`.

Use the [real-corpus resource overrides](local-real-corpus.md#1-establish-the-candidate-data-and-resource-budget):
1/1/1 Qdrant topology, one ingest worker, Kind-only fsGroup 3000, two agent
replicas, Qdrant data/snapshots sized for current data plus a replacement, and
Jaeger persistent Badger storage. Mount `/mnt/rag-kind-checkpoint` read-only at
`/qdrant/snapshots/checkpoint` for restore; never mount a host source over the
writable Qdrant data directory. Provision the read-only corpus PV separately.

Export `AIRGAP_ENV=$KIND_STATE/airgap.env` and the independently trusted
`SNEAKERNET_TRUSTED_PUB`. Unset host gateway consumer URLs/keys before invoking
the pipeline so they cannot override pod endpoints and Secret references.
Run `sh scripts/tools/run-task.sh airgap:validate`, then `airgap:load` and
`airgap:deploy` through that same Task entry. Restore the checkpoint's
data and completion collections synchronously under their original names with
the Qdrant **writer** credential, then restore the serving alias. Verify the
committed manifest and actual content, not just collection counts. Copy the
checkpoint inventory to the fresh ingest-work PVC at the same application path.
Before ordinary ingest, render its scratch claim without starting a writer:

```sh
AIRGAP_DRYRUN=1 sh scripts/tools/run-task.sh airgap:ingest
kubectl apply -f dist/ingest-work-rendered.yaml
```

Use a one-shot Pod with the published ingest image and registry pull Secret,
mounting `ingest-work` at `/work` and the node checkpoint path read-only at
`/checkpoint`. Its command is `python3 -c 'from pathlib import Path;
Path("/work/inventory.jsonl").write_bytes(Path("/checkpoint/inventory.jsonl").read_bytes())'`.
On Kind give this copy Pod UID/GID 1000 and fsGroup 3000 so the existing private
checkpoint is readable and the new scratch directory is writable. Wait for its
`Succeeded` phase and delete it before launching the writer.

For snapshot recovery, use a temporary loopback port-forward to Qdrant and read
the writer key from `qdrant-apikey` privately. The REST operation for each of the
two original physical names is `PUT /collections/NAME/snapshots/recover?wait=true`
with JSON `{"location":"file:///qdrant/snapshots/checkpoint/FILE.snapshot",
"priority":"snapshot","checksum":"RECORDED_SHA256"}`. Require success, then
inspect `/collections/NAME`, scroll stored payload/vectors, and inspect the
completion manifest. Create the alias only after both collections verify using
`POST /collections/aliases` with
`{"actions":[{"create_alias":{"collection_name":"PHYSICAL","alias_name":"mainframe_manuals"}}]}`.
Do not restore over an existing live collection or create an alias to unverified
metadata. Remove the maintenance port-forward afterward.

Run ordinary `sh scripts/tools/run-task.sh airgap:ingest` with no force/re-embedding flags. Require all original
sources accounted for, no failed files, valid completion coverage and unchanged
membership. An incompatible checkpoint must refuse; diagnose the representation
difference rather than inventing completion markers or changing model identity.

## 6. Acceptance and ongoing use

Run the candidate gateway probe from an application pod, including tokenizer,
real embeddings, reasoning and streaming completion. Run `local:check` with the
original manual question and follow-up using private report files. Require
accepted cited answers, a genuine zero-citation trap refusal, terminal SSE
completion, fixed overlong-input rejection, healthy console and a fresh Jaeger
search trace. An uncited draft is not a passing refusal.

Retain port forwards in named tmux sessions:

```sh
kubectl -n rag-kind port-forward --address 127.0.0.1 svc/rag-agent 8087:8080
kubectl -n rag-kind port-forward --address 127.0.0.1 svc/jaeger 16686:16686
```

Open `http://localhost:8087/ui` and `http://localhost:16686`; verify them from the
browser host too. Replace Qdrant, agent and Jaeger pods, then repeat an ordinary
search/answer and fetch the exact pre-replacement trace ID. Record imageIDs,
PVC identities, corpus hashes, collection/control verification, RAM/VRAM and
actual end-to-end timing. Keep the startup commands, protected credentials,
kubeconfig and logs under `KIND_STATE` for repeatable restarts. Old CRC containers
and anonymous model servers must not remain as hidden dependencies.

This verifies the local Kind deployment. It does not qualify OpenShift SCC,
OAuth Routes, multi-node failure tolerance or production release promotion.

If host package installation requires unavailable sudo credentials, Ubuntu
operators can provision the registry-password tool without changing the host:
`apt-get download apache2-utils libapr1t64 libaprutil1t64` into a private tool
directory, extract each with `dpkg-deb -x`, and run its `usr/bin/htpasswd` with
`LD_LIBRARY_PATH` pointing to the extracted `usr/lib/x86_64-linux-gnu`. Record
package versions and retain that tool directory. This is connected workstation
tooling, not an application dependency or an air-gap runtime download.
