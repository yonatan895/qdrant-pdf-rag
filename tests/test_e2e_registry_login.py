"""Execute the workflow's registry-login step with a hermetic Skopeo double."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.helpers_airgap import REPO


def _login_step() -> str:
    workflow = (REPO / ".github/workflows/e2e.yml").read_text()
    step = workflow.split("      - name: Log in to Red Hat registry\n", 1)[1].split("\n      - name:", 1)[0]
    for key in ("REDHAT_REGISTRY_USER", "REDHAT_REGISTRY_PASSWORD"):
        assert f"{key}: ${{{{ secrets.{key} }}}}" in step
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


def _run_login(tmp_path: Path, username: str, password: str, exit_code: int = 0):
    bash = shutil.which("bash")
    assert bash is not None
    executable = tmp_path / "skopeo"
    record = tmp_path / "login.json"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['LOGIN_RECORD']).write_text(json.dumps([sys.argv[1:], sys.stdin.read()]))\n"
        "sys.exit(int(os.environ['LOGIN_EXIT']))\n"
    )
    executable.chmod(0o755)
    result = subprocess.run(
        [bash, "-eu", "-o", "pipefail", "-c", _login_step()],
        env={
            **os.environ,
            "PATH": str(tmp_path),
            "LOGIN_RECORD": str(record),
            "LOGIN_EXIT": str(exit_code),
            "REDHAT_REGISTRY_USER": username,
            "REDHAT_REGISTRY_PASSWORD": password,
        },
        capture_output=True, text=True, check=False,
    )
    return result, record


@pytest.mark.parametrize("username,password", [("", ""), ("user", ""), ("", "synthetic-secret")])
def test_registry_login_requires_both_secrets(tmp_path, username, password):
    result, record = _run_login(tmp_path, username, password)
    assert result.returncode != 0
    assert "are required to package the pinned OAuth image" in result.stdout
    assert not record.exists()
    assert "synthetic-secret" not in result.stdout + result.stderr


def test_registry_login_preserves_credentials_without_password_in_argv(tmp_path):
    username = "123|release user"
    password = "synthetic 'secret' $(literal) `literal` \\ value"
    result, record = _run_login(tmp_path, username, password)
    assert result.returncode == 0, result.stderr
    assert json.loads(record.read_text()) == [
        ["login", "registry.redhat.io", "--username", username, "--password-stdin"], password,
    ]
    assert password not in result.stdout + result.stderr


def test_registry_login_failure_stops_the_step(tmp_path):
    result, record = _run_login(tmp_path, "user", "synthetic-secret", exit_code=27)
    assert record.exists()
    assert result.returncode == 27
