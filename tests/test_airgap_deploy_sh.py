"""scripts/airgap/deploy.sh fail-close + knob regressions (issue #15 / PR #32).

Runs deploy.sh with AIRGAP_DRYRUN=1 against a stubbed bin dir and a copied
tree — no cluster, no helm, no network. The PULL_SECRET fail-close is the
regression the rehearsal build surfaced: values.yaml's placeholder pull-secret
name must never reach a cluster.
"""

import json
import re
import shlex
import shutil
import subprocess

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


def test_packaging_pr_gate_checks_vendored_qdrant_chart():
    """The PR packaging job must execute both existing Qdrant checks.

    This pins workflow dispatch only; real pinned-Helm renders prove chart
    validity separately. A printed dry-run upgrade is not either check.
    """
    import yaml

    workflow = yaml.safe_load((REPO / ".github/workflows/e2e.yml").read_text())
    job = workflow["jobs"]["airgap-dryrun"]
    assert job["if"] == "contains(fromJSON(needs.select.outputs.lanes), 'packaging')"
    commands = [
        shlex.split(line)
        for step in job["steps"]
        if not step.get("if") and not step.get("continue-on-error")
        for line in step.get("run", "").splitlines()
        if line.strip().startswith("sh scripts/tools/run-task.sh artifacts:")
    ]
    for task in ("artifacts:helm-lint", "artifacts:helm-render"):
        output = [">", "$RUNNER_TEMP/qdrant-rendered.yaml"] if task == "artifacts:helm-render" else []
        assert any(
            argv[:3] == ["sh", "scripts/tools/run-task.sh", task]
            and argv[3:] == output
            for argv in commands
        ), f"packaging PR job must execute {task} without masking failure"


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
  *'get '*'servicemonitor.monitoring.coreos.com/rag-agent '*)
    case "${{SM_READ:-}}" in
      forbidden) echo 'Error from server (Forbidden): servicemonitors.monitoring.coreos.com "rag-agent" is forbidden: User "site-user" cannot get resource' >&2; exit 1 ;;
      fail) echo 'dial tcp 10.0.0.1:6443: i/o timeout' >&2; exit 1 ;;
    esac
    [ "${{DISABLED_READ_FAIL:-}}" != 1 ] || exit 1
    [ -z "${{SM_FILE:-}}" ] || cat "$SM_FILE" ;;
  *'get deployment.apps/jaeger '*|*'get serviceaccount/rag-agent '*)
    [ "${{DISABLED_READ_FAIL:-}}" != 1 ] || exit 1
    if [ -n "${{DISABLED_FILE:-}}" ]; then cat "$DISABLED_FILE";
    else :; fi ;;
  *'get -f '*'-o json'*)
    [ "${{ACTIVE_READ_FAIL:-}}" != 1 ] || exit 1
    if [ -n "${{OWNERSHIP_FILE:-}}" ]; then cat "$OWNERSHIP_FILE";
    else :; fi ;;

  *'get deployments,statefulsets,replicasets,pods -o json'*)
    [ -z "${{PODS_FILE:-}}" ] || cat "$PODS_FILE" ;;
  *'get secret openobserve-credentials '*'jsonpath='*)
    case "$*" in *root-user-email*) printf 'dGVzdC11c2Vy' ;; *) printf '%s' "${{OO_PASSWORD_B64:-VGVzdC1wYXNzMQ==}}" ;; esac ;;
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
        "EMBED_BASE_URL": "http://vllm:8000/v1",
        "SKOPEO_REGISTRY": str(tmp_path / "registry"),
        "PODS_FILE": str(tmp_path / "pods.json"),
    }
    for k, v in extra_env:
        env[k] = v
    return run_sh(tmp_path / "scripts" / "airgap" / "deploy.sh", env, tmp_path)


def _helm_log(tree):
    # A refusal before any client call leaves no log at all.
    return tree[1].read_text() if tree[1].exists() else ""


def test_pull_secret_wired_agent_render_keeps_mapping(tree):
    # The wired item must reuse the pod-spec indent: a fixed 2-space insert
    # broke out of the mapping and kubectl rejected agent-rendered.yaml
    # ("did not find expected key") in the Kind rehearsal.
    r = _run(tree, ("PULL_SECRET", "ghcr-pull"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert_pull_secret_wired(rendered, "ghcr-pull")
    log = _helm_log(tree)
    assert "imagePullSecrets[0].name=ghcr-pull" in log
    assert "imagePullSecrets=null" not in log


@pytest.mark.parametrize("source", ["file", "environment", "task"])
def test_model_budget_operator_precedence_reaches_rendered_pod(tree, source):
    """Exercise real common.sh, mapper and Helm, plus the public Task bridge.

    File assignments are deliberately unexported; explicit env/Task settings
    must beat conflicting file values and still reach the final Deployment.
    """
    root, log = tree
    selected = {
        "LLM_MAX_MODEL_LEN": "16384", "LLM_RESERVED_OUTPUT_TOKENS": "2048",
        "LLM_THINKING_RESERVE_TOKENS_SIMPLE": "512",
        "LLM_THINKING_RESERVE_TOKENS_COMPLEX": "2048",
        "LLM_REASONING_EFFORT_SIMPLE": "low", "LLM_REASONING_EFFORT_COMPLEX": "medium",
        "LLM_STREAM": "true", "UI_ENABLED": "true",
        "CHAT_CONDENSE_ENABLED": "true", "RERANK_ENABLED": "false",
    }
    alternate = {
        "LLM_MAX_MODEL_LEN": "8192", "LLM_RESERVED_OUTPUT_TOKENS": "1024",
        "LLM_THINKING_RESERVE_TOKENS_SIMPLE": "0",
        "LLM_THINKING_RESERVE_TOKENS_COMPLEX": "1000",
        "LLM_REASONING_EFFORT_SIMPLE": "medium", "LLM_REASONING_EFFORT_COMPLEX": "high",
        "LLM_STREAM": "false", "UI_ENABLED": "false",
        "CHAT_CONDENSE_ENABLED": "false", "RERANK_ENABLED": "true",
    }
    file_values = {
        "INTERNAL_REGISTRY": "reg.internal", "NAMESPACE": "ns",
        "STORAGE_CLASS": "standard", "IMAGE_SHA": IMAGE_SHA,
        "EMBED_MODEL": "embed", "DENSE_DIM": "64", "EMBED_MODEL_REVISION": "rev-1",
        "EMBED_BASE_URL": "http://vllm:8000/v1",
        **(selected if source == "file" else alternate),
    }
    env_file = root / "airgap.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in file_values.items()))
    if source == "task":
        shutil.copyfile(REPO / "Taskfile.yml", root / "Taskfile.yml")
        shutil.copytree(REPO / "taskfiles", root / "taskfiles")
        tools = root / "scripts/tools"
        tools.mkdir()
        for name in ("run-task.sh", "task-pin.txt"):
            shutil.copyfile(REPO / "scripts/tools" / name, tools / name)
        task_bin = root / ".tools/bin"
        task_bin.mkdir(parents=True)
        (task_bin / "task").symlink_to(REPO / ".tools/bin/task")
        result = subprocess.run(
            ["sh", str(tools / "run-task.sh"), "airgap:deploy",
             *[f"{key}={value}" for key, value in selected.items()]],
            capture_output=True, text=True, check=False, cwd=root,
            env={"PATH": f"{root / 'bin'}:/usr/bin:/bin", "HELM_LOG": str(log),
                 "AIRGAP_ENV": str(env_file), "AIRGAP_DRYRUN": "1", **alternate},
        )
    else:
        overrides = selected if source == "environment" else {}
        result = _run(tree, ("AIRGAP_ENV", str(env_file)), *overrides.items())
    assert result.returncode == 0, result.stderr
    pod = rendered_env((root / "dist/agent-rendered.yaml").read_text(), "agent")
    assert {name: pod.get(name) for name in selected} == selected


def test_pull_secret_bad_name_fails_closed(tree):
    r = _run(tree, ("PULL_SECRET", "a&b"))
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


def test_agent_qdrant_write_key_fails_closed(tree):
    """A render wiring the full-access key must stop the deploy before apply."""
    _stub_with_qdrant_key(tree, "key: api-key")
    r = _run(tree)
    assert r.returncode != 0
    assert "read-only-api-key" in r.stderr


ROLE_REFS = {
    "qdrant": "reg.internal/qdrant/qdrant:v1.19.0-unprivileged",
    "agent": f"reg.internal/qdrant-pdf-rag-agent:{IMAGE_SHA}",
    "openobserve": "reg.internal/openobserve/openobserve:v1.1.2",
    "otelcol": "reg.internal/otel/opentelemetry-collector-contrib:0.160.0",
}
ROLE_REPOS = {
    "qdrant": "reg.internal/qdrant/qdrant",
    "agent": "reg.internal/qdrant-pdf-rag-agent",
    "openobserve": "reg.internal/openobserve/openobserve",
    "otelcol": "reg.internal/otel/opentelemetry-collector-contrib",
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
    assert "verified against packed MANIFEST" in result.stdout
    assert str(_chart(tree).relative_to(tree[0])) in _helm_log(tree)
    assert "upgrade" in _helm_log(tree)


def test_dry_run_with_manifest_is_noticed_not_verified(tree):
    _manifest(tree, "0" * 64)
    result = _run(tree, ("AIRGAP_DRYRUN", "1"))
    assert result.returncode == 0, result.stderr
    assert "not release-verified" in result.stdout


def test_missing_production_values_fails_before_mutation(tree):
    (tree[0] / "charts" / "qdrant-openshift.values.yaml").unlink()
    result = _run(tree, ("AIRGAP_DRYRUN", "0"))
    assert result.returncode != 0
    assert "required Qdrant values file is missing or unreadable" in result.stderr
    assert not tree[1].exists(), "no Helm or cluster command may run without base values"


@pytest.mark.parametrize("extra_env,exit_code,messages", [
    pytest.param({'QDRANT_EXTRA_VALUES': '/nonexistent/vals.yaml'}, 1, ('QDRANT_EXTRA_VALUES file not found',),
                 id='missing_extra_values_file_fails_closed'),
    pytest.param({'EMBED_MODEL_REVISION': '  '}, None, ('EMBED_MODEL_REVISION must be a non-blank',),
                 id='whitespace_embed_revision_fails_before_render'),
    pytest.param({'OTEL_EXPORTER_OTLP_ENDPOINT': 'jaeger:4318'}, None, ('must be http(s) or off',),
                 id='tracing_bad_endpoint_fails_closed'),
    pytest.param({'GATEWAY_API_KEY_SECRET': 'Bad_Name!'}, None, ('GATEWAY_API_KEY_SECRET must be a DNS-subdomain name',),
                 id='gateway_secret_bad_name_fails_closed'),
])
def test_invalid_configuration_fails_closed(tree, extra_env, exit_code, messages):
    result = _run(tree, *extra_env.items())
    assert result.returncode != 0 if exit_code is None else result.returncode == exit_code, result.stderr
    for message in messages:
        assert message in result.stderr


def test_rehearsal_knobs_reach_helm(tree):
    vals = tree[0] / "vals.yaml"
    vals.write_text("resources: {}\n")
    r = _run(tree, ("QDRANT_EXTRA_VALUES", str(vals)), ("QDRANT_STORAGE_SIZE", "1Gi"))
    assert r.returncode == 0, r.stderr
    assert "persistence.size=1Gi" in _helm_log(tree)
    assert "snapshotPersistence.size=1Gi" in _helm_log(tree)
    args = _helm_log(tree).splitlines()
    assert str(vals) in args
    assert args[args.index(str(vals)) - 1] == "-f"
    assert args.index("charts/qdrant-openshift.values.yaml") < args.index(str(vals))


def test_rendered_manifest_substituted_and_written(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # Identical default runs share a render; every field oracle stays explicit.
    assert "reg.internal/qdrant-pdf-rag-agent" in rendered
    assert re.search(r"(?m)^\s*- name: EMBED_MODEL_REVISION$", rendered)
    assert rendered_env(rendered, "agent")["EMBED_MODEL_REVISION"] == "rev-1"
    assert_no_placeholders(rendered)
    log = _helm_log(tree)
    assert "imagePullSecrets=null" in log
    assert "PLACEHOLDER" not in log
    block = _qdrant_block((tree[0] / "dist" / "agent-rendered.yaml").read_text())
    assert re.search(r"(?m)^\s*key: read-only-api-key$", block)
    assert not re.search(r"(?m)^\s*key: api-key$", block)
    assert "not release-verified" in r.stdout
    assert (tree[0] / "dist" / "openobserve-rendered.yaml").exists()
    assert "Tracing off" not in r.stdout
    assert rendered_env(rendered, "agent")["IMAGE_SHA"] == IMAGE_SHA
    assert rendered_env(rendered, "agent")["OTEL_DEPLOYMENT_ENVIRONMENT"] == ""
    assert "OTEL_SERVICE_NAME" not in rendered
    assert "__OTEL_SERVICE_NAME__" not in rendered
    assert not (tree[0] / "dist" / "servicemonitor-rendered.yaml").exists()
    assert re.search(r'METRICS_ENABLED\n\s+value: "false"', rendered, re.MULTILINE)
    assert "Metrics off" in r.stdout
    assert 'value: "false"' in rendered or "value: false" in rendered
    assert "__RERANK_" not in rendered
    assert rendered_env(rendered, "agent")["RERANK_ENDPOINT_ORDER"] == "score_first"
    assert "__RERANK_ENDPOINT_ORDER__" not in rendered
    for env_name in ("LLM_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY"):
        assert env_name not in rendered
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert "QDRANT_API_KEY" in rendered
    assert "RERANK_MODEL" in rendered
    assert "Gateway keys off" in r.stdout


# --------------------------------------------- OpenObserve / tracing (#83, #956)


def test_tracing_on_deploys_openobserve_and_off_removes_it(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    oo = (tree[0] / "dist" / "openobserve-rendered.yaml").read_text()
    agent = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert "reg.internal/openobserve/openobserve:v1.1.2" in oo
    assert "namespace: ns" in oo
    assert_no_placeholders(oo)
    assert_no_placeholders(agent)
    assert "deploy/openobserve" in _helm_log(tree)
    # The collector scrapes /metrics: metrics off renders no otelcol, so no wait
    # and no image check for it; metrics on waits for it.
    assert "deploy/otelcol" not in _helm_log(tree)
    on = _run(tree, ("METRICS_ENABLED", "true"))
    assert on.returncode == 0, on.stderr
    assert "deploy/otelcol" in _helm_log(tree)
    assert "name: otelcol" in (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert "name: otelcol" not in agent
    # The off sentinel renders no backend and waits for none.
    off = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"))
    assert off.returncode == 0, off.stderr
    assert not (tree[0] / "dist" / "openobserve-rendered.yaml").exists()
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert rendered_env(rendered, "agent")["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""
    assert "OpenObserve not deployed" in off.stdout
    # An external OTLP endpoint (tracing.endpoint escape hatch) renders no
    # backend either: no Secret check, identity check, render or rollout wait.
    ext = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.platform:4318"))
    assert ext.returncode == 0, ext.stderr
    assert not (tree[0] / "dist" / "openobserve-rendered.yaml").exists()
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert rendered_env(rendered, "agent")["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector.platform:4318"
    assert "name: openobserve" not in rendered and "name: otelcol" not in rendered
    assert "OpenObserve not deployed" in ext.stdout


@pytest.mark.parametrize("role", ["openobserve", "otelcol"])
def test_openobserve_and_otelcol_are_identity_checked_when_tracing_is_on(tree, role):
    """#956: DEPLOY_IMAGES carries both, so a missing registry image refuses
    before any mutation, and the next run with the image loaded deploys."""
    _release(tree)
    _put_registry(tree, role, None)
    metrics = (("METRICS_ENABLED", "true"),)  # otelcol renders only with metrics on
    refused = _run(tree, *metrics)
    assert refused.returncode != 0
    assert f"cannot read back registry image {ROLE_REFS[role]}" in refused.stderr
    assert _mutations(tree) == []
    _load_all(tree)
    ok = _run(tree, *metrics)
    assert ok.returncode == 0, ok.stderr


@pytest.mark.parametrize("password", ["abcdef0123456789abcdef", "Short1!", "NOLOWER-123", "nodigit-Aaaa", "NoSpecial123x", "x" * 129 + "Aa1!"])
def test_weak_openobserve_root_password_refuses_before_any_mutation(tree, password):
    """OpenObserve v1.1.2 crash-loops on a weak root password: refuse it up front
    with a stable code and never echo the value."""
    import base64
    r = _run(tree, ("OO_PASSWORD_B64", base64.b64encode(password.encode()).decode()))
    assert r.returncode != 0
    assert "E_OPENOBSERVE_WEAK_PASSWORD" in r.stderr
    assert password not in r.stdout + r.stderr and base64.b64encode(password.encode()).decode() not in r.stdout + r.stderr
    assert _mutations(tree) == []
    assert _helm_log(tree) == "" or "upgrade" not in _helm_log(tree)
    ok = _run(tree, ("OO_PASSWORD_B64", base64.b64encode(b"Strong-pass-1").decode()))
    assert ok.returncode == 0, ok.stderr
    # Tracing off or an external endpoint never reads the Secret.
    off = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"), ("OO_PASSWORD_B64", base64.b64encode(password.encode()).decode()))
    assert off.returncode == 0, off.stderr


@pytest.mark.parametrize("missing_key", ["root-user-email", "root-user-password", "otlp-headers", "metrics-auth"])
def test_missing_openobserve_credentials_refuse_before_any_mutation(tree, missing_key):
    r = _run(tree, ("MISSING_KEY", missing_key))
    assert r.returncode != 0
    assert "E_OPENOBSERVE_CREDENTIALS" in r.stderr
    assert "openobserve-credentials" in r.stderr
    assert _mutations(tree) == []
    # Tracing off does not need the Secret; the next ordinary run succeeds.
    ok = _run(tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"), ("MISSING_KEY", missing_key))
    assert ok.returncode == 0, ok.stderr


# ------------------------------------------------------- deploy identity (Phase 2b)


# ------------------------------------------------------- ServiceMonitor (#187)


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
    # Only the literal "true" opts in: "false"/"1"/empty all stay off.
    for value in ("false", "1", ""):
        (tree[0] / "dist" / "servicemonitor-rendered.yaml").unlink(missing_ok=True)
        off = _run(tree, ("METRICS_ENABLED", value))
        assert off.returncode == 0, off.stderr
        assert not (tree[0] / "dist" / "servicemonitor-rendered.yaml").exists()


# ------------------------------------------------------- gateway keys (LiteLLM)


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


def _policy_inventory(tree, **meta_extra):
    meta = {"name": "rag-agent-ingress", "namespace": "ns", **meta_extra}
    path = tree[0] / "existing-policy.json"
    path.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta},
    ]}))
    return str(path)


def test_agent_networkpolicy_is_rendered_by_default_and_announced(tree):
    """Issue #373: default ON, in the one rendered application manifest."""
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert "kind: NetworkPolicy" in rendered and "name: rag-agent-ingress" in rendered
    assert "==> NetworkPolicy rag-agent-ingress: agent HTTP 8080 only from this namespace" in r.stdout


def test_next_ordinary_deploy_adopts_the_owned_networkpolicy(tree):
    """The second deploy finds the release's own policy in the live inventory.
    Without it in check_app_ownership.ALLOWED the preflight refused the
    deploy that created it (lifecycle, not just first install)."""
    inventory = _policy_inventory(tree, annotations={
        "meta.helm.sh/release-name": "mainframe-rag", "meta.helm.sh/release-namespace": "ns"},
        labels={"app.kubernetes.io/managed-by": "Helm"})
    r = _run(tree, ("OWNERSHIP_FILE", inventory))
    assert r.returncode == 0, r.stderr
    log = _helm_log(tree)
    assert "upgrade" in log and "ownership preflight refused" not in r.stderr


@pytest.mark.parametrize("foreign", [
    {"annotations": {"meta.helm.sh/release-name": "other"}},
    {"annotations": {"meta.helm.sh/release-namespace": "elsewhere"}},
    {"ownerReferences": [{"name": "operator"}]},
    {"labels": {"app.kubernetes.io/managed-by": "other"}},
])
def test_foreign_networkpolicy_is_refused_before_any_mutation(tree, foreign):
    r = _run(tree, ("OWNERSHIP_FILE", _policy_inventory(tree, **foreign)))
    assert r.returncode != 0
    assert "ownership preflight refused" in r.stderr
    assert "upgrade" not in _helm_log(tree)


def test_other_networkpolicy_names_are_not_application_inventory(tree):
    path = tree[0] / "existing.json"
    path.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "site-default-deny", "namespace": "ns"}}]}))
    r = _run(tree, ("OWNERSHIP_FILE", str(path)))
    assert r.returncode != 0
    assert "upgrade" not in _helm_log(tree)


def test_disabling_the_networkpolicy_leaves_removal_to_the_release(tree):
    """Helm owns the policy it created: the disabled render omits it, the
    release upgrade prunes it, and the deploy needs no extra cleanup entry
    (the disabled-cleanup inventory stays for pre-Helm legacy objects)."""
    r = _run(tree, ("AGENT_NETWORK_POLICY", "false"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    # AGENT_NETWORK_POLICY governs only the agent ingress policy (docs/deploy.md
    # #agent-networkpolicy); the OpenObserve egress-deny policy is its own
    # object and stays rendered.
    assert "name: rag-agent-ingress" not in rendered
    assert "name: openobserve-egress-deny" in rendered
    assert "off (AGENT_NETWORK_POLICY=false)" in r.stdout
    assert "upgrade" in _helm_log(tree)
    assert "NetworkPolicy" not in (tree[0] / "dist/app-disabled-cleanup.json").read_text()


def test_networkpolicy_allowed_namespaces_reach_the_rendered_manifest(tree):
    r = _run(tree, ("AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES", "mcp,tools-1"))
    assert r.returncode == 0, r.stderr
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert 'kubernetes.io/metadata.name: "mcp"' in rendered
    assert 'kubernetes.io/metadata.name: "tools-1"' in rendered
    assert "AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES (mcp,tools-1)" in r.stdout


@pytest.mark.parametrize("env", [
    (("AGENT_NETWORK_POLICY", "maybe"),),
    (("AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES", "Bad_Name"),),
    (("AGENT_NETWORK_POLICY", "false"), ("AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES", "mcp")),
])
def test_invalid_networkpolicy_selection_fails_before_any_mutation(tree, env):
    r = _run(tree, *env)
    assert r.returncode != 0
    log = _helm_log(tree)
    assert "upgrade" not in log and "new-project" not in log


@pytest.mark.parametrize("ownership", ["legacy", "ours", "other-release", "controller", "other-manager"])
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


@pytest.mark.parametrize("key", ["llm-api-key", ".dockerconfigjson"])
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


@pytest.mark.parametrize("payload", ['{"kind":"List"}'])
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


@pytest.mark.parametrize("owner", ["legacy", "other", "controller"])
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


def test_forbidden_servicemonitor_read_is_a_notice_for_namespace_admins(tree):
    """#680: OpenShift's namespace admin role cannot read monitoring.coreos.com.
    The disabled ServiceMonitor check becomes a notice; deploy proceeds and the
    client's text (it names the user) is not echoed."""
    result = _run(tree, ("SM_READ", "forbidden"))
    assert result.returncode == 0, result.stderr
    assert "may not read ServiceMonitors (Forbidden)" in result.stdout
    assert "site-user" not in result.stdout + result.stderr
    log = _helm_log(tree)
    assert "upgrade" in log
    assert "delete" not in log


def test_other_servicemonitor_read_error_still_blocks_mutations(tree):
    result = _run(tree, ("SM_READ", "fail"))
    assert result.returncode != 0
    assert "cannot read disabled ServiceMonitor rag-agent (unexpected client error)" in result.stderr
    assert "10.0.0.1" not in result.stdout + result.stderr
    assert "upgrade" not in _helm_log(tree)


def test_readable_owned_leftover_servicemonitor_is_still_cleaned(tree):
    import json

    path = tree[0] / "sm.json"
    path.write_text(json.dumps({"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor",
                                "metadata": {"name": "rag-agent", "namespace": "ns"}}))
    result = _run(tree, ("SM_FILE", str(path)))
    assert result.returncode == 0, result.stderr
    cleanup = json.loads((tree[0] / "dist/app-disabled-cleanup.json").read_text())
    assert {"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor",
            "metadata": {"name": "rag-agent", "namespace": "ns"}} in cleanup["items"]
    log = _helm_log(tree)
    assert log.index("delete") > log.index("rollout") > log.index("upgrade")


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


@pytest.mark.parametrize("from_file", [False])
def test_shared_gateway_aliases_and_secret_reach_requests(tree, from_file):
    import json

    import httpx2

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.embed import VllmEmbedder
    from mainframe_rag.ports import ChatMessage
    from tests.helpers_airgap import rendered_container

    options = {
        "EMBED_BASE_URL": "",  # no explicit override: the shared gateway applies
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
        # Issue #868: a buffered answer is read from the streamed wire.
        sse = ('data: {"choices": [{"delta": {"content": "Synthetic answer"}}]}\n\n'
               'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
               "data: [DONE]\n\n")
        return httpx2.Response(200, text=sse, headers={"content-type": "text/event-stream"})

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
    result = _run(tree, ("AIRGAP_ENV", str(path)), ("EMBED_BASE_URL", ""),
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


@pytest.mark.parametrize("key,secret", [('bad"key', "shared"), ("api-key", "")])
def test_invalid_shared_gateway_key_refuses_before_commands(tree, key, secret):
    result = _run(tree, ("GATEWAY_API_KEY_SECRET", secret), ("GATEWAY_API_KEY_SECRET_KEY", key))
    assert result.returncode != 0
    assert "GATEWAY_API_KEY_SECRET_KEY" in result.stderr
    assert not tree[1].exists()


# --------------------------------- Decoupled backends (issue #529 OBS-2)


def test_retired_jaeger_switch_is_ignored_and_external_collector_passes_through(tree):
    # #956: OpenObserve is the only bundled backend; JAEGER_ENABLED is gone.
    r = _run(tree, ("JAEGER_ENABLED", "false"), ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.platform:4318"))
    assert r.returncode == 0, r.stderr
    values = (tree[0] / "dist" / "mainframe-rag-release-values.yaml").read_text()
    assert 'endpoint: "http://collector.platform:4318"' in values
    assert "jaeger" not in values.lower()


def test_upgrade_from_bundled_jaeger_removes_it_after_rollout(tree):
    # Leftover Jaeger objects are removed only after the release is upgraded
    # and the workloads rolled out; the retained Jaeger PVC is never listed.
    import json

    path = tree[0] / "disabled.json"
    path.write_text(json.dumps({"kind": "List", "items": [
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "jaeger", "namespace": "ns"}},
    ]}))
    r = _run(tree, ("DISABLED_FILE", str(path)))
    assert r.returncode == 0, r.stderr
    cleanup = json.loads((tree[0] / "dist/app-disabled-cleanup.json").read_text())
    assert [i["kind"] for i in cleanup["items"]] == ["Deployment"]
    log = _helm_log(tree)
    assert log.index("delete") > log.index("rollout") > log.index("upgrade")
    assert "jaeger-badger" not in log


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
    {"ca": "-----BEGIN CERTIFICATE-----\nT0xELUNB\n-----END CERTIFICATE-----"},
    {"alternates": ("qdrant",)},
], ids=["http-port", "stale-ca", "alternate-backend"])
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


@pytest.mark.parametrize(("backend", "enabled"), [
    ("rag-agent", True), ("rag-agent", False), ("qdrant", False), ("qdrant-headless", False),
    # #804: the content store is a protected Service whether or not it is enabled.
    ("rag-feedback", False), ("rag-feedback", True),
])
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


@pytest.mark.parametrize("payload", ["not-json", '{"kind":"List","items":[{"kind":"Deployment"}]}'])
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


def test_missing_cookie_secret_key_blocks_route_before_mutation(tree):
    r = _route_on(tree, ("MISSING_KEY", "cookie-secret"))
    assert r.returncode != 0
    assert "required Secret key is missing or empty: cookie-secret" in r.stderr
    assert "upgrade" not in _helm_log(tree)


@pytest.mark.parametrize("ca", ["not a certificate"])
def test_unusable_service_ca_blocks_route_before_mutation(tree, ca):
    set_oauth_proxy_pin(tree[0], GOOD_PIN)
    r = _run(tree, ("AGENT_ROUTE", "true"), ("SERVICE_CA", ca), ("ROUTE_LIVE_FILE", _live(tree)))
    assert r.returncode != 0
    assert "not a PEM certificate bundle" in r.stderr
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
    # Chart values carry repository and tag only; the digests are enforced at
    # pack/load and by the registry and running-pod checks (#956).
    assert 'repository: "reg.internal/openobserve/openobserve"' in values
    assert 'tag: "v1.1.2"' in values
    assert digests["openobserve"] not in values and digests["otelcol"] not in values
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert f"image: reg.internal/qdrant-pdf-rag-agent@{digests['agent']}" in rendered
    assert f"reg.internal/qdrant-pdf-rag-agent:{IMAGE_SHA}" not in rendered
    assert "running images match the verified registry digests" in result.stdout
    # The vendored Qdrant chart cannot render a digest: its tag stays, the pods are compared.
    assert "image.tag=v1.19.0" in _helm_log(tree)


@pytest.mark.parametrize("role", ["openobserve", "otelcol"])
def test_missing_registry_image_refuses_before_any_mutation(tree, role):
    """pipeline --skip-load / standalone deploy against an empty or partial registry."""
    _release(tree)
    _put_registry(tree, role, None)
    result = _run(tree, ("METRICS_ENABLED", "true"))  # otelcol renders only with metrics on
    assert result.returncode != 0
    assert f"cannot read back registry image {ROLE_REFS[role]}" in result.stderr
    assert _mutations(tree) == []


def test_manifest_without_config_digests_refuses_before_any_mutation(tree):
    _manifest(tree, sha256_bytes(_chart(tree).read_bytes()), config_digests=False)
    _write_pods(tree, _load_all(tree))
    result = _run(tree)
    assert result.returncode != 0
    assert "has no qdrant_config_digest" in result.stderr
    assert _mutations(tree) == []


def test_swapped_tag_refuses_before_mutation_then_reload_passes_next_run(tree):
    digests = _release(tree)
    _put_registry(tree, "agent", _image_manifest("agent", config=_config_of("someone-else")))
    refused = _run(tree)
    assert refused.returncode != 0
    assert "is not the packed image" in refused.stderr
    assert "nothing was deployed" in refused.stderr
    assert _mutations(tree) == []
    # A reload overwrites the tag with the packed image; the next ordinary run deploys it.
    _load_all(tree)
    result = _run(tree)
    assert result.returncode == 0, result.stderr
    assert f"@{digests['agent']}" in (tree[0] / "dist" / "agent-rendered.yaml").read_text()


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


@pytest.mark.parametrize("phase", ["Failed"])
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


@pytest.mark.parametrize(("failure", "compatible"), [
    ("QDRANT_HELM_FAIL", False), ("ROUTE_READ_FAIL", True),
], ids=["direct-access-before-release", "valid-oauth-unreadable-after-release"])
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


@pytest.mark.parametrize("ownership", ["release"])
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


@pytest.mark.parametrize("malformed", ["layer-digest", "index"])
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


# ------------------------------------------------ feedback service (#804)

FEEDBACK_ENV = (("FEEDBACK_ENABLED", "true"), ("FEEDBACK_SECRET", "fb-secret"))
FEEDBACK_OBJECTS = [
    ("apps/v1", "StatefulSet", "rag-feedback"),
    ("v1", "Service", "rag-feedback"),
    ("v1", "PersistentVolumeClaim", "rag-feedback-data"),
    ("networking.k8s.io/v1", "NetworkPolicy", "rag-feedback-ingress"),
    ("monitoring.coreos.com/v1", "ServiceMonitor", "rag-feedback"),
]
HELM_OWNED = {"annotations": {"meta.helm.sh/release-name": "mainframe-rag",
                              "meta.helm.sh/release-namespace": "ns"},
              "labels": {"app.kubernetes.io/managed-by": "Helm"}}


def _live_inventory(tree, objects, **meta_extra):
    path = tree[0] / "existing-feedback.json"
    path.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": "ns", **meta_extra}}
        for api, kind, name in objects]}))
    return str(path)


def test_feedback_is_off_by_default_and_the_deploy_is_unchanged(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stderr
    assert "==> Feedback service off (FEEDBACK_ENABLED=false)" in r.stdout
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert "rag-feedback" not in rendered and "FEEDBACK" not in rendered
    assert "rag-feedback" not in _helm_log(tree)
    assert "rag-feedback" not in (tree[0] / "dist" / "mainframe-rag-release-values.yaml").read_text()


def test_feedback_enabled_renders_checks_the_secret_key_and_waits_for_the_statefulset(tree):
    r = _run(tree, *FEEDBACK_ENV, ("METRICS_ENABLED", "true"), ("FEEDBACK_RETENTION_DAYS", "90"),
             ("FEEDBACK_STORAGE_SIZE", "20Gi"))
    assert r.returncode == 0, r.stderr
    assert "==> Feedback service on: StatefulSet rag-feedback (1 replica)" in r.stdout
    assert "token from Secret fb-secret (key service-token)" in r.stdout
    values = (tree[0] / "dist" / "mainframe-rag-release-values.yaml").read_text()
    assert 'secretName: "fb-secret"' in values and "retentionDays: 90" in values
    assert 'storageSize: "20Gi"' in values
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    for kind, name in (("StatefulSet", "rag-feedback"), ("Service", "rag-feedback"),
                       ("PersistentVolumeClaim", "rag-feedback-data"),
                       ("NetworkPolicy", "rag-feedback-ingress"), ("ServiceMonitor", "rag-feedback")):
        assert re.search(rf"kind: {kind}\nmetadata:\n  name: {name}\n", rendered), (kind, name)
    assert "FEEDBACK_URL" in rendered and "name: FEEDBACK_API_KEY" in rendered
    log = _helm_log(tree)
    assert "get secret fb-secret" in log.replace("\n", " ")  # key presence only, via go-template
    assert "statefulset/rag-feedback" in log
    # The agent rollout is still waited for and the PVC is never a delete target.
    assert "deploy/rag-agent" in log and "persistentvolumeclaim" not in log.lower()


def test_missing_feedback_service_token_key_blocks_before_mutation(tree):
    r = _run(tree, *FEEDBACK_ENV, ("MISSING_KEY", "service-token"))
    assert r.returncode != 0
    assert "required Secret key is missing or empty: service-token" in r.stderr
    assert "upgrade" not in _helm_log(tree)


@pytest.mark.parametrize(("env", "message"), [
    ((("FEEDBACK_ENABLED", "true"),), "FEEDBACK_SECRET is required"),
    ((("FEEDBACK_ENABLED", "maybe"),), "FEEDBACK_ENABLED must be true/false"),
    ((("FEEDBACK_ENABLED", "true"), ("FEEDBACK_SECRET", "Bad_Name")), "FEEDBACK_SECRET must be"),
    (FEEDBACK_ENV + (("AGENT_NETWORK_POLICY", "false"),), "requires AGENT_NETWORK_POLICY to be enabled"),
    (FEEDBACK_ENV + (("FEEDBACK_STORAGE_SIZE", "ten"),), "FEEDBACK_STORAGE_SIZE must be"),
    (FEEDBACK_ENV + (("FEEDBACK_RETENTION_DAYS", "0"),), "FEEDBACK_RETENTION_DAYS must be"),
    (FEEDBACK_ENV + (("FEEDBACK_RETENTION_DAYS", "3651"),), "FEEDBACK_RETENTION_DAYS must be"),
])
def test_invalid_feedback_selection_fails_before_any_mutation(tree, env, message):
    r = _run(tree, *env)
    assert r.returncode != 0
    assert message in r.stderr
    log = _helm_log(tree)
    assert "upgrade" not in log and "new-project" not in log


OPENOBSERVE_OBJECTS = [
    ("apps/v1", "Deployment", "openobserve"),
    ("v1", "Service", "openobserve"),
    ("v1", "PersistentVolumeClaim", "openobserve-data"),
    ("networking.k8s.io/v1", "NetworkPolicy", "openobserve-egress-deny"),
    ("apps/v1", "Deployment", "otelcol"),
    ("v1", "ConfigMap", "otelcol-config"),
    ("networking.k8s.io/v1", "NetworkPolicy", "otelcol-egress"),
]


def test_next_ordinary_deploy_adopts_every_owned_openobserve_object(tree):
    """Lifecycle, not first install: the deploy after the one that created
    OpenObserve and otelcol finds all seven objects live and must proceed
    (kind-lifecycle: 'application ownership preflight refused')."""
    inventory = _live_inventory(tree, OPENOBSERVE_OBJECTS, **HELM_OWNED)
    r = _run(tree, ("METRICS_ENABLED", "true"), ("OWNERSHIP_FILE", inventory))
    assert r.returncode == 0, r.stderr
    assert "ownership preflight refused" not in r.stderr
    assert "upgrade" in _helm_log(tree)


@pytest.mark.parametrize("obj", OPENOBSERVE_OBJECTS, ids=[o[2] for o in OPENOBSERVE_OBJECTS])
def test_foreign_openobserve_objects_are_refused_before_any_mutation(tree, obj):
    inventory = _live_inventory(tree, [obj], annotations={"meta.helm.sh/release-name": "other"})
    r = _run(tree, ("METRICS_ENABLED", "true"), ("OWNERSHIP_FILE", inventory))
    assert r.returncode != 0
    assert "ownership preflight refused" in r.stderr
    assert "upgrade" not in _helm_log(tree)


def test_next_ordinary_deploy_adopts_every_owned_feedback_object(tree):
    """The deploy after the one that created the feedback service finds all five
    objects in the live inventory. Without them in check_app_ownership.ALLOWED
    the preflight refused the deploy that created them (lifecycle, not first
    install: the #812 failure)."""
    inventory = _live_inventory(tree, FEEDBACK_OBJECTS, **HELM_OWNED)
    r = _run(tree, *FEEDBACK_ENV, ("METRICS_ENABLED", "true"), ("OWNERSHIP_FILE", inventory))
    assert r.returncode == 0, r.stderr
    assert "ownership preflight refused" not in r.stderr
    assert "upgrade" in _helm_log(tree)


@pytest.mark.parametrize("foreign", [
    {"annotations": {"meta.helm.sh/release-name": "other"}},
    {"annotations": {"meta.helm.sh/release-namespace": "elsewhere"}},
    {"ownerReferences": [{"name": "operator"}]},
    {"labels": {"app.kubernetes.io/managed-by": "other"}},
])
@pytest.mark.parametrize("obj", FEEDBACK_OBJECTS, ids=[o[1] for o in FEEDBACK_OBJECTS])
def test_foreign_feedback_objects_are_refused_before_any_mutation(tree, obj, foreign):
    inventory = _live_inventory(tree, [obj], **foreign)
    r = _run(tree, *FEEDBACK_ENV, ("OWNERSHIP_FILE", inventory))
    assert r.returncode != 0
    assert "ownership preflight refused" in r.stderr
    assert "upgrade" not in _helm_log(tree)


def test_unlisted_feedback_name_is_not_application_inventory(tree):
    inventory = _live_inventory(tree, [("v1", "PersistentVolumeClaim", "rag-feedback-data-old")])
    r = _run(tree, *FEEDBACK_ENV, ("OWNERSHIP_FILE", inventory))
    assert r.returncode != 0 and "upgrade" not in _helm_log(tree)


def test_disable_keeps_the_claim_and_re_enable_adopts_it(tree):
    """D6 lifecycle: disabling renders no feedback object and adds no cleanup
    entry (a PVC is never in the cleanup inventory), the claim survives, and the
    next ordinary enabling deploy adopts the retained claim and proceeds."""
    disabled = _run(tree)
    assert disabled.returncode == 0, disabled.stderr
    assert "rag-feedback" not in (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    cleanup = (tree[0] / "dist" / "app-disabled-cleanup.json")
    assert not cleanup.exists() or "rag-feedback" not in cleanup.read_text()
    assert "delete" not in _helm_log(tree).split()
    # Only the retained claim is live (workload, Service and policy were pruned).
    retained = _live_inventory(
        tree, [("v1", "PersistentVolumeClaim", "rag-feedback-data")], **HELM_OWNED)
    enabled = _run(tree, *FEEDBACK_ENV, ("OWNERSHIP_FILE", retained))
    assert enabled.returncode == 0, enabled.stderr
    assert "--take-ownership" in _helm_log(tree)
    assert "kind: PersistentVolumeClaim\nmetadata:\n  name: rag-feedback-data" in (
        tree[0] / "dist" / "agent-rendered.yaml").read_text()


def _write_feedback_pod(tree, digest: str, *, replicas: int = 1) -> None:
    pods = json.loads((tree[0] / "pods.json").read_text())
    repo = ROLE_REPOS["agent"]
    spec = {"containers": [{"name": "feedback", "image": f"{repo}:tag"}]}
    pods["items"].append({
        "kind": "StatefulSet", "metadata": {"name": "rag-feedback", "namespace": "ns", "uid": "fb-sts"},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "rag-feedback"}},
                 "template": {"spec": spec}}, "status": {"updateRevision": "fb-rev"}})
    for n in range(replicas):
        pods["items"].append({
            "kind": "Pod", "metadata": {"name": f"rag-feedback-{n}", "namespace": "ns",
                "labels": {"app": "rag-feedback", "controller-revision-hash": "fb-rev"},
                "ownerReferences": [{"kind": "StatefulSet", "uid": "fb-sts", "controller": True}]},
            "spec": spec, "status": {"phase": "Running", "containerStatuses": [
                {"name": "feedback", "imageID": f"{repo}@{digest}"}]}})
    (tree[0] / "pods.json").write_text(json.dumps(pods))


def test_release_path_verifies_the_feedback_pod_against_the_agent_digest(tree):
    digests = _release(tree)
    _write_feedback_pod(tree, digests["agent"])
    r = _run(tree, *FEEDBACK_ENV)
    assert r.returncode == 0, r.stderr
    assert "running images match the verified registry digests (4 roles)" in r.stdout
    values = (tree[0] / "dist" / "mainframe-rag-release-values.yaml").read_text()
    assert f'digest: "{digests["agent"]}"' in values
    rendered = (tree[0] / "dist" / "agent-rendered.yaml").read_text()
    assert rendered.count(f"image: reg.internal/qdrant-pdf-rag-agent@{digests['agent']}") == 2


@pytest.mark.parametrize("problem", ["wrong-digest", "missing-pod"])
def test_feedback_pod_with_another_image_or_no_pod_fails_after_rollout(tree, problem):
    digests = _release(tree)
    if problem == "wrong-digest":
        _write_feedback_pod(tree, "sha256:" + "9" * 64)
    else:
        _write_feedback_pod(tree, digests["agent"], replicas=0)
    r = _run(tree, *FEEDBACK_ENV)
    assert r.returncode != 0
    assert "running pods do not use the verified registry images" in r.stderr
    if problem == "wrong-digest":
        assert "pod rag-feedback-0 container feedback runs sha256:" + "9" * 64 in r.stderr
    else:
        assert "StatefulSet rag-feedback has 0 current pods, expected 1" in r.stderr


def test_default_smoke_gets_trace_query_settings_without_manual_env(tree):
    """#956: with tracing on, airgap smoke needs no TRACE_QUERY_* env: the URL
    is the in-cluster OpenObserve and the credentials come from the Secret."""
    script = ('. scripts/airgap/common.sh; NAMESPACE=ns; KC=kubectl; OTEL_TRACING_ENABLED=1; '
              'resolve_trace_query; printf "%s|%s|%s" "${TRACE_QUERY_URL-}" "${TRACE_QUERY_USER-}" "${TRACE_QUERY_PASSWORD-}"')
    env = {"PATH": f"{tree[0] / 'bin'}:/usr/bin:/bin", "HELM_LOG": str(tree[1])}
    r = subprocess.run(["sh", "-c", script], cwd=tree[0], env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "http://openobserve:5080|test-user|Test-pass1"
    off = subprocess.run(["sh", "-c", script.replace("OTEL_TRACING_ENABLED=1", "OTEL_TRACING_ENABLED=0")],
                         cwd=tree[0], env=env, capture_output=True, text=True, check=False)
    assert off.stdout == "||"


def test_external_otlp_endpoint_has_no_bundled_trace_query_default(tree):
    """#956: with tracing.endpoint set there is no bundled OpenObserve, so no
    default URL or Secret read is applied; operator values pass through and
    smoke refuses empty ones."""
    base = ('. scripts/airgap/common.sh; NAMESPACE=ns; KC=kubectl; OTEL_TRACING_ENABLED=1; OTEL_BUNDLED=0; '
            'resolve_trace_query; printf "%s|%s" "${TRACE_QUERY_URL-}" "${TRACE_QUERY_USER-}"')
    env = {"PATH": f"{tree[0] / 'bin'}:/usr/bin:/bin", "HELM_LOG": str(tree[1])}
    unset = subprocess.run(["sh", "-c", base], cwd=tree[0], env=env, capture_output=True, text=True, check=False)
    assert unset.returncode == 0 and unset.stdout == "|"
    given = subprocess.run(
        ["sh", "-c", base], cwd=tree[0], capture_output=True, text=True, check=False,
        env={**env, "TRACE_QUERY_URL": "http://oo.platform:5080", "TRACE_QUERY_USER": "u", "TRACE_QUERY_PASSWORD": "p"})
    assert given.returncode == 0, given.stderr
    assert given.stdout == "http://oo.platform:5080|u"


def test_task_runner_empty_trace_query_vars_get_bundled_defaults(tree):
    """The task runner exports every operator key, empty when unset: on the
    bundled path empty means unset (defaults and Secret credentials), while a
    non-empty operator value still wins."""
    script = ('. scripts/airgap/common.sh; NAMESPACE=ns; KC=kubectl; OTEL_TRACING_ENABLED=1; OTEL_BUNDLED=1; '
              'resolve_trace_query; printf "%s|%s|%s" "${TRACE_QUERY_URL-}" "${TRACE_QUERY_USER-}" "${TRACE_QUERY_PASSWORD-}"')
    env = {"PATH": f"{tree[0] / 'bin'}:/usr/bin:/bin", "HELM_LOG": str(tree[1])}
    empty = subprocess.run(["sh", "-c", script], cwd=tree[0], capture_output=True, text=True, check=False,
                           env={**env, "TRACE_QUERY_URL": "", "TRACE_QUERY_USER": "", "TRACE_QUERY_PASSWORD": ""})
    assert empty.returncode == 0, empty.stderr
    assert empty.stdout == "http://openobserve:5080|test-user|Test-pass1"
    given = subprocess.run(["sh", "-c", script], cwd=tree[0], capture_output=True, text=True, check=False,
                           env={**env, "TRACE_QUERY_URL": "http://x:1", "TRACE_QUERY_USER": "u", "TRACE_QUERY_PASSWORD": "p"})
    assert given.stdout == "http://x:1|u|p"
