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
kubectl -n "$NS" apply -f "$SCRIPT_DIR/test-gateway.yaml"
kubectl -n "$NS" rollout status deploy/test-gateway-pg --timeout=180s
kubectl -n "$NS" rollout status deploy/test-gateway --timeout=300s
# Keys never enter command arguments or logs. Mint through the real gateway API.
kubectl -n "$NS" exec -i deploy/test-gateway -- python3 - > "$KEY_DIR/keys.json" <<'PY'
import json
import os
import urllib.error
import urllib.request
base = 'http://127.0.0.1:4000'
master = os.environ['GATEWAY_MASTER_KEY']
def call(path, key, payload=None):
    req = urllib.request.Request(base + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)
keys = {}
for leg, model in [('llm', 'mock-reasoning'), ('embed', 'mock-embed')]:
    key = call('/key/generate', master, {'models': [model]})['key']
    call('/v1/models', key)
    keys[leg + '-api-key'] = key
# Both absent and wrong credentials must be refused by the real gateway.
for key in ['', 'sk-wrong']:
    try:
        call('/v1/models', key)
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
    else:
        raise RuntimeError('gateway accepted invalid credentials')
# Deploy's optional key Secret references all configured fields.
keys['context-llm-api-key'] = keys['llm-api-key']
keys['rerank-api-key'] = 'sk-unused-rerank-disabled'
print(json.dumps(keys))
PY
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
