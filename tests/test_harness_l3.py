"""Unit tests for scripts/harness_l3.py (gate verdict, summary formatting)."""

from __future__ import annotations

import pytest

from mainframe_rag.eval.performance import gate_verdict_l3


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


def test_gate_verdict_no_baseline_records_but_never_drops_errors():
    """Fail-closed invariant: request errors must NOT be dropped when baseline is missing."""
    verdict, reasons = gate_verdict_l3(_sample_report(), None)
    assert verdict == "baseline"
    report = _sample_report()
    report["search"]["errors"] = 3
    verdict, reasons = gate_verdict_l3(report, None)
    assert verdict == "hold"
    assert any("search: 3 request error(s)" in r for r in reasons)


def _no_change(report: dict) -> None:
    return None


def _drop_llm_stage(report: dict) -> None:
    del report["answer"]["stages"]["llm_ms"]


def _slow_llm_stage(report: dict) -> None:
    report["answer"]["stages"]["llm_ms"]["p95"] = 100.0


_GATE_CASES = {
    # A 200 without Server-Timing headers fails the gate.
    "missing-server-timing": (
        lambda r: r["search"].__setitem__("missing_timings", 5), "hold",
        "search: 5 response(s) missing Server-Timing header"),
    # A stage present in the baseline but missing now is a header regression.
    "missing-expected-stage": (_drop_llm_stage, "hold",
                               "answer.stages: expected stage llm_ms missing"),
    "within-tolerance": (_no_change, "pass", None),
    # Baseline llm_ms p95 is 30.0; x3.0 is 90.0, so 100.0 holds.
    "stage-latency-regression": (_slow_llm_stage, "hold", "answer.stages.llm_ms.p95"),
}


@pytest.mark.parametrize("case", sorted(_GATE_CASES))
def test_gate_verdict_stage_matrix(case):
    mutate, verdict, needle = _GATE_CASES[case]
    report = _sample_report()
    mutate(report)
    got, reasons = gate_verdict_l3(report, _sample_baseline(), tolerance=3.0)
    assert got == verdict, reasons
    if needle is None:
        assert reasons == []
    else:
        assert any(needle in r for r in reasons), reasons


def test_gate_verdict_env_mismatch_fails_closed():
    """PR 71 invariant: different environments refuse to gate to prevent spurious regressions."""
    report = _sample_report()
    report["env"] = {"cpu_count": 8, "embed_mode": "vllm", "qdrant_image": "pin-a"}
    baseline = _sample_baseline()
    baseline["_meta"] = {"env": {"cpu_count": 4, "embed_mode": "vllm", "qdrant_image": "pin-a"}}
    verdict, reasons = gate_verdict_l3(report, baseline)
    assert verdict == "hold"
    assert any("baseline env mismatch: cpu_count 4 != runner 8" in r for r in reasons)
    # A p95 recorded at another concurrency or GPU must not gate this run; a
    # side lacking GPU info does not fire the GPU check.
    for key, base_val, run_val, hit in (
        ("concurrency", 2, 8, "concurrency 2 != runner 8"),
        ("gpu_name", "RTX 4090", "RTX 5060", "gpu_name 'RTX 4090' != runner 'RTX 5060'"),
    ):
        same = {"cpu_count": 8, "embed_mode": "vllm", "qdrant_image": "pin-a"}
        report["env"] = {**same, key: run_val}
        baseline["_meta"] = {"env": {**same, key: base_val}}
        verdict, reasons = gate_verdict_l3(report, baseline)
        assert verdict == "hold" and any(hit in r for r in reasons)
    report["env"] = {**same, "gpu_name": None}
    baseline["_meta"] = {"env": {**same, "gpu_name": "RTX 4090"}}
    assert gate_verdict_l3(report, baseline)[0] == "pass"


def test_env_snapshot_records_run_provenance_the_gate_compares(monkeypatch):
    from scripts import harness_l3

    monkeypatch.setattr(harness_l3, "query_gpu_name", lambda: "RTX 5060")
    env = harness_l3.env_snapshot(concurrency=4, duration_s=15, request_timeout_s=None)
    assert env["gpu_name"] == "RTX 5060"
    assert (env["concurrency"], env["duration_s"]) == (4, 15)
    assert "request_timeout_s" not in env  # an unset option must not be recorded as a pin


def _stub_l3_operations(monkeypatch):
    from types import SimpleNamespace

    from scripts import harness_l3

    from mainframe_rag import config, manifest

    monkeypatch.setattr(config, "load_settings", lambda: SimpleNamespace(qdrant_collection="synthetic"))
    monkeypatch.setattr(manifest, "write_run_manifest", lambda *args: {"git_sha": "synthetic"})
    monkeypatch.setattr(harness_l3, "run_load", lambda url, endpoint, *args, **kwargs: _sample_report()[endpoint])
    monkeypatch.setattr(harness_l3, "query_vram_mb", lambda: None)
    monkeypatch.setattr(harness_l3, "env_snapshot", lambda **kwargs: {})
    return harness_l3


def test_main_resolves_mode_baseline_per_invocation(tmp_path, monkeypatch):
    import json

    harness_l3 = _stub_l3_operations(monkeypatch)
    root = tmp_path / "workspace with spaces"
    directory = root / "benchmarks"
    directory.mkdir(parents=True)
    monkeypatch.setattr(harness_l3, "REPO", root)
    hash_path = directory / "harness-l3.json"
    real_path = directory / "harness-l3-vllm.json"
    hash_path.write_text(json.dumps(_sample_baseline()), encoding="utf-8")
    low_latency_baseline = _sample_baseline()
    low_latency_baseline["agent"]["search"]["latency_ms"]["p95"] = 1.0
    real_path.write_text(json.dumps(low_latency_baseline), encoding="utf-8")
    original = (hash_path.read_bytes(), real_path.read_bytes())

    # Both orders in the same imported interpreter; no reload or reset.
    for mode, expected in (("hash", 0), ("vllm", 1), ("vllm", 1), ("hash", 0), ("VLLM", 1), ("", 0)):
        monkeypatch.setenv("EMBED_MODE", mode)
        assert harness_l3.main(["--gate"]) == expected
    monkeypatch.delenv("EMBED_MODE", raising=False)
    assert harness_l3.main(["--gate"]) == 0
    monkeypatch.setenv("EMBED_MODE", "vllm")
    assert harness_l3.main(["--gate", "--baseline", str(hash_path)]) == 0
    assert (hash_path.read_bytes(), real_path.read_bytes()) == original


@pytest.fixture
def l3_task_workspace(tmp_path):
    """Actual Task and L3 CLI; only HTTP load, GPU and manifest effects are replaced."""
    import json
    import os
    import subprocess
    import sys

    from tests.helpers_eval_local import REPO_ROOT as repo
    from tests.helpers_eval_local import task_workspace

    task = task_workspace(tmp_path, "harness_l3.py", "loadtest.py")
    launcher = tmp_path / ".venv/bin/python"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(repo / 'src')!r})\n"
        "spec = importlib.util.spec_from_file_location('actual_l3_cli', sys.argv[1])\n"
        "cli = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(cli)\n"
        "from mainframe_rag import manifest\n"
        "manifest.write_run_manifest = lambda *a: {'git_sha': 'synthetic'}\n"
        "cli.query_vram_mb = lambda: None\n"
        "cli.query_gpu_name = lambda: None\n"
        f"reports = {_sample_report()!r}\n"
        "cli.run_load = lambda url, endpoint, *a, **k: reports[endpoint]\n"
        "raise SystemExit(cli.main(sys.argv[2:]))\n"
    )
    launcher.chmod(0o755)
    benchmarks = tmp_path / "benchmarks"
    benchmarks.mkdir()
    hash_path = benchmarks / "harness-l3.json"
    real_path = benchmarks / "harness-l3-vllm.json"
    hash_path.write_text(json.dumps(_sample_baseline()))
    low = _sample_baseline()
    low["agent"]["search"]["latency_ms"]["p95"] = 1.0
    real_path.write_text(json.dumps(low))
    custom = tmp_path / "custom אב;$(touch SENTINEL).json"
    custom.write_text(json.dumps(_sample_baseline()))
    before = {p: p.read_bytes() for p in (hash_path, real_path, custom)}
    base_env = {"PATH": os.defpath, "HOME": str(tmp_path), "QDRANT_COLLECTION": "synthetic", "VENUE": "dev"}
    def run(mode, override=None, ambient=None, record=False, direct=False):
        env = dict(base_env, EMBED_MODE="hash")
        if ambient is not None:
            env["HARNESS_L3_BASELINE"] = ambient
        if direct:
            env["EMBED_MODE"] = mode
            command = [str(launcher), "scripts/harness_l3.py", "--update-baseline" if record else "--gate"]
            selected = override if override is not None else ambient
            if selected is not None:
                command += ["--baseline", selected]
        else:
            command = [task, "--taskfile", str(tmp_path / "Taskfile.yml"),
                       "eval:harness:l3-baseline" if record else "eval:harness:l3", f"EMBED_MODE={mode}"]
            if override is not None:
                command += [f"HARNESS_L3_BASELINE={override}"]
        return subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False)
    return run, hash_path, real_path, custom, before


@pytest.mark.parametrize("mode,override,ambient,expected", [
    ("hash", None, None, 0),
    ("VLLM", None, None, 1),
    ("vllm", "custom", None, 0),
    ("hash", "missing.json", None, 1),
])
def test_task_l3_matches_actual_cli_baseline_decision(l3_task_workspace, mode, override, ambient, expected):
    run, _, _, custom, before = l3_task_workspace
    override = str(custom) if override == "custom" else override
    ambient = str(custom) if ambient == "custom" else ambient
    direct = run(mode, override, ambient, direct=True)
    task = run(mode, override, ambient)
    assert direct.returncode == expected, direct.stdout + direct.stderr
    assert (task.returncode == 0) == (expected == 0), task.stdout + task.stderr
    verdict = "pass" if expected == 0 else "baseline" if override in ("", "missing.json") or ambient == "" else "hold"
    assert f"L3 VERDICT: {verdict}" in task.stderr
    assert {p: p.read_bytes() for p in before} == before
    assert not (custom.parent / "SENTINEL").exists()


@pytest.mark.parametrize("mode,override,target", [
    ("VLLM", None, "vllm"),
    ("vllm", "", None),
])
def test_task_l3_recording_only_updates_selected_synthetic_baseline(l3_task_workspace, mode, override, target):
    import json

    run, hash_path, real_path, custom, before = l3_task_workspace
    override = str(custom) if override == "custom" else override
    task = run(mode, override, record=True)
    if target is None:
        assert task.returncode != 0
        assert {p: p.read_bytes() for p in before} == before
        return
    assert task.returncode == 0, task.stdout + task.stderr
    selected = {"hash": hash_path, "vllm": real_path, "custom": custom}[target]
    recorded = json.loads(selected.read_text())
    assert recorded["agent"]["search"]["latency_ms"]["p95"] == 20.0
    assert recorded["agent"]["answer"]["latency_ms"]["p95"] == 50.0
    assert recorded["_meta"]["env"]["embed_mode"] == mode.lower()
    for path, original in before.items():
        if path != selected:
            assert path.read_bytes() == original
    # Next ordinary gate uses the newly recorded baseline, without recording again.
    stored = selected.read_bytes()
    gate = run(mode, override)
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert selected.read_bytes() == stored
