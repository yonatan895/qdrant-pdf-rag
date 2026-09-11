"""One logging config for ingest + agent (issue #20 PR D).

One JSON object per line: {"ts", "level", "logger", ...event fields}. Call
sites pass either a pre-serialized JSON object string (merged when it parses
— the agent's json_log and the ingest counters already do this) or plain
text (wrapped as {"message": ...}). No secrets, no PDF text: callers log ids
(doc_id, chunk_id, request_id), counts, and elapsed_ms only. When a traced
request is active, the formatter also stamps the OTel trace_id/span_id
(issue #185) so log lines join to Jaeger traces; with tracing off the
fields are omitted and the log shape is unchanged.
"""

from __future__ import annotations

import json
import logging
import time

from mainframe_rag.tracing import current_trace_ids


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
        }
        message = record.getMessage()
        try:
            parsed = json.loads(message)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            # Event fields first, envelope on top: ts/level/logger can never
            # be shadowed by an event dict.
            payload = {**parsed, **payload}
        else:
            payload["message"] = message
        # Trace correlation last, never overwriting: an explicit caller
        # value (e.g. propagated across a process boundary) wins over the
        # ambient span context.
        for key, value in current_trace_ids().items():
            payload.setdefault(key, value)
        if record.exc_info:
            payload["error_type"] = (
                record.exc_info[0].__name__ if record.exc_info[0] else "Error"
            )
            # formatException is multi-line; json.dumps escapes it, so the
            # log line stays one physical line.
            payload["trace"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger (idempotent)."""
    try:
        resolved = logging.getLevelNamesMapping()[level.upper()]
    except KeyError:
        raise ValueError(
            f"LOG_LEVEL must be a standard level name, got {level!r}"
        ) from None
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(resolved)
