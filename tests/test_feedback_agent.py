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
from tests.test_webui import _client, _parse_sse_events

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
    return Settings(_env_file=None, feedback_enabled=True, feedback_url="http://feedback.internal",
                    feedback_api_key="feedback-test-token", **overrides)


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


@pytest.mark.parametrize("record_rater,expected", [(False, None), (True, "operator-a")])
def test_rater_identity_is_recorded_only_when_enabled(monkeypatch, feedback_client, record_rater, expected):
    monkeypatch.setattr(app_mod.settings, "feedback_record_rater", record_rater)
    feedback_client.post("/ui/feedback", json={"request_id": RID, "rating": "good"},
                         headers={"X-Forwarded-User": "operator-a"})
    assert RecordingFeedback.instances[0].ratings[-1].rater == expected


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
