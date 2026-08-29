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


def test_corpus_guard_never_deletes_operator_data(tmp_path):
    """BENCH_CORPUS_DIR may point at real data: the harness refuses to delete
    a directory that is not a generated bench corpus, and leaves it intact."""
    from scripts.benchmark import generate_corpus

    foreign = tmp_path / "my-real-pdfs"
    foreign.mkdir()
    (foreign / "IEA500I-manual.pdf").write_bytes(b"not really a pdf, but data")

    try:
        generate_corpus(foreign, docs=1)
        raise AssertionError("generate_corpus must refuse to wipe foreign directories")
    except RuntimeError as exc:
        assert "refusing to delete" in str(exc)
    assert (foreign / "IEA500I-manual.pdf").exists(), "operator data must be untouched"

    # A directory that IS a previous bench corpus is regenerated freely.
    bench = tmp_path / "bench-corpus"
    bench.mkdir()
    (bench / "SA22-0000-00.pdf").write_bytes(b"stale bench artifact")
    info = generate_corpus(bench, docs=1)
    assert info["docs"] == 2 and (bench / "SA22-0000-00.pdf").stat().st_size > 1000
