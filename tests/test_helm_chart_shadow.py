"""Shadow parity for the first-party Helm chart (issue #448 H0/H1a/H1b).

Renders the current Kustomize+sed path via the actual producers
(deploy.sh / ingest.sh with AIRGAP_DRYRUN=1, real `kubectl kustomize`) and
the new chart via the actual `helm template` producer on the same
independently constructed synthetic operator inputs, then compares
load-bearing semantics -- not YAML text/order.

No deployment cutover: the current Kustomize path remains authoritative.
No cluster, GPU, or private corpus required. The reencrypt Route manifest
itself is compared against independent expectations (deploy.sh builds it
from the cluster service CA, unavailable in dry-run); the UI overlay
(sidecar/ServiceAccount/Service) uses old/new diffing.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "mainframe-rag"

IMAGE_SHA = "a" * 40
FAKE_CA = "-----BEGIN CERTIFICATE-----\nFAKE-CA-BUNDLE\n-----END CERTIFICATE-----"

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


def make_old_tree(tmp_path: Path) -> Path:
    """Build a minimal tmp repo root that deploy.sh/ingest.sh run in dry-run."""
    (tmp_path / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    for name in ("common.sh", "deploy.sh", "ingest.sh", "map_values.py"):
        shutil.copy(REPO / "scripts" / "airgap" / name, tmp_path / "scripts" / "airgap" / name)
    shutil.copytree(REPO / "deploy" / "kustomize", tmp_path / "deploy" / "kustomize")
    (tmp_path / "overlays" / "openshift").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "overlays" / "openshift" / "values.yaml", tmp_path / "overlays" / "openshift" / "values.yaml")
    shutil.copy(
        REPO / "overlays" / "openshift" / "collection-policy.env",
        tmp_path / "overlays" / "openshift" / "collection-policy.env",
    )
    (tmp_path / "charts").mkdir(exist_ok=True)
    chart_tgz = next(REPO.glob("charts/qdrant-*.tgz"))
    shutil.copy(chart_tgz, tmp_path / "charts" / chart_tgz.name)
    shutil.copytree(REPO / "charts" / "mainframe-rag", tmp_path / "charts" / "mainframe-rag")
    shutil.copy(REPO / "images.txt", tmp_path / "images.txt")
    (tmp_path / "dist").mkdir(exist_ok=True)
    return tmp_path


def _tool_path_env():
    helm_dir = str(Path(shutil.which("helm")).parent)
    kubectl_dir = str(Path(shutil.which("kubectl")).parent)
    return f"/usr/bin:/bin:{helm_dir}:{kubectl_dir}"


def run_old_deploy(tmp_path: Path, extra_env: dict) -> dict:
    """Run deploy.sh dry-run; return parsed agent {(kind, name): doc}."""
    env = {
        "PATH": _tool_path_env(),
        "AIRGAP_DRYRUN": "1",
        **BASE_ENV,
        **extra_env,
    }
    r = run(["sh", str(tmp_path / "scripts" / "airgap" / "deploy.sh")], cwd=tmp_path, env=env)
    assert r.returncode == 0, f"deploy.sh failed: {r.stderr}\n{r.stdout}"
    rendered = (tmp_path / "dist" / "agent-rendered.yaml").read_text()
    assert "__" not in rendered, f"leftover placeholder in old render:\n{rendered}"
    docs = list(yaml.safe_load_all(rendered))
    return {(d["kind"], d["metadata"]["name"]): d for d in docs if d}


def read_old_jaeger(tmp_path: Path) -> dict:
    p = tmp_path / "dist" / "jaeger-rendered.yaml"
    if not p.exists():
        return {}
    text = p.read_text()
    assert "__" not in text, f"leftover placeholder in old jaeger render:\n{text}"
    docs = list(yaml.safe_load_all(text))
    return {(d["kind"], d["metadata"]["name"]): d for d in docs if d}


def read_old_monitor(tmp_path: Path) -> dict:
    p = tmp_path / "dist" / "servicemonitor-rendered.yaml"
    if not p.exists():
        return {}
    text = p.read_text()
    assert "__" not in text
    docs = list(yaml.safe_load_all(text))
    return {(d["kind"], d["metadata"]["name"]): d for d in docs if d}


def run_old_ingest(tmp_path: Path, extra_env: dict) -> dict:
    """Run ingest.sh dry-run; return parsed Job {(kind, name): doc}."""
    env = {
        "PATH": _tool_path_env(),
        "AIRGAP_DRYRUN": "1",
        **BASE_ENV,
        "CORPUS_PVC": "corpus-pvc",
        **extra_env,
    }
    r = run(["sh", str(tmp_path / "scripts" / "airgap" / "ingest.sh")], cwd=tmp_path, env=env)
    assert r.returncode == 0, f"ingest.sh failed: {r.stderr}\n{r.stdout}"
    rendered = (tmp_path / "dist" / "ingest-rendered.yaml").read_text()
    assert "__" not in rendered, f"leftover placeholder in old ingest render:\n{rendered}"
    docs = list(yaml.safe_load_all(rendered))
    return {(d["kind"], d["metadata"]["name"]): d for d in docs if d}


def base_values() -> dict:
    """Independent synthetic operator mapping (not computed from the chart)."""
    return {
        "replicaCount": 2,
        "images": {
            "agent": {
                "repository": f"{BASE_ENV['INTERNAL_REGISTRY']}/qdrant-pdf-rag-agent",
                "tag": IMAGE_SHA,
                "pullPolicy": "IfNotPresent",
            },
            "ingest": {
                "repository": f"{BASE_ENV['INTERNAL_REGISTRY']}/qdrant-pdf-rag-ingest",
                "tag": IMAGE_SHA,
                "pullPolicy": "IfNotPresent",
            },
            "jaeger": {
                "repository": f"{BASE_ENV['INTERNAL_REGISTRY']}/jaegertracing/jaeger",
                "tag": "v2.20.0",
                "pullPolicy": "IfNotPresent",
            },
            "oauthProxy": {
                "repository": f"{BASE_ENV['INTERNAL_REGISTRY']}/openshift4/ose-oauth-proxy",
                "tag": "v4.14",
                "pullPolicy": "IfNotPresent",
            },
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
        "route": {"enabled": False, "timeoutSeconds": 300, "destinationCA": ""},
        "storage": {"className": BASE_ENV["STORAGE_CLASS"]},
        "ingest": {
            "enabled": False,
            "corpusPVC": "",
            "workers": 4,
            "aliasPublish": False,
            "reingest": False,
            "retireDocs": [],
            "contextualEnabled": False,
            "contextLlmBaseUrl": "",
            "contextLlmModel": "",
            "collectionPolicy": {
                "shardNumber": 6,
                "replicationFactor": 3,
                "writeConsistencyFactor": 2,
            },
            "resources": {
                "requests": {"cpu": "4", "memory": "8Gi"},
                "limits": {"cpu": "16", "memory": "32Gi"},
            },
        },
        "pullSecret": {"name": ""},
        "resources": {
            "agent": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            }
        },
    }


def run_new_template(extra_values: dict, namespace: str = "ns") -> dict:
    """Render the new chart with synthetic values; return parsed docs."""
    values = base_values()
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


def run_new_job_only(extra_values: dict, namespace: str = "ns") -> dict:
    """Render only the ingest Job template (the explicit-operation path)."""
    values = base_values()
    values["ingest"]["enabled"] = True
    extras = dict(extra_values)
    if "_namespace" in extras:
        namespace = extras.pop("_namespace")

    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(values, extras)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        values_file = f.name
    try:
        r = run(
            [
                "helm", "template", "test", str(CHART),
                "--namespace", namespace, "-f", values_file,
                "--show-only", "templates/ingest-job.yaml",
            ],
            cwd=REPO,
        )
    finally:
        Path(values_file).unlink(missing_ok=True)
    assert r.returncode == 0, f"helm template (job-only) failed: {r.stderr}"
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    assert len(docs) == 1 and docs[0]["kind"] == "Job", f"expected one Job, got: {r.stdout[:500]}"
    return {("Job", docs[0]["metadata"]["name"]): docs[0]}


def env_map(deployment: dict, container: str = "agent") -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    c = next(x for x in containers if x["name"] == container)
    return {e["name"]: e for e in c.get("env", [])}


def container_map(deployment: dict) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return {c["name"]: c for c in containers}


# ------------------------------------------------------- agent base (H1a)


def test_agent_inventory_matches(tmp_path):
    """Agent Service/Deployment inventory matches between renderers."""
    old = run_old_deploy(make_old_tree(tmp_path), {})
    new = run_new_template({})
    assert {("Service", "rag-agent"), ("Deployment", "rag-agent")} <= set(old)
    assert {("Service", "rag-agent"), ("Deployment", "rag-agent")} <= set(new)


def test_base_parity_with_independent_controls(tmp_path):
    """Exact semantic parity for the base prod overlay (Route off, keyless)."""
    old = run_old_deploy(make_old_tree(tmp_path), {})
    new = run_new_template({})

    old_dep = old[("Deployment", "rag-agent")]
    new_dep = new[("Deployment", "rag-agent")]
    new_svc = new[("Service", "rag-agent")]

    assert new_dep["spec"]["replicas"] == 2
    assert new_dep["metadata"]["namespace"] == "ns"
    assert new_svc["metadata"]["namespace"] == "ns"
    agent = container_map(new_dep)["agent"]
    assert agent["image"] == f"reg.internal/qdrant-pdf-rag-agent:{IMAGE_SHA}"
    assert agent["imagePullPolicy"] == "IfNotPresent"

    assert new_svc["spec"]["type"] == "ClusterIP"
    assert new_svc["spec"]["selector"] == {"app": "rag-agent"}
    assert new_svc["spec"]["ports"] == [{"name": "http", "port": 8080, "targetPort": "http"}]

    assert agent["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert agent["livenessProbe"]["httpGet"] == {"path": "/livez", "port": "http"}
    assert agent["resources"]["requests"] == {"cpu": "100m", "memory": "256Mi"}
    assert agent["resources"]["limits"] == {"cpu": "500m", "memory": "512Mi"}

    old_env = env_map(old_dep)
    new_env = env_map(new_dep)
    assert set(new_env) == set(old_env), f"env mismatch: old={sorted(old_env)} new={sorted(new_env)}"
    for name in old_env:
        o, n = old_env[name], new_env[name]
        assert ("valueFrom" in o) == ("valueFrom" in n), name
        if "valueFrom" in o:
            assert o["valueFrom"] == n["valueFrom"], name
        else:
            assert o.get("value") == n.get("value"), f"{name}: {o.get('value')!r} != {n.get('value')!r}"
            assert type(o.get("value")) is type(n.get("value")), name

    assert new_env["QDRANT_URL"]["value"] == "http://qdrant:6333"
    assert new_env["QDRANT_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "key": "read-only-api-key", "name": "qdrant-apikey",
    }
    assert new_env["DENSE_DIM"]["value"] == "768" and isinstance(new_env["DENSE_DIM"]["value"], str)
    assert new_env["METRICS_ENABLED"]["value"] == "false"
    assert new_env["UI_ENABLED"]["value"] == "true"
    assert new_env["RERANK_ENABLED"]["value"] == "false"
    assert new_env["RERANK_ENDPOINT_ORDER"]["value"] == "score_first"
    assert "OTEL_SERVICE_NAME" not in new_env
    assert "LLM_API_KEY" not in new_env
    assert new_dep["spec"]["template"]["spec"]["imagePullSecrets"] == []
    assert "serviceAccountName" not in new_dep["spec"]["template"]["spec"]

    raw = run(["helm", "template", "test", str(CHART), "--namespace", "ns"], cwd=REPO)
    assert "helm.sh/hook" not in raw.stdout
    assert "kind: StatefulSet" not in raw.stdout


def test_gateway_keys_parity(tmp_path):
    old = run_old_deploy(make_old_tree(tmp_path), {"GATEWAY_API_KEY_SECRET": "gw-keys"})
    new = run_new_template({"gateway": {"apiKeySecretName": "gw-keys"}})
    old_env = env_map(old[("Deployment", "rag-agent")])
    new_env = env_map(new[("Deployment", "rag-agent")])
    for name, key in (
        ("LLM_API_KEY", "llm-api-key"),
        ("EMBED_API_KEY", "embed-api-key"),
        ("RERANK_API_KEY", "rerank-api-key"),
    ):
        assert old_env[name]["valueFrom"]["secretKeyRef"] == {"key": key, "name": "gw-keys"}
        assert new_env[name]["valueFrom"]["secretKeyRef"] == {"key": key, "name": "gw-keys"}


def test_pull_secret_and_service_name_and_tracing_off(tmp_path):
    tree = make_old_tree(tmp_path)
    old = run_old_deploy(tree, {
        "PULL_SECRET": "ghcr-pull",
        "OTEL_SERVICE_NAME": "my-rag-prod",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "off",
    })
    new = run_new_template({
        "pullSecret": {"name": "ghcr-pull"},
        "tracing": {"enabled": False, "endpoint": "", "serviceName": "my-rag-prod"},
    })
    old_dep = old[("Deployment", "rag-agent")]
    new_dep = new[("Deployment", "rag-agent")]
    assert old_dep["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    assert new_dep["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    assert env_map(old_dep)["OTEL_SERVICE_NAME"]["value"] == "my-rag-prod"
    assert env_map(new_dep)["OTEL_SERVICE_NAME"]["value"] == "my-rag-prod"
    assert env_map(old_dep)["OTEL_EXPORTER_OTLP_ENDPOINT"].get("value") in (None, "")
    assert env_map(new_dep)["OTEL_EXPORTER_OTLP_ENDPOINT"].get("value") in (None, "")
    # Tracing off skips Jaeger on both sides.
    assert read_old_jaeger(tree) == {}
    assert not [k for k in new if k[0] in ("Deployment", "Service") and k[1] == "jaeger"]


def test_nondefault_namespace_registry_and_rerank(tmp_path):
    sha = "b" * 40
    old = run_old_deploy(make_old_tree(tmp_path), {
        "IMAGE_SHA": sha,
        "INTERNAL_REGISTRY": "other.example/rr",
        "NAMESPACE": "custom-ns",
        "RERANK_ENABLED": "true",
        "RERANK_BASE_URL": "http://rerank:8002/v1",
        "RERANK_MODEL": "my-reranker",
        "RERANK_ENDPOINT_ORDER": "rerank_first",
    })
    new = run_new_template({
        "_namespace": "custom-ns",
        "images": {"agent": {"repository": "other.example/rr/qdrant-pdf-rag-agent", "tag": sha}},
        "models": {"rerank": {
            "enabled": True, "baseUrl": "http://rerank:8002/v1",
            "model": "my-reranker", "endpointOrder": "rerank_first",
        }},
    })
    assert new[("Deployment", "rag-agent")]["metadata"]["namespace"] == "custom-ns"
    assert old[("Deployment", "rag-agent")]["metadata"]["namespace"] == "custom-ns"
    old_agent = container_map(old[("Deployment", "rag-agent")])["agent"]
    new_agent = container_map(new[("Deployment", "rag-agent")])["agent"]
    assert old_agent["image"] == new_agent["image"] == f"other.example/rr/qdrant-pdf-rag-agent:{sha}"
    for k in ("RERANK_ENABLED", "RERANK_BASE_URL", "RERANK_MODEL", "RERANK_ENDPOINT_ORDER"):
        o = next(e for e in old_agent["env"] if e["name"] == k)
        n = next(e for e in new_agent["env"] if e["name"] == k)
        assert o.get("value") == n.get("value"), k


def test_gateway_ca_parity(tmp_path):
    old = run_old_deploy(make_old_tree(tmp_path), {"GATEWAY_CA_CONFIGMAP": "gw-ca"})
    new = run_new_template({"gateway": {"caConfigMapName": "gw-ca"}})
    for docs in (old, new):
        dep = docs[("Deployment", "rag-agent")]
        agent = container_map(dep)["agent"]
        ssl = next(e for e in agent["env"] if e["name"] == "SSL_CERT_FILE")
        assert ssl["value"] == "/etc/gateway-ca/ca-bundle.crt"
        assert {"name": "gateway-ca", "mountPath": "/etc/gateway-ca", "readOnly": True} in agent["volumeMounts"]
        vols = dep["spec"]["template"]["spec"]["volumes"]
        ca = next(v for v in vols if v["name"] == "gateway-ca")
        assert ca["configMap"]["name"] == "gw-ca"


# ------------------------------------------------------- OAuth / Route (H1b)


def test_route_oauth_parity(tmp_path):
    """UI overlay parity: sidecar, ServiceAccount, Service port/annotation."""
    tree = make_old_tree(tmp_path)
    ca_file = tmp_path / "route-ca.crt"
    ca_file.write_text(FAKE_CA + "\n")
    old = run_old_deploy(tree, {
        "AGENT_ROUTE": "true", "ROUTE_DESTINATION_CA_FILE": str(ca_file),
    })
    new = run_new_template({
        "route": {"enabled": True, "timeoutSeconds": 300, "destinationCA": FAKE_CA},
        "images": {"oauthProxy": {
            "repository": "reg.internal/openshift4/ose-oauth-proxy", "tag": "v4.14",
        }},
    })
    # ServiceAccount.
    assert ("ServiceAccount", "rag-agent") in old
    assert ("ServiceAccount", "rag-agent") in new
    old_sa = old[("ServiceAccount", "rag-agent")]
    new_sa = new[("ServiceAccount", "rag-agent")]
    assert old_sa["metadata"]["annotations"] == new_sa["metadata"]["annotations"]

    # Service oauth port + serving-cert annotation.
    for docs in (old, new):
        svc = docs[("Service", "rag-agent")]
        assert svc["metadata"]["annotations"] == {
            "service.beta.openshift.io/serving-cert-secret-name": "rag-agent-tls",
        }
        ports = {(p["name"], p["port"], p["targetPort"]) for p in svc["spec"]["ports"]}
        assert ports == {("oauth", 8443, "oauth"), ("http", 8080, "http")}

    # Deployment: service account, sidecar, volumes.
    for docs in (old, new):
        dep = docs[("Deployment", "rag-agent")]
        assert dep["spec"]["template"]["spec"]["serviceAccountName"] == "rag-agent"
        oauth = container_map(dep)["oauth-proxy"]
        assert oauth["image"] == "reg.internal/openshift4/ose-oauth-proxy:v4.14"
        assert oauth["args"] == [
            "--provider=openshift",
            "--https-address=:8443",
            "--upstream=http://127.0.0.1:8080",
            "--tls-cert=/etc/tls/private/tls.crt",
            "--tls-key=/etc/tls/private/tls.key",
            "--cookie-secret-file=/etc/oauth/cookie-secret",
            "--email-domain=*",
            "--openshift-service-account=rag-agent",
            "--skip-auth-regex=^/healthz.*$",
        ]
        assert oauth["ports"] == [{"name": "oauth", "containerPort": 8443}]
        assert oauth["resources"] == {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "200m", "memory": "128Mi"},
        }
        vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
        assert vols["oauth-tls"] == {"name": "oauth-tls", "secret": {"secretName": "rag-agent-tls"}}
        assert vols["oauth-cookie"] == {
            "name": "oauth-cookie", "secret": {"secretName": "rag-agent-oauth-cookie"},
        }

    # Route itself: independent expectations (deploy.sh builds it from the
    # cluster service CA, unavailable in dry-run).
    route = new[("Route", "rag-agent")]
    assert route["apiVersion"] == "route.openshift.io/v1"
    assert route["metadata"]["namespace"] == "ns"
    assert route["metadata"]["annotations"] == {"haproxy.router.openshift.io/timeout": "300s"}
    assert route["spec"]["to"] == {"kind": "Service", "name": "rag-agent"}
    assert route["spec"]["port"] == {"targetPort": "oauth"}
    assert route["spec"]["tls"]["termination"] == "reencrypt"
    assert route["spec"]["tls"]["insecureEdgeTerminationPolicy"] == "Redirect"
    assert "FAKE-CA-BUNDLE" in route["spec"]["tls"]["destinationCACertificate"]

    # Route off: none of the OAuth resources exist.
    off = run_new_template({})
    assert ("ServiceAccount", "rag-agent") not in off
    assert ("Route", "rag-agent") not in off
    assert "serviceAccountName" not in off[("Deployment", "rag-agent")]["spec"]["template"]["spec"]


# ------------------------------------------------------- Jaeger (H1b)


def test_jaeger_parity(tmp_path):
    tree = make_old_tree(tmp_path)
    old_agent_env = {"PULL_SECRET": "ghcr-pull"}
    old = run_old_deploy(tree, old_agent_env)
    old_jaeger = read_old_jaeger(tree)
    assert old_jaeger, "expected jaeger-rendered.yaml in dry-run with tracing on"
    new = run_new_template({"pullSecret": {"name": "ghcr-pull"}})

    for key in (
        ("Deployment", "jaeger"), ("Service", "jaeger"),
        ("PersistentVolumeClaim", "jaeger-badger"), ("ConfigMap", "jaeger-config"),
    ):
        assert key in old_jaeger, f"missing old {key}"
        assert key in new, f"missing new {key}"

    old_dep = old_jaeger[("Deployment", "jaeger")]
    new_dep = new[("Deployment", "jaeger")]
    old_c = next(c for c in old_dep["spec"]["template"]["spec"]["containers"] if c["name"] == "jaeger")
    new_c = next(c for c in new_dep["spec"]["template"]["spec"]["containers"] if c["name"] == "jaeger")
    assert old_c["image"] == new_c["image"] == "reg.internal/jaegertracing/jaeger:v2.20.0"
    assert old_c["args"] == new_c["args"] == ["--config", "/etc/jaeger/config-badger.yaml"]
    assert old_c["ports"] == new_c["ports"]
    assert old_c["readinessProbe"] == new_c["readinessProbe"]
    assert old_c["resources"] == new_c["resources"]
    assert old_c["volumeMounts"] == new_c["volumeMounts"]
    assert old_dep["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    assert new_dep["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    assert new_dep["spec"]["strategy"] == {"type": "Recreate", "rollingUpdate": None}

    old_svc = old_jaeger[("Service", "jaeger")]
    new_svc = new[("Service", "jaeger")]
    assert old_svc["spec"]["ports"] == new_svc["spec"]["ports"]
    assert new_svc["spec"]["type"] == "ClusterIP"

    old_pvc = old_jaeger[("PersistentVolumeClaim", "jaeger-badger")]
    new_pvc = new[("PersistentVolumeClaim", "jaeger-badger")]
    assert old_pvc["spec"]["storageClassName"] == new_pvc["spec"]["storageClassName"] == "standard"
    assert new_pvc["spec"]["resources"]["requests"] == {"storage": "10Gi"}

    # Config content parity (parsed inner YAML, not text).
    old_cfg = yaml.safe_load(old_jaeger[("ConfigMap", "jaeger-config")]["data"]["config-badger.yaml"])
    new_cfg = yaml.safe_load(new[("ConfigMap", "jaeger-config")]["data"]["config-badger.yaml"])
    assert old_cfg == new_cfg
    # OTLP/HTTP only: 4317 stays closed.
    assert "4317" not in yaml.safe_dump(new_cfg)

    # Agent still points at the in-cluster Jaeger.
    assert env_map(old[("Deployment", "rag-agent")])["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://jaeger:4318"
    assert env_map(new[("Deployment", "rag-agent")])["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://jaeger:4318"


# ------------------------------------------------------- ServiceMonitor (H1b)


def test_servicemonitor_parity(tmp_path):
    tree = make_old_tree(tmp_path)
    run_old_deploy(tree, {"METRICS_ENABLED": "true"})
    old_sm = read_old_monitor(tree)
    new = run_new_template({"metrics": {"enabled": True}})
    key = ("ServiceMonitor", "rag-agent")
    assert key in old_sm and key in new
    assert old_sm[key]["spec"] == new[key]["spec"]
    assert new[key]["spec"]["selector"] == {"matchLabels": {"app": "rag-agent"}}
    assert new[key]["spec"]["endpoints"] == [{
        "port": "http", "path": "/metrics", "interval": "30s", "scrapeTimeout": "10s",
    }]
    assert env_map(new[("Deployment", "rag-agent")])["METRICS_ENABLED"]["value"] == "true"

    off = run_new_template({})
    assert ("ServiceMonitor", "rag-agent") not in off


# ------------------------------------------------------- ingest Job (H1b)


def ingest_env_map(job: dict) -> dict:
    c = next(x for x in job["spec"]["template"]["spec"]["containers"] if x["name"] == "ingest")
    return {e["name"]: e for e in c.get("env", [])}


def test_ingest_job_parity_normal(tmp_path):
    old = run_old_ingest(make_old_tree(tmp_path), {})
    new = run_new_job_only({"ingest": {"corpusPVC": "corpus-pvc"}})
    old_job = old[("Job", "ingest")]
    new_job = new[("Job", "ingest")]

    old_c = next(c for c in old_job["spec"]["template"]["spec"]["containers"] if c["name"] == "ingest")
    new_c = next(c for c in new_job["spec"]["template"]["spec"]["containers"] if c["name"] == "ingest")
    assert old_c["image"] == new_c["image"] == f"reg.internal/qdrant-pdf-rag-ingest:{IMAGE_SHA}"
    assert old_c["args"] == new_c["args"] == ["--src", "/corpus", "--progress", "/work/inventory.jsonl"]
    assert old_c["resources"] == new_c["resources"]
    assert old_c["volumeMounts"] == new_c["volumeMounts"]

    old_env = ingest_env_map(old_job)
    new_env = ingest_env_map(new_job)
    assert set(new_env) == set(old_env), f"{sorted(old_env)} vs {sorted(new_env)}"
    for name in old_env:
        o, n = old_env[name], new_env[name]
        assert ("valueFrom" in o) == ("valueFrom" in n), name
        if "valueFrom" in o:
            assert o["valueFrom"] == n["valueFrom"], name
        else:
            assert o.get("value") == n.get("value"), f"{name}: {o.get('value')!r} != {n.get('value')!r}"
            assert type(o.get("value")) is type(n.get("value")), name

    # Spot checks: full-access key, quoted collection policy, quoted workers/flags.
    assert new_env["QDRANT_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "qdrant-apikey", "key": "api-key",
    }
    assert new_env["QDRANT_COLLECTION"]["value"] == "mainframe_manuals"
    assert new_env["QDRANT_SHARD_NUMBER"]["value"] == "6"
    assert new_env["INGEST_ALIAS_PUBLISH"]["value"] == "false"
    assert new_env["INGEST_WORKERS"]["value"] == "4"
    assert new_env["OTEL_SERVICE_NAME"]["value"] == "mainframe-rag-ingest"
    assert "EMBED_API_KEY" not in new_env  # keyless strip

    vols = {v["name"]: v for v in new_job["spec"]["template"]["spec"]["volumes"]}
    assert vols["corpus"] == {
        "name": "corpus", "persistentVolumeClaim": {"claimName": "corpus-pvc", "readOnly": True},
    }
    assert vols["work"] == {"name": "work", "persistentVolumeClaim": {"claimName": "ingest-work"}}


def test_ingest_job_parity_maintenance_with_tricky_revision(tmp_path):
    tricky = "SA22-7777-01@vendor|product with spaces|v1|abc123 & more"
    old = run_old_ingest(make_old_tree(tmp_path), {
        "INGEST_ALIAS_PUBLISH": "true",
        "INGEST_REINGEST": "true",
        "INGEST_RETIRE_DOCS": f"SA22-0000-00,{tricky}",
        "GATEWAY_API_KEY_SECRET": "gw-keys",
        "PULL_SECRET": "ghcr-pull",
        "GATEWAY_CA_CONFIGMAP": "gw-ca",
    })
    new = run_new_job_only({"ingest": {
        "corpusPVC": "corpus-pvc",
        "aliasPublish": True,
        "reingest": True,
        "retireDocs": ["SA22-0000-00", tricky],
    }, "gateway": {"apiKeySecretName": "gw-keys", "caConfigMapName": "gw-ca"},
        "pullSecret": {"name": "ghcr-pull"}})
    old_args = next(
        c for c in old[("Job", "ingest")]["spec"]["template"]["spec"]["containers"]
        if c["name"] == "ingest"
    )["args"]
    new_args = next(
        c for c in new[("Job", "ingest")]["spec"]["template"]["spec"]["containers"]
        if c["name"] == "ingest"
    )["args"]
    expected = [
        "--src", "/corpus", "--progress", "/work/inventory.jsonl",
        "--reingest", "--retire-doc", "SA22-0000-00", "--retire-doc", tricky,
    ]
    assert old_args == expected
    assert new_args == expected

    for docs in (old, new):
        env = ingest_env_map(docs[("Job", "ingest")])
        assert env["INGEST_ALIAS_PUBLISH"]["value"] == "true"
        assert env["EMBED_API_KEY"]["valueFrom"]["secretKeyRef"] == {
            "name": "gw-keys", "key": "embed-api-key",
        }
        assert env["CONTEXT_LLM_API_KEY"]["valueFrom"]["secretKeyRef"] == {
            "name": "gw-keys", "key": "context-llm-api-key",
        }
        assert "LLM_API_KEY" not in env and "RERANK_API_KEY" not in env
        assert env["SSL_CERT_FILE"]["value"] == "/etc/gateway-ca/ca-bundle.crt"


def test_ingest_not_owned_by_default():
    new = run_new_template({})
    assert ("Job", "ingest") not in new
    raw = run(["helm", "template", "test", str(CHART), "--namespace", "ns"], cwd=REPO)
    assert "helm.sh/hook" not in raw.stdout
    assert "kind: Job" not in raw.stdout


def test_rerank_enabled_without_base_url_falls_back(tmp_path):
    """Empty RERANK_BASE_URL with rerank on is valid: HttpReranker falls back
    to the embedding URL (retrieve/rerank.py). The chart must preserve the
    bare render, not reject it."""
    old = run_old_deploy(make_old_tree(tmp_path), {
        "RERANK_ENABLED": "true", "RERANK_MODEL": "my-reranker",
    })
    new = run_new_template({"models": {"rerank": {
        "enabled": True, "baseUrl": "", "model": "my-reranker",
    }}})
    for docs in (old, new):
        env = env_map(docs[("Deployment", "rag-agent")])
        assert env["RERANK_ENABLED"]["value"] == "true"
        assert env["RERANK_BASE_URL"].get("value") in (None, "")


# ------------------------------------------------------- negative schema cases


def _helm_template_with_values(values: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        path = f.name
    try:
        return run(["helm", "template", "test", str(CHART), "--namespace", "ns", "-f", path], cwd=REPO)
    finally:
        Path(path).unlink(missing_ok=True)


def test_schema_rejects_invalid_selected_configuration():
    cases = [
        ({"models": {"embedding": {"dimension": 0}}}, "dimension"),
        ({"models": {"embedding": {"revision": "   "}}}, "revision"),
        ({"images": {"agent": {"tag": "HEAD"}}}, "tag"),
        ({"images": {"ingest": {"tag": "latest"}}}, "ingest"),
        ({"gateway": {"apiKeySecretName": "Bad_Name!"}}, "apiKeySecretName"),
        ({"models": {"rerank": {"endpointOrder": "nope"}}}, "endpointOrder"),
        ({"metrics": {"enabled": "false"}}, "metrics"),
        ({"models": {"reasoning": {"model": "foo", "baseUrl": ""}}}, "reasoning"),
        ({"route": {"enabled": True, "destinationCA": ""}}, "destinationCA"),
        ({"route": {"enabled": True, "timeoutSeconds": 0}}, "timeoutSeconds"),
        ({"storage": {"className": "nfs-client"}}, "storage"),
        ({"ingest": {"enabled": True, "corpusPVC": ""}}, "corpusPVC"),
        ({"ingest": {"enabled": True, "corpusPVC": "c", "retireDocs": ["DOC1"]}}, "aliasPublish"),
        ({"ingest": {"retireDocs": ['bad"quote']}}, "retireDocs"),
        ({"ingest": {"workers": 0}}, "workers"),
        ({"ingest": {"collectionPolicy": {"shardNumber": 0}}}, "shardNumber"),
    ]
    for mutate, needle in cases:
        values = base_values()
        values["ingest"]["corpusPVC"] = "c"

        def merge(dst, src):
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    merge(dst[k], v)
                else:
                    dst[k] = v

        merge(values, mutate)
        r = _helm_template_with_values(values)
        assert r.returncode != 0, f"schema accepted invalid {needle}"
        assert needle.split(".")[-1] in r.stderr or "schema" in r.stderr.lower()


def test_chart_shape_no_qdrant_no_hook_no_secret_values():
    names = sorted(p.name for p in (CHART / "templates").glob("*.yaml"))
    assert names == [
        "agent-deployment.yaml",
        "agent-service.yaml",
        "ingest-job.yaml",
        "jaeger-config.yaml",
        "jaeger-deployment.yaml",
        "jaeger-pvc.yaml",
        "jaeger-service.yaml",
        "route.yaml",
        "serviceaccount.yaml",
        "servicemonitor.yaml",
    ]
    text = "".join((CHART / "templates" / n).read_text() for n in names)
    assert "kind: StatefulSet" not in text
    assert "helm.sh/hook" not in text
    assert "qdrant" in text.lower()  # URL/Secret references only
    schema_text = (CHART / "values.schema.json").read_text()
    assert "read-only-api-key" not in schema_text
    values_text = (CHART / "values.yaml").read_text()
    assert "sk-" not in values_text
    assert "registry.example" not in text or "registry.example.internal" in (CHART / "values.yaml").read_text()
    assert "ghcr.io" not in text
