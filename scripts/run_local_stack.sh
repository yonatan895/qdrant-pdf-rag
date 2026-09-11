#!/bin/sh
# Full local production simulation (local-dev only): the complete topology in
# one supervisor — pinned Qdrant + the real LiteLLM gateway in front of the
# three local vLLM backends + this repo's FastAPI agent + Jaeger. This mirrors
# prod, where the platform team owns vLLM + LiteLLM and this repo owns Qdrant,
# ingest, and the agent: locally we stand up both sides, but every consumer
# leg still goes through the gateway (never straight to vLLM).
#
#   make local-stack                      # Qdrant + Jaeger + gateway + agent
#   CORPUS_DIR=/path make local-stack     # also ingest through the gateway
#
# Tracing is part of the stack, not a flag: agent and ingest export OTLP to
# the local Jaeger (started here via scripts/run_local_jaeger.sh, or reused
# when one already answers on the UI port), and the stack refuses to report
# up until a v1.search span has landed.
#
# Prereqs: docker; the three backends already serving (make local-vllm,
# local-vllm-embed, local-vllm-rerank); .venv. Qdrant is started via
# `make sim-qdrant` (the pinned-image Qdrant owner) when unreachable.
# Ctrl-C stops the agent, gateway, and an owned Jaeger; Qdrant is left for
# `make sim-clean`.
# LOCAL_STACK_DRYRUN=1 prints the ordered plan and validates inputs only.
# Never a product path; never in CI or the air gap.

set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

LOCAL_AGENT_PORT="${LOCAL_AGENT_PORT:-8080}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-mainframe_manuals}"
DENSE_DIM="${DENSE_DIM:-1024}"
GATEWAY_PORT="${GATEWAY_PORT:-4000}"
GATEWAY_ENV_FILE="${GATEWAY_ENV_FILE:-${TMPDIR:-/tmp}/local-stack-gateway-${GATEWAY_PORT}.env}"
JAEGER_PORT="${JAEGER_PORT:-16686}"
JAEGER_OTLP_PORT="${JAEGER_OTLP_PORT:-4318}"
JAEGER_UI_URL="http://127.0.0.1:${JAEGER_PORT}"
OTEL_ENDPOINT="http://127.0.0.1:${JAEGER_OTLP_PORT}"
OTEL_TRACE_TIMEOUT="${OTEL_TRACE_TIMEOUT:-30}"
# Service names are distinct per process so one Jaeger shows both: agent and
# ingest. OTEL_SERVICE_NAME overrides the agent name; ingest keeps its own
# knob so a shared exporter never merges the two services.
AGENT_SERVICE_NAME="${OTEL_SERVICE_NAME:-mainframe-rag-agent}"
INGEST_SERVICE_NAME="${OTEL_INGEST_SERVICE_NAME:-mainframe-rag-ingest}"
# Backend URLs have two views: the host liveness check and the container
# api_base. An explicit GATEWAY_*_URL is authoritative and used for both
# (remote backends are reachable by name from host and container alike); the
# defaults check 127.0.0.1 and hand the gateway host.docker.internal.
REASONING_CHECK_URL="${GATEWAY_REASONING_URL:-http://127.0.0.1:8000/v1}"
EMBED_CHECK_URL="${GATEWAY_EMBED_URL:-http://127.0.0.1:8001/v1}"
RERANK_CHECK_URL="${GATEWAY_RERANK_URL:-http://127.0.0.1:8002/v1}"
REASONING_GW_URL="${GATEWAY_REASONING_URL:-http://host.docker.internal:8000/v1}"
EMBED_GW_URL="${GATEWAY_EMBED_URL:-http://host.docker.internal:8001/v1}"
RERANK_GW_URL="${GATEWAY_RERANK_URL:-http://host.docker.internal:8002/v1}"
CORPUS_DIR="${CORPUS_DIR:-}"
LOG_DIR="${LOCAL_STACK_LOG_DIR:-${TMPDIR:-/tmp}}"
DRYRUN="${LOCAL_STACK_DRYRUN:-0}"
PY="${PY:-$REPO_ROOT/.venv/bin/python}"

die() { echo "ERROR: $1" >&2; exit 1; }
step() { echo "==> $1"; }

# Input validation first: a bad value must die before any docker call.
for _pair in "GATEWAY_PORT:$GATEWAY_PORT" "LOCAL_AGENT_PORT:$LOCAL_AGENT_PORT" "DENSE_DIM:$DENSE_DIM" \
    "JAEGER_PORT:$JAEGER_PORT" "JAEGER_OTLP_PORT:$JAEGER_OTLP_PORT" "OTEL_TRACE_TIMEOUT:$OTEL_TRACE_TIMEOUT"; do
    _name="${_pair%%:*}"; _val="${_pair#*:}"
    case "$_val" in
        ''|*[!0-9]*) die "$_name must be a positive integer, got '$_val'" ;;
        0) die "$_name must be greater than 0, got 0" ;;
    esac
done
unset _pair _name _val
for _pair in "QDRANT_URL:$QDRANT_URL" "GATEWAY_REASONING_URL:$REASONING_CHECK_URL" \
    "GATEWAY_EMBED_URL:$EMBED_CHECK_URL" "GATEWAY_RERANK_URL:$RERANK_CHECK_URL"; do
    _name="${_pair%%:*}"; _val="${_pair#*:}"
    case "$_val" in
        http://*|https://*) ;;
        *) die "$_name must begin with http:// or https://, got '$_val'" ;;
    esac
done
unset _pair _name _val
case "$GATEWAY_ENV_FILE" in
    "$REPO_ROOT"/*) die "GATEWAY_ENV_FILE must not live inside the repo (it holds ephemeral keys): $GATEWAY_ENV_FILE" ;;
esac
if [ -n "$CORPUS_DIR" ] && [ ! -d "$CORPUS_DIR" ]; then
    die "CORPUS_DIR is not a directory: $CORPUS_DIR"
fi

if [ "$DRYRUN" = "1" ]; then
    echo "[plan] 1. backends: check $REASONING_CHECK_URL + $EMBED_CHECK_URL + $RERANK_CHECK_URL (start with 'make local-vllm*')"
    echo "[plan] 2. qdrant:  reuse $QDRANT_URL (start with 'make sim-qdrant' if unreachable)"
    echo "[plan] 3. jaeger:  reuse $JAEGER_UI_URL or start scripts/run_local_jaeger.sh (OTLP $OTEL_ENDPOINT)"
    echo "[plan] 4. gateway: GATEWAY_ENV_FILE=$GATEWAY_ENV_FILE sh scripts/run_local_gateway.sh"
    echo "[plan] 5. probe:   $PY scripts/probe_gateway.py --stream"
    if [ -n "$CORPUS_DIR" ]; then
        echo "[plan] 6. ingest:  OTEL_SERVICE_NAME=$INGEST_SERVICE_NAME $PY -m mainframe_rag.ingest.run_ingest --src $CORPUS_DIR (collection $QDRANT_COLLECTION)"
    else
        echo "[plan] 6. ingest:  skipped (CORPUS_DIR unset)"
    fi
    echo "[plan] 7. agent:   OTEL_EXPORTER_OTLP_ENDPOINT=$OTEL_ENDPOINT $PY -m uvicorn mainframe_rag.agent.app:app --port $LOCAL_AGENT_PORT"
    echo "[plan] 8. smoke:   POST http://127.0.0.1:$LOCAL_AGENT_PORT/v1/search"
    echo "[plan] 9. trace:   poll $JAEGER_UI_URL for service $AGENT_SERVICE_NAME + a v1.search span (timeout ${OTEL_TRACE_TIMEOUT}s)"
    exit 0
fi

command -v docker >/dev/null 2>&1 || die "docker is required for the local stack"
[ -x "$PY" ] || die "venv python not found at $PY — run 'make venv' first"

# The three vLLM backends are the platform-team stand-ins: they must already
# serve, because starting GPU servers is a per-terminal operator action.
_missing=""
for _pair in "reasoning:$REASONING_CHECK_URL" "embed:$EMBED_CHECK_URL" "rerank:$RERANK_CHECK_URL"; do
    _label="${_pair%%:*}"; _url="${_pair#*:}"
    _code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "${_url%/}/models" 2>/dev/null || true)"
    if [ "$_code" != "200" ]; then
        _missing="$_missing $_label($_url)"
    fi
done
unset _pair _label _url _code
if [ -n "$_missing" ]; then
    die "vLLM backends not serving:$_missing
Start them first (one per terminal; GPU servers block):
  make local-vllm         # reasoning :8000
  make local-vllm-embed   # embed     :8001
  make local-vllm-rerank  # rerank    :8002"
fi
unset _missing

# Qdrant is ours: start the pinned sim container when the default URL is down.
if curl -s -m 5 -o /dev/null "$QDRANT_URL/readyz" 2>/dev/null; then
    step "Qdrant reachable at $QDRANT_URL"
else
    case "$QDRANT_URL" in
        *127.0.0.1:6333*|*localhost:6333*) ;;
        *) die "Qdrant unreachable at $QDRANT_URL (custom URL — start it yourself)" ;;
    esac
    step "Qdrant unreachable — starting pinned sim (make sim-qdrant)"
    make -C "$REPO_ROOT" sim-qdrant
    _OK=0
    i=0
    while [ "$i" -lt 60 ]; do
        if curl -s -m 3 -o /dev/null "$QDRANT_URL/readyz" 2>/dev/null; then
            _OK=1
            break
        fi
        i=$((i + 1))
        sleep 1
    done
    [ "$_OK" = "1" ] || die "Qdrant did not answer at $QDRANT_URL within 60s"
fi

# Jaeger is part of the stack, not an option: reuse whatever answers on the
# UI port (an operator-managed Jaeger is not ours to stop), otherwise start
# the pinned owner script and stop it again on exit.
JAEGER_PID=""
JAEGER_OWNED=0
if curl -s -m 3 -o /dev/null "$JAEGER_UI_URL/api/services" 2>/dev/null; then
    step "Jaeger reachable at $JAEGER_UI_URL (reusing)"
else
    step "Starting local Jaeger (OTLP $OTEL_ENDPOINT, UI $JAEGER_UI_URL)"
    JAEGER_PORT="$JAEGER_PORT" JAEGER_OTLP_PORT="$JAEGER_OTLP_PORT" \
        sh "$REPO_ROOT/scripts/run_local_jaeger.sh" >"$LOG_DIR/local-stack-jaeger.log" 2>&1 &
    JAEGER_PID=$!
    JAEGER_OWNED=1
    _i=0
    while [ "$_i" -lt 60 ]; do
        curl -s -m 3 -o /dev/null "$JAEGER_UI_URL/api/services" 2>/dev/null && break
        kill -0 "$JAEGER_PID" 2>/dev/null || die "Jaeger exited early — see $LOG_DIR/local-stack-jaeger.log"
        _i=$((_i + 1))
        sleep 1
    done
    if ! curl -s -m 3 -o /dev/null "$JAEGER_UI_URL/api/services" 2>/dev/null; then
        die "Jaeger did not answer at $JAEGER_UI_URL within 60s — see $LOG_DIR/local-stack-jaeger.log"
    fi
fi

# Gateway lifecycle stays owned by run_local_gateway.sh; this supervisor
# starts it, waits for the leg-env handoff, and stops it on exit.
rm -f "$GATEWAY_ENV_FILE"
step "Starting gateway (model legs behind LiteLLM on :$GATEWAY_PORT)"
GATEWAY_PORT="$GATEWAY_PORT" GATEWAY_ENV_FILE="$GATEWAY_ENV_FILE" \
    GATEWAY_REASONING_URL="$REASONING_GW_URL" \
    GATEWAY_EMBED_URL="$EMBED_GW_URL" \
    GATEWAY_RERANK_URL="$RERANK_GW_URL" \
    GATEWAY_OTEL_ENDPOINT="http://host.docker.internal:${JAEGER_OTLP_PORT}" \
    sh "$REPO_ROOT/scripts/run_local_gateway.sh" >"$LOG_DIR/local-stack-gateway.log" 2>&1 &
GW_PID=$!

AGENT_PID=""
cleanup() {
    if [ -n "$AGENT_PID" ]; then
        kill "$AGENT_PID" 2>/dev/null || true
        wait "$AGENT_PID" 2>/dev/null || true
    fi
    if [ -n "${JAEGER_PID:-}" ]; then
        kill -TERM "$JAEGER_PID" 2>/dev/null || true
        wait "$JAEGER_PID" 2>/dev/null || true
    fi
    if [ -n "$GW_PID" ]; then
        kill -TERM "$GW_PID" 2>/dev/null || true
        # Wait for the gateway's own trap to stop its containers (it owns
        # their names and cleanup); a hard crash leaves 'make local-gateway-stop'.
        wait "$GW_PID" 2>/dev/null || true
    fi
    rm -f "$GATEWAY_ENV_FILE"
}
trap cleanup EXIT INT TERM

_i=0
while [ "$_i" -lt 180 ] && [ ! -s "$GATEWAY_ENV_FILE" ]; do
    kill -0 "$GW_PID" 2>/dev/null || die "gateway exited early — see $LOG_DIR/local-stack-gateway.log"
    _i=$((_i + 1))
    sleep 1
done
[ -s "$GATEWAY_ENV_FILE" ] || die "gateway leg env not written within 180s — see $LOG_DIR/local-stack-gateway.log"
# shellcheck disable=SC1090
. "$GATEWAY_ENV_FILE"
export DENSE_DIM QDRANT_URL QDRANT_COLLECTION
# Tracing is on for every process this stack starts (the local Jaeger above).
export OTEL_EXPORTER_OTLP_ENDPOINT="$OTEL_ENDPOINT"
export OTEL_DEPLOYMENT_ENVIRONMENT="${OTEL_DEPLOYMENT_ENVIRONMENT:-local}"

step "Probing every model leg through the gateway"
"$PY" "$REPO_ROOT/scripts/probe_gateway.py" --stream

if [ -n "$CORPUS_DIR" ]; then
    step "Ingesting $CORPUS_DIR through the gateway (collection $QDRANT_COLLECTION)"
    OTEL_SERVICE_NAME="$INGEST_SERVICE_NAME" "$PY" -m mainframe_rag.ingest.run_ingest --src "$CORPUS_DIR" \
        --progress "$LOG_DIR/local-stack-ingest-progress.jsonl"
fi

_health_code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$LOCAL_AGENT_PORT/healthz" 2>/dev/null || true)"
if [ "$_health_code" = "200" ]; then
    die "port $LOCAL_AGENT_PORT already serves — stop the other agent or set LOCAL_AGENT_PORT"
fi
step "Starting agent on :$LOCAL_AGENT_PORT (OTLP $OTEL_ENDPOINT)"
OTEL_SERVICE_NAME="$AGENT_SERVICE_NAME" LLM_STREAM=true "$PY" -m uvicorn mainframe_rag.agent.app:app \
    --host 127.0.0.1 --port "$LOCAL_AGENT_PORT" >"$LOG_DIR/local-stack-agent.log" 2>&1 &
AGENT_PID=$!

_i=0
while [ "$_i" -lt 60 ]; do
    _health_code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$LOCAL_AGENT_PORT/healthz" 2>/dev/null || true)"
    [ "$_health_code" = "200" ] && break
    kill -0 "$AGENT_PID" 2>/dev/null || die "agent exited early — see $LOG_DIR/local-stack-agent.log"
    _i=$((_i + 1))
    sleep 1
done
[ "$_health_code" = "200" ] || die "agent not healthy within 60s — see $LOG_DIR/local-stack-agent.log"

step "Smoke: POST /v1/search"
_smoke_code="$(curl -s -m 30 -o /dev/null -w '%{http_code}' -X POST \
    "http://127.0.0.1:$LOCAL_AGENT_PORT/v1/search" \
    -H 'Content-Type: application/json' \
    -d '{"query": "system parameter syntax", "top_k": 3}' 2>/dev/null || true)"
[ "$_smoke_code" = "200" ] || die "smoke search returned HTTP $_smoke_code — see $LOG_DIR/local-stack-agent.log"

# Tracing is only "active" when a span actually landed: poll the Jaeger
# query API (batch export means spans arrive seconds after the request).
step "Trace check: waiting for a v1.search span (service $AGENT_SERVICE_NAME)"
_trace_ok=0
_i=0
while [ "$_i" -lt "$OTEL_TRACE_TIMEOUT" ]; do
    if curl -s -m 3 "$JAEGER_UI_URL/api/services" 2>/dev/null | grep -q "\"$AGENT_SERVICE_NAME\"" &&
        curl -s -m 5 "$JAEGER_UI_URL/api/traces?service=$AGENT_SERVICE_NAME&operation=v1.search&limit=1" 2>/dev/null | grep -q '"traceID"'; then
        _trace_ok=1
        break
    fi
    _i=$((_i + 1))
    sleep 1
done
[ "$_trace_ok" = "1" ] || die "no v1.search trace landed in Jaeger within ${OTEL_TRACE_TIMEOUT}s — check $JAEGER_UI_URL and $LOG_DIR/local-stack-agent.log"
if [ -n "$CORPUS_DIR" ]; then
    curl -s -m 3 "$JAEGER_UI_URL/api/services" 2>/dev/null | grep -q "\"$INGEST_SERVICE_NAME\"" ||
        die "no $INGEST_SERVICE_NAME service in Jaeger — ingest tracing did not export"
    step "Trace check: ingest traces present (service $INGEST_SERVICE_NAME)"
fi

cat <<EOF

============================================================================
 FULL LOCAL PRODUCTION SIMULATION UP
============================================================================
 Agent (ours)        : http://127.0.0.1:$LOCAL_AGENT_PORT  (log $LOG_DIR/local-stack-agent.log)
 Qdrant (ours)       : $QDRANT_URL  collection '$QDRANT_COLLECTION'
 Gateway (platform)  : http://localhost:$GATEWAY_PORT/v1  (log $LOG_DIR/local-stack-gateway.log)
 Jaeger (ours)       : $JAEGER_UI_URL  (OTLP $OTEL_ENDPOINT, service $AGENT_SERVICE_NAME)
 Model legs          : all through LiteLLM (keys in $GATEWAY_ENV_FILE)
 Ingest              : ${CORPUS_DIR:-skipped (set CORPUS_DIR=<dir> to ingest)}
 Tracing             : ON — v1.search span verified in Jaeger

 Ctrl-C stops the agent, the gateway, and an owned Jaeger. Qdrant stays for
 'make sim-clean'.
 Try:  curl -s -X POST http://127.0.0.1:$LOCAL_AGENT_PORT/v1/search \\
        -H 'Content-Type: application/json' -d '{"query":"LFAREA","top_k":3}'
EOF

wait "$AGENT_PID"
