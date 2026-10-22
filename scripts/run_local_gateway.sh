#!/bin/sh
# Local LiteLLM gateway in front of the real local-vllm backends (local-dev only).
# One gateway origin on :4000 fronts reasoning (:8000), embed (:8001), and
# rerank (:8002) so the agent/ingest exercise the platform-gateway wire shape
# locally: single base URL per leg, model-id routing, per-leg Bearer virtual
# keys, native /rerank, and a /v1/score pass-through to the vLLM backend
# (LiteLLM serves no native /v1/score).
#
#   GATEWAY_MASTER_KEY=$(openssl rand -hex 16) sh scripts/tools/run-task.sh local:gateway:up
#   sh scripts/run_local_gateway.sh transport-probe  # connected Colab transport only
#   sh scripts/run_local_gateway.sh transport-release  # exact owned resources
# Probe/release use GATEWAY_TRANSPORT_NETWORK_ID and the returned created flag
# as GATEWAY_TRANSPORT_NETWORK_CREATED. Pass both to the later normal launch.
# This proves image/network/upstream reachability, not a running gateway service.
# GATEWAY_RUN_OWNER is a fresh run identity; transport-release also recovers
# its labelled gateway/Postgres containers. Never reuse another run's owner.
#
# Keys: pass GATEWAY_MASTER_KEY / GATEWAY_LLM_KEY / GATEWAY_EMBED_KEY /
# GATEWAY_RERANK_KEY to reuse stable values, or leave them unset and the
# script mints random sk-local-... keys and prints them once. Set
# GATEWAY_ENV_FILE to also write the leg env (mode 600) for an orchestrator
# (sh scripts/tools/run-task.sh local:stack); never point it inside the repo. Keys are never
# committed. Minted keys live in the throwaway Postgres volume: env-passed
# values survive restarts; unset values rotate at each start and replace the
# prior key. GATEWAY_RESET_KEYS=1 wipes the store. Key charset is sk- +
# alphanumerics/dash: anything else dies fail-closed (values render into
# YAML unquoted).
# URLs must be http(s); model ids travel verbatim into the routing table.
# Needs Docker and the enabled model backends already up. Two-model profiles
# run local:llm + local:embed and explicitly disable reranking in consumers;
# the configured rerank route does not start a backend.
# GATEWAY_REASONING=off (default on; #862 retrieval-only Colab profiles) serves
# only the embed and rerank legs: no reasoning route, /tokenize pass-through,
# LLM key or LLM_* handoff exports. GATEWAY_REASONING_URL is then unused.
# Never a product path; never in CI or the air gap.

set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

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
GATEWAY_DOCKER_BIN="${GATEWAY_DOCKER_BIN:-docker}"
GATEWAY_TRANSPORT_NETWORK_ID="${GATEWAY_TRANSPORT_NETWORK_ID:-}"
GATEWAY_TRANSPORT_NETWORK_CREATED="${GATEWAY_TRANSPORT_NETWORK_CREATED:-0}"
GATEWAY_RUN_OWNER="${GATEWAY_RUN_OWNER:-}"
GATEWAY_RUN_LABEL="io.mainframe-rag.gateway-run"
GATEWAY_NETWORK_TARGET="$PG_NET"
PG_PASSWORD="${PG_PASSWORD:-litellm-local-dev}"
GATEWAY_RESET_KEYS="${GATEWAY_RESET_KEYS:-0}"

GATEWAY_REASONING_MODEL="${GATEWAY_REASONING_MODEL:-google/gemma-4-E4B-it-qat-mobile-ct}"
GATEWAY_EMBED_MODEL="${GATEWAY_EMBED_MODEL:-Qwen/Qwen3-Embedding-0.6B}"
# Immutable weight identity attested by the caller, not inferred from an alias.
# Existing local profiles retain their simulation label when unset or empty.
GATEWAY_EMBED_MODEL_REVISION="${GATEWAY_EMBED_MODEL_REVISION:-local:${GATEWAY_EMBED_MODEL}}"
GATEWAY_RERANK_MODEL="${GATEWAY_RERANK_MODEL:-BAAI/bge-reranker-v2-m3}"
GATEWAY_REASONING_URL="${GATEWAY_REASONING_URL:-http://host.docker.internal:8000/v1}"
GATEWAY_EMBED_URL="${GATEWAY_EMBED_URL:-http://host.docker.internal:8001/v1}"
GATEWAY_RERANK_URL="${GATEWAY_RERANK_URL:-http://host.docker.internal:8002/v1}"

GATEWAY_MASTER_KEY="${GATEWAY_MASTER_KEY:-}"
GATEWAY_LLM_KEY="${GATEWAY_LLM_KEY:-}"
GATEWAY_EMBED_KEY="${GATEWAY_EMBED_KEY:-}"
GATEWAY_RERANK_KEY="${GATEWAY_RERANK_KEY:-}"
GATEWAY_REASONING="${GATEWAY_REASONING:-on}"
GATEWAY_DRYRUN="${GATEWAY_DRYRUN:-0}"
# LiteLLM --detailed_debug logs full request bodies (prompts with manual
# excerpts, embedding inputs): local short diagnosis only, never default
# (issue #590). Strictly 1/true to enable; unset, empty, 0 or false keep it off.
GATEWAY_DEBUG="${GATEWAY_DEBUG:-0}"
# Container log on disk, not /tmp: /tmp is tmpfs on the reference WSL host, so
# a long run's log is RAM (issue #590). The previous run's log rotates to .1.
GATEWAY_LOG="${GATEWAY_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/mainframe-rag/${GATEWAY_NAME}.log}"
# Optional, complete resource envelope for this gateway's two owned containers.
# Colab freezes explicit values; ordinary local profiles retain their defaults.
GATEWAY_MEMORY_MIB="${GATEWAY_MEMORY_MIB:-}"
PG_MEMORY_MIB="${PG_MEMORY_MIB:-}"
GATEWAY_LOG_MAX_BYTES="${GATEWAY_LOG_MAX_BYTES:-}"
GATEWAY_DOCKER_LOG_MAX_BYTES="${GATEWAY_DOCKER_LOG_MAX_BYTES:-}"
GATEWAY_DOCKER_LOG_FILES="${GATEWAY_DOCKER_LOG_FILES:-}"
# Optional machine handoff for orchestrators (sh scripts/tools/run-task.sh local:stack): when set,
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
gateway_docker() { "$GATEWAY_DOCKER_BIN" "$@"; }
case "${1:-serve}" in
    serve|transport-probe|transport-release) GATEWAY_OPERATION="${1:-serve}" ;;
    *) die "unknown local gateway operation" ;;
esac
[ "$#" -le 1 ] || die "unexpected local gateway arguments"
case "$GATEWAY_TRANSPORT_NETWORK_CREATED" in
    0|1) ;;
    *) die "transport network ownership must be 0 or 1" ;;
esac
case "$GATEWAY_RUN_OWNER" in
    *[!A-Za-z0-9_.-]*) die "gateway run ownership must be a bounded opaque identity" ;;
esac
[ "${#GATEWAY_RUN_OWNER}" -le 128 ] || die "gateway run ownership must be a bounded opaque identity"
case "$GATEWAY_REASONING" in
    on|off) ;;
    *) die "GATEWAY_REASONING must be on or off, got '$GATEWAY_REASONING'" ;;
esac
case "$GATEWAY_DEBUG" in
    1|true) GATEWAY_DEBUG_ARGS="--detailed_debug" ;;
    0|false|'') GATEWAY_DEBUG_ARGS="" ;;
    *) die "GATEWAY_DEBUG must be 1/true or 0/false/empty, got '$GATEWAY_DEBUG'" ;;
esac
GATEWAY_LIMITS_ENABLED=0
if [ "$GATEWAY_OPERATION" = serve ] && \
    [ -n "$GATEWAY_MEMORY_MIB$PG_MEMORY_MIB$GATEWAY_LOG_MAX_BYTES$GATEWAY_DOCKER_LOG_MAX_BYTES$GATEWAY_DOCKER_LOG_FILES" ]; then
    for _bound in "$GATEWAY_MEMORY_MIB:1048576" "$PG_MEMORY_MIB:1048576" \
        "$GATEWAY_LOG_MAX_BYTES:67108864" "$GATEWAY_DOCKER_LOG_MAX_BYTES:67108864" \
        "$GATEWAY_DOCKER_LOG_FILES:8"; do
        _value="${_bound%:*}"; _maximum="${_bound#*:}"
        case "$_value" in
            ''|*[!0-9]*|0*|??????????*) die "gateway resource limits require complete positive bounded integers" ;;
        esac
        [ "$_value" -le "$_maximum" ] || die "gateway resource limits require complete positive bounded integers"
    done
    [ "$GATEWAY_LOG_MAX_BYTES" -ge 64 ] || die "gateway resource limits require at least 64 host log bytes"
    unset _bound _value _maximum
    command -v mkfifo >/dev/null 2>&1 || die "mkfifo is required for bounded gateway capture"
    [ -f "$SCRIPT_DIR/gateway/log_capture.py" ] && [ -r "$SCRIPT_DIR/gateway/log_capture.py" ] || \
        die "bounded gateway capture source is unavailable"
    GATEWAY_LIMITS_ENABLED=1
fi

service_docker() {
    _service_memory="$1"; shift
    if [ "$GATEWAY_LIMITS_ENABLED" = 1 ]; then
        gateway_docker run --memory "${_service_memory}m" --memory-swap "${_service_memory}m" \
            --log-driver json-file --log-opt "max-size=$GATEWAY_DOCKER_LOG_MAX_BYTES" \
            --log-opt "max-file=$GATEWAY_DOCKER_LOG_FILES" "$@"
    else
        gateway_docker run "$@"
    fi
}

LOG_CAPTURE_PID=""
stop_log_capture() {
    if [ -n "$LOG_CAPTURE_PID" ]; then
        kill -TERM "$LOG_CAPTURE_PID" 2>/dev/null || true
        wait "$LOG_CAPTURE_PID" 2>/dev/null || true
        LOG_CAPTURE_PID=""
    fi
}
# The daemon can finish creating a container after its client is interrupted.
# Discover by the admitted run label, then recheck/remove full IDs, never names.
# Validate the whole bounded set before removing any object. Volumes persist.
owned_container_ids() {
    gateway_docker ps -aq --no-trunc --filter "label=$GATEWAY_RUN_LABEL=$GATEWAY_RUN_OWNER"
}

require_fresh_container_owner() {
    [ -n "$GATEWAY_RUN_OWNER" ] || return 0
    _existing_owned="$(owned_container_ids)" || die "gateway run ownership could not be verified"
    [ -z "$_existing_owned" ] || die "gateway run owner already exists; use its recorded recovery"
}

release_owned_containers() {
    [ -n "$GATEWAY_RUN_OWNER" ] || return 0
    _owned_ids="$(owned_container_ids)" || return 1
    _owned_count=0
    for _owned_id in $_owned_ids; do
        _owned_count=$((_owned_count + 1))
        [ "$_owned_count" -le 2 ] || return 1
        case "$_owned_id" in ''|*[!0-9a-f]*) return 1 ;; esac
        [ "${#_owned_id}" -eq 64 ] || return 1
        _owned_info="$(gateway_docker inspect "$_owned_id" --format \
            '{{.Id}} {{index .Config.Labels "io.mainframe-rag.gateway-run"}}' 2>/dev/null)" || return 1
        [ "$_owned_info" = "$_owned_id $GATEWAY_RUN_OWNER" ] || return 1
    done
    for _owned_id in $_owned_ids; do
        # Recheck immediately before deletion: a changed label is not authority.
        _owned_info="$(gateway_docker inspect "$_owned_id" --format \
            '{{.Id}} {{index .Config.Labels "io.mainframe-rag.gateway-run"}}' 2>/dev/null)" || return 1
        [ "$_owned_info" = "$_owned_id $GATEWAY_RUN_OWNER" ] || return 1
        gateway_docker rm -f "$_owned_id" >/dev/null 2>&1 || return 1
    done
    _remaining_owned="$(owned_container_ids)" || return 1
    [ -z "$_remaining_owned" ]
}

stop_gateway() {
    release_owned_containers || return 1
    release_transport_network
}

# Docker IDs, rather than names, keep a replaced network out of an admitted run.
transport_network_id() {
    _network_info="$(gateway_docker network inspect "$PG_NET" --format '{{.Id}} {{.Driver}}' 2>/dev/null)" || return 1
    _network_id="${_network_info% *}"
    [ "${_network_info##* }" = bridge ] || return 1
    case "$_network_id" in ''|*[!0-9a-f]*) return 1 ;; esac
    [ "${#_network_id}" -eq 64 ] || return 1
    printf '%s\n' "$_network_id"
}

release_transport_network() {
    [ "$GATEWAY_TRANSPORT_NETWORK_CREATED" = 1 ] || return 0
    [ -n "$GATEWAY_TRANSPORT_NETWORK_ID" ] || return 1
    _remaining_network_id="$(gateway_docker network ls --no-trunc \
        --filter "id=$GATEWAY_TRANSPORT_NETWORK_ID" --format '{{.ID}}' 2>/dev/null)" || return 1
    [ -n "$_remaining_network_id" ] || return 0
    [ "$_remaining_network_id" = "$GATEWAY_TRANSPORT_NETWORK_ID" ] || return 1
    _current_network_id="$(transport_network_id)" || return 1
    [ "$_current_network_id" = "$GATEWAY_TRANSPORT_NETWORK_ID" ] || return 1
    # Removal by ID cannot delete a new object that subsequently reuses PG_NET.
    # Docker itself refuses a network that still has attached containers.
    gateway_docker network rm "$GATEWAY_TRANSPORT_NETWORK_ID" >/dev/null 2>&1
}

prepare_transport_network() {
    _probe_created=0
    if _probe_network_id="$(transport_network_id)"; then
        if [ -n "$GATEWAY_TRANSPORT_NETWORK_ID" ]; then
            [ "$_probe_network_id" = "$GATEWAY_TRANSPORT_NETWORK_ID" ] || die "transport network identity changed"
            _probe_created="$GATEWAY_TRANSPORT_NETWORK_CREATED"
        fi
    else
        [ -z "$GATEWAY_TRANSPORT_NETWORK_ID" ] || die "transport network identity changed"
        _created_network_id="$(gateway_docker network create "$PG_NET" 2>/dev/null)" || die "transport network could not be prepared"
        _probe_created=1
        if ! _probe_network_id="$(transport_network_id)" || [ "$_probe_network_id" != "$_created_network_id" ]; then
            gateway_docker network rm "$_created_network_id" >/dev/null 2>&1 || true
            die "transport network identity is unavailable"
        fi
    fi
    GATEWAY_TRANSPORT_NETWORK_ID="$_probe_network_id"
    GATEWAY_TRANSPORT_NETWORK_CREATED="$_probe_created"
    GATEWAY_NETWORK_TARGET="$GATEWAY_TRANSPORT_NETWORK_ID"
}

# python3 mints keys and reads JSON below; docker is checked after the
# DRYRUN exit so render-only runs stay hermetic (CI has no daemon).
command -v python3 >/dev/null 2>&1 || die "python3 is required (key minting + JSON reads)"
# The handoff is sourced as shell code. Preserve the opaque revision literally,
# including whitespace/quotes, without interpreting it as commands on sourcing.
EMBED_MODEL_REVISION_SH="$(python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$GATEWAY_EMBED_MODEL_REVISION")"

if [ "$GATEWAY_OPERATION" != serve ]; then
    [ "$GATEWAY_DRYRUN" != 1 ] || die "transport operations cannot run in render-only mode"
    command -v "$GATEWAY_DOCKER_BIN" >/dev/null 2>&1 || die "docker is required for the local gateway"
    if [ "$GATEWAY_OPERATION" = transport-release ]; then
        stop_gateway || die "owned gateway resources could not be released"
        exit 0
    fi
    # Transport-only mode never generates host keys/config/log files or starts the
    # database/gateway. Arguments carry URLs as data, never generated code.
    if ! python3 - "$LITELLM_IMAGE" "$GATEWAY_REASONING_URL" "$GATEWAY_EMBED_URL" <<'PY'
import ipaddress
import re
import sys
from urllib.parse import urlsplit

try:
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", sys.argv[1]):
        raise ValueError
    for raw in sys.argv[2:]:
        url = urlsplit(raw)
        address = ipaddress.ip_address(url.hostname)
        if (url.scheme != "http" or address.version != 4 or address.is_unspecified
                or address.is_multicast or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path not in ("/v1", "/v1/")
                or url.port is None or not 1024 <= url.port <= 65535):
            raise ValueError
except (TypeError, ValueError):
    sys.exit(1)
PY
    then
        die "transport probe requires a pinned image and numeric IPv4 HTTP backend URLs"
    fi
    gateway_docker image inspect "$LITELLM_IMAGE" >/dev/null 2>&1 || die "transport probe requires the pinned image already present"
    require_fresh_container_owner
    prepare_transport_network
    trap 'release_transport_network || true; exit 130' INT TERM
    # Emit ownership before the potentially interrupted probe, so recovery can
    # release an owned network. This identity alone is never a passing result:
    # the caller must also require this operation's exit status to be zero.
    python3 - "$PG_NET" "$_probe_network_id" "$_probe_created" "$LITELLM_IMAGE" \
        "$GATEWAY_REASONING_URL" "$GATEWAY_EMBED_URL" <<'PY'
import json
import sys
print(json.dumps({"network_name": sys.argv[1], "network_id": sys.argv[2],
                  "created": sys.argv[3] == "1", "gateway_image": sys.argv[4],
                  "reasoning_url": sys.argv[5], "embedding_url": sys.argv[6]}))
PY
    if ! gateway_docker run --rm --pull never --network "$_probe_network_id" \
        --add-host=host.docker.internal:host-gateway --entrypoint python \
        "$LITELLM_IMAGE" -c '
import sys
import urllib.request
for origin in sys.argv[1:]:
    url = origin.rstrip("/").removesuffix("/v1") + "/health"
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status != 200 or response.geturl() != url:
            raise RuntimeError("transport health check failed")
' "$GATEWAY_REASONING_URL" "$GATEWAY_EMBED_URL" >/dev/null 2>&1; then
        release_transport_network || true
        die "gateway network transport probe failed"
    fi
    exit 0
fi

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
[ -z "$GATEWAY_LLM_KEY" ] && [ "$GATEWAY_REASONING" = on ] && GATEWAY_LLM_KEY="$(gen_key)"
[ -z "$GATEWAY_EMBED_KEY" ] && GATEWAY_EMBED_KEY="$(gen_key)"
[ -z "$GATEWAY_RERANK_KEY" ] && GATEWAY_RERANK_KEY="$(gen_key)"

_url_vars="GATEWAY_EMBED_URL GATEWAY_RERANK_URL"
[ "$GATEWAY_REASONING" = off ] || _url_vars="GATEWAY_REASONING_URL $_url_vars"
for _url_var in $_url_vars; do
    eval "_url=\${$_url_var:-}"
    case "$_url" in
        http://*|https://*) ;;
        *) die "$_url_var must begin with http:// or https://, got '$_url'" ;;
    esac
done
unset _url_var _url _url_vars

# /v1/score target: the rerank /v1 origin with any trailing slash trimmed,
# plus /score. (A bare-host RERANK_URL would mistarget — the http(s) gate
# above plus this normalization keep exactly one documented shape.)
_SCORE_BASE="$GATEWAY_RERANK_URL"
case "$_SCORE_BASE" in */) _SCORE_BASE="${_SCORE_BASE%/}" ;; esac
case "$_SCORE_BASE" in */v1) ;; *) _SCORE_BASE="${_SCORE_BASE}/v1" ;; esac
SCORE_TARGET="${_SCORE_BASE}/score"
unset _SCORE_BASE
_TOKENIZE_BASE="${GATEWAY_REASONING_URL%/}"
TOKENIZE_TARGET="${_TOKENIZE_BASE%/v1}/tokenize"
unset _TOKENIZE_BASE

# Gateway-side tracing (local only): auto-detect the local Jaeger OTLP port
# when no explicit endpoint was given. Dry-run stays hermetic (no probe).
if [ -z "$GATEWAY_OTEL_ENDPOINT" ] && [ "$GATEWAY_DRYRUN" != "1" ]; then
    if curl -s -m 2 -o /dev/null "http://127.0.0.1:${GATEWAY_OTEL_OTLP_PORT}/v1/traces" 2>/dev/null; then
        GATEWAY_OTEL_ENDPOINT="http://host.docker.internal:${GATEWAY_OTEL_OTLP_PORT}"
    fi
fi

CFG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/local-gateway.XXXXXX")"
cp "$SCRIPT_DIR/gateway/strict_finish.py" "$CFG_DIR/strict_finish.py"
cp "$SCRIPT_DIR/gateway/scoped_passthrough.py" "$CFG_DIR/scoped_passthrough.py"
cp "$SCRIPT_DIR/gateway/trace_privacy.py" "$CFG_DIR/trace_privacy.py"
cat > "${CFG_DIR}/config.yaml" <<EOF
# Rendered by scripts/run_local_gateway.sh (local-dev only, never committed).
model_list:
EOF
if [ "$GATEWAY_REASONING" = on ]; then
    cat >> "${CFG_DIR}/config.yaml" <<EOF
  - model_name: ${GATEWAY_REASONING_MODEL}
    litellm_params:
      model: strict_openai/${GATEWAY_REASONING_MODEL}
      api_base: ${GATEWAY_REASONING_URL}
      api_key: dummy
      # The agent sends reasoning_effort; the openai adapter would reject it
      # (unsupported param) and litellm.drop_params would silently change
      # behavior. Forward it to vLLM instead — fidelity over convenience.
      allowed_openai_params: ["reasoning_effort"]
EOF
fi
cat >> "${CFG_DIR}/config.yaml" <<EOF
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
EOF
if [ "$GATEWAY_REASONING" = on ]; then
    cat >> "${CFG_DIR}/config.yaml" <<EOF
    - path: "/tokenize"
      target: "${TOKENIZE_TARGET}"
      methods: ["POST"]
EOF
fi
cat >> "${CFG_DIR}/config.yaml" <<EOF
    - path: "/v1/score"
      target: "${SCORE_TARGET}"
      methods: ["POST"]
litellm_settings:
  # Issue #636: no request/response content and no API-key-derived metadata
  # in any logging callback. Defence in depth only: the otel export below is
  # also rebuilt from an attribute allowlist (gateway/trace_privacy.py).
  turn_off_message_logging: true
  redact_user_api_key_info: true
  custom_provider_map:
    - provider: strict_openai
      custom_handler: strict_finish.strict_openai
EOF
if [ -n "$GATEWAY_OTEL_ENDPOINT" ]; then
    cat >> "${CFG_DIR}/config.yaml" <<EOF
  callbacks: [scoped_passthrough.guard, trace_privacy.otel]
EOF
else
    echo '  callbacks: [scoped_passthrough.guard]' >> "${CFG_DIR}/config.yaml"
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
EOF
    if [ "$GATEWAY_REASONING" = on ]; then
        cat >> "$GATEWAY_ENV_FILE" <<EOF
export LLM_BASE_URL=${BASE}/v1
export LLM_MODEL_REASONING=${GATEWAY_REASONING_MODEL}
export LLM_API_KEY=${GATEWAY_LLM_KEY}
EOF
    fi
    cat >> "$GATEWAY_ENV_FILE" <<EOF
export EMBED_BASE_URL=${BASE}/v1
export EMBED_MODEL=${GATEWAY_EMBED_MODEL}
export EMBED_API_KEY=${GATEWAY_EMBED_KEY}
# Caller-attested immutable revision, or the legacy local simulation label.
export EMBED_MODEL_REVISION=${EMBED_MODEL_REVISION_SH}
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

# Rendered gateway arguments, for the hermetic tests and for diagnosis.
printf '%s\n' "--config /app/gateway/config.yaml${GATEWAY_DEBUG_ARGS:+ $GATEWAY_DEBUG_ARGS}" "log $GATEWAY_LOG" \
    > "$CFG_DIR/gateway.args"
if [ "$GATEWAY_DRYRUN" = "1" ]; then
    write_gateway_env_file
    echo "$CFG_DIR"
    exit 0
fi
command -v "$GATEWAY_DOCKER_BIN" >/dev/null 2>&1 || die "docker is required for the local gateway"
trap 'rm -rf "$CFG_DIR"' EXIT
[ -n "$GATEWAY_RUN_OWNER" ] || GATEWAY_RUN_OWNER="$(python3 -c 'import uuid; print("gateway-" + uuid.uuid4().hex)')"

for _taken in "$GATEWAY_NAME" "$PG_NAME"; do
    if gateway_docker ps -a --format '{{.Names}}' | grep -qx "$_taken"; then
        die "container '$_taken' already exists — docker stop/remove it first (never auto-killed)"
    fi
done
unset _taken
require_fresh_container_owner
if [ -n "$GATEWAY_TRANSPORT_NETWORK_ID" ]; then
    _admitted_network_id="$(transport_network_id)" || die "transport network identity changed"
    [ "$_admitted_network_id" = "$GATEWAY_TRANSPORT_NETWORK_ID" ] || die "transport network identity changed"
    GATEWAY_NETWORK_TARGET="$GATEWAY_TRANSPORT_NETWORK_ID"
fi
# Arm cleanup before either daemon container or network can be created.
# The controller's finite group-KILL may interrupt this trap; recovery uses the
# same admitted label and exact network ID without a surviving local process.
trap 'stop_gateway || true; stop_log_capture; rm -rf "$CFG_DIR"' EXIT
trap 'exit 130' INT TERM
if [ "$GATEWAY_RESET_KEYS" = "1" ]; then
    gateway_docker volume rm "$PG_VOLUME" >/dev/null 2>&1 || true
    echo "==> Key store wiped ($PG_VOLUME removed)"
fi

prepare_transport_network
echo "==> Starting key-store postgres ($PG_NAME, volume $PG_VOLUME)"
service_docker "$PG_MEMORY_MIB" -d --rm --name "$PG_NAME" --network "$GATEWAY_NETWORK_TARGET" \
    --label "$GATEWAY_RUN_LABEL=$GATEWAY_RUN_OWNER" \
    -e POSTGRES_USER=litellm -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB=litellm \
    -v "${PG_VOLUME}:/var/lib/postgresql/data" \
    "$PG_IMAGE" >/dev/null
echo "==> Waiting for postgres (up to 60s)"
_OK=0
i=0
while [ "$i" -lt 60 ]; do
    if gateway_docker exec "$PG_NAME" pg_isready -U litellm >/dev/null 2>&1; then
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

echo "==> Starting local LiteLLM gateway ($GATEWAY_NAME on :$GATEWAY_PORT, log $GATEWAY_LOG)"
[ -n "$GATEWAY_DEBUG_ARGS" ] && echo "==> WARNING: GATEWAY_DEBUG on: the log records full request bodies (manual text); local diagnosis only"
mkdir -p "$(dirname "$GATEWAY_LOG")" || die "cannot create log directory for $GATEWAY_LOG"
[ -f "$GATEWAY_LOG" ] && mv -f "$GATEWAY_LOG" "$GATEWAY_LOG.1"
( umask 077 && : > "$GATEWAY_LOG" ) || die "cannot write gateway log $GATEWAY_LOG"
GATEWAY_OUTPUT="$GATEWAY_LOG"
if [ "$GATEWAY_LIMITS_ENABLED" = 1 ]; then
    GATEWAY_OUTPUT="$CFG_DIR/gateway-output.fifo"
    mkfifo -m 600 "$GATEWAY_OUTPUT" || die "cannot create bounded gateway log stream"
    (exec python3 -B "$SCRIPT_DIR/gateway/log_capture.py" "$GATEWAY_LOG" "$GATEWAY_LOG_MAX_BYTES" \
        < "$GATEWAY_OUTPUT") &
    LOG_CAPTURE_PID=$!
fi
# OTel env only when a local Jaeger was found: container reaches the host
# collector via the host.docker.internal mapping added above.
OTEL_RUN_ARGS=""
if [ -n "$GATEWAY_OTEL_ENDPOINT" ]; then
    echo "==> Gateway tracing on: OTLP $GATEWAY_OTEL_ENDPOINT (service $GATEWAY_OTEL_SERVICE_NAME)"
    OTEL_RUN_ARGS="-e OTEL_EXPORTER_OTLP_ENDPOINT=$GATEWAY_OTEL_ENDPOINT -e OTEL_SERVICE_NAME=$GATEWAY_OTEL_SERVICE_NAME"
fi
# shellcheck disable=SC2086
service_docker "$GATEWAY_MEMORY_MIB" --rm --name "$GATEWAY_NAME" --network "$GATEWAY_NETWORK_TARGET" \
    --label "$GATEWAY_RUN_LABEL=$GATEWAY_RUN_OWNER" \
    --add-host=host.docker.internal:host-gateway \
    -p "${GATEWAY_PORT}:4000" \
    -v "${CFG_DIR}:/app/gateway:ro" \
    -e "DATABASE_URL=postgresql://litellm:${PG_PASSWORD}@${PG_NAME}:5432/litellm" \
    ${OTEL_RUN_ARGS} \
    "$LITELLM_IMAGE" \
    --config /app/gateway/config.yaml ${GATEWAY_DEBUG_ARGS} \
    >>"$GATEWAY_OUTPUT" 2>&1 &
DOCKER_PID=$!
# The earlier trap owns both labelled daemon containers during readiness too.

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
    die "gateway did not answer within 90s — see $GATEWAY_LOG"
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
        die "key $3 unusable (models HTTP $_code — see $GATEWAY_LOG)"
    fi
}
[ "$GATEWAY_REASONING" = off ] || mint_key "$GATEWAY_LLM_KEY" "$GATEWAY_REASONING_MODEL" "local-llm"
mint_key "$GATEWAY_EMBED_KEY" "$GATEWAY_EMBED_MODEL" "local-embed"
mint_key "$GATEWAY_RERANK_KEY" "$GATEWAY_RERANK_MODEL" "local-rerank"
unset -f mint_key

# Each leg must answer with its own key (proves routing + model restriction).
_legs="embed:${GATEWAY_EMBED_KEY} rerank:${GATEWAY_RERANK_KEY}"
[ "$GATEWAY_REASONING" = off ] || _legs="llm:${GATEWAY_LLM_KEY} $_legs"
for _leg in $_legs; do
    _k="${_leg#*:}"
    if ! curl -s -m 5 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${_k}" \
        "${BASE}/v1/models" | grep -qx '200'; then
        stop_gateway
        die "self-check failed for ${_leg%%:*} leg (see $GATEWAY_LOG)"
    fi
done
unset _leg _k _legs
# And a wrong key must 401 (the enforcement the sim exists to prove).
if curl -s -m 5 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer sk-local-wrong" \
    "${BASE}/v1/models" | grep -qx '200'; then
    stop_gateway
    die "self-check failed: wrong key was accepted (see $GATEWAY_LOG)"
fi

write_gateway_env_file

cat <<EOF
==> Gateway ready: ${BASE} (container ${GATEWAY_NAME}, log $GATEWAY_LOG)
    Keys are stored in volume ${PG_VOLUME} (GATEWAY_RESET_KEYS=1 wipes it);
    never written to git. Point every leg at the gateway, then run
    scripts/probe_gateway.py to confirm and get the RERANK_ENDPOINT_ORDER
    recommendation:

EOF
if [ "$GATEWAY_REASONING" = on ]; then
    cat <<EOF
export LLM_BASE_URL=${BASE}/v1 LLM_MODEL_REASONING=${GATEWAY_REASONING_MODEL} LLM_API_KEY=${GATEWAY_LLM_KEY}
EOF
fi
cat <<EOF
export EMBED_BASE_URL=${BASE}/v1 EMBED_MODEL=${GATEWAY_EMBED_MODEL} EMBED_API_KEY=${GATEWAY_EMBED_KEY} DENSE_DIM=${DENSE_DIM:-1024} EMBED_MODEL_REVISION=${EMBED_MODEL_REVISION_SH}
export RERANK_ENABLED=true RERANK_BASE_URL=${BASE}/v1 RERANK_MODEL=${GATEWAY_RERANK_MODEL} RERANK_API_KEY=${GATEWAY_RERANK_KEY}

Ctrl-C stops the gateway AND its postgres (keys stay in the volume).
EOF
_docker_status=0
wait "$DOCKER_PID" || _docker_status=$?
if [ -n "$LOG_CAPTURE_PID" ]; then
    # Docker has closed its only FIFO writer. Drain/reap before normal cleanup;
    # keep Docker's real failure even when logging also fails.
    if ! wait "$LOG_CAPTURE_PID"; then
        [ "$_docker_status" -ne 0 ] || _docker_status=1
    fi
    LOG_CAPTURE_PID=""
fi
if ! stop_gateway; then
    [ "$_docker_status" -ne 0 ] || _docker_status=1
fi
exit "$_docker_status"
