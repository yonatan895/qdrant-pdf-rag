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


def test_render_eval_html():
    rep = _eval_report()
    html_out = render_eval(rep, rep, "html")
    assert "<!DOCTYPE html>" in html_out
    assert "Retrieval Accuracy Report" in html_out
    assert "IEA500I" in html_out
    assert "SA22-0000-00" in html_out


def test_render_bench_text_md_html():
    b = _bench_report()
    text = render_bench(b, None, "text")
    assert "BENCHMARK PERFORMANCE REPORT" in text
    assert "400.0" in text

    md = render_bench(b, b, "markdown")
    assert "## Benchmark Results" in md
    assert "| Search RPS | 400.0 |" in md

    html_out = render_bench(b, b, "html")
    assert "<!DOCTYPE html>" in html_out
    assert "Benchmark Report" in html_out


def test_compare_eval():
    base = _eval_report()
    cur = _eval_report()
    # Modify query in cur to create an improvement
    cur["rows"][1]["recall@1"] = 1.0
    cur["rows"][1]["mrr"] = 1.0
    cur["recall@1"] = 1.0

    cmp_text = compare_eval(base, cur, "text")
    assert "Improved: 1" in cmp_text
    assert "system tuning" in cmp_text

    cmp_html = compare_eval(base, cur, "html")
    assert "<!DOCTYPE html>" in cmp_html
    assert "Evaluation Comparison" in cmp_html


def test_compare_bench():
    base = _bench_report()
    cur = _bench_report()
    cur["agent"]["search"]["rps"] = 450.0

    cmp_text = compare_bench(base, cur, "text")
    assert "BENCHMARK PERFORMANCE COMPARISON" in cmp_text

    cmp_html = compare_bench(base, cur, "html")
    assert "<!DOCTYPE html>" in cmp_html
    assert "Benchmark Performance Comparison" in cmp_html


def test_cli_main_subcommands(tmp_path: Path):
    eval_p = tmp_path / "eval.json"
    bench_p = tmp_path / "bench.json"
    eval_p.write_text(json.dumps(_eval_report()), encoding="utf-8")
    bench_p.write_text(json.dumps(_bench_report()), encoding="utf-8")

    out_html = tmp_path / "eval.html"
    rc = main(["eval", "--report", str(eval_p), "--format", "html", "--out", str(out_html)])
    assert rc == 0
    assert out_html.exists()
    assert "<!DOCTYPE html>" in out_html.read_text(encoding="utf-8")

    rc_cmp = main(["compare-eval", "--base", str(eval_p), "--current", str(eval_p), "--format", "markdown"])
    assert rc_cmp == 0
