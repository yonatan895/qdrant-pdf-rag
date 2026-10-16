"""Simulator image gate: qa:sim and the CI sim tier must never run against an
unapproved Qdrant image (scripts/sim_qdrant.sh refuses it and never pulls)."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers_simulator import prepare_simulator


class SimQdrantScriptTests(unittest.TestCase):
    def test_sim_helper_rejects_existing_wrong_image_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "calls.jsonl"
            env = dict(prepare_simulator(root, log), RECORDER_LOG=str(log),
                       DOCKER_INSPECT_EXIT="0", DOCKER_RUNNING_IMAGE="sha256:" + "c" * 64)
            proc = subprocess.run(["sh", str(root / "scripts/sim_qdrant.sh"), "up"],
                                  capture_output=True, text=True, env=env, cwd=root, timeout=60, check=False)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unapproved image", proc.stderr)
        self.assertFalse(any(c["argv"][0] in ("run", "stop", "pull", "rm") for c in calls))
