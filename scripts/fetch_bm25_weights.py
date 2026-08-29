#!/usr/bin/env python3
"""Fetch FastEmbed BM25 sparse weights into a directory for image baking.

Run on the CONNECTED host only (make bm25-weights). The output directory is
copied into the ingest and agent images so the air-gap never downloads.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qdrant/bm25")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    # Instantiating the model materializes its files under the cache dir.
    from fastembed import SparseTextEmbedding

    SparseTextEmbedding(model_name=args.model, cache_dir=str(args.out))
    print(f"BM25 weights for {args.model} cached under {args.out}")


if __name__ == "__main__":
    main()
