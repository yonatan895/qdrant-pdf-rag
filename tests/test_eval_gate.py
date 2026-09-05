"""Unit tests for the retrieval evaluation regression gate (pure functions, no docker/network)."""

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
