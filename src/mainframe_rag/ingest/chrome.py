"""Running header/footer stripping by line frequency.

A line that appears (normalized) on >= 35% of sampled pages, within the first
or last EDGE_LINES text lines of those pages, is page chrome. Folios and
folio ranges are likewise only recognized at the page edges: a bare number in
the page body is content (a completion code, a table value), never a folio.
Short documents must not use a threshold of 1 (that would delete every line).
"""

from __future__ import annotations

import re
from collections import Counter

FREQUENCY_THRESHOLD = 0.35
SAMPLE_TARGET = 64
MIN_PAGES_FOR_CHROME = 8
MIN_HITS = 3
# Running headers/footers sit at the page edges. Counting only the first/last
# EDGE_LINES text lines keeps structural labels that repeat in the body
# ("Explanation:") and bare code lines ("806") from being read as chrome.
EDGE_LINES = 4

_WHITESPACE_RE = re.compile(r"\s+")
# Bare page-number lines: decimal ("12", "1234", "12-34", "12.") or a strict
# roman numeral ("xiv", "XII", "i", "iv." — front matter renders as "iv." as
# often as "iv", so both forms accept one trailing [-.]). The roman form is
# structural, not a char-set: [ivxlcdm]+ with IGNORECASE also matched real
# words, so standalone "XML", "civil", "dim" lines were silently deleted from
# pages. The decimal form keeps an inner dot out ("1.2" is a section number,
# not a footer) and is ASCII-only. The roman lookahead rejects empty input
# structurally (an empty fullmatch would otherwise eat every blank line), and
# valid numerals that are also words ("mix" = 1009, "di" = 501) stay treated
# as numerals — inherent ambiguity, not worth a word list.
_ROMAN_NUMERAL_RE = re.compile(
    r"(?=[ivxlcdm])m*(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})(?:[-.])?",
    re.IGNORECASE,
)
_DECIMAL_PAGE_RE = re.compile(r"[0-9]+(?:-[0-9]+)?[-.]?")
# A header/footer that names the first and last entry on the page: a folio
# range, not content. Entries are completion codes ("805 • 806"), message IDs
# ("ICH408I • ICH409I", "U901 • U902") or a family with a lowercase
# placeholder ("0BB • 0Cx", "EC7 • FFx"). A token is either hex (an x
# placeholder may follow) or letters then a digit; case-sensitive, which keeps
# ordinary words out ("Yes • No", "TSO • ISPF").
_ENTRY_TOKEN = r"(?:[0-9A-F][0-9A-Fx]{1,7}|[A-Z]{1,8}[0-9][0-9A-Zx]{0,10})"
_ENTRY_RANGE_RE = re.compile(rf"{_ENTRY_TOKEN} \u2022 {_ENTRY_TOKEN}")


def _normalize(line: str) -> str:
    return _WHITESPACE_RE.sub(" ", line.strip()).lower()


def _is_page_number(line: str) -> bool:
    # No empty-string branch needed: both patterns require at least one
    # numeral character (decimal via [0-9]+, roman via the lookahead), and
    # strip_page never routes an empty normalized line here.
    return bool(
        _DECIMAL_PAGE_RE.fullmatch(line.strip()) or _ROMAN_NUMERAL_RE.fullmatch(line.strip())
    )


def _is_entry_range(line: str) -> bool:
    return bool(_ENTRY_RANGE_RE.fullmatch(_WHITESPACE_RE.sub(" ", line.strip())))


def _edges(lines: list[str]) -> tuple[set[int], set[int]]:
    """Indices of the first/last EDGE_LINES text lines of a page. Lines with no
    letter or digit (revision/change bars, rules) are not text and do not
    push a footer out of the edge window."""
    text = [i for i, ln in enumerate(lines) if any(ch.isalnum() for ch in ln)]
    return set(text[:EDGE_LINES]), set(text[-EDGE_LINES:])


def _sample_indices(page_count: int) -> list[int]:
    if page_count <= SAMPLE_TARGET:
        return list(range(page_count))
    step = page_count / SAMPLE_TARGET
    return sorted({min(page_count - 1, int(i * step)) for i in range(SAMPLE_TARGET)})


def chrome_lines(page_texts: list[str]) -> set[str]:
    """Normalized page-edge lines that appear on >= 35% of sampled pages."""
    pages = [page_texts[i] for i in _sample_indices(len(page_texts))]
    if len(pages) < MIN_PAGES_FOR_CHROME:
        return set()
    counts: Counter[str] = Counter()
    for text in pages:
        raw = text.splitlines()
        top, bottom = _edges(raw)
        counts.update({ln for i in top | bottom if (ln := _normalize(raw[i]))})
    threshold = max(MIN_HITS, int(FREQUENCY_THRESHOLD * len(pages)))
    return {line for line, n in counts.items() if n >= threshold}


def strip_page(text: str, chrome: set[str]) -> str:
    """Drop page-edge chrome and page-number lines from one page's text."""
    kept = []
    lines = text.splitlines()
    top, bottom = _edges(lines)
    # A page has one folio. A footer folio means the top edge carries none, so
    # a bare number there is content (an entry's code line starting the page).
    footer_folio = any(_is_page_number(lines[i]) or _is_entry_range(lines[i]) for i in bottom)
    for i, line in enumerate(lines):
        norm = _normalize(line)
        if not norm:
            kept.append(line)
            continue
        if (i in top or i in bottom) and norm in chrome:
            continue
        if _is_page_number(line) and (i in bottom or (i in top and not footer_folio)):
            continue
        # An entry range is never body text, so unlike a bare number it goes
        # at either edge: message manuals put it in the header above a footer
        # folio, where the one-folio rule kept it.
        if (i in top or i in bottom) and _is_entry_range(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def strip_chrome(page_texts: list[str]) -> list[str]:
    """Strip running headers/footers across a document."""
    chrome = chrome_lines(page_texts)
    return [strip_page(t, chrome) for t in page_texts]
