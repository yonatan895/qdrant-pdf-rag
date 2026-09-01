"""Tests for agent/tokenizer.py (vLLM /tokenize client and fallback)."""

import logging
from types import SimpleNamespace

from mainframe_rag.agent.tokenizer import (
    FallbackTokenizer,
    VllmTokenizer,
    build_tokenizer,
    estimate_tokens,
)
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage


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

    class FakeClient:
        def post(self, url, json, timeout=5.0):
            captured["url"] = url
            captured["json"] = json
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"count": 7, "max_model_len": 4096, "tokens": [1, 2, 3, 4, 5, 6, 7]},
            )

    tok = VllmTokenizer(
        base_url="http://mock-llm:8000/v1",
        model="mock-reasoning",
        client=FakeClient(),
    )
    tokens = tok.count_tokens("Some query text to tokenize")
    assert tokens == 7
    assert captured["url"] == "http://mock-llm:8000/tokenize"
    assert captured["json"]["model"] == "mock-reasoning"


def test_vllm_tokenizer_base_without_v1_unchanged():
    captured = {}

    class FakeClient:
        def post(self, url, json, timeout=5.0):
            captured["url"] = url
            return SimpleNamespace(status_code=200, json=lambda: {"count": 3})

    tok = VllmTokenizer(base_url="http://mock-llm:8000", model="m", client=FakeClient())
    assert tok.count_tokens("abc") == 3
    assert captured["url"] == "http://mock-llm:8000/tokenize"


def test_vllm_tokenizer_count_messages_posts_messages_shape():
    """The verification count is chat-template aware: vLLM /tokenize accepts
    the message list, and that is what consumes max_model_len."""
    captured = {}

    class FakeClient:
        def post(self, url, json, timeout=5.0):
            captured["url"] = url
            captured["json"] = json
            return SimpleNamespace(status_code=200, json=lambda: {"count": 11})

    tok = VllmTokenizer(base_url="http://mock-llm:8000/v1", model="mock-reasoning", client=FakeClient())
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
    calls: list[str] = []

    class FakeClient:
        def post(self, url, json, timeout=5.0):
            calls.append(url)
            return SimpleNamespace(status_code=404, json=lambda: {"error": "not found"})

    tok = VllmTokenizer(base_url="http://litellm:4000/v1", model="m", client=FakeClient())
    with caplog.at_level(logging.WARNING, logger="agent.tokenizer"):
        first = tok.count_tokens("some text to count here")
        second = tok.count_tokens("another text to count")

    assert first >= 5  # estimator fallback answered, not zero
    assert second >= 4
    assert len(calls) == 1  # sticky downgrade: no repeated doomed RPCs
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "/tokenize" in warnings[0].getMessage()


def test_vllm_tokenizer_error_fallback_warns(caplog):
    class ErrorClient:
        def post(self, url, json, timeout=5.0):
            raise RuntimeError("network down")

    tok = VllmTokenizer(
        base_url="http://mock-llm:8000/v1",
        model="mock-reasoning",
        client=ErrorClient(),
    )
    with caplog.at_level(logging.WARNING, logger="agent.tokenizer"):
        tokens = tok.count_tokens("Some query text to tokenize")
    assert tokens >= 4
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_vllm_tokenizer_downgrade_pins_count_messages():
    """After the first /tokenize failure, count_messages must use the
    estimator too — no second network attempt, consistent accounting."""
    calls: list[str] = []

    class FailAlways:
        def post(self, url, json, timeout=5.0):
            calls.append(url)
            raise RuntimeError("down")

    tok = VllmTokenizer(base_url="http://mock-llm:8000/v1", model="m", client=FailAlways())
    tok.count_tokens("warm the sticky downgrade")
    messages = [ChatMessage(role="user", content="IEA500I operator action")]
    assert tok.count_messages(messages) == FallbackTokenizer().count_messages(messages)
    assert len(calls) == 1


def test_vllm_tokenizer_malformed_200_body_downgrades(caplog):
    """HTTP 200 without a usable count is also a failed endpoint."""

    class LyingClient:
        def post(self, url, json, timeout=5.0):
            return SimpleNamespace(status_code=200, json=lambda: {"unexpected": "shape"})

    tok = VllmTokenizer(base_url="http://mock-llm:8000/v1", model="m", client=LyingClient())
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
