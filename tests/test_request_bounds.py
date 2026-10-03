"""Request admission, total deadline and embed-input bound (issue #374).

Hermetic: the real FastAPI app is driven over raw ASGI inside one event loop
(so a stream can be held open and a client disconnect delivered), with fake
retrieval and model legs. No network, no GPU, no Qdrant.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import pytest

from mainframe_rag.agent import admission as admission_mod
from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.admission import AdmissionController, AdmissionRejected
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.ingest.bounds import EmbedInputTooLarge
from tests.test_agent_api import FakeLLM, _hit

OVERLOADED = {"code": "overloaded", "message": "the service is at capacity; retry later"}
DEADLINE = {"code": "deadline_exceeded", "message": "request deadline exceeded"}
INVALID = {"code": "invalid_request", "message": "request body failed validation"}


@pytest.fixture
def env(monkeypatch, servable_representation_gate):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
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
    ) -> None:
        self.method = method
        self.path = path
        self.query = query.encode()
        self.content_type = b"application/x-www-form-urlencoded" if form else b"application/json"
        self.payload = (
            urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        )
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""
        self.first_chunk = asyncio.Event()
        self._sent_request = False
        self._disconnect = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def _receive(self):
        if not self._sent_request:
            self._sent_request = True
            return {"type": "http.request", "body": self.payload, "more_body": False}
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
            "headers": [(b"content-type", self.content_type)],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }
        self.task = asyncio.ensure_future(app_mod.app(scope, self._receive, self._send))
        return self

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


async def _serve(monkeypatch, search, llm=None):
    """Enter the real lifespan (settings from env), then install the fakes
    the same way the TestClient fixture does."""
    cm = app_mod.lifespan(app_mod.app)
    await cm.__aenter__()
    monkeypatch.setattr(app_mod, "retrieve_search", search)
    monkeypatch.setattr(app_mod, "llm", llm or FakeLLM())
    monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
    return cm


def _run(coro_factory):
    asyncio.run(coro_factory())


SEARCH = ("/v1/search", {"query": "IEA500I"})


# ------------------------------------------------------------ controller unit


def test_controller_unlimited_admits_everything_and_counts_nothing():
    async def main():
        ctl = AdmissionController()
        tickets = [await ctl.acquire() for _ in range(50)]
        assert ctl.active == 0 and ctl.queued == 0
        assert not any(t.held for t in tickets)

    _run(main)


@pytest.mark.parametrize("deadline", ["0", "0.2"])
def test_stalled_source_cleanup_is_bounded_and_releases_admission_even_without_deadline(env, deadline):
    from fastapi import Request

    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", deadline)

    async def main():
        cm = await _serve(env, InstantSearch())
        closed = asyncio.Event()

        class Source:
            async def aclose(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        request = Request({"type": "http", "state": {"request_id": "cleanup-probe"}})
        started = app_mod.time.monotonic()
        span = app_mod.trace.get_tracer(__name__).start_span("cleanup-probe")
        owner = app_mod._RequestSpan(request, span, "answer", started)
        try:
            await app_mod._admit(owner)
            stream = app_mod._SpanStream(Source(), owner)
            assert app_mod.admission.active == 1
            await asyncio.wait_for(stream.aclose(), 2)
            assert app_mod.time.monotonic() - started < 1.35
            assert closed.is_set() and owner.ended and app_mod.admission.active == 0
            await stream.aclose()
            assert (await _Call(*SEARCH).start().done()).status == 200
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


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
        holder.release()
        assert ctl.active == 0
        (await ctl.acquire()).release()  # next ordinary request is admitted

    _run(main)


def test_controller_wait_cap_below_queue_wait_applies():
    async def main():
        ctl = AdmissionController(max_active=1, max_queue=1, queue_wait_s=30)
        holder = await ctl.acquire()
        started = asyncio.get_running_loop().time()
        with pytest.raises(AdmissionRejected):
            await ctl.acquire(0.05)
        assert asyncio.get_running_loop().time() - started < 2
        with pytest.raises(AdmissionRejected):
            await ctl.acquire(0.0)
        holder.release()

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
        cm = await _serve(env, search)
        try:
            calls = [_Call(*SEARCH).start() for _ in range(6)]
            await _until(lambda: search.calls == 6, "all six requests in the retrieval leg")
            search.gate.set()
            for c in calls:
                assert (await c.done()).status == 200
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_overload_returns_stable_code_does_no_work_and_recovers(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        search = BlockingSearch()
        cm = await _serve(env, search)
        try:
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
        finally:
            await cm.__aexit__(None, None, None)

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
        cm = await _serve(env, search)
        try:
            holder = _Call("/v1/search", SEARCH[1]).start()
            await _until(lambda: search.calls == 1, "slot held")
            refused = await _Call(path, body, query).start().done()
            assert (refused.status, refused.json()) == (503, OVERLOADED)
            assert search.calls == 1
            search.gate.set()
            await holder.done()
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_probes_are_never_admitted_or_limited(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        search = BlockingSearch()
        cm = await _serve(env, search)
        try:
            holder = _Call(*SEARCH).start()
            await _until(lambda: search.calls == 1, "slot held")
            probe = await _Call("/livez", {}, method="GET").start().done()
            assert (probe.status, probe.json()) == (200, {"status": "alive"})
            search.gate.set()
            await holder.done()
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_bounded_queue_admits_in_order_and_refuses_beyond_it(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_QUEUE_WAIT_S", "5")

    async def main():
        search = BlockingSearch()
        cm = await _serve(env, search)
        try:
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
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_queue_wait_expiry_is_the_overload_code(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_QUEUE_WAIT_S", "0.05")

    async def main():
        search = BlockingSearch()
        cm = await _serve(env, search)
        try:
            first = _Call(*SEARCH).start()
            await _until(lambda: search.calls == 1, "first holds the slot")
            late = await _Call(*SEARCH).start().done()
            assert (late.status, late.json()) == (503, OVERLOADED)
            assert app_mod.admission.queued == 0
            search.gate.set()
            await first.done()
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_stream_holds_slot_until_disconnect_then_releases_exactly_once(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        llm = ParkedStreamLLM()
        cm = await _serve(env, InstantSearch(), llm)
        try:
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
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_slot_is_released_when_the_request_fails(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        async def broken(*a, **k):
            raise RuntimeError("upstream exploded with secret text")

        cm = await _serve(env, broken)
        try:
            failed = await _Call(*SEARCH).start().done()
            assert failed.status == 502
            assert "secret" not in failed.body.decode()
            assert app_mod.admission.active == 0
            app_mod.retrieve_search = InstantSearch()
            assert (await _Call(*SEARCH).start().done()).status == 200
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


# ------------------------------------------------------------------- deadline


def test_deadline_cancels_the_leg_cleanly_and_the_next_request_runs(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.2")

    async def main():
        search = BlockingSearch()
        cm = await _serve(env, search)
        try:
            expired = await _Call(*SEARCH).start().done()
            assert (expired.status, expired.json()) == (504, DEADLINE)
            assert search.cancelled == 1, "the awaiting retrieval leg is cancelled"
            assert app_mod.admission.active == 0

            monkey = InstantSearch()
            app_mod.retrieve_search = monkey
            nxt = await _Call(*SEARCH).start().done()
            assert nxt.status == 200 and monkey.calls == 1
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_deadline_bounds_the_queue_wait_too(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_QUEUE_WAIT_S", "60")
    env.setenv("REQUEST_DEADLINE_S", "30")

    async def main():
        search = BlockingSearch()
        cm = await _serve(env, search)
        try:
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
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_deadline_mid_stream_ends_with_terminal_error_frame_and_frees_the_slot(env):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.3")

    async def main():
        llm = ParkedStreamLLM()
        cm = await _serve(env, InstantSearch(), llm)
        try:
            call = await _Call("/v1/answer", {"query": "IEA500I"}, "stream=true").start().done()
            text = call.body.decode()
            assert call.status == 200
            assert "event: token" in text and "event: error" in text
            assert "event: final" not in text, "an expired stream must not claim completion"
            await asyncio.wait_for(llm.closed.wait(), 5)
            assert app_mod.admission.active == 0
            assert (await _Call(*SEARCH).start().done()).status == 200
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


# ------------------------------------------------------------ embed-input bound


@pytest.mark.parametrize("endpoint", ["search", "answer", "chat"])
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
        cm = await _serve(env, search, llm)
        try:
            over = await _Call(*body("x" * 41)).start().done()
            assert (over.status, over.json()) == (422, INVALID)
            assert search.calls == 0 and llm.calls == 0, "no embed/retrieval/LLM work"

            at_limit = await _Call(*body("x" * 40)).start().done()
            assert at_limit.status == 200 and search.calls == 1
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


@pytest.mark.parametrize("endpoint", ["search", "answer", "chat"])
def test_embedder_refusal_backstop_is_a_422_not_an_upstream_error(env, endpoint):
    async def refuse(*a, **k):
        raise EmbedInputTooLarge(100, 150, 1)

    def body():
        if endpoint == "chat":
            return "/v1/chat", {"messages": [{"role": "user", "content": "IEA500I"}]}
        return f"/v1/{endpoint}", {"query": "IEA500I"}

    async def main():
        llm = FakeLLM()
        cm = await _serve(env, refuse, llm)
        try:
            resp = await _Call(*body()).start().done()
            assert (resp.status, resp.json()) == (422, INVALID)
            assert llm.calls == 0
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_default_embed_bound_is_off_and_long_queries_pass_unchanged(env):
    async def main():
        search = InstantSearch()
        cm = await _serve(env, search)
        try:
            ok = await _Call("/v1/search", {"query": "x" * 1999}).start().done()
            assert ok.status == 200 and search.calls == 1
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


# -------------------------------------------------------------------- console


def test_console_shares_the_admission_slot_and_deadline(env):
    env.setenv("UI_ENABLED", "true")
    env.setenv("REQUEST_MAX_CONCURRENT", "1")

    async def main():
        search = BlockingSearch()
        cm = await _serve(env, search)
        try:
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
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_console_stream_deadline_is_a_terminal_error_frame(env):
    env.setenv("UI_ENABLED", "true")
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.3")

    async def main():
        llm = ParkedStreamLLM()
        cm = await _serve(env, InstantSearch(), llm)
        try:
            call = await _Call(
                "/ui/chat/stream", {"messages": [{"role": "user", "content": "IEA500I"}]}
            ).start().done()
            text = call.body.decode()
            assert call.status == 200
            assert "event: error" in text and "event: final" not in text
            await asyncio.wait_for(llm.closed.wait(), 5)
            assert app_mod.admission.active == 0
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


def test_console_non_stream_deadline_renders_the_fixed_banner_and_frees_the_slot(env):
    env.setenv("UI_ENABLED", "true")
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.2")

    async def main():
        search = BlockingSearch()
        cm = await _serve(env, search)
        try:
            form = await _Call(
                "/ui/chat", {"message": "IEA500I", "messages": ""}, form=True
            ).start().done()
            assert form.status == 502 and b"could not complete" in form.body
            assert search.cancelled == 1 and app_mod.admission.active == 0
        finally:
            await cm.__aexit__(None, None, None)

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
        cm = await _serve(env, search)
        env.setattr(app_mod, "qdrant", qd)
        ref = _expected_ref(BUILD_A, _expected_envelope())
        try:
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
        finally:
            gate.set()
            await cm.__aexit__(None, None, None)

    _run(main)


@pytest.mark.parametrize("endpoint", ["answer", "chat", "console"])
def test_stream_deadline_closes_stalled_asgi_send_and_admits_next_queued_request(env, endpoint):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.4")
    env.setenv("UI_ENABLED", "true")

    async def main():
        llm = ParkedStreamLLM()
        cm = await _serve(env, InstantSearch(), llm)
        send_entered, send_closed = asyncio.Event(), asyncio.Event()
        released = []
        release = admission_mod.AdmissionTicket.release

        def record_release(ticket):
            if ticket.held:
                released.append(ticket)
            return release(ticket)

        env.setattr(admission_mod.AdmissionTicket, "release", record_release)
        path = "/ui/chat/stream" if endpoint == "console" else f"/v1/{endpoint}"
        body = {"query": "IEA500I", "stream": True} if endpoint == "answer" else {
            "messages": [{"role": "user", "content": "IEA500I"}], "stream": True,
        }
        if endpoint == "console":
            body.pop("stream")
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
        try:
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
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


@pytest.mark.parametrize("endpoint", ["answer", "chat", "console"])
def test_producer_deadline_terminal_frames_share_one_absolute_delivery_grace(env, endpoint):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.2")
    env.setenv("UI_ENABLED", "true")

    async def main():
        llm = ParkedStreamLLM()
        cm = await _serve(env, InstantSearch(), llm)
        path = "/ui/chat/stream" if endpoint == "console" else f"/v1/{endpoint}"
        body = {"query": "IEA500I", "stream": True} if endpoint == "answer" else {
            "messages": [{"role": "user", "content": "IEA500I"}],
        }
        if endpoint == "chat":
            body["stream"] = True
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
        try:
            await call.start().done()
            assert grace_started is not None and sends >= 2
            assert asyncio.get_running_loop().time() - grace_started < 1.35
            assert llm.closed.is_set() and app_mod.admission.active == 0
            assert b"event: final" not in call.body
            assert (await _Call(*SEARCH).start().done()).status == 200
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)


@pytest.mark.parametrize("endpoint", ["answer", "chat", "console"])
def test_deadline_and_admission_include_the_closing_asgi_body_send(env, endpoint):
    env.setenv("REQUEST_MAX_CONCURRENT", "1")
    env.setenv("REQUEST_QUEUE_MAX", "1")
    env.setenv("REQUEST_DEADLINE_S", "0.4")
    env.setenv("UI_ENABLED", "true")

    async def main():
        cm = await _serve(env, InstantSearch())
        closing = asyncio.Event()
        path = "/ui/chat/stream" if endpoint == "console" else f"/v1/{endpoint}"
        body = {"query": "IEA500I", "stream": True} if endpoint == "answer" else {
            "messages": [{"role": "user", "content": "IEA500I"}],
        }
        if endpoint == "chat":
            body["stream"] = True
        call = _Call(path, body)
        actual_send = call._send

        async def block_closing_body(message):
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                closing.set()
                await asyncio.Event().wait()
            await actual_send(message)

        call._send = block_closing_body
        try:
            call.start()
            await asyncio.wait_for(closing.wait(), 3)
            assert app_mod.admission.active == 1
            await asyncio.sleep(0.06)
            next_call = _Call(*SEARCH).start()
            await _until(lambda: app_mod.admission.queued == 1, "next request queued")
            await call.done()
            await next_call.done()
            assert next_call.status == 200 and app_mod.admission.active == 0
        finally:
            await cm.__aexit__(None, None, None)

    _run(main)
