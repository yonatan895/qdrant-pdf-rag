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
import logging
from typing import Any, Literal

import httpx2

from mainframe_rag.agent.metrics import record_feedback_rating, record_feedback_record
from mainframe_rag.config import Settings, bearer_auth_headers
from mainframe_rag.feedback.store import AnswerRecord, Rating
from mainframe_rag.logs import error_type, json_log

log = logging.getLogger("feedback")

RateOutcome = Literal["ok", "unknown", "invalid", "unavailable"]


def _json_object(response: Any) -> dict[str, Any]:
    """The response's JSON object, or {} for an empty, non-JSON or non-object body."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _code(response: Any) -> str | None:
    code = _json_object(response).get("code")
    return code if isinstance(code, str) else None


class FeedbackClient:
    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        if not (settings.feedback_url and settings.feedback_api_key):
            raise RuntimeError("FEEDBACK_ENABLED requires FEEDBACK_URL and FEEDBACK_API_KEY")
        self._base = settings.feedback_url.rstrip("/")
        self._headers = bearer_auth_headers(settings.feedback_api_key)
        self._queue: asyncio.Queue[AnswerRecord] = asyncio.Queue(maxsize=settings.feedback_queue_size)
        self._client = client or httpx2.AsyncClient(timeout=settings.feedback_timeout_s)
        self._owned = client is None
        self._drain_timeout_s = settings.feedback_drain_timeout_s
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
                    body = _json_object(response)
                    # 201 means stored; only an explicit recorded=false is a duplicate.
                    record_feedback_record("duplicate" if body.get("recorded") is False else "recorded")
                else:
                    record_feedback_record("failed")
                    log.warning(json_log(record.request_id, "feedback_record", error="status",
                                         status=response.status_code))
            except asyncio.CancelledError:
                # Shutdown cut the in-flight delivery: a counted drop, never silent.
                record_feedback_record("dropped")
                raise
            except Exception as exc:  # noqa: BLE001 — best-effort by contract
                record_feedback_record("failed")
                log.warning(json_log(record.request_id, "feedback_record", error=error_type(exc)))
            finally:
                self._queue.task_done()

    async def rate(self, rating: Rating) -> RateOutcome:
        try:
            response = await self._client.post(f"{self._base}/v1/ratings", json=rating.model_dump(),
                                               headers=self._headers)
        except Exception as exc:  # noqa: BLE001
            log.warning(json_log(rating.request_id, "feedback_rating", error=error_type(exc)))
            return "unavailable"
        if response.status_code == 201:
            labels = _json_object(response)  # metric labels only; the rating is stored
            record_feedback_rating(rating.rating, list(rating.reasons), endpoint=str(labels.get("endpoint") or "unknown"),
                                   verification_state=labels.get("verification_state"))
            return "ok"
        if response.status_code == 404 and _code(response) == "unknown_answer":
            return "unknown"
        if response.status_code == 422:
            return "invalid"
        # A token mismatch (401), a wrong FEEDBACK_URL path (route 404) or a
        # service fault must not look like silence.
        log.warning(json_log(rating.request_id, "feedback_rating", error="status",
                             status=response.status_code))
        return "unavailable"

    async def aclose(self) -> None:
        """Bounded drain on shutdown (FEEDBACK_DRAIN_TIMEOUT_S), then stop.
        Records still queued, or cut in flight, are counted as dropped."""
        if self._worker is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._queue.join(), self._drain_timeout_s)
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            record_feedback_record("dropped")
        if self._owned:
            await self._client.aclose()
