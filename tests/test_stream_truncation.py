"""Incomplete upstream chat completions must never surface as complete
answers (review finding on the mock abort shape; issue #365).

A connection that dies mid-stream (first chunk, clean close, no [DONE])
currently exits the SSE loop normally: content is non-empty so the
empty-recovery never fires, and the partial answer ships labeled
finish_reason "stop" — silent truncation, with no error event and no
answer_alert. Contract (AGENTS.md SSE section): a mid-stream failure emits
event: error and ends WITHOUT final. The same shared parser rejects an
upstream `error` frame, a malformed frame, and [DONE] without an explicit
finish reason: a completion is successful only with an explicit terminal
finish and no failure frame (issue #365).

Hermetic: fake transports / fake LLMs, no network, no Qdrant.
"""

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import (
    REASON_MALFORMED_FRAME,
    REASON_MISSING_DONE,
    REASON_MISSING_FINISH,
    REASON_UPSTREAM_ERROR,
    HttpxLLMClient,
    TruncatedStreamError,
    _chat_result_from_response,
)
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

_CONTENT_LINE = 'data: {"choices": [{"delta": {"content": "Partial "}}]}'
_FINISH_LINE = 'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}'
_DONE_LINE = "data: [DONE]"
# A fixed sentinel the client must never echo: upstream error text is not a
# client response, a log field, or an exception message.
_ERROR_FRAME = 'data: {"error": {"message": "SECRET-UPSTREAM-TEXT", "code": 500}}'


def _settings_kwargs(**overrides):
    return settings_kw(llm_base_url="http://llm.internal/v1", **overrides)


@pytest.mark.parametrize(
    "lines",
    [TRUNCATED_LINES, [_CONTENT_LINE, _ERROR_FRAME, _DONE_LINE]],
    ids=["missing-done", "upstream-error"],
)
@pytest.mark.anyio
async def test_achat_failed_stream_discards_partial_and_recovers_via_post(lines):
    """Buffered content is discarded; one POST supplies the complete answer."""
    fake = HttpxStreamFake(lines=lines, payload=_COMPLETE_PAYLOAD)
    settings = Settings(**_settings_kwargs(llm_stream=True))
    llm = HttpxLLMClient(settings, client=fake)
    result = await llm.achat([ChatMessage(role="user", content="hi")])
    assert len(fake.post_bodies) == 1
    assert result.content == "Complete answer"
    assert result.finish_reason == "stop"


def test_chat_sync_truncated_stream_falls_back_to_complete_post():
    """Sync mirror of the achat case: truncation recovers through POST."""
    fake = HttpxStreamFake(lines=TRUNCATED_LINES, payload=_COMPLETE_PAYLOAD)
    fake.stream = fake.stream_sync  # Explicitly select the sync transport.
    settings = Settings(**_settings_kwargs(llm_stream=True))
    llm = HttpxLLMClient(settings, client=fake)
    result = llm.chat([ChatMessage(role="user", content="hi")])
    assert len(fake.stream_bodies) == 1
    assert len(fake.post_bodies) == 1
    assert result.content == "Complete answer"
    assert result.finish_reason == "stop"


@pytest.mark.parametrize("finish", ["length", "content_filter"])
@pytest.mark.anyio
async def test_chat_stream_explicit_non_stop_finish_is_classified_not_truncated(finish):
    """An explicitly finished stream terminates properly ([DONE] + non-null
    finish_reason): it must NOT raise — stop/length/content_filter stay
    classified by the downstream verification state, not by this parser."""
    fake = HttpxStreamFake(
        lines=[
            'data: {"choices": [{"delta": {"content": "Cut "}}]}',
            f'data: {{"choices": [{{"delta": {{"content": "off"}}, "finish_reason": "{finish}"}}]}}',
            "data: [DONE]",
        ]
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = [item async for item in llm.chat_stream([ChatMessage(role="user", content="hi")])]
    assert fake.post_bodies == []  # no recovery POST on a terminated stream
    assert items[-1]["type"] == "done" and items[-1]["finish_reason"] == finish


@pytest.mark.anyio
async def test_chat_stream_error_frame_text_never_reaches_logs(caplog):
    """Client errors and logs carry fixed messages only (issue #365): the
    upstream error body must not leak through the recovery log line."""
    fake = HttpxStreamFake(lines=[_ERROR_FRAME, _DONE_LINE], payload=_COMPLETE_PAYLOAD)
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    with caplog.at_level(logging.WARNING, logger="mainframe_rag.agent.answer"):
        items = [item async for item in llm.chat_stream([ChatMessage(role="user", content="hi")])]
    assert items[-1]["type"] == "done"
    assert not any("SECRET-UPSTREAM-TEXT" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "lines,reason,deltas",
    [
        pytest.param(TRUNCATED_LINES, REASON_MISSING_DONE, ["Partial ", "answer"], id="missing-done"),
        pytest.param(
            [_CONTENT_LINE, _ERROR_FRAME, _DONE_LINE], REASON_UPSTREAM_ERROR,
            ["Partial "], id="upstream-error",
        ),
        pytest.param(
            [_CONTENT_LINE, _DONE_LINE], REASON_MISSING_FINISH,
            ["Partial "], id="missing-finish",
        ),
    ]
    + [
        pytest.param(
            [_CONTENT_LINE, frame, _DONE_LINE], REASON_MALFORMED_FRAME, ["Partial "], id=name,
        )
        for name, frame in [
            ("invalid-json", "data: {not json"),
            ("non-object-payload", 'data: ["not-a-dict"]'),
            ("non-object-choice", 'data: {"choices": ["not-a-dict"]}'),
            ("non-object-delta", 'data: {"choices": [{"delta": "not-a-dict"}]}'),
            ("non-string-content", 'data: {"choices": [{"delta": {"content": 5}}]}'),
            (
                "non-string-finish",
                'data: {"choices": [{"delta": {"content": "bad token"}, "finish_reason": 42}]}',
            ),
        ]
    ],
)
@pytest.mark.anyio
async def test_chat_stream_failure_after_content_raises_without_replay(lines, reason, deltas):
    """After visible content, protocol failures never replay or emit a done item
    (issues #365 / Q420-P2); only fixed reasons escape the parser."""
    fake = HttpxStreamFake(lines=lines, payload=_COMPLETE_PAYLOAD)
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = []
    with pytest.raises(TruncatedStreamError) as excinfo:
        async for item in llm.chat_stream([ChatMessage(role="user", content="hi")]):
            items.append(item)
    assert [i["type"] for i in items] == ["token"] * len(deltas)
    assert [i["delta"] for i in items] == deltas
    assert excinfo.value.reason == reason
    assert fake.post_bodies == []
    assert "SECRET-UPSTREAM-TEXT" not in str(excinfo.value)


@pytest.mark.anyio
async def test_chat_stream_pre_output_error_frame_failed_fallback_raises():
    """A fallback payload without an explicit finish is itself an incomplete
    completion: no synthesized "stop" may be shipped as done (issue #365)."""
    fake = HttpxStreamFake(
        lines=[_ERROR_FRAME, _DONE_LINE],
        payload={"choices": [{"message": {"content": "fallback body"}}]},
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = []
    with pytest.raises(TruncatedStreamError) as excinfo:
        async for item in llm.chat_stream([ChatMessage(role="user", content="hi")]):
            items.append(item)
    assert len(fake.post_bodies) == 1
    assert excinfo.value.reason == REASON_MISSING_FINISH
    assert all(i["type"] != "done" for i in items)


@pytest.mark.anyio
async def test_achat_missing_finish_on_both_legs_raises_not_synthesized_stop():
    """A missing-finish stream falls back once; a missing-finish fallback
    then fails the request instead of inventing finish_reason "stop"."""
    fake = HttpxStreamFake(
        lines=[_CONTENT_LINE, _DONE_LINE],
        payload={"choices": [{"message": {"content": "fallback body"}}]},
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=fake)
    with pytest.raises(TruncatedStreamError) as excinfo:
        await llm.achat([ChatMessage(role="user", content="hi")])
    assert len(fake.post_bodies) == 1
    assert excinfo.value.reason == REASON_MISSING_FINISH


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"choices": [{"message": {"content": "x"}}]}, REASON_MISSING_FINISH),
        (
            {"choices": [{"message": {"content": "x"}, "finish_reason": None}]},
            REASON_MISSING_FINISH,
        ),
        (
            {"choices": [{"message": {"content": "x"}, "finish_reason": 3}]},
            REASON_MALFORMED_FRAME,
        ),
        ({}, REASON_MALFORMED_FRAME),
        ({"choices": []}, REASON_MALFORMED_FRAME),
        ({"choices": [None]}, REASON_MALFORMED_FRAME),
        (
            {"choices": [{"message": None, "finish_reason": "stop"}]},
            REASON_MALFORMED_FRAME,
        ),
        (
            {
                "error": {"message": "SECRET"},
                "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            },
            REASON_UPSTREAM_ERROR,
        ),
        (
            {"choices": [{"message": {"content": {"bad": 1}}, "finish_reason": "stop"}]},
            REASON_MALFORMED_FRAME,
        ),
        (
            {"choices": [{"message": {"content": [1, 2]}, "finish_reason": "stop"}]},
            REASON_MALFORMED_FRAME,
        ),
        (
            {"choices": [{"message": {"content": 99}, "finish_reason": "stop"}]},
            REASON_MALFORMED_FRAME,
        ),
    ],
)
def test_buffered_payload_requires_explicit_finish(payload, reason):
    """The non-streaming parser (all fallback legs) rejects missing and
    misshapen terminal shapes, top-level errors, and non-string content with
    fixed labels (issues #365 / Q420-P1)."""
    with pytest.raises(TruncatedStreamError) as excinfo:
        _chat_result_from_response(payload)
    assert excinfo.value.reason == reason


@pytest.mark.parametrize(
    "lines,answer",
    [
        pytest.param([], "Recovered answer", id="empty-stream"),
        pytest.param([_ERROR_FRAME, _DONE_LINE], "Complete answer", id="upstream-error"),
        pytest.param(
            [
                'data: {"choices": [{"delta": {"content": "not emitted"}, "finish_reason": 42}]}',
                _DONE_LINE,
            ],
            "Complete answer", id="malformed-first-frame",
        ),
    ],
)
@pytest.mark.anyio
async def test_chat_stream_pre_output_failure_recovers_via_one_post(lines, answer):
    """Before any visible token, one independent POST may recover; rejected
    stream content must never be emitted (issues #365 / Q420-P2)."""
    fake = HttpxStreamFake(
        lines=lines,
        payload={
            **_COMPLETE_PAYLOAD,
            "choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
        },
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = [
        item async for item in llm.chat_stream([ChatMessage(role="user", content="hi")])
    ]
    assert not any("not emitted" in item.get("delta", "") for item in items)
    assert len(fake.post_bodies) == 1
    assert items[0]["type"] == "token" and items[0]["delta"] == answer
    assert items[-1]["type"] == "done" and items[-1]["finish_reason"] == "stop"


@pytest.mark.anyio
async def test_chat_stream_malformed_first_frame_failed_fallback_fails_closed():
    """Q420-P2: a pre-output malformed frame whose fallback also fails
    fails closed without recursion or retry."""
    malformed_first_line = (
        'data: {"choices": [{"delta": {"content": "not emitted"}, "finish_reason": 42}]}'
    )
    fake = HttpxStreamFake(
        lines=[malformed_first_line, _DONE_LINE],
        payload={
            "error": {"message": "SECRET-UPSTREAM-TEXT"},
            "choices": [{"message": {"content": "fallback"}, "finish_reason": "stop"}],
        },
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = []
    with pytest.raises(TruncatedStreamError) as excinfo:
        async for item in llm.chat_stream([ChatMessage(role="user", content="hi")]):
            items.append(item)
    assert len(fake.post_bodies) == 1  # exactly one fallback attempt, no retry loop
    assert excinfo.value.reason == REASON_UPSTREAM_ERROR
    assert "SECRET-UPSTREAM-TEXT" not in str(excinfo.value)
    assert all(i.get("type") != "done" for i in items)


@pytest.mark.parametrize("finish", ["stop", "length", "content_filter"])
def test_buffered_payload_preserves_explicit_finish(finish):
    """Explicit terminal finishes stay literally classified: the parser
    never rewrites a real value."""
    result = _chat_result_from_response(
        {"choices": [{"message": {"content": "x"}, "finish_reason": finish}]}
    )
    assert result.finish_reason == finish


def test_truncation_error_carries_counts_not_content():
    """Log contract: exception text reaches logs, so it must never carry
    response text — counts and a fixed reason only."""
    err = TruncatedStreamError(7)
    assert "7" in str(err)
    assert "[DONE]" in str(err)
    assert "Partial" not in str(err)
    assert err.reason


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
def trunc_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    class TruncLLM:
        async def chat_stream(self, messages, *args, **kwargs):
            yield {"type": "token", "delta": "Partial ", "token": "Partial ", "ttft_ms": 12}
            raise TruncatedStreamError(1)

        def chat(self, *a, **kw):
            raise AssertionError("non-stream chat must not run on the stream path")

    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    yield from _client(monkeypatch, synthetic_pdf, TruncLLM())


@pytest.fixture
def hang_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    yield from _client(monkeypatch, synthetic_pdf, HangingLLM())


def _answer_sse_events(text: str) -> list[tuple[str, dict]]:
    """(event-name, payload) pairs from a raw `event:`/`data:` answer stream."""
    events = []
    current_event = "message"
    current_data: list[str] = []
    for line in text.split("\n"):
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
    return events


@pytest.mark.parametrize(
    "path,stream,llm_stream",
    [
        ("/v1/answer", False, False),
        ("/v1/answer", False, True),
        ("/v1/answer", True, True),
        ("/v1/chat", False, False),
        ("/v1/chat", False, True),
        ("/v1/chat", True, True),
        ("/v1/chat/completions", False, False),
        ("/v1/chat/completions", False, True),
        ("/v1/chat/completions", True, True),
    ],
)
@pytest.mark.parametrize(
    "content,answer,state,script",
    [
        ("\n**Citations:**\n- {cite}", "", "generation_incomplete", None),
        ("Sources:\n- {cite}", "", "generation_incomplete", None),
        ("```thinking\nWork out the answer.\n```\nReferences:\n{cite}",
         "", "generation_incomplete", None),
        ("```jcl\n//JOB EXEC PGM=EXAMPLE\n```\nCitations:\n{cite}",
         "", "unverified_draft", "//JOB EXEC PGM=EXAMPLE"),
        ("Retry.\nCitations:\n{cite}", "Retry.", "accepted", None),
        ("**Answer:**\n\nCitations:\n- {cite}", "**Answer:**", "generation_incomplete", None),
        ("## Answer\n\nCitations:\n- {cite}", "## Answer", "generation_incomplete", None),
        ("Sources: [1]\n\nCitations:\n- {cite}", "Sources: [1]", "generation_incomplete", None),
        ("References: [1]\n\nCitations:\n- {cite}", "References: [1]",
         "generation_incomplete", None),
        ("Citations: <{cite}>\n\nCitations:\n- {cite}", "Citations: <{cite}>",
         "generation_incomplete", None),
        (("Set LFAREA.\n\nReferences:\n- Restart the system with CLPA\n- Check IEASYSxx\n"
          "\nCitations:\n- {cite}"),
         "Set LFAREA.\n\nReferences:\n- Restart the system with CLPA\n- Check IEASYSxx",
         "accepted", None),
        ("**Answer:** Retry.\nCitations:\n{cite}", "**Answer:** Retry.", "accepted", None),
    ],
)
def test_answer_body_state_real_client_all_surfaces(
    monkeypatch, synthetic_pdf, servable_representation_gate, path, stream, llm_stream,
    content, answer, state, script,
):
    search = _search_stub()
    cite = search.search()[0][0].cite
    content = content.format(cite=cite)
    transport = HttpxStreamFake(
        lines=[
            "data: " + json.dumps({"choices": [{"delta": {"content": content}}]}),
            _FINISH_LINE,
            _DONE_LINE,
        ],
        payload={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]},
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=llm_stream)), client=transport)
    monkeypatch.setattr(app_mod, "retrieve_search", search.search)
    for client in _client(monkeypatch, synthetic_pdf, llm):
        if path == "/v1/answer":
            response = client.post(path + ("?stream=true" if stream else ""),
                                   json={"query": "IEA500I"})
            assert response.status_code == 200
            if stream:
                events = _answer_sse_events(response.text)
                assert [kind for kind, _payload in events] == ["token", "final"]
                assert events[0][1]["delta"] == content
                data = events[-1][1]
            else:
                data = response.json()
            assert data["answer"] == answer.format(cite=cite)
        else:
            response = client.post(path, json={
                "messages": [{"role": "user", "content": "IEA500I"}], "stream": stream,
            })
            assert response.status_code == 200
            if stream:
                frames = [json.loads(line[6:]) for line in response.text.splitlines()
                          if line.startswith("data: ") and line != _DONE_LINE]
                terminals = [frame for frame in frames
                             if frame["choices"][0].get("finish_reason")]
                assert len(terminals) == 1
                assert terminals[0]["choices"][0]["finish_reason"] == "stop"
                assert frames[0]["choices"][0]["delta"]["content"] == content
                assert response.text.splitlines().count(_DONE_LINE) == 1
                data = terminals[0]["choices"][0]
            else:
                data = response.json()
                assert data["choices"][0]["finish_reason"] == "stop"
        assert data["verification_state"] == state
        assert data["citations"] == [cite]
        assert data["citations_inferred"] is False
        assert data["script"] == script
        assert data["script_review_required"] is (script is not None)
        assert len(transport.stream_bodies) == int(stream or llm_stream)
        assert len(transport.post_bodies) == int(not (stream or llm_stream))


# Issue #597: adversarial model outputs through the real HttpxLLMClient and
# every answer/chat surface. Outcomes only: state, kept body, citations.
_ADVERSARIAL_SURFACES = [
    ("/v1/answer", False, False),
    ("/v1/answer", True, True),
    ("/v1/chat", False, False),
    ("/v1/chat", True, True),
]


def _drive_surface(client, path, stream):
    """POST one surface; return (final, error). `final` is the normalized
    terminal outcome (answer is None where the wire carries it only as token
    deltas) and `error` the fixed error payload, exactly one of them set."""
    if path == "/v1/answer":
        response = client.post(path + ("?stream=true" if stream else ""),
                               json={"query": "IEA500I"})
        assert response.status_code == 200
        if not stream:
            data = response.json()
            return {"answer": data["answer"], "state": data["verification_state"],
                    "citations": data["citations"], "finish": None}, None
        events = dict(_answer_sse_events(response.text))
        assert ("final" in events) != ("error" in events)
        if "error" in events:
            return None, events["error"]
        data = events["final"]
        return {"answer": data["answer"], "state": data["verification_state"],
                "citations": data["citations"], "finish": None}, None
    response = client.post(path, json={
        "messages": [{"role": "user", "content": "IEA500I"}], "stream": stream,
    })
    assert response.status_code == 200
    if not stream:
        data = response.json()
        choice = data["choices"][0]
        return {"answer": choice["message"]["content"], "state": data["verification_state"],
                "citations": data["citations"], "finish": choice["finish_reason"]}, None
    assert response.text.splitlines().count(_DONE_LINE) == 1
    frames = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith("data: ") and line != _DONE_LINE]
    errors = [frame for frame in frames if "error" in frame]
    terminals = [frame["choices"][0] for frame in frames
                 if frame.get("choices") and frame["choices"][0].get("finish_reason")]
    assert len(errors) + len(terminals) == 1
    if errors:
        return None, errors[0]
    return {"answer": None, "state": terminals[0]["verification_state"],
            "citations": terminals[0]["citations"],
            "finish": terminals[0]["finish_reason"]}, None


def _adversarial_llm(monkeypatch, synthetic_pdf, lines, payload, llm_stream):
    transport = HttpxStreamFake(lines=lines, payload=payload)
    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=llm_stream)), client=transport)
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    return transport, _client(monkeypatch, synthetic_pdf, llm)


@pytest.mark.parametrize("path,stream,llm_stream", _ADVERSARIAL_SURFACES)
def test_reasoning_only_empty_content_never_reads_as_an_answer(
    monkeypatch, synthetic_pdf, servable_representation_gate, path, stream, llm_stream,
):
    """Reasoning channel only, empty content channel, finish `stop`. Buffered
    surfaces report an empty incomplete answer (200); streaming surfaces take the
    one empty-content recovery POST, then fail with the fixed error and no
    success terminal. Nothing accepted, nothing cited."""
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "Thinking it over"}}]}),
        _FINISH_LINE,
        _DONE_LINE,
    ]
    payload = {"choices": [{"message": {"content": "", "reasoning_content": "Thinking it over"},
                            "finish_reason": "stop"}]}
    transport, clients = _adversarial_llm(monkeypatch, synthetic_pdf, lines, payload, llm_stream)
    for client in clients:
        final, error = _drive_surface(client, path, stream)
    assert len(transport.post_bodies) == 1
    assert len(transport.stream_bodies) == int(llm_stream)
    if stream:
        assert final is None
        assert error["verification_state"] == "generation_incomplete"
        assert "Thinking it over" not in json.dumps(error)
    else:
        assert error is None
        assert final["answer"] == ""
        assert final["citations"] == []
        assert final["state"] == "generation_incomplete"


@pytest.mark.parametrize("path,stream,llm_stream", _ADVERSARIAL_SURFACES)
def test_length_finish_mid_citation_keeps_body_drops_fragment(
    monkeypatch, synthetic_pdf, servable_representation_gate, path, stream, llm_stream,
):
    """`finish_reason=length` while the model was writing its Citations block:
    the prose survives, the cut fragment is neither kept in the body nor
    accepted as a citation, and the state is incomplete (never accepted)."""
    cut = ("Reissue the command.\n\nCitations:\n"
           "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA5")
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": cut}}]}),
        'data: {"choices": [{"delta": {}, "finish_reason": "length"}]}',
        _DONE_LINE,
    ]
    payload = {"choices": [{"message": {"content": cut}, "finish_reason": "length"}]}
    transport, clients = _adversarial_llm(monkeypatch, synthetic_pdf, lines, payload, llm_stream)
    for client in clients:
        final, error = _drive_surface(client, path, stream)
    assert error is None
    assert len(transport.stream_bodies) == int(llm_stream)
    assert len(transport.post_bodies) == int(not llm_stream)
    assert final["state"] == "generation_incomplete"
    assert final["citations"] == []
    if final["answer"] is not None:
        assert final["answer"] == "Reissue the command."
    if path == "/v1/chat":
        assert final["finish"] == "length"


def test_v1_answer_stream_truncation_emits_error_without_final(trunc_client):
    """Contract pin: token deltas, then event: error, and NO event: final."""
    resp = trunc_client.post("/v1/answer?stream=true", json={"query": "IEA500I command"})
    assert resp.status_code == 200
    events = _answer_sse_events(resp.text)
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


@pytest.fixture
def error_frame_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    """Real HttpxLLMClient over a fake transport whose stream carries a
    content token, an upstream error frame, then [DONE] — the exact
    counterexample from issue #365, driven through the app routes."""
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    llm = HttpxLLMClient(
        Settings(**_settings_kwargs(llm_stream=True)),
        client=HttpxStreamFake(
            lines=[_CONTENT_LINE, _ERROR_FRAME, _DONE_LINE], payload=_COMPLETE_PAYLOAD
        ),
    )
    yield from _client(monkeypatch, synthetic_pdf, llm)


@pytest.fixture
def missing_finish_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    """Real client whose stream ends with [DONE] but no finish frame, and
    whose non-streaming fallback payload also lacks a finish reason."""
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    llm = HttpxLLMClient(
        Settings(**_settings_kwargs(llm_stream=True)),
        client=HttpxStreamFake(
            lines=[_CONTENT_LINE, _DONE_LINE],
            payload={"choices": [{"message": {"content": "fallback body"}}]},
        ),
    )
    yield from _client(monkeypatch, synthetic_pdf, llm)


def test_v1_answer_real_client_error_frame_emits_fixed_error_without_leak(error_frame_client):
    """Parser-level failure reaches the wire as the fixed error event and no
    final; the upstream error text never appears in the response."""
    resp = error_frame_client.post(
        "/v1/answer?stream=true", json={"query": "IEA500I command"}
    )
    assert resp.status_code == 200
    events = _answer_sse_events(resp.text)
    kinds = [e[0] for e in events]
    assert "token" in kinds
    assert kinds[-1] == "error"
    assert "final" not in kinds
    err = events[-1][1]
    assert err["code"] == "upstream_error" and err["message"] == "stream failed"
    assert err["verification_state"] == "generation_incomplete"
    assert "SECRET-UPSTREAM-TEXT" not in resp.text


def test_v1_chat_real_client_error_frame_is_incomplete_no_success_chunk(error_frame_client):
    """Chat mirror: strict OpenAI error object + [DONE], no chunk claims the
    failed generation completed."""
    resp = error_frame_client.post(
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
    assert error["verification_state"] == "generation_incomplete"
    finished = [
        f
        for f in frames
        if "error" not in f and f.get("choices") and f["choices"][0].get("finish_reason")
    ]
    assert finished == []
    assert "SECRET-UPSTREAM-TEXT" not in resp.text


def test_v1_answer_real_client_missing_finish_both_legs_is_502(missing_finish_client):
    """The buffered JSON path cannot synthesize success from a stream and
    fallback that both lack an explicit finish: fixed 502 envelope only."""
    resp = missing_finish_client.post("/v1/answer", json={"query": "IEA500I command"})
    assert resp.status_code == 502
    assert resp.json() == {"code": "upstream_error", "message": "answer failed"}


def test_v1_chat_json_real_client_missing_finish_is_502(missing_finish_client):
    """Chat JSON shares the answer core: same fixed envelope, never a
    finish_reason "stop" answer body."""
    resp = missing_finish_client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "IEA500I command"}]}
    )
    assert resp.status_code == 502
    assert resp.json() == {"code": "upstream_error", "message": "answer failed"}


def test_v1_chat_stream_real_client_missing_finish_emits_error_and_done(missing_finish_client):
    """Chat SSE: the fallback payload itself lacks a finish, so the stream
    ends with the fixed error frame + [DONE] and no success finish chunk."""
    resp = missing_finish_client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "IEA500I command"}], "stream": True},
    )
    assert resp.status_code == 200
    frames = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    error = next(f for f in frames if "error" in f)
    assert error["error"] == {"code": "upstream_error", "message": "stream failed"}
    assert error["verification_state"] == "generation_incomplete"
    assert all(
        not (f.get("choices") and f["choices"][0].get("finish_reason")) for f in frames
    )


@pytest.fixture
def buffered_error_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    """Client configured with a non-streaming mock returning a top-level error (Q420-P1)."""
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    llm = HttpxLLMClient(
        Settings(**_settings_kwargs(llm_stream=False)),
        client=HttpxStreamFake(
            lines=[],
            payload={
                "error": {"message": "PRIVATE-SENTINEL-KEY"},
                "choices": [
                    {"message": {"content": "should not appear"}, "finish_reason": "stop"}
                ],
            },
        ),
    )
    yield from _client(monkeypatch, synthetic_pdf, llm)


def test_v1_chat_json_real_client_buffered_error_is_502(buffered_error_client):
    """Q420-P1: buffered chat completions with top-level error fail closed as 502
    upstream_error without leaking private error text."""
    resp = buffered_error_client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "IEA500I command"}]},
    )
    assert resp.status_code == 502
    assert resp.json() == {"code": "upstream_error", "message": "answer failed"}
    assert "PRIVATE-SENTINEL-KEY" not in resp.text


@pytest.fixture
def malformed_first_recovering_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    """Client whose first stream frame is malformed finish_reason, recovering via POST (Q420-P2)."""
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    malformed_first_line = (
        'data: {"choices": [{"delta": {"content": "not emitted"}, "finish_reason": 42}]}'
    )
    llm = HttpxLLMClient(
        Settings(**_settings_kwargs(llm_stream=True)),
        client=HttpxStreamFake(
            lines=[malformed_first_line, _DONE_LINE],
            payload=_COMPLETE_PAYLOAD,
        ),
    )
    yield from _client(monkeypatch, synthetic_pdf, llm)


def test_v1_chat_stream_real_client_malformed_first_frame_recovers(
    malformed_first_recovering_client,
):
    """Q420-P2: streaming chat with malformed first frame recovers via single POST fallback,
    emitting no invalid text and completing successfully."""
    resp = malformed_first_recovering_client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "IEA500I command"}], "stream": True},
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text
    assert "not emitted" not in resp.text
    assert "Complete answer" in resp.text


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
async def test_answer_disconnect_after_error_frame_is_not_a_second_abort(
    trunc_client, monkeypatch, caplog
):
    """Terminal frames count even when the client disappears during delivery
    (PR #403 review): closing after the error frame must not record a second
    client_disconnect — the upstream_error outcome already stands."""
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
        assert "event: token" in await body.__anext__()
        assert "event: error" in await body.__anext__()
        await body.aclose()

    assert ("answer", "upstream_error") in recorded
    assert not any(outcome == "client_disconnect" for _, outcome in recorded)
    assert not any('"client_disconnect"' in r.getMessage() for r in caplog.records)


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


def test_truncation_alert_detail_is_fixed_vocabulary():
    """OBS-1A §4.4 vs load-tier contract: the stream_truncated answer_alert
    names the missing terminator via the fixed reason label plus the error
    type — never the exception body (which carries the chunk count) or
    response text."""
    from mainframe_rag.agent.answer import REASON_MISSING_DONE, truncation_alert_detail

    exc = TruncatedStreamError(7, REASON_MISSING_DONE)
    detail = truncation_alert_detail(exc)
    assert "[DONE]" in detail, "alert must name the missing terminator"
    assert "TruncatedStreamError" in detail
    assert "after 7 content chunks" not in detail, "exception body must not reach the alert"
    assert "upstream completion incomplete" not in detail
    assert "Partial" not in detail

    rogue = TruncatedStreamError(0, "custom free-form reason with SECRET-TEXT")
    rogue_detail = truncation_alert_detail(rogue)
    assert rogue_detail == "unknown truncation (TruncatedStreamError)"
    assert "SECRET-TEXT" not in rogue_detail


@pytest.fixture
def blast_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    """LLM stream yields tokens, then raises a non-truncation error after
    headers are committed: the wire shape is a fixed error event, never a
    final, and the request leaves exactly one terminal observation."""

    class BlastLLM:
        async def chat_stream(self, messages, *args, **kwargs):
            yield {"type": "token", "delta": "Partial ", "token": "Partial ", "ttft_ms": 12}
            raise RuntimeError("boom after headers")

        def chat(self, *a, **kw):
            raise AssertionError("non-stream chat must not run on the stream path")

    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    yield from _client(monkeypatch, synthetic_pdf, BlastLLM())


def test_v1_answer_stream_post_header_exception_single_observation(blast_client, monkeypatch):
    """OBS-1B: an exception after headers yields error-without-final with a
    fixed error frame. The single terminal observation rides the same
    terminal/red_recorded path the truncation legs prove under load; the
    SERVER span ends ERROR carrying only the error type."""
    import json as json_mod

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(app_mod, "tracer", provider.get_tracer("blast-test"))
    resp = blast_client.post("/v1/answer?stream=true", json={"query": "IEA500I command"})
    assert resp.status_code == 200
    events = _answer_sse_events(resp.text)
    kinds = [kind for kind, _ in events]
    assert "token" in kinds
    assert "error" in kinds
    assert "final" not in kinds
    # Tokens already sent are provisional output; the terminal error frame
    # itself carries only the fixed contract (no response text).
    error_data = [data for kind, data in events if kind == "error"]
    assert len(error_data) == 1
    assert "Partial" not in json.dumps(error_data[0])
    roots = [s for s in exporter.get_finished_spans() if s.name == "v1.answer"]
    assert len(roots) == 1
    assert roots[0].end_time is not None
    assert roots[0].status.status_code.name == "ERROR"
    assert "boom after headers" not in json_mod.dumps(
        {"attrs": dict(roots[0].attributes),
         "events": [(e.name, dict(e.attributes)) for e in (roots[0].events or [])],
         "status": roots[0].status.description}
    )
