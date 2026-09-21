"""One active-user boundary for chat routes, retrieval and prompt builders."""

from __future__ import annotations

from dataclasses import dataclass

from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage


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
    query = messages[active].content.strip()
    if not query:
        raise InvalidChatTurn("the active user message is blank")
    if settings is not None and len(query) > settings.query_max_chars:
        raise InvalidChatTurn("the active user message exceeds the character limit")
    return PreparedChatTurn(
        query=query,
        history=tuple(m for m in messages[:active] if m.role != "system"),
    )
