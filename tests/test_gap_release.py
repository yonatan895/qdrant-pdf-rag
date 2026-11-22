"""Comprehensive tests for Increment 1 of the F6 gap release pipeline
(GH#1207 Rev 2 D1–D12, ADR-0009 §5):
  - scripts/gap/release.py & scripts/gap/release.sh
  - end-to-end classify -> pack -> external Vault sign -> assemble -> verify -> bootstrap.sh
  - write-once crash recovery, two-fetch repo.bundle content identity (D10)
  - every D2/D3/D4/D6/D7/D10 refusal + next ordinary run after refusal
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from scripts.gap import allowlist, release, sign_vault

from tests.helpers_airgap import (
    REPO,
    reference_docs_member,
    sha256_bytes,
    sign_sums,
)
from tests.helpers_task_artifact import copy_task_tools, task_manifest, task_members

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Gap Tester",
        "GIT_AUTHOR_EMAIL": "gap@example.invalid",
        "GIT_COMMITTER_NAME": "Gap Tester",
        "GIT_COMMITTER_EMAIL": "gap@example.invalid",
        "GIT_AUTHOR_DATE": "2026-11-21T12:00:00Z",
        "GIT_COMMITTER_DATE": "2026-11-21T12:00:00Z",
    }
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True, text=True, check=check)


def _gen_rsa3072(dest: Path, prefix: str) -> tuple[Path, Path]:
    dest.mkdir(parents=True, exist_ok=True)
    priv = dest / f"{prefix}.pem"
    pub = dest / f"{prefix}.pub"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", str(priv)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    return priv, pub


def _make_oci_archive(path: Path, label: str, *, corrupt_diff_id: bool = False, bad_history: bool = False) -> tuple[str, str]:
    """Create a valid single-image OCI archive (gzip layer) and return (manifest_digest, config_digest)."""
    layer_tar_buf = io.BytesIO()
    with tarfile.open(fileobj=layer_tar_buf, mode="w:") as lt:
        data = f"layer-content-{label}\n".encode()
        info = tarfile.TarInfo(name=f"app/{label}.txt")
        info.size = len(data)
        info.mtime = 1700000000
        lt.addfile(info, io.BytesIO(data))
    raw_layer = layer_tar_buf.getvalue()
    diff_id = "sha256:" + hashlib.sha256(raw_layer).hexdigest()
    if corrupt_diff_id:
        diff_id = "sha256:" + "f" * 64

    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
        gz.write(raw_layer)
    gz_bytes = gz_buf.getvalue()
    layer_digest = "sha256:" + hashlib.sha256(gz_bytes).hexdigest()

    history = [{"created_by": f"COPY {label}", "empty_layer": False}]
    if bad_history:
        history.append({"created_by": "EXTRA", "empty_layer": False})

    config_obj = {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        "history": history,
    }
    config_raw = json.dumps(config_obj, sort_keys=True).encode("utf-8")
    config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()

    manifest_obj = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": len(config_raw),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": layer_digest,
                "size": len(gz_bytes),
            }
        ],
    }
    manifest_raw = json.dumps(manifest_obj, sort_keys=True).encode("utf-8")
    manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()

    index_raw = json.dumps({
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_digest,
                "size": len(manifest_raw),
            }
        ],
    }).encode("utf-8")
    layout_raw = json.dumps({"imageLayoutVersion": "1.0.0"}).encode("utf-8")

    blobs = {
        "oci-layout": layout_raw,
        "index.json": index_raw,
        f"blobs/sha256/{manifest_digest[7:]}": manifest_raw,
        f"blobs/sha256/{config_digest[7:]}": config_raw,
        f"blobs/sha256/{layer_digest[7:]}": gz_bytes,
    }
    with tarfile.open(path, "w:") as tf:
        for name, content in blobs.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = 1700000000
            tf.addfile(info, io.BytesIO(content))
    return manifest_digest, config_digest


@pytest.fixture(scope="module")
def keypairs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("gap-keys")
    up_priv, up_pub = _gen_rsa3072(d, "upstream")
    gap_priv, gap_pub = _gen_rsa3072(d, "gap")
    return {"up_priv": up_priv, "up_pub": up_pub, "gap_priv": gap_priv, "gap_pub": gap_pub}


def _build_fixture(tmp_path: Path, keypairs: dict[str, Path]) -> dict[str, Path | str]:
    """Build a complete upstream base bundle + downstream GitLab fork remote with a clean
    gap merge commit + annotated tag gap-release-1."""
    remote = tmp_path / "gitlab-remote"
    remote.mkdir()
    _git(remote, "init", "-b", "main")
    copy_task_tools(remote)
    (remote / "scripts/airgap").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "scripts/airgap/bootstrap.sh", remote / "scripts/airgap/bootstrap.sh")
    (remote / "Taskfile.yml").write_text("version: '3'\ntasks:\n  probe:\n    cmds: ['true']\n", encoding="utf-8")
    (remote / "airgap.env.example").write_text("INTERNAL_REGISTRY=example\n", encoding="utf-8")
    (remote / "docs").mkdir(exist_ok=True)
    (remote / "docs/deploy.md").write_text("# Deploy\nbase\n", encoding="utf-8")
    (remote / "charts/mainframe-rag").mkdir(parents=True, exist_ok=True)
    (remote / "charts/mainframe-rag/values.yaml").write_text("replicas: 2\n", encoding="utf-8")
    (remote / "src").mkdir(exist_ok=True)
    (remote / "src/app.py").write_text("X = 1\n", encoding="utf-8")
    (remote / "requirements.lock.txt").write_text("pkg==1.0\n", encoding="utf-8")
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "Upstream release 1")
    base_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()

    # Build verified base upstream bundle directory + tarball.
    base_dir = tmp_path / "base-bundle-dir"
    base_dir.mkdir()
    shutil.copy(remote / "scripts/airgap/bootstrap.sh", base_dir / "bootstrap.sh")
    _git(remote, "bundle", "create", str(base_dir / "repo.bundle"), "HEAD", "--all")
    (base_dir / "reference-docs.tar.gz").write_bytes(reference_docs_member())
    for m_name, m_bytes in task_members().items():
        (base_dir / m_name).write_bytes(m_bytes)

    qd, qcfg = _make_oci_archive(base_dir / "qdrant-image.tar", "qdrant")
    ood, oocfg = _make_oci_archive(base_dir / "openobserve-image.tar", "openobserve")
    otd, otcfg = _make_oci_archive(base_dir / "otelcol-image.tar", "otelcol")
    ingd, ingcfg = _make_oci_archive(base_dir / f"app-ingest-{base_sha}.tar", "ingest")
    agd, agcfg = _make_oci_archive(base_dir / f"app-agent-{base_sha}.tar", "agent")

    refdocs_sha = sha256_bytes((base_dir / "reference-docs.tar.gz").read_bytes())
    manifest_lines = [
        f"sha: {base_sha}",
        *task_manifest().splitlines(),
        "date: 2026-11-20T00:00:00Z",
        "image_format: oci-archive",
        "qdrant: docker.io/qdrant/qdrant:v1.19.2-unprivileged",
        f"qdrant_digest: {qd}",
        f"qdrant_config_digest: {qcfg}",
        "openobserve: public.ecr.aws/zinclabs/openobserve:v1.1.2",
        f"openobserve_digest: {ood}",
        f"openobserve_config_digest: {oocfg}",
        "otelcol: ghcr.io/open-telemetry/otelcol:0.160.0",
        f"otelcol_digest: {otd}",
        f"otelcol_config_digest: {otcfg}",
        "chart: qdrant-1.19.2",
        "chart_sha256: " + "a" * 64,
        "reference_docs: reference-docs.tar.gz",
        f"reference_docs_sha256: {refdocs_sha}",
        "reference_docs_sets: 0",
        f"ingest: ghcr.io/example/qdrant-pdf-rag-ingest:{base_sha}",
        f"ingest_digest: {ingd}",
        f"ingest_config_digest: {ingcfg}",
        f"agent: ghcr.io/example/qdrant-pdf-rag-agent:{base_sha}",
        f"agent_digest: {agd}",
        f"agent_config_digest: {agcfg}",
        "app_registry: ghcr.io/example",
        "signed: true",
        "origin: upstream",
    ]
    (base_dir / "MANIFEST.txt").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    (base_dir / "PACKING_RECORD.txt").write_text("upstream packing record\n", encoding="utf-8")
    sbom_obj = {
        "generated": "2026-11-20T00:00:00Z",
        "image_sha": base_sha,
        "images": [
            {"name": "qdrant", "ref": "docker.io/qdrant/qdrant:v1.19.2-unprivileged", "digest": qd},
            {"name": "app-ingest", "ref": f"ghcr.io/example/qdrant-pdf-rag-ingest:{base_sha}", "digest": ingd},
            {"name": "app-agent", "ref": f"ghcr.io/example/qdrant-pdf-rag-agent:{base_sha}", "digest": agd},
        ],
    }
    (base_dir / "sbom.json").write_text(json.dumps(sbom_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (base_dir / "THIRD-PARTY-NOTICES.txt").write_text("notices\n", encoding="utf-8")
    shutil.copy(keypairs["up_pub"], base_dir / "sneakernet-signing.pub")

    members = release._expected_members(base_sha, ())
    sums_lines = [f"{sha256_bytes((base_dir / m).read_bytes())}  {m}" for m in members]
    (base_dir / "SHA256SUMS").write_text("\n".join(sums_lines) + "\n", encoding="utf-8")
    sign_sums(base_dir, keypairs["up_priv"])

    base_tar = tmp_path / f"qdrant-pdf-rag-{base_sha}.tar"
    with tarfile.open(base_tar, "w:") as tf:
        for name in [*members, "SHA256SUMS", "SHA256SUMS.sig"]:
            tf.add(base_dir / name, arcname=name)
    Path(str(base_tar) + ".sha256").write_text(
        f"{sha256_bytes(base_tar.read_bytes())}  {base_tar.name}\n",
        encoding="utf-8",
    )

    # Create fork init commit with Upstream-Release trailer + site layer.
    (remote / "gap").mkdir(exist_ok=True)
    (remote / "gap/README.md").write_text("# Gap fork\n", encoding="utf-8")
    (remote / "gap/patches.md").write_text("# Gap patches\n", encoding="utf-8")
    _git(remote, "add", "gap")
    _git(remote, "commit", "-m", f"Create gap fork\n\nUpstream-Release: {base_sha}")

    # Create a feature branch with an allowed no-layer chart patch + ledger entry, then merge --no-ff!
    _git(remote, "checkout", "-b", "feat/chart-tuning")
    (remote / "charts/mainframe-rag/values.yaml").write_text("replicas: 3\n", encoding="utf-8")
    _git(remote, "add", "charts/mainframe-rag/values.yaml")
    _git(remote, "commit", "-m", "Tune replicas\n\nGap-Patch: GL#42")
    patch_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    (remote / "gap/patches.md").write_text(
        f"# Gap patches\n\n## GL#42 {patch_sha}\n- path: charts/mainframe-rag/values.yaml\n",
        encoding="utf-8",
    )
    _git(remote, "add", "gap/patches.md")
    _git(remote, "commit", "-m", "Record GL#42 in ledger")
    _git(remote, "checkout", "main")
    _git(remote, "merge", "--no-ff", "feat/chart-tuning", "-m", "Merge branch 'feat/chart-tuning' (GL#42)")
    merge_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    _git(remote, "tag", "-a", "gap-release-1", "-m", "Gap release 1\n\nGap-Release: GL#42", merge_sha)

    return {
        "remote": remote,
        "base_dir": base_dir,
        "base_tar": base_tar,
        "base_sha": base_sha,
        "merge_sha": merge_sha,
        "patch_sha": patch_sha,
        "session_dir": tmp_path / "empty-session",
    }


def _sign_external_vault_style(sums_path: Path, gap_priv: Path, out_sig: Path) -> None:
    """Simulate workstation Vault transit signing + vault:v1:<b64> conversion (D3)."""
    digest = hashlib.sha256(sums_path.read_bytes()).digest()
    raw_sig = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(gap_priv),
            "-pkeyopt",
            "digest:sha256",
            "-pkeyopt",
            "rsa_padding_mode:pkcs1",
        ],
        input=digest,
        capture_output=True,
        check=True,
    ).stdout
    vault_str = sign_vault.raw_sig_to_vault(raw_sig, version=1)
    converted = sign_vault.vault_sig_to_raw(vault_str)
    out_sig.write_bytes(converted)


def test_full_end_to_end_classify_pack_vault_sign_assemble_verify_and_bootstrap(
    tmp_path: Path, keypairs: dict[str, Path]
):
    fx = _build_fixture(tmp_path, keypairs)
    remote = Path(fx["remote"])
    base_tar = Path(fx["base_tar"])
    base_sha = str(fx["base_sha"])
    merge_sha = str(fx["merge_sha"])
    work_dir = tmp_path / "work" / "gap-release-1"
    session_dir = Path(fx["session_dir"])

    # 1. Classify
    rc, payload = release.cmd_classify(repo=str(remote), tag="gap-release-1", sha=None, base_sha=base_sha)
    assert rc == 0
    assert payload["all_allowed"] is True
    assert payload["needs_layer_replacement"] is False

    # 2. Pack (unsigned, in bastion)
    packed = release.cmd_pack(
        tag="gap-release-1",
        remote=str(remote),
        base_bundle=base_tar,
        work_dir=work_dir,
        upstream_pub=keypairs["up_pub"],
        gap_pub=keypairs["gap_pub"],
        repo_root=REPO,
        session_dir=session_dir,
        require_gap_rsa3072=True,
    )
    assert packed["sha"] == merge_sha
    assert packed["gap_base"] == base_sha
    assert packed["gap_issue"] == "GL#42"
    assert not (work_dir / "scratch").exists()
    # D7: reference-docs.tar.gz copied unchanged from base
    assert (work_dir / "out" / "reference-docs.tar.gz").read_bytes() == (
        Path(fx["base_dir"]) / "reference-docs.tar.gz"
    ).read_bytes()

    # 3. External Vault-style signing (outside bastion) + assemble
    sig_file = tmp_path / "workstation-SHA256SUMS.sig"
    _sign_external_vault_style(work_dir / "out" / "SHA256SUMS", keypairs["gap_priv"], sig_file)
    assembled = release.cmd_assemble(
        work_dir=work_dir,
        signature_path=sig_file,
        upstream_pub=keypairs["up_pub"],
        gap_pub=keypairs["gap_pub"],
        repo_root=REPO,
    )
    assert assembled["release_identity"] == packed["release_identity"]
    gap_tar = Path(assembled["tarball"])
    assert gap_tar.is_file() and Path(str(gap_tar) + ".sha256").is_file()

    # 4. Trusted verify (6-step order)
    verified = release.cmd_verify(
        bundle=gap_tar,
        base_bundle=base_tar,
        upstream_pub=keypairs["up_pub"],
        gap_pub=keypairs["gap_pub"],
        repo_root=REPO,
        remote=str(remote),
    )
    assert verified["sha"] == merge_sha
    assert verified["release_identity"] == packed["release_identity"]
    assert verified["changed_upstream_paths"] == ["charts/mainframe-rag/values.yaml"]

    # 5. Unchanged bootstrap.sh accepts the assembled gap bundle with both anchors!
    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    with tarfile.open(gap_tar, "r:") as tf:
        tf.extractall(boot_dir, filter="data")
    boot_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "SNEAKERNET_TRUSTED_PUB": str(keypairs["up_pub"]),
        "SNEAKERNET_GAP_TRUSTED_PUB": str(keypairs["gap_pub"]),
    }
    proc = subprocess.run(["sh", "bootstrap.sh"], cwd=boot_dir, env=boot_env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (boot_dir / "qdrant-pdf-rag" / "charts/mainframe-rag/values.yaml").read_text() == "replicas: 3\n"


def test_crash_recovery_rerun_converges_to_identical_release_identity(tmp_path: Path, keypairs: dict[str, Path]):
    fx = _build_fixture(tmp_path, keypairs)
    work_dir = tmp_path / "work" / "crash"
    kwargs = {
        "tag": "gap-release-1",
        "remote": str(fx["remote"]),
        "base_bundle": Path(fx["base_tar"]),
        "work_dir": work_dir,
        "upstream_pub": keypairs["up_pub"],
        "gap_pub": keypairs["gap_pub"],
        "repo_root": REPO,
        "session_dir": Path(fx["session_dir"]),
    }
    first = release.cmd_pack(**kwargs)
    # Simulate a mid-pack crash: remove half the written members and leave a partial dir.
    for victim in ("MANIFEST.txt", "PACKING_RECORD.txt", "sbom.json", "SHA256SUMS", "qdrant-image.tar"):
        (work_dir / "out" / victim).unlink()
    (work_dir / "out.partial").mkdir(exist_ok=True)
    (work_dir / "out.partial" / "leftover.tmp").write_text("partial\n", encoding="utf-8")
    (work_dir / "scratch" / "base-bundle").mkdir(parents=True)

    second = release.cmd_pack(**kwargs)
    assert second["release_identity"] == first["release_identity"]
    assert not (work_dir / "out.partial").exists()
    assert not (work_dir / "scratch").exists()


def test_two_separate_packs_with_different_repo_bundle_bytes_both_pass_verify(
    tmp_path: Path, keypairs: dict[str, Path]
):
    """D10: repo.bundle is verified by content identity, not byte equality across packs."""
    fx = _build_fixture(tmp_path, keypairs)
    base_tar = Path(fx["base_tar"])
    merge_sha = str(fx["merge_sha"])
    tarballs: list[Path] = []
    bundle_hashes: list[str] = []

    for idx in (1, 2):
        work_dir = tmp_path / f"work-{idx}"
        release.cmd_pack(
            tag="gap-release-1",
            remote=str(fx["remote"]),
            base_bundle=base_tar,
            work_dir=work_dir,
            upstream_pub=keypairs["up_pub"],
            gap_pub=keypairs["gap_pub"],
            repo_root=REPO,
            session_dir=Path(fx["session_dir"]),
        )
        if idx == 2:
            # Force a different valid pack encoding (compression level 0) for the exact same commit history!
            alt_bare = tmp_path / "alt-bare.git"
            alt_bare.mkdir()
            _git(alt_bare, "init", "--bare", "-q")
            _git(alt_bare, "fetch", "-q", "--no-tags", str(fx["remote"]), f"{merge_sha}:refs/heads/_p")
            _git(alt_bare, "update-ref", "HEAD", merge_sha)
            _git(alt_bare, "update-ref", "-d", "refs/heads/_p")
            _git(alt_bare, "-c", "pack.compression=0", "bundle", "create", str(work_dir / "out" / "repo.bundle"), "HEAD")
            # Recompute SHA256SUMS and request.txt for this second pack
            members = release._expected_members(merge_sha, ())
            sums_bytes = release._format_sha256sums(work_dir / "out", members)
            (work_dir / "out" / "SHA256SUMS").write_bytes(sums_bytes)
            new_id = release._sha256_bytes(sums_bytes)
            req = work_dir / "request.txt"
            req.write_text(
                "\n".join(
                    f"release_identity={new_id}" if line.startswith("release_identity=") else line
                    for line in req.read_text().splitlines()
                ) + "\n",
                encoding="utf-8",
            )
        bundle_hashes.append(sha256_bytes((work_dir / "out" / "repo.bundle").read_bytes()))
        sig = tmp_path / f"sig-{idx}"
        _sign_external_vault_style(work_dir / "out" / "SHA256SUMS", keypairs["gap_priv"], sig)
        asm = release.cmd_assemble(
            work_dir=work_dir,
            signature_path=sig,
            upstream_pub=keypairs["up_pub"],
            gap_pub=keypairs["gap_pub"],
            repo_root=REPO,
        )
        tarballs.append(Path(asm["tarball"]))

    assert bundle_hashes[0] != bundle_hashes[1]
    for tb in tarballs:
        res = release.cmd_verify(
            bundle=tb,
            base_bundle=base_tar,
            upstream_pub=keypairs["up_pub"],
            gap_pub=keypairs["gap_pub"],
            repo_root=REPO,
        )
        assert res["sha"] == merge_sha


def test_pack_refuses_bastion_session_credentials_and_succeeds_once_cleared(tmp_path: Path):
    session_dir = tmp_path / "session"
    priv_dir = session_dir / "private"
    priv_dir.mkdir(parents=True)

    # kubeconfig present -> refused
    (priv_dir / "kubeconfig").write_text("apiVersion: v1\n", encoding="utf-8")
    with pytest.raises(release.ReleaseStop, match="kubeconfig"):
        release.check_pack_session(session_dir, env={})
    (priv_dir / "kubeconfig").unlink()

    # auth.json present -> refused
    (priv_dir / "auth.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(release.ReleaseStop, match="auth.json"):
        release.check_pack_session(session_dir, env={})
    (priv_dir / "auth.json").unlink()

    # SNEAKERNET_SIGNING_KEY set -> refused
    with pytest.raises(release.ReleaseStop, match="never holds a signing key"):
        release.check_pack_session(session_dir, env={"SNEAKERNET_SIGNING_KEY": "/tmp/key.pem"})

    # Clean session succeeds
    release.check_pack_session(session_dir, env={})


def test_tag_binding_merge_commit_allowlist_and_ledger_refusals(tmp_path: Path, keypairs: dict[str, Path]):
    fx = _build_fixture(tmp_path, keypairs)
    remote = Path(fx["remote"])
    base_tar = Path(fx["base_tar"])
    merge_sha = str(fx["merge_sha"])
    patch_sha = str(fx["patch_sha"])
    session_dir = Path(fx["session_dir"])

    def _pack(tag: str, wd: Path) -> dict[str, str]:
        return release.cmd_pack(
            tag=tag,
            remote=str(remote),
            base_bundle=base_tar,
            work_dir=wd,
            upstream_pub=keypairs["up_pub"],
            gap_pub=keypairs["gap_pub"],
            repo_root=REPO,
            session_dir=session_dir,
        )

    # 1. Lightweight tag refused
    _git(remote, "tag", "gap-light", merge_sha)
    with pytest.raises(release.ReleaseStop, match="annotated tag"):
        _pack("gap-light", tmp_path / "w-light")

    # 2. Annotated tag without Gap-Release: GL#<n> refused
    _git(remote, "tag", "-a", "gap-no-issue", "-m", "missing trailer", merge_sha)
    with pytest.raises(release.ReleaseStop, match="Gap-Release: GL#"):
        _pack("gap-no-issue", tmp_path / "w-no-issue")

    # 3. Interior branch commit (not on origin/main first-parent chain) refused
    _git(remote, "tag", "-a", "gap-interior", "-m", "Gap-Release: GL#42", patch_sha)
    with pytest.raises(release.ReleaseStop, match="first-parent chain"):
        _pack("gap-interior", tmp_path / "w-interior")

    # 4. Non-merge direct commit on main refused (D4)
    (remote / "gap/README.md").write_text("# Direct edit\n", encoding="utf-8")
    _git(remote, "add", "gap/README.md")
    _git(remote, "commit", "-m", "Direct non-merge commit on main")
    direct_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    _git(remote, "tag", "-a", "gap-non-merge", "-m", "Gap-Release: GL#43", direct_sha)
    with pytest.raises(release.ReleaseStop, match="not a merge commit"):
        _pack("gap-non-merge", tmp_path / "w-non-merge")

    # 5. Disallowed path (requirements.lock.txt) refused with exact fixed D2 refusal
    _git(remote, "checkout", "-b", "feat/bad-lock")
    (remote / "requirements.lock.txt").write_text("pkg==2.0\n", encoding="utf-8")
    _git(remote, "add", "requirements.lock.txt")
    _git(remote, "commit", "-m", "Bump lock")
    _git(remote, "checkout", "main")
    _git(remote, "merge", "--no-ff", "feat/bad-lock", "-m", "Merge bad lock")
    bad_lock_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    _git(remote, "tag", "-a", "gap-bad-lock", "-m", "Gap-Release: GL#44", bad_lock_sha)
    with pytest.raises(release.ReleaseStop) as exc:
        _pack("gap-bad-lock", tmp_path / "w-bad-lock")
    assert str(exc.value) == allowlist.REFUSAL_MESSAGE

    # 6. Revert bad lock, add src/ change -> refused in Increment 1 (layer replacement)
    _git(remote, "reset", "--hard", merge_sha)
    _git(remote, "checkout", "-b", "feat/src-change")
    (remote / "src/app.py").write_text("X = 2\n", encoding="utf-8")
    _git(remote, "add", "src/app.py")
    _git(remote, "commit", "-m", "Edit src")
    _git(remote, "checkout", "main")
    _git(remote, "merge", "--no-ff", "feat/src-change", "-m", "Merge src change")
    src_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    _git(remote, "tag", "-a", "gap-src", "-m", "Gap-Release: GL#45", src_sha)
    with pytest.raises(release.ReleaseStop) as exc:
        _pack("gap-src", tmp_path / "w-src")
    assert str(exc.value) == allowlist.LAYER_REPLACEMENT_REFUSAL

    # 7. Unledgered upstream-owned change (docs/deploy.md without gap/patches.md entry) refused
    _git(remote, "reset", "--hard", merge_sha)
    _git(remote, "checkout", "-b", "feat/unledgered")
    (remote / "docs/deploy.md").write_text("# Deploy\nchanged\n", encoding="utf-8")
    _git(remote, "add", "docs/deploy.md")
    _git(remote, "commit", "-m", "Edit docs without ledger")
    _git(remote, "checkout", "main")
    _git(remote, "merge", "--no-ff", "feat/unledgered", "-m", "Merge unledgered")
    unled_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    _git(remote, "tag", "-a", "gap-unled", "-m", "Gap-Release: GL#46", unled_sha)
    with pytest.raises(release.ReleaseStop, match="gap/patches.md"):
        _pack("gap-unled", tmp_path / "w-unled")

    # 8. Next ordinary pack of valid tag gap-release-1 succeeds cleanly
    _git(remote, "reset", "--hard", merge_sha)
    ok = _pack("gap-release-1", tmp_path / "w-ok")
    assert ok["sha"] == merge_sha

    # 9. A tag really moved on the remote (to another merge on main) is refused
    #    in the work directory that pinned the first sha.
    _git(remote, "checkout", "-b", "feat/site-note")
    (remote / "gap/README.md").write_text("# Gap fork\nsite note\n", encoding="utf-8")
    _git(remote, "add", "gap/README.md")
    _git(remote, "commit", "-m", "Site note")
    _git(remote, "checkout", "main")
    _git(remote, "merge", "--no-ff", "feat/site-note", "-m", "Merge site note")
    moved_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    _git(remote, "tag", "-f", "-a", "gap-release-1", "-m", "Gap release 1\n\nGap-Release: GL#42", moved_sha)
    with pytest.raises(release.ReleaseStop, match="moved"):
        _pack("gap-release-1", tmp_path / "w-ok")
    assert (tmp_path / "w-ok" / "request.txt").read_text().count(f"sha={merge_sha}") == 1

    # 10. A work directory belongs to one tag.
    _git(remote, "tag", "-a", "gap-release-2", "-m", "Gap release 2\n\nGap-Release: GL#47", moved_sha)
    with pytest.raises(release.ReleaseStop, match="one work directory per tag"):
        _pack("gap-release-2", tmp_path / "w-ok")

    # 11. The next ordinary pack, in a fresh work directory, packs the commit the tag names now.
    assert _pack("gap-release-1", tmp_path / "w-moved")["sha"] == moved_sha
    assert _pack("gap-release-2", tmp_path / "w-two")["sha"] == moved_sha


def test_verify_tamper_and_bundle_identity_refusals(tmp_path: Path, keypairs: dict[str, Path]):
    fx = _build_fixture(tmp_path, keypairs)
    base_tar = Path(fx["base_tar"])
    merge_sha = str(fx["merge_sha"])
    work_dir = tmp_path / "work-base"
    release.cmd_pack(
        tag="gap-release-1",
        remote=str(fx["remote"]),
        base_bundle=base_tar,
        work_dir=work_dir,
        upstream_pub=keypairs["up_pub"],
        gap_pub=keypairs["gap_pub"],
        repo_root=REPO,
        session_dir=Path(fx["session_dir"]),
    )

    def _resign_dir(d: Path, *, key: Path | None = None, pss: bool = False) -> None:
        members = release._expected_members(merge_sha, ())
        sums_bytes = release._format_sha256sums(d, members)
        (d / "SHA256SUMS").write_bytes(sums_bytes)
        priv = key or keypairs["gap_priv"]
        if pss:
            digest = hashlib.sha256(sums_bytes).digest()
            sig = subprocess.run(
                [
                    "openssl", "pkeyutl", "-sign", "-inkey", str(priv),
                    "-pkeyopt", "digest:sha256", "-pkeyopt", "rsa_padding_mode:pss",
                    "-pkeyopt", "rsa_pss_saltlen:digest",
                ],
                input=digest,
                capture_output=True,
                check=True,
            ).stdout
            (d / "SHA256SUMS.sig").write_bytes(sig)
        else:
            _sign_external_vault_style(d / "SHA256SUMS", priv, d / "SHA256SUMS.sig")

    def _clone_out(name: str) -> Path:
        dst = tmp_path / name
        shutil.copytree(work_dir / "out", dst)
        _resign_dir(dst)
        return dst

    # Control: untouched signed dir verifies cleanly
    valid = _clone_out("valid")
    assert release.cmd_verify(
        bundle=valid, base_bundle=base_tar,
        upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO,
    )["sha"] == merge_sha

    # 1. PSS signature refused
    d_pss = _clone_out("tamper-pss")
    _resign_dir(d_pss, pss=True)
    with pytest.raises(release.ReleaseStop, match="PSS padding"):
        release.cmd_verify(bundle=d_pss, base_bundle=base_tar, upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO)

    # 2. Signed by upstream key refused
    d_up = _clone_out("tamper-up-key")
    _resign_dir(d_up, key=keypairs["up_priv"])
    with pytest.raises(release.ReleaseStop, match="verification failed"):
        release.cmd_verify(bundle=d_up, base_bundle=base_tar, upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO)

    # 3. Changed copied member (reference-docs.tar.gz) refused
    d_ref = _clone_out("tamper-refdocs")
    (d_ref / "reference-docs.tar.gz").write_bytes(b"tampered-docs")
    _resign_dir(d_ref)
    with pytest.raises(release.ReleaseStop, match="reference-docs.tar.gz differs from verified base"):
        release.cmd_verify(bundle=d_ref, base_bundle=base_tar, upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO)

    # 4. Wrong gap_tree refused
    d_tree = _clone_out("tamper-tree")
    m = (d_tree / "MANIFEST.txt").read_text()
    old_tree = next(line.split(": ")[1] for line in m.splitlines() if line.startswith("gap_tree: "))
    (d_tree / "MANIFEST.txt").write_text(m.replace(f"gap_tree: {old_tree}", f"gap_tree: {'0' * 40}"))
    _resign_dir(d_tree)
    with pytest.raises(release.ReleaseStop, match="gap_tree"):
        release.cmd_verify(bundle=d_tree, base_bundle=base_tar, upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO)

    # 5. repo.bundle with an extra ref refused
    d_extra_ref = _clone_out("tamper-extra-ref")
    _git(Path(fx["remote"]), "bundle", "create", str(d_extra_ref / "repo.bundle"), "HEAD", "--all")
    _resign_dir(d_extra_ref)
    with pytest.raises(release.ReleaseStop, match="only HEAD"):
        release.cmd_verify(bundle=d_extra_ref, base_bundle=base_tar, upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO)

    # 5b. A gap MANIFEST missing one gap field is refused even when signed.
    d_field = _clone_out("tamper-missing-field")
    m = (d_field / "MANIFEST.txt").read_text()
    (d_field / "MANIFEST.txt").write_text("".join(
        line for line in m.splitlines(keepends=True) if not line.startswith("gap_issue: ")))
    _resign_dir(d_field)
    with pytest.raises(release.ReleaseStop, match="missing or duplicates gap_issue"):
        release.cmd_verify(bundle=d_field, base_bundle=base_tar, upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO)

    # 5c. An upstream bundle offered as the gap candidate is refused: it does not
    #     verify with the gap anchor (its key is the upstream one).
    with pytest.raises(release.ReleaseStop, match="does not match the release trust anchor for origin: gap"):
        release.cmd_verify(bundle=base_tar, base_bundle=base_tar, upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO)

    # 6. repo.bundle with a smuggled unreachable object refused
    d_unreach = _clone_out("tamper-unreachable")
    smuggle = tmp_path / "smuggle.git"
    smuggle.mkdir()
    _git(smuggle, "init", "--bare", "-q")
    _git(smuggle, "fetch", "-q", "--no-tags", str(fx["remote"]), f"{merge_sha}:refs/heads/_p")
    _git(smuggle, "update-ref", "HEAD", merge_sha)
    _git(smuggle, "update-ref", "-d", "refs/heads/_p")
    # Write a stray blob and pack all objects (including the unreachable blob) into a custom bundle.
    blob_oid = subprocess.run(
        ["git", "-C", str(smuggle), "hash-object", "-w", "--stdin"],
        input=b"smuggled-unreachable-payload\n",
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    reach_oids = _git(smuggle, "rev-list", "--objects", merge_sha).stdout.splitlines()
    all_oids = [line.split()[0] for line in reach_oids] + [blob_oid]
    pack_bytes = subprocess.run(
        ["git", "-C", str(smuggle), "pack-objects", "--stdout", "-q"],
        input=("\n".join(all_oids) + "\n").encode(),
        capture_output=True,
        check=True,
    ).stdout
    (d_unreach / "repo.bundle").write_bytes(f"# v2 git bundle\n{merge_sha} HEAD\n\n".encode() + pack_bytes)
    _resign_dir(d_unreach)
    with pytest.raises(release.ReleaseStop, match="unreachable objects"):
        release.cmd_verify(bundle=d_unreach, base_bundle=base_tar, upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO)


def test_inconsistent_oci_archive_is_refused(tmp_path: Path):
    bad_diff = tmp_path / "bad-diff.tar"
    _make_oci_archive(bad_diff, "agent", corrupt_diff_id=True)
    with pytest.raises(release.ReleaseStop, match="diff_id"):
        release.verify_oci_archive_consistency(bad_diff)

    bad_hist = tmp_path / "bad-hist.tar"
    _make_oci_archive(bad_hist, "agent", bad_history=True)
    with pytest.raises(release.ReleaseStop, match="history"):
        release.verify_oci_archive_consistency(bad_hist)


def test_release_sh_cli_contracts(tmp_path: Path, keypairs: dict[str, Path]):
    """CLI contracts for scripts/gap/release.sh: bastion never signs, unverified checkout refused."""
    # 1. `release.sh sign` is explicitly refused (bastion never signs, D3)
    proc_sign = subprocess.run(
        ["sh", str(REPO / "scripts/gap/release.sh"), "sign"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_sign.returncode == 1
    assert "STOP: signing never runs in the bastion" in proc_sign.stderr

    # 2. `release.sh verify` refuses an unverified checkout without CI_REHEARSAL opt-out
    fx = _build_fixture(tmp_path, keypairs)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "SNEAKERNET_TRUSTED_PUB": str(keypairs["up_pub"]),
        "SNEAKERNET_GAP_TRUSTED_PUB": str(keypairs["gap_pub"]),
    }
    proc_unver = subprocess.run(
        [
            "sh", str(REPO / "scripts/gap/release.sh"), "verify",
            "--bundle", str(fx["base_tar"]),
            "--base-bundle", str(fx["base_tar"]),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_unver.returncode == 1
    assert "executing workspace is not a verified signed release" in proc_unver.stderr

    # 3. `release.sh classify` works via python3 -I
    proc_cls = subprocess.run(
        [
            "sh", str(REPO / "scripts/gap/release.sh"), "classify",
            "--repo", str(fx["remote"]),
            "--tag", "gap-release-1",
            "--base-sha", str(fx["base_sha"]),
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_cls.returncode == 0, proc_cls.stderr
    assert json.loads(proc_cls.stdout)["all_allowed"] is True


def test_one_release_identity_per_tag_refuses_a_second_signed_bundle(tmp_path: Path, keypairs: dict[str, Path]):
    """Rev 2 §3e write-once: once a tag's SHA256SUMS is signed, a different
    SHA256SUMS for that tag (here a second, differently encoded repo.bundle of
    the same commit, B3) is refused; the signed release stays assemblable."""
    fx = _build_fixture(tmp_path, keypairs)
    merge_sha = str(fx["merge_sha"])
    work_dir = tmp_path / "work"
    release.cmd_pack(
        tag="gap-release-1", remote=str(fx["remote"]), base_bundle=Path(fx["base_tar"]), work_dir=work_dir,
        upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO,
        session_dir=Path(fx["session_dir"]),
    )
    first_sig = tmp_path / "first.sig"
    _sign_external_vault_style(work_dir / "out" / "SHA256SUMS", keypairs["gap_priv"], first_sig)
    first = release.cmd_assemble(work_dir=work_dir, signature_path=first_sig, upstream_pub=keypairs["up_pub"],
                                 gap_pub=keypairs["gap_pub"], repo_root=REPO)
    kept = tmp_path / "kept-out"
    shutil.copytree(work_dir / "out", kept)

    # A second encoding of the same history gives a different SHA256SUMS.
    alt = tmp_path / "alt.git"
    alt.mkdir()
    _git(alt, "init", "--bare", "-q")
    _git(alt, "fetch", "-q", "--no-tags", str(fx["remote"]), f"{merge_sha}:refs/heads/_p")
    _git(alt, "update-ref", "HEAD", merge_sha)
    _git(alt, "update-ref", "-d", "refs/heads/_p")
    (work_dir / "out" / "SHA256SUMS.sig").unlink()
    _git(alt, "-c", "pack.compression=0", "bundle", "create", str(work_dir / "out" / "repo.bundle"), "HEAD")
    sums = release._format_sha256sums(work_dir / "out", release._expected_members(merge_sha, ()))
    (work_dir / "out" / "SHA256SUMS").write_bytes(sums)
    second_id = release._sha256_bytes(sums)
    assert second_id != first["release_identity"]
    req = work_dir / "request.txt"
    req.write_text(req.read_text().replace(first["release_identity"], second_id), encoding="utf-8")
    second_sig = tmp_path / "second.sig"
    _sign_external_vault_style(work_dir / "out" / "SHA256SUMS", keypairs["gap_priv"], second_sig)
    with pytest.raises(release.ReleaseStop, match="already signed for this tag"):
        release.cmd_assemble(work_dir=work_dir, signature_path=second_sig, upstream_pub=keypairs["up_pub"],
                             gap_pub=keypairs["gap_pub"], repo_root=REPO)
    assert (work_dir / "signed.sha256").read_text().strip() == first["release_identity"]
    assert not (work_dir / "out" / "SHA256SUMS.sig").exists()

    # The next ordinary run with the signed members assembles the same release again.
    shutil.rmtree(work_dir / "out")
    shutil.copytree(kept, work_dir / "out")
    req.write_text(req.read_text().replace(second_id, first["release_identity"]), encoding="utf-8")
    again = release.cmd_assemble(work_dir=work_dir, signature_path=first_sig, upstream_pub=keypairs["up_pub"],
                                 gap_pub=keypairs["gap_pub"], repo_root=REPO)
    assert again["release_identity"] == first["release_identity"]
    assert again["tarball_sha256"] == first["tarball_sha256"]


def test_vendored_subchart_in_candidate_is_refused_by_pack_and_by_verify(
    tmp_path: Path, keypairs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """M1: a ledgered .tgz under charts/mainframe-rag/charts/ is refused by pack,
    and verify refuses a signed bundle whose repo.bundle carries one."""
    fx = _build_fixture(tmp_path, keypairs)
    remote = Path(fx["remote"])
    base_tar = Path(fx["base_tar"])
    _git(remote, "checkout", "-b", "feat/subchart")
    (remote / "charts/mainframe-rag/charts").mkdir()
    (remote / "charts/mainframe-rag/charts/thirdparty-1.0.0.tgz").write_bytes(b"not-a-real-chart")
    _git(remote, "add", "charts/mainframe-rag/charts/thirdparty-1.0.0.tgz")
    _git(remote, "commit", "-m", "Vendor subchart\n\nGap-Patch: GL#50")
    patch_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    ledger = (remote / "gap/patches.md").read_text(encoding="utf-8")
    (remote / "gap/patches.md").write_text(
        ledger + f"\n## GL#50 {patch_sha}\n- path: charts/mainframe-rag/charts/thirdparty-1.0.0.tgz\n",
        encoding="utf-8",
    )
    _git(remote, "add", "gap/patches.md")
    _git(remote, "commit", "-m", "Record GL#50 in ledger")
    _git(remote, "checkout", "main")
    _git(remote, "merge", "--no-ff", "feat/subchart", "-m", "Merge subchart (GL#50)")
    sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    _git(remote, "tag", "-a", "gap-subchart", "-m", "Gap-Release: GL#50", sha)

    def _pack(wd: Path) -> dict[str, str]:
        return release.cmd_pack(
            tag="gap-subchart",
            remote=str(remote),
            base_bundle=base_tar,
            work_dir=wd,
            upstream_pub=keypairs["up_pub"],
            gap_pub=keypairs["gap_pub"],
            repo_root=REPO,
            session_dir=Path(fx["session_dir"]),
        )

    with pytest.raises(release.ReleaseStop) as exc:
        _pack(tmp_path / "w-refused")
    assert str(exc.value) == allowlist.REFUSAL_MESSAGE

    # Force a bundle through pack as a weaker allow-list would have, then verify
    # it under the real allow-list: verify must refuse on its own.
    real = allowlist.classify_paths
    monkeypatch.setattr(allowlist, "classify_paths", lambda paths: real(["docs/deploy.md"]))
    work_dir = tmp_path / "w-forced"
    _pack(work_dir)
    monkeypatch.setattr(allowlist, "classify_paths", real)
    out = work_dir / "out"
    (out / "SHA256SUMS").write_bytes(release._format_sha256sums(out, release._expected_members(sha, ())))
    _sign_external_vault_style(out / "SHA256SUMS", keypairs["gap_priv"], out / "SHA256SUMS.sig")
    with pytest.raises(release.ReleaseStop) as exc:
        release.cmd_verify(
            bundle=out, base_bundle=base_tar,
            upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO,
        )
    assert str(exc.value) == allowlist.REFUSAL_MESSAGE


def _installed_release(root: Path, up_priv: Path) -> Path:
    """A verified signed release workspace as bootstrap leaves it: a git checkout
    of the release scripts whose HEAD equals the signed dist/MANIFEST.txt sha."""
    from tests.helpers_airgap import write_signed_manifest

    for rel in ("scripts/airgap/common.sh", "scripts/airgap/image_manifest.py", "scripts/gap/release.sh",
                "scripts/gap/release.py", "scripts/gap/allowlist.py", "scripts/gap/sign_vault.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "installed release")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    write_signed_manifest(root / "dist", f"sha: {head}\n", key=up_priv)
    return root


def test_release_sh_verify_runs_from_a_verified_installed_release_only(tmp_path: Path, keypairs: dict[str, Path]):
    """Rev 2 §3e step 1 (I9): release.sh runs the release guard on its own
    workspace first, from any working directory; an edited installed release
    is refused before release.py runs, and the restored one verifies again."""
    fx = _build_fixture(tmp_path, keypairs)
    work_dir = tmp_path / "work"
    release.cmd_pack(
        tag="gap-release-1", remote=str(fx["remote"]), base_bundle=Path(fx["base_tar"]), work_dir=work_dir,
        upstream_pub=keypairs["up_pub"], gap_pub=keypairs["gap_pub"], repo_root=REPO,
        session_dir=Path(fx["session_dir"]),
    )
    sig = tmp_path / "SHA256SUMS.sig"
    _sign_external_vault_style(work_dir / "out" / "SHA256SUMS", keypairs["gap_priv"], sig)
    gap_tar = Path(release.cmd_assemble(work_dir=work_dir, signature_path=sig, upstream_pub=keypairs["up_pub"],
                                        gap_pub=keypairs["gap_pub"], repo_root=REPO)["tarball"])
    installed = _installed_release(tmp_path / "installed", keypairs["up_priv"])
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "SNEAKERNET_TRUSTED_PUB": str(keypairs["up_pub"]),
           "SNEAKERNET_GAP_TRUSTED_PUB": str(keypairs["gap_pub"])}

    def verify() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(installed / "scripts/gap/release.sh"), "verify",
             "--bundle", str(gap_tar), "--base-bundle", str(fx["base_tar"])],
            env=env, cwd=tmp_path, capture_output=True, text=True, check=False,
        )

    ok = verify()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert f"VERIFIED {fx['merge_sha']} " in ok.stdout

    with (installed / "scripts/gap/release.py").open("a", encoding="utf-8") as fh:
        fh.write("\n# edited after install\n")
    edited = verify()
    assert edited.returncode == 1
    assert "executing workspace is not a verified signed release" in edited.stderr
    assert "VERIFIED" not in edited.stdout

    _git(installed, "checkout", "--", "scripts/gap/release.py")
    assert verify().returncode == 0


# --- rehearse-check (Rev 2 §3h, D6): the target the deploy will use ----------
#
# Each workspace is a bare tree holding a copy of the shipped common.sh and an
# untracked airgap.env, so the check resolves NAMESPACE, INTERNAL_REGISTRY, KC
# and the kube context through the deploy's own code. The fake kubectl answers
# `config current-context` from a file and lists objects per namespace, logging
# every namespace it is asked about.

_FAKE_KUBECTL = """#!/bin/sh
ctx=${KUBECONFIG:-$FAKE_KUBE_CTX}
if [ "$1" = config ] && [ "$2" = current-context ]; then
    [ -s "$ctx" ] || exit 1
    cat "$ctx"
    exit 0
fi
printf '%s %s %s\n' "$2" "$4" "$(cat "$ctx")" >> "$FAKE_KUBE_LOG"
if [ -n "${FAKE_KUBE_RAW:-}" ]; then
    cat "$FAKE_KUBE_RAW"
    exit 0
fi
if [ -f "$FAKE_KUBE_DIR/$2.json" ]; then cat "$FAKE_KUBE_DIR/$2.json"; else echo '{"items": []}'; fi
"""
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_C = "sha256:" + "c" * 64
LIVE_NS, REH_NS = "mfr-prod", "mfr-reh"
LIVE_REG, REH_REG = "reg.example/mfr", "reg.example/mfr-reh"
CTX = "mfr/api-site-example:6443/operator"


class RehearsalSites:
    """An installed (live) workspace, a rehearsal workspace and a fake cluster."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.kubectl = self.bin / "kubectl"
        self.kubectl.write_text(_FAKE_KUBECTL, encoding="utf-8")
        self.kubectl.chmod(0o755)
        self.ctx = root / "context"
        self.ctx.write_text(CTX + "\n", encoding="utf-8")
        self.kube_dir = root / "cluster"
        self.kube_dir.mkdir()
        self.log = root / "kubectl.log"
        self.installed = self.workspace("installed", NAMESPACE=LIVE_NS, INTERNAL_REGISTRY=LIVE_REG)
        self.rehearsal = self.workspace("rehearsal", NAMESPACE=REH_NS, INTERNAL_REGISTRY=REH_REG)
        self.candidate = root / "cand-MANIFEST.txt"
        self.inst_manifest = root / "inst-MANIFEST.txt"
        self.inst_manifest.write_text(f"qdrant_digest: {_DIGEST_A}\n", encoding="utf-8")
        self.candidate.write_text(f"qdrant_digest: {_DIGEST_A}\n", encoding="utf-8")
        self.env: dict[str, str] = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(root),
            "FAKE_KUBE_CTX": str(self.ctx),
            "FAKE_KUBE_DIR": str(self.kube_dir),
            "FAKE_KUBE_LOG": str(self.log),
        }

    def workspace(self, name: str, **values: str) -> Path:
        ws = self.root / name
        (ws / "scripts" / "airgap").mkdir(parents=True)
        shutil.copy2(REPO / "scripts/airgap/common.sh", ws / "scripts/airgap/common.sh")
        self.write_env(ws, **values)
        return ws

    def write_env(self, ws: Path, **values: str) -> None:
        values.setdefault("KC", str(self.kubectl))
        (ws / "airgap.env").write_text(
            "".join(f"{k}='{v}'\n" for k, v in values.items()), encoding="utf-8"
        )

    def args(self, **typed: str) -> list[str]:
        flags = {
            "rehearsal-workspace": str(self.rehearsal),
            "installed-workspace": str(self.installed),
            "rehearsal-namespace": REH_NS,
            "prod-namespace": LIVE_NS,
            "rehearsal-registry": REH_REG,
            "prod-registry": LIVE_REG,
            "kube-context": CTX,
            "candidate-manifest": str(self.candidate),
            "installed-manifest": str(self.inst_manifest),
        }
        flags.update({k.replace("_", "-"): v for k, v in typed.items()})
        out: list[str] = []
        for key, value in flags.items():
            out += [f"--{key}", value]
        return out

    def run(self, extra_env: dict[str, str] | None = None, **typed: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(REPO / "scripts/gap/release.sh"), "rehearse-check", *self.args(**typed)],
            env={**self.env, **(extra_env or {})},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def queried(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line.split(" ", 1)[0] for line in self.log.read_text(encoding="utf-8").splitlines()]

    def listing_contexts(self) -> set[str]:
        """The kube context each emptiness listing ran against."""
        if not self.log.exists():
            return set()
        return {line.split(" ", 2)[2] for line in self.log.read_text(encoding="utf-8").splitlines()}


@pytest.fixture
def sites(tmp_path: Path) -> RehearsalSites:
    return RehearsalSites(tmp_path)


def _refused(proc: subprocess.CompletedProcess[str], fragment: str) -> None:
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "REHEARSE READY" not in proc.stdout
    assert "Traceback" not in proc.stderr
    assert fragment in proc.stderr, proc.stderr


def test_rehearse_check_ready_reports_the_effective_target(sites: RehearsalSites):
    proc = sites.run()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == (
        f"REHEARSE READY namespace={REH_NS} registry={REH_REG} context={CTX} "
        "qdrant_release=qdrant app_release=mainframe-rag"
    )
    # The emptiness check asked the deploy's KC about the effective namespace only.
    assert set(sites.queried()) == {REH_NS}


def test_rehearse_check_refuses_process_env_namespace_pointing_at_live(sites: RehearsalSites):
    """The reviewer's run (#1336 B1): NAMESPACE=live in the environment wins over the
    rehearsal workspace's airgap.env in the deploy, whatever the operator typed."""
    proc = sites.run(extra_env={"NAMESPACE": LIVE_NS, "INTERNAL_REGISTRY": LIVE_REG})
    _refused(proc, "the rehearsal deploy would target the installed release's namespace")
    assert sites.queried() == []


def test_rehearse_check_refuses_airgap_env_namespace_pointing_at_live(sites: RehearsalSites):
    sites.write_env(sites.rehearsal, NAMESPACE=LIVE_NS, INTERNAL_REGISTRY=REH_REG)
    _refused(sites.run(), "the rehearsal deploy would target the installed release's namespace")


def test_rehearse_check_refuses_selected_env_file_pointing_at_live(sites: RehearsalSites):
    # AIRGAP_ENV selects a file (here the live one) over ./airgap.env in both workspaces.
    proc = sites.run(extra_env={"AIRGAP_ENV": str(sites.installed / "airgap.env")})
    _refused(proc, "the rehearsal deploy would target the installed release's namespace")


def test_rehearse_check_refuses_an_unset_namespace_that_deploy_would_default(sites: RehearsalSites):
    # deploy.sh defaults NAMESPACE to mainframe-rag; the check never accepts the default.
    sites.write_env(sites.rehearsal, INTERNAL_REGISTRY=REH_REG)
    _refused(sites.run(rehearsal_namespace="mainframe-rag"), "NAMESPACE is not set for the rehearsal workspace")
    sites.write_env(sites.rehearsal, NAMESPACE="", INTERNAL_REGISTRY=REH_REG)
    _refused(sites.run(rehearsal_namespace="mainframe-rag"), "NAMESPACE is not set for the rehearsal workspace")


def test_rehearse_check_refuses_typed_values_that_disagree_with_effective_ones(sites: RehearsalSites):
    sites.write_env(sites.rehearsal, NAMESPACE="mfr-reh-2", INTERNAL_REGISTRY=REH_REG)
    _refused(sites.run(), "--rehearsal-namespace does not match")
    sites.write_env(sites.rehearsal, NAMESPACE=REH_NS, INTERNAL_REGISTRY=REH_REG)
    _refused(sites.run(prod_namespace="mfr-other"), "--prod-namespace does not match")
    _refused(sites.run(rehearsal_registry="reg.example/other"), "--rehearsal-registry does not match")
    _refused(sites.run(prod_registry="reg.example/other"), "--prod-registry does not match")
    _refused(sites.run(kube_context="other-context"), "--kube-context does not match")
    _refused(sites.run(rehearsal_namespace=f" {REH_NS}"), "--rehearsal-namespace does not match")


def test_rehearse_check_refuses_an_unresolvable_context_or_registry(sites: RehearsalSites):
    sites.ctx.write_text("", encoding="utf-8")
    _refused(sites.run(), "no current kube context")
    sites.ctx.write_text(CTX + "\n", encoding="utf-8")
    # Helm reads HELM_KUBECONTEXT; kubectl does not: the deploy would split clusters.
    _refused(sites.run(extra_env={"HELM_KUBECONTEXT": "other"}), "HELM_KUBECONTEXT")
    sites.write_env(sites.rehearsal, NAMESPACE=REH_NS)
    _refused(sites.run(), "INTERNAL_REGISTRY is not set for the rehearsal workspace")
    sites.write_env(sites.rehearsal, NAMESPACE=REH_NS, INTERNAL_REGISTRY=REH_REG, KC="kubectl --context x")
    _refused(sites.run(), "KC must name one command")


def test_rehearse_check_honours_helm_kubecontext_exported_from_airgap_env(sites: RehearsalSites):
    """#1336 re-review V5g: helm reads HELM_KUBECONTEXT from what airgap.env
    exports, so the check must too, not only the process environment."""
    with (sites.rehearsal / "airgap.env").open("a", encoding="utf-8") as env_file:
        env_file.write("export HELM_KUBECONTEXT='live-ctx'\n")
    _refused(sites.run(), "HELM_KUBECONTEXT")
    assert sites.queried() == []


def test_rehearse_check_lists_with_the_kubeconfig_exported_from_airgap_env(sites: RehearsalSites):
    """#1336 re-review V5h: KUBECONFIG exported from the rehearsal airgap.env
    selects the deploy's cluster; the emptiness listing runs on that cluster,
    never on the one the operator's shell happens to use."""
    live = sites.root / "live.kubeconfig"
    live.write_text("live-ctx\n", encoding="utf-8")
    with (sites.rehearsal / "airgap.env").open("a", encoding="utf-8") as env_file:
        env_file.write(f"export KUBECONFIG='{live}'\n")
    _refused(sites.run(), "--kube-context does not match")
    proc = sites.run(kube_context="live-ctx")
    assert proc.returncode == 0, proc.stderr
    assert "context=live-ctx" in proc.stdout
    assert sites.listing_contexts() == {"live-ctx"}


@pytest.mark.parametrize(
    "rehearsal_registry", [LIVE_REG, "REG.example/mfr", "reg.example:443/mfr", "reg.example/mfr/"]
)
def test_rehearse_check_refuses_a_shared_registry_when_a_fixed_tag_changes(
    sites: RehearsalSites, rehearsal_registry: str
):
    sites.candidate.write_text(f"qdrant_digest: {_DIGEST_C}\n", encoding="utf-8")
    sites.write_env(sites.rehearsal, NAMESPACE=REH_NS, INTERNAL_REGISTRY=rehearsal_registry)
    _refused(sites.run(rehearsal_registry=rehearsal_registry), "fixed-tag third-party image")
    # The same registry is allowed while every fixed-tag digest is unchanged (D6).
    sites.candidate.write_text(f"qdrant_digest: {_DIGEST_A}\n", encoding="utf-8")
    assert sites.run(rehearsal_registry=rehearsal_registry).returncode == 0


def test_rehearse_check_refuses_the_installed_workspace_itself(sites: RehearsalSites):
    _refused(
        sites.run(rehearsal_workspace=str(sites.installed), rehearsal_namespace=LIVE_NS,
                  rehearsal_registry=LIVE_REG),
        "the rehearsal workspace is the installed workspace",
    )


def test_rehearse_check_refuses_a_non_empty_effective_namespace(sites: RehearsalSites):
    (sites.kube_dir / f"{REH_NS}.json").write_text(
        '{"items": [{"kind": "Deployment", "metadata": {"name": "rag-agent"}}]}', encoding="utf-8"
    )
    _refused(sites.run(), f"target namespace {REH_NS} is not empty")


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "[]", '{"items": {}}', '{"items": ["rag-agent"]}', '{"items": [{"metadata": []}]}'],
)
def test_rehearse_check_refuses_unreadable_cluster_output_with_a_fixed_message(
    sites: RehearsalSites, raw: str
):
    out = sites.root / "raw.json"
    out.write_text(raw, encoding="utf-8")
    _refused(sites.run(extra_env={"FAKE_KUBE_RAW": str(out)}), "STOP:")


def test_rehearse_check_refuses_a_rehearsal_workspace_without_the_deploy_scripts(sites: RehearsalSites):
    _refused(sites.run(rehearsal_workspace=str(sites.root / "cluster")), "not a release workspace")
    # A workspace whose airgap.env makes common.sh die is refused, not trusted.
    (sites.rehearsal / "airgap.env").write_text("AIRGAP_DRYRUN=maybe\n", encoding="utf-8")
    _refused(sites.run(), "cannot resolve the deploy target of the rehearsal workspace")


def test_rehearse_check_resolver_is_the_deploys_only_target_resolution():
    """The resolver sources common.sh and repeats one deploy.sh line (KC). Pin that
    nothing else in scripts/airgap assigns the target variables, so a new override
    in the deploy path cannot leave rehearse-check resolving something else."""
    assigns = re.compile(r"^\s*(?:export\s+)?(NAMESPACE|INTERNAL_REGISTRY|QDRANT_RELEASE|KC)=(.*)$")
    found = set()
    for path in sorted((REPO / "scripts/airgap").glob("*.sh")):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = assigns.match(line)
            if match:
                found.add((path.name, match.group(1), match.group(2).strip()))
    deploy_side = {(n, v, a) for n, v, a in found if n in ("common.sh", "deploy.sh", "load.sh", "pipeline.sh")}
    assert deploy_side == {
        ("common.sh", "INTERNAL_REGISTRY", "${INTERNAL_REGISTRY:-}"),
        ("common.sh", "NAMESPACE", "${NAMESPACE:-mainframe-rag}"),
        ("common.sh", "QDRANT_RELEASE", "${QDRANT_RELEASE:-qdrant}"),
        ("deploy.sh", "KC", "${KC:-$(kc)}"),
        ("pipeline.sh", "KC", "${KC:-$(kc)}"),
    }
    assert "KC=${KC:-$(kc)}" in release._RESOLVE_DEPLOY_TARGET_SH
    assert "resolve_aliases" in release._RESOLVE_DEPLOY_TARGET_SH


def test_member_set_and_manifest_fields_match_pack_sh(tmp_path: Path, keypairs: dict[str, Path]):
    """The member inventory and MANIFEST fields here are pinned to what pack.sh
    signs (derived independently from pack.sh by scripts/extension_surface.py),
    so a real upstream base bundle is not refused and the fixture stays faithful."""
    from scripts import extension_surface

    layout = extension_surface.bundle_layout(REPO)
    asset = next(line.split(":", 1)[1].strip()
                 for line in (REPO / "scripts/tools/task-pin.txt").read_text(encoding="utf-8").splitlines()
                 if line.startswith("asset:"))
    sha = "a" * 40
    packed = {item.split(" ", 1)[1].replace("{IMAGE_SHA}", sha).replace("{TASK_ASSET}", asset)
              for item in layout if item.startswith("member ")}
    assert set(release._expected_members(sha, release.OPTIONAL_IMAGE_MEMBERS)) == packed - {
        "SHA256SUMS", "SHA256SUMS.sig"}
    assert set(release.OPTIONAL_IMAGE_MEMBERS) <= packed

    fields = {item.split(" ", 1)[1] for item in layout if item.startswith("manifest ")}
    rendered = release.render_gap_manifest(
        "", sha=sha, commit_date="d", gap_base=sha, gap_issue="GL#1", gap_tag="t", gap_tree=sha,
        ingest_digest="x", ingest_config_digest="x", agent_digest="x", agent_config_digest="x")
    assert {line.split(": ", 1)[0] for line in rendered.splitlines()} <= fields

    fx = _build_fixture(tmp_path, keypairs)
    base_keys = set(release.parse_manifest((Path(fx["base_dir"]) / "MANIFEST.txt").read_text())[0])
    optional = {f for f in fields if f.startswith(("oauth_proxy", "openwebui"))}
    assert base_keys == fields - optional - set(release.GAP_MANIFEST_FIELDS)
