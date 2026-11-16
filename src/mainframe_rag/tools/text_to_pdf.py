"""Render plain-text sources as PDFs that the existing ingest reads (#1277).

Ingest walks ``*.pdf`` only, and any byte change to an ingest rule module
changes ``extraction_rules_version()``, so text sources are converted outside
ingest instead. Two inputs:

* ``library``: a directory of members exported from one PDS (macros, samples,
  procedures, shop JCL/REXX/HLASM/C). Output: one PDF per library, one top-level
  bookmark per member (bookmark = member name), monospace, columns kept.
* ``markdown``: one Markdown document (the house guides). The H1 is the PDF
  /Title, H2 and H3 are nested bookmarks; fenced code is monospace.

The output is shaped for the rules as they are (docs/install_and_ops.md,
"Adding documents to the corpus"):

* every page carries the same four-line header and footer, which the chrome
  stripper removes, so no source line sits at a page edge (edge lines that
  repeat, look like folios or read like copyright notices are deleted there);
* the document has at least ``MIN_PAGES_FOR_CHROME`` pages and starts with
  ``COVER_PAGES`` pages that carry only that header and footer, so the
  first-pages scan for an IBM form number never reads source text and the doc
  id is always the file stem;
* each member or bookmarked heading starts a new page (page height grows with
  the content, up to ``MAX_LINES_PER_PAGE`` lines), and no page labels are set,
  so a citation reads ``<doc id> <title>, <member>, p. PDF n``.

Before the output is moved into place, the real parse and chunk path
(``parse_pdf``, ``_extract_page_texts``, ``strip_chrome``, ``make_chunks``)
runs on it, and the conversion is refused unless the doc id, title, outline,
every page's text and every chunk's heading are the expected ones.

The tool reads local files and writes one PDF. It opens no network connection
and never contacts z/OS. It adds nothing but the given title and file stem to
the source text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pymupdf

from mainframe_rag.agent.cites import _GENERIC_STEM_LED_RE
from mainframe_rag.ingest.chrome import EDGE_LINES, MIN_PAGES_FOR_CHROME, strip_chrome
from mainframe_rag.ingest.chunk import make_chunks
from mainframe_rag.ingest.ibm_pdf import (
    FILENAME_DOCNO_RE,
    TITLE_MAX_CHARS,
    _extract_page_texts,
    parse_pdf,
    resolve_doc_id,
)
from mainframe_rag.ingest.walk import detect_vendor, infer_from_path
from mainframe_rag.regexes import DOCNO_RE, FRONT_MATTER_RE, SKIP_ALWAYS_RE
from mainframe_rag.retrieve.query import (
    _CITE_DOCNO_RE,
    _CITE_PAGE_MARK_RE,
    CITE_HEADING_MAX_CHARS,
    CITE_TITLE_MAX_CHARS,
    _cite_part,
    clean_cite_field,
)

# --- Size caps (refused above these) ----------------------------------------
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_LIBRARY_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_PAGES = 50_000
MAX_COLUMNS = 1024
DEFAULT_COLUMNS = 80
SEQUENCE_COLUMNS = (73, 80)

# --- Layout -----------------------------------------------------------------
# ibm_pdf scans the first four pages for an IBM form number and prefers it to
# the file stem; the cover keeps source text out of that window.
COVER_PAGES = 4
MAX_LINES_PER_PAGE = 1000
MARGIN = 36.0
PAGE_WIDTH_MIN = 612.0
PAGE_HEIGHT_MIN = 792.0
PROSE_WRAP_WIDTH = PAGE_WIDTH_MIN - 2 * MARGIN
GAP = 12.0
CODE = ("cour", 9.0, 11.0)
PROSE = ("helv", 10.0, 12.0)
BOLD = ("hebo", 10.0, 12.0)
H2 = ("hebo", 13.0, 17.0)
H3 = ("hebo", 11.5, 15.0)
GUARD = ("helv", 8.0, 10.0)
CREATOR = "mainframe-rag text_to_pdf"

# PDS member names: 1-8 characters, A-Z 0-9 @ # $, not starting with a digit.
MEMBER_NAME_RE = re.compile(r"[A-Z@#$][A-Z0-9@#$]{0,7}")
SHOP_NAME_MAX_CHARS = 64
# A member whose name the outline rules skip by name (an "...INDEX" or
# "NOTICES" member, an early "CONTENTS") would get no section of its own and
# its pages would join the previous member's chunks. It is bookmarked with
# this suffix instead, which no PDS name can carry.
SKIPPED_NAME_SUFFIX = " (member)"

# Typographic punctuation in Markdown prose mapped to the Latin-1 text the
# monospace and Helvetica base fonts can carry. Anything else outside
# printable Latin-1 is refused.
MARKDOWN_PUNCTUATION = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "--", "\u2026": "...", "\u2022": "-",
    "\u2192": "->", "\u2190": "<-", "\u00a0": " ",
})

# Literal credentials on keyword operands (PASSWORD=..., API_KEY: ...); a
# value naming a variable (&PW, $PW, %PW%, <pw>) is not a literal.
_SECRET_RE = re.compile(
    r"\b(?:PASSWORD|PASSWD|PASSWRD|PASSPHRASE|NEWPASS|PWD|PASS|API_?KEY|SECRET|TOKEN)"
    r"\s*[=:]\s*['\"]?(?P<value>[^\s,;)'\"]{3,})",
    re.IGNORECASE,
)
_NOT_LITERAL = ("&", "$", "%", "<", "{", "*", "(")

_HEADING_RE = re.compile(r"(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"(`{3,}|~{3,})")
_LIST_RE = re.compile(r"\s*(?:[-*+]|\d{1,3}[.)])\s+")


class ConversionError(Exception):
    """A refused input or a failed self-check. The message names files,
    members and line numbers, never source text."""


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    style: tuple[str, float, float]  # font, size, leading


@dataclass(slots=True)
class Page:
    lines: list[Line] = field(default_factory=list)
    # (level, title) of the bookmarks that start on this page, in order.
    bookmarks: list[tuple[int, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Plan:
    stem: str
    title: str
    description: str
    pages: tuple[Page, ...]
    notices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Result:
    path: Path
    sha256: str
    pages: int
    bookmarks: int
    chunks: int
    notices: tuple[str, ...]


# --- Validation -------------------------------------------------------------
def _printable_latin1(ch: str) -> bool:
    code = ord(ch)
    return 0x20 <= code <= 0x7E or (0xA1 <= code <= 0xFF and code != 0xAD)


def _check_chars(text: str, where: str) -> None:
    for ch in text:
        if not _printable_latin1(ch):
            raise ConversionError(
                f"{where}: character U+{ord(ch):04X} cannot be rendered (printable Latin-1 only)"
            )


def check_cite_field(value: str, what: str, doc_id: str, limit: int = TITLE_MAX_CHARS) -> None:
    """Refuse a title or bookmark the citation would change or misread: a
    comma (the cite's field separator), a page mark ("p. 5"), an IBM form
    number, control or non-Latin-1 characters, stray whitespace, or more than
    `limit` characters. The citation sanitizer itself is the oracle."""
    if not value or not any(ch.isalnum() for ch in value):
        raise ConversionError(f"{what} is empty or has no letter or digit")
    _check_chars(value, what)
    if "," in value:
        raise ConversionError(f"{what} contains a comma, which separates citation fields")
    if _CITE_PAGE_MARK_RE.search(value):
        raise ConversionError(f"{what} contains a page mark (p. or pp.)")
    if _CITE_DOCNO_RE.search(value) or DOCNO_RE.search(value):
        raise ConversionError(f"{what} contains an IBM form number")
    if " > " in value:
        raise ConversionError(f"{what} contains ' > ', the heading path separator")
    if len(value) > limit:
        raise ConversionError(f"{what} is longer than {limit} characters")
    if _cite_part(clean_cite_field(value, CITE_TITLE_MAX_CHARS), doc_id) != value:
        raise ConversionError(f"{what} would change in a citation (spacing or characters)")


def check_stem(stem: str) -> None:
    """The output file stem is the doc id (filename-stem identity): it must
    have the generic stem shape citations recognize and must not read as an
    IBM form number."""
    led = _GENERIC_STEM_LED_RE.match(stem + " ")
    if led is None or led.end() != len(stem) + 1 or len(stem) > 100:
        raise ConversionError(
            "output name must be letters and digits joined by - _ or . "
            "(at least two parts, at most 100 characters), e.g. SITE-MACLIB-SYSA-20261117"
        )
    if FILENAME_DOCNO_RE.match(stem.upper()) or DOCNO_RE.search(stem):
        raise ConversionError("output name must not contain an IBM form number")


def skipped_by_outline_rules(title: str) -> bool:
    """True when ingest's outline rules would skip a bookmark with this title
    (back matter anywhere, front matter early in the book)."""
    return bool(SKIP_ALWAYS_RE.search(title) or FRONT_MATTER_RE.search(title))


def _secret_lines(lines: Sequence[str]) -> list[int]:
    found = []
    for number, line in enumerate(lines, 1):
        for match in _SECRET_RE.finditer(line):
            if not match.group("value").startswith(_NOT_LITERAL):
                found.append(number)
                break
    return found


def _decode(raw: bytes, encoding: str, where: str) -> list[str]:
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise ConversionError(f"{where}: cannot decode as {encoding}") from exc
    text = text.removeprefix("\ufeff").replace("\r\n", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.expandtabs(8) for line in lines]


# --- Member libraries -------------------------------------------------------
def member_name(file_name: str, shop_names: bool) -> str:
    """Bookmark name for one exported member file. PDS names: the file name
    without one extension, upper-cased, validated against PDS naming. Shop
    names: the file name as is, validated for citation safety only."""
    if shop_names:
        if len(file_name) > SHOP_NAME_MAX_CHARS:
            raise ConversionError(f"{file_name!r}: shop file name longer than {SHOP_NAME_MAX_CHARS}")
        return file_name
    base = file_name.rsplit(".", 1)[0] if "." in file_name[1:] else file_name
    name = base.upper()
    if not MEMBER_NAME_RE.fullmatch(name):
        raise ConversionError(
            f"{file_name!r}: not a PDS member name (1-8 of A-Z 0-9 @ # $, not starting "
            "with a digit); use --shop-names for shop file names"
        )
    return name


def _member_files(src: Path) -> list[Path]:
    if not src.is_dir():
        raise ConversionError(f"{src}: not a directory")
    files = []
    for entry in sorted(src.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink() or not entry.is_file():
            raise ConversionError(f"{entry.name!r}: not a regular file (symlinks and folders are refused)")
        files.append(entry)
    if not files:
        raise ConversionError(f"{src}: no member files")
    if len(files) > MAX_MEMBERS:
        raise ConversionError(f"more than {MAX_MEMBERS} members")
    return files


def _member_lines(raw: bytes, name: str, encoding: str, columns: int, strip_sequence: bool) -> list[str]:
    lines = []
    for number, line in enumerate(_decode(raw, encoding, f"member {name}"), 1):
        where = f"member {name} line {number}"
        _check_chars(line, where)
        if len(line.rstrip()) > columns:
            raise ConversionError(f"{where}: longer than {columns} columns (see --columns)")
        if strip_sequence:
            line = line[: SEQUENCE_COLUMNS[0] - 1]
        lines.append(line.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def _code_pages(lines: list[str], bookmarks: list[tuple[int, str]]) -> list[Page]:
    """One member or code block as pages: blank records render as a single
    space so they stay paragraph breaks in the extracted text."""
    pages = []
    for start in range(0, max(len(lines), 1), MAX_LINES_PER_PAGE):
        chunk = lines[start : start + MAX_LINES_PER_PAGE]
        pages.append(Page([Line(text or " ", CODE) for text in chunk], bookmarks if start == 0 else []))
    return pages


def plan_library(
    src: Path,
    stem: str,
    title: str,
    *,
    columns: int = DEFAULT_COLUMNS,
    strip_sequence: bool = False,
    shop_names: bool = False,
    encoding: str = "utf-8",
    allow_secret_like: bool = False,
) -> Plan:
    check_stem(stem)
    check_cite_field(title, "title", stem, CITE_TITLE_MAX_CHARS)
    if not 1 <= columns <= MAX_COLUMNS:
        raise ConversionError(f"--columns must be 1-{MAX_COLUMNS}")
    if strip_sequence and columns != SEQUENCE_COLUMNS[1]:
        raise ConversionError("--strip-sequence needs 80-column records (columns 73-80)")
    files = _member_files(src)
    names: dict[str, str] = {}
    pages: list[Page] = []
    notices: list[str] = []
    secrets: list[str] = []
    total = 0
    for path in files:
        name = member_name(path.name, shop_names)
        if name.upper() in names:
            raise ConversionError(f"{path.name!r} and {names[name.upper()]!r} are the same member name")
        names[name.upper()] = path.name
        size = path.stat().st_size
        total += size
        if size > MAX_MEMBER_BYTES or total > MAX_LIBRARY_BYTES:
            raise ConversionError(f"member {name}: size cap exceeded")
        bookmark = name
        if skipped_by_outline_rules(name):
            bookmark = name + SKIPPED_NAME_SUFFIX
            notices.append(f"member {name} is bookmarked as {bookmark!r} (ingest skips the bare name)")
        check_cite_field(bookmark, f"member name {name!r}", stem)
        lines = _member_lines(path.read_bytes(), name, encoding, columns, strip_sequence)
        secrets.extend(f"{name}:{n}" for n in _secret_lines(lines))
        if not lines:
            notices.append(f"member {name} is empty (bookmarked, no chunks)")
        pages.extend(_code_pages(lines, [(1, bookmark)]))
    if secrets and not allow_secret_like:
        raise ConversionError(
            "possible literal credentials at " + ", ".join(secrets[:20])
            + (" ..." if len(secrets) > 20 else "")
            + "; remove them, or review and rerun with --allow-secret-like"
        )
    return Plan(stem, title, f"Converted member library, {len(files)} members, one bookmark per member",
                tuple(pages), tuple(notices))


# --- Markdown documents -----------------------------------------------------
def _wrap(text: str, style: tuple[str, float, float], width: float, indent: str = "") -> list[str]:
    """Greedy word wrap measured in the rendering font. Deterministic."""
    font, size, _ = style

    def fits(candidate: str) -> bool:
        return pymupdf.get_text_length(candidate, fontname=font, fontsize=size) <= width

    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else (indent if lines else "") + word
        if fits(candidate):
            current = candidate
            continue
        if current:
            lines.append(current)
        current = (indent if lines else "") + word
        while not fits(current):  # a word wider than the line: hard split
            cut = len(current) - 1
            while cut > 1 and not fits(current[:cut]):
                cut -= 1
            lines.append(current[:cut])
            current = indent + current[cut:]
    if current:
        lines.append(current)
    return lines


def plan_markdown(src: Path, *, encoding: str = "utf-8", allow_secret_like: bool = False) -> Plan:
    stem = src.stem
    check_stem(stem)
    if not src.is_file() or src.is_symlink():
        raise ConversionError(f"{src.name!r}: not a regular file")
    if src.stat().st_size > MAX_MEMBER_BYTES:
        raise ConversionError(f"{src.name!r}: size cap exceeded")
    lines = [ln.translate(MARKDOWN_PUNCTUATION) for ln in _decode(src.read_bytes(), encoding, src.name)]
    for number, line in enumerate(lines, 1):
        _check_chars(line, f"{src.name} line {number}")
    secrets = _secret_lines(lines)
    if secrets and not allow_secret_like:
        raise ConversionError(
            f"possible literal credentials at {src.name} lines "
            + ", ".join(map(str, secrets[:20]))
            + "; remove them, or review and rerun with --allow-secret-like"
        )

    title: str | None = None
    pages: list[Page] = []
    page = Page()
    last_was_heading = False
    h2: str | None = None
    prose: list[str] = []
    prose_style = PROSE
    prose_indent = ""
    fence: str | None = None
    fence_line = 0
    code_block: list[str] = []
    paths: set[str] = set()
    table: list[str] = []  # consecutive Markdown table rows, kept verbatim

    def add(new: list[Line]) -> None:
        nonlocal page
        if page.lines and new:
            page.lines.append(Line(" ", CODE))
        for line in new:
            if len(page.lines) >= MAX_LINES_PER_PAGE:
                pages.append(page)
                page = Page()
            page.lines.append(line)

    def flush() -> None:
        nonlocal prose, prose_style, prose_indent, last_was_heading, table
        if table:
            add([Line(t, CODE) for t in table])
            table = []
            last_was_heading = False
        if prose:
            add([Line(t, prose_style) for t in _wrap(" ".join(prose), prose_style, PROSE_WRAP_WIDTH, prose_indent)])
            last_was_heading = False
        prose, prose_style, prose_indent = [], PROSE, ""

    for number, raw in enumerate(lines, 1):
        where = f"{src.name} line {number}"
        if fence is not None:
            if raw.strip().startswith(fence) and not raw.strip().strip(fence[0]):
                fence = None
                while code_block and not code_block[-1]:
                    code_block.pop()
                add([Line(t or " ", CODE) for t in code_block])
                last_was_heading = last_was_heading and not code_block
                continue
            code_block.append(raw.rstrip())
            continue
        if fenced := _FENCE_RE.match(raw.strip()):
            flush()
            fence = fenced.group(1)
            code_block = []
            fence_line = number
            continue
        if title is None and raw.strip() and not raw.startswith("# "):
            raise ConversionError(f"{where}: the document must start with one H1 (# Title)")
        heading = _HEADING_RE.fullmatch(raw)
        if heading:
            flush()
            level, text = len(heading.group(1)), heading.group(2)
            if level == 1:
                if title is not None:
                    raise ConversionError(f"{where}: a second H1; the document has one title")
                check_cite_field(text, f"{where} H1 (the title)", stem, CITE_TITLE_MAX_CHARS)
                title = text
                continue
            if level <= 3:
                if level == 3 and h2 is None:
                    raise ConversionError(f"{where}: an H3 before any H2")
                check_cite_field(text, f"{where} heading", stem)
                if skipped_by_outline_rules(text):
                    raise ConversionError(f"{where}: ingest skips a heading named like this; rename it")
                path = text if level == 2 else f"{h2} > {text}"
                if len(path) > CITE_HEADING_MAX_CHARS:
                    raise ConversionError(f"{where}: heading path longer than {CITE_HEADING_MAX_CHARS}")
                paths.add(path)
                h2 = text if level == 2 else h2
                if page.lines and not last_was_heading:
                    pages.append(page)
                    page = Page()
                page.bookmarks.append((level - 1, text))
                add([Line(text, H2 if level == 2 else H3)])
                last_was_heading = True
                continue
            prose, prose_style = [text], BOLD
            flush()
            continue
        if not raw.strip():
            flush()
            continue
        if raw.lstrip().startswith("|"):
            if prose:
                flush()
            table.append(raw.rstrip())
            continue
        if table:
            flush()
        if _LIST_RE.match(raw):
            flush()
            prose, prose_indent = [raw.strip()], "  "
            continue
        prose.append(raw.strip())
    if fence is not None:
        raise ConversionError(f"{src.name} line {fence_line}: code fence is never closed")
    flush()
    if title is None:
        raise ConversionError(f"{src.name}: no H1 title")
    if page.lines or page.bookmarks:
        pages.append(page)
    return Plan(stem, title, f"Converted Markdown document, {len(paths)} bookmarked headings",
                tuple(pages))


# --- Rendering --------------------------------------------------------------
def _guard_lines(plan: Plan) -> list[str]:
    """The header (and, reversed, the footer) on every page: identical on all
    pages, so ingest strips them as page chrome and no source line is ever
    within EDGE_LINES of a page edge."""
    lines = [plan.title, f"Document {plan.stem}", plan.description, f"Generated by {CREATOR}"]
    assert len(lines) == EDGE_LINES
    return lines


def _width(text: str, style: tuple[str, float, float]) -> float:
    return float(pymupdf.get_text_length(text, fontname=style[0], fontsize=style[1]))


def _creation_stamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise ConversionError("--creation-date must be YYYY-MM-DD") from exc
    return f"D:{day:%Y%m%d}000000Z"


def render(plan: Plan, creation_date: str | None = None) -> tuple[bytes, list[list[str]]]:
    """PDF bytes plus the expected chrome-stripped text of every page.
    Byte-deterministic: no timestamps unless one is given, no random file id."""
    guard = _guard_lines(plan)
    cover = max(COVER_PAGES, MIN_PAGES_FOR_CHROME - len(plan.pages))
    total = cover + len(plan.pages)
    if total > MAX_PAGES:
        raise ConversionError(f"output would have more than {MAX_PAGES} pages")
    widths = [_width(t, GUARD) for t in guard]
    widths += [_width(line.text, line.style) for page in plan.pages for line in page.lines]
    page_width = max(PAGE_WIDTH_MIN, 2 * MARGIN + max(widths))
    guard_height = EDGE_LINES * GUARD[2]
    doc = pymupdf.open()
    toc: list[list[int | str]] = []
    expected: list[list[str]] = []
    for index in range(total):
        content = plan.pages[index - cover] if index >= cover else Page()
        body = sum(line.style[2] for line in content.lines)
        height = max(PAGE_HEIGHT_MIN, 2 * MARGIN + 2 * guard_height + 2 * GAP + body)
        page = doc.new_page(width=page_width, height=height)
        y = MARGIN
        for text in guard:
            y += GUARD[2]
            page.insert_text((MARGIN, y), text, fontname=GUARD[0], fontsize=GUARD[1])
        y += GAP
        for line in content.lines:
            y += line.style[2]
            page.insert_text((MARGIN, y), line.text, fontname=line.style[0], fontsize=line.style[1])
        y = height - MARGIN - guard_height
        for text in reversed(guard):
            y += GUARD[2]
            page.insert_text((MARGIN, y), text, fontname=GUARD[0], fontsize=GUARD[1])
        toc.extend([level, title, index + 1] for level, title in content.bookmarks)
        expected.append([line.text for line in content.lines])
    doc.set_toc(toc)
    stamp = _creation_stamp(creation_date)
    doc.set_metadata({
        "title": plan.title, "author": "", "subject": "", "keywords": "",
        "creator": CREATOR, "producer": "", "creationDate": stamp, "modDate": stamp,
    })
    try:
        return doc.tobytes(garbage=4, deflate=True, no_new_id=True), expected
    finally:
        doc.close()


# --- Self-check through the real ingest path --------------------------------
def _expected_owners(plan: Plan, cover: int) -> dict[int, str]:
    """Heading path that must own each content page (0-based PDF page):
    the deepest bookmark starting on or before it; pages before the first
    bookmark belong to the document title."""
    owners: dict[int, str] = {}
    stack: list[tuple[int, str]] = []
    current = plan.title
    for offset, page in enumerate(plan.pages):
        for level, title in page.bookmarks:
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current = " > ".join(t for _, t in stack)
        owners[cover + offset] = current
    return owners


def self_check(path: Path, plan: Plan, expected_pages: list[list[str]]) -> int:
    """Run ingest's own parse and chunk steps on the output; refuse unless
    identity, title, outline, page text and chunk headings are as planned.
    Returns the chunk count."""
    stem = path.stem
    parsed = parse_pdf(path)
    if parsed.doc_id != stem or resolve_doc_id(path) != stem:
        raise ConversionError("self-check: ingest would not use the file stem as the doc id")
    if parsed.title != plan.title:
        raise ConversionError("self-check: ingest reads a different title")
    cover = len(expected_pages) - len(plan.pages)
    want_toc = [
        (level, title, cover + offset + 1)
        for offset, page in enumerate(plan.pages)
        for level, title in page.bookmarks
    ]
    if list(parsed.toc) != want_toc:
        raise ConversionError("self-check: ingest reads a different outline")
    doc = pymupdf.open(path)
    try:
        texts, labels = _extract_page_texts(doc)
    finally:
        doc.close()
    stripped = strip_chrome(texts)
    for number, (got, want) in enumerate(zip(stripped, expected_pages, strict=True), 1):
        if [ln.rstrip() for ln in got.split("\n")] != [ln.rstrip() for ln in want or [""]]:
            raise ConversionError(f"self-check: ingest extracts different text on PDF page {number}")
    owners = _expected_owners(plan, cover)
    chunks = make_chunks(parsed, stripped, labels)
    for chunk in chunks:
        end = chunk.page_end if chunk.page_end is not None else chunk.page_start
        pages = range(chunk.page_start, end + 1)
        if any(owners.get(p) != chunk.heading_path for p in pages) or chunk.page_label:
            raise ConversionError(
                f"self-check: a chunk on PDF pages {chunk.page_start + 1}-{end + 1} is filed "
                "under another heading"
            )
    return len(chunks)


# --- Output -----------------------------------------------------------------
def write_pdf(
    plan: Plan,
    out_dir: Path,
    *,
    creation_date: str | None = None,
    replace: bool = False,
    corpus_root: Path | None = None,
) -> Result:
    """Render, self-check in a private temporary directory beside the output,
    then move into place. An existing different file is kept unless
    `replace`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{plan.stem}.pdf"
    notices = list(plan.notices)
    if corpus_root is not None:
        _, product, _ = infer_from_path(final, corpus_root)
        if product == "unknown":
            raise ConversionError(
                "output is not at <corpus root>/<vendor>/<product>/<version>/<name>.pdf"
            )
        if detect_vendor(final, corpus_root) != "unknown":
            notices.append(
                f"the path below the corpus root names vendor {detect_vendor(final, corpus_root)!r}; "
                "choose folder names without vendor markers"
            )
    elif detect_vendor(final, None) != "unknown":
        notices.append("the output name contains a vendor marker (ibm, bmc, ca-, ...)")
    data, expected = render(plan, creation_date)
    digest = hashlib.sha256(data).hexdigest()
    if final.exists() and not replace and hashlib.sha256(final.read_bytes()).hexdigest() != digest:
        raise ConversionError(
            f"{final.name} exists with different content; a new export takes a new name "
            "(e.g. a new date), or pass --replace"
        )
    tmp = Path(tempfile.mkdtemp(prefix=".text_to_pdf-", dir=out_dir))
    try:
        staged = tmp / final.name
        staged.write_bytes(data)
        chunks = self_check(staged, plan, expected)
        os.replace(staged, final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    bookmarks = sum(len(page.bookmarks) for page in plan.pages)
    return Result(final, digest, len(expected), bookmarks, chunks, tuple(notices))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mainframe_rag.tools.text_to_pdf",
        description="Render plain-text sources as PDFs for the existing ingest (#1277).",
    )
    sub = parser.add_subparsers(dest="kind", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out-dir", type=Path, required=True,
                        help="corpus layout folder, e.g. <corpus>/site/macros/<sysname>")
    common.add_argument("--encoding", default="utf-8", help="input encoding (default utf-8)")
    common.add_argument("--creation-date", help="fixed PDF creation date YYYY-MM-DD (default none)")
    common.add_argument("--corpus-root", type=Path,
                        help="check that --out-dir is <root>/<vendor>/<product>/<version>")
    common.add_argument("--replace", action="store_true",
                        help="overwrite an existing output with different content")
    common.add_argument("--allow-secret-like", action="store_true",
                        help="convert although lines look like literal credentials (after review)")
    lib = sub.add_parser("library", parents=[common], help="a directory of PDS members, one PDF")
    lib.add_argument("--src", type=Path, required=True, help="directory of exported member files")
    lib.add_argument("--name", required=True, help="output file stem = doc id, e.g. SITE-MACLIB-SYSA-20261117")
    lib.add_argument("--title", required=True,
                     help='PDF title, e.g. "Site export - SYS1.MACLIB macro sources (not IBM documentation)"')
    lib.add_argument("--columns", type=int, default=DEFAULT_COLUMNS, help="record length (default 80)")
    lib.add_argument("--strip-sequence", action="store_true", help="drop sequence numbers in columns 73-80")
    lib.add_argument("--shop-names", action="store_true",
                     help="bookmark file names as they are instead of PDS member names")
    md = sub.add_parser("markdown", parents=[common], help="one Markdown document, one PDF")
    md.add_argument("--src", type=Path, required=True, help="the .md file; its stem is the doc id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.kind == "library":
            plan = plan_library(
                args.src, args.name, args.title, columns=args.columns,
                strip_sequence=args.strip_sequence, shop_names=args.shop_names,
                encoding=args.encoding, allow_secret_like=args.allow_secret_like,
            )
        else:
            plan = plan_markdown(args.src, encoding=args.encoding, allow_secret_like=args.allow_secret_like)
        result = write_pdf(
            plan, args.out_dir, creation_date=args.creation_date,
            replace=args.replace, corpus_root=args.corpus_root,
        )
    except ConversionError as exc:
        print(f"text_to_pdf: refused: {exc}", file=sys.stderr)
        return 2
    for notice in result.notices:
        print(f"text_to_pdf: notice: {notice}", file=sys.stderr)
    print(json.dumps({
        "output": str(result.path), "doc_id": result.path.stem, "sha256": result.sha256,
        "pages": result.pages, "bookmarks": result.bookmarks, "chunks": result.chunks,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
