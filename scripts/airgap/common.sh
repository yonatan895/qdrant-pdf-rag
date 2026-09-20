#!/bin/sh
# Shared plumbing for scripts/airgap/*.sh (issue #15).
# POSIX sh, set -eu. Sources airgap.env when present, resolves legacy aliases,
# and fail-closes on the product's hard rules. No Python packaging, no npx.

set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$REPO_ROOT"

# Explicit environment wins over the env file. Snapshot every documented
# operator key that is already (non-empty) set, source the file, then restore
# the snapshot over whatever the file assigned — so `VAR=x sh scripts/tools/run-task.sh airgap:load`
# beats a stale key in airgap.env instead of being silently overridden by it.
# Empty stays unset, matching the ${VAR:-default} idiom used everywhere below.
OPERATOR_ENV_KEYS="AGENT_ROUTE AIRGAP_APP_REGISTRY AIRGAP_BUNDLE_DIR AIRGAP_DRYRUN AIRGAP_ENV AIRGAP_WORKSPACE CONTEXTUAL_EMBED_ENABLED CONTEXT_LLM_BASE_URL CONTEXT_LLM_MODEL CORPUS_PVC DENSE_DIM EMBED_BASE_URL EMBED_MODE EMBED_MODEL EMBED_MODEL_REVISION GATEWAY_API_KEY_SECRET GATEWAY_CA_CONFIGMAP GHCR_OWNER IMAGE_SHA INGEST_ALIAS_PUBLISH INGEST_EXTRA_PATCH INGEST_REINGEST INGEST_RETIRE_DOCS INGEST_TIMEOUT INGEST_WORKERS INGEST_WORK_SIZE INSECURE_REGISTRY INTERNAL_REGISTRY JAEGER_QUERY_URL KC LLM_BASE_URL LLM_MODEL_REASONING METRICS_ENABLED NAMESPACE OPENSHIFT_NAMESPACE OTEL_DEPLOYMENT_ENVIRONMENT OTEL_EXPORTER_OTLP_ENDPOINT OTEL_SERVICE_NAME PULL_SECRET QDRANT_EXTRA_VALUES QDRANT_IMAGE QDRANT_RELEASE QDRANT_REPLICATION_FACTOR QDRANT_SHARD_NUMBER QDRANT_STORAGE_SIZE QDRANT_TAG QDRANT_WRITE_CONSISTENCY_FACTOR QUERY REGISTRY_INTERNAL RERANK_BASE_URL RERANK_ENABLED RERANK_ENDPOINT_ORDER RERANK_MODEL SKOPEO_ARGS SNAPSHOT_STORAGE_CLASS SNEAKERNET_KEY_TRUSTED SNEAKERNET_SIGNING_KEY SNEAKERNET_TRUSTED_PUB AIRGAP_TASK_ARCHIVE STORAGE_CLASS VLLM_BASE_URL"
_cli_saved_keys=""
for _k in $OPERATOR_ENV_KEYS; do
    eval "_is_set=\${$_k:+set}"
    if [ -n "$_is_set" ]; then
        eval "_cli_saved_${_k}=\${$_k}"
        _cli_saved_keys="${_cli_saved_keys:+$_cli_saved_keys }$_k"
    fi
done
unset _k _is_set

# Production collection distribution preset (issue #360): the checked-in
# target topology (6 shards / RF 3 / W 2; overlays/openshift/collection-policy.env).
# Lowest precedence by construction — it loads before the operator env file,
# while explicit caller values were snapshotted above and are restored after.
# A tree without the fragment (hermetic fixtures, one-node lanes) must select
# its own complete policy explicitly.
if [ -f overlays/openshift/collection-policy.env ]; then
    # shellcheck disable=SC1091
    . overlays/openshift/collection-policy.env
fi

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

# Issue #391 F1: vllm embedding mode refuses a blank attestation at ingest
# preflight and agent startup, so whitespace-only values fail here (before
# any cluster mutation) with the same rule for validate/deploy/ingest.
require_embed_revision() {
    case "${EMBED_MODEL_REVISION:-}" in
        *[![:space:]]*) ;;
        *) die "EMBED_MODEL_REVISION must be a non-blank immutable model/config revision for ${EMBED_MODEL:-?} (a gateway alias is mutable and a dimension is not an identity)" ;;
    esac
}

# Collection distribution policy (issue #360): one complete tuple or a
# fail-closed refusal. Positive integers only; a write-consistency factor
# above the replication factor can never be satisfied. $1 = "required"
# (the air-gap mutation path demands a complete policy) or "optional"
# (read-only callers may run without a selected policy).
validate_collection_policy() {
    _mode=${1:-required}
    _missing=""
    for _key in QDRANT_SHARD_NUMBER QDRANT_REPLICATION_FACTOR QDRANT_WRITE_CONSISTENCY_FACTOR; do
        eval "_val=\${$_key:-}"
        if [ -z "$_val" ]; then
            _missing="${_missing:+$_missing }$_key"
            continue
        fi
        case "$_val" in
            *[!0-9]*) die "$_key must be a positive integer (got '$_val')" ;;
        esac
        if [ "$_val" -lt 1 ] 2>/dev/null; then
            die "$_key must be a positive integer (got '$_val')"
        fi
    done
    if [ -n "$_missing" ]; then
        if [ "$_mode" = "optional" ]; then
            unset _key _val _missing
            return 0
        fi
        die "collection distribution policy is incomplete (missing: $_missing) — select all three of QDRANT_SHARD_NUMBER/QDRANT_REPLICATION_FACTOR/QDRANT_WRITE_CONSISTENCY_FACTOR (production preset: 6/3/2 in overlays/openshift/collection-policy.env; one-node rehearsal: 1/1/1 explicitly); see airgap.env.example"
    fi
    if [ "${QDRANT_WRITE_CONSISTENCY_FACTOR}" -gt "${QDRANT_REPLICATION_FACTOR}" ]; then
        die "QDRANT_WRITE_CONSISTENCY_FACTOR=${QDRANT_WRITE_CONSISTENCY_FACTOR} exceeds QDRANT_REPLICATION_FACTOR=${QDRANT_REPLICATION_FACTOR}: writes could never acknowledge"
    fi
    unset _key _val _missing
}

# Strict boolean parser for operator flags (issue #391 maintenance modes):
# unset/empty keeps the default, explicit true/1/yes and false/0/no are
# accepted, anything else fails closed before a manifest is rendered.
# Prints true|false; called in a substitution, so a die() aborts the caller
# under set -e. $1 = variable name, $2 = default (true|false).
bool_flag() {
    eval "_raw=\${$1:-}"
    case "$_raw" in
        "") _val=$2 ;;
        [Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]) _val=true ;;
        [Ff][Aa][Ll][Ss][Ee]|0|[Nn][Oo]) _val=false ;;
        *) die "$1 must be true/false (got '$_raw')" ;;
    esac
    unset _raw
    printf '%s' "$_val"
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
# local `sh scripts/tools/run-task.sh local:ask` via process env — they just can never enter manifests.
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

# Tracing mode: one rule for deploy / validate / smoke / ingest (issue #83).
# Unset or empty means the in-cluster Jaeger default (tracing ON); an explicit
# off sentinel disables; an http(s) URL is a custom collector; anything else
# fails closed before a manifest is rendered. Sets OTEL_ENDPOINT_RESOLVED
# (empty = disabled) and OTEL_TRACING_ENABLED (1/0). One helper so the four
# call sites can never disagree on what "active" means.
resolve_otel_endpoint() {
    _raw="${OTEL_EXPORTER_OTLP_ENDPOINT:-}"
    case "$_raw" in
        "")
            OTEL_ENDPOINT_RESOLVED="http://jaeger:4318"
            OTEL_TRACING_ENABLED=1
            ;;
        [Oo][Ff][Ff]|[Nn][Oo][Nn][Ee]|[Ff][Aa][Ll][Ss][Ee]|0)
            OTEL_ENDPOINT_RESOLVED=""
            OTEL_TRACING_ENABLED=0
            ;;
        http://*|https://*)
            OTEL_ENDPOINT_RESOLVED="$_raw"
            OTEL_TRACING_ENABLED=1
            ;;
        *)
            die "OTEL_EXPORTER_OTLP_ENDPOINT must be http(s) or off/none/false/0, got '$_raw'"
            ;;
    esac
    unset _raw
    export OTEL_ENDPOINT_RESOLVED OTEL_TRACING_ENABLED
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

# Delete a plain two-line env entry (`- name: X` + `value: ...`) from a
# rendered manifest (used for optional entries whose unset state must leave
# no trace — a blank value would override an in-code default, as with
# OTEL_SERVICE_NAME vs the agent's DEFAULT_SERVICE_NAME in tracing.py).
# $1 = file, $2... = env entry names.
strip_env_entry() {
    _strip_file=$1; shift
    for _entry in "$@"; do
        awk -v entry="$_entry" '
            $0 ~ "- name: " entry "$" { skip=2 }
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

# Issue #366: the serving agent is read-only. Its rendered QDRANT_API_KEY
# must reference the chart's `read-only-api-key` data key — never the
# full-access `api-key`. Secret names only; values are never printed.
# $1 = rendered agent manifest, $2 = label for the message.
check_agent_qdrant_key() {
    _qdrant_block=$(grep -A8 -- "- name: QDRANT_API_KEY" "$1" || true)
    if ! printf '%s\n' "$_qdrant_block" | grep -Eq '^[[:space:]]*key: read-only-api-key$'; then
        unset _qdrant_block
        die "rendered $2 manifest must wire QDRANT_API_KEY to secretKeyRef key read-only-api-key (issue #366)"
    fi
    if printf '%s\n' "$_qdrant_block" | grep -Eq '^[[:space:]]*key: api-key$'; then
        unset _qdrant_block
        die "rendered $2 manifest wires QDRANT_API_KEY to the full-access key (issue #366: serving must use read-only-api-key)"
    fi
    unset _qdrant_block
}

# Issue #366 mirror: ingestion owns corpus mutation, so the rendered ingest
# Job must keep the full-access `api-key` data key. $1 = file, $2 = label.
check_ingest_qdrant_key() {
    _qdrant_block=$(grep -A8 -- "- name: QDRANT_API_KEY" "$1" || true)
    if ! printf '%s\n' "$_qdrant_block" | grep -Eq '^[[:space:]]*key: api-key$'; then
        unset _qdrant_block
        die "rendered $2 manifest must wire QDRANT_API_KEY to secretKeyRef key api-key (ingest owns corpus mutation)"
    fi
    if printf '%s\n' "$_qdrant_block" | grep -Eq '^[[:space:]]*key: read-only-api-key$'; then
        unset _qdrant_block
        die "rendered $2 manifest wires a read-only Qdrant key into the ingest path (issue #366: ingest must keep api-key)"
    fi
    unset _qdrant_block
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

# True (0) only when images.txt records a real digest for the needle; the
# documented sha256:PENDING placeholder must never be treated as pinned.
pin_recorded() {
    _pin_needle=$1
    while IFS= read -r line; do
        case "$line" in
            \#*|"") continue ;;
            *"$_pin_needle"*)
                _pin_d=$(echo "$line" | awk '{print $2}')
                case "$_pin_d" in
                    ""|sha256:PENDING) return 1 ;;
                    *) return 0 ;;
                esac
                ;;
        esac
    done < images.txt
    return 1
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

# Optional complete gateway trust bundle. A targeted kustomize patch preserves
# Services/ServiceAccounts/OAuth sidecars in multi-document agent renders.
wire_gateway_ca() (
    [ -n "${GATEWAY_CA_CONFIGMAP:-}" ] || exit 0
    check_secret_name "$GATEWAY_CA_CONFIGMAP" GATEWAY_CA_CONFIGMAP
    _ca_file="$1"; _ca_kind="$2"; _ca_name="$3"; _ca_container="$4"
    _ca_tmp="$(mktemp -d)"
    trap 'rm -rf "$_ca_tmp"' EXIT HUP INT TERM
    cp "$_ca_file" "$_ca_tmp/resources.yaml"
    cat > "$_ca_tmp/kustomization.yaml" <<EOF_CA
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources: [resources.yaml]
patches:
  - target:
      kind: $_ca_kind
      name: $_ca_name
    patch: |-
      apiVersion: apps/v1
      kind: $_ca_kind
      metadata:
        name: $_ca_name
      spec:
        template:
          spec:
            containers:
              - name: $_ca_container
                env:
                  - name: SSL_CERT_FILE
                    value: /etc/gateway-ca/ca-bundle.crt
                volumeMounts:
                  - name: gateway-ca
                    mountPath: /etc/gateway-ca
                    readOnly: true
            volumes:
              - name: gateway-ca
                configMap:
                  name: $GATEWAY_CA_CONFIGMAP
                  items:
                    - key: ca-bundle.crt
                      path: ca-bundle.crt
EOF_CA
    if [ "$_ca_kind" = "Job" ]; then
        sed -i 's|apiVersion: apps/v1|apiVersion: batch/v1|' "$_ca_tmp/kustomization.yaml"
    fi
    kustomize_render "$_ca_tmp" > "$_ca_tmp/rendered.yaml"
    [ -s "$_ca_tmp/rendered.yaml" ] || die "gateway CA render produced empty output"
    cp "$_ca_tmp/rendered.yaml" "$_ca_file"
)

check_gateway_ca() {
    [ -n "${GATEWAY_CA_CONFIGMAP:-}" ] || return 0
    check_secret_name "$GATEWAY_CA_CONFIGMAP" GATEWAY_CA_CONFIGMAP
    [ "${AIRGAP_DRYRUN:-0}" != "1" ] || return 0
    _ca_bundle="$("$KC" -n "$NAMESPACE" get configmap "$GATEWAY_CA_CONFIGMAP" \
        -o 'jsonpath={.data.ca-bundle\.crt}')" || die "gateway CA ConfigMap is unavailable"
    [ -n "$_ca_bundle" ] || die "gateway CA ConfigMap must contain nonempty ca-bundle.crt"
    unset _ca_bundle
}

# One generated-values boundary for deployment and explicit ingestion. The
# caller has resolved/validated airgap.env; only declared non-secret inputs
# are exported to the mapper, never shell-templated into Kubernetes YAML.
map_app_values() {
    command -v python3 >/dev/null 2>&1 || die "python3 is required for Helm release values"
    command -v helm >/dev/null 2>&1 || die "helm is required on the air-gap bastion"
    _helm_version=$(helm version --short) || die "cannot determine Helm version"
    case "$_helm_version" in
        v4.*) ;;
        *) die "Helm 4 is required; use the checksum-pinned 4.3.0 client" ;;
    esac
    export INTERNAL_REGISTRY NAMESPACE QDRANT_RELEASE IMAGE_SHA EMBED_BASE_URL VLLM_BASE_URL EMBED_MODEL DENSE_DIM EMBED_MODEL_REVISION LLM_BASE_URL LLM_MODEL_REASONING RERANK_ENABLED RERANK_BASE_URL RERANK_MODEL RERANK_ENDPOINT_ORDER GATEWAY_API_KEY_SECRET GATEWAY_CA_CONFIGMAP PULL_SECRET OTEL_EXPORTER_OTLP_ENDPOINT OTEL_ENDPOINT_RESOLVED OTEL_TRACING_ENABLED OTEL_DEPLOYMENT_ENVIRONMENT OTEL_SERVICE_NAME METRICS_ENABLED AGENT_ROUTE ROUTE_DESTINATION_CA_FILE STORAGE_CLASS CORPUS_PVC INGEST_WORKERS INGEST_ALIAS_PUBLISH INGEST_REINGEST INGEST_RETIRE_DOCS CONTEXTUAL_EMBED_ENABLED CONTEXT_LLM_BASE_URL CONTEXT_LLM_MODEL QDRANT_SHARD_NUMBER QDRANT_REPLICATION_FACTOR QDRANT_WRITE_CONSISTENCY_FACTOR INGEST_WORK_SIZE
    if [ -n "${GATEWAY_API_KEY_SECRET:-}" ]; then
        echo "==> Gateway keys wired via Secret references"
    else
        echo "==> Gateway keys off: keyless model endpoints"
    fi
    python3 scripts/airgap/map_values.py "$@"
}

# Check required referenced keys without emitting their values. Selected
# manifests reference all per-container keys, including dormant model legs.
require_secret_keys() {
    [ "${AIRGAP_DRYRUN:-0}" != "1" ] || return 0
    _secret_name=$1
    shift
    [ -n "$_secret_name" ] || return 0
    for _secret_key in "$@"; do
        _key_present=$($KC -n "$NAMESPACE" get secret "$_secret_name" \
            -o "go-template={{if index .data \"$_secret_key\"}}present{{end}}") || die "required Secret cannot be read"
        [ "$_key_present" = present ] || die "required Secret key is missing or empty: $_secret_key"
    done
}
