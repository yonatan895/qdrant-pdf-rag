"""Citation shape enforcement.

The agent must emit citations as:
    SA22-7592-05 z/OS MVS Initialization and Tuning Reference, IEASYSxx > LFAREA, p. 1-17
(doc number, title, heading path, printed page label). LLM output is filtered
to citations that match the format AND appear in the retrieved hit set.
"""

from __future__ import annotations

import re

from mainframe_rag.retrieve.query import format_citation

# docno, title, heading path, printed page label
CITATION_LINE_RE = re.compile(
    r"^\s*(?P<doc_id>[A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?)\s+(?P<title>.+?),\s+"
    r"(?P<heading>.+?),\s+p\.\s+(?P<page>.+?)\s*$"
)

CITATIONS_HEADER_RE = re.compile(r"^\s*Citations?:\s*$", re.IGNORECASE | re.MULTILINE)

# Bullet/dash/numbered-list markers, including multi-digit ("11.") and
# bracketed ("12)") forms. Extends the pre-PR-C set with ")(" — a deliberate,
# shared-behavior delta in the citations list parser.
_LIST_MARKER_CHARS = "-*•0123456789. )("

_WRAP_QUOTES = "`\"'"


def _normalize_citation_line(line: str) -> str:
    """One normalizer for both citation paths (the Citations: list parser and
    the answer-body scanner) so a wrapped fabricated cite can never be clean
    in one path and leaked by the other: peels list markers, blockquote '>',
    and matching wrapping quotes/backticks/parens. Over-stripping digit-led
    prose is harmless — only CITATION_LINE_RE matches act, and the body
    scanner keeps the original line in the output."""
    candidate = line.strip()
    for _ in range(4):  # bounded: '> "11. cite"' style nesting is shallow
        before = candidate
        if candidate[:1].isdigit() or candidate.startswith(("-", "*", "•")):
            candidate = candidate.lstrip(_LIST_MARKER_CHARS).strip()
        if candidate.startswith(">"):
            candidate = candidate.lstrip(">").strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in _WRAP_QUOTES:
            candidate = candidate[1:-1].strip()
        if candidate.startswith("(") and candidate.endswith(")"):
            candidate = candidate[1:-1].strip()
        if candidate == before:
            break
    return candidate


def extract_citation_lines(text: str) -> list[str]:
    """Citation-shaped lines from the model output (after the Citations: header)."""
    lines: list[str] = []
    in_citations = False
    for line in text.splitlines():
        if CITATIONS_HEADER_RE.match(line):
            in_citations = True
            continue
        if in_citations:
            raw = line.strip()
            if not raw:
                if lines:
                    break  # blank line after the list ends it
                continue
            stripped = _normalize_citation_line(raw)
            if stripped:
                lines.append(stripped)
    return lines


def valid_citations(text: str, allowed: set[str]) -> list[str]:
    """Keep only well-formed citations that map to retrieved chunks."""
    result: list[str] = []
    for line in extract_citation_lines(text):
        if line in allowed and line not in result:
            result.append(line)
    return result


def strip_unauthorized_citations(text: str, allowed: set[str]) -> str:
    """Remove citation-shaped lines from the answer body that are not in the
    retrieved hit set. The trailing Citations: list is validated separately;
    this closes the same hole for a fabricated cite quoted mid-answer —
    including wrapped forms (blockquote, backticks, quotes) via the shared
    normalizer. Same exact-match rule as valid_citations."""
    kept: list[str] = []
    for line in text.splitlines():
        candidate = _normalize_citation_line(line)
        if CITATION_LINE_RE.match(candidate) and candidate not in allowed:
            continue
        kept.append(line)
    return "\n".join(kept)


__all__ = [
    "CITATION_LINE_RE",
    "extract_citation_lines",
    "format_citation",
    "strip_unauthorized_citations",
    "valid_citations",
]
