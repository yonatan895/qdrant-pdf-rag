"""Install the repository docs Pipe into Open WebUI, hash-checked (ADR-0007 V2, #1074).

Runs inside the Open WebUI pod (deploy.sh: kubectl exec -i ... python3
/opt/mainframe-rag/openwebui/provision_pipe.py EXPECTED_SHA256). The admin API
key arrives on stdin, never in argv, the environment or the pod spec.

  1. The mounted Pipe (the chart ConfigMap) must hash to EXPECTED_SHA256, the
     sha256 deploy.sh computed from the repository file.
  2. The server certificate on 127.0.0.1:8443 must be this pod's own serving
     certificate (pinned by DER bytes) before the key is sent.
  3. The Function is created, or updated when its stored content differs, and
     then read back: its stored content must hash to EXPECTED_SHA256.
  4. The Function is switched active when it is not (a toggle is applied only
     after reading the current state, so a repeat run converges).

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
PIPE_NAME = "Docs"
PIPE_DESCRIPTION = "Documentation corpus through the mainframe-rag agent"
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

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
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
        if status in (401, 403):
            raise ProvisionError(f"{method} {path} refused the admin API key (HTTP {status})")
        if status != 200:
            return status, None
        try:
            document = json.loads(raw)
        except ValueError:
            raise ProvisionError(f"{method} {path} returned a non-JSON body") from None
        return status, document if isinstance(document, dict) else None


def _function(client: Client) -> dict | None:
    status, document = client.call("GET", f"/api/v1/functions/id/{PIPE_ID}")
    if status == 404:
        return None
    if status != 200 or document is None:
        raise ProvisionError(f"cannot read Function {PIPE_ID} (HTTP {status})")
    return document


def _stored_sha(document: dict) -> str:
    content = document.get("content")
    return sha256_hex(content.encode("utf-8")) if isinstance(content, str) else ""


def provision(client: Client, source: str, expected: str) -> list[str]:
    """Converge the Function to the repository Pipe; return fixed status lines."""
    if sha256_hex(source.encode("utf-8")) != expected:
        raise ProvisionError("mounted docs Pipe does not match the repository file "
                             "(sha256 differs); nothing was installed")
    form = {"id": PIPE_ID, "name": PIPE_NAME, "content": source,
            "meta": {"description": PIPE_DESCRIPTION, "manifest": {}}}
    current = _function(client)
    if current is None:
        status, _ = client.call("POST", "/api/v1/functions/create", form)
        action = "installed"
    elif _stored_sha(current) != expected:
        status, _ = client.call("POST", f"/api/v1/functions/id/{PIPE_ID}/update", form)
        action = "updated"
    else:
        status, action = 200, "unchanged"
    if status != 200:
        raise ProvisionError(f"cannot write Function {PIPE_ID} (HTTP {status})")
    stored = _function(client)
    if stored is None or _stored_sha(stored) != expected:
        raise ProvisionError(f"stored Function {PIPE_ID} does not match the repository "
                             "Pipe after the write")
    if stored.get("is_active") is not True:
        status, _ = client.call("POST", f"/api/v1/functions/id/{PIPE_ID}/toggle")
        if status != 200:
            raise ProvisionError(f"cannot activate Function {PIPE_ID} (HTTP {status})")
        stored = _function(client)
        if stored is None or stored.get("is_active") is not True:
            raise ProvisionError(f"Function {PIPE_ID} is not active after activation")
    return [f"docs Pipe {action}: Function {PIPE_ID} active, sha256 {expected}"]


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
        except (OSError, UnicodeDecodeError, ValueError):
            raise ProvisionError("cannot read the mounted docs Pipe or serving certificate") from None
        for line in provision(Client(key, pinned, HOST, PORT), source, argv[0]):
            print(line)
    except ProvisionError as exc:
        print(f"FAIL: docs Pipe provisioning refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
