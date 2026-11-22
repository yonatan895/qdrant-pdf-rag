"""Read-only z/OS site probe for the FTP bridge (#1343, ADR-0012 D8).

`python -m mainframe_rag.mcp --site-probe [--dataset DSN] [--job-id JOBnnnnn]
[--receipt FILE]` runs in the bridge pod, so its traffic leaves through the
bridge's `EgressIP` and RACF terminal binding, with the bridge's own
configuration (`MCP_FTP_*`, the allowlists) and code: `bridge.connect` with a
logon breaker, `FTPSession`, the SITE pin rule, the JES `LIST` parsers and
the spool window reader. It turns the D8 dry-run checklist into one command
and a text-free receipt (`mainframe_rag.probe_receipt`).

Read-only by construction: one session and **one logon attempt** per run
(a fresh breaker with threshold 1; a failed logon ends the run, nothing
reconnects); after the bridge's own session setup the commands are SITE, STAT,
TYPE, PASV, RETR, LIST, ABOR and QUIT only (no STOR, DELE or job action); `--dataset` must be an exact entry of
`MCP_DATASET_ALLOWLIST`; JES checks run only for a job the operator names with
`--job-id`, and read only that job's `LIST` and its `JESMSGLG`, after the
bridge's jobname/owner prefix check. Without a named target those checks are
`inconclusive` (`no_dataset_named`, `no_job_named`).

The matchers for server wording (`SBDATACONN_RE` over the STAT reply and the
`JESMSGLG` record shapes) are assumptions the dry run exists to test: an
unrecognised shape is `inconclusive`, never `fail`.
"""

from __future__ import annotations

import codecs
import contextlib
import ftplib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mainframe_rag.mcp import bridge
from mainframe_rag.mcp.bridge import FTPConfig, LogonBreaker
from mainframe_rag.probe_receipt import Receipt

PROBE = "zos"

CHECKS: tuple[str, ...] = (
    "zos.control_connect",
    "zos.tls",
    "zos.logon",
    "zos.site_notrailingblanks",
    "zos.site_sbsendeol",
    "zos.stat_sbdataconn",
    "zos.passive_data",
    "zos.dataset_read_ascii",
    "zos.jes_site_notrailingblanks",
    "zos.jes_site_sbsendeol",
    "zos.jes_interface_level2",
    "zos.jes_spool_mapping",
)
_JES_CHECKS: tuple[str, ...] = CHECKS[-4:]

# SITE operand -> check id suffix (the operands are bridge.TEXT_FORMAT_SITES).
_PIN_SUFFIX = {"NOTRAILINGBLANKS": "notrailingblanks", "SBSENDEOL=CRLF": "sbsendeol"}

# STAT: the SBDATACONN line, assumed to carry (file system page, network page);
# the network page is the code page of the TYPE A bytes the bridge decodes.
SBDATACONN_RE = re.compile(
    r"(?i)SBDATACONN\b[^\n(]*\(\s*([A-Za-z0-9_.-]{1,32})\s*,\s*([A-Za-z0-9_.-]{1,32})\s*\)"
)
# JESMSGLG starts with the JES job log banner (spaced or not); another DD's
# first records (JCL statements of JESJCL, allocation messages of JESYSMSG)
# under the listed JESMSGLG id is the APAR PI98636 shift.
JOB_LOG_HEADER_RE = re.compile(r"(?i)J\s?E\s?S\s?[23]\s+J\s?O\s?B\s+L\s?O\s?G")
JCL_RECORD_RE = re.compile(r"^\s*\d*\s*//")
SYSMSG_RECORD_RE = re.compile(r"^\s*(?:IEF|IGD)[A-Z]?\d{3}I\b")
HEADER_SCAN_LINES = 5

# A dataset read counts as translated text when at least this share of its
# bytes are printable ASCII, TAB, CR or LF (untranslated EBCDIC is mostly
# high bytes) and it carries no other control bytes and no U+FFFD.
ASCII_TEXT_MIN_PERCENT = 90
# The LIST of one job is a few dozen lines; past this the listing is abandoned.
LIST_MAX_LINES = 2000
_REPLACEMENT = "\N{REPLACEMENT CHARACTER}"


class _SessionLost(Exception):
    """The session can no longer be trusted (socket error, aborted transfer)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ListCapExceeded(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProbeTargets:
    """Operator-named targets, validated before any connection."""

    dataset: str | None = None
    member: str | None = None
    job_id: str | None = None


def validate_targets(
    config: FTPConfig, *, dataset: str | None = None, job_id: str | None = None
) -> ProbeTargets:
    """Check the named targets against the bridge policy; ValueError (fixed
    message, never the target) when one would be refused."""
    dsn: str | None = None
    mem: str | None = None
    if dataset:
        try:
            dsn, mem = bridge._check_dataset_policy(config, dataset)
        except (bridge._InvalidParamsError, bridge._DeniedError):
            raise ValueError("--dataset must be an exact entry of MCP_DATASET_ALLOWLIST") from None
    jid: str | None = None
    if job_id:
        try:
            jid = bridge._validate_job_id(job_id)
        except bridge._InvalidParamsError:
            raise ValueError("--job-id must be an exact JOBnnnnn id") from None
        if not config.jes_jobname_prefixes and not config.jes_owner_prefixes:
            raise ValueError("--job-id needs MCP_JES_JOBNAME_PREFIXES or MCP_JES_OWNER_PREFIXES")
    return ProbeTargets(dataset=dsn, member=mem, job_id=jid)


def _reply_code(exc: BaseException) -> str:
    text = str(exc)
    return text[:3] if text[:3].isdigit() else "???"


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, Exception):
        return bridge._classify_ftp_error(exc)[0]
    return bridge.UPSTREAM_ERROR


def codec_name(name: str) -> str | None:
    """Python's canonical codec name for a z/OS code page name, if known."""
    lower = name.lower()
    for candidate in (lower, lower.replace("-", ""), lower.replace("ibm-", "cp")):
        try:
            return codecs.lookup(candidate).name
        except LookupError:
            continue
    return None


def text_facts(raw: bytes, encoding: str, *, final: bool = True) -> dict[str, Any]:
    """Byte-level facts of a TYPE A read (counts only, never the text)."""
    total = len(raw)
    control = sum(1 for b in raw if (b < 0x20 and b not in (9, 10, 13)) or b == 0x7F)
    high = sum(1 for b in raw if b >= 0x80)
    printable = total - control - high
    decoded = codecs.getincrementaldecoder(encoding)(errors="replace").decode(raw, final=final)
    return {
        "bytes": total,
        "crlf_record_ends": b"\r\n" in raw,
        "replacement_chars": decoded.count(_REPLACEMENT),
        "control_bytes": control,
        "high_bytes": high,
        "printable_percent": (printable * 100) // total if total else None,
    }


def spool_head_shape(head: bytes, encoding: str) -> str:
    """`job_log`, `jcl`, `sysmsg` or `unknown` from the first records."""
    text = codecs.getincrementaldecoder(encoding)(errors="replace").decode(head, final=False)
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    first = lines[:HEADER_SCAN_LINES]
    if any(JOB_LOG_HEADER_RE.search(line) for line in first):
        return "job_log"
    if any(JCL_RECORD_RE.match(line) for line in first):
        return "jcl"
    if any(SYSMSG_RECORD_RE.match(line) for line in first):
        return "sysmsg"
    return "unknown"


@dataclass
class _ZosRun:
    config: FTPConfig
    targets: ProbeTargets
    clock: Callable[[], float]
    results: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    default_reason: str = "not_reached"

    def put(self, check: str, status: str, **fields: Any) -> None:
        """Record a check once; the first outcome of a check stands."""
        if check not in self.results:
            self.results[check] = (status, fields)

    def receipt(self) -> Receipt:
        receipt = Receipt(PROBE)
        for check in CHECKS:
            status, fields = self.results.get(
                check, ("inconclusive", {"reason": self.default_reason})
            )
            receipt.add(check, status, **fields)
        return receipt

    # --- session setup ----------------------------------------------------------

    def record_setup(
        self, progress: list[str], exc: BaseException | None, breaker: LogonBreaker, ftp: Any
    ) -> None:
        ftps = self.config.transport_mode == "ftps"
        if isinstance(exc, bridge._LogonBreakerOpen):
            self.default_reason = "breaker_open"
            self.put("zos.control_connect", "inconclusive", reason="breaker_open")
            return
        if "connected" not in progress:
            self.put("zos.control_connect", "fail", reason=_error_code(exc) if exc else None)
            return
        self.put("zos.control_connect", "pass")
        tls_facts: dict[str, Any] = {"ca_file_configured": bool(self.config.ca_file)}
        if not ftps:
            self.put("zos.tls", "inconclusive", reason="plain_mode", plain_accepted=True)
        elif "tls" not in progress:
            self.put("zos.tls", "fail", reason="tls_error", **tls_facts)
        else:
            tls_facts["tls_version"] = _tls_version(ftp)
            if "protected" in progress:
                self.put("zos.tls", "pass", prot_p=True, **tls_facts)
            elif "logon" in progress:
                self.put("zos.tls", "fail", reason="prot_p_refused", prot_p=False, **tls_facts)
            else:
                self.put("zos.tls", "pass", prot_p=None, **tls_facts)
        logon_tried = "tls" in progress or not ftps
        if "logon" in progress:
            self.put("zos.logon", "pass", logon_attempts=1)
        elif logon_tried and exc is not None:
            failed = isinstance(exc, bridge._LogonFailed)
            self.put(
                "zos.logon",
                "fail",
                reason="logon_failed" if failed else _error_code(exc),
                logon_attempts=1,
                breaker_open=breaker.is_open(),
            )
        if exc is not None and "logon" in progress:
            self.default_reason = "session_lost"

    # --- after logon -------------------------------------------------------------

    def after_logon(self, ftp: Any) -> None:
        session = bridge.FTPSession(ftp, self.config, clock=self.clock)
        try:
            self.site_pins(ftp, "site")
            self.stat(ftp)
            self.dataset(session)
            self.jes(session)
        except _SessionLost as lost:
            self.default_reason = lost.reason
            return
        self.put("zos.passive_data", "inconclusive", reason="no_transfer")

    def site_pins(self, ftp: Any, prefix: str) -> None:
        for operand in bridge.TEXT_FORMAT_SITES:
            check = f"zos.{prefix}_{_PIN_SUFFIX[operand]}"
            try:
                code, confirmed = bridge.site_reply(ftp, operand)
            except ftplib.error_temp as exc:
                self.put(check, "inconclusive", reason="transient", reply_code=_reply_code(exc))
                continue
            except (OSError, EOFError):
                raise _SessionLost("session_lost") from None
            except ftplib.Error as exc:
                self.put(
                    check, "inconclusive", reason="unexpected_reply", reply_code=_reply_code(exc)
                )
                continue
            self.put(check, "pass" if confirmed else "fail", reply_code=code, confirmed=confirmed)

    def stat(self, ftp: Any) -> None:
        check = "zos.stat_sbdataconn"
        configured = codecs.lookup(self.config.encoding).name
        try:
            reply = ftp.sendcmd("STAT")
        except (OSError, EOFError):
            raise _SessionLost("session_lost") from None
        except ftplib.Error as exc:
            self.put(check, "inconclusive", reason="stat_refused", reply_code=_reply_code(exc))
            return
        code = reply[:3] if reply[:3].isdigit() else "???"
        facts: dict[str, Any] = {"reply_code": code, "configured_codec": configured}
        match = SBDATACONN_RE.search(reply)
        if match is None:
            reason = "unparsed" if "sbdataconn" in reply.lower() else "not_reported"
            self.put(check, "inconclusive", reason=reason, **facts)
            return
        server = codec_name(match.group(2))
        if server is None:
            self.put(check, "inconclusive", reason="unknown_code_page", **facts)
            return
        self.put(
            check,
            "pass" if server == configured else "fail",
            server_codec=server,
            matches_configured=server == configured,
            **facts,
        )

    def _transfer_failed(self, exc: BaseException) -> None:
        """A data transfer raised: classify it for the passive-data check."""
        if isinstance(exc, ftplib.error_temp) and _reply_code(exc) == "425":
            self.put(
                "zos.passive_data", "fail", reason="data_connection_refused", reply_code="425"
            )
        elif isinstance(exc, (OSError, EOFError)):
            self.put("zos.passive_data", "fail", reason="data_connection_failed")
        else:
            self.put("zos.passive_data", "inconclusive", reason="no_data_transferred")

    def dataset(self, session: bridge.FTPSession) -> None:
        check = "zos.dataset_read_ascii"
        dsn, mem = self.targets.dataset, self.targets.member
        if dsn is None:
            self.put(check, "inconclusive", reason="no_dataset_named")
            return
        command = f"RETR '{dsn}'({mem})" if mem else f"RETR '{dsn}'"
        try:
            bridge._pin_text_format(session.ftp)
            raw, truncated = bridge._capped_retr(session.ftp, command, self.config.max_bytes)
        except (OSError, EOFError) as exc:
            self._transfer_failed(exc)
            self.put(check, "inconclusive", reason="session_lost")
            raise _SessionLost("session_lost") from None
        except ftplib.Error as exc:
            self._transfer_failed(exc)
            code = _reply_code(exc)
            reason = "not_found" if code == "550" else "refused"
            self.put(check, "fail", reason=reason, reply_code=code)
            return
        self.put("zos.passive_data", "pass", via="dataset_read")
        facts = text_facts(raw, self.config.encoding, final=not truncated)
        facts["truncated"] = truncated
        if not raw:
            self.put(check, "inconclusive", reason="empty", **facts)
        elif facts["replacement_chars"]:
            self.put(check, "fail", reason="replacement_chars", **facts)
        elif facts["control_bytes"]:
            self.put(check, "fail", reason="control_bytes", **facts)
        elif facts["printable_percent"] < ASCII_TEXT_MIN_PERCENT:
            self.put(check, "fail", reason="not_ascii_text", **facts)
        else:
            self.put(check, "pass", **facts)
        if truncated:
            # The transfer was aborted: later replies may be out of step.
            raise _SessionLost("after_abort")

    def jes(self, session: bridge.FTPSession) -> None:
        jid = self.targets.job_id
        if jid is None:
            for check in _JES_CHECKS:
                self.put(check, "inconclusive", reason="no_job_named")
            return
        try:
            bridge._jes_mode(session)
        except bridge._JesUnavailable as exc:
            code = _reply_code(exc)
            self.put("zos.jes_interface_level2", "fail", reason="jes_refused", reply_code=code)
            for check in _JES_CHECKS:
                self.put(check, "inconclusive", reason="jes_refused")
            return
        except (OSError, EOFError):
            raise _SessionLost("session_lost") from None
        except ftplib.Error as exc:
            for check in _JES_CHECKS:
                self.put(check, "inconclusive", reason="transient", reply_code=_reply_code(exc))
            return
        self.site_pins(session.ftp, "jes_site")
        lines = self._list_job(session, jid)
        if lines is None:
            return
        level2 = "zos.jes_interface_level2"
        if any(bridge._is_level2_header(line) for line in lines):
            self.put(level2, "pass", listing_lines=len(lines))
        elif lines:
            self.put(level2, "fail", reason="no_level2_header", listing_lines=len(lines))
            self.put("zos.jes_spool_mapping", "inconclusive", reason="no_level2_header")
            return
        else:
            self.put(level2, "inconclusive", reason="empty_listing")
            self.put("zos.jes_spool_mapping", "inconclusive", reason="empty_listing")
            return
        self.spool_mapping(session, jid, lines)

    def _list_job(self, session: bridge.FTPSession, jid: str) -> list[str] | None:
        lines: list[str] = []

        def collect(line: str) -> None:
            if len(lines) >= LIST_MAX_LINES:
                raise _ListCapExceeded
            lines.append(line)

        try:
            session.ftp.retrlines(f"LIST {jid}", collect)
        except _ListCapExceeded:
            with contextlib.suppress(Exception):
                session.ftp.abort()
            self.put("zos.passive_data", "pass", via="jes_list")
            self.put("zos.jes_interface_level2", "inconclusive", reason="listing_too_long")
            raise _SessionLost("after_abort") from None
        except (OSError, EOFError) as exc:
            self._transfer_failed(exc)
            raise _SessionLost("session_lost") from None
        except ftplib.Error as exc:
            self._transfer_failed(exc)
            code = _reply_code(exc)
            reason = "job_not_found" if code == "550" else "list_refused"
            self.put("zos.jes_interface_level2", "inconclusive", reason=reason, reply_code=code)
            self.put("zos.jes_spool_mapping", "inconclusive", reason=reason)
            return None
        self.put("zos.passive_data", "pass", via="jes_list")
        return lines

    def spool_mapping(self, session: bridge.FTPSession, jid: str, lines: list[str]) -> None:
        check = "zos.jes_spool_mapping"
        job: dict[str, Any] | None = None
        spool: dict[int, tuple[str, int | None]] = {}
        for line in lines:
            parsed_job = bridge._parse_job_line(line)
            if parsed_job is not None and parsed_job["job_id"] == jid:
                job = parsed_job
                continue
            parsed = bridge._parse_spool_table_line(line)
            if parsed is not None:
                spool[parsed[0]] = (parsed[1], parsed[2])
        if job is None:
            self.put(check, "inconclusive", reason="job_not_listed")
            return
        if not bridge._matches_jes_prefixes(self.config, job["name"], job["owner"]):
            self.put(check, "inconclusive", reason="job_not_allowlisted")
            return
        ids = sorted(sid for sid, (ddname, _) in spool.items() if ddname == "JESMSGLG")
        if not ids:
            self.put(check, "inconclusive", reason="no_jesmsglg", listed_spool_files=len(spool))
            return
        sid = ids[0]
        listed_bytes = spool[sid][1]
        started = self.clock()
        try:
            capture = session.read_spool(f"RETR {jid}.{sid}", size_hint=listed_bytes)
        except (OSError, EOFError):
            self.put(check, "inconclusive", reason="session_lost")
            raise _SessionLost("session_lost") from None
        except ftplib.Error as exc:
            self.put(check, "inconclusive", reason="retr_refused", reply_code=_reply_code(exc))
            return
        shape = spool_head_shape(bytes(capture.head), self.config.encoding)
        facts: dict[str, Any] = {
            "listed_spool_files": len(spool),
            "jesmsglg_spool_id": sid,
            "listed_byte_count": listed_bytes,
            "received_bytes": capture.total,
            "stream_complete": capture.complete,
            "elapsed_ms": int((self.clock() - started) * 1000),
            "header_shape": shape,
            "byte_count_equal": (
                listed_bytes == capture.total
                if capture.complete and listed_bytes is not None
                else None
            ),
        }
        if shape == "job_log":
            self.put(check, "pass", **facts)
        elif shape in ("jcl", "sysmsg"):
            self.put(check, "fail", reason="other_dd_shape", **facts)
        else:
            self.put(check, "inconclusive", reason="unrecognized_shape", **facts)


def _tls_version(ftp: Any) -> str | None:
    sock = getattr(ftp, "sock", None)
    version = getattr(sock, "version", None)
    if not callable(version):
        return None
    with contextlib.suppress(Exception):
        value = version()
        return value if isinstance(value, str) else None
    return None


def run_zos_probe(
    config: FTPConfig,
    targets: ProbeTargets,
    *,
    factory: type[ftplib.FTP] = ftplib.FTP,
    breaker: LogonBreaker | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Receipt:
    """One session, one logon attempt, the D8 checks in a fixed order (control
    commands first, the dataset read, then JES with the spool read last)."""
    run = _ZosRun(config, targets, clock)
    active = breaker or LogonBreaker(threshold=1, cooldown_s=config.logon_breaker_cooldown_s)
    progress: list[str] = []
    try:
        ftp = bridge.connect(config, factory, breaker=active, progress=progress)
    except Exception as exc:  # noqa: BLE001 — recorded as a reason code only
        run.record_setup(progress, exc, active, None)
        return run.receipt()
    run.record_setup(progress, None, active, ftp)
    try:
        run.after_logon(ftp)
    finally:
        with contextlib.suppress(Exception):
            ftp.quit()
        with contextlib.suppress(Exception):
            ftp.close()
    return run.receipt()
