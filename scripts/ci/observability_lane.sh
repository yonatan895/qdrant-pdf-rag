#!/bin/sh
# CI-only kind-observability lane assertions (issue #956, ADR-0006). The images
# arrived through the authenticated kind-registry:5000 path (airgap-load), not
# `kind load`. Prints counts and states only.
set -eu
[ "$#" -eq 1 ] || { echo "usage: $0 <namespace>" >&2; exit 2; }
NS="$1"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# 1. Admitted under PSA restricted and running.
_psa="$(kubectl get namespace "$NS" -o 'jsonpath={.metadata.labels.pod-security\.kubernetes\.io/enforce}')"
[ "$_psa" = "restricted" ] || { echo "namespace $NS is not PSA-enforced restricted (got '$_psa')" >&2; exit 1; }
kubectl -n "$NS" rollout status deploy/openobserve --timeout=300s
_ready="$(kubectl -n "$NS" get deploy openobserve -o 'jsonpath={.status.readyReplicas}')"
[ "${_ready:-0}" -ge 1 ] || { echo "openobserve has no ready replica" >&2; exit 1; }
_phase="$(kubectl -n "$NS" get pods -l app=openobserve -o 'jsonpath={.items[*].status.phase}')"
[ "$_phase" = "Running" ] || { echo "openobserve pod phase: '$_phase'" >&2; exit 1; }
_collector="$(kubectl -n "$NS" get deploy -o name | grep -i otel || true)"
[ -n "$_collector" ] || { echo "no otelcol collector deployment" >&2; exit 1; }
for _d in $_collector; do kubectl -n "$NS" rollout status "$_d" --timeout=300s; done
echo "admission: openobserve and collector running under PSA restricted"

# 2. Trace + metric arrival and the canary scan, run inside the agent pod.
_canary_a="canary-query-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
_canary_b="canary-header-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
kubectl -n "$NS" exec -i deploy/rag-agent -- python3 - "$_canary_a" "$_canary_b" < "$SCRIPT_DIR/observability_checks.py"

# 3. Container logs of the backend and the collector carry no canary either.
_log_hits=0
for _w in deploy/openobserve $_collector; do
    _n="$(kubectl -n "$NS" logs "$_w" --all-containers 2>/dev/null | grep -c -e "$_canary_a" -e "$_canary_b" || true)"
    _log_hits=$((_log_hits + _n))
done
echo "canary_scan_logs: hits=$_log_hits"
[ "$_log_hits" -eq 0 ] || exit 1
