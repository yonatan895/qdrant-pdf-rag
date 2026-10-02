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


# System/user/wait-state completion codes (issue #591).
# S-prefix is self-contexting: S0C4 → 0C4. The first char after S must be
# a digit to avoid matching doc numbers like SC23-6862 (S + C23).
_SYSCODE_S_RE = re.compile(r"\bS([0-9][0-9A-F]{2})\b", re.IGNORECASE)
# Hex literal is self-contexting: X'0C4' → 0C4.
_SYSCODE_HEX_RE = re.compile(r"X'([0-9A-F]{3})'(?![0-9A-F])", re.IGNORECASE)
# Bare 3-hex needs context: 0C4 → 0C4 only with abend/completion code/system code.
_SYSCODE_BARE_RE = re.compile(r"\b([0-9A-F]{3})\b", re.IGNORECASE)
# User completion code: U4038 → U4038, needs abend context.
_USERCODE_RE = re.compile(r"\b(U\d{4})\b", re.IGNORECASE)
# Wait state: wait state 064 → W064.
_WAITSTATE_RE = re.compile(r"\bwait\s+state\s+([0-9A-F]{3})\b", re.IGNORECASE)
# Context words that gate bare code extraction.
_SYSCODE_CONTEXT_RE = re.compile(
    r"(?:abend|completion\s+code|system\s+code)",
    re.IGNORECASE,
)


def find_system_codes(text: str) -> list[str]:
    """Extract system/user/wait-state completion codes (issue #591).

    Returns canonical forms: 0C4 (system), U4038 (user), W064 (wait).
    Context-gated: bare 3-hex and user codes require abend/completion-code/
    system-code context. S-prefix, X'...', and wait-state are self-contexting.
    """
    codes: set[str] = set()
    for m in _SYSCODE_S_RE.finditer(text):
        codes.add(m.group(1).upper())
    for m in _SYSCODE_HEX_RE.finditer(text):
        codes.add(m.group(1).upper())
    for m in _WAITSTATE_RE.finditer(text):
        codes.add(f"W{m.group(1).upper()}")
    if _SYSCODE_CONTEXT_RE.search(text):
        for m in _SYSCODE_BARE_RE.finditer(text):
            codes.add(m.group(1).upper())
        for m in _USERCODE_RE.finditer(text):
            codes.add(m.group(1).upper())
    return sorted(codes)
