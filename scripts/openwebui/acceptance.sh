#!/bin/sh
# Open WebUI acceptance lane (#1070, #1073; ADR-0007 conditions). Deploys the
# working tree's chart (Open WebUI objects) and Docs Pipe next to the agent an
# e2e Kind lane already runs, then executes scripts/openwebui/acceptance.py.
#
#   sh scripts/openwebui/acceptance.sh images     before the egress boundary closes
#   sh scripts/openwebui/acceptance.sh deploy     test IdP, router stand-in, chart objects
#   sh scripts/openwebui/acceptance.sh run        port-forwards, provision, the checks
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
OWUI_PORT=${OWUI_PORT:-8443}
DEX_PORT=${DEX_PORT:-5556}
POD_CIDR=10.244.0.0/16
OWUI_IMAGE=${OWUI_IMAGE:-mainframe-rag/openwebui-dev:0.12.3-slim}
DEX_IMAGE=trial.local/dex:v2.46.0-distroless
NGINX_IMAGE=trial.local/nginx-unprivileged:1.30.6-alpine
OWUI_URL="https://$OWUI_HOST"
[ "$OWUI_PORT" = 443 ] || OWUI_URL="https://$OWUI_HOST:$OWUI_PORT"
DEX_ISSUER="https://$DEX_HOST:$DEX_PORT/dex"
export OWUI_URL DEX_ISSUER KIND_NAME APP_NS KUBECTL
PYH="python3 -I $HERE/client.py"
DRYRUN=0
CMD=${1:-}

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

case "$CMD" in
    images) images ;;
    deploy) deploy ;;
    run) run ;;
    *) sed -n '2,/^set -eu/p' "$0" | sed '$d' >&2; exit 2 ;;
esac
