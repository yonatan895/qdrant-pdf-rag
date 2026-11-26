# Shared watcher for one-shot Jobs launched from the bastion (sourced by
# ingest.sh and generations.sh; needs common.sh, KC and NAMESPACE).
#
# watch_one_shot_job JOB TIMEOUT TASK MODE
#   Waits for job/JOB to complete, with the quota fail-fast (#1378) and the
#   bounded, interruptible removal of a refused Job (#1385). MODE follow
#   streams each pod's log while waiting (ingest); MODE collect does not,
#   ends the wait as soon as the Job fails (a refusal is a routine outcome
#   with backoffLimit 0) and prints the Job's log once at the end. Returns 0
#   when the Job completed; exits non-zero otherwise. Cleanup owns only the
#   local observer processes, never the cluster Job, except the refused Job.

# Quota refusal (#1378). The Job controller's FailedCreate event message
# carries the quota name and its requested/used/limited figures; it is read
# here and never echoed. Prints the INGEST_* keys the refused resources map
# to, "other" for a quota resource those keys do not size, and nothing when
# no line is a quota refusal.
one_shot_quota_keys() {
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
report_one_shot_quota_refusal() {
    # $1 marker file, $2 Job name, $3 rerun task
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
    echo "::error::the namespace ResourceQuota refused the $2 Job's pod (FailedCreate: exceeded quota); the Job never started" >&2
    if [ -n "$_keys" ]; then
        echo "::error::refused on $_what: lower $_keys in airgap.env (docs/deploy.md, site sizing keys) or free that quota in namespace $NAMESPACE, then rerun $3" >&2
    fi
    if grep -qx other "$1"; then
        echo "::error::refused on a quota resource the INGEST_* sizing keys do not set: free that quota in namespace $NAMESPACE, then rerun $3" >&2
    fi
    unset _keys _what _key _label
}

watch_one_shot_job() {
    _osj_job=$1
    _osj_timeout=$2
    _osj_task=$3
    _osj_mode=$4
    echo "==> Waiting for $_osj_job Job (timeout: ${_osj_timeout}s)..."
    LOGS_PID=""
    WAIT_PID=""
    QUOTA_PID=""
    FAILED_PID=""
    cancelled=0
    # Quota fail-fast (#1378): a namespace ResourceQuota that cannot take the
    # Job's pod leaves the Job controller retrying FailedCreate until the
    # timeout. The check reads only this run's events: the Job name is reused
    # and a deleted Job's events outlive it, so it is scoped to the new Job's
    # UID and stays off when the UID or the events cannot be read (the wait
    # alone then decides, as before).
    QUOTA_MARKER=dist/$_osj_job-quota-refused
    FAILED_MARKER=dist/$_osj_job-failed
    rm -f "$QUOTA_MARKER" "$FAILED_MARKER"
    OSJ_JOB_UID=$($KC -n "$NAMESPACE" get job "$_osj_job" -o jsonpath='{.metadata.uid}' 2>/dev/null) || OSJ_JOB_UID=""
    stop_job_observers() {
        # Cleanup owns only these local processes, never the cluster Job.
        trap '' TERM INT
        for observer_pid in "$WAIT_PID" "$LOGS_PID" "$QUOTA_PID" "$FAILED_PID"; do
            [ -n "$observer_pid" ] || continue
            kill "$observer_pid" 2>/dev/null || true
        done
        for observer_pid in "$WAIT_PID" "$LOGS_PID" "$QUOTA_PID" "$FAILED_PID"; do
            [ -n "$observer_pid" ] || continue
            wait "$observer_pid" 2>/dev/null || true
        done
        WAIT_PID=""
        LOGS_PID=""
        QUOTA_PID=""
        FAILED_PID=""
    }
    exit_if_cancelled() {
        [ "$cancelled" -ne 0 ] || return 0
        echo "::warning::observer cancelled; job/$_osj_job in namespace $NAMESPACE may still be running. Reconcile it explicitly before replacing the writer." >&2
        exit "$cancelled"
    }
    # Flag first, then check after assigning each $!: cancellation during
    # child setup must not lose ownership of a just-created process.
    # The handler also stops the owned observers: a signal whose trap runs
    # after the last check but before `wait` would otherwise block until the
    # Job wait ends (up to INGEST_TIMEOUT), losing the operator's Ctrl-C.
    interrupt_job_observers() {
        for observer_pid in "$WAIT_PID" "$LOGS_PID" "$QUOTA_PID" "$FAILED_PID"; do
            [ -n "$observer_pid" ] || continue
            kill "$observer_pid" 2>/dev/null || true
        done
    }
    trap 'cancelled=143; interrupt_job_observers' TERM
    trap 'cancelled=130; interrupt_job_observers' INT
    trap stop_job_observers 0
    # Follow each pod once, including a replacement after a Job retry. The
    # Job wait remains authoritative; this is only a log overlay. A short
    # Job (mode collect) is not followed: its log is printed once at the end.
    if [ "$_osj_mode" = follow ]; then
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
            $KC -n "$NAMESPACE" get pods -l "job-name=$_osj_job" \
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
    fi
    $KC -n "$NAMESPACE" wait --for=condition=complete "job/$_osj_job" --timeout="${_osj_timeout}s" &
    WAIT_PID=$!
    exit_if_cancelled
    if [ "$_osj_mode" = collect ]; then
        # A failed Job never becomes complete, so the wait above would run
        # to the timeout: a routine refusal (backoffLimit 0) ends it here.
        (
            stopping=false
            probe_pid=""
            trap 'stopping=true; if [ -n "$probe_pid" ]; then kill "$probe_pid" 2>/dev/null || true; fi' TERM INT
            trap 'trap "" TERM INT; if [ -n "$probe_pid" ]; then kill "$probe_pid" 2>/dev/null || true; wait "$probe_pid" 2>/dev/null || true; fi' 0
            $KC -n "$NAMESPACE" wait --for=condition=failed "job/$_osj_job" --timeout="${_osj_timeout}s" >/dev/null 2>&1 &
            probe_pid=$!
            [ "$stopping" = false ] || exit 0
            if wait "$probe_pid" && [ "$stopping" = false ]; then
                probe_pid=""
                : > "$FAILED_MARKER"
                kill "$WAIT_PID" 2>/dev/null || true
            fi
            probe_pid=""
        ) &
        FAILED_PID=$!
        exit_if_cancelled
    fi
    if [ -n "$OSJ_JOB_UID" ]; then
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
                    --field-selector "involvedObject.uid=$OSJ_JOB_UID,reason=FailedCreate" \
                    -o jsonpath='{range .items[*]}{.message}{"\n"}{end}' > "$probe_file" 2>/dev/null &
                probe_pid=$!
                [ "$stopping" = false ] || break
                wait "$probe_pid" || : > "$probe_file"
                [ "$stopping" = false ] || break
                probe_pid=""
                refused=$(one_shot_quota_keys "$probe_file")
                if [ -n "$refused" ]; then
                    # "Never had a pod" needs both views: the Job's counters
                    # lag pod creation and finished pods (review F1), and a
                    # pod list misses pods already removed. Any read failure
                    # counts as "a pod may exist".
                    pods_seen=""
                    $KC -n "$NAMESPACE" get job "$_osj_job" \
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
                        $KC -n "$NAMESPACE" get pods -l "batch.kubernetes.io/controller-uid=$OSJ_JOB_UID" \
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
    stop_job_observers
    if [ "$wait_result" -ne 0 ] && [ -s "$QUOTA_MARKER" ]; then
        report_one_shot_quota_refusal "$QUOTA_MARKER" "$_osj_job" "$_osj_task"
        rm -f "$QUOTA_MARKER"
        # Maintainer decision (#1385): a
        # refused Job left behind could start by itself when quota frees and
        # ingest and publish with nobody watching, so it is removed. The check
        # found no pod; a pod created since goes with the Job, and the run
        # still reports failure.
        # stop_job_observers ignored TERM/INT; the delete must stay
        # interruptible (review B1). It runs in the background so a trap can
        # end it at once; --request-timeout bounds each stalled API request
        # and --timeout the wait for the deletion.
        DELETE_PID=""
        interrupt_refused_job_delete() {
            trap '' TERM INT
            if [ -n "$DELETE_PID" ]; then
                kill "$DELETE_PID" 2>/dev/null || true
                wait "$DELETE_PID" 2>/dev/null || true
            fi
            echo "::error::interrupted while removing the refused $_osj_job Job; delete it with: $KC -n $NAMESPACE delete job $_osj_job" >&2
            exit "$1"
        }
        trap 'interrupt_refused_job_delete 143' TERM
        trap 'interrupt_refused_job_delete 130' INT
        $KC -n "$NAMESPACE" delete job "$_osj_job" --ignore-not-found --cascade=foreground --wait=true \
            --timeout=300s --request-timeout=30s >/dev/null 2>&1 &
        DELETE_PID=$!
        delete_result=0
        wait "$DELETE_PID" || delete_result=$?
        trap '' TERM INT
        DELETE_PID=""
        if [ "$delete_result" -eq 0 ]; then
            echo "::error::the refused $_osj_job Job was removed; rerun $_osj_task after lowering INGEST_* or freeing quota" >&2
        else
            echo "::error::could not remove the refused $_osj_job Job; delete it with: $KC -n $NAMESPACE delete job $_osj_job" >&2
        fi
        exit 1
    fi
    rm -f "$FAILED_MARKER"
    if [ "$_osj_mode" = collect ]; then
        echo "==> Output of job/$_osj_job"
        $KC -n "$NAMESPACE" logs "job/$_osj_job" 2>/dev/null ||
            echo "::warning::job/$_osj_job has no readable log (no pod ran, or it was removed)" >&2
    fi
    if [ "$wait_result" -ne 0 ]; then
        echo "::error::$_osj_job Job did not complete successfully" >&2
        if [ "$_osj_mode" = follow ]; then
            $KC -n "$NAMESPACE" logs "job/$_osj_job" --tail=200 || true
        fi
        $KC -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -30 || true
        exit 1
    fi
    return 0
}
