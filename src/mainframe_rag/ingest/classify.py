"""Chunk classification: message | syntax | table | narrative.

Message sections start with an MVS-style message ID (XXXnnnY). Syntax sections
use IBM syntax diagrams (>>-, box drawing, ::=, <parm>). Tables are column-
aligned lines. Everything else is narrative. architecture.md section 4.2.
"""

from __future__ import annotations

import re

_BOX_CHARS = set("─│┌┐└┘├┤┬┴┼━┃┏┓┗┛")
_SYNTAX_RE = re.compile(r"(::=|>>-|>>\+|<--|--\+|-\+-|--\-)")
_PARM_RE = re.compile(r"<[a-zA-Z][\w-]*>")
_COLUMN_RE = re.compile(r"\S(?:.*\S)?(?:\s{2,}\S)+")

MESSAGE_LINE_RE = re.compile(r"^\s*[A-Z]{3}\d{2,5}[A-Z]")


def classify(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "narrative"

    first = lines[0].strip()
    if MESSAGE_LINE_RE.match(first):
        return "message"

    box_lines = sum(1 for ln in lines if set(ln) & _BOX_CHARS)
    syntax_lines = sum(1 for ln in lines if _SYNTAX_RE.search(ln))
    if "::=" in text or box_lines >= 2 or syntax_lines >= 2 or _PARM_RE.search(text):
        return "syntax"

    columnish = sum(1 for ln in lines if _COLUMN_RE.search(ln))
    if lines and columnish / len(lines) >= 0.6:
        return "table"

    return "narrative"
