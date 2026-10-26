"""Unit tests for scripts/parallel_guard.py.

Hermetic: `/proc/meminfo` and `systemd-run` are faked on disk. The fake
systemd-run records the argv it receives, so exact forwarding, the memory cap
and exit propagation are asserted without a user systemd manager or any heavy
child process.
"""

import json
import sys
from pathlib import Path

import pytest
import scripts.parallel_guard as guard

FAKE_SYSTEMD_RUN = """#!{python}
import json
import os
import sys

args = sys.argv[1:]
if os.environ.get("PARALLEL_GUARD_REJECT_NO_EXPAND") and "--expand-environment=no" in args:
    raise SystemExit(1)
if "--" in args:
    with open(os.environ["PARALLEL_GUARD_LOG"], "w", encoding="utf-8") as handle:
        json.dump(args, handle)
    raise SystemExit(int(os.environ.get("PARALLEL_GUARD_CHILD_EXIT", "0")))
raise SystemExit(int(os.environ.get("PARALLEL_GUARD_PROBE_EXIT", "0")))
"""


def write_meminfo(tmp_path, available_mib):
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal: {available_mib * 4096} kB\nMemAvailable: {available_mib * 1024} kB\n")
    return path


@pytest.fixture
def fake_systemd(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "systemd-run"
    fake.write_text(FAKE_SYSTEMD_RUN.format(python=sys.executable))
    fake.chmod(0o755)
    log = tmp_path / "argv.json"
    monkeypatch.setenv("PARALLEL_GUARD_LOG", str(log))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(guard, "MEMINFO_PATH", write_meminfo(tmp_path, 8192))
    return log


def test_available_mib_parses_and_missing_is_an_error():
    assert guard.available_mib("MemTotal: 8192 kB\nMemAvailable: 1536 kB\n") == 1
    assert guard.available_mib("MemAvailable: 2097152 kB\n") == 2048
    with pytest.raises(ValueError):
        guard.available_mib("MemTotal: 8192 kB\n")
    with pytest.raises(ValueError):
        guard.available_mib("MemAvailable: lots kB\n")


def test_refuses_below_floor_with_fixed_message(fake_systemd, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(guard, "MEMINFO_PATH", write_meminfo(tmp_path, 512))
    marker = tmp_path / "ran"
    code = guard.main(["--no-memory-cap", "--", sys.executable, "-c",
                       "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('x')",
                       str(marker)])
    assert code == 2
    assert not marker.exists()
    err = capsys.readouterr().err
    assert "parallel_guard: refusing to run:" in err
    assert "MemAvailable 512 MiB is below the 1024 MiB floor" in err


def test_custom_floor_is_used(fake_systemd, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(guard, "MEMINFO_PATH", write_meminfo(tmp_path, 1536))
    code = guard.main(["--min-available", "2048", "--no-memory-cap", "--", sys.executable, "-c",
                       "print(1)"])
    assert code == 2
    assert "below the 2048 MiB floor" in capsys.readouterr().err


def test_missing_systemd_run_refuses_unless_no_memory_cap(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(guard, "MEMINFO_PATH", write_meminfo(tmp_path, 8192))
    marker = tmp_path / "ran"

    code = guard.main(["--", sys.executable, "-c", "print(1)"])
    assert code == 2
    assert "systemd-run --user is unavailable for the memory cap; pass --no-memory-cap" in (
        capsys.readouterr().err
    )
    assert not marker.exists()

    code = guard.main(["--no-memory-cap", "--", sys.executable, "-c",
                       "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('x')",
                       str(marker)])
    assert code == 0
    assert marker.read_text() == "x"


def test_forwards_exact_argv_under_the_memory_cap(fake_systemd):
    payload = ["$HOME", "$(id)", "${HOME}", "$1", "a b", ";", "--memory-max", "1"]
    code = guard.main(["--memory-max", "777", "--", sys.executable, "-c", "print(1)", *payload])
    assert code == 0
    argv = json.loads(fake_systemd.read_text())
    assert argv == [
        "--user", "--scope", "-q", "--expand-environment=no",
        "-p", "MemoryMax=777M",
        "-p", "MemorySwapMax=0",
        "--", sys.executable, "-c", "print(1)", *payload,
    ]


def test_escapes_dollars_when_systemd_lacks_no_expand(fake_systemd, monkeypatch):
    monkeypatch.setenv("PARALLEL_GUARD_REJECT_NO_EXPAND", "1")
    payload = ["$HOME", "$(id)", "${HOME}", "$1", "a b", ";", "$$"]
    code = guard.main(["--", sys.executable, "-c", "print(1)", *payload])
    assert code == 0
    argv = json.loads(fake_systemd.read_text())
    assert argv[:3] == ["--user", "--scope", "-q"]
    assert "--expand-environment=no" not in argv
    assert argv[-len(payload):] == [argument.replace("$", "$$") for argument in payload]


def test_script_is_executable():
    script = Path(__file__).resolve().parents[1] / "scripts" / "parallel_guard.py"
    assert script.stat().st_mode & 0o111


def test_direct_mode_reports_unexecutable_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(guard, "MEMINFO_PATH", write_meminfo(tmp_path, 8192))
    missing = tmp_path / "missing"
    assert guard.main(["--no-memory-cap", "--", str(missing)]) == 127
    assert "not found" in capsys.readouterr().err

    blocked = tmp_path / "blocked"
    blocked.write_text("#!/bin/sh\nexit 0\n")
    blocked.chmod(0o644)
    assert guard.main(["--no-memory-cap", "--", str(blocked)]) == 126
    assert "permission denied" in capsys.readouterr().err


def test_propagates_child_exit_code(fake_systemd, monkeypatch):
    monkeypatch.setenv("PARALLEL_GUARD_CHILD_EXIT", "42")
    assert guard.main(["--", sys.executable, "-c", "print(1)"]) == 42

    monkeypatch.delenv("PARALLEL_GUARD_CHILD_EXIT")
    assert guard.main(["--no-memory-cap", "--", sys.executable, "-c", "import sys; sys.exit(43)"]) == 43


def test_no_shell_interpolation_in_direct_mode(tmp_path, monkeypatch, capfd):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(guard, "MEMINFO_PATH", write_meminfo(tmp_path, 8192))
    marker = tmp_path / "pwned"
    literal = f"$(touch {marker})"
    code = guard.main(["--no-memory-cap", "--", sys.executable, "-c",
                       "import sys; print(sys.argv[1])", literal])
    assert code == 0
    assert not marker.exists()
    assert capfd.readouterr().out.strip() == literal


def test_no_command_is_refused(fake_systemd, capsys):
    assert guard.main([]) == 2
    assert "no command given after --" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5"])
def test_invalid_memory_values_are_rejected(fake_systemd, value):
    with pytest.raises(SystemExit) as excinfo:
        guard.main(["--memory-max", value, "--", "true"])
    assert excinfo.value.code == 2
