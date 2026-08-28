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


def extract_citation_lines(text: str) -> list[str]:
    """Citation-shaped lines from the model output (after the Citations: header)."""
    lines: list[str] = []
    in_citations = False
    for line in text.splitlines():
        if CITATIONS_HEADER_RE.match(line):
            in_citations = True
            continue
        if in_citations:
            stripped = line.strip()
            if not stripped:
                if lines:
                    break  # blank line after the list ends it
                continue
            if stripped.startswith(("-", "*", "•", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9")):
                stripped = stripped.lstrip("-*•0123456789. ")
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
    exact-match rule as valid_citations: normalization stays out of scope."""
    kept: list[str] = []
    for line in text.splitlines():
        candidate = line.strip()
        if candidate.startswith(("-", "*", "•")):
            candidate = candidate.lstrip("-*• ").strip()
        elif len(candidate) > 2 and candidate[0].isdigit() and candidate[1] in ".)":
            candidate = candidate[2:].strip()
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
