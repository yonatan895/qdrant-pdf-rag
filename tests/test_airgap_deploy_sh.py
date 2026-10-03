"""scripts/airgap/deploy.sh fail-close + knob regressions (issue #15 / PR #32).

Runs deploy.sh with AIRGAP_DRYRUN=1 against a stubbed bin dir and a copied
tree — no cluster, no helm, no network. The PULL_SECRET fail-close is the
regression the rehearsal build surfaced: values.yaml's placeholder pull-secret
name must never reach a cluster.
"""

import json
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
    sha256_bytes,
    write_git_identity_stub,
    write_signed_manifest,
    write_stub,
)

IMAGE_SHA = "a" * 40  # full-sha shaped; deploy.sh only rejects "" / "HEAD"

STUB_BIN = """#!/bin/sh
printf '%s\\n' "$@" >> "$HELM_LOG"
case "$*" in
  *'rollout status '*)
    [ "${{ROLLOUT_FAIL:-}}" != 1 ] || exit 1 ;;
  'api-resources -o name')
    [ "${{DISCOVERY_FAIL:-}}" != 1 ] || exit 1
    printf '%s\\n' routes.route.openshift.io servicemonitors.monitoring.coreos.com ;;
  *'delete route.route.openshift.io/rag-agent'*)
    [ "${{DELETE_FAIL:-}}" != 1 ] || exit 1 ;;
  *'get routes.route.openshift.io '*)
    [ "${{ROUTES_READ_FAIL:-}}" != 1 ] || exit 1
    if [ -n "${{ROUTES_FILE:-}}" ]; then cat "$ROUTES_FILE";
    else printf '%s\\n' '{{"apiVersion":"v1","kind":"List","items":[]}}'; fi ;;
  *'get route.route.openshift.io/rag-agent '*'-o json'*)
    [ "${{ROUTE_LIVE_FILE:-}}" != "" ] || exit 1
    cat "$ROUTE_LIVE_FILE" ;;
  *'get configmap openshift-service-ca.crt'*)
    printf '%s\\n' "${{SERVICE_CA:-}}" ;;
  *'get deployment.apps/jaeger '*|*'get serviceaccount/rag-agent '*|*'get servicemonitor.monitoring.coreos.com/rag-agent '*)
    [ "${{DISABLED_READ_FAIL:-}}" != 1 ] || exit 1
    if [ -n "${{DISABLED_FILE:-}}" ]; then cat "$DISABLED_FILE";
    else :; fi ;;
  *'get -f '*'-o json'*)
    [ "${{ACTIVE_READ_FAIL:-}}" != 1 ] || exit 1
    if [ -n "${{OWNERSHIP_FILE:-}}" ]; then cat "$OWNERSHIP_FILE";
    else :; fi ;;

  *'get deployments,statefulsets,replicasets,pods -o json'*)
    [ -z "${{PODS_FILE:-}}" ] || cat "$PODS_FILE" ;;
  *'get secret '*'go-template='*)
    if [ -n "${{MISSING_KEY:-}}" ]; then
      case "$*" in *"$MISSING_KEY"*) exit 0 ;; esac
    fi
    echo present ;;

esac
exit 0
"""


# Registry stub: `skopeo inspect --raw docker://REF` serves
# $SKOPEO_REGISTRY/<REF with / : @ replaced by _>.raw, else "manifest unknown".
STUB_SKOPEO = r"""#!/bin/sh
printf 'skopeo %s\n' "$*" >> "$HELM_LOG"
[ "$1" = inspect ] || exit 0
for a in "$@"; do case "$a" in docker://*) ref="${a#docker://}" ;; esac; done
f="$SKOPEO_REGISTRY/$(printf '%s' "$ref" | tr '/:@' '___').raw"
[ -f "$f" ] || { echo "manifest unknown" >&2; exit 1; }
cat "$f"
"""


@pytest.fixture
def tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "deploy.sh", "map_values.py"])
    copy_chart(tmp_path)
    shutil.copy(REPO / "charts" / "qdrant-openshift.values.yaml", tmp_path / "charts")
    shutil.copy(REPO / "images.txt", tmp_path / "images.txt")
    helm_log = tmp_path / "helm-args.log"
    for name in ("helm", "kubectl", "oc"):
        p = tmp_path / "bin" / name
        p.write_text(STUB_BIN.format())
        p.chmod(0o755)
    install_rendering_helm(tmp_path)
    write_stub(tmp_path / "bin" / "skopeo", STUB_SKOPEO)
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
        "SKOPEO_REGISTRY": str(tmp_path / "registry"),
        "PODS_FILE": str(tmp_path / "pods.json"),
    }
    for k, v in extra_env:
        env[k] = v
    return run_sh(tmp_path / "scripts" / "airgap" / "deploy.sh", env, tmp_path)


def _helm_log(tree):
    # A refusal before any client call leaves no log at all.
    return tree[1].read_text() if tree[1].exists() else ""


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
    """Mutate the real chart's QDRANT_API_KEY data key."""
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




def test_storage_size_knob_covers_persistence_and_snapshot(tree):
    _run(tree, ("QDRANT_STORAGE_SIZE", "1Gi"))
    log = _helm_log(tree)
    assert "persistence.size=1Gi" in log
    assert "snapshotPersistence.size=1Gi" in log


ROLE_REFS = {
    "qdrant": "reg.internal/qdrant/qdrant:v1.19.0-unprivileged",
    "agent": f"reg.internal/qdrant-pdf-rag-agent:{IMAGE_SHA}",
    "jaeger": "reg.internal/jaegertracing/jaeger:v2.20.0",
}
ROLE_REPOS = {
    "qdrant": "reg.internal/qdrant/qdrant",
    "agent": "reg.internal/qdrant-pdf-rag-agent",
    "jaeger": "reg.internal/jaegertracing/jaeger",
}


def _digest_of(raw: bytes) -> str:
    return "sha256:" + sha256_bytes(raw)


def _config_of(role: str) -> str:
    return _digest_of(f"config-{role}".encode())


def _image_manifest(role: str, config: str | None = None, **extra) -> bytes:
    body = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"digest": config or _config_of(role), "size": 7},
        "layers": [{"digest": _digest_of(f"layer-{role}-gz".encode()), "size": 9}],
        **extra,
    }
    return json.dumps(body, separators=(",", ":")).encode() + b"\n"


def _put_registry(tree, role: str, raw: bytes | None) -> str:
    """Stub registry content for a role's tag; None removes the tag."""
    reg = tree[0] / "registry"
    reg.mkdir(exist_ok=True)
    f = reg / (ROLE_REFS[role].replace("/", "_").replace(":", "_").replace("@", "_") + ".raw")
    if raw is None:
        f.unlink(missing_ok=True)
        return ""
    f.write_bytes(raw)
    return _digest_of(raw)


def _load_all(tree) -> dict[str, str]:
    """What a successful airgap:load leaves in the registry: role -> manifest digest."""
    return {role: _put_registry(tree, role, _image_manifest(role)) for role in ROLE_REFS}


def _write_pods(tree, digests: dict[str, str], **override) -> None:
    items = []
    for role, repo in ROLE_REPOS.items():
        kind = "StatefulSet" if role == "qdrant" else "Deployment"
        name = "rag-agent" if role == "agent" else role
        replicas = 3 if role == "qdrant" else 1
        uid = f"{role}-workload"
        spec = {"containers": [{"name": role, "image": f"{repo}:tag"}]}
        items.append({
            "kind": kind, "metadata": {"name": name, "namespace": "ns", "uid": uid,
                "annotations": {"deployment.kubernetes.io/revision": "2"}},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": {"spec": spec}}, "status": {"updateRevision": "qdrant-current"},
        })
        owner_kind = kind
        if kind == "Deployment":
            owner_kind, uid = "ReplicaSet", f"{role}-rs"
            items.append({"kind": "ReplicaSet", "metadata": {
                "name": uid, "namespace": "ns", "uid": uid,
                "annotations": {"deployment.kubernetes.io/revision": "2"},
                "ownerReferences": [{"kind": kind, "uid": f"{role}-workload", "controller": True}],
            }})
        if role in override and override[role] is None:
            continue
        digest = override.get(role, digests[role])
        for n in range(replicas):
            items.append({
                "kind": "Pod", "metadata": {"name": f"{role}-{n}", "namespace": "ns",
                    "labels": {"app": name, "controller-revision-hash": "qdrant-current"},
                    "ownerReferences": [{"kind": owner_kind, "uid": uid, "controller": True}]},
                "spec": spec,
                "status": {"phase": "Running", "containerStatuses": [
                    {"name": role, "imageID": f"{repo}@{digest}"}]},
            })
    (tree[0] / "pods.json").write_text(json.dumps({"items": items}))


def _manifest(tree, chart_sha=None, sha=IMAGE_SHA, config_digests=True):
    dist = tree[0] / "dist"
    dist.mkdir(exist_ok=True)
    write_git_identity_stub(tree[0], sha)  # a claimed release needs a resolvable checkout (#414)
    lines = [f"sha: {sha}"]
    if chart_sha is not None:
        lines.append(f"chart_sha256: {chart_sha}")
    if config_digests:
        lines += [f"{role}_config_digest: {_config_of(role)}" for role in ROLE_REFS]
    write_signed_manifest(dist, "\n".join(lines) + "\n")


def _chart(tree):
    return next((tree[0] / "charts").glob("qdrant-*.tgz"))


def test_two_qdrant_charts_refuse_before_any_command(tree):
    shutil.copy(_chart(tree), tree[0] / "charts" / "qdrant-9.9.9.tgz")
    result = _run(tree)
    assert result.returncode != 0
    assert "exactly one vendored Qdrant chart is required" in result.stderr
    assert "found 2" in result.stderr
    assert not tree[1].exists() or "upgrade" not in _helm_log(tree)


def test_no_qdrant_chart_refuses(tree):
    _chart(tree).unlink()
    result = _run(tree)
    assert result.returncode != 0
    assert "exactly one vendored Qdrant chart is required" in result.stderr
    assert "found 0" in result.stderr


def test_manifest_chart_sha_mismatch_refuses_before_mutation(tree):
    _manifest(tree, "0" * 64)
    result = _run(tree)
    assert result.returncode != 0
    assert "does not match the packed MANIFEST chart_sha256" in result.stderr
    assert not tree[1].exists() or "upgrade" not in _helm_log(tree)


def test_manifest_without_chart_sha_refuses(tree):
    _manifest(tree)
    result = _run(tree)
    assert result.returncode != 0
    assert "has no chart_sha256" in result.stderr


def test_manifest_chart_sha_match_deploys_that_chart(tree):
    _manifest(tree, sha256_bytes(_chart(tree).read_bytes()))
    _write_pods(tree, _load_all(tree))
    result = _run(tree)
    assert result.returncode == 0, result.stderr
    assert "verified against packed MANIFEST" in result.stdout
    assert str(_chart(tree).relative_to(tree[0])) in _helm_log(tree)


def test_chart_identity_failure_then_fix_passes_on_next_run(tree):
    extra = tree[0] / "charts" / "qdrant-9.9.9.tgz"
    shutil.copy(_chart(tree), extra)
    assert _run(tree).returncode != 0
    extra.unlink()
    _manifest(tree, "0" * 64)
    assert _run(tree).returncode != 0
    _manifest(tree, sha256_bytes(_chart(tree).read_bytes()))
    _write_pods(tree, _load_all(tree))
    result = _run(tree)
    assert result.returncode == 0, result.stderr
    assert "upgrade" in _helm_log(tree)


def test_dry_run_with_manifest_is_noticed_not_verified(tree):
    _manifest(tree, "0" * 64)
    result = _run(tree, ("AIRGAP_DRYRUN", "1"))
    assert result.returncode == 0, result.stderr
    assert "not release-verified" in result.stdout


def test_no_manifest_is_noticed_not_verified(tree):
    result = _run(tree)
    assert result.returncode == 0, result.stderr
    assert "not release-verified" in result.stdout


def test_missing_production_values_fails_before_mutation(tree):
    (tree[0] / "charts" / "qdrant-openshift.values.yaml").unlink()
    result = _run(tree, ("AIRGAP_DRYRUN", "0"))
    assert result.returncode != 0
    assert "required Qdrant values file is missing or unreadable" in result.stderr
    assert not tree[1].exists(), "no Helm or cluster command may run without base values"


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
    assert args.index("charts/qdrant-openshift.values.yaml") < args.index(str(vals))


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
        # Exercise key-before-name ordering in the Secret reference.
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






def test_agent_route_renders_oauth_sidecar_and_reencrypt_route(tree):
    """ADR-0004: AGENT_ROUTE=true renders the openshift-ui overlay (oauth
    sidecar on the internal registry ref) and applies a reencrypt Route to
    the console port; the dry-run keeps rendering cluster-free."""
    tmp_path, _ = tree
    set_oauth_proxy_pin(tmp_path, "sha256:" + "b" * 64)
    # Route-on dry-run renders the chart Route, which
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


@pytest.mark.parametrize("from_file", [False, True])
def test_shared_gateway_aliases_and_secret_reach_requests(tree, from_file):
    import json

    import httpx2

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.embed import VllmEmbedder
    from mainframe_rag.ports import ChatMessage
    from tests.helpers_airgap import rendered_container

    options = {
        "GATEWAY_BASE_URL": "https://sample-api/v1/",
        "GATEWAY_API_KEY_SECRET": "shared-gateway",
        "GATEWAY_API_KEY_SECRET_KEY": "api-key",
        "EMBED_MODEL": "embedding-v1",
        "LLM_MODEL_REASONING": "code",
    }
    if from_file:
        path = tree[0] / "gateway.env"
        path.write_text("\n".join(f"{key}='{value}'" for key, value in options.items()))
        # _run's synthetic embed model is a caller override, so explicitly
        # select the requested alias there while resolving URL/Secret from file.
        extra = [("AIRGAP_ENV", str(path)), ("EMBED_MODEL", "embedding-v1")]
    else:
        extra = list(options.items())
    result = _run(tree, *extra)
    assert result.returncode == 0, result.stderr
    rendered = (tree[0] / "dist/agent-rendered.yaml").read_text()
    scalars = rendered_env(rendered, "agent")
    for key in ("EMBED_BASE_URL", "LLM_BASE_URL", "RERANK_BASE_URL"):
        assert scalars[key] == "https://sample-api/v1/"
    assert scalars["EMBED_MODEL"] == "embedding-v1"
    assert scalars["LLM_MODEL_REASONING"] == "code"
    env = {entry["name"]: entry for entry in rendered_container(rendered, "agent")["env"]}
    for key in ("EMBED_API_KEY", "LLM_API_KEY", "RERANK_API_KEY"):
        assert env[key]["valueFrom"]["secretKeyRef"] == {
            "name": "shared-gateway", "key": "api-key",
        }
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append((str(request.url), body["model"], request.headers["Authorization"]))
        if request.url.path == "/v1/embeddings":
            return httpx2.Response(200, json={"data": [{"index": 0, "embedding": [0.25, 0.75]}]})
        assert request.url.path == "/v1/chat/completions"
        return httpx2.Response(200, json={"choices": [
            {"message": {"content": "Synthetic answer"}, "finish_reason": "stop"},
        ]})

    # Simulate Kubernetes resolving the single referenced Secret, then use
    # real runtime clients/HTTP serialization with an in-process transport.
    settings = Settings(
        _env_file=None, embed_base_url=scalars["EMBED_BASE_URL"],
        embed_model=scalars["EMBED_MODEL"], llm_base_url=scalars["LLM_BASE_URL"],
        llm_model_reasoning=scalars["LLM_MODEL_REASONING"], llm_stream=False,
        embed_api_key="synthetic-shared-key", llm_api_key="synthetic-shared-key",
    )
    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        assert VllmEmbedder(settings, client=client).dense(["Synthetic input"]) == [[0.25, 0.75]]
        answer = HttpxLLMClient(settings, client=client).chat([ChatMessage(role="user", content="Hello")])
        assert answer.content == "Synthetic answer"
    assert seen == [
        ("https://sample-api/v1/embeddings", "embedding-v1", "Bearer synthetic-shared-key"),
        ("https://sample-api/v1/chat/completions", "code", "Bearer synthetic-shared-key"),
    ]


def test_shared_gateway_specific_url_and_caller_override_win(tree):
    path = tree[0] / "gateway.env"
    path.write_text("GATEWAY_BASE_URL=https://file-gateway/v1\nGATEWAY_API_KEY_SECRET_KEY=file-key\n")
    result = _run(tree, ("AIRGAP_ENV", str(path)),
                  ("GATEWAY_BASE_URL", "https://caller-gateway/v1"),
                  ("LLM_BASE_URL", "https://reasoning-override/v1"),
                  ("LLM_MODEL_REASONING", "code"),
                  ("GATEWAY_API_KEY_SECRET", "shared"), ("GATEWAY_API_KEY_SECRET_KEY", "caller-key"))
    assert result.returncode == 0, result.stderr
    rendered = (tree[0] / "dist/agent-rendered.yaml").read_text()
    values = rendered_env(rendered, "agent")
    assert values["EMBED_BASE_URL"] == "https://caller-gateway/v1"
    assert values["LLM_BASE_URL"] == "https://reasoning-override/v1"
    assert 'key: "caller-key"' in rendered
    assert "file-key" not in rendered


def test_missing_shared_gateway_key_blocks_before_mutation(tree):
    result = _run(tree, ("AIRGAP_DRYRUN", "0"),
                  ("GATEWAY_API_KEY_SECRET", "shared"), ("GATEWAY_API_KEY_SECRET_KEY", "shared-key"),
                  ("MISSING_KEY", "shared-key"))
    assert result.returncode != 0
    assert "required Secret key is missing or empty: shared-key" in result.stderr
    log = _helm_log(tree)
    assert "upgrade" not in log and "apply" not in log


@pytest.mark.parametrize("key,secret", [('bad"key', "shared"), ("a" * 254, "shared"), ("api-key", "")])
def test_invalid_shared_gateway_key_refuses_before_commands(tree, key, secret):
    result = _run(tree, ("GATEWAY_API_KEY_SECRET", secret), ("GATEWAY_API_KEY_SECRET_KEY", key))
    assert result.returncode != 0
    assert "GATEWAY_API_KEY_SECRET_KEY" in result.stderr
    assert not tree[1].exists()


# --------------------------------- Decoupled backends (issue #529 OBS-2)


def test_jaeger_false_without_destination_fails_before_mutation(tree):
    # Issue #568: the defaulted http://jaeger:4318 belongs to the bundled
    # backend. Disabling it without naming a collector (or turning tracing
    # off) must fail in preflight, before any render or cluster command.
    r = _run(tree, ("JAEGER_ENABLED", "false"))
    assert r.returncode != 0
    assert "JAEGER_ENABLED=false needs an intentional trace destination" in r.stderr
    assert not tree[1].exists()
    assert not (tree[0] / "dist").exists()


@pytest.mark.parametrize("endpoint", ["http://collector.platform:4318", "http://jaeger:4318"])
def test_jaeger_false_with_explicit_collector_exports_to_it(tree, endpoint):
    # The former hostname stays possible when it is chosen intentionally.
    r = _run(tree, ("JAEGER_ENABLED", "false"), ("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint))
    assert r.returncode == 0, r.stderr
    assert not (tree[0] / "dist" / "jaeger-rendered.yaml").exists()
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert rendered_env(rendered, "agent")["OTEL_EXPORTER_OTLP_ENDPOINT"] == endpoint
    assert "kind: Deployment\nmetadata:\n  name: jaeger" not in rendered
    assert "deploy/jaeger" not in _helm_log(tree)  # no rollout wait for a backend not deployed


def test_jaeger_false_with_tracing_off_is_an_intentional_choice(tree):
    r = _run(tree, ("JAEGER_ENABLED", "false"), ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert rendered_env(rendered, "agent")["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""


def test_jaeger_false_rejection_then_corrected_run_succeeds(tree):
    # Next ordinary run after correcting the config: the refusal left
    # nothing behind that blocks the corrected deploy.
    assert _run(tree, ("JAEGER_ENABLED", "false")).returncode != 0
    r = _run(tree, ("JAEGER_ENABLED", "false"), ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.platform:4318"))
    assert r.returncode == 0, r.stderr
    assert "upgrade" in _helm_log(tree)


def test_upgrade_from_bundled_jaeger_selects_destination_before_removal(tree):
    # Existing bundled Jaeger objects are removed only after the release is
    # upgraded with the explicit destination and the workloads rolled out; the
    # retained Jaeger PVC is never part of the cleanup inventory.
    import json

    path = tree[0] / "disabled.json"
    path.write_text(json.dumps({"kind": "List", "items": [
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "jaeger", "namespace": "ns"}},
    ]}))
    r = _run(tree, ("JAEGER_ENABLED", "false"),
             ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.platform:4318"),
             ("DISABLED_FILE", str(path)))
    assert r.returncode == 0, r.stderr
    values = (tree[0] / "dist" / "mainframe-rag-release-values.yaml").read_text()
    assert 'endpoint: "http://collector.platform:4318"' in values
    cleanup = json.loads((tree[0] / "dist/app-disabled-cleanup.json").read_text())
    assert [i["kind"] for i in cleanup["items"]] == ["Deployment"]
    log = _helm_log(tree)
    assert log.index("delete") > log.index("rollout") > log.index("upgrade")
    assert "jaeger-badger" not in log


def test_jaeger_explicit_true_with_tracing_on(tree):
    r = _run(tree, ("JAEGER_ENABLED", "true"))
    assert r.returncode == 0, r.stderr
    assert (tree[0] / "dist" / "jaeger-rendered.yaml").exists()


def test_jaeger_true_with_tracing_off_fails_closed(tree):
    r = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"), ("JAEGER_ENABLED", "true"))
    assert r.returncode != 0
    assert "JAEGER_ENABLED=true requires tracing" in r.stderr


def test_jaeger_garbage_fails_closed(tree):
    r = _run(tree, ("JAEGER_ENABLED", "maybe"))
    assert r.returncode != 0
    assert "JAEGER_ENABLED must be true/false" in r.stderr


def test_monitor_decoupled_from_exposition(tree):
    r = _run(tree, ("METRICS_ENABLED", "true"), ("SERVICEMONITOR_ENABLED", "false"))
    assert r.returncode == 0, r.stderr
    assert not (tree[0] / "dist" / "servicemonitor-rendered.yaml").exists()
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert rendered_env(rendered, "agent")["METRICS_ENABLED"] == "true"


def test_monitor_true_with_metrics_off_fails_closed(tree):
    r = _run(tree, ("SERVICEMONITOR_ENABLED", "true"))
    assert r.returncode != 0
    assert "SERVICEMONITOR_ENABLED=true requires METRICS_ENABLED=true" in r.stderr


# ----------------------------------------------------- console Route (#373)

SERVICE_CA = "-----BEGIN CERTIFICATE-----\nU0VSVklDRS1DQQ==\n-----END CERTIFICATE-----"
GOOD_PIN = "sha256:" + "b" * 64


def _route(name="rag-agent", *, to="rag-agent", port="oauth", termination="reencrypt",
           insecure="Redirect", ca=SERVICE_CA, alternates=()):
    spec = {
        "to": {"kind": "Service", "name": to},
        "port": {"targetPort": port},
        "tls": {"termination": termination, "insecureEdgeTerminationPolicy": insecure,
                "destinationCACertificate": ca + "\n"},
    }
    if alternates:
        spec["alternateBackends"] = [{"kind": "Service", "name": n} for n in alternates]
    return {"apiVersion": "route.openshift.io/v1", "kind": "Route",
            "metadata": {"name": name, "namespace": "ns"}, "spec": spec}


def _write_json(tree, name, document):
    import json

    path = tree[0] / name
    path.write_text(json.dumps(document))
    return str(path)


def _route_list(tree, *routes):
    return _write_json(tree, "routes.json", {"apiVersion": "v1", "kind": "List", "items": list(routes)})


def _live(tree, route=None):
    return _write_json(tree, "live-route.json", route or _route())


def _route_on(tree, *extra, live=None):
    """Live (non-dry-run) Route-on deploy against stubs."""
    set_oauth_proxy_pin(tree[0], GOOD_PIN)
    return _run(tree, ("AGENT_ROUTE", "true"), ("SERVICE_CA", SERVICE_CA),
                ("ROUTE_LIVE_FILE", live or _live(tree)), *extra)


def test_route_on_live_deploy_verifies_the_actual_route_and_renders_the_cluster_ca(tree):
    r = _route_on(tree)
    assert r.returncode == 0, r.stderr
    assert "Route rag-agent verified" in r.stdout
    log = _helm_log(tree)
    assert "delete" not in log
    # Stored content, not metadata: the rendered Route carries the CA read
    # from the namespace ConfigMap.
    route = (tree[0] / "dist/agent-route.yaml").read_text()
    assert "U0VSVklDRS1DQQ==" in route
    assert "targetPort: oauth" in route and "termination: reencrypt" in route


@pytest.mark.parametrize("drift", [
    {"port": "http"},
    {"termination": "edge"},
    {"insecure": "Allow"},
    {"ca": "-----BEGIN CERTIFICATE-----\nT0xELUNB\n-----END CERTIFICATE-----"},
    {"to": "qdrant"},
    {"alternates": ("qdrant",)},
], ids=["http-port", "edge", "http-allowed", "stale-ca", "wrong-backend", "alternate-backend"])
def test_route_not_converged_after_release_is_deleted_and_fails(tree, drift):
    # Whatever the adopted Route still looks like after the release, it is
    # removed rather than left as a public unauthenticated console.
    r = _route_on(tree, live=_live(tree, _route(**drift)))
    assert r.returncode != 0
    assert "did not converge" in r.stderr
    log = _helm_log(tree)
    assert log.index("delete\nroute.route.openshift.io/rag-agent") > log.index("upgrade")
    assert "rollout" not in log  # fails before any readiness wait


def test_unconverged_route_that_cannot_be_deleted_says_so_loudly(tree):
    r = _route_on(tree, ("DELETE_FAIL", "1"), live=_live(tree, _route(port="http")))
    assert r.returncode != 0
    assert "could not be deleted" in r.stderr and "delete route rag-agent" in r.stderr


def test_route_unreadable_after_release_is_not_a_pass(tree):
    set_oauth_proxy_pin(tree[0], GOOD_PIN)
    r = _run(tree, ("AGENT_ROUTE", "true"), ("SERVICE_CA", SERVICE_CA))
    assert r.returncode != 0
    assert "exposure is NOT verified" in r.stderr


def test_route_refusal_then_corrected_run_succeeds(tree):
    # Next ordinary run: the refused/removed state leaves nothing behind.
    assert _route_on(tree, live=_live(tree, _route(port="http"))).returncode != 0
    assert _route_on(tree).returncode == 0


@pytest.mark.parametrize("backend", ["rag-agent", "qdrant", "qdrant-headless"])
@pytest.mark.parametrize("enabled", [True, False])
def test_other_routes_to_agent_or_qdrant_refuse_before_any_mutation(tree, backend, enabled):
    routes = _route_list(tree, _route("public-console", to=backend))
    extra = (("AGENT_ROUTE", "true"), ("SERVICE_CA", SERVICE_CA)) if enabled else ()
    set_oauth_proxy_pin(tree[0], GOOD_PIN)
    r = _run(tree, ("ROUTES_FILE", routes), ("ROUTE_LIVE_FILE", _live(tree)), *extra)
    assert r.returncode != 0
    assert "Route public-console exposes a protected Service" in r.stderr
    log = _helm_log(tree)
    assert "upgrade" not in log and "delete" not in log and "create" not in log


def test_other_route_alternate_backend_to_qdrant_refuses(tree):
    routes = _route_list(tree, _route("docs", to="docs-site", alternates=("qdrant",)))
    r = _run(tree, ("ROUTES_FILE", routes))
    assert r.returncode != 0
    assert "Route docs exposes" in r.stderr
    assert "upgrade" not in _helm_log(tree)


def test_unrelated_routes_do_not_block_the_deploy(tree):
    routes = _route_list(tree, _route("docs", to="docs-site"))
    r = _run(tree, ("ROUTES_FILE", routes))
    assert r.returncode == 0, r.stderr
    assert "delete" not in _helm_log(tree)


def test_owned_route_with_unowned_alternate_backend_refuses_before_mutation(tree):
    routes = _route_list(tree, _route(alternates=("qdrant",)))
    set_oauth_proxy_pin(tree[0], GOOD_PIN)
    r = _run(tree, ("AGENT_ROUTE", "true"), ("SERVICE_CA", SERVICE_CA),
             ("ROUTES_FILE", routes), ("ROUTE_LIVE_FILE", _live(tree)))
    assert r.returncode != 0
    assert "alternateBackends" in r.stderr
    assert "upgrade" not in _helm_log(tree)


def test_route_off_retires_existing_public_route_before_the_first_mutation(tree):
    # Upgrade from a release with a Route to Route-off: the Route (pointing
    # at the unauthenticated HTTP port) goes first, not after the rollout.
    routes = _route_list(tree, _route(port="http", termination="edge"))
    r = _run(tree, ("ROUTES_FILE", routes))
    assert r.returncode == 0, r.stderr
    log = _helm_log(tree)
    assert log.index("delete\nroute.route.openshift.io/rag-agent") < log.index("upgrade")
    # Next ordinary run: nothing left to retire, no stray delete.
    tree[1].write_text("")
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert "delete" not in _helm_log(tree)


def test_route_off_removal_failure_blocks_the_release(tree):
    routes = _route_list(tree, _route(port="http"))
    r = _run(tree, ("ROUTES_FILE", routes), ("DELETE_FAIL", "1"))
    assert r.returncode != 0
    assert "cannot remove disabled or incompatible owned Route" in r.stderr
    assert "upgrade" not in _helm_log(tree)


@pytest.mark.parametrize("failure", ["ROUTES_READ_FAIL", "DISCOVERY_FAIL"])
def test_unlistable_routes_fail_closed_before_mutation(tree, failure):
    r = _run(tree, (failure, "1"))
    assert r.returncode != 0
    assert "upgrade" not in _helm_log(tree)


@pytest.mark.parametrize("payload", ["", "not-json", '{"kind":"List","items":[{"kind":"Deployment"}]}'])
def test_corrupt_route_inventory_blocks_mutation(tree, payload):
    path = tree[0] / "bad-routes.json"
    path.write_text(payload)
    r = _run(tree, ("ROUTES_FILE", str(path)))
    assert r.returncode != 0
    assert "Route exposure preflight refused" in r.stderr
    assert "upgrade" not in _helm_log(tree)


def test_route_inventory_failure_never_echoes_route_data(tree):
    secret = "PRIVATE-KEY-MATERIAL-DO-NOT-ECHO"
    bad = _route("custom", to="rag-agent")
    bad["spec"]["tls"]["key"] = secret
    r = _run(tree, ("ROUTES_FILE", _route_list(tree, bad)))
    assert r.returncode != 0
    assert secret not in r.stdout + r.stderr + (tree[0] / "helm-args.log").read_text()


def test_invalid_agent_route_value_fails_before_mutation(tree):
    r = _run(tree, ("AGENT_ROUTE", "maybe"))
    assert r.returncode != 0
    assert "AGENT_ROUTE must be true/false" in r.stderr
    assert "upgrade" not in _helm_log(tree)


def test_agent_route_boolean_spellings_select_the_route(tree):
    set_oauth_proxy_pin(tree[0], GOOD_PIN)
    r = _run(tree, ("AGENT_ROUTE", "True"), ("SERVICE_CA", SERVICE_CA), ("ROUTE_LIVE_FILE", _live(tree)))
    assert r.returncode == 0, r.stderr
    assert "ose-oauth-proxy" in (tree[0] / "dist/agent-rendered.yaml").read_text()


def test_missing_cookie_secret_key_blocks_route_before_mutation(tree):
    r = _route_on(tree, ("MISSING_KEY", "cookie-secret"))
    assert r.returncode != 0
    assert "required Secret key is missing or empty: cookie-secret" in r.stderr
    assert "upgrade" not in _helm_log(tree)


@pytest.mark.parametrize("ca", ["", "not a certificate"])
def test_unusable_service_ca_blocks_route_before_mutation(tree, ca):
    set_oauth_proxy_pin(tree[0], GOOD_PIN)
    r = _run(tree, ("AGENT_ROUTE", "true"), ("SERVICE_CA", ca), ("ROUTE_LIVE_FILE", _live(tree)))
    assert r.returncode != 0
    assert "not a PEM certificate bundle" in r.stderr
    assert "upgrade" not in _helm_log(tree)


def test_route_pin_must_match_the_chart_tag(tree):
    # images.txt and the chart tag move together (docs/deploy.md pin note).
    set_oauth_proxy_pin(tree[0], GOOD_PIN)
    images = tree[0] / "images.txt"
    images.write_text(images.read_text().replace("ose-oauth-proxy:v4.14", "ose-oauth-proxy:v4.15"))
    r = _run(tree, ("AGENT_ROUTE", "true"), ("SERVICE_CA", SERVICE_CA), ("ROUTE_LIVE_FILE", _live(tree)))
    assert r.returncode != 0
    assert "does not match the chart" in r.stderr
    assert "upgrade" not in _helm_log(tree)


# ------------------------------------------------- registry image identity (#272)

def _release(tree):
    """A release-path deploy: packed MANIFEST, loaded registry, running pods."""
    _manifest(tree, sha256_bytes(_chart(tree).read_bytes()))
    digests = _load_all(tree)
    _write_pods(tree, digests)
    return digests


def _mutations(tree):
    log = _helm_log(tree) if tree[1].exists() else ""
    return [w for w in ("upgrade", "create", "apply", "delete") if w in log.split()]


def test_release_renders_first_party_images_by_verified_digest(tree):
    digests = _release(tree)
    result = _run(tree)
    assert result.returncode == 0, result.stderr
    values = (tree[0] / "dist" / "mainframe-rag-release-values.yaml").read_text()
    assert f'digest: "{digests["agent"]}"' in values
    assert f'digest: "{digests["jaeger"]}"' in values
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert f"image: reg.internal/qdrant-pdf-rag-agent@{digests['agent']}" in rendered
    assert f"reg.internal/qdrant-pdf-rag-agent:{IMAGE_SHA}" not in rendered
    jaeger = (tree[0] / "dist" / "jaeger-rendered.yaml").read_text()
    assert f"image: reg.internal/jaegertracing/jaeger@{digests['jaeger']}" in jaeger
    assert "running images match the verified registry digests" in result.stdout
    # The vendored Qdrant chart cannot render a digest: its tag stays, the pods are compared.
    assert "image.tag=v1.19.0" in _helm_log(tree)


def test_tag_swapped_after_load_refuses_before_any_mutation(tree):
    _release(tree)
    _put_registry(tree, "agent", _image_manifest("agent", config=_config_of("someone-else")))
    result = _run(tree)
    assert result.returncode != 0
    assert "is not the packed image" in result.stderr
    assert "nothing was deployed" in result.stderr
    assert _mutations(tree) == []


@pytest.mark.parametrize("role", ["qdrant", "agent", "jaeger"])
def test_missing_registry_image_refuses_before_any_mutation(tree, role):
    """pipeline --skip-load / standalone deploy against an empty or partial registry."""
    _release(tree)
    _put_registry(tree, role, None)
    result = _run(tree)
    assert result.returncode != 0
    assert f"cannot read back registry image {ROLE_REFS[role]}" in result.stderr
    assert _mutations(tree) == []


def test_manifest_list_tag_refuses_before_any_mutation(tree):
    _release(tree)
    index = json.dumps({"schemaVersion": 2, "manifests": [{"digest": _config_of("qdrant")}]}).encode()
    _put_registry(tree, "qdrant", index)
    result = _run(tree)
    assert result.returncode != 0
    assert "not a single-image manifest" in result.stderr
    assert _mutations(tree) == []


def test_manifest_without_config_digests_refuses_before_any_mutation(tree):
    _manifest(tree, sha256_bytes(_chart(tree).read_bytes()), config_digests=False)
    _write_pods(tree, _load_all(tree))
    result = _run(tree)
    assert result.returncode != 0
    assert "has no qdrant_config_digest" in result.stderr
    assert _mutations(tree) == []


def test_swapped_tag_refusal_then_reload_passes_next_run(tree):
    digests = _release(tree)
    _put_registry(tree, "agent", _image_manifest("agent", config=_config_of("someone-else")))
    assert _run(tree).returncode != 0
    # A reload overwrites the tag with the packed image; the next ordinary run deploys it.
    _load_all(tree)
    result = _run(tree)
    assert result.returncode == 0, result.stderr
    assert f"@{digests['agent']}" in (tree[0] / "dist" / "agent-rendered.yaml").read_text()


def test_registry_digest_is_read_from_the_registry_not_the_archive(tree):
    """The rendered digest is the registry manifest digest (compressed layers),
    which differs from anything derivable from the archive."""
    digests = _release(tree)
    assert digests["agent"] != _config_of("agent")
    _run(tree)
    values = (tree[0] / "dist" / "mainframe-rag-release-values.yaml").read_text()
    assert digests["agent"] in values
    assert _config_of("agent") not in values


def test_running_pod_with_different_digest_fails_after_rollout(tree):
    digests = _release(tree)
    _write_pods(tree, digests, qdrant="sha256:" + "9" * 64)
    result = _run(tree)
    assert result.returncode != 0
    assert "pod qdrant-0 container qdrant runs sha256:" + "9" * 64 in result.stderr
    assert "running pods do not use the verified registry images" in result.stderr
    assert "upgrade" in _helm_log(tree)


def test_missing_running_pod_for_verified_image_fails(tree):
    digests = _release(tree)
    _write_pods(tree, digests, agent=None)
    result = _run(tree)
    assert result.returncode != 0
    assert "Deployment rag-agent has 0 current pods, expected 1" in result.stderr


def test_dry_run_is_tag_only_and_reads_no_registry(tree):
    _release(tree)
    result = _run(tree, ("AIRGAP_DRYRUN", "1"))
    assert result.returncode == 0, result.stderr
    assert "not release-verified" in result.stderr
    assert "skopeo" not in _helm_log(tree)
    values = (tree[0] / "dist" / "mainframe-rag-release-values.yaml").read_text()
    assert "digest" not in values


def test_digest_from_caller_environment_is_never_rendered(tree):
    result = _run(tree, ("AIRGAP_DRYRUN", "1"), ("IMAGE_DIGEST_AGENT", "sha256:" + "1" * 64))
    assert result.returncode == 0, result.stderr
    assert "digest" not in (tree[0] / "dist" / "mainframe-rag-release-values.yaml").read_text()


@pytest.mark.parametrize("phase", ["Succeeded", "Failed", "Running"])
def test_release_identity_ignores_unrelated_retained_jobs(tree, phase):
    digests = _release(tree)
    path = tree[0] / "pods.json"
    document = json.loads(path.read_text())
    extra = next(item for item in document["items"] if item.get("kind") == "Pod" and item["metadata"]["name"] == "agent-0")
    extra = json.loads(json.dumps(extra))
    extra["metadata"].update(name="diagnostic-history", ownerReferences=[
        {"kind": "Job", "uid": "diagnostic-job", "controller": True}])
    extra["status"].update(phase=phase)
    extra["status"]["containerStatuses"][0]["imageID"] = ROLE_REPOS["agent"] + "@sha256:" + "9" * 64
    document["items"].append(extra)
    path.write_text(json.dumps(document))
    result = _run(tree)
    assert result.returncode == 0, result.stderr
    assert digests["agent"] in result.stdout


def test_release_identity_wrong_target_repository_cannot_use_diagnostic_pod(tree):
    _release(tree)
    path = tree[0] / "pods.json"
    document = json.loads(path.read_text())
    target = next(item for item in document["items"] if item.get("kind") == "Pod" and item["metadata"]["name"] == "agent-0")
    extra = json.loads(json.dumps(target))
    extra["metadata"].update(name="diagnostic", ownerReferences=[
        {"kind": "Job", "uid": "diagnostic-job", "controller": True}])
    document["items"].append(extra)
    target["spec"] = {"containers": [{"name": "agent", "image": "reg.internal/other:tag"}]}
    path.write_text(json.dumps(document))
    result = _run(tree)
    assert result.returncode != 0
    assert "pod agent-0 container agent has an unexpected repository" in result.stderr


def test_release_identity_requires_status_on_each_target_replica(tree):
    _release(tree)
    path = tree[0] / "pods.json"
    document = json.loads(path.read_text())
    target = next(item for item in document["items"] if item.get("kind") == "Pod" and item["metadata"]["name"] == "qdrant-2")
    target["status"]["containerStatuses"] = []
    path.write_text(json.dumps(document))
    result = _run(tree)
    assert result.returncode != 0
    assert "pod qdrant-2 container qdrant runs an unknown digest" in result.stderr


@pytest.mark.parametrize("failure", ["QDRANT_HELM_FAIL", "ROLLOUT_FAIL", "APP_HELM_FAIL", "ROUTE_READ_FAIL"])
@pytest.mark.parametrize("compatible", [False, True], ids=["direct-access", "valid-oauth"])
def test_existing_owned_route_failure_paths_preserve_only_compatible_exposure(tree, failure, compatible):
    existing = _route() if compatible else _route(port="http", termination="edge")
    existing["spec"].update(host="operator.example", wildcardPolicy="None")
    owned_state = tree[0] / "owned-route-state.json"
    owned_state.write_text(json.dumps(existing))
    # Give the process-boundary fake an actual named Route state, separate
    # from the historical inventory snapshot and unrelated operator files.
    kubectl = tree[0] / "bin/kubectl"
    script = kubectl.read_text().replace(
        '[ "${DELETE_FAIL:-}" != 1 ] || exit 1 ;;',
        '[ "${DELETE_FAIL:-}" != 1 ] || exit 1\n'
        '    [ -z "${OWNED_ROUTE_STATE:-}" ] || rm -f "$OWNED_ROUTE_STATE" ;;')
    script = script.replace('[ "${ROUTE_LIVE_FILE:-}" != "" ] || exit 1',
                            '[ "${ROUTE_READ_FAIL:-}" != 1 ] || exit 1\n'
                            '    [ "${ROUTE_LIVE_FILE:-}" != "" ] || exit 1')
    kubectl.write_text(script)
    helm = tree[0] / "bin/helm"
    helm.write_text(helm.read_text().replace('upgrade) exit 0 ;;', '''upgrade)
        case "$*" in
          *'--install mainframe-rag '*)
            [ "${APP_HELM_FAIL:-}" != 1 ] || exit 1
            [ -z "${OWNED_ROUTE_STATE:-}" ] || cp "$ROUTE_LIVE_FILE" "$OWNED_ROUTE_STATE" ;;
          *) [ "${QDRANT_HELM_FAIL:-}" != 1 ] || exit 1 ;;
        esac
        exit 0 ;;'''))
    live = _live(tree, existing if compatible else _route())
    routes = _route_list(tree, existing, _route("operator-docs", to="docs-site"))
    operator_state = tree[0] / "operator-docs-state.json"
    operator_state.write_text(json.dumps(_route("operator-docs", to="docs-site")))
    operator_before = operator_state.read_bytes()
    result = _route_on(tree, ("ROUTES_FILE", routes), ("OWNED_ROUTE_STATE", str(owned_state)),
                       (failure, "1"), live=live)
    assert result.returncode != 0
    log = _helm_log(tree)
    delete = "delete\nroute.route.openshift.io/rag-agent"
    if compatible:
        assert delete not in log
        assert json.loads(owned_state.read_text()) == existing
    else:
        assert log.index(delete) < log.index("upgrade")
        if owned_state.exists():
            assert json.loads(owned_state.read_text())["spec"]["port"]["targetPort"] == "oauth"
        if failure in ("QDRANT_HELM_FAIL", "APP_HELM_FAIL"):
            assert not owned_state.exists()
    assert operator_state.read_bytes() == operator_before
    assert "Route rag-agent -> svc port oauth" not in result.stdout
    if failure == "ROUTE_READ_FAIL":
        assert "exposure is NOT verified" in result.stderr
        assert "Route rag-agent verified:" not in result.stdout
    # The next ordinary operation recovers; no diagnostic history is deleted.
    tree[1].write_text("")
    recovered = _route_on(tree, ("ROUTES_FILE", _route_list(tree, _route())),
                          ("OWNED_ROUTE_STATE", str(owned_state)), live=_live(tree))
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(owned_state.read_text())["spec"]["port"]["targetPort"] == "oauth"
    assert operator_state.read_bytes() == operator_before


@pytest.mark.parametrize("ownership", ["release", "controller", "manager"])
def test_named_operator_owned_route_is_not_retired_or_adopted(tree, ownership):
    route = _route(port="http", termination="edge")
    if ownership == "release":
        route["metadata"]["annotations"] = {"meta.helm.sh/release-name": "operator-release"}
    elif ownership == "controller":
        route["metadata"]["ownerReferences"] = [{"kind": "Operator", "name": "console"}]
    else:
        route["metadata"]["labels"] = {"app.kubernetes.io/managed-by": "operator"}
    result = _route_on(tree, ("ROUTES_FILE", _route_list(tree, route)))
    assert result.returncode != 0
    assert "delete" not in _helm_log(tree) and "upgrade" not in _helm_log(tree)


@pytest.mark.parametrize("malformed", ["layers-object", "config-type", "layer-digest", "index"])
def test_deploy_registry_structure_is_validated_before_mutation(tree, malformed):
    _release(tree)
    manifest = json.loads(_image_manifest("agent"))
    if malformed == "layers-object":
        manifest["layers"] = {"digest": "sha256:" + "a" * 64}
    elif malformed == "config-type":
        manifest["config"]["digest"] = 42
    elif malformed == "layer-digest":
        manifest["layers"][0]["digest"] = "not-a-digest"
    else:
        manifest["manifests"] = []
    _put_registry(tree, "agent", json.dumps(manifest).encode())
    result = _run(tree)
    assert result.returncode != 0
    assert "not a single-image manifest" in result.stderr
    assert _mutations(tree) == []


def test_post_release_operator_ownership_is_not_deleted_on_route_drift(tree):
    foreign = _route(port="http")
    foreign["metadata"]["annotations"] = {"meta.helm.sh/release-name": "operator-release"}
    result = _route_on(tree, live=_live(tree, foreign))
    assert result.returncode != 0
    assert "ownership is not established" in result.stderr
    assert "exposure is NOT verified" in result.stderr
    assert "delete" not in _helm_log(tree)
