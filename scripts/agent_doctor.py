#!/usr/bin/env python3
"""Read-only prerequisite diagnosis; readiness is not application or test acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

try:
    from scripts.qdrant_pin import qdrant_image_pin
except ModuleNotFoundError:  # direct `python scripts/agent_doctor.py` entry
    from qdrant_pin import qdrant_image_pin as _local_qdrant_image_pin

    qdrant_image_pin = _local_qdrant_image_pin

try:
    from scripts.dependency_lock import load as load_dependency_lock
except ModuleNotFoundError:
    from dependency_lock import load as _local_load_dependency_lock

    load_dependency_lock = _local_load_dependency_lock

PROBE_TIMEOUT_S = 5
# Executes only stdlib/metadata reads, never imports product packages or loads .env.
RUNTIME_PROBE = '''
import importlib.metadata as m, json, platform, sys, sysconfig
names = json.loads(sys.argv[1])
versions = {}
for name in names:
    try: versions[name] = m.version(name)
    except m.PackageNotFoundError: versions[name] = None
print(json.dumps(dict(implementation=platform.python_implementation(),
    version=list(sys.version_info[:3]), gil_disabled=bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    jit_enabled=bool(getattr(sys, "_jit", None) and sys._jit.is_enabled()), packages=versions)))
'''

# Every non-ready finding names the command that fixes it, run from the
# checkout root, for the connected host and the air gap where they differ.
# Fixes are fixed text (or tracked-pin fields of a validated shape): never
# environment values, private configuration or subprocess output.
TASK_FIX = ("connected fix: sh scripts/tools/install-task.sh; air-gap fix: sh scripts/tools/install-task.sh"
            " --archive /absolute/path/to/bundle/task_linux_amd64.tar.gz (the verified bundle's copy)")
VENV_CREATE = ("connected fix: sh scripts/tools/run-task.sh dev:setup PY=python3.14; air-gap fix: python3.14"
               " scripts/prepare_python.py --venv .venv --wheelhouse /absolute/path/to/approved/dev-wheels")
VENV_RECREATE = ("connected fix: rm -rf .venv && sh scripts/tools/run-task.sh dev:setup PY=python3.14;"
                 " air-gap fix: rm -rf .venv && python3.14 scripts/prepare_python.py --venv .venv"
                 " --wheelhouse /absolute/path/to/approved/dev-wheels")
PREPARED_PYTHON_FIX = ("connected fix: INTERPRETER scripts/prepare_python.py --python INTERPRETER --wheelhouse"
                       " .tools/python-wheels/dev --connected; air-gap fix: INTERPRETER scripts/prepare_python.py"
                       " --python INTERPRETER --wheelhouse /absolute/path/to/approved/dev-wheels"
                       " (INTERPRETER is the --python value given to this check)")
DOCKER_FIX = "fix: start the local Docker daemon, then check: docker --host unix:///var/run/docker.sock version"
CHECKER_FIX = "fix: env -u PYTHON_JIT python3.14 scripts/agent_doctor.py (a CPython 3.14 GIL build, not python3.14t)"
TRACKED_PINS = "pyproject.toml images.txt requirements.dev.lock.txt locks/cp314-linux-x86_64.json"


def helm_fix(version: str) -> str:
    asset = (f"helm-{version}-linux-amd64.tar.gz" if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version)
             else "helm-VERSION-linux-amd64.tar.gz")
    return ("connected fix: sh scripts/tools/install-helm.sh; air-gap fix: sh scripts/tools/install-helm.sh"
            f" --archive /absolute/path/to/{asset} (an approved transferred copy);"
            ' then select it: export PATH="$PWD/.tools/bin:$PATH"')


def tool_fix(name: str) -> str:
    return f"fix: no repository installer; install {name} with the host package manager, then check: command -v {name}"


@dataclass(frozen=True)
class Finding:
    status: str
    subject: str
    detail: str


def inspect_task(root: Path) -> Finding:
    """Verify the exact workspace executable without executing a PATH program."""
    local = root / ".tools/bin/task"
    try:
        pin = dict(line.split(": ", 1) for line in
                   (root / "scripts/tools/task-pin.txt").read_text(encoding="utf-8").splitlines()
                   if line and not line.startswith("#") and ": " in line)
        version, expected = pin["version"], pin["binary-sha256"]
        asset, archive_expected = pin["asset"], pin["sha256"]
        if (asset != "task_linux_amd64.tar.gz"
                or not re.fullmatch(r"[a-f0-9]{64}", archive_expected)
                or not re.fullmatch(r"[a-f0-9]{64}", expected)):
            raise ValueError("invalid pin")
    except (OSError, UnicodeError, KeyError, ValueError):
        return Finding("unable to verify", "Task runner", "Task pin record unreadable or incomplete;"
                       " fix: git checkout -- scripts/tools/task-pin.txt")
    if not local.is_file() or not os.access(local, os.X_OK):
        return Finding("missing prerequisite", "Task runner",
                       "pinned .tools/bin/task absent; " + TASK_FIX)
    try:
        with local.open("rb") as stream:
            observed = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return Finding("unable to verify", "Task runner", "Task executable unreadable; reinstall it: " + TASK_FIX)
    if observed != expected:
        return Finding("missing prerequisite", "Task runner",
                       "Task binary checksum mismatch; reinstall from the pinned archive: " + TASK_FIX)
    # Artifact tests package the actual transferred archive, not a stand-in.
    # A verified executable alone cannot establish that prerequisite.
    archive = root / ".tools/cache" / asset
    try:
        with archive.open("rb") as stream:
            archive_observed = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return Finding("missing prerequisite", "Task archive",
                       "cached pinned Task archive unavailable; the installer caches it: " + TASK_FIX)
    if archive_observed != archive_expected:
        return Finding("missing prerequisite", "Task archive",
                       "cached Task archive checksum mismatch; the installer replaces it: " + TASK_FIX)
    return Finding("ready", "Task runner",
                   f"pinned go-task {version} executable and archive checksums verified")


def inspect_helm(root: Path) -> Finding:
    """Hash the renderer selected by PATH; never execute an unverified tool."""
    try:
        pin = dict(line.split(": ", 1) for line in
                   (root / "scripts/tools/helm-pin.txt").read_text(encoding="utf-8").splitlines()
                   if line and not line.startswith("#") and ": " in line)
        expected, version = pin["binary-sha256"], pin["version"]
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("invalid pin")
    except (OSError, UnicodeError, KeyError, ValueError):
        return Finding("unable to verify", "Helm", "Helm pin unreadable or incomplete;"
                       " fix: git checkout -- scripts/tools/helm-pin.txt")
    executable = shutil.which("helm")
    if executable is None:
        return Finding("missing prerequisite", "Helm",
                       "pinned Helm absent; " + helm_fix(version))
    try:
        with Path(executable).open("rb") as stream:
            observed = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return Finding("unable to verify", "Helm", "selected Helm executable unreadable; " + helm_fix(version))
    if observed != expected:
        return Finding("missing prerequisite", "Helm",
                       "selected Helm checksum differs from the pin; " + helm_fix(version))
    return Finding("ready", "Helm", f"pinned Helm {version} executable checksum verified")


def inspect_runtime(python: Path, packages: list[str]) -> dict | None:
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", RUNTIME_PROBE, json.dumps(packages)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False,
        )
        if result.returncode:
            return None
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            return None
        version = value.get("version")
        if not isinstance(version, list) or len(version) != 3 or not all(type(v) is int for v in version):
            return None
        if not isinstance(value.get("packages"), dict):
            return None
        if type(value.get("gil_disabled")) is not bool or type(value.get("jit_enabled")) is not bool:
            return None
        return value
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None  # Never echo subprocess output or exception text (may contain secrets).


def inspect_locked_environment(root: Path, python: Path) -> bool:
    """The target interpreter checks its complete inventory and editable source."""
    try:
        result = subprocess.run(
            [str(python), "-I", str(root / "scripts/dependency_lock.py"),
             "--root", str(root), "installed", "--profile", "dev", "--project"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def diagnose(root: Path, profile: str = "unit", probe_docker: bool = False,
             python: Path | None = None) -> list[Finding]:
    results: list[Finding] = []

    def add(status: str, subject: str, detail: str, fix: str = "") -> None:
        if status != "ready" and fix:
            detail = f"{detail}; {fix}"
        results.append(Finding(status, subject, detail))

    required = ["pyproject.toml", "requirements.lock.txt", "requirements.dev.lock.txt",
                "requirements.build.lock.txt", "locks/cp314-linux-x86_64.json",
                "images.txt", "bm25-weights.sha256"]
    if profile == "deploy":
        required += ["airgap.env.example", "scripts/airgap/common.sh",
                     "charts/mainframe-rag/Chart.yaml",
                     "charts/mainframe-rag/values.schema.json"]
    for name in required:
        add("ready" if (root/name).is_file() else "missing prerequisite", name, "tracked input presence",
            f"fix: git checkout -- {name}")
    try:
        project = tomllib.loads((root/"pyproject.toml").read_text(encoding="utf-8"))
        requirement = project["project"]["requires-python"]
        minimum = re.fullmatch(r">=(\d+)\.(\d+)", requirement)
        if not minimum:
            add("unable to verify", "Python requirement",
                "unsupported requirement syntax; requires-python must read >=MAJOR.MINOR",
                "fix: git checkout -- pyproject.toml")
            return results
        minimum_version = tuple(map(int, minimum.groups()))
        _, locked = load_dependency_lock(root, "dev")
        pins = {name: entry["version"] for name, entry in locked.items()}
        if not pins or "qdrant-client" not in pins:
            add("missing prerequisite", "requirements.lock.txt", "expected dependency pins absent",
                "fix: git checkout -- requirements.dev.lock.txt locks/cp314-linux-x86_64.json")
            return results
        image = qdrant_image_pin(root/"images.txt")
        expected = pins["qdrant-client"]
        # Server and client agree on major.minor (qdrant-version-upgrade skill); the
        # chart tracks the server patch version.
        tag = re.fullmatch(r".*:v(\d+\.\d+)\.(\d+)-unprivileged", image)
        matches = bool(tag) and tag.group(1) == ".".join(expected.split(".")[:2])
        add("ready" if matches else "missing prerequisite", "Qdrant pin", "server/client version agreement",
            "fix: git checkout -- images.txt requirements.dev.lock.txt locks/cp314-linux-x86_64.json")
        server = f"{tag.group(1)}.{tag.group(2)}" if tag else expected
        chart = root/"charts"/f"qdrant-{server}.tgz"
        add("ready" if chart.is_file() else "missing prerequisite", "Qdrant chart", "chart matching locked client",
            f"fix: git checkout -- charts/qdrant-{server}.tgz")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        add("unable to verify", "tracked configuration", "missing, unreadable or unsupported pin/config input",
            "fix: git checkout -- " + TRACKED_PINS)
        return results

    tools = ["git"]  # Unit/deploy contracts execute the real chart renderer.
    if profile == "sim":
        tools += ["docker"]
    if profile == "deploy":
        tools += ["skopeo", "tar", "openssl", "sha256sum"]
        cli = shutil.which("oc") or shutil.which("kubectl")
        add("ready" if cli else "missing prerequisite", "oc or kubectl", "CLI presence only; no cluster contact",
            tool_fix("oc"))
    for name in tools:
        add("ready" if shutil.which(name) else "missing prerequisite", name, "CLI presence only", tool_fix(name))

    results.append(inspect_task(root))
    results.append(inspect_helm(root))

    interpreters = [("checker runtime", Path(sys.executable))]
    development = python.absolute() if python is not None else root/".venv/bin/python"
    # Task targets pass --python .venv/bin/python: that is still the local venv.
    local_venv = development.parts[-3:] == (".venv", "bin", "python")
    if development.is_file():
        interpreters.append(("development environment", development))
    else:
        add("missing prerequisite", "development environment",
            "selected development interpreter absent; no environment created",
            VENV_CREATE if local_venv else PREPARED_PYTHON_FIX)
    for label, interpreter in interpreters:
        fix = (CHECKER_FIX if label == "checker runtime"
               else VENV_RECREATE if local_venv else PREPARED_PYTHON_FIX)
        packages = sorted(set(pins) | {"pytest", "ruff", "mypy"}) if label == "development environment" else []
        runtime = inspect_runtime(interpreter, packages)
        if runtime is None:
            add("unable to verify", label, "bounded interpreter/metadata probe unavailable", fix)
            continue
        compatible = (runtime.get("implementation") == "CPython"
                      and tuple(runtime["version"][:2]) == (3, 14)
                      and (3, 14) >= minimum_version
                      and not runtime["gil_disabled"] and not runtime["jit_enabled"])
        add("ready" if compatible else "missing prerequisite", label,
            "CPython 3.14 GIL with experimental JIT disabled required", fix)
        if label == "development environment":
            verified = inspect_locked_environment(root, interpreter)
            add("ready" if verified else "missing prerequisite", "complete development inventory",
                "full lock and editable source identity verified" if verified else
                "installed inventory or editable source differs from the selected lock/worktree", fix)
        for package in packages:
            installed = runtime["packages"].get(package)
            if not installed:
                add("missing prerequisite", package, "not installed in development environment", fix)
            elif package in pins and installed != pins[package]:
                add("missing prerequisite", package, "installed version differs from requirements.dev.lock.txt",
                    fix)
            else:
                add("ready", package, "development package present; locked version checked where specified")

    external_load = profile == "load" and bool(os.environ.get("QDRANT_SIM_URL"))
    if external_load:
        add("ready", "external load server", "operator-selected QDRANT_SIM_URL; image identity is not attested")
    if profile in ("sim", "load", "ha") and not external_load:
        try:
            try:
                from scripts.qdrant_pin import prepared_image, qdrant_digest_pin
            except ModuleNotFoundError:
                from qdrant_pin import prepared_image as _local_prepared_image
                from qdrant_pin import qdrant_digest_pin as _local_qdrant_digest_pin
                prepared_image = _local_prepared_image
                qdrant_digest_pin = _local_qdrant_digest_pin
            image_id = prepared_image(qdrant_digest_pin(root / "images.txt"))
            add("ready", "prepared Qdrant image", image_id)
        except (OSError, ValueError):
            add("missing prerequisite", "prepared Qdrant image",
                "approved digest unavailable in selected daemon",
                "connected fix: sh scripts/tools/run-task.sh artifacts:qdrant; air gap: no supported"
                " preparation (a docker load lacks the digest identity), run this profile on a connected host")
    if profile == "sim":
        try:
            try:
                from scripts.fetch_bm25_weights import prepared_bm25_cache
            except ModuleNotFoundError:
                from fetch_bm25_weights import prepared_bm25_cache as _local_prepared_bm25_cache
                prepared_bm25_cache = _local_prepared_bm25_cache
            prepared_bm25_cache(root)
            add("ready", "prepared BM25 cache", "selected snapshot and file hashes verified")
        except (OSError, ValueError, SystemExit):
            add("missing prerequisite", "prepared BM25 cache",
                "selected cache missing, ambiguous or corrupt",
                "connected fix: sh scripts/tools/run-task.sh artifacts:bm25 (writes the default"
                " bundles/bm25-weights; unset SIM_BM25_CACHE_DIR or point it there); air-gap fix:"
                " export SIM_BM25_CACHE_DIR=/absolute/path/to/offline-prepared/bm25-weights, verified by"
                ' python3 scripts/fetch_bm25_weights.py --out "$SIM_BM25_CACHE_DIR"'
                " --verify-manifest bm25-weights.sha256 --verify-only")

    if probe_docker:
        docker = shutil.which("docker")
        if not docker:
            add("missing prerequisite", "local Docker probe", "docker CLI absent", tool_fix("docker"))
        else:
            try:
                result = subprocess.run(
                    [docker, "--host", "unix:///var/run/docker.sock", "version", "--format", "{{.Server.Version}}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=PROBE_TIMEOUT_S, check=False,
                )
                add("ready" if result.returncode == 0 else "unable to verify", "local Docker probe",
                    "explicit local socket read only; remote contexts are not contacted", DOCKER_FIX)
            except (OSError, subprocess.TimeoutExpired):
                add("unable to verify", "local Docker probe", "local socket unavailable or bounded probe timed out",
                    DOCKER_FIX)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--profile", choices=("unit", "sim", "load", "ha", "deploy"), default="unit")
    parser.add_argument("--probe-docker", action="store_true", help="explicit five-second read of local /var/run/docker.sock")
    parser.add_argument("--python", type=Path, help="prepared development/CI interpreter (default .venv/bin/python)")
    args = parser.parse_args(argv)
    try:
        findings = diagnose(args.root, args.profile, args.probe_docker, args.python)
    except Exception:  # noqa: BLE001 — CLI boundary must redact unexpected failures
        print("internal checker failure: diagnosis unavailable", file=sys.stderr)
        return 1
    for finding in findings:
        print(f"{finding.status}: {finding.subject}: {finding.detail}")
    ready = all(f.status == "ready" for f in findings)
    if not ready:
        print("Run each fix from the checkout root, then rerun this check.")
    print("Prerequisites only; no application, test or production acceptance is established.")
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
