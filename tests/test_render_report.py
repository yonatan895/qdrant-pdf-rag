"""Unit tests for eval.reports (pure functions, no network/docker)."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest

from mainframe_rag.eval.reports import (
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



def test_l1_consumes_canonical_report_renderer():
    from scripts import gate_l1

    from mainframe_rag.eval import reports

    assert gate_l1.render_eval is reports.render_eval


@pytest.fixture
def report_workspace(tmp_path):
    from tests.test_taskfile_contracts import REQUIRE_RUNNER, find_task

    task = find_task()
    if task is None:
        if REQUIRE_RUNNER:
            pytest.fail("pinned Task unavailable in required lane")
        pytest.skip("pinned Task unavailable")
    repo = Path(__file__).resolve().parents[1]
    shutil.copy2(repo / "Taskfile.yml", tmp_path / "Taskfile.yml")
    shutil.copytree(repo / "taskfiles", tmp_path / "taskfiles")
    (tmp_path / "src").symlink_to(repo / "src", target_is_directory=True)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    launcher = tmp_path / ".venv/bin/python"
    launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    launcher.chmod(0o755)
    paths, values = {}, {}
    for family, make_report, baseline_dir in (("eval", _eval_report, "evals"), ("bench", _bench_report, "benchmarks")):
        for override in (False, True):
            current, baseline = make_report(), make_report()
            if family == "eval":
                current["collection"] = "<b>Original override</b>" if override else "Original default"
                current["recall@1"] = 0.125 if override else 0.5
                baseline["recall@1"] = 0.875 if override else 0.75
            else:
                current["env"]["qdrant_image"] = "<b>Original override</b>" if override else "Original default"
                current["agent"]["search"]["rps"] = 125 if override else 400
                baseline["agent"]["search"]["rps"] = 875 if override else 600
            current_path = (f"explicit {family} אב;$(touch SENTINEL).json" if override else f"bundles/{family}-report.json")
            baseline_path = (f"explicit {family} baseline אב;$(touch SENTINEL).json" if override else f"{baseline_dir}/baseline.json")
            for path, data in ((current_path, current), (baseline_path, baseline)):
                target = tmp_path / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(data))
            paths[family, override] = current_path, baseline_path
            values[family, override] = current, baseline
    # Report defaults are deliberately not the retrieval mode-keyed baseline.
    (tmp_path / "evals/baseline-vllm.json").write_text("wrong mode-selected file")
    def run(operation, *args, ambient=None):
        return subprocess.run([task, "--taskfile", str(tmp_path / "Taskfile.yml"), f"eval:{operation}", *args],
            cwd=tmp_path, env={"PATH": os.defpath, "HOME": str(tmp_path),
                               "EMBED_MODE": "vllm", **(ambient or {})},
            capture_output=True, text=True, timeout=30, check=False)
    return tmp_path, run, paths, values


@pytest.mark.parametrize("inherited_source", [False, True])
@pytest.mark.parametrize("operation,module", [
    ("answers", "answers"), ("chat", "chat"), ("report", "reports"),
    ("html", "reports"), ("compare", "reports"), ("bench-report", "reports"),
    ("bench-html", "reports"), ("bench-compare", "reports"),
])
def test_module_tasks_execute_checkout_with_foreign_editable(report_workspace, operation, module, inherited_source):
    root, run, _paths, values = report_workspace
    foreign = root / "foreign-src"
    package = foreign / "mainframe_rag"
    (package / "eval").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "eval/__init__.py").write_text("")
    for name in ("answers", "chat", "reports"):
        (package / f"eval/{name}.py").write_text("raise SystemExit('foreign checkout executed')\n")
    # Real interpreter + editable .pth: no launcher that inserts the right src.
    environment = root / ".venv"
    (environment / "bin/python").unlink()
    venv.EnvBuilder(with_pip=False).create(environment)
    site = Path(sysconfig.get_path("purelib", vars={"base": str(environment), "platbase": str(environment)}))
    (site / "foreign-editable.pth").write_text(f"{foreign}\n{sysconfig.get_path('purelib')}\n")
    (foreign / "sitecustomize.py").write_text('''
import atexit, json, os, sys
from pathlib import Path
def trace():
    module = sys.modules['__main__']
    Path(os.environ['IMPORT_TRACE']).write_text(json.dumps({
        'module': module.__spec__.name, 'file': module.__file__}))
if 'IMPORT_TRACE' in os.environ:
    atexit.register(trace)
''')
    probe = subprocess.run([str(environment / "bin/python"), "-c",
                            "import mainframe_rag; print(mainframe_rag.__file__)"],
                           env={"PATH": os.defpath}, capture_output=True, text=True, check=True)
    assert Path(probe.stdout.strip()) == package / "__init__.py"
    trace = root / "import-trace.json"
    ambient = {"IMPORT_TRACE": str(trace)}
    if inherited_source:
        ambient["PYTHONPATH"] = str(foreign)
    # Parser refusal exercises the answer/chat module without model/storage work.
    proc = run(operation, *(["N=invalid"] if module != "reports" else []), ambient=ambient)
    if module != "reports":
        assert proc.returncode != 0
        assert "invalid int value" in proc.stderr
    else:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        family = "bench" if operation.startswith("bench-") else "eval"
        current, baseline = values[family, False]
        if operation.endswith("html"):
            renderer = render_eval if family == "eval" else render_bench
            assert (root / f"bundles/{family}-report.html").read_text() == renderer(current, baseline, "html")
        else:
            renderer = ((compare_eval if family == "eval" else compare_bench) if operation.endswith("compare")
                        else (render_eval if family == "eval" else render_bench))
            result = renderer(baseline, current, "text")[0] if operation.endswith("compare") else renderer(current, baseline, "text")
            assert proc.stdout == result + "\n"
    assert json.loads(trace.read_text()) == {
        "module": f"mainframe_rag.eval.{module}",
        "file": str(root / f"src/mainframe_rag/eval/{module}.py"),
    }


@pytest.mark.parametrize("operation,family,kind", [
    ("report", "eval", "text"), ("html", "eval", "html"), ("compare", "eval", "compare"),
    ("bench-report", "bench", "text"), ("bench-html", "bench", "html"), ("bench-compare", "bench", "compare"),
])
@pytest.mark.parametrize("selection", ["default", "empty", "override", "ambient"])
def test_report_task_selects_recorded_inputs(report_workspace, operation, family, kind, selection):
    root, run, paths, values = report_workspace
    override = selection in ("override", "ambient")
    current, baseline = values[family, override]
    current_path, baseline_path = paths[family, override]
    first, second = ("CURRENT", "BASE") if kind == "compare" else ("REPORT", "BASELINE")
    ambient, args = {}, []
    if selection == "empty":
        args = [f"{first}=", f"{second}=", "OUT="]
        ambient = {first: "missing.json", second: "missing.json", "OUT": "wrong.html"}
    elif override:
        selected = {first: current_path, second: baseline_path, "OUT": "output אב;$(touch SENTINEL)/chosen.html"}
        if selection == "override":
            args = [f"{key}={value}" for key, value in selected.items()]
            ambient = {first: "missing.json", second: "missing.json", "OUT": "wrong.html"}
        else:
            ambient = selected
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    proc = run(operation, *args, ambient=ambient)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    if kind == "compare":
        expected, _ = (compare_eval if family == "eval" else compare_bench)(baseline, current, "text")
        assert proc.stdout == expected + "\n"
    else:
        expected = (render_eval if family == "eval" else render_bench)(current, baseline, kind)
        if kind == "html":
            output = root / ("output אב;$(touch SENTINEL)/chosen.html" if override else f"bundles/{family}-report.html")
            assert output.read_text() == expected
            assert str(output.relative_to(root)) in proc.stdout
            if override:
                assert "&lt;b&gt;Original override&lt;/b&gt;" in expected
                assert "<b>Original override</b>" not in expected
        else:
            assert proc.stdout == expected + "\n"
    assert {p: p.read_bytes() for p in before} == before
    assert not (root / "SENTINEL").exists()
    assert not (root / "wrong.html").exists()


@pytest.mark.parametrize("family,operation", [("eval", "html"), ("bench", "bench-html")])
@pytest.mark.parametrize("input_index", [0, 1])
def test_report_invalid_input_preserves_output_and_recovers(report_workspace, family, operation, input_index):
    root, run, paths, _values = report_workspace
    assert run(operation).returncode == 0
    output = root / f"bundles/{family}-report.html"
    before = output.read_bytes()
    source = root / paths[family, False][input_index]
    original = source.read_bytes()
    for corrupt in (False, True):
        if corrupt:
            source.write_text("broken json")
        else:
            source.unlink()
        proc = run(operation)
        assert proc.returncode != 0
        assert output.read_bytes() == before
        source.write_bytes(original)
        proc = run(operation)
        assert proc.returncode == 0, proc.stderr
        assert output.read_bytes() == before


@pytest.mark.parametrize("family", ["eval", "bench"])
def test_report_bundle_defaults_require_explicit_opt_in(tmp_path, monkeypatch, capsys, family):
    monkeypatch.chdir(tmp_path)
    current = _eval_report() if family == "eval" else _bench_report()
    baseline = json.loads(json.dumps(current))
    bundle = tmp_path / "bundle אב;$(touch SENTINEL)"
    bundle.mkdir()
    report = bundle / f"{family}-report.json"
    report.write_text(json.dumps(current))
    base = tmp_path / ("evals/baseline.json" if family == "eval" else "benchmarks/baseline.json")
    base.parent.mkdir()
    base.write_text(json.dumps(baseline))
    output = bundle / f"{family}-report.html"
    # Legacy explicit CLI: optional baseline and stdout HTML are preserved.
    assert main([family, "--report", str(report), "--format", "html"]) == 0
    assert "<!DOCTYPE html>" in capsys.readouterr().out
    assert not output.exists()
    for command in (family, f"compare-{family}"):
        with pytest.raises(SystemExit) as exc:
            main([command])
        assert exc.value.code == 2
    assert main([family, "--bundle-dir", str(bundle), "--format", "html"]) == 0
    renderer = render_eval if family == "eval" else render_bench
    assert output.read_text() == renderer(current, baseline, "html")
    assert str(output) in capsys.readouterr().out
    assert main([f"compare-{family}", "--bundle-dir", str(bundle), "--fail-on-regression"]) == 0
    if family == "eval":
        current["rows"][0].update({"recall@1": 0.0, "recall@5": 0.0, "mrr": 0.0})
    else:
        current["agent"]["search"]["rps"] = 1.0
    report.write_text(json.dumps(current))
    assert main([f"compare-{family}", "--bundle-dir", str(bundle), "--fail-on-regression"]) == 1
    assert main([f"compare-{family}", "--bundle-dir", str(bundle)]) == 0
    for flag in ("--report", "--baseline", "--out"):
        with pytest.raises(IsADirectoryError):
            main([family, "--bundle-dir", str(bundle), flag, ""])
    assert not (tmp_path / "SENTINEL").exists()


@pytest.mark.parametrize("command", ["eval", "bench", "compare-eval", "compare-bench"])
def test_empty_report_bundle_preserves_root_relative_read_contract(monkeypatch, command):
    from mainframe_rag.eval import reports

    family = "bench" if command.endswith("bench") else "eval"
    seen = []
    def load(path):
        seen.append(path)
        return _eval_report() if family == "eval" else _bench_report()
    # Inspect the actual loader boundary without accessing host-root files.
    monkeypatch.setattr(reports, "_load_json", load)
    assert main([command, "--bundle-dir", ""]) == 0
    current = Path(f"/{family}-report.json")
    baseline = Path("evals/baseline.json" if family == "eval" else "benchmarks/baseline.json")
    assert seen == ([baseline, current] if command.startswith("compare") else [current, baseline])
    if not command.startswith("compare"):
        seen.clear()
        with pytest.raises(SystemExit) as exc:
            main([command, "--bundle-dir", "", "--format", "html"])
        assert exc.value.code == 2
        assert seen == []
