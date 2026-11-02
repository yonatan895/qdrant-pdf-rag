#!/bin/sh
# CONNECTED SIDE (issue #15): pack a green public-main SHA into one sneakernet
# tarball: git bundle + oci-archive image members + MANIFEST + member checksums.
#
#   sh scripts/tools/run-task.sh airgap:pack              # or: sh scripts/airgap/pack.sh
#
# IMAGE_SHA defaults to the full SHA of the checked-out commit and MUST equal
# the GHCR tag that e2e.yml pushed for that SHA. Fails closed if any image
# pull 404s (main build not finished / wrong SHA). Air-gap does not build:
# connected main is the only image factory.

. "$(dirname -- "$0")/common.sh"
. "$(dirname -- "$0")/image_identity.sh"

enforce_product_rules
resolve_aliases
command -v skopeo >/dev/null 2>&1 || die "skopeo is required on the connected pack host"
command -v openssl >/dev/null 2>&1 || die "openssl is required to sign the bundle"
command -v python3 >/dev/null 2>&1 || die "python3 is required to write sbom.json"
[ -n "${SNEAKERNET_SIGNING_KEY:-}" ] || die "SNEAKERNET_SIGNING_KEY is unset (path to the PEM signing key; CI uses the secret, local rehearsal generates a throwaway pair)"
[ -f "$SNEAKERNET_SIGNING_KEY" ] || die "SNEAKERNET_SIGNING_KEY file not found: $SNEAKERNET_SIGNING_KEY"
openssl pkey -in "$SNEAKERNET_SIGNING_KEY" -noout >/dev/null 2>&1 || die "SNEAKERNET_SIGNING_KEY is not a valid PEM private key (store real newlines, not backslash-escaped ones)"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "run from a git clone of the repository"
[ -n "$IMAGE_SHA" ] || die "IMAGE_SHA could not be resolved from git"
[ "$IMAGE_SHA" = "$(git rev-parse HEAD)" ] || \
    die "IMAGE_SHA=$IMAGE_SHA is not the checked-out commit ($(git rev-parse HEAD)). Pack at the SHA whose GHCR tags exist; the bundle is always of HEAD. Usual cause: a stale IMAGE_SHA in ./airgap.env — explicit env beats the file, or update the file."

# The MANIFEST binds exactly one vendored Qdrant chart (issue #272).
_chart_count=0
for _c in charts/qdrant-*.tgz; do
    if [ -f "$_c" ]; then _chart_count=$((_chart_count + 1)); fi
done
[ "$_chart_count" -eq 1 ] || die "exactly one vendored Qdrant chart is required (charts/qdrant-*.tgz); found $_chart_count"

# Third-party license record (issue #376): fail closed on an unrecorded or changed
# dependency, image pin, vendored asset or notice file, before any image is pulled.
# Unresolved owner decisions are reported, not hidden; they do not block packing.
command -v python3 >/dev/null 2>&1 || die "python3 is required for the license inventory check"
LICENSE_SUMMARY=$(python3 "$REPO_ROOT/scripts/license_inventory.py" --root "$REPO_ROOT" check) || {
    printf '%s\n' "$LICENSE_SUMMARY" >&2
    die "license inventory check failed: record the dependency/notice change in licenses/inventory.json (docs/licensing.md)"
}
printf '%s\n' "$LICENSE_SUMMARY" | sed -n '$p'
LICENSE_STATUS=$(printf '%s\n' "$LICENSE_SUMMARY" | sed -n '$p' | sed 's/^license-inventory: //')

# Packaging provenance (#1030): the shipped Containerfile FROM digests must equal
# the approved pin in images.txt and the lock (the SBOM labels the base from
# images.txt, so an off-pin FROM would otherwise be attested as the pin).
python3 "$REPO_ROOT/scripts/image_inventory.py" --root "$REPO_ROOT" --check-containerfiles >/dev/null ||
    die "Containerfile base digest check failed: images/Containerfile.* FROM must equal the pin in images.txt and the lock (scripts/image_inventory.py --check-containerfiles)"

# `git bundle` records clean HEAD, so the license/SBOM scripts above must have
# read exactly that tree: refuse tracked changes (staged or not) and index flags
# that hide edits (assume-unchanged, skip-worktree, sparse) before anything is
# pulled or signed. Untracked operator files (airgap.env, dist/) stay allowed.
check_pack_tree_clean

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

# ADR-0006 trace/metrics backend (#956): OpenObserve OSS and the stateless
# otelcol-contrib scraper, same pin discipline.
OPENOBSERVE_REF=$(pin_from_images_txt openobserve)
OTELCOL_REF=$(pin_from_images_txt opentelemetry-collector-contrib)

# Operator console (ADR-0004): bundle the oauth-proxy sidecar image only once
# the connected host recorded a real digest — sha256:PENDING is skipped here
# (the CI pack job has no Red Hat registry credentials); deploying with
# AGENT_ROUTE=true fails closed until the digest is recorded and packed.
OAUTH_PROXY_REF=""
OAUTH_TAR=""
if pin_recorded oauth-proxy; then
    OAUTH_PROXY_REF=$(pin_from_images_txt oauth-proxy)
    OAUTH_TAR="oauth-proxy-image.tar"
else
    echo "==> oauth-proxy pin is sha256:PENDING: not bundled (agent console Route stays unavailable until recorded)"
fi

# Optional docs front end (ADR-0007, #1074): the hardened Open WebUI image is
# bundled only once its digest is recorded (#1073); while it is sha256:PENDING
# the member is skipped and deploying with OPENWEBUI_ENABLED=true fails closed.
OPENWEBUI_REF=""
OPENWEBUI_TAR=""
if pin_recorded qdrant-pdf-rag-openwebui; then
    OPENWEBUI_REF=$(pin_from_images_txt qdrant-pdf-rag-openwebui)
    OPENWEBUI_TAR="openwebui-image.tar"
else
    echo "==> Open WebUI pin is sha256:PENDING: not bundled (OPENWEBUI_ENABLED stays unavailable until recorded)"
fi
# The allow-list proxy sidecar of the same pod (ADR-0007 E1) travels with the
# Open WebUI image: bundled exactly when that member is.
OPENWEBUI_PROXY_REF=""
OPENWEBUI_PROXY_TAR=""
if [ -n "$OPENWEBUI_TAR" ]; then
    pin_recorded nginx-unprivileged || die "the Open WebUI image is recorded but the allow-list proxy (nginx-unprivileged) has no digest in images.txt: record it and repack"
    OPENWEBUI_PROXY_REF=$(pin_from_images_txt nginx-unprivileged)
    OPENWEBUI_PROXY_TAR="openwebui-proxy-image.tar"
fi

DIST="$REPO_ROOT/dist"
OUT_TARBALL="$DIST/qdrant-pdf-rag-${IMAGE_SHA}.tar"
mkdir -p "$DIST"
echo "==> Pinned Task host tool (archive, pin and license)"
. "$REPO_ROOT/scripts/tools/task-artifact.sh"
task_read_pin "$REPO_ROOT/scripts/tools/task-pin.txt"
task_check_platform
TASK_SOURCE=${AIRGAP_TASK_ARCHIVE:-$REPO_ROOT/.tools/cache/$TASK_ASSET}
TASK_TMP=$(mktemp -d "$DIST/.task-pack.XXXXXX")
trap 'rm -rf "$TASK_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ ! -f "$TASK_SOURCE" ] && [ -z "${AIRGAP_TASK_ARCHIVE:-}" ]; then
    command -v curl >/dev/null 2>&1 || die "curl or a provisioned AIRGAP_TASK_ARCHIVE is required on the connected pack host"
    TASK_SOURCE="$TASK_TMP/$TASK_ASSET"
    curl -fsSL -o "$TASK_SOURCE" "$TASK_ORIGIN"
fi
task_extract_verified "$TASK_SOURCE" "$TASK_TMP"
[ "$TASK_SOURCE" -ef "$DIST/$TASK_ASSET" ] || cp "$TASK_SOURCE" "$DIST/$TASK_ASSET"
cp "$TASK_PIN" "$DIST/task-pin.txt"
cp "$TASK_TMP/LICENSE" "$DIST/task-LICENSE"
rm -f "$DIST"/repo.bundle "$DIST"/reference-docs.tar.gz "$DIST"/qdrant-image.tar \
      "$DIST"/openobserve-image.tar "$DIST"/otelcol-image.tar "$DIST"/app-*.tar \
      "$DIST"/MANIFEST.txt "$DIST"/THIRD-PARTY-NOTICES.txt "$DIST"/SHA256SUMS "$OUT_TARBALL" "$OUT_TARBALL.sha256"
# shellcheck disable=SC2086
rm -f $OAUTH_TAR
rm -f "$DIST/openwebui-image.tar" "$DIST/openwebui-proxy-image.tar"

echo "==> Git bundle of the checked-out commit"
# HEAD must be an explicit ref: without it, `git clone repo.bundle` on the
# air-gap side falls back to a default branch instead of the packed SHA.
git bundle create "$DIST/repo.bundle" HEAD --all
git bundle verify "$DIST/repo.bundle" >/dev/null

echo "==> Reference docs (pinned upstream doc sets + project docs; GH#1174)"
# Every set in reference-docs.json is read from AIRGAP_REFDOCS_CACHE (archives
# named by sha256; no download at all when it is set, like AIRGAP_TASK_ARCHIVE)
# or from .tools/cache/reference-docs, else downloaded from its pinned URL; each
# archive must equal its pinned sha256, and only the kept subset plus its licence
# files is copied. Nothing is built or rendered. The project docs come from the
# packed commit itself.
REFDOCS_CACHE=${AIRGAP_REFDOCS_CACHE:-$REPO_ROOT/.tools/cache/reference-docs}
if [ -n "${AIRGAP_REFDOCS_CACHE:-}" ] && [ ! -d "$AIRGAP_REFDOCS_CACHE" ]; then
    die "AIRGAP_REFDOCS_CACHE is not a directory: $AIRGAP_REFDOCS_CACHE"
fi
git archive --format=tar -o "$TASK_TMP/project-docs.tar" HEAD -- README.md AGENTS.md LICENSE docs \
    ':(glob).agents/skills/qdrant-*/**' .agents/qdrant-skills-provenance.md LICENSE.qdrant-skills NOTICE.qdrant-skills \
    || die "cannot archive the project docs of the packed commit"
python3 "$REPO_ROOT/scripts/airgap/reference_docs.py" build --manifest "$REPO_ROOT/scripts/airgap/reference-docs.json" \
    --project-tar "$TASK_TMP/project-docs.tar" --cache "$REFDOCS_CACHE" ${AIRGAP_REFDOCS_CACHE:+--offline} --work "$TASK_TMP" \
    --source-sha "$IMAGE_SHA" --mtime "$(git log -1 --format=%ct HEAD)" --output "$DIST/reference-docs.tar.gz" \
    || die "reference docs could not be packed: a pinned doc set is missing, differs from its sha256 or holds an unsafe entry (FAIL line above)"
REFDOCS_SHA256=$(sha256sum "$DIST/reference-docs.tar.gz" | awk '{print $1}')
REFDOCS_SETS=$(python3 "$REPO_ROOT/scripts/airgap/reference_docs.py" check --manifest "$REPO_ROOT/scripts/airgap/reference-docs.json" | sed -n 's/.*sets=//p')

echo "==> Pulling images (fail closed on missing tags)"
extra_args="${SKOPEO_ARGS:-}"
if [ "${INSECURE_REGISTRY:-false}" = "true" ]; then
    extra_args="$extra_args --src-tls-verify=false"
fi
# Image members are oci-archive (#1066). --preserve-digests stores the
# registry's platform manifest, config and compressed layer blobs byte-for-byte
# (skopeo fails rather than convert or recompress), so third-party layers keep
# their upstream compression and the app images keep the gzip layers the
# factory pushed. A manifest-list pin still resolves to one platform image.
# shellcheck disable=SC2086
skopeo copy $extra_args --preserve-digests "docker://$QDRANT_REF" "oci-archive:$DIST/qdrant-image.tar"
# shellcheck disable=SC2086
skopeo copy $extra_args --preserve-digests "docker://$OPENOBSERVE_REF" "oci-archive:$DIST/openobserve-image.tar"
# shellcheck disable=SC2086
skopeo copy $extra_args --preserve-digests "docker://$OTELCOL_REF" "oci-archive:$DIST/otelcol-image.tar"
# shellcheck disable=SC2086
skopeo copy $extra_args --preserve-digests "docker://$INGEST_IMAGE" "oci-archive:$DIST/app-ingest-$IMAGE_SHA.tar"
# shellcheck disable=SC2086
skopeo copy $extra_args --preserve-digests "docker://$AGENT_IMAGE" "oci-archive:$DIST/app-agent-$IMAGE_SHA.tar"
if [ -n "$OAUTH_TAR" ]; then
    # Red Hat images carry signature attachments that an OCI archive cannot
    # store. Omit those attachments, retaining the digest-pinned source and
    # our archive-digest + signed-SHA256SUMS handoff contract below.
    # shellcheck disable=SC2086
    skopeo copy $extra_args --remove-signatures --preserve-digests "docker://$OAUTH_PROXY_REF" "oci-archive:$DIST/$OAUTH_TAR"
fi
if [ -n "$OPENWEBUI_TAR" ]; then
    # Same OCI-archive contract as the other members (#1066): keep the
    # registry manifest and compressed layers byte-for-byte.
    # shellcheck disable=SC2086
    skopeo copy $extra_args --preserve-digests "docker://$OPENWEBUI_REF" "oci-archive:$DIST/$OPENWEBUI_TAR"
fi
if [ -n "$OPENWEBUI_PROXY_TAR" ]; then
    # Same OCI-archive contract as the other members (#1066).
    # shellcheck disable=SC2086
    skopeo copy $extra_args --preserve-digests "docker://$OPENWEBUI_PROXY_REF" "oci-archive:$DIST/$OPENWEBUI_PROXY_TAR"
fi

echo "==> Image digests (bound into MANIFEST, verified on load)"
# Archive digests come from one verified read of each OCI layout (index.json ->
# manifest -> config and every layer blob): the platform manifest digest the
# registry serves. A manifest-list pin in images.txt therefore differs from its
# *_digest by construction. MANIFEST keeps both: the ref line carries the
# requested pin, the *_digest line the bundled bytes that load re-verifies.
# Image config digests (issue #272) are what deploy/ingest verify the registry
# against before any cluster change.
IMAGE_MANIFESTS="$TASK_TMP/image-manifests"
mkdir "$IMAGE_MANIFESTS"
INGEST_DIGEST=$(archive_manifest "$DIST/app-ingest-$IMAGE_SHA.tar" "$IMAGE_MANIFESTS/ingest")
AGENT_DIGEST=$(archive_manifest "$DIST/app-agent-$IMAGE_SHA.tar" "$IMAGE_MANIFESTS/agent")
QDRANT_DIGEST=$(archive_manifest "$DIST/qdrant-image.tar" "$IMAGE_MANIFESTS/qdrant")
OPENOBSERVE_DIGEST=$(archive_manifest "$DIST/openobserve-image.tar" "$IMAGE_MANIFESTS/openobserve")
OTELCOL_DIGEST=$(archive_manifest "$DIST/otelcol-image.tar" "$IMAGE_MANIFESTS/otelcol")
OAUTH_DIGEST=""
if [ -n "$OAUTH_TAR" ]; then
    OAUTH_DIGEST=$(archive_manifest "$DIST/$OAUTH_TAR" "$IMAGE_MANIFESTS/oauth_proxy")
fi
OPENWEBUI_DIGEST=""
if [ -n "$OPENWEBUI_TAR" ]; then
    OPENWEBUI_DIGEST=$(archive_manifest "$DIST/$OPENWEBUI_TAR" "$IMAGE_MANIFESTS/openwebui")
fi
OPENWEBUI_PROXY_DIGEST=""
if [ -n "$OPENWEBUI_PROXY_TAR" ]; then
    OPENWEBUI_PROXY_DIGEST=$(archive_manifest "$DIST/$OPENWEBUI_PROXY_TAR" "$IMAGE_MANIFESTS/openwebui_proxy")
fi
INGEST_CONFIG_DIGEST=$(archive_config_digest "$IMAGE_MANIFESTS/ingest")
AGENT_CONFIG_DIGEST=$(archive_config_digest "$IMAGE_MANIFESTS/agent")
QDRANT_CONFIG_DIGEST=$(archive_config_digest "$IMAGE_MANIFESTS/qdrant")
OPENOBSERVE_CONFIG_DIGEST=$(archive_config_digest "$IMAGE_MANIFESTS/openobserve")
OTELCOL_CONFIG_DIGEST=$(archive_config_digest "$IMAGE_MANIFESTS/otelcol")
OPENWEBUI_CONFIG_DIGEST=""
if [ -n "$OPENWEBUI_TAR" ]; then
    OPENWEBUI_CONFIG_DIGEST=$(archive_config_digest "$IMAGE_MANIFESTS/openwebui")
fi
OPENWEBUI_PROXY_CONFIG_DIGEST=""
if [ -n "$OPENWEBUI_PROXY_TAR" ]; then
    OPENWEBUI_PROXY_CONFIG_DIGEST=$(archive_config_digest "$IMAGE_MANIFESTS/openwebui_proxy")
fi
OAUTH_CONFIG_DIGEST=""
if [ -n "$OAUTH_TAR" ]; then
    OAUTH_CONFIG_DIGEST=$(archive_config_digest "$IMAGE_MANIFESTS/oauth_proxy")
fi
UBI_REF=$(pin_from_images_txt python-314-minimal)
UBI_DIGEST=${UBI_REF##*@}

echo "==> MANIFEST"
CHART_VERSION=$(basename charts/qdrant-*.tgz .tgz)
CHART_SHA256=$(sha256sum charts/qdrant-*.tgz | awk '{print $1}')
{
    echo "sha: $IMAGE_SHA"
    echo "task_version: $TASK_VERSION"
    echo "task_platform: linux-amd64"
    echo "task_asset: $TASK_ASSET"
    echo "task_sha256: $TASK_SHA256"
    echo "task_binary_sha256: $TASK_BINARY_SHA256"
    echo "task_license_sha256: $TASK_LICENSE_SHA256"
    echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Signed format declaration (#1066): load refuses any other value, so a
    # bundle never mixes formats and a pre-#1066 docker-archive bundle is
    # refused with a fixed message instead of being half-loaded.
    echo "image_format: oci-archive"
    echo "qdrant: $QDRANT_REF"
    echo "qdrant_digest: $QDRANT_DIGEST"
    echo "qdrant_config_digest: $QDRANT_CONFIG_DIGEST"
    echo "openobserve: $OPENOBSERVE_REF"
    echo "openobserve_digest: $OPENOBSERVE_DIGEST"
    echo "openobserve_config_digest: $OPENOBSERVE_CONFIG_DIGEST"
    echo "otelcol: $OTELCOL_REF"
    echo "otelcol_digest: $OTELCOL_DIGEST"
    echo "otelcol_config_digest: $OTELCOL_CONFIG_DIGEST"
    if [ -n "$OAUTH_TAR" ]; then
        echo "oauth_proxy: $OAUTH_PROXY_REF"
        echo "oauth_proxy_digest: $OAUTH_DIGEST"
        echo "oauth_proxy_config_digest: $OAUTH_CONFIG_DIGEST"
    fi
    if [ -n "$OPENWEBUI_TAR" ]; then
        echo "openwebui: $OPENWEBUI_REF"
        echo "openwebui_digest: $OPENWEBUI_DIGEST"
        echo "openwebui_config_digest: $OPENWEBUI_CONFIG_DIGEST"
    fi
    if [ -n "$OPENWEBUI_PROXY_TAR" ]; then
        echo "openwebui_proxy: $OPENWEBUI_PROXY_REF"
        echo "openwebui_proxy_digest: $OPENWEBUI_PROXY_DIGEST"
        echo "openwebui_proxy_config_digest: $OPENWEBUI_PROXY_CONFIG_DIGEST"
    fi
    echo "chart: $CHART_VERSION"
    echo "chart_sha256: $CHART_SHA256"
    echo "reference_docs: reference-docs.tar.gz"
    echo "reference_docs_sha256: $REFDOCS_SHA256"
    echo "reference_docs_sets: $REFDOCS_SETS"
    echo "ingest: $INGEST_IMAGE"
    echo "ingest_digest: $INGEST_DIGEST"
    echo "ingest_config_digest: $INGEST_CONFIG_DIGEST"
    echo "agent: $AGENT_IMAGE"
    echo "agent_digest: $AGENT_DIGEST"
    echo "agent_config_digest: $AGENT_CONFIG_DIGEST"
    echo "app_registry: $APP_REGISTRY"
    # Honesty label, not a trust root: "true" only when the caller asserts
    # SNEAKERNET_KEY_TRUSTED=true for a production-custody key; throwaway
    # rehearsal keys record "ephemeral". Strength comes from the load-side
    # SNEAKERNET_TRUSTED_PUB check, never from this string.
    if [ "${SNEAKERNET_KEY_TRUSTED:-}" = "true" ]; then
        echo "signed: true"
    else
        echo "signed: ephemeral"
    fi
} > "$DIST/MANIFEST.txt"
cat "$DIST/MANIFEST.txt"

echo "==> SBOM (digest enumeration of every pinned input)"
SBOM_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
export SBOM_DATE IMAGE_SHA QDRANT_REF QDRANT_DIGEST
export OPENOBSERVE_REF OPENOBSERVE_DIGEST OTELCOL_REF OTELCOL_DIGEST
export INGEST_IMAGE INGEST_DIGEST AGENT_IMAGE AGENT_DIGEST UBI_REF UBI_DIGEST
export OAUTH_PROXY_REF OAUTH_DIGEST CHART_VERSION CHART_SHA256 DIST REPO_ROOT
export OPENWEBUI_REF OPENWEBUI_DIGEST OPENWEBUI_PROXY_REF OPENWEBUI_PROXY_DIGEST
export TASK_VERSION TASK_ASSET TASK_SHA256 TASK_BINARY_SHA256 TASK_LICENSE_SHA256 TASK_ORIGIN REFDOCS_SHA256
python3 - <<'PYEOF'
import json
import os

import sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["REPO_ROOT"]) / "scripts"))
from dependency_lock import load, LockError
from image_inventory import inventory, verify_sbom
root = Path(os.environ["REPO_ROOT"])
try:
    _, locked = load(root, "runtime")
    installed = {role: inventory(root, Path(os.environ["DIST"]) /
        f"app-{role}-{os.environ['IMAGE_SHA']}.tar") for role in ("agent", "ingest")}
except (LockError, OSError, ValueError, KeyError):
    raise SystemExit("SBOM reconciliation failed: actual shipped Python inventory is not qualified")
wheels = [{"name": name, **entry} for name, entry in sorted(locked.items())]

sbom = {
    "generated": os.environ["SBOM_DATE"],
    "image_sha": os.environ["IMAGE_SHA"],
    "images": [
        {"name": "qdrant", "ref": os.environ["QDRANT_REF"], "digest": os.environ["QDRANT_DIGEST"]},
        {"name": "openobserve", "ref": os.environ["OPENOBSERVE_REF"], "digest": os.environ["OPENOBSERVE_DIGEST"]},
        {"name": "otelcol", "ref": os.environ["OTELCOL_REF"], "digest": os.environ["OTELCOL_DIGEST"]},
        {"name": "app-ingest", "ref": os.environ["INGEST_IMAGE"], "digest": os.environ["INGEST_DIGEST"]},
        {"name": "app-agent", "ref": os.environ["AGENT_IMAGE"], "digest": os.environ["AGENT_DIGEST"]},
    ],
    "host_tools": [{
        "name": "go-task/task", "version": os.environ["TASK_VERSION"],
        "platform": "linux-amd64", "asset": os.environ["TASK_ASSET"],
        "sha256": os.environ["TASK_SHA256"], "binary_sha256": os.environ["TASK_BINARY_SHA256"],
        "origin": os.environ["TASK_ORIGIN"], "pin": "task-pin.txt",
        "license": "MIT", "license_file": "task-LICENSE",
        "license_sha256": os.environ["TASK_LICENSE_SHA256"],
    }],
    "base_image": {"ref": os.environ["UBI_REF"], "digest": os.environ["UBI_DIGEST"]},
    "chart": os.environ["CHART_VERSION"],
    "chart_sha256": os.environ["CHART_SHA256"],
    "reference_docs": {
        "member": "reference-docs.tar.gz", "sha256": os.environ["REFDOCS_SHA256"],
        "sets": [{key: doc[key] for key in ("id", "component", "version", "source", "url", "sha256", "license")}
                 for doc in json.loads((root / "scripts/airgap/reference-docs.json").read_text(encoding="utf-8"))["sets"]],
    },
    "wheels": wheels,
    "installed_python": installed,
    "inventory_scope": "Locked Python distributions and explicit inherited pip; base OS, chart, JS and knowledge-asset rights/inventories have separate owners. Not a vulnerability or license approval.",
}
if os.environ.get("OAUTH_PROXY_REF"):
    sbom["images"].append(
        {
            "name": "oauth-proxy",
            "ref": os.environ["OAUTH_PROXY_REF"],
            "digest": os.environ["OAUTH_DIGEST"],
        }
    )
if os.environ.get("OPENWEBUI_REF"):
    sbom["images"].append(
        {
            "name": "openwebui",
            "ref": os.environ["OPENWEBUI_REF"],
            "digest": os.environ["OPENWEBUI_DIGEST"],
        }
    )
if os.environ.get("OPENWEBUI_PROXY_REF"):
    sbom["images"].append(
        {
            "name": "openwebui-proxy",
            "ref": os.environ["OPENWEBUI_PROXY_REF"],
            "digest": os.environ["OPENWEBUI_PROXY_DIGEST"],
        }
    )
with open(os.path.join(os.environ["DIST"], "sbom.json"), "w", encoding="utf-8") as fh:
    json.dump(sbom, fh, indent=2, sort_keys=True)
    fh.write("\n")
try:
    verify_sbom(root, Path(os.environ["DIST"]), os.environ["IMAGE_SHA"])
except (LockError, OSError, ValueError, KeyError):
    raise SystemExit("SBOM reconciliation failed: serialized inventory differs from shipped bytes")
PYEOF

echo "==> Third-party notices (license record + notice texts, readable offline)"
python3 "$REPO_ROOT/scripts/license_inventory.py" --root "$REPO_ROOT" notices --bundle-dir "$DIST" --output "$DIST/THIRD-PARTY-NOTICES.txt" \
    || die "third-party notices could not be generated: a required notice material is missing or differs from licenses/inventory.json"

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
    echo "  - Task host tool:      $TASK_VERSION (linux-amd64), $TASK_ASSET"
    echo "  - Task archive SHA256: $TASK_SHA256"
    echo "  - Task pin/license:    task-pin.txt / task-LICENSE (MIT)"
    echo "  - Git Bundle:          repo.bundle (complete git history)"
    echo "  - Qdrant Image:        $QDRANT_REF"
    echo "  - OpenObserve Image:   $OPENOBSERVE_REF"
    echo "  - otelcol-contrib:     $OTELCOL_REF"
    if [ -n "$OAUTH_TAR" ]; then
        echo "  - oauth-proxy Image:   $OAUTH_PROXY_REF"
    fi
    if [ -n "$OPENWEBUI_TAR" ]; then
        echo "  - Open WebUI Image:    $OPENWEBUI_REF"
        echo "  - Open WebUI proxy:    $OPENWEBUI_PROXY_REF"
    fi
    echo "  - Ingest Image:        $INGEST_IMAGE"
    echo "  - Agent Image:         $AGENT_IMAGE"
    echo "  - Helm Chart:          $CHART_VERSION"
    echo "  - Reference docs:      reference-docs.tar.gz ($REFDOCS_SETS upstream doc sets + project docs; INDEX.md inside)"
    echo "  - Sparse Weights:      FastEmbed Qdrant/bm25 (baked in images)"
    echo "  - Third-party notices: THIRD-PARTY-NOTICES.txt ($LICENSE_STATUS)"
    echo "                           Declared licenses only; not a legal approval. Unresolved owner decisions are listed in that file."
    echo "  - Bundle signature:    SHA256SUMS.sig (openssl dgst; pubkey sneakernet-signing.pub)"
    echo "  - Signing key (pub):   $(openssl pkey -in "$SNEAKERNET_SIGNING_KEY" -pubout | openssl sha256)"
    echo "  - Image format:        oci-archive members, registry blobs kept compressed (MANIFEST image_format)"
    echo "  - Digest binding:      MANIFEST *_digest lines are the archives' platform manifest digests;"
    echo "                           qdrant:/openobserve:/otelcol: refs carry the images.txt list pins they were pulled by."
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
    # shellcheck disable=SC2086
    sha256sum bootstrap.sh repo.bundle reference-docs.tar.gz "$TASK_ASSET" task-pin.txt task-LICENSE qdrant-image.tar \
              openobserve-image.tar otelcol-image.tar app-ingest-"$IMAGE_SHA".tar app-agent-"$IMAGE_SHA".tar $OAUTH_TAR $OPENWEBUI_TAR $OPENWEBUI_PROXY_TAR \
              MANIFEST.txt PACKING_RECORD.txt sbom.json THIRD-PARTY-NOTICES.txt sneakernet-signing.pub > SHA256SUMS
)

echo "==> Offline signature (openssl; verified by bootstrap.sh and load.sh)"
# The signature covers SHA256SUMS as written above (it cannot cover its own
# checksum line); a tampered .sig fails verification just the same.
openssl dgst -sha256 -sign "$SNEAKERNET_SIGNING_KEY" \
    -out "$DIST/SHA256SUMS.sig" "$DIST/SHA256SUMS"

echo "==> Tarball + tarball digest"
# shellcheck disable=SC2086
tar -C "$DIST" -cf "$OUT_TARBALL" bootstrap.sh repo.bundle reference-docs.tar.gz "$TASK_ASSET" task-pin.txt task-LICENSE qdrant-image.tar \
    openobserve-image.tar otelcol-image.tar app-ingest-"$IMAGE_SHA".tar app-agent-"$IMAGE_SHA".tar $OAUTH_TAR $OPENWEBUI_TAR $OPENWEBUI_PROXY_TAR MANIFEST.txt PACKING_RECORD.txt sbom.json THIRD-PARTY-NOTICES.txt sneakernet-signing.pub SHA256SUMS SHA256SUMS.sig
# shellcheck disable=SC2016
( cd "$DIST" && sha256sum "$(basename "$OUT_TARBALL")" ) > "$OUT_TARBALL.sha256"

echo ""
echo "Packed: $OUT_TARBALL"
echo "Transfer $OUT_TARBALL and $OUT_TARBALL.sha256 to the air-gap bastion."
echo "On the bastion: sha256sum -c $(basename "$OUT_TARBALL").sha256, then unpack"
echo "and run sh bootstrap.sh."
next_step "sha256sum -c qdrant-pdf-rag-${IMAGE_SHA}.tar.sha256 && tar xf qdrant-pdf-rag-${IMAGE_SHA}.tar && sh bootstrap.sh"
