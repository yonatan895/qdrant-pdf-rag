#!/bin/sh
# AIR-GAP SIDE (issue #15): one-shot ingest Job against the PROD stack.
#
#   sh scripts/tools/run-task.sh airgap:ingest CORPUS_PVC=<existing-pvc>
#
# The corpus PVC is caller-supplied (back it with NFS RO, block, whatever the
# platform team provides) and is mounted READ-ONLY at /corpus. No demo PDFs,
# no EMBED_MODE=hash — prod embeds via the in-cluster vLLM endpoint.

. "$(dirname -- "$0")/common.sh"

enforce_product_rules
resolve_aliases
require_env INTERNAL_REGISTRY NAMESPACE IMAGE_SHA CORPUS_PVC EMBED_MODEL DENSE_DIM EMBED_MODEL_REVISION VLLM_BASE_URL STORAGE_CLASS
require_embed_revision
check_secret_name "${GATEWAY_API_KEY_SECRET:-}" GATEWAY_API_KEY_SECRET
check_secret_name "${PULL_SECRET:-}" PULL_SECRET
check_secret_name "${GATEWAY_CA_CONFIGMAP:-}" GATEWAY_CA_CONFIGMAP
resolve_otel_endpoint
case "$IMAGE_SHA" in
    ""|HEAD) die "IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)" ;;
esac
# Cross-check against the packed MANIFEST when it is reachable (dist/ or ../).
MANIFEST=$(find_manifest)
check_manifest_sha
require_kc
refuse_nfs_storage
KC=${KC:-$(kc)}
check_gateway_ca

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

if [ "${AIRGAP_DRYRUN:-0}" != "1" ] && ! $KC -n "$NAMESPACE" get pvc ingest-work >/dev/null 2>&1; then
    echo "==> Create ingest-work PVC (scratch + inventory)"
    $KC apply -n "$NAMESPACE" -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ingest-work
  namespace: $NAMESPACE
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: $INGEST_WORK_SIZE
  storageClassName: $STORAGE_CLASS
EOF
fi

# Maintenance modes (issue #391 current packet): the operator launcher is
# the one supported path, so repair/removal flags travel through it with
# validation instead of ad-hoc applies. One Job name and the shared
# ingest-work PVC keep the host-local target lock meaningful (one
# authorized publisher, shared progress path); no distributed lock is
# claimed, and aliases/defaults are never flipped implicitly.
#
# Retirement entries accept the real backend alphabet: a source_rev is
# `vendor|product|version|sha256` (labels may carry '/', '|' and spaces),
# so validation rejects only what would break the shell or the rendered
# YAML — control characters, quotes, backslashes and wildcards — and the
# backend still refuses any revision absent from the approved inventory.
ALIAS_PUBLISH=$(bool_flag INGEST_ALIAS_PUBLISH false)
REINGEST=$(bool_flag INGEST_REINGEST false)
INGEST_ARGS='"--src", "/corpus", "--progress", "/work/inventory.jsonl"'
if [ "$REINGEST" = "true" ]; then
    INGEST_ARGS="$INGEST_ARGS, \"--reingest\""
fi
if [ -n "${INGEST_RETIRE_DOCS:-}" ]; then
    [ "$ALIAS_PUBLISH" = "true" ] || die "INGEST_RETIRE_DOCS requires INGEST_ALIAS_PUBLISH=true — explicit removals are a publication operation and the ingest refuses them in-place"
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
        INGEST_ARGS="$INGEST_ARGS, \"--retire-doc\", \"$_entry\""
    done
    set +f
    IFS=$_old_ifs
    unset _old_ifs _nl _entry
fi
INGEST_ARGS="[$INGEST_ARGS]"

echo "==> Kustomize: prod ingest Job (corpus PVC: $CORPUS_PVC)"
INGEST_WORKERS=${INGEST_WORKERS:-4}
kustomize_render deploy/kustomize/overlays/openshift-ingest | sed -E 's|"(__[A-Z0-9_]+__)"|\1|g' | sed \
    -e "s|__INTERNAL_REGISTRY__|$INTERNAL_REGISTRY|g" \
    -e "s|__IMAGE_SHA__|$IMAGE_SHA|g" \
    -e "s|namespace: mainframe-rag|namespace: $NAMESPACE|g" \
    -e "s|__QDRANT_URL__|$QDRANT_URL|g" \
    -e "s|__QDRANT_RELEASE__|$QDRANT_RELEASE|g" \
    -e "s|__QDRANT_SHARD_NUMBER__|\"${QDRANT_SHARD_NUMBER:-}\"|g" \
    -e "s|__QDRANT_REPLICATION_FACTOR__|\"${QDRANT_REPLICATION_FACTOR:-}\"|g" \
    -e "s|__QDRANT_WRITE_CONSISTENCY_FACTOR__|\"${QDRANT_WRITE_CONSISTENCY_FACTOR:-}\"|g" \
    -e "s|__EMBED_BASE_URL__|$EMBED_BASE_URL|g" \
    -e "s|__EMBED_MODEL__|$EMBED_MODEL|g" \
    -e "s|__EMBED_MODEL_REVISION__|$EMBED_MODEL_REVISION|g" \
    -e "s|__DENSE_DIM__|\"$DENSE_DIM\"|g" \
    -e "s|__CORPUS_PVC__|$CORPUS_PVC|g" \
    -e "s|__INGEST_WORKERS__|\"$INGEST_WORKERS\"|g" \
    -e "s|__INGEST_ALIAS_PUBLISH__|\"$ALIAS_PUBLISH\"|g" \
    -e "s|__CONTEXTUAL_EMBED_ENABLED__|\"${CONTEXTUAL_EMBED_ENABLED:-false}\"|g" \
    -e "s|__CONTEXT_LLM_BASE_URL__|${CONTEXT_LLM_BASE_URL:-}|g" \
    -e "s|__CONTEXT_LLM_MODEL__|${CONTEXT_LLM_MODEL:-}|g" \
    -e "s|__OTEL_EXPORTER_OTLP_ENDPOINT__|${OTEL_ENDPOINT_RESOLVED}|g" \
    -e "s|__OTEL_DEPLOYMENT_ENVIRONMENT__|${OTEL_DEPLOYMENT_ENVIRONMENT:-}|g" \
    > dist/ingest-rendered.yaml
# Literal insertion for the operator-visible args (review F2): real source
# revisions contain '|', '/', spaces and '&', which sed delimiters and
# replacements would reinterpret. awk index/substr copies bytes verbatim;
# control characters are rejected above, so the placeholder stays on one line.
INGEST_ARGS="$INGEST_ARGS" awk '
    { i = index($0, "__INGEST_ARGS__")
      if (i) { print substr($0, 1, i - 1) ENVIRON["INGEST_ARGS"] substr($0, i + 15) }
      else print }' \
    dist/ingest-rendered.yaml > dist/ingest-rendered.args.tmp
mv dist/ingest-rendered.args.tmp dist/ingest-rendered.yaml
# Gateway virtual keys (LiteLLM): same strip-or-substitute contract as the
# agent render in deploy.sh (Secret holds embed-api-key + context-llm-api-key
# for this Job).
if [ -n "${GATEWAY_API_KEY_SECRET:-}" ]; then
    sed -i -e "s|__GATEWAY_API_KEY_SECRET__|$GATEWAY_API_KEY_SECRET|g" dist/ingest-rendered.yaml
    echo "==> Gateway keys wired (Secret $GATEWAY_API_KEY_SECRET: EMBED/CONTEXT_LLM_API_KEY via secretKeyRef)"
else
    strip_gateway_key_entries dist/ingest-rendered.yaml EMBED_API_KEY CONTEXT_LLM_API_KEY
    echo "==> Gateway keys off (GATEWAY_API_KEY_SECRET unset): keyless model endpoints"
fi
# Collection distribution policy (issue #360): the complete validated tuple
# renders as quoted strings; validate_collection_policy ran before rendering,
# so no entry can be blank or missing here.
wire_pull_secret dist/ingest-rendered.yaml
wire_gateway_ca dist/ingest-rendered.yaml Job ingest ingest
fail_on_placeholders dist/ingest-rendered.yaml ingest
# CI-rehearsal knob (never set in the air gap): strategic-merge a patch into
# the rendered Job — e.g. lab-quota resources — without touching the prod
# overlay in git. Client-side only; the cluster is not contacted.
INGEST_EXTRA_PATCH=${INGEST_EXTRA_PATCH:-}
if [ -n "$INGEST_EXTRA_PATCH" ]; then
    [ -f "$INGEST_EXTRA_PATCH" ] || die "INGEST_EXTRA_PATCH file not found: $INGEST_EXTRA_PATCH"
    $KC patch --local -f dist/ingest-rendered.yaml \
        -p "$(cat "$INGEST_EXTRA_PATCH")" -o yaml > dist/ingest-rendered-patched.yaml
    mv dist/ingest-rendered-patched.yaml dist/ingest-rendered.yaml
fi
check_ingest_qdrant_key dist/ingest-rendered.yaml ingest
# Jobs are immutable: remove a previous run so re-ingest works.
if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $KC -n $NAMESPACE delete job ingest --ignore-not-found"
else
    $KC -n "$NAMESPACE" delete job ingest --ignore-not-found
fi
run $KC apply -f dist/ingest-rendered.yaml

if [ "${AIRGAP_DRYRUN:-0}" = "1" ]; then
    echo "[dryrun] $KC -n $NAMESPACE wait --for=condition=complete job/ingest --timeout=${INGEST_TIMEOUT}s"
    echo "[dryrun] rendered manifest kept at dist/ingest-rendered.yaml"
    # First-party chart rehearsal (issue #448 H2a, dry-run only): render the
    # explicit Job from the same chart + mapped values (D3: --show-only, never
    # installed automatically). Production paths above are untouched. The
    # export passes file-sourced values that plain sourcing leaves
    # shell-local to the mapper child process.
    command -v python3 >/dev/null 2>&1 || die "python3 is required for the chart rehearsal render (issue #448 H2a)"
    export INTERNAL_REGISTRY NAMESPACE QDRANT_RELEASE IMAGE_SHA EMBED_BASE_URL VLLM_BASE_URL EMBED_MODEL DENSE_DIM EMBED_MODEL_REVISION LLM_BASE_URL LLM_MODEL_REASONING RERANK_ENABLED RERANK_BASE_URL RERANK_MODEL RERANK_ENDPOINT_ORDER GATEWAY_API_KEY_SECRET GATEWAY_CA_CONFIGMAP PULL_SECRET OTEL_EXPORTER_OTLP_ENDPOINT OTEL_ENDPOINT_RESOLVED OTEL_TRACING_ENABLED OTEL_DEPLOYMENT_ENVIRONMENT OTEL_SERVICE_NAME METRICS_ENABLED AGENT_ROUTE ROUTE_DESTINATION_CA_FILE STORAGE_CLASS CORPUS_PVC INGEST_WORKERS INGEST_ALIAS_PUBLISH INGEST_REINGEST INGEST_RETIRE_DOCS CONTEXTUAL_EMBED_ENABLED CONTEXT_LLM_BASE_URL CONTEXT_LLM_MODEL QDRANT_SHARD_NUMBER QDRANT_REPLICATION_FACTOR QDRANT_WRITE_CONSISTENCY_FACTOR
    python3 scripts/airgap/map_values.py --out dist/mainframe-rag-release-values.yaml
    helm template app charts/mainframe-rag -f dist/mainframe-rag-release-values.yaml --namespace "$NAMESPACE" --show-only templates/ingest-job.yaml > dist/ingest-chart-rendered.yaml
    fail_on_placeholders dist/ingest-chart-rendered.yaml "ingest chart"
    echo "[dryrun] chart Job kept at dist/ingest-chart-rendered.yaml"
else
    echo "==> Waiting for ingest Job (timeout: ${INGEST_TIMEOUT}s)..."
    # Stream logs as an overlay in the background once pod starts
    (
        for i in $(seq 1 60); do
            pod_phase=$($KC -n "$NAMESPACE" get pods -l job-name=ingest -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)
            if [ "$pod_phase" = "Running" ] || [ "$pod_phase" = "Succeeded" ] || [ "$pod_phase" = "Failed" ]; then
                $KC -n "$NAMESPACE" logs -f job/ingest 2>/dev/null || true
                break
            fi
            sleep 2
        done
    ) &
    LOGS_PID=$!

    if ! $KC -n "$NAMESPACE" wait --for=condition=complete job/ingest --timeout="${INGEST_TIMEOUT}s"; then
        kill "$LOGS_PID" 2>/dev/null || true
        echo "::error::ingest Job did not complete successfully" >&2
        $KC -n "$NAMESPACE" logs job/ingest --tail=200 || true
        $KC -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -30 || true
        exit 1
    fi
    kill "$LOGS_PID" 2>/dev/null || true
fi

next_step "sh scripts/tools/run-task.sh airgap:smoke"
