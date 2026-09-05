"""Agent API (v1): /healthz, /v1/search, /v1/answer. In-cluster only.

/v1/search never calls an LLM. /v1/answer retrieves, then calls the reasoning
model with retrieved chunks and validates its citations against the hit set.
Errors return a stable JSON shape {"code", "message"} — never a stack trace
(issue #20 PR C). Logs: request_id, query_kind, hit count, timings. Never the
query text.

SSE contract (?stream=true): zero or more `event: token` deltas, then exactly
one terminal `event: final` carrying the same verified citations/script as the
JSON mode (schema is identical for the empty-hits path too). A mid-stream
failure emits `event: error` and the stream ends WITHOUT a final event —
clients must treat "stream ended with no final" as failure.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field

from mainframe_rag.agent.answer import (
    HttpxLLMClient,
    TruncatedStreamError,
    as_chat_result,
    assert_reasoning_model,
    build_messages,
    build_rewrite_llm,
    classify_query_complexity,
    parse_answer,
)
from mainframe_rag.agent.tokenizer import build_tokenizer
from mainframe_rag.agent.tracing import setup_tracing, shutdown_tracing
from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.embed import build_embedder
from mainframe_rag.logs import configure_logging
from mainframe_rag.ports import (
    AsyncQdrantPoints,
    Embedder,
    LLMClient,
    QdrantPoints,
    Reranker,
    Tokenizer,
    TokenUsage,
)
from mainframe_rag.retrieve.filters import parse_query
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.retrieve.query import async_search as retrieve_search
from mainframe_rag.retrieve.rerank import build_reranker

log = logging.getLogger("agent")

settings: Settings
http: httpx2.AsyncClient
http_sync: httpx2.Client
qdrant: AsyncQdrantPoints | QdrantPoints
embedder: Embedder
llm: LLMClient
tokenizer: Tokenizer
reranker: Reranker | None = None
# Dedicated rewrite client for HyDE/step-back (issue #82): built only when a
# rewrite flag is on; carries the bounded rewrite timeout, not the 300s
# answer pool. None by default — no pool exists when the feature is off.
rewrite_llm: LLMClient | None = None
# Tracer starts as the API proxy (no-op until a real provider is installed).
# Lifespan reassigns it when tracing is enabled (issue #83); tests swap it
# directly with a tracer backed by InMemorySpanExporter.
tracer: trace.Tracer = trace.get_tracer("mainframe-rag.agent")


def _span_error(span: trace.Span, exc: Exception) -> None:
    """Record a failure on the active span. Observability only — never on
    the client response path (export errors surface in logs, if at all)."""
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


class AppError(Exception):
    """Operator-facing API error: stable code + message, no internals."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _require_query_length(request_id: str, query: str) -> None:
    """Fail closed on overlong queries (issue #87) before any embed or
    retrieval work: one helper serves both endpoints so the same fault maps
    to the same code on each. Code and message deliberately match the
    pydantic body-validation failure — an overlong query IS a validation
    failure, and no new client-visible shape is introduced."""
    if len(query) > settings.query_max_chars:
        log.warning(json_log(request_id, "query_too_long", chars=len(query)))
        raise AppError(422, "invalid_request", "request body failed validation")


async def _await_retrieval(res) -> tuple:
    """Sync/async retrieval-leg shim: the pooled async client awaits while
    sync test doubles resolve inline — one helper serves both endpoints so
    the twin call sites cannot diverge (review S2)."""
    if inspect.isawaitable(res):
        return await res
    return res


def _timing_parts(
    timings: dict, llm_ms: int | None = None, ttft_ms: int | None = None
) -> list[str]:
    """Server-Timing parts shared by /v1/search and every /v1/answer path
    (JSON, empty-hits, SSE headers): retrieval legs always, LLM legs only on
    the non-streaming answer path that measured them."""
    parts = []
    if timings.get("embed_ms") is not None:
        parts.append(f"embed;dur={timings['embed_ms']}")
    if timings.get("rewrite_ms") is not None:
        parts.append(f"rewrite;dur={timings['rewrite_ms']}")
    if timings.get("qdrant_ms") is not None:
        parts.append(f"qdrant;dur={timings['qdrant_ms']}")
    if timings.get("rerank_ms") is not None:
        parts.append(f"rerank;dur={timings['rerank_ms']}")
    if llm_ms is not None:
        parts.append(f"llm;dur={llm_ms}")
    if ttft_ms is not None:
        parts.append(f"ttft;dur={ttft_ms}")
    return parts


def _search_span_attrs(kind: str, hits: list[SearchHit]) -> dict:
    return {
        "rag.query_kind": kind,
        "rag.hits": len(hits),
        "rag.doc_ids": ",".join(dict.fromkeys(h.doc_id for h in hits)),
    }


def _answer_span_attrs(kind: str, hits: list[SearchHit], citations: int, has_script: bool) -> dict:
    return {
        "rag.query_kind": kind,
        "rag.hits": len(hits),
        "rag.citations": citations,
        "rag.has_script": has_script,
        "rag.doc_ids": ",".join(dict.fromkeys(h.doc_id for h in hits)),
    }


def _answer_log_fields(
    kind: str,
    complexity: str,
    hits: list[SearchHit],
    timings: dict,
    citations: int,
    has_script: bool,
    finish_reason: str,
    usage: TokenUsage,
    llm_ms: int,
    ttft_ms: int | None,
    started: float,
    stream: bool = False,
) -> dict:
    """Answer-leg log fields shared by the JSON and SSE finals: identical
    keys so log consumers see one shape; stream=True only marks the SSE one."""
    fields: dict = {
        "query_kind": kind,
        "query_complexity": complexity,
        "hits": len(hits),
        "embed_ms": timings.get("embed_ms"),
        "qdrant_ms": timings.get("qdrant_ms"),
        "rerank_ms": timings.get("rerank_ms"),
        "llm_ms": llm_ms,
        "citations": citations,
        "has_script": has_script,
        "finish_reason": finish_reason,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if stream:
        fields["stream"] = True
    if ttft_ms is not None:
        fields["ttft_ms"] = ttft_ms
    return fields


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global settings, http, http_sync, qdrant, embedder, llm, tokenizer, reranker, rewrite_llm
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
    # LLM rewrites (issue #82): fail closed at startup like the embed path —
    # a rewrite flag without a reasoning model cannot work per-request and
    # must not silently degrade every query to the unrewritten form.
    if (settings.hyde_enabled or settings.stepback_enabled) and (
        not settings.llm_base_url or not settings.llm_model_reasoning
    ):
        raise RuntimeError(
            "HYDE_ENABLED or STEPBACK_ENABLED is set but LLM_BASE_URL/"
            "LLM_MODEL_REASONING is not; the rewrite leg cannot run"
        )
    http_limits = httpx2.Limits(
        max_keepalive_connections=settings.http_max_keepalive_connections,
        max_connections=settings.http_max_connections,
    )
    http = httpx2.AsyncClient(
        timeout=settings.embed_timeout_s,
        transport=httpx2.AsyncHTTPTransport(retries=settings.http_connect_retries),
        limits=http_limits,
    )
    # Sync pool for the retrieval leg (embedder / tokenizer / reranker): the
    # Embedder/Reranker/Tokenizer protocols are sync, so their calls run
    # inside asyncio.to_thread off the event loop. Bounded limits like the
    # async pool; closed on shutdown. One pool on purpose — same shape as the
    # pre-async stack (review S4).
    http_sync = httpx2.Client(
        timeout=settings.embed_timeout_s,
        transport=httpx2.HTTPTransport(retries=settings.http_connect_retries),
        limits=http_limits,
    )
    # One dispatch point for embed_mode; the reasoning-model client owns its
    # own connection pool with its own (long) timeout. LLM env stays
    # request-time fail-fast (assert_reasoning_model in /v1/answer).
    embedder = build_embedder(settings, http_sync)
    tokenizer = build_tokenizer(settings, http_sync)
    reranker = build_reranker(settings, http_sync)
    # Two names on purpose: tests swap the `llm` global after startup; shutdown
    # must close the pool THIS lifespan created, never a test double.
    llm_client = HttpxLLMClient(settings)
    llm = llm_client
    rewrite_llm = build_rewrite_llm(settings) if (settings.hyde_enabled or settings.stepback_enabled) else None

    # The agent is async end to end: production always gets AsyncQdrantClient.
    # No runtime sniffing of the module attribute — a swapped class (vendored
    # shim, test double) is used as-is and sync doubles keep working through
    # the isawaitable shims below (review S2).
    import qdrant_client

    qdrant = qdrant_client.AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
        limits=http_limits,
    )
    # OTel tracing (issue #83): OFF unless OTEL_EXPORTER_OTLP_ENDPOINT is set.
    # The provider/exporter live for the process; flush + shutdown at lifespan
    # exit so in-flight spans land even on graceful shutdown. Every bounded
    # knob comes from Settings — no magic numbers here.
    global tracer
    tracer = setup_tracing(
        settings.otel_exporter_otlp_endpoint,
        sample_ratio=settings.otel_sample_ratio,
        export_queue_size=settings.otel_export_queue_size,
        export_timeout_ms=settings.otel_export_timeout_ms,
    )
    yield
    shutdown_tracing()
    if hasattr(http, "aclose"):
        await http.aclose()
    elif hasattr(http, "close"):
        http.close()

    http_sync.close()

    if hasattr(llm_client, "aclose"):
        await llm_client.aclose()
    elif hasattr(llm_client, "close"):
        llm_client.close()

    if rewrite_llm is not None and hasattr(rewrite_llm, "aclose"):
        await rewrite_llm.aclose()
    elif rewrite_llm is not None and hasattr(rewrite_llm, "close"):
        rewrite_llm.close()

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
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        _span_error(span, exc)
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
        # The pooled async client from lifespan only — no sync-call fallback.
        # A blocking GET on the event loop would stall every in-flight
        # request; if the pool is missing that is a startup bug, not
        # something to paper over (review S2).
        resp = await http.get(f"{base}/readyz", timeout=settings.health_qdrant_timeout_s)
        qdrant_ok = resp.status_code == 200 and resp.text.strip().lower() == "all shards are ready"
        if not qdrant_ok:
            # Upstream response bodies go to the log, never the client body.
            log.warning(json_log("healthz", "health", qdrant_detail=resp.text[:200]))
    except Exception as exc:
        log.warning(json_log("healthz", "health", error=str(exc)[:200]))
        raise AppError(503, "qdrant_unready", "qdrant is not ready") from exc

    if settings.embed_base_url and settings.embed_model:
        try:
            resp = await http.post(
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
async def v1_search(request: Request, req: SearchRequest, response: Response) -> SearchResponse:
    request_id = request.state.request_id
    started = time.monotonic()
    _require_query_length(request_id, req.query)
    with tracer.start_as_current_span(
        "v1.search",
        attributes={"http.request_id": request_id, "rag.limit": req.limit, "rag.query": req.query},
    ) as span:
        try:
            res = retrieve_search(
                qdrant, embedder, settings.qdrant_collection, req.query,
                product=req.product, version=req.version, limit=req.limit,
                settings=settings, reranker=reranker, llm=rewrite_llm,
            )
            hits, kind, timings = await _await_retrieval(res)
        except Exception as exc:
            _span_error(span, exc)
            log.error(json_log(request_id, "search", error=str(exc)[:200]))
            raise AppError(502, "upstream_error", "retrieval failed") from exc
        span.set_attributes(_search_span_attrs(kind, hits))
    timing_parts = _timing_parts(timings)
    if timing_parts:
        response.headers["Server-Timing"] = ", ".join(timing_parts)
    log.info(
        json_log(
            request_id, "search", query_kind=kind, hits=len(hits),
            embed_ms=timings.get("embed_ms"), rewrite_ms=timings.get("rewrite_ms"),
            qdrant_ms=timings.get("qdrant_ms"),
            rerank_ms=timings.get("rerank_ms"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    )
    return SearchResponse(
        request_id=request_id,
        query_kind=kind,
        hits=hits,
    )


_EMPTY_ANSWER_MAX_TERMS = 5


def empty_hits_answer(query: str) -> str:
    """Message for the no-hits path (issue #132).

    Identifier queries name the missing codes so typo users can spot and
    retype them; unknown codes get the truth instead of a generic empty.
    Unfiltered-serve fallback is deliberately NOT the mechanism: NEG-08
    proves it would serve the must_not sibling. Capped — a code-salad
    query must not echo unbounded input.
    """
    ids = parse_query(query)
    terms = ids.message_ids + ids.doc_ids + ids.members
    if not terms:
        return "No supporting manual excerpts were found for this question."
    shown = ", ".join(terms[:_EMPTY_ANSWER_MAX_TERMS])
    if len(terms) > _EMPTY_ANSWER_MAX_TERMS:
        shown += f", +{len(terms) - _EMPTY_ANSWER_MAX_TERMS} more"
    return f"No manual excerpts carry {shown}."


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
    _require_query_length(request_id, req.query)
    # Fail fast before any retrieval: the reasoning model (and its endpoint)
    # must be configured; nothing else is callable. Config errors get a fixed
    # client message — the exception text stays in the log.
    try:
        assert_reasoning_model(settings)
    except RuntimeError as exc:
        log.warning(json_log(request_id, "answer", error=str(exc)[:200]))
        raise AppError(503, "not_configured", "reasoning model is not configured") from exc
    llm_model = settings.require_reasoning_model()

    # One trace per request (issue #83): the root span starts after the
    # cheap fail-fast gates and lives until the response body is produced.
    # For SSE the span is ended inside the generator so the LLM stage (the
    # longest leg) is a child of the same trace, not a detached one.
    root_span = tracer.start_span(
        "v1.answer",
        attributes={"http.request_id": request_id, "rag.query": req.query, "rag.stream": is_stream},
    )

    # Retrieval and LLM legs are guarded separately: the same fault must map
    # to the same code+message on every endpoint — a retrieval failure reads
    # "retrieval failed" here exactly as it does on /v1/search, and a model or
    # parse failure must not be mislabeled as a retrieval fault (AGENTS rule 2).
    try:
        # retrieve.* stage spans must land under this request's trace, so the
        # root is made current for the retrieval leg (the root span itself is
        # not created "as current" — the SSE generator outlives this block).
        with trace.use_span(root_span, end_on_exit=False):
            res = retrieve_search(
                qdrant, embedder, settings.qdrant_collection, req.query,
                product=req.product, version=req.version, limit=8,
                settings=settings, reranker=reranker, llm=rewrite_llm,
            )
            hits, kind, timings = await _await_retrieval(res)
    except Exception as exc:
        _span_error(root_span, exc)
        root_span.end()
        log.error(json_log(request_id, "answer", error=str(exc)[:200]))
        raise AppError(502, "upstream_error", "retrieval failed") from exc

    if not hits:
        root_span.set_attributes({"rag.query_kind": kind, "rag.hits": 0})
        root_span.end()
        timing_parts = _timing_parts(timings)
        if timing_parts:
            response.headers["Server-Timing"] = ", ".join(timing_parts)
        log.info(json_log(request_id, "answer", query_kind=kind, hits=0, rerank_ms=timings.get("rerank_ms")))
        if is_stream:
            async def empty_sse():
                # Schema parity with the normal final event (review S6): the
                # empty-hits path carries the same keys; no tokens were
                # streamed, so ttft_ms stays null and usage is all zeros.
                payload = {
                    "type": "final",
                    "request_id": request_id,
                    "answer": empty_hits_answer(req.query),
                    "citations": [],
                    "script": None,
                    "query_kind": kind,
                    "hits": [],
                    "finish_reason": "stop",
                    "ttft_ms": None,
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 0,
                    },
                }
                yield f"event: final\ndata: {json.dumps(payload)}\n\n"

            headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
            if timing_parts:
                headers["Server-Timing"] = ", ".join(timing_parts)
            return StreamingResponse(empty_sse(), media_type="text/event-stream", headers=headers)

        return AnswerResponse(
            request_id=request_id,
            answer=empty_hits_answer(req.query),
            citations=[],
            script=None,
        )

    # Prompt construction is local CPU work (estimate + optional /tokenize
    # verify that pins its own fallback) — deliberately OUTSIDE the upstream
    # try below: a build failure is an internal fault and surfaces as 500
    # "internal", never mislabeled as 502 "answer failed" (review S5; pinned
    # by test_prompt_build_failure_maps_to_internal).
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
    root_ctx = trace.set_span_in_context(root_span)
    with tracer.start_as_current_span(
        "prompt.build",
        context=root_ctx,
        attributes={
            "rag.query_complexity": complexity,
            "rag.reasoning_effort": effort,
            "rag.max_context_chars": max_context,
        },
    ):
        messages = build_messages(
            req.query, hits,
            product=req.product, version=req.version, splunk_context=req.splunk_context,
            max_context_chars=max_context,
            max_chunk_chars=settings.prompt_max_chunk_chars,
            max_chunk_chars_narrative=settings.prompt_max_chunk_chars_complex if complexity == "complex" else None,
            splunk_context_max_chars=settings.splunk_context_max_chars,
            complexity=complexity,
            tokenizer=tokenizer,
            settings=settings,
            order=settings.prompt_order,
        )

    if not is_stream:
        try:
            t0 = time.monotonic()
            with tracer.start_as_current_span(
                "llm.chat", context=root_ctx,
                attributes={"llm.model": llm_model, "llm.reasoning_effort": effort},
            ) as llm_span:
                chat_call = llm.chat(
                    messages,
                    reasoning_effort=effort,
                    temperature=settings.llm_temperature,
                )
                chat_res = as_chat_result(await chat_call if inspect.isawaitable(chat_call) else chat_call)
                llm_span.set_attributes(
                    {
                        "llm.ttft_ms": chat_res.ttft_ms if chat_res.ttft_ms is not None else 0,
                        "llm.finish_reason": chat_res.finish_reason,
                        "llm.prompt_tokens": chat_res.usage.prompt_tokens,
                        "llm.completion_tokens": chat_res.usage.completion_tokens,
                        "llm.reasoning_tokens": chat_res.usage.reasoning_tokens,
                        "llm.total_tokens": chat_res.usage.total_tokens,
                    }
                )
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
            _span_error(root_span, exc)
            root_span.end()
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

        timing_parts = _timing_parts(timings, llm_ms=llm_ms, ttft_ms=ttft_ms)
        if timing_parts:
            response.headers["Server-Timing"] = ", ".join(timing_parts)

        log.info(
            json_log(
                request_id,
                "answer",
                **_answer_log_fields(
                    kind, complexity, hits, timings,
                    len(parsed.citations), parsed.script is not None,
                    finish_reason, usage, llm_ms, ttft_ms, started,
                ),
            )
        )
        root_span.set_attributes(
            _answer_span_attrs(kind, hits, len(parsed.citations), parsed.script is not None)
        )
        root_span.end()
        return AnswerResponse(
            request_id=request_id,
            answer=parsed.answer,
            citations=parsed.citations,
            script=parsed.script,
        )

    # SSE streaming path
    timing_parts = _timing_parts(timings)
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    if timing_parts:
        headers["Server-Timing"] = ", ".join(timing_parts)

    async def sse_event_generator():
        # try/finally, not a per-branch end(): a mid-stream failure (both
        # except branches return) or a client disconnect (GeneratorExit
        # raised at a yield) must still end the root span — an unended trace
        # would linger in the backend until TTL.
        try:
            async for chunk in _sse_events():
                yield chunk
        finally:
            root_span.end()

    async def _sse_events():
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
            with tracer.start_as_current_span(
                "llm.chat", context=root_ctx,
                attributes={"llm.model": llm_model, "llm.reasoning_effort": effort},
            ) as llm_span:
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
                llm_span.set_attributes(
                    {
                        "llm.ttft_ms": ttft_ms if ttft_ms is not None else 0,
                        "llm.finish_reason": finish_reason,
                        "llm.prompt_tokens": usage.prompt_tokens,
                        "llm.completion_tokens": usage.completion_tokens,
                        "llm.reasoning_tokens": usage.reasoning_tokens,
                        "llm.total_tokens": usage.total_tokens,
                    }
                )
        except TruncatedStreamError as exc:
            # Truncation observability: the partial prefix already went out
            # as token events, so the answer_alert carries counts only —
            # never response text.
            _span_error(root_span, exc)
            log.warning(
                json_log(
                    request_id,
                    "answer_alert",
                    alert="stream_truncated",
                    detail=str(exc)[:200],
                )
            )
            log.error(json_log(request_id, "answer_stream", error=str(exc)[:200]))
            err_payload = {"type": "error", "code": "upstream_error", "message": "stream failed"}
            yield f"event: error\ndata: {json.dumps(err_payload)}\n\n"
            return
        except Exception as exc:  # noqa: BLE001
            _span_error(root_span, exc)
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
        log.info(
            json_log(
                request_id,
                "answer",
                **_answer_log_fields(
                    kind, complexity, hits, timings,
                    len(parsed.citations), parsed.script is not None,
                    finish_reason, usage, llm_ms, ttft_ms, started,
                    stream=True,
                ),
            )
        )

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
        root_span.set_attributes(
            _answer_span_attrs(kind, hits, len(parsed.citations), parsed.script is not None)
        )
        yield f"event: final\ndata: {json.dumps(final_payload)}\n\n"

    return StreamingResponse(sse_event_generator(), media_type="text/event-stream", headers=headers)


def json_log(request_id: str, action: str, **fields) -> str:
    return json.dumps({"request_id": request_id, "action": action, **fields})
