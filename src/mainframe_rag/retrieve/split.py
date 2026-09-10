"""Deterministic multi-path query splitting (issue #214).

Single home for comparative / diagnostic decomposition (one rule per concept).
No LLM, no network, no model to vendor. Splitting never alters filters,
identifiers, or the returned query_kind on the operator's original query —
sub-queries feed the embed + prefetch legs only.

Firing rules (in order, first match wins):

- ``trap`` (screen) → single path, always. Expansion must never alter the
  text the screen and refusal path reason about.
- Comparative text-split → two paths. NL queries only (identifier-heavy
  queries bypass: the exact-code path stays exact). Markers carry explicit
  comparison intent: ``versus`` / ``vs`` / ``difference(s) between`` (+
  paired ``and``) / ``compare``-family with two entity-shaped sides /
  narrow ``X or Y`` / narrow ``X and Y`` (both sides single entity-shaped
  tokens plus a compare word — bare ``or``/``and`` in diagnostics must not
  shred the query). Slash pairs (``A/B``) deliberately do NOT split: the
  slash is ambiguous (``DSALIMIT/EDSALIMIT: which values`` means joint
  "both", not "versus") and measured negative on holdout. First marker
  only: three-entity queries still cap at two retrievals. Sub-queries are
  built by *removal* (original minus the marker minus the other entity),
  so shared context is verbatim, never reconstructed.
- Diagnostic dual-path → two paths: symptom leg (original query,
  identifier weights + filter) + cause leg (identifiers stripped, NL
  weights, product/version-only filter). Identifier queries only, gated
  on diagnostic-signal words, excluding pure factoids
  (``what does X mean``). If the stripped leg is too short, single path.

``split_query`` returns ``(paths, mode)`` with ``mode`` in
``{"single", "comparative", "diagnostic"}`` for the bounded trace attrs.
"""

from __future__ import annotations

import re

from mainframe_rag.regexes import DOCNO_RE, MEMBER_RE, MSG_RE
from mainframe_rag.retrieve.filters import MEMBER_QUERY_RE, parse_query
from mainframe_rag.retrieve.screen import screen_query

SPLIT_MODES: tuple[str, str, str] = ("single", "comparative", "diagnostic")

# Comparison words stripped from sub-queries (edges only — content words
# like "documented" stay: they are retrieval signal, not comparison glue).
_COMPARE_WORDS = frozenset({"compare", "compares", "compared", "comparing", "comparison", "versus", "vs", "difference", "differences", "between"})

# Markers, most-specific first. Case-insensitive; ``\\b`` tolerates markdown
# wrapping (``**versus**``, ````vs` ```, ``> versus``) without normalization.
_VERSUS_RE = re.compile(r"\bversus\b|\bvs\.?\b", re.IGNORECASE)
_DIFF_BETWEEN_RE = re.compile(r"\bdifferences?\s+between\b", re.IGNORECASE)
_COMPARE_RE = re.compile(r"\bcompar(?:e|es|ed|ing|ison)\b", re.IGNORECASE)
_BETWEEN_AND_RE = re.compile(r"\bbetween\b(.+?)\band\b", re.IGNORECASE | re.DOTALL)

# An entity-shaped token is decided by _is_entity_token (majority
# uppercase/digits); slash forms (SMP/E, TCP/IP) are single entities
# handled by rewrite.py — split.py never slash-splits (see docstring).
# "word number" entities (level 1, TYPE 2): adjacent pair, number bare.
_WORD_NUM_RE = re.compile(r"^([A-Za-z]+)\s+(\d+)$")

# Diagnostic signal stems (whole-word): failure/recovery vocabulary that
# marks an identifier query as diagnostic rather than factoid.
_DIAGNOSTIC_SIGNAL_RE = re.compile(
    r"\b(abend|capacity|cascad\w*|caus\w*|contention|diagnos\w*|drift\w*|dump\w*|"
    r"error\w*|exhaust\w*|fail\w*|loop\w*|outage\w*|overflow\w*|pend\w*|"
    r"recover\w*|shortage\w*|spill\w*|stuck)\b",
    re.IGNORECASE,
)
# Pure factoids stay single-path even with an accidental signal word.
_FACTOID_RE = re.compile(
    r"^\s*what\s+(does\b.*\bmeans?\b|is\b[^?]*|are\b[^?]*)\s*\??\s*$",
    re.IGNORECASE | re.DOTALL,
)

# Leg floors: a sub-query shorter than this is noise, not a path.
# Single letters (``A``/``B`` test shapes) never qualify; short entity
# codes (``MXT``) do — entity detection already rejected junk.
_MIN_PATH_CHARS = 3
_MIN_STRIPPED_CHARS = 20
_MIN_STRIPPED_WORDS = 3


def _strip_token(token: str) -> str:
    return token.strip("\"'`*~_#>()[]{}|\\.,:;?!")


def _is_entity_token(token: str) -> bool:
    """Entity-shaped: len>=2 with >= half uppercase/digits (JES2, MXT)."""
    core = _strip_token(token)
    if len(core) < 2:
        return False
    upper = sum(1 for c in core if c.isupper() or c.isdigit())
    return upper / len(core) >= 0.5


def _clean_edge(words: list[str], *, from_left: bool) -> list[str]:
    """Drop leading/trailing comparison glue words (compare/versus/...) and
    pure-formatting tokens (``>``, ``**``) — neither carries retrieval
    signal (BM25 needs [A-Za-z0-9]{2,}). Content words always stay."""
    out = list(words)
    while out:
        tok = out[0 if from_left else -1]
        core = _strip_token(tok)
        if not core or core.lower() in _COMPARE_WORDS:
            out.pop(0 if from_left else -1)
        else:
            break
    return out


_ARTICLES = frozenset({"the", "a", "an"})

# Question words are shared interrogative context, never an entity: a side
# whose only adjacent token is one of these (``versus what?``) has no real
# entity to separate, so the query stays single-path.
_QUESTION_WORDS = frozenset(
    {
        "what", "which", "when", "how", "why", "where", "who", "whom",
        "whose", "is", "are", "was", "were", "does", "do", "did", "each",
        "it", "this", "that", "these", "those",
    }
)


def _entity_span(words: list[str], *, side: str) -> tuple[int, int] | None:
    """Span (start, end) of the entity adjacent to the marker, or None when
    the side has no separable entity (pure punctuation, question words,
    lone comparison glue — the query stays single-path).

    ``word number`` pairs first (level 1); a lone entity-shaped token
    (JES2, MXT) stays single so shared context (RACF in ``RACF PERMIT``)
    is never stripped from the other leg; otherwise the adjacent word
    plus one neighbor (``service classes``, ``WLM goal-mode``) — removal
    then separates instead of duplicating. Articles at the marker edge
    (``versus the SET``) are glue and stay shared.
    """
    if not words:
        return None

    def _span_is_glue(span: tuple[int, int]) -> bool:
        return all(
            not _strip_token(w) or _strip_token(w).lower() in _COMPARE_WORDS
            for w in words[span[0]: span[1]]
        )

    def _finish(span: tuple[int, int]) -> tuple[int, int] | None:
        if _span_is_glue(span):
            return None
        if span[1] - span[0] == 1:
            core = _strip_token(words[span[0]]).lower()
            if core in _QUESTION_WORDS or core in _ARTICLES:
                return None
        return span

    if side == "left":
        idx = len(words) - 1
        if not _strip_token(words[idx]):
            return None
        if _strip_token(words[idx]).lower() in _ARTICLES and idx > 0:
            idx -= 1
        if idx >= 1 and _WORD_NUM_RE.match(
            _strip_token(words[idx - 1]) + " " + _strip_token(words[idx])
        ):
            return _finish((idx - 1, idx + 1))
        if _is_entity_token(words[idx]):
            return _finish((idx, idx + 1))
        if idx >= 1:
            return _finish((idx - 1, idx + 1))
        return _finish((idx, idx + 1))
    idx = 0
    if not _strip_token(words[idx]):
        return None
    if _strip_token(words[idx]).lower() in _ARTICLES and len(words) > 1:
        idx += 1
    if idx + 1 < len(words) and _WORD_NUM_RE.match(
        _strip_token(words[idx]) + " " + _strip_token(words[idx + 1])
    ):
        return _finish((idx, idx + 2))
    if _is_entity_token(words[idx]):
        return _finish((idx, idx + 1))
    if idx + 1 < len(words):
        return _finish((idx, idx + 2))
    return _finish((idx, idx + 1))


def _build_removal_subs(
    query: str, marker: tuple[int, int], left_ent: tuple[int, int] | None, right_ent: tuple[int, int] | None
) -> list[str] | None:
    """Removal-view construction: each sub keeps the full original minus
    the marker span minus the other entity span. Shared context verbatim."""
    left_part = query[: marker[0]]
    right_part = query[marker[1]:]
    left_words = left_part.split()
    right_words = right_part.split()
    if left_ent is None or right_ent is None:
        return None
    # Map word-index spans back to char spans for exact removal.
    l_off = 0
    l_spans: list[tuple[int, int]] = []
    for w in left_words:
        i = left_part.find(w, l_off)
        l_spans.append((i, i + len(w)))
        l_off = i + len(w)
    r_base = marker[1]
    r_off = 0
    r_spans: list[tuple[int, int]] = []
    for w in right_words:
        i = right_part.find(w, r_off)
        r_spans.append((r_base + i, r_base + i + len(w)))
        r_off = i + len(w)
    le = (l_spans[left_ent[0]][0], l_spans[left_ent[1] - 1][1])
    re_ = (r_spans[right_ent[0]][0], r_spans[right_ent[1] - 1][1])

    def _drop(spans: list[tuple[int, int]]) -> str:
        cuts = sorted([marker, *spans])
        out: list[str] = []
        pos = 0
        for s, e in cuts:
            out.append(query[pos:s])
            pos = e
        out.append(query[pos:])
        text = _clean_edge(" ".join(" ".join(out).split()).split(), from_left=True)
        text = _clean_edge(text, from_left=False)
        return " ".join(text)

    sub1 = _drop([re_])
    sub2 = _drop([le])
    if (
        not sub1
        or not sub2
        or len(sub1) < _MIN_PATH_CHARS
        or len(sub2) < _MIN_PATH_CHARS
        or sub1.lower() == sub2.lower()
    ):
        return None
    return [sub1, sub2]


def _split_top_placed(query: str, marker: tuple[int, int]) -> list[str] | None:
    """Leading marker (``Versus JES3, JES2 spool handling ...``): the left
    side is empty, so entities come from the right — head before the first
    comma/colon versus the head entity stripped from the shared tail."""
    right = query[marker[1]:]
    for sep in (",", ":"):
        if sep not in right:
            continue
        head, _, tail = right.partition(sep)
        head_words = head.split()
        tail_words = tail.split()
        if not head_words or not tail_words:
            continue
        ent1 = _entity_span(head_words, side="right")
        ent2 = _entity_span(tail_words, side="right")
        if ent1 is None or ent2 is None:
            continue
        # Char spans within the right part (offset by marker end).
        base = marker[1]
        h_off = 0
        h_spans: list[tuple[int, int]] = []
        for w in head_words:
            i = right.find(w, h_off)
            h_spans.append((base + i, base + i + len(w)))
            h_off = i + len(w)
        t_base = base + right.find(tail)
        t_off = 0
        t_spans: list[tuple[int, int]] = []
        for w in tail_words:
            i = tail.find(w, t_off)
            t_spans.append((t_base + i, t_base + i + len(w)))
            t_off = i + len(w)
        e1 = (h_spans[ent1[0]][0], h_spans[ent1[1] - 1][1])
        e2 = (t_spans[ent2[0]][0], t_spans[ent2[1] - 1][1])
        shared = (query[: e1[0]] + query[e1[1]: e2[0]] + query[e2[1]:]).strip()
        shared = " ".join(_clean_edge(_clean_edge(shared.split(), from_left=True), from_left=False))
        ent1_text = query[e1[0]: e1[1]]
        ent2_text = query[e2[0]: e2[1]]
        sub1 = " ".join(_clean_edge(f"{ent1_text} {shared}".split(), from_left=True))
        sub2 = " ".join(_clean_edge(f"{ent2_text} {shared}".split(), from_left=True))
        sub1 = " ".join(_clean_edge(sub1.split(), from_left=False))
        sub2 = " ".join(_clean_edge(sub2.split(), from_left=False))
        if (
            len(sub1) >= _MIN_PATH_CHARS
            and len(sub2) >= _MIN_PATH_CHARS
            and sub1.lower() != sub2.lower()
        ):
            return [sub1, sub2]
        return None
    return None


def _split_on_marker(query: str, marker: tuple[int, int]) -> list[str] | None:
    # Raw word lists throughout: entity spans index these exact lists, and
    # _build_removal_subs strips comparison glue from the final strings.
    # Pre-cleaning here would misalign the spans.
    left_words = query[: marker[0]].split()
    right_words = query[marker[1]:].split()
    if not left_words or not right_words:
        return None
    return _build_removal_subs(
        query,
        marker,
        _entity_span(left_words, side="left"),
        _entity_span(right_words, side="right"),
    )


def _comparative_paths(query: str) -> list[str] | None:
    """Two retrieval paths for comparative markers, else None."""
    m = _VERSUS_RE.search(query)
    if m:
        if not query[: m.start()].split():
            # Top-placed marker ("Versus JES3, JES2 ..."): entities come
            # from the right side; no left entity exists to remove.
            return _split_top_placed(query, (m.start(), m.end()))
        return _split_on_marker(query, (m.start(), m.end()))
    m = _DIFF_BETWEEN_RE.search(query)
    if m:
        # Pair with the "and" after "difference(s) between ... and ...".
        tail = query[m.end():]
        am = re.search(r"\band\b", tail, re.IGNORECASE)
        if am:
            base = m.end()
            return _split_on_marker(query, (base + am.start(), base + am.end()))
        return None
    m = _BETWEEN_AND_RE.search(query)
    if m and _COMPARE_RE.search(query):
        # "trade-offs between A and B" with a compare word present.
        and_m = re.search(r"\band\b", m.group(1), re.IGNORECASE)
        if and_m:
            base = m.start(1)
            return _split_on_marker(query, (base + and_m.start(), base + and_m.end()))
        return None
    # Narrow "X and Y": single entity-shaped tokens both sides + compare word.
    if _COMPARE_RE.search(query):
        for am in re.finditer(r"\band\b", query, re.IGNORECASE):
            left = query[: am.start()].split()
            right = query[am.end():].split()
            if not left or not right:
                continue
            if _is_entity_token(left[-1]) and _is_entity_token(right[0]):
                subs = _split_on_marker(query, (am.start(), am.end()))
                if subs:
                    return subs
    # Narrow "X or Y": both sides single entity-shaped tokens + compare word.
    if _COMPARE_RE.search(query):
        for om in re.finditer(r"\bor\b", query, re.IGNORECASE):
            left = query[: om.start()].split()
            right = query[om.end():].split()
            if not left or not right:
                continue
            if _is_entity_token(left[-1]) and _is_entity_token(right[0]):
                subs = _split_on_marker(query, (om.start(), om.end()))
                if subs:
                    return subs
    return None


def _identifier_spans(query: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for rx in (DOCNO_RE, MSG_RE, MEMBER_RE):
        for m in rx.finditer(query):
            spans.append((m.start(), m.end()))
    # Also catch lowercase-typed codes: parse_query unions the uppercased
    # copy, so match those spans too (word-char class is case-stable).
    # Members likewise via the query-side pattern: a lowercase-typed
    # member (issue #133) strips at the same span its folded form
    # filters on.
    for rx in (DOCNO_RE, MSG_RE):
        for m in rx.finditer(query.upper()):
            spans.append((m.start(), m.end()))
    for m in MEMBER_QUERY_RE.finditer(query):
        spans.append((m.start(), m.end()))
    return sorted(spans)


def strip_identifiers(query: str) -> str:
    """Original query minus identifier token spans, whitespace-collapsed."""
    spans = _identifier_spans(query)
    if not spans:
        return " ".join(query.split())
    out: list[str] = []
    pos = 0
    for s, e in spans:
        out.append(query[pos:s])
        pos = max(pos, e)
    out.append(query[pos:])
    return " ".join(" ".join(out).split())


def split_query(
    query: str,
    *,
    comparative_enabled: bool,
    diagnostic_enabled: bool,
) -> tuple[list[str], str]:
    """Decompose into 1–2 retrieval paths. Gated entirely by flags +
    query shape; trap and bypass rules live here so both twins inherit
    them from one call (one rule per concept)."""
    if not query or not query.strip():
        return ([query], "single")
    if screen_query(query) == "trap":
        return ([query], "single")
    identifiers = parse_query(query)
    if comparative_enabled and not identifiers.has_identifiers:
        subs = _comparative_paths(query)
        if subs:
            return (subs, "comparative")
    if (
        diagnostic_enabled
        and identifiers.has_identifiers
        and _DIAGNOSTIC_SIGNAL_RE.search(query)
        and not _FACTOID_RE.match(query)
    ):
        stripped = strip_identifiers(query)
        words = stripped.split()
        if len(stripped) >= _MIN_STRIPPED_CHARS and len(words) >= _MIN_STRIPPED_WORDS:
            return ([query, stripped], "diagnostic")
    return ([query], "single")
