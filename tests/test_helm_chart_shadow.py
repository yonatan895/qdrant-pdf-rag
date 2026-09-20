"""Shadow parity for the first-party Helm chart (issue #448 H0/H1a).

Renders the current Kustomize+sed path via the actual deploy.sh producer
(AIRGAP_DRYRUN=1, real `kubectl kustomize`, real helm binary for the
preflight) and the new chart via the actual `helm template` producer on
the same independently constructed synthetic operator inputs, then compares
load-bearing semantics -- not YAML text/order.

No deployment cutover: the current Kustomize path remains authoritative.
No cluster, GPU, or private corpus required.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "mainframe-rag"

IMAGE_SHA = "a" * 40

BASE_ENV = {
    "IMAGE_SHA": IMAGE_SHA,
    "INTERNAL_REGISTRY": "reg.internal",
    "NAMESPACE": "ns",
    "STORAGE_CLASS": "standard",
    "EMBED_MODEL": "embed-model",
    "DENSE_DIM": "768",
    "EMBED_MODEL_REVISION": "rev-1",
    "VLLM_BASE_URL": "http://vllm:8000",
    # Explicit EMBED_BASE_URL to pin the derivation (VLLM_BASE_URL + /v1).
    "EMBED_BASE_URL": "http://vllm:8000/v1",
}

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("kubectl") is None,
    reason="helm and kubectl are required for chart shadow parity",
)


def run(cmd, cwd, env=None):
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, env=env, check=False
    )


def make_old_tree(tmp_path: Path, extra_env: dict) -> Path:
    """Build a minimal tmp repo root that deploy.sh can run in dry-run."""
    # Scripts.
    (tmp_path / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    for name in ("common.sh", "deploy.sh"):
        shutil.copy(REPO / "scripts" / "airgap" / name, tmp_path / "scripts" / "airgap" / name)
    # Kustomize trees.
    shutil.copytree(REPO / "deploy" / "kustomize", tmp_path / "deploy" / "kustomize")
    # Qdrant overlay values + policy preset (common.sh loads the preset).
    (tmp_path / "overlays" / "openshift").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "overlays" / "openshift" / "values.yaml", tmp_path / "overlays" / "openshift" / "values.yaml")
    shutil.copy(
        REPO / "overlays" / "openshift" / "collection-policy.env",
        tmp_path / "overlays" / "openshift" / "collection-policy.env",
    )
    # Vendored chart + images pin.
    (tmp_path / "charts").mkdir(exist_ok=True)
    chart_tgz = next(REPO.glob("charts/qdrant-*.tgz"))
    shutil.copy(chart_tgz, tmp_path / "charts" / chart_tgz.name)
    shutil.copy(REPO / "images.txt", tmp_path / "images.txt")
    (tmp_path / "dist").mkdir(exist_ok=True)
    return tmp_path


def run_old_deploy(tmp_path: Path, extra_env: dict) -> dict:
    """Run deploy.sh dry-run; return parsed { (kind, name): doc } for agent Service/Deployment."""
    env = {
        "PATH": f"/usr/bin:/bin:{Path(shutil.which('helm')).parent}:{Path(shutil.which('kubectl')).parent}",
        "AIRGAP_DRYRUN": "1",
        **BASE_ENV,
        **extra_env,
    }
    # Ensure helm/kubectl are found even when PATH is constrained.
    for tool in ("helm", "kubectl"):
        src = shutil.which(tool)
        assert src, f"{tool} is required"
    r = run(["sh", str(tmp_path / "scripts" / "airgap" / "deploy.sh")], cwd=tmp_path, env=env)
    assert r.returncode == 0, f"deploy.sh failed: {r.stderr}\n{r.stdout}"
    rendered = (tmp_path / "dist" / "agent-rendered.yaml").read_text()
    assert "__" not in rendered, f"leftover placeholder in old render:\n{rendered}"
    docs = list(yaml.safe_load_all(rendered))
    return {(d["kind"], d["metadata"]["name"]): d for d in docs if d}


def run_new_template(extra_values: dict, namespace: str = "ns") -> dict:
    """Render the new chart with synthetic values; return parsed docs."""
    # Build values from BASE_ENV + extras, mirroring the operator mapping.
    # This is the test's independent construction, not the chart itself.
    values = {
        "replicaCount": 2,
        "images": {
            "agent": {
                "repository": f"{BASE_ENV['INTERNAL_REGISTRY']}/qdrant-pdf-rag-agent",
                "tag": IMAGE_SHA,
                "pullPolicy": "IfNotPresent",
            }
        },
        "qdrantRelease": "qdrant",
        "models": {
            "embedding": {
                "baseUrl": BASE_ENV["EMBED_BASE_URL"],
                "model": BASE_ENV["EMBED_MODEL"],
                "revision": BASE_ENV["EMBED_MODEL_REVISION"],
                "dimension": int(BASE_ENV["DENSE_DIM"]),
            },
            "reasoning": {"baseUrl": "", "model": ""},
            "rerank": {
                "enabled": False,
                "baseUrl": "",
                "model": "BAAI/bge-reranker-v2-m3",
                "endpointOrder": "score_first",
            },
        },
        "gateway": {"apiKeySecretName": "", "caConfigMapName": ""},
        "tracing": {
            "enabled": True,
            "endpoint": "http://jaeger:4318",
            "deploymentEnvironment": "",
            "serviceName": "",
        },
        "metrics": {"enabled": False},
        "pullSecret": {"name": ""},
        "resources": {
            "agent": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            }
        },
    }
    # Deep-merge extras (excluding test-only _namespace).
    ns = namespace
    extras = dict(extra_values)
    if "_namespace" in extras:
        ns = extras.pop("_namespace")

    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(values, extras)
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        values_file = f.name
    try:
        r = run(
            ["helm", "template", "test", str(CHART), "--namespace", ns, "-f", values_file],
            cwd=REPO,
        )
    finally:
        Path(values_file).unlink(missing_ok=True)
    assert r.returncode == 0, f"helm template failed: {r.stderr}"
    assert "__" not in r.stdout, "placeholder leaked through chart render"
    docs = list(yaml.safe_load_all(r.stdout))
    return {(d["kind"], d["metadata"]["name"]): d for d in docs if d}


def env_map(deployment: dict) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    agent = next(c for c in containers if c["name"] == "agent")
    out = {}
    for e in agent.get("env", []):
        out[e["name"]] = e
    return out


def test_inventory_matches():
    """Both renderers produce exactly Deployment/rag-agent + Service/rag-agent."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        old = run_old_deploy(make_old_tree(Path(td), {}), {})
    new = run_new_template({})
    assert set(old) == {("Service", "rag-agent"), ("Deployment", "rag-agent")}
    assert set(new) == set(old)


def test_base_parity_with_independent_controls(tmp_path):
    """Exact semantic parity for the base prod overlay (Route off, keyless)."""
    old = run_old_deploy(make_old_tree(tmp_path, {}), {})
    new = run_new_template({})

    old_dep = old[("Deployment", "rag-agent")]
    new_dep = new[("Deployment", "rag-agent")]
    new_svc = new[("Service", "rag-agent")]

    # Independent expected controls (not computed from the chart).
    assert new_dep["spec"]["replicas"] == 2
    assert new_dep["metadata"]["namespace"] == "ns"
    assert new_svc["metadata"]["namespace"] == "ns"
    containers = new_dep["spec"]["template"]["spec"]["containers"]
    agent = next(c for c in containers if c["name"] == "agent")
    assert agent["image"] == f"reg.internal/qdrant-pdf-rag-agent:{IMAGE_SHA}"
    assert agent["imagePullPolicy"] == "IfNotPresent"

    # Service selectors/ports.
    assert new_svc["spec"]["type"] == "ClusterIP"
    assert new_svc["spec"]["selector"] == {"app": "rag-agent"}
    assert new_svc["spec"]["ports"] == [{"name": "http", "port": 8080, "targetPort": "http"}]

    # Probes and resources are protected defaults.
    assert agent["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert agent["livenessProbe"]["httpGet"] == {"path": "/livez", "port": "http"}
    assert agent["resources"]["requests"] == {"cpu": "100m", "memory": "256Mi"}
    assert agent["resources"]["limits"] == {"cpu": "500m", "memory": "512Mi"}

    # Env parity by name (order-insensitive).
    old_env = env_map(old_dep)
    new_env = env_map(new_dep)
    assert set(new_env) == set(old_env), f"env mismatch: old={sorted(old_env)} new={sorted(new_env)}"

    for name in old_env:
        o, n = old_env[name], new_env[name]
        # Secret refs vs plain values must agree in kind.
        assert ("valueFrom" in o) == ("valueFrom" in n), name
        if "valueFrom" in o:
            assert o["valueFrom"] == n["valueFrom"], name
        else:
            # YAML types must match: quoted ints/bools stay strings.
            assert o.get("value") == n.get("value"), f"{name}: {o.get('value')!r} != {n.get('value')!r}"
            assert type(o.get("value")) is type(n.get("value")), f"{name} type: {type(o.get('value'))} vs {type(n.get('value'))}"

    # Load-bearing spot checks with independent expectations.
    assert new_env["QDRANT_URL"]["value"] == "http://qdrant:6333"
    qkey = new_env["QDRANT_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert qkey == {"key": "read-only-api-key", "name": "qdrant-apikey"}
    assert new_env["DENSE_DIM"]["value"] == "768" and isinstance(new_env["DENSE_DIM"]["value"], str)
    assert new_env["METRICS_ENABLED"]["value"] == "false"
    assert new_env["UI_ENABLED"]["value"] == "true"
    assert new_env["RERANK_ENABLED"]["value"] == "false"
    assert new_env["RERANK_ENDPOINT_ORDER"]["value"] == "score_first"
    assert "OTEL_SERVICE_NAME" not in new_env  # stripped when unset
    assert "LLM_API_KEY" not in new_env  # keyless strip
    assert new_dep["spec"]["template"]["spec"]["imagePullSecrets"] == []

    # No Qdrant data resource, no hooks.
    for (kind, _name) in new:
        assert kind in ("Deployment", "Service")
    raw = run(["helm", "template", "test", str(CHART), "--namespace", "ns"], cwd=REPO)
    assert "helm.sh/hook" not in raw.stdout
    assert "kind: StatefulSet" not in raw.stdout


def test_gateway_keys_parity(tmp_path):
    old = run_old_deploy(
        make_old_tree(tmp_path, {}), {"GATEWAY_API_KEY_SECRET": "gw-keys"}
    )
    new = run_new_template({"gateway": {"apiKeySecretName": "gw-keys"}})
    old_env = env_map(old[("Deployment", "rag-agent")])
    new_env = env_map(new[("Deployment", "rag-agent")])
    for name, key in (
        ("LLM_API_KEY", "llm-api-key"),
        ("EMBED_API_KEY", "embed-api-key"),
        ("RERANK_API_KEY", "rerank-api-key"),
    ):
        assert old_env[name]["valueFrom"]["secretKeyRef"] == {
            "key": key,
            "name": "gw-keys",
        }
        assert new_env[name]["valueFrom"]["secretKeyRef"] == {
            "key": key,
            "name": "gw-keys",
        }


def test_pull_secret_and_service_name_and_tracing_off(tmp_path):
    old = run_old_deploy(
        make_old_tree(tmp_path, {}),
        {
            "PULL_SECRET": "ghcr-pull",
            "OTEL_SERVICE_NAME": "my-rag-prod",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "off",
        },
    )
    new = run_new_template(
        {
            "pullSecret": {"name": "ghcr-pull"},
            "tracing": {"enabled": False, "endpoint": "", "serviceName": "my-rag-prod"},
        }
    )
    old_dep = old[("Deployment", "rag-agent")]
    new_dep = new[("Deployment", "rag-agent")]
    assert old_dep["spec"]["template"]["spec"]["imagePullSecrets"] == [
        {"name": "ghcr-pull"}
    ]
    assert new_dep["spec"]["template"]["spec"]["imagePullSecrets"] == [
        {"name": "ghcr-pull"}
    ]
    assert env_map(old_dep)["OTEL_SERVICE_NAME"]["value"] == "my-rag-prod"
    assert env_map(new_dep)["OTEL_SERVICE_NAME"]["value"] == "my-rag-prod"
    # Tracing off renders an empty (bare) endpoint in both paths.
    assert env_map(old_dep)["OTEL_EXPORTER_OTLP_ENDPOINT"].get("value") in (None, "")
    assert env_map(new_dep)["OTEL_EXPORTER_OTLP_ENDPOINT"].get("value") in (None, "")


def test_nondefault_namespace_registry_and_rerank(tmp_path):
    sha = "b" * 40
    old_env_extra = {
        "IMAGE_SHA": sha,
        "INTERNAL_REGISTRY": "other.example/rr",
        "NAMESPACE": "custom-ns",
        "RERANK_ENABLED": "true",
        "RERANK_BASE_URL": "http://rerank:8002/v1",
        "RERANK_MODEL": "my-reranker",
        "RERANK_ENDPOINT_ORDER": "rerank_first",
    }
    old = run_old_deploy(make_old_tree(tmp_path, {}), old_env_extra)
    new = run_new_template(
        {
            "_namespace": "custom-ns",
            "images": {
                "agent": {"repository": "other.example/rr/qdrant-pdf-rag-agent", "tag": sha}
            },
            "models": {
                "rerank": {
                    "enabled": True,
                    "baseUrl": "http://rerank:8002/v1",
                    "model": "my-reranker",
                    "endpointOrder": "rerank_first",
                }
            },
        }
    )
    assert new[("Deployment", "rag-agent")]["metadata"]["namespace"] == "custom-ns"
    assert old[("Deployment", "rag-agent")]["metadata"]["namespace"] == "custom-ns"
    old_agent = next(
        c
        for c in old[("Deployment", "rag-agent")]["spec"]["template"]["spec"]["containers"]
        if c["name"] == "agent"
    )
    new_agent = next(
        c
        for c in new[("Deployment", "rag-agent")]["spec"]["template"]["spec"]["containers"]
        if c["name"] == "agent"
    )
    assert old_agent["image"] == new_agent["image"] == f"other.example/rr/qdrant-pdf-rag-agent:{sha}"
    for k in ("RERANK_ENABLED", "RERANK_BASE_URL", "RERANK_MODEL", "RERANK_ENDPOINT_ORDER"):
        o = next(e for e in old_agent["env"] if e["name"] == k)
        n = next(e for e in new_agent["env"] if e["name"] == k)
        assert o.get("value") == n.get("value"), k


def test_gateway_ca_parity(tmp_path):
    old = run_old_deploy(make_old_tree(tmp_path, {}), {"GATEWAY_CA_CONFIGMAP": "gw-ca"})
    new = run_new_template({"gateway": {"caConfigMapName": "gw-ca"}})
    for docs in (old, new):
        dep = docs[("Deployment", "rag-agent")]
        agent = next(
            c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "agent"
        )
        ssl = next(e for e in agent["env"] if e["name"] == "SSL_CERT_FILE")
        assert ssl["value"] == "/etc/gateway-ca/ca-bundle.crt"
        assert {"name": "gateway-ca", "mountPath": "/etc/gateway-ca", "readOnly": True} in agent[
            "volumeMounts"
        ]
        vols = dep["spec"]["template"]["spec"]["volumes"]
        ca = next(v for v in vols if v["name"] == "gateway-ca")
        assert ca["configMap"]["name"] == "gw-ca"


# ------------------------------------------------------- negative schema cases


def _helm_template_with_values(values: dict):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        path = f.name
    try:
        return run(["helm", "template", "test", str(CHART), "--namespace", "ns", "-f", path], cwd=REPO)
    finally:
        Path(path).unlink(missing_ok=True)


def _base_values():
    return {
        "replicaCount": 2,
        "images": {
            "agent": {"repository": "reg.internal/qdrant-pdf-rag-agent", "tag": IMAGE_SHA}
        },
        "qdrantRelease": "qdrant",
        "models": {
            "embedding": {
                "baseUrl": "http://vllm:8000/v1",
                "model": "m",
                "revision": "r1",
                "dimension": 768,
            },
            "reasoning": {"baseUrl": "", "model": ""},
            "rerank": {
                "enabled": False,
                "baseUrl": "",
                "model": "BAAI/bge-reranker-v2-m3",
                "endpointOrder": "score_first",
            },
        },
        "gateway": {"apiKeySecretName": ""},
        "tracing": {"enabled": True, "endpoint": "http://jaeger:4318"},
        "metrics": {"enabled": False},
        "pullSecret": {"name": ""},
        "resources": {"agent": {}},
    }


@pytest.mark.parametrize(
    "mutate,needle",
    [
        ({"models": {"embedding": {"dimension": 0}}}, "dimension"),
        ({"models": {"embedding": {"revision": "   "}}}, "revision"),
        ({"images": {"agent": {"tag": "HEAD"}}}, "tag"),
        ({"gateway": {"apiKeySecretName": "Bad_Name!"}}, "apiKeySecretName"),
        ({"models": {"rerank": {"endpointOrder": "nope"}}}, "endpointOrder"),
        ({"metrics": {"enabled": "false"}}, "metrics"),
        (
            {"models": {"rerank": {"enabled": True, "baseUrl": ""}}},
            "baseUrl",
        ),
        ({"models": {"reasoning": {"model": "foo", "baseUrl": ""}}}, "reasoning"),
    ],
)
def test_schema_rejects_invalid_selected_configuration(mutate, needle):
    values = _base_values()

    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(values, mutate)
    r = _helm_template_with_values(values)
    assert r.returncode != 0, f"schema accepted invalid {needle}: {r.stdout}"
    assert needle.split(".")[-1] in r.stderr or "schema" in r.stderr.lower()


def test_chart_contains_no_qdrant_no_hook_no_secret_values():
    # No Qdrant data lifecycle inside the first-party chart.
    names = [p.name for p in (CHART / "templates").glob("*.yaml")]
    assert sorted(names) == ["agent-deployment.yaml", "agent-service.yaml"]
    text = "".join((CHART / "templates" / n).read_text() for n in names)
    assert "kind: StatefulSet" not in text
    assert "qdrant" in text.lower()  # references only (URL/Secret name), checked below
    assert "helm.sh/hook" not in text
    # Values carry references, never material.
    schema_text = (CHART / "values.schema.json").read_text()
    assert "api-key" not in schema_text or "read-only-api-key" not in schema_text
    values_text = (CHART / "values.yaml").read_text()
    assert "sk-" not in values_text
    # Template must not hardcode a real registry.
    assert "registry.example" not in text
    assert "ghcr.io" not in text
