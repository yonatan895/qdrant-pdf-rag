#!/bin/sh
# Open WebUI acceptance lane (#1070, #1073; ADR-0007 conditions). Deploys the
# working tree's chart (Open WebUI objects) and Rosetta Pipe next to the agent an
# e2e Kind lane already runs, then executes scripts/openwebui/acceptance.py.
#
#   sh scripts/openwebui/acceptance.sh images     before the egress boundary closes
#   sh scripts/openwebui/acceptance.sh deploy     test IdP, router stand-in, chart objects
#   sh scripts/openwebui/acceptance.sh run        port-forwards, provision, the checks
#   sh scripts/openwebui/acceptance.sh team-tools after run: the Splunk gateway (#1077) against a fake
#                                                 Splunk pod, Open WebUI re-rendered with teamTools, the
#                                                 ADR-0010 T4 checks (kind-splunk.yaml, docs/deploy.md)
#
# Operator-deploy lane (#1118 B3): the chart objects come from the operator's own
# airgap:pipeline/airgap:deploy of the bootstrapped bundle, never from this script.
# These steps stand in only for the OpenShift platform and the site's people:
#
#   ... operator images    before the boundary: the test IdP and router stand-in images
#   ... operator site-env  print the OPENWEBUI_* airgap.env lines for the stand-in IdP
#   ... operator prepare   Route API, service CA, serving-certificate Secret, restricted-v2 UID
#                          (kind-platform.yaml); the test IdP; the operator Secret (site step 2)
#   ... operator admin     after the first deploy: serve the stored Route from the router
#                          stand-in, prove no Pipe yet, put the first admin API key into the
#                          Secret as admin-api-key (site step 4; the next airgap:deploy installs)
#   ... operator run       groups and the model grant (site step 5), then the checks
#   ... operator record    before the K1 rollback: the chats and the claim
#   ... operator off       after OPENWEBUI_ENABLED=false + airgap:deploy: objects gone, claim kept
#   ... operator on        after OPENWEBUI_ENABLED=true + airgap:deploy: same claim, chats, Pipe
#   ... operator copy-out  after record: the copy-out block of the workspace's docs/deploy.md
#                          (#owui-copy-out), run as written; the copy matches the pod, mode 0600
#   ... operator copy-later after the next airgap:deploy (B's stand-in): a chat the copy lacks
#   ... operator restore   the restore block (#owui-restore), run as written; webui.db matches
#   ... operator restored  after A's airgap:deploy: as `on`, and the copy-later chat is gone
#   ... operator paced     CI stand-in models only: one marked question on the browser, API-chat
#                          and API-stream paths at once against the slow-model stand-in
#                          (scripts/mock_vllm.py MOCK_PACED_*): complete, cited, and timed
#
#   OWUI_WORKSPACE  (operator mode) the bootstrapped workspace the deploy runs from; its
#                   chart's docs_pipe.py is the Pipe the checks compare against
#   The chart renders https://$OWUI_HOST with no port, so operator mode forwards the router
#   on 127.0.0.1:443 (the caller allows binding it) and OWUI_PORT must stay 443.
#
# Inputs (no defaults for the first three, so a typo cannot touch another cluster):
#   KIND_NAME   the Kind cluster to use (its node container is $KIND_NAME-control-plane)
#   APP_NS      the namespace of the running agent (Service rag-agent on 8080)
#   OWUI_STATE  private state directory (mode 700): passwords, keys, certificates, logs
#   KIND_BIN KUBECTL HELM  tool paths (default: from PATH); PY (default python3)
#   OWUI_PORT   local port of the router stand-in (default 8443), DEX_PORT (default 5556)
#
# Everything is created by this script in the named cluster; nothing outside it
# is touched. Owner doc: docs/openwebui-dev.md.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/../.." && pwd)
KIND_NAME=${KIND_NAME:?set KIND_NAME}
APP_NS=${APP_NS:?set APP_NS to the agent namespace}
STATE=${OWUI_STATE:?set OWUI_STATE to a private directory}
KIND_BIN=${KIND_BIN:-kind}
KUBECTL=${KUBECTL:-kubectl}
HELM=${HELM:-helm}
PY=${PY:-python3}
NODE="$KIND_NAME-control-plane"
OWUI_HOST=owui.localhost
DEX_HOST=dex.localhost
CMD=${1:-}
if [ "$CMD" = operator ]; then OWUI_PORT=${OWUI_PORT:-443}; else OWUI_PORT=${OWUI_PORT:-8443}; fi
DEX_PORT=${DEX_PORT:-5556}
POD_CIDR=10.244.0.0/16
OWUI_IMAGE=${OWUI_IMAGE:-ghcr.io/yonatan895/qdrant-pdf-rag-openwebui:v0.13.4-slim}
DEX_IMAGE=trial.local/dex:v2.46.0-distroless
NGINX_IMAGE=trial.local/nginx-unprivileged:1.30.6-alpine
OWUI_URL="https://$OWUI_HOST"
[ "$OWUI_PORT" = 443 ] || OWUI_URL="https://$OWUI_HOST:$OWUI_PORT"
DEX_ISSUER="https://$DEX_HOST:$DEX_PORT/dex"
export OWUI_URL DEX_ISSUER KIND_NAME APP_NS KUBECTL
PYH="python3 -I $HERE/client.py"
DRYRUN=0

say() { echo "==> $*"; }
plan() { echo "[dry-run] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
k() { "$KUBECTL" --context "kind-$KIND_NAME" "$@"; }

# shellcheck source=lib.sh
. "$HERE/lib.sh"

prepare_state() {
    mkdir -p "$STATE" "$STATE/logs" "$STATE/render"
    chmod 700 "$STATE"
}

images() {
    prepare_state
    docker inspect "$NODE" >/dev/null 2>&1 || die "no Kind node container $NODE"
    build_images
    load_images
}

deploy() {
    prepare_state
    k get namespace "$APP_NS" >/dev/null || die "namespace $APP_NS does not exist"
    k -n "$APP_NS" get service rag-agent >/dev/null || die "no Service rag-agent in $APP_NS: the lane's agent is not deployed"
    k -n "$APP_NS" rollout status deploy/rag-agent --timeout=300s
    coredns_rewrite
    pki
    $PYH secrets "$STATE"
    bcrypt_hashes
    render_chart "$STATE/render"
    set -- $(kind_net)
    render_support "$STATE/render/support.yaml" "$2" "$(sha256sum "$STATE/dex-config.yaml" | cut -c1-16)"
    check_render "$STATE/render"
    k apply -f "$STATE/render/support.yaml" > "$STATE/logs/apply-support.log"
    apply_stack
    k -n owui-idp rollout status deploy/dex --timeout=300s
    k -n "$APP_NS" rollout status deploy/rag-openwebui --timeout=900s
    k -n owui-router rollout status deploy/router --timeout=300s
}

PF_PIDS=""
stop_forwards() {  # end every forward and wait for it, so the next phase can bind the same ports
    for p in $PF_PIDS; do kill "$p" 2>/dev/null || true; done
    for p in $PF_PIDS; do wait "$p" 2>/dev/null || true; done
}

forward() {  # forward NAMESPACE SERVICE LOCAL REMOTE
    # kubectl itself, not the k() function: a backgrounded function is a subshell under dash, and
    # killing it left kubectl running and holding the port, so a later phase's forward could not
    # bind and the old one died with the router pod it pointed at (#1118, run 40772148925).
    "$KUBECTL" --context "kind-$KIND_NAME" -n "$1" port-forward --address 127.0.0.1 "svc/$2" "$3:$4" \
        > "$STATE/logs/forward-$2.log" 2>&1 &
    PF_PIDS="$PF_PIDS $!"
}

forwards_alive() {  # a forward that exited (port in use, no pod) is reported, not waited out
    for p in $PF_PIDS; do
        kill -0 "$p" 2>/dev/null || die "a port-forward exited; see $STATE/logs/forward-*.log"
    done
}

run() {
    prepare_state
    trap stop_forwards EXIT
    forward owui-router router "$OWUI_PORT" 8443
    forward owui-idp dex "$DEX_PORT" "$DEX_PORT"
    i=0
    until $PYH health "$STATE" >/dev/null 2>&1; do
        forwards_alive
        i=$((i + 1)); [ "$i" -lt 60 ] || { $PYH health "$STATE" || true; die "Open WebUI or the test IdP is not reachable through the port-forwards"; }
        sleep 2
    done
    provision_live
    python3 -I "$HERE/acceptance.py" "$STATE" > "$STATE/logs/acceptance.json" || rc=$?
    cat "$STATE/logs/acceptance.json"
    exit "${rc:-0}"
}

# ------------------------------------------------------------------ team tools (#1077)
# After deploy and run: the chart's Splunk gateway (bearer mode, B2) against a fake Splunk
# pod, Open WebUI re-rendered with the team-tools values, then acceptance.py team-tools
# (the gateway start-up check, Open WebUI's MCP handshake and a tool call through the
# NetworkPolicies, a pod outside Open WebUI refused, and ADR-0010 T4 as far as Kind
# automates it). Site procedure: docs/deploy.md#splunk-gateway-deploy.
TEAM_GROUP=internal-team  # team-user's IdP group (client.py USERS)

team_pki() {  # team_pki DIR: the fake Splunk's serving certificate from the run's CA (it admits .svc names)
    d="$STATE/pki"; name=fake-splunk.owui-splunk.svc
    printf 'subjectAltName=DNS:%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n' \
        "$name" > "$1/fake-splunk.ext"
    openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$name" \
        -keyout "$1/fake-splunk.key" -out "$1/fake-splunk.csr" 2>/dev/null
    openssl x509 -req -in "$1/fake-splunk.csr" -CA "$d/ca.crt" -CAkey "$d/ca.key" -CAcreateserial \
        -days 2 -extfile "$1/fake-splunk.ext" -out "$1/fake-splunk.crt" 2>/dev/null
    openssl verify -CAfile "$d/ca.crt" -verify_hostname "$name" "$1/fake-splunk.crt" >/dev/null ||
        die "the fake Splunk certificate does not verify against the run's CA"
    chmod 600 "$1/fake-splunk.key"
}

team_inputs() {  # team_inputs DIR: the Secret values (files, mode 600, never argv) and Open WebUI's complete CA bundle
    ( umask 077
      $PY -I -c 'import secrets; print(secrets.token_hex(24), end="")' > "$1/splunk-mcp-token"
      $PY -I -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > "$1/bearer-secret"
      k -n "$APP_NS" get secret test-gateway-keys -o 'jsonpath={.data.llm-api-key}' | base64 -d > "$1/openwebui-llm-api-key"
      # Stands in for a shared GATEWAY_API_KEY_SECRET_KEY: no LiteLLM key at all, so a Team model that
      # read it could not answer (maintainer decision 3 on #1374).
      printf 'sk-lane-shared-key-not-a-litellm-key' > "$1/api-key"
      k -n "$APP_NS" get configmap test-gateway-ca -o 'jsonpath={.data.ca-bundle\.crt}' > "$1/ca-bundle.crt" )
    [ -s "$1/openwebui-llm-api-key" ] || die "Secret test-gateway-keys has no llm-api-key"
    grep -q 'BEGIN CERTIFICATE' "$1/ca-bundle.crt" || die "ConfigMap test-gateway-ca has no ca-bundle.crt"
    printf '\n' >> "$1/ca-bundle.crt"
    cat "$STATE/pki/ca.crt" >> "$1/ca-bundle.crt"
}

team_values() {  # team_values DIR AGENT_IMAGE GROUP_ID: the teamTools values (JSON is YAML) on top of values.yaml
    "$PY" -I - "$1" "$2" "$3" "$POD_CIDR" "$REPO/deploy/gateways/splunk-gateway.example.json" "$STATE/pki/ca.crt" <<'PY'
import json, sys
team, image, group, pod_cidr, example, ca = sys.argv[1:7]
with open(example, encoding="utf-8") as fh:
    config = json.load(fh)
config.pop("auth")  # bearer mode (B2): the gateway sees one caller, service:openwebui
config["backend"]["base_url"] = "https://fake-splunk.owui-splunk.svc:8089"
if "@" in image:
    repo, digest = image.split("@", 1)
    agent = {"repository": repo, "digest": digest}
else:
    repo, _, tag = image.rpartition(":")
    agent = {"repository": repo, "tag": tag}
agent["pullPolicy"] = "IfNotPresent"  # the lane's agent image, already on the node
with open(ca, encoding="utf-8") as fh:
    ca_pem = fh.read()
print(json.dumps({
    "images": {"agent": agent},
    "models": {"reasoning": {"baseUrl": "https://test-gateway:4000/v1", "model": "mock-reasoning"}},
    "gateway": {"apiKeySecretName": "rag-owui-team-llm", "apiKeySecretKey": "api-key",
                "caConfigMapName": "rag-owui-team-ca"},
    "teamTools": {
        "groupId": group,
        # NetworkPolicy matches the pod address and port after Service translation.
        "model": {"egress": {"cidrs": [pod_cidr], "port": 4000}},
        "splunk": {"enabled": True, "authMode": "bearer", "secretName": "rag-splunk-gateway",
                   "config": json.dumps(config, indent=1), "caBundle": ca_pem,
                   "splunkEgress": {"cidrs": [pod_cidr], "port": 8089}},
    },
}, indent=1))
PY
}

check_team_render() {  # check_team_render RENDER_DIR TEAM_DIR AGENT_IMAGE
    "$PY" -I - "$1" "$2" "$3" <<'PY'
import json, sys, yaml
d, team, image = sys.argv[1:4]
with open(f"{d}/openwebui.yaml", encoding="utf-8") as fh:
    text = fh.read()
docs = [x for x in yaml.safe_load_all(text) if x]
kinds = sorted(f"{x['kind']}/{x['metadata']['name']}" for x in docs)
want = sorted(["ConfigMap/rag-openwebui-pipe", "ConfigMap/rag-openwebui-proxy", "Deployment/rag-openwebui",
               "NetworkPolicy/rag-openwebui", "PersistentVolumeClaim/rag-openwebui-data", "Service/rag-openwebui",
               "ConfigMap/rag-splunk-gateway", "Deployment/rag-splunk-gateway", "NetworkPolicy/rag-splunk-gateway",
               "Service/rag-splunk-gateway"])
assert kinds == want, kinds
for name in ("splunk-mcp-token", "bearer-secret", "openwebui-llm-api-key"):
    with open(f"{team}/{name}", encoding="utf-8") as fh:
        assert fh.read().strip() not in text, f"the {name} value is in the render"
def deployment(name):
    return next(x for x in docs if x["kind"] == "Deployment" and x["metadata"]["name"] == name)["spec"]["template"]["spec"]
owui = deployment("rag-openwebui")
env = {e["name"]: e for e in owui["containers"][0]["env"]}
assert "ca-trust" not in [c["name"] for c in owui.get("initContainers") or []]
assert env["SSL_CERT_FILE"]["value"] == "/etc/gateway-ca/ca-bundle.crt"
assert env["OPENAI_API_KEYS"]["valueFrom"]["secretKeyRef"] == {"name": "rag-owui-team-llm", "key": "openwebui-llm-api-key"}
(conn,) = json.loads(env["TOOL_SERVER_CONNECTIONS"]["value"])
assert conn["auth_type"] == "bearer" and conn["key"] == "$(MFR_SPLUNK_GATEWAY_KEY)", "tool server entry"
assert deployment("rag-splunk-gateway")["containers"][0]["image"] == image
print(f"team render: {len(docs)} objects ({', '.join(kinds)}); the gateway runs the lane's agent image; "
      "Open WebUI reads openwebui-llm-api-key beside a shared api-key; no secret value in the render")
PY
}

team_tools() {
    prepare_state
    team_dir="$STATE/team"
    mkdir -p "$team_dir"; chmod 700 "$team_dir"
    k -n "$APP_NS" get deploy rag-openwebui >/dev/null || die "no deploy/rag-openwebui: run 'deploy' and 'run' first"
    AGENT_IMAGE=$(k -n "$APP_NS" get deploy rag-agent -o 'jsonpath={.spec.template.spec.containers[0].image}')
    [ -n "$AGENT_IMAGE" ] || die "cannot read the image of deploy/rag-agent"
    team_pki "$team_dir"
    team_inputs "$team_dir"
    set -- $(kind_net)
    sed -e "s#@APP_NS@#$APP_NS#g" -e "s#@NODE_IP@#$2#g" -e "s#@AGENT_IMAGE@#$AGENT_IMAGE#g" \
        -e "s#@FAKE_UID@#$(uid_for fake-splunk)#g" -e "s#@INTRUDER_UID@#$(uid_for intruder)#g" \
        "$HERE/kind-splunk.yaml" > "$STATE/render/splunk.yaml"
    k apply -f "$STATE/render/splunk.yaml" > "$STATE/logs/apply-splunk.log"
    k -n owui-splunk create secret tls fake-splunk-tls --cert="$team_dir/fake-splunk.crt" --key="$team_dir/fake-splunk.key" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n owui-splunk create secret generic fake-splunk-token --from-file="$team_dir/splunk-mcp-token" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n owui-splunk create configmap fake-splunk-code --from-file="$REPO/tests/fake_splunk_mcp.py" \
        --from-file="$REPO/scripts/ci/fake_splunk_mcp_server.py" --dry-run=client -o yaml | k apply -f - >/dev/null
    # Site procedure step 4: the gateway Secret, and Open WebUI's own LiteLLM key beside a shared one.
    k -n "$APP_NS" create secret generic rag-splunk-gateway --from-file="$team_dir/splunk-mcp-token" \
        --from-file="$team_dir/bearer-secret" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$APP_NS" create secret generic rag-owui-team-llm --from-file="$team_dir/openwebui-llm-api-key" \
        --from-file="$team_dir/api-key" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$APP_NS" create configmap rag-owui-team-ca --from-file="$team_dir/ca-bundle.crt" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n owui-splunk rollout status deploy/fake-splunk --timeout=300s
    k -n "$APP_NS" wait --for=condition=Ready pod/team-tools-intruder --timeout=300s >/dev/null
    start_forwards
    # Site procedure step 6: the team group's id (v0.13.4 matches tool-server grants by id only).
    gid=$($PYH group-id "$STATE" "$TEAM_GROUP") || die "cannot read the id of group $TEAM_GROUP"
    team_values "$team_dir" "$AGENT_IMAGE" "$gid" > "$team_dir/values.json"
    TEAM_VALUES="$team_dir/values.json" render_chart "$STATE/render-team"
    check_team_render "$STATE/render-team" "$team_dir" "$AGENT_IMAGE"
    # Site procedure step 7 (enable), as the chart renders it.
    k apply -f "$STATE/render-team/openwebui.yaml" > "$STATE/logs/apply-team.log"
    k -n "$APP_NS" rollout status deploy/rag-openwebui --timeout=900s
    if ! k -n "$APP_NS" rollout status deploy/rag-splunk-gateway --timeout=330s; then
        k -n "$APP_NS" logs deploy/rag-splunk-gateway --tail=20 >&2 || true
        die "the Splunk gateway is not Ready against the fake Splunk (its splunk_gateway_start line is above)"
    fi
    wait_health
    phase team-tools
}

# ------------------------------------------------------------------ operator-deploy lane
require_workspace() {
    WS=${OWUI_WORKSPACE:?set OWUI_WORKSPACE to the bootstrapped operator workspace}
    [ -f "$WS/charts/mainframe-rag/files/openwebui/docs_pipe.py" ] || die "no chart Pipe under $WS"
    [ "$OWUI_PORT" = 443 ] || die "operator mode needs OWUI_PORT=443: the chart renders https://$OWUI_HOST with no port"
}

wait_health() {
    i=0
    until $PYH health "$STATE" >/dev/null 2>&1; do
        forwards_alive
        i=$((i + 1)); [ "$i" -lt 60 ] || { $PYH health "$STATE" || true; die "Open WebUI or the test IdP is not reachable through the port-forwards"; }
        sleep 2
    done
}

start_forwards() {
    trap stop_forwards EXIT
    forward owui-router router "$OWUI_PORT" 8443
    forward owui-idp dex "$DEX_PORT" "$DEX_PORT"
    wait_health
}

phase() {  # phase NAME: acceptance.py NAME; JSON kept in logs/NAME.json; the verdicts' exit status
    rc=0
    python3 -I "$HERE/acceptance.py" "$STATE" "$1" > "$STATE/logs/$1.json" || rc=$?
    cat "$STATE/logs/$1.json"
    return "$rc"
}

support() {  # render and apply the IdP, router stand-in and kubelet-probe policy (ROUTE_TIMEOUT: the Route's)
    set -- $(kind_net)
    render_support "$STATE/render/support.yaml" "$2" "$(sha256sum "$STATE/dex-config.yaml" | cut -c1-16)"
    k apply -f "$STATE/render/support.yaml" > "$STATE/logs/apply-support.log"
}

operator_images() {
    prepare_state
    docker inspect "$NODE" >/dev/null 2>&1 || die "no Kind node container $NODE"
    # Only the stand-ins: the Open WebUI and proxy images reach the cluster from the bundle (airgap:load).
    copy_into_node "$(pin DEX_PIN)" "$DEX_IMAGE" dex
    copy_into_node "$(nginx_ref)@$(nginx_pin)" "$NGINX_IMAGE" nginx
    docker exec "$NODE" crictl images > "$STATE/node-images.txt"
}

operator_site_env() {
    # The site's OPENWEBUI_* keys (airgap.env.example) with the stand-in IdP's values.
    cat <<EOF
OPENWEBUI_ENABLED=true
OPENWEBUI_SECRET=rag-openwebui
OPENWEBUI_HOST=$OWUI_HOST
OPENWEBUI_OIDC_PROVIDER_URL=$DEX_ISSUER/.well-known/openid-configuration
OPENWEBUI_OIDC_CLIENT_ID=open-webui
OPENWEBUI_OIDC_GROUP_CLAIM=groups
OPENWEBUI_OIDC_ROLES_CLAIM=groups
OPENWEBUI_OIDC_ALLOWED_ROLES=internal-team,docs-readers,owui-admins
OPENWEBUI_OIDC_ADMIN_ROLES=owui-admins
OPENWEBUI_IDP_EGRESS_CIDRS=$POD_CIDR
OPENWEBUI_IDP_EGRESS_PORT=5556
OPENWEBUI_IDP_CA_FILE=$STATE/pki/ca.crt
OPENWEBUI_RETENTION_DAYS=30
OPENWEBUI_STORAGE_SIZE=1Gi
EOF
}

operator_prepare() {
    prepare_state
    require_workspace
    k get namespace "$APP_NS" >/dev/null || die "namespace $APP_NS does not exist"
    coredns_rewrite
    pki
    $PYH secrets "$STATE"
    OWUI_HASHER=htpasswd bcrypt_hashes
    support
    pk="$STATE/pki"
    k -n owui-idp create secret generic dex-config --from-file=config.yaml="$STATE/dex-config.yaml" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n owui-idp create secret tls dex-tls --cert="$pk/dex.crt" --key="$pk/dex.key" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n owui-router create secret tls router-tls --cert="$pk/router.crt" --key="$pk/router.key" --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n owui-idp rollout restart deploy/dex >/dev/null
    # Platform stand-ins (kind-platform.yaml): the Route API and restricted-v2's UID assignment.
    sed -e "s#@APP_NS@#$APP_NS#g" -e "s#@OWUI_UID@#$(uid_for openwebui)#g" \
        "$HERE/kind-platform.yaml" > "$STATE/render/platform.yaml"
    k apply -f "$STATE/render/platform.yaml" > "$STATE/logs/apply-platform.log"
    k wait --for=condition=Established crd/routes.route.openshift.io --timeout=60s >/dev/null
    # The service CA operator's outputs: the namespace CA bundle and the Service's serving certificate.
    k -n "$APP_NS" create configmap openshift-service-ca.crt --from-file=service-ca.crt="$pk/ca.crt" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$APP_NS" create secret tls rag-openwebui-tls --cert="$pk/owui-svc.crt" --key="$pk/owui-svc.key" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    # Site procedure step 2: the operator Secret (webui-secret-key, oidc-client-secret; files, never argv).
    k -n "$APP_NS" create secret generic rag-openwebui --from-file="$STATE/owui-secret" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    sha256sum "$WS/charts/mainframe-rag/files/openwebui/docs_pipe.py" | cut -d' ' -f1 > "$STATE/render/pipe.sha256"
    k -n owui-idp rollout status deploy/dex --timeout=300s
}

admit_route() {  # the router stand-in serves the stored Route: its host, destination CA and timeout
    k -n "$APP_NS" get route.route.openshift.io/rag-openwebui -o json > "$STATE/render/route.json" ||
        die "no Route rag-openwebui: the operator deploy did not create it"
    ROUTE_TIMEOUT=$(route_for_router "$STATE/render/route.json" "$OWUI_HOST" "$STATE/render/route-ca.crt") ||
        die "the stored Route rag-openwebui cannot be served by the router stand-in"
    export ROUTE_TIMEOUT
    k -n owui-router create configmap trial-ca --from-file=ca.crt="$STATE/render/route-ca.crt" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    support
    # nginx resolves the Service at start; a recreated Service (rollback on) needs a new router pod.
    k -n owui-router rollout restart deploy/router >/dev/null
    k -n owui-router rollout status deploy/router --timeout=300s
}

operator_admin() {
    prepare_state
    require_workspace
    admit_route
    start_forwards
    phase first-pass
    $PYH api-key "$STATE"
    # Site procedure step 4: the key goes into the operator Secret as admin-api-key (file, never argv).
    tr -d '\n' < "$STATE/admin-api-key" > "$STATE/owui-secret/admin-api-key"
    chmod 600 "$STATE/owui-secret/admin-api-key"
    k -n "$APP_NS" create secret generic rag-openwebui --from-file="$STATE/owui-secret" \
        --dry-run=client -o yaml | k apply -f - >/dev/null
    echo "==> admin-api-key added to Secret rag-openwebui; the next airgap:deploy installs the docs Pipe"
}

# Copy-out and restore (#1376): the operator's own blocks from the workspace's docs/deploy.md,
# run as written in a separate `sh -e` with kubectl for oc, the lane's namespace as NAMESPACE
# and a private directory under $STATE as A's workspace (the copy holds chat text; never upload it).
COPY_DIR="$STATE/owui-copy"
docs_block() {  # docs_block ANCHOR: the first sh fence after <a id="ANCHOR"></a> in the workspace's deploy.md
    awk -v a="<a id=\"$1\"></a>" '$0 == a {f = 1; next} f && !p && /^<a id=/ {exit} f && $0 == "```sh" {p = 1; next} p && $0 == "```" {exit} p' \
        "$WS/docs/deploy.md"
}

run_docs_block() {  # run_docs_block ANCHOR: the block's output goes to logs/ANCHOR.log
    docs_block "$1" > "$STATE/render/$1.block"
    [ -s "$STATE/render/$1.block" ] || die "no sh block after #$1 in $WS/docs/deploy.md"
    { echo 'set -eu'; echo 'oc() { "$KUBECTL" --context "kind-$KIND_NAME" "$@"; }'; cat "$STATE/render/$1.block"; } \
        > "$STATE/render/$1.sh"
    mkdir -p "$COPY_DIR"
    chmod 700 "$COPY_DIR"
    rc=0
    NAMESPACE=$APP_NS A_WORKSPACE=$COPY_DIR sh "$STATE/render/$1.sh" > "$STATE/logs/$1.log" 2>&1 || rc=$?
    cat "$STATE/logs/$1.log"
    [ "$rc" = 0 ] || die "the deploy.md block #$1 failed (exit $rc)"
}

copy_db_sha() {  # the webui.db sha256 the copy-out read inside the pod
    sed -n 's/^\([0-9a-f]\{64\}\)  webui\.db$/\1/p' "$COPY_DIR/owui-db-A.sha256.pod"
}

open_webui_down() {  # $1: the phase; no Open WebUI pod and no helper pod is left (an unreadable list fails)
    pods=$(k -n "$APP_NS" get pod -l app=rag-openwebui -o name) || die "$1: cannot list the Open WebUI pods"
    [ -z "$pods" ] || die "$1: an Open WebUI pod still runs"
    pods=$(k -n "$APP_NS" get pod rag-openwebui-copy --ignore-not-found -o name) || die "$1: cannot read the helper pod"
    [ -z "$pods" ] || die "$1: the helper pod is left"
}

operator_copy_out() {
    prepare_state
    require_workspace
    run_docs_block owui-copy-out
    want=$(copy_db_sha)
    got=$(tar -xOf "$COPY_DIR/owui-data-A.tar" ./webui.db | sha256sum | cut -c1-64)
    [ -n "$want" ] && [ "$want" = "$got" ] || die "copy-out: the webui.db sha256 in the tar differs from the pod's"
    [ "$(stat -c %a "$COPY_DIR/owui-data-A.tar")" = 600 ] || die "copy-out: the copy is not mode 0600"
    open_webui_down copy-out
    say "copy-out: webui.db sha256 equal in the pod and the copy ($(wc -c < "$COPY_DIR/owui-data-A.tar") bytes, mode 0600); Open WebUI is down"
}

operator_restore() {
    prepare_state
    require_workspace
    [ -s "$COPY_DIR/owui-data-A.tar" ] || die "restore: no copy in $COPY_DIR (run operator copy-out first)"
    run_docs_block owui-restore
    want=$(copy_db_sha)
    [ -n "$want" ] && grep -qx "$want  webui.db" "$STATE/logs/owui-restore.log" \
        || die "restore: the restored webui.db differs from the copy"
    open_webui_down restore
    say "restore: webui.db sha256 equal to the copy's; Open WebUI is down until the deploy"
}

operator_run() {
    prepare_state
    require_workspace
    start_forwards
    $PYH access "$STATE"
    for u in team-user docs-user; do $PYH login "$STATE" "$u"; done
    phase checks
}

operator() {
    case "${1:-}" in
        images) operator_images ;;
        site-env) operator_site_env ;;
        prepare) operator_prepare ;;
        admin) operator_admin ;;
        run) operator_run ;;
        record) prepare_state; require_workspace; start_forwards; phase record ;;
        off) prepare_state; require_workspace; phase rollback-off ;;
        on) prepare_state; require_workspace; admit_route; start_forwards; phase rollback-on ;;
        copy-out) operator_copy_out ;;
        copy-later) prepare_state; require_workspace; admit_route; start_forwards; phase copy-later ;;
        restore) operator_restore ;;
        restored) prepare_state; require_workspace; admit_route; start_forwards; phase restored ;;
        paced) prepare_state; require_workspace; start_forwards; phase paced ;;
        *) sed -n '2,/^set -eu/p' "$0" | sed '$d' >&2; exit 2 ;;
    esac
}

case "$CMD" in
    images) images ;;
    deploy) deploy ;;
    run) run ;;
    team-tools) team_tools ;;
    operator) shift; operator "$@" ;;
    *) sed -n '2,/^set -eu/p' "$0" | sed '$d' >&2; exit 2 ;;
esac
