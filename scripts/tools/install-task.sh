#!/bin/sh
# Explicit pinned installation. --archive is fully offline; omitted, download
# from the pinned official origin. No sudo, Make, Task, Go or Python needed.
set -eu
BIN_DIR=".tools/bin"
ARCHIVE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir) BIN_DIR="${2:?--bin-dir requires a directory argument}"; shift 2;;
    --bin-dir=*) BIN_DIR="${1#--bin-dir=}"; shift;;
    --archive) ARCHIVE="${2:?--archive requires a file argument}"; shift 2;;
    --archive=*) ARCHIVE="${1#--archive=}"; [ -n "$ARCHIVE" ] || exit 2; shift;;
    -h|--help) echo "usage: sh scripts/tools/install-task.sh [--archive FILE] [--bin-dir DIR]"; exit 0;;
    *) echo "install-task: unsupported argument: $1 (see --help)" >&2; exit 2;;
  esac
done
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
. "$ROOT/scripts/tools/task-artifact.sh"
task_read_pin "$ROOT/scripts/tools/task-pin.txt"
task_check_platform
case "$BIN_DIR" in /*) DEST="$BIN_DIR";; *) DEST="$ROOT/$BIN_DIR";; esac
TMP="$(mktemp -d)"
INSTALL_TMP=""
trap 'rm -rf "$TMP"; [ -z "$INSTALL_TMP" ] || rm -f "$INSTALL_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -z "$ARCHIVE" ]; then
    command -v curl >/dev/null 2>&1 || task_fail "curl is required for connected installation; use --archive for offline installation"
    ARCHIVE="$TMP/$TASK_ASSET"
    echo "install-task: downloading $TASK_ORIGIN"
    curl -fsSL -o "$ARCHIVE" "$TASK_ORIGIN"
fi
task_extract_verified "$ARCHIVE" "$TMP"
GOT="$("$TMP/task" --version)"
[ "$GOT" = "${TASK_VERSION#v}" ] || task_fail "version mismatch (not the pinned go-task executable)"
mkdir -p "$DEST"
# Replace atomically so another launcher never sees a partially copied binary.
INSTALL_TMP=$(mktemp "$DEST/.task.XXXXXX")
cp "$TMP/task" "$INSTALL_TMP"
chmod 0755 "$INSTALL_TMP"
printf '%s  %s\n' "$TASK_BINARY_SHA256" "$INSTALL_TMP" | sha256sum -c - >/dev/null || task_fail "installed binary checksum mismatch"
mv -f "$INSTALL_TMP" "$DEST/task"
INSTALL_TMP=""
# Retain the verified archive for explicit connected pack/offline tests.
mkdir -p "$ROOT/.tools/cache"
if [ ! "$ARCHIVE" -ef "$ROOT/.tools/cache/$TASK_ASSET" ]; then
    cp "$ARCHIVE" "$ROOT/.tools/cache/$TASK_ASSET"
fi
echo "install-task: installed Task $TASK_VERSION (linux-amd64) to $DEST/task"
echo "next: sh scripts/tools/run-task.sh --list"
