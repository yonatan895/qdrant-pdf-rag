# mypy: disallow_untyped_defs=True, disallow_untyped_calls=True, disallow_any_generics=True, warn_return_any=True, no_implicit_reexport=True, strict_equality=True, warn_unused_ignores=True
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
from itertools import zip_longest
from typing import Any, Literal, TypedDict

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import SpanKind, Status, StatusCode

from mainframe_rag.agent.answer import (
    REASON_MALFORMED_FRAME,
    REASON_MISSING_FINISH,
    ParsedAnswer,
    PreparedPrompt,
    PromptBudgetExceeded,
    PromptEvidence,
    SuppliedExcerpt,
    TruncatedStreamError,
    VerificationState,
    assert_reasoning_model,
    build_chat_messages,
    build_messages,
    classify_query_complexity,
    coding_mode_applies,
    condense_query,
    names_identifier,
    parse_answer,
    verification_state_for,
)
from mainframe_rag.agent.chat_turn import (
    chat_body_chars as chat_body_chars,  # noqa: PLC0414 — preserve the existing helper import
)
from mainframe_rag.agent.chat_turn import is_unsearchable_query, prepare_chat_turn
from mainframe_rag.agent.core_ports import (
    RETRIEVAL_HIT_LIMIT,
    AnswerModel,
    ModelDone,
    ModelToken,
    PromptBuilder,
    RetrievalResult,
    Retriever,
)
from mainframe_rag.agent.user_message import derive_retrieval_query
from mainframe_rag.config import REASONING_EFFORTS, ReasoningEffort, Settings
from mainframe_rag.logs import error_type
from mainframe_rag.ports import ChatMessage, Tokenizer, TokenUsage
from mainframe_rag.retrieve.filters import parse_query
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.retrieve.screen import screen_query
from mainframe_rag.tracing import bind_module_tracer, start_as_current_span

VALID_REASONING_EFFORTS: frozenset[ReasoningEffort] = frozenset(REASONING_EFFORTS)

tracer: trace.Tracer = trace.get_tracer("mainframe-rag.agent")
bind_module_tracer(__name__, "tracer", "mainframe-rag.agent")
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
    temperature: float | None = None
    is_chat: bool = False
    hits: list[SearchHit] | None = None
    query_kind: str | None = None
    timings: dict[str, int] | None = None
    reasoning_effort: ReasoningEffort | None = None
    # What retrieval searched for this turn (issue #1268), set with the hits:
    # the condensed query of a chat follow-up. None means the message's own
    # derived retrieval query, the only search a non-chat turn makes.
    search_query: str | None = None


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
    searches its literal text, or, for a message over `query_max_chars`,
    the query derived from it (issue #1192; never a model call). Both
    the core retrieval branch and the route handlers call this so the flag
    cannot be honored on one path only."""
    input_data = _prepare_chat_input(input_data, deps.settings)
    if (
        input_data.is_chat
        and input_data.messages
        and len(input_data.messages) > 1
        and deps.settings.chat_condense_enabled
    ):
        ctx = trace.set_span_in_context(parent_span) if parent_span is not None else None
        with start_as_current_span(tracer, "chat.condense", kind=SpanKind.CLIENT, context=ctx):
            return await condense_query(deps.llm, input_data.messages, deps.settings)
    return derive_retrieval_query(input_data.query, deps.settings)


type FollowUpMode = Literal["literal", "condensed", "anchored"]


@dataclass(frozen=True)
class SearchPlan:
    """What one turn searches (issue #1268). `query` is always searched (the
    message's own retrieval query, or its condensation); `anchor`, set only in
    `anchored` mode, is the previous user message's retrieval query, searched
    as a second leg. `mode` is the text-free label the root span carries."""

    mode: FollowUpMode
    query: str
    anchor: str | None = None

    @property
    def search_text(self) -> str:
        """What the prompt's retrieval-derived decisions read: every searched
        text, the antecedent first."""
        return f"{self.anchor}\n{self.query}" if self.anchor is not None else self.query


def _anchor_query(input_data: AnswerCoreInput, settings: Settings) -> str | None:
    """The previous user message's retrieval query when the active chat
    follow-up should be anchored to it, else None. Follow-ups only; never when
    the active message names an identifier (the condensation bypass), when
    either message screens as a trap, when the antecedent is one the active
    turn would refuse, or when both search the same text."""
    messages = input_data.messages or []
    if not (input_data.is_chat and settings.chat_followup_anchor_enabled and len(messages) > 1):
        return None
    literal = derive_retrieval_query(input_data.query, settings)
    if names_identifier(literal) or screen_query(literal) == "trap":
        return None
    earlier = next((m.content for m in reversed(messages[:-1]) if m.role == "user"), "")
    # The antecedent never passed the active-turn check: one the active turn
    # would refuse (blank, control characters) is not searched.
    if is_unsearchable_query(earlier):
        return None
    earlier = earlier.strip()
    anchor = derive_retrieval_query(earlier, settings)
    if anchor == literal or screen_query(anchor) == "trap":
        return None
    return anchor


async def plan_search(
    input_data: AnswerCoreInput,
    deps: AnswerCoreDeps,
    parent_span: trace.Span | None = None,
) -> SearchPlan:
    """The one owner of what a turn searches, for the core retrieval branch
    and every chat route alike. Condensation and anchoring cannot both be on
    (Settings refuses the pair, issue #1268). The mode is set on
    `parent_span` for chat follow-up turns as a fixed label, never text."""
    input_data = _prepare_chat_input(input_data, deps.settings)
    literal = derive_retrieval_query(input_data.query, deps.settings)
    query = await resolve_search_query(input_data, deps, parent_span)
    anchor = _anchor_query(input_data, deps.settings)
    mode: FollowUpMode = (
        "anchored" if anchor is not None else "condensed" if query != literal else "literal"
    )
    # Follow-up turns only: a first turn's root span keeps its shape.
    if parent_span is not None and input_data.is_chat and len(input_data.messages or []) > 1:
        parent_span.set_attribute("rag.followup_mode", mode)
    return SearchPlan(mode, query, anchor)


def _fuse_by_rank(literal: RetrievalResult, anchored: RetrievalResult) -> RetrievalResult:
    """Interleave two ranked lists, the literal leg first, keeping a chunk at
    its first place, cut to what one retrieval returns (`RETRIEVAL_HIT_LIMIT`,
    or a longer leg's length). Rank, not score: rerank and RRF scores of
    different queries are not comparable. The query kind is the literal leg's
    (the user's own question class); timings add."""
    limit = max(RETRIEVAL_HIT_LIMIT, len(literal.hits), len(anchored.hits))
    seen: set[str] = set()
    hits: list[SearchHit] = []
    for pair in zip_longest(literal.hits, anchored.hits):
        for hit in pair:
            if hit is not None and hit.chunk_id not in seen:
                seen.add(hit.chunk_id)
                hits.append(hit)
    timings = dict(literal.timings)
    for key, value in anchored.timings.items():
        timings[key] = timings.get(key, 0) + value
    return RetrievalResult(hits[:limit], literal.kind, timings)


async def retrieve_planned(
    plan: SearchPlan, source: AnswerCoreInput, deps: AnswerCoreDeps,
) -> RetrievalResult:
    """Run a plan's retrieval: one search, or for an anchored follow-up the
    literal leg then the antecedent leg, fused. Either leg failing fails the
    turn as one retrieval failure does."""
    found = await deps.retrieve(
        plan.query, product=source.product, version=source.version, settings=deps.settings,
    )
    if plan.anchor is None:
        return found
    anchored = await deps.retrieve(
        plan.anchor, product=source.product, version=source.version, settings=deps.settings,
    )
    return _fuse_by_rank(found, anchored)


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
    # Bounded supplied-evidence projection (issue #635), derived from the
    # same finalized prepared prompt as `evidence`; empty on no-hits paths.
    supplied: tuple[SuppliedExcerpt, ...] = ()
    # The effort this answer was generated at (issue #780 answer records);
    # None on the empty-hits path, where no generation ran.
    reasoning_effort: str | None = None
    # Characters of the user's message left out of the prompt to fit the
    # window (issue #1192); 0 when it was sent whole and on no-hits paths.
    message_omitted_chars: int = 0


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


def _prompt_token_upper_bound(messages: list[ChatMessage]) -> int:
    """A non-undercounting upper bound on the rendered prompt's tokens (#1193).

    One token is at least one UTF-8 byte: every byte-level token maps to one or
    more bytes, so counting the UTF-8 bytes of each message's role and content
    can never be smaller than the text's token count. The fixed overhead covers
    chat-template special tokens (role markers, separators and the BOS/EOS the
    count includes). This is the quantity the estimator-only cap is priced
    against; the estimator itself is not trusted, because review measured an
    8x undercount on the default request shape: 8 excerpts of 4000
    space-separated hex characters estimated 4328 tokens against 27459 real,
    and the old ratio cap sent `max_tokens=10752`, making `real prompt + cap =
    38211 > 32768` for a request the base answered uncapped (PR #1201 review
    B1)."""
    return (
        sum(
            len(message.role.encode("utf-8")) + len(message.content.encode("utf-8"))
            for message in messages
        )
        + 8 * len(messages)
        + 16
    )


def _output_cap(settings: Settings, complexity: str, prepared: PreparedPrompt) -> int | None:
    """Completion cap (#777, #1193) bounding `real prompt + cap <= window -
    safety margin`, never an estimate plus a proportional margin.

    A server-verified count is exact: send the full cap (the answer reserve
    plus the path's thinking reserve, the terms the prompt budget priced), so a
    runaway generation ends as `length` (generation_incomplete) instead of
    running to the served window.

    An estimator-only count (a gateway without `/tokenize`, or a transient
    downgrade) cannot establish that bound, so the cap is priced against
    `_prompt_token_upper_bound(prepared.messages)` instead:
    `room = window - upper_bound - safety`; if the full cap fits in `room`,
    send it, otherwise send no cap. Never a cap smaller than the full one: the
    byte bound overstates real tokens (about 4x on the S2-1201 gate prompts),
    so a room-bounded cap of 3296 cut off a medium-effort answer the base
    completed uncapped (DIA-18, PR #1201 gate). With this rule an
    estimator-only generation either gets exactly the verified path's cap
    (the #777 runaway bound: an answer longer than that cap ends as `length`,
    as it would on the verified path) or runs uncapped with the base's request
    bytes. The cap adds no new server refusal and no new 502, and never a
    budget smaller than the verified path's. A prompt over its limit was
    already refused as `prompt_budget_exceeded` before this point.

    None also on the no-tokenizer character-packing path
    (`prompt_tokens is None`), which has no count to place a cap against
    (offline and tests; serving always has a tokenizer)."""
    thinking = (
        settings.llm_thinking_reserve_tokens_complex
        if complexity == "complex"
        else settings.llm_thinking_reserve_tokens_simple
    )
    full = settings.llm_reserved_output_tokens + thinking
    if prepared.budget_verified:
        return full
    if prepared.prompt_tokens is None:
        return None
    room = (
        settings.llm_max_model_len
        - _prompt_token_upper_bound(prepared.messages)
        - settings.llm_token_safety_margin
    )
    return full if room >= full else None


async def _build_prepared_prompt(
    input_data: AnswerCoreInput,
    deps: AnswerCoreDeps,
    hits: list[SearchHit],
    complexity: str,
    effort: str,
    root_ctx: Context | None,
    temperature: float | None = None,
    coding: bool = False,
) -> PreparedPrompt:
    """One prompt-build owner for the JSON and streaming executors: identical
    budget inputs, identical evidence manifest, one prompt.build span."""
    settings = deps.settings
    # Passed only when it fires (issue #1276), like search_query, so a
    # non-coding call keeps its exact keyword set.
    coding_kw: dict[str, Any] = {"coding": True} if coding else {}
    max_context = (
        settings.prompt_max_context_chars_complex
        if complexity == "complex"
        else settings.prompt_max_context_chars
    )
    with start_as_current_span(
        tracer,
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
            # Only a search that differs from the message's own retrieval
            # query is passed (issue #1268), so every other call is unchanged.
            # Both sides derive from the same prepared query: a literal plan's
            # search_text is exactly derive_retrieval_query(query).
            searched = _search_text(input_data, settings)
            extra = (
                {"search_query": searched}
                if searched != derive_retrieval_query(input_data.query, settings)
                else {}
            )
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
                reasoning_effort=effort,
                temperature=temperature,
                **extra,
                **coding_kw,
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
            reasoning_effort=effort,
            temperature=temperature,
            **coding_kw,
        )


class EvidenceBudgetExceeded(PromptBudgetExceeded):
    """Hits were retrieved but final prompt packing supplied no usable
    excerpt (issue #634). A PromptBudgetExceeded subtype, so every transport
    keeps its one fixed budget mapping (422 / SSE error / console banner);
    the distinct type only separates the log/span cause from irreducible
    fixed-content overflow. Carries no prompt or manual text."""

    def __init__(self, retrieved: int) -> None:
        Exception.__init__(
            self, f"no retrieved excerpt fit the prompt budget ({retrieved} retrieved)"
        )
        self.used = 0
        self.limit = 0
        self.retrieved = retrieved


def _require_supplied_evidence(prepared: PreparedPrompt, retrieved: int) -> None:
    """The one post-packing guard for both executors: the model is never
    asked to answer manual questions from zero supplied excerpts. An entry
    counts only when it retains non-empty source text."""
    if not any(entry.included_chars > 0 for entry in prepared.evidence.entries):
        raise EvidenceBudgetExceeded(retrieved)


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
    reasoning_effort: str | None = None,
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
            answer=parsed.answer,
            citations=parsed.citations,
            citations_inferred=parsed.citations_inferred,
            finish_reason=finish_reason,
            abstained=parsed.abstained,
            empty_hits=False,
            script_present=bool(parsed.script),
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
        supplied=prepared.supplied,
        reasoning_effort=reasoning_effort,
        message_omitted_chars=prepared.message_omitted_chars,
    )


async def _retrieve_inputs(
    source: AnswerCoreInput, deps: AnswerCoreDeps, parent_span: trace.Span | None,
) -> tuple[AnswerCoreInput, RetrievalResult]:
    """The retrieval leg, or the hits a route already retrieved; returns the
    input carrying the query that was searched (issue #1268)."""
    if source.hits is not None:
        return source, RetrievalResult(
            source.hits, source.query_kind or "unknown", source.timings or {}
        )
    plan = await plan_search(source, deps, parent_span)
    try:
        found = await retrieve_planned(plan, source, deps)
    except Exception as exc:
        raise RetrievalError(exc) from exc
    return replace(source, search_query=plan.search_text), found


def _search_text(input_data: AnswerCoreInput, settings: Settings) -> str:
    """What retrieval searched: the condensed or anchored search text a chat
    turn recorded, else the message's own derived retrieval query (issue
    #1192, #1268)."""
    if input_data.search_query is not None:
        return input_data.search_query
    return derive_retrieval_query(input_data.query, settings)


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

    input_data, retrieved = await _retrieve_inputs(input_data, deps, parent_span)
    hits, kind, timings = retrieved.hits, retrieved.kind, retrieved.timings

    classify_fn = deps.classify_query_complexity_fn or classify_query_complexity
    # Complexity and the no-hits wording read what retrieval searched, which
    # is the message itself within `query_max_chars` (issue #1192) unless a
    # chat follow-up was condensed (issue #1268).
    search_text = _search_text(input_data, settings)
    # Coding mode (issue #1276) is decided once here, from the operator
    # setting and the turn's own text, and routes the turn to the complex
    # path; with the setting off every turn keeps the classifier's result.
    coding = coding_mode_applies(input_data.query, settings)
    complexity = "complex" if coding else classify_fn(search_text)

    if not hits:
        return _empty_output(search_text, kind, timings, complexity)

    # 3. Prompt building (shared with the streaming executor). The temperature is resolved first:
    # a completion-usage count (#1266) sends the generation's own body.
    effort = _resolve_reasoning_effort(input_data, settings, complexity)
    temperature = (
        input_data.temperature if input_data.temperature is not None else settings.llm_temperature
    )
    prepared = await _build_prepared_prompt(
        input_data, deps, hits, complexity, effort, root_ctx, temperature, coding
    )
    _require_supplied_evidence(prepared, len(hits))

    # 4. LLM inference
    t0 = time.monotonic()
    with start_as_current_span(
        tracer,
        "llm.chat",
        kind=SpanKind.CLIENT,
        context=root_ctx,
        attributes={"llm.model": llm_model, "llm.reasoning_effort": effort},
    ) as llm_span:
        try:
            chat_res = await deps.llm.chat(
                prepared.messages,
                reasoning_effort=effort,
                temperature=temperature,
                max_tokens=_output_cap(settings, complexity, prepared),
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
            llm_span.add_event("exception", {"exception.type": error_type(exc)})
            llm_span.set_status(Status(StatusCode.ERROR, error_type(exc)))
            raise LLMChatError(exc) from exc

    llm_ms = int((time.monotonic() - t0) * 1000)
    return _finalize_answer(
        chat_res.content,
        prepared,
        hits=hits,
        kind=kind,
        timings=timings,
        complexity=complexity,
        reasoning_effort=effort,
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

    input_data, retrieved = await _retrieve_inputs(input_data, deps, parent_span)
    hits, kind, timings = retrieved.hits, retrieved.kind, retrieved.timings

    classify_fn = deps.classify_query_complexity_fn or classify_query_complexity
    # Complexity and the no-hits wording read what retrieval searched, which
    # is the message itself within `query_max_chars` (issue #1192) unless a
    # chat follow-up was condensed (issue #1268).
    search_text = _search_text(input_data, settings)
    # Coding mode (issue #1276) is decided once here, from the operator
    # setting and the turn's own text, and routes the turn to the complex
    # path; with the setting off every turn keeps the classifier's result.
    coding = coding_mode_applies(input_data.query, settings)
    complexity = "complex" if coding else classify_fn(search_text)

    # 2. Empty hits short-circuit: only the terminal record is yielded. The
    # empty answer is not a streamed token — /v1/answer's contract is a
    # single `final` (schema parity, review S6) and the chat routes emit the
    # canned text from the final output themselves.
    if not hits:
        yield {"type": "final", "output": _empty_output(search_text, kind, timings, complexity)}
        return

    # 3. Prompt building (shared with the buffered executor). The temperature is resolved first:
    # a completion-usage count (#1266) sends the generation's own body.
    effort = _resolve_reasoning_effort(input_data, settings, complexity)
    temperature = (
        input_data.temperature if input_data.temperature is not None else settings.llm_temperature
    )
    prepared = await _build_prepared_prompt(
        input_data, deps, hits, complexity, effort, root_ctx, temperature, coding
    )
    _require_supplied_evidence(prepared, len(hits))

    # 4. LLM streaming
    t0 = time.monotonic()
    ttft_ms: int | None = None
    content_parts: list[str] = []
    # No synthesized terminal: a stream is complete only with an explicit
    # non-empty string done finish (issue #365). The initial None means a
    # stream that never yielded a valid done is incomplete, even when tokens
    # arrived.
    finish_reason: str | None = None
    usage = TokenUsage()

    stream_gen = deps.llm.stream(
        prepared.messages, effort, temperature, _output_cap(settings, complexity, prepared)
    )

    with start_as_current_span(
        tracer,
        "llm.chat",
        kind=SpanKind.CLIENT,
        context=root_ctx,
        attributes={"llm.model": llm_model, "llm.reasoning_effort": effort},
    ) as llm_span:
        try:
            async for item in stream_gen:
                # A typed adapter is still a runtime boundary. Never let a
                # second terminal erase an incomplete finish or accept late text.
                if (finish_reason is not None or not isinstance(item, (ModelToken, ModelDone))
                        or (item.ttft_ms is not None and type(item.ttft_ms) is not int)):
                    raise TruncatedStreamError(len(content_parts), REASON_MALFORMED_FRAME)
                if isinstance(item, ModelToken):
                    if not isinstance(item.delta, str):
                        raise TruncatedStreamError(len(content_parts), REASON_MALFORMED_FRAME)
                    if item.delta:
                        if ttft_ms is None:
                            ttft_ms = item.ttft_ms or int((time.monotonic() - t0) * 1000)
                        content_parts.append(item.delta)
                        yield {"type": "token", "delta": item.delta, "ttft_ms": ttft_ms}
                else:
                    if (not isinstance(item.finish_reason, str) or not item.finish_reason
                            or not isinstance(item.usage, TokenUsage)):
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
            llm_span.add_event("exception", {"exception.type": error_type(exc)})
            llm_span.set_status(Status(StatusCode.ERROR, error_type(exc)))
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
        reasoning_effort=effort,
        finish_reason=finish_reason,
        usage=usage,
        llm_ms=llm_ms,
        ttft_ms=ttft_ms,
    )
    yield {"type": "final", "output": output}
