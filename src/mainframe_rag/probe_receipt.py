"""Text-free receipts for the read-only site probes (#1343).

A probe (the z/OS bridge's `--site-probe`, the GitLab gateway's
`--site-probe`, `sign_vault.py vault-probe`) records one JSON line per
check, then a summary line. A record holds the check id, its status
(`pass`, `fail` or `inconclusive`) and facts that are booleans, integers,
`null` or short tokens: reply codes, versions, fixed reason codes, codec
names. Every string value must match `SAFE_VALUE_RE` (no spaces, slashes,
`@`, `:` or quotes), so a dataset line, a spool record, an issue note, a URL
or a user id cannot be stored even by mistake; probes reduce server text to
such tokens before recording it, and never record host names, user ids,
dataset names, job ids or tokens. `scripts/gap/sign_vault.py` runs as a
single stdlib file and carries a copy of these rules (a test pins the two
equal).

Exit code: 1 when any check failed, else 0 (an inconclusive check is not a
failure: it names a fact a human still has to judge or a target the operator
did not name).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import IO

SCHEMA_VERSION = 1
STATUSES: tuple[str, ...] = ("pass", "fail", "inconclusive")
CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}\.[a-z][a-z0-9_]{0,47}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.+=-]{0,48}$")
RESERVED_FIELDS = frozenset({"probe", "check", "status"})

FieldValue = bool | int | str | None


class ReceiptError(ValueError):
    """A record that would break the text-free contract (a probe bug)."""


def _checked_value(name: str, value: object) -> FieldValue:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str) and SAFE_VALUE_RE.fullmatch(value):
        return value
    raise ReceiptError(f"receipt field {name} is not a boolean, integer or safe token")


class Receipt:
    """The records of one probe run, in check order."""

    def __init__(self, probe: str) -> None:
        if not FIELD_RE.fullmatch(probe):
            raise ReceiptError("invalid probe name")
        self.probe = probe
        self.records: list[dict[str, FieldValue]] = []

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
        return {
            "probe": self.probe,
            "check": f"{self.probe}.summary",
            "status": overall,
            "schema": SCHEMA_VERSION,
            **counts,
        }

    def exit_code(self) -> int:
        return 1 if any(record["status"] == "fail" for record in self.records) else 0

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
