#!/bin/sh
# Regression check for the two supported worktree interpreter recipes:
# a per-worktree dev:setup .venv and the shared main .venv + PYTHONPATH.
# Both run a focused qa:local selection that includes the report Task tests.
# Evidence stays under XDG_STATE_HOME; no service, model or network is used.
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
MAIN_ROOT=$(git -C "$ROOT" worktree list --porcelain | sed -n 's/^worktree //p' | head -n 1)
MAIN_VENV=${MAINFRAME_MAIN_VENV:-${MAIN_ROOT:-$ROOT}/.venv}
MAIN_PYTHON="$MAIN_VENV/bin/python"
STATUS=0
RAN=0

run_leg() {
    name=$1
    shift
    echo "== $name"
    if "$@"; then
        echo "PASS $name"
    else
        echo "FAIL $name" >&2
        STATUS=1
    fi
}

if [ -x "$ROOT/.venv/bin/python" ]; then
    RAN=$((RAN + 1))
    run_leg per-worktree sh "$ROOT/scripts/tools/run-task.sh" qa:local -- tests/test_render_report.py
else
    echo "SKIP per-worktree: $ROOT/.venv/bin/python absent; run dev:setup" >&2
fi

if [ -x "$MAIN_PYTHON" ]; then
    RAN=$((RAN + 1))
    run_leg shared-main env PYTHONPATH="$ROOT/src" sh "$ROOT/scripts/tools/run-task.sh" qa:local -- \
        --python "$MAIN_PYTHON" --pass-env PYTHONPATH -- tests/test_render_report.py
else
    echo "SKIP shared-main: $MAIN_PYTHON absent; link or prepare the main .venv" >&2
fi

if [ "$RAN" = 0 ]; then
    echo "check-worktree-qa: no supported interpreter setup found" >&2
    exit 2
fi
if [ "$STATUS" = 0 ]; then
    echo "check-worktree-qa: $RAN available recipe(s) passed"
fi
exit "$STATUS"
