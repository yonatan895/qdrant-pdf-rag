"""Model request arguments (issue #1193).

The answer model sits behind a platform gateway, and the served model and its
chat template can change. Nothing here names a model: an operator supplies
extra chat-request body fields as JSON and chooses how the selected reasoning
effort travels. One `ModelRequestArgs` value then shapes both the generation
request (`answer._chat_body`) and the `/tokenize` count
(`tokenizer.VllmTokenizer`), so the count renders the template the generation
renders. A later named profile can carry its own instance.

Validation is startup-only (`config.Settings`) and fails closed: a setting
that would override a field the application owns never reaches a request.
Messages name fields, never supplied values.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

EffortTransport = Literal["top_level", "chat_template", "none"]
EFFORT_TRANSPORTS: tuple[EffortTransport, ...] = get_args(EffortTransport)

TEMPLATE_KWARGS_KEY = "chat_template_kwargs"
EFFORT_KEY = "reasoning_effort"
# Bounded like every other setting: the body rides on every chat request.
EXTRA_BODY_MAX_CHARS = 4096

# The application builds these; no configuration may replace them.
PROTECTED_BODY_KEYS = frozenset({"model", "messages", "stream", "stream_options"})
# Fields with their own setting. An extra-body copy would be a second owner
# (and an uncapped or over-window `max_tokens`), so it is refused at startup
# instead of winning or losing silently per request.
OWNED_BODY_KEYS: dict[str, str] = {
    "max_tokens": "LLM_RESERVED_OUTPUT_TOKENS and the thinking reserves",
    "max_completion_tokens": "LLM_RESERVED_OUTPUT_TOKENS and the thinking reserves",
    "temperature": "LLM_TEMPERATURE",
    EFFORT_KEY: "LLM_EFFORT_TRANSPORT and the effort settings",
}


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result or not key:
            raise ValueError("duplicate or empty key")
        result[key] = value
    return result


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def parse_body_fields(raw: str, *, name: str, allow_owned: bool = False) -> dict[str, Any]:
    """Parse one JSON object of chat-request body fields.

    Empty text is no fields. Refused: non-objects, duplicate or empty keys,
    non-finite numbers, an oversized value, protected keys, and (unless
    ``allow_owned``, the probe's candidates) keys owned by another setting.
    Nested values pass through untouched; only `chat_template_kwargs` must
    itself be an object, because the effort and the token count merge into it.
    """
    text = raw.strip()
    if not text:
        return {}
    if len(text) > EXTRA_BODY_MAX_CHARS:
        raise ValueError(f"{name} must be at most {EXTRA_BODY_MAX_CHARS} characters")
    shape = f"{name} must be one JSON object with unique, nonempty keys and finite numbers"
    try:
        parsed = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        # An unpaired surrogate escape cannot be sent as a request body.
        json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError):
        raise ValueError(shape) from None
    if not isinstance(parsed, dict) or not _finite(parsed):
        raise ValueError(shape)
    for key in sorted(PROTECTED_BODY_KEYS & parsed.keys()):
        raise ValueError(f"{name} must not set the protected field {key}")
    if not allow_owned:
        for key in sorted(OWNED_BODY_KEYS.keys() & parsed.keys()):
            raise ValueError(f"{name} must not set {key}; use {OWNED_BODY_KEYS[key]}")
    if TEMPLATE_KWARGS_KEY in parsed and not isinstance(parsed[TEMPLATE_KWARGS_KEY], dict):
        raise ValueError(f"{name} {TEMPLATE_KWARGS_KEY} must be a JSON object")
    return parsed


@dataclass(frozen=True)
class ModelRequestArgs:
    """How one answer model is addressed: extra body fields plus the effort
    transport. The default instance sends exactly the pre-#1193 bytes."""

    extra_body: Mapping[str, Any] = field(default_factory=dict)
    effort_transport: EffortTransport = "top_level"

    def __post_init__(self) -> None:
        if self.effort_transport not in EFFORT_TRANSPORTS:
            raise ValueError("LLM_EFFORT_TRANSPORT must be top_level, chat_template or none")
        kwargs = self.extra_body.get(TEMPLATE_KWARGS_KEY, {})
        if self.effort_transport != "none" and EFFORT_KEY in kwargs:
            # With a transport selected the application owns the effort; a
            # fixed template effort is only meaningful when none is sent.
            raise ValueError(
                f"LLM_EXTRA_BODY {TEMPLATE_KWARGS_KEY}.{EFFORT_KEY} requires LLM_EFFORT_TRANSPORT=none"
            )

    def effort_fields(self, effort: str | None) -> dict[str, Any]:
        """Top-level effort field, or nothing for the other transports and
        for an omitted effort."""
        if effort and self.effort_transport == "top_level":
            return {EFFORT_KEY: effort}
        return {}

    def extra_fields(self, effort: str | None) -> dict[str, Any]:
        """The extra body as sent with a generation: a fresh copy, with the
        effort merged into `chat_template_kwargs` on that transport."""
        fields = json.loads(json.dumps(self.extra_body))
        if effort and self.effort_transport == "chat_template":
            fields[TEMPLATE_KWARGS_KEY] = {**fields.get(TEMPLATE_KWARGS_KEY, {}), EFFORT_KEY: effort}
        return fields

    def template_kwargs(self, effort: str | None) -> dict[str, Any]:
        """Template arguments the generation renders with, for `/tokenize`.

        `top_level`: the server maps the top-level effort into the template
        and turns thinking on, so the count states both (the pre-#1193 count);
        configured template arguments are what the generation also sends and
        win on a shared key. `chat_template` and `none`: exactly the
        generation's own `chat_template_kwargs`.
        """
        if self.effort_transport != "top_level":
            return dict(self.extra_fields(effort).get(TEMPLATE_KWARGS_KEY, {}))
        implied: dict[str, Any] = (
            {EFFORT_KEY: effort, "enable_thinking": effort != "none"} if effort else {}
        )
        return {**implied, **self.extra_fields(effort).get(TEMPLATE_KWARGS_KEY, {})}


DEFAULT_MODEL_REQUEST_ARGS = ModelRequestArgs()
