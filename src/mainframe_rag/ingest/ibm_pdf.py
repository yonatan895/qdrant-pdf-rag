"""PDF opening: doc number when present, otherwise filename stem.

Works for IBM-style manuals and any other text-layer PDF. PyMuPDF only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from mainframe_rag.ingest.walk import detect_vendor, infer_from_path
from mainframe_rag.regexes import DOCNO_RE

PRODUCT_VERSION_RE = re.compile(
    r"\b(z/?OS|z/?VM|z/?VSE|z/?TPF)\s+(?:V(\d+)\s*R(\d+)|(\d+\.\d+))",
    re.IGNORECASE,
)
GENERIC_VR_RE = re.compile(r"\bV(\d+)\s*\.?\s*R(\d+)\b")
FILENAME_DOCNO_RE = re.compile(r"^([A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?)(?![\d-])")


@dataclass(frozen=True, slots=True)
class ParsedDoc:
    path: Path
    sha256: str
    doc_id: str
    title: str
    product: str | None = None
    version: str | None = None
    vendor: str = "unknown"
    toc: tuple[tuple[int, str, int], ...] = ()
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
    # sorted() before max(): equal-count ties must not depend on set iteration
    # order — PYTHONHASHSEED differs per spawn worker, so an unsorted tie break
    # flips doc_id between runs and churns the resume path (found on a real
    # z/OS corpus: DCF books carry several form numbers with equal counts).
    return max(sorted(set(matches)), key=matches.count)


def extract_doc_id(doc: pymupdf.Document, path: Path) -> str | None:
    m = FILENAME_DOCNO_RE.match(path.stem.upper())
    if m:
        return m.group(1)
    text = "\n".join(doc[i].get_text() for i in range(min(4, doc.page_count)))
    return _doc_id_from_text(text)


def extract_product_version(doc: pymupdf.Document) -> tuple[str | None, str | None]:
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


def parse_pdf(
    path: Path,
    vendor: str | None = None,
    product: str | None = None,
    version: str | None = None,
    corpus_root: Path | None = None,
    sha256: str | None = None,
) -> ParsedDoc:
    """sha256: caller-supplied digest to avoid re-reading the file — callers
    that already hashed for the inventory skip-check pass it through."""
    path = Path(path)
    doc = pymupdf.open(path)
    try:
        doc_id = extract_doc_id(doc, path) or path.stem
        text_product, text_version = extract_product_version(doc)
        lv, lp, lver = ("unknown", "unknown", "")
        if corpus_root is not None:
            lv, lp, lver = infer_from_path(path, corpus_root)
        vendor_f = vendor or (lv if lv != "unknown" else detect_vendor(path))
        product_f = product or (lp if lp != "unknown" else text_product)
        version_f = version or lver or text_version
        return ParsedDoc(
            path=path,
            sha256=sha256 or sha256_file(path),
            doc_id=doc_id,
            title=extract_title(doc, doc_id),
            product=product_f,
            version=version_f or None,
            vendor=vendor_f or "unknown",
            toc=tuple(doc.get_toc(simple=True)),
            page_count=doc.page_count,
        )
    finally:
        doc.close()
