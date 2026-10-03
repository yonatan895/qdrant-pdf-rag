"""Citation shape enforcement.

The agent must emit citations as:
    SA22-7592-05 z/OS MVS Initialization and Tuning Reference, IEASYSxx > LFAREA, p. 1-17
(doc number, title, heading path, page). The page is the printed page label,
or `PDF n` / `PDF n–m` physical pages when printed labels cannot locate the
chunk (issue #271); both share the `, p. <page>` tail. LLM output is filtered
to citations that match the format AND appear in the retrieved hit set.

The doc identifier is the IBM doc number when ingest found one, otherwise
the filename stem (`tss-messages`). CITATION_LINE_RE only knows the IBM
form, so every shape check also takes the supplied evidence's doc ids:
a filename-stem citation is recognized, validated and rejected exactly
like a doc-number one.
"""

from __future__ import annotations

import re
from collections.abc import Set as AbstractSet

# docno, title, heading path, page (printed label or `PDF n[–m]`, issue #271)
CITATION_LINE_RE = re.compile(
    r"^\s*(?P<doc_id>[A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?)\s+(?P<title>.+?),\s+"
    r"(?P<heading>.+?),\s+p\.\s+(?P<page>.+?)\s*$"
)

CITATIONS_HEADER_RE = re.compile(
    r"^\s*#{0,6}\s*[*_`]*Citations?[*_`]*:[*_`]*\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_CITATION_ALIAS_HEADER_RE = re.compile(
    r"^\s*#{0,6}\s*[*_`]*(?:Sources?|References?)[*_`]*:[*_`]*\s*$",
    re.IGNORECASE,
)

# List markers as a discrete prefix (bullet/space or number + [.)] + space),
# never a greedy char-set lstrip: "- **cite**" must strip only "- " so the
# enclosing markup peels as whole pairs afterwards. Prose like "3.5 inches"
# survives untouched. The "))(" paren form and the bullet set are a deliberate
# extension of the pre-PR-C list parser.
_MARKER_RE = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+|\[\d+\]:?\s*)+")

# Enclosing markup peeled pairwise (with repetition, so **x** and __x__
# resolve cleanly): bold, italic/underscore, inline code, quotes.
_WRAP_CHARS = "`\"'*_"

# Pasted heading-path fragments: the model sometimes copies excerpt
# boilerplate (related-document table rows, "About this document > Table
# 1 ..." lists) into the answer as a standalone line. They are docno-led
# and carry a " > " heading separator, but have no `, p. <page>` tail —
# so they are not citations (handled above) yet read as one while
# validating as nothing. Real prose either does not start with a docno
# or ends with sentence punctuation; real citations are in `allowed`.
_DOCNO_LED_RE = re.compile(r"^[A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?\s+\S")

# CITATION_LINE_RE after its doc id: title, heading path, page.
_CITATION_TAIL_RE = re.compile(r"^\S.*?,\s+.+?,\s+p\.\s+.+?\s*$")


def _known_doc_led(candidate: str, doc_ids: AbstractSet[str]) -> str | None:
    """Remainder after a supplied doc id that leads `candidate`, else None.
    Filename-stem ids have no fixed shape, so only ids actually supplied
    in the prompt are recognized; prose never starts with one by accident."""
    for doc_id in doc_ids:
        if doc_id and candidate.startswith(doc_id + " "):
            return candidate[len(doc_id) + 1 :]
    return None


def is_citation_shaped(candidate: str, doc_ids: AbstractSet[str] = frozenset()) -> bool:
    """Doc id, title, heading path, `, p. <page>` — for an IBM doc number
    (CITATION_LINE_RE) or any supplied doc id (filename stems)."""
    if CITATION_LINE_RE.match(candidate):
        return True
    rest = _known_doc_led(candidate, doc_ids)
    return rest is not None and bool(_CITATION_TAIL_RE.match(rest))


def _is_docno_led_fragment(candidate: str, doc_ids: AbstractSet[str]) -> bool:
    led = bool(_DOCNO_LED_RE.match(candidate)) or _known_doc_led(candidate, doc_ids) is not None
    return led and " > " in candidate and candidate[-1:] not in (".", "!", "?")


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


def extract_body_and_citations(
    text: str,
    doc_ids: AbstractSet[str] = frozenset(),
    allowed: AbstractSet[str] = frozenset(),
) -> tuple[str, list[str]]:
    """Separate prose from citations after Citations:, Sources: or References:.

    Canonical Citations: blocks consume citation-shaped or bulleted lines;
    citation shape includes supplied `doc_ids`, and an exact `allowed`
    line is always a citation.
    Alias blocks consume only citation-shaped lines; a header followed by prose
    is retained with that prose. After a non-citation line or a blank past seen
    cites, subsequent lines are preserved as answer prose.
    """
    body_lines: list[str] = []
    raw_citation_lines: list[str] = []
    in_citations = False
    allow_bullets = False
    pending_header: str | None = None
    block_has_cites = False

    for line in text.splitlines():
        canonical = bool(CITATIONS_HEADER_RE.match(line))
        if canonical or _CITATION_ALIAS_HEADER_RE.match(line):
            in_citations = True
            allow_bullets = canonical
            pending_header = None if canonical else line
            block_has_cites = False
            continue
        if in_citations:
            raw = line.strip()
            if not raw:
                if block_has_cites:
                    in_citations = False
                continue
            is_bullet = bool(_MARKER_RE.match(raw))
            stripped = _normalize_citation_line(raw)
            is_cite = stripped in allowed or is_citation_shaped(stripped, doc_ids)

            if is_cite or (allow_bullets and is_bullet):
                raw_citation_lines.append(stripped)
                pending_header = None
                block_has_cites = True
                continue
            else:
                in_citations = False
                if pending_header is not None:
                    body_lines.append(pending_header)
                    pending_header = None
                body_lines.append(line)
                continue
        body_lines.append(line)

    return "\n".join(body_lines), raw_citation_lines


def extract_citation_lines(
    text: str, doc_ids: AbstractSet[str] = frozenset(), allowed: AbstractSet[str] = frozenset()
) -> list[str]:
    """Citation-shaped lines from the model output (after the Citations: header)."""
    _, lines = extract_body_and_citations(text, doc_ids, allowed)
    return lines


def valid_citations(
    text: str, allowed: AbstractSet[str], doc_ids: AbstractSet[str] = frozenset()
) -> list[str]:
    """Keep only well-formed citations that map to retrieved chunks."""
    result: list[str] = []
    for line in extract_citation_lines(text, doc_ids, allowed):
        if line in allowed and line not in result:
            result.append(line)
    return result


def split_unauthorized_citations(
    text: str, allowed: AbstractSet[str], doc_ids: AbstractSet[str] = frozenset()
) -> tuple[str, list[str]]:
    """Body-level citation hygiene, one predicate for strip and count.

    Returns (kept_text, rejected). Rejected entries are the normalized
    standalone citation lines that are not in the hit set, plus docno-led
    heading-path fragments. Same exact-match rule as valid_citations.

    Operates on standalone citation lines only: a mid-sentence inline mention
    ("refer to SA22-9999-99 ... for details") never matches the full line
    shape and is deliberately left untouched — stripping mid-prose would
    corrupt the answer."""
    kept: list[str] = []
    rejected: list[str] = []
    for line in text.splitlines():
        candidate = _normalize_citation_line(line)
        if candidate in allowed:
            kept.append(line)
            continue
        if is_citation_shaped(candidate, doc_ids) or _is_docno_led_fragment(candidate, doc_ids):
            rejected.append(candidate)
            continue
        kept.append(line)
    return "\n".join(kept), rejected


def strip_unauthorized_citations(
    text: str, allowed: AbstractSet[str], doc_ids: AbstractSet[str] = frozenset()
) -> str:
    """Remove citation-shaped lines from the answer body that are not in the
    retrieved hit set. The trailing Citations: list is validated separately;
    this closes the same hole for a fabricated cite quoted mid-answer —
    including wrapped forms (markup, blockquote, quotes) via the shared
    normalizer."""
    return split_unauthorized_citations(text, allowed, doc_ids)[0]


__all__ = [
    "CITATION_LINE_RE",
    "extract_body_and_citations",
    "extract_citation_lines",
    "is_citation_shaped",
    "normalize_citation_line",
    "split_unauthorized_citations",
    "strip_unauthorized_citations",
    "valid_citations",
]
