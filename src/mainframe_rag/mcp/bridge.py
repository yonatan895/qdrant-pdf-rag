"""FTP transport for the read-only Zowe MCP bridge (ADR-0003).

All four allowlisted tools speak the z/OS FTP server; nothing here opens
any other connection. Text reads use TYPE A so the server converts
EBCDIC on the wire. Credentials travel only inside ftplib and never enter
error strings, logs, or tool results.

JES addressing follows the FTP JES interface (`SITE FILETYPE=JES` plus
JESOWNER/JESJOBNAME/JESSTATUS filters; spool files as `JOBID.n`):
typical ids are 1=JCL, 2=system messages, 3+=SYSOUT, but JES2/JES3
numbering varies — VERIFY the mapping on first contact with a site
(see the operator checklist in the Phase-1 PR body) and never hardcode
site-specific ids here.
"""

from __future__ import annotations

import ftplib
import io
import socket
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class FTPConfig:
    """Connection + safety bounds. Password arrives via env only (there is
    deliberately no CLI flag for it); use_tls upgrades to FTPS where the
    site's AT-TLS policy supports it (plain FTP is the accepted default)."""

    host: str
    user: str
    password: str
    port: int = 21
    timeout_s: float = 15.0
    max_bytes: int = 262144
    use_tls: bool = False


TRUNCATION_SUFFIX = "\n[... truncated: output exceeded the byte cap ...]"

# Stable tool-error codes (client-visible; never exception text).
NOT_FOUND = "not_found"
JES_UNAVAILABLE = "jes_unavailable"
TIMEOUT = "timeout"
UPSTREAM_ERROR = "upstream_error"


class _CapExceeded(Exception):
    pass


class _JesUnavailable(Exception):
    """SITE JES setup refused: the JES FTP interface (not a job) is missing."""


def connect(config: FTPConfig, factory: type[ftplib.FTP] = ftplib.FTP):
    """One FTP session per tool call (read tools are infrequent; shared
    sessions would need reconnect logic for dead control connections).
    Tests inject a FakeFTP subclass here — no network, no credentials."""
    cls = ftplib.FTP_TLS if config.use_tls else factory
    ftp = cls()
    ftp.connect(config.host, config.port, timeout=config.timeout_s)
    ftp.login(config.user, config.password)
    if config.use_tls and isinstance(ftp, ftplib.FTP_TLS):
        ftp.prot_p()
    return ftp


def _capped_retr(ftp: ftplib.FTP, command: str, max_bytes: int) -> tuple[bytes, bool]:
    """RETR aborting past max_bytes. Returns (payload, truncated)."""
    buf = io.BytesIO()

    def _sink(chunk: bytes) -> None:
        remaining = max_bytes - buf.tell()
        if len(chunk) > remaining:
            buf.write(chunk[:remaining])
            raise _CapExceeded
        buf.write(chunk)

    try:
        ftp.retrbinary(command, _sink)
    except _CapExceeded:
        try:
            ftp.abort()
        except Exception:  # noqa: BLE001, S110 — best-effort abort only
            pass
        return buf.getvalue(), True
    return buf.getvalue(), False


def _tool_result(text: str, truncated: bool = False) -> dict:
    if truncated:
        text += TRUNCATION_SUFFIX
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(code: str, message: str) -> dict:
    return {
        "content": [{"type": "text", "text": f"{code}: {message}"}],
        "isError": True,
    }


def _classify_ftp_error(exc: Exception) -> tuple[str, str]:
    """Stable codes from ftplib failures. ftplib never echoes the password;
    our messages interpolate only the operation and the server's reply."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return TIMEOUT, "FTP operation timed out"
    if isinstance(exc, _JesUnavailable):
        return JES_UNAVAILABLE, "JES FTP interface refused the command"
    text = str(exc)
    if isinstance(exc, ftplib.error_perm) and text.startswith("550"):
        return NOT_FOUND, "no such dataset, member, path, job, or spool file"
    return UPSTREAM_ERROR, "FTP request failed"


@dataclass
class FTPSession:
    """One logged-in session shared by a single tool call. Test doubles
    subclass or duck-type this (FakeFTP below shows the surface)."""

    ftp: ftplib.FTP = field(repr=False)
    config: FTPConfig = field(repr=False)

    def read_text(self, command: str) -> tuple[str, bool]:
        raw, truncated = _capped_retr(self.ftp, command, self.config.max_bytes)
        return raw.decode("utf-8", errors="replace"), truncated


def dataset_read(session: FTPSession, dataset: str, member: str | None = None) -> dict:
    """Read a sequential dataset or PDS member (TYPE A converts EBCDIC).
    A PDS name without a member returns the member list instead."""
    name = dataset.strip().upper()
    if not name:
        return _tool_error("invalid_params", "dataset must not be empty")
    quoted = f"'{name}'"
    try:
        session.ftp.sendcmd("TYPE A")
        if member:
            text, truncated = session.read_text(f"RETR {quoted}({member.strip().upper()})")
            return _tool_result(text, truncated)
        try:
            text, truncated = session.read_text(f"RETR {quoted}")
            return _tool_result(text, truncated)
        except ftplib.error_perm as exc:
            if not str(exc).startswith("550"):
                raise
            members = session.ftp.nlst(quoted)
            listed = "\n".join(members)
            return _tool_result(f"Members of {name}:\n{listed}")
    except Exception as exc:  # noqa: BLE001 — mapped to stable codes below
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)


def uss_read(session: FTPSession, path: str) -> dict:
    """Read a USS file (TYPE A converts EBCDIC text)."""
    clean = path.strip()
    if not clean or ".." in clean.split("/"):
        return _tool_error("invalid_params", "path must be absolute without .. segments")
    if not clean.startswith("/"):
        return _tool_error("invalid_params", "path must be absolute")
    try:
        session.ftp.sendcmd("TYPE A")
        text, truncated = session.read_text(f"RETR {clean}")
        return _tool_result(text, truncated)
    except Exception as exc:  # noqa: BLE001 — mapped to stable codes below
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)


def _jes_mode(session: FTPSession, owner: str = "*", job_name: str = "*") -> None:
    try:
        session.ftp.sendcmd("SITE FILETYPE=JES")
        session.ftp.sendcmd(f"SITE JESOWNER={owner or '*'}")
        session.ftp.sendcmd(f"SITE JESJOBNAME={job_name or '*'}")
        session.ftp.sendcmd("SITE JESSTATUS=ALL")
    except ftplib.error_perm as exc:
        # A 550 here means the JES interface refused — not a missing job
        # (RETR 550s still map to NOT_FOUND in _classify_ftp_error).
        raise _JesUnavailable(str(exc)) from exc


def _parse_job_line(line: str) -> dict | None:
    """Parse one JES LIST line defensively: unknown shapes pass through
    with raw text rather than dropping a job the operator asked about."""
    parts = line.split()
    if len(parts) < 4:
        return {"raw": line} if line.strip() else None
    job = {
        "name": parts[0],
        "job_id": parts[1],
        "owner": parts[2],
        "status": parts[3],
        "return_code": None,
        "raw": line,
    }
    for token in parts[4:]:
        upper = token.upper()
        if upper.startswith(("RC=", "CC")):
            job["return_code"] = token.split("=", 1)[-1]
    return job


def job_status(
    session: FTPSession,
    job_name: str | None = None,
    owner: str | None = None,
    job_id: str | None = None,
) -> dict:
    """List jobs (optionally filtered) with status and return code."""
    try:
        _jes_mode(session, owner or "*", job_name or "*")
        lines: list[str] = []
        session.ftp.retrlines("LIST", lines.append)
        jobs = []
        for line in lines:
            parsed = _parse_job_line(line)
            if parsed is None:
                continue
            if job_id and parsed.get("job_id", "").upper() != job_id.strip().upper():
                continue
            jobs.append(parsed)
        rendered_lines = []
        for j in jobs:
            if j.get("name"):
                line = f"{j['name']} {j.get('job_id', '?')} {j.get('status', '?')}"
                if j.get("return_code"):
                    line += f" RC={j['return_code']}"
            else:
                line = j["raw"]
            rendered_lines.append(line)
        return _tool_result("\n".join(rendered_lines) or "No matching jobs.")
    except Exception as exc:  # noqa: BLE001 — mapped to stable codes below
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)


def jes_spool_read(session: FTPSession, job_id: str, spool_id: str) -> dict:
    """Read one spool file of a job (`JOBID.n`; typical ids 1=JCL,
    2=system messages, 3+=SYSOUT — verify per site, see module docstring)."""
    jid = job_id.strip().upper()
    sid = spool_id.strip()
    if not jid or not sid or not sid.isdigit():
        return _tool_error("invalid_params", "job_id must not be empty and spool_id must be numeric")
    try:
        _jes_mode(session)
        text, truncated = session.read_text(f"RETR {jid}.{sid}")
        return _tool_result(text, truncated)
    except Exception as exc:  # noqa: BLE001 — mapped to stable codes below
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)
