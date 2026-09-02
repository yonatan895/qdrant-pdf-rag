#!/usr/bin/env python3
"""Fetch BGE Reranker v2 M3 weights into a directory for offline/air-gap baking.

Run on the CONNECTED host only (make reranker-weights). The output directory is
copied into images or mounted on the cluster so the air-gap never downloads.

Runtime-fetched artifacts are pinned by content (AGENTS.md):
`--verify-manifest reranker-weights.sha256` fails closed when any downloaded
file's sha256 differs from the in-repo manifest, or when upstream adds or
removes files. A manifest update is a dedicated PR.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

MODEL_FILES = (
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


def verify_manifest(target_dir: Path, manifest: Path) -> None:
    """sha256 every file in the target directory and compare with the manifest."""
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            expected[parts[1].strip()] = parts[0].strip()

    actual = {
        str(p.relative_to(target_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(target_dir.rglob("*"))
        if p.is_file() and not p.name.startswith(".")
    }

    problems: list[str] = []
    for name, digest in sorted(expected.items()):
        if name not in actual:
            problems.append(f"missing from download: {name}")
        elif actual[name] != digest:
            problems.append(f"digest mismatch: {name} ({actual[name]} != {digest})")
    for name in sorted(set(actual) - set(expected)):
        problems.append(f"not in manifest (unexpected file): {name}")

    if problems:
        print("Reranker weights verification FAILED (re-record via a dedicated PR):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Reranker weights verified against {manifest} ({len(expected)} files)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="BAAI/bge-reranker-v2-m3", help="Model repo id")
    parser.add_argument(
        "--revision",
        default="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        help="Pinned commit SHA",
    )
    parser.add_argument("--out", type=Path, required=True, help="Destination directory")
    parser.add_argument(
        "--verify-manifest",
        type=Path,
        default=None,
        help="sha256 manifest to verify download against",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import hf_hub_download

    print(f"Fetching {args.model} ({args.revision}) to {args.out}...")
    for filename in MODEL_FILES:
        print(f"  downloading {filename}...")
        hf_hub_download(
            repo_id=args.model,
            filename=filename,
            revision=args.revision,
            local_dir=str(args.out),
        )

    print(f"Reranker weights cached under {args.out}")
    if args.verify_manifest:
        verify_manifest(args.out, args.verify_manifest)


if __name__ == "__main__":
    main()
