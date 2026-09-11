"""scripts/airgap/ingest.sh fail-close and operability tests (issue #15).

Hermetic tests: tests dry-run rendering of the prod ingest Job manifest,
NFS storage refusal, INGEST_WORKERS, contextual embed placeholders,
PULL_SECRET wiring, and strategic merge patches without a cluster.
"""

import re

import pytest

from tests.helpers_airgap import (
    assert_no_placeholders,
    assert_pull_secret_wired,
    make_bin_tree,
    run_sh,
    write_stub,
)

IMAGE_SHA = "d" * 40

STUB_BIN = """#!/bin/sh
if [ "$1" = "kustomize" ] || [ "$1" = "build" ]; then
  cat {stub_yaml}
  exit 0
fi
if [ "$1" = "patch" ]; then
  # Local strategic merge patch stub
  cat {stub_patched_yaml}
  exit 0
fi
printf '%s\\n' "$@" >> "$KC_LOG"
exit 0
"""

STUB_INGEST_KUSTOMIZE = """apiVersion: batch/v1
kind: Job
metadata:
  name: ingest
  namespace: mainframe-rag
spec:
  template:
    spec:
      imagePullSecrets: []
      containers:
        - name: ingest
          image: __INTERNAL_REGISTRY__/qdrant-pdf-rag-ingest:__IMAGE_SHA__
          args: ["--src", "/corpus", "--progress", "/work/inventory.jsonl"]
          env:
            - name: QDRANT_URL
              value: __QDRANT_URL__
            - name: QDRANT_COLLECTION
              value: mainframe_manuals
            - name: QDRANT_API_KEY
              valueFrom:
                secretKeyRef:
                  name: __QDRANT_RELEASE__-apikey
                  key: api-key
            - name: EMBED_BASE_URL
              value: __EMBED_BASE_URL__
            - name: EMBED_MODEL
              value: __EMBED_MODEL__
            - name: DENSE_DIM
              value: __DENSE_DIM__
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: __OTEL_EXPORTER_OTLP_ENDPOINT__
            - name: OTEL_SERVICE_NAME
              value: mainframe-rag-ingest
            - name: OTEL_DEPLOYMENT_ENVIRONMENT
              value: __OTEL_DEPLOYMENT_ENVIRONMENT__
            - name: INGEST_WORKERS
              value: "__INGEST_WORKERS__"
            - name: CONTEXTUAL_EMBED_ENABLED
              value: "__CONTEXTUAL_EMBED_ENABLED__"
            - name: CONTEXT_LLM_BASE_URL
              value: "__CONTEXT_LLM_BASE_URL__"
            - name: CONTEXT_LLM_MODEL
              value: "__CONTEXT_LLM_MODEL__"
            - name: EMBED_API_KEY
              valueFrom:
                secretKeyRef:
                  key: embed-api-key
                  name: __GATEWAY_API_KEY_SECRET__
            - name: CONTEXT_LLM_API_KEY
              valueFrom:
                secretKeyRef:
                  key: context-llm-api-key
                  name: __GATEWAY_API_KEY_SECRET__
      volumes:
        - name: corpus
          persistentVolumeClaim:
            claimName: __CORPUS_PVC__
"""


@pytest.fixture
def ingest_tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "ingest.sh"])
    (tmp_path / "deploy" / "kustomize" / "overlays" / "openshift-ingest").mkdir(
        parents=True, exist_ok=True
    )
    stub_yaml = tmp_path / "stub-ingest.yaml"
    stub_yaml.write_text(STUB_INGEST_KUSTOMIZE)
    stub_patched_yaml = tmp_path / "stub-patched-ingest.yaml"
    stub_patched_yaml.write_text(STUB_INGEST_KUSTOMIZE.replace("cpu: 4", "cpu: 1"))
    kc_log = tmp_path / "kc.log"
    for name in ("kubectl", "oc", "kustomize"):
        write_stub(
            tmp_path / "bin" / name,
            STUB_BIN.format(stub_yaml=stub_yaml, stub_patched_yaml=stub_patched_yaml),
        )
    return tmp_path, kc_log


def _run_ingest(tree, *extra_env):
    tmp_path, kc_log = tree
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "KC_LOG": str(kc_log),
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
        "NAMESPACE": "test-ns",
        "STORAGE_CLASS": "gp3-csi",
        "CORPUS_PVC": "my-manuals-pvc",
        "EMBED_MODEL": "test-embed",
        "DENSE_DIM": "768",
        "VLLM_BASE_URL": "http://vllm:8000/v1",
        "AIRGAP_DRYRUN": "1",
    }
    for k, v in extra_env:
        env[k] = v
    return run_sh(tmp_path / "scripts" / "airgap" / "ingest.sh", env, tmp_path)


def test_ingest_dryrun_renders_clean_manifest(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert_no_placeholders(rendered)
    assert "reg.internal:5000/qdrant-pdf-rag-ingest:" in rendered
    assert "claimName: my-manuals-pvc" in rendered
    assert 'value: "4"' in rendered or "value: 4" in rendered
    assert "value: http://vllm:8000/v1" in rendered


def test_ingest_dryrun_custom_workers(ingest_tree):
    r = _run_ingest(ingest_tree, ("INGEST_WORKERS", "8"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert 'value: "8"' in rendered or "value: 8" in rendered


def test_ingest_dryrun_contextual_embed_propagation(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        ("CONTEXTUAL_EMBED_ENABLED", "true"),
        ("CONTEXT_LLM_BASE_URL", "http://context-llm:8000/v1"),
        ("CONTEXT_LLM_MODEL", "meta-llama/Llama-3-8B"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert 'value: "true"' in rendered or "value: true" in rendered
    assert 'value: "http://context-llm:8000/v1"' in rendered or "value: http://context-llm:8000/v1" in rendered
    assert 'value: "meta-llama/Llama-3-8B"' in rendered or "value: meta-llama/Llama-3-8B" in rendered


def test_ingest_otel_off_by_default(ingest_tree):
    # Unset endpoint renders empty = tracing off; the service name is fixed
    # so the Job can never merge into the agent service in one Jaeger.
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert re.search(r"OTEL_EXPORTER_OTLP_ENDPOINT\n\s+value:\s*$", rendered, re.MULTILINE)
    assert "value: mainframe-rag-ingest" in rendered


def test_ingest_otel_endpoint_and_environment_wired(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"),
        ("OTEL_DEPLOYMENT_ENVIRONMENT", "prod"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "value: http://jaeger:4318" in rendered
    assert "value: prod" in rendered
    assert_no_placeholders(rendered)


def test_ingest_pull_secret_wired_when_set(ingest_tree):
    r = _run_ingest(ingest_tree, ("PULL_SECRET", "custom-registry-secret"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "name: custom-registry-secret" in rendered
    # The wired item must stay inside the pod-spec mapping: a fixed 2-space
    # insert broke out of it and kubectl rejected the manifest
    # ("did not find expected key" in the Kind rehearsal).
    assert_pull_secret_wired(rendered, "custom-registry-secret")


def test_ingest_pull_secret_bad_name_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("PULL_SECRET", "Bad_Name!"))
    assert r.returncode != 0
    assert "PULL_SECRET must be a DNS-subdomain name" in r.stderr


def test_ingest_pull_secret_stays_empty_when_unset(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "imagePullSecrets: []" in rendered
    assert "name: custom-registry-secret" not in rendered


def test_ingest_refuses_nfs_storage(ingest_tree):
    r = _run_ingest(ingest_tree, ("STORAGE_CLASS", "nfs-storage-class"))
    assert r.returncode == 1
    assert "looks like NFS" in r.stderr


def test_ingest_missing_corpus_pvc_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("CORPUS_PVC", ""))
    assert r.returncode == 1
    assert "required variables unset: CORPUS_PVC" in r.stderr


def test_ingest_cli_corpus_pvc_beats_env_file(ingest_tree):
    env_file = ingest_tree[0] / "case.env"
    env_file.write_text("CORPUS_PVC=file-pvc-should-lose\n")
    r = _run_ingest(ingest_tree, ("AIRGAP_ENV", str(env_file)))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "claimName: my-manuals-pvc" in rendered
    assert "file-pvc-should-lose" not in rendered


def test_ingest_gateway_keys_off_strips_secret_block(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    # No gateway secret reference: the only secretKeyRef left is the
    # chart-managed QDRANT_API_KEY. The neighboring plain entries and the
    # volumes section survive the strip.
    assert rendered.count("secretKeyRef") == 1
    assert "EMBED_API_KEY" not in rendered
    assert "CONTEXT_LLM_API_KEY" not in rendered
    assert "RERANK_API_KEY" not in rendered
    assert "CONTEXT_LLM_MODEL" in rendered
    assert "volumes:" in rendered
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert_no_placeholders(rendered)
    assert "Gateway keys off" in r.stdout


def test_ingest_gateway_keys_wired_when_secret_set(ingest_tree):
    import re

    r = _run_ingest(ingest_tree, ("GATEWAY_API_KEY_SECRET", "gateway-api-keys"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    for env_name, data_key in (
        ("EMBED_API_KEY", "embed-api-key"),
        ("CONTEXT_LLM_API_KEY", "context-llm-api-key"),
    ):
        assert re.search(
            rf"- name: {env_name}\n\s+valueFrom:\n\s+secretKeyRef:\n\s+key: {data_key}\n\s+name: gateway-api-keys",
            rendered,
        ), env_name
    # The ingest Job never touches the reasoning/rerank legs (anchored:
    # CONTEXT_LLM_API_KEY contains LLM_API_KEY as a substring).
    assert not re.search(r"^- name: LLM_API_KEY$", rendered, re.MULTILINE)
    assert "RERANK_API_KEY" not in rendered
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert_no_placeholders(rendered)
    assert "Gateway keys wired" in r.stdout


def test_ingest_gateway_secret_bad_name_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("GATEWAY_API_KEY_SECRET", "Bad_Name!"))
    assert r.returncode == 1
    assert "GATEWAY_API_KEY_SECRET must be a DNS-subdomain name" in r.stderr


def test_ingest_gateway_overlay_block_matches_stub_contract():
    """The stub kustomize above mirrors the real ingest overlay by hand —
    pin the real file to the same contract so the two cannot diverge."""
    from tests.helpers_airgap import REPO

    real = (
        REPO
        / "deploy"
        / "kustomize"
        / "overlays"
        / "openshift-ingest"
        / "ingest-job.yaml"
    ).read_text()
    assert "# gateway-api-keys-begin" in real
    assert "# gateway-api-keys-end" in real
    assert real.index("# gateway-api-keys-begin") < real.index("# gateway-api-keys-end")
    for env_name, data_key in (
        ("EMBED_API_KEY", "embed-api-key"),
        ("CONTEXT_LLM_API_KEY", "context-llm-api-key"),
    ):
        assert f"- name: {env_name}" in real
        assert f"key: {data_key}" in real
    assert "__GATEWAY_API_KEY_SECRET__" in real
