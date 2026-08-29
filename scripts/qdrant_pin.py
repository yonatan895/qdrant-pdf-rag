#!/usr/bin/env python3
"""Print the pinned Qdrant image from images.txt (single parser for the
simulation tier: pytest fixture and `make sim-qdrant` both read this)."""

from __future__ import annotations

import sys
from pathlib import Path


def qdrant_image_pin(images_txt: Path) -> str:
    """First qdrant line's name column — a pin bump is picked up automatically."""
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        fields = line.split()
        if fields and "qdrant" in fields[0]:
            return fields[0]
    raise ValueError(f"no qdrant image pin found in {images_txt}")


def main() -> int:
    images_txt = Path(__file__).resolve().parents[1] / "images.txt"
    try:
        print(qdrant_image_pin(images_txt))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
