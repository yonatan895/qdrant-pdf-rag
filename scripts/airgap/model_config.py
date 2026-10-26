"""Pure validation of resolved nonsecret model configuration for air-gap launchers."""

from __future__ import annotations

import os
import re
import ssl
import sys
from collections.abc import Mapping
from fractions import Fraction
from urllib.parse import urlsplit

REASONING_INTEGER_FIELDS = {
    "LLM_MAX_MODEL_LEN": ("maxModelLen", 512, 131072),
    "LLM_RESERVED_OUTPUT_TOKENS": ("reservedOutputTokens", 128, 16384),
    "LLM_THINKING_RESERVE_TOKENS_SIMPLE": ("thinkingReserveTokensSimple", 0, 16384),
    "LLM_THINKING_RESERVE_TOKENS_COMPLEX": ("thinkingReserveTokensComplex", 0, 16384),
}
REASONING_EFFORT_FIELDS = {
    "LLM_REASONING_EFFORT_SIMPLE": "reasoningEffortSimple",
    "LLM_REASONING_EFFORT_COMPLEX": "reasoningEffortComplex",
}
REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
# Mirrors config.Settings defaults; tests pin both sides (#748).
REASONING_EFFORT_DEFAULTS = {"LLM_REASONING_EFFORT_SIMPLE": "medium", "LLM_REASONING_EFFORT_COMPLEX": "medium",
                             "LLM_REASONING_EFFORTS": "low,medium,xhigh"}
REASONING_ENV_KEYS = (*REASONING_INTEGER_FIELDS, *REASONING_EFFORT_FIELDS, "LLM_REASONING_EFFORTS", "LLM_STREAM")

# Request admission and total deadline (#374 handoff, #843 D1). Empty keeps the
# chart default (deadline 270; admission 4/4/30, #843); zero is an explicit selection. Limits match config.Settings and the
# chart schema. The route timeout is the mapper's fixed 300 s.
ROUTE_TIMEOUT_SECONDS = 300
REQUEST_INTEGER_FIELDS = {
    "REQUEST_DEADLINE_S": ("requestDeadlineSeconds", 0, 3600),
    "REQUEST_MAX_CONCURRENT": ("requestMaxConcurrent", 0, 1000),
    "REQUEST_QUEUE_MAX": ("requestQueueMax", 0, 1000),
}
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
    "FEEDBACK_ENABLED", "OPENWEBUI_ENABLED",
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
FEEDBACK_KEYS = ("FEEDBACK_ENABLED", "FEEDBACK_SECRET", "FEEDBACK_STORAGE_SIZE",
                 "FEEDBACK_RETENTION_DAYS")
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
    }


def validate_operator_booleans(values: Mapping[str, str]) -> None:
    require_no_retired_keys(values)
    for name in OPERATOR_BOOL_KEYS:
        parse_strict_bool(name, values.get(name, ""), False)
    require_in_place_retired(values.get("INGEST_ALIAS_PUBLISH", ""))


def reasoning_overrides(values: Mapping[str, str]) -> dict[str, int | str | bool]:
    """Bound explicit app settings without inventing deployment defaults.

    Limits match config.Settings. Empty/unset values stay omitted; zero and
    false are explicit selections. Field-only errors never echo supplied text.
    """
    result: dict[str, int | str | bool] = {}
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
        if not re.fullmatch(r"[0-9]{1,5}", raw) or not minimum <= int(raw) <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        result[field] = int(raw)
    deadline = result.get("requestDeadlineSeconds", 0)
    if deadline >= ROUTE_TIMEOUT_SECONDS:
        raise ValueError(f"REQUEST_DEADLINE_S must be 0 or below the {ROUTE_TIMEOUT_SECONDS} s route timeout")
    raw = values.get(REQUEST_WAIT_KEY, "")
    if raw != "":
        if not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3})?", raw) or float(raw) > 300:
            raise ValueError(f"{REQUEST_WAIT_KEY} must be a number from 0 to 300")
        result["requestQueueWaitSeconds"] = float(raw) if "." in raw else int(raw)
    return result


# Site sizing (#1070 R2): a quota-bound site fits the release into its
# namespace quota with these keys, not with free-form overlays (those stay
# CI-only). Unset keeps the chart default, so the default render is unchanged.
# SIZING_DEFAULTS mirrors charts/qdrant-openshift.values.yaml and
# charts/mainframe-rag/values.yaml (a test pins both) so a lone limit or
# request is compared against the value that will actually render.
SIZING_DEFAULTS = {
    "QDRANT": {"requests": {"cpu": "4", "memory": "16Gi"}, "limits": {"cpu": "8", "memory": "32Gi"}},
    "INGEST": {"requests": {"cpu": "4", "memory": "8Gi"}, "limits": {"cpu": "16", "memory": "32Gi"}},
}
SIZING_PREFIXES = ("QDRANT", "INGEST")
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
    """The explicitly selected requests/limits of one workload (`QDRANT` or
    `INGEST`), validated against each other and the chart default of any side
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


def ingest_resources(values: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """The chart's `ingest.resources` with the selected sizes applied over the defaults."""
    merged = {section: dict(fields) for section, fields in SIZING_DEFAULTS["INGEST"].items()}
    for section, fields in sizing_resources(values, "INGEST").items():
        merged[section].update(fields)
    return merged


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
        validate_model_config(os.environ)
        qdrant_sizing(os.environ)
        ingest_resources(os.environ)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
