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
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 1

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
RATER_MAX_CHARS = 256

Scalar = int | float | str | bool | None


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
    """One human rating of a recorded answer."""

    request_id: str
    rating: RatingValue
    reasons: list[Reason] = Field(default_factory=list, max_length=len(REASONS))
    comment: str = Field(default="", max_length=COMMENT_MAX_CHARS)
    rater: str | None = Field(default=None, max_length=RATER_MAX_CHARS)

    @field_validator("request_id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _request_id(v)

    @field_validator("reasons")
    @classmethod
    def _distinct(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("reasons must be distinct")
        return v


class UnknownAnswerError(LookupError):
    """A rating named a request_id with no recorded answer."""


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
    comment TEXT NOT NULL,
    rater TEXT
);
CREATE INDEX IF NOT EXISTS answers_created ON answers(created_at);
"""


class FeedbackStore:
    """Thread-safe single-connection store; the owning process is the only
    writer (WAL lets a read-only export run alongside)."""

    def __init__(self, path: Path, *, clock: Any = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._db.executescript(_SCHEMA)
            row = self._db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None:
                self._db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            elif row[0] != str(SCHEMA_VERSION):
                raise RuntimeError("feedback store schema is not supported by this version")

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
                "INSERT INTO ratings (request_id, created_at, rating, reasons, comment, rater)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (request_id) DO UPDATE SET"
                " created_at = excluded.created_at, rating = excluded.rating,"
                " reasons = excluded.reasons, comment = excluded.comment, rater = excluded.rater",
                (rating.request_id, self._clock(), rating.rating, json.dumps(rating.reasons),
                 rating.comment, rating.rater),
            )
            row = self._db.execute(
                "SELECT id FROM ratings WHERE request_id = ?", (rating.request_id,)
            ).fetchone()
            return int(row[0])

    def answer(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT request_id, created_at, endpoint, query, answer, script, citations, supplied,"
                " telemetry FROM answers WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        keys = ("request_id", "created_at", "endpoint", "query", "answer", "script", "citations",
                "supplied", "telemetry")
        out = dict(zip(keys, row, strict=True))
        out["citations"] = json.loads(out["citations"])
        out["supplied"] = json.loads(out["supplied"])
        out["telemetry"] = json.loads(out["telemetry"])
        return out

    def rating(self, request_id: str) -> dict[str, Any] | None:
        """The answer's single rating (latest submission), or None."""
        with self._lock:
            r = self._db.execute(
                "SELECT id, created_at, rating, reasons, comment, rater FROM ratings"
                " WHERE request_id = ?", (request_id,)
            ).fetchone()
        if r is None:
            return None
        return {"id": r[0], "created_at": r[1], "rating": r[2], "reasons": json.loads(r[3]),
                "comment": r[4], "rater": r[5]}

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

    def purge(self, *, retention_days: int) -> dict[str, int]:
        """Delete answers (and, by cascade, their ratings) older than the
        retention window. Explicit operator action, never implicit."""
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        cutoff = self._clock() - retention_days * 86400
        with self._lock:
            ratings = self._db.execute(
                "SELECT COUNT(*) FROM ratings WHERE request_id IN"
                " (SELECT request_id FROM answers WHERE created_at < ?)", (cutoff,)
            ).fetchone()[0]
            answers = self._db.execute("DELETE FROM answers WHERE created_at < ?", (cutoff,)).rowcount
        return {"answers": int(answers), "ratings": int(ratings)}
