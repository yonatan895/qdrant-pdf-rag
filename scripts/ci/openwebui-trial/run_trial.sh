#!/bin/sh
# Open WebUI offline Kind trial (issue #1073). Trial-only, connected dev host.
# Never a product path; the production chart is untouched (ADR-0007, #1080).
#
#   sh scripts/ci/openwebui-trial/run_trial.sh images|cluster|deploy|audit|egress-report|teardown
#
# images   build the derived Open WebUI image and the agent image (connected
#          side only), pull the pinned Dex and Qdrant images, record digests
# cluster  create the disposable Kind cluster $KIND_NAME (default owui-trial)
#          with the repository-pinned Kind and node image, load every image
#          (pods use imagePullPolicy: Never), enable CoreDNS query logging
# deploy   create run-time Secrets (generated, never printed), render and
#          apply stack.yaml, run the ingest Job, wait for readiness
# audit    install the node-level egress audit: an nftables set that records
#          every new flow (destination, protocol, port) the Open WebUI pod opens
# egress-report  print that set and the pod's CoreDNS queries
# idp-config  regenerate the Dex config (DEX_TOKEN_EXPIRY) and restart Dex
# toolprobe  add the tool-server identity probe (toolprobe.yaml, #1088): echo
#          tool server, broker IdP, Open WebUI tool-server registration
# teardown delete the cluster (only $KIND_NAME, which this script created)
#
# Inputs: KIND_BIN (pinned kind v0.33.0), EVIDENCE (outside git), PIPE_FILE
# (the #1072 Docs Pipe source), AGENT_IMAGE/OWUI_IMAGE tags. Run under the
# host's heavy-work lock; the caller checks MemAvailable first.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/../../.." && pwd)
KIND_NAME=${KIND_NAME:-owui-trial}
KIND_BIN=${KIND_BIN:-kind}
KUBECTL=${KUBECTL:-kubectl}
EVIDENCE=${EVIDENCE:?set EVIDENCE to a private directory outside git}
NODE_IMAGE=kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
OWUI_BASE=ghcr.io/open-webui/open-webui@sha256:5adfb0355bbe4a5860c0ba9482cc0b40f573de3749a21a64cf4789391a14fffc
# Test-only images by their linux/amd64 manifest digest (a single-platform image
# loads into the node; the index digests are recorded in the trial record):
# Dex v2.46.0-distroless index sha256:8ba378a1...; Qdrant v1.19.2-unprivileged
# index sha256:efb96a94... (the images.txt pin).
DEX_PIN=ghcr.io/dexidp/dex@sha256:a9492ee26351ea7aaac1cb5ce3feed0e5796faea73fb39fb1a8eb649433a3912
QDRANT_PIN=docker.io/qdrant/qdrant@sha256:390ca16351185f9913c23d0c95ded0cdf2ce17e525dd7fbe58d3339c9c80e633
SKOPEO_IMAGE=quay.io/skopeo/stable@sha256:0f75798d450d0cc0ea3700c79d929ae7609fb7d0e627673c14be8a484587c9b1
OWUI_IMAGE=${OWUI_IMAGE:-mainframe-rag/openwebui-trial:0.12.3-slim}
AGENT_IMAGE=${AGENT_IMAGE:-mainframe-rag/agent:1073-trial}
DEX_IMAGE=trial.local/dex:v2.46.0-distroless
QDRANT_IMAGE=trial.local/qdrant:v1.19.2-unprivileged
NS=owui-trial
NODE="$KIND_NAME-control-plane"
export KUBECONFIG="$EVIDENCE/kubeconfig"

mkdir -p "$EVIDENCE"
chmod 700 "$EVIDENCE"
k() { "$KUBECTL" --context "kind-$KIND_NAME" "$@"; }

images() {
    docker pull -q "$OWUI_BASE" >/dev/null
    docker pull -q "$DEX_PIN" >/dev/null
    docker pull -q "$QDRANT_PIN" >/dev/null
    docker tag "$DEX_PIN" "$DEX_IMAGE"
    docker tag "$QDRANT_PIN" "$QDRANT_IMAGE"
    docker build -q -f "$REPO/scripts/ci/openwebui-trial/Containerfile.openwebui" -t "$OWUI_IMAGE" "$REPO/scripts/ci/openwebui-trial" >/dev/null
    BUNDLE_DIR=${BUNDLE_DIR:?set BUNDLE_DIR to the directory holding wheelhouse/ and bm25-weights/}
    docker build -q --build-context wheelhouse="$BUNDLE_DIR/wheelhouse" \
        --build-context bm25="$BUNDLE_DIR/bm25-weights" \
        -f "$REPO/images/Containerfile.agent" -t "$AGENT_IMAGE" "$REPO" >/dev/null
    for ref in "$OWUI_IMAGE" "$AGENT_IMAGE" "$DEX_IMAGE" "$QDRANT_IMAGE" "$OWUI_BASE"; do
        docker image inspect "$ref" --format "$ref {{.Id}} {{json .RepoDigests}} {{.Config.User}}"
    done | tee "$EVIDENCE/images.txt"
}

cluster() {
    if "$KIND_BIN" get clusters | grep -qx "$KIND_NAME"; then
        echo "cluster $KIND_NAME already exists" >&2; exit 1
    fi
    "$KIND_BIN" create cluster --name "$KIND_NAME" --image "$NODE_IMAGE" \
        --kubeconfig "$KUBECONFIG" --wait 120s
    # The node is a container this script owns: cap it so the shared host stays safe.
    docker update --memory 6g --memory-swap 6g "$NODE" >/dev/null
    load
    dnslog
}

dnslog() {
    # CoreDNS query log: every name any pod resolves is recorded.
    k -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' > "$EVIDENCE/Corefile.orig"
    sed 's/^\.:53 {/.:53 {\n    log/' "$EVIDENCE/Corefile.orig" > "$EVIDENCE/Corefile.log"
    k -n kube-system create configmap coredns --from-file=Corefile="$EVIDENCE/Corefile.log" \
        --dry-run=client -o yaml | k apply -f -
    k -n kube-system rollout restart deploy/coredns
    k -n kube-system rollout status deploy/coredns --timeout=120s
}

load() {
    # The two locally built images load with kind. The pulled test images are
    # copied from the registry by their amd64 manifest digest with the pinned
    # skopeo image (on this host `docker save` of a pulled index omits blobs
    # and `kind load` imports --all-platforms), then imported into the node.
    "$KIND_BIN" load docker-image --name "$KIND_NAME" "$OWUI_IMAGE" "$AGENT_IMAGE"
    for pair in "$DEX_PIN=$DEX_IMAGE=dex" "$QDRANT_PIN=$QDRANT_IMAGE=qdrant"; do
        src=${pair%%=*}; rest=${pair#*=}; name=${rest%%=*}; file=${rest#*=}
        docker run --rm --user "$(id -u):$(id -g)" -v "$EVIDENCE:$EVIDENCE" "$SKOPEO_IMAGE" \
            copy -q "docker://$src" "docker-archive:$EVIDENCE/$file.tar:$name"
        docker exec -i "$NODE" ctr -n k8s.io images import --snapshotter overlayfs - < "$EVIDENCE/$file.tar"
        rm -f "$EVIDENCE/$file.tar"
    done
    docker exec "$NODE" crictl images | tee "$EVIDENCE/node-images.txt"
}

render() {
    sed -e "s#@TRIAL_UID@#$TRIAL_UID#g" -e "s#@OWUI_IMAGE@#$OWUI_IMAGE#g" \
        -e "s#@AGENT_IMAGE@#$AGENT_IMAGE#g" -e "s#@DEX_IMAGE@#$DEX_IMAGE#g" \
        -e "s#@QDRANT_IMAGE@#$QDRANT_IMAGE#g" -e "s#@READ_ONLY_ROOT@#${READ_ONLY_ROOT:-true}#g" "$1"
}

secrets() {
    # Generated per run, written only to the 0700 evidence dir and the cluster.
    python3 - "$EVIDENCE" <<'PY'
import json, os, secrets, sys, uuid
ev = sys.argv[1]
path = os.path.join(ev, "secrets.json")
if not os.path.exists(path):
    data = {"webui": secrets.token_urlsafe(32), "client": secrets.token_urlsafe(32),
            "admin": secrets.token_urlsafe(18), "alice": secrets.token_urlsafe(18),
            "bob": secrets.token_urlsafe(18), "gateway": secrets.token_urlsafe(32),
            "broker_client": secrets.token_urlsafe(32)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
PY
    # bcrypt hashes for Dex come from the Open WebUI image's own bcrypt module.
    python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print("\n".join(d[u] for u in ("admin","alice","bob")))' \
        "$EVIDENCE/secrets.json" | docker run --rm -i --network none --entrypoint python3 "$OWUI_IMAGE" -c '
import bcrypt, sys
for line in sys.stdin.read().split():
    print(bcrypt.hashpw(line.encode(), bcrypt.gensalt(10)).decode())' > "$EVIDENCE/hashes.txt"
    python3 - "$EVIDENCE" "${DEX_TOKEN_EXPIRY:-}" "${DEX_STORAGE:-memory}" > "$EVIDENCE/dex-config.yaml" <<'PY'
import json, sys
ev = sys.argv[1]
expiry = sys.argv[2]
storage = sys.argv[3]
s = json.load(open(f"{ev}/secrets.json"))
hashes = open(f"{ev}/hashes.txt").read().split()
users = [("admin", "owui-admins", "4b0f4a8e-0000-4000-8000-000000000001"),
         ("alice", "docs-readers", "4b0f4a8e-0000-4000-8000-000000000002"),
         ("bob", "other-team", "4b0f4a8e-0000-4000-8000-000000000003")]
print("issuer: http://dex.owui-trial.svc.cluster.local:5556/dex")
# DEX_STORAGE=sqlite3 keeps state in the pod's /tmp emptyDir (refresh-token probe).
print("storage: {type: sqlite3, config: {file: /tmp/dex.db}}" if storage == "sqlite3" else "storage: {type: memory}")
print("web: {http: 0.0.0.0:5556}")
print("oauth2: {skipApprovalScreen: true, responseTypes: [code]}")
print("enablePasswordDB: true")
if expiry:
    # Token-lifetime probe (#1088): Dex uses this lifetime for ID and access tokens.
    print(f"expiry: {{idTokens: {expiry}}}")
print("staticClients:")
print("  - id: open-webui")
print("    name: Open WebUI")
print(f"    secret: {json.dumps(s['client'])}")
print("    redirectURIs: ['http://openwebui.owui-trial.svc.cluster.local:8080/oauth/oidc/callback']")
# Tool-probe (#1088): a gateway client that trusts open-webui, so open-webui may
# ask for it as an extra audience (scope audience:server:client_id:team-gateway).
# The probe broker's OIDC connector also authenticates as this client.
print("  - id: team-gateway")
print("    name: Team gateway (trial)")
print(f"    secret: {json.dumps(s['gateway'])}")
print("    trustedPeers: [open-webui]")
print("    redirectURIs: ['http://dex-broker.owui-trial.svc.cluster.local:5556/dex/callback']")
print("staticPasswords:")
for (name, group, uid), h in zip(users, hashes):
    print(f"  - email: {name}@trial.test")
    print(f"    hash: {json.dumps(h)}")
    print(f"    username: {name}")
    print(f"    userID: {uid}")
    print(f"    groups: [{group}]")
PY
    chmod 600 "$EVIDENCE/dex-config.yaml" "$EVIDENCE/hashes.txt"
    k -n "$NS" create secret generic dex-config --from-file=config.yaml="$EVIDENCE/dex-config.yaml" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    python3 - "$EVIDENCE" <<'PY' | k apply -f - >/dev/null
import base64, json, sys
s = json.load(open(f"{sys.argv[1]}/secrets.json"))
b = lambda v: base64.b64encode(v.encode()).decode()
docs = [
    {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "openwebui-secret", "namespace": "owui-trial"},
     "data": {"webui-secret-key": b(s["webui"]), "oauth-client-secret": b(s["client"])}},
    {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "trial-users", "namespace": "owui-trial"},
     "data": {u: b(s[u]) for u in ("admin", "alice", "bob")}},
]
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": docs}))
PY
}

deploy() {
    TRIAL_UID=${TRIAL_UID:-$(python3 -c 'import secrets;print(1000700000 + secrets.randbelow(100000))')}
    echo "$TRIAL_UID" > "$EVIDENCE/trial-uid.txt"
    PIPE_FILE=${PIPE_FILE:?set PIPE_FILE to the Docs Pipe source}
    render "$HERE/stack.yaml" > "$EVIDENCE/stack-rendered.yaml"
    k apply -f "$EVIDENCE/stack-rendered.yaml" 2>&1 | tee "$EVIDENCE/apply.log" | tail -3
    k -n "$NS" create configmap mock-vllm-script --from-file=mock_vllm.py="$REPO/scripts/mock_vllm.py" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$NS" create configmap trial-pipes --from-file=docs_pipe.py="$PIPE_FILE" \
        --from-literal=agent_url=http://agent.owui-trial.svc.cluster.local:8080 \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    secrets
    k -n "$NS" rollout status deploy/qdrant --timeout=300s
    k -n "$NS" rollout status deploy/mock-vllm --timeout=300s
    k -n "$NS" delete job ingest --ignore-not-found
    render "$HERE/ingest-job.yaml" | k apply -f -
    k -n "$NS" wait --for=condition=complete job/ingest --timeout=900s
    k -n "$NS" rollout restart deploy/agent
    for d in agent dex openwebui; do k -n "$NS" rollout status "deploy/$d" --timeout=600s; done
    k -n "$NS" wait --for=condition=Ready pod/browser --timeout=300s
}

audit() {
    # Every new flow any pod opens (source, destination, protocol, port), seen at
    # prerouting before NAT and before the NetworkPolicy verdict, so attempts
    # that the policy drops are recorded too. Covers pod restarts (new IPs).
    docker exec -i "$NODE" nft -f - <<EOF
table inet owui_audit
delete table inet owui_audit
table inet owui_audit {
    set flows { type ipv4_addr . ipv4_addr . inet_proto . inet_service; flags dynamic; size 65535; }
    chain pre {
        type filter hook prerouting priority -150; policy accept;
        ip saddr 10.244.0.0/16 ct state new add @flows { ip saddr . ip daddr . meta l4proto . th dport } counter
    }
}
EOF
    echo "audit on"
}

egress_report() {
    # Flows whose source is any Open WebUI pod IP seen so far (recorded by the caller).
    echo "--- Open WebUI pod IPs"; sort -u "$EVIDENCE/owui-pod-ips.txt"
    echo "--- flows from those IPs (dst proto port)"
    docker exec "$NODE" nft -j list set inet owui_audit flows | python3 "$HERE/egress_report.py" flows "$EVIDENCE/owui-pod-ips.txt"
    echo "--- services"
    k get svc -A --no-headers | awk '{print $1, $2, $4}'
    echo "--- CoreDNS queries from those IPs (count name type)"
    k -n kube-system logs -l k8s-app=kube-dns --tail=-1 | python3 "$HERE/egress_report.py" dns "$EVIDENCE/owui-pod-ips.txt"
}

toolprobe() {
    # Tool-server identity probe (#1088): the echo tool server, the broker IdP
    # and Open WebUI registered with both as system_oauth tool servers (MCP and
    # OpenAPI) and user-info headers on. TOOLPROBE_OAUTH_SCOPES, when set,
    # replaces OAUTH_SCOPES (the extra-audience run). Secrets stay in $EVIDENCE.
    TRIAL_UID=$(cat "$EVIDENCE/trial-uid.txt")
    k -n "$NS" create configmap toolprobe-script --from-file=toolprobe_echo.py="$HERE/toolprobe_echo.py" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    python3 - "$EVIDENCE" <<'PY'
import json, os, sys
ev = sys.argv[1]
s = json.load(open(f"{ev}/secrets.json"))
idp = "http://dex.owui-trial.svc.cluster.local:5556/dex"
broker = "http://dex-broker.owui-trial.svc.cluster.local:5556/dex"
lines = [
    f"issuer: {broker}", "storage: {type: memory}", "web: {http: 0.0.0.0:5556}",
    "oauth2: {skipApprovalScreen: true}",
    "connectors:", "  - type: oidc", "    id: trial-idp", "    name: Trial IdP", "    config:",
    f"      issuer: {idp}", "      clientID: team-gateway", f"      clientSecret: {json.dumps(s['gateway'])}",
    f"      redirectURI: {broker}/callback", "      getUserInfo: true", "      insecureEnableGroups: true",
    "      scopes: [openid, profile, email, groups]",
    "staticClients:",
    "  - id: splunk-gateway", "    name: Splunk gateway (trial)", f"    secret: {json.dumps(s['broker_client'])}",
    "  - id: gitlab-gateway", "    name: GitLab gateway (trial)", f"    secret: {json.dumps(s['broker_client'])}",
    "    trustedPeers: [splunk-gateway]",
]
access = "urn:ietf:params:oauth:token-type:access_token"
id_token = "urn:ietf:params:oauth:token-type:id_token"
attempts = [
    {"label": "idp-local-connector", "token_url": f"{idp}/token", "client_id": "team-gateway",
     "client_secret": s["gateway"], "params": {"connector_id": "local", "subject_token_type": access,
                                              "requested_token_type": access, "scope": "openid groups"}},
    {"label": "broker-access-token", "token_url": f"{broker}/token", "client_id": "splunk-gateway",
     "client_secret": s["broker_client"], "params": {"connector_id": "trial-idp", "subject_token_type": access,
                                                    "requested_token_type": access, "scope": "openid email groups"}},
    {"label": "broker-access-token-extra-audience", "token_url": f"{broker}/token", "client_id": "splunk-gateway",
     "client_secret": s["broker_client"], "params": {
         "connector_id": "trial-idp", "subject_token_type": access, "requested_token_type": access,
         "scope": "openid email groups audience:server:client_id:gitlab-gateway"}},
    {"label": "broker-as-id-token", "token_url": f"{broker}/token", "client_id": "splunk-gateway",
     "client_secret": s["broker_client"], "params": {"connector_id": "trial-idp", "subject_token_type": id_token,
                                                    "requested_token_type": access, "scope": "openid email groups"}},
]
for name, text in (("dex-broker-config.yaml", "\n".join(lines) + "\n"),
                   ("toolprobe-exchange.json", json.dumps(attempts))):
    path = os.path.join(ev, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
PY
    k -n "$NS" create secret generic dex-broker-config --from-file=config.yaml="$EVIDENCE/dex-broker-config.yaml" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$NS" create secret generic toolprobe-exchange --from-file=config.json="$EVIDENCE/toolprobe-exchange.json" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    render "$HERE/toolprobe.yaml" | k apply -f -
    k -n "$NS" rollout restart deploy/toolprobe deploy/dex-broker
    for d in toolprobe dex-broker; do k -n "$NS" rollout status "deploy/$d" --timeout=300s; done
    grants='[{"principal_type": "user", "principal_id": "*", "permission": "read"}]'
    conns=$(printf '[{"type": "mcp", "url": "http://toolprobe.%s.svc.cluster.local:8000/mcp", "path": "", "auth_type": "system_oauth", "key": "", "config": {"enable": true, "access_grants": %s}, "info": {"id": "echo", "name": "Trial echo (MCP)", "description": "trial"}}, {"type": "openapi", "url": "http://toolprobe.%s.svc.cluster.local:8000", "path": "openapi.json", "auth_type": "system_oauth", "key": "", "config": {"enable": true, "access_grants": %s}, "info": {"id": "echo-openapi", "name": "Trial echo (OpenAPI)", "description": "trial"}}]' "$NS" "$grants" "$NS" "$grants")
    k -n "$NS" set env deploy/openwebui ENABLE_FORWARD_USER_INFO_HEADERS=true TOOL_SERVER_CONNECTIONS="$conns" \
        OAUTH_SCOPES="${TOOLPROBE_OAUTH_SCOPES:-openid email profile groups}"
    k -n "$NS" rollout status deploy/openwebui --timeout=600s
}

idp_config() {
    # Regenerate the trial IdP config (DEX_TOKEN_EXPIRY, when set, shortens token
    # lifetimes; DEX_STORAGE=sqlite3 swaps the store) and restart Dex: users sign in again.
    secrets
    k -n "$NS" rollout restart deploy/dex
    k -n "$NS" rollout status deploy/dex --timeout=300s
}

teardown() {
    "$KIND_BIN" delete cluster --name "$KIND_NAME" --kubeconfig "$KUBECONFIG"
    "$KIND_BIN" get clusters
}

case "${1:-}" in
    images) images ;;
    cluster) cluster ;;
    load) load ;;
    dnslog) dnslog ;;
    deploy) deploy ;;
    audit) audit ;;
    egress-report) egress_report ;;
    toolprobe) toolprobe ;;
    idp-config) idp_config ;;
    teardown) teardown ;;
    *) echo "usage: $0 images|cluster|deploy|audit|egress-report|toolprobe|idp-config|teardown" >&2; exit 2 ;;
esac
