"""Feedback store and service (issue #780): exact round-trips by request_id,
first-record-wins, ratings only for recorded answers, fixed client errors,
and no question/answer/comment text in logs."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mainframe_rag.feedback.service import create_app
from mainframe_rag.feedback.store import AnswerRecord, FeedbackStore, Rating, UnknownAnswerError

RID = "0123456789ab"
TOKEN = "feedback-test-token"
QUERY = "What does message SYNTHETIC-QUERY-TEXT indicate?"
ANSWER = "SYNTHETIC-ANSWER-TEXT: the excerpts describe the condition."
COMMENT = "SYNTHETIC-COMMENT-TEXT missing the operator action"


def _record(rid: str = RID, answer: str = ANSWER) -> AnswerRecord:
    return AnswerRecord(
        request_id=rid, endpoint="console", query=QUERY, answer=answer, script=None,
        citations=["SA22-0000-00 Synthetic Reference, p. 1"],
        telemetry={"verification_state": "accepted", "prompt_tokens": 5460, "budget_verified": True,
                   "ttft_ms": 70304, "reasoning_effort": "xhigh"},
    )


@pytest.fixture
def store(tmp_path):
    clock = iter(float(t) for t in range(1_000_000, 2_000_000, 10))
    s = FeedbackStore(tmp_path / "feedback.sqlite3", clock=lambda: next(clock))
    yield s
    s.close()


def test_answer_and_rating_round_trip_exactly(store):
    assert store.record_answer(_record()) is True
    rid = store.add_rating(Rating(request_id=RID, rating="bad", reasons=["incomplete", "too_slow"],
                                  comment=COMMENT, rater=None))
    answer = store.answer(RID)
    assert answer is not None
    assert (answer["query"], answer["answer"], answer["endpoint"]) == (QUERY, ANSWER, "console")
    assert answer["citations"] == ["SA22-0000-00 Synthetic Reference, p. 1"]
    assert answer["telemetry"]["prompt_tokens"] == 5460 and answer["telemetry"]["budget_verified"] is True
    [rating] = store.ratings(RID)
    assert rating["id"] == rid
    assert (rating["rating"], rating["reasons"], rating["comment"], rating["rater"]) == (
        "bad", ["incomplete", "too_slow"], COMMENT, None)


def test_first_record_wins_so_a_rated_answer_is_never_rewritten(store):
    assert store.record_answer(_record()) is True
    assert store.record_answer(_record(answer="a replayed, different answer")) is False
    assert store.answer(RID)["answer"] == ANSWER


def test_rating_converges_to_one_per_answer_across_resends_and_changes(store):
    """Idempotent submission (#780): a resend after a lost response, or a
    double click, must not create a second rating; changing one's mind
    replaces it. The answer's single rating is always the latest submission."""
    store.record_answer(_record())
    first = store.add_rating(Rating(request_id=RID, rating="bad", reasons=["wrong"], comment=COMMENT))
    resend = store.add_rating(Rating(request_id=RID, rating="bad", reasons=["wrong"], comment=COMMENT))
    assert resend == first
    assert [r["rating"] for r in store.ratings(RID)] == ["bad"]
    changed = store.add_rating(Rating(request_id=RID, rating="good"))
    assert changed == first
    [rating] = store.ratings(RID)
    assert (rating["rating"], rating["reasons"], rating["comment"]) == ("good", [], "")


def test_rating_for_an_unrecorded_answer_is_refused_and_not_stored(store):
    with pytest.raises(UnknownAnswerError):
        store.add_rating(Rating(request_id=RID, rating="good"))
    assert store.ratings(RID) == []


@pytest.mark.parametrize("bad", [
    {"request_id": "not-an-agent-id"},
    {"request_id": "0123456789AB"},
    {"rating": "excellent"},
    {"reasons": ["wrong", "wrong"]},
    {"reasons": ["unlisted"]},
    {"comment": "x" * 2001},
])
def test_rating_shape_is_bounded(bad):
    with pytest.raises(ValidationError):
        Rating(**{"request_id": RID, "rating": "good", **bad})


def test_purge_removes_expired_answers_with_their_ratings_only(tmp_path):
    now = [0.0]
    s = FeedbackStore(tmp_path / "f.sqlite3", clock=lambda: now[0])
    s.record_answer(_record("aaaaaaaaaaaa"))
    s.add_rating(Rating(request_id="aaaaaaaaaaaa", rating="bad"))
    now[0] = 200 * 86400.0
    s.record_answer(_record("bbbbbbbbbbbb"))
    assert s.purge(retention_days=180) == {"answers": 1, "ratings": 1}
    assert s.answer("aaaaaaaaaaaa") is None and s.ratings("aaaaaaaaaaaa") == []
    assert s.answer("bbbbbbbbbbbb") is not None
    # The next ordinary operation after cleanup still works.
    assert s.record_answer(_record("cccccccccccc")) is True
    s.close()


def test_service_requires_its_token_and_never_logs_text(store, caplog):
    client = TestClient(create_app(store, TOKEN))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/healthz").status_code == 200
    denied = client.post("/v1/answers", json=_record().model_dump(), headers={"Authorization": "Bearer wrong"})
    assert denied.status_code == 401 and denied.json()["code"] == "unauthorized"
    assert store.answer(RID) is None
    with caplog.at_level(logging.INFO, logger="feedback"):
        assert client.post("/v1/answers", json=_record().model_dump(), headers=auth).json() == {"recorded": True}
        ok = client.post("/v1/ratings", json={"request_id": RID, "rating": "bad", "comment": COMMENT},
                         headers=auth)
    assert ok.status_code == 201 and ok.json()["id"] >= 1
    text = caplog.text
    assert RID in text
    assert QUERY not in text and ANSWER not in text and COMMENT not in text


def test_service_refuses_unknown_answer_and_invalid_input_with_fixed_messages(store):
    client = TestClient(create_app(store, TOKEN))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    unknown = client.post("/v1/ratings", json={"request_id": RID, "rating": "good"}, headers=auth)
    assert unknown.status_code == 404
    assert unknown.json() == {"code": "unknown_answer", "message": "no recorded answer has this request id"}
    invalid = client.post("/v1/ratings", json={"request_id": RID, "rating": "excellent"}, headers=auth)
    assert invalid.status_code == 422
    assert invalid.json() == {"code": "invalid_request", "message": "the feedback request is invalid"}


def test_service_refuses_to_start_without_a_token(store):
    with pytest.raises(RuntimeError, match="FEEDBACK_SERVICE_TOKEN"):
        create_app(store, "")
