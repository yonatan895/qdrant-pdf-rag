"""Unit tests for scripts/query_demo.py (pure functions, no network/docker)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from scripts.query_demo import (
    _format_text_hit,
    main,
    render_query_html,
    render_query_text,
    resolve_runtime_settings,
)

from mainframe_rag.config import Settings
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


def test_format_text_hit():
    hit = _sample_hit()
    formatted = _format_text_hit(1, hit)
    assert "#1 [Score: 0.0333]" in formatted
    assert "SA22-0000-00" in formatted
    assert "IEA500I" in formatted


def test_render_query_text():
    hits = [_sample_hit()]
    rendered = render_query_text("IEA500I", "identifier", hits, {"embed_ms": 5, "qdrant_ms": 10})
    assert "QUERY: IEA500I" in rendered
    assert "[IDENTIFIER]" in rendered
    assert "Embed: 5ms | Qdrant: 10ms" in rendered
    assert "Hits Found     : 1" in rendered


def test_render_query_html():
    hits = [_sample_hit()]
    html_out = render_query_html("IEA500I", "identifier", hits, {"embed_ms": 5, "qdrant_ms": 10})
    assert "<!DOCTYPE html>" in html_out
    assert "Query Inspection Demo" in html_out
    assert "IEA500I" in html_out
    assert "Score: 0.0333" in html_out


@patch("scripts.query_demo.retrieve_search")
@patch("scripts.query_demo.build_embedder")
@patch("qdrant_client.QdrantClient")
def test_main_cli_single_query(mock_qdrant, mock_embed, mock_search, tmp_path: Path):
    mock_search.return_value = ([_sample_hit()], "identifier", {"embed_ms": 2, "qdrant_ms": 8})
    out_file = tmp_path / "query.json"

    rc = main(["--query", "IEA500I", "--format", "json", "--out", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    assert '"query": "IEA500I"' in out_file.read_text(encoding="utf-8")


def test_positive_int_limit_validation():
    import pytest

    with pytest.raises(SystemExit):
        main(["--query", "IEA500I", "--limit", "0"])

    with pytest.raises(SystemExit):
        main(["--query", "IEA500I", "--limit", "-3"])


def test_render_answer_text():
    from scripts.query_demo import render_answer_text

    from mainframe_rag.agent.answer import ParsedAnswer

    hits = [_sample_hit()]
    parsed = ParsedAnswer(
        answer="This message indicates command rejection.",
        citations=["SA22-0000-00 z/OS Messages, Chapter 1 > IEA Messages, p. 1-5"],
        script="//RETRY EXEC PGM=IEFBR14",
        citations_inferred=False,
    )
    rendered = render_answer_text("IEA500I", "identifier", parsed, hits, {"embed_ms": 2, "qdrant_ms": 8})
    assert "QUESTION: IEA500I" in rendered
    assert "MODEL REASONING ANSWER:" in rendered
    assert "This message indicates command rejection." in rendered
    assert "EXTRACTED SCRIPT / CODE:" in rendered
    assert "//RETRY EXEC PGM=IEFBR14" in rendered
    assert "VALIDATED CITATIONS (1) [explicit Citations: section]:" in rendered
    assert "SA22-0000-00" in rendered

    # Inferred citation variant
    parsed_inferred = ParsedAnswer(
        answer="This message indicates command rejection [1].",
        citations=["SA22-0000-00 z/OS Messages, Chapter 1 > IEA Messages, p. 1-5"],
        script=None,
        citations_inferred=True,
        inferred_indices=[1],
    )
    rendered_inferred = render_answer_text("IEA500I", "identifier", parsed_inferred, hits, {"embed_ms": 2, "qdrant_ms": 8})
    assert "VALIDATED CITATIONS (1) [inferred from excerpt [1]]:" in rendered_inferred


def test_render_answer_html():
    from scripts.query_demo import render_answer_html

    from mainframe_rag.agent.answer import ParsedAnswer

    hits = [_sample_hit()]
    parsed = ParsedAnswer(
        answer="This message indicates command rejection.",
        citations=["SA22-0000-00 z/OS Messages, Chapter 1 > IEA Messages, p. 1-5"],
        script="//RETRY EXEC PGM=IEFBR14",
    )
    html_out = render_answer_html("IEA500I", "identifier", parsed, hits, {"embed_ms": 2, "qdrant_ms": 8})
    assert "<!DOCTYPE html>" in html_out
    assert "Mainframe RAG Answer" in html_out
    assert "Extracted Script / Code" in html_out
    assert "Validated Citations" in html_out


@patch("scripts.query_demo.retrieve_search")
@patch("scripts.query_demo.build_embedder")
@patch("mainframe_rag.agent.answer.HttpxLLMClient.chat")
@patch("qdrant_client.QdrantClient")
def test_main_cli_answer_mode(mock_qdrant, mock_chat, mock_embed, mock_search, tmp_path: Path):
    mock_search.return_value = ([_sample_hit()], "identifier", {"embed_ms": 2, "qdrant_ms": 8})
    mock_chat.return_value = (
        "Command rejected.\n\n"
        "Citations:\n"
        "SA22-0000-00 z/OS Messages, Chapter 1 > IEA Messages, p. 1-5"
    )
    out_file = tmp_path / "answer.json"

    rc = main(["--query", "IEA500I", "--answer", "--format", "json", "--out", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert '"query": "IEA500I"' in content
    assert '"answer": "Command rejected."' in content
    assert '"citations_inferred": false' in content


@patch("scripts.query_demo.retrieve_search")
@patch("scripts.query_demo.build_embedder")
@patch("qdrant_client.QdrantClient")
def test_main_cli_answer_mode_zero_hits_renders_gracefully(
    mock_qdrant, mock_embed, mock_search, tmp_path: Path, capsys
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


def test_resolve_runtime_settings_auto_detect_vllm_and_probing(monkeypatch):
    monkeypatch.delenv("EMBED_MODE", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    mock_get = vllm_models_mock(
        {"8001": ["Qwen/Qwen3-Embedding-0.6B"], "8000": ["google/gemma-4-E4B-it-qat-mobile-ct"]}
    )
    mock_post = embedding_mock(1024)

    with patch("httpx2.get", side_effect=mock_get), patch("httpx2.post", side_effect=mock_post):
        settings = resolve_runtime_settings()
        assert settings.embed_mode == "vllm"
        assert settings.embed_base_url == "http://localhost:8001/v1"
        assert settings.embed_model == "Qwen/Qwen3-Embedding-0.6B"
        assert settings.dense_dim == 1024
        assert settings.llm_base_url == "http://localhost:8000/v1"
        assert settings.llm_model_reasoning == "google/gemma-4-E4B-it-qat-mobile-ct"


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


def test_resolve_runtime_settings_explicit_cli_overrides_with_multi_model_discovery():
    mock_get = vllm_models_mock(
        {"9000": ["served-embed-1", "served-embed-2"], "9001": ["served-reasoner-1", "served-reasoner-2"]}
    )

    with patch("httpx2.get", side_effect=mock_get):
        settings = resolve_runtime_settings(
            collection="custom_collection",
            embed_url="http://embed-host:9000/v1",
            embed_model="custom-embed",
            embed_mode="vllm",
            dense_dim=768,
            vllm_url="http://llm-host:9001/v1",
            model="custom-reasoner",
        )
        assert settings.qdrant_collection == "custom_collection"
        assert settings.embed_mode == "vllm"
        assert settings.embed_base_url == "http://embed-host:9000/v1"
        assert settings.embed_model == "custom-embed"
        assert settings.dense_dim == 768
        assert settings.llm_base_url == "http://llm-host:9001/v1"
        assert settings.llm_model_reasoning == "custom-reasoner"


def test_resolve_runtime_settings_explicit_model_matching_served_basename():
    mock_get = vllm_models_mock(
        {
            "8001": ["Qwen/Qwen3-Embedding-0.6B", "unrelated/model"],
            "8000": ["google/gemma-4-E4B-it-qat-mobile-ct", "unrelated/model"],
        }
    )

    with patch("httpx2.get", side_effect=mock_get):
        settings = resolve_runtime_settings(
            embed_model="Qwen3-Embedding-0.6B",
            model="gemma-4-E4B-it-qat-mobile-ct",
        )
        assert settings.embed_model == "Qwen/Qwen3-Embedding-0.6B"
        assert settings.llm_model_reasoning == "google/gemma-4-E4B-it-qat-mobile-ct"


def test_resolve_runtime_settings_malformed_json_fallback():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("Invalid JSON response from proxy")

    with patch("httpx2.get", return_value=mock_resp):
        settings = resolve_runtime_settings()
        assert settings.embed_mode == "hash"
        assert settings.allow_hash_mode is True


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


def test_resolve_runtime_settings_explicit_dense_dim_match_applies(monkeypatch):
    monkeypatch.delenv("EMBED_MODE", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    mock_get, mock_post = _vllm_1024_mocks()
    with patch("httpx2.get", side_effect=mock_get), patch("httpx2.post", side_effect=mock_post):
        settings = resolve_runtime_settings(embed_mode="vllm", dense_dim=1024)
        assert settings.dense_dim == 1024


def test_resolve_runtime_settings_otel_endpoint_explicit():
    with patch("httpx2.get", side_effect=OSError("Connection refused")):
        settings = resolve_runtime_settings(otel_endpoint="http://custom-jaeger:4318")
        assert settings.otel_exporter_otlp_endpoint == "http://custom-jaeger:4318"


def test_resolve_runtime_settings_otel_endpoint_off():
    with patch("httpx2.get", side_effect=OSError("Connection refused")):
        for disable_val in ("off", "none", "false", "0", ""):
            settings = resolve_runtime_settings(otel_endpoint=disable_val)
            assert settings.otel_exporter_otlp_endpoint is None


def test_resolve_runtime_settings_otel_endpoint_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env-collector:4318")
    with patch("httpx2.get", side_effect=OSError("Connection refused")):
        settings = resolve_runtime_settings()
        assert settings.otel_exporter_otlp_endpoint == "http://env-collector:4318"


def test_resolve_runtime_settings_otel_autodetect_local_collector(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    def mock_get(url, timeout=None):
        if "4318" in url:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            return mock_resp
        raise OSError("Connection refused")

    with patch("httpx2.get", side_effect=mock_get):
        settings = resolve_runtime_settings()
        assert settings.otel_exporter_otlp_endpoint == "http://127.0.0.1:4318"


def test_resolve_runtime_settings_otel_probe_failure_stays_none(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    def mock_get(url, timeout=None):
        raise OSError("Connection refused")

    with patch("httpx2.get", side_effect=mock_get):
        settings = resolve_runtime_settings()
        assert settings.otel_exporter_otlp_endpoint is None


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
    assert root.attributes["rag.query"] == "IEA500I"
    assert root.attributes["rag.query_kind"] == "identifier"
    assert root.attributes["rag.hits"] == 1


def test_execute_answer_emits_v1_answer_and_child_spans():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from scripts.query_demo import execute_answer

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    mock_hit = _sample_hit()
    mock_chat_res = MagicMock()
    mock_chat_res.content = "Answer text."
    mock_chat_res.ttft_ms = 150
    mock_chat_res.finish_reason = "stop"
    mock_chat_res.usage.prompt_tokens = 100
    mock_chat_res.usage.completion_tokens = 50
    mock_chat_res.usage.reasoning_tokens = 20
    mock_chat_res.usage.total_tokens = 150

    dummy_settings = Settings(qdrant_collection="test_coll")
    with (
        patch("scripts.query_demo.tracer", provider.get_tracer("test")),
        patch("scripts.query_demo.retrieve_search", return_value=([mock_hit], "identifier", {"embed_ms": 5, "qdrant_ms": 10})),
        patch("scripts.query_demo.build_embedder"),
        patch("qdrant_client.QdrantClient"),
        patch("mainframe_rag.agent.answer.HttpxLLMClient"),
        patch("mainframe_rag.agent.answer.as_chat_result", return_value=mock_chat_res),
        patch("mainframe_rag.agent.answer.parse_answer") as mock_parse,
    ):
        mock_parsed = MagicMock()
        mock_parsed.answer = "Answer text."
        mock_parsed.citations = [mock_hit.cite]
        mock_parsed.script = None
        mock_parse.return_value = mock_parsed

        execute_answer("What does message IEA500I mean?", settings=dummy_settings)

    spans = exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "v1.answer" in span_names
    assert "prompt.build" in span_names
    assert "llm.chat" in span_names

    root = next(s for s in spans if s.name == "v1.answer")
    assert root.attributes["rag.query"] == "What does message IEA500I mean?"
    assert root.attributes["rag.hits"] == 1
    assert root.attributes["rag.citations"] == 1



