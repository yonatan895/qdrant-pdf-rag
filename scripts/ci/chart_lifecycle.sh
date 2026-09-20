#!/bin/sh
# CI-only first-party chart lifecycle rehearsal (issue #448 H2b).
#
# Exercises the migration-required release sequence against the SAME chart
# source + mapper-generated values intended for the air gap, while the
# Kustomize renderer stays the deployment authority (H3 owns cutover):
#
#   fresh A -> unchanged A -> B -> injected failed B -> retain/recover A
#   -> successful B -> explicit compatible redeploy A
#
# A and B are two generated values files that differ in one rollout-visible
# operator mark (OTEL_DEPLOYMENT_ENVIRONMENT, caller-supplied B_MARKER); the
# images stay identical so the data plane cannot drift between revisions.
#
# Data/PVC expectations (D2/D3; #360/#391 stay authoritative for data
# semantics): the chart release must never own a StatefulSet; the chart
# namespace PVC identity set and the data-plane namespace PVC identity set
# must be byte-identical before and after every mutation; serving smoke
# (expected-substring search through the shared data plane) must pass at
# every stable step and after recovery. No --atomic/--cleanup-on-fail:
# application-chart rollback must never imply a data rollback.
#
# The caller owns cluster/registry/images and an ingested data plane
# (Qdrant + gateway + corpus, e.g. the kind-live-rehearsal lane before this
# step). This script owns only the isolated chart namespace plus its
# rehearsal adapters (ExternalName services pointing at the data plane,
# Secret/ConfigMap copies), which it removes when it created the namespace.
# DATA_NS is read for guards/adapters only; this script never mutates it.
#
# usage: chart_lifecycle.sh <namespace> <chart-dir> <values-a> <values-b>
# Required env: DATA_NS (Qdrant + gateway namespace), B_MARKER (the
#   OTEL_DEPLOYMENT_ENVIRONMENT value carried by values-b; absent in A).
# Optional env: RELEASE (default chart-lifecycle), QDRANT_RELEASE (default
#   qdrant; owns the <release>-apikey Secret in DATA_NS), QDRANT_SVC (default
#   qdrant), GATEWAY_SVC (default test-gateway), GATEWAY_SECRET (default
#   test-gateway-keys), GATEWAY_CA (default test-gateway-ca),
#   PULL_SECRET_SRC (dockerconfigjson Secret in DATA_NS to copy; empty
#   skips), BAD_TAG (default 40 zeros: schema-valid full SHA that no
#   registry serves, so the fault fails at pull time with the release
#   marked failed — a non-hex tag would be rejected by schema validation
#   before any mutation, which proves nothing about rollback),
#   TIMEOUT (default 300s), FAIL_TIMEOUT (default 150s), SCALE_VALUES
#   (optional extra -f overlay, e.g. replicaCount for runner quota),
#   SMOKE_QUERY/SMOKE_EXPECT (default the CI IEA500I pair),
#   KEEP_NAMESPACE (default 0: delete the namespace when we created it).
set -eu

die() { echo "chart-lifecycle: $*" >&2; exit 1; }
[ "$#" -eq 4 ] || { echo "usage: $0 <namespace> <chart-dir> <values-a> <values-b>" >&2; exit 2; }
NS="$1"
CHART_DIR="$2"
VALUES_A="$3"
VALUES_B="$4"
case "$NS" in ''|*[!a-z0-9-]*) echo "chart-lifecycle: invalid namespace" >&2; exit 2 ;; esac
[ -n "${DATA_NS:-}" ] || die "DATA_NS is required (Qdrant + gateway namespace)"
[ -n "${B_MARKER:-}" ] || die "B_MARKER is required (values-b rollout mark)"
[ -f "$CHART_DIR/Chart.yaml" ] || die "chart not found: $CHART_DIR/Chart.yaml"
[ -f "$VALUES_A" ] || die "values-a not found: $VALUES_A"
[ -f "$VALUES_B" ] || die "values-b not found: $VALUES_B"
[ -n "${SCALE_VALUES:-}" ] || SCALE_VALUES=""
[ -f "$SCALE_VALUES" ] 2>/dev/null || [ -z "$SCALE_VALUES" ] || die "SCALE_VALUES not found: $SCALE_VALUES"

RELEASE="${RELEASE:-chart-lifecycle}"
QDRANT_RELEASE="${QDRANT_RELEASE:-qdrant}"
QDRANT_SVC="${QDRANT_SVC:-qdrant}"
GATEWAY_SVC="${GATEWAY_SVC:-test-gateway}"
GATEWAY_SECRET="${GATEWAY_SECRET:-test-gateway-keys}"
GATEWAY_CA="${GATEWAY_CA:-test-gateway-ca}"
PULL_SECRET_SRC="${PULL_SECRET_SRC:-}"
BAD_TAG="${BAD_TAG:-0000000000000000000000000000000000000000}"
TIMEOUT="${TIMEOUT:-300s}"
FAIL_TIMEOUT="${FAIL_TIMEOUT:-150s}"
SMOKE_QUERY="${SMOKE_QUERY:-IEA500I operator message}"
SMOKE_EXPECT="${SMOKE_EXPECT:-IEA500I}"
KEEP_NAMESPACE="${KEEP_NAMESPACE:-0}"

OWN_NS=0
cleanup() {
    _rc=$?
    if [ "$OWN_NS" = 1 ] && [ "$KEEP_NAMESPACE" != 1 ] && [ "$_rc" = 0 ]; then
        kubectl delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    fi
    # On failure the (SHA-unique, ephemeral) namespace is kept for the
    # lane diagnostics below instead of deleting the evidence.
    if [ "$_rc" != 0 ]; then
        echo "==> chart-lifecycle: failure diagnostics (namespace $NS kept)" >&2
        kubectl -n "$NS" get pods,pvc,svc -o wide >&2 || true
        helm -n "$NS" status "$RELEASE" >&2 || true
        helm -n "$NS" history "$RELEASE" 2>/dev/null | tail -12 >&2 || true
        kubectl -n "$NS" logs deploy/rag-agent --all-containers --tail=30 >&2 || true
    fi
    exit "$_rc"
}
trap 'cleanup' EXIT

# Revision tracking needs python3 (already required by map_values.py).
revision() {
    helm status "$RELEASE" -n "$NS" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])'
}
release_status() {
    helm status "$RELEASE" -n "$NS" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["status"])'
}
serving_mark() {
    kubectl -n "$NS" get deploy/rag-agent -o "jsonpath={.spec.template.spec.containers[?(@.name=='agent')].env[?(@.name=='OTEL_DEPLOYMENT_ENVIRONMENT')].value}"
}
pvc_uids() {
    kubectl -n "$1" get pvc -o 'jsonpath={range .items[*]}{.metadata.uid}{"\n"}{end}' | sort
}
chart_manifest_has_no_sts() {
    ! helm get manifest "$RELEASE" -n "$NS" | grep -q '^kind: StatefulSet'
}
smoke() {
    # full: the rollout must complete (steady steps). retained: serving must
    # persist through a failed rollout — rollout status would wedge on the
    # failed ReplicaSet, so gate on availability instead (the search itself
    # then proves the retained revision serves).
    case "${1:-full}" in
        full) kubectl -n "$NS" rollout status deploy/rag-agent --timeout="$TIMEOUT" ;;
        retained) kubectl -n "$NS" wait --for=condition=Available deploy/rag-agent --timeout="$TIMEOUT" ;;
        *) die "smoke mode must be full|retained" ;;
    esac
    kubectl -n "$NS" exec deploy/rag-agent -- python3 /app/scripts/smoke_search.py \
        --url http://localhost:8080 --query "$SMOKE_QUERY" --expect "$SMOKE_EXPECT"
}
# Structural guards after every mutation: chart PVC identities stable and
# expected, data-plane PVC identities untouched, no StatefulSet anywhere in
# the chart release inventory or namespace.
guards() {
    _expect="$1"
    _have="$(pvc_uids "$NS")"
    [ "$_have" = "$_expect" ] || die "chart-namespace PVC identities changed (want [$_expect] got [$_have])"
    _data="$(pvc_uids "$DATA_NS")"
    [ "$_data" = "$DATA_PVC_BASELINE" ] || die "data-plane PVC identities changed (want [$DATA_PVC_BASELINE] got [$_data])"
    [ -z "$(kubectl -n "$NS" get sts --no-headers 2>/dev/null)" ] || die "chart namespace owns a StatefulSet"
    chart_manifest_has_no_sts || die "chart release manifest owns a StatefulSet"
}
expect_mark_a() {
    _mark="$(serving_mark)"
    [ "$_mark" != "$B_MARKER" ] || die "serving A but the B mark is deployed (got $_mark)"
    echo "serving A (mark [$_mark])"
}
expect_mark_b() {
    _mark="$(serving_mark)"
    [ "$_mark" = "$B_MARKER" ] || die "serving B expected (want [$B_MARKER] got [$_mark])"
    echo "serving B (mark [$_mark])"
}
# helm -f argument list shared by install/upgrade (values-a, values-b or the
# fault overlay all ride on the same base + optional quota overlay).
values_flags() {
    printf '%s' "-f $1"
    [ -z "$SCALE_VALUES" ] || printf '%s' " -f $SCALE_VALUES"
}

echo "==> chart-lifecycle: lint chart with values-a"
# shellcheck disable=SC2086
helm lint "$CHART_DIR" $(values_flags "$VALUES_A")

echo "==> chart-lifecycle: isolated namespace $NS (data plane $DATA_NS untouched)"
if kubectl create namespace "$NS" >/dev/null 2>&1; then
    OWN_NS=1
else
    echo "namespace $NS exists; reusing without delete-on-exit"
fi

echo "==> chart-lifecycle: rehearsal adapters (ExternalName + Secret/ConfigMap copies)"
kubectl -n "$NS" create service externalname "$QDRANT_SVC" \
    --external-name "$QDRANT_SVC.$DATA_NS.svc.cluster.local" \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create service externalname "$GATEWAY_SVC" \
    --external-name "$GATEWAY_SVC.$DATA_NS.svc.cluster.local" \
    --dry-run=client -o yaml | kubectl apply -f -
copy_secret() {
    kubectl get secret "$1" -n "$DATA_NS" -o yaml | sed \
        -e "s/^  namespace: .*/  namespace: $NS/" \
        -e '/^  uid: /d' -e '/^  resourceVersion: /d' -e '/^  creationTimestamp: /d' \
        | kubectl apply -f -
}
copy_secret "$QDRANT_RELEASE-apikey"
copy_secret "$GATEWAY_SECRET"
kubectl get configmap "$GATEWAY_CA" -n "$DATA_NS" -o yaml | sed \
    -e "s/^  namespace: .*/  namespace: $NS/" \
    -e '/^  uid: /d' -e '/^  resourceVersion: /d' -e '/^  creationTimestamp: /d' \
    | kubectl apply -f -
if [ -n "$PULL_SECRET_SRC" ]; then
    copy_secret "$PULL_SECRET_SRC"
fi

DATA_PVC_BASELINE="$(pvc_uids "$DATA_NS")"
[ -n "$DATA_PVC_BASELINE" ] || die "no PVCs in data-plane namespace $DATA_NS (ingested data plane required)"

echo "==> chart-lifecycle: fresh A"
# shellcheck disable=SC2086
helm install "$RELEASE" "$CHART_DIR" -n "$NS" $(values_flags "$VALUES_A") --wait --timeout="$TIMEOUT"
REV1="$(revision)"
echo "A installed at revision $REV1"
CHART_PVC="$(pvc_uids "$NS")"
[ -n "$CHART_PVC" ] || die "no PVCs after install (Jaeger Badger claim expected)"
guards "$CHART_PVC"
smoke
expect_mark_a

echo "==> chart-lifecycle: unchanged A"
# shellcheck disable=SC2086
helm upgrade "$RELEASE" "$CHART_DIR" -n "$NS" $(values_flags "$VALUES_A") --wait --timeout="$TIMEOUT"
echo "unchanged A at revision $(revision) (was $REV1)"
guards "$CHART_PVC"
smoke
expect_mark_a

echo "==> chart-lifecycle: B"
# shellcheck disable=SC2086
helm upgrade "$RELEASE" "$CHART_DIR" -n "$NS" $(values_flags "$VALUES_B") --wait --timeout="$TIMEOUT"
REV_B="$(revision)"
echo "B at revision $REV_B"
guards "$CHART_PVC"
smoke
expect_mark_b

echo "==> chart-lifecycle: injected failed B (must fail, serving must retain B)"
# shellcheck disable=SC2086
if helm upgrade "$RELEASE" "$CHART_DIR" -n "$NS" $(values_flags "$VALUES_A") \
    --set "images.agent.tag=$BAD_TAG" --wait --timeout="$FAIL_TIMEOUT"; then
    die "fault injection unexpectedly succeeded (bad tag $BAD_TAG deployed)"
fi
[ "$(release_status)" = "failed" ] || die "expected a failed release after fault injection (got $(release_status))"
guards "$CHART_PVC"
# No mark assert here: the failed upgrade rewrote the Deployment template
# (values-A mark) while the running ReplicaSet is still B. The retained
# smoke below is the stronger proof — only B pods can answer it.
smoke retained
echo "serving B retained through failed revision (smoke green)"

echo "==> chart-lifecycle: retain/recover A (rollback to revision $REV1)"
helm rollback "$RELEASE" "$REV1" -n "$NS" --wait --timeout="$TIMEOUT"
echo "recovered A at revision $(revision)"
guards "$CHART_PVC"
smoke
expect_mark_a

echo "==> chart-lifecycle: successful B"
# shellcheck disable=SC2086
helm upgrade "$RELEASE" "$CHART_DIR" -n "$NS" $(values_flags "$VALUES_B") --wait --timeout="$TIMEOUT"
echo "B at revision $(revision) (was $REV_B)"
guards "$CHART_PVC"
smoke
expect_mark_b

echo "==> chart-lifecycle: explicit compatible redeploy A (upgrade, not rollback)"
# shellcheck disable=SC2086
helm upgrade "$RELEASE" "$CHART_DIR" -n "$NS" $(values_flags "$VALUES_A") --wait --timeout="$TIMEOUT"
echo "redeployed A at revision $(revision)"
guards "$CHART_PVC"
smoke
expect_mark_a

echo "chart lifecycle complete: A/A/B/failed-B/rollback-A/B/redeploy-A with stable PVC identities"
