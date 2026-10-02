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


class InvalidChatTurn(ValueError):
    """Invalid conversation input; HTTP adapters supply the fixed 422 envelope."""


@dataclass(frozen=True)
class PreparedChatTurn:
    query: str
    history: tuple[ChatMessage, ...]

    @property
    def messages(self) -> list[ChatMessage]:
        return [*self.history, ChatMessage(role="user", content=self.query)]


def chat_body_chars(messages: list[ChatMessage], splunk_context: str | None = None) -> int:
    """Count the complete supplied body, including entries omitted from prompts."""
    return sum(len(m.content) for m in messages) + (len(splunk_context) if splunk_context else 0)


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
