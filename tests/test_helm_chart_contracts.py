"""First-party Helm render contracts: independent expected behavior.

The retired differential oracle is replaced by explicit expectations for
agent, OAuth/Route, OpenObserve, collector, ServiceMonitor and explicit ingest profiles.
Operator-to-render round trips also live in test_map_values and airgap suites.
"""
import json

import pytest
import yaml

from tests.helpers_helm import (
    CHART,
    FAKE_CA,
    IMAGE_SHA,
    _helm_template_with_values,
    base_values,
    container_map,
    env_map,
    ingest_env_map,
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
