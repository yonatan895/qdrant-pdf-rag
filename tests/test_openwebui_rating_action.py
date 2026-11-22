"""Open WebUI rating Action (#1255): thumbs up/down on a Rosetta answer, stored
only by the agent's feedback store (ADR-0005), never in Open WebUI's database.

The Action runs inside Open WebUI, so these tests drive it through the
interface Open WebUI v0.12.3 calls (`utils/actions.py chat_action`: `body` as
the browser builds it in `Chat.svelte chatActionHandler`, plus the declared
reserved arguments), with no Open WebUI installed. Expected user-visible texts
are spelled out here, not read back from the Action's tables.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from integrations.openwebui import docs_pipe, rating_action

from mainframe_rag.feedback.store import COMMENT_MAX_CHARS, Rating

ROOT = Path(__file__).resolve().parents[1]
RID = "0123456789ab"
ANSWER = "Reissue the command.\n\n**Citations:**\n- SA22-0000-00 Synthetic Reference, Chapter 2, p. 1-6"
REFERENCED = ANSWER + f"\n\nAnswer reference: `{RID}`"
UPSTREAM_TEXT = "Traceback: secret-token-123 at feedback.internal"

RECORDED = "Thanks, your rating was recorded."
NOT_RATEABLE = "This answer cannot be rated: it has no answer reference."
UNKNOWN = "This answer is not recorded yet, or no longer; it cannot be rated now."
INVALID = "The rating was refused. Shorten the reason and retry."
DISABLED = "Ratings are not enabled on this deployment."
UNAVAILABLE = "Ratings are temporarily unavailable. Retry later."
TOO_LONG = "The reason is too long: at most 2000 characters."


class FakeAgent:
    def __init__(self, status: int = 201, body: Any = None, raise_exc: Exception | None = None):
        self.status = status
        self.body = {"status": "recorded"} if body is None else body
        self.raise_exc = raise_exc
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        content = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return httpx.Response(self.status, content=content,
                              headers={"content-type": "application/json"})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def _body(content: Any = REFERENCED, *, message_id: str = "m2", last_id: str = "m2",
          role: str = "assistant") -> dict[str, Any]:
    """The browser's action body (Chat.svelte chatActionHandler, v0.12.3)."""
    return {
        "model": "mainframe_rag_docs",
        "messages": [
            {"id": "m1", "role": "user", "content": "How do I recover from IEA500I?",
             "timestamp": 1},
            {"id": last_id, "role": role, "content": content, "timestamp": 2},
        ],
        "chat_id": "c1",
        "session_id": "s1",
        "id": message_id,
    }


def run_action(fake: FakeAgent | None, action_id: str | None, body: dict[str, Any] | None = None,
               reply: Any = "", *, transport: httpx.AsyncBaseTransport | None = None,
               with_call: bool = True) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """Call the Action as Open WebUI does; return (result, emitted, asked)."""
    action = rating_action.Action()
    action._transport = transport or (httpx.MockTransport(fake.handler) if fake else None)
    emitted: list[dict[str, Any]] = []
    asked: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    async def call(event: dict[str, Any]) -> Any:
        asked.append(event)
        return reply

    result = asyncio.run(action.action(
        body if body is not None else _body(), __id__=action_id, __event_emitter__=emit,
        __event_call__=call if with_call else None))
    return result, emitted, asked


def _notices(emitted: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(e["data"]["type"], e["data"]["content"]) for e in emitted]


# --- What Open WebUI may hand the Action, and what it keeps -------------------


def test_action_declares_no_token_request_or_user_argument():
    """ADR-0007 V2: a Function that declares `__oauth_token__` receives the
    user's tokens, `__request__` reaches the app state, `__user__` the user
    record. The Action declares only what it uses; a new one is a decision."""
    params = list(inspect.signature(rating_action.Action.action).parameters)
    assert params == ["self", "body", "__id__", "__event_emitter__", "__event_call__"]
    source = Path(rating_action.__file__).read_text(encoding="utf-8")
    for reserved in ("__oauth_token__", "__request__", "__user__", "__metadata__", "__model__"):
        assert reserved not in source


@pytest.mark.parametrize("action_id,rating", [("up", "good"), ("down", "bad")])
def test_a_click_is_one_rating_post_and_a_toast_never_chat_state(action_id, rating):
    """Open WebUI saves `status`, `message`, `replace`, `embeds`, `files` and
    `source` events to the chat, and merges a returned `messages` into it:
    the Action emits only a toast (`notification`, not saved) and returns
    None, so nothing about the rating lands in Open WebUI's database."""
    fake = FakeAgent()
    result, emitted, _ = run_action(fake, action_id)
    assert result is None
    assert fake.bodies() == [{"request_id": RID, "rating": rating, "comment": ""}]
    [request] = fake.requests
    assert str(request.url) == "http://rag-agent:8080/v1/feedback"
    assert "authorization" not in request.headers  # anonymous, like the Pipe's /v1/chat
    assert _notices(emitted) == [("success", RECORDED)]
    assert {e["type"] for e in emitted} == {"notification"}


def test_thumbs_up_asks_nothing_and_thumbs_down_asks_for_an_optional_reason():
    _, _, asked = run_action(FakeAgent(), "up")
    assert asked == []
    fake = FakeAgent()
    _, emitted, asked = run_action(fake, "down", reply="  The page number is wrong.  ")
    [question] = asked
    assert question["type"] == "input"
    assert set(question["data"]) == {"title", "message", "placeholder"}
    assert fake.bodies()[0]["comment"] == "The page number is wrong."
    assert _notices(emitted) == [("success", RECORDED)]


def test_an_empty_reason_still_rates():
    fake = FakeAgent()
    run_action(fake, "down", reply="   ")
    assert fake.bodies() == [{"request_id": RID, "rating": "bad", "comment": ""}]


@pytest.mark.parametrize("reply", [False, None, {"error": "Client session disconnected."}, 7])
def test_a_cancelled_or_failed_reason_dialog_rates_nothing(reply):
    fake = FakeAgent()
    result, emitted, _ = run_action(fake, "down", reply=reply)
    assert (result, fake.requests, emitted) == (None, [], [])


def test_thumbs_down_without_an_event_call_rates_without_a_reason():
    fake = FakeAgent()
    run_action(fake, "down", with_call=False)
    assert fake.bodies() == [{"request_id": RID, "rating": "bad", "comment": ""}]


def test_an_over_long_reason_is_refused_before_any_call():
    fake = FakeAgent()
    _, emitted, _ = run_action(fake, "down", reply="x" * (COMMENT_MAX_CHARS + 1))
    assert fake.requests == []
    assert _notices(emitted) == [("error", TOO_LONG)]
    run_action(fake, "down", reply="x" * COMMENT_MAX_CHARS)
    assert len(fake.requests) == 1


@pytest.mark.parametrize("body", [
    pytest.param(_body(ANSWER), id="no-reference"),
    pytest.param(_body(REFERENCED + "\n\nEdited after."), id="reference-not-last"),
    pytest.param(_body(ANSWER + "\n\nAnswer reference: `0123456789AB`"), id="upper-hex"),
    pytest.param(_body(REFERENCED, message_id="m9"), id="clicked-is-not-last"),
    pytest.param(_body(REFERENCED, role="user"), id="user-turn"),
    pytest.param(_body([{"type": "text", "text": REFERENCED}]), id="list-content"),
    pytest.param({"id": "m2", "messages": "x"}, id="no-message-list"),
    pytest.param({"id": "m2", "messages": []}, id="empty-messages"),
])
def test_an_answer_without_its_own_reference_is_not_rated(body):
    fake = FakeAgent()
    result, emitted, asked = run_action(fake, "down", body)
    assert (result, fake.requests, asked) == (None, [], [])
    assert _notices(emitted) == [("error", NOT_RATEABLE)]


@pytest.mark.parametrize("action_id", [None, "", "sideways", "mainframe_rag_rating"])
def test_an_unknown_sub_action_rates_nothing(action_id):
    fake = FakeAgent()
    _, emitted, _ = run_action(fake, action_id)
    assert fake.requests == []
    assert _notices(emitted) == [("error", UNAVAILABLE)]


def test_reference_trailing_whitespace_is_tolerated():
    fake = FakeAgent()
    run_action(fake, "up", _body(REFERENCED + "\n  "))
    assert fake.bodies()[0]["request_id"] == RID


# --- Outcomes: fixed texts only ------------------------------------------------


@pytest.mark.parametrize("status,body,text", [
    (404, {"code": "unknown_answer", "message": UPSTREAM_TEXT}, UNKNOWN),
    (422, {"code": "invalid_request", "message": UPSTREAM_TEXT}, INVALID),
    (404, {"code": "feedback_disabled", "message": UPSTREAM_TEXT}, DISABLED),
    (503, {"code": "feedback_unavailable", "message": UPSTREAM_TEXT}, UNAVAILABLE),
    (404, {"code": "not_found", "message": UPSTREAM_TEXT}, UNAVAILABLE),
    (500, {"code": "internal", "message": UPSTREAM_TEXT}, UNAVAILABLE),
    (200, {"status": "recorded"}, UNAVAILABLE),
    (503, UPSTREAM_TEXT.encode(), UNAVAILABLE),
    (404, [UPSTREAM_TEXT], UNAVAILABLE),
])
def test_agent_outcomes_map_to_fixed_texts(status, body, text):
    _, emitted, _ = run_action(FakeAgent(status, body), "up")
    assert _notices(emitted) == [("error", text)]
    assert UPSTREAM_TEXT not in json.dumps(emitted)


@pytest.mark.parametrize("exc", [httpx.ConnectError(UPSTREAM_TEXT), RuntimeError(UPSTREAM_TEXT)])
def test_a_failure_never_raises_into_open_webui(exc):
    """Open WebUI answers a raised exception with its text as a 400, which the
    browser writes into the message's error and saves: never raise."""
    result, emitted, _ = run_action(FakeAgent(raise_exc=exc), "up")
    assert result is None
    assert _notices(emitted) == [("error", UNAVAILABLE)]
    assert UPSTREAM_TEXT not in json.dumps(emitted)


def test_a_failing_emitter_is_absorbed():
    action = rating_action.Action()
    action._transport = httpx.MockTransport(FakeAgent().handler)

    async def broken(event: dict[str, Any]) -> None:
        raise RuntimeError("socket closed")

    assert asyncio.run(action.action(_body(), __id__="up", __event_emitter__=broken,
                                     __event_call__=None)) is None


# --- Pins between the Action, the Pipe, the store and the chart ----------------


def test_reference_pattern_and_limits_match_the_pipe_and_the_store():
    assert rating_action.REFERENCE_RE.pattern == docs_pipe.REFERENCE_RE.pattern
    line = docs_pipe.REFERENCE_LINE.format(request_id=RID)
    assert rating_action.REFERENCE_RE.search(ANSWER + line).group(1) == RID
    assert rating_action.REASON_MAX_CHARS == COMMENT_MAX_CHARS
    for value in rating_action.RATINGS.values():
        Rating(request_id=RID, rating=value)  # the store's vocabulary


def test_open_webui_pin_and_install_header():
    source = Path(rating_action.__file__).read_text(encoding="utf-8")
    header = source.split('"""', 2)[1]
    fields = dict(re.findall(r"^(\w+):\s*(.+)$", header, flags=re.MULTILINE))
    assert rating_action.OPEN_WEBUI_VERSION == "0.13.4" == fields["required_open_webui_version"]
    assert "requirements" not in fields
    imports = set(re.findall(r"^(?:from|import) (\w+)", source, flags=re.MULTILINE))
    assert imports <= {"__future__", "collections", "contextlib", "json", "re", "typing", "httpx",
                       "pydantic"}
    # Two message buttons with inline icons: no remote asset (ADR-0007 R2).
    assert [a["id"] for a in rating_action.Action.actions] == ["up", "down"]
    for item in rating_action.Action.actions:
        assert item["icon_url"].startswith("data:image/svg+xml;base64,")


def test_chart_ships_the_repository_action_unchanged():
    chart = ROOT / "charts" / "mainframe-rag" / "files" / "openwebui" / "rating_action.py"
    assert chart.read_bytes() == Path(rating_action.__file__).read_bytes()


def test_no_reason_or_answer_text_is_logged(caplog):
    caplog.set_level("DEBUG")
    run_action(FakeAgent(), "down", reply="SYNTHETIC REASON")
    run_action(FakeAgent(503, {"code": "feedback_unavailable"}), "down", reply="SYNTHETIC REASON")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "SYNTHETIC REASON" not in logged and "Reissue the command" not in logged


# --- Real agent round trip ------------------------------------------------------


def test_pipe_reference_rates_the_recorded_answer_through_the_real_agent(
        monkeypatch, synthetic_pdf, servable_representation_gate):
    """Producer to consumer: the Pipe's answer through the real /v1/chat, the
    reference line Open WebUI keeps, the Action's click, the real /v1/feedback
    and the feedback client. The rating names exactly the request id of the
    answer record the agent handed to the store."""
    from mainframe_rag.agent import app as app_mod
    from tests.test_feedback_agent import RecordingFeedback
    from tests.test_webui import _client

    monkeypatch.setenv("FEEDBACK_ENABLED", "true")
    monkeypatch.setenv("FEEDBACK_URL", "http://feedback.internal")
    monkeypatch.setenv("FEEDBACK_API_KEY", "feedback-test-token")
    monkeypatch.setenv("RAG_ANSWER_REFERENCE", "true")
    RecordingFeedback.instances = []
    monkeypatch.setattr(app_mod, "FeedbackClient", RecordingFeedback)
    for client in _client(monkeypatch, ui_enabled=False, synthetic_pdf=synthetic_pdf):
        def forward(request: httpx.Request, client: Any = client) -> httpx.Response:
            resp = client.post(request.url.path, content=request.content,
                               headers={"content-type": "application/json"})
            return httpx.Response(resp.status_code, content=resp.content,
                                  headers={"content-type": resp.headers["content-type"]})

        transport = httpx.MockTransport(forward)
        pipe = docs_pipe.Pipe()
        pipe._transport = transport

        async def answer(pipe: docs_pipe.Pipe = pipe) -> str:
            result = await pipe.pipe({"model": "docs", "stream": False, "messages": [
                {"role": "user", "content": "IEA500I"}]}, __event_emitter__=None)
            assert isinstance(result, str)
            return result

        content = asyncio.run(answer())
        [feedback] = RecordingFeedback.instances
        [record] = feedback.records
        assert content.endswith(f"\n\nAnswer reference: `{record.request_id}`")
        _, emitted, _ = run_action(None, "down", _body(content), reply="SYNTHETIC",
                                   transport=transport)
        assert _notices(emitted) == [("success", RECORDED)]
        [rating] = feedback.ratings
        assert (rating.request_id, rating.rating, rating.comment) == (
            record.request_id, "bad", "SYNTHETIC")
