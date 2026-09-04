"""scripts/airgap/pipeline.sh orchestrator tests (issue #15).

Tests pipeline.sh in dry-run mode against hermetic stubs:
- Help flag prints usage and exits 0.
- Dry-run mode coordinates validate, load, deploy, ingest, and smoke.
- Flag --skip-load skips load stage.
- Flag --skip-ingest skips ingest stage.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMAGE_SHA = "e" * 40

STUB_TOOL = """#!/bin/sh
if [ "$1" = "kustomize" ] || [ "$1" = "build" ]; then
    echo "apiVersion: v1"
    echo "kind: ConfigMap"
    echo "metadata: {name: stub}"
    exit 0
fi
exit 0
"""


@pytest.fixture
def pipe_tree(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "charts").mkdir()
    (tmp_path / "scripts" / "airgap").mkdir(parents=True)
    for f in ("common.sh", "validate.sh", "load.sh", "deploy.sh", "ingest.sh", "smoke.sh", "pipeline.sh"):
        shutil.copy(REPO / "scripts" / "airgap" / f, tmp_path / "scripts" / "airgap" / f)
    shutil.copy(next(REPO.glob("charts/qdrant-*.tgz")), tmp_path / "charts")

    for name in ("skopeo", "helm", "kubectl", "oc", "kustomize"):
        p = tmp_path / "bin" / name
        p.write_text(STUB_TOOL)
        p.chmod(0o755)
    return tmp_path


def _run_pipeline(pipe_tree, *args, extra_env=None):
    env = {
        "PATH": f"{pipe_tree / 'bin'}:/usr/bin:/bin",
        "AIRGAP_DRYRUN": "1",
        "AIRGAP_ENV": "/dev/null",
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
        "NAMESPACE": "mainframe-rag",
        "STORAGE_CLASS": "standard",
        "EMBED_MODEL": "ibm-granite/granite-embedding-125m-english",
        "DENSE_DIM": "768",
        "VLLM_BASE_URL": "http://vllm:8000/v1",
    }
    if extra_env:
        for k, v in extra_env.items():
            env[k] = v
    return subprocess.run(
        ["sh", str(pipe_tree / "scripts" / "airgap" / "pipeline.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=pipe_tree,
        check=False,
    )


def test_pipeline_help(pipe_tree):
    r = _run_pipeline(pipe_tree, "--help")
    assert r.returncode == 0
    assert "Usage:" in r.stdout


def test_pipeline_dryrun_full(pipe_tree):
    r = _run_pipeline(pipe_tree, "--skip-load", extra_env={"CORPUS_PVC": "test-corpus"})
    assert r.returncode == 0, r.stderr
    assert "STAGE 1/5: PRE-FLIGHT VALIDATION" in r.stdout
    assert "STAGE 3/5: STACK DEPLOYMENT" in r.stdout
    assert "STAGE 4/5: CORPUS INGESTION" in r.stdout
    assert "STAGE 5/5: ACCEPTANCE & SMOKE VERIFICATION" in r.stdout
    assert "PIPELINE ORCHESTRATION COMPLETE: AIR-GAP SYSTEM OPERATIONAL & ACCEPTED" in r.stdout


def test_pipeline_dryrun_including_load(pipe_tree):
    # Tests that load.sh honors dryrun without requiring --skip-load
    r = _run_pipeline(pipe_tree, extra_env={"CORPUS_PVC": "test-corpus"})
    assert r.returncode == 0, r.stderr
    assert "STAGE 2/5: IMAGE LOADING & INTEGRITY" in r.stdout
    assert "Loaded 4 images into reg.internal:5000 (dry-run)" in r.stdout
    assert "PIPELINE ORCHESTRATION COMPLETE: AIR-GAP SYSTEM OPERATIONAL & ACCEPTED" in r.stdout


def test_pipeline_skip_ingest_flag(pipe_tree):
    r = _run_pipeline(pipe_tree, "--skip-load", "--skip-ingest", extra_env={"CORPUS_PVC": "test-corpus"})
    assert r.returncode == 0, r.stderr
    assert "STAGE 4/5: CORPUS INGESTION (SKIPPED via --skip-ingest)" in r.stdout
    assert "PIPELINE ORCHESTRATION COMPLETE: DEPLOYMENT READY (Awaiting Corpus Ingest)" in r.stdout


def test_pipeline_without_corpus_pvc_awaits_ingest(pipe_tree):
    r = _run_pipeline(pipe_tree, "--skip-load")
    assert r.returncode == 0, r.stderr
    assert "STAGE 4/5: CORPUS INGESTION (SKIPPED — CORPUS_PVC not set)" in r.stdout
    assert "PIPELINE ORCHESTRATION COMPLETE: DEPLOYMENT READY (Awaiting Corpus Ingest)" in r.stdout
