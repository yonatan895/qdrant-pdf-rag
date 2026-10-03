"""scripts/airgap/load.sh fail-close and operability tests (issue #15).

Hermetic tests: tests artifact discovery (dist, parent, AIRGAP_BUNDLE_DIR),
checksum verification, MANIFEST sha validation, skopeo copy invocations,
and SKOPEO_ARGS / INSECURE_REGISTRY flag propagation without network or live registries.
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers_airgap import (
    REPO,
    gen_other_pub,
    make_bin_tree,
    run_sh,
    sha256_bytes,
    sign_sums,
    write_git_identity_stub,
    write_stub,
)
from tests.helpers_task_artifact import copy_task_tools, task_manifest, task_members

IMAGE_SHA = "b" * 40

# skopeo stub with real registry semantics for identity checks. Archive
# manifests are canned files next to the archives (.skopeo-stub/archive/);
# `copy` "pushes" the matching .skopeo-stub/pushed/ manifest into the stub
# registry ($SKOPEO_STATE/registry/), and `inspect docker://` reads it back.
# Every call is logged on one line.
STUB_SKOPEO = r"""#!/bin/sh
printf '%s\n' "$*" >> "$SKOPEO_LOG"
src=""; dst=""; raw=0
for a in "$@"; do
  case "$a" in
    --raw) raw=1 ;;
    docker-archive:*) src="${a#docker-archive:}" ;;
    docker://*) dst="${a#docker://}" ;;
  esac
done
key=$(printf '%s' "$dst" | tr '/:@' '___')
case "$1" in
  inspect)
    if [ -n "$dst" ]; then
      f="$SKOPEO_STATE/registry/$key.raw"
      [ -f "$f" ] || { echo "manifest unknown" >&2; exit 1; }
      cat "$f"
    else
      stubdir="$(dirname "$src")/.skopeo-stub"
      name=$(basename "$src")
      f="$stubdir/archive/$name.raw"
      if [ "$raw" = 1 ]; then cat "$f"
      elif [ -f "$stubdir/format-digest/$name" ]; then cat "$stubdir/format-digest/$name"
      else printf 'sha256:%s\n' "$(sha256sum "$f" | cut -d' ' -f1)"
      fi
    fi
    ;;
  copy)
    if [ -n "$src" ] && [ -n "$dst" ]; then
      mkdir -p "$SKOPEO_STATE/registry"
      pushed="$(dirname "$src")/.skopeo-stub/pushed/$(basename "$src").raw"
      [ -f "$pushed" ] && cp "$pushed" "$SKOPEO_STATE/registry/$key.raw"
    fi
    ;;
esac
exit 0
"""

def _sha256(data: bytes) -> str:
    return sha256_bytes(data)




def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _manifest(config: str, layer: str) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {"mediaType": "application/vnd.docker.container.image.v1+json", "size": 7, "digest": config},
            "layers": [{"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip", "size": 9, "digest": layer}],
        },
        separators=(",", ":"),
    ).encode()


def _config_of(name: str) -> str:
    return _digest(f"config-{name}".encode())


def _registry_key(ref: str) -> str:
    return ref.replace("/", "_").replace(":", "_").replace("@", "_")


def _stage_stub_images(artdir: Path, sha: str, oauth: bool = False) -> dict[str, str]:
    """Write per-archive stub manifests; return the MANIFEST digest per image.

    The archive manifest is what `skopeo inspect docker-archive:` reports.
    The pushed manifest keeps the same image config but compresses the layer,
    so its digest legitimately differs from the archive digest (the docker
    archive holds uncompressed layers; a registry stores compressed ones).
    """
    stub = artdir / ".skopeo-stub"
    for sub in ("archive", "pushed", "format-digest"):
        (stub / sub).mkdir(parents=True, exist_ok=True)
    tars = {
        "qdrant": "qdrant-image.tar",
        "jaeger": "jaeger-image.tar",
        "ingest": f"app-ingest-{sha}.tar",
        "agent": f"app-agent-{sha}.tar",
    }
    if oauth:
        tars["oauth_proxy"] = "oauth-proxy-image.tar"
    digests = {}
    for image, tar in tars.items():
        archive = _manifest(_config_of(image), _digest(f"layer-{image}".encode()))
        (stub / "archive" / f"{tar}.raw").write_bytes(archive)
        (stub / "pushed" / f"{tar}.raw").write_bytes(
            _manifest(_config_of(image), _digest(f"layer-{image}-gz".encode()))
        )
        digests[image] = _digest(archive)
    return digests


def _make_artifacts(artdir: Path, sha: str = IMAGE_SHA, corrupt: bool = False, oauth: bool = False):
    artdir.mkdir(parents=True, exist_ok=True)
    digests = _stage_stub_images(artdir, sha, oauth)
    files = {
        **task_members(),
        "repo.bundle": b"bundle-content\n",
        "qdrant-image.tar": b"qdrant-tar\n",
        "jaeger-image.tar": b"jaeger-tar\n",
        f"app-ingest-{sha}.tar": b"ingest-tar\n",
        f"app-agent-{sha}.tar": b"agent-tar\n",
        "MANIFEST.txt": (
            f"sha: {sha}\ndate: 2026-09-05T00:00:00Z\n"
            f"qdrant_digest: {digests['qdrant']}\n"
            f"jaeger_digest: {digests['jaeger']}\n"
            f"ingest_digest: {digests['ingest']}\n"
            f"agent_digest: {digests['agent']}\n"
            + "".join(f"{k}_config_digest: {_config_of(k)}\n" for k in ("qdrant", "jaeger", "ingest", "agent"))
            + task_manifest()
        ).encode(),
        "sbom.json": b'{"images": []}\n',
    }
    if oauth:
        files["oauth-proxy-image.tar"] = b"oauth-tar\n"
        files["MANIFEST.txt"] += (
            f"oauth_proxy_digest: {digests['oauth_proxy']}\n"
            f"oauth_proxy_config_digest: {_config_of('oauth_proxy')}\n"
        ).encode()
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
    from tests.helpers_airgap import gen_sign_keypair

    gen_sign_keypair(artdir)
    sign_sums(artdir)


def _digest_of_archive(artdir: Path, tar: str) -> str:
    return _digest((artdir / ".skopeo-stub" / "archive" / f"{tar}.raw").read_bytes())


@pytest.fixture
def load_tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "load.sh"])
    copy_task_tools(tmp_path)
    skopeo_log = tmp_path / "skopeo-args.log"
    write_stub(tmp_path / "bin" / "skopeo", STUB_SKOPEO)
    write_git_identity_stub(tmp_path, IMAGE_SHA)
    return tmp_path, skopeo_log


def _run_load(tree, *extra_env, cwd=None):
    tmp_path, skopeo_log = tree
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "SKOPEO_LOG": str(skopeo_log),
        "SKOPEO_STATE": str(tmp_path / "skopeo-state"),
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
    }
    for k, v in extra_env:
        env[k] = v
    return run_sh(tmp_path / "scripts" / "airgap" / "load.sh", env, cwd or tmp_path)


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
    copy_task_tools(subdir)
    (subdir / "scripts" / "airgap").mkdir(parents=True)
    for f in ("common.sh", "load.sh", "image_identity.sh", "image_manifest.py"):
        shutil.copy(REPO / "scripts" / "airgap" / f, subdir / "scripts" / "airgap" / f)
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "SKOPEO_LOG": str(skopeo_log),
        "SKOPEO_STATE": str(tmp_path / "skopeo-state"),
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
    manifest.write_text(manifest.read_text().replace(_digest_of_archive(artdir, "qdrant-image.tar"), "sha256:" + "c" * 64, 1))
    sums = []
    for line in (artdir / "SHA256SUMS").read_text().splitlines():
        name = line.split("  ", 1)[1]
        sums.append(f"{_sha256((artdir / name).read_bytes())}  {name}\n")
    (artdir / "SHA256SUMS").write_text("".join(sums))
    sign_sums(artdir)
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert "does not match MANIFEST" in r.stderr


def _edit_manifest_and_resign(artdir: Path, edit) -> None:
    manifest = artdir / "MANIFEST.txt"
    manifest.write_text(edit(manifest.read_text()))
    sums = []
    for line in (artdir / "SHA256SUMS").read_text().splitlines():
        name = line.split("  ", 1)[1]
        sums.append(f"{_sha256((artdir / name).read_bytes())}  {name}\n")
    (artdir / "SHA256SUMS").write_text("".join(sums))
    sign_sums(artdir)


def test_load_refuses_manifest_without_config_digest_before_any_push(load_tree):
    tmp_path, skopeo_log = load_tree
    artdir = tmp_path / "dist"
    _make_artifacts(artdir, sha=IMAGE_SHA)
    _edit_manifest_and_resign(
        artdir, lambda t: "".join(ln + "\n" for ln in t.splitlines() if not ln.startswith("agent_config_digest:"))
    )
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert "no agent_config_digest entry" in r.stderr
    assert "copy" not in skopeo_log.read_text().split()


def test_load_refuses_manifest_config_digest_that_is_not_the_archives(load_tree):
    tmp_path, skopeo_log = load_tree
    artdir = tmp_path / "dist"
    _make_artifacts(artdir, sha=IMAGE_SHA)
    _edit_manifest_and_resign(
        artdir, lambda t: t.replace(_config_of("qdrant"), "sha256:" + "d" * 64, 1)
    )
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert "image config does not match MANIFEST qdrant_config_digest" in r.stderr
    assert "copy" not in skopeo_log.read_text().split()


def test_load_trusted_pub_mismatch_refuses(load_tree):
    tmp_path, _ = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    other_pub = gen_other_pub(tmp_path)
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


def test_load_signed_missing_task_member_refuses_before_push(load_tree):
    root, log = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    sums = artdir / "SHA256SUMS"
    sums.write_text("".join(line for line in sums.read_text().splitlines(keepends=True) if "task_linux_amd64.tar.gz" not in line))
    (artdir / "task_linux_amd64.tar.gz").unlink()
    sign_sums(artdir)
    result = _run_load(load_tree)
    assert result.returncode != 0
    assert "Task member missing" in result.stderr
    assert not log.exists()


def test_load_ignores_retained_optional_image_outside_current_signed_inventory(load_tree):
    root, log = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    retained = artdir / "oauth-proxy-image.tar"
    retained.write_bytes(b"retained from prior approved bundle")
    result = _run_load(load_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Loaded 4 images" in result.stdout
    assert "oauth-proxy" not in log.read_text()
    assert retained.read_bytes() == b"retained from prior approved bundle"


# --- Post-load image identity (issue #272, item 3) -------------------------
# After each push, load must read the registry tag back and prove that the
# image the cluster will pull has the packed archive's config (image ID), not
# merely that `skopeo copy` exited 0.

REGISTRY = "reg.internal:5000"
REF = {
    "qdrant": f"{REGISTRY}/qdrant/qdrant:v1.19.0-unprivileged",
    "jaeger": f"{REGISTRY}/jaegertracing/jaeger:v2.20.0",
    "ingest": f"{REGISTRY}/qdrant-pdf-rag-ingest:{IMAGE_SHA}",
    "agent": f"{REGISTRY}/qdrant-pdf-rag-agent:{IMAGE_SHA}",
}
TAR = {
    "qdrant": "qdrant-image.tar",
    "jaeger": "jaeger-image.tar",
    "ingest": f"app-ingest-{IMAGE_SHA}.tar",
    "agent": f"app-agent-{IMAGE_SHA}.tar",
}


def _registry_manifest(root: Path, image: str) -> bytes:
    return (root / "skopeo-state" / "registry" / f"{_registry_key(REF[image])}.raw").read_bytes()


def _set_pushed(artdir: Path, image: str, raw: bytes) -> None:
    (artdir / ".skopeo-stub" / "pushed" / f"{TAR[image]}.raw").write_bytes(raw)


def _registry_calls(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if line.startswith("inspect") and "docker://" in line]


def test_load_success_proves_registry_serves_packed_config(load_tree):
    root, log = load_tree
    _make_artifacts(root / "dist")
    r = _run_load(load_tree)
    assert r.returncode == 0, r.stderr
    for image, ref in REF.items():
        registry_raw = _registry_manifest(root, image)
        archive_raw = (root / "dist/.skopeo-stub/archive" / f"{TAR[image]}.raw").read_bytes()
        # The stored bytes are the packed image's config; the compressed
        # registry manifest has its own digest, which load reports as the pin.
        assert json.loads(registry_raw)["config"] == json.loads(archive_raw)["config"]
        assert _digest(registry_raw) != _digest(archive_raw)
        assert f"{ref}@{_digest(registry_raw)}" in r.stdout
    assert len(_registry_calls(log)) == 4


@pytest.mark.parametrize("image", ["qdrant", "jaeger", "ingest", "agent"])
def test_load_refuses_registry_tag_serving_different_image(load_tree, image):
    root, _ = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    # skopeo copy exits 0 but the tag resolves to another image's config
    # (swapped/mirrored/immutable-policy registry): identity must not be assumed.
    _set_pushed(artdir, image, _manifest(_config_of("swapped"), _digest(b"layer-swapped")))
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert f"registry image {REF[image]} is not the packed image" in r.stderr
    assert "Loaded" not in r.stdout


def test_load_refuse_then_fix_next_run_passes(load_tree):
    root, _ = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    good = (artdir / ".skopeo-stub/pushed" / f"{TAR['agent']}.raw").read_bytes()
    _set_pushed(artdir, "agent", _manifest(_config_of("swapped"), _digest(b"layer-swapped")))
    first = _run_load(load_tree)
    assert first.returncode == 1
    assert "is not the packed image" in first.stderr
    # The bad content really is what the registry holds after the refused run.
    assert json.loads(_registry_manifest(root, "agent"))["config"]["digest"] == _config_of("swapped")
    # Operator fixes the registry/mirror; the next ordinary run succeeds and
    # the registry now holds exactly the packed image.
    _set_pushed(artdir, "agent", good)
    second = _run_load(load_tree)
    assert second.returncode == 0, second.stderr
    assert "Loaded 4 images into reg.internal:5000" in second.stdout
    assert json.loads(_registry_manifest(root, "agent"))["config"]["digest"] == _config_of("agent")


def test_load_refuses_registry_manifest_list_for_tag(load_tree):
    root, _ = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    index = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []})
    _set_pushed(artdir, "ingest", index.encode())
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert f"registry image {REF['ingest']} is not a single-image manifest" in r.stderr


def test_load_refuses_unreadable_registry_after_push(load_tree):
    root, _ = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    # Push "succeeds" but nothing is stored: the stub only stores when a
    # pushed manifest exists.
    (artdir / ".skopeo-stub/pushed" / f"{TAR['qdrant']}.raw").unlink()
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert f"cannot read back registry image {REF['qdrant']}" in r.stderr
    assert "Loaded" not in r.stdout


def test_load_refuses_archive_changed_after_digest_binding(load_tree):
    root, _ = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    # `inspect --format` still reports the MANIFEST digest (as it did at the
    # binding step) but the archive's manifest bytes no longer hash to it.
    manifest_digest = _digest_of_archive(artdir, TAR["agent"])
    (artdir / ".skopeo-stub/format-digest" / TAR["agent"]).write_text(manifest_digest + "\n")
    (artdir / ".skopeo-stub/archive" / f"{TAR['agent']}.raw").write_bytes(
        _manifest(_config_of("agent"), _digest(b"layer-tampered"))
    )
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert f"archive for {REF['agent']} no longer matches its MANIFEST digest" in r.stderr


def test_load_registry_read_back_follows_skopeo_args_and_insecure_flag(load_tree):
    root, log = load_tree
    _make_artifacts(root / "dist")
    r = _run_load(
        load_tree,
        ("SKOPEO_ARGS", "--dest-cert-dir /certs --authfile /a.json --format v2s2 --src-tls-verify=false"),
        ("INSECURE_REGISTRY", "true"),
    )
    assert r.returncode == 0, r.stderr
    calls = _registry_calls(log)
    assert len(calls) == 4
    for call in calls:
        assert "--cert-dir /certs" in call
        assert "--authfile /a.json" in call
        assert "--tls-verify=false" in call
        # Copy-only options never reach `skopeo inspect`.
        assert "--dest-" not in call and "--format v2s2" not in call and "--src-" not in call


def test_load_dryrun_is_hermetic_and_not_release_verified(load_tree):
    root, log = load_tree
    r = _run_load(load_tree, ("AIRGAP_DRYRUN", "1"))
    assert r.returncode == 0, r.stderr
    assert "Loaded 4 images into reg.internal:5000 (dry-run)" in r.stdout
    assert "registry image identity not release-verified" in r.stdout
    assert not log.exists()
    assert not (root / "skopeo-state").exists()


def test_load_verifies_optional_oauth_proxy_image_like_the_others(load_tree):
    root, log = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir, oauth=True)
    ref = f"{REGISTRY}/openshift4/ose-oauth-proxy:v4.14"
    first = _run_load(load_tree)
    assert first.returncode == 0, first.stderr
    assert "Loaded 5 images" in first.stdout
    assert len(_registry_calls(log)) == 5
    assert f"verified {ref}@sha256:" in first.stdout
    (artdir / ".skopeo-stub/pushed/oauth-proxy-image.tar.raw").write_bytes(
        _manifest(_config_of("swapped"), _digest(b"layer-swapped"))
    )
    second = _run_load(load_tree)
    assert second.returncode == 1
    assert f"registry image {ref} is not the packed image" in second.stderr


@pytest.mark.parametrize("malformed", ["layers-object", "config-type", "layer-digest", "index"])
def test_load_registry_structure_uses_same_validation_as_deploy(load_tree, malformed):
    root, _ = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    manifest = json.loads(_manifest(_config_of("agent"), _digest(b"layer-agent-gz")))
    if malformed == "layers-object":
        manifest["layers"] = {"digest": "sha256:" + "a" * 64}
    elif malformed == "config-type":
        manifest["config"]["digest"] = 42
    elif malformed == "layer-digest":
        manifest["layers"][0]["digest"] = "not-a-digest"
    else:
        manifest["manifests"] = []
    _set_pushed(artdir, "agent", json.dumps(manifest).encode())
    result = _run_load(load_tree)
    assert result.returncode != 0
    assert "not a single-image manifest" in result.stderr
    assert "Loaded" not in result.stdout


def test_load_preserves_layer_count_requirement(load_tree):
    root, _ = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    manifest = json.loads(_manifest(_config_of("agent"), _digest(b"layer-agent-gz")))
    manifest["layers"].append({"digest": _digest(b"extra-layer")})
    _set_pushed(artdir, "agent", json.dumps(manifest).encode())
    result = _run_load(load_tree)
    assert result.returncode != 0
    assert "is not the packed image" in result.stderr
