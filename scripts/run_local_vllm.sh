#!/usr/bin/env sh
# Run a local vLLM OpenAI-compatible server using Docker with NVIDIA GPU pass-through.
# Optimized for consumer GPUs (e.g. RTX 5060 with 8GB VRAM) and Gemma-4 models.

set -eu

MODEL="${MODEL:-google/gemma-4-E4B-it-qat-mobile-ct}"
PORT="${PORT:-8000}"
GPU_MEM="${GPU_MEM:-0.85}"
MAX_LEN="${MAX_LEN:-4096}"
IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.28.0}"

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

# TTY flag: only allocate pseudo-TTY if stdin is connected to an interactive terminal
if [ -t 0 ]; then
    TTY_FLAG="-it"
else
    TTY_FLAG=""
fi

# Environment flags:
# - VLLM_WSL2_ENABLE_PIN_MEMORY=1: Enables pinned host memory allocation on WSL2.
# - HF_TOKEN: Forwarded safely via `-e HF_TOKEN` without exposing the secret token string on argv.
ENV_ARGS="-e VLLM_WSL2_ENABLE_PIN_MEMORY=1"
if [ -n "${HF_TOKEN:-}" ]; then
    ENV_ARGS="${ENV_ARGS} -e HF_TOKEN"
fi

# Construct CLI options as individual quoted argv parameters to prevent word-splitting on JSON flags
CLI_OPTS="--gpu-memory-utilization ${GPU_MEM} --max-model-len ${MAX_LEN} --max-num-seqs 1 --port ${PORT}"

# If MODEL is a local directory, mount it directly into the container as /model
# Note: vLLM serve expects the model path/name as a positional argument.
if [ -d "${MODEL}" ]; then
    ABS_MODEL_DIR="$(cd "${MODEL}" && pwd)"
    MODEL_NAME="${SERVED_NAME:-$(basename "${ABS_MODEL_DIR}")}"
    echo " Detected local model directory: ${ABS_MODEL_DIR}"
    echo " Serving as model name:         ${MODEL_NAME}"
    echo "============================================================"
    case "${MODEL_NAME}" in
        *gemma-4*|*gemma4*)
            exec "${RUNTIME}" run --rm ${TTY_FLAG} --gpus all \
                -p "${PORT}:${PORT}" \
                -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
                -v "${ABS_MODEL_DIR}:/model:ro" \
                ${ENV_ARGS} \
                --ipc=host \
                "${IMAGE}" \
                /model \
                --served-model-name "${MODEL_NAME}" \
                --gpu-memory-utilization "${GPU_MEM}" \
                --max-model-len "${MAX_LEN}" \
                --limit-mm-per-prompt '{"image":0,"audio":0}' \
                --max-num-seqs 1 \
                --port "${PORT}" \
                --tool-call-parser gemma4 \
                --reasoning-parser gemma4 \
                --chat-template /vllm-workspace/examples/tool_chat_template_gemma4.jinja \
                "$@"
            ;;
        *)
            exec "${RUNTIME}" run --rm ${TTY_FLAG} --gpus all \
                -p "${PORT}:${PORT}" \
                -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
                -v "${ABS_MODEL_DIR}:/model:ro" \
                ${ENV_ARGS} \
                --ipc=host \
                "${IMAGE}" \
                /model \
                --served-model-name "${MODEL_NAME}" \
                --gpu-memory-utilization "${GPU_MEM}" \
                --max-model-len "${MAX_LEN}" \
                --limit-mm-per-prompt '{"image":0,"audio":0}' \
                --max-num-seqs 1 \
                --port "${PORT}" \
                "$@"
            ;;
    esac
else
    case "${MODEL}" in
        *gemma-4*|*gemma4*)
            exec "${RUNTIME}" run --rm ${TTY_FLAG} --gpus all \
                -p "${PORT}:${PORT}" \
                -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
                ${ENV_ARGS} \
                --ipc=host \
                "${IMAGE}" \
                "${MODEL}" \
                --gpu-memory-utilization "${GPU_MEM}" \
                --max-model-len "${MAX_LEN}" \
                --limit-mm-per-prompt '{"image":0,"audio":0}' \
                --max-num-seqs 1 \
                --port "${PORT}" \
                --tool-call-parser gemma4 \
                --reasoning-parser gemma4 \
                --chat-template /vllm-workspace/examples/tool_chat_template_gemma4.jinja \
                "$@"
            ;;
        *)
            exec "${RUNTIME}" run --rm ${TTY_FLAG} --gpus all \
                -p "${PORT}:${PORT}" \
                -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
                ${ENV_ARGS} \
                --ipc=host \
                "${IMAGE}" \
                "${MODEL}" \
                --gpu-memory-utilization "${GPU_MEM}" \
                --max-model-len "${MAX_LEN}" \
                --limit-mm-per-prompt '{"image":0,"audio":0}' \
                --max-num-seqs 1 \
                --port "${PORT}" \
                "$@"
            ;;
    esac
fi
