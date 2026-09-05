#!/bin/sh
# AIR-GAP SIDE (issue #15): one-shot ingest Job against the PROD stack.
#
#   make airgap-ingest CORPUS_PVC=<existing-pvc>
#
# The corpus PVC is caller-supplied (back it with NFS RO, block, whatever the
# platform team provides) and is mounted READ-ONLY at /corpus. No demo PDFs,
# no EMBED_MODE=hash — prod embeds via the in-cluster vLLM endpoint.

. "$(dirname -- "$0")/common.sh"

enforce_product_rules
resolve_aliases
require_env INTERNAL_REGISTRY NAMESPACE IMAGE_SHA CORPUS_PVC EMBED_MODEL DENSE_DIM VLLM_BASE_URL STORAGE_CLASS
case "$IMAGE_SHA" in
    ""|HEAD) die "IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)" ;;
esac
# Cross-check against the packed MANIFEST when it is reachable (dist/ or ../).
MANIFEST=$(find_manifest)
check_manifest_sha
require_kc
refuse_nfs_storage
KC=${KC:-$(kc)}

QDRANT_URL="http://${QDRANT_RELEASE}:6333"
INGEST_TIMEOUT=${INGEST_TIMEOUT:-3600}
INGEST_WORK_SIZE=${INGEST_WORK_SIZE:-100Gi}   # CI-rehearsal knob; default = prod size
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

echo "==> Kustomize: prod ingest Job (corpus PVC: $CORPUS_PVC)"
INGEST_WORKERS=${INGEST_WORKERS:-4}
kustomize_render deploy/kustomize/overlays/openshift-ingest | sed -E 's|"(__[A-Z0-9_]+__)"|\1|g' | sed \
    -e "s|__INTERNAL_REGISTRY__|$INTERNAL_REGISTRY|g" \
    -e "s|__IMAGE_SHA__|$IMAGE_SHA|g" \
    -e "s|namespace: mainframe-rag|namespace: $NAMESPACE|g" \
    -e "s|__QDRANT_URL__|$QDRANT_URL|g" \
    -e "s|__QDRANT_RELEASE__|$QDRANT_RELEASE|g" \
    -e "s|__EMBED_BASE_URL__|$EMBED_BASE_URL|g" \
    -e "s|__EMBED_MODEL__|$EMBED_MODEL|g" \
    -e "s|__DENSE_DIM__|\"$DENSE_DIM\"|g" \
    -e "s|__CORPUS_PVC__|$CORPUS_PVC|g" \
    -e "s|__INGEST_WORKERS__|\"$INGEST_WORKERS\"|g" \
    -e "s|__CONTEXTUAL_EMBED_ENABLED__|\"${CONTEXTUAL_EMBED_ENABLED:-false}\"|g" \
    -e "s|__CONTEXT_LLM_BASE_URL__|${CONTEXT_LLM_BASE_URL:-}|g" \
    -e "s|__CONTEXT_LLM_MODEL__|${CONTEXT_LLM_MODEL:-}|g" \
    > dist/ingest-rendered.yaml
wire_pull_secret dist/ingest-rendered.yaml
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

next_step "make airgap-smoke"
