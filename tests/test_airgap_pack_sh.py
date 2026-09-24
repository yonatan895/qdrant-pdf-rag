"""scripts/airgap/pack.sh factory tests (issue #15).

Hermetic tests: pack.sh against a stubbed skopeo and a real throwaway git
repo — no GHCR, no network. Covers the fail-closed gates (SHA==HEAD, git
repo present, skopeo present, signing key present) and the happy path:
bundle + 4 image tars + MANIFEST (with image digests) + sbom.json +
offline signature + member checksums + tarball digest, with the bundle
clone-traversable (the airgap-package CI check, hermetically).
"""

import json
import os
import shutil
import subprocess
import tarfile

import pytest

from tests.helpers_airgap import (
    REPO,
    copy_chart,
    gen_sign_keypair,
    make_bin_tree,
    run_sh,
    set_oauth_proxy_pin,
    skopeo_stub,
    symlink_tools,
)
from tests.helpers_image_inventory import image_files, write_image
from tests.helpers_task_artifact import (
    TASK_ASSET,
    TASK_BINARY_SHA256,
    TASK_SHA256,
    copy_task_tools,
    task_archive,
)

# Stub skopeo: materialize every docker-archive:DEST as a marker file;
# answer inspect with a canned digest (pack binds it into MANIFEST, so the
# self-consistency is what the test proves).
STUB_SKOPEO = skopeo_stub("a", materialize=True).replace(
    "printf 'stub-image-tar\\n' > \"$dest\"", 'cp "$PACK_TEST_IMAGE" "$dest"')

STUB_DIGEST = "sha256:" + "a" * 64

# Hermetic tool PATH: every external pack.sh needs, symlinked from the host.
# skopeo is intentionally absent unless the stub below adds it — CI runners
# ship a real skopeo in /usr/bin, so relying on its absence there is red.
TOOLS = (
    "sh",
    "dirname",
    "awk",
    "sed",
    "git",
    "tar",
    "sha256sum",
    "date",
    "basename",
    "cat",
    "chmod",
    "cp",
    "mkdir",
    "rm",
    "grep",
    "tr",
    "cut",
    "head",
    "ls",
    "openssl",
    "python3",
    "mktemp",
    "uname",
    "gzip",
    "cmp",
)


@pytest.fixture
def pack_tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "pack.sh", "bootstrap.sh"])
    copy_task_tools(tmp_path)
    shutil.copy(REPO / "images.txt", tmp_path / "images.txt")
    # Four-image tests exercise the pending state explicitly, even after a
    # production pin is recorded. The recorded-pin test covers all five images.
    set_oauth_proxy_pin(tmp_path, "sha256:PENDING")
    shutil.copy(REPO / "requirements.lock.txt", tmp_path / "requirements.lock.txt")
    shutil.copytree(REPO / "locks", tmp_path / "locks")
    for script in ("dependency_lock.py", "image_inventory.py"):
        shutil.copy(REPO / "scripts" / script, tmp_path / "scripts" / script)
    (tmp_path / "charts").mkdir(exist_ok=True)
    copy_chart(tmp_path)

    # Throwaway git repo so IMAGE_SHA resolves to a real HEAD.
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("Hello")
    (tmp_path / "Taskfile.yml").write_text("version: '3'\ntasks:\n  probe:\n    cmds: ['printf packed-task-ok']\n")
    (tmp_path / "airgap.env.example").write_text("INTERNAL_REGISTRY=fixture\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    skopeo_log = tmp_path / "skopeo-args.log"
    symlink_tools(tmp_path, TOOLS)
    p = tmp_path / "bin" / "skopeo"
    p.write_text(STUB_SKOPEO)
    p.chmod(0o755)

    write_image(tmp_path / "synthetic-image.tar", [image_files(tmp_path)])
    # Throwaway signing key (mirrors the rehearsal flow: pack signs, the
    # bundle carries the derived pub, verification is self-consistent).
    key = gen_sign_keypair(tmp_path)
    return tmp_path, skopeo_log, head, key


def _run_pack(tree, *extra_env):
    tmp_path, _skopeo_log, head, key = tree
    env = {
        "PATH": str(tmp_path / "bin"),
        "SKOPEO_LOG": str(tmp_path / "skopeo-args.log"),
        "PACK_TEST_IMAGE": str(tmp_path / "synthetic-image.tar"),
        "AIRGAP_APP_REGISTRY": "ghcr.io/pack-test",
        "SNEAKERNET_SIGNING_KEY": str(key),
        "AIRGAP_TASK_ARCHIVE": str(task_archive()),
    }
    for k, v in extra_env:
        env[k] = v
    return (
        run_sh(tmp_path / "scripts" / "airgap" / "pack.sh", env, tmp_path),
        head,
    )


def test_pack_sha_mismatch_fails_closed(pack_tree):
    r, _head = _run_pack(pack_tree, ("IMAGE_SHA", "f" * 40))
    assert r.returncode != 0
    assert "is not the checked-out commit" in r.stderr
    assert "airgap.env" in r.stderr


def test_pack_missing_skopeo_fails_closed(pack_tree):
    os.remove(pack_tree[0] / "bin" / "skopeo")
    r, _head = _run_pack(pack_tree)
    assert r.returncode != 0
    assert "skopeo is required on the connected pack host" in r.stderr


def test_pack_missing_signing_key_fails_closed(pack_tree):
    tmp_path, _, _, _ = pack_tree
    (tmp_path / "signing.key").unlink()
    r, _head = _run_pack(pack_tree)
    assert r.returncode != 0
    assert "SNEAKERNET_SIGNING_KEY" in r.stderr


def test_pack_trusted_key_labels_signed_true(pack_tree):
    tmp_path, _, _, _ = pack_tree
    r, _head = _run_pack(pack_tree, ("SNEAKERNET_KEY_TRUSTED", "true"))
    assert r.returncode == 0, r.stderr
    assert "signed: true" in (tmp_path / "dist" / "MANIFEST.txt").read_text()


def test_pack_outside_git_repo_fails_closed(pack_tree):
    tmp_path, _, _, _ = pack_tree
    (tmp_path / ".git").rename(tmp_path / ".git-bak")
    try:
        r, _head = _run_pack(pack_tree)
    finally:
        (tmp_path / ".git-bak").rename(tmp_path / ".git")
    assert r.returncode != 0
    assert "run from a git clone of the repository" in r.stderr


def test_pack_success_builds_verified_tarball(pack_tree):
    tmp_path, skopeo_log, head, _key = pack_tree
    r, _ = _run_pack(pack_tree)
    assert r.returncode == 0, r.stderr
    dist = tmp_path / "dist"

    # All members + tarball + tarball digest.
    for name in (
        "bootstrap.sh",
        "repo.bundle",
        TASK_ASSET, "task-pin.txt", "task-LICENSE",
        "qdrant-image.tar",
        "jaeger-image.tar",
        f"app-ingest-{head}.tar",
        f"app-agent-{head}.tar",
        "MANIFEST.txt",
        "PACKING_RECORD.txt",
        "sbom.json",
        "sneakernet-signing.pub",
        "SHA256SUMS",
        "SHA256SUMS.sig",
        f"qdrant-pdf-rag-{head}.tar",
        f"qdrant-pdf-rag-{head}.tar.sha256",
    ):
        assert (dist / name).is_file(), name

    # Exercise the exact archive just produced, not a reconstructed bootstrap fixture.
    extract = tmp_path / "fresh offline extraction"
    extract.mkdir()
    with tarfile.open(dist / f"qdrant-pdf-rag-{head}.tar") as archive:
        archive.extractall(extract, filter="data")
    boot = subprocess.run(["sh", "bootstrap.sh"], cwd=extract, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=False)
    assert boot.returncode == 0, boot.stdout + boot.stderr
    workspace = extract / "qdrant-pdf-rag"
    probe = subprocess.run([str(workspace / ".tools/bin/task"), "probe"], cwd=workspace, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=False)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout == "packed-task-ok"

    # MANIFEST pins this SHA, the images that were "pulled", and their digests.
    manifest = (dist / "MANIFEST.txt").read_text()
    assert f"sha: {head}" in manifest
    assert f"ghcr.io/pack-test/qdrant-pdf-rag-ingest:{head}" in manifest
    assert f"ghcr.io/pack-test/qdrant-pdf-rag-agent:{head}" in manifest
    assert f"ingest_digest: {STUB_DIGEST}" in manifest
    assert f"agent_digest: {STUB_DIGEST}" in manifest
    assert "qdrant_digest: sha256:" in manifest
    # Tar-manifest digests, not the images.txt list pins: the stub answers
    # every inspect identically, while the fixture images.txt pins differ.
    assert f"qdrant_digest: {STUB_DIGEST}" in manifest
    assert "signed: ephemeral" in manifest
    assert f"task_sha256: {TASK_SHA256}" in manifest
    assert f"task_binary_sha256: {TASK_BINARY_SHA256}" in manifest
    log = skopeo_log.read_text()
    assert log.splitlines().count("copy") == 4
    assert log.count("inspect") == 4
    # Digest-only refs: tag+digest combined is not a valid reference.
    assert "docker.io/qdrant/qdrant@sha256:" in log
    assert "@sha256:" in log
    assert ":v1.19.0-unprivileged@sha256:" not in log

    # sbom.json enumerates the pinned inputs as valid JSON.
    sbom = json.loads((dist / "sbom.json").read_text())
    assert sbom["image_sha"] == head
    assert {img["name"] for img in sbom["images"]} == {"qdrant", "jaeger", "app-ingest", "app-agent"}
    assert sbom["images"][2]["digest"] == STUB_DIGEST
    assert sbom["host_tools"][0]["sha256"] == TASK_SHA256
    assert sbom["host_tools"][0]["license"] == "MIT"
    assert sbom["host_tools"][0]["license_file"] == "task-LICENSE"
    assert "Task host tool:" in (dist / "PACKING_RECORD.txt").read_text()
    with tarfile.open(dist / f"qdrant-pdf-rag-{head}.tar") as archive:
        assert set(archive.getnames()) == {
            "bootstrap.sh", "repo.bundle", TASK_ASSET, "task-pin.txt", "task-LICENSE",
            "qdrant-image.tar", "jaeger-image.tar", f"app-ingest-{head}.tar", f"app-agent-{head}.tar",
            "MANIFEST.txt", "PACKING_RECORD.txt", "sbom.json", "sneakernet-signing.pub",
            "SHA256SUMS", "SHA256SUMS.sig",
        }


def test_pack_skips_pending_oauth_proxy_pin(pack_tree):
    """An explicitly pending pin leaves the sidecar out of the bundle."""
    tmp_path, skopeo_log, _head, _key = pack_tree
    r, _ = _run_pack(pack_tree)
    assert r.returncode == 0, r.stderr
    dist = tmp_path / "dist"
    assert "sha256:PENDING: not bundled" in r.stdout
    assert not (dist / "oauth-proxy-image.tar").exists()
    manifest = (dist / "MANIFEST.txt").read_text()
    assert "oauth_proxy" not in manifest
    assert skopeo_log.read_text().splitlines().count("copy") == 4
    sbom = json.loads((dist / "sbom.json").read_text())
    assert "oauth-proxy" not in {img["name"] for img in sbom["images"]}


def test_pack_bundles_oauth_proxy_once_digest_recorded(pack_tree):
    """Once the connected host records a digest, the sidecar image joins the
    bundle, MANIFEST, SBOM and member checksums."""
    tmp_path, skopeo_log, head, _key = pack_tree
    set_oauth_proxy_pin(tmp_path, "sha256:" + "b" * 64)
    # Model signed-source export: without attachment omission, Docker
    # archive export fails before the archive can be materialized.
    guard = '''signed_source=0
remove_signatures=0
for arg in "$@"; do
  case "$arg" in
    docker://registry.redhat.io/openshift4/ose-oauth-proxy@*) signed_source=1 ;;
    --remove-signatures) remove_signatures=1 ;;
  esac
done
if [ "$signed_source" = 1 ] && [ "$remove_signatures" != 1 ]; then
  echo "Docker archives cannot store signature attachments" >&2
  exit 1
fi
'''
    (tmp_path / "bin" / "skopeo").write_text(STUB_SKOPEO.replace("#!/bin/sh\n", "#!/bin/sh\n" + guard, 1))
    r, _ = _run_pack(pack_tree)
    assert r.returncode == 0, r.stderr
    dist = tmp_path / "dist"
    assert (dist / "oauth-proxy-image.tar").is_file()
    assert skopeo_log.read_text().splitlines().count("--remove-signatures") == 1
    manifest = (dist / "MANIFEST.txt").read_text()
    assert "oauth_proxy: registry.redhat.io/openshift4/ose-oauth-proxy@sha256:" + "b" * 64 in manifest
    assert f"oauth_proxy_digest: {STUB_DIGEST}" in manifest
    assert "oauth-proxy-image.tar" in (dist / "SHA256SUMS").read_text()
    with tarfile.open(dist / f"qdrant-pdf-rag-{head}.tar") as tf:
        assert "oauth-proxy-image.tar" in tf.getnames()
    sbom = json.loads((dist / "sbom.json").read_text())
    assert {img["name"] for img in sbom["images"]} == {
        "qdrant",
        "jaeger",
        "app-ingest",
        "app-agent",
        "oauth-proxy",
    }

    # Offline signature verifies against the derived pubkey.
    sig = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", "sneakernet-signing.pub",
         "-signature", "SHA256SUMS.sig", "SHA256SUMS"],
        cwd=dist,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sig.returncode == 0, sig.stdout + sig.stderr

    # Member checksums verify inside dist/.
    sums = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"], cwd=dist, capture_output=True, text=True, check=False
    )
    assert sums.returncode == 0, sums.stdout + sums.stderr

    # Tarball digest verifies.
    outer = subprocess.run(
        ["sha256sum", "-c", f"qdrant-pdf-rag-{head}.tar.sha256"],
        cwd=dist,
        capture_output=True,
        text=True,
        check=False,
    )
    assert outer.returncode == 0, outer.stdout + outer.stderr

    # The bundle clones and traverses (the runbook's first operator step).
    clone = tmp_path / "bundle-clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(dist / "repo.bundle"), str(clone)], check=True
    )
    logged = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert logged == head


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_pack_rejects_bad_task_before_image_calls(pack_tree, damage):
    tree, log, _, _ = pack_tree
    archive = tree / "unapproved.tar.gz"
    if damage == "corrupt":
        archive.write_bytes(b"not Task")
    result, _ = _run_pack(pack_tree, ("AIRGAP_TASK_ARCHIVE", str(archive)))
    assert result.returncode != 0
    assert "Task archive" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("corruption", ["installed-only", "receipt-omission", "missing-package"])
def test_pack_rejects_actual_inventory_drift_before_signing(pack_tree, corruption):
    root = pack_tree[0]
    files = image_files(root)
    if corruption == "installed-only":
        files["opt/app-root/lib/python3.14/site-packages/injected-1.0.dist-info/METADATA"] = b"Name: injected\nVersion: 1.0\n"
    elif corruption == "missing-package":
        del files[next(p for p in files if p.endswith(".dist-info/METADATA"))]
    else:
        key = "opt/rag-locks/installed-inventory.json"
        receipt = json.loads(files[key])
        del receipt["packages"][next(iter(receipt["packages"]))]
        files[key] = json.dumps(receipt).encode()
    write_image(root / "synthetic-image.tar", [files])
    result, _ = _run_pack(pack_tree)
    assert result.returncode != 0
    assert "SBOM reconciliation failed" in result.stderr
    assert not list((root / "dist").glob("SHA256SUMS.sig"))
