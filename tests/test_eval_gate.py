"""Unit tests for the retrieval evaluation regression gate (pure functions, no docker/network)."""

import json
from pathlib import Path

from scripts.eval_retrieval import (
    _get,
    _set,
    check_baseline,
    update_baseline,
)


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
        "mrr": 0.625,
        "identifier": {
            "recall@1": 1.0,
            "recall@5": 1.0,
            "mrr": 1.0,
        },
        "nl": {
            "recall@1": 0.333,
            "recall@5": 0.667,
            "mrr": 0.5,
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
