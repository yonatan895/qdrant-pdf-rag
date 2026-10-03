"""Request admission: bounded active slots and a bounded FIFO wait queue
(issue #374).

One controller per process (the agent runs a single uvicorn worker). It
decides only *whether and when* a product request may start; the caller owns
the slot for the whole response and releases it exactly once through the
returned `AdmissionTicket`. No limit selected (`max_active == 0`) means the
pre-#374 behaviour: every request is admitted immediately and nothing is
counted against a limit.

Invariants (each has a test in tests/test_admission.py):
- active slots never exceed `max_active`; waiters never exceed `max_queue`;
- a refused request holds nothing and never starts model work;
- release is idempotent, so disconnect/timeout/error/normal completion paths
  can all call it without double-freeing a slot;
- a waiter that is cancelled or times out while a slot is being handed to it
  gives the slot back instead of leaking it;
- FIFO: a slot freed with waiters queued goes to the longest waiter, never to
  a newly arriving request.
"""

from __future__ import annotations

import asyncio
from collections import deque

REASON_QUEUE_FULL = "queue_full"
REASON_QUEUE_TIMEOUT = "queue_timeout"


class AdmissionRejected(Exception):
    """The request was not admitted. `reason` is a fixed label, never text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AdmissionTicket:
    """One held slot (or a no-op ticket when no limit is selected)."""

    __slots__ = ("_controller", "_held", "waited_s")

    def __init__(self, controller: AdmissionController, held: bool, waited_s: float) -> None:
        self._controller = controller
        self._held = held
        self.waited_s = waited_s

    @property
    def held(self) -> bool:
        return self._held

    def release(self) -> None:
        if self._held:
            self._held = False
            self._controller._release()


class AdmissionController:
    def __init__(self, max_active: int = 0, max_queue: int = 0, queue_wait_s: float = 0.0) -> None:
        self.max_active = max(0, max_active)
        self.max_queue = max(0, max_queue)
        self.queue_wait_s = max(0.0, queue_wait_s)
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def active(self) -> int:
        return self._active

    @property
    def queued(self) -> int:
        return len(self._waiters)

    async def acquire(self, wait_s: float | None = None) -> AdmissionTicket:
        """Take a slot or raise `AdmissionRejected`. `wait_s` caps the queue
        wait below `queue_wait_s` (the request deadline's remaining budget)."""
        if self.max_active == 0:
            return AdmissionTicket(self, False, 0.0)
        if self._active < self.max_active and not self._waiters:
            self._active += 1
            return AdmissionTicket(self, True, 0.0)
        if len(self._waiters) >= self.max_queue:
            raise AdmissionRejected(REASON_QUEUE_FULL)
        budget = self.queue_wait_s if wait_s is None else min(self.queue_wait_s, wait_s)
        if budget <= 0:
            raise AdmissionRejected(REASON_QUEUE_TIMEOUT)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append(fut)
        started = loop.time()
        try:
            async with asyncio.timeout(budget):
                await fut
        except BaseException as exc:
            granted = fut.done() and not fut.cancelled()
            if granted:
                # The slot was handed over just as this waiter left: return it.
                self._release()
            else:
                fut.cancel()
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
            if isinstance(exc, TimeoutError):
                raise AdmissionRejected(REASON_QUEUE_TIMEOUT) from exc
            raise
        return AdmissionTicket(self, True, loop.time() - started)

    def _release(self) -> None:
        # Transfer the slot to the longest live waiter (active count is
        # unchanged), else free it.
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(None)
                return
        self._active -= 1
