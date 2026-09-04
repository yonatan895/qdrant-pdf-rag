#!/bin/sh
# AIR-GAP SIDE (issue #15): optional smoke against the in-cluster agent.
# Skips cleanly when nothing has been ingested yet (empty collection).
# Override the query with QUERY="..." make airgap-smoke.

. "$(dirname -- "$0")/common.sh"

resolve_aliases
require_env NAMESPACE
if command -v kubectl >/dev/null 2>&1; then KC=kubectl; else KC=oc; fi
QUERY=${QUERY:-IEA500I operator message}

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 -c '... check /healthz ...'"
    echo "[dryrun] $KC -n $NAMESPACE exec -i deploy/rag-agent -- python3 - \"$QUERY\""
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
    echo "WARNING: /healthz probe did not report ok — check Qdrant and embedder connectivity" >&2
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
    exit 0
elif [ "$status" -ne 0 ]; then
    die "search request failed (status $status)"
fi

echo "Smoke query returned hits. For an expected-substring check run:"
echo "  $KC -n $NAMESPACE exec deploy/rag-agent -- python3 /app/scripts/smoke_search.py --url http://localhost:8080 --query \"$QUERY\" --expect <substring>"
