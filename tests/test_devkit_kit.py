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
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from scripts import license_inventory as li
from scripts.devkit import agents_vm as vm

from tests import helpers_devkit_commands as kitcmd
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
        assert f"pin: {li.kit_pin_path(pin)} " in notices, pin
    # The agent tools' licence texts (#1172) are the committed, pinned ones.
    for tool in ("herdr-core", "opencode"):
        text = (REPO / f"images/devkit/agents/{tool}-LICENSE").read_text().rstrip()
        assert f"### kit-tool:{tool}: images/devkit/agents/{tool}-LICENSE\n{text}" in notices
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


# --- agent tools in the kit image (#1172) -------------------------------------------
# herdr-core and OpenCode are downloaded by the connected-side image build and verified
# by images/devkit/agents/install_agent_tools.py; the hardened OpenCode config and the
# mfr-opencode wrapper carry the trial's five required settings (#1172
# issuecomment-6548130709). The expected digests below are the issue's, typed
# independently of the pin files.

AGENTS = REPO / "images/devkit/agents"
INSTALLER = AGENTS / "install_agent_tools.py"
ISSUE_PINS = {
    "herdr-core-pin.txt": {
        "version": "0.1.15",
        "sha256": "efbb1382da3d5053cb20c60c166a62ec34e73d8299977fb1f5abed28ddec8d66",
        "binary-sha256": "b66176f871f8f1e0f34962a5d3d0dcb54f5a19443f601e51a38f167f829d4981",
        "license": "Apache-2.0",
    },
    "opencode-pin.txt": {
        "version": "2.2.5",
        "sha256": "09f44302c10fd036912f59c81fd1aef5df45dd06f649197f9dfc9bbe2a79614e",
        "binary-sha256": "8d060e1d2fbb9bb44cf09ae6ed1f3f6a7cff070b5227e2f90970132b23a92050",
        "license": "MIT",
    },
}


def test_agent_tool_pins_are_the_issue_digests_and_their_licences_are_pinned():
    for name, expected in ISSUE_PINS.items():
        pin = li.read_pin(REPO, f"images/devkit/agents/{name}")
        assert {k: pin[k] for k in expected} == expected, name
        assert pin["origin"].startswith("https://") and pin["origin"].endswith("/" + pin["asset"])
        tool = name.removesuffix("-pin.txt")
        licence = (AGENTS / f"{tool}-LICENSE").read_bytes()
        assert hashlib.sha256(licence).hexdigest() == pin["license-sha256"]
    # OpenCode's archive holds no licence: the tag's LICENSE is the source, by commit.
    assert "9bbb8ce18c80f099e0e448775f59d965ead40a21/LICENSE" in li.read_pin(
        REPO, "images/devkit/agents/opencode-pin.txt")["license-source"]
    assert (AGENTS / "opencode-LICENSE").read_text().startswith("MIT License\n")
    assert "Apache License" in (AGENTS / "herdr-core-LICENSE").read_text()


def _fake_tool_archive(path: Path, members: dict[str, bytes], *, link: str | None = None) -> None:
    with tarfile.open(path, "w:gz") as handle:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            handle.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link)
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            handle.addfile(info)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def agent_archives(tmp_path):
    """Generated stand-ins for both release archives and pins naming their digests."""
    pins, archives = tmp_path / "pins", tmp_path / "archives"
    pins.mkdir()
    archives.mkdir()
    herdr = {"herdr-core": b"#!/bin/sh\necho 'herdr-core 0.1.15'\n", "LICENSE": b"Apache License (test copy)\n",
             "THIRD-PARTY-NOTICES": b"notices (test copy)\n"}
    opencode = {"opencode": b"#!/bin/sh\necho v2.2.5 >&2\n"}  # a "v" prefix, on stderr
    oc_licence = b"MIT License (test copy)\n"
    _fake_tool_archive(archives / "herdr-core-linux-x86_64.tar.gz", herdr)
    _fake_tool_archive(archives / "opencode-linux-x64.tar.gz", opencode)
    (pins / "opencode-LICENSE").write_bytes(oc_licence)
    (pins / "herdr-core-LICENSE").write_bytes(herdr["LICENSE"])
    (pins / "herdr-core-pin.txt").write_text(
        "# test pin\nversion: 0.1.15\nasset: herdr-core-linux-x86_64.tar.gz\n"
        f"sha256: {_sha((archives / 'herdr-core-linux-x86_64.tar.gz').read_bytes())}\n"
        f"binary: herdr-core\nbinary-sha256: {_sha(herdr['herdr-core'])}\n"
        "origin: https://releases.example/core/0.1.15/herdr-core-linux-x86_64.tar.gz\nlicense: Apache-2.0\n"
        f"license-member: LICENSE\nlicense-sha256: {_sha(herdr['LICENSE'])}\n"
        f"notices-member: THIRD-PARTY-NOTICES\nnotices-sha256: {_sha(herdr['THIRD-PARTY-NOTICES'])}\n")
    (pins / "opencode-pin.txt").write_text(
        "version: 2.2.5\nasset: opencode-linux-x64.tar.gz\n"
        f"sha256: {_sha((archives / 'opencode-linux-x64.tar.gz').read_bytes())}\n"
        f"binary: opencode\nbinary-sha256: {_sha(opencode['opencode'])}\n"
        "origin: https://github.example/releases/download/v2.2.5/opencode-linux-x64.tar.gz\nlicense: MIT\n"
        f"license-sha256: {_sha(oc_licence)}\n")
    return {"pins": pins, "archives": archives, "out": tmp_path / "out", "herdr": herdr, "opencode": opencode,
            "oc_licence": oc_licence}


def _install_tools(ctx):
    return subprocess.run([sys.executable, "-I", str(INSTALLER), "--pins", str(ctx["pins"]), "--archives",
                           str(ctx["archives"]), "--bin-dir", str(ctx["out"] / "bin"),
                           "--license-dir", str(ctx["out"] / "licenses")],
                          capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)


def test_agent_tools_install_only_verified_bytes_with_the_hc_link(agent_archives):
    ctx = agent_archives
    done = _install_tools(ctx)
    assert done.returncode == 0, done.stderr
    out = ctx["out"]
    assert (out / "bin/herdr-core").read_bytes() == ctx["herdr"]["herdr-core"]
    assert os.readlink(out / "bin/hc") == "herdr-core"
    assert (out / "bin/opencode").read_bytes() == ctx["opencode"]["opencode"]
    assert os.access(out / "bin/opencode", os.X_OK)
    assert (out / "licenses/herdr-core/LICENSE").read_bytes() == ctx["herdr"]["LICENSE"]
    assert (out / "licenses/herdr-core/THIRD-PARTY-NOTICES").read_bytes() == ctx["herdr"]["THIRD-PARTY-NOTICES"]
    assert (out / "licenses/opencode/LICENSE").read_bytes() == ctx["oc_licence"]
    assert sorted(p.name for p in (out / "bin").iterdir()) == ["hc", "herdr-core", "opencode"]


def _swap_archive(ctx, asset: str, members: dict[str, bytes], **kw) -> None:
    _fake_tool_archive(ctx["archives"] / asset, members, **kw)


def _repin(ctx, name: str, field: str, value: str) -> None:
    path = ctx["pins"] / name
    lines = [f"{field}: {value}" if line.startswith(f"{field}:") else line for line in path.read_text().splitlines()]
    path.write_text("\n".join(lines) + "\n")


def _refit_archive(ctx, asset: str, pin: str, members: dict[str, bytes], **kw) -> None:
    """Replace an archive and re-pin its archive digest only, so a later check must refuse."""
    _swap_archive(ctx, asset, members, **kw)
    _repin(ctx, pin, "sha256", _sha((ctx["archives"] / asset).read_bytes()))


HERDR_ASSET, OC_ASSET = "herdr-core-linux-x86_64.tar.gz", "opencode-linux-x64.tar.gz"


@pytest.mark.parametrize("damage, message", [
    # The archive itself is not the pinned one (a re-uploaded release, a mirror).
    (lambda c: _swap_archive(c, OC_ASSET, {"opencode": b"#!/bin/sh\necho 2.2.5 tampered\n"}),
     "opencode-linux-x64.tar.gz does not match the sha256 in opencode-pin.txt"),
    # A pinned archive whose binary differs from the binary pin.
    (lambda c: _refit_archive(c, OC_ASSET, "opencode-pin.txt", {"opencode": b"#!/bin/sh\necho 2.2.5 # other\n"}),
     "opencode does not match the binary-sha256 in opencode-pin.txt"),
    # herdr's archive licence or notices differ from their pins.
    (lambda c: _refit_archive(c, HERDR_ASSET, "herdr-core-pin.txt", {**c["herdr"], "LICENSE": b"changed\n"}),
     "the herdr-core licence does not match the license-sha256 in herdr-core-pin.txt"),
    (lambda c: _refit_archive(c, HERDR_ASSET, "herdr-core-pin.txt",
                              {**c["herdr"], "THIRD-PARTY-NOTICES": b"changed\n"}),
     "the herdr-core notices do not match the notices-sha256 in herdr-core-pin.txt"),
    # OpenCode's committed licence text drifted from its pinned digest.
    (lambda c: (c["pins"] / "opencode-LICENSE").write_bytes(b"MIT License (edited)\n"),
     "the opencode licence does not match the license-sha256 in opencode-pin.txt"),
    # A member that is a link, or missing, is never followed or invented.
    (lambda c: _refit_archive(c, OC_ASSET, "opencode-pin.txt", {}, link="opencode"),
     "opencode-linux-x64.tar.gz holds opencode more than once or not as a regular file"),
    (lambda c: _refit_archive(c, OC_ASSET, "opencode-pin.txt", {"other": b"x"}),
     "opencode-linux-x64.tar.gz has no member opencode"),
    # The verified binary does not report the pinned version.
    (lambda c: (_refit_archive(c, OC_ASSET, "opencode-pin.txt", {"opencode": b"#!/bin/sh\necho 2.2.50\n"}),
                _repin(c, "opencode-pin.txt", "binary-sha256", _sha(b"#!/bin/sh\necho 2.2.50\n"))),
     "opencode --version does not name the pinned version 2.2.5 (rc 0)"),
    # A pin whose origin is not https, or does not name its asset, is refused unread.
    (lambda c: _repin(c, "herdr-core-pin.txt", "origin", "http://releases.example/herdr-core-linux-x86_64.tar.gz"),
     "herdr-core-pin.txt origin is not an https URL that names its asset"),
    (lambda c: _repin(c, "opencode-pin.txt", "sha256", "PENDING"),
     "opencode-pin.txt sha256 is not 64 lowercase hex"),
])
def test_agent_tools_refuse_any_digest_mismatch_and_install_nothing(agent_archives, damage, message):
    damage(agent_archives)
    done = _install_tools(agent_archives)
    assert done.returncode == 1
    assert done.stderr == f"agent-tools: {message}; nothing was installed\n"
    assert not (agent_archives["out"] / "bin").exists() and not (agent_archives["out"] / "licenses").exists()


def test_the_image_recipe_installs_the_agent_tools_through_the_verifier():
    recipe = (REPO / "images/Containerfile.devkit").read_text()
    stage = recipe.split("FROM rpms AS agent-tools", 1)[1].split("\nFROM ", 1)[0]
    assert "install_agent_tools.py --pins /opt/devkit-build/agents" in stage
    assert "source=images/devkit/agents" in stage
    # Nothing fetched by a shell tool and nothing piped to a shell: Python verifies, then installs.
    assert not re.search(r"\b(curl|wget)\b|\|\s*(sh|bash)\b", stage)
    assert "COPY --from=agent-tools /out/bin/ /usr/local/bin/" in recipe
    assert "COPY --from=agent-tools /out/licenses/ /usr/share/licenses/" in recipe
    assert "COPY --chmod=0644 images/devkit/agents/opencode.json /etc/opencode/opencode.json" in recipe
    # Setting 1 holds for any invocation in the image, not only through the wrapper.
    assert "OPENCODE_DISABLE_MODELS_FETCH=1" in re.search(r"(?m)^ENV OPENCODE_.*$", recipe)[0]


def test_hardened_opencode_config_carries_the_required_settings_and_no_secret():
    raw = (AGENTS / "opencode.json").read_text()
    config = json.loads(raw)
    assert config["autoupdate"] is False and config["share"] == "disabled"
    assert config["enabled_providers"] == ["site-litellm"] and "$schema" not in config
    provider = config["provider"]["site-litellm"]
    # Setting 3: explicit timeouts, 30 s each (a silently dropped LiteLLM path hung > 300 s without).
    assert {k: provider["options"][k] for k in ("timeout", "headerTimeout", "chunkTimeout")} == {
        "timeout": 30000, "headerTimeout": 30000, "chunkTimeout": 30000}
    # The site LiteLLM with the separate dev key, read from a mounted secret; the model from site config.
    assert provider["options"]["baseURL"] == "{env:MFR_LLM_BASE_URL}"
    assert provider["options"]["apiKey"] == "{file:/run/secrets/mfr-dev-litellm}"
    assert config["model"] == "site-litellm/{env:MFR_LLM_MODEL}"
    assert list(provider["models"]) == ["{env:MFR_LLM_MODEL}"]
    # Setting 4: an explicit shell policy (v2 renamed bash to shell; both are set alike).
    shell = config["permission"]["shell"]
    assert config["permission"]["bash"] == shell
    rules = list(shell.items())
    assert rules[0] == ("*", "ask")  # last match wins: anything unlisted asks, and a headless run rejects it
    for denied in ("git push*", "curl*", "wget*", "ssh*", "oc *", "kubectl*", "podman*", "pip *"):
        assert shell[denied] == "deny", denied
        assert all(action != "allow" for _, action in rules[rules.index((denied, "deny")) + 1:])
    assert config["permission"]["webfetch"] == "deny" and config["permission"]["websearch"] == "deny"
    assert not re.search(r"sk-[A-Za-z0-9]|Bearer|https?://(?!opencode)", raw)


def _run_wrapper(tmp_path, *args, **env):
    fake = tmp_path / "opencode"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "fetch=$OPENCODE_DISABLE_MODELS_FETCH" "$@"\n')
    fake.chmod(0o755)
    full = {"PATH": os.environ["PATH"], "MFR_OPENCODE_BIN": str(fake), **env}
    return subprocess.run(["sh", str(REPO / "images/devkit/bin/mfr-opencode"), *args], capture_output=True,
                          text=True, env=full, check=False, stdin=subprocess.DEVNULL)


def test_wrapper_runs_every_worker_standalone_with_the_catalogue_fetch_off(tmp_path):
    # Setting 2 (--standalone) and setting 1 (the env switch), even against the caller's env.
    done = _run_wrapper(tmp_path, "run", "Write RESULT.txt", OPENCODE_DISABLE_MODELS_FETCH="0")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["fetch=1", "run", "--standalone", "Write RESULT.txt"]
    tui = _run_wrapper(tmp_path, "/work/repo")
    assert tui.stdout.splitlines() == ["fetch=1", "--standalone", "/work/repo"]


@pytest.mark.parametrize("args", [("run", "--auto", "x"), ("--yolo",), ("run", "--dangerously-skip-permissions"),
                                  ("serve",), ("service", "start"), ("upgrade",)])
def test_wrapper_refuses_permission_skips_and_the_shared_service(tmp_path, args):
    done = _run_wrapper(tmp_path, *args)
    assert done.returncode == 2 and done.stdout == ""
    assert done.stderr.startswith("mfr-opencode: ")


@pytest.fixture
def workers_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "T"), ("commit", "-q", "--allow-empty", "-m", "seed")):
        _git(repo, *args)
    log = tmp_path / "hc.log"
    hc = tmp_path / "hc"
    hc.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$HC_LOG"\n'
        'case "$1" in\n'
        '  wait) if [ "$2" = w-bad ]; then code=3; else code=0; fi\n'
        '        printf \'{"ended":true,"reason":"exit","exit_code":%s}\\n\' "$code";;\n'
        '  read) echo "DONE $2";;\n'
        "esac\n")
    hc.chmod(0o755)
    worker = tmp_path / "mfr-opencode"  # the warm-up turn runs the real wrapper's name
    worker.write_text('#!/bin/sh\nprintf "%s\\n" "mfr-opencode $*" >> "$HC_LOG"\n'
                      'mkdir -p "$HOME/.local/share/opencode" && : > "$HOME/.local/share/opencode/opencode.db"\n'
                      'exit "${WARM_RC:-0}"\n')
    worker.chmod(0o755)
    tasks = tmp_path / "tasks.txt"
    tasks.write_text("# id<TAB>task\none\tWrite RESULT.txt\nbad\tFail on purpose\n")
    home = tmp_path / "home"  # a fresh HOME: no OpenCode state database yet
    home.mkdir()
    env = {"PATH": f"{tmp_path}:{os.environ['PATH']}", "MFR_HC_BIN": str(hc), "HC_LOG": str(log), "HOME": str(home)}
    return {"repo": repo, "tasks": tasks, "log": log, "env": env, "tmp": tmp_path}


def _workers(ctx, *args):
    return subprocess.run([sys.executable, str(REPO / "images/devkit/bin/mfr-agent-workers"), *args],
                          capture_output=True, text=True, env=ctx["env"], cwd=ctx["tmp"], check=False,
                          stdin=subprocess.DEVNULL)


def test_workers_get_their_own_worktrees_and_run_mfr_opencode_under_hc(workers_repo):
    ctx = workers_repo
    done = _workers(ctx, "start", "--repo", str(ctx["repo"]), "--tasks", str(ctx["tasks"]))
    assert done.returncode == 0, done.stdout + done.stderr
    worktrees = ctx["tmp"] / "agent-worktrees"
    calls = ctx["log"].read_text().splitlines()
    # One serial warm-up turn on the fresh HOME before any worker starts (Podman trial).
    assert calls[0] == "mfr-opencode run Reply with the single word OK. Do not use any tool."
    assert calls[1:] == [f"run --bg --name w-{i} --cwd {worktrees / f'w-{i}'} --no-parent -- mfr-opencode run {task}"
                     for i, task in (("one", "Write RESULT.txt"), ("bad", "Fail on purpose"))]
    branches = _git(ctx["repo"], "worktree", "list", "--porcelain")
    assert "branch refs/heads/agents/w-one" in branches and "branch refs/heads/agents/w-bad" in branches
    collected = _workers(ctx, "collect", "--tasks", str(ctx["tasks"]), "--logs", str(ctx["tmp"] / "logs"))
    assert collected.returncode == 1
    assert "worker=w-one ended=True reason=exit exit_code=0 ok=yes" in collected.stdout
    assert "worker=w-bad ended=True reason=exit exit_code=3 ok=no" in collected.stdout
    assert collected.stdout.splitlines()[-1] == "workers=2 failures=1"
    assert (ctx["tmp"] / "logs/w-one.log").read_text() == "DONE w-one\n"
    # The state database now exists: a second batch starts without another warm-up.
    ctx["log"].unlink()
    ctx["tasks"].write_text("two\tAnother task\n")
    assert _workers(ctx, "start", "--repo", str(ctx["repo"]), "--tasks", str(ctx["tasks"])).returncode == 0
    assert [c.split(" --cwd")[0] for c in ctx["log"].read_text().splitlines()] == ["run --bg --name w-two"]


def test_a_failed_warm_up_starts_no_worker(workers_repo):
    ctx = workers_repo
    ctx["env"]["WARM_RC"] = "1"
    done = _workers(ctx, "start", "--repo", str(ctx["repo"]), "--tasks", str(ctx["tasks"]))
    assert done.returncode == 2
    assert done.stderr == "mfr-agent-workers: the warm-up turn failed (rc 1); no worker was started\n"
    assert [c for c in ctx["log"].read_text().splitlines() if c.startswith("run ")] == []


@pytest.mark.parametrize("tasks", ["one Write it\n", "One\tx\n", "a\tx\na\ty\n", "# nothing\n"])
def test_workers_refuse_a_malformed_tasks_file_before_any_start(workers_repo, tasks):
    ctx = workers_repo
    ctx["tasks"].write_text(tasks)
    done = _workers(ctx, "start", "--repo", str(ctx["repo"]), "--tasks", str(ctx["tasks"]))
    assert done.returncode == 2 and done.stderr.startswith("mfr-agent-workers: ")
    assert not ctx["log"].exists()


# --- gap orchestration VM: egress fence, proof, container (#1172) --------------------

# Documentation-only addresses (RFC 5737): never a site's.
SITE_ENV = """\
# test site inputs
MFR_VM_AGENTS_USER=agents
MFR_VM_DNS=192.0.2.53
MFR_VM_GITLAB=192.0.2.10
MFR_VM_GITLAB_PORTS=443 22
MFR_VM_GITLAB_HOST=gitlab.site.example
MFR_VM_LITELLM="198.51.100.0/28"
MFR_VM_LITELLM_PORTS=443
MFR_VM_LITELLM_BASE_URL=https://litellm.site.example/v1
MFR_VM_LLM_MODEL=site-reasoning
MFR_VM_BLOCKED=qdrant=203.0.113.6:6333 agent=203.0.113.7:8000 openshift-api=203.0.113.8:6443 internet=203.0.113.9:443 host-loopback=127.0.0.1:22 host-address=192.0.2.99:22
MFR_VM_KIT_IMAGE=localhost/qdrant-pdf-rag-devkit:testkey
MFR_VM_KIT_DIGEST=sha256:%s
MFR_VM_WORK=/home/agents/work
""" % ("ab" * 32)


def _site(tmp_path, text=SITE_ENV, **override):
    lines = [line for line in text.splitlines() if line.split("=", 1)[0] not in override]
    lines += [f"{k}={v}" for k, v in override.items()]
    path = tmp_path / "site.env"
    path.write_text("\n".join(lines) + "\n")
    return path


def _agents_account(monkeypatch):
    """This process's account name, for MFR_VM_AGENTS_USER. A kit CI job (GitLab on
    OpenShift) runs as an arbitrary uid with no passwd entry; give it one, as the VM's
    agents user has, so the agents-user paths still run there."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        entry = pwd.struct_passwd(("agents", "x", os.getuid(), os.getgid(), "", "/nonexistent", "/sbin/nologin"))
        real = pwd.getpwuid
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: entry if uid == os.getuid() else real(uid))
        return "agents"


def _no_passwd_entry(monkeypatch):
    """Simulate the kit's passwd-less CI uid."""
    def missing(uid):
        raise KeyError(f"getpwuid(): uid not found: {uid}")
    monkeypatch.setattr(pwd, "getpwuid", missing)


def test_fence_renders_an_skuid_allow_list_then_drop_and_the_slice_second_layer(tmp_path):
    out = tmp_path / "fence"
    done = vm.main(["render-fence", "--site-env", str(_site(tmp_path)), "--uid", "1500", "--out", str(out)])
    assert done == 0
    nft = (out / "mfr-agents.nft").read_text()
    def chain(name):
        body = nft.split(f"chain {name} {{", 1)[1].split("\n    }", 1)[0]
        return [line.strip() for line in body.splitlines() if line.strip()]

    # Only a positive uid match enters the fence (#1181 B1): a packet with no socket
    # owner (IPv6 neighbour discovery, kernel NFS/iSCSI/WireGuard sockets) cannot match
    # `meta skuid`, so it must fall to the base chain's accept, never to the drop.
    assert chain("output") == [
        "type filter hook output priority filter; policy accept;",
        "meta skuid 1500 jump agents_egress",
    ]
    assert chain("agents_egress") == [
        "ct state established,related accept",
        "ip daddr @dns4 udp dport { 53 } accept",
        "ip daddr @dns4 tcp dport { 53 } accept",
        "ip daddr @gitlab4 tcp dport { 22, 443 } accept",
        "ip daddr @litellm4 tcp dport { 443 } accept",
        'counter drop comment "mfr-agents egress fence: not GitLab, LiteLLM or DNS"',
    ]
    assert "skuid !=" not in nft and nft.count("meta skuid") == 1
    assert "elements = { 192.0.2.53/32 }" in nft and "elements = { 198.51.100.0/28 }" in nft
    # The negative controls are never allowed, nor is loopback: pasta reaches host-local
    # services as the agents uid (Podman trial), so no blanket `oifname "lo" accept`.
    assert "203.0.113." not in nft and "127.0.0.1" not in nft and '"lo"' not in nft
    assert nft.startswith("# Egress fence") and "table inet mfr_agents\ndelete table inet mfr_agents\n" in nft
    dropin = (out / "user-1500.slice.d/50-mfr-agents-egress.conf").read_text().splitlines()
    # The slice layer is cgroup eBPF, so it filters every network namespace of the slice:
    # without localhost it would cut the container's own loopback, over which OpenCode's
    # --standalone client reaches its child server (#1181 B2). The host's loopback stays
    # shut by the nftables layer above, which has no loopback accept.
    assert dropin[dropin.index("[Slice]"):] == [
        "[Slice]", "IPAddressDeny=any", "IPAddressAllow=localhost",
        "IPAddressAllow=192.0.2.53/32 192.0.2.10/32 198.51.100.0/28"]


@pytest.mark.parametrize("override, message", [
    ({"MFR_VM_GITLAB": "gitlab.site.example"}, "MFR_VM_GITLAB holds a value that is not an IP address or CIDR"),
    ({"MFR_VM_LITELLM": "0.0.0.0/0"}, "MFR_VM_LITELLM holds a range broader than /16"),
    ({"MFR_VM_BLOCKED": "qdrant=198.51.100.3:6333 agent=203.0.113.7:8000 openshift-api=203.0.113.8:6443 "
                        "internet=203.0.113.9:443 host-loopback=127.0.0.1:22 host-address=192.0.2.99:22"},
     "blocked target qdrant lies inside an allowed address: its negative control could never fail"),
    ({"MFR_VM_BLOCKED": "qdrant=203.0.113.6:6333 agent=203.0.113.7:8000"},
     "MFR_VM_BLOCKED has no negative control for openshift-api, internet, host-loopback, host-address"),
    ({"MFR_VM_BLOCKED": "qdrant=203.0.113.6:6333 agent=203.0.113.7:8000 openshift-api=203.0.113.8:6443 "
                        "internet=203.0.113.9:443 host-loopback=192.0.2.98:22 host-address=192.0.2.99:22"},
     "the host-loopback negative control must be a loopback address"),
    ({"MFR_VM_VOLUME_LABEL": "U"}, "MFR_VM_VOLUME_LABEL is Z (SELinux private, the default), z (shared) or none"),
    ({"MFR_VM_AGENTS_USER": "root"}, "MFR_VM_AGENTS_USER is not a plain unprivileged user name"),
    ({"MFR_VM_DNS": "$(id)"}, "site env line"),
    ({"MFR_VM_KIT_DIGEST": ""}, "the site env file has no value for MFR_VM_KIT_DIGEST"),
])
def test_fence_refuses_unsafe_or_unprovable_site_inputs(tmp_path, capsys, override, message):
    out = tmp_path / "fence"
    assert vm.main(["render-fence", "--site-env", str(_site(tmp_path, **override)), "--uid", "1500",
                    "--out", str(out)]) == 2
    assert capsys.readouterr().err.startswith(f"agents-vm: {message}")
    assert not out.exists()


def test_site_env_inside_a_git_work_tree_is_refused(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    (tmp_path / "private").mkdir()
    path = tmp_path / "private/site.env"
    path.write_text(SITE_ENV)
    assert vm.main(["render-fence", "--site-env", str(path), "--uid", "1500", "--out", str(tmp_path / "f")]) == 2
    assert "inside a git work tree" in capsys.readouterr().err


def _nft_listing(uid: int, *, op: str = "==", verdict: str = "jump") -> str:
    """`nft -j list table inet mfr_agents` as nftables 1.1.6 prints the loaded fence."""
    dispatch = {"jump": {"target": "agents_egress"}} if verdict == "jump" else {"accept": None}
    items = [
        {"metainfo": {"version": "1.1.6", "json_schema_version": 1}},
        {"table": {"family": "inet", "name": "mfr_agents", "handle": 5}},
        {"chain": {"family": "inet", "table": "mfr_agents", "name": "output", "handle": 1, "type": "filter",
                   "hook": "output", "prio": 0, "policy": "accept"}},
        {"chain": {"family": "inet", "table": "mfr_agents", "name": "agents_egress", "handle": 2}},
        {"rule": {"family": "inet", "table": "mfr_agents", "chain": "output", "handle": 4, "expr": [
            {"match": {"op": op, "left": {"meta": {"key": "skuid"}}, "right": uid}}, dispatch]}},
        {"rule": {"family": "inet", "table": "mfr_agents", "chain": "agents_egress", "handle": 7, "expr": [
            {"counter": {"packets": 0, "bytes": 0}}, {"drop": None}]}},
    ]
    return json.dumps({"nftables": items})


@pytest.fixture
def fence_host(tmp_path, monkeypatch):
    """A stateful fake nft (the table is loaded or not) and systemctl, logging each call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ctx = {"log": tmp_path / "calls.log", "loaded": tmp_path / "nft.loaded", "listing": tmp_path / "nft.json",
           "root": tmp_path / "root", "site": _site(tmp_path), "bin": bin_dir}
    (bin_dir / "nft").write_text(f"""#!/bin/sh
echo "nft $*" >> "{ctx['log']}"
case "$1" in
  -c) exit ${{NFT_RC:-0}} ;;
  -f) [ "${{NFT_RC:-0}}" = 0 ] || exit 1; touch "{ctx['loaded']}"; exit 0 ;;
  -j) [ -f "{ctx['loaded']}" ] || exit 1; cat "{ctx['listing']}"; exit 0 ;;
  list) [ -f "{ctx['loaded']}" ] ;;
  delete) [ -f "{ctx['loaded']}" ] || exit 1; rm "{ctx['loaded']}" ;;
  *) exit 64 ;;
esac
""")
    (bin_dir / "systemctl").write_text(f'#!/bin/sh\necho "systemctl $*" >> "{ctx["log"]}"\n')
    for tool in ("nft", "systemctl"):
        (bin_dir / tool).chmod(0o755)
    ctx["listing"].write_text(_nft_listing(1500))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return ctx


def _fence(ctx, *extra: str) -> int:
    return vm.main(["install-fence", "--site-env", str(ctx["site"]), "--uid", "1500", "--root", str(ctx["root"]),
                    *extra])


def _calls(ctx) -> list[str]:
    lines = ctx["log"].read_text().splitlines() if ctx["log"].exists() else []
    ctx["log"].unlink(missing_ok=True)
    return lines


def test_install_fence_checks_before_loading_and_removes_for_the_negative_control(fence_host, monkeypatch):
    ctx = fence_host
    root = ctx["root"]
    nft_file = root / "etc/nftables/mfr-agents.nft"
    dropin = root / "etc/systemd/system/user-1500.slice.d/50-mfr-agents-egress.conf"
    assert _fence(ctx) == 0
    staged = root / "etc/nftables/.mfr-agents.nft.new"
    # Check, load, then read the live table back before anything else is installed.
    assert _calls(ctx) == [f"nft -c -f {staged}", f"nft -f {staged}", "nft -j list table inet mfr_agents",
                           "systemctl daemon-reload"]
    assert nft_file.read_text().count("meta skuid 1500 jump agents_egress") == 1
    assert dropin.is_file() and not staged.exists()
    installed = (nft_file.read_bytes(), dropin.read_bytes())
    # Installing again converges on the same files and the same live table.
    assert _fence(ctx) == 0 and (nft_file.read_bytes(), dropin.read_bytes()) == installed
    assert ctx["loaded"].exists() and not staged.exists()
    _calls(ctx)
    # A table nft rejects changes nothing.
    monkeypatch.setenv("NFT_RC", "1")
    nft_file.write_text("previous\n")
    assert _fence(ctx) == 2
    assert nft_file.read_text() == "previous\n" and not staged.exists()
    monkeypatch.delenv("NFT_RC")
    _calls(ctx)
    assert _fence(ctx, "--remove") == 0
    assert _calls(ctx) == ["nft delete table inet mfr_agents", "systemctl daemon-reload"]
    assert not nft_file.exists() and not dropin.exists() and not ctx["loaded"].exists()
    # Removing again converges: no table to delete is the state remove wants.
    assert _fence(ctx, "--remove") == 0
    assert _calls(ctx) == ["nft delete table inet mfr_agents", "nft list table inet mfr_agents",
                           "systemctl daemon-reload"]
    # The next ordinary operation after removal: the fence installs again.
    assert _fence(ctx) == 0 and ctx["loaded"].exists() and nft_file.read_bytes() == installed[0]


def test_install_fence_without_nft_refuses_cleanly_and_changes_nothing(fence_host, capsys):
    ctx = fence_host
    (ctx["bin"] / "nft").unlink()
    root = ctx["root"]
    assert _fence(ctx) == 2
    assert capsys.readouterr().err == "agents-vm: nft is not installed or cannot run; nothing was changed\n"
    assert not (root / "etc/nftables/.mfr-agents.nft.new").exists()
    assert not (root / "etc/nftables/mfr-agents.nft").exists()
    assert not (root / "etc/systemd/system/user-1500.slice.d/50-mfr-agents-egress.conf").exists()
    assert _calls(ctx) == []
    # --remove cannot tell whether a table is still loaded: it refuses and keeps the files.
    dropin = root / "etc/systemd/system/user-1500.slice.d/50-mfr-agents-egress.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("kept\n")
    assert _fence(ctx, "--remove") == 2
    assert capsys.readouterr().err == "agents-vm: nft is not installed or cannot run; nothing was changed\n"
    assert dropin.read_text() == "kept\n" and _calls(ctx) == []


@pytest.mark.parametrize("listing", [
    _nft_listing(1500, op="!=", verdict="accept"),  # the #1181 B1 shape: socket-less packets reach the drop
    _nft_listing(1501),                              # another uid
    "not json",
], ids=["skuid-not-equal-accept", "another-uid", "unreadable"])
def test_install_fence_refuses_a_live_table_that_does_not_dispatch_only_the_agents_uid(fence_host, capsys, listing):
    ctx = fence_host
    ctx["listing"].write_text(listing)
    assert _fence(ctx) == 2
    assert capsys.readouterr().err.startswith("agents-vm: the loaded nftables table is not the rendered fence")
    # Nothing past the failed read-back: no drop-in, no daemon-reload.
    assert not (ctx["root"] / "etc/systemd/system/user-1500.slice.d/50-mfr-agents-egress.conf").exists()
    assert _calls(ctx)[-1] == "nft -j list table inet mfr_agents"


def test_remove_refuses_while_an_agents_container_runs(fence_host, capsys):
    ctx = fence_host
    assert _fence(ctx) == 0
    _calls(ctx)
    proc = ctx["root"] / "proc"
    for pid, name, uid in (("41", "conmon", 1600), ("42", "sshd", 1500), ("43", "conmon", 1500)):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "status").write_text(f"Name:\t{name}\nState:\tS (sleeping)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    assert _fence(ctx, "--remove") == 2
    assert capsys.readouterr().err.startswith(
        "agents-vm: a Podman container of the agents user is running; stop it first")
    # The fence stays whole: the container would otherwise run unfenced.
    assert _calls(ctx) == [] and ctx["loaded"].exists()
    assert (ctx["root"] / "etc/nftables/mfr-agents.nft").exists()
    shutil.rmtree(proc / "43")
    assert _fence(ctx, "--remove") == 0 and not ctx["loaded"].exists()


def _results(allowed, blocked, ok_allowed=True, ok_blocked=False):
    out = {label: "connected" if ok_allowed else "failed:TimeoutError" for label in allowed}
    out.update({label: "connected" if ok_blocked else "failed:TimeoutError" for label in blocked})
    return out


def test_proof_verdict_needs_the_allow_list_open_and_every_negative_control_shut(tmp_path):
    site = vm.load_site(_site(tmp_path))
    spec, allowed, blocked = vm.probe_spec(site)
    assert allowed == ["gitlab1:22", "gitlab1:443", "litellm1:443", "dns:resolve"]
    assert blocked == ["blocked:agent", "blocked:host-address", "blocked:host-loopback", "blocked:internet",
                       "blocked:openshift-api", "blocked:qdrant"]
    assert spec["resolve"] == ["dns:resolve", "gitlab.site.example"]
    targets = {label: host for label, host, _ in spec["connect"]}
    assert targets["blocked:host-loopback"] == "127.0.0.1"
    # Inside a container 127.0.0.1 is the container itself: the host's loopback is
    # reached through pasta as host.containers.internal, so that is the probe there.
    inside = {label: host for label, host, _ in vm.probe_spec(site, "container")[0]["connect"]}
    assert inside["blocked:host-loopback"] == "host.containers.internal"
    assert {k: v for k, v in inside.items() if k != "blocked:host-loopback"} == {
        k: v for k, v in targets.items() if k != "blocked:host-loopback"}
    assert vm.verdict(_results(allowed, blocked), allowed, blocked, fence_off=False) == []
    leak = _results(allowed, blocked)
    leak["blocked:openshift-api"] = "connected"
    assert vm.verdict(leak, allowed, blocked, fence_off=False) == [
        "blocked:openshift-api connected with the fence on: the fence leaks"]
    tight = _results(allowed, blocked)
    tight["litellm1:443"] = "failed:ConnectionRefusedError"
    assert vm.verdict(tight, allowed, blocked, fence_off=False)[0].startswith("litellm1:443 must connect")
    # Fence off: the same negative controls must connect, or they prove nothing.
    assert vm.verdict(_results(allowed, blocked, ok_blocked=True), allowed, blocked, fence_off=True) == []
    assert vm.verdict(_results(allowed, blocked), allowed, blocked, fence_off=True)[0].startswith(
        "blocked:agent did not connect with the fence off")
    assert vm.verdict({}, allowed, blocked, fence_off=False)[0].startswith("the probe returned no result")


def _listen(address: str):
    server = socket.socket()
    server.bind((address, 0))
    server.listen(8)
    return server


def test_proof_probes_real_sockets_and_fails_on_a_reachable_negative_control(tmp_path, capsys, monkeypatch):
    """The host run, end to end on loopback: GitLab and LiteLLM listen on 127.0.0.1, the
    four negative controls are 127.0.0.2 ports. Nothing listens there, as a working fence
    would make it; then one starts listening and the same proof must fail."""
    gitlab, litellm = _listen("127.0.0.1"), _listen("127.0.0.1")
    shut = [_listen("127.0.0.2") for _ in range(6)]
    ports = [s.getsockname()[1] for s in shut]
    for s in shut:
        s.close()
    blocked_env = " ".join(f"{label}=127.0.0.2:{port}"
                           for label, port in zip(("qdrant", "agent", "openshift-api", "internet", "host-loopback",
                                                   "host-address"), ports, strict=True))
    env = _site(tmp_path, MFR_VM_AGENTS_USER=_agents_account(monkeypatch), MFR_VM_DNS="127.0.0.53",
                MFR_VM_GITLAB="127.0.0.1", MFR_VM_GITLAB_PORTS=str(gitlab.getsockname()[1]),
                MFR_VM_GITLAB_HOST="localhost", MFR_VM_LITELLM="127.0.0.1",
                MFR_VM_LITELLM_PORTS=str(litellm.getsockname()[1]), MFR_VM_BLOCKED=blocked_env)
    try:
        record = tmp_path / "proof.json"
        assert vm.main(["proof", "--site-env", str(env), "--where", "host", "--output", str(record)]) == 0
        printed = capsys.readouterr().out
        assert printed.splitlines()[-1] == "proof: host PASS (fenced)"
        assert "127.0.0" not in printed  # labels only on stdout; addresses stay in the private record
        run = json.loads(record.read_text())["runs"]["host"]
        assert run["verdict"] == "pass" and run["results"]["blocked:qdrant"].startswith("failed:")
        leak = socket.socket()
        leak.bind(("127.0.0.2", ports[2]))
        leak.listen(1)
        try:
            assert vm.main(["proof", "--site-env", str(env), "--where", "host"]) == 1
            assert "proof: host FAIL blocked:openshift-api connected with the fence on: the fence leaks" in \
                capsys.readouterr().out
        finally:
            leak.close()
    finally:
        gitlab.close()
        litellm.close()


def test_proof_refuses_to_run_as_another_user(tmp_path, capsys):
    assert vm.main(["proof", "--site-env", str(_site(tmp_path, MFR_VM_AGENTS_USER="nobody-else")),
                    "--where", "host"]) == 2
    assert "run the proof as the agents user" in capsys.readouterr().err


def test_container_proof_checks_its_own_loopback_which_opencode_workers_need(tmp_path):
    """#1181 B2: `opencode run --standalone` talks to its child server over 127.0.0.1
    inside the container, so the container proof must see its own loopback work, fenced
    or not. On the host the agents uid's loopback is a negative control instead."""
    site = vm.load_site(_site(tmp_path))
    spec, allowed, blocked = vm.probe_spec(site, "container")
    assert spec["loopback"] == "container:loopback" and "container:loopback" in allowed
    assert "container:loopback" not in blocked
    host_spec, host_allowed, _ = vm.probe_spec(site, "host")
    assert not host_spec.get("loopback") and "container:loopback" not in host_allowed
    # The probe code itself: a listener on its own 127.0.0.1, one byte each way.
    done = subprocess.run([sys.executable, "-I", "-c", vm.PROBE,
                           json.dumps({"connect": [], "resolve": None, "loopback": "container:loopback"})],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(done.stdout) == {"container:loopback": "connected"}
    # A missing result fails the verdict, fenced or not.
    results = _results(allowed, blocked)
    del results["container:loopback"]
    assert vm.verdict(results, allowed, blocked, fence_off=False)[0].startswith("the probe returned no result")
    results["container:loopback"] = "failed:TimeoutError"
    assert vm.verdict(results, allowed, blocked, fence_off=True)[0].startswith("container:loopback must connect")


@pytest.fixture
def start_host(tmp_path, monkeypatch):
    """`start` as the agents user against a fake podman; preflight's host checks pass."""
    log = tmp_path / "podman.log"
    fake = tmp_path / "bin/podman"
    fake.parent.mkdir()
    fake.write_text(f"""#!/bin/sh
echo "podman $*" >> "{log}"
case "$1 $2" in
  "image inspect") echo "sha256:{'ab' * 32}" ;;
esac
""")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(vm, "preflight", lambda *args, **kwargs: None)
    shut = [_listen("127.0.0.2") for _ in range(6)]
    ports = [s.getsockname()[1] for s in shut]
    for s in shut:
        s.close()
    blocked_env = " ".join(f"{label}=127.0.0.2:{port}" for label, port in zip(vm.BLOCKED_LABELS, ports, strict=True))
    site = _site(tmp_path, MFR_VM_AGENTS_USER=_agents_account(monkeypatch), MFR_VM_BLOCKED=blocked_env,
                 MFR_VM_WORK=str(tmp_path / "work"))
    return {"log": log, "site": site, "ports": dict(zip(vm.BLOCKED_LABELS, ports, strict=True))}


def test_start_runs_the_container_only_while_the_fence_refuses_every_negative_control(start_host, capsys):
    ctx = start_host
    assert vm.main(["start", "--site-env", str(ctx["site"])]) == 0
    runs = [line for line in ctx["log"].read_text().splitlines() if line.startswith("podman run")]
    assert len(runs) == 1 and "--name mfr-agents" in runs[0]
    ctx["log"].unlink()
    # One negative control answers: the fence is not in force for this uid (not
    # installed, removed, or another user), so no container starts.
    leak = socket.socket()
    leak.bind(("127.0.0.2", ctx["ports"]["openshift-api"]))
    leak.listen(1)
    try:
        assert vm.main(["start", "--site-env", str(ctx["site"])]) == 2
    finally:
        leak.close()
    err = capsys.readouterr().err
    assert err.startswith("agents-vm: the egress fence is not in force for this user: blocked:openshift-api "
                          "connected")
    assert "127.0.0" not in err
    assert not ctx["log"].exists()  # not even an image inspect


def test_start_refuses_another_user_before_any_probe(start_host, capsys, tmp_path):
    (tmp_path / "other").mkdir()
    other = _site(tmp_path / "other", MFR_VM_AGENTS_USER="nobody-else")
    assert vm.main(["start", "--site-env", str(other)]) == 2
    assert "run start as the agents user" in capsys.readouterr().err
    assert not start_host["log"].exists()


def test_a_uid_without_a_passwd_entry_is_never_the_agents_user(start_host, capsys, tmp_path, monkeypatch):
    """Fail closed: an account with no passwd entry (the kit's arbitrary CI uid) is not
    `agents`, so neither the proof nor start runs; never a KeyError traceback."""
    _no_passwd_entry(monkeypatch)
    (tmp_path / "proof").mkdir()
    proof_site = _site(tmp_path / "proof", MFR_VM_AGENTS_USER="agents")
    assert vm.main(["proof", "--site-env", str(proof_site), "--where", "host"]) == 2
    assert "run the proof as the agents user" in capsys.readouterr().err
    assert vm.main(["start", "--site-env", str(start_host["site"])]) == 2
    assert "run start as the agents user" in capsys.readouterr().err
    assert not start_host["log"].exists()


def test_container_runs_with_init_the_sized_limits_and_no_openshift_credentials(tmp_path):
    site = vm.load_site(_site(tmp_path))
    argv = vm.run_args(site)
    assert argv[:2] == ["podman", "run"] and "--init" in argv  # setting 5
    # Rootless Podman cannot map the kit's uid with its default userns (Podman trial).
    assert {"--userns=keep-id:uid=1000770000,gid=0", "--user=1000770000:0"} <= set(argv)
    assert "--volume=/home/agents/work:/work:Z" in argv
    assert "--volume=/home/agents/work:/work:z" in vm.run_args(vm.load_site(_site(tmp_path, MFR_VM_VOLUME_LABEL="z")))
    assert "--volume=/home/agents/work:/work" in vm.run_args(vm.load_site(_site(tmp_path, MFR_VM_VOLUME_LABEL="none")))
    # Agent overhead (#1172: requests 2 CPU / 2 GiB, limits 4 CPU / 4 GiB) plus the tools' default 2 CPU / 4 GiB.
    assert {"--cpus=6", "--memory=8192m", "--memory-swap=8192m", "--memory-reservation=2048m",
            "--cpu-shares=2048"} <= set(argv)
    assert "--env=KUBECONFIG=/dev/null" in argv and "--secret=mfr-dev-litellm" in argv
    assert not [a for a in argv if "kube" in a.lower() and a != "--env=KUBECONFIG=/dev/null"]
    assert "--env=MFR_LLM_MODEL=site-reasoning" in argv
    bigger = vm.run_args(vm.load_site(_site(tmp_path, MFR_VM_TOOL_CPUS="4", MFR_VM_TOOL_MEMORY_MIB="8192")))
    assert {"--cpus=8", "--memory=12288m", "--memory-reservation=2048m"} <= set(bigger)


@pytest.mark.parametrize("requested, expected", [
    ("", ["exec", "mfr-agents", "hc", "ls"]),
    ("w-1", ["exec", "-it", "mfr-agents", "hc", "attach", "--observe", "w-1"]),
    ("dev/t7", ["exec", "-it", "mfr-agents", "hc", "attach", "--observe", "dev/t7"]),
    ("w-1; sh", None), ("--help", None), ("../x", None),
])
def test_view_only_ssh_command_attaches_observe_only_or_refuses(tmp_path, requested, expected):
    fake = tmp_path / "podman"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" "xdg=$XDG_RUNTIME_DIR"\n')
    fake.chmod(0o755)
    done = subprocess.run([sys.executable, str(REPO / "scripts/devkit/agents_vm.py"), "observe"],
                          env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "SSH_ORIGINAL_COMMAND": requested},
                          capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if expected is None:
        assert done.returncode == 2 and done.stdout == ""
    else:
        # Podman's own runtime dir, so podman exec finds the container (Podman trial).
        assert done.returncode == 0 and done.stdout.splitlines() == [*expected, f"xdg=/run/user/{os.getuid()}"]


def test_view_only_ssh_command_needs_no_passwd_entry(tmp_path):
    """The kit's arbitrary CI uid has no passwd entry; observe still finds Podman's runtime dir."""
    fake = tmp_path / "podman"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" "xdg=$XDG_RUNTIME_DIR" "home=${HOME-unset}"\n')
    fake.chmod(0o755)
    passwdless = ("import pwd, runpy, sys\n"
                  "def missing(uid):\n    raise KeyError(uid)\n"
                  "pwd.getpwuid = missing\n"
                  "sys.argv = [sys.argv[1], 'observe']\n"
                  "runpy.run_path(sys.argv[0], run_name='__main__')\n")
    done = subprocess.run([sys.executable, "-c", passwdless, str(REPO / "scripts/devkit/agents_vm.py")],
                          env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "SSH_ORIGINAL_COMMAND": ""},
                          capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.splitlines() == ["exec", "mfr-agents", "hc", "ls", f"xdg=/run/user/{os.getuid()}", "home=unset"]


def test_the_example_site_env_renders_once_its_kit_placeholders_are_filled(tmp_path):
    example = (REPO / "scripts/devkit/agents-vm.env.example").read_text()
    # Only documentation addresses (RFC 5737) in the committed example.
    for address in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", example):
        assert address.startswith(("192.0.2.", "198.51.100.", "203.0.113.")) or address == "127.0.0.1", address
    filled = example.replace("<kit key>", "testkey").replace("<64 hex from devkit-pin.txt>", "cd" * 32)
    path = tmp_path / "site.env"
    path.write_text(filled)
    assert vm.main(["render-fence", "--site-env", str(path), "--uid", "1500", "--out", str(tmp_path / "f")]) == 0


def _preflight_root(tmp_path, *, subuid="agents:100000:65536\n", controllers="cpuset cpu io memory pids\n",
                    linger=True):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/subuid").write_text(subuid)
    (root / "etc/subgid").write_text("agents:100000:65536\n")
    (root / "sys/fs/cgroup").mkdir(parents=True)
    (root / "sys/fs/cgroup/cgroup.controllers").write_text("cpu memory pids\n")
    service = root / "sys/fs/cgroup/user.slice/user-1500.slice/user@1500.service"
    service.mkdir(parents=True)
    (service / "cgroup.controllers").write_text(controllers)
    (root / "var/lib/systemd/linger").mkdir(parents=True)
    if linger:
        (root / "var/lib/systemd/linger/agents").touch()
    return root


@pytest.mark.parametrize("knobs, message", [
    ({}, None),
    ({"controllers": "cpu pids\n"}, ("user@1500.service does not delegate memory: --memory, --cpus and the pids "
                                    "limit would be silently unenforced")),
    ({"linger": False}, "linger is off for agents"),
    ({"subuid": "agents:100000:1000\n"}, "/etc/subuid gives agents no range of at least 65536 ids"),
])
def test_preflight_fails_clearly_without_delegation_linger_or_subids(tmp_path, capsys, knobs, message):
    root = _preflight_root(tmp_path, **knobs)
    rc = vm.main(["preflight", "--site-env", str(_site(tmp_path)), "--uid", "1500", "--root", str(root)])
    err = capsys.readouterr().err
    if message is None:
        assert rc == 0 and err == ""
    else:
        assert rc == 2 and err.startswith("agents-vm: preflight failed: ") and message in err


# --- the gap scripts' commands are in the kit image (#1183) --------------------------
# GitLab CI and the bastion run the gap scripts in the kit image, so every external
# command they call must be in it. The commands are derived from the scripts; the map
# (tests/helpers_devkit_commands.py) says which recipe input provides each, and is
# checked here against the recipe and the recorded base rpmdb, with no image.
# tests/devkit_image_contracts.py checks the same map against a built image.



def _base_rpm_names():
    lines = (REPO / "licenses/ubi9-python-314-minimal.rpms.txt").read_text(encoding="utf-8").splitlines()
    return {line.split("\t")[0] for line in lines if line and not line.startswith("#")}


def _kit_rpm_names():
    return {li.rpm_name(nevra)[0] for nevra in li.kit_rpm_lines(REPO) or []}


def test_every_command_the_gap_scripts_call_has_a_recorded_kit_provider():
    """The map names exactly the commands the scripts call: a new command needs a
    provider, and a command no script calls any more leaves the map."""
    calls = kitcmd.gap_commands()
    missing = {c: sorted(s) for c, s in calls.items() if c not in kitcmd.KIT_COMMANDS}
    assert missing == {}, "add each to KIT_COMMANDS with the kit input that provides it"
    assert sorted(set(kitcmd.KIT_COMMANDS) - set(calls)) == []
    # The scan reaches the scripts the gap scripts source and the image's own helpers.
    scanned = {p.relative_to(REPO).as_posix() for p in kitcmd.script_paths()}
    assert {"scripts/tools/task-artifact.sh", "scripts/airgap/common.sh", "scripts/gap/import_release.sh",
            "scripts/devkit/install.sh", "images/devkit/bin/mfr-opencode"} <= scanned
    # The issue's commands (#1183), named independently of the scan.
    assert {"cmp", "sha256sum", "python3", "git", "tar", "skopeo", "openssl", "oc", "awk"} <= set(calls)


def test_each_kit_command_provider_is_installed_by_the_image_recipe():
    """Each provider is in the image: an RPM the recipe installs or the pinned base
    holds, a pinned tool the recipe stages into /usr/local/bin, or the base's Python."""
    recipe = (REPO / "images/Containerfile.devkit").read_text(encoding="utf-8")
    rpms = _kit_rpm_names() | _base_rpm_names()
    absent = []
    for name, provider in sorted(kitcmd.KIT_COMMANDS.items()):
        kind, _, detail = provider.partition(":")
        if kind == "rpm":
            ok = detail in rpms
        elif kind == "tool":
            ok = (f'"$pins/{detail}-pin.txt"' in recipe or f"{detail}-pin.txt" in recipe) and (
                (REPO / f"scripts/tools/{detail}-pin.txt").is_file()
                or (REPO / f"images/devkit/agents/{detail}-pin.txt").is_file())
        elif kind == "kit-bin":
            ok = (REPO / "images/devkit/bin" / name).is_file()
        elif kind == "app-root":
            ok = re.search(r"^FROM registry\.access\.redhat\.com/ubi9/python-314-minimal@sha256:[0-9a-f]{64}",
                           recipe, re.MULTILINE) is not None
        else:
            ok = False
        if not ok:
            absent.append(f"{name}: {provider}")
    assert absent == [], "the kit image does not provide these gap-script commands"
    # cmp and diff come from diffutils, which the recipe installs by exact NEVRA (#1183).
    assert "diffutils-0:3.7-12.el9.x86_64" in (li.kit_rpm_lines(REPO) or [])


def test_command_scan_reads_command_positions_and_skips_data(tmp_path):
    """The scan's own contract on a script with every construct the gap scripts use."""
    script = tmp_path / "s.sh"
    script.write_text(
        "#!/bin/sh\n"
        "# a comment naming `notacmd1` and $(notacmd2)\n"
        "set -eu\n"
        "helper() { alpha \"$1\"; }\n"
        "X=$(beta -x | gamma) Y=\"${Z:-not acmd3}\" delta --flag\n"
        "N=$((N + 1))\n"
        "if epsilon; then zeta; elif ! eta; then :; else theta; fi\n"
        "case \"$1\" in\n"
        "    patone|pattwo) iota ;;\n"
        "    *) kappa\n"
        "        ;;\n"
        "esac\n"
        "for t in lambda mu; do command -v \"$t\" >/dev/null || exit 1; done\n"
        "command -v nu >/dev/null\n"
        "env -u GIT_DIR xi --arg\n"
        "omicron 2>/dev/null </dev/null && pi || rho\n"
        "printf '%s' \"$(sigma \"$(tau)\")\" `upsilon`\n"
        "cat <<'EOF' | phi\n"
        "heredocword --not-a-command\n"
        "EOF\n"
        "find . -print0 | xargs -0 chi\n"
        "exec psi \"$@\"\n"
        "helper x\n",
        encoding="utf-8")
    assert kitcmd.scan(script) - kitcmd.defined_functions([script]) == {
        "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu",
        "nu", "env", "xi", "omicron", "pi", "rho", "sigma", "tau", "upsilon", "cat", "phi", "find", "xargs",
        "chi", "psi"}
    assert kitcmd.interpreter(script) == {"sh"}
