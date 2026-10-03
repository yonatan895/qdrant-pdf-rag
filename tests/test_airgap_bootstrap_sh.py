"""scripts/airgap/bootstrap.sh sneakernet extraction and setup tests (issue #15).

Tests bootstrap.sh against a mock sneakernet extraction directory:
- Missing SHA256SUMS fails closed.
- Checksum mismatch fails closed.
- Successful verification clones the bundle, populates dist/, and initializes airgap.env.
"""

import os
import shutil
import subprocess

import pytest

from tests.helpers_airgap import (
    REPO,
    gen_other_pub,
    gen_sign_keypair,
    sha256_bytes,
    sign_sums,
    symlink_tools,
)
from tests.helpers_task_artifact import copy_task_tools, task_manifest, task_members


def _sha256(data: bytes) -> str:
    return sha256_bytes(data)


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
    copy_task_tools(src_repo)
    (src_repo / "Taskfile.yml").write_text("version: '3'\ntasks:\n  probe:\n    cmds: ['printf offline-task-ok']\n")
    (src_repo / "README.md").write_text("Hello")
    (src_repo / "airgap.env.example").write_text("INTERNAL_REGISTRY=example\n")
    subprocess.run(["git", "add", "."], cwd=src_repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=src_repo, check=True)
    subprocess.run(["git", "bundle", "create", str(extract_dir / "repo.bundle"), "HEAD", "--all"], cwd=src_repo, check=True)

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=src_repo, text=True).strip()

    # Add mock image files
    gen_sign_keypair(extract_dir)
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
        f"app-ingest-{head}.tar": b"mock-ingest",
        f"app-agent-{head}.tar": b"mock-agent",
        "oauth-proxy-image.tar": b"mock-oauth-proxy",
        "MANIFEST.txt": (f"sha: {head}\n" + task_manifest()).encode(),
        **task_members(),
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
    sign_sums(extract_dir)

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


def test_bootstrap_trusted_pub_mismatch_refuses(bundle_dir):
    other_pub = gen_other_pub(bundle_dir)
    env = {
        "PATH": "/usr/bin:/bin",
        "AIRGAP_WORKSPACE": "workspace",
        "SNEAKERNET_TRUSTED_PUB": str(other_pub),
    }
    r = subprocess.run(
        ["sh", "bootstrap.sh"], cwd=bundle_dir, capture_output=True, text=True, env=env, check=False
    )
    assert r.returncode != 0
    assert "does not match SNEAKERNET_TRUSTED_PUB" in r.stderr


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
    assert len(list(dist_dir.glob("app-agent-*.tar"))) == 1
    assert (dist_dir / "task_linux_amd64.tar.gz").is_file()
    assert (repo_dir / ".tools/bin/task").is_file()
    assert (dist_dir / "oauth-proxy-image.tar").is_file()
    assert (dist_dir / "MANIFEST.txt").is_file()
    assert (dist_dir / "PACKING_RECORD.txt").is_file()
    assert (dist_dir / "sbom.json").is_file()
    assert (dist_dir / "sneakernet-signing.pub").is_file()
    assert (dist_dir / "SHA256SUMS.sig").is_file()

    # The dist/ copy must stay verifiable: every SHA256SUMS member (now
    # including oauth-proxy-image.tar) landed, so `sha256sum -c` passes there.
    verify = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"],
        cwd=dist_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_signed_handoff_executes_workflow_with_shipped_model_modes(bundle_dir):
    """Run the workflow body with synthetic signed images and relocated CI tools."""
    import yaml

    from tests.helpers_airgap import install_rendering_helm, write_stub

    source = bundle_dir.parent / "src_repo"
    for relative in ("scripts/airgap", "scripts/tools", "taskfiles", "charts"):
        shutil.copytree(REPO / relative, source / relative, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for relative in ("Taskfile.yml", "airgap.env.example", "images.txt", "scripts/qdrant_pin.py"):
        shutil.copy(REPO / relative, source / relative)
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Synthetic handoff source"], cwd=source,
                   check=True, capture_output=True)
    old_sha = (bundle_dir / "MANIFEST.txt").read_text().splitlines()[0].split()[1]
    candidate_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    subprocess.run(["git", "bundle", "create", str(bundle_dir / "repo.bundle"), "HEAD", "--all"],
                   cwd=source, check=True, capture_output=True)
    for kind in ("agent", "ingest"):
        (bundle_dir / f"app-{kind}-{old_sha}.tar").rename(bundle_dir / f"app-{kind}-{candidate_sha}.tar")
    for name in ("MANIFEST.txt", "SHA256SUMS"):
        path = bundle_dir / name
        path.write_text(path.read_text().replace(old_sha, candidate_sha))
    (bundle_dir / "sbom.json").write_text('{"images": [], "tools": [{"name": "go-task/task"}]}\n')
    _resign(bundle_dir)
    gapbox = bundle_dir.parent / "gapbox"
    gapbox.mkdir()
    archive = gapbox / f"qdrant-pdf-rag-{candidate_sha}.tar"
    members = [path.name for path in bundle_dir.iterdir() if path.is_file() and path.name != "signing.key"]
    subprocess.run(["tar", "cf", str(archive), *members], cwd=bundle_dir, check=True, capture_output=True)
    (gapbox / f"{archive.name}.sha256").write_text(f"{_sha256(archive.read_bytes())}  {archive.name}\n")
    tools = bundle_dir.parent / "tools"
    (tools / "bin").mkdir(parents=True)
    for name in ("skopeo", "kubectl", "oc"):
        write_stub(tools / "bin" / name, "#!/bin/sh\nexit 0\n")
    install_rendering_helm(tools)
    workflow = yaml.safe_load((REPO / ".github/workflows/e2e.yml").read_text())
    command = next(step["run"] for step in workflow["jobs"]["airgap-acceptance"]["steps"]
                   if step.get("name", "").startswith("Black-box handoff"))
    command = command.replace('export PATH="/usr/local/bin:$PATH"', 'export PATH="$HANDOFF_TOOLS:$PATH"')
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=bundle_dir.parent,
                            env={"PATH": f"{tools / 'bin'}:" + os.environ["PATH"],
                                 "HANDOFF_TOOLS": str(tools / "bin"),
                                 "SHA": candidate_sha, "IMAGE_SHA": candidate_sha},
                            capture_output=True, text=True, check=False)
    (bundle_dir.parent / "handoff-workflow.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PIPELINE DRY-RUN COMPLETE") == 2
    assert "EMBED_BASE_URL:    http://gateway:4000/v1" in result.stdout
    assert "EMBED_BASE_URL:    http://vllm:8000/v1" in result.stdout
    checkout = gapbox / "qdrant-pdf-rag"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip() == candidate_sha
    assert (checkout / "dist/MANIFEST.txt").read_text().startswith(f"sha: {candidate_sha}\n")
    values = yaml.safe_load((checkout / "dist/mainframe-rag-release-values.yaml").read_text())
    assert values["models"]["reasoning"]["model"] == ""
    assert values["models"]["embedding"]["baseUrl"] == "http://vllm:8000/v1"
    assert values["pullSecret"]["name"] == "acceptance-pull"


def _offline_env(bundle_dir):
    # Deliberately omit Make, Task, Go, Python, curl and all app tools.
    bin_dir = bundle_dir / "bin"
    bin_dir.mkdir(exist_ok=True)
    symlink_tools(bundle_dir, (
        "sh", "sha256sum", "openssl", "cmp", "sed", "awk", "git", "uname",
        "mkdir", "cp", "chmod", "tar", "gzip", "mktemp", "mv", "rm", "grep", "dirname",
    ))
    return {"PATH": str(bin_dir), "AIRGAP_WORKSPACE": "operator workspace"}


def _bootstrap(bundle_dir, env=None):
    return subprocess.run(
        ["sh", "bootstrap.sh"], cwd=bundle_dir, capture_output=True, text=True,
        env=env or _offline_env(bundle_dir), check=False,
    )


def _resign(bundle_dir):
    names = [line.split()[1] for line in (bundle_dir / "SHA256SUMS").read_text().splitlines()]
    (bundle_dir / "SHA256SUMS").write_text("".join(
        f"{_sha256((bundle_dir / name).read_bytes())}  {name}\n" for name in names
    ))
    sign_sums(bundle_dir)


def test_bootstrap_actual_runner_without_build_or_app_tools_and_rerun(bundle_dir):
    env = _offline_env(bundle_dir)
    for name in ("make", "task", "go", "python", "python3", "curl"):
        assert shutil.which(name, path=env["PATH"]) is None
    first = _bootstrap(bundle_dir, env)
    assert first.returncode == 0, first.stdout + first.stderr
    workspace = bundle_dir / "operator workspace"
    run = subprocess.run(
        [str(workspace / ".tools/bin/task"), "--taskfile", str(workspace / "Taskfile.yml"), "probe"],
        cwd=workspace, env=env, capture_output=True, text=True, check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "offline-task-ok"
    assert 'sh scripts/tools/run-task.sh airgap:validate' in first.stdout
    (workspace / "airgap.env").write_text("INTERNAL_REGISTRY=operator-kept\n")
    (workspace / "dist/retained-evidence.txt").write_text("operator artifact")
    (workspace / "private-note.txt").write_text("operator note")
    # Bootstrap must replace, never probe, an untrusted old executable.
    marker = workspace / "unverified-executable-ran"
    (workspace / ".tools/bin/task").write_text(f'#!/bin/sh\nprintf executed > "{marker}"\n')
    rerun = _bootstrap(bundle_dir, env)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert (workspace / "airgap.env").read_text() == "INTERNAL_REGISTRY=operator-kept\n"
    assert (workspace / "dist/retained-evidence.txt").read_text() == "operator artifact"
    assert (workspace / "private-note.txt").read_text() == "operator note"
    assert not marker.exists()


@pytest.mark.parametrize("damage", ["corrupt", "missing", "omitted-checksum", "pin-mismatch", "manifest-mismatch"])
def test_bootstrap_task_tampering_fails_before_install(bundle_dir, damage):
    archive = bundle_dir / "task_linux_amd64.tar.gz"
    if damage == "corrupt":
        archive.write_bytes(b"not the approved executable archive")
        _resign(bundle_dir)  # Outer signature alone does not establish the upstream pin.
    elif damage == "missing":
        archive.unlink()
    elif damage == "omitted-checksum":
        sums = bundle_dir / "SHA256SUMS"
        sums.write_text("".join(line for line in sums.read_text().splitlines(keepends=True) if "task_linux_amd64.tar.gz" not in line))
        archive.unlink()
        sign_sums(bundle_dir)
    elif damage == "pin-mismatch":
        pin = bundle_dir / "task-pin.txt"
        pin.write_text(pin.read_text().replace("v3.53.1", "v3.53.2"))
        _resign(bundle_dir)
    else:
        manifest = bundle_dir / "MANIFEST.txt"
        manifest.write_text(manifest.read_text().replace("task_platform: linux-amd64", "task_platform: linux-arm64"))
        _resign(bundle_dir)
    result = _bootstrap(bundle_dir)
    assert result.returncode != 0
    assert not (bundle_dir / "operator workspace/.tools/bin/task").exists()
    assert "SUCCESS" not in result.stdout


def test_bootstrap_wrong_host_fails_before_install(bundle_dir):
    env = _offline_env(bundle_dir)
    uname = bundle_dir / "bin/uname"
    uname.unlink()
    uname.write_text('#!/bin/sh\ncase "$1" in -s) echo Linux;; *) echo aarch64;; esac\n')
    uname.chmod(0o755)
    result = _bootstrap(bundle_dir, env)
    assert result.returncode != 0
    assert "unsupported platform" in result.stderr
    assert not (bundle_dir / "operator workspace").exists()


def test_bootstrap_refuses_modified_installer(bundle_dir):
    assert _bootstrap(bundle_dir).returncode == 0
    workspace = bundle_dir / "operator workspace"
    # Raw comparison must hold even if Git's working-tree cache hides edits.
    subprocess.run(["git", "update-index", "--assume-unchanged", "scripts/tools/install-task.sh"], cwd=workspace, check=True)
    marker = workspace / "executed-unverified-installer"
    (workspace / "scripts/tools/install-task.sh").write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    result = _bootstrap(bundle_dir)
    assert result.returncode != 0
    assert "tool scripts differ" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("stage", [False, True], ids=["unstaged", "staged"])
def test_bootstrap_refuses_tracked_edit_then_rerun_passes_after_revert(bundle_dir, stage):
    """Equal HEAD with any tracked edit (here a non-installer file) is not
    the signed release: refuse before copying artifacts, never touch the
    operator's work, and pass again once the edit is reverted (issue #414)."""
    assert _bootstrap(bundle_dir).returncode == 0
    workspace = bundle_dir / "operator workspace"
    (workspace / "airgap.env").write_text("INTERNAL_REGISTRY=untracked-operator-file\n")
    (workspace / "dist/retained-evidence.txt").write_text("original")
    (workspace / "dist/MANIFEST.txt").unlink()
    (workspace / "README.md").write_text("locally edited chart/script stand-in\n")
    if stage:
        subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
    refused = _bootstrap(bundle_dir)
    assert refused.returncode != 0
    assert "tracked changes against the approved commit" in refused.stderr
    assert "locally edited" not in refused.stdout + refused.stderr
    assert "SUCCESS" not in refused.stdout
    assert not (workspace / "dist/MANIFEST.txt").exists()
    assert (workspace / "README.md").read_text() == "locally edited chart/script stand-in\n"
    subprocess.run(["git", "reset", "-q", "HEAD", "--", "README.md"], cwd=workspace, check=True)
    subprocess.run(["git", "checkout", "--", "README.md"], cwd=workspace, check=True)
    again = _bootstrap(bundle_dir)
    assert again.returncode == 0, again.stdout + again.stderr
    assert (workspace / "dist/MANIFEST.txt").exists()
    assert (workspace / "airgap.env").read_text() == "INTERNAL_REGISTRY=untracked-operator-file\n"
    assert (workspace / "dist/retained-evidence.txt").read_text() == "original"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_bootstrap_refuses_index_flags_that_hide_edits_then_rerun_passes(bundle_dir, flag):
    """An edit under assume-unchanged/skip-worktree is invisible to
    `git diff`; the flag is refused before dist/ changes and the next run
    passes once cleared (issue #414)."""
    assert _bootstrap(bundle_dir).returncode == 0
    workspace = bundle_dir / "operator workspace"
    subprocess.run(["git", "update-index", flag, "README.md"], cwd=workspace, check=True)
    (workspace / "README.md").write_text("hidden edit\n")
    (workspace / "dist/MANIFEST.txt").unlink()
    refused = _bootstrap(bundle_dir)
    assert refused.returncode != 0
    assert "hides tracked files from change detection" in refused.stderr
    assert "hidden edit" not in refused.stdout + refused.stderr
    assert "SUCCESS" not in refused.stdout
    assert not (workspace / "dist/MANIFEST.txt").exists()
    assert (workspace / "README.md").read_text() == "hidden edit\n"
    subprocess.run(["git", "update-index", flag.replace("--", "--no-"), "README.md"], cwd=workspace, check=True)
    subprocess.run(["git", "checkout", "--", "README.md"], cwd=workspace, check=True)
    again = _bootstrap(bundle_dir)
    assert again.returncode == 0, again.stdout + again.stderr


def test_bootstrapped_workspace_passes_then_refuses_in_the_real_guard(bundle_dir):
    """The dist/ chain bootstrap leaves is exactly what the downstream guard
    verifies (real signature, real clone): pass when clean, refuse once the
    committed MANIFEST is altered or a tracked edit hides behind an index
    flag (issue #414)."""
    assert _bootstrap(bundle_dir).returncode == 0
    workspace = bundle_dir / "operator workspace"

    def guard():
        return subprocess.run(
            ["sh", "-c", f'. "{REPO}/scripts/airgap/common.sh"; cd "$1" || exit 9; MANIFEST=dist/MANIFEST.txt; check_checkout_sha',
             "guard", str(workspace)],
            cwd=workspace, capture_output=True, text=True, check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": str(bundle_dir)},
        )

    ok = guard()
    assert ok.returncode == 0, ok.stderr
    assert ok.stderr == ""  # verified silently, not the "not release-verified" notice
    manifest = workspace / "dist/MANIFEST.txt"
    original = manifest.read_text()
    manifest.write_text(original + "extra: line\n")
    bad = guard()
    assert bad.returncode != 0 and "signed checksum entry" in bad.stderr
    manifest.write_text(original)
    subprocess.run(["git", "update-index", "--assume-unchanged", "README.md"], cwd=workspace, check=True)
    (workspace / "README.md").write_text("hidden\n")
    hidden = guard()
    assert hidden.returncode != 0 and "hides tracked files" in hidden.stderr


def test_bootstrap_approved_upgrade_requires_explicit_checkout_preserves_operator_state(bundle_dir):
    assert _bootstrap(bundle_dir).returncode == 0
    workspace = bundle_dir / "operator workspace"
    (workspace / "airgap.env").write_text("INTERNAL_REGISTRY=kept-during-upgrade\n")
    (workspace / "dist/retained-evidence.txt").write_text("original")
    source = bundle_dir.parent / "src_repo"
    old_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    (source / "README.md").write_text("Next approved release")
    subprocess.run(["git", "commit", "-am", "Next approved release"], cwd=source, check=True, capture_output=True)
    new_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    subprocess.run(["git", "bundle", "create", str(bundle_dir / "repo.bundle"), "HEAD", "--all"], cwd=source, check=True)
    manifest = bundle_dir / "MANIFEST.txt"
    manifest.write_text(manifest.read_text().replace(old_sha, new_sha))
    for kind in ("agent", "ingest"):
        shutil.copy(bundle_dir / f"app-{kind}-{old_sha}.tar", bundle_dir / f"app-{kind}-{new_sha}.tar")
    sums = bundle_dir / "SHA256SUMS"
    sums.write_text(sums.read_text().replace(old_sha, new_sha))
    _resign(bundle_dir)
    # The approved objects may already be fetched; HEAD still names the old release.
    subprocess.run(["git", "fetch", str(bundle_dir / "repo.bundle"), "HEAD"], cwd=workspace, check=True, capture_output=True)
    refused = _bootstrap(bundle_dir)
    assert refused.returncode != 0
    assert "workspace HEAD does not match" in refused.stderr
    assert (workspace / "dist/MANIFEST.txt").read_text().startswith(f"sha: {old_sha}\n")
    subprocess.run(["git", "fetch", str(bundle_dir / "repo.bundle"), "HEAD"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "--detach", new_sha], cwd=workspace, check=True, capture_output=True)
    upgraded = _bootstrap(bundle_dir)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert (workspace / "README.md").read_text() == "Next approved release"
    assert (workspace / "dist/MANIFEST.txt").read_text().startswith(f"sha: {new_sha}\n")
    assert (workspace / "airgap.env").read_text() == "INTERNAL_REGISTRY=kept-during-upgrade\n"
    assert (workspace / "dist/retained-evidence.txt").read_text() == "original"
    assert (workspace / f"dist/app-agent-{old_sha}.tar").exists()
    probe = subprocess.run([str(workspace / ".tools/bin/task"), "probe"], cwd=workspace, env=_offline_env(bundle_dir), capture_output=True, text=True, check=False)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout == "offline-task-ok"


# --- Issue #414: interrupted / failed artifact staging ---------------------------------------

STAGING_DIRNAME = ".bootstrap-staging"

_CP_SHIM = """#!/bin/sh
# Test shim: behaves like cp except for one planned fault on the member named by
# FAULT_MEMBER, fired once (FAULT_MODE: truncate | kill | corrupt).
src=""
for a in "$@"; do case "$a" in -*) ;; *) src=$a; break;; esac; done
for a in "$@"; do last=$a; done
if [ "${src##*/}" = "${FAULT_MEMBER:-}" ] && [ ! -e "$FAULT_MARK" ]; then
    : > "$FAULT_MARK"
    [ -d "$last" ] && out="$last/${src##*/}" || out="$last"
    case "$FAULT_MODE" in
        corrupt) printf 'silently-corrupted' > "$out"; exit 0 ;;
        *) head -c 5 "$src" > "$out" ;;
    esac
    [ "$FAULT_MODE" = kill ] && kill -9 "$PPID"
    exit 1
fi
exec "${REAL_CP:-/usr/bin/cp}" "$@"
"""

_MV_SHIM = """#!/bin/sh
# Test shim: dies (like a power cut) just before moving FAULT_MEMBER, once.
src=""
for a in "$@"; do case "$a" in -*) ;; *) src=$a; break;; esac; done
if [ "${src##*/}" = "${FAULT_MEMBER:-}" ] && [ ! -e "$FAULT_MARK" ]; then
    : > "$FAULT_MARK"
    kill -9 "$PPID"
    exit 1
fi
exec "${REAL_MV:-/usr/bin/mv}" "$@"
"""


def _fault_env(bundle_dir, tool, member, mode="kill"):
    env = _offline_env(bundle_dir)
    shim = bundle_dir / "bin" / tool
    real = shim.resolve()
    shim.unlink()
    shim.write_text(_CP_SHIM if tool == "cp" else _MV_SHIM)
    shim.chmod(0o755)
    env.update({
        "FAULT_MEMBER": member, "FAULT_MODE": mode,
        "FAULT_MARK": str(bundle_dir / f"fault-fired-{tool}"),
        "REAL_CP" if tool == "cp" else "REAL_MV": str(real),
    })
    return env


def _snapshot(workspace):
    """sha256 of every accepted file in dist/ (not the refused staging leftover)
    plus airgap.env: proves what did not change."""
    snap = {}
    for path in sorted((workspace / "dist").rglob("*")):
        if path.is_file() and STAGING_DIRNAME not in path.parts:
            snap[str(path.relative_to(workspace))] = _sha256(path.read_bytes())
    env_file = workspace / "airgap.env"
    snap["airgap.env"] = _sha256(env_file.read_bytes()) if env_file.exists() else None
    return snap


def _dist_matches_bundle(bundle_dir, workspace):
    names = [line.split()[1] for line in (bundle_dir / "SHA256SUMS").read_text().splitlines()]
    names += ["SHA256SUMS", "SHA256SUMS.sig"]
    return all((workspace / "dist" / n).read_bytes() == (bundle_dir / n).read_bytes() for n in names)


@pytest.mark.parametrize("mode", ["truncate", "kill", "corrupt"])
def test_bootstrap_failed_copy_leaves_existing_dist_and_env_untouched(bundle_dir, mode):
    """A failed, killed or silently corrupting artifact copy must not alter a
    previously accepted dist/ or the operator's airgap.env (issue #414)."""
    assert _bootstrap(bundle_dir).returncode == 0
    workspace = bundle_dir / "operator workspace"
    (workspace / "airgap.env").write_text("INTERNAL_REGISTRY=operator-kept\n")
    (workspace / "dist/retained-evidence.txt").write_text("operator artifact")
    before = _snapshot(workspace)
    failed = _bootstrap(bundle_dir, _fault_env(bundle_dir, "cp", "sbom.json", mode))
    assert failed.returncode != 0
    assert "SUCCESS" not in failed.stdout
    assert _snapshot(workspace) == before
    leftover = workspace / "dist" / STAGING_DIRNAME
    if mode == "kill":
        # An unclean death cannot clean up: the leftover is refused, never accepted.
        assert leftover.is_dir()
        refused = _bootstrap(bundle_dir)
        assert refused.returncode != 0
        assert "interrupted bootstrap staging" in refused.stderr
        assert "SUCCESS" not in refused.stdout
        assert _snapshot(workspace) == before
        shutil.rmtree(leftover)  # the documented operator action
    else:
        assert not leftover.exists()
    again = _bootstrap(bundle_dir)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _dist_matches_bundle(bundle_dir, workspace)
    assert not leftover.exists()
    assert (workspace / "airgap.env").read_text() == "INTERNAL_REGISTRY=operator-kept\n"
    assert (workspace / "dist/retained-evidence.txt").read_text() == "operator artifact"


@pytest.mark.parametrize("tool,member", [("cp", "sbom.json"), ("mv", "SHA256SUMS")], ids=["during-copy", "during-commit"])
def test_bootstrap_killed_first_run_never_leaves_accepted_bundle(bundle_dir, tool, member):
    """First run, process dies mid-staging or mid-commit: dist/ must not look
    like an accepted bundle (no MANIFEST/checksum list), a rerun is refused with
    a fixed message until the leftover is removed, then completes (issue #414)."""
    killed = _bootstrap(bundle_dir, _fault_env(bundle_dir, tool, member, "kill"))
    assert killed.returncode != 0
    workspace = bundle_dir / "operator workspace"
    assert not (workspace / "dist/MANIFEST.txt").exists()
    assert not (workspace / "dist/SHA256SUMS").exists()
    leftover = workspace / "dist" / STAGING_DIRNAME
    if tool == "cp":
        assert leftover.is_dir()
        refused = _bootstrap(bundle_dir)
        assert refused.returncode != 0
        assert "interrupted bootstrap staging" in refused.stderr
        assert not (workspace / "dist/MANIFEST.txt").exists()
        shutil.rmtree(leftover)
    else:
        # Mid-commit: staged bytes are verified; the leftover is still refused.
        assert leftover.is_dir()
        assert _bootstrap(bundle_dir).returncode != 0
        shutil.rmtree(leftover)
    again = _bootstrap(bundle_dir)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _dist_matches_bundle(bundle_dir, workspace)
