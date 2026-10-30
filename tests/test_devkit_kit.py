"""The signed dev kit (#1118, ADR-0009 §8): scripts/devkit/pack.sh and the
install.sh it ships, end to end and hermetically.

pack.sh runs in a throwaway git repository against a stub skopeo that "pulls" a
real synthetic OCI layout, fake pinned oras archives and a loopback GitHub API.
Its output, the kit directory, is then consumed by its own install.sh against a
stub registry that stores and serves the pushed manifest bytes, so every check
crosses the real producer-to-consumer boundary.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.helpers_airgap import (
    REPO,
    copy_chart,
    copy_license_inputs,
    gen_other_pub,
    gen_sign_keypair,
    make_bin_tree,
    symlink_tools,
)
from tests.helpers_image_inventory import tar_bytes

TOOLS = ("sh", "dirname", "basename", "awk", "sed", "git", "tar", "gzip", "sha256sum", "date", "cat",
         "chmod", "cp", "mkdir", "rm", "mv", "grep", "tr", "cut", "head", "ls", "openssl", "python3",
         "mktemp", "uname", "cmp", "find", "sort", "uniq", "du", "wc", "env")
ORAS_EXE = b"MZ fake oras.exe for the kit tests\n"
ORAS_LINUX = b"\x7fELF fake linux oras for the kit tests\n"
ORAS_LICENSE = b"Apache License 2.0 (test copy)\n"
ISSUES = {
    1118: {"title": "Tracker", "body": "the plan", "comments": [f"decision {n}" for n in range(3)]},
    588: {"title": "Parking lot", "body": "findings", "comments": []},
}
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"

# Stub registry: `skopeo copy oci:DIR docker://REPO:TAG` stores DIR's manifest
# bytes under the tag and the digest; `skopeo inspect --raw docker://REF` serves
# them. Knobs reproduce registries that convert images or move tags.
STUB_SKOPEO = r'''#!{python}
import hashlib, json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["SKOPEO_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
store = Path(os.environ["STUB_REGISTRY"])
def ref_path(ref):
    ref = ref.removeprefix("docker://")
    if "@" in ref:
        repo, _, name = ref.partition("@")
    else:
        repo, _, name = ref.rpartition(":")
    return store / repo.replace("/", "_") / name.replace(":", "_")
if args[0] == "copy":
    src, dst = args[-2], args[-1]
    if src.startswith("docker://"):
        shutil.copytree(os.environ["DEVKIT_TEST_LAYOUT"], dst.removeprefix("oci:"))
        sys.exit(0)
    if os.environ.get("STUB_COPY_FAIL"):
        sys.exit(1)
    layout = Path(src.removeprefix("oci:"))
    digest = json.loads((layout / "index.json").read_text())["manifests"][0]["digest"]
    raw = (layout / "blobs/sha256" / digest[7:]).read_bytes()
    repo = dst.removeprefix("docker://").rpartition(":")[0]
    for target in (ref_path(dst), ref_path(f"{repo}@{digest}")):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    sys.exit(0)
if args[0] == "inspect":
    path = ref_path(args[-1])
    if not path.is_file():
        sys.exit(1)
    raw = path.read_bytes()
    if ("@" in args[-1] and os.environ.get("STUB_CONVERT")) or ("@" not in args[-1] and os.environ.get("STUB_TAG_MOVED")):
        raw = raw.replace(b'"schemaVersion":2', b'"schemaVersion": 2')
    sys.stdout.buffer.write(raw)
    sys.exit(0)
sys.exit(2)
'''

STUB_OC = r'''#!/bin/sh
printf '%s\n' "$*" >> "$OC_LOG"
printf '%s\n' "$STUB_POD_IMAGE_ID"
'''


def oci_layout(directory: Path, marker: bytes) -> str:
    """Write a real one-image OCI layout directory; return its manifest digest."""
    blobs = directory / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    def put(data: bytes, media_type: str) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        (blobs / digest).write_bytes(data)
        return {"mediaType": media_type, "digest": f"sha256:{digest}", "size": len(data)}

    layer = tar_bytes({"opt/devkit/marker": marker})
    config = json.dumps({"os": "linux", "architecture": "amd64", "rootfs": {
        "type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(layer).hexdigest()]}}).encode()
    manifest = json.dumps({"schemaVersion": 2, "mediaType": OCI_MANIFEST,
                           "config": put(config, "application/vnd.oci.image.config.v1+json"),
                           "layers": [put(layer, "application/vnd.oci.image.layer.v1.tar")]},
                          separators=(",", ":")).encode()
    entry = put(manifest, OCI_MANIFEST)
    (directory / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [entry]}))
    (directory / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    return entry["digest"]


class _GitHub(BaseHTTPRequestHandler):
    issues: dict = ISSUES

    def do_GET(self):
        path, _, query = self.path.partition("?")
        parts = path.strip("/").split("/")
        body: object
        if len(parts) == 5 and parts[3] == "issues":
            number = int(parts[4])
            issue = self.issues[number]
            body = {"number": number, "title": issue["title"], "body": issue["body"], "state": "open",
                    "labels": [{"name": "tracker"}], "user": {"login": "maintainer"},
                    "html_url": f"https://github.com/acme/kit/issues/{number}"}
        else:
            page = int(dict(item.split("=") for item in query.split("&"))["page"])
            comments = self.issues[int(parts[4])]["comments"]
            body = [] if page > 1 else [{"id": index, "body": text, "user": {"login": "maintainer"}}
                                        for index, text in enumerate(comments)]
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def github():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _write_oras_archives(tree: Path) -> dict[str, Path]:
    win = tree / "oras_1.3.4_windows_amd64.zip"
    with zipfile.ZipFile(win, "w") as archive:
        archive.writestr("oras.exe", ORAS_EXE)
        archive.writestr("LICENSE", ORAS_LICENSE)
    linux = tree / "oras_1.3.4_linux_amd64.tar.gz"
    with tarfile.open(linux, "w:gz") as archive:
        for name, data in (("oras", ORAS_LINUX), ("LICENSE", ORAS_LICENSE)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    for pin, archive, binary in (("oras-windows-pin.txt", win, ORAS_EXE), ("oras-pin.txt", linux, ORAS_LINUX)):
        text = (REPO / "scripts/tools" / pin).read_text()
        lines = []
        for line in text.splitlines():
            if line.startswith("sha256:"):
                line = f"sha256: {sha(archive.read_bytes())}"
            elif line.startswith("binary-sha256:"):
                line = f"binary-sha256: {sha(binary)}"
            elif line.startswith("license-sha256:"):
                line = f"license-sha256: {sha(ORAS_LICENSE)}"
            lines.append(line)
        (tree / "scripts/tools" / pin).write_text("\n".join(lines) + "\n")
    return {"DEVKIT_ORAS_ARCHIVE": win, "DEVKIT_ORAS_LINUX_ARCHIVE": linux}


def _git(tree: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True, text=True).stdout.strip()


def _set_pin(tree: Path, digest: str, key: str | None = None) -> None:
    if key is None:
        key = subprocess.run([sys.executable, "scripts/devkit_key.py"], cwd=tree, check=True,
                             capture_output=True, text=True).stdout.strip()
    (tree / "scripts/tools/devkit-pin.txt").write_text(f"key: {key}\ndigest: {digest}\n")


@pytest.fixture
def pack_tree(tmp_path, github):
    tree = tmp_path / "repo"
    tree.mkdir()
    make_bin_tree(tree, ["common.sh", "image_manifest.py"])
    (tree / "scripts/devkit").mkdir()
    for name in ("pack.sh", "install.sh", "issue_snapshot.py", "snapshot-issues.txt"):
        shutil.copy(REPO / "scripts/devkit" / name, tree / "scripts/devkit" / name)
    for relative in ("scripts/devkit_key.py", "scripts/dependency_lock.py", "requirements.lock.txt",
                     "requirements.dev.lock.txt", "images.txt", "deploy/workspace/bastion.template.yaml",
                     "docs/runbooks/devkit-windows.md", "scripts/tools/task-pin.txt",
                     "scripts/tools/helm-pin.txt", "scripts/tools/oc-pin.txt", "scripts/tools/install-task.sh",
                     "scripts/tools/task-artifact.sh", "scripts/tools/install-helm.sh"):
        (tree / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / relative, tree / relative)
    shutil.copytree(REPO / "locks", tree / "locks")
    shutil.copytree(REPO / "images", tree / "images")
    copy_license_inputs(tree)
    copy_chart(tree)
    archives = _write_oras_archives(tree)
    layout = tmp_path / "registry-layout"
    digest = oci_layout(layout, b"pinned")
    _set_pin(tree, digest)
    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "config", "user.name", "Test")
    _git(tree, "config", "user.email", "test@example.com")
    _git(tree, "remote", "add", "origin", "https://github.com/acme/kit.git")
    _git(tree, "add", ".")
    _git(tree, "commit", "-q", "-m", "init")
    symlink_tools(tree, TOOLS)
    skopeo = tree / "bin/skopeo"
    skopeo.write_text(STUB_SKOPEO.replace("{python}", sys.executable))
    skopeo.chmod(0o755)
    oc = tree / "bin/oc"
    oc.write_text(STUB_OC)
    oc.chmod(0o755)
    key = gen_sign_keypair(tmp_path)
    pub = tmp_path / "release-signing.pub"
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)], check=True)
    env = {
        "PATH": str(tree / "bin"),
        "HOME": str(tmp_path),
        "SKOPEO_LOG": str(tmp_path / "skopeo.log"),
        "OC_LOG": str(tmp_path / "oc.log"),
        "STUB_REGISTRY": str(tmp_path / "registry"),
        "DEVKIT_TEST_LAYOUT": str(layout),
        "DEVKIT_GITHUB_API": github,
        "SNEAKERNET_SIGNING_KEY": str(key),
        **{name: str(path) for name, path in archives.items()},
    }
    return {"tree": tree, "env": env, "digest": digest, "pub": pub, "tmp": tmp_path}


def run_pack(ctx, *args, **env):
    return subprocess.run(["sh", "scripts/devkit/pack.sh", *args], cwd=ctx["tree"], capture_output=True,
                          text=True, env={**ctx["env"], **env}, check=False)


def kit_dir(ctx) -> Path:
    kits = sorted((ctx["tree"] / "dist").glob("mfr-devkit-*"))
    assert len(kits) == 1, kits
    return kits[0]


def session(ctx, *, kubeconfig: bool = True) -> Path:
    root = ctx["tmp"] / "session"
    private = root / "private"
    private.mkdir(parents=True, exist_ok=True)
    (private / "auth.json").write_text('{"auths":{"quay.example":{"auth":"eDp5"}}}\n')
    if kubeconfig:
        (private / "kubeconfig").write_text("{}\n")
    return root


def run_install(ctx, kit: Path, *args, **env):
    full = {**ctx["env"], "MFR_SESSION_DIR": str(session(ctx)), "SNEAKERNET_TRUSTED_PUB": str(ctx["pub"]), **env}
    full = {k: v for k, v in full.items() if v is not None}
    return subprocess.run(["sh", str(kit / "install.sh"), "--registry", "quay.example/mf/devkit", *args],
                          capture_output=True, text=True, env=full, check=False)


@pytest.fixture
def packed(pack_tree):
    done = run_pack(pack_tree)
    assert done.returncode == 0, done.stderr
    return pack_tree, kit_dir(pack_tree), done.stdout


def _manifest(kit: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (kit / "MANIFEST.txt").read_text().splitlines():
        name, _, value = line.partition(": ")
        if name != "pin_sha256":
            fields[name] = value
    return fields


def test_pack_writes_a_signed_plain_file_kit_whose_manifest_binds_the_pins(packed):
    ctx, kit, stdout = packed
    tree = ctx["tree"]
    sys.path.insert(0, str(tree / "scripts"))
    try:
        from devkit_key import inputs, key
        expected_key, expected_inputs = key(tree), inputs(tree)
    finally:
        sys.path.remove(str(tree / "scripts"))
    assert kit.name == f"mfr-devkit-{expected_key}"
    # Plain files only: no link, no archive member at the top, NTFS-safe names.
    every = sorted(p for p in kit.rglob("*"))
    assert all(p.is_dir() or (p.is_file() and not p.is_symlink()) for p in every)
    assert not [p for p in kit.iterdir() if p.suffix in (".tar", ".zip", ".tgz", ".gz")]
    assert not [p for p in every if ":" in p.relative_to(kit).as_posix()]
    # SHA256SUMS lists every file except the signature trio, each with its real hash.
    files = {p.relative_to(kit).as_posix() for p in every if p.is_file()}
    sums = dict(reversed(line.split("  ", 1)) for line in (kit / "SHA256SUMS").read_text().splitlines())
    assert set(sums) == files - {"SHA256SUMS", "SHA256SUMS.sig", "SHA256SUMS.sha256"}
    assert all(hashlib.sha256((kit / name).read_bytes()).hexdigest() == want for name, want in sums.items())
    verify = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(ctx["pub"]), "-signature",
                             str(kit / "SHA256SUMS.sig"), str(kit / "SHA256SUMS")], capture_output=True, check=False)
    assert verify.returncode == 0
    fingerprint = hashlib.sha256((kit / "SHA256SUMS").read_bytes()).hexdigest()
    assert (kit / "SHA256SUMS.sha256").read_text() == f"{fingerprint}  SHA256SUMS\n"
    assert f"Trust anchor (publish on GitHub, carry separately): {fingerprint[:16]}" in stdout
    # MANIFEST: key, digest, commit, snapshot and every kit key input's digest.
    fields = _manifest(kit)
    assert fields["key"] == expected_key
    assert fields["image_digest"] == ctx["digest"]
    assert fields["image_format"] == "oci-layout"
    assert fields["commit"] == _git(tree, "rev-parse", "HEAD")
    assert fields["snapshot_issues"] == "1118 588"
    assert fields["oras_sha256"] == hashlib.sha256(ORAS_EXE).hexdigest()
    assert fields["oras_linux_sha256"] == hashlib.sha256(ORAS_LINUX).hexdigest()
    pins = [line.split(" ", 2)[1:] for line in (kit / "MANIFEST.txt").read_text().splitlines()
            if line.startswith("pin_sha256: ")]
    assert [path for _, path in pins] == expected_inputs
    assert all(hashlib.sha256((tree / path).read_bytes()).hexdigest() == digest for digest, path in pins)
    # The extracted binaries, not their archives; the shipped copies are the sources'.
    assert (kit / "oras.exe").read_bytes() == ORAS_EXE
    assert (kit / "oras").read_bytes() == ORAS_LINUX
    for member, source in (("install.sh", "scripts/devkit/install.sh"),
                           ("bastion.template.yaml", "deploy/workspace/bastion.template.yaml"),
                           ("devkit-windows.md", "docs/runbooks/devkit-windows.md"),
                           ("image_manifest.py", "scripts/airgap/image_manifest.py")):
        assert (kit / member).read_bytes() == (tree / source).read_bytes()
    snapshot = json.loads((kit / "issues.json").read_text())
    assert snapshot["snapshot_time"] == fields["snapshot_time"]
    assert [(i["number"], [c["body"] for c in i["comments"]]) for i in snapshot["issues"]] == [
        (1118, ISSUES[1118]["comments"]), (588, [])]
    sbom = json.loads((kit / "sbom.json").read_text())
    assert sbom["image"]["digest"] == ctx["digest"] and sbom["kit_key"] == expected_key
    assert "oras-LICENSE" in (kit / "THIRD-PARTY-NOTICES.txt").read_text()


def test_pack_refuses_a_pending_digest_and_writes_no_kit(pack_tree):
    _set_pin(pack_tree["tree"], "sha256:PENDING")
    _git(pack_tree["tree"], "commit", "-q", "-am", "pending")
    done = run_pack(pack_tree)
    assert done.returncode != 0
    assert "digest is sha256:PENDING: record the digest .github/workflows/devkit.yml pushed" in done.stderr
    assert not list((pack_tree["tree"] / "dist").glob("mfr-devkit-*"))
    assert not (pack_tree["tmp"] / "skopeo.log").exists()


def test_pack_refuses_a_stale_key_and_writes_no_kit(pack_tree):
    _set_pin(pack_tree["tree"], pack_tree["digest"], key="0" * 64)
    _git(pack_tree["tree"], "commit", "-q", "-am", "stale")
    done = run_pack(pack_tree)
    assert done.returncode != 0
    assert "is stale: the kit inputs changed" in done.stderr
    assert not list((pack_tree["tree"] / "dist").glob("mfr-devkit-*"))


def test_pack_refuses_a_pulled_image_that_is_not_the_pinned_digest(pack_tree):
    other = pack_tree["tmp"] / "other-layout"
    oci_layout(other, b"not the pinned image")
    done = run_pack(pack_tree, DEVKIT_TEST_LAYOUT=str(other))
    assert done.returncode != 0
    assert "not the pinned" in done.stderr
    assert not list((pack_tree["tree"] / "dist").glob("mfr-devkit-*"))


def test_pack_refuses_an_oras_archive_off_its_pin(pack_tree):
    bad = pack_tree["tmp"] / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("oras.exe", b"tampered")
        archive.writestr("LICENSE", ORAS_LICENSE)
    done = run_pack(pack_tree, DEVKIT_ORAS_ARCHIVE=str(bad))
    assert done.returncode != 0
    assert "does not match scripts/tools/oras-windows-pin.txt" in done.stderr


def test_snapshot_only_resign_keeps_the_image_and_records_the_new_snapshot(packed):
    ctx, kit, _ = packed
    first = _manifest(kit)
    aside = ctx["tmp"] / "previous-kit"
    kit.rename(aside)
    _GitHub.issues = {**ISSUES, 588: {**ISSUES[588], "comments": ["a later note"]}}
    try:
        done = run_pack(ctx, "--reuse-image", str(aside / "image"),
                        DEVKIT_TEST_LAYOUT=str(ctx["tmp"] / "no-registry"))
    finally:
        _GitHub.issues = ISSUES
    assert done.returncode == 0, done.stderr
    second = _manifest(kit_dir(ctx))
    assert second["image_digest"] == first["image_digest"] and second["key"] == first["key"]
    issues = json.loads((kit_dir(ctx) / "issues.json").read_text())["issues"]
    assert issues[1]["comments"][0]["body"] == "a later note"
    assert second["snapshot_time"] >= first["snapshot_time"]


def test_snapshot_refuses_credential_shaped_text_naming_only_where(pack_tree):
    planted = "ghp_" + "A" * 36
    _GitHub.issues = {**ISSUES, 1118: {**ISSUES[1118], "comments": ["fine", f"token {planted}"]}}
    try:
        done = run_pack(pack_tree)
    finally:
        _GitHub.issues = ISSUES
    assert done.returncode != 0
    assert "issue #1118 comment 1 matches a credential pattern" in done.stderr
    assert planted not in done.stderr + done.stdout
    assert not list((pack_tree["tree"] / "dist").glob("mfr-devkit-*"))


def test_install_verifies_pushes_reads_back_and_checks_the_pod(packed):
    ctx, kit, _ = packed
    digest = ctx["digest"]
    key = _manifest(kit)["key"]
    done = run_install(ctx, kit, "--verify-pod", "mfr-bastion",
                       STUB_POD_IMAGE_ID=f"quay.example/mf/devkit@{digest}")
    assert done.returncode == 0, done.stderr
    assert f"readback: quay.example/mf/devkit:{key} = {digest}" in done.stdout
    assert f"pod: mfr-bastion runs {digest}" in done.stdout
    calls = [json.loads(line) for line in Path(ctx["env"]["SKOPEO_LOG"]).read_text().splitlines()]
    copy = next(c for c in calls if c[0] == "copy" and c[-1].startswith("docker://quay.example"))
    auth = str(ctx["tmp"] / "session/private/auth.json")
    assert "--preserve-digests" in copy and copy[copy.index("--dest-authfile") + 1] == auth
    assert copy[-2:] == [f"oci:{kit}/image", f"docker://quay.example/mf/devkit:{key}"]
    oc = Path(ctx["env"]["OC_LOG"]).read_text()
    assert f"--kubeconfig {ctx['tmp']}/session/private/kubeconfig get pod mfr-bastion" in oc
    # Repeatable: the next ordinary run after a successful one passes again.
    again = run_install(ctx, kit)
    assert again.returncode == 0, again.stderr


def test_install_accepts_a_kit_copied_as_plain_bytes_without_modes(packed):
    ctx, kit, _ = packed
    copy = ctx["tmp"] / "transfer" / kit.name
    for path in kit.rglob("*"):
        target = copy / path.relative_to(kit)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            target.chmod(0o644)
    done = run_install(ctx, copy)
    assert done.returncode == 0, done.stderr


def _tamper_member(kit: Path) -> None:
    blob = next((kit / "image/blobs/sha256").iterdir())
    blob.write_bytes(blob.read_bytes() + b"x")


def _extra_file(kit: Path) -> None:
    (kit / "notes.txt").write_text("not signed\n")


def _link(kit: Path) -> None:
    (kit / "link").symlink_to("MANIFEST.txt")


def _edit_manifest(kit: Path) -> None:
    text = (kit / "MANIFEST.txt").read_text()
    (kit / "MANIFEST.txt").write_text(text.replace("signed: ephemeral", "signed: true"))


@pytest.mark.parametrize(("damage", "message"), [
    (_tamper_member, "a kit member is missing or differs from SHA256SUMS"),
    (_extra_file, "the kit holds files SHA256SUMS does not list"),
    (_link, "the kit holds a link or special file"),
    (_edit_manifest, "a kit member is missing or differs from SHA256SUMS"),
])
def test_install_refuses_a_damaged_kit_before_any_push(packed, damage, message):
    ctx, kit, _ = packed
    damage(kit)
    done = run_install(ctx, kit)
    assert done.returncode != 0
    assert message in done.stderr
    assert not Path(ctx["env"]["STUB_REGISTRY"]).exists()


@pytest.mark.parametrize(("trust", "message"), [
    (lambda ctx, kit: None, "SNEAKERNET_TRUSTED_PUB is unset"),
    (lambda ctx, kit: str(kit / "sneakernet-signing.pub"), "SNEAKERNET_TRUSTED_PUB points inside the kit"),
    (lambda ctx, kit: str(gen_other_pub(ctx["tmp"])), "the kit's signing key is not SNEAKERNET_TRUSTED_PUB"),
])
def test_install_requires_an_independent_trusted_key(packed, trust, message):
    ctx, kit, _ = packed
    done = run_install(ctx, kit, SNEAKERNET_TRUSTED_PUB=trust(ctx, kit))
    assert done.returncode != 0
    assert message in done.stderr
    assert not Path(ctx["env"]["STUB_REGISTRY"]).exists()


def test_install_refuses_a_signature_the_trusted_key_does_not_verify(packed):
    ctx, kit, _ = packed
    other = gen_sign_keypair(ctx["tmp"], "other.key")
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(other), "-out", str(kit / "SHA256SUMS.sig"),
                    str(kit / "SHA256SUMS")], check=True)
    done = run_install(ctx, kit)
    assert done.returncode != 0
    assert "the SHA256SUMS signature does not verify with SNEAKERNET_TRUSTED_PUB" in done.stderr


@pytest.mark.parametrize(("knob", "message"), [
    ("STUB_CONVERT", "the registry serves different manifest bytes"),
    ("STUB_TAG_MOVED", "points at another digest than this kit's image"),
    ("STUB_COPY_FAIL", "the copy to quay.example/mf/devkit failed"),
])
def test_install_refuses_a_registry_that_does_not_read_back_the_kit_digest(packed, knob, message):
    ctx, kit, _ = packed
    done = run_install(ctx, kit, **{knob: "1"})
    assert done.returncode != 0
    assert message in done.stderr


def test_install_refuses_a_pod_running_another_image(packed):
    ctx, kit, _ = packed
    done = run_install(ctx, kit, "--verify-pod", "mfr-bastion",
                       STUB_POD_IMAGE_ID="quay.example/mf/devkit@sha256:" + "e" * 64)
    assert done.returncode != 0
    assert "pod mfr-bastion does not run this kit's image" in done.stderr


def test_install_refuses_without_the_session_registry_auth(packed):
    ctx, kit, _ = packed
    root = ctx["tmp"] / "token-only-session"
    (root / "private").mkdir(parents=True)
    (root / "private/kubeconfig").write_text("{}\n")
    done = run_install(ctx, kit, MFR_SESSION_DIR=str(root))
    assert done.returncode != 0
    assert "no session registry auth: run oc get secret" in done.stderr


@pytest.mark.parametrize("repository", ["https://quay.example/mf/devkit", "quay.example/mf/devkit:tag",
                                        "quay.example/mf/devkit@sha256:" + "a" * 64, "quay.example"])
def test_install_refuses_a_registry_reference_with_scheme_tag_or_no_path(packed, repository):
    ctx, kit, _ = packed
    full = {**ctx["env"], "MFR_SESSION_DIR": str(session(ctx)), "SNEAKERNET_TRUSTED_PUB": str(ctx["pub"])}
    done = subprocess.run(["sh", str(kit / "install.sh"), "--registry", repository], capture_output=True,
                          text=True, env=full, check=False)
    assert done.returncode != 0
    assert "--registry must be <registry>/<path> with no scheme, tag or digest" in done.stderr


def test_layout_reader_refuses_links_and_blobs_that_differ(tmp_path):
    sys.path.insert(0, str(REPO / "scripts/airgap"))
    try:
        import image_manifest
    finally:
        sys.path.remove(str(REPO / "scripts/airgap"))
    good = tmp_path / "good"
    digest = oci_layout(good, b"x")
    assert image_manifest.read_layout(good).digest == digest
    linked = tmp_path / "linked"
    shutil.copytree(good, linked)
    (linked / "extra").symlink_to("index.json")
    with pytest.raises(image_manifest.ArchiveError, match="link"):
        image_manifest.read_layout(linked)
    changed = tmp_path / "changed"
    shutil.copytree(good, changed)
    manifest = json.loads((changed / "blobs/sha256" / digest[7:]).read_bytes())
    layer = changed / "blobs/sha256" / manifest["layers"][0]["digest"][7:]
    layer.write_bytes(b"Z" + layer.read_bytes()[1:])
    with pytest.raises(image_manifest.BlobMismatch):
        image_manifest.read_layout(changed)
    assert os.path.isdir(good)


def test_install_refuses_a_kit_path_skopeo_would_read_as_another_layout(packed):
    """skopeo splits `oci:<path>` at ':' and would open a directory other than the verified one."""
    ctx, kit, _ = packed
    moved = ctx["tmp"] / "kits/a:b" / kit.name
    moved.parent.mkdir(parents=True)
    kit.rename(moved)
    done = run_install(ctx, moved)
    assert done.returncode != 0
    assert "the kit path contains ':'" in done.stderr
    assert not Path(ctx["env"]["STUB_REGISTRY"]).exists()
