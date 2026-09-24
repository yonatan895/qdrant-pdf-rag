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
import hashlib
import os
import sys
from pathlib import Path


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
    if not reference.is_file() or reference.read_text().strip() != snapshot.name:
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
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, name = line.split(maxsplit=1)
        expected[name.strip()] = digest

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
    print(f"BM25 weights verified against {manifest} ({len(expected)} files)")


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

    args.out.mkdir(parents=True, exist_ok=True)
    # Instantiating the model materializes its files under the cache dir.
    from fastembed import SparseTextEmbedding

    SparseTextEmbedding(model_name=args.model, cache_dir=str(args.out))
    print(f"BM25 weights for {args.model} cached under {args.out}")
    if args.verify_manifest:
        verify_manifest(args.out, args.verify_manifest)


if __name__ == "__main__":
    main()
