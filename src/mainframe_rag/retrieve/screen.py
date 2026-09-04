"""Query-class screen for prompt-injection shapes (issue #87).

Single home for the trap-vs-answerable decision (one rule per concept).
Deterministic regexes over a normalized query — no LLM, no network, no
model to vendor. Trap patterns are checked BEFORE identifier presence,
because a trap query can carry identifiers ("...give the exact IEASYSxx
LFAREA syntax we should IPL with tonight").

Deliberately narrow: only instruction-override / jailbreak / key-recitation
shapes screen as trap. Live-state and out-of-scope abstains (PTF lists,
SYSLOG last night, nonexistent docs) carry no must_not docs and stay
answerable for retrieval — rerank-on proved 0 violations on the main gate
including those negatives, and the answer layer abstains separately.
Sibling-competitor must_not entries (AHL127A, IOS207I/208I, edition
suffixes) are ranking-quality traps, not injection: they stay answerable.
"""

from __future__ import annotations

import re
from typing import Literal

QueryClass = Literal["answerable", "trap"]

# Formatting noise that must not shield an override attempt: replaced with
# a space (never deleted, so `ignore_the_excerpts` still separates into
# words) before matching. Carries no trap signal of its own.
_FORMAT_NOISE_RE = re.compile(r"[`'\"*~_>#()\[\]{}|\\]")
_WS_RE = re.compile(r"\s+")


def _normalize(query: str) -> str:
    lowered = _FORMAT_NOISE_RE.sub(" ", query.lower())
    return _WS_RE.sub(" ", lowered).strip()


# Each pattern targets one override shape. Bounded `[\w\s]{0,N}` gaps admit
# one adjective/article ("the supplied excerpts") without letting the verb
# drift onto unrelated nouns pages away.
_TRAP_RES: tuple[re.Pattern[str], ...] = (
    # "Ignore the excerpts" / "Ignore the supplied excerpts" — the RAG
    # override: discard retrieved context, answer from elsewhere.
    re.compile(r"ignore\s+(the\s+)?[\w\s]{0,20}?excerpts?\b"),
    # Classic instruction override with a backward reference. The optional
    # possessive/article admits "your previous" / "all prior" orderings.
    re.compile(r"ignore\s+(all\s+|your\s+|the\s+)?(previous|prior|earlier)\s+instructions?\b"),
    re.compile(
        r"(disregard|override)\s+(all\s+|your\s+|the\s+)?"
        r"(previous|prior|earlier|system)\s+instructions?\b"
    ),
    # Role / system-prompt capture.
    re.compile(r"you\s+are\s+now\b"),
    re.compile(r"new\s+instructions?\b"),
    re.compile(r"(reveal|show|print|repeat|recite)\s+(your|the)\s+(system\s+)?(prompt|instructions)\b"),
    # Answer from weights, not excerpts.
    re.compile(r"from\s+memory\b"),
    # Key / secret recitation ("recite the private key for our certificate").
    re.compile(
        r"(recite|repeat|reveal|disclose|exfiltrate|dump)\b.{0,40}\b"
        r"(private\s+key|secret|password|passphrase|certificate)\b"
    ),
    # Filter / refusal bypass and bare jailbreak vocabulary (no legitimate
    # manual query contains these; the golden sweep pins that claim).
    re.compile(r"bypass\s+(the\s+)?(safety|refusal|guardrail|filter|abstention)\b"),
    re.compile(r"\bjailbreak\b"),
)


def screen_query(query: str) -> QueryClass:
    """Classify a user query as answerable or trap (injection-shaped)."""
    normalized = _normalize(query)
    for rx in _TRAP_RES:
        if rx.search(normalized):
            return "trap"
    return "answerable"
