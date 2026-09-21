#!/usr/bin/env python3
"""Map resolved operator environment to first-party Helm release values.

Issue #448: generated deployment and explicit ingest values.

Reads the ALREADY-RESOLVED environment (common.sh owns alias, precedence,
and file loading; this script never parses airgap.env and never reorders
precedence) and writes dist/mainframe-rag-release-values.yaml. Callers must
export the input set first: sourced files (collection-policy preset,
airgap.env) leave plain assignments shell-local, invisible to child
processes (map_app_values performs this export). The YAML is
emitted by a small deterministic stdlib-only serializer (no pyyaml
dependency, bastion-friendly): strings via JSON double-quoting (valid YAML,
byte-exact round-trip including spaces, pipes, ampersands and newlines),
integers/booleans native, fixed key order.

Fail-closed: exit 1 with "FAIL: ..." on any missing/invalid selected input,
mirroring the shell preflight rules. Never prints secret values.

Invoked by deploy.sh/ingest.sh and the identical dry-run/CI rendering paths.
"""

from __future__ import annotations

import json
import os
import re
import sys
import typing
from pathlib import Path

DEFAULT_OUT = "dist/mainframe-rag-release-values.yaml"


def die(msg: str) -> typing.NoReturn:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def nonempty(name: str, err: str) -> str:
    val = env(name)
    if not val:
        die(f"{name} {err}")
    return val


def positive_int(name: str, raw: str) -> int:
    if not re.fullmatch(r"[0-9]+", raw or "") or int(raw) < 1:
        die(f"{name} must be a positive integer (got {raw!r})")
    return int(raw)


def strict_bool(name: str, raw: str, default: bool) -> bool:
    """Mirror bool_flag in common.sh: unset keeps default, else strict."""
    if raw == "":
        return default
    low = raw.lower()
    if low in ("true", "1", "yes"):
        return True
    if low in ("false", "0", "no"):
        return False
    die(f"{name} must be true/false (got {raw!r})")


def lenient_bool(raw: str) -> bool:
    """Operator flags without shell validation (rerank/metrics semantics).

    Only truthy spellings enable; everything else keeps current effective
    behavior (the agent treats any non-"true" rendering as off).
    """
    return raw.lower() in ("true", "1", "yes")


def resolve_otel() -> tuple[bool, str]:
    """Mirror resolve_otel_endpoint in common.sh exactly."""
    raw = env("OTEL_EXPORTER_OTLP_ENDPOINT")
    # Prefer values already resolved by the calling shell stage.
    if env("OTEL_TRACING_ENABLED") in ("0", "1") and "OTEL_ENDPOINT_RESOLVED" in os.environ:
        return env("OTEL_TRACING_ENABLED") == "1", env("OTEL_ENDPOINT_RESOLVED")
    if raw == "":
        return True, "http://jaeger:4318"
    if raw.lower() in ("off", "none", "false", "0"):
        return False, ""
    if raw.startswith(("http://", "https://")):
        return True, raw
    die(f"OTEL_EXPORTER_OTLP_ENDPOINT must be http(s) or off/none/false/0, got {raw!r}")


def split_retire(raw: str) -> list[str]:
    """Mirror the INGEST_RETIRE_DOCS splitting in ingest.sh (shapes stay shell-owned)."""
    entries: list[str] = []
    for chunk in re.split(r"[,\n]", raw):
        entry = chunk.strip(" \t")
        if entry:
            entries.append(entry)
    return entries


def emit_yaml(node, indent: int = 0) -> list[str]:
    """Deterministic restricted YAML: mappings (insertion-ordered), lists,
    strings (JSON double-quoted), integers, booleans. Empty mappings render
    as {} so helm schema required-object checks still apply."""
    pad = "  " * indent
    if isinstance(node, dict):
        if not node:
            return [pad + "{}"]
        lines: list[str] = []
        for key, val in node.items():
            if isinstance(val, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.extend(emit_yaml(val, indent + 1))
            else:
                lines.append(f"{pad}{key}: {emit_scalar(val)}")
        return lines
    if isinstance(node, list):
        if not node:
            return [pad + "[]"]
        lines = []
        for item in node:
            if isinstance(item, (dict, list)):
                lines.append(pad + "-")
                lines.extend(emit_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}- {emit_scalar(item)}")
        return lines
    return [pad + emit_scalar(node)]


def emit_scalar(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    return json.dumps(val, ensure_ascii=True)


def build_values(deploy_only: bool = False) -> dict:
    internal_registry = env("INTERNAL_REGISTRY") or env("REGISTRY_INTERNAL")
    if not internal_registry:
        die("INTERNAL_REGISTRY is not set (copy airgap.env.example to airgap.env)")
    namespace = env("NAMESPACE") or env("OPENSHIFT_NAMESPACE") or "mainframe-rag"
    if len(namespace) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", namespace):
        die("NAMESPACE must be a DNS label of at most 63 characters")
    qdrant_release = env("QDRANT_RELEASE") or "qdrant"
    image_sha = env("IMAGE_SHA")
    if image_sha in ("", "HEAD"):
        die("IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)")
    if not re.fullmatch(r"[0-9a-f]{40}", image_sha):
        die(f"IMAGE_SHA must be a full git SHA (got {image_sha!r})")

    embed_base = env("EMBED_BASE_URL")
    if not embed_base and env("VLLM_BASE_URL"):
        embed_base = re.sub(r"(/v1)?/*$", "", env("VLLM_BASE_URL")) + "/v1"
    for key in ("EMBED_MODEL", "EMBED_MODEL_REVISION"):
        nonempty(key, "is required for deploy/ingest (see airgap.env.example)")
    if not re.search(r"\S", env("EMBED_MODEL_REVISION")):
        die("EMBED_MODEL_REVISION must be a non-blank immutable revision")
    dimension = positive_int("DENSE_DIM", env("DENSE_DIM"))

    tracing_enabled, otel_endpoint = resolve_otel()

    rerank_enabled = lenient_bool(env("RERANK_ENABLED"))
    metrics_enabled = env("METRICS_ENABLED") == "true"

    agent_route = env("AGENT_ROUTE") == "true"
    route_ca = ""
    if agent_route:
        ca_file = env("ROUTE_DESTINATION_CA_FILE")
        if not ca_file:
            die(
                "AGENT_ROUTE=true needs ROUTE_DESTINATION_CA_FILE pointing at the "
                "namespace service-CA bundle (Route rendering; "
                "route-off dry-run needs no CA)"
            )
        try:
            route_ca = Path(ca_file).read_text(encoding="utf-8")
        except OSError as exc:
            die(f"ROUTE_DESTINATION_CA_FILE is unreadable: {exc}")
        if not route_ca.strip():
            die("ROUTE_DESTINATION_CA_FILE is blank")

    storage_class = env("STORAGE_CLASS")
    if not storage_class:
        die("STORAGE_CLASS is required (RWO block; NFS is refused)")

    corpus_pvc = env("CORPUS_PVC")
    # The deploy stage never owns the one-shot Job (D3): callers pass
    # --without-ingest-job so steady-state values keep ingest.enabled=false
    # even when CORPUS_PVC is set for a later explicit ingest operation.
    ingest_enabled = bool(corpus_pvc) and not deploy_only
    ingest_block: dict = {
        "enabled": False,
        "corpusPVC": "",
        "workers": 4,
        "workSize": env("INGEST_WORK_SIZE") or "100Gi",
        "aliasPublish": False,
        "reingest": False,
        "retireDocs": [],
        "contextualEnabled": False,
        "contextLlmBaseUrl": "",
        "contextLlmModel": "",
        "collectionPolicy": {"shardNumber": 6, "replicationFactor": 3, "writeConsistencyFactor": 2},
        "resources": {
            "requests": {"cpu": "4", "memory": "8Gi"},
            "limits": {"cpu": "16", "memory": "32Gi"},
        },
    }
    if ingest_enabled:
        qdrant_policy = {
            "shardNumber": positive_int("QDRANT_SHARD_NUMBER", env("QDRANT_SHARD_NUMBER")),
            "replicationFactor": positive_int(
                "QDRANT_REPLICATION_FACTOR", env("QDRANT_REPLICATION_FACTOR")
            ),
            "writeConsistencyFactor": positive_int(
                "QDRANT_WRITE_CONSISTENCY_FACTOR", env("QDRANT_WRITE_CONSISTENCY_FACTOR")
            ),
        }
    else:
        # Parser defaults; unused while ingest.enabled is false (schema still
        # requires the complete object, W<=RF stays shell-owned).
        qdrant_policy = {"shardNumber": 6, "replicationFactor": 3, "writeConsistencyFactor": 2}
    if ingest_enabled:
        ingest_block = {
            "enabled": True,
            "workSize": env("INGEST_WORK_SIZE") or "100Gi",
            "corpusPVC": corpus_pvc,
            "workers": positive_int("INGEST_WORKERS", env("INGEST_WORKERS") or "4"),
            "aliasPublish": strict_bool("INGEST_ALIAS_PUBLISH", env("INGEST_ALIAS_PUBLISH"), False),
            "reingest": strict_bool("INGEST_REINGEST", env("INGEST_REINGEST"), False),
            "retireDocs": split_retire(env("INGEST_RETIRE_DOCS")),
            "contextualEnabled": strict_bool(
                "CONTEXTUAL_EMBED_ENABLED", env("CONTEXTUAL_EMBED_ENABLED"), False
            ),
            "contextLlmBaseUrl": env("CONTEXT_LLM_BASE_URL"),
            "contextLlmModel": env("CONTEXT_LLM_MODEL"),
            "collectionPolicy": qdrant_policy,
            "resources": {
                "requests": {"cpu": "4", "memory": "8Gi"},
                "limits": {"cpu": "16", "memory": "32Gi"},
            },
        }
        if ingest_block["retireDocs"] and not ingest_block["aliasPublish"]:
            die("INGEST_RETIRE_DOCS requires INGEST_ALIAS_PUBLISH=true")

    reasoning_model = env("LLM_MODEL_REASONING")
    if reasoning_model:
        reasoning_base = nonempty("LLM_BASE_URL", "is required when LLM_MODEL_REASONING is set")
    else:
        reasoning_base = ""
        reasoning_model = ""

    return {
        "replicaCount": 2,
        "images": {
            "agent": {
                "repository": f"{internal_registry}/qdrant-pdf-rag-agent",
                "tag": image_sha,
                "pullPolicy": "IfNotPresent",
            },
            "ingest": {
                "repository": f"{internal_registry}/qdrant-pdf-rag-ingest",
                "tag": image_sha,
                "pullPolicy": "IfNotPresent",
            },
            "jaeger": {
                "repository": f"{internal_registry}/jaegertracing/jaeger",
                "tag": "v2.20.0",
                "pullPolicy": "IfNotPresent",
            },
            "oauthProxy": {
                "repository": f"{internal_registry}/openshift4/ose-oauth-proxy",
                "tag": "v4.14",
                "pullPolicy": "IfNotPresent",
            },
        },
        "qdrantRelease": qdrant_release,
        "models": {
            "embedding": {
                "baseUrl": embed_base,
                "model": env("EMBED_MODEL"),
                "revision": env("EMBED_MODEL_REVISION"),
                "dimension": dimension,
            },
            "reasoning": {"baseUrl": reasoning_base, "model": reasoning_model,
                          "condenseEnabled": strict_bool("CHAT_CONDENSE_ENABLED",
                                                         env("CHAT_CONDENSE_ENABLED"), False)},
            "rerank": {
                "enabled": rerank_enabled,
                "baseUrl": env("RERANK_BASE_URL"),
                "model": env("RERANK_MODEL") or "BAAI/bge-reranker-v2-m3",
                "endpointOrder": env("RERANK_ENDPOINT_ORDER") or "score_first",
            },
        },
        "gateway": {
            "apiKeySecretName": env("GATEWAY_API_KEY_SECRET"),
            "caConfigMapName": env("GATEWAY_CA_CONFIGMAP"),
        },
        "tracing": {
            "enabled": tracing_enabled,
            "endpoint": otel_endpoint,
            "deploymentEnvironment": env("OTEL_DEPLOYMENT_ENVIRONMENT"),
            "serviceName": env("OTEL_SERVICE_NAME"),
        },
        "metrics": {"enabled": metrics_enabled},
        "route": {
            "enabled": agent_route,
            "timeoutSeconds": 300,
            "destinationCA": route_ca,
        },
        "storage": {"className": storage_class},
        "ingest": ingest_block,
        "pullSecret": {"name": env("PULL_SECRET")},
        "resources": {
            "agent": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            }
        },
        "_meta": {"namespace": namespace},
    }


def main(argv: list[str]) -> int:
    out = DEFAULT_OUT
    deploy_only = False
    args = list(argv)
    while args:
        flag = args.pop(0)
        if flag == "--out" and args:
            out = args.pop(0)
        elif flag == "--without-ingest-job":
            deploy_only = True
        else:
            print(
                "FAIL: unknown argument: "
                f"{flag} (usage: map_values.py [--out PATH] [--without-ingest-job])",
                file=sys.stderr,
            )
            return 1
    values = build_values(deploy_only=deploy_only)
    namespace = values.pop("_meta")["namespace"]
    header = (
        "# Generated release values (issue #448). Deployment input/evidence, "
        "not a source of truth.\n"
        "# Produced from validated operator input; airgap.env remains the owner.\n"
        f"# Namespace: {namespace}\n"
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(header + "\n".join(emit_yaml(values)) + "\n", encoding="utf-8")
    print(f"wrote {out} (namespace {namespace})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
