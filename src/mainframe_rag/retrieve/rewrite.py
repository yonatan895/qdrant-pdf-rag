"""Query rewriting: acronym expansion (issue #82 PR-08).

Single home for query-text rewriting (one rule per concept). Deterministic
and model-free: known mainframe acronyms gain their expansions inline
(`IPL` → `IPL (Initial Program Load)`), which helps both the dense leg
(semantics) and the sparse leg (expansion terms).

Deliberately narrow:

- Whole-word tokens only (`MAP` inside `MAPPING` never fires — and `MAP`
  is excluded from the glossary anyway); slashed forms (`SMP/E`, `TCP/IP`,
  `PR/SM`, `PL/I`, `Z/OSMF`) match as one token.
- Two-letter tokens expand only from an explicit allowlist (`TWO_LETTER`):
  bare two-letter matches are overwhelmingly false positives (`LE` vs
  French `le`, `SE`, `AR`, `CR`, `DR`); each allowlist member was reviewed.
- Identifier-shaped tokens never expand (a `DSN9022I` keeps its exact
  form even if a substring rang a bell) and identifier-heavy queries
  bypass rewriting entirely via `should_rewrite` (exact-code matching
  must not be diluted — the issue's core constraint). Screen-class trap
  queries bypass rewriting too: expansion must never alter the text the
  screen and refusal path reason about (issue #157).
- Ambiguous tokens are EXCLUDED from the glossary, never guessed: DSN
  (Data Set Name vs Db2 prefix), PDF (Portable Document Format), AIX
  (IBM AIX vs Alternate Index), CA, MAP, CP, SAP, PU, DR, BCP, GDS,
  DSS, MQ (not an acronym), SE. Exclusion beats wrong expansion.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from mainframe_rag.retrieve.filters import parse_query
from mainframe_rag.retrieve.screen import screen_query

ACRONYM_GLOSSARY_VERSION = "v1"

# Whole-word token with one optional / group (SMP/E, TCP/IP, PR/SM, PL/I,
# Z/OSMF). Case-insensitive: `what is ipl?` still expands, re-emitted in
# the operator's own casing.
_TOKEN_RE = re.compile(r"\b([A-Za-z0-9]+(?:/[A-Za-z0-9]+)?)\b")

# Two-letter tokens expand only from this reviewed set. Everything else
# of length 2 is left alone no matter what the glossary says. `LE` was
# reviewed OUT: bare `le` is a French word and a typo shape; `LE/370`
# and longer forms still expand (the gate is length-2 only).
TWO_LETTER: frozenset[str] = frozenset({"CF", "LU", "EE"})


@lru_cache(maxsize=1)
def _glossary() -> dict[str, str]:
    path = Path(__file__).resolve().parent / f"acronyms_{ACRONYM_GLOSSARY_VERSION}.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): str(v) for k, v in data.items()}


def should_rewrite(query: str) -> bool:
    """False for identifier-heavy queries (exact-code path stays exact) and
    for screen-class trap queries: a trap must reach the retrieval legs and
    the refusal path on the operator's own words — expansion would change
    the very text the screen and answer path reason about (issue #157).
    Enforced here so every caller inherits it; the screen runs *inside*
    this gate, not only ahead of it at call sites."""
    if screen_query(query) == "trap":
        return False
    return not parse_query(query).has_identifiers


def expand_query(query: str) -> str:
    """Append `ACRONYM (Expansion)` appositions for glossary hits. Returns
    the query unchanged when nothing fires (the common case) and ALWAYS
    unchanged for identifier-heavy queries: the bypass lives here, not only
    at call sites, so no future caller can dilute the exact-code path."""
    if not should_rewrite(query):
        return query
    glossary = _glossary()
    found: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_RE.finditer(query):
        token = match.group(1)
        key = token.upper()
        if key in seen or len(key) < 2:
            continue
        if len(key) == 2 and key not in TWO_LETTER:
            continue
        expansion = glossary.get(key)
        if expansion is None:
            continue
        seen.add(key)
        found.append(f"{token} ({expansion})")
    if not found:
        return query
    return query + " " + " ".join(found)
