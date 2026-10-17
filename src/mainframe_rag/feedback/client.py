"""Agent-side feedback client (issue #780): never on the answer's critical path.

Finalized answer records go into a bounded in-memory queue drained by one
background task. A full queue drops the record (counted, never raised) and a
failed delivery is counted and logged by error type only: a store outage can
neither slow nor fail an answer. Console ratings are forwarded synchronously
because the operator waits for the outcome; they map to fixed outcomes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Literal

import httpx2

from mainframe_rag.agent.metrics import record_feedback_rating, record_feedback_record
from mainframe_rag.config import Settings, bearer_auth_headers
from mainframe_rag.feedback.store import AnswerRecord, Rating

log = logging.getLogger("feedback")

RateOutcome = Literal["ok", "unknown", "invalid", "unavailable"]


def _error_type(exc: BaseException) -> str:
    return type(exc).__name__


class FeedbackClient:
    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        if not (settings.feedback_url and settings.feedback_api_key):
            raise RuntimeError("FEEDBACK_ENABLED requires FEEDBACK_URL and FEEDBACK_API_KEY")
        self._base = settings.feedback_url.rstrip("/")
        self._headers = bearer_auth_headers(settings.feedback_api_key)
        self._queue: asyncio.Queue[AnswerRecord] = asyncio.Queue(maxsize=settings.feedback_queue_size)
        self._client = client or httpx2.AsyncClient(timeout=settings.feedback_timeout_s)
        self._owned = client is None
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.get_running_loop().create_task(self._drain())

    def submit(self, record: AnswerRecord) -> None:
        """Enqueue without waiting; a full queue drops the record."""
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            record_feedback_record("dropped")

    async def _drain(self) -> None:
        while True:
            record = await self._queue.get()
            try:
                response = await self._client.post(f"{self._base}/v1/answers", json=record.model_dump(),
                                                   headers=self._headers)
                if response.status_code == 201:
                    recorded = bool(response.json().get("recorded"))
                    record_feedback_record("recorded" if recorded else "duplicate")
                else:
                    record_feedback_record("failed")
                    log.warning(json.dumps({"request_id": record.request_id, "action": "feedback_record",
                                            "error": "status", "status": response.status_code}))
            except Exception as exc:  # noqa: BLE001 — best-effort by contract
                record_feedback_record("failed")
                log.warning(json.dumps({"request_id": record.request_id, "action": "feedback_record",
                                        "error": _error_type(exc)}))
            finally:
                self._queue.task_done()

    async def rate(self, rating: Rating) -> RateOutcome:
        try:
            response = await self._client.post(f"{self._base}/v1/ratings", json=rating.model_dump(),
                                               headers=self._headers)
        except Exception as exc:  # noqa: BLE001
            log.warning(json.dumps({"request_id": rating.request_id, "action": "feedback_rating",
                                    "error": _error_type(exc)}))
            return "unavailable"
        if response.status_code == 201:
            body = response.json()
            record_feedback_rating(rating.rating, list(rating.reasons), endpoint=str(body.get("endpoint")),
                                   verification_state=body.get("verification_state"))
            return "ok"
        if response.status_code == 404:
            return "unknown"
        if response.status_code == 422:
            return "invalid"
        return "unavailable"

    async def aclose(self, drain_timeout_s: float = 2.0) -> None:
        """Bounded drain on shutdown, then stop; never blocks shutdown longer."""
        if self._worker is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._queue.join(), drain_timeout_s)
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        if self._owned:
            await self._client.aclose()
