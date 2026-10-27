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

import httpx2
import pytest
from fastapi import Request, Response

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import (
    REASON_MALFORMED_FRAME,
    REASON_MISSING_DONE,
    REASON_MISSING_FINISH,
    REASON_UPSTREAM_ERROR,
    HttpxLLMClient,
    TruncatedStreamError,
    _chat_result_from_response,
    is_model_timeout,
)
from mainframe_rag.agent.app import AnswerRequest, ChatRequest
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage
from tests.fakes import HttpxStreamFake, NonPropagatingGateway, SlowOpenAIUpstream, settings_kw
from tests.helpers_agent_local import (
    StreamStubLLM,
    agent_client,
    agent_hit,
    chat_frames,
    truncation_after_token,
)
from tests.helpers_agent_local import (
    sse_events as _answer_sse_events,
)

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


@pytest.mark.parametrize("sync", [False, True], ids=["achat", "chat"])
@pytest.mark.parametrize(
    "lines",
    [TRUNCATED_LINES, [_CONTENT_LINE, _ERROR_FRAME, _DONE_LINE]],
    ids=["missing-done", "upstream-error"],
)
@pytest.mark.anyio
async def test_buffered_stream_discards_partial_and_recovers_via_one_post(lines, sync):
    """Buffered content is discarded; one POST supplies the complete answer,
    over the async and the sync transport alike."""
    fake = HttpxStreamFake(lines=lines, payload=_COMPLETE_PAYLOAD)
    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=fake)
    messages = [ChatMessage(role="user", content="hi")]
    if sync:
        fake.stream = fake.stream_sync  # Explicitly select the sync transport.
        result = await asyncio.to_thread(llm.chat, messages, reasoning_effort="high")
    else:
        result = await llm.achat(messages, reasoning_effort="high")
    assert len(fake.stream_bodies) == 1
    assert len(fake.post_bodies) == 1
    for body in fake.stream_bodies + fake.post_bodies:
        assert body["reasoning_effort"] == "high"
    assert result.content == "Complete answer"
    assert result.finish_reason == "stop"


@pytest.mark.parametrize("finish", ["length"])
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
async def test_every_malformed_frame_shape_fails_closed_after_content():
    """Each structurally wrong frame shape is rejected with the fixed label,
    never folded into the answer (same outcome as the invalid-json case)."""
    for frame in (
        'data: ["not-a-dict"]',
        'data: {"choices": ["not-a-dict"]}',
        'data: {"choices": [{"delta": "not-a-dict"}]}',
        'data: {"choices": [{"delta": {"content": 5}}]}',
    ):
        fake = HttpxStreamFake(lines=[_CONTENT_LINE, frame, _DONE_LINE], payload=_COMPLETE_PAYLOAD)
        llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
        with pytest.raises(TruncatedStreamError) as excinfo:
            async for _ in llm.chat_stream([ChatMessage(role="user", content="hi")]):
                pass
        assert excinfo.value.reason == REASON_MALFORMED_FRAME, frame
        assert fake.post_bodies == []


_NO_FINISH_PAYLOAD = {"choices": [{"message": {"content": "fallback body"}}]}
_MALFORMED_FIRST = (
    'data: {"choices": [{"delta": {"content": "not emitted"}, "finish_reason": 42}]}'
)


@pytest.mark.anyio
@pytest.mark.parametrize("api, lines, payload, reason", [
    pytest.param("chat-stream", [_ERROR_FRAME, _DONE_LINE], _NO_FINISH_PAYLOAD,
                 REASON_MISSING_FINISH, id="error-frame-failed-fallback"),
    pytest.param("achat", [_CONTENT_LINE, _DONE_LINE], _NO_FINISH_PAYLOAD,
                 REASON_MISSING_FINISH, id="missing-finish-both-legs"),
    pytest.param("chat-stream", [_MALFORMED_FIRST, _DONE_LINE],
                 {"error": {"message": "SECRET-UPSTREAM-TEXT"},
                  "choices": [{"message": {"content": "fallback"}, "finish_reason": "stop"}]},
                 REASON_UPSTREAM_ERROR, id="malformed-first-failed-fallback"),
])
async def test_pre_output_failure_with_failed_fallback_fails_closed(api, lines, payload, reason):
    """A pre-output failure recovers through exactly one POST; when that
    fallback is itself incomplete, the request fails closed with the fixed
    reason — no synthesized "stop", no done item, no retry loop and no
    upstream text in the exception (issues #365 / Q420-P2)."""
    fake = HttpxStreamFake(lines=lines, payload=payload)
    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=fake)
    items = []
    with pytest.raises(TruncatedStreamError) as excinfo:
        if api == "achat":
            await llm.achat([ChatMessage(role="user", content="hi")])
        else:
            async for item in llm.chat_stream([ChatMessage(role="user", content="hi")]):
                items.append(item)
    assert len(fake.post_bodies) == 1
    assert excinfo.value.reason == reason
    assert all(i.get("type") != "done" for i in items)
    assert "SECRET-UPSTREAM-TEXT" not in str(excinfo.value)


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"choices": [{"message": {"content": "x"}}]}, REASON_MISSING_FINISH),
        (
            {"choices": [{"message": {"content": "x"}, "finish_reason": 3}]},
            REASON_MALFORMED_FRAME,
        ),
        ({}, REASON_MALFORMED_FRAME),
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


def test_buffered_payload_rejects_every_other_misshapen_terminal():
    for payload in (
        {"choices": [{"message": {"content": "x"}, "finish_reason": None}]},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"message": None, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": [1, 2]}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": 99}, "finish_reason": "stop"}]},
    ):
        with pytest.raises(TruncatedStreamError):
            _chat_result_from_response(payload)


def test_buffered_payload_preserves_explicit_finish():
    """Explicit terminal finishes stay literally classified: the parser
    never rewrites a real value."""
    for finish in ("stop", "length", "content_filter"):
        result = _chat_result_from_response(
            {"choices": [{"message": {"content": "x"}, "finish_reason": finish}]}
        )
        assert result.finish_reason == finish


def _client(monkeypatch, _synthetic_pdf, llm):
    """Hermetic app client; `synthetic_pdf` keeps the session PDF alive."""
    with agent_client(monkeypatch, llm=llm) as client:
        yield client


def _search_stub():
    """One synthetic hit through the patched retrieval leg: tests drive the
    stream machinery, not retrieval."""
    hit = agent_hit(text="IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy")

    class StubSearch:
        def search(self, *a, **kw):
            return [hit], "identifier", {"embed_ms": 1, "qdrant_ms": 2}

    return StubSearch()


class HangingLLM(StreamStubLLM):
    """Streams one token then never finishes, so a test can close the
    response generator the way a client disconnect does."""

    def __init__(self):
        super().__init__(({"type": "token", "delta": "Partial ", "token": "Partial ",
                           "ttft_ms": 5},))

    async def chat_stream(self, messages, *args, **kwargs):
        yield self.items[0]
        await asyncio.Event().wait()


@pytest.fixture
def trunc_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    yield from _client(monkeypatch, synthetic_pdf, truncation_after_token(1))


@pytest.fixture
def hang_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    yield from _client(monkeypatch, synthetic_pdf, HangingLLM())


_BODY_SURFACES = [
    ("/v1/answer", False, False),
    ("/v1/answer", True, True),
    ("/v1/chat", False, True),
    ("/v1/chat", True, True),
    ("/v1/chat/completions", False, False),
    ("/v1/chat/completions", True, True),
]
# Every surface proves the two decisive outcomes (citations-only body is an
# incomplete draft; an in-body "References:" list is kept and accepted); the
# remaining body-shape classes run on one buffered and one streaming surface.
_BODY_COMMON = [
    ("\n**Citations:**\n- {cite}", "", "generation_incomplete", None),
    (("Set LFAREA.\n\nReferences:\n- Restart the system with CLPA\n- Check IEASYSxx\n"
      "\nCitations:\n- {cite}"),
     "Set LFAREA.\n\nReferences:\n- Restart the system with CLPA\n- Check IEASYSxx",
     "accepted", None),
]
_BODY_EXTRA = [
    ("```thinking\nWork out the answer.\n```\nReferences:\n{cite}",
     "", "generation_incomplete", None),
    ("```jcl\n//JOB EXEC PGM=EXAMPLE\n```\nCitations:\n{cite}",
     "", "unverified_draft", "//JOB EXEC PGM=EXAMPLE"),
    ("**Answer:**\n\nCitations:\n- {cite}", "**Answer:**", "generation_incomplete", None),
    ("Sources: [1]\n\nCitations:\n- {cite}", "Sources: [1]", "generation_incomplete", None),
    ("Citations: <{cite}>\n\nCitations:\n- {cite}", "Citations: <{cite}>",
     "generation_incomplete", None),
]
_BODY_CASES = (
    [(*surface, *shape) for surface in _BODY_SURFACES for shape in _BODY_COMMON]
    + [(*_BODY_SURFACES[0], *shape) for shape in _BODY_EXTRA]
    + [(*_BODY_SURFACES[3], *shape) for shape in _BODY_EXTRA]
)


@pytest.mark.parametrize("path,stream,llm_stream,content,answer,state,script", _BODY_CASES)
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
        # Issue #868: every model call is read from the streamed wire.
        assert len(transport.stream_bodies) == 1
        assert len(transport.post_bodies) == 0


# Issue #597: adversarial model outputs through the real HttpxLLMClient and
# every answer/chat surface. Outcomes only: state, kept body, citations.
_ADVERSARIAL_SURFACES = [
    ("/v1/answer", True, True),
    ("/v1/chat", False, False),
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
    # The `llm_stream` opt-in keeps the empty-`stop` recovery POST; a buffered
    # caller reads the stream and has no recovery (issue #868).
    assert len(transport.post_bodies) == int(llm_stream)
    assert len(transport.stream_bodies) == 1
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
    assert len(transport.stream_bodies) == 1
    assert len(transport.post_bodies) == 0
    assert final["state"] == "generation_incomplete"
    assert final["citations"] == []
    if final["answer"] is not None:
        assert final["answer"] == "Reissue the command."
    if path == "/v1/chat":
        assert final["finish"] == "length"


def _real_llm_client(monkeypatch, synthetic_pdf, lines, payload):
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    llm = HttpxLLMClient(
        Settings(**_settings_kwargs(llm_stream=True)),
        client=HttpxStreamFake(lines=lines, payload=payload),
    )
    yield from _client(monkeypatch, synthetic_pdf, llm)


@pytest.fixture
def error_frame_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    """Real client whose stream carries a content token, the upstream error
    frame, then [DONE] — the exact counterexample from issue #365."""
    yield from _real_llm_client(
        monkeypatch, synthetic_pdf, [_CONTENT_LINE, _ERROR_FRAME, _DONE_LINE],
        _COMPLETE_PAYLOAD,
    )


@pytest.fixture
def missing_finish_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    """Real client whose stream ends with [DONE] but no finish frame, and
    whose non-streaming fallback payload also lacks a finish reason."""
    yield from _real_llm_client(
        monkeypatch, synthetic_pdf, [_CONTENT_LINE, _DONE_LINE],
        {"choices": [{"message": {"content": "fallback body"}}]},
    )


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


@pytest.mark.parametrize("failure", ["error-frame", "missing-finish"])
def test_v1_chat_stream_real_client_failure_is_incomplete_no_success_chunk(request, failure):
    """Chat SSE: a parser-level failure ends with the fixed OpenAI error
    object + [DONE]; no chunk claims the failed generation completed and no
    upstream error text reaches the response."""
    client = request.getfixturevalue(
        "error_frame_client" if failure == "error-frame" else "missing_finish_client"
    )
    resp = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "IEA500I command"}], "stream": True},
    )
    assert resp.status_code == 200
    assert resp.text.rstrip().endswith("data: [DONE]")
    frames = chat_frames(resp.text)
    error = next(f for f in frames if "error" in f)
    assert error["error"] == {"code": "upstream_error", "message": "stream failed"}
    assert error["verification_state"] == "generation_incomplete"
    finished = [
        f
        for f in frames
        if f.get("choices") and f["choices"][0].get("finish_reason")
    ]
    assert finished == []
    assert "SECRET-UPSTREAM-TEXT" not in resp.text


@pytest.mark.parametrize("path, body", [
    ("/v1/answer", {"query": "IEA500I command"}),
    ("/v1/chat", {"messages": [{"role": "user", "content": "IEA500I command"}]}),
], ids=["answer", "chat"])
def test_json_real_client_missing_finish_both_legs_is_502(missing_finish_client, path, body):
    """The buffered JSON paths cannot synthesize success from a stream and
    fallback that both lack an explicit finish: fixed 502 envelope only."""
    resp = missing_finish_client.post(path, json=body)
    assert resp.status_code == 502
    assert resp.json() == _ANSWER_FAILED


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
    assert resp.json() == _ANSWER_FAILED
    assert "PRIVATE-SENTINEL-KEY" not in resp.text


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
@pytest.mark.parametrize("route", ["answer", "chat"])
async def test_stream_disconnect_records_generation_incomplete(
    hang_client, monkeypatch, caplog, route
):
    """A client disconnect before any terminal frame cannot receive a frame
    (GeneratorExit at the yield), but is observable server-side (issue #365)
    on both streaming routes: alert log with the incomplete state plus a
    client_disconnect outcome."""

    recorded: list[tuple] = []
    monkeypatch.setattr(
        app_mod,
        "record_request",
        lambda endpoint, outcome, **kw: recorded.append((endpoint, outcome)),
    )
    with caplog.at_level(logging.WARNING, logger="agent"):
        if route == "answer":
            response = await app_mod.v1_answer(
                Request(_scope("/v1/answer")),
                app_mod.AnswerRequest(query="IEA500I command"),
                Response(),
                stream=True,
            )
            first_marker = "event: token"
        else:
            response = await app_mod.chat_completions(
                app_mod.ChatRequest(
                    messages=[{"role": "user", "content": "IEA500I command"}], stream=True
                ),
                Request(_scope("/v1/chat")),
                Response(),
            )
            first_marker = '"delta"'
        body = response.body_iterator
        assert first_marker in await body.__anext__()
        await body.aclose()

    aborts = [r for r in caplog.records if '"client_disconnect"' in r.getMessage()]
    assert len(aborts) == 1
    assert '"verification_state": "generation_incomplete"' in aborts[0].getMessage()
    assert recorded[-1] == (route, "client_disconnect")


@pytest.mark.anyio
async def test_answer_disconnect_after_error_frame_is_not_a_second_abort(
    trunc_client, monkeypatch, caplog
):
    """Terminal frames count even when the client disappears during delivery
    (PR #403 review): closing after the error frame must not record a second
    client_disconnect — the upstream_error outcome already stands."""


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
def truncating_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    """Streams one token, then the upstream connection ends without [DONE]
    (the typed TruncatedStreamError), on every streaming route."""
    monkeypatch.setenv("UI_ENABLED", "true")
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    yield from _client(monkeypatch, synthetic_pdf, truncation_after_token(REASON_MISSING_DONE))


@pytest.mark.parametrize("path", ["/v1/answer", "/v1/chat", "/ui/chat/stream"])
def test_truncated_stream_alerts_once_with_one_terminal_frame(
    truncating_client, monkeypatch, caplog, path
):
    """#985: every streaming route, the console included, observes a
    truncated upstream the same way: one fixed-vocabulary stream_truncated
    answer_alert, one RED record carrying the retrieval's class and hit
    count, and exactly one terminal frame (the error, never a final)."""
    recorded = []
    real = app_mod.record_request

    def spy(endpoint, code, *a, **k):
        recorded.append((endpoint, code, k.get("query_class"), k.get("hits")))
        return real(endpoint, code, *a, **k)

    monkeypatch.setattr(app_mod, "record_request", spy)
    messages = [{"role": "user", "content": "IEA500I command"}]
    body = {"query": "IEA500I command"} if path == "/v1/answer" else {"messages": messages}
    if path != "/ui/chat/stream":
        body["stream"] = True
    with caplog.at_level(logging.INFO):
        resp = truncating_client.post(path, json=body)

    assert resp.status_code == 200
    alerts = [
        json.loads(r.getMessage()) for r in caplog.records
        if '"answer_alert"' in r.getMessage() and '"stream_truncated"' in r.getMessage()
    ]
    assert len(alerts) == 1
    assert "Partial" not in json.dumps(alerts[0])
    endpoint = {"/v1/answer": "answer", "/v1/chat": "chat", "/ui/chat/stream": "console"}[path]
    assert recorded == [(endpoint, "upstream_error", "identifier", 1)]
    if path == "/v1/chat":
        frames = [line for line in resp.text.splitlines() if line.startswith("data: ")]
        assert sum('"error"' in f for f in frames) == 1
        assert not any('"finish_reason": "stop"' in f for f in frames)
    else:
        kinds = [kind for kind, _ in _answer_sse_events(resp.text)]
        assert kinds.count("error") == 1 and "final" not in kinds


@pytest.fixture
def blast_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    """LLM stream yields tokens, then raises a generic (non-truncation)
    error after headers are committed: the wire shape is a fixed error event,
    never a final, and the request leaves exactly one terminal observation
    without a `stream_truncated` alert (that alert is truncation-only)."""
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    llm = StreamStubLLM(
        ({"type": "token", "delta": "Partial ", "token": "Partial ", "ttft_ms": 12},),
        RuntimeError("boom after headers"),
    )
    yield from _client(monkeypatch, synthetic_pdf, llm)


def test_v1_answer_stream_post_header_exception_single_observation(blast_client, monkeypatch, caplog):
    """OBS-1B: a generic exception after headers yields error-without-final
    with a fixed error frame and no `stream_truncated` alert (that alert is
    reserved for typed truncation). The single terminal observation rides the
    same terminal/red_recorded path the truncation legs prove under load; the
    SERVER span ends ERROR carrying only the error type."""
    import json as json_mod

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(app_mod, "tracer", provider.get_tracer("blast-test"))
    with caplog.at_level(logging.WARNING):
        resp = blast_client.post("/v1/answer?stream=true", json={"query": "IEA500I command"})
    assert resp.status_code == 200
    assert not any('"stream_truncated"' in r.getMessage() for r in caplog.records)
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


# ---------------------------------------------------------------------------
# Issue #868: the answer path has one model-leg budget and never re-asks a
# generation that already ended or already ran out of time. A real-socket
# upstream stands in for the gateway so abandonment is observable as a closed
# connection, which is what the model server sees.
# ---------------------------------------------------------------------------

_BUDGET_SURFACES = [
    pytest.param("/v1/answer", False, id="answer-buffered"),
    pytest.param("/v1/answer", True, id="answer-stream"),
    pytest.param("/v1/chat", False, id="chat-buffered"),
    pytest.param("/v1/chat", True, id="chat-stream"),
]
_MODEL_TIMEOUT = {"code": "model_timeout", "message": "model response timed out"}
_ANSWER_FAILED = {"code": "upstream_error", "message": "answer failed"}
_HI = [ChatMessage(role="user", content="hi")]


def _socket_llm(upstream, **overrides):
    return HttpxLLMClient(
        Settings(**settings_kw(llm_base_url=upstream.base_url, **overrides))
    )


def _post_surface(client, path, stream):
    if path == "/v1/answer":
        return client.post(path + ("?stream=true" if stream else ""), json={"query": "IEA500I"})
    return client.post(
        path, json={"messages": [{"role": "user", "content": "IEA500I"}], "stream": stream}
    )


def _stream_error(path, text):
    """The one terminal error object of a stream, in each surface's shape."""
    if path == "/v1/answer":
        events = _answer_sse_events(text)
        assert [kind for kind, _ in events][-1] == "error"
        assert "final" not in [kind for kind, _ in events]
        payload = events[-1][1]
        return {"code": payload["code"], "message": payload["message"]}
    frames = [json.loads(line[6:]) for line in text.splitlines()
              if line.startswith("data: ") and line != _DONE_LINE]
    errors = [frame for frame in frames if "error" in frame]
    assert len(errors) == 1
    assert errors[0]["verification_state"] == "generation_incomplete"
    return errors[0]["error"]


@pytest.mark.anyio
@pytest.mark.parametrize("sync", [False, True], ids=["achat", "chat"])
async def test_length_without_content_is_final_and_not_reasked(sync):
    """A stream that ended with an explicit `length` finish and only
    reasoning text is a complete (incomplete-generation) result, over the
    async and the sync entry point alike. Asking again would run the same
    generation to the same cut, so there is no recovery POST."""
    with SlowOpenAIUpstream(stream="length_reasoning_only", post="stall") as upstream:
        llm = _socket_llm(upstream, llm_stream=True, answer_timeout_s=2.0)
        if sync:
            result = await asyncio.to_thread(llm.chat, _HI)
        else:
            result = await llm.achat(_HI)
        await llm.aclose()
    assert (result.content, result.finish_reason) == ("", "length")
    assert [r["stream"] for r in upstream.requests] == [True]


@pytest.mark.anyio
async def test_chat_stream_length_without_content_ends_with_length_done():
    with SlowOpenAIUpstream(stream="length_reasoning_only", post="stall") as upstream:
        llm = _socket_llm(upstream, answer_timeout_s=2.0)
        items = [i async for i in llm.chat_stream([ChatMessage(role="user", content="hi")])]
        await llm.aclose()
    assert [i["type"] for i in items] == ["done"]
    assert items[0]["finish_reason"] == "length"
    assert [r["stream"] for r in upstream.requests] == [True]


@pytest.mark.parametrize("path,stream", _BUDGET_SURFACES)
def test_length_with_reasoning_only_is_incomplete_not_a_gateway_failure(
    monkeypatch, synthetic_pdf, servable_representation_gate, path, stream,
):
    """Issue #868 counterexample: xhigh reasoning consumes the whole output
    cap, the model finishes `length` with empty content. Every surface reports
    an incomplete generation (never 502), after exactly one model request."""
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    with SlowOpenAIUpstream(stream="length_reasoning_only", post="stall") as upstream:
        llm = _socket_llm(upstream, llm_stream=True, answer_timeout_s=2.0)
        for client in _client(monkeypatch, synthetic_pdf, llm):
            final, error = _drive_surface(client, path, stream)
    assert error is None
    assert final["state"] == "generation_incomplete"
    assert final["citations"] == []
    if final["finish"] is not None:
        assert final["finish"] == "length"
    assert [r["stream"] for r in upstream.requests] == [True]


@pytest.mark.parametrize("path,stream", _BUDGET_SURFACES)
@pytest.mark.parametrize("llm_stream", [False, True], ids=["buffered-config", "llm-stream"])
def test_model_wait_expiry_is_model_timeout_and_closes_the_connection(
    monkeypatch, synthetic_pdf, servable_representation_gate, path, stream, llm_stream,
):
    """answer_timeout_s expiry is a stable `model_timeout` (504 on JSON, the
    terminal error on a stream), never `upstream_error`; the model server sees
    the disconnect, and the timed-out request is not asked a second time."""
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    script = "token_then_stall" if stream else "stall"
    with SlowOpenAIUpstream(stream=script, post="stall") as upstream:
        llm = _socket_llm(upstream, llm_stream=llm_stream, answer_timeout_s=0.3)
        for client in _client(monkeypatch, synthetic_pdf, llm):
            resp = _post_surface(client, path, stream)
        assert upstream.wait_for(lambda u: u.disconnects >= 1)
    if stream:
        assert resp.status_code == 200
        assert _stream_error(path, resp.text) == _MODEL_TIMEOUT
    else:
        assert resp.status_code == 504
        assert resp.json() == _MODEL_TIMEOUT
    assert len(upstream.requests) == 1
    assert upstream.finished == 0


@pytest.mark.parametrize("path,stream", _BUDGET_SURFACES)
def test_request_deadline_expiry_closes_the_model_connection(
    monkeypatch, synthetic_pdf, servable_representation_gate, path, stream,
):
    """The documented total budget (`request_deadline_s`) abandons the model
    request by closing its connection, on JSON and stream, answer and chat,
    with a stable `deadline_exceeded` — never asked a second time."""
    monkeypatch.setattr(app_mod, "retrieve_search", _search_stub().search)
    monkeypatch.setenv("REQUEST_DEADLINE_S", "0.4")
    script = "token_then_stall" if stream else "stall"
    with SlowOpenAIUpstream(stream=script, post="stall") as upstream:
        llm = _socket_llm(upstream, llm_stream=stream, answer_timeout_s=60.0)
        for client in _client(monkeypatch, synthetic_pdf, llm):
            resp = _post_surface(client, path, stream)
        assert upstream.wait_for(lambda u: u.disconnects >= 1)
    if stream:
        assert resp.status_code == 200
        assert "final" not in resp.text
    else:
        assert resp.status_code == 504
        assert resp.json()["code"] == "deadline_exceeded"
    assert len(upstream.requests) == 1


# ---------------------------------------------------------------------------
# Issue #868, buffered callers: abandonment must reach the engine through a
# gateway that forwards a disconnect only for streamed requests.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_gateway_fake_does_not_propagate_a_buffered_disconnect():
    """Control: the fake gateway reproduces the live defect for a plain
    buffered POST, so the tests below prove the client's change, not the fake."""
    with SlowOpenAIUpstream(stream="stall", post="stall") as upstream, NonPropagatingGateway(upstream) as gateway:
        async with httpx2.AsyncClient(timeout=0.3) as http:
            with pytest.raises(httpx2.ReadTimeout):
                await http.post(f"{gateway.base_url}/chat/completions", json={"model": "m", "messages": []})
        assert upstream.wait_for(lambda u: len(u.requests) == 1)
        assert not upstream.wait_for(lambda u: u.disconnects >= 1, timeout=1.0)


@pytest.mark.anyio
@pytest.mark.parametrize("llm_stream", [False, True], ids=["buffered-config", "llm-stream"])
async def test_buffered_call_expiry_reaches_the_engine_through_the_gateway(llm_stream):
    with SlowOpenAIUpstream(stream="stall", post="stall") as upstream, NonPropagatingGateway(upstream) as gateway:
        llm = _socket_llm(gateway, llm_stream=llm_stream, answer_timeout_s=0.3)
        with pytest.raises(httpx2.TimeoutException):
            await llm.achat(_HI)
        await llm.aclose()
        assert upstream.wait_for(lambda u: u.disconnects >= 1, timeout=3.0)
    assert [r["stream"] for r in upstream.requests] == [True]


@pytest.mark.anyio
async def test_buffered_call_cancellation_reaches_the_engine_through_the_gateway():
    """The request-deadline path: the awaiting task is cancelled."""
    with SlowOpenAIUpstream(stream="stall", post="stall") as upstream, NonPropagatingGateway(upstream) as gateway:
        llm = _socket_llm(gateway, answer_timeout_s=60.0)
        task = asyncio.ensure_future(llm.achat(_HI))
        assert await asyncio.to_thread(upstream.wait_for, lambda u: len(u.requests) == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await llm.aclose()
        assert upstream.wait_for(lambda u: u.disconnects >= 1, timeout=3.0)


@pytest.mark.anyio
async def test_buffered_call_keeps_a_total_wait_while_frames_trickle():
    """A buffered POST had no byte until done, so `answer_timeout_s` was the
    whole generation. Over the stream, reasoning frames renew httpx's per-read
    timeout; the total bound must survive, as the same timeout class."""
    with SlowOpenAIUpstream(stream="trickle", step_s=0.05) as upstream:
        llm = _socket_llm(upstream, answer_timeout_s=0.4)
        started = asyncio.get_running_loop().time()
        with pytest.raises(httpx2.ReadTimeout) as caught:
            await llm.achat(_HI)
        await llm.aclose()
        assert asyncio.get_running_loop().time() - started < 3.0
        assert is_model_timeout(caught.value)
        assert upstream.wait_for(lambda u: u.disconnects >= 1)
    assert len(upstream.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("sync", [False, True], ids=["achat", "chat"])
async def test_buffered_call_assembles_the_stream_into_the_buffered_result(sync):
    fake = HttpxStreamFake(
        lines=[
            'data: {"choices": [{"delta": {"reasoning_content": "hmm"}}]}',
            'data: {"choices": [{"delta": {"content": "Complete "}}]}',
            'data: {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}',
            'data: {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}',
            _DONE_LINE,
        ],
        payload=_COMPLETE_PAYLOAD,
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    if sync:
        fake.stream = fake.stream_sync
        result = await asyncio.to_thread(llm.chat, _HI)
    else:
        result = await llm.achat(_HI)
    assert (result.content, result.finish_reason) == ("Complete answer", "stop")
    assert (result.usage.prompt_tokens, result.usage.completion_tokens, result.usage.total_tokens) == (5, 3, 8)
    assert result.ttft_ms is None
    assert fake.post_bodies == []
    assert fake.stream_bodies[0]["stream"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("sync", [False, True], ids=["achat", "chat"])
@pytest.mark.parametrize(
    "lines,reason",
    [
        ([_CONTENT_LINE, _ERROR_FRAME, _DONE_LINE], REASON_UPSTREAM_ERROR),
        (TRUNCATED_LINES, REASON_MISSING_DONE),
        ([_CONTENT_LINE, _DONE_LINE], REASON_MISSING_FINISH),
    ],
    ids=["error-frame", "missing-done", "missing-finish"],
)
async def test_buffered_call_incomplete_stream_is_truncation_without_a_second_ask(
    lines, reason, sync, caplog,
):
    fake = HttpxStreamFake(lines=lines, payload=_COMPLETE_PAYLOAD)
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    if sync:
        fake.stream = fake.stream_sync
    with caplog.at_level(logging.DEBUG), pytest.raises(TruncatedStreamError) as caught:
        if sync:
            await asyncio.to_thread(llm.chat, _HI)
        else:
            await llm.achat(_HI)
    assert reason in str(caught.value)
    assert "SECRET-UPSTREAM-TEXT" not in str(caught.value) + caplog.text
    assert fake.post_bodies == []


@pytest.mark.anyio
async def test_buffered_call_length_without_content_is_the_result_and_empty_stop_stays_empty():
    for finish in ("length", "stop"):
        fake = HttpxStreamFake(
            lines=[f'data: {{"choices": [{{"delta": {{}}, "finish_reason": "{finish}"}}]}}', _DONE_LINE],
        )
        llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
        result = await llm.achat(_HI)
        assert (result.content, result.finish_reason) == ("", finish)
        assert fake.post_bodies == []


# ---------------------------------------------------------------------------
# Issue #868 review follow-up: an endpoint that ignores `stream: true` and
# answers application/json keeps working for buffered callers.
# ---------------------------------------------------------------------------

_JSON_ANSWER = {
    "choices": [{"message": {"content": "Complete answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def _json_only_backend(payload, posts):
    def respond(request):
        posts.append(json.loads(request.content))
        return httpx2.Response(200, json=payload)

    return httpx2.MockTransport(respond)


@pytest.mark.anyio
@pytest.mark.parametrize("sync", [False, True], ids=["achat", "chat"])
async def test_buffered_call_accepts_a_json_only_backend_with_one_request(sync):
    posts: list = []
    settings = Settings(**_settings_kwargs())
    if sync:
        with httpx2.Client(transport=_json_only_backend(_JSON_ANSWER, posts)) as http:
            result = await asyncio.to_thread(
                HttpxLLMClient(settings, client=http).chat, _HI
            )
    else:
        async with httpx2.AsyncClient(transport=_json_only_backend(_JSON_ANSWER, posts)) as http:
            result = await HttpxLLMClient(settings, client=http).achat(_HI)
    assert (result.content, result.finish_reason) == ("Complete answer", "stop")
    assert (result.usage.prompt_tokens, result.usage.total_tokens) == (5, 8)
    assert len(posts) == 1


@pytest.mark.anyio
async def test_json_only_backend_payload_is_still_validated():
    posts: list = []
    bad = {"choices": [{"message": {"content": "x"}}]}  # no finish_reason
    async with httpx2.AsyncClient(transport=_json_only_backend(bad, posts)) as http:
        llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=http)
        with pytest.raises(TruncatedStreamError) as caught:
            await llm.achat(_HI)
    assert caught.value.reason == REASON_MISSING_FINISH
    assert len(posts) == 1


@pytest.mark.anyio
async def test_buffered_usage_absent_matches_the_buffered_payload_default():
    """Base reported zeros for a buffered payload without `usage`; so does the
    assembled stream."""
    posts: list = []
    no_usage = {"choices": [{"message": {"content": "a"}, "finish_reason": "stop"}]}
    async with httpx2.AsyncClient(transport=_json_only_backend(no_usage, posts)) as http:
        base_usage = (await HttpxLLMClient(Settings(**_settings_kwargs()), client=http).achat(_HI)).usage
    fake = HttpxStreamFake(lines=[_CONTENT_LINE, _FINISH_LINE, _DONE_LINE])
    streamed = await HttpxLLMClient(Settings(**_settings_kwargs()), client=fake).achat(_HI)
    assert streamed.usage == base_usage
    assert (base_usage.prompt_tokens, base_usage.completion_tokens, base_usage.total_tokens) == (0, 0, 0)


@pytest.mark.anyio
async def test_json_only_backend_read_keeps_the_total_wait():
    async def slow(request):
        await asyncio.sleep(5)
        return httpx2.Response(200, json=_JSON_ANSWER)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(slow)) as http:
        llm = HttpxLLMClient(Settings(**_settings_kwargs(answer_timeout_s=0.3)), client=http)
        with pytest.raises(httpx2.ReadTimeout):
            await llm.achat(_HI)
