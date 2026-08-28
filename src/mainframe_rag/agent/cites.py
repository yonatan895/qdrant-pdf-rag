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
# bracketed ("12)") forms.
_LIST_MARKER_CHARS = "-*•0123456789. )("


def _strip_list_marker(line: str) -> str:
    """One marker-stripper for both the Citations: list parser and the answer
    body scanner, so the two can never diverge."""
    if line.startswith(("-", "*", "•")) or line[:1].isdigit():
        return line.lstrip(_LIST_MARKER_CHARS)
    return line


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
            stripped = _strip_list_marker(raw)
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
    this closes the same hole for a fabricated cite quoted mid-answer. Same
    exact-match rule and the same list-marker handling as valid_citations."""
    kept: list[str] = []
    for line in text.splitlines():
        candidate = _strip_list_marker(line.strip())
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
