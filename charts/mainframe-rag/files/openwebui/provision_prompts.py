"""Converge Open WebUI's shared prompt templates to the repository file (#1255).

The templates are the workspace Prompts that every signed-in user can insert in
chat with a `/` command. They are repository-owned: users cannot create, import
or edit a prompt (the chart pins USER_PERMISSIONS_WORKSPACE_PROMPTS_ACCESS and
_IMPORT to false; stored XSS through prompts was CVE-2025-64495), and the Route
reaches no prompt write path (ADR-0007 E1: those paths are admitted only from
the pod's loopback, where this installer runs).

Runs inside the Open WebUI pod (deploy.sh: kubectl exec -i ... python3
/opt/mainframe-rag/openwebui/provision_prompts.py PROMPTS_SHA256 INSTALLER_SHA256).
The admin API key arrives on stdin, never in argv, the environment or the pod spec.

  1. This installer and the mounted prompts.json must hash to the sha256 that
     deploy.sh computed from the repository files. A ConfigMap mount trails the
     release by up to a minute (#1252), so a mismatch is refused with a re-run
     message before the key is sent anywhere.
  2. The file is validated: an object {"prompts": [...]}, each entry exactly
     {command, name, content}; commands are lowercase words joined by hyphens,
     unique, and not one of the chat's built-in `/` commands; content holds no
     markup ('<') and no control characters other than newline and tab.
  3. The key is sent only to this pod's own serving certificate on
     127.0.0.1:8443, and proven an admin key on the admin-only Function listing.
  4. Every prompt is listed (all pages). A prompt is repository-managed when its
     meta carries "mainframe_rag_managed": true. A repository command held by an
     unmanaged prompt is refused before any write: the installer never adopts,
     changes or deletes a prompt it does not manage.
  5. Managed prompts are created (with grants: public read only), updated
     (name and content through the update route; grants, when they drifted,
     only through the access route, the one place v0.12.4 and later change
     sharing), re-activated, or deleted when the repository no longer lists
     them. An unchanged prompt is not written (a text update adds a history row
     upstream). The result is listed again and must equal the repository set.

Output and errors are fixed texts plus Open WebUI's HTTP status and counts;
response bodies, the key and the template text are never printed. Endpoints
are those of the pinned release v0.13.4 (routers/prompts.py). Stdlib only.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import ssl
import sys
from pathlib import Path

MOUNT = Path("/opt/mainframe-rag/openwebui")
SELF = Path(__file__)
CERT = Path("/etc/tls/private/tls.crt")
HOST = "127.0.0.1"
PORT = 8443
TIMEOUT_S = 30
MANAGED = "mainframe_rag_managed"
PUBLIC_READ = {"principal_type": "user", "principal_id": "*", "permission": "read"}
COMMAND = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Built-in `/` commands of the chat input (v0.13.4 MessageInput.svelte).
# v0.13.x adds /side (also matched by "btw") and /comments.
BUILT_IN = frozenset({"compact", "fork", "status", "model", "compare", "settings", "temporary",
                      "side", "btw", "comments"})
PROMPT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_PROMPTS = 50
MAX_COMMAND = 32
MAX_NAME = 80
MAX_CONTENT = 4000
MAX_PAGES = 100
RERUN = "wait a minute, then re-run airgap:deploy"


class ProvisionError(Exception):
    """A fixed, operator-facing refusal (never carries response or template text)."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leaf_der(pem_text: str) -> bytes:
    begin = "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    start = pem_text.find(begin)
    stop = pem_text.find(end, start)
    if start < 0 or stop < 0:
        raise ProvisionError("serving certificate is missing or not PEM")
    return ssl.PEM_cert_to_DER_cert(pem_text[start:stop + len(end)])


class Client:
    """HTTPS to this pod's own listener, pinned to its serving certificate."""

    def __init__(self, key: str, pinned_der: bytes, host: str = HOST, port: int = PORT) -> None:
        self._key = key
        self._pinned = pinned_der
        self._host = host
        self._port = port

    def _connect(self) -> http.client.HTTPSConnection:
        context = ssl.create_default_context()
        # Hostname and chain are replaced by an exact pin of the pod's own leaf.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(self._host, self._port, timeout=TIMEOUT_S,
                                           context=context)
        try:
            conn.connect()
        except OSError:
            conn.close()
            raise ProvisionError("Open WebUI is unreachable on 127.0.0.1:8443") from None
        sock = conn.sock
        peer = sock.getpeercert(binary_form=True) if isinstance(sock, ssl.SSLSocket) else None
        if peer != self._pinned:
            conn.close()
            raise ProvisionError("the listener on 127.0.0.1:8443 does not present this pod's "
                                 "serving certificate; the admin key was not sent")
        return conn

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        conn = self._connect()
        try:
            headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException):
            raise ProvisionError(f"{method} {path} failed: Open WebUI is unreachable") from None
        finally:
            conn.close()
        if status != 200:
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError:
            raise ProvisionError(f"{method} {path} returned a non-JSON body") from None


def _text_ok(value: str, *, newlines: bool) -> bool:
    allowed = "\n\t" if newlines else ""
    return not any((ord(c) < 32 and c not in allowed) or 0x7f <= ord(c) < 0xa0 for c in value)


def load_prompts(raw: bytes) -> list[dict]:
    """Validate the repository file; return [{command, name, content}, ...]."""
    def bad(reason: str) -> ProvisionError:
        return ProvisionError(f"the repository prompts file is invalid: {reason}; nothing was changed")

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise bad("not JSON (UTF-8)") from None
    if not isinstance(document, dict) or set(document) != {"prompts"} or \
            not isinstance(document["prompts"], list):
        raise bad('the top level must be exactly {"prompts": [...]}')
    entries = document["prompts"]
    if len(entries) > MAX_PROMPTS:
        raise bad(f"more than {MAX_PROMPTS} prompts")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"entry {index}"
        if not isinstance(entry, dict) or set(entry) != {"command", "name", "content"}:
            raise bad(f"{where} must have exactly the keys command, name, content")
        command, name, content = entry["command"], entry["name"], entry["content"]
        if not isinstance(command, str) or len(command) > MAX_COMMAND or not COMMAND.fullmatch(command):
            raise bad(f"{where} command must be lowercase letters and digits joined by single "
                      f"hyphens, at most {MAX_COMMAND} characters, without a leading /")
        if command in BUILT_IN:
            raise bad(f"{where} command is a built-in chat command")
        if command in seen:
            raise bad(f"{where} command is a duplicate")
        seen.add(command)
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME or \
                not _text_ok(name, newlines=False):
            raise bad(f"{where} name must be 1 to {MAX_NAME} characters of one line")
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT:
            raise bad(f"{where} content must be 1 to {MAX_CONTENT} characters")
        if not _text_ok(content, newlines=True):
            raise bad(f"{where} content holds a control character")
        if "<" in content or "<" in name:
            raise bad(f"{where} holds markup ('<'); templates are plain text")
    return [dict(entry) for entry in entries]


FUNCTIONS_LIST = "/api/v1/functions/list"
PROMPTS_LIST = "/api/v1/prompts/list"


def _prove_admin(client: Client) -> None:
    """The admin-only Function listing: its 401/403 can only mean the key was refused
    (the prompt listing answers 200 to any signed-in user)."""
    status, _ = client.call("GET", FUNCTIONS_LIST)
    if status in (401, 403):
        raise ProvisionError(f"GET {FUNCTIONS_LIST} refused the admin API key (HTTP {status})")
    if status != 200:
        raise ProvisionError(f"cannot prove the admin API key (HTTP {status})")


def _list(client: Client) -> list[dict]:
    """Every prompt, active or not, across all pages; fail closed when the pages
    do not add up to the reported total."""
    found: dict[str, dict] = {}
    total = None
    for page in range(1, MAX_PAGES + 1):
        status, document = client.call("GET", f"{PROMPTS_LIST}?page={page}")
        if status != 200 or not isinstance(document, dict) or \
                not isinstance(document.get("items"), list) or not isinstance(document.get("total"), int):
            raise ProvisionError(f"cannot list prompts (HTTP {status})")
        total = document["total"]
        for item in document["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or \
                    not isinstance(item.get("command"), str):
                raise ProvisionError("the prompt listing has an unexpected shape; nothing was changed")
            found[item["id"]] = item
        if not document["items"] or len(found) >= total:
            break
    if total is None or len(found) != total:
        raise ProvisionError("the prompt listing did not add up to its total; nothing was changed; "
                             "re-run airgap:deploy")
    return list(found.values())


def _managed(item: dict) -> bool:
    meta = item.get("meta")
    return isinstance(meta, dict) and meta.get(MANAGED) is True


def _grants(item: dict) -> set[tuple]:
    grants = item.get("access_grants")
    if not isinstance(grants, list):
        return {("?",)}
    return {(g.get("principal_type"), g.get("principal_id"), g.get("permission")) if isinstance(g, dict)
            else ("?",) for g in grants}


WANT_GRANTS = {(PUBLIC_READ["principal_type"], PUBLIC_READ["principal_id"], PUBLIC_READ["permission"])}


def _matches(item: dict, want: dict) -> bool:
    return (item.get("name") == want["name"] and item.get("content") == want["content"]
            and _grants(item) == WANT_GRANTS and item.get("is_active") is True and _managed(item))


def _form(want: dict) -> dict:
    return {"command": want["command"], "name": want["name"], "content": want["content"],
            "meta": {MANAGED: True}, "tags": [], "access_grants": [dict(PUBLIC_READ)]}


def _id(item: dict) -> str:
    pid = item["id"]
    if not PROMPT_ID.fullmatch(pid):
        raise ProvisionError("a managed prompt has an unexpected id; nothing was changed")
    return pid


def provision(client: Client, raw: bytes, expected: str) -> list[str]:
    """Converge the managed prompts to the repository file; return fixed status lines."""
    if sha256_hex(raw) != expected:
        raise ProvisionError("the mounted prompts.json differs from this release's repository file "
                             f"(the ConfigMap mount can trail the release); nothing was changed: {RERUN}")
    wanted = load_prompts(raw)
    _prove_admin(client)
    current = _list(client)
    by_command: dict[str, list[dict]] = {}
    for item in current:
        by_command.setdefault(item["command"], []).append(item)

    # Plan everything before the first write.
    creates, updates, unchanged = [], [], 0
    for want in wanted:
        held = by_command.get(want["command"], [])
        if any(not _managed(item) for item in held) or len(held) > 1:
            raise ProvisionError("a repository command is taken by a prompt this installer does not "
                                 "manage; rename or remove that prompt, then re-run; nothing was changed")
        if not held:
            creates.append(want)
        elif _matches(held[0], want):
            unchanged += 1
        else:
            updates.append((_id(held[0]), held[0], want))
    listed = {want["command"] for want in wanted}
    removals = [_id(item) for item in current if _managed(item) and item["command"] not in listed]

    for want in creates:
        status, _ = client.call("POST", "/api/v1/prompts/create", _form(want))
        if status != 200:
            raise ProvisionError(f"cannot create a shared prompt (HTTP {status})")
    for pid, item, want in updates:
        if (item.get("name"), item.get("content"), _managed(item)) != (want["name"], want["content"], True):
            status, _ = client.call("POST", f"/api/v1/prompts/id/{pid}/update", _form(want))
            if status != 200:
                raise ProvisionError(f"cannot update a shared prompt (HTTP {status})")
        if _grants(item) != WANT_GRANTS:
            # The update route ignores grants since v0.12.4: sharing changes only here.
            status, _ = client.call("POST", f"/api/v1/prompts/id/{pid}/access/update",
                                    {"access_grants": [dict(PUBLIC_READ)]})
            if status != 200:
                raise ProvisionError(f"cannot restore a shared prompt's read access (HTTP {status})")
    for pid in removals:
        status, document = client.call("DELETE", f"/api/v1/prompts/id/{pid}/delete")
        if status != 200 or document is not True:
            raise ProvisionError(f"cannot remove a shared prompt (HTTP {status})")

    # Re-activate after the writes, from the stored state (a toggle flips).
    for item in _list(client):
        if _managed(item) and item["command"] in listed and item.get("is_active") is not True:
            status, document = client.call("POST", f"/api/v1/prompts/id/{_id(item)}/toggle")
            if status != 200 or not isinstance(document, dict) or document.get("is_active") is not True:
                raise ProvisionError(f"cannot activate a shared prompt (HTTP {status})")

    stored = {item["command"]: item for item in _list(client) if _managed(item)}
    if set(stored) != listed or any(not _matches(stored[w["command"]], w) for w in wanted):
        raise ProvisionError("the stored shared prompts do not match the repository file after the write")
    summary = (f"shared prompts converged: {len(wanted)} managed ({len(creates)} created, "
               f"{len(updates)} updated, {len(removals)} removed, {unchanged} unchanged), "
               f"sha256 {expected}")
    return [summary]


def _is_sha(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 2 or not all(_is_sha(a) for a in argv):
            raise ProvisionError("usage: provision_prompts.py PROMPTS_SHA256 INSTALLER_SHA256 "
                                 "(admin API key on stdin)")
        try:
            own = SELF.read_bytes()
        except OSError:
            raise ProvisionError("cannot read the mounted installer") from None
        if sha256_hex(own) != argv[1]:
            raise ProvisionError("the mounted installer differs from this release's repository file "
                                 f"(the ConfigMap mount can trail the release); nothing was changed: {RERUN}")
        try:
            raw = (MOUNT / "prompts.json").read_bytes()
        except OSError:
            raise ProvisionError(f"the mounted prompts.json is missing; nothing was changed: {RERUN}") from None
        key = sys.stdin.readline().strip()
        if not key:
            raise ProvisionError("no admin API key on stdin")
        try:
            pinned = leaf_der(CERT.read_text(encoding="ascii"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise ProvisionError("cannot read the serving certificate") from None
        for line in provision(Client(key, pinned, HOST, PORT), raw, argv[0]):
            print(line)
    except ProvisionError as exc:
        print(f"FAIL: shared prompt provisioning refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
