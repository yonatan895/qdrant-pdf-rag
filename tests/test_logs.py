"""One logging config: single-line JSON objects (issue #20 PR D)."""

import json
import logging

import pytest

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


# Issue #989: uvicorn's default LOGGING_CONFIG gives its own loggers plain-text
# handlers, and Starlette re-raises an unhandled fault after the 500 so the
# server can log it. Run each service exactly as its image does
# (`python -m uvicorn ...`, default log config) with a fault carrying a canary.
_CANARY = "SECRET-CANARY-989 manual excerpt text"

_AGENT_PROBE = f"""
from mainframe_rag.agent import app as app_mod


def _fault(*_a, **_k):
    raise RuntimeError({_CANARY!r})


app_mod._require_query_length = _fault  # first call on /v1/answer
app = app_mod.app
"""

_FEEDBACK_PROBE = f"""
from mainframe_rag.feedback import service


def _fault(*_a, **_k):
    raise RuntimeError({_CANARY!r})


def app():
    service.generate_latest = _fault  # /metrics renders through it
    return service.app_from_env()
"""


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve_and_fault(tmp_path, probe: str, extra: list[str], env: dict[str, str], ready: str, fault):
    """Start the probe app as the image starts the service, wait until
    `ready` answers, run `fault(client)` and return (response, all output)."""
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    import httpx2

    import mainframe_rag

    (tmp_path / "probe_app.py").write_text(probe)
    port = _free_port()
    src = str(Path(mainframe_rag.__file__).resolve().parents[1])
    child_env = {**os.environ, **env, "PYTHONPATH": src}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "probe_app:app", *extra, "--app-dir", str(tmp_path),
         "--host", "127.0.0.1", "--port", str(port)],
        env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        with httpx2.Client(base_url=base, timeout=10) as client:
            while True:
                try:
                    if client.get(ready).status_code == 200:
                        break
                except httpx2.TransportError:
                    pass
                assert proc.poll() is None, "service exited before it was ready"
                assert time.monotonic() < deadline, "service never became ready"
                time.sleep(0.2)
            response = fault(client)
    finally:
        proc.terminate()
        output, _ = proc.communicate(timeout=20)
    return response, output


@pytest.mark.parametrize("service", ["agent", "feedback"])
def test_unhandled_fault_under_uvicorn_defaults_logs_json_without_exception_text(
    tmp_path, service
):
    """Every line after logging is configured is one JSON object; the crash
    uvicorn logs carries the type and frame locations, never the message."""
    if service == "agent":
        response, output = _serve_and_fault(
            tmp_path, _AGENT_PROBE, [],
            {
                "QDRANT_URL": "http://127.0.0.1:1", "EMBED_MODE": "hash", "ALLOW_HASH_MODE": "true",
                "LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_MODEL_REASONING": "probe-model",
            },
            "/livez",
            lambda c: c.post("/v1/answer", json={"query": "IEA500I"}),
        )
        assert response.json() == {"code": "internal", "message": "internal error"}
    else:
        token = "probe-token"
        response, output = _serve_and_fault(
            tmp_path, _FEEDBACK_PROBE, ["--factory"],
            {
                "FEEDBACK_STORE_PATH": str(tmp_path / "feedback.sqlite3"),
                "FEEDBACK_SERVICE_TOKEN": token, "METRICS_ENABLED": "true",
            },
            "/healthz",
            lambda c: c.get("/metrics", headers={"Authorization": f"Bearer {token}"}),
        )
        assert response.json()["code"] == "internal_error"
    assert response.status_code == 500
    assert _CANARY not in output, "exception text reached the service output"
    lines = [line for line in output.splitlines() if line.strip()]
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            parsed.append(None)
    # Only uvicorn's few pre-lifespan startup lines may precede the JSON handler.
    first = next(i for i, payload in enumerate(parsed) if payload is not None)
    plain_after = [lines[i] for i in range(first, len(lines)) if parsed[i] is None]
    assert plain_after == [], plain_after
    crashes = [p for p in parsed if p and p.get("logger") == "uvicorn.error" and "error_type" in p]
    assert [p["error_type"] for p in crashes] == ["RuntimeError"]
    assert crashes[0]["trace"], "frame locations stay for triage"
