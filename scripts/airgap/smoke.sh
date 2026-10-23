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
JAEGER_QUERY_URL=${JAEGER_QUERY_URL:-http://jaeger:16686}
# Trace backend: must match the chart's tracing.backend (jaeger|openobserve).
# OpenObserve needs TRACE_QUERY_URL plus TRACE_QUERY_USER/TRACE_QUERY_PASSWORD
# (from the Secret; sent to the pod on stdin, never on a command line).
TRACE_BACKEND=${TRACE_BACKEND:-jaeger}
case "$TRACE_BACKEND" in
    jaeger) TRACE_QUERY_URL=${TRACE_QUERY_URL:-$JAEGER_QUERY_URL}; TRACE_LABEL=Jaeger ;;
    openobserve)
        [ -n "${TRACE_QUERY_URL:-}" ] || die "TRACE_BACKEND=openobserve requires TRACE_QUERY_URL"
        TRACE_LABEL=OpenObserve ;;
    *) die "TRACE_BACKEND must be jaeger or openobserve" ;;
esac
# Jaeger runs only against the bundled instance; an explicit OpenObserve
# selection always runs its check (the operator named the query endpoint).
if [ "$TRACE_BACKEND" = "jaeger" ] && [ "$JAEGER_DEPLOY" != "1" ]; then
    TRACE_CHECK=0
else
    TRACE_CHECK=1
fi

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 -c '... check /healthz ...'"
    echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 - \"$QUERY\""
    if [ "$OTEL_TRACING_ENABLED" = "1" ] && [ "$TRACE_CHECK" = "1" ]; then
        echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 - '... poll $TRACE_QUERY_URL for a v1.search trace ...'"
    elif [ "$OTEL_TRACING_ENABLED" = "1" ]; then
        echo "[dryrun] tracing check skipped (bundled Jaeger off — trace arrival NOT VERIFIED)"
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
    if [ "$OTEL_TRACING_ENABLED" = "1" ] && [ "$JAEGER_DEPLOY" = "1" ]; then
        TRACING_LINE="Tracing:       SKIPPED (nothing ingested — no request traced yet)"
    elif [ "$OTEL_TRACING_ENABLED" = "1" ]; then
        TRACING_LINE="Tracing:       SKIPPED (bundled Jaeger off — trace arrival NOT VERIFIED; qualify the external collector separately)"
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

# Tracing check (OTel Phase 3): tracing is ON by default, so the search above
# must have landed a v1.search span in the bundled Jaeger query API —
# /healthz alone renders green with a broken endpoint/URL. Polls from the
# agent pod (same net as the OTLP exporter); the export batch interval means
# spans arrive seconds after the request. Only an explicit off sentinel or
# a disabled bundled backend skips the check. That skip proves nothing about
# the configured collector (issue #568): trace arrival stays NOT VERIFIED
# until its own backend check runs (qualification, not this smoke).
# The query runs scripts/trace_arrival.py shipped in the agent image.
run_trace_check() {
    if [ "$TRACE_BACKEND" = "openobserve" ]; then
        [ -n "${TRACE_QUERY_USER:-}" ] && [ -n "${TRACE_QUERY_PASSWORD:-}" ] ||
            die "TRACE_BACKEND=openobserve requires TRACE_QUERY_USER and TRACE_QUERY_PASSWORD"
        printf '%s:%s\n' "$TRACE_QUERY_USER" "$TRACE_QUERY_PASSWORD" |
            $KC -n "$NAMESPACE" exec -i deploy/rag-agent -- python3 /app/scripts/trace_arrival.py \
                --backend "$TRACE_BACKEND" --url "$TRACE_QUERY_URL" --timeout "$TRACE_TIMEOUT" \
                --operation v1.search --auth-stdin
    else
        $KC -n "$NAMESPACE" exec -i deploy/rag-agent -- python3 /app/scripts/trace_arrival.py \
            --backend "$TRACE_BACKEND" --url "$TRACE_QUERY_URL" --timeout "$TRACE_TIMEOUT" \
            --operation v1.search </dev/null
    fi
}

if [ "$OTEL_TRACING_ENABLED" != "1" ]; then
    TRACING_LINE="Tracing:       OFF (disabled)"
elif [ "$TRACE_CHECK" != "1" ]; then
    TRACING_LINE="Tracing:       SKIPPED (bundled Jaeger off — trace arrival NOT VERIFIED; qualify the external collector separately)"
elif run_trace_check; then
    TRACING_LINE="Tracing:       OK (recent v1.search span in $TRACE_LABEL)"
else
    die "no v1.search span landed in $TRACE_LABEL within ${TRACE_TIMEOUT}s — check OTEL_EXPORTER_OTLP_ENDPOINT and the $TRACE_LABEL deployment"
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
