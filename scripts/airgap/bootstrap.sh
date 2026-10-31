#!/bin/sh
# SNEAKERNET BOOTSTRAP (issue #15): single-step unpack verification,
# git repository extraction from repo.bundle, and workspace setup.
#
# Run from the directory where the sneakernet tarball was extracted:
#   sh bootstrap.sh
#
# Safe, idempotent, zero internet access.

set -eu

echo "==> 1. Verifying bundle signature, then member checksums (SHA256SUMS)"
if [ ! -f SHA256SUMS ]; then
    echo "FAIL: SHA256SUMS not found in current directory. Extract tarball first." >&2
    exit 1
fi
command -v openssl >/dev/null 2>&1 || { echo "FAIL: openssl is required to verify the bundle signature." >&2; exit 1; }
for sigfile in sneakernet-signing.pub SHA256SUMS.sig; do
    if [ ! -f "$sigfile" ]; then
        echo "FAIL: $sigfile not found in current directory. Extract tarball first." >&2
        exit 1
    fi
done
openssl dgst -sha256 -verify sneakernet-signing.pub -signature SHA256SUMS.sig SHA256SUMS >/dev/null \
    || { echo "FAIL: SHA256SUMS signature verification failed — do not trust this bundle." >&2; exit 1; }
# Enforce the new bundle's complete inventory before any member is executed.
# A valid signature on an incomplete checksum list must not authorize a tool.
PACKED_SHA=$(sed -n 's/^sha: *//p' MANIFEST.txt)
[ "${#PACKED_SHA}" -eq 40 ] || { echo "FAIL: MANIFEST must name a full commit SHA." >&2; exit 1; }
case "$PACKED_SHA" in *[!0-9a-f]*) echo "FAIL: invalid MANIFEST SHA." >&2; exit 1;; esac
MEMBERS="bootstrap.sh repo.bundle task_linux_amd64.tar.gz task-pin.txt task-LICENSE qdrant-image.tar openobserve-image.tar otelcol-image.tar app-ingest-$PACKED_SHA.tar app-agent-$PACKED_SHA.tar MANIFEST.txt PACKING_RECORD.txt sbom.json THIRD-PARTY-NOTICES.txt sneakernet-signing.pub"
[ ! -f oauth-proxy-image.tar ] || MEMBERS="$MEMBERS oauth-proxy-image.tar"
[ ! -f openwebui-image.tar ] || MEMBERS="$MEMBERS openwebui-image.tar"
[ ! -f openwebui-proxy-image.tar ] || MEMBERS="$MEMBERS openwebui-proxy-image.tar"
awk -v members="$MEMBERS" '
    BEGIN { n=split(members, names, " "); for (i=1; i<=n; i++) wanted[names[i]]=1 }
    NF != 2 || length($1) != 64 || $1 !~ /^[0-9a-f]+$/ || !($2 in wanted) || seen[$2]++ { bad=1 }
    END { for (name in wanted) if (!seen[name]) bad=1; exit bad }
' SHA256SUMS || { echo "FAIL: SHA256SUMS must contain exactly the required bundle members, including Task." >&2; exit 1; }
# Trust anchors (optional here, strict when set; #1118 F1/F2). Twin of
# check_trusted_pub in common.sh, inlined: no clone exists yet to source.
# SNEAKERNET_TRUSTED_PUB (upstream releases) and SNEAKERNET_GAP_TRUSTED_PUB
# (gap releases) name operator-held public keys obtained out of band. With
# neither set this check is TOFU: it binds the members together but cannot
# prove which key signed (every later live stage requires the anchor). With
# either set, each must be a plain readable file outside this bundle, the two
# must hold different keys, and the bundled key must equal the anchor for the
# origin MANIFEST.txt declares, read only after the signed SHA256SUMS has
# authenticated MANIFEST.txt. Messages name variables, never paths.
if [ -n "${SNEAKERNET_TRUSTED_PUB:-}${SNEAKERNET_GAP_TRUSTED_PUB:-}" ]; then
    [ "$(awk '$2 == "MANIFEST.txt" { print $1 }' SHA256SUMS)" = "$(sha256sum MANIFEST.txt | awk '{ print $1 }')" ] || {
        echo "FAIL: MANIFEST.txt does not match its signed SHA256SUMS entry — do not trust this bundle." >&2; exit 1;
    }
    BUNDLE_REAL=$(pwd -P)
    ANCHOR_IDS=""
    for ANCHOR_PAIR in upstream:SNEAKERNET_TRUSTED_PUB gap:SNEAKERNET_GAP_TRUSTED_PUB; do
        ANCHOR_VAR=${ANCHOR_PAIR#*:}
        eval "ANCHOR=\${$ANCHOR_VAR:-}"
        [ -n "$ANCHOR" ] || continue
        { [ -f "$ANCHOR" ] && [ -r "$ANCHOR" ] && [ ! -L "$ANCHOR" ]; } || {
            echo "FAIL: release trust anchor $ANCHOR_VAR is not a readable plain file (not a link) — point it at the release signing public key obtained out of band." >&2; exit 1;
        }
        case "$ANCHOR" in */*) ANCHOR_DIR=${ANCHOR%/*}; [ -n "$ANCHOR_DIR" ] || ANCHOR_DIR=/ ;; *) ANCHOR_DIR=. ;; esac
        ANCHOR_DIR=$(cd -P -- "$ANCHOR_DIR" && pwd -P)
        case "${ANCHOR_DIR%/}/${ANCHOR##*/}" in "$BUNDLE_REAL"/*)
            echo "FAIL: release trust anchor $ANCHOR_VAR lies inside the bundle it would verify — an anchor never comes from the artifact it vouches for; keep it outside (/work/anchors on the bastion)." >&2; exit 1 ;;
        esac
        if openssl pkey -pubin -in "$ANCHOR" -noout 2>/dev/null; then
            ANCHOR_ID=$(openssl pkey -pubin -in "$ANCHOR" -outform DER | sha256sum | awk '{ print $1 }')
        else
            ANCHOR_ID=$(sha256sum < "$ANCHOR" | awk '{ print $1 }')
        fi
        for ANCHOR_PREV in $ANCHOR_IDS; do
            [ "${ANCHOR_PREV%%:*}" != "$ANCHOR_ID" ] || {
                echo "FAIL: release trust anchors ${ANCHOR_PREV#*:} and $ANCHOR_VAR hold the same key — the upstream and gap release keys must differ; fix the anchor files." >&2; exit 1;
            }
        done
        ANCHOR_IDS="$ANCHOR_IDS $ANCHOR_ID:$ANCHOR_VAR"
    done
    ORIGIN=$(awk '/^origin:/ { n++; v = $0 } END { if (n != 1) exit 1; print v }' MANIFEST.txt) || ORIGIN=""
    case "$ORIGIN" in
        "origin: upstream") ORIGIN=upstream; ANCHOR_VAR=SNEAKERNET_TRUSTED_PUB ;;
        "origin: gap") ORIGIN=gap; ANCHOR_VAR=SNEAKERNET_GAP_TRUSTED_PUB ;;
        *) echo "FAIL: MANIFEST.txt must declare exactly one release origin (origin: upstream or origin: gap) — do not trust this bundle." >&2; exit 1 ;;
    esac
    eval "ANCHOR=\${$ANCHOR_VAR:-}"
    [ -n "$ANCHOR" ] || {
        echo "FAIL: no release trust anchor is configured for the bundle's origin ($ORIGIN) — set $ANCHOR_VAR to the $ORIGIN release signing public key obtained out of band." >&2; exit 1;
    }
    cmp -s "$ANCHOR" sneakernet-signing.pub || {
        echo "FAIL: bundle pubkey does not match $ANCHOR_VAR, the release trust anchor for the bundle's origin ($ORIGIN) — untrusted bundle." >&2; exit 1;
    }
fi
sha256sum -c SHA256SUMS
BUNDLE_HEAD=$(git bundle list-heads repo.bundle HEAD | awk '{print $1}')
[ "$BUNDLE_HEAD" = "$PACKED_SHA" ] || { echo "FAIL: repo.bundle HEAD does not match MANIFEST SHA." >&2; exit 1; }
[ "$(uname -s)/$(uname -m)" = Linux/x86_64 ] || {
    echo "FAIL: unsupported platform; bundled Task covers linux-amd64 only." >&2; exit 1;
}

echo "==> 2. Setting up repository workspace"
DEST_DIR="${AIRGAP_WORKSPACE:-qdrant-pdf-rag}"

if [ ! -e "$DEST_DIR/.git" ]; then
    if [ ! -f repo.bundle ]; then
        echo "FAIL: repo.bundle not found in current directory." >&2
        exit 1
    fi
    echo "    Cloning repository from repo.bundle into ./$DEST_DIR..."
    git clone --quiet -- repo.bundle "$DEST_DIR"
else
    echo "    Repository ./$DEST_DIR already exists."
fi

WORKSPACE_SHA=$(git -C "$DEST_DIR" rev-parse HEAD)
[ "$WORKSPACE_SHA" = "$PACKED_SHA" ] || {
    echo "FAIL: workspace HEAD does not match MANIFEST SHA. Use a fresh AIRGAP_WORKSPACE, or deliberately checkout the approved bundle SHA before rerunning bootstrap." >&2
    exit 1
}
# Existing operator env/untracked artifacts stay intact. Refuse locally changed
# installer code rather than executing it under the bundle's verification claim.
for tool_file in task-artifact.sh install-task.sh task-pin.txt; do
    git -C "$DEST_DIR" show "$PACKED_SHA:scripts/tools/$tool_file" | cmp -s - "$DEST_DIR/scripts/tools/$tool_file" || {
        echo "FAIL: workspace tool scripts differ from the approved commit." >&2; exit 1;
    }
done
# Equal HEAD is not enough: any tracked staged/unstaged edit (scripts, charts,
# Taskfile) would run under the release claim. Untracked operator files stay.
git -C "$DEST_DIR" diff --quiet HEAD -- >/dev/null 2>&1 || {
    echo "FAIL: workspace has tracked changes against the approved commit; restore them (operator settings belong in the untracked airgap.env)." >&2; exit 1;
}
# `git diff` trusts the index: assume-unchanged and skip-worktree (incl. sparse
# checkout) files are never compared, so the flags themselves are refused.
# ls-files tags: H = normal, lowercase = assume-unchanged, S/s = skip-worktree.
WS_FLAGS=$(git -C "$DEST_DIR" ls-files -v -- ':/' 2>/dev/null) || {
    echo "FAIL: workspace index cannot be read; cannot prove no tracked file is hidden from the change check." >&2; exit 1;
}
printf '%s\n' "$WS_FLAGS" | awk 'NF && substr($0, 1, 2) != "H " { bad=1 } END { exit bad }' || {
    echo "FAIL: workspace hides tracked files from change detection (assume-unchanged, skip-worktree or sparse checkout); clear the flags (git update-index --no-assume-unchanged / --no-skip-worktree) and rerun." >&2; exit 1;
}
cmp -s task-pin.txt "$DEST_DIR/scripts/tools/task-pin.txt" || {
    echo "FAIL: bundled Task pin does not match the approved workspace." >&2; exit 1;
}
# A staging directory left by an interrupted run is never accepted and never
# silently deleted: promotion may be partial, and the operator decides cleanup.
STAGE="$DEST_DIR/dist/.bootstrap-staging"
if [ -e "$STAGE" ] || [ -L "$STAGE" ]; then
    echo "FAIL: interrupted bootstrap staging found at $STAGE; dist/ may contain partially promoted members. Do not use it until the complete bundle is reinstalled and verified. Remove only the staging directory (rm -rf '$STAGE') and rerun bootstrap.sh from the verified bundle." >&2
    exit 1
fi

echo "==> 3. Installing verified workspace-local Task (offline)"
BUNDLE_DIR=$(pwd)
. "$DEST_DIR/scripts/tools/task-artifact.sh"
task_read_pin "$DEST_DIR/scripts/tools/task-pin.txt"
task_check_manifest MANIFEST.txt
printf '%s  %s\n' "$TASK_LICENSE_SHA256" task-LICENSE | sha256sum -c - >/dev/null || task_fail "bundled Task license checksum mismatch"
sh "$DEST_DIR/scripts/tools/install-task.sh" --archive "$BUNDLE_DIR/task_linux_amd64.tar.gz"

echo "==> 4. Staging, verifying and committing sneakernet artifacts to ./$DEST_DIR/dist"
# Copy into a staging directory inside dist/ (same filesystem), verify the
# staged bytes against the signed checksum list there, and only then move
# members into place. dist/ keeps operator files and earlier releases, so it is
# never swapped wholesale; instead SHA256SUMS.sig, SHA256SUMS and MANIFEST.txt
# move LAST and in that order, so any interruption leaves an old or incomplete
# bundle that signed member-verification consumers refuse, never a mixed one
# that verifies. An uncleanly killed run leaves the staging directory, which
# the next run refuses (see above) instead of trusting.
mkdir -p "$DEST_DIR/dist"
mkdir "$STAGE"
trap 'rm -rf "$STAGE"' EXIT
trap 'exit 1' HUP INT TERM
for item in $MEMBERS SHA256SUMS SHA256SUMS.sig; do
    cp "$item" "$STAGE/"
done
for item in SHA256SUMS SHA256SUMS.sig; do
    cmp -s "$item" "$STAGE/$item" || { echo "FAIL: staged $item differs from the bundle; dist/ was not modified." >&2; exit 1; }
done
(cd "$STAGE" && openssl dgst -sha256 -verify sneakernet-signing.pub -signature SHA256SUMS.sig SHA256SUMS >/dev/null && sha256sum -c SHA256SUMS >/dev/null) \
    || { echo "FAIL: staged artifacts failed verification (interrupted or corrupt copy); dist/ was not modified." >&2; exit 1; }
for item in $MEMBERS; do
    [ "$item" = MANIFEST.txt ] || mv -f "$STAGE/$item" "$DEST_DIR/dist/$item"
done
mv -f "$STAGE/SHA256SUMS.sig" "$DEST_DIR/dist/SHA256SUMS.sig"
mv -f "$STAGE/SHA256SUMS" "$DEST_DIR/dist/SHA256SUMS"
mv -f "$STAGE/MANIFEST.txt" "$DEST_DIR/dist/MANIFEST.txt"
rm -rf "$STAGE"
trap - EXIT HUP INT TERM
(cd "$DEST_DIR/dist" && sha256sum -c SHA256SUMS >/dev/null) \
    || { echo "FAIL: committed dist/ does not verify against SHA256SUMS; rerun bootstrap.sh." >&2; exit 1; }

echo "==> 5. Checking airgap.env"
if [ ! -f "$DEST_DIR/airgap.env" ]; then
    if [ -f "$DEST_DIR/airgap.env.example" ]; then
        cp "$DEST_DIR/airgap.env.example" "$DEST_DIR/airgap.env"
        echo "    Created $DEST_DIR/airgap.env from example."
        echo "    Please edit $DEST_DIR/airgap.env to specify your INTERNAL_REGISTRY, STORAGE_CLASS, etc."
    fi
else
    echo "    $DEST_DIR/airgap.env already present."
fi

echo ""
echo "SUCCESS: Sneakernet bootstrap completed successfully."
echo "Next step:"
echo "  cd \"$DEST_DIR\" && sh scripts/tools/run-task.sh airgap:validate"
