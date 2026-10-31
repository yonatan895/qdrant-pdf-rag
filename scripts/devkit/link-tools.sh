#!/bin/sh
# Give a checkout inside the dev workspace image (ADR-0009 §8, GH#1118 F5) the
# pinned tools that scripts/tools/run-task.sh and scripts/agent_doctor.py read
# from <checkout>/.tools/bin. The image holds Task and Helm in /usr/local/bin,
# each verified against its pin at build time; this links them after verifying
# each binary's sha256 against the binary-sha256 of task-pin.txt and
# helm-pin.txt again, so a checkout whose pins differ from the image's is
# refused. Nothing is downloaded or built.
#
# --task-archive FILE also caches a transferred Task release archive, verified
# against the sha256 of task-pin.txt, in .tools/cache/: the image holds the Task
# binary only, and the artifact tests and the doctor read that archive.
#
# Idempotent: a rerun verifies again and changes nothing. Messages are fixed
# text; they never echo an argument.
#
# usage: sh scripts/devkit/link-tools.sh [--task-archive FILE] [--from DIR]
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
FROM=/usr/local/bin
TASK_ARCHIVE=""
die() { echo "link-tools: $1" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case "$1" in
        --task-archive) TASK_ARCHIVE="${2:-}"; [ -n "$TASK_ARCHIVE" ] || die "--task-archive requires a file"; shift 2;;
        --from) FROM="${2:-}"; shift 2;;
        -h|--help) echo "usage: sh scripts/devkit/link-tools.sh [--task-archive FILE] [--from DIR]"; exit 0;;
        *) die "unsupported argument (see --help)";;
    esac
done
case "$FROM" in /*) ;; *) die "--from must be an absolute directory";; esac
[ "$(uname -s)/$(uname -m)" = Linux/x86_64 ] || die "unsupported platform; the tool pins cover linux-amd64 only"

. "$ROOT/scripts/tools/task-artifact.sh"
task_read_pin "$ROOT/scripts/tools/task-pin.txt"
HELM_BINARY_SHA256=$(sed -n 's/^binary-sha256: *//p' "$ROOT/scripts/tools/helm-pin.txt")
HELM_VERSION=$(sed -n 's/^version: *//p' "$ROOT/scripts/tools/helm-pin.txt")
printf '%s\n' "$HELM_BINARY_SHA256" | grep -Eqx '[0-9a-f]{64}' ||
    die "scripts/tools/helm-pin.txt has no valid binary-sha256; fix: git checkout -- scripts/tools/helm-pin.txt"
[ -n "$HELM_VERSION" ] || die "scripts/tools/helm-pin.txt has no version; fix: git checkout -- scripts/tools/helm-pin.txt"

KIT_FIX="run this in the dev kit image that scripts/tools/devkit-pin.txt names for this checkout"
digest() { sha256sum "$1" | awk '{print $1}'; }

# Check everything before writing anything, so every refusal leaves .tools/ as it was:
# each image binary's digest and version, the archive, and the checkout's .tools/.
verify() {  # name expected
    [ -f "$FROM/$1" ] && [ ! -L "$FROM/$1" ] && [ -x "$FROM/$1" ] ||
        die "the image has no executable $1; $KIT_FIX"
    [ "$(digest "$FROM/$1")" = "$2" ] ||
        die "the image's $1 does not match the binary-sha256 in scripts/tools/$1-pin.txt; $KIT_FIX"
}
verify task "$TASK_BINARY_SHA256"
verify helm "$HELM_BINARY_SHA256"
[ "$("$FROM/task" --version)" = "${TASK_VERSION#v}" ] || die "the image's task is not the pinned version; $KIT_FIX"
case "$("$FROM/helm" version --short)" in
    "$HELM_VERSION"|"$HELM_VERSION"+*) ;;
    *) die "the image's helm is not the pinned version; $KIT_FIX";;
esac
if [ -n "$TASK_ARCHIVE" ]; then
    [ -f "$TASK_ARCHIVE" ] || die "the --task-archive file is missing"
    [ "$(digest "$TASK_ARCHIVE")" = "$TASK_SHA256" ] ||
        die "the --task-archive file does not match the sha256 in scripts/tools/task-pin.txt"
fi

# Write only into this checkout's own .tools/: through a linked .tools, .tools/bin or
# .tools/cache this would replace pinned tools that another checkout shares.
for dir in .tools .tools/bin .tools/cache; do
    [ ! -L "$ROOT/$dir" ] || die "$dir is a symlink; link-tools writes only a checkout-owned .tools/ (remove the link and rerun)"
done
BIN="$ROOT/.tools/bin"
for name in task helm; do
    [ ! -d "$BIN/$name" ] || [ -L "$BIN/$name" ] || die ".tools/bin/$name is a directory; remove it and rerun"
done
mkdir -p "$BIN"
link() {  # name
    target="$FROM/$1"
    if [ -L "$BIN/$1" ] && [ "$(readlink "$BIN/$1")" = "$target" ]; then
        echo "link-tools: .tools/bin/$1 already links the verified image $1"
        return
    fi
    # Replace atomically (rename, never follow an existing link) so a concurrent
    # run-task.sh never sees a missing or partial tool.
    tmp="$BIN/.$1.link.$$"
    rm -f "$tmp"
    ln -s "$target" "$tmp"
    mv -fT "$tmp" "$BIN/$1"
    echo "link-tools: linked .tools/bin/$1 to the verified image $1"
}
link task
link helm

if [ -n "$TASK_ARCHIVE" ]; then
    CACHE="$ROOT/.tools/cache"
    mkdir -p "$CACHE"
    if [ -f "$CACHE/$TASK_ASSET" ] && [ ! -L "$CACHE/$TASK_ASSET" ] && cmp -s "$TASK_ARCHIVE" "$CACHE/$TASK_ASSET"; then
        echo "link-tools: .tools/cache/$TASK_ASSET already holds the verified archive"
    else
        tmp="$CACHE/.$TASK_ASSET.$$"
        rm -f "$tmp"
        cp "$TASK_ARCHIVE" "$tmp"
        [ "$(digest "$tmp")" = "$TASK_SHA256" ] || { rm -f "$tmp"; die "the --task-archive file changed while it was copied"; }
        mv -fT "$tmp" "$CACHE/$TASK_ASSET"
        echo "link-tools: cached the verified $TASK_ASSET in .tools/cache"
    fi
fi
