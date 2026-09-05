"""scripts/airgap/deploy.sh fail-close + knob regressions (issue #15 / PR #32).

Runs deploy.sh with AIRGAP_DRYRUN=1 against a stubbed bin dir and a copied
tree — no cluster, no helm, no network. The PULL_SECRET fail-close is the
regression the rehearsal build surfaced: values.yaml's placeholder pull-secret
name must never reach a cluster.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMAGE_SHA = "a" * 40  # full-sha shaped; deploy.sh only rejects "" / "HEAD"

# Minimal manifest the stub kustomize prints (sed substitutes these).
STUB_KUSTOMIZE = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: rag-agent
  namespace: mainframe-rag
spec:
  template:
    spec:
      imagePullSecrets: []
      containers:
        - name: agent
          image: __INTERNAL_REGISTRY__/qdrant-pdf-rag-agent:__IMAGE_SHA__
          env:
            - name: EMBED_MODEL
              value: __EMBED_MODEL__
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: __OTEL_EXPORTER_OTLP_ENDPOINT__
            - name: RERANK_ENABLED
              value: "__RERANK_ENABLED__"
            - name: RERANK_BASE_URL
              value: "__RERANK_BASE_URL__"
            - name: RERANK_MODEL
              value: "__RERANK_MODEL__"
"""

# Jaeger stub: mirrors the real render's placeholder surface (issue #83).
STUB_JAEGER = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: jaeger
  namespace: mainframe-rag
spec:
  template:
    spec:
      imagePullSecrets: []
      containers:
        - name: jaeger
          image: __INTERNAL_REGISTRY__/jaegertracing/jaeger:v2.20.0
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: jaeger-badger
spec:
  storageClassName: __STORAGE_CLASS__
"""

STUB_BIN = """#!/bin/sh
if [ "$1" = "kustomize" ] || [ "$1" = "build" ]; then
  case "$2" in
    *jaeger*) cat {jaeger_stub} ;;
    *) cat {stub_yaml} ;;
  esac
  exit 0
fi
printf '%s\\n' "$@" >> "$HELM_LOG"
exit 0
"""


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "charts").mkdir()
    (tmp_path / "overlays" / "openshift").mkdir(parents=True)
    (tmp_path / "deploy" / "kustomize").mkdir(parents=True)
    (tmp_path / "scripts" / "airgap").mkdir(parents=True)
    for f in ("common.sh", "deploy.sh"):
        shutil.copy(REPO / "scripts" / "airgap" / f, tmp_path / "scripts" / "airgap" / f)
    shutil.copy(next(REPO.glob("charts/qdrant-*.tgz")), tmp_path / "charts")
    shutil.copy(REPO / "overlays" / "openshift" / "values.yaml", tmp_path / "overlays" / "openshift")
    shutil.copytree(REPO / "deploy" / "kustomize" / "jaeger", tmp_path / "deploy" / "kustomize" / "jaeger")
    stub_yaml = tmp_path / "stub-kustomize.yaml"
    stub_yaml.write_text(STUB_KUSTOMIZE)
    jaeger_stub = tmp_path / "stub-jaeger.yaml"
    jaeger_stub.write_text(STUB_JAEGER)
    helm_log = tmp_path / "helm-args.log"
    for name in ("helm", "kubectl", "oc", "kustomize"):
        p = tmp_path / "bin" / name
        p.write_text(STUB_BIN.format(stub_yaml=stub_yaml, jaeger_stub=jaeger_stub))
        p.chmod(0o755)
    return tmp_path, helm_log


def _run(tree, *extra_env):
    tmp_path, _ = tree
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "HELM_LOG": str(tmp_path / "helm-args.log"),
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal",
        "NAMESPACE": "ns",
        "STORAGE_CLASS": "standard",
        "EMBED_MODEL": "embed",
        "DENSE_DIM": "64",
        "VLLM_BASE_URL": "http://vllm:8000",
    }
    for k, v in extra_env:
        env[k] = v
    return subprocess.run(
        ["sh", str(tmp_path / "scripts" / "airgap" / "deploy.sh")],
        capture_output=True, text=True, env=env, cwd=tmp_path, check=False,
    )


def _helm_log(tree):
    return (tree[1]).read_text()


def test_no_pull_secret_never_renders_placeholder_name(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    log = _helm_log(tree)
    assert "imagePullSecrets=null" in log
    assert "PLACEHOLDER" not in log


def test_pull_secret_wired_when_set(tree):
    _run(tree, ("PULL_SECRET", "ghcr-pull"))
    log = _helm_log(tree)
    assert "imagePullSecrets[0].name=ghcr-pull" in log
    assert "imagePullSecrets=null" not in log


def test_pull_secret_wired_agent_render_keeps_mapping(tree):
    # The wired item must reuse the pod-spec indent: a fixed 2-space insert
    # broke out of the mapping and kubectl rejected agent-rendered.yaml
    # ("did not find expected key") in the Kind rehearsal.
    r = _run(tree, ("PULL_SECRET", "ghcr-pull"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert re.search(
        r"^([ ]*)imagePullSecrets:\n\1  - name: ghcr-pull$",
        rendered,
        re.MULTILINE,
    )


def test_storage_size_knob_covers_persistence_and_snapshot(tree):
    _run(tree, ("QDRANT_STORAGE_SIZE", "1Gi"))
    log = _helm_log(tree)
    assert "persistence.size=1Gi" in log
    assert "snapshotPersistence.size=1Gi" in log


def test_missing_extra_values_file_fails_closed(tree):
    r = _run(tree, ("QDRANT_EXTRA_VALUES", "/nonexistent/vals.yaml"))
    assert r.returncode == 1
    assert "QDRANT_EXTRA_VALUES file not found" in r.stderr


def test_extra_values_file_reaches_helm(tree):
    vals = tree[0] / "vals.yaml"
    vals.write_text("resources: {}\n")
    r = _run(tree, ("QDRANT_EXTRA_VALUES", str(vals)))
    assert r.returncode == 0, r.stderr
    args = _helm_log(tree).splitlines()
    assert str(vals) in args
    assert args[args.index(str(vals)) - 1] == "-f"


def test_rendered_manifest_substituted_and_written(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert "reg.internal/qdrant-pdf-rag-agent" in rendered
    assert "__" not in rendered


# ------------------------------------------------------- Jaeger / tracing (#83)


def test_tracing_off_skips_jaeger_and_keeps_endpoint_empty(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert not (tree[0] / "dist" / "jaeger-rendered.yaml").exists()
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # Endpoint env var always rendered; empty value = tracing off (fail-closed).
    assert re.search(r"OTEL_EXPORTER_OTLP_ENDPOINT\n\s+value:\s*$", rendered, re.MULTILINE)
    assert "Tracing off" in r.stdout


def test_tracing_enabled_deploys_jaeger_and_wires_endpoint(tree):
    r = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"))
    assert r.returncode == 0, r.stderr
    jaeger = (tree[0] / "dist" / "jaeger-rendered.yaml").read_text()
    agent = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert "reg.internal/jaegertracing/jaeger:v2.20.0" in jaeger
    assert "storageClassName: standard" in jaeger
    assert "namespace: ns" in jaeger
    assert "__" not in jaeger
    assert 'value: http://jaeger:4318' in agent
    assert "__" not in agent


def test_tracing_jaeger_pull_secret_wired_when_set(tree):
    r = _run(
        tree,
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"),
        ("PULL_SECRET", "ghcr-pull"),
    )
    assert r.returncode == 0, r.stderr
    jaeger = (tree[0] / "dist" / "jaeger-rendered.yaml").read_text()
    assert "name: ghcr-pull" in jaeger
    assert re.search(
        r"^([ ]*)imagePullSecrets:\n\1  - name: ghcr-pull$",
        jaeger,
        re.MULTILINE,
    )


def test_tracing_jaeger_pull_secret_stays_absent_when_unset(tree):
    r = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"))
    assert r.returncode == 0, r.stderr
    jaeger = (tree[0] / "dist" / "jaeger-rendered.yaml").read_text()
    assert "imagePullSecrets: []" in jaeger
    assert "name: ghcr-pull" not in jaeger


def test_reranker_defaults_off(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert 'value: "false"' in rendered or "value: false" in rendered
    assert "__RERANK_" not in rendered


def test_reranker_configured_when_enabled(tree):
    r = _run(
        tree,
        ("RERANK_ENABLED", "true"),
        ("RERANK_BASE_URL", "http://rerank:8002/v1"),
        ("RERANK_MODEL", "my-reranker-model"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert 'value: "true"' in rendered or "value: true" in rendered
    assert 'value: "http://rerank:8002/v1"' in rendered or "value: http://rerank:8002/v1" in rendered
    assert 'value: "my-reranker-model"' in rendered or "value: my-reranker-model" in rendered
    assert "__RERANK_" not in rendered
