"""Direct simulator lifecycle contracts; stdlib-only and independent of Task."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers_simulator import prepare_simulator


class SimQdrantScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "calls.jsonl"
        self.env = prepare_simulator(self.root, self.log)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_sim_helper_up_reuses_running_container(self):
        env = dict(self.env,
                   RECORDER_LOG=str(self.log), RECORDER_EXIT="0", DOCKER_INSPECT_EXIT="0")
        proc = subprocess.run(["sh", str(self.root / "scripts/sim_qdrant.sh"), "up"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60, check=False, text=True, env=env, cwd=str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("already running", proc.stdout)
        docker = [c["argv"] for c in self.calls() if c["argv"][:2] != ["image", "inspect"]]
        self.assertEqual(docker, [["inspect", "qdrant-sim"], ["inspect", "--format", "{{.Image}} {{.State.Running}}", "qdrant-sim"]])

    def test_sim_helper_rejects_existing_wrong_image_without_mutation(self):
        env = dict(self.env,
                   RECORDER_LOG=str(self.log), DOCKER_INSPECT_EXIT="0", DOCKER_RUNNING_IMAGE="sha256:" + "c" * 64)
        proc = subprocess.run(["sh", str(self.root / "scripts/sim_qdrant.sh"), "up"],
                              capture_output=True, text=True, env=env, cwd=self.root, timeout=60, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unapproved image", proc.stderr)
        self.assertFalse(any(c["argv"][0] in ("run", "stop", "pull", "rm") for c in self.calls()))

    def test_sim_helper_up_starts_pinned_image(self):
        env = dict(self.env,
                   RECORDER_LOG=str(self.log), RECORDER_EXIT="0", DOCKER_INSPECT_EXIT="1")
        proc = subprocess.run(["sh", str(self.root / "scripts/sim_qdrant.sh"), "up"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60, check=False, text=True, env=env, cwd=str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        docker = [c["argv"] for c in self.calls() if c["argv"][:2] != ["image", "inspect"]]
        self.assertEqual(len(docker), 2)
        self.assertEqual(docker[0], ["inspect", "qdrant-sim"])
        self.assertIn("127.0.0.1:6333:6333", docker[1])
        self.assertIn("sha256:" + "b" * 64, docker[1])
        self.assertIn("--pull=never", docker[1])
        self.assertIn("QDRANT_SIM_URL=http://127.0.0.1:6333", proc.stdout)

    def test_sim_helper_rejects_empty_names(self):
        env = dict(self.env,
                   RECORDER_LOG=str(self.log), RECORDER_EXIT="0",
                   SIM_CONTAINER="", DOCKER_INSPECT_EXIT="1")
        proc = subprocess.run(["sh", str(self.root / "scripts/sim_qdrant.sh"), "up"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60, check=False, text=True, env=env, cwd=str(self.root))
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("must not be empty", proc.stdout)
        self.assertEqual(self.calls(), [], "docker must not run on invalid input")
        proc = subprocess.run(["sh", str(self.root / "scripts/sim_qdrant.sh"), "bogus"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60, check=False, text=True, env=env, cwd=str(self.root))
        self.assertEqual(proc.returncode, 2)

