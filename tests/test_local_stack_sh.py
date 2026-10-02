"""Plan + fail-closed tests for scripts/run_local_stack.sh (local-dev only).

Hermetic: every case runs with LOCAL_STACK_DRYRUN=1, which validates inputs
and prints the ordered plan before any docker/network call.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run_local_stack.sh"


def _run(extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "LOCAL_STACK_DRYRUN": "1", **(extra_env or {})}
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def test_dryrun_plan_order_and_stages():
    r = _run()
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("[plan]")]
    assert len(lines) == 9
    # The prod topology order: backends -> Qdrant -> Jaeger -> gateway ->
    # probe -> ingest -> agent -> smoke -> trace check. Models are never
    # addressed straight from the agent; the plan hands the container
    # host.docker.internal URLs, and tracing is part of the stack.
    assert "backends" in lines[0] and "127.0.0.1:8000" in lines[0]
    assert "qdrant" in lines[1]
    assert "jaeger" in lines[2] and "run_local_jaeger.sh" in lines[2]
    assert "run_local_gateway.sh" in lines[3]
    assert "probe_gateway.py" in lines[4]
    assert "ingest" in lines[5] and "skipped" in lines[5]
    assert "uvicorn" in lines[6] and "OTEL_EXPORTER_OTLP_ENDPOINT" in lines[6]
    assert "UI_ENABLED=true" in lines[6]
    assert "/v1/search" in lines[7] and "/ui" in lines[7]
    assert "v1.search" in lines[8] and "mainframe-rag-agent" in lines[8]


def test_dryrun_ui_enabled_by_default():
    r = _run()
    assert r.returncode == 0, r.stderr
    agent = next(ln for ln in r.stdout.splitlines() if "uvicorn" in ln)
    assert "UI_ENABLED=true" in agent


def test_dryrun_ui_disabled_override_reflected():
    r = _run({"UI_ENABLED": "false"})
    assert r.returncode == 0, r.stderr
    agent = next(ln for ln in r.stdout.splitlines() if "uvicorn" in ln)
    assert "UI_ENABLED=false" in agent
    smoke = next(ln for ln in r.stdout.splitlines() if "/v1/search" in ln)
    assert "/ui" not in smoke and "UI disabled" in smoke


def test_dryrun_ingest_names_its_own_service(tmp_path):
    # A shared OTEL_SERVICE_NAME would merge agent and ingest in one Jaeger;
    # the plan pins the ingest service name explicitly.
    r = _run({"CORPUS_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    ingest = next(ln for ln in r.stdout.splitlines() if "ingest" in ln)
    assert "OTEL_SERVICE_NAME=mainframe-rag-ingest" in ingest


def test_dryrun_trace_timeout_override_reflected():
    r = _run({"OTEL_TRACE_TIMEOUT": "7"})
    assert r.returncode == 0, r.stderr
    assert "timeout 7s" in r.stdout


def test_dryrun_plan_includes_ingest_when_corpus_set(tmp_path):
    r = _run({"CORPUS_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    ingest = next(ln for ln in r.stdout.splitlines() if "ingest" in ln)
    assert str(tmp_path) in ingest and "skipped" not in ingest


def test_local_agent_port_override_reflected(tmp_path):
    r = _run({"LOCAL_AGENT_PORT": "9099"})
    assert r.returncode == 0, r.stderr
    assert "--port 9099" in r.stdout
    assert "http://127.0.0.1:9099/v1/search" in r.stdout


@pytest.mark.parametrize(
    "var",
    ["GATEWAY_PORT", "LOCAL_AGENT_PORT", "DENSE_DIM", "JAEGER_PORT", "JAEGER_OTLP_PORT", "OTEL_TRACE_TIMEOUT"],
)
@pytest.mark.parametrize("bad", ["not-a-number", "0"])
def test_bad_numeric_fails_closed(var, bad):
    r = _run({var: bad})
    assert r.returncode != 0
    assert f"{var} must be" in r.stderr


@pytest.mark.parametrize(
    "var", ["QDRANT_URL", "GATEWAY_REASONING_URL", "GATEWAY_EMBED_URL", "GATEWAY_RERANK_URL"]
)
def test_bare_hostname_fails_closed(var):
    r = _run({var: "gpu-box:8000"})
    assert r.returncode != 0
    assert f"{var} must begin with http:// or https://" in r.stderr


def test_env_file_inside_repo_fails_closed():
    r = _run({"GATEWAY_ENV_FILE": str(REPO / "dist" / "keys.env")})
    assert r.returncode != 0
    assert "must not live inside the repo" in r.stderr


def test_missing_corpus_dir_fails_closed():
    r = _run({"CORPUS_DIR": "/nonexistent/corpus"})
    assert r.returncode != 0
    assert "CORPUS_DIR is not a directory" in r.stderr


def test_rerank_false_selects_two_backend_plan():
    r = _run({'RERANK_ENABLED': 'false'})
    assert r.returncode == 0, r.stderr
    backend_line = r.stdout.splitlines()[0]
    assert '8000' in backend_line and '8001' in backend_line
    assert '8002' not in backend_line


def test_invalid_rerank_flag_fails_before_launch():
    r = _run({'RERANK_ENABLED': 'flase'})
    assert r.returncode != 0
    assert 'RERANK_ENABLED' in r.stderr


def test_two_backend_live_path_preserves_flag_after_gateway_handoff(tmp_path):
    # Stop deliberately at the probe after recording its actual environment.
    # This traverses real backend validation and the gateway handoff, without I/O.
    scripts = tmp_path / 'checkout' / 'scripts'
    scripts.mkdir(parents=True)
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text())
    (scripts / 'run_local_gateway.sh').write_text('''#!/bin/sh
printf 'export RERANK_ENABLED=true\\n' > "$GATEWAY_ENV_FILE"
''')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'calls'
    (bindir / 'curl').write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
printf '200'
''')
    (bindir / 'docker').write_text('#!/bin/sh\nexit 99\n')
    py = bindir / 'python'
    py.write_text('''#!/bin/sh
printf 'probe-rerank=%s\\n' "$RERANK_ENABLED" >> "$CALLS"
exit 23
''')
    for file in bindir.iterdir():
        file.chmod(0o755)
    result = subprocess.run(['sh', str(scripts / SCRIPT.name)], capture_output=True, text=True, check=False,
        env={**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'], 'CALLS': str(log),
             'LOCAL_STACK_DRYRUN': '0', 'RERANK_ENABLED': 'false', 'PY': str(py),
             'GATEWAY_ENV_FILE': str(tmp_path / 'gateway.env'), 'LOCAL_STACK_LOG_DIR': str(tmp_path)})
    assert result.returncode == 23, result.stderr
    calls = log.read_text()
    assert '8000/v1/models' in calls and '8001/v1/models' in calls
    assert '8002' not in calls
    assert 'probe-rerank=false' in calls


# --- Local ingest host-RAM budget (issue #580) -------------------------------

_INGEST_VARS = (
    "INGEST_WORKERS",
    "LOCAL_INGEST_WORKER_MB",
    "HOST_MEM_HEADROOM_MB",
    "HOST_PSI_MAX",
    "HOST_MEMINFO",
    "HOST_PSI_DIR",
    "HOST_CPUS",
    "FORCE_START",
)


def _host(tmp_path: Path, avail_mb: int = 64000, psi: float = 0.0) -> dict[str, str]:
    """Stub host state (meminfo + PSI) and CPU count; tests never read the real host."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: 99999999 kB\nMemAvailable: {avail_mb * 1024} kB\n")
    psi_dir = tmp_path / "pressure"
    psi_dir.mkdir(exist_ok=True)
    line = f"some avg10={psi:.2f} avg60=0.00 avg300=0.00 total=0\n"
    for resource in ("memory", "io"):
        (psi_dir / resource).write_text(line + line.replace("some", "full"))
    return {"HOST_MEMINFO": str(meminfo), "HOST_PSI_DIR": str(psi_dir), "HOST_CPUS": "8"}


def _plan_workers(tmp_path: Path, host: dict[str, str]) -> tuple[subprocess.CompletedProcess, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    clean = {k: v for k, v in os.environ.items() if k not in _INGEST_VARS}
    r = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={**clean, "LOCAL_STACK_DRYRUN": "1", "CORPUS_DIR": str(corpus), **host},
    )
    ingest = next((ln for ln in r.stdout.splitlines() if "ingest" in ln), "")
    return r, ingest


@pytest.mark.parametrize(
    ("avail_mb", "workers"),
    [
        (64000, 7),  # plenty of RAM: the CPU-1 cap (8 CPUs) applies
        (6144, 4),  # (6144-2048)/1024
        (4608, 2),  # a few GB free sizes down
        (3072, 1),  # exactly one worker + headroom
    ],
)
def test_ingest_workers_follow_available_ram(tmp_path, avail_mb, workers):
    r, ingest = _plan_workers(tmp_path, _host(tmp_path, avail_mb=avail_mb))
    assert r.returncode == 0, r.stderr
    assert f"--workers {workers} " in ingest
    assert "REFUSED" not in ingest and "REFUSED" not in r.stderr


def test_ingest_workers_floor_at_one_cpu(tmp_path):
    r, ingest = _plan_workers(tmp_path, {**_host(tmp_path), "HOST_CPUS": "1"})
    assert r.returncode == 0, r.stderr
    assert "--workers 1 " in ingest


def test_per_worker_estimate_and_headroom_envs_are_respected(tmp_path):
    host = {**_host(tmp_path, avail_mb=6144), "LOCAL_INGEST_WORKER_MB": "2048", "HOST_MEM_HEADROOM_MB": "0"}
    _, ingest = _plan_workers(tmp_path, host)
    assert "--workers 3 " in ingest


def test_ingest_workers_env_overrides_sizing(tmp_path):
    r, ingest = _plan_workers(tmp_path, {**_host(tmp_path, avail_mb=64000), "INGEST_WORKERS": "2"})
    assert r.returncode == 0, r.stderr
    assert "--workers 2 " in ingest


@pytest.mark.parametrize("bad", ["0", "-1", "lots", "2.5", "03"])
def test_invalid_ingest_workers_fails_closed(tmp_path, bad):
    r, _ = _plan_workers(tmp_path, {**_host(tmp_path), "INGEST_WORKERS": bad})
    assert r.returncode != 0
    assert "INGEST_WORKERS must be a positive integer" in r.stderr


def test_dryrun_flags_ingest_that_would_be_refused(tmp_path):
    # One worker + headroom = 3072 MiB; 3071 available cannot take any ingest.
    r, ingest = _plan_workers(tmp_path, _host(tmp_path, avail_mb=3071))
    assert r.returncode == 0, r.stderr
    assert "--workers 1 " in ingest and "would be REFUSED" in ingest
    assert "REFUSED: host RAM too low" in r.stderr


def _live(tmp_path: Path, host: dict[str, str], extra: dict[str, str] | None = None):
    """Run the live path with stub gateway/curl/docker/python up to the ingest."""
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text())
    (scripts / "run_local_gateway.sh").write_text(
        "#!/bin/sh\nprintf 'export RERANK_ENABLED=true\\n' > \"$GATEWAY_ENV_FILE\"\n"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    (bindir / "curl").write_text("#!/bin/sh\nprintf '200'\n")
    (bindir / "docker").write_text("#!/bin/sh\nexit 99\n")
    py = bindir / "python"
    # Every python call is recorded; the agent health check then sees a 200
    # and the script stops, so the run ends right after the ingest.
    py.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\n")
    for file in bindir.iterdir():
        file.chmod(0o755)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    clean = {k: v for k, v in os.environ.items() if k not in _INGEST_VARS}
    result = subprocess.run(
        ["sh", str(scripts / SCRIPT.name)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **clean,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "CALLS": str(calls),
            "PY": str(py),
            "CORPUS_DIR": str(corpus),
            "GATEWAY_ENV_FILE": str(tmp_path / "gateway.env"),
            "LOCAL_STACK_LOG_DIR": str(tmp_path),
            **host,
            **(extra or {}),
        },
    )
    return result, (calls.read_text().splitlines() if calls.exists() else [])


def _ingest_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if "mainframe_rag.ingest.run_ingest" in c]


def test_live_ingest_is_launched_with_the_sized_worker_count(tmp_path):
    r, calls = _live(tmp_path, _host(tmp_path, avail_mb=6144))
    ingest = _ingest_calls(calls)
    assert len(ingest) == 1, (r.stderr, calls)
    assert "--workers 4 " in ingest[0]


def test_live_low_ram_refuses_before_any_model_or_ingest_call(tmp_path):
    r, calls = _live(tmp_path, _host(tmp_path, avail_mb=3071))
    assert r.returncode == 75
    assert calls == [], "nothing may run (probe, ingest) on refusal"
    assert "REFUSED: host RAM too low" in r.stderr and "FORCE_START=1" in r.stderr


@pytest.mark.parametrize("resource", ["memory", "io"])
def test_live_pressure_refuses(tmp_path, resource):
    host = _host(tmp_path)
    (Path(host["HOST_PSI_DIR"]) / resource).write_text(
        "some avg10=10.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    r, calls = _live(tmp_path, host)
    assert r.returncode == 75 and calls == []
    assert f"REFUSED: host {resource} pressure is high" in r.stderr


def test_live_pressure_just_below_threshold_is_admitted(tmp_path):
    r, calls = _live(tmp_path, _host(tmp_path, psi=9.99))
    assert _ingest_calls(calls), r.stderr


def test_live_explicit_workers_are_still_admission_checked(tmp_path):
    # 7 workers x 1024 + 2048 = 9216 MiB needed.
    low = tmp_path / "low"
    low.mkdir()
    r, calls = _live(low, _host(low, avail_mb=9215), {"INGEST_WORKERS": "7"})
    assert r.returncode == 75 and calls == []
    ok = tmp_path / "ok"
    ok.mkdir()
    r, calls = _live(ok, _host(ok, avail_mb=9216), {"INGEST_WORKERS": "7"})
    ingest = _ingest_calls(calls)
    assert ingest and "--workers 7 " in ingest[0]


def test_live_force_start_skips_refusal_but_still_sizes_workers(tmp_path):
    host = _host(tmp_path, avail_mb=100, psi=80.0)
    r, calls = _live(tmp_path, host, {"FORCE_START": "1"})
    ingest = _ingest_calls(calls)
    assert ingest and "--workers 1 " in ingest[0], r.stderr
    assert "FORCE_START=1" in r.stderr


def test_missing_host_files_skip_checks_with_notice(tmp_path):
    host = {"HOST_MEMINFO": str(tmp_path / "none"), "HOST_PSI_DIR": str(tmp_path / "nodir"), "HOST_CPUS": "4"}
    r, ingest = _plan_workers(tmp_path, host)
    assert r.returncode == 0, r.stderr
    assert "--workers 3 " in ingest  # falls back to the CPU-1 default
    assert "skipping the RAM admission check" in r.stderr
    assert "skipping the memory pressure check" in r.stderr
