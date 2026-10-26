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


def test_rendered_traceback_in_a_plain_message_keeps_type_and_frames_only():
    """Issue #989: uvicorn logs a failed lifespan as Starlette's
    `traceback.format_exc()` text with no exc_info; the line keeps the type
    and frame locations, never the message or source lines."""
    import traceback

    try:
        raise RuntimeError("SECRET-CANARY-989 rendered")
    except RuntimeError:
        rendered = traceback.format_exc()
    line = JsonFormatter().format(_record(rendered))
    payload = json.loads(line)
    assert "SECRET-CANARY-989" not in line and 'raise RuntimeError("' not in line
    assert payload["error_type"] == "RuntimeError"
    assert "test_logs.py:test_rendered_traceback" in payload["trace"]
    assert payload["message"] == "rendered traceback withheld"
    assert json.loads(JsonFormatter().format(_record("plain text")))["message"] == "plain text"


def test_server_logger_disabled_by_uvicorn_stays_disabled(monkeypatch):
    """`--no-access-log` leaves uvicorn.access with no handlers and no
    propagation; adopting the server loggers must not re-enable it."""
    access = logging.getLogger("uvicorn.access")
    error = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    monkeypatch.setattr(access, "handlers", [])
    monkeypatch.setattr(access, "propagate", False)
    monkeypatch.setattr(error, "handlers", [logging.StreamHandler()])
    monkeypatch.setattr(error, "propagate", False)
    configure_logging("INFO")
    assert (access.handlers, access.propagate) == ([], False)
    assert (error.handlers, error.propagate) == ([], True)


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


_LIFESPAN_PROBE = f"""
from mainframe_rag.agent import app as app_mod

_configure = app_mod.configure_logging


def _configure_then_fail(level):
    _configure(level)
    raise RuntimeError({_CANARY!r})  # first startup step after logging


app_mod.configure_logging = _configure_then_fail
app = app_mod.app
"""

_AGENT_ENV = {
    "QDRANT_URL": "http://127.0.0.1:1", "EMBED_MODE": "hash", "ALLOW_HASH_MODE": "true",
    "LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_MODEL_REASONING": "probe-model",
}


def _free_port(attempts: int = 20) -> int:
    """A loopback port for the child to bind.

    Only a transient bind error is retried here; the close->child-bind race
    itself is handled by `_launch_ready`, which restarts the launch on a fresh
    port when the child reports a bind failure (#991)."""
    import socket

    for _ in range(attempts):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", 0))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise AssertionError("could not reserve a loopback port")


def _launch(tmp_path, probe: str, extra: list[str], env: dict[str, str]):
    """`python -m uvicorn probe_app:app` with uvicorn's default log config, as
    the images launch the services. Python `warnings` output (e.g.
    qdrant-client's version-check UserWarning against the unreachable probe
    Qdrant) is not the logging path and stays out of scope (#989), so the
    child ignores warnings to keep the JSON-only assertion deterministic."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import mainframe_rag

    (tmp_path / "probe_app.py").write_text(probe)
    port = _free_port()
    src = str(Path(mainframe_rag.__file__).resolve().parents[1])
    child_env = {**os.environ, **env, "PYTHONPATH": src, "PYTHONWARNINGS": "ignore"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "probe_app:app", *extra, "--app-dir", str(tmp_path),
         "--host", "127.0.0.1", "--port", str(port)],
        env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return proc, port


def _assert_json_only_without_canary(output: str) -> list[dict]:
    """No canary anywhere; every line from the first JSON line on is JSON
    (only uvicorn lines printed before the app configures logging precede it)."""
    assert _CANARY not in output, "exception text reached the service output"
    lines = [line for line in output.splitlines() if line.strip()]
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            parsed.append(None)
    first = next(i for i, payload in enumerate(parsed) if payload is not None)
    plain_after = [lines[i] for i in range(first, len(lines)) if parsed[i] is None]
    assert plain_after == [], plain_after
    return [payload for payload in parsed if payload is not None]


def _stop_and_collect(proc, timeout: float = 20) -> str:
    """Terminate the child and reap it, escalating to kill on timeout so a
    TERM-ignoring server fails the logging assertion instead of raising
    TimeoutExpired from cleanup."""
    import subprocess

    proc.terminate()
    try:
        return proc.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()[0]


_BIND_FAILURES = ("address already in use", "error while attempting to bind")


def _is_bind_failure(output: str) -> bool:
    text = output.casefold()
    return any(marker in text for marker in _BIND_FAILURES)


def _wait_until_ready(proc, base: str, ready: str) -> None:
    import time

    import httpx2

    deadline = time.monotonic() + 60
    with httpx2.Client(base_url=base, timeout=10) as client:
        while True:
            try:
                if client.get(ready).status_code == 200:
                    return
            except httpx2.TransportError:
                pass
            assert proc.poll() is None, "service exited before it was ready"
            assert time.monotonic() < deadline, "service never became ready"
            time.sleep(0.2)


def _launch_ready(
    tmp_path, probe: str, extra: list[str], env: dict[str, str], ready: str, attempts: int = 3,
):
    """Start the probe app and wait for readiness, retrying only a child bind
    failure on a fresh port. `_free_port` releases the selected port before
    the child binds it, so another process can take it in that window."""
    for attempt in range(1, attempts + 1):
        proc, port = _launch(tmp_path, probe, extra, env)
        try:
            _wait_until_ready(proc, f"http://127.0.0.1:{port}", ready)
        except AssertionError:
            output = _stop_and_collect(proc)
            if attempt < attempts and _is_bind_failure(output):
                continue
            raise AssertionError(
                f"service not ready (attempt {attempt}/{attempts}); "
                f"child output:\n{output}"
            ) from None
        return proc, port
    raise AssertionError("unreachable")  # pragma: no cover


def _serve_and_fault(tmp_path, probe: str, extra: list[str], env: dict[str, str], ready: str, fault):
    """Start the probe app as the image starts the service, wait until
    `ready` answers, run `fault(client)` and return (response, all output)."""
    import httpx2

    proc, port = _launch_ready(tmp_path, probe, extra, env, ready)
    try:
        with httpx2.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            response = fault(client)
    finally:
        output = _stop_and_collect(proc)
    return response, output


@pytest.mark.parametrize("service", ["agent", "feedback"])
def test_unhandled_fault_under_uvicorn_defaults_logs_json_without_exception_text(
    tmp_path, service
):
    """Every line after logging is configured is one JSON object; the crash
    uvicorn logs carries the type and frame locations, never the message."""
    if service == "agent":
        response, output = _serve_and_fault(
            tmp_path, _AGENT_PROBE, [], _AGENT_ENV, "/livez",
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
    parsed = _assert_json_only_without_canary(output)
    crashes = [p for p in parsed if p.get("logger") == "uvicorn.error" and "error_type" in p]
    assert [p["error_type"] for p in crashes] == ["RuntimeError"]
    assert crashes[0]["trace"], "frame locations stay for triage"


def test_lifespan_failure_after_logging_is_configured_withholds_the_traceback(tmp_path):
    """A startup failure after `configure_logging` reaches uvicorn as
    Starlette's rendered traceback text (no exc_info): the process still
    exits non-zero and says so, with the type and frames only."""
    proc, _port = _launch(tmp_path, _LIFESPAN_PROBE, [], _AGENT_ENV)
    output, _ = proc.communicate(timeout=60)
    assert proc.returncode != 0, "a failed startup must not keep serving"
    parsed = _assert_json_only_without_canary(output)
    failed = [p for p in parsed if p.get("logger") == "uvicorn.error" and "error_type" in p]
    assert [p["error_type"] for p in failed] == ["RuntimeError"]
    assert "_configure_then_fail" in failed[0]["trace"]
    assert any(p.get("message") == "Application startup failed. Exiting." for p in parsed)


def test_probe_cleanup_kills_and_reaps_a_child_that_ignores_terminate():
    """#588: cleanup escalates to kill, so a wedged server surfaces as the
    failing logging assertion rather than a TimeoutExpired from the fixture."""
    import signal
    import subprocess
    import sys

    child = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        output = _stop_and_collect(proc, timeout=1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == -signal.SIGKILL
    assert output == ""


def test_bind_failure_retries_launch_with_a_fresh_port(tmp_path, monkeypatch):
    """#588/#991 F-minor: the port can be taken between `_free_port` returning
    it and the child binding it. That window is closed by retrying the whole
    launch on a fresh port, not by retrying `sock.bind(("127.0.0.1", 0))`."""
    import socket
    import sys

    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    held_port = held.getsockname()[1]
    ports: list[int] = []
    real_free_port = _free_port

    def first_port_held() -> int:
        port = held_port if not ports else real_free_port()
        ports.append(port)
        return port

    monkeypatch.setattr(sys.modules[__name__], "_free_port", first_port_held)
    try:
        response, _output = _serve_and_fault(
            tmp_path, _AGENT_PROBE, [], _AGENT_ENV, "/livez", lambda client: client.get("/livez"),
        )
    finally:
        held.close()
    assert response.status_code == 200
    assert len(ports) == 2
    assert ports[0] == held_port and ports[1] != held_port


def test_non_bind_startup_failure_fails_once_with_child_output(tmp_path, monkeypatch):
    """#588: only a child bind failure is retried. Any other early exit keeps
    today's immediate failure and surfaces the child output."""
    import sys

    calls = []
    real_launch = _launch

    def counted_launch(*args, **kwargs):
        calls.append(1)
        return real_launch(*args, **kwargs)

    monkeypatch.setattr(sys.modules[__name__], "_launch", counted_launch)
    with pytest.raises(AssertionError) as excinfo:
        _serve_and_fault(
            tmp_path, _LIFESPAN_PROBE, [], _AGENT_ENV, "/livez", lambda client: client.get("/livez"),
        )
    assert len(calls) == 1
    assert "service not ready (attempt 1/3)" in str(excinfo.value)
    assert "Application startup failed" in str(excinfo.value)
