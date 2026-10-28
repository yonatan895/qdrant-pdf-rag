#!/bin/sh
# Controlled CI/offline entry for the pinned runner. Dispatch stays in Task.
# v3.53.1 reads inherited taskrc files and .env experiments before Taskfiles.
# Refuse external config without reading it; select the config-free tools
# directory for runner initialization. Included modules still run at repo root.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PIN="$ROOT/scripts/tools/task-pin.txt"
TASK_EXE="$ROOT/.tools/bin/task"
fail() { echo "run-task: $1" >&2; echo "run-task: run the fix from the checkout root, then retry" >&2; exit 2; }
# Each refusal names its fix, run from the checkout root, for the connected
# host and the air gap where they differ. Messages are fixed text: never an
# argument, environment value or configuration content.
TASK_FIX="connected fix: sh scripts/tools/install-task.sh; air-gap fix: sh scripts/tools/install-task.sh --archive /absolute/path/to/bundle/task_linux_amd64.tar.gz (the verified bundle's copy)"

case "$(uname -s)/$(uname -m)" in
    Linux/x86_64) ;;
    *) fail "unsupported platform; the Task pin supports linux-amd64 only; fix: run from a Linux x86_64 host or WSL2 (uname -sm must print: Linux x86_64)";;
esac
[ -x "$TASK_EXE" ] || fail "pinned .tools/bin/task absent; $TASK_FIX"
expected=$(sed -n 's/^binary-sha256: *//p' "$PIN")
[ "${#expected}" = 64 ] || fail "Task binary digest is missing from the pin; fix: git checkout -- scripts/tools/task-pin.txt"
observed=$(sha256sum "$TASK_EXE")
[ "${observed%% *}" = "$expected" ] || fail "Task binary checksum mismatch; reinstall from the pinned archive: $TASK_FIX"

# Do not read, print, edit or merge private runner configuration. The pinned
# runner has no switch disabling its taskrc search. Reject instead of silently
# accepting options inherited from outside the reviewed repository. The
# refusal names the file by a shape built only from fixed tokens ($HOME,
# $XDG_CONFIG_HOME, checkout-relative or ../ segments), never the real path.
refuse_config() {
    fail "inherited Task configuration $1 is unsupported and was not read; fix: mv \"$1\" \"$1.disabled\""
}
check_config_dir() {
    for name in .taskrc.yml .taskrc.yaml; do
        [ ! -e "$1/$name" ] && [ ! -L "$1/$name" ] || refuse_config "$2$name"
    done
}
# $HOME first: it is usually also a parent of the checkout.
if [ -n "${HOME:-}" ]; then check_config_dir "$HOME" '$HOME/'; fi
if [ -n "${XDG_CONFIG_HOME:-}" ]; then
    for name in taskrc.yml taskrc.yaml; do
        [ ! -e "$XDG_CONFIG_HOME/task/$name" ] && [ ! -L "$XDG_CONFIG_HOME/task/$name" ] ||
            refuse_config '$XDG_CONFIG_HOME/task/'"$name"
    done
fi
scan="$ROOT/scripts/tools"
shown=scripts/tools/
while :; do
    check_config_dir "$scan" "$shown"
    [ "$scan" != / ] || break
    scan=$(dirname -- "$scan")
    case "$shown" in
        scripts/tools/) shown=scripts/;;
        scripts/) shown=;;
        *) shown="../$shown";;
    esac
done
[ ! -e "$ROOT/scripts/tools/.env" ] && [ ! -L "$ROOT/scripts/tools/.env" ] ||
    fail "runner initialization directory must contain no .env file; fix: mv scripts/tools/.env scripts/tools/.env.disabled"

# A finite set of runner options keeps path/global/remote/parallel overrides
# out of controlled calls. Everything after -- belongs to the selected owner.
for arg do
    case "$arg" in
        --) break;;
        --list|-l|--list-all|-a|--json|-j|--no-status|--nested|--summary|--help|-h|--version|--exit-code|-x|--dry|-n|--silent|-s|--color=false|--color=true) ;;
        -*|TASK_*=*|CLI_*=*|ROOT_DIR=*|TASKFILE_DIR=*|TASKFILE=*)
            fail "unsupported runner override; fix: drop it and pass task arguments after --, e.g. sh scripts/tools/run-task.sh qa:unit -- tests/test_agent_doctor.py -q (allowed runner options: docs/task-runner.md#scope)";;
    esac
done

# TASK_* is reserved for runner controls and internal Taskfile bridges. Reset
# both: an inherited TASK_PY would otherwise override the chosen PY bridge.
# Retain only the test lane's two explicit provisioning/requirement controls.
# Only identifier names leave this pipeline; values are never evaluated/logged.
for key in $(env | sed -n 's/^\(TASK_[A-Za-z0-9_]*\)=.*/\1/p'); do
    case "$key" in TASK_BIN|TASK_CONTRACTS_REQUIRE_RUNNER) continue;; esac
    unset "$key"
done
# TASK_EXE may have been inherited and removed above; use the fixed path.
PATH="$ROOT/.tools/bin:$PATH"
export PATH
cd "$ROOT/scripts/tools"
exec "$ROOT/.tools/bin/task" --offline --dir "$ROOT/scripts/tools" --taskfile "$ROOT/Taskfile.yml" "$@"

