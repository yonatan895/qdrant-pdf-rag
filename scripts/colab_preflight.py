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
import shutil
import subprocess
import time
from pathlib import Path


def census() -> dict:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0]) / 1024
    report = {"python": platform.python_version(), "available_ram_mib": memory["MemAvailable"],
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
    try:
        if time.time() >= profile["expires_at"]:
            raise ValueError("run deadline elapsed before preflight")
        report = census()
        admit(profile, report)
        report["admitted"] = True
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report = {"admitted": False, "error": str(exc), "error_type": type(exc).__name__}
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"preflight_admitted": report["admitted"]}), flush=True)
    return 0 if report["admitted"] else 2


if __name__ == "__main__":
    # A notebook exec displays SystemExit as a remote error. That is intentional
    # for a refusal; the host still downloads the census before releasing the VM.
    status = main()
    if status:
        raise SystemExit(status)
