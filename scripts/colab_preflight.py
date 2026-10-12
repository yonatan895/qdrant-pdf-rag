#!/usr/bin/env python3
"""Remote, standard-library-only resource census; executes before any weights.

The controller uploads the portable shared profile module before this file is
executed in Colab. This script never allocates a runtime or installs packages.
"""
from __future__ import annotations

import csv
import json
import os
import platform
import re
import shutil
import subprocess
import time
from importlib import metadata
from pathlib import Path


def observe_kernel_pip() -> dict:
    """Record the preinstalled installer; never bootstrap or upgrade it."""
    try:
        version = metadata.version("pip")
    except metadata.PackageNotFoundError:
        return {"version": None, "error": "not_found"}
    except (OSError, ValueError, TypeError):
        return {"version": None, "error": "probe_failed"}
    if (type(version) is not str or len(version) > 64
            or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version)):
        return {"version": None, "error": "invalid_version"}
    supported = tuple(int(part) for part in version.split(".")[:2]) >= (22, 3)
    return {"version": version, "error": None if supported else "unsupported_version"}


def observe_inference_python() -> dict:
    """Measure the separate interpreter, rather than assuming kernel parity."""
    executable = shutil.which("python3.12")
    observed = {"executable": executable, "version": None, "implementation": None,
                "error": "not_found" if executable is None else None}
    if executable is None:
        return observed
    try:
        result = subprocess.run(
            [executable, "-I", "-S", "-B", "-c",
             ("import json, platform; print(json.dumps({'version': platform.python_version(), "
              "'implementation': platform.python_implementation()}))")],
            check=True, capture_output=True, text=True, timeout=3,
        )
        value = json.loads(result.stdout)
        if (not isinstance(value, dict) or set(value) != {"version", "implementation"}
                or any(type(item) is not str or not 1 <= len(item) <= 64
                       for item in value.values())):
            raise ValueError("invalid interpreter observation")
        observed.update(value)
    except subprocess.TimeoutExpired:
        observed["error"] = "timeout"
    except (OSError, subprocess.SubprocessError):
        observed["error"] = "probe_failed"
    except ValueError:
        observed["error"] = "invalid_output"
    return observed


def census() -> dict:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0]) / 1024
    try:
        distribution = platform.freedesktop_os_release()
    except (OSError, ValueError):
        distribution = {}
    try:
        glibc = os.confstr("CS_GNU_LIBC_VERSION")
    except (OSError, ValueError):
        glibc = None
    report = {"python": platform.python_version(),
              "python_implementation": platform.python_implementation(),
              "platform": {"system": platform.system(), "machine": platform.machine(),
                           "glibc": glibc, "distribution_id": distribution.get("ID"),
                           "distribution_version": distribution.get("VERSION_ID")},
              "inference_python": observe_inference_python(),
              "kernel_pip": observe_kernel_pip(),
              "available_ram_mib": memory["MemAvailable"],
              "free_disk_mib": shutil.disk_usage("/content").free / 1024**2,
              "gpu_name": "CPU", "vram_mib": 0, "driver_version": None}
    if shutil.which("nvidia-smi"):
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                 "--format=csv,noheader,nounits"],
                                check=True, capture_output=True, text=True, timeout=15)
        rows = list(csv.reader(result.stdout.strip().splitlines()))
        if len(rows) != 1:
            raise ValueError("reference profile requires exactly one observed GPU")
        name, vram, driver = rows[0]
        report.update(gpu_name=name.strip(), vram_mib=float(vram), driver_version=driver.strip())
    return report


def main() -> int:
    # Colab exec runs source in a notebook kernel: no __file__/argv dependency.
    from colab_profiles import admit
    profile = json.loads(Path(os.environ["RAG_COLAB_PROFILE"]).read_text())
    destination = Path("/content/colab-preflight.json")
    report = {}
    try:
        if time.time() >= profile["expires_at"]:
            raise ValueError("run deadline elapsed before preflight")
        report = census()
        admit(profile, report)
        interpreter = report["inference_python"]
        if profile["accelerator"] and (
                interpreter["error"] is not None or interpreter["implementation"] != "CPython"
                or not re.fullmatch(r"3\.12\.[0-9]+", interpreter["version"] or "")):
            raise ValueError("preinstalled CPython 3.12 is required; no paid bootstrap fallback")
        if profile["accelerator"] and report["kernel_pip"]["error"] is not None:
            raise ValueError("preinstalled kernel pip with --python support is required")
        report["admitted"] = True
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report.update(admitted=False, error=str(exc), error_type=type(exc).__name__)
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"preflight_admitted": report["admitted"]}), flush=True)
    return 0 if report["admitted"] else 2


if __name__ == "__main__":
    # A notebook exec displays SystemExit as a remote error. That is intentional
    # for a refusal; the host still downloads the census before releasing the VM.
    status = main()
    if status:
        raise SystemExit(status)
