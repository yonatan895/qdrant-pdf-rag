"""FTP MCP bridge tests (ADR-0003 phase 1). Hermetic: FakeFTP replaces the
wire — no network, no credentials, no mainframe. No binary fixtures."""

from __future__ import annotations

import ftplib
import io
import json

from mainframe_rag.mcp import bridge, server
from mainframe_rag.mcp.bridge import FTPConfig


class FakeFTP:
    """Duck-typed ftplib.FTP: scripted replies, recorded commands."""

    files: dict[str, bytes] | None = None
    listings: dict[str, list[str]] | None = None
    jes_lines: list[str] | None = None
    site_fails: bool = False

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.logged_in: tuple | None = None
        self.aborted = False
        self.files = dict(type(self).files or {})
        self.listings = dict(type(self).listings or {})
        self.jes_lines = list(type(self).jes_lines or [])

    def connect(self, host: str, port: int = 21, timeout: float = 15.0) -> str:
        self.commands.append(f"CONNECT {host}:{port}")
        return "220 welcome"

    def login(self, user: str, passwd: str) -> str:
        self.logged_in = (user, passwd)
        return "230 logged in"

    def sendcmd(self, cmd: str) -> str:
        self.commands.append(cmd)
        if cmd.startswith("SITE") and "JES" in cmd and type(self).site_fails:
            raise ftplib.error_perm("550 JES interface not available")
        return "200 ok"

    def retrbinary(self, cmd: str, callback) -> str:
        self.commands.append(cmd)
        arg = cmd.split(" ", 1)[1]
        if arg not in self.files:
            raise ftplib.error_perm("550 File not found")
        data = self.files[arg]
        for i in range(0, len(data), 8192):
            callback(data[i : i + 8192])
        return "226 done"

    def retrlines(self, cmd: str, callback) -> str:
        self.commands.append(cmd)
        for line in self.jes_lines:
            callback(line)
        return "226 done"

    def nlst(self, arg: str = "") -> list[str]:
        self.commands.append(f"NLST {arg}")
        if arg in self.listings:
            return self.listings[arg]
        raise ftplib.error_perm("550 Not a PDS")

    def quit(self) -> str:
        self.commands.append("QUIT")
        return "221 bye"

    def abort(self) -> str:
        self.aborted = True
        return "226 aborted"


def _config(**overrides) -> FTPConfig:
    base = {"host": "mf.example.com", "user": "READER", "password": "s3cret"}
    base.update(overrides)
    return FTPConfig(**base)


def _session(fake: FakeFTP, **overrides) -> bridge.FTPSession:
    return bridge.FTPSession(fake, _config(**overrides))


JES_FIXTURE = [
    "PAYROLL  JOB00123 READER   OUTPUT A        RC=0000",
    "BACKUP   JOB00124 READER   ACTIVE  B",
    "GARBAGE-LINE-WITHOUT-FIELDS",
]


def test_dataset_read_sequential() -> None:
    fake = FakeFTP()
    fake.files["'SYS1.PARMLIB'"] = b"LINE1\nLINE2\n"
    out = bridge.dataset_read(_session(fake), "sys1.parmlib")
    assert out["isError"] is False
    assert out["content"][0]["text"] == "LINE1\nLINE2\n"
    assert "TYPE A" in fake.commands  # EBCDIC converted on the wire


def test_dataset_read_pds_member() -> None:
    fake = FakeFTP()
    fake.files["'SYS1.PROCLIB'(IEFBR14)"] = b"//X EXEC PGM=IEFBR14\n"
    out = bridge.dataset_read(_session(fake), "SYS1.PROCLIB", member="iefbr14")
    assert out["isError"] is False
    assert "IEFBR14" in out["content"][0]["text"]


def test_dataset_read_pds_without_member_lists_members() -> None:
    fake = FakeFTP()
    fake.listings["'SYS1.PROCLIB'"] = ["IEFBR14", "SORT"]
    out = bridge.dataset_read(_session(fake), "SYS1.PROCLIB")
    assert out["isError"] is False
    assert "IEFBR14" in out["content"][0]["text"]


def test_dataset_read_missing_is_not_found() -> None:
    fake = FakeFTP()
    out = bridge.dataset_read(_session(fake), "NO.SUCH.DSN")
    assert out["isError"] is True
    assert out["content"][0]["text"].startswith("not_found")


def test_dataset_read_empty_name_rejected() -> None:
    out = bridge.dataset_read(_session(FakeFTP()), "   ")
    assert out["isError"] is True


def test_uss_read_and_path_guards() -> None:
    fake = FakeFTP()
    fake.files["/u/reader/notes.txt"] = b"hello\n"
    out = bridge.uss_read(_session(fake), "/u/reader/notes.txt")
    assert out["isError"] is False
    assert out["content"][0]["text"] == "hello\n"
    assert bridge.uss_read(_session(FakeFTP()), "relative/path")["isError"] is True
    assert bridge.uss_read(_session(FakeFTP()), "/u/x/../y")["isError"] is True


def test_job_status_parses_rc_and_filters() -> None:
    fake = FakeFTP()
    fake.jes_lines = list(JES_FIXTURE)
    out = bridge.job_status(_session(fake), job_name="PAY*")
    assert out["isError"] is False
    text = out["content"][0]["text"]
    assert "PAYROLL JOB00123 OUTPUT RC=0000" in text
    assert "BACKUP" in text
    assert "GARBAGE-LINE-WITHOUT-FIELDS" in text  # unknown shapes pass through
    assert any("SITE JESJOBNAME=PAY*" in c for c in fake.commands)
    only = bridge.job_status(_session(fake), job_id="job00124")
    assert "BACKUP" in only["content"][0]["text"]
    assert "PAYROLL" not in only["content"][0]["text"]


def test_jes_spool_read_and_arg_guards() -> None:
    fake = FakeFTP()
    fake.files["JOB00123.3"] = b"IEF142I PAYROLL ENDED RC=0\n"
    out = bridge.jes_spool_read(_session(fake), "job00123", "3")
    assert out["isError"] is False
    assert "RC=0" in out["content"][0]["text"]
    assert bridge.jes_spool_read(_session(FakeFTP()), "JOB00123", "SYSOUT")["isError"] is True
    assert bridge.jes_spool_read(_session(FakeFTP()), "", "3")["isError"] is True


def test_byte_cap_truncates_with_suffix() -> None:
    fake = FakeFTP()
    fake.files["'BIG.DATA'"] = b"x" * 3000
    out = bridge.dataset_read(_session(fake, max_bytes=1000), "BIG.DATA")
    text = out["content"][0]["text"]
    assert out["isError"] is False
    assert text.endswith(bridge.TRUNCATION_SUFFIX)
    assert len(text) == 1000 + len(bridge.TRUNCATION_SUFFIX)
    assert fake.aborted  # transfer aborted, not downloaded whole


def test_timeout_maps_to_stable_code() -> None:
    class SlowFTP(FakeFTP):
        def retrbinary(self, cmd: str, callback) -> str:
            raise TimeoutError("timed out")

    out = bridge.dataset_read(_session(SlowFTP()), "BIG.DATA")
    assert out["isError"] is True
    assert out["content"][0]["text"].startswith("timeout")


def test_site_failure_is_per_tool() -> None:
    """JES refused: job tools report jes_unavailable while dataset reads
    (no SITE JES involved) keep working — never all-or-nothing."""

    class NoJesFTP(FakeFTP):
        site_fails = True

    out = bridge.job_status(_session(NoJesFTP()))
    assert out["isError"] is True
    assert out["content"][0]["text"].startswith("jes_unavailable")
    ok = FakeFTP()
    ok.files["'A.B'"] = b"data\n"
    assert bridge.dataset_read(_session(ok), "A.B")["isError"] is False


def test_errors_never_carry_credentials() -> None:
    fake = FakeFTP()
    for out in (
        bridge.dataset_read(_session(fake), "NOPE.DSN"),
        bridge.job_status(_session(fake)),
    ):
        assert "s3cret" not in out["content"][0]["text"]


def test_connect_logs_in_with_config() -> None:
    config = _config()
    assert isinstance(bridge.connect(config, FakeFTP), FakeFTP)


# ------------------------------------------------------- MCP framing


def _factory():
    return _config(), FakeFTP


def _call(name: str, args: dict) -> dict:
    config = _config()
    return server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args}},
        config,
        lambda _cfg: FakeFTP(),
    )


def test_initialize_negotiates_and_identifies() -> None:
    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 7, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        _config(),
        lambda _cfg: FakeFTP(),
    )
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["serverInfo"]["name"] == "mainframe-ftp-bridge"
    assert reply["result"]["capabilities"] == {"tools": {}}
    assert server.handle_request({"jsonrpc": "2.0", "id": 8, "method": "ping"}, _config(), lambda _c: FakeFTP())["result"] == {}


def test_tools_list_registers_exactly_the_allowlist() -> None:
    """Registration lock: the dispatch table IS the capability tier — any
    fifth tool fails this test."""
    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        _config(),
        lambda _cfg: FakeFTP(),
    )
    names = [t["name"] for t in reply["result"]["tools"]]
    assert names == ["dataset_read", "uss_read", "job_status", "jes_spool_read"]
    for tool in reply["result"]["tools"]:
        assert set(tool["inputSchema"]) >= {"type", "properties"}


def test_tools_call_unknown_and_bad_args_rejected() -> None:
    config = _config()

    def connect(_cfg) -> FakeFTP:
        return FakeFTP()
    bad_tool = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "job_submit", "arguments": {}}},
        config,
        connect,
    )
    assert bad_tool["error"]["code"] == -32602
    bad_args = server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "uss_read", "arguments": {"path": 42}}},
        config,
        connect,
    )
    assert bad_args["error"]["code"] == -32602
    assert server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "nope", "params": {}},
        config,
        connect,
    )["error"]["code"] == -32601
    assert server.handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "notifications/initialized", "params": {}},
        config,
        connect,
    ) is None
    assert server.handle_request("garbage", config, connect)["error"]["code"] == -32600


def test_tools_call_logs_tool_name_only(capsys) -> None:
    """Access log (operators grep this): tool name on stderr, never args or
    content. Hermetic pin — the sim tier proves the call, this proves the log."""
    fake = FakeFTP()
    fake.files["'A.B'"] = b"hello-mock-bytes\n"
    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "dataset_read", "arguments": {"dataset": "A.B"}}},
        _config(),
        lambda _cfg: fake,
    )
    assert reply["result"]["isError"] is False
    logged = capsys.readouterr().err
    assert "mcp tools/call name=dataset_read" in logged
    assert "A.B" not in logged
    assert "hello-mock-bytes" not in logged


def test_stdio_loop_frames_replies_and_skips_blanks(monkeypatch) -> None:
    import sys

    config = _config()
    stdin = io.StringIO(
        "\n"
        '{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}\n'
        "not json\n"
        '{"jsonrpc": "2.0", "id": 2, "method": "notifications/initialized", "params": {}}\n'
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    server.serve_stdio(config, lambda _cfg: FakeFTP())
    frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [f.get("id") for f in frames] == [1, None]  # list reply + parse error
    assert [t["name"] for t in frames[0]["result"]["tools"]] == [
        "dataset_read",
        "uss_read",
        "job_status",
        "jes_spool_read",
    ]
    assert frames[1]["error"]["code"] == -32700


def test_http_transport_lists_tools() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(server.create_app(_config(), lambda _cfg: FakeFTP()))
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert resp.status_code == 200
    assert [t["name"] for t in resp.json()["result"]["tools"]] == [
        "dataset_read",
        "uss_read",
        "job_status",
        "jes_spool_read",
    ]
    bad = client.post("/mcp", content=b"{oops", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400


def test_main_refuses_without_credentials(monkeypatch, tmp_path) -> None:
    from mainframe_rag.mcp.__main__ import main

    for key in ("MCP_FTP_HOST", "MCP_FTP_USER", "MCP_FTP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    assert main(["--transport", "stdio"]) == 2


# ------------------------------------------------------- Tracing (OTel path)


def _test_tracer():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_tools_call_span_joins_parent_and_carries_bounded_attrs(monkeypatch) -> None:
    """tools.call emits a span parented to the extracted W3C context with
    ids/counts only — tool name, byte counts, elapsed, error flag."""
    from opentelemetry import trace

    from mainframe_rag.mcp import server as server_mod

    provider, exporter = _test_tracer()
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(server_mod, "tracer", test_tracer)
    fake = FakeFTP()
    fake.files["JOB00123.3"] = b"IEF142I PAYROLL ENDED RC=0\n"

    parent = test_tracer.start_span("agent-live-fetch")
    parent_ctx = trace.set_span_in_context(parent)
    reply = server_mod.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "jes_spool_read",
                    "arguments": {"job_id": "JOB00123", "spool_id": "3"}}},
        _config(),
        lambda _cfg: fake,
        parent_context=parent_ctx,
    )
    parent.end()
    assert reply["result"]["isError"] is False
    child = next(s for s in exporter.get_finished_spans() if s.name == "tools.call")
    assert child.parent is not None
    assert child.parent.span_id == parent.get_span_context().span_id
    attrs = dict(child.attributes or {})
    assert attrs["mcp.tool"] == "jes_spool_read"
    assert attrs["mcp.bytes_out"] == len(b"IEF142I PAYROLL ENDED RC=0\n")
    assert attrs["mcp.is_error"] is False
    assert isinstance(attrs["mcp.elapsed_ms"], int)


def test_span_attrs_never_carry_content_or_secrets(monkeypatch) -> None:
    """Adversarial: spool bytes containing secret-looking text must not
    appear in any span attribute value."""
    from mainframe_rag.mcp import server as server_mod

    provider, exporter = _test_tracer()
    monkeypatch.setattr(server_mod, "tracer", provider.get_tracer("test"))
    fake = FakeFTP()
    fake.files["'PAYROLL.DATA'"] = b"password hunter2\nSECRET=abc123\nsalary 99999\n"
    server_mod.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "dataset_read", "arguments": {"dataset": "PAYROLL.DATA"}}},
        _config(),
        lambda _cfg: fake,
    )
    haystack = json.dumps(
        [dict(s.attributes or {}) for s in exporter.get_finished_spans()], default=str
    )
    assert "hunter2" not in haystack
    assert "abc123" not in haystack
    assert "s3cret" not in haystack


def test_tracing_off_by_default_serves_untraced(monkeypatch) -> None:
    """No endpoint configured: the proxy tracer no-ops and serving is
    byte-identical (this is also what the pre-tracing tests exercise)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    fake = FakeFTP()
    fake.files["/u/r/n.txt"] = b"hi\n"
    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "uss_read", "arguments": {"path": "/u/r/n.txt"}}},
        _config(),
        lambda _cfg: fake,
    )
    assert reply["result"]["content"][0]["text"] == "hi\n"


def test_lifespan_wires_tracer_setup_and_shutdown(monkeypatch) -> None:
    """HTTP app lifespan owns the tracer: setup with the env endpoint on
    startup, flush on shutdown."""
    from fastapi.testclient import TestClient

    from mainframe_rag import tracing as tracing_mod
    from mainframe_rag.mcp import server as server_mod

    calls: dict = {}
    monkeypatch.setattr(
        tracing_mod, "setup_tracing",
        lambda endpoint, **kw: calls.setdefault("setup", endpoint),
    )
    monkeypatch.setattr(
        tracing_mod, "shutdown_tracing", lambda: calls.setdefault("shutdown", True)
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")
    with TestClient(server_mod.create_app(_config(), lambda _cfg: FakeFTP())):
        pass
    assert calls == {"setup": "http://jaeger:4318", "shutdown": True}


def test_sample_ratio_from_env_clamps_and_falls_back(monkeypatch) -> None:
    from mainframe_rag.mcp.server import sample_ratio_from_env

    monkeypatch.setenv("OTEL_SAMPLE_RATIO", "0.25")
    assert sample_ratio_from_env() == 0.25
    monkeypatch.setenv("OTEL_SAMPLE_RATIO", "9")
    assert sample_ratio_from_env() == 1.0
    monkeypatch.setenv("OTEL_SAMPLE_RATIO", "garbage")
    assert sample_ratio_from_env() == 1.0
    monkeypatch.delenv("OTEL_SAMPLE_RATIO", raising=False)
    assert sample_ratio_from_env() == 1.0
