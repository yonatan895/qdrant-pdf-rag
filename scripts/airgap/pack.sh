#!/bin/sh
# CONNECTED SIDE (issue #15): pack a green public-main SHA into one sneakernet
# tarball: git bundle + 3 image archives + MANIFEST + checksums.
#
#   make airgap-pack              # or: sh scripts/airgap/pack.sh
#
# Fails closed if any GHCR image 404s (main build not finished / wrong SHA).
# Air-gap does not build: connected main is the only image factory.

. "$(dirname -- "$0")/common.sh"

enforce_product_rules
require_env IMAGE_SHA
command -v skopeo >/dev/null 2>&1 || die "skopeo is required on the connected pack host"
[ -d .git ] || die "run from a git clone of the repository"

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

# Qdrant pin comes from images.txt (name column); digest applies when recorded.
QDRANT_REF=""
QDRANT_DIGEST=""
while IFS= read -r line; do
    case "$line" in
        \#*|"") continue ;;
        *qdrant*)
            QDRANT_REF=$(echo "$line" | awk '{print $1}')
            digest=$(echo "$line" | awk '{print $2}')
            [ "$digest" != "sha256:PENDING" ] && QDRANT_DIGEST=$digest
            break
            ;;
    esac
done < images.txt
[ -n "$QDRANT_REF" ] || die "no qdrant image pin found in images.txt"
if [ -n "$QDRANT_DIGEST" ]; then
    QDRANT_REF="${QDRANT_REF%@*}@${QDRANT_DIGEST}"
fi

DIST=dist
OUT_TARBALL="$DIST/qdrant-pdf-rag-${IMAGE_SHA}.tar"
mkdir -p "$DIST"
rm -f "$DIST"/repo.bundle "$DIST"/qdrant-image.tar "$DIST"/app-*.tar \
      "$DIST"/MANIFEST.txt "$DIST"/SHA256SUMS "$OUT_TARBALL"

echo "==> Git bundle of the checked-out commit"
git bundle create "$DIST/repo.bundle" --all
git bundle verify "$DIST/repo.bundle" >/dev/null

echo "==> Pulling images (fail closed on missing GHCR tags)"
skopeo copy "docker://$QDRANT_REF" "docker-archive:$DIST/qdrant-image.tar"
skopeo copy "docker://$INGEST_IMAGE" "docker-archive:$DIST/app-ingest-$IMAGE_SHA.tar"
skopeo copy "docker://$AGENT_IMAGE" "docker-archive:$DIST/app-agent-$IMAGE_SHA.tar"

echo "==> MANIFEST"
CHART_VERSION=$(basename charts/qdrant-*.tgz .tgz)
{
    echo "sha: $IMAGE_SHA"
    echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "qdrant: $QDRANT_REF"
    echo "chart: $CHART_VERSION"
    echo "ingest: $INGEST_IMAGE"
    echo "agent: $AGENT_IMAGE"
    echo "app_registry: $APP_REGISTRY"
} > "$DIST/MANIFEST.txt"
cat "$DIST/MANIFEST.txt"

echo "==> Checksums (members)"
cd "$DIST"
sha256sum repo.bundle qdrant-image.tar app-ingest-"$IMAGE_SHA".tar \
          app-agent-"$IMAGE_SHA".tar MANIFEST.txt > SHA256SUMS
cd ..

echo "==> Tarball"
tar -C "$DIST" -cf "$OUT_TARBALL" repo.bundle qdrant-image.tar \
    app-ingest-"$IMAGE_SHA".tar app-agent-"$IMAGE_SHA".tar MANIFEST.txt SHA256SUMS

echo ""
echo "Packed: $OUT_TARBALL"
echo "Tarball SHA256 (verify before unpack):"
sha256sum "$OUT_TARBALL"
echo "Transfer dist/$(basename "$OUT_TARBALL") and dist/SHA256SUMS to the air-gap bastion."
next_step "sha256sum -c SHA256SUMS && tar xf qdrant-pdf-rag-${IMAGE_SHA}.tar && make airgap-load"
