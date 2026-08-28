"""Running header/footer stripping by line frequency.

A line that appears (normalized) on >= 35% of sampled pages is page chrome
("© Copyright IBM Corp. 1994, 2025", chapter titles, page numbers) and would
otherwise pollute embeddings. architecture.md section 4.1.
"""

from __future__ import annotations

import re
from collections import Counter

FREQUENCY_THRESHOLD = 0.35
SAMPLE_TARGET = 64

_WHITESPACE_RE = re.compile(r"\s+")
_PAGE_NUMBER_RE = re.compile(r"^\d{1,4}$|^[ivxlcdm]{1,8}$|^\d+[-.]?\d*$", re.IGNORECASE)


def _normalize(line: str) -> str:
    return _WHITESPACE_RE.sub(" ", line.strip()).lower()


def _is_page_number(line: str) -> bool:
    return bool(_PAGE_NUMBER_RE.match(line.strip()))


def _sample_indices(page_count: int) -> list[int]:
    if page_count <= SAMPLE_TARGET:
        return list(range(page_count))
    step = page_count / SAMPLE_TARGET
    return sorted({min(page_count - 1, int(i * step)) for i in range(SAMPLE_TARGET)})


def chrome_lines(page_texts: list[str]) -> set[str]:
    """Normalized lines that appear on >= 35% of sampled pages."""
    pages = [page_texts[i] for i in _sample_indices(len(page_texts))]
    if not pages:
        return set()
    counts: Counter[str] = Counter()
    for text in pages:
        lines = {ln for ln in (_normalize(l) for l in text.splitlines()) if ln}
        counts.update(lines)
    threshold = max(1, int(FREQUENCY_THRESHOLD * len(pages)))
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
