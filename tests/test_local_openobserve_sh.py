"""Plan + fail-closed tests for scripts/run_local_openobserve.sh (local-dev only).

Hermetic: O2_DRYRUN=1 validates inputs and prints the docker plan before any
daemon or network call; the stubbed-docker tests exercise the real start path.
The properties ported from the retired Jaeger script: digest pin, loopback-only
publish, bounded memory without swap, bad inputs die before docker, an existing
container or a foreign port squatter fails closed, and a healthy backend is
reused without touching docker. New: root credentials only via a 0600 env file,
never on any command line.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run_local_openobserve.sh"
O2_DIGEST = "sha256:4a154849432f75c4fb024de32fe34163c02406c905dab22bf8d213d2e3386392"
OC_DIGEST = "sha256:799dc6cf12c96192af37b5bdba804da8c10b3bc563b43cb90c3f3c58d9572ad6"
PASSWORD = "s3cret-PASSWORD-canary"


def _run(extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "O2_DRYRUN": "1", **(extra_env or {})}
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def test_dryrun_plan_pins_image_loopback_port_and_hardening():
    r = _run()
    assert r.returncode == 0, r.stderr
    assert "docker run" in r.stdout and "local-openobserve" in r.stdout
    assert "-p 127.0.0.1:5080:5080" in r.stdout
    assert f"docker.io/openobserve/openobserve@{O2_DIGEST}" in r.stdout
    assert "latest" not in r.stdout
    assert "--memory 1g --memory-swap 1g" in r.stdout
    assert "-v local-openobserve-data:/data" in r.stdout
    for flag in ("--read-only", "--cap-drop ALL", "no-new-privileges"):
        assert flag in r.stdout
    for setting in ("ZO_TELEMETRY=false", "ZO_MODEL_PRICING_ENABLED=false", "ZO_MMDB_DISABLE_DOWNLOAD=true"):
        assert setting in r.stdout


def test_collector_plan_only_when_requested():
    assert "local collector" not in _run().stdout
    r = _run({"O2_COLLECTOR": "1", "O2_AGENT_PORT": "9099"})
    assert r.returncode == 0, r.stderr
    assert "--network host" in r.stdout and "127.0.0.1:9099/metrics" in r.stdout
    assert f"opentelemetry-collector-contrib@{OC_DIGEST}" in r.stdout


def test_dryrun_never_names_credentials():
    r = _run({"O2_ENV_FILE": "/nonexistent/o2.env"})
    assert r.returncode == 0, r.stderr
    assert "--env-file </nonexistent/o2.env, mode 0600>" in r.stdout
    assert "PASSWORD" not in r.stdout and "BASIC_AUTH" not in r.stdout


@pytest.mark.parametrize(
    "var, bad",
    [("O2_MEMORY", "0g"), ("O2_MEMORY", "1g --privileged"), ("O2_COLLECTOR", "yes"),
     ("O2_VOLUME", "x -v /:/host"), ("O2_NAME", "--privileged")],
)
def test_bad_bound_fails_closed_before_docker(var, bad):
    r = _run({var: bad})
    assert r.returncode != 0
    assert f"{var} must" in r.stderr
    assert "docker run" not in r.stdout


@pytest.mark.parametrize("var", ["O2_PORT", "O2_AGENT_PORT"])
def test_bad_port_fails_closed(var):
    r = _run({var: "0"})
    assert r.returncode != 0
    assert f"{var} must be" in r.stderr


FAKE_CURL = """#!/bin/sh
# Health probe answers per env; the squatter probe (any other URL) answers per env too.
case "$*" in
    */healthz*) [ "${FAKE_HEALTHY:-0}" = 1 ] && echo '{"status":"ok"}'; exit 0 ;;
    *) exit "${FAKE_PORT_BUSY:-1}" ;;
esac
"""

FAKE_DOCKER = """#!/bin/sh
case "$*" in
    *"ps -a"*) printf '%s\\n' "${FAKE_DOCKER_NAMES:-}" ;;
esac
exit 0
"""

RECORDING_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
    ps) exit 0 ;;
    run) printf '%s\\n' "$@" > "$FAKE_DOCKER_ARGV.$(date +%s%N)" ;;
esac
exit 0
"""


def _stub_tree(tmp_path: Path, docker: str = FAKE_DOCKER) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("docker", docker)):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    return bin_dir


def _env_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "o2.env"
    path.write_text(f"ZO_ROOT_USER_EMAIL=r@x.invalid\nZO_ROOT_USER_PASSWORD={PASSWORD}\nO2_BASIC_AUTH=Zm9v\n")
    path.chmod(mode)
    return path


def _run_real(tmp_path, extra_env, docker=FAKE_DOCKER):
    env = {
        **os.environ,
        "PATH": f"{_stub_tree(tmp_path, docker)}:/usr/bin:/bin",
        "O2_DRYRUN": "0",
        "O2_ENV_FILE": str(_env_file(tmp_path)),
        "O2_LOG": str(tmp_path / "o2.log"),
        **extra_env,
    }
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def test_reuse_path_never_touches_docker(tmp_path):
    r = _run_real(tmp_path, {"FAKE_HEALTHY": "1"})
    assert r.returncode == 0, r.stderr
    assert "reusing" in r.stdout


def test_existing_container_name_fails_closed(tmp_path):
    r = _run_real(tmp_path, {"FAKE_DOCKER_NAMES": "local-openobserve"})
    assert r.returncode != 0
    assert "already exists" in r.stderr


def test_existing_collector_name_fails_closed(tmp_path):
    r = _run_real(tmp_path, {"FAKE_DOCKER_NAMES": "local-otelcol", "O2_COLLECTOR": "1"})
    assert r.returncode != 0
    assert "already exists" in r.stderr


def test_non_openobserve_port_squatter_fails_closed(tmp_path):
    r = _run_real(tmp_path, {"FAKE_PORT_BUSY": "0"})
    assert r.returncode != 0
    assert "not OpenObserve" in r.stderr


def test_env_file_must_be_private(tmp_path):
    env = {
        **os.environ,
        "PATH": f"{_stub_tree(tmp_path)}:/usr/bin:/bin",
        "O2_DRYRUN": "0",
        "O2_ENV_FILE": str(_env_file(tmp_path, 0o644)),
    }
    r = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)
    assert r.returncode != 0 and "mode 0600" in r.stderr
    assert PASSWORD not in r.stdout + r.stderr


def test_first_start_creates_private_credentials_file(tmp_path):
    target = tmp_path / "cfg" / "o2.env"
    env = {
        **os.environ,
        "PATH": f"{_stub_tree(tmp_path)}:/usr/bin:/bin",
        "O2_DRYRUN": "0",
        "O2_ENV_FILE": str(target),
        "O2_LOG": str(tmp_path / "o2.log"),
    }
    r = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    keys = dict(line.split("=", 1) for line in target.read_text().splitlines())
    assert set(keys) == {"ZO_ROOT_USER_EMAIL", "ZO_ROOT_USER_PASSWORD", "O2_BASIC_AUTH"}
    assert len(keys["ZO_ROOT_USER_PASSWORD"]) >= 32
    assert keys["ZO_ROOT_USER_PASSWORD"] not in r.stdout + r.stderr


def test_started_container_carries_bounds_and_no_credentials_on_argv(tmp_path):
    """The real start path (not only the dry-run plan) passes the memory cap,
    the hardening and the env file to docker as separate argv entries, and no
    argv entry carries a credential."""
    log = tmp_path / "docker.log"
    log.write_text("")
    r = _run_real(tmp_path, {"FAKE_DOCKER_LOG": str(log), "FAKE_DOCKER_ARGV": str(tmp_path / "argv"),
                             "O2_MEMORY": "768m"}, docker=RECORDING_DOCKER)
    # The fake container exits at once, so readiness fails closed afterwards.
    assert r.returncode != 0 and "exited early" in r.stderr
    argv_file = next(tmp_path.glob("argv.*"))
    argv = argv_file.read_text().splitlines()
    assert argv[argv.index("--memory") + 1] == "768m"
    assert argv[argv.index("--memory-swap") + 1] == "768m"
    assert argv[argv.index("-p") + 1] == "127.0.0.1:5080:5080"
    assert argv[argv.index("--env-file") + 1] == str(tmp_path / "o2.env")
    assert "--cap-drop" in argv and "--read-only" in argv
    assert argv[-1] == f"docker.io/openobserve/openobserve@{O2_DIGEST}"
    assert PASSWORD not in "\n".join(argv) + log.read_text() + r.stdout + r.stderr
