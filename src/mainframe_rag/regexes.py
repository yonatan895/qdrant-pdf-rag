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
#
# Shape alone cannot carry these: 3 hex characters is far too common an
# English word fragment ("add", "fee", "bad") and an S-prefix collides with
# model numbers (S390) and form numbers (SC23-6862). So the family is
# context-gated, and each family is normalised to the form the manual's
# entry line uses, which is what ingest stores and the filter matches.
#
# Self-contexting (the token shape carries the meaning):
#   S0C4    -> 0C4    system completion code
#   X'0C4'  -> 0C4    hex literal spelling of the same
#   U4038   -> U4038  user completion code (U + 4 digits is unambiguous)
# Wait states are gated on the phrase, never on a bare W-token: `wait state
# 064` -> W064, while "wait state 064" in prose stays a phrase.
#
# Bare 3-hex (0C4, 806, 222) carries no meaning on its own and is the case
# measured as noisy in issue #591: a run of bare codes is an index, and a
# 3-letter word is a word. It is accepted only ADJACENT to a code phrase
# ("abend 0C4", "completion code 222", "system code 0C4") and must carry a
# digit. Adjacency, not mere presence, is what keeps "…read 100 records
# from the ADD file …" out of the identifier path: the word abend appears
# once in the sentence, far from the number.
_SYSCODE_S_RE = re.compile(r"\bS([0-9][0-9A-F]{2})\b", re.IGNORECASE)
_SYSCODE_HEX_RE = re.compile(r"\bX'([0-9A-F]{3})'(?![0-9A-F])", re.IGNORECASE)
_USERCODE_RE = re.compile(r"\bU(\d{4})\b", re.IGNORECASE)
_WAITSTATE_RE = re.compile(
    r"\bwait\s+state\s+([0-9A-F]{3})\b", re.IGNORECASE
)
_SYSCODE_BARE_RE = re.compile(r"\b([0-9A-F]{3})\b", re.IGNORECASE)
# Code phrase that licenses an adjacent bare code: "abend", "abended",
# "completion code", "system code". Matched with its trailing connector
# words so the bare code can sit one or two words away ("abend code 0C4").
_SYSCODE_CONTEXT_BEFORE_RE = re.compile(
    r"(?:abend\w*|(?:completion|system)\s+code(?:s)?)(?:\s+\w+){0,2}\s*\w*\s*$",
    re.IGNORECASE,
)
_SYSCODE_CONTEXT_AFTER_RE = re.compile(
    r"^\s*\w*(?:\s+\w+){0,2}\s*(?:abend\w*|(?:completion|system)\s+code(?:s)?)\b",
    re.IGNORECASE,
)
# Model numbers that share the S+3-hex shape. Listed, not pattern-guessed:
# every exclusion is a reviewed token, so a new architecture number is a
# deliberate addition rather than an accident.
_SYSCODE_MODEL_RE = re.compile(r"\bS(?:370|390)\b", re.IGNORECASE)


def find_system_codes(text: str) -> list[str]:
    """System/user/wait-state completion codes in canonical form (issue #591).

    `0C4` for system codes, `U4038` for user codes, `W064` for wait states.
    Callers must not feed this a whole document: bare 3-hex needs a code
    phrase adjacent to it, so prose that merely discusses hex values yields
    nothing. Returns a sorted, de-duplicated list.
    """
    codes: set[str] = set()
    # Model numbers are codes-shaped but never completion codes; blanking
    # them keeps S390 from yielding both 390 and a spurious match.
    text = _SYSCODE_MODEL_RE.sub(" ", text)
    for m in _SYSCODE_S_RE.finditer(text):
        codes.add(m.group(1).upper())
    for m in _SYSCODE_HEX_RE.finditer(text):
        codes.add(m.group(1).upper())
    for m in _USERCODE_RE.finditer(text):
        codes.add(f"U{m.group(1)}")
    for m in _WAITSTATE_RE.finditer(text):
        codes.add(f"W{m.group(1).upper()}")
    for m in _SYSCODE_BARE_RE.finditer(text):
        token = m.group(1).upper()
        # A code always carries a digit (806, 222, 0C4); ADD/FEE/BAD do not.
        if not any(ch.isdigit() for ch in token):
            continue
        before = text[: m.start()]
        after = text[m.end() :]
        if _SYSCODE_CONTEXT_BEFORE_RE.search(before) or (
            _SYSCODE_CONTEXT_AFTER_RE.search(after)
        ):
            codes.add(token)
    return sorted(codes)
