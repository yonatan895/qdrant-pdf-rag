#!/bin/sh
# AIR-GAP SIDE (issue #15): optional smoke against the in-cluster agent.
# Skips cleanly when nothing has been ingested yet (empty collection).
# Override the query with QUERY="..." make airgap-smoke.

. "$(dirname -- "$0")/common.sh"

resolve_aliases
resolve_otel_endpoint
require_env NAMESPACE
KC=${KC:-$(kc)}
QUERY=${QUERY:-IEA500I operator message}
TRACE_TIMEOUT=${TRACE_TIMEOUT:-60}
JAEGER_QUERY_URL=${JAEGER_QUERY_URL:-http://jaeger:16686}

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 -c '... check /healthz ...'"
    echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 - \"$QUERY\""
    if [ "$OTEL_TRACING_ENABLED" = "1" ]; then
        echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 - '... poll $JAEGER_QUERY_URL for a v1.search trace ...'"
    else
        echo "[dryrun] tracing check skipped (OTEL_EXPORTER_OTLP_ENDPOINT=off)"
    fi
    exit 0
fi

# Pre-flight health probe: check Qdrant and embedder connectivity
if ! $KC -n "$NAMESPACE" exec -i deploy/rag-agent -- python3 - <<'PYEOF'
import sys
import httpx2

try:
    r = httpx2.get("http://localhost:8080/healthz", timeout=10)
    data = r.json()
    if r.status_code != 200 or data.get("status") != "ok":
        print(f"healthz status={r.status_code} body={data}", file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f"healthz probe exception: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
then
    die "/healthz probe did not report ok — check Qdrant and embedder connectivity"
fi

# exit 3 from the pod = empty result = nothing ingested yet (skip, not fail).
if $KC -n "$NAMESPACE" exec -i deploy/rag-agent -- python3 - "$QUERY" <<'PYEOF'
import httpx2
import sys

query = sys.argv[1]
r = httpx2.post("http://localhost:8080/v1/search", json={"query": query, "limit": 8}, timeout=30)
r.raise_for_status()
hits = r.json()["hits"]
print(f"hits={len(hits)}")
sys.exit(0 if hits else 3)
PYEOF
then status=0
else status=$?
fi

if [ "$status" -eq 3 ]; then
    echo "SKIP: nothing ingested yet — run make airgap-ingest CORPUS_PVC=<pvc> first"
    if [ "$OTEL_TRACING_ENABLED" = "1" ]; then
        TRACING_LINE="Tracing:       SKIPPED (nothing ingested — no request traced yet)"
    else
        TRACING_LINE="Tracing:       OFF (disabled)"
    fi
    echo ""
    echo "================================================================================"
    echo "                  AIR-GAP PRODUCTION ACCEPTANCE REPORT"
    echo "================================================================================"
    echo "Namespace:       $NAMESPACE"
    echo "Agent /healthz:  OK (Qdrant and embedding services operational)"
    echo "Smoke Search:    SKIPPED (Collection empty — nothing ingested yet)"
    echo "$TRACING_LINE"
    echo "Status:          INFRASTRUCTURE READY (Corpus not yet ingested)"
    echo "================================================================================"
    exit 0
elif [ "$status" -ne 0 ]; then
    die "search request failed (status $status)"
fi

# Tracing check (OTel Phase 3): tracing is ON by default, so the search above
# must have landed a v1.search span in the Jaeger query API — /healthz alone
# renders green with a broken endpoint/URL. Polls from the agent pod (same net
# as the OTLP exporter); the export batch interval means spans arrive seconds
# after the request. Only an explicit off sentinel skips the check.
if [ "$OTEL_TRACING_ENABLED" != "1" ]; then
    TRACING_LINE="Tracing:       OFF (disabled)"
elif $KC -n "$NAMESPACE" exec -i deploy/rag-agent -- python3 - "$TRACE_TIMEOUT" "$JAEGER_QUERY_URL" <<'PYEOF'
import httpx2
import sys
import time

timeout_s = float(sys.argv[1])
base = sys.argv[2].rstrip("/")
deadline = time.time() + timeout_s
while time.time() < deadline:
    try:
        services = httpx2.get(f"{base}/api/services", timeout=10).json().get("data", [])
        names = [s for s in services if "rag-agent" in s or "mainframe-rag" in s]
        if names:
            svc = "mainframe-rag-agent" if "mainframe-rag-agent" in names else names[0]
            traces = httpx2.get(
                f"{base}/api/traces",
                params={"service": svc, "operation": "v1.search"},
                timeout=10,
            ).json().get("data", [])
            if traces:
                print(f"traced service={svc} spans={len(traces[0].get('spans', []))}")
                sys.exit(0)
    except Exception as e:  # noqa: BLE001
        print(f"jaeger poll: {e}", file=sys.stderr)
    time.sleep(5)
print("no v1.search trace landed in Jaeger", file=sys.stderr)
sys.exit(1)
PYEOF
then
    TRACING_LINE="Tracing:       OK (recent v1.search span in Jaeger)"
else
    die "no v1.search span landed in Jaeger within ${TRACE_TIMEOUT}s — check OTEL_EXPORTER_OTLP_ENDPOINT and the Jaeger deployment"
fi

echo "Smoke query returned hits. For an expected-substring check run:"
echo "  $KC -n $NAMESPACE exec deploy/rag-agent -- python3 /app/scripts/smoke_search.py --url http://localhost:8080 --query \"$QUERY\" --expect <substring>"

echo ""
echo "================================================================================"
echo "                  AIR-GAP PRODUCTION ACCEPTANCE REPORT"
echo "================================================================================"
echo "Namespace:       $NAMESPACE"
echo "Agent /healthz:  OK (Qdrant and embedding services operational)"
echo "Smoke Search:    OK (Query: \"$QUERY\")"
echo "$TRACING_LINE"
echo "Status:          ACCEPTANCE CRITERIA PASSED"
echo "================================================================================"
