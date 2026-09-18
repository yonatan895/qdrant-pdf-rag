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
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("invalid pin")
    except (OSError, UnicodeError, KeyError, ValueError):
        return Finding("unable to verify", "Task runner", "Task pin record unreadable or incomplete")
    if not local.is_file() or not os.access(local, os.X_OK):
        return Finding("missing prerequisite", "Task runner",
                       "pinned .tools/bin/task absent; use the connected installer or signed offline bootstrap")
    try:
        with local.open("rb") as stream:
            observed = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return Finding("unable to verify", "Task runner", "Task executable unreadable")
    if observed != expected:
        return Finding("missing prerequisite", "Task runner",
                       "Task binary checksum mismatch; reinstall from the pinned archive")
    return Finding("ready", "Task runner", f"pinned go-task {version} executable checksum verified")


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


def diagnose(root: Path, profile: str = "unit", probe_docker: bool = False) -> list[Finding]:
    results: list[Finding] = []

    def add(status: str, subject: str, detail: str) -> None:
        results.append(Finding(status, subject, detail))

    required = ["pyproject.toml", "requirements.lock.txt", "images.txt", "bm25-weights.sha256"]
    if profile == "deploy":
        required += ["airgap.env.example", "scripts/airgap/common.sh",
                     "deploy/kustomize/overlays/openshift/kustomization.yaml",
                     "deploy/kustomize/overlays/openshift-ingest/kustomization.yaml"]
    for name in required:
        add("ready" if (root/name).is_file() else "missing prerequisite", name, "tracked input presence")
    try:
        project = tomllib.loads((root/"pyproject.toml").read_text(encoding="utf-8"))
        requirement = project["project"]["requires-python"]
        minimum = re.fullmatch(r">=(\d+)\.(\d+)", requirement)
        if not minimum:
            add("unable to verify", "Python requirement", "unsupported requirement syntax; inspect pyproject.toml")
            return results
        minimum_version = tuple(map(int, minimum.groups()))
        lock = (root/"requirements.lock.txt").read_text(encoding="utf-8")
        pins = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9.+_-]+)\s*$", lock, re.MULTILINE))
        if not pins or "qdrant-client" not in pins:
            add("missing prerequisite", "requirements.lock.txt", "expected dependency pins absent")
            return results
        image = qdrant_image_pin(root/"images.txt")
        expected = pins["qdrant-client"]
        matches = image.endswith(f":v{expected}-unprivileged")
        add("ready" if matches else "missing prerequisite", "Qdrant pin", "server/client version agreement")
        chart = root/"charts"/f"qdrant-{expected}.tgz"
        add("ready" if chart.is_file() else "missing prerequisite", "Qdrant chart", "chart matching locked client")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        add("unable to verify", "tracked configuration", "missing, unreadable or unsupported pin/config input")
        return results

    tools = ["git"]
    if profile == "sim":
        tools += ["docker"]
    if profile == "deploy":
        tools += ["helm", "skopeo", "tar", "openssl", "sha256sum"]
        cli = shutil.which("oc") or shutil.which("kubectl")
        add("ready" if cli else "missing prerequisite", "oc or kubectl", "CLI presence only; no cluster contact")
    for name in tools:
        add("ready" if shutil.which(name) else "missing prerequisite", name, "CLI presence only")

    results.append(inspect_task(root))

    interpreters = [("checker runtime", Path(sys.executable))]
    development = root/".venv/bin/python"
    if development.is_file():
        interpreters.append(("development environment", development))
    else:
        add("missing prerequisite", "development environment", ".venv/bin/python absent; no environment created")
    for label, python in interpreters:
        packages = sorted(set(pins) | {"pytest", "ruff", "mypy"}) if label == "development environment" else []
        runtime = inspect_runtime(python, packages)
        if runtime is None:
            add("unable to verify", label, "bounded interpreter/metadata probe unavailable")
            continue
        compatible = (runtime.get("implementation") == "CPython"
                      and tuple(runtime["version"][:2]) >= minimum_version
                      and not runtime["gil_disabled"] and not runtime["jit_enabled"])
        add("ready" if compatible else "missing prerequisite", label,
            "CPython minimum version, GIL and disabled experimental JIT requirement")
        for package in packages:
            installed = runtime["packages"].get(package)
            if not installed:
                add("missing prerequisite", package, "not installed in development environment")
            elif package in pins and installed != pins[package]:
                add("missing prerequisite", package, "installed version differs from requirements.lock.txt")
            else:
                add("ready", package, "development package present; locked version checked where specified")

    if probe_docker:
        docker = shutil.which("docker")
        if not docker:
            add("missing prerequisite", "local Docker probe", "docker CLI absent")
        else:
            try:
                result = subprocess.run(
                    [docker, "--host", "unix:///var/run/docker.sock", "version", "--format", "{{.Server.Version}}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=PROBE_TIMEOUT_S, check=False,
                )
                add("ready" if result.returncode == 0 else "unable to verify", "local Docker probe",
                    "explicit local socket read only; remote contexts are not contacted")
            except (OSError, subprocess.TimeoutExpired):
                add("unable to verify", "local Docker probe", "local socket unavailable or bounded probe timed out")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--profile", choices=("unit", "sim", "deploy"), default="unit")
    parser.add_argument("--probe-docker", action="store_true", help="explicit five-second read of local /var/run/docker.sock")
    args = parser.parse_args(argv)
    try:
        findings = diagnose(args.root, args.profile, args.probe_docker)
    except Exception:  # noqa: BLE001 — CLI boundary must redact unexpected failures
        print("internal checker failure: diagnosis unavailable", file=sys.stderr)
        return 1
    for finding in findings:
        print(f"{finding.status}: {finding.subject}: {finding.detail}")
    print("Prerequisites only; no application, test or production acceptance is established.")
    return 0 if all(f.status == "ready" for f in findings) else 2


if __name__ == "__main__":
    raise SystemExit(main())
