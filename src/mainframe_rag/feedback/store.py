"""Feedback store (issue #780): SQLite in WAL mode, one process the sole writer.

Two tables. `answers` holds one row per finalized answer, keyed by the agent's
server-minted request_id (12 lowercase hex); the first record wins, so a
replayed or duplicated delivery cannot rewrite what was rated. `ratings`
holds at most one human rating per answer (re-rating replaces it), attached to
an existing answer row only: a rating can never carry or invent answer text. Question and answer text live here and
nowhere else (never in logs or traces); the volume is the protected store.

SQL stays portable (standard `ON CONFLICT DO NOTHING`, JSON as text) and the
public methods are the whole interface, so a later Postgres backend (#780
decision: Crunchy, once proven) is a backend swap, not a redesign.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from urllib.parse import quote

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5000  # sqlite3.connect's default; restored after the purge checkpoint

# The one owner of the feedback vocabularies (issue #780): metrics labels, the
# console route and console.js derive from these (a test pins console.js).
RatingValue = Literal["good", "neutral", "bad"]
Reason = Literal["wrong", "incomplete", "missing_or_wrong_citation", "should_have_declined",
                 "wrongly_declined", "too_slow", "other"]
Endpoint = Literal["answer", "answer_stream", "chat", "chat_stream", "console"]
RATINGS: tuple[str, ...] = get_args(RatingValue)
REASONS: tuple[str, ...] = get_args(Reason)
ENDPOINTS: tuple[str, ...] = get_args(Endpoint)
REQUEST_ID_PATTERN = r"[0-9a-f]{12}"  # the agent's server-minted request id
REQUEST_ID_RE = re.compile(REQUEST_ID_PATTERN)
COMMENT_MAX_CHARS = 2000

Scalar = int | float | str | bool | None


def _distinct(reasons: list[str]) -> list[str]:
    if len(set(reasons)) != len(reasons):
        raise ValueError("reasons must be distinct")
    return reasons


# Distinct reason codes; the console request and the stored rating share it,
# so one cannot accept what the other refuses.
Reasons = Annotated[list[Reason], AfterValidator(_distinct), Field(max_length=len(REASONS))]


def _request_id(value: str) -> str:
    if not REQUEST_ID_RE.fullmatch(value):
        raise ValueError("request_id must be the agent's 12-hex request id")
    return value


class AnswerRecord(BaseModel):
    """One finalized answer as the agent produced it."""

    request_id: str
    endpoint: Endpoint
    query: str
    answer: str
    script: str | None = None
    citations: list[str] = Field(default_factory=list)
    # Citations of the excerpts actually supplied to the model (#635 projection),
    # so analysis can tell a retrieval miss from a reader miss.
    supplied: list[str] = Field(default_factory=list)
    # Numeric/label telemetry only (the agent's finalized log fields plus
    # effort and identities): never text, so the shape stays log-safe.
    telemetry: dict[str, Scalar] = Field(default_factory=dict)
    # When the agent finalized the answer (epoch seconds, agent clock); the
    # retention window counts from it. None falls back to receipt time.
    finalized_at: float | None = None

    @field_validator("request_id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _request_id(v)


class Rating(BaseModel):
    """One human rating of a recorded answer. No rater identity: attribution
    waits for proven header provenance (PR B), so an extra field is refused."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    rating: RatingValue
    reasons: Reasons = Field(default_factory=list)
    comment: str = Field(default="", max_length=COMMENT_MAX_CHARS)

    @field_validator("request_id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _request_id(v)


class UnknownAnswerError(LookupError):
    """A rating named a request_id with no recorded answer."""


class StoreSchemaError(RuntimeError):
    """The store file was written by an unsupported schema version."""


class ImportConflictError(Exception):
    """An import met rows that differ from the target's; nothing was written.
    `counts` is the plan (numbers only, never row content)."""

    def __init__(self, counts: dict[str, int]) -> None:
        super().__init__("import conflicts")
        self.counts = counts


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS answers (
    request_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    endpoint TEXT NOT NULL,
    query TEXT NOT NULL,
    answer TEXT NOT NULL,
    script TEXT,
    citations TEXT NOT NULL,
    supplied TEXT NOT NULL,
    telemetry TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE REFERENCES answers(request_id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    rating TEXT NOT NULL,
    reasons TEXT NOT NULL,
    comment TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS answers_created ON answers(created_at);
"""


_ANSWER_COLUMNS = ("request_id, created_at, endpoint, query, answer, script, citations, supplied,"
                   " telemetry")
_RATING_COLUMNS = "request_id, created_at, rating, reasons, comment"


def _answer_row(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = ("request_id", "created_at", "endpoint", "query", "answer", "script", "citations",
            "supplied", "telemetry")
    out = dict(zip(keys, row, strict=True))
    for key in ("citations", "supplied", "telemetry"):
        out[key] = json.loads(out[key])
    return out


def _rating_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"request_id": row[0], "created_at": row[1], "rating": row[2],
            "reasons": json.loads(row[3]), "comment": row[4]}


def read_snapshot(path: Path, *, connect: Any = None) -> tuple[list[dict[str, Any]],
                                                                         list[dict[str, Any]]]:
    """Every answer and rating as one consistent snapshot, for export (#805).

    Opens the file read-only (`mode=ro`: it cannot alter a live store, take
    the write lock or run the constructor's WAL/schema writes), reads inside a
    single transaction into memory, and closes the connection before returning,
    so no read snapshot outlives the call and pins the WAL (the purge needs
    that, ADR-0005). Rows are ordered by request_id; ratings carry no local id."""
    db = (connect or sqlite3.connect)(f"file:{quote(str(path))}?mode=ro", uri=True, isolation_level=None,
                 timeout=_BUSY_TIMEOUT_MS / 1000)
    try:
        db.execute("BEGIN")
        row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row is None or row[0] != str(SCHEMA_VERSION):
            raise StoreSchemaError("feedback store schema is not supported by this version")
        answers = [_answer_row(r) for r in db.execute(
            f"SELECT {_ANSWER_COLUMNS} FROM answers ORDER BY request_id").fetchall()]
        ratings = [_rating_row(r) for r in db.execute(
            f"SELECT {_RATING_COLUMNS} FROM ratings ORDER BY request_id").fetchall()]
        db.execute("COMMIT")
    finally:
        db.close()
    return answers, ratings


def store_in_use(path: Path) -> bool:
    """True when another connection holds the store file right now.

    Probes for an exclusive lock without waiting; it writes nothing. A WAL
    database keeps a lock for as long as a connection is open, so a running
    service is detected even when idle (no transaction). Limits: it is a
    point-in-time check, not a lock held during the import; a service started
    after the probe is not seen; and file locks are not reliable across hosts
    or on NFS. It supports the single-writer rule (stop the service first,
    docs/install_and_ops.md); it is not a replacement for it (#805)."""
    db = sqlite3.connect(str(path), isolation_level=None, timeout=0)
    try:
        db.execute("PRAGMA locking_mode=EXCLUSIVE")
        db.execute("SELECT count(*) FROM sqlite_master").fetchall()
        db.execute("BEGIN EXCLUSIVE")
        db.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        db.close()


class FeedbackStore:
    """Thread-safe single-connection store; the owning process is the only
    writer (WAL lets a read-only export run alongside)."""

    def __init__(self, path: Path, *, clock: Any = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None,
                                   timeout=_BUSY_TIMEOUT_MS / 1000)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA synchronous=NORMAL")
        # Deleted or replaced content is overwritten, not left in free pages:
        # retention (ADR-0005) must hold on disk, not only for reads.
        self._db.execute("PRAGMA secure_delete=ON")
        with self._lock:
            self._db.executescript(_SCHEMA)
            row = self._db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None:
                self._db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            elif row[0] != str(SCHEMA_VERSION):
                raise StoreSchemaError("feedback store schema is not supported by this version")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record_answer(self, record: AnswerRecord) -> bool:
        """Insert once. Returns False when the request_id is already recorded
        (the first record wins; a rated answer is never rewritten)."""
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO answers (request_id, created_at, endpoint, query, answer, script,"
                " citations, supplied, telemetry) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (record.request_id,
                 record.finalized_at if record.finalized_at is not None else self._clock(),
                 record.endpoint, record.query, record.answer, record.script,
                 json.dumps(record.citations), json.dumps(record.supplied),
                 json.dumps(record.telemetry, sort_keys=True)),
            )
            return cursor.rowcount == 1

    def add_rating(self, rating: Rating) -> int:
        """Attach the answer's rating; refuses unknown request ids. One rating
        per answer: a resend converges and a change replaces it (latest wins),
        keeping the same id, so retries and double submits are idempotent."""
        with self._lock:
            known = self._db.execute(
                "SELECT 1 FROM answers WHERE request_id = ?", (rating.request_id,)
            ).fetchone()
            if known is None:
                raise UnknownAnswerError(rating.request_id)
            self._db.execute(
                "INSERT INTO ratings (request_id, created_at, rating, reasons, comment)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (request_id) DO UPDATE SET"
                " created_at = excluded.created_at, rating = excluded.rating,"
                " reasons = excluded.reasons, comment = excluded.comment",
                (rating.request_id, self._clock(), rating.rating, json.dumps(rating.reasons),
                 rating.comment),
            )
            row = self._db.execute(
                "SELECT id FROM ratings WHERE request_id = ?", (rating.request_id,)
            ).fetchone()
            return int(row[0])

    def answer(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._answer_unlocked(request_id)

    def _answer_unlocked(self, request_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            f"SELECT {_ANSWER_COLUMNS} FROM answers WHERE request_id = ?", (request_id,)
        ).fetchone()
        return None if row is None else _answer_row(row)

    def rating(self, request_id: str) -> dict[str, Any] | None:
        """The answer's single rating (latest submission), or None."""
        with self._lock:
            r = self._db.execute(
                "SELECT id, created_at, rating, reasons, comment FROM ratings"
                " WHERE request_id = ?", (request_id,)
            ).fetchone()
        if r is None:
            return None
        return {"id": r[0], "created_at": r[1], "rating": r[2], "reasons": json.loads(r[3]),
                "comment": r[4]}

    def is_empty(self) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM answers LIMIT 1").fetchone() is None

    def import_records(self, answers: list[dict[str, Any]], ratings: list[dict[str, Any]], *,
                       keep_existing: bool = False, retention_days: int = 180) -> dict[str, int]:
        """Restore validated export rows in one transaction (#805), preserving
        created_at. Mirrors the live rules: an answer already present is never
        rewritten (identical is a duplicate, different is a conflict); a rating
        that differs is replaced when newer, kept when older, and a conflict at
        equal created_at. Any conflict refuses the whole import unless
        `keep_existing`, which keeps the target's rows and skips the incoming
        ones (ratings of a conflicting answer included). A rating must name an
        answer in the target or the batch. All or nothing; the result is
        counts only. `expired` counts inserted answers already past retention
        (the next purge removes them)."""
        counts = {"inserted": 0, "duplicate": 0, "conflict": 0, "superseded": 0, "expired": 0}
        cutoff = self._clock() - retention_days * 86400
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                skipped: set[str] = set()
                for a in answers:
                    existing = self._answer_unlocked(a["request_id"])
                    if existing is None:
                        self._db.execute(
                            "INSERT INTO answers (request_id, created_at, endpoint, query, answer,"
                            " script, citations, supplied, telemetry) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (a["request_id"], a["created_at"], a["endpoint"], a["query"], a["answer"],
                             a["script"], json.dumps(a["citations"]), json.dumps(a["supplied"]),
                             json.dumps(a["telemetry"], sort_keys=True)))
                        counts["inserted"] += 1
                        counts["expired"] += a["created_at"] < cutoff
                    elif existing == a:
                        counts["duplicate"] += 1
                    else:
                        counts["conflict"] += 1
                        skipped.add(a["request_id"])
                for r in ratings:
                    if r["request_id"] in skipped:
                        counts["conflict"] += 1
                        continue
                    if self._db.execute("SELECT 1 FROM answers WHERE request_id = ?",
                                        (r["request_id"],)).fetchone() is None:
                        raise UnknownAnswerError(r["request_id"])
                    row = self._db.execute(
                        f"SELECT {_RATING_COLUMNS} FROM ratings WHERE request_id = ?",
                        (r["request_id"],)).fetchone()
                    values = (r["created_at"], r["rating"], json.dumps(r["reasons"]), r["comment"])
                    if row is None:
                        self._db.execute(
                            "INSERT INTO ratings (request_id, created_at, rating, reasons, comment)"
                            " VALUES (?, ?, ?, ?, ?)", (r["request_id"], *values))
                        counts["inserted"] += 1
                    elif _rating_row(row) == r:
                        counts["duplicate"] += 1
                    elif r["created_at"] > row[1]:
                        self._db.execute(
                            "UPDATE ratings SET created_at = ?, rating = ?, reasons = ?, comment = ?"
                            " WHERE request_id = ?", (*values, r["request_id"]))
                        counts["inserted"] += 1
                    elif r["created_at"] < row[1]:
                        counts["superseded"] += 1
                    else:
                        counts["conflict"] += 1
                if counts["conflict"] and not keep_existing:
                    raise ImportConflictError(counts)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        return counts

    def answer_labels(self, request_id: str) -> tuple[str, str | None] | None:
        """Bounded labels of a recorded answer (endpoint, verification state)
        for metrics, without reading its text."""
        with self._lock:
            row = self._db.execute(
                "SELECT endpoint, telemetry FROM answers WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        state = json.loads(row[1]).get("verification_state")
        return row[0], state if isinstance(state, str) else None

    def purge(self, *, retention_days: int) -> dict[str, int | bool]:
        """Delete answers (and, by cascade, their ratings) older than the
        retention window, then fold the WAL into the zeroed file and truncate
        it. Explicit operator action, never implicit, and idempotent: a purge
        that met a concurrent reader reports `wal_truncated: false` at once
        (no busy wait that would stall writers) and the next purge completes."""
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        cutoff = self._clock() - retention_days * 86400
        with self._lock:
            ratings = self._db.execute(
                "SELECT COUNT(*) FROM ratings WHERE request_id IN"
                " (SELECT request_id FROM answers WHERE created_at < ?)", (cutoff,)
            ).fetchone()[0]
            answers = self._db.execute("DELETE FROM answers WHERE created_at < ?", (cutoff,)).rowcount
            # The WAL still holds pre-delete page images; a held read snapshot
            # makes TRUNCATE busy, so do not wait (writers share this lock).
            self._db.execute("PRAGMA busy_timeout=0")
            try:
                busy = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            finally:
                self._db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return {"answers": int(answers), "ratings": int(ratings), "wal_truncated": busy == 0}
