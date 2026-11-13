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
from typing import TYPE_CHECKING

from mainframe_rag.agent.tokenizer import estimate_tokens
from mainframe_rag.config import Settings
from mainframe_rag.regexes import DOCNO_RE, MEMBER_RE, MSG_RE, find_system_codes
from mainframe_rag.retrieve.filters import parse_query
from mainframe_rag.retrieve.rewrite import expand_query

if TYPE_CHECKING:
    from mainframe_rag.ports import Tokenizer

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


def _select_prose(lines: list[str], room: int) -> str:
    """Prose lines that fit `room`, by priority and then position: lines that
    ask something (contain `?`), then the first and the last prose line, then
    the rest from the top. A line is taken whole or not at all, except the
    very first choice, which is cut at a word when it alone is too long. The
    chosen lines are joined in their original order."""
    last = len(lines) - 1
    order = sorted(
        range(len(lines)),
        key=lambda i: (0 if "?" in lines[i] else 1 if i in (0, last) else 2, i),
    )
    chosen: set[int] = set()
    used = 0
    for i in order:
        cost = len(lines[i]) + (1 if chosen else 0)
        if used + cost <= room:
            chosen.add(i)
            used += cost
        elif not chosen:
            return _cut_at_word(lines[i], max(0, room))
        elif "?" not in lines[i] and i not in (0, last):
            break  # the remainder is a contiguous head; stop at the first miss
    return " ".join(lines[i] for i in sorted(chosen))


def derive_retrieval_query(message: str, settings: Settings) -> str:
    """The text retrieval searches for this message.

    At or under `query_max_chars` the message itself, unchanged. Above it:
    the prose lines (whitespace collapsed; question lines and the first and
    last prose line first, see `_select_prose`) followed by the identifiers
    found in the rest of the message. A message with neither prose nor
    identifiers falls back to its own head. The result never exceeds
    `retrieval_query_limit`, also after acronym expansion.
    """
    if len(message) <= settings.query_max_chars:
        return message
    limit = retrieval_query_limit(settings)
    lines = [" ".join(line.split()) for line in message.splitlines() if is_prose_line(line)]
    suffix = " ".join(_identifier_terms(message, " ".join(lines)))[:limit].rstrip()
    if not lines and not suffix:
        lines = [" ".join(message.split())]
    room = limit - (len(suffix) + 1 if suffix else 0)
    for _ in range(_FIT_ROUNDS):
        query = " ".join(part for part in (_select_prose(lines, room), suffix) if part)
        over = len(_effective(query, settings)) - limit
        if over <= 0:
            return query
        room -= over
    return suffix or _cut_at_word(lines[0], limit // 2)


def _effective(query: str, settings: Settings) -> str:
    return expand_query(query) if settings.acronym_expansion_enabled else query


def _elide(message: str, keep: int) -> tuple[str, int]:
    """Head and tail of `message` around the marker, about `keep` characters
    kept, each cut moved to a nearby line break when one exists; and the
    number of characters left out (0 when the message is returned whole)."""
    if keep >= len(message):
        return message, 0
    half = max(1, keep // 2)
    head_end, tail_start = half, len(message) - half
    slack = max(1, half // 5)
    if (newline := message.rfind("\n", head_end - slack, head_end)) > 0:
        head_end = newline
    if (newline := message.find("\n", tail_start, tail_start + slack)) > 0:
        tail_start = newline + 1
    head, tail = message[:head_end].rstrip(), message[tail_start:].lstrip()
    omitted = len(message) - len(head) - len(tail)
    return head + elision_marker(omitted) + tail, omitted


# Worst-case pricing for text no served tokenizer has counted. A byte-level
# BPE never emits more tokens than UTF-8 bytes, so one token per byte is the
# ceiling; dump text (hex, digits, mixed-case or capital runs, punctuation)
# sits close to it: digits are split one by one and random letter runs find
# few merges (measured about 0.85 tokens per character on spaced hex, 0.65 on
# base64). Only an ordinary word (letters only with no digit attached,
# lowercase, optionally capitalised, at most NATURAL_WORD_MAX letters) is
# cheaper: one token per NATURAL_WORD_CHARS characters, which random
# lowercase letters also stay under. A single space
# joins the next token and is free; a line break or a run of blanks is one.
NATURAL_WORD_CHARS = 2
NATURAL_WORD_MAX = 15
_PRICED = re.compile(r"[A-Za-z0-9]+|\n|[ \t]{2,}|[^\sA-Za-z0-9]")


def conservative_tokens(text: str) -> int:
    """Upper-bound token price of `text` (see the constants above)."""
    total = 0
    for match in _PRICED.finditer(text):
        run = match.group()
        if run[0].isascii() and run[0].isalnum():
            natural = (
                len(run) <= NATURAL_WORD_MAX
                and run.isalpha()
                and (run.islower() or run[1:].islower())
            )
            total += -(-len(run) // NATURAL_WORD_CHARS) if natural else len(run)
        elif run[0].isascii():
            total += 1
        else:
            total += len(run.encode("utf-8", "surrogatepass"))
    return total


def fit_message(
    message: str, budget_tokens: int, tokenizer: Tokenizer | None = None
) -> tuple[str, int, int]:
    """`message` within `budget_tokens`, the tokens it was priced at, and the
    number of its characters left out.

    Whole when it fits; otherwise head and tail around the visible marker,
    never a silent cut. A count the served tokenizer confirmed is used (never
    below the in-process estimate); without one, or when the count fell back
    to the estimator, the text is priced by `conservative_tokens`, because the
    estimator prices a hex word as one token. The message is cut until its
    price fits; only a budget below the marker's own price can leave it over,
    which the caller's whole-prompt verification then refuses.
    """
    budget = max(1, budget_tokens)

    def price(text: str) -> int:
        if tokenizer is not None:
            counted = tokenizer.count_tokens(text)
            if getattr(tokenizer, "remote_confirmed", False):
                return max(counted, estimate_tokens(text))
        return conservative_tokens(text)

    keep = min(len(message), budget * _MAX_CHARS_PER_TOKEN)
    text, omitted = _elide(message, keep)
    tokens = price(text)
    rounds = 0
    while tokens > budget and keep > 2:
        rounds += 1
        # Proportional first; halving after that, so the loop always ends.
        scaled = int(keep * budget / tokens * 0.95) if rounds <= _FIT_ROUNDS else keep // 2
        keep = max(2, min(scaled, keep - 1))
        text, omitted = _elide(message, keep)
        tokens = price(text)
    return text, tokens, omitted
