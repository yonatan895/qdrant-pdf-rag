"""Agent API (v1): /healthz, /v1/search, /v1/answer. In-cluster only.

/v1/search never calls an LLM. /v1/answer retrieves, then calls the reasoning
model with retrieved chunks and validates its citations against the hit set.
Logs: request_id, query_kind, hit count, timings. Never the query text.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from mainframe_rag.agent.answer import build_messages, call_reasoning_model, parse_answer
from mainframe_rag.config import Settings, load_settings
from mainframe_rag.retrieve.query import search as retrieve_search

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

log = logging.getLogger("agent")

settings: Settings
http: httpx.Client
qdrant: QdrantClient  # type: ignore[valid-type]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global settings, http, qdrant
    settings = load_settings()
    http = httpx.Client(timeout=settings.embed_timeout_s)
    from qdrant_client import QdrantClient

    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=30)
    yield
    http.close()
    qdrant.close()


app = FastAPI(title="mainframe-rag agent", version="0.1.0", lifespan=lifespan)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    product: str | None = None
    version: str | None = None
    limit: int = Field(default=8, ge=1, le=40)


class SearchResponse(BaseModel):
    request_id: str
    query_kind: str
    hits: list[dict]


class AnswerRequest(BaseModel):
    query: str = Field(min_length=1)
    product: str | None = None
    version: str | None = None
    splunk_context: str | None = None


class AnswerResponse(BaseModel):
    request_id: str
    answer: str
    citations: list[str]
    script: str | None


@app.get("/healthz")
def healthz() -> dict:
    detail: dict = {"qdrant": False, "embed": None}
    try:
        base = settings.qdrant_url.rstrip("/")
        resp = httpx.get(f"{base}/readyz", timeout=5)
        detail["qdrant"] = resp.status_code == 200 and resp.text.strip().lower() == "all shards are ready"
        if not detail["qdrant"]:
            detail["qdrant_detail"] = resp.text[:120]
    except Exception as exc:
        detail["qdrant_detail"] = str(exc)[:120]
        raise HTTPException(status_code=503, detail=detail) from exc

    if settings.embed_base_url and settings.embed_model:
        try:
            resp = http.post(
                f"{settings.embed_base_url.rstrip('/')}/embeddings",
                json={"model": settings.embed_model, "input": ["ping"]},
                timeout=10,
            )
            detail["embed"] = resp.status_code == 200
        except Exception as exc:  # noqa: BLE001
            detail["embed"] = False
            detail["embed_detail"] = str(exc)[:120]

    return {"status": "ok", **detail}


@app.post("/v1/search", response_model=SearchResponse)
def v1_search(req: SearchRequest) -> SearchResponse:
    request_id = uuid.uuid4().hex[:12]
    try:
        hits, kind, timings = retrieve_search(
            qdrant, settings, req.query,
            product=req.product, version=req.version, limit=req.limit,
        )
    except Exception as exc:
        log.error(json_log(request_id, "search", error=str(exc)[:200]))
        raise HTTPException(status_code=502, detail="retrieval failed") from exc
    log.info(
        json_log(
            request_id, "search", query_kind=kind, hits=len(hits),
            embed_ms=timings.get("embed_ms"), qdrant_ms=timings.get("qdrant_ms"),
        )
    )
    return SearchResponse(
        request_id=request_id,
        query_kind=kind,
        hits=[hit.__dict__ for hit in hits],
    )


@app.post("/v1/answer", response_model=AnswerResponse)
def v1_answer(req: AnswerRequest) -> AnswerResponse:
    request_id = uuid.uuid4().hex[:12]
    try:
        # Fail fast: reasoning model must be configured; nothing else is callable.
        settings.require_reasoning_model()
        hits, kind, timings = retrieve_search(
            qdrant, settings, req.query,
            product=req.product, version=req.version, limit=8,
        )
        if not hits:
            log.info(json_log(request_id, "answer", query_kind=kind, hits=0))
            return AnswerResponse(
                request_id=request_id,
                answer="No supporting manual excerpts were found for this question.",
                citations=[],
                script=None,
            )

        messages = build_messages(
            req.query, hits,
            product=req.product, version=req.version, splunk_context=req.splunk_context,
        )
        t0 = time.monotonic()
        content = call_reasoning_model(messages, settings, http)
        llm_ms = int((time.monotonic() - t0) * 1000)

        parsed = parse_answer(content, {h.cite for h in hits})
        log.info(
            json_log(
                request_id, "answer", query_kind=kind, hits=len(hits),
                embed_ms=timings.get("embed_ms"), qdrant_ms=timings.get("qdrant_ms"),
                llm_ms=llm_ms, citations=len(parsed["citations"]),
            )
        )
        return AnswerResponse(
            request_id=request_id,
            answer=parsed["answer"],
            citations=parsed["citations"],
            script=parsed["script"],
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        log.error(json_log(request_id, "answer", error=str(exc)[:200]))
        raise HTTPException(status_code=502, detail="answer failed") from exc


def json_log(request_id: str, action: str, **fields) -> str:
    import json

    return json.dumps({"request_id": request_id, "action": action, **fields})
