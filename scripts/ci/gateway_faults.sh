#!/bin/sh
# Deterministic model failures THROUGH real LiteLLM from the application pod.
set -eu
[ "$#" -eq 1 ] || { echo "usage: $0 <namespace>" >&2; exit 2; }
NS="$1"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROBE=/app/scripts/probe_gateway.py
run_probe() { kubectl -n "$NS" exec deploy/rag-agent -- python3 "$PROBE" --require-reasoning --stream "$@"; }
run_application() {
    kubectl -n "$NS" exec -i deploy/rag-agent -- python3 - "$@" < "$SCRIPT_DIR/application_contracts.py"
}
restore() {
    kubectl -n "$NS" set env deploy/vllm-mock MOCK_CHAT_FAULT=healthy MOCK_EMBED_FAULT=healthy MOCK_TTFT_MS=0
    kubectl -n "$NS" rollout status deploy/vllm-mock --timeout=180s
}
trap restore EXIT
run_probe
run_application
expect_failure() {
    _label="$1"; shift
    _output="$("$@" 2>&1)" && { echo "ERROR: $_label incorrectly passed" >&2; exit 1; }
    echo "$_output" | grep -F 'GATEWAY PROBE FAILED' >/dev/null || {
        echo "ERROR: $_label failed outside the gateway contract" >&2; exit 1;
    }
    echo "PASS: $_label refused"
}
for _fault in upstream malformed truncated; do
    kubectl -n "$NS" set env deploy/vllm-mock "MOCK_CHAT_FAULT=$_fault"
    kubectl -n "$NS" rollout status deploy/vllm-mock --timeout=180s
    expect_failure "chat $_fault" run_probe
    if [ "$_fault" = truncated ]; then
        run_application --expect-failure --streams-only
    else
        run_application --expect-failure
    fi
    restore
    run_probe
    run_application
 done
for _fault in upstream malformed dimension; do
    kubectl -n "$NS" set env deploy/vllm-mock "MOCK_EMBED_FAULT=$_fault"
    kubectl -n "$NS" rollout status deploy/vllm-mock --timeout=180s
    expect_failure "embed $_fault" run_probe
    restore
    run_probe
 done
expect_failure 'wrong reasoning credentials' kubectl -n "$NS" exec deploy/rag-agent -- \
    env LLM_API_KEY=sk-wrong python3 "$PROBE" --require-reasoning --stream
expect_failure 'missing reasoning credentials' kubectl -n "$NS" exec deploy/rag-agent -- \
    env -u LLM_API_KEY python3 "$PROBE" --require-reasoning --stream
expect_failure 'untrusted gateway CA' kubectl -n "$NS" exec deploy/rag-agent -- \
    env SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt python3 "$PROBE" --require-reasoning --stream
expect_failure 'gateway hostname mismatch' kubectl -n "$NS" exec deploy/rag-agent -- \
    env LLM_BASE_URL=https://test-gateway-wrong-name:4000/v1 python3 "$PROBE" --require-reasoning --stream
kubectl -n "$NS" set env deploy/vllm-mock MOCK_TTFT_MS=5000
kubectl -n "$NS" rollout status deploy/vllm-mock --timeout=180s
expect_failure 'reasoning deadline' run_probe --timeout 1
restore
run_probe
trap - EXIT
