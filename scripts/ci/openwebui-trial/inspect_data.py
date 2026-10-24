"""Open WebUI Kind trial (issue #1073): inspect DATA_DIR for vector-store content.

    kubectl -n owui-trial exec -i deploy/openwebui -- python3 - [CANARY] < inspect_data.py

Prints names, sizes and counts only: every file under DATA_DIR, any
vector-store directory or file (vector_db/, chroma*, *.parquet, *.bin, extra
SQLite files), uploads, the webui.db tables with row counts, the rows of the
tables that would hold retrieval content, and whether a canary string occurs
in any file's bytes (counts only, never the text). Exit 1 on vector content.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

DATA = os.environ.get("DATA_DIR", "/app/backend/data")
CONTENT_TABLES = ("document_chunk", "knowledge", "knowledge_file", "file", "memory", "chat_file", "note")


def main(argv: list[str]) -> int:
    canary = argv[0].encode() if argv else None
    files, vector_hits, canary_files = [], [], []
    for root, dirs, names in os.walk(DATA):
        rel = os.path.relpath(root, DATA)
        for d in dirs:
            if d in ("vector_db", "chroma", "qdrant", "milvus") or d.startswith("chroma"):
                vector_hits.append(os.path.join(rel, d))
        for name in names:
            path = os.path.join(root, name)
            relpath = os.path.normpath(os.path.join(rel, name))
            size = os.path.getsize(path)
            files.append([relpath, size])
            lowered = name.lower()
            if ("chroma" in lowered or lowered.endswith((".parquet", ".bin", ".hnsw"))
                    or (lowered.endswith((".sqlite3", ".sqlite")) and relpath != "webui.db")):
                vector_hits.append(relpath)
            if canary:
                with open(path, "rb") as fh:
                    if canary in fh.read():
                        canary_files.append(relpath)
    uploads = [f for f in files if f[0].startswith("uploads" + os.sep)]
    con = sqlite3.connect(f"file:{DATA}/webui.db?mode=ro", uri=True)
    tables = {}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        tables[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    con.close()
    vectorish = sorted(t for t in tables if any(k in t for k in ("vector", "embedding", "chunk", "collection")))
    content_rows = {t: tables[t] for t in CONTENT_TABLES if t in tables}
    report = {
        "data_dir_files": sorted(files),
        "vector_store_paths": sorted(vector_hits),
        "uploads": len(uploads),
        "tables": tables,
        "vector_like_tables": {t: tables[t] for t in vectorish},
        "retrieval_content_rows": content_rows,
        "canary_files": sorted(canary_files) if canary else None,
    }
    print(json.dumps(report, sort_keys=True))
    bad = vector_hits or uploads or any(tables[t] for t in vectorish) or any(content_rows.values())
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
