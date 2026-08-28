#!/bin/sh
# AIR-GAP SIDE (issue #15): optional smoke against the in-cluster agent.
# Skips cleanly when nothing has been ingested yet (empty collection).
# Override the query with QUERY="..." make airgap-smoke.

. "$(dirname -- "$0")/common.sh"

resolve_aliases
require_env NAMESPACE
if command -v kubectl >/dev/null 2>&1; then KC=kubectl; else KC=oc; fi
QUERY=${QUERY:-IEA500I operator message}

# exit 3 from the pod = empty result = nothing ingested yet (skip, not fail).
if $KC -n "$NAMESPACE" exec -i deploy/rag-agent -- python - "$QUERY" <<'PYEOF'
import httpx
import sys

query = sys.argv[1]
r = httpx.post("http://localhost:8080/v1/search", json={"query": query, "limit": 8}, timeout=30)
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
echo "  $KC -n $NAMESPACE exec deploy/rag-agent -- python /app/scripts/smoke_search.py --url http://localhost:8080 --query \"$QUERY\" --expect <substring>"
