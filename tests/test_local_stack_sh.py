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
    assert len(lines) == 7
    # The prod topology order: backends -> Qdrant -> gateway -> probe ->
    # ingest -> agent -> smoke. Models are never addressed straight from
    # the agent; the plan hands the container host.docker.internal URLs.
    assert "backends" in lines[0] and "127.0.0.1:8000" in lines[0]
    assert "qdrant" in lines[1]
    assert "run_local_gateway.sh" in lines[2]
    assert "probe_gateway.py" in lines[3]
    assert "ingest" in lines[4] and "skipped" in lines[4]
    assert "uvicorn" in lines[5]
    assert "/v1/search" in lines[6]


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


@pytest.mark.parametrize("var", ["GATEWAY_PORT", "LOCAL_AGENT_PORT", "DENSE_DIM"])
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
