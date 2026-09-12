"""Unit tests for /v1/chat/completions OpenAI-compatible endpoint.

Hermetic tests: Qdrant, embedder, and LLM are faked.
Tests cover single-turn, multi-turn condensation, condensation bypass (message codes & abends),
streaming SSE formatting, empty-hits short circuit, config fail-fast, input validation,
and context pruning.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import _ABEND_RE, build_chat_messages, condense_query
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
    def __init__(self, return_hits: bool = True):
        self.calls = []
        self.return_hits = return_hits

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
        hits = [_hit()] if self.return_hits else []
        return hits, "identifier", {"embed_ms": 1, "qdrant_ms": 2}


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


def test_chat_completions_multi_turn_with_condensation(chat_client, monkeypatch):
    # Second turn has no identifier ("How do I resolve this?") -> triggers
    # condense_query only when the gated setting is on (ADR-0004: default off).
    monkeypatch.setattr(app_mod.settings, "chat_condense_enabled", True)
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


def test_chat_completions_abend_code_bypass(chat_client):
    # Abend codes (e.g. S0C4, U4038) bypass query condensation via _ABEND_RE
    messages = [
        {"role": "user", "content": "System had a crash."},
        {"role": "assistant", "content": "What error occurred?"},
        {"role": "user", "content": "We received abend S0C4 in module XYZ"},
    ]
    res = chat_client.post(
        "/v1/chat/completions",
        json={"messages": messages, "stream": False},
    )
    assert res.status_code == 200
    search_calls = chat_client.mock_search.calls
    assert len(search_calls) == 1
    assert search_calls[0]["query"] == "We received abend S0C4 in module XYZ"


def test_chat_completions_condensation_default_off(chat_client):
    # ADR-0004: the condensation rewrite ships gated off; a follow-up without
    # identifiers searches its literal text and never rewrites.
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
    search_calls = chat_client.mock_search.calls
    assert len(search_calls) == 1
    assert search_calls[0]["query"] == "How do I resolve this issue?"


def test_chat_native_route_matches_openai_alias(chat_client):
    # Native POST /v1/chat and the OpenAI alias share one handler and shape.
    payload = {"messages": [{"role": "user", "content": "What is IEA500I?"}]}
    native = chat_client.post("/v1/chat", json=payload)
    alias = chat_client.post("/v1/chat/completions", json=payload)
    assert native.status_code == 200
    assert alias.status_code == 200
    assert native.json()["object"] == alias.json()["object"] == "chat.completion"
    assert native.json()["choices"][0]["message"] == alias.json()["choices"][0]["message"]
    assert native.json()["citations"] == alias.json()["citations"]
    assert native.json()["hits"] == alias.json()["hits"]


class ExplodingChatLLM:
    """Streams one token, then fails: the stream must terminate with the
    OpenAI error object + [DONE], never a fake finish_reason="error" chunk."""

    async def chat_stream(self, messages, reasoning_effort=None, temperature=None):
        yield {"type": "token", "delta": "partial answer", "ttft_ms": 5}
        raise RuntimeError("stream exploded")


def test_chat_completions_stream_error_is_openai_error(chat_client, monkeypatch):
    monkeypatch.setattr(app_mod, "llm", ExplodingChatLLM())
    res = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "What is IEA500I?"}], "stream": True},
    )
    assert res.status_code == 200
    assert "partial answer" in res.text
    assert '"error"' in res.text
    assert "upstream_error" in res.text
    assert "[DONE]" in res.text
    assert '"finish_reason": "error"' not in res.text


def test_chat_completions_traceparent_header_propagation(chat_client):
    headers = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
    res = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "What is IEA500I?"}]},
        headers=headers,
    )
    assert res.status_code == 200


def test_chat_completions_empty_hits_short_circuit(chat_client, monkeypatch):
    # When retrieval yields 0 hits, endpoint short-circuits without calling LLM
    empty_search = MockSearch(return_hits=False)
    monkeypatch.setattr(app_mod, "retrieve_search", empty_search.search)

    # 1. Non-streaming
    res = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "What is unknown ABC999I?"}], "stream": False},
    )
    assert res.status_code == 200
    data = res.json()
    assert "No manual excerpts carry ABC999I." in data["choices"][0]["message"]["content"]
    assert data["citations"] == []
    assert data["hits"] == []
    # LLM should never have been called
    assert chat_client.fake_llm.chat_calls == []

    # 2. Streaming
    res_stream = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "What is unknown ABC999I?"}], "stream": True},
    )
    assert res_stream.status_code == 200
    assert "text/event-stream" in res_stream.headers["content-type"]
    assert "No manual excerpts carry ABC999I." in res_stream.text
    assert "[DONE]" in res_stream.text


def test_chat_completions_not_configured_fail_fast(chat_client, monkeypatch):
    monkeypatch.setattr(app_mod.settings, "llm_model_reasoning", None)
    res = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "What is IEA500I?"}]},
    )
    assert res.status_code == 503
    assert res.json()["code"] == "not_configured"


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


def test_build_chat_messages_pruning_and_cap():
    huge_prev_answer = "Here is the explanation.\n" + ("x" * 2000) + "\nRetrieved manual excerpts:\n[1] Chunk\n[2] Chunk2"
    past_messages = [
        ChatMessage(role="user", content="Explain IEA500I"),
        ChatMessage(
            role="assistant",
            content=huge_prev_answer,
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
    # Verify retrieved manual excerpts were pruned
    assert "Retrieved manual excerpts:" not in assembled[2].content
    # Verify history was capped
    assert "[history truncated]" in assembled[2].content
    assert len(assembled[2].content) <= 1100

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

    # Abend code bypass
    msg_abend = [
        ChatMessage(role="user", content="Hello"),
        ChatMessage(role="assistant", content="Hi"),
        ChatMessage(role="user", content="We encountered abend S0C4 in step 1"),
    ]
    res_abend = await condense_query(NoOpLLM(), msg_abend)  # type: ignore[arg-type]
    assert res_abend == "We encountered abend S0C4 in step 1"


def test_abend_regex():
    assert _ABEND_RE.search("ABEND S0C4")
    assert _ABEND_RE.search("s0c4")
    assert _ABEND_RE.search("abend U4038")
    assert _ABEND_RE.search("System abend S013 occurred")
    assert not _ABEND_RE.search("How do I fix this error?")
