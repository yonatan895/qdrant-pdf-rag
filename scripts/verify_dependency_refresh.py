#!/usr/bin/env python3
"""Offline candidate-lock verification for a Python dependency refresh (retro 3).

An author's ambient environment can hide a missing or stale fixture: the
fresh lock must also install from verified wheel bytes into a clean temporary
environment, pass ``pip check`` and run the focused dependency/BM25/embedding
suites there. This entry never resolves, downloads, edits a lock or installs
into the repository ``.venv``; it is an on-demand maintenance check, not an
every-lane CI job.

Only the complete dev profile contains the test runner (and is a strict
superset of runtime/build at the same pins), so the focused suites require it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from scripts import dependency_lock as locks
    from scripts.prepare_python import PIP_RUNNER
except ModuleNotFoundError:
    import dependency_lock as _local_locks
    from prepare_python import PIP_RUNNER as _local_pip_runner

    locks = _local_locks
    PIP_RUNNER = _local_pip_runner

ROOT = locks.ROOT
FOCUSED_TESTS = (
    "tests/test_dependency_lock.py",
    "tests/test_license_inventory.py",
    "tests/test_bm25_weights.py",
    "tests/test_embed_budget.py",
    "tests/test_hash_embed.py",
)
TARGET_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 900
CHECK_TIMEOUT_S = 1800


class RefreshError(ValueError):
    """Fixed, non-sensitive diagnostics at the CLI boundary."""


def _environment(root: Path, venv: Path | None = None) -> dict[str, str]:
    """Inherited settings that could redirect pip/pytest are removed, not overridden."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["HF_HUB_OFFLINE"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "src")))
    if venv is not None:
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = os.pathsep.join((str(venv / "bin"), env.get("PATH", "")))
    return env


def _invoke(command: list[str], *, root: Path, env: dict[str, str], timeout: int,
            failure: str, runner, quiet: bool = False) -> None:
    kwargs: dict = {"cwd": root, "env": env, "timeout": timeout, "check": True}
    if quiet:
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        runner(command, **kwargs)
    except subprocess.TimeoutExpired:
        raise RefreshError("verification step exceeded its time limit") from None
    except (OSError, subprocess.CalledProcessError):
        raise RefreshError(failure) from None


def _validate_inputs(root: Path, python: Path, wheelhouse: Path) -> None:
    if not python.is_file():
        raise RefreshError("selected interpreter is unavailable")
    if not wheelhouse.is_dir():
        raise RefreshError("wheelhouse directory is unavailable")
    environment = (root / ".venv").resolve()
    candidate = wheelhouse.resolve()
    if candidate == root.resolve() or candidate == environment or environment in candidate.parents:
        raise RefreshError("refusing the repository environment as a wheelhouse")


def _refuse_environment_destination(venv: Path, root: Path) -> None:
    environment = (root / ".venv").resolve()
    candidate = venv.resolve()
    if candidate == environment or environment in candidate.parents:
        raise RefreshError(
            "refusing to create the temporary environment inside the repository environment")


def verify(root: Path, profile: str, wheelhouse: Path, python: Path, *,
           runner=None) -> dict:
    """Prove one lock profile installs offline into a clean, disposable environment."""
    runner = runner or subprocess.run
    root = Path(root).absolute()
    wheelhouse = Path(wheelhouse)
    python = Path(python)
    _validate_inputs(root, python, wheelhouse)
    try:
        _, packages = locks.load(root, profile)
    except locks.LockError as exc:
        raise RefreshError(str(exc)) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise RefreshError("unreadable or invalid lock metadata") from None
    if "pytest" not in packages:
        raise RefreshError("focused verification requires the complete dev profile")
    try:
        locks.verify_wheelhouse(root, profile, wheelhouse)
    except locks.LockError:
        raise RefreshError("wheelhouse is incomplete, unselected or tampered") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise RefreshError("wheelhouse is incomplete, unselected or tampered") from None
    if any(not (root / relative).is_file() for relative in FOCUSED_TESTS):
        raise RefreshError("focused suite is missing from this checkout")

    python = python.absolute()
    wheelhouse = wheelhouse.absolute()
    requirements = root / locks.PROFILE_FILES[profile]
    base_env = _environment(root)
    target_script = Path(__file__).resolve().with_name("dependency_lock.py")
    _invoke([str(python), "-I", str(target_script), "--root", str(root), "target"],
            root=root, env=base_env, timeout=TARGET_TIMEOUT_S,
            failure="selected interpreter is not a qualified CPython 3.14 target",
            runner=runner, quiet=True)

    with tempfile.TemporaryDirectory(prefix="dependency-refresh-") as temporary:
        venv = Path(temporary) / "venv"
        _refuse_environment_destination(venv, root)
        _invoke([str(python), "-I", "-m", "venv", "--without-pip", str(venv)],
                root=root, env=base_env, timeout=TARGET_TIMEOUT_S,
                failure="temporary environment creation failed", runner=runner)
        venv_python = venv / "bin/python"
        if not venv_python.is_file():
            raise RefreshError("temporary environment creation failed")
        env = _environment(root, venv)
        pip = [str(venv_python), "-I", "-c", PIP_RUNNER,
               str(wheelhouse / packages["pip"]["wheel"]), "--isolated"]
        _invoke(pip + ["install", "--no-index", "--no-cache-dir", "--only-binary=:all:",
                       "--require-hashes", "--find-links", str(wheelhouse),
                       "-r", str(requirements)],
                root=root, env=env, timeout=INSTALL_TIMEOUT_S,
                failure="offline installation failed", runner=runner)
        _invoke(pip + ["check"], root=root, env=env, timeout=CHECK_TIMEOUT_S,
                failure="pip check failed", runner=runner)
        _invoke([str(venv_python), "-m", "pytest", *FOCUSED_TESTS, "-q",
                 "-p", "no:cacheprovider"],
                root=root, env=env, timeout=CHECK_TIMEOUT_S,
                failure="focused suites failed", runner=runner)

    return {
        "schema_version": 1,
        "profile": profile,
        "lock_sha256": locks.digest(root / locks.MANIFEST),
        "requirements_sha256": locks.digest(requirements),
        "wheelhouse": str(wheelhouse),
        "wheels": len(packages),
        "python": str(python),
        "checks": ["pip check", *FOCUSED_TESTS],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--profile", choices=tuple(locks.PROFILE_FILES), default="dev")
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable),
                        help="prepared CPython 3.14 interpreter (default: this interpreter)")
    args = parser.parse_args(argv)
    try:
        receipt = verify(args.root, args.profile, args.wheelhouse, args.python)
    except RefreshError as exc:
        print(f"dependency-refresh: {exc}", file=sys.stderr)
    except locks.LockError as exc:
        print(f"dependency-refresh: {exc}", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print("dependency-refresh: unreadable or invalid lock/input metadata", file=sys.stderr)
    else:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
