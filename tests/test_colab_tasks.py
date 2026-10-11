"""Colab Task argv contracts; inert interpreter, no Colab/model/service calls."""
from __future__ import annotations

import os
import unittest

from tests import test_taskfile_contracts as contracts


class ColabTaskTests(unittest.TestCase):
    def setUp(self):
        # Reuse the provisioned pinned Task/temporary workspace contract without
        # inheriting (and collecting) its unrelated task tests a second time.
        self.boundary = contracts.TaskContractsTests(methodName="runTest")
        self.addCleanup(self.boundary.doCleanups)
        self.boundary.setUp()
        self.boundary.make_venv_fake()
        self.boundary.recorder_env.update({
            key: "" for key in os.environ
            if key.startswith("COLAB_") or key in ("PRODUCTION_CORPUS_DIR", "PRODUCTION_GOLDEN_PATH")
        })

    def test_prepare_preserves_private_values_and_overrides_ambient_inputs(self):
        boundary = self.boundary
        marker = boundary.root / "must-not-exist"
        private = str(boundary.root / f"private space'|=;$(touch {marker})")
        image = "example.invalid/gateway@sha256:" + "a" * 64
        proc = boundary.run_task(
            "local:colab:prepare", f"COLAB_STATE={private}",
            f"COLAB_GATEWAY_IMAGE={image}", "COLAB_PROFILE=COLAB_CPU_TRANSPORT",
            "COLAB_DATASET=synthetic", f"PRODUCTION_CORPUS_DIR={private}/manuals",
            f"PRODUCTION_GOLDEN_PATH={private}/grounded.jsonl",
            f"COLAB_INVENTORY={private}/inventory.json",
            f"COLAB_SOURCE_INVENTORY={private}/source-discovery.json",
            f"COLAB_EMBEDDING_EVIDENCE={private}/embedding evidence",
            f"COLAB_PRODUCTION_APPROVAL={private}/production-approval.json",
            f"COLAB_DATA_STATE={private}/retained", "COLAB_KUBE_CONTEXT=context = 'one'",
            "COLAB_DEADLINE_SECONDS=180", "COLAB_CU_CAP=1", "COLAB_CU_FLOOR=10",
            extra_env={**boundary.recorder_env, "COLAB_STATE": "ambient wrong value",
                       "PRODUCTION_CORPUS_DIR": "ambient wrong corpus"},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual([call["argv"] for call in boundary.calls()], [[
            "scripts/colab.py", "prepare", "--state", private,
            "--gateway-image", image, "--profile", "COLAB_CPU_TRANSPORT",
            "--dataset", "synthetic", "--corpus", private + "/manuals",
            "--golden", private + "/grounded.jsonl", "--inventory", private + "/inventory.json",
            "--source-inventory", private + "/source-discovery.json",
            "--embedding-evidence", private + "/embedding evidence",
            "--production-approval", private + "/production-approval.json",
            "--data-state", private + "/retained", "--kube-context", "context = 'one'",
            "--deadline-seconds", "180", "--cu-cap", "1", "--cu-floor", "10",
        ]])
        self.assertFalse(marker.exists(), "Task input was evaluated as shell source")

    def test_prepare_omits_optional_flags_and_requires_explicit_state_and_image(self):
        boundary = self.boundary
        image = "example.invalid/gateway@sha256:" + "b" * 64
        proc = boundary.run_task("local:colab:prepare", "COLAB_STATE=/private/run",
                                 extra_env=boundary.recorder_env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(boundary.calls(), [])
        proc = boundary.run_task("local:colab:prepare", "COLAB_STATE=/private/run",
                                 f"COLAB_GATEWAY_IMAGE={image}", "COLAB_PROFILE=",
                                 extra_env=boundary.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(boundary.calls()[0]["argv"], [
            "scripts/colab.py", "prepare", "--state", "/private/run", "--gateway-image", image,
        ])

    def test_run_requires_cu_approval_and_preserves_one_executable_path(self):
        boundary = self.boundary
        private = "/private/run space|='one'"
        proc = boundary.run_task("local:colab:run", f"COLAB_STATE={private}",
                                 "COLAB_APPROVE_CU=", extra_env=boundary.recorder_env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(boundary.calls(), [])
        workload = "/private/wrapper space|='two'"
        proc = boundary.run_task("local:colab:run", f"COLAB_STATE={private}",
                                 "COLAB_APPROVE_CU=20", f"COLAB_WORKLOAD={workload}",
                                 extra_env=boundary.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(boundary.calls()[0]["argv"], [
            "scripts/colab.py", "run", "--state", private, "--approve-cu", "20", "--", workload,
        ])

    def test_stop_preserves_exact_run_state_and_rejects_trailing_shell_source(self):
        boundary = self.boundary
        private = "/private/run space|='one'"
        proc = boundary.run_task("local:colab:stop", f"COLAB_STATE={private}",
                                 "--", "unreviewed command", extra_env=boundary.recorder_env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(boundary.calls(), [])
        proc = boundary.run_task("local:colab:stop", f"COLAB_STATE={private}",
                                 extra_env=boundary.recorder_env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(boundary.calls()[0]["argv"], [
            "scripts/colab.py", "stop", "--state", private,
        ])
