"""Feedback service (issue #780, ADR-0005): the single writer of the feedback store.

Agents (any replica) POST finalized answer records and forwarded console
ratings with a dedicated bearer token. The retention window is enforced by the
service's own scheduled purge (feedback/purge.py, #804) and, as a manual
operator action, by the authenticated `POST /v1/purge`. Client errors carry
fixed messages and stable codes, never exception or upstream text; logs follow
the agent's JSON-line contract and carry the request_id and counts only, never
query, answer or comment text. `GET /metrics` (404 unless `METRICS_ENABLED`)
needs the same bearer token as every route except `/healthz`. Handlers are
synchronous so FastAPI runs the blocking SQLite calls in its threadpool, off
the event loop.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from mainframe_rag.feedback.purge import (
    FeedbackMetrics,
    PurgeRunner,
    PurgeSchedule,
    run_schedule,
)
from mainframe_rag.feedback.store import AnswerRecord, FeedbackStore, Rating, UnknownAnswerError
from mainframe_rag.logs import error_type, json_log

log = logging.getLogger("feedback")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def _bearer(token: str | None) -> bytes:
    """The one token normalization, mirroring config.bearer_auth_headers
    (pasted Secrets often trail a newline)."""
    return f"Bearer {(token or '').strip()}".encode()


def create_app(
    store: FeedbackStore,
    token: str,
    *,
    retention_days: int = 180,
    metrics_enabled: bool = False,
    schedule: PurgeSchedule | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FastAPI:
    """`schedule` starts the in-process purge with the app (single replica,
    single worker: one scheduler); None starts none (unit tests, tools)."""
    if not (token and token.strip()):
        raise RuntimeError("FEEDBACK_SERVICE_TOKEN is required")
    expected = _bearer(token)
    metrics = FeedbackMetrics()
    runner = PurgeRunner(store, retention_days, metrics)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        task = (asyncio.create_task(run_schedule(runner, schedule, sleep=sleep))
                if schedule is not None else None)
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="mainframe-rag feedback", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    @app.middleware("http")
    async def authorize(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path == "/healthz":
            return await call_next(request)
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(_bearer(supplied.removeprefix("Bearer ")), expected):
            return _error(401, "unauthorized", "a valid feedback service token is required")
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_request", "the feedback request is invalid")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error(404, "not_found", "no such feedback operation")
        if exc.status_code == 405:
            return _error(405, "method_not_allowed", "the method is not allowed for this operation")
        return _error(exc.status_code, "request_failed", "the feedback request failed")

    @app.exception_handler(Exception)
    async def internal(_request: Request, exc: Exception) -> JSONResponse:
        log.error(json_log("feedback", "feedback_error", error=error_type(exc)))
        return _error(500, "internal_error", "the feedback service failed")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/answers", status_code=201)
    def record_answer(record: AnswerRecord) -> dict[str, bool]:
        recorded = store.record_answer(record)
        log.info(json_log(record.request_id, "feedback_answer", recorded=recorded))
        return {"recorded": recorded}

    @app.post("/v1/ratings", status_code=201, response_model=None)
    def add_rating(rating: Rating) -> dict[str, int | str | None] | JSONResponse:
        try:
            rating_id = store.add_rating(rating)
        except UnknownAnswerError:
            return _error(404, "unknown_answer", "no recorded answer has this request id")
        endpoint, state = store.answer_labels(rating.request_id) or (None, None)
        log.info(json_log(rating.request_id, "feedback_rating", rating=rating.rating,
                          reasons=len(rating.reasons)))
        # Bounded labels for the agent's rating counter; never text.
        return {"id": rating_id, "endpoint": endpoint, "verification_state": state}

    @app.get("/metrics")
    def scrape() -> Response:
        if not metrics_enabled:
            return _error(404, "not_found", "no such feedback operation")
        return Response(content=generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/purge")
    def purge() -> dict[str, int | bool]:
        """Manual retention purge (ADR-0005), the same attempt the schedule
        runs. `wal_truncated: false` means a concurrent reader deferred
        removing the purged text from disk; rerun it afterwards (idempotent)."""
        return runner.run()[1]

    return app


def app_from_env() -> FastAPI:
    """uvicorn entry point: `uvicorn --factory mainframe_rag.feedback.service:app_from_env`."""
    from pathlib import Path

    from mainframe_rag.config import load_settings
    from mainframe_rag.logs import configure_logging

    settings = load_settings()
    configure_logging(settings.log_level)
    return create_app(
        FeedbackStore(Path(settings.feedback_store_path)),
        settings.feedback_service_token or "",
        retention_days=settings.feedback_retention_days,
        metrics_enabled=settings.metrics_enabled,
        schedule=PurgeSchedule(interval_s=settings.feedback_purge_interval_s,
                               first_delay_s=settings.feedback_purge_first_delay_s),
    )
