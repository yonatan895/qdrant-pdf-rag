"""scripts/airgap/bootstrap.sh sneakernet extraction and setup tests (issue #15).

Tests bootstrap.sh against a mock sneakernet extraction directory:
- Missing SHA256SUMS fails closed.
- Checksum mismatch fails closed.
- Successful verification clones the bundle, populates dist/, and initializes airgap.env.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def bundle_dir(tmp_path):
    # Create mock bundle directory simulating extracted sneakernet tarball
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    shutil.copy(REPO / "scripts" / "airgap" / "bootstrap.sh", extract_dir / "bootstrap.sh")

    # Create a real git repo and make a bundle from it
    src_repo = tmp_path / "src_repo"
    src_repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=src_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=src_repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=src_repo, check=True)
    (src_repo / "README.md").write_text("Hello")
    (src_repo / "airgap.env.example").write_text("INTERNAL_REGISTRY=example\n")
    subprocess.run(["git", "add", "."], cwd=src_repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=src_repo, check=True)
    subprocess.run(["git", "bundle", "create", str(extract_dir / "repo.bundle"), "HEAD", "--all"], cwd=src_repo, check=True)

    # Add mock image files
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", str(extract_dir / "signing.key")],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(extract_dir / "signing.key"),
         "-pubout", "-out", str(extract_dir / "sneakernet-signing.pub")],
        check=True,
        capture_output=True,
    )
    files = {
        "bootstrap.sh": (extract_dir / "bootstrap.sh").read_bytes(),
        "repo.bundle": (extract_dir / "repo.bundle").read_bytes(),
        "qdrant-image.tar": b"mock-qdrant",
        "jaeger-image.tar": b"mock-jaeger",
        "app-ingest-test.tar": b"mock-ingest",
        "app-agent-test.tar": b"mock-agent",
        "MANIFEST.txt": b"sha: test\n",
        "PACKING_RECORD.txt": b"record\n",
        "sbom.json": b'{"images": []}\n',
        "sneakernet-signing.pub": (extract_dir / "sneakernet-signing.pub").read_bytes(),
    }
    sums = []
    for name, content in files.items():
        p = extract_dir / name
        p.write_bytes(content)
        sums.append(f"{_sha256(content)}  {name}\n")
    (extract_dir / "SHA256SUMS").write_text("".join(sums))
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(extract_dir / "signing.key"),
         "-out", str(extract_dir / "SHA256SUMS.sig"), str(extract_dir / "SHA256SUMS")],
        check=True,
        capture_output=True,
    )

    return extract_dir


def test_bootstrap_missing_sums_fails(bundle_dir):
    (bundle_dir / "SHA256SUMS").unlink()
    r = subprocess.run(["sh", "bootstrap.sh"], cwd=bundle_dir, capture_output=True, text=True, check=False)
    assert r.returncode != 0
    assert "SHA256SUMS not found" in r.stderr


def test_bootstrap_corrupt_checksum_fails(bundle_dir):
    (bundle_dir / "qdrant-image.tar").write_bytes(b"corrupt")
    r = subprocess.run(["sh", "bootstrap.sh"], cwd=bundle_dir, capture_output=True, text=True, check=False)
    assert r.returncode != 0
    assert "FAILED" in r.stdout or "FAIL" in r.stderr


def test_bootstrap_tampered_sums_fails_signature(bundle_dir):
    with open(bundle_dir / "SHA256SUMS", "a") as f:
        f.write("0" * 64 + "  injected\n")
    r = subprocess.run(["sh", "bootstrap.sh"], cwd=bundle_dir, capture_output=True, text=True, check=False)
    assert r.returncode != 0
    assert "signature verification failed" in r.stderr


def test_bootstrap_success(bundle_dir):
    r = subprocess.run(
        ["sh", "bootstrap.sh"],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AIRGAP_WORKSPACE": "workspace"},
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "Sneakernet bootstrap completed successfully." in r.stdout

    # Verify repository clone was created
    repo_dir = bundle_dir / "workspace"
    assert (repo_dir / ".git").is_dir()
    assert (repo_dir / "airgap.env").is_file()
    assert "INTERNAL_REGISTRY=example" in (repo_dir / "airgap.env").read_text()

    # Verify dist directory was populated with image artifacts
    dist_dir = repo_dir / "dist"
    assert (dist_dir / "bootstrap.sh").is_file()
    assert (dist_dir / "qdrant-image.tar").is_file()
    assert (dist_dir / "app-agent-test.tar").is_file()
    assert (dist_dir / "MANIFEST.txt").is_file()
    assert (dist_dir / "PACKING_RECORD.txt").is_file()
    assert (dist_dir / "sbom.json").is_file()
    assert (dist_dir / "sneakernet-signing.pub").is_file()
    assert (dist_dir / "SHA256SUMS.sig").is_file()
