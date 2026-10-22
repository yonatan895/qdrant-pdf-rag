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


# --- Scheduled purge and its entry point (issue #804) -----------------------


def _launch_service(tmp_path, port, extra_env):
    """The real uvicorn command the chart will run, with only the env the
    service needs (no FEEDBACK_ENABLED, no Qdrant or model env)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": src,
           "FEEDBACK_STORE_PATH": str(tmp_path / "feedback.sqlite3"),
           "FEEDBACK_SERVICE_TOKEN": TOKEN + "\n", **extra_env}
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "--factory", "mainframe_rag.feedback.service:app_from_env",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until(predicate, timeout=30.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not reached in time")


def test_launch_entry_serves_records_and_purges_on_its_own_schedule(tmp_path):
    """The real `uvicorn --factory ...:app_from_env` command with the chart's
    env: it serves, authenticates /metrics, and its scheduler (first attempt at
    once here) removes an expired record from the files, with the outcome
    visible on /metrics. A broken entry point or an unwired scheduler fails
    here, which the agent image's own CMD cannot catch."""
    import subprocess
    import time

    import httpx2

    old = AnswerRecord(request_id="aaaaaaaaaaaa", endpoint="answer", query="LAUNCH-OLD-CANARY",
                       answer="a", finalized_at=time.time() - 3 * 86400)
    seed = FeedbackStore(tmp_path / "feedback.sqlite3")
    assert seed.record_answer(old)
    seed.close()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = _launch_service(tmp_path, port, {
        "FEEDBACK_RETENTION_DAYS": "1", "METRICS_ENABLED": "true",
        "FEEDBACK_PURGE_FIRST_DELAY_S": "0"})
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        _wait_until(lambda: proc.poll() is not None or _healthy(base))
        assert proc.poll() is None, proc.stdout.read()
        assert httpx2.get(f"{base}/metrics").status_code == 401
        assert httpx2.post(f"{base}/v1/answers", json=_record("bbbbbbbbbbbb").model_dump(),
                           headers=auth).json() == {"recorded": True}
        assert httpx2.post(f"{base}/v1/ratings", json={"request_id": "bbbbbbbbbbbb", "rating": "good"},
                           headers=auth).status_code == 201

        def purged():
            text = httpx2.get(f"{base}/metrics", headers=auth).text
            return 'rag_feedback_purge_total{outcome="truncated"} 1.0' in text and text

        text = _wait_until(purged)
        assert 'rag_feedback_purged_rows_total{kind="answers"} 1.0' in text
        assert 'rag_feedback_purge_total{outcome="error"} 0.0' in text
        on_disk = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
        assert b"LAUNCH-OLD-CANARY" not in on_disk
        assert b"SYNTHETIC-QUERY-TEXT" in on_disk  # the unexpired record is still stored
    finally:
        proc.terminate()
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
    for text in (QUERY, ANSWER, "LAUNCH-OLD-CANARY"):
        assert text not in output  # uvicorn's access log: method, path, status only


def _healthy(base: str) -> bool:
    import httpx2

    try:
        return httpx2.get(f"{base}/healthz", timeout=1.0).status_code == 200
    except httpx2.TransportError:
        return False


def test_launch_entry_metrics_are_404_when_disabled(tmp_path):
    import httpx2

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = _launch_service(tmp_path, port, {})
    try:
        _wait_until(lambda: proc.poll() is not None or _healthy(base))
        assert proc.poll() is None, proc.stdout.read()
        denied = httpx2.get(f"{base}/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        assert denied.status_code == 404 and denied.json()["code"] == "not_found"
    finally:
        proc.terminate()
        proc.communicate(timeout=10)


class _Stop(Exception):
    """Ends the otherwise endless schedule from the injected sleep."""


def _drive(runner, schedule, on_sleep):
    """Run the schedule with a sleep that records its argument and lets the
    test act between attempts; returns the requested sleeps."""
    import asyncio

    from mainframe_rag.feedback.purge import run_schedule

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        on_sleep(len(sleeps), seconds)

    with pytest.raises(_Stop):
        asyncio.run(run_schedule(runner, schedule, sleep=sleep))
    return sleeps


def _series(metrics, name, **labels):
    return metrics.registry.get_sample_value(name, labels)


def _disk(tmp_path) -> bytes:
    return b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())


def test_purge_retries_an_untruncated_wal_on_the_ladder_then_hourly_until_it_truncates(tmp_path):
    """`wal_truncated: false` is a retry, never success: sleeps are 30 (first),
    60, 300, 900, then hourly catch-up; the success gauge stays unset and the
    expired text is really still on disk until the reader is gone; the next
    attempt truncates, records success and the text leaves both files; the
    next ordinary operations then work."""
    import sqlite3

    from mainframe_rag.feedback.purge import FeedbackMetrics, PurgeRunner, PurgeSchedule

    now = [10_000_000.0]
    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path, clock=lambda: now[0])
    store.record_answer(AnswerRecord(request_id="aaaaaaaaaaaa", endpoint="answer",
                                     query="RETRY-CANARY", answer="a",
                                     finalized_at=now[0] - 200 * 86400))
    reader = sqlite3.connect(str(path))
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM answers").fetchone()
    metrics = FeedbackMetrics()
    runner = PurgeRunner(store, 180, metrics, clock=lambda: 1234.5)
    seen: dict[str, object] = {}

    def on_sleep(n: int, seconds: float) -> None:
        if n == 5:  # before the first hourly catch-up attempt
            seen["deferred"] = _series(metrics, "rag_feedback_purge_total", outcome="deferred")
            seen["gauge_while_deferred"] = _series(
                metrics, "rag_feedback_purge_last_success_timestamp_seconds")
            seen["text_still_on_disk"] = b"RETRY-CANARY" in _disk(tmp_path)
            reader.rollback()
            reader.close()
        if n == 6:  # the daily sleep after success
            raise _Stop

    sleeps = _drive(runner, PurgeSchedule(interval_s=86400.0), on_sleep)
    assert sleeps == [30.0, 60.0, 300.0, 900.0, 3600.0, 86400.0]
    assert seen == {"deferred": 4.0, "gauge_while_deferred": 0.0, "text_still_on_disk": True}
    assert _series(metrics, "rag_feedback_purge_total", outcome="truncated") == 1.0
    assert _series(metrics, "rag_feedback_purge_total", outcome="error") == 0.0
    assert _series(metrics, "rag_feedback_purge_last_success_timestamp_seconds") == 1234.5
    assert _series(metrics, "rag_feedback_purged_rows_total", kind="answers") == 1.0
    assert b"RETRY-CANARY" not in _disk(tmp_path)
    # Next ordinary operations after the cleanup.
    assert store.record_answer(AnswerRecord(request_id="bbbbbbbbbbbb", endpoint="chat", query="q",
                                            answer="a"))
    store.add_rating(Rating(request_id="bbbbbbbbbbbb", rating="good"))
    assert runner.run() == ("truncated", {"answers": 0, "ratings": 0, "wal_truncated": True})
    store.close()


def test_a_truncated_first_attempt_waits_the_interval_without_retrying(tmp_path):
    from mainframe_rag.feedback.purge import FeedbackMetrics, PurgeRunner, PurgeSchedule

    store = FeedbackStore(tmp_path / "f.sqlite3")
    metrics = FeedbackMetrics()

    def on_sleep(n: int, seconds: float) -> None:
        if n == 3:
            raise _Stop

    sleeps = _drive(PurgeRunner(store, 180, metrics), PurgeSchedule(interval_s=120.0, first_delay_s=1.0),
                    on_sleep)
    assert sleeps == [1.0, 120.0, 120.0]
    assert _series(metrics, "rag_feedback_purge_total", outcome="truncated") == 2.0
    store.close()


def test_a_failing_purge_is_an_error_outcome_the_loop_survives_and_logs_only_the_type(tmp_path, caplog):
    import sqlite3

    from mainframe_rag.feedback.purge import FeedbackMetrics, PurgeRunner, PurgeSchedule

    store = FeedbackStore(tmp_path / "f.sqlite3")
    store.record_answer(_record())
    real_purge = store.purge
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError(f"database is locked {QUERY}")
        return real_purge(**kwargs)

    store.purge = flaky  # type: ignore[method-assign]
    metrics = FeedbackMetrics()

    def on_sleep(n: int, seconds: float) -> None:
        if n == 4:
            raise _Stop

    with caplog.at_level(logging.INFO, logger="feedback"):
        sleeps = _drive(PurgeRunner(store, 180, metrics), PurgeSchedule(interval_s=86400.0), on_sleep)
    assert sleeps == [30.0, 60.0, 300.0, 86400.0]
    assert _series(metrics, "rag_feedback_purge_total", outcome="error") == 2.0
    assert _series(metrics, "rag_feedback_purge_total", outcome="truncated") == 1.0
    assert "OperationalError" in caplog.text
    assert QUERY not in caplog.text and "locked" not in caplog.text
    store.close()


def test_manual_purge_shares_the_outcome_accounting_and_metrics_hold_no_text(tmp_path, caplog):
    """POST /v1/purge goes through the same runner as the schedule; neither
    the logs nor the exposition ever carry question, answer or comment text,
    also not after a deferred purge."""
    import sqlite3

    now = [10_000_000.0]
    path = tmp_path / "f.sqlite3"
    store = FeedbackStore(path, clock=lambda: now[0])
    client = TestClient(create_app(store, TOKEN, retention_days=180, metrics_enabled=True))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    old = _record("aaaaaaaaaaaa").model_copy(update={"finalized_at": now[0] - 200 * 86400})
    client.post("/v1/answers", json=old.model_dump(), headers=auth)
    client.post("/v1/answers", json=_record().model_dump(), headers=auth)
    client.post("/v1/ratings", json={"request_id": RID, "rating": "bad", "comment": COMMENT},
                headers=auth)
    reader = sqlite3.connect(str(path))
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM answers").fetchone()
    with caplog.at_level(logging.INFO, logger="feedback"):
        deferred = client.post("/v1/purge", headers=auth)
        reader.rollback()
        reader.close()
        done = client.post("/v1/purge", headers=auth)
    assert deferred.json() == {"answers": 1, "ratings": 0, "wal_truncated": False}
    assert done.json() == {"answers": 0, "ratings": 0, "wal_truncated": True}
    body = client.get("/metrics", headers=auth).text
    assert 'rag_feedback_purge_total{outcome="deferred"} 1.0' in body
    assert 'rag_feedback_purge_total{outcome="truncated"} 1.0' in body
    for text in (QUERY, ANSWER, COMMENT):
        assert text not in caplog.text and text not in body
    purges = [r for r in caplog.records if '"feedback_purge"' in r.getMessage()]
    assert [r.levelname for r in purges] == ["WARNING", "INFO"]
    store.close()


def test_scheduler_runs_with_the_app_and_stops_with_it(tmp_path):
    """The lifespan owns the task: it starts a schedule on the loop and
    cancels it on shutdown, and an app without a schedule starts none."""
    import asyncio

    from mainframe_rag.feedback.purge import PurgeSchedule

    store = FeedbackStore(tmp_path / "f.sqlite3")
    started: list[float] = []

    async def sleeping(seconds: float) -> None:
        started.append(seconds)
        await asyncio.sleep(3600)

    app = create_app(store, TOKEN, schedule=PurgeSchedule(interval_s=86400.0, first_delay_s=7.0),
                     sleep=sleeping)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert _wait_until(lambda: started) == [7.0]
    plain = create_app(store, TOKEN)
    with TestClient(plain):
        pass
    assert started == [7.0]
    store.close()


def test_metrics_endpoint_requires_the_token_and_is_404_when_off(store):
    off = TestClient(create_app(store, TOKEN))
    on = TestClient(create_app(store, TOKEN, metrics_enabled=True))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert off.get("/metrics", headers=auth).status_code == 404
    assert on.get("/metrics").status_code == 401
    assert on.get("/metrics", headers=auth).status_code == 200
    assert on.get("/healthz").status_code == 200
