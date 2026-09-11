#!/bin/sh
# AIR-GAP SIDE (issue #15): pre-flight validation of environment, tools,
# storage class, registry connectivity, and cluster security context.
#
#   make airgap-validate
#
# Safe, read-only pre-flight inspection before modifying any cluster state.

. "$(dirname -- "$0")/common.sh"

enforce_product_rules
resolve_aliases

echo "==> 1. Validating environment variables"
require_env INTERNAL_REGISTRY NAMESPACE STORAGE_CLASS EMBED_MODEL DENSE_DIM VLLM_BASE_URL
refuse_nfs_storage

case "$DENSE_DIM" in
    ''|*[!0-9]*) die "DENSE_DIM must be a positive integer, got '$DENSE_DIM'" ;;
    0) die "DENSE_DIM must be greater than 0, got 0" ;;
esac

case "$VLLM_BASE_URL" in
    http://*|https://*) ;;
    *) die "VLLM_BASE_URL must begin with http:// or https://, got '$VLLM_BASE_URL'" ;;
esac

# Optional model-endpoint overrides: empty means "derived/disabled", anything
# else must be an http(s) URL — a gateway hostname without a scheme fails
# here, not as a cryptic connect error inside the cluster.
for _url_var in EMBED_BASE_URL LLM_BASE_URL RERANK_BASE_URL CONTEXT_LLM_BASE_URL; do
    eval "_url=\${$_url_var:-}"
    case "$_url" in
        "") ;;
        http://*|https://*) ;;
        *) die "$_url_var must begin with http:// or https://, got '$_url'" ;;
    esac
done
unset _url_var _url

check_secret_name "${GATEWAY_API_KEY_SECRET:-}" GATEWAY_API_KEY_SECRET
check_secret_name "${PULL_SECRET:-}" PULL_SECRET
resolve_otel_endpoint

case "${RERANK_ENDPOINT_ORDER:-score_first}" in
    score_first|rerank_first) ;;
    *) die "RERANK_ENDPOINT_ORDER must be score_first or rerank_first, got '${RERANK_ENDPOINT_ORDER}'" ;;
esac

case "$IMAGE_SHA" in
    ""|HEAD) die "IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)" ;;
esac

echo "    INTERNAL_REGISTRY: $INTERNAL_REGISTRY"
echo "    NAMESPACE:         $NAMESPACE"
echo "    STORAGE_CLASS:     $STORAGE_CLASS"
echo "    EMBED_MODEL:       $EMBED_MODEL"
echo "    DENSE_DIM:         $DENSE_DIM"
echo "    VLLM_BASE_URL:     $VLLM_BASE_URL"
echo "    IMAGE_SHA:         $IMAGE_SHA"
if [ "$OTEL_TRACING_ENABLED" = "1" ]; then
    echo "    Tracing:           ON ($OTEL_ENDPOINT_RESOLVED)"
else
    echo "    Tracing:           OFF (OTEL_EXPORTER_OTLP_ENDPOINT=off)"
fi
if [ -n "${GATEWAY_API_KEY_SECRET:-}" ]; then
    echo "    GATEWAY_API_KEY_SECRET: $GATEWAY_API_KEY_SECRET"
fi

echo "==> 2. Validating required CLI tools"
command -v skopeo >/dev/null 2>&1 || die "skopeo is required on the air-gap bastion"
command -v helm >/dev/null 2>&1 || die "helm is required on the air-gap bastion"
command -v openssl >/dev/null 2>&1 || die "openssl is required on the air-gap bastion"
KC=${KC:-$(kc)}
command -v "$KC" >/dev/null 2>&1 || die "oc or kubectl is required on the air-gap bastion"
echo "    skopeo: $(command -v skopeo)"
echo "    helm:   $(command -v helm)"
echo "    client: $(command -v "$KC") ($KC)"

echo "==> 3. Validating sneakernet package manifest"
MANIFEST=$(find_manifest)
if [ -n "$MANIFEST" ]; then
    packed_sha=$(awk '/^sha: /{print $2}' "$MANIFEST")
    [ "$IMAGE_SHA" = "$packed_sha" ] || \
        die "IMAGE_SHA=$IMAGE_SHA does not match packed MANIFEST sha ($packed_sha)"
    echo "    Verified matching MANIFEST at $MANIFEST ($packed_sha)"
else
    echo "    Notice: no MANIFEST.txt found in ./dist or ../ (using env IMAGE_SHA=$IMAGE_SHA)"
fi

CHART=$(ls charts/qdrant-*.tgz 2>/dev/null | head -1 || true)
[ -n "$CHART" ] || die "vendored chart missing (charts/qdrant-*.tgz)"
echo "    Vendored chart: $CHART"

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "==> [dryrun] Cluster and registry live probes skipped"
    echo ""
    echo "SUCCESS: Pre-flight validation passed (dry-run mode)."
    next_step "make airgap-load"
    exit 0
fi

echo "==> 4. Validating cluster context & StorageClass"
if ! $KC cluster-info >/dev/null 2>&1; then
    die "cannot connect to Kubernetes/OpenShift API server using $KC"
fi

if ! $KC get storageclass "$STORAGE_CLASS" >/dev/null 2>&1; then
    echo "    WARNING: StorageClass '$STORAGE_CLASS' not found in cluster. Available classes:"
    $KC get storageclass --no-headers 2>/dev/null | awk '{print "      - " $1}' || true
    die "StorageClass '$STORAGE_CLASS' must exist before deployment"
fi
echo "    StorageClass '$STORAGE_CLASS' verified in cluster"

if [ -n "${GATEWAY_API_KEY_SECRET:-}" ]; then
    if $KC get namespace "$NAMESPACE" >/dev/null 2>&1; then
        $KC -n "$NAMESPACE" get secret "$GATEWAY_API_KEY_SECRET" >/dev/null 2>&1 || \
            die "Secret '$GATEWAY_API_KEY_SECRET' not found in namespace '$NAMESPACE' — create it before deploying (see airgap.env.example)"
        echo "    Gateway key Secret '$GATEWAY_API_KEY_SECRET' verified in namespace '$NAMESPACE'"
    else
        echo "    Notice: namespace '$NAMESPACE' does not exist yet — create Secret '$GATEWAY_API_KEY_SECRET' there before 'make airgap-deploy'"
    fi
fi

echo "==> 5. Checking OpenShift Security Context Constraints (SCC)"
if command -v oc >/dev/null 2>&1 && oc get scc >/dev/null 2>&1; then
    # OpenShift cluster detected
    if [ -n "${QDRANT_EXTRA_VALUES:-}" ] && [ -f "$QDRANT_EXTRA_VALUES" ]; then
        echo "    QDRANT_EXTRA_VALUES provided ($QDRANT_EXTRA_VALUES); overriding default UID settings."
    else
        echo "    OpenShift cluster detected. Qdrant unprivileged image runs as UID 1000."
        echo "    If namespace '$NAMESPACE' enforces MustRunAsRange UID allocation,"
        echo "    ensure the ServiceAccount is granted anyuid SCC:"
        echo "      oc adm policy add-scc-to-user anyuid -z qdrant -n $NAMESPACE"
    fi
else
    echo "    Standard Kubernetes cluster detected (non-OpenShift SCC)."
fi

echo ""
echo "SUCCESS: Pre-flight validation passed cleanly."
next_step "make airgap-load"
