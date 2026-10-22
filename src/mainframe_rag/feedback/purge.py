"""Scheduled retention purge for the feedback service (issue #804, ADR-0005).

The service is the single SQLite writer, so it also schedules the purge: one
asyncio task started with the app, calling the same `FeedbackStore.purge` the
authenticated `POST /v1/purge` calls. Only an attempt whose result carries
`wal_truncated: true` counts as success. `wal_truncated: false` (a concurrent
read snapshot kept pre-delete page images in the WAL) and an exception (for
example `database is locked` from a foreign writer) are failures that are
retried: after 60, 300 and 900 seconds within a cycle, then once an hour
until a truncated result, then back to the daily cadence. Every attempt
re-runs the same idempotent delete and checkpoint, so retries and restarts
converge. Nothing here logs or exports text: logs carry counts, the boolean
and the exception type; metrics carry three bounded outcome labels.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, ProcessCollector

from mainframe_rag.feedback.store import FeedbackStore
from mainframe_rag.logs import error_type, json_log

log = logging.getLogger("feedback")

OUTCOMES: Final = ("truncated", "deferred", "error")
ROW_KINDS: Final = ("answers", "ratings")

# Protected defaults (ADR-0005, maintainer decision of 2026-10-18): a restore or
# a re-enable purges before data ages further, the daily cadence is
# `Settings.feedback_purge_interval_s`, and the retry ladder is a code constant
# so a site cannot configure a tight loop.
FIRST_DELAY_S: Final = 30.0
RETRY_DELAYS_S: Final = (60.0, 300.0, 900.0)
CATCH_UP_INTERVAL_S: Final = 3600.0


class FeedbackMetrics:
    """The service's own registry: purge outcomes only, no request- or
    content-derived labels. `prometheus_client` directly (no OTel setup); one
    process, so the per-process registry is the whole truth."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        self.purge_total = Counter(
            "rag_feedback_purge", "Feedback retention purge attempts by outcome",
            ["outcome"], registry=self.registry)
        self.last_success = Gauge(
            "rag_feedback_purge_last_success_timestamp_seconds",
            "Unix time of the last purge whose WAL was truncated (0 = never)",
            registry=self.registry)
        self.purged_rows = Counter(
            "rag_feedback_purged_rows", "Rows deleted by the purge", ["kind"],
            registry=self.registry)
        for outcome in OUTCOMES:  # series exist at 0 so rate() and absent() behave
            self.purge_total.labels(outcome)
        for kind in ROW_KINDS:
            self.purged_rows.labels(kind)


class PurgeRunner:
    """One purge attempt: the single place that classifies, counts and logs a
    result, shared by the scheduler and `POST /v1/purge`."""

    def __init__(self, store: FeedbackStore, retention_days: int, metrics: FeedbackMetrics,
                 *, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._retention_days = retention_days
        self._metrics = metrics
        self._clock = clock

    def run(self) -> tuple[str, dict[str, int | bool]]:
        """Blocking attempt. Returns (outcome, store result); an exception is
        counted as outcome `error`, logged by type only, and re-raised."""
        try:
            result = self._store.purge(retention_days=self._retention_days)
        except Exception as exc:
            self._metrics.purge_total.labels("error").inc()
            log.error(json_log("feedback", "feedback_purge", outcome="error", error=error_type(exc)))
            raise
        outcome = "truncated" if result["wal_truncated"] else "deferred"
        self._metrics.purge_total.labels(outcome).inc()
        self._metrics.purged_rows.labels("answers").inc(int(result["answers"]))
        self._metrics.purged_rows.labels("ratings").inc(int(result["ratings"]))
        if outcome == "truncated":
            self._metrics.last_success.set(self._clock())
        log.log(logging.INFO if outcome == "truncated" else logging.WARNING,
                json_log("feedback", "feedback_purge", outcome=outcome, **result))
        return outcome, result


@dataclass(frozen=True)
class PurgeSchedule:
    interval_s: float
    first_delay_s: float = FIRST_DELAY_S
    retry_delays_s: tuple[float, ...] = RETRY_DELAYS_S
    catch_up_interval_s: float = CATCH_UP_INTERVAL_S


async def run_schedule(
    runner: PurgeRunner,
    schedule: PurgeSchedule,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Purge forever: first attempt after `first_delay_s`, then every
    `interval_s`. A cycle that never reaches `truncated` ends failed and the
    loop switches to one attempt per `catch_up_interval_s` until it does. The
    blocking store call runs in the threadpool, off the event loop."""

    async def attempt() -> bool:
        try:
            outcome, _ = await asyncio.to_thread(runner.run)
        except Exception:  # noqa: BLE001 - already counted and logged by the runner
            return False
        return outcome == "truncated"

    await sleep(schedule.first_delay_s)
    while True:
        done = await attempt()
        for delay in schedule.retry_delays_s:
            if done:
                break
            await sleep(delay)
            done = await attempt()
        while not done:
            await sleep(schedule.catch_up_interval_s)
            done = await attempt()
        await sleep(schedule.interval_s)
