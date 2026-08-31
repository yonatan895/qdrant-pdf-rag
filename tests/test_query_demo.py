"""Unit tests for scripts/query_demo.py (pure functions, no network/docker)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.query_demo import (
    _format_text_hit,
    main,
    render_query_html,
    render_query_text,
    resolve_runtime_settings,
)

from mainframe_rag.retrieve.query import SearchHit


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

    hits = [_sample_hit()]
    parsed = {
        "answer": "This message indicates command rejection.",
        "citations": ["SA22-0000-00 z/OS Messages, Chapter 1 > IEA Messages, p. 1-5"],
        "script": "//RETRY EXEC PGM=IEFBR14",
        "citations_inferred": False,
    }
    rendered = render_answer_text("IEA500I", "identifier", parsed, hits, {"embed_ms": 2, "qdrant_ms": 8})
    assert "QUESTION: IEA500I" in rendered
    assert "MODEL REASONING ANSWER:" in rendered
    assert "This message indicates command rejection." in rendered
    assert "EXTRACTED SCRIPT / CODE:" in rendered
    assert "//RETRY EXEC PGM=IEFBR14" in rendered
    assert "VALIDATED CITATIONS (1) [explicit Citations: section]:" in rendered
    assert "SA22-0000-00" in rendered

    # Inferred citation variant
    parsed_inferred = {
        "answer": "This message indicates command rejection [1].",
        "citations": ["SA22-0000-00 z/OS Messages, Chapter 1 > IEA Messages, p. 1-5"],
        "script": None,
        "citations_inferred": True,
        "inferred_indices": [1],
    }
    rendered_inferred = render_answer_text("IEA500I", "identifier", parsed_inferred, hits, {"embed_ms": 2, "qdrant_ms": 8})
    assert "VALIDATED CITATIONS (1) [inferred from excerpt [1]]:" in rendered_inferred


def test_render_answer_html():
    from scripts.query_demo import render_answer_html

    hits = [_sample_hit()]
    parsed = {
        "answer": "This message indicates command rejection.",
        "citations": ["SA22-0000-00 z/OS Messages, Chapter 1 > IEA Messages, p. 1-5"],
        "script": "//RETRY EXEC PGM=IEFBR14",
    }
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

    def mock_get(url, timeout=None):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        if "8001" in url:
            mock_resp.json.return_value = {"data": [{"id": "Qwen/Qwen3-Embedding-0.6B"}]}
        elif "8000" in url:
            mock_resp.json.return_value = {"data": [{"id": "google/gemma-4-E4B-it-qat-mobile-ct"}]}
        return mock_resp

    def mock_post(url, json=None, timeout=None):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"embedding": [0.1] * 1024}]}
        return mock_resp

    with patch("httpx2.get", side_effect=mock_get), patch("httpx2.post", side_effect=mock_post):
        settings = resolve_runtime_settings()
        assert settings.embed_mode == "vllm"
        assert settings.embed_base_url == "http://localhost:8001/v1"
        assert settings.embed_model == "Qwen/Qwen3-Embedding-0.6B"
        assert settings.dense_dim == 1024
        assert settings.llm_base_url == "http://localhost:8000/v1"
        assert settings.llm_model_reasoning == "google/gemma-4-E4B-it-qat-mobile-ct"


def test_resolve_runtime_settings_explicit_cli_overrides_with_multi_model_discovery():
    def mock_get(url, timeout=None):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        if "9000" in url:
            mock_resp.json.return_value = {"data": [{"id": "served-embed-1"}, {"id": "served-embed-2"}]}
        elif "9001" in url:
            mock_resp.json.return_value = {"data": [{"id": "served-reasoner-1"}, {"id": "served-reasoner-2"}]}
        return mock_resp

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
    def mock_get(url, timeout=None):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        if "8001" in url:
            mock_resp.json.return_value = {"data": [{"id": "Qwen/Qwen3-Embedding-0.6B"}, {"id": "unrelated/model"}]}
        elif "8000" in url:
            mock_resp.json.return_value = {"data": [{"id": "google/gemma-4-E4B-it-qat-mobile-ct"}, {"id": "unrelated/model"}]}
        return mock_resp

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



