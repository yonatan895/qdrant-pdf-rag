"""scripts/airgap/ingest.sh fail-close and operability tests (issue #15).

Hermetic tests: tests dry-run rendering of the prod ingest Job manifest,
NFS storage refusal, INGEST_WORKERS, contextual embed placeholders,
PULL_SECRET wiring, and strategic merge patches without a cluster.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
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
            - name: INGEST_WORKERS
              value: "__INGEST_WORKERS__"
            - name: CONTEXTUAL_EMBED_ENABLED
              value: "__CONTEXTUAL_EMBED_ENABLED__"
            - name: CONTEXT_LLM_BASE_URL
              value: "__CONTEXT_LLM_BASE_URL__"
            - name: CONTEXT_LLM_MODEL
              value: "__CONTEXT_LLM_MODEL__"
      volumes:
        - name: corpus
          persistentVolumeClaim:
            claimName: __CORPUS_PVC__
"""


@pytest.fixture
def ingest_tree(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "scripts" / "airgap").mkdir(parents=True)
    (tmp_path / "deploy" / "kustomize" / "overlays" / "openshift-ingest").mkdir(parents=True)
    for f in ("common.sh", "ingest.sh"):
        shutil.copy(REPO / "scripts" / "airgap" / f, tmp_path / "scripts" / "airgap" / f)
    stub_yaml = tmp_path / "stub-ingest.yaml"
    stub_yaml.write_text(STUB_INGEST_KUSTOMIZE)
    stub_patched_yaml = tmp_path / "stub-patched-ingest.yaml"
    stub_patched_yaml.write_text(STUB_INGEST_KUSTOMIZE.replace("cpu: 4", "cpu: 1"))
    kc_log = tmp_path / "kc.log"
    for name in ("kubectl", "oc", "kustomize"):
        p = tmp_path / "bin" / name
        p.write_text(
            STUB_BIN.format(stub_yaml=stub_yaml, stub_patched_yaml=stub_patched_yaml)
        )
        p.chmod(0o755)
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
    return subprocess.run(
        ["sh", str(tmp_path / "scripts" / "airgap" / "ingest.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )


def test_ingest_dryrun_renders_clean_manifest(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "__" not in rendered
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


def test_ingest_pull_secret_wired_when_set(ingest_tree):
    r = _run_ingest(ingest_tree, ("PULL_SECRET", "custom-registry-secret"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "name: custom-registry-secret" in rendered


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
    assert "required variable CORPUS_PVC is unset" in r.stderr
