"""Unit tests for the benchmark regression gate (pure functions, no docker).

The round-trip test exists because a flat-keyed baseline once made the gate
a permanent no-op against the tool's own output (review round 1, blocker 1).
"""

import json

import pytest
from scripts.benchmark import (
    GATED_METRICS,
    _get,
    _parse_size_mb,
    _set,
    aggregate_runs,
    check_baseline,
    update_baseline,
)

from mainframe_rag.eval.load import _percentile


def _result() -> dict:
    return {
        "env": {"cpu_count": 4},
        "ingest": {"peak_rss_mb": 100.0},
        "qdrant": {"mem_mb": 50.0, "disk_mb": 4.0},
        "agent": {
            "search": {"latency_ms": {"p95": 40.0}, "errors": 0, "requests": 10},
            "answer": {"latency_ms": {"p95": 50.0}, "errors": 0, "requests": 10},
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


def test_unmeasured_metrics_refuse_required_gate(tmp_path):
    """qdrant mem/disk are unmeasurable on the QDRANT_SIM_URL reuse path —
    a requested gate cannot qualify unavailable measurements."""
    path = tmp_path / "baseline.json"
    update_baseline(_result(), path)
    baseline = json.loads(path.read_text())
    partial = _result()
    _set(partial, "qdrant.mem_mb", None)
    _set(partial, "qdrant.disk_mb", None)

    issues = check_baseline(partial, baseline)
    assert issues == [
        "qdrant.mem_mb: required finite nonnegative number unavailable",
        "qdrant.disk_mb: required finite nonnegative number unavailable",
    ]


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


def test_baseline_missing_keys_refuse_required_gate(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"_meta": {}}))
    issues = check_baseline(_result(), json.loads(path.read_text()))
    assert len(issues) == 5
    assert "ingest.peak_rss_mb: required finite nonnegative number unavailable" in issues


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


def _pass(p95_search: float, errors: int = 0, rps: float = 200.0, note: str = "x") -> dict:
    return {
        "env": {"cpu_count": 4, "python": "3.14.7"},
        "ingest": {"docs": 31, "chunks": 211, "wall_s": 5.0, "docs_per_s": 6.0, "peak_rss_mb": 170.0},
        "qdrant": {"mem_mb": 95.0, "disk_mb": 1.7, "points": 211, "metrics_available": True},
        "agent": {
            "model_note": note,
            "search": {"rps": rps, "latency_ms": {"p50": 30.0, "p95": p95_search}, "errors": errors, "requests": 10},
            "answer": {"rps": rps, "latency_ms": {"p50": 55.0, "p95": 100.0}, "errors": errors, "requests": 10},
        },
    }


def test_aggregate_runs_noise_floor_policy():
    """Latency/footprint = min (contention is noise, not signal); errors and
    throughput = max (a failing pass must not average away); strings/bools
    come from the first pass."""
    merged = aggregate_runs([
        _pass(50.0, errors=0, rps=250.0),
        _pass(72.0, errors=2, rps=200.0),  # contended + failing pass
        _pass(45.0, errors=0, rps=310.0),
    ])
    assert merged["agent"]["search"]["latency_ms"]["p95"] == 45.0
    assert merged["agent"]["answer"]["latency_ms"]["p95"] == 100.0
    assert merged["agent"]["search"]["errors"] == 2, "a failing pass must survive aggregation"
    assert merged["agent"]["search"]["rps"] == 310.0
    assert merged["ingest"]["peak_rss_mb"] == 170.0
    assert merged["qdrant"]["mem_mb"] == 95.0
    assert merged["ingest"]["docs"] == 31
    assert merged["qdrant"]["metrics_available"] is True
    assert merged["agent"]["model_note"] == "x"


def test_aggregate_runs_tolerates_unpaired_keys():
    """A pass missing a leaf (e.g. docker stats unavailable) must not crash;
    the first pass's value stands."""
    a = _pass(50.0)
    b = _pass(70.0)
    del b["qdrant"]["mem_mb"]
    merged = aggregate_runs([a, b])
    assert merged["qdrant"]["mem_mb"] == 95.0


def test_aggregate_runs_single_pass_identity():
    run = _pass(50.0)
    assert aggregate_runs([run]) is run


def test_update_baseline_records_capture_method(tmp_path):
    """The baseline must be self-describing: how many passes and which
    aggregation policy produced it, so a reviewer can trust (or reject)
    the numbers without archaeology."""
    path = tmp_path / "baseline.json"
    result = _result()
    result["repeats"] = 3
    update_baseline(result, path)
    meta = json.loads(path.read_text())["_meta"]
    assert meta["capture"]["repeats"] == 3
    assert "noise floor" in meta["capture"]["policy"]
    assert meta["env"] == {"cpu_count": 4}


def test_update_baseline_defaults_capture_to_single_pass(tmp_path):
    path = tmp_path / "baseline.json"
    update_baseline(_result(), path)
    assert json.loads(path.read_text())["_meta"]["capture"]["repeats"] == 1


def test_env_mismatch_fails_distinct_before_metric_gates(tmp_path):
    """A 24-core baseline gating a 4-vCPU runner must fail with an actionable
    env message — not a misleading `p95 > x3` that sends someone hunting for
    a code regression that does not exist."""
    path = tmp_path / "baseline.json"
    result = _result()
    update_baseline({**result, "env": {"cpu_count": 24}}, path)
    baseline = json.loads(path.read_text())
    regressions = check_baseline(_scaled(10.0), baseline)  # would blow every metric gate
    assert len(regressions) == 1
    assert "cpu_count 24 != runner 4" in regressions[0]
    assert "p95" not in "".join(regressions)


def test_qdrant_image_mismatch_fails_distinct(tmp_path):
    path = tmp_path / "baseline.json"
    result = _result()
    update_baseline({**result, "env": {"cpu_count": 4, "qdrant_image": "qdrant:v1.18.0"}}, path)
    baseline = json.loads(path.read_text())
    live = _result()
    live["env"]["qdrant_image"] = "qdrant:v1.19.0-unprivileged"
    regressions = check_baseline(live, baseline)
    assert len(regressions) == 1
    assert "qdrant_image" in regressions[0]


def test_matching_env_gates_metrics_normally(tmp_path):
    path = tmp_path / "baseline.json"
    update_baseline(_result(), path)
    baseline = json.loads(path.read_text())
    assert len(check_baseline(_scaled(4.0), baseline)) == len(GATED_METRICS)


def test_baseline_without_recorded_env_still_gates_metrics(tmp_path):
    """Old baselines without _meta.env keep working — env gating is opt-in
    via what the record tool writes, not a hard requirement."""
    path = tmp_path / "baseline.json"
    update_baseline(_result(), path)
    baseline = json.loads(path.read_text())
    del baseline["_meta"]["env"]
    assert len(check_baseline(_scaled(4.0), baseline)) == len(GATED_METRICS)


def test_ci_prepares_qdrant_before_check_or_record_and_stops_on_preparation_failure(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/bench.yml').read_text())
    steps = workflow['jobs']['bench']['steps']
    start = next(i for i, step in enumerate(steps) if step.get('name', '').startswith('run benchmark'))
    preparation = steps[start - 1]['run']
    assert preparation == 'python scripts/qdrant_pin.py --prepare'
    benchmark = steps[start]['run']
    for record in ('false', 'true'):
        for prepare_status in (0, 7):
            case = tmp_path / f'{record}-{prepare_status}'
            case.mkdir()
            python = case / 'python'
            python.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> calls\n'
                'if [ "$1" = scripts/qdrant_pin.py ]; then\n'
                '  test "$2" = --prepare || exit 9\n'
                '  test "$PREPARE_STATUS" = 0 || exit "$PREPARE_STATUS"\n'
                '  touch prepared\n'
                'elif [ "$1" = scripts/benchmark.py ]; then\n'
                '  test -f prepared || exit 8\n'
                '  touch measured\n'
                'else\n'
                '  exit 10\n'
                'fi\n'
            )
            python.chmod(0o755)
            command = preparation + '\n' + benchmark.replace('${{ inputs.update_baseline }}', record).replace(
                "${{ inputs.repeats || '3' }}", '3')
            result = subprocess.run(
                ['sh', '-eu', '-c', command], cwd=case,
                env={**os.environ, 'PATH': f"{case}:{os.environ['PATH']}", 'PREPARE_STATUS': str(prepare_status)},
                capture_output=True, text=True, check=False,
            )
            assert result.returncode == prepare_status, result.stdout + result.stderr
            assert (case / 'measured').exists() == (prepare_status == 0)
            calls = (case / 'calls').read_text().splitlines()
            assert calls[0] == 'scripts/qdrant_pin.py --prepare'
            assert len(calls) == (2 if prepare_status == 0 else 1)
            if prepare_status == 0:
                expected = ('--update-baseline bench-baseline.json --repeats 3' if record == 'true'
                            else '--check benchmarks/baseline.json')
                assert calls[1].endswith(expected)


@pytest.fixture
def benchmark_cli(monkeypatch, tmp_path):
    """Actual CLI/gate/output with only workload and environment effects controlled."""
    import copy
    from types import SimpleNamespace

    from scripts import benchmark

    events = []
    result = _pass(50.0)
    for endpoint in ('search', 'answer'):
        result['agent'][endpoint]['latency_ms']['p99'] = 110.0
    env = {'cpu_count': 4, 'mem_total_mb': 1000, 'qdrant_image': 'synthetic'}
    result['env'] = env
    def simulator(*args):
        events.append('start')
        return SimpleNamespace(stop=lambda: events.append('stop'))
    def corpus(root, docs):
        events.append('corpus')
        return {'root': tmp_path / 'corpus', 'docs': 31}
    def measure(*args):
        events.append('measure')
        return copy.deepcopy(result)
    def manifest(*args):
        events.append('manifest')
        return {'git_sha': 'synthetic'}
    monkeypatch.setattr(benchmark, 'start_simulator', simulator)
    monkeypatch.setattr(benchmark, 'generate_corpus', corpus)
    monkeypatch.setattr(benchmark, 'measure_once', measure)
    monkeypatch.setattr(benchmark, 'env_snapshot', lambda: env)
    monkeypatch.setattr(benchmark, 'load_settings', lambda: None)
    monkeypatch.setattr(benchmark, 'write_run_manifest', manifest)
    return benchmark, events, result


@pytest.mark.parametrize('contents', [None, b'{broken', b'\xff', b'null', b'[]', b'"text"', b'1', b'false', 'directory'])
def test_requested_baseline_refuses_before_work_and_preserves_outputs_then_recovers(
    benchmark_cli, tmp_path, contents, capsys,
):
    benchmark, events, result = benchmark_cli
    reference = tmp_path / 'required.json'
    if contents == 'directory':
        reference.mkdir()
    elif contents is not None:
        reference.write_bytes(contents)
    out, summary = tmp_path / 'result.json', tmp_path / 'summary.md'
    out.write_text('previous JSON')
    summary.write_text('previous summary')
    args = ['--check', str(reference), '--out', str(out), '--summary', str(summary)]
    assert benchmark.main(args) == 2
    assert events == []
    assert out.read_text() == 'previous JSON'
    assert summary.read_text() == 'previous summary'
    assert 'requested benchmark baseline' in capsys.readouterr().err
    if reference.is_dir():
        reference.rmdir()
    update_baseline(result, reference)
    before = reference.read_bytes()
    assert benchmark.main(args) == 0
    assert events == ['start', 'corpus', 'measure', 'stop', 'manifest']
    assert reference.read_bytes() == before
    assert json.loads(out.read_text())['agent']['search']['latency_ms']['p95'] == 50.0
    assert '| agent.search.latency_ms.p95 | 50.0 | 50.0 | <= 150.0 |' in summary.read_text()


def test_requested_baseline_uses_preflight_snapshot_and_still_fails_regressions(
    benchmark_cli, monkeypatch, tmp_path,
):
    benchmark, events, result = benchmark_cli
    reference = tmp_path / 'required.json'
    update_baseline(_result(), reference)
    original = json.loads(reference.read_text())
    # The actual observed result is too large for the initial resource pin.
    # Replacing the file during measurement must not select a more lenient gate.
    result['ingest']['peak_rss_mb'] = 1000
    def measure(*args):
        events.append('measure')
        update_baseline(result, reference)
        return result
    monkeypatch.setattr(benchmark, 'measure_once', measure)
    output = tmp_path / 'out.json'
    summary = tmp_path / 'summary.md'
    assert benchmark.main(['--check', str(reference), '--out', str(output), '--summary', str(summary)]) == 1
    assert events == ['start', 'corpus', 'measure', 'stop', 'manifest']
    assert json.loads(output.read_text())['ingest']['peak_rss_mb'] == 1000
    assert '| ingest.peak_rss_mb | 1000 | 100.0 | <= 150.0 |' in summary.read_text()
    assert json.loads(reference.read_text()) != original
    # Next ordinary invocation sees the new reference and passes.
    assert benchmark.main(['--check', str(reference), '--out', str(output)]) == 0


def test_no_check_and_explicit_record_keep_their_operations(benchmark_cli, tmp_path):
    benchmark, events, result = benchmark_cli
    output = tmp_path / 'out.json'
    reference = tmp_path / 'recorded.json'
    assert benchmark.main(['--out', str(output)]) == 0
    assert not reference.exists()
    assert json.loads(output.read_text())['repeats'] == 1
    events.clear()
    assert benchmark.main(['--update-baseline', str(reference), '--repeats', '2', '--out', str(output)]) == 0
    assert events == ['start', 'corpus', 'measure', 'measure', 'stop', 'manifest']
    recorded = json.loads(reference.read_text())
    assert recorded['_meta']['capture']['repeats'] == 2
    assert check_baseline(result, recorded) == []
    assert benchmark.main(['--check', str(reference), '--out', str(output)]) == 0


@pytest.mark.parametrize("dotted", [
    "ingest.peak_rss_mb", "qdrant.mem_mb", "qdrant.disk_mb",
    "agent.search.latency_ms.p95", "agent.answer.latency_ms.p95",
])
@pytest.mark.parametrize("value", [None, True, "10", -1, float("nan"), float("inf"), float("-inf"), 10**400])
def test_invalid_gate_numbers_refuse_baseline_result_and_recording(tmp_path, dotted, value):
    baseline = _result()
    broken = _result()
    _set(broken, dotted, value)
    assert any(dotted in issue for issue in check_baseline(_result(), broken))
    assert any(dotted in issue for issue in check_baseline(broken, baseline))
    reference = tmp_path / "reference.json"
    reference.write_bytes(b"previous approved reference")
    with pytest.raises(ValueError, match="cannot record invalid benchmark measurements"):
        update_baseline(broken, reference)
    assert reference.read_bytes() == b"previous approved reference"
    update_baseline(_result(), reference)
    assert check_baseline(_result(), json.loads(reference.read_text())) == []


@pytest.mark.parametrize("value", [None, False, "0", -1, 0.0, float("nan"), 1])
@pytest.mark.parametrize("endpoint", ["search", "answer"])
def test_invalid_or_failed_error_counts_cannot_qualify_or_record(tmp_path, endpoint, value):
    broken = _result()
    broken["agent"][endpoint]["errors"] = value
    assert any(f"agent.{endpoint}.errors" in issue for issue in check_baseline(broken, _result()))
    reference = tmp_path / "reference.json"
    with pytest.raises(ValueError):
        update_baseline(broken, reference)
    assert not reference.exists()


@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("operation", ["--check", "--update-baseline"])
def test_invalid_pass_cannot_hide_in_aggregation_or_overwrite_outputs(
    benchmark_cli, monkeypatch, tmp_path, bad_first, operation,
):
    import copy
    benchmark, events, result = benchmark_cli
    reference = tmp_path / "reference.json"
    update_baseline(result, reference)
    before = reference.read_bytes()
    out = tmp_path / "out.json"
    summary = tmp_path / "summary.md"
    out.write_text("previous output")
    summary.write_text("previous summary")
    broken = copy.deepcopy(result)
    broken["ingest"]["peak_rss_mb"] = float("nan")
    passes = iter([broken, result] if bad_first else [result, broken])
    def measure(*args):
        events.append("measure")
        return next(passes)
    monkeypatch.setattr(benchmark, "measure_once", measure)
    args = [operation, str(reference), "--repeats", "2", "--out", str(out), "--summary", str(summary)]
    assert benchmark.main(args) == 2
    assert events == ["start", "corpus"] + ["measure"] * (1 if bad_first else 2) + ["stop"]
    assert reference.read_bytes() == before
    assert out.read_text() == "previous output"
    assert summary.read_text() == "previous summary"
    monkeypatch.setattr(benchmark, "measure_once", lambda *args: result)
    assert benchmark.main(args) == 0
    assert json.loads(out.read_text())["ingest"]["peak_rss_mb"] == 170.0


@pytest.mark.parametrize("reference", [
    {}, {"ingest": {"peak_rss_mb": float("nan")}},
    {**_result(), "_meta": []}, {**_result(), "_meta": {"env": "bad"}},
])
def test_semantically_invalid_reference_refuses_before_work(benchmark_cli, tmp_path, reference):
    benchmark, events, result = benchmark_cli
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(reference))
    assert benchmark.main(["--check", str(path)]) == 2
    assert events == []
    update_baseline(result, path)
    assert benchmark.main(["--check", str(path)]) == 0


@pytest.mark.parametrize("operation", ["--check", "--update-baseline"])
def test_real_empty_load_cannot_qualify_benchmark(benchmark_cli, monkeypatch, tmp_path, operation):
    from mainframe_rag.eval import load
    benchmark, events, result = benchmark_cli
    reference = tmp_path / "reference.json"
    update_baseline(result, reference)
    before = reference.read_bytes()
    monkeypatch.setattr(load, "query_vram_mb", lambda: None)
    for endpoint in ("search", "answer"):
        result["agent"][endpoint] = load.run_load("http://unused.invalid", endpoint, ["original"], 0, 0)
        assert result["agent"][endpoint]["requests"] == 0
        assert result["agent"][endpoint]["errors"] == 0
        assert result["agent"][endpoint]["latency_ms"]["p95"] == 0
    output = tmp_path / "out.json"
    assert benchmark.main([operation, str(reference), "--out", str(output)]) == 2
    assert events == ["start", "corpus", "measure", "stop"]
    assert reference.read_bytes() == before
    assert not output.exists()
    # Still usable as an explicitly ungated diagnostic.
    assert benchmark.main(["--out", str(output)]) == 0
    assert json.loads(output.read_text())["agent"]["search"]["requests"] == 0


@pytest.mark.parametrize("requests", [None, False, "1", 0, -1, 1.0])
@pytest.mark.parametrize("endpoint", ["search", "answer"])
def test_required_measurements_have_positive_integer_request_counts(tmp_path, endpoint, requests):
    result = _result()
    result["agent"][endpoint]["requests"] = requests
    assert any(f"agent.{endpoint}.requests" in issue for issue in check_baseline(result, _result()))
    with pytest.raises(ValueError):
        update_baseline(result, tmp_path / "reference.json")
    assert not (tmp_path / "reference.json").exists()
