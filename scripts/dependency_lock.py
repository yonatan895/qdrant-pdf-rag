#!/usr/bin/env python3
"""Controlled CPython 3.14 wheel profiles and offline inventory verification (#371).

Generation consumes pip reports as data; it never resolves, installs or downloads.
Verification uses only the standard library and never repairs an environment.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import sys
import sysconfig
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "locks/cp314-linux-x86_64.json"
PROFILE_FILES = {
    "runtime": "requirements.lock.txt",
    "dev": "requirements.dev.lock.txt",
    "build": "requirements.build.lock.txt",
}
PIP_VERSION = "26.2.1"
BASE_IMAGE = (
    "registry.access.redhat.com/ubi9/python-314-minimal@sha256:"
    "1e4b43488508216fee35b87e6b5425f4ef475eacf9c113f5b901e27af3844fba"
)
HASH = re.compile(r"[a-f0-9]{64}")
NAME = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*")


class LockError(ValueError):
    """Fixed, non-sensitive diagnostics at the CLI boundary."""


def canonical(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise LockError("invalid distribution name")
    return re.sub(r"[-_.]+", "-", name).lower()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    if path.stat().st_size > 8_000_000:
        raise LockError("input exceeds the bounded metadata size")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LockError("metadata must be an object")
    return value


def compatible_wheel(filename: str) -> bool:
    """Only wheels for the supported GIL CPython/glibc target, never generic Linux."""
    if Path(filename).name != filename or not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) not in (5, 6):
        return False
    python_tag, abi, target = parts[-3:]
    if abi == "none" and "py3" in python_tag.split(".") and target == "any":
        return True
    if not ((abi == "none" and "py3" in python_tag.split(".")) or
            (python_tag == "cp314" and abi == "cp314") or
            (re.fullmatch(r"cp3\d+", python_tag) and abi == "abi3"
             and int(python_tag[3:]) <= 14)):
        return False
    for tag in target.split("."):
        if tag in ("manylinux1_x86_64", "manylinux2010_x86_64", "manylinux2014_x86_64"):
            return True
        match = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", tag)
        if match and tuple(map(int, match.groups())) <= (2, 34):
            return True
    return False


def generate(root: Path, reports: Path) -> None:
    packages: dict[str, dict] = {}
    profiles: dict[str, dict] = {}
    for profile, filename in PROFILE_FILES.items():
        report = read_json(reports / f"{profile}.json")
        env = report.get("environment", {})
        if (report.get("version") != "1" or report.get("pip_version") != PIP_VERSION
                or env.get("python_version") != "3.14"
                or env.get("platform_python_implementation") != "CPython"
                or env.get("sys_platform") != "linux" or env.get("platform_machine") != "x86_64"):
            raise LockError("resolver report is not from the qualified pip/target")
        selected: list[str] = []
        for item in report["install"]:
            name = canonical(item["metadata"]["name"])
            version = item["metadata"]["version"]
            wheel = unquote(urlsplit(item["download_info"]["url"]).path.rsplit("/", 1)[-1])
            sha = item["download_info"]["archive_info"]["hashes"]["sha256"]
            if not VERSION.fullmatch(version) or not HASH.fullmatch(sha) or not compatible_wheel(wheel):
                raise LockError("report contains an unsupported version, hash or wheel")
            url = item["download_info"]["url"]
            origin = urlsplit(url)
            if (origin.scheme != "https" or origin.hostname != "files.pythonhosted.org"
                    or origin.username or origin.password or origin.query or origin.fragment):
                raise LockError("generation requires public hash-bound PyPI wheel origins")
            entry = {"version": version, "wheel": wheel, "sha256": sha, "url": url}
            if name in selected or (name in packages and packages[name] != entry):
                raise LockError("duplicate or inconsistent distribution across profiles")
            packages[name] = entry
            selected.append(name)
        if not selected:
            raise LockError("empty dependency profile")
        selected.sort()
        profiles[profile] = {"requirements": filename, "packages": selected}
    if not (set(profiles["runtime"]["packages"]) | set(profiles["build"]["packages"])) <= set(profiles["dev"]["packages"]):
        raise LockError("dev must contain the identical runtime and build closures")
    for profile, info in profiles.items():
        text = "# CPython 3.14 GIL / Linux x86_64 / glibc >= 2.34.\n"
        text += f"# Complete {profile} wheel profile; pip {PIP_VERSION}. See locks/cp314-linux-x86_64.json.\n"
        text += "# Generated from reviewed pip reports; refresh only in a dedicated dependency concern.\n"
        for name in info["packages"]:
            entry = packages[name]
            text += f"{name}=={entry['version']} --hash=sha256:{entry['sha256']}\n"
        (root / info["requirements"]).write_text(text, encoding="utf-8")
        info["sha256"] = digest(root / info["requirements"])
    manifest = {
        "schema_version": 1,
        "target": "cp314-gil-linux-x86_64-glibc2.34",
        "resolver": {"name": "pip", "version": PIP_VERSION},
        "base_image": {"reference": BASE_IMAGE, "inherited_python_packages": {"pip": "24.2"}},
        "packages": dict(sorted(packages.items())), "profiles": profiles,
    }
    destination = root / MANIFEST
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load(root: Path, profile: str) -> tuple[dict, dict[str, dict]]:
    manifest = read_json(root / MANIFEST)
    if (manifest.get("schema_version") != 1
            or manifest.get("target") != "cp314-gil-linux-x86_64-glibc2.34"
            or manifest.get("resolver") != {"name": "pip", "version": PIP_VERSION}):
        raise LockError("unsupported lock schema, target or resolver")
    info = manifest["profiles"][profile]
    if info["requirements"] != PROFILE_FILES[profile] or not HASH.fullmatch(info["sha256"]):
        raise LockError("invalid profile requirement identity")
    path = root / PROFILE_FILES[profile]
    if digest(path) != info["sha256"]:
        raise LockError("requirements and lock manifest disagree")
    selected = info["packages"]
    if not selected or len(set(selected)) != len(selected):
        raise LockError("empty or duplicate profile membership")
    expected = {name: manifest["packages"][name] for name in selected}
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    required = []
    for name, entry in sorted(expected.items()):
        if (canonical(name) != name or not VERSION.fullmatch(entry["version"])
                or not HASH.fullmatch(entry["sha256"]) or not compatible_wheel(entry["wheel"])):
            raise LockError("invalid locked distribution")
        required.append(f"{name}=={entry['version']} --hash=sha256:{entry['sha256']}")
    if lines != required:
        raise LockError("requirements do not exactly cover the profile")
    return manifest, expected


def verify_wheelhouse(root: Path, profile: str, directory: Path) -> dict:
    _, packages = load(root, profile)
    expected = {entry["wheel"]: entry["sha256"] for entry in packages.values()}
    actual = {path.name for path in directory.glob("*.whl")}
    if actual != set(expected):
        raise LockError("wheelhouse has missing or unselected distributions")
    for name, sha in expected.items():
        path = directory / name
        if path.is_symlink() or digest(path) != sha:
            raise LockError("wheelhouse member checksum mismatch or symlink")
    return {"schema_version": 1, "profile": profile, "lock_sha256": digest(root / MANIFEST),
            "wheels": dict(sorted(expected.items()))}


def acquire(root: Path, profile: str, directory: Path) -> dict:
    """Explicit connected acquisition of approved wheels; never called by verification."""
    check_target()
    _, packages = load(root, profile)
    expected = {entry["wheel"] for entry in packages.values()}
    if {path.name for path in directory.glob("*.whl")} - expected:
        raise LockError("refusing to modify a wheelhouse containing unselected wheels")
    directory.mkdir(parents=True, exist_ok=True)
    for entry in packages.values():
        destination = directory / entry["wheel"]
        if destination.is_symlink():
            raise LockError("refusing a symlink wheel destination")
        if destination.is_file() and digest(destination) == entry["sha256"]:
            continue
        origin = urlsplit(entry["url"])
        if (origin.scheme != "https" or origin.hostname != "files.pythonhosted.org"
                or origin.username or origin.password or origin.query or origin.fragment):
            raise LockError("unapproved connected acquisition origin")
        fd, name = tempfile.mkstemp(prefix=".wheel-", dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output, urllib.request.urlopen(entry["url"], timeout=60) as source:
                shutil.copyfileobj(source, output)
            if digest(temporary) != entry["sha256"]:
                raise LockError("downloaded wheel checksum mismatch")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return verify_wheelhouse(root, profile, directory)


def check_target() -> dict:
    libc, version = platform.libc_ver()
    gil_disabled = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    jit = bool(getattr(sys, "_jit", None) and sys._jit.is_enabled())
    if (platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 14)
            or gil_disabled or jit or platform.system() != "Linux" or platform.machine() != "x86_64"
            or libc != "glibc" or tuple(map(int, version.split("."))) < (2, 34)):
        raise LockError("unsupported interpreter, architecture or libc target")
    return {"implementation": "CPython", "version": list(sys.version_info[:3]),
            "gil_disabled": gil_disabled, "jit_enabled": jit, "libc": [libc, version]}


def verify_installed(root: Path, profile: str, project: bool = False, image: bool = False) -> dict:
    runtime = check_target()
    manifest, packages = load(root, profile)
    expected = {name: entry["version"] for name, entry in packages.items()}
    if project:
        metadata = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        project_name = canonical(metadata["name"])
        expected[project_name] = metadata["version"]
        installed_project = importlib.metadata.distribution(project_name)
        direct = json.loads(installed_project.read_text("direct_url.json") or "{}")
        source = urlsplit(direct.get("url", ""))
        if (source.scheme != "file" or source.netloc not in ("", "localhost")
                or Path(unquote(source.path)).resolve() != root.resolve()
                or direct.get("dir_info", {}).get("editable") is not True):
            raise LockError("editable project belongs to a different source tree")
    if image:
        if profile != "runtime" or manifest["base_image"] != {
            "reference": BASE_IMAGE, "inherited_python_packages": {"pip": "24.2"},
        }:
            raise LockError("unrecognized runtime base profile")
        expected.update(manifest["base_image"]["inherited_python_packages"])
    actual: dict[str, str] = {}
    locations: dict[str, Path] = {}
    for distribution in importlib.metadata.distributions():
        name = canonical(distribution.metadata["Name"])
        location = Path(str(distribution.locate_file(""))).resolve()
        if name in actual and (actual[name] != distribution.version or locations[name] != location):
            raise LockError("conflicting installed distribution versions")
        actual[name] = distribution.version
        locations[name] = location
    if actual != expected:
        raise LockError("installed inventory differs from the selected lock/base/project profile")
    return {"schema_version": 1, "profile": profile, "lock_sha256": digest(root / MANIFEST),
            "requirements_sha256": digest(root / PROFILE_FILES[profile]),
            "runtime": runtime, "project_installed": project, "base_image": BASE_IMAGE if image else None,
            "packages": dict(sorted(actual.items()))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("target")
    generate_parser = sub.add_parser("generate")
    generate_parser.add_argument("--reports", type=Path, required=True)
    for command in ("validate", "wheelhouse", "acquire", "installed"):
        child = sub.add_parser(command)
        child.add_argument("--profile", choices=PROFILE_FILES, required=True)
        if command in ("wheelhouse", "acquire"):
            child.add_argument("--directory", type=Path, required=True)
        if command == "installed":
            child.add_argument("--project", action="store_true")
            child.add_argument("--image", action="store_true")
        child.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "target":
            print(json.dumps(check_target(), sort_keys=True))
            return 0
        if args.command == "generate":
            generate(args.root, args.reports)
            return 0
        if args.command == "validate":
            _, packages = load(args.root, args.profile)
            result = {"profile": args.profile, "packages": len(packages),
                      "lock_sha256": digest(args.root / MANIFEST)}
        elif args.command == "acquire":
            result = acquire(args.root, args.profile, args.directory)
        elif args.command == "wheelhouse":
            result = verify_wheelhouse(args.root, args.profile, args.directory)
        else:
            result = verify_installed(args.root, args.profile, args.project, args.image)
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        else:
            print(encoded, end="")
        return 0
    except LockError as exc:
        print(f"dependency-lock: {exc}", file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError):
        print("dependency-lock: unreadable or invalid lock/input metadata", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
