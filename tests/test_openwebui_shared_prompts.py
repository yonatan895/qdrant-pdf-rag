"""Repository-owned shared prompt templates for Open WebUI (#1255).

charts/mainframe-rag/files/openwebui/provision_prompts.py converges the
workspace Prompts (the chat `/` commands) to the repository file prompts.json,
run inside the pod by deploy.sh with the admin key on stdin. The fake below is
the Prompts API of the pinned release (v0.13.4, backend/open_webui/routers/
prompts.py and models/prompts.py at tag v0.13.4) as the installer uses it.
Every template text here is synthetic; the shipped set is never asserted on.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import ssl
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

FILES = Path(__file__).resolve().parent.parent / "charts" / "mainframe-rag" / "files" / "openwebui"
KEY = "sk-synthetic-admin-key"
USER_KEY = "sk-synthetic-user-key"
ADMIN_ID = "admin-1"
PUBLIC_READ = ("user", "*", "read")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"owui_{name}", FILES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prompts = _load("provision_prompts")


def _doc(*items):
    return json.dumps({"prompts": [dict(zip(("command", "name", "content"), item, strict=True)) for item in items]},
                      indent=2).encode()


SET_A = _doc(("alpha", "Alpha", "Synthetic template A.\n\n"),
             ("beta-two", "Beta", "Synthetic template B: "))
SET_A_SHA = hashlib.sha256(SET_A).hexdigest()


def _cert(tmp_path, name):
    key, crt = tmp_path / f"{name}.key", tmp_path / f"{name}.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", f"/CN={name}", "-keyout", str(key), "-out", str(crt)],
                   check=True, capture_output=True)
    return key, crt


class FakePrompts:
    """Prompts API of v0.13.4 as the installer uses it.

    - GET /api/v1/functions/list is admin-only: 401 for any other key.
    - GET /api/v1/prompts/list?page=N: verified users; the admin (with the
      default BYPASS_ADMIN_ACCESS_CONTROL) sees every prompt, active or not,
      ``page_size`` per page (30 upstream), with the total.
    - POST /create: admin only here (the chart pins the user create/import
      permissions off); 400 when the command is taken; id is a server UUID.
    - POST /id/{id}/update: keeps the stored meta when the form's is empty and
      ignores any grants in the form (since v0.12.4 sharing changes only through
      the access route); a text change adds a history row upstream (counted here).
    - POST /id/{id}/access/update {"access_grants": [...]}: replaces the grants
      (an admin may grant public read) and answers the prompt.
    - POST /id/{id}/toggle flips is_active; DELETE /id/{id}/delete answers true.
    """

    def __init__(self):
        self.prompts: dict[str, dict] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.page_size = 30
        self.status_override: int | None = None
        self.mangle_content = False
        self.history_rows = 0

    def add(self, command, *, managed, name="N", content="C", grants=(PUBLIC_READ,), active=True):
        pid = str(uuid.uuid4())
        meta = {"mainframe_rag_managed": True} if managed else {"description": "made by hand"}
        self.prompts[pid] = {"id": pid, "command": command, "user_id": ADMIN_ID, "name": name,
                             "content": content, "meta": meta, "tags": [], "is_active": active,
                             "access_grants": [self._grant(pid, g) for g in grants]}
        return pid

    @staticmethod
    def _grant(pid, grant):
        principal_type, principal_id, permission = grant
        return {"id": str(uuid.uuid4()), "resource_type": "prompt", "resource_id": pid,
                "principal_type": principal_type, "principal_id": principal_id,
                "permission": permission, "created_at": 1}

    def _grants(self, pid, raw):
        return [self._grant(pid, (g["principal_type"], g["principal_id"], g["permission"])) for g in raw]

    def by_command(self):
        return {p["command"]: p for p in self.prompts.values()}


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
            admin = auth == f"Bearer {KEY}"
            url = urlsplit(self.path)
            if method == "GET" and url.path == "/api/v1/functions/list":
                return self._reply(200, []) if admin else self._reply(401, {"detail": "x"})
            if method == "GET" and url.path == "/api/v1/prompts/list":
                page = int(parse_qs(url.query).get("page", ["1"])[0])
                items = sorted(state.prompts.values(), key=lambda p: p["command"])
                if not admin:
                    items = [p for p in items if any(
                        (g["principal_type"], g["principal_id"], g["permission"]) == PUBLIC_READ
                        for g in p["access_grants"])]
                start = (page - 1) * state.page_size
                return self._reply(200, {"items": items[start:start + state.page_size],
                                         "total": len(items)})
            if not admin:
                return self._reply(401, {"detail": "x"})
            if method == "POST" and url.path == "/api/v1/prompts/create":
                if body["command"] in state.by_command():
                    return self._reply(400, {"detail": "command taken"})
                pid = str(uuid.uuid4())
                content = body["content"] + ("#x" if state.mangle_content else "")
                state.prompts[pid] = {"id": pid, "command": body["command"], "user_id": ADMIN_ID,
                                      "name": body["name"], "content": content,
                                      "meta": body.get("meta") or {}, "tags": body.get("tags") or [],
                                      "is_active": True,
                                      "access_grants": state._grants(pid, body.get("access_grants") or [])}
                return self._reply(200, state.prompts[pid])
            prefix = "/api/v1/prompts/id/"
            if url.path.startswith(prefix):
                pid, _, action = url.path[len(prefix):].partition("/")
                prompt = state.prompts.get(pid)
                if prompt is None:
                    return self._reply(404, {"detail": "not found"})
                if method == "POST" and action == "update":
                    prompt.update(command=body["command"], name=body["name"],
                                  content=body["content"] + ("#x" if state.mangle_content else ""),
                                  meta=body.get("meta") or prompt["meta"])
                    if body.get("tags") is not None:
                        prompt["tags"] = body["tags"]
                    state.history_rows += 1
                    return self._reply(200, prompt)
                if method == "POST" and action == "access/update":
                    if set(body) != {"access_grants"} or not isinstance(body["access_grants"], list):
                        return self._reply(422, {"detail": "invalid form"})
                    prompt["access_grants"] = state._grants(pid, body["access_grants"])
                    return self._reply(200, prompt)
                if method == "POST" and action == "toggle":
                    prompt["is_active"] = not prompt["is_active"]
                    return self._reply(200, prompt)
                if method == "DELETE" and action == "delete":
                    del state.prompts[pid]
                    return self._reply(200, True)
            return self._reply(404, {"detail": "no route"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def owui(tmp_path):
    key_file, cert_file = _cert(tmp_path, "rag-openwebui")
    state = FakePrompts()
    httpd = _server(state, key_file, cert_file)
    pinned = prompts.leaf_der(cert_file.read_text())
    client = prompts.Client(KEY, pinned, host="127.0.0.1", port=httpd.server_address[1])
    yield state, client, tmp_path
    httpd.shutdown()
    httpd.server_close()


def _writes(state):
    return [(m, p) for m, p, _ in state.requests if m != "GET"]


def _stored(state):
    """The stored outcome: command -> (name, content, grants, active, managed)."""
    return {p["command"]: (p["name"], p["content"],
                           sorted((g["principal_type"], g["principal_id"], g["permission"])
                                  for g in p["access_grants"]),
                           p["is_active"], (p["meta"] or {}).get("mainframe_rag_managed") is True)
            for p in state.prompts.values()}


EXPECTED_A = {
    "alpha": ("Alpha", "Synthetic template A.\n\n", [PUBLIC_READ], True, True),
    "beta-two": ("Beta", "Synthetic template B: ", [PUBLIC_READ], True, True),
}


def test_first_run_creates_the_repository_prompts_read_only_for_every_signed_in_user(owui):
    state, client, _ = owui
    lines = prompts.provision(client, SET_A, SET_A_SHA)
    assert lines == [(f"shared prompts converged: 2 managed (2 created, 0 updated, 0 removed, "
                      f"0 unchanged), sha256 {SET_A_SHA}")]
    assert _stored(state) == EXPECTED_A
    assert [m for m, _ in _writes(state)] == ["POST", "POST"]
    assert {auth for _, _, auth in state.requests} == {f"Bearer {KEY}"}
    # The key is proven an admin key before any prompt call.
    assert state.requests[0][:2] == ("GET", "/api/v1/functions/list")


def test_repeat_run_makes_no_writes(owui):
    state, client, _ = owui
    prompts.provision(client, SET_A, SET_A_SHA)
    state.requests.clear()
    rows = state.history_rows
    lines = prompts.provision(client, SET_A, SET_A_SHA)
    assert "(0 created, 0 updated, 0 removed, 2 unchanged)" in lines[0]
    assert _writes(state) == []
    assert state.history_rows == rows


def test_drift_is_converged_back_to_the_repository_text_grants_and_state(owui):
    state, client, _ = owui
    prompts.provision(client, SET_A, SET_A_SHA)
    current = state.by_command()
    current["alpha"]["content"] = "edited"
    current["beta-two"]["access_grants"].append(state._grant(current["beta-two"]["id"], ("user", "*", "write")))
    state.requests.clear()
    lines = prompts.provision(client, SET_A, SET_A_SHA)
    assert "(0 created, 2 updated, 0 removed, 0 unchanged)" in lines[0]
    assert _stored(state) == EXPECTED_A
    # A deactivated managed prompt is switched back on, never toggled off.
    current["alpha"]["is_active"] = False
    prompts.provision(client, SET_A, SET_A_SHA)
    assert _stored(state) == EXPECTED_A
    state.requests.clear()
    prompts.provision(client, SET_A, SET_A_SHA)
    assert _writes(state) == []


def test_grant_drift_alone_is_restored_through_the_access_route_without_a_text_write(owui):
    """#1357: v0.13.4 changes sharing only through /id/{id}/access/update; the
    text update route ignores grants, so a grants-only drift needs that route
    and must not add a history row."""
    state, client, _ = owui
    prompts.provision(client, SET_A, SET_A_SHA)
    beta = state.by_command()["beta-two"]
    beta["access_grants"] = [state._grant(beta["id"], ("group", "g-1", "write"))]
    rows = state.history_rows
    state.requests.clear()
    lines = prompts.provision(client, SET_A, SET_A_SHA)
    assert "(0 created, 1 updated, 0 removed, 1 unchanged)" in lines[0]
    assert _stored(state) == EXPECTED_A
    assert _writes(state) == [("POST", f"/api/v1/prompts/id/{beta['id']}/access/update")]
    assert state.history_rows == rows


def test_a_prompt_dropped_from_the_repository_is_removed_and_others_are_never_touched(owui):
    state, client, _ = owui
    prompts.provision(client, SET_A, SET_A_SHA)
    hand = state.add("by-hand", managed=False, content="hand text", grants=())
    before = json.dumps(state.prompts[hand], sort_keys=True)
    only_alpha = _doc(("alpha", "Alpha", "Synthetic template A.\n\n"))
    sha = hashlib.sha256(only_alpha).hexdigest()
    state.requests.clear()
    lines = prompts.provision(client, only_alpha, sha)
    assert "1 managed (0 created, 0 updated, 1 removed, 1 unchanged)" in lines[0]
    assert set(state.by_command()) == {"alpha", "by-hand"}
    assert json.dumps(state.prompts[hand], sort_keys=True) == before
    assert all(hand not in path for _, path in _writes(state))
    # The empty set is a valid converged state: every managed prompt goes.
    empty = _doc()
    lines = prompts.provision(client, empty, hashlib.sha256(empty).hexdigest())
    assert "0 managed (0 created, 0 updated, 1 removed, 0 unchanged)" in lines[0]
    assert set(state.by_command()) == {"by-hand"}


def test_a_repository_command_taken_by_an_unmanaged_prompt_is_refused_before_any_write(owui):
    state, client, _ = owui
    hand = state.add("beta-two", managed=False, content="hand text")
    before = json.dumps(state.prompts[hand], sort_keys=True)
    with pytest.raises(prompts.ProvisionError, match="taken by a prompt this installer does not manage"):
        prompts.provision(client, SET_A, SET_A_SHA)
    assert _writes(state) == []
    assert json.dumps(state.prompts[hand], sort_keys=True) == before


def test_every_listing_page_is_read_before_deciding(owui):
    state, client, _ = owui
    state.page_size = 2
    for n in range(5):
        state.add(f"hand-{n}", managed=False)
    stale = state.add("zz-old", managed=True)  # sorts onto the last page
    prompts.provision(client, SET_A, SET_A_SHA)
    assert stale not in state.prompts
    assert len(state.prompts) == 7
    pages = [p for m, p, _ in state.requests if p.startswith("/api/v1/prompts/list")]
    assert "/api/v1/prompts/list?page=3" in pages


@pytest.mark.parametrize("raw, reason", [
    (b"not json", "not JSON"),
    (b'{"prompts": [], "extra": 1}', "top level"),
    (_doc(("Bad Command", "N", "C")), "command"),
    (_doc(("/alpha", "N", "C")), "command"),
    (_doc(("compare", "N", "C")), "built-in"),
    # v0.13.4 SlashCommands.svelte adds /side (typed as side or btw) and /comments.
    (_doc(("side", "N", "C")), "built-in"),
    (_doc(("btw", "N", "C")), "built-in"),
    (_doc(("comments", "N", "C")), "built-in"),
    (_doc(("alpha", "N", "C"), ("alpha", "M", "D")), "duplicate"),
    (_doc(("alpha", "", "C")), "name"),
    (_doc(("alpha", "N", "")), "content"),
    (_doc(("alpha", "N", "<img src=x onerror=alert(1)>")), "markup"),
    (_doc(("alpha", "N", "C\x07")), "control"),
    (json.dumps({"prompts": [{"command": "a", "name": "N", "content": "C", "x": 1}]}).encode(), "keys"),
])
def test_an_invalid_repository_file_is_refused_before_the_key_is_used(owui, raw, reason):
    state, client, _ = owui
    with pytest.raises(prompts.ProvisionError, match=reason):
        prompts.provision(client, raw, hashlib.sha256(raw).hexdigest())
    assert state.requests == []


def test_the_shipped_repository_file_is_valid():
    raw = (FILES / "prompts.json").read_bytes()
    assert isinstance(prompts.load_prompts(raw), list)


def test_a_mounted_file_that_is_not_this_release_is_refused_with_a_rerun_message(owui):
    state, client, _ = owui
    with pytest.raises(prompts.ProvisionError, match="wait a minute, then re-run airgap:deploy"):
        prompts.provision(client, SET_A + b"\n", SET_A_SHA)
    assert state.requests == []


@pytest.mark.parametrize("key", ["sk-synthetic-unknown-key", USER_KEY], ids=["unknown", "non-admin"])
def test_a_key_that_is_not_an_admin_key_is_refused_without_writes(owui, key):
    state, client, _ = owui
    bad = prompts.Client(key, client._pinned, host="127.0.0.1", port=client._port)
    with pytest.raises(prompts.ProvisionError, match=r"refused the admin API key \(HTTP 401\)"):
        prompts.provision(bad, SET_A, SET_A_SHA)
    assert _writes(state) == [] and state.prompts == {}


def test_a_stored_prompt_that_differs_after_the_write_fails_the_run(owui):
    state, client, _ = owui
    state.mangle_content = True
    with pytest.raises(prompts.ProvisionError, match="after the write"):
        prompts.provision(client, SET_A, SET_A_SHA)


def test_the_key_is_never_sent_to_another_certificate(owui, tmp_path):
    state, client, _ = owui
    _, other = _cert(tmp_path, "impostor")
    wrong = prompts.Client(KEY, prompts.leaf_der(other.read_text()), host="127.0.0.1",
                           port=client._port)
    with pytest.raises(prompts.ProvisionError, match="admin key was not sent"):
        prompts.provision(wrong, SET_A, SET_A_SHA)
    assert state.requests == []


def _main_env(monkeypatch, client, tmp_path, stdin):
    (tmp_path / "prompts.json").write_bytes(SET_A)
    monkeypatch.setattr(prompts, "MOUNT", tmp_path)
    monkeypatch.setattr(prompts, "CERT", tmp_path / "rag-openwebui.crt")
    monkeypatch.setattr(prompts, "PORT", client._port)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return hashlib.sha256(Path(prompts.__file__).read_bytes()).hexdigest()


@pytest.mark.parametrize("status", [401, 403, 500])
def test_errors_are_fixed_text_without_response_bodies(owui, status, capsys, monkeypatch):
    state, client, tmp_path = owui
    state.status_override = status
    installer_sha = _main_env(monkeypatch, client, tmp_path, KEY + "\n")
    assert prompts.main([SET_A_SHA, installer_sha]) == 1
    err = capsys.readouterr().err
    assert err.startswith("FAIL: shared prompt provisioning refused:") and f"HTTP {status}" in err
    assert "UPSTREAM-SECRET-TEXT" not in err and KEY not in err


def test_main_checks_both_hashes_and_reads_the_key_from_stdin_only(owui, capsys, monkeypatch):
    state, client, tmp_path = owui
    installer_sha = _main_env(monkeypatch, client, tmp_path, "")
    assert prompts.main([SET_A_SHA, installer_sha]) == 1
    assert "no admin API key on stdin" in capsys.readouterr().err
    assert prompts.main([SET_A_SHA]) == 1
    assert prompts.main(["not-a-sha", installer_sha]) == 1
    # A stale mounted installer (mount lag) refuses before the key is read.
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert prompts.main([SET_A_SHA, "0" * 64]) == 1
    err = capsys.readouterr().err
    assert "installer" in err and "wait a minute, then re-run airgap:deploy" in err
    assert state.requests == []
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert prompts.main([SET_A_SHA, installer_sha]) == 0
    out = capsys.readouterr().out
    assert out.startswith("shared prompts converged: 2 managed (2 created,")
    assert KEY not in out and "Synthetic template" not in out
