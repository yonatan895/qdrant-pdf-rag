from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx2
from scripts.test_local_e2e_vllm import (
    check_embedding_connection,
    check_vllm_connection,
    run_e2e_query,
    setup_local_corpus,
)

from mainframe_rag.config import Settings


def test_vllm_connection_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [{"id": "gemma-4-E4B-it-qat-mobile-ct"}]
    }
    with patch("httpx2.get", return_value=mock_resp):
        ok, model = check_vllm_connection(
            "http://localhost:8000/v1", "google/gemma-4-E4B-it-qat-mobile-ct"
        )
        assert ok is True
        assert model == "gemma-4-E4B-it-qat-mobile-ct"


def test_vllm_connection_failure():
    with patch("httpx2.get", side_effect=httpx2.ConnectError("Connection refused")):
        ok, _ = check_vllm_connection(
            "http://localhost:8000/v1", "google/gemma-4-E4B-it-qat-mobile-ct"
        )
        assert ok is False


def test_check_embedding_connection_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [{"embedding": [0.1] * 1024}]
    }
    with patch("httpx2.post", return_value=mock_resp):
        ok, model, dim = check_embedding_connection(
            "http://localhost:8001/v1", "Qwen/Qwen3-Embedding-0.6B"
        )
        assert ok is True
        assert model == "Qwen/Qwen3-Embedding-0.6B"
        assert dim == 1024


def test_check_embedding_connection_failure():
    with patch("httpx2.post", side_effect=httpx2.ConnectError("Connection refused")):
        ok, model, dim = check_embedding_connection(
            "http://localhost:8001/v1", "Qwen/Qwen3-Embedding-0.6B"
        )
        assert ok is False
        assert model == "Qwen/Qwen3-Embedding-0.6B"
        assert dim == 1024


def test_check_embedding_connection_invalid_response():
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Model not found"
    with patch("httpx2.post", return_value=mock_resp):
        ok, model, dim = check_embedding_connection(
            "http://localhost:8001/v1", "invalid-model"
        )
        assert ok is False
        assert model == "invalid-model"
        assert dim == 1024


def test_setup_local_corpus_vllm(tmp_path: Path):
    settings = Settings(
        qdrant_url="http://localhost:6333",
        qdrant_collection="test_col",
        embed_mode="vllm",
        embed_base_url="http://localhost:8001/v1",
        embed_model="Qwen/Qwen3-Embedding-0.6B",
        dense_dim=1024,
    )
    with (
        patch("scripts.test_local_e2e_vllm.build") as mock_build,
        patch("scripts.test_local_e2e_vllm.run_ingest", return_value=0) as mock_ingest,
    ):
        setup_local_corpus(settings, tmp_path)
        assert mock_build.called
        assert mock_ingest.called


def test_run_e2e_query_flow():
    mock_client = MagicMock()

    # Mock /v1/search response
    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.json.return_value = {
        "query_kind": "identifier",
        "hits": [
            {
                "score": 0.95,
                "cite": "SA22-7592-05 z/OS MVS Init, Section 1, p. 1-1",
                "text": "IEA500I IOSCMDS command rejected.",
            }
        ],
    }

    # Mock /v1/answer response with grounded citations
    mock_answer_resp = MagicMock()
    mock_answer_resp.status_code = 200
    mock_answer_resp.json.return_value = {
        "answer": "To resolve IEA500I, check device allocation.",
        "citations": ["SA22-7592-05 z/OS MVS Init, Section 1, p. 1-1"],
        "script": "//RETRY EXEC PGM=IEFBR14",
    }

    mock_client.post.side_effect = [mock_search_resp, mock_answer_resp]

    result = run_e2e_query(mock_client, "How to resolve IEA500I?")
    assert result["success"] is True
    assert len(result["citations"]) == 1
    assert "SA22-7592-05" in result["citations"][0]


def test_run_e2e_query_ungrounded_fails():
    mock_client = MagicMock()

    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.json.return_value = {
        "query_kind": "identifier",
        "hits": [{"score": 0.95, "cite": "SA22-7592-05 cite", "text": "sample"}],
    }

    # Answer without citations must fail grounding assertion
    mock_answer_resp = MagicMock()
    mock_answer_resp.status_code = 200
    mock_answer_resp.json.return_value = {
        "answer": "Ungrounded answer without citations.",
        "citations": [],
        "script": None,
    }

    mock_client.post.side_effect = [mock_search_resp, mock_answer_resp]

    result = run_e2e_query(mock_client, "How to resolve IEA500I?")
    assert result["success"] is False
    assert result["error"] == "zero_citations"

