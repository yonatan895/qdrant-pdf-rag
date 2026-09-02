"""Reasoning-model chat for /v1/answer.

Contract (architecture.md 4.6): /v1/answer must use the reasoning/thinking
model — never a cheap chat model. The only model this module can call is
settings.llm_model_reasoning; there is deliberately no other model knob.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx2
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

from mainframe_rag.agent.tokenizer import estimate_tokens
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage, ChatResult, Tokenizer, TokenUsage
from mainframe_rag.regexes import find_message_ids
from mainframe_rag.retrieve.query import SearchHit

FENCE_RE = re.compile(r"```([a-zA-Z0-9_-]*)\n(.*?)```", re.DOTALL)
SCRIPT_LANGS = frozenset({"jcl", "rexx", "sh", "bash", "shell", "python", "py", "yaml", "yml", "json", "ops", "rule", "parmlib"})

# Prompt-packing heuristics (tokenizer path). Chars-per-token bridges the
# estimator's token budget to char-space cuts; the verification loop against
# the real tokenizer is what actually guarantees the window.
_APPROX_CHARS_PER_TOKEN = 3.5
_TRUNCATED_SUFFIX = "\n... [truncated]"
_MAX_TRIM_ROUNDS = 4
_TRIM_OVERCUT_CHARS = 64
_MIN_TAIL_CHARS = 80


class ParsedAnswer(BaseModel):
    answer: str
    citations: list[str] = Field(default_factory=list)
    script: str | None = None
    citations_inferred: bool = False
    inferred_indices: list[int] = Field(default_factory=list)


SYSTEM_PROMPT = (
    "You are a mainframe operations expert (z/OS, CICS, Db2, IMS, JES2/3, RACF, "
    "z/VM, VTAM, OPS/MVS). Answer operational questions. Rules:\n"
    "1. Only assert facts and parameters that the supplied excerpts support. Do not invent fictitious keywords or commands.\n"
    "2. When asked how to code or configure a specific case, apply the syntax templates, grammars, and parameter rules documented in the excerpts to the user's scenario. Do not refuse to synthesize code, JCL, rules, or commands simply because the manual lacks an identical verbatim example for the user's specific values.\n"
    "3. If manuals disagree between versions, say which version each statement comes from.\n"
    "4. If the excerpts truly do not contain the syntax, parameters, or rules to answer the question, say so explicitly.\n"
    "5. When you propose JCL, REXX, rule definitions, or operator steps, put them in a fenced code block and explain how they map to the documented syntax. Scripts are examples, not production-ready without review.\n"
    "6. You MUST end your reply with a 'Citations:' section listing the exact citation strings of the excerpts used, for example:\n"
    "Citations:\n"
    "SA22-7592-05 z/OS MVS Initialization and Tuning Reference, IEASYSxx > LFAREA, p. 1-17\n"
)

COMPLEX_ROOTS = ("diagnos", "recover", "abend", "compar", "tuning", "optimi", "tradeoff")


def classify_query_complexity(query: str) -> str:
    """Classifies query as 'simple' (factoid, message id, or single parameter lookup)
    or 'complex' (diagnostic, procedural, comparative, tuning, or multi-step inquiry)."""
    q_lower = query.lower().strip()

    # If the query contains a message ID (e.g. "How do I resolve IEA500I IOSCMDS command rejected and what operator action is needed?"),
    # keep it simple unless explicit deep diagnostic/recovery/abend/compare/tuning roots are present.
    msg_ids = find_message_ids(query)
    has_complex_root = any(r in q_lower for r in COMPLEX_ROOTS)
    if msg_ids and not has_complex_root:
        return "simple"

    if has_complex_root:
        return "complex"

    # Multi-step configuration / procedural intent
    if any(k in q_lower for k in ("how to configure", "how do i configure", "how to setup", "how do i create", "step by step", "steps to", "configuration procedure")):
        return "complex"

    # Comparative analysis
    if any(k in q_lower for k in ("versus", " vs ", "difference between")):
        return "complex"

    # Specific operational procedures or rule definitions
    if any(k in q_lower for k in ("how to", "how do", "how can", "explain how", "procedure")) and any(
        w in q_lower for w in ("rule", "interval", "parameter", "parmlib", "jcl", "policy", "threshold", "journal")
    ):
        return "complex"

    return "simple"


SYSTEM_PROMPT_COMPLEX_EXTENSION = (
    "\nReasoning & Synthesis Protocol for Complex Mainframe Inquiries:\n"
    "When answering complex diagnostic, procedural, or architectural questions:\n"
    "1. Deep Internal Reasoning: In your internal thinking, conduct thorough step-by-step analysis:\n"
    "   - Analyze the exact operational scenario, failure mode, and system components involved.\n"
    "   - Systematically examine each retrieved excerpt for syntax specifications, parameter values, return codes, hardware/software prerequisites, and operational limits.\n"
    "   - When synthesizing rules, JCL, or commands: extract the documented syntax template/grammar, map the user's scenario values into each parameter slot, and verify the resulting statement against the documented rules.\n"
    "   - Cross-verify statements across excerpts before synthesizing the answer.\n"
    "2. High-Actionability Response Structure:\n"
    "   - Structure your response logically with clear technical headings.\n"
    "   - Provide concrete, verified syntax, parmlib statements, rule definitions, or JCL examples in fenced code blocks whenever procedures or configurations are discussed.\n"
    "   - Explain the derivation of each parameter from the documented syntax rules.\n"
    "   - Detail both diagnosis (what happened and how to verify) and recovery (exact remediation steps).\n"
    "3. Explicit Citations:\n"
    "   - You MUST end your reply with the 'Citations:' section explicitly listing each excerpt citation used.\n"
)


def build_messages(
    query: str,
    hits: list[SearchHit],
    product: str | None = None,
    version: str | None = None,
    splunk_context: str | None = None,
    max_context_chars: int = 8000,
    max_chunk_chars: int = 3000,
    max_chunk_chars_narrative: int | None = None,
    complexity: str | None = None,
    tokenizer: Tokenizer | None = None,
    settings: Settings | None = None,
) -> list[ChatMessage]:
    if complexity is None:
        complexity = classify_query_complexity(query)

    parts: list[str] = []
    context_bits = []
    if product:
        context_bits.append(f"product: {product}")
    if version:
        context_bits.append(f"version: {version}")
    if context_bits:
        parts.append("Sysplex context: " + ", ".join(context_bits))
    if splunk_context:
        parts.append(
            "Splunk context (live system observation; join key is the message ID):\n"
            + splunk_context.strip()
        )
    parts.append("Question: " + query)

    system_content = (
        SYSTEM_PROMPT + SYSTEM_PROMPT_COMPLEX_EXTENSION
        if complexity == "complex"
        else SYSTEM_PROMPT
    )

    example_cite = (
        hits[0].cite
        if hits
        else "SA22-7592-05 z/OS MVS Initialization and Tuning Reference, IEASYSxx > LFAREA, p. 1-17"
    )
    tail_part = (
        "Please answer based strictly on the retrieved manual excerpts above and conclude with the 'Citations:' section copying the exact citation line for each excerpt used, for example:\n"
        f"Citations:\n{example_cite}"
    )

    packed: list[tuple[str, str]] = []
    if tokenizer is not None:
        if settings is None:
            raise ValueError("settings is required when a tokenizer is provided")
        model_len = settings.llm_max_model_len
        reserved = settings.llm_reserved_output_tokens
        margin = settings.llm_token_safety_margin
        narrative_token_cap = settings.llm_max_chunk_tokens_narrative

        # Planning is estimator-only (zero RPC): the budget for chunk bodies
        # after the fixed preamble (system prompt + context + question +
        # trailing citation example).
        fixed_tokens = estimate_tokens(
            system_content + "\n" + "\n".join(parts) + "\n" + tail_part
        )
        budget_tokens = max(100, model_len - reserved - margin - fixed_tokens)

        total_tokens = 0
        for i, hit in enumerate(hits, 1):
            text = hit.text.strip()
            if hit.chunk_type not in ("syntax", "message", "table"):
                if max_chunk_chars_narrative is not None and len(text) > max_chunk_chars_narrative:
                    text = text[:max_chunk_chars_narrative].rstrip() + _TRUNCATED_SUFFIX
                elif complexity == "complex" and estimate_tokens(text) > narrative_token_cap:
                    text = text[: int(narrative_token_cap * _APPROX_CHARS_PER_TOKEN)].rstrip() + _TRUNCATED_SUFFIX
            elif len(text) > max_chunk_chars:
                text = text[:max_chunk_chars].rstrip() + _TRUNCATED_SUFFIX
            header = f"[{i}] {hit.cite}"
            chunk_tokens = estimate_tokens(f"{header}\n{text}")
            if total_tokens + chunk_tokens > budget_tokens and packed:
                rem_tokens = budget_tokens - total_tokens
                # The char cut must leave room for the header too, or the
                # packed sum can exceed the budget by the header size.
                body_rem_tokens = rem_tokens - estimate_tokens(header)
                if body_rem_tokens > 60:
                    packed.append(
                        (
                            header,
                            text[: int(body_rem_tokens * _APPROX_CHARS_PER_TOKEN)].rstrip() + _TRUNCATED_SUFFIX,
                        )
                    )
                break
            packed.append((header, text))
            total_tokens += chunk_tokens

        # Verification is the only tokenizer work: count the packed prompt
        # once, chat-template aware, and trim the tail if the estimator
        # drifted past the window. Bounded rounds; a prompt that still does
        # not fit surfaces later as finish_reason=length (alerted in app).
        verify_limit = model_len - reserved
        for _ in range(_MAX_TRIM_ROUNDS):
            if not packed:
                break
            used = tokenizer.count_messages(
                [
                    ChatMessage(role="system", content=system_content),
                    ChatMessage(role="user", content=_user_content(parts, packed, tail_part)),
                ]
            )
            if used <= verify_limit:
                break
            overshoot = used - verify_limit
            cut = int(overshoot * _APPROX_CHARS_PER_TOKEN) + _TRIM_OVERCUT_CHARS
            header, body = packed[-1]
            trimmed = body[:-cut] if cut < len(body) else ""
            if len(trimmed.rstrip()) < _MIN_TAIL_CHARS:
                packed.pop()
            else:
                packed[-1] = (header, trimmed.rstrip() + _TRUNCATED_SUFFIX)
    else:
        total_chars = 0
        for i, hit in enumerate(hits, 1):
            text = hit.text.strip()
            # High-fidelity chunk types (syntax, message, table) preserve their grammar/structure
            # up to max_chunk_chars; narrative prose is bounded by max_chunk_chars_narrative if provided.
            narrative_cap = (
                max_chunk_chars_narrative
                if max_chunk_chars_narrative is not None
                else max_chunk_chars
            )
            chunk_cap = (
                max_chunk_chars
                if hit.chunk_type in ("syntax", "message", "table")
                else min(max_chunk_chars, narrative_cap)
            )
            if len(text) > chunk_cap:
                text = text[:chunk_cap].rstrip() + _TRUNCATED_SUFFIX
            header = f"[{i}] {hit.cite}"
            chunk_len = len(header) + 1 + len(text)
            if total_chars + chunk_len > max_context_chars and packed:
                remaining = max_context_chars - total_chars
                if remaining > 200:
                    packed.append((header, text[:remaining].rstrip() + _TRUNCATED_SUFFIX))
                break
            packed.append((header, text))
            total_chars += chunk_len

    parts.append("Retrieved manual excerpts:\n" + "\n\n".join(f"{h}\n{b}" for h, b in packed))
    parts.append(tail_part)

    return [
        ChatMessage(role="system", content=system_content),
        ChatMessage(role="user", content="\n\n".join(parts)),
    ]


def _user_content(parts: list[str], packed: list[tuple[str, str]], tail_part: str) -> str:
    """The final user message, used by the verification loop to count the
    exact prompt that will be sent."""
    excerpt_part = "Retrieved manual excerpts:\n" + "\n\n".join(f"{h}\n{b}" for h, b in packed)
    return "\n\n".join([*parts, excerpt_part, tail_part])


def as_chat_result(raw: ChatResult | str) -> ChatResult:
    """Single adapter for LLMClient.chat() results: the production client
    returns ChatResult; test doubles may still return a bare string. Every
    consumer (app, query_demo) funnels through here instead of carrying its
    own isinstance/hasattr branch."""
    if isinstance(raw, ChatResult):
        return raw
    return ChatResult(content=str(raw), finish_reason="stop", usage=TokenUsage())


def assert_reasoning_model(settings: Settings) -> tuple[str, str]:
    """Fail closed: no reasoning model configured, no LLM call. Returns
    (base_url, model) so callers never re-read the Optional settings fields."""
    if not settings.llm_base_url:
        raise RuntimeError("LLM_BASE_URL is unset; /v1/answer cannot run.")
    return settings.llm_base_url, settings.require_reasoning_model()


class HttpxLLMClient:
    """LLMClient implementation: the reasoning model only — deliberately no
    other model knob (architecture.md 4.6). LLM env fails closed at request
    time (assert_reasoning_model, called by /v1/answer before retrieval);
    startup fail-fast covers the embed path (PR D).
    Owns its own connection pool with the long answer timeout (do NOT share
    the embed client's short timeout). No retries: /v1/answer is a single
    shot — a retry would re-ask a reasoning model that may already be
    thinking, and answers are not idempotent (issue #20 PR C)."""

    def __init__(self, settings: Settings, client: httpx2.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    def _http(self) -> httpx2.Client:
        if self._client is None:
            # retries=0: connection blips surface as errors, never re-issued.
            self._client = httpx2.Client(
                timeout=self._settings.answer_timeout_s,
                transport=httpx2.HTTPTransport(retries=0),
            )
        return self._client

    def close(self) -> None:
        # Deliberately does not null the client: a post-shutdown chat() must
        # fail loudly on the closed pool, never silently rebuild one.
        if self._client is not None:
            self._client.close()

    def chat(
        self,
        messages: list[ChatMessage],
        reasoning_effort: str | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        base_url, model = assert_reasoning_model(self._settings)
        serialized = [m.model_dump() for m in messages]
        body: dict[str, Any] = {"model": model, "messages": serialized}
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if temperature is not None:
            body["temperature"] = temperature
        if getattr(self._settings, "llm_stream", False) and hasattr(self._http(), "stream"):
            body_stream = {**body, "stream": True, "stream_options": {"include_usage": True}}
            try:
                t0 = time.monotonic()
                ttft_ms: int | None = None
                content_parts: list[str] = []
                finish_reason = "stop"
                usage_data: dict[str, Any] = {}
                with self._http().stream(
                    "POST",
                    f"{base_url.rstrip('/')}/chat/completions",
                    json=body_stream,
                ) as stream_resp:
                    stream_resp.raise_for_status()
                    for line in stream_resp.iter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        chunk_str = line[5:].strip()
                        if chunk_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(chunk_str)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage_data = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            delta_content = delta.get("content")
                            if delta_content:
                                if ttft_ms is None:
                                    ttft_ms = int((time.monotonic() - t0) * 1000)
                                content_parts.append(delta_content)
                            if choices[0].get("finish_reason"):
                                finish_reason = str(choices[0]["finish_reason"])
                content = "".join(content_parts)
                if not content:
                    log.warning("streaming chat returned empty content; falling back to non-streaming POST")
                else:
                    reasoning_tokens = (
                        usage_data.get("reasoning_tokens")
                        or (usage_data.get("completion_tokens_details") or {}).get("reasoning_tokens")
                        or 0
                    )
                    usage = TokenUsage(
                        prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
                        completion_tokens=int(usage_data.get("completion_tokens") or 0),
                        reasoning_tokens=int(reasoning_tokens),
                        total_tokens=int(usage_data.get("total_tokens") or 0),
                    )
                    return ChatResult(content=content, finish_reason=finish_reason, usage=usage, ttft_ms=ttft_ms)
            except (httpx2.HTTPError, json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
                log.warning("streaming chat failed (%s); falling back to non-streaming POST", exc)

        resp = self._http().post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        # Reasoning models may return content: None (all output went to the
        # reasoning channel) — that must surface as "", never the string "None".
        content = str(choice["message"].get("content") or "")
        finish_reason = str(choice.get("finish_reason") or "stop")
        usage_data = data.get("usage") or {}
        reasoning_tokens = (
            usage_data.get("reasoning_tokens")
            or (usage_data.get("completion_tokens_details") or {}).get("reasoning_tokens")
            or 0
        )
        usage = TokenUsage(
            prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
            completion_tokens=int(usage_data.get("completion_tokens") or 0),
            reasoning_tokens=int(reasoning_tokens),
            total_tokens=int(usage_data.get("total_tokens") or 0),
        )
        return ChatResult(content=content, finish_reason=finish_reason, usage=usage)


def parse_answer(
    content: str,
    allowed_citations: set[str],
    ordered_cites: list[str] | None = None,
) -> ParsedAnswer:
    """Split model output into answer, validated citations, optional script.

    The `citations` list and the answer body are filtered to the hit set.
    `script` (fenced block) is code and deliberately passes through
    unvalidated — stripping citation-looking lines would corrupt examples.
    Documented behavior, pinned by test (issue #20 PR C)."""
    from mainframe_rag.agent.cites import (
        extract_body_and_citations,
        strip_unauthorized_citations,
    )

    # 1. Process code fences: extract scripts, drop thinking blocks, unwrap prose fences
    scripts: list[str] = []
    text_processed = content
    for match in FENCE_RE.finditer(content):
        lang = match.group(1).strip().lower()
        code = match.group(2).strip()
        if lang in SCRIPT_LANGS:
            scripts.append(code)
            text_processed = text_processed.replace(match.group(0), "")
        elif lang in ("thought", "thinking"):
            text_processed = text_processed.replace(match.group(0), "")
        else:
            # Unlabeled or prose markdown code fence - unwrap into answer body
            text_processed = text_processed.replace(match.group(0), code)

    script = "\n\n".join(scripts).strip() if scripts else None

    # 2. Extract citations & body prose
    body, raw_cite_lines = extract_body_and_citations(text_processed)

    citations: list[str] = []
    for c in raw_cite_lines:
        if c in allowed_citations and c not in citations:
            citations.append(c)

    citations_inferred = False
    inferred_indices: list[int] = []

    if not citations:
        # Check if model ended the response with citation lines matching allowed_citations
        # even if the literal 'Citations:' header was omitted.
        from mainframe_rag.agent.cites import normalize_citation_line

        body_lines = body.splitlines()
        trailing_cites: list[str] = []
        while body_lines:
            candidate = body_lines[-1].strip()
            if not candidate:
                body_lines.pop()
                continue
            norm = normalize_citation_line(candidate)
            if norm in allowed_citations:
                if norm not in trailing_cites:
                    trailing_cites.append(norm)
                body_lines.pop()
            else:
                break
        if trailing_cites:
            trailing_cites.reverse()
            citations = trailing_cites
            body = "\n".join(body_lines)

    if not citations and ordered_cites:
        # Strictly match bracketed numbers like [1], [2], [1, 2] corresponding to [{i}] prompt excerpts.
        # Parentheses (e.g. "z/OS (3.1)", "(2)", "APARs (1, 2)") are ignored to avoid false inference.
        for match in re.finditer(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]", content):
            for num_str in re.findall(r"\b\d+\b", match.group(1)):
                idx = int(num_str) - 1
                if 0 <= idx < len(ordered_cites):
                    cite = ordered_cites[idx]
                    if cite in allowed_citations and cite not in citations:
                        citations.append(cite)
                        inferred_indices.append(idx + 1)
                        citations_inferred = True

    # 3. Clean up unauthorized citations in body
    body = strip_unauthorized_citations(body, allowed_citations)

    return ParsedAnswer(
        answer=body.strip(),
        citations=citations,
        script=script,
        citations_inferred=citations_inferred,
        inferred_indices=inferred_indices,
    )
