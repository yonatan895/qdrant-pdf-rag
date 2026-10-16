"""Tests for agent/tokenizer.py (vLLM /tokenize client and fallback)."""

import json
import logging

import httpx2
import pytest

from mainframe_rag.agent.tokenizer import (
    FallbackTokenizer,
    VllmTokenizer,
    build_tokenizer,
    estimate_tokens,
)
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage
from tests.fakes import TokenizerPostFake


def test_fallback_estimator_counts_text_and_messages():
    assert estimate_tokens("") == 0
    assert estimate_tokens("Hello world, this is a test.") >= 6
    tok = FallbackTokenizer()
    messages = [
        ChatMessage(role="system", content="Be terse."),
        ChatMessage(role="user", content="IEA500I operator action"),
    ]
    assert tok.count_messages(messages) >= sum(tok.count_tokens(m.content) for m in messages)
    assert tok.count_messages([]) == 0


def test_vllm_tokenizer_calls_origin_root():
    """LLM_BASE_URL ends in /v1; vLLM serves /tokenize at the server root,
    so the request must strip /v1 and hit the origin (/v1/tokenize is 404)."""
    captured = {}
    client = TokenizerPostFake(
        count=7, extra={"max_model_len": 4096, "tokens": [1, 2, 3, 4, 5, 6, 7]}, capture=captured
    )

    tok = VllmTokenizer(
        base_url="http://mock-llm:8000/v1",
        model="mock-reasoning",
        client=client,
    )
    tokens = tok.count_tokens("Some query text to tokenize")
    assert tokens == 7
    assert captured["url"] == "http://mock-llm:8000/tokenize"
    assert captured["json"]["model"] == "mock-reasoning"
    bare = {}
    VllmTokenizer(
        base_url="http://mock-llm:8000", model="m", client=TokenizerPostFake(count=3, capture=bare)
    ).count_tokens("abc")
    assert bare["url"] == "http://mock-llm:8000/tokenize"


def test_tokenize_auth_header_follows_the_reasoning_key():
    """A gateway-guarded /tokenize sees the reasoning leg's key; keyless
    setups keep the pre-gateway shape (no header)."""
    keyless = {}
    VllmTokenizer(
        base_url="http://mock-llm:8000/v1", model="m", client=TokenizerPostFake(count=7, capture=keyless)
    ).count_tokens("abc")
    assert keyless["headers"] == {}
    captured = {}
    tok = build_tokenizer(
        Settings(
            llm_base_url="http://mock-llm:8000/v1",
            llm_model_reasoning="m",
            llm_api_key="sk-test-llm",
            _env_file=None,
        ),
        client=TokenizerPostFake(count=7, capture=captured),
    )
    assert tok.count_tokens("abc") == 7
    assert captured["headers"] == {"Authorization": "Bearer sk-test-llm"}


@pytest.mark.parametrize("fake", [
    lambda: TokenizerPostFake(count=0, status_code=404),
    lambda: TokenizerPostFake(count=0, raises=RuntimeError("network down")),
    lambda: TokenizerPostFake(count=None, extra={"unexpected": "shape"}),
], ids=["non-200", "transport-error", "malformed-200-body"])
def test_failed_tokenize_downgrades_permanently_with_one_warning(fake, caplog):
    """A non-200 (e.g. LiteLLM without /tokenize), transport error or 200
    without a usable count must warn, answer from the estimator, flip
    remote_confirmed, and never re-attempt the endpoint (count_messages
    included)."""
    client = fake()
    tok = VllmTokenizer(base_url="http://litellm:4000/v1", model="m", client=client)
    assert tok.remote_confirmed is True
    with caplog.at_level(logging.WARNING, logger="agent.tokenizer"):
        first = tok.count_tokens("some text to count here")
        second = tok.count_tokens("another text to count")
        messages = [ChatMessage(role="user", content="IEA500I operator action")]
        counted = tok.count_messages(messages)
    assert first >= 5 and second >= 4  # estimator answered, not zero
    assert counted == FallbackTokenizer().count_messages(messages)
    assert len(client.calls) == 1  # sticky downgrade: no repeated doomed RPCs
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert tok.remote_confirmed is False


def test_build_tokenizer_dispatch():
    s_empty = Settings(_env_file=None)
    assert isinstance(build_tokenizer(s_empty), FallbackTokenizer)

    s_vllm = Settings(
        llm_base_url="http://localhost:8000/v1",
        llm_model_reasoning="mock-model",
        _env_file=None,
    )
    assert isinstance(build_tokenizer(s_vllm), VllmTokenizer)


@pytest.mark.parametrize("count", [-1, True, "12"])
def test_malformed_count_cannot_verify_even_with_tokens(count):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx2.Response(200, json={"count": count, "tokens": [1, 2]})
    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning", client=client)
        messages = [ChatMessage(role="user", content="synthetic question")]
        expected = FallbackTokenizer().count_messages(messages)
        assert tokenizer.count_messages(messages) == expected
        assert not tokenizer.remote_confirmed
        assert tokenizer.count_messages(messages) == expected
        assert len(calls) == 1


def test_chat_tokenizer_matches_inference_template_flags():
    captured = {}
    tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning",
                              client=TokenizerPostFake(count=12, capture=captured))
    messages = [ChatMessage(role="user", content="synthetic question")]
    assert tokenizer.count_messages(messages) == 12
    assert captured["json"] == {"model": "reasoning", "messages": [messages[0].model_dump()],
                                "add_generation_prompt": True, "add_special_tokens": False}


@pytest.mark.parametrize("effort,count", [("low", 45), ("none", 17), (None, 57)])
def test_chat_token_count_uses_selected_request_template(effort, count):
    """Model-specific counts differ; omitted effort cannot verify a low prompt."""
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        selected = body.get("chat_template_kwargs", {}).get("reasoning_effort")
        return httpx2.Response(200, json={"count": {"low": 45, "medium": 15, "xhigh": 57, "none": 17, None: 57}[selected]})

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning", client=client)
        messages = [ChatMessage(role="user", content="Return the word READY")]
        assert tokenizer.count_messages(messages, reasoning_effort=effort) == count
        assert tokenizer.remote_confirmed
    expected = {"reasoning_effort": effort, "enable_thinking": effort != "none"} if effort else None
    assert bodies[0].get("chat_template_kwargs") == expected


def test_chat_token_counts_do_not_retain_another_requests_effort():
    calls = []

    def respond(request):
        selected = json.loads(request.content).get("chat_template_kwargs", {}).get("reasoning_effort")
        calls.append(selected)
        return httpx2.Response(200, json={"count": {"low": 45, "medium": 15, None: 57}[selected]})

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning", client=client)
        messages = [ChatMessage(role="user", content="Return the word READY")]
        assert tokenizer.count_messages(messages, reasoning_effort="low") == 45
        assert tokenizer.count_messages(messages, reasoning_effort="medium") == 15
        assert tokenizer.count_messages(messages, reasoning_effort="low") == 45
        assert tokenizer.count_messages(messages) == 57
    assert calls == ["low", "medium", "low", None]


def test_effort_specific_count_failure_still_pins_unverified_estimation():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(503)

    messages = [ChatMessage(role="user", content="Original synthetic question")]
    fallback = FallbackTokenizer()
    expected = fallback.count_messages(messages)
    assert fallback.count_messages(messages, reasoning_effort="xhigh") == expected
    assert not fallback.remote_confirmed
    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        tokenizer = VllmTokenizer("http://synthetic/v1", "reasoning", client=client)
        assert tokenizer.count_messages(messages, reasoning_effort="low") == expected
        assert tokenizer.count_messages(messages, reasoning_effort="medium") == expected
        assert not tokenizer.remote_confirmed
    assert len(requests) == 1
