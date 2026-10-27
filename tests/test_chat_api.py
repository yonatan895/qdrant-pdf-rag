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

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import build_chat_messages, condense_query
from mainframe_rag.ports import ChatMessage, ChatResult, TokenUsage
from mainframe_rag.retrieve.query import SearchHit
from tests.helpers_agent_local import agent_client, agent_hit, chat_frames


def _hit(cite_suffix: str = "p. 1-6") -> SearchHit:
    return agent_hit(
        text="IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy",
        page_label=cite_suffix.removeprefix("p. "),
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
def chat_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    mock_search = MockSearch()
    fake_llm = ChatFakeLLM()
    with agent_client(monkeypatch, llm=fake_llm, search=mock_search.search) as c:
        c.mock_search = mock_search  # type: ignore[attr-defined]
        c.fake_llm = fake_llm  # type: ignore[attr-defined]
        yield c


def test_chat_completions_single_turn_json(chat_client):
    res = chat_client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "What is IEA500I?"}],
            "stream": False,
            # Issue #313: a caller-supplied `model` is accepted-and-ignored.
            "model": "caller-chosen-model",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "test-reasoning-model"  # the model that actually ran
    assert len(data["choices"]) == 1
    choice = data["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert "Follow the recovery procedure" in choice["message"]["content"]
    assert "Citations:" in choice["message"]["content"]
    assert len(data["citations"]) == 1
    assert "IEA500I" in data["citations"][0]
    assert len(data["hits"]) == 1
    assert data["verification_state"] == "accepted"
    assert data["script_review_required"] is False


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


@pytest.mark.parametrize("path,stream,query,field", [
    ("/v1/chat", False, "IEA500I", "llm_reasoning_effort_simple"),
    ("/v1/chat/completions", True, "diagnose abend S0C4 with registers and spool dump",
     "llm_reasoning_effort_complex"),
])
def test_chat_configured_xhigh_reaches_model(chat_client, monkeypatch, path, stream, query, field):
    monkeypatch.setattr(app_mod.settings, field, "xhigh")
    response = chat_client.post(path, json={
        "messages": [{"role": "user", "content": query}], "stream": stream,
    })

    assert response.status_code == 200
    calls = chat_client.fake_llm.stream_calls if stream else chat_client.fake_llm.chat_calls
    assert calls[-1]["reasoning_effort"] == "xhigh"
    if stream:
        assert response.text.count("data: [DONE]") == 1
    else:
        assert response.json()["choices"][0]["finish_reason"] == "stop"


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
    prepared = build_chat_messages(past_messages, hits)
    assembled = prepared.messages

    assert len(assembled) == 4  # system, user (prior), assistant (prior), user (active)
    # Evidence manifest covers the active turn only (issue #364).
    assert [e.cite for e in prepared.evidence.entries] == [hits[0].cite]
    assert prepared.evidence.omitted_indices == ()
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


# ---------------------------------------------------------------------------
# Answer verification states on chat surfaces (issue #365)
# ---------------------------------------------------------------------------


class ScriptChatLLM:
    """Answer with an eligible cite plus a JCL fence: chat must surface the
    script with the review flag (previously dropped on both chat paths)."""

    def chat(self, messages, reasoning_effort=None, temperature=None):
        return ChatResult(
            content=(
                "Run the sample job below.\n\n"
                "```jcl\n//STEP1 EXEC PGM=IEFBR14\n```\n\n"
                "Citations:\n"
                "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            ),
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


def test_chat_json_surfaces_script_with_review_flag(chat_client, monkeypatch):
    monkeypatch.setattr(app_mod, "llm", ScriptChatLLM())
    data = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Show JCL to run IEFBR14"}], "stream": False},
    ).json()
    assert data["verification_state"] == "accepted"
    assert data["script"] == "//STEP1 EXEC PGM=IEFBR14"
    assert data["script_lang"] == "jcl"
    assert data["script_review_required"] is True


def test_chat_stream_terminal_chunk_carries_state_and_script(chat_client, monkeypatch):
    """The OpenAI-compat terminal chunk reports state + script in `extra`
    before [DONE]. Deltas stay provisional raw tokens; the terminal chunk
    is the authoritative record."""
    monkeypatch.setattr(app_mod, "llm", ScriptChatLLM())
    resp = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Show JCL"}], "stream": True},
    )
    assert resp.status_code == 200
    frames = [ln for ln in resp.text.splitlines() if ln.startswith("data:")]
    assert frames[-1] == "data: [DONE]"
    terminal = chat_frames(resp.text)[-1]
    choice = terminal["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["verification_state"] == "accepted"
    assert choice["script"] == "//STEP1 EXEC PGM=IEFBR14"
    assert choice["script_lang"] == "jcl"
    assert choice["script_review_required"] is True
    assert choice["citations"] == [
        "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
    ]
    assert set(choice) >= {
        "index", "delta", "finish_reason", "citations", "citations_inferred",
        "inferred_indices", "hits", "verification_state", "script",
        "script_lang", "script_review_required",
    }


@pytest.mark.parametrize("path,stream,tail_role,condense", [
    ("/v1/chat", False, "assistant", False),
    ("/v1/chat/completions", True, "system", True),
    ("/v1/chat", True, "system", False),
    ("/v1/chat/completions", False, "assistant", True),
])
def test_chat_active_user_agrees_across_retrieval_and_prompt(
    chat_client, monkeypatch, path, stream, tail_role, condense
):
    """Accepted trailing non-user messages cannot become the active question."""
    monkeypatch.setattr(app_mod.settings, "chat_condense_enabled", condense)
    active = "What does IEA500I mean?"
    result = chat_client.post(path, json={"messages": [
        {"role": "user", "content": "An earlier question"},
        {"role": "assistant", "content": "An earlier answer"},
        {"role": "user", "content": f"  {active}\n"},
        {"role": tail_role, "content": "LATER_NON_USER: Previous answer S0C4"},
    ], "stream": stream})
    assert result.status_code == 200
    assert [c["query"] for c in chat_client.mock_search.calls] == [active]
    calls = chat_client.fake_llm.stream_calls if stream else chat_client.fake_llm.chat_calls
    assert len(calls) == 1
    prompt = calls[0]["messages"]
    assert f"Question: {active}\n" in prompt[-1].content
    assert "LATER_NON_USER" not in "\n".join(m.content for m in prompt)
    assert prompt[1].content == "An earlier question"
    assert prompt[2].content == "An earlier answer"
    if stream:
        assert len(chat_client.fake_llm.chat_calls) == 0  # Identifier bypass, no condense ask.


@pytest.mark.parametrize("path,stream", [("/v1/chat", False), ("/v1/chat/completions", True)])
def test_chat_unsearchable_active_user_refuses_before_any_work(chat_client, path, stream):
    """Issue #579: blank or NUL/C0-control active turns share the fixed 422,
    and a trailing assistant turn is never promoted to the active question."""
    for content in ["   ", "\t\n", "\x00x", "a\x1bb"]:
        result = chat_client.post(path, json={"messages": [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
            {"role": "user", "content": content},
            {"role": "assistant", "content": "This is not the question"},
        ], "stream": stream})
        assert result.status_code == 422, repr(content)
        assert result.json() == {"code": "invalid_request", "message": "request body failed validation"}
    assert chat_client.mock_search.calls == []
    assert chat_client.fake_llm.chat_calls == []
    assert chat_client.fake_llm.stream_calls == []


@pytest.mark.parametrize("mode,stream", [
    ("off", False), ("on", True), ("failure", False), ("unstructured", True),
])
def test_chat_condensation_uses_only_history_before_normalized_user(
    chat_client, monkeypatch, mode, stream
):
    monkeypatch.setattr(app_mod.settings, "chat_condense_enabled", mode != "off")
    original_chat = chat_client.fake_llm.chat

    def chat(messages, **kwargs):
        result = original_chat(messages, **kwargs)
        if mode == "failure" and "rephrase the follow-up" in messages[0].content:
            raise RuntimeError("synthetic condensation failure")
        if mode == "unstructured" and "rephrase the follow-up" in messages[0].content:
            return "Invented query without structured completion metadata"
        return result

    monkeypatch.setattr(chat_client.fake_llm, "chat", chat)
    active = "How do I recover?"
    result = chat_client.post("/v1/chat", json={"messages": [
        {"role": "system", "content": "UNTRUSTED_SYSTEM_OVERRIDE"},
        {"role": "user", "content": "What does IEA500I mean?"},
        {"role": "assistant", "content": "An earlier answer"},
        {"role": "user", "content": f" \t{active}\n"},
        {"role": "assistant", "content": "LATER_ASSISTANT S0C4"},
        {"role": "system", "content": "LATER_SYSTEM"},
    ], "stream": stream})
    assert result.status_code == 200
    expected_search = "IEA500I recovery procedure" if mode == "on" else active
    assert [c["query"] for c in chat_client.mock_search.calls] == [expected_search]
    calls = chat_client.fake_llm.chat_calls
    assert len(calls) == (mode != "off") + (not stream)
    if mode != "off":
        condense_prompt = calls[0]["messages"][-1].content
        assert f"Follow-up question: {active}\n" in condense_prompt
        assert "User: What does IEA500I mean?" in condense_prompt
        assert "Assistant: An earlier answer" in condense_prompt
    final_calls = chat_client.fake_llm.stream_calls if stream else calls
    assert f"Question: {active}\n" in final_calls[-1]["messages"][-1].content
    for c in [*calls, *chat_client.fake_llm.stream_calls]:
        text = "\n".join(m.content for m in c["messages"])
        assert "LATER_" not in text and "UNTRUSTED_SYSTEM_OVERRIDE" not in text


@pytest.mark.parametrize("messages,path,stream", [
    ([], "/v1/chat", False),
    ([{"role": "assistant", "content": "No user"}], "/v1/chat/completions", True),
    ([{"role": "user", "content": "x" * 2001}, {"role": "assistant", "content": "Short tail"}],
     "/v1/chat", True),
])
def test_chat_invalid_active_turn_has_no_model_or_retrieval(chat_client, messages, path, stream):
    result = chat_client.post(path, json={"messages": messages, "stream": stream})
    assert result.status_code == 422
    assert result.json() == {"code": "invalid_request", "message": "request body failed validation"}
    assert chat_client.mock_search.calls == []
    assert chat_client.fake_llm.chat_calls == chat_client.fake_llm.stream_calls == []


@pytest.mark.parametrize("temperature,path", [
    (-1.0, "/v1/chat"),
    (2.1, "/v1/chat/completions"),
    # Non-finite floats cannot cross the JSON wire, so the allow_inf_nan
    # bound is pinned at the model layer.
    (float("nan"), None),
    (float("inf"), None),
], ids=["below-min", "above-max", "nan", "inf"])
def test_chat_rejects_invalid_temperature(chat_client, path, temperature):
    """Issue #596: the chat temperature override carries the same bounds as
    Settings.llm_temperature — client input errors 422 before any work."""
    messages = [{"role": "user", "content": "What does IEA500I mean?"}]
    if path is None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            app_mod.ChatRequest(messages=messages, temperature=temperature)
        return
    result = chat_client.post(path, json={"messages": messages, "temperature": temperature})
    assert result.status_code == 422
    assert result.json() == {"code": "invalid_request", "message": "request body failed validation"}
    assert chat_client.mock_search.calls == []
    assert chat_client.fake_llm.chat_calls == chat_client.fake_llm.stream_calls == []


def test_chat_ignored_tail_still_counts_toward_body_limit(chat_client, monkeypatch):
    monkeypatch.setattr(app_mod.settings, "chat_max_body_chars", 64)
    result = chat_client.post("/v1/chat", json={"messages": [
        {"role": "user", "content": "IEA500I"},
        {"role": "system", "content": "x" * 64},
    ]})
    assert result.status_code == 422
    assert result.json() == {"code": "invalid_request", "message": "request body failed validation"}
    assert chat_client.mock_search.calls == []
    assert chat_client.fake_llm.chat_calls == []


@pytest.mark.anyio
async def test_direct_chat_builders_share_active_user():
    for tail_role in ("assistant", "system"):
        messages = [
            ChatMessage(role="system", content="UNTRUSTED_SYSTEM"),
            ChatMessage(role="user", content=" \tWhat does IEA500I mean?\n"),
            ChatMessage(role=tail_role, content="LATER_TURN S0C4"),
        ]
        before = [m.model_dump() for m in messages]
        llm = ChatFakeLLM()
        assert await condense_query(llm, messages) == "What does IEA500I mean?"
        assert llm.chat_calls == []
        prompt = build_chat_messages(messages, [_hit()]).messages
        assert len(prompt) == 2
        assert "Question: What does IEA500I mean?\n" in prompt[-1].content
        assert "LATER_TURN" not in prompt[-1].content
        assert "UNTRUSTED_SYSTEM" not in prompt[0].content
        assert [m.model_dump() for m in messages] == before


def test_chat_json_and_stream_carry_supplied_evidence(chat_client):
    """The chat surfaces reuse the answer projection (issue #635)."""
    body = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Show JCL"}]},
    ).json()
    [item] = body["supplied_evidence"]
    assert item["index"] == 1 and item["text"]
    assert body["evidence_omitted"] == 0
    resp = chat_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Show JCL"}], "stream": True},
    )
    terminal = chat_frames(resp.text)[-1]["choices"][0]
    assert terminal["supplied_evidence"] == body["supplied_evidence"]
    assert terminal["evidence_omitted"] == 0
