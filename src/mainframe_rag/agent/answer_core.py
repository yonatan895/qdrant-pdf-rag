"""Shared core execution pipeline for single-turn and multi-turn mainframe technical RAG.

Extracted from /v1/answer and /v1/chat to serve single-turn answer, OpenAI-compatible
multi-turn chat, and the operator console (/ui) over identical budget, prompt,
retrieval, and inference logic.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from mainframe_rag.agent.answer import (
    ParsedAnswer,
    as_chat_result,
    assert_reasoning_model,
    build_chat_messages,
    build_messages,
    classify_query_complexity,
    condense_query,
    parse_answer,
)
from mainframe_rag.agent.sse import fallback_stream
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage, LLMClient, Tokenizer, TokenUsage
from mainframe_rag.retrieve.filters import parse_query
from mainframe_rag.retrieve.query import SearchHit

tracer: trace.Tracer = trace.get_tracer("mainframe-rag.agent")
log = logging.getLogger(__name__)

_EMPTY_ANSWER_MAX_TERMS = 5


def empty_hits_answer(query: str) -> str:
    """Message for the no-hits path (issue #132).

    Identifier queries name the missing codes so typo users can spot and
    retype them; unknown codes get the truth instead of a generic empty.
    Unfiltered-serve fallback is deliberately NOT the mechanism: NEG-08
    proves it would serve the must_not sibling. Capped — a code-salad
    query must not echo unbounded input.
    """
    ids = parse_query(query)
    terms = ids.message_ids + ids.doc_ids + ids.members
    if not terms:
        return "No supporting manual excerpts were found for this question."
    shown = ", ".join(terms[:_EMPTY_ANSWER_MAX_TERMS])
    if len(terms) > _EMPTY_ANSWER_MAX_TERMS:
        shown += f", +{len(terms) - _EMPTY_ANSWER_MAX_TERMS} more"
    return f"No manual excerpts carry {shown}."


@dataclass
class AnswerCoreInput:
    query: str
    messages: list[ChatMessage] | None = None
    product: str | None = None
    version: str | None = None
    splunk_context: str | None = None
    stream: bool = False
    temperature: float | None = None
    model: str | None = None
    request_id: str | None = None
    is_chat: bool = False
    hits: list[SearchHit] | None = None
    query_kind: str | None = None
    timings: dict[str, int] | None = None


@dataclass
class AnswerCoreDeps:
    settings: Settings
    llm: LLMClient
    qdrant: Any
    embedder: Any
    reranker: Any = None
    tokenizer: Tokenizer | None = None
    retrieve_search_fn: Any = None
    build_messages_fn: Any = None
    build_chat_messages_fn: Any = None
    classify_query_complexity_fn: Any = None


class LLMChatError(Exception):
    """Raised when the LLM chat invocation fails."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


class RetrievalError(Exception):
    """Raised when the retrieval leg fails. Handlers map it to the same
    502 "retrieval failed" shape as /v1/search (one fault, one code)."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


async def resolve_search_query(
    input_data: AnswerCoreInput,
    deps: AnswerCoreDeps,
    parent_span: trace.Span | None = None,
) -> str:
    """The one owner of the follow-up condensation rule: chat turns after the
    first condense only when CHAT_CONDENSE_ENABLED is on; every other turn
    searches its literal text. Both the core retrieval branch and the route
    handlers call this so the flag cannot be honored on one path only."""
    if (
        input_data.is_chat
        and input_data.messages
        and len(input_data.messages) > 1
        and deps.settings.chat_condense_enabled
    ):
        ctx = trace.set_span_in_context(parent_span) if parent_span is not None else None
        with tracer.start_as_current_span("chat.condense", context=ctx):
            return await condense_query(deps.llm, input_data.messages, deps.settings)
    return input_data.query


@dataclass
class AnswerCoreOutput:
    answer: str
    citations: list[str]
    citations_inferred: bool
    inferred_indices: list[int]
    script: str | None
    query_kind: str
    hits: list[SearchHit]
    finish_reason: str
    usage: TokenUsage
    timings: dict[str, int]
    llm_ms: int
    ttft_ms: int | None
    complexity: str
    parsed: ParsedAnswer


async def _await_retrieval(res: Any) -> tuple[list[SearchHit], str, dict[str, int]]:
    if inspect.isawaitable(res):
        return await res
    return res


async def execute_answer_core(
    input_data: AnswerCoreInput,
    deps: AnswerCoreDeps,
    parent_span: trace.Span | None = None,
) -> AnswerCoreOutput:
    """Execute the non-streaming core pipeline: retrieval, prompt build, LLM inference, and citation parsing."""
    settings = deps.settings
    _base_url, llm_model = assert_reasoning_model(settings)
    active_model = input_data.model or llm_model
    root_ctx = trace.set_span_in_context(parent_span) if parent_span is not None else None

    # 1. Retrieval
    if input_data.hits is None:
        search_query = await resolve_search_query(input_data, deps, parent_span)

        retrieve_fn = deps.retrieve_search_fn
        if retrieve_fn is None:
            from mainframe_rag.retrieve.query import async_search as retrieve_fn

        try:
            res = retrieve_fn(
                deps.qdrant,
                deps.embedder,
                settings.qdrant_collection,
                search_query,
                product=input_data.product,
                version=input_data.version,
                limit=8,
                settings=settings,
                reranker=deps.reranker,
            )
            hits, kind, timings = await _await_retrieval(res)
        except Exception as exc:
            raise RetrievalError(exc) from exc
    else:
        hits = input_data.hits
        kind = input_data.query_kind or "unknown"
        timings = input_data.timings or {}

    classify_fn = deps.classify_query_complexity_fn or classify_query_complexity
    complexity = classify_fn(input_data.query)

    # 2. Empty hits short-circuit
    if not hits:
        empty_text = empty_hits_answer(input_data.query)
        return AnswerCoreOutput(
            answer=empty_text,
            citations=[],
            citations_inferred=False,
            inferred_indices=[],
            script=None,
            query_kind=kind,
            hits=[],
            finish_reason="stop",
            usage=TokenUsage(),
            timings=timings,
            llm_ms=0,
            ttft_ms=None,
            complexity=complexity,
            parsed=ParsedAnswer(answer=empty_text),
        )

    # 3. Prompt building
    max_context = (
        settings.prompt_max_context_chars_complex
        if complexity == "complex"
        else settings.prompt_max_context_chars
    )
    effort = (
        settings.llm_reasoning_effort_complex
        if complexity == "complex"
        else settings.llm_reasoning_effort_simple
    )

    with tracer.start_as_current_span(
        "prompt.build",
        context=root_ctx,
        attributes={
            "rag.query_complexity": complexity,
            "rag.reasoning_effort": effort,
            "rag.max_context_chars": max_context,
        },
    ):
        if input_data.is_chat and input_data.messages:
            build_chat_fn = deps.build_chat_messages_fn or build_chat_messages
            prompt_messages = await asyncio.to_thread(
                build_chat_fn,
                input_data.messages,
                hits,
                product=input_data.product,
                version=input_data.version,
                splunk_context=input_data.splunk_context,
                max_context_chars=max_context,
                max_chunk_chars=settings.prompt_max_chunk_chars,
                max_chunk_chars_narrative=(
                    settings.prompt_max_chunk_chars_complex if complexity == "complex" else None
                ),
                splunk_context_max_chars=settings.splunk_context_max_chars,
                complexity=complexity,
                tokenizer=deps.tokenizer,
                settings=settings,
                order=settings.prompt_order,
            )
        else:
            build_msg_fn = deps.build_messages_fn or build_messages
            prompt_messages = await asyncio.to_thread(
                build_msg_fn,
                input_data.query,
                hits,
                product=input_data.product,
                version=input_data.version,
                splunk_context=input_data.splunk_context,
                max_context_chars=max_context,
                max_chunk_chars=settings.prompt_max_chunk_chars,
                max_chunk_chars_narrative=(
                    settings.prompt_max_chunk_chars_complex if complexity == "complex" else None
                ),
                splunk_context_max_chars=settings.splunk_context_max_chars,
                complexity=complexity,
                tokenizer=deps.tokenizer,
                settings=settings,
                order=settings.prompt_order,
            )

    # 4. LLM inference
    temperature = (
        input_data.temperature
        if input_data.temperature is not None
        else settings.llm_temperature
    )
    t0 = time.monotonic()
    with tracer.start_as_current_span(
        "llm.chat",
        context=root_ctx,
        attributes={"llm.model": active_model, "llm.reasoning_effort": effort},
    ) as llm_span:
        try:
            chat_call = deps.llm.chat(
                prompt_messages,
                reasoning_effort=effort,
                temperature=temperature,
            )
            chat_res = as_chat_result(await chat_call if inspect.isawaitable(chat_call) else chat_call)
            llm_span.set_attributes(
                {
                    "llm.ttft_ms": chat_res.ttft_ms if chat_res.ttft_ms is not None else 0,
                    "llm.finish_reason": chat_res.finish_reason,
                    "llm.prompt_tokens": chat_res.usage.prompt_tokens,
                    "llm.completion_tokens": chat_res.usage.completion_tokens,
                    "llm.reasoning_tokens": chat_res.usage.reasoning_tokens,
                    "llm.total_tokens": chat_res.usage.total_tokens,
                }
            )
        except Exception as exc:
            llm_span.record_exception(exc)
            llm_span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise LLMChatError(exc) from exc

    llm_ms = int((time.monotonic() - t0) * 1000)
    parsed = parse_answer(
        chat_res.content,
        {h.cite for h in hits},
        ordered_cites=[h.cite for h in hits],
    )

    return AnswerCoreOutput(
        answer=parsed.answer,
        citations=parsed.citations,
        citations_inferred=parsed.citations_inferred,
        inferred_indices=parsed.inferred_indices,
        script=parsed.script,
        query_kind=kind,
        hits=hits,
        finish_reason=chat_res.finish_reason,
        usage=chat_res.usage,
        timings=timings,
        llm_ms=llm_ms,
        ttft_ms=chat_res.ttft_ms,
        complexity=complexity,
        parsed=parsed,
    )


async def execute_answer_core_stream(
    input_data: AnswerCoreInput,
    deps: AnswerCoreDeps,
    parent_span: trace.Span | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Execute the streaming core pipeline: yields token deltas, then terminal citation/metadata record."""
    settings = deps.settings
    _base_url, llm_model = assert_reasoning_model(settings)
    active_model = input_data.model or llm_model
    root_ctx = trace.set_span_in_context(parent_span) if parent_span is not None else None

    # 1. Retrieval
    if input_data.hits is None:
        search_query = await resolve_search_query(input_data, deps, parent_span)

        retrieve_fn = deps.retrieve_search_fn
        if retrieve_fn is None:
            from mainframe_rag.retrieve.query import async_search as retrieve_fn

        try:
            res = retrieve_fn(
                deps.qdrant,
                deps.embedder,
                settings.qdrant_collection,
                search_query,
                product=input_data.product,
                version=input_data.version,
                limit=8,
                settings=settings,
                reranker=deps.reranker,
            )
            hits, kind, timings = await _await_retrieval(res)
        except Exception as exc:
            raise RetrievalError(exc) from exc
    else:
        hits = input_data.hits
        kind = input_data.query_kind or "unknown"
        timings = input_data.timings or {}

    classify_fn = deps.classify_query_complexity_fn or classify_query_complexity
    complexity = classify_fn(input_data.query)

    # 2. Empty hits short-circuit: only the terminal record is yielded. The
    # empty answer is not a streamed token — /v1/answer's contract is a
    # single `final` (schema parity, review S6) and the chat routes emit the
    # canned text from the final output themselves.
    if not hits:
        empty_text = empty_hits_answer(input_data.query)
        output = AnswerCoreOutput(
            answer=empty_text,
            citations=[],
            citations_inferred=False,
            inferred_indices=[],
            script=None,
            query_kind=kind,
            hits=[],
            finish_reason="stop",
            usage=TokenUsage(),
            timings=timings,
            llm_ms=0,
            ttft_ms=None,
            complexity=complexity,
            parsed=ParsedAnswer(answer=empty_text),
        )
        yield {"type": "final", "output": output}
        return

    # 3. Prompt building
    max_context = (
        settings.prompt_max_context_chars_complex
        if complexity == "complex"
        else settings.prompt_max_context_chars
    )
    effort = (
        settings.llm_reasoning_effort_complex
        if complexity == "complex"
        else settings.llm_reasoning_effort_simple
    )

    with tracer.start_as_current_span(
        "prompt.build",
        context=root_ctx,
        attributes={
            "rag.query_complexity": complexity,
            "rag.reasoning_effort": effort,
            "rag.max_context_chars": max_context,
        },
    ):
        if input_data.is_chat and input_data.messages:
            build_chat_fn = deps.build_chat_messages_fn or build_chat_messages
            prompt_messages = await asyncio.to_thread(
                build_chat_fn,
                input_data.messages,
                hits,
                product=input_data.product,
                version=input_data.version,
                splunk_context=input_data.splunk_context,
                max_context_chars=max_context,
                max_chunk_chars=settings.prompt_max_chunk_chars,
                max_chunk_chars_narrative=(
                    settings.prompt_max_chunk_chars_complex if complexity == "complex" else None
                ),
                splunk_context_max_chars=settings.splunk_context_max_chars,
                complexity=complexity,
                tokenizer=deps.tokenizer,
                settings=settings,
                order=settings.prompt_order,
            )
        else:
            build_msg_fn = deps.build_messages_fn or build_messages
            prompt_messages = await asyncio.to_thread(
                build_msg_fn,
                input_data.query,
                hits,
                product=input_data.product,
                version=input_data.version,
                splunk_context=input_data.splunk_context,
                max_context_chars=max_context,
                max_chunk_chars=settings.prompt_max_chunk_chars,
                max_chunk_chars_narrative=(
                    settings.prompt_max_chunk_chars_complex if complexity == "complex" else None
                ),
                splunk_context_max_chars=settings.splunk_context_max_chars,
                complexity=complexity,
                tokenizer=deps.tokenizer,
                settings=settings,
                order=settings.prompt_order,
            )

    # 4. LLM streaming
    temperature = (
        input_data.temperature
        if input_data.temperature is not None
        else settings.llm_temperature
    )
    t0 = time.monotonic()
    ttft_ms: int | None = None
    content_parts: list[str] = []
    finish_reason = "stop"
    usage = TokenUsage()

    if hasattr(deps.llm, "chat_stream"):
        stream_gen = deps.llm.chat_stream(
            prompt_messages,
            reasoning_effort=effort,
            temperature=temperature,
        )
    else:
        stream_gen = fallback_stream(deps.llm, prompt_messages, effort, temperature)

    with tracer.start_as_current_span(
        "llm.chat",
        context=root_ctx,
        attributes={"llm.model": active_model, "llm.reasoning_effort": effort},
    ) as llm_span:
        try:
            async for item in stream_gen:
                itype = item.get("type")
                if itype == "token":
                    delta = item.get("delta") or ""
                    if delta:
                        if ttft_ms is None:
                            ttft_ms = item.get("ttft_ms") or int((time.monotonic() - t0) * 1000)
                        content_parts.append(delta)
                        yield {"type": "token", "delta": delta, "ttft_ms": ttft_ms}
                elif itype == "done":
                    finish_reason = item.get("finish_reason") or "stop"
                    if item.get("usage"):
                        usage = item["usage"]
                    if ttft_ms is None and item.get("ttft_ms") is not None:
                        ttft_ms = item["ttft_ms"]

            llm_span.set_attributes(
                {
                    "llm.ttft_ms": ttft_ms if ttft_ms is not None else 0,
                    "llm.finish_reason": finish_reason,
                    "llm.prompt_tokens": usage.prompt_tokens,
                    "llm.completion_tokens": usage.completion_tokens,
                    "llm.reasoning_tokens": usage.reasoning_tokens,
                    "llm.total_tokens": usage.total_tokens,
                }
            )
        except Exception as exc:
            llm_span.record_exception(exc)
            llm_span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise

    full_content = "".join(content_parts)
    llm_ms = int((time.monotonic() - t0) * 1000)
    parsed = parse_answer(
        full_content,
        {h.cite for h in hits},
        ordered_cites=[h.cite for h in hits],
    )
    output = AnswerCoreOutput(
        answer=parsed.answer,
        citations=parsed.citations,
        citations_inferred=parsed.citations_inferred,
        inferred_indices=parsed.inferred_indices,
        script=parsed.script,
        query_kind=kind,
        hits=hits,
        finish_reason=finish_reason,
        usage=usage,
        timings=timings,
        llm_ms=llm_ms,
        ttft_ms=ttft_ms,
        complexity=complexity,
        parsed=parsed,
    )
    yield {"type": "final", "output": output}
