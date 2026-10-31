"""The signed dev kit (#1118, ADR-0009 §8): scripts/devkit/pack.sh and the
install.sh it ships, end to end and hermetically; and scripts/devkit/link-tools.sh,
which gives a checkout inside the kit image its pinned .tools/bin tools.

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
from scripts import license_inventory as li

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
from tests.helpers_task_artifact import task_archive, task_members

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


def fenced_after(marker: str, path: Path) -> str:
    """The ```powershell block that follows a marker line, as its exact text."""
    lines = path.read_text(encoding="utf-8").split("\n")
    start = lines.index(marker)
    assert lines[start + 1] == "```powershell"
    end = lines.index("```", start + 2)
    return "".join(line + "\n" for line in lines[start + 2:end])


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


def _inventory(tree: Path) -> dict:
    return json.loads((tree / "licenses/inventory.json").read_text())


def _write_inventory(tree: Path, inventory: dict, *, approve: bool = True) -> None:
    """Record the tree's inventory; with ``approve`` the kit approval names its current
    digest (a test stand-in for the owner's record, never the repository's)."""
    if approve:
        inventory["devkit"]["approval"] = {"approval_ref": "test-owner-record",
                                           "approved_digest": li.kit_scope_digest(inventory)}
    (tree / "licenses/inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")


def _resolve_kit_inventory(tree: Path) -> None:
    """The fixture's owner: rebind the oras records to the fake pins, resolve every
    open kit decision and approve the result, so a pack can pass the licence gate."""
    _resolve_tools_only(tree)
    inventory = _inventory(tree)
    for entry in inventory["devkit"]["components"]:
        if entry["status"] == "pending-owner-decision":
            entry.update(status="approved", approval_ref="test-owner-record")
    _write_inventory(tree, inventory)


def _resolve_tools_only(tree: Path) -> None:
    """Rebind the oras records to the fixture's fake pins and nothing else."""
    inventory = _inventory(tree)
    for entry in inventory["devkit"]["components"]:
        if entry["kind"] == "tool":
            pin = li.read_pin(tree, entry["bind"]["pin"].removeprefix("scripts/tools/"))
            entry["bind"].update({k: pin[k] for k in li.KIT_TOOL_FIELDS if k in pin})
    _write_inventory(tree, inventory, approve=False)


def _commit_inventory(ctx, inventory: dict, *, approve: bool = True) -> None:
    _write_inventory(ctx["tree"], inventory, approve=approve)
    _git(ctx["tree"], "commit", "-q", "-am", "inventory")


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
                     "docs/runbooks/devkit-windows.md", "docs/adr/0009-gap-fork.md", "scripts/tools/task-pin.txt",
                     "scripts/tools/helm-pin.txt", "scripts/tools/oc-pin.txt", "scripts/tools/install-task.sh",
                     "scripts/tools/task-artifact.sh", "scripts/tools/install-helm.sh"):
        (tree / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / relative, tree / relative)
    shutil.copytree(REPO / "locks", tree / "locks")
    shutil.copytree(REPO / "images", tree / "images")
    copy_license_inputs(tree)
    copy_chart(tree)
    archives = _write_oras_archives(tree)
    _resolve_kit_inventory(tree)
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
        "DEVKIT_TASK_ARCHIVE": str(task_archive()),
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
    printed = stdout.split("----- anchor bootstrap -----\n", 1)[1].split("----- end anchor bootstrap -----", 1)[0]
    assert printed == fenced_after("<!-- devkit-anchor-bootstrap -->", tree / "docs/adr/0009-gap-fork.md")
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
    assert fields["license_status"].startswith("OK (") and "kit_unresolved=0 kit_approval=current" in fields["license_status"]
    # Enforced by default, even with an ephemeral key.
    assert fields["license_gate"] == "enforced" and fields["signed"] == "ephemeral"
    assert (kit / "task-LICENSE").read_bytes() == task_members()["task-LICENSE"]


def test_kit_notices_name_every_kit_component_and_carry_the_pinned_oras_licence(packed):
    ctx, kit, _ = packed
    notices = (kit / "THIRD-PARTY-NOTICES.txt").read_text()
    inventory = _inventory(ctx["tree"])
    assert "Owner approval of the kit inventory: current" in notices
    assert f"Kit scope digest: {li.kit_scope_digest(inventory)}" in notices
    # Every wheel the dev image installs (the dev lock, independently read), every RPM
    # NEVRA of the package list, every tool pin and the shared base and project.
    lock = [line.split(" ", 1)[0] for line in (ctx["tree"] / "requirements.dev.lock.txt").read_text().splitlines()
            if line and not line.startswith("#")]
    assert len(lock) == len(inventory["python"]) + len(inventory["devkit"]["python"])
    for pinned in lock:
        assert f"\n{pinned}  " in notices, pinned
    rpms = [line for line in (ctx["tree"] / "images/devkit-packages.txt").read_text().splitlines()
            if line and not line.startswith("#")]
    for nevra in rpms:
        assert f"nevra: {nevra}  RPM License tag: " in notices, nevra
    for _, pin in li.KIT_TOOL_PINS:
        assert f"pin: scripts/tools/{pin} " in notices, pin
    assert "image:ubi9-python-314-minimal" in notices and "project:mainframe-rag" in notices
    # The texts: the oras licence from the kit, the project LICENSE and the base RPM list.
    assert f"### kit-tool:oras-windows: oras-LICENSE (kit member)\n{ORAS_LICENSE.decode().rstrip()}" in notices
    task_license = task_members()["task-LICENSE"].decode().rstrip()
    assert f"### kit-tool:task: task-LICENSE (kit member)\n{task_license}" in notices
    assert "### project:mainframe-rag: LICENSE" in notices
    assert "### image:ubi9-python-314-minimal: licenses/ubi9-python-314-minimal.rpms.txt" in notices


def _drop(group: str, key: str, value: str):
    def mutate(inventory):
        records = inventory["devkit"][group]
        records.remove(next(e for e in records if e[key] == value))
    return mutate


@pytest.mark.parametrize("mutate, message", [
    (_drop("components", "id", "rpm:git-core"),
     "devkit: rpm git-core-0:2.52.0-1.el9.x86_64: kit package has no license record"),
    (_drop("python", "name", "ruff"), "devkit: python:ruff: locked dev package has no kit license record"),
    (_drop("components", "id", "kit-tool:oc"), "devkit: tool scripts/tools/oc-pin.txt: kit tool has no license record"),
])
def test_pack_refuses_a_kit_component_missing_from_the_inventory(pack_tree, mutate, message):
    """Even with a current kit approval, a component the kit ships without a record refuses."""
    inventory = _inventory(pack_tree["tree"])
    mutate(inventory)
    _commit_inventory(pack_tree, inventory)
    done = run_pack(pack_tree)
    assert done.returncode != 0
    assert message in done.stderr
    assert "the dev kit licence check failed" in done.stderr
    assert not list((pack_tree["tree"] / "dist").glob("mfr-devkit-*"))
    assert not (pack_tree["tmp"] / "skopeo.log").exists()


def test_pack_refuses_a_stale_kit_approval(pack_tree):
    """A record changed after the owner approved the kit digest makes the approval stale."""
    inventory = _inventory(pack_tree["tree"])
    openssl = next(e for e in inventory["devkit"]["components"] if e["id"] == "rpm:openssl")
    # A re-derived RPM header now declares more than the approved record did.
    openssl.update(license_declared="Apache-2.0 AND MIT", license_spdx="Apache-2.0 AND MIT")
    _commit_inventory(pack_tree, inventory, approve=False)
    done = run_pack(pack_tree)
    assert done.returncode != 0
    assert "devkit approval: kit dependency or license records changed since the approved digest" in done.stderr
    assert "kit_approval=stale" in done.stderr
    assert not list((pack_tree["tree"] / "dist").glob("mfr-devkit-*"))
    # Re-approving the new digest is the fix the message names; the next pack passes.
    _commit_inventory(pack_tree, inventory)
    assert run_pack(pack_tree).returncode == 0


@pytest.mark.parametrize("status, message", [
    ("pending-owner-decision", "devkit: rpm:git-core: GPL-2.0-only (strong-copyleft) awaits owner decision"),
    # Exclusion is no way out: the image installs the exact RPM list either way.
    ("excluded", "devkit: rpm:git-core: excluded, but the kit still ships it"),
])
def test_pack_refuses_an_unresolved_kit_decision(pack_tree, status, message):
    """A withdrawn decision (status only, so the approved digest stays current) refuses
    an ordinary pack with an ephemeral key: enforcement does not depend on the key."""
    inventory = _inventory(pack_tree["tree"])
    git = next(e for e in inventory["devkit"]["components"] if e["id"] == "rpm:git-core")
    git.update(status=status, approval_ref=None)
    _commit_inventory(pack_tree, inventory, approve=False)
    done = run_pack(pack_tree)
    assert done.returncode != 0
    assert message in done.stderr
    assert "kit_approval=current" in done.stderr
    assert not list((pack_tree["tree"] / "dist").glob("mfr-devkit-*"))


def _open_decision(ctx) -> None:
    """An open RPM decision and no kit approval, committed."""
    inventory = _inventory(ctx["tree"])
    libedit = next(e for e in inventory["devkit"]["components"] if e["id"] == "rpm:libedit")
    libedit.update(status="pending-owner-decision", approval_ref=None)
    inventory["devkit"]["approval"] = {"approval_ref": None, "approved_digest": None}
    _commit_inventory(ctx, inventory, approve=False)


@pytest.mark.parametrize("args, env, message", [
    ((), {}, "devkit: rpm:libedit: NOASSERTION (unknown) awaits owner decision"),
    ((), {"CI_REHEARSAL": "1"}, "devkit approval: no owner approval is recorded for the kit inventory"),
    (("--license-rehearsal",), {}, "--license-rehearsal needs CI_REHEARSAL=1"),
    (("--license-rehearsal",), {"CI_REHEARSAL": "1", "SNEAKERNET_KEY_TRUSTED": "true"},
     "--license-rehearsal cannot be combined with SNEAKERNET_KEY_TRUSTED=true"),
])
def test_pack_enforces_open_decisions_unless_an_explicit_ci_rehearsal(pack_tree, args, env, message):
    _open_decision(pack_tree)
    done = run_pack(pack_tree, *args, **env)
    assert done.returncode != 0
    assert message in done.stderr
    assert not list((pack_tree["tree"] / "dist").glob("mfr-devkit-*"))


def test_licence_rehearsal_kit_records_open_items_and_installs_only_as_a_rehearsal(pack_tree):
    _open_decision(pack_tree)
    done = run_pack(pack_tree, "--license-rehearsal", CI_REHEARSAL="1")
    assert done.returncode == 0, done.stderr
    assert "WARNING: licence rehearsal kit" in done.stderr
    kit = kit_dir(pack_tree)
    fields = _manifest(kit)
    assert fields["license_gate"] == "rehearsal" and fields["signed"] == "ephemeral"
    assert "kit_unresolved=1 kit_approval=absent kit_gate=rehearsal" in fields["license_status"]
    notices = (kit / "THIRD-PARTY-NOTICES.txt").read_text()
    assert "Owner approval of the kit inventory: absent" in notices
    assert "Components awaiting owner decision: 1\n  - rpm:libedit: NOASSERTION (unknown)" in notices
    # The gap installer refuses it before any push; only an explicit CI rehearsal accepts it.
    refused = run_install(pack_tree, kit)
    assert refused.returncode != 0
    assert "the kit MANIFEST has no license_gate: enforced" in refused.stderr
    assert not (pack_tree["tmp"] / "registry").exists()
    accepted = run_install(pack_tree, kit, "--rehearsal")
    assert accepted.returncode == 0, accepted.stderr
    assert "WARNING: licence rehearsal kit accepted for a CI rehearsal" in accepted.stderr


def test_pack_refuses_a_pending_digest_and_writes_no_kit(pack_tree):
    _set_pin(pack_tree["tree"], "sha256:PENDING")
    _git(pack_tree["tree"], "commit", "-q", "-am", "pending")
    done = run_pack(pack_tree)
    assert done.returncode != 0
    assert "records digest: sha256:PENDING, which is no longer a committed state: record key:" in done.stderr
    assert "in one pin PR, after the change merges" in done.stderr
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
    assert _manifest(kit)["license_gate"] == "enforced" and "licence rehearsal" not in done.stderr
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


def test_the_runbook_anchor_bootstrap_is_the_adr_text_byte_for_byte():
    """The kit carries a convenience copy; ADR-0009 §8 holds the published one."""
    published = fenced_after("<!-- devkit-anchor-bootstrap -->", REPO / "docs/adr/0009-gap-fork.md")
    in_kit = fenced_after("<!-- devkit-runbook: step 1 -->", REPO / "docs/runbooks/devkit-windows.md")
    assert in_kit.encode("utf-8") == published.encode("utf-8")
    assert published.count("\n") == 3


def test_pack_refuses_a_runbook_whose_bootstrap_drifted_from_the_adr(pack_tree):
    runbook = pack_tree["tree"] / "docs/runbooks/devkit-windows.md"
    runbook.write_text(runbook.read_text().replace("if($e){\"FAIL:", "if($false){\"FAIL:", 1))
    _git(pack_tree["tree"], "commit", "-q", "-am", "drift")
    done = run_pack(pack_tree)
    assert done.returncode != 0
    assert "step 1 differs from the anchor bootstrap in ADR-0009" in done.stderr
    assert not list((pack_tree["tree"] / "dist").glob("mfr-devkit-*"))


# ---- scripts/devkit/link-tools.sh: a checkout in the kit image gets its tools (F5) ----
#
# The helper runs from a copy of the repository's scripts against fake pins whose
# binary digests are those of small stand-in executables in a fake image
# /usr/local/bin, so every check is real but nothing needs the actual tools.

FAKE_TASK = b'#!/bin/sh\ncase " $* " in *" --version "*) echo 3.53.1;; *) echo "fake-task $*";; esac\n'
FAKE_HELM = b'#!/bin/sh\necho v4.3.0+gtest\n'
FAKE_TASK_ARCHIVE = b"stand-in for task_linux_amd64.tar.gz\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def link_tree(tmp_path):
    tree = tmp_path / "checkout"
    for name in ("scripts/devkit/link-tools.sh", "scripts/tools/task-artifact.sh", "scripts/tools/run-task.sh"):
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / name, tree / name)
    (tree / "scripts/tools/task-pin.txt").write_text(
        "version: v3.53.1\nasset: task_linux_amd64.tar.gz\n"
        f"sha256: {_sha(FAKE_TASK_ARCHIVE)}\nbinary-sha256: {_sha(FAKE_TASK)}\nlicense-sha256: {'a' * 64}\n"
        "origin: https://github.com/go-task/task/releases/download/v3.53.1/task_linux_amd64.tar.gz\n")
    (tree / "scripts/tools/helm-pin.txt").write_text(
        f"version: v4.3.0\nasset: helm-v4.3.0-linux-amd64.tar.gz\nsha256: {'b' * 64}\n"
        f"binary-sha256: {_sha(FAKE_HELM)}\n")
    image = tmp_path / "image-usr-local-bin"
    image.mkdir()
    for name, data in (("task", FAKE_TASK), ("helm", FAKE_HELM)):
        (image / name).write_bytes(data)
        (image / name).chmod(0o755)
    archive = tmp_path / "runner mount" / "task archive.tar.gz"
    archive.parent.mkdir()
    archive.write_bytes(FAKE_TASK_ARCHIVE)
    return tree, image, archive


def _link(tree: Path, image: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(tree.parent)}
    env.pop("XDG_CONFIG_HOME", None)
    return subprocess.run(["sh", str(tree / "scripts/devkit/link-tools.sh"), "--from", str(image), *args],
                          cwd=tree.parent, env=env, capture_output=True, text=True, check=False)


def _state(tree: Path) -> dict[str, tuple[int, int, str]]:
    """(inode, mtime, link target or digest) of everything under .tools/, links not followed."""
    out = {}
    for path in sorted((tree / ".tools").rglob("*")):
        info = path.lstat()
        what = os.readlink(path) if path.is_symlink() else ("dir" if path.is_dir() else _sha(path.read_bytes()))
        out[str(path.relative_to(tree))] = (info.st_ino, info.st_mtime_ns, what)
    return out


def test_link_tools_gives_run_task_and_the_doctor_the_verified_image_tools(link_tree, monkeypatch):
    from scripts import agent_doctor

    tree, image, archive = link_tree
    proc = _link(tree, image, "--task-archive", str(archive))
    assert proc.returncode == 0, proc.stderr
    for name in ("task", "helm"):
        assert os.readlink(tree / ".tools/bin" / name) == str(image / name)
    assert (tree / ".tools/cache/task_linux_amd64.tar.gz").read_bytes() == FAKE_TASK_ARCHIVE
    # The consumers accept the result: run-task.sh verifies and runs the linked Task...
    env = {**os.environ, "HOME": str(tree.parent)}
    env.pop("XDG_CONFIG_HOME", None)
    ran = subprocess.run(["sh", "scripts/tools/run-task.sh", "--list"], cwd=tree, env=env,
                         capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.startswith("fake-task --offline --dir ") and ran.stdout.rstrip().endswith("--list")
    # ...and the doctor finds the pinned Task, its cached archive and the pinned Helm on PATH.
    assert agent_doctor.inspect_task(tree).status == "ready"
    monkeypatch.setenv("PATH", f"{tree / '.tools/bin'}:{os.environ['PATH']}")
    assert agent_doctor.inspect_helm(tree).status == "ready"


@pytest.mark.parametrize("damage, message", [
    (lambda image, archive: (image / "task").write_bytes(FAKE_TASK + b"#\n"),
     "the image's task does not match the binary-sha256 in scripts/tools/task-pin.txt"),
    (lambda image, archive: (image / "helm").write_bytes(FAKE_HELM + b"#\n"),
     "the image's helm does not match the binary-sha256 in scripts/tools/helm-pin.txt"),
    (lambda image, archive: (image / "helm").unlink(), "the image has no executable helm"),
    (lambda image, archive: ((image / "real-task").write_bytes(FAKE_TASK), (image / "task").unlink(),
                             (image / "task").symlink_to(image / "real-task")), "the image has no executable task"),
    (lambda image, archive: archive.write_bytes(b"another archive\n"),
     "the --task-archive file does not match the sha256 in scripts/tools/task-pin.txt"),
    (lambda image, archive: archive.unlink(), "the --task-archive file is missing"),
], ids=["task-digest", "helm-digest", "helm-absent", "task-symlink", "archive-digest", "archive-absent"])
def test_link_tools_refuses_a_tool_off_its_pin_and_changes_nothing(link_tree, damage, message):
    tree, image, archive = link_tree
    damage(image, archive)
    proc = _link(tree, image, "--task-archive", str(archive))
    assert proc.returncode == 2
    assert proc.stderr.startswith(f"link-tools: {message}")
    # Fixed text: never the paths it was given.
    assert str(image) not in proc.stderr and str(archive.parent) not in proc.stderr
    assert not (tree / ".tools").exists()


@pytest.mark.parametrize("name, data, message", [
    ("task", b"#!/bin/sh\necho 3.52.0\n", "the image's task is not the pinned version"),
    ("helm", b"#!/bin/sh\necho v4.2.0+gold\n", "the image's helm is not the pinned version"),
])
def test_link_tools_refuses_a_binary_that_is_not_the_pinned_version(link_tree, name, data, message):
    # A pin whose digest matches bytes that report another version is refused, not
    # trusted, and before anything is linked: the checkout keeps no new link.
    tree, image, _archive = link_tree
    (image / name).write_bytes(data)
    pin = tree / "scripts/tools" / f"{name}-pin.txt"
    expected = _sha(FAKE_TASK if name == "task" else FAKE_HELM)
    pin.write_text(pin.read_text().replace(f"binary-sha256: {expected}", f"binary-sha256: {_sha(data)}"))
    proc = _link(tree, image)
    assert proc.returncode == 2
    assert proc.stderr.startswith(f"link-tools: {message}")
    assert not (tree / ".tools").exists()


def test_link_tools_refuses_a_directory_in_place_of_a_tool(link_tree):
    tree, image, _archive = link_tree
    (tree / ".tools/bin/helm").mkdir(parents=True)
    proc = _link(tree, image)
    assert proc.returncode == 2
    assert proc.stderr.startswith("link-tools: .tools/bin/helm is a directory")
    assert (tree / ".tools/bin/helm").is_dir() and not (tree / ".tools/bin/helm").is_symlink()
    assert not (tree / ".tools/bin/task").exists()


@pytest.mark.parametrize("linked", [".tools", ".tools/bin", ".tools/cache"])
def test_link_tools_never_writes_through_a_shared_tools_link(link_tree, tmp_path, linked):
    # A worktree whose .tools (or a part of it) links another checkout's pinned tools:
    # writing through the link would replace that checkout's binaries.
    tree, image, archive = link_tree
    shared = tmp_path / "shared-tools"
    for name in ("bin", "cache"):
        (shared / name).mkdir(parents=True)
    (shared / "bin/task").write_bytes(b"#!/bin/sh\necho shared\n")
    before = {p.relative_to(shared): p.read_bytes() for p in shared.rglob("*") if p.is_file()}
    target = shared if linked == ".tools" else shared / linked.split("/")[1]
    if linked != ".tools":
        (tree / ".tools").mkdir()
    (tree / linked).symlink_to(target)
    proc = _link(tree, image, "--task-archive", str(archive))
    assert proc.returncode == 2
    assert proc.stderr.startswith(f"link-tools: {linked} is a symlink; link-tools writes only a checkout-owned")
    assert {p.relative_to(shared): p.read_bytes() for p in shared.rglob("*") if p.is_file()} == before
    assert sorted(p.name for p in (shared / "bin").iterdir()) == ["task"]


def test_link_tools_rerun_is_a_no_op_and_a_stale_copy_converges(link_tree):
    tree, image, archive = link_tree
    assert _link(tree, image, "--task-archive", str(archive)).returncode == 0
    first = _state(tree)
    again = _link(tree, image, "--task-archive", str(archive))
    assert again.returncode == 0, again.stderr
    assert _state(tree) == first
    assert "already links the verified image task" in again.stdout
    assert "already holds the verified archive" in again.stdout
    # A copy left by an older install (other bytes) is replaced by the verified link.
    (tree / ".tools/bin/task").unlink()
    (tree / ".tools/bin/task").write_bytes(b"#!/bin/sh\necho stale\n")
    assert _link(tree, image).returncode == 0
    assert os.readlink(tree / ".tools/bin/task") == str(image / "task")
    assert {k: v for k, v in _state(tree).items() if k != ".tools/bin/task" and k != ".tools/bin"} == \
        {k: v for k, v in first.items() if k != ".tools/bin/task" and k != ".tools/bin"}
