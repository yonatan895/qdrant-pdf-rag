"""Execute the workflow's sneakernet-bundle check with a hermetic `gh` double (#1118).

actions/download-artifact has gone green with no file. The step after each
bundle download must re-download only absent files, a bounded number of times,
and treat a checksum mismatch as fatal.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.helpers_airgap import REPO

SHA = "a" * 40
BUNDLE = f"qdrant-pdf-rag-{SHA}.tar"
JOBS = ("airgap-acceptance", "kind-live-rehearsal", "airgap-rehearsal")
VERIFY = "Verify the bundle arrived (bounded re-download)"


def _bundle_steps():
    jobs = yaml.safe_load((REPO / ".github/workflows/e2e.yml").read_text())["jobs"]
    found = {}
    for name, job in jobs.items():
        steps = job.get("steps", [])
        for index, step in enumerate(steps):
            if ("download-artifact@" in step.get("uses", "")
                    and step.get("with", {}).get("name") == "sneakernet-bundle-${{ env.SHA }}"):
                found[name] = (step, steps[index + 1], job)
    return found


def test_every_bundle_download_is_followed_by_the_same_bounded_check():
    found = _bundle_steps()
    assert sorted(found) == sorted(JOBS)
    runs = set()
    for name, (download, verify, job) in found.items():
        # A red download still reaches the check, which then decides.
        assert download["continue-on-error"] is True, name
        assert verify["name"] == VERIFY, name
        assert verify["env"] == {"GH_TOKEN": "${{ github.token }}",
                                 "BUNDLE_ARTIFACT": "sneakernet-bundle-${{ env.SHA }}"}, name
        assert job["permissions"]["actions"] == "read", name
        runs.add(verify["run"])
    assert len(runs) == 1


def _run(tmp_path: Path, deliveries: list[str], initial: str):
    """Run the step; `initial` and each gh call deliver: none, good or bad."""
    bash = shutil.which("bash")
    assert bash is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    good = tmp_path / "good"
    good.mkdir()
    (good / BUNDLE).write_bytes(b"synthetic bundle bytes")
    (good / (BUNDLE + ".sha256")).write_text(
        f"{hashlib.sha256(b'synthetic bundle bytes').hexdigest()}  {BUNDLE}\n")
    bad = tmp_path / "bad"
    shutil.copytree(good, bad)
    (bad / BUNDLE).write_bytes(b"truncated")
    (tmp_path / "deliveries").write_text("".join(f"{item}\n" for item in deliveries))
    (bin_dir / "gh").write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, shutil, sys\n"
        "work = pathlib.Path(os.environ['WORK'])\n"
        "with open(work / 'gh.log', 'a') as log:\n"
        "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "queue = (work / 'deliveries').read_text().splitlines()\n"
        "item, rest = queue[0], queue[1:]\n"
        "(work / 'deliveries').write_text(''.join(r + '\\n' for r in rest))\n"
        "target = pathlib.Path(sys.argv[sys.argv.index('--dir') + 1])\n"
        "if item != 'none':\n"
        "    shutil.copytree(work / item, target, dirs_exist_ok=True)\n"
    )
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if initial != "none":
        shutil.copytree(tmp_path / initial, workspace / "gapbox")
    step = next(iter(_bundle_steps().values()))[1]
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], cwd=workspace,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "WORK": str(tmp_path), "SHA": SHA,
             "GH_TOKEN": "synthetic", "BUNDLE_ARTIFACT": f"sneakernet-bundle-{SHA}",
             "GITHUB_RUN_ID": "123", "GITHUB_REPOSITORY": "synthetic/repository"},
        capture_output=True, text=True, check=False, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls, workspace / "gapbox"


@pytest.mark.parametrize("initial,deliveries,code,downloads", [
    ("good", [], 0, 0),                    # the action delivered: no re-download
    ("none", ["none", "good"], 0, 2),      # absent twice, then delivered
    ("none", ["none", "none"], 1, 2),      # bounded: three attempts in total
    ("bad", [], 1, 0),                     # a checksum mismatch is fatal at once
    ("none", ["bad", "good"], 1, 1),       # also after a re-download
])
def test_bundle_check_redownloads_only_absent_files(tmp_path, initial, deliveries, code, downloads):
    result, calls, gapbox = _run(tmp_path, deliveries, initial)
    assert result.returncode == code, result.stdout + result.stderr
    assert calls == [f"run download 123 --repo synthetic/repository --name sneakernet-bundle-{SHA} --dir gapbox"] * downloads
    if code == 0:
        assert (gapbox / BUNDLE).read_bytes() == b"synthetic bundle bytes"
    elif initial == "none" and "bad" not in deliveries:
        assert "sneakernet bundle missing after 3 download attempts" in result.stdout
