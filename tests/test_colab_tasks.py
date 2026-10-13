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
            f"COLAB_DOCKER_GATEWAY={private}",
            extra_env={**boundary.recorder_env, "COLAB_STATE": "ambient wrong value",
                       "PRODUCTION_CORPUS_DIR": "ambient wrong corpus",
                       "COLAB_DOCKER_GATEWAY": "ambient wrong address"},
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
            "--docker-gateway", private,
        ]])
        self.assertFalse(marker.exists(), "Task input was evaluated as shell source")

    def test_prepare_bridges_only_an_explicit_ingest_reingest_choice(self):
        # Issue #737: production resume of a pending build is an explicit choice.
        boundary = self.boundary
        image = "example.invalid/gateway@sha256:" + "c" * 64
        base = ["scripts/colab.py", "prepare", "--state", "/private/run", "--gateway-image", image]
        for value, expected in (("1", base + ["--ingest-reingest"]), ("0", base), ("", base)):
            proc = boundary.run_task("local:colab:prepare", "COLAB_STATE=/private/run",
                                     f"COLAB_GATEWAY_IMAGE={image}", f"COLAB_INGEST_REINGEST={value}",
                                     extra_env={**boundary.recorder_env, "COLAB_INGEST_REINGEST": "1"})
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertEqual(boundary.calls()[0]["argv"], expected)
            boundary.log.unlink()
        for bad in ("true", "2", " 1"):
            proc = boundary.run_task("local:colab:prepare", "COLAB_STATE=/private/run",
                                     f"COLAB_GATEWAY_IMAGE={image}", f"COLAB_INGEST_REINGEST={bad}",
                                     extra_env=boundary.recorder_env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("COLAB_INGEST_REINGEST must be 0 or 1", proc.stdout)
            self.assertEqual(boundary.calls(), [])

    def test_initialize_fresh_preserves_exact_target_inputs_without_cloud_flags(self):
        boundary = self.boundary
        marker = boundary.root / "must-not-exist"
        private = str(boundary.root / f"fresh space'|=;$(touch {marker})")
        values = [
            ("COLAB_STATE", "--state"), ("COLAB_INITIALIZE_COLLECTION", "--collection"),
            ("PRODUCTION_CORPUS_DIR", "--corpus"), ("PRODUCTION_GOLDEN_PATH", "--golden"),
            ("COLAB_INVENTORY", "--inventory"), ("COLAB_SOURCE_INVENTORY", "--source-inventory"),
            ("COLAB_EMBEDDING_EVIDENCE", "--embedding-evidence"),
            ("COLAB_PRODUCTION_APPROVAL", "--production-approval"), ("COLAB_DATA_STATE", "--data-state"),
            ("COLAB_KUBECONFIG", "--kubeconfig"), ("COLAB_KUBE_CONTEXT", "--kube-context"),
            ("COLAB_NAMESPACE", "--namespace"), ("COLAB_QDRANT_SERVICE", "--qdrant-service"),
            ("COLAB_INFERENCE_LOCK", "--inference-lock"), ("COLAB_PROFILE", "--profile"),
            ("COLAB_QDRANT_PORT", "--qdrant-port"), ("COLAB_EMBEDDING_MAX_LEN", "--embedding-max-len"),
            ("COLAB_INITIALIZE_SECONDS", "--deadline-seconds"), ("COLAB_KUBECTL_BIN", "--kubectl"),
        ]
        proc = boundary.run_task("local:colab:initialize-fresh", *(f"{key}={private}/{key}" for key, _ in values),
                                 extra_env={**boundary.recorder_env, "COLAB_STATE": "ambient wrong state"})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(boundary.calls()[0]["argv"], ["scripts/colab.py", "initialize-fresh", *[
            item for key, flag in values for item in (flag, f"{private}/{key}")]])
        self.assertFalse(marker.exists())
        boundary.log.unlink()
        proc = boundary.run_task("local:colab:initialize-fresh", "--", "unreviewed source",
                                 extra_env=boundary.recorder_env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(boundary.calls(), [])

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
