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
