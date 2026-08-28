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

SECTION_MAX_CHARS = 6000
SPLIT_OVERLAP_CHARS = 400
FRONT_MATTER_FRACTION = 0.15
FRONT_MATTER_MIN_PAGES = 2

_BLANK_SPLIT_RE = re.compile(r"\n\s*\n")


@dataclass
class Section:
    heading_path: str
    page_start: int
    page_end: int


@dataclass
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


def make_chunk_id(doc_id: str, heading_path: str, page_start: int, ordinal: int) -> str:
    key = f"{doc_id}|{heading_path}|{page_start}|{ordinal}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


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


def _split_blocks(paras: list[tuple[int, str]]) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    current: list[tuple[int, str]] = []
    current_len = 0

    for page_idx, para in paras:
        if len(para) > SECTION_MAX_CHARS:
            if current:
                blocks.append((current[0][0], "\n\n".join(p for _, p in current)))
                current, current_len = [], 0
            for i in range(0, len(para), SECTION_MAX_CHARS):
                blocks.append((page_idx, para[i : i + SECTION_MAX_CHARS]))
            continue
        if current_len + len(para) > SECTION_MAX_CHARS and current:
            blocks.append((current[0][0], "\n\n".join(p for _, p in current)))
            tail = "\n\n".join(p for _, p in current)[-SPLIT_OVERLAP_CHARS:]
            current = [(current[-1][0], tail), (page_idx, para)]
            current_len = len(tail) + len(para) + 2
        else:
            current.append((page_idx, para))
            current_len += len(para) + 2

    if current:
        blocks.append((current[0][0], "\n\n".join(p for _, p in current)))
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
