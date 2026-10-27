"""scripts/airgap/pipeline.sh orchestrator tests (issue #15).

Tests pipeline.sh in dry-run mode against hermetic stubs:
- Help flag prints usage and exits 0.
- Dry-run mode coordinates validate, load, deploy, ingest, and smoke.
- Flag --skip-load skips load stage.
- Flag --skip-ingest skips ingest stage.
"""


import os
import shutil

import pytest

from tests.helpers_airgap import REPO, copy_chart, install_rendering_helm, make_bin_tree, write_stub

IMAGE_SHA = "e" * 40

STUB_PIPE_TOOL = "#!/bin/sh\nexit 0\n"


@pytest.fixture
def pipe_tree(tmp_path):
    make_bin_tree(
        tmp_path,
        ["common.sh", "validate.sh", "load.sh", "deploy.sh", "ingest.sh", "smoke.sh", "pipeline.sh", "map_values.py"],
    )
    copy_chart(tmp_path)
    shutil.copy(REPO / "charts" / "qdrant-openshift.values.yaml", tmp_path / "charts")
    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(tmp_path / "bin" / name, STUB_PIPE_TOOL)
    install_rendering_helm(tmp_path)
    return tmp_path


def _run_pipeline(pipe_tree, *args, extra_env=None):
    import subprocess as _sp

    # Issue #478: an empty regular file (not /dev/null) keeps the
    # environment-only shape while ignoring repo ./airgap.env.
    empty_env = pipe_tree / "empty.env"
    empty_env.write_text("")
    env = {
        "PATH": f"{pipe_tree / 'bin'}:/usr/bin:/bin",
        "AIRGAP_DRYRUN": "1",
        "AIRGAP_ENV": str(empty_env),
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
        "NAMESPACE": "mainframe-rag",
        "STORAGE_CLASS": "standard",
        "EMBED_MODEL": "ibm-granite/granite-embedding-125m-english",
        "DENSE_DIM": "768",
        "EMBED_MODEL_REVISION": "rev-1",
        "EMBED_BASE_URL": "http://vllm:8000/v1",
        "OPENWEBUI_ENABLED": "false",  # default-on (ADR-0008): fixtures opt out explicitly
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


@pytest.mark.parametrize("argument", ["--skip-laod"])
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
    assert "STAGE 5/5: SMOKE VERIFICATION" in r.stdout
    assert "PIPELINE DRY-RUN COMPLETE: rendering passed; live acceptance NOT RUN" in r.stdout


@pytest.mark.parametrize(("probe_exit", "skip_ingest", "budget"), [
    (0, False, "16"), (0, True, "512"), (1, False, "16"),
])
def test_pipeline_live_gateway_probe_gates_ingest(pipe_tree, probe_exit, skip_ingest, budget):
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
    r = _run_pipeline(pipe_tree, *(["--skip-ingest"] if skip_ingest else []), extra_env={
        "AIRGAP_DRYRUN": "0", "CORPUS_PVC": "test-corpus",
        "KC": "custom-kc", "STAGE_LOG": str(log), "GATEWAY_PROBE_MAX_TOKENS": budget,
    })
    stages = log.read_text().splitlines()
    assert stages[:4] == [
        "validate", "load", "deploy",
        f"probe -n mainframe-rag exec deploy/rag-agent -- python3 /app/scripts/probe_gateway.py --max-tokens {budget}",
    ]
    if probe_exit:
        assert r.returncode != 0
        assert len(stages) == 4
        assert "OPERATIONAL & ACCEPTED" not in r.stdout
    else:
        assert r.returncode == 0, r.stderr
        assert stages[4:] == (["smoke"] if skip_ingest else ["ingest", "smoke"])
        assert "PIPELINE STAGES COMPLETE" in r.stdout
        assert ("ingest NOT RUN" in r.stdout) is skip_ingest
        assert "Full application/release acceptance remains separate" in r.stdout
    assert "OPERATIONAL & ACCEPTED" not in r.stdout


@pytest.mark.parametrize("bad", ["0", "4097", "bad"])
def test_pipeline_bad_probe_budget_fails_before_stages(pipe_tree, bad):
    r = _run_pipeline(pipe_tree, extra_env={"GATEWAY_PROBE_MAX_TOKENS": bad})
    assert r.returncode != 0
    assert "GATEWAY_PROBE_MAX_TOKENS must be" in r.stderr
    assert "STAGE" not in r.stdout


def test_pipeline_dryrun_including_load(pipe_tree):
    # Tests that load.sh honors dryrun without requiring --skip-load
    r = _run_pipeline(pipe_tree, extra_env={"CORPUS_PVC": "test-corpus"})
    assert r.returncode == 0, r.stderr
    assert "STAGE 2/5: IMAGE LOADING & INTEGRITY" in r.stdout
    assert "Loaded 5 images into reg.internal:5000 (dry-run)" in r.stdout
    assert "PIPELINE DRY-RUN COMPLETE: rendering passed; live acceptance NOT RUN" in r.stdout


def test_pipeline_skip_ingest_flag(pipe_tree):
    r = _run_pipeline(pipe_tree, "--skip-load", "--skip-ingest", extra_env={"CORPUS_PVC": "test-corpus"})
    assert r.returncode == 0, r.stderr
    assert "STAGE 4/5: CORPUS INGESTION (SKIPPED via --skip-ingest)" in r.stdout
    assert "PIPELINE DRY-RUN COMPLETE: rendering passed; live acceptance NOT RUN" in r.stdout


def test_pipeline_without_corpus_pvc_awaits_ingest(pipe_tree):
    r = _run_pipeline(pipe_tree, "--skip-load")
    assert r.returncode == 0, r.stderr
    assert "STAGE 4/5: CORPUS INGESTION (SKIPPED — CORPUS_PVC not set)" in r.stdout
    assert "PIPELINE DRY-RUN COMPLETE: rendering passed; live acceptance NOT RUN" in r.stdout


def test_incomplete_reasoning_stops_before_load_then_corrected_config_runs(pipe_tree):
    calls = pipe_tree / "stage-calls"
    for stage in ("load", "deploy", "ingest", "smoke"):
        write_stub(pipe_tree / "scripts/airgap" / f"{stage}.sh",
                   f'#!/bin/sh\nprintf "{stage}\\n" >> "{calls}"\n')
    config = {"LLM_MODEL_REASONING": "code", "CORPUS_PVC": "corpus"}
    result = _run_pipeline(pipe_tree, extra_env=config)
    assert result.returncode != 0
    assert "LLM_BASE_URL is required when LLM_MODEL_REASONING is set" in result.stderr
    assert not calls.exists()
    config["GATEWAY_BASE_URL"] = "https://shared.example/v1"
    result = _run_pipeline(pipe_tree, extra_env=config)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["load", "deploy", "ingest", "smoke"]


def test_pipeline_dryrun_with_the_seven_openwebui_inputs_needs_the_ca_only_to_render(pipe_tree):
    """B1: stage 1 (validate) passes with the seven inputs and no service CA; the
    dry-run render then refuses with the existing Route-rendering message (live
    deploy fetches the CA instead), never at validation."""
    from tests.helpers_airgap import set_openwebui_pin

    shutil.copy(REPO / "images.txt", pipe_tree / "images.txt")
    set_openwebui_pin(pipe_tree, "sha256:" + "b" * 64)
    pipe = pipe_tree / "charts" / "mainframe-rag" / "files" / "openwebui" / "docs_pipe.py"
    pipe.write_text("class Pipe:\n    pass\n")
    inputs = {
        "OPENWEBUI_ENABLED": "true", "OPENWEBUI_SECRET": "owui-secrets",
        "OPENWEBUI_HOST": "docs.apps.example.internal",
        "OPENWEBUI_OIDC_PROVIDER_URL": "https://idp.example.internal/.well-known/openid-configuration",
        "OPENWEBUI_OIDC_CLIENT_ID": "docs-frontend", "OPENWEBUI_OIDC_ALLOWED_ROLES": "staff",
        "OPENWEBUI_OIDC_ADMIN_ROLES": "docs-admins", "OPENWEBUI_IDP_EGRESS_CIDRS": "10.20.0.0/24",
        "CORPUS_PVC": "test-corpus",
    }
    r = _run_pipeline(pipe_tree, "--skip-load", extra_env=inputs)
    assert "inputs are missing" not in r.stdout + r.stderr
    assert "STAGE 1/5" in r.stdout
    assert r.returncode != 0 and "needs ROUTE_DESTINATION_CA_FILE" in r.stderr
    assert "STAGE 3/5" in r.stdout
    ca = pipe_tree / "service-ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nU0VSVklDRS1DQQ==\n-----END CERTIFICATE-----\n")
    r = _run_pipeline(pipe_tree, "--skip-load", extra_env={**inputs, "ROUTE_DESTINATION_CA_FILE": str(ca)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "STAGE 4/5: CORPUS INGESTION" in r.stdout


def test_pipeline_refuses_a_legacy_collection_at_validation_before_any_change(pipe_tree):
    """Issue #1070 (R1): the d53595e plain collection stops the live pipeline
    at stage 1, before the registry load and every cluster change."""
    from tests.helpers_airgap import FakeQdrant

    recording = r"""#!/bin/sh
printf '%s\n' "$0 $*" >> "$TOOL_LOG"
case "$*" in
  *'get statefulset/qdrant '*) echo statefulset.apps/qdrant ;;
  *'exec -i deploy/rag-agent -- python3 -c '*)
    while [ "$1" != "--" ]; do shift; done; shift
    exec env QDRANT_URL="$FAKE_QDRANT_URL" QDRANT_API_KEY="$FAKE_READ_KEY" "$@" ;;
esac
exit 0
"""
    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(pipe_tree / "bin" / name, recording)
    qdrant = FakeQdrant()
    try:
        qdrant.add_collection("mainframe_manuals", [{"id": 1, "vector": {"dense": [0.0] * 4}}])
        log = pipe_tree / "tool.log"
        r = _run_pipeline(pipe_tree, extra_env={
            "AIRGAP_DRYRUN": "0", "TOOL_LOG": str(log),
            "FAKE_QDRANT_URL": qdrant.url, "FAKE_READ_KEY": FakeQdrant.READ_KEY})
    finally:
        qdrant.close()
    assert r.returncode != 0
    assert "E_LEGACY_COLLECTION" in r.stderr
    assert "STAGE 2/5" not in r.stdout
    calls = log.read_text()
    assert "skopeo copy" not in calls and " upgrade " not in calls
    assert " apply " not in calls and " delete " not in calls and " create " not in calls


def test_pipeline_refuses_a_d53595e_shaped_env_before_stage_one(pipe_tree):
    from tests.helpers_airgap import D53595E_SHAPED_ENV, RETIRED_ENV_KEYS, retired_keys_listed

    log = pipe_tree / "stages.log"
    for stage in ("validate", "load", "deploy", "ingest", "smoke"):
        write_stub(
            pipe_tree / "scripts" / "airgap" / f"{stage}.sh",
            f'#!/bin/sh\necho {stage} >> "{log}"\n',
        )
    env_file = pipe_tree / "d53595e.env"
    env_file.write_text(D53595E_SHAPED_ENV)
    r = _run_pipeline(pipe_tree, "--skip-load", extra_env={"AIRGAP_ENV": str(env_file)})
    assert r.returncode != 0, r.stdout
    assert sorted(retired_keys_listed(r.stderr)) == sorted(RETIRED_ENV_KEYS), r.stderr
    assert "sentinel-" not in r.stdout + r.stderr
    assert "STAGE" not in r.stdout
    assert not log.exists()


# --- #1118 B2: the Task dry-run renders the default-on Open WebUI from the
# documented fake site inputs; the operator path keeps failing closed.

def _task_tree(pipe_tree):
    from tests.test_taskfile_contracts import find_task

    task = find_task()
    if task is None:
        if os.environ.get("TASK_CONTRACTS_REQUIRE_RUNNER") == "1":
            pytest.fail("pinned Task unavailable; the required lane must fail, not skip")
        pytest.skip("pinned Task not provisioned; see scripts/tools/install-task.sh")
    shutil.copy(REPO / "Taskfile.yml", pipe_tree / "Taskfile.yml")
    shutil.copytree(REPO / "taskfiles", pipe_tree / "taskfiles")
    shutil.copy(REPO / "images.txt", pipe_tree / "images.txt")
    shutil.copy(REPO / "scripts/airgap/collection-policy.env", pipe_tree / "scripts/airgap")
    return task


def _run_task(pipe_tree, task, *args, extra_env=None):
    import subprocess as _sp

    env = {"PATH": f"{pipe_tree / 'bin'}:{os.path.dirname(task)}:/usr/bin:/bin",
           "HOME": str(pipe_tree), **(extra_env or {})}
    return _sp.run([task, "--taskfile", str(pipe_tree / "Taskfile.yml"), "--dir", str(pipe_tree), *args],
                   capture_output=True, text=True, env=env, cwd=pipe_tree, check=False, timeout=300)


def _subset(expected, actual, path="openwebui"):
    """Every value the fixture sets reaches the mapped release values."""
    if isinstance(expected, dict):
        for key, value in expected.items():
            assert isinstance(actual, dict) and key in actual, f"{path}.{key} missing"
            _subset(value, actual[key], f"{path}.{key}")
    else:
        assert actual == expected, path


def test_task_dryrun_renders_open_webui_from_the_site_fixture(pipe_tree):
    import yaml

    task = _task_tree(pipe_tree)
    # An ambient opt-out (as a CI job environment sets) does not mask the default-on path.
    r = _run_task(pipe_tree, task, "airgap:dryrun", extra_env={"OPENWEBUI_ENABLED": "false"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PIPELINE DRY-RUN COMPLETE: rendering passed; live acceptance NOT RUN" in r.stdout
    fixture = yaml.safe_load((REPO / "charts/mainframe-rag/ci/openwebui-site-values.yaml").read_text())
    values = yaml.safe_load((pipe_tree / "dist/mainframe-rag-release-values.yaml").read_text())
    assert values["openwebui"]["enabled"] is True
    _subset(fixture["openwebui"], values["openwebui"])
    assert values["route"]["destinationCA"] == fixture["route"]["destinationCA"]
    assert (REPO / "charts/mainframe-rag/ci/openwebui-route-ca.pem").read_text() == fixture["route"]["destinationCA"]
    docs = [d for d in yaml.safe_load_all((pipe_tree / "dist/agent-rendered.yaml").read_text()) if d]
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert {("Deployment", "rag-openwebui"), ("Route", "rag-openwebui")} <= kinds


@pytest.mark.parametrize("stage", ["preview", "live-deploy"])
def test_operator_path_without_site_inputs_still_refuses_with_the_opt_out(pipe_tree, stage):
    task = _task_tree(pipe_tree)
    helm_log = pipe_tree / "helm.log"
    empty_env = pipe_tree / "empty.env"
    empty_env.write_text("")
    operator = {"AIRGAP_ENV": str(empty_env), "IMAGE_SHA": IMAGE_SHA, "INTERNAL_REGISTRY": "reg.internal:5000",
                "STORAGE_CLASS": "standard", "EMBED_MODEL": "embed-v1", "DENSE_DIM": "768",
                "EMBED_MODEL_REVISION": "rev-1", "EMBED_BASE_URL": "http://vllm:8000/v1",
                "HELM_LOG": str(helm_log)}
    if stage == "preview":
        r = _run_task(pipe_tree, task, "airgap:pipeline", "AIRGAP_DRYRUN=1", extra_env=operator)
    else:
        # A live deploy reads the namespace service CA first (read-only); answer it
        # so the run reaches the site-input check that this test is about.
        write_stub(pipe_tree / "bin" / "kubectl", "#!/bin/sh\ncase \"$*\" in *openshift-service-ca.crt*)\n"
                   "printf -- '-----BEGIN CERTIFICATE-----\\nX\\n-----END CERTIFICATE-----\\n' ;; esac\nexit 0\n")
        r = _run_task(pipe_tree, task, "airgap:deploy", extra_env=operator)
    assert r.returncode != 0
    assert "Open WebUI is enabled by default (ADR-0008) but required inputs are missing" in r.stderr
    assert "opt out with OPENWEBUI_ENABLED=false" in r.stderr
    assert "STAGE 3/5" not in r.stdout
    assert not helm_log.exists() or "upgrade" not in helm_log.read_text().split()
