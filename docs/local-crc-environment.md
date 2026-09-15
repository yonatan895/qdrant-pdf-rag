# Reproduce the Windows/WSL CRC environment

This file owns the local machine setup. The release gate and evidence requirements
are in [crc-release-verification.md](crc-release-verification.md); production
installation uses [install_and_ops.md](install_and_ops.md#4-standard-deployment-architecture-air-gap-production--local-cluster-testing).
Run shell blocks in order, stop on any nonzero exit, and use a separate terminal
for each foreground service. Set `umask 077` in operator shells. Keep private
state outside both Git and the extracted candidate; save only sanitized evidence.
Local addresses, certificates, model choices and resource overrides below belong
to the rehearsal. Production receives its model endpoints and identities from the
platform team.

The simultaneous-fit attempt did not meet all acceptance criteria. The 32-minute
live workload passed, but cold-start headroom failed at 12GiB CRC and scheduler
reservations blocked ingest at 10.5GiB. The approved operating strategy is the
[complementary fallback](local-release-fallback.md). The steps below preserve the live
attempt's reproducible configuration and the distinction between its workload
success and failed fit qualification.

## 1. Topology and prerequisites

Run the product images, Qdrant, ingest, two agent replicas with OAuth sidecars,
and persistent Jaeger in Windows CRC. Run the two GPU models, the real LiteLLM
process and PostgreSQL in WSL. An authenticated HTTPS gateway and a separate
TLS/authenticated registry connect these environments. Keep the preserved Kind
cluster stopped and its registry, volumes, corpus and backups intact.

| Component | Rehearsal configuration |
|---|---|
| Host | Windows plus Ubuntu 24.04 WSL; 32 GiB RAM and 8 GiB VRAM |
| CRC | 2.63.0, OpenShift 4.22.7; 8 vCPUs, 12288 MiB, 50 GiB virtual disk |
| WSL | 12 GB ceiling, 8 processors, 2 GB swap, page reporting, gradual cache reclamation |
| Python | CPython 3.14 GIL; repository lockfile |
| Models | Gemma reasoning and Qwen embeddings; 4096-token windows, dimension 1024 |
| Local serving | `LOCAL_CRC_32GB`; eager execution, one sequence per model, GPU shares 0.54 / 0.43 |
| Reranking | Explicitly disabled for this rehearsal |
| Registry / HTTPS gateway | Separate loopback listeners on 5443 / 8444; backend gateway on 4000 |
| Tools | Repository-pinned Linux tools; Windows CRC `oc` and checksum-verified Windows Helm 3.19.0 |

Install Docker with WSL integration and NVIDIA GPU support. Verify `docker info`
and `nvidia-smi` before downloading models. Install Git, Make, OpenSSL, a supported
Python 3.14 interpreter, and the repository's pinned deployment tools. On the
connected clone, run `make venv` and `make bm25-weights`. Save model access tokens
outside Git; gated model access must already be approved by the model publisher.

For ordinary development without CRC, the complete simulation entrypoint remains
`RERANK_ENABLED=false make local-stack`, after starting both model backends.
Do not run a second host agent/Qdrant/Jaeger stack alongside the CRC product pods.
[The local stack owner](live-stack.md) describes its lifecycle.

## 2. Prepare Windows and WSL without losing the existing environment

Inventory Docker containers, mounted volumes, original Kind node names, corpus,
Qdrant collections and verified snapshot backups. Stop only the recorded Kind
node containers. Do not delete the cluster or prune Docker volumes.

Back up `%UserProfile%\.wslconfig`, preserve unrelated options, and set:

```ini
[wsl2]
memory=12GB
processors=8
swap=2GB
pageReporting=true

[experimental]
autoMemoryReclaim=gradual
```

This is the Windows file, not `/etc/wsl.conf`. The latter may enable systemd but
does not own WSL's VM memory ceiling. Save a resume checkpoint and stop active
work before the operator runs `wsl --shutdown`. Restart the distribution and
verify `free -m`, `docker info`, and `nvidia-smi`. A memory ceiling is not a
reservation.

Recover disk headroom before downloading/extracting another bundle. Inventory
archives first; retain the current candidate and last successful candidate.
Delete only confirmed reproducible duplicates. Preserve model caches, corpus,
snapshots, credentials and CRC instance files. Deleting inside WSL does not
necessarily shrink its Windows VHD; compact only during a separately controlled,
offline shutdown.

Complete CRC installation, checksum verification, Windows reboot if required,
and `crc setup` using [the pinned installation procedure](crc-release-verification.md#2-install-the-pinned-windows-crc-release).
Configure `host-network-access=true`. Keep CRC stopped while starting models.

## 3. Pin and start the two model servers sequentially

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
links inside the container. On the same WSL filesystem, create a separate view
whose files are hardlinks to the resolved cached files:

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
set `REASONING_VIEW` and `EMBED_VIEW` to those absolute paths.

In separate terminals, start reasoning first, wait for its `/health` to succeed,
then start embedding. Keep the foreground launcher sessions alive:

```sh
# Terminal 1, from the connected development clone:
make local-vllm \
  MODEL="$REASONING_VIEW" SERVED_NAME=google/gemma-4-E4B-it-qat-mobile-ct \
  PORT=8000 BUDGET_PROFILE=LOCAL_CRC_32GB \
  VLLM_IMAGE=vllm/vllm-openai@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14

# Terminal 2, only after reasoning is healthy:
make local-vllm-embed \
  MODEL="$EMBED_VIEW" SERVED_NAME=Qwen/Qwen3-Embedding-0.6B \
  PORT=8001 BUDGET_PROFILE=LOCAL_CRC_32GB \
  VLLM_IMAGE=vllm/vllm-openai@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14
```

Verify startup logs show eager execution and one sequence for both models,
text-only reasoning with zero multimodal processor cache, and explicit embedding
prefix-cache/chunked-prefill disables. Reasoning prefix caching remains enabled.
These flags do not prove zero KV allocation; record actual RAM and VRAM.
Do not shorten either context window or change model IDs to make the test fit.

Direct backend `/health` and `/tokenize` calls are serving diagnostics only.
Agent and ingest requests always go through the real gateway.

## 4. Start and preserve the gateway

Use [run_local_gateway.sh](../scripts/run_local_gateway.sh), which owns the pinned
LiteLLM/PostgreSQL containers, database volume and generated configuration. Keep
its handoff and startup log in a private directory: the startup output can contain
credentials. Use a private PostgreSQL password on initial provisioning and reuse
it with the same database volume.

```sh
umask 077
export LOCAL_STATE="$HOME/.config/mainframe-rag/local-crc"
mkdir -p "$LOCAL_STATE"
export GATEWAY_NAME=crc-litellm-gateway
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
make local-gateway > "$LOCAL_STATE/gateway.log" 2>&1
```

The default backend URLs route through Docker's `host.docker.internal` to 8000
and 8001. Specify `GATEWAY_REASONING_URL` / `GATEWAY_EMBED_URL` when that route
differs. Preserve the same gateway name, PostgreSQL volume and keys across
restarts; never set `GATEWAY_RESET_KEYS=1` during release verification.

The gateway handoff currently enables reranking. Source it and then apply the
rehearsal's explicit override in every consumer/probe shell:

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

## 5. TLS, authenticated registry and Windows clients

Create local TLS material in a protected directory. The CA must have critical
`CA:TRUE` basic constraints and certificate-signing key usage. The server leaf
must cover the actual registry/gateway hostname; the tested local leaf also
covered `localhost` and `127.0.0.1`. Mount only the leaf certificate/key into the
servers. Never mount the CA private key.

The complete gateway `ca-bundle.crt` must include the required ordinary trust
roots plus the local CA. Mount it through `GATEWAY_CA_CONFIGMAP`, with the key
`ca-bundle.crt`; the deployment sets `SSL_CERT_FILE` for agent and ingest.
Keep an independent wrong CA and a hostname absent from the leaf SAN for the
negative checks.

Use separate local containers for:

- Distribution registry `registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373`,
  TLS and bcrypt htpasswd authentication, persistent registry data, 256 MiB limit,
  published on `127.0.0.1:5443`.
- Gateway TLS front `nginx:1.30.4-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c`,
  forwarding to the actual gateway on 4000, published on `127.0.0.1:8444`,
  non-root, read-only filesystem, dropped capabilities, 128 MiB limit.

For a new local PKI (never overwrite an existing rehearsal identity):

```sh
umask 077
mkdir -p "$LOCAL_STATE/pki" "$LOCAL_STATE/registry-data"
openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
  -subj '/CN=Local CRC rehearsal CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash' \
  -keyout "$LOCAL_STATE/pki/ca.key" -out "$LOCAL_STATE/pki/ca.crt"
openssl req -new -newkey rsa:3072 -nodes -subj '/CN=host.crc.testing' \
  -keyout "$LOCAL_STATE/pki/server.key" -out "$LOCAL_STATE/pki/server.csr"
cat > "$LOCAL_STATE/pki/server.ext" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:host.crc.testing,DNS:localhost,IP:127.0.0.1
EOF
openssl x509 -req -days 365 -in "$LOCAL_STATE/pki/server.csr" \
  -CA "$LOCAL_STATE/pki/ca.crt" -CAkey "$LOCAL_STATE/pki/ca.key" \
  -CAcreateserial -extfile "$LOCAL_STATE/pki/server.ext" \
  -out "$LOCAL_STATE/pki/server.crt"
cat /etc/ssl/certs/ca-certificates.crt "$LOCAL_STATE/pki/ca.crt" \
  > "$LOCAL_STATE/pki/ca-bundle.crt"
openssl verify -CAfile "$LOCAL_STATE/pki/ca.crt" "$LOCAL_STATE/pki/server.crt"
```

Install `htpasswd` from the connected host's Apache utilities if absent. Prompt
for the registry password; do not put it in a shell argument or Git:

```sh
export LOCAL_REGISTRY_USER=crc-loader
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
  htpasswd: {realm: Local CRC release registry, path: /auth/htpasswd}
EOF
docker run -d --name crc-fit-registry --memory 256m \
  -p 127.0.0.1:5443:5000 \
  -v "$LOCAL_STATE/registry-data:/var/lib/registry" \
  -v "$LOCAL_STATE/registry.yaml:/etc/docker/registry/config.yml:ro" \
  -v "$LOCAL_STATE/htpasswd:/auth/htpasswd:ro" \
  -v "$LOCAL_STATE/pki/server.crt:/pki/server.crt:ro" \
  -v "$LOCAL_STATE/pki/server.key:/pki/server.key:ro" \
  registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
```

The local registry is a host-side test service; product pod SCC requirements
still apply independently. Create the gateway front configuration without shell
expansion of Nginx's `$host` variable:

```sh
cat > "$LOCAL_STATE/nginx.conf" <<'EOF'
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 128; }
http {
  access_log off;
  server {
    listen 8444 ssl;
    server_name host.crc.testing localhost;
    ssl_certificate /pki/server.crt;
    ssl_certificate_key /pki/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / {
      proxy_pass http://host.docker.internal:4000;
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
docker run -d --name crc-fit-gateway-tls --entrypoint nginx \
  --user 1000:1000 --read-only --cap-drop ALL \
  --security-opt no-new-privileges --memory 128m \
  --tmpfs /tmp:uid=1000,gid=1000 --tmpfs /var/cache/nginx:uid=1000,gid=1000 \
  --add-host host.docker.internal:host-gateway -p 127.0.0.1:8444:8444 \
  -v "$LOCAL_STATE/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$LOCAL_STATE/pki/server.crt:/pki/server.crt:ro" \
  -v "$LOCAL_STATE/pki/server.key:/pki/server.key:ro" \
  nginx:1.30.4-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c \
  -g 'daemon off;'
```

Here 1000 is the WSL owner's ID, not an OpenShift pod identity. Adjust ownership
and tmpfs IDs together on a different WSL installation. The proxy's long I/O
ceiling does not extend the application's existing request deadlines.

In the consumer shell, after sourcing the gateway handoff:

```sh
export RERANK_ENABLED=false DENSE_DIM=1024
export LLM_BASE_URL=https://localhost:8444/v1
export EMBED_BASE_URL=https://localhost:8444/v1
export SSL_CERT_FILE="$LOCAL_STATE/pki/ca-bundle.crt"
.venv/bin/python scripts/probe_gateway.py --require-reasoning --stream
```

Keep the preserved Kind registry on port 5000 separate. Use the identical
registry authority and repository suffixes in load, deploy, ingest and pull
Secrets. The tested CRC authority was `host.crc.testing:5443`; CRC host access
reaches the Windows host, while the loader resolves that name to its WSL
loopback listener. Verify the actual route on the target machine rather than
assuming WSL NAT and localhost forwarding are identical everywhere. The tested
machine required no `netsh interface portproxy` entries and no new API bridge.

After CRC starts and Windows clients work (section 6), create the namespace,
registry trust and application inputs (section 7). For the loader, use the pinned
Skopeo container so its DNS override does not change the host's global hosts file:

```sh
mkdir -p "$LOCAL_STATE/registry-trust" "$LOCAL_STATE/registry-auth"
cp "$LOCAL_STATE/pki/ca.crt" "$LOCAL_STATE/registry-trust/ca.crt"
docker run --rm -it --network host --add-host host.crc.testing:127.0.0.1 \
  --user "$(id -u):$(id -g)" \
  -v "$LOCAL_STATE/registry-trust:/certs:ro" \
  -v "$LOCAL_STATE/registry-auth:/auth" \
  quay.io/skopeo/stable@sha256:0f75798d450d0cc0ea3700c79d929ae7609fb7d0e627673c14be8a484587c9b1 \
  login --cert-dir /certs --authfile /auth/auth.json \
  --username "$LOCAL_REGISTRY_USER" host.crc.testing:5443
export REGISTRY_AUTH_FILE="$LOCAL_STATE/registry-auth/auth.json"
```

The login prompts for the password. For an approved password-file workflow use
`docker run -i` and `--password-stdin`, redirecting the protected file into stdin.
Require anonymous `https://localhost:5443/v2/` to return 401 with the correct CA;
authenticated registry inspection/load and the uncached node check must succeed.

## 6. Windows API access from the WSL pipeline

When CRC's Kubernetes API is available only on Windows loopback, use Windows
`oc` and Windows Helm with a dedicated CRC kubeconfig. Leave the preserved Kind
context untouched. Verify the kubeconfig server and certificate; do not expose
a new Kubernetes API bridge merely to make a Linux client connect.

The tested Windows Helm archive was
`https://get.helm.sh/helm-v3.19.0-windows-amd64.zip`, SHA256
`6488630c2e5d5945ed990fa02fd9e99f9c6792cdbcd79eb264b6cfb90179d2d1`.
Its executable SHA256 was
`a18c49a4cd16f8b162031159eff6b4d657e04ec2df0c2be5544ad11ddf8fae79`.
Invoke the verified executable by absolute path.

Create a private CRC kubeconfig in the restricted Windows folder. In PowerShell,
use the local CRC administrator for the namespace/trust setup; `oc login` prompts
for the password obtained locally from `crc console --credentials`. Keep that
output out of logs. Browser acceptance later uses the ordinary developer user.

```powershell
crc oc-env | Invoke-Expression
oc login https://api.crc.testing:6443 --username kubeadmin --kubeconfig "$env:USERPROFILE\.crc-secrets\crc-kubeconfig"
oc --kubeconfig "$env:USERPROFILE\.crc-secrets\crc-kubeconfig" whoami --show-server
```

Keep it separate from Kind. Set the following WSL environment to your actual
absolute paths (the kubeconfig argument itself is a Windows path):

```sh
export CRC_OC_EXE='/mnt/c/Users/USER/.crc/bin/oc/oc.exe'
export CRC_HELM_EXE='/mnt/c/Users/USER/.crc-secrets/tools/helm.exe'
export CRC_KUBECONFIG_WIN='C:\Users\USER\.crc-secrets\crc-kubeconfig'
export CRC_LINUX_KUBECTL='/absolute/path/to/checksum-verified/linux/kubectl'
export OPERATOR_BIN="$LOCAL_STATE/operator-bin"
mkdir -p "$OPERATOR_BIN"
cat > "$OPERATOR_BIN/oc" <<'PY_ADAPTER'
#!/usr/bin/python3
import os, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name in ('oc', 'kubectl') and ('kustomize' in args or '--local' in args):
    exe = os.environ['CRC_LINUX_KUBECTL']
    os.execv(exe, [exe, *args])
exe = os.environ['CRC_HELM_EXE' if name == 'helm' else 'CRC_OC_EXE']
file_flags = {'-f', '--filename', '--values', '--from-file', '--from-env-file',
              '--cert', '--key'}
def windows_path(value, flag=''):
    prefix, path = '', value
    if flag == '--from-file' and '=' in value:
        key, candidate = value.split('=', 1)
        if '/' not in key:
            prefix, path = key + '=', candidate
    if '/' in path and Path(path).exists():
        path = subprocess.check_output(
            ['wslpath', '-w', str(Path(path).resolve())], text=True).strip()
    return prefix + path
converted, remote, pending = [], False, ''
for arg in args:
    if arg == '--':
        remote = True
    if not remote:
        if pending:
            arg = windows_path(arg, pending)
            pending = ''
        elif arg in file_flags:
            pending = arg
        elif arg.split('=', 1)[0] in file_flags and '=' in arg:
            flag, value = arg.split('=', 1)
            arg = flag + '=' + windows_path(value, flag)
        elif not arg.startswith('-'):
            arg = windows_path(arg)
    converted.append(arg)
os.execv(exe, [exe, '--kubeconfig', os.environ['CRC_KUBECONFIG_WIN'], *converted])
PY_ADAPTER
chmod 700 "$OPERATOR_BIN/oc"
ln -s oc "$OPERATOR_BIN/kubectl"
ln -s oc "$OPERATOR_BIN/helm"
export PATH="$OPERATOR_BIN:$PATH"
oc whoami --show-server
oc get nodes
helm list --all-namespaces
kubectl kustomize deploy/kustomize/overlays/openshift > "$LOCAL_STATE/render-check.yaml"
```

Only existing path arguments containing `/` are translated, and translation
stops at `--`: `deploy` is a Kubernetes resource word even when a local directory
has that name. Commands after `exec ... --` run inside pods, not on Windows.
The adapter also translates `--from-file=KEY=PATH` and explicit certificate,
key and values-file arguments. Verify file-based `apply`, a client-only
`create configmap --from-file` dry run, and stdin `exec` before the pipeline. No API bridge or TLS-verification bypass is required.

Install a local Skopeo adapter beside these clients. Set `CRC_ARTIFACT_ROOT` to
the **fresh extracted candidate directory**, containing its bootstrapped clone.
The socket mount gives this tool access to Docker; run only the pinned image.
The adapter exposes the candidate, registry public trust and auth file to Skopeo:

```sh
cat > "$OPERATOR_BIN/skopeo" <<'SH_SKOPEO'
#!/bin/sh
set -eu
: "${CRC_ARTIFACT_ROOT:?Set the verified unpacked candidate directory}"
: "${LOCAL_STATE:?Set the private local rehearsal directory}"
exec docker run --rm -i --network host --add-host host.crc.testing:127.0.0.1 \
  --user "$(id -u):$(id -g)" --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "$CRC_ARTIFACT_ROOT:$CRC_ARTIFACT_ROOT:ro" \
  -v "$LOCAL_STATE/registry-trust:/certs:ro" \
  -v "$LOCAL_STATE/registry-auth/auth.json:/auth.json:ro" \
  -e REGISTRY_AUTH_FILE=/auth.json -w "$PWD" \
  quay.io/skopeo/stable@sha256:0f75798d450d0cc0ea3700c79d929ae7609fb7d0e627673c14be8a484587c9b1 "$@"
SH_SKOPEO
chmod 700 "$OPERATOR_BIN/skopeo"
```

## 7. Candidate deployment and measured acceptance

Use a fresh trusted bootstrap of a green published-main bundle. Set
`AIRGAP_ENV` to a protected file outside the fresh checkout. The approved local
Qdrant sizing file is:

```yaml
replicaCount: 1
resources:
  requests: {cpu: 200m, memory: 256Mi}
  limits: {cpu: "2", memory: 2Gi}
```

The original 512 MiB request left too little scheduler reservation for ingest
at 12 GiB CRC, despite several GiB of actual free RAM. The 256 MiB request was
explicitly approved for this small synthetic workload; the 2 GiB limit stays.
Use one ingest worker, with requests 500m/1Gi and limits 2CPU/2Gi. Set Qdrant
data/snapshot, corpus and ingest-work claims to 1Gi. Retain both agents, both
OAuth sidecars and Jaeger's existing 10Gi claim. Keep production defaults intact.

Before starting 12288 MiB CRC, require at least 14 GiB available in **Windows**
with both models and the gateway already healthy. After completed archive work,
let clean file cache be reclaimed and recheck Windows memory. Do not confuse
Linux reclaimable cache with Windows available RAM. A registry reload can cause
a transient host-memory dip; record it, allow setup to settle, and measure the
steady workload separately. Do not purge cache during the measured workload.

Run the full pipeline and all acceptance checks in
[the release procedure](crc-release-verification.md). Require two cold starts
and a measured 30-minute run with one interactive client and actual fresh ingest
work. Keep the existing request deadlines and require explicit citations,
follow-up chat, all stream endings, long embedding input, unchanged images,
healthy operators, writable/persistent volumes and enforced egress restrictions.
The release record remains blocked until every required check is attributable
to the exact candidate bytes and passes.

### 7.1 Provision namespace, trust and private inputs

From the fresh verified workspace, use a dedicated local namespace and its
actual storage class. Example values below are local coordinates; inspect
`oc get sc` before setting `CRC_STORAGE_CLASS`.

```sh
export CRC_NAMESPACE=rag-crc-release
export CRC_STORAGE_CLASS=crc-csi-hostpath-provisioner
export IMAGE_SHA="$(git rev-parse HEAD)"
oc create namespace "$CRC_NAMESPACE"
oc -n "$CRC_NAMESPACE" create secret generic crc-registry-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$REGISTRY_AUTH_FILE"
oc -n "$CRC_NAMESPACE" create configmap crc-gateway-ca \
  --from-file=ca-bundle.crt="$LOCAL_STATE/pki/ca-bundle.crt"
```

For cluster node trust, first inspect
`oc get image.config.openshift.io/cluster -o yaml`. If `additionalTrustedCA`
already names a ConfigMap, add the local CA key to that ConfigMap and preserve
all existing keys. If no trust ConfigMap exists, the initial creation is:

```sh
oc -n openshift-config create configmap crc-registry-ca \
  --from-file=host.crc.testing..5443="$LOCAL_STATE/pki/ca.crt"
oc patch image.config.openshift.io/cluster --type=merge \
  -p '{"spec":{"additionalTrustedCA":{"name":"crc-registry-ca"}}}'
```

Wait for the machine configuration and cluster operators to converge; confirm
actual node trust with an uncached pull, rather than treating the patch as proof.

Create Secret inputs from the gateway handoff without printing keys or passing
them as command arguments. All four referenced key names must exist:

```sh
. "$GATEWAY_ENV_FILE"
export RERANK_ENABLED=false
mkdir -p "$LOCAL_STATE/secret-inputs"
python3 - <<'PY_SECRET'
import os, secrets
from pathlib import Path
root = Path(os.environ['LOCAL_STATE']) / 'secret-inputs'
for filename, variable in {'llm-api-key':'LLM_API_KEY',
                          'embed-api-key':'EMBED_API_KEY',
                          'rerank-api-key':'RERANK_API_KEY',
                          'context-llm-api-key':'LLM_API_KEY'}.items():
    p = root / filename
    p.write_text(os.environ[variable]); p.chmod(0o600)
p = root / 'cookie-secret'
with p.open('x') as f:
    f.write(secrets.token_urlsafe(24))
p.chmod(0o600)
PY_SECRET
oc -n "$CRC_NAMESPACE" create secret generic crc-gateway-keys \
  --from-file="$LOCAL_STATE/secret-inputs/llm-api-key" \
  --from-file="$LOCAL_STATE/secret-inputs/embed-api-key" \
  --from-file="$LOCAL_STATE/secret-inputs/rerank-api-key" \
  --from-file="$LOCAL_STATE/secret-inputs/context-llm-api-key"
oc -n "$CRC_NAMESPACE" create secret generic rag-agent-oauth-cookie \
  --from-file="$LOCAL_STATE/secret-inputs/cookie-secret"
```

These are first-provisioning commands. On restart reuse the existing credentials,
PVCs and Secrets; do not regenerate keys or cookies. Contextual embedding remains
disabled in this local rehearsal.

### 7.2 Configure and deploy the same candidate

Save the Qdrant sizing above as `$LOCAL_STATE/qdrant-sizing.yaml`. Save this
strategic merge patch as `$LOCAL_STATE/ingest-sizing.json`:

```json
{"spec":{"template":{"spec":{"containers":[{"name":"ingest","resources":{"requests":{"cpu":"500m","memory":"1Gi"},"limits":{"cpu":"2","memory":"2Gi"}}}]}}}}
```

Create the local env file from the verified clone. No key values go in it:

```sh
cat > "$LOCAL_STATE/airgap.env" <<EOF
INTERNAL_REGISTRY=host.crc.testing:5443
NAMESPACE=$CRC_NAMESPACE
STORAGE_CLASS=$CRC_STORAGE_CLASS
IMAGE_SHA=$IMAGE_SHA
VLLM_BASE_URL=https://host.crc.testing:8444
EMBED_BASE_URL=https://host.crc.testing:8444/v1
LLM_BASE_URL=https://host.crc.testing:8444/v1
EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B
LLM_MODEL_REASONING=google/gemma-4-E4B-it-qat-mobile-ct
DENSE_DIM=1024
EMBED_MODEL_REVISION=local:Qwen/Qwen3-Embedding-0.6B
RERANK_ENABLED=false
AGENT_ROUTE=true
PULL_SECRET=crc-registry-pull
GATEWAY_API_KEY_SECRET=crc-gateway-keys
GATEWAY_CA_CONFIGMAP=crc-gateway-ca
CORPUS_PVC=crc-synthetic-corpus
QDRANT_STORAGE_SIZE=1Gi
INGEST_WORK_SIZE=1Gi
INGEST_WORKERS=1
QDRANT_EXTRA_VALUES=$LOCAL_STATE/qdrant-sizing.yaml
INGEST_EXTRA_PATCH=$LOCAL_STATE/ingest-sizing.json
INSECURE_REGISTRY=false
SKOPEO_ARGS='--dest-cert-dir /certs'
EOF
export AIRGAP_ENV="$LOCAL_STATE/airgap.env"
export SNEAKERNET_TRUSTED_PUB=/absolute/path/to/independently-trusted/signing.pub
```

Run pipeline commands in a clean operator shell: environment values override
`AIRGAP_ENV`. In particular, the host gateway handoff sets localhost URLs and
plaintext keys; those must not override the pod URLs and Secret references.
Unset consumer-only values after preparing the Secret:

```sh
unset LLM_BASE_URL EMBED_BASE_URL RERANK_BASE_URL CONTEXT_LLM_BASE_URL
unset LLM_API_KEY EMBED_API_KEY RERANK_API_KEY CONTEXT_LLM_API_KEY
make airgap-validate
make airgap-load
```

Create the synthetic corpus with the **loaded candidate ingest image**:

```sh
oc -n "$CRC_NAMESPACE" apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: crc-synthetic-corpus}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $CRC_STORAGE_CLASS
  resources: {requests: {storage: 1Gi}}
---
apiVersion: batch/v1
kind: Job
metadata: {name: crc-corpus-generator}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      imagePullSecrets: [{name: crc-registry-pull}]
      containers:
      - name: generator
        image: host.crc.testing:5443/qdrant-pdf-rag-ingest:$IMAGE_SHA
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
        persistentVolumeClaim: {claimName: crc-synthetic-corpus}
EOF
oc -n "$CRC_NAMESPACE" wait --for=condition=complete job/crc-corpus-generator --timeout=300s
oc -n "$CRC_NAMESPACE" logs job/crc-corpus-generator
```

For a rerun delete only the generator Job, retaining the PVC. Record its admitted
SCC and writes. Before deploying the application, prove the intended gateway settings from a
temporary pod using the loaded agent image and the same key/trust references:

```sh
oc -n "$CRC_NAMESPACE" apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata: {name: crc-gateway-probe}
spec:
  restartPolicy: Never
  imagePullSecrets: [{name: crc-registry-pull}]
  containers:
  - name: probe
    image: host.crc.testing:5443/qdrant-pdf-rag-agent:$IMAGE_SHA
    command: [python3, /app/scripts/probe_gateway.py, --require-reasoning, --stream]
    env:
    - {name: EMBED_MODE, value: vllm}
    - {name: EMBED_BASE_URL, value: 'https://host.crc.testing:8444/v1'}
    - {name: LLM_BASE_URL, value: 'https://host.crc.testing:8444/v1'}
    - {name: EMBED_MODEL, value: Qwen/Qwen3-Embedding-0.6B}
    - {name: LLM_MODEL_REASONING, value: google/gemma-4-E4B-it-qat-mobile-ct}
    - {name: DENSE_DIM, value: '1024'}
    - {name: RERANK_ENABLED, value: 'false'}
    - {name: SSL_CERT_FILE, value: /etc/gateway-ca/ca-bundle.crt}
    - name: EMBED_API_KEY
      valueFrom: {secretKeyRef: {name: crc-gateway-keys, key: embed-api-key}}
    - name: LLM_API_KEY
      valueFrom: {secretKeyRef: {name: crc-gateway-keys, key: llm-api-key}}
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
    configMap: {name: crc-gateway-ca}
EOF
oc -n "$CRC_NAMESPACE" wait --for=jsonpath='{.status.phase}'=Succeeded pod/crc-gateway-probe --timeout=600s
oc -n "$CRC_NAMESPACE" logs crc-gateway-probe
oc -n "$CRC_NAMESPACE" get pod crc-gateway-probe -o yaml > "$LOCAL_STATE/gateway-probe-evidence.yaml"
oc -n "$CRC_NAMESPACE" delete pod crc-gateway-probe
```

Stop on any failed command. Confirm exit code zero, `restricted-v2` and the
candidate image identity in the evidence, then run:

```sh
make airgap-pipeline
oc -n "$CRC_NAMESPACE" exec deploy/rag-agent -c agent -- \
  python3 /app/scripts/probe_gateway.py --require-reasoning --stream
oc -n "$CRC_NAMESPACE" exec -i deploy/rag-agent -c agent -- python3 - \
  < scripts/ci/application_contracts.py
```

The last helper sends original synthetic questions to actual application HTTP
interfaces. It can exercise real models; do not run the fault-injection helper
against the real model deployment. Run browser login/stream checks as well.

### 7.3 Browser trust and OAuth

Obtain the public ingress CA from the cluster's configured default ingress
certificate chain and compare its fingerprint with the cluster value. For this
CRC pin the generated CA is in the `router-ca` Secret in `openshift-ingress-operator`;
export **only** `tls.crt` to a local `.crt` file, never the private key. Inspect the
current cluster before relying on that name:

```sh
oc -n openshift-ingress-operator get secret router-ca \
  -o 'jsonpath={.data.tls\.crt}' | base64 -d > "$LOCAL_STATE/crc-ingress-ca.crt"
openssl x509 -in "$LOCAL_STATE/crc-ingress-ca.crt" -noout -subject -fingerprint -sha256
```

Copy the public certificate to the protected Windows folder, verify its SHA256
there, then import it in ordinary PowerShell:

```powershell
Get-FileHash "$env:USERPROFILE\.crc-secrets\crc-ingress-ca.crt" -Algorithm SHA256
$cert = Import-Certificate -FilePath "$env:USERPROFILE\.crc-secrets\crc-ingress-ca.crt" -CertStoreLocation Cert:\CurrentUser\Root
$cert.Thumbprint
```

Record the exact thumbprint. When retiring CRC remove only that entry with
`Remove-Item Cert:\CurrentUser\Root\RECORDED_THUMBPRINT`. Never disable browser
certificate checks. A fresh request may show the OAuth provider chooser with
HTTP 403; `/oauth/start` then redirects to login. Use an ordinary CRC user, return
to `/ui`, verify local assets, send a cited question and contextual follow-up,
and confirm Send → Stop → Send and cleared busy state on both completions.

### 7.4 Network enforcement and node pulls

Apply the [release egress procedure](crc-release-verification.md#8-runtime-egress-isolation)
using the actual CRC network observations. The tested namespace-wide policy had
`podSelector: {}` and allowed only these destinations:

| Destination | Allowed ports |
|---|---|
| Same namespace pods | TCP 6333, 6334, 6335, 4318, 16686 |
| `openshift-dns`, pod label `dns.operator.openshift.io/daemonset-dns=default` | UDP/TCP 53 and 5353 |
| `openshift-authentication`, pod label `app=oauth-openshift` | TCP 6443 |
| Observed `host.crc.testing` gateway destination, a /32 | TCP 8444 |
| Observed API/OAuth/router destination addresses, individual /32 entries | TCP 443 and 6443 |

Resolve and record the addresses from the pod/network path; do not copy another
machine's IPs. Do not add `0.0.0.0/0`. Save the rendered policy, before/after
public direct-IP controls and working internal dependencies. Repeat the complete
application, ingest and OAuth checks under enforcement.

Node image policy is separate. This CRC needed approved whole-domain allowlist
exceptions for `registry.redhat.io`, `registry.access.redhat.com` and `quay.io` so
the built-in samples operator could remain Managed and healthy. Alongside the
local and integrated registries these permit pulls from all repositories on
those public domains; they are **local CRC exceptions**, not an air-gap policy.
Preserve the prior image configuration before an administrator applies the
reviewed `registrySources.allowedRegistries` list. Docker Hub remained denied.
Inspect all operator conditions after reconciliation.

For each pull control, inspect the **complete CRI-O image cache** (for example,
admin `oc debug node/crc -- chroot /host crictl images -o json`) and require both
manifest and normalized image-config IDs to be absent. `node.status.images` is
capped and cannot establish absence. Pull a unique disposable control image from
the authenticated TLS registry, then a separately uncached public Docker Hub
image. Require the first to run under `restricted-v2` and the second to fail from
node policy. Retain events and remove only those diagnostic pods. This control
image is diagnostic scaffolding, never a replacement release image.

### 7.5 Cold starts, workload, recovery and evidence

Record UTC boundaries and sample Windows available memory/paging, CRC available
memory, GPU free memory, WSL swap counters, operator conditions and pod/container
restart counts throughout the workload. Use Windows `Win32_OperatingSystem` and
`Win32_PerfFormattedData_PerfOS_Memory`, CRC node `stats/summary`, `nvidia-smi`,
`/proc/meminfo` and `/proc/vmstat`. Require the stated headroom on every steady
workload sample; historical swap usage alone is not evidence of sustained paging.

For each of two cold starts, stop CRC, then the recorded model containers and
`make local-gateway-stop GATEWAY_NAME=crc-litellm-gateway
PG_NAME=crc-litellm-gateway-pg PG_NET=crc-litellm-gateway-net`. Retain the database
volume, registry and TLS front. Restart reasoning, then embedding, restore the
same private gateway keys, probe HTTPS, require the Windows startup headroom,
then boot CRC. Recheck every operator, application gateway/streams, collection
IDs, PVCs and credential fingerprints. Expected restarts from this deliberate
shutdown must be separated from unexpected restarts during measured workload.

For the 30-minute run keep one interactive client and one ingest worker. Repeat
the application contract command while generating a new uniquely named original
synthetic PDF and running `make airgap-ingest` at intervals. Merely re-running an
unchanged inventory is not fresh ingest work. Record each request/job outcome,
explicit non-inferred citations, long embedding inputs and completion markers.
Afterward run the existing snapshot/PVC/trace helper and repeat the full pipeline:

```sh
sh scripts/ci/check_lifecycle.sh "$CRC_NAMESPACE"
make airgap-pipeline
```

Compare exact point IDs/counts and hashes of credentials before/after; do not
print the credentials. Export only a synthetic snapshot outside Git and prove
restore/query equivalence in an isolated collection. Reconcile archive and
registry image identities as described in [deploy.md](deploy.md#image-identity-across-archive-and-registry-formats).
Hash the original tarball again and complete the protected release record.

To return to the preserved Kind environment, stop CRC, select the saved Kind
kubeconfig, start only its recorded node containers, and verify its original PVCs,
collections and backups. Do not delete/recreate that cluster. The local registry,
model files, TLS material and gateway key store remain available for the next CRC
rehearsal. A production transfer still requires operator sign-off and the exact
bytes that passed the release gate.


## 8. Reproduce the certificate and long-input controls

Use the intended cluster's client and namespace. For CRC, `kubectl` is the
Windows adapter; for Kind it is the pinned Linux client with the private Kind
kubeconfig. Run diagnostics while the interactive client is idle.

This check uses the actual application's Settings, Secret-backed key and CA
mount. It launches a fresh client process per CA choice so trust cannot be
retained from the positive case. The diagnostic timeout is 10 seconds; it does
not change application request deadlines.

```sh
# In the CRC shell; use "$KIND_NAMESPACE" instead in the Kind shell.
export TEST_NAMESPACE="$CRC_NAMESPACE"
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
controls to fail TLS. For ingest, clone the already rendered ingest Job into a
uniquely named diagnostic Job, preserving its image, environment, CA/Secret
references and volume/security settings, and replace only its command with the
same Python diagnostic. Remove server-generated metadata/selectors, set
`backoffLimit: 0`, wait for completion, retain logs/admission/imageID, then delete
only that diagnostic Job. The measured run exercised both actual images.

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
checked separately by `application_contracts.py` and the browser exercise.
The production gateway uses its owning team's model/tokenizer and dimension;
do not copy these local model constants into production settings.
