#!/bin/sh
# AIR-GAP SIDE (issue #15): deploy Qdrant (vendored chart, PROD sizing) and the
# agent (prod kustomize overlay) into $NAMESPACE, then wait for Ready.
#
#   make airgap-deploy
#
# Prod Qdrant: 3 replicas / 500Gi RWO block / unprivileged / ClusterIP, no
# Route (overlays/openshift/values.yaml is never shrunk). No NFS. No Cloud.

. "$(dirname -- "$0")/common.sh"

enforce_product_rules
resolve_aliases
require_env INTERNAL_REGISTRY NAMESPACE STORAGE_CLASS EMBED_MODEL DENSE_DIM VLLM_BASE_URL
case "$IMAGE_SHA" in
    ""|HEAD) die "IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)" ;;
esac
# Cross-check against the packed MANIFEST when it is reachable (dist/ or ../).
MANIFEST=""
[ -f dist/MANIFEST.txt ] && MANIFEST=dist/MANIFEST.txt
[ -z "$MANIFEST" ] && [ -f ../MANIFEST.txt ] && MANIFEST=../MANIFEST.txt
if [ -n "$MANIFEST" ] && [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    packed_sha=$(awk '/^sha: /{print $2}' "$MANIFEST")
    [ "$IMAGE_SHA" = "$packed_sha" ] || \
        die "IMAGE_SHA=$IMAGE_SHA does not match the packed MANIFEST sha ($packed_sha) — wrong SHA for this sneakernet bundle"
fi
[ "${AIRGAP_DRYRUN:-0}" = "1" ] || command -v oc >/dev/null 2>&1 || command -v kubectl >/dev/null 2>&1 || die "oc or kubectl is required on the air-gap bastion (or set AIRGAP_DRYRUN=1 to preview)"
command -v helm >/dev/null 2>&1 || die "helm is required on the air-gap bastion"

refuse_nfs_storage
EMBED_BASE_URL=${EMBED_BASE_URL:-$(echo "$VLLM_BASE_URL" | sed -E 's:(/v1)?/*$::')/v1}
SNAPSHOT_STORAGE_CLASS=${SNAPSHOT_STORAGE_CLASS:-$STORAGE_CLASS}
# Chart appends "-unprivileged" to the tag when useUnprivilegedImage=true;
# values.yaml pins v1.19.0 — set it explicitly so it always matches load.sh.
# Strip a "-unprivileged" suffix from the pin: the chart re-adds it itself.
QDRANT_TAG=${QDRANT_TAG:-$(echo "${QDRANT_IMAGE:-docker.io/qdrant/qdrant:v1.19.0-unprivileged}" | sed "s/.*://; s/-unprivileged\$//")}
QDRANT_URL="http://${QDRANT_RELEASE}:6333"

KC=${KC:-$(if command -v kubectl >/dev/null 2>&1; then echo kubectl; else echo oc; fi)}
mkdir -p dist

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

CHART=$(ls charts/qdrant-*.tgz | head -1)
[ -n "$CHART" ] || die "vendored chart missing (charts/qdrant-*.tgz)"

# CI-rehearsal knobs (never set in the air gap): shrink PVCs / resources for
# the lab run WITHOUT touching the prod values in git. Empty = git values.
QDRANT_STORAGE_SIZE=${QDRANT_STORAGE_SIZE:-}
QDRANT_EXTRA_VALUES=${QDRANT_EXTRA_VALUES:-}
if [ -n "$QDRANT_EXTRA_VALUES" ] && [ ! -f "$QDRANT_EXTRA_VALUES" ]; then
    die "QDRANT_EXTRA_VALUES file not found: $QDRANT_EXTRA_VALUES"
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

echo "==> Kustomize: agent (prod overlay, placeholders substituted from airgap.env)"
if command -v kustomize >/dev/null 2>&1; then
    render="kustomize build deploy/kustomize/overlays/openshift"
else
    # kubectl ships a built-in kustomize; always present on an OpenShift bastion.
    render="$KC kustomize deploy/kustomize/overlays/openshift"
fi
$render | sed -E 's|"(__[A-Z0-9_]+__)"|\1|g' | sed \
    -e "s|__INTERNAL_REGISTRY__|$INTERNAL_REGISTRY|g" \
    -e "s|__IMAGE_SHA__|$IMAGE_SHA|g" \
    -e "s|namespace: mainframe-rag|namespace: $NAMESPACE|g" \
    -e "s|__QDRANT_URL__|$QDRANT_URL|g" \
    -e "s|__QDRANT_RELEASE__|$QDRANT_RELEASE|g" \
    -e "s|__EMBED_BASE_URL__|$EMBED_BASE_URL|g" \
    -e "s|__EMBED_MODEL__|$EMBED_MODEL|g" \
    -e "s|__DENSE_DIM__|\"$DENSE_DIM\"|g" \
    -e "s|__LLM_BASE_URL__|${LLM_BASE_URL:-}|g" \
    -e "s|__LLM_MODEL_REASONING__|${LLM_MODEL_REASONING:-}|g" \
    -e "s|__OTEL_EXPORTER_OTLP_ENDPOINT__|${OTEL_EXPORTER_OTLP_ENDPOINT:-}|g" \
    -e "s|__RERANK_ENABLED__|\"${RERANK_ENABLED:-false}\"|g" \
    -e "s|__RERANK_BASE_URL__|${RERANK_BASE_URL:-}|g" \
    -e "s|__RERANK_MODEL__|${RERANK_MODEL:-BAAI/bge-reranker-v2-m3}|g" \
    > dist/agent-rendered.yaml
if [ -n "${PULL_SECRET:-}" ]; then
    sed -i "s|imagePullSecrets: \[\]|imagePullSecrets:\n  - name: $PULL_SECRET|" dist/agent-rendered.yaml
fi
if grep -Eq "__[A-Z][A-Z0-9_]*__" dist/agent-rendered.yaml; then
    die "unsubstituted placeholder left in rendered agent manifest (check airgap.env)"
fi
run $KC apply -f dist/agent-rendered.yaml

# Jaeger v2 trace backend (issue #83): opt-in via OTEL_EXPORTER_OTLP_ENDPOINT
# in airgap.env. The agent env var is always rendered (empty = tracing off);
# the Jaeger deployment only exists when tracing is on. Badger on RWO block,
# ClusterIP only, UI via port-forward — no Route, ever.
JAEGER_UI_HINT=""
if [ -n "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]; then
    echo "==> Kustomize: Jaeger v2 all-in-one (badger on RWO block, ClusterIP)"
    if command -v kustomize >/dev/null 2>&1; then
        jrender="kustomize build deploy/kustomize/jaeger"
    else
        jrender="$KC kustomize deploy/kustomize/jaeger"
    fi
    $jrender | sed \
        -e "s|__INTERNAL_REGISTRY__|$INTERNAL_REGISTRY|g" \
        -e "s|__STORAGE_CLASS__|$STORAGE_CLASS|g" \
        -e "s|namespace: mainframe-rag|namespace: $NAMESPACE|g" \
        > dist/jaeger-rendered.yaml
    if [ -n "${PULL_SECRET:-}" ]; then
        sed -i "s|imagePullSecrets: \[\]|imagePullSecrets:\n  - name: $PULL_SECRET|" dist/jaeger-rendered.yaml
    fi
    if grep -Eq "__[A-Z][A-Z0-9_]*__" dist/jaeger-rendered.yaml; then
        die "unsubstituted placeholder left in rendered Jaeger manifest (check airgap.env)"
    fi
    run $KC apply -f dist/jaeger-rendered.yaml
    JAEGER_UI_HINT="   |   traces UI: $KC -n $NAMESPACE port-forward svc/jaeger 16686:16686"
else
    echo "==> Tracing off (OTEL_EXPORTER_OTLP_ENDPOINT unset): Jaeger not deployed"
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
    echo "[dryrun] $KC -n $NAMESPACE rollout status statefulset/$QDRANT_RELEASE --timeout=600s"
    echo "[dryrun] $KC -n $NAMESPACE rollout status deploy/rag-agent --timeout=300s"
    [ -n "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ] && \
        echo "[dryrun] $KC -n $NAMESPACE rollout status deploy/jaeger --timeout=120s"
    [ "${AGENT_ROUTE:-false}" = "true" ] && echo "[dryrun] oc create route edge rag-agent --service=rag-agent -n $NAMESPACE"
    echo "[dryrun] rendered manifest kept at dist/agent-rendered.yaml"
    [ -n "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ] && \
        echo "[dryrun] Jaeger manifest kept at dist/jaeger-rendered.yaml"
else
    echo "==> Wait for Qdrant + agent Ready"
    wait_rollout "statefulset/$QDRANT_RELEASE" 600
    wait_rollout "deploy/rag-agent" 300
    if [ -n "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]; then
        wait_rollout "deploy/jaeger" 120
    fi
    if [ "${AGENT_ROUTE:-false}" = "true" ]; then
        $KC -n "$NAMESPACE" get route rag-agent >/dev/null 2>&1 || \
            oc create route edge rag-agent --service=rag-agent -n "$NAMESPACE"
        echo "Agent Route: $KC -n $NAMESPACE get route rag-agent"
    fi
fi

next_step "corpus ready? make airgap-ingest CORPUS_PVC=<pvc>   |   smoke: make airgap-smoke$JAEGER_UI_HINT"
