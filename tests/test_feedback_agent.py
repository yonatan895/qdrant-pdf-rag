"""Agent-side answer feedback (issue #780): one record per finalized answer on
every path, nothing when disabled, never on the answer's critical path, and
the client's queue/delivery/rating outcomes."""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

import httpx2
import pytest

from mainframe_rag.agent import app as app_mod
from mainframe_rag.config import Settings
from mainframe_rag.feedback.client import FeedbackClient
from mainframe_rag.feedback.store import REQUEST_ID_RE, AnswerRecord, Rating
from tests.fakes import parse_sse_events as _parse_sse_events
from tests.test_webui import _client

QUERY = "IEA500I"


class RecordingFeedback:
    """FeedbackClient double: records what the agent hands over."""

    instances: ClassVar[list[RecordingFeedback]] = []

    def __init__(self, settings):
        self.records: list[AnswerRecord] = []
        self.ratings: list[Rating] = []
        self.outcome = "ok"
        RecordingFeedback.instances.append(self)

    def start(self):
        pass

    def submit(self, record):
        self.records.append(record)

    async def rate(self, rating):
        self.ratings.append(rating)
        return self.outcome

    async def aclose(self, drain_timeout_s=2.0):
        pass


@pytest.fixture
def feedback_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    monkeypatch.setenv("FEEDBACK_ENABLED", "true")
    monkeypatch.setenv("FEEDBACK_URL", "http://feedback.internal")
    monkeypatch.setenv("FEEDBACK_API_KEY", "feedback-test-token")
    RecordingFeedback.instances = []
    monkeypatch.setattr(app_mod, "FeedbackClient", RecordingFeedback)
    yield from _client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf)


def _records():
    [instance] = RecordingFeedback.instances
    return instance.records


@pytest.mark.parametrize("endpoint,call", [
    ("answer", lambda c: c.post("/v1/answer", json={"query": QUERY})),
    ("answer_stream", lambda c: c.post("/v1/answer?stream=true", json={"query": QUERY})),
    ("chat", lambda c: c.post("/v1/chat", json={"messages": [{"role": "user", "content": QUERY}]})),
    ("chat_stream", lambda c: c.post("/v1/chat", json={"messages": [{"role": "user", "content": QUERY}],
                                                         "stream": True})),
    ("console", lambda c: c.post("/ui/chat/stream", json={"messages": [{"role": "user", "content": QUERY}]})),
    ("console", lambda c: c.post("/ui/chat", data={"message": QUERY, "messages": ""},
                                 headers={"HX-Request": "true"})),
])
def test_every_finalized_answer_is_recorded_exactly_once(feedback_client, endpoint, call):
    response = call(feedback_client)
    assert response.status_code == 200
    [record] = _records()
    assert record.endpoint == endpoint
    assert record.query == QUERY
    assert REQUEST_ID_RE.fullmatch(record.request_id)
    assert record.answer
    telemetry = record.telemetry
    assert telemetry["verification_state"] in ("accepted", "unverified_draft", "insufficient_evidence",
                                               "generation_incomplete")
    assert {"prompt_tokens", "budget_verified", "units_omitted", "llm_model", "reasoning_effort"} <= set(telemetry)
    assert all(v is None or isinstance(v, (bool, int, float, str)) for v in telemetry.values())
    if endpoint == "answer":
        assert record.request_id == response.json()["request_id"]
    if endpoint == "console" and "stream" in response.request.url.path:
        [final] = [p for n, p in _parse_sse_events(response.text) if n == "final"]
        assert record.request_id == final["request_id"]


def test_disabled_feedback_builds_no_client_and_records_nothing(monkeypatch, synthetic_pdf,
                                                                 servable_representation_gate):
    monkeypatch.delenv("FEEDBACK_ENABLED", raising=False)

    def refuse(*_a, **_k):
        raise AssertionError("no feedback client may exist when FEEDBACK_ENABLED is off")

    monkeypatch.setattr(app_mod, "FeedbackClient", refuse)
    for client in _client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf):
        assert app_mod.feedback is None
        assert client.post("/v1/answer", json={"query": QUERY}).status_code == 200


def test_a_failing_feedback_client_never_changes_the_answer(feedback_client, monkeypatch):
    baseline = feedback_client.post("/v1/answer", json={"query": QUERY}).json()

    def explode(record):
        raise RuntimeError("feedback store down")

    monkeypatch.setattr(RecordingFeedback.instances[0], "submit", explode)
    again = feedback_client.post("/v1/answer", json={"query": QUERY})
    assert again.status_code == 200
    assert again.json()["answer"] == baseline["answer"]
    assert again.json()["verification_state"] == baseline["verification_state"]


def test_enabled_feedback_requires_its_endpoint_and_token():
    with pytest.raises(ValueError, match="FEEDBACK_URL and FEEDBACK_API_KEY"):
        Settings(_env_file=None, feedback_enabled=True, feedback_url="http://feedback.internal")


def _settings(**overrides):
    values = {"feedback_url": "http://feedback.internal", "feedback_api_key": "feedback-test-token",
              **overrides}
    return Settings(_env_file=None, feedback_enabled=True, **values)


def _record(rid="0123456789ab"):
    return AnswerRecord(request_id=rid, endpoint="answer", query="q", answer="a")


@pytest.mark.anyio
async def test_full_queue_drops_without_blocking_and_delivery_failures_are_absorbed(monkeypatch):
    outcomes = []
    monkeypatch.setattr("mainframe_rag.feedback.client.record_feedback_record", outcomes.append)
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers["authorization"]))
        return httpx2.Response(500)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = FeedbackClient(_settings(feedback_queue_size=1), client=http)
        client.submit(_record("aaaaaaaaaaaa"))
        client.submit(_record("bbbbbbbbbbbb"))  # queue of one is full: dropped, not awaited
        assert outcomes == ["dropped"]
        client.start()
        await asyncio.wait_for(client._queue.join(), 2)
        await client.aclose()
    assert outcomes == ["dropped", "failed"]
    assert seen == [("/v1/answers", "Bearer feedback-test-token")]


@pytest.mark.anyio
@pytest.mark.parametrize("status,body,outcome", [
    (201, {"id": 1, "endpoint": "console", "verification_state": "accepted"}, "ok"),
    (404, {"code": "unknown_answer"}, "unknown"),
    (422, {"code": "invalid_request"}, "invalid"),
    (503, {}, "unavailable"),
])
async def test_rating_outcomes_are_fixed_and_counted_only_when_accepted(monkeypatch, status, body, outcome):
    counted = []
    monkeypatch.setattr("mainframe_rag.feedback.client.record_feedback_rating",
                        lambda *a, **k: counted.append((a, k)))
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda request: httpx2.Response(status, json=body))) as http:
        client = FeedbackClient(_settings(), client=http)
        result = await client.rate(Rating(request_id="0123456789ab", rating="bad", reasons=["wrong"]))
        await client.aclose()
    assert result == outcome
    if outcome == "ok":
        assert counted == [(("bad", ["wrong"]), {"endpoint": "console", "verification_state": "accepted"})]
    else:
        assert counted == []


@pytest.mark.anyio
@pytest.mark.parametrize("status,code", [(401, "unauthorized"), (405, "method_not_allowed"),
                                         (500, "internal_error"), (404, "not_found")])
async def test_unexpected_rating_status_is_unavailable_and_logged_by_status(caplog, status, code):
    """Only the service's unknown_answer 404 means "not recorded"; a route 404
    (a wrong FEEDBACK_URL prefix) is a misconfiguration and must be visible."""
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda request: httpx2.Response(status, json={"code": code,
                                                          "message": "SYNTHETIC-UPSTREAM"}))) as http:
        client = FeedbackClient(_settings(), client=http)
        with caplog.at_level("WARNING", logger="feedback"):
            result = await client.rate(Rating(request_id=RID, rating="bad", comment="SYNTHETIC-COMMENT"))
        await client.aclose()
    assert result == "unavailable"
    [line] = [json.loads(r.getMessage()) for r in caplog.records]
    assert (line["action"], line["error"], line["status"]) == ("feedback_rating", "status", status)
    assert "SYNTHETIC" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("body", [b"", b"<html>proxy</html>", b"[1, 2]", b"null"])
async def test_accepted_rating_with_an_odd_body_is_still_ok(monkeypatch, body):
    """A 201 means the service stored the rating; an unparseable body only
    loses the metric labels, never turns a stored rating into a 500."""
    counted = []
    monkeypatch.setattr("mainframe_rag.feedback.client.record_feedback_rating",
                        lambda *a, **k: counted.append(k))
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda request: httpx2.Response(201, content=body))) as http:
        client = FeedbackClient(_settings(), client=http)
        assert await client.rate(Rating(request_id=RID, rating="good")) == "ok"
        await client.aclose()
    assert counted == [{"endpoint": "unknown", "verification_state": None}]  # bounded fallback labels


@pytest.mark.anyio
@pytest.mark.parametrize("body,outcome", [(b"", "recorded"), (b'{"recorded": true}', "recorded"),
                                          (b'{"recorded": false}', "duplicate"), (b"[]", "recorded")])
async def test_delivered_record_is_labelled_by_the_body_only_when_it_says_so(monkeypatch, body, outcome):
    outcomes = []
    monkeypatch.setattr("mainframe_rag.feedback.client.record_feedback_record", outcomes.append)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda request: httpx2.Response(201, content=body))) as http:
        client = FeedbackClient(_settings(), client=http)
        client.start()
        client.submit(_record())
        await asyncio.wait_for(client._queue.join(), 2)
        await client.aclose()
    assert outcomes == [outcome]


@pytest.mark.anyio
async def test_unreachable_service_is_unavailable_and_logs_no_text(caplog):
    def handler(request):
        raise httpx2.ConnectError("refused", request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = FeedbackClient(_settings(), client=http)
        with caplog.at_level("WARNING", logger="feedback"):
            result = await client.rate(Rating(request_id="0123456789ab", rating="bad",
                                              comment="SYNTHETIC-COMMENT-TEXT"))
        await client.aclose()
    assert result == "unavailable"
    assert "SYNTHETIC-COMMENT-TEXT" not in caplog.text
    assert json.loads(caplog.records[-1].getMessage())["error"] == "ConnectError"


RID = "0123456789ab"


@pytest.mark.parametrize("outcome,status,code", [
    ("ok", 201, None),
    ("unknown", 404, "unknown_answer"),
    ("invalid", 422, "invalid_request"),
    ("unavailable", 503, "feedback_unavailable"),
])
def test_console_rating_maps_store_outcomes_to_fixed_responses(feedback_client, outcome, status, code):
    RecordingFeedback.instances[0].outcome = outcome
    response = feedback_client.post("/ui/feedback", json={
        "request_id": RID, "rating": "bad", "reasons": ["incomplete"], "comment": "SYNTHETIC"})
    assert response.status_code == status
    if code is not None:
        assert response.json()["code"] == code
    [rating] = RecordingFeedback.instances[0].ratings
    assert (rating.request_id, rating.rating, rating.reasons, rating.comment) == (
        RID, "bad", ["incomplete"], "SYNTHETIC")


def test_rater_is_never_taken_from_a_forwarded_header(feedback_client):
    """The agent port is reachable without the OAuth proxy, so a forwarded
    user header proves nothing: no rater is recorded (deferred to PR B)."""
    feedback_client.post("/ui/feedback", json={"request_id": RID, "rating": "good"},
                         headers={"X-Forwarded-User": "operator-a"})
    forwarded = RecordingFeedback.instances[0].ratings[-1].model_dump()
    assert "rater" not in forwarded and "operator-a" not in json.dumps(forwarded)


@pytest.mark.parametrize("route", ["json", "form"])
def test_duplicate_reasons_are_a_fixed_422_never_a_500(feedback_client, route):
    if route == "json":
        response = feedback_client.post("/ui/feedback", json={
            "request_id": RID, "rating": "bad", "reasons": ["wrong", "wrong"]})
        assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    else:
        response = feedback_client.post("/ui/feedback/form", data={
            "request_id": RID, "rating": "bad", "reasons": ["wrong", "wrong"]},
            headers={"HX-Request": "true"})
        assert response.status_code == 200 and "the rating is invalid" in response.text
    assert RecordingFeedback.instances[0].ratings == []


@pytest.mark.parametrize("outcome,text", [
    ("unknown", "not recorded, or not yet"),
    ("unavailable", "feedback is temporarily unavailable"),
])
def test_htmx_rating_failures_are_swappable_and_say_why(feedback_client, outcome, text):
    """htmx 1.9 swaps only 2xx/3xx (not 204): a failure fragment must be 200,
    exactly like the /ui/chat banner, or the operator sees nothing."""
    RecordingFeedback.instances[0].outcome = outcome
    response = feedback_client.post("/ui/feedback/form", data={"request_id": RID, "rating": "bad"},
                                    headers={"HX-Request": "true"})
    assert response.status_code == 200 and text in response.text
    assert "<html" not in response.text and "<div" not in response.text  # text for the status slot


def test_htmx_rating_form_keeps_itself_so_a_failed_rating_can_be_retried(feedback_client):
    page = feedback_client.post("/ui/chat", data={"message": QUERY, "messages": ""},
                                headers={"HX-Request": "true"})
    form = page.text[page.text.index('<form class="turn-feedback"'):]
    form = form[:form.index("</form>")]
    assert 'hx-target="find .feedback-status"' in form and 'hx-swap="innerHTML"' in form
    assert 'class="feedback-status" role="status"' in form and "outerHTML" not in form


def test_every_rate_outcome_has_a_fixed_console_response():
    from typing import get_args

    from mainframe_rag.feedback.client import RateOutcome
    from mainframe_rag.webui.routes import _FEEDBACK_OUTCOMES

    assert set(_FEEDBACK_OUTCOMES) == set(get_args(RateOutcome))


def test_no_js_rating_opens_a_full_page_and_keeps_the_console(feedback_client):
    """Without JS the form posts natively into a new tab (the console page and
    its conversation stay), and the answer is a full page, not a bare fragment."""
    page = feedback_client.post("/ui/chat", data={"message": QUERY, "messages": ""},
                                headers={"HX-Request": "true"})
    assert 'action="/ui/feedback/form"' in page.text and 'target="_blank"' in page.text
    [record] = _records()
    RecordingFeedback.instances[0].outcome = "unavailable"
    failed = feedback_client.post("/ui/feedback/form", data={"request_id": record.request_id,
                                                             "rating": "bad"})
    assert failed.status_code == 503
    assert failed.text.startswith("<!doctype html>") and "feedback is temporarily unavailable" in failed.text
    RecordingFeedback.instances[0].outcome = "ok"
    rated = feedback_client.post("/ui/feedback/form", data={"request_id": record.request_id,
                                                            "rating": "good"})
    assert rated.status_code == 201 and rated.text.startswith("<!doctype html>")
    assert "Rated: good" in rated.text


@pytest.mark.parametrize("body", [
    {"request_id": "not-an-id", "rating": "good"},
    {"request_id": RID, "rating": "excellent"},
    {"request_id": RID, "rating": "good", "answer": "client-supplied answer text"},
    {"request_id": RID, "rating": "good", "comment": "x" * 2001},
])
def test_invalid_console_rating_never_reaches_the_store(feedback_client, body):
    assert feedback_client.post("/ui/feedback", json=body).status_code == 422
    assert RecordingFeedback.instances[0].ratings == []


def test_console_page_offers_rating_only_when_enabled(feedback_client, monkeypatch, synthetic_pdf,
                                                     servable_representation_gate):
    assert 'data-feedback="on"' in feedback_client.get("/ui").text
    monkeypatch.setattr(app_mod, "feedback", None)
    assert 'data-feedback="off"' in feedback_client.get("/ui").text
    response = feedback_client.post("/ui/feedback", json={"request_id": RID, "rating": "good"})
    assert response.status_code == 404 and response.json()["code"] == "feedback_disabled"


_CALLS = [
    ("answer", lambda c: c.post("/v1/answer", json={"query": QUERY})),
    ("answer_stream", lambda c: c.post("/v1/answer?stream=true", json={"query": QUERY})),
    ("chat", lambda c: c.post("/v1/chat", json={"messages": [{"role": "user", "content": QUERY}]})),
    ("chat_stream", lambda c: c.post("/v1/chat", json={"messages": [{"role": "user", "content": QUERY}],
                                                         "stream": True})),
    ("console", lambda c: c.post("/ui/chat/stream", json={"messages": [{"role": "user", "content": QUERY}]})),
    ("console", lambda c: c.post("/ui/chat", data={"message": QUERY, "messages": ""},
                                 headers={"HX-Request": "true"})),
]


@pytest.mark.parametrize("endpoint,call", _CALLS)
def test_zero_hit_declines_are_recorded_on_every_path(feedback_client, monkeypatch, endpoint, call):
    """A no-evidence decline is exactly the answer `wrongly_declined` rates:
    it must be recorded like any other finalized answer (review S1)."""
    monkeypatch.setattr(app_mod, "retrieve_search",
                        lambda *a, **k: ([], "nl", {"embed_ms": 1, "qdrant_ms": 2}))
    response = call(feedback_client)
    assert response.status_code == 200
    [record] = _records()
    assert record.endpoint == endpoint and record.query == QUERY
    assert record.telemetry["verification_state"] == "insufficient_evidence"
    assert record.telemetry["hits"] == 0


def test_record_carries_supplied_evidence_identities_and_finalize_time(feedback_client, monkeypatch):
    import time

    monkeypatch.setenv("IMAGE_SHA", "a" * 40)
    before = time.time()
    feedback_client.post("/v1/answer", json={"query": QUERY})
    [record] = _records()
    assert record.supplied and all(isinstance(c, str) for c in record.supplied)
    assert before <= record.finalized_at <= time.time()
    assert record.telemetry["release"] == "a" * 40
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    assert record.telemetry["extraction_rules_version"] == extraction_rules_version()


def test_fallback_console_answer_renders_a_rating_form_that_round_trips(feedback_client):
    page = feedback_client.post("/ui/chat", data={"message": QUERY, "messages": ""},
                                headers={"HX-Request": "true"})
    [record] = _records()
    assert f'name="request_id" value="{record.request_id}"' in page.text
    rated = feedback_client.post("/ui/feedback/form", data={
        "request_id": record.request_id, "rating": "bad", "reasons": ["incomplete", "too_slow"],
        "comment": "SYNTHETIC"}, headers={"HX-Request": "true"})
    assert rated.status_code == 200 and "Rated: bad" in rated.text
    [rating] = RecordingFeedback.instances[0].ratings
    assert (rating.request_id, rating.rating, rating.reasons) == (record.request_id, "bad",
                                                                   ["incomplete", "too_slow"])
    invalid = feedback_client.post("/ui/feedback/form", data={"request_id": "nope", "rating": "bad"},
                                   headers={"HX-Request": "true"})
    assert invalid.status_code == 200 and "the rating is invalid" in invalid.text
    assert len(RecordingFeedback.instances[0].ratings) == 1


def test_fallback_console_renders_no_rating_form_when_disabled(monkeypatch, synthetic_pdf,
                                                               servable_representation_gate):
    monkeypatch.delenv("FEEDBACK_ENABLED", raising=False)
    for client in _client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf):
        page = client.post("/ui/chat", data={"message": QUERY, "messages": ""}, headers={"HX-Request": "true"})
        assert page.status_code == 200 and "feedback/form" not in page.text


def test_shutdown_unpublishes_the_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    monkeypatch.setenv("FEEDBACK_ENABLED", "true")
    monkeypatch.setenv("FEEDBACK_URL", "http://feedback.internal")
    monkeypatch.setenv("FEEDBACK_API_KEY", "feedback-test-token")
    monkeypatch.setattr(app_mod, "FeedbackClient", RecordingFeedback)
    for _ in _client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf):
        assert app_mod.feedback is not None
    assert app_mod.feedback is None


def test_answer_path_logs_never_carry_query_or_answer_text(feedback_client, caplog):
    canary = "SYNTHETIC-CANARY-QUERY-7731"
    with caplog.at_level("DEBUG"):
        feedback_client.post("/v1/answer", json={"query": f"{QUERY} {canary}"})
        feedback_client.post("/ui/chat/stream", json={"messages": [{"role": "user",
                                                                    "content": f"{QUERY} {canary}"}]})
    assert [r.query for r in _records()] == [f"{QUERY} {canary}"] * 2
    assert canary not in caplog.text


@pytest.mark.anyio
async def test_shutdown_counts_every_undelivered_record_as_dropped(monkeypatch):
    outcomes = []
    monkeypatch.setattr("mainframe_rag.feedback.client.record_feedback_record", outcomes.append)
    gate = asyncio.Event()

    async def hang(request):
        await gate.wait()
        return httpx2.Response(201, json={"recorded": True})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(hang)) as http:
        client = FeedbackClient(_settings(feedback_drain_timeout_s=0.05), client=http)
        client.start()
        for rid in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
            client.submit(_record(rid))
        await asyncio.sleep(0.05)  # the worker is now blocked on the first delivery
        await client.aclose()
    assert outcomes == ["dropped"] * 3


def test_console_js_vocabulary_matches_the_store():
    import re
    from pathlib import Path

    from mainframe_rag.feedback.store import RATINGS, REASONS

    js = (Path(app_mod.__file__).parents[1] / "webui/static/js/console.js").read_text()

    def values(name):
        block = re.search(name + r" = \[(.*?)\];", js, re.DOTALL).group(1)
        return tuple(re.findall(r'\["([a-z_]+)",', block))

    assert values("FEEDBACK_RATINGS") == RATINGS
    assert values("FEEDBACK_REASONS") == REASONS


@pytest.mark.anyio
async def test_client_and_service_agree_over_real_http(tmp_path, monkeypatch):
    """S4: the real FeedbackClient against the real service on loopback HTTP
    (no transport doubles): path, bearer token, status and body contracts,
    proven by the stored content, and a wrong token is a counted failure."""
    import socket
    import threading
    import time

    import uvicorn

    from mainframe_rag.feedback.service import create_app
    from mainframe_rag.feedback.store import FeedbackStore

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    # The service strips Secret whitespace exactly as the agent's header does.
    server = uvicorn.Server(uvicorn.Config(create_app(store, "feedback-test-token\n"),
                                           host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "feedback service did not start"
        await asyncio.sleep(0.02)
    url = f"http://127.0.0.1:{port}"
    outcomes: list[str] = []
    monkeypatch.setattr("mainframe_rag.feedback.client.record_feedback_record", outcomes.append)
    try:
        client = FeedbackClient(_settings(feedback_url=url + "/"))
        client.start()
        record = AnswerRecord(request_id=RID, endpoint="chat", query="q text", answer="a text",
                              citations=["c1"], supplied=["c1", "c2"],
                              telemetry={"verification_state": "accepted", "ttft_ms": 12.5},
                              finalized_at=1_000.0)
        client.submit(record)
        client.submit(record)  # a duplicate delivery: first record wins
        await asyncio.wait_for(client._queue.join(), 5)
        assert await client.rate(Rating(request_id=RID, rating="neutral")) == "ok"
        assert await client.rate(Rating(request_id=RID, rating="bad", reasons=["incomplete"],
                                        comment="c")) == "ok"
        assert await client.rate(Rating(request_id="ffffffffffff", rating="good")) == "unknown"
        await client.aclose()
        assert outcomes == ["recorded", "duplicate"]

        stored = store.answer(RID)
        assert stored is not None
        assert {k: stored[k] for k in ("endpoint", "query", "answer", "citations", "supplied",
                                       "telemetry", "created_at")} == {
            "endpoint": "chat", "query": "q text", "answer": "a text", "citations": ["c1"],
            "supplied": ["c1", "c2"], "telemetry": {"verification_state": "accepted", "ttft_ms": 12.5},
            "created_at": 1_000.0}
        rated = store.rating(RID)
        assert rated is not None
        assert (rated["rating"], rated["reasons"], rated["comment"]) == ("bad", ["incomplete"], "c")
        assert store.rating("ffffffffffff") is None

        outcomes.clear()
        wrong = FeedbackClient(_settings(feedback_url=url, feedback_api_key="not-the-token"))
        wrong.start()
        try:
            wrong.submit(_record("bbbbbbbbbbbb"))
            await asyncio.wait_for(wrong._queue.join(), 5)
            assert await wrong.rate(Rating(request_id=RID, rating="good")) == "unavailable"
        finally:
            await wrong.aclose()
        assert outcomes == ["failed"]
        assert store.answer("bbbbbbbbbbbb") is None
        assert store.rating(RID)["rating"] == "bad"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        store.close()
