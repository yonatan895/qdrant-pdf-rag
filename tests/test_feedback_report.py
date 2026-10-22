"""Offline feedback report (issue #805 PR 2, ADR-0005 D5): counts and bounded
labels from an export file. Export files are generated here through the PR 1
export path; every text field carries a sentinel that must never reach output."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mainframe_rag.agent.answer import VERIFICATION_STATES as AGENT_STATES
from mainframe_rag.config import REASONING_EFFORTS
from mainframe_rag.feedback import exchange, report
from mainframe_rag.feedback.store import AnswerRecord, FeedbackStore, Rating

SENTINEL = "SYNTHETIC-SENTINEL-TEXT"
SRC = Path(__file__).resolve().parents[1] / "src"
BASE = 1_790_000_000.0  # 2026-09-21 UTC
NOW = BASE + 10 * 86400


def _tele(**kw):
    return kw


# (id, telemetry, rating, reasons); an independent answer key is written below.
ROWS = [
    ("000000000001", _tele(verification_state="accepted", query_kind="nl", reasoning_effort="xhigh",
                           units_omitted=0, budget_verified=True, evidence=3, has_script=False,
                           elapsed_ms=100, llm_model="qwen-27b", release="abcdef123456"),
     "bad", ["wrong", "too_slow"]),
    ("000000000002", _tele(verification_state="accepted", query_kind="nl", reasoning_effort="xhigh",
                           units_omitted=0, budget_verified=True, evidence=2, has_script=False,
                           elapsed_ms=200, llm_model="qwen-27b", release="abcdef123456"),
     "good", []),
    ("000000000003", _tele(verification_state="unverified_draft", query_kind="identifier",
                           reasoning_effort="low", units_omitted=2, budget_verified=False,
                           evidence=0, has_script=True, elapsed_ms=400, llm_model="qwen-27b"),
     "bad", ["wrong"]),
    ("000000000004", _tele(verification_state="insufficient_evidence", query_kind="nl"),
     "neutral", ["other"]),
    ("000000000005", _tele(verification_state="accepted"), None, []),
    ("000000000006", _tele(verification_state=f"{SENTINEL} free text", llm_model=f"{SENTINEL} a b",
                           query_kind=SENTINEL, elapsed_ms="x",
                           reasoning_effort="ultra", units_omitted=SENTINEL),
     "bad", []),
    ("000000000007", {}, "good", []),
]


def _make_db(path: Path, rows=ROWS) -> Path:
    t = iter(range(10_000))
    store = FeedbackStore(path, clock=lambda: BASE + next(t))
    for rid, tele, rating, reasons in rows:
        store.record_answer(AnswerRecord(
            request_id=rid, endpoint="console", query=f"{SENTINEL} q", answer=f"{SENTINEL} a",
            script=f"{SENTINEL} s", citations=[f"{SENTINEL} c"], supplied=[f"{SENTINEL} x"],
            telemetry={**tele, "note": f"{SENTINEL} note"}, finalized_at=BASE))
        if rating:
            store.add_rating(Rating(request_id=rid, rating=rating, reasons=reasons,
                                    comment=f"{SENTINEL} comment"))
    store.close()
    return path


def _export(db: Path, out: Path) -> Path:
    assert exchange.main(["export", "--db", str(db), "--out", str(out), "--retention-days", "180"],
                         clock=lambda: BASE + 5 * 86400) == 0
    return out


@pytest.fixture
def export_file(tmp_path):
    return _export(_make_db(tmp_path / "s.sqlite3"), tmp_path / "e.jsonl")


def _run(argv, *, clock=lambda: NOW):
    return report.main(argv, clock=clock)


def _json(export_file, tmp_path, name="r.json"):
    out = tmp_path / name
    assert _run(["--in", str(export_file), "--out", str(out), "--format", "json"]) == 0
    return json.loads(out.read_text())


def test_counts_match_the_independent_answer_key(export_file, tmp_path):
    rep = _json(export_file, tmp_path)
    cov = rep["coverage"]
    assert (cov["answers"], cov["ratings"], cov["rated_answers"]) == (7, 6, 6)
    assert cov["rated_share"] == round(6 / 7, 4) and cov["first_date"] == "2026-09-21"
    by = rep["ratings_by"]
    assert by["rating"] == {"bad": 3, "good": 2, "neutral": 1}
    assert by["reason"] == {"other": 1, "too_slow": 1, "wrong": 2}
    assert by["verification_state"] == {"accepted": 2, "insufficient_evidence": 1,
                                        "other": 1, "unknown": 1, "unverified_draft": 1}
    assert by["query_kind"] == {"identifier": 1, "nl": 3, "other": 1, "unknown": 1}
    assert by["reasoning_effort"] == {"low": 1, "other": 1, "unknown": 2, "xhigh": 2}
    assert by["endpoint"] == {"console": 6}
    assert by["date"] == {"2026-09-21": 6}
    assert by["model_profile"]["llm_model"] == {"other": 1, "qwen-27b": 3, "unknown": 2}
    assert by["model_profile"]["release"] == {"abcdef123456": 2, "unknown": 4}
    cl = rep["failure_clusters"]
    assert cl["reasoning_effort"]["xhigh"] == {"n": 2, "bad": 1, "bad_share": 0.5, "low_n": True}
    assert cl["units_omitted"]["gt0"]["bad"] == 1 and cl["units_omitted"]["0"]["n"] == 2
    assert cl["units_omitted"]["unknown"]["n"] == 3
    assert cl["has_script"]["true"]["n"] == 1 and cl["evidence"]["zero"]["bad"] == 1
    assert cl["query_kind_x_verification_state"]["nl/accepted"]["n"] == 2
    top = rep["top_failure_codes"]
    assert top["reason"] == [["wrong", 2], ["too_slow", 1]]
    assert top["verification_state"][0][1] == 1
    num = rep["numeric_bad_vs_good"]["elapsed_ms"]
    assert num["bad"]["n"] == 2 and num["bad"]["median"] == 250.0 and num["good"]["median"] == 200.0
    q = rep["review_queue"]
    assert q["total_bad"] == 3
    assert {i["request_id"] for i in q["items"]} == {"000000000001", "000000000003", "000000000006"}
    assert all(i["trace_expected"] for i in q["items"])
    assert rep["export"] == {"exported_date": "2026-09-26", "retention_days": 180, "stale": False}


@pytest.mark.parametrize("fmt", ["md", "json"])
def test_no_free_text_reaches_output(export_file, tmp_path, capsys, fmt):
    out = tmp_path / f"r.{fmt}"
    assert _run(["--in", str(export_file), "--out", str(out), "--format", fmt]) == 0
    assert _run(["--in", str(export_file), "--format", fmt]) == 0
    text = out.read_text() + capsys.readouterr().out
    assert SENTINEL not in text and "free text" not in text
    assert "SYNTHETIC" not in text and "console" in text  # labels still present
    # the file really contained the sentinel (the test would be vacuous otherwise)
    assert SENTINEL.encode() in export_file.read_bytes()


def test_output_is_deterministic_and_private(export_file, tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    for p in (a, b):
        assert _run(["--in", str(export_file), "--out", str(p)]) == 0
    assert a.read_bytes() == b.read_bytes()
    assert (a.stat().st_mode & 0o777) == 0o600
    # hash of a rebuilt export of the same store is also stable
    again = _export(tmp_path / "s.sqlite3", tmp_path / "e2.jsonl")
    assert hashlib.sha256(again.read_bytes()).digest() == hashlib.sha256(
        export_file.read_bytes()).digest()


def test_vocabularies_match_their_owners():
    assert set(report.VERIFICATION_STATES) == set(AGENT_STATES)
    assert set(REASONING_EFFORTS) == {"low", "medium", "high", "xhigh"}


def test_identity_labels_are_capped_and_bucketed(tmp_path):
    rows = [(f"{i:012x}", {"llm_model": f"m{i}"}, "good", []) for i in range(1, 13)]
    rows.append(("0000000000ff", {"llm_model": "with space"}, "bad", []))
    out = _json(_export(_make_db(tmp_path / "m.sqlite3", rows), tmp_path / "m.jsonl"), tmp_path)
    models = out["ratings_by"]["model_profile"]["llm_model"]
    assert len(models) == 9 and models["other"] == 5 and sum(models.values()) == 13


def test_empty_and_unrated_exports(tmp_path, capsys):
    empty = _export(_make_db(tmp_path / "z.sqlite3", []), tmp_path / "z.jsonl")
    rep = _json(empty, tmp_path)
    assert rep["coverage"]["answers"] == 0 and rep["coverage"]["first_date"] is None
    assert rep["coverage"]["low_n"] is True and rep["review_queue"]["items"] == []
    assert _run(["--in", str(empty)]) == 0
    unrated = _export(_make_db(tmp_path / "u.sqlite3", [ROWS[4]]), tmp_path / "u.jsonl")
    assert _json(unrated, tmp_path, "u.json")["ratings_by"]["rating"] == {}


def test_stale_and_trace_expiry_follow_the_clock(export_file, tmp_path):
    late = BASE + 5 * 86400 + 181 * 86400
    out = tmp_path / "late.json"
    assert _run(["--in", str(export_file), "--out", str(out), "--format", "json"],
                clock=lambda: late) == 0
    rep = json.loads(out.read_text())
    assert rep["export"]["stale"] is True
    assert not any(i["trace_expected"] for i in rep["review_queue"]["items"])


def test_review_queue_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "QUEUE_LIMIT", 2)
    rows = [(f"{i:012x}", {}, "bad", []) for i in range(1, 6)]
    rep = _json(_export(_make_db(tmp_path / "q.sqlite3", rows), tmp_path / "q.jsonl"), tmp_path)
    q = rep["review_queue"]
    assert (q["total_bad"], q["listed"], q["truncated"]) == (5, 2, 3)


def _lines(path):
    return path.read_bytes().split(b"\n")[:-1]


def _rehash(lines):
    body = b"".join(line + b"\n" for line in lines[:-1])
    t = json.loads(lines[-1])
    t["sha256"] = hashlib.sha256(body).hexdigest()
    return body + json.dumps(t, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _mut(lines, idx, **changes):
    row = json.loads(lines[idx])
    row.update(changes)
    out = list(lines)
    out[idx] = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
    return out


@pytest.mark.parametrize("case,code", [
    ("flipped_byte", "bad_hash"), ("truncated", "no_trailer"), ("version_2", "unsupported_version"),
    ("unknown_field", "invalid_answer_line_2"), ("bad_enum", "invalid_answer_line_2"),
    ("count_mismatch", "count_mismatch"), ("dangling_rating", "dangling_rating"),
    ("future_timestamp", "invalid_answer_line_2")])
def test_hostile_inputs_are_refused_with_import_codes(export_file, tmp_path, capsys, case, code):
    lines = _lines(export_file)
    r = next(i for i, ln in enumerate(lines) if b'"kind":"rating"' in ln)
    data = {
        "flipped_byte": export_file.read_bytes().replace(b"qwen", b"qwez"),
        "truncated": export_file.read_bytes()[:-40],
        "version_2": _rehash(_mut(lines, 0, version=2)),
        "unknown_field": _rehash(_mut(lines, 1, extra=SENTINEL)),
        "bad_enum": _rehash(_mut(lines, 1, endpoint=SENTINEL)),
        "count_mismatch": _rehash(_mut(lines, -1, answers=99)),
        "dangling_rating": _rehash(_mut(lines, r, request_id="0000000000ff")),
        "future_timestamp": _rehash(_mut(lines, 1, created_at=BASE * 3)),
    }[case]
    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(data)
    bad.chmod(0o600)
    out = tmp_path / "o.md"
    assert _run(["--in", str(bad), "--out", str(out)]) == 2
    cap = capsys.readouterr()
    assert cap.err == f"feedback-report: refused: {code}\n" and cap.out == ""
    assert not out.exists() and SENTINEL not in cap.err


def test_group_readable_input_unreadable_and_existing_output_are_refused(export_file, tmp_path,
                                                                         capsys):
    export_file.chmod(0o644)
    assert _run(["--in", str(export_file)]) == 2
    assert "insecure_permissions" in capsys.readouterr().err
    export_file.chmod(0o600)
    assert _run(["--in", str(tmp_path / "missing")]) == 2
    assert "input_unreadable" in capsys.readouterr().err
    out = tmp_path / "exists.md"
    out.write_text("keep")
    assert _run(["--in", str(export_file), "--out", str(out)]) == 2
    assert "output_exists" in capsys.readouterr().err and out.read_text() == "keep"
    link = tmp_path / "link.md"
    link.symlink_to(tmp_path / "target.md")
    assert _run(["--in", str(export_file), "--out", str(link)]) == 2
    assert not (tmp_path / "target.md").exists()


def test_output_inside_a_git_worktree_is_refused(export_file, tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert _run(["--in", str(export_file), "--out", str(repo / "r.md")]) == 2
    assert "output_in_git_worktree" in capsys.readouterr().err
    assert not (repo / "r.md").exists()


def test_module_runs_as_a_subprocess_and_stdin_input(export_file, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.run([sys.executable, "-m", "mainframe_rag.feedback.report", "--in", "-",
                           "--format", "json"], input=export_file.read_bytes(), env=env,
                          capture_output=True, check=False)
    assert proc.returncode == 0 and json.loads(proc.stdout)["coverage"]["answers"] == 7
    assert SENTINEL.encode() not in proc.stdout + proc.stderr
