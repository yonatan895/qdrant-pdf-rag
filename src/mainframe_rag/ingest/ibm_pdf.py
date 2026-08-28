"""IBM-style manual opening: doc number, product/version, TOC, page labels.

Uses PyMuPDF directly (architecture.md section 4.4). No framework.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from mainframe_rag.regexes import DOCNO_RE

# z/OS V2R5 -> 2.5 ; z/OS 3.1 -> 3.1 ; generic VnRn for other products.
PRODUCT_VERSION_RE = re.compile(r"\b(z/?OS|z/?VM|z/?VSE|z/?TPF)\s+(?:V(\d+)\s*R(\d+)|(\d+\.\d+))", re.IGNORECASE)
GENERIC_VR_RE = re.compile(r"\bV(\d+)\s*\.?\s*R(\d+)\b")

# Filenames like SA22-7592-05.pdf or SA22-0000-00_outline.pdf; the (?![\d-])
# lookahead stops the optional edition suffix from being cut by a trailing \b.
FILENAME_DOCNO_RE = re.compile(r"^([A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?)(?![\d-])")


@dataclass
class ParsedDoc:
    path: Path
    sha256: str
    doc_id: str | None
    title: str
    product: str | None
    version: str | None
    vendor: str
    toc: list[tuple[int, str, int]] = field(default_factory=list)  # (level, title, 1-based page)
    page_count: int = 0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _doc_id_from_text(text: str) -> str | None:
    matches = DOCNO_RE.findall(text)
    if not matches:
        return None
    # Most common match wins (title pages repeat the form number).
    return max(set(matches), key=matches.count)


def extract_doc_id(doc: pymupdf.Document, path: Path) -> str | None:
    """Form number from filename, then title pages (first 4)."""
    m = FILENAME_DOCNO_RE.match(path.stem.upper())
    if m:
        return m.group(1)
    text = "\n".join(doc[i].get_text() for i in range(min(4, doc.page_count)))
    return _doc_id_from_text(text)


def extract_product_version(doc: pymupdf.Document) -> tuple[str | None, str | None]:
    """Product and version from the first 4 pages (z/OS V2R5 -> ('z/OS', '2.5'))."""
    text = "\n".join(doc[i].get_text() for i in range(min(4, doc.page_count)))
    m = PRODUCT_VERSION_RE.search(text)
    if m:
        product = "z/OS" if m.group(1).lower().replace("/", "") == "zos" else m.group(1)
        version = f"{m.group(2)}.{m.group(3)}" if m.group(2) else m.group(4)
        return product, version
    m = GENERIC_VR_RE.search(text)
    if m:
        return None, f"{m.group(1)}.{m.group(2)}"
    return None, None


def extract_title(doc: pymupdf.Document, doc_id: str | None) -> str:
    meta_title = (doc.metadata or {}).get("title") or ""
    if meta_title.strip():
        return meta_title.strip()
    first = doc[0].get_text().strip().splitlines() if doc.page_count else []
    for line in first[:10]:
        if line.strip() and not DOCNO_RE.search(line):
            return line.strip()
    return doc_id or "Untitled"


def parse_pdf(path: Path, vendor: str = "IBM") -> ParsedDoc:
    doc = pymupdf.open(path)
    try:
        doc_id = extract_doc_id(doc, path)
        product, version = extract_product_version(doc)
        return ParsedDoc(
            path=path,
            sha256=sha256_file(path),
            doc_id=doc_id,
            title=extract_title(doc, doc_id),
            product=product,
            version=version,
            vendor=vendor,
            toc=doc.get_toc(simple=True),
            page_count=doc.page_count,
        )
    finally:
        doc.close()
