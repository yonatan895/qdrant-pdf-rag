#!/usr/bin/env python3
"""Host-headroom guard for heavy commands run by parallel agents.

The workflow lets agents fan out, but the host does not grow with them. This
wrapper makes the shared limit mechanical instead of hand-managed:

- Refuse to start when `/proc/meminfo` MemAvailable is below a floor
  (default 1024 MiB) so a new command cannot eat the last headroom.
- Run the command inside a transient `systemd-run --user --scope` capped at
  `MemoryMax` (default 3072 MiB) with swap disabled, so one runaway command
  cannot OOM the host. When `systemd-run --user` is unavailable the guard
  refuses unless `--no-memory-cap` is passed explicitly. `$` is never expanded
  by systemd (`--expand-environment=no`, or `$$` escaping on older systemd).
- Forward argv exactly, never through a shell, and exit with the child's code.

Usage:

    scripts/parallel_guard.py -- <command...>
    scripts/parallel_guard.py --memory-max 2048 --min-available 512 -- <command...>

Exit codes: the child's exit code; 2 when the guard refuses before running.
With `--no-memory-cap`, an unexecutable command yields 127 (not found) or 126
(permission denied); under `systemd-run`, systemd-run's own failure status
(typically 1) is returned instead.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

DEFAULT_MEMORY_MAX_MIB = 3072
DEFAULT_MIN_AVAILABLE_MIB = 1024
MEMINFO_PATH = Path("/proc/meminfo")
SYSTEMD_RUN = "systemd-run"


def available_mib(text: str) -> int:
    """Host MemAvailable in MiB from `/proc/meminfo` text; ValueError if absent."""
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key != "MemAvailable":
            continue
        fields = rest.split()
        if not fields or not fields[0].isdecimal():
            break
        return int(fields[0]) // 1024
    raise ValueError("MemAvailable is missing or malformed")


def mib(value: str) -> int:
    """Argparse type: a positive whole number of MiB."""
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a positive integer number of MiB")
    return int(value)


def _systemd_probe(*options: str) -> int:
    probe = subprocess.run(
        [SYSTEMD_RUN, "--user", "--scope", "-q", *options, "true"],
        capture_output=True,
        check=False,
    )
    return probe.returncode


def expansion_mode() -> str | None:
    """`systemd-run` argv-expansion strategy, or None when the scope is unusable.

    `no-expand` keeps `$` literal through `--expand-environment=no` (newer
    systemd). Older systemd falls back to `escape`, doubling each `$` so
    systemd turns it back into one literal `$`.
    """
    if shutil.which(SYSTEMD_RUN) is None:
        return None
    if _systemd_probe("--expand-environment=no") == 0:
        return "no-expand"
    if _systemd_probe() == 0:
        return "escape"
    return None


def refuse(message: str) -> int:
    print(f"parallel_guard: refusing to run: {message}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="parallel_guard.py",
        description="Refuse below a host memory floor and run a command under a memory cap.",
    )
    parser.add_argument("--memory-max", type=mib, default=DEFAULT_MEMORY_MAX_MIB,
                        metavar="MIB", help=f"scope MemoryMax (default {DEFAULT_MEMORY_MAX_MIB})")
    parser.add_argument("--min-available", type=mib, default=DEFAULT_MIN_AVAILABLE_MIB,
                        metavar="MIB",
                        help=f"required MemAvailable floor (default {DEFAULT_MIN_AVAILABLE_MIB})")
    parser.add_argument("--no-memory-cap", action="store_true",
                        help="run without systemd-run when it is unavailable")
    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND...",
                        help="command and arguments to run")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("parallel_guard: refusing to run: no command given after --", file=sys.stderr)
        return 2
    program = command[0]

    try:
        meminfo = MEMINFO_PATH.read_text()
        available = available_mib(meminfo)
    except (OSError, ValueError):
        return refuse(f"cannot read MemAvailable from {MEMINFO_PATH}")

    if available < args.min_available:
        return refuse(
            f"MemAvailable {available} MiB is below the {args.min_available} MiB floor"
        )

    if not args.no_memory_cap:
        mode = expansion_mode()
        if mode is None:
            return refuse(
                "systemd-run --user is unavailable for the memory cap; pass --no-memory-cap"
            )
        options = ["--expand-environment=no"] if mode == "no-expand" else []
        if mode == "escape":
            command = [argument.replace("$", "$$") for argument in command]
        command = [
            SYSTEMD_RUN, "--user", "--scope", "-q",
            *options,
            "-p", f"MemoryMax={args.memory_max}M",
            "-p", "MemorySwapMax=0",
            "--", *command,
        ]

    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError:
        print(f"parallel_guard: cannot execute {program}: not found", file=sys.stderr)
        return 127
    except PermissionError:
        print(f"parallel_guard: cannot execute {program}: permission denied",
              file=sys.stderr)
        return 126
    if completed.returncode < 0:
        return 128 - completed.returncode
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
