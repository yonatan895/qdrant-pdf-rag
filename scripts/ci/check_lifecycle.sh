#!/bin/sh
# CI-only persistent-volume lifecycle. Caller runs repeat pipeline afterwards.
set -eu
[ "$#" -eq 1 ] || { echo "usage: $0 <namespace>" >&2; exit 2; }
NS="$1"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# Snapshot creation/recovery is administrative: the serving agent stays read-only.
python3 "$SCRIPT_DIR/check_snapshot.py" "$NS"
# Existing trace must survive Jaeger replacement, not merely a newly sent span.
_trace_id="$(kubectl -n "$NS" exec -i deploy/rag-agent -- python3 - <<'PY_TRACE'
import httpx2
r=httpx2.get('http://jaeger:16686/api/traces',params={'service':'mainframe-rag-agent','limit':1},timeout=30)
r.raise_for_status()
print(r.json()['data'][0]['traceID'])
PY_TRACE
)"
# Capture PVC identities so new empty claims cannot masquerade as recovery.
_before="$(kubectl -n "$NS" get pvc -o 'jsonpath={range .items[*]}{.metadata.uid}{"\n"}{end}' | sort)"
kubectl -n "$NS" rollout restart statefulset/qdrant deploy/rag-agent deploy/jaeger
kubectl -n "$NS" rollout status statefulset/qdrant --timeout=300s
kubectl -n "$NS" rollout status deploy/rag-agent --timeout=300s
kubectl -n "$NS" rollout status deploy/jaeger --timeout=180s
_after="$(kubectl -n "$NS" get pvc -o 'jsonpath={range .items[*]}{.metadata.uid}{"\n"}{end}' | sort)"
[ "$_before" = "$_after" ] || { echo "PVC identities changed" >&2; exit 1; }
kubectl -n "$NS" exec deploy/rag-agent -- python3 /app/scripts/smoke_search.py \
    --url http://localhost:8080 --query 'IEA500I operator message' --expect IEA500I

kubectl -n "$NS" exec -i deploy/rag-agent -- python3 - "$_trace_id" <<'PY_TRACE'
import sys,httpx2
r=httpx2.get('http://jaeger:16686/api/traces/'+sys.argv[1],timeout=30)
r.raise_for_status()
assert r.json()['data'][0]['traceID']==sys.argv[1]
print('Pre-restart trace persisted')
PY_TRACE
