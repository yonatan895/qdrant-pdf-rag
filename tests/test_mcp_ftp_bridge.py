"""FTP MCP bridge tests (ADR-0003, ADR-0012 PR 1, #1302).

Hermetic: FakeFTP replaces the mainframe wire for unit/policy tests, while
FTPS verification and logon-free probe tests spin up a real loopback TLS
socket with an ephemeral openssl-generated CA and certificates in tmp_path.
No external network, no real credentials, no binary fixtures committed.
"""

from __future__ import annotations

import asyncio
import contextlib
import ftplib
import io
import json
import socket
import ssl
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Self

import httpx2
import pytest
from fastapi.testclient import TestClient

from mainframe_rag.mcp import bridge, server
from mainframe_rag.mcp.bridge import FTPConfig, LogonBreaker


class FakeFTP:
    """Duck-typed ftplib.FTP: scripted replies, recorded commands."""

    files: dict[str, bytes] | None = None
    listings: dict[str, list[str]] | None = None
    jes_lines: list[str] | None = None
    jes_list_by_job: dict[str, list[str]] | None = None
    site_fails: bool = False
    login_error: Exception | None = None

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.logged_in: tuple | None = None
        self.aborted = False
        self.pasv: bool | None = None
        self.closed = False
        self.files = dict(type(self).files or {})
        self.listings = dict(type(self).listings or {})
        self.jes_lines = list(type(self).jes_lines or [])
        self.jes_list_by_job = {
            k: list(v) for k, v in (type(self).jes_list_by_job or {}).items()
        }

    def connect(self, host: str, port: int = 21, timeout: float = 15.0) -> str:
        self.commands.append(f"CONNECT {host}:{port}")
        return "220 welcome"

    def login(self, user: str, passwd: str, acct: str = "", secure: bool = True) -> str:
        if type(self).login_error is not None:
            raise type(self).login_error
        self.logged_in = (user, passwd)
        return "230 logged in"

    def set_pasv(self, val: bool) -> None:
        self.pasv = bool(val)

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
        _, _, arg = cmd.partition(" ")
        key = arg.strip().upper()
        lines = self.jes_list_by_job.get(key, self.jes_lines) if key else self.jes_lines
        for line in lines:
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

    def close(self) -> None:
        self.closed = True

    def abort(self) -> str:
        self.aborted = True
        return "226 aborted"


def _config(**overrides) -> FTPConfig:
    base: dict = {
        "host": "mf.example.com",
        "user": "READER",
        "password": "s3cret",
        "transport_mode": "plain",
        "plain_ftp_accepted": True,
        "dataset_allowlist": ("SYS1.PARMLIB", "SYS1.PROCLIB(IEFBR14)", "A.B", "BIG.DATA"),
        "enabled_tools": ("dataset_read",),
    }
    base.update(overrides)
    return FTPConfig(**base)


def _jes_config(**overrides) -> FTPConfig:
    base: dict = {
        "enabled_tools": ("dataset_read", "job_status", "jes_spool_read"),
        "jes_jobname_prefixes": ("PAY", "BACK"),
        "jes_owner_prefixes": ("READER",),
    }
    base.update(overrides)
    return _config(**base)


def _session(fake: FakeFTP, **overrides) -> bridge.FTPSession:
    return bridge.FTPSession(fake, _config(**overrides))


JES_FIXTURE = [
    "JOBNAME  JOBID    OWNER    STATUS CLASS",
    "PAYROLL  JOB00123 READER   OUTPUT A        RC=0000",
    "BACKUP   JOB00124 READER   ACTIVE B",
    "OTHERJOB JOB00999 OTHERUSR OUTPUT A        RC=0000",
    "GARBAGE-LINE-WITHOUT-FIELDS",
]

JES_LIST_JOB00123 = [
    "JOBNAME  JOBID    OWNER    STATUS CLASS",
    "PAYROLL  JOB00123 READER   OUTPUT A        RC=0000",
    "--------",
    "         ID  STEPNAME PROCSTEP C DDNAME   BYTE-COUNT",
    "         001 JES2              A JESJCL          412",
    "         002 JES2              A JESMSGLG       1584",
    "         003 JES2              A JESYSMSG        920",
    "         004 STEP1    PROC1    A SYSPRINT       2048",
    "         005 STEP1             A SYSOUT         1024",
    "5 spool files",
]

JES_LIST_JOB00999 = [
    "JOBNAME  JOBID    OWNER    STATUS CLASS",
    "OTHERJOB JOB00999 OTHERUSR OUTPUT A        RC=0000",
    "--------",
    "         ID  STEPNAME PROCSTEP C DDNAME   BYTE-COUNT",
    "         002 JES2              A JESMSGLG        512",
    "1 spool files",
]


# ------------------------------------------- D2: dataset allowlist & no NLST


def test_dataset_read_allowlisted_sequential_member_and_no_nlst_fallback() -> None:
    fake = FakeFTP()
    fake.files["'SYS1.PARMLIB'"] = b"LINE1\nLINE2\n"
    fake.files["'SYS1.PROCLIB'(IEFBR14)"] = b"//X EXEC PGM=IEFBR14\n"
    fake.listings["'SYS1.PROCLIB'"] = ["IEFBR14", "SORT"]

    # Exact sequential dataset (case-normalized to allowlist)
    out = bridge.dataset_read(_session(fake), "sys1.parmlib")
    assert out["isError"] is False
    text = out["content"][0]["text"]
    assert "LINE1\nLINE2\n" in text
    assert text.startswith(bridge.OBS_BEGIN_PREFIX)
    assert text.endswith(bridge.OBS_END_MARKER)
    assert "TYPE A" in fake.commands

    # Exact PDS member via separate member arg and inline syntax
    out = bridge.dataset_read(_session(fake), "SYS1.PROCLIB", member="iefbr14")
    assert out["isError"] is False and "IEFBR14" in out["content"][0]["text"]
    out_inline = bridge.dataset_read(_session(fake), "SYS1.PROCLIB(IEFBR14)")
    assert out_inline["isError"] is False and "IEFBR14" in out_inline["content"][0]["text"]

    # PDS without member is DENIED when only SYS1.PROCLIB(IEFBR14) is allowlisted
    pds_fake = FakeFTP()
    pds_fake.listings["'SYS1.PROCLIB'"] = ["IEFBR14", "SORT"]
    denied = bridge.dataset_read(_session(pds_fake), "SYS1.PROCLIB")
    assert denied["isError"] is True
    assert denied["content"][0]["text"].startswith("denied:")
    assert pds_fake.commands == []

    # Even if an allowlisted sequential name returns 550 (e.g. actually a PDS),
    # NO NLST fallback is ever issued (ADR-0012 D2 point 3).
    pds_550 = FakeFTP()
    pds_550.listings["'SYS1.PARMLIB'"] = ["IEASYS00"]
    missing = bridge.dataset_read(_session(pds_550), "SYS1.PARMLIB")
    assert missing["isError"] is True
    assert missing["content"][0]["text"].startswith("not_found:")
    assert not any(c.startswith("NLST") for c in pds_550.commands)
    assert "s3cret" not in missing["content"][0]["text"]


@pytest.mark.parametrize(
    "dataset,member,expected_prefix",
    [
        # Off-allowlist names (including case variation)
        ("SYS1.LINKLIB", None, "denied:"),
        ("sys1.linklib", None, "denied:"),
        # Member tricks on allowlisted sequential or PDS
        ("SYS1.PARMLIB", "IEASYS00", "denied:"),
        ("SYS1.PARMLIB(IEASYS00)", None, "denied:"),
        ("SYS1.PROCLIB", "SORT", "denied:"),
        ("SYS1.PROCLIB(SORT)", None, "denied:"),
        ("SYS1.PROCLIB(IEFBR14)", "SORT", "invalid_params:"),
        # Quoting tricks
        ("'SYS1.PARMLIB'", None, "invalid_params:"),
        ('"SYS1.PARMLIB"', None, "invalid_params:"),
        ("SYS1.PROCLIB", "'IEFBR14'", "invalid_params:"),
        # Wildcards
        ("SYS1.*", None, "invalid_params:"),
        ("SYS1.PARM%", None, "invalid_params:"),
        ("SYS1.PARM?", None, "invalid_params:"),
        ("SYS1.PROCLIB", "IEF*", "invalid_params:"),
        # Relative GDG references
        ("SYS1.PARMLIB(0)", None, "invalid_params:"),
        ("SYS1.PARMLIB(-1)", None, "invalid_params:"),
        ("SYS1.PARMLIB(+1)", None, "invalid_params:"),
        ("SYS1.PARMLIB", "0", "invalid_params:"),
        ("SYS1.PARMLIB", "-1", "invalid_params:"),
        # Control chars / CRLF injection / non-ASCII lookalike / malformed
        ("SYS1.PARMLIB\r\nSTOR EVIL", None, "invalid_params:"),
        ("SYS1.PARMLIB\x00", None, "invalid_params:"),
        ("SY\u04051.PARMLIB", None, "invalid_params:"),
        ("   ", None, "invalid_params:"),
        ("SYS1..PARMLIB", None, "invalid_params:"),
        (".SYS1.PARMLIB", None, "invalid_params:"),
        ("1BAD.QUAL", None, "invalid_params:"),
        ("A.B.C.D.E.F.G.H.I.J.K.L.M.N.O.P.Q.R.S.T.U.V.W", None, "invalid_params:"),
        ("SYS1.PROCLIB(TOOLONGMEMBER)", None, "invalid_params:"),
    ],
)
def test_dataset_allowlist_bypass_attempts_send_zero_ftp_commands(
    dataset: str, member: str | None, expected_prefix: str
) -> None:
    """ADR-0012 G1: every bypass attempt is refused before any FTP connect/command."""
    connect_calls = 0

    def connect(_cfg: FTPConfig) -> FakeFTP:
        nonlocal connect_calls
        connect_calls += 1
        return FakeFTP()

    args: dict = {"dataset": dataset}
    if member is not None:
        args["member"] = member
    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "dataset_read", "arguments": args}},
        _config(),
        connect,
    )
    assert reply is not None
    if "error" in reply:
        assert reply["error"]["code"] == server.INVALID_PARAMS
    else:
        result = reply["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"].startswith(expected_prefix)
    assert connect_calls == 0


def test_empty_dataset_allowlist_fails_closed() -> None:
    fake = FakeFTP()
    fake.files["'SYS1.PARMLIB'"] = b"DATA\n"
    out = bridge.dataset_read(_session(fake, dataset_allowlist=()), "SYS1.PARMLIB")
    assert out["isError"] is True
    assert out["content"][0]["text"].startswith("denied:")
    assert fake.commands == []


def test_config_rejects_wildcards_quotes_or_gdg_in_allowlist() -> None:
    for bad in ("SYS1.*", "'SYS1.PARMLIB'", "SYS1.GDG(0)", "SYS1.GDG(-1)"):
        with pytest.raises(ValueError):
            _config(dataset_allowlist=(bad,))


# ------------------------------------------- D1/D7: MCP_ENABLED_TOOLS


def test_enabled_tools_default_dataset_read_only_and_refuses_uss_read() -> None:
    default_cfg = FTPConfig(host="mf.example.com", user="READER", password="s3cret")
    assert default_cfg.enabled_tools == ("dataset_read",)
    assert default_cfg.transport_mode == "ftps"

    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        default_cfg,
        lambda _c: FakeFTP(),
    )
    assert [t["name"] for t in reply["result"]["tools"]] == ["dataset_read"]

    # Calling unregistered tools fails closed with ZERO FTP connections
    connect_calls = 0

    def connect(_cfg: FTPConfig) -> FakeFTP:
        nonlocal connect_calls
        connect_calls += 1
        return FakeFTP()

    for disabled in ("job_status", "jes_spool_read", "uss_read", "job_submit"):
        res = server.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": disabled, "arguments": {}}},
            default_cfg,
            connect,
        )
        assert res["error"]["code"] == server.INVALID_PARAMS
    assert connect_calls == 0

    # Opt-in pilot subset works; uss_read or unknown tools fail closed at config time
    opt_in = _jes_config()
    reply_opt = server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        opt_in,
        lambda _c: FakeFTP(),
    )
    assert [t["name"] for t in reply_opt["result"]["tools"]] == [
        "dataset_read", "job_status", "jes_spool_read"
    ]
    for forbidden_tools in (("uss_read",), ("dataset_read", "uss_read"), ("job_submit",), ()):
        with pytest.raises(ValueError):
            _config(enabled_tools=forbidden_tools)


# ------------------------------------------- D2/D4 Option 4A: JES guardrails


def test_job_status_exact_target_and_prefix_allowlist_guardrails() -> None:
    fake = FakeFTP()
    fake.jes_lines = list(JES_FIXTURE)
    cfg = _jes_config()

    # Wildcard PAY* is REFUSED before any FTP command (ADR-0012 G1)
    bad_wild = bridge.job_status(bridge.FTPSession(fake, cfg), job_name="PAY*", owner="READER")
    assert bad_wild["isError"] is True
    assert bad_wild["content"][0]["text"].startswith("invalid_params:")
    assert fake.commands == []

    # Unscoped query (no exact job_id and no exact job_name+owner) refused
    unscoped = bridge.job_status(bridge.FTPSession(fake, cfg))
    assert unscoped["isError"] is True and unscoped["content"][0]["text"].startswith("invalid_params:")
    assert fake.commands == []

    # Empty JES prefix allowlist fails closed before FTP
    no_prefixes = _jes_config(jes_jobname_prefixes=(), jes_owner_prefixes=())
    denied_empty = bridge.job_status(bridge.FTPSession(fake, no_prefixes), job_id="JOB00123")
    assert denied_empty["isError"] is True and denied_empty["content"][0]["text"].startswith("denied:")
    assert fake.commands == []

    # Exact allowlisted job_id succeeds and excludes garbage/other jobs
    ok = bridge.job_status(bridge.FTPSession(fake, cfg), job_id="job00123")
    assert ok["isError"] is False
    text = ok["content"][0]["text"]
    assert "PAYROLL JOB00123 OUTPUT RC=0000" in text
    assert "BACKUP" not in text
    assert "GARBAGE-LINE-WITHOUT-FIELDS" not in text

    # Off-allowlist job found in LIST is denied
    denied_job = bridge.job_status(bridge.FTPSession(fake, cfg), job_id="JOB00999")
    assert denied_job["isError"] is True
    assert denied_job["content"][0]["text"].startswith("denied:")


def test_jes_spool_read_option_4a_list_prefix_dd_scope_and_type_a() -> None:
    """ADR-0012 Option 4A: LIST before RETR, prefix allowlist, JESMSGLG/JESYSMSG
    DD scope only, explicit TYPE A, and zero RETR for forbidden DDs or jobs."""
    cfg = _jes_config()

    # Allowed DDs: JESMSGLG (2) and JESYSMSG (3)
    for sid, expected_dd in (("2", "JESMSGLG"), ("3", "JESYSMSG")):
        fake = FakeFTP()
        fake.jes_list_by_job["JOB00123"] = list(JES_LIST_JOB00123)
        fake.files[f"JOB00123.{sid}"] = b"IEF142I PAYROLL ENDED RC=0\n"
        out = bridge.jes_spool_read(bridge.FTPSession(fake, cfg), "job00123", sid)
        assert out["isError"] is False
        body = out["content"][0]["text"]
        assert "IEF142I PAYROLL ENDED RC=0" in body
        assert f"target=JOB00123.{sid}({expected_dd})" in body
        # Verify ordering: LIST JOB00123 -> TYPE A -> RETR JOB00123.<sid>
        list_idx = fake.commands.index("LIST JOB00123")
        type_idx = fake.commands.index("TYPE A")
        retr_idx = fake.commands.index(f"RETR JOB00123.{sid}")
        assert list_idx < type_idx < retr_idx

    # Forbidden DDs: JESJCL (1), SYSPRINT (4), SYSOUT (5) -> DENIED with ZERO RETR
    for forbidden_sid in ("1", "4", "5"):
        fake = FakeFTP()
        fake.jes_list_by_job["JOB00123"] = list(JES_LIST_JOB00123)
        fake.files[f"JOB00123.{forbidden_sid}"] = b"SENSITIVE-SPOOL-DATA\n"
        res = bridge.jes_spool_read(bridge.FTPSession(fake, cfg), "JOB00123", forbidden_sid)
        assert res["isError"] is True
        assert res["content"][0]["text"].startswith("denied:")
        assert not any(c.startswith("RETR") for c in fake.commands)

    # Off-allowlist job prefix -> DENIED with ZERO RETR
    off_fake = FakeFTP()
    off_fake.jes_list_by_job["JOB00999"] = list(JES_LIST_JOB00999)
    off_fake.files["JOB00999.2"] = b"OTHER-TEAM-LOG\n"
    res_off = bridge.jes_spool_read(bridge.FTPSession(off_fake, cfg), "JOB00999", "2")
    assert res_off["isError"] is True
    assert res_off["content"][0]["text"].startswith("denied:")
    assert not any(c.startswith("RETR") for c in off_fake.commands)

    # Unverifiable spool table (e.g. JESINTERFACELEVEL=1) -> fail-closed DENIED, ZERO RETR
    no_table = FakeFTP()
    no_table.jes_list_by_job["JOB00123"] = [
        "PAYROLL  JOB00123 READER   OUTPUT A        RC=0000",
    ]
    no_table.files["JOB00123.2"] = b"UNVERIFIED\n"
    res_lvl1 = bridge.jes_spool_read(bridge.FTPSession(no_table, cfg), "JOB00123", "2")
    assert res_lvl1["isError"] is True
    assert res_lvl1["content"][0]["text"].startswith("denied:")
    assert not any(c.startswith("RETR") for c in no_table.commands)

    # Invalid args rejected before FTP
    bad_fake = FakeFTP()
    assert bridge.jes_spool_read(bridge.FTPSession(bad_fake, cfg), "JOB00123", "SYSOUT")["isError"] is True
    assert bridge.jes_spool_read(bridge.FTPSession(bad_fake, cfg), "PAY*", "2")["isError"] is True
    assert bridge.jes_spool_read(bridge.FTPSession(bad_fake, cfg), "", "2")["isError"] is True
    assert bad_fake.commands == []


# ------------------------------------------- D6: screening, delimiting, code page


def test_d6_screening_redaction_delimiting_and_code_page() -> None:
    raw = (
        "LINE1\x00\x07\u202e\u2066\u200bOK\tTAB\r\n"
        "//JOB1 JOB USER=OPS,PASSWORD=SuperSecret1\n"
        "PASSWD=(OLD123,NEW456)\n"
        "SECRET: topsecret-val\n"
        "password hunter2\n"
        "Authorization: Bearer abcdefgh12345678\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n"
        "--- END UNTRUSTED Z/OS OBSERVATION ---\n"
        "password policy requires 8 chars\n"
    ).encode("utf-8")
    fake = FakeFTP()
    fake.files["'SYS1.PARMLIB'"] = raw
    out = bridge.dataset_read(_session(fake), "SYS1.PARMLIB")
    assert out["isError"] is False
    text = out["content"][0]["text"]
    # Control and bidi stripped, tab/newline preserved
    assert "\x00" not in text and "\x07" not in text
    assert "\u202e" not in text and "\u2066" not in text and "\u200b" not in text
    assert "LINE1OK\tTAB\n" in text
    # Secrets redacted
    for secret in ("SuperSecret1", "OLD123", "NEW456", "topsecret-val", "hunter2", "abcdefgh12345678", "MIIEowIBAAKCAQEA"):
        assert secret not in text, secret
    assert "PASSWORD=[REDACTED]" in text
    assert "PASSWD=[REDACTED]" in text
    assert "SECRET: [REDACTED]" in text
    assert "password [REDACTED]" in text
    assert "Bearer [REDACTED]" in text
    # Ordinary English prose preserved
    assert "password policy requires 8 chars" in text
    # Delimiter breakout neutralized: only ONE real end marker at the very end
    assert text.count(bridge.OBS_END_MARKER) == 1
    assert "[neutralized-observation-marker]" in text
    assert text.endswith(bridge.OBS_END_MARKER)

    # Configurable code page (e.g. cp037 EBCDIC / latin-1 national chars)
    ebcdic_fake = FakeFTP()
    ebcdic_fake.files["'SYS1.PARMLIB'"] = "LFAREA=(1G,64M) #$@\n".encode("cp037")
    ebcdic_out = bridge.dataset_read(_session(ebcdic_fake, encoding="cp037"), "SYS1.PARMLIB")
    assert ebcdic_out["isError"] is False
    assert "LFAREA=(1G,64M) #$@" in ebcdic_out["content"][0]["text"]

    with pytest.raises(ValueError, match="unknown encoding"):
        _config(encoding="no-such-codepage-999")


def test_byte_cap_truncates_with_suffix() -> None:
    fake = FakeFTP()
    fake.files["'BIG.DATA'"] = b"x" * 3000
    out = bridge.dataset_read(_session(fake, max_bytes=1000), "BIG.DATA")
    text = out["content"][0]["text"]
    assert out["isError"] is False
    assert bridge.TRUNCATION_SUFFIX in text
    assert "x" * 1000 in text
    assert "x" * 1001 not in text
    assert fake.aborted  # transfer aborted, not downloaded whole


@pytest.mark.parametrize(
    ("line", "secrets", "kept"),
    [
        ("ALTUSER IBMUSER PASSWORD(NEWPW1) NOEXPIRED", ["NEWPW1"], ["ALTUSER IBMUSER", "NOEXPIRED"]),
        ("ALU IBMUSER PHRASE('correct horse battery')", ["correct", "horse", "battery"], ["ALU IBMUSER"]),
        ("PW(SECRET99)", ["SECRET99"], ["PW([REDACTED])"]),
        ("pass(Lower7x) pw(lower8y)", ["Lower7x", "lower8y"], []),
        ("Password ( Mixed8Case )", ["Mixed8Case"], []),
        ("PHRASE ('it''s ) a tricky one')", ["it''s", "tricky one"], []),
        ('PASSPHRASE("dq phrase val")', ["dq phrase val"], []),
        ("NEWPASSWORD(NPW77) PASSWD(PWD88) PWD(PW999) NEWPHRASE('np phrase')", ["NPW77", "PWD88", "PW999", "np phrase"], []),
        ("PASSWORD PASSWORD(OLDPW1 NEWPW2)", ["OLDPW1", "NEWPW2"], []),
        ("//STEP1 EXEC PGM=X,PARM='PW=JCLPW1,PASS=JCLPW2,NEWPASSWORD=JCLPW3'", ["JCLPW1", "JCLPW2", "JCLPW3"], ["PGM=X"]),
        ("RDEFINE PTKTDATA APPL1 SSIGNON(KEYMASKED(0123456789ABCDEF))", ["0123456789ABCDEF"], ["RDEFINE PTKTDATA APPL1"]),
        ("LOGON IBMUSER/TSOPW1/TSONEW2 SIZE(4096)", ["TSOPW1", "TSONEW2"], ["LOGON IBMUSER", "SIZE(4096)"]),
        ("ALU IBMUSER NOPASSWORD NOPHRASE", [], ["ALU IBMUSER NOPASSWORD NOPHRASE"]),
    ],
)
def test_d6_redacts_zos_password_operand_forms(line: str, secrets: list[str], kept: list[str]) -> None:
    """B2 (#1331 review): RACF/TSO/JCL password and passphrase operands are
    redacted in the tool result, quoted or not, any case, without eating the
    neighbouring operands."""
    fake = FakeFTP()
    fake.files["'SYS1.PARMLIB'"] = (line + "\n").encode("utf-8")
    out = bridge.dataset_read(_session(fake), "SYS1.PARMLIB")
    assert out["isError"] is False
    text = out["content"][0]["text"]
    for secret in secrets:
        assert secret not in text, secret
    for neighbour in kept:
        assert neighbour in text, neighbour


@pytest.mark.parametrize(
    ("operand", "leak"),
    [("PASSWORD(SECRETPW99)", "SECR"), ("PHRASE('cut phrase words')", "cut ph")],
)
def test_d6_redacts_operand_cut_open_by_the_byte_cap(operand: str, leak: str) -> None:
    """The cap cuts raw bytes before screening; an operand left unclosed at the
    cut must not leak its prefix."""
    prefix = b"X" * 980 + b" ALU IBMUSER "
    data = prefix + operand.encode("ascii") + b" NOEXPIRED\n"
    cut = len(prefix) + operand.index(leak) + len(leak)
    fake = FakeFTP()
    fake.files["'BIG.DATA'"] = data
    out = bridge.dataset_read(_session(fake, max_bytes=cut), "BIG.DATA")
    text = out["content"][0]["text"]
    assert bridge.TRUNCATION_SUFFIX in text
    assert leak not in text


def test_job_status_output_is_byte_capped_and_stops_the_listing() -> None:
    """job_status follows dataset_read's cap: at most max_bytes of rendered
    job lines plus the truncation suffix, and the LIST transfer is aborted
    instead of being buffered whole."""
    total = 60000
    rows = [f"PAYROLL  JOB{i:05d} READER   OUTPUT A        RC=0000" for i in range(total)]

    class ManyJobsFTP(FakeFTP):
        delivered = 0

        def retrlines(self, cmd: str, callback) -> str:
            self.commands.append(cmd)
            for row in rows:
                self.delivered += 1
                callback(row)
            return "226 done"

    fake = ManyJobsFTP()
    out = bridge.job_status(
        bridge.FTPSession(fake, _jes_config(max_bytes=1000)), job_name="PAYROLL", owner="READER"
    )
    assert out["isError"] is False
    text = out["content"][0]["text"]
    assert bridge.TRUNCATION_SUFFIX in text
    body = text.split("\n", 1)[1]
    payload = body[: body.index(bridge.TRUNCATION_SUFFIX)]
    assert len(payload.encode("utf-8")) == 1000
    assert payload.startswith("PAYROLL JOB00000 OUTPUT RC=0000\n")
    assert fake.aborted
    assert fake.delivered < total


def test_timeout_and_jes_refusal_map_to_stable_codes_per_tool() -> None:
    class SlowFTP(FakeFTP):
        def retrbinary(self, cmd: str, callback) -> str:
            raise TimeoutError("timed out")

    out = bridge.dataset_read(_session(SlowFTP()), "BIG.DATA")
    assert out["isError"] is True
    assert out["content"][0]["text"].startswith("timeout:")

    class NoJesFTP(FakeFTP):
        site_fails = True

    jes_cfg = _jes_config()
    out = bridge.job_status(bridge.FTPSession(NoJesFTP(), jes_cfg), job_id="JOB00123")
    assert out["isError"] is True
    assert out["content"][0]["text"].startswith("jes_unavailable:")
    assert "s3cret" not in out["content"][0]["text"]
    out = bridge.jes_spool_read(bridge.FTPSession(NoJesFTP(), jes_cfg), "JOB00123", "2")
    assert out["isError"] is True
    assert out["content"][0]["text"].startswith("jes_unavailable:")
    ok = FakeFTP()
    ok.files["'A.B'"] = b"data\n"
    assert bridge.dataset_read(_session(ok), "A.B")["isError"] is False


# ------------------------------------------- D4/D5: transport, PASV, real FTPS TLS, probe


def test_plain_ftp_requires_explicit_owner_acceptance_and_sets_pasv() -> None:
    with pytest.raises(ValueError, match="plain_ftp_accepted"):
        FTPConfig(host="mf.example.com", user="READER", password="s3cret", transport_mode="plain")

    plain = bridge.connect(_config(transport_mode="plain", plain_ftp_accepted=True), factory=FakeFTP)
    assert type(plain) is FakeFTP
    assert plain.commands == ["CONNECT mf.example.com:21"]
    assert plain.logged_in == ("READER", "s3cret")
    assert plain.pasv is True


def _generate_tls_fixtures(tmp_path: Path) -> dict[str, Path]:
    """Generate an ephemeral CA, valid localhost cert, wrong-host cert, and
    self-signed cert using openssl CLI (no committed keys/certs)."""

    def _run(*args: str) -> None:
        subprocess.run(["openssl", *args], check=True, capture_output=True)

    ca_key = tmp_path / "ca.key"
    ca_crt = tmp_path / "ca.crt"
    _run("req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(ca_key), "-out", str(ca_crt),
         "-days", "1", "-subj", "/CN=Test-Site-CA",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign")

    def _signed_cert(prefix: str, cn: str, san: str) -> tuple[Path, Path]:
        key = tmp_path / f"{prefix}.key"
        csr = tmp_path / f"{prefix}.csr"
        crt = tmp_path / f"{prefix}.crt"
        ext = tmp_path / f"{prefix}.ext"
        ext.write_text(f"subjectAltName={san}\n", encoding="utf-8")
        _run("req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={cn}")
        _run("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
             "-CAcreateserial", "-out", str(crt), "-days", "1", "-extfile", str(ext))
        return key, crt

    good_key, good_crt = _signed_cert("good", "localhost", "DNS:localhost,IP:127.0.0.1")
    wrong_key, wrong_crt = _signed_cert("wrong", "wrong.example.com", "DNS:wrong.example.com")

    ss_key = tmp_path / "selfsigned.key"
    ss_crt = tmp_path / "selfsigned.crt"
    _run("req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(ss_key), "-out", str(ss_crt),
         "-days", "1", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1")

    return {
        "ca_crt": ca_crt,
        "good_key": good_key,
        "good_crt": good_crt,
        "wrong_key": wrong_key,
        "wrong_crt": wrong_crt,
        "ss_key": ss_key,
        "ss_crt": ss_crt,
    }


class _MiniFTPSServer:
    """Minimal real-socket FTP/FTPS control server for TLS verification tests."""

    def __init__(self, *, certfile: Path | None = None, keyfile: Path | None = None, reject_auth: bool = False) -> None:
        self._certfile = certfile
        self._keyfile = keyfile
        self._reject_auth = reject_auth
        self.received_commands: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2.0)

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self._handle(conn)

    def _handle(self, raw_conn: socket.socket) -> None:
        conn: socket.socket = raw_conn
        conn.settimeout(2.0)
        try:
            conn.sendall(b"220 mini-ftps ready\r\n")
            buf = b""
            while True:
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                line_bytes, _, buf = buf.partition(b"\n")
                line = line_bytes.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                self.received_commands.append(line)
                upper = line.upper()
                if upper == "AUTH TLS":
                    if self._reject_auth or self._certfile is None or self._keyfile is None:
                        conn.sendall(b"500 AUTH TLS not supported\r\n")
                        continue
                    conn.sendall(b"234 Proceed with negotiation\r\n")
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(certfile=str(self._certfile), keyfile=str(self._keyfile))
                    conn = ctx.wrap_socket(conn, server_side=True)
                elif upper.startswith("USER "):
                    conn.sendall(b"331 Password required\r\n")
                elif upper.startswith("PASS "):
                    conn.sendall(b"230 Logged in\r\n")
                elif upper == "PBSZ 0":
                    conn.sendall(b"200 PBSZ 0 ok\r\n")
                elif upper == "PROT P":
                    conn.sendall(b"200 PROT P ok\r\n")
                elif upper == "QUIT":
                    conn.sendall(b"221 Bye\r\n")
                    return
                else:
                    conn.sendall(b"200 Ok\r\n")
        except (OSError, ssl.SSLError):
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def test_ftps_real_tls_verifies_ca_and_hostname_and_never_downgrades(tmp_path: Path) -> None:
    """ADR-0012 D5: verified FTPS succeeds with matching CA+hostname, refuses
    self-signed and wrong-hostname certs fail-closed, refuses AUTH TLS rejection
    with zero plain-FTP fallback, and NEVER sends USER/PASS on TLS failure."""
    certs = _generate_tls_fixtures(tmp_path)

    # 1. Valid CA-signed cert for localhost -> succeeds, PROT P and set_pasv(True) called
    with _MiniFTPSServer(certfile=certs["good_crt"], keyfile=certs["good_key"]) as srv:
        cfg = FTPConfig(
            host="localhost",
            port=srv.port,
            user="READER",
            password="s3cret",
            transport_mode="ftps",
            ca_file=str(certs["ca_crt"]),
        )
        ftp = bridge.connect(cfg)
        try:
            assert isinstance(ftp, ftplib.FTP_TLS)
            assert ftp.passiveserver is True
        finally:
            ftp.quit()
        assert "AUTH TLS" in srv.received_commands
        assert "PROT P" in srv.received_commands
        assert "USER READER" in srv.received_commands

    # 2. Self-signed untrusted cert -> REFUSED fail-closed, zero USER/PASS sent
    with _MiniFTPSServer(certfile=certs["ss_crt"], keyfile=certs["ss_key"]) as srv:
        cfg = FTPConfig(
            host="localhost",
            port=srv.port,
            user="READER",
            password="s3cret",
            transport_mode="ftps",
            ca_file=str(certs["ca_crt"]),
        )
        with pytest.raises(bridge._TlsVerificationError):
            bridge.connect(cfg)
        assert not any(c.startswith(("USER ", "PASS ")) for c in srv.received_commands)

    # 3. CA-signed cert with wrong hostname -> REFUSED fail-closed, zero USER/PASS sent
    with _MiniFTPSServer(certfile=certs["wrong_crt"], keyfile=certs["wrong_key"]) as srv:
        cfg = FTPConfig(
            host="localhost",
            port=srv.port,
            user="READER",
            password="s3cret",
            transport_mode="ftps",
            ca_file=str(certs["ca_crt"]),
        )
        with pytest.raises(bridge._TlsVerificationError):
            bridge.connect(cfg)
        assert not any(c.startswith(("USER ", "PASS ")) for c in srv.received_commands)

    # 4. Server rejects AUTH TLS -> REFUSED fail-closed, ZERO downgrade to plain FTP
    with _MiniFTPSServer(reject_auth=True) as srv:
        cfg = FTPConfig(
            host="localhost",
            port=srv.port,
            user="READER",
            password="s3cret",
            transport_mode="ftps",
            ca_file=str(certs["ca_crt"]),
        )
        with pytest.raises(bridge._TlsVerificationError):
            bridge.connect(cfg)
        assert not any(c.startswith(("USER ", "PASS ")) for c in srv.received_commands)


def test_logon_free_connectivity_probe_never_sends_credentials(tmp_path: Path) -> None:
    """ADR-0012 D5 point 5: one-time logon-free probe reports exact outcome
    without ever sending USER or PASS."""
    certs = _generate_tls_fixtures(tmp_path)

    with _MiniFTPSServer(certfile=certs["good_crt"], keyfile=certs["good_key"]) as srv:
        res = bridge.probe_transport("localhost", srv.port, ca_file=str(certs["ca_crt"]))
        assert res.outcome == "ftps_verified" and res.recommended_mode == "ftps"
        assert not any(c.startswith(("USER", "PASS")) for c in srv.received_commands)

    with _MiniFTPSServer(certfile=certs["ss_crt"], keyfile=certs["ss_key"]) as srv:
        res = bridge.probe_transport("localhost", srv.port, ca_file=str(certs["ca_crt"]))
        assert res.outcome == "ftps_unverified" and res.recommended_mode is None
        assert not any(c.startswith(("USER", "PASS")) for c in srv.received_commands)

    with _MiniFTPSServer(reject_auth=True) as srv:
        res = bridge.probe_transport("localhost", srv.port, ca_file=str(certs["ca_crt"]))
        assert res.outcome == "plain_only" and res.recommended_mode == "plain"
        assert not any(c.startswith(("USER", "PASS")) for c in srv.received_commands)


# ------------------------------------------- D9: FTP 530 logon-failure breaker


def test_logon_530_breaker_trips_blocks_repeats_and_recovers_on_cooldown_or_reset() -> None:
    now = [100.0]
    breaker = LogonBreaker(threshold=1, cooldown_s=60.0, clock=lambda: now[0])
    connect_attempts = 0
    should_fail_530 = [True]

    def connect(cfg: FTPConfig) -> FakeFTP:
        nonlocal connect_attempts
        connect_attempts += 1
        fake = FakeFTP()
        fake.files["'SYS1.PARMLIB'"] = b"OK\n"
        if should_fail_530[0]:
            raise ftplib.error_perm("530 Login incorrect for READER secret-detail")
        return fake

    cfg = _config()
    call_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "dataset_read", "arguments": {"dataset": "SYS1.PARMLIB"}},
    }

    # 1st call hits 530 -> logon_failed, no upstream text echoed, breaker trips
    r1 = server.handle_request(call_msg, cfg, connect, breaker=breaker)
    assert r1["result"]["isError"] is True
    assert r1["result"]["content"][0]["text"].startswith("logon_failed:")
    assert "secret-detail" not in r1["result"]["content"][0]["text"]
    assert connect_attempts == 1
    assert breaker.is_open() is True

    # 2nd call while open -> fails closed with logon_failed and ZERO connect attempts
    r2 = server.handle_request(call_msg, cfg, connect, breaker=breaker)
    assert r2["result"]["isError"] is True
    assert r2["result"]["content"][0]["text"].startswith("logon_failed:")
    assert connect_attempts == 1

    # Advance clock past cooldown -> next ordinary call succeeds
    should_fail_530[0] = False
    now[0] += 61.0
    assert breaker.is_open() is False
    r3 = server.handle_request(call_msg, cfg, connect, breaker=breaker)
    assert r3["result"]["isError"] is False
    assert "OK" in r3["result"]["content"][0]["text"]
    assert connect_attempts == 2

    # Trip again and recover via operator HTTP reset endpoint
    should_fail_530[0] = True
    server.handle_request(call_msg, cfg, connect, breaker=breaker)
    assert breaker.is_open() is True
    should_fail_530[0] = False
    app = server.create_app(cfg, connect, auth_token="bridge-tok", breaker=breaker)
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok", "logon_breaker_open": True}
    assert client.post("/admin/reset-logon-breaker").status_code == 401
    reset = client.post(
        "/admin/reset-logon-breaker", headers={"Authorization": "Bearer bridge-tok"}
    )
    assert reset.status_code == 200 and reset.json() == {"status": "reset"}
    assert client.get("/healthz").json() == {"status": "ok", "logon_breaker_open": False}
    recovered = client.post(
        "/mcp", json=call_msg, headers={"Authorization": "Bearer bridge-tok"}
    )
    assert recovered.status_code == 200
    assert recovered.json()["result"]["isError"] is False


_READ_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "dataset_read", "arguments": {"dataset": "SYS1.PARMLIB"}},
}


def _burst(n: int, call) -> list:
    """Run `call` from n threads at once and return every result."""
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda _i: call(), range(n)))


class _CountingLogon:
    """Fake connect that counts logon attempts. Each attempt parks on a barrier
    sized for the whole burst, so every caller the breaker admits is in flight
    together: an unbounded breaker lets all n through (the barrier fills), a
    bounded one admits only `threshold` (the barrier times out, they fail)."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.attempts = 0
        self._lock = threading.Lock()
        self.rearm()

    def rearm(self) -> None:
        self.barrier = threading.Barrier(self.n, timeout=1.5)

    def __call__(self, _cfg: FTPConfig) -> FakeFTP:
        with self._lock:
            self.attempts += 1
        with contextlib.suppress(threading.BrokenBarrierError):
            self.barrier.wait()
        raise ftplib.error_perm("530 PASS command failed secret-detail")


@pytest.mark.parametrize("threshold", [1, 2])
def test_logon_breaker_bounds_concurrent_530_logons_to_threshold(threshold: int) -> None:
    """B1 (#1331 review): under concurrency at most `threshold` failing logons
    are in flight; every later caller is refused without touching the server,
    and the same bound holds again when the cooldown expires."""
    n = 6
    now = [100.0]
    breaker = LogonBreaker(threshold=threshold, cooldown_s=60.0, clock=lambda: now[0])
    logon = _CountingLogon(n)
    cfg = _config()

    def call() -> dict:
        return server.handle_request(_READ_CALL, cfg, logon, breaker=breaker)

    replies = _burst(n, call)
    assert logon.attempts == threshold
    for reply in replies:
        text = reply["result"]["content"][0]["text"]
        assert reply["result"]["isError"] is True
        assert text.startswith("logon_failed:")
        assert "secret-detail" not in text
    assert breaker.is_open() is True

    # Half-open after the cooldown: the next burst is bounded the same way.
    now[0] += 61.0
    logon.rearm()
    _burst(n, call)
    assert logon.attempts == 2 * threshold
    assert breaker.is_open() is True


def test_http_concurrent_wrong_password_burst_makes_one_logon() -> None:
    """The review's counterexample through the real HTTP transport
    (asyncio.to_thread offload): 6 concurrent POSTs at the default threshold 1
    make exactly one logon attempt."""
    n = 6
    logon = _CountingLogon(n)
    cfg = _config()
    assert cfg.logon_breaker_threshold == 1
    app = server.create_app(cfg, logon, auth_token="tok")
    headers = {"Authorization": "Bearer tok"}
    with TestClient(app) as client:
        responses = _burst(n, lambda: client.post("/mcp", json=_READ_CALL, headers=headers))
        assert logon.attempts == 1
        for resp in responses:
            assert resp.status_code == 200
            assert resp.json()["result"]["content"][0]["text"].startswith("logon_failed:")
        assert client.get("/healthz").json() == {"status": "ok", "logon_breaker_open": True}


def test_logon_breaker_keeps_successful_reads_concurrent() -> None:
    """Only the logon is bounded: admitted sessions read in parallel. Every read
    waits on a barrier sized for the whole burst, which breaks (failing the
    read) if reads were serialized behind the breaker."""
    n = 4
    read_barrier = threading.Barrier(n, timeout=5.0)

    class ParallelReadFTP(FakeFTP):
        def retrbinary(self, cmd: str, callback) -> str:
            self.commands.append(cmd)
            read_barrier.wait()
            callback(b"PARALLEL-OK\n")
            return "226 done"

    breaker = LogonBreaker(threshold=1, cooldown_s=60.0)
    replies = _burst(
        n,
        lambda: server.handle_request(
            _READ_CALL, _config(), lambda _cfg: ParallelReadFTP(), breaker=breaker
        ),
    )
    for reply in replies:
        assert reply["result"]["isError"] is False
        assert "PARALLEL-OK" in reply["result"]["content"][0]["text"]
    assert breaker.is_open() is False


def test_logon_slot_released_on_non_530_failure_and_bounded_wait_times_out() -> None:
    """A non-530 connect failure frees the logon slot (the next ordinary call
    logs on), and a caller queued behind a hung logon gives up after the FTP
    timeout with the fixed `timeout` code, without a logon of its own."""
    breaker = LogonBreaker(threshold=1, cooldown_s=60.0)
    cfg = _config(timeout_s=0.3)
    outcomes = iter([OSError("connection reset secret-detail"), None])

    def flaky_connect(_cfg: FTPConfig) -> FakeFTP:
        exc = next(outcomes)
        if exc is not None:
            raise exc
        fake = FakeFTP()
        fake.files["'SYS1.PARMLIB'"] = b"AFTER-RESET-OK\n"
        return fake

    first = server.handle_request(_READ_CALL, cfg, flaky_connect, breaker=breaker)
    assert first["result"]["content"][0]["text"].startswith("upstream_error:")
    assert breaker.is_open() is False
    second = server.handle_request(_READ_CALL, cfg, flaky_connect, breaker=breaker)
    assert second["result"]["isError"] is False
    assert "AFTER-RESET-OK" in second["result"]["content"][0]["text"]

    entered = threading.Event()
    release = threading.Event()
    attempts = 0

    def hung_connect(_cfg: FTPConfig) -> FakeFTP:
        nonlocal attempts
        attempts += 1
        entered.set()
        release.wait(timeout=5.0)
        return FakeFTP()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(server.handle_request, _READ_CALL, cfg, hung_connect, breaker=breaker)
        assert entered.wait(timeout=5.0)
        queued = server.handle_request(_READ_CALL, cfg, hung_connect, breaker=breaker)
        assert queued["result"]["content"][0]["text"].startswith("timeout:")
        assert attempts == 1
        release.set()
        holder.result(timeout=5.0)


# ------------------------------------------- D4 Point 2: bearer caller auth


def test_http_bearer_caller_auth_and_healthz() -> None:
    app = server.create_app(_config(), lambda _cfg: FakeFTP(), auth_token="top-secret-token\n")
    client = TestClient(app)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    # Healthz stays reachable for kubelet probes without auth
    hz = client.get("/healthz")
    assert hz.status_code == 200 and hz.json()["status"] == "ok"

    # Missing, wrong, empty, or non-Bearer auth headers are rejected 401
    for bad_headers in (
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Bearer "},
        {"Authorization": "top-secret-token"},
        {"Authorization": "Basic dG9wLXNlY3JldC10b2tlbg=="},
    ):
        resp = client.post("/mcp", json=payload, headers=bad_headers)
        assert resp.status_code == 401
        body = resp.json()
        assert body == {"code": "unauthorized", "message": "a valid bridge token is required"}
        assert "top-secret-token" not in json.dumps(body)

    # Valid Bearer token succeeds (with trailing newline trimmed)
    ok = client.post(
        "/mcp", json=payload, headers={"Authorization": "Bearer top-secret-token"}
    )
    assert ok.status_code == 200
    assert [t["name"] for t in ok.json()["result"]["tools"]] == ["dataset_read"]

    with pytest.raises(RuntimeError, match="MCP_BRIDGE_AUTH_TOKEN"):
        server.create_app(_config(), lambda _cfg: FakeFTP(), auth_token="   ")


# ------------------------------------------- #1302: async offload concurrency


@pytest.mark.anyio
async def test_http_slow_blocking_ftp_call_does_not_block_concurrent_requests() -> None:
    """#1302: blocking ftplib I/O runs via asyncio.to_thread so slow FTP calls
    never stall concurrent /mcp ping or /healthz requests on uvicorn's loop."""
    entered = threading.Event()
    release = threading.Event()

    def slow_connect(_cfg: FTPConfig) -> FakeFTP:
        entered.set()
        release.wait(timeout=5.0)
        fake = FakeFTP()
        fake.files["'SYS1.PARMLIB'"] = b"SLOW-OK\n"
        return fake

    app = server.create_app(_config(), slow_connect, auth_token="tok")
    headers = {"Authorization": "Bearer tok"}
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://bridge") as client,
    ):
        slow_task = asyncio.create_task(
            client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "dataset_read", "arguments": {"dataset": "SYS1.PARMLIB"}},
                },
            )
        )
        # Wait until the blocking connect is actually executing on a worker thread.
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "slow_connect never entered"
        assert not slow_task.done()

        # Concurrent /healthz and /mcp ping MUST complete while slow_connect is still blocked!
        hz = await asyncio.wait_for(client.get("/healthz"), timeout=1.0)
        assert hz.status_code == 200
        ping = await asyncio.wait_for(
            client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
            ),
            timeout=1.0,
        )
        assert ping.status_code == 200 and ping.json()["result"] == {}
        assert not slow_task.done()

        release.set()
        slow_resp = await asyncio.wait_for(slow_task, timeout=2.0)
        assert slow_resp.status_code == 200
        assert "SLOW-OK" in slow_resp.json()["result"]["content"][0]["text"]


# ------------------------------------------------------- MCP framing & CLI


def test_initialize_negotiates_and_framing_errors() -> None:
    config = _config()

    def connect(_cfg: FTPConfig) -> FakeFTP:
        return FakeFTP()

    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 7, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        config,
        connect,
    )
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["serverInfo"]["name"] == "mainframe-ftp-bridge"
    assert reply["result"]["capabilities"] == {"tools": {}}
    assert server.handle_request(
        {"jsonrpc": "2.0", "id": 8, "method": "ping"}, config, connect
    )["result"] == {}
    assert server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "dataset_read", "arguments": {"dataset": 42}}},
        config,
        connect,
    )["error"]["code"] == -32602
    assert server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "nope", "params": {}}, config, connect
    )["error"]["code"] == -32601
    assert server.handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "notifications/initialized", "params": {}},
        config,
        connect,
    ) is None
    assert server.handle_request("garbage", config, connect)["error"]["code"] == -32600


def test_tools_call_logs_tool_name_only(capsys) -> None:
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


def test_stdio_loop_frames_replies_and_preserves_stdout_purity(monkeypatch) -> None:
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
    assert [f.get("id") for f in frames] == [1, None]
    assert [t["name"] for t in frames[0]["result"]["tools"]] == ["dataset_read"]
    assert frames[1]["error"]["code"] == -32700


def test_http_transport_bad_json() -> None:
    client = TestClient(server.create_app(_config(), lambda _cfg: FakeFTP()))
    bad = client.post("/mcp", content=b"{oops", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400


def test_main_fail_closed_gates_and_probe_cli(monkeypatch, capsys) -> None:
    from mainframe_rag.mcp.__main__ import main

    for key in (
        "MCP_FTP_HOST", "MCP_FTP_USER", "MCP_FTP_PASSWORD",
        "MCP_FTP_TRANSPORT_MODE", "MCP_FTP_PLAIN_ACCEPTED",
        "MCP_ENABLED_TOOLS", "MCP_DATASET_ALLOWLIST", "MCP_BRIDGE_AUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    # Missing credentials refused
    assert main(["--transport", "stdio"]) == 2

    # Plain FTP without explicit acceptance refused
    monkeypatch.setenv("MCP_FTP_HOST", "mf.example.com")
    monkeypatch.setenv("MCP_FTP_USER", "READER")
    monkeypatch.setenv("MCP_FTP_PASSWORD", "s3cret")
    monkeypatch.setenv("MCP_FTP_TRANSPORT_MODE", "plain")
    assert main(["--transport", "stdio"]) == 2

    # uss_read in MCP_ENABLED_TOOLS refused
    monkeypatch.setenv("MCP_FTP_PLAIN_ACCEPTED", "true")
    monkeypatch.setenv("MCP_ENABLED_TOOLS", "dataset_read,uss_read")
    assert main(["--transport", "stdio"]) == 2

    # Live HTTP without MCP_BRIDGE_AUTH_TOKEN refused
    monkeypatch.setenv("MCP_ENABLED_TOOLS", "dataset_read")
    assert main(["--transport", "http"]) == 2

    # --probe requires only MCP_FTP_HOST (no credentials)
    monkeypatch.delenv("MCP_FTP_USER", raising=False)
    monkeypatch.delenv("MCP_FTP_PASSWORD", raising=False)
    monkeypatch.setattr(
        "mainframe_rag.mcp.__main__.probe_transport",
        lambda host, port, **kw: bridge.TransportProbeResult(
            host=host, port=port, outcome="ftps_verified", recommended_mode="ftps"
        ),
    )
    assert main(["--probe"]) == 0
    probe_out = json.loads(capsys.readouterr().out)
    assert probe_out == {
        "host": "mf.example.com",
        "port": 21,
        "outcome": "ftps_verified",
        "recommended_mode": "ftps",
    }


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
    from opentelemetry import trace

    from mainframe_rag.mcp import server as server_mod

    provider, exporter = _test_tracer()
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(server_mod, "tracer", test_tracer)
    fake = FakeFTP()
    fake.jes_list_by_job["JOB00123"] = list(JES_LIST_JOB00123)
    fake.files["JOB00123.2"] = b"IEF142I PAYROLL ENDED RC=0\npassword hunter2\nSECRET=abc123\n"

    parent = test_tracer.start_span("agent-live-fetch")
    parent_ctx = trace.set_span_in_context(parent)
    reply = server_mod.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "jes_spool_read",
                    "arguments": {"job_id": "JOB00123", "spool_id": "2"}}},
        _jes_config(),
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
    assert attrs["mcp.bytes_out"] == len(reply["result"]["content"][0]["text"].encode("utf-8"))
    assert attrs["mcp.is_error"] is False
    assert isinstance(attrs["mcp.elapsed_ms"], int)
    haystack = json.dumps(
        [dict(s.attributes or {}) for s in exporter.get_finished_spans()], default=str
    )
    assert "hunter2" not in haystack and "abc123" not in haystack and "s3cret" not in haystack


def test_lifespan_wires_tracer_setup_and_shutdown(monkeypatch) -> None:
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


def test_all_pilot_tools_issue_only_read_ftp_verbs_and_no_nlst() -> None:
    """ADR-0003 / ADR-0012 G3 read-only boundary at the wire: only read verbs
    (RETR/LIST/TYPE A/SITE JES query filters) — never NLST, STOR, DELE, etc."""
    FakeFTP.files = {"'SYS1.PARMLIB'": b"X\n", "JOB00123.2": b"log\n"}
    FakeFTP.listings = {}
    FakeFTP.jes_lines = list(JES_FIXTURE)
    FakeFTP.jes_list_by_job = {"JOB00123": list(JES_LIST_JOB00123)}
    FakeFTP.site_fails = False
    sessions: list[FakeFTP] = []

    def connect(_cfg: FTPConfig) -> FakeFTP:
        fake = FakeFTP()
        sessions.append(fake)
        return fake

    cfg = _jes_config()
    calls = [
        ("dataset_read", {"dataset": "SYS1.PARMLIB"}),
        ("job_status", {"job_id": "JOB00123"}),
        ("jes_spool_read", {"job_id": "JOB00123", "spool_id": "2"}),
    ]
    try:
        for i, (name, args) in enumerate(calls):
            reply = server.handle_request(
                {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                 "params": {"name": name, "arguments": args}},
                cfg,
                connect,
            )
            assert reply["result"]["isError"] is False, name
    finally:
        FakeFTP.files = FakeFTP.listings = FakeFTP.jes_lines = FakeFTP.jes_list_by_job = None
    sent = [cmd for fake in sessions for cmd in fake.commands]
    verbs = {cmd.split(" ", 1)[0] for cmd in sent}
    assert verbs <= {"CONNECT", "TYPE", "SITE", "RETR", "LIST", "QUIT"}, verbs
    assert "NLST" not in verbs
    assert {cmd for cmd in sent if cmd.startswith("SITE")} <= {
        "SITE FILETYPE=JES",
        "SITE JESOWNER=*",
        "SITE JESJOBNAME=*",
        "SITE JESSTATUS=ALL",
    }
