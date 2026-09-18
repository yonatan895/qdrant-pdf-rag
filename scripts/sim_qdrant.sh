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
set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SIM_CONTAINER="${SIM_CONTAINER-qdrant-sim}"
SIM_PORT="${SIM_PORT-6333}"
PY="${PY-python3}"
if [ -z "$SIM_CONTAINER" ] || [ -z "$SIM_PORT" ]; then
  echo "sim_qdrant: SIM_CONTAINER and SIM_PORT must not be empty" >&2
  exit 2
fi

case "${1:?usage: sim_qdrant.sh up|down}" in
  up)
    if docker inspect "$SIM_CONTAINER" >/dev/null 2>&1; then
      echo "sim qdrant already running: QDRANT_SIM_URL=http://127.0.0.1:${SIM_PORT}"
    else
      docker run -d --name "$SIM_CONTAINER" --rm -p "127.0.0.1:${SIM_PORT}:6333" \
        "$("$PY" "$REPO_ROOT/scripts/qdrant_pin.py")"
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
