#!/bin/sh
# Install the repository-pinned Task binary (connected host only).
#
# Explicit opt-in installer: default discovery (`task --list`), doctor and
# verification never auto-install. Invoke only when the caller chooses
# installation:
#   sh scripts/tools/install-task.sh [--bin-dir DIR]
#
# Reads scripts/tools/task-pin.txt (single source: version, asset, sha256,
# origin). Verifies OS/arch (linux-amd64 only, fail closed), checks the
# downloaded asset against the pinned SHA-256 before execution, extracts to a
# workspace-local directory (default .tools/bin, no sudo, no global PATH or
# profile mutation), and verifies `task --version` matches the pin while
# distinguishing go-task from any other program named `task`.
#
# Offline/air-gap delivery is a later increment and must NOT call this script
# (no network in the gap); it consumes the signed bundled artifact instead.
set -eu

BIN_DIR=".tools/bin"
while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir)
      BIN_DIR="${2:?--bin-dir requires a directory argument}"; shift 2;;
    --bin-dir=*)
      BIN_DIR="${1#--bin-dir=}"; shift;;
    -h|--help)
      echo "usage: sh scripts/tools/install-task.sh [--bin-dir DIR]"; exit 0;;
    *)
      echo "install-task: unsupported argument: $1 (see --help)" >&2; exit 2;;
  esac
done

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
PIN="$ROOT/scripts/tools/task-pin.txt"
VERSION="$(sed -n 's/^version: *//p' "$PIN" | head -n 1)"
ASSET="$(sed -n 's/^asset: *//p' "$PIN" | head -n 1)"
SHA256="$(sed -n 's/^sha256: *//p' "$PIN" | head -n 1)"
ORIGIN="$(sed -n 's/^origin: *//p' "$PIN" | head -n 1)"
if [ -z "$VERSION" ] || [ -z "$ASSET" ] || [ -z "$SHA256" ] || [ -z "$ORIGIN" ]; then
  echo "install-task: incomplete pin record in scripts/tools/task-pin.txt" >&2
  exit 2
fi
EXPECTED="${VERSION#v}"

OS="$(uname -s)"
ARCH="$(uname -m)"
if [ "$OS" != "Linux" ] || [ "$ARCH" != "x86_64" ]; then
  echo "install-task: unsupported platform ${OS}/${ARCH}; pin covers linux-amd64 only" >&2
  exit 2
fi
command -v curl >/dev/null 2>&1 || { echo "install-task: curl is required (connected host)" >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo "install-task: sha256sum is required" >&2; exit 2; }
command -v tar >/dev/null 2>&1 || { echo "install-task: tar is required" >&2; exit 2; }

case "$BIN_DIR" in
  /*) DEST="$BIN_DIR";;
  *) DEST="$ROOT/$BIN_DIR";;
esac
mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

echo "install-task: downloading $ORIGIN"
curl -fsSL -o "$TMP/$ASSET" "$ORIGIN"
printf '%s  %s\n' "$SHA256" "$TMP/$ASSET" | (cd / && sha256sum -c -)
tar xzf "$TMP/$ASSET" -C "$TMP"
if [ ! -x "$TMP/task" ]; then
  echo "install-task: extracted archive has no executable task binary" >&2
  exit 1
fi
GOT="$("$TMP/task" --version 2>/dev/null | tr -d '[:space:]')"
if [ "$GOT" != "$EXPECTED" ]; then
  echo "install-task: version mismatch: got '${GOT:-unreadable}', want '$EXPECTED' (not go-task?)" >&2
  exit 1
fi
install -m 0755 "$TMP/task" "$DEST/task"
FINAL="$("$DEST/task" --version 2>/dev/null | tr -d '[:space:]')"
if [ "$FINAL" != "$EXPECTED" ]; then
  echo "install-task: installed binary failed verification ($FINAL)" >&2
  exit 1
fi
echo "install-task: installed Task $VERSION (linux-amd64) to $DEST/task"
echo "next: export PATH=\"$DEST:\$PATH\" (session-local only; no global mutation)"
