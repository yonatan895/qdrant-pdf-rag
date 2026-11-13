"""The user's message versus the retrieval query (issue #1192).

On the answer and chat paths the user's whole message is prompt content; what
retrieval searches is derived from it here, deterministically and without a
model call. A message no longer than `query_max_chars` is its own retrieval
query, unchanged, and is never elided: those requests keep the retrieval
input, the prompt bytes and the embed-input refusal (#374) they had before
this module existed.

A longer message (a pasted dump, listing or log around a question) yields:

- a retrieval query built from the user's own prose lines plus the identifiers
  the shared extraction finds anywhere in the message, bounded by
  `query_max_chars` and by the embed-input bound (#374), so the raw paste never
  reaches the embedder or BM25 whole;
- prompt text that keeps the head and the tail of the message around one
  visible marker when the message exceeds its share of the served window.

Nothing here logs, and no error text carries, any part of the message.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from mainframe_rag.agent.tokenizer import estimate_tokens
from mainframe_rag.config import Settings
from mainframe_rag.regexes import DOCNO_RE, MEMBER_RE, MSG_RE, find_system_codes
from mainframe_rag.retrieve.filters import parse_query
from mainframe_rag.retrieve.rewrite import expand_query

# A long message may take at most this fraction of the prompt tokens left
# after the fixed instructions, caller context and the mandatory previous
# turn; the rest stays reserved for retrieved excerpts.
MESSAGE_WINDOW_SHARE = 0.5
# Identifiers appended to the derived retrieval query, across all families.
MAX_DERIVED_IDENTIFIERS = 12
# A prose line: at least this many whitespace-separated tokens, at least this
# fraction of them plain words, and not written mostly in capitals.
PROSE_MIN_TOKENS = 3
PROSE_WORD_FRACTION = 0.7
# A hex-dump line: at least this fraction of its tokens are hex words. Its
# words are identifier-shaped by accident, so it contributes no identifiers.
HEX_LINE_FRACTION = 0.5
_HEX_WORD = re.compile(r"[0-9A-Fa-f]{4,}")
# No tokenizer prices real text above this many characters per token, so the
# first cut never sends more than this to a remote count.
_MAX_CHARS_PER_TOKEN = 8
_FIT_ROUNDS = 4
_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
_EDGE_PUNCTUATION = "\"'’“”()[]{}<>.,;:!?"


def retrieval_query_limit(settings: Settings) -> int:
    """Longest derived retrieval query: `query_max_chars`, lowered to what the
    embed-input bound leaves after the dense query prefix."""
    limit = settings.query_max_chars
    if settings.embed_max_input_chars > 0:
        limit = min(limit, settings.embed_max_input_chars - len(settings.dense_query_prefix))
    return max(1, limit)


def elision_marker(omitted: int) -> str:
    """The one visible mark for message text left out of the prompt."""
    return f"\n[... {omitted} characters of the pasted message omitted to fit the model window ...]\n"


def is_prose_line(line: str) -> bool:
    """True for a line that reads as the user's own sentence, not pasted output."""
    tokens = line.split()
    if len(tokens) < PROSE_MIN_TOKENS:
        return False
    upper = sum(1 for ch in line if ch.isupper())
    if upper > sum(1 for ch in line if ch.islower()):
        return False  # mostly capitals: console output, JCL, listings
    words = sum(1 for t in tokens if _WORD.fullmatch(t.strip(_EDGE_PUNCTUATION)))
    return words >= PROSE_WORD_FRACTION * len(tokens)


def _is_hex_line(line: str) -> bool:
    tokens = line.split()
    hex_words = sum(1 for t in tokens if _HEX_WORD.fullmatch(t))
    return bool(tokens) and hex_words >= HEX_LINE_FRACTION * len(tokens)


def _first_seen(text: str, pattern: re.Pattern[str]) -> list[str]:
    seen: dict[str, None] = {}
    for match in pattern.finditer(text):
        seen.setdefault(match.group(1), None)
    return list(seen)


def _code_term(code: str) -> str:
    """A canonical system code spelled so the shared extraction reads it back."""
    if code.startswith("U") and len(code) == 5:
        return code
    if code.startswith("W") and len(code) == 4:
        return f"wait state {code[1:]}"
    return f"abend {code}"


def _identifier_terms(message: str, prose: str) -> list[str]:
    """Identifiers the prose does not already carry, by family (message ids,
    system codes, members, document numbers) and first occurrence, capped."""
    message = "\n".join(line for line in message.splitlines() if not _is_hex_line(line))
    have = parse_query(prose)
    known = {*have.message_ids, *have.system_codes, *have.members, *have.doc_ids}
    upper = message.upper()
    terms: list[str] = []
    for message_id in _first_seen(upper, MSG_RE):
        if message_id not in known:
            terms.append(message_id)
    terms.extend(_code_term(c) for c in find_system_codes(message) if c not in known)
    # Members come from the case-sensitive ingest pattern: the folded query
    # pattern would admit every `step01`-shaped token of a listing.
    terms.extend(m for m in _first_seen(message, MEMBER_RE) if m not in known)
    terms.extend(d for d in _first_seen(message, DOCNO_RE) if d not in known)
    return list(dict.fromkeys(terms))[:MAX_DERIVED_IDENTIFIERS]


def _cut_at_word(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if not text[limit].isspace() and (space := cut.rfind(" ")) > 0:
        cut = cut[:space]
    return cut.rstrip()


def derive_retrieval_query(message: str, settings: Settings) -> str:
    """The text retrieval searches for this message.

    At or under `query_max_chars` the message itself, unchanged. Above it:
    the prose lines (whitespace collapsed, in order, head kept) followed by
    the identifiers found in the rest of the message. A message with neither
    prose nor identifiers falls back to its own head. The result never
    exceeds `retrieval_query_limit`, also after acronym expansion.
    """
    if len(message) <= settings.query_max_chars:
        return message
    limit = retrieval_query_limit(settings)
    prose = " ".join(" ".join(line.split()) for line in message.splitlines() if is_prose_line(line))
    suffix = " ".join(_identifier_terms(message, prose))[:limit].rstrip()
    if not prose and not suffix:
        prose = " ".join(message.split())
    room = limit - (len(suffix) + 1 if suffix else 0)
    for _ in range(_FIT_ROUNDS):
        head = _cut_at_word(prose, max(0, room))
        query = " ".join(part for part in (head, suffix) if part)
        over = len(_effective(query, settings)) - limit
        if over <= 0:
            return query
        room -= over
    return suffix or _cut_at_word(prose, limit // 2)


def _effective(query: str, settings: Settings) -> str:
    return expand_query(query) if settings.acronym_expansion_enabled else query


def _elide(message: str, keep: int) -> str:
    """Head and tail of `message` around the marker, about `keep` characters
    kept, each cut moved to a nearby line break when one exists."""
    if keep >= len(message):
        return message
    half = max(1, keep // 2)
    head_end, tail_start = half, len(message) - half
    slack = max(1, half // 5)
    if (newline := message.rfind("\n", head_end - slack, head_end)) > 0:
        head_end = newline
    if (newline := message.find("\n", tail_start, tail_start + slack)) > 0:
        tail_start = newline + 1
    head, tail = message[:head_end].rstrip(), message[tail_start:].lstrip()
    return head + elision_marker(len(message) - len(head) - len(tail)) + tail


def fit_message(
    message: str, budget_tokens: int, count: Callable[[str], int] | None = None
) -> tuple[str, int]:
    """`message` within `budget_tokens`, and the tokens it was priced at.

    Whole when it fits; otherwise head and tail around the visible marker,
    never a silent cut. `count` is the served tokenizer when one exists; a
    count is never trusted below the in-process estimate. Bounded rounds: the
    caller's whole-prompt verification stays the final check.
    """
    budget = max(1, budget_tokens)

    def price(text: str) -> int:
        estimated = estimate_tokens(text)
        return max(estimated, count(text)) if count is not None else estimated

    keep = min(len(message), budget * _MAX_CHARS_PER_TOKEN)
    text = _elide(message, keep)
    tokens = price(text)
    for _ in range(_FIT_ROUNDS):
        if tokens <= budget:
            break
        keep = max(2, int(keep * budget / tokens * 0.95))
        text = _elide(message, keep)
        tokens = price(text)
    return text, tokens
