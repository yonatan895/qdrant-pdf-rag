"""Section outline + chunk contract.

One Qdrant point = one chunk. Point id is UUID5 of
    f"{doc_id}|{heading_path}|{page_start}|{ordinal}"
(Qdrant accepts UUID or unsigned int only; sha256 hex is invalid.)
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING

from mainframe_rag.ingest.classify import classify, is_table_block
from mainframe_rag.ingest.identity import source_rev_key
from mainframe_rag.regexes import (
    FRONT_MATTER_RE,
    SKIP_ALWAYS_RE,
    find_members,
    find_message_ids,
)

if TYPE_CHECKING:
    # Annotation-only: ibm_pdf pulls in pymupdf, which the serving agent
    # process must not import transitively (answer.py reuses the unit-span
    # helpers below for prompt packing).
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

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
#
# JCL matching runs on lstripped lines: page.get_text() keeps the left pad
# IBM manuals put on examples, so column-0 anchoring would miss real cards.
# A card starting `//` + non-space opens a statement (`//name`, `//*`
# comment); `//` + 2+ spaces continues one (operand column — col-72
# continuation semantics without depending on exact PDF column fidelity);
# `//` + exactly one space is an unnamed op (`// EXEC`, `// DD`), which is
# its own statement, not a continuation.
_JCL_CARD_RE = re.compile(r"^//")
_JCL_STMT_START_RE = re.compile(r"^//\S")
_JCL_UNNAMED_RE = re.compile(r"^//\s\S")
# In-stream data marker: `//name DD *` or `DD DATA` (optionally followed by
# `,DLM=..`). Everything after it (until the next card) is data records.
_JCL_DD_DATA_RE = re.compile(r"^//\S+\s+DD\s+(\*|DATA)(?=[\s,]|$)", re.IGNORECASE)
_REXX_HEADER_RE = re.compile(r"/\*\s*rexx", re.IGNORECASE)
# REXX keyword fallback (issue #216): balanced-comment samples with no
# header carry no unbalanced `/*`, so sample line-initial keywords instead.
# Line-anchored and requiring a code signal (assignment or `;`) beside it:
# manual prose opens lines with "Do not"/"If" too, but without assignments
# it stays prose. A miss still falls back to paragraph behavior.
_REXX_KEYWORD_RE = re.compile(
    r"^\s*(say|do|end|parse|pull|push|queue|exit|return|address|trace|signal"
    r"|call|select|when|otherwise|nop|drop|interpret)\b",
    re.IGNORECASE,
)
_REXX_ASSIGN_RE = re.compile(r"^\s*[A-Za-z_][\w.]*\s*=[^=]")
# A SYSIN-adjacency chain breaks at sentence punctuation (issue #216):
# data records do not end lines with `.`/`?`/`!`/`:`; prose explanations
# do. A missed break only makes prose line-atomic (text preserved); a
# false break restores today's char-slice (status quo, never worse).
_SENTENCE_END_RE = re.compile(r"[.?!:]\s*$")
# Minimum JCL statement-starts before a paragraph is treated as mixed
# prose+JCL (issue #216): one `//see`-style prose line must not flip a
# paragraph; two cards are an example, not a mention.
_MIXED_JCL_MIN_STARTS = 2

# Code-entry detection (issue #591): a line that is exactly a completion code,
# followed within two lines by description text rather than another bare code.
# A user completion code keeps its U prefix on its own line (U4038); system and
# wait-state entries are both bare 3-hex, and which one a bare entry is comes
# from the section heading, not from the line (see _canonical_code).
_CODE_ENTRY_RE = re.compile(r"^(?:[0-9A-F]{3}|U\d{4})$")
# A section whose heading names wait states makes its bare 3-hex entries wait
# states. The heading is plain English any code manual uses, so this stays a
# generic-parsing signal rather than a vendor gate; a manual that never says
# "wait state" simply yields completion codes only.
_WAITSTATE_HEADING_RE = re.compile(r"\bwait\s+states?\b", re.IGNORECASE)
# A code entry's own description label (issue #621). A code line directly
# followed by it is a confirmed entry; a section with none is not a code
# section, so its code-shaped lines (index pages, table cells, module tables,
# return codes) record no system_codes. Plain English, so a manual without the
# label simply stores no codes: optional payload, never an ingest gate.
_ENTRY_LABEL_RE = re.compile(r"^Explanation\s*(?::|$)", re.IGNORECASE)


def _canonical_code(raw: str, wait_state: bool) -> str:
    """Canonical stored form of one entry-start code (issue #591).

    User codes keep the U (`U4038`). A bare 3-hex entry is a completion code
    (`0C4`) unless its section heading names wait states, in which case the
    canonical form is the W-prefix the query parser emits for it (`W064`) —
    the two sides must agree or the filter can never match.
    """
    upper = raw.upper()
    if upper.startswith("U"):
        return upper
    return f"W{upper}" if wait_state else upper


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
    stripped = [line.lstrip() for line in lines]
    if sum(1 for line in stripped if _JCL_CARD_RE.match(line)) / len(lines) >= 0.6:
        return "jcl"
    # A DD-instream card makes the whole paragraph JCL even when data
    # records dominate the line count: without this, a 200-line SYSIN
    # block reads as prose and an oversize paragraph would char-slice
    # mid-record. Prose merely mentioning such a card misdetects, but the
    # only consequence is line-wise (never mid-word) overflow splits.
    if any(_JCL_DD_DATA_RE.match(line) for line in stripped):
        return "jcl"
    if _REXX_HEADER_RE.search(text) or any(
        line.count("/*") > line.count("*/") for line in lines
    ):
        return "rexx"
    keyword_lines = sum(1 for line in lines if _REXX_KEYWORD_RE.match(line))
    if keyword_lines >= 2 and (
        any(_REXX_ASSIGN_RE.match(line) for line in lines) or ";" in text
    ):
        return "rexx"
    if sum(1 for line in lines if line[:1].isspace()) / len(lines) >= 0.6:
        return "console"
    return None


def _is_dd_data_para(text: str) -> bool:
    """True when the paragraph opens a SYSIN in-stream block (a DD */DATA
    card), making following data paragraphs adjacency candidates."""
    return any(_JCL_DD_DATA_RE.match(line.lstrip()) for line in text.splitlines())


def detect_table_region(text: str) -> bool:
    """Column-block test for atomic row splitting (issue #216): a table
    block only when it is NOT code (JCL continuations carry wide indents
    that read as columns) and the shared classify helper agrees. One rule
    per concept: the 0.6 column heuristic lives in classify.is_table_block."""
    if detect_code_region(text) is not None:
        return False
    return is_table_block(text)


def _is_code_entry_start(lines: list[str], idx: int) -> bool:
    """Check if lines[idx] is a code entry start (issue #591).

    A line that is exactly a 3-hex code, followed within two lines by
    description text rather than another bare code. This excludes index runs.
    """
    if idx >= len(lines):
        return False
    if not _CODE_ENTRY_RE.match(lines[idx].strip()):
        return False
    for j in range(idx + 1, min(idx + 3, len(lines))):
        line = lines[j].strip()
        if not line:
            continue
        return not _CODE_ENTRY_RE.match(line)
    return False


def _code_entries(text: str) -> list[tuple[str, bool]] | None:
    """Split a paragraph into code entries (issue #591).

    Returns None if the paragraph doesn't contain code entries. Each code
    entry is atomic; text before the first entry is prose.
    """
    lines = text.splitlines()
    starts = []
    for i, line in enumerate(lines):
        if _CODE_ENTRY_RE.match(line.strip()) and _is_code_entry_start(lines, i):
            starts.append(i)
    if not starts:
        return None
    items: list[tuple[str, bool]] = []
    if starts[0] > 0:
        prefix = "\n".join(lines[: starts[0]]).strip()
        if prefix:
            items.append((prefix, False))
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(lines)
        entry = "\n".join(lines[start:end]).strip()
        if entry:
            items.extend((piece, True) for piece in _cap_entry(entry))
    return items or None


def _cap_entry(entry: str) -> list[str]:
    """Cut a code entry longer than SECTION_MAX_CHARS at line boundaries.

    A code entry is an explanation, not a statement: unlike a JCL card it
    has no internal syntax a line cut would break, and nothing else bounds
    it. Emitted whole, a dump-listing "entry" reached 5,813 chars / 4,081
    tokens and overflowed the 4096-token embed window. The first piece
    keeps the code line with the lines after it, so the entry is still
    detected there; a single line over the cap stays whole.
    """
    if len(entry) <= SECTION_MAX_CHARS:
        return [entry]
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in entry.splitlines():
        if current and size + 1 + len(line) > SECTION_MAX_CHARS:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + (1 if size else 0)
    if current:
        pieces.append("\n".join(current))
    return [piece for piece in (p.strip() for p in pieces) if piece]


def _extract_system_codes(text: str, wait_state: bool = False) -> list[str]:
    """Every code-entry start in a finished chunk (issue #591).

    A block can hold several entries, so this scans the whole chunk rather
    than reading only the first line: reading line 0 recorded 16% of the
    codes in a real system-codes manual, including missing the issue's own
    0C4 whenever its chunk opened with a neighbouring entry. Detection is
    the same rule the splitter used, so the payload lists exactly the
    entries this chunk contains.
    """
    lines = text.splitlines()
    codes: list[str] = []
    for i, line in enumerate(lines):
        if _CODE_ENTRY_RE.match(line.strip()) and _is_code_entry_start(lines, i):
            code = _canonical_code(line.strip(), wait_state)
            if code not in codes:
                codes.append(code)
    return codes


def _is_code_section(paras: list[tuple[int, str]]) -> bool:
    """True when the section holds a labelled code entry (issue #621).

    Gates `system_codes` per section rather than per entry: a sub-entry
    such as `0C4` inside the `0Cx` entry carries a description but no label
    of its own, and is still a real code. Lines are read across paragraph
    and page breaks so a label that opens the next page still confirms the
    code that closed the previous one.
    """
    lines = [line.strip() for _, para in paras for line in para.splitlines() if line.strip()]
    return any(
        _CODE_ENTRY_RE.match(line) and _ENTRY_LABEL_RE.match(nxt) for line, nxt in pairwise(lines)
    )


@dataclass(frozen=True, slots=True)
class Section:
    heading_path: str
    page_start: int
    page_end: int
    # Sub-page bounds for bookmarks that share a start page: char offsets
    # into page_texts[page_start]. end_char is set only on a section that
    # ends on its own start page (page_end == page_start + 1).
    start_char: int = 0
    end_char: int | None = None


# Unit-span kinds (issue #368): "atomic" spans (code statements, table
# rows, SYSIN records) snap whole-or-omitted at prompt packing; "prose"
# spans keep the legacy character cut. Two values only — finer taxonomy is
# extraction work (#271), not a prompt-packing concern.
UNIT_ATOMIC = "atomic"
UNIT_PROSE = "prose"
_UNIT_KINDS = frozenset({UNIT_ATOMIC, UNIT_PROSE})

# Payload-size bound on persisted spans: a pathological multi-thousand-line
# block stays servable by falling back to pack-time redetection (the same
# detectors) instead of shipping megabytes of spans per point.
_MAX_STORED_SPANS = 512


@dataclass(frozen=True, slots=True)
class UnitSpan:
    """One atomic-or-prose unit range over stripped chunk text: [start, end)
    char offsets plus the cut rule. Ordered and non-overlapping per chunk."""

    start: int
    end: int
    kind: str


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
    # System/user/wait-state completion codes (issue #591). Empty for
    # non-code chunks. Not part of identity.
    system_codes: list[str] = field(default_factory=list)
    # Atomic-unit spans for prompt packing (issue #368): ordered ranges
    # over the stripped chunk text. Spans always tile whole items, so every
    # retained prefix is a whole number of units and a table chunk's header
    # (its first unit) ships with any retained row. None means unknown
    # (span list capped, see _MAX_STORED_SPANS) — pack with the shared
    # fallback detector, never char slicing. () means known prose: legacy
    # character truncation still applies. Identity and the four-type
    # chunk_type vocabulary are untouched by this field.
    units: tuple[UnitSpan, ...] | None = None
    # Last physical PDF page the chunk touches, 0-based and inclusive like
    # page_start (issue #271). None only for chunks built outside
    # make_chunks; readers treat it as page_start. Not part of identity.
    page_end: int | None = None


_WHITESPACE_RE = re.compile(r"\s+")


def make_chunk_id(source_rev: str, heading_path: str, page_start: int, ordinal: int) -> str:
    """Point-id contract: UUID5 of the chunk key. The key's first segment is
    the source revision (issue #361), not the printed doc_id — two revisions
    sharing a form number must mint distinct point ids, or the second
    writer's upsert silently collides with the first. The UUID5-of-key
    scheme itself never changes (only the key content did, once, in the
    361B migration). Chunk.doc_id (payload, citations, filters) is
    unaffected."""
    key = f"{source_rev}|{heading_path}|{page_start}|{ordinal}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _clean_title(title: str) -> str:
    return _WHITESPACE_RE.sub(" ", title).strip()


def _title_key(title: str) -> str:
    return _WHITESPACE_RE.sub(" ", title).strip().casefold()


def _title_line_offset(text: str, title: str, start: int) -> int | None:
    """Offset of the first line at or after `start` that opens with the
    bookmark title (whitespace-collapsed, case-insensitive, whole words)."""
    want = _title_key(title)
    offset = 0
    for line in text.splitlines(keepends=True):
        if offset >= start:
            have = _title_key(line)
            if have and (have == want or have.startswith(want + " ")):
                return offset
        offset += len(line)
    return None


def _shared_page_pieces(
    group: list[tuple[int, str, str]], page_text: str, unique_titles: frozenset[str]
) -> list[tuple[str, int]] | None:
    """Split one page among the kept bookmarks that start on it.

    Only a bookmark whose title occurs once in the outline opens a piece:
    a message, command or macro name is an addressable entry, while a
    recurring title ("Restrictions", "Syntax", "Examples") is a generic
    subsection label whose text stays with the entry before it. Splitting
    on those produced hundreds of tiny, byte-identical, context-free
    chunks per reference manual.

    `group` holds (level, title, heading_path) in outline order. Returns
    (heading_path, start_char) per piece, or None to keep the page whole
    under the last bookmark (the pre-split behavior). Each later bookmark
    must be found as its own title line, in order, or nothing is split.
    A piece folds into the next one when the next bookmark is deeper (a
    same-page parent's intro stays with its first child, issue #577) or
    when it holds nothing but its own title line. Nothing is split when two
    pieces would share a heading path (the chunk key would collide).
    """
    group = [group[0], *(e for e in group[1:] if _title_key(e[1]) in unique_titles)]
    starts = [0]
    for _, title, _ in group[1:]:
        found = _title_line_offset(page_text, title, starts[-1] + (len(starts) > 1))
        if found is None:
            return None
        starts.append(found)
    pieces: list[tuple[str, int]] = []
    for idx, (level, title, path) in enumerate(group):
        start = pieces.pop()[1] if pieces and pieces[-1][0] == "" else starts[idx]
        if idx + 1 < len(group):
            body = page_text[starts[idx] : starts[idx + 1]]
            rest = [ln for ln in body.splitlines() if ln.strip() and _title_key(ln) != _title_key(title)]
            if group[idx + 1][0] > level or not rest:
                pieces.append(("", start))
                continue
        if any(p == path for p, _ in pieces):
            return None
        pieces.append((path, start))
    return pieces


def outline_sections(parsed: ParsedDoc, page_texts: list[str] | None = None) -> list[Section]:
    if not parsed.toc:
        return [Section(heading_path=parsed.title, page_start=0, page_end=parsed.page_count)]

    front_matter_limit = max(
        FRONT_MATTER_MIN_PAGES, int(FRONT_MATTER_FRACTION * parsed.page_count)
    )
    entries = sorted(parsed.toc, key=lambda e: (e[2], e[0]))

    # Issue #577: skipped headings (empty, SKIP_ALWAYS, front matter) produce
    # no section, so they must not bound a kept section either — otherwise a
    # skipped child cuts off its parent and those pages are never chunked.
    # Filter once with the same rules as the main loop, then bound each kept
    # section at the next kept entry of any level.
    kept: list[tuple[int, str, int]] = []
    for level, raw_title, page_1based in entries:
        title = _clean_title(raw_title)
        if not title or SKIP_ALWAYS_RE.search(title):
            continue
        if FRONT_MATTER_RE.search(title) and page_1based <= front_matter_limit:
            continue
        kept.append((level, title, page_1based))

    title_counts = Counter(_title_key(title) for _, title, _ in kept)
    unique_titles = frozenset(key for key, n in title_counts.items() if n == 1)
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    # Kept bookmarks starting on the current page, in outline order. Before
    # sub-page splitting, every one but the last had an empty page range
    # and was dropped, so a page of several messages was filed under the
    # last message's heading. With the page text available, the page is
    # split at each later bookmark's own title line instead.
    group: list[tuple[int, str, str]] = []

    for idx, (level, title, page_1based) in enumerate(kept):

        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        heading_path = " > ".join(t for _, t in stack)
        group.append((level, title, heading_path))

        start = max(0, page_1based - 1)
        end = parsed.page_count
        for _, _, nxt_page in kept[idx + 1 :]:
            end = max(start, nxt_page - 1)
            break

        if end > start:
            pieces = None
            if len(group) > 1 and page_texts is not None and start < len(page_texts):
                pieces = _shared_page_pieces(group, page_texts[start], unique_titles)
            if pieces and len(pieces) > 1:
                for (path, first), (_, nxt) in pairwise(pieces):
                    sections.append(Section(path, start, start + 1, first, nxt))
                path, first = pieces[-1]
                sections.append(Section(path, start, end, first))
            else:
                sections.append(
                    Section(heading_path=heading_path, page_start=start, page_end=end)
                )
            group = []

    return sections


def _split_jcl_statements(text: str) -> list[str]:
    """Group JCL cards into statements on lstripped lines (see the column
    note above): `^//\\S` and one-space unnamed ops (`// EXEC`) open one;
    `^//\\s{2,}` continues the open statement. A bare `//` line whose next
    line carries a parameter (`=`, not blank, not `/`-starting) is an
    extraction-wrapped card (`//` + newline + `ASMBLR=...`, seen in real IBM
    manuals) and rejoins into the true card; without the `=` guard a null
    statement followed by prose or SYSIN data would glue into a phantom
    card, so those stay split. Non-card lines (in-stream SYSIN data, the
    `/*` delimiter) become single-line units of their own: gluing a 20k
    SYSIN block onto its `DD *` card would make one giant atom that no
    window can hold — data lines split between lines, only `//` cards stay
    continuation-atomic."""
    statements: list[str] = []
    current: list[str] = []
    lines = text.splitlines()
    joined: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        nxt_stripped = nxt.lstrip()
        if (
            line.strip() == "//"
            and "=" in nxt
            and nxt.strip()
            and not nxt_stripped.startswith("/")
        ):
            joined.append("//" + nxt_stripped)
            i += 2
        else:
            joined.append(line)
            i += 1
    for line in joined:
        nospace = line.lstrip()
        if _JCL_STMT_START_RE.match(nospace) or _JCL_UNNAMED_RE.match(nospace):
            if current:
                statements.append("\n".join(current))
            current = [line]
        elif _JCL_CARD_RE.match(nospace):
            if not current:
                current = [line]
            else:
                current.append(line)
        else:
            if current:
                statements.append("\n".join(current))
                current = []
            if line.strip():
                statements.append(line)
    if current:
        statements.append("\n".join(current))
    return [s for s in (stmt.rstrip() for stmt in statements) if s.strip()]


def _split_rexx_statements(text: str) -> list[str]:
    """Split a REXX region on `;` and line ends, but never inside `/* */`
    comments (which nest per TSO/E), string literals (with `''` escape
    handling), or before a `,` line-continuation. Comment/string state
    tracks across lines; an unterminated comment or string swallows to the
    end — fail-safe toward fewer, larger statements, never a split inside
    an ambiguous construct."""
    statements: list[str] = []
    cur: list[str] = []
    in_comment = 0
    quote: str | None = None
    for line in text.splitlines():
        i, n = 0, len(line)
        while i < n:
            two = line[i : i + 2]
            if in_comment:
                if two == "*/":
                    cur.append("*/")
                    i += 2
                    in_comment -= 1
                elif two == "/*":
                    # TSO/E nests block comments: only the matching close
                    # exits, so a `;` inside the outer comment never splits.
                    cur.append("/*")
                    i += 2
                    in_comment += 1
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
                in_comment = 1
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


def _mixed_jcl_items(para: str) -> list[tuple[str, bool]]:
    """Split a mixed prose+JCL paragraph (issue #216): runs of `//` cards
    expand to atomic JCL statements, prose runs stay single blobs with
    original newlines. Fewer than _MIXED_JCL_MIN_STARTS statement-starts
    passes the paragraph through untouched (byte-identical prose path)."""
    lstripped = [line.lstrip() for line in para.splitlines()]
    starts = sum(
        1
        for line in lstripped
        if _JCL_STMT_START_RE.match(line) or _JCL_UNNAMED_RE.match(line)
    )
    if starts < _MIXED_JCL_MIN_STARTS:
        return [(para, False)]
    items: list[tuple[str, bool]] = []
    run: list[str] = []
    prose: list[str] = []

    def flush_prose() -> None:
        if prose:
            items.append(("\n".join(prose), False))
            prose.clear()

    for line in para.splitlines():
        if _JCL_CARD_RE.match(line.lstrip()):
            flush_prose()
            run.append(line)
        else:
            if run:
                items.extend((s, True) for s in _split_jcl_statements("\n".join(run)))
                run.clear()
            prose.append(line)
    if run:
        items.extend((s, True) for s in _split_jcl_statements("\n".join(run)))
    flush_prose()
    return [(t, a) for (t, a) in items if t.strip()] or [(para, False)]


def _block_span(items: list[tuple[int, str, bool]]) -> tuple[int, int]:
    """Min/max page over the items composing one block (issue #216: chunk
    labels span every page a block touches, not just its first page)."""
    pages = [p for (p, _, _) in items]
    return (min(pages), max(pages))


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


def _join_items(items: list[tuple[int, str, bool]]) -> tuple[str, list[int]]:
    """Join accumulated items: adjacent atomic (code-statement) items share
    a single newline so listings keep their shape and exact-card sparse
    matches are not diluted by blank lines; every other boundary keeps the
    historical double newline. Returns the text plus each item's start
    offset (for the overlap walk). Prose-only currents join exactly as
    before, byte for byte."""
    parts: list[str] = []
    offsets: list[int] = []
    pos = 0
    for idx, (_, text, atomic) in enumerate(items):
        if idx:
            sep = "\n" if (atomic and items[idx - 1][2]) else "\n\n"
            parts.append(sep)
            pos += len(sep)
        offsets.append(pos)
        parts.append(text)
        pos += len(text)
    return "".join(parts), offsets


def _overlap_seed(
    items: list[tuple[int, str, bool]], joined: str, offsets: list[int]
) -> list[tuple[int, str, bool]]:
    """Overlap seed for the next accumulation. Identical to today's blind
    SPLIT_OVERLAP_CHARS tail unless that tail would cut inside an atomic
    (code) statement — then back off to whole trailing items, possibly
    empty. Non-code blocks always take the blind tail, byte for byte."""
    tail_start = len(joined) - SPLIT_OVERLAP_CHARS
    if tail_start <= 0:
        return [(items[-1][0], joined, False)]
    for idx, (page_idx, text, atomic) in enumerate(items):
        if offsets[idx] <= tail_start < offsets[idx] + len(text) and atomic and tail_start > offsets[idx]:
            return [(p, t, a) for (p, t, a) in items[idx + 1 :]]
    return [(items[-1][0], joined[tail_start:], False)]


def _item_spans(
    items: list[tuple[int, str, bool]], offsets: list[int]
) -> tuple[UnitSpan, ...]:
    """Unit spans for one joined block (issue #368): each item's range from
    the join offsets, kinded by the item's atomic flag. Spans tile whole
    items, so any prefix cut at a span end keeps whole units only."""
    return tuple(
        UnitSpan(
            start=offsets[idx],
            end=offsets[idx] + len(text),
            kind=UNIT_ATOMIC if atomic else UNIT_PROSE,
        )
        for idx, (_, text, atomic) in enumerate(items)
    )


def _locate_parts(
    stripped: str, parts: list[tuple[str, str]]
) -> tuple[UnitSpan, ...] | None:
    """Locate ordered (part, kind) substrings as spans over `stripped`.
    None when a part is not found in order (defensive: the caller falls
    back to legacy char treatment rather than inventing boundaries)."""
    spans: list[UnitSpan] = []
    cursor = 0
    for part, kind in parts:
        pos = stripped.find(part, cursor)
        if pos < 0:
            return None
        spans.append(UnitSpan(start=pos, end=pos + len(part), kind=kind))
        cursor = pos + len(part)
    return tuple(spans)


def units_for_text(text: str) -> tuple[UnitSpan, ...]:
    """Pack-time fallback detector for legacy points without persisted spans
    (issue #368): the SAME splitters the chunker uses, run on the stripped
    hit text, so no second divergent parser exists. () means prose (legacy
    char rules) or an unlocatable layout (defensive legacy, never invented
    boundaries). Persisted spans always win when present."""
    stripped = text.strip()
    if not stripped:
        return ()
    statements = _code_statements(stripped)
    if statements:
        return _locate_parts(stripped, [(s, UNIT_ATOMIC) for s in statements]) or ()
    if detect_table_region(stripped):
        rows = _nonblank_lines(stripped)
        return _locate_parts(stripped, [(r, UNIT_ATOMIC) for r in rows]) or ()
    items = _mixed_jcl_items(stripped)
    if len(items) > 1 or items[0][1]:
        located = _locate_parts(
            stripped, [(t, UNIT_ATOMIC if a else UNIT_PROSE) for (t, a) in items]
        )
        return located or ()
    return ()


def _build_blocks(
    paras: list[tuple[int, str]],
    code_section: bool = True,
) -> list[tuple[int, int, str, tuple[UnitSpan, ...]]]:
    """Split paragraphs into capped blocks with page spans AND unit spans.

    Returns (page_start, page_end, text, spans): the page span covers every
    page the block's items touch (issue #216); spans tile the block's items
    in join order (issue #368). Oversize prose slices carry () — their
    interior was char-cut at ingest, so no whole-unit claim is possible.
    Code entries split only in a code section (see _is_code_section): bare
    numbers in a dump listing or a table are not entries.
    """
    blocks: list[tuple[int, int, str, tuple[UnitSpan, ...]]] = []
    # Expand structured paragraphs into atomic items; prose passes through
    # untouched. Item shape: (page_idx, text, atomic).
    items: list[tuple[int, str, bool]] = []
    dd_data_open = False
    for page_idx, para in paras:
        statements = _code_statements(para)
        if statements:
            items.extend((page_idx, statement, True) for statement in statements)
            dd_data_open = _is_dd_data_para(para)
        elif code_section and (code_items := _code_entries(para)) is not None:
            items.extend((page_idx, text, atomic) for text, atomic in code_items)
            dd_data_open = False
        elif detect_table_region(para):
            # Table rows are atomic like code statements: overflow splits
            # at row boundaries and the overlap backs off to whole rows.
            # One line is one row (wrapped-row detection is unreliable);
            # separator/dash lines stay glued by accumulation.
            items.extend((page_idx, line, True) for line in _nonblank_lines(para))
            dd_data_open = False
        elif dd_data_open and _nonblank_lines(para):
            data_lines = _nonblank_lines(para)
            if any(_SENTENCE_END_RE.search(line) for line in data_lines):
                # Prose explanation after the SYSIN block: the adjacency
                # chain ends here (status-quo prose path for this para).
                items.append((page_idx, para, False))
                dd_data_open = False
            else:
                # SYSIN data adjacency (issue #216): records following a
                # DD */DATA card split between lines, never mid-record —
                # even across page/paragraph boundaries.
                items.extend((page_idx, line, True) for line in data_lines)
        else:
            items.extend((page_idx, t, a) for (t, a) in _mixed_jcl_items(para))
            dd_data_open = False
    current: list[tuple[int, str, bool]] = []
    current_len = 0

    for page_idx, text, atomic in items:
        if len(text) > SECTION_MAX_CHARS:
            if current:
                joined, offsets = _join_items(current)
                span = _block_span(current)
                blocks.append((span[0], span[1], joined, _item_spans(current, offsets)))
                current, current_len = [], 0
            if atomic:
                # A single statement longer than the section cap is emitted
                # whole: slicing it would be exactly the bug this module
                # fixes, and the 4096-token embed window still covers roughly
                # twice the cap. The overlap chain restarts after it rather
                # than seeding from a sliced statement.
                blocks.append((page_idx, page_idx, text, (UnitSpan(0, len(text), UNIT_ATOMIC),)))
            else:
                for i in range(0, len(text), SECTION_MAX_CHARS):
                    blocks.append((page_idx, page_idx, text[i : i + SECTION_MAX_CHARS], ()))
            continue
        if current_len + len(text) > SECTION_MAX_CHARS and current:
            joined, offsets = _join_items(current)
            span = _block_span(current)
            blocks.append((span[0], span[1], joined, _item_spans(current, offsets)))
            seed = _overlap_seed(current, joined, offsets)
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
        joined, offsets = _join_items(current)
        span = _block_span(current)
        blocks.append((span[0], span[1], joined, _item_spans(current, offsets)))
    return blocks


def _split_blocks(paras: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """Split paragraphs into capped (page_start, page_end, text) blocks.
    Compatibility projection of _build_blocks: unit spans ride the rich
    internal only, so existing block-shape assertions are untouched."""
    return [(start, end, text) for (start, end, text, _) in _build_blocks(paras)]


# No-TOC fallback sectioning (issue #216): books without bookmarks no
# longer collapse to one giant whole-document section. A new section opens
# at a heading-like page lead (numbered headings, Chapter/Appendix/Section
# leads) or when the current run reaches FALLBACK_MAX_PAGES — whichever
# comes first, so over-eager heading matches cannot strand giant runs and
# missing headings cannot collapse the book. Deterministic in the input:
# same pages in, same sections (and ordinals) out.
_FALLBACK_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)+\s+\S|(?:chapter|appendix|section)\b)", re.IGNORECASE
)
FALLBACK_MAX_PAGES = 10
_FALLBACK_HEADING_CHARS = 100


def fallback_sections(page_texts: list[str], title: str) -> list[Section]:
    """Windowed sections for documents without a table of contents."""
    sections: list[Section] = []
    start = 0
    heading_path = title
    for idx, page_text in enumerate(page_texts):
        if idx > start:
            lead = ""
            for line in page_text.splitlines():
                if line.strip():
                    lead = line.strip()
                    break
            new_heading = bool(lead and _FALLBACK_HEADING_RE.match(lead))
            window_full = idx - start >= FALLBACK_MAX_PAGES
            if new_heading or window_full:
                if idx > start:
                    sections.append(
                        Section(heading_path=heading_path, page_start=start, page_end=idx)
                    )
                start = idx
                heading_path = (
                    title
                    if not new_heading
                    else f"{title} > {_clean_title(lead)[:_FALLBACK_HEADING_CHARS]}"
                )
    sections.append(
        Section(heading_path=heading_path, page_start=start, page_end=len(page_texts))
    )
    return [s for s in sections if s.page_end > s.page_start]


def _page_label_range(labels: list[str | None]) -> str:
    """Printed-label range for a chunk's physical page span, or '' when the
    printed labels cannot honestly locate the whole span (issue #271).

    '' when any page in the span is unlabeled (a surviving label would
    understate the span) or when a multi-page span starts and ends on the
    same printed label (repeated folios, e.g. every page labeled 'A-'). The
    citation then falls back to the physical PDF pages (page_start/page_end).
    A printed label is never invented for an unlabeled page."""
    present = [lbl for lbl in labels if lbl]
    if not present or len(present) != len(labels):
        return ""
    first, last = present[0], present[-1]
    if len(present) == 1:
        return first
    return "" if first == last else f"{first}\u2013{last}"


def make_chunks(
    parsed: ParsedDoc, page_texts: list[str], page_labels: list[str | None] | None = None
) -> list[Chunk]:
    doc_id = parsed.doc_id or parsed.path.stem
    # Chunk identity keys on the source revision (issue #361): the payload
    # doc_id stays the printed family key for citations and filters.
    revision = source_rev_key(parsed.vendor, parsed.product, parsed.version, parsed.sha256)
    labels = page_labels or [None] * parsed.page_count
    chunks: list[Chunk] = []

    sections = (
        outline_sections(parsed, page_texts) if parsed.toc else fallback_sections(page_texts, parsed.title)
    )
    for section in sections:
        body_pages = list(page_texts[section.page_start : section.page_end])
        if body_pages and (section.start_char or section.end_char is not None):
            body_pages[0] = body_pages[0][section.start_char : section.end_char]
        # Issue #591: a wait-state section's bare 3-hex entries canonicalize
        # to the W-form the query parser emits for them, so both sides agree.
        wait_state = bool(_WAITSTATE_HEADING_RE.search(section.heading_path))

        paras: list[tuple[int, str]] = []
        for offset, page_text in enumerate(body_pages):
            for para in _BLANK_SPLIT_RE.split(page_text):
                if para.strip():
                    paras.append((section.page_start + offset, para.strip()))
        if not paras:
            continue
        code_section = _is_code_section(paras)

        blocks = _build_blocks(paras, code_section)
        for ordinal, (page_start, page_end, text, spans) in enumerate(blocks):
            # UUID pins the span start: the deterministic chunk key contract
            # (revision|heading|page|ordinal) carries the source revision, so
            # same-form-number revisions never share point ids.
            chunk_id = make_chunk_id(revision, section.heading_path, page_start, ordinal)
            # One entry per physical page in the span; a page past the end of
            # the label list is unlabeled, never silently dropped (#271).
            span_labels = [
                labels[idx] if idx < len(labels) else None for idx in range(page_start, page_end + 1)
            ]
            label = _page_label_range(span_labels)
            # Issue #591: every code entry this block carries, canonicalized
            # for its section. The alias spelling stays out of `text` on
            # purpose (see _extract_system_codes) so unit spans stay aligned
            # and the stored text is the manual's own words. Only a code
            # section records them (#621) or splits on them.
            system_codes = _extract_system_codes(text, wait_state) if code_section else []
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    heading_path=section.heading_path,
                    page_start=page_start,
                    page_label=label,
                    chunk_type=classify(text),
                    text=text,
                    message_ids=find_message_ids(text),
                    members=find_members(text),
                    system_codes=system_codes,
                    ordinal=ordinal,
                    units=_stored_spans(spans),
                    page_end=page_end,
                )
            )

    return chunks


def _stored_spans(spans: tuple[UnitSpan, ...]) -> tuple[UnitSpan, ...] | None:
    """Persisted-span projection (issue #368): all-prose blocks store () —
    known prose, legacy char rules at pack. Blocks with atomic units store
    their spans, or None past _MAX_STORED_SPANS (pack redetects with the
    same detectors instead of shipping unbounded payloads)."""
    if not any(s.kind == UNIT_ATOMIC for s in spans):
        return ()
    if len(spans) > _MAX_STORED_SPANS:
        return None
    return spans
