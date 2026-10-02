"""Pure validation of resolved nonsecret model configuration for air-gap launchers."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

MODEL_CONFIG_KEYS = (
    "GATEWAY_BASE_URL", "VLLM_BASE_URL", "EMBED_BASE_URL", "EMBED_MODEL",
    "LLM_BASE_URL", "LLM_MODEL_REASONING", "RERANK_BASE_URL",
    "CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL", "CONTEXTUAL_EMBED_ENABLED",
)

# Operator booleans that fail closed on any non-boolean spelling. One parser
# for preflight and the values mapper (map_values.strict_bool wraps it).
OPERATOR_BOOL_KEYS = (
    "UI_ENABLED", "CONTEXTUAL_EMBED_ENABLED", "INGEST_ALIAS_PUBLISH",
    "INGEST_REINGEST", "CHAT_CONDENSE_ENABLED",
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


def validate_operator_booleans(values: Mapping[str, str]) -> None:
    for name in OPERATOR_BOOL_KEYS:
        parse_strict_bool(name, values.get(name, ""), False)


def validate_model_config(values: Mapping[str, str]) -> None:
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
