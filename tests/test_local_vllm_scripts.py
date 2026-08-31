import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx2
import pytest
from scripts.test_local_e2e_vllm import (
    check_embedding_connection,
    check_vllm_connection,
    run_e2e_query,
    setup_local_corpus,
)

from mainframe_rag.config import Settings

_ENV_SANDBOX_VARS = (
    "QDRANT_URL",
    "QDRANT_COLLECTION",
    "EMBED_MODE",
    "EMBED_BASE_URL",
    "EMBED_MODEL",
    "DENSE_DIM",
    "ALLOW_HASH_MODE",
    "LLM_BASE_URL",
    "LLM_MODEL_REASONING",
)


@pytest.fixture(autouse=True)
def _sandbox_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure no environment variables modified during tests leak into other test files."""
    for key in _ENV_SANDBOX_VARS:
        if key in os.environ:
            monkeypatch.setenv(key, os.environ[key])
        else:
            monkeypatch.setenv(key, "")
            del os.environ[key]


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


def test_setup_local_corpus_vllm(tmp_path: Path, monkeypatch):
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
        patch("qdrant_client.QdrantClient") as mock_qdrant_cls,
    ):
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = False
        mock_qdrant_cls.return_value = mock_client
        setup_local_corpus(settings, tmp_path)
        assert mock_build.called
        assert mock_ingest.called


def test_setup_local_corpus_dimension_mismatch(tmp_path: Path, monkeypatch):
    settings = Settings(
        qdrant_url="http://localhost:6333",
        qdrant_collection="test_col",
        embed_mode="vllm",
        embed_base_url="http://localhost:8001/v1",
        embed_model="Qwen/Qwen3-Embedding-0.6B",
        dense_dim=1024,
    )
    inventory_file = tmp_path / "inventory.jsonl"
    inventory_file.write_text("dummy record\n")

    with (
        patch("scripts.test_local_e2e_vllm.build") as mock_build,
        patch("scripts.test_local_e2e_vllm.run_ingest", return_value=0) as mock_ingest,
        patch("qdrant_client.QdrantClient") as mock_qdrant_cls,
    ):
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        mock_info = MagicMock()
        mock_dense_vector = MagicMock()
        mock_dense_vector.size = 256
        mock_info.config.params.vectors = {"dense": mock_dense_vector}
        mock_client.get_collection.return_value = mock_info
        mock_qdrant_cls.return_value = mock_client

        setup_local_corpus(settings, tmp_path)
        assert mock_client.delete_collection.called
        assert mock_client.delete_collection.call_args[0][0] == "test_col"
        assert not inventory_file.exists()
        assert mock_build.called
        assert mock_ingest.called


def test_check_collection_dimension():
    from scripts.test_local_e2e_vllm import check_collection_dimension

    settings = Settings(
        qdrant_url="http://localhost:6333",
        qdrant_collection="test_col",
        embed_mode="vllm",
        embed_base_url="http://localhost:8001/v1",
        embed_model="Qwen/Qwen3-Embedding-0.6B",
        dense_dim=1024,
    )
    with patch("qdrant_client.QdrantClient") as mock_qdrant_cls:
        mock_client = MagicMock()
        mock_qdrant_cls.return_value = mock_client

        # 1. Non-existent collection
        mock_client.collection_exists.return_value = False
        matches, actual, expected = check_collection_dimension(settings, client=mock_client)
        assert matches is False
        assert actual is None
        assert expected == 1024

        # 2. Existing matching collection
        mock_client.collection_exists.return_value = True
        mock_info = MagicMock()
        mock_dense = MagicMock()
        mock_dense.size = 1024
        mock_info.config.params.vectors = {"dense": mock_dense}
        mock_client.get_collection.return_value = mock_info
        matches, actual, expected = check_collection_dimension(settings, client=mock_client)
        assert matches is True
        assert actual == 1024
        assert expected == 1024

        # 3. Existing mismatched collection (single vector config style)
        mock_single_vec = MagicMock()
        mock_single_vec.size = 256
        mock_info.config.params.vectors = mock_single_vec
        matches, actual, expected = check_collection_dimension(settings, client=mock_client)
        assert matches is False
        assert actual == 256
        assert expected == 1024


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


def _run_vllm_script(tmp_path: Path, env_overrides: dict[str, str] | None = None) -> tuple[int, list[str]]:
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_file = tmp_path / "docker_args.log"
    stub_docker = bin_dir / "docker"
    stub_docker.write_text(f"""#!/bin/sh
printf '%s\\n' "$@" > "{log_file}"
exit 0
""")
    stub_docker.chmod(0o755)

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "run_local_vllm.sh"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    if env_overrides:
        env.update(env_overrides)

    res = subprocess.run(
        ["sh", str(script_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    args = log_file.read_text().splitlines() if log_file.exists() else []
    return res.returncode, args


def test_run_local_vllm_sh_embedding_model_no_task_arg(tmp_path: Path):
    rc, args = _run_vllm_script(
        tmp_path,
        {
            "MODEL": "Qwen/Qwen3-Embedding-0.6B",
            "PORT": "8001",
            "GPU_MEM": "0.30",
            "MAX_LEN": "4096",
        },
    )
    assert rc == 0
    # In vLLM v0.28.0+, --task embedding must NOT be passed (unrecognized argument error)
    assert "--task" not in args
    assert "embedding" not in args
    assert "run" in args
    assert "-p" in args
    assert "8001:8001" in args
    assert "Qwen/Qwen3-Embedding-0.6B" in args
    assert "--gpu-memory-utilization" in args
    assert "0.30" in args


def test_run_local_vllm_sh_reasoning_gemma4_model(tmp_path: Path):
    rc, args = _run_vllm_script(
        tmp_path,
        {
            "MODEL": "google/gemma-4-E4B-it-qat-mobile-ct",
            "PORT": "8000",
            "GPU_MEM": "0.65",
            "MAX_LEN": "4096",
        },
    )
    assert rc == 0
    assert "--tool-call-parser" in args
    assert "gemma4" in args
    assert "--reasoning-parser" in args
    assert "--chat-template" in args
    assert "-p" in args
    assert "8000:8000" in args
    assert "0.65" in args


def test_run_local_vllm_sh_local_directory_mount(tmp_path: Path):
    model_dir = tmp_path / "my-custom-model"
    model_dir.mkdir()
    rc, args = _run_vllm_script(
        tmp_path,
        {
            "MODEL": str(model_dir),
            "PORT": "8000",
        },
    )
    assert rc == 0
    assert any(a.startswith(f"{model_dir}:/model:ro") for a in args)
    assert "/model" in args
    assert "--served-model-name" in args
    assert "my-custom-model" in args


def test_run_local_vllm_sh_hf_token_forwarding(tmp_path: Path):
    rc, args = _run_vllm_script(
        tmp_path,
        {
            "MODEL": "google/gemma-4-E4B-it-qat-mobile-ct",
            "HF_TOKEN": "hf_secret_12345",
        },
    )
    assert rc == 0
    # HF_TOKEN must be passed as -e HF_TOKEN without the secret value in argv
    assert "HF_TOKEN" in args
    assert "hf_secret_12345" not in args

