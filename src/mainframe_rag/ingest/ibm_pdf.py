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
# Deliberately anchored, not regexes.DOCNO_RE: filename stems need the
# start anchor + trailing-guard, and doc_id feeds UUID5 point ids — folding
# this into the text-search form risks identity churn, not a cleanup.
FILENAME_DOCNO_RE = re.compile(r"^([A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?)(?![\d-])")

# Invisible / control characters stripped from extracted page text (issue
# #87): terminal escape sequences first (dropping lone ESC would leave
# "[31m" behind), then C0 controls and DEL, Unicode bidi overrides (visual
# spoofing), and zero-width / BOM characters (token smuggling that also
# fractures sparse matching). \t and \n are preserved; every printable
# character — including JCL alignment whitespace — is byte-identical,
# pinned by the verbatim code-card tests.
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff]")


def sanitize_page_text(text: str) -> str:
    """Drop control, bidi-override, and zero-width characters from text
    extracted from a PDF page. Printable content is untouched."""
    text = _CSI_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = _BIDI_RE.sub("", text)
    return _ZERO_WIDTH_RE.sub("", text)


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


def _first_pages_text(doc: pymupdf.Document, pages: int = 4) -> str:
    """One helper for every first-pages scan (doc numbers, product/version):
    two regexes over one join will diverge, and the divergence is the bug."""
    return "\n".join(doc[i].get_text() for i in range(min(pages, doc.page_count)))


def _doc_id_from_text(text: str) -> str | None:
    matches = DOCNO_RE.findall(text)
    if not matches:
        return None
    # sorted() before max(): equal-count ties must not depend on set iteration
    # order — PYTHONHASHSEED differs per spawn worker, so an unsorted tie break
    # flips doc_id between runs and churns the resume path (found on a real
    # z/OS corpus: DCF books carry several form numbers with equal counts).
    return max(sorted(set(matches)), key=matches.count)


def _doc_id_from_doc(doc: pymupdf.Document, path: Path) -> str | None:
    m = FILENAME_DOCNO_RE.match(path.stem.upper())
    if m:
        return m.group(1)
    return _doc_id_from_text(_first_pages_text(doc))


def extract_doc_id(doc: pymupdf.Document, path: Path) -> str | None:
    """Doc id from an open document (parse path)."""
    return _doc_id_from_doc(doc, path)


def resolve_doc_id(path: Path) -> str | None:
    """Best-effort doc id for the planning prescan (issue #361): the
    filename-form match needs no open; otherwise open and scan the first
    pages through the same helper the parse path resolves through, so
    prescan keys and worker doc_ids cannot diverge — including the
    filename-stem fallback (``parse_pdf`` resolves
    ``extract_doc_id(...) or path.stem``). Returns None only when the file
    cannot be opened or read: prescan validates identity, never file
    health, so it never raises — the parse worker owns that error, and an
    unreadable file must neither cause nor hide a collision (a file that
    cannot be read can never become a delete/upsert writer, so excluding
    it misses nothing)."""
    path = Path(path)
    m = FILENAME_DOCNO_RE.match(path.stem.upper())
    if m:
        return m.group(1)
    try:
        doc = pymupdf.open(path)
    except Exception:  # noqa: BLE001 — unreadable input is the worker's error, not prescan's
        return None
    try:
        return _doc_id_from_text(_first_pages_text(doc)) or path.stem
    except Exception:  # noqa: BLE001 — same: never fail planning on file health
        return None
    finally:
        doc.close()


def extract_product_version(doc: pymupdf.Document) -> tuple[str | None, str | None]:
    text = _first_pages_text(doc)
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
            title=sanitize_page_text(extract_title(doc, doc_id)),
            product=product_f,
            version=version_f or None,
            vendor=vendor_f or "unknown",
            toc=tuple(doc.get_toc(simple=True)),
            page_count=doc.page_count,
        )
    finally:
        doc.close()


# --- Page text extraction (issues #85, #87, #271) ---------------------------
# Lives here, not in run_ingest.py, because this module is hashed into
# extraction_rules_version: a payload-changing extraction rule outside the
# hashed modules would let resumed ingests silently keep stale payloads.

# A block is "cell-like" (a table column, not prose) when its lines are
# short: at most this many words on average and per line. Two-column prose
# runs 8+ words per line, so it never qualifies and keeps stream order.
_CELL_MEAN_WORDS = 5.0
_CELL_MAX_WORDS = 8
# A column set is only reassembled into rows when at least this many
# baselines carry two or more cells: header/footer pairs and
# a lone "title ... page" line stay untouched.
_MIN_TABLE_ROWS = 3
# Consecutive cell-like blocks join one column set while each lies within this
# many line heights of the set's vertical extent (MuPDF splits a column into
# several blocks wherever a wrapped row opens a larger gap).
_MAX_GAP_LINES = 1.5
_CHANGE_BAR_LINE = "|"


def _strip_change_bars(text: str) -> str:
    """Drop lines that are only an IBM change-bar glyph. Bars are margin
    marks, never content; a lone ``|`` line only pollutes chunk text and
    embeddings. A ``|`` inside a longer line is untouched."""
    if _CHANGE_BAR_LINE not in text:
        return text
    kept = [ln for ln in text.split("\n") if ln.strip() != _CHANGE_BAR_LINE]
    return "\n".join(kept)


def _is_cell_block(text: str) -> bool:
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return False
    counts = [len(ln.split()) for ln in lines]
    return max(counts) <= _CELL_MAX_WORDS and sum(counts) / len(counts) <= _CELL_MEAN_WORDS


def _same_row(a: tuple[float, float], b: tuple[float, float]) -> bool:
    ca, cb = (a[0] + a[1]) / 2, (b[0] + b[1]) / 2
    return a[0] <= cb <= a[1] and b[0] <= ca <= b[1]


def _table_rows(
    group: list[tuple[int, str]],
    line_boxes: dict[tuple[int, int], tuple[float, float, float, float]],
) -> list[str] | None:
    """Rebuild row lines from a set of column blocks, or None when the
    geometry does not describe a table (then the caller keeps stream
    order). group: (block_no, block text) in stream order."""
    cells: list[tuple[float, float, float, int, str]] = []  # y0, y1, x0, block, text
    for block_no, text in group:
        for line_no, ln in enumerate(text.split("\n")):
            if not ln.strip():
                continue
            box = line_boxes.get((block_no, line_no))
            if box is None:
                return None
            cells.append((box[1], box[3], box[0], block_no, ln.strip()))
    cells.sort(key=lambda c: (c[0], c[2]))
    rows: list[list[tuple[float, float, float, int, str]]] = []
    for cell in cells:
        if rows and _same_row((rows[-1][0][0], rows[-1][0][1]), (cell[0], cell[1])):
            rows[-1].append(cell)
        else:
            rows.append([cell])
    multi = sum(1 for r in rows if len(r) >= 2)
    if multi < _MIN_TABLE_ROWS:
        return None
    return [" ".join(c[4] for c in sorted(r, key=lambda c: c[2])) for r in rows]


def extract_page_text(page: pymupdf.Page) -> str:
    """Page text, with column-major drawn tables reassembled into rows.

    Plain ``get_text()`` follows content-stream order. A table drawn column
    by column therefore extracts as one list per column and the row/value
    association (``MAXJOBS 200 Maximum queued jobs``) is lost before
    chunking (issue #85). ``get_text(sort=True)`` is not the fix: in current
    PyMuPDF it simulates layout (padding, blank lines, cross-column lines)
    and interleaves multi-column prose line by line.

    The correction is therefore narrow. Consecutive text blocks that are
    vertically adjacent or overlapping and *cell-like* (short lines, see
    _is_cell_block) form a column set; only when at least
    _MIN_TABLE_ROWS baselines carry two or more cells is the set
    emitted as one line per baseline (cells joined by a single space). Prose,
    including multi-column prose, never qualifies and stays byte-identical
    to plain extraction, as does any page where the rebuilt text would not
    reproduce plain extraction (checked, not assumed). Bare change-bar lines
    are dropped (see _strip_change_bars).
    """
    plain = page.get_text()
    blocks = [b for b in page.get_text("blocks") if len(b) >= 7 and b[6] == 0]
    if not blocks or "".join(b[4] for b in blocks) != plain:
        return _strip_change_bars(plain)

    # (block_no, bbox, text without change bars, original text); blocks that
    # were nothing but change bars vanish here.
    entries = [
        (b[5], (b[0], b[1], b[2], b[3]), stripped, b[4])
        for b in blocks
        if (stripped := _strip_change_bars(b[4])).strip()
    ]
    groups: list[tuple[int, int]] = []  # [start, end) indexes into entries
    i = 0
    while i < len(entries):
        if not _is_cell_block(entries[i][2]):
            i += 1
            continue
        j = i + 1
        y0, y1 = entries[i][1][1], entries[i][1][3]
        while j < len(entries) and _is_cell_block(entries[j][2]):
            _, by0, _, by1 = entries[j][1]
            lines = max(1, sum(1 for ln in entries[j][2].split("\n") if ln.strip()))
            gap = max(by0 - y1, y0 - by1)  # negative when the ranges overlap
            if gap > _MAX_GAP_LINES * (by1 - by0) / lines:
                break
            y0, y1 = min(y0, by0), max(y1, by1)
            j += 1
        groups.append((i, j))
        i = max(j, i + 1)
    if not groups:
        return _strip_change_bars(plain)

    line_boxes: dict[tuple[int, int], tuple[float, float, float, float]] = {}
    for w in page.get_text("words"):
        key = (w[5], w[6])
        x0, y0, x1, y1 = line_boxes.get(key, (w[0], w[1], w[2], w[3]))
        line_boxes[key] = (min(x0, w[0]), min(y0, w[1]), max(x1, w[2]), max(y1, w[3]))

    out: list[str] = []
    cursor = 0
    for start, end in groups:
        out.extend(e[2] for e in entries[cursor:start])
        # Word-based line indexes only line up with unmodified block text, so
        # a block that also carried change-bar lines keeps stream order.
        rows = None
        if all(e[2] == e[3] for e in entries[start:end]):
            rows = _table_rows([(e[0], e[2]) for e in entries[start:end]], line_boxes)
        if rows is None:
            out.extend(e[2] for e in entries[start:end])
        else:
            out.append("\n".join(rows) + "\n")
        cursor = end
    out.extend(e[2] for e in entries[cursor:])
    return "".join(out)


def _page_label(page: pymupdf.Page) -> str | None:
    """Printed label, or None when the page has none (issue #271).

    A /PageLabels tree whose first rule starts after page 0 leaves the
    earlier pages unlabeled, and PyMuPDF's get_label() raises IndexError on
    them instead of returning ''. Treat that page's label as absent rather
    than failing the whole document: chunking cites a physical-page
    fallback for any span that is not fully labeled."""
    try:
        return page.get_label()
    except IndexError:
        return None


def _extract_page_texts(doc: pymupdf.Document) -> tuple[list[str], list[str | None]]:
    """Page texts sanitized at extraction plus page labels, in page order.

    Lives beside the other payload rules (hashed into extraction_rules_version)
    and is unit-testable with a stub document: control/bidi/zero-width
    characters are dropped by sanitize_page_text (issue #87) before chrome
    detection sees the text, since those characters would also fracture
    chrome line-matching. Table rows are rebuilt by extract_page_text
    (issue #85). Labels pass through untouched, except that an unreadable
    label is absent (see _page_label).
    """
    page_texts: list[str] = []
    page_labels: list[str | None] = []
    for i in range(doc.page_count):
        page = doc[i]
        page_texts.append(sanitize_page_text(extract_page_text(page)))
        page_labels.append(_page_label(page))
    return page_texts, page_labels
