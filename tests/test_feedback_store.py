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
                                  comment=COMMENT))
    answer = store.answer(RID)
    assert answer is not None
    assert (answer["query"], answer["answer"], answer["endpoint"]) == (QUERY, ANSWER, "console")
    assert answer["citations"] == ["SA22-0000-00 Synthetic Reference, p. 1"]
    assert answer["telemetry"]["prompt_tokens"] == 5460 and answer["telemetry"]["budget_verified"] is True
    rating = store.rating(RID)
    assert rating["id"] == rid
    assert rating == {"id": rid, "created_at": rating["created_at"], "rating": "bad",
                      "reasons": ["incomplete", "too_slow"], "comment": COMMENT}


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
    assert store.rating(RID)["rating"] == "bad"
    changed = store.add_rating(Rating(request_id=RID, rating="good"))
    assert changed == first
    rating = store.rating(RID)
    assert (rating["rating"], rating["reasons"], rating["comment"]) == ("good", [], "")


def test_rating_for_an_unrecorded_answer_is_refused_and_not_stored(store):
    with pytest.raises(UnknownAnswerError):
        store.add_rating(Rating(request_id=RID, rating="good"))
    assert store.rating(RID) is None


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
    assert s.purge(retention_days=180) == {"answers": 1, "ratings": 1, "wal_truncated": True}
    assert s.answer("aaaaaaaaaaaa") is None and s.rating("aaaaaaaaaaaa") is None
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
    assert unknown.status_code == 404 and unknown.json()["code"] == "unknown_answer"
    invalid = client.post("/v1/ratings", json={"request_id": RID, "rating": "excellent"}, headers=auth)
    assert invalid.status_code == 422 and invalid.json()["code"] == "invalid_request"
    missing = client.get("/v1/no-such-route", headers=auth)
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


def test_service_refuses_to_start_without_a_token(store):
    with pytest.raises(RuntimeError, match="FEEDBACK_SERVICE_TOKEN"):
        create_app(store, "")


def test_token_round_trips_across_secret_whitespace(store):
    """A Secret created --from-file trails a newline on both sides: the
    agent's header and the service's expectation must normalize alike."""
    from mainframe_rag.config import bearer_auth_headers

    client = TestClient(create_app(store, TOKEN + "\n"))
    headers = bearer_auth_headers(TOKEN + "\n")
    assert client.post("/v1/answers", json=_record().model_dump(), headers=headers).status_code == 201


def test_service_owns_purge_and_the_next_write_still_works(tmp_path):
    now = [0.0]
    s = FeedbackStore(tmp_path / "f.sqlite3", clock=lambda: now[0])
    client = TestClient(create_app(s, TOKEN, retention_days=180))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    client.post("/v1/answers", json=_record("aaaaaaaaaaaa").model_dump(), headers=auth)
    now[0] = 181 * 86400.0
    assert client.post("/v1/purge", headers=auth).json() == {"answers": 1, "ratings": 0, "wal_truncated": True}
    assert s.answer("aaaaaaaaaaaa") is None
    assert client.post("/v1/answers", json=_record("bbbbbbbbbbbb").model_dump(), headers=auth).json() == {
        "recorded": True}
    assert client.post("/v1/purge", headers={"Authorization": "Bearer wrong"}).status_code == 401
    s.close()


def test_schema_version_mismatch_refuses_to_open(tmp_path):
    path = tmp_path / "f.sqlite3"
    FeedbackStore(path).close()
    import sqlite3

    db = sqlite3.connect(path)
    db.execute("UPDATE meta SET value = '999' WHERE key = 'schema'")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="schema"):
        FeedbackStore(path)


def test_purged_and_replaced_text_does_not_survive_on_disk(tmp_path):
    """Retention holds on disk, not only for reads: purged answers and a
    replaced comment leave no bytes in the database file or the WAL."""
    now = [10_000_000.0]
    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path, clock=lambda: now[0])
    old = AnswerRecord(request_id="aaaaaaaaaaaa", endpoint="answer", query="PURGED-QUERY-CANARY",
                       answer="PURGED-ANSWER-CANARY", finalized_at=now[0] - 200 * 86400)
    keep = AnswerRecord(request_id="bbbbbbbbbbbb", endpoint="answer", query="q", answer="a")
    assert store.record_answer(old) and store.record_answer(keep)
    store.add_rating(Rating(request_id="aaaaaaaaaaaa", rating="bad", comment="PURGED-COMMENT-CANARY"))
    store.add_rating(Rating(request_id="bbbbbbbbbbbb", rating="bad", comment="REPLACED-COMMENT-CANARY"))
    store.add_rating(Rating(request_id="bbbbbbbbbbbb", rating="good", comment="kept"))
    assert store.purge(retention_days=180) == {"answers": 1, "ratings": 1, "wal_truncated": True}
    on_disk = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    for canary in (b"PURGED-QUERY-CANARY", b"PURGED-ANSWER-CANARY", b"PURGED-COMMENT-CANARY",
                   b"REPLACED-COMMENT-CANARY"):
        assert canary not in on_disk, canary
    assert store.rating("bbbbbbbbbbbb")["comment"] == "kept"
    assert store.record_answer(AnswerRecord(request_id="cccccccccccc", endpoint="chat", query="q2",
                                            answer="a2"))
    store.close()


def test_purge_under_a_held_reader_reports_the_wal_untruncated_fast_then_the_retry_completes(tmp_path):
    """A concurrent read snapshot (WAL lets an export run alongside) blocks the
    WAL truncation: purge must say so at once, not report success after
    stalling every writer, and the next purge, once the reader is gone,
    removes the text from disk."""
    import sqlite3
    import time

    now = [10_000_000.0]
    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path, clock=lambda: now[0])
    store.record_answer(AnswerRecord(request_id="aaaaaaaaaaaa", endpoint="answer", query="HELD-CANARY",
                                     answer="a", finalized_at=now[0] - 200 * 86400))
    reader = sqlite3.connect(str(path))
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM answers").fetchone()
    started = time.monotonic()
    assert store.purge(retention_days=180) == {"answers": 1, "ratings": 0, "wal_truncated": False}
    assert time.monotonic() - started < 1.0
    assert store.record_answer(AnswerRecord(request_id="bbbbbbbbbbbb", endpoint="chat", query="q",
                                            answer="a"))  # writers are not stalled
    reader.rollback()
    reader.close()
    assert store.purge(retention_days=180) == {"answers": 0, "ratings": 0, "wal_truncated": True}
    on_disk = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert b"HELD-CANARY" not in on_disk
    assert store.answer("bbbbbbbbbbbb") is not None
    store.close()


def test_service_refuses_a_rater_field(store):
    """No attribution until its provenance is proven (PR B): a token holder
    cannot write an unverified rater."""
    store.record_answer(_record())
    client = TestClient(create_app(store, TOKEN))
    response = client.post("/v1/ratings", json={"request_id": RID, "rating": "good", "rater": "someone"},
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    assert store.rating(RID) is None
