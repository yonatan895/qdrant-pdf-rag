# Release verification

Owner: this file owns the pre-transfer release gate. Deployment commands and
signing contracts remain in [deploy.md](deploy.md) and
[install_and_ops.md](install_and_ops.md#4-standard-deployment-architecture-air-gap-production--local-cluster-testing).
Run shell blocks in order, stop on any nonzero exit, use a separate terminal for
each foreground service and set `umask 077` in operator shells. Keep private
state outside Git and the extracted candidate; save only sanitized evidence.
Local addresses, certificates, model choices and resource overrides below belong
to the rehearsal. Production receives its model endpoints and identities from the
platform team.

## Gate

Every published-main release must pass both lanes below against the **same
original signed bundle** before its transfer into production. A
missing, skipped or failed required check blocks transfer. The pipeline's
`OPERATIONAL & ACCEPTED` banner alone does not pass this gate. Copy
[the record template](release-record.md) outside Git for each candidate and
initialize every result as `NOT RUN`.

| Lane | Required real components and coverage |
|---|---|
| Required GitHub jobs | Fresh bootstrap of the published bundle, real application containers, registry, gateway/database and Kubernetes; deterministic computation and the gateway fault contracts (upstream, malformed, dimension, timeout, truncated stream, credentials); Open WebUI on through the operator `airgap:pipeline` with OpenShift and IdP stand-ins (`kind-openwebui-operator`): the 20 checks, the two-pass Pipe install, the K1 rollback, and a [slow-model stand-in](openwebui-dev.md#slow-model) answering with its citation on the browser, API-chat and API-stream paths |
| Disposable local Kind | The same product images and pipeline, both real GPU models behind the authenticated TLS gateway, Qdrant, ingest, OpenObserve, and Open WebUI on through the same operator stand-ins and scripts as `kind-openwebui-operator` (its 20 checks, two-pass Pipe and K1 rollback, [below](#openwebui-pass)) answered by the real models; citations, follow-up chat, all streams, lifecycle and recovery |

**What each lane proves for Open WebUI.** The GitHub lane proves the wiring and the
time limits of the user-facing path (router stand-in, allow-list proxy, Open WebUI,
docs Pipe, agent, gateway) with stand-in models. Its paced stand-in sends a first
token 150 s after the request and ends the stream at 240 s. No idle or total limit
on that path cuts such an answer; the agent's 270 s request deadline is the binding
limit ([inventory](openwebui-dev.md#slow-model)). It proves nothing about real
model behavior or answer quality. The Kind lane runs the same scripts and checks
with the real models answering. Each answer verdict records its measured seconds
(`end_s`, `done_s`). An answer that ends later than 240 s is outside what CI
proved, and its row fails. Neither lane exercises the browser's socket transport
(the checks read the saved message), OpenShift's HAProxy router (the stand-in is
nginx with the stored Route's timeout) or the site IdP. Site OpenShift acceptance
covers those.

**Not checked before transfer.** OpenShift-specific behavior (`restricted-v2`
SCC admission and assigned identities, the OAuth console Route, Service CA and
reencrypt certificates, agent ingress and namespace egress NetworkPolicy and node image-pull
isolation) has no local lane. It is verified on the site, after transfer and
before go-live, by [site OpenShift acceptance](install_and_ops.md#site-openshift-acceptance).
A failure there blocks go-live and requires a new candidate. Kind evidence never
stands in for those checks.

Mock citations prove interfaces, not answer quality; the Kind lane uses real
models. Site-specific production storage, identity, network policy and capacity
acceptance remain separate. No laptop runner or new deployment framework is
needed. A changed image or configuration requires a new candidate and new
acceptance; never edit a signed bundle or repack separately per lane.
[Extension-surface](adr/0009-gap-fork.md#9-extension-surface-open-question-2-decided)
removals and renames need a [release note](release-notes.md).

## 1. Host and pinned models

| Component | Rehearsal configuration |
|---|---|
| Host | Linux or Windows plus Ubuntu 24.04 WSL, Docker with NVIDIA GPU support |
| Python | CPython 3.14 GIL; repository lockfile |
| Models | Gemma reasoning and Qwen embeddings; 4096-token windows, dimension 1024 |
| Local serving | `LOCAL_RT_8GB` budget profile, reasoning first |
| Reranking | Explicitly disabled for this rehearsal |
| Registry / HTTPS gateway | Loopback listeners on 5443 / 8444; backend gateway on 4000 |
| Tools | Repository-pinned Kind, kubectl and Helm clients |

Verify `docker info` and `nvidia-smi`. Install Git, OpenSSL, a supported Python
3.14 interpreter, Apache `htpasswd` and the repository's pinned deployment tools.
On the connected clone run `sh scripts/tools/install-task.sh`, then
`sh scripts/tools/run-task.sh dev:setup PY=python3.14` and
`sh scripts/tools/run-task.sh artifacts:bm25`. Save model access tokens outside
Git; gated model access must already be approved by the model publisher.
Inventory existing clusters, volumes, corpus, collections and verified snapshot
backups first; this lane creates a new cluster and must not reuse, delete or
prune them.

<a id="pinned-models"></a>
The measured model revisions are:

| Served model ID | Revision |
|---|---|
| `google/gemma-4-E4B-it-qat-mobile-ct` | `3624117cf04528e099519f93839f0f0b7a18913d` |
| `Qwen/Qwen3-Embedding-0.6B` | `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` |

Download these exact revisions into the normal model cache on the connected
host, after authenticating through the approved Hugging Face credential store:

```sh
.venv/bin/python - <<'PY_MODELS'
from huggingface_hub import snapshot_download
for model, revision in (
    ('google/gemma-4-E4B-it-qat-mobile-ct', '3624117cf04528e099519f93839f0f0b7a18913d'),
    ('Qwen/Qwen3-Embedding-0.6B', '97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3'),
):
    print(snapshot_download(repo_id=model, revision=revision))
PY_MODELS
```

Use immutable revision directories. A Hugging Face cache snapshot contains
symlinks into its blob directory; mounting only that snapshot can leave broken
links inside the container. On the same filesystem, create a separate view whose
files are hardlinks to the resolved cached files:

```sh
# Set these to the already downloaded, verified revision and a new view directory.
export MODEL_SNAPSHOT=/path/to/cache/snapshots/REVISION
export MODEL_VIEW=/path/to/local-model-views/ROLE-REVISION
.venv/bin/python - <<'PY'
import os
from pathlib import Path
source = Path(os.environ['MODEL_SNAPSHOT'])
target = Path(os.environ['MODEL_VIEW'])
target.mkdir(parents=True, exist_ok=False)
for item in source.rglob('*'):
    if item.is_file():
        destination = target / item.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(item.resolve(strict=True), destination)
PY
```

Do not edit hardlinked files. Record hashes of weights, configuration and tokenizer
files, the model revision, and the actual image digest. Use one view per model;
set `REASONING_VIEW` and `EMBED_VIEW` to those absolute paths. In retained
terminals, start reasoning first, wait for `http://127.0.0.1:8000/health`, then
start embedding:

```sh
sh scripts/tools/run-task.sh local:llm CONTAINER_NAME=rag-kind-reasoning \
  MODEL="$REASONING_VIEW" SERVED_NAME=google/gemma-4-E4B-it-qat-mobile-ct \
  PORT=8000 BUDGET_PROFILE=LOCAL_RT_8GB \
  VLLM_IMAGE=vllm/vllm-openai@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14

sh scripts/tools/run-task.sh local:embed CONTAINER_NAME=rag-kind-embed \
  MODEL="$EMBED_VIEW" SERVED_NAME=Qwen/Qwen3-Embedding-0.6B \
  PORT=8001 BUDGET_PROFILE=LOCAL_RT_8GB \
  VLLM_IMAGE=vllm/vllm-openai@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14
```

Record actual startup flags and RAM/VRAM; budget estimates do not establish fit.
Do not shorten either context window or change model IDs to make the test fit.
Direct backend `/health` and `/tokenize` calls are serving diagnostics only.
Agent and ingest requests always go through the real gateway.

<a id="gateway"></a>
## 2. Gateway

Use [run_local_gateway.sh](../scripts/run_local_gateway.sh), which owns the pinned
LiteLLM/PostgreSQL containers, database volume and generated configuration. Keep
its handoff and startup log in a private directory: the startup output can contain
credentials. Use a private PostgreSQL password on initial provisioning and reuse
it with the same database volume.

```sh
umask 077
export LOCAL_STATE="$HOME/.config/mainframe-rag/release-services"
mkdir -p "$LOCAL_STATE"
export GATEWAY_NAME=rag-kind-gateway PG_NAME=rag-kind-gateway-db
export PG_VOLUME=rag-kind-gateway-data PG_NET=rag-kind-models
export GATEWAY_ENV_FILE="$LOCAL_STATE/gateway.env"
# First provisioning only: create a restart file without printing credentials.
python3 - <<'PY_KEYS'
import os, secrets
from pathlib import Path
p = Path(os.environ['LOCAL_STATE']) / 'gateway-restart.env'
with p.open('x') as f:
    f.write('PG_PASSWORD=' + secrets.token_hex(24) + '\n')
    for name in ('MASTER', 'LLM', 'EMBED', 'RERANK'):
        f.write('GATEWAY_' + name + '_KEY=sk-' + secrets.token_hex(24) + '\n')
p.chmod(0o600)
PY_KEYS
set -a
. "$LOCAL_STATE/gateway-restart.env"
set +a
sh scripts/tools/run-task.sh local:gateway:up > "$LOCAL_STATE/gateway.log" 2>&1
```

The default backend URLs route through Docker's `host.docker.internal` to 8000
and 8001. Specify `GATEWAY_REASONING_URL` / `GATEWAY_EMBED_URL` when that route
differs. Preserve the same gateway name, PostgreSQL volume and keys across
restarts; never set `GATEWAY_RESET_KEYS=1` during release verification.
The gateway handoff enables reranking. Source it and then apply the rehearsal's
explicit override in every consumer/probe shell:

```sh
. "$GATEWAY_ENV_FILE"
export RERANK_ENABLED=false
export DENSE_DIM=1024
```

The local/CI reasoning provider uses the narrowly scoped
[strict-finish adapter](../scripts/gateway/strict_finish.py). It rejects an
inferred successful finish when the underlying provider never supplied a finish
reason. It does not add an application dependency or install anything on the
production model tier. The platform gateway needs equivalent failure behavior;
its raw stream must still satisfy the application finish and `[DONE]` checks.

<a id="tls-services"></a>
## 3. TLS registry and gateway front

Create local TLS material in a protected directory. The CA must have critical
`CA:TRUE` basic constraints and certificate-signing key usage. The server leaf
covers `rag-kind.test`, `localhost` and `127.0.0.1`. Mount only the leaf
certificate/key into the servers; never mount the CA private key. The complete
gateway `ca-bundle.crt` includes the ordinary trust roots plus the local CA and
is mounted through `GATEWAY_CA_CONFIGMAP` (key `ca-bundle.crt`); the deployment
sets `SSL_CERT_FILE` for agent and ingest. Keep an independent wrong CA and a
hostname absent from the leaf SAN for the negative checks. For a new local PKI
(never overwrite an existing rehearsal identity):

```sh
umask 077
mkdir -p "$LOCAL_STATE/pki" "$LOCAL_STATE/registry-data"
openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
  -subj '/CN=Local Kind CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash' \
  -keyout "$LOCAL_STATE/pki/ca.key" -out "$LOCAL_STATE/pki/ca.crt"
openssl req -new -newkey rsa:3072 -nodes -subj '/CN=rag-kind.test' \
  -keyout "$LOCAL_STATE/pki/server.key" -out "$LOCAL_STATE/pki/server.csr"
cat > "$LOCAL_STATE/pki/server.ext" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:rag-kind.test,DNS:localhost,IP:127.0.0.1
EOF
openssl x509 -req -days 365 -in "$LOCAL_STATE/pki/server.csr" \
  -CA "$LOCAL_STATE/pki/ca.crt" -CAkey "$LOCAL_STATE/pki/ca.key" \
  -CAcreateserial -extfile "$LOCAL_STATE/pki/server.ext" \
  -out "$LOCAL_STATE/pki/server.crt"
cat /etc/ssl/certs/ca-certificates.crt "$LOCAL_STATE/pki/ca.crt" \
  > "$LOCAL_STATE/pki/ca-bundle.crt"
openssl verify -CAfile "$LOCAL_STATE/pki/ca.crt" "$LOCAL_STATE/pki/server.crt"
```

Prompt for the registry password; do not put it in a shell argument or Git:

```sh
export LOCAL_REGISTRY_USER=kind-loader
htpasswd -B -c "$LOCAL_STATE/htpasswd" "$LOCAL_REGISTRY_USER"
cat > "$LOCAL_STATE/registry.yaml" <<'EOF'
version: 0.1
log: {level: warn, formatter: json}
storage:
  filesystem: {rootdirectory: /var/lib/registry}
http:
  addr: :5000
  tls: {certificate: /pki/server.crt, key: /pki/server.key}
auth:
  htpasswd: {realm: Local Kind release registry, path: /auth/htpasswd}
EOF
docker run -d --name rag-kind-registry --memory 256m \
  -p 127.0.0.1:5443:5000 \
  -v "$LOCAL_STATE/registry-data:/var/lib/registry" \
  -v "$LOCAL_STATE/registry.yaml:/etc/docker/registry/config.yml:ro" \
  -v "$LOCAL_STATE/htpasswd:/auth/htpasswd:ro" \
  -v "$LOCAL_STATE/pki/server.crt:/pki/server.crt:ro" \
  -v "$LOCAL_STATE/pki/server.key:/pki/server.key:ro" \
  registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
```

Create the gateway TLS front without shell expansion of Nginx variables. It joins
the gateway's Docker network and resolves the gateway dynamically through Docker
DNS, so recreating the gateway does not leave a stale upstream address:

```sh
cat > "$LOCAL_STATE/nginx.conf" <<'EOF'
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 128; }
http {
  access_log off;
  resolver 127.0.0.11 valid=10s ipv6=off;
  server {
    listen 8444 ssl;
    server_name rag-kind.test localhost;
    ssl_certificate /pki/server.crt;
    ssl_certificate_key /pki/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / {
      set $gateway_backend rag-kind-gateway:4000;
      proxy_pass http://$gateway_backend;
      proxy_http_version 1.1;
      proxy_set_header Connection "";
      proxy_set_header Host $host;
      proxy_buffering off;
      proxy_request_buffering off;
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
    }
  }
}
EOF
docker run -d --name rag-kind-gateway-tls --entrypoint nginx \
  --user 1000:1000 --read-only --cap-drop ALL \
  --security-opt no-new-privileges --memory 128m \
  --tmpfs /tmp:uid=1000,gid=1000 --tmpfs /var/cache/nginx:uid=1000,gid=1000 \
  --network rag-kind-models -p 127.0.0.1:8444:8444 \
  -v "$LOCAL_STATE/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$LOCAL_STATE/pki/server.crt:/pki/server.crt:ro" \
  -v "$LOCAL_STATE/pki/server.key:/pki/server.key:ro" \
  nginx:1.30.4-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c \
  -g 'daemon off;'
docker exec rag-kind-gateway-tls nginx -t
```

Here 1000 is the host owner's ID, not a pod identity; adjust ownership and tmpfs
IDs together on a different installation. The proxy's long I/O ceiling does not
extend the application's request deadlines. In the consumer shell, after
sourcing the gateway handoff:

```sh
export RERANK_ENABLED=false DENSE_DIM=1024
export LLM_BASE_URL=https://localhost:8444/v1
export EMBED_BASE_URL=https://localhost:8444/v1
export SSL_CERT_FILE="$LOCAL_STATE/pki/ca-bundle.crt"
.venv/bin/python scripts/probe_gateway.py --require-reasoning --stream
```

Require anonymous `https://localhost:5443/v2/` to return 401 with the correct CA.
Write the protected loader auth file `$LOCAL_STATE/registry-auth.json` (mode 0600)
with `auths["rag-kind.test:5000"]` and `auths["rag-kind.test:5443"]`, each holding
the base64-encoded `kind-loader:password` credential.

<a id="kind-cluster"></a>
## 4. Disposable Kind cluster

Use the checksum-pinned Kind, kubectl and Helm versions of the
`kind-live-rehearsal` job in [the passing workflow](../.github/workflows/e2e.yml):
Kind 0.32.0, kubectl 1.33.12, Helm 4.3.0 and node image
`kindest/node:v1.33.12@sha256:3f5c8443c620245e4d355cfe09e96a91ead32ceaa569d3f1ca9edf0cb2fe2ff4`.
The Open WebUI pass needs this node: its UID stand-in is a Kubernetes 1.33 alpha
API (`kind-openwebui-operator` enables the same feature gate). Install them in a
task-local `KIND_BIN`, leaving existing developer binaries alone, and verify
their in-repository SHA256 values before invoking them:

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
```

Every Kind command uses this private kubeconfig; do not switch the user's default
context. Attach the two TLS services to Kind's standard Docker network, preserving
their existing connections. Create the `kind` network with Docker's bridge driver
only if it does not already exist:

```sh
docker network inspect kind
docker network connect --alias rag-kind.test kind rag-kind-registry
docker network connect kind rag-kind-gateway-tls
export KIND_GATEWAY_IP="$(docker inspect -f '{{(index .NetworkSettings.Networks "kind").IPAddress}}' rag-kind-gateway-tls)"
```

Use `INTERNAL_REGISTRY=rag-kind.test:5000`. The loader runs on Docker's `kind`
network and reaches the registry's internal TLS port. The node resolves
`rag-kind.test` through Docker DNS to the registry alias; application pods resolve
the same hostname through CoreDNS to the **gateway** address on 8444. Test both
DNS paths; sharing a hostname does not prove them. Set `KIND_ARTIFACT_ROOT` to the
fresh extracted candidate directory and create a pinned Skopeo adapter:

```sh
export KIND_ARTIFACT_ROOT=/absolute/path/to/fresh-kind-candidate
cp "$LOCAL_STATE/registry-auth.json" "$KIND_STATE/registry-auth.json"
mkdir -p "$LOCAL_STATE/registry-trust"
cp "$LOCAL_STATE/pki/ca.crt" "$LOCAL_STATE/registry-trust/ca.crt"
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
export REGISTRY_AUTH_FILE="$KIND_STATE/registry-auth.json"
```

Keep `INSECURE_REGISTRY=false` and `SKOPEO_ARGS='--dest-cert-dir /certs'`. When a
host path contains a colon, use Docker's `--mount type=bind,src=...,dst=...,readonly`
instead of the colon-delimited `-v` form. Create the cluster with node trust for
the registry and, as `kind-openwebui-operator` does, the alpha
`MutatingAdmissionPolicy` API for the restricted-v2 UID stand-in:

```sh
mkdir -p "$KIND_STATE/node-trust/rag-kind.test:5000"
cp "$LOCAL_STATE/pki/ca.crt" "$KIND_STATE/node-trust/rag-kind.test:5000/ca.crt"
cat > "$KIND_STATE/node-trust/rag-kind.test:5000/hosts.toml" <<'EOF_HOSTS'
server = "https://rag-kind.test:5000"
[host."https://rag-kind.test:5000"]
  capabilities = ["pull", "resolve"]
  ca = "/etc/containerd/certs.d/rag-kind.test:5000/ca.crt"
EOF_HOSTS
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['KIND_STATE'])
config = {'kind':'Cluster', 'apiVersion':'kind.x-k8s.io/v1alpha4',
          'nodes':[{'role':'control-plane', 'extraMounts':[
              {'hostPath':str(root/'node-trust'),
               'containerPath':'/etc/containerd/certs.d', 'readOnly':True}]}],
          'featureGates':{'MutatingAdmissionPolicy':True},
          'runtimeConfig':{'admissionregistration.k8s.io/v1alpha1':'true'}}
(root/'kind-config.json').write_text(json.dumps(config))
PY
"$KIND_BIN/kind" create cluster --name "$KIND_NAME" \
  --config "$KIND_STATE/kind-config.json" --kubeconfig "$KUBECONFIG" \
  --image kindest/node:v1.33.12@sha256:3f5c8443c620245e4d355cfe09e96a91ead32ceaa569d3f1ca9edf0cb2fe2ff4
chmod 600 "$KUBECONFIG"
kubectl get nodes
kubectl get storageclass standard
```

Save CoreDNS's original configuration, then map the gateway hostname in this
cluster only. The script refuses an existing hosts block for operator review
instead of overwriting it:

```sh
kubectl -n kube-system get configmap coredns -o json > "$KIND_STATE/coredns-before.json"
python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
root = Path(os.environ['KIND_STATE'])
cm = json.loads((root/'coredns-before.json').read_text())
core = cm['data']['Corefile']
assert 'hosts {' not in core and '.:53 {' in core
entry = '.:53 {\n    hosts {\n        ' + os.environ['KIND_GATEWAY_IP'] + ' rag-kind.test\n        fallthrough\n    }'
cm['data']['Corefile'] = core.replace('.:53 {', entry, 1)
subprocess.run(['kubectl','apply','-f','-'], input=json.dumps(cm), text=True, check=True)
PY
kubectl -n kube-system rollout restart deployment/coredns
kubectl -n kube-system rollout status deployment/coredns --timeout=180s
```

<a id="deploy-candidate"></a>
## 5. Deploy the candidate

Start from a green published-main bundle, its outer checksum and the
independently trusted public signing key. Use the
[trusted bootstrap](install_and_ops.md#42-transfer--automated-bootstrap) in a fresh
directory and run deployment commands from its bootstrapped checkout; never
repack from it. Record the original tar SHA256, full commit, archive/registry
image identities, model revisions, configuration hashes and both lanes'
evidence locations.

Create the namespace and file-based inputs from the gateway handoff without
printing keys or passing them as command arguments. All four referenced key
names must exist:

```sh
export IMAGE_SHA="$(git rev-parse HEAD)"
kubectl create namespace "$KIND_NAMESPACE"
kubectl -n "$KIND_NAMESPACE" create secret generic rag-kind-registry-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$REGISTRY_AUTH_FILE"
kubectl -n "$KIND_NAMESPACE" create configmap rag-kind-gateway-ca \
  --from-file=ca-bundle.crt="$LOCAL_STATE/pki/ca-bundle.crt"
. "$GATEWAY_ENV_FILE"
mkdir -p "$KIND_STATE/secret-inputs"
python3 - <<'PY_SECRET'
import os
from pathlib import Path
root = Path(os.environ['KIND_STATE']) / 'secret-inputs'
for filename, variable in {'llm-api-key':'LLM_API_KEY',
                          'embed-api-key':'EMBED_API_KEY',
                          'rerank-api-key':'RERANK_API_KEY',
                          'context-llm-api-key':'LLM_API_KEY'}.items():
    p = root / filename
    p.write_text(os.environ[variable]); p.chmod(0o600)
PY_SECRET
kubectl -n "$KIND_NAMESPACE" create secret generic rag-kind-gateway-keys \
  --from-file="$KIND_STATE/secret-inputs/llm-api-key" \
  --from-file="$KIND_STATE/secret-inputs/embed-api-key" \
  --from-file="$KIND_STATE/secret-inputs/rerank-api-key" \
  --from-file="$KIND_STATE/secret-inputs/context-llm-api-key"
```

Tracing is on by default with the bundled OpenObserve, so create its
credentials Secret too (`deploy.sh` checks its four keys and the password
rule before any change). Values go through files in a private directory:

```sh
OO="$KIND_STATE/openobserve-inputs"
mkdir -p "$OO" && chmod 700 "$OO"
printf 'gate@openobserve.invalid' > "$OO/root-user-email"
{ od -An -N24 -tx1 /dev/urandom | tr -d ' \n'; printf 'Aa1!'; } > "$OO/root-user-password"
OO_B64="$(printf '%s:%s' "$(cat "$OO/root-user-email")" "$(cat "$OO/root-user-password")" | base64 | tr -d '\n')"
printf 'Authorization=Basic %s' "$OO_B64" > "$OO/otlp-headers"
printf 'Basic %s' "$OO_B64" > "$OO/metrics-auth"
unset OO_B64
kubectl -n "$KIND_NAMESPACE" create secret generic openobserve-credentials \
  --from-file=root-user-email="$OO/root-user-email" \
  --from-file=root-user-password="$OO/root-user-password" \
  --from-file=otlp-headers="$OO/otlp-headers" \
  --from-file=metrics-auth="$OO/metrics-auth"
```

<a id="openwebui-stand-ins"></a>
Open WebUI runs with the same stand-ins and script as `kind-openwebui-operator`,
from the bootstrapped checkout, with private state in `$OWUI_STATE`. The script
touches only the named cluster. The chart renders `https://owui.localhost` with
no port, so the router stand-in is forwarded on loopback port 443; ports 443 and
5556 on 127.0.0.1 must be free. Record the current unprivileged-port floor, allow
443 for the rehearsal, and copy the test IdP and router images into the node. The
Open WebUI and proxy images arrive later from the bundle through `airgap:load`:

```sh
export APP_NS="$KIND_NAMESPACE" OWUI_STATE="$KIND_STATE/owui-acceptance"
OWUI="sh scripts/openwebui/acceptance.sh operator"
sysctl -n net.ipv4.ip_unprivileged_port_start > "$KIND_STATE/unprivileged-port-start.before"
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=443
docker pull "$(sed -n 's/^SKOPEO_IMAGE=//p' scripts/openwebui/pins.txt)"
SKOPEO_NATIVE=0 $OWUI images
```

`SKOPEO_NATIVE=0` makes the script use its pinned Skopeo image with its own
state mount, not the registry adapter above.

No console OAuth cookie or agent Route is needed in Kind. Save the sizing files:
Qdrant `$KIND_STATE/qdrant-sizing.yaml` (the volume group is a Kind-only override;
OpenShift keeps project-assigned identities and must not receive this fixed ID):

```yaml
replicaCount: 1
podSecurityContext: {fsGroup: 3000}
resources:
  requests: {cpu: 200m, memory: 256Mi}
  limits: {cpu: "2", memory: 2Gi}
```

and the ingest Job strategic merge patch `$KIND_STATE/ingest-sizing.json`:

```json
{"spec":{"template":{"spec":{"containers":[{"name":"ingest","resources":{"requests":{"cpu":"500m","memory":"1Gi"},"limits":{"cpu":"2","memory":"2Gi"}}}]}}}}
```

Create the env file. No key values go in it. It selects the one-node 1/1/1
collection policy and pins a non-root id for the bundled OpenObserve (OpenShift
assigns one). Open WebUI stays on, its default; the next block adds its site
keys for the test IdP:

```sh
cat > "$KIND_STATE/airgap.env" <<EOF
INTERNAL_REGISTRY=rag-kind.test:5000
NAMESPACE=$KIND_NAMESPACE
STORAGE_CLASS=standard
IMAGE_SHA=$IMAGE_SHA
EMBED_BASE_URL=https://rag-kind.test:8444/v1
LLM_BASE_URL=https://rag-kind.test:8444/v1
EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B
LLM_MODEL_REASONING=google/gemma-4-E4B-it-qat-mobile-ct
DENSE_DIM=1024
EMBED_MODEL_REVISION=local:Qwen/Qwen3-Embedding-0.6B
RERANK_ENABLED=false
AGENT_ROUTE=false
PULL_SECRET=rag-kind-registry-pull
GATEWAY_API_KEY_SECRET=rag-kind-gateway-keys
GATEWAY_CA_CONFIGMAP=rag-kind-gateway-ca
CORPUS_PVC=rag-kind-synthetic-corpus
QDRANT_STORAGE_SIZE=1Gi
INGEST_WORK_SIZE=1Gi
INGEST_WORKERS=1
QDRANT_EXTRA_VALUES=$KIND_STATE/qdrant-sizing.yaml
INGEST_EXTRA_PATCH=$KIND_STATE/ingest-sizing.json
INSECURE_REGISTRY=false
SKOPEO_ARGS='--dest-cert-dir /certs'
QDRANT_SHARD_NUMBER=1
QDRANT_REPLICATION_FACTOR=1
QDRANT_WRITE_CONSISTENCY_FACTOR=1
OPENOBSERVE_RUN_AS_ID=10001
EOF
export AIRGAP_ENV="$KIND_STATE/airgap.env"
export SNEAKERNET_TRUSTED_PUB=/absolute/path/to/independently-trusted/signing.pub
```

Every live stage below needs that anchor: outside dry-run, validate, load,
deploy, ingest and retire-legacy refuse a checkout that is not a verified signed release
([deploy §4](deploy.md#4-signing-and-provenance)). Never set the connected
opt-out `AIRGAP_UNVERIFIED_CHECKOUT` for this gate.

Append the `OPENWEBUI_*` site keys the script prints for its test IdP (the
`airgap.env.example` keys, as `kind-openwebui-operator` writes them):

```sh
$OWUI site-env >> "$KIND_STATE/airgap.env"
grep -qx 'OPENWEBUI_ENABLED=true' "$KIND_STATE/airgap.env"
```

Then apply the OpenShift stand-ins (Route API, namespace service CA, serving
certificate, restricted-v2 UID), the test IdP and the operator Secret, as site
procedure step 2 ([deploy.md](deploy.md#docs-front-end)) does on OpenShift:

```sh
export OWUI_WORKSPACE="$PWD"
$OWUI prepare
```

Run pipeline commands in a clean operator shell: environment values override
`AIRGAP_ENV`, and the host gateway handoff sets localhost URLs and plaintext keys
that must not override the pod URLs and Secret references. The two sizing
overrides above are CI-rehearsal inputs, accepted only with `CI_REHEARSAL=1` in
the process environment (this disposable lab only, never the air gap):

```sh
unset LLM_BASE_URL EMBED_BASE_URL RERANK_BASE_URL CONTEXT_LLM_BASE_URL
unset LLM_API_KEY EMBED_API_KEY RERANK_API_KEY CONTEXT_LLM_API_KEY
export CI_REHEARSAL=1
sh scripts/tools/run-task.sh airgap:validate
sh scripts/tools/run-task.sh airgap:load
```

<a id="synthetic-corpus"></a>
Create the synthetic corpus with the **loaded candidate ingest image**:

```sh
kubectl -n "$KIND_NAMESPACE" apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: rag-kind-synthetic-corpus}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources: {requests: {storage: 1Gi}}
---
apiVersion: batch/v1
kind: Job
metadata: {name: rag-kind-corpus-generator}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      imagePullSecrets: [{name: rag-kind-registry-pull}]
      containers:
      - name: generator
        image: rag-kind.test:5000/qdrant-pdf-rag-ingest:$IMAGE_SHA
        command: [/bin/sh, -ec]
        args:
        - |
          python3 /app/scripts/make_synthetic_pdf.py --out /corpus/SA22-0000-00_outline.pdf
          python3 /app/scripts/make_synthetic_pdf.py --plain --out /corpus/plain-widget-notes.pdf
          id
          ls -l /corpus
        securityContext:
          runAsNonRoot: true
          allowPrivilegeEscalation: false
          capabilities: {drop: [ALL]}
          seccompProfile: {type: RuntimeDefault}
        resources:
          requests: {cpu: 100m, memory: 128Mi}
          limits: {cpu: "1", memory: 512Mi}
        volumeMounts: [{name: corpus, mountPath: /corpus}]
      volumes:
      - name: corpus
        persistentVolumeClaim: {claimName: rag-kind-synthetic-corpus}
EOF
kubectl -n "$KIND_NAMESPACE" wait --for=condition=complete job/rag-kind-corpus-generator --timeout=300s
kubectl -n "$KIND_NAMESPACE" logs job/rag-kind-corpus-generator
```

For a rerun delete only the generator Job, retaining the PVC. Before deploying
the application, prove the gateway settings from a temporary pod using the
loaded agent image and the same key/trust references:

```sh
kubectl -n "$KIND_NAMESPACE" apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata: {name: rag-kind-gateway-probe}
spec:
  restartPolicy: Never
  imagePullSecrets: [{name: rag-kind-registry-pull}]
  containers:
  - name: probe
    image: rag-kind.test:5000/qdrant-pdf-rag-agent:$IMAGE_SHA
    command: [python3, /app/scripts/probe_gateway.py, --require-reasoning, --stream]
    env:
    - {name: EMBED_MODE, value: vllm}
    - {name: EMBED_BASE_URL, value: 'https://rag-kind.test:8444/v1'}
    - {name: LLM_BASE_URL, value: 'https://rag-kind.test:8444/v1'}
    - {name: EMBED_MODEL, value: Qwen/Qwen3-Embedding-0.6B}
    - {name: LLM_MODEL_REASONING, value: google/gemma-4-E4B-it-qat-mobile-ct}
    - {name: DENSE_DIM, value: '1024'}
    - {name: RERANK_ENABLED, value: 'false'}
    - {name: SSL_CERT_FILE, value: /etc/gateway-ca/ca-bundle.crt}
    - name: EMBED_API_KEY
      valueFrom: {secretKeyRef: {name: rag-kind-gateway-keys, key: embed-api-key}}
    - name: LLM_API_KEY
      valueFrom: {secretKeyRef: {name: rag-kind-gateway-keys, key: llm-api-key}}
    securityContext:
      runAsNonRoot: true
      allowPrivilegeEscalation: false
      capabilities: {drop: [ALL]}
      seccompProfile: {type: RuntimeDefault}
    resources:
      requests: {cpu: 100m, memory: 128Mi}
      limits: {cpu: '1', memory: 512Mi}
    volumeMounts: [{name: gateway-ca, mountPath: /etc/gateway-ca, readOnly: true}]
  volumes:
  - name: gateway-ca
    configMap: {name: rag-kind-gateway-ca}
EOF
kubectl -n "$KIND_NAMESPACE" wait --for=jsonpath='{.status.phase}'=Succeeded pod/rag-kind-gateway-probe --timeout=600s
kubectl -n "$KIND_NAMESPACE" logs rag-kind-gateway-probe
kubectl -n "$KIND_NAMESPACE" delete pod rag-kind-gateway-probe
```

Then run the full pipeline and the application contracts. Run the gateway probe
to completion before starting the contract client: with one active reasoning
sequence an overlapping diagnostic can queue behind a long answer and hit its
unchanged timeout. Retain such failures and rerun under single-user conditions;
do not increase deadlines. The first pass verifies the Open WebUI Route and
installs no Pipe yet (site step 3):

```sh
set -o pipefail
sh scripts/tools/run-task.sh airgap:pipeline 2>&1 | tee "$OWUI_STATE/logs/pipeline-first-pass.log"
grep -q '==> Open WebUI front end on: ' "$OWUI_STATE/logs/pipeline-first-pass.log"
grep -q '==> Route rag-openwebui verified: ' "$OWUI_STATE/logs/pipeline-first-pass.log"
grep -q 'docs Pipe NOT installed' "$OWUI_STATE/logs/pipeline-first-pass.log"
kubectl -n "$KIND_NAMESPACE" exec deploy/rag-agent -- \
  python3 /app/scripts/probe_gateway.py --require-reasoning --stream
kubectl -n "$KIND_NAMESPACE" exec -i deploy/rag-agent -- python3 - \
  < scripts/ci/application_contracts.py
```

Save stage exit statuses. Empty search or skipped ingest/tracing is a failed
release check even if the script exits successfully. Do not run the
fault-injection helper against the real model deployment; CI supplies that
coverage against the same bundle.

<a id="openwebui-pass"></a>
### Open WebUI with the real models

The steps and checks of `kind-openwebui-operator`, in its order, with the real
models answering. `admin` serves the stored Route from the router stand-in, proves
no Pipe exists yet, signs in as the IdP admin and adds its API key to the Secret
as `admin-api-key` (site step 4). The second deploy installs the Pipe. `run`
grants the model to the groups (site step 5) and runs the 20 checks:

```sh
$OWUI admin
sh scripts/tools/run-task.sh airgap:deploy 2>&1 | tee "$OWUI_STATE/logs/deploy-second-pass.log"
! grep -q 'docs Pipe NOT installed' "$OWUI_STATE/logs/deploy-second-pass.log"
$OWUI run
```

Rollback K1 by one flag, then the next ordinary use on the kept claim:

```sh
$OWUI record
sed -i 's|^OPENWEBUI_ENABLED=.*|OPENWEBUI_ENABLED=false|' "$AIRGAP_ENV"
sh scripts/tools/run-task.sh airgap:deploy 2>&1 | tee "$OWUI_STATE/logs/deploy-off.log"
grep -q '==> Open WebUI front end off ' "$OWUI_STATE/logs/deploy-off.log"
$OWUI off
sed -i 's|^OPENWEBUI_ENABLED=.*|OPENWEBUI_ENABLED=true|' "$AIRGAP_ENV"
sh scripts/tools/run-task.sh airgap:deploy 2>&1 | tee "$OWUI_STATE/logs/deploy-on.log"
! grep -q 'docs Pipe NOT installed' "$OWUI_STATE/logs/deploy-on.log"
$OWUI on
```

Each step prints its verdicts and keeps them in `$OWUI_STATE/logs/*.json` (ids,
counts and seconds, never text, passwords or keys). Every verdict must be
`"ok": true`. The CI-only `paced` phase is not run here: it needs the mock model's
pacing. The real models' timing is read from the answer verdicts instead (see
[the gate](#gate)).

<a id="kind-acceptance"></a>
## 6. Acceptance

| Check | Required evidence |
|---|---|
| Images | Requested image refs and each running container's `imageID` match loaded release artifacts. Account for manifest-list/platform and archive/registry compression differences using [the image-identity procedure](deploy.md#image-identity-across-archive-and-registry-formats). |
| Storage writes | Bound PVCs, successful ingest scratch/data writes, Qdrant snapshot creation and OpenObserve span persistence. |
| Pod replacement | Replace one Qdrant pod and one agent pod, recording old/new UIDs. PVC identity, exact point count, representative hit IDs/citations and health survive. Restart OpenObserve and verify an earlier trace remains queryable. |
| Repeat deployment | Repeat `sh scripts/tools/run-task.sh airgap:pipeline` on the same bundle/env; require rollout, new ingest Job completion, unchanged synthetic point count/IDs, healthy search and tracing. |
| Snapshot recovery | Snapshot only the synthetic collection, export and checksum it outside Git, restore into an isolated empty collection with the same Qdrant version and vector config, and verify exact count, sampled payload/point IDs and equivalent queries. Never commit snapshot bytes. |
| Cited answer | Ask `What does IEA500I mean?`; verify the answer and non-inferred citation against the generated PDF, with no error/degraded response. A transport-only success is insufficient. |
| Follow-up | A contextual follow-up completes coherently with a valid synthetic citation. Record the condensation setting; do not flip its default. |
| Streams | `/v1/answer`: exactly one successful terminal `final`, no `error`. `/v1/chat` and `/v1/chat/completions`: successful finish chunk plus `[DONE]`, no error frame. `/ui`: terminal completion and cleared busy state. |
| Tracing | Search and answer/chat traces appear in OpenObserve with retrieval and reasoning children where applicable. |
| TLS, credentials, long input | The controls below pass from both the agent and ingest images. |
| Open WebUI (real models) | [The pass above](#openwebui-pass): `first-pass`, `checks` (20 verdicts), `record`, `rollback-off` and `rollback-on` all `ok`. No answer verdict has `end_s` or `done_s` above 240 s, the end of the CI envelope. |

For persistence and redeployment run
`sh scripts/ci/check_lifecycle.sh "$KIND_NAMESPACE"` and then the pipeline again,
comparing point IDs/counts, credential fingerprints, PVC identities and old-trace
recovery. Access the console privately with
`kubectl -n "$KIND_NAMESPACE" port-forward svc/rag-agent 8080:8080`.

<a id="tls-and-long-input-controls"></a>
### TLS, credential and long-input controls

This check uses the actual application's Settings, Secret-backed key and CA mount.
It launches a fresh client process per CA choice so trust cannot be retained from
the positive case. The diagnostic timeout is 10 seconds; it does not change
application request deadlines. Run it while the interactive client is idle:

```sh
export TEST_NAMESPACE="$KIND_NAMESPACE"
kubectl -n "$TEST_NAMESPACE" exec -i deploy/rag-agent -c agent -- python3 - <<'PY_TLS'
import json,os,socket,subprocess,sys,urllib.parse
from mainframe_rag.config import load_settings
s=load_settings()
base=s.embed_base_url.rstrip('/')
url=base+'/models'
parts=urllib.parse.urlsplit(url)
ip=socket.gethostbyname(parts.hostname)
wrong_hostname=urllib.parse.urlunsplit((parts.scheme,ip+(':'+str(parts.port) if parts.port else ''),parts.path,'',''))
child="""import os,json,httpx2
headers={'Authorization':'Bearer '+os.environ['PROBE_KEY']} if os.environ.get('PROBE_KEY') else {}
try:
 with httpx2.Client(timeout=10) as c:
  r=c.get(os.environ['PROBE_URL'],headers=headers)
  print(json.dumps({'status':r.status_code}))
except httpx2.HTTPError as e:
 print(json.dumps({'tls_error':any(w in str(e).lower() for w in ('certificate','ssl','tls'))}))
"""
rows=[]
for name,endpoint,key,authority,expected in [
 ('correct',url,s.embed_api_key,os.environ['SSL_CERT_FILE'],200),
 ('missing-key',url,'',os.environ['SSL_CERT_FILE'],401),
 ('wrong-key',url,'sk-invalid',os.environ['SSL_CERT_FILE'],401),
 ('wrong-ca',url,s.embed_api_key,'/var/run/secrets/kubernetes.io/serviceaccount/ca.crt','tls'),
 ('wrong-hostname',wrong_hostname,s.embed_api_key,os.environ['SSL_CERT_FILE'],'tls'),
]:
 r=subprocess.run([sys.executable,'-c',child],env={**os.environ,'PROBE_URL':endpoint,'PROBE_KEY':key,'SSL_CERT_FILE':authority},capture_output=True,text=True,check=True)
 result=json.loads(r.stdout);rows.append({'case':name,**result})
 assert result.get('tls_error') is True if expected=='tls' else result.get('status')==expected
print(json.dumps({'cases':rows,'passed':True}))
PY_TLS
```

Require correct CA/key to return 200, missing/wrong key 401, and both certificate
controls to fail TLS. For ingest, clone the rendered ingest Job into a uniquely
named diagnostic Job, preserving its image, environment, CA/Secret references and
volume/security settings, and replace only its command with the same Python
diagnostic. Remove server-generated metadata/selectors, set `backoffLimit: 0`,
wait for completion, retain logs and imageID, then delete only that Job.

For the pinned Qwen tokenizer, the following original input measured 3994 tokens.
Check the actual count at the backend's diagnostic `/tokenize` route before
posting the embedding through the application pod's authenticated gateway:

```sh
python3 - <<'PY_LONG'
import json, subprocess, os, urllib.request
text = ('//SYNTH001 EXEC PGM=SYNTHAPP\n'
        '//INPUT DD DSN=TEST.ORIGINAL.DATA,DISP=SHR\n') * 121
request = urllib.request.Request('http://127.0.0.1:8001/tokenize',
    data=json.dumps({'model':'Qwen/Qwen3-Embedding-0.6B','prompt':text}).encode(),
    headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request, timeout=10) as response:
    tokens = json.load(response)['count']
assert 3900 <= tokens < 4096, tokens
code = 'INPUT_TEXT=' + repr(text) + '\n' + '''
import httpx2
from mainframe_rag.config import load_settings, bearer_auth_headers
s = load_settings()
with httpx2.Client(timeout=s.embed_timeout_s) as client:
    r = client.post(s.embed_base_url.rstrip('/') + '/embeddings',
        headers=bearer_auth_headers(s.embed_api_key),
        json={'model':s.embed_model,'input':[INPUT_TEXT]})
    r.raise_for_status()
    assert len(r.json()['data'][0]['embedding']) == 1024
    print('Long embedding passed, dimension=1024')
'''
subprocess.run(['kubectl','-n',os.environ['TEST_NAMESPACE'],'exec','-i',
                'deploy/rag-agent','-c','agent','--','python3','-'],
               input=code, text=True, check=True)
print('token_count=', tokens)
PY_LONG
```

This input is synthetic. Representative RAG prompts and explicit citations are
checked separately by `application_contracts.py` and the browser exercise. The
production gateway uses its owner's model/tokenizer and dimension; do not
copy these local constants into production settings.

<a id="promotion"></a>
## 7. Cleanup and promotion

After recording results, stop the private port-forward and export only the
synthetic recovery evidence the record needs. Remove only the disposable
cluster and its two added network connections:

```sh
"$KIND_BIN/kind" delete cluster --name "$KIND_NAME" --kubeconfig "$KUBECONFIG"
docker network disconnect kind rag-kind-registry
docker network disconnect kind rag-kind-gateway-tls
sudo sysctl -w net.ipv4.ip_unprivileged_port_start="$(cat "$KIND_STATE/unprivileged-port-start.before")"
```

The TLS services keep their original connections and loopback ports, and the
registry, model files, TLS material and gateway key store remain for the next
rehearsal. Never delete another cluster or prune its volumes.

Promotion requires every required row in [the release record](release-record.md)
to be `PASS`, evidence attributable to this bundle, and named operator sign-off.
`NOT RUN`, `BLOCKED`, `FAIL`, missing artifacts or stale results prohibit
transfer. This is an operator-enforced gate; the pack/load scripts do not read the
record. Transfer **the identical tested tarball and checksum**, plus its record,
through the approved channel. Do not rebuild, repack, edit or substitute an image.
A release that ships a dev kit publishes the record's dev-kit line with the
record before the kit is transferred
([record template](release-record.md#sign-off-and-handoff)).
Recheck the checksum on arrival, then use the bootstrap, validation,
deployment pipeline, [site OpenShift acceptance](install_and_ops.md#site-openshift-acceptance)
and production acceptance commands. A bundle or security/configuration fix
requires a new candidate and a fresh gate.

Before claiming production compatibility, obtain the OpenShift
version, storage driver/topology, SCC/RBAC policy, identity integration, registry
trust and network restrictions, and record each difference with its acceptance
owner. Local success establishes neither production capacity nor multi-node
resilience, enterprise identity behavior or general answer quality.
