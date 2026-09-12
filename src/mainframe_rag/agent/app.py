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

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
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
    build_chat_messages,
    build_messages,
    classify_query_complexity,
    condense_query,
    parse_answer,
)
from mainframe_rag.agent.metrics import endpoint_for_path, record_request, setup_metrics
from mainframe_rag.agent.sse import (
    empty_final_payload,
    error_payload,
    fallback_stream,
    final_payload,
    format_openai_chunk,
    format_openai_done,
    format_sse_event,
)
from mainframe_rag.agent.tokenizer import build_tokenizer
from mainframe_rag.agent.zowe_mcp import build_zowe_mcp, probe_zowe_mcp
from mainframe_rag.config import Settings, bearer_auth_headers, load_settings
from mainframe_rag.ingest.embed import build_embedder
from mainframe_rag.logs import configure_logging
from mainframe_rag.ports import (
    AsyncQdrantPoints,
    ChatMessage,
    Embedder,
    LLMClient,
    QdrantPoints,
    Reranker,
    Tokenizer,
    TokenUsage,
    ZoweMCP,
)
from mainframe_rag.retrieve.filters import parse_query
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.retrieve.query import async_search as retrieve_search
from mainframe_rag.retrieve.rerank import build_reranker, probe_reranker
from mainframe_rag.tracing import parent_context, setup_tracing, shutdown_tracing

log = logging.getLogger("agent")

settings: Settings
http: httpx2.AsyncClient
http_sync: httpx2.Client
qdrant: AsyncQdrantPoints | QdrantPoints
embedder: Embedder
llm: LLMClient
tokenizer: Tokenizer
reranker: Reranker | None = None
zowe_mcp: ZoweMCP | None = None
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
    inline_bracket_present: bool = False,
    citations_header_present: bool = False,
    cites_rejected_shape_bad: int = 0,
    cites_rejected_unmapped: int = 0,
) -> dict:
    """Answer-leg log fields shared by the JSON and SSE finals: identical
    keys so log consumers see one shape; stream=True only marks the SSE one.
    The citation-attempt counters (issue #299) let the eval split zero-cite
    rows into malformed vs fabricated vs never-attempted without putting
    model output on the wire."""
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
        "inline_bracket_present": inline_bracket_present,
        "citations_header_present": citations_header_present,
        "cites_rejected_shape_bad": cites_rejected_shape_bad,
        "cites_rejected_unmapped": cites_rejected_unmapped,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if stream:
        fields["stream"] = True
    if ttft_ms is not None:
        fields["ttft_ms"] = ttft_ms
    return fields


def _alert_finish_reason_non_stop(request_id: str, finish_reason: str) -> None:
    """Per-request alert when the reasoning model stops abnormally: the JSON
    warning is the real, worker-safe log signal; the countable signal is the
    OTel rag.requests counter (single uvicorn worker only, see metrics.py).
    Shared by the JSON and SSE finals so the alert cannot diverge copies."""
    if finish_reason != "stop":
        log.warning(
            json_log(
                request_id,
                "answer_alert",
                alert="finish_reason_non_stop",
                finish_reason=finish_reason,
            )
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global settings, http, http_sync, qdrant, embedder, llm, tokenizer, reranker, zowe_mcp
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
    if reranker is not None:
        # Best-effort reachability ping (warn-only): a mispointed
        # RERANK_BASE_URL should surface as one loud startup line, not as
        # per-request failures. Never fail-closed here — rerank is opt-in
        # and must not keep the agent from listening at startup.
        # Off the event loop like every other sync leg.
        probe_error = await asyncio.to_thread(probe_reranker, reranker)
        if probe_error is not None:
            log.warning(json_log("lifespan", "reranker_unreachable", error=probe_error[:200]))
    # Live z/OS state (ADR-0003, phase 2): default-off client, built only
    # when enabled. Same warn-only probe discipline as the reranker — a
    # dead bridge or a surprising tool registration must not keep the
    # agent from listening. No endpoint calls it yet (phase 3 wiring).
    zowe_mcp = build_zowe_mcp(settings)
    if zowe_mcp is not None:
        probe_error = await asyncio.to_thread(probe_zowe_mcp, zowe_mcp)
        if probe_error is not None:
            log.warning(json_log("lifespan", "zowe_mcp_unreachable", error=probe_error[:200]))
    # Two names on purpose: tests swap the `llm` global after startup; shutdown
    # must close the pool THIS lifespan created, never a test double.
    llm_client = HttpxLLMClient(settings)
    llm = llm_client

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
    # Prometheus metrics (issue #187): process-global provider + reader for
    # UWM scrapes of GET /metrics. Pull model — nothing to flush, so no
    # shutdown step; idempotent across lifespan re-entry.
    setup_metrics(settings.metrics_enabled)
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

    if hasattr(qdrant, "close"):
        close_res = qdrant.close()
        if inspect.isawaitable(close_res):
            await close_res

    if zowe_mcp is not None and hasattr(zowe_mcp, "close"):
        zowe_mcp.close()


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
    # Provenance (issue #269): true when every citation was mapped from bare
    # bracket markers with no explicit citation line — surfaced so clients
    # and the eval never mistake inferred provenance for grounding.
    citations_inferred: bool = False
    # Which prompt excerpt indices the inferred citations came from, 1-based
    # (issue #299): the bool says the cites were inferred, this says from
    # where, so right-doc/wrong-index is measurable. Empty on every other
    # path, so the schema is identical on JSON and SSE.
    inferred_indices: list[int] = Field(default_factory=list)
    script: str | None


class ChatCompletionsRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    splunk_context: str | None = None
    product: str | None = None
    version: str | None = None


class ChatMessageResponse(BaseModel):
    role: str = "assistant"
    content: str


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessageResponse
    finish_reason: str = "stop"


class ChatCompletionsResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: TokenUsage = Field(default_factory=TokenUsage)
    citations: list[str] = Field(default_factory=list)
    citations_inferred: bool = False
    inferred_indices: list[int] = Field(default_factory=list)
    hits: list[SearchHit] = Field(default_factory=list)


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
    unhandled-error handler (round-7 review). Also stamps the arrival time so
    error handlers (which have no endpoint-local `started`) can still record
    RED durations."""
    request.state.request_id = uuid.uuid4().hex[:12]
    request.state.started = time.monotonic()
    return await call_next(request)


def _record_handler_error(request: Request, code: str) -> None:
    """RED outcome for every error-handler response. One helper serves all
    six handlers so a new error shape cannot forget its series; non-product
    paths (scrapes, probes, unknown routes) map to no endpoint and are
    skipped. query_class is unknown here by construction — the handlers run
    for requests whose retrieval leg may never have started. Skipped when
    the endpoint already recorded a richer series (kind/hits known) before
    raising — each request counts exactly once."""
    if getattr(request.state, "red_recorded", False):
        return
    # getattr, not attribute access: the handler also serves synthetic
    # requests (tests) that carry no URL — telemetry degrades to skipping.
    url = getattr(request, "url", None)
    path = getattr(url, "path", "") if url is not None else ""
    endpoint = endpoint_for_path(path)
    if endpoint is None:
        return
    started = getattr(request.state, "started", None)
    elapsed = time.monotonic() - started if started is not None else 0.0
    record_request(endpoint, code, elapsed_s=elapsed)


def _record_endpoint(
    request: Request,
    endpoint: str,
    outcome: str,
    started: float,
    *,
    query_class: str = "unknown",
    hits: int | None = None,
    ttft_ms: int | None = None,
    llm_model: str | None = None,
) -> None:
    """RED record for endpoint-leg outcomes (success and raised errors).
    Marks the request so the error handler does not double-count the
    AppError that follows a recorded raise."""
    request.state.red_recorded = True
    record_request(
        endpoint, outcome, query_class=query_class,
        elapsed_s=time.monotonic() - started, hits=hits,
        ttft_ms=ttft_ms, llm_model=llm_model,
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    _record_handler_error(request, exc.code)
    return JSONResponse(status_code=exc.status, content=ErrorEnvelope(code=exc.code, message=exc.message).model_dump())


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # Fixed message on purpose: nothing in src/ raises HTTPException, this
    # only fires from framework internals, and exc.detail must never reach a
    # client body (the "no internals" rule is structural, not incidental).
    _record_handler_error(request, "http_error")
    return JSONResponse(
        status_code=exc.status_code, content=ErrorEnvelope(code="http_error", message="request failed").model_dump()
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    _record_handler_error(request, "invalid_request")
    return JSONResponse(
        status_code=422,
        content=ErrorEnvelope(code="invalid_request", message="request body failed validation").model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full trace stays in server logs; the client never sees internals.
    _record_handler_error(request, "internal")
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
async def not_found_handler(request: Request, _exc: Exception) -> JSONResponse:
    _record_handler_error(request, "not_found")
    return JSONResponse(
        status_code=404, content=ErrorEnvelope(code="not_found", message="not found").model_dump()
    )


@app.exception_handler(405)
async def method_not_allowed_handler(request: Request, _exc: Exception) -> JSONResponse:
    _record_handler_error(request, "method_not_allowed")
    return JSONResponse(
        status_code=405,
        content=ErrorEnvelope(code="method_not_allowed", message="method not allowed").model_dump(),
    )


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus text exposition for UWM scrapes (issue #187). Opt-in via
    Settings.metrics_enabled — disabled serves the stable 404 envelope, so
    scanners learn nothing about the process. Scrape failures are a fixed
    503; upstream text never reaches the client body. No trace span: scrapes
    must not pollute request traces."""
    if not settings.metrics_enabled:
        raise AppError(404, "not_found", "not found")
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        body = generate_latest()
    except Exception as exc:
        log.warning(json_log("metrics", "scrape_failed", error=str(exc)[:200]))
        raise AppError(503, "metrics_unavailable", "metrics are not available") from exc
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)


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
                headers=bearer_auth_headers(settings.embed_api_key),
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
        context=parent_context(request.headers),
        attributes={"http.request_id": request_id, "rag.limit": req.limit, "rag.query": req.query},
    ) as span:
        try:
            res = retrieve_search(
                qdrant, embedder, settings.qdrant_collection, req.query,
                product=req.product, version=req.version, limit=req.limit,
                settings=settings, reranker=reranker,
            )
            hits, kind, timings = await _await_retrieval(res)
        except Exception as exc:
            _span_error(span, exc)
            _record_endpoint(request, "search", "upstream_error", started)
            log.error(json_log(request_id, "search", error=str(exc)[:200]))
            raise AppError(502, "upstream_error", "retrieval failed") from exc
        span.set_attributes(_search_span_attrs(kind, hits))
    timing_parts = _timing_parts(timings)
    if timing_parts:
        response.headers["Server-Timing"] = ", ".join(timing_parts)
    _record_endpoint(request, "search", "ok", started, query_class=kind, hits=len(hits))
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
        _record_endpoint(request, "answer", "not_configured", started)
        log.warning(json_log(request_id, "answer", error=str(exc)[:200]))
        raise AppError(503, "not_configured", "reasoning model is not configured") from exc
    llm_model = settings.require_reasoning_model()

    # One trace per request (issue #83): the root span starts after the
    # cheap fail-fast gates and lives until the response body is produced.
    # For SSE the span is ended inside the generator so the LLM stage (the
    # longest leg) is a child of the same trace, not a detached one.
    root_span = tracer.start_span(
        "v1.answer",
        context=parent_context(request.headers),
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
                settings=settings, reranker=reranker,
            )
            hits, kind, timings = await _await_retrieval(res)
    except Exception as exc:
        _span_error(root_span, exc)
        root_span.end()
        _record_endpoint(request, "answer", "upstream_error", started)
        log.error(json_log(request_id, "answer", error=str(exc)[:200]))
        raise AppError(502, "upstream_error", "retrieval failed") from exc

    if not hits:
        root_span.set_attributes({"rag.query_kind": kind, "rag.hits": 0})
        root_span.end()
        _record_endpoint(request, "answer", "ok", started, query_class=kind, hits=0)
        timing_parts = _timing_parts(timings)
        if timing_parts:
            response.headers["Server-Timing"] = ", ".join(timing_parts)
        log.info(json_log(request_id, "answer", query_kind=kind, hits=0, rerank_ms=timings.get("rerank_ms")))
        if is_stream:
            async def empty_sse():
                # Schema parity with the normal final event (review S6): the
                # empty-hits path carries the same keys; no tokens were
                # streamed, so ttft_ms stays null and usage is all zeros.
                yield format_sse_event(
                    "final",
                    empty_final_payload(request_id, empty_hits_answer(req.query), kind),
                )

            headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
            if timing_parts:
                headers["Server-Timing"] = ", ".join(timing_parts)
            return StreamingResponse(empty_sse(), media_type="text/event-stream", headers=headers)

        return AnswerResponse(
            request_id=request_id,
            answer=empty_hits_answer(req.query),
            citations=[],
            citations_inferred=False,
            inferred_indices=[],
            script=None,
        )

    # Prompt construction is local CPU work plus the optional /tokenize
    # verify RPC (the tokenizer protocol is sync), dispatched via
    # asyncio.to_thread so neither blocks the event loop. A build failure is
    # an internal fault and surfaces as 500 "internal", never mislabeled as
    # 502 "answer failed" (review S5; pinned by
    # test_prompt_build_failure_maps_to_internal).
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
        messages = await asyncio.to_thread(
            build_messages,
            req.query,
            hits,
            product=req.product,
            version=req.version,
            splunk_context=req.splunk_context,
            max_context_chars=max_context,
            max_chunk_chars=settings.prompt_max_chunk_chars,
            max_chunk_chars_narrative=(
                settings.prompt_max_chunk_chars_complex if complexity == "complex" else None
            ),
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
            _record_endpoint(
                request, "answer", "upstream_error", started,
                query_class=kind, hits=len(hits),
            )
            log.error(json_log(request_id, "answer", error=str(exc)[:200]))
            raise AppError(502, "upstream_error", "answer failed") from exc

        _alert_finish_reason_non_stop(request_id, finish_reason)

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
                    inline_bracket_present=parsed.inline_bracket_present,
                    citations_header_present=parsed.citations_header_present,
                    cites_rejected_shape_bad=parsed.cites_rejected_shape_bad,
                    cites_rejected_unmapped=parsed.cites_rejected_unmapped,
                ),
            )
        )
        root_span.set_attributes(
            _answer_span_attrs(kind, hits, len(parsed.citations), parsed.script is not None)
        )
        root_span.end()
        _record_endpoint(
            request, "answer", "ok", started, query_class=kind,
            hits=len(hits), ttft_ms=ttft_ms, llm_model=llm_model,
        )
        return AnswerResponse(
            request_id=request_id,
            answer=parsed.answer,
            citations=parsed.citations,
            citations_inferred=parsed.citations_inferred,
            inferred_indices=parsed.inferred_indices,
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

    async def _sse_events() -> AsyncIterator[str]:
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
            stream_gen = fallback_stream(llm, messages, effort, settings.llm_temperature)

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
                            yield format_sse_event("token", {"type": "token", "delta": delta, "token": delta})
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
            _record_endpoint(
                request, "answer", "upstream_error", started,
                query_class=kind, hits=len(hits),
            )
            log.error(json_log(request_id, "answer_stream", error=str(exc)[:200]))
            yield format_sse_event("error", error_payload())
            return
        except Exception as exc:  # noqa: BLE001
            _span_error(root_span, exc)
            _record_endpoint(
                request, "answer", "upstream_error", started,
                query_class=kind, hits=len(hits),
            )
            log.error(json_log(request_id, "answer_stream", error=str(exc)[:200]))
            yield format_sse_event("error", error_payload())
            return

        full_content = "".join(content_parts)
        parsed = parse_answer(
            full_content,
            {h.cite for h in hits},
            ordered_cites=[h.cite for h in hits],
        )

        _alert_finish_reason_non_stop(request_id, finish_reason)

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
                    inline_bracket_present=parsed.inline_bracket_present,
                    citations_header_present=parsed.citations_header_present,
                    cites_rejected_shape_bad=parsed.cites_rejected_shape_bad,
                    cites_rejected_unmapped=parsed.cites_rejected_unmapped,
                ),
            )
        )

        final = final_payload(
            request_id,
            parsed.answer,
            parsed.citations,
            parsed.citations_inferred,
            parsed.script,
            kind,
            hits,
            finish_reason,
            ttft_ms,
            usage,
            inferred_indices=parsed.inferred_indices,
        )
        root_span.set_attributes(
            _answer_span_attrs(kind, hits, len(parsed.citations), parsed.script is not None)
        )
        _record_endpoint(
            request, "answer", "ok", started, query_class=kind,
            hits=len(hits), ttft_ms=ttft_ms, llm_model=llm_model,
        )
        yield format_sse_event("final", final)

    return StreamingResponse(sse_event_generator(), media_type="text/event-stream", headers=headers)


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(req: ChatCompletionsRequest, request: Request, response: Response):
    """OpenAI-compatible multi-turn chat completions route.

    Supports non-streaming JSON and streaming SSE (data: {...}\n\ndata: [DONE]\n\n).
    Executes low-effort query condensation for follow-up turns (>1), hybrid retrieval against
    Qdrant, sliding-window context assembly with excerpt pruning, reasoning LLM generation,
    and turn-local citation validation.
    """
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
    started = getattr(request.state, "started", time.monotonic())
    parent_ctx = parent_context(request)
    llm_model = req.model or settings.llm_model_reasoning or "reasoning-model"

    latest_user_msgs = [m for m in req.messages if m.role == "user"]
    if not latest_user_msgs:
        raise AppError(422, "invalid_request", "at least one user message is required")
    latest_query = latest_user_msgs[-1].content.strip()
    _require_query_length(request_id, latest_query)

    is_stream = req.stream
    root_span = tracer.start_span("v1.chat", context=parent_ctx, attributes={"rag.stream": is_stream})
    root_ctx = trace.set_span_in_context(root_span, parent_ctx)

    try:
        if len(req.messages) > 1:
            with tracer.start_as_current_span("chat.condense", context=root_ctx):
                search_query = await condense_query(llm, req.messages, settings)
        else:
            search_query = latest_query

        with tracer.start_as_current_span("chat.retrieve", context=root_ctx):
            retrieval_coro = retrieve_search(
                qdrant,
                embedder,
                settings.qdrant_collection,
                search_query,
                product=req.product,
                version=req.version,
                limit=8,
                settings=settings,
                reranker=reranker,
            )
            hits, kind, _timings = await _await_retrieval(retrieval_coro)
    except Exception as exc:
        _span_error(root_span, exc)
        root_span.end()
        _record_endpoint(request, "chat", "upstream_error", started)
        log.error(json_log(request_id, "chat_retrieval", error=str(exc)[:200]))
        raise AppError(502, "upstream_error", "retrieval failed") from exc

    complexity = classify_query_complexity(latest_query)
    effort = (
        settings.llm_reasoning_effort_complex
        if complexity == "complex"
        else settings.llm_reasoning_effort_simple
    )
    max_context = (
        settings.prompt_max_context_chars_complex
        if complexity == "complex"
        else settings.prompt_max_context_chars
    )

    chat_messages = await asyncio.to_thread(
        build_chat_messages,
        req.messages,
        hits,
        product=req.product,
        version=req.version,
        splunk_context=req.splunk_context,
        max_context_chars=max_context,
        max_chunk_chars=settings.prompt_max_chunk_chars,
        max_chunk_chars_narrative=(
            settings.prompt_max_chunk_chars_complex if complexity == "complex" else None
        ),
        splunk_context_max_chars=settings.splunk_context_max_chars,
        complexity=complexity,
        tokenizer=tokenizer,
        settings=settings,
        order=settings.prompt_order,
    )

    if not is_stream:
        try:
            with tracer.start_as_current_span(
                "llm.chat", context=root_ctx,
                attributes={"llm.model": llm_model, "llm.reasoning_effort": effort},
            ) as llm_span:
                chat_call = llm.chat(
                    chat_messages,
                    reasoning_effort=effort,
                    temperature=req.temperature if req.temperature is not None else settings.llm_temperature,
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
            parsed = parse_answer(
                chat_res.content,
                {h.cite for h in hits},
                ordered_cites=[h.cite for h in hits],
            )
        except Exception as exc:
            _span_error(root_span, exc)
            root_span.end()
            _record_endpoint(request, "chat", "upstream_error", started, query_class=kind, hits=len(hits))
            log.error(json_log(request_id, "chat_answer", error=str(exc)[:200]))
            raise AppError(502, "upstream_error", "answer failed") from exc

        _alert_finish_reason_non_stop(request_id, chat_res.finish_reason)

        content = parsed.answer
        if parsed.citations:
            content += "\n\n**Citations:**\n" + "\n".join(f"- {c}" for c in parsed.citations)

        _record_endpoint(
            request, "chat", "ok", started, query_class=kind,
            hits=len(hits), ttft_ms=chat_res.ttft_ms, llm_model=llm_model,
        )
        root_span.end()

        return ChatCompletionsResponse(
            id=f"chatcmpl-{request_id}",
            created=int(time.time()),
            model=llm_model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessageResponse(role="assistant", content=content),
                    finish_reason=chat_res.finish_reason,
                )
            ],
            usage=chat_res.usage,
            citations=parsed.citations,
            citations_inferred=parsed.citations_inferred,
            inferred_indices=parsed.inferred_indices,
            hits=hits,
        )

    async def _chat_sse_events() -> AsyncIterator[str]:
        t0 = time.monotonic()
        ttft_ms: int | None = None
        content_parts: list[str] = []
        finish_reason = "stop"
        usage = TokenUsage()
        chat_id = f"chatcmpl-{request_id}"

        if hasattr(llm, "chat_stream"):
            stream_gen = llm.chat_stream(
                chat_messages,
                reasoning_effort=effort,
                temperature=req.temperature if req.temperature is not None else settings.llm_temperature,
            )
        else:
            stream_gen = fallback_stream(
                llm, chat_messages, effort,
                req.temperature if req.temperature is not None else settings.llm_temperature,
            )

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
                            yield format_openai_chunk(chat_id, llm_model, delta_content=delta)
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
        except Exception as exc:  # noqa: BLE001 — streaming SSE generator traps upstream error
            _span_error(root_span, exc)
            _record_endpoint(request, "chat", "upstream_error", started, query_class=kind, hits=len(hits))
            log.error(json_log(request_id, "chat_stream", error=str(exc)[:200]))
            yield format_openai_chunk(chat_id, llm_model, finish_reason="error")
            yield format_openai_done()
            return

        full_content = "".join(content_parts)
        parsed = parse_answer(
            full_content,
            {h.cite for h in hits},
            ordered_cites=[h.cite for h in hits],
        )

        if parsed.citations:
            cites_delta = "\n\n**Citations:**\n" + "\n".join(f"- {c}" for c in parsed.citations)
            yield format_openai_chunk(chat_id, llm_model, delta_content=cites_delta)

        extra_meta = {
            "citations": parsed.citations,
            "citations_inferred": parsed.citations_inferred,
            "inferred_indices": parsed.inferred_indices,
            "hits": [h.model_dump() for h in hits],
        }
        yield format_openai_chunk(chat_id, llm_model, finish_reason=finish_reason, extra=extra_meta)
        yield format_openai_done()

        _record_endpoint(
            request, "chat", "ok", started, query_class=kind,
            hits=len(hits), ttft_ms=ttft_ms, llm_model=llm_model,
        )

    async def sse_event_generator() -> AsyncIterator[str]:
        try:
            async for chunk in _chat_sse_events():
                yield chunk
        finally:
            root_span.end()

    return StreamingResponse(sse_event_generator(), media_type="text/event-stream")


def json_log(request_id: str, action: str, **fields) -> str:
    return json.dumps({"request_id": request_id, "action": action, **fields})
