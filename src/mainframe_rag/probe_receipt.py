"""Text-free receipts for the read-only site probes (#1343).

A probe (the z/OS bridge's `--site-probe`, the GitLab gateway's
`--site-probe`, `sign_vault.py vault-probe`) records one JSON line per
check, then a summary line. A record holds the check id, its status
(`pass`, `fail` or `inconclusive`) and facts that are booleans, integers,
`null` or short tokens: reply codes, versions, fixed reason codes, codec
names. Every string value must match `SAFE_VALUE_RE` (no spaces, slashes,
`@`, `:`, quotes or line breaks), so a free-text line (a dataset record, a
spool line, an issue note, a URL) cannot be stored whole. The pattern does
not stop a short token such as a user id, a host name, a dataset name or a
job id: keeping those out is the probes' job (they reduce server text to
fixed reason codes, reply codes, versions, counts and codec names, and never
record targets or credentials), and the marker tests check it on every path.
`scripts/gap/sign_vault.py` runs as a single stdlib file and carries a copy
of these rules (a test pins the two equal).

Exit codes, shared by the three probes: 0 when no check failed (an
inconclusive check is not a failure: it names a fact a human still has to
judge or a target the operator did not name); 1 when a check failed; 2 when
the probe refused to start (configuration, target or usage error) or the
receipt file could not be written; 3 when the probe stopped on an unexpected
error. A probe that stops still prints its whole receipt: the checks it
finished keep their outcome, the others are `inconclusive` with reason
`probe_error`, and the summary has status `error` and names only the
exception's class (never its message).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import IO

SCHEMA_VERSION = 1
STATUSES: tuple[str, ...] = ("pass", "fail", "inconclusive")
CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}\.[a-z][a-z0-9_]{0,47}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.+=-]{0,64}$")
RESERVED_FIELDS = frozenset({"probe", "check", "status"})
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_PROBE_ERROR = 0, 1, 2, 3
PROBE_ERROR = "probe_error"

FieldValue = bool | int | str | None


class ReceiptError(ValueError):
    """A record that would break the text-free contract (a probe bug)."""


def _checked_value(name: str, value: object) -> FieldValue:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str) and SAFE_VALUE_RE.fullmatch(value):
        return value
    raise ReceiptError(f"receipt field {name} is not a boolean, integer or safe token")


def exception_class_name(exc: BaseException) -> str:
    """The class name of `exc` as a safe token; never its message."""
    name = type(exc).__name__
    return name if name and SAFE_VALUE_RE.fullmatch(name) else "unnamed"


class Receipt:
    """The records of one probe run, in check order."""

    def __init__(self, probe: str) -> None:
        if not FIELD_RE.fullmatch(probe):
            raise ReceiptError("invalid probe name")
        self.probe = probe
        self.records: list[dict[str, FieldValue]] = []
        self.error_class: str | None = None

    def add(self, check: str, status: str, **fields: object) -> None:
        if not CHECK_ID_RE.fullmatch(check) or not check.startswith(f"{self.probe}."):
            raise ReceiptError("invalid check id")
        if status not in STATUSES:
            raise ReceiptError("invalid status")
        record: dict[str, FieldValue] = {"probe": self.probe, "check": check, "status": status}
        for name, value in fields.items():
            if not FIELD_RE.fullmatch(name) or name in RESERVED_FIELDS:
                raise ReceiptError("invalid receipt field name")
            record[name] = _checked_value(name, value)
        self.records.append(record)

    def status_of(self, check: str) -> str | None:
        for record in self.records:
            if record["check"] == check:
                return str(record["status"])
        return None

    def abort(self, exc: BaseException, checks: Iterable[str] = ()) -> None:
        """The probe stopped on an unexpected exception: keep the finished
        records, record each of `checks` not recorded yet as inconclusive
        `probe_error`, and remember only the exception's class."""
        if self.error_class is None:
            self.error_class = exception_class_name(exc)
        for check in checks:
            if self.status_of(check) is None:
                self.add(check, "inconclusive", reason=PROBE_ERROR)

    def summary(self) -> dict[str, FieldValue]:
        counts = {status: 0 for status in STATUSES}
        for record in self.records:
            counts[str(record["status"])] += 1
        if counts["fail"]:
            overall = "fail"
        elif counts["inconclusive"]:
            overall = "inconclusive"
        else:
            overall = "pass"
        summary: dict[str, FieldValue] = {
            "probe": self.probe,
            "check": f"{self.probe}.summary",
            "status": "error" if self.error_class else overall,
            "schema": SCHEMA_VERSION,
            **counts,
        }
        if self.error_class:
            summary[PROBE_ERROR] = True
            summary["error_class"] = self.error_class
        return summary

    def exit_code(self) -> int:
        if self.error_class:
            return EXIT_PROBE_ERROR
        failed = any(record["status"] == "fail" for record in self.records)
        return EXIT_FAILED if failed else EXIT_OK

    def lines(self) -> list[str]:
        return [_dumps(record) for record in [*self.records, self.summary()]]


def _dumps(record: Mapping[str, FieldValue]) -> str:
    return json.dumps(record, sort_keys=False, separators=(",", ":"))


def write_receipt(receipt: Receipt, stream: IO[str], path: Path | None = None) -> None:
    """Print the receipt to `stream`; with `path`, also write it there as a
    0600 file (replacing an earlier receipt; a symlink is refused)."""
    text = "".join(f"{line}\n" for line in receipt.lines())
    stream.write(text)
    stream.flush()
    if path is None:
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
