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
    follow-up]."""

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
    messages = [{"role": "user", "content": question},
                {"role": "assistant", "content": kept},
                {"role": "user", "content": follow_up}]
    request = pipe_mod.build_request(messages, None)
    if request is None:
        return None
    fitted, _trimmed = pipe_mod.fit_request(request, pipe_mod.DEFAULT_MAX_BODY_CHARS)
    return fitted


def prior_answer_chars(request: dict[str, Any]) -> int:
    """Characters of the assistant history the request carries (0 when the
    Pipe dropped an empty answer): what the agent's prior-turn cut sees."""
    return sum(len(m["content"]) for m in request["messages"][:-1] if m["role"] == "assistant")
