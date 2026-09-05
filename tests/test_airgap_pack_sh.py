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
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Stub skopeo: materialize every docker-archive:DEST as a marker file;
# answer inspect with a canned digest (pack binds it into MANIFEST, so the
# self-consistency is what the test proves).
STUB_SKOPEO = """#!/bin/sh
if [ "$1" = "inspect" ]; then
  printf 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n'
  printf '%s\\n' "$@" >> "$SKOPEO_LOG"
  exit 0
fi
for arg in "$@"; do
  case "$arg" in
    docker-archive:*)
      dest="${arg#docker-archive:}"
      mkdir -p "$(dirname "$dest")"
      printf 'stub-image-tar\\n' > "$dest"
      ;;
  esac
done
printf '%s\\n' "$@" >> "$SKOPEO_LOG"
exit 0
"""

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
)


@pytest.fixture
def pack_tree(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "scripts" / "airgap").mkdir(parents=True)
    for f in ("common.sh", "pack.sh", "bootstrap.sh"):
        shutil.copy(REPO / "scripts" / "airgap" / f, tmp_path / "scripts" / "airgap" / f)
    shutil.copy(REPO / "images.txt", tmp_path / "images.txt")
    (tmp_path / "charts").mkdir()
    shutil.copy(next(REPO.glob("charts/qdrant-*.tgz")), tmp_path / "charts")

    # Throwaway git repo so IMAGE_SHA resolves to a real HEAD.
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("Hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    skopeo_log = tmp_path / "skopeo-args.log"
    for tool in TOOLS:
        src = shutil.which(tool)
        if src and not (tmp_path / "bin" / tool).exists():
            (tmp_path / "bin" / tool).symlink_to(src)
    p = tmp_path / "bin" / "skopeo"
    p.write_text(STUB_SKOPEO)
    p.chmod(0o755)

    # Throwaway signing key (mirrors the rehearsal flow: pack signs, the
    # bundle carries the derived pub, verification is self-consistent).
    key = tmp_path / "signing.key"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", str(key)],
        check=True,
        capture_output=True,
    )
    return tmp_path, skopeo_log, head, key


def _run_pack(tree, *extra_env):
    tmp_path, _skopeo_log, head, key = tree
    env = {
        "PATH": str(tmp_path / "bin"),
        "SKOPEO_LOG": str(tmp_path / "skopeo-args.log"),
        "AIRGAP_APP_REGISTRY": "ghcr.io/pack-test",
        "SNEAKERNET_SIGNING_KEY": str(key),
    }
    for k, v in extra_env:
        env[k] = v
    return (
        subprocess.run(
            ["sh", str(tmp_path / "scripts" / "airgap" / "pack.sh")],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
        ),
        head,
    )


def test_pack_sha_mismatch_fails_closed(pack_tree):
    r, _head = _run_pack(pack_tree, ("IMAGE_SHA", "f" * 40))
    assert r.returncode != 0
    assert "is not the checked-out commit" in r.stderr


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
    assert "signed: true" in manifest
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
