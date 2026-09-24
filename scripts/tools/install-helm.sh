#!/bin/sh
# Explicit preparation only. --archive never downloads; verification never calls this.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PIN="$ROOT/scripts/tools/helm-pin.txt"
ARCHIVE=""
BIN_DIR="$ROOT/.tools/bin"
fail() { echo "install-helm: $1" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case "$1" in
        --archive) ARCHIVE="${2:?--archive requires a file}"; [ -n "$ARCHIVE" ] || exit 2; shift 2;;
        --bin-dir) BIN_DIR="${2:?--bin-dir requires a directory}"; shift 2;;
        --help|-h) echo 'usage: sh scripts/tools/install-helm.sh [--archive FILE] [--bin-dir DIR]'; exit 0;;
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
    ARCHIVE="$TMP/helm.tgz"
    curl -fsSL -o "$ARCHIVE" "$origin"
fi
observed=$(sha256sum "$ARCHIVE")
[ "${observed%% *}" = "$archive_sha" ] || fail 'archive checksum mismatch'
tar xzf "$ARCHIVE" -C "$TMP" linux-amd64/helm
observed=$(sha256sum "$TMP/linux-amd64/helm")
[ "${observed%% *}" = "$binary_sha" ] || fail 'binary checksum mismatch'
actual=$("$TMP/linux-amd64/helm" version --short)
case "$actual" in "$version"|"$version"+*) ;; *) fail 'version mismatch';; esac
mkdir -p "$BIN_DIR"
INSTALL_TMP=$(mktemp "$BIN_DIR/.helm.XXXXXX")
cp "$TMP/linux-amd64/helm" "$INSTALL_TMP"
chmod 0755 "$INSTALL_TMP"
mv -f "$INSTALL_TMP" "$BIN_DIR/helm"
INSTALL_TMP=""
echo "install-helm: installed pinned Helm $version (linux-amd64)"
