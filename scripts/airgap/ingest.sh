#!/bin/sh
# AIR-GAP SIDE (issue #15): one-shot ingest Job against the PROD stack.
#
#   sh scripts/tools/run-task.sh airgap:ingest CORPUS_PVC=<existing-pvc>
#
# The corpus PVC is caller-supplied (back it with NFS RO, block, whatever the
# platform team provides) and is mounted READ-ONLY at /corpus. No demo PDFs,
# no EMBED_MODE=hash — prod embeds via the in-cluster vLLM endpoint.

. "$(dirname -- "$0")/common.sh"
. "$(dirname -- "$0")/image_identity.sh"
. "$(dirname -- "$0")/one_shot_job.sh"

enforce_product_rules
resolve_aliases
require_env INTERNAL_REGISTRY NAMESPACE IMAGE_SHA CORPUS_PVC EMBED_MODEL DENSE_DIM EMBED_MODEL_REVISION EMBED_BASE_URL STORAGE_CLASS
require_embed_revision
check_secret_name "${GATEWAY_API_KEY_SECRET:-}" GATEWAY_API_KEY_SECRET
check_secret_name "${PULL_SECRET:-}" PULL_SECRET
check_secret_name "${GATEWAY_CA_CONFIGMAP:-}" GATEWAY_CA_CONFIGMAP
resolve_observability
case "$IMAGE_SHA" in
    ""|HEAD) die "IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)" ;;
esac
# Cross-check against the packed MANIFEST when it is reachable (dist/ or ../).
MANIFEST=$(find_manifest)
check_manifest_sha
# The executing checkout itself must resolve to the packed SHA (issue #414):
# an explicitly set IMAGE_SHA alone never changes which code executes.
check_checkout_sha
# A gap bundle ingests only as a declared rehearsal or promotion (#1338).
check_gap_release_stage "$MANIFEST"
require_kc
refuse_nfs_storage
KC=${KC:-$(kc)}
check_gateway_ca
# The Job image must be the packed one (issue #272); it is then rendered as
# repository@digest. Verified before any cluster change.
verify_registry_images ingest

QDRANT_URL="http://${QDRANT_RELEASE}:6333"
INGEST_TIMEOUT=${INGEST_TIMEOUT:-3600}
INGEST_WORK_SIZE=${INGEST_WORK_SIZE:-100Gi}   # CI-rehearsal knob; default = prod size
# Collection distribution policy (issue #360): the production path requires
# one complete validated tuple (checked-in preset 6/3/2 with caller > file >
# preset precedence; one-node lanes select 1/1/1 explicitly). Partial,
# unknown or impossible policies fail closed here, before rendering or any
# cluster mutation. Migration of existing collections is out of scope
# (later slice), never automatic recreation.
validate_collection_policy required
mkdir -p dist

# Maintenance modes (issue #391 current packet): the operator launcher is
# the one supported path, so repair/removal flags travel through it with
# validation instead of ad-hoc applies. One Job name and the shared
# ingest-work PVC keep the host-local target lock meaningful (one
# authorized publisher, shared progress path); no distributed lock is
# claimed, and aliases/defaults are never flipped implicitly. Alias
# publication is the only ingest mode (in-place was retired, #360; the
# values mapper refuses INGEST_ALIAS_PUBLISH=false).
#
# Retirement entries accept the real backend alphabet: a source_rev is
# `vendor|product|version|sha256` (labels may carry '/', '|' and spaces),
# so validation rejects only what would break the shell or the rendered
# YAML — control characters, quotes, backslashes and wildcards — and the
# backend still refuses any revision absent from the approved inventory.
REINGEST=$(bool_flag INGEST_REINGEST false)
if [ -n "${INGEST_RETIRE_DOCS:-}" ]; then
    _old_ifs=$IFS
    _nl='
'
    IFS=",$_nl"
    set -f  # operator input is data, never a pathname pattern (review F3)
    for _entry in $INGEST_RETIRE_DOCS; do
        _entry=$(printf '%s' "$_entry" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        [ -n "$_entry" ] || continue
        case "$_entry" in
            @*|*@) die "malformed INGEST_RETIRE_DOCS entry '$_entry': expected DOCID or DOCID@SOURCEREV (empty side of '@')" ;;
            *'"'*|*\\*|*\**|*\?*) die "malformed INGEST_RETIRE_DOCS entry '$_entry': quotes, backslashes and wildcards are not allowed" ;;
        esac
        if printf '%s' "$_entry" | LC_ALL=C grep -q '[[:cntrl:]]'; then
            die "malformed INGEST_RETIRE_DOCS entry: control characters are not allowed"
        fi
    done
    set +f
    IFS=$_old_ifs
    unset _old_ifs _nl _entry
fi

echo "==> Helm: explicit ingest Job (corpus PVC: $CORPUS_PVC)"
# Route configuration belongs to deployment, not this one-shot operation; so
# does the Open WebUI front end (its Route needs the service CA that only
# deploy fetches) and its team tools (#1077: the Splunk gateway needs Open
# WebUI), so the Job render opts out of them explicitly; so does the z/OS
# bridge (#1294: with Open WebUI off its role guard would read no role list).
AGENT_ROUTE=false OPENWEBUI_ENABLED=false SPLUNK_GATEWAY_ENABLED=false ZOS_READ_ENABLED=false map_app_values --out dist/mainframe-rag-ingest-values.yaml
helm template mainframe-rag charts/mainframe-rag -f dist/mainframe-rag-ingest-values.yaml \
    --namespace "$NAMESPACE" --show-only templates/ingest-job.yaml > dist/ingest-rendered.yaml
helm template mainframe-rag charts/mainframe-rag -f dist/mainframe-rag-ingest-values.yaml \
    --namespace "$NAMESPACE" --show-only templates/ingest-work-pvc.yaml > dist/ingest-work-rendered.yaml
fail_on_placeholders dist/ingest-rendered.yaml ingest
# CI-rehearsal knob (never set in the air gap): strategic-merge a patch into
# the rendered Job — e.g. lab-quota resources — without touching the prod
# production template in git. Client-side only; the cluster is not contacted.
INGEST_EXTRA_PATCH=${INGEST_EXTRA_PATCH:-}
if [ -n "$INGEST_EXTRA_PATCH" ]; then
    require_ci_rehearsal INGEST_EXTRA_PATCH "$INGEST_SIZING_KEYS"
    [ -f "$INGEST_EXTRA_PATCH" ] || die "INGEST_EXTRA_PATCH file not found: $INGEST_EXTRA_PATCH"
    $KC patch --local -f dist/ingest-rendered.yaml \
        -p "$(cat "$INGEST_EXTRA_PATCH")" -o yaml > dist/ingest-rendered-patched.yaml
    # Re-validate after the patch: only container resources may differ, and
    # no hash-embedding env may appear (#1014).
    python3 scripts/airgap/check_ingest_patch.py dist/ingest-rendered.yaml dist/ingest-rendered-patched.yaml ||
        die "INGEST_EXTRA_PATCH may only change container resources (the patched Job differs elsewhere)"
    mv dist/ingest-rendered-patched.yaml dist/ingest-rendered.yaml
fi
check_ingest_qdrant_key dist/ingest-rendered.yaml ingest
require_gateway_secret_keys embed-api-key context-llm-api-key
require_secret_keys "${PULL_SECRET:-}" .dockerconfigjson

# The external scratch PVC is deliberately not owned by the app release.
# Render it from the same chart, and create only when absent.
if [ "${AIRGAP_DRYRUN:-0}" != "1" ]; then
    _scratch=$($KC -n "$NAMESPACE" get pvc ingest-work --ignore-not-found -o name) || die "cannot check ingest-work PVC"
    if [ -z "$_scratch" ]; then
        $KC apply -f dist/ingest-work-rendered.yaml
    fi
fi
# Jobs are immutable. Wait for the old pods as well as their Job to disappear:
# a terminating pod may still hold the shared progress/publisher lock.
if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $KC -n $NAMESPACE delete job ingest --ignore-not-found --cascade=foreground --wait=true"
else
    $KC -n "$NAMESPACE" delete job ingest --ignore-not-found --cascade=foreground --wait=true
fi
run $KC apply -f dist/ingest-rendered.yaml

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $KC -n $NAMESPACE wait --for=condition=complete job/ingest --timeout=${INGEST_TIMEOUT}s"
    echo "[dryrun] rendered manifest kept at dist/ingest-rendered.yaml"
else
    watch_one_shot_job ingest "$INGEST_TIMEOUT" airgap:ingest follow
fi

next_step "sh scripts/tools/run-task.sh airgap:smoke"
