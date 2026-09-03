#!/usr/bin/env sh
# Run a local vLLM OpenAI-compatible server using Docker with NVIDIA GPU pass-through.
# Optimized for consumer GPUs (e.g. RTX 5060 with 8GB VRAM) and Gemma-4 models.

set -eu

MODEL="${MODEL:-google/gemma-4-E4B-it-qat-mobile-ct}"
PORT="${PORT:-8000}"
# Track explicit overrides so the embed branch can apply its own default
# without clobbering user-supplied values (issue #99).
GPU_MEM_SET=0
[ -n "${GPU_MEM:-}" ] && GPU_MEM_SET=1
# Default GPU_MEM=0.65 allows co-residency with the local embed server
# (GPU_MEM=0.33, see the embed branch below) on 8GB VRAM.
# For solo reasoning server runs, set GPU_MEM=0.85 to maximize KV cache throughput.
GPU_MEM="${GPU_MEM:-0.65}"
# 4096 for both servers: the complex-query prompt budget physically requires
# it on the reasoning side, and the issue #99 tokenizer sweep rejected a
# 2048 embed window (worst case measured 2043 tokens).
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

# Detect local model directory vs HuggingFace hub model ID
LOCAL_MOUNT=""
if [ -d "${MODEL}" ]; then
    ABS_MODEL_DIR="$(cd "${MODEL}" && pwd)"
    MODEL_NAME="${SERVED_NAME:-$(basename "${ABS_MODEL_DIR}")}"
    LOCAL_MOUNT="-v ${ABS_MODEL_DIR}:/model:ro"
    SERVED_TARGET="/model"
    echo " Detected local model directory: ${ABS_MODEL_DIR}"
    echo " Serving as model name:         ${MODEL_NAME}"
    echo "============================================================"
else
    MODEL_NAME="${SERVED_NAME:-${MODEL}}"
    SERVED_TARGET="${MODEL}"
fi

# Resolve embed-server defaults BEFORE the args are built (issue #99): the
# same case string as the flag block below, evaluated early so GPU_MEM lands
# in --gpu-memory-utilization directly instead of relying on later-arg
# override semantics.
IS_EMBED=0
case "${MODEL} ${MODEL_NAME} ${TASK:-}" in
    *embed*|*Embed*) IS_EMBED=1 ;;
esac
if [ "${IS_EMBED}" -eq 1 ]; then
    # Local dev embed server default (issue #99, verified on 8GB VRAM
    # co-resident with the reasoning server at GPU_MEM=0.65):
    # - GPU_MEM=0.33: the KV pool is sized to fill gpu-memory-utilization x
    #   total VRAM, so the reasoning default (0.65) would over-commit the
    #   8GB card at startup; 0.33 holds with 1.29 GiB KV headroom.
    # - MAX_LEN stays 4096 (the reasoning default): a 2048 window was
    #   rejected by the Task 1 tokenizer sweep — the worst-case embedded
    #   string (header + a SECTION_MAX_CHARS=3500 body carrying the
    #   SPLIT_OVERLAP_CHARS=400 seed) measures 2043 tokens under the real
    #   Qwen3-Embedding tokenizer (~2.03 chars/token on syntax-dense text,
    #   sweep artifact on PR #100; pinned by tests/test_embed_budget.py).
    # - --enforce-eager: torch.compile + CUDA-graph workspace pushed the
    #   profiled peak over the co-residency budget; embeddings are
    #   single-shot prefill, so eager costs little.
    # Explicit GPU_MEM / MAX_LEN always win (user-supplied values are
    # applied, never silently replaced).
    if [ "${GPU_MEM_SET}" -eq 0 ]; then GPU_MEM="0.33"; fi
fi

# Build positional vllm serve arguments
# Note: vLLM serve expects the model path/name as the first positional argument.
set -- "${SERVED_TARGET}" \
    --gpu-memory-utilization "${GPU_MEM}" \
    --max-model-len "${MAX_LEN}" \
    --limit-mm-per-prompt '{"image":0,"audio":0}' \
    --max-num-seqs 1 \
    --port "${PORT}"

if [ "${SERVED_TARGET}" = "/model" ]; then
    set -- "$@" --served-model-name "${MODEL_NAME}"
fi

# Add Gemma-4 reasoning parser flags or embedding task flags
case "${MODEL} ${MODEL_NAME} ${TASK:-}" in
    *embed*|*Embed*)
        # vLLM v0.28.0 removed --task; the pooling runner with the embed
        # converter is its replacement (issue #99). Batched tokens follow
        # MAX_LEN so the memory-profiling peak stays bounded by the model
        # window, and eager mode avoids the torch.compile + CUDA-graph
        # workspace that pushed the profiled peak over the 8GB co-residency
        # budget (embeddings are single-shot prefill; eager costs little).
        set -- "$@" \
            --runner pooling \
            --convert embed \
            --max-num-batched-tokens "${MAX_LEN}" \
            --enforce-eager
        ;;
    *gemma-4*|*gemma4*)
        CHAT_TMPL="${CHAT_TEMPLATE:-/vllm-workspace/examples/tool_chat_template_gemma4.jinja}"
        set -- "$@" \
            --tool-call-parser gemma4 \
            --reasoning-parser gemma4 \
            --chat-template "${CHAT_TMPL}"
        ;;
esac

# Single unified container execution
exec "${RUNTIME}" run --rm ${TTY_FLAG} --gpus all \
    -p "${PORT}:${PORT}" \
    -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
    ${LOCAL_MOUNT} \
    ${ENV_ARGS} \
    --ipc=host \
    "${IMAGE}" \
    "$@"
