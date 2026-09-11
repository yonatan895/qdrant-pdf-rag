"""Chunk classification: message | syntax | table | narrative.

Message sections start with an MVS-style message ID (XXXnnnY), possibly after a
short heading. Syntax sections use diagrams (>>-, box drawing, ::=, <parm>).
"""

from __future__ import annotations

import re
from typing import Literal

# Frozen `chunk_type` vocabulary (AGENTS.md: no new values). The Literal
# annotation makes mypy reject a new return value; the vocabulary test pins
# the exact set.
ChunkType = Literal["message", "syntax", "table", "narrative"]

_BOX_CHARS = set("\u2500\u2502\u250c\u2510\u2514\u2518\u251c\u2524\u252c\u2534\u253c\u2501\u2503\u250f\u2513\u2517\u251b")
_SYNTAX_RE = re.compile(r"(::=|>>-|>>\+|<--|--\+|-\+-|--\\-)")
_PARM_RE = re.compile(r"<[a-zA-Z][\w-]*>")
_COLUMN_RE = re.compile(r"\S(?:.*\S)?(?:\s{2,}\S)+")

# Deliberately narrower than regexes.MSG_RE: chunk_type is a precision label
# (line-anchored classic form only), while MSG_RE is the broad extractor.
# Widening this changes chunk_type distribution — needs eval, not a cleanup.
#
# Issue #216 widens the anchored families toward MSG_RE (CICS DFH cards,
# IMS DFS codes with optional severity, 4-letter-prefix codes) so
# table/syntax-embedded diagnostics classify as message. Still
# line-anchored: a bare mention mid-line never flips a chunk. No new
# chunk_type values (fixed vocabulary: message/syntax/table/narrative).
MESSAGE_LINE_RE = re.compile(
    r"^\s*(?:[A-Z]{3}\d{2,5}[A-Z]|DFH[A-Z]{0,2}\d{4,5}|DFS\d{3,4}[A-Z]?|[A-Z]{4}\d{2,5}[A-Z])"
)

# How many leading non-blank lines may carry explanation before the
# anchored message id (issue #216 "buried-after-explanation"). Six, not
# four: message docs open with 1-2 explanation lines above the id card.
MESSAGE_SCAN_LINES = 6


def is_table_block(text: str) -> bool:
    """Column-block test shared by classify() and chunk splitting (one rule
    per concept, issue #216): ≥0.6 of the non-blank lines carry 2+-spaced
    columns. Empty text is never a table."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    columnish = sum(1 for ln in lines if _COLUMN_RE.search(ln))
    return columnish / len(lines) >= 0.6


def classify(text: str) -> ChunkType:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "narrative"

    for line in lines[:MESSAGE_SCAN_LINES]:
        if MESSAGE_LINE_RE.match(line.strip()):
            return "message"

    box_lines = sum(1 for ln in lines if set(ln) & _BOX_CHARS)
    syntax_lines = sum(1 for ln in lines if _SYNTAX_RE.search(ln))
    if "::=" in text or box_lines >= 2 or syntax_lines >= 2 or _PARM_RE.search(text):
        return "syntax"

    if is_table_block(text):
        return "table"

    return "narrative"
