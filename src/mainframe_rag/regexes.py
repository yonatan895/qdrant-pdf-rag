"""Shared identifier regexes (Appendix A of docs/retrieval.md).

Single source of truth imported by ingest and retrieve.
"""

import re

DOCNO_RE = re.compile(r"\b([A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?)\b")
# Message ids: classic 3-letter form (IEA500I) plus the families the 3-letter
# shape misses (issue #120, measured on real Broadcom/IBM corpora):
# - CICS DFH cards with 0-2 middle letters and no trailing severity
#   (DFHAC2006, DFHSI1579, DFH0690);
# - IMS DFS codes with optional trailing severity (DFS058 alongside DFS058I);
# - 4-letter-prefix codes (DSNA670I, TSSC001E, BPXI040I, CSLM000I).
# One shared pattern on purpose: ingest payloads and query parsing use the
# same helper, so a token either matches on both sides or neither — there
# is no asymmetric false positive, only term matching. Tune before widening
# further; see retrieval.md Appendix A.
MSG_RE = re.compile(
    r"\b([A-Z]{3}\d{2,5}[A-Z]|DFH[A-Z]{0,2}\d{4,5}|DFS\d{3,4}[A-Z]?|[A-Z]{4}\d{2,5}[A-Z])\b"
)
# Prefer precision: xx-suffixed PARMLIB-style names (IEASYSxx, PROGxx) or short
# member-shaped tokens. Tune before widening; see retrieval.md Appendix A.
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
