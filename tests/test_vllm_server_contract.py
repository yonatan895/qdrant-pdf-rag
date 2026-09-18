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


import json
from contextlib import asynccontextmanager

import pytest

from mainframe_rag.agent.answer import (
    REASON_MALFORMED_FRAME,
    REASON_MISSING_FINISH,
    REASON_UPSTREAM_ERROR,
    HttpxLLMClient,
    TruncatedStreamError,
)
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage
from tests.fakes import HttpxStreamFake, PostResp, StreamResp, settings_kw


def _settings_kwargs(**overrides):
    return settings_kw(**overrides)


def _msgs():
    return [ChatMessage(role="user", content="What does IEA500I mean?")]


def _stream_lines(lines):
    return HttpxStreamFake(lines=lines)


def _post_payload(payload):
    return HttpxStreamFake(payload=payload)


@pytest.mark.anyio
async def test_stream_request_shape_targets_completions_with_usage_ask():
    """The stream leg must POST {base}/chat/completions with stream=true and
    stream_options.include_usage — the client cannot build TokenUsage when a
    server silently ignores the usage ask."""
    seen = {}
    fake = _stream_lines(
        [
            'data: {"choices": [{"delta": {"content": "hi"}}]}',
            'data: {"choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}',
            "data: [DONE]",
        ]
    )
    fake.capture = seen

    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=fake)
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
    varies, and an explicitly finished terminal shape is required (issue
    #365). None of that may break termination or content."""
    lines = [
        ": ping",
        "",
        'data: {"choices": [{"delta": {"content": "A"}}]}',
        ": ping",
        'data:{"choices": [{"delta": {"content": "B"}}]}',
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
        "data:  [DONE]  ",
    ]
    fake = _stream_lines(lines)

    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = [item async for item in llm.chat_stream(_msgs())]
    assert "".join(i["delta"] for i in items if i["type"] == "token") == "AB"
    assert items[-1] == {
        "type": "done",
        "finish_reason": "stop",
        "usage": items[-1]["usage"],
        "ttft_ms": items[-1]["ttft_ms"],
    }


@pytest.mark.anyio
async def test_stream_tolerates_usage_only_and_finish_only_frames():
    """Supported protocol variants (issue #365): an OpenAI include_usage
    frame (`choices: []`) and a finish-only delta frame are not malformed."""
    lines = [
        'data: {"choices": [{"delta": {"content": "hi"}}]}',
        'data: {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}',
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
        "data: [DONE]",
    ]
    fake = _stream_lines(lines)

    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = [item async for item in llm.chat_stream(_msgs())]
    assert items[-1]["type"] == "done"
    assert items[-1]["finish_reason"] == "stop"
    assert items[-1]["usage"].total_tokens == 4
    assert fake.post_bodies == []


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
    fake = _stream_lines(lines)

    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=fake)
    result = await llm.achat(_msgs())
    assert result.content == "hi"
    assert (
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        result.usage.total_tokens,
    ) == (0, 0, 0)


def test_nonstream_post_shape_and_explicit_finish():
    """llm_stream=False posts WITHOUT a stream key; an explicit finish_reason
    is preserved and absent usage defaults, never KeyError."""
    seen = {}
    fake = _post_payload(
        {"choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}]}
    )
    fake.capture = seen

    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    result = llm.chat(_msgs())
    assert seen["url"] == "http://llm.internal:8000/v1/chat/completions"
    assert "stream" not in seen["json"]
    assert seen["json"]["model"] == "trial-reasoning-model"
    assert result.content == "ans"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 0


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"choices": [{"message": {"content": "ans"}}]}, REASON_MISSING_FINISH),
        (
            {"choices": [{"message": {"content": "ans"}, "finish_reason": None}]},
            REASON_MISSING_FINISH,
        ),
        (
            {"choices": [{"message": {"content": "ans"}, "finish_reason": 3}]},
            REASON_MALFORMED_FRAME,
        ),
        (
            {
                "error": {"message": "PRIVATE_SENTINEL"},
                "choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}],
            },
            REASON_UPSTREAM_ERROR,
        ),
        (
            {
                "error": "upstream exploded",
                "choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}],
            },
            REASON_UPSTREAM_ERROR,
        ),
        (
            {"choices": [{"message": {"content": {"unexpected": "shape"}}, "finish_reason": "stop"}]},
            REASON_MALFORMED_FRAME,
        ),
        (
            {"choices": [{"message": {"content": ["unexpected", "list"]}, "finish_reason": "stop"}]},
            REASON_MALFORMED_FRAME,
        ),
        (
            {"choices": [{"message": {"content": 42}, "finish_reason": "stop"}]},
            REASON_MALFORMED_FRAME,
        ),
    ],
)
def test_nonstream_payload_without_explicit_finish_fails_closed(payload, reason):
    """A non-streaming payload without an explicit terminal finish, with a
    top-level error, or with non-string content fails closed (issues #365 / Q420-P1)."""
    fake = _post_payload(payload)

    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    with pytest.raises(TruncatedStreamError) as excinfo:
        llm.chat(_msgs())
    assert excinfo.value.reason == reason


@pytest.mark.parametrize(
    "payload, expected_content, expected_finish",
    [
        (
            {"choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}]},
            "ans",
            "stop",
        ),
        (
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            "",
            "stop",
        ),
        (
            {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]},
            "",
            "stop",
        ),
        (
            {
                "error": None,
                "choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}],
            },
            "ans",
            "stop",
        ),
        (
            {"choices": [{"message": {"content": "ans"}, "finish_reason": "length"}]},
            "ans",
            "length",
        ),
        (
            {
                "choices": [
                    {"message": {"content": "ans"}, "finish_reason": "content_filter"}
                ]
            },
            "ans",
            "content_filter",
        ),
    ],
)
def test_nonstream_payload_healthy_variants(payload, expected_content, expected_finish):
    """Healthy non-streaming payloads, including null/empty content, error: None,
    and explicit terminal classifications, are preserved (issues #365 / Q420-P1)."""
    fake = _post_payload(payload)
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    result = llm.chat(_msgs())
    assert result.content == expected_content
    assert result.finish_reason == expected_finish


def test_nonstream_client_sync_rejects_error_payload():
    """Sync chat() rejects top-level error payloads without leaking
    error-body text into the exception (issues #365 / Q420-P1)."""
    payload = {
        "error": {"message": "PRIVATE_SENTINEL"},
        "choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}],
    }
    fake = _post_payload(payload)
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)

    with pytest.raises(TruncatedStreamError) as excinfo:
        llm.chat(_msgs())
    assert excinfo.value.reason == REASON_UPSTREAM_ERROR
    assert "PRIVATE_SENTINEL" not in str(excinfo.value)


@pytest.mark.anyio
async def test_nonstream_client_async_rejects_error_payload():
    """Async achat() rejects top-level error payloads without leaking
    error-body text into the exception (issues #365 / Q420-P1)."""
    payload = {
        "error": {"message": "PRIVATE_SENTINEL"},
        "choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}],
    }
    fake = _post_payload(payload)
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)

    with pytest.raises(TruncatedStreamError) as excinfo:
        await llm.achat(_msgs())
    assert excinfo.value.reason == REASON_UPSTREAM_ERROR
    assert "PRIVATE_SENTINEL" not in str(excinfo.value)


def test_nonstream_client_sync_rejects_object_content():
    """Sync chat() rejects object content instead of stringifying
    it into answer prose (issues #365 / Q420-P1)."""
    payload = {
        "choices": [
            {"message": {"content": {"unexpected": "shape"}}, "finish_reason": "stop"}
        ],
    }
    fake = _post_payload(payload)
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)

    with pytest.raises(TruncatedStreamError) as excinfo:
        llm.chat(_msgs())
    assert excinfo.value.reason == REASON_MALFORMED_FRAME


@pytest.mark.anyio
async def test_nonstream_client_async_rejects_object_content():
    """Async achat() rejects object content instead of stringifying
    it into answer prose (issues #365 / Q420-P1)."""
    payload = {
        "choices": [
            {"message": {"content": {"unexpected": "shape"}}, "finish_reason": "stop"}
        ],
    }
    fake = _post_payload(payload)
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)

    with pytest.raises(TruncatedStreamError) as excinfo:
        await llm.achat(_msgs())
    assert excinfo.value.reason == REASON_MALFORMED_FRAME


def test_nonstream_nested_reasoning_tokens_mapped():
    """vLLM reports reasoning usage nested under completion_tokens_details;
    the client must surface it (drives cost/observability accounting)."""
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "completion_tokens_details": {"reasoning_tokens": 12},
    }

    fake = _post_payload(
        {
            "choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}],
            "usage": usage,
        }
    )

    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    result = llm.chat(_msgs())
    assert result.usage.reasoning_tokens == 12
    assert result.usage.total_tokens == 15


@pytest.mark.anyio
async def test_stream_and_nonstream_bodies_agree_on_model_and_messages():
    """The streaming twin must ask for the same model/messages as the
    non-streaming POST — a trial server must serve both identically."""

    fake = HttpxStreamFake(
        lines=["data: [DONE]"],
        payload={"choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}]},
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=fake)
    await llm.achat(_msgs())  # empty stream -> recovery POST fires
    assert fake.capture["json"] is not None and fake.post_bodies[-1] is not None
    for key in ("model", "messages"):
        assert fake.stream_bodies[0][key] == fake.post_bodies[-1][key]
    assert fake.stream_bodies[0]["stream"] is True
    assert "stream" not in fake.post_bodies[-1]


@pytest.mark.anyio
async def test_reasoning_effort_and_temperature_sent_only_when_set():
    """Effort routing (low/high) depends on the server accepting the
    vLLM-specific reasoning_effort field; unset params must be absent so
    servers with strict schemas are not broken by nulls."""
    fake = _stream_lines(
        [
            'data: {"choices": [{"delta": {"content": "x"}}]}',
            'data: {"choices": [{"finish_reason": "stop"}]}',
            "data: [DONE]",
        ]
    )

    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=fake)
    await llm.achat(_msgs(), reasoning_effort="high", temperature=0.2)
    await llm.achat(_msgs())
    bodies = fake.stream_bodies
    assert bodies[0]["reasoning_effort"] == "high"
    assert bodies[0]["temperature"] == 0.2
    assert "reasoning_effort" not in bodies[1]
    assert "temperature" not in bodies[1]


def test_trailing_slash_base_url_builds_clean_completions_url():
    """A trailing slash in LLM_BASE_URL must not produce //chat/completions
    (some servers 404 the doubled slash while vLLM tolerates it)."""

    fake = _post_payload({"choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}]})

    llm = HttpxLLMClient(
        Settings(**_settings_kwargs(llm_base_url="http://llm.internal:8000/v1/")), client=fake
    )
    llm.chat(_msgs())
    assert fake.capture["url"] == "http://llm.internal:8000/v1/chat/completions"


@pytest.mark.anyio
async def test_bearer_header_sent_on_stream_leg_when_key_set():
    """A gateway-guarded endpoint must see the virtual key on the SSE leg."""
    seen = {}
    fake = _stream_lines(
        [
            'data: {"choices": [{"delta": {"content": "hi"}}]}',
            'data: {"choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}',
            "data: [DONE]",
        ]
    )
    fake.capture = seen

    llm = HttpxLLMClient(
        Settings(**_settings_kwargs(llm_stream=True, llm_api_key="sk-test-llm")), client=fake
    )
    await llm.achat(_msgs())
    assert seen["headers"] == {"Authorization": "Bearer sk-test-llm"}


def test_bearer_header_sent_on_nonstream_post_when_key_set():
    """The non-streaming POST (and the stream-fallback leg) carries the key."""
    seen = {}
    fake = _post_payload({"choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}]})
    fake.capture = seen

    llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_api_key="sk-test-llm")), client=fake)
    llm.chat(_msgs())
    assert seen["headers"] == {"Authorization": "Bearer sk-test-llm"}


@pytest.mark.anyio
async def test_no_auth_header_sent_on_stream_leg_when_key_unset():
    """Keyless setups keep the pre-gateway wire shape: no Authorization
    header on the SSE leg."""
    stream_seen: dict = {}
    stream_fake = _stream_lines(
        [
            'data: {"choices": [{"delta": {"content": "hi"}}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
            "data: [DONE]",
        ]
    )
    stream_fake.capture = stream_seen
    stream_llm = HttpxLLMClient(Settings(**_settings_kwargs(llm_stream=True)), client=stream_fake)
    await stream_llm.achat(_msgs())
    assert stream_seen["headers"] == {}


def test_no_auth_header_sent_on_nonstream_post_when_key_unset():
    """Keyless setups keep the pre-gateway wire shape: no Authorization
    header on the non-streaming POST."""
    post_seen: dict = {}
    post_fake = _post_payload({"choices": [{"message": {"content": "ans"}, "finish_reason": "stop"}]})
    post_fake.capture = post_seen
    post_llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=post_fake)
    post_llm.chat(_msgs())
    assert post_seen["headers"] == {}


@pytest.mark.anyio
async def test_chat_stream_empty_fallback_posts_without_stream_flag():
    """Issue #363: the chat_stream empty-content recovery POST must carry
    non-streaming semantics (no stream key, no stream_options) while keeping
    model/messages/reasoning_effort/temperature identical to the SSE leg. A
    protocol-aware backend answers stream=True with SSE, which resp.json()
    cannot parse — so the flag is asserted on the wire body, not just the
    parsed result. Fails on the pre-fix body reuse."""
    fake = HttpxStreamFake(
        lines=[
            'data: {"choices": [{"delta": {}}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}',
            "data: [DONE]",
        ],
        payload={
            "choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = [
        item
        async for item in llm.chat_stream(_msgs(), reasoning_effort="high", temperature=0.2)
    ]
    assert fake.stream_bodies[0]["stream"] is True
    fallback = fake.post_bodies[-1]
    assert "stream" not in fallback
    assert "stream_options" not in fallback
    for key in ("model", "messages", "reasoning_effort", "temperature"):
        assert fallback[key] == fake.stream_bodies[0][key]
    assert [i["type"] for i in items] == ["token", "done"]
    assert items[0]["delta"] == "recovered"
    assert items[0]["ttft_ms"] is not None
    assert items[-1]["finish_reason"] == "stop"
    assert items[-1]["usage"].total_tokens == 8


@pytest.mark.anyio
async def test_chat_stream_fallback_sse_bytes_raise_without_fabricated_done():
    """Issue #363: if the fallback answers SSE (the server honored a stale
    stream=True), resp.json() fails — that must surface as an error, never a
    token/done(stop) fabricated from unparsed bytes."""

    def _sse_instead_of_json():
        raise json.JSONDecodeError("Expecting value", "data: {", 0)

    fake = HttpxStreamFake(
        lines=['data: {"choices": [{"delta": {}}]}', "data: [DONE]"],
        payload=_sse_instead_of_json,
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = []
    with pytest.raises(json.JSONDecodeError):
        async for item in llm.chat_stream(_msgs()):
            items.append(item)
    assert items == []


@pytest.mark.anyio
async def test_chat_stream_empty_fallback_result_raises_without_token_or_done():
    """Issue #363: an empty fallback is a failed generation, not a silent
    success — raising takes the app's event: error path instead of shipping
    done(stop) with no content."""
    fake = HttpxStreamFake(
        lines=["data: [DONE]"],
        payload={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
    )
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = []
    with pytest.raises(RuntimeError, match="empty content"):
        async for item in llm.chat_stream(_msgs()):
            items.append(item)
    assert items == []


@pytest.mark.anyio
async def test_chat_stream_rejected_fallback_propagates_without_done():
    """Issue #363: a rejected fallback (auth/overload) must propagate as a
    failure — no token, no done, exactly one fallback attempt."""

    class _RejectingClient:
        def __init__(self, lines):
            self._lines = lines
            self.post_bodies: list = []

        @asynccontextmanager
        async def stream(self, method, url, json=None, headers=None):
            yield StreamResp(self._lines)

        async def post(self, url, json=None, headers=None):
            self.post_bodies.append(json)
            return PostResp({"error": "overloaded"}, status_code=500)

    fake = _RejectingClient(lines=["data: [DONE]"])
    llm = HttpxLLMClient(Settings(**_settings_kwargs()), client=fake)
    items = []
    with pytest.raises(RuntimeError, match="HTTP 500"):
        async for item in llm.chat_stream(_msgs()):
            items.append(item)
    assert items == []
    assert len(fake.post_bodies) == 1
    assert "stream" not in fake.post_bodies[0]
