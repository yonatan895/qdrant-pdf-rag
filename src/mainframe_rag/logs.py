"""One logging config for ingest + agent (issue #20 PR D).

One JSON object per line: {"ts", "level", "logger", ...event fields}. Call
sites pass either a pre-serialized JSON object string (merged when it parses
— the agent's json_log and the ingest counters already do this) or plain
text (wrapped as {"message": ...}). No secrets, no PDF text, no raw queries,
prompts, evidence, responses, headers, or source paths: callers log ids
(doc_id, chunk_id, request_id), counts, elapsed_ms, and error *types* only.
Exception bodies are never exported — use error_type() for the stable
class name; tracebacks render as frame locations (file:function:line)
without messages or source text (issue #529 OBS-1A). When a traced
request is active, the formatter also stamps the OTel trace_id/span_id
(issue #185) so log lines join to Jaeger traces; with tracing off the
fields are omitted and the log shape is unchanged.
"""

from __future__ import annotations

import json
import logging
import time
import traceback

from mainframe_rag.tracing import current_trace_ids


def error_type(exc: BaseException | None) -> str:
    """Stable telemetry label for a failure: the exception class name only.

    Exception messages are untrusted input for telemetry — they routinely
    carry query text, URLs, file paths, or upstream bodies — so no telemetry
    surface (stdout JSON, span attribute/event/status, metric label) may
    export str(exc). Callers needing the body for local debugging read it
    from the raised error itself, never from collected telemetry.
    """
    if exc is None:
        return "Error"
    return type(exc).__name__ or "Error"


def _frame_locations(exc_info) -> list[str]:
    """Frame metadata without messages, locals, or source text: one
    path:function:line entry per traceback frame, outermost first."""
    _, _, tb = exc_info
    locations = []
    for frame in traceback.extract_tb(tb):
        locations.append(f"{frame.filename}:{frame.name}:{frame.lineno}")
    return locations


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
            payload["error_type"] = error_type(record.exc_info[1])
            # Frame locations only (issue #529 OBS-1A): the formatted
            # traceback text would export the exception message and source
            # lines, so the log line keeps locations while the body stays
            # out of collected telemetry.
            payload["trace"] = "\n".join(_frame_locations(record.exc_info))
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
