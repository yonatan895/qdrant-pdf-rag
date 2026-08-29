"""Unit tests for scripts/render_report.py (pure functions, no network/docker)."""

import json
from pathlib import Path

from scripts.render_report import (
    compare_bench,
    compare_eval,
    main,
    render_bench,
    render_eval,
)


def _eval_report() -> dict:
    return {
        "n": 2,
        "failures": 0,
        "elapsed_s": 0.1,
        "embed_mode": "hash",
        "collection": "test-coll",
        "recall@1": 0.5,
        "recall@3": 1.0,
        "recall@5": 1.0,
        "mrr": 0.75,
        "identifier": {"recall@1": 1.0, "recall@5": 1.0, "mrr": 1.0},
        "nl": {"recall@1": 0.0, "recall@5": 1.0, "mrr": 0.5},
        "rows": [
            {
                "query": "IEA500I",
                "kind": "identifier",
                "recall@1": 1.0,
                "recall@5": 1.0,
                "mrr": 1.0,
                "hit_doc_ids": ["SA22-0000-00"],
            },
            {
                "query": "system tuning",
                "kind": "nl",
                "recall@1": 0.0,
                "recall@5": 1.0,
                "mrr": 0.5,
                "hit_doc_ids": ["SC23-0000-00"],
            },
        ],
    }


def _bench_report() -> dict:
    return {
        "env": {"cpu_count": 8, "mem_total_mb": 16000.0, "qdrant_image": "qdrant:v1.19.0"},
        "corpus": {"docs": 10},
        "ingest": {"wall_s": 5.0, "docs_per_s": 2.0, "docs": 10, "chunks": 50, "peak_rss_mb": 150.0},
        "qdrant": {"points": 50, "indexed_vectors": 50, "mem_mb": 120.0, "disk_mb": 3.0},
        "agent": {
            "search": {
                "rps": 400.0,
                "errors": 0,
                "latency_ms": {"p50": 10.0, "p90": 15.0, "p95": 20.0, "p99": 25.0},
            },
            "answer": {
                "rps": 200.0,
                "errors": 0,
                "latency_ms": {"p50": 20.0, "p90": 30.0, "p95": 40.0, "p99": 50.0},
            },
        },
    }


def test_render_eval_text_and_markdown():
    rep = _eval_report()
    text = render_eval(rep, None, "text")
    assert "RETRIEVAL EVALUATION REPORT" in text
    assert "recall@1" in text

    md = render_eval(rep, rep, "markdown")
    assert "## Retrieval Evaluation Report" in md
    assert "| recall@1 | 0.5 |" in md


def test_render_eval_html_and_escaping():
    rep = _eval_report()
    rep["rows"][0]["kind"] = "<img src=x onerror=alert(1)>"
    html_out = render_eval(rep, rep, "html")
    assert "<!DOCTYPE html>" in html_out
    assert "Retrieval Accuracy Report" in html_out
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_out
    assert "<img src=x" not in html_out


def test_render_bench_html_escaping():
    b = _bench_report()
    b["env"]["qdrant_image"] = "<script>alert(1)</script>"
    b["env"]["cpu_count"] = "<b>24</b>"
    html_out = render_bench(b, b, "html")
    assert "<!DOCTYPE html>" in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out
    assert "<script>" not in html_out
    assert "&lt;b&gt;24&lt;/b&gt;" in html_out


def test_render_eval_markdown_escaping():
    rep = _eval_report()
    rep["rows"][0]["query"] = "pipe|query\nwith newline"
    rep["rows"][0]["hit_doc_ids"] = ["DOC|1"]
    md = render_eval(rep, rep, "markdown")
    assert "pipe\\|query with newline" in md
    assert "DOC\\|1" in md


def test_compare_eval_classification_shifts_and_population():
    base = _eval_report()
    cur = _eval_report()

    # Classification shift: IEA500I identifier -> nl
    cur["rows"][0]["kind"] = "nl"
    # Added query
    cur["rows"].append({
        "query": "new query",
        "kind": "nl",
        "recall@1": 1.0,
        "recall@5": 1.0,
        "mrr": 1.0,
        "hit_doc_ids": ["NEW-01"],
    })
    # Removed query (remove system tuning from cur)
    cur["rows"].pop(1)

    cmp_text, _has_reg = compare_eval(base, cur, "text")
    assert "Classification Shifts:" in cmp_text
    assert "IEA500I: identifier -> nl" in cmp_text
    assert "Added Queries" in cmp_text
    assert "new query" in cmp_text
    assert "Removed Queries" in cmp_text
    assert "system tuning" in cmp_text

    cmp_md, _ = compare_eval(base, cur, "markdown")
    assert "### Classification Shifts:" in cmp_md
    assert "| `IEA500I` | `identifier` | `nl` |" in cmp_md
    assert "### Added Queries" in cmp_md
    assert "### Removed Queries" in cmp_md

    cmp_html, _ = compare_eval(base, cur, "html")
    assert "Classification Shifts" in cmp_html
    assert "Evaluated Population Changes" in cmp_html


def test_compare_eval_regression_alert_and_fail_flag(tmp_path: Path):
    base = _eval_report()
    cur = _eval_report()
    # Regress system tuning recall@5 from 1.0 to 0.0
    cur["rows"][1]["recall@5"] = 0.0
    cur["rows"][1]["mrr"] = 0.0
    cur["recall@5"] = 0.5
    cur["mrr"] = 0.5

    cmp_text, has_reg = compare_eval(base, cur, "text")
    assert has_reg is True
    assert "ALERT: Regressions detected" in cmp_text
    assert "! system tuning" in cmp_text

    base_p = tmp_path / "base.json"
    cur_p = tmp_path / "cur.json"
    base_p.write_text(json.dumps(base), encoding="utf-8")
    cur_p.write_text(json.dumps(cur), encoding="utf-8")

    # Without flag -> exit 0
    assert main(["compare-eval", "--base", str(base_p), "--current", str(cur_p)]) == 0
    # With --fail-on-regression -> exit 1
    assert main(["compare-eval", "--base", str(base_p), "--current", str(cur_p), "--fail-on-regression"]) == 1


def test_compare_bench_regression_and_fail_flag(tmp_path: Path):
    base = _bench_report()
    cur = _bench_report()
    # Regress latency p95 by 4x (exceeds 3.0x tolerance)
    cur["agent"]["search"]["latency_ms"]["p95"] = 100.0

    cmp_text, has_reg = compare_bench(base, cur, "text")
    assert has_reg is True
    assert "ALERT: Benchmark regressions detected" in cmp_text

    base_p = tmp_path / "base.json"
    cur_p = tmp_path / "cur.json"
    base_p.write_text(json.dumps(base), encoding="utf-8")
    cur_p.write_text(json.dumps(cur), encoding="utf-8")

    assert main(["compare-bench", "--base", str(base_p), "--current", str(cur_p)]) == 0
    assert main(["compare-bench", "--base", str(base_p), "--current", str(cur_p), "--fail-on-regression"]) == 1


def test_compare_eval_mixed_directional_change_regression(tmp_path: Path):
    """Mixed directional change (e.g. improved recall@5 but reduced MRR) must NOT
    be masked as improved; regression gate must catch query-level degradation even
    when aggregate report metrics improve or stay flat."""
    base = _eval_report()
    cur = _eval_report()

    # Query 0 in base: recall@5: 0.0, mrr: 1.0
    base["rows"][0]["recall@5"] = 0.0
    base["rows"][0]["mrr"] = 1.0
    base["recall@5"] = 0.5
    base["mrr"] = 0.75

    # Query 0 in cur: recall@5: 1.0 (improved), mrr: 0.2 (regressed)
    cur["rows"][0]["recall@5"] = 1.0
    cur["rows"][0]["mrr"] = 0.2
    # Keep aggregate metrics equal or improved (recall@5 improved, mrr equal)
    cur["recall@5"] = 1.0
    cur["mrr"] = 0.75

    cmp_text, has_reg = compare_eval(base, cur, "text")
    assert has_reg is True
    assert "Regressed: 1" in cmp_text
    assert "Improved: 0" in cmp_text
    assert "! IEA500I" in cmp_text

    base_p = tmp_path / "base.json"
    cur_p = tmp_path / "cur.json"
    base_p.write_text(json.dumps(base), encoding="utf-8")
    cur_p.write_text(json.dumps(cur), encoding="utf-8")

    assert main(["compare-eval", "--base", str(base_p), "--current", str(cur_p), "--fail-on-regression"]) == 1

