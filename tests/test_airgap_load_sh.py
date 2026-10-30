"""scripts/airgap/load.sh fail-close and operability tests (issue #15).

Hermetic tests: tests artifact discovery (dist, parent, AIRGAP_BUNDLE_DIR),
checksum verification, MANIFEST sha validation, skopeo copy invocations,
and SKOPEO_ARGS / INSECURE_REGISTRY flag propagation without network or live registries.
"""

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from scripts.airgap import image_manifest

from tests.helpers_airgap import (
    REPO,
    gen_other_pub,
    make_bin_tree,
    release_anchor,
    release_key,
    run_sh,
    sha256_bytes,
    sign_sums,
    write_git_identity_stub,
    write_stub,
)
from tests.helpers_image_inventory import (
    DOCKER_MANIFEST,
    oci_archive,
    tar_bytes,
    write_docker_archive,
)
from tests.helpers_task_artifact import copy_task_tools, task_manifest, task_members

IMAGE_SHA = "b" * 40

# skopeo stub with real registry semantics for identity checks. Archives are
# real oci-archive layouts that load.sh reads itself; `copy` "pushes" the
# matching .skopeo-stub/pushed/ manifest into the stub registry
# ($SKOPEO_STATE/registry/), and `inspect docker://` reads it back. A
# .skopeo-stub/swap/<member> file replaces the archive at copy time (the
# bytes skopeo actually reads) and pushes that image's manifest instead.
# Load never needs `skopeo inspect` of an archive: any such call fails.
# Every call is logged on one line.
STUB_SKOPEO = r"""#!/bin/sh
printf '%s\n' "$*" >> "$SKOPEO_LOG"
src=""; dst=""
for a in "$@"; do
  case "$a" in
    oci-archive:*) src="${a#oci-archive:}" ;;
    docker-archive:*) echo "stub: docker-archive transport is not used by load" >&2; exit 1 ;;
    docker://*) dst="${a#docker://}" ;;
  esac
done
key=$(printf '%s' "$dst" | tr '/:@' '___')
case "$1" in
  inspect)
    [ -n "$dst" ] || { echo "stub: load must not inspect an archive" >&2; exit 1; }
    f="$SKOPEO_STATE/registry/$key.raw"
    [ -f "$f" ] || { echo "manifest unknown" >&2; exit 1; }
    cat "$f"
    ;;
  copy)
    if [ -n "$src" ] && [ -n "$dst" ]; then
      mkdir -p "$SKOPEO_STATE/registry"
      stubdir="$(dirname "$src")/.skopeo-stub"
      name=$(basename "$src")
      if [ -f "$stubdir/swap/$name" ]; then
        cp "$stubdir/swap/$name" "$src"
        cp "$stubdir/swap/$name.raw" "$SKOPEO_STATE/registry/$key.raw"
      elif [ -f "$stubdir/pushed/$name.raw" ]; then
        cp "$stubdir/pushed/$name.raw" "$SKOPEO_STATE/registry/$key.raw"
      fi
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
    """A registry-side manifest (sizes are not identity; config and layers are)."""
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


def _write_member(path: Path, image: str) -> bytes:
    """A real oci-archive member: config blob `config-<image>`, one layer blob."""
    return oci_archive(path, f"config-{image}".encode(), [f"layer-{image}".encode()],
                       manifest_type=DOCKER_MANIFEST, compressed=True)


def _stage_stub_images(artdir: Path, sha: str, oauth: bool = False,
                       openwebui: bool = False) -> dict[str, str]:
    """Write each image member as an oci-archive; return the MANIFEST digest per image.

    The archive keeps the registry's manifest bytes (pack copies with
    --preserve-digests), so by default the stub registry stores exactly the
    archive's manifest after a push.
    """
    stub = artdir / ".skopeo-stub"
    for sub in ("pushed", "swap"):
        (stub / sub).mkdir(parents=True, exist_ok=True)
    tars = {
        "qdrant": "qdrant-image.tar",
        "openobserve": "openobserve-image.tar",
        "otelcol": "otelcol-image.tar",
        "ingest": f"app-ingest-{sha}.tar",
        "agent": f"app-agent-{sha}.tar",
    }
    if oauth:
        tars["oauth_proxy"] = "oauth-proxy-image.tar"
    if openwebui:
        tars["openwebui"] = "openwebui-image.tar"
        tars["openwebui_proxy"] = "openwebui-proxy-image.tar"  # ADR-0007 E1: rides with it
    digests = {}
    for image, tar in tars.items():
        raw = _write_member(artdir / tar, image)
        (stub / "pushed" / f"{tar}.raw").write_bytes(raw)
        digests[image] = _digest(raw)
    return digests


IMAGE_MEMBERS = ("qdrant-image.tar", "openobserve-image.tar", "otelcol-image.tar", "oauth-proxy-image.tar",
                 "openwebui-image.tar", "openwebui-proxy-image.tar")


def _make_artifacts(artdir: Path, sha: str = IMAGE_SHA, corrupt: bool = False, oauth: bool = False,
                    openwebui: bool = False):
    artdir.mkdir(parents=True, exist_ok=True)
    digests = _stage_stub_images(artdir, sha, oauth, openwebui)
    files = {
        **task_members(),
        "repo.bundle": b"bundle-content\n",
        "MANIFEST.txt": (
            f"sha: {sha}\ndate: 2026-09-05T00:00:00Z\nimage_format: oci-archive\n"
            f"qdrant_digest: {digests['qdrant']}\n"
            f"openobserve_digest: {digests['openobserve']}\n"
            f"otelcol_digest: {digests['otelcol']}\n"
            f"ingest_digest: {digests['ingest']}\n"
            f"agent_digest: {digests['agent']}\n"
            + "".join(f"{k}_config_digest: {_config_of(k)}\n" for k in ("qdrant", "openobserve", "otelcol", "ingest", "agent"))
            + task_manifest()
        ).encode(),
        "sbom.json": b'{"images": []}\n',
    }
    if oauth:
        files["MANIFEST.txt"] += (
            f"oauth_proxy_digest: {digests['oauth_proxy']}\n"
            f"oauth_proxy_config_digest: {_config_of('oauth_proxy')}\n"
        ).encode()
    if openwebui:
        files["MANIFEST.txt"] += (
            f"openwebui_digest: {digests['openwebui']}\n"
            f"openwebui_config_digest: {_config_of('openwebui')}\n"
            f"openwebui_proxy_digest: {digests['openwebui_proxy']}\n"
            f"openwebui_proxy_config_digest: {_config_of('openwebui_proxy')}\n"
        ).encode()
    for name, content in files.items():
        (artdir / name).write_bytes(content)
    members = [*files, *(name for name in sorted(p.name for p in artdir.glob("*.tar"))
                         if name in IMAGE_MEMBERS or name.startswith("app-"))]
    sums = []
    for name in members:
        content = (artdir / name).read_bytes()
        digest = _sha256(b"corrupt\n" if corrupt and name == "qdrant-image.tar" else content)
        sums.append(f"{digest}  {name}\n")
    (artdir / "SHA256SUMS").write_text("".join(sums))
    _sign_artifacts(artdir)


def _sign_artifacts(artdir: Path) -> None:
    """The throwaway release key + offline signature, mirroring pack.sh output.
    One key per process, so release_anchor() is its trust anchor (#1118 F1)."""
    shutil.copy(release_key(), artdir / "signing.key")
    sign_sums(artdir)


def _archive_manifest(artdir: Path, tar: str) -> bytes:
    with tarfile.open(artdir / tar) as archive:
        return image_manifest.read_archive(archive).manifest_raw


def _digest_of_archive(artdir: Path, tar: str) -> str:
    return _digest(_archive_manifest(artdir, tar))


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
        # The release trust anchor (#1118 F1): every fixture bundle is signed
        # by release_key(), so a load is a verified signed release.
        "SNEAKERNET_TRUSTED_PUB": str(release_anchor()),
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


def test_load_succeeds_from_the_parent_dir_and_an_explicit_bundle_dir(load_tree):
    """The bundle may sit next to the clone (../dist) or where AIRGAP_BUNDLE_DIR
    says; both load the same signed contents."""
    tmp_path, skopeo_log = load_tree
    _make_artifacts(tmp_path, sha=IMAGE_SHA)
    subdir = tmp_path / "clone-dir"
    subdir.mkdir()
    copy_task_tools(subdir)
    (subdir / "scripts" / "airgap").mkdir(parents=True)
    for f in ("common.sh", "load.sh", "image_identity.sh", "image_manifest.py", "model_config.py"):
        shutil.copy(REPO / "scripts" / "airgap" / f, subdir / "scripts" / "airgap" / f)
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "SKOPEO_LOG": str(skopeo_log),
           "SKOPEO_STATE": str(tmp_path / "skopeo-state"), "IMAGE_SHA": IMAGE_SHA,
           "INTERNAL_REGISTRY": "reg.internal:5000", "SNEAKERNET_TRUSTED_PUB": str(release_anchor())}
    r = subprocess.run(["sh", str(subdir / "scripts" / "airgap" / "load.sh")], capture_output=True,
                       text=True, env=env, cwd=subdir, check=False)
    assert r.returncode == 0, r.stderr
    assert "Loaded 5 images into reg.internal:5000" in r.stdout
    assert skopeo_log.exists()
    custom_dir = tmp_path / "custom-bundle-location"
    _make_artifacts(custom_dir, sha=IMAGE_SHA)
    custom = _run_load(load_tree, ("AIRGAP_BUNDLE_DIR", str(custom_dir)))
    assert custom.returncode == 0, custom.stderr
    assert "Loaded 5 images into reg.internal:5000" in custom.stdout


def test_load_tampered_sums_fails_signature(load_tree):
    tmp_path, _ = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    with open(tmp_path / "dist" / "SHA256SUMS", "a") as f:
        f.write(f"{'0' * 64}  injected\n")
    r = _run_load(load_tree)
    assert r.returncode != 0
    assert "signature verification failed" in r.stderr


@pytest.mark.parametrize(("selection", "member", "key"), [
    ({}, "qdrant-image.tar", "qdrant_digest"),
    ({"openwebui": True}, "openwebui-image.tar", "openwebui_digest"),
], ids=["first-party", "openwebui"])
def test_load_image_digest_mismatch_fails_closed(load_tree, selection, member, key):
    """A validly-signed bundle with a lying digest: only the binding can catch it."""
    tmp_path, log = load_tree
    artdir = tmp_path / "dist"
    _make_artifacts(artdir, sha=IMAGE_SHA, **selection)
    manifest = artdir / "MANIFEST.txt"
    manifest.write_text(manifest.read_text().replace(
        _digest_of_archive(artdir, member), "sha256:" + "c" * 64, 1))
    _resign(artdir)
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert "does not match MANIFEST" in r.stderr and key in r.stderr
    assert not _copies(log)


def _copies(log: Path) -> list[str]:
    """skopeo copy calls so far; load verifies archives itself, so a refusal
    before any push may leave no skopeo call at all."""
    return [line for line in log.read_text().splitlines() if line.startswith("copy")] if log.exists() else []


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
    assert not _copies(skopeo_log)


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
    assert not _copies(skopeo_log)


# --- OCI archive members (#1066) --------------------------------------------
# A validly signed bundle (pack bug or key holder) must still not push bytes
# that differ from the archive's own descriptors or from the MANIFEST, and a
# bundle of another format is refused as a whole before any push.


def _resign(artdir: Path) -> None:
    _edit_manifest_and_resign(artdir, lambda text: text)


def _rewrite_member(path: Path, edit) -> None:
    with tarfile.open(path) as archive:
        members = {item.name: (archive.extractfile(item).read() if item.isfile() else None) for item in archive}
    path.write_bytes(tar_bytes(edit(members)))


def _rewrite_manifest(path: Path, edit) -> None:
    """Rewrite the image manifest and rebind its size/digest in index.json."""
    with tarfile.open(path) as archive:
        members = {item.name: (archive.extractfile(item).read() if item.isfile() else None) for item in archive}
    index = json.loads(members["index.json"])
    entry = index["manifests"][0]
    old = f"blobs/sha256/{entry['digest'][7:]}"
    manifest = json.loads(members[old])
    edit(manifest)
    raw = json.dumps(manifest, separators=(",", ":")).encode()
    new = f"blobs/sha256/{_digest(raw)[7:]}"
    entry["digest"] = _digest(raw)
    entry["size"] = len(raw)
    members.pop(old)
    members[new] = raw
    members["index.json"] = json.dumps(index, separators=(",", ":")).encode()
    path.write_bytes(tar_bytes(members))


@pytest.mark.parametrize("blob", ["layer", "config", "manifest"])
def test_load_refuses_tampered_oci_blob_before_any_push(load_tree, blob):
    tmp_path, skopeo_log = load_tree
    artdir = tmp_path / "dist"
    _make_artifacts(artdir)
    manifest = json.loads(_archive_manifest(artdir, TAR["otelcol"]))
    target = {"layer": manifest["layers"][0]["digest"], "config": manifest["config"]["digest"],
              "manifest": _digest_of_archive(artdir, TAR["otelcol"])}[blob]
    name = f"blobs/sha256/{target[7:]}"

    def flip(members):
        data = bytearray(members[name])
        data[0] ^= 1  # same size, different bytes
        return {**members, name: bytes(data)}

    _rewrite_member(artdir / TAR["otelcol"], flip)
    _resign(artdir)
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert "otelcol-image.tar: an image blob is missing or does not match its digest" in r.stderr
    assert not _copies(skopeo_log)


def test_load_refuses_consistent_archive_of_another_image(load_tree):
    tmp_path, skopeo_log = load_tree
    artdir = tmp_path / "dist"
    _make_artifacts(artdir)
    # Internally consistent, correctly checksummed and signed, but not the image
    # the MANIFEST binds: only the digest binding can catch it.
    other = _digest(_write_member(artdir / TAR["qdrant"], "someone-else"))
    _resign(artdir)
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert f"qdrant-image.tar digest {other} does not match MANIFEST qdrant_digest" in r.stderr
    assert not _copies(skopeo_log)


@pytest.mark.parametrize("declared", [None, "docker-archive"])
def test_load_refuses_docker_archive_bundle_then_loads_the_next_oci_bundle(load_tree, declared):
    """Rollback decision (#1066): a pre-#1066 docker-archive bundle is refused with
    a fixed message before any registry call; its own release loads it."""
    tmp_path, skopeo_log = load_tree
    artdir = tmp_path / "dist"
    _make_artifacts(artdir)
    for tar in TAR.values():
        write_docker_archive(artdir / tar, [{"etc/legacy": b"legacy"}])
    _edit_manifest_and_resign(
        artdir,
        lambda t: t.replace("image_format: oci-archive\n", "" if declared is None else f"image_format: {declared}\n"),
    )
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert ("FAIL: bundle image format is not oci-archive (a docker-archive bundle from an older release, "
            "or an unknown format) — load an older approved release with that release's own bootstrap.sh "
            "and load.sh in a separate workspace; nothing was pushed") in r.stderr
    assert not skopeo_log.exists()
    # The next ordinary operation: the current release's bundle loads.
    shutil.rmtree(artdir)
    _make_artifacts(artdir)
    again = _run_load(load_tree)
    assert again.returncode == 0, again.stderr
    assert "Loaded 5 images into reg.internal:5000" in again.stdout


def _two_images(members):
    index = json.loads(members["index.json"])
    index["manifests"].append(dict(index["manifests"][0]))
    return {**members, "index.json": json.dumps(index).encode()}


@pytest.mark.parametrize(
    "shape",
    [
        "docker-archive member",
        "two-image index",
        "shadowed blob",
        "index and manifest media types differ",
        "config media type differs",
        "layer media type differs",
        "docker manifest with OCI layer type",
        "OCI index entry for a docker manifest",
        "symlink member",
        "hardlink member",
    ],
)
def test_load_refuses_member_that_is_not_one_oci_image_before_any_push(load_tree, shape):
    tmp_path, skopeo_log = load_tree
    artdir = tmp_path / "dist"
    _make_artifacts(artdir)
    member = artdir / TAR["otelcol"]
    if shape == "docker-archive member":
        # MANIFEST declares oci-archive: one member of another format is a mixed bundle.
        write_docker_archive(member, [{"etc/legacy": b"legacy"}])
    elif shape == "two-image index":
        _rewrite_member(member, _two_images)
    elif shape == "shadowed blob":
        # A second copy of a blob name would shadow the verified bytes for some readers.
        with tarfile.open(member) as archive:
            blob = next(item for item in archive if item.name.startswith("blobs/sha256/") and item.isfile())
            data = archive.extractfile(blob).read()
        with tarfile.open(member, "a") as archive:
            info = tarfile.TarInfo(blob.name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    elif shape == "index and manifest media types differ":
        # The reader binds the manifest body to the index entry's declared type.
        def flip(manifest):
            manifest["mediaType"] = "application/vnd.oci.image.manifest.v1+json"
        _rewrite_manifest(member, flip)
    elif shape == "config media type differs":
        def wrong_config(manifest):
            manifest["config"]["mediaType"] = "application/vnd.oci.image.config.v1+json"
        _rewrite_manifest(member, wrong_config)
    elif shape == "layer media type differs":
        def wrong_layer(manifest):
            manifest["layers"][0]["mediaType"] = "application/vnd.oci.image.layer.v1.tar+zstd"
        _rewrite_manifest(member, wrong_layer)
    elif shape == "docker manifest with OCI layer type":
        # A known layer type of the other family is still a family mismatch.
        def oci_layer(manifest):
            manifest["layers"][0]["mediaType"] = "application/vnd.oci.image.layer.v1.tar+gzip"
        _rewrite_manifest(member, oci_layer)
    elif shape == "OCI index entry for a docker manifest":
        # The docker-v2s2 body is unchanged, so its digest still matches the entry.
        def oci_entry(members):
            index = json.loads(members["index.json"])
            index["manifests"][0]["mediaType"] = "application/vnd.oci.image.manifest.v1+json"
            return {**members, "index.json": json.dumps(index).encode()}
        _rewrite_member(member, oci_entry)
    elif shape == "symlink member":
        with tarfile.open(member, "a") as archive:
            info = tarfile.TarInfo("unused-symlink")
            info.type = tarfile.SYMTYPE
            info.linkname = "index.json"
            archive.addfile(info)
    else:
        with tarfile.open(member, "a") as archive:
            info = tarfile.TarInfo("unused-hardlink")
            info.type = tarfile.LNKTYPE
            info.linkname = "oci-layout"
            archive.addfile(info)
    _resign(artdir)
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert "otelcol-image.tar is not a single-image oci-archive" in r.stderr
    assert not _copies(skopeo_log)


def test_load_trusted_pub_mismatch_refuses(load_tree):
    tmp_path, _ = load_tree
    _make_artifacts(tmp_path / "dist", sha=IMAGE_SHA)
    pinned = _run_load(load_tree, ("SNEAKERNET_TRUSTED_PUB", str(tmp_path / "dist" / "sneakernet-signing.pub")))
    assert pinned.returncode == 0, pinned.stderr
    other_pub = gen_other_pub(tmp_path)
    r = _run_load(load_tree, ("SNEAKERNET_TRUSTED_PUB", str(other_pub)))
    assert r.returncode != 0
    assert "does not match SNEAKERNET_TRUSTED_PUB" in r.stderr


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


# --- Post-load image identity (issue #272, item 3) -------------------------
# After each push, load must read the registry tag back and prove that the
# image the cluster will pull has the packed archive's config (image ID), not
# merely that `skopeo copy` exited 0.

REGISTRY = "reg.internal:5000"
REF = {
    "qdrant": f"{REGISTRY}/qdrant/qdrant:v1.19.2-unprivileged",
    "openobserve": f"{REGISTRY}/openobserve/openobserve:v1.1.2",
    "otelcol": f"{REGISTRY}/otel/opentelemetry-collector-contrib:0.160.0",
    "ingest": f"{REGISTRY}/qdrant-pdf-rag-ingest:{IMAGE_SHA}",
    "agent": f"{REGISTRY}/qdrant-pdf-rag-agent:{IMAGE_SHA}",
}
TAR = {
    "qdrant": "qdrant-image.tar",
    "openobserve": "openobserve-image.tar",
    "otelcol": "otelcol-image.tar",
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
    artdir = root / "dist"
    _make_artifacts(artdir)
    # A registry may re-serialize what it stores: the agent tag holds the same
    # config under another manifest digest, which load still accepts and pins.
    reserialized = _manifest(_config_of("agent"), _digest(b"layer-agent-recompressed"))
    _set_pushed(artdir, "agent", reserialized)
    r = _run_load(load_tree)
    assert r.returncode == 0, r.stderr
    assert "Loaded 5 images into reg.internal:5000" in r.stdout
    for image, ref in REF.items():
        registry_raw = _registry_manifest(root, image)
        archive_raw = _archive_manifest(artdir, TAR[image])
        assert json.loads(registry_raw)["config"]["digest"] == json.loads(archive_raw)["config"]["digest"]
        # The printed pin is always the registry's digest. The archive keeps the
        # registry's manifest bytes, so it equals the archive digest unless the
        # registry re-serialized the manifest.
        assert f"{ref}@{_digest(registry_raw)}" in r.stdout
        assert (_digest(registry_raw) == _digest(archive_raw)) is (image != "agent")
    assert len(_registry_calls(log)) == 5


def test_load_refuse_then_fix_next_run_passes(load_tree):
    root, _ = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    good = (artdir / ".skopeo-stub/pushed" / f"{TAR['agent']}.raw").read_bytes()
    _set_pushed(artdir, "agent", _manifest(_config_of("swapped"), _digest(b"layer-swapped")))
    first = _run_load(load_tree)
    assert first.returncode == 1
    assert f"registry image {REF['agent']} is not the packed image" in first.stderr
    assert "Loaded" not in first.stdout
    # The bad content really is what the registry holds after the refused run.
    assert json.loads(_registry_manifest(root, "agent"))["config"]["digest"] == _config_of("swapped")
    # Operator fixes the registry/mirror; the next ordinary run succeeds and
    # the registry now holds exactly the packed image.
    _set_pushed(artdir, "agent", good)
    second = _run_load(load_tree)
    assert second.returncode == 0, second.stderr
    assert "Loaded 5 images into reg.internal:5000" in second.stdout
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


def test_load_refuses_archive_swapped_after_digest_binding(load_tree):
    root, _ = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir)
    # The archive verifies before any push, then another image replaces it
    # before skopeo reads it: the registry now serves that image, and the
    # read-back compares it with the manifest that was verified, not the file.
    other = artdir / ".skopeo-stub/swap" / TAR["agent"]
    (artdir / ".skopeo-stub/swap" / f"{TAR['agent']}.raw").write_bytes(_write_member(other, "someone-else"))
    r = _run_load(load_tree)
    assert r.returncode == 1
    assert f"registry image {REF['agent']} is not the packed image" in r.stderr
    assert "Loaded" not in r.stdout


def test_load_registry_read_back_follows_skopeo_args_and_insecure_flag(load_tree):
    root, log = load_tree
    _make_artifacts(root / "dist")
    r = _run_load(
        load_tree,
        ("SKOPEO_ARGS", "--dest-cert-dir /certs --authfile /a.json --format v2s2 --src-tls-verify=false"),
        ("INSECURE_REGISTRY", "true"),
    )
    assert r.returncode == 0, r.stderr
    copies = [line for line in log.read_text().splitlines() if line.startswith("copy")]
    assert len(copies) == 5
    for copy in copies:
        assert "--authfile /a.json" in copy and "--dest-tls-verify=false" in copy
    calls = _registry_calls(log)
    assert len(calls) == 5
    for call in calls:
        assert "--cert-dir /certs" in call
        assert "--authfile /a.json" in call
        assert "--tls-verify=false" in call
        # Copy-only options never reach `skopeo inspect`.
        assert "--dest-" not in call and "--format v2s2" not in call and "--src-" not in call


def test_load_passes_configured_tmpdir_to_copies_only(load_tree):
    root, log = load_tree
    _make_artifacts(root / "dist")
    tmpdir = root / "work-tmp"
    tmpdir.mkdir()
    r = _run_load(load_tree, ("SKOPEO_ARGS", f"--tmpdir {tmpdir}"))
    assert r.returncode == 0, r.stderr
    copies = [line for line in log.read_text().splitlines() if line.startswith("copy")]
    assert len(copies) == 5
    for copy in copies:
        assert f"--tmpdir {tmpdir}" in copy
    for call in _registry_calls(log):
        assert "--tmpdir" not in call


@pytest.mark.parametrize(("missing_dir", "needle"), [
    (True, "skopeo temporary directory from SKOPEO_ARGS --tmpdir"),
    (False, "less than 0.3 GB (or the largest image member) free"),
], ids=["missing-dir", "no-space"])
def test_load_refuses_a_skopeo_tmpdir_before_any_push(load_tree, missing_dir, needle):
    root, log = load_tree
    _make_artifacts(root / "dist")
    tmpdir = root / "work-tmp"
    if missing_dir:
        tmpdir = root / "no-such-tmp"
    else:
        tmpdir.mkdir()
        write_stub(root / "bin" / "df", "#!/bin/sh\n"
                   "printf '%s\\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
                   "printf '%s\\n' 'stub 1000 900 100 90% /'\n")
    r = _run_load(load_tree, ("SKOPEO_ARGS", f"--tmpdir {tmpdir}"))
    assert r.returncode == 1
    assert needle in r.stderr
    if missing_dir:
        assert "nothing was pushed" in r.stderr
    assert not log.exists()


def test_load_counts_optional_openwebui_member_in_skopeo_tmpdir_space(load_tree):
    """#1074 + #1068: the Open WebUI member can be the largest, so the preflight
    must size it too and refuse before any push when the tmpdir cannot hold it."""
    root, log = load_tree
    _make_artifacts(root / "dist", openwebui=True)
    tmpdir = root / "work-tmp"
    tmpdir.mkdir()
    write_stub(
        root / "bin" / "df",
        "#!/bin/sh\n"
        "printf '%s\\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        "printf '%s\\n' 'stub 1000000 600000 400000 60% /'\n",
    )
    # The archive stays a real oci-archive; only its reported size is large, so
    # this isolates the sizing loop from a multi-GB fixture.
    write_stub(
        root / "bin" / "wc",
        '#!/bin/sh\n'
        'if [ "$1" = "-c" ]; then\n'
        '  target=$(readlink /proc/self/fd/0)\n'
        '  case "$target" in *openwebui-image.tar) echo 999999999; exit 0 ;; esac\n'
        'fi\n'
        'exec /usr/bin/wc "$@"\n',
    )
    r = _run_load(load_tree, ("SKOPEO_ARGS", f"--tmpdir={tmpdir}"))
    assert r.returncode == 1
    assert "less than 0.3 GB (or the largest image member) free" in r.stderr
    assert not log.exists()


def test_load_dryrun_is_hermetic_and_not_release_verified(load_tree):
    root, log = load_tree
    r = _run_load(load_tree, ("AIRGAP_DRYRUN", "1"))
    assert r.returncode == 0, r.stderr
    assert "Loaded 5 images into reg.internal:5000 (dry-run)" in r.stdout
    assert "registry image identity not release-verified" in r.stdout
    assert not log.exists()
    assert not (root / "skopeo-state").exists()


OPENWEBUI_REF = f"{REGISTRY}/qdrant-pdf-rag-openwebui:v0.12.3-slim"
OPENWEBUI_PROXY_REF = f"{REGISTRY}/nginxinc/nginx-unprivileged:1.30.6-alpine"
OAUTH_REF = f"{REGISTRY}/openshift4/ose-oauth-proxy:v4.14"


@pytest.mark.parametrize(("selection", "member", "ref", "count"), [
    ({"oauth": True}, "oauth-proxy-image.tar", OAUTH_REF, 6),
    ({"openwebui": True}, "openwebui-image.tar", OPENWEBUI_REF, 7),
    ({"openwebui": True}, "openwebui-proxy-image.tar", OPENWEBUI_PROXY_REF, 7),
], ids=["oauth-proxy", "openwebui", "openwebui-proxy"])
def test_load_verifies_optional_signed_member_like_the_others(load_tree, selection, member, ref, count):
    """ADR-0007 (#1074): every optional signed member is digest-checked, pushed to
    the tag the chart renders and read back; a swapped registry image refuses."""
    root, log = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir, **selection)
    first = _run_load(load_tree)
    assert first.returncode == 0, first.stderr
    assert f"Loaded {count} images" in first.stdout
    assert len(_registry_calls(log)) == count
    assert f"copy oci-archive:{artdir}/{member} docker://{ref}" in log.read_text()
    assert f"verified {ref}@sha256:" in first.stdout
    (artdir / ".skopeo-stub/pushed" / f"{member}.raw").write_bytes(
        _manifest(_config_of("swapped"), _digest(b"layer-swapped")))
    second = _run_load(load_tree)
    assert second.returncode == 1
    assert f"registry image {ref} is not the packed image" in second.stderr


@pytest.mark.parametrize(("selection", "member", "ref"), [
    ({}, "oauth-proxy-image.tar", OAUTH_REF),
    ({}, "openwebui-image.tar", OPENWEBUI_REF),
], ids=["retained-oauth-proxy", "retained-openwebui"])
def test_load_ignores_an_unsigned_member_but_dryrun_names_it(load_tree, selection, member, ref):
    """A retained archive that the current SHA256SUMS does not list is not part of
    the signed inventory: it is neither pushed nor touched, and the dry-run names
    the file's tag so the operator can see what a real run would push."""
    root, log = load_tree
    artdir = root / "dist"
    _make_artifacts(artdir, **selection)
    retained = artdir / member
    retained.write_bytes(b"retained from prior approved bundle")
    result = _run_load(load_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Loaded 5 images" in result.stdout and ref not in log.read_text()
    assert retained.read_bytes() == b"retained from prior approved bundle"
    (artdir / "SHA256SUMS").unlink()
    dry = _run_load(load_tree, ("AIRGAP_DRYRUN", "1"))
    assert dry.returncode == 0, dry.stderr
    assert f"oci-archive:{artdir}/{member} docker://{ref}" in dry.stdout
    assert "Loaded 6 images" in dry.stdout


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
