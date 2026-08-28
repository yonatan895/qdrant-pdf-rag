"""Recursive PDF walk. Collects *.pdf only; ignores .pdx/.idx catalogs (they are
Adobe Reader search indexes, not a RAG source). Vendor is inferred from the path."""

from __future__ import annotations

from pathlib import Path

VENDOR_MARKERS = {
    "broadcom": "Broadcom",
    "ca-": "Broadcom",
    "/ca/": "Broadcom",
    "bmc": "BMC",
    "precisely": "Precisely",
}

_IGNORED_DIRS = {".", "..", "__MACOSX", "lost+found"}


def detect_vendor(path: Path) -> str:
    p = str(path).lower()
    for marker, vendor in VENDOR_MARKERS.items():
        if marker in p:
            return vendor
    return "IBM"


def walk_pdfs(root: Path) -> list[Path]:
    """Return sorted .pdf files under root. Skips hidden dirs and macOS metadata."""
    pdfs: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith(".") or part in _IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() != ".pdf":
            continue  # .pdx / .idx / everything else: not our input
        pdfs.append(path)
    return pdfs
