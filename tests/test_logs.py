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
