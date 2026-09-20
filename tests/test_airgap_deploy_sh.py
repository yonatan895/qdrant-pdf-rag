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
    install_rendering_helm,
    make_bin_tree,
    rendered_env,
    run_sh,
    set_oauth_proxy_pin,
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
            - name: EMBED_MODEL_REVISION
              value: __EMBED_MODEL_REVISION__
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: __OTEL_EXPORTER_OTLP_ENDPOINT__
            - name: IMAGE_SHA
              value: __IMAGE_SHA__
            - name: QDRANT_API_KEY
              valueFrom:
                secretKeyRef:
                  key: read-only-api-key
                  name: qdrant-apikey
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
    *openshift-ui*) cat {oauth_stub} ;;
    *) cat {stub_yaml} ;;
  esac
  exit 0
fi
printf '%s\\n' "$@" >> "$HELM_LOG"
case "$*" in
  *'rollout status '*)
    [ "${{ROLLOUT_FAIL:-}}" != 1 ] || exit 1 ;;
  'api-resources -o name')
    [ "${{DISCOVERY_FAIL:-}}" != 1 ] || exit 1
    printf '%s\\n' routes.route.openshift.io servicemonitors.monitoring.coreos.com ;;
  *'get deployment.apps/jaeger '*|*'get serviceaccount/rag-agent '*|*'get servicemonitor.monitoring.coreos.com/rag-agent '*)
    [ "${{DISABLED_READ_FAIL:-}}" != 1 ] || exit 1
    if [ -n "${{DISABLED_FILE:-}}" ]; then cat "$DISABLED_FILE";
    else :; fi ;;
  *'get -f '*'-o json'*)
    [ "${{ACTIVE_READ_FAIL:-}}" != 1 ] || exit 1
    if [ -n "${{OWNERSHIP_FILE:-}}" ]; then cat "$OWNERSHIP_FILE";
    else :; fi ;;

  *'get secret '*'go-template='*)
    if [ -n "${{MISSING_KEY:-}}" ]; then
      case "$*" in *"$MISSING_KEY"*) exit 0 ;; esac
    fi
    echo present ;;

esac
exit 0
"""


@pytest.fixture
def tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "deploy.sh", "map_values.py"])
    (tmp_path / "overlays" / "openshift").mkdir(parents=True, exist_ok=True)
    (tmp_path / "deploy" / "kustomize").mkdir(parents=True, exist_ok=True)
    copy_chart(tmp_path)
    shutil.copy(REPO / "overlays" / "openshift" / "values.yaml", tmp_path / "overlays" / "openshift")
    shutil.copytree(REPO / "deploy" / "kustomize" / "jaeger", tmp_path / "deploy" / "kustomize" / "jaeger")
    shutil.copytree(REPO / "deploy" / "kustomize" / "servicemonitor", tmp_path / "deploy" / "kustomize" / "servicemonitor")
    shutil.copy(REPO / "images.txt", tmp_path / "images.txt")
    stub_yaml = tmp_path / "stub-kustomize.yaml"
    stub_yaml.write_text(STUB_KUSTOMIZE)
    jaeger_stub = tmp_path / "stub-jaeger.yaml"
    jaeger_stub.write_text(STUB_JAEGER)
    servicemonitor_stub = tmp_path / "stub-servicemonitor.yaml"
    servicemonitor_stub.write_text(STUB_SERVICEMONITOR)
    oauth_stub = tmp_path / "stub-kustomize-ui.yaml"
    oauth_stub.write_text(
        STUB_KUSTOMIZE
        + "        - name: oauth-proxy\n          image: __OAUTH_PROXY_IMAGE__\n"
    )
    helm_log = tmp_path / "helm-args.log"
    for name in ("helm", "kubectl", "oc", "kustomize"):
        p = tmp_path / "bin" / name
        p.write_text(STUB_BIN.format(stub_yaml=stub_yaml, jaeger_stub=jaeger_stub, servicemonitor_stub=servicemonitor_stub, oauth_stub=oauth_stub))
        p.chmod(0o755)
    install_rendering_helm(tmp_path)
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
        "EMBED_MODEL_REVISION": "rev-1",
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


# ------------------------------------------------------- Qdrant least privilege (#366)

def _stub_with_qdrant_key(tree, key_line):
    """Rewrite the stub kustomize output's QDRANT_API_KEY data key."""
    tmp_path, _ = tree
    stub = (tmp_path / "charts/mainframe-rag/templates/agent-deployment.yaml").read_text()
    assert "key: read-only-api-key" in stub
    (tmp_path / "charts/mainframe-rag/templates/agent-deployment.yaml").write_text(
        stub.replace("key: read-only-api-key", key_line)
    )


def _qdrant_block(rendered):
    lines = rendered.splitlines()
    start = next(i for i, l in enumerate(lines) if "- name: QDRANT_API_KEY" in l)
    return "\n".join(lines[start : start + 5])


def test_agent_qdrant_key_wired_readonly(tree):
    """Issue #366: the rendered agent must reference the chart's read-only
    key — never the full-access one."""
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    block = _qdrant_block((tree[0] / "dist" / "agent-rendered.yaml").read_text())
    assert re.search(r"(?m)^\s*key: read-only-api-key$", block)
    assert not re.search(r"(?m)^\s*key: api-key$", block)


def test_agent_qdrant_write_key_fails_closed(tree):
    """A render wiring the full-access key must stop the deploy before apply."""
    _stub_with_qdrant_key(tree, "key: api-key")
    r = _run(tree)
    assert r.returncode != 0
    assert "read-only-api-key" in r.stderr


def test_agent_qdrant_key_missing_fails_closed(tree):
    """No QDRANT_API_KEY block at all must also stop the deploy."""
    tmp_path, _ = tree
    stub = (tmp_path / "charts/mainframe-rag/templates/agent-deployment.yaml").read_text()
    lines = stub.splitlines()
    start = next(i for i, l in enumerate(lines) if "- name: QDRANT_API_KEY" in l)
    del lines[start : start + 5]
    (tmp_path / "charts/mainframe-rag/templates/agent-deployment.yaml").write_text("\n".join(lines) + "\n")
    r = _run(tree)
    assert r.returncode != 0
    assert "read-only-api-key" in r.stderr


def test_agent_overlay_qdrant_contract():
    """The stub above mirrors the real prod overlay by hand — pin the real
    file to the same Qdrant contract so the two cannot silently diverge."""
    real = (
        REPO / "deploy" / "kustomize" / "overlays" / "openshift" / "agent-prod-patch.yaml"
    ).read_text()
    assert "- name: QDRANT_API_KEY" in real
    assert re.search(r"(?m)^\s*key: read-only-api-key$", real)
    assert not re.search(r"(?m)^\s*key: api-key$", real)
    assert "__QDRANT_RELEASE__-apikey" in real


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
    # Issue #391 F1: the operator-declared revision reaches the agent
    # container (the agent refuses a blank attestation at startup).
    assert re.search(r"(?m)^\s*- name: EMBED_MODEL_REVISION$", rendered)
    assert rendered_env(rendered, "agent")["EMBED_MODEL_REVISION"] == "rev-1"
    assert_no_placeholders(rendered)


def test_missing_embed_revision_fails_before_render(tree):
    r = _run(tree, ("EMBED_MODEL_REVISION", ""))
    assert r.returncode != 0
    assert "required variables unset" in r.stderr and "EMBED_MODEL_REVISION" in r.stderr


def test_whitespace_embed_revision_fails_before_render(tree):
    r = _run(tree, ("EMBED_MODEL_REVISION", "  "))
    assert r.returncode != 0
    assert "EMBED_MODEL_REVISION must be a non-blank" in r.stderr


def test_agent_overlay_embed_revision_contract():
    """The stub above mirrors the real prod overlay by hand — pin the real
    file to the same revision contract so the two cannot silently diverge."""
    real = (
        REPO / "deploy" / "kustomize" / "overlays" / "openshift" / "agent-prod-patch.yaml"
    ).read_text()
    assert re.search(r"(?m)^\s*- name: EMBED_MODEL_REVISION$", real)
    assert re.search(r"(?m)^\s*value: __EMBED_MODEL_REVISION__$", real)


# ------------------------------------------------------- Jaeger / tracing (#83)


def test_tracing_on_by_default_deploys_jaeger(tree):
    # Unset OTEL_EXPORTER_OTLP_ENDPOINT resolves to the in-cluster Jaeger:
    # tracing is active in production unless explicitly disabled.
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert (tree[0] / "dist" / "jaeger-rendered.yaml").exists()
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert 'value: "http://jaeger:4318"' in rendered
    assert_no_placeholders(rendered)
    assert "Tracing off" not in r.stdout


@pytest.mark.parametrize("token", ["off", "none", "false", "0", "OFF", "Off"])
def test_tracing_off_sentinel_skips_jaeger(tree, token):
    r = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", token))
    assert r.returncode == 0, r.stderr
    assert not (tree[0] / "dist" / "jaeger-rendered.yaml").exists()
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # Endpoint env var always rendered; empty value = tracing off.
    assert rendered_env(rendered, "agent")["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""
    assert "Tracing off" in r.stdout


def test_tracing_bad_endpoint_fails_closed(tree):
    r = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "jaeger:4318"))
    assert r.returncode != 0
    assert "must be http(s) or off" in r.stderr


def test_tracing_enabled_deploys_jaeger_and_wires_endpoint(tree):
    r = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"))
    assert r.returncode == 0, r.stderr
    jaeger = (tree[0] / "dist" / "jaeger-rendered.yaml").read_text()
    agent = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert "reg.internal/jaegertracing/jaeger:v2.20.0" in jaeger
    assert "storageClassName: standard" in jaeger
    assert "namespace: ns" in jaeger
    assert_no_placeholders(jaeger)
    assert rendered_env(agent, "agent")["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://jaeger:4318"
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
    assert rendered_env(rendered, "agent")["IMAGE_SHA"] == IMAGE_SHA


def test_deploy_identity_environment_empty_by_default(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # Optional: bare `value:` renders and the agent omits the attribute —
    # same empty-renders-bare convention as the OTEL endpoint above.
    assert rendered_env(rendered, "agent")["OTEL_DEPLOYMENT_ENVIRONMENT"] == ""


def test_deploy_identity_environment_wired_when_set(tree):
    r = _run(tree, ("OTEL_DEPLOYMENT_ENVIRONMENT", "lab"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert rendered_env(rendered, "agent")["OTEL_DEPLOYMENT_ENVIRONMENT"] == "lab"


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
    assert rendered_env(rendered, "agent")["OTEL_SERVICE_NAME"] == "my-rag-prod"


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
    assert rendered_env(rendered, "agent")["RERANK_ENDPOINT_ORDER"] == "score_first"
    assert "__RERANK_ENDPOINT_ORDER__" not in rendered


def test_reranker_endpoint_order_rerank_first_renders(tree):
    r = _run(tree, ("RERANK_ENDPOINT_ORDER", "rerank_first"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert rendered_env(rendered, "agent")["RERANK_ENDPOINT_ORDER"] == "rerank_first"


# ------------------------------------------------------- gateway keys (LiteLLM)


def test_gateway_keys_off_strips_secret_block(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # Gateway secret references are gone (no key env names, no surviving
    # token) while the neighboring plain entries survive the strip (an
    # end-anchored range running past its entry would eat them). The
    # chart-managed QDRANT_API_KEY ref is not a gateway key and stays
    # (issue #366).
    for env_name in ("LLM_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY"):
        assert env_name not in rendered
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert "QDRANT_API_KEY" in rendered
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
        assert rendered_env(rendered, "agent")[env_name] == {
            "secretKeyRef": {"key": data_key, "name": "gateway-api-keys"}
        }
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


def test_agent_route_renders_oauth_sidecar_and_reencrypt_route(tree):
    """ADR-0004: AGENT_ROUTE=true renders the openshift-ui overlay (oauth
    sidecar on the internal registry ref) and applies a reencrypt Route to
    the console port; the dry-run keeps rendering cluster-free."""
    tmp_path, _ = tree
    set_oauth_proxy_pin(tmp_path, "sha256:" + "b" * 64)
    # Issue #448 H2a: route-on dry-run rehearses the chart Route too, which
    # needs the namespace service CA as a generated value (D8).
    ca_file = tmp_path / "route-ca.crt"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nDRYRUN\n-----END CERTIFICATE-----\n")
    r = _run(
        tree,
        ("AGENT_ROUTE", "true"),
        ("AIRGAP_DRYRUN", "1"),
        ("ROUTE_DESTINATION_CA_FILE", str(ca_file)),
    )
    assert r.returncode == 0, r.stderr
    rendered = (tmp_path / "dist" / "agent-rendered.yaml").read_text()
    assert "reg.internal/openshift4/ose-oauth-proxy:v4.14" in rendered
    assert "__OAUTH_PROXY_IMAGE__" not in rendered
    assert "Route rag-agent -> svc port oauth, reencrypt, timeout 300s" in r.stdout


def test_agent_route_fails_closed_on_pending_oauth_pin(tree):
    """An explicitly pending pin: enabling the console Route
    without a recorded digest must stop the deploy with the fix spelled out."""
    set_oauth_proxy_pin(tree[0], "sha256:PENDING")
    r = _run(tree, ("AGENT_ROUTE", "true"))
    assert r.returncode != 0
    assert "oauth-proxy digest recorded" in r.stderr
    assert "sha256:PENDING" in r.stderr


@pytest.mark.parametrize("ownership", ["legacy", "ours", "other-release", "other-namespace", "controller", "other-manager"])
def test_app_adoption_checks_ownership_before_any_release_mutation(tree, ownership):
    import json

    meta = {"name": "rag-agent", "namespace": "ns"}
    if ownership in ("ours", "other-release", "other-namespace"):
        meta["annotations"] = {
            "meta.helm.sh/release-name": "other" if ownership == "other-release" else "mainframe-rag",
            "meta.helm.sh/release-namespace": "other" if ownership == "other-namespace" else "ns",
        }
    if ownership == "controller":
        meta["ownerReferences"] = [{"name": "operator"}]
    if ownership == "other-manager":
        meta["labels"] = {"app.kubernetes.io/managed-by": "other"}
    inventory = tree[0] / "existing.json"
    inventory.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta},
    ]}))
    result = _run(tree, ("OWNERSHIP_FILE", str(inventory)))
    allowed = ownership in ("legacy", "ours")
    assert (result.returncode == 0) == allowed, result.stderr
    log = _helm_log(tree)
    assert ("upgrade" in log) == allowed
    if allowed:
        assert "--take-ownership" in log
        assert "--server-side=false" in log
    else:
        assert "ownership preflight refused" in result.stderr


@pytest.mark.parametrize("key", ["llm-api-key", "embed-api-key", "rerank-api-key", ".dockerconfigjson"])
def test_missing_referenced_secret_key_blocks_before_mutation(tree, key):
    result = _run(tree, ("GATEWAY_API_KEY_SECRET", "gateway-keys"),
                  ("PULL_SECRET", "registry-pull"), ("MISSING_KEY", key))
    assert result.returncode != 0
    assert "required Secret key is missing or empty" in result.stderr
    assert "upgrade" not in _helm_log(tree)


def test_invalid_schema_fails_before_namespace_or_release_mutation(tree):
    result = _run(tree, ("RERANK_ENDPOINT_ORDER", "invalid"))
    assert result.returncode != 0
    log = _helm_log(tree)
    assert "upgrade" not in log
    assert "new-project" not in log
    assert "create" not in log


def test_model_revision_yaml_characters_round_trip(tree):
    revision = 'release: #tag | a & b "quoted"\nsecond line'
    result = _run(tree, ("EMBED_MODEL_REVISION", revision), ("EMBED_MODEL", "true"))
    assert result.returncode == 0, result.stderr
    rendered = (tree[0] / "dist/agent-rendered.yaml").read_text()
    assert rendered_env(rendered, "agent")["EMBED_MODEL_REVISION"] == revision
    assert rendered_env(rendered, "agent")["EMBED_MODEL"] == "true"


@pytest.mark.parametrize("payload", ['{"kind":"List"}', '{"kind":"List","items":null}', 'not-json'])
def test_corrupt_ownership_inventory_blocks_mutation(tree, payload):
    inventory = tree[0] / "existing.json"
    inventory.write_text(payload)
    result = _run(tree, ("OWNERSHIP_FILE", str(inventory)))
    assert result.returncode != 0
    assert "ownership preflight refused" in result.stderr
    assert "upgrade" not in _helm_log(tree)


def test_old_helm_fails_before_any_release_mutation(tree):
    helm = tree[0] / "bin/helm"
    helm.write_text(helm.read_text().replace('case "$1" in',
        'if [ "$1" = version ]; then echo v3.19.0; exit 0; fi\ncase "$1" in'))
    result = _run(tree)
    assert result.returncode != 0
    assert "Helm 4 is required" in result.stderr
    assert "upgrade" not in _helm_log(tree)


@pytest.mark.parametrize("owner", ["legacy", "ours", "other", "controller"])
def test_disabled_legacy_resources_are_checked_then_cleaned_after_rollout(tree, owner):
    import json

    meta = {"name": "jaeger", "namespace": "ns"}
    if owner in ("ours", "other"):
        meta["annotations"] = {"meta.helm.sh/release-name": "mainframe-rag" if owner == "ours" else "another"}
    if owner == "controller":
        meta["ownerReferences"] = [{"name": "operator"}]
    path = tree[0] / "disabled.json"
    path.write_text(json.dumps({"kind": "List", "items": [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta,
         "spec": {"unrelated": "must not enter deletion file"}},
    ]}))
    result = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"), ("DISABLED_FILE", str(path)))
    log = _helm_log(tree)
    if owner in ("other", "controller"):
        assert result.returncode != 0
        assert "upgrade" not in log
        assert "delete" not in log
    else:
        assert result.returncode == 0, result.stderr
        cleanup = json.loads((tree[0] / "dist/app-disabled-cleanup.json").read_text())
        assert cleanup["items"] == [{"apiVersion": "apps/v1", "kind": "Deployment",
                                      "metadata": {"name": "jaeger", "namespace": "ns"}}]
        assert log.index("delete") > log.index("rollout") > log.index("upgrade")


@pytest.mark.parametrize("failure", ["DISCOVERY_FAIL", "DISABLED_READ_FAIL", "ACTIVE_READ_FAIL"])
def test_disabled_resource_read_failures_block_mutations(tree, failure):
    result = _run(tree, (failure, "1"))
    assert result.returncode != 0
    assert "upgrade" not in _helm_log(tree)
    assert "delete" not in _helm_log(tree)


def test_disabled_cleanup_never_accepts_a_pvc(tree):
    import json

    path = tree[0] / "disabled.json"
    path.write_text(json.dumps({"kind": "List", "items": [
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": "jaeger-badger", "namespace": "ns"}},
    ]}))
    result = _run(tree, ("DISABLED_FILE", str(path)))
    assert result.returncode != 0
    assert "upgrade" not in _helm_log(tree)
    assert "delete" not in _helm_log(tree)


def test_legacy_cleanup_waits_for_successful_rollouts(tree):
    import json

    path = tree[0] / "disabled.json"
    path.write_text(json.dumps({"kind": "List", "items": [
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "jaeger", "namespace": "ns"}},
    ]}))
    result = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"),
                  ("DISABLED_FILE", str(path)), ("ROLLOUT_FAIL", "1"))
    assert result.returncode != 0
    log = _helm_log(tree)
    assert "upgrade" in log
    assert "delete" not in log
