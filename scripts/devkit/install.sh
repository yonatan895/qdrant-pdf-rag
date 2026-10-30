#!/bin/sh
# BASTION SIDE (#1118, ADR-0009 §8 "The kit"): verify the dev kit and install
# its image into the site registry. Ships inside the kit as install.sh.
#
#   SNEAKERNET_TRUSTED_PUB=<release-signing.pub> sh install.sh \
#       --registry <registry>/<path> [--verify-pod <pod>] [--registry-ca-dir DIR] [--kit DIR]
#
# Run it only after the Windows trust-anchor check (devkit-windows.md step 1): a
# verifier inside the kit cannot vouch for itself, so these checks are trusted
# only because the anchor fixed SHA256SUMS first. In order, it refuses:
#   1. no SNEAKERNET_TRUSTED_PUB, or one inside the kit (no trust on first use);
#   2. a signature over SHA256SUMS that the trusted key does not verify;
#   3. any member missing or differing from SHA256SUMS, and any file, link or
#      special file SHA256SUMS does not list (the inventory);
#   4. a MANIFEST whose key, image digest, config digest or oras.exe digest
#      differs from the members;
# then copies the OCI layout to <registry>/<path>:<key> with skopeo, using the
# session registry auth (mfr-session-login --registry), reads the manifest back
# by digest and by tag, and with --verify-pod checks that pod's imageID.
# Repeatable: a second run re-verifies and the copy finds every blob present.
set -eu

die() {
    echo "FAIL: $*" >&2
    exit 1
}

KIT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPOSITORY=""
POD=""
CA_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --kit) KIT=$(CDPATH= cd -- "${2:-}" 2>/dev/null && pwd -P) || die "--kit must name the kit directory"; shift 2 ;;
        --registry) REPOSITORY="${2:-}"; shift 2 ;;
        --verify-pod) POD="${2:-}"; [ -n "$POD" ] || die "--verify-pod needs a pod name"; shift 2 ;;
        --registry-ca-dir) CA_DIR="${2:-}"; [ -d "$CA_DIR" ] || die "--registry-ca-dir must name a directory of CA certificates (*.crt)"; shift 2 ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) die "unsupported argument (usage: sh install.sh --registry <registry>/<path> [--verify-pod <pod>] [--registry-ca-dir DIR] [--kit DIR])" ;;
    esac
done
case "$KIT" in
    *:*) die "the kit path contains ':', which skopeo reads as a reference separator: move the kit to a path without ':'" ;;
esac
printf '%s\n' "$REPOSITORY" | grep -Eqx '[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+' ||
    die "--registry must be <registry>/<path> with no scheme, tag or digest (for example quay.example/mf/qdrant-pdf-rag-devkit)"

for _tool in openssl sha256sum python3 skopeo awk find; do
    command -v "$_tool" >/dev/null 2>&1 || die "$_tool is missing: run install.sh inside the bastion (the dev workspace image)"
done
SESSION="${MFR_SESSION_DIR:-/run/mfr-session}/private"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "==> 1. Trusted key"
[ -n "${SNEAKERNET_TRUSTED_PUB:-}" ] ||
    die "SNEAKERNET_TRUSTED_PUB is unset: oc cp the release-signing public key you verified with the release bundle's anchor into the bastion and point SNEAKERNET_TRUSTED_PUB at it (a kit never vouches for itself)"
[ -f "$SNEAKERNET_TRUSTED_PUB" ] || die "SNEAKERNET_TRUSTED_PUB file not found: copy the release-signing public key into the bastion first"
TRUSTED=$(CDPATH= cd -- "$(dirname -- "$SNEAKERNET_TRUSTED_PUB")" && pwd -P)/$(basename -- "$SNEAKERNET_TRUSTED_PUB")
[ ! -L "$SNEAKERNET_TRUSTED_PUB" ] || die "SNEAKERNET_TRUSTED_PUB must be a plain file, not a link"
case "$TRUSTED" in
    "$KIT"/*) die "SNEAKERNET_TRUSTED_PUB points inside the kit: use the release-signing key you hold from the release bundle, not the kit's own copy" ;;
esac

echo "==> 2. Signature over SHA256SUMS"
for _member in SHA256SUMS SHA256SUMS.sig SHA256SUMS.sha256 sneakernet-signing.pub MANIFEST.txt image_manifest.py; do
    [ -f "$KIT/$_member" ] || die "the kit has no $_member: copy the whole signed kit directory again (oc cp it as one directory)"
done
cmp -s "$SNEAKERNET_TRUSTED_PUB" "$KIT/sneakernet-signing.pub" ||
    die "the kit's signing key is not SNEAKERNET_TRUSTED_PUB: this kit was not signed by the release key; do not install it and report it"
openssl dgst -sha256 -verify "$SNEAKERNET_TRUSTED_PUB" -signature "$KIT/SHA256SUMS.sig" "$KIT/SHA256SUMS" >/dev/null 2>&1 ||
    die "the SHA256SUMS signature does not verify with SNEAKERNET_TRUSTED_PUB: do not install this kit; transfer a fresh copy"
[ "$(sha256sum "$KIT/SHA256SUMS" | awk '{print $1}')" = "$(awk 'NR == 1 {print $1}' "$KIT/SHA256SUMS.sha256")" ] ||
    die "SHA256SUMS.sha256 does not match SHA256SUMS: transfer the kit again"

echo "==> 3. Members and inventory"
# Strict list shape: `<64 hex>  <relative path>`, safe names, no duplicates.
awk '
    NF != 2 || length($1) != 64 || $1 !~ /^[0-9a-f]+$/ || substr($0, 65, 2) != "  " { bad = 1 }
    $2 !~ /^[A-Za-z0-9][A-Za-z0-9._-]*(\/[A-Za-z0-9][A-Za-z0-9._-]*)*$/ || $2 ~ /(^|\/)\.\.?(\/|$)/ { bad = 1 }
    seen[$2]++ { bad = 1 }
    END { exit bad || NR == 0 }
' "$KIT/SHA256SUMS" || die "SHA256SUMS is malformed: transfer the kit again"
(cd "$KIT" && sha256sum -c --strict --quiet SHA256SUMS >/dev/null 2>&1) ||
    die "a kit member is missing or differs from SHA256SUMS: transfer the kit again"
[ -z "$(find "$KIT" ! -type f ! -type d | head -n 1)" ] ||
    die "the kit holds a link or special file: copy only the signed kit directory (plain files)"
awk '{print $2}' "$KIT/SHA256SUMS" | LC_ALL=C sort > "$WORK/listed"
(cd "$KIT" && find . -type f) | sed 's#^\./##' |
    grep -vx -e SHA256SUMS -e SHA256SUMS.sig -e SHA256SUMS.sha256 | LC_ALL=C sort > "$WORK/present"
cmp -s "$WORK/listed" "$WORK/present" ||
    die "the kit holds files SHA256SUMS does not list: copy only the signed kit directory, into an empty directory"

echo "==> 4. MANIFEST against the members"
field() { awk -v k="$1:" '$1 == k { n++; v = $2 } END { if (n == 1) print v }' "$KIT/MANIFEST.txt"; }
KEY=$(field key)
DIGEST=$(field image_digest)
printf '%s\n' "$KEY" | grep -Eqx '[0-9a-f]{64}' || die "MANIFEST names no valid key: transfer the kit again"
[ "$(field kit)" = "mfr-devkit-$KEY" ] || die "MANIFEST kit name differs from its key: transfer the kit again"
[ "$(field image_format)" = "oci-layout" ] || die "MANIFEST image_format is not oci-layout: this installer cannot read the kit"
printf '%s\n' "$DIGEST" | grep -Eqx 'sha256:[0-9a-f]{64}' || die "MANIFEST names no valid image_digest: transfer the kit again"
LAYOUT=$(python3 -I "$KIT/image_manifest.py" layout "$KIT/image" "$WORK/manifest.json") ||
    die "the kit image is not one verified OCI image: transfer the kit again"
[ "$LAYOUT" = "$DIGEST" ] || die "the kit image is $LAYOUT, not the MANIFEST image_digest: do not install this kit"
[ "$(python3 -I "$KIT/image_manifest.py" config < "$WORK/manifest.json")" = "$(field image_config_digest)" ] ||
    die "the kit image config differs from the MANIFEST image_config_digest: do not install this kit"
[ "$(sha256sum "$KIT/oras.exe" | awk '{print $1}')" = "$(field oras_sha256)" ] ||
    die "oras.exe differs from the MANIFEST oras_sha256: do not use this kit"
[ "$(sha256sum "$KIT/oras" | awk '{print $1}')" = "$(field oras_linux_sha256)" ] ||
    die "oras differs from the MANIFEST oras_linux_sha256: do not use this kit"

echo "==> 5. Copy to $REPOSITORY:$KEY"
AUTH="$SESSION/auth.json"
[ -f "$AUTH" ] ||
    die "no session registry auth: run oc get secret <robot> -o jsonpath='{.data.\\.dockerconfigjson}' | oc exec -i <bastion> -- mfr-session-login --registry"
set -- --authfile "$AUTH"
[ -z "$CA_DIR" ] || set -- "$@" --cert-dir "$CA_DIR"
COPY_CA=""
[ -z "$CA_DIR" ] || COPY_CA="--dest-cert-dir=$CA_DIR"
# shellcheck disable=SC2086
skopeo copy --preserve-digests --dest-authfile "$AUTH" $COPY_CA "oci:$KIT/image" "docker://$REPOSITORY:$KEY" >/dev/null ||
    die "the copy to $REPOSITORY failed: check the robot's write access, the registry CA (--registry-ca-dir) and that the repository exists"

echo "==> 6. Registry readback"
skopeo inspect --raw "$@" "docker://$REPOSITORY@$DIGEST" > "$WORK/by-digest" ||
    die "the registry does not serve $REPOSITORY@$DIGEST after the copy: check the registry and run install.sh again"
[ "sha256:$(sha256sum "$WORK/by-digest" | awk '{print $1}')" = "$DIGEST" ] ||
    die "the registry serves different manifest bytes for $DIGEST: the registry converted the image; do not use it"
skopeo inspect --raw "$@" "docker://$REPOSITORY:$KEY" > "$WORK/by-tag" ||
    die "the registry does not serve $REPOSITORY:$KEY after the copy: run install.sh again"
[ "sha256:$(sha256sum "$WORK/by-tag" | awk '{print $1}')" = "$DIGEST" ] ||
    die "$REPOSITORY:$KEY points at another digest than this kit's image: the tag was moved; reconcile it with the registry owner, then run install.sh again"
echo "readback: $REPOSITORY:$KEY = $DIGEST"

if [ -n "$POD" ]; then
    echo "==> 7. Pod $POD imageID"
    KC=$(command -v oc || command -v kubectl) || die "oc is missing: run install.sh inside the bastion"
    [ -f "$SESSION/kubeconfig" ] ||
        die "no session kubeconfig: run oc whoami -t | oc exec -i <bastion> -- mfr-session-login"
    "$KC" --kubeconfig "$SESSION/kubeconfig" get pod "$POD" \
        -o 'jsonpath={range .status.containerStatuses[*]}{.imageID}{"\n"}{end}' > "$WORK/image-ids" 2>/dev/null ||
        die "cannot read pod $POD: check the pod name and that your session token is current (mfr-session-login)"
    [ -s "$WORK/image-ids" ] || die "pod $POD reports no running container image yet: wait until it is Running, then run install.sh again"
    awk -v d="$DIGEST" '{ n++; if ($0 !~ ("@" d "$")) bad = 1 } END { exit bad || n == 0 }' "$WORK/image-ids" ||
        die "pod $POD does not run this kit's image $DIGEST: render the bastion with DEVKIT_DIGEST from this MANIFEST, delete the pod and apply again"
    echo "pod: $POD runs $DIGEST"
fi

echo ""
echo "devkit-install: mfr-devkit-$KEY verified and installed as $REPOSITORY:$KEY ($DIGEST)"
