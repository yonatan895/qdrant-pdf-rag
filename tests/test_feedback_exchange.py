"""Feedback export/import (issue #805, ADR-0005): a canonical hashed JSONL
snapshot taken read-only, restored into an unserved store. All text is
generated here; the records come through the real service."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.feedback import exchange
from mainframe_rag.feedback.service import create_app
from mainframe_rag.feedback.store import (
    AnswerRecord,
    FeedbackStore,
    Rating,
    read_snapshot,
    store_in_use,
)

TOKEN = "feedback-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SENTINEL = "SYNTHETIC-SENTINEL-TEXT"
NASTY = f'{SENTINEL} "q" \\ \n tab\t   \U0001f600 café'
SRC = Path(__file__).resolve().parents[1] / "src"
NOW = 5_000_000.0


def _clock():
    t = iter(float(x) for x in range(1_000_000, 4_000_000, 10))
    return lambda: next(t)


def _post_answer(client, rid, *, answer=NASTY, telemetry=None, script=None, finalized_at=None):
    body = {"request_id": rid, "endpoint": "console", "query": NASTY, "answer": answer,
            "script": script, "citations": ["SA22-0000-00 Synthetic, p. 1"], "supplied": ["x"],
            "telemetry": telemetry if telemetry is not None else
            {"verification_state": "accepted", "ttft_ms": 70304.5, "budget_verified": True,
             "note": None, "n": 3}}
    if finalized_at is not None:
        body["finalized_at"] = finalized_at
    assert client.post("/v1/answers", json=body, headers=AUTH).status_code == 201


def _rate(client, rid, rating="bad", reasons=("wrong", "too_slow"), comment=NASTY):
    r = client.post("/v1/ratings", headers=AUTH, json={
        "request_id": rid, "rating": rating, "reasons": list(reasons), "comment": comment})
    assert r.status_code == 201


@pytest.fixture
def source(tmp_path):
    db = tmp_path / "src.sqlite3"
    store = FeedbackStore(db, clock=_clock())
    client = TestClient(create_app(store, TOKEN))
    _post_answer(client, "000000000001")
    _post_answer(client, "000000000002", script="", telemetry={})
    _post_answer(client, "000000000003", answer="plain", script="echo hi")
    _rate(client, "000000000001")
    _rate(client, "000000000001", rating="good", reasons=(), comment="")  # re-rate: latest wins
    _rate(client, "000000000003", rating="neutral", reasons=("other",), comment="")
    store.close()
    return db


def _export(db, out, *, clock=lambda: NOW, retention=180):
    argv = ["export", "--db", str(db), "--out", str(out), "--retention-days", str(retention)]
    return exchange.main(argv, clock=clock)


def _import(src, db, *flags, clock=lambda: NOW):
    return exchange.main(["import", "--in", str(src), "--db", str(db), "--retention-days", "180",
                          *flags], clock=clock)


def test_round_trip_is_byte_identical_and_preserves_records(source, tmp_path, capsys):
    out1, db2, out2 = tmp_path / "a.jsonl", tmp_path / "dst.sqlite3", tmp_path / "b.jsonl"
    assert _export(source, out1) == 0
    assert _import(out1, db2) == 0
    assert json.loads(capsys.readouterr().out) == {
        "conflict": 0, "duplicate": 0, "expired": 0, "inserted": 5, "superseded": 0}
    assert _export(db2, out2) == 0
    assert out1.read_bytes() == out2.read_bytes()
    # independent check of the stored content and the trailer hash
    lines = out1.read_bytes().split(b"\n")
    trailer = json.loads(lines[-2])
    assert trailer["sha256"] == hashlib.sha256(out1.read_bytes()[: -len(lines[-2]) - 1]).hexdigest()
    assert (trailer["answers"], trailer["ratings"]) == (3, 2)
    a, b = FeedbackStore(source), FeedbackStore(db2)
    for rid in ("000000000001", "000000000002", "000000000003"):
        assert a.answer(rid) == b.answer(rid)
        ra, rb = a.rating(rid), b.rating(rid)
        assert (ra and {k: v for k, v in ra.items() if k != "id"}) == (
            rb and {k: v for k, v in rb.items() if k != "id"})
    got = b.answer("000000000001")
    assert got["query"] == NASTY and got["telemetry"]["ttft_ms"] == 70304.5
    assert b.answer("000000000002")["script"] == "" and b.answer("000000000002")["telemetry"] == {}
    assert b.rating("000000000001")["rating"] == "good"
    a.close()
    b.close()


def test_ordinary_operations_work_after_import(source, tmp_path):
    out, db2 = tmp_path / "a.jsonl", tmp_path / "dst.sqlite3"
    assert _export(source, out) == 0 and _import(out, db2) == 0
    store = FeedbackStore(db2, clock=lambda: 20_000_000.0)
    assert store.record_answer(AnswerRecord(request_id="0000000000aa", endpoint="chat",
                                            query="q", answer="a")) is True
    assert store.record_answer(AnswerRecord(request_id="000000000001", endpoint="chat",
                                            query="q", answer="a")) is False  # first still wins
    store.add_rating(Rating(request_id="000000000002", rating="bad"))  # imported id, no rating yet
    store.add_rating(Rating(request_id="000000000001", rating="bad"))  # re-rate imported
    assert store.rating("000000000001")["rating"] == "bad"
    # imported rows are 1_000_000-era; a 20_000_000 clock puts them past 180 days
    assert store.purge(retention_days=180) == {"answers": 3, "ratings": 3, "wal_truncated": True}
    assert store.answer("000000000001") is None and store.answer("0000000000aa")["answer"] == "a"
    store.close()


def test_import_twice_into_populated_store_needs_merge_and_converges(source, tmp_path, capsys):
    out, db2 = tmp_path / "a.jsonl", tmp_path / "dst.sqlite3"
    assert _export(source, out) == 0 and _import(out, db2) == 0
    capsys.readouterr()
    assert _import(out, db2) == 2  # target not empty
    assert "target_not_empty" in capsys.readouterr().err
    assert _import(out, db2, "--merge") == 0
    assert json.loads(capsys.readouterr().out)["duplicate"] == 5
    out2 = tmp_path / "b.jsonl"
    assert _export(db2, out2) == 0 and out.read_bytes() == out2.read_bytes()


def _mutate_target(db, rid="000000000002"):
    s = FeedbackStore(db)
    s._db.execute("UPDATE answers SET answer = 'different' WHERE request_id = ?", (rid,))
    s.close()


def test_conflict_refuses_and_leaves_target_unchanged(source, tmp_path, capsys):
    out, db2 = tmp_path / "a.jsonl", tmp_path / "dst.sqlite3"
    assert _export(source, out) == 0 and _import(out, db2) == 0
    _mutate_target(db2)
    before = tmp_path / "before.jsonl"
    assert _export(db2, before) == 0
    capsys.readouterr()
    assert _import(out, db2, "--merge") == 2
    err = capsys.readouterr().err
    assert "conflict_1" in err and SENTINEL not in err
    after = tmp_path / "after.jsonl"
    assert _export(db2, after) == 0
    assert before.read_bytes() == after.read_bytes()


def test_keep_existing_keeps_target_rows_and_counts_conflicts(source, tmp_path, capsys):
    out, db2 = tmp_path / "a.jsonl", tmp_path / "dst.sqlite3"
    assert _export(source, out) == 0 and _import(out, db2) == 0
    _mutate_target(db2, "000000000003")  # also has a rating in the file
    capsys.readouterr()
    assert _import(out, db2, "--merge", "--keep-existing") == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts["conflict"] == 2 and counts["inserted"] == 0  # answer + its rating skipped
    s = FeedbackStore(db2)
    assert s.answer("000000000003")["answer"] == "different"
    s.close()


def test_rating_newest_wins_and_tie_conflicts(tmp_path, capsys):
    def make(path, created):
        s = FeedbackStore(path, clock=lambda: 1_500_000.0)
        s.record_answer(AnswerRecord(request_id="000000000001", endpoint="chat", query="q",
                                     answer="a", finalized_at=1_000_000.0))
        s._clock = lambda: created
        s.add_rating(Rating(request_id="000000000001", rating="bad", comment=f"at {created}"))
        s.close()

    old, new, target = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3", tmp_path / "t.sqlite3"
    make(old, 2_000_000.0)
    make(new, 3_000_000.0)
    make(target, 2_500_000.0)
    f_old, f_new, f_tie = tmp_path / "o.jsonl", tmp_path / "n.jsonl", tmp_path / "t.jsonl"
    assert _export(old, f_old) == 0 and _export(new, f_new) == 0
    tie = tmp_path / "tie.sqlite3"
    make(tie, 2_500_000.0)
    s = FeedbackStore(tie)
    s._db.execute("UPDATE ratings SET comment = 'other'")
    s.close()
    assert _export(tie, f_tie) == 0
    capsys.readouterr()
    assert _import(f_tie, target, "--merge") == 2  # equal created_at, different content
    assert _import(f_old, target, "--merge") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["superseded"] == 1
    s = FeedbackStore(target)
    assert s.rating("000000000001")["comment"] == "at 2500000.0"
    s.close()
    assert _import(f_new, target, "--merge") == 0
    s = FeedbackStore(target)
    assert s.rating("000000000001")["comment"] == "at 3000000.0"
    s.close()


def test_expired_rows_are_counted_for_the_next_purge(tmp_path, capsys):
    db = tmp_path / "s.sqlite3"
    s = FeedbackStore(db)
    s.record_answer(AnswerRecord(request_id="000000000001", endpoint="chat", query="q", answer="a",
                                 finalized_at=NOW - 181 * 86400))
    s.record_answer(AnswerRecord(request_id="000000000002", endpoint="chat", query="q", answer="a",
                                 finalized_at=NOW - 86400))
    s.close()
    out = tmp_path / "e.jsonl"
    assert _export(db, out) == 0 and _import(out, tmp_path / "d.sqlite3") == 0
    assert json.loads(capsys.readouterr().out)["expired"] == 1


def _lines(path):
    return path.read_bytes().split(b"\n")[:-1]


def _rehash(lines):
    body = b"".join(line + b"\n" for line in lines[:-1])
    t = json.loads(lines[-1])
    t["sha256"] = hashlib.sha256(body).hexdigest()
    return body + json.dumps(t, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _tampered(source, tmp_path):
    out = tmp_path / "good.jsonl"
    assert _export(source, out) == 0
    return out, _lines(out)


def _write(tmp_path, data, name="bad.jsonl"):
    p = tmp_path / name
    p.write_bytes(data)
    p.chmod(0o600)
    return p


def _mutate(lines, idx, **changes):
    row = json.loads(lines[idx])
    for k, v in changes.items():
        if v is exchange:  # sentinel: delete key
            del row[k]
        else:
            row[k] = v
    out = list(lines)
    out[idx] = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
    return out


@pytest.mark.parametrize("case", [
    "flipped_byte", "truncated", "no_trailer", "version_2", "schema_2", "dangling_rating",
    "long_comment", "unknown_field", "duplicate_id", "bad_enum", "count_mismatch"])
def test_tampered_files_are_refused_without_writing_or_echoing_text(source, tmp_path, capsys, case):
    good, lines = _tampered(source, tmp_path)
    first_answer = 1
    rating_idx = next(i for i, ln in enumerate(lines) if b'"kind":"rating"' in ln)
    big = SENTINEL + "x" * 3000
    data = {
        "flipped_byte": good.read_bytes().replace(b"plain", b"plaiz"),
        "truncated": good.read_bytes()[:-40],
        "no_trailer": b"\n".join(lines[:-1]) + b"\n",
        "version_2": _rehash(_mutate(lines, 0, version=2)),
        "schema_2": _rehash(_mutate(lines, 0, store_schema=2)),
        "dangling_rating": _rehash(_mutate(lines, rating_idx, request_id="0000000000ff")),
        "long_comment": _rehash(_mutate(lines, rating_idx, comment=big)),
        "unknown_field": _rehash(_mutate(lines, first_answer, extra=SENTINEL)),
        "duplicate_id": _rehash([*lines[:-1], lines[first_answer], lines[-1]]),
        "bad_enum": _rehash(_mutate(lines, first_answer, endpoint=SENTINEL)),
        "count_mismatch": _rehash(_mutate(lines, -1, answers=99)),
    }[case]
    assert case != "flipped_byte" or data != good.read_bytes()
    db2 = tmp_path / "dst.sqlite3"
    assert _import(_write(tmp_path, data), db2) == 2
    cap = capsys.readouterr()
    assert SENTINEL not in cap.err + cap.out and NASTY not in cap.err + cap.out
    assert cap.err.startswith("feedback-exchange: refused: ") and cap.out == ""
    s = FeedbackStore(db2)
    assert s.is_empty()  # nothing written
    s.close()


def test_import_refuses_group_readable_input(source, tmp_path, capsys):
    out = tmp_path / "a.jsonl"
    assert _export(source, out) == 0
    out.chmod(0o644)
    assert _import(out, tmp_path / "d.sqlite3") == 2
    assert "insecure_permissions" in capsys.readouterr().err
    assert not (tmp_path / "d.sqlite3").exists()


def test_export_file_is_0600_o_excl_and_never_in_git(source, tmp_path, capsys):
    old = os.umask(0o000)
    try:
        out = tmp_path / "a.jsonl"
        assert _export(source, out) == 0
    finally:
        os.umask(old)
    assert (out.stat().st_mode & 0o777) == 0o600
    before = out.read_bytes()
    assert _export(source, out) == 2 and "output_exists" in capsys.readouterr().err
    assert out.read_bytes() == before
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert _export(source, repo / "x.jsonl") == 2
    assert "output_in_git_worktree" in capsys.readouterr().err and not (repo / "x.jsonl").exists()


def test_export_refuses_a_terminal(source, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert exchange.main(["export", "--db", str(source), "--out", "-",
                          "--retention-days", "180"]) == 2
    assert "output_is_terminal" in capsys.readouterr().err


def test_export_to_stdout_matches_file_export(source, tmp_path):
    out = tmp_path / "a.jsonl"
    assert _export(source, out) == 0
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    cp = subprocess.run([sys.executable, "-m", "mainframe_rag.feedback.exchange", "export", "--db",
                         str(source), "--out", "-", "--retention-days", "180"], env=env,
                        capture_output=True, check=True)
    body = out.read_bytes()
    # only exported_at (and so the trailer hash) differs
    assert cp.stdout.split(b"\n")[1:-2] == body.split(b"\n")[1:-2]


# --- D7 (amended): read-only snapshot, released before output -----------------------------


class _Spy:
    """Connection proxy: records the URI, runs a hook at the first read."""

    def __init__(self, real, hook):
        self.real, self.hook, self.closed = real, hook, False

    def execute(self, sql, *a):
        if "FROM answers" in sql and self.hook:
            hook, self.hook = self.hook, None
            hook(self.real)
        return self.real.execute(sql, *a)

    def close(self):
        self.closed = True
        self.real.close()


def test_snapshot_is_read_only_consistent_and_does_not_block_a_writer(source):
    seen: dict = {}

    def connect(target, **kw):
        seen["uri"] = target
        return _Spy(sqlite3.connect(target, **kw), seen.get("hook"))

    def hook(real):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            real.execute("DELETE FROM answers")  # writes fail on this connection
        writer = FeedbackStore(source)
        done = threading.Event()

        def write():
            writer.record_answer(AnswerRecord(request_id="0000000000bb", endpoint="chat",
                                              query="q", answer="a"))
            done.set()

        threading.Thread(target=write, daemon=True).start()
        seen["writer_not_blocked"] = done.wait(timeout=3)  # snapshot open, writer proceeds
        writer.close()

    seen["hook"] = hook
    answers, _ratings = read_snapshot(source, connect=connect)
    assert "mode=ro" in seen["uri"] and seen["uri"].startswith("file:")
    assert seen["writer_not_blocked"] is True
    assert {a["request_id"] for a in answers} == {"000000000001", "000000000002", "000000000003"}


def test_export_releases_the_store_before_writing_and_never_pins_the_wal(source, tmp_path,
                                                                         monkeypatch):
    holder: dict = {}
    real_connect = sqlite3.connect

    def connect(target, **kw):
        holder["conn"] = _Spy(real_connect(target, **kw), None)
        return holder["conn"]

    real_write = exchange._write_private

    def write(path, data):
        holder["closed_before_output"] = holder["conn"].closed
        real_write(path, data)

    monkeypatch.setattr("mainframe_rag.feedback.store.sqlite3.connect", connect)
    monkeypatch.setattr(exchange, "_write_private", write)
    assert _export(source, tmp_path / "a.jsonl") == 0
    assert holder["closed_before_output"] is True
    monkeypatch.undo()
    s = FeedbackStore(source, clock=lambda: NOW)
    assert s.purge(retention_days=180)["wal_truncated"] is True  # no snapshot left behind
    s.close()


def test_export_does_not_modify_the_store_file(source, tmp_path):
    s = FeedbackStore(source)
    s.close()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assert _export(source, tmp_path / "a.jsonl") == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_export_of_a_missing_store_is_refused_without_creating_it(tmp_path, capsys):
    assert _export(tmp_path / "none.sqlite3", tmp_path / "a.jsonl") == 2
    assert "store_unreadable" in capsys.readouterr().err
    assert not (tmp_path / "none.sqlite3").exists() and not (tmp_path / "a.jsonl").exists()


# --- single writer: import is refused while a service holds the store ----------------------

_HOLDER = """
import sys, time
from mainframe_rag.feedback.store import FeedbackStore
s = FeedbackStore(sys.argv[1])
print("ready", flush=True)
sys.stdin.readline()
"""


def test_import_refuses_while_the_store_is_in_use_and_works_after(source, tmp_path, capsys):
    out, db2 = tmp_path / "a.jsonl", tmp_path / "dst.sqlite3"
    assert _export(source, out) == 0
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(db2)], env=env, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline().strip() == "ready"
        assert store_in_use(db2) is True
        capsys.readouterr()
        assert _import(out, db2, "--merge") == 2
        assert "store_in_use" in capsys.readouterr().err
    finally:
        p.communicate("\n", timeout=10)
    assert store_in_use(db2) is False
    assert _import(out, db2, "--merge") == 0  # the next ordinary operation succeeds


def test_probe_sees_an_idle_open_connection_but_is_only_point_in_time(tmp_path):
    """What the probe does and does not promise (#805, D3): the single-writer
    rule is the runbook step (stop the service); the probe is a backstop. A WAL
    store held open by an idle connection (no transaction) IS detected; it holds
    nothing afterwards, so a holder that starts later is not seen at all."""
    db = tmp_path / "held.sqlite3"
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(db)], env=env, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline().strip() == "ready"  # constructed, committed, idle
        assert store_in_use(db) is True
    finally:
        p.communicate("\n", timeout=10)
    assert store_in_use(db) is False
    late = FeedbackStore(db)  # a service starting after the probe: not prevented
    assert store_in_use(db) is True  # visible only to the next probe
    late.close()
