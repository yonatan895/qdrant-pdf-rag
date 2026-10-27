#!/bin/sh
# Explicit preparation only. --archive never downloads; verification never calls this.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PIN="$ROOT/scripts/tools/oras-pin.txt"
ARCHIVE=""
BIN_DIR="$ROOT/.tools/bin"
fail() { echo "install-oras: $1" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case "$1" in
        --archive) ARCHIVE="${2:?--archive requires a file}"; [ -n "$ARCHIVE" ] || exit 2; shift 2;;
        --bin-dir) BIN_DIR="${2:?--bin-dir requires a directory}"; shift 2;;
        --help|-h) echo 'usage: sh scripts/tools/install-oras.sh [--archive FILE] [--bin-dir DIR]'; exit 0;;
        *) fail 'unsupported argument';;
    esac
done
case "$(uname -s)/$(uname -m)" in Linux/x86_64) ;; *) fail 'unsupported platform; linux-amd64 only';; esac
archive_sha=$(sed -n 's/^sha256: *//p' "$PIN")
binary_sha=$(sed -n 's/^binary-sha256: *//p' "$PIN")
origin=$(sed -n 's/^origin: *//p' "$PIN")
version=$(sed -n 's/^version: *//p' "$PIN")
[ "${#archive_sha}" = 64 ] && [ "${#binary_sha}" = 64 ] || fail 'invalid artifact pin'
TMP=$(mktemp -d)
INSTALL_TMP=""
trap 'rm -rf "$TMP"; [ -z "$INSTALL_TMP" ] || rm -f "$INSTALL_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -z "$ARCHIVE" ]; then
    ARCHIVE="$TMP/oras.tgz"
    curl -fsSL --retry 3 -o "$ARCHIVE" "$origin"
fi
observed=$(sha256sum "$ARCHIVE")
[ "${observed%% *}" = "$archive_sha" ] || fail 'archive checksum mismatch'
tar xzf "$ARCHIVE" -C "$TMP" oras
observed=$(sha256sum "$TMP/oras")
[ "${observed%% *}" = "$binary_sha" ] || fail 'binary checksum mismatch'
actual=$("$TMP/oras" version | sed -n 's/^Version: *//p')
[ "v$actual" = "$version" ] || fail 'version mismatch'
mkdir -p "$BIN_DIR"
INSTALL_TMP=$(mktemp "$BIN_DIR/.oras.XXXXXX")
cp "$TMP/oras" "$INSTALL_TMP"
chmod 0755 "$INSTALL_TMP"
mv -f "$INSTALL_TMP" "$BIN_DIR/oras"
INSTALL_TMP=""
echo "install-oras: installed pinned ORAS $version (linux-amd64)"
