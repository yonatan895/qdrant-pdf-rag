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

GENERATIONS_USAGE="usage: generations.sh list [--keep N] [--format text|json] | dispose-snapshots --build-id UUID... [--keep N] [--allow-below-keep] [--execute] [--recovery-ref REF]"
GENERATIONS_TIMEOUT=3600

# --- arguments (validated before anything else; values are data) ----------
[ $# -ge 1 ] || die "$GENERATIONS_USAGE"
GEN_COMMAND=$1
shift
case "$GEN_COMMAND" in
    list|dispose-snapshots) ;;
    *) die "$GENERATIONS_USAGE" ;;
esac
# Every token is checked against a fixed charset with no whitespace or
# pattern characters, so the list below word-splits safely (set -f).
GEN_ARGS="$GEN_COMMAND --progress /work/inventory.jsonl"
gen_builds=0
gen_execute=false
while [ $# -gt 0 ]; do
    _flag=$1
    shift
    case "$GEN_COMMAND:$_flag" in
        *:--keep)
            [ $# -ge 1 ] || die "--keep needs a value"
            printf '%s' "$1" | grep -Eqx '[1-9][0-9]{0,3}' || die "--keep takes a whole number from 1 (keeping 0 is not supported)"
            GEN_ARGS="$GEN_ARGS --keep $1"
            shift ;;
        list:--format)
            [ $# -ge 1 ] || die "--format needs a value"
            case "$1" in text|json) ;; *) die "--format takes text or json" ;; esac
            GEN_ARGS="$GEN_ARGS --format $1"
            shift ;;
        dispose-snapshots:--build-id)
            [ $# -ge 1 ] || die "--build-id needs a value"
            printf '%s' "$1" | grep -Eqx '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' ||
                die "--build-id takes a full lowercase build UUID (see the listing), never a collection name"
            GEN_ARGS="$GEN_ARGS --build-id $1"
            gen_builds=$((gen_builds + 1))
            shift ;;
        dispose-snapshots:--allow-below-keep)
            GEN_ARGS="$GEN_ARGS --allow-below-keep" ;;
        dispose-snapshots:--execute)
            gen_execute=true
            GEN_ARGS="$GEN_ARGS --execute" ;;
        dispose-snapshots:--recovery-ref)
            [ $# -ge 1 ] || die "--recovery-ref needs a value"
            printf '%s' "$1" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}' ||
                die "--recovery-ref takes a ticket or decision id ([A-Za-z0-9._:/#-], at most 128 characters)"
            GEN_ARGS="$GEN_ARGS --recovery-ref $1"
            shift ;;
        *) die "unknown or misplaced argument for $GEN_COMMAND: $_flag ($GENERATIONS_USAGE)" ;;
    esac
done
unset _flag
if [ "$GEN_COMMAND" = dispose-snapshots ]; then
    [ "$gen_builds" -ge 1 ] || die "dispose-snapshots needs at least one --build-id"
    if [ "$gen_execute" = true ]; then
        # The audit record names who ran it (maintainer decision 2026-11-26);
        # an unusable login name is left to the module's uid fallback.
        _operator=${USER:-$(id -un 2>/dev/null || true)}
        if printf '%s' "$_operator" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9._@-]{0,63}'; then
            GEN_ARGS="$GEN_ARGS --operator $_operator"
        else
            echo "::warning::the bastion login name is not usable as an audit name ([A-Za-z0-9._@-]); the audit record names the pod's uid instead" >&2
        fi
        unset _operator
    fi
fi

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
set -f
# shellcheck disable=SC2086 # validated tokens, see above
set -- $GEN_ARGS
set +f
_map_args=""
for _arg in "$@"; do
    _map_args="$_map_args --generations-arg $_arg"
done
set -f
# shellcheck disable=SC2086
AGENT_ROUTE=false OPENWEBUI_ENABLED=false SPLUNK_GATEWAY_ENABLED=false \
    map_app_values --out dist/mainframe-rag-generations-values.yaml $_map_args
set +f
unset _arg _map_args
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
watch_one_shot_job generations "$GENERATIONS_TIMEOUT" airgap:generations collect
