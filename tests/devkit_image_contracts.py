"""Dev kit image contracts: every command the gap scripts call is in the built image (#1183).

Not a unit test (the file name keeps it out of default collection, like
tests/openwebui_image_contracts.py): it needs Docker and a built kit image. The unit
suite checks the same map (tests/helpers_devkit_commands.py KIT_COMMANDS) against the
recipe without an image (tests/test_devkit_kit.py); this file checks it against the
image itself, so a provider recorded wrongly in the map fails here.

  MFR_DEVKIT_IMAGE=<image> flock ~/.cache/agent-tmp/mainframe-rag-heavy.lock \
      mfr-pytest <worktree> tests/devkit_image_contracts.py -rA

The image defaults to the pinned digest (scripts/tools/devkit-pin.txt) when it is
present locally; it is never pulled. Without Docker or the image it skips, and a skip
is not a pass. Run it after any change to the kit's RPM list, tools or base, and when
a gap script starts calling a new command.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers_devkit_commands import KIT_COMMANDS

REPO = Path(__file__).resolve().parent.parent
# The arbitrary non-root UID in group 0 that OpenShift restricted-v2 assigns (devkit.yml).
SMOKE_USER = "1000770000:0"


def _image() -> str:
    explicit = os.environ.get("MFR_DEVKIT_IMAGE")
    if explicit:
        return explicit
    digest = re.search(r"^digest: *(sha256:[0-9a-f]{64})$",
                       (REPO / "scripts/tools/devkit-pin.txt").read_text(encoding="utf-8"), re.MULTILINE)
    assert digest, "scripts/tools/devkit-pin.txt has no digest"
    return f"ghcr.io/yonatan895/qdrant-pdf-rag-devkit@{digest.group(1)}"


@pytest.fixture(scope="module")
def resolved() -> dict[str, tuple[str, str]]:
    """{command: (path from `command -v` on the image's own PATH, owning RPM or '-')}."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    image = _image()
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False).returncode != 0:
        pytest.skip(f"{image} is not present locally (never pulled here)")
    probe = (
        'for c in "$@"; do p=$(command -v "$c" 2>/dev/null) || p=-; '
        'o=-; case "$p" in /*) o=$(rpm -qf --qf "%{NAME}" "$p" 2>/dev/null) || o=-;; esac; '
        'printf "%s\\t%s\\t%s\\n" "$c" "$p" "$o"; done')
    run = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", SMOKE_USER, image,
                          "sh", "-c", probe, "sh", *sorted(KIT_COMMANDS)],
                         capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode == 0, run.stderr[-2000:]
    out: dict[str, tuple[str, str]] = {}
    for line in run.stdout.splitlines():
        name, path, owner = line.split("\t")
        out[name] = (path, owner)
    assert set(out) == set(KIT_COMMANDS)
    return out


def test_every_gap_command_is_on_the_kit_images_path(resolved):
    missing = sorted(name for name, (path, _) in resolved.items() if path == "-")
    assert missing == [], f"the kit image lacks commands the gap scripts call: {missing}"


def test_each_command_comes_from_its_recorded_provider(resolved):
    """The map's provider is the real one: the RPM owns the path, a pinned tool or kit
    helper is in /usr/local/bin, python3 is the base environment's."""
    wrong = []
    for name, provider in sorted(KIT_COMMANDS.items()):
        path, owner = resolved[name]
        kind, _, detail = provider.partition(":")
        if kind == "rpm":
            ok = owner == detail
        elif kind in ("tool", "kit-bin"):
            ok = path == f"/usr/local/bin/{name}" and owner == "-"
        elif kind == "app-root":
            ok = path == f"/opt/app-root/bin/{name}"
        else:
            ok = False
        if not ok:
            wrong.append(f"{name}: recorded {provider}, image has {path} (rpm {owner})")
    assert wrong == []
