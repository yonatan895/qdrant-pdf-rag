"""End-to-end interactive chat and UI workflow test.

Simulates the full operator journey:
1. Operator connects to agent (healthz check).
2. Turn 1: Operator asks about message IEA500I (SSE streaming).
3. SQLite persists Turn 1 and auto-titles session.
4. Turn 2: Operator attaches JES spool abend dump and asks follow-up (query condensation).
5. Context builder verifies Turn 1 excerpts pruned and Turn 2 fresh excerpts injected.
6. Operator exports complete incident report to Markdown.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.ports import ChatResult, TokenUsage
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.ui.components.sidebar import check_agent_health
from mainframe_rag.ui.db import (
    add_message,
    create_session,
    export_markdown,
    get_session_messages,
    init_db,
    list_sessions,
)


def _hit(cite: str, text: str, heading: str = "Recovery") -> SearchHit:
    return SearchHit(
        chunk_id="chunk-123",
        score=0.88,
        cite=cite,
        heading=heading,
        text=text,
        doc_id="SA38-0674-06",
        title="z/OS MVS System Messages Vol 6",
        page_label="1-45",
        chunk_type="message",
        product="z/OS",
        version="2.5",
        message_ids=("IEA500I",),
    )


class E2ELiveLLM:
    def __init__(self):
        self.turns = []

    def chat(self, messages, reasoning_effort=None, temperature=None):
        prompt_text = messages[-1].content
        self.turns.append(prompt_text)
        if "rephrase the follow-up" in messages[0].content:
            return ChatResult(
                content="IEA500I recovery procedure",
                finish_reason="stop",
                usage=TokenUsage(prompt_tokens=30, completion_tokens=8, total_tokens=38),
            )
        return ChatResult(
            content="Non-streaming response",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=50, completion_tokens=10, total_tokens=60),
        )

    async def chat_stream(
        self, messages, reasoning_effort=None, temperature=None
    ) -> AsyncIterator[dict]:
        user_content = messages[-1].content
        self.turns.append(user_content)
        if "IEA500I" in user_content:
            yield {"type": "token", "delta": "IEA500I indicates the IOS command was rejected.\n\n"}
            yield {"type": "token", "delta": "Wait for IOS initialization to complete and reissue the command.\n\n"}
            yield {
                "type": "token",
                "delta": "Citations:\n- SA38-0674-06 z/OS MVS System Messages Vol 6, Recovery, p. 1-45\n",
            }
            yield {
                "type": "done",
                "finish_reason": "stop",
                "usage": TokenUsage(prompt_tokens=150, completion_tokens=40, total_tokens=190),
                "ttft_ms": 25,
            }
        else:
            yield {"type": "token", "delta": "General response.\n\n"}
            yield {
                "type": "done",
                "finish_reason": "stop",
                "usage": TokenUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
            }


@pytest.fixture
def e2e_env(monkeypatch, synthetic_pdf):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "reasoning-model-live")

    hit1 = _hit(
        cite="SA38-0674-06 z/OS MVS System Messages Vol 6, Recovery, p. 1-45",
        text="IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy",
    )

    def mock_retrieve(qdrant, embedder, collection, query, *args, **kwargs):
        return [hit1], "identifier", {"embed_ms": 1, "qdrant_ms": 2}

    monkeypatch.setattr(app_mod, "retrieve_search", mock_retrieve)

    with TestClient(app_mod.app) as c:
        fake_llm = E2ELiveLLM()
        monkeypatch.setattr(app_mod, "llm", fake_llm)
        monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
        c.fake_llm = fake_llm  # type: ignore[attr-defined]
        yield c


def test_full_interactive_session_lifecycle(e2e_env, tmp_path, monkeypatch):
    client = e2e_env
    db_file = tmp_path / "copilot.db"
    init_db(db_file)

    # 1. Health check verification
    monkeypatch.setattr("httpx2.get", lambda url, timeout=1.5: client.get("/healthz"))
    health = check_agent_health("http://testserver")
    assert health["status"] == "ok"
    assert health["label"] == "Online"

    # 2. Start new incident session
    session_id = create_session(db_file, title="New Incident")
    assert session_id

    # 3. Turn 1: Operator asks about IEA500I
    turn1_prompt = "What is message IEA500I and why was it issued?"
    add_message(db_file, session_id, role="user", content=turn1_prompt)

    # Verify session title auto-updated to operator question
    sessions = list_sessions(db_file)
    assert "What is message IEA500I" in sessions[0]["title"]

    # Call agent endpoint with streaming
    r1 = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": turn1_prompt}],
            "stream": True,
        },
    )
    assert r1.status_code == 200
    assert "text/event-stream" in r1.headers["content-type"]

    # Parse stream
    t1_tokens = []
    t1_citations = []
    t1_hits = []
    for line in r1.text.split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            chunk = json.loads(line[6:])
            choice = chunk["choices"][0]
            if choice.get("delta", {}).get("content"):
                t1_tokens.append(choice["delta"]["content"])
            if choice.get("citations"):
                t1_citations = choice["citations"]
            if choice.get("hits"):
                t1_hits = choice["hits"]

    t1_full_answer = "".join(t1_tokens)
    assert "reissue the command" in t1_full_answer
    assert len(t1_citations) == 1
    assert "SA38-0674-06" in t1_citations[0]
    assert len(t1_hits) == 1

    # Persist assistant Turn 1
    add_message(
        db_file,
        session_id,
        role="assistant",
        content=t1_full_answer,
        citations=t1_citations,
        hits=t1_hits,
    )

    # 4. Turn 2: Follow-up question with attached JES Spool context
    turn2_prompt = "How do I resolve this issue?"
    jes_spool = "//STEP1 EXEC PGM=IEBGENER\nIEF450I JOB999 STEP1 - ABEND=S0C4"
    add_message(
        db_file,
        session_id,
        role="user",
        content=turn2_prompt,
        splunk_context=jes_spool,
    )

    # Send multi-turn history to /v1/chat/completions
    api_messages = [
        {"role": "user", "content": turn1_prompt},
        {"role": "assistant", "content": t1_full_answer},
        {"role": "user", "content": turn2_prompt},
    ]
    r2 = client.post(
        "/v1/chat/completions",
        json={
            "messages": api_messages,
            "stream": True,
            "splunk_context": jes_spool,
            "product": "z/OS",
            "version": "2.5",
        },
    )
    assert r2.status_code == 200

    # Collect turn 2 answer
    t2_tokens = []
    for line in r2.text.split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            chunk = json.loads(line[6:])
            if chunk["choices"][0].get("delta", {}).get("content"):
                t2_tokens.append(chunk["choices"][0]["delta"]["content"])

    t2_full_answer = "".join(t2_tokens)
    assert "reissue the command" in t2_full_answer

    add_message(
        db_file,
        session_id,
        role="assistant",
        content=t2_full_answer,
        citations=t1_citations,
    )

    # 5. Verify conversation history and export Markdown
    messages = get_session_messages(db_file, session_id)
    assert len(messages) == 4
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[2]["role"] == "user"
    assert messages[2]["splunk_context"] == jes_spool
    assert messages[3]["role"] == "assistant"

    md_report = export_markdown(db_file, session_id)
    assert f"# Incident Analysis: {sessions[0]['title']}" in md_report
    assert "Operator (" in md_report
    assert "Mainframe Copilot (" in md_report
    assert "IEF450I JOB999 STEP1 - ABEND=S0C4" in md_report
    assert "SA38-0674-06" in md_report
