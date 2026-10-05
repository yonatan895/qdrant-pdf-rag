#!/bin/sh
# AIR-GAP SIDE (issue #15): pre-flight validation of environment, tools,
# storage class, registry connectivity, and cluster security context.
#
#   sh scripts/tools/run-task.sh airgap:validate
#
# Safe, read-only pre-flight inspection before modifying any cluster state.

. "$(dirname -- "$0")/common.sh"

enforce_product_rules
resolve_aliases

echo "==> 1. Validating environment variables"
require_env INTERNAL_REGISTRY NAMESPACE STORAGE_CLASS EMBED_MODEL DENSE_DIM EMBED_MODEL_REVISION EMBED_BASE_URL
require_embed_revision
refuse_nfs_storage
# Issue #360: a partial, impossible or absent collection policy must fail
# pre-flight, before any ingest/publication mutation. The checked-in
# production preset supplies 6/3/2; one-node lanes select 1/1/1 explicitly.
validate_collection_policy required

case "$DENSE_DIM" in
    ''|*[!0-9]*) die "DENSE_DIM must be a positive integer, got '$DENSE_DIM'" ;;
    0) die "DENSE_DIM must be greater than 0, got 0" ;;
esac

validate_model_config

check_secret_name "${GATEWAY_API_KEY_SECRET:-}" GATEWAY_API_KEY_SECRET
check_secret_name "${PULL_SECRET:-}" PULL_SECRET
check_secret_name "${GATEWAY_CA_CONFIGMAP:-}" GATEWAY_CA_CONFIGMAP
resolve_otel_endpoint
resolve_bundle_choices
resolve_agent_route

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
echo "    EMBED_MODEL_REVISION: $EMBED_MODEL_REVISION"
echo "    DENSE_DIM:         $DENSE_DIM"
echo "    EMBED_BASE_URL:    $EMBED_BASE_URL"
echo "    Collection policy: S=$QDRANT_SHARD_NUMBER RF=$QDRANT_REPLICATION_FACTOR W=$QDRANT_WRITE_CONSISTENCY_FACTOR"
echo "    IMAGE_SHA:         $IMAGE_SHA"
if [ "$OTEL_TRACING_ENABLED" = "1" ]; then
    echo "    Tracing:           ON ($OTEL_ENDPOINT_RESOLVED)"
else
    echo "    Tracing:           OFF (OTEL_EXPORTER_OTLP_ENDPOINT=off)"
fi
if [ "$JAEGER_DEPLOY" = "1" ]; then
    echo "    Jaeger backend:    bundled"
else
    echo "    Jaeger backend:    not deployed"
fi
if [ "$SERVICEMONITOR_DEPLOY" = "1" ]; then
    echo "    ServiceMonitor:    rendered"
else
    echo "    ServiceMonitor:    not rendered"
fi
if [ -n "${GATEWAY_API_KEY_SECRET:-}" ]; then
    echo "    GATEWAY_API_KEY_SECRET: $GATEWAY_API_KEY_SECRET"
fi
if [ "$AGENT_ROUTE" = "true" ]; then
    echo "    Console Route:     OAuth reencrypt (AGENT_ROUTE=true)"
else
    echo "    Console Route:     off (ClusterIP only)"
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
# The executing checkout itself must resolve to the packed SHA when one is
# reachable (issue #414): overriding IMAGE_SHA alone never changes which
# code executes. No MANIFEST means connected-development notice path above.
check_checkout_sha

# Issue #373: the console Route is only ever offered behind the oauth-proxy
# sidecar. Its pin must be recorded, consistent with the chart and with the
# packed bundle before the Route can be selected. Static: runs in dry-run.
if [ "$AGENT_ROUTE" = "true" ]; then
    require_oauth_pin
    echo "    oauth-proxy pin recorded; matches the chart${MANIFEST:+ and the packed bundle}"
    echo "    Notice: the console Route authenticates any OpenShift user (oauth-proxy --email-domain=*, no SAR); the cohort restriction is a site decision (issue #373)"
fi

CHART=$(ls charts/qdrant-*.tgz 2>/dev/null | head -1 || true)
[ -n "$CHART" ] || die "vendored chart missing (charts/qdrant-*.tgz)"
echo "    Vendored chart: $CHART"

# Issue #366: serving is read-only, ingest owns mutation. The prod agent
# template must reference the chart's `read-only-api-key` data key while the
# ingest template keeps the full-access `api-key`. Source-level gate (no
# cluster): deploy.sh/ingest.sh re-check the rendered manifests.
AGENT_TEMPLATE=charts/mainframe-rag/templates/agent-deployment.yaml
_agent_block=$(grep -A8 -- "- name: QDRANT_API_KEY" "$AGENT_TEMPLATE" || true)
printf '%s\n' "$_agent_block" | grep -Eq '^[[:space:]]*key: read-only-api-key$' || \
    die "prod agent template must wire QDRANT_API_KEY to read-only-api-key (issue #366)"
if printf '%s\n' "$_agent_block" | grep -Eq '^[[:space:]]*key: api-key$'; then
    die "prod agent template wires QDRANT_API_KEY to the full-access key (issue #366)"
fi
unset _agent_block
INGEST_TEMPLATE=charts/mainframe-rag/templates/ingest-job.yaml
_ingest_block=$(grep -A8 -- "- name: QDRANT_API_KEY" "$INGEST_TEMPLATE" || true)
printf '%s\n' "$_ingest_block" | grep -Eq '^[[:space:]]*key: api-key$' || \
    die "ingest template must wire QDRANT_API_KEY to api-key (ingest owns corpus mutation)"
if printf '%s\n' "$_ingest_block" | grep -Eq '^[[:space:]]*key: read-only-api-key$'; then
    die "ingest template wires a read-only Qdrant key into the ingest path (issue #366)"
fi
unset _ingest_block
echo "    Qdrant key separation verified (agent read-only, ingest write)"

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "==> [dryrun] Cluster and registry live probes skipped"
    echo ""
    echo "SUCCESS: Pre-flight validation passed (dry-run mode)."
    next_step "sh scripts/tools/run-task.sh airgap:load"
    exit 0
fi

# Read-only probe classified by the client's error reason token, "(Forbidden)"
# or "(NotFound)" (never free text such as a resource name), not by exit
# status alone: sets PROBE to ok|notfound|forbidden|error. The client's text
# is not echoed (it can carry identities/upstream detail).
probe() {
    if _probe_err=$("$@" 2>&1 >/dev/null); then
        PROBE=ok
    else
        case "$_probe_err" in
            *"(Forbidden)"*) PROBE=forbidden ;;
            *"(NotFound)"*|*"doesn't have a resource type"*) PROBE=notfound ;;
            *) PROBE=error ;;
        esac
    fi
    unset _probe_err
}

echo "==> 4. Validating cluster context & StorageClass"
# API discovery needs an authenticated identity but no namespace beyond the
# deployer's own (#678): `cluster-info` lists kube-system Services, which a
# namespace admin cannot, and /version is readable anonymously.
if ! $KC get --raw /api >/dev/null 2>&1; then
    die "cannot connect to Kubernetes/OpenShift API server using $KC"
fi

probe $KC get storageclass "$STORAGE_CLASS"
case "$PROBE" in
    ok) echo "    StorageClass '$STORAGE_CLASS' verified in cluster" ;;
    notfound)
        echo "    WARNING: StorageClass '$STORAGE_CLASS' not found in cluster. Available classes:"
        $KC get storageclass --no-headers 2>/dev/null | awk '{print "      - " $1}' || true
        die "StorageClass '$STORAGE_CLASS' must exist before deployment"
        ;;
    forbidden)
        echo "    Notice: this identity may not read StorageClass '$STORAGE_CLASS' (Forbidden); existence is NOT verified — confirm it with the platform owner"
        ;;
    *) die "cannot read StorageClass '$STORAGE_CLASS' (unexpected client error)" ;;
esac

if [ -n "${GATEWAY_API_KEY_SECRET:-}" ]; then
    probe $KC get namespace "$NAMESPACE"
    case "$PROBE" in
        notfound)
            echo "    Notice: namespace '$NAMESPACE' does not exist yet — create Secret '$GATEWAY_API_KEY_SECRET' there before 'sh scripts/tools/run-task.sh airgap:deploy'"
            ;;
        ok|forbidden)
            # A namespace-scoped deployer cannot read the cluster-scoped
            # Namespace object (Forbidden); that is not absence, so check the
            # Secret in its own namespace instead of skipping the check.
            probe $KC -n "$NAMESPACE" get secret "$GATEWAY_API_KEY_SECRET"
            case "$PROBE" in
                ok) echo "    Gateway key Secret '$GATEWAY_API_KEY_SECRET' verified in namespace '$NAMESPACE'" ;;
                notfound) die "Secret '$GATEWAY_API_KEY_SECRET' not found in namespace '$NAMESPACE' — create it before deploying (see airgap.env.example)" ;;
                forbidden) die "this identity may not read Secret '$GATEWAY_API_KEY_SECRET' in namespace '$NAMESPACE' (Forbidden); grant namespace-scoped get on secrets, not cluster-admin" ;;
                *) die "cannot read Secret '$GATEWAY_API_KEY_SECRET' in namespace '$NAMESPACE' (unexpected client error)" ;;
            esac
            ;;
        *) die "cannot read namespace '$NAMESPACE' (unexpected client error)" ;;
    esac
fi

check_gateway_ca

echo "==> 4b. Console Route prerequisites and exposure"
if [ "$AGENT_ROUTE" = "true" ]; then
    probe $KC -n "$NAMESPACE" get secret rag-agent-oauth-cookie
    case "$PROBE" in
        ok) require_oauth_cookie_secret
            echo "    Cookie Secret 'rag-agent-oauth-cookie' has a nonempty cookie-secret key" ;;
        notfound) die "Secret 'rag-agent-oauth-cookie' not found in namespace '$NAMESPACE' — create it before AGENT_ROUTE=true (docs/install_and_ops.md 4.4.2)" ;;
        forbidden) die "this identity may not read Secret 'rag-agent-oauth-cookie' in namespace '$NAMESPACE' (Forbidden); grant namespace-scoped get on secrets, not cluster-admin" ;;
        *) die "cannot read Secret 'rag-agent-oauth-cookie' in namespace '$NAMESPACE' (unexpected client error)" ;;
    esac
    _ca_tmp=$(mktemp)
    trap 'rm -f "$_ca_tmp"' EXIT
    fetch_route_destination_ca "$_ca_tmp"
    echo "    Namespace service CA readable (PEM bundle)"
    check_route_exposure enabled
else
    check_route_exposure disabled
fi
echo "    No Route other than the OAuth-protected rag-agent Route reaches the agent or Qdrant Services"

echo "==> 5. Checking OpenShift Security Context Constraints (SCC)"
# A failed `get scc` is evidence of a non-OpenShift cluster only when the API
# server lacks the resource type; a denied or failed read proves nothing.
probe $KC get scc
case "$PROBE" in
    ok)
        if [ -n "${QDRANT_EXTRA_VALUES:-}" ] && [ -f "$QDRANT_EXTRA_VALUES" ]; then
            echo "    QDRANT_EXTRA_VALUES provided ($QDRANT_EXTRA_VALUES); overriding default UID settings."
        else
            echo "    OpenShift cluster detected. Qdrant unprivileged image runs as UID 1000."
            echo "    If namespace '$NAMESPACE' enforces MustRunAsRange UID allocation,"
            echo "    keep restricted-v2 admission; use the documented unprivileged Qdrant values."
        fi
        ;;
    notfound)
        echo "    Standard Kubernetes cluster detected (non-OpenShift SCC)."
        ;;
    forbidden)
        echo "    Notice: this identity may not list SCCs (Forbidden); the cluster type is NOT determined."
        echo "    On OpenShift keep restricted-v2 admission for namespace '$NAMESPACE' and the documented unprivileged Qdrant values; never grant anyuid/cluster-admin."
        ;;
    *)
        echo "    Notice: SCC discovery failed (unexpected client error); the cluster type is NOT determined."
        ;;
esac

echo ""
echo "SUCCESS: Pre-flight validation passed cleanly."
next_step "sh scripts/tools/run-task.sh airgap:load"
