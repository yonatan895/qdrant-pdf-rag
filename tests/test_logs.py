"""One logging config: single-line JSON objects (issue #20 PR D)."""

import json
import logging

from mainframe_rag.logs import JsonFormatter, configure_logging


def _record(msg: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="ingest", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )


def test_json_and_plain_messages_become_one_payload_and_envelope_wins():
    payload = json.loads(
        JsonFormatter().format(_record('{"action": "upsert", "doc_id": "SA22-0000-00"}'))
    )
    assert payload["action"] == "upsert" and payload["doc_id"] == "SA22-0000-00"
    assert payload["level"] == "INFO" and payload["logger"] == "ingest" and "ts" in payload

    assert json.loads(JsonFormatter().format(_record("plain text")))["message"] == "plain text"

    clash = json.loads(
        JsonFormatter().format(_record('{"ts": "fake", "level": "BOGUS", "doc_id": "d"}'))
    )
    assert clash["ts"] != "fake" and clash["level"] == "INFO" and clash["doc_id"] == "d"


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
    # Issue #529 OBS-1A: the exception body never enters telemetry — only
    # the type and frame locations (no message, no source text).
    assert "boom" not in line
    assert "trace" in payload and "test_logs.py" in payload["trace"]


def test_configure_logging_is_idempotent(monkeypatch):
    root = logging.getLogger()
    # Swap the handler list so the original (pytest's capture) is restored
    # after the test.
    monkeypatch.setattr(root, "handlers", [])
    configure_logging("INFO")
    configure_logging("INFO")
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_trace_ids_follow_the_active_span_and_caller_values_win():
    """Issue #185: a line inside a span carries its ids (joining logs to
    traces); no span means no new keys; an explicit caller value is never
    overwritten by the ambient span."""
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import TracerProvider

    from mainframe_rag import tracing as tracing_mod

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("op") as span:
        stamped = json.loads(JsonFormatter().format(_record('{"request_id": "abc"}')))
        caller = json.loads(
            JsonFormatter().format(_record('{"trace_id": "caller", "span_id": "s"}'))
        )
    ctx = span.get_span_context()
    assert stamped["trace_id"] == trace_api.format_trace_id(ctx.trace_id)
    assert stamped["span_id"] == trace_api.format_span_id(ctx.span_id)
    assert caller["trace_id"] == "caller" and caller["span_id"] == "s"
    assert tracing_mod.current_trace_ids() == {}

    bare = json.loads(JsonFormatter().format(_record('{"request_id": "abc"}')))
    assert "trace_id" not in bare and "span_id" not in bare
