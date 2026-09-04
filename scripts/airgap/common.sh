#!/bin/sh
# Shared plumbing for scripts/airgap/*.sh (issue #15).
# POSIX sh, set -eu. Sources airgap.env when present, resolves legacy aliases,
# and fail-closes on the product's hard rules. No Python packaging, no npx.

set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$REPO_ROOT"

if [ -f airgap.env ]; then
    # shellcheck disable=SC1091
    . ./airgap.env
elif [ -f "${AIRGAP_ENV:-/nonexistent}" ]; then
    # shellcheck disable=SC1091
    . "$AIRGAP_ENV"
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
