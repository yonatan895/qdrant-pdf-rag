"""Unit tests for the benchmark regression gate (pure functions, no docker).

The round-trip test exists because a flat-keyed baseline once made the gate
a permanent no-op against the tool's own output (review round 1, blocker 1).
"""

import json

from scripts.benchmark import (
    GATED_METRICS,
    _get,
    _parse_size_mb,
    _set,
    check_baseline,
    update_baseline,
)
from scripts.loadtest import _percentile


def _result() -> dict:
    return {
        "env": {"cpu_count": 4},
        "ingest": {"peak_rss_mb": 100.0},
        "qdrant": {"mem_mb": 50.0, "disk_mb": 4.0},
        "agent": {
            "search": {"latency_ms": {"p95": 40.0}, "errors": 0},
            "answer": {"latency_ms": {"p95": 50.0}, "errors": 0},
        },
    }


def _scaled(factor: float) -> dict:
    result = _result()
    for dotted in GATED_METRICS:
        _set(result, dotted, _get(result, dotted) * factor)
    return result


def test_get_and_set_round_trip():
    doc: dict = {}
    _set(doc, "a.b.c", 7)
    assert doc == {"a": {"b": {"c": 7}}}
    assert _get(doc, "a.b.c") == 7
    assert _get(doc, "a.b.missing") is None
    assert _get(doc, "a.b.c.d") is None  # dotted path against a non-dict leaf


def test_update_baseline_emits_nested_shape_that_the_gate_reads(tmp_path):
    """The blocker regression test: update_baseline must write the SAME shape
    check_baseline reads (nested), else the tool-written baseline gates
    nothing forever."""
    path = tmp_path / "baseline.json"
    update_baseline(_result(), path)
    baseline = json.loads(path.read_text())
    assert isinstance(baseline["ingest"], dict), "baseline must be nested, not flat dotted keys"
    assert baseline["ingest"]["peak_rss_mb"] == 100.0

    # x2 crosses the x1.5 resource gates but not the x3 latency gates — exactly 3 fire.
    assert len(check_baseline(_scaled(2.0), baseline)) == 3
    # x4 crosses every tolerance.
    assert len(check_baseline(_scaled(4.0), baseline)) == len(GATED_METRICS)
    assert check_baseline(_scaled(0.001), baseline) == [], "improvements never fail"


def test_unmeasured_metrics_warn_and_skip(tmp_path, capsys):
    """qdrant mem/disk are unmeasurable on the QDRANT_SIM_URL reuse path —
    warn, never regress."""
    path = tmp_path / "baseline.json"
    update_baseline(_result(), path)
    baseline = json.loads(path.read_text())
    partial = _result()
    _set(partial, "qdrant.mem_mb", None)
    _set(partial, "qdrant.disk_mb", None)

    assert check_baseline(partial, baseline) == []
    assert "not measured this run" in capsys.readouterr().err


def test_errors_under_load_fail_the_gate(tmp_path):
    """A load phase where requests fail must not look healthy."""
    path = tmp_path / "baseline.json"
    update_baseline(_result(), path)
    baseline = json.loads(path.read_text())

    broken = _result()
    _set(broken, "agent.answer.errors", 5)
    regressions = check_baseline(broken, baseline)
    assert any("agent.answer.errors" in r for r in regressions)
    assert check_baseline(_result(), baseline) == []


def test_baseline_missing_keys_warn_and_skip(tmp_path, capsys):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"_meta": {}}))
    assert check_baseline(_result(), json.loads(path.read_text())) == []
    assert "baseline has no ingest.peak_rss_mb" in capsys.readouterr().err


def test_parse_size_mb():
    assert _parse_size_mb("123.4MiB") == 123.4
    assert _parse_size_mb("1.2GiB") == 1228.8
    assert _parse_size_mb("512kB") == 0.5
    assert _parse_size_mb("0B") == 0.0
    assert _parse_size_mb("junk") is None


def test_percentile():
    values = [10.0, 20.0, 30.0]
    assert _percentile(values, 0) == 10.0
    assert _percentile(values, 50) == 20.0
    assert _percentile(values, 100) == 30.0
    assert _percentile([], 50) == 0.0


def test_profile_pipeline_microbenchmarks(tmp_path):
    from scripts.profile_pipeline import (
        profile_answer_parsing,
        profile_embedding,
        profile_pdf_parsing_and_chunking,
    )

    res = profile_pdf_parsing_and_chunking(tmp_path, num_docs=2)
    assert res["docs"] == 2
    assert res["total_chunks"] > 0
    assert res["docs_per_s"] > 0

    embed_res = profile_embedding(res["sample_parsed_docs"])
    assert embed_res["total_chunks"] == res["total_chunks"]
    assert embed_res["dense_chunks_per_s"] > 0

    answer_res = profile_answer_parsing(iterations=10)
    assert answer_res["iterations"] == 10
    assert answer_res["parses_per_s"] > 0

