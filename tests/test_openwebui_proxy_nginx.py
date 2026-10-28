"""The Open WebUI allow-list proxy against a real, pinned nginx (ADR-0007 E1, #1074).

Runs charts/mainframe-rag/files/openwebui/proxy.nginx.conf, unchanged, in the
image pinned in images.txt (digest, no tag), in front of a stub upstream that
shares the container network namespace on 127.0.0.1:8080, exactly as the sidecar
shares the pod's.

Marker ``integration`` (#1118 A5): the unit tier never pulls an image, so this
file runs in the GitHub ``sim`` lane, which prepares the pinned digest explicitly
before collection. The fixture never pulls: it requires the prepared image by
digest, starts it by immutable ID with ``--pull=never``, and fails (never skips)
when Docker, openssl or the prepared image is missing. Offline GitLab has no
local Docker daemon, so its sim job omits this file (as it omits
tests/test_qdrant_auth.py). The unit tier keeps the hermetic config contracts in
tests/test_helm_chart_contracts.py. Locally, prepare once and run it under the
heavy lock:

  docker pull docker.io/nginxinc/nginx-unprivileged@<images.txt digest>
  flock ~/.cache/agent-tmp/mainframe-rag-heavy.lock mfr-pytest <worktree> -m integration tests/test_openwebui_proxy_nginx.py

It proves the decision is made on nginx's decoded, normalised $uri for every
spelling that uvicorn would route to a blocked handler, and that the request
line is forwarded to the upstream byte for byte.
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import ssl
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]
CONF = REPO / "charts" / "mainframe-rag" / "files" / "openwebui" / "proxy.nginx.conf"

STUB_CONF = """\
pid /tmp/stub.pid;
error_log /dev/stderr warn;
events {}
http {
    access_log off;
    client_body_temp_path /tmp/c; proxy_temp_path /tmp/p; fastcgi_temp_path /tmp/f;
    uwsgi_temp_path /tmp/u; scgi_temp_path /tmp/s;
    server {
        listen 127.0.0.1:8080;
        location / {
            return 200 "stub:$request_uri|$uri|$http_upgrade|$http_connection|$http_x_forwarded_proto|$request_method\\n";
        }
    }
}
"""


def _pinned_image() -> str:
    for line in (REPO / "images.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "nginx-unprivileged" not in line:
            continue
        ref, digest = line.split()[:2]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "the nginx pin must be a real digest"
        return f"{ref.rsplit(':', 1)[0]}@{digest}"
    raise AssertionError("no nginx-unprivileged line in images.txt")


def _docker(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def _prepared_image(reference: str) -> str:
    """The immutable ID of the explicitly prepared pinned image; never pulls.

    Presence of a tag is not identity: inspect by the approved repository digest
    and require that digest in the daemon record, then launch the ID itself.
    """
    probe = _docker("image", "inspect", reference, check=False, timeout=30)
    if probe.returncode != 0:
        pytest.fail(f"the pinned nginx image is not prepared; prepare it explicitly: docker pull {reference}")
    records = json.loads(probe.stdout)
    assert isinstance(records, list) and len(records) == 1, "invalid image inspection"
    image_id = records[0].get("Id", "")
    digests = [d.removeprefix("docker.io/") for d in records[0].get("RepoDigests") or [] if isinstance(d, str)]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image_id), "invalid image ID"
    assert reference.removeprefix("docker.io/") in digests, "the local nginx image is not the pinned digest"
    return image_id


@pytest.fixture(scope="module")
def proxy(tmp_path_factory):
    for tool in ("docker", "openssl"):
        if shutil.which(tool) is None:
            pytest.fail(f"{tool} is required by the real-nginx proxy tier")
    try:
        _docker("info", timeout=20)
    except (subprocess.SubprocessError, OSError):
        pytest.fail("the Docker daemon is not reachable")
    image = _prepared_image(_pinned_image())
    work = tmp_path_factory.mktemp("nginx")
    tls = work / "tls"
    tls.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
                    "-subj", "/CN=localhost", "-keyout", str(tls / "tls.key"), "-out", str(tls / "tls.crt")],
                   check=True, capture_output=True)
    for path in (tls, tls / "tls.key", tls / "tls.crt"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    stub = work / "stub.conf"
    stub.write_text(STUB_CONF, encoding="utf-8")
    conf = work / "nginx.conf"
    conf.write_bytes(CONF.read_bytes())  # unchanged: no port or address substitution
    conf.chmod(0o644)
    stub.chmod(0o644)
    tag = uuid.uuid4().hex[:8]
    upstream, front = f"owui-stub-{tag}", f"owui-proxy-{tag}"
    try:
        _docker("run", "--pull=never", "-d", "--name", upstream, "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                "-p", "127.0.0.1::8443", "-v", f"{stub}:/etc/nginx/stub.conf:ro", image,
                "nginx", "-c", "/etc/nginx/stub.conf", "-g", "daemon off;")
        _docker("run", "--pull=never", "-d", "--name", front, "--network", f"container:{upstream}", "--read-only",
                "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-v", f"{conf}:/etc/nginx/proxy/nginx.conf:ro", "-v", f"{tls}:/etc/tls/private:ro", image,
                "nginx", "-c", "/etc/nginx/proxy/nginx.conf", "-g", "daemon off;")
        port = int(_docker("port", upstream, "8443/tcp").stdout.split(":")[-1])
        # A bare TCP connect is not readiness: Docker's port proxy accepts on the host
        # port before nginx listens in the container, so the first real request then
        # sees an EOF. Wait for a full TLS round trip through the proxy to the stub.
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                status, body = _request(port, "GET", ALLOWED[0])
            except (OSError, ssl.SSLError, IndexError, ValueError):  # refused, EOF, empty reply
                status, body = 0, ""
            if status == 200 and body.startswith("stub:"):
                break
            time.sleep(0.3)
        else:
            pytest.fail("the proxy container never listened: " + _docker("logs", front, check=False).stderr)
        yield port
    finally:
        _docker("rm", "-f", front, upstream, check=False)


def _request(port: int, method: str, target: str, headers: dict | None = None) -> tuple[int, str]:
    """One raw request: the target is sent exactly as given (no client-side normalisation)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    raw = (f"{method} {target} HTTP/1.1\r\nHost: docs.example\r\n{extra}Content-Length: 0\r\n"
           "Connection: close\r\n\r\n").encode()
    with (socket.create_connection(("127.0.0.1", port), timeout=10) as sock,
          context.wrap_socket(sock, server_hostname="localhost") as tls):
        tls.sendall(raw)
        data = b""
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, body.decode("latin-1")


ALLOWED = [
    "/", "/c/3f2a", "/auth", "/admin/users", "/_app/immutable/a.js", "/static/loader.js", "/favicon.png",
    "/health", "/health/db", "/oauth/oidc/login", "/oauth/oidc/callback?code=x&state=y", "/ws/socket.io/?EIO=4",
    "/api/config", "/api/version", "/api/models", "/api/chat/completions", "/api/chat/completed",
    "/api/tasks/stop/x", "/api/v1/auths/", "/api/v1/auths/api_key", "/api/v1/chats/list?page=1",
    "/api/v1/users/user/settings", "/api/v1/models/list", "/api/v1/groups/",
    "/api/v1/functions/id/mainframe_rag_docs", "/api/v1/functions/create", "/api/v1/configs/banners",
    # Encoded spellings of ALLOWED paths still work, and are forwarded unchanged.
    "/api/v1/ch%61ts/list", "/api/v1%2Fchats/list", "/api/v1/chats/a%20b?x=1%2F2",
    # Merged slashes: nginx decides on the merged path; the raw line is forwarded.
    "/api//v1/chats/",
]

BLOCKED_FILES = [
    "/api/v1/files/", "/api/v1/files", "/api/v1/files/abc/content", "/api/v1/files/?x=1",
    "/API/v1/files/", "//api/v1/files/", "/api//v1//files/", "/api/v1/./files/", "/api/v1/%66iles/",
    "/api/v1/%46iles/", "/api/v1%2Ffiles/", "/api/v1%2ffiles/", "/api/v1/files%2F", "/%61pi/v1/files/",
    "/api/v1/chats/../files/", "/api/v1/chats/%2e%2e/files/", "/api/v1/chats/..%2ffiles/",
    "/api/v1/chats%2F..%2Ffiles/", "/api/v1/%2566iles/", "/api/v1/files;x/", "/api/v1/files%2e",
    "/api/v1/chats/./../files/", "/api/v1/chats/%2e/%2e%2e/files",
]
BLOCKED_OTHER = [
    "/api/v1/retrieval/process/text", "/api/v1/%72etrieval/process/text", "/api/v1/knowledge/",
    "/api/v1/knowledg%65/create", "/api/v1/audio/transcriptions", "/api/v1/audi%6f/speech",
    "/api/v1/images/generations", "/api/v1/memories/add", "/api/v1/notes/", "/api/v1/channels/",
    "/api/v1/utils/code/execute", "/api/v1/utils/db/download", "/openai/chat/completions",
    "/ollama/api/chat", "/docs", "/openapi.json", "/api/version/updates", "/api/v1/pipelines/",
    "/api/v1/scim/v2/Users", "/api/v1/chatsX", "/oauthx", "/health/x", "/api", "/api/", "/api/v1/",
]


@pytest.mark.parametrize("target", ALLOWED)
def test_allowed_paths_reach_the_upstream_with_the_original_request_line(proxy, target):
    status, body = _request(proxy, "GET", target)
    assert status == 200, (target, status, body)
    forwarded = body.removeprefix("stub:").split("|")[0]
    assert forwarded == target, "the upstream must see the request URI exactly as the client sent it"


@pytest.mark.parametrize("target", BLOCKED_FILES + BLOCKED_OTHER)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_blocked_paths_and_every_encoded_variant_get_a_fixed_403_before_the_upstream(proxy, method, target):
    status, body = _request(proxy, method, target)
    assert status == 403 and body == "denied\n", (method, target, status, body)


def test_a_null_byte_never_reaches_the_upstream(proxy):
    for target in ("/api/v1/files%00", "/api/v1/chats/%00", "/api/v1/files/%00x"):
        status, body = _request(proxy, "POST", target)
        assert status in (400, 403) and "stub:" not in body, (target, status, body)


def test_websocket_upgrade_headers_and_forwarded_proto_reach_the_upstream(proxy):
    status, body = _request(proxy, "GET", "/ws/socket.io/?EIO=4&transport=websocket",
                            {"Upgrade": "websocket", "Connection": "Upgrade"})
    assert status == 200
    fields = body.removeprefix("stub:").rstrip("\n").split("|")
    assert fields[2] == "websocket" and fields[3].lower() == "upgrade" and fields[4] == "https"


def test_post_to_an_allowed_path_is_proxied_and_the_denial_text_is_fixed(proxy):
    status, body = _request(proxy, "POST", "/api/v1/functions/create")
    assert status == 200 and body.endswith("|POST\n")
    _status, denied = _request(proxy, "GET", "/api/v1/files/secret-name-123")
    assert denied == "denied\n"  # no path, no upstream text in the refusal
