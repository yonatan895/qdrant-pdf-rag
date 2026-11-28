#!/bin/sh
# AIR-GAP SIDE (issue #1373): list retained publication generations, or
# delete the snapshots of named retained builds, as a one-shot Job.
#
#   sh scripts/airgap/generations.sh list [--keep N] [--format text|json]
#   sh scripts/airgap/generations.sh dispose-snapshots --build-id <uuid> [--build-id <uuid>...]
#       [--keep N] [--allow-below-keep] [--execute] [--recovery-ref <ticket>]
#
# The Job is the ingest Job's template in generations mode (chart
# ingest.generations.args): the same packed image, writer key (Secret
# reference), peer URLs, restricted security context and the ingest-work
# volume, which holds the inventory, the publish sidecar and the publish
# lock. It runs `python -m mainframe_rag.ingest.generations` with
# --progress /work/inventory.jsonl; the module owns every eligibility rule
# (docs/ingest.md#retained-generations). dispose-snapshots is a dry run
# unless --execute is given. No reader stop: snapshots are never served.
#
# AIRGAP_DRYRUN=1 only renders and prints the cluster calls; it starts no
# Job. Without it, even the module's own dry run runs as a Job.
#
# Refuses while an ingest Job has not finished (or its pod is still running
# or terminating), when the ingest-work volume is absent, and while an
# earlier generations Job is unfinished. The publish lock inside the Job is
# the second exclusion against a concurrent publisher.

. "$(dirname -- "$0")/common.sh"
. "$(dirname -- "$0")/image_identity.sh"
. "$(dirname -- "$0")/one_shot_job.sh"

# The module's third subcommand, `dispose` (data disposal), is refused here
# by name: it has no site path in this release.
GENERATIONS_USAGE="usage: generations.sh list [--keep N] [--format text|json] | dispose-snapshots --build-id UUID... [--keep N] [--allow-below-keep] [--execute] [--recovery-ref REF]"
GENERATIONS_TIMEOUT=3600

# --- arguments (validated before anything else; values are data) ----------
[ $# -ge 1 ] || die "$GENERATIONS_USAGE"
GEN_COMMAND=$1
shift
case "$GEN_COMMAND" in
    list|dispose-snapshots) ;;
    dispose)
        # Data disposal (the module's `dispose`) deletes a served build's
        # collection. It is not reachable from the site in this release: the
        # agent must first refuse a build that is being retired, and the run
        # needs the reader-stop launcher. Refused here by name, whatever
        # else is passed; the module refuses --execute as well.
        die "data disposal (dispose) is not available from this launcher in this release; only list and dispose-snapshots are" ;;
    *) die "$GENERATIONS_USAGE" ;;
esac
# A value is one argument and is checked whole: `case` patterns match the
# entire string (a line-wise grep accepted a multi-line value on one good
# line, review B1 of #1387), with every allowed character spelled out so no
# locale range can widen a set. The arguments stay positional parameters to
# the end and are never word-split.
_GEN_DIGITS=0123456789
_GEN_ALNUM=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz$_GEN_DIGITS
_h='[0123456789abcdef]'
_GEN_UUID=$_h$_h$_h$_h$_h$_h$_h$_h-$_h$_h$_h$_h-$_h$_h$_h$_h-$_h$_h$_h$_h-$_h$_h$_h$_h$_h$_h$_h$_h$_h$_h$_h$_h
unset _h
gen_keep_ok() {
    case "$1" in
        ""|0*|*[!"$_GEN_DIGITS"]*) return 1 ;;
    esac
    [ "${#1}" -le 4 ]
}
gen_build_ok() {
    # shellcheck disable=SC2254 # the variable is the pattern
    case "$1" in
        $_GEN_UUID) return 0 ;;
    esac
    return 1
}
gen_ref_ok() {
    case "$1" in
        ""|[!"$_GEN_ALNUM"]*|*[!"$_GEN_ALNUM"._:/#-]*) return 1 ;;
    esac
    [ "${#1}" -le 128 ]
}
gen_operator_ok() {
    case "$1" in
        ""|[!"$_GEN_ALNUM"]*|*[!"$_GEN_ALNUM"._@-]*) return 1 ;;
    esac
    [ "${#1}" -le 64 ]
}
gen_builds=0
gen_execute=false
_expect=""
for _arg in "$@"; do
    case "$_expect" in
        "") ;;
        --keep)
            gen_keep_ok "$_arg" || die "--keep takes a whole number from 1 to 9999 (keeping 0 is not supported)"
            _expect=""
            continue ;;
        --format)
            case "$_arg" in text|json) ;; *) die "--format takes text or json" ;; esac
            _expect=""
            continue ;;
        --build-id)
            gen_build_ok "$_arg" ||
                die "--build-id takes a full lowercase build UUID (see the listing), never a collection name"
            gen_builds=$((gen_builds + 1))
            _expect=""
            continue ;;
        --recovery-ref)
            gen_ref_ok "$_arg" ||
                die "--recovery-ref takes a ticket or decision id ([A-Za-z0-9._:/#-], at most 128 characters, one line)"
            _expect=""
            continue ;;
    esac
    case "$GEN_COMMAND:$_arg" in
        *:--keep|list:--format|dispose-snapshots:--build-id|dispose-snapshots:--recovery-ref)
            _expect=$_arg ;;
        dispose-snapshots:--allow-below-keep) ;;
        dispose-snapshots:--execute)
            gen_execute=true ;;
        *) die "unknown or misplaced argument for $GEN_COMMAND: $_arg ($GENERATIONS_USAGE)" ;;
    esac
done
[ -z "$_expect" ] || die "$_expect needs a value"
unset _arg _expect
if [ "$GEN_COMMAND" = dispose-snapshots ]; then
    [ "$gen_builds" -ge 1 ] || die "dispose-snapshots needs at least one --build-id"
    if [ "$gen_execute" = true ]; then
        # The audit record names who ran it (maintainer decision 2026-11-26).
        # A login name with a control character (a newline above all) refuses
        # the run; any other unusable name is left to the module's uid
        # fallback and never reaches the Job.
        _operator=${USER:-${LOGNAME:-}}
        case "$_operator" in
            *[[:cntrl:]]*) die "the bastion login name (USER or LOGNAME) contains a control character; refusing to run" ;;
        esac
        if gen_operator_ok "$_operator"; then
            set -- "$@" --operator "$_operator"
        else
            echo "::warning::the bastion login name is not usable as an audit name ([A-Za-z0-9._@-]); the audit record names the pod's uid instead" >&2
        fi
        unset _operator
    fi
fi
# The Job's argument list, one positional parameter per argument.
set -- "$GEN_COMMAND" --progress /work/inventory.jsonl "$@"

# --- the same preflight as the ingest launcher, without a corpus ----------
enforce_product_rules
resolve_aliases
require_env INTERNAL_REGISTRY NAMESPACE IMAGE_SHA EMBED_MODEL DENSE_DIM EMBED_MODEL_REVISION EMBED_BASE_URL STORAGE_CLASS
require_embed_revision
check_secret_name "${GATEWAY_API_KEY_SECRET:-}" GATEWAY_API_KEY_SECRET
check_secret_name "${PULL_SECRET:-}" PULL_SECRET
check_secret_name "${GATEWAY_CA_CONFIGMAP:-}" GATEWAY_CA_CONFIGMAP
resolve_observability
case "$IMAGE_SHA" in
    ""|HEAD) die "IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)" ;;
esac
MANIFEST=$(find_manifest)
check_manifest_sha
check_checkout_sha
check_gap_release_stage "$MANIFEST"
require_kc
refuse_nfs_storage
KC=${KC:-$(kc)}
check_gateway_ca
verify_registry_images ingest
# The listing judges peer completeness against the selected policy and
# QDRANT_PEER_URLS, exactly as the publication gate does.
validate_collection_policy required
mkdir -p dist

echo "==> Helm: generations Job ($GEN_COMMAND)"
# Each argument becomes one --generations-arg pair; quoted throughout.
_gen_count=$#
for _arg in "$@"; do
    set -- "$@" --generations-arg "$_arg"
done
shift "$_gen_count"
unset _arg _gen_count
AGENT_ROUTE=false OPENWEBUI_ENABLED=false SPLUNK_GATEWAY_ENABLED=false \
    map_app_values --out dist/mainframe-rag-generations-values.yaml "$@"
helm template mainframe-rag charts/mainframe-rag -f dist/mainframe-rag-generations-values.yaml \
    --namespace "$NAMESPACE" --show-only templates/ingest-job.yaml > dist/generations-rendered.yaml
fail_on_placeholders dist/generations-rendered.yaml generations
grep -Eq '^  name: generations$' dist/generations-rendered.yaml || die "the render is not the generations Job"
check_ingest_qdrant_key dist/generations-rendered.yaml generations
require_gateway_secret_keys embed-api-key context-llm-api-key
require_secret_keys "${PULL_SECRET:-}" .dockerconfigjson

# --- cluster preconditions -------------------------------------------------
# A Job is finished when it carries Complete=True or Failed=True; a pod that
# is not Succeeded/Failed, or is being deleted, may still hold the work
# volume or the publish lock. Any read failure refuses.
job_unfinished() {
    _ju=$($KC -n "$NAMESPACE" get job "$1" --ignore-not-found \
        -o jsonpath='{.metadata.name}{"|"}{range .status.conditions[*]}{.type}{"="}{.status}{","}{end}') ||
        die "cannot read job/$1 in namespace $NAMESPACE; refusing"
    _jp=$($KC -n "$NAMESPACE" get pods -l "job-name=$1" \
        -o jsonpath='{range .items[*]}{.status.phase}{"|"}{.metadata.deletionTimestamp}{"\n"}{end}') ||
        die "cannot read the pods of job/$1 in namespace $NAMESPACE; refusing"
    _jstate=finished
    if [ -n "$_ju" ]; then
        case "$_ju" in
            *Complete=True*|*Failed=True*) ;;
            *) _jstate=unfinished ;;
        esac
    fi
    if printf '%s\n' "$_jp" | grep -v -e '^$' -e '^Succeeded|$' -e '^Failed|$' | grep -q .; then
        _jstate=unfinished
    fi
    [ "$_jstate" = unfinished ]
    _jrc=$?
    unset _ju _jp _jstate
    return $_jrc
}

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] refuse while job/ingest or job/generations is unfinished; require pvc/ingest-work"
    echo "[dryrun] $KC -n $NAMESPACE delete job generations --ignore-not-found --cascade=foreground --wait=true --timeout=300s --request-timeout=30s"
    echo "[dryrun] $KC apply -f dist/generations-rendered.yaml"
    echo "[dryrun] $KC -n $NAMESPACE wait --for=condition=complete job/generations --timeout=${GENERATIONS_TIMEOUT}s"
    echo "[dryrun] rendered manifest kept at dist/generations-rendered.yaml"
    exit 0
fi
_work=$($KC -n "$NAMESPACE" get pvc ingest-work --ignore-not-found -o name) || die "cannot check the ingest-work PVC"
[ -n "$_work" ] || die "the ingest-work PVC does not exist in namespace $NAMESPACE: no ingest has run here, so there is no inventory, sidecar or publish lock to use (this launcher never creates it)"
unset _work
if job_unfinished ingest; then
    die "job/ingest exists in namespace $NAMESPACE and has not finished (or its pod is still running or terminating); run generations after the ingest ends"
fi
if job_unfinished generations; then
    die "an earlier job/generations in namespace $NAMESPACE has not finished; wait for it or reconcile it explicitly"
fi
# A finished earlier generations Job is replaced; the delete is bounded.
$KC -n "$NAMESPACE" delete job generations --ignore-not-found --cascade=foreground --wait=true \
    --timeout=300s --request-timeout=30s >/dev/null || die "could not remove the earlier job/generations; delete it with: $KC -n $NAMESPACE delete job generations"
$KC apply -f dist/generations-rendered.yaml
watch_one_shot_job generations "$GENERATIONS_TIMEOUT" "sh scripts/airgap/generations.sh" collect
