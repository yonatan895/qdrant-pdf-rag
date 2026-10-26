"""First-party Helm render contracts: independent expected behavior.

The retired differential oracle is replaced by explicit expectations for
agent, OAuth/Route, OpenObserve, collector, ServiceMonitor and explicit ingest profiles.
Operator-to-render round trips also live in test_map_values and airgap suites.
"""
import hashlib
import json

import pytest
import yaml

from tests.helpers_helm import (
    CHART,
    FAKE_CA,
    IMAGE_SHA,
    REPO,
    _helm_template_with_values,
    base_values,
    container_map,
    env_map,
    ingest_env_map,
    run,
    run_new_job_only,
    run_new_template,
)

FEEDBACK_ON = {"feedback": {"enabled": True, "secretName": "fb-secret"}}


def test_base_contract_with_independent_controls():
    """Agent defaults, types, probes and read-only credentials."""
    new = run_new_template({})
    new_dep = new["Deployment", "rag-agent"]
    new_svc = new["Service", "rag-agent"]
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
    new_env = env_map(new_dep)
    expected_scalars = {
        "QDRANT_URL": "http://qdrant:6333",
        "EMBED_BASE_URL": "http://vllm:8000/v1",
        "EMBED_MODEL": "embed-model",
        "DENSE_DIM": "768",
        "EMBED_MODEL_REVISION": "rev-1",
        "LLM_BASE_URL": "",
        "LLM_MODEL_REASONING": "",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://openobserve:5080/api/default",
        "IMAGE_SHA": IMAGE_SHA,
        "OTEL_DEPLOYMENT_ENVIRONMENT": "",
        "METRICS_ENABLED": "false",
        "UI_ENABLED": "true",
        "CHAT_CONDENSE_ENABLED": "false",
        "RERANK_ENABLED": "false",
        "RERANK_BASE_URL": "",
        "RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
        "RERANK_ENDPOINT_ORDER": "score_first",
        "LLM_REASONING_EFFORT_SIMPLE": "medium",  # chart default, #917
        "LLM_REASONING_EFFORT_COMPLEX": "medium",
        "REQUEST_DEADLINE_S": "270",  # chart default, #843
        "REQUEST_MAX_CONCURRENT": "4",
        "REQUEST_QUEUE_MAX": "4",
        "REQUEST_QUEUE_WAIT_S": "30",
    }
    assert {key: env["value"] for key, env in new_env.items() if "value" in env} == expected_scalars
    assert set(new_env) == set(expected_scalars) | {"QDRANT_API_KEY", "OTEL_EXPORTER_OTLP_HEADERS"}
    assert new_env["QDRANT_URL"]["value"] == "http://qdrant:6333"
    assert new_env["QDRANT_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "key": "read-only-api-key",
        "name": "qdrant-apikey",
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
    raw = _helm_template_with_values(base_values())
    assert raw.returncode == 0, raw.stderr
    assert "helm.sh/hook" not in raw.stdout
    assert "kind: StatefulSet" not in raw.stdout
    # Ingest is operator-owned: the default release renders no Job.
    assert ("Job", "ingest") not in new
    assert "kind: Job" not in raw.stdout

def test_gateway_keys_contract():
    new = run_new_template({"gateway": {"apiKeySecretName": "gw-keys"}})
    new_env = env_map(new["Deployment", "rag-agent"])
    for name, key in (
        ("LLM_API_KEY", "llm-api-key"),
        ("EMBED_API_KEY", "embed-api-key"),
        ("RERANK_API_KEY", "rerank-api-key"),
    ):
        assert new_env[name]["valueFrom"]["secretKeyRef"] == {"key": key, "name": "gw-keys"}

def test_pull_secret_and_service_name_and_tracing_off():
    new = run_new_template(
        {
            "pullSecret": {"name": "ghcr-pull"},
            "tracing": {"enabled": False, "endpoint": "", "serviceName": "my-rag-prod"},
        }
    )
    new_dep = new["Deployment", "rag-agent"]
    assert new_dep["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    assert env_map(new_dep)["OTEL_SERVICE_NAME"]["value"] == "my-rag-prod"
    assert env_map(new_dep)["OTEL_EXPORTER_OTLP_ENDPOINT"].get("value") in (None, "")
    assert not [k for k in new if "openobserve" in k[1] or "otelcol" in k[1]]

def test_nondefault_namespace_registry_and_rerank():
    sha = "b" * 40
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
    assert new["Deployment", "rag-agent"]["metadata"]["namespace"] == "custom-ns"
    new_agent = container_map(new["Deployment", "rag-agent"])["agent"]
    assert new_agent["image"] == f"other.example/rr/qdrant-pdf-rag-agent:{sha}"
    expected = {
        "RERANK_ENABLED": "true",
        "RERANK_BASE_URL": "http://rerank:8002/v1",
        "RERANK_MODEL": "my-reranker",
        "RERANK_ENDPOINT_ORDER": "rerank_first",
    }
    for key, value in expected.items():
        assert env_map(new["Deployment", "rag-agent"])[key]["value"] == value

def test_route_oauth_contract():
    """OAuth sidecar, ServiceAccount, reencrypt Route and protected ports."""
    new = run_new_template(
        {
            "route": {"enabled": True, "timeoutSeconds": 300, "destinationCA": FAKE_CA},
            "images": {
                "oauthProxy": {
                    "repository": "reg.internal/openshift4/ose-oauth-proxy",
                    "tag": "v4.14",
                }
            },
        }
    )
    assert ("ServiceAccount", "rag-agent") in new
    new_sa = new["ServiceAccount", "rag-agent"]

    redirect = json.loads(
        new_sa["metadata"]["annotations"][
            "serviceaccounts.openshift.io/oauth-redirectreference.primary"
        ]
    )
    assert redirect == {
        "kind": "OAuthRedirectReference",
        "apiVersion": "v1",
        "reference": {"kind": "Route", "name": "rag-agent"},
    }
    for docs in (new,):
        svc = docs["Service", "rag-agent"]
        assert svc["metadata"]["annotations"] == {
            "service.beta.openshift.io/serving-cert-secret-name": "rag-agent-tls"
        }
        ports = {(p["name"], p["port"], p["targetPort"]) for p in svc["spec"]["ports"]}
        assert ports == {("oauth", 8443, "oauth"), ("http", 8080, "http")}
    for docs in (new,):
        dep = docs["Deployment", "rag-agent"]
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
            "--skip-auth-regex=^/livez$",
        ]
        assert oauth["ports"] == [{"name": "oauth", "containerPort": 8443}]
        assert oauth["resources"] == {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "200m", "memory": "128Mi"},
        }
        vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
        assert vols["oauth-tls"] == {"name": "oauth-tls", "secret": {"secretName": "rag-agent-tls"}}
        assert vols["oauth-cookie"] == {
            "name": "oauth-cookie",
            "secret": {"secretName": "rag-agent-oauth-cookie"},
        }
    route = new["Route", "rag-agent"]
    assert route["apiVersion"] == "route.openshift.io/v1"
    assert route["metadata"]["namespace"] == "ns"
    assert route["metadata"]["annotations"] == {"haproxy.router.openshift.io/timeout": "300s"}
    assert route["spec"]["to"] == {"kind": "Service", "name": "rag-agent"}
    assert route["spec"]["port"] == {"targetPort": "oauth"}
    assert route["spec"]["tls"]["termination"] == "reencrypt"
    assert route["spec"]["tls"]["insecureEdgeTerminationPolicy"] == "Redirect"
    assert "FAKE-CA-BUNDLE" in route["spec"]["tls"]["destinationCACertificate"]
    off = run_new_template({})
    assert ("ServiceAccount", "rag-agent") not in off
    assert ("Route", "rag-agent") not in off
    assert "serviceAccountName" not in off["Deployment", "rag-agent"]["spec"]["template"]["spec"]

def test_oauth_skip_regex_exempts_only_the_work_free_liveness_path():
    """#1018: the unauthenticated OAuth bypass must never reach /healthz, which
    performs Qdrant, embedding and serving-gate work per request. Kubelet probes
    hit the pod port directly, so they never needed the bypass."""
    import re

    docs = run_new_template({"route": {"enabled": True, "destinationCA": FAKE_CA}})
    oauth = container_map(docs["Deployment", "rag-agent"])["oauth-proxy"]
    patterns = [a.split("=", 1)[1] for a in oauth["args"] if a.startswith("--skip-auth-regex=")]
    assert patterns, "livez bypass pinned"
    for path in ("/healthz", "/healthz/", "/healthzx", "/ui", "/v1/search", "/metrics"):
        assert not any(re.search(p, path) for p in patterns), path
    assert any(re.search(p, "/livez") for p in patterns)
    # The kubelet probes target the container port, not the oauth sidecar.
    agent = container_map(docs["Deployment", "rag-agent"])["agent"]
    assert agent["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}


def test_servicemonitor_contract():
    new = run_new_template({"metrics": {"enabled": True}})
    key = ("ServiceMonitor", "rag-agent")
    assert key in new
    assert new[key]["spec"]["selector"] == {"matchLabels": {"app": "rag-agent"}}
    assert new[key]["spec"]["endpoints"] == [
        {"port": "http", "path": "/metrics", "interval": "30s", "scrapeTimeout": "10s"}
    ]
    assert env_map(new["Deployment", "rag-agent"])["METRICS_ENABLED"]["value"] == "true"
    off = run_new_template({})
    assert ("ServiceMonitor", "rag-agent") not in off

def test_ingest_job_contract_normal():
    new = run_new_job_only({"ingest": {"corpusPVC": "corpus-pvc"}})
    new_job = new["Job", "ingest"]
    new_c = next(
        c for c in new_job["spec"]["template"]["spec"]["containers"] if c["name"] == "ingest"
    )
    assert new_c["image"] == f"reg.internal/qdrant-pdf-rag-ingest:{IMAGE_SHA}"
    assert new_c["args"] == ["--src", "/corpus", "--progress", "/work/inventory.jsonl"]
    assert new_c["resources"] == {
        "requests": {"cpu": "4", "memory": "8Gi"},
        "limits": {"cpu": "16", "memory": "32Gi"},
    }
    assert new_c["volumeMounts"] == [
        {"name": "tmp", "mountPath": "/tmp"},
        {"name": "corpus", "mountPath": "/corpus", "readOnly": True},
        {"name": "work", "mountPath": "/work"},
    ]
    new_env = ingest_env_map(new_job)
    expected_scalars = {
        "QDRANT_URL": "http://qdrant:6333",
        "QDRANT_COLLECTION": "mainframe_manuals",
        "QDRANT_SHARD_NUMBER": "6",
        "QDRANT_REPLICATION_FACTOR": "3",
        "QDRANT_WRITE_CONSISTENCY_FACTOR": "2",
        "EMBED_BASE_URL": "http://vllm:8000/v1",
        "EMBED_MODEL": "embed-model",
        "DENSE_DIM": "768",
        "EMBED_MODEL_REVISION": "rev-1",
        "IMAGE_SHA": IMAGE_SHA,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://openobserve:5080/api/default",
        "OTEL_SERVICE_NAME": "mainframe-rag-ingest",
        "OTEL_DEPLOYMENT_ENVIRONMENT": "",
        "INGEST_WORKERS": "4",
        "INGEST_PARSE_TMP_DIR": "/work/parse-tmp",
        "CONTEXTUAL_EMBED_ENABLED": "false",
        "CONTEXT_LLM_BASE_URL": "",
        "CONTEXT_LLM_MODEL": "",
    }
    assert {key: env["value"] for key, env in new_env.items() if "value" in env} == expected_scalars
    assert set(new_env) == set(expected_scalars) | {"QDRANT_API_KEY", "OTEL_EXPORTER_OTLP_HEADERS"}
    assert new_env["QDRANT_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "qdrant-apikey",
        "key": "api-key",
    }
    assert new_env["QDRANT_COLLECTION"]["value"] == "mainframe_manuals"
    assert new_env["QDRANT_SHARD_NUMBER"]["value"] == "6"
    # In-place ingest was retired (#360): the Job never renders the switch.
    assert "INGEST_ALIAS_PUBLISH" not in new_env
    assert new_env["INGEST_WORKERS"]["value"] == "4"
    assert new_env["OTEL_SERVICE_NAME"]["value"] == "mainframe-rag-ingest"
    assert "EMBED_API_KEY" not in new_env
    vols = {v["name"]: v for v in new_job["spec"]["template"]["spec"]["volumes"]}
    assert vols["corpus"] == {
        "name": "corpus",
        "persistentVolumeClaim": {"claimName": "corpus-pvc", "readOnly": True},
    }
    assert vols["work"] == {"name": "work", "persistentVolumeClaim": {"claimName": "ingest-work"}}


def test_ingest_job_private_parse_copies_resolve_under_the_work_mount():
    """#1058: the per-document private copy is made under INGEST_PARSE_TMP_DIR;
    that must sit on the sized `ingest-work` PVC mount, never on the node
    ephemeral `/tmp` emptyDir or the read-only corpus mount."""
    from pathlib import PurePosixPath

    job = run_new_job_only({"ingest": {"corpusPVC": "corpus-pvc"}})["Job", "ingest"]
    pod = job["spec"]["template"]["spec"]
    container = next(c for c in pod["containers"] if c["name"] == "ingest")
    target = PurePosixPath(ingest_env_map(job)["INGEST_PARSE_TMP_DIR"]["value"])
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    owner = max(
        (PurePosixPath(p) for p in mounts if target.is_relative_to(PurePosixPath(p))),
        key=lambda p: len(p.parts),
    )
    mount = mounts[str(owner)]
    assert target.is_absolute() and target != owner  # a subdirectory, not the mount root
    assert mount["name"] == "work" and not mount.get("readOnly")
    volume = next(v for v in pod["volumes"] if v["name"] == "work")
    assert volume["persistentVolumeClaim"]["claimName"] == "ingest-work"


def test_ingest_job_contract_maintenance_with_tricky_revision():
    tricky = "SA22-7777-01@vendor|product with spaces|v1|abc123 & more"
    new = run_new_job_only(
        {
            "ingest": {
                "corpusPVC": "corpus-pvc",
                "reingest": True,
                "retireDocs": ["SA22-0000-00", tricky],
            },
            "gateway": {"apiKeySecretName": "gw-keys", "caConfigMapName": "gw-ca"},
            "pullSecret": {"name": "ghcr-pull"},
        }
    )
    new_args = next(
        c
        for c in new["Job", "ingest"]["spec"]["template"]["spec"]["containers"]
        if c["name"] == "ingest"
    )["args"]
    expected = [
        "--src",
        "/corpus",
        "--progress",
        "/work/inventory.jsonl",
        "--reingest",
        "--retire-doc",
        "SA22-0000-00",
        "--retire-doc",
        tricky,
    ]
    assert new_args == expected
    for docs in (new,):
        env = ingest_env_map(docs["Job", "ingest"])
        assert "INGEST_ALIAS_PUBLISH" not in env
        assert env["EMBED_API_KEY"]["valueFrom"]["secretKeyRef"] == {
            "name": "gw-keys",
            "key": "embed-api-key",
        }
        assert env["CONTEXT_LLM_API_KEY"]["valueFrom"]["secretKeyRef"] == {
            "name": "gw-keys",
            "key": "context-llm-api-key",
        }
        assert "LLM_API_KEY" not in env and "RERANK_API_KEY" not in env
        assert env["SSL_CERT_FILE"]["value"] == "/etc/gateway-ca/ca-bundle.crt"

def test_rerank_enabled_without_base_url_falls_back():
    """Empty RERANK_BASE_URL with rerank on is valid: HttpReranker falls back
    to the embedding URL (retrieve/rerank.py). The chart must preserve the
    bare render, not reject it."""
    new = run_new_template(
        {"models": {"rerank": {"enabled": True, "baseUrl": "", "model": "my-reranker"}}}
    )
    for docs in (new,):
        env = env_map(docs["Deployment", "rag-agent"])
        assert env["RERANK_ENABLED"]["value"] == "true"
        assert env["RERANK_BASE_URL"].get("value") in (None, "")

@pytest.mark.parametrize("authorize", [False, True])
def test_ingest_job_renders_authorize_from_catalog_only_when_set(authorize):
    """Issue #1005 (option B): `ingest.authorizeFromCatalog` renders
    `--authorize-from-catalog` after the retirements; the default renders
    no flag."""
    ingest = {"corpusPVC": "corpus-pvc", "retireDocs": ["SA22-0000-00@ibm|zos|2.5|abc"]}
    if authorize:
        ingest["authorizeFromCatalog"] = True
    new = run_new_job_only(
        {
            "ingest": ingest,
            "gateway": {"apiKeySecretName": "gw-keys", "caConfigMapName": "gw-ca"},
            "pullSecret": {"name": "ghcr-pull"},
        }
    )
    args = next(
        c
        for c in new["Job", "ingest"]["spec"]["template"]["spec"]["containers"]
        if c["name"] == "ingest"
    )["args"]
    tail = ["--retire-doc", "SA22-0000-00@ibm|zos|2.5|abc"]
    assert args[-3:] == [*tail, "--authorize-from-catalog"] if authorize else args[-2:] == tail
    assert ("--authorize-from-catalog" in args) is authorize


def test_authorize_from_catalog_without_retire_docs_is_refused_at_render():
    """Issue #1005 (option B): the flag alone is refused by the chart (schema
    and template), so a hand-written values file cannot render it either."""
    values = base_values()
    values["ingest"].update({"enabled": True, "corpusPVC": "c", "authorizeFromCatalog": True})
    r = _helm_template_with_values(values)
    assert r.returncode != 0
    assert "authorizeFromCatalog" in r.stderr or "retireDocs" in r.stderr


def test_schema_rejects_invalid_selected_configuration():
    cases = [
        ({"models": {"embedding": {"dimension": 0}}}, "dimension"),
        ({"models": {"embedding": {"revision": "   "}}}, "revision"),
        ({"images": {"agent": {"tag": "HEAD"}}}, "tag"),
        ({"images": {"ingest": {"tag": "latest"}}}, "ingest"),
        ({"images": {"agent": {"digest": "sha256:abc"}}}, "digest"),
        ({"images": {"openobserve": {"digest": "v1.1.2"}}}, "digest"),
        ({"gateway": {"apiKeySecretName": "Bad_Name!"}}, "apiKeySecretName"),
        ({"models": {"rerank": {"endpointOrder": "nope"}}}, "endpointOrder"),
        ({"metrics": {"enabled": "false"}}, "metrics"),
        ({"ui": {"enabled": "false"}}, "ui"),
        ({"models": {"reasoning": {"model": "foo", "baseUrl": ""}}}, "reasoning"),
        ({"route": {"enabled": True, "destinationCA": ""}}, "destinationCA"),
        ({"route": {"enabled": True, "timeoutSeconds": 0}}, "timeoutSeconds"),
        ({"storage": {"className": "nfs-client"}}, "storage"),
        ({"ingest": {"enabled": True, "corpusPVC": ""}}, "corpusPVC"),
        # The retired in-place switch (#360) is refused, not silently ignored.
        ({"ingest": {"aliasPublish": False}}, "aliasPublish"),
        ({"ingest": {"retireDocs": ['bad"quote']}}, "retireDocs"),
        ({"ingest": {"workers": 0}}, "workers"),
        ({"ingest": {"collectionPolicy": {"shardNumber": 0}}}, "shardNumber"),
        ({"tracing": {"endpoint": "oo:4318"}}, "endpoint"),
        ({"tracing": {"backend": "jaeger"}}, "backend"),
        ({"tracing": {"jaeger": {"enabled": False}}}, "jaeger"),
        ({"images": {"jaeger": {"repository": "x", "tag": "v1"}}}, "jaeger"),
        ({"metrics": {"collector": {"enabled": "false"}}}, "enabled"),
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
    text = "".join((CHART / "templates" / n).read_text() for n in names)
    # The only StatefulSet the chart owns is the feedback service (#804);
    # Qdrant's is the vendored chart's.
    stateful = [n for n in names if "kind: StatefulSet" in (CHART / "templates" / n).read_text()]
    assert stateful == ["feedback-statefulset.yaml"]
    assert "helm.sh/hook" not in text
    assert "qdrant" in text.lower()
    schema_text = (CHART / "values.schema.json").read_text()
    assert "read-only-api-key" not in schema_text
    values_text = (CHART / "values.yaml").read_text()
    assert "sk-" not in values_text
    assert (
        "registry.example" not in text
        or "registry.example.internal" in (CHART / "values.yaml").read_text()
    )
    assert "ghcr.io" not in text

def test_numeric_looking_release_sha_remains_an_exact_env_string():
    sha = "0" * 40
    images = {"agent": {"tag": sha}, "ingest": {"tag": sha}}
    agent = run_new_template({"images": images})["Deployment", "rag-agent"]
    job = run_new_job_only({"images": images, "ingest": {"corpusPVC": "corpus"}})["Job", "ingest"]
    assert env_map(agent)["IMAGE_SHA"]["value"] == sha
    assert env_map(job, "ingest")["IMAGE_SHA"]["value"] == sha

def test_chart_preserves_positive_operator_dimension_and_worker_range():
    rendered = run_new_template(
        {
            "models": {"embedding": {"dimension": 8192}},
            "ingest": {"enabled": True, "corpusPVC": "corpus", "workers": 64},
        }
    )
    agent_env = env_map(rendered["Deployment", "rag-agent"])
    assert agent_env["DENSE_DIM"]["value"] == "8192"
    assert ingest_env_map(rendered["Job", "ingest"])["INGEST_WORKERS"]["value"] == "64"


@pytest.mark.parametrize("field,value,environment", [
    ("maxModelLen", 131072, "LLM_MAX_MODEL_LEN"),
    ("thinkingReserveTokensSimple", 0, "LLM_THINKING_RESERVE_TOKENS_SIMPLE"),
    ("reasoningEfforts", "low,medium,xhigh", "LLM_REASONING_EFFORTS"),
    ("stream", False, "LLM_STREAM"),
])
def test_direct_chart_optional_budget_limits_reach_pod(field, value, environment):
    rendered = run_new_template({"models": {"reasoning": {field: value}}})
    pod = env_map(rendered["Deployment", "rag-agent"])
    expected = "false" if value is False else str(value)
    assert pod[environment]["value"] == expected


@pytest.mark.parametrize("field,value", [
    ("maxModelLen", 511), ("maxModelLen", "16384"),
    ("reservedOutputTokens", 127),
    ("thinkingReserveTokensComplex", 16385),
    ("reasoningEffortSimple", "unsupported"),
    ("reasoningEfforts", "low,maximal"), ("reasoningEfforts", "low, medium"), ("reasoningEfforts", ""),
    ("stream", "true"),
])
def test_direct_chart_refuses_invalid_model_budget(field, value):
    values = base_values()
    values["models"]["reasoning"][field] = value
    result = _helm_template_with_values(values)
    assert result.returncode != 0
    assert field in result.stderr


@pytest.mark.parametrize("route_enabled", [False, True])
def test_ui_off_by_route_matrix(route_enabled):
    """UI selection is independent of the Route: an API-only install keeps
    Route/OAuth/TLS wiring exactly as selected, and Route-off is not UI-off."""
    extra = {"ui": {"enabled": False}}
    if route_enabled:
        extra["route"] = {"enabled": True, "timeoutSeconds": 300, "destinationCA": FAKE_CA}
        extra["images"] = {
            "oauthProxy": {
                "repository": "reg.internal/openshift4/ose-oauth-proxy",
                "tag": "v4.14",
            }
        }
    new = run_new_template(extra)
    assert env_map(new["Deployment", "rag-agent"])["UI_ENABLED"]["value"] == "false"
    assert (("Route", "rag-agent") in new) == route_enabled
    assert (("ServiceAccount", "rag-agent") in new) == route_enabled
    if route_enabled:
        containers = container_map(new["Deployment", "rag-agent"])
        assert "oauth-proxy" in containers


def test_direct_peers_are_typed_and_ingest_only():
    peers = " http://peer-0:6333,\n\thttp://peer-1:6333 "
    values = base_values()
    values["ingest"].update(enabled=True, corpusPVC="manuals", peerUrls=peers)
    rendered = _helm_template_with_values(values)
    assert rendered.returncode == 0, rendered.stderr
    objects = {
        (obj["kind"], obj["metadata"]["name"]): obj
        for obj in yaml.safe_load_all(rendered.stdout) if obj
    }
    assert ingest_env_map(objects["Job", "ingest"])["QDRANT_PEER_URLS"]["value"] == peers
    assert "QDRANT_PEER_URLS" not in env_map(objects["Deployment", "rag-agent"])
    values["ingest"]["peerUrls"] = ["http://peer-0:6333"]
    rejected = _helm_template_with_values(values)
    assert rejected.returncode != 0
    assert "peerUrls" in rejected.stderr


def test_servicemonitor_decoupled_from_exposition():
    """OBS-2 item 3: exposition without the monitor (platform-UWM modes).
    METRICS_ENABLED stays true while the object disappears."""
    new = run_new_template({"metrics": {"enabled": True, "serviceMonitor": {"enabled": False}}})
    assert ("ServiceMonitor", "rag-agent") not in new
    assert env_map(new["Deployment", "rag-agent"])["METRICS_ENABLED"]["value"] == "true"


def test_servicemonitor_enabled_with_metrics_off_fails():
    """OBS-2 item 3: monitoring a disabled endpoint is rejected."""
    values = base_values()
    values["metrics"]["serviceMonitor"] = {"enabled": True}
    raw = _helm_template_with_values(values)
    assert raw.returncode != 0
    assert "metrics.serviceMonitor.enabled=true requires metrics.enabled=true" in raw.stderr


def _hardening_violations(pod_spec: dict) -> list[str]:
    """Pod/container hardening that must not depend on restricted-v2 alone (#585).

    Fixed identities stay forbidden: OpenShift assigns UID/GID/fsGroup.
    """
    problems = []
    pod_sc = pod_spec.get("securityContext") or {}
    if pod_sc.get("runAsNonRoot") is not True:
        problems.append("pod runAsNonRoot")
    if (pod_sc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
        problems.append("pod seccompProfile")
    for key in ("runAsUser", "runAsGroup", "fsGroup"):
        if key in pod_sc:
            problems.append(f"pod {key} set")
    for c in pod_spec.get("initContainers", []) + pod_spec["containers"]:
        sc = c.get("securityContext") or {}
        if sc.get("allowPrivilegeEscalation") is not False:
            problems.append(f"{c['name']} allowPrivilegeEscalation")
        if (sc.get("capabilities") or {}).get("drop") != ["ALL"]:
            problems.append(f"{c['name']} capabilities.drop")
        for key in ("runAsUser", "runAsGroup"):
            if key in sc:
                problems.append(f"{c['name']} {key} set")
    return problems


def test_every_rendered_pod_is_hardened_without_fixed_identities():
    docs = run_new_template(
        {
            "route": {"enabled": True, "destinationCA": FAKE_CA},
            "ingest": {"enabled": True, "corpusPVC": "corpus-pvc"},
            **FEEDBACK_ON,
        }
    )
    # Every workload kind that carries a PodSpec, so a new kind cannot escape.
    pods = {
        key: doc["spec"]["template"]["spec"]
        for key, doc in docs.items()
        if key[0] in ("Deployment", "StatefulSet", "Job", "DaemonSet", "CronJob")
    }
    assert set(pods) == {
        ("Deployment", "rag-agent"),
        ("Deployment", "openobserve"),
        ("StatefulSet", "rag-feedback"),
        ("Job", "ingest"),
    }
    assert pods["StatefulSet", "rag-feedback"]["automountServiceAccountToken"] is False
    assert {c["name"] for c in pods["Deployment", "rag-agent"]["containers"]} == {
        "agent",
        "oauth-proxy",
    }
    for key, pod in pods.items():
        assert _hardening_violations(pod) == [], key


@pytest.mark.parametrize("route", [False, True])
def test_no_workload_automounts_a_service_account_token(route):
    """#1029: pods with no Kubernetes API use mount no token; with the Route on,
    only the oauth-proxy container gets a projected token."""
    extra = {
        "ingest": {"enabled": True, "corpusPVC": "corpus-pvc"},
        "metrics": {"enabled": True},
        **FEEDBACK_ON,
    }
    if route:
        extra["route"] = {"enabled": True, "destinationCA": FAKE_CA}
    docs = run_new_template(extra)
    pods = {
        key: doc["spec"]["template"]["spec"]
        for key, doc in docs.items()
        if key[0] in ("Deployment", "StatefulSet", "Job", "DaemonSet", "CronJob")
    }
    assert {k[1] for k in pods} >= {"rag-agent", "openobserve", "otelcol", "ingest", "rag-feedback"}
    for key, pod in pods.items():
        assert pod.get("automountServiceAccountToken") is False, key
    agent = pods["Deployment", "rag-agent"]
    sa_path = "/var/run/secrets/kubernetes.io/serviceaccount"
    mounting = {
        c["name"]
        for pod in pods.values()
        for c in pod["containers"] + pod.get("initContainers", [])
        for m in c.get("volumeMounts", [])
        if m["mountPath"] == sa_path
    }
    assert mounting == ({"oauth-proxy"} if route else set())
    if route:
        vol = next(v for v in agent["volumes"] if v["name"] == "oauth-sa-token")
        # The oauth-proxy's full projection: bearer token, kube root CA,
        # optional service CA, and the pod namespace. Dropping any item
        # (notably ca.crt or namespace) breaks its in-cluster auth.
        assert vol["projected"]["sources"] == [
            {"serviceAccountToken": {"path": "token", "expirationSeconds": 3607}},
            {"configMap": {
                "name": "kube-root-ca.crt",
                "items": [{"key": "ca.crt", "path": "ca.crt"}],
            }},
            {"configMap": {
                "name": "openshift-service-ca.crt",
                "optional": True,
                "items": [{"key": "service-ca.crt", "path": "service-ca.crt"}],
            }},
            {"downwardAPI": {
                "items": [{
                    "path": "namespace",
                    "fieldRef": {"fieldPath": "metadata.namespace"},
                }],
            }},
        ]


def _assert_app_read_only_paths(pod, container_name, writable_paths):
    container = next(c for c in pod["containers"] if c["name"] == container_name)
    assert container["securityContext"].get("readOnlyRootFilesystem") is True, (
        f"{container_name} readOnlyRootFilesystem"
    )
    mounts = {mount["mountPath"]: mount for mount in container.get("volumeMounts", [])}
    actual_writable = {path for path, mount in mounts.items() if not mount.get("readOnly", False)}
    assert actual_writable == writable_paths, f"{container_name} writable paths"
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes[mounts["/tmp"]["name"]] == {"name": mounts["/tmp"]["name"], "emptyDir": {}}
    if container_name == "feedback":
        assert volumes[mounts["/data"]["name"]]["persistentVolumeClaim"] == {
            "claimName": "rag-feedback-data"}
    if container_name == "ingest":
        assert mounts["/corpus"]["readOnly"] is True
        assert volumes[mounts["/work"]["name"]]["persistentVolumeClaim"] == {"claimName": "ingest-work"}


def test_app_read_only_roots_keep_only_declared_writable_paths():
    """#585: baked source/weights stay read-only; temporary/work writes have mounts.

    This checks the chart boundary. Actual-image and lifecycle execution remain
    separate evidence for whether the declared paths cover runtime writes.
    """
    extras = {"route": {"enabled": True, "destinationCA": FAKE_CA},
              "gateway": {"caConfigMapName": "model-ca"}, **FEEDBACK_ON}
    docs = run_new_template({**extras, "ingest": {"enabled": True, "corpusPVC": "corpus-pvc"}})
    for key, container_name, writable_paths in (
        (("Deployment", "rag-agent"), "agent", {"/tmp"}),
        (("Job", "ingest"), "ingest", {"/tmp", "/work"}),
        (("StatefulSet", "rag-feedback"), "feedback", {"/tmp", "/data"}),
    ):
        pod = docs[key]["spec"]["template"]["spec"]
        _assert_app_read_only_paths(pod, container_name, writable_paths)


def test_verified_digest_renders_images_by_digest_keeping_tag_env():
    """Issue #272: a digest makes the pod image repository@digest; the SHA tag
    stays only the informational release identity (GIT_SHA-style env)."""
    digests = {k: "sha256:" + c * 64 for k, c in (("agent", "a"), ("openobserve", "b"), ("oauthProxy", "c"))}
    extra = {"images": {k: {"digest": v} for k, v in digests.items()}, "route": {"enabled": True, "destinationCA": FAKE_CA}}
    docs = run_new_template(extra)
    text = json.dumps(list(docs.values()), default=str)
    assert f"reg.internal/qdrant-pdf-rag-agent@{digests['agent']}" in text
    assert f"reg.internal/openobserve/openobserve@{digests['openobserve']}" in text
    assert f"reg.internal/openshift4/ose-oauth-proxy@{digests['oauthProxy']}" in text
    assert f"qdrant-pdf-rag-agent:{IMAGE_SHA}" not in text
    assert env_map(docs[("Deployment", "rag-agent")])["IMAGE_SHA"]["value"] == IMAGE_SHA  # informational tag stays


# ------------------------------------------------- agent NetworkPolicy (#373)

POLICY_KEY = ("NetworkPolicy", "rag-agent-ingress")
ROUTER_LABEL = {"network.openshift.io/policy-group": "ingress"}
MONITORING_LABEL = {"network.openshift.io/policy-group": "monitoring"}
UWM_NAME = {"kubernetes.io/metadata.name": "openshift-user-workload-monitoring"}


def _route_values(**extra):
    return {"route": {"enabled": True, "destinationCA": FAKE_CA}, **extra}


def _allows(policy, source_ns, source_ns_labels, port, release_ns="ns"):
    """Independent re-implementation of NetworkPolicy ingress semantics for one
    pod of `source_ns` reaching an agent pod on TCP `port` (peers within one
    rule are OR-ed; a rule's ports must also match). It checks the rendered
    object's meaning; enforcement is the CNI's and stays site evidence."""
    for rule in policy["spec"]["ingress"]:
        if port not in {p["port"] for p in rule["ports"] if p["protocol"] == "TCP"}:
            continue
        for peer in rule["from"]:
            if peer == {"podSelector": {}}:
                if source_ns == release_ns:
                    return True
            elif set(peer) == {"namespaceSelector"}:
                match = peer["namespaceSelector"]["matchLabels"]
                if all(source_ns_labels.get(k) == v for k, v in match.items()):
                    return True
            else:
                raise AssertionError(f"unexpected peer shape: {peer}")
    return False


def _ns(name, **labels):
    return name, {"kubernetes.io/metadata.name": name, **labels}


def test_networkpolicy_default_on_is_agent_only_ingress_only():
    docs = run_new_template({"_namespace": "rag-ns"})
    policy = docs[POLICY_KEY]
    assert policy["apiVersion"] == "networking.k8s.io/v1"
    assert policy["metadata"]["namespace"] == "rag-ns"
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "rag-agent"}}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    # Route off, metrics off: only same-namespace traffic to the HTTP port.
    assert policy["spec"]["ingress"] == [
        {"from": [{"podSelector": {}}], "ports": [{"protocol": "TCP", "port": 8080}]}
    ]
    assert [k for k in docs if k[0] == "NetworkPolicy"] == [POLICY_KEY, ("NetworkPolicy", "openobserve-egress-deny")]
    # The selector is the Deployment's pod label, so Qdrant/OpenObserve/ingest are not selected.
    labels = docs["Deployment", "rag-agent"]["spec"]["template"]["metadata"]["labels"]
    assert labels == policy["spec"]["podSelector"]["matchLabels"]
    oo = docs["Deployment", "openobserve"]["spec"]["template"]["metadata"]["labels"]
    assert not set(policy["spec"]["podSelector"]["matchLabels"].items()) <= set(oo.items())


def test_networkpolicy_route_adds_router_to_oauth_port_only():
    policy = run_new_template(_route_values())[POLICY_KEY]
    assert policy["spec"]["ingress"] == [
        {"from": [{"podSelector": {}}],
         "ports": [{"protocol": "TCP", "port": 8080}, {"protocol": "TCP", "port": 8443}]},
        {"from": [{"namespaceSelector": {"matchLabels": ROUTER_LABEL}}],
         "ports": [{"protocol": "TCP", "port": 8443}]},
    ]


def test_networkpolicy_metrics_admits_monitoring_by_label_or_uwm_name():
    policy = run_new_template({"metrics": {"enabled": True}})[POLICY_KEY]
    assert policy["spec"]["ingress"][1:] == [
        {"from": [{"namespaceSelector": {"matchLabels": MONITORING_LABEL}},
                  {"namespaceSelector": {"matchLabels": UWM_NAME}}],
         "ports": [{"protocol": "TCP", "port": 8080}]},
    ]
    # Keyed on exposition, not on the ServiceMonitor: a platform-owned scrape
    # (SERVICEMONITOR_ENABLED=false) still needs the port.
    scraped = run_new_template({"metrics": {"enabled": True, "serviceMonitor": {"enabled": False}}})
    assert ("ServiceMonitor", "rag-agent") not in scraped
    assert scraped[POLICY_KEY]["spec"]["ingress"] == policy["spec"]["ingress"]
    assert len(run_new_template({})[POLICY_KEY]["spec"]["ingress"]) == 1


def test_networkpolicy_allowed_namespaces_reach_http_only():
    names = ["mcp", "tools-1"]
    policy = run_new_template(
        _route_values(networkPolicy={"allowedNamespaces": names}))[POLICY_KEY]
    assert policy["spec"]["ingress"][-1] == {
        "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": n}}}
                 for n in names],
        "ports": [{"protocol": "TCP", "port": 8080}],
    }


def test_networkpolicy_semantics_for_the_documented_counterexamples():
    policy = run_new_template(_route_values(
        metrics={"enabled": True}, networkPolicy={"allowedNamespaces": ["mcp"]}))[POLICY_KEY]
    router = _ns("openshift-ingress", **ROUTER_LABEL)
    monitoring = _ns("openshift-monitoring", **MONITORING_LABEL)
    uwm = _ns("openshift-user-workload-monitoring")  # admitted by name, no label needed
    mcp = _ns("mcp")
    stranger = _ns("someone-else")
    # The router reaches the OAuth port and nothing else.
    assert _allows(policy, *router, 8443)
    assert not _allows(policy, *router, 8080)
    # Monitoring scrapes /metrics on 8080 only.
    for source in (monitoring, uwm):
        assert _allows(policy, *source, 8080)
        assert not _allows(policy, *source, 8443)
    # Same-namespace consumers keep both ports.
    assert _allows(policy, "ns", {"kubernetes.io/metadata.name": "ns"}, 8080)
    assert _allows(policy, "ns", {"kubernetes.io/metadata.name": "ns"}, 8443)
    # An allowed namespace reaches the HTTP port only.
    assert _allows(policy, *mcp, 8080)
    assert not _allows(policy, *mcp, 8443)
    # Any other namespace is denied on both ports.
    assert not _allows(policy, *stranger, 8080)
    assert not _allows(policy, *stranger, 8443)
    # Only the exact policy-group value counts.
    assert not _allows(policy, *_ns("x", **{"network.openshift.io/policy-group": "other"}), 8443)


def test_networkpolicy_disabled_renders_nothing_and_leaves_the_rest_unchanged():
    on = run_new_template(_route_values(metrics={"enabled": True}))
    off = run_new_template(_route_values(
        metrics={"enabled": True}, networkPolicy={"enabled": False}))
    assert POLICY_KEY in on and POLICY_KEY not in off
    assert {k: v for k, v in on.items() if k != POLICY_KEY} == off


@pytest.mark.parametrize("mutate", [
    {"networkPolicy": {"enabled": "false"}},
    {"networkPolicy": {"allowedNamespaces": ["Bad_Name"]}},
    {"networkPolicy": {"allowedNamespaces": ["mcp", "mcp"]}},
    {"networkPolicy": {"allowedNamespaces": ["a" * 64]}},
    {"networkPolicy": {"allowedNamespaces": [""]}},
    {"networkPolicy": {"allowedNamespaces": "mcp"}},
    {"networkPolicy": {"enabled": False, "allowedNamespaces": ["mcp"]}},
    {"networkPolicy": {"extra": True}},
])
def test_networkpolicy_schema_refuses_invalid_selection(mutate):
    values = base_values()
    values["networkPolicy"].update(mutate["networkPolicy"])
    r = _helm_template_with_values(values)
    assert r.returncode != 0
    assert "networkPolicy" in r.stderr


# ------------------------------------------------ feedback service (#804)

FEEDBACK_KEYS = [
    ("StatefulSet", "rag-feedback"), ("PersistentVolumeClaim", "rag-feedback-data"),
    ("Service", "rag-feedback"), ("NetworkPolicy", "rag-feedback-ingress"),
]
FEEDBACK_POLICY = ("NetworkPolicy", "rag-feedback-ingress")


def test_feedback_off_by_default_renders_nothing_and_changes_nothing():
    """Default off: no feedback object, no FEEDBACK_* on the agent, and an
    explicit enabled=false renders exactly the default (byte-identical)."""
    default = run_new_template({})
    assert not [k for k in default if "feedback" in k[1]]
    assert not [n for n in env_map(default["Deployment", "rag-agent"]) if n.startswith("FEEDBACK")]
    assert run_new_template({"feedback": {"enabled": False}}) == default
    # Settings made while disabled do not leak a thing into the render.
    assert run_new_template({"feedback": {"enabled": False, "secretName": "x", "storageSize": "1Ti"}}) == default
    raw = _helm_template_with_values(base_values())
    assert "rag-feedback" not in raw.stdout and "FEEDBACK" not in raw.stdout


def test_feedback_enabled_workload_contract():
    docs = run_new_template({**FEEDBACK_ON, "_namespace": "rag-ns"})
    assert {k for k in docs if "feedback" in k[1]} == set(FEEDBACK_KEYS)
    sts = docs["StatefulSet", "rag-feedback"]
    assert sts["metadata"]["namespace"] == "rag-ns"
    spec = sts["spec"]
    # Single writer: one replica, ordered, selector = pod label = Service/policy selector.
    assert spec["replicas"] == 1 and spec["podManagementPolicy"] == "OrderedReady"
    assert spec["serviceName"] == "rag-feedback"
    assert spec["selector"]["matchLabels"] == {"app": "rag-feedback"}
    assert spec["template"]["metadata"]["labels"] == {"app": "rag-feedback"}
    assert "volumeClaimTemplates" not in spec  # standalone, kept claim (D2)
    container = container_map(sts)["feedback"]
    assert list(container_map(sts)) == ["feedback"]
    # The existing agent image, started through the service entry point with
    # exactly one uvicorn worker (no --workers).
    assert container["image"] == f"reg.internal/qdrant-pdf-rag-agent:{IMAGE_SHA}"
    assert container["command"] == ["python3", "-m", "uvicorn"]
    assert container["args"] == ["--factory", "mainframe_rag.feedback.service:app_from_env",
                                 "--host", "0.0.0.0", "--port", "8080"]
    env = env_map(sts, "feedback")
    # Exactly this env set (no Qdrant, model, gateway or OTLP settings, and no
    # FEEDBACK_ENABLED, which is the agent's client switch).
    assert set(env) == {"FEEDBACK_STORE_PATH", "FEEDBACK_RETENTION_DAYS", "METRICS_ENABLED",
                        "FEEDBACK_SERVICE_TOKEN"}
    assert {k: v["value"] for k, v in env.items() if "value" in v} == {
        "FEEDBACK_STORE_PATH": "/data/feedback.sqlite3", "FEEDBACK_RETENTION_DAYS": "180",
        "METRICS_ENABLED": "false"}
    assert env["FEEDBACK_SERVICE_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "key": "service-token", "name": "fb-secret"}
    assert container["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert container["livenessProbe"]["failureThreshold"] == 6
    assert container["resources"] == {"requests": {"cpu": "50m", "memory": "64Mi"},
                                      "limits": {"cpu": "250m", "memory": "256Mi"}}
    assert spec["template"]["spec"]["imagePullSecrets"] == []
    assert "serviceAccountName" not in spec["template"]["spec"]


def test_feedback_claim_service_and_agent_wiring():
    docs = run_new_template({"feedback": {**FEEDBACK_ON["feedback"], "storageSize": "20Gi",
                                          "retentionDays": 30},
                             "storage": {"className": "fast-block"}, "metrics": {"enabled": True}})
    pvc = docs["PersistentVolumeClaim", "rag-feedback-data"]
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert pvc["spec"] == {"accessModes": ["ReadWriteOnce"], "storageClassName": "fast-block",
                           "resources": {"requests": {"storage": "20Gi"}}}
    env = env_map(docs["StatefulSet", "rag-feedback"], "feedback")
    assert env["FEEDBACK_RETENTION_DAYS"]["value"] == "30" and env["METRICS_ENABLED"]["value"] == "true"
    svc = docs["Service", "rag-feedback"]
    assert svc["spec"]["type"] == "ClusterIP" and svc["spec"]["selector"] == {"app": "rag-feedback"}
    assert svc["spec"]["ports"] == [{"name": "http", "port": 8080, "targetPort": "http"}]
    assert not [k for k in docs if k[0] in ("Route", "Ingress")]
    # The agent reaches the Service by short name with the same Secret key.
    agent = env_map(docs["Deployment", "rag-agent"])
    assert agent["FEEDBACK_ENABLED"]["value"] == "true"
    assert agent["FEEDBACK_URL"]["value"] == "http://rag-feedback:8080"
    assert agent["FEEDBACK_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "key": "service-token", "name": "fb-secret"}
    claim = docs["StatefulSet", "rag-feedback"]["spec"]["template"]["spec"]["volumes"]
    assert {"name": "data", "persistentVolumeClaim": {"claimName": "rag-feedback-data"}} in claim


def test_feedback_pod_holds_no_credential_other_than_its_own_token():
    docs = run_new_template({**FEEDBACK_ON, "gateway": {"apiKeySecretName": "gw-keys"},
                             "tracing": {"deploymentEnvironment": "prod"}})
    text = json.dumps(docs["StatefulSet", "rag-feedback"])
    for forbidden in ("QDRANT", "gw-keys", "LLM_", "EMBED_", "RERANK", "OTEL", "apikey"):
        assert forbidden not in text, forbidden
    assert text.count("secretKeyRef") == 1


def test_feedback_policy_admits_agent_pods_only_and_monitoring_only_with_metrics():
    docs = run_new_template({**FEEDBACK_ON, "networkPolicy": {"allowedNamespaces": ["mcp"]}})
    policy = docs[FEEDBACK_POLICY]
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "rag-feedback"}}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert policy["spec"]["ingress"] == [
        {"from": [{"podSelector": {"matchLabels": {"app": "rag-agent"}}}],
         "ports": [{"protocol": "TCP", "port": 8080}]}]
    # The agent policy is unchanged by the feedback service and does not select it.
    assert docs[POLICY_KEY]["spec"]["podSelector"] == {"matchLabels": {"app": "rag-agent"}}
    with_metrics = run_new_template({**FEEDBACK_ON, "metrics": {"enabled": True}})[FEEDBACK_POLICY]
    assert with_metrics["spec"]["ingress"][1:] == [
        {"from": [{"namespaceSelector": {"matchLabels": MONITORING_LABEL}},
                  {"namespaceSelector": {"matchLabels": UWM_NAME}}],
         "ports": [{"protocol": "TCP", "port": 8080}]}]


def _feedback_allows(policy, source_ns, ns_labels, pod_labels, port, release_ns="ns"):
    """Independent NetworkPolicy ingress semantics for one pod (in namespace
    `source_ns` with `ns_labels`, carrying `pod_labels`) reaching a feedback
    pod. A podSelector peer matches pods of the release namespace only; a
    namespaceSelector peer matches any pod of a matching namespace; the rule's
    ports must match too. Enforcement is the CNI's and stays site evidence."""
    for rule in policy["spec"]["ingress"]:
        if port not in {p["port"] for p in rule["ports"] if p["protocol"] == "TCP"}:
            continue
        for peer in rule["from"]:
            if set(peer) == {"podSelector"}:
                want = peer["podSelector"].get("matchLabels", {})
                if source_ns == release_ns and all(pod_labels.get(k) == v for k, v in want.items()):
                    return True
            elif set(peer) == {"namespaceSelector"}:
                want = peer["namespaceSelector"]["matchLabels"]
                if all(ns_labels.get(k) == v for k, v in want.items()):
                    return True
            else:
                raise AssertionError(f"unexpected peer shape: {peer}")
    return False


def test_feedback_policy_semantics_for_the_documented_counterexamples():
    policy = run_new_template({**FEEDBACK_ON, "metrics": {"enabled": True},
                               "networkPolicy": {"allowedNamespaces": ["mcp"]}})[FEEDBACK_POLICY]
    agent = {"app": "rag-agent"}
    here = {"kubernetes.io/metadata.name": "ns"}
    # Agent pod of the release namespace: allowed on 8080 only.
    assert _feedback_allows(policy, "ns", here, agent, 8080)
    assert not _feedback_allows(policy, "ns", here, agent, 9090)
    # The ingest Job, a debug pod and any other pod of the same namespace: denied
    # (unlike rag-agent-ingress, which admits podSelector {}).
    for labels in ({"app": "rag-ingest"}, {"job-name": "ingest"}, {}):
        assert not _feedback_allows(policy, "ns", here, labels, 8080)
    # An agent-labelled pod in another namespace, and an allowedNamespaces
    # namespace (admitted to the agent, not to feedback): denied.
    assert not _feedback_allows(policy, "elsewhere", {"kubernetes.io/metadata.name": "elsewhere"}, agent, 8080)
    assert not _feedback_allows(policy, "mcp", {"kubernetes.io/metadata.name": "mcp"}, {}, 8080)
    # Monitoring namespaces (metrics on): admitted by label or by UWM name, 8080 only.
    for name, labels in (("openshift-monitoring", MONITORING_LABEL),
                         ("openshift-user-workload-monitoring", UWM_NAME)):
        assert _feedback_allows(policy, name, labels, {}, 8080)
        assert not _feedback_allows(policy, name, labels, {}, 8443)
    assert not _feedback_allows(policy, "other", {"kubernetes.io/metadata.name": "other"}, {}, 8080)
    # Metrics off: monitoring is not admitted.
    off = run_new_template({**FEEDBACK_ON})[FEEDBACK_POLICY]
    assert not _feedback_allows(off, "openshift-monitoring", MONITORING_LABEL, {}, 8080)


def test_feedback_servicemonitor_follows_the_agent_decision_and_authenticates():
    key = ("ServiceMonitor", "rag-feedback")
    docs = run_new_template({**FEEDBACK_ON, "metrics": {"enabled": True}})
    assert docs[key]["spec"]["selector"] == {"matchLabels": {"app": "rag-feedback"}}
    assert docs[key]["spec"]["endpoints"] == [{
        "port": "http", "path": "/metrics", "interval": "30s", "scrapeTimeout": "10s",
        "authorization": {"credentials": {"name": "fb-secret", "key": "service-token"}}}]
    # Metrics off, monitor explicitly off, or feedback off: no monitor.
    assert key not in run_new_template({**FEEDBACK_ON})
    assert key not in run_new_template({**FEEDBACK_ON, "metrics": {"enabled": True, "serviceMonitor": {"enabled": False}}})
    assert key not in run_new_template({"metrics": {"enabled": True}})
    # The agent's own contract is untouched by the feedback monitor.
    assert ("ServiceMonitor", "rag-agent") in docs


@pytest.mark.parametrize("mutate", [
    {"feedback": {"enabled": "true", "secretName": "s"}},
    {"feedback": {"enabled": True, "secretName": ""}},
    {"feedback": {"enabled": True, "secretName": "Bad_Name"}},
    {"feedback": {"enabled": True, "secretName": "s", "storageSize": "ten"}},
    {"feedback": {"enabled": True, "secretName": "s", "storageSize": "0Gi"}},
    {"feedback": {"enabled": True, "secretName": "s", "retentionDays": 0}},
    {"feedback": {"enabled": True, "secretName": "s", "retentionDays": 3651}},
    {"feedback": {"enabled": True, "secretName": "s", "retentionDays": "180"}},
    {"feedback": {"enabled": True, "secretName": "s", "extra": 1}},
])
def test_feedback_schema_refuses_invalid_selection(mutate):
    values = base_values()
    values.update(mutate)
    r = _helm_template_with_values(values)
    assert r.returncode != 0 and "feedback" in r.stderr


def test_feedback_requires_the_networkpolicy_and_a_secret():
    values = base_values()
    values["feedback"] = {"enabled": True, "secretName": "s"}
    values["networkPolicy"] = {"enabled": False, "allowedNamespaces": []}
    r = _helm_template_with_values(values)
    assert r.returncode != 0 and "requires networkPolicy.enabled=true" in r.stderr
    # Not enabled: the policy switch stays independent.
    values["feedback"] = {"enabled": False}
    assert _helm_template_with_values(values).returncode == 0


def test_feedback_claim_is_the_only_object_retained_when_disabled():
    """Disabling renders no feedback object at all (Helm prunes workload,
    Service, policy and monitor); the claim's keep policy is what retains it."""
    on = run_new_template({**FEEDBACK_ON, "metrics": {"enabled": True}})
    off = run_new_template({"metrics": {"enabled": True}})
    assert {k for k in on if "feedback" in k[1]} == {
        *FEEDBACK_KEYS, ("ServiceMonitor", "rag-feedback")}
    assert not [k for k in off if "feedback" in k[1]]
    assert on["PersistentVolumeClaim", "rag-feedback-data"]["metadata"]["annotations"] == {
        "helm.sh/resource-policy": "keep"}


# ------------------------------------------- Open WebUI front end (#1074)

OWUI_KEYS = {
    ("Deployment", "rag-openwebui"), ("Service", "rag-openwebui"),
    ("PersistentVolumeClaim", "rag-openwebui-data"), ("NetworkPolicy", "rag-openwebui"),
    ("Route", "rag-openwebui"), ("ConfigMap", "rag-openwebui-pipe"),
    ("ConfigMap", "rag-openwebui-proxy"),
}
# The complete environment of the Open WebUI container (N4: pin every toggle).
OWUI_ENV_NAMES = {
    "ANONYMIZED_TELEMETRY", "API_KEYS_ALLOWED_ENDPOINTS", "AUDIT_LOG_LEVEL",
    "BYPASS_EMBEDDING_AND_RETRIEVAL", "BYPASS_MODEL_ACCESS_CONTROL", "CORS_ALLOW_ORIGIN",
    "DATA_DIR", "DO_NOT_TRACK", "ENABLE_ADMIN_CHAT_ACCESS", "ENABLE_ADMIN_EXPORT",
    "ENABLE_API_KEYS", "ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS",
    "ENABLE_AUTOCOMPLETE_GENERATION", "ENABLE_CHANNELS", "ENABLE_CODE_EXECUTION",
    "ENABLE_CODE_INTERPRETER", "ENABLE_COMMUNITY_SHARING", "ENABLE_DIRECT_CONNECTIONS",
    "ENABLE_DIRECT_INTEGRATIONS", "ENABLE_EVALUATION_ARENA_MODELS", "ENABLE_FOLDERS",
    "ENABLE_FOLLOW_UP_GENERATION", "ENABLE_IMAGE_GENERATION", "ENABLE_INITIAL_ADMIN_SIGNUP",
    "ENABLE_LOGIN_FORM", "ENABLE_MEMORIES", "ENABLE_MESSAGE_RATING", "ENABLE_NOTES",
    "ENABLE_OAUTH_GROUP_CREATION", "ENABLE_OAUTH_GROUP_MANAGEMENT",
    "ENABLE_OAUTH_PERSISTENT_CONFIG", "ENABLE_OAUTH_ROLE_MANAGEMENT", "ENABLE_OAUTH_SIGNUP",
    "ENABLE_OAUTH_TOKEN_EXCHANGE", "ENABLE_OLLAMA_API", "ENABLE_OPENAI_API", "ENABLE_OTEL",
    "ENABLE_PASSWORD_AUTH", "ENABLE_PERSISTENT_CONFIG",
    "ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS", "ENABLE_RETRIEVAL_QUERY_GENERATION",
    "ENABLE_SEARCH_QUERY_GENERATION", "ENABLE_SIGNUP", "ENABLE_TAGS_GENERATION",
    "ENABLE_TITLE_GENERATION", "ENABLE_TOOLS", "ENABLE_TOOL_SERVERS",
    "ENABLE_VERSION_UPDATE_CHECK", "ENABLE_WEB_SEARCH", "GLOBAL_LOG_LEVEL", "HF_HUB_OFFLINE",
    "HOME", "JWT_EXPIRES_IN", "OAUTH_ADMIN_ROLES", "OAUTH_ALLOWED_ROLES", "OAUTH_CLIENT_ID",
    "OAUTH_CLIENT_SECRET", "OAUTH_EMAIL_CLAIM", "OAUTH_GROUP_CLAIM", "OAUTH_MERGE_ACCOUNTS_BY_EMAIL",
    "OAUTH_ROLES_CLAIM", "OAUTH_SCOPES", "OAUTH_USERNAME_CLAIM", "OFFLINE_MODE", "OLLAMA_BASE_URL",
    "OPENID_PROVIDER_URL", "OPENID_REDIRECT_URI", "RAG_EMBEDDING_MODEL_AUTO_UPDATE",
    "RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE", "RAG_RERANKING_MODEL_AUTO_UPDATE",
    "RAG_RERANKING_MODEL_TRUST_REMOTE_CODE", "SCARF_NO_ANALYTICS", "TASK_MODEL",
    "USER_PERMISSIONS_CHAT_CALL", "USER_PERMISSIONS_CHAT_FILE_UPLOAD",
    "USER_PERMISSIONS_CHAT_STT", "USER_PERMISSIONS_CHAT_TTS",
    "USER_PERMISSIONS_FEATURES_API_KEYS", "USER_PERMISSIONS_FEATURES_CODE_INTERPRETER",
    "USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS",
    "USER_PERMISSIONS_FEATURES_IMAGE_GENERATION", "USER_PERMISSIONS_FEATURES_MEMORIES",
    "USER_PERMISSIONS_FEATURES_NOTES", "USER_PERMISSIONS_FEATURES_WEB_SEARCH",
    "USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS", "USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS",
    "USER_PERMISSIONS_WORKSPACE_TOOLS_ACCESS", "UVICORN_WORKERS", "VECTOR_DB", "WEBUI_AUTH",
    "WEBUI_SECRET_KEY", "WEBUI_URL", "WHISPER_MODEL_AUTO_UPDATE",
}
OWUI_PIPE = "class Pipe:\n    \"\"\"Synthetic stand-in for the #1072 docs Pipe.\"\"\"\n"
OWUI_ON = {
    "route": {"destinationCA": FAKE_CA},
    "openwebui": {
        "enabled": True,
        "secretName": "owui-secrets",
        "host": "docs.apps.example.internal",
        "retentionDays": 45,
        "oidc": {
            "providerUrl": "https://idp.example.internal/realms/x/.well-known/openid-configuration",
            "clientId": "docs-frontend",
            "allowedRoles": "staff",
            "adminRoles": "docs-admins,docs-owners",
        },
        "idpEgress": {"cidrs": ["10.20.0.0/24", "10.21.0.8/32"], "port": 8443},
    },
}


@pytest.fixture(scope="module")
def owui_chart(tmp_path_factory):
    """The chart plus a generated stand-in for the #1072 Pipe: the real chart
    ships no Pipe until #1072 lands, and enabling it then fails closed."""
    import shutil

    root = tmp_path_factory.mktemp("owui") / "mainframe-rag"
    shutil.copytree(CHART, root)
    (root / "files" / "openwebui" / "docs_pipe.py").write_text(OWUI_PIPE, encoding="utf-8")
    return root


def _owui(chart, extra=None, **kw):
    values = json.loads(json.dumps(OWUI_ON))
    for key, val in (extra or {}).items():
        if isinstance(val, dict) and isinstance(values.get(key), dict):
            values[key] = {**values[key], **val}
        else:
            values[key] = val
    return run_new_template(values, chart=chart, **kw)


def _bare_default_values() -> dict:
    """The base values with the Open WebUI block removed: the chart default applies."""
    values = base_values()
    del values["openwebui"]
    return values


OWUI_FAIL_CLOSED_KEYS = [
    "openwebui.secretName", "openwebui.host", "openwebui.oidc.providerUrl",
    "openwebui.oidc.clientId", "openwebui.oidc.allowedRoles", "openwebui.oidc.adminRoles",
    "openwebui.idpEgress.cidrs", "route.destinationCA",
]


def test_openwebui_is_on_by_default_and_the_bare_render_fails_closed_with_a_fixed_message():
    """ADR-0008: the shipped default is enabled; with no site inputs the render
    is refused (never half-configured) and the message names every missing
    value and the opt-out, and nothing else."""
    with open(CHART / "values.yaml", encoding="utf-8") as f:
        assert yaml.safe_load(f)["openwebui"]["enabled"] is True
    r = _helm_template_with_values(_bare_default_values())
    assert r.returncode != 0
    expected = (
        "Open WebUI is enabled by default (ADR-0008) but required site inputs are missing: "
        + ", ".join(OWUI_FAIL_CLOSED_KEYS)
        + ". Set them, or opt out with --set openwebui.enabled=false "
        "(OPENWEBUI_ENABLED=false in airgap.env)."
    )
    assert expected in r.stderr
    assert "rag-openwebui" not in r.stdout


@pytest.mark.parametrize("key", OWUI_FAIL_CLOSED_KEYS)
def test_openwebui_fail_closed_names_exactly_the_one_missing_input(owui_chart, key):
    values = _bare_default_values()
    values.update(json.loads(json.dumps(OWUI_ON)))
    section, _, rest = key.partition(".")
    node = values[section]
    path = rest.split(".")
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = [] if path[-1] == "cidrs" else ""
    r = _helm_template_with_values(values, chart=owui_chart)
    assert r.returncode != 0
    assert f"inputs are missing: {key}. Set them, or opt out with --set openwebui.enabled=false" in r.stderr
    # Values (host, client id, CA) never reach the diagnostic.
    for leaked in ("docs.apps.example.internal", "docs-frontend", "FAKE-CA-BUNDLE"):
        assert leaked not in r.stderr


def test_openwebui_opt_out_is_byte_identical_to_a_chart_without_the_component(tmp_path):
    """K1: enabled=false renders byte-for-byte what a chart that does not contain
    the front end renders, even with every site input present."""
    import shutil
    stripped = tmp_path / "chart"
    shutil.copytree(CHART, stripped)
    for tpl in (stripped / "templates").glob("openwebui-*.yaml"):
        tpl.unlink()
    off = base_values()
    off["openwebui"] = {**OWUI_ON["openwebui"], "enabled": False}
    with_component = _helm_template_with_values(off)
    without = _helm_template_with_values(base_values(), chart=stripped)
    assert with_component.returncode == 0 and without.returncode == 0
    assert "openwebui" not in with_component.stdout and "OPENID" not in with_component.stdout
    assert with_component.stdout == without.stdout
    # Opting out needs no other front-end input at all.
    assert _helm_template_with_values(base_values()).stdout == with_component.stdout


def test_openwebui_ci_site_inputs_render_the_front_end_and_stay_fake():
    """The checked-in test inputs (for helm lint/template in CI) satisfy the
    fail-closed check, and are non-routable so they cannot become site values."""
    path = CHART / "ci" / "openwebui-site-values.yaml"
    inputs = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert inputs["openwebui"]["host"].endswith(".invalid")
    assert inputs["openwebui"]["oidc"]["providerUrl"].startswith("https://idp.example.invalid/")
    assert inputs["openwebui"]["idpEgress"]["cidrs"] == ["192.0.2.0/24"]
    r = run(["helm", "template", "test", str(CHART), "--namespace", "ns", "-f", str(path)], cwd=REPO)
    assert r.returncode == 0, r.stderr
    assert "name: rag-openwebui" in r.stdout


def test_openwebui_enabled_adds_its_objects_and_leaves_the_rest_unchanged(owui_chart):
    """Enabling adds exactly the front-end set; agent, console, feedback and
    backend objects are identical (no agent behaviour change)."""
    on = _owui(owui_chart)
    default = run_new_template({}, chart=owui_chart)
    assert set(on) - set(default) == OWUI_KEYS
    for key, doc in default.items():
        assert on[key] == doc, key
    # Not tied to the console Route: route.enabled stays false and no oauth-proxy appears.
    assert ("Route", "rag-agent") not in on
    assert list(container_map(on["Deployment", "rag-agent"])) == ["agent"]


def test_openwebui_workload_is_single_replica_restricted_and_holds_no_model_key(owui_chart):
    docs = _owui(owui_chart, {"_namespace": "rag-ns"})
    dep = docs["Deployment", "rag-openwebui"]
    assert dep["metadata"]["namespace"] == "rag-ns"
    spec = dep["spec"]
    assert spec["replicas"] == 1 and spec["strategy"] == {"type": "Recreate"}
    assert spec["selector"]["matchLabels"] == {"app": "rag-openwebui"}
    pod = spec["template"]["spec"]
    assert spec["template"]["metadata"]["labels"] == {"app": "rag-openwebui"}
    # restricted-v2 with a platform-assigned (random) UID: no fixed identity,
    # no anyuid request, no service-account token, no privileged knobs.
    assert _hardening_violations(pod) == []
    assert pod["automountServiceAccountToken"] is False and "serviceAccountName" not in pod
    for forbidden in ("hostNetwork", "hostPID", "hostIPC", "initContainers"):
        assert forbidden not in pod
    assert not [k for k in docs if k[0] in ("Role", "RoleBinding", "SecurityContextConstraints")]
    assert "anyuid" not in json.dumps(dep)
    assert [c["name"] for c in pod["containers"]] == ["openwebui", "proxy", "chat-purge"]
    container, proxy, purge = pod["containers"]
    assert container["image"] == "qdrant-pdf-rag-openwebui:v0.12.3-slim"
    assert container["securityContext"].get("privileged") is not True
    # One worker, plain HTTP on loopback ONLY: the allow-list proxy sidecar is
    # the single listener the Service and Route reach (nothing bypasses it).
    assert container["args"][:2] == ["open_webui.main:app", "--host"]
    args = container["args"]
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--workers") + 1] == "1" and args[args.index("--port") + 1] == "8080"
    assert "--ssl-certfile" not in args and "--ssl-keyfile" not in args
    assert "ports" not in container
    assert container["securityContext"]["readOnlyRootFilesystem"] is True  # B1
    for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
        assert "httpGet" not in container[probe] and "exec" in container[probe], probe
        assert "127.0.0.1:8080" in " ".join(container[probe]["exec"]["command"]), probe
    # The proxy: pinned nginx-unprivileged class, TLS from the serving certificate
    # on the only declared port, a read-only root, the allow-list from the ConfigMap.
    assert proxy["image"] == "nginxinc/nginx-unprivileged:1.30.6-alpine"
    assert proxy["ports"] == [{"containerPort": 8443, "name": "https"}]
    assert proxy["args"] == ["nginx", "-c", "/etc/nginx/proxy/nginx.conf", "-g", "daemon off;"]
    assert proxy["securityContext"]["readOnlyRootFilesystem"] is True
    assert proxy["securityContext"]["allowPrivilegeEscalation"] is False
    assert "env" not in proxy
    assert proxy["readinessProbe"]["httpGet"] == {"path": "/health/db", "port": "https", "scheme": "HTTPS"}
    assert proxy["livenessProbe"]["tcpSocket"] == {"port": "https"}
    assert proxy["startupProbe"]["tcpSocket"] == {"port": "https"}
    assert {m["mountPath"]: m.get("readOnly", False) for m in proxy["volumeMounts"]} == {
        "/etc/nginx/proxy": True, "/etc/tls/private": True, "/tmp": False}
    proxy_vols = {m["mountPath"]: m["name"] for m in proxy["volumeMounts"]}
    pod_vols = {v["name"]: v for v in pod["volumes"]}
    assert pod_vols[proxy_vols["/etc/nginx/proxy"]]["configMap"] == {"name": "rag-openwebui-proxy"}
    assert pod_vols[proxy_vols["/tmp"]] == {"name": proxy_vols["/tmp"], "emptyDir": {}}
    assert proxy_vols["/tmp"] != next(m["name"] for m in container["volumeMounts"] if m["mountPath"] == "/tmp")
    assert dep["spec"]["template"]["metadata"]["annotations"]["checksum/proxy-config"] == hashlib.sha256(
        (CHART / "files" / "openwebui" / "proxy.nginx.conf").read_bytes()).hexdigest()
    env = env_map(dep, "openwebui")
    plain = {k: v["value"] for k, v in env.items() if "value" in v}
    for key, value in {
        "OFFLINE_MODE": "true", "ENABLE_PERSISTENT_CONFIG": "false", "UVICORN_WORKERS": "1",
        "ENABLE_OPENAI_API": "false", "ENABLE_OLLAMA_API": "false",
        # #1073 trial R2: without an explicit loopback base URL v0.12.3 probes
        # host.docker.internal:11434/12434 on first start.
        "OLLAMA_BASE_URL": "http://127.0.0.1:9",
        "ENABLE_DIRECT_CONNECTIONS": "false", "ENABLE_COMMUNITY_SHARING": "false",
        "ENABLE_VERSION_UPDATE_CHECK": "false", "BYPASS_EMBEDDING_AND_RETRIEVAL": "true",
        "ENABLE_WEB_SEARCH": "false", "ENABLE_MEMORIES": "false",
        "USER_PERMISSIONS_CHAT_FILE_UPLOAD": "false", "ENABLE_CODE_EXECUTION": "false",
        "ENABLE_CODE_INTERPRETER": "false", "ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS": "false",
        "ENABLE_TITLE_GENERATION": "false", "ENABLE_ADMIN_CHAT_ACCESS": "false",
        "ENABLE_ADMIN_EXPORT": "false", "ENABLE_SIGNUP": "false", "ENABLE_LOGIN_FORM": "false",
        "ENABLE_PASSWORD_AUTH": "false", "ENABLE_INITIAL_ADMIN_SIGNUP": "false",
        "ENABLE_OAUTH_TOKEN_EXCHANGE": "false", "ENABLE_OAUTH_GROUP_MANAGEMENT": "true",
        "ENABLE_OAUTH_ROLE_MANAGEMENT": "true", "JWT_EXPIRES_IN": "8h",
        "DATA_DIR": "/app/backend/data", "AUDIT_LOG_LEVEL": "METADATA",
        "OPENID_PROVIDER_URL": OWUI_ON["openwebui"]["oidc"]["providerUrl"],
        "OPENID_REDIRECT_URI": "https://docs.apps.example.internal/oauth/oidc/callback",
        "WEBUI_URL": "https://docs.apps.example.internal", "OAUTH_CLIENT_ID": "docs-frontend",
        "OAUTH_GROUP_CLAIM": "groups", "OAUTH_ROLES_CLAIM": "roles",
        # Open WebUI v0.12.3's own defaults, pinned explicitly (config.py).
        "OAUTH_EMAIL_CLAIM": "email", "OAUTH_USERNAME_CLAIM": "name",
        "OAUTH_ALLOWED_ROLES": "staff", "OAUTH_ADMIN_ROLES": "docs-admins,docs-owners",
        # B1 (trial/review): tools, direct integrations, per-feature permissions,
        # task model, audio, scopes, CORS and the narrow admin-only API key.
        "ENABLE_TOOLS": "false", "ENABLE_DIRECT_INTEGRATIONS": "false",
        "BYPASS_MODEL_ACCESS_CONTROL": "false", "TASK_MODEL": "",
        "USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS": "false",
        "USER_PERMISSIONS_FEATURES_NOTES": "false",
        "USER_PERMISSIONS_FEATURES_IMAGE_GENERATION": "false",
        "USER_PERMISSIONS_CHAT_STT": "false", "USER_PERMISSIONS_CHAT_TTS": "false",
        "USER_PERMISSIONS_CHAT_CALL": "false",
        "OAUTH_SCOPES": "openid email profile groups offline_access",
        "CORS_ALLOW_ORIGIN": "https://docs.apps.example.internal",
        "ENABLE_API_KEYS": "true", "USER_PERMISSIONS_FEATURES_API_KEYS": "false",
        "ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS": "true",
        "API_KEYS_ALLOWED_ENDPOINTS": "/api/v1/functions",
    }.items():
        assert plain.get(key) == value, key
    assert "PGVECTOR_DB_URL" not in env and "OPENAI_API_KEY" not in env
    # N1: no Ollama setting but the pinned loopback base URL.
    assert {k for k in env if k.startswith("OLLAMA_")} == {"OLLAMA_BASE_URL"}
    # N4: every toggle the pod sets is pinned above or below; a new env name is a
    # deliberate edit of this table, never an accident.
    assert set(env) == OWUI_ENV_NAMES
    # Exactly the two operator Secret references; nothing else is a credential.
    refs = {k: v["valueFrom"]["secretKeyRef"] for k, v in env.items() if "valueFrom" in v}
    assert refs == {
        "WEBUI_SECRET_KEY": {"name": "owui-secrets", "key": "webui-secret-key"},
        "OAUTH_CLIENT_SECRET": {"name": "owui-secrets", "key": "oidc-client-secret"},
    }
    # No agent, gateway, Qdrant or model connection setting reaches the pod.
    for name in env:
        assert not name.startswith(("QDRANT", "LLM_", "EMBED_", "RERANK_", "GATEWAY", "OTEL_",
                                    "OPENAI_API_", "FEEDBACK")), name
    text = json.dumps(dep)
    for forbidden in ("api-key", "apikey", "qdrant:6333", "openobserve-credentials"):
        assert forbidden not in text, forbidden
    assert dep["metadata"]["annotations"]["mainframe-rag/chat-retention-days"] == "45"
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert set(mounts) == {"/app/backend/data", "/tmp", "/etc/tls/private",
                           "/opt/mainframe-rag/openwebui", "/app/backend/open_webui/static", "/root"}
    # The root filesystem is read-only: every writable path is the claim or an emptyDir.
    pod_volumes = {v["name"]: v for v in pod["volumes"]}
    for writable in ("/tmp", "/app/backend/open_webui/static", "/root"):
        assert pod_volumes[mounts[writable]["name"]]["emptyDir"] == {}, writable
    assert mounts["/etc/tls/private"]["readOnly"] and mounts["/opt/mainframe-rag/openwebui"]["readOnly"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes[mounts["/app/backend/data"]["name"]]["persistentVolumeClaim"] == {
        "claimName": "rag-openwebui-data"}
    assert volumes[mounts["/tmp"]["name"]] == {"name": mounts["/tmp"]["name"], "emptyDir": {}}
    assert volumes[mounts["/etc/tls/private"]["name"]]["secret"] == {"secretName": "rag-openwebui-tls"}
    assert volumes[mounts["/opt/mainframe-rag/openwebui"]["name"]]["configMap"] == {
        "name": "rag-openwebui-pipe"}
    # The retention purge (ADR-0007 S2): same image and claim, the configured
    # window, a read-only root and no credential of any kind.
    assert purge["image"] == container["image"]
    assert purge["command"] == ["python3", "/opt/mainframe-rag/openwebui/purge_chats.py"]
    assert purge["args"] == ["--days", "45", "--data-dir", "/app/backend/data"]
    assert purge["securityContext"]["readOnlyRootFilesystem"] is True
    assert "env" not in purge and "ports" not in purge
    assert {m["mountPath"]: m.get("readOnly", False) for m in purge["volumeMounts"]} == {
        "/app/backend/data": False, "/tmp": False, "/opt/mainframe-rag/openwebui": True}


IDP_CA_PEM = ("-----BEGIN CERTIFICATE-----\nSURQLUNBLTE=\n-----END CERTIFICATE-----\n"
              "-----BEGIN CERTIFICATE-----\nSURQLUNBLTI=\n-----END CERTIFICATE-----\n")
CA_TRUST_FILE = "/etc/mainframe-rag/ca-trust/ca-bundle.crt"
# Open WebUI v0.12.3 (pinned image): the sign-in calls (discovery, token, JWKS,
# userinfo) use authlib's httpx client with trust_env on, which reads
# SSL_CERT_FILE and otherwise certifi (httpx/_config.py create_ssl_context);
# aiohttp (token refresh, utils/oauth.py) uses ssl.create_default_context(),
# whose OpenSSL default file is also SSL_CERT_FILE; requests reads
# REQUESTS_CA_BUNDLE. SSL_CERT_DIR or AIOHTTP_CLIENT_SESSION_SSL=<file> would
# only add a second, partial owner of the same trust.
CA_TRUST_ENV = {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}


def test_openwebui_idp_ca_bundle_is_optional_and_adds_only_its_trust(owui_chart):
    """A public-CA IdP needs no bundle: the default render has no CA objects,
    variables or init step. With a bundle, the IdP CA is a ConfigMap mounted
    read-only into a hardened init step that writes the public stores the image
    already trusts (OpenSSL's system bundle and certifi) plus the IdP CA into an
    emptyDir, which the app mounts read-only and every HTTP client is pointed at."""
    plain = _owui(owui_chart)
    pod = plain["Deployment", "rag-openwebui"]["spec"]["template"]["spec"]
    assert "initContainers" not in pod
    assert not CA_TRUST_ENV & set(env_map(plain["Deployment", "rag-openwebui"], "openwebui"))
    assert ("ConfigMap", "rag-openwebui-idp-ca") not in plain
    assert "ca-trust" not in json.dumps(plain["Deployment", "rag-openwebui"])

    with_ca = _owui(owui_chart, {"openwebui": {
        **OWUI_ON["openwebui"], "oidc": {**OWUI_ON["openwebui"]["oidc"], "caBundle": IDP_CA_PEM}}})
    # Only the CA ConfigMap is added; every other object but the pod is unchanged.
    assert set(with_ca) - set(plain) == {("ConfigMap", "rag-openwebui-idp-ca")}
    for key, doc in plain.items():
        if key != ("Deployment", "rag-openwebui"):
            assert with_ca[key] == doc, key
    cm = with_ca["ConfigMap", "rag-openwebui-idp-ca"]
    script = (CHART / "files" / "openwebui" / "ca_trust.py").read_text(encoding="utf-8")
    assert cm["data"] == {"ca.crt": IDP_CA_PEM, "ca_trust.py": script}
    dep = with_ca["Deployment", "rag-openwebui"]
    pod = dep["spec"]["template"]["spec"]
    assert _hardening_violations(pod) == []
    # A changed bundle or script rolls the pod.
    assert dep["spec"]["template"]["metadata"]["annotations"]["checksum/idp-ca"] == hashlib.sha256(
        (IDP_CA_PEM + script).encode()).hexdigest()
    (init,) = pod["initContainers"]
    app = next(c for c in pod["containers"] if c["name"] == "openwebui")
    assert init["image"] == app["image"]
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    assert "env" not in init and "ports" not in init and "args" not in init
    # The shipped, tested script (test_openwebui_frontend_scripts.py): it loads
    # every IdP certificate and fails the init step on anything else.
    assert init["command"] == ["python3", "-I", "/etc/mainframe-rag/idp-ca/ca_trust.py"]
    # The public stores stay trusted: both defaults are read, then the IdP CA.
    for needle in ("certifi.where()", "openssl_cafile", '"/etc/mainframe-rag/idp-ca/ca.crt"',
                   f'"{CA_TRUST_FILE}"'):
        assert needle in script, needle
    volumes = {v["name"]: v for v in pod["volumes"]}
    init_mounts = {m["mountPath"]: m for m in init["volumeMounts"]}
    assert set(init_mounts) == {"/etc/mainframe-rag/idp-ca", "/etc/mainframe-rag/ca-trust"}
    assert init_mounts["/etc/mainframe-rag/idp-ca"]["readOnly"] is True
    assert volumes[init_mounts["/etc/mainframe-rag/idp-ca"]["name"]]["configMap"] == {
        "name": "rag-openwebui-idp-ca"}
    trust = init_mounts["/etc/mainframe-rag/ca-trust"]
    assert volumes[trust["name"]]["emptyDir"] == {}
    app_mounts = {m["mountPath"]: m for m in app["volumeMounts"]}
    assert app_mounts["/etc/mainframe-rag/ca-trust"] == {**trust, "readOnly": True}
    assert "/etc/mainframe-rag/idp-ca" not in app_mounts
    # The complete CA variable set, all on the combined file; nothing else changes.
    env = env_map(dep, "openwebui")
    assert {k: v["value"] for k, v in env.items() if k in CA_TRUST_ENV} == dict.fromkeys(
        CA_TRUST_ENV, CA_TRUST_FILE)
    assert set(env) == OWUI_ENV_NAMES | CA_TRUST_ENV
    assert not {"SSL_CERT_DIR", "AIOHTTP_CLIENT_SSL_CERT_FILE", "AIOHTTP_CLIENT_SESSION_SSL",
                "CURL_CA_BUNDLE"} & set(env)
    for name in ("proxy", "chat-purge"):
        container = next(c for c in pod["containers"] if c["name"] == name)
        assert "ca-trust" not in json.dumps(container), name


IDP_CA_REFUSAL = ("openwebui.oidc.caBundle must hold only PEM certificates (-----BEGIN CERTIFICATE----- "
                  "blocks, no key or other text); the value is not shown")
SECRET_KEY_BODY = "TUlJRXZRSUJBREFOQmdrcWhraUc5dzBCQVFFRkFBU0NCS2N3Z2dTakFnRUFBb0lCQVFDN0tFWV9TRUNSRVQ="


@pytest.mark.parametrize("bad", [
    "  \n", "not a certificate",
    "-----BEGIN CERTIFICATE-----\n%%%\n-----END CERTIFICATE-----\n",
    IDP_CA_PEM + "trailing junk\n",
    f"-----BEGIN PRIVATE KEY-----\n{SECRET_KEY_BODY}\n-----END PRIVATE KEY-----\n",
    IDP_CA_PEM + f"-----BEGIN RSA PRIVATE KEY-----\n{SECRET_KEY_BODY}\n-----END RSA PRIVATE KEY-----\n",
])
def test_openwebui_idp_ca_bundle_refuses_blank_or_garbage_without_echoing_it(owui_chart, bad):
    """Review #1083 B1: a schema pattern made Helm print the rejected value, so a
    pasted private key reached stderr. The chart refuses with fixed text only.
    (A corrupted certificate body that keeps the PEM shape passes the chart, which
    cannot parse X.509; the ca-trust init step refuses it and the pod is not Ready.)"""
    values = base_values()
    values.update(json.loads(json.dumps(OWUI_ON)))
    values["openwebui"]["oidc"]["caBundle"] = bad
    r = _helm_template_with_values(values, chart=owui_chart)
    assert r.returncode != 0 and IDP_CA_REFUSAL in r.stderr
    assert "rag-openwebui" not in r.stdout
    for leaked in (SECRET_KEY_BODY, "PRIVATE KEY", "junk", "not a certificate", "SURQLUNB", "%%%"):
        assert leaked not in r.stderr, leaked


def test_openwebui_idp_ca_bundle_schema_checks_the_type_only():
    """The schema keeps a type check (a string cannot be echoed by it) and no
    pattern, so the content check is the template's fixed-text refusal."""
    schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
    ca = schema["properties"]["openwebui"]["properties"]["oidc"]["properties"]["caBundle"]
    assert ca["type"] == "string" and "pattern" not in ca


def test_openwebui_claim_is_kept_rwo_on_the_block_class(owui_chart):
    docs = _owui(owui_chart, {"storage": {"className": "fast-block"},
                              "openwebui": {**OWUI_ON["openwebui"], "storageSize": "25Gi"}})
    pvc = docs["PersistentVolumeClaim", "rag-openwebui-data"]
    assert pvc["metadata"]["annotations"] == {"helm.sh/resource-policy": "keep"}
    assert pvc["spec"] == {"accessModes": ["ReadWriteOnce"], "storageClassName": "fast-block",
                           "resources": {"requests": {"storage": "25Gi"}}}
    values = base_values()
    values.update(json.loads(json.dumps(OWUI_ON)))
    values["storage"] = {"className": "nfs-client"}
    r = _helm_template_with_values(values, chart=owui_chart)
    assert r.returncode != 0 and "storage" in r.stderr


def test_openwebui_service_and_route_are_tls_reencrypt_with_the_namespace_ca(owui_chart):
    docs = _owui(owui_chart)
    svc = docs["Service", "rag-openwebui"]
    assert svc["metadata"]["annotations"] == {
        "service.beta.openshift.io/serving-cert-secret-name": "rag-openwebui-tls"}
    assert svc["spec"] == {"type": "ClusterIP", "selector": {"app": "rag-openwebui"},
                           "ports": [{"name": "https", "port": 8443, "targetPort": "https"}]}
    route = docs["Route", "rag-openwebui"]
    assert route["spec"]["host"] == "docs.apps.example.internal"
    assert route["spec"]["to"] == {"kind": "Service", "name": "rag-openwebui"}
    assert "alternateBackends" not in route["spec"]
    assert route["spec"]["port"] == {"targetPort": "https"}
    tls = route["spec"]["tls"]
    assert tls["termination"] == "reencrypt" and tls["insecureEdgeTerminationPolicy"] == "Redirect"
    assert tls["destinationCACertificate"].strip() == FAKE_CA
    assert set(tls) == {"termination", "insecureEdgeTerminationPolicy", "destinationCACertificate"}


def _owui_egress_allows(policy, peer_ns, peer_pod_labels, ip, proto, port, release_ns="ns"):
    """Independent egress semantics: a podSelector peer is a pod of the release
    namespace, a namespaceSelector peer a pod of a matching namespace (by
    name expression), an ipBlock peer an address inside the CIDR."""
    import ipaddress

    for rule in policy["spec"]["egress"]:
        if (proto, port) not in {(p["protocol"], p["port"]) for p in rule["ports"]}:
            continue
        for peer in rule["to"]:
            if set(peer) == {"podSelector"}:
                want = peer["podSelector"]["matchLabels"]
                if peer_ns == release_ns and all(peer_pod_labels.get(k) == v for k, v in want.items()):
                    return True
            elif set(peer) == {"namespaceSelector"}:
                (expr,) = peer["namespaceSelector"]["matchExpressions"]
                assert expr["key"] == "kubernetes.io/metadata.name" and expr["operator"] == "In"
                if peer_ns in expr["values"]:
                    return True
            elif set(peer) == {"ipBlock"}:
                assert set(peer["ipBlock"]) == {"cidr"}
                if ip and ipaddress.ip_address(ip) in ipaddress.ip_network(peer["ipBlock"]["cidr"]):
                    return True
            else:
                raise AssertionError(f"unexpected peer shape: {peer}")
    return False


def test_openwebui_policy_admits_the_router_only_and_egresses_to_agent_dns_and_idp(owui_chart):
    docs = _owui(owui_chart, {"networkPolicy": {"enabled": False, "allowedNamespaces": []}})
    # Rendered even with the agent policy off: it is the front end's egress control.
    policy = docs["NetworkPolicy", "rag-openwebui"]
    assert POLICY_KEY not in docs
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app": "rag-openwebui"}}
    assert spec["policyTypes"] == ["Ingress", "Egress"]
    assert spec["ingress"] == [{"from": [{"namespaceSelector": {"matchLabels": ROUTER_LABEL}}],
                                "ports": [{"protocol": "TCP", "port": 8443}]}]
    agent = {"app": "rag-agent"}
    assert _owui_egress_allows(policy, "ns", agent, None, "TCP", 8080)
    # Counterexamples: other pods or ports of the namespace, Qdrant, OpenObserve,
    # the feedback store, the agent of another namespace, the model gateway.
    for labels, port in (({"app": "rag-agent"}, 8443), ({"app": "qdrant"}, 6333),
                         ({"app": "openobserve"}, 5080), ({"app": "rag-feedback"}, 8080), ({}, 8080)):
        assert not _owui_egress_allows(policy, "ns", labels, None, "TCP", port), (labels, port)
    assert not _owui_egress_allows(policy, "other", agent, None, "TCP", 8080)
    for dns_ns in ("openshift-dns", "kube-system"):
        assert _owui_egress_allows(policy, dns_ns, {}, None, "UDP", 53)
    assert not _owui_egress_allows(policy, "openshift-ingress", {}, None, "UDP", 53)
    # IdP back channel: only the listed CIDRs, only the listed port.
    assert _owui_egress_allows(policy, None, {}, "10.20.0.77", "TCP", 8443)
    assert _owui_egress_allows(policy, None, {}, "10.21.0.8", "TCP", 8443)
    for ip, port in (("10.21.0.9", 8443), ("10.20.0.77", 443), ("8.8.8.8", 443), ("1.1.1.1", 53)):
        assert not _owui_egress_allows(policy, None, {}, ip, "TCP", port), (ip, port)
    # No empty (everything) peer and no 0.0.0.0/0.
    rules = spec["egress"]
    assert all(peer for r in rules for peer in r["to"])
    assert "0.0.0.0/0" not in json.dumps(rules)


def test_openwebui_pipe_configmap_carries_the_repository_files_and_their_hashes(owui_chart):
    import hashlib

    cm = _owui(owui_chart)["ConfigMap", "rag-openwebui-pipe"]
    installer = (CHART / "files" / "openwebui" / "provision_pipe.py").read_text(encoding="utf-8")
    purge = (CHART / "files" / "openwebui" / "purge_chats.py").read_text(encoding="utf-8")
    assert cm["data"] == {"docs_pipe.py": OWUI_PIPE, "provision_pipe.py": installer,
                          "purge_chats.py": purge}
    assert cm["metadata"]["annotations"] == {
        "mainframe-rag/docs-pipe-sha256": hashlib.sha256(OWUI_PIPE.encode()).hexdigest(),
        "mainframe-rag/provision-pipe-sha256": hashlib.sha256(installer.encode()).hexdigest(),
        "mainframe-rag/purge-chats-sha256": hashlib.sha256(purge.encode()).hexdigest(),
    }


ALLOWED_PATHS = [
    "/", "/c/3f2a", "/auth", "/admin/users", "/_app/immutable/chunks/a.js", "/static/loader.js",
    "/favicon.png", "/manifest.json", "/health", "/health/db", "/oauth/oidc/login",
    "/oauth/oidc/callback", "/ws/socket.io/", "/api/config", "/api/version", "/api/models",
    "/api/chat/completions", "/api/chat/completed", "/api/tasks/stop/x",
    "/api/v1/auths/", "/api/v1/auths/api_key", "/api/v1/chats/list", "/api/v1/chats/abc",
    "/api/v1/users/user/settings", "/api/v1/models/list", "/api/v1/groups/",
    "/api/v1/functions/id/mainframe_rag_docs", "/api/v1/functions/create",
    "/api/v1/configs/banners",
]
DENIED_PATHS = [
    "/api/v1/files/", "/api/v1/files", "/api/v1/files/abc/content", "/api/v1/retrieval/process/text",
    "/api/v1/retrieval/config", "/api/v1/knowledge/", "/api/v1/knowledge/create", "/api/v1/audio/transcriptions",
    "/api/v1/images/generations", "/api/v1/memories/add", "/api/v1/notes/", "/api/v1/channels/",
    "/api/v1/folders/", "/api/v1/utils/code/execute", "/api/v1/utils/db/download", "/api/v1/pipelines/",
    "/api/v1/scim/v2/Users", "/api/v1/terminals/", "/api/v1/search", "/api/v1/skills/",
    "/api/v1/evaluations/feedbacks", "/api/v1/automations/", "/openai/chat/completions",
    "/ollama/api/chat", "/docs", "/openapi.json", "/api/version/updates", "/api/changelog",
    "/api/chat", "/api/v1", "/api/v1/", "/api/", "/api", "/api/v1/chatsX", "/api/v1/auths_x",
    "/API/v1/files/", "/api/v1/files/../files/", "/ws", "/oauthx", "/health/x",
]


def _proxy_conf():
    return (CHART / "files" / "openwebui" / "proxy.nginx.conf").read_text(encoding="utf-8")


def _allow_regexes():
    import re
    conf = _proxy_conf()
    block = re.search(r"map \$uri \$owui_allowed \{(.*?)\n    \}", conf, re.DOTALL).group(1)
    entries = re.findall(r"^\s*~(\S+)\s+1;\s*$", block, re.MULTILINE)
    assert re.search(r"^\s*default\s+0;\s*$", block, re.MULTILINE), "deny must be the default"
    assert entries, "no allow-list entries"
    return [re.compile(e) for e in entries]


def test_openwebui_proxy_allow_list_admits_exactly_the_pilot_paths():
    rules = _allow_regexes()
    for path in ALLOWED_PATHS:
        assert any(r.search(path) for r in rules), path
    for path in DENIED_PATHS:
        assert not any(r.search(path) for r in rules), path


def test_openwebui_proxy_config_matches_the_decoded_uri_and_forwards_the_original_uri():
    """Maintainer decision 2026-10-24 (ADR-0007 E1): nginx decides on its
    decoded, normalised $uri; the request line is forwarded to uvicorn as the
    client sent it, so both see the same path; everything else is a fixed 403."""
    import re
    conf = _proxy_conf()
    assert re.search(r"^\s*merge_slashes\s+on;", conf, re.MULTILINE)
    assert re.search(r"^\s*server_tokens\s+off;", conf, re.MULTILINE)
    assert re.search(r"^\s*listen\s+8443\s+ssl;", conf, re.MULTILINE)
    assert "server 127.0.0.1:8080;" in conf
    # The URI the upstream sees is the original one: no URI part on proxy_pass,
    # no rewrite of any kind, and nothing else may change $uri before the check.
    proxy_passes = re.findall(r"^\s*proxy_pass\s+(\S+);", conf, re.MULTILINE)
    assert proxy_passes == ["http://owui_upstream"]
    assert not re.search(r"^\s*(rewrite|set\s+\$uri|try_files|alias|root)\b", conf, re.MULTILINE)
    assert re.search(r'if \(\$owui_allowed = 0\) \{\s*return 403 "denied\\n";\s*\}', conf)
    # The check precedes the proxy_pass inside the one location.
    assert conf.index("$owui_allowed = 0") < conf.index("proxy_pass http://")
    assert len(re.findall(r"^\s*location\b", conf, re.MULTILINE)) == 1
    # No request content in logs (S4): the access log is off.
    assert re.search(r"^\s*access_log\s+off;", conf, re.MULTILINE)
    # Streaming and socket.io upgrade.
    for needle in ("proxy_http_version 1.1;", "proxy_buffering off;", "$http_upgrade", "ssl_protocols TLSv1.2 TLSv1.3;"):
        assert needle in conf, needle
    assert "client_max_body_size 1m;" in conf


def test_openwebui_proxy_configmap_carries_the_repository_config(owui_chart):
    docs = _owui(owui_chart)
    cm = docs["ConfigMap", "rag-openwebui-proxy"]
    text = _proxy_conf()
    assert cm["data"] == {"nginx.conf": text}
    assert cm["metadata"]["annotations"] == {
        "mainframe-rag/proxy-config-sha256": hashlib.sha256(text.encode()).hexdigest()}
    # Not rendered (and the default render unchanged) when the front end is off.
    default = run_new_template({}, chart=owui_chart)
    assert ("ConfigMap", "rag-openwebui-proxy") not in default


def test_openwebui_network_policy_admits_only_the_proxy_port_and_nothing_reaches_the_app_port(owui_chart):
    docs = _owui(owui_chart)
    ingress = docs["NetworkPolicy", "rag-openwebui"]["spec"]["ingress"]
    assert [p["port"] for rule in ingress for p in rule["ports"]] == [8443]
    svc = docs["Service", "rag-openwebui"]["spec"]
    assert [p["port"] for p in svc["ports"]] == [8443] and svc["ports"][0]["targetPort"] == "https"
    pod = docs["Deployment", "rag-openwebui"]["spec"]["template"]["spec"]
    owui = next(c for c in pod["containers"] if c["name"] == "openwebui")
    assert "ports" not in owui  # the app listens on 127.0.0.1:8080 only


def test_openwebui_retention_defaults_to_30_days_and_cannot_be_unset(owui_chart):
    """Maintainer decision 2026-10-24: a 30-day window, enforced by the purge
    sidecar. Omitting it keeps 30; an explicit null fails the render."""
    with open(CHART / "values.yaml", encoding="utf-8") as f:
        assert yaml.safe_load(f)["openwebui"]["retentionDays"] == 30
    values = base_values()
    values.update(json.loads(json.dumps(OWUI_ON)))
    del values["openwebui"]["retentionDays"]
    docs = run_new_template(values, chart=owui_chart)
    purge = container_map(docs["Deployment", "rag-openwebui"])["chat-purge"]
    assert purge["args"][:2] == ["--days", "30"]
    values["openwebui"]["retentionDays"] = 7
    purge = container_map(run_new_template(values, chart=owui_chart)["Deployment", "rag-openwebui"])["chat-purge"]
    assert purge["args"][:2] == ["--days", "7"]
    values["openwebui"]["retentionDays"] = None
    r = _helm_template_with_values(values, chart=owui_chart)
    assert r.returncode != 0 and "retentionDays" in r.stderr


def test_openwebui_refuses_to_enable_without_the_repository_docs_pipe(tmp_path):
    """A chart without the Pipe file fails closed when enabled (#1072)."""
    import shutil
    chart = tmp_path / "chart"
    shutil.copytree(CHART, chart)
    (chart / "files" / "openwebui" / "docs_pipe.py").unlink(missing_ok=True)
    values = base_values()
    values.update(json.loads(json.dumps(OWUI_ON)))
    r = _helm_template_with_values(values, chart=chart)
    assert r.returncode != 0 and "docs Pipe (#1072)" in r.stderr


def test_openwebui_chart_pipe_is_byte_identical_to_the_repository_pipe():
    """The #1072 Pipe source (integrations/openwebui/docs_pipe.py) and the copy
    the chart ships (Helm reads only chart files) are both absent or equal; the
    installer's hash check then binds the deployed Function to that source."""
    source = CHART.parent.parent / "integrations" / "openwebui" / "docs_pipe.py"
    copy = CHART / "files" / "openwebui" / "docs_pipe.py"
    assert source.exists() == copy.exists()
    if source.exists():
        assert copy.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("patch,needle", [
    ({"enabled": "true"}, "openwebui"),
    ({"secretName": ""}, "openwebui"),
    ({"secretName": "Bad_Name"}, "openwebui"),
    ({"host": ""}, "openwebui"),
    ({"host": "https://docs.example"}, "openwebui"),
    ({"retentionDays": 0}, "openwebui"),
    ({"retentionDays": 3651}, "openwebui"),
    ({"retentionDays": "30"}, "openwebui"),
    ({"storageSize": "lots"}, "openwebui"),
    ({"oidc": {"providerUrl": "http://idp.example/.well-known/openid-configuration",
               "clientId": "c", "allowedRoles": "r", "adminRoles": "a"}}, "openwebui"),
    ({"oidc": {"providerUrl": "https://idp.example/realms/x", "clientId": "c",
               "allowedRoles": "r", "adminRoles": "a"}}, "openwebui"),
    ({"oidc": {"providerUrl": "https://idp.example/.well-known/openid-configuration",
               "clientId": "", "allowedRoles": "r", "adminRoles": "a"}}, "openwebui"),
    ({"oidc": {"providerUrl": "https://idp.example/.well-known/openid-configuration",
               "clientId": "c", "allowedRoles": "r", "adminRoles": ""}}, "openwebui"),
    ({"idpEgress": {"cidrs": [], "port": 443}}, "openwebui"),
    ({"idpEgress": {"cidrs": ["0.0.0.0/33"], "port": 443}}, "openwebui"),
    ({"idpEgress": {"cidrs": ["idp.example"], "port": 443}}, "openwebui"),
    ({"idpEgress": {"cidrs": ["10.0.0.0/8"], "port": 0}}, "openwebui"),
    ({"replicas": 2}, "openwebui"),
    ({"extra": 1}, "openwebui"),
])
def test_openwebui_schema_refuses_invalid_selection(owui_chart, patch, needle):
    values = base_values()
    values.update(json.loads(json.dumps(OWUI_ON)))
    values["openwebui"].update(patch)
    r = _helm_template_with_values(values, chart=owui_chart)
    assert r.returncode != 0 and needle in r.stderr


def test_openwebui_requires_the_namespace_service_ca(owui_chart):
    values = base_values()
    values.update(json.loads(json.dumps(OWUI_ON)))
    values["route"] = {"enabled": False, "timeoutSeconds": 300, "destinationCA": "  "}
    r = _helm_template_with_values(values, chart=owui_chart)
    assert r.returncode != 0 and "destinationCA" in r.stderr


def test_default_chart_carries_medium_effort_matching_settings():
    """#917: the chart sets the shipped effort explicitly; it must equal the Settings default."""
    from mainframe_rag.config import Settings

    pod = env_map(run_new_template({})["Deployment", "rag-agent"])
    settings = Settings(_env_file=None)
    assert pod["LLM_REASONING_EFFORT_SIMPLE"]["value"] == settings.llm_reasoning_effort_simple == "medium"
    assert pod["LLM_REASONING_EFFORT_COMPLEX"]["value"] == settings.llm_reasoning_effort_complex == "medium"
    # An operator override still wins over the chart default.
    override = env_map(run_new_template(
        {"models": {"reasoning": {"reasoningEffortComplex": "xhigh"}}})["Deployment", "rag-agent"])
    assert override["LLM_REASONING_EFFORT_COMPLEX"]["value"] == "xhigh"
    assert override["LLM_REASONING_EFFORT_SIMPLE"]["value"] == "medium"


def test_chart_ships_request_deadline_270_and_reaches_settings(monkeypatch):
    """#843 D6: chart value -> rendered env -> Settings; Settings default stays 0 (D5)."""
    from mainframe_rag.config import Settings

    pod = env_map(run_new_template({})["Deployment", "rag-agent"])
    assert pod["REQUEST_DEADLINE_S"]["value"] == "270"
    monkeypatch.setenv("REQUEST_DEADLINE_S", pod["REQUEST_DEADLINE_S"]["value"])
    assert Settings(_env_file=None).request_deadline_s == 270.0
    monkeypatch.delenv("REQUEST_DEADLINE_S")
    assert Settings(_env_file=None).request_deadline_s == 0.0
    # Explicit zero is retained (deadline off).
    off = env_map(run_new_template({"requestDeadlineSeconds": 0})["Deployment", "rag-agent"])
    assert off["REQUEST_DEADLINE_S"]["value"] == "0"


def test_chart_request_deadline_stays_strictly_below_the_route_timeout():
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    # Strict: a deadline equal to the route timeout races the route cut (#843).
    assert 0 < values["requestDeadlineSeconds"] < values["route"]["timeoutSeconds"]


@pytest.mark.parametrize("bad", [-1, 3601, "300", 1.5])
def test_direct_chart_refuses_invalid_request_deadline(bad):
    values = base_values()
    values["requestDeadlineSeconds"] = bad
    result = _helm_template_with_values(values)
    assert result.returncode != 0
    assert "requestDeadlineSeconds" in result.stderr


def test_chart_ships_admission_4_4_30_and_reaches_settings(monkeypatch):
    """#843: default render carries 4/4/30 into Settings; the library defaults stay 0 (off)."""
    from mainframe_rag.config import Settings

    names = ("REQUEST_MAX_CONCURRENT", "REQUEST_QUEUE_MAX", "REQUEST_QUEUE_WAIT_S")
    pod = env_map(run_new_template({})["Deployment", "rag-agent"])
    assert [pod[n]["value"] for n in names] == ["4", "4", "30"]
    for n in names:
        monkeypatch.setenv(n, pod[n]["value"])
    shipped = Settings(_env_file=None)
    assert (shipped.request_max_concurrent, shipped.request_queue_max, shipped.request_queue_wait_s) == (4, 4, 30.0)
    for n in names:
        monkeypatch.delenv(n)
    bare = Settings(_env_file=None)
    assert (bare.request_max_concurrent, bare.request_queue_max) == (0, 0)


def test_chart_admission_overrides_propagate_and_zero_is_retained():
    """#374: an operator value wins over the chart default; explicit zero is retained."""
    chosen = env_map(run_new_template(
        {"requestMaxConcurrent": 8, "requestQueueMax": 3, "requestQueueWaitSeconds": 2.5},
    )["Deployment", "rag-agent"])
    assert chosen["REQUEST_MAX_CONCURRENT"]["value"] == "8"
    assert chosen["REQUEST_QUEUE_MAX"]["value"] == "3"
    assert chosen["REQUEST_QUEUE_WAIT_S"]["value"] == "2.5"
    off = env_map(run_new_template(
        {"requestMaxConcurrent": 0, "requestQueueMax": 0, "requestQueueWaitSeconds": 0},
    )["Deployment", "rag-agent"])
    assert [off[n]["value"] for n in ("REQUEST_MAX_CONCURRENT", "REQUEST_QUEUE_MAX", "REQUEST_QUEUE_WAIT_S")] == ["0"] * 3


def test_direct_chart_refuses_zero_queue_wait_while_admission_is_on():
    values = base_values()
    values["requestMaxConcurrent"] = 4
    values["requestQueueWaitSeconds"] = 0
    result = _helm_template_with_values(values)
    assert result.returncode != 0
    assert "requestQueueWaitSeconds" in result.stderr


def test_direct_chart_requires_queue_wait_when_override_omits_it_while_admission_is_on():
    """#588: a null override drops the key (Helm deletes it from the merge), so the
    schema itself must require it whenever requestMaxConcurrent > 0."""
    values = base_values()
    values["requestMaxConcurrent"] = 4
    values["requestQueueWaitSeconds"] = None
    result = _helm_template_with_values(values)
    assert result.returncode != 0
    assert "requestQueueWaitSeconds" in result.stderr
    values["requestMaxConcurrent"] = 0
    off = _helm_template_with_values(values)
    assert off.returncode == 0, off.stderr


@pytest.mark.parametrize("key,bad", [
    ("requestMaxConcurrent", -1), ("requestMaxConcurrent", 1001), ("requestMaxConcurrent", "2"),
    ("requestQueueMax", 1.5), ("requestQueueWaitSeconds", 301), ("requestQueueWaitSeconds", -1),
])
def test_direct_chart_refuses_invalid_admission_values(key, bad):
    values = base_values()
    values[key] = bad
    result = _helm_template_with_values(values)
    assert result.returncode != 0
    assert key in result.stderr


# OpenObserve is the only trace backend (issue #956, ADR-0006). Tags in
# base_values; the digest pin is enforced at pack/load (#960).
METRICS_ON = {"metrics": {"enabled": True}}


def test_default_render_is_openobserve_only():
    docs = run_new_template({})
    assert not [k for k in docs if "jaeger" in k[1]]
    job = run_new_job_only({"ingest": {"corpusPVC": "c"}})["Job", "ingest"]
    assert ingest_env_map(job)["OTEL_EXPORTER_OTLP_HEADERS"]["valueFrom"]["secretKeyRef"] == {
        "name": "openobserve-credentials", "key": "otlp-headers",
    }
    assert not [k for k in docs if k[0] in ("Route", "Ingress")]
    assert {k for k in docs if "openobserve" in k[1]} == {
        ("Deployment", "openobserve"),
        ("Service", "openobserve"),
        ("PersistentVolumeClaim", "openobserve-data"),
        ("NetworkPolicy", "openobserve-egress-deny"),
    }
    # Metrics are off by default, so no collector objects.
    assert not [k for k in docs if "otelcol" in k[1]]
    env = env_map(docs["Deployment", "rag-agent"])
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://openobserve:5080/api/default"
    assert env["OTEL_EXPORTER_OTLP_HEADERS"]["valueFrom"]["secretKeyRef"] == {
        "name": "openobserve-credentials", "key": "otlp-headers",
    }


def test_openobserve_vanilla_kubernetes_identity_is_opt_in():
    """The OpenObserve image runs as root (User 0): restricted-v2 assigns a UID on
    OpenShift, plain Kubernetes (Kind) needs one pinned non-root id for the pod,
    with fsGroup so /data is writable. otelcol's image already runs as 10001, so
    it never pins an identity."""
    default = run_new_template({})
    pod = default["Deployment", "openobserve"]["spec"]["template"]["spec"]["securityContext"]
    assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(pod)
    pinned = run_new_template({"openobserve": {"runAsId": 10001}})
    pod = pinned["Deployment", "openobserve"]["spec"]["template"]["spec"]
    assert pod["securityContext"] == {
        "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"}}
    c = pod["containers"][0]["securityContext"]
    assert c == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                 "capabilities": {"drop": ["ALL"]}}
    col = run_new_template({**METRICS_ON, "openobserve": {"runAsId": 10001}})["Deployment", "otelcol"]["spec"]["template"]["spec"]
    assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(col["securityContext"])
    assert col["securityContext"]["runAsNonRoot"] is True
    for bad in (0, 70000, "x"):
        assert _helm_template_with_values({**base_values(), "openobserve": {"runAsId": bad}}).returncode != 0


def test_openobserve_backend_contract():
    docs = run_new_template({})
    dep = docs["Deployment", "openobserve"]
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"]["type"] == "Recreate"
    pod = dep["spec"]["template"]["spec"]
    assert pod["securityContext"] == {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
    c = pod["containers"][0]
    assert c["image"] == "reg.internal/openobserve/openobserve:v1.1.2"
    assert c["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert "runAsUser" not in c["securityContext"] and "runAsUser" not in pod["securityContext"]
    for probe in ("readinessProbe", "livenessProbe"):
        assert c[probe]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert c["livenessProbe"]["failureThreshold"] >= 3
    env = {e["name"]: e for e in c["env"]}
    assert {n: e["value"] for n, e in env.items() if "value" in e} == {
        "ZO_LOCAL_MODE": "true",
        "ZO_LOCAL_MODE_STORAGE": "disk",
        "ZO_DATA_DIR": "/data",
        "ZO_TELEMETRY": "false",
        "ZO_MODEL_PRICING_ENABLED": "false",
        "ZO_MMDB_DISABLE_DOWNLOAD": "true",
    }
    for name, key in (("ZO_ROOT_USER_EMAIL", "root-user-email"), ("ZO_ROOT_USER_PASSWORD", "root-user-password")):
        assert "value" not in env[name]
        assert env[name]["valueFrom"]["secretKeyRef"] == {"name": "openobserve-credentials", "key": key}
    svc = docs["Service", "openobserve"]
    assert svc["spec"]["type"] == "ClusterIP"
    assert [p["port"] for p in svc["spec"]["ports"]] == [5080]
    pvc = docs["PersistentVolumeClaim", "openobserve-data"]
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["storageClassName"] == "standard"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_openobserve_storage_size_is_configurable():
    docs = run_new_template({"openobserve": {"storage": {"size": "20Gi"}}})
    assert docs["PersistentVolumeClaim", "openobserve-data"]["spec"]["resources"]["requests"]["storage"] == "20Gi"


def test_openobserve_egress_policy_denies_all():
    spec = run_new_template({})["NetworkPolicy", "openobserve-egress-deny"]["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app": "openobserve"}}
    assert spec["policyTypes"] == ["Egress"]
    assert "egress" not in spec and "ingress" not in spec


def test_external_endpoint_is_the_generic_otlp_escape_hatch():
    docs = run_new_template({"tracing": {"endpoint": "http://collector.platform:4318"}, **METRICS_ON})
    assert not [k for k in docs if "openobserve" in k[1] or "otelcol" in k[1] or "jaeger" in k[1]]
    env = env_map(docs["Deployment", "rag-agent"])
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://collector.platform:4318"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env


def test_tracing_off_renders_no_backend_and_needs_no_openobserve_values():
    values = base_values()
    values["tracing"]["enabled"] = False
    del values["openobserve"]
    result = _helm_template_with_values(values)
    assert result.returncode == 0, result.stderr
    kinds = [d["metadata"]["name"] for d in yaml.safe_load_all(result.stdout) if d]
    assert not [n for n in kinds if "openobserve" in n or "otelcol" in n]
    docs = run_new_template({"tracing": {"enabled": False}, **METRICS_ON})
    assert not [k for k in docs if "openobserve" in k[1] or "otelcol" in k[1]]
    assert env_map(docs["Deployment", "rag-agent"])["OTEL_EXPORTER_OTLP_ENDPOINT"].get("value") in (None, "")


def _values_with(**patch):
    values = base_values()
    values["metrics"] = {"enabled": True}
    for path, val in patch.items():
        node = values
        *parents, leaf = path.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        if val is None:
            node.pop(leaf, None)
        else:
            node[leaf] = val
    return values


@pytest.mark.parametrize("patch,needle", [
    ({"images.openobserve.tag": ""}, "tag"),
    ({"images.otelcol.tag": ""}, "tag"),
    ({"openobserve.credentialsSecretName": ""}, "credentialsSecretName is required"),
])
def test_openobserve_and_collector_fail_closed(patch, needle):
    result = _helm_template_with_values(_values_with(**patch))
    assert result.returncode != 0
    assert needle in result.stderr


def test_collector_forced_on_without_prerequisites_fails():
    for patch in (
        {"metrics.enabled": False, "metrics.collector": {"enabled": True}},
        {"tracing.endpoint": "http://collector.platform:4318", "metrics.collector": {"enabled": True}},
    ):
        result = _helm_template_with_values(_values_with(**patch))
        assert result.returncode != 0
        assert "metrics.collector.enabled=true requires" in result.stderr


def test_collector_can_be_switched_off_while_metrics_stay_on():
    docs = run_new_template({"metrics": {"enabled": True, "collector": {"enabled": False}}})
    assert not [k for k in docs if "otelcol" in k[1]]
    assert env_map(docs["Deployment", "rag-agent"])["METRICS_ENABLED"]["value"] == "true"


def test_collector_contract():
    docs = run_new_template(METRICS_ON)
    assert {k for k in docs if "otelcol" in k[1]} == {
        ("Deployment", "otelcol"),
        ("ConfigMap", "otelcol-config"),
        ("NetworkPolicy", "otelcol-egress"),
    }
    # Stateless and unexposed: no Service, no PVC beyond the existing claims.
    assert ("Service", "otelcol") not in docs
    assert "otelcol" not in "".join(k[1] for k in docs if k[0] == "PersistentVolumeClaim")
    dep = docs["Deployment", "otelcol"]
    pod = dep["spec"]["template"]["spec"]
    assert pod["securityContext"] == {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
    assert _hardening_violations(pod) == []
    (c,) = pod["containers"]
    assert c["image"] == "reg.internal/otel/opentelemetry-collector-contrib:0.160.0"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["env"] == [
        {"name": "O2_AUTH", "valueFrom": {"secretKeyRef": {"name": "openobserve-credentials", "key": "metrics-auth"}}}
    ]
    assert {v["mountPath"] for v in c["volumeMounts"] if not v.get("readOnly")} == {"/tmp"}
    assert not [v for v in pod["volumes"] if "persistentVolumeClaim" in v]
    cfg_text = docs["ConfigMap", "otelcol-config"]["data"]["config.yaml"]
    cfg = yaml.safe_load(cfg_text)
    (job,) = cfg["receivers"]["prometheus"]["config"]["scrape_configs"]
    assert job["metrics_path"] == "/metrics"
    assert job["static_configs"] == [{"targets": ["rag-agent:8080"]}]
    exp = cfg["exporters"]["otlp_http"]
    assert exp["endpoint"] == "http://openobserve:5080/api/default"
    assert exp["headers"] == {"Authorization": "${env:O2_AUTH}"}
    assert cfg["service"]["pipelines"] == {
        "metrics": {"receivers": ["prometheus"], "exporters": ["otlp_http"]}
    }
    assert "openobserve-credentials" not in cfg_text and "Basic" not in cfg_text


def test_collector_egress_allows_only_openobserve_agent_and_dns():
    spec = run_new_template(METRICS_ON)["NetworkPolicy", "otelcol-egress"]["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app": "otelcol"}}
    assert spec["policyTypes"] == ["Egress"]
    assert "ingress" not in spec
    rules = spec["egress"]
    assert len(rules) == 3
    assert rules[0] == {
        "to": [{"podSelector": {"matchLabels": {"app": "openobserve"}}}],
        "ports": [{"protocol": "TCP", "port": 5080}],
    }
    assert rules[1] == {
        "to": [{"podSelector": {"matchLabels": {"app": "rag-agent"}}}],
        "ports": [{"protocol": "TCP", "port": 8080}],
    }
    (dns_peer,) = rules[2]["to"]
    assert set(dns_peer) == {"namespaceSelector"}
    (expr,) = dns_peer["namespaceSelector"]["matchExpressions"]
    assert expr["values"] == ["openshift-dns", "kube-system"]
    assert {p["port"] for p in rules[2]["ports"]} == {53, 5353}
    # No rule has an ipBlock or an empty (everything) peer.
    assert all("ipBlock" not in peer and peer for r in rules for peer in r["to"])


def test_agent_ingress_policy_admits_the_collector_scrape():
    docs = run_new_template(METRICS_ON)
    first = docs[POLICY_KEY]["spec"]["ingress"][0]
    assert first["from"] == [{"podSelector": {}}]
    assert {"protocol": "TCP", "port": 8080} in first["ports"]


@pytest.mark.parametrize("path,bad", [
    (("idpEgress", "port"), None), (("idpEgress", "port"), ""), (("idpEgress", "port"), " "),
    (("idpEgress", "port"), 0), (("idpEgress", "port"), "443"),
    (("oidc", "groupClaim"), None), (("oidc", "groupClaim"), ""), (("oidc", "groupClaim"), " "),
    (("oidc", "rolesClaim"), None), (("oidc", "rolesClaim"), ""), (("oidc", "rolesClaim"), " "),
    (("oidc", "emailClaim"), None), (("oidc", "emailClaim"), ""), (("oidc", "emailClaim"), " "),
    (("oidc", "usernameClaim"), None), (("oidc", "usernameClaim"), ""),
    (("oidc", "scopes"), None), (("oidc", "scopes"), ""), (("oidc", "scopes"), " "),
    (("oidc", "scopes"), "email profile"), (("oidc", "scopes"), "openid,email"),
    (("oidc", "scopes"), "openid  email"), (("oidc", "scopes"), "openidx email"),
])
def test_openwebui_refuses_null_empty_or_blank_port_and_claims(owui_chart, path, bad):
    """B2: a null IdP port would render an egress rule open to every TCP port, and
    an empty claim a half-configured sign-in; both must refuse, not render."""
    values = _bare_default_values()
    values.update(json.loads(json.dumps(OWUI_ON)))
    values["openwebui"][path[0]][path[1]] = bad
    r = _helm_template_with_values(values, chart=owui_chart)
    assert r.returncode != 0, r.stdout[:200]
    assert "rag-openwebui" not in r.stdout


def test_openwebui_adfs_scopes_and_identity_claims_reach_the_pod(owui_chart):
    """AD FS (docs/deploy.md): custom claims reach the ID token only with
    allatclaims, and the e-mail is usually upn. Scopes and the e-mail and
    username claims are site inputs that reach Open WebUI verbatim."""
    oidc = {**OWUI_ON["openwebui"]["oidc"], "scopes": "openid profile email allatclaims",
            "emailClaim": "upn", "usernameClaim": "unique_name"}
    docs = _owui(owui_chart, {"openwebui": {**OWUI_ON["openwebui"], "oidc": oidc}})
    env = env_map(docs["Deployment", "rag-openwebui"], "openwebui")
    assert {k: env[k]["value"] for k in ("OAUTH_SCOPES", "OAUTH_EMAIL_CLAIM", "OAUTH_USERNAME_CLAIM")} == {
        "OAUTH_SCOPES": "openid profile email allatclaims", "OAUTH_EMAIL_CLAIM": "upn",
        "OAUTH_USERNAME_CLAIM": "unique_name"}
    assert set(env) == OWUI_ENV_NAMES


ROLE_BASELINE_REFUSAL = (
    "openwebui.oidc.allowedRoles and adminRoles must not contain * or the baseline role signed-in")


@pytest.mark.parametrize("key,bad", [
    ("allowedRoles", "*"), ("allowedRoles", "staff,*"), ("allowedRoles", "signed-in"),
    ("allowedRoles", "staff,signed-in"), ("adminRoles", "signed-in"), ("adminRoles", "docs-admins,*"),
])
def test_openwebui_refuses_role_lists_that_would_admit_every_signed_in_user(owui_chart, key, bad):
    """Review #1083 B2: revocation through the IdP needs a roles claim on every
    sign-in, so AD FS issues the baseline value signed-in to every user
    (docs/deploy.md). A role list containing it, or *, would admit everyone the
    IdP authenticates and make removal from the role groups revoke nothing."""
    values = base_values()
    values.update(json.loads(json.dumps(OWUI_ON)))
    values["openwebui"]["oidc"][key] = bad
    r = _helm_template_with_values(values, chart=owui_chart)
    assert r.returncode != 0 and ROLE_BASELINE_REFUSAL in r.stderr
    assert "rag-openwebui" not in r.stdout


def test_openwebui_networkpolicy_egress_rules_always_carry_a_port(owui_chart):
    policy = _owui(owui_chart)["NetworkPolicy", "rag-openwebui"]
    for rule in policy["spec"]["egress"]:
        assert rule.get("ports") and all("port" in p for p in rule["ports"])
