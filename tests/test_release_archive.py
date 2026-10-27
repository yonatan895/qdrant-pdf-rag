"""Private release archive tool (#1118, decision D5; docs/deploy.md#release-archive).

scripts/release_archive.py runs as a real subprocess against stub `oras` and
`gh` executables that share one fake registry/package state file. The stub
registry computes real manifest and blob digests, so digest checks and
signature checks exercise the tool's actual verification, not a mock answer.
Blob downloads go over real HTTP to a loopback registry (bearer challenge,
token endpoint, Range) that redirects to a second origin standing in for blob
storage, with injectable drops, Range-ignoring and corruption faults.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.helpers_airgap import gen_other_pub, gen_sign_keypair, sign_sums

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release_archive.py"
REPOSITORY = "ghcr.io/yonatan895/qdrant-pdf-rag-release-archive"
PACKAGE = "qdrant-pdf-rag-release-archive"

FAKE_ORAS = r'''
import hashlib, json, os, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
argv = sys.argv[1:]
if "--registry-config" in argv:
    i = argv.index("--registry-config"); config = argv[i + 1]; del argv[i:i + 2]
argv = [a for a in argv if a != "--no-tty"]
state["calls"].append(argv[0])

def save():
    state_path.write_text(json.dumps(state))

def put_blob(data):
    hexd = hashlib.sha256(data).hexdigest()
    state["blobs"][hexd] = data.hex()
    return hexd

cmd = argv[0]
if cmd == "version":
    print("Version:        1.3.4"); save(); sys.exit(0)
if cmd == "login":
    if not sys.stdin.read().strip():
        sys.exit(1)
    Path(config).write_text("{\"auths\": {}}"); save(); sys.exit(0)
if cmd == "push":
    ref = argv[1]; rest = argv[2:]; annotations = {}; files = []; export = None; atype = None
    while rest:
        a = rest.pop(0)
        if a == "--annotation":
            k, v = rest.pop(0).split("=", 1); annotations[k] = v
        elif a == "--artifact-type":
            atype = rest.pop(0)
        elif a == "--export-manifest":
            export = rest.pop(0)
        else:
            files.append(a)
    layers = []
    for name in files:
        if os.path.isabs(name) or "/" in name:
            print("path validation failed", file=sys.stderr); sys.exit(1)
        data = Path(name).read_bytes()
        layers.append({"mediaType": "application/vnd.oci.image.layer.v1.tar",
                       "digest": "sha256:" + put_blob(data), "size": len(data),
                       "annotations": {"org.opencontainers.image.title": name}})
    manifest = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                           "artifactType": atype, "layers": layers,
                           "annotations": annotations}, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    state["manifests"][digest] = manifest.hex()
    if digest not in state["ids"]:
        state["ids"][digest] = len(state["ids"]) + 100
    state["tags"][ref.rsplit(":", 1)[1]] = digest
    if export:
        Path(export).write_bytes(manifest)
    if os.environ.get("FAKE_VISIBILITY_AFTER_PUSH"):
        state["visibility"] = os.environ["FAKE_VISIBILITY_AFTER_PUSH"]
    save()
    if os.environ.get("FAKE_BLOCK_PUSH"):
        import time
        Path(os.environ["FAKE_BLOCK_PUSH"]).write_text("pushing")
        time.sleep(60)
    sys.exit(0)
if cmd == "resolve":
    digest = state["tags"].get(argv[1].rsplit(":", 1)[1])
    save()
    if digest is None:
        sys.exit(1)
    print(digest); sys.exit(0)
if cmd == "manifest" and argv[1] == "fetch":
    digest = argv[2].split("@", 1)[1]
    raw = bytes.fromhex(state["manifests"][digest]) if digest in state["manifests"] else None
    save()
    if raw is None:
        sys.exit(1)
    if os.environ.get("FAKE_SERVE_OTHER_MANIFEST"):
        raw = raw.replace(b'"schemaVersion":2', b'"schemaVersion": 2')
    sys.stdout.buffer.write(raw); sys.exit(0)
print("unsupported", argv, file=sys.stderr); sys.exit(3)
'''

FAKE_GH = r'''
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
argv = sys.argv[1:]
state["gh"].append(" ".join(argv))
def done(code, body=None):
    state_path.write_text(json.dumps(state))
    if body is not None:
        print(json.dumps(body))
    sys.exit(code)
if argv[:2] == ["auth", "token"]:
    print("gho_fake")
    done(0)
pkg = "/user/packages/container/" + os.environ["FAKE_PACKAGE"]
if argv == ["api", pkg]:
    if not state["manifests"]:
        done(1, {"message": "Package not found.", "status": "404"})
    done(0, {"name": os.environ["FAKE_PACKAGE"], "visibility": state["visibility"]})
if argv == ["api", "--paginate", "--slurp", pkg + "/versions"]:
    versions = [{"id": state["ids"][d], "name": d,
                 "metadata": {"container": {"tags": [t for t, v in state["tags"].items() if v == d]}}}
                for d in state["manifests"]]
    done(0, [versions])
if argv[:3] == ["api", "--method", "DELETE"] and argv[3].startswith(pkg + "/versions/"):
    vid = int(argv[3].rsplit("/", 1)[1])
    digest = next((d for d, i in state["ids"].items() if i == vid and d in state["manifests"]), None)
    if digest is None:
        done(1, {"status": "404"})
    del state["manifests"][digest]
    state["tags"] = {t: d for t, d in state["tags"].items() if d != digest}
    state["deleted"].append(digest)
    done(0)
done(4, {"message": "unexpected gh call"})
'''


OWNER = "yonatan895"
TOKEN = "tok-registry-1"


class FakeRegistry:
    """Loopback OCI blob API: bearer challenge, token endpoint, then a 307 to a
    second origin (blob storage) that honours Range unless a fault says otherwise."""

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.faults: dict = {}
        self.requests: list[dict] = []
        self.served: dict[str, int] = {}
        registry, storage = self._handler("registry"), self._handler("storage")
        self.registry = ThreadingHTTPServer(("127.0.0.1", 0), registry)
        self.storage = ThreadingHTTPServer(("127.0.0.1", 0), storage)
        for server in (self.registry, self.storage):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.registry.server_port}"
        self.storage_url = f"http://127.0.0.1:{self.storage.server_port}"

    def close(self) -> None:
        for server in (self.registry, self.storage):
            server.shutdown()
            server.server_close()

    def blob(self, hexd: str) -> bytes:
        return bytes.fromhex(json.loads(self.state_path.read_text())["blobs"][hexd])

    def storage_ranges(self, hexd: str) -> list:
        return [r["range"] for r in self.requests if r["server"] == "storage" and hexd in r["path"]]

    def _handler(self, role: str):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, code: int, body: bytes = b"", headers: dict | None = None):
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                auth = self.headers.get("Authorization")
                fake.requests.append({"server": role, "path": self.path, "auth": auth,
                                      "range": self.headers.get("Range")})
                if role == "registry":
                    return self._registry(auth)
                return self._storage()

            def _registry(self, auth):
                if self.path.startswith("/token?"):
                    want = "Basic " + base64.b64encode(f"{OWNER}:gho_fake".encode()).decode()
                    if auth != want:
                        return self._send(401)
                    return self._send(200, json.dumps({"token": TOKEN}).encode())
                match = re.fullmatch(rf"/v2/{OWNER}/{PACKAGE}/blobs/sha256:([0-9a-f]{{64}})", self.path)
                if not match:
                    return self._send(404)
                if auth != f"Bearer {TOKEN}":
                    challenge = (f'Bearer realm="{fake.url}/token",service="fake-registry",'
                                 f'scope="repository:{OWNER}/{PACKAGE}:pull"')
                    return self._send(401, headers={"WWW-Authenticate": challenge})
                status = fake.faults.get("status")
                if status:
                    return self._send(status)
                location = f"{fake.storage_url}/blobs/{match[1]}?sig=signed-query"
                return self._send(307, headers={"Location": location})

            def _storage(self):
                hexd = self.path.split("/blobs/", 1)[1].split("?", 1)[0]
                data = fake.blob(hexd)
                if hexd == fake.faults.get("tamper"):
                    data += b"tampered"
                count = fake.served[hexd] = fake.served.get(hexd, 0) + 1
                requested = self.headers.get("Range")
                start = int(re.fullmatch(r"bytes=(\d+)-", requested)[1]) if requested else 0
                ignore = fake.faults.get("ignore_range") == hexd
                if start and not ignore:
                    body = data[start:]
                    if fake.faults.get("corrupt_resumed") == hexd:
                        body = bytes([body[0] ^ 0xFF]) + body[1:]
                    code = 206
                    headers = {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"}
                else:
                    body, code, headers = data, 200, {}
                drop = fake.faults.get("drop", {}).get(hexd)
                if drop and (drop["times"] is None or count <= drop["times"]):
                    # Promise the whole body, send part of it, then cut the connection.
                    self.send_response(code)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body[:drop["after"]])
                    self.wfile.flush()
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return None
                return self._send(code, body, headers)

        return Handler


@pytest.fixture(scope="module")
def signing(tmp_path_factory):
    """One throwaway signing key per module plus its public half (the trusted key)."""
    keys = tmp_path_factory.mktemp("release-archive-keys")
    key = gen_sign_keypair(keys)
    pub = keys / "trusted.pub"
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    return key, pub


@pytest.fixture
def env(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("oras", FAKE_ORAS), ("gh", FAKE_GH)):
        path = stubs / name
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(0o755)
    pin = tmp_path / "oras-pin.txt"
    pin.write_text("version: v1.3.4\nbinary-sha256: "
                   + hashlib.sha256((stubs / "oras").read_bytes()).hexdigest() + "\n")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"blobs": {}, "manifests": {}, "tags": {}, "ids": {},
                                 "visibility": "private", "calls": [], "gh": [], "deleted": []}))
    run_env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path), "FAKE_STATE": str(state),
               "FAKE_PACKAGE": PACKAGE, "TMPDIR": str(tmp_path), "NO_PROXY": "*"}
    registry = FakeRegistry(state)
    yield {"stubs": stubs, "pin": pin, "state": state, "env": run_env, "registry": registry,
           "record": tmp_path / "release-archive.txt", "tmp": tmp_path}
    registry.close()


def command(env, *args):
    return [sys.executable, str(SCRIPT), "--record", str(env["record"]),
            "--oras", str(env["stubs"] / "oras"), "--oras-pin", str(env["pin"]),
            "--registry-url", env["registry"].url, *args]


def run(env, *args, extra=None):
    return subprocess.run(command(env, *args), capture_output=True, text=True, check=False,
                          env={**env["env"], **(extra or {})}, stdin=subprocess.DEVNULL)


def pull(env, signing, digest, out, *extra_args):
    return run(env, "pull", "--digest", digest, "--out", str(out), "--trusted-pub", str(signing[1]),
               "--backoff", "0", *extra_args)


def layer_hex(env, title):
    data = state(env)
    for raw in data["manifests"].values():
        for layer in json.loads(bytes.fromhex(raw))["layers"]:
            if layer["annotations"]["org.opencontainers.image.title"] == title:
                return layer["digest"].split(":", 1)[1]
    raise AssertionError(title)


def state(env):
    return json.loads(env["state"].read_text())


def sha_of(n: int) -> str:
    return hashlib.sha1(f"release-{n}".encode()).hexdigest()


def make_bundle(root: Path, signing, sha: str, date: str = "2026-10-06T20:47:05Z") -> Path:
    """Loose signed members as the site receives them (original synthetic content)."""
    key, pub = signing
    bundle = root / f"bundle-{sha[:7]}"
    bundle.mkdir()
    shutil.copyfile(pub, bundle / "sneakernet-signing.pub")
    (bundle / "MANIFEST.txt").write_text(f"sha: {sha}\ndate: {date}\nimage_format: oci-archive\n")
    (bundle / "PACKING_RECORD.txt").write_text(f"Commit SHA: {sha}\n")
    (bundle / f"app-agent-{sha}.tar").write_bytes(os.urandom(40_000))
    write_sums(bundle)
    sign_sums(bundle, key)
    return bundle


def write_sums(bundle: Path) -> None:
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
             for p in sorted(bundle.iterdir()) if p.name not in ("SHA256SUMS", "SHA256SUMS.sig")]
    (bundle / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def push(env, signing, bundle, sha, extra=None):
    return run(env, "push", "--bundle", str(bundle), "--release-sha", sha,
               "--trusted-pub", str(signing[1]), extra=extra)


def record_rows(env):
    return [line.split() for line in env["record"].read_text().splitlines()
            if line and not line.startswith("#")]


# ------------------------------------------------------------ push refusals


def test_push_refuses_unsigned_bundle_before_any_registry_call(env, signing):
    sha = sha_of(1)
    bundle = make_bundle(env["tmp"], signing, sha)
    (bundle / "SHA256SUMS.sig").unlink()
    result = push(env, signing, bundle, sha)
    assert result.returncode == 2
    assert "unsigned or incomplete bundle: SHA256SUMS.sig is missing" in result.stderr
    assert state(env)["calls"] == [] and state(env)["gh"] == []
    assert not env["record"].exists()


@pytest.mark.parametrize("tamper, message", [
    ("member", "member checksum mismatch"),
    ("sums", "SHA256SUMS signature verification failed"),
    ("extra", "SHA256SUMS must list exactly the bundle members"),
    ("manifest_sha", "MANIFEST sha does not match the release SHA"),
])
def test_push_refuses_tampered_bundle(env, signing, tamper, message):
    sha = sha_of(2)
    bundle = make_bundle(env["tmp"], signing, sha)
    if tamper == "member":
        (bundle / f"app-agent-{sha}.tar").write_bytes(b"substituted image")
    elif tamper == "sums":
        write_sums(bundle)
        with (bundle / "SHA256SUMS").open("a") as sums:
            sums.write(f"{'0' * 64}  extra.bin\n")
    elif tamper == "extra":
        (bundle / "unsigned-extra.tar").write_bytes(b"rides along")
    else:
        (bundle / "MANIFEST.txt").write_text(f"sha: {sha_of(99)}\ndate: 2026-10-06T20:47:05Z\n")
        write_sums(bundle)
        sign_sums(bundle, signing[0])
    result = push(env, signing, bundle, sha)
    assert result.returncode == 2
    assert message in result.stderr
    assert state(env)["calls"] == []
    assert not env["record"].exists()


def test_push_refuses_bundle_signed_by_another_key(env, signing, tmp_path):
    sha = sha_of(3)
    bundle = make_bundle(env["tmp"], signing, sha)
    other = gen_other_pub(tmp_path)
    result = run(env, "push", "--bundle", str(bundle), "--release-sha", sha, "--trusted-pub", str(other))
    assert result.returncode == 2
    assert "bundle public key does not match the trusted key" in result.stderr
    assert state(env)["calls"] == []


def test_push_refuses_unpinned_oras(env, signing):
    sha = sha_of(4)
    bundle = make_bundle(env["tmp"], signing, sha)
    env["pin"].write_text("version: v1.3.4\nbinary-sha256: " + "a" * 64 + "\n")
    result = push(env, signing, bundle, sha)
    assert result.returncode == 2
    assert "oras binary missing or not the pinned build" in result.stderr
    assert state(env)["calls"] == []


def test_push_refuses_public_package_before_pushing(env, signing):
    first, second = sha_of(5), sha_of(6)
    assert push(env, signing, make_bundle(env["tmp"], signing, first), first).returncode == 0
    data = state(env)
    data["visibility"] = "public"
    env["state"].write_text(json.dumps(data))
    result = push(env, signing, make_bundle(env["tmp"], signing, second, "2026-10-08T00:00:00Z"), second)
    assert result.returncode == 2
    assert "is public before push, not private" in result.stderr
    assert "push" not in state(env)["calls"][len(data["calls"]):]
    assert [row[0] for row in record_rows(env)] == [first]


def test_push_stops_and_does_not_record_when_package_turns_public(env, signing):
    sha = sha_of(7)
    result = push(env, signing, make_bundle(env["tmp"], signing, sha), sha,
                  extra={"FAKE_VISIBILITY_AFTER_PUSH": "public"})
    assert result.returncode == 2
    assert "package visibility before push: absent" in result.stdout
    assert "is public after push, not private" in result.stderr
    assert not env["record"].exists()


# ------------------------------------------------------------ push and pull


def test_push_records_digest_then_pull_by_digest_reverifies(env, signing):
    sha = sha_of(8)
    bundle = make_bundle(env["tmp"], signing, sha, "2026-10-06T20:47:05Z")
    result = push(env, signing, bundle, sha)
    assert result.returncode == 0, result.stderr
    assert "package visibility before push: absent" in result.stdout
    assert "package visibility after push: private" in result.stdout
    data = state(env)
    digest = data["tags"][sha]
    manifest = bytes.fromhex(data["manifests"][digest])
    assert digest == "sha256:" + hashlib.sha256(manifest).hexdigest()
    titles = sorted(layer["annotations"]["org.opencontainers.image.title"]
                    for layer in json.loads(manifest)["layers"])
    assert titles == sorted(p.name for p in bundle.iterdir())
    # Privacy boundary: a source annotation would link the package to the public repo.
    assert set(json.loads(manifest)["annotations"]) == {
        "org.opencontainers.image.created", "org.opencontainers.image.revision",
        "io.github.yonatan895.signing-key-sha256"}
    key_id = hashlib.sha256(signing[1].read_bytes()).hexdigest()
    assert record_rows(env) == [[sha, digest, key_id, "2026-10-06T20:47:05Z"]]

    out = env["tmp"] / "pulled"
    pulled = pull(env, signing, digest, out)
    assert pulled.returncode == 0, pulled.stderr
    assert "re-verified its signature" in pulled.stdout
    for member in bundle.iterdir():
        assert (out / member.name).read_bytes() == member.read_bytes()
    assert not (env["tmp"] / ".pulled.partial").exists()
    assert "blob" not in state(env)["calls"]  # blobs never go through oras


def test_push_rerun_adopts_identical_unrecorded_tag_without_pushing(env, signing):
    sha = sha_of(9)
    bundle = make_bundle(env["tmp"], signing, sha)
    assert push(env, signing, bundle, sha).returncode == 0
    digest = record_rows(env)[0][1]
    env["record"].unlink()  # a crash between the push and the record write
    before = state(env)["calls"].count("push")
    result = push(env, signing, bundle, sha)
    assert result.returncode == 0, result.stderr
    assert state(env)["calls"].count("push") == before
    assert record_rows(env)[0][:2] == [sha, digest]


def test_push_refuses_to_overwrite_tag_with_different_content(env, signing, tmp_path):
    sha = sha_of(10)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    digest = state(env)["tags"][sha]
    env["record"].unlink()
    again = tmp_path / "again"
    again.mkdir()
    rebuilt = make_bundle(again, signing, sha)  # same release SHA, different bytes
    result = push(env, signing, rebuilt, sha)
    assert result.returncode == 2
    assert "archive tag already exists with different content" in result.stderr
    assert state(env)["tags"][sha] == digest


def test_pull_refuses_digest_mismatch(env, signing):
    sha = sha_of(11)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    digest = record_rows(env)[0][1]
    out = env["tmp"] / "pulled"
    result = run(env, "pull", "--digest", digest, "--out", str(out), "--trusted-pub", str(signing[1]),
                 extra={"FAKE_SERVE_OTHER_MANIFEST": "1"})
    assert result.returncode == 2
    assert "digest mismatch" in result.stderr
    assert env["registry"].requests == []
    assert not out.exists() or not any(out.iterdir())


def test_pull_refuses_tampered_layer_content(env, signing):
    sha = sha_of(12)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    digest = record_rows(env)[0][1]
    env["registry"].faults["tamper"] = layer_hex(env, f"app-agent-{sha}.tar")
    result = pull(env, signing, digest, env["tmp"] / "pulled")
    assert result.returncode == 2
    assert f"fetched blob does not match its layer digest: app-agent-{sha}.tar" in result.stderr
    assert not (env["tmp"] / "pulled" / f"app-agent-{sha}.tar").exists()
    assert not (env["tmp"] / ".pulled.partial" / f"app-agent-{sha}.tar").exists()


def test_pull_refuses_unrecorded_digest_and_wrong_trusted_key(env, signing, tmp_path):
    sha = sha_of(13)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    digest = record_rows(env)[0][1]
    unknown = run(env, "pull", "--digest", "sha256:" + "f" * 64, "--out", str(tmp_path / "a"),
                  "--trusted-pub", str(signing[1]))
    assert unknown.returncode == 2 and "digest is not in the release-archive record" in unknown.stderr
    wrong = run(env, "pull", "--digest", digest, "--out", str(tmp_path / "b"),
                "--trusted-pub", str(gen_other_pub(tmp_path)))
    assert wrong.returncode == 2 and "trusted key does not match the recorded signing key" in wrong.stderr
    missing = run(env, "pull", "--digest", digest, "--out", str(tmp_path / "c"),
                  "--trusted-pub", str(tmp_path / "absent.pub"))
    assert missing.returncode == 2 and "trusted public key file not found" in missing.stderr
    assert env["registry"].requests == []


def test_pull_reverifies_signature_of_a_self_consistent_substituted_artifact(env, signing):
    """Registry bytes that match their own layer digests still fail the signed checksums."""
    sha = sha_of(16)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    data = state(env)
    old = record_rows(env)[0][1]
    manifest = json.loads(bytes.fromhex(data["manifests"].pop(old)))
    substitute = b"substituted image bytes"
    hexd = hashlib.sha256(substitute).hexdigest()
    data["blobs"][hexd] = substitute.hex()
    for layer in manifest["layers"]:
        if layer["annotations"]["org.opencontainers.image.title"] == f"app-agent-{sha}.tar":
            layer.update(digest="sha256:" + hexd, size=len(substitute))
    raw = json.dumps(manifest, separators=(",", ":")).encode()
    new = "sha256:" + hashlib.sha256(raw).hexdigest()
    data["manifests"][new] = raw.hex()
    env["state"].write_text(json.dumps(data))
    env["record"].write_text(env["record"].read_text().replace(old, new))
    result = pull(env, signing, new, env["tmp"] / "pulled")
    assert result.returncode == 2
    assert f"member checksum mismatch: app-agent-{sha}.tar" in result.stderr
    assert "re-verified" not in result.stdout


def test_pull_resumes_a_blob_after_dropped_connections(env, signing):
    """Every response is cut after 15,000 bytes; each retry resumes with Range."""
    sha = sha_of(14)
    bundle = make_bundle(env["tmp"], signing, sha)
    assert push(env, signing, bundle, sha).returncode == 0
    agent = layer_hex(env, f"app-agent-{sha}.tar")
    env["registry"].faults["drop"] = {agent: {"after": 15_000, "times": None}}
    out = env["tmp"] / "pulled"
    result = pull(env, signing, record_rows(env)[0][1], out, "--attempts", "2")
    assert result.returncode == 0, result.stderr
    assert env["registry"].storage_ranges(agent) == [None, "bytes=15000-", "bytes=30000-"]
    assert "resuming app-agent-" in result.stderr
    for member in bundle.iterdir():
        assert (out / member.name).read_bytes() == member.read_bytes()
    assert not (env["tmp"] / ".pulled.partial").exists()


def test_pull_resumes_a_partial_left_by_an_earlier_run(env, signing):
    sha = sha_of(17)
    bundle = make_bundle(env["tmp"], signing, sha)
    assert push(env, signing, bundle, sha).returncode == 0
    agent = layer_hex(env, f"app-agent-{sha}.tar")
    out = env["tmp"] / "pulled"
    out.mkdir()
    shutil.copyfile(bundle / "MANIFEST.txt", out / "MANIFEST.txt")  # a completed member
    (out / "SHA256SUMS").write_text("truncated")                  # a damaged member
    partial = env["tmp"] / ".pulled.partial"
    partial.mkdir()
    (partial / f"app-agent-{sha}.tar").write_bytes((bundle / f"app-agent-{sha}.tar").read_bytes()[:12_345])
    result = pull(env, signing, record_rows(env)[0][1], out)
    assert result.returncode == 0, result.stderr
    assert env["registry"].storage_ranges(agent) == ["bytes=12345-"]
    manifest_hex = hashlib.sha256((bundle / "MANIFEST.txt").read_bytes()).hexdigest()
    assert env["registry"].storage_ranges(manifest_hex) == []
    for member in bundle.iterdir():
        assert (out / member.name).read_bytes() == member.read_bytes()


def test_pull_restarts_cleanly_when_server_ignores_range(env, signing):
    sha = sha_of(18)
    bundle = make_bundle(env["tmp"], signing, sha)
    assert push(env, signing, bundle, sha).returncode == 0
    agent = layer_hex(env, f"app-agent-{sha}.tar")
    env["registry"].faults.update(drop={agent: {"after": 15_000, "times": 1}}, ignore_range=agent)
    out = env["tmp"] / "pulled"
    result = pull(env, signing, record_rows(env)[0][1], out)
    assert result.returncode == 0, result.stderr
    # The resume asked for a Range, got a full 200, and rewrote the file from byte 0.
    assert env["registry"].storage_ranges(agent) == [None, "bytes=15000-"]
    assert (out / f"app-agent-{sha}.tar").read_bytes() == (bundle / f"app-agent-{sha}.tar").read_bytes()


def test_pull_refuses_a_corrupted_resumed_byte(env, signing):
    sha = sha_of(19)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    agent = layer_hex(env, f"app-agent-{sha}.tar")
    env["registry"].faults.update(drop={agent: {"after": 15_000, "times": 1}}, corrupt_resumed=agent)
    out = env["tmp"] / "pulled"
    result = pull(env, signing, record_rows(env)[0][1], out)
    assert result.returncode == 2
    assert f"fetched blob does not match its layer digest: app-agent-{sha}.tar" in result.stderr
    assert not (out / f"app-agent-{sha}.tar").exists()
    assert not (env["tmp"] / ".pulled.partial" / f"app-agent-{sha}.tar").exists()
    assert "re-verified" not in result.stdout


def test_blob_redirect_never_forwards_authorization_and_token_is_never_printed(env, signing):
    sha = sha_of(20)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    result = pull(env, signing, record_rows(env)[0][1], env["tmp"] / "pulled")
    assert result.returncode == 0, result.stderr
    requests = env["registry"].requests
    token_requests = [r for r in requests if r["path"].startswith("/token?")]
    assert len(token_requests) == 1 and token_requests[0]["auth"].startswith("Basic ")
    blob_requests = [r for r in requests if r["server"] == "registry" and "/blobs/" in r["path"]]
    assert blob_requests[0]["auth"] is None  # the challenge
    assert {r["auth"] for r in blob_requests[1:]} == {f"Bearer {TOKEN}"}
    storage = [r for r in requests if r["server"] == "storage"]
    assert storage and all(r["auth"] is None for r in storage)
    for secret in (TOKEN, "gho_fake"):
        assert secret not in result.stdout + result.stderr


def test_pull_refuses_when_blob_downloads_make_no_progress(env, signing):
    sha = sha_of(21)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    env["registry"].faults["status"] = 503
    result = pull(env, signing, record_rows(env)[0][1], env["tmp"] / "pulled", "--attempts", "3")
    assert result.returncode == 2
    assert "blob download made no progress in 3 attempts" in result.stderr
    blob_gets = [r for r in env["registry"].requests if r["server"] == "registry" and "/blobs/" in r["path"]]
    assert len(blob_gets) == 1 + 3  # one challenge, then three bounded attempts


def test_pull_refuses_foreign_files_in_output(env, signing):
    sha = sha_of(15)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    out = env["tmp"] / "pulled"
    out.mkdir()
    (out / "operator-notes.txt").write_text("not a member")
    result = pull(env, signing, record_rows(env)[0][1], out)
    assert result.returncode == 2
    assert "output directory must be empty or hold only this artifact's members" in result.stderr
    assert (out / "operator-notes.txt").read_text() == "not a member"


# ------------------------------------------------------------ list and prune


def test_prune_keeps_exactly_three_newest_by_date_then_next_push_works(env, signing):
    # Pushed out of date order: prune ranks by the recorded packed date.
    releases = [(sha_of(20), "2026-10-03T00:00:00Z"), (sha_of(21), "2026-10-01T00:00:00Z"),
                (sha_of(22), "2026-10-04T00:00:00Z"), (sha_of(23), "2026-10-02T00:00:00Z")]
    for sha, date in releases:
        assert push(env, signing, make_bundle(env["tmp"], signing, sha, date), sha).returncode == 0
    oldest_digest = state(env)["tags"][sha_of(21)]

    listed = run(env, "list")
    assert listed.returncode == 0
    assert [line.split()[0] for line in listed.stdout.splitlines()] == [
        sha_of(22), sha_of(20), sha_of(23), sha_of(21)]
    assert listed.stdout.splitlines()[-1].endswith("prune")

    pruned = run(env, "prune")
    assert pruned.returncode == 0, pruned.stderr
    assert [row[0] for row in record_rows(env)] == [sha_of(22), sha_of(20), sha_of(23)]
    assert state(env)["deleted"] == [oldest_digest]
    assert sha_of(21) not in state(env)["tags"]
    assert len(state(env)["manifests"]) == 3

    again = run(env, "prune")  # converges: nothing more to delete
    assert again.returncode == 0 and "nothing to prune" in again.stdout
    assert len(state(env)["deleted"]) == 1

    new = sha_of(24)
    bundle = make_bundle(env["tmp"], signing, new, "2026-10-05T00:00:00Z")
    assert push(env, signing, bundle, new).returncode == 0
    assert record_rows(env)[0][0] == new
    out = env["tmp"] / "pulled-new"
    pulled = pull(env, signing, record_rows(env)[0][1], out)
    assert pulled.returncode == 0, pulled.stderr
    assert run(env, "prune").returncode == 0
    assert [row[0] for row in record_rows(env)] == [new, sha_of(22), sha_of(20)]

    remote = run(env, "list", "--remote")
    assert remote.returncode == 0, remote.stderr
    assert remote.stdout.count("remote=ok") == 3
    assert "package visibility: private" in remote.stdout


def test_prune_resumes_after_registry_version_already_deleted(env, signing):
    shas = [sha_of(n) for n in range(30, 34)]
    for index, sha in enumerate(shas):
        date = f"2026-10-0{index + 1}T00:00:00Z"
        assert push(env, signing, make_bundle(env["tmp"], signing, sha, date), sha).returncode == 0
    data = state(env)  # an earlier prune deleted the version, then stopped before rewriting the record
    digest = data["tags"].pop(shas[0])
    del data["manifests"][digest]
    env["state"].write_text(json.dumps(data))
    result = run(env, "prune")
    assert result.returncode == 0, result.stderr
    assert [row[0] for row in record_rows(env)] == shas[:0:-1]
    assert state(env)["deleted"] == []


@pytest.mark.parametrize("keep", ["0", "1", "2"])
def test_prune_refuses_to_keep_fewer_than_three(env, signing, keep):
    for n in range(4):
        sha = sha_of(40 + n)
        assert push(env, signing, make_bundle(env["tmp"], signing, sha, f"2026-10-0{n + 1}T00:00:00Z"),
                    sha).returncode == 0
    result = run(env, "prune", "--keep", keep)
    assert result.returncode == 2
    assert "prune keeps at least 3 releases" in result.stderr
    assert len(record_rows(env)) == 4 and state(env)["deleted"] == []


def test_sigterm_during_push_removes_the_token_config(env, signing):
    sha = sha_of(50)
    bundle = make_bundle(env["tmp"], signing, sha)
    marker = env["tmp"] / "push-started"
    process = subprocess.Popen(
        command(env, "push", "--bundle", str(bundle), "--release-sha", sha, "--trusted-pub", str(signing[1])),
        env={**env["env"], "FAKE_BLOCK_PUSH": str(marker)}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert process.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    configs = list(env["tmp"].glob("release-archive-*/registry-config.json"))
    assert len(configs) == 1  # the login wrote the token-bearing config
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=30) == 128 + signal.SIGTERM
    assert list(env["tmp"].glob("release-archive-*")) == []
    assert not env["record"].exists()


def test_contract_forbids_public_linking_pushes():
    section = (REPO / "docs" / "deploy.md").read_text().split('<a id="release-archive"></a>', 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "GITHUB_TOKEN" in section and "org.opencontainers.image.source" in section
    assert "org.opencontainers.image.source" not in SCRIPT.read_text().replace(
        "No org.opencontainers.image.source", "")


def test_record_rejects_malformed_row(env):
    env["record"].write_text("# header\nnot-a-sha sha256:abc key 2026\n")
    result = run(env, "list")
    assert result.returncode == 2
    assert "malformed row" in result.stderr


def test_checked_in_record_is_well_formed():
    rows = [line.split() for line in (REPO / "release-archive.txt").read_text().splitlines()
            if line and not line.startswith("#")]
    assert 1 <= len(rows) <= 3
    result = subprocess.run([sys.executable, str(SCRIPT), "list"], capture_output=True, text=True,
                            check=False, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr
    assert [line.split()[0] for line in result.stdout.splitlines()] == [row[0] for row in rows]
