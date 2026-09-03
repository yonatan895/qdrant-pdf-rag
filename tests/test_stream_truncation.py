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
from mainframe_rag.agent.answer import TruncatedStreamError
from mainframe_rag.agent.tokenizer import FallbackTokenizer

TRUNCATED_LINES = [
    'data: {"choices": [{"delta": {"role": "assistant", "content": "Partial "}}]}',
    'data: {"choices": [{"delta": {"content": "answer"}}]}',
    # No [DONE]: the connection died here.
]


def _settings_kwargs(**overrides):
    base = {
        "llm_base_url": "http://llm.internal/v1",
        "llm_model_reasoning": "test-reasoning-model",
        "_env_file": None,
    }
    base.update(overrides)
    return base


@pytest.mark.anyio
async def test_chat_stream_truncated_raises_after_tokens():
    """chat_stream yields what arrived, then raises: tokens already went to
    the client, so recovery (which would duplicate them) is impossible and
    the app must take its event: error path."""
    from contextlib import asynccontextmanager

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    class TruncatedStreamResp:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in TRUNCATED_LINES:
                yield line

    class FakeAsyncHttpClient:
        @asynccontextmanager
        async def stream(self, method, url, json=None):
            yield TruncatedStreamResp()

    client = HttpxLLMClient(Settings(**_settings_kwargs()), client=FakeAsyncHttpClient())
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
    from contextlib import asynccontextmanager

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    class TruncatedStreamResp:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in TRUNCATED_LINES:
                yield line

    class PostResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "Complete answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }

    class FakeClient:
        def __init__(self):
            self.posts = 0

        @asynccontextmanager
        async def stream(self, method, url, json=None):
            yield TruncatedStreamResp()

        async def post(self, url, json=None):
            self.posts += 1
            return PostResp()

    fake = FakeClient()
    settings = Settings(**_settings_kwargs(llm_stream=True))
    llm = HttpxLLMClient(settings, client=fake)
    result = await llm.achat([ChatMessage(role="user", content="hi")])
    assert fake.posts == 1
    assert result.content == "Complete answer"
    assert result.finish_reason == "stop"


def test_chat_sync_truncated_stream_falls_back_to_complete_post():
    """Sync mirror of the achat case: truncation recovers through POST."""
    from contextlib import contextmanager

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    class TruncatedStreamResp:
        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(TRUNCATED_LINES)

    class PostResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "Complete answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }

    class FakeClient:
        def __init__(self):
            self.posts = 0

        @contextmanager
        def stream(self, method, url, json=None):
            yield TruncatedStreamResp()

        def post(self, url, json=None):
            self.posts += 1
            return PostResp()

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
    from contextlib import asynccontextmanager

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    class DeadStreamResp:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            return
            yield  # pragma: no cover - empty async generator

    class PostResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "Recovered answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }

    class FakeClient:
        def __init__(self):
            self.posts = 0

        @asynccontextmanager
        async def stream(self, method, url, json=None):
            yield DeadStreamResp()

        async def post(self, url, json=None):
            self.posts += 1
            return PostResp()

    fake = FakeClient()
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = [item async for item in llm.chat_stream([ChatMessage(role="user", content="hi")])]
    assert fake.posts == 1
    assert items[0]["type"] == "token" and items[0]["delta"] == "Recovered answer"
    assert items[-1]["type"] == "done" and items[-1]["finish_reason"] == "stop"


@pytest.mark.anyio
async def test_chat_stream_length_finish_with_done_is_not_truncation():
    """A length-limited stream terminates properly ([DONE] + finish_reason):
    it must NOT raise — length handling downstream is unchanged."""
    from contextlib import asynccontextmanager

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    class LengthStreamResp:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices": [{"delta": {"content": "Cut "}}]}'
            yield 'data: {"choices": [{"delta": {"content": "off"}, "finish_reason": "length"}]}'
            yield "data: [DONE]"

    class FakeAsyncHttpClient:
        @asynccontextmanager
        async def stream(self, method, url, json=None):
            yield LengthStreamResp()

        async def post(self, url, json=None):
            raise AssertionError("no recovery POST on a terminated stream")

    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=FakeAsyncHttpClient())
    items = [item async for item in llm.chat_stream([ChatMessage(role="user", content="hi")])]
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
