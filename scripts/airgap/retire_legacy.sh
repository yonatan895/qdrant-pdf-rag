#!/bin/sh
# AIR-GAP SIDE (issue #1070, R1): retire the plain `mainframe_manuals`
# collection of release d53595e before this release is deployed, in a
# maintenance window, or roll the alias back to the kept copy.
#
#   sh scripts/airgap/retire_legacy.sh --confirm mainframe_manuals [--space-verified] [--no-keep-ok]
#   sh scripts/airgap/retire_legacy.sh --rollback --confirm mainframe_manuals
#
# Retire converges to: mainframe_manuals and mainframe_manuals__completions
# deleted, their verified points-API copies mainframe_manuals_d53595e_keep(__completions)
# kept as the rollback data (manifest re-keyed so the d53595e agent serves it),
# and a verified snapshot of both on every Qdrant peer. Nothing is deleted
# before all of that is verified; a re-run after a crash continues. Rollback
# points the alias mainframe_manuals at the kept copy in one atomic call.
# Uses the writer key (`api-key`), sent on stdin, never printed.
# Runbook: docs/install_and_ops.md#upgrade-from-a-legacy-flat-collection-issue-1070

. "$(dirname -- "$0")/common.sh"

usage="usage: $0 --confirm $LEGACY_COLLECTION [--space-verified] [--no-keep-ok] | --rollback --confirm $LEGACY_COLLECTION"
MODE=retire
CONFIRM=""
SPACE_VERIFIED=0
NO_KEEP_OK=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm)
            [ "$#" -ge 2 ] || die "E_RETIRE_CONFIRM: retire needs --confirm $LEGACY_COLLECTION"
            CONFIRM=$2
            shift 2 ;;
        --rollback) MODE=rollback; shift ;;
        --space-verified) SPACE_VERIFIED=1; shift ;;
        --no-keep-ok) NO_KEEP_OK=1; shift ;;
        --help|-h) echo "$usage"; exit 0 ;;
        *) die "unknown argument (see --help): $usage" ;;
    esac
done
[ "$CONFIRM" = "$LEGACY_COLLECTION" ] ||
    die "E_RETIRE_CONFIRM: this step changes stored data; run it with --confirm $LEGACY_COLLECTION ($LAYOUT_RUNBOOK)"

resolve_aliases
case "$QDRANT_RELEASE" in
    ""|-*|*[!a-z0-9-]*) die "QDRANT_RELEASE must be a DNS-1123 label" ;;
esac
KEEP="${LEGACY_COLLECTION}_d53595e_keep"

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $MODE $LEGACY_COLLECTION in namespace $NAMESPACE (Qdrant $QDRANT_RELEASE): no cluster read or change"
    if [ "$MODE" = retire ]; then
        echo "[dryrun] would check free space on every peer, copy $LEGACY_COLLECTION(__completions) to $KEEP(__completions), verify ids, re-key the manifest, snapshot both on every peer, then delete both"
    else
        echo "[dryrun] would point alias $LEGACY_COLLECTION at $KEEP in one atomic call"
    fi
    exit 0
fi
require_kc
KC=${KC:-$(kc)}

_sts=$($KC -n "$NAMESPACE" get "statefulset/$QDRANT_RELEASE" --ignore-not-found -o name 2>/dev/null) ||
    die "E_RETIRE_UNREADABLE: statefulset/$QDRANT_RELEASE in namespace $NAMESPACE is unreadable; nothing was changed"
[ -n "$_sts" ] ||
    die "E_RETIRE_NO_QDRANT: no Qdrant release $QDRANT_RELEASE in namespace $NAMESPACE: nothing to $MODE"
REPLICAS=$($KC -n "$NAMESPACE" get "statefulset/$QDRANT_RELEASE" -o 'jsonpath={.spec.replicas}' 2>/dev/null) || REPLICAS=""
case "$REPLICAS" in
    ''|*[!0-9]*|0) die "E_RETIRE_UNREADABLE: cannot read the replica count of statefulset/$QDRANT_RELEASE; nothing was changed" ;;
esac

# Peers for the node-local snapshots: the operator's direct peer URLs, else
# the StatefulSet's headless names.
set --
if [ -n "${QDRANT_PEER_URLS:-}" ]; then
    _old_ifs=$IFS
    IFS=,
    for _url in $QDRANT_PEER_URLS; do
        [ -z "$_url" ] || set -- "$@" --peer-url "$_url"
    done
    IFS=$_old_ifs
else
    _i=0
    while [ "$_i" -lt "$REPLICAS" ]; do
        set -- "$@" --peer-url "http://$QDRANT_RELEASE-$_i.$QDRANT_RELEASE-headless:6333"
        _i=$((_i + 1))
    done
fi

if [ "$MODE" = retire ]; then
    _layout=$(printf '\n' | layout_exec check --collection "$LEGACY_COLLECTION" 2>/dev/null |
        sed -n 's/^LAYOUT //p' | tail -n 1) || _layout=""
    case "$_layout" in
        empty|ok|flat_legacy|unbound_alias|rolled_back) ;;
        *) die "E_RETIRE_UNREADABLE: no running rag-agent pod could read the stored layout; nothing was changed ($LAYOUT_RUNBOOK)" ;;
    esac
    # Free space, before any write and only while there is something to copy.
    # Per peer: the copy and Qdrant's temporary snapshot both land on the data
    # volume (storage/snapshots_temp), the snapshot on the snapshot volume.
    # The data volume's used size bounds the legacy pair's size from above.
    if [ "$_layout" = flat_legacy ] && [ "$SPACE_VERIFIED" != 1 ]; then
        _i=0
        while [ "$_i" -lt "$REPLICAS" ]; do
            _df=$($KC -n "$NAMESPACE" exec "$QDRANT_RELEASE-$_i" -- df -Pk /qdrant/storage /qdrant/snapshots 2>/dev/null) || _df=""
            _verdict=$(printf '%s\n' "$_df" | awk '
                $NF == "/qdrant/storage" { used = $3; free = $4; s = 1 }
                $NF == "/qdrant/snapshots" { snap = $4; n = 1 }
                END {
                    if (!s || !n || used !~ /^[0-9]+$/ || free !~ /^[0-9]+$/ || snap !~ /^[0-9]+$/) { print "unknown"; exit }
                    if (free >= 2 * used && snap >= used) print "ok"; else print "short"
                }')
            case "$_verdict" in
                ok) echo "==> Free space on $QDRANT_RELEASE-$_i: enough for the kept copy and the snapshot" ;;
                short) die "E_RETIRE_SPACE: $QDRANT_RELEASE-$_i lacks room: the data volume needs free space of twice its used size (kept copy plus temporary snapshot) and the snapshot volume the used size again. Expand the claims first (runbook: PVC expansion); nothing was changed" ;;
                *) die "E_RETIRE_SPACE_UNKNOWN: cannot read the volume usage of $QDRANT_RELEASE-$_i. Check every peer's qdrant-storage and qdrant-snapshots claims by the site's means: the data volume needs free space of twice its used size and the snapshot volume the used size again. Then re-run with --space-verified; nothing was changed" ;;
            esac
            _i=$((_i + 1))
        done
    fi
    [ "$NO_KEEP_OK" != 1 ] || set -- "$@" --no-keep-ok
fi

_key_b64=$($KC -n "$NAMESPACE" get secret "$QDRANT_RELEASE-apikey" -o "jsonpath={.data['api-key']}" 2>/dev/null) || _key_b64=""
[ -n "$_key_b64" ] ||
    die "E_RETIRE_NO_WRITER_KEY: cannot read key api-key of Secret $QDRANT_RELEASE-apikey; nothing was changed"
echo "==> $MODE: running in the agent pod with the writer key (sent on stdin, never printed)"
_rc=0
{ printf '%s' "$_key_b64" | base64 -d 2>/dev/null; printf '\n'; } |
    layout_exec "$MODE" --collection "$LEGACY_COLLECTION" "$@" || _rc=$?
unset _key_b64
case "$_rc" in
    0) next_step "deploy this release (sh scripts/tools/run-task.sh airgap:deploy), then a full ingest ($LAYOUT_RUNBOOK)" ;;
    1) die "$MODE refused (see the FAIL line above); fix the named condition and re-run ($LAYOUT_RUNBOOK)" ;;
    3) die "$MODE stopped on a Qdrant read or write failure; re-run the same command, it continues where it stopped" ;;
    *) die "$MODE did not complete (the agent pod could not run it); re-run the same command when a rag-agent pod is running" ;;
esac
