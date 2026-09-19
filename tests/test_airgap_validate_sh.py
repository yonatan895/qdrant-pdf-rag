"""scripts/airgap/validate.sh pre-flight validation tests (issue #15).

Tests validate.sh in dry-run mode against hermetic stubs:
- Required env vars fail-closed when unset.
- Format checks: positive integer DENSE_DIM, http/https VLLM_BASE_URL.
- Storage class checks: NFS-looking names refused.
- Missing CLI tools fail-closed.
- Clean configuration exits 0.
"""

import os
import shutil

import pytest

from tests.helpers_airgap import (
    REPO,
    STUB_TOOL,
    copy_chart,
    make_bin_tree,
    run_sh,
    symlink_tools,
    write_stub,
)

IMAGE_SHA = "b" * 40


@pytest.fixture
def tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "validate.sh"])
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

    for name in ("skopeo", "helm", "kubectl", "oc"):
        write_stub(tmp_path / "bin" / name, STUB_TOOL)
    return tmp_path


def _run(tree, extra_env=None):
    env = {
        "PATH": f"{tree / 'bin'}:/usr/bin:/bin",
        "AIRGAP_DRYRUN": "1",
        "AIRGAP_ENV": "/dev/null",  # ignore repo ./airgap.env
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
        "NAMESPACE": "mainframe-rag",
        "STORAGE_CLASS": "standard",
        "EMBED_MODEL": "ibm-granite/granite-embedding-125m-english",
        "DENSE_DIM": "768",
        "EMBED_MODEL_REVISION": "rev-1",
        "VLLM_BASE_URL": "http://vllm:8000/v1",
        # Issue #360: fixtures select the explicit single-node 1/1/1 profile;
        # the production preset is exercised by its own test below.
        "QDRANT_SHARD_NUMBER": "1",
        "QDRANT_REPLICATION_FACTOR": "1",
        "QDRANT_WRITE_CONSISTENCY_FACTOR": "1",
    }
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return run_sh(tree / "scripts" / "airgap" / "validate.sh", env, tree)


def test_validate_clean_exits_zero(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert "SUCCESS: Pre-flight validation passed (dry-run mode)." in r.stdout


def test_validate_reports_selected_policy(tree):
    r = _run(tree)
    assert "Collection policy: S=1 RF=1 W=1" in r.stdout


def test_validate_production_preset_supplies_tuple(tree):
    """Issue #360: with no explicit selection the checked-in production
    preset supplies 6/3/2 to preflight (and therefore to the ingest render)."""
    target = tree / "overlays" / "openshift"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "overlays" / "openshift" / "collection-policy.env", target)
    r = _run(
        tree,
        {
            "QDRANT_SHARD_NUMBER": None,
            "QDRANT_REPLICATION_FACTOR": None,
            "QDRANT_WRITE_CONSISTENCY_FACTOR": None,
        },
    )
    assert r.returncode == 0, r.stderr
    assert "Collection policy: S=6 RF=3 W=2" in r.stdout


def test_validate_partial_policy_fails_closed(tree):
    r = _run(tree, {"QDRANT_REPLICATION_FACTOR": None})
    assert r.returncode == 1
    assert "collection distribution policy is incomplete" in r.stderr
    assert "QDRANT_REPLICATION_FACTOR" in r.stderr


def test_validate_write_above_replication_fails_closed(tree):
    r = _run(
        tree,
        {
            "QDRANT_SHARD_NUMBER": "6",
            "QDRANT_REPLICATION_FACTOR": "2",
            "QDRANT_WRITE_CONSISTENCY_FACTOR": "3",
        },
    )
    assert r.returncode == 1
    assert "exceeds" in r.stderr


def test_validate_agent_write_key_fails_closed(tree):
    """Issue #366: a prod agent overlay wiring the full-access Qdrant key
    must fail preflight before anything reaches the cluster."""
    overlay = (
        tree / "deploy" / "kustomize" / "overlays" / "openshift" / "agent-prod-patch.yaml"
    )
    overlay.write_text(overlay.read_text().replace("key: read-only-api-key", "key: api-key"))
    r = _run(tree)
    assert r.returncode != 0
    assert "read-only-api-key" in r.stderr


def test_validate_ingest_readonly_key_fails_closed(tree):
    """Issue #366 mirror: downgrading the ingest overlay to read-only must
    also fail preflight — ingest owns corpus mutation."""
    overlay = (
        tree
        / "deploy"
        / "kustomize"
        / "overlays"
        / "openshift-ingest"
        / "ingest-job.yaml"
    )
    lines = [
        "key: read-only-api-key" if line.strip() == "key: api-key" else line
        for line in overlay.read_text().splitlines()
    ]
    overlay.write_text("\n".join(lines) + "\n")
    r = _run(tree)
    assert r.returncode != 0
    assert "to api-key" in r.stderr


def test_validate_ingest_copresent_readonly_key_fails_closed(tree):
    """Anti-revert parity with the agent gate: a co-present read-only key
    in the ingest overlay fails preflight even with api-key present."""
    overlay = (
        tree
        / "deploy"
        / "kustomize"
        / "overlays"
        / "openshift-ingest"
        / "ingest-job.yaml"
    )
    text = overlay.read_text()
    anchor = "key: api-key\n"
    assert anchor in text
    overlay.write_text(
        text.replace(
            anchor,
            anchor
            + "            - name: QDRANT_READ_API_KEY\n"
            + "              valueFrom:\n"
            + "                secretKeyRef:\n"
            + "                  key: read-only-api-key\n"
            + "                  name: __QDRANT_RELEASE__-apikey\n",
        )
    )
    r = _run(tree)
    assert r.returncode != 0
    assert "read-only" in r.stderr


def test_validate_tracing_on_by_default(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert "Tracing:           ON (http://jaeger:4318)" in r.stdout


def test_validate_tracing_off_sentinel(tree):
    r = _run(tree, {"OTEL_EXPORTER_OTLP_ENDPOINT": "off"})
    assert r.returncode == 0, r.stderr
    assert "Tracing:           OFF" in r.stdout


def test_validate_tracing_bad_endpoint_fails_closed(tree):
    r = _run(tree, {"OTEL_EXPORTER_OTLP_ENDPOINT": "jaeger:4318"})
    assert r.returncode != 0
    assert "must be http(s) or off" in r.stderr


def test_validate_missing_registry_fails(tree):
    r = _run(tree, {"INTERNAL_REGISTRY": None, "REGISTRY_INTERNAL": None})
    assert r.returncode != 0
    assert "INTERNAL_REGISTRY" in r.stderr and "FAIL:" in r.stderr


def test_validate_lists_all_missing_vars_at_once(tree):
    r = _run(tree, {"INTERNAL_REGISTRY": None, "REGISTRY_INTERNAL": None, "STORAGE_CLASS": None})
    assert r.returncode != 0
    assert "INTERNAL_REGISTRY" in r.stderr
    assert "STORAGE_CLASS" in r.stderr


def test_validate_nfs_storage_refused(tree):
    r = _run(tree, {"STORAGE_CLASS": "nfs-client"})
    assert r.returncode != 0
    assert "looks like NFS" in r.stderr


def test_validate_dense_dim_non_integer_refused(tree):
    r = _run(tree, {"DENSE_DIM": "not-a-number"})
    assert r.returncode != 0
    assert "DENSE_DIM must be a positive integer" in r.stderr


def test_validate_dense_dim_zero_refused(tree):
    r = _run(tree, {"DENSE_DIM": "0"})
    assert r.returncode != 0
    assert "DENSE_DIM must be greater than 0" in r.stderr


def test_validate_missing_embed_revision_fails(tree):
    """Issue #391 F1: vllm mode refuses a blank attestation at startup, so
    a missing revision must fail pre-flight, not crash-loop the pods."""
    r = _run(tree, {"EMBED_MODEL_REVISION": None})
    assert r.returncode != 0
    assert "EMBED_MODEL_REVISION" in r.stderr
    assert "required variables unset" in r.stderr


def test_validate_whitespace_embed_revision_fails(tree):
    r = _run(tree, {"EMBED_MODEL_REVISION": "   "})
    assert r.returncode != 0
    assert "EMBED_MODEL_REVISION must be a non-blank" in r.stderr


def test_validate_embed_revision_echoed(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert "EMBED_MODEL_REVISION: rev-1" in r.stdout


def test_validate_rerank_endpoint_order_bad_value_refused(tree):
    r = _run(tree, {"RERANK_ENDPOINT_ORDER": "bogus"})
    assert r.returncode != 0
    assert "RERANK_ENDPOINT_ORDER must be score_first or rerank_first" in r.stderr


def test_validate_rerank_endpoint_order_rerank_first_accepted(tree):
    r = _run(tree, {"RERANK_ENDPOINT_ORDER": "rerank_first"})
    assert r.returncode == 0, r.stderr


def test_validate_vllm_url_bad_scheme_refused(tree):
    r = _run(tree, {"VLLM_BASE_URL": "ftp://vllm:8000"})
    assert r.returncode != 0
    assert "VLLM_BASE_URL must begin with http:// or https://" in r.stderr


@pytest.mark.parametrize(
    "var", ["EMBED_BASE_URL", "LLM_BASE_URL", "RERANK_BASE_URL", "CONTEXT_LLM_BASE_URL"]
)
def test_validate_optional_model_url_bad_scheme_refused(tree, var):
    r = _run(tree, {var: "gateway.internal:4000/v1"})
    assert r.returncode != 0
    assert f"{var} must begin with http:// or https://" in r.stderr


def test_validate_missing_skopeo_fails(tree):
    os.remove(tree / "bin" / "skopeo")
    symlink_tools(tree, ("sh", "dirname", "awk", "sed", "head", "ls"))
    r = _run(tree, {"PATH": str(tree / "bin")})
    assert r.returncode != 0
    assert "skopeo is required" in r.stderr


POISON_ENV_FILE = """\
INTERNAL_REGISTRY=wrong.invalid:5000
NAMESPACE=file-ns
STORAGE_CLASS=nfs-client
EMBED_MODEL=file-model
EMBED_MODEL_REVISION=file-rev-should-lose
DENSE_DIM=not-a-number
VLLM_BASE_URL=ftp://file-vllm:8000
GATEWAY_API_KEY_SECRET=file-secret-should-lose
"""

VALID_ENV_FILE = """\
INTERNAL_REGISTRY=reg.internal:5000
NAMESPACE=mainframe-rag
STORAGE_CLASS=standard
EMBED_MODEL=ibm-granite/granite-embedding-125m-english
EMBED_MODEL_REVISION=file-rev-1
DENSE_DIM=768
VLLM_BASE_URL=http://vllm:8000/v1
"""


def _write_env_file(tree, content):
    p = tree / "case.env"
    p.write_text(content)
    return str(p)


def test_explicit_env_beats_env_file(tree):
    # Every file value here would fail validation on its own (NFS storage,
    # non-http URL, non-integer dim); exit 0 proves the explicit environment
    # won on all keys. The secret name is charset-valid either way, so
    # precedence is pinned by echoing the winner instead.
    r = _run(
        tree,
        {
            "AIRGAP_ENV": _write_env_file(tree, POISON_ENV_FILE),
            "GATEWAY_API_KEY_SECRET": "explicit-secret",
        },
    )
    assert r.returncode == 0, r.stderr
    assert "SUCCESS: Pre-flight validation passed (dry-run mode)." in r.stdout
    assert "GATEWAY_API_KEY_SECRET: explicit-secret" in r.stdout
    assert "file-secret-should-lose" not in r.stdout
    assert "file-rev-should-lose" not in r.stdout


def test_env_file_still_feeds_unset_vars(tree):
    r = _run(
        tree,
        {
            "AIRGAP_ENV": _write_env_file(tree, VALID_ENV_FILE),
            "INTERNAL_REGISTRY": None,
            "REGISTRY_INTERNAL": None,
            "NAMESPACE": None,
            "STORAGE_CLASS": None,
            "EMBED_MODEL": None,
            "EMBED_MODEL_REVISION": None,
            "DENSE_DIM": None,
            "VLLM_BASE_URL": None,
        },
    )
    assert r.returncode == 0, r.stderr


def test_empty_env_value_leaves_file_value(tree):
    # Empty is unset everywhere (${VAR:-} idiom): the file still feeds the key.
    r = _run(
        tree,
        {"AIRGAP_ENV": _write_env_file(tree, VALID_ENV_FILE), "INTERNAL_REGISTRY": ""},
    )
    assert r.returncode == 0, r.stderr


def test_taskfiles_do_not_load_airgap_env():
    # Locks the precedence fix at its new locus (issue #402 D): no Taskfile
    # may declare a `dotenv:` that loads the executable airgap.env file —
    # a task-level load would silently override explicit caller values with
    # stale file values. Scripts source the file themselves (see common.sh
    # OPERATOR_ENV_KEYS); Task passes per-task bridges only.
    taskfiles = [REPO / "Taskfile.yml", *sorted((REPO / "taskfiles").glob("*.yml"))]
    assert len(taskfiles) >= 7, [p.name for p in taskfiles]
    offenders = [p for p in taskfiles
                 if any("dotenv" in ln
                        for ln in p.read_text(encoding="utf-8").splitlines()
                        if not ln.lstrip().startswith("#"))]
    assert offenders == [], (
        "no Taskfile may load operator configuration via dotenv: "
        f"{[p.name for p in offenders]}"
    )


# ------------------------------------------------------- gateway keys (LiteLLM)


@pytest.mark.parametrize(
    "var", ["LLM_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY", "CONTEXT_LLM_API_KEY"]
)
def test_validate_refuses_plaintext_gateway_key_in_env_file(tree, var):
    r = _run(tree, {"AIRGAP_ENV": _write_env_file(tree, f"{var}=sk-plaintext-must-die\n")})
    assert r.returncode != 0
    assert "plaintext gateway virtual key" in r.stderr


def test_validate_allows_empty_and_commented_gateway_keys(tree):
    r = _run(
        tree,
        {"AIRGAP_ENV": _write_env_file(tree, "LLM_API_KEY=\n#EMBED_API_KEY=sk-commented\n")},
    )
    assert r.returncode == 0, r.stderr


def test_validate_gateway_secret_bad_name_fails_closed(tree):
    r = _run(tree, {"GATEWAY_API_KEY_SECRET": "Bad_Name!"})
    assert r.returncode != 0
    assert "GATEWAY_API_KEY_SECRET must be a DNS-subdomain name" in r.stderr


@pytest.mark.parametrize("bad_name", ["Bad_Name!", "a&b", "a|b"])
def test_validate_pull_secret_bad_name_fails_closed(tree, bad_name):
    # Same gate as the gateway secret name: sed-active chars (&, |) would
    # silently rewrite the manifest via wire_pull_secret / Helm --set.
    r = _run(tree, {"PULL_SECRET": bad_name})
    assert r.returncode != 0
    assert "PULL_SECRET must be a DNS-subdomain name" in r.stderr


def test_validate_live_verifies_gateway_secret(tree):
    # Non-dry-run against all-zero stubs: namespace + Secret resolve.
    r = _run(tree, {"AIRGAP_DRYRUN": "0", "GATEWAY_API_KEY_SECRET": "gateway-api-keys"})
    assert r.returncode == 0, r.stderr
    assert "Gateway key Secret 'gateway-api-keys' verified" in r.stdout


def test_validate_live_missing_gateway_secret_fails(tree):
    write_stub(
        tree / "bin" / "kubectl",
        "#!/bin/sh\nfor arg in \"$@\"; do\n  if [ \"$arg\" = \"secret\" ]; then exit 1; fi\ndone\nexit 0\n",
    )
    r = _run(tree, {"AIRGAP_DRYRUN": "0", "GATEWAY_API_KEY_SECRET": "gateway-api-keys"})
    assert r.returncode != 0
    assert "Secret 'gateway-api-keys' not found" in r.stderr


def test_validate_live_missing_namespace_notices_secret(tree):
    write_stub(
        tree / "bin" / "kubectl",
        "#!/bin/sh\nfor arg in \"$@\"; do\n  if [ \"$arg\" = \"namespace\" ]; then exit 1; fi\ndone\nexit 0\n",
    )
    r = _run(tree, {"AIRGAP_DRYRUN": "0", "GATEWAY_API_KEY_SECRET": "gateway-api-keys"})
    assert r.returncode == 0, r.stderr
    assert "does not exist yet" in r.stdout
