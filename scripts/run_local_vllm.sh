#!/usr/bin/env sh
# Run a local vLLM OpenAI-compatible server using Docker with NVIDIA GPU pass-through.
# Launch flags (GPU memory, context window, runner/eager shape) resolve from
# `mainframe_rag.serve` Budget profiles (default LOCAL_RT_8GB, tuned for
# consumer 8GB cards); explicit GPU_MEM / MAX_LEN / SEQS / ROLE in the
# environment always win. Run via `make local-vllm*` so BUDGET_PYTHON points
# at the project venv.

set -eu

MODEL="${MODEL:-google/gemma-4-E4B-it-qat-mobile-ct}"
PORT="${PORT:-8000}"
# Track explicit overrides so Budget resolution fills only what the operator
# did not set (serving-budget track PR-B; extends the issue #99 pattern).
# Launch-flag defaults come from `mainframe_rag.serve` Budget profiles below;
# explicit GPU_MEM / MAX_LEN / SEQS in the environment always win.
GPU_MEM_SET=0
[ -n "${GPU_MEM:-}" ] && GPU_MEM_SET=1
MAX_LEN_SET=0
[ -n "${MAX_LEN:-}" ] && MAX_LEN_SET=1
SEQS_SET=0
[ -n "${SEQS:-}" ] && SEQS_SET=1
GPU_MEM="${GPU_MEM:-}"
MAX_LEN="${MAX_LEN:-}"
SEQS="${SEQS:-}"
IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.28.0}"

# Startup banner is printed after Budget resolution below (it reports the
# resolved GPU_MEM / MAX_LEN).

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

# Detect local model directory vs HuggingFace hub model ID
ABS_MODEL_DIR=""
if [ -d "${MODEL}" ]; then
    ABS_MODEL_DIR="$(cd "${MODEL}" && pwd)"
    MODEL_NAME="${SERVED_NAME:-$(basename "${ABS_MODEL_DIR}")}"
    SERVED_TARGET="/model"
    echo " Detected local model directory: ${ABS_MODEL_DIR}"
    echo " Serving as model name:         ${MODEL_NAME}"
    echo "============================================================"
else
    MODEL_NAME="${SERVED_NAME:-${MODEL}}"
    SERVED_TARGET="${MODEL}"
fi

# Serving-budget sizing (serving-budget track PR-B): Budget
# (src/mainframe_rag/serve) is the single source of truth for launch flags —
# the same resolve path for every environment. ROLE selects the profile
# server: `make local-vllm*` passes it explicitly; direct invocations derive
# it from the model name with the match the old embed branch used (issue #99).
# --check-pack preflights the whole co-resident pack, so a pack that does not
# fit refuses here instead of failing at the second server's startup.
ROLE="${ROLE:-}"
if [ -z "${ROLE}" ]; then
    case "${MODEL} ${MODEL_NAME} ${TASK:-}" in
        *embed*|*Embed*) ROLE="embed" ;;
        *) ROLE="reasoning" ;;
    esac
fi
BUDGET_PYTHON="${BUDGET_PYTHON:-python3}"
BUDGET_PROFILE="${BUDGET_PROFILE:-LOCAL_RT_8GB}"
if ! BUDGET_OUT="$("${BUDGET_PYTHON}" -m mainframe_rag.serve resolve \
        --profile "${BUDGET_PROFILE}" --role "${ROLE}" --check-pack)"; then
    echo "ERROR: serving-budget resolve failed for profile '${BUDGET_PROFILE}' role '${ROLE}'." >&2
    echo "Run via 'make local-vllm*' (provides the .venv python) or set BUDGET_PYTHON to a python with mainframe_rag installed." >&2
    exit 1
fi
if [ -z "${BUDGET_OUT}" ]; then
    echo "ERROR: serving-budget resolve returned empty output; refusing to launch with unset flags." >&2
    exit 1
fi
eval "${BUDGET_OUT}"
: "${BUDGET_GPU_MEM:?serving-budget resolve did not emit BUDGET_GPU_MEM}"
: "${BUDGET_MAX_LEN:?serving-budget resolve did not emit BUDGET_MAX_LEN}"
: "${BUDGET_RUNNER:?serving-budget resolve did not emit BUDGET_RUNNER}"
: "${BUDGET_SEQS:?serving-budget resolve did not emit BUDGET_SEQS}"
# Explicit environment wins over Budget resolution (operator override rule).
if [ "${GPU_MEM_SET}" -eq 0 ]; then GPU_MEM="${BUDGET_GPU_MEM}"; fi
if [ "${MAX_LEN_SET}" -eq 0 ]; then MAX_LEN="${BUDGET_MAX_LEN}"; fi
if [ "${SEQS_SET}" -eq 0 ]; then SEQS="${BUDGET_SEQS}"; fi

echo "============================================================"
echo " Starting local vLLM server"
echo "============================================================"
echo " Model:             ${MODEL}"
echo " Port:              ${PORT}"
echo " Budget profile:    ${BUDGET_PROFILE} role ${ROLE}"
echo " GPU Memory Util:   ${GPU_MEM}"
echo " Max Context Len:   ${MAX_LEN}"
echo " Container Image:   ${IMAGE}"
echo "============================================================"

# Build positional vllm serve arguments
# Note: vLLM serve expects the model path/name as the first positional argument.
set -- "${SERVED_TARGET}" \
    --gpu-memory-utilization "${GPU_MEM}" \
    --max-model-len "${MAX_LEN}" \
    --limit-mm-per-prompt '{"image":0,"audio":0}' \
    --max-num-seqs "${SEQS}" \
    --port "${PORT}"

if [ "${SERVED_TARGET}" = "/model" ]; then
    set -- "$@" --served-model-name "${MODEL_NAME}"
fi

# Serving shape comes from Budget (single source of truth); model-family
# parser flags stay name-based (a model family, not a serving shape).
# Pooling rationale (issue #99, values now resolved from the Budget table):
# vLLM v0.28.0 removed --task; the pooling runner with the embed converter
# is its replacement. Batched tokens follow the Budget window so the
# memory-profiling peak stays bounded (a 2048 window was rejected by the
# tokenizer sweep — worst case 2043 tokens; pinned by
# tests/test_embed_budget.py), and eager mode avoids the torch.compile +
# CUDA-graph workspace that pushed the profiled peak over the 8GB
# co-residency budget (embeddings are single-shot prefill; eager costs
# little). A MAX_LEN operator override does not raise the batched cap:
# erring small here is the safe side.
if [ "${BUDGET_RUNNER}" = "pooling" ]; then
    set -- "$@" --runner "${BUDGET_RUNNER}"
    if [ "${BUDGET_CONVERT}" != "none" ]; then
        set -- "$@" --convert "${BUDGET_CONVERT}"
    fi
    if [ -n "${BUDGET_BATCHED_TOKENS:-}" ]; then
        set -- "$@" --max-num-batched-tokens "${BUDGET_BATCHED_TOKENS}"
    fi
fi
if [ "${BUDGET_EAGER:-0}" = "1" ]; then
    set -- "$@" --enforce-eager
fi
# vLLM prefix caching (KV reuse across shared prompt prefixes): resolved
# from the Budget profile, off unless the profile enables it. Issue #80
# measures the hit rate before enabling it anywhere.
if [ "${BUDGET_PREFIX_CACHE:-0}" = "1" ]; then
    set -- "$@" --enable-prefix-caching
else
    set -- "$@" --no-enable-prefix-caching
fi
case "${BUDGET_CHUNKED_PREFILL:-}" in
    1) set -- "$@" --enable-chunked-prefill ;;
    0) set -- "$@" --no-enable-chunked-prefill ;;
esac
if [ "${BUDGET_LANGUAGE_MODEL_ONLY:-0}" = "1" ]; then
    set -- "$@" --language-model-only
fi
if [ -n "${BUDGET_MM_PROCESSOR_CACHE_GB:-}" ]; then
    set -- "$@" --mm-processor-cache-gb "${BUDGET_MM_PROCESSOR_CACHE_GB}"
fi
# Add Gemma-4 reasoning parser flags
case "${MODEL} ${MODEL_NAME} ${TASK:-}" in
    *gemma-4*|*gemma4*)
        CHAT_TMPL="${CHAT_TEMPLATE:-/vllm-workspace/examples/tool_chat_template_gemma4.jinja}"
        set -- "$@" \
            --tool-call-parser gemma4 \
            --reasoning-parser gemma4 \
            --chat-template "${CHAT_TMPL}"
        ;;
esac

# Prepend runtime arguments as an argv vector; local paths may contain spaces.
set -- --gpus all -p "${PORT}:${PORT}" \
    -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
    -e VLLM_WSL2_ENABLE_PIN_MEMORY=1 --ipc=host "${IMAGE}" "$@"
if [ -n "${ABS_MODEL_DIR}" ]; then
    set -- -v "${ABS_MODEL_DIR}:/model:ro" "$@"
fi
# Forward the token by environment name, never by value on argv.
if [ -n "${HF_TOKEN:-}" ]; then
    set -- -e HF_TOKEN "$@"
fi
if [ -t 0 ]; then
    set -- -it "$@"
fi
exec "${RUNTIME}" run --rm "$@"
