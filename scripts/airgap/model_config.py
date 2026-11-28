"""Pure validation of resolved nonsecret model configuration for air-gap launchers."""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import sys
from collections.abc import Mapping
from fractions import Fraction
from urllib.parse import urlsplit

REASONING_INTEGER_FIELDS = {
    "LLM_MAX_MODEL_LEN": ("maxModelLen", 512, 262144),
    "LLM_RESERVED_OUTPUT_TOKENS": ("reservedOutputTokens", 128, 16384),
    "LLM_THINKING_RESERVE_TOKENS_SIMPLE": ("thinkingReserveTokensSimple", 0, 16384),
    "LLM_THINKING_RESERVE_TOKENS_COMPLEX": ("thinkingReserveTokensComplex", 0, 16384),
    # Chat history caps (#1268): prior turns kept, characters kept per prior turn.
    "CHAT_MAX_TURNS": ("chatMaxTurns", 1, 50),
    "CHAT_MAX_PRIOR_TURN_CHARS": ("chatMaxPriorTurnChars", 200, 5000),
    # Retrieval depth (#1191): hits per answer, chunks kept per manual.
    "ANSWER_HIT_LIMIT": ("answerHitLimit", 1, 32),
    "RETRIEVE_MAX_CHUNKS_PER_DOC": ("retrieveMaxChunksPerDoc", 1, 10),
    # Tokens priced per pasted image (#1287): config.Settings bounds.
    "LLM_IMAGE_TOKEN_RESERVE": ("imageTokenReserve", 256, 65536),
    # Pasted-image limits at the agent (#1287 decision 7; operator keys since
    # #1368): config.Settings bounds. Only the agent hop reads them.
    "CHAT_IMAGE_MAX_BYTES": ("chatImageMaxBytes", 1024, 20971520),
    "CHAT_IMAGE_MAX_COUNT": ("chatImageMaxCount", 1, 8),
    "CHAT_IMAGE_MAX_PIXELS": ("chatImageMaxPixels", 4096, 33554432),
}
REASONING_EFFORT_FIELDS = {
    "LLM_REASONING_EFFORT_SIMPLE": "reasoningEffortSimple",
    "LLM_REASONING_EFFORT_COMPLEX": "reasoningEffortComplex",
}
REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
# Mirrors config.Settings defaults; tests pin both sides (#748).
REASONING_EFFORT_DEFAULTS = {"LLM_REASONING_EFFORT_SIMPLE": "medium", "LLM_REASONING_EFFORT_COMPLEX": "medium",
                             "LLM_REASONING_EFFORTS": "low,medium,xhigh"}
# Model request arguments (#1193). Mirrors mainframe_rag.model_args, which the
# bastion preflight cannot import; tests pin the constants and the refusals of
# both sides in step. Empty keeps the application default (the earlier request).
EXTRA_BODY_KEY = "LLM_EXTRA_BODY"
EXTRA_BODY_MAX_CHARS = 4096
EXTRA_BODY_PROTECTED_KEYS = ("model", "messages", "stream", "stream_options")
EXTRA_BODY_OWNED_KEYS = {
    "max_tokens": "LLM_RESERVED_OUTPUT_TOKENS and the thinking reserves",
    "max_completion_tokens": "LLM_RESERVED_OUTPUT_TOKENS and the thinking reserves",
    "temperature": "LLM_TEMPERATURE",
    "reasoning_effort": "LLM_EFFORT_TRANSPORT and the effort settings",
}
EFFORT_TRANSPORT_KEY = "LLM_EFFORT_TRANSPORT"
EFFORT_TRANSPORTS = ("top_level", "chat_template", "none")
MODEL_ARGUMENT_ENV_KEYS = (EXTRA_BODY_KEY, EFFORT_TRANSPORT_KEY)
# Prompt-count source (#1266). Mirrors config.TOKEN_COUNT_SOURCES; a test pins
# both sides. Empty keeps the application default (auto since #1274).
TOKEN_COUNT_SOURCE_KEY = "LLM_TOKEN_COUNT_SOURCE"
TOKEN_COUNT_SOURCES = ("tokenize", "completion_usage", "auto", "estimate")
# Antecedent-anchored follow-up retrieval (#1268): optional boolean, exclusive
# with CHAT_CONDENSE_ENABLED. The refusal mirrors config.CHAT_FOLLOWUP_MODES_CONFLICT
# (a test pins both sides).
FOLLOWUP_ANCHOR_KEY = "CHAT_FOLLOWUP_ANCHOR_ENABLED"
CHAT_FOLLOWUP_MODES_CONFLICT = (
    "CHAT_CONDENSE_ENABLED and CHAT_FOLLOWUP_ANCHOR_ENABLED are mutually exclusive; enable at most one"
)
# Coding mode (#1276): optional boolean, default on in Settings; empty keeps it.
CODING_MODE_KEY = "ANSWER_CODING_MODE_ENABLED"
# SPL mode (#1293): optional boolean, default off in Settings; empty keeps it.
SPL_MODE_KEY = "ANSWER_SPL_MODE_ENABLED"
# Pasted chat images (#1287): optional boolean, default off in Settings; empty keeps it.
CHAT_IMAGES_KEY = "CHAT_IMAGES_ENABLED"
# Candidate selection (#1309) and neighbour expansion (#1314): optional
# booleans, default on in Settings since #1368; empty keeps it.
SELECT_KEY = "ANSWER_SELECT_ENABLED"
EXPAND_KEY = "ANSWER_EXPAND_ENABLED"
REASONING_ENV_KEYS = (*REASONING_INTEGER_FIELDS, *REASONING_EFFORT_FIELDS, "LLM_REASONING_EFFORTS", "LLM_STREAM",
                      *MODEL_ARGUMENT_ENV_KEYS, TOKEN_COUNT_SOURCE_KEY, FOLLOWUP_ANCHOR_KEY, CODING_MODE_KEY,
                      SPL_MODE_KEY, CHAT_IMAGES_KEY, SELECT_KEY, EXPAND_KEY)

# Request admission and total deadline (#374 handoff, #843 D1). Empty keeps the
# chart default (deadline 270; admission 4/4/30, #843; body cap/budget 24/128 MiB, #1352); zero is an explicit selection. Limits match config.Settings and the
# chart schema. The route timeout is the mapper's fixed 300 s.
ROUTE_TIMEOUT_SECONDS = 300
REQUEST_INTEGER_FIELDS = {
    "REQUEST_DEADLINE_S": ("requestDeadlineSeconds", 0, 3600),
    "REQUEST_MAX_CONCURRENT": ("requestMaxConcurrent", 0, 1000),
    "REQUEST_QUEUE_MAX": ("requestQueueMax", 0, 1000),
    # Request-body bounds (#1352), bytes per agent replica; budget >= cap.
    "REQUEST_MAX_BODY_BYTES": ("requestMaxBodyBytes", 1048576, 268435456),
    "REQUEST_BODY_BUDGET_BYTES": ("requestBodyBudgetBytes", 1048576, 4294967296),
}
# Chart defaults (charts/mainframe-rag/values.yaml, a test pins them) that the
# budget >= cap rule compares an empty side against.
REQUEST_BODY_DEFAULTS = {"requestMaxBodyBytes": 25165824, "requestBodyBudgetBytes": 134217728}
REQUEST_WAIT_KEY = "REQUEST_QUEUE_WAIT_S"  # chart requestQueueWaitSeconds, 0..300, decimal
REQUEST_ENV_KEYS = (*REQUEST_INTEGER_FIELDS, REQUEST_WAIT_KEY)

MODEL_CONFIG_KEYS = (
    "GATEWAY_BASE_URL", "EMBED_BASE_URL", "EMBED_MODEL",
    "LLM_BASE_URL", "LLM_MODEL_REASONING", "RERANK_BASE_URL",
    "CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL", "CONTEXTUAL_EMBED_ENABLED",
    *REASONING_ENV_KEYS,
    *REQUEST_ENV_KEYS,
)

# Operator booleans that fail closed on any non-boolean spelling. One parser
# for preflight and the values mapper (map_values.strict_bool wraps it).
OPERATOR_BOOL_KEYS = (
    "UI_ENABLED", "CONTEXTUAL_EMBED_ENABLED", "INGEST_ALIAS_PUBLISH",
    "INGEST_REINGEST", "INGEST_AUTHORIZE_FROM_CATALOG", "CHAT_CONDENSE_ENABLED", "LLM_STREAM", "AGENT_NETWORK_POLICY",
    "FEEDBACK_ENABLED", "OPENWEBUI_ENABLED", "CHAT_FOLLOWUP_ANCHOR_ENABLED", "ANSWER_CODING_MODE_ENABLED",
    "ANSWER_SPL_MODE_ENABLED", "OPENWEBUI_RATINGS_ENABLED", "CHAT_IMAGES_ENABLED",
    "ANSWER_SELECT_ENABLED", "ANSWER_EXPAND_ENABLED", "SPLUNK_GATEWAY_ENABLED",
    "ZOS_READ_ENABLED", "ZOS_READ_DRY_RUN", "ZOS_FTP_PLAIN_ACCEPTED",
)

# Agent ingress NetworkPolicy (issue #373): default ON; the allow-list names
# extra namespaces that may reach the agent HTTP port.
NETWORK_POLICY_KEY = "AGENT_NETWORK_POLICY"
NETWORK_POLICY_NAMESPACES_KEY = "AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES"
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


def parse_strict_bool(name: str, raw: str, default: bool) -> bool:
    """Unset/empty keeps the default; true/1/yes and false/0/no (any case)
    are accepted. The diagnostic names the key, never the value."""
    if raw == "":
        return default
    low = raw.lower()
    if low in ("true", "1", "yes"):
        return True
    if low in ("false", "0", "no"):
        return False
    raise ValueError(f"{name} must be true/false")


# Operator keys that earlier releases honoured and the current code ignores
# (#1070). Ignoring them silently would let an upgrade run with a different
# registry, namespace, embedding origin or tracing than the operator wrote, so
# any non-empty value refuses. (key, retiring commit, fixed migration text).
RETIRED_KEYS: tuple[tuple[str, str, str], ...] = (
    ("REGISTRY_INTERNAL", "547d04d", "set INTERNAL_REGISTRY and remove it"),
    ("OPENSHIFT_NAMESPACE", "547d04d", "set NAMESPACE and remove it"),
    (
        "VLLM_BASE_URL",
        "547d04d",
        ("set EMBED_BASE_URL to the old value plus /v1 (clients append /embeddings to a base "
         "that must end in /v1; do not repeat /v1), then remove it"),
    ),
    (
        "JAEGER_ENABLED",
        "d7765dc",
        "remove it; OTEL_EXPORTER_OTLP_ENDPOINT selects the collector (off disables tracing)",
    ),
    (
        "JAEGER_QUERY_URL",
        "d7765dc",
        "remove it; airgap:smoke reads TRACE_QUERY_URL, TRACE_QUERY_USER and TRACE_QUERY_PASSWORD",
    ),
)


# d53595e's example pointed OTEL_EXPORTER_OTLP_ENDPOINT at the bundled Jaeger
# Service. Any set endpoint now means an external collector (no OpenObserve),
# so that value would drop every trace silently (#1070).
OTEL_ENDPOINT_KEY = "OTEL_EXPORTER_OTLP_ENDPOINT"
_RETIRED_JAEGER_HOST = re.compile(r"jaeger(\.[a-z0-9-]+\.svc(\.[a-z0-9.-]*)?)?")
_JAEGER_ENDPOINT_MESSAGE = (
    "points at the retired in-cluster Jaeger; unset it to use the bundled OpenObserve "
    "collector, or set your external collector"
)


def _points_at_retired_jaeger(raw: str) -> bool:
    candidate = raw.strip()
    if "://" not in candidate:
        return False  # not a URL: the endpoint validator refuses it with its own message
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return False
    return _RETIRED_JAEGER_HOST.fullmatch(host.lower().rstrip(".")) is not None


def require_no_retired_keys(values: Mapping[str, str]) -> None:
    """Refuse every retired key that carries a non-empty value, and a tracing
    endpoint naming the retired in-cluster Jaeger Service. One fixed message
    lists them all; values are never echoed. Empty or absent is fine."""
    lines = [
        f"  {key} (retired in {ref}): {text}"
        for key, ref, text in RETIRED_KEYS
        if values.get(key, "") != ""
    ]
    if _points_at_retired_jaeger(values.get(OTEL_ENDPOINT_KEY, "")):
        lines.append(f"  {OTEL_ENDPOINT_KEY} (retired in d7765dc): {_JAEGER_ENDPOINT_MESSAGE}")
    if lines:
        raise ValueError(
            "retired airgap.env keys are set and no longer read; apply the migration "
            "or empty/remove each key before any cluster change:\n" + "\n".join(lines)
        )


def require_in_place_retired(raw: str) -> None:
    """In-place ingest was retired (#360): INGEST_ALIAS_PUBLISH may be unset
    or true, and an explicit false fails closed instead of being ignored."""
    if not parse_strict_bool("INGEST_ALIAS_PUBLISH", raw, True):
        raise ValueError(
            "in-place ingest was retired (#360): alias publication is the only "
            "ingest mode; unset INGEST_ALIAS_PUBLISH"
        )


def network_policy(values: Mapping[str, str]) -> tuple[bool, list[str]]:
    """(enabled, allowed namespaces) for the agent ingress NetworkPolicy.

    The list is comma-separated DNS-1123 labels with no spaces, empty entries
    or repeats; anything else fails closed (a typo must not silently widen or
    narrow the policy). A list with the policy disabled is contradictory.
    Diagnostics name the key, never the value."""
    enabled = parse_strict_bool(NETWORK_POLICY_KEY, values.get(NETWORK_POLICY_KEY, ""), True)
    raw = values.get(NETWORK_POLICY_NAMESPACES_KEY, "")
    names = raw.split(",") if raw != "" else []
    if any(_DNS_LABEL.fullmatch(name) is None for name in names):
        raise ValueError(
            f"{NETWORK_POLICY_NAMESPACES_KEY} must be comma-separated namespace names "
            "(lowercase DNS-1123 labels, no spaces)"
        )
    if len(set(names)) != len(names):
        raise ValueError(f"{NETWORK_POLICY_NAMESPACES_KEY} must not repeat a namespace")
    if names and not enabled:
        raise ValueError(
            f"{NETWORK_POLICY_NAMESPACES_KEY} requires {NETWORK_POLICY_KEY} to be enabled: "
            "refusing an allow-list for a policy that is not rendered"
        )
    return enabled, names


# Answer-feedback service (issue #804, ADR-0005), default OFF. The enabled flag
# is a strict boolean; the secret is the operator-created Secret holding the
# bearer token; size and retention are the operator's data decisions. One
# validation owner for validate.sh and map_values.py, like network_policy.
FEEDBACK_DEFAULT_STORAGE_SIZE = "10Gi"
FEEDBACK_DEFAULT_RETENTION_DAYS = 180  # ADR-0005; Settings bounds 1..3650
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_QUANTITY = re.compile(r"[1-9][0-9]*(?:Mi|Gi|Ti)")


def feedback_config(values: Mapping[str, str]) -> dict[str, object] | None:
    """The feedback service selection, or None while it is off.

    Shapes are checked whenever a key is set (a typo must not wait for the day
    the feature is enabled); the secret is required only when enabled. The
    store's ingress limit is the agent NetworkPolicy, so enabling the service
    without it fails closed (the bearer token alone would be the only control).
    Diagnostics name the key, never the value."""
    enabled = parse_strict_bool("FEEDBACK_ENABLED", values.get("FEEDBACK_ENABLED", ""), False)
    secret = values.get("FEEDBACK_SECRET", "")
    if secret and (len(secret) > 253 or _DNS_SUBDOMAIN.fullmatch(secret) is None):
        raise ValueError("FEEDBACK_SECRET must be a DNS-subdomain Secret name")
    size = values.get("FEEDBACK_STORAGE_SIZE", "") or FEEDBACK_DEFAULT_STORAGE_SIZE
    if _QUANTITY.fullmatch(size) is None:
        raise ValueError("FEEDBACK_STORAGE_SIZE must be a quantity such as 10Gi (Mi, Gi or Ti)")
    raw_days = values.get("FEEDBACK_RETENTION_DAYS", "")
    days = FEEDBACK_DEFAULT_RETENTION_DAYS
    if raw_days != "":
        if re.fullmatch(r"[0-9]+", raw_days) is None or not 1 <= int(raw_days) <= 3650:
            raise ValueError("FEEDBACK_RETENTION_DAYS must be an integer from 1 to 3650")
        days = int(raw_days)
    if not enabled:
        return None
    if not secret:
        raise ValueError("FEEDBACK_SECRET is required when FEEDBACK_ENABLED=true "
                         "(operator-created Secret with data key service-token)")
    if not parse_strict_bool(NETWORK_POLICY_KEY, values.get(NETWORK_POLICY_KEY, ""), True):
        raise ValueError(
            f"FEEDBACK_ENABLED=true requires {NETWORK_POLICY_KEY} to be enabled: the "
            "feedback store's agent-only ingress limit is the NetworkPolicy; refusing to "
            "expose it on a bearer token alone"
        )
    return {"enabled": True, "secretName": secret, "storageSize": size, "retentionDays": days}


# Optional Open WebUI docs front end (#1074, ADR-0007). Retention: maintainer
# decision 2026-10-24 (30 days); the chart's purge sidecar enforces it.
OPENWEBUI_DEFAULT_RETENTION_DAYS = 30
OPENWEBUI_DEFAULT_STORAGE_SIZE = "10Gi"
_HOSTNAME = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)+")
_PROVIDER_URL = re.compile(r"https://[^\s/]+(?:/\S*)?/\.well-known/openid-configuration")
_CLAIM = re.compile(r"[A-Za-z0-9_:/.-]+")
_SCOPES = re.compile(r"[A-Za-z0-9_:/.-]+(?: [A-Za-z0-9_:/.-]+)*")
# The roles-claim value the IdP issues to every signed-in user (docs/deploy.md).
OPENWEBUI_BASELINE_ROLE = "signed-in"
# The chart's scope set (values.yaml openwebui.oidc.scopes); AD FS sites set their own.
OPENWEBUI_DEFAULT_SCOPES = "openid email profile groups offline_access"
_NAME_LIST = re.compile(r"[^,\s]+(?:,[^,\s]+)*")
_CIDR = re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}/(?:[0-9]|[12][0-9]|3[0-2])")


_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----\r?\n(.*?)-----END \1-----", re.DOTALL)


def pem_certificates(text: str) -> bool:
    """True when text is one or more PEM CERTIFICATE blocks with only whitespace
    between them and OpenSSL loads every one (a key, stray text or a corrupted
    body that is still valid base64 fails). Used for the optional IdP CA bundle;
    the chart's ca-trust init step (files/openwebui/ca_trust.py) applies the same
    rule in the pod."""
    blocks = list(_PEM_BLOCK.finditer(text))
    if not blocks or _PEM_BLOCK.sub("", text).strip():
        return False
    for block in blocks:
        if block.group(1) != "CERTIFICATE":
            return False
        try:
            der = ssl.PEM_cert_to_DER_cert(block.group(0) + "\n")
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=der)
        except (ValueError, ssl.SSLError):
            return False
    return True


def openwebui_config(values: Mapping[str, str]) -> dict[str, object] | None:
    """The Open WebUI selection as chart values, or None while it is off.

    Shapes are checked whenever a key is set; the Secret, host, OIDC client,
    roles and IdP CIDRs are required only when enabled. The IdP egress is
    NetworkPolicy ipBlocks (a policy cannot match a DNS name), so at least one
    CIDR is required. Diagnostics name the key, never the value."""
    enabled = parse_strict_bool("OPENWEBUI_ENABLED", values.get("OPENWEBUI_ENABLED", ""), True)
    secret = values.get("OPENWEBUI_SECRET", "")
    if secret and (len(secret) > 253 or _DNS_SUBDOMAIN.fullmatch(secret) is None):
        raise ValueError("OPENWEBUI_SECRET must be a DNS-subdomain Secret name")
    host = values.get("OPENWEBUI_HOST", "")
    if host and (len(host) > 253 or _HOSTNAME.fullmatch(host) is None):
        raise ValueError("OPENWEBUI_HOST must be a lowercase DNS host name (no scheme or path)")
    raw_days = values.get("OPENWEBUI_RETENTION_DAYS", "")
    days = OPENWEBUI_DEFAULT_RETENTION_DAYS
    if raw_days != "":
        if re.fullmatch(r"[0-9]+", raw_days) is None or not 1 <= int(raw_days) <= 3650:
            raise ValueError("OPENWEBUI_RETENTION_DAYS must be an integer from 1 to 3650")
        days = int(raw_days)
    size = values.get("OPENWEBUI_STORAGE_SIZE", "") or OPENWEBUI_DEFAULT_STORAGE_SIZE
    if _QUANTITY.fullmatch(size) is None:
        raise ValueError("OPENWEBUI_STORAGE_SIZE must be a quantity such as 10Gi (Mi, Gi or Ti)")
    provider = values.get("OPENWEBUI_OIDC_PROVIDER_URL", "")
    if provider and _PROVIDER_URL.fullmatch(provider) is None:
        raise ValueError("OPENWEBUI_OIDC_PROVIDER_URL must be the https://.../.well-known/"
                         "openid-configuration URL of the identity team's client")
    client_id = values.get("OPENWEBUI_OIDC_CLIENT_ID", "")
    if client_id and (len(client_id) > 255 or re.search(r"[\s,]", client_id)):
        raise ValueError("OPENWEBUI_OIDC_CLIENT_ID must not contain spaces or commas")
    claims = {}
    for key, default in (("OPENWEBUI_OIDC_GROUP_CLAIM", "groups"),
                         ("OPENWEBUI_OIDC_ROLES_CLAIM", "roles"),
                         ("OPENWEBUI_OIDC_EMAIL_CLAIM", "email"),
                         ("OPENWEBUI_OIDC_USERNAME_CLAIM", "name")):
        claims[key] = values.get(key, "") or default
        if _CLAIM.fullmatch(claims[key]) is None:
            raise ValueError(f"{key} must be a claim name (letters, digits, _ : / . -)")
    scopes = values.get("OPENWEBUI_OIDC_SCOPES", "") or OPENWEBUI_DEFAULT_SCOPES
    if _SCOPES.fullmatch(scopes) is None or "openid" not in scopes.split(" "):
        raise ValueError("OPENWEBUI_OIDC_SCOPES must be space-separated scope names "
                         "including openid")
    roles = {}
    for key in ("OPENWEBUI_OIDC_ALLOWED_ROLES", "OPENWEBUI_OIDC_ADMIN_ROLES"):
        roles[key] = values.get(key, "")
        if roles[key] and _NAME_LIST.fullmatch(roles[key]) is None:
            raise ValueError(f"{key} must be a comma-separated list of role names")
        # Revocation through the IdP needs a roles claim on every sign-in, so the
        # IdP issues the baseline value signed-in to every user (docs/deploy.md,
        # AD FS). It, or *, in a role list would admit every signed-in user.
        if set(roles[key].split(",")) & {"*", OPENWEBUI_BASELINE_ROLE}:
            raise ValueError(f"{key} must not contain * or {OPENWEBUI_BASELINE_ROLE} (the baseline "
                             "role every signed-in user carries)")
    cidrs = [part.strip() for part in values.get("OPENWEBUI_IDP_EGRESS_CIDRS", "").split(",")
             if part.strip()]
    for cidr in cidrs:
        if _CIDR.fullmatch(cidr) is None or any(int(o) > 255 for o in cidr.split("/")[0].split(".")):
            raise ValueError("OPENWEBUI_IDP_EGRESS_CIDRS must be comma-separated IPv4 CIDRs")
    if len(set(cidrs)) != len(cidrs):
        raise ValueError("OPENWEBUI_IDP_EGRESS_CIDRS must not repeat a CIDR")
    raw_port = values.get("OPENWEBUI_IDP_EGRESS_PORT", "") or "443"
    if re.fullmatch(r"[0-9]{1,5}", raw_port) is None or not 1 <= int(raw_port) <= 65535:
        raise ValueError("OPENWEBUI_IDP_EGRESS_PORT must be a TCP port from 1 to 65535")
    # Ratings (#1255): unset writes no chart value, so the chart follows
    # feedback.enabled (#1274: on wherever the feedback store is on); an
    # explicit spelling is carried, false included. True needs the store
    # (the chart refuses it too).
    raw_ratings = values.get("OPENWEBUI_RATINGS_ENABLED", "")
    ratings = (None if raw_ratings == ""
               else parse_strict_bool("OPENWEBUI_RATINGS_ENABLED", raw_ratings, False))
    if ratings and not parse_strict_bool("FEEDBACK_ENABLED", values.get("FEEDBACK_ENABLED", ""), False):
        raise ValueError("OPENWEBUI_RATINGS_ENABLED=true requires FEEDBACK_ENABLED=true: Open WebUI "
                         "ratings are stored only by the agent's feedback store (ADR-0005)")
    if not enabled:
        return None
    required = {"OPENWEBUI_SECRET": secret, "OPENWEBUI_HOST": host,
                "OPENWEBUI_OIDC_PROVIDER_URL": provider, "OPENWEBUI_OIDC_CLIENT_ID": client_id,
                **roles, "OPENWEBUI_IDP_EGRESS_CIDRS": cidrs}
    missing = [key for key, value in required.items() if not value]
    if missing:
        # The service CA is not an operator input on the live path (deploy.sh
        # fetches it); map_values enforces it only for dry-run rendering.
        # One fixed message: key names only, never values.
        raise ValueError("Open WebUI is enabled by default (ADR-0008) but required inputs are "
                         f"missing: {', '.join(missing)}. Set them, or opt out with "
                         "OPENWEBUI_ENABLED=false")
    return {
        "enabled": True,
        "secretName": secret,
        "host": host,
        "retentionDays": days,
        "storageSize": size,
        "oidc": {
            "providerUrl": provider,
            "clientId": client_id,
            "groupClaim": claims["OPENWEBUI_OIDC_GROUP_CLAIM"],
            "rolesClaim": claims["OPENWEBUI_OIDC_ROLES_CLAIM"],
            "allowedRoles": roles["OPENWEBUI_OIDC_ALLOWED_ROLES"],
            "adminRoles": roles["OPENWEBUI_OIDC_ADMIN_ROLES"],
            "scopes": scopes,
            "emailClaim": claims["OPENWEBUI_OIDC_EMAIL_CLAIM"],
            "usernameClaim": claims["OPENWEBUI_OIDC_USERNAME_CLAIM"],
        },
        "idpEgress": {"cidrs": cidrs, "port": int(raw_port)},
        **({} if ratings is None else {"ratings": {"enabled": ratings}}),
    }


# Team tools (#1077 part 2, ADR-0015): the Splunk gateway behind Open WebUI,
# default OFF. One validation owner for validate.sh (model_config main),
# airgap:config check and map_values.py, like openwebui_config. The chart
# mounts the site's gateway configuration and Splunk CA from one ConfigMap at
# a fixed path, so the configuration must name that path; the gateway's own
# start-up checks (timeout ordering, auth in oidc mode, the token-exchange
# variable) are repeated here so a refusal stops before any cluster change
# instead of as a crash-looping pod. Diagnostics name keys, never values.
SPLUNK_GATEWAY_CA_PATH = "/etc/mainframe-rag/splunk-gateway/ca-bundle.crt"
SPLUNK_TOKEN_EXCHANGE_SECRET_ENV = "SPLUNK_GATEWAY_TOKEN_EXCHANGE_SECRET"
SPLUNK_AUTH_MODES = ("oidc", "bearer")
# Gateway defaults (mainframe_rag.gateways.splunk: SplunkBackend.timeout_s and
# LIMIT_DEFAULTS["tool_timeout_s"]); the bastion cannot import them.
SPLUNK_BACKEND_TIMEOUT_DEFAULT = 50
SPLUNK_TOOL_TIMEOUT_DEFAULT = 60
_GROUP_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _cidr_list(key: str, raw: str) -> list[str]:
    cidrs = [part.strip() for part in raw.split(",") if part.strip()]
    for cidr in cidrs:
        if _CIDR.fullmatch(cidr) is None or any(int(o) > 255 for o in cidr.split("/")[0].split(".")):
            raise ValueError(f"{key} must be comma-separated IPv4 CIDRs")
    if len(set(cidrs)) != len(cidrs):
        raise ValueError(f"{key} must not repeat a CIDR")
    return cidrs


def _tcp_port(key: str, raw: str, default: int) -> int:
    raw = raw or str(default)
    if re.fullmatch(r"[0-9]{1,5}", raw) is None or not 1 <= int(raw) <= 65535:
        raise ValueError(f"{key} must be a TCP port from 1 to 65535")
    return int(raw)


def _site_text(key: str, path: str) -> str:
    """A site file's exact text (bytes decoded as UTF-8; line endings kept)."""
    try:
        with open(path, "rb") as handle:
            text = handle.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise ValueError(f"{key} is unreadable (a UTF-8 text file is required)") from None
    if not text.strip():
        raise ValueError(f"{key} is blank")
    return text


def _positive_number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _splunk_gateway_config(text: str, mode: str) -> None:
    """The deploy-relevant rules of the site's gateway configuration."""
    key = "SPLUNK_GATEWAY_CONFIG_FILE"
    try:
        doc = json.loads(text, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        raise ValueError(f"{key} must hold one JSON object with unique, nonempty keys") from None
    if not isinstance(doc, dict):
        raise ValueError(f"{key} must hold one JSON object with unique, nonempty keys")
    if doc.get("gateway") != "splunk":
        raise ValueError(f'{key} must set "gateway" to "splunk"')
    backend = doc.get("backend")
    if not isinstance(backend, dict) or backend.get("ca_file") != SPLUNK_GATEWAY_CA_PATH:
        raise ValueError(f"{key} backend.ca_file must be {SPLUNK_GATEWAY_CA_PATH} "
                         "(where the chart mounts SPLUNK_GATEWAY_CA_FILE)")
    limits = doc.get("limits")
    limits = {} if limits is None else limits
    if not isinstance(limits, dict):
        raise ValueError(f"{key} limits must be an object")
    tool_timeout = limits.get("tool_timeout_s", SPLUNK_TOOL_TIMEOUT_DEFAULT)
    if not _positive_number(tool_timeout):
        raise ValueError(f"{key} limits.tool_timeout_s must be a positive number")
    timeout = backend.get("timeout_s", SPLUNK_BACKEND_TIMEOUT_DEFAULT)
    if not _positive_number(timeout) or not timeout < 60 or not timeout < tool_timeout:
        raise ValueError(f"{key} backend.timeout_s must be above 0, below 60 and below "
                         f"limits.tool_timeout_s (defaults {SPLUNK_BACKEND_TIMEOUT_DEFAULT} "
                         f"and {SPLUNK_TOOL_TIMEOUT_DEFAULT})")
    if mode == "oidc" and not isinstance(doc.get("auth"), dict):
        raise ValueError(f'{key} must have an "auth" object in oidc mode (SPLUNK_GATEWAY_AUTH_MODE)')
    exchange = doc.get("token_exchange")
    if exchange is not None and (not isinstance(exchange, dict)
                                 or exchange.get("client_secret_env") != SPLUNK_TOKEN_EXCHANGE_SECRET_ENV):
        raise ValueError(f"{key} token_exchange.client_secret_env must be {SPLUNK_TOKEN_EXCHANGE_SECRET_ENV} "
                         "(the only variable the chart sets, from Secret key token-exchange-client-secret)")


def team_tools_config(values: Mapping[str, str]) -> dict[str, object] | None:
    """The chart's `teamTools` values, or None while the Splunk gateway is off.

    Shapes are checked whenever a key is set (a typo must not wait for the day
    the gateway is enabled); required keys and the site files only when it is
    enabled. Off writes nothing, so an existing deployment renders what it did."""
    enabled = parse_strict_bool("SPLUNK_GATEWAY_ENABLED", values.get("SPLUNK_GATEWAY_ENABLED", ""), False)
    group = values.get("TEAM_TOOLS_GROUP_ID", "")
    if group and _GROUP_ID.fullmatch(group) is None:
        raise ValueError("TEAM_TOOLS_GROUP_ID must be an Open WebUI group id (a lowercase UUID)")
    mode = values.get("SPLUNK_GATEWAY_AUTH_MODE", "") or "oidc"
    if mode not in SPLUNK_AUTH_MODES:
        raise ValueError("SPLUNK_GATEWAY_AUTH_MODE must be oidc or bearer")
    secret = values.get("SPLUNK_GATEWAY_SECRET", "")
    if secret and (len(secret) > 253 or _DNS_SUBDOMAIN.fullmatch(secret) is None):
        raise ValueError("SPLUNK_GATEWAY_SECRET must be a DNS-subdomain Secret name")
    model_cidrs = _cidr_list("TEAM_MODEL_EGRESS_CIDRS", values.get("TEAM_MODEL_EGRESS_CIDRS", ""))
    model_port = _tcp_port("TEAM_MODEL_EGRESS_PORT", values.get("TEAM_MODEL_EGRESS_PORT", ""), 443)
    splunk_cidrs = _cidr_list("SPLUNK_EGRESS_CIDRS", values.get("SPLUNK_EGRESS_CIDRS", ""))
    splunk_port = _tcp_port("SPLUNK_EGRESS_PORT", values.get("SPLUNK_EGRESS_PORT", ""), 8089)
    idp_cidrs = _cidr_list("SPLUNK_GATEWAY_IDP_EGRESS_CIDRS", values.get("SPLUNK_GATEWAY_IDP_EGRESS_CIDRS", ""))
    idp_port = _tcp_port("SPLUNK_GATEWAY_IDP_EGRESS_PORT", values.get("SPLUNK_GATEWAY_IDP_EGRESS_PORT", ""), 443)
    resources = _chart_resources(values, "SPLUNK_GATEWAY")
    if not enabled:
        return None
    if not parse_strict_bool("OPENWEBUI_ENABLED", values.get("OPENWEBUI_ENABLED", ""), True):
        raise ValueError("SPLUNK_GATEWAY_ENABLED=true requires OPENWEBUI_ENABLED=true: team tools are "
                         "reached only through Open WebUI")
    config_file = values.get("SPLUNK_GATEWAY_CONFIG_FILE", "")
    ca_file = values.get("SPLUNK_GATEWAY_CA_FILE", "")
    required: dict[str, object] = {
        "SPLUNK_GATEWAY_SECRET": secret, "SPLUNK_GATEWAY_CONFIG_FILE": config_file,
        "SPLUNK_GATEWAY_CA_FILE": ca_file, "SPLUNK_EGRESS_CIDRS": splunk_cidrs,
        "TEAM_MODEL_EGRESS_CIDRS": model_cidrs,
        "LLM_MODEL_REASONING": values.get("LLM_MODEL_REASONING", ""),
        "LLM_BASE_URL (or GATEWAY_BASE_URL)": values.get("LLM_BASE_URL", "") or values.get("GATEWAY_BASE_URL", ""),
        "GATEWAY_API_KEY_SECRET": values.get("GATEWAY_API_KEY_SECRET", ""),
    }
    if mode == "oidc":
        required["SPLUNK_GATEWAY_IDP_EGRESS_CIDRS"] = idp_cidrs
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError(f"SPLUNK_GATEWAY_ENABLED=true ({mode} mode) but required inputs are missing: "
                         f"{', '.join(missing)}. Set them, or keep SPLUNK_GATEWAY_ENABLED=false")
    config = _site_text("SPLUNK_GATEWAY_CONFIG_FILE", config_file)
    _splunk_gateway_config(config, mode)
    ca_bundle = _site_text("SPLUNK_GATEWAY_CA_FILE", ca_file)
    if not pem_certificates(ca_bundle):
        raise ValueError("SPLUNK_GATEWAY_CA_FILE must hold only PEM certificates that load "
                         "(-----BEGIN CERTIFICATE----- blocks, no key or other text)")
    return {
        "groupId": group,
        "model": {"egress": {"cidrs": model_cidrs, "port": model_port}},
        "splunk": {
            "enabled": True,
            "authMode": mode,
            "secretName": secret,
            "config": config,
            "caBundle": ca_bundle,
            "splunkEgress": {"cidrs": splunk_cidrs, "port": splunk_port},
            "idpEgress": {"cidrs": idp_cidrs, "port": idp_port},
            "resources": resources,
        },
    }


# Read-only z/OS dataset reads for the Docs chat (pilot; ADR-0012, #1294 PR 3),
# default OFF. One validation owner for validate.sh (model_config main),
# airgap:config check and map_values.py, like team_tools_config. It also owns
# the deploy-side refusals ADR-0012 decides (D3 guards (b) and (c), D5, D7);
# the chart repeats them for a direct helm render. FEEDBACK_ENABLED=true is
# not refused (D6, Option 7A: the agent suppresses observation turns instead).
# The bastion cannot import the bridge, so its name, prefix and tool rules are
# repeated here and pinned against it in tests/test_map_values.py.
# Diagnostics name keys, never roles, dataset names, addresses or paths.
ZOS_APPROVED_TOOLS = ("dataset_read", "job_status", "jes_spool_read")
ZOS_DEFAULT_TOOLS = ("dataset_read",)
ZOS_JES_TOOLS = ("job_status", "jes_spool_read")
ZOS_TRANSPORT_MODES = ("ftps", "plain")
ZOS_DEFAULT_ENCODING = "latin-1"
ZOS_DEFAULT_AUDIT_KEY = "audit-key"
_ZOS_QUALIFIER = re.compile(r"[A-Z#$@][A-Z0-9#$@-]{0,7}")
_ZOS_MEMBER = re.compile(r"[A-Z#$@][A-Z0-9#$@]{0,7}")
_ZOS_FORBIDDEN = frozenset("'\"*?%/\\")
_ZOS_HOST = re.compile(r"[A-Za-z0-9](?:[-A-Za-z0-9.]*[A-Za-z0-9])?")
_IPV4 = re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}")
_ZOS_ENCODING = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_ZOS_GUARD_C = ("z/OS reads are limited to signed-in team members (ADR-0012 D3 guard (c)); change it, "
                "or keep ZOS_READ_ENABLED=false")


def _zos_items(raw: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,\n]", raw) if item.strip()]


def _zos_disallowed(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) >= 127 or ch in _ZOS_FORBIDDEN for ch in text)


def zos_dataset_allowlist(key: str, raw: str) -> list[str]:
    """Exact dataset or PDS(MEMBER) names in the bridge's canonical (upper-case)
    form, first spelling kept once. The bridge's parse_dataset_allowlist rule."""
    message = (f"{key} must hold exact dataset or PDS(MEMBER) names, comma-separated (no wildcard, quote, "
               "relative generation or blank inside a name); names are not shown")
    names: list[str] = []
    for item in _zos_items(raw):
        if _zos_disallowed(item) or " " in item:
            raise ValueError(message)
        dsn, member = item, None
        if "(" in item or ")" in item:
            if item.count("(") != 1 or item.count(")") != 1 or not item.endswith(")"):
                raise ValueError(message)
            dsn, _, rest = item.partition("(")
            member = rest[:-1].upper()
            if _ZOS_MEMBER.fullmatch(member) is None:
                raise ValueError(message)
        dsn = dsn.upper()
        qualifiers = dsn.split(".")
        if (not dsn or len(dsn) > 44 or len(qualifiers) > 22
                or any(_ZOS_QUALIFIER.fullmatch(part) is None for part in qualifiers)):
            raise ValueError(message)
        name = f"{dsn}({member})" if member else dsn
        if name not in names:
            names.append(name)
    return names


def zos_jes_prefixes(key: str, raw: str) -> list[str]:
    """Exact JES jobname or owner prefixes (the bridge's parse_jes_prefixes rule)."""
    prefixes: list[str] = []
    for item in _zos_items(raw):
        upper = item.upper()
        if _zos_disallowed(item) or " " in item or _ZOS_MEMBER.fullmatch(upper) is None:
            raise ValueError(f"{key} must hold exact prefixes of 1 to 8 characters, comma-separated "
                             "(no wildcard); values are not shown")
        if upper not in prefixes:
            prefixes.append(upper)
    return prefixes


def _ipv4(text: str) -> bool:
    return _IPV4.fullmatch(text) is not None and all(int(part) <= 255 for part in text.split("."))


def zos_read_config(values: Mapping[str, str]) -> dict[str, object] | None:
    """The chart's `zosRead` values, or None while z/OS reads are off.

    Shapes are checked whenever a key is set (a typo must not wait for the day
    reads are enabled); required keys, the refusals and the CA file only when
    enabled. Off writes nothing, so an existing deployment renders what it did."""
    enabled = parse_strict_bool("ZOS_READ_ENABLED", values.get("ZOS_READ_ENABLED", ""), False)
    dry_run = parse_strict_bool("ZOS_READ_DRY_RUN", values.get("ZOS_READ_DRY_RUN", ""), False)
    plain_accepted = parse_strict_bool("ZOS_FTP_PLAIN_ACCEPTED", values.get("ZOS_FTP_PLAIN_ACCEPTED", ""), False)
    secret = values.get("ZOS_READ_SECRET", "")
    if secret and (len(secret) > 253 or _DNS_SUBDOMAIN.fullmatch(secret) is None):
        raise ValueError("ZOS_READ_SECRET must be a DNS-subdomain Secret name")
    audit_secret = values.get("ZOS_READ_AUDIT_SECRET", "")
    if audit_secret and (len(audit_secret) > 253 or _DNS_SUBDOMAIN.fullmatch(audit_secret) is None):
        raise ValueError("ZOS_READ_AUDIT_SECRET must be a DNS-subdomain Secret name")
    audit_key = values.get("ZOS_READ_AUDIT_SECRET_KEY", "") or ZOS_DEFAULT_AUDIT_KEY
    if len(audit_key) > 253 or re.fullmatch(r"[-._a-zA-Z0-9]+", audit_key) is None:
        raise ValueError("ZOS_READ_AUDIT_SECRET_KEY must be a Secret data key (letters, digits, '-', '_', '.')")
    raw_roles = values.get("ZOS_READ_APPROVED_ROLES", "")
    if raw_roles and _NAME_LIST.fullmatch(raw_roles) is None:
        raise ValueError("ZOS_READ_APPROVED_ROLES must be a comma-separated list of role names")
    approved = list(dict.fromkeys(raw_roles.split(","))) if raw_roles else []
    allowlist = zos_dataset_allowlist("ZOS_READ_DATASET_ALLOWLIST", values.get("ZOS_READ_DATASET_ALLOWLIST", ""))
    tools_message = ("ZOS_READ_ENABLED_TOOLS must be a subset of dataset_read, job_status, jes_spool_read "
                     "(ADR-0012 D7); uss_read and any other tool stay unapproved")
    raw_tools = values.get("ZOS_READ_ENABLED_TOOLS", "")
    tools = list(dict.fromkeys(_zos_items(raw_tools))) if raw_tools else list(ZOS_DEFAULT_TOOLS)
    if not tools or any(tool not in ZOS_APPROVED_TOOLS for tool in tools):
        raise ValueError(tools_message)
    job_prefixes = zos_jes_prefixes("ZOS_READ_JES_JOBNAME_PREFIXES", values.get("ZOS_READ_JES_JOBNAME_PREFIXES", ""))
    owner_prefixes = zos_jes_prefixes("ZOS_READ_JES_OWNER_PREFIXES", values.get("ZOS_READ_JES_OWNER_PREFIXES", ""))
    host = values.get("ZOS_FTP_HOST", "")
    if host and (len(host) > 253 or _ZOS_HOST.fullmatch(host) is None):
        raise ValueError("ZOS_FTP_HOST must be a host name or an IPv4 address (no scheme, port or path)")
    port = _tcp_port("ZOS_FTP_PORT", values.get("ZOS_FTP_PORT", ""), 21)
    host_ip = values.get("ZOS_FTP_HOST_IP", "")
    if host_ip and not _ipv4(host_ip):
        raise ValueError("ZOS_FTP_HOST_IP must be one IPv4 address (no prefix length)")
    if not host_ip and _ipv4(host):
        host_ip = host
    passive: dict[str, int] = {}
    raw_passive = values.get("ZOS_FTP_PASSIVE_PORTS", "")
    if raw_passive:
        match = re.fullmatch(r"([0-9]{1,5})-([0-9]{1,5})", raw_passive)
        if match is None or not 1 <= int(match.group(1)) <= int(match.group(2)) <= 65535:
            raise ValueError("ZOS_FTP_PASSIVE_PORTS must be FIRST-LAST, two TCP ports with FIRST not above LAST "
                             "(the server's FTP.DATA PASSIVEPORTRANGE)")
        passive = {"first": int(match.group(1)), "last": int(match.group(2))}
    mode_message = ("ZOS_FTP_TRANSPORT_MODE must be ftps or plain: the operator pins the transport, with no "
                    "fallback (ADR-0012 D5)")
    mode = values.get("ZOS_FTP_TRANSPORT_MODE", "")
    if mode and mode not in ZOS_TRANSPORT_MODES:
        raise ValueError(mode_message)
    encoding = values.get("ZOS_FTP_ENCODING", "") or ZOS_DEFAULT_ENCODING
    encoding_message = "ZOS_FTP_ENCODING must be a Python text codec name such as latin-1 or utf-8"
    if _ZOS_ENCODING.fullmatch(encoding) is None:
        raise ValueError(encoding_message)
    try:
        "".encode(encoding)
    except LookupError:
        raise ValueError(encoding_message) from None
    resources = _chart_resources(values, "ZOS_BRIDGE")
    if not enabled:
        return None
    # ADR-0012 D3 guard (c): no reader outside signed-in team members.
    if parse_strict_bool("AGENT_ROUTE", values.get("AGENT_ROUTE", ""), False):
        raise ValueError(f"ZOS_READ_ENABLED=true refused with AGENT_ROUTE=true: {_ZOS_GUARD_C}")
    policy_enabled, policy_namespaces = network_policy(values)
    if not policy_enabled:
        raise ValueError(f"ZOS_READ_ENABLED=true refused with AGENT_NETWORK_POLICY=false: {_ZOS_GUARD_C}")
    if policy_namespaces:
        raise ValueError("ZOS_READ_ENABLED=true refused with AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES set: "
                         f"{_ZOS_GUARD_C}")
    # ADR-0012 D3 guard (b): every sign-in and admin role is approved (an admin
    # role alone signs a user in). The lists are the ones Open WebUI renders.
    if parse_strict_bool("OPENWEBUI_ENABLED", values.get("OPENWEBUI_ENABLED", ""), True):
        # ADR-0012 D3(d), #1397: the docs Pipe tags every agent call with a keyed
        # audit tag and fails closed (no Docs answer at all) without its key.
        if not audit_secret:
            raise ValueError("ZOS_READ_ENABLED=true refused without ZOS_READ_AUDIT_SECRET: the docs Pipe tags every "
                             "agent call with a keyed audit tag (ADR-0012 D3(d)) and answers nothing without its "
                             "key. Create the Secret and set it, or keep ZOS_READ_ENABLED=false")
        if audit_secret == secret:
            raise ValueError("ZOS_READ_AUDIT_SECRET must not be ZOS_READ_SECRET: the Open WebUI pod must not "
                             "reference the Secret that holds the z/OS ID")
        signed_in = [role.strip() for key in ("OPENWEBUI_OIDC_ALLOWED_ROLES", "OPENWEBUI_OIDC_ADMIN_ROLES")
                     for role in values.get(key, "").split(",") if role.strip()]
        if any(role not in approved for role in signed_in):
            raise ValueError("ZOS_READ_ENABLED=true refused: every role in OPENWEBUI_OIDC_ALLOWED_ROLES and "
                             "OPENWEBUI_OIDC_ADMIN_ROLES must be listed in ZOS_READ_APPROVED_ROLES (ADR-0012 D3 "
                             "guard (b)); role names are not shown. Approve the role after the premise review, "
                             "or keep ZOS_READ_ENABLED=false")
    if not mode:
        raise ValueError(mode_message)
    ca_file = values.get("ZOS_FTP_CA_FILE", "")
    required: dict[str, object] = {
        "ZOS_READ_SECRET": secret, "ZOS_READ_DATASET_ALLOWLIST": allowlist, "ZOS_FTP_HOST": host,
        "ZOS_FTP_HOST_IP": host_ip, "ZOS_FTP_PASSIVE_PORTS": passive,
    }
    if mode == "ftps":
        required["ZOS_FTP_CA_FILE"] = ca_file
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError(f"ZOS_READ_ENABLED=true but required inputs are missing: {', '.join(missing)}. "
                         "Set them, or keep ZOS_READ_ENABLED=false")
    if _ipv4(host) and host != host_ip:
        raise ValueError("ZOS_FTP_HOST is an IPv4 address that differs from ZOS_FTP_HOST_IP: the bridge "
                         "reaches one z/OS host")
    if mode == "plain" and not plain_accepted:
        raise ValueError("ZOS_FTP_TRANSPORT_MODE=plain requires ZOS_FTP_PLAIN_ACCEPTED=true: the site security "
                         "owner's written acceptance of plain FTP (ADR-0012 D5)")
    ca_bundle = ""
    if mode == "ftps":
        ca_bundle = _site_text("ZOS_FTP_CA_FILE", ca_file)
        if not pem_certificates(ca_bundle):
            raise ValueError("ZOS_FTP_CA_FILE must hold only PEM certificates that load "
                             "(-----BEGIN CERTIFICATE----- blocks, no key or other text)")
    if set(tools) & set(ZOS_JES_TOOLS) and not job_prefixes and not owner_prefixes:
        raise ValueError("ZOS_READ_ENABLED_TOOLS names a JES tool but ZOS_READ_JES_JOBNAME_PREFIXES and "
                         "ZOS_READ_JES_OWNER_PREFIXES are both empty: the bridge would deny every job")
    return {
        "enabled": True,
        "dryRun": dry_run,
        "secretName": secret,
        "auditKey": {"secretName": audit_secret, "key": audit_key},
        "approvedRoles": approved,
        "datasetAllowlist": allowlist,
        "enabledTools": tools,
        "jesJobnamePrefixes": job_prefixes,
        "jesOwnerPrefixes": owner_prefixes,
        "ftp": {
            "host": host,
            "port": port,
            "hostIp": host_ip,
            "passivePorts": passive,
            "transportMode": mode,
            "plainAccepted": mode == "plain",
            "caBundle": ca_bundle,
            "encoding": encoding,
        },
        "resources": resources,
    }


def validate_operator_booleans(values: Mapping[str, str]) -> None:
    require_no_retired_keys(values)
    for name in OPERATOR_BOOL_KEYS:
        parse_strict_bool(name, values.get(name, ""), False)
    require_in_place_retired(values.get("INGEST_ALIAS_PUBLISH", ""))
    require_one_follow_up_mode(values)


def require_one_follow_up_mode(values: Mapping[str, str]) -> None:
    """Condensation and anchoring are exclusive arms (#1268); the pod would
    refuse the pair at startup, so preflight and the mapper refuse it first."""
    if (parse_strict_bool("CHAT_CONDENSE_ENABLED", values.get("CHAT_CONDENSE_ENABLED", ""), False)
            and parse_strict_bool(FOLLOWUP_ANCHOR_KEY, values.get(FOLLOWUP_ANCHOR_KEY, ""), False)):
        raise ValueError(CHAT_FOLLOWUP_MODES_CONFLICT)


def yaml_json_string(text: str) -> str:
    """`text` as a JSON double-quoted string that YAML reads back byte-exact:
    ASCII only, each surrogate-pair escape in YAML's 8-digit form (the chart's
    YAML reader refuses JSON's pairs, as JsonObject.flow notes) and DEL
    escaped (YAML does not accept it raw). Identical to json.dumps otherwise."""

    def pair(match: re.Match[str]) -> str:
        if match.group(1) is None:
            return match.group(0)  # an escaped backslash, kept as is
        high, low = int(match.group(1), 16), int(match.group(2), 16)
        return f"\\U{0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00):08X}"

    quoted = re.sub(r"\\\\|\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})", pair, json.dumps(text, ensure_ascii=True))
    return quoted.replace("\x7f", "\\u007f")


class JsonObject(dict):
    """An operator-supplied JSON object carried into the release values.

    Its keys and strings are arbitrary text, so the values serializer writes
    it as one JSON flow mapping (YAML reads JSON) instead of block keys."""

    def flow(self) -> str:
        """ASCII JSON, with each surrogate-pair escape as YAML's 8-digit
        form: the chart's YAML reader refuses JSON's `\\ud83d\\ude00` pairs."""

        def pair(match: re.Match[str]) -> str:
            if match.group(1) is None:
                return match.group(0)  # an escaped backslash, kept as is
            high, low = int(match.group(1), 16), int(match.group(2), 16)
            return f"\\U{0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00):08X}"

        return re.sub(r"\\\\|\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})", pair,
                      json.dumps(self, ensure_ascii=True))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result or not key:
            raise ValueError("duplicate or empty key")
        result[key] = value
    return result


def _reject_json_constant(_name: str) -> object:
    raise ValueError("non-finite number")


def _finite_json(value: object) -> bool:
    """Finite, and integers exact through the chart: Helm reads every number
    as a 64-bit float, which would silently change a larger integer."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return abs(value) <= 2**53
    if isinstance(value, dict):
        return all(_finite_json(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    return True


def model_argument_overrides(values: Mapping[str, str]) -> dict[str, object]:
    """Model request arguments (#1193) as chart values; empty is omitted.

    The same rules as mainframe_rag.model_args, so a refused selection stops
    at preflight instead of at pod startup. Diagnostics name the field or the
    fixed key, never the supplied text.
    """
    result: dict[str, object] = {}
    transport = values.get(EFFORT_TRANSPORT_KEY, "")
    if transport != "":
        if transport not in EFFORT_TRANSPORTS:
            raise ValueError(f"{EFFORT_TRANSPORT_KEY} must be top_level, chat_template or none")
        result["effortTransport"] = transport
    raw = values.get(EXTRA_BODY_KEY, "").strip()
    if raw != "":
        shape = (f"{EXTRA_BODY_KEY} must be one JSON object with unique, nonempty keys and finite numbers"
                 " (integers within 2^53)")
        if len(raw) > EXTRA_BODY_MAX_CHARS:
            raise ValueError(f"{EXTRA_BODY_KEY} must be at most {EXTRA_BODY_MAX_CHARS} characters")
        try:
            body = json.loads(raw, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            raise ValueError(shape) from None
        if not isinstance(body, dict) or not _finite_json(body):
            raise ValueError(shape)
        try:
            # An unpaired surrogate escape cannot be sent as a request body.
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(shape) from None
        for key in EXTRA_BODY_PROTECTED_KEYS:
            if key in body:
                raise ValueError(f"{EXTRA_BODY_KEY} must not set the protected field {key}")
        for key, owner in EXTRA_BODY_OWNED_KEYS.items():
            if key in body:
                raise ValueError(f"{EXTRA_BODY_KEY} must not set {key}; use {owner}")
        kwargs = body.get("chat_template_kwargs", {})
        if not isinstance(kwargs, dict):
            raise ValueError(f"{EXTRA_BODY_KEY} chat_template_kwargs must be a JSON object")
        if "reasoning_effort" in kwargs and transport != "none":
            raise ValueError(
                f"{EXTRA_BODY_KEY} chat_template_kwargs.reasoning_effort requires {EFFORT_TRANSPORT_KEY}=none")
        if body:
            result["extraBody"] = JsonObject(body)
    return result


def reasoning_overrides(values: Mapping[str, str]) -> dict[str, object]:
    """Bound explicit app settings without inventing deployment defaults.

    Limits match config.Settings. Empty/unset values stay omitted; zero and
    false are explicit selections. Field-only errors never echo supplied text.
    """
    result: dict[str, object] = model_argument_overrides(values)
    for name, (field, minimum, maximum) in REASONING_INTEGER_FIELDS.items():
        raw = values.get(name, "")
        if raw == "":
            continue
        message = f"{name} must be an integer from {minimum} to {maximum}"
        if not re.fullmatch(r"[0-9]+", raw):
            raise ValueError(message)
        try:
            number = int(raw)
        except ValueError:
            raise ValueError(message) from None
        if not minimum <= number <= maximum:
            raise ValueError(message)
        result[field] = number
    for name, field in REASONING_EFFORT_FIELDS.items():
        raw = values.get(name, "")
        if raw == "":
            continue
        if raw not in REASONING_EFFORTS:
            raise ValueError(f"{name} must be low/medium/high/xhigh")
        result[field] = raw
    # The served template's effort domain; both efforts, explicit or default,
    # must be members so the console and complex path never send another (#748).
    raw = values.get("LLM_REASONING_EFFORTS", "")
    parts = [part.strip() for part in (raw or REASONING_EFFORT_DEFAULTS["LLM_REASONING_EFFORTS"]).split(",")]
    if any(part not in REASONING_EFFORTS for part in parts) or len(set(parts)) != len(parts):
        raise ValueError("LLM_REASONING_EFFORTS must list distinct low/medium/high/xhigh values")
    if raw != "":
        # Canonical ascending form: whatever preflight accepts, the chart schema accepts.
        result["reasoningEfforts"] = ",".join(effort for effort in REASONING_EFFORTS if effort in parts)
    for name in REASONING_EFFORT_FIELDS:
        if (values.get(name, "") or REASONING_EFFORT_DEFAULTS[name]) not in parts:
            raise ValueError(f"{name} must be one of LLM_REASONING_EFFORTS")
    if values.get("LLM_STREAM", "") != "":
        result["stream"] = parse_strict_bool("LLM_STREAM", values["LLM_STREAM"], False)
    if values.get(FOLLOWUP_ANCHOR_KEY, "") != "":
        result["followupAnchorEnabled"] = parse_strict_bool(FOLLOWUP_ANCHOR_KEY, values[FOLLOWUP_ANCHOR_KEY], False)
    if values.get(CODING_MODE_KEY, "") != "":
        result["codingModeEnabled"] = parse_strict_bool(CODING_MODE_KEY, values[CODING_MODE_KEY], True)
    if values.get(SPL_MODE_KEY, "") != "":
        result["splModeEnabled"] = parse_strict_bool(SPL_MODE_KEY, values[SPL_MODE_KEY], False)
    if values.get(CHAT_IMAGES_KEY, "") != "":
        result["chatImagesEnabled"] = parse_strict_bool(CHAT_IMAGES_KEY, values[CHAT_IMAGES_KEY], False)
    if values.get(SELECT_KEY, "") != "":
        result["answerSelectEnabled"] = parse_strict_bool(SELECT_KEY, values[SELECT_KEY], True)
    if values.get(EXPAND_KEY, "") != "":
        result["answerExpandEnabled"] = parse_strict_bool(EXPAND_KEY, values[EXPAND_KEY], True)
    source = values.get(TOKEN_COUNT_SOURCE_KEY, "")
    if source != "":
        if source not in TOKEN_COUNT_SOURCES:
            raise ValueError(f"{TOKEN_COUNT_SOURCE_KEY} must be tokenize, completion_usage, auto or estimate")
        result["tokenCountSource"] = source
    return result


def request_overrides(values: Mapping[str, str]) -> dict[str, int | float]:
    """Explicit request-bound selections as chart values; empty is omitted.

    A nonzero deadline must stay strictly below the route timeout so the
    agent's 504 fires before the route cut (#843 D2). Diagnostics name the
    field, never the value.
    """
    result: dict[str, int | float] = {}
    for name, (field, minimum, maximum) in REQUEST_INTEGER_FIELDS.items():
        raw = values.get(name, "")
        if raw == "":
            continue
        if not re.fullmatch(r"[0-9]{1,10}", raw) or not minimum <= int(raw) <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        result[field] = int(raw)
    deadline = result.get("requestDeadlineSeconds", 0)
    if deadline >= ROUTE_TIMEOUT_SECONDS:
        raise ValueError(f"REQUEST_DEADLINE_S must be 0 or below the {ROUTE_TIMEOUT_SECONDS} s route timeout")
    body = {**REQUEST_BODY_DEFAULTS, **{k: v for k, v in result.items() if k in REQUEST_BODY_DEFAULTS}}
    if body["requestBodyBudgetBytes"] < body["requestMaxBodyBytes"]:
        raise ValueError("REQUEST_BODY_BUDGET_BYTES must be at least REQUEST_MAX_BODY_BYTES")
    raw = values.get(REQUEST_WAIT_KEY, "")
    if raw != "":
        if not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3})?", raw) or float(raw) > 300:
            raise ValueError(f"{REQUEST_WAIT_KEY} must be a number from 0 to 300")
        result["requestQueueWaitSeconds"] = float(raw) if "." in raw else int(raw)
    return result


# Site sizing (#1070 R2, agent #1348): a quota-bound site fits the release
# into its namespace quota with these keys, not with free-form overlays (those
# stay CI-only). Unset keeps the chart default, so the default render is
# unchanged. SIZING_DEFAULTS mirrors charts/qdrant-openshift.values.yaml and
# charts/mainframe-rag/values.yaml (a test pins both) so a lone limit or
# request is compared against the value that will actually render.
SIZING_DEFAULTS = {
    "QDRANT": {"requests": {"cpu": "4", "memory": "16Gi"}, "limits": {"cpu": "8", "memory": "32Gi"}},
    "INGEST": {"requests": {"cpu": "4", "memory": "8Gi"}, "limits": {"cpu": "16", "memory": "32Gi"}},
    "AGENT": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
    # The Splunk gateway container (#1077, teamTools.splunk.resources): same
    # rules, applied by team_tools_config only while the gateway is enabled.
    "SPLUNK_GATEWAY": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
    # The z/OS FTP bridge container (#1294, zosRead.resources): applied by
    # zos_read_config only while z/OS reads are enabled.
    "ZOS_BRIDGE": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
}
SIZING_PREFIXES = ("QDRANT", "INGEST", "AGENT")
QDRANT_VOLUME_KEYS = {"QDRANT_DATA_SIZE": "persistence.size", "QDRANT_SNAPSHOT_SIZE": "snapshotPersistence.size"}
SIZING_ENV_KEYS = (
    *QDRANT_VOLUME_KEYS,
    *(f"{prefix}_{res}_{kind}" for prefix in SIZING_PREFIXES for kind in ("REQUEST", "LIMIT") for res in ("CPU", "MEMORY")),
)
_CPU_QUANTITY = re.compile(r"(?:[0-9]+(?:\.[0-9]{1,3})?|[0-9]+m)")
_BYTE_QUANTITY = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:Ki|Mi|Gi|Ti)")
_BINARY_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def _quantity(name: str, raw: str, cpu: bool) -> Fraction:
    """A positive CPU or byte quantity as an exact number; the message never echoes the value."""
    if cpu:
        if _CPU_QUANTITY.fullmatch(raw):
            number = Fraction(raw[:-1]) / 1000 if raw.endswith("m") else Fraction(raw)
            if number > 0:
                return number
        raise ValueError(f"{name} must be a positive CPU quantity (whole or decimal cores, or millicores with an m suffix)")
    if _BYTE_QUANTITY.fullmatch(raw):
        number = Fraction(raw[:-2]) * _BINARY_UNITS[raw[-2:]]
        if number > 0:
            return number
    raise ValueError(f"{name} must be a positive quantity with a unit (Ki, Mi, Gi or Ti)")


def sizing_resources(values: Mapping[str, str], prefix: str) -> dict[str, dict[str, str]]:
    """The explicitly selected requests/limits of one workload (`QDRANT`,
    `INGEST` or `AGENT`), validated against each other and the chart default of any side
    left unset. Unset keys are omitted."""
    chosen: dict[str, dict[str, str]] = {"requests": {}, "limits": {}}
    quantities: dict[tuple[str, str], Fraction] = {}
    for section, kind in (("requests", "REQUEST"), ("limits", "LIMIT")):
        for field, res in (("cpu", "CPU"), ("memory", "MEMORY")):
            name = f"{prefix}_{res}_{kind}"
            raw = values.get(name, "")
            if raw != "":
                chosen[section][field] = raw
            quantities[(section, field)] = _quantity(
                name, raw or SIZING_DEFAULTS[prefix][section][field], field == "cpu")
    for field, res in (("cpu", "CPU"), ("memory", "MEMORY")):
        if quantities[("limits", field)] < quantities[("requests", field)]:
            raise ValueError(f"{prefix}_{res}_LIMIT must not be below {prefix}_{res}_REQUEST")
    return {section: fields for section, fields in chosen.items() if fields}


def qdrant_sizing(values: Mapping[str, str]) -> list[str]:
    """`helm key=value` assignments for the Qdrant release, in a fixed order."""
    result = []
    for name, helm_key in QDRANT_VOLUME_KEYS.items():
        raw = values.get(name, "")
        if raw != "":
            _quantity(name, raw, False)
            result.append(f"{helm_key}={raw}")
    for section, fields in sizing_resources(values, "QDRANT").items():
        result.extend(f"resources.{section}.{field}={raw}" for field, raw in fields.items())
    return result


def _chart_resources(values: Mapping[str, str], prefix: str) -> dict[str, dict[str, str]]:
    merged = {section: dict(fields) for section, fields in SIZING_DEFAULTS[prefix].items()}
    for section, fields in sizing_resources(values, prefix).items():
        merged[section].update(fields)
    return merged


def ingest_resources(values: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """The chart's `ingest.resources` with the selected sizes applied over the defaults."""
    return _chart_resources(values, "INGEST")


def agent_resources(values: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """The chart's `resources.agent` (agent container only, never the oauth-proxy
    sidecar) with the selected sizes applied over the defaults (#1348)."""
    return _chart_resources(values, "AGENT")


def validate_model_config(values: Mapping[str, str]) -> None:
    reasoning_overrides(values)
    request_overrides(values)
    for name in MODEL_CONFIG_KEYS:
        if name.endswith("BASE_URL"):
            value = values.get(name, "")
            if value and not value.startswith(("http://", "https://")):
                raise ValueError(f"{name} must begin with http:// or https://")
    for name in ("EMBED_MODEL", "EMBED_BASE_URL"):
        if not values.get(name):
            raise ValueError(f"{name} is required for deploy/ingest (see airgap.env.example)")
    if values.get("LLM_MODEL_REASONING") and not values.get("LLM_BASE_URL"):
        raise ValueError("LLM_BASE_URL is required when LLM_MODEL_REASONING is set")
    if parse_strict_bool("CONTEXTUAL_EMBED_ENABLED", values.get("CONTEXTUAL_EMBED_ENABLED", ""), False):
        for name in ("CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL"):
            if not values.get(name):
                raise ValueError(f"{name} is required when CONTEXTUAL_EMBED_ENABLED is set")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["--retired-keys"]:
            require_no_retired_keys(os.environ)
            return 0
        if args == ["--qdrant-sizing"]:
            require_no_retired_keys(os.environ)
            print("\n".join(qdrant_sizing(os.environ)))
            return 0
            return 0
        validate_operator_booleans(os.environ)
        network_policy(os.environ)
        feedback_config(os.environ)
        openwebui_config(os.environ)
        team_tools_config(os.environ)
        zos_read_config(os.environ)
        validate_model_config(os.environ)
        qdrant_sizing(os.environ)
        ingest_resources(os.environ)
        agent_resources(os.environ)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
