#!/usr/bin/env python3
"""Fetch FastEmbed BM25 sparse weights into a directory for image baking.

Run on the CONNECTED host only (make bm25-weights). The output directory is
copied into the ingest and agent images so the air-gap never downloads.

Runtime-fetched artifacts are pinned by content (AGENTS.md section 6):
`--verify-manifest bm25-weights.sha256` fails closed when any downloaded
file's sha256 differs from the in-repo manifest, or when upstream adds or
removes files. A manifest update is a dedicated PR.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def verify_manifest(cache_dir: Path, manifest: Path) -> None:
    """sha256 every file under the model snapshot (symlinks resolved) and
    compare both ways with the manifest — mismatch, missing, or extra file
    all fail closed."""
    model_dir = next(
        (p for p in cache_dir.glob("models--*") if p.is_dir()), None
    )
    snapshots = model_dir / "snapshots" if model_dir else None
    snapshot = next((p for p in snapshots.glob("*") if p.is_dir()), None) if snapshots else None
    if snapshot is None:
        raise SystemExit(f"verification failed: no model snapshot under {cache_dir}")

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qdrant/bm25")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--verify-manifest", type=Path, default=None,
        help="sha256 manifest (bm25-weights.sha256) to verify the download against",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    # Instantiating the model materializes its files under the cache dir.
    from fastembed import SparseTextEmbedding

    SparseTextEmbedding(model_name=args.model, cache_dir=str(args.out))
    print(f"BM25 weights for {args.model} cached under {args.out}")
    if args.verify_manifest:
        verify_manifest(args.out, args.verify_manifest)


if __name__ == "__main__":
    main()
