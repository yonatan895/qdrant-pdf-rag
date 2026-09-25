"""SSE event builders for /v1/answer streaming (split from app.py).

Pure constructors only: no spans, no logs, no metrics, no request state.
The route handler owns the lifecycle (span end, logging, endpoint records);
this module owns the bytes. The terminal `final` schema is identical on the
normal and empty-hits paths by construction — both call sites build it here.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from mainframe_rag.ports import TokenUsage

if TYPE_CHECKING:
    from mainframe_rag.retrieve.query import SearchHit

# Single error shape for every mid-stream failure (was two identical
# literals): fixed code + message client-side, detail stays in logs.
SSE_ERROR_CODE = "upstream_error"
SSE_ERROR_MESSAGE = "stream failed"


def format_sse_event(name: str, payload: dict[str, Any]) -> str:
    """One SSE frame: `event: <name>` + JSON data + blank-line terminator."""
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n"


def error_payload() -> dict[str, str]:
    """Mid-stream `error` event. The stream never produced a finalized
    answer, so the state is always `generation_incomplete` (issue #365):
    partial tokens already sent are provisional and cannot be retracted.
    The state is hardcoded, never caller-supplied, so no error path can
    relabel a failed stream as accepted."""
    return {
        "type": "error",
        "code": SSE_ERROR_CODE,
        "message": SSE_ERROR_MESSAGE,
        "verification_state": "generation_incomplete",
    }


def _usage_payload(usage: TokenUsage) -> dict[str, int]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
    }


def empty_final_payload(
    request_id: str,
    answer: str,
    query_kind: str,
    verification_state: str = "insufficient_evidence",
    script_review_required: bool = False,
) -> dict[str, Any]:
    """Terminal `final` for the no-hits path: same keys as final_payload
    (review S6); no tokens streamed, so ttft_ms stays null and usage zeros.
    The state is always `insufficient_evidence` — nothing was attempted from
    evidence — and no script can ride this path, so both default closed."""
    return {
        "type": "final",
        "request_id": request_id,
        "answer": answer,
        "citations": [],
        "citations_inferred": False,
        "inferred_indices": [],
        "verification_state": verification_state,
        "script": None,
        "script_lang": None,
        "script_review_required": script_review_required,
        "query_kind": query_kind,
        "hits": [],
        "finish_reason": "stop",
        "ttft_ms": None,
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        },
    }


def final_payload(
    request_id: str,
    answer: str,
    citations: list[str],
    citations_inferred: bool,
    script: str | None,
    query_kind: str,
    hits: list[SearchHit],
    finish_reason: str,
    ttft_ms: int | None,
    usage: TokenUsage,
    inferred_indices: list[int] | None = None,
    script_lang: str | None = None,
    verification_state: str = "unverified_draft",
    script_review_required: bool = False,
) -> dict[str, Any]:
    """Terminal `final` for the streamed answer: verified citations/script
    identical in shape to the JSON mode and the empty-hits path.
    `citations_inferred` is the provenance flag (issue #269): true when the
    cites were mapped from bare bracket markers, never from an explicit
    citation line. `inferred_indices` (issue #299) carries which supplied
    `[n]` prompt labels those markers pointed at, 1-based (issue #364: labels
    come from the final evidence manifest, not retrieval rank); it is
    optional and defaults to empty so callers built against the pre-#299
    signature keep working, and the payload always carries a list.
    `verification_state` (issue #365) defaults closed (`unverified_draft`,
    never `accepted`): every route passes the core-computed label."""
    return {
        "type": "final",
        "request_id": request_id,
        "answer": answer,
        "citations": citations,
        "citations_inferred": citations_inferred,
        "inferred_indices": list(inferred_indices or []),
        "verification_state": verification_state,
        "script": script,
        "script_lang": script_lang,
        "script_review_required": script_review_required,
        "query_kind": query_kind,
        "hits": [h.model_dump() for h in hits],
        "finish_reason": finish_reason,
        "ttft_ms": ttft_ms,
        "usage": _usage_payload(usage),
    }


def format_openai_chunk(
    request_id: str,
    model: str,
    delta_content: str | None = None,
    finish_reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Format an SSE event in standard OpenAI chat.completion.chunk format:
    data: {"id": "...", "object": "chat.completion.chunk", ...}\n\n
    """
    choice: dict[str, Any] = {
        "index": 0,
        "delta": {"content": delta_content} if delta_content is not None else {},
        "finish_reason": finish_reason,
    }
    if extra:
        choice.update(extra)
    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }
    return f"data: {json.dumps(payload)}\n\n"


def format_openai_done() -> str:
    """Terminal marker for OpenAI chat completion streams."""
    return "data: [DONE]\n\n"


def format_openai_error(code: str = SSE_ERROR_CODE, message: str = SSE_ERROR_MESSAGE) -> str:
    """Format an SSE error event for OpenAI-compatible chat streams:
    data: {"error": {"code": "...", "message": "..."}, "verification_state": "..."}\n\n
    The `error` object keeps the strict OpenAI shape; the sibling top-level
    `verification_state` (issue #365) is additive and hardcoded
    `generation_incomplete` — a failed stream is never accepted guidance.
    Standard clients ignore unknown top-level keys.
    """
    payload = {
        "error": {"code": code, "message": message},
        "verification_state": "generation_incomplete",
    }
    return f"data: {json.dumps(payload)}\n\n"
