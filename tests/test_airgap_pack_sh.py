"""scripts/airgap/pack.sh factory tests (issue #15).

Hermetic tests: pack.sh against a stubbed skopeo and a real throwaway git
repo — no GHCR, no network. Covers the fail-closed gates (SHA==HEAD, git
repo present, skopeo present) and the happy path: bundle + 4 image tars +
MANIFEST + packing record + member checksums + tarball digest, with the
bundle clone-traversable (the airgap-package CI check, hermetically).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Stub skopeo: materialize every docker-archive:DEST as a marker file.
STUB_SKOPEO = """#!/bin/sh
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
    p = tmp_path / "bin" / "skopeo"
    p.write_text(STUB_SKOPEO)
    p.chmod(0o755)
    return tmp_path, skopeo_log, head


def _run_pack(tree, *extra_env, with_skopeo=True):
    tmp_path, _skopeo_log, head = tree
    path = f"{tmp_path / 'bin'}:/usr/bin:/bin" if with_skopeo else "/usr/bin:/bin"
    env = {
        "PATH": path,
        "SKOPEO_LOG": str(tmp_path / "skopeo-args.log"),
        "AIRGAP_APP_REGISTRY": "ghcr.io/pack-test",
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
    r, _head = _run_pack(pack_tree, with_skopeo=False)
    assert r.returncode != 0
    assert "skopeo is required on the connected pack host" in r.stderr


def test_pack_outside_git_repo_fails_closed(pack_tree):
    tmp_path, _, _ = pack_tree
    (tmp_path / ".git").rename(tmp_path / ".git-bak")
    try:
        r, _head = _run_pack(pack_tree)
    finally:
        (tmp_path / ".git-bak").rename(tmp_path / ".git")
    assert r.returncode != 0
    assert "run from a git clone of the repository" in r.stderr


def test_pack_success_builds_verified_tarball(pack_tree):
    tmp_path, skopeo_log, head = pack_tree
    r, _ = _run_pack(pack_tree)
    assert r.returncode == 0, r.stderr
    dist = tmp_path / "dist"

    # All 8 members + tarball + tarball digest.
    for name in (
        "bootstrap.sh",
        "repo.bundle",
        "qdrant-image.tar",
        "jaeger-image.tar",
        f"app-ingest-{head}.tar",
        f"app-agent-{head}.tar",
        "MANIFEST.txt",
        "PACKING_RECORD.txt",
        "SHA256SUMS",
        f"qdrant-pdf-rag-{head}.tar",
        f"qdrant-pdf-rag-{head}.tar.sha256",
    ):
        assert (dist / name).is_file(), name

    # MANIFEST pins this SHA and the images that were "pulled".
    manifest = (dist / "MANIFEST.txt").read_text()
    assert f"sha: {head}" in manifest
    assert f"ghcr.io/pack-test/qdrant-pdf-rag-ingest:{head}" in manifest
    assert f"ghcr.io/pack-test/qdrant-pdf-rag-agent:{head}" in manifest
    log = skopeo_log.read_text()
    assert log.count("docker-archive:") == 4

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
