from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx2
from scripts.test_local_e2e_vllm import check_vllm_connection, run_e2e_query

from mainframe_rag.config import Settings
from mainframe_rag.retrieve.query import SearchHit


def test_vllm_connection_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [{"id": "google/gemma-4-E4B-it-qat-mobile-ct"}]
    }
    with patch("httpx2.get", return_value=mock_resp):
        assert check_vllm_connection("http://localhost:8000/v1", "google/gemma-4-E4B-it-qat-mobile-ct") is True


def test_vllm_connection_failure():
    with patch("httpx2.get", side_effect=httpx2.ConnectError("Connection refused")):
        assert check_vllm_connection("http://localhost:8000/v1", "google/gemma-4-E4B-it-qat-mobile-ct") is False


def test_run_e2e_query_flow():
    settings = Settings(
        qdrant_url="http://localhost:6333",
        qdrant_collection="test_corpus",
        embed_mode="hash",
        allow_hash_mode=True,
        llm_base_url="http://localhost:8000/v1",
        llm_model_reasoning="google/gemma-4-E4B-it-qat-mobile-ct",
    )

    mock_hit = SearchHit(
        chunk_id="abc123",
        score=0.95,
        cite="SA22-7592-05 z/OS MVS Init, Section 1, p. 1-1",
        heading="Section 1",
        text="IEA500I IOSCMDS command rejected. Check device status.",
        doc_id="SA22-7592-05",
        title="z/OS MVS Init",
        page_label="1-1",
        chunk_type="message",
        product="z/OS",
        version="3.1",
        message_ids=("IEA500I",),
    )

    model_reply = (
        "To resolve IEA500I, check device allocation.\n\n"
        "```jcl\n//RETRY EXEC PGM=IEFBR14\n```\n\n"
        "Citations:\n"
        "SA22-7592-05 z/OS MVS Init, Section 1, p. 1-1"
    )

    with patch("scripts.test_local_e2e_vllm.search", return_value=[mock_hit]), \
         patch("mainframe_rag.agent.answer.HttpxLLMClient.chat", return_value=model_reply):
        result = run_e2e_query(settings, "How to resolve IEA500I?")
        assert result["success"] is True
        assert len(result["citations"]) == 1
        assert "SA22-7592-05" in result["citations"][0]
