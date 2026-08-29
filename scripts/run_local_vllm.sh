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

ENV_FLAGS="-e VLLM_USE_V1=0 -e VLLM_WSL2_ENABLE_PIN_MEMORY=1"
if [ -n "${HF_TOKEN:-}" ]; then
    ENV_FLAGS="${ENV_FLAGS} -e HF_TOKEN=${HF_TOKEN}"
fi

# If MODEL is a local directory, mount it directly into the container as /model
EXTRA_VOLUMES=""
SERVED_MODEL="${MODEL}"
if [ -d "${MODEL}" ]; then
    ABS_MODEL_DIR="$(cd "${MODEL}" && pwd)"
    EXTRA_VOLUMES="-v ${ABS_MODEL_DIR}:/model:ro"
    MODEL_NAME="${SERVED_NAME:-$(basename "${ABS_MODEL_DIR}")}"
    echo " Detected local model directory: ${ABS_MODEL_DIR}"
    echo " Serving as model name:         ${MODEL_NAME}"
    echo "============================================================"
    exec "${RUNTIME}" run --rm -it --gpus all \
        -p "${PORT}:8000" \
        -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
        ${EXTRA_VOLUMES} \
        ${ENV_FLAGS} \
        --ipc=host \
        "${IMAGE}" \
        --model /model \
        --served-model-name "${MODEL_NAME}" \
        --gpu-memory-utilization "${GPU_MEM}" \
        --max-model-len "${MAX_LEN}" \
        --port 8000 "$@"
else
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
fi
