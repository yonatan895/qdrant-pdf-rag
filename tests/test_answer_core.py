"""Unit tests for the shared answer core (M2 extraction).

Route handlers pass precomputed hits, so these tests drive the branches the
routes do not: the core's own retrieval leg (success and typed failure), the
condensation gate, and the empty-hits stream shape.
"""

from __future__ import annotations

import pytest

from mainframe_rag.agent.answer_core import (
    AnswerCoreDeps,
    AnswerCoreInput,
    LLMChatError,
    RetrievalError,
    chat_body_chars,
    execute_answer_core,
    execute_answer_core_stream,
    resolve_search_query,
)
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage, ChatResult, TokenUsage
from mainframe_rag.retrieve.query import SearchHit


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        embed_mode="hash",
        allow_hash_mode=True,
        **overrides,
    )


def _hit() -> SearchHit:
    return SearchHit(
        chunk_id="abc123",
        score=0.42,
        cite="SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6",
        heading="Chapter 2 > IEA500I",
        text="IEA500I synthetic text",
        doc_id="SA22-0000-00",
        title="Synthetic Reference",
        page_label="1-6",
        chunk_type="message",
        product="z/OS",
        version="9.9",
        message_ids=("IEA500I",),
    )


class CoreFakeLLM:
    def __init__(self, content: str | None = None, raise_exc: Exception | None = None):
        self.content = content or (
            "Reissue the command.\n\nCitations:\n"
            "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
        )
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def chat(self, messages, reasoning_effort=None, temperature=None):
        self.calls.append({"messages": messages, "reasoning_effort": reasoning_effort})
        if self.raise_exc is not None:
            raise self.raise_exc
        return ChatResult(content=self.content, finish_reason="stop", usage=TokenUsage())


def _deps(settings: Settings, llm, retrieve) -> AnswerCoreDeps:
    return AnswerCoreDeps(
        settings=settings,
        llm=llm,
        qdrant=None,
        embedder=None,
        retrieve_search_fn=retrieve,
    )


def test_chat_body_chars_counts_messages_and_context():
    messages = [
        ChatMessage(role="user", content="abc"),
        ChatMessage(role="assistant", content="de"),
    ]
    assert chat_body_chars(messages) == 5
    assert chat_body_chars(messages, "xyz") == 8
    assert chat_body_chars(messages, "") == 5


@pytest.mark.anyio
async def test_core_retrieval_branch_feeds_prompt_and_output():
    calls: list[str] = []

    def retrieve(qdrant, embedder, collection, query, **kwargs):
        calls.append(query)
        return [_hit()], "identifier", {"embed_ms": 3, "qdrant_ms": 4}

    llm = CoreFakeLLM()
    out = await execute_answer_core(
        AnswerCoreInput(query="IEA500I rejected"),
        _deps(_settings(), llm, retrieve),
    )

    assert calls == ["IEA500I rejected"]
    assert out.query_kind == "identifier"
    assert out.timings == {"embed_ms": 3, "qdrant_ms": 4}
    assert out.citations == [_hit().cite]
    assert out.answer == "Reissue the command."
    assert llm.calls[0]["messages"][-1].role == "user"


@pytest.mark.anyio
async def test_core_retrieval_failure_raises_typed_error():
    def retrieve(*_a, **_k):
        raise RuntimeError("qdrant exploded")

    with pytest.raises(RetrievalError) as excinfo:
        await execute_answer_core(
            AnswerCoreInput(query="IEA500I"),
            _deps(_settings(), CoreFakeLLM(), retrieve),
        )
    assert isinstance(excinfo.value.original, RuntimeError)


@pytest.mark.anyio
async def test_core_stream_retrieval_failure_raises_typed_error():
    def retrieve(*_a, **_k):
        raise RuntimeError("qdrant exploded")

    with pytest.raises(RetrievalError):
        async for _item in execute_answer_core_stream(
            AnswerCoreInput(query="IEA500I"),
            _deps(_settings(), CoreFakeLLM(), retrieve),
        ):
            pass


@pytest.mark.anyio
async def test_core_llm_failure_raises_typed_error():
    def retrieve(*_a, **_k):
        return [_hit()], "identifier", {}

    with pytest.raises(LLMChatError):
        await execute_answer_core(
            AnswerCoreInput(query="IEA500I"),
            _deps(_settings(), CoreFakeLLM(raise_exc=RuntimeError("boom")), retrieve),
        )


@pytest.mark.anyio
async def test_core_stream_empty_hits_yields_only_final():
    def retrieve(*_a, **_k):
        return [], "nl", {"embed_ms": 1, "qdrant_ms": 1}

    items = [
        item
        async for item in execute_answer_core_stream(
            AnswerCoreInput(query="random obscure thing"),
            _deps(_settings(), CoreFakeLLM(), retrieve),
        )
    ]
    assert [item["type"] for item in items] == ["final"]
    output = items[0]["output"]
    assert output.answer == "No supporting manual excerpts were found for this question."
    assert output.citations == []
    assert output.hits == []


@pytest.mark.anyio
async def test_resolve_search_query_condenses_only_when_enabled():
    messages = [
        ChatMessage(role="user", content="What causes IEA500I?"),
        ChatMessage(role="assistant", content="It is an IOS command rejection."),
        ChatMessage(role="user", content="How do I resolve this?"),
    ]

    class CondensingLLM(CoreFakeLLM):
        def chat(self, messages, reasoning_effort=None, temperature=None):
            self.calls.append({"messages": messages, "reasoning_effort": reasoning_effort})
            return ChatResult(
                content="IEA500I recovery procedure", finish_reason="stop", usage=TokenUsage()
            )

    off_input = AnswerCoreInput(query="How do I resolve this?", messages=messages, is_chat=True)
    llm_off = CondensingLLM()
    assert (
        await resolve_search_query(off_input, _deps(_settings(), llm_off, None))
        == "How do I resolve this?"
    )
    assert llm_off.calls == []

    on_input = AnswerCoreInput(query="How do I resolve this?", messages=messages, is_chat=True)
    llm_on = CondensingLLM()
    assert (
        await resolve_search_query(
            on_input, _deps(_settings(chat_condense_enabled=True), llm_on, None)
        )
        == "IEA500I recovery procedure"
    )
    assert len(llm_on.calls) == 1
