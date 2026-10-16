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
REASONING_EFFORT_DEFAULTS = {"LLM_REASONING_EFFORT_SIMPLE": "low", "LLM_REASONING_EFFORT_COMPLEX": "high",
                             "LLM_REASONING_EFFORTS": "low,medium,high"}
REASONING_ENV_KEYS = (*REASONING_INTEGER_FIELDS, *REASONING_EFFORT_FIELDS, "LLM_REASONING_EFFORTS", "LLM_STREAM")

MODEL_CONFIG_KEYS = (
    "GATEWAY_BASE_URL", "EMBED_BASE_URL", "EMBED_MODEL",
    "LLM_BASE_URL", "LLM_MODEL_REASONING", "RERANK_BASE_URL",
    "CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL", "CONTEXTUAL_EMBED_ENABLED",
    *REASONING_ENV_KEYS,
)

# Operator booleans that fail closed on any non-boolean spelling. One parser
# for preflight and the values mapper (map_values.strict_bool wraps it).
OPERATOR_BOOL_KEYS = (
    "UI_ENABLED", "CONTEXTUAL_EMBED_ENABLED", "INGEST_ALIAS_PUBLISH",
    "INGEST_REINGEST", "CHAT_CONDENSE_ENABLED", "LLM_STREAM",
)


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


def validate_model_config(values: Mapping[str, str]) -> None:
    reasoning_overrides(values)
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
        validate_model_config(os.environ)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
