"""Original simulator image/daemon stand-ins shared by script and Task bridges."""
from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def prepare_simulator(root: Path, log: Path) -> dict[str, str]:
    """Own only this workspace; no Task lookup, real daemon or image acquisition."""
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    for name in ("sim_qdrant.sh", "qdrant_pin.py"):
        shutil.copy(REPO / "scripts" / name, scripts / name)
    (root / "images.txt").write_text(
        "example.com/qdrant/qdrant:v9.9.9-unprivileged sha256:" + "a" * 64 + "\n", encoding="utf-8")
    bindir = root / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / "docker"
    # The recorder runs on this test's interpreter: os.defpath need not hold a python3
    # (the dev kit image has only python3.14 in /usr/bin, #1183).
    path.write_text(
        '#!/bin/sh\n'
        f'{shlex.quote(sys.executable)} - "$RECORDER_LOG" "tool-docker" "$@" <<\'PYEOF\'\n'
        'import json, os, sys\n'
        'log, tag, argv = sys.argv[1], sys.argv[2], sys.argv[3:]\n'
        'with open(log, "a", encoding="utf-8") as fh:\n'
        '    fh.write(json.dumps({"tag": tag, "argv": argv, "cwd": os.getcwd()}) + "\\n")\n'
        'if argv[:2] == ["container", "inspect"]:\n'
        '    argv = argv[1:]\n'
        'if argv[:2] == ["image", "inspect"]:\n'
        '    print(json.dumps([{"Id": "sha256:" + "b" * 64, "RepoDigests": ["example.com/qdrant/qdrant@sha256:" + "a" * 64]}]))\n'
        'if argv[:2] == ["inspect", "--format"] and "Mounts" in argv[2]:\n'
        '    print(os.environ.get("DOCKER_MOUNT_VOLUME", ""))\n'
        'elif argv[:2] == ["inspect", "--format"]:\n'
        '    print(os.environ.get("DOCKER_RUNNING_IMAGE", "sha256:" + "b" * 64) + " true")\n'
        'PYEOF\n'
        '# DOCKER_INSPECT_EXIT=0: the container exists. DOCKER_VOLUME_EXISTS=1: only a volume of\n'
        '# that name exists, which an untyped inspect finds (its --format has no .Image: exit 1).\n'
        'if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then exit "${DOCKER_INSPECT_EXIT:-1}"; fi\n'
        'if [ "$1" = "inspect" ]; then\n'
        '    [ "${DOCKER_INSPECT_EXIT:-1}" = 0 ] && exit 0\n'
        '    [ "${DOCKER_VOLUME_EXISTS:-0}" = 1 ] && [ "$2" != "--format" ] && exit 0\n'
        '    exit 1\n'
        'fi\n'
        'exit 0\n',
        encoding="utf-8")
    path.chmod(0o755)
    return {
        "PATH": str(bindir) + os.pathsep + os.defpath,
        "RECORDER_LOG": str(log), "DOCKER_INSPECT_EXIT": "1",
        "SIM_CONTAINER": "qdrant-sim", "SIM_PORT": "6333", "PY": sys.executable,
    }
