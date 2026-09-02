"""Unit tests for scripts/harness_l3.py (gate verdict, summary formatting)."""

from __future__ import annotations

from scripts.harness_l3 import gate_verdict_l3, summary_markdown_l3


def _sample_report() -> dict:
    return {
        "search": {
            "rps": 50.0,
            "errors": 0,
            "latency_ms": {"p50": 10.0, "p95": 20.0},
            "stages": {
                "embed_ms": {"p50": 4.0, "p95": 8.0, "max": 12.0},
                "qdrant_ms": {"p50": 6.0, "p95": 12.0, "max": 15.0},
            },
        },
        "answer": {
            "rps": 20.0,
            "errors": 0,
            "latency_ms": {"p50": 25.0, "p95": 50.0},
            "stages": {
                "embed_ms": {"p50": 4.0, "p95": 8.0, "max": 12.0},
                "qdrant_ms": {"p50": 6.0, "p95": 12.0, "max": 15.0},
                "llm_ms": {"p50": 15.0, "p95": 30.0, "max": 40.0},
                "ttft_ms": {"p50": 10.0, "p95": 20.0, "max": 25.0},
            },
        },
        "vram": {"used_mb": 4000.0, "total_mb": 8192.0},
    }


def _sample_baseline() -> dict:
    return {
        "agent": {
            "search": {
                "latency_ms": {"p95": 20.0},
                "stages": {
                    "embed_ms": {"p95": 8.0},
                    "qdrant_ms": {"p95": 12.0},
                },
            },
            "answer": {
                "latency_ms": {"p95": 50.0},
                "stages": {
                    "embed_ms": {"p95": 8.0},
                    "qdrant_ms": {"p95": 12.0},
                    "llm_ms": {"p95": 30.0},
                    "ttft_ms": {"p95": 20.0},
                },
            },
        },
        "vram": {"used_mb": 4000.0},
    }


def test_gate_verdict_no_baseline():
    report = _sample_report()
    verdict, reasons = gate_verdict_l3(report, None)
    assert verdict == "baseline"
    assert "no baseline recorded" in reasons[0]


def test_gate_verdict_passes_within_tolerance():
    report = _sample_report()
    baseline = _sample_baseline()
    verdict, reasons = gate_verdict_l3(report, baseline, tolerance=3.0)
    assert verdict == "pass"
    assert reasons == []


def test_gate_verdict_fails_on_errors():
    report = _sample_report()
    report["search"]["errors"] = 2
    baseline = _sample_baseline()
    verdict, reasons = gate_verdict_l3(report, baseline)
    assert verdict == "hold"
    assert any("search: 2 request error(s)" in r for r in reasons)


def test_gate_verdict_fails_on_total_latency_regression():
    report = _sample_report()
    # Baseline answer p95 is 50.0; x3.0 limit is 150.0; 160.0 should trigger hold
    report["answer"]["latency_ms"]["p95"] = 160.0
    baseline = _sample_baseline()
    verdict, reasons = gate_verdict_l3(report, baseline, tolerance=3.0)
    assert verdict == "hold"
    assert any("answer.latency_ms.p95" in r for r in reasons)


def test_gate_verdict_fails_on_stage_latency_regression():
    report = _sample_report()
    # Baseline llm_ms p95 is 30.0; x3.0 limit is 90.0; 100.0 should trigger hold
    report["answer"]["stages"]["llm_ms"]["p95"] = 100.0
    baseline = _sample_baseline()
    verdict, reasons = gate_verdict_l3(report, baseline, tolerance=3.0)
    assert verdict == "hold"
    assert any("answer.stages.llm_ms.p95" in r for r in reasons)


def test_summary_markdown_renders_tables_and_vram():
    report = _sample_report()
    baseline = _sample_baseline()
    md = summary_markdown_l3(report, baseline)
    assert "Harness L3 — performance & latency report" in md
    assert "Request Latencies" in md
    assert "Per-Stage Latencies" in md
    assert "embed_ms" in md
    assert "qdrant_ms" in md
    assert "llm_ms" in md
    assert "ttft_ms" in md
    assert "VRAM Footprint" in md
    assert "4000.0 MB" in md
