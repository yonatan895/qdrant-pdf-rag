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
import contextlib
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace

import anyio
import httpx2
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, ConfigDict, Field

from mainframe_rag.agent.answer import (
    HttpxLLMClient,
    PromptBudgetExceeded,
    TruncatedStreamError,
    assert_reasoning_model,
    build_chat_messages,
    build_messages,
    classify_query_complexity,
    truncation_alert_detail,
)
from mainframe_rag.agent.answer_core import (
    AnswerCoreDeps,
    AnswerCoreInput,
    AnswerCoreOutput,
    LLMChatError,
    execute_answer_core,
    execute_answer_core_stream,
    resolve_search_query,
)
from mainframe_rag.agent.chat_turn import InvalidChatTurn, PreparedChatTurn, prepare_chat_turn
from mainframe_rag.agent.core_ports import RetrievalResult
from mainframe_rag.agent.metrics import endpoint_for_path, record_request, setup_metrics
from mainframe_rag.agent.model_adapter import ModelAdapter
from mainframe_rag.agent.serving import ServingGate, ServingGeneration
from mainframe_rag.agent.sse import (
    empty_final_payload,
    error_payload,
    final_payload,
    format_openai_chunk,
    format_openai_done,
    format_openai_error,
    format_sse_event,
)
from mainframe_rag.agent.tokenizer import build_tokenizer
from mainframe_rag.agent.zowe_mcp import build_zowe_mcp, probe_zowe_mcp
from mainframe_rag.config import Settings, bearer_auth_headers, load_settings
from mainframe_rag.ingest.embed import build_embedder
from mainframe_rag.ingest.representation import require_attested_revision
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.logs import configure_logging, error_type
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
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.retrieve.query import async_search as retrieve_search
from mainframe_rag.retrieve.rerank import build_reranker, probe_reranker
from mainframe_rag.tracing import (
    bind_module_tracer,
    parent_context,
    setup_tracing,
    shutdown_tracing,
    start_span,
    use_span,
)
from mainframe_rag.webui.routes import router as webui_router

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
# Serving-generation gate (issues #391 F3/F4): created in lifespan from
# Settings, or injected by tests before startup (never overwritten then).
serving_gate: ServingGate | None = None
# Tracer starts as the API proxy (no-op until a real provider is installed).
# Lifespan reassigns it when tracing is enabled (issue #83); tests swap it
# directly with a tracer backed by InMemorySpanExporter.
tracer: trace.Tracer = trace.get_tracer("mainframe-rag.agent")
bind_module_tracer(__name__, "tracer", "mainframe-rag.agent")


def _span_error(span: trace.Span, exc: Exception) -> None:
    """Record a failure on the active span. Observability only — never on
    the client response path (export errors surface in logs, if at all).
    Type only (issue #529 OBS-1A): the SDK's record_exception would export
    the exception message and stack text, which routinely carry query text,
    URLs, or file paths."""
    span.add_event("exception", {"exception.type": error_type(exc)})
    span.set_status(Status(StatusCode.ERROR, error_type(exc)))


class _RequestSpan:
    """Own a request root until completion or transfer to its response body."""

    def __init__(self, request, span, endpoint, started):
        self.request = request
        self.span = span
        self.endpoint = endpoint
        self.started = started
        self.streaming = False
        self.ended = False
        self.query_class = "unknown"
        self.hits = None

    def end(self):
        if not self.ended:
            self.ended = True
            self.span.end()

    def abort(self):
        if getattr(self.request.state, "red_recorded", False):
            return
        request_id = getattr(self.request.state, "request_id", "unknown")
        if self.streaming:
            _record_stream_abort(
                self.request,
                request_id,
                self.endpoint,
                self.started,
                self.query_class,
                self.hits,
                self.span,
            )
        else:
            self.span.set_attribute("rag.request_aborted", True)
            log.warning(json_log(request_id, self.endpoint, outcome="client_disconnect"))
            _record_endpoint(self.request, self.endpoint, "client_disconnect", self.started)

    def stream(self, source, *, query_class="unknown", hits=None, **kwargs):
        response = _RequestStreamingResponse(_SpanStream(source, self), **kwargs)
        self.query_class = query_class
        self.hits = hits
        self.streaming = True
        return response


class _SpanStream:
    """Finalize the root and owned iterator on exhaustion, cancellation or close."""

    def __init__(self, source, owner):
        self.source = source
        self.owner = owner
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        with use_span(self.owner.span, end_on_exit=False):
            try:
                return await self.source.__anext__()
            except BaseException:
                await self.aclose()
                raise

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        with use_span(self.owner.span, end_on_exit=False):
            with anyio.CancelScope(shield=True):
                try:
                    await self.source.aclose()
                finally:
                    try:
                        self.owner.abort()
                    finally:
                        self.owner.end()


class _RequestStreamingResponse(StreamingResponse):
    async def stream_response(self, send):
        try:
            await super().stream_response(send)
        finally:
            await self.body_iterator.aclose()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.body_iterator.aclose()


@contextlib.contextmanager
def _request_span(request, span, endpoint, started):
    owner = _RequestSpan(request, span, endpoint, started)
    with use_span(span, end_on_exit=False):
        try:
            yield owner
        except asyncio.CancelledError, GeneratorExit:
            owner.abort()
            raise
        except AppError as exc:
            if not getattr(request.state, "red_recorded", False):
                if exc.status >= 500:
                    _span_error(span, exc)
                log.warning(
                    json_log(
                        request.state.request_id, endpoint, error=error_type(exc), code=exc.code
                    )
                )
            raise
        except Exception as exc:
            if not getattr(request.state, "red_recorded", False):
                _span_error(span, exc)
                event = json_log(request.state.request_id, "unhandled", error=error_type(exc))
                log.exception(event)
                request.state.failure_logged = True
            raise
        finally:
            if not owner.streaming:
                owner.end()

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


def prepare_chat_request(
    request_id: str, messages: list[ChatMessage], splunk_context: str | None = None
) -> PreparedChatTurn:
    """Map the common chat-input validation to the API/console error contract."""
    try:
        return prepare_chat_turn(messages, settings, splunk_context)
    except InvalidChatTurn as exc:
        log.warning(json_log(request_id, "invalid_chat_turn", reason=error_type(exc)))
        raise AppError(422, "invalid_request", "request body failed validation") from exc


async def _await_retrieval(
    res: tuple[list[SearchHit], str, dict[str, int]]
    | Awaitable[tuple[list[SearchHit], str, dict[str, int]]],
) -> tuple[list[SearchHit], str, dict[str, int]]:
    """Sync/async retrieval-leg shim: the pooled async client awaits while
    sync test doubles resolve inline — one helper serves both endpoints so
    the twin call sites cannot diverge (review S2)."""
    if inspect.isawaitable(res):
        return await res
    return res


def core_deps() -> AnswerCoreDeps:
    """Build the shared-engine dependency bag from the module globals at call
    time, so tests that monkeypatch app_mod (llm, retrieve_search,
    build_messages) drive the core through the same seam as production, and
    the operator console reuses the identical retrieval/LLM wiring."""
    client, embedding, ranking, retrieve_fn = qdrant, embedder, reranker, retrieve_search

    async def retrieve(
        query: str, *, product: str | None, version: str | None, settings: Settings
    ) -> RetrievalResult:
        hits, kind, timings = await _await_retrieval(retrieve_fn(
            client, embedding, settings.qdrant_collection, query,
            product=product, version=version, limit=8, settings=settings, reranker=ranking,
        ))
        return RetrievalResult(hits, kind, timings)

    return AnswerCoreDeps(
        settings=settings,
        llm=ModelAdapter(llm),
        retrieve=retrieve,
        tokenizer=tokenizer,
        build_messages_fn=build_messages,
        build_chat_messages_fn=build_chat_messages,
        classify_query_complexity_fn=classify_query_complexity,
    )


# One fixed refusal for every non-servable generation state; the outcome and
# physical name go to the log only (error contract: stable code + message,
# never internals).
_REPRESENTATION_UNAVAILABLE = "the retrieval generation is not available"


async def serving_settings() -> Settings:
    """The one serving boundary (issues #391 F3/F4): resolve the configured
    alias through the TTL-cached gate and return settings bound to the
    validated physical collection. Raises the stable 503 before any
    retrieval or stream opens — an unverified or incompatible generation is
    never queried, and the physical name means an alias swap cannot redirect
    this request."""
    assert serving_gate is not None, "lifespan must initialize the serving gate"
    try:
        generation = await serving_gate.generation(qdrant, settings, extraction_rules_version())
    except Exception as exc:
        log.error(
            json_log("serving", "representation_unavailable", error_type=type(exc).__name__)
        )
        raise AppError(503, "representation_unavailable", _REPRESENTATION_UNAVAILABLE) from exc
    if not generation.servable:
        log.warning(
            json_log(
                "serving",
                "representation_unavailable",
                outcome=generation.outcome,
                physical=generation.physical or "",
            )
        )
        raise AppError(503, "representation_unavailable", _REPRESENTATION_UNAVAILABLE)
    assert generation.physical is not None
    return settings.model_copy(update={"qdrant_collection": generation.physical})


async def serving_deps() -> AnswerCoreDeps:
    """Shared answer-core deps bound to the validated physical generation —
    the one gate for /v1/answer, /v1/chat*, and the operator console."""
    return replace(core_deps(), settings=await serving_settings())


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
    }


def _answer_span_attrs(
    kind: str,
    hits: list[SearchHit],
    citations: int,
    has_script: bool,
    evidence: int = 0,
) -> dict:
    return {
        "rag.query_kind": kind,
        "rag.hits": len(hits),
        # Supplied excerpts (issue #364): counts only, never cite text. The
        # gap between rag.hits and rag.evidence is packing/trim omission.
        # Per-document identifiers stay out of span attributes (issue #529
        # OBS-1A); request_id joins logs to traces.
        "rag.evidence": evidence,
        "rag.citations": citations,
        "rag.has_script": has_script,
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
    evidence: int = 0,
    inline_bracket_present: bool = False,
    citations_header_present: bool = False,
    cites_rejected_shape_bad: int = 0,
    cites_rejected_unmapped: int = 0,
    verification_state: str = "unverified_draft",
    budget_verified: bool = False,
    units_omitted: int = 0,
) -> dict:
    """Answer-leg log fields shared by the JSON and SSE finals: identical
    keys so log consumers see one shape; stream=True only marks the SSE one.
    The citation-attempt counters (issue #299) let the eval split zero-cite
    rows into malformed vs fabricated vs never-attempted without putting
    model output on the wire. `evidence` is the supplied-excerpt count from
    the final prompt manifest (issue #364) — counts only, never text.
    `verification_state` (issue #365) is the finalized label, so log joins
    can split accepted vs draft vs incomplete answers without re-deriving.
    `budget_verified` (issue #368) marks remote-tokenizer-confirmed window
    compliance; `units_omitted` counts whole atomic units dropped by packing
    across packed excerpts, so truncation depth is countable from logs."""
    fields: dict = {
        "query_kind": kind,
        "query_complexity": complexity,
        "hits": len(hits),
        "evidence": evidence,
        "embed_ms": timings.get("embed_ms"),
        "qdrant_ms": timings.get("qdrant_ms"),
        "rerank_ms": timings.get("rerank_ms"),
        "llm_ms": llm_ms,
        "citations": citations,
        "has_script": has_script,
        "finish_reason": finish_reason,
        "verification_state": verification_state,
        "budget_verified": budget_verified,
        "units_omitted": units_omitted,
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


def _record_stream_abort(
    request: Request,
    request_id: str,
    endpoint: str,
    started: float,
    kind: str,
    hits: int | None,
    span: trace.Span,
) -> None:
    """Client disconnect or cancellation before a terminal frame (issue #365):
    partial tokens already left the server as provisional output, so the only
    honest state is `generation_incomplete`. No frame can follow a disconnect;
    the countable signals are the alert log and the RED outcome. Counts only —
    never response text."""
    span.set_attribute("rag.stream_aborted", True)
    log.warning(
        json_log(
            request_id,
            "answer_alert",
            alert="client_disconnect",
            endpoint=endpoint,
            verification_state="generation_incomplete",
        )
    )
    _record_endpoint(
        request,
        endpoint,
        "client_disconnect",
        started,
        query_class=kind,
        hits=hits,
        verification_state="generation_incomplete",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global settings, http, http_sync, qdrant, embedder, llm, tokenizer, reranker, zowe_mcp
    global serving_gate
    settings = load_settings()
    configure_logging(settings.log_level)
    # Startup fail-fast (issue #20 PR D): the agent refuses to listen on a
    # misconfigured embed path rather than failing per-request. Hash mode is
    # CI/dev only and must be explicitly allowed.
    if settings.embed_mode not in ("hash", "vllm"):
        raise RuntimeError(f"EMBED_MODE={settings.embed_mode!r} is not one of hash|vllm")
    if settings.embed_mode == "hash" and not settings.allow_hash_mode:
        raise RuntimeError(
            "EMBED_MODE=hash is CI/dev only; set ALLOW_HASH_MODE=true (CI overlay) to allow it"
        )
    if settings.embed_mode == "vllm":
        settings.require_dense_dim()
        settings.require_embed()
        # Operator attestation (issue #362 req 3): a mutable gateway alias
        # is not a model identity. Fail fast here (config error, before any
        # client is built) like every other embed-path misconfiguration.
        require_attested_revision(settings)
    http_limits = httpx2.Limits(
        max_keepalive_connections=settings.http_max_keepalive_connections,
        max_connections=settings.http_max_connections,
    )
    http_client = httpx2.AsyncClient(
        timeout=settings.embed_timeout_s,
        transport=httpx2.AsyncHTTPTransport(retries=settings.http_connect_retries),
        limits=http_limits,
    )
    http = http_client
    # Sync pool for the retrieval leg (embedder / tokenizer / reranker): the
    # Embedder/Reranker/Tokenizer protocols are sync, so their calls run
    # inside asyncio.to_thread off the event loop. Bounded limits like the
    # async pool; closed on shutdown. One pool on purpose — same shape as the
    # pre-async stack (review S4).
    http_sync_client = httpx2.Client(
        timeout=settings.embed_timeout_s,
        transport=httpx2.HTTPTransport(retries=settings.http_connect_retries),
        limits=http_limits,
    )
    http_sync = http_sync_client
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

    qdrant_client_inst = qdrant_client.AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
        limits=http_limits,
    )
    qdrant = qdrant_client_inst
    # Serving-generation gate (issues #391 F3/F4): one instance per process,
    # created from Settings unless a test injected its own (never overwritten
    # then). The cache is invalidated at every startup so a validation from a
    # previous lifespan can never leak into this one.
    if serving_gate is None:
        serving_gate = ServingGate(settings.representation_cache_ttl_s)
    else:
        serving_gate.invalidate()
    # Startup gate: refuse to listen when the RESOLVED physical generation is
    # known-incompatible (drift, legacy, or a pending migration — issue #391
    # F2). An unreachable store reports unknown and the process starts; every
    # request still passes the same gate, so an unverifiable state is refused
    # (503) rather than served (F3). /healthz re-evaluates per scrape.
    try:
        generation = await serving_gate.generation(
            qdrant, settings, extraction_rules_version(), fresh=True
        )
    except Exception as exc:  # noqa: BLE001 — exotic transports report unknown
        generation = ServingGeneration(None, "unknown", (type(exc).__name__,))
    if generation.outcome in ("reembed_required", "legacy", "pending"):
        raise RuntimeError(
            f"agent refuses a {generation.outcome} collection "
            f"{settings.qdrant_collection!r} "
            f"({', '.join(generation.details) or 'no contract'}): "
            "re-run ingest with --reingest under these settings to re-embed, then restart "
            "(never serve queries against incompatible vectors)."
        )
    if generation.outcome in ("record_only_drift", "unknown"):
        log.warning(
            json_log(
                "lifespan",
                "representation_not_proven",
                outcome=generation.outcome,
                details=",".join(generation.details),
            )
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
    if hasattr(http_client, "aclose"):
        await http_client.aclose()
    elif hasattr(http_client, "close"):
        http_client.close()

    http_sync_client.close()

    if hasattr(llm_client, "aclose"):
        await llm_client.aclose()
    elif hasattr(llm_client, "close"):
        llm_client.close()

    if hasattr(qdrant_client_inst, "close"):
        close_res = qdrant_client_inst.close()
        if inspect.isawaitable(close_res):
            await close_res

    if zowe_mcp is not None and hasattr(zowe_mcp, "close"):
        zowe_mcp.close()


app = FastAPI(title="mainframe-rag agent", version="0.1.0", lifespan=lifespan)
# ADR-0004 operator console: same process, same image, same Route. The router
# fails closed (stable 404 envelope) while Settings.ui_enabled is False.
app.include_router(webui_router)


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
    # Optional per-request temperature override (issue #596): None keeps the
    # configured Settings.llm_temperature (production default 0.2). The
    # answer eval pins 0 for comparison stability; production callers omit it.
    # Bounded like Settings.llm_temperature so client input errors 422 before
    # retrieval instead of surfacing as upstream 502s.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0, allow_inf_nan=False)


class AnswerResponse(BaseModel):
    request_id: str
    answer: str
    citations: list[str]
    # Provenance (issue #269): true when every citation was mapped from bare
    # bracket markers with no explicit citation line — surfaced so clients
    # and the eval never mistake inferred provenance for grounding.
    citations_inferred: bool = False
    # Which supplied [n] prompt labels the inferred citations came from,
    # 1-based (issues #299/#364): the bool says the cites were inferred, this
    # says from where, so right-doc/wrong-index is measurable. Labels come
    # from the final evidence manifest, never the retrieval list. Empty on
    # every other path, so the schema is identical on JSON and SSE.
    inferred_indices: list[int] = Field(default_factory=list)
    script: str | None
    # Language tag of the extracted script fence (issue #336), None when
    # no script was extracted.
    script_lang: str | None = None
    # Verification state (issue #365): insufficient_evidence |
    # unverified_draft | generation_incomplete | accepted. Additive with a
    # closed default (never `accepted`): every route passes the
    # core-computed label; direct constructions stay non-accepted.
    verification_state: str = "unverified_draft"
    # True whenever a script fence was extracted (issue #365): scripts pass
    # through unvalidated, so a surfaced script is a human-review-required
    # draft, never certified-executable guidance.
    script_review_required: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = None
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0.0, le=2.0, allow_inf_nan=False)
    max_tokens: int | None = Field(
        default=None,
        description="OpenAI-compat parameter; token limits are managed server-side by the reasoning profile.",
    )
    splunk_context: str | None = None
    product: str | None = None
    version: str | None = None


ChatCompletionsRequest = ChatRequest


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
    # Verification state (issue #365): same vocabulary as AnswerResponse.
    verification_state: str = "unverified_draft"
    # Scripts ride chat too (issue #365): previously dropped on both chat
    # paths, now surfaced with the review-required flag, like answers.
    script: str | None = None
    script_lang: str | None = None
    script_review_required: bool = False


ChatResponse = ChatCompletionsResponse


class HealthzResponse(BaseModel):
    status: str = "ok"
    qdrant: bool
    embed: bool | None = None
    # Stored-representation readiness (issue #362): compatible |
    # record_only_drift | empty (servable) vs reembed_required | legacy |
    # unknown (degraded — smoke.sh fails closed on degraded). Lifespan
    # refuses the hard cases at startup; this is the live per-scrape
    # signal for stores that change under a running agent.
    representation: str | None = None


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
    if endpoint is None or (endpoint == "console" and not settings.ui_enabled):
        return
    started = getattr(request.state, "started", None)
    elapsed = time.monotonic() - started if started is not None else 0.0
    request.state.red_recorded = True
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
    verification_state: str | None = None,
) -> None:
    """RED record for endpoint-leg outcomes (success and raised errors).
    Marks the request so the error handler does not double-count the
    AppError that follows a recorded raise."""
    if getattr(request.state, "red_recorded", False):
        return
    request.state.red_recorded = True
    record_request(
        endpoint,
        outcome,
        query_class=query_class,
        elapsed_s=time.monotonic() - started,
        hits=hits,
        ttft_ms=ttft_ms,
        llm_model=llm_model,
        verification_state=verification_state,
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    _record_handler_error(request, exc.code)
    return JSONResponse(
        status_code=exc.status,
        content=ErrorEnvelope(code=exc.code, message=exc.message).model_dump(),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # Fixed message on purpose: nothing in src/ raises HTTPException, this
    # only fires from framework internals, and exc.detail must never reach a
    # client body (the "no internals" rule is structural, not incidental).
    _record_handler_error(request, "http_error")
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorEnvelope(code="http_error", message="request failed").model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    _record_handler_error(request, "invalid_request")
    return JSONResponse(
        status_code=422,
        content=ErrorEnvelope(
            code="invalid_request", message="request body failed validation"
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full trace stays in server logs; the client never sees internals.
    _record_handler_error(request, "internal")
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        _span_error(span, exc)
    request_id = getattr(request.state, "request_id", "unknown")
    if not getattr(request.state, "failure_logged", False):
        log.exception(json_log(request_id, "unhandled", error=error_type(exc)))
    return JSONResponse(
        status_code=500,
        content=ErrorEnvelope(code="internal", message="internal error").model_dump(),
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
        log.warning(json_log("metrics", "scrape_failed", error=error_type(exc)))
        raise AppError(503, "metrics_unavailable", "metrics are not available") from exc
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)


async def evaluate_healthz() -> tuple[HealthzResponse, int]:
    """Readiness evaluation shared by GET /healthz and the console badge:
    returns (body, HTTP status). A degraded body is an HTTP failure (issue
    #391 F3) — Kubernetes HTTP probes treat 200-399 as success, so the
    previous degraded-but-200 label never made a pod unready. `empty` stays
    ready on purpose: the deploy -> ingest sequence waits for the agent
    before any data exists, and requests are still refused by the serving
    gate while empty (bootstrap must not deadlock)."""
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
            # Upstream response bodies stay out of logs and client bodies
            # alike (issue #529 OBS-1A); the status code is the diagnosis.
            log.warning(json_log("healthz", "health", qdrant_status=resp.status_code))
    except Exception as exc:
        log.warning(json_log("healthz", "health", error=error_type(exc)))
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
            log.warning(json_log("healthz", "health", embed_error=error_type(exc)))

    # Readiness is the live, uncached evaluation (F4): probe scrapes must see
    # an alias rollback or a degraded contract immediately, not a cached
    # generation. Request paths keep the TTL cache for cost.
    representation = "unknown"
    try:
        assert serving_gate is not None, "lifespan must initialize the serving gate"
        generation = await serving_gate.generation(
            qdrant, settings, extraction_rules_version(), fresh=True
        )
        representation = generation.outcome
    except Exception as exc:  # noqa: BLE001 — exotic doubles report unknown
        log.warning(json_log("healthz", "health", representation_error=type(exc).__name__))

    representation_ok = representation in ("compatible", "record_only_drift", "empty")
    status = "ok" if qdrant_ok and embed_ok is not False and representation_ok else "degraded"
    return (
        HealthzResponse(
            status=status, qdrant=qdrant_ok, embed=embed_ok, representation=representation
        ),
        200 if status == "ok" else 503,
    )


@app.get("/healthz", response_model=HealthzResponse)
async def healthz(response: Response) -> HealthzResponse:
    body, code = await evaluate_healthz()
    response.status_code = code
    return body


@app.get("/livez")
async def livez() -> dict[str, str]:
    """Process liveness (issue #391 F3): always 200 while the event loop is
    serving. Data-serving readiness lives in /healthz — a non-servable
    generation must never restart an otherwise healthy process, so the
    livenessProbe points here and the readinessProbe at /healthz."""
    return {"status": "alive"}


@app.post("/v1/search", response_model=SearchResponse)
async def v1_search(request: Request, req: SearchRequest, response: Response) -> SearchResponse:
    request_id = request.state.request_id
    started = time.monotonic()
    # One SERVER span per request (issue #529 OBS-1B §4.3): created before
    # the fail-fast gates so length rejections and serving refusals are
    # covered by the same trace as admitted work.
    root_span = start_span(
        tracer,
        "v1.search",
        context=parent_context(request.headers),
        kind=SpanKind.SERVER,
        attributes={"http.request_id": request_id, "rag.limit": req.limit},
    )
    with _request_span(request, root_span, "search", started) as owner:
        return await _search_response(req, response, owner)


async def _search_response(req, response, owner):
    request = owner.request
    root_span = owner.span
    request_id = request.state.request_id
    started = owner.started
    _require_query_length(request_id, req.query)
    # Gate before any retrieval work (issue #391 F3/F4): 503 when the
    # resolved generation is not validated; otherwise bind to its physical.
    bound = await serving_settings()
    with use_span(root_span, end_on_exit=False):
        try:
            res = retrieve_search(
                qdrant,
                embedder,
                bound.qdrant_collection,
                req.query,
                product=req.product,
                version=req.version,
                limit=req.limit,
                settings=bound,
                reranker=reranker,
            )
            hits, kind, timings = await _await_retrieval(res)
        except Exception as exc:
            _span_error(root_span, exc)
            _record_endpoint(request, "search", "upstream_error", started)
            log.error(json_log(request_id, "search", error=error_type(exc)))
            raise AppError(502, "upstream_error", "retrieval failed") from exc
        root_span.set_attributes(_search_span_attrs(kind, hits))
        log.info(
            json_log(
                request_id,
                "search",
                query_kind=kind,
                hits=len(hits),
                embed_ms=timings.get("embed_ms"),
                qdrant_ms=timings.get("qdrant_ms"),
                rerank_ms=timings.get("rerank_ms"),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        )
    timing_parts = _timing_parts(timings)
    if timing_parts:
        response.headers["Server-Timing"] = ", ".join(timing_parts)
    _record_endpoint(request, "search", "ok", started, query_class=kind, hits=len(hits))
    return SearchResponse(
        request_id=request_id,
        query_kind=kind,
        hits=hits,
    )


# Orchestration shared by /v1/answer and /v1/chat (issue #583). Each helper
# owns one step both routes perform identically; the endpoint label and the
# log action names are the only per-route parameters, so a gate, terminal
# frame or observation cannot be fixed on one route and forgotten on the
# other. Step order inside the routes is unchanged.


def _require_reasoning_model(request_id: str, action: str) -> None:
    """Fail fast before any retrieval: the reasoning model (and its endpoint)
    must be configured; nothing else is callable. Config errors get a fixed
    client message; the exception text stays in the log. The error handler
    records the single terminal observation."""
    try:
        assert_reasoning_model(settings)
    except RuntimeError as exc:
        log.warning(json_log(request_id, action, error=error_type(exc)))
        raise AppError(503, "not_configured", "reasoning model is not configured") from exc


def _retrieval_failed(owner: _RequestSpan, endpoint: str, action: str, exc: Exception) -> AppError:
    """Terminal observation for a failed retrieval leg: the same fault maps to
    the same code+message on every endpoint ("retrieval failed", as on
    /v1/search). The caller raises the returned error from `exc`."""
    _span_error(owner.span, exc)
    _record_endpoint(owner.request, endpoint, "upstream_error", owner.started)
    log.error(json_log(owner.request.state.request_id, action, error=error_type(exc)))
    return AppError(502, "upstream_error", "retrieval failed")


def _set_server_timing(headers, timings: dict, **llm_legs: int | None) -> None:
    """Server-Timing on a response or SSE header mapping, when any leg ran."""
    parts = _timing_parts(timings, **llm_legs)
    if parts:
        headers["Server-Timing"] = ", ".join(parts)


async def _execute_core_or_raise(
    core_input: AnswerCoreInput,
    deps: AnswerCoreDeps,
    owner: _RequestSpan,
    endpoint: str,
    action: str,
    kind: str,
    hit_count: int,
):
    """Run the shared core for a JSON response. A model failure maps to
    "answer failed" (never a retrieval code); an irreducible prompt-budget
    overflow maps to the explicit 422 budget contract (issue #368) — never
    silent, never a model call; a prompt-build failure stays an internal 500."""
    root_span = owner.span
    request_id = owner.request.state.request_id
    try:
        return await execute_answer_core(core_input, deps, parent_span=root_span)
    except PromptBudgetExceeded as exc:
        _span_error(root_span, exc)
        with use_span(root_span, end_on_exit=False):
            log.warning(json_log(request_id, action, error=error_type(exc)))
        _record_endpoint(
            owner.request,
            endpoint,
            "prompt_budget_exceeded",
            owner.started,
            query_class=kind,
            hits=hit_count,
        )
        raise AppError(
            422, "prompt_budget_exceeded", "prompt exceeds the model token budget"
        ) from exc
    except LLMChatError as exc:
        _span_error(root_span, exc.original)
        with use_span(root_span, end_on_exit=False):
            log.error(json_log(request_id, action, error=error_type(exc)))
        _record_endpoint(
            owner.request,
            endpoint,
            "upstream_error",
            owner.started,
            query_class=kind,
            hits=hit_count,
        )
        raise AppError(502, "upstream_error", "answer failed") from exc


def _output_log_fields(
    output: AnswerCoreOutput, kind: str, timings: dict, started: float, *, stream: bool
) -> dict:
    """Answer-leg log fields of a finalized core output: one shape for the
    JSON and SSE finals of both routes."""
    return _answer_log_fields(
        kind,
        output.complexity,
        output.hits,
        timings,
        len(output.citations),
        output.script is not None,
        output.finish_reason,
        output.usage,
        output.llm_ms,
        output.ttft_ms,
        started,
        stream=stream,
        evidence=output.evidence.supplied_count,
        inline_bracket_present=output.parsed.inline_bracket_present,
        citations_header_present=output.parsed.citations_header_present,
        cites_rejected_shape_bad=output.parsed.cites_rejected_shape_bad,
        cites_rejected_unmapped=output.parsed.cites_rejected_unmapped,
        verification_state=output.verification_state,
        budget_verified=output.budget_verified,
        units_omitted=output.evidence.units_omitted,
    )


def _record_no_hits(
    owner: _RequestSpan, endpoint: str, kind: str, output: AnswerCoreOutput
) -> None:
    """RED record of the no-evidence terminal (nothing was retrieved)."""
    _record_endpoint(
        owner.request,
        endpoint,
        "ok",
        owner.started,
        query_class=kind,
        hits=0,
        verification_state=output.verification_state,
    )


def _record_answered(
    owner: _RequestSpan, endpoint: str, kind: str, output: AnswerCoreOutput, llm_model: str
) -> None:
    """RED record of a successfully finalized answer."""
    _record_endpoint(
        owner.request,
        endpoint,
        "ok",
        owner.started,
        query_class=kind,
        hits=len(output.hits),
        ttft_ms=output.ttft_ms,
        llm_model=llm_model,
        verification_state=output.verification_state,
    )


def _record_stream_failure(
    owner: _RequestSpan, endpoint: str, action: str, kind: str, hit_count: int, exc: Exception
) -> None:
    """Terminal observation of a stream that failed after headers were sent.
    The wire frame is the caller's (it is the only per-route part): the
    fault is labeled budget for a pre-token prompt-budget overflow, else
    upstream. Truncation observability: the partial prefix already went out
    as token events, so the answer_alert carries the fixed reason label
    only — never response text or the exception body."""
    request_id = owner.request.state.request_id
    _span_error(owner.span, exc)
    budget = isinstance(exc, PromptBudgetExceeded)
    if isinstance(exc, TruncatedStreamError):
        log.warning(
            json_log(
                request_id,
                "answer_alert",
                alert="stream_truncated",
                detail=truncation_alert_detail(exc),
            )
        )
    _record_endpoint(
        owner.request,
        endpoint,
        "prompt_budget_exceeded" if budget else "upstream_error",
        owner.started,
        query_class=kind,
        hits=hit_count,
        verification_state="generation_incomplete",
    )
    (log.warning if budget else log.error)(json_log(request_id, action, error=error_type(exc)))


async def _stream_answer_core(
    core_input: AnswerCoreInput,
    deps: AnswerCoreDeps,
    owner: _RequestSpan,
    *,
    endpoint: str,
    action: str,
    kind: str,
    hit_count: int,
    token_frame: Callable[[str], str],
    final_frames: Callable[[AnswerCoreOutput], Iterator[str]],
    error_frames: Callable[[], Iterable[str]],
) -> AsyncIterator[str]:
    """The one SSE generator behind /v1/answer and /v1/chat. The route
    supplies only the wire mapping (token frame, the final frames with their
    side effects, the terminal error frames); the core loop, the terminal
    failure observation and the core close are shared.

    A final or error frame is terminal. If the generator is closed before
    one is produced, the client saw only provisional tokens: the root-span
    owner records the abort as generation_incomplete (issue #365). A handled
    error counts as terminal — its frame already carries the incomplete
    state."""
    core_events = execute_answer_core_stream(core_input, deps, parent_span=owner.span)
    try:
        async for item in core_events:
            if item["type"] == "token":
                delta = item["delta"]
                if delta:
                    yield token_frame(delta)
            elif item["type"] == "final":
                for frame in final_frames(item["output"]):
                    yield frame
    except Exception as exc:  # noqa: BLE001 — streaming SSE generator traps upstream error
        _record_stream_failure(owner, endpoint, action, kind, hit_count, exc)
        for frame in error_frames():
            yield frame
    finally:
        await core_events.aclose()


def _answer_final_frames(
    owner: _RequestSpan, kind: str, timings: dict, llm_model: str, output: AnswerCoreOutput
) -> Iterator[str]:
    """The `final` frame of an answer stream, with its terminal side effects
    (log, span attributes, RED record) performed in their fixed order."""
    root_span = owner.span
    request_id = owner.request.state.request_id
    if not output.hits:
        root_span.set_attributes({"rag.query_kind": kind, "rag.hits": 0})
        _record_no_hits(owner, "answer", kind, output)
        log.info(
            json_log(
                request_id, "answer", query_kind=kind, hits=0, rerank_ms=timings.get("rerank_ms")
            )
        )
        yield format_sse_event(
            "final",
            empty_final_payload(
                request_id,
                output.answer,
                kind,
                verification_state=output.verification_state,
                script_review_required=output.script_review_required,
            ),
        )
        return

    _alert_finish_reason_non_stop(request_id, output.finish_reason)

    log.info(
        json_log(
            request_id,
            "answer",
            **_output_log_fields(output, kind, timings, owner.started, stream=True),
        )
    )

    final = final_payload(
        request_id,
        output.answer,
        output.citations,
        output.citations_inferred,
        output.script,
        kind,
        output.hits,
        output.finish_reason,
        output.ttft_ms,
        output.usage,
        inferred_indices=output.inferred_indices,
        script_lang=output.script_lang,
        verification_state=output.verification_state,
        script_review_required=output.script_review_required,
    )
    root_span.set_attributes(
        _answer_span_attrs(
            kind,
            output.hits,
            len(output.citations),
            output.script is not None,
            evidence=output.evidence.supplied_count,
        )
    )
    _record_answered(owner, "answer", kind, output, llm_model)
    yield format_sse_event("final", final)


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
    # One SERVER span per request (issue #83, #529 OBS-1B §4.3): the root
    # span starts before the cheap fail-fast gates and lives until the
    # response body is produced, so early rejections share the admitted
    # trace. For SSE it is ended inside the generator so the LLM stage (the
    # longest leg) is a child of the same trace, not a detached one.
    root_span = start_span(
        tracer,
        "v1.answer",
        context=parent_context(request.headers),
        kind=SpanKind.SERVER,
        attributes={"http.request_id": request_id, "rag.stream": is_stream},
    )
    with _request_span(request, root_span, "answer", started) as owner:
        return await _answer_response(req, response, is_stream, owner)


async def _answer_response(req, response, is_stream, owner):
    request = owner.request
    root_span = owner.span
    request_id = request.state.request_id
    started = owner.started
    _require_query_length(request_id, req.query)
    _require_reasoning_model(request_id, "answer")
    # Serving gate before retrieval and before the root stream opens (issue
    # #391 F3/F4): the request binds to the validated physical generation.
    bound = await serving_settings()
    llm_model = settings.require_reasoning_model()

    # Retrieval and LLM legs are guarded separately: the same fault must map
    # to the same code+message on every endpoint — a retrieval failure reads
    # "retrieval failed" here exactly as it does on /v1/search, and a model or
    # parse failure must not be mislabeled as a retrieval fault (AGENTS rule 2).
    try:
        # retrieve.* stage spans must land under this request's trace, so the
        # root is made current for the retrieval leg (the root span itself is
        # not created "as current" — the SSE generator outlives this block).
        with use_span(root_span, end_on_exit=False):
            res = retrieve_search(
                qdrant,
                embedder,
                bound.qdrant_collection,
                req.query,
                product=req.product,
                version=req.version,
                limit=8,
                settings=bound,
                reranker=reranker,
            )
            hits, kind, timings = await _await_retrieval(res)
    except Exception as exc:
        raise _retrieval_failed(owner, "answer", "answer", exc) from exc

    core_input = AnswerCoreInput(
        query=req.query,
        product=req.product,
        version=req.version,
        splunk_context=req.splunk_context,
        stream=is_stream,
        temperature=req.temperature,
        request_id=request_id,
        is_chat=False,
        hits=hits,
        query_kind=kind,
        timings=timings,
    )
    deps = replace(core_deps(), settings=bound)

    if not is_stream:
        output = await _execute_core_or_raise(
            core_input, deps, owner, "answer", "answer", kind, len(hits)
        )

        _alert_finish_reason_non_stop(request_id, output.finish_reason)

        if not output.hits:
            _record_no_hits(owner, "answer", kind, output)
            _set_server_timing(response.headers, timings)
            with use_span(root_span, end_on_exit=False):
                root_span.set_attributes({"rag.query_kind": kind, "rag.hits": 0})
                log.info(
                    json_log(
                        request_id,
                        "answer",
                        query_kind=kind,
                        hits=0,
                        rerank_ms=timings.get("rerank_ms"),
                    )
                )
            return AnswerResponse(
                request_id=request_id,
                answer=output.answer,
                citations=[],
                citations_inferred=False,
                inferred_indices=[],
                script=None,
                script_lang=None,
                verification_state=output.verification_state,
                script_review_required=output.script_review_required,
            )

        _set_server_timing(response.headers, timings, llm_ms=output.llm_ms, ttft_ms=output.ttft_ms)

        with use_span(root_span, end_on_exit=False):
            log.info(
                json_log(
                    request_id,
                    "answer",
                    **_output_log_fields(output, kind, timings, started, stream=False),
                )
            )
            root_span.set_attributes(
                _answer_span_attrs(
                    kind,
                    output.hits,
                    len(output.citations),
                    output.script is not None,
                    evidence=output.evidence.supplied_count,
                )
            )
        _record_answered(owner, "answer", kind, output, llm_model)
        return AnswerResponse(
            request_id=request_id,
            answer=output.answer,
            citations=output.citations,
            citations_inferred=output.citations_inferred,
            inferred_indices=output.inferred_indices,
            script=output.script,
            script_lang=output.script_lang,
            verification_state=output.verification_state,
            script_review_required=output.script_review_required,
        )

    # SSE streaming path
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    _set_server_timing(headers, timings)
    return owner.stream(
        _stream_answer_core(
            core_input,
            deps,
            owner,
            endpoint="answer",
            action="answer_stream",
            kind=kind,
            hit_count=len(hits),
            token_frame=lambda delta: format_sse_event(
                "token", {"type": "token", "delta": delta, "token": delta}
            ),
            final_frames=lambda output: _answer_final_frames(
                owner, kind, timings, llm_model, output
            ),
            error_frames=lambda: (format_sse_event("error", error_payload()),),
        ),
        query_class=kind,
        hits=len(hits),
        media_type="text/event-stream",
        headers=headers,
    )


@app.post("/v1/chat", response_model=None)
@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(req: ChatRequest, request: Request, response: Response):
    """Multi-turn chat completions: native POST /v1/chat and its
    OpenAI-compatible alias POST /v1/chat/completions.

    Supports non-streaming JSON and streaming SSE (OpenAI `data: {...}` chunks
    terminated by `data: [DONE]`). Follow-up turns condense only when
    CHAT_CONDENSE_ENABLED is on; retrieval, prompt assembly, reasoning
    generation, and turn-local citation validation all run through the shared
    answer core.
    """
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
    started = getattr(request.state, "started", time.monotonic())

    # One SERVER span per request (issue #529 OBS-1B §4.3): created before
    # turn validation and the fail-fast gates so rejections share the trace.
    root_span = start_span(
        tracer,
        "v1.chat",
        context=parent_context(request.headers),
        kind=SpanKind.SERVER,
        attributes={"http.request_id": request_id, "rag.stream": req.stream},
    )
    with _request_span(request, root_span, "chat", started) as owner:
        return await _chat_response(req, response, owner)


def _chat_final_frames(
    owner: _RequestSpan,
    chat_id: str,
    kind: str,
    timings: dict,
    llm_model: str,
    output: AnswerCoreOutput,
) -> Iterator[str]:
    """The terminal OpenAI chunks of a chat stream (citations delta, the
    finish chunk carrying the answer metadata, then [DONE]), with their
    terminal side effects performed in their fixed order."""
    root_span = owner.span
    request_id = owner.request.state.request_id
    if not output.hits:
        yield format_openai_chunk(chat_id, llm_model, delta_content=output.answer)
        extra_meta = {
            "citations": [],
            "citations_inferred": False,
            "inferred_indices": [],
            "hits": [],
            "verification_state": output.verification_state,
            "script": None,
            "script_lang": None,
            "script_review_required": output.script_review_required,
        }
        root_span.set_attributes({"rag.query_kind": kind, "rag.hits": 0})
        _record_no_hits(owner, "chat", kind, output)
        log.info(json_log(request_id, "chat", query_kind=kind, hits=0, stream=True))
        yield format_openai_chunk(chat_id, llm_model, finish_reason="stop", extra=extra_meta)
        yield format_openai_done()
        return

    if output.citations:
        cites_delta = "\n\n**Citations:**\n" + "\n".join(f"- {c}" for c in output.citations)
        yield format_openai_chunk(chat_id, llm_model, delta_content=cites_delta)

    extra_meta = {
        "citations": output.citations,
        "citations_inferred": output.citations_inferred,
        "inferred_indices": output.inferred_indices,
        "hits": [h.model_dump() for h in output.hits],
        "verification_state": output.verification_state,
        "script": output.script,
        "script_lang": output.script_lang,
        "script_review_required": output.script_review_required,
    }
    _record_answered(owner, "chat", kind, output, llm_model)
    log.info(
        json_log(
            request_id,
            "chat",
            **_output_log_fields(output, kind, timings, owner.started, stream=True),
        )
    )
    yield format_openai_chunk(
        chat_id, llm_model, finish_reason=output.finish_reason, extra=extra_meta
    )
    yield format_openai_done()


async def _chat_response(req, response, owner):
    request = owner.request
    root_span = owner.span
    request_id = request.state.request_id
    started = owner.started
    turn = prepare_chat_request(request_id, req.messages, req.splunk_context)
    _require_reasoning_model(request_id, "chat")
    # The response `model` reports what actually ran: inference is always
    # the reasoning model (issue #313), so the caller-supplied OpenAI-compat
    # field stays accepted-and-ignored, exactly like `max_tokens`.
    llm_model = settings.require_reasoning_model()

    is_stream = req.stream
    # Serving gate before the stream opens (issue #391 F3/F4): bind the
    # request to the validated physical generation or refuse with the
    # stable 503. The error handler records the single terminal
    # observation (same series the explicit record produced).
    deps = await serving_deps()

    core_input = AnswerCoreInput(
        query=turn.query,
        messages=turn.messages,
        product=req.product,
        version=req.version,
        splunk_context=req.splunk_context,
        stream=is_stream,
        temperature=req.temperature,
        request_id=request_id,
        is_chat=True,
    )

    try:
        with use_span(root_span, end_on_exit=False):
            search_query = await resolve_search_query(core_input, deps, parent_span=root_span)
            retrieval_coro = retrieve_search(
                qdrant,
                embedder,
                deps.settings.qdrant_collection,
                search_query,
                product=req.product,
                version=req.version,
                limit=8,
                settings=deps.settings,
                reranker=reranker,
            )
            hits, kind, timings = await _await_retrieval(retrieval_coro)
    except Exception as exc:
        raise _retrieval_failed(owner, "chat", "chat_retrieval", exc) from exc

    core_input.hits = hits
    core_input.query_kind = kind
    core_input.timings = timings

    if not is_stream:
        output = await _execute_core_or_raise(
            core_input, deps, owner, "chat", "chat_answer", kind, len(hits)
        )

        _alert_finish_reason_non_stop(request_id, output.finish_reason)

        if not output.hits:
            _record_no_hits(owner, "chat", kind, output)
            with use_span(root_span, end_on_exit=False):
                root_span.set_attributes({"rag.query_kind": kind, "rag.hits": 0})
                log.info(json_log(request_id, "chat", query_kind=kind, hits=0))
            return ChatCompletionsResponse(
                id=f"chatcmpl-{request_id}",
                created=int(time.time()),
                model=llm_model,
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        message=ChatMessageResponse(role="assistant", content=output.answer),
                        finish_reason="stop",
                    )
                ],
                usage=TokenUsage(),
                citations=[],
                citations_inferred=False,
                inferred_indices=[],
                hits=[],
                verification_state=output.verification_state,
                script=None,
                script_lang=None,
                script_review_required=output.script_review_required,
            )

        content = output.answer
        if output.citations:
            content += "\n\n**Citations:**\n" + "\n".join(f"- {c}" for c in output.citations)

        with use_span(root_span, end_on_exit=False):
            log.info(
                json_log(
                    request_id,
                    "chat",
                    **_output_log_fields(output, kind, timings, started, stream=False),
                )
            )
        _record_answered(owner, "chat", kind, output, llm_model)

        return ChatCompletionsResponse(
            id=f"chatcmpl-{request_id}",
            created=int(time.time()),
            model=llm_model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessageResponse(role="assistant", content=content),
                    finish_reason=output.finish_reason,
                )
            ],
            usage=output.usage,
            citations=output.citations,
            citations_inferred=output.citations_inferred,
            inferred_indices=output.inferred_indices,
            hits=output.hits,
            verification_state=output.verification_state,
            script=output.script,
            script_lang=output.script_lang,
            script_review_required=output.script_review_required,
        )

    chat_id = f"chatcmpl-{request_id}"
    return owner.stream(
        _stream_answer_core(
            core_input,
            deps,
            owner,
            endpoint="chat",
            action="chat_stream",
            kind=kind,
            hit_count=len(hits),
            token_frame=lambda delta: format_openai_chunk(chat_id, llm_model, delta_content=delta),
            final_frames=lambda output: _chat_final_frames(
                owner, chat_id, kind, timings, llm_model, output
            ),
            error_frames=lambda: (format_openai_error(), format_openai_done()),
        ),
        query_class=kind,
        hits=len(hits),
        media_type="text/event-stream",
    )


def json_log(request_id: str, action: str, **fields) -> str:
    return json.dumps({"request_id": request_id, "action": action, **fields})
