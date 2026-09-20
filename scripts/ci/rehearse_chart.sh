#!/bin/sh
# Published-bundle H2 rehearsal. Run from the verified source checkout;
# common.sh remains the operator-file/explicit-environment precedence owner.
set -eu
set -a
. "$(dirname -- "$0")/../airgap/common.sh"
resolve_aliases
resolve_otel_endpoint
set +a

DATA_NS="$NAMESPACE"
B_MARKER=chart-lifecycle-B
export DATA_NS B_MARKER
PULL_SECRET_SRC=${PULL_SECRET:-}
export PULL_SECRET_SRC
mkdir -p dist
# The installed release must never own the explicit ingest Job.
OTEL_DEPLOYMENT_ENVIRONMENT=chart-lifecycle-A python3 scripts/airgap/map_values.py \
    --without-ingest-job --out dist/chart-lifecycle-a.yaml
OTEL_DEPLOYMENT_ENVIRONMENT="$B_MARKER" python3 scripts/airgap/map_values.py \
    --without-ingest-job --out dist/chart-lifecycle-b.yaml
sh scripts/ci/chart_lifecycle.sh "$NAMESPACE-chart" charts/mainframe-rag \
    dist/chart-lifecycle-a.yaml dist/chart-lifecycle-b.yaml
