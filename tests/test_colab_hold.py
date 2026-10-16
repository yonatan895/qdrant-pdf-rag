"""Sandbox hold workload (#742): clean, bounded end conditions; no Colab calls."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from scripts import colab_hold

REPO = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now
        self.sleeps = 0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def test_stop_file_ends_the_hold_before_the_deadline(tmp_path):
    clock = _Clock(1000.0)

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.sleeps == 3:
            (tmp_path / "sandbox-stop").touch()

    assert colab_hold.hold(tmp_path, 10_000.0, clock=clock.time, sleep=sleep) == 0
    assert clock.sleeps == 3


def test_deadline_margin_ends_the_hold_without_waiting_past_it(tmp_path):
    clock = _Clock(1000.0)
    expiry = 1000.0 + colab_hold.DEADLINE_MARGIN_SECONDS + 3
    assert colab_hold.hold(tmp_path, expiry, clock=clock.time, sleep=clock.sleep) == 0
    assert clock.now <= expiry - colab_hold.DEADLINE_MARGIN_SECONDS + colab_hold.POLL_SECONDS
    already_late = _Clock(expiry)
    assert colab_hold.hold(tmp_path, expiry, clock=already_late.time, sleep=already_late.sleep) == 0
    assert already_late.sleeps == 0


@pytest.mark.parametrize("environment", [
    {},
    {"RAG_COLAB_STATE": "{state}", "RAG_COLAB_WORK_EXPIRY": "nan"},
    {"RAG_COLAB_STATE": "{missing}", "RAG_COLAB_WORK_EXPIRY": "1e18"},
])
def test_missing_or_invalid_controller_inputs_refuse_before_holding(tmp_path, environment):
    state = tmp_path / "state"
    state.mkdir()
    (state / "sandbox.env").write_text("export GATEWAY_RUN_OWNER=fixture\n")
    env = {"PATH": "/usr/bin:/bin"}
    env.update({key: value.format(state=state, missing=tmp_path / "absent") for key, value in environment.items()})
    result = subprocess.run([sys.executable, str(REPO / "scripts/colab_hold.py")], env=env,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 2 and "colab_hold:" in result.stderr


def test_missing_sandbox_handoff_refuses_before_holding(tmp_path):
    result = subprocess.run([sys.executable, str(REPO / "scripts/colab_hold.py")],
                            env={"PATH": "/usr/bin:/bin", "RAG_COLAB_STATE": str(tmp_path),
                                 "RAG_COLAB_WORK_EXPIRY": "1e18"},
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 2 and "sandbox handoff is unavailable" in result.stderr
