"""map_values.py unit + producer round-trip.

The mapper reads the already-resolved operator environment (single owner:
common.sh) and writes the Helm release values with a stdlib-only
deterministic emitter. Tests pin the mapping table, the OTEL/bool mirrors
of the shell helpers, fail-closed behavior, and the full producer chain
(env -> values file -> helm template -> parsed resources), including tricky
revisions with spaces/pipes/ampersands. No cluster required (helm template
only); the helm round-trip skips without helm.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
MAPPER = REPO / "scripts" / "airgap" / "map_values.py"
CHART = REPO / "charts" / "mainframe-rag"

IMAGE_SHA = "a" * 40

MAPPER_KEYS = [
    "INTERNAL_REGISTRY", "REGISTRY_INTERNAL", "NAMESPACE", "OPENSHIFT_NAMESPACE",
    "QDRANT_RELEASE", "IMAGE_SHA", "EMBED_BASE_URL", "VLLM_BASE_URL",
    "EMBED_MODEL", "DENSE_DIM", "EMBED_MODEL_REVISION",
    "LLM_BASE_URL", "LLM_MODEL_REASONING",
    "RERANK_ENABLED", "RERANK_BASE_URL", "RERANK_MODEL", "RERANK_ENDPOINT_ORDER",
    "GATEWAY_API_KEY_SECRET", "GATEWAY_CA_CONFIGMAP", "PULL_SECRET",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_ENDPOINT_RESOLVED", "OTEL_TRACING_ENABLED",
    "OTEL_DEPLOYMENT_ENVIRONMENT", "OTEL_SERVICE_NAME",
    "METRICS_ENABLED", "AGENT_ROUTE", "ROUTE_DESTINATION_CA_FILE",
    "STORAGE_CLASS", "CORPUS_PVC", "INGEST_WORKERS", "INGEST_WORK_SIZE",
    "INGEST_ALIAS_PUBLISH", "INGEST_REINGEST", "INGEST_RETIRE_DOCS",
    "CONTEXTUAL_EMBED_ENABLED", "CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL",
    "QDRANT_SHARD_NUMBER", "QDRANT_REPLICATION_FACTOR", "QDRANT_WRITE_CONSISTENCY_FACTOR",
]

BASE_ENV = {
    "INTERNAL_REGISTRY": "reg.internal",
    "NAMESPACE": "ns",
    "QDRANT_RELEASE": "qdrant",
    "IMAGE_SHA": IMAGE_SHA,
    "EMBED_BASE_URL": "http://vllm:8000/v1",
    "EMBED_MODEL": "embed-model",
    "DENSE_DIM": "768",
    "EMBED_MODEL_REVISION": "rev-1",
    "STORAGE_CLASS": "standard",
    "QDRANT_SHARD_NUMBER": "6",
    "QDRANT_REPLICATION_FACTOR": "3",
    "QDRANT_WRITE_CONSISTENCY_FACTOR": "2",
}


@pytest.fixture
def mapper_env(monkeypatch):
    """Clean all mapper inputs, then install the base mapping."""
    for key in MAPPER_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in ("LLM_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY", "CONTEXT_LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, val in BASE_ENV.items():
        monkeypatch.setenv(key, val)

    def run_mapper(*argv):
        out = Path(tempfile.mkdtemp()) / "release-values.yaml"
        r = subprocess.run(
            ["python3", str(MAPPER), "--out", str(out), *argv],
            capture_output=True, text=True, cwd=REPO, check=False,
        )
        return r, out

    return run_mapper


def load_values(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_full_mapping_table(mapper_env):
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    v = load_values(out)
    assert v["images"]["agent"] == {
        "repository": "reg.internal/qdrant-pdf-rag-agent",
        "tag": IMAGE_SHA, "pullPolicy": "IfNotPresent",
    }
    assert v["images"]["ingest"]["tag"] == IMAGE_SHA
    assert v["images"]["jaeger"]["tag"] == "v2.20.0"
    assert v["images"]["oauthProxy"]["tag"] == "v4.14"
    assert v["qdrantRelease"] == "qdrant"
    assert v["models"]["embedding"] == {
        "baseUrl": "http://vllm:8000/v1", "model": "embed-model",
        "revision": "rev-1", "dimension": 768,
    }
    assert isinstance(v["models"]["embedding"]["dimension"], int)
    assert v["models"]["rerank"]["model"] == "BAAI/bge-reranker-v2-m3"
    assert v["models"]["rerank"]["endpointOrder"] == "score_first"
    assert v["tracing"] == {
        "enabled": True, "endpoint": "http://jaeger:4318",
        "deploymentEnvironment": "", "serviceName": "",
    }
    assert v["metrics"] == {"enabled": False}
    assert v["route"] == {"enabled": False, "timeoutSeconds": 300, "destinationCA": ""}
    assert v["storage"] == {"className": "standard"}
    assert v["ingest"]["enabled"] is False  # no CORPUS_PVC
    assert v["pullSecret"] == {"name": ""}
    assert v["replicaCount"] == 2


def test_embed_base_url_derived_from_vllm(monkeypatch, mapper_env):
    monkeypatch.delenv("EMBED_BASE_URL")
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm:8000/v1///")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["models"]["embedding"]["baseUrl"] == "http://vllm:8000/v1"


@pytest.mark.parametrize("token,enabled,endpoint", [
    ("", True, "http://jaeger:4318"),
    ("off", False, ""), ("none", False, ""), ("false", False, ""),
    ("0", False, ""), ("OFF", False, ""),
    ("http://collector:4318", True, "http://collector:4318"),
])
def test_otel_mirror(monkeypatch, mapper_env, token, enabled, endpoint):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", token)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    t = load_values(out)["tracing"]
    assert t["enabled"] is enabled and t["endpoint"] == endpoint


def test_otel_bad_value_fails_closed(monkeypatch, mapper_env):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "jaeger:4318")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in r.stderr


def test_otel_prefers_shell_resolved(monkeypatch, mapper_env):
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "0")
    monkeypatch.setenv("OTEL_ENDPOINT_RESOLVED", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://ignored:4318")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    t = load_values(out)["tracing"]
    assert t == {"enabled": False, "endpoint": "",
                 "deploymentEnvironment": "", "serviceName": ""}


@pytest.mark.parametrize("raw,expected", [
    ("", False), ("true", True), ("1", True), ("YES", True), ("false", False),
    ("0", False), ("no", False), ("anything-else", False),
])
def test_lenient_rerank_bool(monkeypatch, mapper_env, raw, expected):
    monkeypatch.setenv("RERANK_ENABLED", raw)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["models"]["rerank"]["enabled"] is expected


def test_metrics_literal_true_only(monkeypatch, mapper_env):
    monkeypatch.setenv("METRICS_ENABLED", "1")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["metrics"] == {"enabled": False}
    monkeypatch.setenv("METRICS_ENABLED", "true")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["metrics"] == {"enabled": True}


@pytest.mark.parametrize("raw,expected", [
    ("", False), ("true", True), ("1", True), ("yes", True),
    ("false", False), ("0", False), ("no", False),
])
def test_strict_maintenance_bool(monkeypatch, mapper_env, raw, expected):
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", raw)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["ingest"]["aliasPublish"] is expected


def test_strict_bool_rejects_garbage(monkeypatch, mapper_env):
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("INGEST_REINGEST", "maybe")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "INGEST_REINGEST" in r.stderr


def test_ingest_enabled_block_and_tricky_retire(monkeypatch, mapper_env):
    tricky = "SA22-7777-01@vendor|product with spaces|v1|abc123 & more"
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("INGEST_WORKERS", "8")
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.setenv("INGEST_REINGEST", "true")
    monkeypatch.setenv("INGEST_RETIRE_DOCS", f"SA22-0000-00,\n{tricky}\n")
    monkeypatch.setenv("GATEWAY_API_KEY_SECRET", "gw-keys")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    ing = load_values(out)["ingest"]
    assert ing["enabled"] is True
    assert ing["corpusPVC"] == "corpus-pvc"
    assert ing["workers"] == 8 and isinstance(ing["workers"], int)
    assert ing["aliasPublish"] is True and ing["reingest"] is True
    assert ing["retireDocs"] == ["SA22-0000-00", tricky]
    assert ing["collectionPolicy"] == {
        "shardNumber": 6, "replicationFactor": 3, "writeConsistencyFactor": 2,
    }


def test_retire_without_alias_fails_closed(monkeypatch, mapper_env):
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("INGEST_RETIRE_DOCS", "DOC1")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "INGEST_ALIAS_PUBLISH" in r.stderr


def test_route_needs_ca_file(monkeypatch, mapper_env, tmp_path):
    monkeypatch.setenv("AGENT_ROUTE", "true")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "ROUTE_DESTINATION_CA_FILE" in r.stderr
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("ROUTE_DESTINATION_CA_FILE", str(ca))
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    route = load_values(out)["route"]
    assert route["enabled"] is True and route["timeoutSeconds"] == 300
    assert "FAKE" in route["destinationCA"]


@pytest.mark.parametrize("key,val", [
    ("IMAGE_SHA", ""), ("IMAGE_SHA", "HEAD"), ("IMAGE_SHA", "abc123"),
    ("DENSE_DIM", "0"), ("DENSE_DIM", "abc"), ("EMBED_MODEL_REVISION", "   "),
    ("INTERNAL_REGISTRY", ""), ("STORAGE_CLASS", ""),
    ("INGEST_WORKERS", "0"),
])
def test_fail_closed_inputs(monkeypatch, mapper_env, key, val):
    if key == "INGEST_WORKERS":
        monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv(key, val)
    if key == "INTERNAL_REGISTRY":
        monkeypatch.delenv("REGISTRY_INTERNAL", raising=False)
    r, _ = mapper_env()
    assert r.returncode != 0, f"accepted invalid {key}={val!r}"
    assert "FAIL" in r.stderr


def test_without_ingest_job_mode(monkeypatch, mapper_env):
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.delenv("QDRANT_SHARD_NUMBER")
    monkeypatch.delenv("QDRANT_REPLICATION_FACTOR")
    monkeypatch.delenv("QDRANT_WRITE_CONSISTENCY_FACTOR")
    r, out = mapper_env("--without-ingest-job")
    assert r.returncode == 0, r.stderr
    assert load_values(out)["ingest"]["enabled"] is False


def test_no_secret_material_leaks(monkeypatch, mapper_env):
    monkeypatch.setenv("LLM_API_KEY", "sk-fake-llm")
    monkeypatch.setenv("EMBED_API_KEY", "sk-fake-embed")
    monkeypatch.setenv("GATEWAY_API_KEY_SECRET", "gw-keys")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert "sk-fake-llm" not in text and "sk-fake-embed" not in text


def test_emitter_round_trip_byte_exact():
    """Tricky strings survive emit -> parse with identical values."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("map_values", str(MAPPER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tricky = "a|b /c &d \"q\" 's'\ttab\nnewline snowman"
    lines = mod.emit_yaml({"k": tricky, "n": 6, "b": True, "l": [tricky], "e": ""})
    parsed = yaml.safe_load("\n".join(lines))
    assert parsed == {"k": tricky, "n": 6, "b": True, "l": [tricky], "e": ""}


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required for the round-trip")
def test_mapper_to_helm_round_trip(mapper_env, tmp_path):
    """Producer-to-consumer: env -> values -> helm template -> resources."""
    tricky = "SA22-7777-01@vendor|product with spaces|v1|abc123 & more"
    os.environ["CORPUS_PVC"] = "corpus-pvc"
    os.environ["INGEST_ALIAS_PUBLISH"] = "true"
    os.environ["INGEST_RETIRE_DOCS"] = tricky
    os.environ["RERANK_ENABLED"] = "true"
    os.environ["RERANK_BASE_URL"] = "http://rerank:8002/v1"
    try:
        out = tmp_path / "release-values.yaml"
        r = subprocess.run(
            ["python3", str(MAPPER), "--out", str(out)], capture_output=True,
            text=True, cwd=REPO, check=False,
        )
        assert r.returncode == 0, r.stderr
        t = subprocess.run(
            ["helm", "template", "app", str(CHART), "-f", str(out),
             "--namespace", "ns", "--show-only", "templates/ingest-job.yaml"],
            capture_output=True, text=True, cwd=REPO, check=False,
        )
        assert t.returncode == 0, t.stderr
        job = yaml.safe_load(t.stdout)
        containers = job["spec"]["template"]["spec"]["containers"]
        ingest = next(c for c in containers if c["name"] == "ingest")
        assert ingest["args"] == [
            "--src", "/corpus", "--progress", "/work/inventory.jsonl",
            "--retire-doc", tricky,
        ]
        env = {e["name"]: e for e in ingest["env"]}
        assert "RERANK_ENABLED" not in env  # agent-only key
        assert env["QDRANT_SHARD_NUMBER"]["value"] == "6"
        assert env["INGEST_ALIAS_PUBLISH"]["value"] == "true"
    finally:
        for k in ("CORPUS_PVC", "INGEST_ALIAS_PUBLISH", "INGEST_RETIRE_DOCS",
                  "RERANK_ENABLED", "RERANK_BASE_URL"):
            os.environ.pop(k, None)


def test_reasoning_disabled_when_model_unset(monkeypatch, mapper_env):
    monkeypatch.delenv("LLM_MODEL_REASONING", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://litellm:4000/v1")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    reasoning = load_values(out)["models"]["reasoning"]
    assert reasoning == {"baseUrl": "", "model": ""}


def test_reasoning_mapped_when_both_set(monkeypatch, mapper_env):
    monkeypatch.setenv("LLM_MODEL_REASONING", "mock-reasoning")
    monkeypatch.setenv("LLM_BASE_URL", "http://litellm:4000/v1")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    reasoning = load_values(out)["models"]["reasoning"]
    assert reasoning == {"baseUrl": "http://litellm:4000/v1", "model": "mock-reasoning"}


def test_reasoning_fails_closed_when_model_set_without_base_url(monkeypatch, mapper_env):
    monkeypatch.setenv("LLM_MODEL_REASONING", "mock-reasoning")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "LLM_BASE_URL is required when LLM_MODEL_REASONING is set" in r.stderr



@pytest.mark.parametrize("namespace", ["bad\nfield: value", "bad.name", "Upper", "-bad", "bad-", "a" * 64])
def test_namespace_rejects_invalid_label_before_values_write(mapper_env, monkeypatch, namespace):
    monkeypatch.setenv("NAMESPACE", namespace)
    result, out = mapper_env("--without-ingest-job")
    assert result.returncode != 0
    assert "NAMESPACE must be a DNS label" in result.stderr
    assert not out.exists()
