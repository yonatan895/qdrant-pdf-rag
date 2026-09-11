"""scripts/airgap/deploy.sh fail-close + knob regressions (issue #15 / PR #32).

Runs deploy.sh with AIRGAP_DRYRUN=1 against a stubbed bin dir and a copied
tree — no cluster, no helm, no network. The PULL_SECRET fail-close is the
regression the rehearsal build surfaced: values.yaml's placeholder pull-secret
name must never reach a cluster.
"""

import re
import shutil

import pytest

from tests.helpers_airgap import (
    REPO,
    assert_no_placeholders,
    assert_pull_secret_wired,
    copy_chart,
    make_bin_tree,
    run_sh,
)

IMAGE_SHA = "a" * 40  # full-sha shaped; deploy.sh only rejects "" / "HEAD"

# Minimal manifest the stub kustomize prints (sed substitutes these). Shaped
# like real `kubectl kustomize` output: no comments, mapping keys sorted
# (secretKeyRef key before name) — the strip logic must work on this shape,
# not on the overlay source shape.
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
            - name: IMAGE_SHA
              value: __IMAGE_SHA__
            - name: OTEL_DEPLOYMENT_ENVIRONMENT
              value: __OTEL_DEPLOYMENT_ENVIRONMENT__
            - name: OTEL_SERVICE_NAME
              value: __OTEL_SERVICE_NAME__
            - name: METRICS_ENABLED
              value: "__METRICS_ENABLED__"
            - name: RERANK_ENABLED
              value: "__RERANK_ENABLED__"
            - name: RERANK_BASE_URL
              value: "__RERANK_BASE_URL__"
            - name: RERANK_MODEL
              value: "__RERANK_MODEL__"
            - name: RERANK_ENDPOINT_ORDER
              value: __RERANK_ENDPOINT_ORDER__
            - name: LLM_API_KEY
              valueFrom:
                secretKeyRef:
                  key: llm-api-key
                  name: __GATEWAY_API_KEY_SECRET__
            - name: EMBED_API_KEY
              valueFrom:
                secretKeyRef:
                  key: embed-api-key
                  name: __GATEWAY_API_KEY_SECRET__
            - name: RERANK_API_KEY
              valueFrom:
                secretKeyRef:
                  key: rerank-api-key
                  name: __GATEWAY_API_KEY_SECRET__
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

# ServiceMonitor stub: mirrors the real render's placeholder surface (issue
# #187) — namespace rewritten by deploy.sh, no images or storage.
STUB_SERVICEMONITOR = """apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: rag-agent
  namespace: mainframe-rag
spec:
  endpoints:
    - port: http
      path: /metrics
"""

STUB_BIN = """#!/bin/sh
if [ "$1" = "kustomize" ] || [ "$1" = "build" ]; then
  case "$2" in
    *jaeger*) cat {jaeger_stub} ;;
    *servicemonitor*) cat {servicemonitor_stub} ;;
    *) cat {stub_yaml} ;;
  esac
  exit 0
fi
printf '%s\\n' "$@" >> "$HELM_LOG"
exit 0
"""


@pytest.fixture
def tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "deploy.sh"])
    (tmp_path / "overlays" / "openshift").mkdir(parents=True, exist_ok=True)
    (tmp_path / "deploy" / "kustomize").mkdir(parents=True, exist_ok=True)
    copy_chart(tmp_path)
    shutil.copy(REPO / "overlays" / "openshift" / "values.yaml", tmp_path / "overlays" / "openshift")
    shutil.copytree(REPO / "deploy" / "kustomize" / "jaeger", tmp_path / "deploy" / "kustomize" / "jaeger")
    shutil.copytree(REPO / "deploy" / "kustomize" / "servicemonitor", tmp_path / "deploy" / "kustomize" / "servicemonitor")
    stub_yaml = tmp_path / "stub-kustomize.yaml"
    stub_yaml.write_text(STUB_KUSTOMIZE)
    jaeger_stub = tmp_path / "stub-jaeger.yaml"
    jaeger_stub.write_text(STUB_JAEGER)
    servicemonitor_stub = tmp_path / "stub-servicemonitor.yaml"
    servicemonitor_stub.write_text(STUB_SERVICEMONITOR)
    helm_log = tmp_path / "helm-args.log"
    for name in ("helm", "kubectl", "oc", "kustomize"):
        p = tmp_path / "bin" / name
        p.write_text(STUB_BIN.format(stub_yaml=stub_yaml, jaeger_stub=jaeger_stub, servicemonitor_stub=servicemonitor_stub))
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
    return run_sh(tmp_path / "scripts" / "airgap" / "deploy.sh", env, tmp_path)


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
    assert_pull_secret_wired(rendered, "ghcr-pull")


@pytest.mark.parametrize("bad_name", ["Bad_Name!", "a&b", "a|b"])
def test_pull_secret_bad_name_fails_closed(tree, bad_name):
    r = _run(tree, ("PULL_SECRET", bad_name))
    assert r.returncode != 0
    assert "PULL_SECRET must be a DNS-subdomain name" in r.stderr


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
    assert_no_placeholders(rendered)


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
    assert_no_placeholders(jaeger)
    assert 'value: http://jaeger:4318' in agent
    assert_no_placeholders(agent)


def test_tracing_jaeger_pull_secret_wired_when_set(tree):
    r = _run(
        tree,
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"),
        ("PULL_SECRET", "ghcr-pull"),
    )
    assert r.returncode == 0, r.stderr
    jaeger = (tree[0] / "dist" / "jaeger-rendered.yaml").read_text()
    assert "name: ghcr-pull" in jaeger
    assert_pull_secret_wired(jaeger, "ghcr-pull")


def test_tracing_jaeger_pull_secret_stays_absent_when_unset(tree):
    r = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"))
    assert r.returncode == 0, r.stderr
    jaeger = (tree[0] / "dist" / "jaeger-rendered.yaml").read_text()
    assert "imagePullSecrets: []" in jaeger
    assert "name: ghcr-pull" not in jaeger


# ------------------------------------------------------- deploy identity (Phase 2b)


def test_deploy_identity_version_always_rendered(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # service.version is the packed SHA: always set, deploy.sh fail-closes
    # on empty/HEAD before rendering.
    assert re.search(r"IMAGE_SHA\n\s+value: " + IMAGE_SHA, rendered, re.MULTILINE)


def test_deploy_identity_environment_empty_by_default(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # Optional: bare `value:` renders and the agent omits the attribute —
    # same empty-renders-bare convention as the OTEL endpoint above.
    assert re.search(r"OTEL_DEPLOYMENT_ENVIRONMENT\n\s+value:\s*$", rendered, re.MULTILINE)


def test_deploy_identity_environment_wired_when_set(tree):
    r = _run(tree, ("OTEL_DEPLOYMENT_ENVIRONMENT", "lab"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert re.search(r"OTEL_DEPLOYMENT_ENVIRONMENT\n\s+value: lab$", rendered, re.MULTILINE)


def test_service_name_stripped_when_unset(tree):
    # Unset must leave no entry at all: a blank value would override the
    # agent default (mainframe-rag-agent) with "" (tracing.py: no fallback).
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert "OTEL_SERVICE_NAME" not in rendered
    assert "__OTEL_SERVICE_NAME__" not in rendered


def test_service_name_wired_when_set(tree):
    r = _run(tree, ("OTEL_SERVICE_NAME", "my-rag-prod"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert re.search(r"OTEL_SERVICE_NAME\n\s+value: my-rag-prod$", rendered, re.MULTILINE)


# ------------------------------------------------------- ServiceMonitor (#187)


def test_metrics_off_skips_servicemonitor_and_renders_false(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert not (tree[0] / "dist" / "servicemonitor-rendered.yaml").exists()
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # Env var always rendered, quoted "false" = /metrics 404s (fail-closed).
    assert re.search(r'METRICS_ENABLED\n\s+value: "false"', rendered, re.MULTILINE)
    assert "Metrics off" in r.stdout


def test_metrics_enabled_deploys_servicemonitor_and_wires_env(tree):
    r = _run(tree, ("METRICS_ENABLED", "true"))
    assert r.returncode == 0, r.stderr
    sm = (tree[0] / "dist" / "servicemonitor-rendered.yaml").read_text()
    agent = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert "kind: ServiceMonitor" in sm
    assert "path: /metrics" in sm
    assert "namespace: ns" in sm
    assert_no_placeholders(sm)
    assert re.search(r'METRICS_ENABLED\n\s+value: "true"', agent, re.MULTILINE)
    assert_no_placeholders(agent)


def test_metrics_non_true_value_skips_servicemonitor(tree):
    # Only the literal "true" opts in — "false"/"1"/empty all stay off.
    for value in ("false", "1", ""):
        r = _run(tree, ("METRICS_ENABLED", value))
        assert r.returncode == 0, r.stderr
        assert not (tree[0] / "dist" / "servicemonitor-rendered.yaml").exists()


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


def test_reranker_endpoint_order_defaults_score_first(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert re.search(r"RERANK_ENDPOINT_ORDER\n\s+value: score_first", rendered, re.MULTILINE)
    assert "__RERANK_ENDPOINT_ORDER__" not in rendered


def test_reranker_endpoint_order_rerank_first_renders(tree):
    r = _run(tree, ("RERANK_ENDPOINT_ORDER", "rerank_first"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert re.search(r"RERANK_ENDPOINT_ORDER\n\s+value: rerank_first", rendered, re.MULTILINE)


# ------------------------------------------------------- gateway keys (LiteLLM)


def test_gateway_keys_off_strips_secret_block(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # No secret reference at all: no secretKeyRef, no key env names, no
    # surviving token — while the neighboring plain entries survive the
    # strip (an end-anchored range running past its entry would eat them).
    assert "secretKeyRef" not in rendered
    assert "API_KEY" not in rendered
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert "RERANK_MODEL" in rendered
    assert_no_placeholders(rendered)
    assert "Gateway keys off" in r.stdout


def test_gateway_keys_wired_when_secret_set(tree):
    r = _run(tree, ("GATEWAY_API_KEY_SECRET", "gateway-api-keys"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    for env_name, data_key in (
        ("LLM_API_KEY", "llm-api-key"),
        ("EMBED_API_KEY", "embed-api-key"),
        ("RERANK_API_KEY", "rerank-api-key"),
    ):
        # Sorted-key order (key before name), as kustomize renders mappings.
        assert re.search(
            rf"- name: {env_name}\n\s+valueFrom:\n\s+secretKeyRef:\n\s+key: {data_key}\n\s+name: gateway-api-keys",
            rendered,
        ), env_name
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert_no_placeholders(rendered)
    assert "Gateway keys wired" in r.stdout


def test_gateway_secret_bad_name_fails_closed(tree):
    r = _run(tree, ("GATEWAY_API_KEY_SECRET", "Bad_Name!"))
    assert r.returncode != 0
    assert "GATEWAY_API_KEY_SECRET must be a DNS-subdomain name" in r.stderr


def test_gateway_overlay_block_matches_stub_contract():
    """The stub kustomize above mirrors the real prod overlay by hand — pin
    the real file to the same contract (markers, env names, secret token,
    data keys) so the two cannot silently diverge."""
    real = (REPO / "deploy" / "kustomize" / "overlays" / "openshift" / "agent-prod-patch.yaml").read_text()
    assert "# gateway-api-keys-begin" in real
    assert "# gateway-api-keys-end" in real
    assert real.index("# gateway-api-keys-begin") < real.index("# gateway-api-keys-end")
    for env_name, data_key in (
        ("LLM_API_KEY", "llm-api-key"),
        ("EMBED_API_KEY", "embed-api-key"),
        ("RERANK_API_KEY", "rerank-api-key"),
    ):
        assert f"- name: {env_name}" in real
        assert f"key: {data_key}" in real
    assert "__GATEWAY_API_KEY_SECRET__" in real


def test_gateway_overlay_renders_endpoint_order_token():
    """The real prod overlay carries the order token deploy.sh substitutes."""
    real = (REPO / "deploy" / "kustomize" / "overlays" / "openshift" / "agent-prod-patch.yaml").read_text()
    assert re.search(r"- name: RERANK_ENDPOINT_ORDER\n\s+value: __RERANK_ENDPOINT_ORDER__", real)
