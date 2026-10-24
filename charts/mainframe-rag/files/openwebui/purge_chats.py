"""Enforce the Open WebUI chat retention window (ADR-0007 S2, #1074).

Runs as the chat-purge sidecar of the Open WebUI pod, on the same RWO claim
as the SQLite database (the ADR-0005/#804 purge pattern):

  purge_chats.py --days N [--once] [--interval-seconds S] [--retry-seconds S]

Each run deletes, in one transaction, every chat whose last update is older
than N days, the shared snapshots of those chats (and stale snapshots), the
rows of every other table that reference a deleted chat by `chat_id`, and
file records older than the window with their uploaded files. Deletion runs
with secure_delete on; the WAL is then truncated and the database vacuumed so
the deleted text is gone from the files on the claim.

Fail closed and counted, never reported as success: an unexpected schema (a
pin import changed it) or a timestamp that is not epoch seconds refuses to
delete anything; a busy database or a failed vacuum is a failed run, retried
after --retry-seconds. Each run writes counters to .mainframe-rag-purge.json
next to the database and logs one fixed line of counts. Chat content, titles
and identifiers are never logged. The table layout is Open WebUI v0.12.3's,
pending #1073 qualification and re-qualified on every pin import (P1).
Stdlib only: the image's own interpreter runs it.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DAY_S = 86400
STATUS_NAME = ".mainframe-rag-purge.json"
SHARED_PREFIX = "shared-"
# Epoch seconds stay below this for centuries; milliseconds or nanoseconds do not.
MAX_EPOCH_S = 10**11


class PurgeError(Exception):
    """A failed run with a fixed, content-free reason code."""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({_quote(table)})")}


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [row[0] for row in rows]


def _check_epoch(conn: sqlite3.Connection, table: str) -> None:
    (latest,) = conn.execute(f"SELECT max(updated_at) FROM {_quote(table)}").fetchone()
    if latest is not None and (not isinstance(latest, int) or latest > MAX_EPOCH_S):
        raise PurgeError("timestamp-unit")


def purge(db: Path, data_dir: Path, days: int, now: float, busy_timeout_s: float = 30.0) -> dict:
    """One retention run. Returns counts; raises PurgeError on any failure."""
    if days < 1:
        raise PurgeError("bad-window")
    if not db.exists():
        # Open WebUI has not created its database yet: nothing is stored.
        return {"chats": 0, "snapshots": 0, "linked_rows": 0, "files": 0}
    cutoff = int(now) - days * DAY_S
    uploads = (data_dir / "uploads").resolve()
    try:
        conn = sqlite3.connect(str(db), timeout=busy_timeout_s, isolation_level=None)
    except sqlite3.Error:
        raise PurgeError("open") from None
    removed: list[Path] = []
    try:
        tables = _tables(conn)
        if "chat" not in tables or not {"id", "user_id", "updated_at"} <= _columns(conn, "chat"):
            raise PurgeError("schema")
        _check_epoch(conn, "chat")
        has_files = "file" in tables and {"id", "path", "updated_at"} <= _columns(conn, "file")
        if has_files:
            _check_epoch(conn, "file")
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TEMP TABLE purge_chat(id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO purge_chat SELECT id FROM chat WHERE updated_at < ? "
                     "AND user_id NOT LIKE ?", (cutoff, SHARED_PREFIX + "%"))
        (chats,) = conn.execute("SELECT count(*) FROM purge_chat").fetchone()
        # Shared snapshots are chat rows owned by "shared-<chat id>": those of a
        # purged chat go with it, and a snapshot older than the window goes too.
        conn.execute("INSERT OR IGNORE INTO purge_chat SELECT id FROM chat "
                     "WHERE user_id LIKE ? AND (updated_at < ? OR substr(user_id, ?) IN "
                     "(SELECT id FROM purge_chat))",
                     (SHARED_PREFIX + "%", cutoff, len(SHARED_PREFIX) + 1))
        (total,) = conn.execute("SELECT count(*) FROM purge_chat").fetchone()
        linked = 0
        for table in tables:
            if table != "chat" and "chat_id" in _columns(conn, table):
                cur = conn.execute(f"DELETE FROM {_quote(table)} WHERE chat_id IN "
                                   "(SELECT id FROM purge_chat)")
                linked += max(cur.rowcount, 0)
        conn.execute("DELETE FROM chat WHERE id IN (SELECT id FROM purge_chat)")
        files = 0
        if has_files:
            conn.execute("CREATE TEMP TABLE purge_file(id TEXT PRIMARY KEY, path TEXT)")
            conn.execute("INSERT INTO purge_file SELECT id, path FROM file WHERE updated_at < ?",
                         (cutoff,))
            for (path,) in conn.execute("SELECT path FROM purge_file"):
                if isinstance(path, str) and path:
                    candidate = Path(path).resolve()
                    if candidate.is_relative_to(uploads):
                        removed.append(candidate)
            for table in tables:
                if table != "file" and "file_id" in _columns(conn, table):
                    cur = conn.execute(f"DELETE FROM {_quote(table)} WHERE file_id IN "
                                       "(SELECT id FROM purge_file)")
                    linked += max(cur.rowcount, 0)
            cur = conn.execute("DELETE FROM file WHERE id IN (SELECT id FROM purge_file)")
            files = max(cur.rowcount, 0)
        conn.execute("COMMIT")
        for path in removed:
            path.unlink(missing_ok=True)
        if total or files or linked:
            # Freed pages are zeroed (secure_delete); the WAL still holds the old
            # pages until it is checkpointed and truncated, and VACUUM rebuilds
            # the file. Either failing is a failed run: the text may remain.
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.execute("VACUUM")
            busy_after = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if (busy and busy[0]) or (busy_after and busy_after[0]):
                raise PurgeError("checkpoint-busy")
        return {"chats": chats, "snapshots": total - chats, "linked_rows": linked, "files": files}
    except sqlite3.OperationalError as exc:
        code = "busy" if "locked" in str(exc) or "busy" in str(exc) else "sqlite"
        raise PurgeError(code) from None
    except (sqlite3.Error, OSError):
        raise PurgeError("sqlite") from None
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def _write_status(path: Path, status: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def run_once(db: Path, data_dir: Path, days: int, status: dict, now: float) -> bool:
    status["last_attempt"] = int(now)
    try:
        counts = purge(db, data_dir, days, now)
    except PurgeError as exc:
        status["consecutive_failures"] = status.get("consecutive_failures", 0) + 1
        status["total_failures"] = status.get("total_failures", 0) + 1
        status["last_error"] = str(exc)
        print(f"chat purge FAILED ({exc}); consecutive failures "
              f"{status['consecutive_failures']}", file=sys.stderr, flush=True)
        ok = False
    else:
        status.update(consecutive_failures=0, last_success=int(now), last_counts=counts,
                      last_error="")
        print("chat purge ok: window {} days, chats {chats}, snapshots {snapshots}, "
              "linked rows {linked_rows}, files {files}".format(days, **counts), flush=True)
        ok = True
    status["window_days"] = days
    try:
        _write_status(data_dir / STATUS_NAME, status)
    except OSError:
        print("chat purge status file could not be written", file=sys.stderr, flush=True)
    return ok


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Open WebUI chat retention purge")
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("/app/backend/data"))
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--interval-seconds", type=int, default=3600)
    parser.add_argument("--retry-seconds", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 3650 or args.interval_seconds < 1 or args.retry_seconds < 1:
        print("chat purge refused: --days must be 1..3650 and intervals positive", file=sys.stderr)
        return 2
    db = args.db or args.data_dir / "webui.db"
    status: dict = {}
    while True:
        ok = run_once(db, args.data_dir, args.days, status, time.time())
        if args.once:
            return 0 if ok else 1
        time.sleep(args.interval_seconds if ok else args.retry_seconds)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
