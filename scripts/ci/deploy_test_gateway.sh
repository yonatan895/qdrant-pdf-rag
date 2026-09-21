#!/bin/sh
# CI-only real LiteLLM + PostgreSQL with virtual keys. Caller owns namespace.
# Only model computation is replaced by scripts/mock_vllm.py.
set -eu
[ "$#" -eq 1 ] || { echo "usage: $0 <test-namespace>" >&2; exit 2; }
NS="$1"
case "$NS" in ''|*[!a-z0-9-]*) echo "invalid namespace" >&2; exit 2 ;; esac
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
umask 077
KEY_DIR="$(mktemp -d)"
trap 'rm -rf "$KEY_DIR"' EXIT HUP INT TERM
python3 - "$KEY_DIR" <<'PY'
import secrets
import sys
from pathlib import Path
root = Path(sys.argv[1])
password = secrets.token_hex(24)
(root / 'pg-password').write_text(password)
(root / 'database-url').write_text(f'postgresql://litellm:{password}@test-gateway-pg:5432/litellm')
(root / 'master-key').write_text('sk-' + secrets.token_hex(24))
PY
kubectl -n "$NS" create secret generic test-gateway-admin \
    --from-file="$KEY_DIR/master-key" --from-file="$KEY_DIR/pg-password" --from-file="$KEY_DIR/database-url"
sh "$SCRIPT_DIR/create_test_tls.sh" "$KEY_DIR/tls" test-gateway
kubectl -n "$NS" create secret tls test-gateway-tls \
    --cert="$KEY_DIR/tls/tls.crt" --key="$KEY_DIR/tls/tls.key"
kubectl -n "$NS" create configmap test-gateway-ca \
    --from-file="$KEY_DIR/tls/ca-bundle.crt"
kubectl -n "$NS" create configmap test-gateway-hooks \
    --from-file="strict_finish.py=$SCRIPT_DIR/../gateway/strict_finish.py" \
    --from-file="scoped_passthrough.py=$SCRIPT_DIR/../gateway/scoped_passthrough.py"
kubectl -n "$NS" apply -f "$SCRIPT_DIR/test-gateway.yaml"
kubectl -n "$NS" rollout status deploy/test-gateway-pg --timeout=180s
kubectl -n "$NS" rollout status deploy/test-gateway --timeout=300s
# Keys never enter command arguments or logs. Mint through the real gateway API.
kubectl -n "$NS" exec -i deploy/test-gateway -- python3 - > "$KEY_DIR/keys.json" \
    < "$SCRIPT_DIR/mint_gateway_keys.py"
python3 - "$KEY_DIR" <<'PY'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
for name, value in json.loads((root / 'keys.json').read_text()).items():
    (root / name).write_text(value)
PY
kubectl -n "$NS" create secret generic test-gateway-keys \
    --from-file="$KEY_DIR/llm-api-key" --from-file="$KEY_DIR/embed-api-key" \
    --from-file="$KEY_DIR/context-llm-api-key" --from-file="$KEY_DIR/rerank-api-key"
