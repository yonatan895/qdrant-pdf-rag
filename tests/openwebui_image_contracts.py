"""Open WebUI contracts that run inside the pinned upstream image (#1074, #1083).

Not a unit test (the file name keeps it out of default collection, like
tests/console_browser_contracts.py): it needs Docker and the Open WebUI image
pinned in images.txt (the ghcr.io/open-webui/open-webui upstream line, by
digest). CI runs it in its own job, which pulls that digest first and refuses
any skip (scripts/check_junit_clean.py). Locally, with the image present, run
it under the heavy lock:

  flock ~/.cache/agent-tmp/mainframe-rag-heavy.lock mfr-pytest <worktree> tests/openwebui_image_contracts.py

Without Docker or the image it skips (never pulled here, about 1 GB).

- The shipped ca-trust init script (charts/mainframe-rag/files/openwebui/
  ca_trust.py) as the pod runs it: a corrupt bundle stops the init step; a good
  one makes httpx and aiohttp TLS to the IdP work.
- The v0.12.3 role mapping (utils/oauth.py get_user_role) that the AD FS
  procedure in docs/deploy.md relies on for revocation.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FILES = REPO / "charts" / "mainframe-rag" / "files" / "openwebui"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"owui_{name}", FILES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ca_trust = _load("ca_trust")


def _cert(tmp_path, name):
    key, crt = tmp_path / f"{name}.key", tmp_path / f"{name}.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", f"/CN={name}", "-keyout", str(key), "-out", str(crt)],
                   check=True, capture_output=True)
    return key, crt


def _drop_line(pem):
    """One full 64-character body line missing: still valid base64 (review #1083 B1)."""
    lines = pem.splitlines(keepends=True)
    body = [i for i, line in enumerate(lines) if len(line.strip()) == 64]
    assert len(body) > 3
    del lines[body[len(body) // 2]]
    return "".join(lines)


def _owui_image():
    lines = (REPO / "images.txt").read_text(encoding="utf-8").splitlines()
    ref, digest = next(line.split()[:2] for line in lines
                       if line.startswith("ghcr.io/open-webui/open-webui:"))
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    image = f"{ref.rsplit(':', 1)[0]}@{digest}"
    if shutil.which("docker") is None:
        pytest.skip("docker is required")
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False)
    if probe.returncode != 0:
        pytest.skip("the pinned Open WebUI image is not present locally")
    return image


def _in_image(image, *args, mounts=(), env=(), timeout=240):
    cmd = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
           "--user", "1000700000:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "-w", "/tmp"]
    for src, dst, mode in mounts:
        cmd += ["-v", f"{src}:{dst}:{mode}"]
    for item in env:
        cmd += ["-e", item]
    return subprocess.run([*cmd, "--entrypoint", "python3", image, *args],
                          capture_output=True, text=True, timeout=timeout, check=False)


TLS_PROBE = """
import asyncio, ssl, threading, http.server
import httpx, aiohttp
srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
srv.RequestHandlerClass.do_GET = lambda h: (h.send_response(200), h.end_headers())
srv.RequestHandlerClass.log_message = lambda *a: None
c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); c.load_cert_chain("/probe/leaf.crt", "/probe/leaf.key")
srv.socket = c.wrap_socket(srv.socket, server_side=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"https://localhost:{srv.server_address[1]}/"
async def main():
    async with httpx.AsyncClient() as h:
        print("httpx", (await h.get(url)).status_code)
    async with aiohttp.ClientSession() as s:
        async with s.get(url, ssl=True) as r:
            print("aiohttp", r.status)
asyncio.run(main())
"""


def test_ca_trust_in_the_pinned_image_refuses_a_corrupt_bundle_and_makes_tls_work(tmp_path):
    """The shipped script, as the init step runs it (read-only root, arbitrary
    UID, no network): a dropped-line bundle exits 1 with the fixed text; a good
    one exits 0, and httpx (sign-in) and aiohttp (refresh) then complete TLS to
    a server whose certificate the IdP CA signed."""
    image = _owui_image()
    ca_dir, trust, probe = tmp_path / "idp-ca", tmp_path / "trust", tmp_path / "probe"
    for d in (ca_dir, trust, probe):
        d.mkdir()
        d.chmod(0o777)
    ca_key, ca_crt = _cert(tmp_path, "idp-root")
    (ca_dir / "ca_trust.py").write_bytes((FILES / "ca_trust.py").read_bytes())
    mounts = [(ca_dir, "/etc/mainframe-rag/idp-ca", "ro"), (trust, "/etc/mainframe-rag/ca-trust", "rw")]
    script = "/etc/mainframe-rag/idp-ca/ca_trust.py"

    (ca_dir / "ca.crt").write_text(_drop_line(ca_crt.read_text()))
    bad = _in_image(image, "-I", script, mounts=mounts)
    assert bad.returncode == 1 and ca_trust.REFUSAL in bad.stderr
    assert not (trust / "ca-bundle.crt").exists()

    (ca_dir / "ca.crt").write_text(ca_crt.read_text())
    good = _in_image(image, "-I", script, mounts=mounts)
    assert good.returncode == 0, good.stderr
    assert good.stdout == "ca-trust: 1 IdP certificate(s) added to the public trust stores\n"

    subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                    "-keyout", str(probe / "leaf.key"), "-out", str(probe / "leaf.csr")],
                   check=True, capture_output=True)
    (probe / "ext.cnf").write_text("subjectAltName=DNS:localhost\n")
    subprocess.run(["openssl", "x509", "-req", "-in", str(probe / "leaf.csr"), "-CA", str(ca_crt),
                    "-CAkey", str(ca_key), "-CAcreateserial", "-days", "1",
                    "-extfile", str(probe / "ext.cnf"), "-out", str(probe / "leaf.crt")],
                   check=True, capture_output=True)
    (probe / "probe.py").write_text(TLS_PROBE)
    for f in probe.iterdir():
        f.chmod(0o644)
    trusted = _in_image(image, "-I", "/probe/probe.py",
                        mounts=[(probe, "/probe", "ro"), (trust, "/etc/mainframe-rag/ca-trust", "ro")],
                        env=["SSL_CERT_FILE=/etc/mainframe-rag/ca-trust/ca-bundle.crt"])
    assert trusted.stdout.split() == ["httpx", "200", "aiohttp", "200"], trusted.stderr[-400:]
    untrusted = _in_image(image, "-I", "/probe/probe.py", mounts=[(probe, "/probe", "ro")])
    assert untrusted.returncode != 0 and "httpx 200" not in untrusted.stdout


ROLE_PROBE = """
import asyncio, json, sys, types
from fastapi import HTTPException
import open_webui.utils.oauth as o
cfg = types.SimpleNamespace(ENABLE_OAUTH_ROLE_MANAGEMENT=True, OAUTH_ROLES_CLAIM="roles",
                            OAUTH_ALLOWED_ROLES=["docs-user"], OAUTH_ADMIN_ROLES=["docs-admin"],
                            DEFAULT_USER_ROLE="pending")
async def runtime_config():
    return cfg
async def user_count(*a, **k):
    return 5
o.get_oauth_runtime_config = runtime_config
o.Users.get_num_users = user_count
manager = o.OAuthManager.__new__(o.OAuthManager)
async def role(stored, claims):
    user = types.SimpleNamespace(role=stored) if stored else None
    try:
        return await manager.get_user_role(user, claims)
    except HTTPException as e:
        return f"HTTP {e.status_code}"
async def main():
    cases = json.loads(sys.argv[1])
    print(json.dumps([await role(stored, claims) for stored, claims in cases]))
asyncio.run(main())
"""


def test_pinned_role_mapping_needs_a_roles_claim_on_every_sign_in(tmp_path):
    """docs/deploy.md (AD FS) relies on this v0.12.3 behaviour of
    utils/oauth.py get_user_role, which the sign-in callback runs for an
    existing user on every sign-in (review #1083 B2):
    - no roles claim: an existing user KEEPS the stored role (an admin stays
      admin); a new user is pending;
    - a roles claim with no allowed or admin value (the always-issued baseline
      `signed-in` only): HTTP 403, so the sign-in is refused;
    - the claim is recomputed: an admin left with only docs-user becomes user."""
    image = _owui_image()
    probe = tmp_path / "probe"
    probe.mkdir()
    probe.chmod(0o755)
    (probe / "role_probe.py").write_text(ROLE_PROBE)
    (probe / "role_probe.py").chmod(0o644)
    cases = [["admin", {}], [None, {}], ["admin", {"roles": ["signed-in"]}],
             ["user", {"roles": "signed-in"}], ["admin", {"roles": ["signed-in", "docs-user"]}],
             [None, {"roles": ["signed-in", "docs-admin"]}], ["user", {"roles": ["docs-admin"]}]]
    r = _in_image(image, "/probe/role_probe.py", json.dumps(cases),
                  mounts=[(probe, "/probe", "ro")],
                  env=["PYTHONPATH=/app/backend", "DATA_DIR=/tmp", "HOME=/tmp", "WEBUI_SECRET_KEY=x",
                       "OFFLINE_MODE=true", "HF_HUB_OFFLINE=1", "VECTOR_DB=pgvector", "ENABLE_OTEL=false"])
    assert r.returncode == 0, r.stderr[-600:]
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [
        "admin", "pending", "HTTP 403", "HTTP 403", "user", "admin", "admin"]
