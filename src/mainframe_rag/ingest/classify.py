"""Chunk classification: message | syntax | table | narrative.

Message sections start with an MVS-style message ID (XXXnnnY), possibly after a
short heading. Syntax sections use diagrams (>>-, box drawing, ::=, <parm>).
"""

from __future__ import annotations

import re

_BOX_CHARS = set("\u2500\u2502\u250c\u2510\u2514\u2518\u251c\u2524\u252c\u2534\u253c\u2501\u2503\u250f\u2513\u2517\u251b")
_SYNTAX_RE = re.compile(r"(::=|>>-|>>\+|<--|--\+|-\+-|--\\-)")
_PARM_RE = re.compile(r"<[a-zA-Z][\w-]*>")
_COLUMN_RE = re.compile(r"\S(?:.*\S)?(?:\s{2,}\S)+")

# Deliberately narrower than regexes.MSG_RE: chunk_type is a precision label
# (line-anchored classic form only), while MSG_RE is the broad extractor.
# Widening this changes chunk_type distribution — needs eval, not a cleanup.
MESSAGE_LINE_RE = re.compile(r"^\s*[A-Z]{3}\d{2,5}[A-Z]")


def classify(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "narrative"

    for line in lines[:4]:
        if MESSAGE_LINE_RE.match(line.strip()):
            return "message"

    box_lines = sum(1 for ln in lines if set(ln) & _BOX_CHARS)
    syntax_lines = sum(1 for ln in lines if _SYNTAX_RE.search(ln))
    if "::=" in text or box_lines >= 2 or syntax_lines >= 2 or _PARM_RE.search(text):
        return "syntax"

    columnish = sum(1 for ln in lines if _COLUMN_RE.search(ln))
    if columnish / len(lines) >= 0.6:
        return "table"

    return "narrative"
