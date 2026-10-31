#!/bin/sh
# CONNECTED SIDE (#1118, ADR-0009 §8 "The kit"): pack the signed dev kit.
#
#   SNEAKERNET_SIGNING_KEY=<pem> sh scripts/devkit/pack.sh [--reuse-image DIR] [--license-rehearsal]
#
# Output: dist/mfr-devkit-<key>/, a directory of plain files. The transfer rule
# forbids top-level archives and links, so the kit is never a tarball: the image
# is an OCI layout directory (blobs/sha256/<hex>, NTFS-safe names), and every
# other member is a regular file.
#
#   image/                 the pinned dev workspace image (oras.exe and skopeo read it)
#   oras.exe, oras-LICENSE the pinned Windows ORAS (scripts/tools/oras-windows-pin.txt)
#   oras                   the pinned static Linux ORAS (oras-pin.txt), for the runbook fallback
#   task-LICENSE           the licence of the image's pinned Task (task-pin.txt license-sha256)
#   bastion.template.yaml  deploy/workspace/bastion.template.yaml
#   devkit-windows.md      docs/runbooks/devkit-windows.md
#   install.sh, image_manifest.py   the bastion verifier/installer and its OCI reader
#   issues.json            the offline issue snapshot (scripts/devkit/snapshot-issues.txt)
#   MANIFEST.txt, sbom.json, THIRD-PARTY-NOTICES.txt, sneakernet-signing.pub
#   SHA256SUMS, SHA256SUMS.sig   every member above, signed like the release bundle
#   SHA256SUMS.sha256      the fingerprint the Windows runbook checks with Get-FileHash;
#                          its first 16 hex are the trust anchor published separately (B2)
#
# Refuses a stale or unpublished kit pin (D-8, devkit_key.py --check), and runs the kit licence
# gate (license_inventory.py check --kit, docs/licensing.md#dev-kit) before any pull:
# an unrecorded kit component, an open owner decision or an absent or stale kit
# approval refuses the pack, whatever the key (MANIFEST license_gate: enforced). The
# one exception, --license-rehearsal, needs CI_REHEARSAL=1 and no custodied key
# (SNEAKERNET_KEY_TRUSTED): it records the open items and license_gate: rehearsal,
# and install.sh refuses such a kit without --rehearsal. --reuse-image takes the image/
# directory of an earlier kit with the same key instead of pulling: the snapshot-only
# re-sign (D-9). Its digest must still equal the pin.

. "$(dirname -- "$0")/../airgap/common.sh"

REUSE_IMAGE=""
LICENSE_GATE=enforced
while [ $# -gt 0 ]; do
    case "$1" in
        --reuse-image) REUSE_IMAGE="${2:-}"; [ -n "$REUSE_IMAGE" ] || die "--reuse-image needs the image/ directory of an earlier kit"; shift 2 ;;
        --license-rehearsal) LICENSE_GATE=rehearsal; shift ;;
        -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
        *) die "unsupported argument (usage: sh scripts/devkit/pack.sh [--reuse-image DIR] [--license-rehearsal])" ;;
    esac
done

for _tool in openssl python3 git sha256sum; do
    command -v "$_tool" >/dev/null 2>&1 || die "$_tool is required on the connected pack host"
done
[ -n "$REUSE_IMAGE" ] || command -v skopeo >/dev/null 2>&1 || die "skopeo is required to pull the dev workspace image"
[ -n "${SNEAKERNET_SIGNING_KEY:-}" ] || die "SNEAKERNET_SIGNING_KEY is unset (path to the PEM release signing key; a rehearsal generates a throwaway pair)"
[ -f "$SNEAKERNET_SIGNING_KEY" ] || die "SNEAKERNET_SIGNING_KEY file not found: $SNEAKERNET_SIGNING_KEY"
openssl pkey -in "$SNEAKERNET_SIGNING_KEY" -noout >/dev/null 2>&1 || die "SNEAKERNET_SIGNING_KEY is not a valid PEM private key (store real newlines, not backslash-escaped ones)"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "run from a git clone of the repository"

# MANIFEST records the commit, so it must describe exactly that tree.
check_pack_tree_clean
COMMIT=$(git rev-parse HEAD)

# D-8: a stale key or an unpublished (sha256:PENDING) digest is refused here, by
# devkit_key.py --check, never by ordinary CI.
python3 "$REPO_ROOT/scripts/devkit_key.py" --check >/dev/null ||
    die "the dev kit pin is stale or unpublished (devkit_key.py --check above names the fix); a kit is packed only for the pinned, published key"
KEY=$(sed -n 's/^key: *//p' "$REPO_ROOT/scripts/tools/devkit-pin.txt")
DIGEST=$(sed -n 's/^digest: *//p' "$REPO_ROOT/scripts/tools/devkit-pin.txt")
printf '%s\n' "$DIGEST" | grep -Eqx 'sha256:[0-9a-f]{64}' || die "scripts/tools/devkit-pin.txt digest is malformed"

# The kit's licence gate (docs/licensing.md#dev-kit): the bundle's check plus the
# kit scope. A kit component (dev wheel, RPM, tool pin, base image) without a
# record, an open owner decision, or an absent or stale kit approval refuses the
# pack before anything is pulled. Its summary line goes into MANIFEST.
# The licence rehearsal is an explicit CI exception (the devkit-proof transport
# proof): never with a custodied key, and never without the CI_REHEARSAL marker.
if [ "$LICENSE_GATE" = rehearsal ]; then
    [ "${CI_REHEARSAL:-}" = 1 ] ||
        die "--license-rehearsal needs CI_REHEARSAL=1: it packs a CI rehearsal kit that install.sh refuses; pack a kit for the gap without it (docs/licensing.md#dev-kit)"
    [ "${SNEAKERNET_KEY_TRUSTED:-}" != true ] ||
        die "--license-rehearsal cannot be combined with SNEAKERNET_KEY_TRUSTED=true: a custodied-key kit always enforces the licence gate"
fi
# shellcheck disable=SC2046
LICENSE_SUMMARY=$(python3 "$REPO_ROOT/scripts/license_inventory.py" --root "$REPO_ROOT" check --kit \
    $([ "$LICENSE_GATE" = rehearsal ] && echo --rehearsal)) || {
    printf '%s\n' "$LICENSE_SUMMARY" >&2
    die "the dev kit licence check failed (its FAIL lines above name each item): record every kit component in the devkit section of licenses/inventory.json, have the owner resolve open decisions, then record devkit.approval with the kit scope digest above (docs/licensing.md#dev-kit)"
}
LICENSE_STATUS=$(printf '%s\n' "$LICENSE_SUMMARY" | sed -n '$p' | sed 's/^license-inventory: //')
if [ "$LICENSE_GATE" = rehearsal ] && printf '%s\n' "$LICENSE_SUMMARY" | grep -q '^license-inventory: UNRESOLVED: devkit'; then
    printf '%s\n' "$LICENSE_SUMMARY" | grep '^license-inventory: UNRESOLVED: devkit' >&2
    echo "WARNING: licence rehearsal kit: the open licence items above are recorded, not enforced; install.sh refuses this kit without --rehearsal (docs/licensing.md#dev-kit)" >&2
fi

REMOTE=$(git remote get-url origin 2>/dev/null || true)
GITHUB_REPO=${DEVKIT_SNAPSHOT_REPO:-}
if [ -z "$GITHUB_REPO" ]; then
    case "$REMOTE" in
        *github.com[:/]*) GITHUB_REPO=$(printf '%s\n' "$REMOTE" | sed -E 's#.*github\.com[:/]##; s#\.git$##') ;;
    esac
fi
[ -n "$GITHUB_REPO" ] || die "cannot infer the GitHub repository from 'git remote get-url origin'; set DEVKIT_SNAPSHOT_REPO=OWNER/REPO"
IMAGE_REPOSITORY=${DEVKIT_IMAGE_REPOSITORY:-ghcr.io/$(printf '%s\n' "${GHCR_OWNER:-${GITHUB_REPO%%/*}}" | tr 'A-Z' 'a-z')/qdrant-pdf-rag-devkit}

KIT_NAME="mfr-devkit-$KEY"
DIST="$REPO_ROOT/dist"
OUT="$DIST/$KIT_NAME"
[ ! -e "$OUT" ] || die "dist/$KIT_NAME already exists: move it aside (a snapshot-only re-sign passes its image/ with --reuse-image) and pack again"
mkdir -p "$DIST"
STAGE=$(mktemp -d "$DIST/.mfr-devkit-pack.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
KIT="$STAGE/$KIT_NAME"
mkdir "$KIT"

echo "==> Dev workspace image $IMAGE_REPOSITORY@$DIGEST (OCI layout)"
if [ -n "$REUSE_IMAGE" ]; then
    [ -d "$REUSE_IMAGE" ] && [ ! -L "$REUSE_IMAGE" ] || die "--reuse-image must name the image/ directory of an earlier kit"
    cp -R "$REUSE_IMAGE" "$KIT/image"
else
    # --preserve-digests keeps the registry's manifest and compressed layers
    # byte-for-byte, so the layout's manifest digest is the pinned digest.
    # shellcheck disable=SC2086
    skopeo copy ${SKOPEO_ARGS:-} --preserve-digests "docker://$IMAGE_REPOSITORY@$DIGEST" "oci:$KIT/image" >/dev/null ||
        die "could not pull $IMAGE_REPOSITORY@$DIGEST: check registry access, or that the factory pushed this digest"
fi
LAYOUT_DIGEST=$(python3 -I "$REPO_ROOT/scripts/airgap/image_manifest.py" layout "$KIT/image" "$STAGE/manifest.json") ||
    die "the image directory is not one verified OCI image (a blob is missing or differs)"
[ "$LAYOUT_DIGEST" = "$DIGEST" ] ||
    die "the image layout holds $LAYOUT_DIGEST, not the pinned $DIGEST: pack from the pinned digest only"
CONFIG_DIGEST=$(python3 -I "$REPO_ROOT/scripts/airgap/image_manifest.py" config < "$STAGE/manifest.json") ||
    die "the image manifest names no valid config"

echo "==> Pinned oras.exe (Windows operator push)"
ORAS_PIN="$REPO_ROOT/scripts/tools/oras-windows-pin.txt"
ORAS_VERSION=$(sed -n 's/^version: *//p' "$ORAS_PIN")
ORAS_ASSET=$(sed -n 's/^asset: *//p' "$ORAS_PIN")
ORAS_SHA256=$(sed -n 's/^sha256: *//p' "$ORAS_PIN")
ORAS_BINARY_SHA256=$(sed -n 's/^binary-sha256: *//p' "$ORAS_PIN")
ORAS_LICENSE_SHA256=$(sed -n 's/^license-sha256: *//p' "$ORAS_PIN")
ORAS_ORIGIN=$(sed -n 's/^origin: *//p' "$ORAS_PIN")
case "$ORAS_ORIGIN" in https://*/"$ORAS_ASSET") ;; *) die "scripts/tools/oras-windows-pin.txt origin does not name its asset" ;; esac
ORAS_SOURCE=${DEVKIT_ORAS_ARCHIVE:-$REPO_ROOT/.tools/cache/$ORAS_ASSET}
if [ ! -f "$ORAS_SOURCE" ] && [ -z "${DEVKIT_ORAS_ARCHIVE:-}" ]; then
    command -v curl >/dev/null 2>&1 || die "curl or a provisioned DEVKIT_ORAS_ARCHIVE is required on the connected pack host"
    ORAS_SOURCE="$STAGE/$ORAS_ASSET"
    curl -fsSL --retry 3 --retry-all-errors -o "$ORAS_SOURCE" "$ORAS_ORIGIN" || die "could not download $ORAS_ASSET"
fi
[ -f "$ORAS_SOURCE" ] || die "DEVKIT_ORAS_ARCHIVE file not found: $ORAS_SOURCE"
[ "$(sha256sum "$ORAS_SOURCE" | awk '{print $1}')" = "$ORAS_SHA256" ] ||
    die "$ORAS_ASSET does not match scripts/tools/oras-windows-pin.txt: download it again from the pinned origin"
# The kit ships the extracted binary and licence, never the zip.
python3 -I - "$ORAS_SOURCE" "$KIT" <<'PYEOF' || die "$ORAS_ASSET does not hold oras.exe and LICENSE"
import sys, zipfile
from pathlib import Path
with zipfile.ZipFile(sys.argv[1]) as archive:
    names = {info.filename: info for info in archive.infolist()}
    for member, target in (("oras.exe", "oras.exe"), ("LICENSE", "oras-LICENSE")):
        Path(sys.argv[2], target).write_bytes(archive.read(names[member]))
PYEOF
[ "$(sha256sum "$KIT/oras.exe" | awk '{print $1}')" = "$ORAS_BINARY_SHA256" ] ||
    die "oras.exe does not match the binary-sha256 in scripts/tools/oras-windows-pin.txt"
[ "$(sha256sum "$KIT/oras-LICENSE" | awk '{print $1}')" = "$ORAS_LICENSE_SHA256" ] ||
    die "the ORAS licence does not match the license-sha256 in scripts/tools/oras-windows-pin.txt"

echo "==> Pinned static Linux oras (the runbook's fallback push from a pod)"
ORAS_LINUX_PIN="$REPO_ROOT/scripts/tools/oras-pin.txt"
ORAS_LINUX_ASSET=$(sed -n 's/^asset: *//p' "$ORAS_LINUX_PIN")
ORAS_LINUX_SHA256=$(sed -n 's/^sha256: *//p' "$ORAS_LINUX_PIN")
ORAS_LINUX_BINARY_SHA256=$(sed -n 's/^binary-sha256: *//p' "$ORAS_LINUX_PIN")
[ "$(sed -n 's/^version: *//p' "$ORAS_LINUX_PIN")" = "$ORAS_VERSION" ] ||
    die "scripts/tools/oras-pin.txt and oras-windows-pin.txt name different versions: bump both in one pin PR"
ORAS_LINUX_SOURCE=${DEVKIT_ORAS_LINUX_ARCHIVE:-$REPO_ROOT/.tools/cache/$ORAS_LINUX_ASSET}
if [ ! -f "$ORAS_LINUX_SOURCE" ] && [ -z "${DEVKIT_ORAS_LINUX_ARCHIVE:-}" ]; then
    command -v curl >/dev/null 2>&1 || die "curl or a provisioned DEVKIT_ORAS_LINUX_ARCHIVE is required on the connected pack host"
    ORAS_LINUX_SOURCE="$STAGE/$ORAS_LINUX_ASSET"
    curl -fsSL --retry 3 --retry-all-errors -o "$ORAS_LINUX_SOURCE" "$(sed -n 's/^origin: *//p' "$ORAS_LINUX_PIN")" ||
        die "could not download $ORAS_LINUX_ASSET"
fi
[ -f "$ORAS_LINUX_SOURCE" ] || die "DEVKIT_ORAS_LINUX_ARCHIVE file not found: $ORAS_LINUX_SOURCE"
[ "$(sha256sum "$ORAS_LINUX_SOURCE" | awk '{print $1}')" = "$ORAS_LINUX_SHA256" ] ||
    die "$ORAS_LINUX_ASSET does not match scripts/tools/oras-pin.txt: download it again from the pinned origin"
mkdir "$STAGE/oras-linux"
tar -xzf "$ORAS_LINUX_SOURCE" -C "$STAGE/oras-linux" oras || die "$ORAS_LINUX_ASSET does not hold oras"
[ "$(sha256sum "$STAGE/oras-linux/oras" | awk '{print $1}')" = "$ORAS_LINUX_BINARY_SHA256" ] ||
    die "the Linux oras does not match the binary-sha256 in scripts/tools/oras-pin.txt"
cp "$STAGE/oras-linux/oras" "$KIT/oras"

echo "==> Pinned Task licence (the image's /usr/local/bin/task)"
# The image holds the Task binary only; its MIT notice travels in the kit.
. "$REPO_ROOT/scripts/tools/task-artifact.sh"
task_read_pin "$REPO_ROOT/scripts/tools/task-pin.txt"
TASK_SOURCE=${DEVKIT_TASK_ARCHIVE:-$REPO_ROOT/.tools/cache/$TASK_ASSET}
if [ ! -f "$TASK_SOURCE" ] && [ -z "${DEVKIT_TASK_ARCHIVE:-}" ]; then
    command -v curl >/dev/null 2>&1 || die "curl or a provisioned DEVKIT_TASK_ARCHIVE is required on the connected pack host"
    TASK_SOURCE="$STAGE/$TASK_ASSET"
    curl -fsSL --retry 3 --retry-all-errors -o "$TASK_SOURCE" "$TASK_ORIGIN" || die "could not download $TASK_ASSET"
fi
[ -f "$TASK_SOURCE" ] || die "DEVKIT_TASK_ARCHIVE file not found: $TASK_SOURCE"
mkdir "$STAGE/task"
# Verifies the archive, binary and licence against task-pin.txt or exits non-zero.
( task_extract_verified "$TASK_SOURCE" "$STAGE/task" ) ||
    die "$TASK_ASSET does not match scripts/tools/task-pin.txt: download it again from the pinned origin"
cp "$STAGE/task/LICENSE" "$KIT/task-LICENSE"

echo "==> Anchor bootstrap (ADR-0009 §8 verbatim; the runbook's copy must be identical)"
# The fenced block after a marker line, exactly.
fenced_after() {
    awk -v m="$1" '$0 == m { f = 1; next } f == 1 && $0 == "```powershell" { f = 2; next } f == 2 && $0 == "```" { exit } f == 2 { print }' "$2"
}
fenced_after '<!-- devkit-anchor-bootstrap -->' "$REPO_ROOT/docs/adr/0009-gap-fork.md" > "$STAGE/bootstrap.txt"
fenced_after '<!-- devkit-runbook: step 1 -->' "$REPO_ROOT/docs/runbooks/devkit-windows.md" > "$STAGE/runbook-step1.txt"
[ "$(wc -l < "$STAGE/bootstrap.txt" | tr -d ' ')" = 3 ] ||
    die "ADR-0009 §8 holds no three-line anchor bootstrap after <!-- devkit-anchor-bootstrap -->"
cmp -s "$STAGE/bootstrap.txt" "$STAGE/runbook-step1.txt" ||
    die "docs/runbooks/devkit-windows.md step 1 differs from the anchor bootstrap in ADR-0009 §8: copy the ADR text verbatim"

echo "==> Bastion template, Windows runbook, installer"
cp "$REPO_ROOT/deploy/workspace/bastion.template.yaml" "$KIT/bastion.template.yaml"
cp "$REPO_ROOT/docs/runbooks/devkit-windows.md" "$KIT/devkit-windows.md"
cp "$REPO_ROOT/scripts/devkit/install.sh" "$KIT/install.sh"
cp "$REPO_ROOT/scripts/airgap/image_manifest.py" "$KIT/image_manifest.py"

echo "==> Offline issue snapshot ($GITHUB_REPO)"
SNAPSHOT_TIME=$(python3 -I "$REPO_ROOT/scripts/devkit/issue_snapshot.py" --repo "$GITHUB_REPO" --output "$KIT/issues.json") ||
    die "the issue snapshot failed (its message above names the fix)"
SNAPSHOT_ISSUES=$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$REPO_ROOT/scripts/devkit/snapshot-issues.txt" | awk '{printf "%s%s", sep, $1; sep=" "} END {print ""}')

echo "==> MANIFEST"
{
    echo "kit: $KIT_NAME"
    echo "key: $KEY"
    echo "image: $IMAGE_REPOSITORY"
    echo "image_digest: $DIGEST"
    echo "image_config_digest: $CONFIG_DIGEST"
    echo "image_format: oci-layout"
    echo "commit: $COMMIT"
    echo "snapshot_time: $SNAPSHOT_TIME"
    echo "snapshot_repository: $GITHUB_REPO"
    echo "snapshot_issues: $SNAPSHOT_ISSUES"
    echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "oras_version: $ORAS_VERSION"
    echo "oras_platform: windows-amd64"
    echo "oras_sha256: $ORAS_BINARY_SHA256"
    echo "oras_linux_sha256: $ORAS_LINUX_BINARY_SHA256"
    echo "license_status: $LICENSE_STATUS"
    echo "license_gate: $LICENSE_GATE"
    # Every kit key input, as `pin_sha256: <hex> <path>` in the key's order.
    python3 -I - "$REPO_ROOT" <<'PYEOF'
import hashlib, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts"))
from devkit_key import inputs
for relative in inputs(root):
    print(f"pin_sha256: {hashlib.sha256((root / relative).read_bytes()).hexdigest()} {relative}")
PYEOF
    # Honesty label, not a trust root (the bundle's convention): strength comes
    # from SNEAKERNET_TRUSTED_PUB in install.sh and the published anchor.
    if [ "${SNEAKERNET_KEY_TRUSTED:-}" = "true" ]; then echo "signed: true"; else echo "signed: ephemeral"; fi
} > "$KIT/MANIFEST.txt"

echo "==> SBOM and third-party notices"
python3 -I - "$REPO_ROOT" "$KIT" "$IMAGE_REPOSITORY" "$DIGEST" "$CONFIG_DIGEST" "$LICENSE_STATUS" "$COMMIT" <<'PYEOF' ||
import json, re, sys
from pathlib import Path
root, kit = Path(sys.argv[1]), Path(sys.argv[2])
image, digest, config, license_status, commit = sys.argv[3:8]
sys.path.insert(0, str(root / "scripts"))
from dependency_lock import load
from license_inventory import KIT_TOOL_PINS, read_pin

_, wheels = load(root, "dev")
base = re.search(r"(?m)^FROM\s+(\S+@sha256:[0-9a-f]{64})", (root / "images/Containerfile.devkit").read_text(encoding="utf-8"))[1]
rpms = [line for line in (root / "images/devkit-packages.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")]
tools = []
for name, pin_file in KIT_TOOL_PINS:
    fields = read_pin(root, pin_file)
    tools.append({"name": name, "pin": f"scripts/tools/{pin_file}", **{k: fields[k] for k in
                  ("version", "asset", "sha256", "binary-sha256", "origin", "license", "platforms") if k in fields}})
sbom = {
    "kit_key": kit.name.removeprefix("mfr-devkit-"),
    "commit": commit,
    "image": {"ref": image, "digest": digest, "config_digest": config, "format": "oci-layout"},
    "base_image": {"ref": base, "digest": base.split("@", 1)[1]},
    "rpms": rpms,
    "wheels": [{"name": name, **entry} for name, entry in sorted(wheels.items())],
    "tools": tools,
    "inventory_scope": "Declared from the pins the image was built from (images/Containerfile.devkit verifies each during the build). Not a vulnerability or license approval.",
}
(kit / "sbom.json").write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")

PYEOF
    die "the SBOM could not be written from the pins"
# Every kit component with its licence, decision state and the notice texts held
# (oras-LICENSE is read from the kit and checked against its pinned digest).
python3 "$REPO_ROOT/scripts/license_inventory.py" --root "$REPO_ROOT" notices --kit --bundle-dir "$KIT" \
    --output "$KIT/THIRD-PARTY-NOTICES.txt" >/dev/null ||
    die "the kit third-party notices could not be rendered from licenses/inventory.json (docs/licensing.md#dev-kit)"

echo "==> Member checksums and signature (the release bundle's mechanism)"
openssl pkey -in "$SNEAKERNET_SIGNING_KEY" -pubout -out "$KIT/sneakernet-signing.pub"
# Every regular file in the kit, sorted, as `<sha256>  <path>`. A link or any
# other special file is refused: the transfer rule cannot carry it.
[ -z "$(find "$KIT" ! -type f ! -type d | head -n 1)" ] || die "the kit holds a link or special file; it must be plain files only"
(cd "$KIT" && find . -type f -exec sha256sum {} + > "$STAGE/SHA256SUMS.raw") ||
    die "the member checksums could not be written"
sed 's#  \./#  #' "$STAGE/SHA256SUMS.raw" | LC_ALL=C sort -k 2 > "$STAGE/SHA256SUMS"
# Names Windows (NTFS) and install.sh both accept: no colon or other reserved
# character, and no two names that differ only in case.
awk '$2 !~ /^[A-Za-z0-9][A-Za-z0-9._-]*(\/[A-Za-z0-9][A-Za-z0-9._-]*)*$/ { bad = 1 } END { exit bad }' "$STAGE/SHA256SUMS" ||
    die "a kit member name is not NTFS-safe (letters, digits, . _ - and / only)"
[ -z "$(awk '{print tolower($2)}' "$STAGE/SHA256SUMS" | LC_ALL=C sort | uniq -d)" ] ||
    die "two kit member names differ only in case; Windows would merge them"
mv "$STAGE/SHA256SUMS" "$KIT/SHA256SUMS"
openssl dgst -sha256 -sign "$SNEAKERNET_SIGNING_KEY" -out "$KIT/SHA256SUMS.sig" "$KIT/SHA256SUMS"
FINGERPRINT=$(sha256sum "$KIT/SHA256SUMS" | awk '{print $1}')
printf '%s  SHA256SUMS\n' "$FINGERPRINT" > "$KIT/SHA256SUMS.sha256"

mv "$KIT" "$OUT"
ANCHOR=$(printf '%s' "$FINGERPRINT" | cut -c1-16)
SIZE=$(du -sk "$OUT" | awk '{print $1}')
BOOTSTRAP=$(cat "$STAGE/bootstrap.txt")
echo ""
echo "Packed: dist/$KIT_NAME ($SIZE KiB, $(wc -l < "$OUT/SHA256SUMS" | tr -d ' ') members)"
echo "Fingerprint (sha256 of SHA256SUMS): $FINGERPRINT"
echo "Trust anchor (publish on GitHub, carry separately): $ANCHOR"
echo "Anchor bootstrap (publish verbatim with the anchor; the operator sets \$Kit and \$Anchor, then pastes):"
echo "----- anchor bootstrap -----"
printf '%s\n' "$BOOTSTRAP"
echo "----- end anchor bootstrap -----"
trap - EXIT
rm -rf "$STAGE"
next_step "publish the fingerprint with the release record, then transfer the dist/$KIT_NAME directory as plain files and follow its devkit-windows.md"
