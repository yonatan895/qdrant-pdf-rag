#!/bin/sh
# Shared plumbing for scripts/airgap/*.sh (issue #15).
# POSIX sh, set -eu. Sources airgap.env when present, resolves legacy aliases,
# and fail-closes on the product's hard rules. No Python packaging, no npx.

set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$REPO_ROOT"

if [ -n "${AIRGAP_ENV:-}" ]; then
    if [ -f "$AIRGAP_ENV" ]; then
        # shellcheck disable=SC1091
        . "$AIRGAP_ENV"
    fi
elif [ -f airgap.env ]; then
    # shellcheck disable=SC1091
    . ./airgap.env
fi

die() {
    echo "FAIL: $*" >&2
    exit 1
}

require_env() {
    for key in "$@"; do
        eval "val=\${$key:-}"
        [ -n "$val" ] || die "required variable $key is unset (copy airgap.env.example to airgap.env and edit it)"
    done
}

# Product rules that every air-gap step enforces (AGENTS.md).
enforce_product_rules() {
    [ "${EMBED_MODE:-}" != "hash" ] || die "EMBED_MODE=hash is CI/dev only; air-gap uses the in-cluster vLLM endpoint"
}

resolve_aliases() {
    INTERNAL_REGISTRY=${INTERNAL_REGISTRY:-${REGISTRY_INTERNAL:-}}
    NAMESPACE=${NAMESPACE:-${OPENSHIFT_NAMESPACE:-mainframe-rag}}
    QDRANT_RELEASE=${QDRANT_RELEASE:-qdrant}
    IMAGE_SHA=${IMAGE_SHA:-}
    if [ -z "$IMAGE_SHA" ]; then
        if git rev-parse HEAD >/dev/null 2>&1; then
            IMAGE_SHA=$(git rev-parse HEAD)  # full SHA: must equal the GHCR tag
        fi
    fi
    EMBED_BASE_URL=${EMBED_BASE_URL:-${VLLM_BASE_URL:+$(echo "$VLLM_BASE_URL" | sed -E 's:(/v1)?/*$::')/v1}}
}

# Qdrant data scratch and snapshots live on block storage; NFS is refused.
refuse_nfs_storage() {
    case "${STORAGE_CLASS:-}" in
        *[Nn][Ff][Ss]*)
            die "STORAGE_CLASS='${STORAGE_CLASS}' looks like NFS — Qdrant-adjacent volumes require RWO block storage"
            ;;
    esac
}

# kubectl preferred, oc fallback — same choice in deploy/ingest/validate/smoke.
kc() {
    if command -v kubectl >/dev/null 2>&1; then echo kubectl; else echo oc; fi
}

# Packed MANIFEST lookup: dist/ (bootstrap copy) or ../ (unpack next to clone).
find_manifest() {
    if [ -f dist/MANIFEST.txt ]; then
        echo "dist/MANIFEST.txt"
    elif [ -f ../MANIFEST.txt ]; then
        echo "../MANIFEST.txt"
    fi
}

# Strict IMAGE_SHA cross-check for deploy/ingest (silent on success, skipped
# in dry-run). Uses $MANIFEST + $IMAGE_SHA. Validate.sh keeps its own
# notice/verbose variant, load.sh checks $ARTDIR/MANIFEST.txt instead.
check_manifest_sha() {
    if [ -n "${MANIFEST:-}" ] && [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
        packed_sha=$(awk '/^sha: /{print $2}' "$MANIFEST")
        [ "$IMAGE_SHA" = "$packed_sha" ] || \
            die "IMAGE_SHA=$IMAGE_SHA does not match the packed MANIFEST sha ($packed_sha) — wrong SHA for this sneakernet bundle"
    fi
}

# oc/kubectl must exist unless previewing (deploy/ingest only; validate and
# load enforce their own tool rules).
require_kc() {
    [ "${AIRGAP_DRYRUN:-0}" = "1" ] || command -v oc >/dev/null 2>&1 || command -v kubectl >/dev/null 2>&1 || die "oc or kubectl is required on the air-gap bastion (or set AIRGAP_DRYRUN=1 to preview)"
}

# Render a kustomize overlay: standalone kustomize when present, else the
# kubectl/oc built-in. Callers pipe through sed placeholder substitution.
kustomize_render() {
    if command -v kustomize >/dev/null 2>&1; then
        kustomize build "$1"
    else
        ${KC:-$(kc)} kustomize "$1"
    fi
}

# Wire PULL_SECRET into a rendered manifest (no-op when unset).
wire_pull_secret() {
    if [ -n "${PULL_SECRET:-}" ]; then
        sed -i "s|imagePullSecrets: \[\]|imagePullSecrets:\n  - name: $PULL_SECRET|" "$1"
    fi
}

# Fail closed on leftover __PLACEHOLDER__s. $1 = file, $2 = label for the message.
fail_on_placeholders() {
    if grep -Eq "__[A-Z][A-Z0-9_]*__" "$1"; then
        die "unsubstituted placeholder left in rendered $2 manifest (check airgap.env)"
    fi
}

# Third-party image pin from images.txt (name column); digest applies when
# recorded. $1 = needle (e.g. qdrant, jaeger). Used by pack.sh only.
pin_from_images_txt() {
    _pin_needle=$1
    _pin_ref=""
    _pin_digest=""
    while IFS= read -r line; do
        case "$line" in
            \#*|"") continue ;;
            *"$_pin_needle"*)
                _pin_ref=$(echo "$line" | awk '{print $1}')
                _pin_d=$(echo "$line" | awk '{print $2}')
                [ "$_pin_d" != "sha256:PENDING" ] && _pin_digest=$_pin_d
                break
                ;;
        esac
    done < images.txt
    [ -n "$_pin_ref" ] || die "no $_pin_needle image pin found in images.txt"
    if [ -n "$_pin_digest" ]; then
        _pin_ref="${_pin_ref%@*}@${_pin_digest}"
    fi
    echo "$_pin_ref"
}

# Run a command, or only print it under AIRGAP_DRYRUN=1.
run() {
    if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
        echo "[dryrun] $*"
    else
        "$@"
    fi
}

next_step() {
    echo ""
    echo "Next: $*"
}
