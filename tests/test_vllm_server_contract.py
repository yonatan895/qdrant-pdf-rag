"""vLLM server-contract battery (bigger-reasoning-model trials).

Client-side pins for the exact wire behaviors a trial reasoning server must
reproduce: stream request shape (stream_options usage ask), SSE keepalive
tolerance, non-stream POST shape, nested usage mapping, and
reasoning_effort/temperature pass-through. A candidate server (CUDA quant,
CPU backend, or anything else) that fails this battery is rejected without
running quality trials — the battery, not the engine name, is the
exact-API proof.

Hermetic: fake transports, no network, no GPU.
"""

from contextlib import asynccontextmanager

import pytest

from mainframe_rag.agent.answer import HttpxLLMClient
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage


def _settings_kwargs(**overrides):
    base = {
        "llm_base_url": "http://llm.internal:8000/v1",
        "llm_model_reasoning": "trial-reasoning-model",
        "_env_file": None,
    }
    base.update(overrides)
    return base


def _msgs():
    return [ChatMessage(role="user", content="What does IEA500I mean?")]


class _StreamResp:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def __iter__(self):
        return iter(self._lines)

    def iter_lines(self):
        return iter(self._lines)


class _PostResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.mark.anyio
async def test_stream_request_shape_targets_completions_with_usage_ask():
    """The stream leg must POST {base}/chat/completions with stream=true and
    stream_options.include_usage — the client cannot build TokenUsage when a
    server silently ignores the usage ask."""
    seen = {}

    class FakeHttp:
        @asynccontextmanager
        async def stream(self, method, url, json=None):
            seen["method"] = method
            seen["url"] = url
            seen["json"] = json
            yield _StreamResp(
                [
                    'data: {"choices": [{"delta": {"content": "hi"}}]}',
                    'data: {"choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}',
                    "data: [DONE]",
                ]
            )

    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=FakeHttp())
    result = await llm.achat(_msgs())
    assert seen["method"] == "POST"
    assert seen["url"] == "http://llm.internal:8000/v1/chat/completions"
    assert seen["json"]["stream"] is True
    assert seen["json"]["stream_options"] == {"include_usage": True}
    assert seen["json"]["model"] == "trial-reasoning-model"
    assert [m["role"] for m in seen["json"]["messages"]] == ["user"]
    assert result.usage.total_tokens == 4


@pytest.mark.anyio
async def test_stream_tolerates_sse_keepalives_and_done_spacing():
    """vLLM emits `: ` comment keepalives; spacing around data:/[DONE]
    varies. None of that may break termination or content."""
    lines = [
        ": ping",
        "",
        'data: {"choices": [{"delta": {"content": "A"}}]}',
        ": ping",
        'data:{"choices": [{"delta": {"content": "B"}}]}',
        "data:  [DONE]  ",
    ]

    class FakeHttp:
        @asynccontextmanager
        async def stream(self, method, url, json=None):
            yield _StreamResp(lines)

    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=FakeHttp())
    items = [item async for item in llm.chat_stream(_msgs())]
    assert "".join(i["delta"] for i in items if i["type"] == "token") == "AB"
    assert items[-1] == {
        "type": "done",
        "finish_reason": "stop",
        "usage": items[-1]["usage"],
        "ttft_ms": items[-1]["ttft_ms"],
    }


@pytest.mark.anyio
async def test_stream_without_usage_chunk_yields_zero_usage():
    """Documents the silent failure when a server ignores stream_options:
    no usage chunk means zero TokenUsage — no error, no retry. Trial
    servers must therefore honor the usage ask (first test)."""
    lines = [
        'data: {"choices": [{"delta": {"content": "hi"}}]}',
        'data: {"choices": [{"finish_reason": "stop"}]}',
        "data: [DONE]",
    ]

    class FakeHttp:
        @asynccontextmanager
        async def stream(self, method, url, json=None):
            yield _StreamResp(lines)

    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=FakeHttp())
    result = await llm.achat(_msgs())
    assert result.content == "hi"
    assert (
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        result.usage.total_tokens,
    ) == (0, 0, 0)


def test_nonstream_post_shape_and_stop_default():
    """llm_stream=False posts WITHOUT a stream key; a null finish_reason
    and absent usage must default, never KeyError."""
    seen = {}

    class FakeHttp:
        def post(self, url, json=None):
            seen["url"] = url
            seen["json"] = json
            return _PostResp({"choices": [{"message": {"content": "ans"}, "finish_reason": None}]})

    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=FakeHttp())
    result = llm.chat(_msgs())
    assert seen["url"] == "http://llm.internal:8000/v1/chat/completions"
    assert "stream" not in seen["json"]
    assert seen["json"]["model"] == "trial-reasoning-model"
    assert result.content == "ans"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 0


def test_nonstream_nested_reasoning_tokens_mapped():
    """vLLM reports reasoning usage nested under completion_tokens_details;
    the client must surface it (drives cost/observability accounting)."""
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "completion_tokens_details": {"reasoning_tokens": 12},
    }

    class FakeHttp:
        def post(self, url, json=None):
            return _PostResp(
                {
                    "choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}],
                    "usage": usage,
                }
            )

    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=FakeHttp())
    result = llm.chat(_msgs())
    assert result.usage.reasoning_tokens == 12
    assert result.usage.total_tokens == 15


@pytest.mark.anyio
async def test_stream_and_nonstream_bodies_agree_on_model_and_messages():
    """The streaming twin must ask for the same model/messages as the
    non-streaming POST — a trial server must serve both identically."""

    class FakeHttp:
        def __init__(self):
            self.stream_json = None
            self.post_json = None

        @asynccontextmanager
        async def stream(self, method, url, json=None):
            self.stream_json = json
            yield _StreamResp(["data: [DONE]"])

        async def post(self, url, json=None):
            self.post_json = json
            return _PostResp(
                {"choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}]}
            )

    fake = FakeHttp()
    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=fake)
    await llm.achat(_msgs())  # empty stream -> recovery POST fires
    assert fake.stream_json is not None and fake.post_json is not None
    for key in ("model", "messages"):
        assert fake.stream_json[key] == fake.post_json[key]
    assert fake.stream_json["stream"] is True
    assert "stream" not in fake.post_json


@pytest.mark.anyio
async def test_reasoning_effort_and_temperature_sent_only_when_set():
    """Effort routing (low/high) depends on the server accepting the
    vLLM-specific reasoning_effort field; unset params must be absent so
    servers with strict schemas are not broken by nulls."""
    bodies = []

    class FakeHttp:
        @asynccontextmanager
        async def stream(self, method, url, json=None):
            bodies.append(json)
            yield _StreamResp(
                [
                    'data: {"choices": [{"delta": {"content": "x"}}]}',
                    'data: {"choices": [{"finish_reason": "stop"}]}',
                    "data: [DONE]",
                ]
            )

    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=FakeHttp())
    await llm.achat(_msgs(), reasoning_effort="high", temperature=0.2)
    await llm.achat(_msgs())
    assert bodies[0]["reasoning_effort"] == "high"
    assert bodies[0]["temperature"] == 0.2
    assert "reasoning_effort" not in bodies[1]
    assert "temperature" not in bodies[1]


def test_trailing_slash_base_url_builds_clean_completions_url():
    """A trailing slash in LLM_BASE_URL must not produce //chat/completions
    (some servers 404 the doubled slash while vLLM tolerates it)."""

    class FakeHttp:
        def post(self, url, json=None):
            self.url = url
            return _PostResp(
                {"choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}]}
            )

    fake = FakeHttp()
    llm = HttpxLLMClient(
        Settings(**_settings_kwargs(llm_base_url="http://llm.internal:8000/v1/")), client=fake
    )
    llm.chat(_msgs())
    assert fake.url == "http://llm.internal:8000/v1/chat/completions"
