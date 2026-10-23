"""Pure validation of resolved nonsecret model configuration for air-gap launchers."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping

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
# chart default (deadline 270; admission unset, i.e. off until the D4 stepped
# run); zero is an explicit selection. Limits match config.Settings and the
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
    "INGEST_REINGEST", "CHAT_CONDENSE_ENABLED", "LLM_STREAM", "AGENT_NETWORK_POLICY",
    "FEEDBACK_ENABLED",
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


def validate_operator_booleans(values: Mapping[str, str]) -> None:
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


def main() -> int:
    try:
        validate_operator_booleans(os.environ)
        network_policy(os.environ)
        feedback_config(os.environ)
        validate_model_config(os.environ)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
