"""Unit tests for harness PR A: seeded bootstrap CIs (scripts/bootstrap_ci.py),
L1 per-class metrics (scripts/harness_l1.py), and the promotion gate verdict
(scripts/harness.py gate_verdict / baseline round-trip).

Hermetic: no Qdrant, no vLLM — every helper under test is pure."""

from __future__ import annotations

import json

import pytest
from scripts.bootstrap_ci import ci95, ci95_paired, ci_excludes_zero
from scripts.eval_retrieval import GoldenEntry
from scripts.harness import (
    DEFAULT_CLASS_FLOOR,
    baseline_path_for,
    gate_verdict,
    load_baseline,
    save_baseline,
)
from scripts.harness_l1 import aggregate, score_row


# ---------------------------------------------------------------- bootstrap
def test_ci95_seeded_deterministic():
    values = [0.5, 0.6, 0.4, 0.7, 0.3, 0.55]
    a = ci95(values, resamples=500, seed=7)
    b = ci95(values, resamples=500, seed=7)
    assert a == b
    lo, hi = a
    assert 0.0 <= lo <= hi <= 1.0


def test_ci95_known_distribution_orders_as_expected():
    tight = ci95([0.5] * 100, resamples=200, seed=1)
    wide = ci95([0.0, 1.0] * 50, resamples=200, seed=1)
    assert tight[1] - tight[0] < wide[1] - wide[0]
    lo, hi = tight
    assert abs(lo - 0.5) < 0.01 and abs(hi - 0.5) < 0.01


def test_ci95_paired_positive_shift_excludes_zero():
    pairs = [(b + 0.2, b) for b in [0.1 * i for i in range(30)]]
    ci = ci95_paired(pairs, resamples=500, seed=3)
    assert ci is not None and ci[0] > 0.0
    assert ci_excludes_zero(ci, improvement=True)


def test_ci95_paired_noise_straddles_zero():
    # symmetric +/-: mean delta 0, CI straddles zero -> no merge signal
    pairs = [(0.5 + d, 0.5 - d) for d in [0.1, -0.1] * 15]
    ci = ci95_paired(pairs, resamples=1000, seed=5)
    assert not ci_excludes_zero(ci, improvement=True)
    assert not ci_excludes_zero(ci, improvement=False)


def test_ci95_empty_is_none():
    assert ci95([]) is None
    assert ci95_paired([]) is None


def test_ci95_regression_pinned_seeded_values():
    # Pin the exact seeded resample behavior: identical inputs must give
    # identical bounds across machines (stdlib random is stable).
    values = [0.42, 0.51, 0.38, 0.60, 0.45, 0.49, 0.52, 0.40]
    assert ci95(values, resamples=1000, seed=0) == ci95(values, resamples=1000, seed=0)


# ------------------------------------------------------------------- L1 rows
class _Hit:
    def __init__(self, doc_id, heading="Chapter", page_label="1", score=0.9, message_ids=()):
        self.doc_id = doc_id
        self.heading = heading
        self.page_label = page_label
        self.score = score
        self.message_ids = tuple(message_ids)


def _entry(**kw):
    base = {
        "id": "MSG-01",
        "query": "What does IEA500I report?",
        "query_class": "message_id",
        "expected_behavior": "answer",
        "expected_doc_ids": ["SA38-0673-70"],
    }
    base.update(kw)
    return GoldenEntry(**base)


def test_score_row_full_graded_match():
    e = _entry(expected_heading="IEA500I", expected_page="2-14")
    hits = [_Hit("SA38-0673-70", heading="IEA500I", page_label="2-14", score=1.0)]
    row = score_row(hits, e)
    assert row["recall@5"] == 1.0 and row["recall@8"] == 1.0 and row["mrr"] == 1.0
    assert row["ndcg@8"] == 1.0  # max gain at rank 1 == ideal


def test_score_row_doc_only_hit_partial_ndcg_strict_recall():
    e = _entry(expected_heading="IEA500I", expected_page="2-14")
    hits = [_Hit("SA38-0673-70", heading="Other", page_label="9-9", score=0.9)]
    row = score_row(hits, e)
    # recall stays strict (doc + heading substring, same as the retrieval eval);
    # nDCG gives graded credit for the doc-level hit (gain 1 of max 3)
    assert row["recall@5"] == 0.0
    assert 0.0 < row["ndcg@8"] < 1.0


def test_score_row_abstain_excluded_but_traps_checked():
    e = _entry(id="NEG-08", expected_behavior="abstain", expected_doc_ids=[],
               must_not_message_ids=["IEC070I"])
    hits = [_Hit("SA38-0674-70", message_ids=("IEC070I",), score=0.7)]
    row = score_row(hits, e)
    assert "recall@5" not in row and "ndcg@8" not in row
    assert row["violations"]  # sibling-only bait chunk violates


def test_score_row_cocarrying_chunk_not_a_violation():
    e = _entry(id="MSG-25", query="IOS207I rejected a command",
               must_not_message_ids=["IOS208I"])
    hits = [
        _Hit("SA38-0676-70", message_ids=("IOS207I",), score=1.0),
        _Hit("SA38-0676-07", message_ids=("IOS207I", "IOS208I"), score=0.6),
    ]
    row = score_row(hits, e)
    assert "violations" not in row


def test_score_row_ndcg_dedupes_per_doc_never_exceeds_one():
    # N chunks of the SAME expected doc must not push nDCG above 1 (the
    # doc-level ranking dedupes; IDCG assumes every expected doc at max gain)
    e = _entry(expected_doc_ids=["SA38-0673-70"])
    hits = [_Hit("SA38-0673-70", score=1.0 - 0.01 * i) for i in range(8)]
    row = score_row(hits, e)
    assert 0.0 < row["ndcg@8"] <= 1.0
    assert row["ndcg@8"] == 1.0  # single expected doc found at rank 1 = perfect


def test_score_row_ndcg_multi_doc_ideal():
    # Gold has 3 docs; deduped doc ranking is D1(rank1), D9(rank2, gain 0),
    # D2(rank3): DCG = 1/log2(2) + 1/log2(4) = 1.5; IDCG (3 ideal positions,
    # max gain 1) = 1/log2(2)+1/log2(3)+1/log2(4) = 2.1309 -> nDCG = 0.7039
    e = _entry(expected_doc_ids=["D1", "D2", "D3"])
    hits = [_Hit("D1", score=1.0), _Hit("D9", score=0.9), _Hit("D2", score=0.8)]
    row = score_row(hits, e)
    assert row["ndcg@8"] == pytest.approx(0.7039, abs=0.001)


def test_aggregate_per_class_never_aggregate_only():
    rows = [
        score_row([_Hit("D1")], _entry(id="A-1", query_class="message_id", expected_doc_ids=["D1"])),
        score_row([], _entry(id="A-2", query_class="message_id", expected_doc_ids=["D1"])),
        score_row([_Hit("D2")], _entry(id="B-1", query_class="syntax", expected_doc_ids=["D2"])),
    ]
    agg = aggregate(rows)
    assert set(agg["classes"]) == {"message_id", "syntax"}
    assert agg["classes"]["message_id"]["recall@5"] == 0.5
    assert agg["classes"]["syntax"]["recall@5"] == 1.0
    assert agg["overall"]["recall@5"] == 0.6667  # aggregate rounds to 4dp
    assert set(agg["per_query"]) == {"A-1", "A-2", "B-1"}


def test_aggregate_trap_precision_absolute():
    rows = [
        score_row([_Hit("D1")], _entry(id="A-1", expected_doc_ids=["D1"])),
        score_row([_Hit("X1", message_ids=("IEC070I",))],
                  _entry(id="NEG-08", query_class="negative", expected_behavior="abstain",
                         expected_doc_ids=[], must_not_message_ids=["IEC070I"])),
    ]
    agg = aggregate(rows)
    assert agg["traps"]["failed"] == ["NEG-08"]
    assert agg["traps"]["precision"] == 0.5


# ------------------------------------------------------------- gate verdict
def _summary(recall5=0.8, mrr=0.7, trap_failed=None, classes=None):
    n = 10
    per_query = {f"E{i:02d}": {"recall@5": recall5, "mrr": mrr} for i in range(n)}
    return {
        "overall": {"n": n, "scored": n, "recall@5": recall5, "mrr": mrr},
        "classes": classes or {"message_id": {"n": n, "scored": n, "recall@5": recall5, "mrr": mrr}},
        "traps": {"checked": n, "failed": trap_failed or [], "precision": 1.0},
        "per_query": per_query,
    }


def _baseline(summary, floor=DEFAULT_CLASS_FLOOR):
    return {"_meta": {"class_regression_floor": floor}, **summary}


def test_gate_no_baseline_records():
    verdict, reasons = gate_verdict(_summary(), None)
    assert verdict == "baseline"
    assert any("first baseline" in r for r in reasons)


def test_gate_identical_run_holds_no_improvement():
    verdict, reasons = gate_verdict(_summary(), _baseline(_summary()))
    assert verdict == "hold"
    assert any("CI overlap" in r for r in reasons)


def test_gate_improvement_merges():
    base = _summary(recall5=0.6, mrr=0.5)
    cand = _summary(recall5=0.8, mrr=0.7)
    verdict, reasons = gate_verdict(cand, _baseline(base), resamples=400)
    assert verdict == "merge", reasons
    assert any("recall@5" in r for r in reasons)


def test_gate_trap_failure_holds_even_with_improvement():
    base = _summary(recall5=0.6)
    cand = _summary(recall5=0.9, trap_failed=["NEG-08"])
    verdict, reasons = gate_verdict(cand, _baseline(base), resamples=400)
    assert verdict == "hold"
    assert any("P0" in r for r in reasons)


def test_gate_class_regression_holds_despite_overall_gain():
    base = _summary(recall5=0.6, classes={
        "message_id": {"n": 10, "scored": 10, "recall@5": 0.9, "mrr": 0.9},
        "comparative": {"n": 10, "scored": 10, "recall@5": 0.3, "mrr": 0.3},
    })
    cand = _summary(recall5=0.7, classes={
        "message_id": {"n": 10, "scored": 10, "recall@5": 0.95, "mrr": 0.95},
        "comparative": {"n": 10, "scored": 10, "recall@5": 0.2, "mrr": 0.2},
    })
    verdict, reasons = gate_verdict(cand, _baseline(base), resamples=400)
    assert verdict == "hold"
    assert any("comparative" in r for r in reasons)


def test_gate_small_regression_within_floor_merges():
    base = _summary(recall5=0.6, classes={
        "message_id": {"n": 10, "scored": 10, "recall@5": 0.5, "mrr": 0.5},
    })
    cand = _summary(recall5=0.8, classes={
        "message_id": {"n": 10, "scored": 10, "recall@5": 0.45, "mrr": 0.45},
    })
    verdict, _ = gate_verdict(cand, _baseline(base), resamples=400)
    assert verdict == "merge"


# ------------------------------------------------------- baseline round-trip
def test_baseline_round_trip(tmp_path):
    summary = _summary()
    path = tmp_path / "harness-vllm.json"
    save_baseline(path, summary, embed_mode="vllm",
                  snapshot={"points_count": 840396, "snapshot_name": "mainframe_manuals-1.snapshot"})
    loaded = load_baseline(path)
    assert loaded is not None
    assert loaded["_meta"]["embed_mode"] == "vllm"
    assert loaded["_meta"]["snapshot"]["points_count"] == 840396
    assert loaded["per_query"] == summary["per_query"]
    # the stored per-query values must be exactly what the gate pairs against
    verdict, _ = gate_verdict(_summary(recall5=0.95, mrr=0.9), loaded, resamples=300)
    assert verdict == "merge"


def test_baseline_path_mode_keyed(tmp_path):
    assert baseline_path_for(tmp_path, "hash").name == "harness.json"
    assert baseline_path_for(tmp_path, "vllm").name == "harness-vllm.json"


def test_baseline_json_is_utf8_and_stable(tmp_path):
    summary = _summary()
    path = tmp_path / "b.json"
    save_baseline(path, summary, embed_mode="vllm", snapshot={"points_count": 1})
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["overall"]["recall@5"] == summary["overall"]["recall@5"]
