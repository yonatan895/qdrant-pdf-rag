"""
title: Rosetta rating
author: mainframe-rag
version: 0.1.0
required_open_webui_version: 0.12.3
license: Apache-2.0
description: Rate a Rosetta answer. The rating goes to the agent's feedback store, never to Open WebUI.
"""

# Open WebUI Action (issue #1255): two message buttons that rate a Rosetta
# answer through the agent's POST /v1/feedback. The rating, and the optional
# reason, are stored only by the agent's feedback store (ADR-0005); Open
# WebUI's own ratings stay off (ENABLE_MESSAGE_RATING=false). Nothing about
# the rating is written to Open WebUI's database: the Action emits only a
# toast (`notification`, never saved) and asks for the reason through an
# `input` event call, and it returns None (a returned `messages` would be
# merged into the chat). It never raises: Open WebUI would show the exception
# text and save it as the message's error. No tools, no model calls, no
# network call other than the configured agent URL, no logging. Like the docs
# Pipe it has no `requirements:` line and imports only what Open WebUI ships.
# It declares no user, request or OAuth-token argument (ADR-0007 V2; a test
# pins the signature): the agent records no rater (ADR-0005 D5).
# Contract: docs/agent.md#openwebui-ratings.

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

OPEN_WEBUI_VERSION = "0.12.3"
FEEDBACK_PATH = "/v1/feedback"
ERROR_BODY_MAX_BYTES = 65536

Emitter = Callable[[dict[str, Any]], Awaitable[Any]]

# The docs Pipe ends a rateable answer with this line (docs_pipe.py
# REFERENCE_LINE); a test pins the two patterns equal.
REFERENCE_RE = re.compile(r"\n\nAnswer reference: `([0-9a-f]{12})`[ \t\r\n]*\Z")
# Sub-action id -> the feedback store's rating vocabulary.
RATINGS: dict[str, str] = {"up": "good", "down": "bad"}
REASON_MAX_CHARS = 2000  # the store's COMMENT_MAX_CHARS; a test pins it

# Fixed user-visible texts (a proposal; the wording is a maintainer decision,
# #1255). The agent's error `message` is never shown: only its `code` selects.
RECORDED = "Thanks, your rating was recorded."
NOT_RATEABLE = "This answer cannot be rated: it has no answer reference."
UNAVAILABLE = "Ratings are temporarily unavailable. Retry later."
TOO_LONG = f"The reason is too long: at most {REASON_MAX_CHARS} characters."
ERROR_TEXTS: dict[str, str] = {
    "unknown_answer": "This answer is not recorded yet, or no longer; it cannot be rated now.",
    "invalid_request": "The rating was refused. Shorten the reason and retry.",
    "feedback_disabled": "Ratings are not enabled on this deployment.",
}
REASON_PROMPT: dict[str, str] = {
    "title": "What was wrong with this answer?",
    "message": "Optional. A short reason helps the documentation team. "
               "Do not include passwords or other secrets.",
    "placeholder": "Optional reason",
}

_UP = ("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdC"
       "b3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0iMiIg"
       "c3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PHBhdGggZD0iTTMgMTFoNHYxMEgzeiIvPjxwYXRoIGQ9Ik03IDExbDQt"
       "OGMxLjUgMCAyLjUgMSAyLjUgMi41VjloNS41YTIgMiAwIDAgMSAyIDIuM2wtMS4zIDcuN2EyIDIgMCAwIDEtMiAx"
       "LjdINyIvPjwvc3ZnPg==")
_DOWN = ("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZX"
         "dCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0i"
         "MiIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PGcgdHJhbnNmb3JtPSJtYXRyaXgoMSAwIDAgLTEgMCAyNCkiPjxw"
         "YXRoIGQ9Ik0zIDExaDR2MTBIM3oiLz48cGF0aCBkPSJNNyAxMWw0LThjMS41IDAgMi41IDEgMi41IDIuNVY5aDUu"
         "NWEyIDIgMCAwIDEgMiAyLjNsLTEuMyA3LjdhMiAyIDAgMCAxLTIgMS43SDciLz48L2c+PC9zdmc+")


def _reference(body: object) -> str | None:
    """The agent request id of the clicked answer: the last message of the
    browser's list must be the clicked one (`body.id`), an assistant turn
    whose string content ends with the Pipe's reference line."""
    if not isinstance(body, dict):
        return None
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        return None
    last = messages[-1]
    content = last.get("content")
    if last.get("id") != body.get("id") or last.get("role") != "assistant" \
            or not isinstance(content, str):
        return None
    match = REFERENCE_RE.search(content)
    return match.group(1) if match else None


def _outcome(status: int, raw: bytes) -> tuple[bool, str]:
    if status == 201:
        return True, RECORDED
    try:
        document = json.loads(raw)
    except ValueError:
        return False, UNAVAILABLE
    code = document.get("code") if isinstance(document, dict) else None
    return False, ERROR_TEXTS.get(code, UNAVAILABLE) if isinstance(code, str) else UNAVAILABLE


class Action:
    # Two buttons under each answer; Open WebUI passes the sub-action id as `__id__`.
    actions: ClassVar[list[dict[str, str]]] = [
        {"id": "up", "name": "Good answer", "icon_url": _UP},
        {"id": "down", "name": "Bad answer", "icon_url": _DOWN},
    ]

    class Valves(BaseModel):
        AGENT_URL: str = Field(
            default="http://rag-agent:8080",
            description="Agent base URL (in-cluster Service). The only host this Action calls.",
        )
        TIMEOUT_S: float = Field(default=10.0, gt=0, le=60,
                                 description="Timeout for one rating call, seconds.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        # Tests inject a fake agent here; Open WebUI never sets it.
        self._transport: httpx.AsyncBaseTransport | None = None

    async def action(
        self,
        body: dict[str, Any],
        __id__: str | None = None,
        __event_emitter__: Emitter | None = None,
        __event_call__: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> None:
        try:
            result = await self._rate(body, __id__, __event_call__)
        except Exception:  # noqa: BLE001 - the fixed-text boundary
            result = (False, UNAVAILABLE)
        if result is not None and __event_emitter__ is not None:
            ok, text = result
            # Open WebUI's emitter, not our state: a lost toast must not raise.
            with contextlib.suppress(Exception):
                await __event_emitter__({"type": "notification",
                                         "data": {"type": "success" if ok else "error",
                                                  "content": text}})

    async def _rate(self, body: dict[str, Any], action_id: object,
                    ask: Callable[[dict[str, Any]], Awaitable[Any]] | None
                    ) -> tuple[bool, str] | None:
        """(recorded, fixed text), or None when the user cancelled."""
        rating = RATINGS.get(action_id) if isinstance(action_id, str) else None
        if rating is None:
            return False, UNAVAILABLE
        request_id = _reference(body)
        if request_id is None:
            return False, NOT_RATEABLE
        comment = ""
        if rating == "bad" and ask is not None:
            reply = await ask({"type": "input", "data": dict(REASON_PROMPT)})
            if not isinstance(reply, str):
                return None  # cancelled (False), or the browser is gone (an error dict)
            comment = reply.strip()
            if len(comment) > REASON_MAX_CHARS:
                return False, TOO_LONG
        timeout = httpx.Timeout(self.valves.TIMEOUT_S, connect=min(self.valves.TIMEOUT_S, 10.0))
        async with httpx.AsyncClient(
            base_url=self.valves.AGENT_URL.rstrip("/"), timeout=timeout,
            transport=self._transport, trust_env=False, follow_redirects=False,
        ) as client:
            response = await client.post(FEEDBACK_PATH, json={
                "request_id": request_id, "rating": rating, "comment": comment})
            raw = response.content[:ERROR_BODY_MAX_BYTES]
        return _outcome(response.status_code, raw)
