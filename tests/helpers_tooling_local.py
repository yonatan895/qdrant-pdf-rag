"""Shared builders for the review-tooling, acceptance and verifier tests.

The collectors read an approved-main tree, packed evidence archives and a
GitHub API double. Builders here pin only that shape; each test still passes
its own verdict-bearing data explicitly.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

from scripts.acceptance import VERIFICATION_INPUTS
from scripts.acceptance_evidence import PRODUCERS

CI_UNIT_SOURCE = b"    name: unit (${{ matrix.shard }}/2)\n        shard: [1, 2]\n"


def pack_archive(receipt: dict, **members: bytes) -> tuple[bytes, str]:
    """Evidence zip plus the ``sha256:`` digest GitHub reports for it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("evidence.json", json.dumps(receipt))
        for name, content in members.items():
            archive.writestr(name, content)
    raw = buffer.getvalue()
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()


def approved_repository(root: Path, *, policy: bytes = b"approved policy",
                        producer: bytes = b"approved producer",
                        verifier_input: bytes = b"approved verifier input",
                        extras: dict[str, bytes] | None = None) -> str:
    """Materialize the approved-main tree the collectors read; return its SHA.

    ``extras`` wins over the per-workflow defaults and over ``verifier_input``
    for the named paths, so a test can adopt a candidate catalogue or a wider
    unit shard while everything else stays approved.
    """
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts/review_tooling.py").write_bytes(policy)
    (root / "scripts/ci_evidence.py").write_bytes(producer)
    workflows = root / ".github/workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    for workflow in {p.workflow for p in PRODUCERS}:
        workflows.joinpath(workflow).write_bytes(
            CI_UNIT_SOURCE if workflow == "ci.yml" else b"approved workflow")
    extras = extras or {}
    for relative, content in extras.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for relative in (*VERIFICATION_INPUTS, "taskfiles/quality.yml"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(verifier_input)
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    git("init", "-b", "main")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "synthetic@example.invalid")
    git("add", ".")
    git("commit", "-m", "approved base")
    return git("rev-parse", "HEAD")
