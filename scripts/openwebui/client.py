"""Open WebUI dev helper (#1070, from #1073): host-side browser stand-in, stdlib only.

Run by dev.sh as `python3 -I client.py COMMAND STATE_DIR [ARGS]`. It signs in
through the real redirect chain a browser follows (router stand-in -> Open WebUI ->
Dex login form -> Open WebUI callback), over HTTPS verified against the run's local
CA, with *.localhost resolved to 127.0.0.1 as Chrome, Edge and Firefox do. Output is
JSON with ids, counts and states: never a password, token, key, question or answer.

Commands:
  secrets STATE                  create the mode-600 password files and secrets.json if absent
  users STATE                    print the user names, one per line (the Dex hash order)
  dex-config STATE HASHES        print the Dex config (HASHES: one bcrypt hash per USERS line)
  login STATE USER               sign in; print role, groups and visible models
  api-key STATE                  admin: create an API key into STATE/admin-api-key (mode 600)
  access STATE                   admin: create the groups, grant the Docs model to them
  smoke STATE                    both users: sign in, models, one docs question, blocked paths
  health STATE                   Open WebUI /health through the router and Dex discovery

Environment: OWUI_URL (https://owui.localhost), DEX_ISSUER (https://dex.localhost:5556/dex).
"""
from __future__ import annotations

import http.client
import http.cookiejar
import json
import os
import re
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

# name -> (group, Dex user id). The two test users the maintainer signs in as, and
# the admin who installs the Pipe (the chart's site procedure step 4).
USERS = {
    "owui-admin": ("owui-admins", "7c1f3e52-0000-4000-8000-000000000001"),
    "team-user": ("internal-team", "7c1f3e52-0000-4000-8000-000000000002"),
    "docs-user": ("docs-readers", "7c1f3e52-0000-4000-8000-000000000003"),
}
EMAIL_DOMAIN = "trial.test"
DOCS_MODEL = "mainframe_rag_docs"  # provision_pipe.py PIPE_ID
DOCS_GROUPS = ("internal-team", "docs-readers")
# The site acceptance list (docs/deploy.md, allow-list proxy, #1083): each must be 403
# "denied" from the proxy sidecar. The null-byte form is sent percent-encoded.
BLOCKED = (
    "/api/v1/files/", "/api/v1/files", "/API/v1/files/", "//api/v1/files/", "/api/v1/./files/",
    "/api/v1/%66iles/", "/api/v1%2Ffiles/", "/api/v1/files%2F", "/%61pi/v1/files/",
    "/api/v1/chats/%2e%2e/files/", "/api/v1/%2566iles/", "/api/v1/files%00",
    "/api/v1/retrieval/process/text", "/api/v1/knowledge/", "/api/v1/audio/transcriptions",
    "/openai/chat/completions", "/ollama/api/chat", "/api/v1/utils/code/execute",
)

OWUI_URL = os.environ.get("OWUI_URL", "https://owui.localhost").rstrip("/")
DEX_ISSUER = os.environ.get("DEX_ISSUER", "https://dex.localhost:5556/dex").rstrip("/")
ADMIN = "owui-admin"

_real_getaddrinfo = socket.getaddrinfo


def _getaddrinfo(host, *args, **kwargs):
    # Browsers resolve every *.localhost name to loopback (RFC 6761); do the same here.
    if isinstance(host, str) and host.endswith(".localhost"):
        host = "127.0.0.1"
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _getaddrinfo


def out(obj) -> None:
    print(json.dumps(obj, sort_keys=True))


def _private_write(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


class Session:
    def __init__(self, state: str) -> None:
        self.state = state
        self.ctx = ssl.create_default_context(cafile=os.path.join(state, "pki", "ca.crt"))
        os.makedirs(os.path.join(state, "sessions"), mode=0o700, exist_ok=True)

    # ------------------------------------------------------------ sign-in
    def password(self, user: str) -> str:
        with open(os.path.join(self.state, f"{user}.password"), encoding="utf-8") as fh:
            return fh.read().strip()

    def login(self, user: str) -> dict:
        password = self.password(user)
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                         urllib.request.HTTPSHandler(context=self.ctx))
        with op.open(OWUI_URL + "/oauth/oidc/login", timeout=30) as resp:
            form_url = resp.geturl()
            html = resp.read().decode("utf-8", "replace")
        action = re.search(r'<form[^>]*action="([^"]+)"', html)
        post_url = urllib.parse.urljoin(form_url, action.group(1).replace("&amp;", "&")) if action else form_url
        data = urllib.parse.urlencode({"login": f"{user}@{EMAIL_DOMAIN}", "password": password}).encode()
        with op.open(urllib.request.Request(post_url, data=data, method="POST"), timeout=30) as resp:
            final = resp.geturl()
            resp.read()
        hops = {"login_form_host": urllib.parse.urlsplit(form_url).netloc,
                "final_host": urllib.parse.urlsplit(final).netloc,
                "final_path": urllib.parse.urlsplit(final).path}
        tok = next((c.value for c in jar if c.name == "token"), None)
        if not tok:
            return {"user": user, "signed_in": False, **hops}
        _private_write(self._token_path(user), tok)
        session = next((c.value for c in jar if c.name == "oauth_session_id"), None) or ""
        _private_write(self._token_path(user) + ".oauth", session)
        return {"user": user, "signed_in": True, **hops}

    def _token_path(self, user: str) -> str:
        return os.path.join(self.state, "sessions", f"{user}.token")

    def _headers(self, user: str | None) -> dict:
        headers = {"Accept": "application/json"}
        if user:
            with open(self._token_path(user), encoding="utf-8") as fh:
                headers["Authorization"] = f"Bearer {fh.read().strip()}"
            try:
                with open(self._token_path(user) + ".oauth", encoding="utf-8") as fh:
                    session = fh.read().strip()
            except OSError:
                session = ""
            if session:
                headers["Cookie"] = f"oauth_session_id={session}"
        return headers

    # ------------------------------------------------------------ API
    def api(self, user: str | None, method: str, path: str, body=None, timeout=60):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(OWUI_URL + path, data=data, method=method, headers=self._headers(user))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = {"non_json_bytes": len(raw)}
        return status, parsed

    def raw(self, user: str | None, method: str, path: str) -> tuple[int, bytes]:
        """One request with the path sent exactly as given (no client normalisation)."""
        parts = urllib.parse.urlsplit(OWUI_URL)
        conn = http.client.HTTPSConnection(parts.hostname or "", parts.port or 443, timeout=30, context=self.ctx)
        try:
            headers = self._headers(user)
            headers["Content-Type"] = "application/json"
            conn.request(method, path, body=b"{}", headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def whoami(self, user: str) -> dict:
        status, me = self.api(user, "GET", "/api/v1/auths/")
        gstatus, groups = self.api(user, "GET", "/api/v1/groups/")
        mstatus, models = self.api(user, "GET", "/api/models")
        return {"user": user, "auth_status": status, "role": (me or {}).get("role"),
                "groups": sorted(g.get("name") for g in groups) if gstatus == 200 and isinstance(groups, list) else [],
                "models_status": mstatus,
                "models": sorted(m["id"] for m in (models or {}).get("data", [])) if mstatus == 200 else []}

    def group_ids(self) -> dict:
        status, groups = self.api(ADMIN, "GET", "/api/v1/groups/")
        if status != 200 or not isinstance(groups, list):
            raise SystemExit(f"cannot list groups (HTTP {status})")
        return {g.get("name"): g.get("id") for g in groups}

    # ------------------------------------------------------------ state reads (acceptance)
    def chat_ids(self, user: str) -> list[str]:
        status, body = self.api(user, "GET", "/api/v1/chats/list")
        return sorted(c.get("id") for c in body) if status == 200 and isinstance(body, list) else []

    def chat_status(self, user: str, chat_id: str) -> int:
        return self.api(user, "GET", f"/api/v1/chats/{chat_id}")[0]

    def function_sha256(self) -> str | None:
        status, fn = self.api(ADMIN, "GET", f"/api/v1/functions/id/{DOCS_MODEL}")
        if status != 200 or not isinstance(fn, dict):
            return None
        import hashlib
        return hashlib.sha256((fn.get("content") or "").encode()).hexdigest()

    def admin_config_value(self, key: str):
        status, cfg = self.api(ADMIN, "GET", "/api/v1/auths/admin/config")
        return (cfg or {}).get(key) if status == 200 and isinstance(cfg, dict) else None

    def admin_config_set(self, key: str, value) -> int:
        status, cfg = self.api(ADMIN, "GET", "/api/v1/auths/admin/config")
        if status != 200 or not isinstance(cfg, dict):
            return status
        cfg[key] = value
        return self.api(ADMIN, "POST", "/api/v1/auths/admin/config", cfg)[0]

    # ------------------------------------------------------------ chat
    def chat(self, user: str, model: str, text: str) -> dict:
        uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
        now = int(time.time())
        umsg = {"id": uid, "parentId": None, "childrenIds": [aid], "role": "user", "content": text,
                "timestamp": now, "models": [model]}
        amsg = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant", "content": "",
                "model": model, "modelName": model, "modelIdx": 0, "timestamp": now}
        status, created = self.api(user, "POST", "/api/v1/chats/new", {"chat": {
            "title": "Hands-on smoke", "models": [model], "params": {},
            "history": {"messages": {uid: umsg, aid: amsg}, "currentId": aid},
            "messages": [umsg, amsg], "tags": [], "timestamp": now * 1000}})
        if status != 200:
            return {"chat_create": status}
        chat_id = created["id"]
        status, _resp = self.api(user, "POST", "/api/chat/completions", {
            "model": model, "stream": True, "messages": [{"role": "user", "content": text}],
            "chat_id": chat_id, "id": aid, "parent_id": uid, "session_id": None,
            "features": {}, "params": {}, "background_tasks": {}}, timeout=360)
        message, stored = {}, {}
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            _status, stored = self.api(user, "GET", f"/api/v1/chats/{chat_id}")
            message = (((stored or {}).get("chat") or {}).get("history") or {}).get("messages", {}).get(aid, {})
            if message.get("done"):
                break
            time.sleep(3)
        sources = message.get("sources") or []
        return {"chat_id": chat_id, "completion_status": status, "done": message.get("done"),
                "content_chars": len(message.get("content") or ""),
                "status_events": len(message.get("statusHistory") or []),
                "source_chips": len(sources),
                "source_documents": sum(len(s.get("document") or []) for s in sources)}


def cmd_secrets(state: str) -> None:
    os.makedirs(state, mode=0o700, exist_ok=True)
    created = []
    for user in USERS:
        path = os.path.join(state, f"{user}.password")
        if not os.path.exists(path):
            _private_write(path, secrets.token_urlsafe(18) + "\n")
            created.append(user)
    path = os.path.join(state, "secrets.json")
    if not os.path.exists(path):
        _private_write(path, json.dumps({"webui": secrets.token_hex(32), "client": secrets.token_urlsafe(32)}))
        created.append("secrets.json")
    # One file per Secret key, for `kubectl create secret --from-file` (never argv).
    with open(path, encoding="utf-8") as fh:
        values = json.load(fh)
    secret_dir = os.path.join(state, "owui-secret")
    os.makedirs(secret_dir, mode=0o700, exist_ok=True)
    _private_write(os.path.join(secret_dir, "webui-secret-key"), values["webui"])
    _private_write(os.path.join(secret_dir, "oidc-client-secret"), values["client"])
    out({"created": created, "users": list(USERS)})


def cmd_users(state: str) -> None:
    del state
    print("\n".join(USERS))


def cmd_dex_config(state: str, hashes_path: str) -> None:
    with open(os.path.join(state, "secrets.json"), encoding="utf-8") as fh:
        client_secret = json.load(fh)["client"]
    with open(hashes_path, encoding="utf-8") as fh:
        hashes = fh.read().split()
    if len(hashes) != len(USERS):
        raise SystemExit("hash count does not match USERS")
    redirect = OWUI_URL + "/oauth/oidc/callback"
    lines = [
        f"issuer: {DEX_ISSUER}",
        "storage: {type: memory}",
        "web: {https: 0.0.0.0:5556, tlsCert: /etc/dex-tls/tls.crt, tlsKey: /etc/dex-tls/tls.key}",
        "oauth2: {skipApprovalScreen: true, responseTypes: [code]}",
        "enablePasswordDB: true",
        "staticClients:",
        "  - id: open-webui",
        "    name: Open WebUI",
        f"    secret: {json.dumps(client_secret)}",
        f"    redirectURIs: [{json.dumps(redirect)}]",
        "staticPasswords:",
    ]
    for (name, (group, uid)), digest in zip(USERS.items(), hashes, strict=True):
        lines += [f"  - email: {name}@{EMAIL_DOMAIN}", f"    hash: {json.dumps(digest)}",
                  f"    username: {name}", f"    userID: {uid}", f"    groups: [{group}]"]
    print("\n".join(lines))


def cmd_login(state: str, user: str) -> None:
    s = Session(state)
    result = s.login(user)
    if result.get("signed_in"):
        result.update(s.whoami(user))
    out(result)
    if not result.get("signed_in"):
        sys.exit(1)


def cmd_api_key(state: str) -> None:
    s = Session(state)
    result = s.login(ADMIN)
    if not result.get("signed_in"):
        out(result)
        sys.exit(1)
    me = s.whoami(ADMIN)
    if me.get("role") != "admin":
        out({"user": ADMIN, "role": me.get("role"), "api_key": "refused: not admin"})
        sys.exit(1)
    status, body = s.api(ADMIN, "POST", "/api/v1/auths/api_key")
    key = (body or {}).get("api_key") if isinstance(body, dict) else None
    if status != 200 or not key:
        out({"api_key_status": status})
        sys.exit(1)
    _private_write(os.path.join(state, "admin-api-key"), key + "\n")
    out({"user": ADMIN, "role": "admin", "api_key_status": status, "api_key_file": "admin-api-key"})



def cmd_access(state: str) -> None:
    s = Session(state)
    if not s.login(ADMIN).get("signed_in"):
        raise SystemExit("admin sign-in failed")
    ids = s.group_ids()
    created = {}
    for name in DOCS_GROUPS:
        if name not in ids:
            created[name] = s.api(ADMIN, "POST", "/api/v1/groups/create",
                                  {"name": name, "description": "hands-on test group (IdP claim)"})[0]
    ids = s.group_ids()
    missing = [g for g in DOCS_GROUPS if not ids.get(g)]
    if missing:
        out({"groups_created": created, "missing": missing})
        sys.exit(1)
    grants = [{"principal_type": "group", "principal_id": ids[g], "permission": "read"} for g in DOCS_GROUPS]
    status = s.api(ADMIN, "POST", "/api/v1/models/model/access/update",
                   {"id": DOCS_MODEL, "name": "Docs", "access_grants": grants})[0]
    out({"groups_created": created, "groups": sorted(DOCS_GROUPS), "docs_model_access_update": status})
    if status != 200:
        sys.exit(1)


def cmd_seed_function(state: str, pipe_path: str) -> None:
    """Workaround, acceptance lane only (OWUI_SEED_FUNCTION=1): create the Docs Function through the admin
    session so the chart's installer finds it. Open WebUI v0.12.3 answers HTTP 401, not 404, for a Function
    that does not exist, and provision_pipe.py reads 401 as a refused key, so its first install fails. The
    installer still verifies the hash and activates; remove this once it handles the absent Function."""
    s = Session(state)
    if not s.login(ADMIN).get("signed_in"):
        raise SystemExit("admin sign-in failed")
    with open(pipe_path, encoding="utf-8") as fh:
        source = fh.read()
    status = s.api(ADMIN, "POST", "/api/v1/functions/create",
                   {"id": DOCS_MODEL, "name": "Docs", "content": source, "meta": {"description": "seed"}})[0]
    out({"seed_function_status": status})
    if status != 200:
        sys.exit(1)


def cmd_smoke(state: str) -> None:
    s = Session(state)
    report: dict[str, Any] = {"users": [], "ok": True}
    for user in ("team-user", "docs-user"):
        login = s.login(user)
        entry = {**login, **(s.whoami(user) if login.get("signed_in") else {})}
        entry["docs_only"] = entry.get("models") == [DOCS_MODEL]
        entry["group_ok"] = USERS[user][0] in entry.get("groups", [])
        # group_ok is part of ok: the IdP group claim must map to the Open WebUI group (N8 of #1095).
        report["ok"] &= (bool(login.get("signed_in")) and entry["docs_only"] and entry["group_ok"]
                         and entry.get("role") == "user")
        report["users"].append(entry)
    qpath = os.path.join(state, "smoke-question.txt")
    try:
        with open(qpath, encoding="utf-8") as fh:
            question = fh.read().strip()
    except OSError:
        question = ""
    if question:
        chat = s.chat("docs-user", DOCS_MODEL, question)
        report["chat"] = chat
        report["ok"] &= bool(chat.get("done")) and chat.get("source_chips", 0) > 0
    else:
        report["chat"] = "skipped: write one docs question to STATE/smoke-question.txt (mode 600)"
        report["ok"] = False
    blocked = {}
    for path in BLOCKED:
        status, body = s.raw("docs-user", "POST", path)
        blocked[path] = "proxy-403" if status == 403 and body == b"denied\n" else f"HTTP {status}"
    report["blocked"] = blocked
    report["blocked_all_proxy_403"] = all(v == "proxy-403" for v in blocked.values())
    report["allowed_control"] = s.api("docs-user", "GET", "/api/models")[0]
    report["ok"] &= report["blocked_all_proxy_403"] and report["allowed_control"] == 200
    out(report)
    if not report["ok"]:
        sys.exit(1)


def cmd_health(state: str) -> None:
    s = Session(state)
    result = {}
    for name, url in (("owui_health", OWUI_URL + "/health"),
                      ("dex_discovery", DEX_ISSUER + "/.well-known/openid-configuration")):
        try:
            with urllib.request.urlopen(url, timeout=10, context=s.ctx) as resp:
                result[name] = resp.status
        except urllib.error.HTTPError as exc:
            result[name] = exc.code
        except (OSError, ssl.SSLError) as exc:
            result[name] = f"unreachable: {type(exc).__name__}"
    out(result)
    if any(v != 200 for v in result.values()):
        sys.exit(1)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    command, state, rest = argv[0], argv[1], argv[2:]
    handlers: dict[str, Any] = {"secrets": cmd_secrets, "users": cmd_users, "dex-config": cmd_dex_config, "login": cmd_login,
                "api-key": cmd_api_key, "seed-function": cmd_seed_function, "access": cmd_access, "smoke": cmd_smoke, "health": cmd_health}
    if command not in handlers:
        print(__doc__, file=sys.stderr)
        return 2
    handlers[command](state, *rest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
