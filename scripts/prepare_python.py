#!/usr/bin/env python3
"""Explicit controlled dev environment preparation; offline unless --connected is set."""
from __future__ import annotations

import argparse
import importlib
import importlib.machinery
import importlib.metadata
import json
import os
import site
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

try:
    from scripts import dependency_lock as locks
except ModuleNotFoundError:
    import dependency_lock as _local_locks

    locks = _local_locks

# Dev workspace image layout (images/Containerfile.devkit): the two lock files its
# installed, verified dev environment was built from. The image holds no wheelhouse,
# so the dev wheels ship only once, as that environment (#1118, D-7).
KIT_ROOT = Path("/opt/devkit")
KIT_LOCK_FILES = ("requirements.dev.lock.txt", "locks/cp314-linux-x86_64.json")
# The kit venv's one layering file: it adds the image environment's site directories.
# site processes .pth files in sorted order, so this name sorts before the checkout's
# editable .pth (`__editable__.*`): locked distributions precede the checkout's src/.
KIT_LAYER = "00-mfr-devkit-layer.pth"
# Run in the kit venv: every importable top-level name and the distributions providing it.
TOP_LEVEL_NAMES = "import importlib.metadata as m, json; print(json.dumps(m.packages_distributions()))"

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


def _kit_name(root: Path) -> str:
    """The kit this checkout needs. Imported only for this message, so ordinary
    preparation never depends on scripts/devkit_key.py."""
    fallback = "the kit `python3 scripts/devkit_key.py` names for this checkout"
    try:
        kit_key = importlib.import_module("scripts.devkit_key" if locks.__name__.startswith("scripts.")
                                          else "devkit_key")
    except ImportError:
        return fallback
    try:
        return f"mfr-devkit-{kit_key.key(root)}"
    except (kit_key.DevkitKeyError, OSError):
        return fallback


def check_kit(root: Path, kit: Path) -> None:
    """Refuse a dev kit image whose installed environment was built for another dev lock.

    A checkout whose dev lock differs needs the kit built for it, named by its key.
    """
    for name in KIT_LOCK_FILES:
        shipped, wanted = kit / "locks" / name, root / name
        if (shipped.is_symlink() or not shipped.is_file() or not wanted.is_file()
                or shipped.read_bytes() != wanted.read_bytes()):
            raise locks.LockError(f"this dev kit image was built for another dev lock; request {_kit_name(root)}")


def image_environment() -> tuple[list[Path], Path]:
    """The installed environment of the interpreter running --kit, the image's python3:
    its site directories and its scripts directory.

    That interpreter is itself a venv (/opt/app-root), so a venv created with
    --system-site-packages would see the base interpreter's empty site-packages
    instead; the kit venv layers these directories explicitly.
    """
    return ([Path(path) for path in site.getsitepackages() if Path(path).is_dir()],
            Path(sysconfig.get_path("scripts")))


def prepare_kit(root: Path, kit: Path, venv: Path) -> None:
    """A venv layered over the dev kit image's environment, with only the checkout added.

    Nothing is installed from wheels: the image's environment already equals the dev
    lock. The result must equal that lock plus the editable checkout exactly.
    """
    locks.check_target()
    locks.load(root, "dev")
    check_kit(root, kit)
    if (Path(sysconfig.get_path("purelib")) / KIT_LAYER).exists():
        raise locks.LockError("--kit is running inside a kit venv; run it with the image's python3")
    if venv.is_symlink():
        raise locks.LockError("refusing to prepare a symlink environment")
    layers, scripts = image_environment()
    if not layers or any("\n" in str(path) or "\r" in str(path) for path in layers):
        raise locks.LockError("the dev kit image's installed environment is unavailable")
    venv = venv.absolute()
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    # site.addsitedir, not a bare path line, so the image's own .pth files still apply.
    purelib = Path(sysconfig.get_path("purelib", scheme="venv", vars={"base": str(venv), "platbase": str(venv)}))
    (purelib / KIT_LAYER).write_text(
        "".join(f"import site; site.addsitedir({str(path)!r})\n" for path in layers), encoding="utf-8")
    # A native tool's module finds its binary beside the interpreter (`python -m ruff`), so
    # link the image's native executables; its Python entry points run the image's python.
    for tool in sorted(scripts.iterdir()):
        link = venv / "bin" / tool.name
        if tool.is_symlink() or not tool.is_file() or os.path.lexists(link):
            continue
        with tool.open("rb") as handle:
            if handle.read(4) == b"\x7fELF":
                link.symlink_to(tool)
    python = venv / "bin/python"
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    command = [str(python), "-I", "-m", "pip", "--isolated"]
    subprocess.run(command + ["install", "--no-index", "--no-cache-dir", "--no-deps",
                              "--no-build-isolation", "-e", str(root)], env=env, check=True)
    subprocess.run(command + ["check"], env=env, check=True)
    subprocess.run([str(python), "-I", str(root / "scripts/dependency_lock.py"),
                    "--root", str(root), "installed", "--profile", "dev", "--project"],
                   env=env, check=True)
    provided = json.loads(subprocess.run([str(python), "-I", "-c", TOP_LEVEL_NAMES], env=env, check=True,
                                         capture_output=True, text=True).stdout)
    project = locks.canonical(tomllib.loads((root / "pyproject.toml").read_text())["project"]["name"])
    for name in checkout_top_level(root):
        if any(locks.canonical(owner) != project for owner in provided.get(name, ())):
            raise locks.LockError(f"the checkout's src/{name} shadows a locked distribution's "
                                  f"top-level module {name}; rename it")


def checkout_top_level(root: Path) -> list[str]:
    """Top-level module and package names the checkout's editable src/ path provides."""
    names = set()
    source = root / "src"
    for entry in source.iterdir() if source.is_dir() else ():
        if entry.name == "__pycache__":
            continue
        if entry.is_dir() and entry.name.isidentifier():
            names.add(entry.name)
        elif entry.is_file():
            for suffix in (*importlib.machinery.SOURCE_SUFFIXES, *importlib.machinery.EXTENSION_SUFFIXES):
                if entry.name.endswith(suffix) and entry.name[:-len(suffix)].isidentifier():
                    names.add(entry.name[:-len(suffix)])
    return sorted(names)


def prepare(root: Path, wheelhouse: Path, python: Path | None, venv: Path | None,
            connected: bool, *, download_workers: int = 1) -> None:
    locks.check_target()
    locks.download_workers(download_workers)
    if not connected and download_workers != 1:
        raise locks.LockError("parallel acquisition requires explicit connected preparation")
    _, packages = locks.load(root, "dev")
    if connected:
        if download_workers == 1:
            locks.acquire(root, "dev", wheelhouse)
        else:
            locks.acquire(root, "dev", wheelhouse, workers=download_workers)
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
    wheels = parser.add_mutually_exclusive_group(required=True)
    wheels.add_argument("--wheelhouse", type=Path)
    wheels.add_argument("--kit", type=Path, nargs="?", const=KIT_ROOT, metavar="DIR",
                        help=f"offline: a --venv over the dev kit image's installed environment "
                             f"(default {KIT_ROOT})")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--python", type=Path, help="explicit prepared CI interpreter")
    target.add_argument("--venv", type=Path, help="local environment directory owned by this setup")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--connected", action="store_true", help="explicit acquisition from pinned public origins")
    source.add_argument("--internal-index", action="store_true", help="explicit acquisition using only PIP_INDEX_URL")
    parser.add_argument("--download-workers", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        locks.download_workers(args.download_workers)
        if not args.connected and args.download_workers != 1:
            raise locks.LockError("parallel acquisition requires explicit connected preparation")
        if args.kit is not None:
            if args.connected or args.internal_index:
                raise locks.LockError("--kit is offline only: the kit image's environment is already installed")
            if args.venv is None:
                raise locks.LockError("--kit prepares its own environment: use --venv")
            prepare_kit(args.root, args.kit, args.venv)
            return 0
        wheelhouse = args.wheelhouse
        if args.internal_index:
            acquire_internal(args.root, wheelhouse)
        prepare(args.root, wheelhouse, args.python, args.venv, args.connected,
                download_workers=args.download_workers)
        return 0
    except locks.LockError as exc:
        print(f"prepare-python: {exc}", file=sys.stderr)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("prepare-python: preparation failed; environment is not qualified", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
