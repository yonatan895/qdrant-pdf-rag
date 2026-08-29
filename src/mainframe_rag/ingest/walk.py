"""Recursive PDF walk. Collects *.pdf only; ignores .pdx/.idx catalogs."""

from __future__ import annotations

from pathlib import Path

VENDOR_MARKERS = {
    "broadcom": "Broadcom",
    "ca-": "Broadcom",
    "/ca/": "Broadcom",
    "bmc": "BMC",
    "precisely": "Precisely",
    "ibm": "IBM",
    "red-hat": "Red Hat",
    "redhat": "Red Hat",
}

# Dot-prefixed dirs are already excluded by the startswith(".") check; these
# are the non-dot dirs to skip.
_IGNORED_DIRS = {"__MACOSX", "lost+found"}


def infer_from_path(pdf: Path, root: Path) -> tuple[str, str, str]:
    """If corpus is root/vendor/product/version/*.pdf, use that. Else unknown."""
    try:
        rel = pdf.resolve().relative_to(root.resolve())
    except ValueError:
        return "unknown", "unknown", ""
    parts = rel.parts
    if len(parts) >= 4:
        return parts[0], parts[1], parts[2]
    return "unknown", "unknown", ""


def detect_vendor(path: Path) -> str:
    p = str(path).lower().replace("\\", "/")
    for marker, vendor in VENDOR_MARKERS.items():
        if marker in p:
            return vendor
    return "unknown"


def walk_pdfs(root: Path) -> list[Path]:
    pdfs: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith(".") or part in _IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() != ".pdf":
            continue
        pdfs.append(path)
    return pdfs
