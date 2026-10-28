#!/bin/sh
# Open WebUI acceptance lane (#1070, #1073; ADR-0007 conditions). Deploys the
# working tree's chart (Open WebUI objects) and Rosetta Pipe next to the agent an
# e2e Kind lane already runs, then executes scripts/openwebui/acceptance.py.
#
#   sh scripts/openwebui/acceptance.sh images     before the egress boundary closes
#   sh scripts/openwebui/acceptance.sh deploy     test IdP, router stand-in, chart objects
#   sh scripts/openwebui/acceptance.sh run        port-forwards, provision, the checks
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
OWUI_IMAGE=${OWUI_IMAGE:-ghcr.io/yonatan895/qdrant-pdf-rag-openwebui:v0.12.3-slim}
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
stop_forwards() {
    for p in $PF_PIDS; do kill "$p" 2>/dev/null || true; done
}

forward() {  # forward NAMESPACE SERVICE LOCAL REMOTE
    k -n "$1" port-forward --address 127.0.0.1 "svc/$2" "$3:$4" > "$STATE/logs/forward-$2.log" 2>&1 &
    PF_PIDS="$PF_PIDS $!"
}

run() {
    prepare_state
    trap stop_forwards EXIT
    forward owui-router router "$OWUI_PORT" 8443
    forward owui-idp dex "$DEX_PORT" "$DEX_PORT"
    i=0
    until $PYH health "$STATE" >/dev/null 2>&1; do
        i=$((i + 1)); [ "$i" -lt 60 ] || { $PYH health "$STATE" || true; die "Open WebUI or the test IdP is not reachable through the port-forwards"; }
        sleep 2
    done
    provision_live
    python3 -I "$HERE/acceptance.py" "$STATE" > "$STATE/logs/acceptance.json" || rc=$?
    cat "$STATE/logs/acceptance.json"
    exit "${rc:-0}"
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
        paced) prepare_state; require_workspace; start_forwards; phase paced ;;
        *) sed -n '2,/^set -eu/p' "$0" | sed '$d' >&2; exit 2 ;;
    esac
}

case "$CMD" in
    images) images ;;
    deploy) deploy ;;
    run) run ;;
    operator) shift; operator "$@" ;;
    *) sed -n '2,/^set -eu/p' "$0" | sed '$d' >&2; exit 2 ;;
esac
