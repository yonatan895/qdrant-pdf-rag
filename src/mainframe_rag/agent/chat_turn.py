"""One active-user boundary for chat routes, retrieval and prompt builders."""

from __future__ import annotations

import re
from dataclasses import dataclass

from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage

# NUL and C0 controls except \t \n \r (issue #579). DEL and C1 are not
# rejected: they are not C0, and \x85 already counts as whitespace below.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def is_unsearchable_query(text: str) -> bool:
    """True for text with no content after strip() or with NUL/C0 controls.

    A predicate only: callers keep searching the text unmodified, so leading
    and trailing whitespace around real text stays legitimate. Shared by the
    search/answer length guard and the chat active-turn boundary.
    """
    return not text.strip() or _CONTROL_CHARS.search(text) is not None


def has_lone_surrogate(text: str) -> bool:
    """True when text holds an unpaired surrogate, which cannot be UTF-8 encoded
    and would fail later at the prompt/model boundary (issue #1013)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def validate_request_text(
    settings: Settings,
    *,
    query: str | None = None,
    product: str | None = None,
    version: str | None = None,
    splunk_context: str | None = None,
    messages: list[ChatMessage] | None = None,
) -> None:
    """Bound and encode-check the caller-supplied prompt text (issues #1011, #1013).

    Runs at the request model, before retrieval or any model call. `product`
    and `version` are bounded by Settings.query_max_chars; chat bodies
    (`messages` given) are also held to Settings.chat_max_body_chars across
    every field. Every field must be UTF-8 encodable. Raises ValueError with a
    fixed message and never echoes the value, so the 422 envelope and logs
    carry no input text.
    """
    for value in (product, version):
        if value is not None and len(value) > settings.query_max_chars:
            raise ValueError("field exceeds the character limit")
    if messages is not None and (
        chat_body_chars(messages, splunk_context, product, version) > settings.chat_max_body_chars
    ):
        raise ValueError("chat body exceeds the character limit")
    texts = [query, product, version, splunk_context, *(m.content for m in messages or [])]
    if any(t is not None and has_lone_surrogate(t) for t in texts):
        raise ValueError("field contains an unpaired surrogate")


class InvalidChatTurn(ValueError):
    """Invalid conversation input; HTTP adapters supply the fixed 422 envelope."""


@dataclass(frozen=True)
class PreparedChatTurn:
    query: str
    history: tuple[ChatMessage, ...]

    @property
    def messages(self) -> list[ChatMessage]:
        return [*self.history, ChatMessage(role="user", content=self.query)]


def chat_body_chars(
    messages: list[ChatMessage],
    splunk_context: str | None = None,
    product: str | None = None,
    version: str | None = None,
) -> int:
    """Count the complete supplied body, including entries omitted from prompts."""
    extra = sum(len(v) for v in (splunk_context, product, version) if v)
    return sum(len(m.content) for m in messages) + extra


def prepare_chat_turn(
    messages: list[ChatMessage],
    settings: Settings | None = None,
    splunk_context: str | None = None,
) -> PreparedChatTurn:
    """Select and validate the latest user, retaining only its earlier history.

    Later assistant/system entries remain accepted request syntax but cannot
    become this turn's question or history. Caller system messages never enter
    the model prompt. Check raw body size before excluding any supplied entry.
    """
    if settings is not None and chat_body_chars(messages, splunk_context) > settings.chat_max_body_chars:
        raise InvalidChatTurn("chat body exceeds the character limit")
    active = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), None)
    if active is None:
        raise InvalidChatTurn("a user message is required")
    if is_unsearchable_query(messages[active].content):
        raise InvalidChatTurn("the active user message is blank or has control characters")
    query = messages[active].content.strip()
    if settings is not None and len(query) > settings.query_max_chars:
        raise InvalidChatTurn("the active user message exceeds the character limit")
    return PreparedChatTurn(
        query=query,
        history=tuple(m for m in messages[:active] if m.role != "system"),
    )
