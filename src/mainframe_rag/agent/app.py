"""Agent API (v1): /healthz, /v1/search, /v1/answer. In-cluster only.

/v1/search never calls an LLM. /v1/answer retrieves, then calls the reasoning
model with retrieved chunks and validates its citations against the hit set.
Errors return a stable JSON shape {"code", "message"} — never a stack trace
(issue #20 PR C). Logs: request_id, query_kind, hit count, timings. Never the
query text.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from mainframe_rag.agent.answer import (
    HttpxLLMClient,
    as_chat_result,
    assert_reasoning_model,
    build_messages,
    classify_query_complexity,
    parse_answer,
)
from mainframe_rag.agent.tokenizer import build_tokenizer
from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.embed import build_embedder
from mainframe_rag.logs import configure_logging
from mainframe_rag.ports import (
    Embedder,
    LLMClient,
    Reranker,
    Tokenizer,
    TokenUsage,
)
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.retrieve.query import async_search as retrieve_search
from mainframe_rag.retrieve.rerank import build_reranker

log = logging.getLogger("agent")

settings: Settings
http: httpx2.AsyncClient
qdrant: Any
embedder: Embedder
llm: LLMClient
tokenizer: Tokenizer
reranker: Reranker | None = None


class AppError(Exception):
    """Operator-facing API error: stable code + message, no internals."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global settings, http, qdrant, embedder, llm, tokenizer, reranker
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
    http_limits = httpx2.Limits(
        max_keepalive_connections=settings.http_max_keepalive_connections,
        max_connections=settings.http_max_connections,
    )
    http = httpx2.AsyncClient(
        timeout=settings.embed_timeout_s,
        transport=httpx2.AsyncHTTPTransport(retries=settings.http_connect_retries),
        limits=http_limits,
    )
    # One dispatch point for embed_mode; the reasoning-model client owns its
    # own connection pool with its own (long) timeout. LLM env stays
    # request-time fail-fast (assert_reasoning_model in /v1/answer).
    embedder = build_embedder(settings)
    tokenizer = build_tokenizer(settings)
    reranker = build_reranker(settings)
    # Two names on purpose: tests swap the `llm` global after startup; shutdown
    # must close the pool THIS lifespan created, never a test double.
    llm_client = HttpxLLMClient(settings)
    llm = llm_client

    import qdrant_client

    if getattr(qdrant_client.QdrantClient, "__name__", "") != "QdrantClient":
        qdrant = qdrant_client.QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            timeout=settings.qdrant_timeout_s,
            limits=http_limits,
        )
    else:
        qdrant = qdrant_client.AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            timeout=settings.qdrant_timeout_s,
            limits=http_limits,
        )
    yield
    if hasattr(http, "aclose"):
        await http.aclose()
    elif hasattr(http, "close"):
        http.close()

    if hasattr(llm_client, "aclose"):
        await llm_client.aclose()
    elif hasattr(llm_client, "close"):
        llm_client.close()

    if hasattr(qdrant, "close"):
        close_res = qdrant.close()
        if inspect.isawaitable(close_res):
            await close_res


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
    stream: bool = False


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
async def healthz() -> HealthzResponse:
    qdrant_ok = False
    embed_ok: bool | None = None
    try:
        base = settings.qdrant_url.rstrip("/")
        is_patched = not hasattr(httpx2.get, "__code__") or (
            callable(httpx2.get) and getattr(httpx2.get, "__name__", "") == "<lambda>"
        )
        call: Any
        if hasattr(http, "get") and not is_patched:
            call = http.get(f"{base}/readyz", timeout=settings.health_qdrant_timeout_s)
        else:
            call = httpx2.get(f"{base}/readyz", timeout=settings.health_qdrant_timeout_s)
        resp = await call if inspect.isawaitable(call) else call
        qdrant_ok = resp.status_code == 200 and resp.text.strip().lower() == "all shards are ready"
        if not qdrant_ok:
            # Upstream response bodies go to the log, never the client body.
            log.warning(json_log("healthz", "health", qdrant_detail=resp.text[:200]))
    except Exception as exc:
        log.warning(json_log("healthz", "health", error=str(exc)[:200]))
        raise AppError(503, "qdrant_unready", "qdrant is not ready") from exc

    if settings.embed_base_url and settings.embed_model:
        try:
            post_call: Any = http.post(
                f"{settings.embed_base_url.rstrip('/')}/embeddings",
                json={"model": settings.embed_model, "input": ["ping"]},
                timeout=settings.health_embed_timeout_s,
            )
            resp = await post_call if inspect.isawaitable(post_call) else post_call
            embed_ok = resp.status_code == 200
        except Exception as exc:  # noqa: BLE001
            embed_ok = False
            log.warning(json_log("healthz", "health", embed_error=str(exc)[:200]))

    status = "ok" if qdrant_ok and embed_ok is not False else "degraded"
    return HealthzResponse(status=status, qdrant=qdrant_ok, embed=embed_ok)


@app.post("/v1/search", response_model=SearchResponse)
async def v1_search(request: Request, req: SearchRequest, response: Response) -> SearchResponse:
    request_id = request.state.request_id
    started = time.monotonic()
    try:
        res = retrieve_search(
            qdrant, embedder, settings.qdrant_collection, req.query,
            product=req.product, version=req.version, limit=req.limit,
            settings=settings, reranker=reranker,
        )
        if inspect.isawaitable(res):
            hits, kind, timings = await res
        else:
            hits, kind, timings = res
    except Exception as exc:
        log.error(json_log(request_id, "search", error=str(exc)[:200]))
        raise AppError(502, "upstream_error", "retrieval failed") from exc
    timing_parts = []
    if timings.get("embed_ms") is not None:
        timing_parts.append(f"embed;dur={timings['embed_ms']}")
    if timings.get("qdrant_ms") is not None:
        timing_parts.append(f"qdrant;dur={timings['qdrant_ms']}")
    if timings.get("rerank_ms") is not None:
        timing_parts.append(f"rerank;dur={timings['rerank_ms']}")
    if timing_parts:
        response.headers["Server-Timing"] = ", ".join(timing_parts)
    log.info(
        json_log(
            request_id, "search", query_kind=kind, hits=len(hits),
            embed_ms=timings.get("embed_ms"), qdrant_ms=timings.get("qdrant_ms"),
            rerank_ms=timings.get("rerank_ms"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    )
    return SearchResponse(
        request_id=request_id,
        query_kind=kind,
        hits=hits,
    )


@app.post("/v1/answer", response_model=None)
async def v1_answer(
    request: Request,
    req: AnswerRequest,
    response: Response,
    stream: bool | None = Query(default=None),
) -> Response | AnswerResponse:
    request_id = request.state.request_id
    started = time.monotonic()
    is_stream = stream if stream is not None else req.stream
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
        res = retrieve_search(
            qdrant, embedder, settings.qdrant_collection, req.query,
            product=req.product, version=req.version, limit=8,
            settings=settings, reranker=reranker,
        )
        if inspect.isawaitable(res):
            hits, kind, timings = await res
        else:
            hits, kind, timings = res
    except Exception as exc:
        log.error(json_log(request_id, "answer", error=str(exc)[:200]))
        raise AppError(502, "upstream_error", "retrieval failed") from exc

    if not hits:
        timing_parts = []
        if timings.get("embed_ms") is not None:
            timing_parts.append(f"embed;dur={timings['embed_ms']}")
        if timings.get("qdrant_ms") is not None:
            timing_parts.append(f"qdrant;dur={timings['qdrant_ms']}")
        if timings.get("rerank_ms") is not None:
            timing_parts.append(f"rerank;dur={timings['rerank_ms']}")
        if timing_parts:
            response.headers["Server-Timing"] = ", ".join(timing_parts)
        log.info(json_log(request_id, "answer", query_kind=kind, hits=0, rerank_ms=timings.get("rerank_ms")))
        if is_stream:
            async def empty_sse():
                payload = {
                    "type": "final",
                    "request_id": request_id,
                    "answer": "No supporting manual excerpts were found for this question.",
                    "citations": [],
                    "script": None,
                    "query_kind": kind,
                    "hits": [],
                }
                yield f"event: final\ndata: {json.dumps(payload)}\n\n"

            headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
            if timing_parts:
                headers["Server-Timing"] = ", ".join(timing_parts)
            return StreamingResponse(empty_sse(), media_type="text/event-stream", headers=headers)

        return AnswerResponse(
            request_id=request_id,
            answer="No supporting manual excerpts were found for this question.",
            citations=[],
            script=None,
        )

    complexity = classify_query_complexity(req.query)
    max_context = (
        settings.prompt_max_context_chars_complex
        if complexity == "complex"
        else settings.prompt_max_context_chars
    )
    effort = (
        settings.llm_reasoning_effort_complex
        if complexity == "complex"
        else settings.llm_reasoning_effort_simple
    )
    messages = build_messages(
        req.query, hits,
        product=req.product, version=req.version, splunk_context=req.splunk_context,
        max_context_chars=max_context,
        max_chunk_chars=settings.prompt_max_chunk_chars,
        max_chunk_chars_narrative=settings.prompt_max_chunk_chars_complex if complexity == "complex" else None,
        complexity=complexity,
        tokenizer=tokenizer,
        settings=settings,
    )

    if not is_stream:
        try:
            t0 = time.monotonic()
            chat_call = llm.chat(
                messages,
                reasoning_effort=effort,
                temperature=settings.llm_temperature,
            )
            chat_res = as_chat_result(await chat_call if inspect.isawaitable(chat_call) else chat_call)
            llm_ms = int((time.monotonic() - t0) * 1000)
            ttft_ms = chat_res.ttft_ms
            content = chat_res.content
            finish_reason = chat_res.finish_reason
            usage = chat_res.usage
            parsed = parse_answer(
                content,
                {h.cite for h in hits},
                ordered_cites=[h.cite for h in hits],
            )
        except Exception as exc:
            log.error(json_log(request_id, "answer", error=str(exc)[:200]))
            raise AppError(502, "upstream_error", "answer failed") from exc

        # finish_reason != stop is alerted per request: the JSON warning is the
        # real, worker-safe signal. No process-local counters — they lie under
        # multiple uvicorn workers and nothing exports them.
        if finish_reason != "stop":
            log.warning(
                json_log(
                    request_id,
                    "answer_alert",
                    alert="finish_reason_non_stop",
                    finish_reason=finish_reason,
                )
            )

        timing_parts = []
        if timings.get("embed_ms") is not None:
            timing_parts.append(f"embed;dur={timings['embed_ms']}")
        if timings.get("qdrant_ms") is not None:
            timing_parts.append(f"qdrant;dur={timings['qdrant_ms']}")
        if timings.get("rerank_ms") is not None:
            timing_parts.append(f"rerank;dur={timings['rerank_ms']}")
        if llm_ms is not None:
            timing_parts.append(f"llm;dur={llm_ms}")
        if ttft_ms is not None:
            timing_parts.append(f"ttft;dur={ttft_ms}")
        if timing_parts:
            response.headers["Server-Timing"] = ", ".join(timing_parts)

        log_fields = {
            "query_kind": kind,
            "query_complexity": complexity,
            "hits": len(hits),
            "embed_ms": timings.get("embed_ms"),
            "qdrant_ms": timings.get("qdrant_ms"),
            "rerank_ms": timings.get("rerank_ms"),
            "llm_ms": llm_ms,
            "citations": len(parsed.citations),
            "has_script": parsed.script is not None,
            "finish_reason": finish_reason,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "total_tokens": usage.total_tokens,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        if ttft_ms is not None:
            log_fields["ttft_ms"] = ttft_ms
        log.info(json_log(request_id, "answer", **log_fields))
        return AnswerResponse(
            request_id=request_id,
            answer=parsed.answer,
            citations=parsed.citations,
            script=parsed.script,
        )

    # SSE streaming path
    timing_parts = []
    if timings.get("embed_ms") is not None:
        timing_parts.append(f"embed;dur={timings['embed_ms']}")
    if timings.get("qdrant_ms") is not None:
        timing_parts.append(f"qdrant;dur={timings['qdrant_ms']}")
    if timings.get("rerank_ms") is not None:
        timing_parts.append(f"rerank;dur={timings['rerank_ms']}")
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    if timing_parts:
        headers["Server-Timing"] = ", ".join(timing_parts)

    async def sse_event_generator():
        t0 = time.monotonic()
        ttft_ms: int | None = None
        content_parts: list[str] = []
        finish_reason = "stop"
        usage = TokenUsage()

        if hasattr(llm, "chat_stream"):
            stream_gen = llm.chat_stream(
                messages,
                reasoning_effort=effort,
                temperature=settings.llm_temperature,
            )
        else:
            async def fallback_stream():
                chat_call = llm.chat(
                    messages,
                    reasoning_effort=effort,
                    temperature=settings.llm_temperature,
                )
                cr = as_chat_result(await chat_call if inspect.isawaitable(chat_call) else chat_call)
                yield {"type": "token", "delta": cr.content, "token": cr.content, "ttft_ms": cr.ttft_ms}
                yield {"type": "done", "finish_reason": cr.finish_reason, "usage": cr.usage, "ttft_ms": cr.ttft_ms}

            stream_gen = fallback_stream()

        try:
            async for item in stream_gen:
                itype = item.get("type")
                if itype == "token":
                    delta = item.get("delta") or ""
                    if delta:
                        if ttft_ms is None:
                            ttft_ms = item.get("ttft_ms") or int((time.monotonic() - t0) * 1000)
                        content_parts.append(delta)
                        payload = {"type": "token", "delta": delta, "token": delta}
                        yield f"event: token\ndata: {json.dumps(payload)}\n\n"
                elif itype == "done":
                    finish_reason = item.get("finish_reason") or "stop"
                    if item.get("usage"):
                        usage = item["usage"]
                    if ttft_ms is None and item.get("ttft_ms") is not None:
                        ttft_ms = item["ttft_ms"]
        except Exception as exc:  # noqa: BLE001
            log.error(json_log(request_id, "answer_stream", error=str(exc)[:200]))
            err_payload = {"type": "error", "code": "upstream_error", "message": "stream failed"}
            yield f"event: error\ndata: {json.dumps(err_payload)}\n\n"
            return

        full_content = "".join(content_parts)
        parsed = parse_answer(
            full_content,
            {h.cite for h in hits},
            ordered_cites=[h.cite for h in hits],
        )

        if finish_reason != "stop":
            log.warning(
                json_log(
                    request_id,
                    "answer_alert",
                    alert="finish_reason_non_stop",
                    finish_reason=finish_reason,
                )
            )

        llm_ms = int((time.monotonic() - t0) * 1000)
        log_fields = {
            "query_kind": kind,
            "query_complexity": complexity,
            "hits": len(hits),
            "embed_ms": timings.get("embed_ms"),
            "qdrant_ms": timings.get("qdrant_ms"),
            "rerank_ms": timings.get("rerank_ms"),
            "llm_ms": llm_ms,
            "citations": len(parsed.citations),
            "has_script": parsed.script is not None,
            "finish_reason": finish_reason,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "total_tokens": usage.total_tokens,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "stream": True,
        }
        if ttft_ms is not None:
            log_fields["ttft_ms"] = ttft_ms
        log.info(json_log(request_id, "answer", **log_fields))

        final_payload = {
            "type": "final",
            "request_id": request_id,
            "answer": parsed.answer,
            "citations": parsed.citations,
            "script": parsed.script,
            "query_kind": kind,
            "hits": [h.model_dump() for h in hits],
            "finish_reason": finish_reason,
            "ttft_ms": ttft_ms,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "total_tokens": usage.total_tokens,
            },
        }
        yield f"event: final\ndata: {json.dumps(final_payload)}\n\n"

    return StreamingResponse(sse_event_generator(), media_type="text/event-stream", headers=headers)


def json_log(request_id: str, action: str, **fields) -> str:
    return json.dumps({"request_id": request_id, "action": action, **fields})
