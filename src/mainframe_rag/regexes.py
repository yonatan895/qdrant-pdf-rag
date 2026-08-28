"""Shared identifier regexes (Appendix A of docs/architecture.md).

Single source of truth imported by ingest and retrieve.
"""

import re

DOCNO_RE = re.compile(r"\b([A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?)\b")
MSG_RE = re.compile(r"\b([A-Z]{3}\d{2,5}[A-Z])\b")
# Prefer precision: xx-suffixed PARMLIB-style names (IEASYSxx, PROGxx) or short
# member-shaped tokens. Tune before widening; see architecture.md Appendix A.
MEMBER_RE = re.compile(r"\b([A-Z]{3,8}(?:xx|\d{2}))\b")

# Back/front matter bookmark titles to skip entirely. IBM titles often carry
# prefixes ("Appendix A. Notices"), so match titles ENDING with these words.
SKIP_ALWAYS_RE = re.compile(
    r"(notices?|trademarks?|reader'?s comments|bibliography|copyright|index)\s*$",
    re.IGNORECASE,
)
# Early Contents/Figures/Tables are front matter; a mid-book chapter with the
# same name must NOT be skipped (architecture.md section 4.1).
FRONT_MATTER_RE = re.compile(r"^(contents|figures|tables|summary of changes)$", re.IGNORECASE)


def find_docnos(text: str) -> list[str]:
    return sorted(set(DOCNO_RE.findall(text)))


def find_message_ids(text: str) -> list[str]:
    return sorted(set(MSG_RE.findall(text)))


def find_members(text: str) -> list[str]:
    return sorted(set(MEMBER_RE.findall(text)))
