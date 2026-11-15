"""Install the repository docs Pipe into Open WebUI, hash-checked (ADR-0007 V2, #1074).

Runs inside the Open WebUI pod (deploy.sh: kubectl exec -i ... python3
/opt/mainframe-rag/openwebui/provision_pipe.py EXPECTED_SHA256). The admin API
key arrives on stdin, never in argv, the environment or the pod spec.

  1. The mounted Pipe (the chart ConfigMap) must hash to EXPECTED_SHA256, the
     sha256 deploy.sh computed from the repository file.
  2. The server certificate on 127.0.0.1:8443 must be this pod's own serving
     certificate (pinned by DER bytes) before the key is sent.
  3. The key is proven to be an admin key, and the installed Functions are
     listed, by the admin-only listing GET /api/v1/functions/list: its 401/403
     can only mean the key was refused. No other Function may exist (every
     Function can read the user's tokens).
  3b. The Function is created when that listing does not contain it, or updated
     when its stored content differs, and then read back: its stored content must
     hash to EXPECTED_SHA256. Open WebUI v0.12.3 answers HTTP 401, not 404, for
     an absent Function (routers/functions.py get_function_by_id) and also 401 for
     an unknown or non-admin key (utils/auth.py), so presence is decided by the
     listing, never by a 401 on the Function itself.
  4. The Function is switched active when it is not (a toggle is applied only
     after reading the current state, so a repeat run converges).
  5. Ratings (#1255): when the chart mounts rating_action.py, the repository
     rating Action is converged the same way, plus switched global (a
     Function-backed model shows only global Actions). Its expected sha256 is
     ACTION_SHA256 below, pinned by a test to the repository file, because
     deploy.sh passes only the Pipe's hash. When the Action is not mounted
     (ratings off) an installed one is deleted. Any Function outside this
     repository set is still refused, and a repository id holding another
     kind of Function is never adopted.

Output and errors are fixed texts plus Open WebUI's HTTP status; response
bodies, the key and the Pipe content are never printed. The Open WebUI
endpoints are those of the pinned release (pending #1073 qualification).
Stdlib only: the image's own interpreter runs it.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import ssl
import sys
from pathlib import Path

PIPE_ID = "mainframe_rag_docs"
PIPE_NAME = "Rosetta"
PIPE_DESCRIPTION = "Documentation corpus through the mainframe-rag agent"
ACTION_ID = "mainframe_rag_rating"
ACTION_NAME = "Rosetta rating"
ACTION_DESCRIPTION = "Rate a Rosetta answer; stored by the agent's feedback store only"
# sha256 of charts/mainframe-rag/files/openwebui/rating_action.py (a test pins it).
ACTION_SHA256 = "4053f91ce37492712c455bd17440443d92afb091fd0d3a64c1aacea097c40abe"
MOUNT = Path("/opt/mainframe-rag/openwebui")
CERT = Path("/etc/tls/private/tls.crt")
HOST = "127.0.0.1"
PORT = 8443
TIMEOUT_S = 30


class ProvisionError(Exception):
    """A fixed, operator-facing refusal (never carries response text)."""


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

    def call(self, method: str, path: str,
             body: dict | None = None) -> tuple[int, dict | list | None]:
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
            document = json.loads(raw)
        except ValueError:
            raise ProvisionError(f"{method} {path} returned a non-JSON body") from None
        return status, document if isinstance(document, (dict, list)) else None


LIST_PATH = "/api/v1/functions/list"
# The repository Function set: id -> Open WebUI type, and the word in messages.
KINDS = {PIPE_ID: "pipe", ACTION_ID: "action"}
LABELS = {PIPE_ID: "Pipe", ACTION_ID: "Action"}


def _listing(client: Client) -> dict[str, str]:
    """List the Functions through the admin-only listing: {id: type}. The listing
    has no not-found answer, so its 401/403 is the key's refusal (unknown key,
    non-admin key, or API keys not allowed there).

    Every Function can read the user's access and ID tokens (__oauth_token__),
    so only the hash-checked repository Functions may exist (ADR-0007 V2): the
    docs Pipe and, with ratings, the rating Action. Another one is refused by
    name-free fixed text: remove it, then re-run."""
    status, document = client.call("GET", LIST_PATH)
    if status in (401, 403):
        raise ProvisionError(f"GET {LIST_PATH} refused the admin API key (HTTP {status})")
    if status != 200 or not isinstance(document, list):
        raise ProvisionError(f"cannot list Functions (HTTP {status})")
    if any(not isinstance(item, dict) or item.get("id") not in KINDS for item in document):
        raise ProvisionError("a Function other than the repository docs Pipe and rating Action is "
                             "installed; only the hash-checked repository Functions may exist "
                             "(Functions can read user tokens): remove it and re-run; nothing "
                             "was installed")
    listed = {str(item["id"]): str(item.get("type") or KINDS[item["id"]]) for item in document}
    for fid, kind in listed.items():
        if kind != KINDS[fid]:
            raise ProvisionError(f"Function {fid} is not a {KINDS[fid]}; remove it and re-run; "
                                 "nothing was installed")
    return listed


def _function(client: Client, fid: str) -> dict:
    """Read a Function the listing showed or the write just stored: any non-200
    here (v0.12.3 answers an absent Function with 401) is a failure."""
    status, document = client.call("GET", f"/api/v1/functions/id/{fid}")
    if status != 200 or not isinstance(document, dict):
        raise ProvisionError(f"cannot read Function {fid} (HTTP {status})")
    return document


def _stored_sha(document: dict) -> str:
    content = document.get("content")
    return sha256_hex(content.encode("utf-8")) if isinstance(content, str) else ""


def _switch_on(client: Client, fid: str, field: str, path: str, what: str) -> dict:
    """Toggle `field` on when it is off; toggles flip, so read the state first."""
    stored = _function(client, fid)
    if stored.get(field) is not True:
        status, _ = client.call("POST", f"/api/v1/functions/id/{fid}/{path}")
        if status != 200:
            raise ProvisionError(f"cannot make Function {fid} {what} (HTTP {status})")
        stored = _function(client, fid)
        if stored.get(field) is not True:
            raise ProvisionError(f"Function {fid} is not {what} after the switch")
    return stored


def _converge(client: Client, fid: str, form: dict, expected: str, listed: dict[str, str]) -> str:
    """Create or update one repository Function to its expected content, read it
    back by hash and kind, and return the action word."""
    current = _function(client, fid) if fid in listed else None
    if current is None:
        status, _ = client.call("POST", "/api/v1/functions/create", form)
        action = "installed"
    elif _stored_sha(current) != expected:
        status, _ = client.call("POST", f"/api/v1/functions/id/{fid}/update", form)
        action = "updated"
    else:
        status, action = 200, "unchanged"
    if status != 200:
        raise ProvisionError(f"cannot write Function {fid} (HTTP {status})")
    stored = _function(client, fid)
    if _stored_sha(stored) != expected:
        raise ProvisionError(f"stored Function {fid} does not match the repository "
                             f"{LABELS[fid]} after the write")
    if stored.get("type", KINDS[fid]) != KINDS[fid]:
        raise ProvisionError(f"Function {fid} is not a {KINDS[fid]} after the write")
    if fid not in _listing(client):
        raise ProvisionError(f"Function {fid} is missing from the listing after the write")
    _switch_on(client, fid, "is_active", "toggle", "active")
    return action


def provision(client: Client, source: str, expected: str, action: str | None = None) -> list[str]:
    """Converge the Functions to the repository set; return fixed status lines.
    `action` is the mounted rating Action, or None when ratings are off."""
    if sha256_hex(source.encode("utf-8")) != expected:
        raise ProvisionError("mounted docs Pipe does not match the repository file "
                             "(sha256 differs); nothing was installed")
    if action is not None and sha256_hex(action.encode("utf-8")) != ACTION_SHA256:
        raise ProvisionError("mounted rating Action does not match the repository file "
                             "(sha256 differs); nothing was installed")
    listed = _listing(client)
    form = {"id": PIPE_ID, "name": PIPE_NAME, "content": source,
            "meta": {"description": PIPE_DESCRIPTION, "manifest": {}}}
    done = _converge(client, PIPE_ID, form, expected, listed)
    lines = [f"docs Pipe {done}: Function {PIPE_ID} active, sha256 {expected}"]
    if action is not None:
        form = {"id": ACTION_ID, "name": ACTION_NAME, "content": action,
                "meta": {"description": ACTION_DESCRIPTION, "manifest": {}}}
        done = _converge(client, ACTION_ID, form, ACTION_SHA256, listed)
        _switch_on(client, ACTION_ID, "is_global", "toggle/global", "global")
        lines.append(f"rating Action {done}: Function {ACTION_ID} active and global, "
                     f"sha256 {ACTION_SHA256}")
    elif ACTION_ID in listed:
        status, _ = client.call("DELETE", f"/api/v1/functions/id/{ACTION_ID}/delete")
        if status != 200 or ACTION_ID in _listing(client):
            raise ProvisionError(f"cannot remove Function {ACTION_ID} (HTTP {status})")
        lines.append(f"rating Action removed: Function {ACTION_ID} (ratings are off)")
    return lines


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 1 or len(argv[0]) != 64 or any(c not in "0123456789abcdef" for c in argv[0]):
            raise ProvisionError("usage: provision_pipe.py EXPECTED_SHA256 (admin API key on stdin)")
        key = sys.stdin.readline().strip()
        if not key:
            raise ProvisionError("no admin API key on stdin")
        try:
            source = (MOUNT / "docs_pipe.py").read_text(encoding="utf-8")
            pinned = leaf_der(CERT.read_text(encoding="ascii"))
            # Mounted only with openwebui.ratings.enabled (#1255).
            action_file = MOUNT / "rating_action.py"
            action = action_file.read_text(encoding="utf-8") if action_file.exists() else None
        except (OSError, UnicodeDecodeError, ValueError):
            raise ProvisionError("cannot read the mounted docs Pipe, rating Action or serving "
                                 "certificate") from None
        for line in provision(Client(key, pinned, HOST, PORT), source, argv[0], action):
            print(line)
    except ProvisionError as exc:
        print(f"FAIL: docs Pipe provisioning refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
