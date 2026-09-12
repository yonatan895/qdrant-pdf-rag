"""SQLite persistence layer for UI chat sessions and incident history.

Stores named sessions, dialogue history, citations, and incident dump contexts.
Supports markdown export for incident ticketing and handovers.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


def _get_connection(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str) -> None:
    """Initialize the SQLite schema if not present."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _get_connection(p) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                citations TEXT,
                hits TEXT,
                splunk_context TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)"
        )
        conn.commit()


def create_session(db_path: Path | str, title: str = "New Incident") -> str:
    """Create a new chat session and return its id."""
    session_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, title, now, now),
        )
        conn.commit()
    return session_id


def list_sessions(db_path: Path | str) -> list[dict[str, Any]]:
    """List all chat sessions ordered by most recently updated."""
    with _get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]


def get_session_messages(db_path: Path | str, session_id: str) -> list[dict[str, Any]]:
    """Retrieve all messages for a given session."""
    with _get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, session_id, role, content, created_at, citations, hits, splunk_context
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        )
        records = []
        for row in cur.fetchall():
            rec = dict(row)
            if rec.get("citations"):
                try:
                    rec["citations"] = json.loads(rec["citations"])
                except (json.JSONDecodeError, TypeError):
                    rec["citations"] = []
            else:
                rec["citations"] = []

            if rec.get("hits"):
                try:
                    rec["hits"] = json.loads(rec["hits"])
                except (json.JSONDecodeError, TypeError):
                    rec["hits"] = []
            else:
                rec["hits"] = []
            records.append(rec)
        return records


def add_message(
    db_path: Path | str,
    session_id: str,
    role: str,
    content: str,
    citations: list[str] | None = None,
    hits: list[dict[str, Any]] | None = None,
    splunk_context: str | None = None,
) -> int:
    """Add a message to a session and update the session updated_at timestamp."""
    now = time.time()
    citations_json = json.dumps(citations) if citations else None
    hits_json = json.dumps(hits) if hits else None

    with _get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (session_id, role, content, created_at, citations, hits, splunk_context)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content, now, citations_json, hits_json, splunk_context),
        )
        msg_id = cur.lastrowid or 0

        # Auto-update session title if it's the first user message and default title
        cur_title = conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if cur_title and cur_title["title"] == "New Incident" and role == "user":
            first_line = content.strip().split("\n")[0][:45]
            conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (first_line, now, session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        conn.commit()
    return msg_id


def delete_session(db_path: Path | str, session_id: str) -> None:
    """Delete a session and all its associated messages."""
    with _get_connection(db_path) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()


def export_markdown(db_path: Path | str, session_id: str) -> str:
    """Generate a clean markdown report of the incident conversation."""
    with _get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT title, created_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return "# Session Not Found"
        title = row["title"]
        session_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(row["created_at"]))

    messages = get_session_messages(db_path, session_id)
    lines = [
        f"# Incident Analysis: {title}",
        f"*Session ID: `{session_id}` | Date: {session_time}*",
        "",
        "---",
        "",
    ]

    for m in messages:
        role_label = "Operator" if m["role"] == "user" else "Mainframe Copilot"
        msg_time = time.strftime("%H:%M:%S", time.gmtime(m["created_at"]))
        lines.append(f"### {role_label} ({msg_time})")

        if m.get("splunk_context"):
            lines.append("```text")
            lines.append("--- Attached Incident Context (JES Spool / SYSLOG / Abend Dump) ---")
            lines.append(m["splunk_context"])
            lines.append("```")
            lines.append("")

        lines.append(m["content"])
        lines.append("")

        if m.get("citations"):
            lines.append("**Verified Citations:**")
            for c in m["citations"]:
                lines.append(f"- {c}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)
