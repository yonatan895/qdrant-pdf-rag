"""First-party Helm render contracts: independent expected behavior.

The retired differential oracle is replaced by explicit expectations for
agent, OAuth/Route, Jaeger, ServiceMonitor and explicit ingest profiles.
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


def test_agent_inventory_matches():
    """Agent Service/Deployment inventory is present."""
    new = run_new_template({})
    assert {("Service", "rag-agent"), ("Deployment", "rag-agent")} <= set(new)

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
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4318",
        "IMAGE_SHA": IMAGE_SHA,
        "OTEL_DEPLOYMENT_ENVIRONMENT": "",
        "METRICS_ENABLED": "false",
        "UI_ENABLED": "true",
        "CHAT_CONDENSE_ENABLED": "false",
        "RERANK_ENABLED": "false",
        "RERANK_BASE_URL": "",
        "RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
        "RERANK_ENDPOINT_ORDER": "score_first",
    }
    assert {key: env["value"] for key, env in new_env.items() if "value" in env} == expected_scalars
    assert set(new_env) == set(expected_scalars) | {"QDRANT_API_KEY"}
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
    assert not [k for k in new if k[0] in ("Deployment", "Service") and k[1] == "jaeger"]

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

def test_gateway_ca_contract():
    new = run_new_template({"gateway": {"caConfigMapName": "gw-ca"}})
    for docs in (new,):
        dep = docs["Deployment", "rag-agent"]
        agent = container_map(dep)["agent"]
        ssl = next(e for e in agent["env"] if e["name"] == "SSL_CERT_FILE")
        assert ssl["value"] == "/etc/gateway-ca/ca-bundle.crt"
        assert {"name": "gateway-ca", "mountPath": "/etc/gateway-ca", "readOnly": True} in agent[
            "volumeMounts"
        ]
        vols = dep["spec"]["template"]["spec"]["volumes"]
        ca = next(v for v in vols if v["name"] == "gateway-ca")
        assert ca["configMap"]["name"] == "gw-ca"

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

def test_jaeger_contract():
    new = run_new_template({"pullSecret": {"name": "ghcr-pull"}})
    for key in (
        ("Deployment", "jaeger"),
        ("Service", "jaeger"),
        ("PersistentVolumeClaim", "jaeger-badger"),
        ("ConfigMap", "jaeger-config"),
    ):
        assert key in new, f"missing new {key}"
    new_dep = new["Deployment", "jaeger"]
    new_c = next(
        c for c in new_dep["spec"]["template"]["spec"]["containers"] if c["name"] == "jaeger"
    )
    assert new_c["image"] == "reg.internal/jaegertracing/jaeger:v2.20.0"
    assert new_c["args"] == ["--config", "/etc/jaeger/config-badger.yaml"]
    assert new_dep["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    assert new_dep["spec"]["strategy"] == {"type": "Recreate", "rollingUpdate": None}
    new_svc = new["Service", "jaeger"]
    assert new_svc["spec"]["type"] == "ClusterIP"
    new_pvc = new["PersistentVolumeClaim", "jaeger-badger"]
    assert new_pvc["spec"]["storageClassName"] == "standard"
    assert new_pvc["spec"]["resources"]["requests"] == {"storage": "10Gi"}
    assert new_c["ports"] == [
        {"containerPort": 4318, "name": "otlp-http"},
        {"containerPort": 16686, "name": "ui"},
    ]
    assert new_c["readinessProbe"] == {
        "httpGet": {"path": "/", "port": "ui"},
        "initialDelaySeconds": 5,
        "periodSeconds": 10,
    }
    assert new_c["resources"] == {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }
    assert new_c["volumeMounts"] == [
        {"name": "config", "mountPath": "/etc/jaeger", "readOnly": True},
        {"name": "badger", "mountPath": "/badger"},
    ]
    assert new_svc["spec"]["ports"] == [
        {"name": "otlp-http", "port": 4318, "targetPort": "otlp-http"},
        {"name": "ui", "port": 16686, "targetPort": "ui"},
    ]
    new_cfg = yaml.safe_load(new["ConfigMap", "jaeger-config"]["data"]["config-badger.yaml"])
    assert new_cfg["service"]["pipelines"]["traces"] == {
        "receivers": ["otlp"],
        "processors": ["batch"],
        "exporters": ["jaeger_storage_exporter"],
    }
    assert new_cfg["service"]["extensions"] == ["jaeger_storage", "jaeger_query", "healthcheckv2"]
    assert new_cfg["extensions"]["jaeger_storage"]["backends"]["badger_store"]["badger"] == {
        "directories": {"keys": "/badger/keys", "values": "/badger/values"},
        "ephemeral": False,
        "ttl": {"spans": "336h"},
        "metrics_update_interval": "10s",
    }
    assert new_cfg["extensions"]["jaeger_query"]["storage"] == {"traces": "badger_store"}
    assert new_cfg["receivers"] == {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}}
    assert new_cfg["exporters"] == {"jaeger_storage_exporter": {"trace_storage": "badger_store"}}
    assert "4317" not in yaml.safe_dump(new_cfg)
    assert (
        env_map(new["Deployment", "rag-agent"])["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"]
        == "http://jaeger:4318"
    )

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
        "INGEST_ALIAS_PUBLISH": "false",
        "EMBED_BASE_URL": "http://vllm:8000/v1",
        "EMBED_MODEL": "embed-model",
        "DENSE_DIM": "768",
        "EMBED_MODEL_REVISION": "rev-1",
        "IMAGE_SHA": IMAGE_SHA,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4318",
        "OTEL_SERVICE_NAME": "mainframe-rag-ingest",
        "OTEL_DEPLOYMENT_ENVIRONMENT": "",
        "INGEST_WORKERS": "4",
        "CONTEXTUAL_EMBED_ENABLED": "false",
        "CONTEXT_LLM_BASE_URL": "",
        "CONTEXT_LLM_MODEL": "",
    }
    assert {key: env["value"] for key, env in new_env.items() if "value" in env} == expected_scalars
    assert set(new_env) == set(expected_scalars) | {"QDRANT_API_KEY"}
    assert new_env["QDRANT_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "qdrant-apikey",
        "key": "api-key",
    }
    assert new_env["QDRANT_COLLECTION"]["value"] == "mainframe_manuals"
    assert new_env["QDRANT_SHARD_NUMBER"]["value"] == "6"
    assert new_env["INGEST_ALIAS_PUBLISH"]["value"] == "false"
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
                "aliasPublish": True,
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
        assert env["INGEST_ALIAS_PUBLISH"]["value"] == "true"
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

def test_ingest_not_owned_by_default():
    new = run_new_template({})
    assert ("Job", "ingest") not in new
    raw = _helm_template_with_values(base_values())
    assert raw.returncode == 0, raw.stderr
    assert "helm.sh/hook" not in raw.stdout
    assert "kind: Job" not in raw.stdout

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
        ({"gateway": {"apiKeySecretName": "Bad_Name!"}}, "apiKeySecretName"),
        ({"models": {"rerank": {"endpointOrder": "nope"}}}, "endpointOrder"),
        ({"metrics": {"enabled": "false"}}, "metrics"),
        ({"ui": {"enabled": "false"}}, "ui"),
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
        "ingest-work-pvc.yaml",
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

def test_jaeger_claim_is_retained_across_release_removal():
    docs = run_new_template({})
    claim = docs["PersistentVolumeClaim", "jaeger-badger"]
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    disabled = run_new_template({"tracing": {"enabled": False}})
    assert not any((name.startswith("jaeger") for _, name in disabled))

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


def test_explicit_followup_condensation_reaches_agent():
    deployment = run_new_template({"models": {"reasoning": {"condenseEnabled": True}}})["Deployment", "rag-agent"]
    assert env_map(deployment)["CHAT_CONDENSE_ENABLED"]["value"] == "true"


def test_ui_disabled_renders_false_and_keeps_agent_contract():
    """Issue #479: ui.enabled=false reaches the Deployment; probes,
    selectors and read-only credentials are unchanged."""
    new = run_new_template({"ui": {"enabled": False}})
    dep = new["Deployment", "rag-agent"]
    new_env = env_map(dep)
    assert new_env["UI_ENABLED"]["value"] == "false"
    agent = container_map(dep)["agent"]
    assert agent["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert agent["livenessProbe"]["httpGet"] == {"path": "/livez", "port": "http"}
    assert new_env["QDRANT_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "key": "read-only-api-key",
        "name": "qdrant-apikey",
    }


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
