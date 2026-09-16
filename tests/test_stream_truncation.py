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

import asyncio
import json
import logging

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
        def stream(self, method, url, json=None, headers=None):
            yield StreamResp(TRUNCATED_LINES)

        def post(self, url, json=None, headers=None):
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


def _search_stub():
    """One synthetic hit through the patched retrieval leg: tests drive the
    stream machinery, not retrieval."""
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

    class StubSearch:
        def search(self, *a, **kw):
            return [hit], "identifier", {"embed_ms": 1, "qdrant_ms": 2}

    return StubSearch()


class HangingLLM:
    """Streams one token then never finishes, so a test can close the
    response generator the way a client disconnect does."""

    async def chat_stream(self, messages, *args, **kwargs):
        yield {"type": "token", "delta": "Partial ", "token": "Partial ", "ttft_ms": 5}
        await asyncio.Event().wait()

    def chat(self, *a, **kw):
        raise AssertionError("non-stream chat must not run on the stream path")


@pytest.fixture
def trunc_client(monkeypatch, synthetic_pdf):
    class TruncLLM:
        async def chat_stream(self, messages, *args, **kwargs):
            yield {"type": "token", "delta": "Partial ", "token": "Partial ", "ttft_ms": 12}
            raise TruncatedStreamError(1)

        def chat(self, *a, **kw):
            raise AssertionError("non-stream chat must not run on the stream path")

    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    yield from _client(monkeypatch, synthetic_pdf, TruncLLM())


@pytest.fixture
def hang_client(monkeypatch, synthetic_pdf):
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    yield from _client(monkeypatch, synthetic_pdf, HangingLLM())


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
    # The error frame carries the machine-readable incomplete state
    # (issue #365): a truncated stream can never read as accepted guidance.
    assert err["verification_state"] == "generation_incomplete"


def test_v1_chat_stream_truncation_error_frame_carries_incomplete_state(trunc_client):
    """Chat mirrors the answer path (issue #365): the strict OpenAI `error`
    object stays intact, a sibling top-level state rides the same frame, and
    no finish chunk ever claims the truncated content completed."""
    resp = trunc_client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "IEA500I command"}], "stream": True},
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text
    frames = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    error = next(f for f in frames if "error" in f)
    assert error["error"] == {"code": "upstream_error", "message": "stream failed"}
    assert error["verification_state"] == "generation_incomplete"
    finished = [
        f
        for f in frames
        if "error" not in f
        and f.get("choices")
        and f["choices"][0].get("finish_reason") is not None
    ]
    assert finished == []


def _scope(path: str) -> dict:
    """Minimal ASGI scope for a direct route call: middleware normally
    stamps request_id/started, so the test does."""
    import time

    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {"request_id": "disconnect-test", "started": time.monotonic()},
    }


@pytest.mark.anyio
async def test_answer_stream_disconnect_records_generation_incomplete(
    hang_client, monkeypatch, caplog
):
    """A client disconnect before any terminal frame cannot receive a frame
    (GeneratorExit at the yield), but is observable server-side (issue #365):
    alert log with the incomplete state plus a client_disconnect outcome."""
    from fastapi import Request, Response

    from mainframe_rag.agent.app import AnswerRequest

    recorded: list[tuple] = []
    monkeypatch.setattr(
        app_mod,
        "record_request",
        lambda endpoint, outcome, **kw: recorded.append((endpoint, outcome)),
    )
    with caplog.at_level(logging.WARNING, logger="agent"):
        response = await app_mod.v1_answer(
            Request(_scope("/v1/answer")),
            AnswerRequest(query="IEA500I command"),
            Response(),
            stream=True,
        )
        body = response.body_iterator
        first = await body.__anext__()
        assert "event: token" in first
        await body.aclose()

    aborts = [r for r in caplog.records if '"client_disconnect"' in r.getMessage()]
    assert len(aborts) == 1
    assert '"verification_state": "generation_incomplete"' in aborts[0].getMessage()
    assert recorded[-1] == ("answer", "client_disconnect")


@pytest.mark.anyio
async def test_chat_stream_disconnect_records_generation_incomplete(
    hang_client, monkeypatch, caplog
):
    """Chat mirror of the answer disconnect (issue #365)."""
    from fastapi import Request, Response

    from mainframe_rag.agent.app import ChatRequest

    recorded: list[tuple] = []
    monkeypatch.setattr(
        app_mod,
        "record_request",
        lambda endpoint, outcome, **kw: recorded.append((endpoint, outcome)),
    )
    with caplog.at_level(logging.WARNING, logger="agent"):
        response = await app_mod.chat_completions(
            ChatRequest(
                messages=[{"role": "user", "content": "IEA500I command"}], stream=True
            ),
            Request(_scope("/v1/chat")),
            Response(),
        )
        body = response.body_iterator
        first = await body.__anext__()
        assert '"delta"' in first
        await body.aclose()

    aborts = [r for r in caplog.records if '"client_disconnect"' in r.getMessage()]
    assert len(aborts) == 1
    assert '"verification_state": "generation_incomplete"' in aborts[0].getMessage()
    assert recorded[-1] == ("chat", "client_disconnect")
