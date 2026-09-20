"""scripts/airgap/pipeline.sh orchestrator tests (issue #15).

Tests pipeline.sh in dry-run mode against hermetic stubs:
- Help flag prints usage and exits 0.
- Dry-run mode coordinates validate, load, deploy, ingest, and smoke.
- Flag --skip-load skips load stage.
- Flag --skip-ingest skips ingest stage.
"""

import shutil

import pytest

from tests.helpers_airgap import REPO, copy_chart, install_rendering_helm, make_bin_tree, write_stub

IMAGE_SHA = "e" * 40

STUB_PIPE_TOOL = """#!/bin/sh
if [ "$1" = "kustomize" ] || [ "$1" = "build" ]; then
    echo "apiVersion: v1"
    echo "kind: ConfigMap"
    echo "metadata: {name: stub}"
    # The Qdrant key contract differs per overlay (issue #366): the agent
    # renders the read-only key, ingest the full-access one.
    case "$2" in
      *openshift-ingest*) _qkey="api-key" ;;
      *) _qkey="read-only-api-key" ;;
    esac
    echo "            - name: QDRANT_API_KEY"
    echo "              valueFrom:"
    echo "                secretKeyRef:"
    echo "                  key: $_qkey"
    echo "                  name: stub-apikey"
    exit 0
fi
exit 0
"""


@pytest.fixture
def pipe_tree(tmp_path):
    make_bin_tree(
        tmp_path,
        ["common.sh", "validate.sh", "load.sh", "deploy.sh", "ingest.sh", "smoke.sh", "pipeline.sh", "map_values.py"],
    )
    copy_chart(tmp_path)
    # validate.sh pins the Qdrant key contract on the overlay sources
    # (issue #366): the copied tree needs the real files it inspects.
    agent_overlay = tmp_path / "deploy" / "kustomize" / "overlays" / "openshift"
    agent_overlay.mkdir(parents=True, exist_ok=True)
    ingest_overlay = tmp_path / "deploy" / "kustomize" / "overlays" / "openshift-ingest"
    ingest_overlay.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        REPO / "deploy" / "kustomize" / "overlays" / "openshift" / "agent-prod-patch.yaml",
        agent_overlay,
    )
    shutil.copy(
        REPO / "deploy" / "kustomize" / "overlays" / "openshift-ingest" / "ingest-job.yaml",
        ingest_overlay,
    )

    for name in ("skopeo", "helm", "kubectl", "oc", "kustomize"):
        write_stub(tmp_path / "bin" / name, STUB_PIPE_TOOL)
    install_rendering_helm(tmp_path)
    return tmp_path


def _run_pipeline(pipe_tree, *args, extra_env=None):
    import subprocess as _sp

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
        "EMBED_MODEL_REVISION": "rev-1",
        "VLLM_BASE_URL": "http://vllm:8000/v1",
        # Issue #360: the hermetic single-node pipeline fixtures select the
        # 1/1/1 non-HA profile explicitly (the production preset is tested
        # by test_airgap_ingest_sh/test_airgap_validate_sh).
        "QDRANT_SHARD_NUMBER": "1",
        "QDRANT_REPLICATION_FACTOR": "1",
        "QDRANT_WRITE_CONSISTENCY_FACTOR": "1",
    }
    if extra_env:
        for k, v in extra_env.items():
            env[k] = v
    return _sp.run(
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


@pytest.mark.parametrize("argument", ["--skip-laod", "--skip-ingest=true", "unexpected"])
def test_pipeline_unknown_argument_fails_before_stages(pipe_tree, argument):
    r = _run_pipeline(pipe_tree, argument)
    assert r.returncode != 0
    assert f"unknown argument: {argument}" in r.stderr
    assert "STAGE" not in r.stdout


def test_pipeline_dryrun_full(pipe_tree):
    r = _run_pipeline(pipe_tree, "--skip-load", extra_env={"CORPUS_PVC": "test-corpus"})
    assert r.returncode == 0, r.stderr
    assert "STAGE 1/5: PRE-FLIGHT VALIDATION" in r.stdout
    assert "STAGE 3/5: STACK DEPLOYMENT" in r.stdout
    assert "exec deploy/rag-agent -- python3 /app/scripts/probe_gateway.py" in r.stdout
    assert "STAGE 4/5: CORPUS INGESTION" in r.stdout
    assert "STAGE 5/5: ACCEPTANCE & SMOKE VERIFICATION" in r.stdout
    assert "PIPELINE ORCHESTRATION COMPLETE: AIR-GAP SYSTEM OPERATIONAL & ACCEPTED" in r.stdout


@pytest.mark.parametrize("probe_exit", [0, 1])
def test_pipeline_live_gateway_probe_gates_ingest(pipe_tree, probe_exit):
    # Run the real orchestrator with stage recorders. The gateway command
    # must run between deploy and ingest and use the selected cluster client.
    log = pipe_tree / "stages.log"
    for stage in ("validate", "load", "deploy", "ingest", "smoke"):
        write_stub(
            pipe_tree / "scripts" / "airgap" / f"{stage}.sh",
            f'#!/bin/sh\necho {stage} >> "$STAGE_LOG"\n',
        )
    write_stub(
        pipe_tree / "bin" / "custom-kc",
        '#!/bin/sh\nprintf "probe %s\\n" "$*" >> "$STAGE_LOG"\n'
        f'exit {probe_exit}\n',
    )
    r = _run_pipeline(pipe_tree, extra_env={
        "AIRGAP_DRYRUN": "0", "CORPUS_PVC": "test-corpus",
        "KC": "custom-kc", "STAGE_LOG": str(log),
    })
    stages = log.read_text().splitlines()
    assert stages[:4] == [
        "validate", "load", "deploy",
        "probe -n mainframe-rag exec deploy/rag-agent -- python3 /app/scripts/probe_gateway.py",
    ]
    if probe_exit:
        assert r.returncode != 0
        assert len(stages) == 4
        assert "OPERATIONAL & ACCEPTED" not in r.stdout
    else:
        assert r.returncode == 0, r.stderr
        assert stages[4:] == ["ingest", "smoke"]


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
