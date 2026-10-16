# Sourced (POSIX sh), never executed. One owner for the local 8 GB vLLM lane's
# reasoning budget (#777).
#
# The application defaults are sized for Qwen3.8-27B at a 32768-token window;
# the local lane serves 4096. Unless the caller declares the served window
# (LLM_MAX_MODEL_LEN, as the Colab lane does), pin this lane's budget for every
# key left unset, so no local tool prices a 32k prompt against a 4k server.
if [ -z "${LLM_MAX_MODEL_LEN:-}" ]; then
    : "${LLM_MAX_MODEL_LEN:=4096}" "${LLM_RESERVED_OUTPUT_TOKENS:=1536}"
    : "${LLM_THINKING_RESERVE_TOKENS_SIMPLE:=0}" "${LLM_THINKING_RESERVE_TOKENS_COMPLEX:=1000}"
    : "${LLM_REASONING_EFFORT_SIMPLE:=low}" "${LLM_REASONING_EFFORT_COMPLEX:=high}"
    : "${LLM_REASONING_EFFORTS:=low,medium,high}" "${LLM_MAX_CHUNK_TOKENS_NARRATIVE:=350}"
    : "${PROMPT_MAX_CONTEXT_CHARS:=8000}" "${PROMPT_MAX_CONTEXT_CHARS_COMPLEX:=4500}"
    : "${PROMPT_MAX_CHUNK_CHARS:=3000}" "${PROMPT_MAX_CHUNK_CHARS_COMPLEX:=1100}"
    export LLM_MAX_MODEL_LEN LLM_RESERVED_OUTPUT_TOKENS LLM_THINKING_RESERVE_TOKENS_SIMPLE \
        LLM_THINKING_RESERVE_TOKENS_COMPLEX LLM_REASONING_EFFORT_SIMPLE LLM_REASONING_EFFORT_COMPLEX \
        LLM_REASONING_EFFORTS LLM_MAX_CHUNK_TOKENS_NARRATIVE PROMPT_MAX_CONTEXT_CHARS \
        PROMPT_MAX_CONTEXT_CHARS_COMPLEX PROMPT_MAX_CHUNK_CHARS PROMPT_MAX_CHUNK_CHARS_COMPLEX
fi
