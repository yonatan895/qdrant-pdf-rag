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
- LLM rewrites (HyDE, step-back — issue #82) share the same home and the
  same bypass: gated, default-off, dense-leg-only, fail-open to the
  operator's own words.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from mainframe_rag.ports import ChatMessage
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


# ------------------------------------------------------- LLM rewrites (issue #82)
# HyDE / step-back feed the DENSE leg only; the sparse (BM25) and rerank legs
# keep the operator's own words. The bypass (traps, identifier-heavy) and the
# fallback (any LLM failure -> unrewritten query) live in the shared composer —
# one rule for every stage; the sync and async twins differ only in adapters.

_HYDE_PROMPT = (
    "Write a short excerpt from a hypothetical IBM mainframe technical manual "
    "that would contain the information needed to answer the question below. "
    "Plain manual prose, 150-200 words. No preamble, no markdown, no lists. "
    "Do not answer the question and do not restate it.\n\nQuestion: {query}"
)

_STEPBACK_PROMPT = (
    "Rewrite the question below as one shorter, more general question about "
    "the same topic — the question a person would ask before drilling into "
    "these specifics. Output only the rewritten question, no quotes, no "
    "preamble.\n\nQuestion: {query}"
)


def _stage_plan(settings: Any) -> tuple[str, ...]:
    """Order is the composition: step-back first, HyDE over its output."""
    stages: list[str] = []
    if settings.stepback_enabled:
        stages.append("stepback")
    if settings.hyde_enabled:
        stages.append("hyde")
    return tuple(stages)


def _prompt(stage: str, text: str) -> str:
    template = _STEPBACK_PROMPT if stage == "stepback" else _HYDE_PROMPT
    return template.format(query=text)


def _cap(settings: Any, stage: str) -> int:
    return settings.stepback_max_chars if stage == "stepback" else settings.hyde_max_chars


def _compose(stages: tuple[str, ...], lexical: str, complete: Callable[[str, str], str | None]) -> str:
    """One composition rule for both twins: run the planned stages in order,
    feeding each stage the previous stage's output; ANY stage failure —
    exception, empty text — falls back to the unrewritten query. Traps and
    identifier-heavy queries never reach the LLM (should_rewrite)."""
    if not stages or not should_rewrite(lexical):
        return lexical
    base = lexical
    for stage in stages:
        out = complete(stage, base)
        if out is None or not out.strip():
            return lexical
        base = out
    return base


def complete_sync(llm: Any, prompt: str, max_chars: int) -> str | None:
    """Blocking completion for the sync twin (eval/scripts). An awaitable
    result is treated as a failure: the sync path cannot await it."""
    try:
        raw = llm.chat([ChatMessage(role="user", content=prompt)], temperature=0.0)
        if inspect.isawaitable(raw):
            return None
        content = raw.content if hasattr(raw, "content") else str(raw)
        text = str(content).strip()
        return text[:max_chars] or None
    except Exception:  # noqa: BLE001 — any rewrite fault falls back to the query
        return None


async def complete_async(llm: Any, prompt: str, max_chars: int) -> str | None:
    """Awaitable completion for the async twin (production handlers)."""
    try:
        raw = llm.chat([ChatMessage(role="user", content=prompt)], temperature=0.0)
        if inspect.isawaitable(raw):
            raw = await raw
        content = raw.content if hasattr(raw, "content") else str(raw)
        text = str(content).strip()
        return text[:max_chars] or None
    except Exception:  # noqa: BLE001 — any rewrite fault falls back to the query
        return None


def dense_text_sync(settings: Any, llm: Any, lexical: str) -> str:
    """Sync twin: dense-leg embed text (the lexical query when untouched)."""
    stages = _stage_plan(settings)
    if not stages:
        return lexical

    def complete(stage: str, text: str) -> str | None:
        return complete_sync(llm, _prompt(stage, text), _cap(settings, stage))

    return _compose(stages, lexical, complete)


async def dense_text_async(settings: Any, llm: Any, lexical: str) -> str:
    """Async twin: dense-leg embed text (the lexical query when untouched)."""
    stages = _stage_plan(settings)
    if not stages:
        return lexical

    async def complete(stage: str, text: str) -> str | None:
        return await complete_async(llm, _prompt(stage, text), _cap(settings, stage))

    return await _compose_async(stages, lexical, complete)


async def _compose_async(
    stages: tuple[str, ...],
    lexical: str,
    complete: Callable[[str, str], Awaitable[str | None]],
) -> str:
    if not stages or not should_rewrite(lexical):
        return lexical
    base = lexical
    for stage in stages:
        out = await complete(stage, base)
        if out is None or not out.strip():
            return lexical
        base = out
    return base
