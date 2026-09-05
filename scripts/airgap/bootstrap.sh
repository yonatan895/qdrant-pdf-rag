#!/bin/sh
# SNEAKERNET BOOTSTRAP (issue #15): single-step unpack verification,
# git repository extraction from repo.bundle, and workspace setup.
#
# Run from the directory where the sneakernet tarball was extracted:
#   sh bootstrap.sh
#
# Safe, idempotent, zero internet access.

set -eu

echo "==> 1. Verifying member checksums (SHA256SUMS)"
if [ ! -f SHA256SUMS ]; then
    echo "FAIL: SHA256SUMS not found in current directory. Extract tarball first." >&2
    exit 1
fi
sha256sum -c SHA256SUMS

echo "==> 2. Setting up repository workspace"
DEST_DIR="${AIRGAP_WORKSPACE:-qdrant-pdf-rag}"

if [ ! -d "$DEST_DIR/.git" ]; then
    if [ ! -f repo.bundle ]; then
        echo "FAIL: repo.bundle not found in current directory." >&2
        exit 1
    fi
    echo "    Cloning repository from repo.bundle into ./$DEST_DIR..."
    git clone --quiet repo.bundle "$DEST_DIR"
else
    echo "    Repository ./$DEST_DIR already exists."
fi

echo "==> 3. Linking sneakernet artifacts to ./$DEST_DIR/dist"
mkdir -p "$DEST_DIR/dist"
for item in bootstrap.sh repo.bundle qdrant-image.tar jaeger-image.tar app-ingest-*.tar app-agent-*.tar MANIFEST.txt PACKING_RECORD.txt SHA256SUMS; do
    # shellcheck disable=SC2086
    if [ -f $item ]; then
        cp $item "$DEST_DIR/dist/"
    fi
done

echo "==> 4. Checking airgap.env"
if [ ! -f "$DEST_DIR/airgap.env" ]; then
    if [ -f "$DEST_DIR/airgap.env.example" ]; then
        cp "$DEST_DIR/airgap.env.example" "$DEST_DIR/airgap.env"
        echo "    Created $DEST_DIR/airgap.env from example."
        echo "    Please edit $DEST_DIR/airgap.env to specify your INTERNAL_REGISTRY, STORAGE_CLASS, etc."
    fi
else
    echo "    $DEST_DIR/airgap.env already present."
fi

echo ""
echo "SUCCESS: Sneakernet bootstrap completed successfully."
echo "Next step:"
echo "  cd $DEST_DIR && make airgap-validate"
