"""Open WebUI docs Pipe over the agent's streaming /v1/chat (issue #1072).

The Pipe runs inside Open WebUI, so these tests drive it through its
documented interface only (`Pipe().pipe(body, __user__, __event_emitter__,
__task__)`), with no Open WebUI installed. The fake agent server builds its
frames with the agent's own SSE formatters and `SuppliedExcerpt` model, and
one round trip runs the real agent app, so producer and consumer cannot
drift apart silently. Expected user-visible texts are spelled out here, not
read back from the Pipe's tables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from integrations.openwebui import docs_pipe

from mainframe_rag.agent.answer import SuppliedExcerpt
from mainframe_rag.agent.sse import format_openai_chunk, format_openai_done, format_openai_error

QUESTION = "How do I recover from IEA500I?"
CITE_A = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
CITE_B = "SA22-0001-00 Other Reference, Chapter 9 > Recovery, p. 9-2"
EXCERPT_A = "IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy"
EXCERPT_B = "Reissue the command after the recovery completes."
# The agent's own fixed texts and an injected upstream-looking string: the
# Pipe must show neither (client errors are fixed messages and stable codes).
UPSTREAM_TEXT = "Traceback: secret-token-123 at llm.internal"


def _excerpt(index: int, citation: str, text: str, doc_id: str) -> dict[str, Any]:
    return SuppliedExcerpt(
        index=index,
        citation=citation,
        product="z/OS",
        version="3.1",
        doc_id=doc_id,
        title="Synthetic Reference",
        heading="Chapter 2 > IEA500I",
        page_label="1-6",
        pdf_page_start=6,
        pdf_page_end=6,
        start_char=0,
        end_char=len(text),
        source_chars=len(text),
        truncated_start=False,
        truncated_end=False,
        text=text,
    ).model_dump()


def _final_meta(**overrides: Any) -> dict[str, Any]:
    """The agent's terminal-chunk metadata (`_answer_metadata` shape)."""
    meta: dict[str, Any] = {
        "citations": [CITE_A],
        "citations_inferred": False,
        "inferred_indices": [],
        "hits": [{"chunk_id": "abc123"}],
        "verification_state": "accepted",
        "script": None,
        "script_lang": None,
        "script_review_required": False,
        "supplied_evidence": [
            _excerpt(1, CITE_A, EXCERPT_A, "SA22-0000-00"),
            _excerpt(2, CITE_B, EXCERPT_B, "SA22-0001-00"),
        ],
        "evidence_omitted": 0,
    }
    meta.update(overrides)
    return meta


def _frames(*deltas: str, finish: str | None = "stop", meta: dict[str, Any] | None = None,
            done: bool = True) -> list[str]:
    frames = [format_openai_chunk("chatcmpl-x", "m", delta_content=d) for d in deltas]
    if finish is not None:
        frames.append(format_openai_chunk(
            "chatcmpl-x", "m", finish_reason=finish, extra=_final_meta() if meta is None else meta
        ))
    if done:
        frames.append(format_openai_done())
    return frames


class FakeAgent:
    """A fake agent server behind httpx.MockTransport: records every request
    and answers with fixed SSE frames or a JSON error envelope."""

    def __init__(self, frames: list[str] | None = None, *, status: int = 200,
                 body: bytes | None = None, raise_exc: Exception | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.frames = frames if frames is not None else _frames("Answer.")
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.raise_exc = raise_exc
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.status != 200:
            return httpx.Response(self.status, content=self.body or b"",
                                  headers={"content-type": "application/json", **self.headers})
        return httpx.Response(200, content="".join(self.frames).encode(),
                              headers={"content-type": "text/event-stream"})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def _messages(*turns: tuple[str, Any]) -> list[dict[str, Any]]:
    return [{"role": role, "content": content} for role, content in turns]


async def owui_stream(result: Any, events: list[dict[str, Any]]) -> str:
    """Model of how Open WebUI v0.12.3 turns a streaming Pipe's yields into the
    message (upstream `functions.py` stream wrapper plus `utils/middleware.py`):
    a dict becomes a `data: <json>` frame, a string that starts with `data:` is
    forwarded as a raw SSE frame, any other string is wrapped as a content
    chunk. A frame with a top-level `event` is emitted as that event; otherwise
    `choices[0].delta.content` is appended to the message."""
    content: list[str] = []
    async for item in result:
        if isinstance(item, dict):
            line = "data: " + json.dumps(item)
        elif isinstance(item, str) and item.startswith("data:"):
            line = item
        else:
            line = "data: " + json.dumps({"choices": [{"delta": {"content": item}}]})
        try:
            frame = json.loads(line[5:].strip())
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        if "event" in frame:
            events.append(frame["event"])
            continue
        choices = frame.get("choices") or [{}]
        piece = (choices[0].get("delta") or {}).get("content")
        if isinstance(piece, str):
            content.append(piece)
    return "".join(content)


def run_pipe(fake: FakeAgent | None, *, messages: list[dict[str, Any]] | None = None,
             user: dict[str, Any] | None = None, task: str | None = None,
             metadata: dict[str, Any] | None = None, body_extra: dict[str, Any] | None = None,
             pipe: docs_pipe.Pipe | None = None,
             transport: httpx.AsyncBaseTransport | None = None, stream: bool = True,
             ) -> tuple[str, list[dict[str, Any]]]:
    """Drive the Pipe as Open WebUI does; return (content, emitted events)."""
    pipe = pipe or docs_pipe.Pipe()
    if transport is not None:
        pipe._transport = transport
    elif fake is not None:
        pipe._transport = httpx.MockTransport(fake.handler)
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    body = {"model": "docs", "stream": stream,
            "messages": messages if messages is not None else _messages(("user", QUESTION)),
            **(body_extra or {})}

    async def main() -> str:
        result = await pipe.pipe(body, __user__=user or {"id": "u1"},
                                 __event_emitter__=emit, __task__=task,
                                 __metadata__=metadata or {})
        if isinstance(result, str):
            return result
        assert stream, "a non-streaming call must get one string, not a generator"
        return await owui_stream(result, events)

    return asyncio.run(main()), events


def _statuses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["data"] for e in events if e["type"] == "status"]


def _final_status(events: list[dict[str, Any]]) -> str:
    done = [s for s in _statuses(events) if s.get("done")]
    assert len(done) == 1, events
    return str(done[0]["description"])


def _sources(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["data"] for e in events if e["type"] == "source"]


# --- Request shape -----------------------------------------------------------


@pytest.fixture
def chat_request(monkeypatch) -> Any:
    """The agent's real strict ChatRequest model, with the settings its
    bounds validator reads (normally bound by the app lifespan)."""
    from mainframe_rag.agent import app as app_mod
    from mainframe_rag.config import load_settings

    monkeypatch.setattr(app_mod, "settings", load_settings(), raising=False)
    return app_mod.ChatRequest


def test_request_body_is_exactly_the_strict_chat_schema(chat_request):
    """Only fields ChatRequest accepts: user/assistant string turns, stream,
    nothing Open WebUI adds (system prompts, file parts, model, params)."""
    fake = FakeAgent()
    run_pipe(fake, messages=[
        {"role": "system", "content": "Open WebUI system prompt"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "assistant", "content": "   "},
        {"role": "user", "content": [{"type": "text", "text": "multimodal part"}]},
        {"role": "tool", "content": "tool output"},
        {"role": "user", "content": QUESTION, "files": [{"id": "f1"}]},
    ])
    (body,) = fake.bodies()
    assert body == {
        "messages": [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "multimodal part"},
            {"role": "user", "content": QUESTION},
        ],
        "stream": True,
    }
    # The agent's real strict model accepts it unchanged (extra="forbid").
    validated = chat_request.model_validate(body)
    assert validated.model_dump(exclude_unset=True) == body
    assert fake.requests[0].method == "POST"
    assert fake.requests[0].url == httpx.URL("http://rag-agent:8080/v1/chat")


# --- History trimming to the agent's body cap (#1072) ------------------------

CAP = 32768
TRIM_NOTE = "Earlier messages in this chat were not sent"


def _history(turns: int, size: int) -> list[dict[str, Any]]:
    """`turns` alternating user/assistant turns of `size` chars each, oldest
    first, starting with a user turn; each text is unique."""
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"{i:04d}" + "x" * (size - 4)} for i in range(turns)]


def _body_chars(body: dict[str, Any]) -> int:
    """The agent's own count (chat_body_chars), not a re-implementation."""
    from mainframe_rag.agent.app import ChatMessage
    from mainframe_rag.agent.chat_turn import chat_body_chars

    return chat_body_chars([ChatMessage(**m) for m in body["messages"]], None,
                           body.get("product"), body.get("version"))


def test_max_body_chars_valve_defaults_to_the_agents_default():
    from mainframe_rag.config import Settings

    assert docs_pipe.Pipe.Valves().MAX_BODY_CHARS == Settings.model_fields["chat_max_body_chars"].default


def test_long_history_is_trimmed_oldest_first_and_the_real_chat_request_accepts_it(chat_request):
    history = _history(8, 6000)  # 48000 chars: the agent would answer 422
    messages = [*history, {"role": "user", "content": QUESTION}]
    with pytest.raises(ValueError):
        chat_request.model_validate({"messages": [{"role": m["role"], "content": m["content"]}
                                                  for m in messages], "stream": True})
    fake = FakeAgent()
    _content, events = run_pipe(fake, messages=messages)
    (body,) = fake.bodies()
    assert _body_chars(body) <= CAP
    chat_request.model_validate(body)
    sent = [m["content"] for m in body["messages"]]
    # A suffix of the original turns: only the oldest were dropped, none reordered.
    assert sent == [m["content"] for m in messages][-len(sent):]
    assert len(sent) < len(messages)
    assert TRIM_NOTE in [s["description"] for s in _statuses(events)]


def test_trim_keeps_as_many_recent_turns_as_fit():
    fake = FakeAgent()
    run_pipe(fake, messages=[*_history(8, 6000), {"role": "user", "content": QUESTION}],
             user={"id": "u1", "valves": {"product": "z/OS", "version": "3.1"}})
    (body,) = fake.bodies()
    assert _body_chars(body) <= CAP
    # Dropping nothing more than needed: the next older turn would not have fit.
    older = 6000 + _body_chars(body)
    assert older > CAP or body["messages"][0]["role"] == "user"
    assert body["product"] == "z/OS" and body["version"] == "3.1"


def test_last_turn_is_always_sent_and_history_never_starts_with_an_assistant_turn():
    for size in range(5400, 6600, 137):  # every cut point lands somewhere different
        fake = FakeAgent()
        run_pipe(fake, messages=[*_history(9, size), {"role": "user", "content": QUESTION}])
        (body,) = fake.bodies()
        assert body["messages"][-1] == {"role": "user", "content": QUESTION}
        assert body["messages"][0]["role"] == "user", size
        assert _body_chars(body) <= CAP


def test_last_turn_alone_over_the_cap_is_a_fixed_message_with_zero_calls():
    fake = FakeAgent()
    secret = "q" * (CAP + 1)
    content, events = run_pipe(fake, messages=[
        *_history(2, 100), {"role": "user", "content": secret}])
    assert fake.requests == []
    assert content == docs_pipe.ERROR_MESSAGES["invalid_request"]
    assert "q" * 20 not in content
    assert TRIM_NOTE not in [s["description"] for s in _statuses(events)]


def test_product_and_version_count_against_the_cap():
    fake = FakeAgent()
    content, _ = run_pipe(fake, messages=[{"role": "user", "content": "q" * (CAP - 4)}],
                          user={"id": "u1", "valves": {"product": "z/OS", "version": "3.1"}})
    assert fake.requests == []
    assert content == docs_pipe.ERROR_MESSAGES["invalid_request"]


def test_exactly_at_the_cap_is_sent_whole_and_no_note_is_emitted():
    fake = FakeAgent()
    messages = [{"role": "user", "content": "a" * 100}, {"role": "assistant", "content": "b" * 100},
                {"role": "user", "content": "q" * (CAP - 200)}]
    _content, events = run_pipe(fake, messages=messages)
    (body,) = fake.bodies()
    assert len(body["messages"]) == 3 and _body_chars(body) == CAP
    assert TRIM_NOTE not in [s["description"] for s in _statuses(events)]


def test_short_chat_emits_no_trim_note():
    _content, events = run_pipe(FakeAgent())
    assert all(TRIM_NOTE not in s["description"] for s in _statuses(events))


def test_trim_note_is_fixed_text_without_content_and_survives_the_final_banner():
    _content, events = run_pipe(FakeAgent(), messages=[
        *_history(8, 6000), {"role": "user", "content": QUESTION}])
    notes = [s for s in _statuses(events) if TRIM_NOTE in s["description"]]
    assert notes
    for s in notes:
        assert "xxxx" not in s["description"] and QUESTION not in s["description"]
    assert TRIM_NOTE in _final_status(events)


def test_admin_valve_sets_the_cap():
    pipe = docs_pipe.Pipe()
    pipe.valves.MAX_BODY_CHARS = 2000
    fake = FakeAgent()
    run_pipe(fake, pipe=pipe, messages=[*_history(8, 600), {"role": "user", "content": QUESTION}])
    (body,) = fake.bodies()
    assert 0 < _body_chars(body) <= 2000 and len(body["messages"]) < 9
    with pytest.raises(ValueError):
        docs_pipe.Pipe.Valves(MAX_BODY_CHARS=0)



def test_no_string_turn_left_makes_no_agent_call():
    fake = FakeAgent()
    content, events = run_pipe(fake, messages=[
        {"role": "system", "content": "only a system prompt"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
    ])
    assert fake.requests == []
    assert content == "Ask a question in plain text; attachments and images are not supported."
    assert events == []


@pytest.mark.parametrize("last", [
    pytest.param({"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
                 id="image-only"),
    pytest.param({"role": "user", "content": [{"type": "text", "text": "  "}]}, id="blank-text"),
    pytest.param({"role": "assistant", "content": "an assistant turn last"}, id="assistant-last"),
])
def test_unusable_last_turn_never_falls_back_to_an_earlier_question(last: dict[str, Any]):
    """The question is the last message. Without usable text there, the Pipe
    must not send the history and let the agent answer an older question."""
    fake = FakeAgent()
    content, events = run_pipe(fake, messages=[
        {"role": "user", "content": "an earlier question"},
        {"role": "assistant", "content": "an earlier answer"},
        last,
    ])
    assert fake.requests == []
    assert content == "Ask a question in plain text; attachments and images are not supported."
    assert events == []


def test_text_parts_of_a_multimodal_last_turn_are_the_question():
    fake = FakeAgent()
    run_pipe(fake, messages=[
        {"role": "user", "content": "an earlier question"},
        {"role": "assistant", "content": "an earlier answer"},
        {"role": "user", "content": [{"type": "text", "text": "first line"},
                                     {"type": "image_url", "image_url": {"url": "x"}},
                                     {"type": "text", "text": "second line"}]},
    ])
    (body,) = fake.bodies()
    assert body["messages"][-1] == {"role": "user", "content": "first line\nsecond line"}


@pytest.mark.parametrize("valves", [
    pytest.param(lambda: docs_pipe.Pipe.UserValves(product="z/OS", version="3.1"), id="instance"),
    pytest.param(lambda: {"product": "z/OS", "version": "3.1"}, id="dict"),
])
def test_product_and_version_come_from_user_valves(valves: Callable[[], Any], chat_request):
    fake = FakeAgent()
    run_pipe(fake, user={"id": "u1", "valves": valves()})
    (body,) = fake.bodies()
    assert body["product"] == "z/OS"
    assert body["version"] == "3.1"
    assert chat_request.model_validate(body).product == "z/OS"


@pytest.mark.parametrize("user", [
    pytest.param({"id": "u1"}, id="no-valves"),
    pytest.param({"id": "u1", "valves": docs_pipe.Pipe.UserValves()}, id="defaults"),
    pytest.param({"id": "u1", "valves": {"product": "  ", "version": ""}}, id="blank"),
])
def test_unset_product_and_version_are_not_sent(user: dict[str, Any]):
    fake = FakeAgent()
    run_pipe(fake, user=user)
    (body,) = fake.bodies()
    assert "product" not in body
    assert "version" not in body


def test_only_product_set_sends_only_product():
    fake = FakeAgent()
    run_pipe(fake, user={"id": "u1", "valves": {"product": " z/OS ", "version": ""}})
    (body,) = fake.bodies()
    assert body["product"] == "z/OS"
    assert "version" not in body


def test_agent_url_valve_is_the_only_destination():
    fake = FakeAgent()
    pipe = docs_pipe.Pipe()
    pipe.valves = docs_pipe.Pipe.Valves(AGENT_URL="http://rag-agent.docs.svc:8080/")
    run_pipe(fake, pipe=pipe)
    assert [str(r.url) for r in fake.requests] == ["http://rag-agent.docs.svc:8080/v1/chat"]
    # No caller identity or Open WebUI headers are forwarded to the agent.
    sent = {k.lower() for k in fake.requests[0].headers}
    assert not {h for h in sent if h.startswith("x-openwebui") or h == "authorization"}


def test_agent_redirect_is_not_followed():
    """A redirect from the agent URL would send the question to another host."""
    fake = FakeAgent(status=302, headers={"location": "http://elsewhere.example/v1/chat"})
    content, events = run_pipe(fake)
    assert [str(r.url) for r in fake.requests] == ["http://rag-agent:8080/v1/chat"]
    assert content == UNREACHABLE
    assert _final_status(events) == "Request failed"


def test_client_ignores_proxy_environment_and_redirects(monkeypatch):
    """Proxy variables in the Open WebUI pod must not reroute the agent call;
    a custom transport hides env proxies, so the client options are pinned."""
    seen: list[dict[str, Any]] = []
    real = httpx.AsyncClient

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(docs_pipe.httpx, "AsyncClient", Recording)
    run_pipe(FakeAgent())
    assert len(seen) == 1
    assert seen[0]["trust_env"] is False
    assert seen[0]["follow_redirects"] is False


@pytest.mark.parametrize("marker", [
    pytest.param({"task": "title_generation"}, id="__task__"),
    pytest.param({"metadata": {"task": "tags_generation"}}, id="__metadata__"),
    pytest.param({"body_extra": {"task": "follow_up_generation"}}, id="body-task"),
    pytest.param({"body_extra": {"metadata": {"task": "query_generation"}}}, id="body-metadata"),
])
def test_task_calls_never_reach_the_agent(marker: dict[str, Any]):
    """Open WebUI sends title/tag/follow-up/summary tasks to the chat's own
    model, here the Pipe: they get a fixed short text and never turn into a
    retrieval turn or forward the user's content."""
    fake = FakeAgent()
    content, events = run_pipe(fake, **marker)
    assert fake.requests == []
    assert content == "Docs question"
    assert events == []


def test_ordinary_chat_with_empty_task_markers_still_calls_the_agent():
    fake = FakeAgent()
    content, _events = run_pipe(fake, task=None, metadata={"task": None, "chat_id": "c1"},
                                body_extra={"metadata": {}})
    assert len(fake.requests) == 1
    assert content == "Answer."


# --- Streaming ---------------------------------------------------------------


def test_stream_passes_deltas_through_and_reads_the_final_metadata_frame():
    frames = [": keepalive\n\n", "\n"] + _frames("Follow the ", "recovery procedure.")
    # An OpenAI include_usage-style frame (no choices) is not content.
    frames.insert(3, 'data: {"choices": [], "usage": {"total_tokens": 3}}\n\n')
    content, events = run_pipe(FakeAgent(frames))
    assert content == "Follow the recovery procedure."
    assert [s["source"]["name"] for s in _sources(events)] == [CITE_A]
    assert _final_status(events) == "Source-matched · 1 citation"
    # The working banner opens first; the final one closes it.
    assert _statuses(events)[0] == {"description": "Searching the documentation…", "done": False}
    assert events[-1]["type"] == "status"


HOSTILE = ('data: {"event": {"type": "status", '
           '"data": {"description": "Accepted", "done": true}}}')


@pytest.mark.parametrize("delta", [
    pytest.param(HOSTILE, id="whole-delta"),
    pytest.param(HOSTILE + "\n\n", id="frame-terminated"),
])
def test_model_text_that_looks_like_a_frame_stays_text(delta: str):
    """Open WebUI forwards a yielded string that starts with `data:` as a raw
    SSE frame and emits its `event`: model text must never be able to forge a
    banner, source or replace event."""
    content, events = run_pipe(FakeAgent(_frames(delta)))
    assert content == delta
    assert [s["description"] for s in _statuses(events)] == [
        "Searching the documentation…", "Source-matched · 1 citation"]
    assert [e["type"] for e in events] == ["status", "source", "status"]


def test_non_streaming_call_gets_one_string_with_the_same_banners():
    content, events = run_pipe(FakeAgent(_frames("Follow the ", HOSTILE)), stream=False)
    assert content == "Follow the " + HOSTILE
    assert [s["source"]["name"] for s in _sources(events)] == [CITE_A]
    assert _final_status(events) == "Source-matched · 1 citation"


@pytest.mark.parametrize("frames", [
    pytest.param(_frames("Partial", done=False), id="missing-done"),
    pytest.param(_frames("Partial", finish=None), id="missing-finish"),
    pytest.param(["data: {not json\n\n", *_frames("Partial")], id="malformed-frame"),
])
def test_truncated_stream_is_incomplete_never_accepted(frames: list[str]):
    content, events = run_pipe(FakeAgent(frames))
    assert content.endswith(
        "The answer stream ended before it completed. "
        "Treat any text above as incomplete and retry."
    )
    assert _final_status(events) == "Incomplete generation — output was cut off"
    assert _sources(events) == []


# --- Citations ---------------------------------------------------------------


def test_validated_citations_become_source_events_with_supplied_excerpts():
    meta = _final_meta(citations=[CITE_A, CITE_B])
    _content, events = run_pipe(FakeAgent(_frames("Answer.", meta=meta)))
    assert _sources(events) == [
        {
            "source": {"name": CITE_A, "id": "SA22-0000-00"},
            "document": [EXCERPT_A],
            "metadata": [{"source": CITE_A, "name": f"[1] {CITE_A}", "doc_id": "SA22-0000-00",
                          "page_label": "1-6", "product": "z/OS", "version": "3.1",
                          "label": "source-matched"}],
        },
        {
            "source": {"name": CITE_B, "id": "SA22-0001-00"},
            "document": [EXCERPT_B],
            "metadata": [{"source": CITE_B, "name": f"[2] {CITE_B}", "doc_id": "SA22-0001-00",
                          "page_label": "1-6", "product": "z/OS", "version": "3.1",
                          "label": "source-matched"}],
        },
    ]
    assert _final_status(events) == "Source-matched · 2 citations"


def test_supplied_but_uncited_excerpts_are_not_sources():
    """Only the agent's validated citations are sources, never every excerpt
    the prompt carried."""
    _content, events = run_pipe(FakeAgent(_frames("Answer.")))
    assert [s["source"]["name"] for s in _sources(events)] == [CITE_A]


@pytest.mark.parametrize("citation", [
    "https://evil.example/x, Title, p. 1",
    "HTTP://evil.example/x, Title, p. 1",
    "httpd Guide, Chapter 1, p. 2",
    "javascript:alert(1), Title, p. 1",
    "//evil.example/x, Title, p. 1",
    "www.evil.example, Title, p. 1",
])
def test_url_looking_citations_never_become_links(citation: str):
    """Open WebUI renders a source name or metadata source that starts with
    `http` as a link; every name the Pipe sets stays plain text."""
    meta = _final_meta(citations=[citation], supplied_evidence=[
        _excerpt(1, citation, EXCERPT_A, citation.split(",")[0])])
    _content, events = run_pipe(FakeAgent(_frames("Answer.", meta=meta)))
    (source,) = _sources(events)
    names = [source["source"]["name"], source["source"]["id"],
             *(m[k] for m in source["metadata"] for k in ("source", "name", "doc_id"))]
    assert all(n.startswith(("Citation: ", "[1] Citation: ")) for n in names), names
    assert source["source"]["name"] == "Citation: " + citation
    assert source["document"] == [EXCERPT_A]


def test_citation_without_supplied_text_says_so_without_inventing_text():
    meta = _final_meta(supplied_evidence=[])
    _content, events = run_pipe(FakeAgent(_frames("Answer.", meta=meta)))
    (source,) = _sources(events)
    assert source["document"] == ["Excerpt text is not available in this response."]
    assert source["metadata"] == [{"source": CITE_A, "name": CITE_A, "label": "source-matched"}]


def test_inferred_citations_are_labelled_not_grounding():
    meta = _final_meta(citations_inferred=True, inferred_indices=[1],
                       verification_state="unverified_draft")
    _content, events = run_pipe(FakeAgent(_frames("Answer [1].", meta=meta)))
    (source,) = _sources(events)
    assert source["metadata"][0]["label"] == "inferred, not grounding"
    assert _final_status(events) == (
        "Unverified draft — no eligible citation · "
        "Inferred citations — bracket provenance, not grounding"
    )


# --- Verification and abstention ---------------------------------------------


@pytest.mark.parametrize("state,expected", [
    ("accepted", "Source-matched · 1 citation"),
    ("insufficient_evidence", "Insufficient evidence — not operational guidance"),
    ("unverified_draft", "Unverified draft — no eligible citation"),
    ("generation_incomplete", "Incomplete generation — output was cut off"),
    (None, "Unverified — verification state unknown"),
    ("verified", "Unverified — verification state unknown"),
])
def test_each_verification_state_is_a_fixed_banner(state: str | None, expected: str):
    _content, events = run_pipe(FakeAgent(_frames("Answer.", meta=_final_meta(
        verification_state=state))))
    assert _final_status(events) == expected


def test_accepted_without_citations_is_never_shown_as_source_matched():
    meta = _final_meta(citations=[])
    _content, events = run_pipe(FakeAgent(_frames("Answer.", meta=meta)))
    assert _final_status(events) == "Unverified — verification state unknown"


def test_abstention_keeps_the_decline_text_and_shows_the_banner():
    """Empty-hits short circuit: the agent's decline text streams through,
    no sources, the insufficient-evidence banner."""
    decline = "The provided excerpts do not cover IEA500I."
    meta = _final_meta(citations=[], hits=[], supplied_evidence=[],
                       verification_state="insufficient_evidence")
    content, events = run_pipe(FakeAgent(_frames(decline, meta=meta)))
    assert content == decline
    assert _sources(events) == []
    assert _final_status(events) == "Insufficient evidence — not operational guidance"


def test_script_review_warning_rides_the_banner():
    meta = _final_meta(script="//JOB", script_lang="jcl", script_review_required=True)
    _content, events = run_pipe(FakeAgent(_frames("Answer.", meta=meta)))
    assert _final_status(events) == (
        "Source-matched · 1 citation · "
        "Human review required — draft script, not certified executable"
    )


# --- Errors ------------------------------------------------------------------

ERROR_CASES = [
    ("invalid_request", 422, ("The request was refused as invalid. Shorten or rephrase the question "
     "and retry.")),
    ("prompt_budget_exceeded", 422, ("The question and conversation are too long for the model. "
     "Start a new chat or shorten the question.")),
    ("upstream_error", 502, ("The documentation service could not complete this request. "
     "Retry in a moment.")),
    ("model_timeout", 504, "The reasoning model did not respond in time. Retry in a moment."),
    ("deadline_exceeded", 504, ("The request took longer than the service allows. "
     "Retry, or ask a narrower question.")),
    ("overloaded", 503, "The documentation service is at capacity. Retry in a moment."),
    ("not_configured", 503, ("The documentation service has no reasoning model configured. "
     "Contact the operators.")),
    ("representation_unavailable", 503, ("The documentation index is not available right now. "
     "Retry later.")),
    ("internal", 500, ("The documentation service hit an internal error. Retry; if it persists, "
     "contact the operators.")),
    ("not_found", 404, ("The documentation service rejected the request. Ask an administrator "
     "to check the Docs Pipe configuration.")),
    ("method_not_allowed", 405, ("The documentation service rejected the request. Ask an "
     "administrator to check the Docs Pipe configuration.")),
    ("http_error", 400, ("The documentation service rejected the request. Ask an administrator "
     "to check the Docs Pipe configuration.")),
]
UNREACHABLE = ("The documentation service could not be reached. Retry; if it persists, "
               "contact the operators.")


@pytest.mark.parametrize("code,status,expected", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
def test_each_error_envelope_code_is_a_fixed_message(code: str, status: int, expected: str):
    envelope = json.dumps({"code": code, "message": UPSTREAM_TEXT}).encode()
    content, events = run_pipe(FakeAgent(status=status, body=envelope))
    assert content == expected
    assert _final_status(events) == "Request failed"
    assert UPSTREAM_TEXT not in json.dumps(events)
    assert _sources(events) == []


@pytest.mark.parametrize("code,expected", [
    ("upstream_error", ERROR_CASES[2][2]),
    ("model_timeout", ERROR_CASES[3][2]),
    ("deadline_exceeded", ERROR_CASES[4][2]),
    ("prompt_budget_exceeded", ERROR_CASES[1][2]),
])
def test_in_stream_error_frame_is_a_fixed_message_and_incomplete(code: str, expected: str):
    frames = [format_openai_chunk("chatcmpl-x", "m", delta_content="partial "),
              format_openai_error(code, UPSTREAM_TEXT), format_openai_done()]
    content, events = run_pipe(FakeAgent(frames))
    # Provisional tokens stay visible but are closed by the fixed failure text.
    assert content == "partial \n\n" + expected
    assert UPSTREAM_TEXT not in content
    assert _final_status(events) == "Incomplete generation — output was cut off"
    assert _sources(events) == []


@pytest.mark.parametrize("fake", [
    pytest.param(FakeAgent(status=502, body=b"<html>" + UPSTREAM_TEXT.encode()), id="non-json"),
    pytest.param(FakeAgent(status=503, body=json.dumps(
        {"code": "brand_new_code", "message": UPSTREAM_TEXT}).encode()), id="unknown-code"),
    pytest.param(FakeAgent(status=502, body=json.dumps(
        {"code": ["upstream_error"], "message": UPSTREAM_TEXT}).encode()), id="code-not-string"),
    pytest.param(FakeAgent(raise_exc=httpx.ConnectError(UPSTREAM_TEXT)), id="connect-error"),
    pytest.param(FakeAgent(raise_exc=httpx.ReadTimeout(UPSTREAM_TEXT)), id="read-timeout"),
])
def test_unknown_or_transport_failures_never_show_upstream_text(fake: FakeAgent):
    content, events = run_pipe(fake)
    assert content == UNREACHABLE
    assert UPSTREAM_TEXT not in json.dumps(events)
    assert _final_status(events) == "Request failed"


def test_pipe_never_raises_into_open_webui(monkeypatch):
    """Open WebUI shows a raised exception's text to the user; the Pipe must
    turn any internal fault into the fixed message instead."""
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(UPSTREAM_TEXT)

    monkeypatch.setattr(docs_pipe, "_source_events", boom)
    content, events = run_pipe(FakeAgent())
    assert content.endswith(UNREACHABLE)
    assert UPSTREAM_TEXT not in content + json.dumps(events)


# --- Privacy and pins --------------------------------------------------------


def test_no_user_content_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    run_pipe(FakeAgent(_frames("Answer about IEA500I.")))
    run_pipe(FakeAgent(status=502, body=b'{"code":"upstream_error","message":"x"}'))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert QUESTION not in logged
    assert EXCERPT_A not in logged
    assert "Answer about" not in logged


def test_open_webui_pin_and_install_header():
    """The admin-installed file pins its target Open WebUI version and asks
    Open WebUI for no package installs (no runtime downloads)."""
    source = Path(docs_pipe.__file__).read_text(encoding="utf-8")
    header = source.split('"""', 2)[1]
    fields = dict(re.findall(r"^(\w+):\s*(.+)$", header, flags=re.MULTILINE))
    assert docs_pipe.OPEN_WEBUI_VERSION == "0.12.3"
    assert fields["required_open_webui_version"] == docs_pipe.OPEN_WEBUI_VERSION
    assert fields["title"] == "Docs"
    assert "requirements" not in fields
    imports = set(re.findall(r"^(?:from|import) (\w+)", source, flags=re.MULTILINE))
    assert imports <= {"__future__", "collections", "json", "re", "typing", "httpx", "pydantic"}


# --- Real agent round trip ---------------------------------------------------


@pytest.fixture
def real_agent(monkeypatch, servable_representation_gate):
    """The real agent app (strict ChatRequest, real chat frames) with faked
    retrieval and reasoning, reached through a transport that forwards the
    Pipe's request unchanged."""
    from fastapi.testclient import TestClient

    from mainframe_rag.agent import app as app_mod
    from mainframe_rag.agent.tokenizer import FallbackTokenizer
    from mainframe_rag.ports import TokenUsage
    from mainframe_rag.retrieve.query import SearchHit

    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    searches: list[dict[str, Any]] = []

    def search(_qdrant, _embedder, _collection, query, product=None, version=None,
               *args: Any, **kwargs: Any) -> Any:
        searches.append({"query": query, "product": product, "version": version})
        hit = SearchHit(chunk_id="abc123", score=0.42, cite=CITE_A, heading="Chapter 2 > IEA500I",
                        text=EXCERPT_A, doc_id="SA22-0000-00", title="Synthetic Reference",
                        page_label="1-6", chunk_type="message", product="z/OS", version="3.1",
                        message_ids=("IEA500I",))
        return [hit], "identifier", {"embed_ms": 1, "qdrant_ms": 2}

    class FakeLLM:
        async def chat_stream(self, messages, reasoning_effort=None, temperature=None):
            yield {"type": "token", "delta": "Reissue the command.\n\n", "ttft_ms": 3}
            yield {"type": "token", "delta": f"Citations:\n- {CITE_A}\n"}
            yield {"type": "done", "finish_reason": "stop", "ttft_ms": 3,
                   "usage": TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)}

    monkeypatch.setattr(app_mod, "retrieve_search", search)
    with TestClient(app_mod.app) as client:
        monkeypatch.setattr(app_mod, "llm", FakeLLM())
        monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())

        def forward(request: httpx.Request) -> httpx.Response:
            resp = client.post(request.url.path, content=request.content,
                               headers={"content-type": "application/json"})
            return httpx.Response(resp.status_code, content=resp.content,
                                  headers={"content-type": resp.headers["content-type"]})

        yield httpx.MockTransport(forward), searches


def test_round_trip_through_the_real_agent(real_agent):
    transport, searches = real_agent
    content, events = run_pipe(None, transport=transport,
                               user={"id": "u1", "valves": {"product": "z/OS", "version": "3.1"}})
    assert searches == [{"query": QUESTION, "product": "z/OS", "version": "3.1"}]
    assert content.startswith("Reissue the command.")
    (source,) = _sources(events)
    assert source["source"] == {"name": CITE_A, "id": "SA22-0000-00"}
    assert source["document"] == [EXCERPT_A]
    assert _final_status(events) == "Source-matched · 1 citation"


def test_round_trip_asks_the_last_multimodal_turn_not_an_earlier_one(real_agent):
    """Open WebUI sends list content when an image is attached: the agent must
    retrieve for that last turn's text, never for the earlier string turn."""
    transport, searches = real_agent
    run_pipe(None, transport=transport, messages=[
        {"role": "user", "content": "What is IEA499I?"},
        {"role": "assistant", "content": "IEA499I is a different message."},
        {"role": "user", "content": [{"type": "text", "text": QUESTION},
                                     {"type": "image_url", "image_url": {"url": "data:x"}}]},
    ])
    assert [s["query"] for s in searches] == [QUESTION]


def test_round_trip_a_long_chat_is_200_where_the_untrimmed_body_is_422(real_agent):
    transport, searches = real_agent
    messages = [*_history(8, 6000), {"role": "user", "content": QUESTION}]
    untrimmed = httpx.Client(transport=transport, base_url="http://rag-agent:8080").post(
        "/v1/chat", json={"messages": [{"role": m["role"], "content": m["content"]}
                                       for m in messages], "stream": True})
    assert untrimmed.status_code == 422 and searches == []
    content, events = run_pipe(None, transport=transport, messages=messages)
    assert [s["query"] for s in searches] == [QUESTION]
    assert content.startswith("Reissue the command.")
    assert _final_status(events).endswith(TRIM_NOTE)


def test_round_trip_maps_the_real_strict_schema_refusal(real_agent):
    """A product longer than the agent's bound is refused by the real model
    with invalid_request; the Pipe shows the fixed text."""
    transport, searches = real_agent
    content, _events = run_pipe(None, transport=transport,
                                user={"id": "u1", "valves": {"product": "z" * 100_000}})
    assert searches == []
    assert content == ERROR_CASES[0][2]
