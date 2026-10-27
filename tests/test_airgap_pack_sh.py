"""scripts/airgap/pack.sh factory tests (issue #15).

Hermetic tests: pack.sh against a stubbed skopeo and a real throwaway git
repo — no GHCR, no network. Covers the fail-closed gates (SHA==HEAD, git
repo present, skopeo present, signing key present) and the happy path:
bundle + oci-archive image members + MANIFEST (with image digests) + sbom.json +
offline signature + member checksums + tarball digest, with the bundle
clone-traversable (the airgap-package CI check, hermetically).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile

import pytest

from tests.helpers_airgap import (
    REPO,
    copy_chart,
    copy_license_inputs,
    gen_sign_keypair,
    make_bin_tree,
    run_sh,
    set_oauth_proxy_pin,
    set_openwebui_pin,
    sha256_bytes,
    skopeo_stub,
    symlink_tools,
)
from tests.helpers_image_inventory import (
    image_files,
    oci_archive,
    tar_bytes,
    write_docker_archive,
    write_image,
)
from tests.helpers_task_artifact import (
    TASK_ASSET,
    TASK_BINARY_SHA256,
    TASK_SHA256,
    copy_task_tools,
    task_archive,
)

# Stub skopeo: materialize every oci-archive:DEST from the fixture member of
# the same role (real OCI layouts written by the test); pack reads each
# archive itself (#1066), so it never needs `skopeo inspect`.
STUB_SKOPEO = skopeo_stub("a", materialize=True).replace(
    "printf 'stub-image-tar\\n' > \"$dest\"",
    'n=$(basename "$dest"); case "$n" in app-ingest-*) n=app-ingest.tar ;; app-agent-*) n=app-agent.tar ;; esac\n'
    '      cp "$PACK_TEST_IMAGES/$n" "$dest"')
assert "PACK_TEST_IMAGES" in STUB_SKOPEO

THIRD_PARTY_MEMBERS = ("qdrant-image", "openobserve-image", "otelcol-image", "oauth-proxy-image",
                       "openwebui-image", "openwebui-proxy-image")


def write_pack_images(root, app_files=None) -> dict[str, tuple[str, str]]:
    """Fixture registry images; returns {member stem: (manifest digest, config digest)}.

    App images carry the shipped Python inventory plus a role layer, so every
    image has its own config digest.
    """
    images = root / "pack-images"
    images.mkdir(exist_ok=True)
    raws = {stem: oci_archive(images / f"{stem}.tar", f"config-{stem}".encode(), [f"layer-{stem}".encode()],
                              compressed=True) for stem in THIRD_PARTY_MEMBERS}
    for role in ("ingest", "agent"):
        raws[f"app-{role}"] = write_image(images / f"app-{role}.tar",
                                          [app_files or image_files(root), {f"app/{role}": role.encode()}],
                                          compressed=True)
    return {stem: ("sha256:" + hashlib.sha256(raw).hexdigest(), json.loads(raw)["config"]["digest"])
            for stem, raw in raws.items()}


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
    copy_license_inputs(tmp_path)
    set_oauth_proxy_pin(tmp_path, "sha256:PENDING")
    set_openwebui_pin(tmp_path, "sha256:PENDING")
    for script in ("dependency_lock.py", "image_inventory.py"):
        shutil.copy(REPO / "scripts" / script, tmp_path / "scripts" / script)
    (tmp_path / "charts").mkdir(exist_ok=True)
    copy_chart(tmp_path)
    shutil.copytree(REPO / "images", tmp_path / "images")

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

    write_pack_images(tmp_path)
    # Throwaway signing key (mirrors the rehearsal flow: pack signs, the
    # bundle carries the derived pub, verification is self-consistent).
    key = gen_sign_keypair(tmp_path)
    return tmp_path, skopeo_log, head, key


def _run_pack(tree, *extra_env):
    tmp_path, _skopeo_log, head, key = tree
    env = {
        "PATH": str(tmp_path / "bin"),
        "SKOPEO_LOG": str(tmp_path / "skopeo-args.log"),
        "PACK_TEST_IMAGES": str(tmp_path / "pack-images"),
        "AIRGAP_APP_REGISTRY": "ghcr.io/pack-test",
        "SNEAKERNET_SIGNING_KEY": str(key),
        "AIRGAP_TASK_ARCHIVE": str(task_archive()),
    }
    for k, v in extra_env:
        env[k] = v
    return (run_sh(tmp_path / 'scripts' / 'airgap' / 'pack.sh', env, tmp_path), head)


def test_pack_sha_mismatch_fails_closed(pack_tree):
    r, _head = _run_pack(pack_tree, ("IMAGE_SHA", "f" * 40))
    assert r.returncode != 0
    assert "is not the checked-out commit" in r.stderr
    assert "airgap.env" in r.stderr


@pytest.mark.parametrize("role", ["agent", "ingest"])
def test_pack_refuses_a_containerfile_base_digest_off_the_pin(pack_tree, role):
    """#1030: the shipped Containerfile FROM must equal images.txt and the lock."""
    root, skopeo_log, _head, _key = pack_tree
    containerfile = root / "images" / f"Containerfile.{role}"
    good = containerfile.read_text()
    containerfile.write_text(re.sub(r"@sha256:[0-9a-f]{64}", "@sha256:" + "0" * 64, good))
    result, _ = _run_pack(pack_tree)
    assert result.returncode != 0
    assert "Containerfile base digest" in result.stderr
    assert not skopeo_log.exists()
    assert not (root / "dist" / "SHA256SUMS.sig").exists()
    containerfile.write_text(good)
    result, _ = _run_pack(pack_tree)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("directive", [
    "COPY --from=evil.example/img:latest / /x",
    "COPY --from=docker.io/library/busybox@sha256:" + "a" * 64 + " / /x",
    "RUN --mount=type=bind,from=evil.example/img:latest,target=/m true",
    "RUN --mount=type=bind,source=/,from=unknown,target=/m true",
    "COPY --from=missingstage \\\n    / /x",
], ids=["copy-external", "copy-digest-not-in-images", "mount-external", "mount-unknown", "continued"])
def test_pack_refuses_a_containerfile_that_reads_from_an_external_image(pack_tree, directive):
    """#1030: a non-FROM image reference is as unpinned as an off-pin FROM."""
    root, skopeo_log, _head, _key = pack_tree
    containerfile = root / "images" / "Containerfile.agent"
    good = containerfile.read_text()
    containerfile.write_text(good + "\n" + directive + "\n")
    result, _ = _run_pack(pack_tree)
    assert result.returncode != 0
    assert "Containerfile base digest check failed" in result.stderr
    assert not skopeo_log.exists()
    containerfile.write_text(good)
    result, _ = _run_pack(pack_tree)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("dirt", ["modified", "assume-unchanged", "skip-worktree", "staged"])
def test_pack_refuses_a_dirty_tree_before_signing(pack_tree, dirt):
    """#1030: `git bundle` records clean HEAD, so a dirty tree must not be signed."""
    root, _skopeo_log, _head, _key = pack_tree
    readme = root / "README.md"

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    if dirt == "assume-unchanged":
        git("update-index", "--assume-unchanged", "README.md")
    elif dirt == "skip-worktree":
        git("update-index", "--skip-worktree", "README.md")
    if dirt != "skip-worktree":
        readme.write_text("tampered\n")
    if dirt == "staged":
        git("add", "README.md")
    result, _ = _run_pack(pack_tree)
    assert result.returncode != 0
    assert "pack tree is not the clean checked-out commit" in result.stderr
    assert not (root / "dist" / "SHA256SUMS.sig").exists()
    if dirt == "assume-unchanged":
        git("update-index", "--no-assume-unchanged", "README.md")
    elif dirt == "skip-worktree":
        git("update-index", "--no-skip-worktree", "README.md")
    git("checkout", "HEAD", "--", "README.md")
    result, _ = _run_pack(pack_tree)
    assert result.returncode == 0, result.stderr


def test_pack_refuses_several_chart_archives_then_passes_after_fix(pack_tree):
    tmp_path, _skopeo_log, _head, _key = pack_tree
    chart = next((tmp_path / "charts").glob("qdrant-*.tgz"))
    extra = tmp_path / "charts" / "qdrant-9.9.9.tgz"
    shutil.copy(chart, extra)
    r, _ = _run_pack(pack_tree)
    assert r.returncode != 0
    assert "exactly one vendored Qdrant chart is required" in r.stderr
    assert not (tmp_path / "dist" / "MANIFEST.txt").exists()
    extra.unlink()
    r, _ = _run_pack(pack_tree)
    assert r.returncode == 0, r.stderr
    manifest = (tmp_path / "dist" / "MANIFEST.txt").read_text()
    assert manifest.count("chart_sha256: ") == 1
    assert f"chart_sha256: {sha256_bytes(chart.read_bytes())}\n" in manifest


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
        "openobserve-image.tar",
        "otelcol-image.tar",
        f"app-ingest-{head}.tar",
        f"app-agent-{head}.tar",
        "MANIFEST.txt",
        "PACKING_RECORD.txt",
        "sbom.json",
        "THIRD-PARTY-NOTICES.txt",
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
    images = write_pack_images(tmp_path)  # same deterministic fixture bytes the stub copied
    # Archive (platform manifest) digests and config digests read from each OCI
    # layout, not the images.txt list pins the images were pulled by.
    for role, stem in (
        ("qdrant", "qdrant-image"),
        ("openobserve", "openobserve-image"),
        ("otelcol", "otelcol-image"),
        ("ingest", "app-ingest"),
        ("agent", "app-agent"),
    ):
        assert f"{role}_digest: {images[stem][0]}\n" in manifest
        assert f"{role}_config_digest: {images[stem][1]}\n" in manifest
    assert len({digest for digest, _ in images.values()}) == len(images)
    assert images["qdrant-image"][0] not in (tmp_path / "images.txt").read_text()
    assert "image_format: oci-archive\n" in manifest
    assert "signed: ephemeral" in manifest
    assert "sha256:PENDING: not bundled" in r.stdout
    assert not (dist / "oauth-proxy-image.tar").exists()
    assert "oauth_proxy" not in manifest
    # The optional Open WebUI image (#1074) is skipped while its pin is pending.
    assert "Open WebUI pin is sha256:PENDING: not bundled" in r.stdout
    assert not (dist / "openwebui-image.tar").exists()
    assert "openwebui" not in manifest and "openwebui" not in (dist / "SHA256SUMS").read_text()
    assert "Open WebUI Image" not in (dist / "PACKING_RECORD.txt").read_text()
    assert "open-webui" not in skopeo_log.read_text()  # the build base is provenance only
    assert f"task_sha256: {TASK_SHA256}" in manifest
    assert f"task_binary_sha256: {TASK_BINARY_SHA256}" in manifest
    log = skopeo_log.read_text()
    assert log.splitlines().count("copy") == 5
    # Every image is copied as an oci-archive with the registry's own bytes
    # (no conversion or recompression); pack reads the layouts itself.
    assert log.splitlines().count("--preserve-digests") == 5
    assert len([a for a in log.splitlines() if a.startswith("oci-archive:")]) == 5
    assert "docker-archive" not in log
    assert "inspect" not in log
    # Digest-only refs: tag+digest combined is not a valid reference.
    assert "docker.io/qdrant/qdrant@sha256:" in log
    assert "@sha256:" in log
    # No Qdrant reference carries a tag at all, so no stale pin can slip in.
    qdrant_refs = re.findall(r"docker\.io/qdrant/qdrant\S*", log)
    assert qdrant_refs
    assert all(re.fullmatch(r"docker\.io/qdrant/qdrant@sha256:[0-9a-f]{64}", r) for r in qdrant_refs), qdrant_refs

    # sbom.json enumerates the pinned inputs as valid JSON.
    sbom = json.loads((dist / "sbom.json").read_text())
    assert sbom["image_sha"] == head
    assert {img["name"] for img in sbom["images"]} == {"qdrant", "openobserve", "otelcol", "app-ingest", "app-agent"}
    assert sbom["images"][4]["digest"] == images["app-agent"][0]
    assert sbom["host_tools"][0]["sha256"] == TASK_SHA256
    assert sbom["host_tools"][0]["license"] == "MIT"
    assert sbom["host_tools"][0]["license_file"] == "task-LICENSE"
    assert "Task host tool:" in (dist / "PACKING_RECORD.txt").read_text()
    with tarfile.open(dist / f"qdrant-pdf-rag-{head}.tar") as archive:
        assert set(archive.getnames()) == {
            "bootstrap.sh", "repo.bundle", TASK_ASSET, "task-pin.txt", "task-LICENSE",
            "qdrant-image.tar", "openobserve-image.tar", "otelcol-image.tar", f"app-ingest-{head}.tar", f"app-agent-{head}.tar",
            "MANIFEST.txt", "PACKING_RECORD.txt", "sbom.json", "THIRD-PARTY-NOTICES.txt",
            "sneakernet-signing.pub", "SHA256SUMS", "SHA256SUMS.sig",
        }
        notices = archive.extractfile("THIRD-PARTY-NOTICES.txt").read().decode()
    # Signed, readable offline, complete: actual notice texts and the open decisions.
    assert "THIRD-PARTY-NOTICES.txt" in (dist / "SHA256SUMS").read_text()
    assert "Apache License" in notices and "Zero-Clause BSD" in notices and "MIT License" in notices
    assert "Components awaiting owner decision: 0" in notices
    assert "pymupdf==" not in notices.split("COMPONENTS")[0]
    assert "Owner approval of this inventory: absent" in notices
    assert "Third-party notices: THIRD-PARTY-NOTICES.txt" in (dist / "PACKING_RECORD.txt").read_text()


def test_pack_bundles_oauth_proxy_once_digest_recorded(pack_tree):
    """Once the connected host records a digest, the sidecar image joins the
    bundle, MANIFEST, SBOM and member checksums."""
    tmp_path, skopeo_log, head, _key = pack_tree
    set_oauth_proxy_pin(tmp_path, "sha256:" + "b" * 64)
    # The recorded pin is committed state: the pack tree must be clean (#1030).
    subprocess.run(["git", "commit", "-qam", "record oauth-proxy pin"], cwd=tmp_path, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                          capture_output=True, text=True).stdout.strip()
    # Model signed-source export: without attachment omission, OCI archive
    # export fails before the archive can be materialized.
    guard = '''signed_source=0
remove_signatures=0
for arg in "$@"; do
  case "$arg" in
    docker://registry.redhat.io/openshift4/ose-oauth-proxy@*) signed_source=1 ;;
    --remove-signatures) remove_signatures=1 ;;
  esac
done
if [ "$signed_source" = 1 ] && [ "$remove_signatures" != 1 ]; then
  echo "OCI archives cannot store signature attachments" >&2
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
    images = write_pack_images(tmp_path)
    assert f"oauth_proxy_digest: {images['oauth-proxy-image'][0]}\n" in manifest
    assert f"oauth_proxy_config_digest: {images['oauth-proxy-image'][1]}\n" in manifest
    assert "oauth-proxy-image.tar" in (dist / "SHA256SUMS").read_text()
    with tarfile.open(dist / f"qdrant-pdf-rag-{head}.tar") as tf:
        assert "oauth-proxy-image.tar" in tf.getnames()
    sbom = json.loads((dist / "sbom.json").read_text())
    assert {img["name"] for img in sbom["images"]} == {
        "qdrant",
        "openobserve",
        "otelcol",
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


def test_pack_bundles_openwebui_once_digest_recorded(pack_tree):
    """ADR-0007 (#1074): a recorded Open WebUI pin joins the bundle, MANIFEST,
    SBOM, packing record and member checksums, and the packed archive's own
    bootstrap accepts the optional member."""
    tmp_path, skopeo_log, head, _key = pack_tree
    pin = "sha256:" + "e" * 64
    set_openwebui_pin(tmp_path, pin)
    subprocess.run(["git", "commit", "-qam", "record open webui pin"], cwd=tmp_path, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                          capture_output=True, text=True).stdout.strip()
    r, _ = _run_pack(pack_tree)
    assert r.returncode == 0, r.stderr
    assert "Open WebUI pin is sha256:PENDING" not in r.stdout
    dist = tmp_path / "dist"
    assert (dist / "openwebui-image.tar").is_file()
    ref = f"ghcr.io/yonatan895/qdrant-pdf-rag-openwebui@{pin}"
    assert f"docker://{ref}" in skopeo_log.read_text().splitlines()
    manifest = (dist / "MANIFEST.txt").read_text()
    images = write_pack_images(tmp_path)  # same deterministic fixture bytes the stub copied
    assert f"openwebui: {ref}" in manifest.splitlines()
    assert f"openwebui_digest: {images['openwebui-image'][0]}" in manifest.splitlines()
    assert f"openwebui_config_digest: {images['openwebui-image'][1]}" in manifest.splitlines()
    assert "openwebui-image.tar" in (dist / "SHA256SUMS").read_text()
    assert f"Open WebUI Image:    {ref}" in (dist / "PACKING_RECORD.txt").read_text()
    sbom = json.loads((dist / "sbom.json").read_text())
    assert {"name": "openwebui", "ref": ref, "digest": images["openwebui-image"][0]} in sbom["images"]
    # ADR-0007 E1: the allow-list proxy joins the bundle exactly when the Open WebUI member does.
    proxy_ref = ("docker.io/nginxinc/nginx-unprivileged@sha256:"
                 "f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03")
    assert f"docker://{proxy_ref}" in skopeo_log.read_text().splitlines()
    assert (dist / "openwebui-proxy-image.tar").is_file()
    assert f"openwebui_proxy: {proxy_ref}" in manifest.splitlines()
    assert f"openwebui_proxy_digest: {images['openwebui-proxy-image'][0]}" in manifest.splitlines()
    assert f"openwebui_proxy_config_digest: {images['openwebui-proxy-image'][1]}" in manifest.splitlines()
    assert "openwebui-proxy-image.tar" in (dist / "SHA256SUMS").read_text()
    assert {"name": "openwebui-proxy", "ref": proxy_ref,
            "digest": images["openwebui-proxy-image"][0]} in sbom["images"]
    assert {img["name"] for img in sbom["images"]} == {
        "qdrant", "openobserve", "otelcol", "app-ingest", "app-agent", "openwebui", "openwebui-proxy"}
    extract = tmp_path / "owui extraction"
    extract.mkdir()
    with tarfile.open(dist / f"qdrant-pdf-rag-{head}.tar") as archive:
        assert "openwebui-image.tar" in archive.getnames()
        assert "openwebui-proxy-image.tar" in archive.getnames()
        notices = archive.extractfile("THIRD-PARTY-NOTICES.txt").read().decode()
        archive.extractall(extract, filter="data")
    assert "image:openwebui  [approved]" in notices
    assert "image:openwebui-proxy  [approved]" in notices
    assert "LicenseRef-Open-WebUI-License" in notices
    boot = subprocess.run(["sh", "bootstrap.sh"], cwd=extract, env={"PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, check=False)
    assert boot.returncode == 0, boot.stdout + boot.stderr
    # Only the derived image is a member: the upstream build base is never pulled.
    assert "ghcr.io/open-webui/open-webui" not in skopeo_log.read_text()


def test_openwebui_base_and_derived_pin_needles_are_disjoint(pack_tree):
    """images.txt lookups are substring matches: each Open WebUI needle must
    select its own line and nothing else, whichever line comes first."""
    tmp_path, _log, _head, _key = pack_tree
    images = tmp_path / "images.txt"
    text = images.read_text()
    base = "ghcr.io/open-webui/open-webui:v0.12.3-slim"
    derived = "ghcr.io/yonatan895/qdrant-pdf-rag-openwebui:v0.12.3-slim"
    assert re.search(rf"^{re.escape(base)}\s+sha256:5adfb035[0-9a-f]{{56}}$", text, re.MULTILINE)
    assert re.search(rf"^{re.escape(derived)}\s+sha256:PENDING$", text, re.MULTILINE)
    text = re.sub(rf"^({re.escape(base)})\s+\S+$", r"\1 sha256:" + "1" * 64, text, flags=re.MULTILINE)
    images.write_text(text)
    probe = tmp_path / "scripts" / "airgap" / "pin-probe.sh"
    probe.write_text('. "$(dirname -- "$0")/common.sh"\n'
                     "pin_from_images_txt open-webui:v0.12.3-slim\n"
                     "pin_from_images_txt qdrant-pdf-rag-openwebui\n"
                     "if pin_recorded qdrant-pdf-rag-openwebui; then echo derived-recorded; else echo derived-pending; fi\n"
                     "pin_from_images_txt qdrant\n")
    r = subprocess.run(["sh", str(probe)], cwd=tmp_path, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"}, check=False)
    assert r.returncode == 0, r.stderr
    base_out, derived_out, state, qdrant_out = r.stdout.split()
    assert (base_out, derived_out, state) == (
        "ghcr.io/open-webui/open-webui@sha256:" + "1" * 64, derived, "derived-pending")
    # Neither line contains the qdrant needle's first match.
    assert qdrant_out.startswith("docker.io/qdrant/qdrant@sha256:")


def test_pack_rejects_bad_task_before_image_calls(pack_tree):
    tree, log, _, _ = pack_tree
    archive = tree / "unapproved.tar.gz"
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
    write_pack_images(root, files)
    result, _ = _run_pack(pack_tree)
    assert result.returncode != 0
    assert "SBOM reconciliation failed" in result.stderr
    assert not list((root / "dist").glob("SHA256SUMS.sig"))


@pytest.mark.parametrize("damage", ["tampered blob", "docker-archive"])
def test_pack_refuses_an_image_member_that_is_not_a_verified_oci_archive(pack_tree, damage):
    """#1066: pack binds digests only from a verified OCI layout, before signing."""
    root = pack_tree[0]
    member = root / "pack-images" / "otelcol-image.tar"
    if damage == "docker-archive":
        write_docker_archive(member, [{"etc/otelcol": b"otelcol"}])
    else:
        with tarfile.open(member) as source:
            members = {item.name: source.extractfile(item).read() if item.isfile() else None for item in source}
        layer = next(name for name, data in members.items() if data == b"layer-otelcol-image")
        members[layer] = b"layer-otelcol-imagf"  # same size, different bytes
        member.write_bytes(tar_bytes(members))
    result, _ = _run_pack(pack_tree)
    assert result.returncode != 0
    expected = ("otelcol-image.tar: an image blob is missing or does not match its digest" if damage == "tampered blob"
                else "otelcol-image.tar is not a single-image oci-archive")
    assert expected in result.stderr
    assert not list((root / "dist").glob("SHA256SUMS*"))


def _break_license_record(tree, mutate):
    record = tree[0] / "licenses" / "inventory.json"
    inventory = json.loads(record.read_text())
    mutate(inventory)
    record.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")


@pytest.mark.parametrize("change", ["image-digest", "unrecorded-dependency", "license-text-change"])
def test_pack_refuses_unreviewed_license_change_before_any_image_call(pack_tree, change):
    """A synthetic missing notice or unreviewed dependency/image change blocks the
    transfer bundle, then the next ordinary pack passes once it is reverted (#376)."""
    root, skopeo_log, _head, _key = pack_tree
    saved = {}
    if change == "missing-notice":
        saved["NOTICE.qdrant-skills"] = (root / "NOTICE.qdrant-skills").read_bytes()
        (root / "NOTICE.qdrant-skills").unlink()
    elif change == "license-text-change":
        saved["LICENSE.qdrant-skills"] = (root / "LICENSE.qdrant-skills").read_bytes()
        (root / "LICENSE.qdrant-skills").write_bytes(saved["LICENSE.qdrant-skills"] + b"\nchanged\n")
    elif change == "image-digest":
        saved["images.txt"] = (root / "images.txt").read_bytes()
        (root / "images.txt").write_text(
            (root / "images.txt").read_text().replace("sha256:efb96a94", "sha256:b0e04fe6"))
    else:
        saved["licenses/inventory.json"] = (root / "licenses/inventory.json").read_bytes()
        _break_license_record(pack_tree, lambda inv: inv["python"].pop(0))
    result, _ = _run_pack(pack_tree)
    assert result.returncode != 0
    assert "license inventory check failed" in result.stderr
    assert "license-inventory: FAIL" in result.stderr
    assert not skopeo_log.exists()
    assert not (root / "dist" / "SHA256SUMS.sig").exists()
    for name, data in saved.items():
        (root / name).write_bytes(data)
    result, _ = _run_pack(pack_tree)
    assert result.returncode == 0, result.stderr
    assert (root / "dist" / "THIRD-PARTY-NOTICES.txt").is_file()


# --- #1000: the release signing key is reachable only from the protected path

_SIGNING_ENV = "release-signing"
_MAIN_REF = "refs/heads/main"


def _workflow_jobs():
    import yaml

    for path in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        for name, job in (doc.get("jobs") or {}).items():
            yield path.name, name, job


def test_signing_secret_and_trusted_flag_only_on_protected_release_path():
    """No PR-triggerable or non-main path can read the key or claim trust.

    Same-repo PR workflows get repository secrets, so the secret must live in
    a protected GitHub environment (deployment branches limited to main), and
    every read and the trusted flag must be gated on the main ref as well.
    """
    seen = 0
    for wf, name, job in _workflow_jobs():
        steps = job.get("steps") or []
        text = json.dumps(job)
        reads_key = "secrets.SNEAKERNET_SIGNING_KEY" in text
        sets_trusted = re.search(r"SNEAKERNET_KEY_TRUSTED=true", text) is not None
        if not (reads_key or sets_trusted):
            continue
        seen += 1
        where = f"{wf}:{name}"
        env = job.get("environment")
        env_name = env.get("name") if isinstance(env, dict) else env
        assert env_name and _SIGNING_ENV in str(env_name), f"{where}: no {_SIGNING_ENV} environment"
        # Branch dispatches must fall back to an environment without the secret.
        assert _MAIN_REF in str(env_name), f"{where}: environment not selected by main ref"
        assert "pull_request" not in str(job.get("if", "")), f"{where}: PR-triggerable"
        for step in steps:
            step_text = json.dumps(step)
            if "secrets.SNEAKERNET_SIGNING_KEY" in step_text:
                for value in (step.get("env") or {}).values():
                    if "secrets.SNEAKERNET_SIGNING_KEY" in str(value):
                        assert _MAIN_REF in str(value), f"{where}: secret read not gated on main"
            if "SNEAKERNET_KEY_TRUSTED=true" in step_text:
                run = step.get("run", "")
                before = run.split("SNEAKERNET_KEY_TRUSTED=true")[0]
                assert "GITHUB_REF" in before and "refs/heads/main" in before, (
                    f"{where}: trusted flag not guarded by the main ref")
                assert "GITHUB_EVENT_NAME" in before, f"{where}: trusted flag not guarded by event"
    assert seen, "signing step not found: update this contract if it moved"


def _eval_gha(expr: str, event: str, ref: str, secret: str = "SECRET") -> str:
    """Evaluate the `a && b && 'x' || 'y'` subset of a workflow expression."""
    body = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", str(expr), re.DOTALL)
    assert body, f"not a plain expression: {expr!r}"
    py = body.group(1).replace("&&", " and ").replace("||", " or ")
    py = py.replace("github.event_name", repr(event)).replace("github.ref", repr(ref))
    py = py.replace("secrets.SNEAKERNET_SIGNING_KEY", repr(secret))
    assert not re.search(r"[A-Za-z_]\w*\.\w", re.sub(r"'[^']*'", "", py)), f"unknown context: {expr!r}"
    return str(eval(py, {"__builtins__": {}}))


def test_only_manual_main_dispatch_signs_and_nightly_packs_ephemeral():
    """#1051: schedule on main packs with the ephemeral key (no approval prompt);
    only workflow_dispatch on main reaches release-signing, the secret and trust."""
    import yaml

    doc = yaml.safe_load((REPO / ".github" / "workflows" / "e2e.yml").read_text())
    job = doc["jobs"]["airgap-package"]
    step = next(s for s in job["steps"] if s.get("name") == "airgap-pack")
    key_expr = step["env"]["SNEAKERNET_SIGNING_KEY"]
    cond = re.search(r"if (\[[^\n]*\]); then\s*\n\s*export SNEAKERNET_KEY_TRUSTED=true", step["run"])
    assert cond, "trusted-flag guard not found"
    matrix = [
        ("schedule", _MAIN_REF, "branch-unsigned", "", False),
        ("workflow_dispatch", _MAIN_REF, _SIGNING_ENV, "SECRET", True),
        ("workflow_dispatch", "refs/heads/feature", "branch-unsigned", "", False),
        ("schedule", "refs/heads/feature", "branch-unsigned", "", False),
        ("pull_request", "refs/pull/1/merge", "branch-unsigned", "", False),
        ("push", _MAIN_REF, "branch-unsigned", "", False),
    ]
    for event, ref, env_name, secret, trusted in matrix:
        where = f"{event}@{ref}"
        assert _eval_gha(job["environment"], event, ref) == env_name, where
        assert _eval_gha(key_expr, event, ref) == secret, where
        out = subprocess.run(
            ["sh", "-c", f"{cond.group(1)} && echo trusted"], capture_output=True, text=True, check=False,
            env={"PATH": os.environ["PATH"], "GITHUB_REF": ref, "GITHUB_EVENT_NAME": event})
        assert (out.stdout.strip() == "trusted") is trusted, f"{where}: trusted flag"


def test_openwebui_product_image_is_the_pinned_upstream_plus_one_ownership_layer():
    """ADR-0007 R1: images/openwebui/Containerfile derives from exactly the upstream
    slim digest recorded in images.txt, adds no content (no COPY/ADD, no installs,
    no downloads) and ends as a non-root UID in group 0 (restricted-v2)."""
    import re

    text = (REPO / "images" / "openwebui" / "Containerfile").read_text()
    instructions = [line.split(None, 1) for line in text.replace("\\\n", " ").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    froms = [parts[1].strip() for parts in instructions if parts[0] == "FROM"]
    pins = {p[0]: p[1] for p in (line.split() for line in (REPO / "images.txt").read_text().splitlines()
                                 if line.strip() and not line.lstrip().startswith("#")) if len(p) >= 2}
    upstream = next(k for k in pins if k.startswith("ghcr.io/open-webui/open-webui:"))
    assert froms == [f"{upstream.partition(':')[0]}@{pins[upstream]}"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", pins[upstream])
    forbidden = {"COPY", "ADD"}
    assert not forbidden & {parts[0] for parts in instructions}
    runs = " ".join(parts[1] for parts in instructions if parts[0] == "RUN")
    assert not re.search(r"\b(pip|npm|apt|apt-get|apk|curl|wget|dnf|microdnf)\b", runs)
    assert [parts[1].strip() for parts in instructions if parts[0] == "USER"][-1] == "1000:0"
    # The derived pin is named for the product repository and the same upstream tag.
    derived = next(k for k in pins if k.startswith("ghcr.io/yonatan895/qdrant-pdf-rag-openwebui:"))
    assert derived.partition(":")[2] == upstream.partition(":")[2]
