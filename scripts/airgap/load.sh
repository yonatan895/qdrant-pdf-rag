#!/bin/sh
# AIR-GAP SIDE (issue #15): verify member checksums, load the packed images,
# push to the internal registry under the SAME names and SHA tags.
#
#   sh scripts/tools/run-task.sh airgap:load
#
# Run from inside the clone of repo.bundle (see README). The packed artifacts
# may sit in ./dist or the unpack directory (parent). No cloning happens here:
# clone is the operator's step. Registry credentials: `skopeo login
# $INTERNAL_REGISTRY` (or a logged-in podman credential store) before running.
# No tokens in git, ever.

. "$(dirname -- "$0")/common.sh"
. "$(dirname -- "$0")/image_identity.sh"

enforce_product_rules
resolve_aliases
require_env INTERNAL_REGISTRY IMAGE_SHA
command -v skopeo >/dev/null 2>&1 || die "skopeo is required on the air-gap bastion"
command -v python3 >/dev/null 2>&1 || die "python3 is required on the air-gap bastion"

# Packed artifacts: unpacked in the current directory or the parent (the docs
# flow unpacks next to the clone), or specified by AIRGAP_BUNDLE_DIR.
ARTDIR=""
if [ -n "${AIRGAP_BUNDLE_DIR:-}" ] && [ -f "$AIRGAP_BUNDLE_DIR/SHA256SUMS" ]; then
    ARTDIR="$AIRGAP_BUNDLE_DIR"
elif [ -f dist/SHA256SUMS ]; then
    ARTDIR=dist
elif [ -f ../SHA256SUMS ]; then
    ARTDIR=..
elif [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    die "packed artifacts not found — unpack the sneakernet tarball next to this clone (tar xf qdrant-pdf-rag-<sha>.tar)"
else
    ARTDIR=dist
fi
case "$ARTDIR" in
    /*) ;;
    *) ARTDIR="$(pwd)/$ARTDIR" ;;
esac

# Operator console (ADR-0004): the oauth-proxy member exists only when the
# connected pack host had a recorded digest; older bundles simply lack it.
OAUTH_TAR=""
if [ -f "$ARTDIR/SHA256SUMS" ]; then
    # Workspace upgrades retain old archives. Only the current signed inventory
    # selects optional images; every selected member is verified below.
    if awk '$2 == "oauth-proxy-image.tar" { found=1 } END { exit !found }' "$ARTDIR/SHA256SUMS"; then
        OAUTH_TAR="oauth-proxy-image.tar"
    fi
elif [ "${AIRGAP_DRYRUN:-0}" = "1" ] && [ -f "$ARTDIR/oauth-proxy-image.tar" ]; then
    OAUTH_TAR="oauth-proxy-image.tar"
fi
LOADED_COUNT=4
[ -n "$OAUTH_TAR" ] && LOADED_COUNT=5

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "==> [dryrun] Member checksum verification skipped"
    echo "[dryrun] skopeo copy docker-archive:$ARTDIR/qdrant-image.tar docker://$INTERNAL_REGISTRY/qdrant/qdrant:v1.19.0-unprivileged"
    echo "[dryrun] skopeo copy docker-archive:$ARTDIR/jaeger-image.tar docker://$INTERNAL_REGISTRY/jaegertracing/jaeger:v2.20.0"
    echo "[dryrun] skopeo copy docker-archive:$ARTDIR/app-ingest-$IMAGE_SHA.tar docker://$INTERNAL_REGISTRY/qdrant-pdf-rag-ingest:$IMAGE_SHA"
    echo "[dryrun] skopeo copy docker-archive:$ARTDIR/app-agent-$IMAGE_SHA.tar docker://$INTERNAL_REGISTRY/qdrant-pdf-rag-agent:$IMAGE_SHA"
    if [ -n "$OAUTH_TAR" ]; then
        echo "[dryrun] skopeo copy docker-archive:$ARTDIR/$OAUTH_TAR docker://$INTERNAL_REGISTRY/openshift4/ose-oauth-proxy:v4.14"
    fi
    echo "Notice: registry image identity not release-verified (dry-run pushes nothing and reads no registry)"
    echo ""
    echo "Loaded $LOADED_COUNT images into $INTERNAL_REGISTRY (dry-run)."
    next_step "sh scripts/tools/run-task.sh airgap:deploy"
    exit 0
fi

echo "==> Verify bundle signature, then member checksums"
command -v openssl >/dev/null 2>&1 || die "openssl is required to verify the bundle signature"
for sigfile in sneakernet-signing.pub SHA256SUMS.sig; do
    [ -f "$ARTDIR/$sigfile" ] || die "$sigfile not found in $ARTDIR — unpack the sneakernet tarball first"
done
check_trusted_pub "$ARTDIR"
(cd "$ARTDIR" && openssl dgst -sha256 -verify sneakernet-signing.pub -signature SHA256SUMS.sig SHA256SUMS >/dev/null) \
    || die "SHA256SUMS signature verification failed — do not trust this bundle"
(cd "$ARTDIR" && sha256sum -c SHA256SUMS)

# Cross-check IMAGE_SHA against the packed MANIFEST.
packed_sha=$(awk '/^sha: /{print $2}' "$ARTDIR/MANIFEST.txt")
[ "$IMAGE_SHA" = "$packed_sha" ] || \
    die "IMAGE_SHA=$IMAGE_SHA does not match the packed MANIFEST sha ($packed_sha) — wrong SHA for this sneakernet bundle"
# The executing checkout itself must resolve to the packed SHA (issue #414):
# loading B images with A scripts checked out must fail before any push.
MANIFEST="$ARTDIR/MANIFEST.txt" check_checkout_sha

# Task is a signed host artifact, never an image to push. Require its members
# even if a malformed, signed checksum list omitted them.
for member in task_linux_amd64.tar.gz task-pin.txt task-LICENSE; do
    awk -v member="$member" '$2 == member { n++ } END { exit n != 1 }' "$ARTDIR/SHA256SUMS" || die "Task member missing from SHA256SUMS: $member"
done
cmp -s "$ARTDIR/task-pin.txt" "$REPO_ROOT/scripts/tools/task-pin.txt" || die "bundled Task pin differs from approved workspace"
. "$REPO_ROOT/scripts/tools/task-artifact.sh"
task_read_pin "$ARTDIR/task-pin.txt"
task_verify_archive "$ARTDIR/$TASK_ASSET"
task_check_manifest "$ARTDIR/MANIFEST.txt"
printf '%s  %s\n' "$TASK_LICENSE_SHA256" "$ARTDIR/task-LICENSE" | sha256sum -c - >/dev/null || die "Task license checksum mismatch"

# Digest binding: every image must equal the MANIFEST-recorded digest.
check_image_digest() {
    _tar=$1
    _key=$2
    _actual=$(skopeo inspect "docker-archive:$ARTDIR/$_tar" --format '{{.Digest}}')
    _expected=$(awk -F': ' -v k="$_key" '$1 == k {print $2}' "$ARTDIR/MANIFEST.txt")
    [ -n "$_expected" ] || die "MANIFEST.txt has no $_key entry"
    [ "$_actual" = "$_expected" ] || \
        die "$_tar digest $_actual does not match MANIFEST $_key ($_expected) — wrong image bytes"
    # The config digest is what deploy/ingest later verify the registry against
    # (issue #272): it must be present and be this archive's image config.
    _cfgkey="${_key%_digest}_config_digest"
    _cfg=$(awk -F': ' -v k="$_cfgkey" '$1 == k {print $2}' "$ARTDIR/MANIFEST.txt")
    [ -n "$_cfg" ] || die "MANIFEST.txt has no $_cfgkey entry — repack the bundle with this release's pack.sh"
    [ "$(archive_config_digest "$ARTDIR/$_tar")" = "$_cfg" ] || \
        die "$_tar image config does not match MANIFEST $_cfgkey — wrong image bytes"
}
check_image_digest qdrant-image.tar qdrant_digest
check_image_digest jaeger-image.tar jaeger_digest
check_image_digest "app-ingest-$IMAGE_SHA.tar" ingest_digest
check_image_digest "app-agent-$IMAGE_SHA.tar" agent_digest
if [ -n "$OAUTH_TAR" ]; then
    check_image_digest "$OAUTH_TAR" oauth_proxy_digest
fi

# Post-load identity (issue #272): `skopeo copy` exiting 0 does not prove what
# the tag now resolves to. Read the tag back and require that the registry
# serves the packed image, by image config digest (which pins the rootfs
# diffIDs). Archive and registry MANIFEST digests legitimately differ: a docker
# archive holds uncompressed layers, a registry stores compressed ones. The
# verified registry manifest digest is printed as the immutable pin for
# `repo@digest` references; a tag alone is never accepted as identity.
VERIFY_TMP=$(mktemp -d)
trap 'rm -rf "$VERIFY_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

verify_registry_image() {
    _vtar=$1
    _vdst=$2
    _vkey=$3
    _vexpected=$(awk -F': ' -v k="$_vkey" '$1 == k {print $2}' "$ARTDIR/MANIFEST.txt")
    skopeo inspect --raw "docker-archive:$ARTDIR/$_vtar" > "$VERIFY_TMP/archive.raw" || \
        die "cannot read the packed archive manifest for $_vdst"
    # shellcheck disable=SC2046
    skopeo inspect --raw $(inspect_args) "docker://$_vdst" > "$VERIFY_TMP/registry.raw" || \
        die "cannot read back registry image $_vdst after the push — it is not verified; fix registry access and rerun"
    _vrc=0
    _vdigest=$(python3 scripts/airgap/image_manifest.py load "$VERIFY_TMP/archive.raw" "$VERIFY_TMP/registry.raw" "$_vexpected") || _vrc=$?
    case "$_vrc" in
        0) ;;
        3) die "archive for $_vdst no longer matches its MANIFEST digest — do not trust this bundle" ;;
        4) die "registry image $_vdst is not a single-image manifest — a tag must resolve to exactly the packed image" ;;
        *) die "registry image $_vdst is not the packed image (config digest differs from the archive) — the tag was swapped or not overwritten; do not deploy it" ;;
    esac
    echo "==> verified $_vdst@$_vdigest"
}

load() {
    src=$1
    dst=$2
    key=$3
    extra_args="${SKOPEO_ARGS:-}"
    if [ "${INSECURE_REGISTRY:-false}" = "true" ]; then
        extra_args="$extra_args --dest-tls-verify=false"
    fi
    echo "==> $src -> $dst"
    # shellcheck disable=SC2086
    run skopeo copy $extra_args "docker-archive:$ARTDIR/$src" "docker://$dst"
    verify_registry_image "$src" "$dst" "$key"
}

load qdrant-image.tar "$INTERNAL_REGISTRY/qdrant/qdrant:v1.19.0-unprivileged" qdrant_digest
# Upstream source tag is 2.20.0 in images.txt; retagged to v2.20.0 to match the first-party Jaeger template
load jaeger-image.tar "$INTERNAL_REGISTRY/jaegertracing/jaeger:v2.20.0" jaeger_digest
load "app-ingest-$IMAGE_SHA.tar" "$INTERNAL_REGISTRY/qdrant-pdf-rag-ingest:$IMAGE_SHA" ingest_digest
load "app-agent-$IMAGE_SHA.tar" "$INTERNAL_REGISTRY/qdrant-pdf-rag-agent:$IMAGE_SHA" agent_digest
if [ -n "$OAUTH_TAR" ]; then
    # ADR-0004 console Route: same tag the production chart renders for the sidecar.
    load "$OAUTH_TAR" "$INTERNAL_REGISTRY/openshift4/ose-oauth-proxy:v4.14" oauth_proxy_digest
fi

echo ""
echo "Loaded $LOADED_COUNT images into $INTERNAL_REGISTRY (SHA tag: $IMAGE_SHA)."
next_step "sh scripts/tools/run-task.sh airgap:deploy"
