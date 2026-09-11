"""Unit tests for harness L4 (answer-quality gate) — pure functions, no stack.

The contract under test: structural faults fail unconditionally in any
repeat; rates are repeat means gated against a committed reference with a
tolerance band (inside the band holds for human review, outside fails); an
uncomputed metric fails rather than disappearing; the reference fails
closed when missing, malformed, or from another tier.
"""

import json
from pathlib import Path

import pytest
from scripts.harness_l4 import (
    GATED_METRICS,
    ThresholdError,
    build_review_queue,
    classify,
    gate_l4,
    load_thresholds,
    mean_metric,
    record_blockers,
    save_thresholds,
    summarize_l4,
)


def _run_metrics(**over):
    m = {
        "queries": 24,
        "structural_fails": 0,
        "errors": 0,
        "grounded_rate": 0.8,
        "citation_precision": 0.5,
        "citation_recall": 0.6,
        "truncation_rate": 0.1,
        "syntax_compliance": 0.9,
        "faithfulness": {"judge_errors": 0, "entailed": 0.7, "neutral": 0.2, "contradiction": 0.1},
        "relevance": {"judge_errors": 0, "relevant": 0.8, "partial": 0.1, "irrelevant": 0.1},
    }
    m.update(over)
    return m


def _run(rows=None, **over):
    return {"rows": rows or [], "metrics": _run_metrics(**over)}


def _thresholds(tolerance=0.05, metrics=None, meta=None):
    refs = {
        "grounded_rate": 0.8,
        "citation_precision": 0.5,
        "citation_recall": 0.6,
        "truncation_rate": 0.1,
        "syntax_compliance": 0.9,
        "faithfulness.entailed": 0.7,
        "faithfulness.contradiction": 0.1,
        "relevance.relevant": 0.8,
        "relevance.irrelevant": 0.1,
    }
    refs.update(metrics or {})
    meta_doc = {
        "tolerance": tolerance,
        "venue": "real_manuals",
        "embed_mode": "vllm",
        "llm_model_reasoning": "E4B",
        "updated": "2026-09-11",
    }
    meta_doc.update(meta or {})
    return {"_meta": meta_doc, "metrics": refs}


# ---------------------------------------------------------------- classify
def test_classify_min_directions():
    assert classify(0.80, 0.80, 0.05, "min") == "pass"
    assert classify(0.78, 0.80, 0.05, "min") == "borderline"
    assert classify(0.70, 0.80, 0.05, "min") == "fail"


def test_classify_max_directions():
    assert classify(0.10, 0.10, 0.05, "max") == "pass"
    assert classify(0.13, 0.10, 0.05, "max") == "borderline"
    assert classify(0.20, 0.10, 0.05, "max") == "fail"


# ---------------------------------------------------------------- summarize
def test_summarize_means_rates_and_sums_faults():
    runs = [
        _run(grounded_rate=0.8, structural_fails=1, errors=1,
             faithfulness={"judge_errors": 1, "entailed": 0.6, "neutral": 0.3, "contradiction": 0.1}),
        _run(grounded_rate=1.0, structural_fails=0, errors=0,
             relevance={"judge_errors": 2, "relevant": 0.9, "partial": 0.1, "irrelevant": 0.0}),
    ]
    summary = summarize_l4(runs)
    assert summary["repeats"] == 2
    assert summary["queries_per_run"] == 24
    assert summary["structural_fails"] == 1
    assert summary["errors"] == 1
    assert summary["judge_errors"] == 3  # faithfulness + relevance legs
    assert summary["metrics"]["grounded_rate"] == pytest.approx(0.9)
    assert summary["metrics"]["relevance.relevant"] == pytest.approx(0.85)


def test_mean_metric_none_when_a_repeat_missing():
    runs = [_run(), {"rows": [], "metrics": {"queries": 24}}]
    assert mean_metric(runs, "grounded_rate") is None


# ---------------------------------------------------------------- gate
def test_gate_passes_at_reference():
    summary = summarize_l4([_run(), _run()])
    verdict, reasons, borderline = gate_l4(summary, _thresholds())
    assert verdict == "pass", reasons
    assert borderline == []


def test_gate_holds_inside_the_band():
    # grounded only misses inside tolerance: review queue, not fail.
    summary = summarize_l4([_run(grounded_rate=0.77), _run(grounded_rate=0.77)])
    verdict, reasons, borderline = gate_l4(summary, _thresholds())
    assert verdict == "hold"
    assert "grounded_rate" in borderline
    assert any("borderline" in r for r in reasons)


def test_gate_fails_outside_the_band():
    summary = summarize_l4([_run(grounded_rate=0.60), _run(grounded_rate=0.60)])
    verdict, reasons, _ = gate_l4(summary, _thresholds())
    assert verdict == "fail"
    assert any("grounded_rate" in r and "outside" in r for r in reasons)


def test_gate_structural_fault_dominates_healthy_rates():
    summary = summarize_l4([_run(structural_fails=2), _run()])
    verdict, reasons, _ = gate_l4(summary, _thresholds())
    assert verdict == "fail"
    assert any("structural failure" in r for r in reasons)
    assert not any("grounded_rate" in r for r in reasons)


def test_gate_uncomputed_metric_fails_closed():
    # syntax_compliance is None when a sample has no syntax rows.
    runs = [_run(syntax_compliance=None), _run(syntax_compliance=None)]
    verdict, reasons, _ = gate_l4(summarize_l4(runs), _thresholds())
    assert verdict == "fail"
    assert any("syntax_compliance" in r and "not computed" in r for r in reasons)


def test_gate_max_metric_regression_fails():
    summary = summarize_l4([_run(truncation_rate=0.30), _run(truncation_rate=0.30)])
    verdict, reasons, _ = gate_l4(summary, _thresholds())
    assert verdict == "fail"
    assert any("truncation_rate" in r for r in reasons)


# ---------------------------------------------------------------- reference file
def test_load_thresholds_roundtrip(tmp_path: Path):
    path = tmp_path / "ref.json"
    path.write_text(json.dumps(_thresholds()))
    doc = load_thresholds(path)
    assert doc["_meta"]["tolerance"] == 0.05
    assert set(doc["metrics"]) == {name for name, _ in GATED_METRICS}


def test_load_thresholds_missing_fails_closed(tmp_path: Path):
    with pytest.raises(ThresholdError):
        load_thresholds(tmp_path / "nope.json")


def test_load_thresholds_metric_mismatch_fails_closed(tmp_path: Path):
    ref = _thresholds()
    del ref["metrics"]["truncation_rate"]
    path = tmp_path / "ref.json"
    path.write_text(json.dumps(ref))
    with pytest.raises(ThresholdError):
        load_thresholds(path)


def test_load_thresholds_bad_tolerance_fails_closed(tmp_path: Path):
    ref = _thresholds()
    ref["_meta"]["tolerance"] = 2.0
    path = tmp_path / "ref.json"
    path.write_text(json.dumps(ref))
    with pytest.raises(ThresholdError):
        load_thresholds(path)


def test_load_thresholds_missing_tier_meta_fails_closed(tmp_path: Path):
    ref = _thresholds()
    del ref["_meta"]["llm_model_reasoning"]
    path = tmp_path / "ref.json"
    path.write_text(json.dumps(ref))
    with pytest.raises(ThresholdError):
        load_thresholds(path)


def test_save_thresholds_records_mode_and_rounds(tmp_path: Path):
    class _S:
        qdrant_collection = "real_manuals"
        embed_mode = "vllm"
        llm_model_reasoning = "E4B"

    summary = summarize_l4([_run(), _run()])
    path = tmp_path / "ref.json"
    doc = save_thresholds(path, summary, _S(), repeats=3)
    assert doc["_meta"]["venue"] == "real_manuals"
    assert doc["_meta"]["embed_mode"] == "vllm"
    assert doc["_meta"]["llm_model_reasoning"] == "E4B"
    assert doc["_meta"]["tolerance"] == 0.15
    assert doc["metrics"]["grounded_rate"] == 0.8
    assert load_thresholds(path)["metrics"]["relevance.relevant"] == 0.8


def test_record_blockers_allow_structural_debt():
    # Product structural fails are a finding, not broken measurement —
    # they gate every run on their own and must not block recording.
    summary = summarize_l4([_run(structural_fails=3), _run()])
    assert record_blockers(summary) == []


def test_record_blockers_reject_broken_measurement():
    assert any("request error" in b for b in record_blockers(summarize_l4([_run(errors=1), _run()])))
    assert any(
        "judge infra" in b
        for b in record_blockers(summarize_l4([
            _run(faithfulness={"judge_errors": 1, "entailed": 0.5, "neutral": 0.5, "contradiction": 0.0}),
            _run(),
        ]))
    )
    assert any(
        "uncomputed" in b
        for b in record_blockers(summarize_l4([_run(syntax_compliance=None), _run(syntax_compliance=None)]))
    )


# ---------------------------------------------------------------- review queue
def _row(rid, **over):
    row = {
        "id": rid,
        "query": f"q-{rid}",
        "query_class": "message_id",
        "expected_behavior": "answer",
        "verdict": "pass",
        "failures": [],
        "judge_label": "entailed",
        "relevance_label": "relevant",
        "truncated": False,
        "citations": ["[1] c"],
        "citation_precision": 1.0,
        "citation_recall": 1.0,
    }
    row.update(over)
    return row


def test_review_queue_selects_weak_rows_only():
    clean = _row("A")
    neutral = _row("B", judge_label="neutral")
    partial = _row("C", relevance_label="partial")
    truncated = _row("D", truncated=True)
    failed = _row("E", verdict="fail", failures=["syntax pattern missed"])
    queue = build_review_queue([{"rows": [clean, neutral, partial, truncated, failed], "metrics": {}}])
    assert [r["id"] for r in queue] == ["B", "C", "D", "E"]
    assert queue[0]["faithfulness"] == ["neutral"]
    assert queue[1]["relevance"] == ["partial"]
    assert queue[3]["failures"] == ["run 1: syntax pattern missed"]


def test_review_queue_aggregates_repeats():
    rows_a = [_row("A", judge_label="neutral"), _row("B")]
    rows_b = [_row("A"), _row("B")]
    queue = build_review_queue([
        {"rows": rows_a, "metrics": {}},
        {"rows": rows_b, "metrics": {}},
    ])
    assert [r["id"] for r in queue] == ["A"]
    assert queue[0]["faithfulness"] == ["neutral", "entailed"]
    assert queue[0]["verdicts"] == ["pass", "pass"]


# ---------------------------------------------------------------- entry point
def test_main_exit_2_when_reference_missing(tmp_path: Path, monkeypatch, capfd):
    from scripts.harness_l4 import main as l4_main

    from mainframe_rag import config
    from mainframe_rag.config import Settings

    monkeypatch.delenv("VENUE", raising=False)
    monkeypatch.setattr(
        config, "load_settings",
        lambda: Settings(embed_mode="hash", qdrant_collection="test-corpus", _env_file=None),
    )
    rc = l4_main(["--thresholds", str(tmp_path / "nope.json")])
    assert rc == 2
    assert "no L4 reference" in capfd.readouterr().err
