#!/bin/sh
# AIR-GAP PIPELINE ORCHESTRATOR (issue #15): single master entrypoint
# that runs the entire end-to-end air-gap promotion pipeline:
#
#   1. Validate Environment & Prereqs (scripts/airgap/validate.sh)
#   2. Load Images into Registry      (scripts/airgap/load.sh)
#   3. Deploy Cluster & Agent Stack   (scripts/airgap/deploy.sh)
#   4. Ingest Corpus (if CORPUS_PVC)  (scripts/airgap/ingest.sh)
#   5. Verify Acceptance Smoke Test   (scripts/airgap/smoke.sh)
#
# Usage:
#   make airgap-pipeline
#   sh scripts/airgap/pipeline.sh [--skip-load] [--skip-ingest] [--dry-run]

. "$(dirname -- "$0")/common.sh"

SKIP_LOAD=0
SKIP_INGEST=0

for arg in "$@"; do
    case "$arg" in
        --skip-load)   SKIP_LOAD=1 ;;
        --skip-ingest) SKIP_INGEST=1 ;;
        --dry-run)     export AIRGAP_DRYRUN=1 ;;
        --help|-h)
            echo "Usage: $0 [--skip-load] [--skip-ingest] [--dry-run]"
            exit 0
            ;;
    esac
done

echo "================================================================================"
echo "          MAINFRAME RAG — AIR-GAP PROMOTION PIPELINE ORCHESTRATOR"
echo "================================================================================"
echo "Mode: $([ "${AIRGAP_DRYRUN:-0}" = "1" ] && echo "DRY-RUN (simulation)" || echo "LIVE PRODUCTION")"
echo ""

echo ">>> STAGE 1/5: PRE-FLIGHT VALIDATION"
sh scripts/airgap/validate.sh

if [ "$SKIP_LOAD" -eq 1 ]; then
    echo ""
    echo ">>> STAGE 2/5: IMAGE LOADING (SKIPPED via --skip-load)"
else
    echo ""
    echo ">>> STAGE 2/5: IMAGE LOADING & INTEGRITY"
    sh scripts/airgap/load.sh
fi

echo ""
echo ">>> STAGE 3/5: STACK DEPLOYMENT"
sh scripts/airgap/deploy.sh

INGEST_PERFORMED=0
if [ "$SKIP_INGEST" -eq 1 ]; then
    echo ""
    echo ">>> STAGE 4/5: CORPUS INGESTION (SKIPPED via --skip-ingest)"
elif [ -n "${CORPUS_PVC:-}" ]; then
    echo ""
    echo ">>> STAGE 4/5: CORPUS INGESTION (PVC: $CORPUS_PVC)"
    sh scripts/airgap/ingest.sh
    INGEST_PERFORMED=1
else
    echo ""
    echo ">>> STAGE 4/5: CORPUS INGESTION (SKIPPED — CORPUS_PVC not set)"
    echo "    To ingest later: make airgap-ingest CORPUS_PVC=<pvc>"
fi

echo ""
echo ">>> STAGE 5/5: ACCEPTANCE & SMOKE VERIFICATION"
sh scripts/airgap/smoke.sh

echo ""
echo "================================================================================"
if [ "$INGEST_PERFORMED" -eq 1 ]; then
    echo "   PIPELINE ORCHESTRATION COMPLETE: AIR-GAP SYSTEM OPERATIONAL & ACCEPTED"
else
    echo "   PIPELINE ORCHESTRATION COMPLETE: DEPLOYMENT READY (Awaiting Corpus Ingest)"
fi
echo "================================================================================"
