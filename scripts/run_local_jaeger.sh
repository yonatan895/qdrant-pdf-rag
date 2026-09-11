#!/bin/sh
# Local Jaeger v2 trace backend for `make local-stack` (local-dev only).
# OTLP/HTTP on :4318 + UI/API on :16686, in-memory storage (ephemeral by
# design — local traces are debug data). Same image/digest the air-gap pack
# mirrors (images.txt), started with the image's default config.
#
#   make local-jaeger        # foreground; Ctrl-C stops the container
#   make local-jaeger-stop   # stop a leftover container
#
# If a Jaeger already answers on the UI port (the local-stack reuse path),
# this script reports it and exits 0 without taking ownership; a non-Jaeger
# squatter dies fail-closed. JAEGER_DRYRUN=1 prints the plan hermetically.

set -eu

JAEGER_PORT="${JAEGER_PORT:-16686}"
JAEGER_OTLP_PORT="${JAEGER_OTLP_PORT:-4318}"
JAEGER_NAME="${JAEGER_NAME:-local-jaeger}"
# Digest pinned from images.txt (upstream tag 2.20.0). Bump deliberately.
JAEGER_IMAGE="${JAEGER_IMAGE:-cr.jaegertracing.io/jaegertracing/jaeger@sha256:46a886260e04002d8f45e213fc39063fa11a50446048fdaa64786fc0840cb9f8}"
JAEGER_DRYRUN="${JAEGER_DRYRUN:-0}"
JAEGER_LOG="${JAEGER_LOG:-${TMPDIR:-/tmp}/local-jaeger.log}"

die() { echo "ERROR: $1" >&2; exit 1; }

for _pair in "JAEGER_PORT:$JAEGER_PORT" "JAEGER_OTLP_PORT:$JAEGER_OTLP_PORT"; do
    _name="${_pair%%:*}"; _val="${_pair#*:}"
    case "$_val" in
        ''|*[!0-9]*) die "$_name must be a positive integer, got '$_val'" ;;
        0) die "$_name must be greater than 0, got 0" ;;
    esac
done
unset _pair _name _val

BASE="http://127.0.0.1:${JAEGER_PORT}"
if [ "$JAEGER_DRYRUN" = "1" ]; then
    echo "[plan] local Jaeger: docker run --rm --name $JAEGER_NAME -p 127.0.0.1:$JAEGER_PORT:16686 -p 127.0.0.1:$JAEGER_OTLP_PORT:4318 $JAEGER_IMAGE"
    echo "[plan] reuse when $BASE/api/services already answers; UI $BASE"
    exit 0
fi

if curl -s -m 3 -o /dev/null "$BASE/api/services" 2>/dev/null; then
    echo "==> Jaeger already answering at $BASE (reusing; not owned by this script)"
    exit 0
fi
command -v docker >/dev/null 2>&1 || die "docker is required for local Jaeger"
if docker ps -a --format '{{.Names}}' | grep -qx "$JAEGER_NAME"; then
    die "container '$JAEGER_NAME' already exists — 'make local-jaeger-stop' first (never auto-killed)"
fi
if curl -s -m 2 -o /dev/null "http://127.0.0.1:${JAEGER_OTLP_PORT}/" 2>/dev/null; then
    die "port $JAEGER_OTLP_PORT is serving something that is not Jaeger — free it or set JAEGER_OTLP_PORT"
fi

echo "==> Starting local Jaeger ($JAEGER_NAME, UI $BASE, OTLP :$JAEGER_OTLP_PORT, log $JAEGER_LOG)"
docker run --rm --name "$JAEGER_NAME" \
    -p "127.0.0.1:${JAEGER_PORT}:16686" \
    -p "127.0.0.1:${JAEGER_OTLP_PORT}:4318" \
    "$JAEGER_IMAGE" >"$JAEGER_LOG" 2>&1 &
DOCKER_PID=$!
# shellcheck disable=SC2064
trap "docker stop '$JAEGER_NAME' >/dev/null 2>&1 || true; exit 130" INT TERM

_i=0
while [ "$_i" -lt 60 ]; do
    if curl -s -m 3 -o /dev/null "$BASE/api/services" 2>/dev/null; then
        echo "==> Jaeger ready: UI $BASE, OTLP/HTTP http://127.0.0.1:${JAEGER_OTLP_PORT}"
        wait "$DOCKER_PID"
        docker stop "$JAEGER_NAME" >/dev/null 2>&1 || true
        exit 0
    fi
    kill -0 "$DOCKER_PID" 2>/dev/null || die "Jaeger exited early — see $JAEGER_LOG"
    _i=$((_i + 1))
    sleep 1
done
docker stop "$JAEGER_NAME" >/dev/null 2>&1 || true
die "Jaeger UI did not answer within 60s — see $JAEGER_LOG"
