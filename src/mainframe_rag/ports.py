"""Layer-boundary protocols (issue #20 PR A).

These are the only types layers may use to talk to each other for embed,
Qdrant points, and LLM access. Implementations: VllmEmbedder / HashEmbedder
(ingest.embed), qdrant_client.QdrantClient (satisfies QdrantPoints and QdrantReader
structurally — parameter names/returns mirror the real client), HttpxLLMClient
(agent.answer), HttpZoweMCP (agent.zowe_mcp, ADR-0003).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from qdrant_client import models

SparseVector = tuple[list[int], list[float]]


async def maybe_await[T](value: T | Awaitable[T]) -> T:
    """The one sync/async compatibility rule (issue #369): production
    clients return awaitables, sync tooling and test doubles return values.
    Awaited when awaitable, returned as-is otherwise; cancellation and errors
    propagate unchanged. Every serving call site that accepts either shape
    goes through here."""
    if inspect.isawaitable(value):
        return await value
    return value


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


@runtime_checkable
class Embedder(Protocol):
    """Dense + sparse text embedding. Two implementations: VllmEmbedder
    (prod) and HashEmbedder (CI/dev only). Callers never branch on
    embed_mode — dispatch happens once in build_embedder()."""

    def dense(self, texts: list[str]) -> list[list[float]]: ...

    def dense_query(self, queries: list[str]) -> list[list[float]]: ...

    def sparse(self, texts: list[str]) -> list[SparseVector]: ...


@runtime_checkable
class Reranker(Protocol):
    """Relevance scoring protocol for candidate chunks (issue #76 PR-02).
    Implementations: HttpReranker (prod vLLM/TEI),
    HashReranker (CI/dev)."""

    def score(self, query: str, texts: list[str]) -> list[float]: ...


@runtime_checkable
class QdrantReader(Protocol):
    """The read-only Qdrant surface (issue #369): alias and collection
    lookup, scroll/retrieve, dense/sparse queries and cluster inspection. Serving
    consumers (retrieval, the serving gate, the evidence service, the agent's
    resources) are typed against this protocol, so the type checker rejects a
    write through them; read-only Qdrant credentials remain the runtime
    boundary. Unit tests fake it, which is why the query_points signature
    (query_filter, not filter) stays honest. Parameter names mirror
    qdrant_client.QdrantClient so the real client satisfies it structurally."""

    def collection_exists(self, collection_name: str) -> bool: ...

    def get_collection(self, collection_name: str) -> models.CollectionInfo: ...

    def get_aliases(self) -> models.CollectionsAliasesResponse: ...

    def scroll(
        self,
        collection_name: str,
        *,
        scroll_filter: models.Filter | None = None,
        limit: int = 10,
        with_payload: bool | list[str],
        with_vectors: bool = False,
        offset: int | str | UUID | None = None,
    ) -> tuple[list[models.Record], int | str | UUID | None]: ...

    def retrieve(
        self,
        collection_name: str,
        ids: list[str],
        *,
        with_payload: bool | list[str],
        with_vectors: bool = False,
    ) -> list[models.Record]: ...

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

    def collection_cluster_info(
        self, collection_name: str
    ) -> models.CollectionClusterInfo: ...

    def cluster_status(self) -> models.ClusterStatus: ...


@runtime_checkable
class QdrantPoints(QdrantReader, Protocol):
    """The full Qdrant surface ingest/publication uses: the read surface plus
    collection/alias/snapshot administration and point writes. Only the
    writer (ingest and admin tooling) holds it."""

    def create_collection(
        self,
        collection_name: str,
        *,
        vectors_config: dict[str, models.VectorParams],
        sparse_vectors_config: dict[str, models.SparseVectorParams],
        on_disk_payload: bool,
        shard_number: int | None = None,
        replication_factor: int | None = None,
        write_consistency_factor: int | None = None,
    ) -> bool: ...

    def delete_collection(self, collection_name: str) -> bool: ...

    def update_collection_aliases(
        self,
        change_aliases_operations: list[
            models.CreateAliasOperation | models.DeleteAliasOperation
        ],
    ) -> bool: ...

    def create_snapshot(
        self, collection_name: str, *, wait: bool = True
    ) -> models.SnapshotDescription | None: ...

    def recover_snapshot(
        self,
        collection_name: str,
        location: str,
        *,
        priority: models.SnapshotPriority | None = None,
        wait: bool = True,
    ) -> bool | None: ...

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

    def delete(
        self,
        collection_name: str,
        *,
        points_selector: models.FilterSelector | models.PointIdsList,
        wait: bool = True,
    ) -> models.UpdateResult: ...

    def upsert(
        self,
        collection_name: str,
        *,
        points: list[models.PointStruct],
        wait: bool = True,
    ) -> models.UpdateResult: ...


@runtime_checkable
class AsyncQdrantReader(Protocol):
    """The async read surface for agent endpoints (issues #77, #369): the
    same reads as `QdrantReader`, plus close. Mirrors
    qdrant_client.AsyncQdrantClient. Nothing in the async serving path
    writes, so no async write surface exists."""

    async def collection_exists(self, collection_name: str) -> bool: ...

    async def get_collection(self, collection_name: str) -> models.CollectionInfo: ...

    async def get_aliases(self) -> models.CollectionsAliasesResponse: ...

    async def scroll(
        self,
        collection_name: str,
        *,
        scroll_filter: models.Filter | None = None,
        limit: int = 10,
        with_payload: bool | list[str],
        offset: int | str | UUID | None = None,
    ) -> tuple[list[models.Record], int | str | UUID | None]: ...

    async def retrieve(
        self,
        collection_name: str,
        ids: list[str],
        *,
        with_payload: bool | list[str],
        with_vectors: bool = False,
    ) -> list[models.Record]: ...

    async def query_points(
        self,
        collection_name: str,
        *,
        query: list[float] | models.SparseVector,
        using: str,
        limit: int,
        query_filter: models.Filter | None,
        with_payload: bool | list[str],
    ) -> models.QueryResponse: ...

    async def query_batch_points(
        self,
        collection_name: str,
        *,
        requests: list[models.QueryRequest],
    ) -> list[models.QueryResponse]: ...

    async def close(self) -> None: ...


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
    ttft_ms: int | None = None


@runtime_checkable
class Tokenizer(Protocol):
    """Token counting for context budgeting. Implementations: VllmTokenizer
    (one /tokenize RPC per call — reserve it for whole-prompt verification,
    never per-chunk counting) and FallbackTokenizer (in-process estimator)."""

    def count_tokens(self, text: str) -> int: ...

    def count_messages(self, messages: list[ChatMessage]) -> int: ...


@runtime_checkable
class ZoweMCP(Protocol):
    """Read-only live z/OS state over the MCP bridge (ADR-0003).
    Implementation: HttpZoweMCP (agent.zowe_mcp, Streamable HTTP). Sync by
    protocol — callers offload with asyncio.to_thread like the reranker.
    Tool results are untrusted data; screening happens at the call site."""

    def call_tool(self, name: str, arguments: dict[str, str]) -> dict: ...

    def close(self) -> None: ...


@runtime_checkable
class LLMClient(Protocol):
    """Reasoning-model chat (answer path only). Implementations fail closed
    when no reasoning model is configured. May return ChatResult or Awaitable[ChatResult].

    Implementations may additionally expose ``async chat_stream(messages, ...)
    -> AsyncIterator[dict]`` (yielding {"type": "token", ...} then a terminal
    {"type": "done", ...} item); /v1/answer streaming duck-types this
    capability via hasattr and falls back to non-streaming chat otherwise.
    There is deliberately no separate async-chat protocol: HttpxLLMClient's
    chat() resolves to a coroutine when called on a running loop, and every
    consumer funnels through as_chat_result / maybe_await.
    """

    def chat(
        self,
        messages: list[ChatMessage],
        reasoning_effort: str | None = None,
        temperature: float | None = None,
    ) -> ChatResult | Awaitable[ChatResult]: ...
