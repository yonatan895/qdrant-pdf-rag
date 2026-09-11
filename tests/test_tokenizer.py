"""Tests for agent/tokenizer.py (vLLM /tokenize client and fallback)."""

import logging

from mainframe_rag.agent.tokenizer import (
    FallbackTokenizer,
    VllmTokenizer,
    build_tokenizer,
    estimate_tokens,
)
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage
from tests.fakes import TokenizerPostFake


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_text():
    count = estimate_tokens("Hello world, this is a test.")
    assert count >= 6


def test_fallback_tokenizer():
    tok = FallbackTokenizer()
    assert tok.count_tokens("IEA500I IOSCMDS") >= 2


def test_fallback_count_messages():
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


def test_vllm_tokenizer_sends_bearer_when_key_set():
    """A gateway-guarded /tokenize must see the reasoning leg's virtual key."""
    captured = {}
    client = TokenizerPostFake(count=7, capture=captured)

    tok = VllmTokenizer(
        base_url="http://mock-llm:8000/v1", model="m", client=client, api_key="sk-test-llm"
    )
    assert tok.count_tokens("abc") == 7
    assert captured["headers"] == {"Authorization": "Bearer sk-test-llm"}


def test_vllm_tokenizer_omits_auth_when_key_unset():
    """Keyless setups keep the pre-gateway /tokenize shape: no header."""
    captured = {}
    client = TokenizerPostFake(count=7, capture=captured)

    tok = VllmTokenizer(base_url="http://mock-llm:8000/v1", model="m", client=client)
    assert tok.count_tokens("abc") == 7
    assert captured["headers"] == {}


def test_build_tokenizer_wires_llm_api_key():
    """build_tokenizer passes the reasoning leg's key into the client."""
    captured = {}
    client = TokenizerPostFake(count=7, capture=captured)

    tok = build_tokenizer(
        Settings(
            llm_base_url="http://mock-llm:8000/v1",
            llm_model_reasoning="m",
            llm_api_key="sk-test-llm",
            _env_file=None,
        ),
        client=client,
    )
    assert tok.count_tokens("abc") == 7
    assert captured["headers"] == {"Authorization": "Bearer sk-test-llm"}


def test_vllm_tokenizer_base_without_v1_unchanged():
    captured = {}
    client = TokenizerPostFake(count=3, capture=captured)

    tok = VllmTokenizer(base_url="http://mock-llm:8000", model="m", client=client)
    assert tok.count_tokens("abc") == 3
    assert captured["url"] == "http://mock-llm:8000/tokenize"


def test_vllm_tokenizer_count_messages_posts_messages_shape():
    """The verification count is chat-template aware: vLLM /tokenize accepts
    the message list, and that is what consumes max_model_len."""
    captured = {}
    client = TokenizerPostFake(count=11, capture=captured)

    tok = VllmTokenizer(base_url="http://mock-llm:8000/v1", model="mock-reasoning", client=client)
    messages = [
        ChatMessage(role="system", content="System prompt."),
        ChatMessage(role="user", content="Question: IEA500I?"),
    ]
    assert tok.count_messages(messages) == 11
    assert captured["url"] == "http://mock-llm:8000/tokenize"
    assert [m["role"] for m in captured["json"]["messages"]] == ["system", "user"]
    assert captured["json"]["model"] == "mock-reasoning"


def test_vllm_tokenizer_non_200_warns_once_then_sticks(caplog):
    """A non-200 (e.g. LiteLLM without /tokenize) must log a warning — not
    silently pass — and permanently pin the estimator: a second call must
    not re-attempt the endpoint."""
    client = TokenizerPostFake(count=0, status_code=404)

    tok = VllmTokenizer(base_url="http://litellm:4000/v1", model="m", client=client)
    with caplog.at_level(logging.WARNING, logger="agent.tokenizer"):
        first = tok.count_tokens("some text to count here")
        second = tok.count_tokens("another text to count")

    assert first >= 5  # estimator fallback answered, not zero
    assert second >= 4
    assert len(client.calls) == 1  # sticky downgrade: no repeated doomed RPCs
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "/tokenize" in warnings[0].getMessage()


def test_vllm_tokenizer_error_fallback_warns(caplog):
    client = TokenizerPostFake(count=0, raises=RuntimeError("network down"))

    tok = VllmTokenizer(
        base_url="http://mock-llm:8000/v1",
        model="mock-reasoning",
        client=client,
    )
    with caplog.at_level(logging.WARNING, logger="agent.tokenizer"):
        tokens = tok.count_tokens("Some query text to tokenize")
    assert tokens >= 4
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_vllm_tokenizer_downgrade_pins_count_messages():
    """After the first /tokenize failure, count_messages must use the
    estimator too — no second network attempt, consistent accounting."""
    client = TokenizerPostFake(count=0, raises=RuntimeError("down"))

    tok = VllmTokenizer(base_url="http://mock-llm:8000/v1", model="m", client=client)
    tok.count_tokens("warm the sticky downgrade")
    messages = [ChatMessage(role="user", content="IEA500I operator action")]
    assert tok.count_messages(messages) == FallbackTokenizer().count_messages(messages)
    assert len(client.calls) == 1


def test_vllm_tokenizer_malformed_200_body_downgrades(caplog):
    """HTTP 200 without a usable count is also a failed endpoint."""

    client = TokenizerPostFake(count=None, extra={"unexpected": "shape"})

    tok = VllmTokenizer(base_url="http://mock-llm:8000/v1", model="m", client=client)
    with caplog.at_level(logging.WARNING, logger="agent.tokenizer"):
        assert tok.count_tokens("some text to count") >= 4
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_build_tokenizer_dispatch():
    s_empty = Settings(_env_file=None)
    assert isinstance(build_tokenizer(s_empty), FallbackTokenizer)

    s_vllm = Settings(
        llm_base_url="http://localhost:8000/v1",
        llm_model_reasoning="mock-model",
        _env_file=None,
    )
    assert isinstance(build_tokenizer(s_vllm), VllmTokenizer)
