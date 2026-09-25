"""Normalize legacy chat/stream seams once; never own or close the shared client."""
from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol, cast, runtime_checkable

from mainframe_rag.agent.answer import (
    REASON_MALFORMED_FRAME,
    REASON_MISSING_FINISH,
    TruncatedStreamError,
    as_chat_result,
)
from mainframe_rag.agent.core_ports import ModelDone, ModelEvent, ModelToken
from mainframe_rag.ports import ChatMessage, ChatResult, LLMClient, TokenUsage


@runtime_checkable
class StreamingClient(Protocol):
    def chat_stream(
        self, messages: list[ChatMessage], reasoning_effort: str | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[Mapping[str, object]]: ...


class ModelAdapter:
    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def chat(
        self, messages: list[ChatMessage], reasoning_effort: str | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        result = self._client.chat(
            messages, reasoning_effort=reasoning_effort, temperature=temperature
        )
        return as_chat_result(await result if inspect.isawaitable(result) else result)

    async def stream(
        self, messages: list[ChatMessage], reasoning_effort: str, temperature: float,
    ) -> AsyncGenerator[ModelEvent]:
        if not isinstance(self._client, StreamingClient):
            result = await self.chat(messages, reasoning_effort, temperature)
            yield ModelToken(result.content, result.ttft_ms)
            yield ModelDone(result.finish_reason, result.usage, result.ttft_ms)
            return
        stream = self._client.chat_stream(
            messages, reasoning_effort=reasoning_effort, temperature=temperature
        )
        tokens = 0
        done = False
        try:
            async for item in stream:
                if done or not isinstance(item, Mapping) or "error" in item:
                    raise TruncatedStreamError(tokens, REASON_MALFORMED_FRAME)
                kind = item.get("type")
                ttft = item.get("ttft_ms")
                if ttft is not None and (not isinstance(ttft, int) or isinstance(ttft, bool)):
                    raise TruncatedStreamError(tokens, REASON_MALFORMED_FRAME)
                if kind == "token":
                    delta = item.get("delta")
                    if not isinstance(delta, str):
                        raise TruncatedStreamError(tokens, REASON_MALFORMED_FRAME)
                    if delta:
                        tokens += 1
                        yield ModelToken(delta, ttft)
                elif kind == "done":
                    finish = item.get("finish_reason")
                    if finish is None:
                        raise TruncatedStreamError(tokens, REASON_MISSING_FINISH)
                    if not isinstance(finish, str) or not finish:
                        raise TruncatedStreamError(tokens, REASON_MALFORMED_FRAME)
                    usage = item.get("usage", TokenUsage())
                    if not isinstance(usage, TokenUsage):
                        raise TruncatedStreamError(tokens, REASON_MALFORMED_FRAME)
                    done = True
                    yield ModelDone(finish, usage, ttft)
                else:
                    raise TruncatedStreamError(tokens, REASON_MALFORMED_FRAME)
            if not done:
                raise TruncatedStreamError(tokens, REASON_MISSING_FINISH)
        finally:
            # Async generators hold an upstream HTTP response across yields.
            # Close that operation on cancellation, never the shared HTTP pool.
            close = getattr(stream, "aclose", None)
            if close is not None:
                await cast(Callable[[], Awaitable[None]], close)()
