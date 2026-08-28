#!/bin/sh
# AIR-GAP SIDE (issue #15): verify member checksums, load the packed images,
# push to the internal registry under the SAME names and SHA tags.
#
#   make airgap-load
#
# Run from inside the clone of repo.bundle (see README). The packed artifacts
# may sit in ./dist or the unpack directory (parent). No cloning happens here:
# clone is the operator's step. Registry credentials: `skopeo login
# $INTERNAL_REGISTRY` (or a logged-in podman credential store) before running.
# No tokens in git, ever.

. "$(dirname -- "$0")/common.sh"

enforce_product_rules
resolve_aliases
require_env INTERNAL_REGISTRY IMAGE_SHA
command -v skopeo >/dev/null 2>&1 || die "skopeo is required on the air-gap bastion"

# Packed artifacts: unpacked in the current directory or the parent (the docs
# flow unpacks next to the clone). Either works.
ARTDIR=""
if [ -f dist/SHA256SUMS ] && [ -f dist/repo.bundle ]; then
    ARTDIR=dist
elif [ -f ../SHA256SUMS ] && [ -f ../repo.bundle ]; then
    ARTDIR=..
else
    die "packed artifacts not found — unpack the sneakernet tarball next to this clone (tar xf qdrant-pdf-rag-<sha>.tar)"
fi
case "$ARTDIR" in
    /*) ;;
    *) ARTDIR="$(pwd)/$ARTDIR" ;;
esac

echo "==> Verify member checksums"
(cd "$ARTDIR" && sha256sum -c SHA256SUMS)

# Cross-check IMAGE_SHA against the packed MANIFEST.
packed_sha=$(awk '/^sha: /{print $2}' "$ARTDIR/MANIFEST.txt")
[ "$IMAGE_SHA" = "$packed_sha" ] || \
    die "IMAGE_SHA=$IMAGE_SHA does not match the packed MANIFEST sha ($packed_sha) — wrong SHA for this sneakernet bundle"

load() {
    src=$1
    dst=$2
    echo "==> $src -> $dst"
    run skopeo copy "docker-archive:$ARTDIR/$src" "docker://$dst"
}

load qdrant-image.tar "$INTERNAL_REGISTRY/qdrant/qdrant:v1.19.0-unprivileged"
load "app-ingest-$IMAGE_SHA.tar" "$INTERNAL_REGISTRY/qdrant-pdf-rag-ingest:$IMAGE_SHA"
load "app-agent-$IMAGE_SHA.tar" "$INTERNAL_REGISTRY/qdrant-pdf-rag-agent:$IMAGE_SHA"

echo ""
echo "Loaded 3 images into $INTERNAL_REGISTRY (SHA tag: $IMAGE_SHA)."
next_step "make airgap-deploy"
