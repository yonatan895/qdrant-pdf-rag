"""Characterization of the /v1/answer and /v1/chat orchestration (issue #583).

The two route bodies share their gate/retrieve/finalize/SSE shape. This suite
pins the full externally observable outcome of each path so the shared
helpers can be extracted without any change: the exact response bytes
(JSON body or raw SSE frames), the Server-Timing and SSE headers, and ONE
ordered timeline of every log line and RED (`record_request`) observation,
plus the root SERVER span's attributes, status and events.

Golden values live in tests/data/answer_chat_orchestration.json and were
captured from the pre-refactor code (origin/main). Time-varying fields
(elapsed/llm milliseconds, `created`) are normalized; request ids are fixed.
Hermetic: scripted LLM, patched retrieval, no network.
"""

import contextlib
import json
import logging
import re
import types
from pathlib import Path

import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import PromptBudgetExceeded, TruncatedStreamError
from mainframe_rag.agent.app import AnswerRequest, ChatRequest
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.ports import ChatResult, TokenUsage
from tests.test_stream_truncation import _client, _scope, _search_stub

GOLDEN_PATH = Path(__file__).parent / "data" / "answer_chat_orchestration.json"
_CITE = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
_BODY = (
    f"Reissue the command.\n\n```jcl\n// example only\nIOSCMDS LIST\n```\n\nCitations:\n- {_CITE}\n"
)
_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=25, reasoning_tokens=5, total_tokens=40)
_FIXED_ID = "feedfacecafe"
_NORMALIZE = [
    (re.compile(r'"(elapsed_ms|llm_ms|created)":\s*\d+'), r'"\1": N'),
    (re.compile(r"llm;dur=\d+"), "llm;dur=N"),
]


def _norm(text: str) -> str:
    for pattern, repl in _NORMALIZE:
        text = pattern.sub(repl, text)
    return text


def _check(name: str, observed: dict) -> None:
    golden = json.loads(GOLDEN_PATH.read_text())
    assert observed == golden[name]


class _ScriptedLLM:
    """Streams the scripted token/done items (then optionally raises);
    non-stream chat returns the scripted result or raises."""

    def __init__(self, *, finish="stop", stream_exc=None, chat_exc=None, hang=False):
        self.finish = finish
        self.stream_exc = stream_exc
        self.chat_exc = chat_exc
        self.hang = hang

    def chat(self, messages, *args, **kwargs):
        if self.chat_exc is not None:
            raise self.chat_exc
        return ChatResult(content=_BODY, finish_reason=self.finish, usage=_USAGE, ttft_ms=7)

    async def chat_stream(self, messages, *args, **kwargs):
        yield {"type": "token", "delta": "Reissue the command.\n\n", "ttft_ms": 12}
        if self.hang:
            import asyncio

            await asyncio.Event().wait()
        if self.stream_exc is not None:
            raise self.stream_exc
        yield {
            "type": "token",
            "delta": f"```jcl\n// example only\nIOSCMDS LIST\n```\n\nCitations:\n- {_CITE}\n",
            "ttft_ms": 12,
        }
        yield {"type": "done", "finish_reason": self.finish, "usage": _USAGE, "ttft_ms": 12}


class _Timeline:
    """One ordered list of log lines and RED observations, plus root spans."""

    def __init__(self, monkeypatch):
        self.events: list[str] = []
        monkeypatch.setattr(app_mod, "record_request", self._red)
        monkeypatch.setattr(
            app_mod.uuid, "uuid4", lambda: types.SimpleNamespace(hex=_FIXED_ID + "0123456789ab")
        )
        timeline = self

        class _Handler(logging.Handler):
            def emit(self, record):
                timeline.events.append(f"LOG {record.levelname} {_norm(record.getMessage())}")

        self._logger = logging.getLogger("agent")
        self._handler = _Handler(level=logging.DEBUG)
        self._old_level = self._logger.level
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        self.exporter = InMemorySpanExporter()
        self._monkeypatch = monkeypatch

    def trace(self):
        """Swap the tracer AFTER lifespan startup (it rebinds the module tracer)."""
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self._monkeypatch.setattr(app_mod, "tracer", provider.get_tracer("orchestration-test"))

    def close(self):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._old_level)

    def _red(self, endpoint, outcome, **kw):
        kw.pop("elapsed_s", None)
        self.events.append(f"RED {endpoint} {outcome} {json.dumps(kw, sort_keys=True)}")

    def spans(self, name: str) -> list[dict]:
        out = []
        for span in self.exporter.get_finished_spans():
            if span.name != name:
                continue
            out.append(
                {
                    "attributes": {k: span.attributes[k] for k in sorted(span.attributes)},
                    "status": span.status.status_code.name,
                    "status_description": span.status.description,
                    "events": [[e.name, dict(e.attributes or {})] for e in span.events],
                    "ended": span.end_time is not None,
                }
            )
        return out


@contextlib.contextmanager
def _timeline(monkeypatch):
    timeline = _Timeline(monkeypatch)
    try:
        yield timeline
    finally:
        timeline.close()


def _retrieval(monkeypatch, *, hits: bool):
    stub = _search_stub()

    def retrieve(*args, **kwargs):
        found, kind, timings = stub.search()
        return (found if hits else []), kind, timings

    monkeypatch.setattr(app_mod, "retrieve_search", retrieve)


def _raiser(exc):
    def run(*args, **kwargs):
        raise exc

    return run


def _raising_stream(exc):
    async def run(*args, **kwargs):
        raise exc
        yield  # pragma: no cover - makes this an async generator

    return run


def _scenario(monkeypatch, name: str) -> _ScriptedLLM:
    _retrieval(monkeypatch, hits=name != "no_hits")
    if name == "budget_stream":
        monkeypatch.setattr(
            app_mod, "execute_answer_core_stream", _raising_stream(PromptBudgetExceeded(9, 4))
        )
    if name == "budget_json":
        monkeypatch.setattr(app_mod, "execute_answer_core", _raiser(PromptBudgetExceeded(9, 4)))
    return {
        "hang": _ScriptedLLM(hang=True),
        "length": _ScriptedLLM(finish="length"),
        "runtime_error": _ScriptedLLM(stream_exc=RuntimeError("boom"), chat_exc=RuntimeError("x")),
        "truncated": _ScriptedLLM(stream_exc=TruncatedStreamError(1)),
        "llm_error": _ScriptedLLM(chat_exc=RuntimeError("upstream SECRET")),
    }.get(name, _ScriptedLLM())


def _request(client: TestClient, endpoint: str, stream: bool):
    if endpoint == "answer":
        return client.post(
            "/v1/answer" + ("?stream=true" if stream else ""), json={"query": "IEA500I command"}
        )
    return client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "IEA500I command"}], "stream": stream},
    )


def _observe(client, timeline, endpoint, stream):
    resp = _request(client, endpoint, stream)
    headers = {
        k: resp.headers[k]
        for k in ("content-type", "cache-control", "x-accel-buffering", "server-timing")
        if k in resp.headers
    }
    if "server-timing" in headers:
        headers["server-timing"] = _norm(headers["server-timing"])
    return {
        "status": resp.status_code,
        "headers": headers,
        "body": _norm(resp.text),
        "timeline": timeline.events,
        "root_spans": timeline.spans("v1." + endpoint),
    }


_STREAM_SCENARIOS = ["ok", "length", "no_hits", "runtime_error", "truncated", "budget_stream"]
_JSON_SCENARIOS = ["ok", "length", "no_hits", "budget_json", "llm_error"]


@pytest.fixture
def start(monkeypatch, synthetic_pdf, servable_representation_gate):
    """start(scenario) -> (client, timeline); everything is torn down after."""
    with contextlib.ExitStack() as stack:

        def run(scenario):
            timeline = stack.enter_context(_timeline(monkeypatch))
            llm = _scenario(monkeypatch, scenario)
            client = stack.enter_context(
                contextlib.contextmanager(_client)(monkeypatch, synthetic_pdf, llm)
            )
            monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
            timeline.trace()
            return client, timeline

        yield run


@pytest.mark.parametrize("endpoint", ["answer", "chat"])
@pytest.mark.parametrize("scenario", _STREAM_SCENARIOS)
def test_stream_outcome_is_pinned(start, endpoint, scenario):
    client, timeline = start(scenario)
    _check(f"{endpoint}/stream/{scenario}", _observe(client, timeline, endpoint, True))


@pytest.mark.parametrize("endpoint", ["answer", "chat"])
@pytest.mark.parametrize("scenario", _JSON_SCENARIOS)
def test_json_outcome_is_pinned(start, endpoint, scenario):
    client, timeline = start(scenario)
    _check(f"{endpoint}/json/{scenario}", _observe(client, timeline, endpoint, False))


@pytest.mark.anyio
@pytest.mark.parametrize("endpoint", ["answer", "chat"])
@pytest.mark.parametrize("scenario,frames", [("hang", 1), ("truncated", 2), ("ok", 3)])
async def test_close_outcome_is_pinned(start, endpoint, scenario, frames):
    """Close the body at a frame boundary the way a client disconnect does:
    mid-stream (hang), right after a terminal error frame, right after the
    final frame (a terminal frame is never a second abort)."""
    _, timeline = start(scenario)
    if endpoint == "answer":
        response = await app_mod.v1_answer(
            Request(_scope("/v1/answer")),
            AnswerRequest(query="IEA500I command"),
            Response(),
            stream=True,
        )
    else:
        response = await app_mod.chat_completions(
            ChatRequest(messages=[{"role": "user", "content": "IEA500I command"}], stream=True),
            Request(_scope("/v1/chat")),
            Response(),
        )
    body = response.body_iterator
    seen = [_norm(await body.__anext__()) for _ in range(frames)]
    await body.aclose()
    _check(
        f"{endpoint}/close/{scenario}",
        {
            "frames": seen,
            "timeline": timeline.events,
            "root_spans": timeline.spans("v1." + endpoint),
        },
    )
