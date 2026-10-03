#!/bin/sh
# Image identity helpers (issue #272). Sourced by pack/load/deploy/ingest after
# common.sh; never executed directly.
#
# Contract: the signed MANIFEST carries each packed image's CONFIG digest
# (<role>_config_digest). The config digest pins the rootfs diffIDs and is
# stable across the archive (uncompressed layers) -> registry (compressed
# layers) conversion, unlike the manifest digest (docs/deploy.md, "Image
# identity across archive and registry formats"). Before any cluster change,
# deploy/ingest read the registry tag back, require the packed config digest and
# a single-image manifest, and render the first-party images as
# repository@<registry manifest digest>. The vendored Qdrant chart cannot take
# a digest (it appends -unprivileged to the tag and semver-compares it), so its
# digest is compared with the running pods after rollout instead.

# `skopeo inspect` takes fewer options than `skopeo copy`. Reuse only the
# registry-access options from SKOPEO_ARGS (authfile, creds, cert-dir,
# tls-verify; a --dest- prefix is dropped) so a read-back reaches the registry
# the same way the push did. Copy-only options are never forwarded.
inspect_args() {
    _ia=""
    _ia_take=0
    # shellcheck disable=SC2086
    for _t in ${SKOPEO_ARGS:-}; do
        if [ "$_ia_take" = 1 ]; then _ia="$_ia $_t"; _ia_take=0; continue; fi
        _n=${_t#--}
        _n=${_n#dest-}
        case "$_t" in
            --*) ;;
            *) continue ;;
        esac
        case "$_n" in
            authfile=*|creds=*|cert-dir=*|tls-verify=*|registry-token=*|no-creds) _ia="$_ia --$_n" ;;
            authfile|creds|cert-dir|registry-token) _ia="$_ia --$_n"; _ia_take=1 ;;
        esac
    done
    if [ "${INSECURE_REGISTRY:-false}" = "true" ]; then
        _ia="$_ia --tls-verify=false"
    fi
    printf '%s' "$_ia"
}

# $1 = docker-archive tar path. Prints the image config digest of the archive.
archive_config_digest() {
    _acd_raw=$(skopeo inspect --raw "docker-archive:$1") || die "cannot read the archive manifest of $(basename "$1")"
    printf '%s' "$_acd_raw" | python3 -c '
import json, re, sys
try:
    digest = json.load(sys.stdin)["config"]["digest"]
except (ValueError, KeyError, TypeError):
    sys.exit(1)
if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    sys.exit(1)
print(digest)
' || die "archive manifest of $(basename "$1") has no usable image config digest"
}

# Release path = a packed MANIFEST is reachable and this is not a dry-run.
# Dry-run and connected development keep tag-only references (stated, never silent).
image_identity_enforced() {
    [ -n "${MANIFEST:-}" ] && [ "${AIRGAP_DRYRUN:-0}" != "1" ]
}

# $1 = role (qdrant|agent|ingest|jaeger|oauth_proxy). Prints the registry ref.
image_role_ref() {
    case "$1" in
        qdrant) echo "$INTERNAL_REGISTRY/qdrant/qdrant:${QDRANT_TAG}-unprivileged" ;;
        agent) echo "$INTERNAL_REGISTRY/qdrant-pdf-rag-agent:$IMAGE_SHA" ;;
        ingest) echo "$INTERNAL_REGISTRY/qdrant-pdf-rag-ingest:$IMAGE_SHA" ;;
        jaeger) echo "$INTERNAL_REGISTRY/jaegertracing/jaeger:v2.20.0" ;;
        oauth_proxy) echo "$INTERNAL_REGISTRY/openshift4/ose-oauth-proxy:v4.14" ;;
        *) die "unknown image role: $1" ;;
    esac
}

# Read the registry tag back, require the packed config digest, export
# IMAGE_DIGEST_<ROLE>=<registry manifest digest>. Fixed messages; no auth values.
verify_registry_role() {
    _vr_role=$1
    _vr_ref=$(image_role_ref "$_vr_role")
    _vr_upper=$(printf '%s' "$_vr_role" | tr 'a-z' 'A-Z')
    _vr_expected=$(awk -F': ' -v k="${_vr_role}_config_digest" '$1 == k {print $2}' "$MANIFEST")
    [ -n "$_vr_expected" ] || die "packed MANIFEST has no ${_vr_role}_config_digest — repack the bundle with this release's pack.sh; the registry image cannot be verified"
    _vr_tmp=$(mktemp) || die "cannot create a temporary file"
    # shellcheck disable=SC2046
    skopeo inspect --raw $(inspect_args) "docker://$_vr_ref" > "$_vr_tmp" || {
        rm -f "$_vr_tmp"
        die "cannot read back registry image $_vr_ref — it is not verified; load the bundle or fix registry access"
    }
    _vr_rc=0
    _vr_digest=$(python3 - "$_vr_tmp" "$_vr_expected" <<'PYEOF'
import hashlib
import json
import sys

raw = open(sys.argv[1], "rb").read()
try:
    manifest = json.loads(raw)
    config = manifest["config"]["digest"]
    manifest["layers"]
except (ValueError, KeyError, TypeError):
    sys.exit(4)
if config != sys.argv[2]:
    sys.exit(5)
print("sha256:" + hashlib.sha256(raw).hexdigest())
PYEOF
    ) || _vr_rc=$?
    rm -f "$_vr_tmp"
    case "$_vr_rc" in
        0) ;;
        4) die "registry image $_vr_ref is not a single-image manifest — a tag must resolve to exactly the packed image" ;;
        *) die "registry image $_vr_ref is not the packed image (config digest differs from the signed MANIFEST) — the tag was swapped or never loaded; nothing was deployed" ;;
    esac
    export "IMAGE_DIGEST_${_vr_upper}=$_vr_digest"
    echo "==> registry image verified: $_vr_ref@$_vr_digest"
}

# $@ = roles to verify before any cluster change.
verify_registry_images() {
    # Digests only ever come from this verification, never from the caller's environment.
    unset IMAGE_DIGEST_QDRANT IMAGE_DIGEST_AGENT IMAGE_DIGEST_INGEST IMAGE_DIGEST_JAEGER IMAGE_DIGEST_OAUTH_PROXY
    if ! image_identity_enforced; then
        echo "Notice: images are referenced by tag and not release-verified (dry-run or no packed MANIFEST)" >&2
        return 0
    fi
    command -v skopeo >/dev/null 2>&1 || die "skopeo is required to verify registry image identity"
    command -v python3 >/dev/null 2>&1 || die "python3 is required to verify registry image identity"
    for _vi_role in "$@"; do
        verify_registry_role "$_vi_role"
    done
}

# After rollout: every running container of a verified image must report the
# verified digest as its imageID. $@ = roles (same set as verify_registry_images).
verify_running_images() {
    image_identity_enforced || return 0
    _vp_pairs=""
    for _vp_role in "$@"; do
        _vp_upper=$(printf '%s' "$_vp_role" | tr 'a-z' 'A-Z')
        eval "_vp_digest=\${IMAGE_DIGEST_${_vp_upper}:-}"
        [ -n "$_vp_digest" ] || die "internal: no verified digest for $_vp_role"
        _vp_ref=$(image_role_ref "$_vp_role")
        _vp_pairs="$_vp_pairs ${_vp_ref%:*}=$_vp_digest"
    done
    _vp_tmp=$(mktemp) || die "cannot create a temporary file"
    $KC -n "$NAMESPACE" get pods -o json > "$_vp_tmp" || { rm -f "$_vp_tmp"; die "cannot read pods to verify the running image identity"; }
    _vp_rc=0
    # shellcheck disable=SC2086
    python3 scripts/airgap/check_pod_images.py $_vp_pairs < "$_vp_tmp" || _vp_rc=$?
    rm -f "$_vp_tmp"
    [ "$_vp_rc" -eq 0 ] || die "running pods do not use the verified registry images"
}
