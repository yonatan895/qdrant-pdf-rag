"""Human spot-check and adjudication workflow (issue #1317, PR 2).

Hermetic end-to-end tests over original synthetic points and a fake model:
gold_candidates -> spot_check export -> record -> report -> promote ->
datasets.load_golden -> eval.retrieval scoring -> replay_sweep attribution.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from mainframe_rag.eval import datasets as ds
from mainframe_rag.eval import gold_candidates as gc
from mainframe_rag.eval import retrieval as ret
from mainframe_rag.eval import spot_check as sc
from mainframe_rag.retrieve.query import _to_hit
from tests.test_gold_candidates import (
    PASSAGE_MARK,
    QUESTION_MARK,
    ModelDouble,
    ReaderDouble,
    _point,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_candidates(tmp_path: Path, count: int = 8) -> tuple[Path, ReaderDouble]:
    points = [
        _point("DOC-A", i, "narrative", heading="Part A > Valves")
        for i in range(4)
    ] + [
        _point("DOC-B", i, "syntax", heading="Part B > Commands")
        for i in range(4)
    ]
    reader = ReaderDouble(points)
    out = tmp_path / "candidates.jsonl"
    code = gc.main(
        [
            "--out", str(out),
            "--collection", "synthetic",
            "--count", str(count),
            "--seed", "11",
            "--classes", "diagnostic,syntax",
            "--model-label", "fake-model",
        ],
        client=reader,
        model=ModelDouble(),
    )
    assert code == 0
    return out, reader


def _review_all(review_path: Path, verdicts: dict[int, str], reviewer: str = "alice", date: str = "2026-11-21"):
    lines = [json.loads(line) for line in review_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    items = [rec for rec in lines if rec["record"] == "spot_check_item"]
    for idx, item in enumerate(items):
        verdict = verdicts.get(idx, "accept")
        rc = sc.main([
            "record",
            "--review", str(review_path),
            "--id", item["id"],
            "--verdict", verdict,
            "--reviewer", reviewer,
            "--reviewed-at", date,
            *(["--reason", "ambiguous passage"] if verdict == "reject" else []),
        ])
        assert rc == 0
    return items


# -------------------------------------------------- full flywheel round-trip
def test_full_flywheel_roundtrip_adjudicated_and_spot_checked_modes(tmp_path, capsys, monkeypatch):
    """Real producer-to-consumer round trip:
    generate -> export -> record -> report -> promote -> load_golden ->
    eval.retrieval.score_entry/summarize -> replay_sweep scoring & attribution."""
    from scripts.capture_pool import legs_to_record
    from scripts.replay_sweep import SweepConfig, attribute_config, run_config

    from tests.helpers_eval_local import eval_settings

    monkeypatch.delenv("VENUE", raising=False)
    candidates, reader = _make_candidates(tmp_path, count=8)
    capsys.readouterr()
    review = tmp_path / "review.jsonl"

    # 1. Export stratified sample of 4 of 8 candidates.
    assert sc.main([
        "export",
        "--candidates", str(candidates),
        "--out", str(review),
        "--sample", "4",
        "--seed", "7",
    ]) == 0
    export_out = capsys.readouterr().out
    assert QUESTION_MARK not in export_out and PASSAGE_MARK not in export_out
    assert stat.S_IMODE(review.stat().st_mode) == 0o600
    export_summary = json.loads(export_out)
    assert export_summary["sample_n"] == 4 and export_summary["total_candidates"] == 8
    assert set(export_summary["by_class"]) == {"diagnostic", "syntax"}

    # Review file itself is refused by golden readers without leaking question text.
    with pytest.raises(ds.CandidateDatasetError) as refused:
        ds.load_golden(review)
    assert str(refused.value) == ds.SPOT_CHECK_REFUSAL
    assert QUESTION_MARK not in str(refused.value)

    # 2. Record 4/4 accepts (100% >= 85%).
    items = _review_all(review, {})
    assert stat.S_IMODE(review.stat().st_mode) == 0o600
    rec_out = capsys.readouterr().out
    assert QUESTION_MARK not in rec_out and PASSAGE_MARK not in rec_out

    # 3. Report passes (min-sample=4 for this 4-row sample).
    assert sc.main([
        "report",
        "--candidates", str(candidates),
        "--review", str(review),
        "--min-sample", "4",
        "--min-accept-rate", "0.85",
    ]) == 0
    report_text = capsys.readouterr().out
    assert QUESTION_MARK not in report_text and PASSAGE_MARK not in report_text
    report = json.loads(report_text)
    assert report["verdict"] == "passed"
    assert report["accept_rate"] == 1.0
    assert report["accepted"] == 4 and report["rejected"] == 0 and report["unreviewed_n"] == 0
    assert report["reviewers"] == ["alice"]

    # 4a. Promote in default 'adjudicated' mode (#806): emits ONLY the 4 reviewed-accepted rows.
    adj_path = tmp_path / "adjudicated.jsonl"
    assert sc.main([
        "promote",
        "--candidates", str(candidates),
        "--review", str(review),
        "--out", str(adj_path),
        "--min-sample", "4",
    ]) == 0
    capsys.readouterr()
    assert stat.S_IMODE(adj_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(gc.manifest_path(adj_path).stat().st_mode) == 0o600
    adj_entries = ds.load_golden(adj_path)
    assert len(adj_entries) == 4
    assert all(e.status == "adjudicated" and e.source == "generated" for e in adj_entries)
    assert all(e.reviewed_by == "alice" and e.reviewed_at == "2026-11-21" for e in adj_entries)
    assert all(e.gold_chunk_ids for e in adj_entries)
    assert {e.id for e in adj_entries} == {item["id"] for item in items}

    # 4b. Promote in explicit 'spot-checked' mode: emits all 8 rows (4 adjudicated + 4 spot_checked).
    spot_path = tmp_path / "spot_checked.jsonl"
    assert sc.main([
        "promote",
        "--candidates", str(candidates),
        "--review", str(review),
        "--out", str(spot_path),
        "--mode", "spot-checked",
        "--min-sample", "4",
    ]) == 0
    capsys.readouterr()
    spot_entries = ds.load_golden(spot_path)
    assert len(spot_entries) == 8
    assert sum(1 for e in spot_entries if e.status == "adjudicated") == 4
    assert sum(1 for e in spot_entries if e.status == "spot_checked") == 4
    # Individual reviewer fields are never fabricated on unreviewed spot_checked rows.
    for e in spot_entries:
        assert e.source == "generated"
        if e.status == "spot_checked":
            assert e.reviewed_by is None and e.reviewed_at is None and e.reviewers == ["alice"]

    # 5. Score promoted entries through eval.retrieval and replay_sweep attribution.
    by_id = {p.id: p for p in reader.points}
    scored_rows = []
    pool_records = []
    for entry in adj_entries:
        src = by_id[entry.gold_chunk_ids[0]]
        hit = _to_hit(SimpleNamespace(id=src.id, payload=src.payload, score=1.0), 1.0)
        scored_rows.append({**ret.score_entry([hit], entry), "kind": "nl"})
        pool_records.append(legs_to_record(
            entry.query, "nl",
            [{"effective_text": entry.query, "dense": [src], "sparse": []}],
            {}, {"depth": 40},
        ))
    eval_report = ret.summarize(scored_rows, failures=0, elapsed_s=0.1, embed_mode="hash", collection="c")
    assert eval_report["passage"]["checked"] == 4
    assert eval_report["passage"]["chunk"]["recall@1"] == 1.0
    assert eval_report["passage"]["section"]["recall@1"] == 1.0

    entries_by_query = {e.query: e for e in adj_entries}
    sweep = run_config(pool_records, entries_by_query, SweepConfig(label="prod"), eval_settings())
    assert sweep["passage"]["chunk"]["recall@1"] == 1.0
    attr = attribute_config(pool_records, entries_by_query, SweepConfig(label="16/5", limit=16, max_per_doc=5), eval_settings())
    assert attr["counts"]["hit"] == 4 and attr["counts"]["unclassifiable"] == 0


# ----------------------------------------- gating: incomplete & failed batches
def test_incomplete_and_failed_spot_checks_cannot_promote(tmp_path, capsys):
    candidates, _ = _make_candidates(tmp_path, count=8)
    review = tmp_path / "review.jsonl"
    assert sc.main(["export", "--candidates", str(candidates), "--out", str(review), "--sample", "4", "--seed", "1"]) == 0
    capsys.readouterr()

    # Review only 2 of 4 -> incomplete -> report exits 1, promote writes nothing and exits 1.
    items = [json.loads(line) for line in review.read_text(encoding="utf-8").splitlines() if line.strip()][1:]
    for item in items[:2]:
        assert sc.main([
            "record", "--review", str(review), "--id", item["id"],
            "--verdict", "accept", "--reviewer", "alice", "--reviewed-at", "2026-11-21",
        ]) == 0
    capsys.readouterr()

    assert sc.main(["report", "--candidates", str(candidates), "--review", str(review), "--min-sample", "4"]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["verdict"] == "incomplete" and rep["unreviewed_n"] == 2

    out = tmp_path / "must-not-exist.jsonl"
    assert sc.main(["promote", "--candidates", str(candidates), "--review", str(review), "--out", str(out), "--min-sample", "4"]) == 1
    assert not out.exists() and not gc.manifest_path(out).exists()
    capsys.readouterr()

    # Finish remaining 2 as reject -> 2/4 = 50% < 85% -> failed -> promote writes nothing and exits 1.
    for item in items[2:]:
        assert sc.main([
            "record", "--review", str(review), "--id", item["id"],
            "--verdict", "reject", "--reviewer", "alice", "--reviewed-at", "2026-11-21",
        ]) == 0
    capsys.readouterr()

    assert sc.main(["report", "--candidates", str(candidates), "--review", str(review), "--min-sample", "4"]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["verdict"] == "failed" and rep["accept_rate"] == 0.5
    assert sc.main(["promote", "--candidates", str(candidates), "--review", str(review), "--out", str(out), "--min-sample", "4"]) == 1
    assert not out.exists() and not gc.manifest_path(out).exists()


def test_spot_checked_mode_always_excludes_rejected_rows(tmp_path, capsys):
    candidates, _ = _make_candidates(tmp_path, count=8)
    review = tmp_path / "review.jsonl"
    assert sc.main(["export", "--candidates", str(candidates), "--out", str(review), "--sample", "5", "--seed", "2"]) == 0
    capsys.readouterr()
    # 4 accept + 1 reject = 80% -> passes with --min-accept-rate 0.80, but rejected row must be excluded!
    items = _review_all(review, {0: "reject"})
    rejected_id = items[0]["id"]
    out = tmp_path / "spot.jsonl"
    assert sc.main([
        "promote",
        "--candidates", str(candidates),
        "--review", str(review),
        "--out", str(out),
        "--mode", "spot-checked",
        "--min-sample", "5",
        "--min-accept-rate", "0.80",
    ]) == 0
    entries = ds.load_golden(out)
    assert len(entries) == 7
    assert rejected_id not in {e.id for e in entries}
    manifest = json.loads(gc.manifest_path(out).read_text(encoding="utf-8"))
    assert manifest["excluded_rejected_n"] == 1
    assert manifest["adjudicated_n"] == 4 and manifest["spot_checked_n"] == 3


def test_too_small_sample_is_incomplete_even_at_100_percent_accept(tmp_path, capsys):
    candidates, _ = _make_candidates(tmp_path, count=8)
    review = tmp_path / "review.jsonl"
    assert sc.main(["export", "--candidates", str(candidates), "--out", str(review), "--sample", "2", "--seed", "3"]) == 0
    _review_all(review, {})
    capsys.readouterr()
    # Default min_sample is 20 -> required_sample = min(20, 8) = 8; 2 reviewed is incomplete!
    assert sc.main(["report", "--candidates", str(candidates), "--review", str(review)]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["verdict"] == "incomplete" and rep["required_sample"] == 8 and rep["reviewed_n"] == 2


# ----------------------------------- integrity, privacy & refusal edge cases
def test_tampering_candidates_after_export_refuses_report_and_promote(tmp_path, capsys):
    candidates, _ = _make_candidates(tmp_path, count=4)
    review = tmp_path / "review.jsonl"
    assert sc.main(["export", "--candidates", str(candidates), "--out", str(review), "--sample", "4", "--seed", "1"]) == 0
    _review_all(review, {})
    capsys.readouterr()

    # Mutate candidates file after export (and update its manifest to isolate review binding check).
    raw = candidates.read_text(encoding="utf-8") + "\n"
    candidates.write_text(raw, encoding="utf-8")
    mpath = gc.manifest_path(candidates)
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    manifest["rows_sha256"] = hashlib.sha256(candidates.read_bytes()).hexdigest()
    mpath.write_text(json.dumps(manifest), encoding="utf-8")

    assert sc.main(["report", "--candidates", str(candidates), "--review", str(review), "--min-sample", "4"]) == 2
    assert sc.REFUSALS["candidates_mismatch"] in capsys.readouterr().err
    out = tmp_path / "promoted.jsonl"
    assert sc.main(["promote", "--candidates", str(candidates), "--review", str(review), "--out", str(out), "--min-sample", "4"]) == 2
    assert not out.exists()


def test_record_refuses_unknown_id_duplicate_without_overwrite_and_bad_date(tmp_path, capsys):
    candidates, _ = _make_candidates(tmp_path, count=4)
    review = tmp_path / "review.jsonl"
    assert sc.main(["export", "--candidates", str(candidates), "--out", str(review), "--sample", "2", "--seed", "1"]) == 0
    items = [json.loads(line) for line in review.read_text(encoding="utf-8").splitlines() if line.strip()][1:]
    capsys.readouterr()

    # Unknown id
    assert sc.main([
        "record", "--review", str(review), "--id", "GEN-NOPE",
        "--verdict", "accept", "--reviewer", "alice", "--reviewed-at", "2026-11-21",
    ]) == 2
    assert sc.REFUSALS["unknown_id"] in capsys.readouterr().err

    # Bad date
    assert sc.main([
        "record", "--review", str(review), "--id", items[0]["id"],
        "--verdict", "accept", "--reviewer", "alice", "--reviewed-at", "2026-02-30",
    ]) == 2
    assert sc.REFUSALS["invalid_date"] in capsys.readouterr().err

    # First record succeeds; second without --overwrite refuses; with --overwrite updates.
    assert sc.main([
        "record", "--review", str(review), "--id", items[0]["id"],
        "--verdict", "accept", "--reviewer", "alice", "--reviewed-at", "2026-11-21",
    ]) == 0
    assert sc.main([
        "record", "--review", str(review), "--id", items[0]["id"],
        "--verdict", "reject", "--reviewer", "bob", "--reviewed-at", "2026-11-22",
    ]) == 2
    assert sc.REFUSALS["already_reviewed"] in capsys.readouterr().err
    assert sc.main([
        "record", "--review", str(review), "--id", items[0]["id"],
        "--verdict", "reject", "--reviewer", "bob", "--reviewed-at", "2026-11-22", "--overwrite",
    ]) == 0
    updated = [json.loads(line) for line in review.read_text(encoding="utf-8").splitlines() if line.strip()][1]
    assert updated["verdict"] == "reject" and updated["reviewer"] == "bob"


def test_privacy_guards_refuse_git_worktree_holdout_and_release_set(tmp_path, capsys):
    candidates, _ = _make_candidates(tmp_path, count=4)
    # Output inside repo or under holdout/release names is refused with exit 2.
    for bad_out in (
        ROOT / "evals" / "spot-review.jsonl",
        tmp_path / "holdout.jsonl",
        tmp_path / "HOLDOUT.jsonl.bak",
        tmp_path / "release_set.jsonl",
    ):
        assert sc.main(["export", "--candidates", str(candidates), "--out", str(bad_out), "--sample", "2", "--seed", "1"]) == 2
        assert gc.OUTPUT_REFUSAL in capsys.readouterr().err
        assert not bad_out.exists()

    # Holdout and release_set are refused even as INPUTS regardless of VENUE.
    holdout = tmp_path / "holdout.jsonl"
    holdout.write_bytes(candidates.read_bytes())
    assert sc.main(["export", "--candidates", str(holdout), "--out", str(tmp_path / "r.jsonl"), "--sample", "2", "--seed", "1"]) == 2
    assert sc.REFUSALS["input_protected"] in capsys.readouterr().err


def test_export_with_passages_verifies_text_sha256_and_never_leaks_to_stdout(tmp_path, capsys):
    candidates, reader = _make_candidates(tmp_path, count=4)
    review = tmp_path / "review-passages.jsonl"
    assert sc.main(
        [
            "export",
            "--candidates", str(candidates),
            "--out", str(review),
            "--sample", "2",
            "--seed", "4",
            "--with-passages",
            "--collection", "synthetic",
        ],
        client=reader,
    ) == 0
    out = capsys.readouterr().out
    assert PASSAGE_MARK not in out and QUESTION_MARK not in out
    items = [json.loads(line) for line in review.read_text(encoding="utf-8").splitlines() if line.strip()][1:]
    assert all(PASSAGE_MARK in item["passage_text"] for item in items)

    # Corrupt stored passage text of a sampled chunk -> sha256 mismatch refuses before writing!
    target_cid = items[0]["gold_chunk_ids"][0]
    for pt in reader.points:
        if pt.id == target_cid:
            pt.payload["text"] = "tampered text that does not match sha256"
    bad_review = tmp_path / "review-tampered.jsonl"
    assert sc.main(
        [
            "export",
            "--candidates", str(candidates),
            "--out", str(bad_review),
            "--sample", "4",
            "--seed", "4",
            "--with-passages",
            "--collection", "synthetic",
        ],
        client=reader,
    ) == 2
    assert sc.REFUSALS["passage_mismatch"] in capsys.readouterr().err
    assert not bad_review.exists()


def test_record_refuses_symlink_and_git_worktree_review_file(tmp_path, capsys):
    candidates, _ = _make_candidates(tmp_path, count=4)
    review = tmp_path / "review.jsonl"
    assert sc.main(["export", "--candidates", str(candidates), "--out", str(review), "--sample", "2", "--seed", "1"]) == 0
    items = [json.loads(line) for line in review.read_text(encoding="utf-8").splitlines() if line.strip()][1:]
    capsys.readouterr()

    link = tmp_path / "link-review.jsonl"
    link.symlink_to(review)
    assert sc.main([
        "record", "--review", str(link), "--id", items[0]["id"],
        "--verdict", "accept", "--reviewer", "alice", "--reviewed-at", "2026-11-21",
    ]) == 2
    assert sc.REFUSALS["review_unsafe_path"] in capsys.readouterr().err

    # Also refuse a review file inside a directory containing a .git marker.
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / ".git").mkdir()
    in_repo = fake_repo / "review.jsonl"
    in_repo.write_bytes(review.read_bytes())
    assert sc.main([
        "record", "--review", str(in_repo), "--id", items[0]["id"],
        "--verdict", "accept", "--reviewer", "alice", "--reviewed-at", "2026-11-21",
    ]) == 2
    assert sc.REFUSALS["review_unsafe_path"] in capsys.readouterr().err


def test_no_tracked_eval_set_holds_generated_rows():
    for name in ("golden.jsonl", "paraphrase.jsonl", "sections.jsonl", "expert_golden_seed.jsonl"):
        for line in (ROOT / "evals" / name).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assert json.loads(line).get("source") != "generated"


def test_export_with_passages_refuses_real_corpus_collection_and_alias_outside_rc(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("VENUE", raising=False)
    candidates, reader = _make_candidates(tmp_path, count=4)
    capsys.readouterr()
    reader.aliases["prod_alias"] = "real_manuals"
    for coll in ("real_manuals", "prod_alias"):
        out = tmp_path / f"rev-{coll}.jsonl"
        assert sc.main(
            [
                "export", "--candidates", str(candidates), "--out", str(out),
                "--sample", "2", "--seed", "1", "--with-passages", "--collection", coll,
            ],
            client=reader,
        ) == 2
        assert sc.REFUSALS["venue"] in capsys.readouterr().err
        assert not out.exists()
