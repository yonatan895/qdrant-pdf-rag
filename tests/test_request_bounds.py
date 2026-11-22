"""Request admission, total deadline and embed-input bound (issue #374).

Hermetic: the real FastAPI app is driven over raw ASGI inside one event loop
(so a stream can be held open and a client disconnect delivered), with fake
retrieval and model legs. No network, no GPU, no Qdrant; the body-bound
socket tests (#1352) serve the app with uvicorn on a loopback port 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import urllib.parse
from pathlib import Path

import pytest

from mainframe_rag.agent import admission as admission_mod
from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.admission import AdmissionController, AdmissionRejected, BodyBudget
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.ingest.bounds import EmbedInputTooLarge
from tests.helpers_agent_local import set_agent_env
from tests.test_agent_api import FakeLLM, _hit

OVERLOADED = {"code": "overloaded", "message": "the service is at capacity; retry later"}
DEADLINE = {"code": "deadline_exceeded", "message": "request deadline exceeded"}
INVALID = {"code": "invalid_request", "message": "request body failed validation"}


@pytest.fixture
def env(monkeypatch, servable_representation_gate):
    set_agent_env(monkeypatch)
    return monkeypatch


class _Call:
    """One in-flight ASGI request: records the response, can disconnect."""

    def __init__(
        self,
        path: str,
        body: dict,
        query: str = "",
        method: str = "POST",
        form: bool = False,
        framing: str | None = None,
        chunks: list[bytes | None] | None = None,
        declared: int | None = None,
    ) -> None:
        # framing (issue #1352): None sends no length header (the pre-#1352
        # shape of these tests), "length" a Content-Length (`declared` or the
        # payload size), "chunked" Transfer-Encoding with `chunks` as the
        # request messages (a trailing None: the upload stalls until
        # disconnect). `receives` counts body reads by the app.
        self.framing = framing
        self.declared = declared
        self.receives = 0
        self.method = method
        self.path = path
        self.query = query.encode()
        self.content_type = b"application/x-www-form-urlencoded" if form else b"application/json"
        self.payload = (
            urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        )
        self._chunks: list[bytes | None] = list(chunks) if chunks is not None else [self.payload]
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""
        self.first_chunk = asyncio.Event()
        self._disconnect = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def _receive(self):
        if self._chunks and self._chunks[0] is not None:
            self.receives += 1
            chunk = self._chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(self._chunks)}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")
            if message.get("body"):
                self.first_chunk.set()

    def start(self) -> _Call:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": self.method,
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": self.query,
            "root_path": "",
            "headers": [(b"content-type", self.content_type), *self._framing_headers()],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }
        self.task = asyncio.ensure_future(app_mod.app(scope, self._receive, self._send))
        return self

    def _framing_headers(self) -> list[tuple[bytes, bytes]]:
        if self.framing == "length":
            size = len(self.payload) if self.declared is None else self.declared
            return [(b"content-length", str(size).encode())]
        if self.framing == "chunked":
            return [(b"transfer-encoding", b"chunked")]
        return []

    def disconnect(self) -> None:
        self._disconnect.set()

    async def done(self) -> _Call:
        assert self.task is not None
        await asyncio.wait_for(self.task, 5)
        return self

    def json(self) -> dict:
        return json.loads(self.body)


async def _until(predicate, what: str, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


class BlockingSearch:
    """Async retrieval double that parks until released; records cancellation."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0
        self.gate = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return [_hit()], "identifier", {"embed_ms": 1, "qdrant_ms": 2}


class InstantSearch:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        return [_hit()], "identifier", {"embed_ms": 1, "qdrant_ms": 2}


class ParkedStreamLLM(FakeLLM):
    """Streams one token, then parks until cancelled (a slow model leg)."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = asyncio.Event()

    async def chat_stream(self, messages, *args, **kwargs):
        try:
            yield {"type": "token", "delta": "Reissue the command", "ttft_ms": 1}
            await asyncio.Event().wait()
        finally:
            self.closed.set()


@contextlib.asynccontextmanager
async def _serving(monkeypatch, search, llm=None):
    """Real lifespan (settings from env) plus the same fakes the TestClient
    fixture installs, unwound on every exit path."""
    cm = app_mod.lifespan(app_mod.app)
    await cm.__aenter__()
    monkeypatch.setattr(app_mod, "retrieve_search", search)
    monkeypatch.setattr(app_mod, "llm", llm or FakeLLM())
    monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
    try:
        yield
    finally:
        await cm.__aexit__(None, None, None)


def _run(coro_factory):
    asyncio.run(coro_factory())


SEARCH = ("/v1/search", {"query": "IEA500I"})


def _stream_call(endpoint):
    """(path, JSON body) of one endpoint's streaming surface."""
    if endpoint == "console":
        return "/ui/chat/stream", {"messages": [{"role": "user", "content": "IEA500I"}]}
    if endpoint == "answer":
        return "/v1/answer", {"query": "IEA500I", "stream": True}
    return "/v1/chat", {
        "messages": [{"role": "user", "content": "IEA500I"}], "stream": True,
    }


# ------------------------------------------------------------ controller unit


def test_controller_never_exceeds_active_or_queue_and_is_fifo():
    async def main():
        ctl = AdmissionController(max_active=2, max_queue=2, queue_wait_s=5)
        a, b = await ctl.acquire(), await ctl.acquire()
        order: list[str] = []

        async def waiter(name):
            t = await ctl.acquire()
            order.append(name)
            return t

        w1 = asyncio.ensure_future(waiter("w1"))
        await _until(lambda: ctl.queued == 1, "w1 queued")
        w2 = asyncio.ensure_future(waiter("w2"))
        await _until(lambda: ctl.queued == 2, "w2 queued")
        with pytest.raises(AdmissionRejected) as full:
            await ctl.acquire()
        assert full.value.reason == admission_mod.REASON_QUEUE_FULL
        assert ctl.active == 2 and ctl.queued == 2

        a.release()
        t1 = await asyncio.wait_for(w1, 1)
        assert order == ["w1"] and ctl.active == 2 and ctl.queued == 1
        # A new arrival cannot overtake the queued waiter.
        b.release()
        t2 = await asyncio.wait_for(w2, 1)
        assert order == ["w1", "w2"] and ctl.active == 2 and ctl.queued == 0
        t1.release()
        t2.release()
        assert ctl.active == 0

    _run(main)


def test_controller_release_is_idempotent():
    async def main():
        ctl = AdmissionController(max_active=1)
        t = await ctl.acquire()
        t.release()
        t.release()
        assert ctl.active == 0
        again = await ctl.acquire()
        assert ctl.active == 1
        again.release()

    _run(main)


def test_controller_queue_timeout_leaves_no_waiter_and_no_leak():
    async def main():
        ctl = AdmissionController(max_active=1, max_queue=1, queue_wait_s=0.05)
        holder = await ctl.acquire()
        with pytest.raises(AdmissionRejected) as timed_out:
            await ctl.acquire()
        assert timed_out.value.reason == admission_mod.REASON_QUEUE_TIMEOUT
        assert ctl.queued == 0 and ctl.active == 1
        with pytest.raises(AdmissionRejected):  # an exhausted caller deadline never queues
            await ctl.acquire(0.0)
        assert ctl.queued == 0
        holder.release()
        assert ctl.active == 0
        (await ctl.acquire()).release()  # next ordinary request is admitted

    _run(main)


def test_controller_cancelled_waiter_that_was_just_granted_returns_the_slot():
    async def main():
        ctl = AdmissionController(max_active=1, max_queue=1, queue_wait_s=5)
        holder = await ctl.acquire()
        waiter = asyncio.ensure_future(ctl.acquire())
        await _until(lambda: ctl.queued == 1, "waiter queued")
        holder.release()  # hands the slot to the waiter's future ...
        waiter.cancel()  # ... which is cancelled before it resumes
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert ctl.active == 0 and ctl.queued == 0
        (await ctl.acquire()).release()

    _run(main)


# ------------------------------------------------------------------ admission


def test_default_settings_impose_no_limit(env):
    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            calls = [_Call(*SEARCH).start() for _ in range(6)]
            await _until(lambda: search.calls == 6, "all six requests in the retrieval leg")
            search.gate.set()
            for c in calls:
                assert (await c.done()).status == 200

    _run(main)


def test_overload_returns_stable_code_does_no_work_and_recovers(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            first = _Call(*SEARCH).start()
            await _until(lambda: search.calls == 1, "first request holding the slot")
            assert app_mod.admission.active == 1

            refused = await _Call(*SEARCH).start().done()
            assert refused.status == 503
            assert refused.json() == OVERLOADED
            assert refused.headers["retry-after"] == "1"
            assert search.calls == 1, "a refused request must not start any work"

            search.gate.set()
            assert (await first.done()).status == 200
            assert app_mod.admission.active == 0

            nxt = await _Call(*SEARCH).start().done()  # next ordinary request
            assert nxt.status == 200 and nxt.json()["hits"]
            assert app_mod.admission.active == 0 and app_mod.admission.queued == 0

    _run(main)


@pytest.mark.parametrize(
    ("path", "body", "query"),
    [
        ("/v1/answer", {"query": "IEA500I"}, ""),
        ("/v1/chat", {"messages": [{"role": "user", "content": "IEA500I"}]}, ""),
        ("/v1/chat/completions", {"messages": [{"role": "user", "content": "IEA500I"}]}, ""),
    ],
)
def test_every_product_endpoint_is_admitted(env, path, body, query):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            holder = _Call("/v1/search", SEARCH[1]).start()
            await _until(lambda: search.calls == 1, "slot held")
            refused = await _Call(path, body, query).start().done()
            assert (refused.status, refused.json()) == (503, OVERLOADED)
            assert search.calls == 1
            search.gate.set()
            await holder.done()

    _run(main)


def test_probes_are_never_admitted_or_limited(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            holder = _Call(*SEARCH).start()
            await _until(lambda: search.calls == 1, "slot held")
            probe = await _Call("/livez", {}, method="GET").start().done()
            assert (probe.status, probe.json()) == (200, {"status": "alive"})
            search.gate.set()
            await holder.done()

    _run(main)


def test_bounded_queue_admits_in_order_and_refuses_beyond_it(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_QUEUE_WAIT_S", "5")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            first = _Call(*SEARCH).start()
            await _until(lambda: search.calls == 1, "first holds the slot")
            queued = _Call(*SEARCH).start()
            await _until(lambda: app_mod.admission.queued == 1, "second queued")
            third = await _Call(*SEARCH).start().done()
            assert (third.status, third.json()) == (503, OVERLOADED)
            assert search.calls == 1, "queued and refused requests have started nothing"

            search.gate.set()
            assert (await first.done()).status == 200
            assert (await queued.done()).status == 200
            assert search.calls == 2
            assert app_mod.admission.active == 0 and app_mod.admission.queued == 0

    _run(main)


def test_queue_wait_expiry_is_the_overload_code(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_QUEUE_WAIT_S", "0.05")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            first = _Call(*SEARCH).start()
            await _until(lambda: search.calls == 1, "first holds the slot")
            late = await _Call(*SEARCH).start().done()
            assert (late.status, late.json()) == (503, OVERLOADED)
            assert app_mod.admission.queued == 0
            search.gate.set()
            await first.done()

    _run(main)


def test_stream_holds_slot_until_disconnect_then_releases_exactly_once(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        llm = ParkedStreamLLM()
        async with _serving(env, InstantSearch(), llm):
            stream = _Call("/v1/answer", {"query": "IEA500I"}, "stream=true").start()
            await asyncio.wait_for(stream.first_chunk.wait(), 5)
            assert stream.status == 200 and app_mod.admission.active == 1

            refused = await _Call(*SEARCH).start().done()
            assert (refused.status, refused.json()) == (503, OVERLOADED)

            stream.disconnect()
            await stream.done()
            await asyncio.wait_for(llm.closed.wait(), 5)  # model leg torn down
            assert app_mod.admission.active == 0

            after = await _Call(*SEARCH).start().done()
            assert after.status == 200
            assert app_mod.admission.active == 0

    _run(main)


def test_slot_is_released_when_the_request_fails(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        async def broken(*a, **k):
            raise RuntimeError("upstream exploded with secret text")

        async with _serving(env, broken):
            failed = await _Call(*SEARCH).start().done()
            assert failed.status == 502
            assert "secret" not in failed.body.decode()
            assert app_mod.admission.active == 0
            app_mod.retrieve_search = InstantSearch()
            assert (await _Call(*SEARCH).start().done()).status == 200

    _run(main)


# ------------------------------------------------------------------- deadline


def test_deadline_cancels_the_leg_cleanly_and_the_next_request_runs(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.2")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            expired = await _Call(*SEARCH).start().done()
            assert (expired.status, expired.json()) == (504, DEADLINE)
            assert search.cancelled == 1, "the awaiting retrieval leg is cancelled"
            assert app_mod.admission.active == 0

            monkey = InstantSearch()
            app_mod.retrieve_search = monkey
            nxt = await _Call(*SEARCH).start().done()
            assert nxt.status == 200 and monkey.calls == 1

    _run(main)


def test_deadline_bounds_the_queue_wait_too(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_QUEUE_WAIT_S", "60")
    env.setenv("REQUEST_DEADLINE_S", "30")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            first = _Call(*SEARCH).start()
            await _until(lambda: search.calls == 1, "first holds the slot")
            # Shrink the deadline for the waiter only; the queue wait cap is
            # min(queue_wait_s, remaining deadline).
            env.setattr(app_mod.settings, "request_deadline_s", 0.1)
            started = asyncio.get_running_loop().time()
            late = await _Call(*SEARCH).start().done()
            assert (late.status, late.json()) == (503, OVERLOADED)
            assert asyncio.get_running_loop().time() - started < 3
            assert app_mod.admission.queued == 0
            search.gate.set()
            await first.done()

    _run(main)


def test_deadline_mid_stream_ends_with_terminal_error_frame_and_frees_the_slot(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.3")

    async def main():
        llm = ParkedStreamLLM()
        async with _serving(env, InstantSearch(), llm):
            call = await _Call("/v1/answer", {"query": "IEA500I"}, "stream=true").start().done()
            text = call.body.decode()
            assert call.status == 200
            assert "event: token" in text and "event: error" in text
            assert "event: final" not in text, "an expired stream must not claim completion"
            await asyncio.wait_for(llm.closed.wait(), 5)
            assert app_mod.admission.active == 0
            assert (await _Call(*SEARCH).start().done()).status == 200

    _run(main)


# ------------------------------------------------------------ embed-input bound


@pytest.mark.parametrize("endpoint", ["search", "chat"])
def test_oversize_query_is_rejected_before_any_model_call(env, endpoint):
    prefix_len = len(app_mod.Settings.model_fields["dense_query_prefix"].default)
    env.setenv("EMBED_MAX_INPUT_CHARS", str(prefix_len + 40))

    def body(text):
        if endpoint == "chat":
            return "/v1/chat", {"messages": [{"role": "user", "content": text}]}
        return f"/v1/{endpoint}", {"query": text}

    async def main():
        search = InstantSearch()
        llm = FakeLLM()
        async with _serving(env, search, llm):
            over = await _Call(*body("x" * 41)).start().done()
            assert (over.status, over.json()) == (422, INVALID)
            assert search.calls == 0 and llm.calls == 0, "no embed/retrieval/LLM work"

            at_limit = await _Call(*body("x" * 40)).start().done()
            assert at_limit.status == 200 and search.calls == 1

    _run(main)


@pytest.mark.parametrize("endpoint", ["search", "chat"])
def test_embedder_refusal_backstop_is_a_422_not_an_upstream_error(env, endpoint):
    async def refuse(*a, **k):
        raise EmbedInputTooLarge(100, 150, 1)

    def body():
        if endpoint == "chat":
            return "/v1/chat", {"messages": [{"role": "user", "content": "IEA500I"}]}
        return f"/v1/{endpoint}", {"query": "IEA500I"}

    async def main():
        llm = FakeLLM()
        async with _serving(env, refuse, llm):
            resp = await _Call(*body()).start().done()
            assert (resp.status, resp.json()) == (422, INVALID)
            assert llm.calls == 0

    _run(main)


def test_default_embed_bound_is_off_and_long_queries_pass_unchanged(env):
    async def main():
        search = InstantSearch()
        async with _serving(env, search):
            ok = await _Call("/v1/search", {"query": "x" * 1999}).start().done()
            assert ok.status == 200 and search.calls == 1

    _run(main)


# -------------------------------------------------------------------- console


def test_console_shares_the_admission_slot_and_deadline(env):
    env.setenv("UI_ENABLED", "true")
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            holder = _Call(*SEARCH).start()
            await _until(lambda: search.calls == 1, "API request holds the slot")

            stream = await _Call(
                "/ui/chat/stream", {"messages": [{"role": "user", "content": "IEA500I"}]}
            ).start().done()
            assert (stream.status, stream.json()) == (503, OVERLOADED)

            form = await _Call(
                "/ui/chat", {"message": "IEA500I", "messages": ""}, form=True
            ).start().done()
            assert form.status == 502  # fixed console banner, not a stack or upstream text
            assert b"overloaded" not in form.body and b"capacity" not in form.body
            assert search.calls == 1, "console refusals start no work either"
            search.gate.set()
            await holder.done()
            assert app_mod.admission.active == 0

    _run(main)


def test_console_stream_deadline_is_a_terminal_error_frame(env):
    env.setenv("UI_ENABLED", "true")
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.3")

    async def main():
        llm = ParkedStreamLLM()
        async with _serving(env, InstantSearch(), llm):
            call = await _Call(
                "/ui/chat/stream", {"messages": [{"role": "user", "content": "IEA500I"}]}
            ).start().done()
            text = call.body.decode()
            assert call.status == 200
            assert "event: error" in text and "event: final" not in text
            await asyncio.wait_for(llm.closed.wait(), 5)
            assert app_mod.admission.active == 0

    _run(main)


def test_console_non_stream_deadline_renders_the_fixed_banner_and_frees_the_slot(env):
    env.setenv("UI_ENABLED", "true")
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.2")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            form = await _Call(
                "/ui/chat", {"message": "IEA500I", "messages": ""}, form=True
            ).start().done()
            assert form.status == 502 and b"could not complete" in form.body
            assert search.cancelled == 1 and app_mod.admission.active == 0

    _run(main)


# -------------------------------------------------------------------- metrics


def test_admission_metrics_are_bounded_and_fail_open(monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from mainframe_rag.agent import metrics as metrics_mod

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        metrics_mod, "_instruments", metrics_mod.create_instruments(provider.get_meter("t"))
    )
    metrics_mod.record_admission("search", delta=1, wait_s=0.25)
    metrics_mod.record_admission("search", delta=-1)
    metrics_mod.record_admission_rejected("search", "queue_full")
    metrics_mod.record_admission("not-an-endpoint", delta=1)  # unbounded label: ignored

    data = reader.get_metrics_data()
    points = {
        m.name: m.data.data_points
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert points["rag.admission.inflight"][0].value == 0
    assert dict(points["rag.admission.inflight"][0].attributes) == {"endpoint": "search"}
    assert points["rag.admission.wait"][0].count == 1
    assert dict(points["rag.admission.rejected"][0].attributes) == {
        "endpoint": "search",
        "reason": "queue_full",
    }
    assert len(points["rag.admission.inflight"]) == 1


@pytest.mark.parametrize("termination", ["success", "refusal", "deadline", "disconnect"])
def test_exact_storage_read_shares_admission_and_releases_for_next_request(env, termination):
    from tests.test_evidence_service import (
        BUILD_A,
        LOGICAL,
        TEXT,
        _expected_envelope,
        _expected_ref,
        _world,
    )

    env.setenv("QDRANT_COLLECTION", LOGICAL)
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    if termination == "deadline":
        env.setenv("REQUEST_DEADLINE_S", "0.2")

    async def main():
        entered, gate, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        qd = _world(serving=True)

        async def storage_wait(name, ids):
            entered.set()
            try:
                await gate.wait()
            finally:
                closed.set()

        qd.before_retrieve = storage_wait
        search = InstantSearch()
        async with _serving(env, search), contextlib.AsyncExitStack() as cleanup:
            cleanup.callback(gate.set)  # release the parked read even on failure
            env.setattr(app_mod, "qdrant", qd)
            ref = _expected_ref(BUILD_A, _expected_envelope())
            holder = _Call(f"/v1/evidence/{ref}", {}, "max_bytes=4" if termination == "refusal" else "", method="GET").start()
            await asyncio.wait_for(entered.wait(), 2)
            refused = await _Call(f"/v1/evidence/{ref}", {}, method="GET").start().done()
            ordinary = await _Call(*SEARCH).start().done()
            assert (refused.status, refused.json()) == (503, OVERLOADED)
            assert (ordinary.status, ordinary.json()) == (503, OVERLOADED)
            assert search.calls == 0 and qd.calls.count("retrieve") == 1
            assert app_mod.admission.active == 1
            assert (await _Call("/livez", {}, method="GET").start().done()).status == 200
            if termination == "disconnect":
                assert holder.task is not None
                holder.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await holder.task
            else:
                if termination != "deadline":
                    gate.set()
                await holder.done()
                expected = {"success": 200, "refusal": 413, "deadline": 504}[termination]
                assert holder.status == expected
                if termination == "deadline":
                    assert holder.json() == DEADLINE
            assert closed.is_set() and app_mod.admission.active == 0
            gate.set()
            next_read = await _Call(f"/v1/evidence/{ref}", {}, method="GET").start().done()
            assert next_read.status == 200 and next_read.json()["text"] == TEXT
            assert (await _Call(*SEARCH).start().done()).status == 200
            assert app_mod.admission.active == 0

    _run(main)


@pytest.mark.parametrize("endpoint", ["answer", "chat", "console"])
def test_stream_deadline_closes_stalled_asgi_send_and_admits_next_queued_request(env, endpoint):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.4")
    env.setenv("UI_ENABLED", "true")

    async def main():
        llm = ParkedStreamLLM()
        async with _serving(env, InstantSearch(), llm):
            send_entered, send_closed = asyncio.Event(), asyncio.Event()
            released = []
            release = admission_mod.AdmissionTicket.release

            def record_release(ticket):
                if ticket.held:
                    released.append(ticket)
                return release(ticket)

            env.setattr(admission_mod.AdmissionTicket, "release", record_release)
            path, body = _stream_call(endpoint)
            call = _Call(path, body)
            actual_send = call._send

            async def blocked_send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    send_entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        send_closed.set()
                await actual_send(message)

            call._send = blocked_send
            call.start()
            await asyncio.wait_for(send_entered.wait(), 3)
            await asyncio.sleep(0.06)
            next_call = _Call(*SEARCH).start()
            await _until(lambda: app_mod.admission.queued == 1, "ordinary request queued")
            await call.done()
            assert send_closed.is_set() and llm.closed.is_set()
            assert llm.calls <= 1 and b"event: final" not in call.body
            await next_call.done()
            assert next_call.status == 200
            assert app_mod.admission.active == app_mod.admission.queued == 0
            assert len(released) == 2 and released[0] is not released[1]
            assert (await _Call(*SEARCH).start().done()).status == 200

    _run(main)


@pytest.mark.parametrize("endpoint", ["answer", "console"])
def test_producer_deadline_terminal_frames_share_one_absolute_delivery_grace(env, endpoint):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.2")
    env.setenv("UI_ENABLED", "true")

    async def main():
        llm = ParkedStreamLLM()
        async with _serving(env, InstantSearch(), llm):
            path, body = _stream_call(endpoint)
            call = _Call(path, body)
            actual_send = call._send
            grace_started = None
            sends = 0

            async def slow_terminal_send(message):
                nonlocal grace_started, sends
                wire = message.get("body", b"")
                if message["type"] == "http.response.body" and (
                    grace_started is not None or b"event: error" in wire or b'"error"' in wire
                ):
                    if grace_started is None:
                        grace_started = asyncio.get_running_loop().time()
                    sends += 1
                    await asyncio.sleep(0.65)
                await actual_send(message)

            call._send = slow_terminal_send
            await call.start().done()
            assert grace_started is not None and sends >= 2
            assert asyncio.get_running_loop().time() - grace_started < 1.35
            assert llm.closed.is_set() and app_mod.admission.active == 0
            assert b"event: final" not in call.body
            assert (await _Call(*SEARCH).start().done()).status == 200

    _run(main)


@pytest.mark.parametrize("endpoint", ["chat", "console"])
def test_deadline_and_admission_include_the_closing_asgi_body_send(env, endpoint):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.4")
    env.setenv("UI_ENABLED", "true")

    async def main():
        async with _serving(env, InstantSearch()):
            closing = asyncio.Event()
            path, body = _stream_call(endpoint)
            call = _Call(path, body)
            actual_send = call._send

            async def block_closing_body(message):
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    closing.set()
                    await asyncio.Event().wait()
                await actual_send(message)

            call._send = block_closing_body
            call.start()
            await asyncio.wait_for(closing.wait(), 3)
            assert app_mod.admission.active == 1
            await asyncio.sleep(0.06)
            next_call = _Call(*SEARCH).start()
            await _until(lambda: app_mod.admission.queued == 1, "next request queued")
            await call.done()
            await next_call.done()
            assert next_call.status == 200 and app_mod.admission.active == 0

    _run(main)


# ------------------------------------------------- request-body budget (#1352)

MIB = 1 << 20
TOO_LARGE_1MIB = {"code": "invalid_request", "message": f"request body exceeds the {MIB}-byte limit"}
CHAT = ("/v1/chat", {"messages": [{"role": "user", "content": "IEA500I"}]})


def test_body_budget_is_fifo_and_never_exceeds_capacity():
    async def main():
        budget = BodyBudget(100)
        a = await budget.acquire(60, 1)
        assert budget.in_use == 60
        big = asyncio.ensure_future(budget.acquire(70, 5))
        await _until(lambda: budget.waiting == 1, "big waits")
        small = asyncio.ensure_future(budget.acquire(10, 5))
        await _until(lambda: budget.waiting == 2, "small queued behind big")
        await asyncio.sleep(0.01)
        assert not small.done(), "a late small body never overtakes a queued large one"
        a.release()
        a.release()  # idempotent
        b = await asyncio.wait_for(big, 1)
        s = await asyncio.wait_for(small, 1)
        assert budget.in_use == 80 and budget.waiting == 0
        assert b.waited_s > 0
        b.release()
        s.release()
        assert budget.in_use == 0

    _run(main)


def test_body_budget_timeout_refuses_and_a_leaving_head_lets_followers_in():
    async def main():
        budget = BodyBudget(100)
        holder = await budget.acquire(50, 1)
        with pytest.raises(AdmissionRejected) as refused:
            await budget.acquire(60, 0)  # no wait allowed
        assert refused.value.reason == admission_mod.REASON_BODY_BUDGET
        head = asyncio.ensure_future(budget.acquire(60, 0.05))
        await _until(lambda: budget.waiting == 1, "head waits")
        follower = asyncio.ensure_future(budget.acquire(40, 5))
        await _until(lambda: budget.waiting == 2, "follower queued")
        with pytest.raises(AdmissionRejected):
            await head  # times out ...
        f = await asyncio.wait_for(follower, 1)  # ... and the follower fits now
        assert budget.in_use == 90 and budget.waiting == 0
        f.release()
        holder.release()
        assert budget.in_use == 0

    _run(main)


def test_body_budget_cancelled_waiter_that_was_just_granted_returns_the_bytes():
    async def main():
        budget = BodyBudget(100)
        holder = await budget.acquire(100, 1)
        waiter = asyncio.ensure_future(budget.acquire(100, 5))
        await _until(lambda: budget.waiting == 1, "waiter queued")
        holder.release()  # hands the bytes to the waiter's future ...
        waiter.cancel()  # ... which is cancelled before it resumes
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert budget.in_use == 0 and budget.waiting == 0
        (await budget.acquire(100, 0)).release()
        assert BodyBudget().capacity == 0  # import-time instance: unlimited
        assert (await BodyBudget().acquire(10**12, 0)).nbytes == 0

    _run(main)


def _body_env(env, *, budget=MIB, wait="0.2"):
    env.setenv("REQUEST_MAX_BODY_BYTES", str(MIB))
    env.setenv("REQUEST_BODY_BUDGET_BYTES", str(budget))
    env.setenv("REQUEST_QUEUE_WAIT_S", wait)


@pytest.mark.parametrize("path", ["/v1/search", "/v1/answer", "/v1/chat", "/ui/chat"])
def test_declared_over_cap_body_is_refused_before_anything_reads_it(env, path):
    _body_env(env)

    async def main():
        search = InstantSearch()
        async with _serving(env, search):
            call = _Call(path, {"query": "IEA500I"}, framing="length", declared=MIB + 1).start()
            await call.done()
            assert (call.status, call.json()) == (413, TOO_LARGE_1MIB)
            assert "connection" not in call.headers, "the server discards the rest; a close would reset it"
            assert call.receives == 0 and search.calls == 0
            assert app_mod.body_budget.in_use == 0

    _run(main)


@pytest.mark.parametrize(("path", "body"), [SEARCH, CHAT])
def test_streamed_over_cap_body_is_the_fixed_413_not_a_parse_failure(env, path, body):
    """A body without a declared length is counted as it streams: past the cap
    it is refused with the 413 envelope (never the framework's 400/422), the
    budget is returned and the next ordinary request is served."""
    _body_env(env, budget=2 * MIB)
    red: list[tuple[str, str]] = []
    real_record = app_mod.record_request

    def spy_record(endpoint, outcome, *args, **kwargs):
        red.append((endpoint, outcome))
        return real_record(endpoint, outcome, *args, **kwargs)

    env.setattr(app_mod, "record_request", spy_record)
    rejected: list[tuple] = []
    env.setattr(app_mod, "record_admission_rejected", lambda *a: rejected.append(a))

    async def main():
        search = InstantSearch()
        async with _serving(env, search):
            chunks = [b"x" * (MIB // 2)] * 3
            call = await _Call(path, body, framing="chunked", chunks=chunks).start().done()
            assert (call.status, call.json()) == (413, TOO_LARGE_1MIB)
            assert "connection" not in call.headers
            assert search.calls == 0
            assert app_mod.body_budget.in_use == 0
            # One RED record for the refusal (the app's swallowed reaction and
            # the middleware's envelope never both count); no admission label.
            assert [outcome for _, outcome in red] == ["invalid_request"]
            assert rejected == []
            ok = await _Call(path, body, framing="chunked").start().done()
            assert ok.status == 200 and search.calls == 1
            assert app_mod.body_budget.in_use == 0

    _run(main)


def test_body_waits_for_budget_without_reading_then_gets_the_overload_code(env):
    _body_env(env)

    async def main():
        search = InstantSearch()
        async with _serving(env, search):
            holder = await app_mod.body_budget.acquire(MIB, 1)
            waiting = _Call(*SEARCH, framing="length").start()
            await _until(lambda: app_mod.body_budget.waiting == 1, "request waits for budget")
            assert waiting.receives == 0, "a waiting request reads nothing"
            probe = await _Call("/livez", {}, method="GET").start().done()
            assert (probe.status, probe.json()) == (200, {"status": "alive"})
            refused = await waiting.done()
            assert (refused.status, refused.json()) == (503, OVERLOADED)
            assert refused.headers["retry-after"] == "1"
            assert "connection" not in refused.headers
            assert refused.receives == 0 and search.calls == 0
            assert app_mod.body_budget.waiting == 0

            queued = _Call(*SEARCH, framing="length").start()
            await _until(lambda: app_mod.body_budget.waiting == 1, "next request waits")
            holder.release()
            served = await queued.done()
            assert served.status == 200 and search.calls == 1
            assert "connection" not in served.headers
            assert app_mod.body_budget.in_use == 0

    _run(main)


def test_chat_is_admitted_before_its_body_is_read_or_budgeted(env):
    """Admission first on /v1/chat*: a refused or queued chat has read no byte
    and holds no budget, so the admission queue keeps its immediate refusal."""
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    _body_env(env, wait="5")

    async def main():
        search = BlockingSearch()
        async with _serving(env, search):
            holder = _Call(*SEARCH, framing="length").start()
            await _until(lambda: search.calls == 1, "search holds the slot")
            held = app_mod.body_budget.in_use
            assert held == len(holder.payload)
            queued = _Call(*CHAT, framing="length").start()
            await _until(lambda: app_mod.admission.queued == 1, "chat queued for admission")
            assert queued.receives == 0 and app_mod.body_budget.in_use == held
            refused = await _Call(*CHAT, framing="length").start().done()
            assert (refused.status, refused.json()) == (503, OVERLOADED)
            assert refused.receives == 0 and app_mod.body_budget.in_use == held
            # Kept open: the server discards the unread body after the response,
            # so a client still uploading receives the envelope, not a reset.
            assert "connection" not in refused.headers
            search.gate.set()
            assert (await holder.done()).status == 200
            assert (await queued.done()).status == 200
            assert queued.receives == 1 and "connection" not in queued.headers
            assert app_mod.body_budget.in_use == 0 and app_mod.admission.active == 0

    _run(main)


def test_chat_core_runs_without_the_raw_body_or_parsed_json_on_the_request(env):
    """The bytes and the parsed dict are locals of the parse, never cached on
    the Request, which lives as long as the response (issue #1352)."""
    _body_env(env)
    seen: list[tuple[bool, bool]] = []
    real = app_mod._chat_response

    async def spy(req, response, owner):
        seen.append((hasattr(owner.request, "_body"), hasattr(owner.request, "_json")))
        return await real(req, response, owner)

    env.setattr(app_mod, "_chat_response", spy)

    async def main():
        async with _serving(env, InstantSearch()):
            ok = await _Call(*CHAT, framing="length").start().done()
            assert ok.status == 200

    _run(main)
    assert seen == [(False, False)]


def test_admitted_chat_waits_for_body_budget_then_gets_the_overload_code(env):
    _body_env(env)

    async def main():
        async with _serving(env, InstantSearch()):
            holder = await app_mod.body_budget.acquire(MIB, 1)
            refused = await _Call(*CHAT, framing="length").start().done()
            assert (refused.status, refused.json()) == (503, OVERLOADED)
            assert refused.headers["retry-after"] == "1" and refused.receives == 0
            holder.release()
            ok = await _Call(*CHAT, framing="length").start().done()
            assert ok.status == 200 and app_mod.body_budget.in_use == 0

    _run(main)


def test_chat_stream_holds_its_body_bytes_until_disconnect_then_releases_them(env):
    _body_env(env, wait="5")

    async def main():
        llm = ParkedStreamLLM()
        async with _serving(env, InstantSearch(), llm):
            body = {"messages": [{"role": "user", "content": "IEA500I"}], "stream": True}
            stream = _Call("/v1/chat", body, framing="length").start()
            await asyncio.wait_for(stream.first_chunk.wait(), 5)
            assert stream.status == 200
            assert app_mod.body_budget.in_use == len(stream.payload)
            stream.disconnect()
            await stream.done()
            await asyncio.wait_for(llm.closed.wait(), 5)
            assert app_mod.body_budget.in_use == 0
            after = await _Call(*CHAT, framing="length").start().done()
            assert after.status == 200 and app_mod.body_budget.in_use == 0

    _run(main)


def test_chat_disconnect_mid_upload_releases_slot_and_budget(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    _body_env(env, wait="5")

    async def main():
        search = InstantSearch()
        async with _serving(env, search):
            call = _Call(*CHAT, framing="chunked", chunks=[b'{"messages": ', None]).start()
            await _until(lambda: call.receives == 1, "first chunk read")
            call.disconnect()
            await call.done()
            assert search.calls == 0
            assert app_mod.admission.active == 0 and app_mod.body_budget.in_use == 0
            after = await _Call(*CHAT, framing="length").start().done()
            assert after.status == 200

    _run(main)


@contextlib.asynccontextmanager
async def _uvicorn(monkeypatch, search):
    """The app behind a real uvicorn socket (h11, as deployed), port 0."""
    import uvicorn

    async with _serving(monkeypatch, search):
        config = uvicorn.Config(app_mod.app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        try:
            yield server.servers[0].sockets[0].getsockname()[1]
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)


async def _read_http_response(reader) -> tuple[int, dict[str, str], bytes]:
    head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
    headers = dict(line.lower().split(": ", 1) for line in head[1:] if ": " in line)
    body = await reader.readexactly(int(headers["content-length"]))
    return int(head[0].split()[1]), headers, body


@pytest.mark.parametrize("framing", ["length", "chunked"])
@pytest.mark.parametrize("path,payload", [SEARCH, CHAT], ids=["search", "chat"])
def test_over_cap_upload_on_a_real_socket_gets_the_413_and_is_never_buffered(env, path, payload, framing):
    """#1356 review: a client still uploading an over-cap body (declared or
    chunked) receives the fixed 413 envelope, not a connection reset. The
    rest of the body is discarded by the server, never buffered (traced
    allocations stay far below the upload), and the same keep-alive
    connection then serves an ordinary request."""
    import tracemalloc

    _body_env(env)
    total, piece = 16 * MIB, b"x" * (256 * 1024)
    ordinary = json.dumps(payload).encode()

    async def main():
        search = InstantSearch()
        async with _uvicorn(env, search) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            framing_header = (b"Content-Length: %d" % total if framing == "length"
                              else b"Transfer-Encoding: chunked")
            writer.write(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: t\r\n"
                         b"Content-Type: application/json\r\n" + framing_header + b"\r\n\r\n")

            async def upload():
                for _ in range(total // len(piece)):
                    writer.write(b"%x\r\n%s\r\n" % (len(piece), piece) if framing == "chunked" else piece)
                    await writer.drain()
                if framing == "chunked":
                    writer.write(b"0\r\n\r\n")
                    await writer.drain()

            tracemalloc.start()
            try:
                tracemalloc.reset_peak()
                sending = asyncio.create_task(upload())
                status, headers, body = await asyncio.wait_for(_read_http_response(reader), 20)
                await asyncio.wait_for(sending, 20)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            assert (status, json.loads(body)) == (413, TOO_LARGE_1MIB)
            assert "connection" not in headers
            assert search.calls == 0
            assert peak < total // 4, f"over-cap body buffered: traced peak {peak} bytes"
            writer.write(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: t\r\n"
                         b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(ordinary)
                         + ordinary)
            await writer.drain()
            status, _, _ = await asyncio.wait_for(_read_http_response(reader), 20)
            assert status == 200 and search.calls == 1
            writer.close()
            await _until(lambda: app_mod.body_budget.in_use == 0, "budget returned")

    _run(main)


@pytest.mark.parametrize("call", [SEARCH, CHAT], ids=["search", "chat"])
def test_stalled_upload_ends_at_the_deadline_and_releases_budget_and_slot(env, call):
    """D-A (#1356 review): an upload that stops sending holds its budget
    bytes (and on chat its admission slot) only until the request deadline,
    then gets the fixed 504; the next ordinary request is served."""
    _body_env(env, wait="5")
    env.setenv("REQUEST_DEADLINE_S", "0.5")
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "0")
    red: list[str] = []
    real_record = app_mod.record_request

    def spy_record(endpoint, outcome, *args, **kwargs):
        red.append(outcome)
        return real_record(endpoint, outcome, *args, **kwargs)

    env.setattr(app_mod, "record_request", spy_record)

    async def main():
        search = InstantSearch()
        async with _serving(env, search):
            stalled = _Call(*call, framing="chunked", chunks=[b'{"', None]).start()
            await _until(lambda: stalled.receives == 1, "upload started")
            assert app_mod.body_budget.in_use == MIB
            refused = await stalled.done()
            assert (refused.status, refused.json()) == (504, DEADLINE)
            assert search.calls == 0 and red == ["deadline_exceeded"]
            assert app_mod.body_budget.in_use == 0 and app_mod.admission.active == 0
            ok = await _Call(*call, framing="length").start().done()
            assert ok.status == 200 and search.calls == 1
            assert app_mod.body_budget.in_use == 0 and app_mod.admission.active == 0

    _run(main)


def test_openapi_describes_the_chat_request_body_as_before():
    """The chat handler parses its own body (issue #1352); /openapi.json still
    publishes exactly the pre-#1352 request body, schemas, 422 response and
    description for both chat routes (pinned from origin/main)."""
    pinned = json.loads((Path(__file__).parent / "fixtures" / "openapi-chat-body.json").read_text())
    app_mod.app.openapi_schema = None
    spec = app_mod.app.openapi()
    for path, expected in pinned["operations"].items():
        operation = spec["paths"][path]["post"]
        assert {"requestBody": operation["requestBody"], "422": operation["responses"]["422"],
                "description": operation["description"]} == expected
    for name, schema in pinned["schemas"].items():
        assert spec["components"]["schemas"][name] == schema, name
    refs = re.findall(r'"\$ref": "#/components/schemas/([^"]+)"', json.dumps(spec))
    assert set(refs) <= set(spec["components"]["schemas"]), "every reference resolves"
