# mypy: disallow_untyped_defs=True, disallow_untyped_calls=True, disallow_any_generics=True, warn_return_any=True, no_implicit_reexport=True, strict_equality=True, warn_unused_ignores=True
"""Candidate selection for the answer path (issue #1309, default off).

One low-effort call to the reasoning model picks which of one retrieval's
candidates are packed for the answer. The model sees a numbered list (cite,
heading, a bounded prefix of the text) and returns numbers only; a number
that names no supplied candidate selects nothing, so the stage can only
reorder and cut what retrieval returned. It never rewrites the query and
never retrieves. `answer_core.retrieve_for_answer` owns when it runs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from mainframe_rag.agent.answer import names_identifier
from mainframe_rag.agent.core_ports import AnswerModel
from mainframe_rag.config import Settings
from mainframe_rag.logs import error_type
from mainframe_rag.ports import ChatMessage
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.retrieve.screen import screen_query

log = logging.getLogger(__name__)

type SelectMode = Literal["selected", "bypass", "fallback"]
type SelectReason = Literal[
    "identifier", "trap", "few_candidates", "model_error", "invalid_output"
]

# Characters of each candidate shown to the model. The prefix bounds the
# selection prompt at about 40 x 1,000 characters at the widest list.
CANDIDATE_TEXT_CHARS = 600
CANDIDATE_LABEL_CHARS = 200

SELECT_SYSTEM_PROMPT = (
    "You choose which manual excerpts will be used to answer a question. "
    "You are given the question and a numbered list of candidate excerpts. "
    "Choose the excerpts that contain information needed to answer the question, "
    "most useful first, at most {limit}. "
    "Do NOT answer the question. "
    "Reply with ONLY a JSON array of the chosen excerpt numbers, for example [3, 1, 7]."
)


@dataclass(frozen=True)
class SelectOutcome:
    """What the stage did on one turn, text-free: fixed labels and counts.
    `candidates` is the number of hits the stage held; `selected` the
    number of packed hits the model chose (fill and fallback hits excluded)."""

    mode: SelectMode
    candidates: int
    selected: int = 0
    reason: SelectReason | None = None

    def span_attributes(self) -> dict[str, str | int]:
        attributes: dict[str, str | int] = {
            "rag.select_mode": self.mode,
            "rag.select_candidates": self.candidates,
            "rag.select_selected": self.selected,
        }
        if self.reason is not None:
            attributes["rag.select_reason"] = self.reason
        return attributes


def bypass_reason(query: str, anchor: str | None = None) -> SelectReason | None:
    """Why a turn is packed in retrieval order with no model call, decided
    from the searched text alone, before retrieval: the turn's own search
    names an identifier (the condensation and anchoring bypass,
    `names_identifier`), or a searched text screens as a trap."""
    if names_identifier(query):
        return "identifier"
    if any(screen_query(text) == "trap" for text in (query, anchor) if text is not None):
        return "trap"
    return None


def _clip(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def build_select_messages(
    query: str, anchor: str | None, candidates: list[SearchHit], limit: int,
) -> list[ChatMessage]:
    """The selection prompt: the searched question (and, on an anchored
    follow-up, the earlier question it was searched with) and each candidate
    as `[n] cite`, its heading and a bounded prefix of its text."""
    listed = "\n\n".join(
        f"[{number}] {_clip(hit.cite, CANDIDATE_LABEL_CHARS)}\n"
        f"{_clip(hit.heading, CANDIDATE_LABEL_CHARS)}\n"
        f"{_clip(hit.text, CANDIDATE_TEXT_CHARS)}"
        for number, hit in enumerate(candidates, start=1)
    )
    earlier = f"Earlier question: {anchor}\n" if anchor is not None else ""
    return [
        ChatMessage(role="system", content=SELECT_SYSTEM_PROMPT.format(limit=limit)),
        ChatMessage(
            role="user",
            content=(
                f"{earlier}Question: {query}\n\nCandidate excerpts:\n{listed}\n\n"
                "Chosen excerpt numbers:"
            ),
        ),
    ]


def parse_selection(content: str, count: int) -> list[int]:
    """Zero-based positions of the supplied candidates the reply names, in
    its order. The reply must be one JSON array; in it only an integer from
    1 to `count` counts, once. Anything else (other text, another JSON
    shape, a boolean, a string, a non-integer number, an unknown or repeated
    number) selects nothing, so the result may be empty."""
    try:
        parsed = json.loads(content)
    except (ValueError, RecursionError):
        return []
    if not isinstance(parsed, list):
        return []
    picked: list[int] = []
    for item in parsed:
        if type(item) is int and 1 <= item <= count and item - 1 not in picked:
            picked.append(item - 1)
    return picked


def order_selection(candidates: list[SearchHit], picked: list[int], limit: int) -> list[SearchHit]:
    """The one ordering rule: the model's choices in the model's order, then
    the remaining candidates in retrieval order until `limit` are packed.
    Change the packed order here only."""
    chosen = [candidates[position] for position in picked]
    rest = [hit for position, hit in enumerate(candidates) if position not in set(picked)]
    return (chosen + rest)[:limit]


async def select_candidates(
    llm: AnswerModel, settings: Settings, query: str, anchor: str | None,
    candidates: list[SearchHit],
) -> tuple[list[SearchHit], SelectOutcome]:
    """Pick `answer_hit_limit` of the candidates. No call when there are no
    more candidates than that. Any failure of the one call (transport,
    timeout, empty or unusable reply) packs the retrieval order instead, as
    condensation falls back to the raw query; cancellation propagates."""
    limit = settings.answer_hit_limit
    count = len(candidates)
    if count <= limit:
        return candidates, SelectOutcome("bypass", count, reason="few_candidates")
    try:
        result = await llm.chat(
            build_select_messages(query, anchor, candidates, limit),
            reasoning_effort=settings.reasoning_depth_effort("quick"),
            temperature=0.0,
        )
        content = result.content
    except Exception as exc:  # noqa: BLE001 — an optional stage must not fail the answer
        log.warning("candidate selection failed (%s); packing the retrieval order", error_type(exc))
        return candidates[:limit], SelectOutcome("fallback", count, reason="model_error")
    picked = parse_selection(content, count) if isinstance(content, str) else []
    if not picked:
        return candidates[:limit], SelectOutcome("fallback", count, reason="invalid_output")
    return (
        order_selection(candidates, picked, limit),
        SelectOutcome("selected", count, selected=min(len(picked), limit)),
    )
