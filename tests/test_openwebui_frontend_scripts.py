"""Open WebUI chart helper scripts (#1074): Pipe installer and retention purge.

Both scripts ship in the chart ConfigMap and run with the Open WebUI image's
own interpreter, so they are loaded here from the chart files. The installer
runs against a real local HTTPS server that stands in for Open WebUI's
Functions API; the purge runs against a real SQLite file with the table
layout it guards (synthetic content only).
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sqlite3
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FILES = Path(__file__).resolve().parent.parent / "charts" / "mainframe-rag" / "files" / "openwebui"
KEY = "sk-synthetic-admin-key"
PIPE = 'class Pipe:\n    """Synthetic docs Pipe."""\n'
PIPE_SHA = hashlib.sha256(PIPE.encode()).hexdigest()


def _load(name):
    spec = importlib.util.spec_from_file_location(f"owui_{name}", FILES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provision = _load("provision_pipe")
purge = _load("purge_chats")


# ------------------------------------------------------------ Pipe installer

def _cert(tmp_path, name):
    key, crt = tmp_path / f"{name}.key", tmp_path / f"{name}.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", f"/CN={name}", "-keyout", str(key), "-out", str(crt)],
                   check=True, capture_output=True)
    return key, crt


USER_KEY = "sk-synthetic-user-key"


class FakeOpenWebUI:
    """Functions API of the pinned release (v0.12.3), as the installer uses it.

    Status codes follow the release source (backend/open_webui): an unknown key
    is 401 (utils/auth.py get_current_user_by_api_key), a valid non-admin key is
    401 on every admin route (utils/auth.py get_admin_user) but 200 on the
    verified-user listing GET /api/v1/functions/, and an absent Function is 401
    NOT_FOUND on GET /id/{id} and POST /id/{id}/toggle (routers/functions.py),
    not 404. Listings carry no content.
    """

    def __init__(self):
        self.functions: dict[str, dict] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.mangle_content = False
        self.status_override: int | None = None
        self.absent_status = 401
        self.functions_disabled = False  # ENABLE_FUNCTIONS=false: both listings are []


def _server(state, key_file, cert_file):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            auth = self.headers.get("Authorization")
            state.requests.append((method, self.path, auth))
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            if state.status_override:
                return self._reply(state.status_override, {"detail": "UPSTREAM-SECRET-TEXT"})
            if auth not in (f"Bearer {KEY}", f"Bearer {USER_KEY}"):
                return self._reply(401, {"detail": "UPSTREAM-SECRET-TEXT"})
            listing = [] if state.functions_disabled else [
                {k: v for k, v in fn.items() if k != "content"} for fn in state.functions.values()]
            if method == "GET" and self.path == "/api/v1/functions/":
                return self._reply(200, listing)
            if auth != f"Bearer {KEY}":
                return self._reply(401, {"detail": "UPSTREAM-SECRET-TEXT"})
            prefix = "/api/v1/functions/id/"
            if method == "GET" and self.path == "/api/v1/functions/list":
                return self._reply(200, listing)
            if method == "GET" and self.path.startswith(prefix):
                fn = state.functions.get(self.path[len(prefix):])
                return self._reply(200, fn) if fn else self._reply(state.absent_status,
                                                                    {"detail": "not found"})
            if method == "POST" and self.path == "/api/v1/functions/create":
                if body["id"] in state.functions:
                    return self._reply(400, {"detail": "id taken"})
                content = body["content"] + ("#x" if state.mangle_content else "")
                state.functions[body["id"]] = {"id": body["id"], "name": body["name"],
                                               "content": content, "is_active": False}
                return self._reply(200, state.functions[body["id"]])
            if method == "POST" and self.path.startswith(prefix):
                fid, _, action = self.path[len(prefix):].partition("/")
                fn = state.functions.get(fid)
                if fn is None:
                    return self._reply(401 if action == "toggle" else 400, {"detail": "not found"})
                if action == "update":
                    fn["content"] = body["content"]
                elif action == "toggle":
                    fn["is_active"] = not fn["is_active"]
                return self._reply(200, fn)
            return self._reply(404, {"detail": "no route"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def owui(tmp_path):
    key_file, cert_file = _cert(tmp_path, "rag-openwebui")
    state = FakeOpenWebUI()
    httpd = _server(state, key_file, cert_file)
    pinned = provision.leaf_der(cert_file.read_text())
    client = provision.Client(KEY, pinned, host="127.0.0.1", port=httpd.server_address[1])
    yield state, client, tmp_path
    httpd.shutdown()
    httpd.server_close()


def _writes(state):
    return [(m, p) for m, p, _ in state.requests if m == "POST"]


def test_installer_creates_activates_and_verifies_the_stored_pipe(owui):
    state, client, _ = owui
    lines = provision.provision(client, PIPE, PIPE_SHA)
    assert lines == [f"docs Pipe installed: Function mainframe_rag_docs active, sha256 {PIPE_SHA}"]
    stored = state.functions["mainframe_rag_docs"]
    assert hashlib.sha256(stored["content"].encode()).hexdigest() == PIPE_SHA
    assert stored["is_active"] is True and stored["name"] == "Docs"
    assert _writes(state) == [("POST", "/api/v1/functions/create"),
                              ("POST", "/api/v1/functions/id/mainframe_rag_docs/toggle")]
    assert {auth for _, _, auth in state.requests} == {f"Bearer {KEY}"}


@pytest.mark.parametrize("absent_status", [401, 404], ids=["v0.12.3-401", "404"])
def test_installer_first_install_creates_when_the_function_is_absent(owui, absent_status):
    """v0.12.3 answers an absent Function with 401 (routers/functions.py
    get_function_by_id); a fresh install must create it, never read that 401 as
    a refused key (#1107 lane finding)."""
    state, client, _ = owui
    state.absent_status = absent_status
    lines = provision.provision(client, PIPE, PIPE_SHA)
    assert lines == [f"docs Pipe installed: Function mainframe_rag_docs active, sha256 {PIPE_SHA}"]
    assert _writes(state) == [("POST", "/api/v1/functions/create"),
                              ("POST", "/api/v1/functions/id/mainframe_rag_docs/toggle")]
    stored = state.functions["mainframe_rag_docs"]
    assert hashlib.sha256(stored["content"].encode()).hexdigest() == PIPE_SHA
    assert stored["is_active"] is True
    # The stored content is read back after the create and before activation.
    order = [(m, p) for m, p, _ in state.requests]
    create = order.index(("POST", "/api/v1/functions/create"))
    toggle = order.index(("POST", "/api/v1/functions/id/mainframe_rag_docs/toggle"))
    assert ("GET", "/api/v1/functions/id/mainframe_rag_docs") in order[create + 1:toggle]


@pytest.mark.parametrize("key", ["sk-synthetic-unknown-key", USER_KEY], ids=["unknown", "non-admin"])
def test_installer_refuses_a_bad_key_without_creating(owui, key):
    """An unknown key and a non-admin key are both 401 on admin routes; either is
    the fixed refusal, never an absent Function, and nothing is written."""
    state, client, _ = owui
    bad = provision.Client(key, client._pinned, host="127.0.0.1", port=client._port)
    with pytest.raises(provision.ProvisionError, match=r"refused the admin API key \(HTTP 401\)"):
        provision.provision(bad, PIPE, PIPE_SHA)
    assert _writes(state) == []
    assert state.functions == {}


def test_installer_fails_closed_when_the_listing_cannot_show_the_function(owui):
    """With Functions disabled the listing is always empty, so the only-one-Function
    guard would be blind: the run fails after the write instead of reporting success,
    and a re-run does not overwrite blindly (create of an existing id is HTTP 400)."""
    state, client, _ = owui
    state.functions_disabled = True
    with pytest.raises(provision.ProvisionError, match="missing from the listing after the write"):
        provision.provision(client, PIPE, PIPE_SHA)
    with pytest.raises(provision.ProvisionError, match=r"cannot write Function .* \(HTTP 400\)"):
        provision.provision(client, PIPE, PIPE_SHA)


def test_installer_refuses_when_any_other_function_exists(owui):
    """Every Function can read the user's tokens: the Docs Pipe is the only one."""
    state, client, _ = owui
    state.functions["community_x"] = {"id": "community_x", "name": "X", "content": "c", "is_active": True}
    with pytest.raises(provision.ProvisionError, match="a Function other than the repository docs Pipe"):
        provision.provision(client, PIPE, PIPE_SHA)
    assert _writes(state) == []


def test_installer_repeat_run_converges_without_writes(owui):
    state, client, _ = owui
    provision.provision(client, PIPE, PIPE_SHA)
    state.requests.clear()
    lines = provision.provision(client, PIPE, PIPE_SHA)
    assert lines[0].startswith("docs Pipe unchanged:")
    assert _writes(state) == []
    # A deactivated Function is switched back on, never toggled off.
    state.functions["mainframe_rag_docs"]["is_active"] = False
    provision.provision(client, PIPE, PIPE_SHA)
    assert state.functions["mainframe_rag_docs"]["is_active"] is True
    provision.provision(client, PIPE, PIPE_SHA)
    assert state.functions["mainframe_rag_docs"]["is_active"] is True


def test_installer_updates_drifted_stored_content(owui):
    state, client, _ = owui
    provision.provision(client, PIPE, PIPE_SHA)
    state.functions["mainframe_rag_docs"]["content"] = "class Pipe: pass  # edited in the admin UI\n"
    state.requests.clear()
    lines = provision.provision(client, PIPE, PIPE_SHA)
    assert lines[0].startswith("docs Pipe updated:")
    assert _writes(state) == [("POST", "/api/v1/functions/id/mainframe_rag_docs/update")]
    assert state.functions["mainframe_rag_docs"]["content"] == PIPE


def test_installer_refuses_a_mounted_pipe_that_is_not_the_repository_file(owui):
    state, client, _ = owui
    with pytest.raises(provision.ProvisionError, match="does not match the repository file"):
        provision.provision(client, PIPE + "# tampered\n", PIPE_SHA)
    assert state.requests == []


def test_installer_refuses_when_the_stored_content_differs_after_the_write(owui):
    state, client, _ = owui
    state.mangle_content = True
    with pytest.raises(provision.ProvisionError, match="does not match the repository Pipe"):
        provision.provision(client, PIPE, PIPE_SHA)


def test_installer_never_sends_the_key_to_another_certificate(owui, tmp_path):
    state, client, _ = owui
    _, other = _cert(tmp_path, "impostor")
    wrong = provision.Client(KEY, provision.leaf_der(other.read_text()), host="127.0.0.1",
                             port=client._port)
    with pytest.raises(provision.ProvisionError, match="admin key was not sent"):
        provision.provision(wrong, PIPE, PIPE_SHA)
    assert state.requests == []


@pytest.mark.parametrize("status", [401, 403, 500])
def test_installer_errors_are_fixed_text_without_response_bodies(owui, status, capsys,
                                                                  monkeypatch):
    state, client, tmp_path = owui
    state.status_override = status
    (tmp_path / "docs_pipe.py").write_text(PIPE, encoding="utf-8")
    monkeypatch.setattr(provision, "MOUNT", tmp_path)
    monkeypatch.setattr(provision, "CERT", tmp_path / "rag-openwebui.crt")
    monkeypatch.setattr(provision, "PORT", client._port)
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert provision.main([PIPE_SHA]) == 1
    err = capsys.readouterr().err
    assert err.startswith("FAIL: docs Pipe provisioning refused:") and f"HTTP {status}" in err
    assert "UPSTREAM-SECRET-TEXT" not in err and KEY not in err


def test_installer_main_reads_the_key_from_stdin_only(owui, capsys, monkeypatch):
    _state, client, tmp_path = owui
    (tmp_path / "docs_pipe.py").write_text(PIPE, encoding="utf-8")
    monkeypatch.setattr(provision, "MOUNT", tmp_path)
    monkeypatch.setattr(provision, "CERT", tmp_path / "rag-openwebui.crt")
    monkeypatch.setattr(provision, "PORT", client._port)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert provision.main([PIPE_SHA]) == 1
    assert "no admin API key on stdin" in capsys.readouterr().err
    assert provision.main(["not-a-sha"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert provision.main([PIPE_SHA]) == 0
    out = capsys.readouterr().out
    assert out == f"docs Pipe installed: Function mainframe_rag_docs active, sha256 {PIPE_SHA}\n"
    assert KEY not in out and PIPE not in out


# ------------------------------------------------------------ retention purge

NOW = 1_800_000_000
DAY = 86400
CANARY_OLD = "CANARY-OLD-CHAT-TEXT"


def _db(tmp_path, *, chat_columns="id TEXT PRIMARY KEY, user_id TEXT, title TEXT, chat TEXT, "
                                  "created_at INTEGER, updated_at INTEGER, share_id TEXT"):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    db = data / "webui.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"CREATE TABLE chat ({chat_columns})")
    conn.execute("CREATE TABLE chat_message (id TEXT PRIMARY KEY, chat_id TEXT, content TEXT)")
    conn.execute("CREATE TABLE file (id TEXT PRIMARY KEY, user_id TEXT, path TEXT, "
                 "updated_at INTEGER)")
    conn.execute("CREATE TABLE chat_file (chat_id TEXT, file_id TEXT)")
    return data, db, conn


def _seed(conn, data):
    old, new = NOW - 31 * DAY, NOW - 29 * DAY
    rows = [
        ("old-1", "u1", f"title {CANARY_OLD}", json.dumps({"m": CANARY_OLD}), old, old, "s-old"),
        ("old-2", "u2", "t", "{}", old, old, None),
        ("new-1", "u1", "keep", json.dumps({"m": "KEEP-TEXT"}), new, new, "s-new"),
        # Shared snapshots: owned by "shared-<chat id>".
        ("s-old", "shared-old-1", "snap", json.dumps({"m": CANARY_OLD}), new, new, None),
        ("s-new", "shared-new-1", "snap", "{}", new, new, None),
        ("s-stale", "shared-gone", "snap", "{}", old, old, None),
    ]
    conn.executemany("INSERT INTO chat VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO chat_message VALUES (?, ?, ?)", [
        ("m1", "old-1", CANARY_OLD), ("m2", "old-2", "x"), ("m3", "new-1", "KEEP-TEXT")])
    inside = data / "uploads" / "old.txt"
    inside.write_text(CANARY_OLD)
    outside = data.parent / "outside.txt"
    outside.write_text("not ours")
    conn.executemany("INSERT INTO file VALUES (?, ?, ?, ?)", [
        ("f-old", "u1", str(inside), old), ("f-out", "u1", str(outside), old),
        ("f-new", "u1", str(data / "uploads" / "new.txt"), new)])
    conn.executemany("INSERT INTO chat_file VALUES (?, ?)", [("new-1", "f-old"), ("new-1", "f-new")])
    return inside, outside


def test_purge_deletes_expired_chats_snapshots_links_and_files_and_reclaims_the_text(tmp_path):
    data, db, conn = _db(tmp_path)
    inside, outside = _seed(conn, data)
    counts = purge.purge(db, data, 30, NOW)
    assert counts == {"chats": 2, "snapshots": 2, "linked_rows": 3, "files": 2, "orphans": 0}
    ids = {row[0] for row in conn.execute("SELECT id FROM chat")}
    assert ids == {"new-1", "s-new"}
    assert [r[0] for r in conn.execute("SELECT id FROM chat_message")] == ["m3"]
    assert [r[0] for r in conn.execute("SELECT id FROM file")] == ["f-new"]
    assert list(conn.execute("SELECT * FROM chat_file")) == [("new-1", "f-new")]
    assert not inside.exists() and outside.exists()
    conn.close()
    # The deleted text is gone from the database files on the claim.
    for path in data.iterdir():
        if path.is_file():
            assert CANARY_OLD.encode() not in path.read_bytes(), path.name
    # The next ordinary operation succeeds on the purged database.
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO chat VALUES ('next', 'u1', 't', '{}', ?, ?, NULL)", (NOW, NOW))
    conn.commit()
    assert conn.execute("SELECT count(*) FROM chat").fetchone() == (3,)
    # A repeat run converges: nothing left to delete.
    assert purge.purge(db, data, 30, NOW) == {"chats": 0, "snapshots": 0, "linked_rows": 0,
                                              "files": 0, "orphans": 0}


def test_purge_reclaims_freed_pages_so_the_file_shrinks(tmp_path):
    data, db, conn = _db(tmp_path)
    old = NOW - 60 * DAY
    conn.executemany("INSERT INTO chat VALUES (?, 'u', 't', ?, 0, ?, NULL)",
                     [(f"c{i}", "x" * 4000, old) for i in range(300)])
    conn.execute("INSERT INTO chat VALUES ('keep', 'u', 't', '{}', 0, ?, NULL)", (NOW,))
    (before,) = conn.execute("PRAGMA page_count").fetchone()
    assert purge.purge(db, data, 30, NOW)["chats"] == 300
    (after,) = conn.execute("PRAGMA page_count").fetchone()
    assert conn.execute("PRAGMA freelist_count").fetchone() == (0,)
    assert after < before / 10


def test_purge_window_boundary_keeps_a_chat_updated_exactly_at_the_cutoff(tmp_path):
    data, db, conn = _db(tmp_path)
    cutoff = NOW - 30 * DAY
    conn.executemany("INSERT INTO chat VALUES (?, 'u', 't', '{}', 0, ?, NULL)",
                     [("at", cutoff), ("before", cutoff - 1)])
    assert purge.purge(db, data, 30, NOW)["chats"] == 1
    assert [r[0] for r in conn.execute("SELECT id FROM chat")] == ["at"]


@pytest.mark.parametrize("columns,error", [
    ("id TEXT PRIMARY KEY, user_id TEXT, chat TEXT, created_at INTEGER", "schema"),
    ("id TEXT PRIMARY KEY, owner TEXT, updated_at INTEGER", "schema"),
])
def test_purge_refuses_an_unexpected_schema_and_deletes_nothing(tmp_path, columns, error):
    data, db, conn = _db(tmp_path, chat_columns=columns)
    conn.execute("INSERT INTO chat_message VALUES ('m', 'c', 'x')")
    with pytest.raises(purge.PurgeError, match=f"^{error}$"):
        purge.purge(db, data, 30, NOW)
    assert conn.execute("SELECT count(*) FROM chat_message").fetchone() == (1,)


def test_purge_refuses_timestamps_that_are_not_epoch_seconds(tmp_path):
    data, db, conn = _db(tmp_path)
    conn.execute("INSERT INTO chat VALUES ('ms', 'u', 't', '{}', 0, ?, NULL)", (NOW * 1000,))
    conn.execute("INSERT INTO chat VALUES ('old', 'u', 't', '{}', 0, ?, NULL)", (NOW - 90 * DAY,))
    with pytest.raises(purge.PurgeError, match="^timestamp-unit$"):
        purge.purge(db, data, 30, NOW)
    assert conn.execute("SELECT count(*) FROM chat").fetchone() == (2,)


def test_purge_busy_database_is_a_failed_run_then_the_next_run_succeeds(tmp_path):
    data, db, conn = _db(tmp_path)
    _seed(conn, data)
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(purge.PurgeError, match="^busy$"):
        purge.purge(db, data, 30, NOW, busy_timeout_s=0.05)
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT count(*) FROM chat").fetchone() == (6,)
    assert purge.purge(db, data, 30, NOW)["chats"] == 2


def test_purge_with_an_open_reader_fails_until_the_deleted_text_is_gone(tmp_path):
    """B2: a reader pins the WAL, so the checkpoint cannot truncate it. The run
    fails, and so does every later run while the content remains, even when
    there is nothing new to delete; success is reported only once the text is
    gone from the files on the claim."""
    data, db, conn = _db(tmp_path)
    _seed(conn, data)
    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM chat").fetchone()
    # New writes after the reader's snapshot keep the WAL from truncating.
    for _ in range(2):
        with pytest.raises(purge.PurgeError, match="^checkpoint-busy$"):
            purge.purge(db, data, 30, NOW, busy_timeout_s=0.05)
    status: dict = {}
    assert purge.run_once(db, data, 30, status, NOW, busy_timeout_s=0.05) is False
    assert status["last_error"] == "checkpoint-busy" and "last_success" not in status
    reader.execute("ROLLBACK")
    reader.close()
    counts = purge.purge(db, data, 30, NOW)
    assert counts["chats"] == 0  # the rows went in the first run; only the scrub remained
    conn.close()
    for path in data.iterdir():
        if path.is_file():
            assert CANARY_OLD.encode() not in path.read_bytes(), path.name
    assert not (data / purge.PENDING_NAME).exists()
    # Converged: the next run does no scrub work and succeeds.
    assert purge.purge(db, data, 30, NOW)["chats"] == 0


def test_purge_unlink_failure_is_a_failed_run_and_the_next_run_removes_the_upload(
        tmp_path, monkeypatch):
    """B2: the upload is removed inside the transaction; if it cannot be, the
    rows stay, the run fails, and the next run retries (never an orphaned file
    with no row left to find it)."""
    data, db, conn = _db(tmp_path)
    inside, _outside = _seed(conn, data)
    real_unlink = Path.unlink

    def failing(self, *args, **kwargs):
        if self.name == "old.txt":
            raise PermissionError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing)
    with pytest.raises(purge.PurgeError):
        purge.purge(db, data, 30, NOW)
    assert inside.exists()
    assert {r[0] for r in conn.execute("SELECT id FROM file")} == {"f-old", "f-out", "f-new"}
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert purge.purge(db, data, 30, NOW)["files"] == 2
    assert not inside.exists()


def _upload(data, name, text, age_days):
    import os
    path = data / "uploads" / name
    path.write_text(text)
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def test_purge_removes_the_uploaded_bytes_of_purged_chats_but_keeps_shared_ones(tmp_path):
    """Trial V1 follow-on: rows alone are not enough, the bytes in uploads/ must
    go. A file attached to a purged chat is removed even when its own row is
    recent; a file a surviving chat still references stays."""
    data, db, conn = _db(tmp_path)
    old, new = NOW - 31 * DAY, NOW - 1 * DAY
    conn.execute("INSERT INTO chat VALUES ('gone', 'u1', 't', '{}', ?, ?, NULL)", (old, old))
    conn.execute("INSERT INTO chat VALUES ('kept', 'u1', 't', '{}', ?, ?, NULL)", (new, new))
    mine = _upload(data, "attached.txt", CANARY_OLD, 0)
    shared = _upload(data, "shared.txt", "SHARED", 0)
    other = _upload(data, "unrelated.txt", "KEEP", 0)
    conn.executemany("INSERT INTO file VALUES (?, 'u1', ?, ?)", [
        ("f-mine", str(mine), new), ("f-shared", str(shared), new), ("f-other", str(other), new)])
    conn.executemany("INSERT INTO chat_file VALUES (?, ?)", [
        ("gone", "f-mine"), ("gone", "f-shared"), ("kept", "f-shared")])
    counts = purge.purge(db, data, 30, NOW)
    assert counts["chats"] == 1 and counts["files"] == 1
    assert not mine.exists() and shared.exists() and other.exists()
    assert {r[0] for r in conn.execute("SELECT id FROM file")} == {"f-shared", "f-other"}
    conn.close()
    for path in data.rglob("*"):
        if path.is_file() and path.name not in ("shared.txt", "unrelated.txt"):
            assert CANARY_OLD.encode() not in path.read_bytes(), path.name


def test_purge_removes_orphaned_uploads_after_a_grace_and_never_a_referenced_file(tmp_path):
    """An upload with no row (left by an older failure, or a bypassed upload)
    is removed once it is past the grace period; a fresh one (its row may be
    about to be written), a referenced one and anything outside uploads/ stay."""
    data, db, conn = _db(tmp_path)
    kept_row = _upload(data, "referenced.txt", "ROW", 40)
    stale = _upload(data, "orphan-stale.txt", CANARY_OLD, 3)
    fresh = _upload(data, "orphan-fresh.txt", "FRESH", 0)
    nested = data / "uploads" / "sub"
    nested.mkdir()
    deep = nested / "orphan-deep.txt"
    deep.write_text("DEEP")
    import os
    os.utime(deep, (NOW - 5 * DAY, NOW - 5 * DAY))
    outside = data.parent / "outside.txt"
    outside.write_text("not ours")
    conn.execute("INSERT INTO file VALUES ('f-ref', 'u1', ?, ?)", (str(kept_row), NOW - DAY))
    counts = purge.purge(db, data, 30, NOW)
    assert counts["orphans"] == 2 and counts["files"] == 0
    assert kept_row.exists() and fresh.exists() and outside.exists()
    assert not stale.exists() and not deep.exists()
    # Converged, and the next ordinary operation works.
    assert purge.purge(db, data, 30, NOW)["orphans"] == 0
    conn.execute("INSERT INTO chat VALUES ('next', 'u1', 't', '{}', ?, ?, NULL)", (NOW, NOW))


def test_purge_orphan_unlink_failure_is_a_failed_run_retried_next_time(tmp_path, monkeypatch):
    data, db, _conn = _db(tmp_path)
    stale = _upload(data, "orphan-stale.txt", CANARY_OLD, 3)
    real_unlink = Path.unlink

    def failing(self, *args, **kwargs):
        if self.name == "orphan-stale.txt":
            raise PermissionError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing)
    status: dict = {}
    assert purge.run_once(db, data, 30, status, NOW) is False
    assert status["last_error"] == "unlink"
    assert stale.exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert purge.run_once(db, data, 30, status, NOW + 1) is True and not stale.exists()


def test_purge_before_open_webui_created_its_database_is_an_empty_success(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    assert purge.purge(data / "webui.db", data, 30, NOW) == {
        "chats": 0, "snapshots": 0, "linked_rows": 0, "files": 0, "orphans": 0}
    assert not (data / "webui.db").exists()


def test_purge_runs_are_counted_and_failures_never_report_success(tmp_path, capsys):
    data, db, conn = _db(tmp_path, chat_columns="id TEXT PRIMARY KEY")
    status: dict = {}
    assert purge.run_once(db, data, 30, status, NOW) is False
    assert purge.run_once(db, data, 30, status, NOW + 1) is False
    recorded = json.loads((data / purge.STATUS_NAME).read_text())
    assert recorded["consecutive_failures"] == 2 and recorded["total_failures"] == 2
    assert recorded["last_error"] == "schema" and "last_success" not in recorded
    err = capsys.readouterr().err
    assert "chat purge FAILED (schema)" in err and "ok" not in err
    conn.execute("DROP TABLE chat")
    conn.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, updated_at INTEGER)")
    conn.execute("INSERT INTO chat VALUES ('c', 'u', ?)", (NOW - 40 * DAY,))
    assert purge.run_once(db, data, 30, status, NOW + 2) is True
    recorded = json.loads((data / purge.STATUS_NAME).read_text())
    assert recorded["consecutive_failures"] == 0 and recorded["total_failures"] == 2
    assert recorded["last_success"] == NOW + 2 and recorded["last_counts"]["chats"] == 1
    out = capsys.readouterr().out
    assert out == ("chat purge ok: window 30 days, chats 1, snapshots 0, linked rows 0, "
                   "files 0, orphan uploads 0\n")


def test_purge_cli_once_exit_codes_and_window_bounds(tmp_path):
    data, _db_path, conn = _db(tmp_path)
    _seed(conn, data)
    conn.close()
    assert purge.main(["--days", "0", "--data-dir", str(data), "--once"]) == 2
    assert purge.main(["--days", "3651", "--data-dir", str(data), "--once"]) == 2
    assert purge.main(["--days", "30", "--data-dir", str(data), "--once"]) == 0
    (data / "webui.db").write_bytes(b"not a database")
    for suffix in ("-wal", "-shm"):
        (data / f"webui.db{suffix}").unlink(missing_ok=True)
    assert purge.main(["--days", "30", "--data-dir", str(data), "--once"]) == 1
