#!/usr/bin/env python3
"""Gap release pipeline Increment 1: classify, pack, assemble, verify, rehearse-check
(GH#1207 Rev 2 D1–D12, ADR-0009 §5).

Trust path with zero replaced layers (Increment 1):
  - ``classify``: classify every path in ``gap_base..sha`` against the D2 allow-list.
  - ``pack``: pack an unsigned ``origin: gap`` bundle in the bastion (refuses cluster
    token, registry auth, or signing key in the session; fetches only the tag and
    ``origin/main`` into a bare repo with hooks disabled; checks annotated tag,
    ``Gap-Release: GL#<n>``, first-parent merge commit on ``origin/main`` (D4),
    ``Upstream-Release:`` ancestry, ledger proof, D2 allow-list, OCI archive
    consistency, and write-once member generation; copies ``reference-docs.tar.gz``
    unchanged per D7; prints release identity = ``sha256(SHA256SUMS)`` per D10).
  - ``assemble``: after external signing (Vault transit on workstation or ephemeral
    ``sign`` pod, D3), verify ``SHA256SUMS.sig`` against ``SNEAKERNET_GAP_TRUSTED_PUB``,
    enforce write-once ``signed.sha256`` (one release identity per tag), and write a
    deterministic ``qdrant-pdf-rag-<sha>.tar`` + ``.sha256``.
  - ``verify``: trusted pre-promotion verification (Rev 2 §3e 6-step order) using the
    installed signed release's code: inventory, gap anchor signature, ``repo.bundle``
    content identity (single ``HEAD`` ref, no unreachable objects, exact ``gap_tree``),
    base upstream bundle verification, allow-list & ledger proof, and per-member
    reproduction/equality checks.
  - ``rehearse-check``: enforce D6 rehearsal-namespace guards (separate namespace from
    installed release, no changed fixed-tag third-party image on a shared registry path,
    empty rehearsal namespace).

Signing NEVER runs in this tool or in the bastion (use ``scripts/gap/sign_vault.py``
on the workstation or in the ephemeral F9 ``sign`` pod).

Standard library only (runs under ``python3 -I``).
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

try:
    from scripts.airgap import image_manifest as oci
    from scripts.gap import allowlist, sign_vault
except ModuleNotFoundError:
    _GAP_DIR = Path(__file__).resolve().parent
    _SCRIPTS_DIR = _GAP_DIR.parent
    sys.path.append(str(_GAP_DIR))
    sys.path.append(str(_SCRIPTS_DIR / "airgap"))
    import allowlist as _local_allowlist
    import image_manifest as _local_oci
    import sign_vault as _local_sign_vault

    allowlist = _local_allowlist
    oci = _local_oci
    sign_vault = _local_sign_vault

# Extension-surface gap manifest fields (ADR-0009 §5 / §9, GH#1207 Rev 2 §3e).
GAP_MANIFEST_FIELDS: tuple[str, ...] = ("gap_base", "gap_issue", "gap_tag", "gap_tree")

OPTIONAL_IMAGE_MEMBERS: tuple[str, ...] = (
    "oauth-proxy-image.tar",
    "openwebui-image.tar",
    "openwebui-proxy-image.tar",
)

# Third-party images loaded under fixed registry tags (load.sh); a rehearsal on a
# shared registry path must not change what a restarted live pod pulls (D6).
FIXED_TAG_IMAGE_KEYS: tuple[str, ...] = (
    "qdrant_digest",
    "openobserve_digest",
    "otelcol_digest",
    "oauth_proxy_digest",
    "openwebui_digest",
    "openwebui_proxy_digest",
)

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_TAG_NAME = re.compile(r"^[A-Za-z0-9._/-]+$")
_GAP_ISSUE = re.compile(r"^GL#[0-9]+$")
_LEDGER_HEADING = re.compile(r"^## (GL#[0-9]+) ([0-9a-f]{40})$")
_GIT_ENV_STRIP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
)


class ReleaseStop(ValueError):
    """Fixed ``STOP: ...`` refusal (never leaks tokens or secrets)."""

    def __init__(self, message: str) -> None:
        text = message if message.startswith("STOP:") else f"STOP: {message}"
        super().__init__(text)


@dataclass(frozen=True)
class AnchorSet:
    upstream_path: Path
    upstream_fp: str
    gap_path: Path
    gap_fp: str


@dataclass(frozen=True)
class VerifiedBaseBundle:
    directory: Path
    sha: str
    manifest_text: str
    manifest: dict[str, str]
    sums: dict[str, str]
    optional_images: tuple[str, ...]


def _clean_git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_STRIP}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_PAGER"] = "cat"
    env["GIT_EDITOR"] = "false"
    env["LC_ALL"] = "C"
    return env


# A fetch from GitLab or an unpack of the whole history can take minutes on the
# bastion; past this bound the step stops with a fixed message, never a traceback.
GIT_TIMEOUT_S = 1800


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
    stdin: IO[bytes] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            stdin=stdin,
            env=_clean_git_env(),
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise ReleaseStop(f"git {' '.join(args[:2])} did not finish in {GIT_TIMEOUT_S} s") from None
    if check and proc.returncode != 0:
        raise ReleaseStop(f"git {' '.join(args[:2])} failed")
    return proc


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.partial.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        if mode is not None:
            tmp.chmod(mode)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _write_once_bytes(out_dir: Path, name: str, data: bytes, *, mode: int = 0o644) -> None:
    """Write a member atomically into out_dir/name if absent; if present, verify exact equality."""
    target = out_dir / name
    if target.is_symlink():
        raise ReleaseStop(f"output member {name} is a link; refusing to overwrite or trust it")
    if target.exists():
        if not target.is_file() or target.read_bytes() != data:
            raise ReleaseStop(
                f"output member {name} already exists with different bytes; refuse to overwrite (remove incomplete work dir or start a new tag)"
            )
        return
    partial = out_dir.parent / "out.partial"
    partial.mkdir(parents=True, exist_ok=True)
    tmp = partial / f"{name}.tmp.{os.getpid()}"
    try:
        tmp.write_bytes(data)
        tmp.chmod(mode)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _write_once_copy(out_dir: Path, name: str, src: Path, expected_sha256: str) -> None:
    """Copy a verified member from src into out_dir/name write-once."""
    target = out_dir / name
    if target.is_symlink():
        raise ReleaseStop(f"output member {name} is a link; refusing to overwrite or trust it")
    if target.exists():
        if not target.is_file() or _sha256_file(target) != expected_sha256:
            raise ReleaseStop(
                f"output member {name} already exists with different checksum; refuse to overwrite"
            )
        return
    partial = out_dir.parent / "out.partial"
    partial.mkdir(parents=True, exist_ok=True)
    tmp = partial / f"{name}.tmp.{os.getpid()}"
    try:
        shutil.copyfile(src, tmp)
        tmp.chmod(0o644)
        if _sha256_file(tmp) != expected_sha256:
            raise ReleaseStop(f"copied member {name} failed post-copy checksum check")
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _is_inside(candidate: Path, parent: Path) -> bool:
    try:
        candidate.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_anchors(
    upstream_pub: Path | None,
    gap_pub: Path | None,
    *,
    forbidden_roots: Iterable[Path] = (),
    require_gap_rsa3072: bool = False,
) -> AnchorSet:
    """Validate both release trust anchors (plain files, no links, outside artifacts, distinct keys)."""
    if upstream_pub is None or not str(upstream_pub):
        raise ReleaseStop("SNEAKERNET_TRUSTED_PUB is required (no trust on first use)")
    if gap_pub is None or not str(gap_pub):
        raise ReleaseStop("SNEAKERNET_GAP_TRUSTED_PUB is required (no trust on first use)")

    checked: list[tuple[str, Path, str]] = []
    for var_name, path in (("SNEAKERNET_TRUSTED_PUB", upstream_pub), ("SNEAKERNET_GAP_TRUSTED_PUB", gap_pub)):
        if path.is_symlink() or not path.is_file():
            raise ReleaseStop(f"release trust anchor {var_name} is not a readable plain file (not a link)")
        for root in forbidden_roots:
            if root.exists() and _is_inside(path, root):
                raise ReleaseStop(
                    f"release trust anchor {var_name} lies inside the checkout, bundle, or work directory"
                )
        try:
            raw = path.read_bytes()
        except OSError:
            raise ReleaseStop(f"release trust anchor {var_name} is unreadable") from None
        try:
            if var_name == "SNEAKERNET_GAP_TRUSTED_PUB" and require_gap_rsa3072:
                sign_vault.normalize_public_key_pem(raw, require_rsa_3072=True)
            fp = sign_vault.pubkey_fingerprint_sha256(path)
        except sign_vault.SignError as exc:
            raise ReleaseStop(str(exc)) from None
        checked.append((var_name, path, fp))

    if checked[0][2] == checked[1][2]:
        raise ReleaseStop(
            "release trust anchors SNEAKERNET_TRUSTED_PUB and SNEAKERNET_GAP_TRUSTED_PUB hold the same key"
        )
    return AnchorSet(
        upstream_path=checked[0][1],
        upstream_fp=checked[0][2],
        gap_path=checked[1][1],
        gap_fp=checked[1][2],
    )


def check_pack_session(session_dir: Path, env: Mapping[str, str] | None = None) -> None:
    """Enforce Rev 2 §3b / D3 session separation on the bastion during pack."""
    environ = os.environ if env is None else env
    if environ.get("SNEAKERNET_SIGNING_KEY"):
        raise ReleaseStop(
            "bastion pack refused: SNEAKERNET_SIGNING_KEY is set — the bastion never holds a signing key (Rev 2 D3)"
        )
    priv_dir = session_dir / "private"
    if (priv_dir / "kubeconfig").exists() or (priv_dir / "kubeconfig").is_symlink():
        raise ReleaseStop(
            "bastion pack refused: cluster session kubeconfig is present — pack runs without cluster credentials"
        )
    if (priv_dir / "auth.json").exists() or (priv_dir / "auth.json").is_symlink():
        raise ReleaseStop(
            "bastion pack refused: registry session auth.json is present — pack runs without registry credentials"
        )
    for var_name in ("KUBECONFIG", "REGISTRY_AUTH_FILE"):
        val = environ.get(var_name, "").strip()
        if val and Path(val).exists():
            raise ReleaseStop(
                f"bastion pack refused: {var_name} points to an active credential file — pack runs without cluster or registry credentials"
            )


def _expected_members(sha: str, optional_images: Sequence[str]) -> list[str]:
    members = [
        "bootstrap.sh",
        "repo.bundle",
        "reference-docs.tar.gz",
        "task_linux_amd64.tar.gz",
        "task-pin.txt",
        "task-LICENSE",
        "qdrant-image.tar",
        "openobserve-image.tar",
        "otelcol-image.tar",
        f"app-ingest-{sha}.tar",
        f"app-agent-{sha}.tar",
    ]
    for opt in OPTIONAL_IMAGE_MEMBERS:
        if opt in optional_images:
            members.append(opt)
    members.extend([
        "MANIFEST.txt",
        "PACKING_RECORD.txt",
        "sbom.json",
        "THIRD-PARTY-NOTICES.txt",
        "sneakernet-signing.pub",
    ])
    return members


def parse_manifest(text: str) -> tuple[dict[str, str], dict[str, int]]:
    """Parse key: value MANIFEST lines and count occurrences per key."""
    values: dict[str, str] = {}
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if ": " not in line:
            raise ReleaseStop("MANIFEST.txt contains a malformed line")
        key, value = line.split(": ", 1)
        counts[key] = counts.get(key, 0) + 1
        values[key] = value
    return values, counts


def check_manifest_gap_consistency(manifest_text: str, expected_origin: str) -> dict[str, str]:
    values, counts = parse_manifest(manifest_text)
    if counts.get("origin", 0) != 1 or values.get("origin") != expected_origin:
        raise ReleaseStop(f"MANIFEST.txt must declare exactly one origin: {expected_origin}")
    if expected_origin == "upstream":
        for field in GAP_MANIFEST_FIELDS:
            if counts.get(field, 0) != 0:
                raise ReleaseStop(
                    "MANIFEST.txt declares origin: upstream but carries gap release metadata (gap_base/gap_issue/gap_tag/gap_tree)"
                )
    elif expected_origin == "gap":
        for field in GAP_MANIFEST_FIELDS:
            if counts.get(field, 0) != 1:
                raise ReleaseStop(
                    f"MANIFEST.txt declares origin: gap but is missing or duplicates {field}"
                )
        if not _HEX40.fullmatch(values["gap_base"]):
            raise ReleaseStop("MANIFEST.txt gap_base is not a 40-hex commit SHA")
        if not _HEX40.fullmatch(values["gap_tree"]):
            raise ReleaseStop("MANIFEST.txt gap_tree is not a 40-hex tree ID")
        if not _GAP_ISSUE.fullmatch(values["gap_issue"]):
            raise ReleaseStop("MANIFEST.txt gap_issue must match GL#<digits>")
        if not _TAG_NAME.fullmatch(values["gap_tag"]):
            raise ReleaseStop("MANIFEST.txt gap_tag has invalid characters")
    else:
        raise ReleaseStop("unknown expected release origin")
    return values


def parse_sha256sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            raise ReleaseStop("SHA256SUMS contains a blank line")
        parts = line.split("  ")
        if len(parts) != 2 or not _HEX64.fullmatch(parts[0]) or not _PLAIN_NAME.fullmatch(parts[1]):
            raise ReleaseStop("SHA256SUMS contains a malformed line")
        if parts[1] in sums:
            raise ReleaseStop(f"SHA256SUMS lists {parts[1]} more than once")
        sums[parts[1]] = parts[0]
    return sums


def inspect_and_unpack_tarball(tarball: Path, dest: Path) -> list[str]:
    """Verify tarball inventory (regular files only, plain names, no duplicates) before unpacking."""
    if tarball.is_symlink() or not tarball.is_file():
        raise ReleaseStop("bundle tarball is not a readable plain file")
    sha_sidecar = Path(str(tarball) + ".sha256")
    if sha_sidecar.is_file():
        lines = sha_sidecar.read_text(encoding="utf-8").splitlines()
        if len(lines) != 1:
            raise ReleaseStop("bundle .sha256 sidecar must contain exactly one line")
        parts = lines[0].split()
        if len(parts) != 2 or parts[1] != tarball.name or not _HEX64.fullmatch(parts[0]):
            raise ReleaseStop("bundle .sha256 sidecar is malformed")
        if _sha256_file(tarball) != parts[0]:
            raise ReleaseStop("bundle tarball does not match its .sha256 file")

    names: list[str] = []
    seen: set[str] = set()
    try:
        with tarfile.open(tarball, "r:") as tf:
            for member in tf.getmembers():
                if not member.isfile() or member.issym() or member.islnk():
                    raise ReleaseStop(
                        "bundle tarball holds a non-regular entry (link, directory, or device)"
                    )
                if not _PLAIN_NAME.fullmatch(member.name) or member.name in (".", ".."):
                    raise ReleaseStop(
                        "bundle tarball holds a member with an unsafe path or unusual character"
                    )
                if member.name in seen:
                    raise ReleaseStop("bundle tarball holds a duplicate member name")
                seen.add(member.name)
                names.append(member.name)
            dest.mkdir(parents=True, exist_ok=True)
            for member in tf.getmembers():
                src = tf.extractfile(member)
                if src is None:
                    raise ReleaseStop(f"cannot extract {member.name} from tarball")
                # Image members are gigabytes: stream them, never hold one in memory.
                with src, (dest / member.name).open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
    except tarfile.TarError:
        raise ReleaseStop("cannot read bundle tarball") from None
    return names


def check_directory_inventory(bundle_dir: Path) -> list[str]:
    """Check an unpacked bundle directory has only plain regular files (no links or subdirs)."""
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise ReleaseStop("bundle path is not a readable directory")
    names: list[str] = []
    for entry in sorted(bundle_dir.iterdir(), key=lambda p: p.name):
        if entry.is_symlink() or not entry.is_file():
            raise ReleaseStop("bundle directory holds a non-regular entry (a link, directory or device)")
        if not _PLAIN_NAME.fullmatch(entry.name):
            raise ReleaseStop("bundle directory holds a member with an unsafe name or unusual character")
        names.append(entry.name)
    return names


def verify_oci_archive_consistency(archive_path: Path) -> tuple[str, str, int]:
    """Rev 2 §3a condition 3: verify OCI archive descriptors, uncompressed layer diff_ids,
    and non-empty history count. Returns (manifest_digest, config_digest, layer_count)."""
    try:
        with tarfile.open(archive_path, "r:") as outer:
            image = oci.read_archive(outer)
            config = json.loads(image.config_raw)
            rootfs = config.get("rootfs")
            if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
                raise ReleaseStop("OCI image config rootfs is invalid")
            diff_ids = rootfs.get("diff_ids")
            if not isinstance(diff_ids, list) or len(diff_ids) != len(image.layers) or not diff_ids:
                raise ReleaseStop("OCI image diff_ids count does not match manifest layers")
            for layer_digest, diff_id in zip(image.layers, diff_ids, strict=True):
                if not isinstance(diff_id, str) or not oci.DIGEST.fullmatch(diff_id):
                    raise ReleaseStop("OCI image config has an invalid diff_id")
                stream = oci.open_blob(outer, image, layer_digest)
                with stream:
                    magic = stream.read(2)
                    stream.seek(0)
                    reader = gzip.GzipFile(fileobj=stream) if magic == b"\x1f\x8b" else stream
                    h = hashlib.sha256()
                    while block := reader.read(1024 * 1024):
                        h.update(block)
                    if "sha256:" + h.hexdigest() != diff_id:
                        raise ReleaseStop(
                            "OCI image archive is inconsistent: uncompressed layer bytes do not match config diff_id"
                        )
            history = config.get("history")
            if not isinstance(history, list):
                raise ReleaseStop("OCI image config has no history list")
            non_empty = sum(1 for item in history if isinstance(item, dict) and not item.get("empty_layer", False))
            if non_empty != len(diff_ids):
                raise ReleaseStop(
                    "OCI image archive is inconsistent: non-empty history entries do not match layer count"
                )
            config_digest = oci.manifest_digest(image.config_raw)
            return image.digest, config_digest, len(diff_ids)
    except ReleaseStop:
        raise
    except (oci.ArchiveError, oci.BlobMismatch, tarfile.TarError, ValueError, KeyError, OSError):
        raise ReleaseStop(f"OCI image archive {archive_path.name} failed structural or digest verification") from None


def _verify_bundle_dir_common(
    bundle_dir: Path,
    inventory: Sequence[str],
    *,
    expected_origin: str,
    anchor_path: Path,
) -> tuple[str, str, dict[str, str], dict[str, str], tuple[str, ...]]:
    for req in ("SHA256SUMS", "SHA256SUMS.sig", "sneakernet-signing.pub", "MANIFEST.txt"):
        if req not in inventory:
            raise ReleaseStop(f"bundle is missing required member {req}")

    # 1. Verify signature directly against the anchor file (never the bundled pubkey alone).
    if (bundle_dir / "sneakernet-signing.pub").read_bytes() != anchor_path.read_bytes():
        raise ReleaseStop(
            f"bundle pubkey does not match the release trust anchor for origin: {expected_origin}"
        )
    try:
        sign_vault.verify_signature_openssl(
            anchor_path,
            bundle_dir / "SHA256SUMS",
            bundle_dir / "SHA256SUMS.sig",
        )
    except sign_vault.SignError as exc:
        raise ReleaseStop(str(exc)) from None

    # 2. Parse SHA256SUMS and enforce exact inventory match (no extra or missing files).
    sums = parse_sha256sums((bundle_dir / "SHA256SUMS").read_text(encoding="utf-8"))
    if "MANIFEST.txt" not in sums:
        raise ReleaseStop("SHA256SUMS does not list MANIFEST.txt")
    if _sha256_file(bundle_dir / "MANIFEST.txt") != sums["MANIFEST.txt"]:
        raise ReleaseStop("MANIFEST.txt does not match its signed SHA256SUMS entry")

    manifest_text = (bundle_dir / "MANIFEST.txt").read_text(encoding="utf-8")
    manifest = check_manifest_gap_consistency(manifest_text, expected_origin)
    sha = manifest.get("sha", "")
    if not _HEX40.fullmatch(sha):
        raise ReleaseStop("MANIFEST.txt sha is not a 40-hex commit SHA")

    optional_present = tuple(opt for opt in OPTIONAL_IMAGE_MEMBERS if opt in sums)
    expected = _expected_members(sha, optional_present)
    if set(sums) != set(expected):
        raise ReleaseStop("SHA256SUMS member set does not equal the required bundle inventory")
    if set(inventory) != set(expected) | {"SHA256SUMS", "SHA256SUMS.sig"}:
        raise ReleaseStop("bundle holds an unlisted member or is missing a required member")

    # 3. Verify every member's sha256.
    for name, want_hash in sums.items():
        if _sha256_file(bundle_dir / name) != want_hash:
            raise ReleaseStop(f"bundle member {name} does not match its signed SHA256SUMS entry")

    return sha, manifest_text, manifest, sums, optional_present


def load_verified_base_bundle(
    base_bundle: Path,
    anchors: AnchorSet,
    scratch: Path,
) -> VerifiedBaseBundle:
    """Verify the base upstream bundle (tarball or unpacked dir) against SNEAKERNET_TRUSTED_PUB."""
    if base_bundle.is_file():
        unpacked = scratch / "base-bundle"
        inventory = inspect_and_unpack_tarball(base_bundle, unpacked)
        bundle_dir = unpacked
    else:
        bundle_dir = base_bundle
        inventory = check_directory_inventory(bundle_dir)

    sha, manifest_text, manifest, sums, optional_present = _verify_bundle_dir_common(
        bundle_dir,
        inventory,
        expected_origin="upstream",
        anchor_path=anchors.upstream_path,
    )
    # Check repo.bundle HEAD equals base sha.
    heads = _git(bundle_dir, "bundle", "list-heads", str(bundle_dir / "repo.bundle"), "HEAD").stdout.decode("utf-8").splitlines()
    if len(heads) != 1 or heads[0].split()[0] != sha:
        raise ReleaseStop("base bundle repo.bundle HEAD does not match its MANIFEST sha")

    return VerifiedBaseBundle(
        directory=bundle_dir,
        sha=sha,
        manifest_text=manifest_text,
        manifest=manifest,
        sums=sums,
        optional_images=optional_present,
    )


def verify_ledger_proof(repo: Path, base_sha: str, target_sha: str) -> list[str]:
    """Prove outside gap/ and .gitlab/ the tree at target_sha equals base_sha except
    exactly the paths listed in gap/patches.md (same rule as import_release.sh step 7).
    Returns sorted list of changed upstream-owned paths."""
    changed_raw = _git(
        repo,
        "diff",
        "--no-renames",
        "-z",
        "--name-only",
        base_sha,
        target_sha,
        "--",
        ".",
        ":(exclude)gap",
        ":(exclude).gitlab",
    ).stdout
    changed_paths = sorted({p.decode("utf-8", "surrogateescape") for p in changed_raw.split(b"\x00") if p})

    has_ledger = _git(repo, "cat-file", "-e", f"{target_sha}:gap/patches.md", check=False).returncode == 0
    ledger_paths: set[str] = set()
    if has_ledger:
        ledger_text = _git(repo, "show", f"{target_sha}:gap/patches.md").stdout.decode("utf-8")
        seen_commits: set[str] = set()
        # split("\n"), not splitlines(): import_release.sh reads the ledger with
        # sed and awk, which keep a CR or other separator as part of the line.
        for lineno, line in enumerate(ledger_text.split("\n"), 1):
            if line.lstrip().startswith("##"):
                m = _LEDGER_HEADING.fullmatch(line)
                if not m:
                    raise ReleaseStop(
                        f'gap/patches.md line {lineno} is not a ledger heading "## GL#NNN <full commit sha>"'
                    )
                commit_sha = m.group(2)
                if commit_sha in seen_commits:
                    raise ReleaseStop(f"gap/patches.md lists gap patch {commit_sha} twice")
                seen_commits.add(commit_sha)
                is_anc = _git(repo, "merge-base", "--is-ancestor", commit_sha, target_sha, check=False).returncode == 0
                if not is_anc:
                    raise ReleaseStop(f"gap/patches.md names {commit_sha}, which is not an ancestor of {target_sha}")
            elif line.startswith("- path: "):
                ledger_paths.add(line[len("- path: "):])

    if sorted(ledger_paths) != changed_paths:
        raise ReleaseStop(
            "upstream-owned paths differing from gap_base do not match gap/patches.md — ledger every upstream-owned change or revert it on main"
        )
    return changed_paths


def verify_repo_bundle_content(bundle_path: Path, expected_sha: str, expected_tree: str, scratch: Path) -> Path:
    """Verify repo.bundle content identity (D10):
    1. Only one ref: HEAD -> expected_sha, and no prerequisite commits.
    2. Unpack raw packfile into a fresh empty bare repo so EVERY object in the
       bundle (reachable or not) is loaded, then prove all_objects == reachable_from_sha.
    3. Prove expected_sha^{tree} == expected_tree.
    Returns path to the unpacked bare repository for downstream git reads.
    """
    bare = scratch / "bundle-repo.git"
    if bare.exists():
        shutil.rmtree(bare)
    bare.mkdir(parents=True)
    _git(bare, "init", "--bare", "-q")
    _git(bare, "config", "core.hooksPath", "/dev/null")

    # Read the header line by line and stream the packfile to unpack-objects:
    # the bundle holds the whole history and is never read into memory at once.
    with bundle_path.open("rb") as fh:
        first = fh.readline(256)
        if first not in (b"# v2 git bundle\n", b"# v3 git bundle\n"):
            raise ReleaseStop("repo.bundle is not a valid git bundle header")
        header_lines: list[str] = []
        while True:
            line = fh.readline(4096)
            if not line or not line.endswith(b"\n") or len(header_lines) > 64:
                raise ReleaseStop("repo.bundle header is malformed")
            if line == b"\n":
                break
            header_lines.append(line[:-1].decode("utf-8", "replace"))
        if any(line.startswith("-") for line in header_lines):
            raise ReleaseStop("repo.bundle has prerequisite commits (not self-contained)")
        ref_lines = [line for line in header_lines if not line.startswith("@")]
        if ref_lines != [f"{expected_sha} HEAD"]:
            raise ReleaseStop(
                "repo.bundle must contain only HEAD pointing to the MANIFEST sha (no extra branches or tags)"
            )
        pack_start = fh.tell()
        if fh.read(4) != b"PACK":
            raise ReleaseStop("repo.bundle does not contain a valid git packfile")
    # A buffered reader's descriptor offset is past its read-ahead: hand the
    # child an unbuffered descriptor positioned at the packfile.
    with bundle_path.open("rb", buffering=0) as raw:
        raw.seek(pack_start)
        unpack = _git(bare, "unpack-objects", "-q", check=False, stdin=raw)
    if unpack.returncode != 0:
        raise ReleaseStop("repo.bundle packfile could not be unpacked cleanly")

    # Check commit exists and its tree equals gap_tree.
    got_commit = _git(bare, "rev-parse", "--verify", f"{expected_sha}^{{commit}}", check=False)
    if got_commit.returncode != 0 or got_commit.stdout.decode().strip() != expected_sha:
        raise ReleaseStop("repo.bundle does not contain the declared commit")
    got_tree = _git(bare, "rev-parse", "--verify", f"{expected_sha}^{{tree}}").stdout.decode().strip()
    if got_tree != expected_tree:
        raise ReleaseStop("repo.bundle commit tree does not match MANIFEST gap_tree")

    # Every object unpacked from the bundle MUST be reachable from expected_sha.
    all_objs_out = _git(bare, "cat-file", "--batch-check=%(objectname)", "--batch-all-objects").stdout.decode()
    all_objs = {line.strip() for line in all_objs_out.splitlines() if line.strip()}
    reach_out = _git(bare, "rev-list", "--objects", expected_sha).stdout.decode()
    reachable = {line.split()[0] for line in reach_out.splitlines() if line.strip()}
    if all_objs != reachable:
        raise ReleaseStop("repo.bundle contains unreachable objects outside the tagged commit history")

    # Attach HEAD; the object store must pass a strict fsck and the standard
    # `git bundle verify` (Rev 2 §3e, repo.bundle row).
    _git(bare, "update-ref", "HEAD", expected_sha)
    if _git(bare, "fsck", "--strict", "--no-progress", check=False).returncode != 0:
        raise ReleaseStop("repo.bundle objects fail git fsck --strict")
    if _git(bare, "bundle", "verify", str(bundle_path), check=False).returncode != 0:
        raise ReleaseStop("git bundle verify failed on repo.bundle")
    return bare


def create_clean_repo_bundle(source_bare: Path, target_sha: str, out_bundle: Path, scratch: Path) -> None:
    """Create repo.bundle holding ONLY target_sha's reachable history under HEAD (D10)."""
    isolated = scratch / "pack-only-repo.git"
    if isolated.exists():
        shutil.rmtree(isolated)
    isolated.mkdir(parents=True)
    _git(isolated, "init", "--bare", "-q")
    _git(isolated, "config", "core.hooksPath", "/dev/null")
    # Fetch ONLY target_sha into a single temporary ref, attach HEAD, drop temp ref, bundle HEAD.
    _git(isolated, "fetch", "-q", "--no-tags", str(source_bare), f"{target_sha}:refs/heads/_pack")
    _git(isolated, "update-ref", "HEAD", target_sha)
    _git(isolated, "update-ref", "-d", "refs/heads/_pack")
    _git(isolated, "bundle", "create", str(out_bundle), "HEAD")


def _commit_timestamp(repo: Path, sha: str) -> tuple[int, str]:
    ct = int(_git(repo, "show", "-s", "--format=%ct", sha).stdout.decode().strip())
    iso = datetime.datetime.fromtimestamp(ct, tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ct, iso


def render_gap_manifest(
    base_manifest_text: str,
    *,
    sha: str,
    commit_date: str,
    gap_base: str,
    gap_issue: str,
    gap_tag: str,
    gap_tree: str,
    ingest_digest: str,
    ingest_config_digest: str,
    agent_digest: str,
    agent_config_digest: str,
    signed_label: str = "true",
) -> str:
    """Deterministic MANIFEST.txt derived from base MANIFEST with a closed changed-line set (Rev 2 §3e)."""
    ingest_ref = f"gap/qdrant-pdf-rag-ingest:{sha}"
    agent_ref = f"gap/qdrant-pdf-rag-agent:{sha}"
    replacements = {
        "sha": sha,
        "date": commit_date,
        "ingest": ingest_ref,
        "ingest_digest": ingest_digest,
        "ingest_config_digest": ingest_config_digest,
        "agent": agent_ref,
        "agent_digest": agent_digest,
        "agent_config_digest": agent_config_digest,
        "app_registry": "gap",
        "signed": signed_label,
        "origin": "gap",
    }
    out_lines: list[str] = []
    seen: set[str] = set()
    for line in base_manifest_text.splitlines():
        if ": " not in line:
            continue
        key, _val = line.split(": ", 1)
        if key in GAP_MANIFEST_FIELDS:
            raise ReleaseStop("base MANIFEST unexpectedly contains gap metadata")
        if key in replacements:
            out_lines.append(f"{key}: {replacements[key]}")
            seen.add(key)
        else:
            out_lines.append(line)
    for key, val in replacements.items():
        if key not in seen:
            out_lines.append(f"{key}: {val}")
    out_lines.extend([
        f"gap_base: {gap_base}",
        f"gap_issue: {gap_issue}",
        f"gap_tag: {gap_tag}",
        f"gap_tree: {gap_tree}",
    ])
    return "\n".join(out_lines) + "\n"


def render_gap_packing_record(
    *,
    sha: str,
    commit_date: str,
    gap_base: str,
    gap_issue: str,
    gap_tag: str,
    gap_tree: str,
    gap_anchor_fp: str,
    upstream_anchor_fp: str,
    base_manifest: Mapping[str, str],
    optional_images: Sequence[str],
    replaced_layers: int = 0,
) -> str:
    """Deterministic PACKING_RECORD.txt (no wall clock, D7 reference-docs note)."""
    lines = [
        "=" * 80,
        "                   MAINFRAME RAG — GAP RELEASE PACKING RECORD",
        "=" * 80,
        f"Commit SHA:       {sha}",
        f"Commit Date:      {commit_date}",
        "Release Origin:   gap",
        f"Gap Tag:          {gap_tag}",
        f"Gap Issue:        {gap_issue}",
        f"Gap Base SHA:     {gap_base}",
        f"Gap Tree ID:      {gap_tree}",
        f"Replaced Layers:  {replaced_layers}",
        "",
        "TRUST ANCHOR FINGERPRINTS (SHA-256 DER):",
        f"  - Upstream anchor: {upstream_anchor_fp}",
        f"  - Gap anchor:      {gap_anchor_fp}",
        "",
        "PACKAGED ARTIFACTS:",
        f"  - Task host tool:      {base_manifest.get('task_version', 'unknown')} (linux-amd64), {base_manifest.get('task_asset', 'task_linux_amd64.tar.gz')}",
        f"  - Git Bundle:          repo.bundle (tagged commit history only, HEAD={sha})",
        f"  - Qdrant Image:        {base_manifest.get('qdrant', '')} (copied from base)",
        f"  - OpenObserve Image:   {base_manifest.get('openobserve', '')} (copied from base)",
        f"  - otelcol-contrib:     {base_manifest.get('otelcol', '')} (copied from base)",
    ]
    if "oauth-proxy-image.tar" in optional_images:
        lines.append(f"  - oauth-proxy Image:   {base_manifest.get('oauth_proxy', '')} (copied from base)")
    if "openwebui-image.tar" in optional_images:
        lines.append(f"  - Open WebUI Image:    {base_manifest.get('openwebui', '')} (copied from base)")
    if "openwebui-proxy-image.tar" in optional_images:
        lines.append(f"  - Open WebUI proxy:    {base_manifest.get('openwebui_proxy', '')} (copied from base)")
    lines.extend([
        f"  - Ingest Image:        gap/qdrant-pdf-rag-ingest:{sha} (base archive reused, 0 layers replaced)",
        f"  - Agent Image:         gap/qdrant-pdf-rag-agent:{sha} (base archive reused, 0 layers replaced)",
        f"  - Helm Chart:          {base_manifest.get('chart', '')}",
        f"  - Reference docs:      reference-docs.tar.gz (copied unchanged from base upstream release {gap_base}; D7)",
        "  - Third-party notices: THIRD-PARTY-NOTICES.txt (copied unchanged from base upstream release)",
        "  - Bundle signature:    SHA256SUMS.sig (signed outside the bastion; pubkey sneakernet-signing.pub)",
        "=" * 80,
    ])
    return "\n".join(lines) + "\n"


def render_gap_sbom(
    base_sbom_bytes: bytes,
    *,
    sha: str,
    commit_date: str,
    gap_base: str,
    gap_issue: str,
    gap_tag: str,
    ingest_digest: str,
    agent_digest: str,
) -> bytes:
    """Deterministic sbom.json derived from verified base sbom.json with committer date."""
    try:
        sbom = json.loads(base_sbom_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReleaseStop("base bundle sbom.json is malformed") from None
    if not isinstance(sbom, dict):
        raise ReleaseStop("base bundle sbom.json is not a JSON object")
    sbom["generated"] = commit_date
    sbom["image_sha"] = sha
    sbom["origin"] = "gap"
    sbom["gap_base"] = gap_base
    sbom["gap_issue"] = gap_issue
    sbom["gap_tag"] = gap_tag
    images = sbom.get("images")
    if isinstance(images, list):
        for entry in images:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") == "app-ingest":
                entry["ref"] = f"gap/qdrant-pdf-rag-ingest:{sha}"
                entry["digest"] = ingest_digest
            elif entry.get("name") == "app-agent":
                entry["ref"] = f"gap/qdrant-pdf-rag-agent:{sha}"
                entry["digest"] = agent_digest
    return (json.dumps(sbom, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _extract_gap_issue_from_tag(repo: Path, tag: str) -> str:
    obj_type = _git(repo, "cat-file", "-t", f"refs/tags/{tag}", check=False).stdout.decode().strip()
    if obj_type != "tag":
        raise ReleaseStop(
            f"tag {tag} must be an annotated tag carrying Gap-Release: GL#<n> (lightweight tags are refused)"
        )
    msg = _git(repo, "for-each-ref", "--format=%(contents)", f"refs/tags/{tag}").stdout.decode("utf-8", "replace")
    found = [line.split("Gap-Release: ", 1)[1].strip() for line in msg.splitlines() if line.startswith("Gap-Release: ")]
    if len(found) != 1 or not _GAP_ISSUE.fullmatch(found[0]):
        raise ReleaseStop(
            f"annotated tag {tag} must contain exactly one 'Gap-Release: GL#<digits>' line"
        )
    return found[0]


def _verify_tag_merge_commit_and_base(repo: Path, tag: str, base_sha: str) -> tuple[str, str, str]:
    """Validate annotated tag, Gap-Release issue, first-parent merge commit on origin/main (D4),
    and base upstream ancestry."""
    gap_issue = _extract_gap_issue_from_tag(repo, tag)
    sha = _git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").stdout.decode().strip()
    if not _HEX40.fullmatch(sha):
        raise ReleaseStop(f"tag {tag} does not resolve to a commit")
    tree = _git(repo, "rev-parse", "--verify", f"{sha}^{{tree}}").stdout.decode().strip()

    # First-parent chain of origin/main check (§3c / D4).
    fp_chain = _git(repo, "rev-list", "--first-parent", "refs/remotes/origin/main").stdout.decode().splitlines()
    if sha not in fp_chain:
        raise ReleaseStop(
            f"tagged commit {sha} is not on the first-parent chain of origin/main — tag only a merge commit on protected main"
        )

    # Merge-commit check (D4): commit must have >= 2 parents.
    parents_line = _git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.decode().strip().split()
    if len(parents_line) < 3:
        raise ReleaseStop(
            f"tagged commit {sha} is not a merge commit (found {len(parents_line) - 1} parent) — D4 requires a merge commit on main"
        )

    # Base upstream ancestry check.
    if _git(repo, "merge-base", "--is-ancestor", base_sha, sha, check=False).returncode != 0:
        raise ReleaseStop(
            f"tagged commit {sha} does not descend from base upstream release {base_sha}"
        )

    # Newest Upstream-Release trailer on sha's history must equal base_sha (if any exists).
    trailers_out = _git(
        repo,
        "log",
        "-1",
        "--format=%B",
        "--grep=^Upstream-Release: ",
        sha,
    ).stdout.decode("utf-8", "replace")
    trailers = [
        line.split("Upstream-Release: ", 1)[1].strip()
        for line in trailers_out.splitlines()
        if line.startswith("Upstream-Release: ")
    ]
    if trailers and trailers[-1] != base_sha:
        raise ReleaseStop(
            f"base bundle sha ({base_sha}) does not match the newest Upstream-Release trailer ({trailers[-1]}) on {sha}"
        )

    return sha, tree, gap_issue


def _fetch_tag_and_main(remote: str, tag: str, bare: Path) -> None:
    if not bare.exists():
        bare.mkdir(parents=True, exist_ok=True)
        _git(bare, "init", "--bare", "-q")
        _git(bare, "config", "core.hooksPath", "/dev/null")
    # Fetch ONLY the named tag and refs/heads/main (no arbitrary branch refs).
    _git(
        bare,
        "fetch",
        "-q",
        "--no-tags",
        remote,
        f"+refs/tags/{tag}:refs/tags/{tag}",
        "+refs/heads/main:refs/remotes/origin/main",
    )


def _format_sha256sums(out_dir: Path, members: Sequence[str]) -> bytes:
    lines = [f"{_sha256_file(out_dir / name)}  {name}" for name in members]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _read_kv_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def cmd_classify(
    *,
    repo: str,
    tag: str | None,
    sha: str | None,
    base_sha: str,
    as_json: bool = False,
) -> tuple[int, dict[str, object]]:
    """Classify the cumulative delta base_sha..target against the D2 allow-list."""
    if not _HEX40.fullmatch(base_sha):
        raise ReleaseStop("--base-sha must be a 40-hex commit SHA")
    with tempfile.TemporaryDirectory(prefix="mfr-gap-classify-") as td:
        tmp = Path(td)
        bare = tmp / "repo.git"
        bare.mkdir()
        _git(bare, "init", "--bare", "-q")
        _git(bare, "config", "core.hooksPath", "/dev/null")
        if tag:
            if not _TAG_NAME.fullmatch(tag):
                raise ReleaseStop("invalid --tag name")
            _git(bare, "fetch", "-q", "--no-tags", repo, f"+refs/tags/{tag}:refs/tags/{tag}")
            target = _git(bare, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").stdout.decode().strip()
        elif sha and _HEX40.fullmatch(sha):
            _git(bare, "fetch", "-q", "--no-tags", repo, f"{sha}:refs/heads/_target")
            target = sha
        else:
            raise ReleaseStop("provide --tag <name> or --sha <40-hex>")
        if _git(bare, "merge-base", "--is-ancestor", base_sha, target, check=False).returncode != 0:
            raise ReleaseStop(f"target {target} does not descend from base {base_sha}")
        diff_raw = _git(bare, "diff", "--no-renames", "-z", "--name-only", base_sha, target).stdout
        paths = [p.decode("utf-8", "surrogateescape") for p in diff_raw.split(b"\x00") if p]
        report = allowlist.classify_paths(paths)
        payload = {"base_sha": base_sha, "target_sha": target, **report.to_dict()}
        if as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"base_sha: {base_sha}")
            print(f"target_sha: {target}")
            for item in (*report.no_layer, *report.layer_replacement, *report.refused):
                print(f"{item.verdict}\t{item.path}\t{item.reason}")
            if not report.all_allowed:
                print(allowlist.REFUSAL_MESSAGE, file=sys.stderr)
            elif report.needs_layer_replacement:
                print(allowlist.LAYER_REPLACEMENT_REFUSAL, file=sys.stderr)
        return (0 if report.all_allowed and not report.needs_layer_replacement else 1), payload


def cmd_pack(
    *,
    tag: str,
    remote: str,
    base_bundle: Path,
    work_dir: Path,
    upstream_pub: Path,
    gap_pub: Path,
    repo_root: Path,
    session_dir: Path,
    signed_label: str = "true",
    require_gap_rsa3072: bool = False,
) -> dict[str, str]:
    """Pack an unsigned origin: gap bundle into work_dir/out/ (Increment 1, zero replaced layers)."""
    if not _TAG_NAME.fullmatch(tag):
        raise ReleaseStop("invalid --tag name")
    if signed_label not in ("true", "ephemeral"):
        raise ReleaseStop("--signed-label must be 'true' or 'ephemeral'")

    # 1. Session separation (D3: no cluster token, no registry auth, no signing key).
    check_pack_session(session_dir)

    # 2. Validate trust anchors (outside checkout, base bundle, and work_dir).
    anchors = validate_anchors(
        upstream_pub,
        gap_pub,
        forbidden_roots=(repo_root, base_bundle.parent if base_bundle.is_file() else base_bundle, work_dir),
        require_gap_rsa3072=require_gap_rsa3072,
    )

    # Scratch lives in the work directory, which is on /work: the base bundle
    # unpacks to several GB, more than a pod's ephemeral /tmp holds. Pack owns
    # it by name (like out.partial) and removes it on entry and exit.
    work_dir.mkdir(parents=True, exist_ok=True)
    scratch = work_dir / "scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir()
    try:
        return _pack_into(tag=tag, remote=remote, base_bundle=base_bundle, work_dir=work_dir,
                          anchors=anchors, scratch=scratch, signed_label=signed_label)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _pack_into(
    *,
    tag: str,
    remote: str,
    base_bundle: Path,
    work_dir: Path,
    anchors: AnchorSet,
    scratch: Path,
    signed_label: str,
) -> dict[str, str]:
    # 3. Verify base upstream bundle.
    base = load_verified_base_bundle(base_bundle, anchors, scratch)

    # 4. Fetch ONLY the tag and main into bare repo inside work_dir (no checkout, no hooks).
    bare = work_dir / "repo.git"
    _fetch_tag_and_main(remote, tag, bare)

    # 5. Tag binding, merge-commit check (D4), base ancestry, and tag-move refusal.
    sha, tree, gap_issue = _verify_tag_merge_commit_and_base(bare, tag, base.sha)
    req_file = work_dir / "request.txt"
    if req_file.is_file():
        prev = _read_kv_file(req_file)
        if prev.get("tag") and prev.get("tag") != tag:
            raise ReleaseStop(
                f"work directory belongs to tag {prev['tag']}, not {tag} — use one work directory per tag"
            )
        if prev.get("sha") and prev.get("sha") != sha:
            raise ReleaseStop(
                f"tag {tag} moved (request.txt recorded {prev['sha']}, now resolves to {sha}) — abandon the tag and increment"
            )

    # 6. Allow-list check (D2) on cumulative delta base.sha..sha.
    diff_raw = _git(bare, "diff", "--no-renames", "-z", "--name-only", base.sha, sha).stdout
    delta_paths = [p.decode("utf-8", "surrogateescape") for p in diff_raw.split(b"\x00") if p]
    report = allowlist.classify_paths(delta_paths)
    if not report.all_allowed:
        raise ReleaseStop(allowlist.REFUSAL_MESSAGE)
    if report.needs_layer_replacement:
        raise ReleaseStop(allowlist.LAYER_REPLACEMENT_REFUSAL)

    # 7. Ledger proof (upstream-owned paths differing from base.sha == gap/patches.md).
    changed_upstream = verify_ledger_proof(bare, base.sha, sha)

    # 8. OCI consistency check on both base app archives (Rev 2 §3a condition 3).
    base_agent = base.directory / f"app-agent-{base.sha}.tar"
    base_ingest = base.directory / f"app-ingest-{base.sha}.tar"
    agent_digest, agent_cfg_digest, _ = verify_oci_archive_consistency(base_agent)
    ingest_digest, ingest_cfg_digest, _ = verify_oci_archive_consistency(base_ingest)
    if agent_digest != base.manifest.get("agent_digest") or agent_cfg_digest != base.manifest.get("agent_config_digest"):
        raise ReleaseStop("base app-agent archive digest differs from base MANIFEST")
    if ingest_digest != base.manifest.get("ingest_digest") or ingest_cfg_digest != base.manifest.get("ingest_config_digest"):
        raise ReleaseStop("base app-ingest archive digest differs from base MANIFEST")

    # Verify task-pin.txt in tagged commit equals base bundle's task-pin.txt.
    tree_task_pin = _git(bare, "show", f"{sha}:scripts/tools/task-pin.txt").stdout
    if tree_task_pin != (base.directory / "task-pin.txt").read_bytes():
        raise ReleaseStop("tagged commit scripts/tools/task-pin.txt differs from base bundle task-pin.txt")

    # Clean any stale out.partial from a crashed earlier run, NEVER delete out/.
    partial_dir = work_dir / "out.partial"
    if partial_dir.exists():
        shutil.rmtree(partial_dir)
    out_dir = work_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 9. Write-once copied members from verified base bundle.
    copied_same_name = [
        "reference-docs.tar.gz",
        "task_linux_amd64.tar.gz",
        "task-pin.txt",
        "task-LICENSE",
        "qdrant-image.tar",
        "openobserve-image.tar",
        "otelcol-image.tar",
        "THIRD-PARTY-NOTICES.txt",
        *base.optional_images,
    ]
    for name in copied_same_name:
        _write_once_copy(out_dir, name, base.directory / name, base.sums[name])
    _write_once_copy(out_dir, f"app-ingest-{sha}.tar", base_ingest, base.sums[f"app-ingest-{base.sha}.tar"])
    _write_once_copy(out_dir, f"app-agent-{sha}.tar", base_agent, base.sums[f"app-agent-{base.sha}.tar"])

    # 10. Write-once derived members.
    _write_once_bytes(out_dir, "sneakernet-signing.pub", anchors.gap_path.read_bytes(), mode=0o644)
    bootstrap_bytes = _git(bare, "show", f"{sha}:scripts/airgap/bootstrap.sh").stdout
    _write_once_bytes(out_dir, "bootstrap.sh", bootstrap_bytes, mode=0o755)

    # repo.bundle: write-once with content identity check on existing file (D10).
    repo_bundle_target = out_dir / "repo.bundle"
    if repo_bundle_target.exists():
        if repo_bundle_target.is_symlink() or not repo_bundle_target.is_file():
            raise ReleaseStop("output repo.bundle is not a regular file")
        verify_repo_bundle_content(repo_bundle_target, sha, tree, scratch)
    else:
        partial_dir.mkdir(parents=True, exist_ok=True)
        tmp_bundle = partial_dir / f"repo.bundle.tmp.{os.getpid()}"
        create_clean_repo_bundle(bare, sha, tmp_bundle, scratch)
        verify_repo_bundle_content(tmp_bundle, sha, tree, scratch)
        tmp_bundle.replace(repo_bundle_target)

    commit_ts, commit_date = _commit_timestamp(bare, sha)
    manifest_text = render_gap_manifest(
        base.manifest_text,
        sha=sha,
        commit_date=commit_date,
        gap_base=base.sha,
        gap_issue=gap_issue,
        gap_tag=tag,
        gap_tree=tree,
        ingest_digest=ingest_digest,
        ingest_config_digest=ingest_cfg_digest,
        agent_digest=agent_digest,
        agent_config_digest=agent_cfg_digest,
        signed_label=signed_label,
    )
    _write_once_bytes(out_dir, "MANIFEST.txt", manifest_text.encode("utf-8"), mode=0o644)

    packing_record = render_gap_packing_record(
        sha=sha,
        commit_date=commit_date,
        gap_base=base.sha,
        gap_issue=gap_issue,
        gap_tag=tag,
        gap_tree=tree,
        gap_anchor_fp=anchors.gap_fp,
        upstream_anchor_fp=anchors.upstream_fp,
        base_manifest=base.manifest,
        optional_images=base.optional_images,
        replaced_layers=0,
    )
    _write_once_bytes(out_dir, "PACKING_RECORD.txt", packing_record.encode("utf-8"), mode=0o644)

    sbom_bytes = render_gap_sbom(
        (base.directory / "sbom.json").read_bytes(),
        sha=sha,
        commit_date=commit_date,
        gap_base=base.sha,
        gap_issue=gap_issue,
        gap_tag=tag,
        ingest_digest=ingest_digest,
        agent_digest=agent_digest,
    )
    _write_once_bytes(out_dir, "sbom.json", sbom_bytes, mode=0o644)

    members = _expected_members(sha, base.optional_images)
    sums_bytes = _format_sha256sums(out_dir, members)
    release_identity = _sha256_bytes(sums_bytes)

    # Write-once SHA256SUMS and signed.sha256 guard.
    _write_once_bytes(out_dir, "SHA256SUMS", sums_bytes, mode=0o644)
    signed_marker = work_dir / "signed.sha256"
    if signed_marker.is_file():
        recorded = signed_marker.read_text(encoding="utf-8").strip()
        if recorded != release_identity:
            raise ReleaseStop(
                f"tag {tag} was already signed with release identity {recorded}; refusing a different SHA256SUMS"
            )

    if partial_dir.exists():
        shutil.rmtree(partial_dir)

    request_lines = [
        f"tag={tag}",
        f"sha={sha}",
        f"tree={tree}",
        f"commit_timestamp={commit_ts}",
        f"commit_date={commit_date}",
        f"gap_base={base.sha}",
        f"gap_issue={gap_issue}",
        f"release_identity={release_identity}",
        f"upstream_anchor_sha256={anchors.upstream_fp}",
        f"gap_anchor_sha256={anchors.gap_fp}",
        f"changed_upstream_count={len(changed_upstream)}",
    ]
    _atomic_write_bytes(req_file, ("\n".join(request_lines) + "\n").encode("utf-8"), mode=0o644)

    print(f"upstream_anchor_sha256: {anchors.upstream_fp}")
    print(f"gap_anchor_sha256: {anchors.gap_fp}")
    print(f"release_identity: {release_identity}")
    print(f"PACKED {sha} {release_identity}")
    return {
        "tag": tag,
        "sha": sha,
        "tree": tree,
        "gap_base": base.sha,
        "gap_issue": gap_issue,
        "release_identity": release_identity,
        "upstream_anchor_sha256": anchors.upstream_fp,
        "gap_anchor_sha256": anchors.gap_fp,
    }


def cmd_assemble(
    *,
    work_dir: Path,
    signature_path: Path,
    upstream_pub: Path,
    gap_pub: Path,
    repo_root: Path,
    out_tarball: Path | None = None,
) -> dict[str, str]:
    """Attach an externally-produced SHA256SUMS.sig, verify it against the gap anchor,
    enforce one release identity per tag (signed.sha256), and write the deterministic tarball."""
    anchors = validate_anchors(
        upstream_pub,
        gap_pub,
        forbidden_roots=(repo_root, work_dir),
    )
    out_dir = work_dir / "out"
    req_file = work_dir / "request.txt"
    if not out_dir.is_dir() or not req_file.is_file():
        raise ReleaseStop("work directory is missing out/ or request.txt — run pack first")
    req = _read_kv_file(req_file)
    sha = req.get("sha", "")
    if not _HEX40.fullmatch(sha) or not req.get("commit_timestamp", "").isdigit():
        raise ReleaseStop("request.txt is malformed — rerun pack")
    commit_ts = int(req["commit_timestamp"])

    sums_path = out_dir / "SHA256SUMS"
    if not sums_path.is_file():
        raise ReleaseStop("out/SHA256SUMS is missing — run pack first")
    release_identity = _sha256_file(sums_path)
    if req.get("release_identity") != release_identity:
        raise ReleaseStop("out/SHA256SUMS differs from request.txt release_identity")

    if signature_path.is_symlink() or not signature_path.is_file():
        raise ReleaseStop("signature file is not a readable plain file")
    sig_bytes = signature_path.read_bytes()

    signed_marker = work_dir / "signed.sha256"
    if signed_marker.is_file():
        prev_id = signed_marker.read_text(encoding="utf-8").strip()
        if prev_id != release_identity:
            raise ReleaseStop(
                f"a different bundle ({prev_id}) was already signed for this tag — one release identity per tag"
            )

    # Verify signature BEFORE installing it into out/SHA256SUMS.sig.
    try:
        sign_vault.verify_signature_openssl(anchors.gap_path, sums_path, signature_path)
    except sign_vault.SignError as exc:
        raise ReleaseStop(str(exc)) from None

    _write_once_bytes(out_dir, "SHA256SUMS.sig", sig_bytes, mode=0o644)
    _atomic_write_bytes(signed_marker, (release_identity + "\n").encode("utf-8"), mode=0o644)

    sums = parse_sha256sums(sums_path.read_text(encoding="utf-8"))
    optional_present = tuple(opt for opt in OPTIONAL_IMAGE_MEMBERS if opt in sums)
    tar_members = [*_expected_members(sha, optional_present), "SHA256SUMS", "SHA256SUMS.sig"]

    tarball = out_tarball or (work_dir / f"qdrant-pdf-rag-{sha}.tar")
    tmp_tar = tarball.with_name(f".{tarball.name}.partial.{os.getpid()}")
    try:
        with tarfile.open(tmp_tar, "w:", format=tarfile.USTAR_FORMAT) as tf:
            for name in tar_members:
                src = out_dir / name
                info = tarfile.TarInfo(name=name)
                info.size = src.stat().st_size
                info.mtime = commit_ts
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = 0o755 if name == "bootstrap.sh" else 0o644
                with src.open("rb") as fh:
                    tf.addfile(info, fh)
        tmp_tar.replace(tarball)
    finally:
        if tmp_tar.exists():
            tmp_tar.unlink(missing_ok=True)

    tar_sha256 = _sha256_file(tarball)
    _atomic_write_bytes(
        Path(str(tarball) + ".sha256"),
        f"{tar_sha256}  {tarball.name}\n".encode(),
        mode=0o644,
    )
    print(f"upstream_anchor_sha256: {anchors.upstream_fp}")
    print(f"gap_anchor_sha256: {anchors.gap_fp}")
    print(f"ASSEMBLED {sha} {release_identity} {tar_sha256}")
    return {
        "sha": sha,
        "release_identity": release_identity,
        "tarball": str(tarball),
        "tarball_sha256": tar_sha256,
    }


def cmd_verify(
    *,
    bundle: Path,
    base_bundle: Path,
    upstream_pub: Path,
    gap_pub: Path,
    repo_root: Path,
    remote: str | None = None,
    scratch_dir: Path | None = None,
) -> dict[str, object]:
    """Trusted pre-promotion verification in Rev 2 §3e 6-step order.

    The candidate and the base tarball are unpacked into scratch_dir (default:
    TMPDIR); on the bastion point it at /work, not the pod's ephemeral /tmp."""
    # Step 1 (release guard) is enforced by scripts/gap/release.sh before invoking python3 -I.
    anchors = validate_anchors(
        upstream_pub,
        gap_pub,
        forbidden_roots=(
            repo_root,
            bundle.parent if bundle.is_file() else bundle,
            base_bundle.parent if base_bundle.is_file() else base_bundle,
        ),
    )

    with tempfile.TemporaryDirectory(prefix="mfr-gap-verify-", dir=scratch_dir) as td:
        scratch = Path(td)
        # Step 2: Member inventory (regular files only, plain names, no links/duplicates).
        if bundle.is_file():
            bundle_dir = scratch / "candidate"
            inventory = inspect_and_unpack_tarball(bundle, bundle_dir)
        else:
            bundle_dir = bundle
            inventory = check_directory_inventory(bundle_dir)

        # Step 3: Gap anchor signature, exact SHA256SUMS inventory & hashes, origin: gap + gap_* fields.
        sha, manifest_text, manifest, sums, optional_present = _verify_bundle_dir_common(
            bundle_dir,
            inventory,
            expected_origin="gap",
            anchor_path=anchors.gap_path,
        )
        release_identity = _sha256_file(bundle_dir / "SHA256SUMS")
        gap_base = manifest["gap_base"]
        gap_issue = manifest["gap_issue"]
        gap_tag = manifest["gap_tag"]
        gap_tree = manifest["gap_tree"]
        signed_label = manifest.get("signed", "true")

        # Step 4: repo.bundle content identity (single HEAD ref, no unreachable objects, exact gap_tree).
        bare = verify_repo_bundle_content(bundle_dir / "repo.bundle", sha, gap_tree, scratch)

        # Optional live tag resolution check when GitLab remote is supplied.
        if remote:
            ls_out = _git(bare, "ls-remote", "--tags", remote, f"refs/tags/{gap_tag}^{{}}", f"refs/tags/{gap_tag}").stdout.decode()
            resolved = None
            for line in ls_out.splitlines():
                oid, ref = line.split("\t", 1)
                if ref == f"refs/tags/{gap_tag}^{{}}":
                    resolved = oid
                    break
                if ref == f"refs/tags/{gap_tag}" and resolved is None:
                    resolved = oid
            if resolved != sha:
                raise ReleaseStop(
                    f"remote tag {gap_tag} resolves to {resolved or 'missing'}, not MANIFEST sha {sha}"
                )

        # Step 5: Verify base upstream bundle, ancestry, D2 allow-list, and ledger proof.
        base = load_verified_base_bundle(base_bundle, anchors, scratch)
        if base.sha != gap_base:
            raise ReleaseStop(f"base bundle sha ({base.sha}) does not match MANIFEST gap_base ({gap_base})")
        if base.optional_images != optional_present:
            raise ReleaseStop("candidate optional image set differs from base bundle")
        if _git(bare, "merge-base", "--is-ancestor", gap_base, sha, check=False).returncode != 0:
            raise ReleaseStop(f"repo.bundle commit {sha} does not descend from gap_base {gap_base}")

        diff_raw = _git(bare, "diff", "--no-renames", "-z", "--name-only", gap_base, sha).stdout
        delta_paths = [p.decode("utf-8", "surrogateescape") for p in diff_raw.split(b"\x00") if p]
        report = allowlist.classify_paths(delta_paths)
        if not report.all_allowed:
            raise ReleaseStop(allowlist.REFUSAL_MESSAGE)
        if report.needs_layer_replacement:
            raise ReleaseStop(allowlist.LAYER_REPLACEMENT_REFUSAL)

        changed_upstream = verify_ledger_proof(bare, gap_base, sha)

        # Step 6: Verify every other member by its §3e rule.
        copied_same_name = [
            "reference-docs.tar.gz",
            "task_linux_amd64.tar.gz",
            "task-pin.txt",
            "task-LICENSE",
            "qdrant-image.tar",
            "openobserve-image.tar",
            "otelcol-image.tar",
            "THIRD-PARTY-NOTICES.txt",
            *optional_present,
        ]
        for name in copied_same_name:
            if sums[name] != base.sums[name]:
                raise ReleaseStop(
                    f"copied member {name} differs from verified base upstream bundle"
                )
        if _git(bare, "show", f"{sha}:scripts/tools/task-pin.txt").stdout != (bundle_dir / "task-pin.txt").read_bytes():
            raise ReleaseStop("task-pin.txt does not match tagged commit scripts/tools/task-pin.txt")

        if (bundle_dir / "bootstrap.sh").read_bytes() != _git(bare, "show", f"{sha}:scripts/airgap/bootstrap.sh").stdout:
            raise ReleaseStop("bootstrap.sh does not match tagged commit scripts/airgap/bootstrap.sh")

        # App archives: OCI consistency + byte equality to base in Increment 1.
        cand_agent = bundle_dir / f"app-agent-{sha}.tar"
        cand_ingest = bundle_dir / f"app-ingest-{sha}.tar"
        agent_digest, agent_cfg_digest, _ = verify_oci_archive_consistency(cand_agent)
        ingest_digest, ingest_cfg_digest, _ = verify_oci_archive_consistency(cand_ingest)
        if sums[f"app-agent-{sha}.tar"] != base.sums[f"app-agent-{base.sha}.tar"]:
            raise ReleaseStop("app-agent archive differs from base bundle (layer replacement is Increment 2)")
        if sums[f"app-ingest-{sha}.tar"] != base.sums[f"app-ingest-{base.sha}.tar"]:
            raise ReleaseStop("app-ingest archive differs from base bundle (layer replacement is Increment 2)")

        # Regenerate MANIFEST.txt, PACKING_RECORD.txt, sbom.json and compare byte-for-byte.
        _, commit_date = _commit_timestamp(bare, sha)
        want_manifest = render_gap_manifest(
            base.manifest_text,
            sha=sha,
            commit_date=commit_date,
            gap_base=gap_base,
            gap_issue=gap_issue,
            gap_tag=gap_tag,
            gap_tree=gap_tree,
            ingest_digest=ingest_digest,
            ingest_config_digest=ingest_cfg_digest,
            agent_digest=agent_digest,
            agent_config_digest=agent_cfg_digest,
            signed_label=signed_label,
        )
        if manifest_text != want_manifest:
            raise ReleaseStop("MANIFEST.txt does not match the deterministic regeneration from trusted inputs")

        want_record = render_gap_packing_record(
            sha=sha,
            commit_date=commit_date,
            gap_base=gap_base,
            gap_issue=gap_issue,
            gap_tag=gap_tag,
            gap_tree=gap_tree,
            gap_anchor_fp=anchors.gap_fp,
            upstream_anchor_fp=anchors.upstream_fp,
            base_manifest=base.manifest,
            optional_images=optional_present,
            replaced_layers=0,
        )
        if (bundle_dir / "PACKING_RECORD.txt").read_text(encoding="utf-8") != want_record:
            raise ReleaseStop("PACKING_RECORD.txt does not match the deterministic regeneration from trusted inputs")

        want_sbom = render_gap_sbom(
            (base.directory / "sbom.json").read_bytes(),
            sha=sha,
            commit_date=commit_date,
            gap_base=gap_base,
            gap_issue=gap_issue,
            gap_tag=gap_tag,
            ingest_digest=ingest_digest,
            agent_digest=agent_digest,
        )
        if (bundle_dir / "sbom.json").read_bytes() != want_sbom:
            raise ReleaseStop("sbom.json does not match the deterministic regeneration from trusted inputs")

        print(f"upstream_anchor_sha256: {anchors.upstream_fp}")
        print(f"gap_anchor_sha256: {anchors.gap_fp}")
        print(f"changed_upstream_paths: {len(changed_upstream)}")
        for path in changed_upstream:
            print(f"  - {path}")
        print(f"VERIFIED {sha} {release_identity}")
        return {
            "sha": sha,
            "release_identity": release_identity,
            "gap_base": gap_base,
            "gap_issue": gap_issue,
            "gap_tag": gap_tag,
            "gap_tree": gap_tree,
            "upstream_anchor_sha256": anchors.upstream_fp,
            "gap_anchor_sha256": anchors.gap_fp,
            "changed_upstream_paths": changed_upstream,
        }


def cmd_rehearse_check(
    *,
    rehearsal_namespace: str,
    prod_namespace: str,
    rehearsal_registry: str,
    prod_registry: str,
    candidate_manifest: Path,
    installed_manifest: Path,
    namespace_objects_json: Path | None = None,
    kubectl_cmd: str | None = None,
) -> dict[str, object]:
    """Enforce Rev 2 §3h / D6 rehearsal-namespace preflight refusals:
    1. Rehearsal namespace must differ from the installed/production namespace.
    2. If any fixed-tag third-party image digest differs from the installed release,
       the rehearsal must use a separate registry path.
    3. Target rehearsal namespace must be empty of existing workloads/PVCs/Helm releases.
    """
    reh_ns = rehearsal_namespace.strip()
    prod_ns = prod_namespace.strip()
    if not reh_ns or not prod_ns:
        raise ReleaseStop("rehearsal and production namespaces must both be non-empty")
    if reh_ns == prod_ns:
        raise ReleaseStop(
            "rehearsal refused: target rehearsal namespace equals the installed release namespace — "
            "deploy rehearsal to a dedicated rehearsal namespace (Rev 2 D6)"
        )

    cand_map, _ = parse_manifest(candidate_manifest.read_text(encoding="utf-8"))
    inst_map, _ = parse_manifest(installed_manifest.read_text(encoding="utf-8"))
    changed_fixed_tags = [
        key for key in FIXED_TAG_IMAGE_KEYS
        if cand_map.get(key) and cand_map.get(key) != inst_map.get(key)
    ]
    if changed_fixed_tags and rehearsal_registry.strip().rstrip("/") == prod_registry.strip().rstrip("/"):
        raise ReleaseStop(
            "rehearsal refused: candidate changes a fixed-tag third-party image while sharing "
            "INTERNAL_REGISTRY with the installed release — use a separate rehearsal registry path"
        )

    # Check target rehearsal namespace is empty.
    existing: list[str] = []
    if namespace_objects_json is not None:
        data = json.loads(namespace_objects_json.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ReleaseStop("namespace objects JSON is malformed")
        for item in items:
            if isinstance(item, dict):
                kind = str(item.get("kind", "Object"))
                name = str((item.get("metadata") or {}).get("name", ""))
                existing.append(f"{kind}/{name}")
    elif kubectl_cmd is not None:
        for kind in ("deployments.apps", "statefulsets.apps", "jobs.batch", "pods", "pvc", "secrets"):
            args = [kubectl_cmd, "-n", reh_ns, "get", kind, "-o", "json"]
            if kind == "secrets":
                args.extend(["-l", "owner=helm"])
            proc = subprocess.run(args, capture_output=True, text=True, check=False, timeout=30)
            if proc.returncode != 0:
                raise ReleaseStop(f"rehearsal refused: cannot inspect {kind} in namespace {reh_ns}")
            parsed = json.loads(proc.stdout or "{}")
            for item in parsed.get("items") or []:
                name = str((item.get("metadata") or {}).get("name", ""))
                existing.append(f"{kind}/{name}")
    else:
        raise ReleaseStop("rehearse-check requires --namespace-objects-json or --kubectl")

    if existing:
        raise ReleaseStop(
            f"rehearsal refused: target namespace {reh_ns} is not empty ({len(existing)} existing object(s)) — "
            "run the rehearsal teardown procedure first"
        )

    print(f"REHEARSE READY namespace={reh_ns} registry={rehearsal_registry}")
    return {
        "rehearsal_namespace": reh_ns,
        "prod_namespace": prod_ns,
        "changed_fixed_tags": changed_fixed_tags,
    }


def _env_path(name: str) -> Path | None:
    val = os.environ.get(name, "").strip()
    return Path(val) if val else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gap release pipeline Increment 1: classify, pack, assemble, verify, rehearse-check."
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    sub = parser.add_subparsers(dest="command", required=True)

    p_cls = sub.add_parser("classify", help="classify cumulative delta against the D2 allow-list")
    p_cls.add_argument("--repo", required=True, help="git remote URL or local repo path")
    p_cls.add_argument("--tag", default=None)
    p_cls.add_argument("--sha", default=None)
    p_cls.add_argument("--base-sha", required=True)
    p_cls.add_argument("--json", action="store_true", dest="as_json")

    p_pack = sub.add_parser("pack", help="pack an unsigned origin: gap bundle in the bastion")
    p_pack.add_argument("--tag", required=True)
    p_pack.add_argument("--remote", required=True, help="read-only GitLab URL or local repo path")
    p_pack.add_argument("--base-bundle", type=Path, required=True)
    p_pack.add_argument("--work-dir", type=Path, required=True)
    p_pack.add_argument("--upstream-pub", type=Path, default=_env_path("SNEAKERNET_TRUSTED_PUB"))
    p_pack.add_argument("--gap-pub", type=Path, default=_env_path("SNEAKERNET_GAP_TRUSTED_PUB"))
    p_pack.add_argument("--session-dir", type=Path, default=Path(os.environ.get("MFR_SESSION_DIR", "/run/mfr-session")))
    p_pack.add_argument("--signed-label", default="true", choices=("true", "ephemeral"))
    p_pack.add_argument("--require-rsa-3072", action="store_true")

    p_asm = sub.add_parser("assemble", help="attach externally-produced signature and write deterministic tarball")
    p_asm.add_argument("--work-dir", type=Path, required=True)
    p_asm.add_argument("--signature", type=Path, required=True)
    p_asm.add_argument("--out-tarball", type=Path, default=None)
    p_asm.add_argument("--upstream-pub", type=Path, default=_env_path("SNEAKERNET_TRUSTED_PUB"))
    p_asm.add_argument("--gap-pub", type=Path, default=_env_path("SNEAKERNET_GAP_TRUSTED_PUB"))

    p_ver = sub.add_parser("verify", help="trusted pre-promotion verification of a signed gap bundle")
    p_ver.add_argument("--bundle", type=Path, required=True)
    p_ver.add_argument("--base-bundle", type=Path, required=True)
    p_ver.add_argument("--upstream-pub", type=Path, default=_env_path("SNEAKERNET_TRUSTED_PUB"))
    p_ver.add_argument("--gap-pub", type=Path, default=_env_path("SNEAKERNET_GAP_TRUSTED_PUB"))
    p_ver.add_argument("--remote", default=None, help="optional GitLab remote to check gap_tag still resolves to sha")
    p_ver.add_argument("--scratch-dir", type=Path, default=None,
                       help="directory to unpack the candidate and base into (default TMPDIR; use /work on the bastion)")

    p_reh = sub.add_parser("rehearse-check", help="enforce D6 rehearsal-namespace preflight refusals")
    p_reh.add_argument("--rehearsal-namespace", required=True)
    p_reh.add_argument("--prod-namespace", required=True)
    p_reh.add_argument("--rehearsal-registry", required=True)
    p_reh.add_argument("--prod-registry", required=True)
    p_reh.add_argument("--candidate-manifest", type=Path, required=True)
    p_reh.add_argument("--installed-manifest", type=Path, required=True)
    p_reh.add_argument("--namespace-objects-json", type=Path, default=None)
    p_reh.add_argument("--kubectl", default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "classify":
            rc, _ = cmd_classify(
                repo=args.repo,
                tag=args.tag,
                sha=args.sha,
                base_sha=args.base_sha,
                as_json=args.as_json,
            )
            return rc
        if args.command == "pack":
            if args.upstream_pub is None or args.gap_pub is None:
                raise ReleaseStop("both SNEAKERNET_TRUSTED_PUB and SNEAKERNET_GAP_TRUSTED_PUB are required")
            cmd_pack(
                tag=args.tag,
                remote=args.remote,
                base_bundle=args.base_bundle,
                work_dir=args.work_dir,
                upstream_pub=args.upstream_pub,
                gap_pub=args.gap_pub,
                repo_root=args.repo_root,
                session_dir=args.session_dir,
                signed_label=args.signed_label,
                require_gap_rsa3072=args.require_rsa_3072,
            )
            return 0
        if args.command == "assemble":
            if args.upstream_pub is None or args.gap_pub is None:
                raise ReleaseStop("both SNEAKERNET_TRUSTED_PUB and SNEAKERNET_GAP_TRUSTED_PUB are required")
            cmd_assemble(
                work_dir=args.work_dir,
                signature_path=args.signature,
                upstream_pub=args.upstream_pub,
                gap_pub=args.gap_pub,
                repo_root=args.repo_root,
                out_tarball=args.out_tarball,
            )
            return 0
        if args.command == "verify":
            if args.upstream_pub is None or args.gap_pub is None:
                raise ReleaseStop("both SNEAKERNET_TRUSTED_PUB and SNEAKERNET_GAP_TRUSTED_PUB are required")
            cmd_verify(
                bundle=args.bundle,
                base_bundle=args.base_bundle,
                upstream_pub=args.upstream_pub,
                gap_pub=args.gap_pub,
                repo_root=args.repo_root,
                remote=args.remote,
                scratch_dir=args.scratch_dir,
            )
            return 0
        if args.command == "rehearse-check":
            cmd_rehearse_check(
                rehearsal_namespace=args.rehearsal_namespace,
                prod_namespace=args.prod_namespace,
                rehearsal_registry=args.rehearsal_registry,
                prod_registry=args.prod_registry,
                candidate_manifest=args.candidate_manifest,
                installed_manifest=args.installed_manifest,
                namespace_objects_json=args.namespace_objects_json,
                kubectl_cmd=args.kubectl,
            )
            return 0
        raise ReleaseStop("unknown subcommand")
    except ReleaseStop as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("STOP: gap release operation encountered an unreadable or unwritable path", file=sys.stderr)
        return 1
    except (ValueError, KeyError, subprocess.SubprocessError):
        # Malformed input (bad UTF-8 or JSON, a missing field): fixed text, no traceback.
        print("STOP: gap release input is malformed; nothing was trusted (see the step above)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
