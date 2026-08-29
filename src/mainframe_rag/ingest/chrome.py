"""Running header/footer stripping by line frequency.

A line that appears (normalized) on >= 35% of sampled pages is page chrome.
Short documents must not use a threshold of 1 (that would delete every line).
"""

from __future__ import annotations

import re
from collections import Counter

FREQUENCY_THRESHOLD = 0.35
SAMPLE_TARGET = 64
MIN_PAGES_FOR_CHROME = 8
MIN_HITS = 3

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


def _normalize(line: str) -> str:
    return _WHITESPACE_RE.sub(" ", line.strip()).lower()


def _is_page_number(line: str) -> bool:
    # No empty-string branch needed: both patterns require at least one
    # numeral character (decimal via [0-9]+, roman via the lookahead), and
    # strip_page never routes an empty normalized line here.
    return bool(
        _DECIMAL_PAGE_RE.fullmatch(line.strip()) or _ROMAN_NUMERAL_RE.fullmatch(line.strip())
    )


def _sample_indices(page_count: int) -> list[int]:
    if page_count <= SAMPLE_TARGET:
        return list(range(page_count))
    step = page_count / SAMPLE_TARGET
    return sorted({min(page_count - 1, int(i * step)) for i in range(SAMPLE_TARGET)})


def chrome_lines(page_texts: list[str]) -> set[str]:
    """Normalized lines that appear on >= 35% of sampled pages."""
    pages = [page_texts[i] for i in _sample_indices(len(page_texts))]
    if len(pages) < MIN_PAGES_FOR_CHROME:
        return set()
    counts: Counter[str] = Counter()
    for text in pages:
        lines = {ln for ln in (_normalize(l) for l in text.splitlines()) if ln}
        counts.update(lines)
    threshold = max(MIN_HITS, int(FREQUENCY_THRESHOLD * len(pages)))
    return {line for line, n in counts.items() if n >= threshold}


def strip_page(text: str, chrome: set[str]) -> str:
    """Drop chrome lines and bare page-number lines from one page's text."""
    kept = []
    for line in text.splitlines():
        norm = _normalize(line)
        if not norm:
            kept.append(line)
            continue
        if norm in chrome or _is_page_number(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def strip_chrome(page_texts: list[str]) -> list[str]:
    """Strip running headers/footers across a document."""
    chrome = chrome_lines(page_texts)
    return [strip_page(t, chrome) for t in page_texts]
