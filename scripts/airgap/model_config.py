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
    if values.get("CONTEXTUAL_EMBED_ENABLED", "").lower() in ("true", "1", "yes"):
        for name in ("CONTEXT_LLM_BASE_URL", "CONTEXT_LLM_MODEL"):
            if not values.get(name):
                raise ValueError(f"{name} is required when CONTEXTUAL_EMBED_ENABLED is set")


def main() -> int:
    try:
        validate_model_config(os.environ)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
