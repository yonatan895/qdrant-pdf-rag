#!/bin/sh
# Local OpenObserve trace + metrics backend for `sh scripts/tools/run-task.sh local:stack`
# (local-dev only; ADR-0006, issue #956). Replaces the retired Jaeger script.
#
# One loopback port serves the UI, the search API and OTLP/HTTP (:5080). Data
# lives in a named docker volume, so local traces survive a restart (unlike the
# retired in-memory Jaeger). Same image digest the air-gap pack mirrors
# (images.txt); telemetry, GeoIP download and model pricing are off.
#
# Credentials: the root user comes from a mode-0600 env file (created with a
# random password on first start; never on a command line, never printed).
# Anything that must authenticate (the agent's OTLP export, the trace check)
# reads the same file: ZO_ROOT_USER_EMAIL, ZO_ROOT_USER_PASSWORD, O2_BASIC_AUTH
# (base64 of email:password).
#
# With O2_COLLECTOR=1 a pinned otelcol-contrib container scrapes the local
# agent's /metrics (127.0.0.1:O2_AGENT_PORT) and forwards OTLP metrics to
# OpenObserve. Host networking is what lets it reach the loopback-bound agent.
#
#   sh scripts/tools/run-task.sh local:openobserve:up     # foreground; Ctrl-C stops the containers
#   sh scripts/tools/run-task.sh local:openobserve:down   # stop leftover containers
#
# If an OpenObserve already answers on the port (the local-stack reuse path),
# this script reports it and exits 0 without taking ownership; a different
# service on the port dies fail-closed. O2_DRYRUN=1 prints the plan hermetically.

set -eu

O2_PORT="${O2_PORT:-5080}"
O2_AGENT_PORT="${O2_AGENT_PORT:-8080}"
O2_NAME="${O2_NAME:-local-openobserve}"
O2_COLLECTOR_NAME="${O2_COLLECTOR_NAME:-local-otelcol}"
O2_VOLUME="${O2_VOLUME:-local-openobserve-data}"
O2_NETWORK="${O2_NETWORK:-local-openobserve-net}"
# Digests pinned from images.txt (OpenObserve v1.1.2, otelcol-contrib 0.160.0). Bump deliberately.
O2_IMAGE="${O2_IMAGE:-docker.io/openobserve/openobserve@sha256:4a154849432f75c4fb024de32fe34163c02406c905dab22bf8d213d2e3386392}"
O2_COLLECTOR_IMAGE="${O2_COLLECTOR_IMAGE:-docker.io/otel/opentelemetry-collector-contrib@sha256:799dc6cf12c96192af37b5bdba804da8c10b3bc563b43cb90c3f3c58d9572ad6}"
O2_MEMORY="${O2_MEMORY:-1g}"
O2_COLLECTOR="${O2_COLLECTOR:-0}"
O2_ENV_FILE="${O2_ENV_FILE:-${HOME:-/nonexistent}/.config/mainframe-rag/local-openobserve.env}"
O2_DRYRUN="${O2_DRYRUN:-0}"
O2_LOG="${O2_LOG:-${TMPDIR:-/tmp}/local-openobserve.log}"

die() { echo "ERROR: $1" >&2; exit 1; }

for _pair in "O2_PORT:$O2_PORT" "O2_AGENT_PORT:$O2_AGENT_PORT"; do
    _name="${_pair%%:*}"; _val="${_pair#*:}"
    case "$_val" in
        ''|*[!0-9]*) die "$_name must be a positive integer, got '$_val'" ;;
        0) die "$_name must be greater than 0, got 0" ;;
    esac
done
unset _pair _name _val
case "$O2_MEMORY" in
    [1-9]*[mg]) case "${O2_MEMORY%[mg]}" in *[!0-9]*) die "O2_MEMORY must look like 512m or 1g, got '$O2_MEMORY'" ;; esac ;;
    *) die "O2_MEMORY must look like 512m or 1g, got '$O2_MEMORY'" ;;
esac
case "$O2_COLLECTOR" in 0|1) ;; *) die "O2_COLLECTOR must be 0 or 1, got '$O2_COLLECTOR'" ;; esac
for _pair in "O2_NAME:$O2_NAME" "O2_COLLECTOR_NAME:$O2_COLLECTOR_NAME" "O2_VOLUME:$O2_VOLUME" "O2_NETWORK:$O2_NETWORK"; do
    _name="${_pair%%:*}"; _val="${_pair#*:}"
    case "$_val" in
        ''|-*|*[!A-Za-z0-9_.-]*) die "$_name must be a plain docker name, got '$_val'" ;;
    esac
done
unset _pair _name _val

BASE="http://127.0.0.1:${O2_PORT}"
# The unauthenticated liveness probe; OpenObserve answers {"status":"ok"}.
o2_healthy() { curl -s -m 3 "$BASE/healthz" 2>/dev/null | grep -q '"status" *: *"ok"'; }

# Hardening shared by both containers: no capabilities, no privilege gain,
# read-only root filesystem with a small tmpfs, bounded pids, no swap.
HARDEN="--read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,size=64m --pids-limit 512"

if [ "$O2_DRYRUN" = "1" ]; then
    echo "[plan] local OpenObserve: docker run --rm --name $O2_NAME --memory $O2_MEMORY --memory-swap $O2_MEMORY $HARDEN --env-file <$O2_ENV_FILE, mode 0600> -e HOME=/tmp -e ZO_DATA_DIR=/data -e ZO_LOCAL_MODE=true -e ZO_HTTP_ADDR=0.0.0.0 -e ZO_TELEMETRY=false -e ZO_MODEL_PRICING_ENABLED=false -e ZO_MMDB_DISABLE_DOWNLOAD=true -v $O2_VOLUME:/data --network $O2_NETWORK -p 127.0.0.1:$O2_PORT:5080 $O2_IMAGE"
    if [ "$O2_COLLECTOR" = "1" ]; then
        echo "[plan] local collector: docker run -d --rm --name $O2_COLLECTOR_NAME --network host --memory 256m $HARDEN scrape 127.0.0.1:$O2_AGENT_PORT/metrics -> $BASE/api/default $O2_COLLECTOR_IMAGE"
    fi
    echo "[plan] reuse when $BASE/healthz already answers; UI $BASE/web/"
    exit 0
fi

if o2_healthy; then
    echo "==> OpenObserve already answering at $BASE (reusing; not owned by this script — its bounds are whatever it was started with)"
    exit 0
fi
command -v docker >/dev/null 2>&1 || die "docker is required for local OpenObserve"
if docker ps -a --format '{{.Names}}' | grep -qx "$O2_NAME"; then
    die "container '$O2_NAME' already exists — 'sh scripts/tools/run-task.sh local:openobserve:down' first (never auto-killed)"
fi
if [ "$O2_COLLECTOR" = "1" ] && docker ps -a --format '{{.Names}}' | grep -qx "$O2_COLLECTOR_NAME"; then
    die "container '$O2_COLLECTOR_NAME' already exists — 'sh scripts/tools/run-task.sh local:openobserve:down' first (never auto-killed)"
fi
if curl -s -m 2 -o /dev/null "$BASE/" 2>/dev/null; then
    die "port $O2_PORT is serving something that is not OpenObserve — free it or set O2_PORT"
fi

# Credentials file: create once with a random password, then only verify.
if [ ! -e "$O2_ENV_FILE" ]; then
    mkdir -p "$(dirname "$O2_ENV_FILE")"
    ( umask 077
      _pw="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')Aa1!"  # v1.1.2 needs upper+lower+digit+special
      _email="local@openobserve.invalid"
      _b64="$(printf '%s:%s' "$_email" "$_pw" | base64 | tr -d '\n')"
      printf 'ZO_ROOT_USER_EMAIL=%s\nZO_ROOT_USER_PASSWORD=%s\nO2_BASIC_AUTH=%s\n' "$_email" "$_pw" "$_b64" > "$O2_ENV_FILE" )
    echo "==> Created credentials file $O2_ENV_FILE (mode 0600)"
fi
[ -f "$O2_ENV_FILE" ] && [ ! -L "$O2_ENV_FILE" ] || die "$O2_ENV_FILE must be a regular file, not a symlink"
[ "$(stat -c %a "$O2_ENV_FILE")" = "600" ] || die "$O2_ENV_FILE must have mode 0600"
for _key in ZO_ROOT_USER_EMAIL ZO_ROOT_USER_PASSWORD O2_BASIC_AUTH; do
    grep -q "^${_key}=." "$O2_ENV_FILE" || die "$O2_ENV_FILE lacks $_key"
done
unset _key

# A user-defined network lets the local gateway container reach OpenObserve by name
# while the published port stays loopback-only.
docker network inspect "$O2_NETWORK" >/dev/null 2>&1 || docker network create "$O2_NETWORK" >/dev/null
echo "==> Starting local OpenObserve ($O2_NAME, UI $BASE/web/, OTLP $BASE/api/default, memory $O2_MEMORY, volume $O2_VOLUME, log $O2_LOG)"
# shellcheck disable=SC2086
docker run --rm --name "$O2_NAME" \
    --memory "$O2_MEMORY" --memory-swap "$O2_MEMORY" $HARDEN \
    --env-file "$O2_ENV_FILE" \
    -e HOME=/tmp -e ZO_DATA_DIR=/data -e ZO_LOCAL_MODE=true -e ZO_HTTP_ADDR=0.0.0.0 \
    -e ZO_TELEMETRY=false -e ZO_MODEL_PRICING_ENABLED=false -e ZO_MMDB_DISABLE_DOWNLOAD=true \
    -v "$O2_VOLUME:/data" --network "$O2_NETWORK" \
    -p "127.0.0.1:${O2_PORT}:5080" \
    "$O2_IMAGE" >"$O2_LOG" 2>&1 &
DOCKER_PID=$!
stop_all() {
    docker stop "$O2_COLLECTOR_NAME" >/dev/null 2>&1 || true
    docker stop "$O2_NAME" >/dev/null 2>&1 || true
    if [ -n "${_cdir:-}" ]; then rm -rf "$_cdir"; fi
}
# shellcheck disable=SC2064
trap "stop_all; exit 130" INT TERM

_i=0
while [ "$_i" -lt 90 ]; do
    if o2_healthy; then
        break
    fi
    kill -0 "$DOCKER_PID" 2>/dev/null || die "OpenObserve exited early — see $O2_LOG"
    _i=$((_i + 1))
    sleep 1
done
if ! o2_healthy; then
    stop_all
    die "OpenObserve did not answer within 90s — see $O2_LOG"
fi

if [ "$O2_COLLECTOR" = "1" ]; then
    _cdir="$(mktemp -d "${TMPDIR:-/tmp}/local-otelcol.XXXXXX")"
    _cenv="$(umask 077; mktemp "${TMPDIR:-/tmp}/local-otelcol-env.XXXXXX")"
    grep '^O2_BASIC_AUTH=' "$O2_ENV_FILE" > "$_cenv"
    cat > "$_cdir/config.yaml" <<CFG
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: rag-agent
          scrape_interval: 15s
          scrape_protocols: [PrometheusText0.0.4]
          static_configs:
            - targets: ['127.0.0.1:${O2_AGENT_PORT}']
exporters:
  otlphttp:
    endpoint: ${BASE}/api/default
    headers:
      Authorization: "Basic \${env:O2_BASIC_AUTH}"
service:
  telemetry:
    metrics:
      level: none
  pipelines:
    metrics:
      receivers: [prometheus]
      exporters: [otlphttp]
CFG
    chmod 755 "$_cdir"; chmod 644 "$_cdir/config.yaml"
    # shellcheck disable=SC2086
    docker run -d --rm --name "$O2_COLLECTOR_NAME" --network host \
        --memory 256m --memory-swap 256m $HARDEN \
        --env-file "$_cenv" \
        -v "$_cdir/config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
        "$O2_COLLECTOR_IMAGE" >/dev/null || { rm -f "$_cenv"; stop_all; die "collector failed to start"; }
    rm -f "$_cenv"
    echo "==> Collector ready: scraping 127.0.0.1:${O2_AGENT_PORT}/metrics -> OpenObserve"
fi

echo "==> OpenObserve ready: UI $BASE/web/, OTLP/HTTP $BASE/api/default (credentials: $O2_ENV_FILE)"
wait "$DOCKER_PID" || true
stop_all
