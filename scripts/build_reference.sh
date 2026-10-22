#!/bin/sh
# Build the Sphinx API reference (issue #889) into the given output directory.
#
#   scripts/build_reference.sh <output-dir>
#
# Docs-only: needs Sphinx from site-src/reference/requirements.docs.lock.txt
# (pip install --require-hashes --no-deps -r <that file>) on PATH or in the
# active virtualenv. Downloads nothing. Warnings fail the build.
set -eu

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "usage: $0 <output-dir>" >&2
    exit 2
fi
out=$1
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
src=$root/site-src/reference

if ! command -v sphinx-build >/dev/null 2>&1; then
    echo "sphinx-build not found; install site-src/reference/requirements.docs.lock.txt with --require-hashes" >&2
    exit 2
fi

# sphinx.ext.apidoc regenerates the stubs in api/ from src/ on every build.
rm -rf "$src/api"
sphinx-build -W --keep-going -b html "$src" "$out"
