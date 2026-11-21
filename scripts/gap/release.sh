#!/bin/sh
# Gap release pipeline entry point (GH#1207 Rev 2, ADR-0009 §5, Increment 1).
#
# Runs from the installed signed release workspace on the bastion:
#   sh scripts/gap/release.sh classify --repo <url> --tag <tag> --base-sha <sha>
#   sh scripts/gap/release.sh pack --tag <tag> --remote <url> --base-bundle <path> --work-dir <dir>
#   sh scripts/gap/release.sh assemble --work-dir <dir> --signature <SHA256SUMS.sig>
#   sh scripts/gap/release.sh verify --bundle <tar-or-dir> --base-bundle <tar-or-dir>
#   sh scripts/gap/release.sh rehearse-check --rehearsal-workspace <dir> ...
#   sh scripts/gap/release.sh render-role --template <sign|sign-vault|rehearsal-egress> --param-file <file>
#
# Signing NEVER runs in the bastion (Rev 2 D3): there is no `sign` subcommand here.
# Use scripts/gap/sign_vault.py on the maintainer workstation or in the ephemeral
# F9 `sign` pod (docs/runbooks/gap-release.md).

set -eu

REPO_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
COMMON="$REPO_ROOT/scripts/airgap/common.sh"
RELEASE_PY="$REPO_ROOT/scripts/gap/release.py"

usage() {
    cat <<'EOF'
Gap release pipeline (ADR-0009 §5, GH#1207 Increment 1).

Subcommands:
  classify        Classify changed paths against the gap release allow-list (D2).
  pack            Pack an unsigned origin: gap bundle in the bastion (zero replaced layers).
  assemble        Verify an externally-produced signature and write the deterministic tarball.
  verify          Trusted pre-promotion verification of a signed gap bundle.
  rehearse-check  Enforce D6 refusals on the rehearsal workspace's resolved deploy target.
  render-role     Render an F6 workspace template; refuse an egress rule without peers.

Signing is never run in the bastion (Rev 2 D3); see docs/runbooks/gap-release.md.
EOF
}

case "${1:-}" in
    ""|-h|--help)
        usage
        exit 0
        ;;
    sign)
        echo "STOP: signing never runs in the bastion (ADR-0009 §5, Rev 2 D3) — sign SHA256SUMS on the maintainer workstation with Vault transit or in the ephemeral sign pod (docs/runbooks/gap-release.md)." >&2
        exit 1
        ;;
    classify|rehearse-check|render-role)
        exec python3 -I "$RELEASE_PY" --repo-root "$REPO_ROOT" "$@"
        ;;
    pack|assemble|verify)
        # Enforce the installed-release guard (Rev 2 §3e step 1, I9): only a
        # verified signed release workspace may pack, assemble, or verify.
        # Run in a subshell, from the workspace root (find_manifest reads
        # dist/MANIFEST.txt relative to it), with an empty AIRGAP_ENV so
        # common.sh checks the executing checkout without operator deploy
        # settings: the anchors come from the process environment only.
        _guard_tmp=$(mktemp -d)
        trap 'rm -rf "$_guard_tmp"' EXIT
        : > "$_guard_tmp/empty.env"
        if ! AIRGAP_ENV="$_guard_tmp/empty.env" AIRGAP_DRYRUN=0 \
                sh -c 'cd "$2" && . "$1" && MANIFEST=$(find_manifest) && check_checkout_sha' \
                "$REPO_ROOT/scripts/gap/release.sh" "$COMMON" "$REPO_ROOT"; then
            echo "STOP: gap release refused: executing workspace is not a verified signed release (FAIL line above)." >&2
            exit 1
        fi
        rm -rf "$_guard_tmp"
        trap - EXIT
        exec python3 -I "$RELEASE_PY" --repo-root "$REPO_ROOT" "$@"
        ;;
    *)
        echo "STOP: unknown subcommand '${1}' (expected classify, pack, assemble, verify, rehearse-check or render-role)." >&2
        exit 2
        ;;
esac
