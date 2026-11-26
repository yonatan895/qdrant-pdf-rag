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
# WebUI), so the Job render opts out of them explicitly.
AGENT_ROUTE=false OPENWEBUI_ENABLED=false SPLUNK_GATEWAY_ENABLED=false map_app_values --out dist/mainframe-rag-ingest-values.yaml
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

# Quota refusal (#1378). The Job controller's FailedCreate event message
# carries the quota name and its requested/used/limited figures; it is read
# here and never echoed. Prints the INGEST_* keys the refused resources map
# to, "other" for a quota resource those keys do not size, and nothing when
# no line is a quota refusal.
ingest_quota_keys() {
    awk '
        /forbidden: failed quota: / { print "other"; next }
        /exceeded quota/ {
            line = $0
            if (!sub(/.*requested: /, "", line)) { print "other"; next }
            sub(/, used: .*/, "", line)
            n = split(line, parts, ",")
            for (i = 1; i <= n; i++) {
                res = parts[i]
                sub(/=.*/, "", res)
                gsub(/[ \t]/, "", res)
                if (res == "requests.cpu" || res == "cpu") print "INGEST_CPU_REQUEST"
                else if (res == "requests.memory" || res == "memory") print "INGEST_MEMORY_REQUEST"
                else if (res == "limits.cpu") print "INGEST_CPU_LIMIT"
                else if (res == "limits.memory") print "INGEST_MEMORY_LIMIT"
                else print "other"
            }
        }' "$1" | LC_ALL=C sort -u
}

# Fixed operator message for a quota refusal: resource names and keys only,
# never figures from the cluster.
report_ingest_quota_refusal() {
    _keys=""
    _what=""
    for _key in INGEST_CPU_REQUEST INGEST_MEMORY_REQUEST INGEST_CPU_LIMIT INGEST_MEMORY_LIMIT; do
        grep -qx "$_key" "$1" || continue
        case "$_key" in
            INGEST_CPU_REQUEST) _label="CPU requests" ;;
            INGEST_MEMORY_REQUEST) _label="memory requests" ;;
            INGEST_CPU_LIMIT) _label="CPU limits" ;;
            INGEST_MEMORY_LIMIT) _label="memory limits" ;;
        esac
        _keys="${_keys:+$_keys, }$_key"
        _what="${_what:+$_what, }$_label"
    done
    echo "::error::the namespace ResourceQuota refused the ingest Job's pod (FailedCreate: exceeded quota); the Job never started" >&2
    if [ -n "$_keys" ]; then
        echo "::error::refused on $_what: lower $_keys in airgap.env (docs/deploy.md, site sizing keys) or free that quota in namespace $NAMESPACE, then rerun airgap:ingest" >&2
    fi
    if grep -qx other "$1"; then
        echo "::error::refused on a quota resource the INGEST_* sizing keys do not set: free that quota in namespace $NAMESPACE, then rerun airgap:ingest" >&2
    fi
    echo "::warning::job/ingest is left in namespace $NAMESPACE and its controller keeps retrying; a rerun replaces it, or remove it with: $KC -n $NAMESPACE delete job ingest" >&2
    unset _keys _what _key _label
}

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $KC -n $NAMESPACE wait --for=condition=complete job/ingest --timeout=${INGEST_TIMEOUT}s"
    echo "[dryrun] rendered manifest kept at dist/ingest-rendered.yaml"
else
    echo "==> Waiting for ingest Job (timeout: ${INGEST_TIMEOUT}s)..."
    LOGS_PID=""
    WAIT_PID=""
    QUOTA_PID=""
    cancelled=0
    # Quota fail-fast (#1378): a namespace ResourceQuota that cannot take the
    # Job's pod leaves the Job controller retrying FailedCreate until the
    # timeout. The check reads only this run's events: the Job name is reused
    # and a deleted Job's events outlive it, so it is scoped to the new Job's
    # UID and stays off when the UID or the events cannot be read (the wait
    # alone then decides, as before).
    QUOTA_MARKER=dist/ingest-quota-refused
    rm -f "$QUOTA_MARKER"
    INGEST_JOB_UID=$($KC -n "$NAMESPACE" get job ingest -o jsonpath='{.metadata.uid}' 2>/dev/null) || INGEST_JOB_UID=""
    stop_ingest_observers() {
        # Cleanup owns only these local processes, never the cluster Job.
        trap '' TERM INT
        for observer_pid in "$WAIT_PID" "$LOGS_PID" "$QUOTA_PID"; do
            [ -n "$observer_pid" ] || continue
            kill "$observer_pid" 2>/dev/null || true
        done
        for observer_pid in "$WAIT_PID" "$LOGS_PID" "$QUOTA_PID"; do
            [ -n "$observer_pid" ] || continue
            wait "$observer_pid" 2>/dev/null || true
        done
        WAIT_PID=""
        LOGS_PID=""
        QUOTA_PID=""
    }
    exit_if_cancelled() {
        [ "$cancelled" -ne 0 ] || return 0
        echo "::warning::observer cancelled; job/ingest in namespace $NAMESPACE may still be running. Reconcile it explicitly before replacing the writer." >&2
        exit "$cancelled"
    }
    # Flag first, then check after assigning each $!: cancellation during
    # child setup must not lose ownership of a just-created process.
    # The handler also stops the owned observers: a signal whose trap runs
    # after the last check but before `wait` would otherwise block until the
    # Job wait ends (up to INGEST_TIMEOUT), losing the operator's Ctrl-C.
    interrupt_ingest_observers() {
        for observer_pid in "$WAIT_PID" "$LOGS_PID" "$QUOTA_PID"; do
            [ -n "$observer_pid" ] || continue
            kill "$observer_pid" 2>/dev/null || true
        done
    }
    trap 'cancelled=143; interrupt_ingest_observers' TERM
    trap 'cancelled=130; interrupt_ingest_observers' INT
    trap stop_ingest_observers 0
    # Follow each pod once, including a replacement after a Job retry. The
    # Job wait remains authoritative; this is only a log overlay.
    (
        # A signal sets a flag rather than exiting between fork and assigning
        # $!: the exit handler can then always reap the owned stream.
        stopping=false
        stream_pid=""
        pod_rows_file=""
        # Same lost-wakeup guard as the parent: a stop that lands between a
        # check and `wait` also ends the blocking stream.
        trap 'stopping=true; if [ -n "$stream_pid" ]; then kill "$stream_pid" 2>/dev/null || true; fi' TERM INT
        trap 'trap "" TERM INT; if [ -n "$stream_pid" ]; then kill "$stream_pid" 2>/dev/null || true; wait "$stream_pid" 2>/dev/null || true; fi; if [ -n "$pod_rows_file" ]; then rm -f "$pod_rows_file"; fi' 0
        pod_rows_file=$(mktemp)
        followed_pods=" "
        while [ "$stopping" = false ]; do
            $KC -n "$NAMESPACE" get pods -l job-name=ingest \
                --sort-by=.metadata.creationTimestamp \
                -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' > "$pod_rows_file" 2>/dev/null &
            stream_pid=$!
            [ "$stopping" = false ] || break
            wait "$stream_pid" || true
            [ "$stopping" = false ] || break
            stream_pid=""
            while read -r pod_name pod_phase; do
                [ "$stopping" = false ] || break
                [ -n "$pod_name" ] || continue
                case "$followed_pods" in *" $pod_name "*) continue ;; esac
                case "$pod_phase" in Running|Succeeded|Failed) ;; *) continue ;; esac
                $KC -n "$NAMESPACE" logs -f "$pod_name" 2>/dev/null &
                stream_pid=$!
                [ "$stopping" = false ] || break
                wait "$stream_pid" || true
                [ "$stopping" = false ] || break
                stream_pid=""
                followed_pods="$followed_pods$pod_name "
            done < "$pod_rows_file"
            [ "$stopping" = false ] || break
            sleep 2 &
            stream_pid=$!
            [ "$stopping" = false ] || break
            wait "$stream_pid" || true
            [ "$stopping" = false ] || break
            stream_pid=""
        done
    ) &
    LOGS_PID=$!
    exit_if_cancelled
    $KC -n "$NAMESPACE" wait --for=condition=complete job/ingest --timeout="${INGEST_TIMEOUT}s" &
    WAIT_PID=$!
    exit_if_cancelled
    if [ -n "$INGEST_JOB_UID" ]; then
        # Started after WAIT_PID so it can end the wait. It acts only while
        # the Job has never had a pod: a refusal that a later pod overtook
        # must not stop a running ingest. Marker first, then the kill, so the
        # parent can tell this stop from a genuine wait failure.
        (
            stopping=false
            probe_pid=""
            probe_file=""
            trap 'stopping=true; if [ -n "$probe_pid" ]; then kill "$probe_pid" 2>/dev/null || true; fi' TERM INT
            trap 'trap "" TERM INT; if [ -n "$probe_pid" ]; then kill "$probe_pid" 2>/dev/null || true; wait "$probe_pid" 2>/dev/null || true; fi; if [ -n "$probe_file" ]; then rm -f "$probe_file"; fi' 0
            probe_file=$(mktemp)
            while [ "$stopping" = false ]; do
                $KC -n "$NAMESPACE" get events \
                    --field-selector "involvedObject.uid=$INGEST_JOB_UID,reason=FailedCreate" \
                    -o jsonpath='{range .items[*]}{.message}{"\n"}{end}' > "$probe_file" 2>/dev/null &
                probe_pid=$!
                [ "$stopping" = false ] || break
                wait "$probe_pid" || : > "$probe_file"
                [ "$stopping" = false ] || break
                probe_pid=""
                refused=$(ingest_quota_keys "$probe_file")
                if [ -n "$refused" ]; then
                    # "Never had a pod" needs both views: the Job's counters
                    # lag pod creation and finished pods (review F1), and a
                    # pod list misses pods already removed. Any read failure
                    # counts as "a pod may exist".
                    pods_seen=""
                    $KC -n "$NAMESPACE" get job ingest \
                        -o jsonpath='{.status.active}{" "}{.status.succeeded}{" "}{.status.failed}{" "}{.status.uncountedTerminatedPods}' > "$probe_file" 2>/dev/null &
                    probe_pid=$!
                    [ "$stopping" = false ] || break
                    if wait "$probe_pid"; then
                        pods_seen=$(tr -d ' 0{}\n' < "$probe_file")
                    else
                        pods_seen=unknown
                    fi
                    [ "$stopping" = false ] || break
                    probe_pid=""
                    # The pod list is read last: it is the freshest view, so
                    # only a pod created between it and the kill can be missed.
                    if [ -z "$pods_seen" ]; then
                        $KC -n "$NAMESPACE" get pods -l "batch.kubernetes.io/controller-uid=$INGEST_JOB_UID" \
                            -o name > "$probe_file" 2>/dev/null &
                        probe_pid=$!
                        [ "$stopping" = false ] || break
                        if wait "$probe_pid"; then
                            pods_seen=$(tr -d ' \n' < "$probe_file")
                        else
                            pods_seen=unknown
                        fi
                        [ "$stopping" = false ] || break
                        probe_pid=""
                    fi
                    if [ -z "$pods_seen" ]; then
                        printf '%s\n' "$refused" > "$QUOTA_MARKER"
                        kill "$WAIT_PID" 2>/dev/null || true
                        break
                    fi
                fi
                sleep 5 &
                probe_pid=$!
                [ "$stopping" = false ] || break
                wait "$probe_pid" || true
                [ "$stopping" = false ] || break
                probe_pid=""
            done
        ) &
        QUOTA_PID=$!
        exit_if_cancelled
    fi
    wait_result=0
    wait "$WAIT_PID" || wait_result=$?
    exit_if_cancelled
    WAIT_PID=""
    stop_ingest_observers
    if [ "$wait_result" -ne 0 ] && [ -s "$QUOTA_MARKER" ]; then
        report_ingest_quota_refusal "$QUOTA_MARKER"
        rm -f "$QUOTA_MARKER"
        exit 1
    fi
    if [ "$wait_result" -ne 0 ]; then
        echo "::error::ingest Job did not complete successfully" >&2
        $KC -n "$NAMESPACE" logs job/ingest --tail=200 || true
        $KC -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -30 || true
        exit 1
    fi
fi

next_step "sh scripts/tools/run-task.sh airgap:smoke"
