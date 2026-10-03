# mypy: disallow_untyped_defs=True, disallow_untyped_calls=True, disallow_any_generics=True, warn_return_any=True, no_implicit_reexport=True, strict_equality=True, warn_unused_ignores=True
"""Application-scoped resources, passed explicitly to request code.

The lifespan in `agent/app.py` is the only OWNER of the clients (it creates and
closes them). `AgentResources` is a read-only, per-request VIEW of what the
lifespan published: it never closes anything, and a request that captured one
keeps those clients even if the published names are replaced mid-flight. It
carries no Qdrant admin/write surface beyond the existing read protocol, and it
imports neither the application singleton nor any transport.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mainframe_rag.agent.answer_core import AnswerCoreDeps
from mainframe_rag.agent.core_ports import RetrievalResult
from mainframe_rag.agent.model_adapter import ModelAdapter
from mainframe_rag.config import Settings
from mainframe_rag.ports import (
    AsyncQdrantPoints,
    Embedder,
    LLMClient,
    QdrantPoints,
    Reranker,
    Tokenizer,
)
from mainframe_rag.retrieve.query import SearchHit

type SearchOutcome = tuple[list[SearchHit], str, dict[str, int]]
# Sync doubles resolve inline; the pooled production retriever is awaited.
type SearchFn = Callable[..., SearchOutcome | Awaitable[SearchOutcome]]


async def await_retrieval(res: SearchOutcome | Awaitable[SearchOutcome]) -> SearchOutcome:
    """Sync/async retrieval-leg shim: the pooled async client awaits while
    sync test doubles resolve inline — one helper serves every endpoint so
    the call sites cannot diverge (review S2)."""
    if inspect.isawaitable(res):
        return await res
    return res


@dataclass(frozen=True)
class AgentResources:
    settings: Settings
    qdrant: AsyncQdrantPoints | QdrantPoints
    embedder: Embedder
    reranker: Reranker | None
    llm: LLMClient
    tokenizer: Tokenizer | None
    search: SearchFn

    async def retrieve(
        self, query: str, *, product: str | None, version: str | None,
        settings: Settings, limit: int = 8,
    ) -> RetrievalResult:
        """The one retrieval wiring for /v1/search, /v1/answer, /v1/chat and the
        console. `settings` carries the validated physical collection."""
        hits, kind, timings = await await_retrieval(self.search(
            self.qdrant, self.embedder, settings.qdrant_collection, query,
            product=product, version=version, limit=limit, settings=settings,
            reranker=self.reranker,
        ))
        return RetrievalResult(hits, kind, timings)

    def core_deps(self, settings: Settings | None = None) -> AnswerCoreDeps:
        """Answer-use-case dependencies over THIS view; the model adapter
        borrows `llm` and never closes it."""

        async def retrieve(
            query: str, *, product: str | None, version: str | None, settings: Settings
        ) -> RetrievalResult:
            return await self.retrieve(query, product=product, version=version, settings=settings)

        return AnswerCoreDeps(
            settings=settings or self.settings,
            llm=ModelAdapter(self.llm),
            retrieve=retrieve,
            tokenizer=self.tokenizer,
        )
