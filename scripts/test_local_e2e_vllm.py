#!/usr/bin/env python3
"""End-to-end integration test against a local vLLM server running a real model.

Exercises the full Mainframe RAG pipeline:
1. Validates connectivity to the local vLLM OpenAI-compatible endpoint.
2. Ensures a local Qdrant instance is running and populated with synthetic manuals.
3. Submits operational questions to the Agent (/v1/answer and /v1/search).
4. Verifies response generation, citation grounding, and script extraction.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import httpx2

from mainframe_rag.config import Settings
from mainframe_rag.ingest.run_ingest import run as run_ingest

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


def check_vllm_connection(base_url: str, model_name: str) -> bool:
    """Verify vLLM endpoint is listening and serving the expected model."""
    url = f"{base_url.rstrip('/')}/models"
    try:
        resp = httpx2.get(url, timeout=5.0)
        if resp.status_code != 200:
            print(f"[-] vLLM returned HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
            return False
        data = resp.json()
        models = [m.get("id") for m in data.get("data", [])]
        print(f"[+] Connected to vLLM at {base_url}. Available models: {models}")
        if model_name not in models and not any(model_name in m for m in models):
            print(
                f"[!] Warning: Requested model '{model_name}' not explicitly listed in {models}. "
                "Will proceed assuming the server handles alias or default routing.",
                file=sys.stderr,
            )
        return True
    except (httpx2.HTTPError, OSError) as exc:
        print(
            f"[-] Could not connect to vLLM at {base_url}: {exc}\n"
            "    Please start the local vLLM server first:\n"
            f"      make local-vllm\n"
            f"      or: MODEL={model_name} sh scripts/run_local_vllm.sh",
            file=sys.stderr,
        )
        return False


def setup_local_corpus(settings: Settings, work_dir: str) -> None:
    """Generate synthetic IBM-shaped manuals and ingest them into Qdrant."""
    print("[*] Preparing synthetic mainframe manual corpus...")
    os.makedirs(work_dir, exist_ok=True)
    pdf_path = Path(work_dir) / "SA22-7592-05_mvs_init.pdf"
    build(pdf_path, doc_id="SA22-7592-05", title="z/OS MVS Initialization and Tuning Reference")

    print(f"[*] Ingesting {pdf_path} into collection '{settings.qdrant_collection}'...")
    os.environ["QDRANT_URL"] = settings.qdrant_url
    os.environ["QDRANT_COLLECTION"] = settings.qdrant_collection
    os.environ["EMBED_MODE"] = settings.embed_mode
    os.environ["ALLOW_HASH_MODE"] = "true"

    progress_file = Path(work_dir) / "inventory.jsonl"
    rc = run_ingest(
        src=Path(work_dir),
        progress=progress_file,
        workers=1,
        limit=None,
        dry_run=False,
    )
    print(f"[+] Ingest completed with code {rc}")


def run_e2e_query(client: Any, query: str, product: str | None = None, version: str | None = None) -> dict[str, Any]:
    """Execute hybrid retrieval and reasoning via the real /v1/search and /v1/answer HTTP endpoints."""
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
        print(f"[-] /v1/search failed with HTTP {search_resp.status_code}: {search_resp.text}", file=sys.stderr)
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
        print(f"[-] /v1/answer failed with HTTP {answer_resp.status_code}: {answer_resp.text}", file=sys.stderr)
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
        print("[-] FAIL: /v1/answer returned zero validated citations! Response is ungrounded.", file=sys.stderr)
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
    parser = argparse.ArgumentParser(description="Test Mainframe RAG end-to-end with local vLLM")
    parser.add_argument("--vllm-url", default=os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1"), help="vLLM base URL")
    parser.add_argument(
        "--model",
        default=os.getenv("LLM_MODEL_REASONING", "google/gemma-4-E4B-it-qat-mobile-ct"),
        help="Reasoning model name on vLLM",
    )
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://localhost:6333"), help="Qdrant server URL")
    parser.add_argument("--collection", default="local_vllm_test_corpus", help="Qdrant collection name")
    parser.add_argument("--skip-ingest", action="store_true", help="Skip corpus ingest if already populated")
    args = parser.parse_args(argv)

    # Check vLLM connectivity
    if not check_vllm_connection(args.vllm_url, args.model):
        return 1

    # Manage Qdrant container if not reachable
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
        embed_mode="hash",
        allow_hash_mode=True,
        llm_base_url=args.vllm_url,
        llm_model_reasoning=args.model,
    )

    try:
        if not args.skip_ingest:
            work_dir = os.path.join(os.getcwd(), "output", "vllm-demo-pdfs")
            setup_local_corpus(settings, work_dir)

        # Set environment variables for FastAPI app
        os.environ["QDRANT_URL"] = settings.qdrant_url
        os.environ["QDRANT_COLLECTION"] = settings.qdrant_collection
        os.environ["EMBED_MODE"] = "hash"
        os.environ["ALLOW_HASH_MODE"] = "true"
        os.environ["LLM_BASE_URL"] = settings.llm_base_url
        os.environ["LLM_MODEL_REASONING"] = settings.require_reasoning_model()

        from fastapi.testclient import TestClient

        import mainframe_rag.agent.app as app_mod

        # Run test queries via FastAPI HTTP TestClient
        test_queries = [
            ("How do I resolve IEA500I IOSCMDS command rejected and what operator action is needed?", None, None),
            ("What parameter controls the 64-bit large frame area (LFAREA) in IEASYSxx?", None, None),
        ]

        successes = 0
        with TestClient(app_mod.app) as client:
            for q, prod, ver in test_queries:
                res = run_e2e_query(client, q, product=prod, version=ver)
                if res.get("success"):
                    successes += 1

        print("\n" + "=" * 60)
        print(f" E2E vLLM Test Complete: {successes}/{len(test_queries)} queries passed grounding validation")
        print("=" * 60)
        return 0 if successes == len(test_queries) else 1

    finally:
        if server_ctx is not None:
            server_ctx.stop()


if __name__ == "__main__":
    sys.exit(main())
