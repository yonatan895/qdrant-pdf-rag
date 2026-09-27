"""L2 judge prompts, call parameters, labels and cited-evidence measurement.

The L2 runner owns live execution; L4 owns repeated quality decisions.
Importing this module performs no model, storage, or application setup.
"""
from __future__ import annotations

import json
import re
from typing import Any

from mainframe_rag.ports import ChatMessage

# Evidence bound for the judge prompt: 8 chunk-capped excerpts can exceed
# 25k chars; the judge needs the claims' context, not the whole pool.
JUDGE_MAX_EVIDENCE_CHARS = 6000

CITE_PREFIX_RE = re.compile(r"^\s*\[\d+\]\s*")
JUDGE_LABELS = ("entailed", "neutral", "contradiction")
RELEVANCE_LABELS = ("relevant", "partial", "irrelevant")
JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

# Judge calls run at temp 0 with an explicit low reasoning effort. Measured
# (#305): with the server-default effort the judge burned 1200+ reasoning
# tokens on long answers and put its chain-of-thought in the content channel,
# so the JSON label never appeared and the row failed as a judge infra error.
# Low effort parses and keeps the label decision (DOC-02/VER-03: neutral both
# ways). The production answer path already sends this knob; the judge must
# too — a judge that flips with server defaults is not an instrument.
JUDGE_REASONING_EFFORT = "low"


class JudgeError(RuntimeError):
    """Judge output could not be parsed into a label — structural FAIL."""


def judge_chat(client: Any, messages: list[ChatMessage]) -> Any:
    """One judge call shape: temperature 0 + bounded reasoning effort. Every
    judge leg (faithfulness, relevance) funnels through here so the call
    parameters cannot diverge between the two labels."""
    return client.chat(messages, temperature=0.0, reasoning_effort=JUDGE_REASONING_EFFORT)


def citation_to_hit(citation: str, hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Map a validated citation string (`[n] {hit.cite}`) back to its hit by
    exact cite match. None when the hit is not in the fetched search rows —
    the caller records it rather than guessing a doc id with a regex."""
    cite = CITE_PREFIX_RE.sub("", citation)
    for h in hits:
        if h.get("cite") == cite:
            return h
    return None


def cited_doc_ids(citations: list[str], hits: list[dict[str, Any]]) -> tuple[set[str], list[str]]:
    """Doc ids behind validated citations, plus any citation that could not
    be mapped (recorded for diagnosis; never guessed)."""
    docs: set[str] = set()
    unmatched: list[str] = []
    for c in citations:
        hit = citation_to_hit(c, hits)
        if hit is None:
            unmatched.append(c)
        else:
            docs.add(str(hit.get("doc_id") or ""))
    return docs, unmatched


def precision_recall(cited: set[str], gold: set[str]) -> tuple[float | None, float | None]:
    """Doc-level citation precision/recall. None denominators (no citations
    / no gold) stay None so abstain rows and zero-hit paths never dilute
    the averages."""
    if not gold:
        return None, None
    if not cited:
        return None, 0.0
    inter = len(cited & gold)
    return inter / len(cited), inter / len(gold)


def judge_messages(answer: str, evidence: str) -> list[ChatMessage]:
    """NLI-style claim-vs-excerpt prompt. The judge sees evidence text and
    the answer body — never citation markers (the validator owns those).
    Returns ChatMessage rows so the production HttpxLLMClient can send them
    unmodified (one client shape, no second serializer)."""
    return [
        ChatMessage(
            role="system",
            content=(
                "You are a strict grounding judge for a retrieval-augmented "
                "answer system. You are given manual EXCERPTS and an ANSWER "
                'produced from them. Reply with exactly one JSON object: '
                '{"label": "entailed"} when every factual claim in the ANSWER '
                'is supported by the EXCERPTS, {"label": "contradiction"} '
                'when the EXCERPTS contradict a claim, {"label": "neutral"} '
                "when the ANSWER makes claims the EXCERPTS neither support "
                "nor contradict. No other text."
            ),
        ),
        ChatMessage(role="user", content=f"EXCERPTS:\n{evidence}\n\nANSWER:\n{answer}"),
    ]


def relevance_messages(question: str, answer: str) -> list[ChatMessage]:
    """Answer-relevance prompt: does the ANSWER address the QUESTION.

    Separate from faithfulness by design (one label per prompt): relevance
    needs no excerpts, so the judge never sees manual text or citation
    markers here. Returns ChatMessage rows for the production client.
    """
    return [
        ChatMessage(
            role="system",
            content=(
                "You are a strict answer-relevance judge for a "
                "retrieval-augmented question answering system. Given a "
                "QUESTION and an ANSWER, reply with exactly one JSON object: "
                '{"label": "relevant"} when the ANSWER directly addresses the '
                'QUESTION, {"label": "partial"} when it addresses only part '
                'of the QUESTION or drifts off-topic, {"label": "irrelevant"} '
                "when it does not address the QUESTION. No other text."
            ),
        ),
        ChatMessage(role="user", content=f"QUESTION:\n{question}\n\nANSWER:\n{answer}"),
    ]


def _parse_label(text: str, labels: tuple[str, ...]) -> str:
    """Extract one label from the judge's JSON reply (tolerates fences/prose
    around the object; anything else fails closed)."""
    m = JSON_BLOCK_RE.search(text)
    if m:
        try:
            label = json.loads(m.group(0)).get("label")
        except json.JSONDecodeError:
            label = None
        if label in labels:
            return label
    raise JudgeError(f"unparseable judge reply: {text[:120]!r}")


def parse_judge_label(text: str) -> str:
    """Faithfulness label (entailed/neutral/contradiction)."""
    return _parse_label(text, JUDGE_LABELS)


def parse_relevance_label(text: str) -> str:
    """Relevance label (relevant/partial/irrelevant)."""
    return _parse_label(text, RELEVANCE_LABELS)


def evidence_for_citations(citations: list[str], hits: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Excerpt text behind the cited hits (deterministic retrieval makes the
    /v1/search rows the same pool the agent answered from), bounded for the
    judge prompt. Returns (evidence, unmapped citations)."""
    texts: list[str] = []
    unmapped: list[str] = []
    for c in citations:
        hit = citation_to_hit(c, hits)
        if hit is None:
            unmapped.append(c)
        else:
            texts.append(str(hit.get("text") or ""))
    evidence = "\n\n---\n\n".join(texts)
    if len(evidence) > JUDGE_MAX_EVIDENCE_CHARS:
        evidence = evidence[:JUDGE_MAX_EVIDENCE_CHARS] + "\n…[truncated for the judge]"
    return evidence, unmapped


