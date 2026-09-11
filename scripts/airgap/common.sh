#!/bin/sh
# Shared plumbing for scripts/airgap/*.sh (issue #15).
# POSIX sh, set -eu. Sources airgap.env when present, resolves legacy aliases,
# and fail-closes on the product's hard rules. No Python packaging, no npx.

set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$REPO_ROOT"

# Explicit environment wins over the env file. Snapshot every documented
# operator key that is already (non-empty) set, source the file, then restore
# the snapshot over whatever the file assigned — so `VAR=x make airgap-*`
# beats a stale key in airgap.env instead of being silently overridden by it.
# Empty stays unset, matching the ${VAR:-default} idiom used everywhere below.
OPERATOR_ENV_KEYS="AGENT_ROUTE AIRGAP_APP_REGISTRY AIRGAP_BUNDLE_DIR AIRGAP_DRYRUN AIRGAP_WORKSPACE CONTEXTUAL_EMBED_ENABLED CONTEXT_LLM_BASE_URL CONTEXT_LLM_MODEL CORPUS_PVC DENSE_DIM EMBED_BASE_URL EMBED_MODE EMBED_MODEL GATEWAY_API_KEY_SECRET GHCR_OWNER IMAGE_SHA INGEST_EXTRA_PATCH INGEST_TIMEOUT INGEST_WORKERS INGEST_WORK_SIZE INSECURE_REGISTRY INTERNAL_REGISTRY KC LLM_BASE_URL LLM_MODEL_REASONING METRICS_ENABLED NAMESPACE OPENSHIFT_NAMESPACE OTEL_DEPLOYMENT_ENVIRONMENT OTEL_EXPORTER_OTLP_ENDPOINT PULL_SECRET QDRANT_EXTRA_VALUES QDRANT_IMAGE QDRANT_RELEASE QDRANT_STORAGE_SIZE QDRANT_TAG QUERY REGISTRY_INTERNAL RERANK_BASE_URL RERANK_ENABLED RERANK_ENDPOINT_ORDER RERANK_MODEL SKOPEO_ARGS SNAPSHOT_STORAGE_CLASS SNEAKERNET_KEY_TRUSTED SNEAKERNET_SIGNING_KEY SNEAKERNET_TRUSTED_PUB STORAGE_CLASS VLLM_BASE_URL"
_cli_saved_keys=""
for _k in $OPERATOR_ENV_KEYS; do
    eval "_is_set=\${$_k:+set}"
    if [ -n "$_is_set" ]; then
        eval "_cli_saved_${_k}=\${$_k}"
        _cli_saved_keys="${_cli_saved_keys:+$_cli_saved_keys }$_k"
    fi
done
unset _k _is_set

if [ -n "${AIRGAP_ENV:-}" ]; then
    if [ -f "$AIRGAP_ENV" ]; then
        # shellcheck disable=SC1091
        . "$AIRGAP_ENV"
    fi
elif [ -f airgap.env ]; then
    # shellcheck disable=SC1091
    . ./airgap.env
fi

for _k in $_cli_saved_keys; do
    eval "${_k}=\${_cli_saved_${_k}}"
    eval "unset _cli_saved_${_k}"
done
unset _k _cli_saved_keys OPERATOR_ENV_KEYS

die() {
    echo "FAIL: $*" >&2
    exit 1
}

require_env() {
    # Collect every missing key before failing, so one run tells the
    # operator everything to fill in — not one variable per attempt.
    _missing=""
    for key in "$@"; do
        eval "val=\${$key:-}"
        [ -n "$val" ] || _missing="${_missing:+$_missing }$key"
    done
    [ -z "$_missing" ] || die "required variables unset: $_missing (copy airgap.env.example to airgap.env and edit it)"
}

# Product rules that every air-gap step enforces (AGENTS.md).
enforce_product_rules() {
    [ "${EMBED_MODE:-}" != "hash" ] || die "EMBED_MODE=hash is CI/dev only; air-gap uses the in-cluster vLLM endpoint"
    refuse_plaintext_gateway_keys
}

# Gateway virtual keys must arrive via an operator-created Secret
# (GATEWAY_API_KEY_SECRET + secretKeyRef), never as plaintext in the env
# file: the file travels on sneakernet media and lingers on bastions, while
# the new Settings knobs (PR1) would happily send a leaked value as a
# Bearer header. Scans the same file the sourcing above selected; commented
# lines and empty assignments are not keys. Plaintext keys stay usable for
# local `make ask` via process env — they just can never enter manifests.
refuse_plaintext_gateway_keys() {
    _env_file=""
    if [ -n "${AIRGAP_ENV:-}" ]; then
        [ -f "$AIRGAP_ENV" ] && _env_file="$AIRGAP_ENV"
    elif [ -f airgap.env ]; then
        _env_file="airgap.env"
    fi
    if [ -n "$_env_file" ]; then
        _leaked=$(grep -E "^[[:space:]]*(LLM_API_KEY|EMBED_API_KEY|RERANK_API_KEY|CONTEXT_LLM_API_KEY)=[^[:space:]]" "$_env_file" || true)
        [ -z "$_leaked" ] || die "plaintext gateway virtual key in $_env_file — keys live only in the cluster Secret named by GATEWAY_API_KEY_SECRET (see airgap.env.example); delete the plaintext assignment"
    fi
    unset _env_file _leaked
}

# Secret names land inside sed replacements and k8s manifests: restrict to
# the DNS-subdomain charset so neither the render nor the apply can break
# (a sed-active char like & would silently rewrite the manifest).
# $1 = value, $2 = variable name for the error. Empty is allowed (opt-in).
check_secret_name() {
    case "$1" in
        "") ;;
        *[!a-z0-9.-]*) die "$2 must be a DNS-subdomain name (lowercase alphanumerics, '-', '.'), got '$1'" ;;
    esac
}

# Delete gateway key entries from a rendered manifest (used when
# GATEWAY_API_KEY_SECRET is unset). Each entry is exactly five lines
# (`- name:` + valueFrom/secretKeyRef/key/name in either mapping order),
# so awk skips a fixed count anchored on the entry name — never on
# comments (kustomize drops them) and never on inner line order (kustomize
# sorts mapping keys). Entry shape is pinned by the render tests.
# $1 = file, $2... = env entry names.
strip_gateway_key_entries() {
    _strip_file=$1; shift
    for _entry in "$@"; do
        awk -v entry="$_entry" '
            $0 ~ "- name: " entry "$" { skip=5 }
            skip > 0 { skip--; next }
            { print }
        ' "$_strip_file" > "$_strip_file.tmp" && mv "$_strip_file.tmp" "$_strip_file"
    done
    unset _strip_file _entry
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
# The inserted item reuses the matched line's indent: every overlay nests
# `imagePullSecrets: []` inside the pod spec, and a fixed 2-space item breaks
# out of the mapping (kubectl: "did not find expected key" on apply).
# PULL_SECRET is a DNS-subdomain secret name by contract — no sed-active chars.
wire_pull_secret() {
    if [ -n "${PULL_SECRET:-}" ]; then
        sed -E -i "s|^([[:space:]]*)imagePullSecrets: \[\]|\1imagePullSecrets:\n\1  - name: $PULL_SECRET|" "$1"
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
        # Digest-only form: tag+digest combined is not a valid reference
        # (skopeo and docker builds reject it).
        _pin_leaf=${_pin_ref##*/}
        case "$_pin_leaf" in
            *:* ) _pin_ref="${_pin_ref%:*}@${_pin_digest}" ;;
            * ) _pin_ref="${_pin_ref}@${_pin_digest}" ;;
        esac
    fi
    echo "$_pin_ref"
}

# Trust anchor (optional, strict): when SNEAKERNET_TRUSTED_PUB names a
# pubkey file obtained out of band (org-published fingerprint, HTTPS
# Actions artifact), the bundle is refused unless its bundled pub matches
# byte-for-byte. Without it, signature verification is TOFU: it binds the
# members together but cannot prove which key signed. $1 = artifact dir.
# (bootstrap.sh carries an inline twin: it verifies before any clone exists
# to source this file from.)
check_trusted_pub() {
    if [ -n "${SNEAKERNET_TRUSTED_PUB:-}" ]; then
        [ -f "$SNEAKERNET_TRUSTED_PUB" ] || \
            die "SNEAKERNET_TRUSTED_PUB file not found: $SNEAKERNET_TRUSTED_PUB"
        cmp -s "$SNEAKERNET_TRUSTED_PUB" "$1/sneakernet-signing.pub" || \
            die "bundle pubkey does not match SNEAKERNET_TRUSTED_PUB — untrusted bundle"
    fi
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
