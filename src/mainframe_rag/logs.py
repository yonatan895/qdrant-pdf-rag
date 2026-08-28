"""One logging config for ingest + agent (issue #20 PR D).

One JSON object per line: {"ts", "level", "logger", ...event fields}. Call
sites pass either a pre-serialized JSON object string (merged when it parses
— the agent's json_log and the ingest counters already do this) or plain
text (wrapped as {"message": ...}). No secrets, no PDF text: callers log ids
(doc_id, chunk_id, request_id), counts, and elapsed_ms only.
"""

from __future__ import annotations

import json
import logging
import time


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
            payload.update(parsed)
        else:
            payload["message"] = message
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
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
