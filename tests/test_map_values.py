"""map_values.py unit + producer round-trip.

The mapper reads the operator environment and writes the Helm release values
with a stdlib-only deterministic emitter; it is also the one owner of the
observability decisions the shell stages consume (--bundle-decisions). Tests
pin the mapping table, those decisions against the rendered release,
fail-closed behavior, and the full producer chain
(env -> values file -> helm template -> parsed resources), including tricky
revisions with spaces/pipes/ampersands. No cluster required (helm template
only); the helm round-trip skips without helm.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
MAPPER = REPO / "scripts" / "airgap" / "map_values.py"
CHART = REPO / "charts" / "mainframe-rag"

IMAGE_SHA = "a" * 40

OPENWEBUI_KEYS = [
    "OPENWEBUI_ENABLED", "OPENWEBUI_SECRET", "OPENWEBUI_HOST", "OPENWEBUI_RETENTION_DAYS",
    "OPENWEBUI_STORAGE_SIZE", "OPENWEBUI_OIDC_PROVIDER_URL", "OPENWEBUI_OIDC_CLIENT_ID",
    "OPENWEBUI_OIDC_GROUP_CLAIM", "OPENWEBUI_OIDC_ROLES_CLAIM", "OPENWEBUI_OIDC_ALLOWED_ROLES",
    "OPENWEBUI_OIDC_ADMIN_ROLES", "OPENWEBUI_IDP_EGRESS_CIDRS", "OPENWEBUI_IDP_EGRESS_PORT",
    "ROUTE_DESTINATION_CA_FILE", "IMAGE_DIGEST_OPENWEBUI", "IMAGE_DIGEST_OPENWEBUI_PROXY",
]
MAPPER_KEYS = [
    *OPENWEBUI_KEYS,
    "INTERNAL_REGISTRY", "REGISTRY_INTERNAL", "NAMESPACE", "OPENSHIFT_NAMESPACE",
    "QDRANT_RELEASE", "IMAGE_SHA", "EMBED_BASE_URL", "VLLM_BASE_URL",
    "EMBED_MODEL", "DENSE_DIM", "EMBED_MODEL_REVISION",
    "LLM_BASE_URL", "LLM_MODEL_REASONING", "CHAT_CONDENSE_ENABLED",
    "LLM_MAX_MODEL_LEN", "LLM_RESERVED_OUTPUT_TOKENS",
    "LLM_THINKING_RESERVE_TOKENS_SIMPLE", "LLM_THINKING_RESERVE_TOKENS_COMPLEX",
    "LLM_REASONING_EFFORT_SIMPLE", "LLM_REASONING_EFFORT_COMPLEX", "LLM_REASONING_EFFORTS", "LLM_STREAM",
    "REQUEST_DEADLINE_S", "REQUEST_MAX_CONCURRENT", "REQUEST_QUEUE_MAX", "REQUEST_QUEUE_WAIT_S",
    "RERANK_ENABLED", "RERANK_BASE_URL", "RERANK_MODEL", "RERANK_ENDPOINT_ORDER",
    "GATEWAY_API_KEY_SECRET", "GATEWAY_API_KEY_SECRET_KEY", "GATEWAY_CA_CONFIGMAP", "PULL_SECRET",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_ENDPOINT_RESOLVED", "OTEL_TRACING_ENABLED",
    "OTEL_DEPLOYMENT_ENVIRONMENT", "OTEL_SERVICE_NAME",
    "METRICS_ENABLED", "SERVICEMONITOR_ENABLED", "AGENT_ROUTE", "ROUTE_DESTINATION_CA_FILE",
    "UI_ENABLED", "AGENT_NETWORK_POLICY", "AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES",
    "FEEDBACK_ENABLED", "FEEDBACK_SECRET", "FEEDBACK_STORAGE_SIZE", "FEEDBACK_RETENTION_DAYS",
    "IMAGE_DIGEST_AGENT", "IMAGE_DIGEST_INGEST", "IMAGE_DIGEST_OPENOBSERVE", "IMAGE_DIGEST_OTELCOL", "IMAGE_DIGEST_OAUTH_PROXY",
    "STORAGE_CLASS", "CORPUS_PVC", "INGEST_WORKERS", "INGEST_WORK_SIZE",
    "INGEST_ALIAS_PUBLISH", "INGEST_REINGEST", "INGEST_RETIRE_DOCS",
    "INGEST_AUTHORIZE_FROM_CATALOG",
    "CONTEXTUAL_EMBED_ENABLED", "CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL",
    "QDRANT_PEER_URLS", "QDRANT_SHARD_NUMBER", "QDRANT_REPLICATION_FACTOR", "QDRANT_WRITE_CONSISTENCY_FACTOR",
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
    "OPENWEBUI_ENABLED": "false",  # default-on (ADR-0008): base mapping opts out explicitly
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
    assert "jaeger" not in v["images"]
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
        "enabled": True, "endpoint": "",
        "deploymentEnvironment": "", "serviceName": "",
    }
    assert v["metrics"] == {"enabled": False, "serviceMonitor": {"enabled": False}}
    assert v["route"] == {"enabled": False, "timeoutSeconds": 300, "destinationCA": ""}
    assert v["storage"] == {"className": "standard"}
    assert v["ingest"]["enabled"] is False  # no CORPUS_PVC
    assert v["pullSecret"] == {"name": ""}
    assert v["replicaCount"] == 2


def test_removed_legacy_env_names_are_not_honored(monkeypatch, mapper_env):
    """The removed aliases no longer stand in for the current inputs: a
    VLLM_BASE_URL origin never supplies the embedding URL, and
    REGISTRY_INTERNAL never supplies the registry."""
    monkeypatch.delenv("EMBED_BASE_URL")
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm:8000/v1")
    r, _ = mapper_env()
    assert r.returncode != 0 and "EMBED_BASE_URL is required" in r.stderr
    monkeypatch.setenv("EMBED_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.delenv("INTERNAL_REGISTRY", raising=False)
    monkeypatch.setenv("REGISTRY_INTERNAL", "old.example:5000")
    r, _ = mapper_env()
    assert r.returncode != 0 and "INTERNAL_REGISTRY is not set" in r.stderr


@pytest.mark.parametrize("token,enabled,endpoint", [
    ("", True, ""),
    ("off", False, ""), ("OFF", False, ""),
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


def test_otel_is_owned_by_the_mapper_not_stale_shell_state(monkeypatch, mapper_env):
    """One owner: decisions exported by an earlier shell stage can never
    override the operator's input in the rendered release."""
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "0")
    monkeypatch.setenv("OTEL_ENDPOINT_RESOLVED", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    t = load_values(out)["tracing"]
    assert (t["enabled"], t["endpoint"]) == (True, "http://collector:4318")
    assert "jaeger" not in t


@pytest.mark.parametrize(("environment", "expected"), [
    ({}, {"OTEL_TRACING_ENABLED": "1", "OTEL_ENDPOINT_RESOLVED": "",
          "OTEL_BUNDLED": "1", "OTEL_COLLECTOR": "0", "SERVICEMONITOR_DEPLOY": "0"}),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "off"},
     {"OTEL_TRACING_ENABLED": "0", "OTEL_ENDPOINT_RESOLVED": "",
      "OTEL_BUNDLED": "0", "OTEL_COLLECTOR": "0", "SERVICEMONITOR_DEPLOY": "0"}),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.platform:4318"},
     {"OTEL_TRACING_ENABLED": "1", "OTEL_ENDPOINT_RESOLVED": "http://collector.platform:4318",
      "OTEL_BUNDLED": "0", "OTEL_COLLECTOR": "0", "SERVICEMONITOR_DEPLOY": "0"}),
    ({"METRICS_ENABLED": "true"},
     {"OTEL_TRACING_ENABLED": "1", "OTEL_ENDPOINT_RESOLVED": "",
      "OTEL_BUNDLED": "1", "OTEL_COLLECTOR": "1", "SERVICEMONITOR_DEPLOY": "1"}),
])
def test_bundle_decisions_match_the_rendered_release(monkeypatch, mapper_env, environment, expected):
    """The shell stages `eval` --bundle-decisions; the release renders the
    same function's booleans, so smoke/cleanup gates and the chart agree by
    construction (no mirror to keep in sync)."""
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    r = subprocess.run(["python3", str(MAPPER), "--bundle-decisions"], capture_output=True,
                       text=True, cwd=REPO, check=False)
    assert r.returncode == 0, r.stderr
    decisions = dict(line.split("=", 1) for line in r.stdout.splitlines())
    decisions["OTEL_ENDPOINT_RESOLVED"] = decisions["OTEL_ENDPOINT_RESOLVED"].strip("'")
    assert decisions == expected
    r2, out = mapper_env()
    assert r2.returncode == 0, r2.stderr
    v = load_values(out)
    assert ("openobserve" in v["images"]) is (expected["OTEL_BUNDLED"] == "1")
    # The chart renders otelcol only with the bundled backend and metrics on.
    assert ("otelcol" in v["images"]) is (expected["OTEL_COLLECTOR"] == "1")
    assert v["metrics"]["serviceMonitor"]["enabled"] is (expected["SERVICEMONITOR_DEPLOY"] == "1")


def test_bundle_decisions_quote_operator_endpoints_for_shell_eval(monkeypatch):
    """The endpoint is operator input evaluated by the shell: it must come
    back verbatim and never execute."""
    endpoint = "https://c.example:4318/$(touch pwned)'`id`"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    r = subprocess.run(["python3", str(MAPPER), "--bundle-decisions"], capture_output=True,
                       text=True, cwd=REPO, check=False)
    assert r.returncode == 0, r.stderr
    shell = subprocess.run(["sh", "-c", r.stdout + '\nprintf %s "$OTEL_ENDPOINT_RESOLVED"'],
                           capture_output=True, text=True, cwd=REPO, check=False)
    assert shell.stdout == endpoint
    assert not (REPO / "pwned").exists()


@pytest.mark.parametrize("raw,expected", [
    ("", False), ("YES", True), ("anything-else", False),
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
    assert load_values(out)["metrics"] == {"enabled": False, "serviceMonitor": {"enabled": False}}
    monkeypatch.setenv("METRICS_ENABLED", "true")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["metrics"] == {"enabled": True, "serviceMonitor": {"enabled": True}}


@pytest.mark.parametrize("raw", ["true"])
def test_retired_in_place_switch_accepts_publication(monkeypatch, mapper_env, raw):
    """In-place ingest was retired (#360): unset or true still renders, and
    the chart carries no switch (publication is the only mode)."""
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", raw)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert "aliasPublish" not in load_values(out)["ingest"]


@pytest.mark.parametrize("raw", ["false"])
def test_retired_in_place_switch_refuses_false(monkeypatch, mapper_env, raw):
    """An explicit false fails closed with the retirement, never silently
    rendering a publication the operator did not ask for."""
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", raw)
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "in-place ingest was retired" in r.stderr


def test_strict_bool_rejects_garbage(monkeypatch, mapper_env):
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("INGEST_REINGEST", "maybe")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "INGEST_REINGEST" in r.stderr


@pytest.mark.parametrize("raw,expected", [
    ("", True), ("true", True), ("1", True), ("yes", True),
    ("false", False), ("0", False), ("no", False),
])
def test_ui_enabled_selection(monkeypatch, mapper_env, raw, expected):
    # Issue #479: unset keeps the chart default (true); selected values map
    # exactly; false must survive truthiness/defaulting all the way down.
    monkeypatch.setenv("UI_ENABLED", raw)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["ui"] == {"enabled": expected}


def test_ui_enabled_rejects_garbage(monkeypatch, mapper_env):
    monkeypatch.setenv("UI_ENABLED", "maybe")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "UI_ENABLED must be true/false" in r.stderr
    assert "maybe" not in r.stderr  # the key is named, the value is not echoed


@pytest.mark.parametrize("raw,expected", [
    ("", True), ("true", True), ("1", True), ("YES", True),
    ("false", False), ("0", False), ("no", False),
])
def test_agent_network_policy_selection_defaults_on(monkeypatch, mapper_env, raw, expected):
    # Issue #373: the policy is default ON; only an explicit false turns it off.
    monkeypatch.setenv("AGENT_NETWORK_POLICY", raw)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["networkPolicy"] == {"enabled": expected, "allowedNamespaces": []}


def test_agent_network_policy_rejects_garbage(monkeypatch, mapper_env):
    monkeypatch.setenv("AGENT_NETWORK_POLICY", "maybe")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "AGENT_NETWORK_POLICY must be true/false" in r.stderr
    assert "maybe" not in r.stderr  # the key is named, the value is not echoed


def test_agent_network_policy_allowed_namespaces_round_trip(monkeypatch, mapper_env):
    monkeypatch.setenv("AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES", "mcp,tools-1,a")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["networkPolicy"] == {
        "enabled": True, "allowedNamespaces": ["mcp", "tools-1", "a"]}


@pytest.mark.parametrize("raw", [
    " mcp", "mcp ", "mcp, tools", "mcp,", ",mcp", "mcp,,tools", "MCP", "mcp_tools",
    "-mcp", "mcp-", "mcp.tools", "a" * 64, "mcp,mcp", "*", "mcp;tools",
])
def test_agent_network_policy_allowed_namespaces_are_strict(monkeypatch, mapper_env, raw):
    monkeypatch.setenv("AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES", raw)
    r, out = mapper_env()
    assert r.returncode != 0
    assert "AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES must" in r.stderr
    assert not out.exists()


def test_agent_network_policy_allowed_namespaces_accept_the_63_char_limit(monkeypatch, mapper_env):
    monkeypatch.setenv("AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES", "a" * 63)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["networkPolicy"]["allowedNamespaces"] == ["a" * 63]


def test_agent_network_policy_allow_list_requires_the_policy(monkeypatch, mapper_env):
    monkeypatch.setenv("AGENT_NETWORK_POLICY", "false")
    monkeypatch.setenv("AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES", "mcp")
    r, out = mapper_env()
    assert r.returncode != 0
    assert "requires AGENT_NETWORK_POLICY to be enabled" in r.stderr
    assert not out.exists()


def test_contextual_embed_invalid_value_is_refused_not_false(monkeypatch, mapper_env):
    """Issue #272: model_config used to read an invalid value as false."""
    monkeypatch.setenv("CONTEXTUAL_EMBED_ENABLED", "on")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "CONTEXTUAL_EMBED_ENABLED must be true/false" in r.stderr


def test_ingest_enabled_block_and_tricky_retire(monkeypatch, mapper_env):
    tricky = "SA22-7777-01@vendor|product with spaces|v1|abc123 & more"
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("INGEST_WORKERS", "8")
    monkeypatch.setenv("INGEST_REINGEST", "true")
    monkeypatch.setenv("INGEST_RETIRE_DOCS", f"SA22-0000-00,\n{tricky}\n")
    monkeypatch.setenv("GATEWAY_API_KEY_SECRET", "gw-keys")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    ing = load_values(out)["ingest"]
    assert ing["enabled"] is True
    assert ing["corpusPVC"] == "corpus-pvc"
    assert ing["workers"] == 8 and isinstance(ing["workers"], int)
    assert ing["reingest"] is True and "aliasPublish" not in ing
    assert ing["retireDocs"] == ["SA22-0000-00", tricky]
    assert ing["collectionPolicy"] == {
        "shardNumber": 6, "replicationFactor": 3, "writeConsistencyFactor": 2,
    }
    assert ing["authorizeFromCatalog"] is False, "the catalog authority is off by default"


@pytest.mark.parametrize("retire", ["SA22-0000-00@ibm|zos|2.5|abc", ""])
def test_authorize_from_catalog_maps_only_with_retire_docs(monkeypatch, mapper_env, retire):
    """Issue #1005 (option B): INGEST_AUTHORIZE_FROM_CATALOG maps to
    ingest.authorizeFromCatalog only together with INGEST_RETIRE_DOCS; alone
    it is refused at map time with a fixed message, never silently dropped."""
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("GATEWAY_API_KEY_SECRET", "gw-keys")
    monkeypatch.setenv("INGEST_AUTHORIZE_FROM_CATALOG", "true")
    if retire:
        monkeypatch.setenv("INGEST_RETIRE_DOCS", retire)
    r, out = mapper_env()
    if retire:
        assert r.returncode == 0, r.stderr
        ing = load_values(out)["ingest"]
        assert ing["authorizeFromCatalog"] is True
        assert ing["retireDocs"] == [retire]
    else:
        assert r.returncode != 0
        assert "INGEST_AUTHORIZE_FROM_CATALOG requires INGEST_RETIRE_DOCS" in r.stderr


def test_authorize_from_catalog_refuses_a_non_boolean(monkeypatch, mapper_env):
    monkeypatch.setenv("CORPUS_PVC", "corpus-pvc")
    monkeypatch.setenv("GATEWAY_API_KEY_SECRET", "gw-keys")
    monkeypatch.setenv("INGEST_RETIRE_DOCS", "SA22-0000-00")
    monkeypatch.setenv("INGEST_AUTHORIZE_FROM_CATALOG", "yes please")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "INGEST_AUTHORIZE_FROM_CATALOG" in r.stderr


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
    ("IMAGE_SHA", "HEAD"), ("IMAGE_SHA", "abc123"),
    ("DENSE_DIM", "0"), ("DENSE_DIM", "abc"), ("EMBED_MODEL_REVISION", "   "),
    ("INTERNAL_REGISTRY", ""),
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
        assert "INGEST_ALIAS_PUBLISH" not in env  # retired switch (#360)
    finally:
        for k in ("CORPUS_PVC", "INGEST_ALIAS_PUBLISH", "INGEST_RETIRE_DOCS",
                  "RERANK_ENABLED", "RERANK_BASE_URL"):
            os.environ.pop(k, None)


def test_reasoning_leg_is_mapped_only_when_model_is_set(monkeypatch, mapper_env):
    monkeypatch.delenv("LLM_MODEL_REASONING", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://litellm:4000/v1")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["models"]["reasoning"] == {"baseUrl": "", "model": "", "condenseEnabled": False}
    monkeypatch.setenv("LLM_MODEL_REASONING", "mock-reasoning")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["models"]["reasoning"] == {
        "baseUrl": "http://litellm:4000/v1", "model": "mock-reasoning", "condenseEnabled": False}


def test_reasoning_fails_closed_when_model_set_without_base_url(monkeypatch, mapper_env):
    monkeypatch.setenv("LLM_MODEL_REASONING", "mock-reasoning")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "LLM_BASE_URL is required when LLM_MODEL_REASONING is set" in r.stderr


@pytest.mark.parametrize("missing", ["CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL"])
def test_contextual_pair_matches_preflight_validation(monkeypatch, mapper_env, missing):
    monkeypatch.setenv("CONTEXTUAL_EMBED_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_LLM_BASE_URL", "https://context/v1")
    monkeypatch.setenv("CONTEXT_LLM_MODEL", "context-model")
    monkeypatch.delenv(missing, raising=False)
    result, output = mapper_env()
    assert result.returncode != 0
    assert f"{missing} is required when CONTEXTUAL_EMBED_ENABLED is set" in result.stderr
    assert not output.exists()



def test_namespace_rejects_invalid_label_before_values_write(mapper_env, monkeypatch):
    for namespace in ["bad\nfield: value", "Upper", "-bad", "a" * 64]:
        monkeypatch.setenv("NAMESPACE", namespace)
        result, out = mapper_env("--without-ingest-job")
        assert result.returncode != 0, namespace
        assert "NAMESPACE must be a DNS label" in result.stderr
        assert not out.exists()


@pytest.mark.parametrize("raw,want", [("", False), ("true", True)])
def test_chat_condensation_operator_mapping(monkeypatch, mapper_env, raw, want):
    monkeypatch.setenv("CHAT_CONDENSE_ENABLED", raw)
    result, output = mapper_env()
    assert result.returncode == 0, result.stderr
    assert load_values(output)["models"]["reasoning"]["condenseEnabled"] is want


@pytest.mark.parametrize("stream,simple_reserve,complex_effort,efforts", [
    ("true", "512", "medium", "low,medium,high"),
    ("false", "0", "xhigh", " xhigh, low,medium"),  # accepted spelling reaches the pod canonically
])
def test_selected_model_budgets_reach_rendered_agent(monkeypatch, mapper_env, stream, simple_reserve, complex_effort,
                                                     efforts):
    selected = {
        "LLM_MAX_MODEL_LEN": "16384",
        "LLM_RESERVED_OUTPUT_TOKENS": "2048",
        "LLM_THINKING_RESERVE_TOKENS_SIMPLE": simple_reserve,
        "LLM_THINKING_RESERVE_TOKENS_COMPLEX": "2048",
        "LLM_REASONING_EFFORT_SIMPLE": "low",
        "LLM_REASONING_EFFORT_COMPLEX": complex_effort,
        "LLM_REASONING_EFFORTS": efforts,
        "LLM_STREAM": stream, "UI_ENABLED": "true",
        "CHAT_CONDENSE_ENABLED": "true", "RERANK_ENABLED": "false",
    }
    for name, value in selected.items():
        monkeypatch.setenv(name, value)
    result, output = mapper_env()
    assert result.returncode == 0, result.stderr
    rendered = subprocess.run(
        [str(REPO / ".tools/bin/helm"), "template", "app", str(CHART), "-f", str(output),
         "--namespace", "ns", "--show-only", "templates/agent-deployment.yaml"],
        capture_output=True, text=True, check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    deployment = yaml.safe_load(rendered.stdout)
    agent = deployment["spec"]["template"]["spec"]["containers"][0]
    actual = {entry["name"]: entry.get("value") for entry in agent["env"]}
    expected = {**selected, "LLM_REASONING_EFFORTS": ",".join(
        effort for effort in ("low", "medium", "high", "xhigh")
        if effort in {part.strip() for part in efforts.split(",")})}
    assert {name: actual.get(name) for name in selected} == expected


@pytest.mark.parametrize("name,bad", [
    ("LLM_MAX_MODEL_LEN", "511"),
    ("LLM_MAX_MODEL_LEN", "16384.0"),
    pytest.param("LLM_MAX_MODEL_LEN", "9" * 5000, id="oversized-integer"),
    ("LLM_RESERVED_OUTPUT_TOKENS", "16385"),
    ("LLM_THINKING_RESERVE_TOKENS_SIMPLE", "-1"),
    ("LLM_REASONING_EFFORT_SIMPLE", "unsupported-private-value"),
    ("LLM_REASONING_EFFORTS", "unsupported-private-value"),
    ("LLM_REASONING_EFFORTS", "low,low,high"),
    ("LLM_STREAM", "unsupported-private-value"),
])
def test_model_budget_invalid_selection_fails_without_artifact(monkeypatch, mapper_env, name, bad):
    monkeypatch.setenv(name, bad)
    result, output = mapper_env()
    assert result.returncode != 0
    assert name in result.stderr
    assert not output.exists()
    if bad == "unsupported-private-value":
        assert bad not in result.stderr + result.stdout


@pytest.mark.parametrize("selected,field", [
    # #748/#777/#917: a served domain without a selected effort. medium is the
    # default and served by every shipped domain, so xhigh is selected explicitly;
    # an unserved default (domain without medium) is refused too.
    ({"LLM_REASONING_EFFORTS": "low,medium,high", "LLM_REASONING_EFFORT_SIMPLE": "low",
      "LLM_REASONING_EFFORT_COMPLEX": "xhigh"}, "LLM_REASONING_EFFORT_COMPLEX"),
    ({"LLM_REASONING_EFFORTS": "low,medium,high", "LLM_REASONING_EFFORT_COMPLEX": "high",
      "LLM_REASONING_EFFORT_SIMPLE": "xhigh"}, "LLM_REASONING_EFFORT_SIMPLE"),
    ({"LLM_REASONING_EFFORTS": "low,high"}, "LLM_REASONING_EFFORT_SIMPLE"),
])
def test_effort_outside_served_set_fails_before_artifact(monkeypatch, mapper_env, selected, field):
    for name, value in selected.items():
        monkeypatch.setenv(name, value)
    result, output = mapper_env()
    assert result.returncode != 0
    assert f"{field} must be one of LLM_REASONING_EFFORTS" in result.stderr
    assert not output.exists()


def test_preflight_effort_mirror_matches_settings():
    """The bastion preflight cannot import pydantic; its mirror must not drift (#748)."""
    from scripts.airgap import model_config

    from mainframe_rag.config import REASONING_EFFORTS, Settings

    assert model_config.REASONING_EFFORTS == REASONING_EFFORTS
    fields = Settings.model_fields
    expected = {
        name.upper(): fields[name].default
        for name in ("llm_reasoning_effort_simple", "llm_reasoning_effort_complex", "llm_reasoning_efforts")}
    assert expected == model_config.REASONING_EFFORT_DEFAULTS


def test_empty_model_budget_inputs_preserve_application_defaults(monkeypatch, mapper_env):
    for name in ("LLM_MAX_MODEL_LEN", "LLM_RESERVED_OUTPUT_TOKENS",
                 "LLM_THINKING_RESERVE_TOKENS_SIMPLE", "LLM_THINKING_RESERVE_TOKENS_COMPLEX",
                 "LLM_REASONING_EFFORT_SIMPLE", "LLM_REASONING_EFFORT_COMPLEX", "LLM_REASONING_EFFORTS",
                 "LLM_STREAM"):
        monkeypatch.setenv(name, "")
    result, output = mapper_env()
    assert result.returncode == 0, result.stderr
    assert load_values(output)["models"]["reasoning"] == {
        "baseUrl": "", "model": "", "condenseEnabled": False,
    }


def test_decoupled_backend_flags_follow_the_parent_by_default(mapper_env):
    """OBS-2: an unset ServiceMonitor flag follows the parent leg; the release
    carries the effective boolean (the chart never re-derives the default).
    Tracing has no backend switch: OpenObserve is the only backend (#956)."""
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    v = load_values(out)
    assert "jaeger" not in v["tracing"]
    assert v["metrics"]["serviceMonitor"] == {"enabled": v["metrics"]["enabled"]}


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False)])
def test_servicemonitor_enabled_selection(monkeypatch, mapper_env, raw, expected):
    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setenv("SERVICEMONITOR_ENABLED", raw)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["metrics"] == {"enabled": True, "serviceMonitor": {"enabled": expected}}


@pytest.mark.parametrize("key", ["SERVICEMONITOR_ENABLED"])
def test_decoupled_flags_reject_garbage(monkeypatch, mapper_env, key):
    monkeypatch.setenv(key, "maybe")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert key in r.stderr


def test_monitor_for_disabled_metrics_fails_closed(monkeypatch, mapper_env):
    monkeypatch.setenv("SERVICEMONITOR_ENABLED", "true")
    r, _ = mapper_env()
    assert r.returncode != 0
    assert "SERVICEMONITOR_ENABLED=true requires METRICS_ENABLED=true" in r.stderr


def test_retired_jaeger_switch_is_ignored(monkeypatch, mapper_env):
    """#956: JAEGER_ENABLED no longer selects or refuses anything; tracing
    follows OTEL_EXPORTER_OTLP_ENDPOINT alone and no Jaeger key is emitted."""
    monkeypatch.setenv("JAEGER_ENABLED", "false")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    v = load_values(out)
    assert v["tracing"]["enabled"] is True and "jaeger" not in v["tracing"]
    assert "jaeger" not in v["images"]


@pytest.mark.parametrize("endpoint,enabled", [("", True), ("http://collector.platform:4318", False), ("off", False)])
def test_openobserve_and_otelcol_images_follow_tracing(monkeypatch, mapper_env, endpoint, enabled):
    """#956: the bundled backend (tracing on, empty endpoint) emits images.openobserve and
    images.otelcol (repository, tag, pullPolicy; never a digest, which lives in images.txt).
    Tracing off and an external endpoint (the chart renders no backend) emit neither, and
    the shell gates' OTEL_BUNDLED decision agrees with the images."""
    bundled = enabled
    decisions = subprocess.run(
        [sys.executable, "scripts/airgap/map_values.py", "--bundle-decisions"],
        capture_output=True, text=True, cwd=REPO, check=False,
        env={**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint})
    assert decisions.returncode == 0, decisions.stderr
    assert f"OTEL_BUNDLED={int(bundled)}" in decisions.stdout.split()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("METRICS_ENABLED", "true")  # otelcol renders only with metrics on
    monkeypatch.setenv("IMAGE_DIGEST_OPENOBSERVE", "sha256:" + "c" * 64)
    monkeypatch.setenv("IMAGE_DIGEST_OTELCOL", "sha256:" + "d" * 64)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    images = load_values(out)["images"]
    if not enabled:
        assert "openobserve" not in images and "otelcol" not in images
        assert load_values(out)["tracing"]["endpoint"] == (endpoint if endpoint != "off" else "")
        return
    assert images["openobserve"] == {
        "repository": "reg.internal/openobserve/openobserve", "tag": "v1.1.2",
        "pullPolicy": "IfNotPresent"}
    assert images["otelcol"] == {
        "repository": "reg.internal/otel/opentelemetry-collector-contrib", "tag": "0.160.0",
        "pullPolicy": "IfNotPresent"}


def test_openobserve_run_as_id_maps_only_when_set_and_bundled(monkeypatch, mapper_env):
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert "runAsId" not in load_values(out).get("openobserve", {})
    monkeypatch.setenv("OPENOBSERVE_RUN_AS_ID", "10001")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["openobserve"]["runAsId"] == 10001
    for bad in ("0", "abc", "70000"):
        monkeypatch.setenv("OPENOBSERVE_RUN_AS_ID", bad)
        assert mapper_env()[0].returncode != 0, bad
    monkeypatch.setenv("OPENOBSERVE_RUN_AS_ID", "10001")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.platform:4318")
    r, _ = mapper_env()
    assert r.returncode != 0 and "requires the bundled OpenObserve" in r.stderr


def test_mapper_to_helm_round_trip_decoupled(mapper_env, tmp_path):
    """Producer-to-consumer: decoupled flags survive env -> values -> helm."""
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector.platform:4318"
    os.environ["METRICS_ENABLED"] = "true"
    os.environ["SERVICEMONITOR_ENABLED"] = "false"
    try:
        out = tmp_path / "release-values.yaml"
        r = subprocess.run(
            ["python3", str(MAPPER), "--out", str(out)], capture_output=True,
            text=True, cwd=REPO, check=False,
        )
        assert r.returncode == 0, r.stderr
        v = load_values(out)
        assert "jaeger" not in v["tracing"]
        assert v["metrics"]["serviceMonitor"] == {"enabled": False}
        t = subprocess.run(
            ["helm", "template", "app", str(CHART), "-f", str(out),
             "--namespace", "ns"],
            capture_output=True, text=True, cwd=REPO, check=False,
        )
        assert t.returncode == 0, t.stderr
        kinds = {(d["kind"], d["metadata"]["name"]) for d in yaml.safe_load_all(t.stdout) if d}
        assert ("ServiceMonitor", "rag-agent") not in kinds
        agent = next(d for d in yaml.safe_load_all(t.stdout) if d
                     and (d["kind"], d["metadata"]["name"]) == ("Deployment", "rag-agent"))
        env = {e["name"]: e["value"] for e in agent["spec"]["template"]["spec"]["containers"][0]["env"]
               if "value" in e}
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector.platform:4318"
        assert env["METRICS_ENABLED"] == "true"
    finally:
        for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "METRICS_ENABLED", "SERVICEMONITOR_ENABLED"):
            os.environ.pop(k, None)



# ------------------------------------------------- verified image digests (#272)

def test_image_digest_env_renders_digest_per_image(monkeypatch, mapper_env):
    monkeypatch.setenv("IMAGE_DIGEST_AGENT", "sha256:" + "a" * 64)
    monkeypatch.setenv("IMAGE_DIGEST_OAUTH_PROXY", "sha256:" + "b" * 64)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    images = load_values(out)["images"]
    assert images["agent"]["digest"] == "sha256:" + "a" * 64
    assert images["oauthProxy"]["digest"] == "sha256:" + "b" * 64
    assert "digest" not in images["ingest"]
    assert images["agent"]["tag"] == IMAGE_SHA  # the tag stays informational
    monkeypatch.delenv("IMAGE_DIGEST_AGENT")
    monkeypatch.delenv("IMAGE_DIGEST_OAUTH_PROXY")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert all("digest" not in image for image in load_values(out)["images"].values())


def test_openobserve_digest_never_enters_chart_values(monkeypatch, mapper_env):
    """#956: like every chart image, OpenObserve renders repository and tag; the
    verified digest from the identity check is not copied into the values."""
    monkeypatch.setenv("IMAGE_DIGEST_OPENOBSERVE", "sha256:" + "c" * 64)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    image = load_values(out)["images"]["openobserve"]
    assert "digest" not in image and image["tag"] == "v1.1.2"
    assert image["repository"].endswith("/openobserve/openobserve")


@pytest.mark.parametrize("bad", ["latest", "sha256:" + "G" * 64])
def test_malformed_image_digest_fails_closed(monkeypatch, mapper_env, bad):
    monkeypatch.setenv("IMAGE_DIGEST_INGEST", bad)
    r, out = mapper_env()
    assert r.returncode == 1
    assert "IMAGE_DIGEST_INGEST must be a sha256 manifest digest" in r.stderr
    assert not out.exists()


# ------------------------------------------------ feedback service (#804)

FEEDBACK_ON = {"FEEDBACK_ENABLED": "true", "FEEDBACK_SECRET": "fb-secret"}


def test_feedback_is_absent_from_the_release_values_by_default(mapper_env):
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    values = load_values(out)
    assert "feedback" not in values and set(values["resources"]) == {"agent"}
    assert "feedback" not in out.read_text().lower()


def test_feedback_selection_round_trips_with_independent_defaults(monkeypatch, mapper_env):
    for key, value in FEEDBACK_ON.items():
        monkeypatch.setenv(key, value)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    values = load_values(out)
    # ADR-0005 retention and the D8 size are applied when the operator is silent.
    assert values["feedback"] == {"enabled": True, "secretName": "fb-secret",
                                  "storageSize": "10Gi", "retentionDays": 180}
    assert values["resources"]["feedback"] == {"requests": {"cpu": "50m", "memory": "64Mi"},
                                               "limits": {"cpu": "250m", "memory": "256Mi"}}
    monkeypatch.setenv("FEEDBACK_STORAGE_SIZE", "1Ti")
    monkeypatch.setenv("FEEDBACK_RETENTION_DAYS", "3650")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["feedback"] == {"enabled": True, "secretName": "fb-secret",
                                            "storageSize": "1Ti", "retentionDays": 3650}


@pytest.mark.parametrize("env,message", [
    ({"FEEDBACK_ENABLED": "maybe"}, "FEEDBACK_ENABLED must be true/false"),
    ({"FEEDBACK_ENABLED": "true"}, "FEEDBACK_SECRET is required"),
    ({**FEEDBACK_ON, "FEEDBACK_SECRET": "Bad_Name"}, "FEEDBACK_SECRET must be"),
    ({**FEEDBACK_ON, "FEEDBACK_SECRET": "a" * 254}, "FEEDBACK_SECRET must be"),
    ({**FEEDBACK_ON, "FEEDBACK_STORAGE_SIZE": "10"}, "FEEDBACK_STORAGE_SIZE must be"),
    ({**FEEDBACK_ON, "FEEDBACK_STORAGE_SIZE": "0Gi"}, "FEEDBACK_STORAGE_SIZE must be"),
    ({**FEEDBACK_ON, "FEEDBACK_STORAGE_SIZE": "10 Gi"}, "FEEDBACK_STORAGE_SIZE must be"),
    ({**FEEDBACK_ON, "FEEDBACK_RETENTION_DAYS": "0"}, "FEEDBACK_RETENTION_DAYS must be"),
    ({**FEEDBACK_ON, "FEEDBACK_RETENTION_DAYS": "3651"}, "FEEDBACK_RETENTION_DAYS must be"),
    ({**FEEDBACK_ON, "FEEDBACK_RETENTION_DAYS": "1e2"}, "FEEDBACK_RETENTION_DAYS must be"),
    ({**FEEDBACK_ON, "FEEDBACK_RETENTION_DAYS": "-5"}, "FEEDBACK_RETENTION_DAYS must be"),
    ({**FEEDBACK_ON, "AGENT_NETWORK_POLICY": "false"}, "requires AGENT_NETWORK_POLICY to be enabled"),
    # Shapes are checked even while the feature is off: a typo fails today.
    ({"FEEDBACK_RETENTION_DAYS": "forever"}, "FEEDBACK_RETENTION_DAYS must be"),
])
def test_feedback_selection_is_strict_and_writes_nothing(monkeypatch, mapper_env, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    r, out = mapper_env()
    assert r.returncode != 0
    assert message in r.stderr
    assert not out.exists()
    for value in env.values():
        if len(value) > 4:
            assert value not in r.stderr  # the key is named, the value is not echoed


def test_feedback_off_with_the_network_policy_off_is_still_accepted(monkeypatch, mapper_env):
    monkeypatch.setenv("AGENT_NETWORK_POLICY", "false")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert "feedback" not in load_values(out)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required for the round-trip")
def test_feedback_mapper_to_helm_round_trip(monkeypatch, mapper_env, tmp_path):
    """Producer-to-consumer: operator env -> values -> helm template -> the pod
    and claim the operator asked for, and the default env renders no feedback."""
    for key, value in {**FEEDBACK_ON, "FEEDBACK_STORAGE_SIZE": "30Gi", "FEEDBACK_RETENTION_DAYS": "45",
                       "METRICS_ENABLED": "true"}.items():
        monkeypatch.setenv(key, value)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    rendered = subprocess.run(["helm", "template", "app", str(CHART), "-f", str(out), "--namespace", "ns"],
                              capture_output=True, text=True, cwd=REPO, check=False)
    assert rendered.returncode == 0, rendered.stderr
    docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(rendered.stdout) if d}
    pvc = docs["PersistentVolumeClaim", "rag-feedback-data"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "30Gi"
    assert pvc["spec"]["storageClassName"] == "standard"
    pod = docs["StatefulSet", "rag-feedback"]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in pod["env"]}
    assert env["FEEDBACK_RETENTION_DAYS"]["value"] == "45"
    assert env["FEEDBACK_SERVICE_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "key": "service-token", "name": "fb-secret"}
    assert ("ServiceMonitor", "rag-feedback") in docs and ("NetworkPolicy", "rag-feedback-ingress") in docs


# ------------------------------------------- Open WebUI front end (#1074)

OWUI_ON = {
    "OPENWEBUI_ENABLED": "true", "OPENWEBUI_SECRET": "owui-secrets",
    "OPENWEBUI_HOST": "docs.apps.example.internal",
    "OPENWEBUI_OIDC_PROVIDER_URL": "https://idp.example.internal/realms/x/.well-known/openid-configuration",
    "OPENWEBUI_OIDC_CLIENT_ID": "docs-frontend", "OPENWEBUI_OIDC_ALLOWED_ROLES": "staff",
    "OPENWEBUI_OIDC_ADMIN_ROLES": "docs-admins", "OPENWEBUI_IDP_EGRESS_CIDRS": "10.20.0.0/24, 10.21.0.8/32",
}
OWUI_CA = "-----BEGIN CERTIFICATE-----\nT1dVSS1DQQ==\n-----END CERTIFICATE-----\n"


def _owui_env(monkeypatch, tmp_path, **extra):
    ca = tmp_path / "service-ca.crt"
    ca.write_text(OWUI_CA)
    for key, value in {**OWUI_ON, "ROUTE_DESTINATION_CA_FILE": str(ca), **extra}.items():
        monkeypatch.setenv(key, value)


def test_openwebui_opt_out_renders_only_the_explicit_off_switch(mapper_env):
    """OPENWEBUI_ENABLED=false (the base env) reaches the chart as enabled=false:
    the chart default is on, so the opt-out must be written, and nothing else
    of the front end (image, route CA, inputs) is emitted (K1)."""
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    values = load_values(out)
    assert values["openwebui"] == {"enabled": False}
    assert "openwebui" not in values["images"] and "openwebuiProxy" not in values["images"]


def test_openwebui_unset_is_enabled_and_fails_closed_naming_the_opt_out(monkeypatch, mapper_env):
    monkeypatch.delenv("OPENWEBUI_ENABLED", raising=False)
    r, out = mapper_env()
    assert r.returncode != 0 and not out.exists()
    assert ("inputs are missing: OPENWEBUI_SECRET, OPENWEBUI_HOST, OPENWEBUI_OIDC_PROVIDER_URL, "
            "OPENWEBUI_OIDC_CLIENT_ID, OPENWEBUI_OIDC_ALLOWED_ROLES, OPENWEBUI_OIDC_ADMIN_ROLES, "
            "OPENWEBUI_IDP_EGRESS_CIDRS, ROUTE_DESTINATION_CA_FILE. Set them, or opt out with "
            "OPENWEBUI_ENABLED=false") in r.stderr


def test_openwebui_selection_round_trips_with_the_decided_defaults(monkeypatch, mapper_env, tmp_path):
    _owui_env(monkeypatch, tmp_path)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    values = load_values(out)
    # 30 days is the maintainer's 2026-10-24 decision; groups/roles are the
    # Open WebUI claim names; 443 the IdP port.
    assert values["openwebui"] == {
        "enabled": True, "secretName": "owui-secrets", "host": "docs.apps.example.internal",
        "retentionDays": 30, "storageSize": "10Gi",
        "oidc": {"providerUrl": OWUI_ON["OPENWEBUI_OIDC_PROVIDER_URL"], "clientId": "docs-frontend",
                 "groupClaim": "groups", "rolesClaim": "roles", "allowedRoles": "staff",
                 "adminRoles": "docs-admins"},
        "idpEgress": {"cidrs": ["10.20.0.0/24", "10.21.0.8/32"], "port": 443},
    }
    assert values["images"]["openwebui"] == {
        "repository": "reg.internal/qdrant-pdf-rag-openwebui", "tag": "v0.12.3-slim",
        "pullPolicy": "IfNotPresent"}
    # The namespace service CA is the destination CA while the console Route is off.
    assert values["route"] == {"enabled": False, "timeoutSeconds": 300, "destinationCA": OWUI_CA}
    digest = "sha256:" + "d" * 64
    monkeypatch.setenv("IMAGE_DIGEST_OPENWEBUI", digest)
    monkeypatch.setenv("OPENWEBUI_RETENTION_DAYS", "7")
    r, out = mapper_env()
    values = load_values(out)
    assert values["images"]["openwebui"]["digest"] == digest
    assert values["openwebui"]["retentionDays"] == 7


def test_openwebui_allow_list_proxy_image_is_rendered_with_its_verified_digest(monkeypatch, mapper_env, tmp_path):
    """ADR-0007 E1: the nginx sidecar's image follows the front end's selection and
    carries the registry digest deploy verified; it is absent when the front end is off."""
    _owui_env(monkeypatch, tmp_path)
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    assert load_values(out)["images"]["openwebuiProxy"] == {
        "repository": "reg.internal/nginxinc/nginx-unprivileged", "tag": "1.30.6-alpine",
        "pullPolicy": "IfNotPresent"}
    digest = "sha256:" + "f" * 64
    monkeypatch.setenv("IMAGE_DIGEST_OPENWEBUI_PROXY", digest)
    r, out = mapper_env()
    assert load_values(out)["images"]["openwebuiProxy"]["digest"] == digest
    monkeypatch.setenv("IMAGE_DIGEST_OPENWEBUI_PROXY", "not-a-digest")
    r, _ = mapper_env()
    assert r.returncode != 0 and "IMAGE_DIGEST_OPENWEBUI_PROXY must be a sha256 manifest digest" in r.stderr


@pytest.mark.parametrize("env,message", [
    ({"OPENWEBUI_ENABLED": "maybe"}, "OPENWEBUI_ENABLED must be true/false"),
    ({"OPENWEBUI_SECRET": ""}, "inputs are missing: OPENWEBUI_SECRET"),
    ({"OPENWEBUI_SECRET": "Bad_Name"}, "OPENWEBUI_SECRET must be"),
    ({"OPENWEBUI_HOST": ""}, "inputs are missing: OPENWEBUI_HOST"),
    ({"OPENWEBUI_HOST": "https://docs.example.internal"}, "OPENWEBUI_HOST must be"),
    ({"OPENWEBUI_OIDC_PROVIDER_URL": "http://idp.example/.well-known/openid-configuration"},
     "OPENWEBUI_OIDC_PROVIDER_URL must be"),
    ({"OPENWEBUI_OIDC_PROVIDER_URL": ""}, "inputs are missing: OPENWEBUI_OIDC_PROVIDER_URL"),
    ({"OPENWEBUI_OIDC_CLIENT_ID": ""}, "inputs are missing: OPENWEBUI_OIDC_CLIENT_ID"),
    ({"OPENWEBUI_OIDC_ADMIN_ROLES": ""}, "inputs are missing: OPENWEBUI_OIDC_ADMIN_ROLES"),
    ({"OPENWEBUI_OIDC_ALLOWED_ROLES": "a,,b"}, "OPENWEBUI_OIDC_ALLOWED_ROLES must be"),
    ({"OPENWEBUI_OIDC_GROUP_CLAIM": "group claim"}, "OPENWEBUI_OIDC_GROUP_CLAIM must be"),
    ({"OPENWEBUI_IDP_EGRESS_CIDRS": ""}, "inputs are missing: OPENWEBUI_IDP_EGRESS_CIDRS"),
    ({"OPENWEBUI_IDP_EGRESS_CIDRS": "idp.example.internal"}, "OPENWEBUI_IDP_EGRESS_CIDRS must be"),
    ({"OPENWEBUI_IDP_EGRESS_CIDRS": "10.0.0.300/32"}, "OPENWEBUI_IDP_EGRESS_CIDRS must be"),
    ({"OPENWEBUI_IDP_EGRESS_PORT": "0"}, "OPENWEBUI_IDP_EGRESS_PORT must be"),
    ({"OPENWEBUI_RETENTION_DAYS": "0"}, "OPENWEBUI_RETENTION_DAYS must be"),
    ({"OPENWEBUI_RETENTION_DAYS": "3651"}, "OPENWEBUI_RETENTION_DAYS must be"),
    ({"OPENWEBUI_STORAGE_SIZE": "10"}, "OPENWEBUI_STORAGE_SIZE must be"),
    ({"ROUTE_DESTINATION_CA_FILE": ""}, "inputs are missing: ROUTE_DESTINATION_CA_FILE"),
    # Shapes are checked even while the feature is off: a typo fails today.
    ({"OPENWEBUI_ENABLED": "false", "OPENWEBUI_RETENTION_DAYS": "forever"}, "OPENWEBUI_RETENTION_DAYS must be"),
])
def test_openwebui_selection_is_strict_and_writes_nothing(monkeypatch, mapper_env, tmp_path, env, message):
    _owui_env(monkeypatch, tmp_path, **env)
    r, out = mapper_env()
    assert r.returncode != 0
    assert message in r.stderr
    assert not out.exists()


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required for the round-trip")
def test_openwebui_mapper_to_helm_round_trip(monkeypatch, mapper_env, tmp_path):
    """Producer-to-consumer: operator env -> values -> helm template (a chart
    copy holding a stand-in Pipe) -> the pod, Route and policy asked for."""
    _owui_env(monkeypatch, tmp_path, OPENWEBUI_RETENTION_DAYS="14", OPENWEBUI_IDP_EGRESS_PORT="8443")
    r, out = mapper_env()
    assert r.returncode == 0, r.stderr
    chart = tmp_path / "mainframe-rag"
    shutil.copytree(CHART, chart)
    (chart / "files" / "openwebui" / "docs_pipe.py").write_text("class Pipe:\n    pass\n")
    rendered = subprocess.run(["helm", "template", "app", str(chart), "-f", str(out), "--namespace", "ns"],
                              capture_output=True, text=True, cwd=REPO, check=False)
    assert rendered.returncode == 0, rendered.stderr
    docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(rendered.stdout) if d}
    dep = docs["Deployment", "rag-openwebui"]
    containers = {c["name"]: c for c in dep["spec"]["template"]["spec"]["containers"]}
    assert containers["openwebui"]["image"] == "reg.internal/qdrant-pdf-rag-openwebui:v0.12.3-slim"
    assert containers["chat-purge"]["args"][:2] == ["--days", "14"]
    env = {e["name"]: e for e in containers["openwebui"]["env"]}
    assert env["OAUTH_CLIENT_ID"]["value"] == "docs-frontend"
    assert env["OPENID_REDIRECT_URI"]["value"] == "https://docs.apps.example.internal/oauth/oidc/callback"
    route = docs["Route", "rag-openwebui"]["spec"]
    assert route["host"] == "docs.apps.example.internal"
    assert route["tls"]["destinationCACertificate"].strip() == OWUI_CA.strip()
    egress = docs["NetworkPolicy", "rag-openwebui"]["spec"]["egress"][2]
    assert egress == {"to": [{"ipBlock": {"cidr": "10.20.0.0/24"}}, {"ipBlock": {"cidr": "10.21.0.8/32"}}],
                      "ports": [{"protocol": "TCP", "port": 8443}]}
    assert ("Route", "rag-agent") not in docs


def test_unset_efforts_render_medium_on_the_agent_pod(monkeypatch, mapper_env):
    """#917: with no operator effort, mapper values omit it and the chart default puts medium on the pod."""
    for name in ("LLM_REASONING_EFFORT_SIMPLE", "LLM_REASONING_EFFORT_COMPLEX", "LLM_REASONING_EFFORTS"):
        monkeypatch.setenv(name, "")
    result, output = mapper_env()
    assert result.returncode == 0, result.stderr
    reasoning = load_values(output)["models"]["reasoning"]
    assert "reasoningEffortSimple" not in reasoning and "reasoningEffortComplex" not in reasoning
    rendered = subprocess.run(
        [str(REPO / ".tools/bin/helm"), "template", "app", str(CHART), "-f", str(output),
         "--namespace", "ns", "--show-only", "templates/agent-deployment.yaml"],
        capture_output=True, text=True, check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    agent = yaml.safe_load(rendered.stdout)["spec"]["template"]["spec"]["containers"][0]
    actual = {entry["name"]: entry.get("value") for entry in agent["env"]}
    assert actual["LLM_REASONING_EFFORT_SIMPLE"] == actual["LLM_REASONING_EFFORT_COMPLEX"] == "medium"


def test_airgap_render_carries_the_request_deadline(mapper_env):
    """#843 D6: the mapped release values add no override, so the chart's 300 s reaches the pod."""
    result, output = mapper_env()
    assert result.returncode == 0, result.stderr
    assert "requestDeadlineSeconds" not in load_values(output)
    rendered = subprocess.run(
        [str(REPO / ".tools/bin/helm"), "template", "app", str(CHART), "-f", str(output),
         "--namespace", "ns", "--show-only", "templates/agent-deployment.yaml"],
        capture_output=True, text=True, check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    agent = yaml.safe_load(rendered.stdout)["spec"]["template"]["spec"]["containers"][0]
    actual = {entry["name"]: entry.get("value") for entry in agent["env"]}
    assert actual["REQUEST_DEADLINE_S"] == "270"
    assert yaml.safe_load(output.read_text())["route"]["timeoutSeconds"] > int(actual["REQUEST_DEADLINE_S"])


def _agent_env(output):
    rendered = subprocess.run(
        [str(REPO / ".tools/bin/helm"), "template", "app", str(CHART), "-f", str(output),
         "--namespace", "ns", "--show-only", "templates/agent-deployment.yaml"],
        capture_output=True, text=True, check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    agent = yaml.safe_load(rendered.stdout)["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry.get("value") for entry in agent["env"]}


def test_request_bound_inputs_unset_keep_chart_deadline_and_admission(mapper_env):
    """#374 handoff: no operator value means no override; the chart's 270 and 4/4/30 reach the pod (#843)."""
    result, output = mapper_env()
    assert result.returncode == 0, result.stderr
    values = load_values(output)
    assert not {"requestDeadlineSeconds", "requestMaxConcurrent", "requestQueueMax",
                "requestQueueWaitSeconds"} & values.keys()
    actual = _agent_env(output)
    assert actual["REQUEST_DEADLINE_S"] == "270"
    assert [actual[n] for n in ("REQUEST_MAX_CONCURRENT", "REQUEST_QUEUE_MAX", "REQUEST_QUEUE_WAIT_S")] == ["4", "4", "30"]


def test_request_bound_operator_values_reach_the_pod_and_settings(monkeypatch, mapper_env):
    from mainframe_rag.config import Settings

    chosen = {"REQUEST_DEADLINE_S": "120", "REQUEST_MAX_CONCURRENT": "4",
              "REQUEST_QUEUE_MAX": "8", "REQUEST_QUEUE_WAIT_S": "2.5"}
    for name, value in chosen.items():
        monkeypatch.setenv(name, value)
    result, output = mapper_env()
    assert result.returncode == 0, result.stderr
    actual = _agent_env(output)
    assert {name: actual[name] for name in chosen} == chosen
    for name in chosen:
        monkeypatch.setenv(name, actual[name])
    settings = Settings(_env_file=None)
    assert (settings.request_deadline_s, settings.request_max_concurrent,
            settings.request_queue_max, settings.request_queue_wait_s) == (120.0, 4, 8, 2.5)


def test_request_bound_explicit_zero_is_retained(monkeypatch, mapper_env):
    for name in ("REQUEST_DEADLINE_S", "REQUEST_MAX_CONCURRENT", "REQUEST_QUEUE_MAX", "REQUEST_QUEUE_WAIT_S"):
        monkeypatch.setenv(name, "0")
    result, output = mapper_env()
    assert result.returncode == 0, result.stderr
    actual = _agent_env(output)
    assert [actual[n] for n in ("REQUEST_DEADLINE_S", "REQUEST_MAX_CONCURRENT",
                                "REQUEST_QUEUE_MAX", "REQUEST_QUEUE_WAIT_S")] == ["0"] * 4


@pytest.mark.parametrize("name,raw", [
    ("REQUEST_DEADLINE_S", "-1"), ("REQUEST_DEADLINE_S", "3601"), ("REQUEST_DEADLINE_S", "1.5"),
    ("REQUEST_DEADLINE_S", "300"), ("REQUEST_DEADLINE_S", "900"), ("REQUEST_DEADLINE_S", "private-value"),
    ("REQUEST_MAX_CONCURRENT", "1001"), ("REQUEST_MAX_CONCURRENT", "two"),
    ("REQUEST_QUEUE_MAX", "-3"), ("REQUEST_QUEUE_WAIT_S", "301"), ("REQUEST_QUEUE_WAIT_S", "1e2"),
])
def test_request_bound_invalid_values_fail_without_echo(monkeypatch, mapper_env, name, raw):
    monkeypatch.setenv(name, raw)
    result, _ = mapper_env()
    assert result.returncode != 0
    assert name in result.stderr
    assert "private-value" not in result.stderr


def test_preflight_rejects_invalid_request_bound():
    """validate.sh / model_config.py shares the mapper's validator."""
    env = {**os.environ, "EMBED_MODEL": "m", "EMBED_BASE_URL": "http://e", "REQUEST_QUEUE_MAX": "x",
           "OPENWEBUI_ENABLED": "false"}
    result = subprocess.run([sys.executable, str(REPO / "scripts/airgap/model_config.py")],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1 and "REQUEST_QUEUE_MAX" in result.stderr
