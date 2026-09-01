"""Citation shape enforcement.

The agent must emit citations as:
    SA22-7592-05 z/OS MVS Initialization and Tuning Reference, IEASYSxx > LFAREA, p. 1-17
(doc number, title, heading path, printed page label). LLM output is filtered
to citations that match the format AND appear in the retrieved hit set.
"""

from __future__ import annotations

import re

# docno, title, heading path, printed page label
CITATION_LINE_RE = re.compile(
    r"^\s*(?P<doc_id>[A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?)\s+(?P<title>.+?),\s+"
    r"(?P<heading>.+?),\s+p\.\s+(?P<page>.+?)\s*$"
)

CITATIONS_HEADER_RE = re.compile(r"^\s*#{0,6}\s*Citations?:\s*$", re.IGNORECASE | re.MULTILINE)

# List markers as a discrete prefix (bullet/space or number + [.)] + space),
# never a greedy char-set lstrip: "- **cite**" must strip only "- " so the
# enclosing markup peels as whole pairs afterwards. Prose like "3.5 inches"
# survives untouched. The "))(" paren form and the bullet set are a deliberate
# extension of the pre-PR-C list parser.
_MARKER_RE = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+)+")

# Enclosing markup peeled pairwise (with repetition, so **x** and __x__
# resolve cleanly): bold, italic/underscore, inline code, quotes.
_WRAP_CHARS = "`\"'*_"


def normalize_citation_line(line: str) -> str:
    """One normalizer for both citation paths (the Citations: list parser and
    the answer-body scanner) so a wrapped fabricated cite can never be clean
    in one path and leaked by the other. Supported wrappers, each peeled
    cleanly to the bare citation: list markers (bullet or number + [.)]),
    blockquote '>', enclosing pairs (** __ * ` " '), angle brackets <...>,
    markdown links [x](url), and parentheses. Only CITATION_LINE_RE matches
    act, and the body scanner keeps the original line in the output."""
    candidate = line.strip()
    for _ in range(6):  # bounded: '> "11. cite"' style nesting is shallow
        before = candidate
        candidate = _MARKER_RE.sub("", candidate)
        if candidate.startswith(">"):
            candidate = candidate.lstrip(">").strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in _WRAP_CHARS:
            candidate = candidate[1:-1].strip()
        if candidate.startswith("[") and candidate.endswith(")"):
            idx = candidate.find("](")
            if idx != -1:
                candidate = candidate[1:idx]
        if candidate.startswith("<") and candidate.endswith(">"):
            candidate = candidate[1:-1].strip()
        if candidate.startswith("(") and candidate.endswith(")"):
            candidate = candidate[1:-1].strip()
        if candidate == before:
            break
    return candidate


_normalize_citation_line = normalize_citation_line


def extract_body_and_citations(text: str) -> tuple[str, list[str]]:
    """Separates answer body prose from citations following any Citations: header.

    A citations block begins at CITATIONS_HEADER_RE and consumes citation-shaped
    lines matching CITATION_LINE_RE or bulleted list markers. At the first non-citation/
    non-bullet line or blank line, the citations block terminates, and subsequent lines
    are preserved as answer prose.
    """
    body_lines: list[str] = []
    raw_citation_lines: list[str] = []
    in_citations = False

    for line in text.splitlines():
        if CITATIONS_HEADER_RE.match(line):
            in_citations = True
            continue
        if in_citations:
            raw = line.strip()
            if not raw:
                if raw_citation_lines:
                    in_citations = False
                continue
            is_bullet = bool(_MARKER_RE.match(raw))
            stripped = _normalize_citation_line(raw)
            is_cite = bool(CITATION_LINE_RE.match(stripped))

            if is_cite or is_bullet:
                raw_citation_lines.append(stripped)
                continue
            else:
                # Non-citation, non-bullet line ends the block; belongs to body prose
                in_citations = False
                body_lines.append(line)
                continue
        body_lines.append(line)

    return "\n".join(body_lines), raw_citation_lines


def extract_citation_lines(text: str) -> list[str]:
    """Citation-shaped lines from the model output (after the Citations: header)."""
    _, lines = extract_body_and_citations(text)
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
    including wrapped forms (markup, blockquote, quotes) via the shared
    normalizer. Same exact-match rule as valid_citations.

    Operates on standalone citation lines only: a mid-sentence inline mention
    ("refer to SA22-9999-99 ... for details") never matches the full line
    shape and is deliberately left untouched — stripping mid-prose would
    corrupt the answer."""
    kept: list[str] = []
    for line in text.splitlines():
        candidate = _normalize_citation_line(line)
        if CITATION_LINE_RE.match(candidate) and candidate not in allowed:
            continue
        kept.append(line)
    return "\n".join(kept)


__all__ = [
    "CITATION_LINE_RE",
    "extract_body_and_citations",
    "extract_citation_lines",
    "normalize_citation_line",
    "strip_unauthorized_citations",
    "valid_citations",
]
