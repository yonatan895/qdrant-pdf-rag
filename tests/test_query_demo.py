"""Unit tests for scripts/query_demo.py (pure functions, no network/docker)."""

import json
from pathlib import Path
from unittest.mock import patch

import httpx2
import pytest
from scripts.query_demo import (
    main,
    resolve_runtime_settings,
)

from mainframe_rag.agent.answer import HttpxLLMClient
from mainframe_rag.agent.tokenizer import VllmTokenizer
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatResult, TokenUsage
from mainframe_rag.retrieve.query import SearchHit
from tests.fakes import embedding_mock, vllm_models_mock


def _sample_hit() -> SearchHit:
    return SearchHit(
        chunk_id="abc",
        score=0.0333,
        cite="SA22-0000-00 z/OS Messages, Chapter 1 > IEA Messages, p. 1-5",
        heading="Chapter 1 > IEA Messages",
        text="IEA500I IOSCMDS COMMAND REJECTED",
        doc_id="SA22-0000-00",
        title="z/OS Messages",
        page_label="1-5",
        chunk_type="message",
        product="z/OS",
        version="3.2",
        message_ids=("IEA500I",),
    )


@pytest.mark.parametrize("script", [None, "//JOB EXEC PGM=EXAMPLE"])
def test_execute_answer_citation_only_uses_parsed_body_state(script):
    from scripts.query_demo import execute_answer

    hit = _sample_hit()
    content = f"Sources:\n{hit.cite}"
    if script is not None:
        content = f"```jcl\n{script}\n```\n{content}"
    with (
        patch("scripts.query_demo.execute_query", return_value=([hit], "identifier", {})),
        patch("scripts.query_demo.build_tokenizer", return_value=None),
        patch("mainframe_rag.agent.answer.HttpxLLMClient") as model_factory,
    ):
        model_factory.return_value.chat.return_value = ChatResult(
            content=content, finish_reason="stop", usage=TokenUsage(),
        )
        parsed, _hits, _kind, _timings = execute_answer(
            "IEA500I", settings=Settings(_env_file=None),
        )
    assert parsed.answer == ""
    assert parsed.citations == [hit.cite]
    assert parsed.verification_state == (
        "generation_incomplete" if script is None else "unverified_draft"
    )
    assert parsed.script == script
    assert parsed.script_review_required is (script is not None)
    model_factory.return_value.chat.assert_called_once()
    model_factory.return_value.close.assert_called_once()


@pytest.mark.parametrize("effort,query,field", [
    ("low", "IEA500I", "llm_reasoning_effort_simple"),
    ("medium", "diagnose abend S0C4", "llm_reasoning_effort_complex"),
])
def test_cli_answer_counts_the_same_effort_as_completion_near_window(effort, query, field):
    from scripts.query_demo import execute_answer

    settings = Settings(
        _env_file=None, llm_base_url="http://synthetic/v1", llm_model_reasoning="reasoning",
        llm_max_model_len=4096, llm_reserved_output_tokens=128,
        llm_thinking_reserve_tokens_simple=0, llm_thinking_reserve_tokens_complex=0,
        llm_token_safety_margin=0, **{field: effort},
    )
    original_settings = settings.model_dump()
    hit = _sample_hit()
    counted, completed = [], []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            counted.append(body)
            selected = body.get("chat_template_kwargs", {}).get("reasoning_effort")
            # Independent synthetic near-window oracle: omitted effort adds
            # 12 tokens and falsely exceeds the 4096 - 128 = 3968 budget.
            return httpx2.Response(200, json={"count": 3963 if selected == effort else 3975})
        assert request.url.path == "/v1/chat/completions"
        completed.append(body)
        return httpx2.Response(200, json={"choices": [{
            "message": {"content": "Original guidance.\n\nCitations:\n" + hit.cite},
            "finish_reason": "stop",
        }]})

    with (
        httpx2.Client(transport=httpx2.MockTransport(respond)) as client,
        patch("scripts.query_demo.execute_query", return_value=([hit], "identifier", {})),
        patch("scripts.query_demo.build_tokenizer", return_value=VllmTokenizer(
            "http://synthetic/v1", "reasoning", client=client)),
        patch("mainframe_rag.agent.answer.HttpxLLMClient", side_effect=lambda values: HttpxLLMClient(values, client=client)),
    ):
        parsed, _hits, _kind, _timings = execute_answer(query, settings=settings)
    assert parsed.answer == "Original guidance."
    assert parsed.citations == [hit.cite]
    assert len(completed) == 1 and completed[0]["reasoning_effort"] == effort
    assert counted[-1]["messages"] == completed[0]["messages"]
    assert all(body["chat_template_kwargs"] == {"reasoning_effort": effort, "enable_thinking": True}
               for body in counted)
    assert settings.model_dump() == original_settings


@patch("scripts.query_demo.retrieve_search")
@patch("scripts.query_demo.build_embedder")
@patch("mainframe_rag.agent.answer.HttpxLLMClient.chat")
@patch("qdrant_client.QdrantClient")
def test_main_cli_answer_json_carries_verification_contract(
    mock_qdrant, mock_chat, mock_embed, mock_search, tmp_path: Path
):
    """Issue #365: dev-tool JSON bundles carry the same contract labels as
    the API — a fluent-no-cite answer exports as unverified_draft, and a
    fenced script exports flagged for review."""
    from scripts.query_demo import answer_export_payload, render_answer_text

    from mainframe_rag.agent.answer import ParsedAnswer

    mock_search.return_value = ([_sample_hit()], "identifier", {"embed_ms": 2, "qdrant_ms": 8})
    mock_chat.return_value = ChatResult(
        content=(
            "Reissue the command after initialization completes."
        ),
        finish_reason="stop",
        usage=TokenUsage(),
    )
    out_file = tmp_path / "draft.json"

    rc = main(["--query", "IEA500I", "--answer", "--format", "json", "--out", str(out_file)])
    assert rc == 0
    exported = json.loads(out_file.read_text(encoding="utf-8"))
    assert exported["verification_state"] == "unverified_draft"
    assert exported["script_review_required"] is False
    assert exported["citations"] == []

    # Script-carrying draft: review flag rides text render and export alike.
    mock_chat.return_value = ChatResult(
        content=(
            "Reissue the command after initialization completes.\n\n"
            "```jcl\n//STEP1 EXEC PGM=IEFBR14\n```\n"
        ),
        finish_reason="stop",
        usage=TokenUsage(),
    )
    out_script = tmp_path / "script.json"
    rc = main(["--query", "IEA500I", "--answer", "--format", "json", "--out", str(out_script)])
    assert rc == 0
    exported_script = json.loads(out_script.read_text(encoding="utf-8"))
    assert exported_script["verification_state"] == "unverified_draft"
    assert exported_script["script_review_required"] is True
    assert exported_script["script"] == "//STEP1 EXEC PGM=IEFBR14"

    draft = ParsedAnswer(answer="Fluent prose.", citations=[], script="//X JOB")
    draft.verification_state = "unverified_draft"
    draft.script_review_required = True
    assert answer_export_payload("q", "nl", {}, draft, [])["verification_state"] == "unverified_draft"
    rendered = render_answer_text("q", "nl", draft, [], {})
    assert "Verification   : [unverified_draft]" in rendered
    assert "human review required" in rendered


@patch("scripts.query_demo.retrieve_search")
@patch("scripts.query_demo.build_embedder")
@patch("mainframe_rag.agent.answer.HttpxLLMClient.chat")
@patch("qdrant_client.QdrantClient")
def test_main_cli_answer_mode_zero_hits_renders_gracefully(
    mock_qdrant, mock_chat, mock_embed, mock_search, tmp_path: Path, capsys
):
    # Issue #181: the empty-hits path returned a dict while renderers expect
    # ParsedAnswer attributes — answer mode crashed instead of rendering.
    mock_search.return_value = ([], "nl", {"embed_ms": 2, "qdrant_ms": 8})
    out_file = tmp_path / "empty.json"

    rc = main(["--query", "ZZZ9Z9Z9Z", "--answer", "--format", "json", "--out", str(out_file)])
    assert rc == 0
    content = out_file.read_text(encoding="utf-8")
    assert "No relevant manual excerpts found" in content

    rc = main(["--query", "ZZZ9Z9Z9Z", "--answer"])
    assert rc == 0
    assert "No relevant manual excerpts found" in capsys.readouterr().out


def test_resolve_runtime_settings_fallback_to_hash(monkeypatch):
    monkeypatch.delenv("EMBED_MODE", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    with patch("httpx2.get", side_effect=OSError("Connection refused")):
        settings = resolve_runtime_settings()
        assert settings.embed_mode == "hash"
        assert settings.allow_hash_mode is True


def test_resolve_runtime_settings_probes_send_gateway_keys(monkeypatch):
    """Gateway-guarded discovery probes (/models, /embeddings) must carry
    the matching leg's virtual key so auto-detect works through LiteLLM."""
    monkeypatch.delenv("EMBED_MODE", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("EMBED_API_KEY", "sk-test-embed")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-llm")

    inner_get = vllm_models_mock(
        {"8001": ["Qwen/Qwen3-Embedding-0.6B"], "8000": ["google/gemma-4-E4B-it-qat-mobile-ct"]}
    )
    get_headers: dict = {}

    def mock_get(url, timeout=None, headers=None):
        get_headers[url] = headers
        return inner_get(url, timeout=timeout)

    inner_post = embedding_mock(1024)
    post_headers: dict = {}

    def mock_post(url, json=None, timeout=None, headers=None):
        post_headers[url] = headers
        return inner_post(url, json=json, timeout=timeout)

    with patch("httpx2.get", side_effect=mock_get), patch("httpx2.post", side_effect=mock_post):
        settings = resolve_runtime_settings()
        assert settings.embed_mode == "vllm"

    assert get_headers["http://localhost:8001/v1/models"] == {
        "Authorization": "Bearer sk-test-embed"
    }
    assert post_headers["http://localhost:8001/v1/embeddings"] == {
        "Authorization": "Bearer sk-test-embed"
    }
    assert get_headers["http://localhost:8000/v1/models"] == {
        "Authorization": "Bearer sk-test-llm"
    }


def _vllm_1024_mocks():
    return (
        vllm_models_mock({"": ["Qwen/Qwen3-Embedding-0.6B"]}),
        embedding_mock(1024),
    )


def test_resolve_runtime_settings_explicit_dense_dim_mismatch_fails_closed(monkeypatch):
    # Issue #180: an explicit --dense-dim disagreeing with the server's
    # native dim must exit nonzero, never silently search at native dim.
    monkeypatch.delenv("EMBED_MODE", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    mock_get, mock_post = _vllm_1024_mocks()
    with (
        patch("httpx2.get", side_effect=mock_get),
        patch("httpx2.post", side_effect=mock_post),
        pytest.raises(SystemExit) as exc,
    ):
        resolve_runtime_settings(embed_mode="vllm", dense_dim=512)
    assert "512" in str(exc.value.code) and "1024" in str(exc.value.code)


def test_execute_query_emits_v1_search_span():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from scripts.query_demo import execute_query

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    mock_hit = _sample_hit()
    dummy_settings = Settings(qdrant_collection="test_coll")
    with (
        patch("scripts.query_demo.tracer", provider.get_tracer("test")),
        patch("scripts.query_demo.retrieve_search", return_value=([mock_hit], "identifier", {"embed_ms": 5, "qdrant_ms": 10})),
        patch("scripts.query_demo.build_embedder"),
        patch("qdrant_client.QdrantClient"),
    ):
        execute_query("IEA500I", settings=dummy_settings)

    spans = exporter.get_finished_spans()
    search_spans = [s for s in spans if s.name == "v1.search"]
    assert len(search_spans) == 1
    root = search_spans[0]
    # Issue #529 OBS-1A: raw query text never enters span attributes.
    assert "rag.query" not in root.attributes
    assert root.attributes["rag.query_kind"] == "identifier"
    assert root.attributes["rag.hits"] == 1

