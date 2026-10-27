"""Builders shared by the agent/answer/HTTP suites.

Complements `tests/fakes.py`: that module owns transport doubles, this one
owns the synthetic manual hit and the real application client those suites
drive. Expected outcomes stay explicit in each test.
"""

from __future__ import annotations

import contextlib
import json

from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import TruncatedStreamError
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.retrieve.query import SearchHit

_FALLBACK = FallbackTokenizer()

_AGENT_ENV = {
    "QDRANT_URL": "http://localhost:6333",
    # CI/dev embed profile: hash mode, explicitly allowed (PR D fail-fast).
    "EMBED_MODE": "hash",
    "ALLOW_HASH_MODE": "true",
    "LLM_BASE_URL": "http://llm.internal/v1",
    "LLM_MODEL_REASONING": "test-reasoning-model",
}


def agent_hit(
    text: str = "IEA500I synthetic text",
    doc_id: str = "SA22-0000-00",
    *,
    cite: str | None = None,
    heading: str = "Chapter 2 > IEA500I",
    page_label: str = "1-6",
    chunk_id: str = "abc123",
    chunk_type: str = "message",
    **overrides,
) -> SearchHit:
    """The synthetic manual hit the agent suites retrieve. The cite keeps the
    manual shape, so citation tests exercise the allowlist rather than the
    shape gate; `cite=`/`**overrides` pin a different field."""
    return SearchHit(
        chunk_id=chunk_id,
        score=0.42,
        cite=cite or f"{doc_id} Synthetic Reference, {heading}, p. {page_label}",
        heading=heading,
        text=text,
        doc_id=doc_id,
        title="Synthetic Reference",
        page_label=page_label,
        chunk_type=chunk_type,
        product="z/OS",
        version="9.9",
        message_ids=("IEA500I",),
        **overrides,
    )


def set_agent_env(monkeypatch, **overrides) -> None:
    """Register the hermetic agent env (hash mode is explicitly allowed)."""
    for key, value in {**_AGENT_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


@contextlib.contextmanager
def agent_client(
    monkeypatch,
    *,
    llm=None,
    search=None,
    tokenizer=_FALLBACK,
    env: dict[str, str] | None = None,
):
    """Real app lifespan over TestClient with the hermetic env in place.
    `search`/`tokenizer` are patched before the lifespan builds the app,
    `llm` after (the lifespan owns that module global until then)."""
    set_agent_env(monkeypatch, **(env or {}))
    if search is not None:
        monkeypatch.setattr(app_mod, "retrieve_search", search)
    with TestClient(app_mod.app) as client:
        if llm is not None:
            monkeypatch.setattr(app_mod, "llm", llm)
        if tokenizer is not None:
            monkeypatch.setattr(app_mod, "tokenizer", tokenizer)
        yield client


def sse_events(text: str) -> list[tuple[str, dict]]:
    """(event-name, payload) pairs of a raw `event:`/`data:` stream; multi-line
    data is joined and a frame without `event:` reads as "message" (unlike
    `fakes.parse_sse_events`, which ignores both)."""
    events: list[tuple[str, dict]] = []
    name = "message"
    data: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if data:
                events.append((name, json.loads("\n".join(data))))
                name, data = "message", []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
    if data:
        events.append((name, json.loads("\n".join(data))))
    return events


def chat_frames(text: str) -> list[dict]:
    """JSON objects from `data:` lines, excluding the `[DONE]` sentinel."""
    return [
        json.loads(line[len("data: "):])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


class StreamStubLLM:
    """Streams the given items, then either ends or raises `error`; `chat`
    fails loudly because the streaming surfaces must never call it."""

    def __init__(self, items: tuple[dict, ...], error: BaseException | None = None):
        self.items = items
        self.error = error

    async def chat_stream(self, messages, *args, **kwargs):
        for item in self.items:
            yield item
        if self.error is not None:
            raise self.error

    def chat(self, *args, **kwargs):
        raise AssertionError("non-stream chat must not run on the stream path")


def truncation_after_token(reason: int = 1) -> StreamStubLLM:
    """One visible token, then a typed incomplete-upstream failure."""
    return StreamStubLLM(
        ({"type": "token", "delta": "Partial ", "token": "Partial ", "ttft_ms": 12},),
        TruncatedStreamError(reason),
    )
