#!/bin/sh
# Local LiteLLM gateway in front of the real local-vllm backends (local-dev only).
# One gateway origin on :4000 fronts reasoning (:8000), embed (:8001), and
# rerank (:8002) so the agent/ingest exercise the platform-gateway wire shape
# locally: single base URL per leg, model-id routing, per-leg Bearer virtual
# keys, native /rerank, and a /v1/score pass-through to the vLLM backend
# (LiteLLM serves no native /v1/score).
#
#   GATEWAY_MASTER_KEY=$(openssl rand -hex 16) make local-gateway
#
# Keys: pass GATEWAY_MASTER_KEY / GATEWAY_LLM_KEY / GATEWAY_EMBED_KEY /
# GATEWAY_RERANK_KEY to reuse stable values, or leave them unset and the
# script mints random sk-local-... keys and prints them once. Set
# GATEWAY_ENV_FILE to also write the leg env (mode 600) for an orchestrator
# (make local-stack); never point it inside the repo. Keys are never
# committed. Minted keys live in the throwaway Postgres volume: env-passed
# values survive restarts; unset values rotate at each start and replace the
# prior key. GATEWAY_RESET_KEYS=1 wipes the store. Key charset is sk- +
# alphanumerics/dash: anything else dies fail-closed (values render into
# YAML unquoted).
# URLs must be http(s); model ids travel verbatim into the routing table.
# Needs docker + the three `make local-vllm*` backends already up.
# Never a product path; never in CI or the air gap.

set -eu

GATEWAY_PORT="${GATEWAY_PORT:-4000}"
GATEWAY_NAME="${GATEWAY_NAME:-local-litellm-gateway}"
# Pinned by digest (tag at pin time: main-stable). Bump deliberately, never
# :latest-shaped drift: the digest is the version under test.
LITELLM_IMAGE="${LITELLM_IMAGE:-ghcr.io/berriai/litellm@sha256:a3715fa7ad8387941ab697259bd2881d68931657247a41984f90fae6d11c62bf}"
# Key store: /key/generate needs a database, so the sim runs a throwaway
# Postgres (named volume = minted keys survive gateway restarts).
# Local-dev only; the password guards a throwaway local DB, never prod.
PG_IMAGE="${PG_IMAGE:-docker.io/library/postgres@sha256:67f41722b7a8cbdb868a44a4995c846eddfdc2973bccb291ce937dce88ad5675}"
PG_NAME="${PG_NAME:-${GATEWAY_NAME}-pg}"
PG_VOLUME="${PG_VOLUME:-${GATEWAY_NAME}-pgdata}"
PG_NET="${PG_NET:-${GATEWAY_NAME}-net}"
PG_PASSWORD="${PG_PASSWORD:-litellm-local-dev}"
GATEWAY_RESET_KEYS="${GATEWAY_RESET_KEYS:-0}"

GATEWAY_REASONING_MODEL="${GATEWAY_REASONING_MODEL:-google/gemma-4-E4B-it-qat-mobile-ct}"
GATEWAY_EMBED_MODEL="${GATEWAY_EMBED_MODEL:-Qwen/Qwen3-Embedding-0.6B}"
GATEWAY_RERANK_MODEL="${GATEWAY_RERANK_MODEL:-BAAI/bge-reranker-v2-m3}"
GATEWAY_REASONING_URL="${GATEWAY_REASONING_URL:-http://host.docker.internal:8000/v1}"
GATEWAY_EMBED_URL="${GATEWAY_EMBED_URL:-http://host.docker.internal:8001/v1}"
GATEWAY_RERANK_URL="${GATEWAY_RERANK_URL:-http://host.docker.internal:8002/v1}"

GATEWAY_MASTER_KEY="${GATEWAY_MASTER_KEY:-}"
GATEWAY_LLM_KEY="${GATEWAY_LLM_KEY:-}"
GATEWAY_EMBED_KEY="${GATEWAY_EMBED_KEY:-}"
GATEWAY_RERANK_KEY="${GATEWAY_RERANK_KEY:-}"
GATEWAY_DRYRUN="${GATEWAY_DRYRUN:-0}"
GATEWAY_DEBUG="${GATEWAY_DEBUG:-0}"
# Optional machine handoff for orchestrators (make local-stack): when set,
# the leg env (URLs, model ids, per-leg keys) is written there mode 600.
# Contains ephemeral keys — never a repo path, never committed.
GATEWAY_ENV_FILE="${GATEWAY_ENV_FILE:-}"
# Gateway-side tracing (local simulation only): when a Jaeger OTLP port is
# reachable, LiteLLM's otel callback exports its spans there so the local
# waterfall shows the platform-stand-in hop. Prod platform config is theirs
# and untouched. GATEWAY_OTEL_ENDPOINT overrides the auto-detect (container
# address — host.docker.internal); empty means tracing off.
GATEWAY_OTEL_ENDPOINT="${GATEWAY_OTEL_ENDPOINT:-}"
GATEWAY_OTEL_OTLP_PORT="${GATEWAY_OTEL_OTLP_PORT:-4318}"
GATEWAY_OTEL_SERVICE_NAME="${GATEWAY_OTEL_SERVICE_NAME:-litellm-local}"
BASE="http://localhost:${GATEWAY_PORT}"

die() { echo "ERROR: $1" >&2; exit 1; }
# Stops both containers by name (never kills PIDs: killing the attached
# `docker run` client detaches and orphans the container, as a first failed
# launch proved). Safe before anything started (names simply miss). The
# network is disposable; the key-store volume is not (remove explicitly).
stop_gateway() {
    docker stop "$GATEWAY_NAME" "$PG_NAME" >/dev/null 2>&1 || true
    docker network rm "$PG_NET" >/dev/null 2>&1 || true
}

# python3 mints keys and reads JSON below; docker is checked after the
# DRYRUN exit so render-only runs stay hermetic (CI has no daemon).
command -v python3 >/dev/null 2>&1 || die "python3 is required (key minting + JSON reads)"

# Operator-supplied keys stay in a tight charset (they render into YAML);
# generated keys are sk-local- + 16 hex bytes.
gen_key() { python3 -c "import secrets; print('sk-local-' + secrets.token_hex(16))"; }
check_key() {
    case "$1" in
        sk-*|sk-local-*) ;;
        *) die "$2 must look like sk-... (hex/dash only), got '$1'" ;;
    esac
    case "$1" in
        *[!A-Za-z0-9-]*)
            # Strip the sk- prefix before judging: the charset above allows
            # alphanumerics and dash only (no $ or backticks — the render is
            # a shell heredoc, so expansion-active chars must never arrive).
            die "$2 must contain only alphanumerics and '-', got '$1'" ;;
    esac
}
for _kv in "MASTER_KEY:${GATEWAY_MASTER_KEY}" "LLM_KEY:${GATEWAY_LLM_KEY}" \
    "EMBED_KEY:${GATEWAY_EMBED_KEY}" "RERANK_KEY:${GATEWAY_RERANK_KEY}"; do
    _name="${_kv%%:*}"; _val="${_kv#*:}"
    [ -n "$_val" ] && check_key "$_val" "GATEWAY_$_name"
done
unset _kv _name _val
case "$PG_PASSWORD" in
    *[!A-Za-z0-9_-]*)
        die "GATEWAY_PG_PASSWORD must contain only alphanumerics, '-' and '_'" ;;
esac
[ -z "$GATEWAY_MASTER_KEY" ] && GATEWAY_MASTER_KEY="$(gen_key)"
[ -z "$GATEWAY_LLM_KEY" ] && GATEWAY_LLM_KEY="$(gen_key)"
[ -z "$GATEWAY_EMBED_KEY" ] && GATEWAY_EMBED_KEY="$(gen_key)"
[ -z "$GATEWAY_RERANK_KEY" ] && GATEWAY_RERANK_KEY="$(gen_key)"

for _url_var in GATEWAY_REASONING_URL GATEWAY_EMBED_URL GATEWAY_RERANK_URL; do
    eval "_url=\${$_url_var:-}"
    case "$_url" in
        http://*|https://*) ;;
        *) die "$_url_var must begin with http:// or https://, got '$_url'" ;;
    esac
done
unset _url_var _url

# /v1/score target: the rerank /v1 origin with any trailing slash trimmed,
# plus /score. (A bare-host RERANK_URL would mistarget — the http(s) gate
# above plus this normalization keep exactly one documented shape.)
_SCORE_BASE="$GATEWAY_RERANK_URL"
case "$_SCORE_BASE" in */) _SCORE_BASE="${_SCORE_BASE%/}" ;; esac
case "$_SCORE_BASE" in */v1) ;; *) _SCORE_BASE="${_SCORE_BASE}/v1" ;; esac
SCORE_TARGET="${_SCORE_BASE}/score"
unset _SCORE_BASE

# Gateway-side tracing (local only): auto-detect the local Jaeger OTLP port
# when no explicit endpoint was given. Dry-run stays hermetic (no probe).
if [ -z "$GATEWAY_OTEL_ENDPOINT" ] && [ "$GATEWAY_DRYRUN" != "1" ]; then
    if curl -s -m 2 -o /dev/null "http://127.0.0.1:${GATEWAY_OTEL_OTLP_PORT}/v1/traces" 2>/dev/null; then
        GATEWAY_OTEL_ENDPOINT="http://host.docker.internal:${GATEWAY_OTEL_OTLP_PORT}"
    fi
fi

CFG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/local-gateway.XXXXXX")"
cat > "${CFG_DIR}/config.yaml" <<EOF
# Rendered by scripts/run_local_gateway.sh (local-dev only, never committed).
model_list:
  - model_name: ${GATEWAY_REASONING_MODEL}
    litellm_params:
      model: openai/${GATEWAY_REASONING_MODEL}
      api_base: ${GATEWAY_REASONING_URL}
      api_key: dummy
      # The agent sends reasoning_effort; the openai adapter would reject it
      # (unsupported param) and litellm.drop_params would silently change
      # behavior. Forward it to vLLM instead — fidelity over convenience.
      allowed_openai_params: ["reasoning_effort"]
  - model_name: ${GATEWAY_EMBED_MODEL}
    litellm_params:
      model: openai/${GATEWAY_EMBED_MODEL}
      api_base: ${GATEWAY_EMBED_URL}
      api_key: dummy
  - model_name: ${GATEWAY_RERANK_MODEL}
    litellm_params:
      # hosted_vllm is the self-hosted vLLM rerank provider in the pinned
      # image (the plain vllm provider is not registered for /rerank).
      model: hosted_vllm/${GATEWAY_RERANK_MODEL}
      api_base: ${GATEWAY_RERANK_URL}
      api_key: dummy
general_settings:
  master_key: ${GATEWAY_MASTER_KEY}
  pass_through_endpoints:
    - path: "/v1/score"
      target: "${SCORE_TARGET}"
      methods: ["POST"]
EOF
if [ -n "$GATEWAY_OTEL_ENDPOINT" ]; then
    cat >> "${CFG_DIR}/config.yaml" <<EOF
litellm_settings:
  callbacks: ["otel"]
EOF
fi

# Leg env handoff (one writer; local-stack sources it). Nothing here is a
# consumer setting beyond the gateway legs — rerank_enabled is included
# because the rerank leg exists by construction in this gateway.
write_gateway_env_file() {
    [ -n "$GATEWAY_ENV_FILE" ] || return 0
    _old_umask=$(umask)
    umask 077
    cat > "$GATEWAY_ENV_FILE" <<EOF
# Generated by scripts/run_local_gateway.sh (local-dev; ephemeral keys)
export LLM_BASE_URL=${BASE}/v1
export LLM_MODEL_REASONING=${GATEWAY_REASONING_MODEL}
export LLM_API_KEY=${GATEWAY_LLM_KEY}
export EMBED_BASE_URL=${BASE}/v1
export EMBED_MODEL=${GATEWAY_EMBED_MODEL}
export EMBED_API_KEY=${GATEWAY_EMBED_KEY}
export RERANK_ENABLED=true
export RERANK_BASE_URL=${BASE}/v1
export RERANK_MODEL=${GATEWAY_RERANK_MODEL}
export RERANK_API_KEY=${GATEWAY_RERANK_KEY}
EOF
    umask "$_old_umask"
    chmod 600 "$GATEWAY_ENV_FILE"
    unset _old_umask
    echo "==> Leg env written to $GATEWAY_ENV_FILE (mode 600)"
}

if [ "$GATEWAY_DRYRUN" = "1" ]; then
    write_gateway_env_file
    echo "$CFG_DIR"
    exit 0
fi
command -v docker >/dev/null 2>&1 || die "docker is required for the local gateway"
trap 'rm -rf "$CFG_DIR"' EXIT

for _taken in "$GATEWAY_NAME" "$PG_NAME"; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$_taken"; then
        die "container '$_taken' already exists — docker stop/remove it first (never auto-killed)"
    fi
done
unset _taken
if [ "$GATEWAY_RESET_KEYS" = "1" ]; then
    docker volume rm "$PG_VOLUME" >/dev/null 2>&1 || true
    echo "==> Key store wiped ($PG_VOLUME removed)"
fi

docker network create "$PG_NET" >/dev/null 2>&1 || true
echo "==> Starting key-store postgres ($PG_NAME, volume $PG_VOLUME)"
docker run -d --rm --name "$PG_NAME" --network "$PG_NET" \
    -e POSTGRES_USER=litellm -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB=litellm \
    -v "${PG_VOLUME}:/var/lib/postgresql/data" \
    "$PG_IMAGE" >/dev/null
echo "==> Waiting for postgres (up to 60s)"
_OK=0
i=0
while [ "$i" -lt 60 ]; do
    if docker exec "$PG_NAME" pg_isready -U litellm >/dev/null 2>&1; then
        _OK=1
        break
    fi
    i=$((i + 1))
    sleep 1
done
if [ "$_OK" != "1" ]; then
    stop_gateway
    die "postgres did not answer within 60s"
fi

echo "==> Starting local LiteLLM gateway ($GATEWAY_NAME on :$GATEWAY_PORT)"
# OTel env only when a local Jaeger was found: container reaches the host
# collector via the host.docker.internal mapping added above.
OTEL_RUN_ARGS=""
if [ -n "$GATEWAY_OTEL_ENDPOINT" ]; then
    echo "==> Gateway tracing on: OTLP $GATEWAY_OTEL_ENDPOINT (service $GATEWAY_OTEL_SERVICE_NAME)"
    OTEL_RUN_ARGS="-e OTEL_EXPORTER_OTLP_ENDPOINT=$GATEWAY_OTEL_ENDPOINT -e OTEL_SERVICE_NAME=$GATEWAY_OTEL_SERVICE_NAME"
fi
# shellcheck disable=SC2086
docker run --rm --name "$GATEWAY_NAME" --network "$PG_NET" \
    --add-host=host.docker.internal:host-gateway \
    -p "${GATEWAY_PORT}:4000" \
    -v "${CFG_DIR}:/app/gateway:ro" \
    -e "DATABASE_URL=postgresql://litellm:${PG_PASSWORD}@${PG_NAME}:5432/litellm" \
    ${OTEL_RUN_ARGS} \
    "$LITELLM_IMAGE" \
    --config /app/gateway/config.yaml ${GATEWAY_DEBUG:+--detailed_debug} \
    >/tmp/"$GATEWAY_NAME".log 2>&1 &
DOCKER_PID=$!
# Ctrl-C stops both containers by name (the attached client alone would
# detach); --rm removes them, the named volume keeps the minted keys.
trap 'stop_gateway; rm -rf "$CFG_DIR"; exit 130' INT TERM

echo "==> Waiting for $BASE/v1/models (up to 90s)"
_OK=0
i=0
while [ "$i" -lt 90 ]; do
    if curl -s -m 3 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" \
        "${BASE}/v1/models" | grep -qx '200'; then
        _OK=1
        break
    fi
    i=$((i + 1))
    sleep 1
done
if [ "$_OK" != "1" ]; then
    stop_gateway
    die "gateway did not answer within 90s — see /tmp/${GATEWAY_NAME}.log"
fi

mint_key() {
    # $1 = key value, $2 = model id, $3 = alias. Idempotent: a key that
    # already authenticates is kept; otherwise any stale alias from an
    # earlier run is deleted, then the key is generated. Keys persist in the
    # pg volume, so stable env values survive gateway restarts.
    _code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $1" \
        "${BASE}/v1/models")"
    if [ "$_code" != "200" ]; then
        curl -s -m 10 -o /dev/null -X POST "${BASE}/key/delete" \
            -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" \
            -H 'Content-Type: application/json' \
            -d "{\"key_aliases\": [\"$3\"]}" || true
        curl -s -m 10 -o /dev/null -X POST "${BASE}/key/generate" \
            -H "Authorization: Bearer ${GATEWAY_MASTER_KEY}" \
            -H 'Content-Type: application/json' \
            -d "{\"key\": \"$1\", \"models\": [\"$2\"], \"key_alias\": \"$3\"}" || true
        _code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' \
            -H "Authorization: Bearer $1" \
            "${BASE}/v1/models")"
    fi
    if [ "$_code" != "200" ]; then
        stop_gateway
        die "key $3 unusable (models HTTP $_code — see /tmp/${GATEWAY_NAME}.log)"
    fi
}
mint_key "$GATEWAY_LLM_KEY" "$GATEWAY_REASONING_MODEL" "local-llm"
mint_key "$GATEWAY_EMBED_KEY" "$GATEWAY_EMBED_MODEL" "local-embed"
mint_key "$GATEWAY_RERANK_KEY" "$GATEWAY_RERANK_MODEL" "local-rerank"
unset -f mint_key

# Each leg must answer with its own key (proves routing + model restriction).
for _leg in "llm:${GATEWAY_LLM_KEY}" "embed:${GATEWAY_EMBED_KEY}" "rerank:${GATEWAY_RERANK_KEY}"; do
    _k="${_leg#*:}"
    if ! curl -s -m 5 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${_k}" \
        "${BASE}/v1/models" | grep -qx '200'; then
        stop_gateway
        die "self-check failed for ${_leg%%:*} leg (see /tmp/${GATEWAY_NAME}.log)"
    fi
done
unset _leg _k
# And a wrong key must 401 (the enforcement the sim exists to prove).
if curl -s -m 5 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer sk-local-wrong" \
    "${BASE}/v1/models" | grep -qx '200'; then
    stop_gateway
    die "self-check failed: wrong key was accepted (see /tmp/${GATEWAY_NAME}.log)"
fi

write_gateway_env_file

cat <<EOF
==> Gateway ready: ${BASE} (container ${GATEWAY_NAME}, log /tmp/${GATEWAY_NAME}.log)
    Keys are stored in volume ${PG_VOLUME} (GATEWAY_RESET_KEYS=1 wipes it);
    never written to git. Point every leg at the gateway, then run
    scripts/probe_gateway.py to confirm and get the RERANK_ENDPOINT_ORDER
    recommendation:

export LLM_BASE_URL=${BASE}/v1 LLM_MODEL_REASONING=${GATEWAY_REASONING_MODEL} LLM_API_KEY=${GATEWAY_LLM_KEY}
export EMBED_BASE_URL=${BASE}/v1 EMBED_MODEL=${GATEWAY_EMBED_MODEL} EMBED_API_KEY=${GATEWAY_EMBED_KEY} DENSE_DIM=1024
export RERANK_ENABLED=true RERANK_BASE_URL=${BASE}/v1 RERANK_MODEL=${GATEWAY_RERANK_MODEL} RERANK_API_KEY=${GATEWAY_RERANK_KEY}

Ctrl-C stops the gateway AND its postgres (keys stay in the volume).
EOF
wait "$DOCKER_PID"
stop_gateway
