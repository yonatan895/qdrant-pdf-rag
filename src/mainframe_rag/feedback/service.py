"""Feedback service (issue #780): the single writer of the feedback store.

Agents (any replica) POST finalized answer records and forwarded console
ratings with a dedicated bearer token. Client errors carry fixed messages and
stable codes, never exception or upstream text; logs carry the request_id and
action only — never query, answer or comment text.
"""

from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mainframe_rag.feedback.store import AnswerRecord, FeedbackStore, Rating, UnknownAnswerError

log = logging.getLogger("feedback")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def create_app(store: FeedbackStore, token: str) -> FastAPI:
    if not token:
        raise RuntimeError("FEEDBACK_SERVICE_TOKEN is required")
    expected = f"Bearer {token}".encode()
    app = FastAPI(title="mainframe-rag feedback", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authorize(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path == "/healthz":
            return await call_next(request)
        supplied = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(supplied, expected):
            return _error(401, "unauthorized", "a valid feedback service token is required")
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_request", "the feedback request is invalid")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/answers", status_code=201)
    async def record_answer(record: AnswerRecord) -> dict[str, bool]:
        recorded = store.record_answer(record)
        log.info(json.dumps({"request_id": record.request_id, "action": "feedback_answer",
                             "recorded": recorded}))
        return {"recorded": recorded}

    @app.post("/v1/ratings", status_code=201, response_model=None)
    async def add_rating(rating: Rating) -> dict[str, int | str | None] | JSONResponse:
        try:
            rating_id = store.add_rating(rating)
        except UnknownAnswerError:
            return _error(404, "unknown_answer", "no recorded answer has this request id")
        answer = store.answer(rating.request_id) or {}
        log.info(json.dumps({"request_id": rating.request_id, "action": "feedback_rating",
                             "rating": rating.rating, "reasons": len(rating.reasons)}))
        # Bounded labels for the agent's rating counter; never text.
        state = (answer.get("telemetry") or {}).get("verification_state")
        return {"id": rating_id, "endpoint": answer.get("endpoint"),
                "verification_state": state if isinstance(state, str) else None}

    return app


def app_from_env() -> FastAPI:
    """uvicorn entry point: `uvicorn --factory mainframe_rag.feedback.service:app_from_env`."""
    import os

    path = Path(os.environ.get("FEEDBACK_STORE_PATH", "/data/feedback.sqlite3"))
    return create_app(FeedbackStore(path), os.environ.get("FEEDBACK_SERVICE_TOKEN", ""))
