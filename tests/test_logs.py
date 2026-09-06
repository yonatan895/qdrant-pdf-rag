"""One logging config: single-line JSON objects (issue #20 PR D)."""

import json
import logging

from mainframe_rag.logs import JsonFormatter, configure_logging


def _record(msg: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="ingest", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )


def test_json_message_merges_into_payload():
    line = JsonFormatter().format(_record('{"action": "upsert", "doc_id": "SA22-0000-00"}'))
    payload = json.loads(line)
    assert payload["action"] == "upsert"
    assert payload["doc_id"] == "SA22-0000-00"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "ingest"
    assert "ts" in payload


def test_plain_message_is_wrapped():
    payload = json.loads(JsonFormatter().format(_record("plain text")))
    assert payload["message"] == "plain text"


def test_envelope_wins_over_event_fields():
    payload = json.loads(
        JsonFormatter().format(_record('{"ts": "fake", "level": "BOGUS", "doc_id": "d"}'))
    )
    assert payload["ts"] != "fake"
    assert payload["level"] == "INFO"
    assert payload["doc_id"] == "d"


def test_invalid_level_fails_with_clear_message():
    import pytest

    with pytest.raises(ValueError, match="LOG_LEVEL"):
        configure_logging("bogus")


def test_exception_goes_into_payload_one_line():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        line = JsonFormatter().format(_record("failed", exc_info=sys.exc_info()))
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["error_type"] == "ValueError"
    assert "boom" in payload["trace"]


def test_configure_logging_is_idempotent(monkeypatch):
    root = logging.getLogger()
    # Swap the handler list so the original (pytest's capture) is restored
    # after the test.
    monkeypatch.setattr(root, "handlers", [])
    configure_logging("INFO")
    configure_logging("INFO")
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_trace_ids_stamped_under_active_span():
    """Issue #185: a log line emitted inside a span carries that span's ids,
    joining JSON logs to Jaeger traces."""
    import json as _json

    from opentelemetry.sdk.trace import TracerProvider

    from mainframe_rag.agent import tracing as tracing_mod

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("op") as span:
        line = JsonFormatter().format(_record('{"request_id": "abc"}'))
    payload = _json.loads(line)
    ctx = span.get_span_context()
    from opentelemetry import trace as trace_api

    assert payload["trace_id"] == trace_api.format_trace_id(ctx.trace_id)
    assert payload["span_id"] == trace_api.format_span_id(ctx.span_id)
    assert tracing_mod.current_trace_ids() == {}


def test_trace_ids_omitted_without_span():
    """Issue #185: tracing off -> log shape unchanged (no new keys)."""
    import json as _json

    payload = _json.loads(JsonFormatter().format(_record('{"request_id": "abc"}')))
    assert "trace_id" not in payload
    assert "span_id" not in payload


def test_caller_trace_id_wins_over_ambient_span():
    """Issue #185: an explicit caller value (e.g. propagated across a
    process boundary) is never overwritten by the ambient span."""
    import json as _json

    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("op"):
        line = JsonFormatter().format(_record('{"trace_id": "caller", "span_id": "s"}'))
    payload = _json.loads(line)
    assert payload["trace_id"] == "caller"
    assert payload["span_id"] == "s"
