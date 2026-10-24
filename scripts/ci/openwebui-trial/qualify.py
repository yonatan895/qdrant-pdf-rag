"""Open WebUI Kind trial driver (issue #1073): the browser stand-in. Trial-only.

Runs inside the trial's `browser` pod, standard library only:

    kubectl -n owui-trial exec -i browser -- python3 - COMMAND [ARGS] < qualify.py

It signs in through the real OIDC redirect chain (Open WebUI -> Dex login form
-> Open WebUI callback) with a cookie jar, as a browser does, and then calls
Open WebUI's HTTP API with the session token. Tokens stay in the pod's /tmp
(mode 0600); passwords come from the mounted run-time Secret /secrets. Output
is JSON with ids, counts and states; it prints no token, password or key.

Commands:
  login USER                 sign in (USER = admin | alice | bob); prints role and groups
  setup                      admin: install the Docs Pipe and a hidden control Pipe, grant Docs to docs-readers
  models USER                model ids USER can see
  chat USER MODEL TEXT       one saved chat turn through the UI's completion path; prints the stored message
  chats USER                 the user's chat ids and titles
  task USER                  call the title/tags/follow-up task endpoints directly
  refusals USER              upload, knowledge, memory and signup attempts (all must be refused)
  config-get / config-set K V  admin config read / live edit (check h)
  login-form                 the password sign-in path (must be refused)
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "http://openwebui.owui-trial.svc.cluster.local:8080"
STATE = "/tmp/q"
USERS = {"admin": "admin@trial.test", "alice": "alice@trial.test", "bob": "bob@trial.test"}


def out(obj) -> None:
    print(json.dumps(obj, sort_keys=True))


class _NoSecretRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects like a browser (303/302 become GET)."""


def _opener(jar):
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoSecretRedirect())


def _token_path(user: str) -> str:
    return f"{STATE}/{user}.token"


def token(user: str) -> str:
    with open(_token_path(user), encoding="utf-8") as fh:
        return fh.read().strip()


def api(user: str | None, method: str, path: str, body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if user:
        req.add_header("Authorization", f"Bearer {token(user)}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = {"non_json_bytes": len(raw)}
    return status, parsed


def login(user: str) -> None:
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    with open(f"/secrets/{user}", encoding="utf-8") as fh:
        password = fh.read().strip()
    jar = http.cookiejar.CookieJar()
    op = _opener(jar)
    # 1. Open WebUI -> IdP authorize -> local connector -> login form.
    with op.open(BASE + "/oauth/oidc/login", timeout=30) as resp:
        form_url = resp.geturl()
        html = resp.read().decode("utf-8", "replace")
    hops = {"login_form_host": urllib.parse.urlsplit(form_url).netloc}
    action = re.search(r'<form[^>]*action="([^"]+)"', html)
    post_url = urllib.parse.urljoin(form_url, action.group(1).replace("&amp;", "&")) if action else form_url
    # 2. Post the credentials; the IdP redirects to Open WebUI's callback.
    data = urllib.parse.urlencode({"login": USERS[user], "password": password}).encode()
    with op.open(urllib.request.Request(post_url, data=data, method="POST"), timeout=30) as resp:
        final = resp.geturl()
        resp.read()
    hops["final_path"] = urllib.parse.urlsplit(final).path
    tok = next((c.value for c in jar if c.name == "token"), None)
    if not tok:
        out({"user": user, "signed_in": False, **hops})
        sys.exit(1)
    fd = os.open(_token_path(user), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    status, me = api(user, "GET", "/api/v1/auths/")
    groups = []
    if status == 200:
        gstatus, glist = api(user, "GET", "/api/v1/groups/")
        if gstatus == 200 and isinstance(glist, list):
            groups = sorted(g.get("name") for g in glist)
    out({"user": user, "signed_in": status == 200, "role": (me or {}).get("role"),
         "groups_visible": groups, "cookies": sorted(c.name for c in jar), **hops})


def _group_id(name: str) -> str | None:
    _status, groups = api("admin", "GET", "/api/v1/groups/")
    for g in groups or []:
        if g.get("name") == name:
            return g["id"]
    return None


HIDDEN_PIPE = '''"""
title: Hidden control
version: 0.0.1
description: Trial-only control model (issue #1073); never granted to any group. Reports which reserved arguments Open WebUI passes.
"""
class Pipe:
    async def pipe(self, body: dict, __user__=None, __task__=None, __metadata__=None, __event_emitter__=None) -> str:
        if __event_emitter__ is not None:
            await __event_emitter__({"type": "status", "data": {"description": "control status", "done": True}})
            await __event_emitter__({"type": "source", "data": {"source": {"name": "control-source", "id": "c1"},
                                     "document": ["control excerpt"], "metadata": [{"source": "control"}]}})
        meta = __metadata__ or {}
        return "probe task=%s metadata_task=%s metadata_keys=%s emitter=%s" % (
            __task__, meta.get("task"), ",".join(sorted(meta)), __event_emitter__ is not None)
'''


def setup() -> None:
    result = {}
    with open("/pipes/docs_pipe.py", encoding="utf-8") as fh:
        docs_src = fh.read()
    with open("/pipes/agent_url", encoding="utf-8") as fh:
        agent_url = fh.read().strip()
    for fid, name, src in (("docs", "Docs", docs_src), ("hidden_control", "Hidden control", HIDDEN_PIPE)):
        status, _ = api("admin", "GET", f"/api/v1/functions/id/{fid}")
        if status != 200:
            status, _created = api("admin", "POST", "/api/v1/functions/create", {
                "id": fid, "name": name, "content": src, "meta": {"description": name}})
            result[f"create_{fid}"] = status
        status, fn = api("admin", "GET", f"/api/v1/functions/id/{fid}")
        if status == 200 and not fn.get("is_active"):
            result[f"toggle_{fid}"] = api("admin", "POST", f"/api/v1/functions/id/{fid}/toggle")[0]
    result["docs_valves"] = api("admin", "POST", "/api/v1/functions/id/docs/valves/update",
                                {"AGENT_URL": agent_url})[0]
    gid = _group_id("docs-readers")
    result["docs_readers_group"] = bool(gid)
    grants = [{"principal_type": "group", "principal_id": gid, "permission": "read"}] if gid else []
    result["docs_access"] = api("admin", "POST", "/api/v1/models/model/access/update",
                                {"id": "docs", "name": "Docs", "access_grants": grants})[0]
    result["hidden_access"] = api("admin", "POST", "/api/v1/models/model/access/update",
                                  {"id": "hidden_control", "name": "Hidden control", "access_grants": []})[0]
    out(result)


def models(user: str) -> None:
    status, body = api(user, "GET", "/api/models")
    ids = sorted(m["id"] for m in (body or {}).get("data", [])) if status == 200 else []
    out({"user": user, "status": status, "models": ids})


def chat(user: str, model: str, text: str) -> None:
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    now = int(time.time())
    umsg = {"id": uid, "parentId": None, "childrenIds": [aid], "role": "user", "content": text,
            "timestamp": now, "models": [model]}
    amsg = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant", "content": "",
            "model": model, "modelName": model, "modelIdx": 0, "timestamp": now}
    status, created = api(user, "POST", "/api/v1/chats/new", {"chat": {
        "title": "New Chat", "models": [model], "params": {},
        "history": {"messages": {uid: umsg, aid: amsg}, "currentId": aid},
        "messages": [umsg, amsg], "tags": [], "timestamp": now * 1000}})
    if status != 200:
        out({"chat_create": status})
        sys.exit(1)
    chat_id = created["id"]
    # The UI's request shape, including the background tasks it asks for.
    status, resp = api(user, "POST", "/api/chat/completions", {
        "model": model, "stream": True, "messages": [{"role": "user", "content": text}],
        "chat_id": chat_id, "id": aid, "parent_id": uid, "session_id": None,
        "features": {}, "params": {},
        "background_tasks": {"title_generation": True, "tags_generation": True,
                             "follow_up_generation": True}}, timeout=180)
    message = {}
    for _ in range(90):
        gstatus, stored = api(user, "GET", f"/api/v1/chats/{chat_id}")
        message = (((stored or {}).get("chat") or {}).get("history") or {}).get("messages", {}).get(aid, {})
        if message.get("done") or (message.get("content") and gstatus == 200 and status == 200
                                   and not isinstance(resp, dict)):
            break
        time.sleep(2)
    sources = message.get("sources") or []
    out({"chat_id": chat_id, "completion_status": status,
         "completion_shape": sorted(resp) if isinstance(resp, dict) else type(resp).__name__,
         "title": (stored or {}).get("title"), "done": message.get("done"),
         "content_chars": len(message.get("content") or ""),
         "status_history": [s.get("description") for s in (message.get("statusHistory") or [])],
         "follow_ups": len(message.get("followUps") or []),
         "sources": [{"name": (s.get("source") or {}).get("name"),
                      "documents": len(s.get("document") or []),
                      "metadata": len(s.get("metadata") or []),
                      "excerpt_chars": sum(len(d) for d in (s.get("document") or []))} for s in sources],
         "tags": (stored or {}).get("meta", {}).get("tags")})


def chats(user: str) -> None:
    status, body = api(user, "GET", "/api/v1/chats/list")
    out({"user": user, "status": status, "chats": [c.get("id") for c in (body or [])]})


def task(user: str) -> None:
    res = {}
    for name in ("title", "tags", "follow_up", "queries", "auto", "emoji", "image_prompt", "moa"):
        status, body = api(user, "POST", f"/api/v1/tasks/{name}/completions", {
            "model": "docs", "messages": [{"role": "user", "content": "trial task probe"}],
            "prompt": "trial task probe", "chat_id": "local:trial"})
        text = ""
        if isinstance(body, dict) and body.get("choices"):
            text = (body["choices"][0].get("message") or {}).get("content") or ""
        res[name] = {"status": status, "keys": sorted(body) if isinstance(body, dict) else None,
                     "content": text if text in ("", "Docs question") else f"<{len(text)} chars>"}
    out(res)


def refusals(user: str) -> None:
    res = {}
    boundary = "trial1073"
    payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.txt\"\r\n"
               f"Content-Type: text/plain\r\n\r\ntrial upload body\r\n--{boundary}--\r\n").encode()
    req = urllib.request.Request(BASE + "/api/v1/files/", data=payload, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token(user)}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res["upload"] = resp.status
    except urllib.error.HTTPError as exc:
        res["upload"] = exc.code
    res["knowledge_create"] = api(user, "POST", "/api/v1/knowledge/create",
                                  {"name": "trial", "description": "trial"})[0]
    res["memory_add"] = api(user, "POST", "/api/v1/memories/add", {"content": "trial memory"})[0]
    res["retrieval_query"] = api(user, "POST", "/api/v1/retrieval/query/collection",
                                 {"collection_names": ["trial"], "query": "trial"})[0]
    res["web_search"] = api(user, "POST", "/api/v1/retrieval/process/web/search", {"queries": ["trial"]})[0]
    res["signup"] = api(None, "POST", "/api/v1/auths/signup",
                        {"name": "x", "email": "x@trial.test", "password": "x" * 16})[0]
    res["community_share"] = api(user, "GET", "/api/config")[1].get("features", {}).get(
        "enable_community_sharing")
    out(res)


def login_form() -> None:
    out({"signin": api(None, "POST", "/api/v1/auths/signin",
                       {"email": USERS["alice"], "password": "not-the-password"})[0],
         "config_login_form": (api(None, "GET", "/api/config")[1] or {}).get("features", {}).get(
             "enable_login_form")})


def config_get(key: str) -> None:
    status, cfg = api("admin", "GET", "/api/v1/auths/admin/config")
    out({"status": status, key: (cfg or {}).get(key)})


def config_set(key: str, value: str) -> None:
    status, cfg = api("admin", "GET", "/api/v1/auths/admin/config")
    if status != 200:
        out({"get": status})
        sys.exit(1)
    cfg[key] = value
    status, new = api("admin", "POST", "/api/v1/auths/admin/config", cfg)
    out({"set_status": status, key: (new or {}).get(key)})


def pipe_hash() -> None:
    import hashlib
    with open("/pipes/docs_pipe.py", "rb") as fh:
        repo = hashlib.sha256(fh.read()).hexdigest()
    status, fn = api("admin", "GET", "/api/v1/functions/id/docs")
    stored = hashlib.sha256((fn or {}).get("content", "").encode()).hexdigest() if status == 200 else None
    out({"status": status, "repo_sha256": repo, "stored_sha256": stored, "match": repo == stored,
         "active": (fn or {}).get("is_active"), "type": (fn or {}).get("type")})


def task_direct(user: str, model: str) -> None:
    """Call the title task endpoint for MODEL and print the generated text length/prefix class."""
    status, body = api(user, "POST", "/api/v1/tasks/title/completions", {
        "model": model, "messages": [{"role": "user", "content": "trial task probe"},
                                     {"role": "assistant", "content": "trial answer"}],
        "chat_id": "local:trial"}, timeout=120)
    text = ""
    if isinstance(body, dict):
        choices = body.get("choices") or []
        if choices:
            text = (choices[0].get("message") or {}).get("content") or ""
    out({"status": status, "keys": sorted(body) if isinstance(body, dict) else None,
         "text": text if model == "hidden_control" or text == "Docs question" else f"<{len(text)} chars>"})


def main(argv: list[str]) -> int:
    cmd, args = argv[0], argv[1:]
    table = {"login": login, "setup": setup, "models": models, "chat": chat, "chats": chats,
             "task": task, "refusals": refusals, "login-form": login_form,
             "config-get": config_get, "config-set": config_set,
             "pipe-hash": pipe_hash, "task-direct": task_direct}
    table[cmd](*args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
