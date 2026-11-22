#!/usr/bin/env python3
"""Single-source registry of air-gap operator configuration keys (issue #1284).

Strictly stdlib-only (`python3 -I`-safe): runs on the air-gap bastion without
third-party packages or `mainframe_rag` imports.

Owns the canonical key metadata for every operator key and generates/checks:
- `OPERATOR_ENV_KEYS` in `scripts/airgap/common.sh`
- `TASK_OP_KEYS` and `TASK_<KEY>` bridge lines in `taskfiles/airgap.yml`
- key coverage and section headers in `airgap.env.example`
- key coverage in `tests/fixtures/extension-surface.json`
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    from scripts.airgap.model_config import RETIRED_KEYS
except ModuleNotFoundError:
    # Run as a file (`python3 -I`): search sibling after stdlib.
    sys.path.append(str(Path(__file__).resolve().parent))
    from model_config import RETIRED_KEYS  # type: ignore[no-redef]

KeyType = Literal["bool", "int", "float", "enum", "str", "path", "secret-ref"]
KEY_TYPES: tuple[KeyType, ...] = ("bool", "int", "float", "enum", "str", "path", "secret-ref")

VALID_SURFACES: frozenset[str] = frozenset({
    "shell_snapshot",  # scripts/airgap/common.sh OPERATOR_ENV_KEYS
    "task_bridge",     # taskfiles/airgap.yml TASK_OP_KEYS + TASK_<KEY>
    "example",         # airgap.env.example
    "preflight",       # scripts/airgap/validate.sh / common.sh / model_config.py / stage scripts
    "mapper",          # scripts/airgap/map_values.py (with helm_path)
    "chart_env",       # charts/mainframe-rag/templates/*.yaml (with chart_env var names)
    "qdrant_helm",     # charts/qdrant-*.tgz --set-string in deploy.sh / model_config.py --qdrant-sizing
})

# Plaintext gateway keys refused in airgap.env (#1016).
FORBIDDEN_ENV_FILE_KEYS: tuple[str, ...] = (
    "LLM_API_KEY",
    "EMBED_API_KEY",
    "RERANK_API_KEY",
    "CONTEXT_LLM_API_KEY",
)

# Honored from caller process environment only, never from airgap.env (#1014, #1118 F1).
PROCESS_ENV_ONLY_KEYS: tuple[str, ...] = (
    "CI_REHEARSAL",
    "AIRGAP_UNVERIFIED_CHECKOUT",
)


def ci_only_example_keys(root: Path) -> tuple[str, ...]:
    """GitLab CI/CD variable names shown indented in `airgap.env.example` prose
    ('not from this file') and never consumed by scripts/airgap/."""
    example = root / "airgap.env.example"
    if not example.is_file():
        return ()
    return tuple(sorted(set(re.findall(r"(?m)^#[ \t]{2,}(CI_[A-Z0-9_]+)=", example.read_text(encoding="utf-8")))))


@dataclass(frozen=True)
class OperatorKey:
    name: str
    section: str
    key_type: KeyType
    default: str | int | float | bool | None
    description: str
    issue: str
    surfaces: frozenset[str]
    helm_path: str | None = None
    chart_env: tuple[str, ...] = ()
    min_value: int | float | None = None
    max_value: int | float | None = None
    allowed_values: tuple[str, ...] = ()
    required: bool = False
    deprecated: str | None = None
    ci_rehearsal_only: bool = False
    sensitive: bool = False


_BASE = frozenset({"shell_snapshot", "task_bridge"})
_EX = _BASE | frozenset({"example"})
_EX_PRE = _EX | frozenset({"preflight"})
_EX_MAP = _EX_PRE | frozenset({"mapper"})
_EX_CHART = _EX_MAP | frozenset({"chart_env"})
_EX_QDRANT = _EX_PRE | frozenset({"qdrant_helm"})

_SEC_REGISTRY = "Internal registry (push from air-gap, pulled in-cluster)"
_SEC_ANCHORS = "Release trust anchors (#1118 F1/F2; docs/deploy.md section 4)"
_SEC_CLUSTER = "Target namespace / cluster"
_SEC_IMAGES = "Images (pack side pins these; see images.txt for the Qdrant pin)"
_SEC_INFERENCE = "Platform inference through LiteLLM (this repo does not host models)"
_SEC_RERANK = "Optional cross-encoder reranker (issue #76, default off)"
_SEC_GW_KEY = "Gateway API key (LiteLLM, all model legs)"
_SEC_CONTEXT = "Optional ingest: contextual retrieval prefixes (issue #78, default off)"
_SEC_TRACING = "Tracing (issue #83, ON by default)"
_SEC_CHAT = "Chat and retrieval behavior"
_SEC_METRICS = "Optional metrics (issue #187, default off)"
_SEC_CONSOLE = "Operator console selection (issue #479, default on)"
_SEC_NETPOL = "Agent ingress NetworkPolicy (issue #373, default ON)"
_SEC_FEEDBACK = "Answer-feedback service (issue #804, ADR-0005, default OFF)"
_SEC_OWUI = "Open WebUI docs front end (issue #1074, ADR-0007, default ON)"
_SEC_AGENT_EXTRAS = "Prod agent extras"
_SEC_INGEST = "Optional ingest (sh scripts/tools/run-task.sh airgap:ingest)"
_SEC_POLICY = "Collection distribution policy (issue #360)"
_SEC_FORBID = "Forbid list (scripts refuse these)"
_SEC_GW_CA = "Gateway TLS trust bundle"
_SEC_CLI = "CLI / stage controls"
_SEC_PACK = "Bundle packing and transfer"
_SEC_SMOKE = "Smoke and trace verification"

SECTIONS: tuple[str, ...] = (
    _SEC_REGISTRY,
    _SEC_ANCHORS,
    _SEC_CLUSTER,
    _SEC_IMAGES,
    _SEC_INFERENCE,
    _SEC_RERANK,
    _SEC_GW_KEY,
    _SEC_CONTEXT,
    _SEC_TRACING,
    _SEC_CHAT,
    _SEC_METRICS,
    _SEC_CONSOLE,
    _SEC_NETPOL,
    _SEC_FEEDBACK,
    _SEC_OWUI,
    _SEC_AGENT_EXTRAS,
    _SEC_INGEST,
    _SEC_POLICY,
    _SEC_FORBID,
    _SEC_GW_CA,
    _SEC_CLI,
    _SEC_PACK,
    _SEC_SMOKE,
)

OPERATOR_KEYS: tuple[OperatorKey, ...] = (
    # --- Internal registry ---
    OperatorKey(
        "INTERNAL_REGISTRY", _SEC_REGISTRY, "str", None,
        "Internal container registry host[/path] pushed by load and pulled in-cluster.",
        "#15", _EX_MAP | frozenset({"qdrant_helm"}),
        helm_path="images.agent.repository", required=True,
    ),
    OperatorKey(
        "SKOPEO_ARGS", _SEC_REGISTRY, "str", "",
        "Optional extra skopeo flags (e.g. --dest-cert-dir, --dest-authfile, --tmpdir).",
        "#15", _EX_PRE,
    ),
    OperatorKey(
        "INSECURE_REGISTRY", _SEC_REGISTRY, "bool", False,
        "Allow insecure TLS on skopeo copy (--src/dest-tls-verify=false).",
        "#15", _EX_PRE,
    ),

    # --- Release trust anchors ---
    OperatorKey(
        "SNEAKERNET_TRUSTED_PUB", _SEC_ANCHORS, "path", "",
        "Plain public-key file outside checkout/bundle verifying upstream release bundles.",
        "#1118", _EX_PRE,
    ),
    OperatorKey(
        "SNEAKERNET_GAP_TRUSTED_PUB", _SEC_ANCHORS, "path", "",
        "Plain public-key file outside checkout/bundle verifying gap release bundles.",
        "#1118", _EX_PRE,
    ),

    # --- Target namespace / cluster ---
    OperatorKey(
        "NAMESPACE", _SEC_CLUSTER, "str", "mainframe-rag",
        "Target OpenShift/Kubernetes namespace (DNS-1123 label, max 63 chars).",
        "#15", _EX_PRE, required=True,
    ),
    OperatorKey(
        "QDRANT_RELEASE", _SEC_CLUSTER, "str", "qdrant",
        "Qdrant Helm release name, Service name, and apikey Secret prefix.",
        "#448", _EX_MAP, helm_path="qdrantRelease",
    ),
    OperatorKey(
        "STORAGE_CLASS", _SEC_CLUSTER, "str", None,
        "RWO block StorageClass for Qdrant, OpenObserve, ingest-work, feedback and Open WebUI (NFS refused).",
        "#15", _EX_MAP | frozenset({"qdrant_helm"}),
        helm_path="storage.className", required=True,
    ),
    OperatorKey(
        "SNAPSHOT_STORAGE_CLASS", _SEC_CLUSTER, "str", "",
        "Optional RWO block StorageClass for Qdrant snapshots (defaults to STORAGE_CLASS; NFS refused).",
        "#15", _EX_QDRANT,
    ),
    OperatorKey(
        "PULL_SECRET", _SEC_CLUSTER, "secret-ref", "",
        "Optional Kubernetes imagePullSecret name if the namespace cannot pull anonymously.",
        "#15", _EX_MAP | frozenset({"qdrant_helm"}), helm_path="pullSecret.name",
    ),
    OperatorKey(
        "QDRANT_EXTRA_VALUES", _SEC_CLUSTER, "path", "",
        "CI-rehearsal-only extra Helm -f values file for Qdrant (requires CI_REHEARSAL=1).",
        "#1014", _EX_PRE, ci_rehearsal_only=True,
    ),
    OperatorKey(
        "QDRANT_STORAGE_SIZE", _SEC_CLUSTER, "str", "",
        "Rehearsal shorthand overriding both Qdrant data and snapshot PVC sizes (empty = 500Gi).",
        "#15", _EX_QDRANT,
    ),
    OperatorKey(
        "QDRANT_DATA_SIZE", _SEC_CLUSTER, "str", "500Gi",
        "Site sizing override for Qdrant data PVC (Ki/Mi/Gi/Ti; immutable after StatefulSet creation).",
        "#1070", _EX_QDRANT,
    ),
    OperatorKey(
        "QDRANT_SNAPSHOT_SIZE", _SEC_CLUSTER, "str", "500Gi",
        "Site sizing override for Qdrant snapshot PVC (Ki/Mi/Gi/Ti; immutable after StatefulSet creation).",
        "#1070", _EX_QDRANT,
    ),
    OperatorKey(
        "QDRANT_CPU_REQUEST", _SEC_CLUSTER, "str", "4",
        "Site sizing override for Qdrant container CPU request.",
        "#1070", _EX_QDRANT,
    ),
    OperatorKey(
        "QDRANT_MEMORY_REQUEST", _SEC_CLUSTER, "str", "16Gi",
        "Site sizing override for Qdrant container memory request.",
        "#1070", _EX_QDRANT,
    ),
    OperatorKey(
        "QDRANT_CPU_LIMIT", _SEC_CLUSTER, "str", "8",
        "Site sizing override for Qdrant container CPU limit (must be >= request).",
        "#1070", _EX_QDRANT,
    ),
    OperatorKey(
        "QDRANT_MEMORY_LIMIT", _SEC_CLUSTER, "str", "32Gi",
        "Site sizing override for Qdrant container memory limit (must be >= request).",
        "#1070", _EX_QDRANT,
    ),
    OperatorKey(
        "INGEST_CPU_REQUEST", _SEC_CLUSTER, "str", "4",
        "Site sizing override for ingest Job CPU request.",
        "#1070", _EX_MAP, helm_path="ingest.resources.requests.cpu",
    ),
    OperatorKey(
        "INGEST_MEMORY_REQUEST", _SEC_CLUSTER, "str", "8Gi",
        "Site sizing override for ingest Job memory request.",
        "#1070", _EX_MAP, helm_path="ingest.resources.requests.memory",
    ),
    OperatorKey(
        "INGEST_CPU_LIMIT", _SEC_CLUSTER, "str", "16",
        "Site sizing override for ingest Job CPU limit (must be >= request).",
        "#1070", _EX_MAP, helm_path="ingest.resources.limits.cpu",
    ),
    OperatorKey(
        "INGEST_MEMORY_LIMIT", _SEC_CLUSTER, "str", "32Gi",
        "Site sizing override for ingest Job memory limit (must be >= request).",
        "#1070", _EX_MAP, helm_path="ingest.resources.limits.memory",
    ),
    OperatorKey(
        "AGENT_CPU_REQUEST", _SEC_CLUSTER, "str", "250m",
        "Site sizing override for the agent container CPU request (not the oauth-proxy sidecar).",
        "#1348", _EX_MAP, helm_path="resources.agent.requests.cpu",
    ),
    OperatorKey(
        "AGENT_MEMORY_REQUEST", _SEC_CLUSTER, "str", "512Mi",
        "Site sizing override for the agent container memory request.",
        "#1348", _EX_MAP, helm_path="resources.agent.requests.memory",
    ),
    OperatorKey(
        "AGENT_CPU_LIMIT", _SEC_CLUSTER, "str", "2",
        "Site sizing override for the agent container CPU limit (must be >= request).",
        "#1348", _EX_MAP, helm_path="resources.agent.limits.cpu",
    ),
    OperatorKey(
        "AGENT_MEMORY_LIMIT", _SEC_CLUSTER, "str", "2Gi",
        "Site sizing override for the agent container memory limit (must be >= request).",
        "#1348", _EX_MAP, helm_path="resources.agent.limits.memory",
    ),

    # --- Images ---
    OperatorKey(
        "IMAGE_SHA", _SEC_IMAGES, "str", "",
        "Full 40-hex packed git SHA used as agent/ingest image tag (defaults to git rev-parse HEAD).",
        "#15", _EX_CHART, helm_path="images.agent.tag", chart_env=("IMAGE_SHA",),
    ),
    OperatorKey(
        "QDRANT_IMAGE", _SEC_IMAGES, "str", "docker.io/qdrant/qdrant:v1.19.2-unprivileged",
        "Pinned unprivileged Qdrant image reference (must match images.txt).",
        "#15", _EX_QDRANT,
    ),
    OperatorKey(
        "QDRANT_TAG", _SEC_IMAGES, "str", "",
        "Optional Qdrant chart image tag override (rehearsal only; empty derives from QDRANT_IMAGE).",
        "#15", _EX_QDRANT,
    ),

    # --- Platform inference through LiteLLM ---
    OperatorKey(
        "GATEWAY_BASE_URL", _SEC_INFERENCE, "str", "",
        "Shared LiteLLM/vLLM OpenAI-compatible API base URL ending in /v1.",
        "#245", _EX_PRE,
    ),
    OperatorKey(
        "EMBED_BASE_URL", _SEC_INFERENCE, "str", "",
        "Embedding endpoint base URL (defaults to GATEWAY_BASE_URL).",
        "#245", _EX_CHART, helm_path="models.embedding.baseUrl", chart_env=("EMBED_BASE_URL",),
    ),
    OperatorKey(
        "EMBED_MODEL", _SEC_INFERENCE, "str", None,
        "Platform embedding model alias served at EMBED_BASE_URL.",
        "#245", _EX_CHART, helm_path="models.embedding.model", chart_env=("EMBED_MODEL",), required=True,
    ),
    OperatorKey(
        "DENSE_DIM", _SEC_INFERENCE, "int", None,
        "Dense embedding vector dimension matching EMBED_MODEL output.",
        "#245", _EX_CHART, helm_path="models.embedding.dimension", chart_env=("DENSE_DIM",),
        min_value=1, required=True,
    ),
    OperatorKey(
        "EMBED_MODEL_REVISION", _SEC_INFERENCE, "str", None,
        "Non-blank immutable embedding model/config revision attesting deployed weights.",
        "#362", _EX_CHART, helm_path="models.embedding.revision", chart_env=("EMBED_MODEL_REVISION",), required=True,
    ),
    OperatorKey(
        "LLM_BASE_URL", _SEC_INFERENCE, "str", "",
        "Reasoning LLM endpoint base URL (defaults to GATEWAY_BASE_URL).",
        "#245", _EX_CHART, helm_path="models.reasoning.baseUrl", chart_env=("LLM_BASE_URL",),
    ),
    OperatorKey(
        "LLM_MODEL_REASONING", _SEC_INFERENCE, "str", "",
        "Designated reasoning model alias (empty keeps answers/chat disabled).",
        "#245", _EX_CHART, helm_path="models.reasoning.model", chart_env=("LLM_MODEL_REASONING",),
    ),
    OperatorKey(
        "GATEWAY_PROBE_MAX_TOKENS", _SEC_INFERENCE, "int", 16,
        "Max tokens used by pipeline.sh gateway readiness probe (1..4096).",
        "#706", _EX_PRE, min_value=1, max_value=4096,
    ),
    OperatorKey(
        "LLM_MAX_MODEL_LEN", _SEC_INFERENCE, "int", 262144,
        "Served reasoning model context window in tokens (512..262144).",
        "#706", _EX_CHART, helm_path="models.reasoning.maxModelLen", chart_env=("LLM_MAX_MODEL_LEN",),
        min_value=512, max_value=262144,
    ),
    OperatorKey(
        "LLM_RESERVED_OUTPUT_TOKENS", _SEC_INFERENCE, "int", 2560,
        "Output token budget reserved for final answers (128..16384).",
        "#706", _EX_CHART, helm_path="models.reasoning.reservedOutputTokens",
        chart_env=("LLM_RESERVED_OUTPUT_TOKENS",), min_value=128, max_value=16384,
    ),
    OperatorKey(
        "LLM_THINKING_RESERVE_TOKENS_SIMPLE", _SEC_INFERENCE, "int", 8192,
        "Thinking-token reserve for simple prompts (0..16384).",
        "#706", _EX_CHART, helm_path="models.reasoning.thinkingReserveTokensSimple",
        chart_env=("LLM_THINKING_RESERVE_TOKENS_SIMPLE",), min_value=0, max_value=16384,
    ),
    OperatorKey(
        "LLM_THINKING_RESERVE_TOKENS_COMPLEX", _SEC_INFERENCE, "int", 8192,
        "Thinking-token reserve for complex prompts (0..16384).",
        "#706", _EX_CHART, helm_path="models.reasoning.thinkingReserveTokensComplex",
        chart_env=("LLM_THINKING_RESERVE_TOKENS_COMPLEX",), min_value=0, max_value=16384,
    ),
    OperatorKey(
        "LLM_REASONING_EFFORT_SIMPLE", _SEC_INFERENCE, "enum", "medium",
        "Reasoning effort for simple prompts (must be listed in LLM_REASONING_EFFORTS).",
        "#917", _EX_CHART, helm_path="models.reasoning.reasoningEffortSimple",
        chart_env=("LLM_REASONING_EFFORT_SIMPLE",), allowed_values=("low", "medium", "high", "xhigh"),
    ),
    OperatorKey(
        "LLM_REASONING_EFFORT_COMPLEX", _SEC_INFERENCE, "enum", "medium",
        "Reasoning effort for complex prompts (must be listed in LLM_REASONING_EFFORTS).",
        "#917", _EX_CHART, helm_path="models.reasoning.reasoningEffortComplex",
        chart_env=("LLM_REASONING_EFFORT_COMPLEX",), allowed_values=("low", "medium", "high", "xhigh"),
    ),
    OperatorKey(
        "LLM_REASONING_EFFORTS", _SEC_INFERENCE, "str", "low,medium,xhigh",
        "Comma-separated ascending subset of low/medium/high/xhigh accepted by the served template.",
        "#748", _EX_CHART, helm_path="models.reasoning.reasoningEfforts",
        chart_env=("LLM_REASONING_EFFORTS",),
    ),
    OperatorKey(
        "LLM_STREAM", _SEC_INFERENCE, "bool", False,
        "Enable streaming responses from the reasoning LLM.",
        "#706", _EX_CHART, helm_path="models.reasoning.stream", chart_env=("LLM_STREAM",),
    ),
    OperatorKey(
        "LLM_EXTRA_BODY", _SEC_INFERENCE, "str", "",
        "Optional JSON object merged into every chat completion request (max 4096 chars).",
        "#1193", _EX_CHART, helm_path="models.reasoning.extraBody", chart_env=("LLM_EXTRA_BODY",),
    ),
    OperatorKey(
        "LLM_EFFORT_TRANSPORT", _SEC_INFERENCE, "enum", "top_level",
        "Where reasoning_effort is placed in chat requests (top_level, chat_template, or none).",
        "#1193", _EX_CHART, helm_path="models.reasoning.effortTransport",
        chart_env=("LLM_EFFORT_TRANSPORT",), allowed_values=("top_level", "chat_template", "none"),
    ),
    OperatorKey(
        "LLM_TOKEN_COUNT_SOURCE", _SEC_INFERENCE, "enum", "auto",
        "Source for prompt token counting (auto, tokenize, completion_usage, or estimate).",
        "#1266", _EX_CHART, helm_path="models.reasoning.tokenCountSource",
        chart_env=("LLM_TOKEN_COUNT_SOURCE",), allowed_values=("tokenize", "completion_usage", "auto", "estimate"),
    ),
    OperatorKey(
        "REQUEST_DEADLINE_S", _SEC_INFERENCE, "int", 270,
        "Total per-request agent deadline in seconds (0 disables; nonzero must be < 300 s route timeout).",
        "#843", _EX_CHART, helm_path="requestDeadlineSeconds", chart_env=("REQUEST_DEADLINE_S",),
        min_value=0, max_value=3600,
    ),
    OperatorKey(
        "REQUEST_MAX_CONCURRENT", _SEC_INFERENCE, "int", 4,
        "Max concurrent answer/chat requests per agent replica (0..1000; 0 = unlimited).",
        "#843", _EX_CHART, helm_path="requestMaxConcurrent", chart_env=("REQUEST_MAX_CONCURRENT",),
        min_value=0, max_value=1000,
    ),
    OperatorKey(
        "REQUEST_QUEUE_MAX", _SEC_INFERENCE, "int", 4,
        "Max queued answer/chat requests per agent replica (0..1000).",
        "#843", _EX_CHART, helm_path="requestQueueMax", chart_env=("REQUEST_QUEUE_MAX",),
        min_value=0, max_value=1000,
    ),
    OperatorKey(
        "REQUEST_QUEUE_WAIT_S", _SEC_INFERENCE, "float", 30.0,
        "Max seconds a queued request may wait before 503 (0..300; > 0 while concurrency > 0).",
        "#843", _EX_CHART, helm_path="requestQueueWaitSeconds", chart_env=("REQUEST_QUEUE_WAIT_S",),
        min_value=0.0, max_value=300.0,
    ),
    OperatorKey(
        "REQUEST_MAX_BODY_BYTES", _SEC_INFERENCE, "int", 25165824,
        "Largest request body in bytes per agent replica; larger is refused with 413 before parsing (1 MiB..256 MiB).",
        "#1352", _EX_CHART, helm_path="requestMaxBodyBytes", chart_env=("REQUEST_MAX_BODY_BYTES",),
        min_value=1048576, max_value=268435456,
    ),
    OperatorKey(
        "REQUEST_BODY_BUDGET_BYTES", _SEC_INFERENCE, "int", 134217728,
        "Sum of request bodies in flight per agent replica; excess waits, then 503 (1 MiB..4 GiB; >= REQUEST_MAX_BODY_BYTES).",
        "#1352", _EX_CHART, helm_path="requestBodyBudgetBytes", chart_env=("REQUEST_BODY_BUDGET_BYTES",),
        min_value=1048576, max_value=4294967296,
    ),

    # --- Optional cross-encoder reranker ---
    OperatorKey(
        "RERANK_ENABLED", _SEC_RERANK, "bool", False,
        "Enable cross-encoder reranking over retrieved candidates.",
        "#76", _EX_CHART, helm_path="models.rerank.enabled", chart_env=("RERANK_ENABLED",),
    ),
    OperatorKey(
        "RERANK_BASE_URL", _SEC_RERANK, "str", "",
        "Reranker endpoint base URL (defaults to GATEWAY_BASE_URL, then EMBED_BASE_URL fallback).",
        "#76", _EX_CHART, helm_path="models.rerank.baseUrl", chart_env=("RERANK_BASE_URL",),
    ),
    OperatorKey(
        "RERANK_MODEL", _SEC_RERANK, "str", "BAAI/bge-reranker-v2-m3",
        "Cross-encoder reranker model identifier.",
        "#76", _EX_CHART, helm_path="models.rerank.model", chart_env=("RERANK_MODEL",),
    ),
    OperatorKey(
        "RERANK_ENDPOINT_ORDER", _SEC_RERANK, "enum", "score_first",
        "Endpoint probe order for reranking (score_first or rerank_first).",
        "#76", _EX_CHART, helm_path="models.rerank.endpointOrder", chart_env=("RERANK_ENDPOINT_ORDER",),
        allowed_values=("score_first", "rerank_first"),
    ),

    # --- Gateway API key ---
    OperatorKey(
        "GATEWAY_API_KEY_SECRET", _SEC_GW_KEY, "secret-ref", "",
        "Operator-created Kubernetes Secret name holding LiteLLM gateway virtual keys.",
        "#245", _EX_MAP, helm_path="gateway.apiKeySecretName",
    ),
    OperatorKey(
        "GATEWAY_API_KEY_SECRET_KEY", _SEC_GW_KEY, "secret-ref", "",
        "Shared data-key name inside GATEWAY_API_KEY_SECRET (empty keeps legacy per-leg keys).",
        "#245", _EX_MAP, helm_path="gateway.apiKeySecretKey",
    ),

    # --- Optional ingest: contextual retrieval prefixes ---
    OperatorKey(
        "CONTEXTUAL_EMBED_ENABLED", _SEC_CONTEXT, "bool", False,
        "Generate per-chunk contextual retrieval prefixes during ingest (changes dense vectors).",
        "#78", _EX_CHART, helm_path="ingest.contextualEnabled", chart_env=("CONTEXTUAL_EMBED_ENABLED",),
    ),
    OperatorKey(
        "CONTEXT_LLM_BASE_URL", _SEC_CONTEXT, "str", "",
        "Base URL for contextual prefix LLM (defaults to GATEWAY_BASE_URL).",
        "#78", _EX_CHART, helm_path="ingest.contextLlmBaseUrl", chart_env=("CONTEXT_LLM_BASE_URL",),
    ),
    OperatorKey(
        "CONTEXT_LLM_MODEL", _SEC_CONTEXT, "str", "",
        "Model alias for contextual prefix generation (required when CONTEXTUAL_EMBED_ENABLED=true).",
        "#78", _EX_CHART, helm_path="ingest.contextLlmModel", chart_env=("CONTEXT_LLM_MODEL",),
    ),

    # --- Tracing ---
    OperatorKey(
        "OTEL_EXPORTER_OTLP_ENDPOINT", _SEC_TRACING, "str", "",
        "OTLP trace collector URL, empty for bundled OpenObserve, or off/none/false/0 to disable.",
        "#83", _EX_CHART, helm_path="tracing.endpoint", chart_env=("OTEL_EXPORTER_OTLP_ENDPOINT",),
    ),
    OperatorKey(
        "OPENOBSERVE_RUN_AS_ID", _SEC_TRACING, "int", None,
        "Non-root UID/GID/fsGroup for bundled OpenObserve on plain Kubernetes (Kind only; 1..65535).",
        "#956", _EX_MAP, helm_path="openobserve.runAsId", min_value=1, max_value=65535,
    ),
    OperatorKey(
        "OTEL_DEPLOYMENT_ENVIRONMENT", _SEC_TRACING, "str", "",
        "OpenTelemetry deployment.environment resource attribute on emitted spans.",
        "#83", _EX_CHART, helm_path="tracing.deploymentEnvironment",
        chart_env=("OTEL_DEPLOYMENT_ENVIRONMENT",),
    ),
    OperatorKey(
        "OTEL_SERVICE_NAME", _SEC_TRACING, "str", "",
        "Optional OpenTelemetry service.name override (empty keeps per-process defaults).",
        "#83", _EX_CHART, helm_path="tracing.serviceName", chart_env=("OTEL_SERVICE_NAME",),
    ),

    # --- Chat and retrieval behavior ---
    OperatorKey(
        "CHAT_CONDENSE_ENABLED", _SEC_CHAT, "bool", False,
        "Rewrite follow-up chat questions via reasoning model (mutually exclusive with CHAT_FOLLOWUP_ANCHOR_ENABLED).",
        "#1268", _EX_CHART, helm_path="models.reasoning.condenseEnabled",
        chart_env=("CHAT_CONDENSE_ENABLED",),
    ),
    OperatorKey(
        "CHAT_MAX_TURNS", _SEC_CHAT, "int", 10,
        "Maximum prior chat messages kept in prompt history (1..50).",
        "#1268", _EX_CHART, helm_path="models.reasoning.chatMaxTurns",
        chart_env=("CHAT_MAX_TURNS",), min_value=1, max_value=50,
    ),
    OperatorKey(
        "CHAT_MAX_PRIOR_TURN_CHARS", _SEC_CHAT, "int", 4000,
        "Maximum characters kept per prior chat message (200..5000).",
        "#1268", _EX_CHART, helm_path="models.reasoning.chatMaxPriorTurnChars",
        chart_env=("CHAT_MAX_PRIOR_TURN_CHARS",), min_value=200, max_value=5000,
    ),
    OperatorKey(
        "ANSWER_HIT_LIMIT", _SEC_CHAT, "int", 16,
        "Retrieved hits packed per answer/chat/console request (1..32).",
        "#1191", _EX_CHART, helm_path="models.reasoning.answerHitLimit",
        chart_env=("ANSWER_HIT_LIMIT",), min_value=1, max_value=32,
    ),
    OperatorKey(
        "RETRIEVE_MAX_CHUNKS_PER_DOC", _SEC_CHAT, "int", 5,
        "Maximum chunks kept per manual across all retrieval endpoints (1..10).",
        "#1191", _EX_CHART, helm_path="models.reasoning.retrieveMaxChunksPerDoc",
        chart_env=("RETRIEVE_MAX_CHUNKS_PER_DOC",), min_value=1, max_value=10,
    ),
    OperatorKey(
        "CHAT_FOLLOWUP_ANCHOR_ENABLED", _SEC_CHAT, "bool", True,
        "Anchor identifier-free follow-ups with previous user message (mutually exclusive with CHAT_CONDENSE_ENABLED).",
        "#1268", _EX_CHART, helm_path="models.reasoning.followupAnchorEnabled",
        chart_env=("CHAT_FOLLOWUP_ANCHOR_ENABLED",),
    ),
    OperatorKey(
        "ANSWER_CODING_MODE_ENABLED", _SEC_CHAT, "bool", True,
        "Route mainframe coding requests (JCL/REXX/HLASM/C) to the complex coding prompt.",
        "#1276", _EX_CHART, helm_path="models.reasoning.codingModeEnabled",
        chart_env=("ANSWER_CODING_MODE_ENABLED",),
    ),
    OperatorKey(
        "ANSWER_SPL_MODE_ENABLED", _SEC_CHAT, "bool", False,
        "Route Splunk SPL requests and pasted Splunk results over z/OS data to the complex SPL prompt.",
        "#1293", _EX_CHART, helm_path="models.reasoning.splModeEnabled",
        chart_env=("ANSWER_SPL_MODE_ENABLED",),
    ),
    OperatorKey(
        "CHAT_IMAGES_ENABLED", _SEC_CHAT, "bool", False,
        "Enable pasted images in Open WebUI temporary chats (requires --probe-image pass).",
        "#1287", _EX_CHART, helm_path="models.reasoning.chatImagesEnabled",
        chart_env=("CHAT_IMAGES_ENABLED",),
    ),
    OperatorKey(
        "LLM_IMAGE_TOKEN_RESERVE", _SEC_CHAT, "int", 6144,
        "Prompt tokens reserved per pasted chat image (256..65536).",
        "#1287", _EX_CHART, helm_path="models.reasoning.imageTokenReserve",
        chart_env=("LLM_IMAGE_TOKEN_RESERVE",), min_value=256, max_value=65536,
    ),

    # --- Optional metrics ---
    OperatorKey(
        "METRICS_ENABLED", _SEC_METRICS, "bool", False,
        "Serve Prometheus /metrics on agent and enable UWM ServiceMonitor by default.",
        "#187", _EX_CHART, helm_path="metrics.enabled", chart_env=("METRICS_ENABLED",),
    ),
    OperatorKey(
        "SERVICEMONITOR_ENABLED", _SEC_METRICS, "bool", None,
        "Decouple ServiceMonitor rendering from METRICS_ENABLED (unset follows METRICS_ENABLED).",
        "#529", _EX_MAP, helm_path="metrics.serviceMonitor.enabled",
    ),

    # --- Operator console selection ---
    OperatorKey(
        "UI_ENABLED", _SEC_CONSOLE, "bool", True,
        "Serve the in-cluster /ui operator console (false renders API-only profile).",
        "#479", _EX_CHART, helm_path="ui.enabled", chart_env=("UI_ENABLED",),
    ),

    # --- Agent ingress NetworkPolicy ---
    OperatorKey(
        "AGENT_NETWORK_POLICY", _SEC_NETPOL, "bool", True,
        "Render rag-agent-ingress NetworkPolicy restricting HTTP 8080 to approved namespaces.",
        "#373", _EX_MAP, helm_path="networkPolicy.enabled",
    ),
    OperatorKey(
        "AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES", _SEC_NETPOL, "str", "",
        "Comma-separated DNS-1123 namespace names additionally allowed to reach agent port 8080.",
        "#373", _EX_MAP, helm_path="networkPolicy.allowedNamespaces",
    ),

    # --- Answer-feedback service ---
    OperatorKey(
        "FEEDBACK_ENABLED", _SEC_FEEDBACK, "bool", False,
        "Deploy the rag-feedback StatefulSet for answer feedback (requires AGENT_NETWORK_POLICY=true).",
        "#804", _EX_CHART, helm_path="feedback.enabled", chart_env=("FEEDBACK_ENABLED",),
    ),
    OperatorKey(
        "FEEDBACK_SECRET", _SEC_FEEDBACK, "secret-ref", "",
        "Operator-created Kubernetes Secret name with data key service-token for rag-feedback.",
        "#804", _EX_MAP, helm_path="feedback.secretName",
    ),
    OperatorKey(
        "FEEDBACK_STORAGE_SIZE", _SEC_FEEDBACK, "str", "10Gi",
        "RWO PVC size for rag-feedback-data (Mi/Gi/Ti).",
        "#804", _EX_MAP, helm_path="feedback.storageSize",
    ),
    OperatorKey(
        "FEEDBACK_RETENTION_DAYS", _SEC_FEEDBACK, "int", 180,
        "Days before expired feedback records are purged (1..3650).",
        "#804", _EX_CHART, helm_path="feedback.retentionDays", chart_env=("FEEDBACK_RETENTION_DAYS",),
        min_value=1, max_value=3650,
    ),

    # --- Open WebUI docs front end ---
    OperatorKey(
        "OPENWEBUI_ENABLED", _SEC_OWUI, "bool", True,
        "Deploy the Open WebUI docs front end (default on per ADR-0008; false opts out).",
        "#1074", _EX_MAP, helm_path="openwebui.enabled",
    ),
    OperatorKey(
        "OPENWEBUI_SECRET", _SEC_OWUI, "secret-ref", "",
        "Operator-created Kubernetes Secret name with webui-secret-key and oidc-client-secret.",
        "#1074", _EX_MAP, helm_path="openwebui.secretName",
    ),
    OperatorKey(
        "OPENWEBUI_HOST", _SEC_OWUI, "str", "",
        "Lowercase DNS hostname for the rag-openwebui reencrypt Route (no scheme/path).",
        "#1074", _EX_MAP, helm_path="openwebui.host",
    ),
    OperatorKey(
        "OPENWEBUI_OIDC_PROVIDER_URL", _SEC_OWUI, "str", "",
        "HTTPS OIDC discovery URL ending in /.well-known/openid-configuration.",
        "#1074", _EX_CHART, helm_path="openwebui.oidc.providerUrl", chart_env=("OPENID_PROVIDER_URL",),
    ),
    OperatorKey(
        "OPENWEBUI_OIDC_CLIENT_ID", _SEC_OWUI, "str", "",
        "OIDC client identifier registered with the site identity provider.",
        "#1074", _EX_CHART, helm_path="openwebui.oidc.clientId", chart_env=("OAUTH_CLIENT_ID",),
    ),
    OperatorKey(
        "OPENWEBUI_OIDC_GROUP_CLAIM", _SEC_OWUI, "str", "groups",
        "ID-token claim carrying group names.",
        "#1074", _EX_CHART, helm_path="openwebui.oidc.groupClaim", chart_env=("OAUTH_GROUP_CLAIM",),
    ),
    OperatorKey(
        "OPENWEBUI_OIDC_ROLES_CLAIM", _SEC_OWUI, "str", "roles",
        "ID-token claim carrying role names.",
        "#1074", _EX_CHART, helm_path="openwebui.oidc.rolesClaim", chart_env=("OAUTH_ROLES_CLAIM",),
    ),
    OperatorKey(
        "OPENWEBUI_OIDC_ALLOWED_ROLES", _SEC_OWUI, "str", "",
        "Comma-separated roles allowed to sign in to Open WebUI (must not contain * or signed-in).",
        "#1074", _EX_CHART, helm_path="openwebui.oidc.allowedRoles", chart_env=("OAUTH_ALLOWED_ROLES",),
    ),
    OperatorKey(
        "OPENWEBUI_OIDC_ADMIN_ROLES", _SEC_OWUI, "str", "",
        "Comma-separated roles granted Open WebUI admin access (must not contain * or signed-in).",
        "#1074", _EX_CHART, helm_path="openwebui.oidc.adminRoles", chart_env=("OAUTH_ADMIN_ROLES",),
    ),
    OperatorKey(
        "OPENWEBUI_OIDC_SCOPES", _SEC_OWUI, "str", "openid email profile groups offline_access",
        "Space-separated OIDC scopes requested at sign-in (must include openid).",
        "#1074", _EX_CHART, helm_path="openwebui.oidc.scopes", chart_env=("OAUTH_SCOPES",),
    ),
    OperatorKey(
        "OPENWEBUI_OIDC_EMAIL_CLAIM", _SEC_OWUI, "str", "email",
        "ID-token claim carrying the user email address.",
        "#1074", _EX_CHART, helm_path="openwebui.oidc.emailClaim", chart_env=("OAUTH_EMAIL_CLAIM",),
    ),
    OperatorKey(
        "OPENWEBUI_OIDC_USERNAME_CLAIM", _SEC_OWUI, "str", "name",
        "ID-token claim carrying the user display name.",
        "#1074", _EX_CHART, helm_path="openwebui.oidc.usernameClaim", chart_env=("OAUTH_USERNAME_CLAIM",),
    ),
    OperatorKey(
        "OPENWEBUI_IDP_EGRESS_CIDRS", _SEC_OWUI, "str", "",
        "Comma-separated IPv4 CIDRs permitted in rag-openwebui egress NetworkPolicy for the IdP.",
        "#1074", _EX_MAP, helm_path="openwebui.idpEgress.cidrs",
    ),
    OperatorKey(
        "OPENWEBUI_IDP_EGRESS_PORT", _SEC_OWUI, "int", 443,
        "TCP port for Open WebUI IdP back-channel egress (1..65535).",
        "#1074", _EX_MAP, helm_path="openwebui.idpEgress.port", min_value=1, max_value=65535,
    ),
    OperatorKey(
        "OPENWEBUI_IDP_CA_FILE", _SEC_OWUI, "path", "",
        "Optional PEM CA certificate bundle path for an enterprise IdP TLS certificate.",
        "#1074", _EX_MAP, helm_path="openwebui.oidc.caBundle",
    ),
    OperatorKey(
        "OPENWEBUI_RETENTION_DAYS", _SEC_OWUI, "int", 30,
        "Chat retention window enforced by the Open WebUI purge sidecar (1..3650).",
        "#1074", _EX_MAP, helm_path="openwebui.retentionDays", min_value=1, max_value=3650,
    ),
    OperatorKey(
        "OPENWEBUI_RATINGS_ENABLED", _SEC_OWUI, "bool", None,
        "Enable thumbs up/down answer ratings in Open WebUI (unset follows FEEDBACK_ENABLED).",
        "#1255", _EX_MAP, helm_path="openwebui.ratings.enabled",
    ),
    OperatorKey(
        "OPENWEBUI_STORAGE_SIZE", _SEC_OWUI, "str", "10Gi",
        "RWO PVC size for rag-openwebui-data (Mi/Gi/Ti).",
        "#1074", _EX_MAP, helm_path="openwebui.storageSize",
    ),

    # --- Prod agent extras ---
    OperatorKey(
        "AGENT_ROUTE", _SEC_AGENT_EXTRAS, "bool", False,
        "Render reencrypt OpenShift Route and oauth-proxy sidecar for the agent console.",
        "#373", _EX_MAP, helm_path="route.enabled",
    ),

    # --- Optional ingest ---
    OperatorKey(
        "CORPUS_PVC", _SEC_INGEST, "str", "",
        "Caller-supplied corpus PVC name mounted read-only at /corpus during ingest.",
        "#15", _EX_MAP, helm_path="ingest.corpusPVC",
    ),
    OperatorKey(
        "INGEST_WORKERS", _SEC_INGEST, "int", 4,
        "CPU worker count for parallel PDF ingest.",
        "#15", _EX_CHART, helm_path="ingest.workers", chart_env=("INGEST_WORKERS",), min_value=1,
    ),
    OperatorKey(
        "INGEST_TIMEOUT", _SEC_INGEST, "int", 3600,
        "Seconds ingest.sh waits for job/ingest completion.",
        "#15", _EX_PRE, min_value=1,
    ),
    OperatorKey(
        "INGEST_WORK_SIZE", _SEC_INGEST, "str", "100Gi",
        "Size of the ingest-work scratch PVC (empty = 100Gi).",
        "#448", _EX_MAP, helm_path="ingest.workSize",
    ),
    OperatorKey(
        "INGEST_REINGEST", _SEC_INGEST, "bool", False,
        "Force full rebuild/repair ingest (--reingest).",
        "#391", _EX_MAP, helm_path="ingest.reingest",
    ),
    OperatorKey(
        "INGEST_RETIRE_DOCS", _SEC_INGEST, "str", "",
        "Comma- or newline-separated DOCID[@SOURCEREV] entries approved for removal.",
        "#391", _EX_MAP, helm_path="ingest.retireDocs",
    ),
    OperatorKey(
        "INGEST_AUTHORIZE_FROM_CATALOG", _SEC_INGEST, "bool", False,
        "Approve retirement of live-published revisions when inventory was lost (requires INGEST_RETIRE_DOCS).",
        "#1005", _EX_MAP, helm_path="ingest.authorizeFromCatalog",
    ),
    OperatorKey(
        "INGEST_ALIAS_PUBLISH", _SEC_INGEST, "bool", True,
        "Deprecated alias-publication switch (in-place ingest was retired in #360; unset or true only).",
        "#360", _BASE | frozenset({"preflight"}),
        deprecated="in-place ingest was retired (#360); alias publication is always on — remove INGEST_ALIAS_PUBLISH",
    ),
    OperatorKey(
        "INGEST_EXTRA_PATCH", _SEC_INGEST, "path", "",
        "CI-rehearsal-only strategic-merge resource patch for job/ingest (requires CI_REHEARSAL=1).",
        "#1014", _BASE | frozenset({"preflight"}), ci_rehearsal_only=True,
    ),

    # --- Collection distribution policy ---
    OperatorKey(
        "QDRANT_SHARD_NUMBER", _SEC_POLICY, "int", 6,
        "Logical Qdrant shard count for corpus and completion collections (preset 6).",
        "#360", _EX_CHART, helm_path="ingest.collectionPolicy.shardNumber",
        chart_env=("QDRANT_SHARD_NUMBER",), min_value=1,
    ),
    OperatorKey(
        "QDRANT_REPLICATION_FACTOR", _SEC_POLICY, "int", 3,
        "Qdrant shard replication factor (preset 3).",
        "#360", _EX_CHART, helm_path="ingest.collectionPolicy.replicationFactor",
        chart_env=("QDRANT_REPLICATION_FACTOR",), min_value=1,
    ),
    OperatorKey(
        "QDRANT_WRITE_CONSISTENCY_FACTOR", _SEC_POLICY, "int", 2,
        "Required Qdrant write acknowledgements (preset 2; must be <= QDRANT_REPLICATION_FACTOR).",
        "#360", _EX_CHART, helm_path="ingest.collectionPolicy.writeConsistencyFactor",
        chart_env=("QDRANT_WRITE_CONSISTENCY_FACTOR",), min_value=1,
    ),
    OperatorKey(
        "QDRANT_PEER_URLS", _SEC_POLICY, "str", "",
        "Comma-separated direct per-pod Qdrant REST URLs for RF>1 placement verification.",
        "#360", _EX_CHART, helm_path="ingest.peerUrls", chart_env=("QDRANT_PEER_URLS",),
    ),

    # --- Forbid list ---
    OperatorKey(
        "EMBED_MODE", _SEC_FORBID, "enum", "vllm",
        "Embedding mode; hash is CI/dev only and always refused in air-gap manifests.",
        "#8", _EX_PRE, allowed_values=("vllm",),
    ),

    # --- Gateway TLS trust bundle ---
    OperatorKey(
        "GATEWAY_CA_CONFIGMAP", _SEC_GW_CA, "str", "",
        "Optional ConfigMap name in NAMESPACE holding key ca-bundle.crt for gateway TLS trust.",
        "#245", _EX_MAP, helm_path="gateway.caConfigMapName",
    ),

    # --- CLI / stage controls ---
    OperatorKey(
        "AIRGAP_ENV", _SEC_CLI, "path", "",
        "Explicit path to operator env file (must be a readable regular file when set; default ./airgap.env).",
        "#478", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "AIRGAP_DRYRUN", _SEC_CLI, "bool", False,
        "Preview/render-only interlock (1/true/yes previews without cluster mutation; 0/false/no runs live).",
        "#1015", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "KC", _SEC_CLI, "str", "",
        "Kubernetes CLI override (defaults to kubectl if present, else oc).",
        "#15", _BASE | frozenset({"preflight"}),
    ),

    # --- Bundle packing and transfer ---
    OperatorKey(
        "AIRGAP_APP_REGISTRY", _SEC_PACK, "str", "",
        "Source app image registry override for pack.sh (defaults to ghcr.io/<GHCR_OWNER>).",
        "#15", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "GHCR_OWNER", _SEC_PACK, "str", "",
        "GitHub Container Registry owner for pack.sh (inferred from git origin when unset).",
        "#15", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "AIRGAP_TASK_ARCHIVE", _SEC_PACK, "path", "",
        "Local path to pinned Task archive used by pack.sh instead of downloading.",
        "#402", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "SNEAKERNET_SIGNING_KEY", _SEC_PACK, "path", "",
        "PEM private signing key path used by pack.sh on the connected pack host.",
        "#15", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "SNEAKERNET_KEY_TRUSTED", _SEC_PACK, "bool", False,
        "Mark pack.sh bundle MANIFEST signed: true when using a production-custody signing key.",
        "#15", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "AIRGAP_BUNDLE_DIR", _SEC_PACK, "path", "",
        "Directory holding unpacked bundle artifacts for load.sh (default ./dist or ..).",
        "#15", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "AIRGAP_WORKSPACE", _SEC_PACK, "path", "qdrant-pdf-rag",
        "Target directory cloned from repo.bundle by bootstrap.sh.",
        "#15", _BASE | frozenset({"preflight"}),
    ),

    # --- Smoke and trace verification ---
    OperatorKey(
        "QUERY", _SEC_SMOKE, "str", "IEA500I operator message",
        "Literal search query sent by smoke.sh against /v1/search.",
        "#15", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "TRACE_QUERY_URL", _SEC_SMOKE, "str", "",
        "Trace backend query URL for smoke.sh (defaults to http://openobserve:5080 when bundled).",
        "#956", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "TRACE_QUERY_USER", _SEC_SMOKE, "str", "",
        "Trace backend query username for smoke.sh (read from openobserve-credentials Secret when bundled).",
        "#956", _BASE | frozenset({"preflight"}),
    ),
    OperatorKey(
        "TRACE_QUERY_PASSWORD", _SEC_SMOKE, "str", "",
        "Trace backend query password for smoke.sh (read from Secret when bundled; never printed).",
        "#956", _BASE | frozenset({"preflight"}), sensitive=True,
    ),
    OperatorKey(
        "TRACE_TIMEOUT", _SEC_SMOKE, "int", 60,
        "Seconds smoke.sh waits for a v1.search trace span to land in OpenObserve.",
        "#956", _BASE | frozenset({"preflight"}), min_value=1,
    ),
)

OPERATOR_KEYS_BY_NAME: dict[str, OperatorKey] = {entry.name: entry for entry in OPERATOR_KEYS}


def operator_env_keys() -> tuple[str, ...]:
    """Sorted canonical OPERATOR_ENV_KEYS list for `scripts/airgap/common.sh`."""
    return tuple(sorted(entry.name for entry in OPERATOR_KEYS if "shell_snapshot" in entry.surfaces))


def retired_key_names() -> tuple[str, ...]:
    """Sorted canonical retired key names (from `model_config.RETIRED_KEYS`)."""
    return tuple(sorted(key for key, _, _ in RETIRED_KEYS))


def task_bridge_keys() -> tuple[str, ...]:
    """Sorted canonical TASK_OP_KEYS list for `taskfiles/airgap.yml` (operator + retired keys)."""
    names = {entry.name for entry in OPERATOR_KEYS if "task_bridge" in entry.surfaces}
    names.update(retired_key_names())
    return tuple(sorted(names))


def example_keys() -> tuple[str, ...]:
    """Sorted operator keys documented in `airgap.env.example`."""
    return tuple(sorted(entry.name for entry in OPERATOR_KEYS if "example" in entry.surfaces))


def render_operator_env_keys_line() -> str:
    """Exact `OPERATOR_ENV_KEYS="..."` assignment line for `scripts/airgap/common.sh`."""
    return f'OPERATOR_ENV_KEYS="{" ".join(operator_env_keys())}"'


def render_taskfile_bridge() -> str:
    """Exact `env: &operator_env` block for `taskfiles/airgap.yml` (mechanical Verifier diff)."""
    keys = task_bridge_keys()
    lines = [
        "    env: &operator_env",
        f'      TASK_OP_KEYS: "{" ".join(keys)}"',
    ]
    for key in keys:
        lines.append(f"      TASK_{key}: '{{{{.{key}}}}}'")
    return "\n".join(lines)


def check_sync(root: Path) -> list[str]:
    """Verify every declared surface matches this registry. Returns list of drift findings."""
    findings: list[str] = []

    # 1. common.sh OPERATOR_ENV_KEYS
    common_path = root / "scripts" / "airgap" / "common.sh"
    common_text = common_path.read_text(encoding="utf-8")
    m_common = re.search(r'^OPERATOR_ENV_KEYS="([^"]+)"', common_text, re.MULTILINE)
    if not m_common:
        findings.append("scripts/airgap/common.sh: missing OPERATOR_ENV_KEYS assignment")
    else:
        have = m_common.group(1).split()
        want = list(operator_env_keys())
        if have != want:
            missing = sorted(set(want) - set(have))
            extra = sorted(set(have) - set(want))
            if missing or extra:
                findings.append(f"scripts/airgap/common.sh: OPERATOR_ENV_KEYS mismatch (missing={missing}, extra={extra})")
            else:
                findings.append("scripts/airgap/common.sh: OPERATOR_ENV_KEYS is not in canonical sorted order")

    # 1b. common.sh _RETIRED_ENV_KEYS — the exported retired-key guard must
    # match model_config.RETIRED_KEYS exactly, not a hand-kept copy: retiring
    # a key in model_config without updating common.sh would silently stop
    # file-only retired settings from being exported (and refused) at all.
    m_retired = re.search(r'^_RETIRED_ENV_KEYS="([^"]+)"', common_text, re.MULTILINE)
    if not m_retired:
        findings.append("scripts/airgap/common.sh: missing _RETIRED_ENV_KEYS assignment")
    else:
        have_retired = m_retired.group(1).split()
        want_retired = list(retired_key_names())
        if sorted(have_retired) != want_retired:
            missing_retired = sorted(set(want_retired) - set(have_retired))
            extra_retired = sorted(set(have_retired) - set(want_retired))
            findings.append(
                "scripts/airgap/common.sh: _RETIRED_ENV_KEYS mismatch "
                f"(missing={missing_retired}, extra={extra_retired})"
            )

    # 2. taskfiles/airgap.yml bridge block
    taskfile_path = root / "taskfiles" / "airgap.yml"
    taskfile_text = taskfile_path.read_text(encoding="utf-8")
    expected_block = render_taskfile_bridge()
    if expected_block not in taskfile_text:
        findings.append(
            "taskfiles/airgap.yml: operator_env bridge block does not match `operator_keys.py --taskfile-bridge`"
        )

    # 3. airgap.env.example keys and section headers
    example_path = root / "airgap.env.example"
    example_text = example_path.read_text(encoding="utf-8")
    have_example = set(re.findall(r"(?m)^#?[ \t]*([A-Z][A-Z0-9_]*)=.*", example_text))
    want_example = set(example_keys()) | set(ci_only_example_keys(root))
    if have_example != want_example:
        missing = sorted(want_example - have_example)
        extra = sorted(have_example - want_example)
        findings.append(f"airgap.env.example: key mismatch (missing={missing}, extra={extra})")
    have_sections = set(re.findall(r"(?m)^# --- (.+?) ---$", example_text))
    for entry in OPERATOR_KEYS:
        if "example" in entry.surfaces and entry.section not in have_sections:
            findings.append(f"airgap.env.example: missing section header '# --- {entry.section} ---' for {entry.name}")

    # 4. tests/fixtures/extension-surface.json
    ext_path = root / "tests" / "fixtures" / "extension-surface.json"
    if ext_path.is_file():
        ext = json.loads(ext_path.read_text(encoding="utf-8"))
        have_ext = set(ext.get("current", {}).get("airgap_env_keys", []))
        if have_ext != want_example:
            missing = sorted(want_example - have_ext)
            extra = sorted(have_ext - want_example)
            findings.append(
                f"tests/fixtures/extension-surface.json: airgap_env_keys mismatch (missing={missing}, extra={extra})"
            )
        have_ret = set(ext.get("current", {}).get("retired_env_keys", []))
        want_ret = set(retired_key_names())
        if have_ret != want_ret:
            findings.append(
                f"tests/fixtures/extension-surface.json: retired_env_keys mismatch "
                f"(missing={sorted(want_ret - have_ret)}, extra={sorted(have_ret - want_ret)})"
            )

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Air-gap operator configuration key registry (#1284).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--operator-env-keys", action="store_true", help="Print canonical common.sh OPERATOR_ENV_KEYS line.")
    group.add_argument("--taskfile-bridge", action="store_true", help="Print canonical taskfiles/airgap.yml operator_env block.")
    group.add_argument("--check", action="store_true", help="Verify all surfaces match the registry.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2], help="Repository root.")
    args = parser.parse_args(argv)

    if args.operator_env_keys:
        print(render_operator_env_keys_line())
        return 0
    if args.taskfile_bridge:
        print(render_taskfile_bridge())
        return 0
    if args.check:
        findings = check_sync(args.root)
        if findings:
            for item in findings:
                print(f"FAIL: {item}", file=sys.stderr)
            return 1
        print(f"OK: {len(OPERATOR_KEYS)} operator keys in sync across all surfaces")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
