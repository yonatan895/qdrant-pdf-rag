"""FTP transport for the read-only Zowe MCP bridge (ADR-0003, ADR-0012).

All approved tools speak the z/OS FTP server; nothing here opens any other
connection. Text reads stay in TYPE A end to end (never ftplib's retrbinary,
which switches to TYPE I), so the server translates EBCDIC on the wire; the
received bytes are decoded with `FTPConfig.encoding` (`MCP_FTP_ENCODING`), the
code page of the server's ASCII output (ISO8859-1, i.e. `latin-1`, which maps every
byte to one character and never fails, unless the site's FTP.DATA SBDATACONN says
otherwise, e.g. `MCP_FTP_ENCODING=utf-8`), not the dataset's EBCDIC page. Credentials travel only
inside ftplib and never enter error strings, logs, or tool results.

ADR-0012 pilot controls enforced here:
- Pinned transport mode (D5): verified FTPS (CA bundle + hostname check,
  fail-closed, zero downgrade fallback) or explicitly owner-accepted plain FTP.
- Explicit passive mode (`ftp.set_pasv(True)`, D4/G5), mandatory behind
  OpenShift EgressIP SNAT.
- Exact-name dataset allowlist (D2/G1): every qualifier and member validated
  before any FTP command; wildcards, relative GDG references, quotes, case/
  member tricks, and PDS-without-member NLST fallback are refused.
- Approved tool subset (`MCP_ENABLED_TOOLS`, D1/D7): default `dataset_read`
  only; opt-in `job_status` and DD-scoped `jes_spool_read` (`JESMSGLG` and
  `JESYSMSG` only, verified via `LIST` against bridge-side jobname/owner
  prefix allowlists before any `RETR`, with explicit `TYPE A`); `uss_read`
  stays unapproved and is refused.
- Content screening & untrusted delimiting (D6): control/bidi characters
  stripped, password-shaped strings redacted (the forms `screen_zos_text`
  lists), output wrapped in untrusted observation delimiters with breakout
  neutralization. Text reads pin `SITE NOTRAILINGBLANKS` and
  `SITE SBSENDEOL=CRLF` (unconfirmed replies proceed, marked by code).
- Spool window (D6): `jes_spool_read` returns the first quarter and last
  three quarters of a job log that exceeds the cap (`_spool_window`), bounded
  by `spool_stream_max_bytes` / `spool_stream_max_s`.
- Logon-failure breaker (D9): FTP 530 maps to `logon_failed`; at most
  `threshold` failing logons can be in flight whatever the concurrency, and
  once tripped no logon reaches the server until cooldown elapses or an
  operator resets the breaker.
- Logon-free connectivity probe (D5 point 5): tests `AUTH TLS` and certificate
  verification without sending credentials.
"""

from __future__ import annotations

import codecs
import contextlib
import ftplib
import re
import socket
import ssl
import sys
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from opentelemetry import trace

# Closed four-tool vocabulary (ADR-0003); adding a fifth tool requires a new ADR.
CLOSED_TOOLS: tuple[str, ...] = ("dataset_read", "uss_read", "job_status", "jes_spool_read")

# ADR-0012 D1/D7: approved pilot subset; `uss_read` remains unapproved.
PILOT_APPROVED_TOOLS: tuple[str, ...] = ("dataset_read", "job_status", "jes_spool_read")
DEFAULT_ENABLED_TOOLS: tuple[str, ...] = ("dataset_read",)

# ADR-0012 D2/Option 4A: only JESMSGLG and JESYSMSG may be read from spool.
ALLOWED_JES_DDS: frozenset[str] = frozenset({"JESMSGLG", "JESYSMSG"})

TRUNCATION_SUFFIX = "\n[... truncated: output exceeded the byte cap ...]"
OBS_BEGIN_PREFIX = "--- BEGIN UNTRUSTED Z/OS OBSERVATION"
OBS_END_MARKER = "--- END UNTRUSTED Z/OS OBSERVATION ---"

# Text-format SITE pins sent before every text read (one operand per SITE):
# strip the trailing blanks of fixed-format records, and pin CRLF record ends
# (the spool window cuts on line ends). Unverified on the site: ADR-0012 D8.
TEXT_FORMAT_SITES: tuple[str, ...] = ("NOTRAILINGBLANKS", "SBSENDEOL=CRLF")

# jes_spool_read start-and-end window (maintainer decision 2026-11-21): the
# framed result fits max_bytes, so the agent's head-only live cap of the same
# size never drops the tail or the END delimiter. FRAME_RESERVE_BYTES covers
# the delimiters, the window header and the gap marker; of the rest, the first
# 1/SPOOL_HEAD_DIVISOR goes to the head (job identity, first steps) and the
# remainder to the tail (completion codes, abends).
FRAME_RESERVE_BYTES = 1024
SPOOL_HEAD_DIVISOR = 4
# Extra tail bytes kept before the line trim, so the tail's first safe start
# (`_tail_start`) usually lies before the last T bytes and a key there is in
# view of its value. Safety does not rest on this size (separators span any
# run of whitespace): no kept line starts where a match could span.
TAIL_LOOKBEHIND_BYTES = 4096
SPOOL_GAP_MARKER = (
    "[... z/OS bridge omitted about {n} bytes between the first and last parts"
    " of this spool file ...]"
)
SPOOL_STOP_MARKER = "[... z/OS bridge stopped reading after {n} bytes; the end was not read ...]"
# The agent's default per-call bridge timeout (Settings.zowe_mcp_timeout_s);
# the bridge cannot read the agent's setting, so the spool stream budget is
# validated against this default (a test pins the two equal).
AGENT_CALL_TIMEOUT_DEFAULT_S = 15.0
_RECV_CHUNK = 8192

# Stable tool-error codes (client-visible; never exception or upstream text).
NOT_FOUND = "not_found"
DENIED = "denied"
LOGON_FAILED = "logon_failed"
TLS_ERROR = "tls_error"
JES_UNAVAILABLE = "jes_unavailable"
TIMEOUT = "timeout"
UPSTREAM_ERROR = "upstream_error"
INVALID_PARAMS = "invalid_params"
NOT_CONFIGURED = "not_configured"

_QUALIFIER_RE = re.compile(r"^[A-Z#$@][A-Z0-9#$@-]{0,7}$")
_MEMBER_RE = re.compile(r"^[A-Z#$@][A-Z0-9#$@]{0,7}$")
_JOB_ID_RE = re.compile(r"^JOB\d{5,7}$")
_JES_IDENT_RE = re.compile(r"^[A-Z#$@][A-Z0-9#$@]{0,7}$")
_SPOOL_ID_RE = re.compile(r"^\d{1,5}$")
_COMPLETION_CODE_RE = re.compile(r"^[A-Z0-9]{1,8}$")
_LEVEL2_HEADER = ("JOBNAME", "JOBID", "OWNER", "STATUS", "CLASS")
_FORBIDDEN_NAME_CHARS = frozenset("'\"*?%/\\")

# D6 redaction patterns (applied after control/bidi stripping).
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
# Open-ended keys (applied after complete blocks are gone): a BEGIN whose END
# was cut off by the byte cap redacts to the end of the text; an END without
# its BEGIN (text that starts inside a key) redacts from the start of the text.
_PEM_OPEN_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*\Z")
_PEM_OPEN_END_RE = re.compile(r"\A[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")
# One BEGIN or END line of those patterns, for tracking a key across a read.
_PEM_MARKER_RE = re.compile(r"-----(BEGIN|END) [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")
# Password and passphrase keywords as RACF, TSO and JCL spell them (any case):
# the operands of ADDUSER/ALTUSER/PASSWORD/LOGON, their NEW* forms, and the
# PassTicket/session key operands, whose values are secrets too.
_PASSWORD_WORDS = (
    r"new(?:password|passwd|passphrase|phrase|pass|pw)|"
    r"password|passwd|passphrase|phrase|pass|pwd|pw|"
    r"keymasked|keyencrypted|sesskey"
)
# Operand form: PASSWORD(x), PHRASE('a b c'), PW (x), quoted with doubled
# quotes or bare; the closing parenthesis or quote is optional because the byte
# cap can cut the operand open before screening.
_OPERAND_SECRET_RE = re.compile(
    rf"(?i)(?<![A-Z0-9#$@])({_PASSWORD_WORDS})(\s*\(\s*)"
    r"(?:'(?:[^'\n]|'')*'?|\"[^\"\n]*\"?|[^)\n]*)"
)
# TSO logon positional form: LOGON userid/password[/newpassword].
_TSO_LOGON_RE = re.compile(r"(?i)\b(LOGON\s+[A-Z#$@][A-Z0-9#$@]{0,7})/[^\s/]*(?:/[^\s/]*)?")
# Key words of the key-value form (shared with `_KEY_WORD_END_RE`).
_KV_KEY_WORDS = (
    rf"{_PASSWORD_WORDS}|secret|api[_-]?key|apikey|"
    r"auth[_-]?token|access[_-]?token|bearer[_-]?token|token|credential"
)
_KV_SECRET_RE = re.compile(
    rf"(?i)\b({_KV_KEY_WORDS})"
    r"(\s*[=:]\s*)"
    # Closing ) or quote optional: the byte cap or a record end can cut it off.
    r"(?:\([^)\n]*\)?|'[^'\n]*'?|\"[^\"\n]*\"?|[^\s,;]+)"
)
_SPACE_PASSWORD_RE = re.compile(
    r"(?i)\b(password|passwd|passphrase)(\s+)"
    r"(?!(?:is|was|were|for|to|in|of|not|must|should|cannot|expired|revoked|"
    r"changed|required|invalid|failed|error|length|rules?|policy)\b)"
    r"(?:'[^'\n]*'?|\"[^\"\n]*\"?|[^\s,;]+)"
)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(Bearer|Basic)(\s+)[A-Za-z0-9._~+/=-]{8,}")
_OBS_BOUNDARY_RE = re.compile(
    r"(?i)(---\s*(?:BEGIN|END)\s+UNTRUSTED\s+Z/OS\s+OBSERVATION[^\n]*)"
)
# Spool text imitating the bridge's own window markers.
_WINDOW_MARKER_RE = re.compile(r"(?i)\[\s*\.\.\.\s*z/OS\s+bridge\b[^\n]*")
# Every key a single-line redaction pattern can carry across a line end to its
# value (the D6 keywords and key-value keys, TSO LOGON, the auth schemes),
# matched where the text before a candidate tail start ends. The keys are
# words far shorter than _KEY_SEARCH_CHARS.
_KEY_WORD_END_RE = re.compile(rf"(?i)(?:{_KV_KEY_WORDS}|logon|bearer|basic)\Z")
_KEY_SEARCH_CHARS = 64
# The word run at a tail ring's start, which may be the end of a key cut by the
# receive (U+FFFD: a multibyte character split there).
_LEAD_RUN_RE = re.compile(r"[\w#$@\ufffd-]*")


class _CapExceeded(Exception):
    pass


class _JesUnavailable(Exception):
    """SITE JES setup refused: the JES FTP interface (not a job) is missing."""


class _LogonFailed(Exception):
    """FTP 530 authentication failure (never retried)."""


class _LogonBreakerOpen(Exception):
    """Logon-failure breaker is open; no FTP connection or logon attempted."""


class _TlsVerificationError(Exception):
    """FTPS handshake or certificate/hostname verification failed (no fallback)."""


class _DeniedError(Exception):
    """Target or operation denied by bridge policy before or during execution."""


class _InvalidParamsError(Exception):
    """Malformed target or tool parameter rejected before any FTP command."""


def _has_disallowed_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) >= 127 or ch in _FORBIDDEN_NAME_CHARS for ch in value)


def parse_dataset_target(dataset: str, member: str | None = None) -> tuple[str, str | None]:
    """Validate and canonicalize an exact MVS dataset or PDS(MEMBER) target.

    Refuses quotes, wildcards, relative GDG references (e.g. `(0)`, `(-1)`,
    `(+1)`), control/non-ASCII characters, internal whitespace, double-member
    specifications, and invalid qualifiers/member names.
    """
    if not isinstance(dataset, str) or _has_disallowed_chars(dataset):
        raise _InvalidParamsError("dataset contains invalid characters")
    raw_dsn = dataset.strip(" ")
    if not raw_dsn or " " in raw_dsn:
        raise _InvalidParamsError("dataset must be a single valid MVS name")

    raw_mem: str | None = None
    if member is not None:
        if not isinstance(member, str) or _has_disallowed_chars(member):
            raise _InvalidParamsError("member contains invalid characters")
        if member != "":
            stripped = member.strip(" ")
            if not stripped or " " in stripped or "(" in stripped or ")" in stripped:
                raise _InvalidParamsError("member must be a valid PDS member name")
            raw_mem = stripped

    mem_part: str | None
    if "(" in raw_dsn or ")" in raw_dsn:
        if raw_mem is not None:
            raise _InvalidParamsError("member must not be specified both inline and separately")
        if raw_dsn.count("(") != 1 or raw_dsn.count(")") != 1 or not raw_dsn.endswith(")"):
            raise _InvalidParamsError("malformed PDS member syntax")
        dsn_part, _, rest = raw_dsn.partition("(")
        mem_part = rest[:-1]
        if not mem_part:
            raise _InvalidParamsError("PDS member name must not be empty")
    else:
        dsn_part = raw_dsn
        mem_part = raw_mem

    dsn = dsn_part.upper()
    if not dsn or len(dsn) > 44 or dsn.startswith(".") or dsn.endswith(".") or ".." in dsn:
        raise _InvalidParamsError("dataset name length or dot placement is invalid")
    qualifiers = dsn.split(".")
    if len(qualifiers) > 22 or any(not _QUALIFIER_RE.fullmatch(q) for q in qualifiers):
        raise _InvalidParamsError("dataset qualifier is invalid")

    mem: str | None = None
    if mem_part is not None:
        mem = mem_part.upper()
        if not _MEMBER_RE.fullmatch(mem):
            raise _InvalidParamsError("PDS member name is invalid")
    return dsn, mem


def canonical_dataset_name(dsn: str, member: str | None = None) -> str:
    return f"{dsn}({member})" if member else dsn


def _split_list(raw: str | Sequence[str]) -> list[str]:
    if isinstance(raw, str):
        return [item.strip() for item in re.split(r"[,\n]", raw) if item.strip()]
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def parse_dataset_allowlist(raw: str | Sequence[str]) -> tuple[str, ...]:
    """Validate and canonicalize operator-configured exact dataset allowlist."""
    entries: list[str] = []
    seen: set[str] = set()
    for item in _split_list(raw):
        try:
            dsn, mem = parse_dataset_target(item)
        except _InvalidParamsError as exc:
            raise ValueError(f"invalid dataset allowlist entry: {exc}") from exc
        canonical = canonical_dataset_name(dsn, mem)
        if canonical not in seen:
            seen.add(canonical)
            entries.append(canonical)
    return tuple(entries)


def parse_enabled_tools(raw: str | Sequence[str]) -> tuple[str, ...]:
    """Validate `MCP_ENABLED_TOOLS` against the ADR-0012 pilot subset.

    `uss_read` remains unapproved and is refused even if named. Any tool
    outside the closed four is refused. Empty tool lists are refused.
    """
    items = _split_list(raw)
    if not items:
        raise ValueError("MCP_ENABLED_TOOLS must name at least one approved tool")
    enabled: list[str] = []
    seen: set[str] = set()
    for name in items:
        if name == "uss_read":
            raise ValueError("uss_read is not approved for the ADR-0012 pilot")
        if name not in PILOT_APPROVED_TOOLS:
            raise ValueError(f"tool is not in the approved pilot subset: {name}")
        if name not in seen:
            seen.add(name)
            enabled.append(name)
    return tuple(enabled)


def parse_jes_prefixes(raw: str | Sequence[str]) -> tuple[str, ...]:
    """Validate JES jobname/owner prefixes (exact prefixes, never wildcards)."""
    entries: list[str] = []
    seen: set[str] = set()
    for item in _split_list(raw):
        if _has_disallowed_chars(item) or " " in item:
            raise ValueError("JES prefix contains invalid or wildcard characters")
        upper = item.upper()
        if not _JES_IDENT_RE.fullmatch(upper):
            raise ValueError("JES prefix must match [A-Z#$@][A-Z0-9#$@]{0,7}")
        if upper not in seen:
            seen.add(upper)
            entries.append(upper)
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class FTPConfig:
    """Connection + ADR-0012 policy bounds.

    Password arrives via env only (there is deliberately no CLI flag for it).
    `transport_mode` is pinned by configuration: `"ftps"` (verified TLS,
    fail-closed default) or `"plain"` (requires explicit `plain_ftp_accepted=True`
    recording site security owner acceptance). Zero automatic downgrade.
    """

    host: str
    user: str
    password: str
    port: int = 21
    timeout_s: float = 15.0
    max_bytes: int = 262144
    transport_mode: Literal["ftps", "plain"] = "ftps"
    plain_ftp_accepted: bool = False
    ca_file: str | None = None
    encoding: str = "latin-1"  # server's TYPE A output code page (see module doc)
    dataset_allowlist: tuple[str, ...] = ()
    enabled_tools: tuple[str, ...] = DEFAULT_ENABLED_TOOLS
    jes_jobname_prefixes: tuple[str, ...] = ()
    jes_owner_prefixes: tuple[str, ...] = ()
    logon_breaker_cooldown_s: float = 900.0
    logon_breaker_threshold: int = 1
    # jes_spool_read stream ceilings: past either, the read stops head-only.
    spool_stream_max_bytes: int = 16 * 1024 * 1024
    spool_stream_max_s: float = 8.0

    def __post_init__(self) -> None:
        if self.transport_mode not in ("ftps", "plain"):
            raise ValueError("transport_mode must be 'ftps' or 'plain'")
        if self.transport_mode == "plain" and not self.plain_ftp_accepted:
            raise ValueError("plain FTP requires explicit owner acceptance (plain_ftp_accepted=True)")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self.logon_breaker_cooldown_s <= 0:
            raise ValueError("logon_breaker_cooldown_s must be positive")
        if self.logon_breaker_threshold < 1:
            raise ValueError("logon_breaker_threshold must be >= 1")
        if self.spool_stream_max_bytes < self.max_bytes:
            raise ValueError("spool_stream_max_bytes must be at least max_bytes")
        if not 0 < self.spool_stream_max_s < AGENT_CALL_TIMEOUT_DEFAULT_S:
            raise ValueError(
                "spool_stream_max_s must be positive and below the agent's "
                f"{AGENT_CALL_TIMEOUT_DEFAULT_S:g}s call timeout"
            )
        try:
            codecs.lookup(self.encoding)
        except LookupError as exc:
            raise ValueError(f"unknown encoding: {self.encoding}") from exc
        object.__setattr__(self, "enabled_tools", parse_enabled_tools(self.enabled_tools))
        object.__setattr__(
            self, "dataset_allowlist", parse_dataset_allowlist(self.dataset_allowlist)
        )
        object.__setattr__(
            self, "jes_jobname_prefixes", parse_jes_prefixes(self.jes_jobname_prefixes)
        )
        object.__setattr__(
            self, "jes_owner_prefixes", parse_jes_prefixes(self.jes_owner_prefixes)
        )


class LogonBreaker:
    """FTP 530 logon-failure circuit breaker, bounded under concurrency (D9).

    Every logon runs inside `logon_slot()`, which admits a caller only while
    `failures + in_flight < threshold`. So at most `threshold` failing logons
    are ever in flight or recorded per closed/half-open window, however many
    calls arrive at once (default 1, so a wrong password cannot revoke the
    RACF ID). A caller without a slot waits, bounded by the FTP timeout, and
    re-checks: if the in-flight logon tripped the breaker it is refused without
    touching the server. Only the logon holds a slot; reads run concurrently.
    Once tripped, no logon is admitted until `cooldown_s` passes or an operator
    calls `reset()`; callers after the cooldown are bounded the same way.
    """

    def __init__(
        self,
        *,
        threshold: int = 1,
        cooldown_s: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        if cooldown_s <= 0:
            raise ValueError("cooldown_s must be positive")
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._cond = threading.Condition(threading.Lock())
        self._failures = 0
        self._in_flight = 0
        self._opened_until: float | None = None

    def _expire_locked(self) -> None:
        if self._opened_until is not None and self._clock() >= self._opened_until:
            self._opened_until = None
            self._failures = 0

    @contextlib.contextmanager
    def logon_slot(self, *, wait_s: float) -> Iterator[None]:
        """Admit one logon attempt; record its outcome when the block exits.

        Raises `_LogonBreakerOpen` (no logon) while the breaker is open, and
        `TimeoutError` (no logon) after waiting `wait_s` for a free slot. A
        530 (`_LogonFailed` or a raw 530 `error_perm`) counts as a failure;
        any other exception releases the slot without counting.
        """
        deadline = time.monotonic() + wait_s
        with self._cond:
            while True:
                self._expire_locked()
                if self._opened_until is not None:
                    raise _LogonBreakerOpen("FTP logon breaker is open")
                if self._failures + self._in_flight < self._threshold:
                    self._in_flight += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("no logon slot within the FTP timeout")
                self._cond.wait(remaining)
        outcome: Literal["success", "failure", "neutral"] = "neutral"
        try:
            yield
            outcome = "success"
        except Exception as exc:
            if isinstance(exc, _LogonFailed) or _is_530_error(exc):
                outcome = "failure"
            raise
        finally:
            with self._cond:
                self._in_flight -= 1
                if outcome == "success":
                    self._failures = 0
                elif outcome == "failure":
                    self._failures += 1
                    if self._failures >= self._threshold:
                        self._opened_until = self._clock() + self._cooldown_s
                self._cond.notify_all()

    def reset(self) -> None:
        with self._cond:
            self._failures = 0
            self._opened_until = None
            self._cond.notify_all()

    def is_open(self) -> bool:
        with self._cond:
            self._expire_locked()
            return self._opened_until is not None


def build_verified_tls_context(ca_file: str | None = None) -> ssl.SSLContext:
    """Strict server-verifying TLS context for FTPS (ADR-0012 D5).

    Always requires certificate verification (`CERT_REQUIRED`), hostname
    matching (`check_hostname=True`), and TLS 1.2+. Never uses ftplib's
    unverified stdlib default context.
    """
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


@dataclass(frozen=True, slots=True)
class TransportProbeResult:
    """Outcome of the one-time logon-free FTP/FTPS connectivity probe (D5)."""

    host: str
    port: int
    outcome: Literal["ftps_verified", "ftps_unverified", "plain_only", "unreachable"]
    recommended_mode: Literal["ftps", "plain"] | None


def probe_transport(
    host: str,
    port: int = 21,
    *,
    timeout_s: float = 15.0,
    ca_file: str | None = None,
    ftp_cls: type[ftplib.FTP_TLS] | None = None,
) -> TransportProbeResult:
    """Logon-free connectivity probe (ADR-0012 D5, point 5).

    Connects to the FTP control port and tests `AUTH TLS` with verified CA and
    hostname checking, then sends `QUIT`. Never sends `USER` or `PASS`.
    """
    cls = ftp_cls or ftplib.FTP_TLS
    ctx = build_verified_tls_context(ca_file)
    ftp = cls(context=ctx)
    try:
        try:
            ftp.connect(host, port, timeout=timeout_s)
        except (OSError, ftplib.Error):
            return TransportProbeResult(
                host=host, port=port, outcome="unreachable", recommended_mode=None
            )
        try:
            ftp.auth()
        except (ssl.SSLError, ssl.CertificateError):
            return TransportProbeResult(
                host=host, port=port, outcome="ftps_unverified", recommended_mode=None
            )
        except ftplib.error_perm:
            return TransportProbeResult(
                host=host, port=port, outcome="plain_only", recommended_mode="plain"
            )
        except (OSError, ftplib.Error):
            return TransportProbeResult(
                host=host, port=port, outcome="ftps_unverified", recommended_mode=None
            )
        return TransportProbeResult(
            host=host, port=port, outcome="ftps_verified", recommended_mode="ftps"
        )
    finally:
        with contextlib.suppress(Exception):
            ftp.quit()
        with contextlib.suppress(Exception):
            ftp.close()


def _is_530_error(exc: Exception) -> bool:
    return isinstance(exc, ftplib.error_perm) and str(exc).strip().startswith("530")


def connect(
    config: FTPConfig,
    factory: type[ftplib.FTP] = ftplib.FTP,
    *,
    breaker: LogonBreaker | None = None,
) -> Any:
    """Open one FTP/FTPS session for a single tool call.

    - With a breaker, the whole session setup runs inside its logon slot, so
      no socket is opened while the breaker is open (see `LogonBreaker`).
    - In `ftps` mode, builds a verified TLS context (CA + hostname check),
      completes `AUTH TLS` BEFORE `login()`, refuses any TLS failure with zero
      downgrade fallback, and protects the data channel with `PROT P`.
    - In `plain` mode, requires `plain_ftp_accepted=True`.
    - Sets explicit passive mode (`ftp.set_pasv(True)`) on every session.
    """
    if breaker is None:
        return _open_session(config, factory)
    with breaker.logon_slot(wait_s=config.timeout_s):
        return _open_session(config, factory)


def _open_session(config: FTPConfig, factory: type[ftplib.FTP]) -> Any:
    if config.transport_mode == "ftps":
        ctx = build_verified_tls_context(config.ca_file)
        ftp: Any = ftplib.FTP_TLS(context=ctx)
        try:
            ftp.connect(config.host, config.port, timeout=config.timeout_s)
            try:
                ftp.auth()
            except (ssl.SSLError, ftplib.error_perm, ftplib.error_temp, OSError) as exc:
                raise _TlsVerificationError("FTPS verification failed") from exc
        except Exception:
            with contextlib.suppress(Exception):
                ftp.close()
            raise
    else:
        if not config.plain_ftp_accepted:
            raise ValueError("plain FTP requires explicit owner acceptance")
        ftp = factory()
        try:
            ftp.connect(config.host, config.port, timeout=config.timeout_s)
        except Exception:
            with contextlib.suppress(Exception):
                ftp.close()
            raise

    try:
        if config.transport_mode == "ftps":
            # TLS was already established and verified by ftp.auth() above.
            ftp.login(config.user, config.password, secure=False)
        else:
            ftp.login(config.user, config.password)
    except Exception as exc:
        with contextlib.suppress(Exception):
            ftp.close()
        if _is_530_error(exc):
            raise _LogonFailed("FTP logon failed") from exc
        raise

    try:
        if config.transport_mode == "ftps" and isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        ftp.set_pasv(True)
    except Exception:
        with contextlib.suppress(Exception):
            ftp.close()
        raise
    return ftp


def screen_zos_text(text: str) -> str:
    """Screen raw z/OS text before returning (ADR-0012 D6).

    - Normalizes line endings (`\\r\\n`, `\\r`, Unicode line/paragraph seps -> `\\n`).
    - Strips C0/C1 control characters and Unicode format/bidi characters
      (category Cc/Cf), retaining only `\\n` and `\\t`.
    - Redacts PEM private keys, including one cut open by the byte cap (BEGIN
      without END redacts to the end, END without BEGIN from the start);
      RACF/TSO/JCL password and passphrase operands
      (`PASSWORD(x)`, `PHRASE('a b')`, `PW(x)`, `PASS(x)`, `PWD(x)`, the
      `NEW*` forms, `KEYMASKED`/`KEYENCRYPTED`/`SESSKEY`; any case, quoted or
      bare, closed or cut open by the byte cap); TSO `LOGON user/pw[/newpw]`;
      password/secret/token key-value assignments (`=` or `:`); space-separated
      password values; and Bearer/Basic auth headers.
    """
    return _redact_zos_text(_normalize_zos_text(text))


def _normalize_zos_text(text: str) -> str:
    """Line ends to `\\n`; C0/C1 control and format/bidi characters dropped."""
    normalized = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
    )
    return "".join(
        ch
        for ch in normalized
        if ch in ("\n", "\t") or unicodedata.category(ch) not in ("Cc", "Cf")
    )


def _redact_zos_text(cleaned: str) -> str:
    """The D6 redactions over normalized text (see `screen_zos_text`)."""
    cleaned = _PEM_PRIVATE_KEY_RE.sub("[REDACTED]", cleaned)
    cleaned = _PEM_OPEN_BEGIN_RE.sub("[REDACTED]", cleaned)
    cleaned = _PEM_OPEN_END_RE.sub("[REDACTED]", cleaned)
    cleaned = _OPERAND_SECRET_RE.sub(r"\1\2[REDACTED]", cleaned)
    cleaned = _TSO_LOGON_RE.sub(r"\1/[REDACTED]", cleaned)
    cleaned = _KV_SECRET_RE.sub(r"\1\2[REDACTED]", cleaned)
    cleaned = _SPACE_PASSWORD_RE.sub(r"\1\2[REDACTED]", cleaned)
    return _AUTH_SCHEME_RE.sub(r"\1\2[REDACTED]", cleaned)


def frame_untrusted_observation(
    text: str, *, tool: str, target: str, window: str | None = None
) -> str:
    """Wrap screened z/OS text in untrusted observation delimiters (D6).

    Any embedded delimiter markers inside the payload are neutralized so
    untrusted dataset or spool lines cannot forge an early block close.
    `window` (bridge-authored, never data) repeats a spool window's facts in
    the header, because the gap marker in the body could be forged by data.
    """
    safe_body = _OBS_BOUNDARY_RE.sub("[neutralized-observation-marker]", text)
    facts = f" {window}" if window else ""
    header = f"{OBS_BEGIN_PREFIX} (tool={tool} target={target}{facts}) ---"
    if safe_body and not safe_body.endswith("\n"):
        return f"{header}\n{safe_body}\n{OBS_END_MARKER}"
    return f"{header}\n{safe_body}{OBS_END_MARKER}"


def _pin_text_format(ftp: Any) -> tuple[str, ...]:
    """Send the `TEXT_FORMAT_SITES` pins before a text read; return the
    names the server did not confirm.

    Confirmed means a reply starting `200` with no "unrecognized" line (z/OS
    is recalled, not verified, to answer 200 while ignoring an operand; ADR-0012
    D8(d) checks the wording). A 202, a 200-unrecognized or a
    5xx (`error_perm`) is a capacity loss (trailing blanks kept), not a
    security failure: the read proceeds and the name is marked on stderr and
    the span with the reply code only, never the reply text. A 4xx
    (`error_temp`) or socket error propagates. Kept out of `connect`, so a
    SITE failure never touches the logon breaker.
    """
    unconfirmed: list[str] = []
    for operand in TEXT_FORMAT_SITES:
        try:
            reply = ftp.sendcmd(f"SITE {operand}")
        except ftplib.error_perm as exc:
            reply = str(exc)
        if reply.startswith("200") and "unrecogni" not in reply.lower():
            continue
        name = operand.split("=", 1)[0]
        unconfirmed.append(name)
        code = reply[:3] if reply[:3].isdigit() else "???"
        print(f"mcp bridge: SITE {name} unconfirmed reply_code={code}", file=sys.stderr, flush=True)
    if unconfirmed:
        trace.get_current_span().set_attribute("mcp.ftp.site_unconfirmed", ",".join(unconfirmed))
    return tuple(unconfirmed)


def _receive_ascii(
    ftp: Any,
    command: str,
    accept: Callable[[bytes], bool],
    *,
    deadline: float | None = None,
    timeout_s: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
) -> Literal["complete", "stopped", "deadline"]:
    """The TYPE A (ASCII) receive loop shared by every text read.

    Sends `TYPE A` itself and reads the data connection directly: ftplib's
    `retrbinary` would switch to `TYPE I` and receive untranslated EBCDIC,
    and `retrlines` drops record ends and refuses lines over 8 KiB. The
    payload is the server's translated output (its ASCII code page, CRLF
    record ends), decoded by the caller with `FTPConfig.encoding`.

    `accept` takes each chunk and returns False to stop (`"stopped"`). With a
    `deadline`, each receive waits at most `min(timeout_s, remaining)`; a
    spent deadline returns `"deadline"`, while a stall inside it still raises
    the socket timeout. The caller ends the transfer (reply, ABOR or close).
    """
    ftp.sendcmd("TYPE A")
    outcome: Literal["complete", "stopped", "deadline"] = "complete"
    with ftp.transfercmd(command) as conn:
        while True:
            if deadline is not None:
                remaining = deadline - clock()
                if remaining <= 0:
                    outcome = "deadline"
                    break
                conn.settimeout(min(timeout_s, remaining))
            try:
                chunk = conn.recv(_RECV_CHUNK)
            except TimeoutError:
                if deadline is not None and clock() >= deadline:
                    outcome = "deadline"
                    break
                raise
            if not chunk:
                break
            if not accept(chunk):
                outcome = "stopped"
                break
        if outcome == "complete" and isinstance(conn, ssl.SSLSocket):
            conn.unwrap()
    return outcome


def _capped_retr(ftp: ftplib.FTP, command: str, max_bytes: int) -> tuple[bytes, bool]:
    """Head-only TYPE A receive aborting past max_bytes. Returns (payload, truncated)."""
    buf = bytearray()

    def accept(chunk: bytes) -> bool:
        room = max_bytes - len(buf)
        buf.extend(chunk[:room])
        return len(chunk) <= room

    truncated = _receive_ascii(ftp, command, accept) == "stopped"
    if truncated:
        with contextlib.suppress(Exception):  # best-effort abort only
            ftp.abort()
    else:
        ftp.voidresp()
    return bytes(buf), truncated


def _spool_budget(max_bytes: int) -> tuple[int, int, int]:
    """(body B, head H, tail T) in UTF-8 bytes of screened text. B leaves
    FRAME_RESERVE_BYTES of max_bytes for the frame (half the cap when the cap
    is too small for the full reserve; such caps cannot hold the frame)."""
    body = max_bytes - min(FRAME_RESERVE_BYTES, max_bytes // 2)
    head = body // SPOOL_HEAD_DIVISOR
    return body, head, body - head


# The text a key marker split by a receive chunk can have before the cut.
PEM_CARRY_CHARS = 1024
_ASCII_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _marker_text(text: str) -> str:
    """`_normalize_zos_text` as far as key markers are concerned (a split
    CR LF may give two line ends), with a fast path for ASCII."""
    if text.isascii():
        return _ASCII_CONTROL_RE.sub("", text.replace("\r", "\n"))
    return _normalize_zos_text(text)


class _OpenKey:
    """Whether a private key is open after the spool bytes fed so far, in
    stream order: the last BEGIN or END line of a private key seen is a
    BEGIN. That is the whole-file rule (a key runs from a BEGIN line to the
    next END line, or to the end of the text; an END with no open key is not
    a key). Bytes are decoded and normalized as screening does; a marker
    split by a chunk edge is found through a carry of the unfinished line
    (up to PEM_CARRY_CHARS). Memory is bounded; time is linear."""

    def __init__(self, encoding: str) -> None:
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self._carry = ""
        self.open = False

    def feed(self, raw: bytes) -> None:
        self.feed_text(self._decoder.decode(raw))

    def feed_text(self, text: str) -> None:
        """Feed text already decoded by `decode_rest` or `feed`."""
        text = self._carry + _marker_text(text)
        consumed = 0
        for match in _PEM_MARKER_RE.finditer(text):
            self.open = match.group(1) == "BEGIN"
            consumed = match.end()
        self._carry = text[max(consumed, text.rfind("\n") + 1, len(text) - PEM_CARRY_CHARS) :]

    def decode_rest(self, raw: bytes) -> str:
        """Decode the bytes after those fed, the decoder state carried over
        (a character split by the last fed chunk is whole)."""
        return self._decoder.decode(raw, final=True)


@dataclass
class _SpoolCapture:
    """Receive state for the spool window: the first `head_cap` bytes, a
    deque of the last chunks holding at least `tail_keep` bytes, and a byte
    count. `head_only` stops once the head is full. `keys`, when set, is fed
    every byte that is not in the ring (the head and each evicted chunk), so
    the tail knows whether it starts inside a private key."""

    head_cap: int
    tail_keep: int
    ceiling: int
    head_only: bool = False
    head: bytearray = field(default_factory=bytearray)
    tail: deque[bytes] = field(default_factory=deque)
    tail_len: int = 0
    total: int = 0
    evicted: bool = False
    complete: bool = False
    keys: _OpenKey | None = None

    def accept(self, chunk: bytes) -> bool:
        if self.total + len(chunk) > self.ceiling:
            return False
        self.total += len(chunk)
        room = self.head_cap - len(self.head)
        if room > 0:
            self.head.extend(chunk[:room])
            if self.keys is not None:
                self.keys.feed(chunk[:room])
            chunk = chunk[room:]
        if self.head_only:
            return len(self.head) < self.head_cap
        if chunk:
            self.tail.append(chunk)
            self.tail_len += len(chunk)
            while self.tail_len - len(self.tail[0]) >= self.tail_keep:
                evicted = self.tail.popleft()
                self.tail_len -= len(evicted)
                self.evicted = True
                if self.keys is not None:
                    self.keys.feed(evicted)
        return True


_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+")


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _screen_part(raw: bytes, encoding: str, *, final: bool) -> str:
    """Decode one window part once (an incomplete trailing character is held
    back unless `final`), screen it, and neutralize window-marker lookalikes."""
    text = codecs.getincrementaldecoder(encoding)(errors="replace").decode(raw, final=final)
    return _WINDOW_MARKER_RE.sub("[neutralized-window-marker]", screen_zos_text(text))


def _keep_first_lines(text: str, budget: int) -> str:
    """Whole lines from the start within budget UTF-8 bytes (a first line
    longer than the budget is cut at a character boundary)."""
    if _utf8_len(text) <= budget:
        return text
    kept: list[str] = []
    used = 0
    for line in _LINE_RE.findall(text):
        size = _utf8_len(line)
        if used + size > budget:
            break
        kept.append(line)
        used += size
    if not kept:
        return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return "".join(kept)


def _keep_last_lines(text: str, budget: int) -> str:
    """Whole lines from the end within budget UTF-8 bytes (a last line longer
    than the budget keeps its end, cut at a character boundary)."""
    if _utf8_len(text) <= budget:
        return text
    kept: list[str] = []
    used = 0
    for line in reversed(_LINE_RE.findall(text)):
        size = _utf8_len(line)
        if used + size > budget:
            break
        kept.append(line)
        used += size
    if not kept:
        if budget <= 0:
            return ""
        return text.encode("utf-8")[-budget:].decode("utf-8", errors="ignore")
    return "".join(reversed(kept))


def _may_carry_a_key(text: str, end: int, lead_run: int) -> bool:
    """Whether a redaction match could span a line start whose preceding text
    has its last non-whitespace character just before `end`: that text ends
    in a key, then at most one `=`, `:` or `(` and whitespace (every
    pattern's separator). A word run reaching the ring's start (`lead_run`)
    may be a key cut by the receive, so it counts too."""
    if end > 0 and text[end - 1] in "=:(":
        end -= 1
        while end > 0 and text[end - 1].isspace():
            end -= 1
    if end <= lead_run:
        return True
    return _KEY_WORD_END_RE.search(text, max(0, end - _KEY_SEARCH_CHARS), end) is not None


def _tail_start(text: str) -> int:
    """The first line start after a normalized tail ring's first line end that
    no redaction match can span (`len(text)` if there is none).

    Every non-PEM value is single-line, so a match spans a line start only
    through its key's separator. From such a start, screening the rest on its
    own pairs keys and values exactly as whole-file screening does; a PEM
    block it starts inside is redacted from the start by the open-END rule,
    or by `_screen_tail` when its END line is missing too.
    Dropping the first line also drops a CR/LF or character split by the
    receive. The size of the ring's lookbehind does not matter for safety.
    """
    lead = _LEAD_RUN_RE.match(text)
    lead_run = lead.end() if lead else 0
    end = 0
    unsafe: bool | None = None
    line_start = 0
    while (line_end := text.find("\n", line_start)) >= 0:
        content = len(text[line_start:line_end].rstrip())
        if content:
            end, unsafe = line_start + content, None
        line_start = line_end + 1
        if unsafe is None:
            unsafe = _may_carry_a_key(text, end, lead_run)
        if not unsafe:
            return line_start
    return len(text)


def _screen_tail(raw: bytes, encoding: str, keys: _OpenKey | None = None) -> str:
    """Decode an evicted tail ring and screen it from `_tail_start` on, then
    neutralize window-marker lookalikes.

    With `keys` (fed every byte before the ring), a private key still open
    at the tail start (its BEGIN line in the head, the gap or the ring's
    lead) is redacted from the tail start to its END line, or to the end of
    the tail when it has none, as whole-file screening redacts it."""
    if keys is None:
        decoded = codecs.getincrementaldecoder(encoding)(errors="replace").decode(raw, final=True)
    else:
        decoded = keys.decode_rest(raw)
    text = _normalize_zos_text(decoded)
    start = _tail_start(text)
    rest = text[start:]
    lead = ""
    if keys is not None:
        keys.feed_text(text[:start])
        if keys.open:
            end = _PEM_END_RE.search(rest)
            lead, rest = "[REDACTED]", rest[end.end() :] if end else ""
    screened = lead + _redact_zos_text(rest)
    return _WINDOW_MARKER_RE.sub("[neutralized-window-marker]", screened)


def _join_window(head: str, marker: str, tail: str) -> str:
    if head and not head.endswith("\n"):
        head += "\n"
    return f"{head}{marker}\n{tail}"


def _spool_window(
    capture: _SpoolCapture, encoding: str, max_bytes: int
) -> tuple[str, dict[str, Any], str | None]:
    """Assemble a spool read into (body, structured fields, header facts).

    A complete, contiguous read is screened once as a whole, as a full read
    would be: it returns whole if it fits B, otherwise its first lines within
    H and last lines within T. With a gap in the read (the tail ring evicted
    chunks), the head is cut back to its last line end, and each part is
    decoded and screened on its own, so a single-line redaction cannot cross
    the cut or eat the marker. The tail is screened from its first line
    start that no redaction match can span (`_tail_start`), found over the
    ring's partial first line and lookbehind, so its keys and values pair as
    in whole-file screening. Only then are whole lines trimmed to H and T,
    and the caller frames the joined body.
    """
    body_budget, head_budget, tail_budget = _spool_budget(max_bytes)
    head_raw = bytes(capture.head)
    tail_raw = b"".join(capture.tail)
    if capture.complete and not capture.evicted:
        whole = _screen_part(head_raw + tail_raw, encoding, final=True)
        if _utf8_len(whole) <= body_budget:
            fields = {
                "truncated": False,
                "window": "full",
                "total_bytes": capture.total,
                "omitted_bytes": 0,
                "stream_complete": True,
            }
            return whole, fields, None
        # H + T = B is under the text's size, so the two parts never overlap.
        return _head_tail_window(
            _keep_first_lines(whole, head_budget),
            _keep_last_lines(whole, tail_budget),
            capture.total,
        )
    cut = head_raw.rfind(b"\n")
    if cut >= 0:
        head_raw = head_raw[: cut + 1]
    head = _keep_first_lines(_screen_part(head_raw, encoding, final=False), head_budget)
    if not capture.complete:
        body = _join_window(head, SPOOL_STOP_MARKER.format(n=capture.total), "")
        fields = {
            "truncated": True,
            "window": "head_only",
            "total_bytes": None,
            "omitted_bytes": None,
            "stream_complete": False,
        }
        return body, fields, f"window=head-only read_bytes={capture.total}"
    tail = _keep_last_lines(_screen_tail(tail_raw, encoding, capture.keys), tail_budget)
    return _head_tail_window(head, tail, capture.total)


def _head_tail_window(head: str, tail: str, total: int) -> tuple[str, dict[str, Any], str]:
    omitted = max(0, total - _utf8_len(head) - _utf8_len(tail))
    body = _join_window(head, SPOOL_GAP_MARKER.format(n=omitted), tail)
    fields = {
        "truncated": True,
        "window": "head_tail",
        "total_bytes": total,
        "omitted_bytes": omitted,
        "stream_complete": True,
    }
    return body, fields, f"window=head+tail omitted_bytes={omitted} total_bytes={total}"


def _tool_result(
    text: str,
    truncated: bool = False,
    *,
    frame_tool: str | None = None,
    frame_target: str | None = None,
) -> dict:
    screened = screen_zos_text(text)
    if truncated:
        screened += TRUNCATION_SUFFIX
    if frame_tool is not None and frame_target is not None:
        screened = frame_untrusted_observation(screened, tool=frame_tool, target=frame_target)
    return {"content": [{"type": "text", "text": screened}], "isError": False}


def _tool_error(code: str, message: str) -> dict:
    return {
        "content": [{"type": "text", "text": f"{code}: {message}"}],
        "isError": True,
    }


def _classify_ftp_error(exc: Exception) -> tuple[str, str]:
    """Stable codes and fixed messages from ftplib/policy failures.

    Never echoes exception text, upstream replies, or credentials.
    """
    if isinstance(exc, _InvalidParamsError):
        return INVALID_PARAMS, "invalid target or parameters"
    if isinstance(exc, _DeniedError):
        return DENIED, "requested z/OS target is not permitted"
    if isinstance(exc, _LogonBreakerOpen):
        return LOGON_FAILED, "FTP logon paused by failure breaker"
    if isinstance(exc, _LogonFailed) or _is_530_error(exc):
        return LOGON_FAILED, "FTP logon failed"
    if isinstance(exc, (_TlsVerificationError, ssl.SSLError)):
        return TLS_ERROR, "FTPS server verification failed"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return TIMEOUT, "FTP operation timed out"
    if isinstance(exc, _JesUnavailable):
        return JES_UNAVAILABLE, "JES FTP interface refused the command"
    if isinstance(exc, ftplib.error_perm) and str(exc).strip().startswith("550"):
        return NOT_FOUND, "no such dataset, member, path, job, or spool file"
    return UPSTREAM_ERROR, "FTP request failed"


@dataclass
class FTPSession:
    """One logged-in session shared by a single tool call."""

    ftp: Any = field(repr=False)
    config: FTPConfig = field(repr=False)
    clock: Callable[[], float] = field(default_factory=lambda: time.monotonic, repr=False)

    def read_text(self, command: str) -> tuple[str, bool]:
        """Head-only text read capped at max_bytes (dataset_read, uss_read)."""
        _pin_text_format(self.ftp)
        raw, truncated = _capped_retr(self.ftp, command, self.config.max_bytes)
        return raw.decode(self.config.encoding, errors="replace"), truncated

    def read_spool(self, command: str, *, size_hint: int | None = None) -> _SpoolCapture:
        """Start-and-end windowed text read (jes_spool_read).

        Streams the whole file, keeping the head and the last tail bytes, until
        it ends or hits `spool_stream_max_bytes` or `spool_stream_max_s`. A
        `size_hint` (LIST BYTE-COUNT) above the byte ceiling reads the head
        only. On a ceiling the data and control connections are closed with
        no ABOR or final reply wait, since either can block for timeout_s.
        """
        cfg = self.config
        _, head_budget, tail_budget = _spool_budget(cfg.max_bytes)
        capture = _SpoolCapture(
            head_cap=head_budget,
            tail_keep=tail_budget + TAIL_LOOKBEHIND_BYTES,
            ceiling=cfg.spool_stream_max_bytes,
            head_only=size_hint is not None and size_hint > cfg.spool_stream_max_bytes,
            keys=_OpenKey(cfg.encoding),
        )
        _pin_text_format(self.ftp)
        outcome = _receive_ascii(
            self.ftp,
            command,
            capture.accept,
            deadline=self.clock() + cfg.spool_stream_max_s,
            timeout_s=cfg.timeout_s,
            clock=self.clock,
        )
        if outcome == "complete":
            self.ftp.voidresp()
            capture.complete = True
        else:
            with contextlib.suppress(Exception):  # the session ends here
                self.ftp.close()
        return capture


def _check_dataset_policy(config: FTPConfig, dataset: str, member: str | None = None) -> tuple[str, str | None]:
    dsn, mem = parse_dataset_target(dataset, member)
    canonical = canonical_dataset_name(dsn, mem)
    if canonical not in config.dataset_allowlist:
        raise _DeniedError("dataset is not on the allowlist")
    return dsn, mem


def _matches_jes_prefixes(config: FTPConfig, job_name: str, owner: str) -> bool:
    if not config.jes_jobname_prefixes and not config.jes_owner_prefixes:
        return False
    upper_name = job_name.upper()
    upper_owner = owner.upper()
    if config.jes_jobname_prefixes and not any(
        upper_name.startswith(p) for p in config.jes_jobname_prefixes
    ):
        return False
    return not (
        config.jes_owner_prefixes
        and not any(upper_owner.startswith(p) for p in config.jes_owner_prefixes)
    )


def _validate_jes_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _has_disallowed_chars(value):
        raise _InvalidParamsError(f"{label} contains invalid characters")
    stripped = value.strip(" ")
    if not stripped or " " in stripped:
        raise _InvalidParamsError(f"{label} must be a single exact identifier")
    upper = stripped.upper()
    if not _JES_IDENT_RE.fullmatch(upper):
        raise _InvalidParamsError(f"{label} is not a valid exact JES identifier")
    return upper


def _validate_job_id(value: str) -> str:
    if not isinstance(value, str) or _has_disallowed_chars(value):
        raise _InvalidParamsError("job_id contains invalid characters")
    stripped = value.strip(" ")
    if not stripped or " " in stripped:
        raise _InvalidParamsError("job_id must be a single exact JOBnnnnn identifier")
    upper = stripped.upper()
    if not _JOB_ID_RE.fullmatch(upper):
        raise _InvalidParamsError("job_id must match JOBnnnnn")
    return upper


def _validate_spool_id(value: str) -> str:
    if not isinstance(value, str) or _has_disallowed_chars(value):
        raise _InvalidParamsError("spool_id contains invalid characters")
    stripped = value.strip(" ")
    if not _SPOOL_ID_RE.fullmatch(stripped) or int(stripped) < 1:
        raise _InvalidParamsError("spool_id must be a positive integer")
    return str(int(stripped))


def _check_job_status_policy(
    config: FTPConfig,
    job_name: str | None = None,
    owner: str | None = None,
    job_id: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    clean_id = _validate_job_id(job_id) if job_id is not None and job_id != "" else None
    clean_name = (
        _validate_jes_identifier(job_name, "job_name")
        if job_name is not None and job_name != ""
        else None
    )
    clean_owner = (
        _validate_jes_identifier(owner, "owner")
        if owner is not None and owner != ""
        else None
    )
    if clean_id is None and not (clean_name and clean_owner):
        raise _InvalidParamsError("job_status requires an exact job_id or exact job_name + owner")
    if not config.jes_jobname_prefixes and not config.jes_owner_prefixes:
        raise _DeniedError("JES prefix allowlist is not configured")
    if clean_name and config.jes_jobname_prefixes and not any(
        clean_name.startswith(p) for p in config.jes_jobname_prefixes
    ):
        raise _DeniedError("job_name is outside the allowed JES jobname prefixes")
    if clean_owner and config.jes_owner_prefixes and not any(
        clean_owner.startswith(p) for p in config.jes_owner_prefixes
    ):
        raise _DeniedError("owner is outside the allowed JES owner prefixes")
    return clean_name, clean_owner, clean_id


def _check_jes_spool_policy(config: FTPConfig, job_id: str, spool_id: str) -> tuple[str, str]:
    clean_id = _validate_job_id(job_id)
    clean_sid = _validate_spool_id(spool_id)
    if not config.jes_jobname_prefixes and not config.jes_owner_prefixes:
        raise _DeniedError("JES prefix allowlist is not configured")
    return clean_id, clean_sid


def validate_tool_policy(name: str, args: dict[str, Any], config: FTPConfig) -> dict | None:
    """Pre-connect policy check so denied/invalid calls never open FTP."""
    try:
        if name == "dataset_read":
            dataset = args.get("dataset", "")
            member = args.get("member")
            _check_dataset_policy(config, dataset, member if member else None)
        elif name == "job_status":
            _check_job_status_policy(
                config,
                job_name=args.get("job_name") or None,
                owner=args.get("owner") or None,
                job_id=args.get("job_id") or None,
            )
        elif name == "jes_spool_read":
            _check_jes_spool_policy(
                config,
                args.get("job_id", ""),
                args.get("spool_id", ""),
            )
        elif name == "uss_read":
            raise _DeniedError("uss_read is not approved")
    except Exception as exc:  # noqa: BLE001
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)
    return None


def dataset_read(session: FTPSession, dataset: str, member: str | None = None) -> dict:
    """Read an allowlisted sequential dataset or PDS member (TYPE A).

    No PDS-without-member NLST fallback (ADR-0012 D2 point 3): a 550 from RETR
    returns `not_found` without sending NLST.
    """
    try:
        dsn, mem = _check_dataset_policy(session.config, dataset, member)
    except Exception as exc:  # noqa: BLE001
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)

    canonical = canonical_dataset_name(dsn, mem)
    quoted = f"'{dsn}'"
    try:
        cmd = f"RETR {quoted}({mem})" if mem else f"RETR {quoted}"
        text, truncated = session.read_text(cmd)
        return _tool_result(text, truncated, frame_tool="dataset_read", frame_target=canonical)
    except Exception as exc:  # noqa: BLE001 — mapped to stable codes below
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)


def uss_read(session: FTPSession, path: str) -> dict:
    """Read a USS file (TYPE A converts EBCDIC text).

    Unapproved for the ADR-0012 pilot (`MCP_ENABLED_TOOLS` refuses it).
    Kept in the closed four-tool module surface for ADR-0003 continuity.
    """
    clean = path.strip()
    if not clean or ".." in clean.split("/"):
        return _tool_error(INVALID_PARAMS, "path must be absolute without .. segments")
    if not clean.startswith("/"):
        return _tool_error(INVALID_PARAMS, "path must be absolute")
    try:
        text, truncated = session.read_text(f"RETR {clean}")
        return _tool_result(text, truncated, frame_tool="uss_read", frame_target=clean)
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


def _parse_job_line(line: str) -> dict[str, Any] | None:
    """Parse one JES LIST job summary line (skipping headers/separators)."""
    parts = line.split()
    if len(parts) < 4:
        return None
    if parts[0].upper() == "JOBNAME" and parts[1].upper() == "JOBID":
        return None
    if not _JOB_ID_RE.fullmatch(parts[1].upper()):
        return None
    job: dict[str, Any] = {
        "name": parts[0].upper(),
        "job_id": parts[1].upper(),
        "owner": parts[2].upper(),
        "status": parts[3].upper(),
        "completion": None,
        "raw": line,
    }
    # Completion is rendered from validated tokens only, never raw line text.
    # Wire spellings beyond RC= are unverified until the D8(c) dry run.
    rest = " ".join(parts[4:]).upper()
    for token in parts[4:]:
        upper = token.upper()
        if upper.startswith(("RC=", "CC")):
            value = upper.split("=", 1)[-1]
            if _COMPLETION_CODE_RE.fullmatch(value):
                job["completion"] = f"RC={value}"
        elif upper.startswith("ABEND="):
            value = upper.split("=", 1)[1]
            if _COMPLETION_CODE_RE.fullmatch(value):
                job["completion"] = f"ABEND={value}"
    if "JCL ERROR" in rest:
        job["completion"] = "JCL ERROR"
    elif "SYS FAIL" in rest:
        job["completion"] = "SYS FAIL"
    return job


def _is_level2_header(line: str) -> bool:
    """The JESINTERFACELEVEL=2 LIST header; at level 1 the columns differ
    (no OWNER) and would be misread, so the JES tools require it."""
    return [p.upper() for p in line.split()[:5]] == list(_LEVEL2_HEADER)


def _parse_spool_table_line(line: str) -> tuple[int, str, int | None] | None:
    """Parse one JESINTERFACELEVEL=2 spool row: (spool_id_int, ddname, byte_count).

    BYTE-COUNT is None when the row has none; it is only a size hint (its
    units are unverified until the D8 dry run)."""
    stripped = line.strip()
    if not stripped or set(stripped) <= {"-"}:
        return None
    lower = stripped.lower()
    if "spool file" in lower:
        return None
    parts = stripped.split()
    if len(parts) < 4:
        return None
    if parts[0].upper() in ("ID", "JOBNAME"):
        return None
    if not parts[0].isdigit():
        return None
    # Exclude job summary lines (where parts[1] is JOBnnnnn).
    if _JOB_ID_RE.fullmatch(parts[1].upper()):
        return None
    spool_num = int(parts[0])
    # Standard z/OS JESINTERFACELEVEL=2 format:
    #   ID STEPNAME [PROCSTEP] C DDNAME BYTE-COUNT
    byte_count: int | None = None
    if parts[-1].isdigit() and len(parts) >= 5:
        ddname = parts[-2].upper()
        byte_count = int(parts[-1])
    elif len(parts[-2]) == 1:
        ddname = parts[-1].upper()
    else:
        return None
    if not _JES_IDENT_RE.fullmatch(ddname):
        return None
    return spool_num, ddname, byte_count


def job_status(
    session: FTPSession,
    job_name: str | None = None,
    owner: str | None = None,
    job_id: str | None = None,
) -> dict:
    """List one exact job matching bridge-side JES prefix allowlists.

    An exact `job_id` sends `LIST <jobid>` (a bare `LIST` is bounded by
    JESENTRYLIMIT and could miss the job); a name/owner query sends a bare
    `LIST` under the `SITE` filters. The listing must carry the
    JESINTERFACELEVEL=2 header or the call fails closed (`jes_unavailable`).
    Lines are filtered as the `LIST` streams in; the rendered output is capped
    at `max_bytes` like `dataset_read` (truncation suffix, transfer aborted),
    so a large listing is never buffered whole.
    """
    try:
        clean_name, clean_owner, clean_id = _check_job_status_policy(
            session.config, job_name=job_name, owner=owner, job_id=job_id
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)

    try:
        _jes_mode(session, clean_owner or "*", clean_name or "*")
        cap = session.config.max_bytes
        rendered: list[str] = []
        used = 0
        matched_any_target = False
        allowed_any = False
        saw_header = False

        def _collect(line: str) -> None:
            nonlocal used, matched_any_target, allowed_any, saw_header
            if not saw_header:
                saw_header = _is_level2_header(line)
                return
            parsed = _parse_job_line(line)
            if parsed is None:
                return
            if clean_id and parsed["job_id"] != clean_id:
                return
            if clean_name and parsed["name"] != clean_name:
                return
            if clean_owner and parsed["owner"] != clean_owner:
                return
            matched_any_target = True
            if not _matches_jes_prefixes(session.config, parsed["name"], parsed["owner"]):
                return
            allowed_any = True
            piece = f"{parsed['name']} {parsed['job_id']} {parsed['status']}"
            if parsed.get("completion"):
                piece += f" {parsed['completion']}"
            if rendered:
                piece = "\n" + piece
            encoded = piece.encode("utf-8")
            if used + len(encoded) > cap:
                rendered.append(encoded[: cap - used].decode("utf-8", errors="ignore"))
                raise _CapExceeded
            rendered.append(piece)
            used += len(encoded)

        truncated = False
        try:
            session.ftp.retrlines(f"LIST {clean_id}" if clean_id else "LIST", _collect)
        except _CapExceeded:
            truncated = True
            with contextlib.suppress(Exception):  # best-effort abort only
                session.ftp.abort()
        if not saw_header:
            raise _JesUnavailable("LIST lacks the JESINTERFACELEVEL=2 header")
        if matched_any_target and not allowed_any:
            return _tool_error(DENIED, "requested z/OS target is not permitted")
        target_label = clean_id or f"{clean_name}/{clean_owner}"
        return _tool_result(
            "".join(rendered) or "No matching jobs.",
            truncated,
            frame_tool="job_status",
            frame_target=target_label,
        )
    except Exception as exc:  # noqa: BLE001 — mapped to stable codes below
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)


def jes_spool_read(session: FTPSession, job_id: str, spool_id: str) -> dict:
    """Read one DD-scoped spool file (`JESMSGLG`/`JESYSMSG` only, Option 4A).

    Always runs `SITE FILETYPE=JES` + `LIST <JOBID>` BEFORE any `RETR` to
    verify both the job's jobname/owner against the bridge-side prefix
    allowlists and the spool file's DDNAME against `ALLOWED_JES_DDS`. Sends
    the text-format SITE pins and explicit `TYPE A` before
    `RETR <JOBID>.<spool_id>`.

    The result is a start-and-end window (see `_spool_window`): a file whose
    screened text fits the cap comes back whole; a longer one keeps its first
    quarter and last three quarters around a gap marker, so completion codes
    and abends at the end survive. The framed result stays within
    `max_bytes`, and `structuredContent` carries `truncated`, `window`
    (`full | head_tail | head_only`), `total_bytes`, `omitted_bytes`
    (approximate) and `stream_complete`.
    """
    try:
        jid, sid = _check_jes_spool_policy(session.config, job_id, spool_id)
    except Exception as exc:  # noqa: BLE001
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)

    try:
        _jes_mode(session)
        lines: list[str] = []
        session.ftp.retrlines(f"LIST {jid}", lines.append)

        if not any(_is_level2_header(line) for line in lines):
            # JESINTERFACELEVEL=1 (or unknown) layout: columns would be misread.
            raise _JesUnavailable("LIST lacks the JESINTERFACELEVEL=2 header")
        job_info: dict[str, Any] | None = None
        spool_dds: dict[int, tuple[str, int | None]] = {}
        for line in lines:
            parsed_job = _parse_job_line(line)
            if parsed_job is not None and parsed_job["job_id"] == jid:
                job_info = parsed_job
                continue
            parsed_spool = _parse_spool_table_line(line)
            if parsed_spool is not None:
                spool_num, ddname, byte_count = parsed_spool
                spool_dds[spool_num] = (ddname, byte_count)

        if job_info is None:
            return _tool_error(NOT_FOUND, "no such dataset, member, path, job, or spool file")
        if not _matches_jes_prefixes(session.config, job_info["name"], job_info["owner"]):
            return _tool_error(DENIED, "requested z/OS target is not permitted")
        if not spool_dds:
            # Cannot verify DDNAME (e.g. JESINTERFACELEVEL=1): fail closed, no RETR.
            return _tool_error(DENIED, "requested z/OS target is not permitted")
        sid_num = int(sid)
        if sid_num not in spool_dds:
            return _tool_error(NOT_FOUND, "no such dataset, member, path, job, or spool file")
        ddname, byte_count = spool_dds[sid_num]
        if ddname not in ALLOWED_JES_DDS:
            return _tool_error(DENIED, "requested z/OS target is not permitted")

        capture = session.read_spool(f"RETR {jid}.{sid}", size_hint=byte_count)  # TYPE A
        body, fields, window = _spool_window(
            capture, session.config.encoding, session.config.max_bytes
        )
        framed = frame_untrusted_observation(
            body, tool="jes_spool_read", target=f"{jid}.{sid}({ddname})", window=window
        )
        return {
            "content": [{"type": "text", "text": framed}],
            "structuredContent": fields,
            "isError": False,
        }
    except Exception as exc:  # noqa: BLE001 — mapped to stable codes below
        code, message = _classify_ftp_error(exc)
        return _tool_error(code, message)
