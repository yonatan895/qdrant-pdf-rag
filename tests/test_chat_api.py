"""Unit tests for /v1/chat/completions OpenAI-compatible endpoint.

Hermetic tests: Qdrant, embedder, and LLM are faked.
Tests cover single-turn, multi-turn condensation, condensation bypass,
streaming SSE formatting, input validation guardrails, and context pruning.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import build_chat_messages, condense_query
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.ports import ChatMessage, ChatResult, TokenUsage
from mainframe_rag.retrieve.query import SearchHit


def _hit(
    cite_suffix: str = "p. 1-6",
    text: str = "IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy",
) -> SearchHit:
    return SearchHit(
        chunk_id="abc123",
        score=0.42,
        cite=f"SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, {cite_suffix}",
        heading="Chapter 2 > IEA500I",
        text=text,
        doc_id="SA22-0000-00",
        title="Synthetic Reference",
        page_label="1-6",
        chunk_type="message",
        product="z/OS",
        version="9.9",
        message_ids=("IEA500I",),
    )


class MockSearch:
    def __init__(self):
        self.calls = []

    def search(
        self,
        qdrant,
        embedder,
        collection,
        query,
        product=None,
        version=None,
        limit=8,
        *args,
        **kwargs,
    ):
        self.calls.append({"query": query, "product": product, "version": version})
        return [_hit()], "identifier", {"embed_ms": 1, "qdrant_ms": 2}


class ChatFakeLLM:
    def __init__(self):
        self.chat_calls = []
        self.stream_calls = []

    def chat(self, messages, reasoning_effort=None, temperature=None):
        self.chat_calls.append(
            {
                "messages": messages,
                "reasoning_effort": reasoning_effort,
                "temperature": temperature,
            }
        )
        # If this is a condense query prompt
        if "rephrase the follow-up" in messages[0].content:
            return ChatResult(
                content="Standalone search query: IEA500I recovery procedure",
                finish_reason="stop",
                usage=TokenUsage(prompt_tokens=20, completion_tokens=8, total_tokens=28),
            )
        return ChatResult(
            content=(
                "Follow the recovery procedure by reissuing the command.\n\n"
                "Citations:\n"
                "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            ),
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=30, total_tokens=130),
            ttft_ms=45,
        )

    async def chat_stream(
        self, messages, reasoning_effort=None, temperature=None
    ) -> AsyncIterator[dict]:
        self.stream_calls.append(
            {
                "messages": messages,
                "reasoning_effort": reasoning_effort,
                "temperature": temperature,
            }
        )
        yield {
            "type": "token",
            "delta": "Follow the recovery procedure.\n\n",
            "ttft_ms": 30,
        }
        yield {
            "type": "token",
            "delta": "Citations:\n- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n",
        }
        yield {
            "type": "done",
            "finish_reason": "stop",
            "usage": TokenUsage(prompt_tokens=100, completion_tokens=25, total_tokens=125),
            "ttft_ms": 30,
        }


@pytest.fixture
def chat_client(monkeypatch, synthetic_pdf):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")

    mock_search = MockSearch()
    monkeypatch.setattr(app_mod, "retrieve_search", mock_search.search)

    with TestClient(app_mod.app) as c:
        fake_llm = ChatFakeLLM()
        monkeypatch.setattr(app_mod, "llm", fake_llm)
        monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
        c.mock_search = mock_search  # type: ignore[attr-defined]
        c.fake_llm = fake_llm  # type: ignore[attr-defined]
        yield c


def test_chat_completions_single_turn_json(chat_client):
    res = chat_client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "What is IEA500I?"}],
            "stream": False,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "test-reasoning-model"
    assert len(data["choices"]) == 1
    choice = data["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert "Follow the recovery procedure" in choice["message"]["content"]
    assert "Citations:" in choice["message"]["content"]
    assert len(data["citations"]) == 1
    assert "IEA500I" in data["citations"][0]
    assert len(data["hits"]) == 1


def test_chat_completions_multi_turn_with_condensation(chat_client):
    # Second turn has no identifier ("How do I resolve this?") -> triggers condense_query
    messages = [
        {"role": "user", "content": "What causes IEA500I?"},
        {"role": "assistant", "content": "IEA500I occurs during IOS initialization."},
        {"role": "user", "content": "How do I resolve this issue?"},
    ]
    res = chat_client.post(
        "/v1/chat/completions",
        json={"messages": messages, "stream": False},
    )
    assert res.status_code == 200
    # Search should have been invoked with the condensed query returned by FakeLLM
    search_calls = chat_client.mock_search.calls
    assert len(search_calls) == 1
    assert search_calls[0]["query"] == "IEA500I recovery procedure"


def test_chat_completions_multi_turn_bypass_condensation(chat_client):
    # Second turn contains explicit identifier "IEE400I" -> skips condensation LLM call
    messages = [
        {"role": "user", "content": "What causes IEA500I?"},
        {"role": "assistant", "content": "IEA500I occurs during IOS initialization."},
        {"role": "user", "content": "What about message IEE400I?"},
    ]
    res = chat_client.post(
        "/v1/chat/completions",
        json={"messages": messages, "stream": False},
    )
    assert res.status_code == 200
    search_calls = chat_client.mock_search.calls
    assert len(search_calls) == 1
    assert search_calls[0]["query"] == "What about message IEE400I?"


def test_chat_completions_streaming_sse(chat_client):
    messages = [{"role": "user", "content": "What is IEA500I?"}]
    res = chat_client.post(
        "/v1/chat/completions",
        json={"messages": messages, "stream": True},
    )
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]

    lines = [line for line in res.text.split("\n") if line.strip()]
    assert any("[DONE]" in line for line in lines)

    data_lines = [line[len("data: ") :] for line in lines if line.startswith("data: ") and line != "data: [DONE]"]
    assert len(data_lines) >= 2

    chunks = [json.loads(line) for line in data_lines]
    # Check that chunks have object chat.completion.chunk
    for ch in chunks:
        assert ch["object"] == "chat.completion.chunk"
        assert ch["id"].startswith("chatcmpl-")

    # Check that at least one chunk has delta content
    contents = [ch["choices"][0]["delta"].get("content", "") for ch in chunks]
    full_stream_text = "".join(contents)
    assert "Follow the recovery procedure" in full_stream_text

    # Final chunk should have citations metadata
    final_chunk = chunks[-1]
    assert "citations" in final_chunk["choices"][0]
    assert len(final_chunk["choices"][0]["citations"]) == 1


def test_chat_completions_validation_errors(chat_client):
    # Empty messages list
    r1 = chat_client.post("/v1/chat/completions", json={"messages": []})
    assert r1.status_code == 422

    # Messages with no user role
    r2 = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "assistant", "content": "Hello"}]},
    )
    assert r2.status_code == 422

    # Query over limit
    huge_text = "A" * 5000
    r3 = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": huge_text}]},
    )
    assert r3.status_code == 422
    assert r3.json()["code"] == "invalid_request"


def test_build_chat_messages_pruning():
    past_messages = [
        ChatMessage(role="user", content="Explain IEA500I"),
        ChatMessage(
            role="assistant",
            content="Here is the explanation.\n\nRetrieved manual excerpts:\n[1] Chunk text\n[2] More text",
        ),
        ChatMessage(role="user", content="How do I resolve it?"),
    ]
    hits = [_hit()]
    assembled = build_chat_messages(past_messages, hits)

    assert len(assembled) == 4  # system, user (prior), assistant (prior), user (active)
    assert assembled[0].role == "system"
    assert assembled[1].role == "user"
    assert assembled[1].content == "Explain IEA500I"
    assert assembled[2].role == "assistant"
    # Verify retrieved manual excerpts were pruned from historical assistant message
    assert "Retrieved manual excerpts:" not in assembled[2].content
    assert assembled[2].content == "Here is the explanation."
    # Active user message contains the fresh excerpts
    assert assembled[3].role == "user"
    assert "Retrieved manual excerpts:" in assembled[3].content
    assert "How do I resolve it?" in assembled[3].content


@pytest.mark.anyio
async def test_condense_query_direct_bypass():
    class NoOpLLM:
        def chat(self, *args, **kwargs):
            raise AssertionError("LLM should not be called when query has identifier")

    msg = [
        ChatMessage(role="user", content="Hello"),
        ChatMessage(role="assistant", content="Hi"),
        ChatMessage(role="user", content="What does message DFS058I mean?"),
    ]
    res = await condense_query(NoOpLLM(), msg)  # type: ignore[arg-type]
    assert res == "What does message DFS058I mean?"
