"""Layer-boundary protocols (issue #20 PR A).

These are the only types layers may use to talk to each other for embed,
Qdrant points, and LLM access. Implementations: VllmEmbedder / HashEmbedder
(ingest.embed), qdrant_client.QdrantClient (satisfies QdrantPoints
structurally — parameter names/returns mirror the real client), HttpxLLMClient
(agent.answer).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from qdrant_client import models

SparseVector = tuple[list[int], list[float]]


class ChatMessage(BaseModel):
    role: str
    content: str


@runtime_checkable
class Embedder(Protocol):
    """Dense + sparse text embedding. Two implementations: VllmEmbedder
    (prod) and HashEmbedder (CI/dev only). Callers never branch on
    embed_mode — dispatch happens once in build_embedder()."""

    def dense(self, texts: list[str]) -> list[list[float]]: ...

    def dense_query(self, queries: list[str]) -> list[list[float]]: ...

    def sparse(self, texts: list[str]) -> list[SparseVector]: ...


@runtime_checkable
class QdrantPoints(Protocol):
    """The Qdrant surface this project actually uses — only these methods may
    appear at layer edges. Unit tests fake this protocol, which is why the
    query_points signature (query_filter, not filter) stays honest. Parameter
    names mirror qdrant_client.QdrantClient so the real client satisfies the
    protocol structurally."""

    def collection_exists(self, collection_name: str) -> bool: ...

    def get_collection(self, collection_name: str) -> models.CollectionInfo: ...

    def create_collection(
        self,
        collection_name: str,
        *,
        vectors_config: dict[str, models.VectorParams],
        sparse_vectors_config: dict[str, models.SparseVectorParams],
        on_disk_payload: bool,
    ) -> bool: ...

    def create_payload_index(
        self,
        collection_name: str,
        *,
        field_name: str,
        field_schema: models.PayloadSchemaType,
    ) -> models.UpdateResult: ...

    def update_collection(
        self,
        collection_name: str,
        *,
        optimizer_config: models.OptimizersConfigDiff,
    ) -> bool: ...

    def scroll(
        self,
        collection_name: str,
        *,
        scroll_filter: models.Filter | None = None,
        limit: int = 10,
        with_payload: bool | list[str],
    ) -> tuple[list[models.Record], int | str | UUID | None]: ...

    def delete(
        self,
        collection_name: str,
        *,
        points_selector: models.FilterSelector,
        wait: bool = True,
    ) -> models.UpdateResult: ...

    def upsert(
        self,
        collection_name: str,
        *,
        points: list[models.PointStruct],
        wait: bool = True,
    ) -> models.UpdateResult: ...

    def query_points(
        self,
        collection_name: str,
        *,
        query: list[float] | models.SparseVector,
        using: str,
        limit: int,
        query_filter: models.Filter | None,
        with_payload: bool | list[str],
    ) -> models.QueryResponse: ...

    def query_batch_points(
        self,
        collection_name: str,
        *,
        requests: list[models.QueryRequest],
    ) -> list[models.QueryResponse]: ...


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


class ChatResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: str
    finish_reason: str = "stop"
    usage: TokenUsage = Field(default_factory=TokenUsage)


@runtime_checkable
class Tokenizer(Protocol):
    """Token counting for context budgeting. Implementations: VllmTokenizer
    (one /tokenize RPC per call — reserve it for whole-prompt verification,
    never per-chunk counting) and FallbackTokenizer (in-process estimator)."""

    def count_tokens(self, text: str) -> int: ...

    def count_messages(self, messages: list[ChatMessage]) -> int: ...


@runtime_checkable
class LLMClient(Protocol):
    """Reasoning-model chat (answer path only). Implementations fail closed
    when no reasoning model is configured."""

    def chat(
        self,
        messages: list[ChatMessage],
        reasoning_effort: str | None = None,
        temperature: float | None = None,
    ) -> ChatResult: ...
