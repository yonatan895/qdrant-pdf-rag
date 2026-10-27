"""Private release archive tool (#1118, decision D5; docs/deploy.md#release-archive).

scripts/release_archive.py runs as a real subprocess against stub `oras` and
`gh` executables that share one fake registry/package state file. The stub
registry computes real manifest and blob digests, so digest checks and
signature checks exercise the tool's actual verification, not a mock answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
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
    save(); sys.exit(0)
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
if cmd == "pull":
    digest = argv[1].split("@", 1)[1]; out = Path(argv[argv.index("--output") + 1])
    manifest = json.loads(bytes.fromhex(state["manifests"][digest]))
    for layer in manifest["layers"]:
        data = bytes.fromhex(state["blobs"][layer["digest"].split(":", 1)[1]])
        title = layer["annotations"]["org.opencontainers.image.title"]
        if title == os.environ.get("FAKE_TAMPER_LAYER"):
            data = data + b"tampered"
        (out / title).write_bytes(data)
    save(); sys.exit(0)
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
               "FAKE_PACKAGE": PACKAGE, "TMPDIR": str(tmp_path)}
    return {"stubs": stubs, "pin": pin, "state": state, "env": run_env,
            "record": tmp_path / "release-archive.txt", "tmp": tmp_path}


def run(env, *args, extra=None):
    command = [sys.executable, str(SCRIPT), "--record", str(env["record"]),
               "--oras", str(env["stubs"] / "oras"), "--oras-pin", str(env["pin"]), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False,
                          env={**env["env"], **(extra or {})}, stdin=subprocess.DEVNULL)


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
    (bundle / f"app-agent-{sha}.tar").write_bytes(os.urandom(4096))
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
    key_id = hashlib.sha256(signing[1].read_bytes()).hexdigest()
    assert record_rows(env) == [[sha, digest, key_id, "2026-10-06T20:47:05Z"]]

    out = env["tmp"] / "pulled"
    pulled = run(env, "pull", "--digest", digest, "--out", str(out), "--trusted-pub", str(signing[1]))
    assert pulled.returncode == 0, pulled.stderr
    assert "re-verified its signature" in pulled.stdout
    for member in bundle.iterdir():
        assert (out / member.name).read_bytes() == member.read_bytes()


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
    assert "pull" not in state(env)["calls"]
    assert not out.exists() or not any(out.iterdir())


def test_pull_refuses_tampered_layer_content(env, signing):
    sha = sha_of(12)
    assert push(env, signing, make_bundle(env["tmp"], signing, sha), sha).returncode == 0
    digest = record_rows(env)[0][1]
    result = run(env, "pull", "--digest", digest, "--out", str(env["tmp"] / "pulled"),
                 "--trusted-pub", str(signing[1]), extra={"FAKE_TAMPER_LAYER": f"app-agent-{sha}.tar"})
    assert result.returncode == 2
    assert f"member checksum mismatch: app-agent-{sha}.tar" in result.stderr


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
    assert "pull" not in state(env)["calls"]


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
    pulled = run(env, "pull", "--digest", record_rows(env)[0][1], "--out", str(out),
                 "--trusted-pub", str(signing[1]))
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
