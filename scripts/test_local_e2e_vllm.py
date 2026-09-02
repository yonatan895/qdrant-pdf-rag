#!/usr/bin/env python3
"""End-to-end integration test against local vLLM servers running real models.

Exercises the full Mainframe RAG pipeline:
1. Validates connectivity to the local vLLM reasoning and embedding endpoints.
2. Ensures a local Qdrant instance is running and populated with synthetic manuals.
3. Submits operational questions to the Agent (/v1/search and /v1/answer).
4. Verifies response generation, citation grounding, and script extraction.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2

from mainframe_rag.config import Settings
from mainframe_rag.ingest.run_ingest import run as run_ingest

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

# Allow running directly via `python scripts/test_local_e2e_vllm.py`
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from scripts.make_synthetic_pdf import build
    from scripts.qdrant_sim import QdrantSim, start_simulator
except ImportError:
    from make_synthetic_pdf import build
    from qdrant_sim import QdrantSim, start_simulator


def check_vllm_connection(base_url: str, model_name: str) -> tuple[bool, str]:
    """Verify vLLM endpoint is listening and serving the expected reasoning model."""
    url = f"{base_url.rstrip('/')}/models"
    try:
        resp = httpx2.get(url, timeout=5.0)
        if resp.status_code != 200:
            print(f"[-] vLLM returned HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
            return False, model_name
        data = resp.json()
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        print(f"[+] Connected to reasoning vLLM at {base_url}. Available models: {models}")

        if model_name in models:
            return True, model_name

        # Match without org prefix or basename match (e.g. google/gemma-4... <-> gemma-4...)
        for m in models:
            if (
                m.endswith(model_name)
                or model_name.endswith(m)
                or m.split("/")[-1] == model_name.split("/")[-1]
            ):
                print(f"[+] Using matching served model ID: '{m}' (requested: '{model_name}')")
                return True, m

        # If single model available on local test server, auto-select it
        if len(models) == 1:
            print(
                f"[+] Auto-selecting the only served model: '{models[0]}' "
                f"(requested: '{model_name}')"
            )
            return True, models[0]

        print(
            f"[!] Warning: Requested model '{model_name}' not explicitly listed in {models}. "
            "Will proceed assuming the server handles alias or default routing.",
            file=sys.stderr,
        )
        return True, model_name
    except (httpx2.HTTPError, OSError) as exc:
        print(
            f"[-] Could not connect to reasoning vLLM at {base_url}: {exc}\n"
            "    Please start the local vLLM server first:\n"
            "      make local-vllm\n"
            f"      or: MODEL={model_name} PORT=8000 sh scripts/run_local_vllm.sh",
            file=sys.stderr,
        )
        return False, model_name


def check_embedding_connection(
    base_url: str, model_name: str, explicit_dim: int | None = None
) -> tuple[bool, str, int]:
    """Verify embedding endpoint is listening and probe dense dimension."""
    url = f"{base_url.rstrip('/')}/embeddings"
    try:
        resp = httpx2.post(
            url,
            json={"model": model_name, "input": ["ping"]},
            timeout=10.0,
        )
        if resp.status_code != 200:
            print(
                f"[-] Embedding endpoint returned HTTP {resp.status_code}: {resp.text}",
                file=sys.stderr,
            )
            return False, model_name, explicit_dim or 1024
        data = resp.json().get("data", [])
        if not data or "embedding" not in data[0]:
            print(
                f"[-] Invalid embedding payload from {url}: {resp.text}",
                file=sys.stderr,
            )
            return False, model_name, explicit_dim or 1024
        dim = len(data[0]["embedding"])
        if explicit_dim and explicit_dim != dim:
            print(
                f"[!] Warning: Specified DENSE_DIM={explicit_dim} does not match "
                f"model output dimension {dim}. Using probed dimension {dim}."
            )
        print(
            f"[+] Connected to embedding endpoint at {base_url}. "
            f"Model: '{model_name}', Dense Dim: {dim}"
        )
        return True, model_name, dim
    except (httpx2.HTTPError, OSError) as exc:
        print(
            f"[-] Could not connect to embedding endpoint at {base_url}: {exc}\n"
            "    Please start the local vLLM embedding server first:\n"
            "      make local-vllm-embed\n"
            f"      or: MODEL={model_name} PORT=8001 sh scripts/run_local_vllm.sh",
            file=sys.stderr,
        )
        return False, model_name, explicit_dim or 1024


def check_collection_dimension(
    settings: Settings,
    client: QdrantClient | None = None,
) -> tuple[bool, int | None, int]:
    """Check if collection exists and whether its vector dimension matches Settings.
    Returns (matches, actual_dim, expected_dim).
    actual_dim is None when collection does not exist."""
    from qdrant_client import QdrantClient

    if client is None:
        client = QdrantClient(url=settings.qdrant_url, timeout=10)
    expected_dim = settings.require_dense_dim()
    if not client.collection_exists(settings.qdrant_collection):
        return False, None, expected_dim
    info = client.get_collection(settings.qdrant_collection)
    dense_cfg = info.config.params.vectors
    if isinstance(dense_cfg, dict):
        actual = dense_cfg.get("dense")
        actual_size = actual.size if actual is not None else None
    else:
        actual_size = dense_cfg.size if dense_cfg is not None else None
    return (actual_size == expected_dim), actual_size, expected_dim


def setup_local_corpus(settings: Settings, work_dir: Path | str) -> None:
    """Generate synthetic IBM-shaped manuals and ingest them into Qdrant."""
    work_path = Path(work_dir)
    print("[*] Preparing synthetic mainframe manual corpus...")
    work_path.mkdir(parents=True, exist_ok=True)
    pdf_path = work_path / "SA22-7592-05_mvs_init.pdf"
    build(
        pdf_path,
        doc_id="SA22-7592-05",
        title="z/OS MVS Initialization and Tuning Reference",
        message_id="IEA500I",
    )

    from qdrant_client import QdrantClient

    client = QdrantClient(url=settings.qdrant_url, timeout=10)
    if client.collection_exists(settings.qdrant_collection):
        matches, actual_size, expected_dim = check_collection_dimension(settings, client=client)
        if not matches:
            print(
                f"[*] Recreating collection '{settings.qdrant_collection}' "
                f"due to dimension change ({actual_size} -> {expected_dim})..."
            )
            client.delete_collection(settings.qdrant_collection)
            progress_file = work_path / "inventory.jsonl"
            if progress_file.exists():
                progress_file.unlink()

    print(f"[*] Ingesting {pdf_path} into collection '{settings.qdrant_collection}'...")
    os.environ["QDRANT_URL"] = settings.qdrant_url
    os.environ["QDRANT_COLLECTION"] = settings.qdrant_collection
    os.environ["EMBED_MODE"] = settings.embed_mode
    if settings.embed_mode == "vllm":
        os.environ["EMBED_BASE_URL"] = settings.embed_base_url or ""
        os.environ["EMBED_MODEL"] = settings.embed_model or ""
        os.environ["DENSE_DIM"] = str(settings.dense_dim or 1024)
    else:
        os.environ["ALLOW_HASH_MODE"] = "true"

    progress_file = work_path / "inventory.jsonl"
    rc = run_ingest(
        src=work_path,
        progress=progress_file,
        workers=1,
        limit=None,
        dry_run=False,
    )
    print(f"[+] Ingest completed with code {rc}")


def run_e2e_query(
    client: Any, query: str, product: str | None = None, version: str | None = None
) -> dict[str, Any]:
    """Execute hybrid retrieval and reasoning via /v1/search and /v1/answer."""
    print("\n" + "=" * 60)
    print(f" QUERY: {query}")
    print("=" * 60)

    # 1. Test /v1/search endpoint
    print("[*] Calling POST /v1/search...")
    search_resp = client.post(
        "/v1/search",
        json={"query": query, "product": product, "version": version, "limit": 5},
    )
    if search_resp.status_code != 200:
        print(
            f"[-] /v1/search failed with HTTP {search_resp.status_code}: {search_resp.text}",
            file=sys.stderr,
        )
        return {"success": False, "error": "search_http_error"}

    search_data = search_resp.json()
    hits = search_data.get("hits", [])
    print(f"[+] /v1/search returned {len(hits)} hits (kind={search_data.get('query_kind')}):")
    for i, h in enumerate(hits, 1):
        print(f"    [{i}] score={h.get('score', 0):.4f} cite={h.get('cite')}")
        print(f"        preview: {h.get('text', '')[:100]}...")

    if not hits:
        print("[-] Retrieval returned 0 hits! Grounding impossible.", file=sys.stderr)
        return {"success": False, "error": "zero_hits"}

    # 2. Test /v1/answer endpoint
    print("\n[*] Calling POST /v1/answer (Reasoning model + citation grounding)...")
    answer_resp = client.post(
        "/v1/answer",
        json={"query": query, "product": product, "version": version},
    )
    if answer_resp.status_code != 200:
        print(
            f"[-] /v1/answer failed with HTTP {answer_resp.status_code}: {answer_resp.text}",
            file=sys.stderr,
        )
        return {"success": False, "error": "answer_http_error"}

    answer_data = answer_resp.json()
    answer_text = answer_data.get("answer", "")
    citations = answer_data.get("citations", [])
    script = answer_data.get("script")

    print("\n[+] MODEL REASONING RESPONSE:")
    print("-" * 60)
    print(answer_text)
    print("-" * 60)

    if script:
        print("\n[+] EXTRACTED SCRIPT / CODE:")
        print(script)

    print(f"\n[+] Validated Citations ({len(citations)}):")
    for c in citations:
        print(f"    * {c}")

    # 3. Grounding assertions: fail if citations are empty or answer is ungrounded
    if not citations:
        print(
            "[-] FAIL: /v1/answer returned zero validated citations! Response is ungrounded.",
            file=sys.stderr,
        )
        return {"success": False, "error": "zero_citations", "answer_data": answer_data}

    if "no supporting manual excerpts" in answer_text.lower():
        print("[-] FAIL: Model claimed no supporting excerpts were found.", file=sys.stderr)
        return {"success": False, "error": "unsupported_answer", "answer_data": answer_data}

    return {
        "success": True,
        "answer": answer_text,
        "citations": citations,
        "script": script,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Test Mainframe RAG end-to-end with local vLLM servers"
    )
    parser.add_argument(
        "--vllm-url",
        "--llm-url",
        dest="vllm_url",
        default=os.getenv("LLM_BASE_URL", os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")),
        help="vLLM reasoning base URL (default: http://localhost:8000/v1)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LLM_MODEL_REASONING", "google/gemma-4-E4B-it-qat-mobile-ct"),
        help="Reasoning model name on vLLM",
    )
    parser.add_argument(
        "--embed-url",
        default=os.getenv("EMBED_BASE_URL", "http://localhost:8001/v1"),
        help="vLLM embeddings base URL (default: http://localhost:8001/v1)",
    )
    parser.add_argument(
        "--embed-model",
        default=os.getenv("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B"),
        help="Dense embedding model name (default: Qwen/Qwen3-Embedding-0.6B)",
    )
    parser.add_argument(
        "--dense-dim",
        type=int,
        default=int(os.getenv("DENSE_DIM", "0")) or None,
        help="Dense vector dimension (auto-probed if omitted)",
    )
    parser.add_argument(
        "--embed-mode",
        choices=["vllm", "hash"],
        default=os.getenv("EMBED_MODE", "vllm"),
        help="Embedding mode (vllm or hash, default: vllm)",
    )
    parser.add_argument(
        "--qdrant-url",
        default=os.getenv("QDRANT_URL", "http://localhost:6333"),
        help="Qdrant server URL",
    )
    parser.add_argument(
        "--collection",
        default="local_vllm_test_corpus",
        help="Qdrant collection name",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Skip corpus ingest if already populated",
    )
    args = parser.parse_args(argv)

    # 1. Check reasoning LLM connectivity
    ok_llm, actual_llm = check_vllm_connection(args.vllm_url, args.model)
    if not ok_llm:
        return 1

    # 2. Check embedding model connectivity & probe dimension (if in vllm mode)
    actual_embed_model = args.embed_model
    dense_dim = args.dense_dim
    if args.embed_mode == "vllm":
        ok_embed, actual_embed_model, dense_dim = check_embedding_connection(
            args.embed_url, args.embed_model, args.dense_dim
        )
        if not ok_embed:
            return 1

    # 3. Manage Qdrant container if not reachable
    server_ctx: QdrantSim | None = None
    q_url = args.qdrant_url
    try:
        httpx2.get(f"{args.qdrant_url.rstrip('/')}/readyz", timeout=2.0)
    except (httpx2.HTTPError, OSError):
        print("[*] Local Qdrant not running on port 6333; starting ephemeral Docker container...")
        server_ctx = start_simulator(Path.cwd())
        q_url = server_ctx.url

    settings = Settings(
        qdrant_url=q_url,
        qdrant_collection=args.collection,
        embed_mode=args.embed_mode,
        allow_hash_mode=(args.embed_mode == "hash"),
        embed_base_url=args.embed_url if args.embed_mode == "vllm" else None,
        embed_model=actual_embed_model if args.embed_mode == "vllm" else None,
        dense_dim=dense_dim if args.embed_mode == "vllm" else None,
        llm_base_url=args.vllm_url,
        llm_model_reasoning=actual_llm,
    )

    try:
        if args.skip_ingest:
            matches, actual_dim, expected_dim = check_collection_dimension(settings)
            if actual_dim is None:
                print(
                    f"[-] Collection '{settings.qdrant_collection}' does not exist. "
                    f"Drop --skip-ingest to create and ingest the demo corpus.",
                    file=sys.stderr,
                )
                return 1
            if not matches:
                print(
                    f"[-] Collection '{settings.qdrant_collection}' vector dimension ({actual_dim}) "
                    f"does not match expected dimension ({expected_dim}). "
                    f"Drop --skip-ingest to recreate and re-ingest the collection.",
                    file=sys.stderr,
                )
                return 1
        else:
            work_dir = Path.cwd() / "output" / "vllm-demo-pdfs"
            setup_local_corpus(settings, work_dir)

        # Set environment variables for FastAPI app
        os.environ["QDRANT_URL"] = settings.qdrant_url
        os.environ["QDRANT_COLLECTION"] = settings.qdrant_collection
        os.environ["EMBED_MODE"] = settings.embed_mode
        if settings.embed_mode == "vllm":
            os.environ["EMBED_BASE_URL"] = settings.embed_base_url or ""
            os.environ["EMBED_MODEL"] = settings.embed_model or ""
            os.environ["DENSE_DIM"] = str(settings.dense_dim or 1024)
        else:
            os.environ["ALLOW_HASH_MODE"] = "true"
        os.environ["LLM_BASE_URL"] = settings.llm_base_url or ""
        os.environ["LLM_MODEL_REASONING"] = settings.require_reasoning_model()
        os.environ["LLM_STREAM"] = "true"

        from fastapi.testclient import TestClient

        import mainframe_rag.agent.app as app_mod

        # Run test queries via FastAPI HTTP TestClient
        test_queries = [
            (
                "How do I resolve IEA500I IOSCMDS command rejected and what operator action is needed?",
                None,
                None,
            ),
            (
                "What parameter controls the 64-bit large frame area (LFAREA) in IEASYSxx?",
                None,
                None,
            ),
        ]

        successes = 0
        with TestClient(app_mod.app) as client:
            for q, prod, ver in test_queries:
                res = run_e2e_query(client, q, product=prod, version=ver)
                if res.get("success"):
                    successes += 1

        print("\n" + "=" * 60)
        print(
            f" E2E vLLM Test Complete: {successes}/{len(test_queries)} "
            "queries passed grounding validation"
        )
        print("=" * 60)
        return 0 if successes == len(test_queries) else 1

    finally:
        if server_ctx is not None:
            server_ctx.stop()


if __name__ == "__main__":
    sys.exit(main())
