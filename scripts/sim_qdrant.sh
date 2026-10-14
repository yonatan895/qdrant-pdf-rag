#!/bin/sh
# Pinned sim-Qdrant lifecycle owner (issue #402 B3): the fixed name/port/pin
# contract shared by `task local:qdrant:up|down` and scripts/run_local_stack.sh.
# Directly callable and runner-free (helpers never invoke a task runner, so no
# script -> Task -> script cycle is possible):
#   SIM_CONTAINER=qdrant-sim SIM_PORT=6333 sh scripts/sim_qdrant.sh up|down
# SIM_CONTAINER/SIM_PORT follow Make `?=` parity: unset selects the defaults;
# an explicit empty value fails closed here with a clear message (Make leaves
# it to a cryptic docker error; recorded in docs/task-runner.md). PY selects
# the interpreter for scripts/qdrant_pin.py (default python3, stdlib-only).
# SIM_QDRANT_VOLUME (opt-in, env-only; #742) names a Docker volume mounted at
# /qdrant/storage so data survives `down`/`up`; unset keeps the ephemeral default.
set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SIM_CONTAINER="${SIM_CONTAINER-qdrant-sim}"
SIM_PORT="${SIM_PORT-6333}"
PY="${PY-python3}"
if [ -z "$SIM_CONTAINER" ] || [ -z "$SIM_PORT" ]; then
  echo "sim_qdrant: SIM_CONTAINER and SIM_PORT must not be empty" >&2
  exit 2
fi
SIM_QDRANT_VOLUME="${SIM_QDRANT_VOLUME:-}"
case "$SIM_QDRANT_VOLUME" in
  '') ;;
  [!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*)
    echo "sim_qdrant: SIM_QDRANT_VOLUME must be a plain Docker volume name" >&2
    exit 2
    ;;
esac

case "${1:?usage: sim_qdrant.sh up|down}" in
  up)
    IMAGE_ID=$("$PY" "$REPO_ROOT/scripts/qdrant_pin.py" --prepared)
    if docker inspect "$SIM_CONTAINER" >/dev/null 2>&1; then
      OBSERVED=$(docker inspect --format '{{.Image}} {{.State.Running}}' "$SIM_CONTAINER")
      [ "$OBSERVED" = "$IMAGE_ID true" ] || {
        echo "sim_qdrant: existing container is stopped or uses unapproved image bytes" >&2
        exit 1
      }
      if [ -n "$SIM_QDRANT_VOLUME" ]; then
        MOUNTED=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Name}}{{end}}{{end}}' "$SIM_CONTAINER")
        [ "$MOUNTED" = "$SIM_QDRANT_VOLUME" ] || {
          echo "sim_qdrant: running container does not persist to SIM_QDRANT_VOLUME; stop it first" >&2
          exit 1
        }
      fi
      echo "sim qdrant already running: QDRANT_SIM_URL=http://127.0.0.1:${SIM_PORT}"
    elif [ -n "$SIM_QDRANT_VOLUME" ]; then
      docker run --pull=never -d --name "$SIM_CONTAINER" --rm -p "127.0.0.1:${SIM_PORT}:6333" \
        -v "${SIM_QDRANT_VOLUME}:/qdrant/storage" "$IMAGE_ID"
      echo "Qdrant sim up (persistent volume ${SIM_QDRANT_VOLUME}): QDRANT_SIM_URL=http://127.0.0.1:${SIM_PORT}"
    else
      docker run --pull=never -d --name "$SIM_CONTAINER" --rm -p "127.0.0.1:${SIM_PORT}:6333" \
        "$IMAGE_ID"
      echo "Qdrant sim up: QDRANT_SIM_URL=http://127.0.0.1:${SIM_PORT} task qa:sim"
    fi
    ;;
  down)
    docker stop "$SIM_CONTAINER" 2>/dev/null || true
    ;;
  *)
    echo "sim_qdrant: usage: sim_qdrant.sh up|down" >&2
    exit 2
    ;;
esac
