"""Trial prototype of the ADR-0007 S2 chat-retention purge (issue #1073). Trial-only.

Not the product mechanism: #1074 owns the scheduled, repository-owned purge.
This file exercises the mechanism the ADR describes against Open WebUI
v0.12.3's SQLite schema, so the trial can prove its effect on stored bytes:

    kubectl -n owui-trial exec -i deploy/openwebui -- python3 - \
        --db /app/backend/data/webui.db --window-seconds 300 < purge.py

It deletes chats whose last update is older than the window, with their
messages, shared snapshots, chat-file links and linked file rows, in one
IMMEDIATE transaction, with secure_delete on; then it checkpoints the WAL and
VACUUMs so the freed pages leave the file. A busy database is retried and
counted, never reported as success. Re-running converges (0 deleted).
Output is counts only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

RETRIES = 5


def _ids(cur, sql, args=()):
    return [row[0] for row in cur.execute(sql, args)]


def _in(ids):
    return ",".join("?" for _ in ids)


def purge_once(db: str, cutoff: int) -> dict:
    con = sqlite3.connect(db, timeout=30, isolation_level=None)
    try:
        con.execute("PRAGMA secure_delete=ON")
        con.execute("PRAGMA foreign_keys=ON")
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        chats = _ids(cur, "SELECT id FROM chat WHERE COALESCE(updated_at, created_at, 0) < ?", (cutoff,))
        counts = {"chats": len(chats), "messages": 0, "shared": 0, "chat_files": 0, "files": 0}
        if chats:
            marks = _in(chats)
            files = _ids(cur, f"SELECT file_id FROM chat_file WHERE chat_id IN ({marks})", chats)
            counts["messages"] = cur.execute(f"DELETE FROM chat_message WHERE chat_id IN ({marks})", chats).rowcount
            counts["shared"] = cur.execute(f"DELETE FROM shared_chat WHERE chat_id IN ({marks})", chats).rowcount
            counts["chat_files"] = cur.execute(f"DELETE FROM chat_file WHERE chat_id IN ({marks})", chats).rowcount
            if files:
                counts["files"] = cur.execute(f"DELETE FROM file WHERE id IN ({_in(files)})", files).rowcount
            cur.execute(f"DELETE FROM chat WHERE id IN ({marks})", chats)
        counts["files"] += cur.execute(
            "DELETE FROM file WHERE COALESCE(updated_at, created_at, 0) < ?", (cutoff,)).rowcount
        cur.execute("COMMIT")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return counts
    finally:
        con.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--window-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    cutoff = int(time.time()) - args.window_seconds
    busy = 0
    for _ in range(RETRIES):
        try:
            counts = purge_once(args.db, cutoff)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                print(json.dumps({"ok": False, "error": "operational"}))
                return 1
            busy += 1
            time.sleep(2)
            continue
        print(json.dumps({"ok": True, "busy_retries": busy, "deleted": counts}, sort_keys=True))
        return 0
    print(json.dumps({"ok": False, "busy_retries": busy}))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
