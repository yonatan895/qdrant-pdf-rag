"""Agent API (v1): /healthz, /v1/search, /v1/answer. In-cluster only.

/v1/search never calls an LLM. /v1/answer retrieves, then calls the reasoning
model with retrieved chunks and validates its citations against the hit set.
Errors return a stable JSON shape {"code", "message"} — never a stack trace
(issue #20 PR C). Logs: request_id, query_kind, hit count, timings. Never the
query text.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mainframe_rag.agent.answer import (
    HttpxLLMClient,
    assert_reasoning_model,
    build_messages,
    parse_answer,
)
from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.embed import build_embedder
from mainframe_rag.logs import configure_logging
from mainframe_rag.ports import Embedder, LLMClient
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.retrieve.query import search as retrieve_search

if TYPE_CHECKING:
    from mainframe_rag.ports import QdrantPoints

log = logging.getLogger("agent")

settings: Settings
http: httpx2.Client
qdrant: QdrantPoints
embedder: Embedder
llm: LLMClient


class AppError(Exception):
    """Operator-facing API error: stable code + message, no internals."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global settings, http, qdrant, embedder, llm
    settings = load_settings()
    configure_logging(settings.log_level)
    # Startup fail-fast (issue #20 PR D): the agent refuses to listen on a
    # misconfigured embed path rather than failing per-request. Hash mode is
    # CI/dev only and must be explicitly allowed.
    if settings.embed_mode not in ("hash", "vllm"):
        raise RuntimeError(
            f"EMBED_MODE={settings.embed_mode!r} is not one of hash|vllm"
        )
    if settings.embed_mode == "hash" and not settings.allow_hash_mode:
        raise RuntimeError(
            "EMBED_MODE=hash is CI/dev only; set ALLOW_HASH_MODE=true (CI overlay) to allow it"
        )
    if settings.embed_mode == "vllm":
        settings.require_dense_dim()
        settings.require_embed()
    # Bounded connection retries only fire when the request was never sent
    # (DNS/refused) — safe for any method. No request-level retries exist.
    http_limits = httpx2.Limits(max_keepalive_connections=100, max_connections=200)
    http = httpx2.Client(
        timeout=settings.embed_timeout_s,
        transport=httpx2.HTTPTransport(retries=settings.http_connect_retries),
        limits=http_limits,
    )
    # One dispatch point for embed_mode; the reasoning-model client owns its
    # own connection pool with its own (long) timeout. LLM env stays
    # request-time fail-fast (assert_reasoning_model in /v1/answer).
    embedder = build_embedder(settings, http)
    # Two names on purpose: tests swap the `llm` global after startup; shutdown
    # must close the pool THIS lifespan created, never a test double.
    llm_client = HttpxLLMClient(settings)
    llm = llm_client

    from qdrant_client import QdrantClient

    qdrant = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
        limits=http_limits,
    )
    yield
    http.close()
    llm_client.close()
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
    hits: list[SearchHit]


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


class HealthzResponse(BaseModel):
    status: str = "ok"
    qdrant: bool
    embed: bool | None = None


class ErrorEnvelope(BaseModel):
    code: str
    message: str


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    """One request id per request, shared by every log line including the
    unhandled-error handler (round-7 review)."""
    request.state.request_id = uuid.uuid4().hex[:12]
    return await call_next(request)


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=ErrorEnvelope(code=exc.code, message=exc.message).model_dump())


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    # Fixed message on purpose: nothing in src/ raises HTTPException, this
    # only fires from framework internals, and exc.detail must never reach a
    # client body (the "no internals" rule is structural, not incidental).
    return JSONResponse(
        status_code=exc.status_code, content=ErrorEnvelope(code="http_error", message="request failed").model_dump()
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=ErrorEnvelope(code="invalid_request", message="request body failed validation").model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full trace stays in server logs; the client never sees internals.
    request_id = getattr(request.state, "request_id", "unknown")
    log.exception(json_log(request_id, "unhandled", error=str(exc)[:200]))
    return JSONResponse(
        status_code=500, content=ErrorEnvelope(code="internal", message="internal error").model_dump()
    )


# Router-level 404/405 raise Starlette's HTTPException, which the FastAPI
# subclass handler does not cover — key these by status code.
@app.exception_handler(404)
async def not_found_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=404, content=ErrorEnvelope(code="not_found", message="not found").model_dump()
    )


@app.exception_handler(405)
async def method_not_allowed_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=405,
        content=ErrorEnvelope(code="method_not_allowed", message="method not allowed").model_dump(),
    )


@app.get("/healthz", response_model=HealthzResponse)
def healthz() -> HealthzResponse:
    qdrant_ok = False
    embed_ok: bool | None = None
    try:
        base = settings.qdrant_url.rstrip("/")
        resp = httpx2.get(f"{base}/readyz", timeout=settings.health_qdrant_timeout_s)
        qdrant_ok = resp.status_code == 200 and resp.text.strip().lower() == "all shards are ready"
        if not qdrant_ok:
            # Upstream response bodies go to the log, never the client body.
            log.warning(json_log("healthz", "health", qdrant_detail=resp.text[:200]))
    except Exception as exc:
        log.warning(json_log("healthz", "health", error=str(exc)[:200]))
        raise AppError(503, "qdrant_unready", "qdrant is not ready") from exc

    if settings.embed_base_url and settings.embed_model:
        try:
            resp = http.post(
                f"{settings.embed_base_url.rstrip('/')}/embeddings",
                json={"model": settings.embed_model, "input": ["ping"]},
                timeout=settings.health_embed_timeout_s,
            )
            embed_ok = resp.status_code == 200
        except Exception as exc:  # noqa: BLE001
            embed_ok = False
            log.warning(json_log("healthz", "health", embed_error=str(exc)[:200]))

    status = "ok" if qdrant_ok and embed_ok is not False else "degraded"
    return HealthzResponse(status=status, qdrant=qdrant_ok, embed=embed_ok)


@app.post("/v1/search", response_model=SearchResponse)
def v1_search(request: Request, req: SearchRequest) -> SearchResponse:
    request_id = request.state.request_id
    started = time.monotonic()
    try:
        hits, kind, timings = retrieve_search(
            qdrant, embedder, settings.qdrant_collection, req.query,
            product=req.product, version=req.version, limit=req.limit,
        )
    except Exception as exc:
        log.error(json_log(request_id, "search", error=str(exc)[:200]))
        raise AppError(502, "upstream_error", "retrieval failed") from exc
    log.info(
        json_log(
            request_id, "search", query_kind=kind, hits=len(hits),
            embed_ms=timings.get("embed_ms"), qdrant_ms=timings.get("qdrant_ms"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    )
    return SearchResponse(
        request_id=request_id,
        query_kind=kind,
        hits=hits,
    )


@app.post("/v1/answer", response_model=AnswerResponse)
def v1_answer(request: Request, req: AnswerRequest) -> AnswerResponse:
    request_id = request.state.request_id
    started = time.monotonic()
    # Fail fast before any retrieval: the reasoning model (and its endpoint)
    # must be configured; nothing else is callable. Config errors get a fixed
    # client message — the exception text stays in the log.
    try:
        assert_reasoning_model(settings)
    except RuntimeError as exc:
        log.warning(json_log(request_id, "answer", error=str(exc)[:200]))
        raise AppError(503, "not_configured", "reasoning model is not configured") from exc

    # Retrieval and LLM legs are guarded separately: the same fault must map
    # to the same code+message on every endpoint — a retrieval failure reads
    # "retrieval failed" here exactly as it does on /v1/search, and a model or
    # parse failure must not be mislabeled as a retrieval fault (AGENTS rule 2).
    try:
        hits, kind, timings = retrieve_search(
            qdrant, embedder, settings.qdrant_collection, req.query,
            product=req.product, version=req.version, limit=8,
        )
    except Exception as exc:
        log.error(json_log(request_id, "answer", error=str(exc)[:200]))
        raise AppError(502, "upstream_error", "retrieval failed") from exc

    if not hits:
        log.info(json_log(request_id, "answer", query_kind=kind, hits=0))
        return AnswerResponse(
            request_id=request_id,
            answer="No supporting manual excerpts were found for this question.",
            citations=[],
            script=None,
        )

    try:
        messages = build_messages(
            req.query, hits,
            product=req.product, version=req.version, splunk_context=req.splunk_context,
            max_context_chars=settings.prompt_max_context_chars,
            max_chunk_chars=settings.prompt_max_chunk_chars,
        )
        t0 = time.monotonic()
        content = llm.chat(messages)
        llm_ms = int((time.monotonic() - t0) * 1000)
        parsed = parse_answer(
            content,
            {h.cite for h in hits},
            ordered_cites=[h.cite for h in hits],
        )
    except Exception as exc:
        log.error(json_log(request_id, "answer", error=str(exc)[:200]))
        raise AppError(502, "upstream_error", "answer failed") from exc

    log.info(
        json_log(
            request_id, "answer", query_kind=kind, hits=len(hits),
            embed_ms=timings.get("embed_ms"), qdrant_ms=timings.get("qdrant_ms"),
            llm_ms=llm_ms, citations=len(parsed.citations),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    )
    return AnswerResponse(
        request_id=request_id,
        answer=parsed.answer,
        citations=parsed.citations,
        script=parsed.script,
    )


def json_log(request_id: str, action: str, **fields) -> str:
    return json.dumps({"request_id": request_id, "action": action, **fields})
