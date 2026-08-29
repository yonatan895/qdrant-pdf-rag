from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx2
from scripts.test_local_e2e_vllm import check_vllm_connection, run_e2e_query


def test_vllm_connection_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [{"id": "gemma-4-E4B-it-qat-mobile-ct"}]
    }
    with patch("httpx2.get", return_value=mock_resp):
        ok, model = check_vllm_connection("http://localhost:8000/v1", "google/gemma-4-E4B-it-qat-mobile-ct")
        assert ok is True
        assert model == "gemma-4-E4B-it-qat-mobile-ct"


def test_vllm_connection_failure():
    with patch("httpx2.get", side_effect=httpx2.ConnectError("Connection refused")):
        ok, _ = check_vllm_connection("http://localhost:8000/v1", "google/gemma-4-E4B-it-qat-mobile-ct")
        assert ok is False



def test_run_e2e_query_flow():
    mock_client = MagicMock()
    
    # Mock /v1/search response
    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.json.return_value = {
        "query_kind": "identifier",
        "hits": [{
            "score": 0.95,
            "cite": "SA22-7592-05 z/OS MVS Init, Section 1, p. 1-1",
            "text": "IEA500I IOSCMDS command rejected.",
        }],
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

