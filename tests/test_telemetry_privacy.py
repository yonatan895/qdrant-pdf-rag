"""Source-privacy canaries (issue #529 OBS-1A §4.4).

Prohibited content — raw query text, header values, exception bodies,
document identifiers — must be absent from every telemetry surface
(stdout JSON, span attributes/events/status, metric labels), both before
and after collection. Safe context (action, request_id, error types,
counts, timings, finite labels) must remain. All assertions run through
the real SDK exporters and test readers, never around them.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from mainframe_rag import tracing as tracing_mod
from mainframe_rag.agent import answer_core as answer_core_mod
from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent import metrics as metrics_mod
from mainframe_rag.logs import JsonFormatter, error_type
from mainframe_rag.ports import ChatResult, TokenUsage
from tests.helpers_agent_local import agent_client, agent_hit
from tests.test_metrics import _points

CANARY_QUERY = "canary-query-ZK7xq Kostenstelle"
CANARY_HEADER = "canary-header-QQ9vv"
CANARY_EXC = "canary-exc-XJ4zz"
CANARY_DOC = "CANARY-DOC-11"
CANARIES = (CANARY_QUERY, CANARY_HEADER, CANARY_EXC, CANARY_DOC)

FINITE_METRIC_LABELS = {
    "endpoint",
    "query_class",
    "outcome",
    "verification_state",
    "model",
}


def _provider():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _hit(doc_id="SA22-0000-00"):
    return agent_hit(doc_id=doc_id, cite=f"{doc_id} Synthetic Reference, Chapter 2 > IEA500I, p. 1-6")


class MagicSearch:
    def __init__(self, exc=None, hits=None):
        self.exc = exc
        self.hits = hits if hits is not None else [_hit()]

    def __call__(self, *args, **kwargs):
        if self.exc:
            raise self.exc
        return list(self.hits), "identifier", {"embed_ms": 1, "qdrant_ms": 2}


class FakeLLM:
    def chat(self, messages, reasoning_effort=None, temperature=None):
        return ChatResult(
            content=(
                "Answer text.\n\n"
                "Citations:\n"
                "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            ),
            finish_reason="stop",
            usage=TokenUsage(),
        )


@pytest.fixture(autouse=True)
def _reset_provider(monkeypatch):
    monkeypatch.setattr(tracing_mod, "_provider", None)
    monkeypatch.setattr(trace, "set_tracer_provider", lambda _p: None)


@pytest.fixture
def client(monkeypatch, servable_representation_gate):
    provider, exporter = _provider()
    with agent_client(
        monkeypatch, llm=FakeLLM(), search=MagicSearch(),
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": ""},
    ) as c:
        monkeypatch.setattr(app_mod, "tracer", provider.get_tracer("test"))
        monkeypatch.setattr(answer_core_mod, "tracer", provider.get_tracer("test"))
        yield c, exporter


@pytest.fixture
def metric_reader(monkeypatch):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        metrics_mod,
        "_instruments",
        metrics_mod.create_instruments(provider.get_meter("test")),
    )
    return reader


def _span_text(spans):
    """Complete SDK records, including attributes, events and status text."""
    return "\n".join(span.to_json() for span in spans)


def _metric_points(reader):
    return [
        point
        for rm in reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        for point in metric.data.data_points
    ]


def _metric_text(reader):
    return "\n".join(
        item
        for point in _metric_points(reader)
        for pair in dict(point.attributes or {}).items()
        for item in (pair[0], str(pair[1]))
    )


def _metric_label_keys(reader):
    return {key for point in _metric_points(reader) for key in dict(point.attributes or {})}


def _formatted_logs(caplog):
    formatter = JsonFormatter()
    return [formatter.format(record) for record in caplog.records]


def test_error_type_never_exports_bodies():
    assert error_type(ValueError(CANARY_EXC)) == "ValueError"
    assert error_type(None) == "Error"
    assert CANARY_EXC not in error_type(ValueError(CANARY_EXC))


def test_success_hides_canaries_from_logs_and_spans(client, caplog, monkeypatch):
    c, exporter = client
    # The hit itself carries the identifier canary: proves doc ids stay
    # out of spans and logs while counts remain.
    monkeypatch.setattr(
        app_mod, "retrieve_search", MagicSearch(hits=[_hit(doc_id=CANARY_DOC)])
    )
    with caplog.at_level(logging.INFO):
        # Repeated requests must stay canary-free too (no per-request cache).
        for _ in range(2):
            resp = c.post(
                "/v1/search",
                json={"query": CANARY_QUERY},
                headers={"X-Canary-Probe": CANARY_HEADER},
            )
    assert resp.status_code == 200
    logs_blob = "\n".join(_formatted_logs(caplog))
    assert CANARY_QUERY not in logs_blob
    assert CANARY_HEADER not in logs_blob
    spans_blob = _span_text(exporter.get_finished_spans())
    assert exporter.get_finished_spans(), "success must render spans"
    assert CANARY_QUERY not in spans_blob
    assert CANARY_HEADER not in spans_blob
    assert CANARY_DOC not in spans_blob
    # Safe context remains: request identity, kind, counts.
    assert resp.json()["request_id"] in logs_blob
    root = next(s for s in exporter.get_finished_spans() if s.name == "v1.search")
    assert root.attributes["rag.query_kind"] == "identifier"
    assert root.attributes["rag.hits"] == 1


@pytest.mark.parametrize("anchor,condense,active,mode", [
    (False, False, CANARY_QUERY, "literal"),
    (True, False, CANARY_QUERY, "anchored"),
    (True, False, f"{CANARY_QUERY} IEA500I", "literal"),
    (False, True, CANARY_QUERY, "condensed"),
])
def test_follow_up_mode_is_a_text_free_root_attribute(
    client, caplog, monkeypatch, anchor, condense, active, mode
):
    """Issue #1268: the chat root span names which follow-up mode ran, from a
    fixed vocabulary, never the follow-up or antecedent text."""
    c, exporter = client
    monkeypatch.setattr(app_mod.settings, "chat_followup_anchor_enabled", anchor)
    monkeypatch.setattr(app_mod.settings, "chat_condense_enabled", condense)
    messages = [
        {"role": "user", "content": f"{CANARY_QUERY} opener"},
        {"role": "assistant", "content": "Original synthetic answer"},
        {"role": "user", "content": active},
    ]
    with caplog.at_level(logging.INFO):
        resp = c.post("/v1/chat", json={"messages": messages})
    assert resp.status_code == 200
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "v1.chat")
    assert root.attributes["rag.followup_mode"] == mode
    # A first turn is no follow-up: its root span keeps its earlier shape.
    exporter.clear()
    assert c.post("/v1/chat", json={"messages": messages[-1:]}).status_code == 200
    first = next(s for s in exporter.get_finished_spans() if s.name == "v1.chat")
    assert "rag.followup_mode" not in first.attributes
    assert CANARY_QUERY not in _span_text(spans)
    assert CANARY_QUERY not in "\n".join(_formatted_logs(caplog))


class _SelectingLLM(FakeLLM):
    """FakeLLM whose candidate-selection call (told apart by its candidate
    list) returns `reply` or raises it."""

    def __init__(self, reply):
        self.reply = reply
        self.select_calls = 0

    def chat(self, messages, reasoning_effort=None, temperature=None):
        if "Candidate excerpts:" not in messages[-1].content:
            return super().chat(messages, reasoning_effort, temperature)
        self.select_calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return ChatResult(content=self.reply, finish_reason="stop", usage=TokenUsage())


@pytest.mark.parametrize("path,stream,root_name", [
    ("/v1/answer", False, "v1.answer"),
    ("/v1/answer", True, "v1.answer"),
    ("/v1/chat", False, "v1.chat"),
    ("/v1/chat", True, "v1.chat"),
    ("/ui/chat", False, "ui.chat"),
])
@pytest.mark.parametrize("reply,attributes,kept", [
    ("[2]", {"rag.select_mode": "selected", "rag.select_selected": 1}, 1),
    (f"[2] {CANARY_EXC}",
     {"rag.select_mode": "fallback", "rag.select_reason": "invalid_output", "rag.select_selected": 0}, 0),
    (RuntimeError(" / ".join(CANARIES)),
     {"rag.select_mode": "fallback", "rag.select_reason": "model_error", "rag.select_selected": 0}, 0),
])
def test_candidate_selection_records_only_labels_and_counts(
    client, caplog, monkeypatch, path, stream, root_name, reply, attributes, kept
):
    """Issue #1309: on every answer route the stage's outcome is a fixed
    label and counts on the root span, with one `answer.select` child for the
    model call; no span or log carries the question, a candidate, the model's
    reply or its error text. The hit the client receives is the selection."""
    c, exporter = client
    hits = [_hit(doc_id=f"{CANARY_DOC}-{n}") for n in range(3)]
    llm = _SelectingLLM(reply)
    monkeypatch.setattr(app_mod, "retrieve_search", MagicSearch(hits=hits))
    monkeypatch.setattr(app_mod, "llm", llm)
    monkeypatch.setattr(app_mod.settings, "answer_select_enabled", True)
    monkeypatch.setattr(app_mod.settings, "answer_hit_limit", 1)
    monkeypatch.setattr(app_mod.settings, "ui_enabled", True)
    with caplog.at_level(logging.INFO):
        if path == "/ui/chat":
            resp = c.post(path, data={"message": CANARY_QUERY, "messages": ""})
        elif path == "/v1/answer":
            resp = c.post(path, json={"query": CANARY_QUERY, "stream": stream})
        else:
            resp = c.post(path, json={"messages": [{"role": "user", "content": CANARY_QUERY}],
                                      "stream": stream})
    assert resp.status_code == 200
    assert llm.select_calls == 1
    assert f"{CANARY_DOC}-{kept}" in resp.text
    assert [f"{CANARY_DOC}-{n}" in resp.text for n in range(3) if n != kept] == [False, False]
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == root_name)
    assert {k: v for k, v in root.attributes.items() if k.startswith("rag.select")} == {
        "rag.select_candidates": 3, "rag.select_pinned": 0, **attributes,
    }
    children = [s for s in spans if s.name == "answer.select"]
    assert len(children) == 1 and children[0].parent.span_id == root.context.span_id
    assert dict(children[0].attributes) == {"rag.select_candidates": 3}
    spans_blob, logs_blob = _span_text(spans), "\n".join(_formatted_logs(caplog))
    for canary in CANARIES:
        assert canary not in spans_blob, f"{canary} leaked into spans"
        assert canary not in logs_blob, f"{canary} leaked into stdout JSON"


class _FailingStore:
    async def retrieve(self, *args, **kwargs):
        raise RuntimeError(" / ".join(CANARIES))

    scroll = retrieve


@pytest.mark.parametrize("path,stream,root_name", [
    ("/v1/answer", False, "v1.answer"),
    ("/v1/answer", True, "v1.answer"),
    ("/v1/chat", False, "v1.chat"),
    ("/v1/chat", True, "v1.chat"),
    ("/ui/chat", False, "ui.chat"),
])
@pytest.mark.parametrize("fails,attributes,hit_count", [
    (False, {"rag.expand_mode": "expanded", "rag.expand_added": 2, "rag.expand_hits": 1,
             "rag.expand_stopped": 0, "rag.expand_budget_cut": 0}, 3),
    (True, {"rag.expand_mode": "fallback", "rag.expand_reason": "lookup_error",
            "rag.expand_added": 0, "rag.expand_hits": 0, "rag.expand_stopped": 0,
            "rag.expand_budget_cut": 0}, 1),
])
def test_neighbour_expansion_records_only_labels_and_counts(
    client, caplog, monkeypatch, path, stream, root_name, fails, attributes, hit_count
):
    """Issue #1314: on every answer route the stage's outcome is a fixed
    label and counts on the root span, with one `answer.expand` child for
    the stored-point reads; `rag.hits` stays the number of hits offered to
    packing (neighbours included) and `rag.expand_added` says how many of
    them are neighbours. No span or log carries the question, a stored
    field, or the store's error text."""
    from tests.fakes import StoredChunksFake
    from tests.test_answer_expand import Corpus

    c, exporter = client
    labels = ("A#0", "A#1", "A#2")
    corpus = Corpus().add([("A", [(3, 3)] * 3)],
                          scope={label: {"doc_id": CANARY_DOC, "title": CANARY_HEADER} for label in labels})
    monkeypatch.setattr(app_mod, "retrieve_search", MagicSearch(hits=[corpus.hit("A#1")]))
    monkeypatch.setattr(app_mod, "qdrant", _FailingStore() if fails else StoredChunksFake(corpus.payloads))
    monkeypatch.setattr(app_mod.settings, "answer_expand_enabled", True)
    monkeypatch.setattr(app_mod.settings, "ui_enabled", True)
    with caplog.at_level(logging.INFO):
        if path == "/ui/chat":
            resp = c.post(path, data={"message": CANARY_QUERY, "messages": ""})
        elif path == "/v1/answer":
            resp = c.post(path, json={"query": CANARY_QUERY, "stream": stream})
        else:
            resp = c.post(path, json={"messages": [{"role": "user", "content": CANARY_QUERY}],
                                      "stream": stream})
    assert resp.status_code == 200
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == root_name)
    assert {k: v for k, v in root.attributes.items() if k.startswith("rag.expand")} == attributes
    # Only /v1/answer puts rag.hits on its root span.
    assert root.attributes.get("rag.hits") == (hit_count if path == "/v1/answer" else None)
    children = [s for s in spans if s.name == "answer.expand"]
    assert len(children) == 1 and children[0].parent.span_id == root.context.span_id
    assert dict(children[0].attributes) == {"rag.expand_parents": 1}
    spans_blob, logs_blob = _span_text(spans), "\n".join(_formatted_logs(caplog))
    for canary in CANARIES:
        assert canary not in spans_blob, f"{canary} leaked into spans"
        assert canary not in logs_blob, f"{canary} leaked into stdout JSON"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("depth,effort", [("quick", "low"), ("thorough", "high"), ("standard", "medium")])
def test_answer_depth_records_only_the_resolved_effort(client, caplog, monkeypatch, stream, depth, effort):
    """Issue #1289: a symbolic depth is recorded as the effort it resolved to,
    in the existing prompt.build and llm.chat effort attributes; no span or
    log carries the question text."""
    c, exporter = client
    monkeypatch.setattr(app_mod.settings, "llm_reasoning_efforts", "low,medium,high")
    monkeypatch.setattr(app_mod.settings, "llm_reasoning_effort_simple", "medium")
    monkeypatch.setattr(app_mod.settings, "llm_reasoning_effort_complex", "medium")
    with caplog.at_level(logging.INFO):
        resp = c.post("/v1/chat", json={"messages": [{"role": "user", "content": CANARY_QUERY}],
                                        "stream": stream, "reasoning_depth": depth})
    assert resp.status_code == 200
    spans = exporter.get_finished_spans()
    assert [s.attributes["rag.reasoning_effort"] for s in spans if s.name == "prompt.build"] == [effort]
    assert [s.attributes["llm.reasoning_effort"] for s in spans if s.name == "llm.chat"] == [effort]
    assert CANARY_QUERY not in _span_text(spans)
    assert CANARY_QUERY not in "\n".join(_formatted_logs(caplog))


def test_failure_hides_canaries_but_keeps_error_type(client, caplog, monkeypatch):
    c, exporter = client
    monkeypatch.setattr(
        app_mod, "retrieve_search", MagicSearch(exc=RuntimeError(CANARY_EXC))
    )
    with caplog.at_level(logging.INFO):
        resp = c.post(
            "/v1/search",
            json={"query": CANARY_QUERY},
            headers={"X-Canary-Probe": CANARY_HEADER},
        )
    assert resp.status_code == 502
    logs_blob = "\n".join(_formatted_logs(caplog))
    for canary in CANARIES:
        assert canary not in logs_blob, f"{canary} leaked into stdout JSON"
    assert "RuntimeError" in logs_blob
    spans = exporter.get_finished_spans()
    assert spans, "failure must still render spans"
    spans_blob = _span_text(spans)
    for canary in CANARIES:
        assert canary not in spans_blob, f"{canary} leaked into spans"
    root = next(s for s in spans if s.name == "v1.search")
    assert root.status.status_code == trace.StatusCode.ERROR
    assert root.status.description == "RuntimeError"
    assert any(
        e.name == "exception"
        and e.attributes.get("exception.type") == "RuntimeError"
        for e in root.events
    )


@pytest.mark.parametrize(
    ("path", "stream", "endpoint", "failure"),
    [
        ("/v1/answer", False, "answer", "sync_retrieval"),
        ("/v1/answer", True, "answer", "awaited_retrieval"),
        ("/v1/chat", False, "chat", "sync_condensation"),
        ("/v1/chat", True, "chat", "awaited_condensation"),
        ("/v1/chat/completions", True, "chat", "awaited_retrieval"),
        ("/ui/chat", False, "console", "sync_retrieval"),
        ("/ui/chat", False, "console", "awaited_condensation"),
        ("/ui/chat/stream", True, "console", "sync_retrieval"),
        ("/ui/chat/stream", True, "console", "awaited_condensation"),
    ],
)
def test_attachment_failures_hide_canaries(
    client, metric_reader, caplog, monkeypatch, path, stream, endpoint, failure,
):
    """Issue #563: escaping failures must be private before SDK export."""
    connection, exporter = client
    calls = []

    def fail_sync(*args, **kwargs):
        calls.append(failure)
        raise RuntimeError(" / ".join(CANARIES))

    async def fail_awaited(*args, **kwargs):
        await asyncio.sleep(0)
        fail_sync(*args, **kwargs)

    fail = fail_awaited if failure.startswith("awaited") else fail_sync
    condensation = failure.endswith("condensation")
    if condensation:
        monkeypatch.setattr(answer_core_mod, "condense_query", fail)
        monkeypatch.setattr(app_mod.settings, "chat_condense_enabled", True)
    else:
        monkeypatch.setattr(app_mod, "retrieve_search", fail)
    monkeypatch.setattr(app_mod.settings, "ui_enabled", True)
    messages = [
        {"role": "user", "content": "Original synthetic question"},
        {"role": "assistant", "content": "Original synthetic answer"},
        {"role": "user", "content": CANARY_QUERY},
    ]
    headers = {
        "X-Canary-Probe": CANARY_HEADER,
        "traceparent": "00-1234567890abcdef1234567890abcdef-abcdef1234567890-01",
    }
    with caplog.at_level(logging.INFO):
        if path == "/ui/chat":
            response = connection.post(path, headers=headers, data={
                "message": CANARY_QUERY, "messages": json.dumps(messages[:-1]),
            })
        else:
            payload = {} if endpoint == "console" else {"stream": stream}
            payload.update({"query": CANARY_QUERY} if endpoint == "answer" else {"messages": messages})
            response = connection.post(path, headers=headers, json=payload)
    # Anchored follow-ups (#1268/#1274) launch literal and anchor retrieval
    # legs concurrently (#1297), so both legs invoke retrieve_search before
    # fail-fast surfaces the single failure.
    expected_calls = (
        [failure, failure]
        if endpoint in {"chat", "console"} and not condensation
        else [failure]
    )
    assert calls == expected_calls
    # The console retrieves before its stream opens, as /v1/chat does (#985):
    # the failure is the same JSON envelope, never a post-header error frame.
    if path == "/ui/chat":
        assert response.status_code == 502
        assert "The reasoning agent could not complete this request." in response.text
    else:
        assert response.status_code == 502
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"code": "upstream_error", "message": "retrieval failed"}

    spans = exporter.get_finished_spans()
    assert spans, "failure must still render spans"
    logs_blob = "\n".join(_formatted_logs(caplog))
    spans_blob = _span_text(spans)
    metrics_blob = _metric_text(metric_reader)
    for canary in CANARIES:
        assert canary not in spans_blob, f"{canary} leaked into spans"
        assert canary not in logs_blob, f"{canary} leaked into stdout JSON"
        assert canary not in metrics_blob, f"{canary} leaked into metric labels"
    assert _metric_label_keys(metric_reader) <= FINITE_METRIC_LABELS
    root_name = "ui.chat" if endpoint == "console" else f"v1.{endpoint}"
    roots = [span for span in spans if span.name == root_name]
    assert len(roots) == 1
    root = roots[0]
    expected_type = "RuntimeError"
    assert root.status.status_code == trace.StatusCode.ERROR
    assert root.status.description == expected_type
    assert [dict(event.attributes) for event in root.events if event.name == "exception"] == [
        {"exception.type": expected_type},
    ]
    assert root.kind == trace.SpanKind.SERVER
    assert root.context.trace_id == 0x1234567890ABCDEF1234567890ABCDEF
    assert root.parent.span_id == 0xABCDEF1234567890
    assert len(root.attributes["http.request_id"]) == 12
    assert root.attributes["http.request_id"] in logs_blob
    if condensation:
        children = [span for span in spans if span.name == "chat.condense"]
        assert len(children) == 1
        assert children[0].parent.span_id == root.context.span_id
    counts = _points(metric_reader, "rag.requests.total")
    assert len(counts) == 1 and counts[0].value == 1
    assert dict(counts[0].attributes) == {
        "endpoint": endpoint, "outcome": "upstream_error", "query_class": "unknown",
    }
    assert sum(point.count for point in _points(metric_reader, "rag.request.duration")) == 1


@pytest.mark.parametrize(
    "enabled,end_on_exit,awaited",
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_safe_attachment_preserves_parent_and_lifetime(enabled, end_on_exit, awaited):
    provider, exporter = _provider()
    span_tracer = (
        provider.get_tracer("attachment") if enabled else trace.NoOpTracerProvider().get_tracer("off")
    )
    root = tracing_mod.start_span(span_tracer, "root")
    ambient = trace.get_current_span()

    async def escape():
        with tracing_mod.use_span(root, end_on_exit=end_on_exit):
            assert trace.get_current_span() is root
            with tracing_mod.start_as_current_span(span_tracer, "child"):
                pass
            if awaited:
                await asyncio.sleep(0)
            raise RuntimeError(CANARY_EXC)

    try:
        with pytest.raises(RuntimeError, match=CANARY_EXC):
            asyncio.run(escape())
        assert trace.get_current_span() is ambient
        assert root.is_recording() == (enabled and not end_on_exit)
        if not end_on_exit:
            app_mod._span_error(root, RuntimeError(CANARY_EXC))
            root.end()
        spans = exporter.get_finished_spans()
        assert CANARY_EXC not in _span_text(spans)
        if enabled:
            assert {span.name for span in spans} == {"root", "child"}
            child = next(span for span in spans if span.name == "child")
            assert child.parent.span_id == root.get_span_context().span_id
            finished_root = next(span for span in spans if span.name == "root")
            assert finished_root.status.status_code == (
                trace.StatusCode.UNSET if end_on_exit else trace.StatusCode.ERROR
            )
            assert [dict(event.attributes) for event in finished_root.events] == (
                [] if end_on_exit else [{"exception.type": "RuntimeError"}]
            )
        else:
            assert not spans
    finally:
        provider.shutdown()


def test_metric_labels_hide_canaries_and_stay_finite(
    client, metric_reader, monkeypatch
):
    c, _exporter = client
    monkeypatch.setattr(
        app_mod, "retrieve_search", MagicSearch(exc=RuntimeError(CANARY_EXC))
    )
    c.post("/v1/search", json={"query": CANARY_QUERY})
    monkeypatch.setattr(app_mod, "retrieve_search", MagicSearch())
    c.post("/v1/search", json={"query": CANARY_QUERY})
    blob = _metric_text(metric_reader)
    for canary in CANARIES:
        assert canary not in blob, f"{canary} leaked into metric labels"
    assert _metric_label_keys(metric_reader) <= FINITE_METRIC_LABELS


def test_exception_frames_carry_no_message_or_source():
    try:
        raise ValueError(CANARY_EXC)
    except ValueError:
        import sys

        line = JsonFormatter().format(
            logging.makeLogRecord(
                {"msg": "failed", "levelname": "ERROR", "name": "t", "exc_info": sys.exc_info()}
            )
        )
    payload = json.loads(line)
    assert payload["error_type"] == "ValueError"
    assert CANARY_EXC not in line
    assert "test_telemetry_privacy.py" in payload["trace"]


def test_attachment_privacy_negative_control(client, metric_reader, caplog, monkeypatch):
    """Restoring SDK attachment defaults must trip the endpoint canary."""
    monkeypatch.setattr(app_mod, "use_span", trace.use_span)
    with pytest.raises(AssertionError, match="leaked into spans"):
        test_attachment_failures_hide_canaries(
            client, metric_reader, caplog, monkeypatch,
            "/v1/answer", False, "answer", "sync_retrieval",
        )
