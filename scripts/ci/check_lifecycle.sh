#!/bin/sh
# CI-only persistent-volume lifecycle. Caller runs repeat pipeline afterwards.
set -eu
[ "$#" -eq 1 ] || { echo "usage: $0 <namespace>" >&2; exit 2; }
NS="$1"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# Snapshot creation/recovery is administrative: the serving agent stays read-only.
# Restores the data and completions pair into scratch names and compares stored content.
python3 "$SCRIPT_DIR/check_snapshot.py" "$NS"
# Existing trace must survive OpenObserve replacement, not merely a newly sent
# span. The query runs inside the agent pod with the credential the chart already
# gives it (OTEL_EXPORTER_OTLP_HEADERS); CI never reads the Secret.
_o2_query='
import json, os, sys, time, urllib.parse
import httpx2
header = urllib.parse.unquote(os.environ["OTEL_EXPORTER_OTLP_HEADERS"]).split("Authorization=", 1)[1]
now = int(time.time() * 1_000_000)
where = "trace_id = \x27%s\x27" % sys.argv[1] if len(sys.argv) > 1 else "service_name = \x27mainframe-rag-agent\x27 AND operation_name = \x27v1.search\x27"
sql = "SELECT trace_id FROM \"default\" WHERE " + where + " ORDER BY _timestamp DESC"
body = {"query": {"sql": sql, "start_time": now - 3600 * 1_000_000, "end_time": now + 60_000_000, "from": 0, "size": 1}}
r = httpx2.post("http://openobserve:5080/api/default/_search", params={"type": "traces"}, json=body, headers={"Authorization": header}, timeout=30)
r.raise_for_status()
print(r.json()["hits"][0]["trace_id"])
'
_trace_id="$(kubectl -n "$NS" exec -i deploy/rag-agent -- python3 -c "$_o2_query")"
# Capture PVC identities so new empty claims cannot masquerade as recovery.
_before="$(kubectl -n "$NS" get pvc -o 'jsonpath={range .items[*]}{.metadata.uid}{"\n"}{end}' | sort)"
kubectl -n "$NS" rollout restart statefulset/qdrant deploy/rag-agent deploy/openobserve
kubectl -n "$NS" rollout status statefulset/qdrant --timeout=300s
kubectl -n "$NS" rollout status deploy/rag-agent --timeout=300s
kubectl -n "$NS" rollout status deploy/openobserve --timeout=180s
_after="$(kubectl -n "$NS" get pvc -o 'jsonpath={range .items[*]}{.metadata.uid}{"\n"}{end}' | sort)"
[ "$_before" = "$_after" ] || { echo "PVC identities changed" >&2; exit 1; }
kubectl -n "$NS" exec deploy/rag-agent -- python3 /app/scripts/smoke_search.py \
    --url http://localhost:8080 --query 'IEA500I operator message' --expect IEA500I

_after_id="$(kubectl -n "$NS" exec -i deploy/rag-agent -- python3 -c "$_o2_query" "$_trace_id")"
[ "$_after_id" = "$_trace_id" ] || { echo "pre-restart trace missing after restart" >&2; exit 1; }
echo 'Pre-restart trace persisted'
