"""Operator export and import of the feedback store (issue #805, ADR-0005).

`python -m mainframe_rag.feedback.exchange {export,import}` runs inside the
boundary (the feedback pod); there is no HTTP export route. The export is a
canonical JSONL file (format v1): a header, one `answer` line per answer, one
`rating` line per rating (request_id order, ASCII-escaped, sorted keys), and a
trailer with the counts and the sha256 of every preceding byte. The hash is
integrity, not authenticity. The file holds question, answer and comment text
and has no automatic expiry, so it is created `O_EXCL` with mode 0600, never
inside a git work tree and never on a terminal; the operator deletes it.

Export opens the store read-only and releases it before writing (store.
read_snapshot), so it neither writes the live file nor pins a WAL snapshot
that would defer the purge. Import validates the whole file with the live
models before it touches the target, then applies it in one transaction
(store.FeedbackStore.import_records). It needs a store that no service holds,
and an empty one unless `--merge`. Every refusal is a fixed code on stderr and
exit 2; no message or count ever carries row text.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from mainframe_rag.feedback.store import (
    SCHEMA_VERSION,
    AnswerRecord,
    FeedbackStore,
    ImportConflictError,
    Rating,
    StoreSchemaError,
    UnknownAnswerError,
    read_snapshot,
    store_in_use,
)

FORMAT = "feedback-export"
VERSION = 1
EXIT_REFUSED = 2

_ANSWER_KEYS = frozenset({"kind", "request_id", "created_at", "endpoint", "query", "answer",
                          "script", "citations", "supplied", "telemetry"})
_RATING_KEYS = frozenset({"kind", "request_id", "created_at", "rating", "reasons", "comment"})
_HEADER_KEYS = frozenset({"kind", "format", "version", "store_schema", "exported_at",
                          "retention_days"})
_TRAILER_KEYS = frozenset({"kind", "answers", "ratings", "sha256"})


class Refused(Exception):
    """A refusal with a stable code; the code is the whole message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _line(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("ascii") + b"\n"


def build_export(answers: list[dict[str, Any]], ratings: list[dict[str, Any]], *,
                 exported_at: float, retention_days: int) -> bytes:
    body = _line({"kind": "header", "format": FORMAT, "version": VERSION,
                  "store_schema": SCHEMA_VERSION, "exported_at": exported_at,
                  "retention_days": retention_days})
    body += b"".join(_line({"kind": "answer", **a}) for a in answers)
    body += b"".join(_line({"kind": "rating", **r}) for r in ratings)
    trailer = {"kind": "trailer", "answers": len(answers), "ratings": len(ratings),
               "sha256": hashlib.sha256(body).hexdigest()}
    return body + _line(trailer)


def _load(raw: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except ValueError:
        raise Refused("bad_format") from None
    if not isinstance(obj, dict):
        raise Refused("bad_format")
    return obj


def _validate(model: type[BaseModel], fields: dict[str, Any], code: str, n: int) -> BaseModel:
    try:
        return model(**fields)
    except (ValidationError, TypeError, ValueError):
        # Never str(exc): pydantic embeds the offending input value.
        raise Refused(f"{code}_line_{n}") from None


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def parse_export(data: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate a whole export; return (answers, ratings) as store rows."""
    if not data.endswith(b"\n"):
        raise Refused("no_trailer")
    lines = data.split(b"\n")[:-1]
    if len(lines) < 2:
        raise Refused("no_trailer")
    trailer = _load(lines[-1])
    if trailer.get("kind") != "trailer" or set(trailer) != _TRAILER_KEYS:
        raise Refused("no_trailer")
    body = data[: len(data) - len(lines[-1]) - 1]
    digest = hashlib.sha256(body).hexdigest()
    if not isinstance(trailer["sha256"], str) or not hmac.compare_digest(digest, trailer["sha256"]):
        raise Refused("bad_hash")
    header = _load(lines[0])
    if header.get("kind") != "header" or header.get("format") != FORMAT \
            or set(header) != _HEADER_KEYS:
        raise Refused("bad_format")
    if header["version"] != VERSION:
        raise Refused("unsupported_version")
    if header["store_schema"] != SCHEMA_VERSION:
        raise Refused("schema_mismatch")
    answers: list[dict[str, Any]] = []
    ratings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for n, raw in enumerate(lines[1:-1], start=2):
        row = _load(raw)
        kind = row.get("kind")
        if kind not in ("answer", "rating"):
            raise Refused(f"bad_format_line_{n}")
        if set(row) != (_ANSWER_KEYS if kind == "answer" else _RATING_KEYS) \
                or not _number(row["created_at"]):
            raise Refused(f"invalid_{kind}_line_{n}")
        fields = {k: v for k, v in row.items() if k not in ("kind", "created_at")}
        out: dict[str, Any]
        if kind == "answer":
            rec = _validate(AnswerRecord, {**fields, "finalized_at": row["created_at"]},
                            "invalid_answer", n)
            out = {**rec.model_dump(exclude={"finalized_at"}), "created_at": float(row["created_at"])}
        else:
            rat = _validate(Rating, fields, "invalid_rating", n)
            out = {**rat.model_dump(), "created_at": float(row["created_at"])}
        key = (kind, out["request_id"])
        if key in seen:
            raise Refused(f"duplicate_id_line_{n}")
        seen.add(key)
        (answers if kind == "answer" else ratings).append(out)
    if len(answers) != trailer["answers"] or len(ratings) != trailer["ratings"]:
        raise Refused("count_mismatch")
    # A rating may name an answer already in the target; the store decides.
    return answers, ratings


def _inside_git_worktree(path: Path) -> bool:
    return any((p / ".git").exists() for p in (path.parent.resolve(), *path.parent.resolve().parents))


def _write_private(path: Path, data: bytes) -> None:
    if _inside_git_worktree(path):
        raise Refused("output_in_git_worktree")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Refused("output_exists") from None
    except OSError:
        raise Refused("output_unwritable") from None
    try:
        os.fchmod(fd, 0o600)  # a permissive umask cannot widen it; a strict one is kept
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _read_input(source: str) -> bytes:
    if source == "-":
        return sys.stdin.buffer.read()
    try:
        st = os.stat(source)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise Refused("insecure_permissions")
        return Path(source).read_bytes()
    except OSError:
        raise Refused("input_unreadable") from None


def run_export(db: Path, out: str, *, retention_days: int, clock: Callable[[], float]) -> None:
    if out == "-" and sys.stdout.isatty():
        raise Refused("output_is_terminal")
    if out != "-" and _inside_git_worktree(Path(out)):
        raise Refused("output_in_git_worktree")
    try:
        answers, ratings = read_snapshot(db)
    except StoreSchemaError:
        raise Refused("schema_mismatch") from None
    except (sqlite3.Error, OSError):
        raise Refused("store_unreadable") from None
    # The store is released here; only now is output written.
    data = build_export(answers, ratings, exported_at=clock(), retention_days=retention_days)
    if out == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        _write_private(Path(out), data)


def run_import(src: str, db: Path, *, merge: bool, keep_existing: bool, retention_days: int,
               clock: Callable[[], float]) -> dict[str, int]:
    answers, ratings = parse_export(_read_input(src))
    if db.exists() and store_in_use(db):
        raise Refused("store_in_use")
    try:
        store = FeedbackStore(db, clock=clock)
    except StoreSchemaError:
        raise Refused("schema_mismatch") from None
    except (sqlite3.Error, OSError):
        raise Refused("store_unwritable") from None
    try:
        if not merge and not store.is_empty():
            raise Refused("target_not_empty")
        try:
            return store.import_records(answers, ratings, keep_existing=keep_existing,
                                        retention_days=retention_days)
        except ImportConflictError as e:
            raise Refused(f"conflict_{e.counts['conflict']}") from None
        except UnknownAnswerError:
            raise Refused("dangling_rating") from None
    finally:
        store.close()


def _settings_default(name: str) -> Any:
    from mainframe_rag.config import load_settings

    return getattr(load_settings(), name)


def main(argv: list[str] | None = None, *, clock: Callable[[], float] = time.time) -> int:
    parser = argparse.ArgumentParser(prog="python -m mainframe_rag.feedback.exchange",
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="write the store as a canonical JSONL snapshot")
    exp.add_argument("--db", help="store file (default FEEDBACK_STORE_PATH)")
    exp.add_argument("--out", required=True, help="new file (mode 0600) or - for stdout")
    exp.add_argument("--retention-days", type=int,
                     help="recorded in the header (default FEEDBACK_RETENTION_DAYS)")
    imp = sub.add_parser("import", help="restore an export; the service must be stopped")
    imp.add_argument("--in", dest="src", required=True, help="export file or - for stdin")
    imp.add_argument("--db", help="store file (default FEEDBACK_STORE_PATH)")
    imp.add_argument("--merge", action="store_true", help="allow a populated target")
    imp.add_argument("--keep-existing", action="store_true",
                     help="on conflicts keep the target's rows instead of refusing")
    imp.add_argument("--retention-days", type=int,
                     help="window for the `expired` count (default FEEDBACK_RETENTION_DAYS)")
    args = parser.parse_args(argv)
    try:
        db = Path(args.db or _settings_default("feedback_store_path"))
        retention = args.retention_days or _settings_default("feedback_retention_days")
        if args.command == "export":
            run_export(db, args.out, retention_days=retention, clock=clock)
        else:
            counts = run_import(args.src, db, merge=args.merge, keep_existing=args.keep_existing,
                                retention_days=retention, clock=clock)
            print(json.dumps(counts, sort_keys=True))
    except Refused as e:
        print(f"feedback-exchange: refused: {e.code}", file=sys.stderr)
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
