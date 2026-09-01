"""Tests for agent/tokenizer.py (vLLM /tokenize client and fallback)."""

from types import SimpleNamespace

from mainframe_rag.agent.tokenizer import (
    FallbackTokenizer,
    VllmTokenizer,
    build_tokenizer,
    estimate_tokens,
)
from mainframe_rag.config import Settings


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_text():
    count = estimate_tokens("Hello world, this is a test.")
    assert count >= 6


def test_fallback_tokenizer():
    tok = FallbackTokenizer()
    assert tok.count_tokens("IEA500I IOSCMDS") >= 2


def test_vllm_tokenizer_calls_endpoint():
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
    assert captured["url"] == "http://mock-llm:8000/v1/tokenize"
    assert captured["json"]["model"] == "mock-reasoning"


def test_vllm_tokenizer_fallback_on_error():
    class ErrorClient:
        def post(self, url, json, timeout=5.0):
            raise RuntimeError("network down")

    tok = VllmTokenizer(
        base_url="http://mock-llm:8000/v1",
        model="mock-reasoning",
        client=ErrorClient(),
    )
    # Should not raise; falls back to FallbackTokenizer
    tokens = tok.count_tokens("Some query text to tokenize")
    assert tokens >= 4


def test_build_tokenizer_dispatch():
    s_empty = Settings(_env_file=None)
    assert isinstance(build_tokenizer(s_empty), FallbackTokenizer)

    s_vllm = Settings(
        llm_base_url="http://localhost:8000/v1",
        llm_model_reasoning="mock-model",
        _env_file=None,
    )
    assert isinstance(build_tokenizer(s_vllm), VllmTokenizer)
