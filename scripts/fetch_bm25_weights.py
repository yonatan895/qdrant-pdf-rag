#!/usr/bin/env python3
"""Fetch FastEmbed BM25 sparse weights into a directory for image baking.

Run on the CONNECTED host only (sh scripts/tools/run-task.sh artifacts:bm25). The output directory is
copied into the ingest and agent images so the air-gap never downloads.

Runtime-fetched artifacts are pinned by content (AGENTS.md section 6):
`--verify-manifest bm25-weights.sha256` fails closed when any downloaded
file's sha256 differs from the in-repo manifest, or when upstream adds or
removes files. A manifest update is a dedicated PR.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

MODEL = "Qdrant/bm25"
MODEL_DIR = "models--Qdrant--bm25"
_BLOB_NAME = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"


def _manifest_entries(manifest: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        digest, name = line.split(maxsplit=1)
        name = name.strip()
        path = Path(name)
        if (path.is_absolute() or ".." in path.parts or not path.parts
                or name in expected or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("invalid BM25 manifest member")
        expected[name] = digest
    if not expected:
        raise ValueError("empty BM25 manifest")
    return expected


def verify_manifest(cache_dir: Path, manifest: Path) -> None:
    """sha256 every file under the model snapshot (symlinks resolved) and
    compare both ways with the manifest — mismatch, missing, or extra file
    all fail closed."""
    models = [p for p in cache_dir.glob("models--*") if p.is_dir()]
    if len(models) != 1 or models[0].name != "models--Qdrant--bm25":
        raise SystemExit("verification failed: expected one Qdrant/bm25 model cache")
    model_dir = models[0]
    snapshots = [p for p in (model_dir / "snapshots").glob("*") if p.is_dir()]
    if len(snapshots) != 1:
        raise SystemExit("verification failed: expected one unambiguous BM25 snapshot")
    snapshot = snapshots[0]
    reference = model_dir / "refs/main"
    try:
        selected = reference.read_text()
    except (OSError, UnicodeError):
        selected = None
    if selected != snapshot.name:
        raise SystemExit("verification failed: BM25 selected revision does not match the verified snapshot")
    if not snapshot.resolve().is_relative_to(model_dir.resolve()):
        raise SystemExit("verification failed: snapshot leaves model cache")
    for path in snapshot.rglob("*"):
        if path.is_symlink() and (not path.is_file() or not path.resolve().is_relative_to(model_dir.resolve())):
            raise SystemExit("verification failed: unresolved or external model file")

    actual = {
        str(p.relative_to(snapshot)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(snapshot.rglob("*"))
        if p.is_file() or (p.is_symlink() and p.resolve().is_file())
    }
    expected = _manifest_entries(manifest)

    problems = []
    for name, digest in sorted(expected.items()):
        if name not in actual:
            problems.append(f"missing from download: {name}")
        elif actual[name] != digest:
            problems.append(f"digest mismatch: {name} ({actual[name]} != {digest})")
    for name in sorted(set(actual) - set(expected)):
        problems.append(f"not in manifest (upstream added?): {name}")
    if problems:
        print("BM25 weights verification FAILED (re-record via a dedicated PR):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        raise SystemExit(1)
    # FastEmbed reads this optional sidecar before selecting the local snapshot.
    # Valid snapshot hashes alone cannot certify a cache it cannot load offline.
    metadata_path = model_dir / "files_metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text())
            if not isinstance(metadata, dict):
                raise TypeError
            for name, entry in metadata.items():
                member = model_dir / name
                if (not member.resolve().is_relative_to(model_dir.resolve())
                        or not isinstance(entry, dict)
                        or type(entry.get("size")) is not int
                        or not isinstance(entry.get("blob_id"), str)
                        or not member.is_file() or member.stat().st_size != entry["size"]):
                    raise ValueError
        except (OSError, ValueError, TypeError):
            raise SystemExit("verification failed: invalid BM25 cache metadata") from None
    print(f"BM25 weights verified against {manifest} ({len(expected)} files)")


def _safe_destination(cache: Path) -> None:
    if cache == Path(cache.anchor) or any(p.is_symlink() for p in (cache, *cache.parents)):
        raise ValueError("refusing root or symlink BM25 destination")
    if cache.exists() and not cache.is_dir():
        raise ValueError("BM25 destination must be a directory")


def _owned_cache(cache: Path, members: set[str]) -> None:
    """Only the selected HF cache layout is replaceable; never follow directories."""
    _safe_destination(cache)
    if not cache.exists():
        return
    model = cache / MODEL_DIR
    for path in cache.rglob("*"):
        parts = path.relative_to(cache).parts
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            if (len(parts) < 4 or parts[:2] != (MODEL_DIR, "snapshots")
                    or not path.resolve().is_relative_to(model / "blobs")):
                raise ValueError("refusing external or directory BM25 symlink")
            if path.exists() and not path.is_file():
                raise ValueError("refusing directory BM25 symlink")
        elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError("refusing special BM25 cache member")
        if parts[0] == ".task-complete":
            allowed = len(parts) == 1 and stat.S_ISREG(mode)
        elif parts[0] == ".locks":
            allowed = (
                len(parts) == 1 and stat.S_ISDIR(mode)
                or len(parts) == 2 and parts[1] == MODEL_DIR and stat.S_ISDIR(mode)
                or len(parts) == 3 and parts[1] == MODEL_DIR and stat.S_ISREG(mode)
                and re.fullmatch(_BLOB_NAME + r"\.lock", parts[2]) is not None
            )
        elif parts[0] == MODEL_DIR:
            allowed = stat.S_ISDIR(mode) if len(parts) == 1 else parts[1] in {
                "blobs", "snapshots", "refs", "files_metadata.json", ".no_exist",
            }
            if len(parts) == 2:
                allowed = allowed and (stat.S_ISREG(mode) if parts[1] == "files_metadata.json"
                                       else stat.S_ISDIR(mode))
            if len(parts) > 2 and parts[1] == "refs":
                allowed = parts[2:] == ("main",) and stat.S_ISREG(mode)
            if len(parts) > 2 and parts[1] == "blobs":
                allowed = (len(parts) == 3 and stat.S_ISREG(mode)
                           and re.fullmatch(_BLOB_NAME + r"(?:\.incomplete)?", parts[2]) is not None)
            if len(parts) == 3 and parts[1] in {"snapshots", ".no_exist"}:
                allowed = stat.S_ISDIR(mode)
            if len(parts) > 3 and parts[1] == ".no_exist":
                allowed = len(parts) == 4 and stat.S_ISREG(mode) and parts[3] in members | {
                    "mock.file", "config.json", "tokenizer.json", "tokenizer_config.json",
                    "special_tokens_map.json", "preprocessor_config.json",
                }
            if len(parts) > 3 and parts[1] == "snapshots":
                name = "/".join(parts[3:])
                allowed = name in members or (
                    stat.S_ISDIR(mode) and any(m.startswith(name + "/") for m in members)
                )
        else:
            allowed = False
        if not allowed:
            raise ValueError("refusing unowned BM25 cache member")


def _acquire(model: str, cache: Path) -> None:
    from fastembed import SparseTextEmbedding

    SparseTextEmbedding(model_name=model, cache_dir=str(cache))


def prepare(cache: Path, manifest: Path, model: str = MODEL) -> None:
    """Stage verified acquisition before replacing cache members; receipt last.

    Explicit preparation excludes active readers. Publication is recoverable,
    not an atomic directory swap: interruption may leave missing members, which
    the next preparation verifies and repairs. Downloads never change old bytes.
    """
    if model != MODEL:
        raise ValueError("the pinned manifest supports only Qdrant/bm25")
    members = set(_manifest_entries(manifest))
    # Check the literal path before normalizing away a symlink/.. traversal.
    _safe_destination(cache.absolute())
    cache = Path(os.path.abspath(cache))
    _safe_destination(cache)
    identity = {
        "schema_version": 1, "model": model,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "recipe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(cache.with_name(f".{cache.name}.prepare.lock"),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _owned_cache(cache, members)
        try:
            verify_manifest(cache, manifest)
            valid = True
        except (SystemExit, OSError):
            valid = False
        marker = cache / ".task-complete"
        receipt = json.dumps(identity, sort_keys=True) + "\n"
        try:
            recorded = marker.read_text()
        except (OSError, UnicodeError):
            recorded = None
        if valid and recorded == receipt:
            return
        with tempfile.TemporaryDirectory(prefix=f".{cache.name}.prepare-", dir=cache.parent) as temp:
            stage = Path(temp)
            if not valid:
                _acquire(model, stage)
                verify_manifest(stage, manifest)
                _owned_cache(stage, members)
                _owned_cache(cache, members)
                cache.mkdir(exist_ok=True)
                marker.unlink(missing_ok=True)
                # Only known cache-owned trees; no recursive deletion of the
                # caller's destination. Missing members remain visibly invalid.
                for name in (MODEL_DIR, ".locks"):
                    old = cache / name
                    if old.exists():
                        shutil.rmtree(old)
                    incoming = stage / name
                    if incoming.exists():
                        incoming.replace(old)
            verify_manifest(cache, manifest)
            staged_marker = stage / ".task-complete"
            staged_marker.write_text(receipt)
            staged_marker.replace(marker)


def prepared_bm25_cache(root: Path) -> Path:
    """An explicit cache selection never silently falls back to another tree."""
    selected = os.environ.get('SIM_BM25_CACHE_DIR')
    if selected is not None and not selected:
        raise ValueError('SIM_BM25_CACHE_DIR must not be empty')
    cache = Path(selected) if selected is not None else root / 'bundles/bm25-weights'
    verify_manifest(cache, root / 'bm25-weights.sha256')
    return cache.absolute()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qdrant/bm25")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--verify-manifest", type=Path, default=None,
        help="sha256 manifest (bm25-weights.sha256) to verify the download against",
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="verify existing downloaded files against manifest without importing or downloading",
    )
    args = parser.parse_args()

    if args.verify_only:
        if not args.verify_manifest:
            raise SystemExit("error: --verify-only requires --verify-manifest")
        verify_manifest(args.out, args.verify_manifest)
        return

    prepare(args.out, args.verify_manifest or Path(__file__).resolve().parents[1] / "bm25-weights.sha256",
            args.model)


if __name__ == "__main__":
    main()
