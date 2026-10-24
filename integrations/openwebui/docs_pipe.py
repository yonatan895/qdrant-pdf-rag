"""
title: Docs
author: mainframe-rag
version: 0.1.0
required_open_webui_version: 0.12.3
license: Apache-2.0
description: The documentation corpus through the agent's /v1/chat, with its validated citations, abstention and verification state.
"""

# Open WebUI Pipe (issue #1072): a stateless translator between Open WebUI and
# the agent's streaming POST /v1/chat. No tools, no model calls, no network
# call other than the configured agent URL, no logging. The header above is
# Open WebUI's function frontmatter; it deliberately has no `requirements:`
# line, because that would make Open WebUI pip-install packages at load time.
# Only httpx and pydantic are imported; both ship with Open WebUI.
# Contract and install procedure: docs/agent.md#openwebui-docs-pipe.

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, Field

OPEN_WEBUI_VERSION = "0.12.3"
CHAT_PATH = "/v1/chat"
ERROR_BODY_MAX_BYTES = 65536

Emitter = Callable[[dict[str, Any]], Awaitable[Any]]
# Anything Open WebUI could render as a link: a scheme (http:, javascript:,
# data:...), a bare `http` prefix, a protocol-relative `//` or `www.`.
_URL_LIKE = re.compile(r"\s*(?:http|www\.|//|[a-z][a-z0-9+.\-]*:)", re.IGNORECASE)

# Fixed user-visible texts. The agent's error `message` and any upstream body
# are never shown: only the stable `code` selects one of these.
_REJECTED = ("The documentation service rejected the request. Ask an administrator to check "
             "the Docs Pipe configuration.")
ERROR_MESSAGES: dict[str, str] = {
    "invalid_request": "The request was refused as invalid. Shorten or rephrase the question "
                       "and retry.",
    "prompt_budget_exceeded": "The question and conversation are too long for the model. "
                              "Start a new chat or shorten the question.",
    "upstream_error": "The documentation service could not complete this request. "
                      "Retry in a moment.",
    "model_timeout": "The reasoning model did not respond in time. Retry in a moment.",
    "deadline_exceeded": "The request took longer than the service allows. "
                         "Retry, or ask a narrower question.",
    "overloaded": "The documentation service is at capacity. Retry in a moment.",
    "not_configured": "The documentation service has no reasoning model configured. "
                      "Contact the operators.",
    "representation_unavailable": "The documentation index is not available right now. "
                                  "Retry later.",
    "internal": "The documentation service hit an internal error. Retry; if it persists, "
                "contact the operators.",
    "not_found": _REJECTED,
    "method_not_allowed": _REJECTED,
    "http_error": _REJECTED,
}
UNREACHABLE = ("The documentation service could not be reached. Retry; if it persists, "
               "contact the operators.")
TRUNCATED = ("The answer stream ended before it completed. "
             "Treat any text above as incomplete and retry.")
NO_TEXT_TURN = "Ask a question in plain text; attachments and images are not supported."
TRIMMED = "Earlier messages in this chat were not sent"
DEFAULT_MAX_BODY_CHARS = 32768  # the agent's Settings.chat_max_body_chars default
NO_EXCERPT = "Excerpt text is not available in this response."
TASK_REPLY = "Docs question"

WORKING = "Searching the documentation…"
FAILED = "Request failed"
STATE_LABELS: dict[str, str] = {
    "insufficient_evidence": "Insufficient evidence — not operational guidance",
    "unverified_draft": "Unverified draft — no eligible citation",
    "generation_incomplete": "Incomplete generation — output was cut off",
}
UNKNOWN_STATE = "Unverified — verification state unknown"
INFERRED = "Inferred citations — bracket provenance, not grounding"
SCRIPT_REVIEW = "Human review required — draft script, not certified executable"


class _Failure(Exception):
    """A fixed user-visible failure; carries the fixed text, never upstream text."""

    def __init__(self, text: str, *, incomplete: bool) -> None:
        super().__init__(text)
        self.text = text
        self.incomplete = incomplete


def _error_text(code: object) -> str:
    return ERROR_MESSAGES.get(code, UNREACHABLE) if isinstance(code, str) else UNREACHABLE


def _envelope_text(raw: bytes) -> str:
    """Fixed text for an agent error body: only a string `code` is read."""
    try:
        body = json.loads(raw)
    except ValueError:
        return UNREACHABLE
    return _error_text(body.get("code")) if isinstance(body, dict) else UNREACHABLE


def _valve(valves: object, name: str) -> str:
    value = valves.get(name) if isinstance(valves, dict) else getattr(valves, name, None)
    return value.strip() if isinstance(value, str) else ""


def _is_task(task: object, metadata: object, body: dict[str, Any]) -> bool:
    """A background task call (title, tags, follow-ups, summaries...). Open
    WebUI passes the task name as `__task__` (from the request metadata);
    the metadata and body keys are checked too, so a task is never mistaken
    for a docs question if the reserved argument is not populated."""
    body_metadata = body.get("metadata")
    return any((
        task,
        isinstance(metadata, dict) and metadata.get("task"),
        body.get("task"),
        isinstance(body_metadata, dict) and body_metadata.get("task"),
    ))


def _text(content: object) -> str:
    """Plain text of a message: a string, or the text parts of an Open WebUI
    content list joined by newlines (image and file parts dropped)."""
    if isinstance(content, str):
        return content if content.strip() else ""
    if not isinstance(content, list):
        return ""
    parts = [p.get("text") for p in content if isinstance(p, dict) and p.get("type") == "text"]
    joined = "\n".join(t for t in parts if isinstance(t, str) and t.strip())
    return joined if joined.strip() else ""


def build_request(messages: object, user_valves: object) -> dict[str, Any] | None:
    """The agent ChatRequest body: user/assistant turns as plain text
    (system prompts, image and file parts, tool turns and every other Open
    WebUI field are dropped), `stream: true`, and product/version only when
    the user set them. The last message is the question: None, so no agent
    call, unless it is a user turn with text. An earlier turn is never asked
    in its place."""
    items = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
    if not items or items[-1].get("role") != "user" or not _text(items[-1].get("content")):
        return None
    turns: list[dict[str, str]] = []
    for message in items:
        role, text = message.get("role"), _text(message.get("content"))
        if role in ("user", "assistant") and text:
            turns.append({"role": role, "content": text})
    body: dict[str, Any] = {"messages": turns, "stream": True}
    for name in ("product", "version"):
        if value := _valve(user_valves, name):
            body[name] = value
    return body


def fit_request(request: dict[str, Any], max_chars: int) -> tuple[dict[str, Any] | None, bool]:
    """Fit a built request to the agent's body cap, counted as the agent counts
    it (chat_body_chars): message characters plus product plus version. Drops
    the oldest turns first and then any leading assistant turn, so the history
    never starts with an answer to a dropped question. The last turn is always
    kept. Returns (request, trimmed); (None, False) when the last turn alone
    does not fit, so there is no call to make."""
    turns: list[dict[str, str]] = request["messages"]
    budget = max_chars - sum(len(request.get(name, "")) for name in ("product", "version"))
    if len(turns[-1]["content"]) > budget:
        return None, False
    total = sum(len(t["content"]) for t in turns)
    start = 0
    while start < len(turns) - 1 and (total > budget or turns[start]["role"] != "user"):
        total -= len(turns[start]["content"])
        start += 1
    if start == 0:
        return request, False
    return {**request, "messages": turns[start:]}, True


def _plain(value: object) -> object:
    """A display name Open WebUI cannot render as a link."""
    if isinstance(value, str) and _URL_LIKE.match(value):
        return "Citation: " + value
    return value


def _source_events(final: dict[str, Any]) -> list[dict[str, Any]]:
    """One Open WebUI `source` event per validated citation; the document is
    the excerpt text the agent supplied under that citation, never a search."""
    citations = final.get("citations")
    if not isinstance(citations, list):
        return []
    supplied = [e for e in final.get("supplied_evidence") or [] if isinstance(e, dict)]
    label = "inferred, not grounding" if final.get("citations_inferred") is True else "source-matched"
    events: list[dict[str, Any]] = []
    for citation in citations:
        if not isinstance(citation, str):
            continue
        matches = [e for e in supplied if e.get("citation") == citation]
        documents = [str(e.get("text", "")) for e in matches] or [NO_EXCERPT]
        metadata: list[dict[str, Any]] = [
            {"source": _plain(citation), "name": f"[{e.get('index')}] {_plain(citation)}",
             "doc_id": _plain(e.get("doc_id")), "page_label": e.get("page_label"),
             "product": e.get("product"), "version": e.get("version"), "label": label}
            for e in matches
        ] or [{"source": _plain(citation), "name": _plain(citation), "label": label}]
        doc_id = matches[0].get("doc_id") if matches else None
        events.append({"type": "source", "data": {
            "source": {"name": _plain(citation),
                       "id": _plain(doc_id if isinstance(doc_id, str) else citation)},
            "document": documents,
            "metadata": metadata,
        }})
    return events


def _status_text(final: dict[str, Any]) -> str:
    """The fixed verification banner for a completed answer."""
    state = final.get("verification_state")
    citations = final.get("citations")
    count = len(citations) if isinstance(citations, list) else 0
    if state == "accepted" and count and final.get("citations_inferred") is not True:
        parts = [f"Source-matched · {count} citation" + ("" if count == 1 else "s")]
    else:
        parts = [STATE_LABELS.get(state, UNKNOWN_STATE) if isinstance(state, str)
                 else UNKNOWN_STATE]
    if final.get("citations_inferred") is True:
        parts.append(INFERRED)
    if final.get("script_review_required") is True:
        parts.append(SCRIPT_REVIEW)
    return " · ".join(parts)


async def _deltas(lines: AsyncIterator[str], final: dict[str, Any]) -> AsyncIterator[str]:
    """Content deltas of an OpenAI-style chat stream. On success `final`
    holds the terminal choice (finish_reason plus the agent metadata). An
    error frame, a malformed frame, no finish or no [DONE] raise _Failure."""
    finished = False
    async for line in lines:
        if not line.startswith("data:"):
            continue  # blank lines, `:` keepalives, other SSE fields
        data = line[5:].strip()
        if data == "[DONE]":
            if not finished:
                break
            return
        try:
            frame = json.loads(data)
        except ValueError:
            raise _Failure(TRUNCATED, incomplete=True) from None
        if not isinstance(frame, dict):
            raise _Failure(TRUNCATED, incomplete=True)
        if "error" in frame:
            error = frame["error"]
            raise _Failure(_error_text(error.get("code") if isinstance(error, dict) else None),
                           incomplete=True)
        choices = frame.get("choices")
        if not isinstance(choices, list) or not choices:
            continue  # include_usage-style frame
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta")
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str) and content:
            yield content
        if choice.get("finish_reason") is not None:
            final.clear()
            final.update(choice)
            finished = True
    raise _Failure(TRUNCATED, incomplete=True)


async def _chunks(texts: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Each text as an OpenAI chunk dict. Open WebUI forwards a yielded string
    that starts with `data:` as a raw SSE frame and emits its `event`, so a
    model delta yielded as a string could forge a banner, source or replace
    event; inside a dict it is only ever JSON string content."""
    async for text in texts:
        yield {"object": "chat.completion.chunk",
               "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


class Pipe:
    class Valves(BaseModel):
        AGENT_URL: str = Field(
            default="http://rag-agent:8080",
            description="Agent base URL (in-cluster Service). The only host this Pipe calls.",
        )
        TIMEOUT_S: float = Field(
            default=300.0, gt=0,
            description="Read timeout for one answer, seconds (connect is bounded at 10 s).",
        )
        MAX_BODY_CHARS: int = Field(
            default=DEFAULT_MAX_BODY_CHARS, ge=1000, le=100000,
            description="Request body cap in characters. Must equal the deployed agent's "
                        "chat_max_body_chars; older turns are dropped to fit it.",
        )

    class UserValves(BaseModel):
        product: str = Field(default="", description="Restrict answers to this product.")
        version: str = Field(default="", description="Restrict answers to this version.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        # Tests inject a fake agent here; Open WebUI never sets it.
        self._transport: httpx.AsyncBaseTransport | None = None

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Emitter | None = None,
        __task__: str | None = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> str | AsyncIterator[dict[str, Any]]:
        if _is_task(__task__, __metadata__, body):
            # Title/tag/follow-up/summary generation is not a docs question:
            # a fixed short text, never an agent call (it would retrieve,
            # record a turn and carry the user's content).
            return TASK_REPLY
        user_valves = (__user__ or {}).get("valves")
        request = build_request(body.get("messages"), user_valves)
        if request is None:
            return NO_TEXT_TURN
        request, trimmed = fit_request(request, self.valves.MAX_BODY_CHARS)
        if request is None:
            return ERROR_MESSAGES["invalid_request"]  # the question alone exceeds the cap
        answer = self._answer(request, __event_emitter__, trimmed)
        if body.get("stream") is True:
            return _chunks(answer)
        return "".join([text async for text in answer])

    async def _answer(self, request: dict[str, Any], emit: Emitter | None,
                      trimmed: bool = False) -> AsyncIterator[str]:
        async def event(payload: dict[str, Any]) -> None:
            if emit is None:
                return
            try:
                await emit(payload)
            except Exception:  # noqa: BLE001 - Open WebUI's emitter, not our state
                return  # a lost banner must not fail or leak into the answer

        async def status(text: str, done: bool) -> None:
            await event({"type": "status", "data": {"description": text, "done": done}})

        started = False
        try:
            if trimmed:
                await status(TRIMMED, False)
            await status(WORKING, False)
            final: dict[str, Any] = {}
            timeout = httpx.Timeout(self.valves.TIMEOUT_S, connect=10.0)
            async with (
                httpx.AsyncClient(
                    base_url=self.valves.AGENT_URL.rstrip("/"), timeout=timeout,
                    transport=self._transport, trust_env=False, follow_redirects=False,
                ) as client,
                client.stream("POST", CHAT_PATH, json=request,
                              headers={"accept": "text/event-stream"}) as response,
            ):
                if response.status_code != 200:
                    raw = b""
                    async for chunk in response.aiter_bytes():
                        raw += chunk
                        if len(raw) > ERROR_BODY_MAX_BYTES:
                            break
                    raise _Failure(_envelope_text(raw[:ERROR_BODY_MAX_BYTES]), incomplete=False)
                async for delta in _deltas(response.aiter_lines(), final):
                    started = True
                    yield delta
            for source in _source_events(final):
                await event(source)
            await status(_status_text(final) + (f" · {TRIMMED}" if trimmed else ""), True)
        except _Failure as failure:
            yield ("\n\n" if started else "") + failure.text
            await status(STATE_LABELS["generation_incomplete"] if failure.incomplete else FAILED,
                         True)
        except Exception:  # noqa: BLE001 - the fixed-text boundary
            # Never raise into Open WebUI: it would show the exception text.
            yield ("\n\n" if started else "") + UNREACHABLE
            await status(FAILED, True)
