#!/bin/sh
# AIR-GAP SIDE (issue #1070, R1): retire the plain `mainframe_manuals`
# collection of release d53595e before this release is deployed, or roll the
# alias back to the kept copy. Two explicit stages; no stage refuses.
#
#   sh scripts/airgap/retire_legacy.sh --stage copy   --confirm mainframe_manuals [--space-verified]
#   sh scripts/airgap/retire_legacy.sh --stage retire --confirm mainframe_manuals [--space-verified] [--no-keep-ok]
#   sh scripts/airgap/retire_legacy.sh --rollback     --confirm mainframe_manuals [--qdrant-url http://127.0.0.1:<port>]
#
# copy (before the window; d53595e keeps serving): verified points-API copies
#   mainframe_manuals_d53595e_keep(__completions), manifest re-keyed so the
#   d53595e agent serves them. Never deletes.
# retire (the window starts here): this run's snapshot of both collections on
#   every peer, bound to their id sets and recorded in
#   mainframe_manuals_d53595e_retire_record, then the delete. A re-run after a
#   crash continues; a snapshot this run did not record is never used.
# rollback: one atomic alias call mainframe_manuals -> the kept copy. Through
#   the agent pod, or with --qdrant-url through `port-forward svc/<qdrant>`
#   when no agent runs (the runbook's rollback path).
# Uses the writer key (`api-key`), sent on stdin, never printed. Outside
# dry-run it runs only from a verified signed release (check_checkout_sha).
# Runbook: docs/install_and_ops.md#upgrade-from-a-legacy-flat-collection-issue-1070

. "$(dirname -- "$0")/common.sh"

refuse_retired_keys

usage="usage: $0 --stage copy|retire --confirm $LEGACY_COLLECTION [--space-verified] [--no-keep-ok] | --rollback --confirm $LEGACY_COLLECTION [--qdrant-url http://127.0.0.1:<port>]"
MODE=""
CONFIRM=""
QDRANT_LOCAL_URL=""
SPACE_VERIFIED=0
NO_KEEP_OK=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm|--stage|--qdrant-url)
            [ "$#" -ge 2 ] || die "E_RETIRE_ARGS: $1 needs a value; $usage"
            case "$1" in
                --confirm) CONFIRM=$2 ;;
                --qdrant-url) QDRANT_LOCAL_URL=$2 ;;
                --stage)
                    case "$2" in
                        copy|retire) [ -z "$MODE" ] || die "E_RETIRE_ARGS: choose one of --stage or --rollback; $usage"; MODE=$2 ;;
                        *) die "E_RETIRE_STAGE: --stage is copy or retire; $usage" ;;
                    esac ;;
            esac
            shift 2 ;;
        --rollback) [ -z "$MODE" ] || die "E_RETIRE_ARGS: choose one of --stage or --rollback; $usage"; MODE=rollback; shift ;;
        --space-verified) SPACE_VERIFIED=1; shift ;;
        --no-keep-ok) NO_KEEP_OK=1; shift ;;
        --help|-h) echo "$usage"; exit 0 ;;
        *) die "unknown argument (see --help): $usage" ;;
    esac
done
[ -n "$MODE" ] ||
    die "E_RETIRE_STAGE: choose --stage copy (before the window, nothing deleted) or --stage retire (deletes; the window), each with --confirm $LEGACY_COLLECTION ($LAYOUT_RUNBOOK)"
[ "$CONFIRM" = "$LEGACY_COLLECTION" ] ||
    die "E_RETIRE_CONFIRM: this step changes stored data; run it with --confirm $LEGACY_COLLECTION ($LAYOUT_RUNBOOK)"
if [ -n "$QDRANT_LOCAL_URL" ]; then
    [ "$MODE" = rollback ] || die "E_RETIRE_ARGS: --qdrant-url is only for --rollback"
    # The writer key goes to this URL: loopback only (a port-forward).
    printf '%s\n' "$QDRANT_LOCAL_URL" | grep -Eqx 'http://(127\.0\.0\.1|localhost):[0-9]{1,5}/?' ||
        die "E_RETIRE_ARGS: --qdrant-url must be a loopback port-forward, http://127.0.0.1:<port>"
fi

resolve_aliases
case "$QDRANT_RELEASE" in
    ""|-*|*[!a-z0-9-]*) die "QDRANT_RELEASE must be a DNS-1123 label" ;;
esac
KEEP="${LEGACY_COLLECTION}_d53595e_keep"
QDRANT_NAME=$(qdrant_fullname)

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $MODE $LEGACY_COLLECTION in namespace $NAMESPACE (Qdrant $QDRANT_NAME): no cluster read or change"
    case "$MODE" in
        copy) echo "[dryrun] would check free space, copy $LEGACY_COLLECTION(__completions) to $KEEP(__completions), verify ids and re-key the manifest; nothing deleted" ;;
        retire) echo "[dryrun] would check free space, snapshot both collections on every peer, bind and record the snapshots, then delete both" ;;
        *) echo "[dryrun] would point alias $LEGACY_COLLECTION at $KEEP in one atomic call" ;;
    esac
    exit 0
fi
# The writer key below retires and re-points collections: like deploy, ingest,
# validate and load, run only from a verified signed release (#1118 F1, item e),
# before any cluster or Qdrant contact. Same opt-out rules as the other entry
# points (AIRGAP_UNVERIFIED_CHECKOUT=1 only with CI_REHEARSAL=1).
MANIFEST=$(find_manifest)
check_checkout_sha
require_kc
KC=${KC:-$(kc)}

_sts=$($KC -n "$NAMESPACE" get "statefulset/$QDRANT_NAME" --ignore-not-found -o name 2>/dev/null) ||
    die "E_RETIRE_UNREADABLE: statefulset/$QDRANT_NAME in namespace $NAMESPACE is unreadable; nothing was changed"
[ -n "$_sts" ] ||
    die "E_RETIRE_NO_QDRANT: no Qdrant StatefulSet $QDRANT_NAME in namespace $NAMESPACE: nothing to $MODE"
REPLICAS=$($KC -n "$NAMESPACE" get "statefulset/$QDRANT_NAME" -o 'jsonpath={.spec.replicas}' 2>/dev/null) || REPLICAS=""
case "$REPLICAS" in
    ''|*[!0-9]*|0) die "E_RETIRE_UNREADABLE: cannot read the replica count of statefulset/$QDRANT_NAME; nothing was changed" ;;
esac

set --
if [ "$MODE" = retire ]; then
    # Peers for the node-local snapshots: the operator's direct peer URLs,
    # else the StatefulSet's headless names.
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
            set -- "$@" --peer-url "http://$QDRANT_NAME-$_i.$QDRANT_NAME-headless:6333"
            _i=$((_i + 1))
        done
    fi
    [ "$NO_KEEP_OK" != 1 ] || set -- "$@" --no-keep-ok
fi

if [ "$MODE" != rollback ]; then
    _layout=$(printf '\n' | layout_exec check --collection "$LEGACY_COLLECTION" 2>/dev/null |
        sed -n 's/^LAYOUT //p' | tail -n 1) || _layout=""
    case "$_layout" in
        empty|ok|flat_legacy|unbound_alias|rolled_back) ;;
        *) die "E_RETIRE_UNREADABLE: no running rag-agent pod could read the stored layout; nothing was changed ($LAYOUT_RUNBOOK)" ;;
    esac
    # Free space, before any write and only while the legacy pair exists. Per
    # peer, with the data volume's used size U as the upper bound: copy adds
    # the kept copy (≤ U) to the data volume; retire, with the copy already
    # counted in U, adds Qdrant's temporary snapshot (storage/snapshots_temp,
    # ≤ U/2) to the data volume and the snapshot (≤ U/2) to the snapshot volume.
    if [ "$_layout" = flat_legacy ] && [ "$SPACE_VERIFIED" != 1 ]; then
        _i=0
        while [ "$_i" -lt "$REPLICAS" ]; do
            _df=$($KC -n "$NAMESPACE" exec "$QDRANT_NAME-$_i" -- df -Pk /qdrant/storage /qdrant/snapshots 2>/dev/null) || _df=""
            _verdict=$(printf '%s\n' "$_df" | awk -v mode="$MODE" '
                $NF == "/qdrant/storage" { used = $3; free = $4; s = 1 }
                $NF == "/qdrant/snapshots" { snap = $4; n = 1 }
                END {
                    if (!s || !n || used !~ /^[0-9]+$/ || free !~ /^[0-9]+$/ || snap !~ /^[0-9]+$/) { print "unknown"; exit }
                    if (mode == "copy") ok = (free >= used); else ok = (free >= used && 2 * snap >= used)
                    print (ok ? "ok" : "short")
                }')
            case "$_verdict" in
                ok) echo "==> Free space on $QDRANT_NAME-$_i: enough for the $MODE stage" ;;
                short) die "E_RETIRE_SPACE: $QDRANT_NAME-$_i lacks room for the $MODE stage (data volume free must be at least its used size; for retire the snapshot volume free at least half of it). Expand the claims (runbook: PVC expansion), or if you have checked the real need yourself, re-run with --space-verified; nothing was changed" ;;
                *) die "E_RETIRE_SPACE_UNKNOWN: cannot read the volume usage of $QDRANT_NAME-$_i. Check every peer's qdrant-storage and qdrant-snapshots claims by the site's means (data volume free at least its used size; for retire the snapshot volume free at least half of it), then re-run with --space-verified; nothing was changed" ;;
            esac
            _i=$((_i + 1))
        done
    fi
fi

_key_b64=$($KC -n "$NAMESPACE" get secret "$QDRANT_NAME-apikey" -o "jsonpath={.data['api-key']}" 2>/dev/null) || _key_b64=""
[ -n "$_key_b64" ] ||
    die "E_RETIRE_NO_WRITER_KEY: cannot read key api-key of Secret $QDRANT_NAME-apikey; nothing was changed"
_rc=0
if [ -n "$QDRANT_LOCAL_URL" ]; then
    echo "==> $MODE: running on this host against $QDRANT_LOCAL_URL with the writer key (sent on stdin, never printed)"
    { printf '%s' "$_key_b64" | base64 -d 2>/dev/null; printf '\n'; } |
        QDRANT_URL=$QDRANT_LOCAL_URL python3 scripts/airgap/legacy_layout.py "$MODE" --collection "$LEGACY_COLLECTION" "$@" || _rc=$?
else
    echo "==> $MODE: running in the agent pod with the writer key (sent on stdin, never printed)"
    { printf '%s' "$_key_b64" | base64 -d 2>/dev/null; printf '\n'; } |
        layout_exec "$MODE" --collection "$LEGACY_COLLECTION" "$@" || _rc=$?
fi
unset _key_b64
case "$_rc" in
    0)
        case "$MODE" in
            copy) next_step "in the maintenance window: sh scripts/airgap/retire_legacy.sh --stage retire --confirm $LEGACY_COLLECTION ($LAYOUT_RUNBOOK)" ;;
            retire) next_step "deploy this release (sh scripts/tools/run-task.sh airgap:deploy), then a full ingest ($LAYOUT_RUNBOOK)" ;;
            *) next_step "redeploy d53595e from its own workspace, then verify ($LAYOUT_RUNBOOK)" ;;
        esac ;;
    4) die "$MODE refused (see the FAIL line above); fix the named condition and re-run ($LAYOUT_RUNBOOK)" ;;
    3) die "$MODE stopped on a Qdrant read or write failure; re-run the same command, it continues where it stopped. If the space check now refuses because the kept copy counts as used, confirm the claims and add --space-verified" ;;
    *)
        if [ "$MODE" = rollback ]; then
            die "rollback did not run: no rag-agent pod could run it. Use the port-forward path: $KC -n $NAMESPACE port-forward svc/$QDRANT_NAME 16333:6333, then re-run with --qdrant-url http://127.0.0.1:16333 ($LAYOUT_RUNBOOK)"
        fi
        die "$MODE did not complete (the agent pod could not run it); re-run the same command when a rag-agent pod is running" ;;
esac
