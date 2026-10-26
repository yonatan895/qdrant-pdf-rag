#!/bin/sh
# AIR-GAP SIDE (issue #15): deploy Qdrant (vendored chart, PROD sizing) and the
# agent (first-party Helm chart) into $NAMESPACE, then wait for Ready.
#
#   sh scripts/tools/run-task.sh airgap:deploy
#
# Prod Qdrant: 3 replicas / 500Gi RWO block / unprivileged / ClusterIP, no
# Route (charts/qdrant-openshift.values.yaml is never shrunk). No NFS. No Cloud.

. "$(dirname -- "$0")/common.sh"
. "$(dirname -- "$0")/image_identity.sh"

[ -f charts/qdrant-openshift.values.yaml ] && [ -r charts/qdrant-openshift.values.yaml ] ||
    die "required Qdrant values file is missing or unreadable: charts/qdrant-openshift.values.yaml"

enforce_product_rules
resolve_aliases
require_env INTERNAL_REGISTRY NAMESPACE STORAGE_CLASS EMBED_MODEL DENSE_DIM EMBED_MODEL_REVISION EMBED_BASE_URL
require_embed_revision
check_secret_name "${GATEWAY_API_KEY_SECRET:-}" GATEWAY_API_KEY_SECRET
check_secret_name "${PULL_SECRET:-}" PULL_SECRET
check_secret_name "${GATEWAY_CA_CONFIGMAP:-}" GATEWAY_CA_CONFIGMAP
resolve_observability
resolve_agent_route
resolve_network_policy
resolve_feedback
resolve_openwebui
case "$IMAGE_SHA" in
    ""|HEAD) die "IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)" ;;
esac
# Cross-check against the packed MANIFEST when it is reachable (dist/ or ../).
MANIFEST=$(find_manifest)
check_manifest_sha
# The executing checkout itself must resolve to the packed SHA (issue #414):
# an explicitly set IMAGE_SHA alone never changes which code executes.
check_checkout_sha
# Exactly one vendored Qdrant chart (issue #272): never pick one of several.
CHART=""
_chart_count=0
for _c in charts/qdrant-*.tgz; do
    [ -f "$_c" ] || continue
    CHART=$_c
    _chart_count=$((_chart_count + 1))
done
[ "$_chart_count" -eq 1 ] || die "exactly one vendored Qdrant chart is required (charts/qdrant-*.tgz); found $_chart_count"
# A packed MANIFEST binds the chart bytes: the same release gate as the SHA
# checks above (skipped in dry-run, which mutates nothing).
if [ -n "$MANIFEST" ] && [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    _chart_packed=$(awk '/^chart_sha256: /{print $2}' "$MANIFEST")
    [ -n "$_chart_packed" ] || die "packed MANIFEST ($MANIFEST) has no chart_sha256 — cannot verify the vendored Qdrant chart"
    _chart_actual=$(sha256sum "$CHART" | awk '{print $1}')
    [ "$_chart_actual" = "$_chart_packed" ] || die "vendored Qdrant chart $CHART does not match the packed MANIFEST chart_sha256 — wrong or altered chart for this sneakernet bundle"
    echo "==> Qdrant chart $CHART verified against packed MANIFEST"
else
    echo "==> Notice: Qdrant chart $CHART is not release-verified (dry-run or no packed MANIFEST)"
fi
require_kc
command -v helm >/dev/null 2>&1 || die "helm is required on the air-gap bastion"

# Operator console Route (ADR-0004, issue #373): rendering the OAuth sidecar
# and the reencrypt Route needs a recorded oauth-proxy pin (not the
# sha256:PENDING placeholder) that matches the chart and the packed bundle.
if [ "$AGENT_ROUTE" = "true" ]; then
    require_oauth_pin
    echo "==> Notice: the console Route authenticates any OpenShift user (oauth-proxy --email-domain=*, no SAR); restrict the cohort with the site's approved mechanism (issue #373)"
fi

# Issue #373: the agent's unauthenticated HTTP port is namespace-scoped by
# default. Enforcement and coexistence with site policies are site evidence.
echo "==> NetworkPolicy $(describe_network_policy)"
# Issue #804: the answer-feedback service is default off; enabling it refuses
# (in map_values.py, before any mutation) a missing Secret name or a disabled
# agent NetworkPolicy. Content stays on the kept claim even when disabled later.
echo "==> Feedback service $(describe_feedback)"
# Issue #1074 (ADR-0007): the Open WebUI docs front end is default off. Enabling
# needs the recorded image pin (images.txt, chart and packed bundle agree);
# map_values.py refuses missing OIDC/host/IdP-egress settings before mutation.
if [ "$OPENWEBUI_ENABLED" = "true" ]; then
    require_openwebui_pin
fi
echo "==> Open WebUI front end $(describe_openwebui)"

refuse_nfs_storage
SNAPSHOT_STORAGE_CLASS=${SNAPSHOT_STORAGE_CLASS:-$STORAGE_CLASS}
# Chart appends "-unprivileged" to the tag when useUnprivilegedImage=true;
# values.yaml pins v1.19.2 — set it explicitly so it always matches load.sh.
# Strip a "-unprivileged" suffix from the pin: the chart re-adds it itself.
QDRANT_TAG=${QDRANT_TAG:-$(echo "${QDRANT_IMAGE:-docker.io/qdrant/qdrant:v1.19.2-unprivileged}" | sed "s/.*://; s/-unprivileged\$//")}
validate_helm_inputs
QDRANT_URL="http://${QDRANT_RELEASE}:6333"

# Registry image identity (issue #272), before any cluster change and for every
# entry path (load -> deploy, pipeline --skip-load, standalone deploy): each
# deployed image must be the packed one. First-party images are then rendered
# as repository@digest; the Qdrant digest is checked against the pods below.
DEPLOY_IMAGES="qdrant agent"
# OpenObserve is the only trace backend (ADR-0006, #956): with tracing on, the
# chart renders it (and, with metrics on, the otelcol collector), so those are
# identity-checked.
[ "$OTEL_BUNDLED" != "1" ] || DEPLOY_IMAGES="$DEPLOY_IMAGES openobserve"
# otelcol (the /metrics scraper) renders only with metrics on (OTEL_COLLECTOR).
[ "$OTEL_COLLECTOR" != "1" ] || DEPLOY_IMAGES="$DEPLOY_IMAGES otelcol"
[ "$AGENT_ROUTE" != "true" ] || DEPLOY_IMAGES="$DEPLOY_IMAGES oauth_proxy"
[ "$OPENWEBUI_ENABLED" != "true" ] || DEPLOY_IMAGES="$DEPLOY_IMAGES openwebui openwebui_proxy"
# shellcheck disable=SC2086
verify_registry_images $DEPLOY_IMAGES
# The feedback pod runs the agent image: no extra registry read-back, but its
# own StatefulSet is bound to the verified agent digest after rollout.
RUNNING_IMAGES=$DEPLOY_IMAGES
[ "$FEEDBACK_ENABLED" != "true" ] || RUNNING_IMAGES="$RUNNING_IMAGES feedback"

KC=${KC:-$(kc)}
check_gateway_ca
mkdir -p dist

# Public namespace service CA becomes a generated value, never a YAML patch.
# It is the destination CA of both reencrypt Routes (console and Open WebUI).
if [ "$AGENT_ROUTE" = "true" ] && [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    require_oauth_cookie_secret
fi
if { [ "$AGENT_ROUTE" = "true" ] || [ "$OPENWEBUI_ENABLED" = "true" ]; } && [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    ROUTE_DESTINATION_CA_FILE=dist/namespace-service-ca.crt
    fetch_route_destination_ca "$ROUTE_DESTINATION_CA_FILE"
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
rm -f dist/openobserve-rendered.yaml dist/servicemonitor-rendered.yaml dist/agent-route.yaml
if [ "$OTEL_BUNDLED" = "1" ]; then
    cat dist/app-helm-render/mainframe-rag/templates/openobserve-*.yaml > dist/openobserve-rendered.yaml
fi
if [ "$SERVICEMONITOR_DEPLOY" = "1" ]; then
    cp dist/app-helm-render/mainframe-rag/templates/servicemonitor.yaml dist/servicemonitor-rendered.yaml
fi
if [ "$AGENT_ROUTE" = "true" ]; then
    cp dist/app-helm-render/mainframe-rag/templates/route.yaml dist/agent-route.yaml
fi

require_gateway_secret_keys llm-api-key embed-api-key rerank-api-key
# Operator-created OpenObserve credentials (ADR-0006, #956), checked before any
# mutation. Only presence of keys is read; values are never printed.
if [ "$OTEL_BUNDLED" = "1" ] && [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    for _oo_key in root-user-email root-user-password otlp-headers metrics-auth; do
        _oo_present=$($KC -n "$NAMESPACE" get secret openobserve-credentials \
            -o "go-template={{if index .data \"$_oo_key\"}}present{{end}}" 2>/dev/null) || _oo_present=""
        [ "$_oo_present" = present ] ||
            die "E_OPENOBSERVE_CREDENTIALS: Secret openobserve-credentials must exist with non-empty keys root-user-email, root-user-password, otlp-headers and metrics-auth"
    done
    # OpenObserve v1.1.2 panics at startup on a weak root password (8-128
    # characters with a lowercase, an uppercase, a digit and a special
    # character), which would crash-loop the rollout. The value stays in a shell
    # variable and reaches the checks on stdin, never argv or output.
    _oo_pw=$(_read_oo_secret_key root-user-password) || _oo_pw=""
    # Characters, not bytes (dash counts bytes in ${#var}): drop UTF-8
    # continuation bytes, locale-independent.
    _oo_len=$(printf '%s' "$_oo_pw" | LC_ALL=C tr -d '\200-\277' | wc -c)
    _oo_len=$((_oo_len + 0))
    if [ "$_oo_len" -lt 8 ] || [ "$_oo_len" -gt 128 ] ||
        ! printf '%s' "$_oo_pw" | LC_ALL=C grep -q '[a-z]' ||
        ! printf '%s' "$_oo_pw" | LC_ALL=C grep -q '[A-Z]' ||
        ! printf '%s' "$_oo_pw" | LC_ALL=C grep -q '[0-9]' ||
        ! printf '%s' "$_oo_pw" | LC_ALL=C grep -q '[^A-Za-z0-9]'; then
        unset _oo_pw _oo_len
        die "E_OPENOBSERVE_WEAK_PASSWORD: Secret openobserve-credentials key root-user-password must be 8-128 characters with a lowercase letter, an uppercase letter, a digit and a special character"
    fi
    unset _oo_pw _oo_len
fi
require_secret_keys "${PULL_SECRET:-}" .dockerconfigjson
# Operator-created, never rendered: the bearer token both the service and the
# agent read. Checked before any mutation (a missing key would otherwise show
# as CreateContainerConfigError after the release).
[ "$FEEDBACK_ENABLED" != "true" ] || require_secret_keys "$FEEDBACK_SECRET" service-token
# Open WebUI (#1074): the session-signing key and the OIDC client secret are
# operator-created; presence only, values never read here.
[ "$OPENWEBUI_ENABLED" != "true" ] || require_secret_keys "$OPENWEBUI_SECRET" webui-secret-key oidc-client-secret
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
    # Jaeger is retired (ADR-0006, #956): a leftover from an earlier release is
    # removed through the validated cleanup inventory (PVCs are never members).
    set -- "$@" deployment.apps/jaeger service/jaeger configmap/jaeger-config
    if [ "$AGENT_ROUTE" != "true" ]; then
        set -- "$@" serviceaccount/rag-agent
        if printf '%s\n' "$_app_apis" | grep -qx 'routes.route.openshift.io'; then
            set -- "$@" route.route.openshift.io/rag-agent
        fi
    fi
    if [ "$#" -gt 0 ]; then
        $KC -n "$NAMESPACE" get "$@" --ignore-not-found -o json > dist/app-disabled-existing.json
    else
        printf '%s\n' '{"apiVersion":"v1","kind":"List","items":[]}' > dist/app-disabled-existing.json
    fi
    # Issue #680: OpenShift's namespace admin role has no monitoring.coreos.com
    # rights, so read the ServiceMonitor alone. A Forbidden read is a notice:
    # the object is metrics-only (never an exposure path, unlike the Route) and
    # this identity could not remove one anyway. Any other failure still stops.
    # The client's text is not echoed (it can carry identities).
    if [ "$SERVICEMONITOR_DEPLOY" != "1" ] &&
        printf '%s\n' "$_app_apis" | grep -qx 'servicemonitors.monitoring.coreos.com'; then
        if _sm_err=$($KC -n "$NAMESPACE" get servicemonitor.monitoring.coreos.com/rag-agent \
            --ignore-not-found -o json 2>&1 >dist/app-disabled-servicemonitor.json); then
            python3 - dist/app-disabled-existing.json dist/app-disabled-servicemonitor.json \
                > dist/app-disabled-merged.json <<'PY' || die "cannot merge disabled application resources"
import json, sys
items = []
for path in sys.argv[1:]:
    with open(path) as f:
        raw = f.read()
    if raw.strip():
        doc = json.loads(raw)
        items += doc.get('items', []) if doc.get('kind') == 'List' else [doc]
json.dump({'apiVersion': 'v1', 'kind': 'List', 'items': items}, sys.stdout)
PY
            mv dist/app-disabled-merged.json dist/app-disabled-existing.json
        else
            case "$_sm_err" in
                *"(Forbidden)"*)
                    echo "==> Notice: this identity may not read ServiceMonitors (Forbidden); a leftover ServiceMonitor rag-agent from an earlier metrics-enabled release is NOT checked or removed. Remove it with a monitoring-capable identity if one exists." ;;
                *) die "cannot read disabled ServiceMonitor rag-agent (unexpected client error)" ;;
            esac
        fi
        unset _sm_err
    fi
    python3 scripts/airgap/check_app_ownership.py "$NAMESPACE" --disabled \
        < dist/app-disabled-existing.json > dist/app-disabled-cleanup.json

    # Issue #373: no Route other than the OAuth-protected rag-agent Route may
    # reach the unauthenticated agent HTTP port or Qdrant, whatever the
    # AGENT_ROUTE selection. Refused here, before any mutation.
    # #1074: rag-openwebui is protected the same way; only its own Route may
    # back it, and with the front end on an incompatible owned Route is retired.
    if [ "$AGENT_ROUTE" = "true" ]; then
        check_route_exposure enabled "$ROUTE_DESTINATION_CA_FILE" "${ROUTE_DESTINATION_CA_FILE:-}"
    else
        check_route_exposure disabled "" "${ROUTE_DESTINATION_CA_FILE:-}"
    fi
fi

# CI-rehearsal knobs (never set in the air gap): shrink PVCs / resources for
# the lab run WITHOUT touching the prod values in git. Empty = git values.
QDRANT_STORAGE_SIZE=${QDRANT_STORAGE_SIZE:-}
QDRANT_EXTRA_VALUES=${QDRANT_EXTRA_VALUES:-}
if [ -n "$QDRANT_EXTRA_VALUES" ]; then
    require_ci_rehearsal QDRANT_EXTRA_VALUES "$QDRANT_SIZING_KEYS"
    [ -f "$QDRANT_EXTRA_VALUES" ] || die "QDRANT_EXTRA_VALUES file not found: $QDRANT_EXTRA_VALUES"
fi

# Issue #1070 (R1): the stored Qdrant layout must be one this release can
# serve, read before the first cluster change below (namespace, Route deletes,
# both helm upgrades). A d53595e plain collection refuses here with the
# maintenance-window steps instead of failing at the agent rollout after
# Qdrant and the application were already upgraded.
check_stored_layout

# Issue #1070 R2 (read-only, before any change): a StatefulSet's volume claim
# templates are immutable, so the sizes this run would render must equal the
# existing StatefulSet's. Kept separate from the layout preflight above.
check_qdrant_volume_sizes() {
    [ "${AIRGAP_DRYRUN:-0}" != "1" ] || return 0
    _live=$($KC -n "$NAMESPACE" get statefulset "${QDRANT_RELEASE}-qdrant" -o json --ignore-not-found) ||
        die "cannot read the existing Qdrant StatefulSet"
    printf '%s' "$_live" > dist/qdrant-live-statefulset.json
    set -- -n "$NAMESPACE" -f charts/qdrant-openshift.values.yaml
    if [ -n "$QDRANT_STORAGE_SIZE" ]; then
        set -- "$@" --set-string "persistence.size=$QDRANT_STORAGE_SIZE" \
            --set-string "snapshotPersistence.size=$QDRANT_STORAGE_SIZE"
    fi
    for _assignment in $(python3 scripts/airgap/model_config.py --qdrant-sizing); do
        set -- "$@" --set-string "$_assignment"
    done
    [ -z "$QDRANT_EXTRA_VALUES" ] || set -- "$@" -f "$QDRANT_EXTRA_VALUES"
    helm template "$QDRANT_RELEASE" "$CHART" "$@" > dist/qdrant-size-probe.yaml ||
        die "cannot render the Qdrant release for the volume size check"
    python3 scripts/airgap/check_qdrant_volumes.py dist/qdrant-live-statefulset.json < dist/qdrant-size-probe.yaml ||
        die "requested Qdrant volume sizes differ from the existing StatefulSet"
    unset _live _assignment
}
check_qdrant_volume_sizes

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

# Retire a disabled or confirmed incompatible owned Route before release work.
# Early Helm/rollout failure must not preserve known direct access. A valid
# existing OAuth Route, including operator host/certificate choices, stays.
if [ "${OWNED_ROUTE_PRESENT:-0}" = "1" ] && { [ "$AGENT_ROUTE" != "true" ] || [ "${OWNED_ROUTE_INCOMPATIBLE:-0}" = "1" ]; }; then
    echo "==> Removing disabled or incompatible owned Route rag-agent before the release"
    $KC -n "$NAMESPACE" delete route.route.openshift.io/rag-agent --ignore-not-found ||
        die "cannot remove disabled or incompatible owned Route rag-agent before the release"
fi
if [ "${OPENWEBUI_ROUTE_INCOMPATIBLE:-0}" = "1" ]; then
    echo "==> Removing incompatible owned Route rag-openwebui before the release"
    $KC -n "$NAMESPACE" delete route.route.openshift.io/rag-openwebui --ignore-not-found ||
        die "cannot remove incompatible owned Route rag-openwebui before the release"
fi

echo "==> Helm: Qdrant from the vendored chart with PROD values"
set -- -n "$NAMESPACE" \
    -f charts/qdrant-openshift.values.yaml \
    --set-string "image.repository=$INTERNAL_REGISTRY/qdrant/qdrant" \
    --set-string "image.tag=$QDRANT_TAG" \
    --set-string "persistence.storageClassName=$STORAGE_CLASS" \
    --set-string "snapshotPersistence.storageClassName=$SNAPSHOT_STORAGE_CLASS"
if [ -n "$QDRANT_STORAGE_SIZE" ]; then
    set -- "$@" --set-string "persistence.size=$QDRANT_STORAGE_SIZE" \
        --set-string "snapshotPersistence.size=$QDRANT_STORAGE_SIZE"
fi
# Site sizing keys (#1070 R2), validated by validate_helm_inputs; they win
# over QDRANT_STORAGE_SIZE. Unset keys add nothing, so the default is unchanged.
_sizing=$(python3 scripts/airgap/model_config.py --qdrant-sizing) || die "invalid Qdrant sizing"
for _assignment in $_sizing; do
    set -- "$@" --set-string "$_assignment"
done
unset _sizing _assignment
if [ -n "$QDRANT_EXTRA_VALUES" ]; then
    set -- "$@" -f "$QDRANT_EXTRA_VALUES"
fi
if [ -n "${PULL_SECRET:-}" ]; then
    set -- "$@" --set-string "imagePullSecrets[0].name=$PULL_SECRET"
else
    # values.yaml ships a placeholder pull-secret name; without a real secret
    # that placeholder must never reach the cluster (fail closed, not open).
    set -- "$@" --set "imagePullSecrets=null"
fi
# Issue #1014: whatever the values files and flags produced, the Qdrant render
# must be private and hardened (no Ingress/Route, ClusterIP, API keys, non-
# privileged, read-only root, the verified registry image) before any apply.
helm template "$QDRANT_RELEASE" "$CHART" -n "$NAMESPACE" "$@" > dist/qdrant-rendered.yaml ||
    die "cannot render the Qdrant release for validation"
python3 scripts/airgap/check_qdrant_render.py "$INTERNAL_REGISTRY/qdrant/qdrant:" "$QDRANT_RELEASE" < dist/qdrant-rendered.yaml ||
    die "rendered Qdrant release refused (see above)"
run helm upgrade -i "$QDRANT_RELEASE" "$CHART" "$@"

echo "==> Helm: mainframe-rag application release"
run helm upgrade --install mainframe-rag charts/mainframe-rag \
    --namespace "$NAMESPACE" -f dist/mainframe-rag-release-values.yaml --take-ownership --server-side=false
if [ "$AGENT_ROUTE" = "true" ]; then
    # Require the live OAuth contract, remove a readable owned mismatch,
    # and refuse unknown/unreadable exposure without claiming verification.
    verify_agent_route "${ROUTE_DESTINATION_CA_FILE:-}"
fi
[ "$OPENWEBUI_ENABLED" != "true" ] || verify_openwebui_route "${ROUTE_DESTINATION_CA_FILE:-}"
[ "${METRICS_ENABLED:-false}" = "true" ] || echo "==> Metrics off: ServiceMonitor not deployed"
[ "$OTEL_BUNDLED" = "1" ] || echo "==> OpenObserve not deployed (tracing off or external OTLP endpoint)"

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
    if [ "$OTEL_BUNDLED" = "1" ]; then
        wait_rollout "deploy/openobserve" 120
        [ "$OTEL_COLLECTOR" != "1" ] || wait_rollout "deploy/otelcol" 120
    fi
    if [ "$FEEDBACK_ENABLED" = "true" ]; then
        wait_rollout "statefulset/rag-feedback" 120
    fi
    # Open WebUI migrates its schema at startup (startupProbe allows 10 min).
    if [ "$OPENWEBUI_ENABLED" = "true" ]; then
        wait_rollout "deploy/rag-openwebui" 660
    fi
    # Tags can move after the registry read-back: the running pods must report
    # the verified digests (fail closed; legacy cleanup below is skipped).
    # shellcheck disable=SC2086
    verify_running_images $RUNNING_IMAGES
    # Reconcile disabled legacy resources only after the selected workloads
    # are ready. PVCs are never members of this validated cleanup inventory.
    if [ -s dist/app-disabled-cleanup.json ]; then
        $KC -n "$NAMESPACE" delete -f dist/app-disabled-cleanup.json --ignore-not-found
    fi
fi
# #1074: the docs Pipe is the front end's only model; install it hash-checked.
[ "$OPENWEBUI_ENABLED" != "true" ] || provision_openwebui_pipe
if [ "$AGENT_ROUTE" = "true" ]; then
    echo "Route rag-agent -> svc port oauth, reencrypt, timeout 300s"
fi

next_step "corpus ready? sh scripts/tools/run-task.sh airgap:ingest CORPUS_PVC=<pvc>   |   smoke: sh scripts/tools/run-task.sh airgap:smoke"
