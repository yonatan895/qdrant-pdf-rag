# Reproduce the complementary local release rehearsal

Use this operating mode when the simultaneous CRC/model fit gate fails. The
recorded attempt passed a 32-minute live workload but failed Windows headroom
after a 12GiB CRC cold start; 10.5GiB CRC could not schedule the required ingest
worker. Do not report a fully live CRC qualification from that partial success.
Both complementary local lanes subsequently passed against candidate
`89e10d3926b2e7f6a2e6ad0c1450c68c60e1b5f8` and its unchanged original tarball.
The measured results and acceptance rules remain in [crc-release-verification.md](crc-release-verification.md).

The two local lanes and required CI jobs must use the **same original signed
bundle**. Keep separate vector stores, namespaces, kubeconfigs and records:

| Lane | Running locally | Stopped |
|---|---|---|
| CRC | Real OpenShift, product images, OAuth, Qdrant, Jaeger, LiteLLM/PostgreSQL, TLS registry/gateway; deterministic model computation | Both GPU models and Kind nodes |
| Disposable Kind | The same product images and pipeline, both real GPU models, LiteLLM/PostgreSQL, TLS registry/gateway, Qdrant, Jaeger | CRC and mock computation |

The combined OpenShift/live-model fit remains a coverage gap in this mode. Mock
citations prove interfaces, not answer quality. Site-specific production storage,
identity, network policy and capacity acceptance are still required.

## 1. Shared prerequisites and evidence

First complete the host, pinned-model, gateway, TLS-registry and Windows-client
setup in [local-crc-environment.md](local-crc-environment.md). Preserve the
original Kind cluster and its volumes. Start with a green published-main bundle,
its outer checksum and the independently trusted public signing key. Use the
[trusted bootstrap](install_and_ops.md#42-transfer--automated-bootstrap) in a fresh
directory for the disposable Kind lane; do not repack from its checkout.

Record the original tar SHA256, full commit, archive/registry image identities,
model revisions, configuration hashes and verification mode. Keep credentials,
PDFs, snapshots and private evidence outside Git. The two lanes may use different
local namespace/registry coordinates; record both configurations and their hashes.
The image bytes and application behavior remain those of the same candidate.

## 2. CRC with deterministic computation behind the real gateway

1. Preserve any real-vector CRC namespace. Scale its agent, Qdrant and Jaeger to
   zero before changing model computation; retain all PVCs and completed evidence.
2. Stop the recorded real model containers. Keep the existing real gateway,
   PostgreSQL key store, TLS front and authenticated registry running.
3. In two terminals, run the existing mock from the verified candidate checkout:

   ```sh
   # Terminal 1: reasoning computation only.
   MOCK_DIM=1024 PORT=8000 python3 scripts/mock_vllm.py

   # Terminal 2: embedding computation only.
   MOCK_DIM=1024 PORT=8001 python3 scripts/mock_vllm.py
   ```

   These are host-side test processes, not application image additions. The
   retained LiteLLM configuration still exposes the explicit Gemma/Qwen served
   IDs and per-leg virtual keys; its upstream requests now reach the deterministic
   stand-in. Mark this model computation as mocked in the record. Do not call the
   stand-in directly from agent or ingest.
4. Source the private gateway handoff, then set `RERANK_ENABLED=false`,
   `DENSE_DIM=1024`, `EMBED_MODEL_REVISION=local:Qwen/Qwen3-Embedding-0.6B`,
   the HTTPS consumer URLs and complete CA bundle as in the
   local guide. Require `probe_gateway.py --require-reasoning --stream` to pass.
5. With at least 14GiB available in Windows, start CRC at 12288MiB. Keep the
   original Kind nodes stopped. The smaller Qdrant request still matters for
   Kubernetes reservations even though GPU models are now off.
6. Create a **new namespace** and separate corpus/data PVCs using the local
   guide's production-overlay recipe. Use the same gateway/pull credentials and
   CA trust through operator-created Secrets/ConfigMaps. Generate original
   synthetic PDFs with the loaded candidate ingest image. Do not use the
   real-vector namespace or copy mock vectors into it.
7. Apply the reviewed namespace egress policy after recording its public TCP
   positive control. Retain the approved CRC node-registry exceptions separately;
   application NetworkPolicy does not enforce node image pulls.
8. Run the full `sh scripts/tools/run-task.sh airgap:pipeline`, actual pod gateway/application contracts,
   TLS/auth negative controls, browser OAuth and stream completion, SCC/image
   identity checks, snapshot/PVC/trace lifecycle, and a second complete pipeline.
   Compare point IDs and credential fingerprints before/after.

The mock namespace can have the same collection *name* as the real namespace
only because its Qdrant instance and PVCs are separate. Verify the server/namespace
as well as the collection name before every ingest or snapshot operation.

Before switching lanes, stop CRC and terminate only the two recorded mock
processes. Retain both CRC namespaces, PVCs and their records. Keep the gateway,
PostgreSQL, registry and TLS front running.

## 3. Disposable Kind with both real models

Start reasoning and then embedding with the same immutable views, served IDs,
image digest and `LOCAL_CRC_32GB` profile from the local guide. Wait for both
backends and the authenticated HTTPS gateway probe. Keep CRC stopped.

### 3.1 Use isolated clients and configuration

Use the checksum-pinned Kind, kubectl and Helm versions from
[the passing workflow](../.github/workflows/e2e.yml): Kind 0.33.0, kubectl 1.37.0,
Helm 4.3.0 and this node image:

```text
kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
```

Install these in a task-local `KIND_BIN`, leaving existing developer binaries
alone. Verify their in-repository SHA256 values before invoking them. Set:

```sh
umask 077
export KIND_STATE="$HOME/.config/mainframe-rag/local-kind-release"
export KIND_BIN=/absolute/path/to/checksum-verified/kind-tools
export KIND_NAME=rag-live-release
export KIND_NAMESPACE=rag-kind-live-release
mkdir -p "$KIND_STATE"
export KUBECONFIG="$KIND_STATE/kubeconfig"
export PATH="$KIND_BIN:$PATH"
export KC=kubectl
# In this shell, all operations target the new Kind cluster.
# Do not leave the CRC operator adapters earlier in PATH.
```

Every Kind command uses this private kubeconfig. Do not switch the user's default
context or use the Windows CRC adapters for Kind.

### 3.2 Reuse authenticated TLS services without changing host ports

The registry from the local guide listens on TLS port 5000 **inside its container**
and host loopback 5443. The gateway TLS front listens on 8444. Attach these two
existing containers to Kind's standard Docker network; preserve their existing
network connections. Kind creates the `kind` network, or it may already exist
for the preserved cluster:

```sh
# If no Kind network exists, create it before attaching the services.
# On the measured host it already existed; do not recreate it.
docker network inspect kind

docker network connect --alias host.crc.testing kind crc-fit-registry
docker network connect kind crc-fit-gateway-tls
```

Run each connection command only when that container is not already attached.
For a machine with no existing network, create `kind` with Docker's bridge driver
first. Record both assigned addresses:

```sh
docker inspect -f '{{(index .NetworkSettings.Networks "kind").IPAddress}}' crc-fit-registry
export KIND_GATEWAY_IP="$(docker inspect -f '{{(index .NetworkSettings.Networks "kind").IPAddress}}' crc-fit-gateway-tls)"
```

Use `INTERNAL_REGISTRY=host.crc.testing:5000` in this lane. The loader runs on
Docker's `kind` network and reaches the registry's internal TLS port; it does not
use the preserved HTTP registry on host port 5000. The node resolves
`host.crc.testing` through Docker DNS to the registry alias. The application pods
will resolve that hostname through CoreDNS to the **gateway** address on 8444.
These two DNS paths must both be tested; sharing a hostname does not prove them.

Copy the existing protected registry auth entry to the new authority without
printing credentials or changing its password:

```sh
python3 - <<'PY'
import json, os
from pathlib import Path
source = Path(os.environ['LOCAL_STATE']) / 'registry-auth/auth.json'
target = Path(os.environ['KIND_STATE']) / 'registry-auth.json'
auth = json.loads(source.read_text())
auth['auths']['host.crc.testing:5000'] = auth['auths']['host.crc.testing:5443']
target.write_text(json.dumps(auth)); target.chmod(0o600)
PY
```

Create a separate pinned Skopeo adapter for this lane. Set `KIND_ARTIFACT_ROOT`
to the fresh extracted candidate directory, containing its bootstrapped clone:

```sh
export KIND_ARTIFACT_ROOT=/absolute/path/to/fresh-kind-candidate
cat > "$KIND_BIN/skopeo" <<'SH_SKOPEO'
#!/bin/sh
set -eu
: "${KIND_ARTIFACT_ROOT:?Set the fresh verified candidate directory}"
: "${KIND_STATE:?Set the protected Kind state directory}"
: "${LOCAL_STATE:?Set the shared TLS service state directory}"
exec docker run --rm -i --network kind \
  --user "$(id -u):$(id -g)" --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "$KIND_ARTIFACT_ROOT:$KIND_ARTIFACT_ROOT:ro" \
  -v "$LOCAL_STATE/registry-trust:/certs:ro" \
  -v "$KIND_STATE/registry-auth.json:/auth.json:ro" \
  -e REGISTRY_AUTH_FILE=/auth.json -w "$PWD" \
  quay.io/skopeo/stable@sha256:0f75798d450d0cc0ea3700c79d929ae7609fb7d0e627673c14be8a484587c9b1 "$@"
SH_SKOPEO
chmod 700 "$KIND_BIN/skopeo"
```

Keep `INSECURE_REGISTRY=false` and `SKOPEO_ARGS='--dest-cert-dir /certs'`.
The CRC adapter and host loopback listeners remain available for its next run.

### 3.3 Create the new cluster and node trust

```sh
mkdir -p "$KIND_STATE/node-trust/host.crc.testing:5000"
cp "$LOCAL_STATE/pki/ca.crt" "$KIND_STATE/node-trust/host.crc.testing:5000/ca.crt"
cat > "$KIND_STATE/node-trust/host.crc.testing:5000/hosts.toml" <<'EOF_HOSTS'
server = "https://host.crc.testing:5000"
[host."https://host.crc.testing:5000"]
  capabilities = ["pull", "resolve"]
  ca = "/etc/containerd/certs.d/host.crc.testing:5000/ca.crt"
EOF_HOSTS
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['KIND_STATE'])
config = {'kind':'Cluster', 'apiVersion':'kind.x-k8s.io/v1alpha4',
          'nodes':[{'role':'control-plane', 'extraMounts':[
              {'hostPath':str(root/'node-trust'),
               'containerPath':'/etc/containerd/certs.d', 'readOnly':True}]}]}
(root/'kind-config.json').write_text(json.dumps(config))
PY
"$KIND_BIN/kind" create cluster --name "$KIND_NAME" \
  --config "$KIND_STATE/kind-config.json" --kubeconfig "$KUBECONFIG" \
  --image kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
chmod 600 "$KUBECONFIG"
kubectl get nodes
kubectl get storageclass standard
```

Save CoreDNS's original configuration, then add the gateway hostname to the
new cluster only. The following refuses an existing hosts block for operator
review instead of overwriting it:

```sh
kubectl -n kube-system get configmap coredns -o json > "$KIND_STATE/coredns-before.json"
python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
root = Path(os.environ['KIND_STATE'])
cm = json.loads((root/'coredns-before.json').read_text())
core = cm['data']['Corefile']
assert 'hosts {' not in core and '.:53 {' in core
entry = '.:53 {\n    hosts {\n        ' + os.environ['KIND_GATEWAY_IP'] + ' host.crc.testing\n        fallthrough\n    }'
cm['data']['Corefile'] = core.replace('.:53 {', entry, 1)
subprocess.run(['kubectl','apply','-f','-'], input=json.dumps(cm), text=True, check=True)
PY
kubectl -n kube-system rollout restart deployment/coredns
kubectl -n kube-system rollout status deployment/coredns --timeout=180s
```

### 3.4 Deploy from the fresh bundle

Create the new namespace, `crc-registry-pull` Secret from the Kind auth file,
`crc-gateway-keys` from the retained gateway handoff, and `crc-gateway-ca` from
its complete CA bundle. Use the local guide's file-based Secret commands; no
OAuth cookie or Route is needed in Kind.

Copy the local sizing files into `KIND_STATE`. Keep the one-worker ingest patch.
The Kind Qdrant override additionally needs the same volume group used in CI:

```yaml
replicaCount: 1
podSecurityContext: {fsGroup: 3000}
resources:
  requests: {cpu: 200m, memory: 256Mi}
  limits: {cpu: "2", memory: 2Gi}
```

Kind has no SCC to assign this group. It is a local Kind override; OpenShift
continues to use project-assigned identities and must not receive this fixed ID.

Create a protected `AIRGAP_ENV` for the Kind lane using the local CRC file as a
starting template, changing exactly these coordinates:

| Input | Kind value |
|---|---|
| `NAMESPACE` | The new `KIND_NAMESPACE` |
| `INTERNAL_REGISTRY` | `host.crc.testing:5000` |
| `STORAGE_CLASS` | `standard`, after checking the actual provisioner |
| `AGENT_ROUTE` | `false` |
| `QDRANT_EXTRA_VALUES` / `INGEST_EXTRA_PATCH` | The Kind files' absolute paths |

Keep the full SHA, original model IDs, dimension 1024, the original
`EMBED_MODEL_REVISION`, gateway HTTPS URLs, Secret/CA names, reranking
disabled, 1Gi data/corpus/scratch sizing and Jaeger's 10Gi claim. Clear host consumer URL/key exports before invoking deployment.
Keep `SNEAKERNET_TRUSTED_PUB` set to the independently trusted public PEM.

```sh
sh scripts/tools/run-task.sh airgap:validate
sh scripts/tools/run-task.sh airgap:load
```

Use the local guide's temporary application-image gateway probe, substituting
the new namespace and registry authority and using the default ServiceAccount.
Require successful TLS/auth, both real models, streaming finish and dimension.
Then create the separate synthetic corpus PVC and generator with `standard`
storage and the loaded Kind-authority ingest image. Retain the same original
synthetic generator and security settings. Run:

```sh
sh scripts/tools/run-task.sh airgap:pipeline
kubectl -n "$KIND_NAMESPACE" exec deploy/rag-agent -- \
  python3 /app/scripts/probe_gateway.py --require-reasoning --stream
kubectl -n "$KIND_NAMESPACE" exec -i deploy/rag-agent -- python3 - \
  < scripts/ci/application_contracts.py
```

Run the gateway probe to completion before starting the application contract
client. With one active reasoning sequence, an overlapping diagnostic can queue
behind a long answer and hit its unchanged probe timeout. Retain such failures
and rerun under the declared single-user conditions; do not increase deadlines.

Verify a cited answer against the generated material, contextual follow-up, all
streams, representative RAG prompts and a long embedding input below the actual
4096-token model limit. Record model revisions/launch flags, exact imageIDs,
request results and host/WSL/GPU observations. Run [TLS/credential and long-input controls](local-crc-environment.md#8-reproduce-the-certificate-and-long-input-controls)
from the actual application image. CI supplies deterministic upstream,
malformed, dimension, timeout and truncated-stream fault coverage against the
same bundle; do not inject mock vectors into this real collection.

For persistence and redeployment:

```sh
sh scripts/ci/check_lifecycle.sh "$KIND_NAMESPACE"
sh scripts/tools/run-task.sh airgap:pipeline
```

Compare point IDs/counts, credential fingerprints, PVC identities and old trace
recovery. Access the console privately with `kubectl -n "$KIND_NAMESPACE"
port-forward svc/rag-agent 8080:8080`. OAuth, SCC, Service CA, Routes and OpenShift
network enforcement are covered in CRC, not inferred from Kind.

## 4. Retain evidence and recover the original environment

For ongoing console access to your manuals, follow [the real-corpus restore](local-real-corpus.md)
in a separate local deployment. Retain that deployment while it is in use; the
cleanup below applies to the finished disposable synthetic rehearsal.

Stop the private port-forward and observer after recording results. Export only
the synthetic recovery evidence needed for the record. When the disposable
rehearsal is finished, remove only its cluster and its two added network
connections:

```sh
"$KIND_BIN/kind" delete cluster --name "$KIND_NAME" --kubeconfig "$KUBECONFIG"
docker network disconnect kind crc-fit-registry
docker network disconnect kind crc-fit-gateway-tls
```

The TLS services retain their original Docker connections and loopback ports.
Keep the shared `kind` network and preserved registry. Only after this cleanup,
restart the recorded original Kind nodes with their saved kubeconfig and verify
original PVCs, counts, aliases and backups. Never delete/recreate the original
cluster or prune its volumes.

For another CRC mock rehearsal, stop GPU models, start the two mock computation
processes behind the retained gateway, probe it, and then start CRC. For another
Kind live rehearsal, keep CRC and mocks stopped and follow section 3. Do not boot
both lanes together and describe that as the approved fallback.

Complete the protected release record only when both local lanes and all required
CI checks pass. Transfer the original tarball and checksum unchanged, followed by
the production bootstrap, strict pod gateway probe before ingest, full application
acceptance and site-specific sign-off in [install_and_ops.md](install_and_ops.md).
