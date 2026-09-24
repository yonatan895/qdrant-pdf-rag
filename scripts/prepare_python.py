#!/usr/bin/env python3
"""Explicit controlled dev environment preparation; offline unless --connected is set."""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    from scripts import dependency_lock as locks
except ModuleNotFoundError:
    import dependency_lock as _local_locks

    locks = _local_locks

# Run verified pip wheel code using the target interpreter. The wheelhouse
# must be completely verified before this command or any environment mutation.
PIP_RUNNER = (
    "import runpy,sys; wheel=sys.argv.pop(1); sys.path.insert(0,wheel); "
    "sys.argv[0]='pip'; runpy.run_module('pip',run_name='__main__')"
)


def acquire_internal(root: Path, wheelhouse: Path) -> None:
    """Explicit mirror-only preparation; pinned pip, no inherited extra indexes."""
    locks.check_target()
    locks.load(root, "dev")
    index = os.environ.get("PIP_INDEX_URL", "")
    parsed = urlsplit(index)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.hostname.lower().rstrip(".") in
            ("pypi.org", "www.pypi.org", "pypi.python.org", "files.pythonhosted.org")
            or parsed.query or parsed.fragment):
        raise locks.LockError("an explicit internal HTTPS package index is required")
    if importlib.metadata.version("pip") != locks.PIP_VERSION:
        raise locks.LockError("internal acquisition requires the pinned pip in the prepared CI image")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    # Captured diagnostics may include an authenticated mirror URL. Never
    # forward them to logs; failed acquisition is reported by the fixed CLI error.
    subprocess.run([sys.executable, "-I", "-m", "pip", "--isolated", "download",
                    "--no-cache-dir", "--only-binary=:all:", "--require-hashes",
                    "--index-url", index, "--dest", str(wheelhouse),
                    "-r", str(root / "requirements.dev.lock.txt")],
                   env=env, check=True, capture_output=True)
    locks.verify_wheelhouse(root, "dev", wheelhouse)


def prepare(root: Path, wheelhouse: Path, python: Path | None, venv: Path | None,
            connected: bool) -> None:
    locks.check_target()
    _, packages = locks.load(root, "dev")
    if connected:
        locks.acquire(root, "dev", wheelhouse)
    else:
        locks.verify_wheelhouse(root, "dev", wheelhouse)
    # Reject accidental shared-environment mutation. Prepared CI uses --python
    # explicitly; local setup owns a real venv directory, never an alias.
    if venv is not None:
        if venv.is_symlink():
            raise locks.LockError("refusing to prepare a symlink environment")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        python = venv.absolute() / "bin/python"
    if python is None or not python.is_file():
        raise locks.LockError("selected interpreter is unavailable")
    python = python.absolute()  # resolving a venv symlink loses its prefix
    subprocess.run([str(python), "-I", str(root / "scripts/dependency_lock.py"), "target"], check=True)
    wheel = wheelhouse.absolute() / packages["pip"]["wheel"]
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    command = [str(python), "-I", "-c", PIP_RUNNER, str(wheel), "--isolated"]
    subprocess.run(command + ["install", "--no-index", "--no-cache-dir", "--only-binary=:all:",
                              "--require-hashes", "--find-links", str(wheelhouse.absolute()),
                              "-r", str(root / "requirements.dev.lock.txt")], env=env, check=True)
    subprocess.run(command + ["install", "--no-index", "--no-cache-dir", "--no-deps",
                              "--no-build-isolation", "-e", str(root)], env=env, check=True)
    subprocess.run(command + ["check"], env=env, check=True)
    subprocess.run([str(python), "-I", str(root / "scripts/dependency_lock.py"),
                    "--root", str(root), "installed", "--profile", "dev", "--project"],
                   env=env, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=locks.ROOT)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--python", type=Path, help="explicit prepared CI interpreter")
    target.add_argument("--venv", type=Path, help="local environment directory owned by this setup")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--connected", action="store_true", help="explicit acquisition from pinned public origins")
    source.add_argument("--internal-index", action="store_true", help="explicit acquisition using only PIP_INDEX_URL")
    args = parser.parse_args(argv)
    try:
        if args.internal_index:
            acquire_internal(args.root, args.wheelhouse)
        prepare(args.root, args.wheelhouse, args.python, args.venv, args.connected)
        return 0
    except locks.LockError as exc:
        print(f"prepare-python: {exc}", file=sys.stderr)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("prepare-python: preparation failed; environment is not qualified", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
