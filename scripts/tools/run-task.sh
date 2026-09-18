#!/bin/sh
# Controlled CI/offline entry for the pinned runner. Dispatch stays in Task.
# v3.53.1 reads inherited taskrc files and .env experiments before Taskfiles.
# Refuse external config without reading it; select the config-free tools
# directory for runner initialization. Included modules still run at repo root.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PIN="$ROOT/scripts/tools/task-pin.txt"
TASK_EXE="$ROOT/.tools/bin/task"
fail() { echo "run-task: $1" >&2; exit 2; }

case "$(uname -s)/$(uname -m)" in
    Linux/x86_64) ;;
    *) fail "unsupported platform; the Task pin supports linux-amd64 only";;
esac
[ -x "$TASK_EXE" ] || fail "pinned .tools/bin/task absent; run install-task.sh on a connected host or bootstrap the signed bundle offline"
expected=$(sed -n 's/^binary-sha256: *//p' "$PIN")
[ "${#expected}" = 64 ] || fail "Task binary digest is missing from the pin"
observed=$(sha256sum "$TASK_EXE")
[ "${observed%% *}" = "$expected" ] || fail "Task binary checksum mismatch; reinstall from the pinned archive"

# Do not read, print, edit or merge private runner configuration. The pinned
# runner has no switch disabling its taskrc search. Reject instead of silently
# accepting options inherited from outside the reviewed repository.
check_config_dir() {
    for name in .taskrc.yml .taskrc.yaml; do
        [ ! -e "$1/$name" ] && [ ! -L "$1/$name" ] ||
            fail "inherited Task configuration is unsupported; use a clean runner environment or the direct owner scripts"
    done
}
scan="$ROOT/scripts/tools"
while :; do
    check_config_dir "$scan"
    [ "$scan" != / ] || break
    scan=$(dirname -- "$scan")
done
if [ -n "${HOME:-}" ]; then check_config_dir "$HOME"; fi
if [ -n "${XDG_CONFIG_HOME:-}" ]; then
    for name in taskrc.yml taskrc.yaml; do
        [ ! -e "$XDG_CONFIG_HOME/task/$name" ] && [ ! -L "$XDG_CONFIG_HOME/task/$name" ] ||
            fail "inherited Task configuration is unsupported; use a clean runner environment or the direct owner scripts"
    done
fi
[ ! -e "$ROOT/scripts/tools/.env" ] && [ ! -L "$ROOT/scripts/tools/.env" ] ||
    fail "runner initialization directory must contain no .env file"

# A finite set of runner options keeps path/global/remote/parallel overrides
# out of controlled calls. Everything after -- belongs to the selected owner.
for arg do
    case "$arg" in
        --) break;;
        --list|-l|--list-all|-a|--json|-j|--no-status|--nested|--summary|--help|-h|--version|--exit-code|-x|--dry|-n|--silent|-s|--color=false|--color=true) ;;
        -*|TASK_*=*|CLI_*=*|ROOT_DIR=*|TASKFILE_DIR=*|TASKFILE=*)
            fail "unsupported runner override; see docs/task-runner.md";;
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

