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
MANIFEST=""
[ -f dist/MANIFEST.txt ] && MANIFEST=dist/MANIFEST.txt
[ -z "$MANIFEST" ] && [ -f ../MANIFEST.txt ] && MANIFEST=../MANIFEST.txt
if [ -n "$MANIFEST" ]; then
    packed_sha=$(awk '/^sha: /{print $2}' "$MANIFEST")
    [ "$IMAGE_SHA" = "$packed_sha" ] || \
        die "IMAGE_SHA=$IMAGE_SHA does not match the packed MANIFEST sha ($packed_sha) — wrong SHA for this sneakernet bundle"
fi
[ "${AIRGAP_DRYRUN:-0}" = "1" ] || command -v oc >/dev/null 2>&1 || die "oc is required on the air-gap bastion (or set AIRGAP_DRYRUN=1 to preview)"
refuse_nfs_storage
if command -v kubectl >/dev/null 2>&1; then KC=kubectl; else KC=oc; fi

EMBED_BASE_URL=${EMBED_BASE_URL:-$(echo "$VLLM_BASE_URL" | sed 's:/*$::')/v1}
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
if command -v kustomize >/dev/null 2>&1; then
    render="kustomize build deploy/kustomize/overlays/openshift-ingest"
else
    render="$KC kustomize deploy/kustomize/overlays/openshift-ingest"
fi
$render | sed \
    -e "s|__INTERNAL_REGISTRY__|$INTERNAL_REGISTRY|g" \
    -e "s|__IMAGE_SHA__|$IMAGE_SHA|g" \
    -e "s|namespace: mainframe-rag|namespace: $NAMESPACE|g" \
    -e "s|__QDRANT_URL__|$QDRANT_URL|g" \
    -e "s|__QDRANT_RELEASE__|$QDRANT_RELEASE|g" \
    -e "s|__EMBED_BASE_URL__|$EMBED_BASE_URL|g" \
    -e "s|__EMBED_MODEL__|$EMBED_MODEL|g" \
    -e "s|__DENSE_DIM__|$DENSE_DIM|g" \
    -e "s|__CORPUS_PVC__|$CORPUS_PVC|g" \
    > dist/ingest-rendered.yaml
if [ -n "${PULL_SECRET:-}" ]; then
    sed -i "s|imagePullSecrets: \[\]|imagePullSecrets:\n  - name: $PULL_SECRET|" dist/ingest-rendered.yaml
fi
if grep -Eq "__[A-Z][A-Z0-9_]*__" dist/ingest-rendered.yaml; then
    die "unsubstituted placeholder left in rendered ingest manifest (check airgap.env)"
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
    echo "==> Wait for ingest Job (timeout: ${INGEST_TIMEOUT}s)"
    if ! $KC -n "$NAMESPACE" wait --for=condition=complete job/ingest --timeout="${INGEST_TIMEOUT}s"; then
        echo "::error::ingest Job failed" >&2
        $KC -n "$NAMESPACE" logs job/ingest --tail=200 || true
        exit 1
    fi
fi

next_step "make airgap-smoke"
