#!/bin/sh
# Configured rerank leg THROUGH real LiteLLM from the application pod (issue #815).
# The agent was deployed with RERANK_ENABLED=true, the stand-in gateway route and
# the minted per-leg rerank-api-key Secret entry. Proves wiring, refusal, order,
# fallback, the causal gateway call count and fail-closed behavior; it says
# nothing about ranking quality, the site's real endpoint or its key.
set -eu
[ "$#" -eq 1 ] || { echo "usage: $0 <namespace>" >&2; exit 2; }
NS="$1"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
WORK="$(mktemp -d)"
in_agent() { kubectl -n "$NS" exec -i deploy/rag-agent -- python3 - "$@" < "$SCRIPT_DIR/rerank_contracts.py"; }
mock() { kubectl -n "$NS" exec -i deploy/test-gateway -- python3 - "$@" < "$SCRIPT_DIR/rerank_mock.py"; }
check() { python3 "$SCRIPT_DIR/rerank_checks.py" "$@"; }
set_fault() {
    kubectl -n "$NS" set env deploy/vllm-mock "MOCK_RERANK_FAULT=$1"
    kubectl -n "$NS" rollout status deploy/vllm-mock --timeout=180s
    mock wait "$1"
}
restore() { set_fault healthy; rm -rf "$WORK"; }
trap restore EXIT
snapshot() { mock stats > "$WORK/$1.json"; }

# 1. Wiring and refusal at the gateway. Requests the gateway refuses must not
#    reach the stand-in: only the two accepted calls do.
in_agent ready
snapshot s0
in_agent gateway-auth
snapshot s1
check delta "$WORK/s0.json" "$WORK/s1.json" score.search=1 rerank.search=1

# 2. Rerank on, score_first (the default): one search is one call, on the score route.
in_agent discover-scope > "$WORK/scope.json"
SCOPE="$(cat "$WORK/scope.json")"
snapshot s2
in_agent search --scope-json "$SCOPE" > "$WORK/on.json"
snapshot s3
check delta "$WORK/s2.json" "$WORK/s3.json" score.search=1 rerank.search=0

# 3. Score route unavailable: the rerank route serves the same stand-in scores
#    (both attempts counted: score refused, rerank answered).
set_fault score_unavailable
snapshot s4
in_agent search --scope-json "$SCOPE" > "$WORK/fallback.json"
snapshot s5
check delta "$WORK/s4.json" "$WORK/s5.json" score.search=1 rerank.search=1
check same-order "$WORK/on.json" "$WORK/fallback.json"

# 4. Every rerank route unavailable: current contract (docs/agent.md#serving-contract),
#    non-identifier search fails closed with the fixed message, identifier lookups
#    bypass rerank, /healthz is 503 with the rerank leg unready. Then recovery.
set_fault unavailable
in_agent degraded --scope-json "$SCOPE"
set_fault healthy
in_agent recovered --scope-json "$SCOPE"

# 5. Rerank off for the same scoped query: no stand-in call, and the order is
#    explained only by the stand-in's scores.
kubectl -n "$NS" set env deploy/rag-agent RERANK_ENABLED=false
kubectl -n "$NS" rollout status deploy/rag-agent --timeout=300s
in_agent off
snapshot s6
in_agent search --scope-json "$SCOPE" > "$WORK/off.json"
snapshot s7
check delta "$WORK/s6.json" "$WORK/s7.json" score.search=0 rerank.search=0
check compare "$WORK/on.json" "$WORK/off.json"
echo "RERANK REHEARSAL PASSED"
