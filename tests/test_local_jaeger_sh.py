"""Plan + fail-closed tests for scripts/run_local_jaeger.sh (local-dev only).

Hermetic: JAEGER_DRYRUN=1 validates inputs and prints the docker plan before
any daemon or network call. The reuse path is deliberately not unit-tested
(it probes a live port); the stack's integration behavior covers it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run_local_jaeger.sh"


def _run(extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "JAEGER_DRYRUN": "1", **(extra_env or {})}
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def test_dryrun_plan_pins_image_and_ports():
    r = _run()
    assert r.returncode == 0, r.stderr
    assert "docker run" in r.stdout and "local-jaeger" in r.stdout
    assert "127.0.0.1:16686:16686" in r.stdout
    assert "127.0.0.1:4318:4318" in r.stdout
    # Digest-pinned (same image the air-gap pack mirrors), never :latest.
    assert "cr.jaegertracing.io/jaegertracing/jaeger@sha256:" in r.stdout
    assert "latest" not in r.stdout


def test_dryrun_plan_ports_override():
    r = _run({"JAEGER_PORT": "26686", "JAEGER_OTLP_PORT": "24318"})
    assert r.returncode == 0, r.stderr
    assert "127.0.0.1:26686:16686" in r.stdout
    assert "127.0.0.1:24318:4318" in r.stdout


@pytest.mark.parametrize("var", ["JAEGER_PORT", "JAEGER_OTLP_PORT"])
@pytest.mark.parametrize("bad", ["not-a-number", "0"])
def test_bad_port_fails_closed(var, bad):
    r = _run({var: bad})
    assert r.returncode != 0
    assert f"{var} must be" in r.stderr


FAKE_CURL = """#!/bin/sh
# Reuse probe answers per env; the OTLP squatter probe answers per env too.
case "$*" in
    *api/services*) exit "${FAKE_REUSE:-1}" ;;
    *4318/*) exit "${FAKE_OTLP_BUSY:-1}" ;;
esac
exit 1
"""

FAKE_DOCKER = """#!/bin/sh
case "$*" in
    *"ps -a"*) printf '%s\\n' "${FAKE_DOCKER_NAMES:-}" ;;
esac
exit 0
"""


def _stub_tree(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("docker", FAKE_DOCKER)):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    return bin_dir


def _run_real(tmp_path, extra_env):
    env = {
        **os.environ,
        "PATH": f"{_stub_tree(tmp_path)}:/usr/bin:/bin",
        "JAEGER_DRYRUN": "0",
        **extra_env,
    }
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def test_reuse_path_never_touches_docker(tmp_path):
    r = _run_real(tmp_path, {"FAKE_REUSE": "0"})
    assert r.returncode == 0, r.stderr
    assert "reusing" in r.stdout


def test_existing_container_name_fails_closed(tmp_path):
    r = _run_real(tmp_path, {"FAKE_DOCKER_NAMES": "local-jaeger"})
    assert r.returncode != 0
    assert "already exists" in r.stderr


def test_non_jaeger_port_squatter_fails_closed(tmp_path):
    r = _run_real(tmp_path, {"FAKE_OTLP_BUSY": "0"})
    assert r.returncode != 0
    assert "not Jaeger" in r.stderr
