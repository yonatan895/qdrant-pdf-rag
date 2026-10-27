"""Real Helm rendering with independent synthetic operator values."""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

CHART = REPO / "charts" / "mainframe-rag"

IMAGE_SHA = "a" * 40

FAKE_CA = "-----BEGIN CERTIFICATE-----\nFAKE-CA-BUNDLE\n-----END CERTIFICATE-----"

BASE_ENV = {
    "INTERNAL_REGISTRY": "reg.internal",
    "STORAGE_CLASS": "standard",
    "EMBED_MODEL": "embed-model",
    "DENSE_DIM": "768",
    "EMBED_MODEL_REVISION": "rev-1",
    "EMBED_BASE_URL": "http://vllm:8000/v1",
}

def run(cmd, cwd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, check=False)

def merge_values(dst: dict, src: dict) -> dict:
    """Recursive merge of synthetic values (`src` wins on scalars)."""
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            merge_values(dst[key], value)
        else:
            dst[key] = value
    return dst

def set_path(values: dict, path: str, value) -> dict:
    """Assign a dotted path in a values mapping, creating parents as needed."""
    *parents, leaf = path.split(".")
    node = values
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value
    return values

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
            "openobserve": {
                "repository": f"{BASE_ENV['INTERNAL_REGISTRY']}/openobserve/openobserve",
                "tag": "v1.1.2",
                "pullPolicy": "IfNotPresent",
            },
            "otelcol": {
                "repository": f"{BASE_ENV['INTERNAL_REGISTRY']}/otel/opentelemetry-collector-contrib",
                "tag": "0.160.0",
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
            "endpoint": "",
            "deploymentEnvironment": "",
            "serviceName": "",
        },
        "openobserve": {"credentialsSecretName": "openobserve-credentials"},
        "metrics": {"enabled": False},
        "ui": {"enabled": True},
        # Open WebUI is default-on (ADR-0008) and fails closed without site
        # inputs; the shared base is an explicit opt-out, tests that exercise
        # the front end override it with their own inputs.
        "openwebui": {"enabled": False},
        "networkPolicy": {"enabled": True, "allowedNamespaces": []},
        "route": {"enabled": False, "timeoutSeconds": 300, "destinationCA": ""},
        "storage": {"className": BASE_ENV["STORAGE_CLASS"]},
        "ingest": {
            "enabled": False,
            "corpusPVC": "",
            "workers": 4,
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

def run_new_template(extra_values: dict, namespace: str = "ns", *, job_only: bool = False,
                     chart: Path = CHART) -> dict:
    """Render the new chart (or a test copy of it) with synthetic values; return parsed docs."""
    values = base_values()
    if job_only:
        values["ingest"]["enabled"] = True
    ns = namespace
    extras = dict(extra_values)
    if "_namespace" in extras:
        ns = extras.pop("_namespace")

    merge_values(values, extras)
    r = _helm_template_with_values(values, ns, job_only=job_only, chart=chart)
    assert r.returncode == 0, f"helm template failed: {r.stderr}"
    if not job_only:
        # The deploy gate's placeholder form (fail_on_placeholders); Python
        # dunders in the Open WebUI ConfigMap (#1074) are not placeholders.
        assert not re.search(r"__[A-Z][A-Z0-9_]*__", r.stdout), "placeholder leaked through chart render"
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    if job_only:
        assert len(docs) == 1 and docs[0]["kind"] == "Job", f"expected one Job, got: {r.stdout[:500]}"
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}

def run_new_job_only(extra_values: dict, namespace: str = "ns") -> dict:
    """Render only the ingest Job template (the explicit-operation path)."""
    return run_new_template(extra_values, namespace, job_only=True)

def env_map(deployment: dict, container: str = "agent") -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    c = next(x for x in containers if x["name"] == container)
    return {e["name"]: e for e in c.get("env", [])}

def container_map(deployment: dict) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return {c["name"]: c for c in containers}

def ingest_env_map(job: dict) -> dict:
    c = next(x for x in job["spec"]["template"]["spec"]["containers"] if x["name"] == "ingest")
    return {e["name"]: e for e in c.get("env", [])}

def _helm_template_with_values(values: dict, namespace: str = "ns", *, job_only: bool = False,
                               chart: Path = CHART):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        path = f.name
    try:
        cmd = ["helm", "template", "test", str(chart), "--namespace", namespace, "-f", path]
        if job_only:
            cmd.extend(["--show-only", "templates/ingest-job.yaml"])
        return run(cmd, cwd=REPO)
    finally:
        Path(path).unlink(missing_ok=True)
