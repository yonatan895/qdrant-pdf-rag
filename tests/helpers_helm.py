"""Real Helm rendering with independent synthetic operator values."""
from __future__ import annotations

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
        "ui": {"enabled": True},
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

def run_new_template(extra_values: dict, namespace: str = "ns", *, job_only: bool = False) -> dict:
    """Render the new chart with synthetic values; return parsed docs."""
    values = base_values()
    if job_only:
        values["ingest"]["enabled"] = True
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
    r = _helm_template_with_values(values, ns, job_only=job_only)
    assert r.returncode == 0, f"helm template failed: {r.stderr}"
    if not job_only:
        assert "__" not in r.stdout, "placeholder leaked through chart render"
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

def _helm_template_with_values(values: dict, namespace: str = "ns", *, job_only: bool = False):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        path = f.name
    try:
        cmd = ["helm", "template", "test", str(CHART), "--namespace", namespace, "-f", path]
        if job_only:
            cmd.extend(["--show-only", "templates/ingest-job.yaml"])
        return run(cmd, cwd=REPO)
    finally:
        Path(path).unlink(missing_ok=True)
