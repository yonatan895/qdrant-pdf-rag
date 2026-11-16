"""Follow-up turns for the two-turn evals (issue #1268): the one owner of the
follow-up questions, and the chat history the Open WebUI Pipe would send.

Follow-ups
    One question per golden query class, read by both the condense
    retrieval A/B (`eval.chat`) and the two-turn site answer eval
    (`eval.answers --turns 2`). Each refers back to the opener without
    naming its identifier: `condense_query` bypasses the rewrite when the
    latest turn carries a message id, member name or abend code, so a
    follow-up that names one measures nothing (a test pins this with the
    agent's own predicates).

History
    The second turn's body is what the Pipe would send after Open WebUI kept
    the first answer. The Pipe is loaded from the evaluator's own checkout
    (integrations/openwebui/docs_pipe.py) and run on the recorded first-turn
    frames, so the stored assistant text is the Pipe's own output, not a
    copy of its filter: model prose without the model's own citations
    section, followed by the agent's validated `**Citations:**` list (the
    Pipe keeps that list in the content; its source events go to the
    emitter, not the message). The second-turn request is the Pipe's
    `build_request` and `fit_request` over [question, kept answer,
    follow-up].

Follow-up kinds (`eval.answers --follow-up`)
    `anaphoric` is the class follow-up above (refers back, needs new
    evidence). `switch` asks another sampled row's own question instead (a
    topic switch; `switch_pairs`). `restate` and `summary` ask about the
    previous answer itself (`RESTATE_FOLLOW_UPS`). Every kind branches off
    the same opener and the same kept answer.

History as the agent sees it
    The agent strips each prior message, drops a pasted excerpts block and
    cuts what is left to `chat_max_prior_turn_chars`, keeping the head
    (`agent.answer.build_chat_messages`). The evaluator cannot read the
    deployed value, so the cap is the operator's declaration
    (`--prior-cap`); `history_view`, `history_exposure` and `gold_positions`
    apply it to the assistant message of the fitted request, the history as
    sent. A test pins `history_view` to the agent's own prompt builder."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

FOLLOW_UPS: dict[str, str] = {
    "message_id": "What does it mean and how do I resolve it?",
    "syntax": "What are the parameters for this?",
    "diagnostic": "How do I recover from it?",
    "doc_number": "What does this document cover?",
    "comparative": "How do the options compare?",
    "version": "What is the difference for my release?",
    "table": "Where is this laid out?",
}
DEFAULT_FOLLOW_UP = "Tell me more about this."

# Follow-ups about the previous answer itself (class B). Like the class
# follow-ups they name no identifier, so condensation is never bypassed.
RESTATE_FOLLOW_UPS: dict[str, str] = {
    "restate": "Repeat the exact command, message or name from your last answer.",
    "summary": "Sum up your last answer in three short bullet points.",
}
# Every follow-up kind, in the order a session asks them.
FOLLOW_UP_KINDS: tuple[str, ...] = ("anaphoric", "switch", *RESTATE_FOLLOW_UPS)
# Where the appended citation list lies against the prior-turn cut.
CITATION_LIST_STATES = frozenset({"absent", "kept", "partial", "cut"})
# Where a gold string lies in the history as the agent sees it.
GOLD_POSITIONS: tuple[str, ...] = ("before_cut", "after_cut", "not_in_history")
_CITATIONS_MARK = "**Citations:**"

# The evaluator's checkout root (src/mainframe_rag/eval/ -> repo root).
PIPE_PATH = Path(__file__).resolve().parents[3] / "integrations" / "openwebui" / "docs_pipe.py"


class PipeUnavailable(RuntimeError):
    """The Pipe file is missing or does not import (fixed message only)."""


def follow_up_for(query_class: str | None) -> str:
    return FOLLOW_UPS.get(query_class or "", DEFAULT_FOLLOW_UP)


def follow_ups_sha256() -> str:
    """Identity of the follow-up set, so two arms can prove they asked the same."""
    payload = json.dumps({"by_class": FOLLOW_UPS, "default": DEFAULT_FOLLOW_UP},
                         sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def restate_follow_ups_sha256() -> str:
    """Identity of the class-B follow-ups, apart from `follow_ups_sha256` so
    a run without `--follow-up` keeps its params."""
    payload = json.dumps(RESTATE_FOLLOW_UPS, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def every_follow_up_text() -> list[str]:
    """Every canned follow-up the evaluator can ask, for export reviews."""
    return [*FOLLOW_UPS.values(), DEFAULT_FOLLOW_UP, *RESTATE_FOLLOW_UPS.values()]


def switch_pairs(eligible: list[dict[str, Any]]) -> dict[str, str]:
    """Topic-switch partner per opener id: the next row after it in sample
    order (wrapping around) that shares no expected doc id with it. No seed:
    the same sample always pairs the same way. An opener with no such row
    is left out (it gets no switch turn)."""
    pairs: dict[str, str] = {}
    for i, entry in enumerate(eligible):
        gold = {str(d) for d in entry.get("expected_doc_ids") or []}
        for step in range(1, len(eligible)):
            other = eligible[(i + step) % len(eligible)]
            if not gold & {str(d) for d in other.get("expected_doc_ids") or []}:
                pairs[str(entry["id"])] = str(other["id"])
                break
    return pairs


def switch_pairs_sha256(sample_ids: list[str], pairs: dict[str, str]) -> str:
    """Identity of the pairing, so two arms can prove they switched alike."""
    payload = json.dumps([[i, pairs[i]] for i in sample_ids if i in pairs], ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def history_answer(request: dict[str, Any]) -> str:
    """The assistant message of a two-turn request as sent ("" when the
    Pipe dropped it)."""
    sent = [m["content"] for m in request["messages"][:-1] if m["role"] == "assistant"]
    return sent[-1] if sent else ""


def history_view(content: str) -> str:
    """One prior assistant message before the agent's cut: stripped, a
    pasted excerpts block dropped (the agent's own steps, in its order)."""
    from mainframe_rag.agent.answer import EXCERPTS_HEADER

    text = content.strip()
    if EXCERPTS_HEADER in text:
        text = text.split(EXCERPTS_HEADER)[0].strip()
    return text


def history_exposure(view: str, cap: int) -> tuple[bool, str]:
    """Whether the history answer exceeds the cap, and where its appended
    `**Citations:**` list lies against the cut: kept whole, partly cut, cut
    entirely, or absent."""
    start = view.rfind(_CITATIONS_MARK)
    if start < 0:
        state = "absent"
    elif len(view) <= cap:
        state = "kept"
    else:
        state = "cut" if start >= cap else "partial"
    return len(view) > cap, state


def gold_positions(view: str, gold: list[str], cap: int) -> list[str]:
    """Per gold string (case-folded, as the judge matches): wholly inside
    the kept head, present but not wholly inside it, or not in the history."""
    hay = view.lower()
    positions = []
    for s in gold:
        needle = str(s).lower()
        at = hay.find(needle)
        positions.append("not_in_history" if at < 0
                         else "before_cut" if at + len(needle) <= cap else "after_cut")
    return positions


def load_pipe(path: Path | None = None) -> tuple[ModuleType, str]:
    """The Pipe module from the evaluator's checkout and its file sha256.
    Loaded by path: the site runs the module with only `src/` on the path."""
    path = PIPE_PATH if path is None else path
    try:
        raw = path.read_bytes()
        spec = importlib.util.spec_from_file_location("mfr_eval_docs_pipe", path)
        if spec is None or spec.loader is None:
            raise PipeUnavailable("Open WebUI Pipe not loadable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The seams this module drives; a Pipe without them refuses here,
        # not as a history failure on every follow-up row.
        if not (hasattr(module.Pipe(), "_transport") and callable(module.build_request)
                and callable(module.fit_request) and type(module.DEFAULT_MAX_BODY_CHARS) is int):
            raise PipeUnavailable("Open WebUI Pipe not loadable")
    except PipeUnavailable:
        raise
    except Exception:  # noqa: BLE001 — a fixed message, never the import error text
        raise PipeUnavailable("Open WebUI Pipe not loadable") from None
    return module, hashlib.sha256(raw).hexdigest()


def kept_answer(pipe_mod: ModuleType, question: str, frames: list[str]) -> str:
    """The assistant message Open WebUI keeps for one answered turn: the
    Pipe run on the recorded `data:` lines of that turn's stream."""
    import httpx

    replay = "".join(f"{line}\n\n" for line in frames).encode("utf-8")

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=replay, headers={"content-type": "text/event-stream"})

    pipe = pipe_mod.Pipe()
    pipe._transport = httpx.MockTransport(respond)
    body = {"messages": [{"role": "user", "content": question}], "stream": False}

    async def run() -> Any:
        return await pipe.pipe(body, __user__=None, __event_emitter__=None,
                               __task__=None, __metadata__=None)

    result = asyncio.run(run())
    return result if isinstance(result, str) else ""


def second_turn_request(
    pipe_mod: ModuleType, question: str, kept: str, follow_up: str
) -> dict[str, Any] | None:
    """The Pipe's request for the follow-up after [question, kept answer],
    fitted to its default body cap (the deployed agent's
    chat_max_body_chars default); None when the Pipe would make no call."""
    return history_request(pipe_mod, [{"role": "user", "content": question},
                                      {"role": "assistant", "content": kept},
                                      {"role": "user", "content": follow_up}])


def history_request(pipe_mod: ModuleType, turns: list[dict[str, str]]) -> dict[str, Any] | None:
    """The Pipe's request for a conversation (its last turn is the question),
    fitted to its default body cap; None when the Pipe would make no call."""
    request = pipe_mod.build_request(turns, None)
    if request is None:
        return None
    fitted, _trimmed = pipe_mod.fit_request(request, pipe_mod.DEFAULT_MAX_BODY_CHARS)
    return fitted


# Fields a session row may carry for the structural judge, as on a golden row.
_SESSION_JUDGE_FIELDS = ("expected_verification_state", "gold_must_contain",
                         "gold_must_not_contain", "must_cite_identifier", "trap_type", "domain")


class SessionError(ValueError):
    """A malformed sessions file (fixed message: line number and rule only,
    never turn text)."""


def parse_sessions(raw: bytes) -> list[dict[str, Any]]:
    """Fixed-history sessions (issue #1191, graded follow-up rows): one JSON
    object per line with `id`, `query_class`, `expected_behavior`, optional
    `expected_doc_ids` and judge fields, and `turns`: user and assistant turns
    alternating, first and last a user turn, every content non-blank. The
    assistant turns are authored text, so the last turn is asked on a fixed
    history. The entry's `query` is the last user turn."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            raise SessionError(f"session line {number}: not JSON") from None
        if not isinstance(record, dict):
            raise SessionError(f"session line {number}: not an object")
        rid, cls, behavior = (record.get(k) for k in ("id", "query_class", "expected_behavior"))
        if not isinstance(rid, str) or not rid or rid in seen:
            raise SessionError(f"session line {number}: missing or duplicate id")
        if not isinstance(cls, str) or behavior not in ("answer", "abstain"):
            raise SessionError(f"session line {number}: query_class/expected_behavior")
        docs = record.get("expected_doc_ids") or []
        if not isinstance(docs, list) or not all(isinstance(d, str) for d in docs):
            raise SessionError(f"session line {number}: expected_doc_ids")
        turns = record.get("turns")
        if (
            not isinstance(turns, list) or not turns or len(turns) % 2 == 0
            or not all(isinstance(t, dict) and isinstance(t.get("content"), str)
                       and t["content"].strip()
                       and t.get("role") == ("user" if i % 2 == 0 else "assistant")
                       for i, t in enumerate(turns))
        ):
            raise SessionError(
                f"session line {number}: turns must alternate user/assistant, start and end "
                "on a user turn, with non-blank text")
        seen.add(rid)
        entries.append({
            "id": rid, "query": turns[-1]["content"], "query_class": cls,
            "expected_behavior": behavior, "expected_doc_ids": list(docs),
            **{k: record[k] for k in _SESSION_JUDGE_FIELDS if k in record},
            "turns": [{"role": t["role"], "content": t["content"]} for t in turns],
        })
    if not entries:
        raise SessionError("sessions file has no sessions")
    return entries


def prior_answer_chars(request: dict[str, Any]) -> int:
    """Characters of the assistant history the request carries (0 when the
    Pipe dropped an empty answer): what the agent's prior-turn cut sees."""
    return sum(len(m["content"]) for m in request["messages"][:-1] if m["role"] == "assistant")
