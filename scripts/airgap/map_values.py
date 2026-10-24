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

try:
    from scripts.airgap.model_config import (
        MODEL_CONFIG_KEYS,
        feedback_config,
        network_policy,
        parse_strict_bool,
        reasoning_overrides,
        request_overrides,
        require_in_place_retired,
        validate_model_config,
    )
except ModuleNotFoundError:
    from model_config import (  # type: ignore[no-redef]  # Direct bastion-script execution.
        MODEL_CONFIG_KEYS,
        feedback_config,
        network_policy,
        parse_strict_bool,
        reasoning_overrides,
        request_overrides,
        require_in_place_retired,
        validate_model_config,
    )

DEFAULT_OUT = "dist/mainframe-rag-release-values.yaml"


def die(msg: str) -> typing.NoReturn:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def positive_int(name: str, raw: str) -> int:
    if not re.fullmatch(r"[0-9]+", raw or "") or int(raw) < 1:
        die(f"{name} must be a positive integer (got {raw!r})")
    return int(raw)


def strict_bool(name: str, raw: str, default: bool) -> bool:
    """Mirror bool_flag in common.sh: unset keeps default, else strict."""
    try:
        return parse_strict_bool(name, raw, default)
    except ValueError as exc:
        die(str(exc))


def optional_bool(name: str, raw: str) -> bool | None:
    """Decoupled backend flags (issue #529 OBS-2): unset (None) follows the
    parent leg; an explicit value must be a boolean."""
    if raw == "":
        return None
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
    """Effective tracing leg. Unset means the bundled OpenObserve (ADR-0006,
    #956), the only trace backend: tracing on with an empty endpoint, which the
    chart resolves to that service. off/none/false/0 disables tracing."""
    raw = env("OTEL_EXPORTER_OTLP_ENDPOINT")
    if raw == "":
        return True, ""
    if raw.lower() in ("off", "none", "false", "0"):
        return False, ""
    if raw.startswith(("http://", "https://")):
        return True, raw
    die(f"OTEL_EXPORTER_OTLP_ENDPOINT must be http(s) or off/none/false/0, got {raw!r}")


def observability() -> tuple[bool, str, bool]:
    """The one owner of the observability decisions (issue #529 OBS-2):
    (tracing, endpoint, deploy ServiceMonitor). The trace backend is always the
    bundled OpenObserve while tracing is on. Explicit SERVICEMONITOR_ENABLED
    overrides; unset follows the parent leg. A backend for a disabled leg fails before any manifest
    is rendered. The shell stages consume this (--bundle-decisions) and the
    chart receives the effective booleans, never a default to re-derive."""
    tracing, endpoint = resolve_otel()
    metrics = env("METRICS_ENABLED") == "true"
    monitor = optional_bool("SERVICEMONITOR_ENABLED", env("SERVICEMONITOR_ENABLED"))
    if monitor and not metrics:
        die(
            "SERVICEMONITOR_ENABLED=true requires METRICS_ENABLED=true: refusing to "
            "monitor a disabled endpoint"
        )
    return (
        tracing,
        endpoint,
        metrics if monitor is None else monitor,
    )


def split_retire(raw: str) -> list[str]:
    """Mirror the INGEST_RETIRE_DOCS splitting in ingest.sh (shapes stay shell-owned)."""
    entries: list[str] = []
    for chunk in re.split(r"[,\n]", raw):
        entry = chunk.strip(" \t")
        if entry:
            entries.append(entry)
    return entries


def image_digest(role: str) -> dict:
    """Verified registry manifest digest for an image (issue #272), exported
    as IMAGE_DIGEST_<ROLE> by the deploy/ingest identity check. Unset keeps the
    tag-only reference (dry-run, no packed MANIFEST); a malformed value fails
    closed rather than rendering a reference that pins nothing."""
    raw = env(f"IMAGE_DIGEST_{role}")
    if not raw:
        return {}
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", raw):
        die(f"IMAGE_DIGEST_{role} must be a sha256 manifest digest")
    return {"digest": raw}


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
    internal_registry = env("INTERNAL_REGISTRY")
    if not internal_registry:
        die("INTERNAL_REGISTRY is not set (copy airgap.env.example to airgap.env)")
    namespace = env("NAMESPACE") or "mainframe-rag"
    if len(namespace) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", namespace):
        die("NAMESPACE must be a DNS label of at most 63 characters")
    qdrant_release = env("QDRANT_RELEASE") or "qdrant"
    image_sha = env("IMAGE_SHA")
    if image_sha in ("", "HEAD"):
        die("IMAGE_SHA must be the packed git SHA (see dist/MANIFEST.txt)")
    if not re.fullmatch(r"[0-9a-f]{40}", image_sha):
        die(f"IMAGE_SHA must be a full git SHA (got {image_sha!r})")

    embed_base = env("EMBED_BASE_URL")
    model_config = {name: env(name) for name in MODEL_CONFIG_KEYS}
    model_config["EMBED_BASE_URL"] = embed_base
    try:
        validate_model_config(model_config)
    except ValueError as exc:
        die(str(exc))
    if not env("EMBED_MODEL_REVISION"):
        die("EMBED_MODEL_REVISION is required for deploy/ingest (see airgap.env.example)")
    if not re.search(r"\S", env("EMBED_MODEL_REVISION")):
        die("EMBED_MODEL_REVISION must be a non-blank immutable revision")
    dimension = positive_int("DENSE_DIM", env("DENSE_DIM"))

    tracing_enabled, otel_endpoint, monitor_deploy = observability()

    rerank_enabled = lenient_bool(env("RERANK_ENABLED"))
    metrics_enabled = env("METRICS_ENABLED") == "true"
    # Issue #479: operator console selection. Unset keeps the chart default
    # (true); invalid nonempty values fail before mutation. Independent of
    # AGENT_ROUTE — disabling the UI never touches Route/OAuth/TLS wiring.
    ui_enabled = strict_bool("UI_ENABLED", env("UI_ENABLED"), True)

    # Issue #373: agent ingress NetworkPolicy, default on. Strict like the
    # other operator booleans; the allow-list is validated by its one owner.
    try:
        policy_enabled, policy_namespaces = network_policy(os.environ)
    except ValueError as exc:
        die(str(exc))

    # Issue #804: the answer-feedback service, default off. Validated by its
    # one owner; the block (and its resources) is emitted only when enabled,
    # so a deployment that never sets FEEDBACK_ENABLED renders what it did.
    try:
        feedback = feedback_config(os.environ)
    except ValueError as exc:
        die(str(exc))

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
        "peerUrls": "",
        "reingest": False,
        "retireDocs": [],
        "authorizeFromCatalog": False,
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
        try:
            require_in_place_retired(env("INGEST_ALIAS_PUBLISH"))
        except ValueError as exc:
            die(str(exc))
        qdrant_policy = {
            "shardNumber": positive_int("QDRANT_SHARD_NUMBER", env("QDRANT_SHARD_NUMBER")),
            "replicationFactor": positive_int(
                "QDRANT_REPLICATION_FACTOR", env("QDRANT_REPLICATION_FACTOR")
            ),
            "writeConsistencyFactor": positive_int(
                "QDRANT_WRITE_CONSISTENCY_FACTOR", env("QDRANT_WRITE_CONSISTENCY_FACTOR")
            ),
        }
        retire_docs = split_retire(env("INGEST_RETIRE_DOCS"))
        authorize_from_catalog = strict_bool(
            "INGEST_AUTHORIZE_FROM_CATALOG", env("INGEST_AUTHORIZE_FROM_CATALOG"), False
        )
        if authorize_from_catalog and not retire_docs:
            # The catalog authority only extends an explicit removal (#1005).
            die("INGEST_AUTHORIZE_FROM_CATALOG requires INGEST_RETIRE_DOCS")
        ingest_block = {
            "enabled": True,
            "workSize": env("INGEST_WORK_SIZE") or "100Gi",
            "corpusPVC": corpus_pvc,
            "workers": positive_int("INGEST_WORKERS", env("INGEST_WORKERS") or "4"),
            "reingest": strict_bool("INGEST_REINGEST", env("INGEST_REINGEST"), False),
            "retireDocs": retire_docs,
            "authorizeFromCatalog": authorize_from_catalog,
            "contextualEnabled": strict_bool(
                "CONTEXTUAL_EMBED_ENABLED", env("CONTEXTUAL_EMBED_ENABLED"), False
            ),
            "contextLlmBaseUrl": env("CONTEXT_LLM_BASE_URL"),
            "contextLlmModel": env("CONTEXT_LLM_MODEL"),
            "collectionPolicy": qdrant_policy,
            "peerUrls": env("QDRANT_PEER_URLS"),
            "resources": {
                "requests": {"cpu": "4", "memory": "8Gi"},
                "limits": {"cpu": "16", "memory": "32Gi"},
            },
        }

    reasoning_model = env("LLM_MODEL_REASONING")
    reasoning_base = env("LLM_BASE_URL") if reasoning_model else ""

    resources: dict = {
        "agent": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        }
    }
    if feedback:
        # Protected defaults (D8: unmeasured, 10Gi included); not operator keys.
        resources["feedback"] = {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "250m", "memory": "256Mi"},
        }

    # OpenObserve and otelcol (ADR-0006, #956) are the only trace/metrics
    # backend: present only for the bundled backend (tracing on, empty endpoint); a set
    # endpoint is the external-collector escape hatch and renders neither. Like every other
    # chart image they carry repository and tag only; the pinned digests live in
    # images.txt and are enforced at pack/load and by the pod-image check.
    tracing_images = {}
    if tracing_enabled and not otel_endpoint:
        tracing_images["openobserve"] = {
            "repository": f"{internal_registry}/openobserve/openobserve",
            "tag": "v1.1.2",
            "pullPolicy": "IfNotPresent",
        }
        # The collector scrapes /metrics: the chart renders it only with
        # metrics on (metrics.collector.enabled stays null and follows it).
        if metrics_enabled:
            tracing_images["otelcol"] = {
                "repository": f"{internal_registry}/otel/opentelemetry-collector-contrib",
                "tag": "0.160.0",
                "pullPolicy": "IfNotPresent",
            }

    # Plain Kubernetes (Kind) has no SCC to assign a UID, and the OpenObserve
    # image runs as root: an operator may pin one non-root id for the bundled
    # backend. Unset keeps the restricted-v2 platform-assigned identity.
    oo_values = {}
    run_as_id = env("OPENOBSERVE_RUN_AS_ID")
    if run_as_id:
        if not (tracing_enabled and not otel_endpoint):
            die("OPENOBSERVE_RUN_AS_ID requires the bundled OpenObserve (tracing on, empty OTEL_EXPORTER_OTLP_ENDPOINT)")
        uid = positive_int("OPENOBSERVE_RUN_AS_ID", run_as_id)
        if uid > 65535:
            die(f"OPENOBSERVE_RUN_AS_ID must be at most 65535 (got {run_as_id!r})")
        oo_values = {"openobserve": {"runAsId": uid}}

    return {
        **oo_values,
        "replicaCount": 2,
        "images": {
            **tracing_images,
            "agent": {
                "repository": f"{internal_registry}/qdrant-pdf-rag-agent",
                "tag": image_sha,
                "pullPolicy": "IfNotPresent",
                **image_digest("AGENT"),
            },
            "ingest": {
                "repository": f"{internal_registry}/qdrant-pdf-rag-ingest",
                "tag": image_sha,
                "pullPolicy": "IfNotPresent",
                **image_digest("INGEST"),
            },
            "oauthProxy": {
                "repository": f"{internal_registry}/openshift4/ose-oauth-proxy",
                "tag": "v4.14",
                "pullPolicy": "IfNotPresent",
                **image_digest("OAUTH_PROXY"),
            },
        },
        "qdrantRelease": qdrant_release,
        **request_overrides(model_config),
        "models": {
            "embedding": {
                "baseUrl": embed_base,
                "model": env("EMBED_MODEL"),
                "revision": env("EMBED_MODEL_REVISION"),
                "dimension": dimension,
            },
            "reasoning": {"baseUrl": reasoning_base, "model": reasoning_model,
                          "condenseEnabled": strict_bool("CHAT_CONDENSE_ENABLED",
                                                         env("CHAT_CONDENSE_ENABLED"), False),
                          **reasoning_overrides(model_config)},
            "rerank": {
                "enabled": rerank_enabled,
                "baseUrl": env("RERANK_BASE_URL"),
                "model": env("RERANK_MODEL") or "BAAI/bge-reranker-v2-m3",
                "endpointOrder": env("RERANK_ENDPOINT_ORDER") or "score_first",
            },
        },
        "gateway": {
            "apiKeySecretName": env("GATEWAY_API_KEY_SECRET"),
            "apiKeySecretKey": env("GATEWAY_API_KEY_SECRET_KEY"),
            "caConfigMapName": env("GATEWAY_CA_CONFIGMAP"),
        },
        "tracing": {
            "enabled": tracing_enabled,
            "endpoint": otel_endpoint,
            "deploymentEnvironment": env("OTEL_DEPLOYMENT_ENVIRONMENT"),
            "serviceName": env("OTEL_SERVICE_NAME"),
        },
        "metrics": {
            "enabled": metrics_enabled,
            "serviceMonitor": {"enabled": monitor_deploy},
        },
        "ui": {"enabled": ui_enabled},
        "networkPolicy": {"enabled": policy_enabled, "allowedNamespaces": policy_namespaces},
        **({"feedback": feedback} if feedback else {}),
        "route": {
            "enabled": agent_route,
            "timeoutSeconds": 300,
            "destinationCA": route_ca,
        },
        "storage": {"className": storage_class},
        "ingest": ingest_block,
        "pullSecret": {"name": env("PULL_SECRET")},
        "resources": resources,
        "_meta": {"namespace": namespace},
    }


def print_bundle_decisions() -> None:
    """Shell-safe assignments the airgap stages `eval` (common.sh
    resolve_observability): the one owner's decisions, never re-derived."""
    import shlex

    tracing, endpoint, monitor = observability()
    print(f"OTEL_TRACING_ENABLED={int(tracing)}")
    print(f"OTEL_ENDPOINT_RESOLVED={shlex.quote(endpoint)}")
    print(f"OTEL_BUNDLED={int(tracing and not endpoint)}")
    # Same condition as the chart's otelcolEnabled with metrics.collector.enabled
    # null: bundled OpenObserve and metrics on.
    print(f"OTEL_COLLECTOR={int(tracing and not endpoint and env('METRICS_ENABLED') == 'true')}")
    print(f"SERVICEMONITOR_DEPLOY={int(monitor)}")


def main(argv: list[str]) -> int:
    out = DEFAULT_OUT
    deploy_only = False
    args = list(argv)
    if args == ["--bundle-decisions"]:
        print_bundle_decisions()
        return 0
    while args:
        flag = args.pop(0)
        if flag == "--out" and args:
            out = args.pop(0)
        elif flag == "--without-ingest-job":
            deploy_only = True
        else:
            print(
                "FAIL: unknown argument: "
                f"{flag} (usage: map_values.py [--out PATH] [--without-ingest-job] | --bundle-decisions)",
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
