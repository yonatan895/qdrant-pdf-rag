#!/bin/sh
# CONNECTED SIDE (issue #15): pack a green public-main SHA into one sneakernet
# tarball: git bundle + 3 image archives + MANIFEST + member checksums.
#
#   make airgap-pack              # or: sh scripts/airgap/pack.sh
#
# IMAGE_SHA defaults to the full SHA of the checked-out commit and MUST equal
# the GHCR tag that e2e.yml pushed for that SHA. Fails closed if any image
# pull 404s (main build not finished / wrong SHA). Air-gap does not build:
# connected main is the only image factory.

. "$(dirname -- "$0")/common.sh"

enforce_product_rules
resolve_aliases
command -v skopeo >/dev/null 2>&1 || die "skopeo is required on the connected pack host"
command -v openssl >/dev/null 2>&1 || die "openssl is required to sign the bundle"
[ -n "${SNEAKERNET_SIGNING_KEY:-}" ] || die "SNEAKERNET_SIGNING_KEY is unset (path to the PEM signing key; CI uses the secret, local rehearsal generates a throwaway pair)"
[ -f "$SNEAKERNET_SIGNING_KEY" ] || die "SNEAKERNET_SIGNING_KEY file not found: $SNEAKERNET_SIGNING_KEY"
[ -d .git ] || die "run from a git clone of the repository"
[ -n "$IMAGE_SHA" ] || die "IMAGE_SHA could not be resolved from git"
[ "$IMAGE_SHA" = "$(git rev-parse HEAD)" ] || \
    die "IMAGE_SHA=$IMAGE_SHA is not the checked-out commit ($(git rev-parse HEAD)). Pack at the SHA whose GHCR tags exist; the bundle is always of HEAD."

GHCR_OWNER=${GHCR_OWNER:-}
APP_REGISTRY=${AIRGAP_APP_REGISTRY:-}
if [ -z "$APP_REGISTRY" ]; then
    if [ -z "$GHCR_OWNER" ]; then
        remote=$(git remote get-url origin 2>/dev/null || true)
        case "$remote" in
            *github.com[:/]*) GHCR_OWNER=$(echo "$remote" | sed -E 's#.*github\.com[:/]##; s#\.git$##' | cut -d/ -f1 | tr 'A-Z' 'a-z') ;;
        esac
    fi
    [ -n "$GHCR_OWNER" ] || die "cannot infer GHCR owner from 'git remote get-url origin'; set GHCR_OWNER or AIRGAP_APP_REGISTRY"
    APP_REGISTRY="ghcr.io/${GHCR_OWNER}"
fi

INGEST_IMAGE="${APP_REGISTRY}/qdrant-pdf-rag-ingest:${IMAGE_SHA}"
AGENT_IMAGE="${APP_REGISTRY}/qdrant-pdf-rag-agent:${IMAGE_SHA}"

# Third-party pins come from images.txt (name column); digest applies when recorded.
QDRANT_REF=$(pin_from_images_txt qdrant)

# Jaeger v2 trace-backend pin (issue #83): same pin discipline.
JAEGER_REF=$(pin_from_images_txt jaeger)

DIST="$REPO_ROOT/dist"
OUT_TARBALL="$DIST/qdrant-pdf-rag-${IMAGE_SHA}.tar"
mkdir -p "$DIST"
rm -f "$DIST"/repo.bundle "$DIST"/qdrant-image.tar "$DIST"/jaeger-image.tar "$DIST"/app-*.tar \
      "$DIST"/MANIFEST.txt "$DIST"/SHA256SUMS "$OUT_TARBALL" "$OUT_TARBALL.sha256"

echo "==> Git bundle of the checked-out commit"
# HEAD must be an explicit ref: without it, `git clone repo.bundle` on the
# air-gap side falls back to a default branch instead of the packed SHA.
git bundle create "$DIST/repo.bundle" HEAD --all
git bundle verify "$DIST/repo.bundle" >/dev/null

echo "==> Pulling images (fail closed on missing tags)"
extra_args="${SKOPEO_ARGS:-}"
if [ "${INSECURE_REGISTRY:-false}" = "true" ]; then
    extra_args="$extra_args --src-tls-verify=false"
fi
# shellcheck disable=SC2086
skopeo copy $extra_args "docker://$QDRANT_REF" "docker-archive:$DIST/qdrant-image.tar"
# shellcheck disable=SC2086
skopeo copy $extra_args "docker://$JAEGER_REF" "docker-archive:$DIST/jaeger-image.tar"
# shellcheck disable=SC2086
skopeo copy $extra_args "docker://$INGEST_IMAGE" "docker-archive:$DIST/app-ingest-$IMAGE_SHA.tar"
# shellcheck disable=SC2086
skopeo copy $extra_args "docker://$AGENT_IMAGE" "docker-archive:$DIST/app-agent-$IMAGE_SHA.tar"

echo "==> Image digests (bound into MANIFEST, verified on load)"
INGEST_DIGEST=$(skopeo inspect "docker-archive:$DIST/app-ingest-$IMAGE_SHA.tar" --format '{{.Digest}}')
AGENT_DIGEST=$(skopeo inspect "docker-archive:$DIST/app-agent-$IMAGE_SHA.tar" --format '{{.Digest}}')
QDRANT_DIGEST=${QDRANT_REF##*@}
JAEGER_DIGEST=${JAEGER_REF##*@}
UBI_REF=$(pin_from_images_txt python-314-minimal)
UBI_DIGEST=${UBI_REF##*@}

echo "==> MANIFEST"
CHART_VERSION=$(basename charts/qdrant-*.tgz .tgz)
CHART_SHA256=$(sha256sum charts/qdrant-*.tgz | awk '{print $1}')
{
    echo "sha: $IMAGE_SHA"
    echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "qdrant: $QDRANT_REF"
    echo "qdrant_digest: $QDRANT_DIGEST"
    echo "jaeger: $JAEGER_REF"
    echo "jaeger_digest: $JAEGER_DIGEST"
    echo "chart: $CHART_VERSION"
    echo "chart_sha256: $CHART_SHA256"
    echo "ingest: $INGEST_IMAGE"
    echo "ingest_digest: $INGEST_DIGEST"
    echo "agent: $AGENT_IMAGE"
    echo "agent_digest: $AGENT_DIGEST"
    echo "app_registry: $APP_REGISTRY"
    echo "signed: true"
} > "$DIST/MANIFEST.txt"
cat "$DIST/MANIFEST.txt"

echo "==> SBOM (digest enumeration of every pinned input)"
WHEELS=$(awk '!/^#/ && NF {print $0}' requirements.lock.txt | sed 's/^/    "/; s/$/",/' | sed '$ s/,$//')
{
    echo "{"
    echo "  \"generated\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
    echo "  \"image_sha\": \"$IMAGE_SHA\","
    echo "  \"images\": ["
    echo "    {\"name\": \"qdrant\", \"ref\": \"$QDRANT_REF\", \"digest\": \"$QDRANT_DIGEST\"},"
    echo "    {\"name\": \"jaeger\", \"ref\": \"$JAEGER_REF\", \"digest\": \"$JAEGER_DIGEST\"},"
    echo "    {\"name\": \"app-ingest\", \"ref\": \"$INGEST_IMAGE\", \"digest\": \"$INGEST_DIGEST\"},"
    echo "    {\"name\": \"app-agent\", \"ref\": \"$AGENT_IMAGE\", \"digest\": \"$AGENT_DIGEST\"}"
    echo "  ],"
    echo "  \"base_image\": {\"ref\": \"$UBI_REF\", \"digest\": \"$UBI_DIGEST\"},"
    echo "  \"chart\": \"$CHART_VERSION\","
    echo "  \"chart_sha256\": \"$CHART_SHA256\","
    echo "  \"wheels\": ["
    echo "$WHEELS"
    echo "  ]"
    echo "}"
} > "$DIST/sbom.json"

echo "==> Bootstrap helper & Packing Record"
cp "$REPO_ROOT/scripts/airgap/bootstrap.sh" "$DIST/bootstrap.sh"
chmod +x "$DIST/bootstrap.sh"
{
    echo "================================================================================"
    echo "                   MAINFRAME RAG — AIR-GAP PACKING RECORD"
    echo "================================================================================"
    echo "Commit SHA:     $IMAGE_SHA"
    echo "Build Date:     $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Source Branch:  $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
    echo ""
    echo "PACKAGED ARTIFACTS:"
    echo "  - Git Bundle:          repo.bundle (complete git history)"
    echo "  - Qdrant Image:        $QDRANT_REF"
    echo "  - Jaeger Image:        $JAEGER_REF"
    echo "  - Ingest Image:        $INGEST_IMAGE"
    echo "  - Agent Image:         $AGENT_IMAGE"
    echo "  - Helm Chart:          $CHART_VERSION"
    echo "  - Sparse Weights:      FastEmbed Qdrant/bm25 (baked in images)"
    echo "  - Bundle signature:    SHA256SUMS.sig (openssl dgst; pubkey sneakernet-signing.pub)"
    echo "  - Signing key (pub):   $(openssl pkey -in "$SNEAKERNET_SIGNING_KEY" -pubout | openssl sha256)"
    echo ""
    echo "INSPECTION & BOOTSTRAP INSTRUCTIONS:"
    echo "  1. Verify root archive digest:"
    echo "       sha256sum -c qdrant-pdf-rag-${IMAGE_SHA}.tar.sha256"
    echo "  2. Extract archive:"
    echo "       tar -xf qdrant-pdf-rag-${IMAGE_SHA}.tar"
    echo "  3. Execute automated bootstrap:"
    echo "       sh bootstrap.sh"
    echo "================================================================================"
} > "$DIST/PACKING_RECORD.txt"

echo "==> Member checksums (verified again inside the air-gap after unpack)"
openssl pkey -in "$SNEAKERNET_SIGNING_KEY" -pubout -out "$DIST/sneakernet-signing.pub"
(
    cd "$DIST"
    sha256sum bootstrap.sh repo.bundle qdrant-image.tar jaeger-image.tar \
              app-ingest-"$IMAGE_SHA".tar app-agent-"$IMAGE_SHA".tar \
              MANIFEST.txt PACKING_RECORD.txt sbom.json sneakernet-signing.pub > SHA256SUMS
)

echo "==> Offline signature (openssl; verified by bootstrap.sh and load.sh)"
# The signature covers SHA256SUMS as written above (it cannot cover its own
# checksum line); a tampered .sig fails verification just the same.
openssl dgst -sha256 -sign "$SNEAKERNET_SIGNING_KEY" \
    -out "$DIST/SHA256SUMS.sig" "$DIST/SHA256SUMS"

echo "==> Tarball + tarball digest"
tar -C "$DIST" -cf "$OUT_TARBALL" bootstrap.sh repo.bundle qdrant-image.tar jaeger-image.tar \
    app-ingest-"$IMAGE_SHA".tar app-agent-"$IMAGE_SHA".tar MANIFEST.txt PACKING_RECORD.txt sbom.json sneakernet-signing.pub SHA256SUMS SHA256SUMS.sig
# shellcheck disable=SC2016
( cd "$DIST" && sha256sum "$(basename "$OUT_TARBALL")" ) > "$OUT_TARBALL.sha256"

echo ""
echo "Packed: $OUT_TARBALL"
echo "Transfer $OUT_TARBALL and $OUT_TARBALL.sha256 to the air-gap bastion."
echo "On the bastion: sha256sum -c $(basename "$OUT_TARBALL").sha256, then unpack"
echo "and run sh bootstrap.sh."
next_step "sha256sum -c qdrant-pdf-rag-${IMAGE_SHA}.tar.sha256 && tar xf qdrant-pdf-rag-${IMAGE_SHA}.tar && sh bootstrap.sh"
