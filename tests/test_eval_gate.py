"""Unit tests for the retrieval evaluation regression gate (pure functions, no docker/network)."""

import hashlib
import json
from pathlib import Path

from scripts.eval_retrieval import (
    _get,
    _set,
    check_baseline,
    main,
    update_baseline,
)

from mainframe_rag.config import Settings


def _report() -> dict:
    return {
        "n": 12,
        "scored": 12,
        "must_not": {"checked": 12, "violations": 0},
        "failures": 0,
        "elapsed_s": 0.25,
        "embed_mode": "hash",
        "collection": "test-corpus",
        "recall@1": 0.5,
        "recall@3": 0.75,
        "recall@5": 0.75,
        "recall@8": 0.80,
        "mrr": 0.625,
        "ndcg@8": 0.70,
        "identifier": {
            "recall@1": 1.0,
            "recall@5": 1.0,
            "recall@8": 1.0,
            "mrr": 1.0,
            "ndcg@8": 1.0,
        },
        "nl": {
            "recall@1": 0.333,
            "recall@5": 0.667,
            "recall@8": 0.70,
            "mrr": 0.5,
            "ndcg@8": 0.60,
        },
        "rows": [],
    }


def test_get_and_set_round_trip():
    doc: dict = {}
    _set(doc, "identifier.recall@1", 1.0)
    assert doc == {"identifier": {"recall@1": 1.0}}
    assert _get(doc, "identifier.recall@1") == 1.0
    assert _get(doc, "identifier.missing") is None
    assert _get(doc, "identifier.recall@1.sub") is None


def test_update_baseline_writes_nested_schema(tmp_path: Path):
    path = tmp_path / "baseline.json"
    rep = _report()
    update_baseline(rep, path)
    baseline = json.loads(path.read_text(encoding="utf-8"))

    assert baseline["recall@1"] == 0.5
    assert baseline["recall@5"] == 0.75
    assert baseline["mrr"] == 0.625
    assert baseline["identifier"]["recall@1"] == 1.0
    assert baseline["_meta"]["n"] == 12

    # Perfect match produces 0 regressions
    assert check_baseline(rep, baseline) == []


def test_check_baseline_detects_recall_regression():
    baseline = {
        "recall@1": 0.5,
        "recall@5": 0.80,
        "mrr": 0.60,
        "identifier": {"recall@1": 1.0},
    }
    # Current recall@5 is 0.70 < 0.80 * 0.95 (0.76)
    rep = _report()
    rep["recall@5"] = 0.70
    regressions = check_baseline(rep, baseline)
    assert len(regressions) == 1
    assert "recall@5" in regressions[0]


def test_check_baseline_detects_identifier_drop():
    baseline = {
        "recall@1": 0.5,
        "recall@5": 0.75,
        "mrr": 0.60,
        "identifier": {"recall@1": 1.0},
    }
    # Identifier recall must never drop (min ratio 1.0)
    rep = _report()
    rep["identifier"]["recall@1"] = 0.90
    regressions = check_baseline(rep, baseline)
    assert len(regressions) == 1
    assert "identifier.recall@1" in regressions[0]


def test_check_baseline_detects_query_failures():
    baseline = {
        "recall@1": 0.5,
        "recall@5": 0.75,
        "mrr": 0.60,
        "identifier": {"recall@1": 1.0},
    }
    rep = _report()
    rep["failures"] = 2
    regressions = check_baseline(rep, baseline)
    assert any("failures: 2 > 0" in r for r in regressions)


def test_check_baseline_none_baseline_passes():
    rep = _report()
    assert check_baseline(rep, None) == []


# --- main() exit-code contract (issue #159): a skipped gate is not a pass ---

def _hermetic_main(monkeypatch, tmp_path: Path, **settings_kwargs) -> Path:
    """Drive eval_retrieval.main() with evaluate() canned and settings
    patched: no Qdrant, no embedder, no repo-file manifest writes."""
    import scripts.eval_retrieval as ev

    golden = tmp_path / "golden.jsonl"
    golden.write_text((Path("evals/golden.jsonl").read_text().splitlines()[0]) + "\n")
    settings = Settings(embed_mode="hash", qdrant_collection="test-corpus", _env_file=None, **settings_kwargs)
    monkeypatch.setattr(ev, "evaluate", lambda golden_entries, s: _report())
    monkeypatch.setattr(ev, "load_settings", lambda: settings)
    monkeypatch.setattr(ev, "write_run_manifest", lambda *a, **k: {"git_sha": "test"})
    return golden


def test_main_exit_2_when_collection_mismatch(tmp_path, monkeypatch, capfd):
    golden = _hermetic_main(monkeypatch, tmp_path)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"_meta": {"collection": "other-corpus", "embed_mode": "hash"}}))
    summary = tmp_path / "summary.md"
    rc = main(["--golden", str(golden), "--check", str(baseline), "--summary", str(summary)])
    assert rc == 2
    assert "skipping gate (different corpora)" in capfd.readouterr().err
    assert summary.read_text().startswith("## Retrieval eval")  # artifacts still written


def test_main_exit_2_when_check_file_missing(tmp_path, monkeypatch, capfd):
    golden = _hermetic_main(monkeypatch, tmp_path)
    rc = main(["--golden", str(golden), "--check", str(tmp_path / "nope.json")])
    assert rc == 2
    assert "cannot be applied" in capfd.readouterr().err


def test_main_exit_2_when_holdout_without_rc_declaration(tmp_path, monkeypatch, capfd):
    """The frozen holdout is an RC instrument (issue #268): a dev run that
    points at it fails closed instead of tuning against it."""
    _hermetic_main(monkeypatch, tmp_path)
    monkeypatch.delenv("VENUE", raising=False)
    rc = main(["--golden", "evals/holdout.jsonl", "--no-check"])
    assert rc == 2
    assert "frozen holdout" in capfd.readouterr().err


def test_main_allows_holdout_with_rc_declaration(tmp_path, monkeypatch):
    _hermetic_main(monkeypatch, tmp_path)
    monkeypatch.setenv("VENUE", "rc")
    assert main(["--golden", "evals/holdout.jsonl", "--no-check"]) == 0


def test_main_exit_0_when_gate_applied_and_green(tmp_path, monkeypatch):
    golden = _hermetic_main(monkeypatch, tmp_path)
    baseline = tmp_path / "baseline.json"
    update_baseline(_report(), baseline)  # same collection + numbers as the canned report
    rc = main(["--golden", str(golden), "--check", str(baseline)])
    assert rc == 0


def test_main_exit_0_when_no_gate_requested(tmp_path, monkeypatch):
    golden = _hermetic_main(monkeypatch, tmp_path)
    assert main(["--golden", str(golden), "--no-check"]) == 0


def test_main_exit_1_when_query_failures_despite_skip(tmp_path, monkeypatch):
    """Failures dominate the skip signal: both are job-failing, but real
    query errors must keep the more specific verdict."""
    import scripts.eval_retrieval as ev

    golden = _hermetic_main(monkeypatch, tmp_path)
    rep = _report() | {"failures": 2}
    monkeypatch.setattr(ev, "evaluate", lambda golden_entries, s: rep)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"_meta": {"collection": "other-corpus", "embed_mode": "hash"}}))
    assert main(["--golden", str(golden), "--check", str(baseline)]) == 1


def test_load_golden_validates_and_rejects_empty(tmp_path: Path):
    from scripts.eval_retrieval import load_golden, score_entry

    from mainframe_rag.retrieve.query import SearchHit

    # Valid file with comments and blanks
    golden_file = tmp_path / "valid_golden.jsonl"
    golden_file.write_text(
        "# Comment line\n\n"
        '{"query": "IEA500I message", "expected_doc_ids": ["SC14-7315-70"], "expected_heading": "Chapter 2"}\n'
        '{"query": "LFAREA parmlib", "expected_doc_ids": ["SA22-7592-05"]}\n'
    )
    entries = load_golden(golden_file)
    assert len(entries) == 2
    assert entries[0].query == "IEA500I message"
    assert entries[0].expected_doc_ids == ["SC14-7315-70"]
    assert entries[0].expected_heading == "Chapter 2"
    assert entries[1].query == "LFAREA parmlib"

    # Score entry against SearchHit
    hit1 = SearchHit(
        chunk_id="c1",
        score=0.9,
        cite="SC14-7315-70 Manual, p. 1",
        heading="Chapter 2 > IEA500I",
        text="Sample text",
        doc_id="SC14-7315-70",
        title="Manual",
        page_label="1",
        chunk_type="narrative",
        message_ids=("IEA500I",),
    )
    score = score_entry([hit1], entries[0])
    assert score["recall@1"] == 1.0
    assert score["recall@8"] == 1.0
    assert score["mrr"] == 1.0
    assert score["ndcg@8"] == 1.0

    # Reject empty query
    bad1 = tmp_path / "bad1.jsonl"
    bad1.write_text('{"query": "", "expected_doc_ids": ["SC14-7315-70"]}\n')
    import pytest
    with pytest.raises(SystemExit):
        load_golden(bad1)

    # Reject empty expected_doc_ids
    bad2 = tmp_path / "bad2.jsonl"
    bad2.write_text('{"query": "valid query", "expected_doc_ids": []}\n')
    with pytest.raises(SystemExit):
        load_golden(bad2)


def test_check_baseline_detects_recall8_regression():
    baseline = {
        "recall@8": 0.85,
    }
    rep = _report()
    rep["recall@8"] = 0.70  # 0.70 < 0.85 * 0.95 (0.8075)
    regressions = check_baseline(rep, baseline)
    assert len(regressions) == 1
    assert "recall@8" in regressions[0]


def test_check_baseline_detects_ndcg8_regression():
    baseline = {
        "ndcg@8": 0.80,
    }
    rep = _report()
    rep["ndcg@8"] = 0.65  # 0.65 < 0.80 * 0.95 (0.76)
    regressions = check_baseline(rep, baseline)
    assert len(regressions) == 1
    assert "ndcg@8" in regressions[0]


def test_gain_and_ndcg_at_k_calculation():
    from scripts.eval_retrieval import GoldenEntry, gain, ndcg_at_k

    from mainframe_rag.retrieve.query import SearchHit

    entry = GoldenEntry(
        query="test query",
        expected_doc_ids=["DOC-1", "DOC-2"],
        expected_heading="Overview",
        expected_page="5",
    )

    # Perfect hit: doc + heading + page -> gain = 3
    assert gain("DOC-1", "Overview", "5", entry) == 3
    # Doc + heading -> gain = 2
    assert gain("DOC-1", "Overview", "6", entry) == 2
    # Doc only -> gain = 1
    assert gain("DOC-1", "Different", "6", entry) == 1
    # Irrelevant doc -> gain = 0
    assert gain("DOC-99", "Overview", "5", entry) == 0

    h1 = SearchHit(chunk_id="1", score=1.0, cite="c", heading="Overview", text="t",
                   doc_id="DOC-1", title="T", page_label="5", chunk_type="narrative", message_ids=())
    h2 = SearchHit(chunk_id="2", score=0.9, cite="c", heading="Detail", text="t",
                   doc_id="DOC-2", title="T", page_label="10", chunk_type="narrative", message_ids=())

    # Ideal order: DOC-1 (gain 3) at rank 1, DOC-2 (gain 1) at rank 2 -> nDCG = 1.0
    score = ndcg_at_k([h1, h2], entry, k=8)
    assert score is not None
    assert abs(score - 1.0) < 1e-4

    # Reversed order: DOC-2 at rank 1, DOC-1 at rank 2 -> nDCG < 1.0
    score_rev = ndcg_at_k([h2, h1], entry, k=8)
    assert score_rev is not None and score_rev < 1.0


def test_generate_synthetic_golden_corpus(tmp_path: Path):
    import pymupdf
    from scripts.gate_l1 import generate_synthetic_golden_corpus

    entries = [
        {
            "id": "Q1",
            "query": "query 1",
            "expected_doc_ids": ["DOC-A"],
            "expected_heading": "Chapter 1. Title",
            "must_cite_identifier": "MSG100I",
            "gold_must_contain": ["termA"],
        },
        {
            "id": "Q2",
            "query": "query 2",
            "expected_doc_ids": ["DOC-A", "DOC-B"],
            "expected_heading": "Chapter 2. Title",
        },
    ]

    out_dir = tmp_path / "corpus"
    result = generate_synthetic_golden_corpus(entries, out_dir)
    assert result["docs_generated"] == 3  # DOC-A, DOC-B, plus generic distractor
    assert (out_dir / "DOC-A.pdf").exists()
    assert (out_dir / "DOC-B.pdf").exists()
    assert (out_dir / "generic-distractor.pdf").exists()

    doc_a = pymupdf.open(out_dir / "DOC-A.pdf")
    assert len(doc_a) >= 3  # cover + 2 sections
    text = "".join(page.get_text() for page in doc_a)
    assert "query 1" in text
    assert "MSG100I" in text
    assert "termA" in text
    doc_a.close()


# --- gate_l1 fail-closed contract (issue #267): a skipped gate is not a pass ---

def _tmp_golden(tmp_path: Path) -> Path:
    golden = tmp_path / "golden.jsonl"
    golden.write_text(Path("evals/golden.jsonl").read_text(encoding="utf-8").splitlines()[0] + "\n")
    return golden


def test_gate_missing_baseline_exits_2(tmp_path: Path, capfd):
    from scripts.gate_l1 import run_gate

    rc, md = run_gate(golden_path=_tmp_golden(tmp_path), baseline_path=tmp_path / "missing.json")

    assert rc == 2
    assert "cannot be applied" in capfd.readouterr().err
    assert "**ERROR:**" in md


def test_gate_non_hash_baseline_exits_2(tmp_path: Path, capfd):
    from scripts.gate_l1 import run_gate

    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"_meta": {"embed_mode": "vllm"}}))

    rc, _ = run_gate(golden_path=_tmp_golden(tmp_path), baseline_path=baseline)

    assert rc == 2
    assert "not a hash-mode baseline" in capfd.readouterr().err


def test_gate_missing_meta_baseline_exits_2(tmp_path: Path, capfd):
    from scripts.gate_l1 import run_gate

    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"recall@1": 1.0}))

    rc, _ = run_gate(golden_path=_tmp_golden(tmp_path), baseline_path=baseline)

    assert rc == 2
    assert "not a hash-mode baseline" in capfd.readouterr().err


def test_gate_golden_sha_mismatch_exits_2(tmp_path: Path, capfd):
    from scripts.gate_l1 import run_gate

    golden = _tmp_golden(tmp_path)
    Path(f"{golden}.sha256").write_text("0" * 64 + "  golden.jsonl\n")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"_meta": {"embed_mode": "hash"}}))

    rc, _ = run_gate(golden_path=golden, baseline_path=baseline)

    assert rc == 2
    assert "sha256 mismatch" in capfd.readouterr().err


def test_verify_golden_sha_accepts_matching_pin(tmp_path: Path):
    from scripts.gate_l1 import _verify_golden_sha

    golden = tmp_path / "g.jsonl"
    golden.write_text('{"query": "x"}\n')
    digest = hashlib.sha256(golden.read_bytes()).hexdigest()
    Path(f"{golden}.sha256").write_text(f"{digest}  g.jsonl\n")

    assert _verify_golden_sha(golden) is None


def test_repo_golden_set_is_sha_pinned():
    from scripts.gate_l1 import _verify_golden_sha

    assert _verify_golden_sha(Path("evals/golden.jsonl")) is None
    assert Path("evals/golden.jsonl.sha256").exists()


def test_expected_state_follows_behavior_and_overrides(monkeypatch):
    """Issue #365 acceptance states are authored deterministically by the
    builder: answer-tier accepted, abstain-tier insufficient_evidence, with
    an explicit adjudication override path."""
    from scripts import build_golden_corpus as bgc

    assert bgc.expected_state_for({"id": "X", "expected_behavior": "answer"}) == "accepted"
    assert (
        bgc.expected_state_for({"id": "Y", "expected_behavior": "abstain"})
        == "insufficient_evidence"
    )
    monkeypatch.setitem(bgc.EXPECTED_STATE_OVERRIDES, "X", "unverified_draft")
    assert bgc.expected_state_for({"id": "X", "expected_behavior": "answer"}) == "unverified_draft"


def test_repo_golden_rows_opt_into_acceptance_states():
    """Every committed golden and holdout row carries the issue #365
    acceptance state, consistent with its expected behavior: the holdout
    opts in through the adjudicated re-freeze."""
    for path, expected_n in (("evals/golden.jsonl", 121), ("evals/holdout.jsonl", 72)):
        rows = [
            json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == expected_n
        for row in rows:
            expected = (
                "insufficient_evidence" if row["expected_behavior"] == "abstain" else "accepted"
            )
            assert row["expected_verification_state"] == expected, f"{path}:{row['id']}"


def test_doc03_premise_correction_is_adjudicated():
    """Issue #307/#365 disposition: DOC-03's false premise expects a premise
    correction naming the real book, not a blind refusal or a confirmation of
    the asserted identity. The builder override and the committed holdout row
    must agree, and the state stays answer-tier `accepted`."""
    from scripts import build_golden_corpus as bgc

    assert bgc.GOLD_OVERRIDES["DOC-03"]["gold_must_contain"] == ["SC23-6858", "Magnetic Tapes"]
    doc03 = next(
        json.loads(line)
        for line in Path("evals/holdout.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["id"] == "DOC-03"
    )
    assert doc03["expected_behavior"] == "answer"
    assert doc03["expected_verification_state"] == "accepted"
    assert doc03["gold_must_contain"] == ["SC23-6858", "Magnetic Tapes"]
    assert "premise correction" in doc03["note"]


# --- absolute identifier gates (AGENTS.md: "identifier recall@1 strict 1.0") ---

def test_identifier_gate_is_absolute_not_ratio():
    baseline = {"identifier": {"recall@1": 0.5}}
    rep = _report()
    rep["identifier"]["recall@1"] = 0.99

    regressions = check_baseline(rep, baseline)

    assert any("identifier.recall@1" in r and "absolute gate" in r for r in regressions)


def test_message_id_gate_is_absolute_not_ratio():
    baseline = {"classes": {"message_id": {"recall@1": 0.5}}}
    rep = _report()
    rep["classes"] = {"message_id": {"recall@1": 0.99}}

    regressions = check_baseline(rep, baseline)

    assert any("classes.message_id.recall@1" in r and "absolute gate" in r for r in regressions)


def test_identifier_gates_pass_at_one():
    rep = _report()
    rep["classes"] = {"message_id": {"recall@1": 1.0}}

    assert check_baseline(rep, {}) == []


def test_real_corpus_baseline_uses_no_drop_floor():
    """Issue #286: the real-corpus baseline sits below 1.0; the gate must go
    green at its own baseline and still flag any identifier/message-id drop."""
    baseline = {
        "_meta": {"collection": "real_manuals", "embed_mode": "vllm"},
        "identifier": {"recall@1": 0.692},
        "classes": {"message_id": {"recall@1": 0.8}},
    }
    rep = _report()
    rep["identifier"]["recall@1"] = 0.692
    rep["classes"] = {"message_id": {"recall@1": 0.8}}
    assert check_baseline(rep, baseline) == []

    dropped = _report()
    dropped["identifier"]["recall@1"] = 0.65
    dropped["classes"] = {"message_id": {"recall@1": 0.75}}
    regressions = check_baseline(dropped, baseline)
    assert any("identifier.recall@1" in r and "real-corpus gate" in r for r in regressions)
    assert any(
        "classes.message_id.recall@1" in r and "real-corpus gate" in r for r in regressions
    )


def test_synthetic_baseline_keeps_absolute_floor():
    """A synthetic collection name keeps 1.0-must-mean-1.0 (no-venue-meta
    baselines keep it too; see test_identifier_gate_is_absolute_not_ratio)."""
    baseline = {"_meta": {"collection": "gate-l1-123"}, "identifier": {"recall@1": 0.5}}
    rep = _report()
    rep["identifier"]["recall@1"] = 0.99
    regressions = check_baseline(rep, baseline)
    assert any("absolute gate" in r for r in regressions)


# --- table query class (issue #270 step 4) ---------------------------------

def test_table_query_class_is_valid():
    """`table` joins the class vocabulary; unknown classes stay rejected."""
    import pydantic
    import pytest
    from scripts.eval_retrieval import QUERY_CLASSES, GoldenEntry

    assert "table" in QUERY_CLASSES
    entry = GoldenEntry(
        query="Which table lists the traced fields?", expected_doc_ids=["D"], query_class="table"
    )
    assert entry.query_class == "table"
    with pytest.raises(pydantic.ValidationError):
        GoldenEntry(query="q", expected_doc_ids=["D"], query_class="not_a_class")


def test_table_class_entries_are_authored_and_well_formed():
    """The authored TBL series binds real docs/headings for the class; the
    re-freeze that lands them in golden/holdout is a dedicated commit."""
    from scripts import build_golden_corpus as bgc

    tbl = [entry for entry in bgc.E if str(entry["id"]).startswith("TBL-")]
    assert len(tbl) == 6
    assert all(entry["query_class"] == "table" for entry in tbl)
    assert all(entry["expected_doc_ids"] and entry["expected_heading"] for entry in tbl)
    ids = [entry["id"] for entry in bgc.E]
    assert len(ids) == len(set(ids))

# Issue 367: requested gates need actual eligible observations, not just numbers.
def test_requested_gate_rejects_empty_and_abstain_only_reports():
    from scripts.eval_retrieval import GoldenEntry, score_entry, summarize

    for rows in ([], [score_entry([], GoldenEntry(query="unknown", expected_behavior="abstain"))]):
        rep = summarize(rows, failures=0, elapsed_s=0, embed_mode="hash", collection="c")
        assert check_baseline(rep, {"recall@1": 0.0})


def test_requested_gate_rejects_nonfinite_scores():
    for value in (float("nan"), float("inf"), float("-inf")):
        rep = _report()
        rep["recall@1"] = value
        assert any("recall@1" in r for r in check_baseline(rep, {"recall@1": 0.5}))


def test_requested_gate_rejects_missing_required_class():
    baseline = {"classes": {"table": {"n": 1, "scored": 1, "recall@5": 1.0}}}
    assert any("table" in r for r in check_baseline(_report(), baseline))


def test_main_exit_2_when_embed_mode_mismatch(tmp_path, monkeypatch):
    golden = _hermetic_main(monkeypatch, tmp_path)
    baseline = tmp_path / "baseline.json"
    update_baseline(_report() | {"embed_mode": "vllm"}, baseline)
    assert main(["--golden", str(golden), "--check", str(baseline)]) == 2


def test_cli_scores_actual_rows_before_deciding_gate(tmp_path, monkeypatch):
    """Keep loading, scoring, aggregation, reporting and CLI verdict real.

    Only external retrieval/model/storage boundaries are replaced. Independent
    baseline values are deliberately not generated by the scorer under test.
    """
    import qdrant_client
    import scripts.eval_retrieval as ev

    from mainframe_rag.ingest import embed, qdrant_io
    from mainframe_rag.retrieve import rerank
    from mainframe_rag.retrieve.query import SearchHit

    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **kw: object())
    monkeypatch.setattr(embed, "build_embedder", lambda settings: object())
    monkeypatch.setattr(qdrant_io, "stored_rules_version", lambda *a: None)
    monkeypatch.setattr(rerank, "build_reranker", lambda settings: None)
    monkeypatch.setattr(ev, "load_settings", lambda: Settings(embed_mode="hash", qdrant_collection="c", _env_file=None))
    monkeypatch.setattr(ev, "write_run_manifest", lambda *a, **kw: {"git_sha": "synthetic"})
    hit = SearchHit(chunk_id="original-row", doc_id="A", text="Original test evidence", score=1.0,
                    cite="A p. 1", heading="Example", title="Original", page_label="1", chunk_type="narrative", message_ids=())
    monkeypatch.setattr(ev, "retrieve_search", lambda *a, **kw: ([hit], "nl", {}))
    answer = {"query": "original question", "expected_doc_ids": ["A"], "query_class": "table"}
    abstain = {"query": "unsupported premise", "expected_behavior": "abstain", "query_class": "negative"}
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"_meta": {"collection": "c", "embed_mode": "hash"},
        "recall@1": 1.0, "recall@5": 1.0, "recall@8": 1.0, "mrr": 1.0, "ndcg@8": 1.0,
        "classes": {"table": {"n": 1, "scored": 1, "recall@1": 1.0},
                    "negative": {"n": 1, "scored": 0, "recall@1": None}}}))
    golden, output = tmp_path / "golden.jsonl", tmp_path / "report.json"
    # Failure followed by success proves the next ordinary invocation is usable.
    for rows, code in (([], 1), ([abstain], 1), ([answer], 1),
                       ([answer | {"query_class": "syntax"}, abstain], 1),
                       ([answer, abstain], 0)):
        golden.write_text("".join(json.dumps(row) + "\n" for row in rows))
        assert main(["--golden", str(golden), "--check", str(baseline), "--out", str(output)]) == code
        report = json.loads(output.read_text())
        assert report["n"] == len(rows)
        assert report["gate"]["status"] == ("passed" if code == 0 else "failed")
    assert report["scored"] == 1
    assert report["abstain"]["n"] == 1
    assert report["classes"]["negative"]["recall@1"] is None
    assert report["recall@1"] == 1.0
    golden.write_text("")
    assert main(["--golden", str(golden), "--no-check", "--out", str(output)]) == 0
    assert json.loads(output.read_text())["gate"]["status"] == "not_requested"


def test_cli_rejects_missing_and_nonfinite_required_metrics(tmp_path, monkeypatch):
    import scripts.eval_retrieval as ev

    golden = _hermetic_main(monkeypatch, tmp_path)
    baseline, output = tmp_path / "baseline.json", tmp_path / "report.json"
    update_baseline(_report(), baseline)
    for key in ("recall@1", "recall@5", "recall@8", "mrr", "ndcg@8", "identifier.recall@1", "must_not.violations"):
        for value in (None, float("nan"), float("inf"), float("-inf"), "1", True):
            report = _report()
            _set(report, key, value)
            monkeypatch.setattr(ev, "evaluate", lambda *a, report=report: report)
            assert main(["--golden", str(golden), "--check", str(baseline), "--out", str(output)]) == 1
    for value in (float("nan"), float("inf")):
        baseline.write_text(json.dumps({"recall@1": value}))
        monkeypatch.setattr(ev, "evaluate", lambda *a: _report())
        assert main(["--golden", str(golden), "--check", str(baseline), "--out", str(output)]) == 1



def test_explicit_gate_cannot_be_bypassed_by_diagnostic_cli_modes(monkeypatch):
    import pytest
    import scripts.eval_retrieval as ev

    def unexpected_settings():
        raise AssertionError("conflicting gate flags must fail before accessing a live venue")

    monkeypatch.setattr(ev, "load_settings", unexpected_settings)
    for flag in ("--no-check", "--label-draft"):
        with pytest.raises(SystemExit) as error:
            main(["--check", "baseline.json", flag])
        assert error.value.code == 2
