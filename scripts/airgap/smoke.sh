#!/bin/sh
# AIR-GAP SIDE (issue #15): optional smoke against the in-cluster agent.
# Skips cleanly when nothing has been ingested yet (empty collection).
# Override the query with QUERY="..." sh scripts/tools/run-task.sh airgap:smoke.

. "$(dirname -- "$0")/common.sh"

resolve_aliases
resolve_observability
require_env NAMESPACE
KC=${KC:-$(kc)}
QUERY=${QUERY:-IEA500I operator message}
TRACE_TIMEOUT=${TRACE_TIMEOUT:-60}
# Trace arrival is checked in OpenObserve (issue #956). TRACE_QUERY_URL and
# TRACE_QUERY_USER/TRACE_QUERY_PASSWORD (from the Secret; sent to the pod on
# stdin, never on a command line) are required whenever tracing is enabled.

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 -c '... check /healthz ...'"
    echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 - \"$QUERY\""
    if [ "$OTEL_TRACING_ENABLED" = "1" ]; then
        echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 /app/scripts/trace_arrival.py --url ${TRACE_QUERY_URL:-<TRACE_QUERY_URL>} --operation v1.search --auth-stdin"
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
    die "/healthz probe did not report ok — check the printed body: representation reembed_required/pending needs --reingest, legacy needs a new collection (unsupported stored data), unknown means the store is unreachable, otherwise Qdrant/embedder connectivity"
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
    echo "SKIP: nothing ingested yet — run sh scripts/tools/run-task.sh airgap:ingest CORPUS_PVC=<pvc> first"
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
    echo "Agent /healthz:  OK (Qdrant, embedding, and representation gate operational)"
    echo "Smoke Search:    SKIPPED (Collection empty — nothing ingested yet)"
    echo "$TRACING_LINE"
    echo "Status:          INFRASTRUCTURE READY (Corpus not yet ingested)"
    echo "================================================================================"
    exit 0
elif [ "$status" -ne 0 ]; then
    die "search request failed (status $status)"
fi

# Tracing check: with tracing enabled the search above must have landed a
# v1.search span in OpenObserve — /healthz alone renders green with a broken
# endpoint/URL. Polls from the agent pod (same net as the OTLP exporter) with
# scripts/trace_arrival.py from the agent image; spans arrive seconds after
# the request (export batch interval).
run_trace_check() {
    printf '%s:%s\n' "$TRACE_QUERY_USER" "$TRACE_QUERY_PASSWORD" |
        $KC -n "$NAMESPACE" exec -i deploy/rag-agent -- python3 /app/scripts/trace_arrival.py \
            --url "$TRACE_QUERY_URL" --timeout "$TRACE_TIMEOUT" --operation v1.search --auth-stdin
}

if [ "$OTEL_TRACING_ENABLED" != "1" ]; then
    TRACING_LINE="Tracing:       OFF (disabled)"
else
    [ -n "${TRACE_QUERY_URL:-}" ] || die "tracing is enabled: TRACE_QUERY_URL is required"
    [ -n "${TRACE_QUERY_USER:-}" ] && [ -n "${TRACE_QUERY_PASSWORD:-}" ] ||
        die "tracing is enabled: TRACE_QUERY_USER and TRACE_QUERY_PASSWORD are required"
    if run_trace_check; then
        TRACING_LINE="Tracing:       OK (recent v1.search span in OpenObserve)"
    else
        die "no v1.search span landed in OpenObserve within ${TRACE_TIMEOUT}s — check OTEL_EXPORTER_OTLP_ENDPOINT and the OpenObserve deployment"
    fi
fi

echo "Smoke query returned hits. For an expected-substring check run:"
echo "  $KC -n $NAMESPACE exec deploy/rag-agent -- python3 /app/scripts/smoke_search.py --url http://localhost:8080 --query \"$QUERY\" --expect <substring>"

echo ""
echo "================================================================================"
echo "                  AIR-GAP PRODUCTION ACCEPTANCE REPORT"
echo "================================================================================"
echo "Namespace:       $NAMESPACE"
echo "Agent /healthz:  OK (Qdrant, embedding, and representation gate operational)"
echo "Smoke Search:    OK (Query: \"$QUERY\")"
echo "$TRACING_LINE"
echo "Status:          ACCEPTANCE CRITERIA PASSED"
echo "================================================================================"
