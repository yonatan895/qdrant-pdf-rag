#!/bin/sh
# AIR-GAP SIDE (issue #15): deploy Qdrant (vendored chart, PROD sizing) and the
# agent (first-party Helm chart) into $NAMESPACE, then wait for Ready.
#
#   sh scripts/tools/run-task.sh airgap:deploy
#
# Prod Qdrant: 3 replicas / 500Gi RWO block / unprivileged / ClusterIP, no
# Route (overlays/openshift/values.yaml is never shrunk). No NFS. No Cloud.

. "$(dirname -- "$0")/common.sh"

enforce_product_rules
resolve_aliases
require_env INTERNAL_REGISTRY NAMESPACE STORAGE_CLASS EMBED_MODEL DENSE_DIM EMBED_MODEL_REVISION VLLM_BASE_URL
require_embed_revision
check_secret_name "${GATEWAY_API_KEY_SECRET:-}" GATEWAY_API_KEY_SECRET
check_secret_name "${PULL_SECRET:-}" PULL_SECRET
check_secret_name "${GATEWAY_CA_CONFIGMAP:-}" GATEWAY_CA_CONFIGMAP
resolve_otel_endpoint
case "$IMAGE_SHA" in
    ""|HEAD) die "IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)" ;;
esac
# Cross-check against the packed MANIFEST when it is reachable (dist/ or ../).
MANIFEST=$(find_manifest)
check_manifest_sha
require_kc
command -v helm >/dev/null 2>&1 || die "helm is required on the air-gap bastion"

# Operator console Route (ADR-0004): rendering the OAuth sidecar and
# the reencrypt Route needs the oauth-proxy image pin to be recorded, not the
# sha256:PENDING placeholder.
AGENT_ROUTE=${AGENT_ROUTE:-false}
if [ "$AGENT_ROUTE" = "true" ]; then
    pin_recorded oauth-proxy || die "AGENT_ROUTE=true needs the oauth-proxy digest recorded in images.txt (currently sha256:PENDING); record it on the connected host and repack"
fi

refuse_nfs_storage
SNAPSHOT_STORAGE_CLASS=${SNAPSHOT_STORAGE_CLASS:-$STORAGE_CLASS}
# Chart appends "-unprivileged" to the tag when useUnprivilegedImage=true;
# values.yaml pins v1.19.0 — set it explicitly so it always matches load.sh.
# Strip a "-unprivileged" suffix from the pin: the chart re-adds it itself.
QDRANT_TAG=${QDRANT_TAG:-$(echo "${QDRANT_IMAGE:-docker.io/qdrant/qdrant:v1.19.0-unprivileged}" | sed "s/.*://; s/-unprivileged\$//")}
QDRANT_URL="http://${QDRANT_RELEASE}:6333"

KC=${KC:-$(kc)}
check_gateway_ca
mkdir -p dist

# Public namespace service CA becomes a generated value, never a YAML patch.
if [ "$AGENT_ROUTE" = "true" ] && [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    require_secret_keys rag-agent-oauth-cookie cookie-secret
    ROUTE_DESTINATION_CA_FILE=dist/namespace-service-ca.crt
    $KC -n "$NAMESPACE" get configmap openshift-service-ca.crt \
        -o 'jsonpath={.data.service-ca\.crt}' > "$ROUTE_DESTINATION_CA_FILE" || die "cannot read namespace service CA"
fi
map_app_values --out dist/mainframe-rag-release-values.yaml --without-ingest-job
helm lint charts/mainframe-rag -f dist/mainframe-rag-release-values.yaml
# Clear only this stage's generated render directory; disabled templates
# must not leave stale evidence from an earlier invocation.
rm -rf dist/app-helm-render
helm template mainframe-rag charts/mainframe-rag -f dist/mainframe-rag-release-values.yaml \
    --namespace "$NAMESPACE" --output-dir dist/app-helm-render
cat dist/app-helm-render/mainframe-rag/templates/*.yaml > dist/agent-rendered.yaml
fail_on_placeholders dist/agent-rendered.yaml agent
check_agent_qdrant_key dist/agent-rendered.yaml agent
cp dist/agent-rendered.yaml dist/agent-chart-rendered.yaml
rm -f dist/jaeger-rendered.yaml dist/servicemonitor-rendered.yaml dist/agent-route.yaml
if [ "$OTEL_TRACING_ENABLED" = "1" ]; then
    cat dist/app-helm-render/mainframe-rag/templates/jaeger-*.yaml > dist/jaeger-rendered.yaml
fi
if [ "${METRICS_ENABLED:-false}" = "true" ]; then
    cp dist/app-helm-render/mainframe-rag/templates/servicemonitor.yaml dist/servicemonitor-rendered.yaml
fi
if [ "$AGENT_ROUTE" = "true" ]; then
    cp dist/app-helm-render/mainframe-rag/templates/route.yaml dist/agent-route.yaml
fi

require_secret_keys "${GATEWAY_API_KEY_SECRET:-}" llm-api-key embed-api-key rerank-api-key
require_secret_keys "${PULL_SECRET:-}" .dockerconfigjson
# Only first-party objects listed by this render may be adopted. Never take
# resources belonging to a different release/controller. All deployments
# in the owned namespace must be serialized by the operator.
if [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    $KC -n "$NAMESPACE" get -f dist/agent-rendered.yaml --ignore-not-found -o json > dist/app-existing.json
    python3 scripts/airgap/check_app_ownership.py "$NAMESPACE" < dist/app-existing.json

    # Objects omitted from the first Helm release never enter its history.
    # Discover supported optional APIs, then inspect only the fixed legacy
    # inventory selected for removal. Failed discovery/reads stop deployment.
    _app_apis=$($KC api-resources -o name) || die "cannot discover optional application APIs"
    set --
    if [ "$OTEL_TRACING_ENABLED" != "1" ]; then
        set -- "$@" deployment.apps/jaeger service/jaeger configmap/jaeger-config
    fi
    if [ "$AGENT_ROUTE" != "true" ]; then
        set -- "$@" serviceaccount/rag-agent
        if printf '%s\n' "$_app_apis" | grep -qx 'routes.route.openshift.io'; then
            set -- "$@" route.route.openshift.io/rag-agent
        fi
    fi
    if [ "${METRICS_ENABLED:-false}" != "true" ]; then
        if printf '%s\n' "$_app_apis" | grep -qx 'servicemonitors.monitoring.coreos.com'; then
            set -- "$@" servicemonitor.monitoring.coreos.com/rag-agent
        fi
    fi
    if [ "$#" -gt 0 ]; then
        $KC -n "$NAMESPACE" get "$@" --ignore-not-found -o json > dist/app-disabled-existing.json
    else
        printf '%s\n' '{"apiVersion":"v1","kind":"List","items":[]}' > dist/app-disabled-existing.json
    fi
    python3 scripts/airgap/check_app_ownership.py "$NAMESPACE" --disabled \
        < dist/app-disabled-existing.json > dist/app-disabled-cleanup.json
fi

CHART=$(ls charts/qdrant-*.tgz | head -1)
[ -n "$CHART" ] || die "vendored chart missing (charts/qdrant-*.tgz)"

# CI-rehearsal knobs (never set in the air gap): shrink PVCs / resources for
# the lab run WITHOUT touching the prod values in git. Empty = git values.
QDRANT_STORAGE_SIZE=${QDRANT_STORAGE_SIZE:-}
QDRANT_EXTRA_VALUES=${QDRANT_EXTRA_VALUES:-}
if [ -n "$QDRANT_EXTRA_VALUES" ] && [ ! -f "$QDRANT_EXTRA_VALUES" ]; then
    die "QDRANT_EXTRA_VALUES file not found: $QDRANT_EXTRA_VALUES"
fi

echo "==> Namespace: $NAMESPACE"
if [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    if ! $KC get namespace "$NAMESPACE" >/dev/null 2>&1; then
        if command -v oc >/dev/null 2>&1; then
            oc new-project "$NAMESPACE"
        else
            $KC create namespace "$NAMESPACE"
        fi
    fi
fi

echo "==> Helm: Qdrant from the vendored chart with PROD values"
set -- helm upgrade -i "$QDRANT_RELEASE" "$CHART" \
    -n "$NAMESPACE" \
    -f overlays/openshift/values.yaml \
    --set "image.repository=$INTERNAL_REGISTRY/qdrant/qdrant" \
    --set "image.tag=$QDRANT_TAG" \
    --set "persistence.storageClassName=$STORAGE_CLASS" \
    --set "snapshotPersistence.storageClassName=$SNAPSHOT_STORAGE_CLASS"
if [ -n "$QDRANT_STORAGE_SIZE" ]; then
    set -- "$@" --set "persistence.size=$QDRANT_STORAGE_SIZE" \
        --set "snapshotPersistence.size=$QDRANT_STORAGE_SIZE"
fi
if [ -n "$QDRANT_EXTRA_VALUES" ]; then
    set -- "$@" -f "$QDRANT_EXTRA_VALUES"
fi
if [ -n "${PULL_SECRET:-}" ]; then
    set -- "$@" --set "imagePullSecrets[0].name=$PULL_SECRET"
else
    # values.yaml ships a placeholder pull-secret name; without a real secret
    # that placeholder must never reach the cluster (fail closed, not open).
    set -- "$@" --set "imagePullSecrets=null"
fi
run "$@"

echo "==> Helm: mainframe-rag application release"
run helm upgrade --install mainframe-rag charts/mainframe-rag \
    --namespace "$NAMESPACE" -f dist/mainframe-rag-release-values.yaml --take-ownership --server-side=false
JAEGER_UI_HINT=""
[ "${METRICS_ENABLED:-false}" = "true" ] || echo "==> Metrics off: ServiceMonitor not deployed"
[ "$OTEL_TRACING_ENABLED" = "1" ] || echo "==> Tracing off: Jaeger not deployed"
if [ "$OTEL_TRACING_ENABLED" = "1" ]; then
    JAEGER_UI_HINT="   |   traces UI: $KC -n $NAMESPACE port-forward svc/jaeger 16686:16686"
fi

wait_rollout() {
    target=$1
    timeout_s=$2
    if ! $KC -n "$NAMESPACE" rollout status "$target" --timeout="${timeout_s}s"; then
        echo "::error::Rollout failed for $target" >&2
        echo "==> Diagnostic: Pod statuses in $NAMESPACE" >&2
        $KC -n "$NAMESPACE" get pods -o wide 2>/dev/null || true
        echo "==> Diagnostic: Recent warning events" >&2
        $KC -n "$NAMESPACE" get events --field-selector type=Warning --sort-by=.lastTimestamp 2>/dev/null | tail -20 || true
        echo "==> Diagnostic: Pod logs (tail 50)" >&2
        $KC -n "$NAMESPACE" logs "$target" --tail=50 --all-containers=true 2>/dev/null || true
        die "rollout of $target did not succeed within ${timeout_s}s"
    fi
}

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] workloads would be checked for readiness"
    echo "[dryrun] chart values kept at dist/mainframe-rag-release-values.yaml"
    echo "[dryrun] rendered manifest kept at dist/agent-rendered.yaml"
else
    wait_rollout "statefulset/$QDRANT_RELEASE" 600
    wait_rollout "deploy/rag-agent" 300
    if [ "$OTEL_TRACING_ENABLED" = "1" ]; then
        wait_rollout "deploy/jaeger" 120
    fi
    # Reconcile disabled legacy resources only after the selected workloads
    # are ready. PVCs are never members of this validated cleanup inventory.
    if [ -s dist/app-disabled-cleanup.json ]; then
        $KC -n "$NAMESPACE" delete -f dist/app-disabled-cleanup.json --ignore-not-found
    fi
fi
if [ "$AGENT_ROUTE" = "true" ]; then
    echo "Route rag-agent -> svc port oauth, reencrypt, timeout 300s"
fi

next_step "corpus ready? sh scripts/tools/run-task.sh airgap:ingest CORPUS_PVC=<pvc>   |   smoke: sh scripts/tools/run-task.sh airgap:smoke$JAEGER_UI_HINT"
