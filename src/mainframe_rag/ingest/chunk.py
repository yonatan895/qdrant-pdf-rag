"""Section outline + chunk contract.

One Qdrant point = one chunk. Point id is UUID5 of
    f"{doc_id}|{heading_path}|{page_start}|{ordinal}"
(Qdrant accepts UUID or unsigned int only; sha256 hex is invalid.)
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from mainframe_rag.ingest.classify import classify
from mainframe_rag.ingest.ibm_pdf import ParsedDoc
from mainframe_rag.regexes import (
    FRONT_MATTER_RE,
    SKIP_ALWAYS_RE,
    find_members,
    find_message_ids,
)

SECTION_MAX_CHARS = 3500
SPLIT_OVERLAP_CHARS = 400
FRONT_MATTER_FRACTION = 0.15
FRONT_MATTER_MIN_PAGES = 2

_BLANK_SPLIT_RE = re.compile(r"\n\s*\n")


# Code-region detection (issue #79): JCL cards, REXX programs, and
# monospaced console blocks must never be sliced mid-statement. Detection is
# deliberately conservative (line-anchored markers with a 0.6 dominance
# threshold); a missed region falls back to today's paragraph behavior,
# never to an error. A false positive only makes a paragraph atomic, which
# changes nothing below SECTION_MAX_CHARS.
_JCL_CARD_RE = re.compile(r"^//")
_JCL_STMT_START_RE = re.compile(r"^//\S")
_REXX_HEADER_RE = re.compile(r"/\*\s*rexx", re.IGNORECASE)


def _nonblank_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def detect_code_region(text: str) -> str | None:
    """Classify a paragraph as code for atomic splitting: "jcl", "rexx",
    "console", or None (prose path). Precedence is JCL, then REXX, then
    console: a `//*` JCL comment line also carries `/*`, so JCL must win.
    REXX needs its header or a line-unbalanced `/*` (a real block comment);
    prose merely mentioning a complete `/*...*/` pair stays prose. Console
    is sustained indentation without stronger markers."""
    lines = _nonblank_lines(text)
    if not lines:
        return None
    if sum(1 for line in lines if _JCL_CARD_RE.match(line)) / len(lines) >= 0.6:
        return "jcl"
    if _REXX_HEADER_RE.search(text) or any(
        line.count("/*") > line.count("*/") for line in lines
    ):
        return "rexx"
    if sum(1 for line in lines if line[:1].isspace()) / len(lines) >= 0.6:
        return "console"
    return None


@dataclass(frozen=True, slots=True)
class Section:
    heading_path: str
    page_start: int
    page_end: int


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    doc_id: str
    heading_path: str
    page_start: int
    page_label: str
    chunk_type: str
    text: str
    message_ids: list[str]
    members: list[str]
    ordinal: int


_WHITESPACE_RE = re.compile(r"\s+")


def make_chunk_id(doc_id: str, heading_path: str, page_start: int, ordinal: int) -> str:
    key = f"{doc_id}|{heading_path}|{page_start}|{ordinal}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _clean_title(title: str) -> str:
    return _WHITESPACE_RE.sub(" ", title).strip()


def outline_sections(parsed: ParsedDoc) -> list[Section]:
    if not parsed.toc:
        return [Section(heading_path=parsed.title, page_start=0, page_end=parsed.page_count)]

    front_matter_limit = max(
        FRONT_MATTER_MIN_PAGES, int(FRONT_MATTER_FRACTION * parsed.page_count)
    )
    entries = sorted(parsed.toc, key=lambda e: (e[2], e[0]))

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []

    for idx, (level, raw_title, page_1based) in enumerate(entries):
        title = _clean_title(raw_title)
        if not title or SKIP_ALWAYS_RE.search(title):
            continue
        if FRONT_MATTER_RE.search(title) and page_1based <= front_matter_limit:
            continue

        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        heading_path = " > ".join(t for _, t in stack)

        start = max(0, page_1based - 1)
        end = parsed.page_count
        for nxt_level, _, nxt_page in entries[idx + 1 :]:
            if nxt_level <= level:
                end = max(start, nxt_page - 1)
                break

        if end > start:
            sections.append(
                Section(heading_path=heading_path, page_start=start, page_end=end)
            )

    return sections


def _split_jcl_statements(text: str) -> list[str]:
    """Group JCL cards into statements: a `^//\\S` line (name, `//*`
    comment) starts one; a `^//\\s` line continues the open statement
    (column-72 continuation semantics without depending on exact PDF
    column fidelity). Non-card lines attach to the open statement so no
    text is ever dropped."""
    statements: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if _JCL_STMT_START_RE.match(line) or not current:
            if current:
                statements.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        statements.append("\n".join(current))
    return [s for s in (stmt.rstrip() for stmt in statements) if s.strip()]


def _split_rexx_statements(text: str) -> list[str]:
    """Split a REXX region on `;` and line ends, but never inside `/* */`
    comments, string literals (with `''` escape handling), or before a `,`
    line-continuation. Comment/string state tracks across lines; an
    unterminated comment or string swallows to the end — fail-safe toward
    fewer, larger statements, never a split inside an ambiguous construct."""
    statements: list[str] = []
    cur: list[str] = []
    in_comment = False
    quote: str | None = None
    for line in text.splitlines():
        i, n = 0, len(line)
        while i < n:
            two = line[i : i + 2]
            if in_comment:
                if two == "*/":
                    cur.append("*/")
                    i += 2
                    in_comment = False
                else:
                    cur.append(line[i])
                    i += 1
            elif quote is not None:
                cur.append(line[i])
                i += 1
                if line[i - 1] == quote:
                    if line[i : i + 1] == quote:
                        cur.append(quote)
                        i += 1
                    else:
                        quote = None
            elif two == "/*":
                cur.append("/*")
                i += 2
                in_comment = True
            elif line[i] in "\"'":
                quote = line[i]
                cur.append(line[i])
                i += 1
            elif line[i] == ";":
                cur.append(";")
                i += 1
                statements.append("".join(cur))
                cur = []
            else:
                cur.append(line[i])
                i += 1
        if not in_comment and quote is None:
            joined = "".join(cur)
            if joined.strip().endswith(","):
                cur.append("\n")
            elif joined.strip():
                statements.append(joined)
                cur = []
        else:
            cur.append("\n")
    tail = "".join(cur)
    if tail.strip():
        statements.append(tail)
    return [s.rstrip() for s in statements if s.strip()]


def _code_statements(text: str) -> list[str] | None:
    """Statement units for a code region, or None for the prose path.
    Console blocks split between lines; JCL/REXX split at statement
    boundaries. Empty results fall back to prose (never an empty expansion).
    """
    kind = detect_code_region(text)
    if kind is None:
        return None
    if kind == "jcl":
        statements = _split_jcl_statements(text)
    elif kind == "rexx":
        statements = _split_rexx_statements(text)
    else:
        statements = [line for line in text.splitlines() if line.strip()]
    return statements or None


def _overlap_seed(
    items: list[tuple[int, str, bool]], joined: str
) -> list[tuple[int, str, bool]]:
    """Overlap seed for the next accumulation. Identical to today's blind
    SPLIT_OVERLAP_CHARS tail unless that tail would cut inside an atomic
    (code) statement — then back off to whole trailing items, possibly
    empty. Non-code blocks always take the blind tail, byte for byte."""
    tail_start = len(joined) - SPLIT_OVERLAP_CHARS
    if tail_start <= 0:
        return [(items[-1][0], joined, False)]
    offset = 0
    for idx, (page_idx, text, atomic) in enumerate(items):
        if offset <= tail_start < offset + len(text) and atomic and tail_start > offset:
            return [(p, t, a) for (p, t, a) in items[idx + 1 :]]
        offset += len(text) + 2
    return [(items[-1][0], joined[tail_start:], False)]


def _split_blocks(paras: list[tuple[int, str]]) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    # Expand code paragraphs into atomic statement items; prose passes
    # through untouched. Item shape: (page_idx, text, atomic).
    items: list[tuple[int, str, bool]] = []
    for page_idx, para in paras:
        statements = _code_statements(para)
        if statements:
            items.extend((page_idx, statement, True) for statement in statements)
        else:
            items.append((page_idx, para, False))
    current: list[tuple[int, str, bool]] = []
    current_len = 0

    for page_idx, text, atomic in items:
        if len(text) > SECTION_MAX_CHARS:
            if current:
                joined = "\n\n".join(t for _, t, _ in current)
                blocks.append((current[0][0], joined))
                current, current_len = [], 0
            if atomic:
                # A single statement longer than the section cap is emitted
                # whole: slicing it would be exactly the bug this module
                # fixes, and the 4096-token embed window still covers roughly
                # twice the cap. The overlap chain restarts after it rather
                # than seeding from a sliced statement.
                blocks.append((page_idx, text))
            else:
                for i in range(0, len(text), SECTION_MAX_CHARS):
                    blocks.append((page_idx, text[i : i + SECTION_MAX_CHARS]))
            continue
        if current_len + len(text) > SECTION_MAX_CHARS and current:
            joined = "\n\n".join(t for _, t, _ in current)
            blocks.append((current[0][0], joined))
            seed = _overlap_seed(current, joined)
            if len(seed) == 1 and not seed[0][2]:
                # Historical blind-tail seed: exact legacy accounting.
                current = [seed[0], (page_idx, text, atomic)]
                current_len = len(seed[0][1]) + len(text) + 2
            else:
                # Code backoff seed (possibly empty): quirk-consistent.
                current = [*seed, (page_idx, text, atomic)]
                current_len = sum(len(t) for _, t, _ in current) + 2 * len(current)
        else:
            current.append((page_idx, text, atomic))
            current_len += len(text) + 2

    if current:
        blocks.append((current[0][0], "\n\n".join(t for _, t, _ in current)))
    return blocks


def _page_label_range(labels: list[str | None]) -> str:
    present = [lbl for lbl in labels if lbl]
    if not present:
        return ""
    if len(present) == 1:
        return present[0]
    first, last = present[0], present[-1]
    return first if first == last else f"{first}\u2013{last}"


def make_chunks(
    parsed: ParsedDoc, page_texts: list[str], page_labels: list[str | None] | None = None
) -> list[Chunk]:
    doc_id = parsed.doc_id or parsed.path.stem
    labels = page_labels or [None] * parsed.page_count
    chunks: list[Chunk] = []

    for section in outline_sections(parsed):
        body_pages = page_texts[section.page_start : section.page_end]

        paras: list[tuple[int, str]] = []
        for offset, page_text in enumerate(body_pages):
            for para in _BLANK_SPLIT_RE.split(page_text):
                if para.strip():
                    paras.append((section.page_start + offset, para.strip()))
        if not paras:
            continue

        for ordinal, (page_idx, text) in enumerate(_split_blocks(paras)):
            chunk_id = make_chunk_id(doc_id, section.heading_path, page_idx, ordinal)
            label = _page_label_range([labels[page_idx]]) if page_idx < len(labels) else ""
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    heading_path=section.heading_path,
                    page_start=page_idx,
                    page_label=label,
                    chunk_type=classify(text),
                    text=text,
                    message_ids=find_message_ids(text),
                    members=find_members(text),
                    ordinal=ordinal,
                )
            )

    return chunks
