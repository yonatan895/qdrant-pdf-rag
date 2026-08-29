#!/usr/bin/env sh
# Run a local vLLM OpenAI-compatible server using Docker with NVIDIA GPU pass-through.
# Optimized for consumer GPUs (e.g. RTX 5060 with 8GB VRAM).

set -eu

MODEL="${MODEL:-google/gemma-4-E4B-it-qat-mobile-ct}"
PORT="${PORT:-8000}"
GPU_MEM="${GPU_MEM:-0.85}"
MAX_LEN="${MAX_LEN:-8192}"
IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:latest}"

echo "============================================================"
echo " Starting local vLLM server"
echo "============================================================"
echo " Model:             ${MODEL}"
echo " Port:              ${PORT}"
echo " GPU Memory Util:   ${GPU_MEM}"
echo " Max Context Len:   ${MAX_LEN}"
echo " Container Image:   ${IMAGE}"
echo "============================================================"

# Detect container runtime
if command -v docker >/dev/null 2>&1; then
    RUNTIME="docker"
elif command -v podman >/dev/null 2>&1; then
    RUNTIME="podman"
else
    echo "ERROR: Neither docker nor podman found on PATH." >&2
    exit 1
fi

HF_CACHE_DIR="${HF_HOME:-${HOME}/.cache/huggingface}"
mkdir -p "${HF_CACHE_DIR}"

ENV_FLAGS=""
if [ -n "${HF_TOKEN:-}" ]; then
    ENV_FLAGS="-e HF_TOKEN=${HF_TOKEN}"
fi

exec "${RUNTIME}" run --rm -it --gpus all \
    -p "${PORT}:8000" \
    -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
    ${ENV_FLAGS} \
    --ipc=host \
    "${IMAGE}" \
    --model "${MODEL}" \
    --gpu-memory-utilization "${GPU_MEM}" \
    --max-model-len "${MAX_LEN}" \
    --port 8000 "$@"
