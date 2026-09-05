"""scripts/airgap/load.sh fail-close and operability tests (issue #15).

Hermetic tests: tests artifact discovery (dist, parent, AIRGAP_BUNDLE_DIR),
checksum verification, MANIFEST sha validation, skopeo copy invocations,
and SKOPEO_ARGS / INSECURE_REGISTRY flag propagation without network or live registries.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMAGE_SHA = "b" * 40

STUB_SKOPEO = """#!/bin/sh
if [ "$1" = "inspect" ]; then
  printf 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n'
  printf '%s\\n' "$@" >> "$SKOPEO_LOG"
  exit 0
fi
printf '%s\\n' "$@" >> "$SKOPEO_LOG"
exit 0
"""

STUB_DIGEST = "sha256:" + "b" * 64


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_artifacts(artdir: Path, sha: str = IMAGE_SHA, corrupt: bool = False):
    artdir.mkdir(parents=True, exist_ok=True)
    files = {
        "repo.bundle": b"bundle-content\n",
        "qdrant-image.tar": b"qdrant-tar\n",
        "jaeger-image.tar": b"jaeger-tar\n",
        f"app-ingest-{sha}.tar": b"ingest-tar\n",
        f"app-agent-{sha}.tar": b"agent-tar\n",
        "MANIFEST.txt": (
            f"sha: {sha}\ndate: 2026-09-05T00:00:00Z\n"
            f"qdrant_digest: {STUB_DIGEST}\n"
            f"jaeger_digest: {STUB_DIGEST}\n"
            f"ingest_digest: {STUB_DIGEST}\n"
            f"agent_digest: {STUB_DIGEST}\n"
        ).encode(),
        "sbom.json": b'{"images": []}\n',
    }
    sums = []
    for name, content in files.items():
        p = artdir / name
        p.write_bytes(content)
        digest = _sha256(b"corrupt\n" if corrupt and name == "qdrant-image.tar" else content)
        sums.append(f"{digest}  {name}\n")
    (artdir / "SHA256SUMS").write_text("".join(sums))
    _sign_artifacts(artdir)


def _sign_artifacts(artdir: Path) -> None:
    """Throwaway keypair + offline signature, mirroring pack.sh output."""
    key = artdir / "signing.key"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", str(key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(key), "-pubout",
         "-out", str(artdir / "sneakernet-signing.pub")],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key),
         "-out", str(artdir / "SHA256SUMS.sig"), str(artdir / "SHA256SUMS")],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def load_tree(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "scripts" / "airgap").mkdir(parents=True)
    for f in ("common.sh", "load.sh"):
        shutil.copy(REPO / "scripts" / "airgap" / f, tmp_path / "scripts" / "airgap" / f)
    skopeo_log = tmp_path / "skopeo-args.log"
    p = tmp_path / "bin" / "skopeo"
    p.write_text(STUB_SKOPEO)
    p.chmod(0o755)
    return tmp_path, skopeo_log


def _run_load(tree, *extra_env, cwd=None):
    tmp_path, skopeo_log = tree
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "SKOPEO_LOG": str(skopeo_log),
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
    }
    for k, v in extra_env:
        env[k] = v
    return subprocess.run(
        ["sh", str(tmp_path / "scripts" / "airgap" / "load.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or tmp_path,
        check=False,
    )


def test_load_missing_artifacts_fails_closed(load_tree):
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert "packed artifacts not found" in r.stderr


def test_load_corrupted_checksum_fails(load_tree):
    tmp_path, _ = load_tree
    _make_artifacts(tmp_path / "dist", corrupt=True)
    r = _run_load(load_tree)
    assert r.returncode != 0
    assert "FAILED" in r.stdout or "FAILED" in r.stderr or "checksum" in r.stderr.lower()


def test_load_manifest_sha_mismatch_fails_closed(load_tree):
    tmp_path, _ = load_tree
    _make_artifacts(tmp_path / "dist", sha="c" * 40)
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert "does not match the packed MANIFEST sha" in r.stderr


def test_load_success_with_dist_dir(load_tree):
    tmp_path, skopeo_log = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    r = _run_load(load_tree)
    assert r.returncode == 0, r.stderr
    assert "Loaded 4 images into reg.internal:5000" in r.stdout
    log = skopeo_log.read_text()
    assert f"docker://reg.internal:5000/qdrant-pdf-rag-agent:{IMAGE_SHA}" in log
    assert f"docker://reg.internal:5000/qdrant-pdf-rag-ingest:{IMAGE_SHA}" in log
    assert "docker://reg.internal:5000/qdrant/qdrant:v1.19.0-unprivileged" in log
    assert "docker://reg.internal:5000/jaegertracing/jaeger:v2.20.0" in log


def test_load_success_with_parent_dir(load_tree):
    tmp_path, skopeo_log = load_tree
    _make_artifacts(tmp_path, sha=IMAGE_SHA)
    subdir = tmp_path / "clone-dir"
    subdir.mkdir()
    (subdir / "scripts" / "airgap").mkdir(parents=True)
    for f in ("common.sh", "load.sh"):
        shutil.copy(REPO / "scripts" / "airgap" / f, subdir / "scripts" / "airgap" / f)
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "SKOPEO_LOG": str(skopeo_log),
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
    }
    r = subprocess.run(
        ["sh", str(subdir / "scripts" / "airgap" / "load.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=subdir,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "Loaded 4 images into reg.internal:5000" in r.stdout
    assert skopeo_log.exists()


def test_load_success_with_bundle_dir_override(load_tree):
    tmp_path, _skopeo_log = load_tree
    custom_dir = tmp_path / "custom-bundle-location"
    _make_artifacts(custom_dir, sha=IMAGE_SHA)
    r = _run_load(load_tree, ("AIRGAP_BUNDLE_DIR", str(custom_dir)))
    assert r.returncode == 0, r.stderr
    assert "Loaded 4 images into reg.internal:5000" in r.stdout


def test_load_skopeo_args_forwarded(load_tree):
    tmp_path, skopeo_log = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    r = _run_load(load_tree, ("SKOPEO_ARGS", "--authfile /tmp/auth.json"))
    assert r.returncode == 0, r.stderr
    log = skopeo_log.read_text()
    assert "--authfile" in log
    assert "/tmp/auth.json" in log


def test_load_insecure_registry_flag(load_tree):
    tmp_path, skopeo_log = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    r = _run_load(load_tree, ("INSECURE_REGISTRY", "true"))
    assert r.returncode == 0, r.stderr
    log = skopeo_log.read_text()
    assert "--dest-tls-verify=false" in log


def test_load_tampered_sums_fails_signature(load_tree):
    tmp_path, _ = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    with open(tmp_path / "dist" / "SHA256SUMS", "a") as f:
        f.write(f"{'0' * 64}  injected\n")
    r = _run_load(load_tree)
    assert r.returncode != 0
    assert "signature verification failed" in r.stderr


def test_load_image_digest_mismatch_fails_closed(load_tree):
    tmp_path, _ = load_tree
    artdir = tmp_path / "dist"
    _make_artifacts(artdir, sha=IMAGE_SHA)
    # A validly-signed bundle with a lying manifest: re-checksum and
    # re-sign after the edit, so only the digest binding can catch it.
    manifest = artdir / "MANIFEST.txt"
    manifest.write_text(manifest.read_text().replace(STUB_DIGEST, "sha256:" + "c" * 64, 1))
    sums = []
    for line in (artdir / "SHA256SUMS").read_text().splitlines():
        name = line.split("  ", 1)[1]
        sums.append(f"{hashlib.sha256((artdir / name).read_bytes()).hexdigest()}  {name}\n")
    (artdir / "SHA256SUMS").write_text("".join(sums))
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(artdir / "signing.key"),
         "-out", str(artdir / "SHA256SUMS.sig"), str(artdir / "SHA256SUMS")],
        check=True,
        capture_output=True,
    )
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert "does not match MANIFEST" in r.stderr


def test_load_trusted_pub_mismatch_refuses(load_tree):
    tmp_path, _ = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    other = tmp_path / "other.key"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", str(other)],
        check=True,
        capture_output=True,
    )
    other_pub = tmp_path / "other.pub"
    subprocess.run(
        ["openssl", "pkey", "-in", str(other), "-pubout", "-out", str(other_pub)],
        check=True,
        capture_output=True,
    )
    r = _run_load(load_tree, ("SNEAKERNET_TRUSTED_PUB", str(other_pub)))
    assert r.returncode != 0
    assert "does not match SNEAKERNET_TRUSTED_PUB" in r.stderr


def test_load_trusted_pub_match_passes(load_tree):
    tmp_path, _ = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    r = _run_load(load_tree, ("SNEAKERNET_TRUSTED_PUB", str(tmp_path / "dist" / "sneakernet-signing.pub")))
    assert r.returncode == 0, r.stderr
    assert "Loaded 4 images into reg.internal:5000" in r.stdout


def test_load_missing_internal_registry_fails_closed(load_tree):
    tmp_path, _ = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    r = _run_load(load_tree, ("INTERNAL_REGISTRY", ""))
    assert r.returncode == 1
    assert "required variables unset: INTERNAL_REGISTRY" in r.stderr
