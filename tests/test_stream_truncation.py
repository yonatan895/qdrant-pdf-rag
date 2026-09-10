"""Truncated SSE streams must never surface as complete answers (review finding
on the mock abort shape).

A connection that dies mid-stream (first chunk, clean close, no [DONE])
currently exits the SSE loop normally: content is non-empty so the
empty-recovery never fires, and the partial answer ships labeled
finish_reason "stop" — silent truncation, with no error event and no
answer_alert. Contract (AGENTS.md SSE section): a mid-stream failure emits
event: error and ends WITHOUT final.

Hermetic: fake transports / fake LLMs, no network, no Qdrant.
"""

import json

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import HttpxLLMClient, TruncatedStreamError
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage
from tests.fakes import HttpxStreamFake, settings_kw

TRUNCATED_LINES = [
    'data: {"choices": [{"delta": {"role": "assistant", "content": "Partial "}}]}',
    'data: {"choices": [{"delta": {"content": "answer"}}]}',
    # No [DONE]: the connection died here.
]

_COMPLETE_PAYLOAD = {
    "choices": [{"message": {"content": "Complete answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def _settings_kwargs(**overrides):
    return settings_kw(llm_base_url="http://llm.internal/v1", **overrides)


@pytest.mark.anyio
async def test_chat_stream_truncated_raises_after_tokens():
    """chat_stream yields what arrived, then raises: tokens already went to
    the client, so recovery (which would duplicate them) is impossible and
    the app must take its event: error path."""
    client = HttpxLLMClient(
        Settings(**_settings_kwargs()), client=HttpxStreamFake(lines=TRUNCATED_LINES)
    )
    items = []
    with pytest.raises(TruncatedStreamError):
        async for item in client.chat_stream([ChatMessage(role="user", content="hi")]):
            items.append(item)
    assert [i["type"] for i in items] == ["token", "token"]
    assert "".join(i["delta"] for i in items) == "Partial answer"


@pytest.mark.anyio
async def test_achat_truncated_stream_falls_back_to_complete_post():
    """achat has yielded nothing: a truncated stream re-asks via the
    non-streaming POST, so the caller gets a COMPLETE answer — never the
    partial prefix labeled stop."""
    fake = HttpxStreamFake(lines=TRUNCATED_LINES, payload=_COMPLETE_PAYLOAD)
    settings = Settings(**_settings_kwargs(llm_stream=True))
    llm = HttpxLLMClient(settings, client=fake)
    result = await llm.achat([ChatMessage(role="user", content="hi")])
    assert len(fake.post_bodies) == 1
    assert result.content == "Complete answer"
    assert result.finish_reason == "stop"


def test_chat_sync_truncated_stream_falls_back_to_complete_post():
    """Sync mirror of the achat case: truncation recovers through POST."""
    from contextlib import contextmanager

    from tests.fakes import PostResp, StreamResp

    class FakeClient:
        """Stays local: the sync leg needs a sync .stream, while the shared
        HttpxStreamFake.stream is async-only (async client legs)."""

        def __init__(self):
            self.posts = 0

        @contextmanager
        def stream(self, method, url, json=None):
            yield StreamResp(TRUNCATED_LINES)

        def post(self, url, json=None):
            self.posts += 1
            return PostResp(_COMPLETE_PAYLOAD)

    fake = FakeClient()
    settings = Settings(**_settings_kwargs(llm_stream=True))
    llm = HttpxLLMClient(settings, client=fake)
    result = llm.chat([ChatMessage(role="user", content="hi")])
    assert fake.posts == 1
    assert result.content == "Complete answer"
    assert result.finish_reason == "stop"


@pytest.mark.anyio
async def test_chat_stream_truncated_empty_still_recovers_via_post():
    """Boundary: truncation before any byte is indistinguishable from the
    empty-content defect — recovery still fires (nothing to duplicate)."""
    fake = HttpxStreamFake(
        lines=[],
        payload={
            "choices": [{"message": {"content": "Recovered answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = [item async for item in llm.chat_stream([ChatMessage(role="user", content="hi")])]
    assert len(fake.post_bodies) == 1
    assert items[0]["type"] == "token" and items[0]["delta"] == "Recovered answer"
    assert items[-1]["type"] == "done" and items[-1]["finish_reason"] == "stop"


@pytest.mark.anyio
async def test_chat_stream_length_finish_with_done_is_not_truncation():
    """A length-limited stream terminates properly ([DONE] + finish_reason):
    it must NOT raise — length handling downstream is unchanged."""
    fake = HttpxStreamFake(
        lines=[
            'data: {"choices": [{"delta": {"content": "Cut "}}]}',
            'data: {"choices": [{"delta": {"content": "off"}, "finish_reason": "length"}]}',
            "data: [DONE]",
        ]
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = [item async for item in llm.chat_stream([ChatMessage(role="user", content="hi")])]
    assert fake.post_bodies == []  # no recovery POST on a terminated stream
    assert items[-1]["type"] == "done" and items[-1]["finish_reason"] == "length"


def test_truncation_error_carries_counts_not_content():
    """Log contract: exception text reaches logs, so it must never carry
    response text — counts only."""
    err = TruncatedStreamError(7)
    assert "7" in str(err)
    assert "Partial" not in str(err)


def _client(monkeypatch, synthetic_pdf, llm):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    with TestClient(app_mod.app) as c:
        monkeypatch.setattr(app_mod, "llm", llm)
        monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
        yield c


@pytest.fixture
def trunc_client(monkeypatch, synthetic_pdf):
    from mainframe_rag.retrieve.query import SearchHit

    hit = SearchHit(
        chunk_id="abc123",
        score=0.42,
        cite="SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6",
        heading="Chapter 2 > IEA500I",
        text="IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy",
        doc_id="SA22-0000-00",
        title="Synthetic Reference",
        page_label="1-6",
        chunk_type="message",
        product="z/OS",
        version="9.9",
        message_ids=("IEA500I",),
    )

    class TruncSearch:
        def search(self, *a, **kw):
            return [hit], "identifier", {"embed_ms": 1, "qdrant_ms": 2}

    class TruncLLM:
        async def chat_stream(self, messages, *args, **kwargs):
            yield {"type": "token", "delta": "Partial ", "token": "Partial ", "ttft_ms": 12}
            raise TruncatedStreamError(1)

        def chat(self, *a, **kw):
            raise AssertionError("non-stream chat must not run on the stream path")

    monkeypatch.setattr(app_mod, "retrieve_search", TruncSearch().search)
    yield from _client(monkeypatch, synthetic_pdf, TruncLLM())


def test_v1_answer_stream_truncation_emits_error_without_final(trunc_client):
    """Contract pin: token deltas, then event: error, and NO event: final."""
    resp = trunc_client.post("/v1/answer?stream=true", json={"query": "IEA500I command"})
    assert resp.status_code == 200
    events = []
    current_event = "message"
    current_data: list[str] = []
    for line in resp.text.split("\n"):
        line = line.strip()
        if not line:
            if current_data:
                events.append((current_event, json.loads("\n".join(current_data))))
                current_event, current_data = "message", []
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            current_data.append(line[5:].strip())
    if current_data:
        events.append((current_event, json.loads("\n".join(current_data))))
    kinds = [e[0] for e in events]
    assert "token" in kinds
    assert "error" in kinds
    assert "final" not in kinds
    err = next(e[1] for e in events if e[0] == "error")
    assert err["code"] == "upstream_error"
