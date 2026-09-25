"""Shared core execution pipeline for single-turn and multi-turn mainframe technical RAG.

Extracted from /v1/answer and /v1/chat to serve single-turn answer, OpenAI-compatible
multi-turn chat, and the operator console (/ui) over identical budget, prompt,
retrieval, and inference logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, replace
from typing import Literal, TypedDict

ReasoningEffort = Literal["low", "medium", "high"]
VALID_REASONING_EFFORTS: frozenset[ReasoningEffort] = frozenset({"low", "medium", "high"})


from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode

from mainframe_rag.agent.answer import (
    REASON_MALFORMED_FRAME,
    REASON_MISSING_FINISH,
    ParsedAnswer,
    PreparedPrompt,
    PromptEvidence,
    TruncatedStreamError,
    VerificationState,
    assert_reasoning_model,
    build_chat_messages,
    build_messages,
    classify_query_complexity,
    condense_query,
    parse_answer,
    verification_state_for,
)
from mainframe_rag.agent.chat_turn import (
    chat_body_chars as chat_body_chars,  # noqa: PLC0414 — preserve the existing helper import
)
from mainframe_rag.agent.chat_turn import prepare_chat_turn
from mainframe_rag.agent.core_ports import (
    AnswerModel,
    ModelToken,
    PromptBuilder,
    RetrievalResult,
    Retriever,
)
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage, Tokenizer, TokenUsage
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
    request_id: str | None = None
    is_chat: bool = False
    hits: list[SearchHit] | None = None
    query_kind: str | None = None
    timings: dict[str, int] | None = None
    reasoning_effort: ReasoningEffort | None = None


@dataclass
class AnswerCoreDeps:
    settings: Settings
    llm: AnswerModel
    retrieve: Retriever
    tokenizer: Tokenizer | None = None
    build_messages_fn: PromptBuilder[str] | None = None
    build_chat_messages_fn: PromptBuilder[list[ChatMessage]] | None = None
    classify_query_complexity_fn: Callable[[str], str] | None = None


def _prepare_chat_input(input_data: AnswerCoreInput, settings: Settings) -> AnswerCoreInput:
    if not input_data.is_chat:
        return input_data
    turn = prepare_chat_turn(input_data.messages or [], settings, input_data.splunk_context)
    return replace(input_data, query=turn.query, messages=turn.messages)


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
    input_data = _prepare_chat_input(input_data, deps.settings)
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
    script_lang: str | None
    # Verification state (issue #365): what was established about this
    # answer — eligibility is not semantic proof. Computed once in
    # _finalize_answer (or the empty-hits short-circuit) so JSON, SSE,
    # chat, and console cannot disagree.
    verification_state: VerificationState
    # True whenever a script fence was extracted: scripts pass through
    # unvalidated, so any surfaced script is a human-review-required draft.
    script_review_required: bool
    query_kind: str
    hits: list[SearchHit]
    finish_reason: str
    usage: TokenUsage
    timings: dict[str, int]
    llm_ms: int
    ttft_ms: int | None
    complexity: str
    parsed: ParsedAnswer
    # Final supplied-evidence manifest (issue #364): `hits` stays the
    # retrieval list (retrieved candidates for diagnostics); `evidence` is
    # what the citation allowlist was actually derived from.
    evidence: PromptEvidence
    # Token-budget compliance of the final prompt (issue #368): True only
    # for a fitting remote tokenizer measurement; estimator and
    # char-packing paths report False (estimated, never confirmed).
    budget_verified: bool = False


class CoreToken(TypedDict):
    type: Literal["token"]
    delta: str
    ttft_ms: int | None


class CoreFinal(TypedDict):
    type: Literal["final"]
    output: AnswerCoreOutput


type CoreEvent = CoreToken | CoreFinal


def _resolve_reasoning_effort(
    input_data: AnswerCoreInput, settings: Settings, complexity: str
) -> ReasoningEffort:
    """Explicit override wins when valid; else the complexity default. One
    rule for both executors so JSON and SSE cannot pick different efforts."""
    if input_data.reasoning_effort in VALID_REASONING_EFFORTS:
        return input_data.reasoning_effort
    return (
        settings.llm_reasoning_effort_complex
        if complexity == "complex"
        else settings.llm_reasoning_effort_simple
    )


async def _build_prepared_prompt(
    input_data: AnswerCoreInput,
    deps: AnswerCoreDeps,
    hits: list[SearchHit],
    complexity: str,
    effort: str,
    root_ctx: Context | None,
) -> PreparedPrompt:
    """One prompt-build owner for the JSON and streaming executors: identical
    budget inputs, identical evidence manifest, one prompt.build span."""
    settings = deps.settings
    max_context = (
        settings.prompt_max_context_chars_complex
        if complexity == "complex"
        else settings.prompt_max_context_chars
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
            return await asyncio.to_thread(
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
        build_msg_fn = deps.build_messages_fn or build_messages
        return await asyncio.to_thread(
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


def _finalize_answer(
    content: str,
    prepared: PreparedPrompt,
    *,
    hits: list[SearchHit],
    kind: str,
    timings: dict[str, int],
    complexity: str,
    finish_reason: str,
    usage: TokenUsage,
    llm_ms: int,
    ttft_ms: int | None,
) -> AnswerCoreOutput:
    """One finalize owner for the JSON and streaming executors: citation
    parsing consumes the prepared evidence manifest, and the output carries
    the retrieval list separately (issue #364). The verification state is
    derived here from the finalized parse plus the transport outcome, so
    every surface reports the same label for the same answer."""
    parsed = parse_answer(content, prepared.evidence)
    return AnswerCoreOutput(
        answer=parsed.answer,
        citations=parsed.citations,
        citations_inferred=parsed.citations_inferred,
        inferred_indices=parsed.inferred_indices,
        script=parsed.script,
        script_lang=parsed.script_lang,
        verification_state=verification_state_for(
            citations=parsed.citations,
            citations_inferred=parsed.citations_inferred,
            finish_reason=finish_reason,
            abstained=parsed.abstained,
            empty_hits=False,
            empty_content=not content.strip(),
        ),
        script_review_required=parsed.script is not None,
        query_kind=kind,
        hits=hits,
        finish_reason=finish_reason,
        usage=usage,
        timings=timings,
        llm_ms=llm_ms,
        ttft_ms=ttft_ms,
        complexity=complexity,
        parsed=parsed,
        evidence=prepared.evidence,
        budget_verified=prepared.budget_verified,
    )


async def _retrieve_inputs(
    source: AnswerCoreInput, deps: AnswerCoreDeps, parent_span: trace.Span | None,
) -> RetrievalResult:
    if source.hits is not None:
        return RetrievalResult(source.hits, source.query_kind or "unknown", source.timings or {})
    query = await resolve_search_query(source, deps, parent_span)
    try:
        return await deps.retrieve(
            query, product=source.product, version=source.version, settings=deps.settings,
        )
    except Exception as exc:
        raise RetrievalError(exc) from exc


def _empty_output(query: str, kind: str, timings: dict[str, int], complexity: str) -> AnswerCoreOutput:
    empty_text = empty_hits_answer(query)
    return AnswerCoreOutput(
        answer=empty_text,
        citations=[],
        citations_inferred=False,
        inferred_indices=[],
        script=None,
        script_lang=None,
        verification_state="insufficient_evidence",
        script_review_required=False,
        query_kind=kind,
        hits=[],
        finish_reason="stop",
        usage=TokenUsage(),
        timings=timings,
        llm_ms=0,
        ttft_ms=None,
        complexity=complexity,
        parsed=ParsedAnswer(answer=empty_text),
        evidence=PromptEvidence(),
    )


async def execute_answer_core(
    input_data: AnswerCoreInput,
    deps: AnswerCoreDeps,
    parent_span: trace.Span | None = None,
) -> AnswerCoreOutput:
    """Execute the non-streaming core pipeline: retrieval, prompt build, LLM inference, and citation parsing."""
    settings = deps.settings
    input_data = _prepare_chat_input(input_data, settings)
    _base_url, llm_model = assert_reasoning_model(settings)
    root_ctx = trace.set_span_in_context(parent_span) if parent_span is not None else None

    retrieved = await _retrieve_inputs(input_data, deps, parent_span)
    hits, kind, timings = retrieved.hits, retrieved.kind, retrieved.timings

    classify_fn = deps.classify_query_complexity_fn or classify_query_complexity
    complexity = classify_fn(input_data.query)

    if not hits:
        return _empty_output(input_data.query, kind, timings, complexity)

    # 3. Prompt building (shared with the streaming executor)
    effort = _resolve_reasoning_effort(input_data, settings, complexity)
    prepared = await _build_prepared_prompt(input_data, deps, hits, complexity, effort, root_ctx)

    # 4. LLM inference
    temperature = (
        input_data.temperature if input_data.temperature is not None else settings.llm_temperature
    )
    t0 = time.monotonic()
    with tracer.start_as_current_span(
        "llm.chat",
        context=root_ctx,
        attributes={"llm.model": llm_model, "llm.reasoning_effort": effort},
    ) as llm_span:
        try:
            chat_res = await deps.llm.chat(
                prepared.messages,
                reasoning_effort=effort,
                temperature=temperature,
            )
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
    return _finalize_answer(
        chat_res.content,
        prepared,
        hits=hits,
        kind=kind,
        timings=timings,
        complexity=complexity,
        finish_reason=chat_res.finish_reason,
        usage=chat_res.usage,
        llm_ms=llm_ms,
        ttft_ms=chat_res.ttft_ms,
    )


async def execute_answer_core_stream(
    input_data: AnswerCoreInput,
    deps: AnswerCoreDeps,
    parent_span: trace.Span | None = None,
) -> AsyncGenerator[CoreEvent]:
    """Execute the streaming core pipeline: yields token deltas, then terminal citation/metadata record."""
    settings = deps.settings
    input_data = _prepare_chat_input(input_data, settings)
    _base_url, llm_model = assert_reasoning_model(settings)
    root_ctx = trace.set_span_in_context(parent_span) if parent_span is not None else None

    retrieved = await _retrieve_inputs(input_data, deps, parent_span)
    hits, kind, timings = retrieved.hits, retrieved.kind, retrieved.timings

    classify_fn = deps.classify_query_complexity_fn or classify_query_complexity
    complexity = classify_fn(input_data.query)

    # 2. Empty hits short-circuit: only the terminal record is yielded. The
    # empty answer is not a streamed token — /v1/answer's contract is a
    # single `final` (schema parity, review S6) and the chat routes emit the
    # canned text from the final output themselves.
    if not hits:
        yield {"type": "final", "output": _empty_output(input_data.query, kind, timings, complexity)}
        return

    # 3. Prompt building (shared with the buffered executor)
    effort = _resolve_reasoning_effort(input_data, settings, complexity)
    prepared = await _build_prepared_prompt(input_data, deps, hits, complexity, effort, root_ctx)

    # 4. LLM streaming
    temperature = (
        input_data.temperature if input_data.temperature is not None else settings.llm_temperature
    )
    t0 = time.monotonic()
    ttft_ms: int | None = None
    content_parts: list[str] = []
    # No synthesized terminal: a stream is complete only with an explicit
    # non-empty string done finish (issue #365). The initial None means a
    # stream that never yielded a valid done is incomplete, even when tokens
    # arrived.
    finish_reason: str | None = None
    usage = TokenUsage()

    stream_gen = deps.llm.stream(prepared.messages, effort, temperature)

    with tracer.start_as_current_span(
        "llm.chat",
        context=root_ctx,
        attributes={"llm.model": llm_model, "llm.reasoning_effort": effort},
    ) as llm_span:
        try:
            async for item in stream_gen:
                if isinstance(item, ModelToken):
                    if item.delta:
                        if ttft_ms is None:
                            ttft_ms = item.ttft_ms or int((time.monotonic() - t0) * 1000)
                        content_parts.append(item.delta)
                        yield {"type": "token", "delta": item.delta, "ttft_ms": ttft_ms}
                else:
                    if not item.finish_reason:
                        raise TruncatedStreamError(len(content_parts), REASON_MALFORMED_FRAME)
                    finish_reason = item.finish_reason
                    usage = item.usage
                    if ttft_ms is None and item.ttft_ms is not None:
                        ttft_ms = item.ttft_ms

            if finish_reason is None:
                # No valid done item arrived: never finalize tokens as "stop".
                raise TruncatedStreamError(len(content_parts), REASON_MISSING_FINISH)

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
        finally:
            await stream_gen.aclose()

    full_content = "".join(content_parts)
    llm_ms = int((time.monotonic() - t0) * 1000)
    output = _finalize_answer(
        full_content,
        prepared,
        hits=hits,
        kind=kind,
        timings=timings,
        complexity=complexity,
        finish_reason=finish_reason,
        usage=usage,
        llm_ms=llm_ms,
        ttft_ms=ttft_ms,
    )
    yield {"type": "final", "output": output}
