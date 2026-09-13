#!/bin/sh
# CI-only persistent-volume lifecycle. Caller runs repeat pipeline afterwards.
set -eu
[ "$#" -eq 1 ] || { echo "usage: $0 <namespace>" >&2; exit 2; }
NS="$1"
# Record point count and create a synthetic snapshot before replacement.
kubectl -n "$NS" exec -i deploy/rag-agent -- python3 - <<'PY'
from mainframe_rag.config import load_settings
import httpx2
import uuid
s=load_settings()
headers={'api-key':s.qdrant_api_key} if s.qdrant_api_key else {}
with httpx2.Client(headers=headers,timeout=30) as client:
    url=s.qdrant_url+'/collections/'+s.qdrant_collection
    r=client.get(url);r.raise_for_status()
    count=r.json()['result']['points_count']
    assert count>0
    r=client.post(url+'/points/scroll',json={'limit':1,'with_vector':True});r.raise_for_status()
    vector=r.json()['result']['points'][0]['vector']['dense']
    query={'query':vector,'using':'dense','limit':5,'params':{'exact':True}}
    r=client.post(url+'/points/query',json=query);r.raise_for_status()
    expected=[p['id'] for p in r.json()['result']['points']]
    r=client.post(url+'/snapshots');r.raise_for_status()
    snapshot=r.json()['result']['name']
    r=client.get(url+'/snapshots/'+snapshot);r.raise_for_status()
    snapshot_bytes=r.content
    restore_url=s.qdrant_url+'/collections/ci-restore-'+uuid.uuid4().hex
    try:
        r=client.post(restore_url+'/snapshots/upload?priority=snapshot',
                      files={'snapshot':('synthetic.snapshot',snapshot_bytes,'application/octet-stream')})
        r.raise_for_status()
        r=client.get(restore_url);r.raise_for_status()
        assert r.json()['result']['points_count']==count
        r=client.post(restore_url+'/points/query',json=query);r.raise_for_status()
        assert [p['id'] for p in r.json()['result']['points']]==expected
        print('Synthetic snapshot restore and exact-query equivalence passed')
    finally:
        r=client.delete(restore_url)
        assert r.status_code in (200,404)

PY
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
