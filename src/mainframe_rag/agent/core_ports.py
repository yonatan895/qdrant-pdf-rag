# mypy: disallow_untyped_defs=True, disallow_untyped_calls=True, disallow_any_generics=True, warn_return_any=True, no_implicit_reexport=True, strict_equality=True, warn_unused_ignores=True
"""Typed operations supplied to the answer use case, without storage/admin handles."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from mainframe_rag.agent.answer import PreparedPrompt
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage, ChatResult, Tokenizer, TokenUsage
from mainframe_rag.retrieve.query import SearchHit

if TYPE_CHECKING:
    from qdrant_client import models


@dataclass(frozen=True)
class RetrievalResult:
    hits: list[SearchHit]
    kind: str
    timings: dict[str, int]


class Retriever(Protocol):
    async def __call__(
        self, query: str, *, product: str | None, version: str | None, settings: Settings,
        limit: int | None = ...,
    ) -> RetrievalResult:
        """`limit` is passed only for a wider candidate list (issue #1309);
        without it the hit count is the operator's `answer_hit_limit`."""
        ...


class StoredChunkReader(Protocol):
    """The two stored-point reads neighbour expansion makes (issue #1314): a
    read-only subset of `AsyncQdrantReader`, the port GET /v1/evidence reads
    through. No search, write or admin surface."""

    async def retrieve(
        self, collection_name: str, ids: list[str], *,
        with_payload: bool | list[str], with_vectors: bool = False,
    ) -> list[models.Record]: ...

    async def scroll(
        self, collection_name: str, *,
        scroll_filter: models.Filter | None = None, limit: int = 10,
        with_payload: bool | list[str], offset: int | str | UUID | None = None,
    ) -> tuple[list[models.Record], int | str | UUID | None]: ...


class PromptBuilder[T](Protocol):
    def __call__(
        self, source: T, hits: list[SearchHit], /, *,
        product: str | None, version: str | None, splunk_context: str | None,
        max_context_chars: int, max_chunk_chars: int,
        max_chunk_chars_narrative: int | None, splunk_context_max_chars: int,
        complexity: str, tokenizer: Tokenizer | None, settings: Settings,
        order: Literal["retrieval", "stable_cache"],
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        # Chat only, and only when the search differs from the message's own
        # retrieval query (issue #1268); never passed otherwise.
        search_query: str | None = None,
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
        temperature: float | None = None, max_tokens: int | None = None,
    ) -> ChatResult: ...

    def stream(
        self, messages: list[ChatMessage], reasoning_effort: str,
        temperature: float, max_tokens: int | None = None,
    ) -> AsyncGenerator[ModelEvent]: ...
