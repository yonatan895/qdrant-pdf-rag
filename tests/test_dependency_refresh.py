"""Offline candidate-lock verification for dependency refreshes (retro 3).

Refusal and argument units use a fake process/venv boundary: no wheel is
installed and no network is touched. The real local smoke is recorded in the
PR evidence against a complete prepared dev wheelhouse.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from scripts import dependency_lock as locks
from scripts import verify_dependency_refresh as refresh


def _report(items):
    return {
        "version": "1", "pip_version": "26.2.2",
        "environment": {"python_version": "3.14", "platform_python_implementation": "CPython",
                        "sys_platform": "linux", "platform_machine": "x86_64"},
        "install": items,
    }


@pytest.fixture
def candidate(tmp_path):
    """Synthetic dev lock, its complete wheelhouse and the focused test paths."""
    root = tmp_path / "root"
    root.mkdir()
    for relative in refresh.FOCUSED_TESTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# focused suite stand-in\n", encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    items = {}
    for name, version in (("example", "1.0"), ("pip", "26.2.2"), ("pytest", "9.1.1")):
        wheel = f"{name}-{version}-py3-none-any.whl"
        content = f"synthetic candidate wheel bytes: {name}\n".encode()
        (wheels / wheel).write_bytes(content)
        items[name] = {
            "metadata": {"name": name, "version": version},
            "download_info": {
                "url": "https://files.pythonhosted.org/packages/" + wheel,
                "archive_info": {"hashes": {"sha256": hashlib.sha256(content).hexdigest()}},
            },
        }
    for profile, names in (("runtime", ("example",)), ("dev", ("example", "pip", "pytest")),
                           ("build", ("pip",))):
        (reports / f"{profile}.json").write_text(
            json.dumps(_report([items[name] for name in names])), encoding="utf-8")
    locks.generate(root, reports)
    return root, wheels


class ProcessRecorder:
    """Fake subprocess boundary: records argv/env and creates only venv/bin/python."""

    def __init__(self, *, fail_steps=(), timeout_steps=()):
        self.calls: list[dict] = []
        self.fail_steps = fail_steps
        self.timeout_steps = timeout_steps

    @staticmethod
    def _step(command) -> str:
        if command[-1] == "target":
            return "target"
        if command[1:4] == ["-I", "-m", "venv"]:
            return "venv"
        if "install" in command:
            return "install"
        if command[-1] == "check":
            return "check"
        return "pytest" if "pytest" in command else "unknown"

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, "kwargs": kwargs})
        step = self._step(command)
        if step in self.timeout_steps:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))
        if step == "venv":
            target = Path(command[-1]) / "bin/python"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)
        if step in self.fail_steps:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)


def _temp_root(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "temps"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


def test_clean_environment_installs_offline_and_runs_focused_suites(candidate, tmp_path,
                                                                    monkeypatch):
    root, wheels = candidate
    temps = _temp_root(tmp_path, monkeypatch)
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").write_text("REPOSITORY-ENVIRONMENT\n", encoding="utf-8")
    locks_before = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (locks.MANIFEST, *locks.PROFILE_FILES.values())
    }
    monkeypatch.setenv("PIP_INDEX_URL", "https://credentials.invalid/simple")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing")
    runner = ProcessRecorder()

    receipt = refresh.verify(root, "dev", wheels, Path(sys.executable), runner=runner)

    commands = [call["command"] for call in runner.calls]
    assert len(commands) == 5
    python = str(Path(sys.executable).absolute())
    assert commands[0][0] == python and commands[0][-1] == "target"
    assert commands[0][-2] == str(root)
    venv = Path(commands[1][-1])
    assert commands[1][:5] == [python, "-I", "-m", "venv", "--without-pip"]
    assert venv.parent.parent == temps, "temporary environment must live under TMPDIR"
    install = commands[2]
    assert Path(install[0]) == venv / "bin/python"
    assert "--no-index" in install and "--no-cache-dir" in install
    assert install[install.index("--find-links") + 1] == str(wheels.absolute())
    assert install[install.index("-r") + 1].endswith("requirements.dev.lock.txt")
    assert not {"--index-url", "--extra-index-url"} & set(install)
    assert commands[3][-1] == "check"
    pytest_command = commands[4]
    assert pytest_command[1:3] == ["-m", "pytest"]
    assert all(test in pytest_command for test in refresh.FOCUSED_TESTS)
    assert pytest_command[pytest_command.index("-p") + 1] == "no:cacheprovider"

    for call in runner.calls:
        env = call["kwargs"]["env"]
        assert "PIP_INDEX_URL" not in env
        assert "PYTEST_ADDOPTS" not in env
        assert env["PIP_CONFIG_FILE"] == os.devnull
        assert env["PYTHONNOUSERSITE"] == "1"
        assert {key for key in env if key.startswith("PIP_")} <= {
            "PIP_CONFIG_FILE", "PIP_DISABLE_PIP_VERSION_CHECK"}
        assert str(root / ".venv") not in " ".join(call["command"])
    assert runner.calls[2]["kwargs"]["env"]["VIRTUAL_ENV"] == str(venv)

    assert receipt["profile"] == "dev" and receipt["wheels"] == 3
    assert receipt["lock_sha256"] == locks.digest(root / locks.MANIFEST)
    assert receipt["requirements_sha256"] == locks.digest(root / locks.PROFILE_FILES["dev"])
    assert receipt["checks"] == ["pip check", *refresh.FOCUSED_TESTS]
    assert (root / ".venv/bin/python").read_text() == "REPOSITORY-ENVIRONMENT\n"
    assert locks_before == {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (locks.MANIFEST, *locks.PROFILE_FILES.values())
    }
    assert not list(temps.iterdir()), "temporary venv must be removed on success"


@pytest.mark.parametrize(("target", "message"), [
    ("missing", "selected interpreter is unavailable"),
    ("wheelhouse", "wheelhouse directory is unavailable"),
])
def test_missing_interpreter_or_wheelhouse_refuses_before_effects(candidate, tmp_path,
                                                                  target, message):
    root, wheels = candidate
    runner = ProcessRecorder()
    with pytest.raises(refresh.RefreshError, match=message):
        if target == "missing":
            refresh.verify(root, "dev", wheels, tmp_path / "absent-python", runner=runner)
        else:
            refresh.verify(root, "dev", tmp_path / "absent-wheelhouse", Path(sys.executable),
                           runner=runner)
    assert runner.calls == []


def test_repository_environment_or_file_is_never_a_wheelhouse(candidate, tmp_path):
    root, _ = candidate
    (root / ".venv").mkdir()
    runner = ProcessRecorder()
    with pytest.raises(refresh.RefreshError, match="repository environment"):
        refresh.verify(root, "dev", root / ".venv", Path(sys.executable), runner=runner)
    not_a_directory = tmp_path / "wheelhouse-file"
    not_a_directory.write_text("", encoding="utf-8")
    with pytest.raises(refresh.RefreshError, match="wheelhouse directory is unavailable"):
        refresh.verify(root, "dev", not_a_directory, Path(sys.executable), runner=runner)
    assert runner.calls == []


@pytest.mark.parametrize("damage", ["missing", "extra", "tampered", "symlink"])
def test_incomplete_or_unsafe_wheelhouse_refuses_before_effects(candidate, tmp_path, damage):
    root, wheels = candidate
    selected = max(wheels.glob("*.whl"))
    if damage == "missing":
        selected.unlink()
    elif damage == "extra":
        (wheels / "unselected-1.0-py3-none-any.whl").write_bytes(b"extra")
    elif damage == "tampered":
        selected.write_bytes(b"tampered bytes")
    else:
        target = tmp_path / "outside.whl"
        target.write_bytes(selected.read_bytes())
        selected.unlink()
        selected.symlink_to(target)
    runner = ProcessRecorder()
    with pytest.raises(refresh.RefreshError, match="incomplete, unselected or tampered"):
        refresh.verify(root, "dev", wheels, Path(sys.executable), runner=runner)
    assert runner.calls == []


def test_only_the_dev_profile_has_the_focused_test_runner(candidate, tmp_path):
    root, wheels = candidate
    runner = ProcessRecorder()
    with pytest.raises(refresh.RefreshError, match="requires the complete dev profile"):
        refresh.verify(root, "runtime", wheels, Path(sys.executable), runner=runner)
    assert runner.calls == []


def test_missing_focused_suite_refuses_before_effects(candidate, tmp_path):
    root, wheels = candidate
    (root / refresh.FOCUSED_TESTS[0]).unlink()
    runner = ProcessRecorder()
    with pytest.raises(refresh.RefreshError, match="focused suite is missing"):
        refresh.verify(root, "dev", wheels, Path(sys.executable), runner=runner)
    assert runner.calls == []


@pytest.mark.parametrize(("fail_steps", "message"), [
    (("install",), "offline installation failed"),
    (("check",), "pip check failed"),
    (("pytest",), "focused suites failed"),
])
def test_install_check_or_suite_failure_stops_later_steps_and_cleans_up(candidate, tmp_path,
                                                                        monkeypatch, fail_steps,
                                                                        message):
    root, wheels = candidate
    temps = _temp_root(tmp_path, monkeypatch)
    runner = ProcessRecorder(fail_steps=fail_steps)
    with pytest.raises(refresh.RefreshError, match=message):
        refresh.verify(root, "dev", wheels, Path(sys.executable), runner=runner)
    assert not list(temps.iterdir())
    if fail_steps == ("install",):
        assert len(runner.calls) == 3
        assert "install" in runner.calls[-1]["command"]
    elif fail_steps == ("check",):
        assert len(runner.calls) == 4
        assert "install" in runner.calls[-2]["command"]
        assert runner.calls[-1]["command"][-1] == "check"
    else:
        assert len(runner.calls) == 5
        assert "pytest" in runner.calls[-1]["command"]


def test_timeout_is_bounded_and_cleans_up(candidate, tmp_path, monkeypatch):
    root, wheels = candidate
    temps = _temp_root(tmp_path, monkeypatch)
    runner = ProcessRecorder(timeout_steps=("install",))
    with pytest.raises(refresh.RefreshError, match="exceeded its time limit"):
        refresh.verify(root, "dev", wheels, Path(sys.executable), runner=runner)
    assert not list(temps.iterdir())


def test_main_returns_fixed_message_and_nonzero_exit(candidate, tmp_path, capsys):
    root, _ = candidate
    code = refresh.main(["--root", str(root), "--wheelhouse", str(tmp_path / "absent")])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "dependency-refresh: wheelhouse directory is unavailable\n"


def test_main_accepts_an_explicit_profile_and_interpreter(candidate, capsys, monkeypatch):
    root, wheels = candidate
    monkeypatch.setattr(refresh, "verify", lambda *args, **kwargs: {"schema_version": 1})
    code = refresh.main(["--root", str(root), "--profile", "dev", "--wheelhouse", str(wheels),
                         "--python", sys.executable])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"schema_version": 1}
