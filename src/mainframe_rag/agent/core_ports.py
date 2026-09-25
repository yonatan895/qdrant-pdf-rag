# mypy: disallow_untyped_defs=True, disallow_untyped_calls=True, disallow_any_generics=True, warn_return_any=True, no_implicit_reexport=True, strict_equality=True, warn_unused_ignores=True
"""Typed operations supplied to the answer use case, without storage/admin handles."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Literal, Protocol

from mainframe_rag.agent.answer import PreparedPrompt
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage, ChatResult, Tokenizer, TokenUsage
from mainframe_rag.retrieve.query import SearchHit


@dataclass(frozen=True)
class RetrievalResult:
    hits: list[SearchHit]
    kind: str
    timings: dict[str, int]


class Retriever(Protocol):
    async def __call__(
        self, query: str, *, product: str | None, version: str | None, settings: Settings
    ) -> RetrievalResult: ...


class PromptBuilder[T](Protocol):
    def __call__(
        self, source: T, hits: list[SearchHit], /, *,
        product: str | None, version: str | None, splunk_context: str | None,
        max_context_chars: int, max_chunk_chars: int,
        max_chunk_chars_narrative: int | None, splunk_context_max_chars: int,
        complexity: str, tokenizer: Tokenizer | None, settings: Settings,
        order: Literal["retrieval", "stable_cache"],
    ) -> PreparedPrompt: ...


@dataclass(frozen=True)
class ModelToken:
    delta: str
    ttft_ms: int | None = None


@dataclass(frozen=True)
class ModelDone:
    finish_reason: str
    usage: TokenUsage
    ttft_ms: int | None = None


type ModelEvent = ModelToken | ModelDone


class AnswerModel(Protocol):
    async def chat(
        self, messages: list[ChatMessage], reasoning_effort: str | None = None,
        temperature: float | None = None,
    ) -> ChatResult: ...

    def stream(
        self, messages: list[ChatMessage], reasoning_effort: str,
        temperature: float,
    ) -> AsyncGenerator[ModelEvent]: ...
