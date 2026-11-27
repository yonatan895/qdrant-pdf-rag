"""L2 judge prompts, call parameters, labels and cited-evidence measurement.

The L2 runner owns live execution; L4 owns repeated quality decisions.
Importing this module performs no model, storage, or application setup.
"""
from __future__ import annotations

import json
import re
from typing import Any

from mainframe_rag.ports import ChatMessage

# Evidence bound for the judge prompt: 8 chunk-capped excerpts can exceed
# 25k chars; the judge needs the claims' context, not the whole pool.
JUDGE_MAX_EVIDENCE_CHARS = 6000

CITE_PREFIX_RE = re.compile(r"^\s*\[\d+\]\s*")
JUDGE_LABELS = ("entailed", "neutral", "contradiction")
RELEVANCE_LABELS = ("relevant", "partial", "irrelevant")
JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

# Judge calls run at temp 0 with an explicit low reasoning effort. Measured
# (#305): with the server-default effort the judge burned 1200+ reasoning
# tokens on long answers and put its chain-of-thought in the content channel,
# so the JSON label never appeared and the row failed as a judge infra error.
# Low effort parses and keeps the label decision (DOC-02/VER-03: neutral both
# ways). The production answer path already sends this knob; the judge must
# too — a judge that flips with server defaults is not an instrument.
JUDGE_REASONING_EFFORT = "low"


class JudgeError(RuntimeError):
    """Judge output could not be parsed into a label — structural FAIL."""


def judge_chat(client: Any, messages: list[ChatMessage]) -> Any:
    """One judge call shape: temperature 0 + bounded reasoning effort. Every
    judge leg (faithfulness, relevance) funnels through here so the call
    parameters cannot diverge between the two labels."""
    return client.chat(messages, temperature=0.0, reasoning_effort=JUDGE_REASONING_EFFORT)


def citation_to_hit(citation: str, hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Map a validated citation string (`[n] {hit.cite}`) back to its hit by
    exact cite match. None when the hit is not in the fetched search rows —
    the caller records it rather than guessing a doc id with a regex."""
    cite = CITE_PREFIX_RE.sub("", citation)
    for h in hits:
        if h.get("cite") == cite:
            return h
    return None


def cited_doc_ids(citations: list[str], hits: list[dict[str, Any]]) -> tuple[set[str], list[str]]:
    """Doc ids behind validated citations, plus any citation that could not
    be mapped (recorded for diagnosis; never guessed)."""
    docs: set[str] = set()
    unmatched: list[str] = []
    for c in citations:
        hit = citation_to_hit(c, hits)
        if hit is None:
            unmatched.append(c)
        else:
            docs.add(str(hit.get("doc_id") or ""))
    return docs, unmatched


def _packed_cite(item: dict[str, Any]) -> str | None:
    """Cite line of a packed entry: `citation` on a SuppliedExcerpt, `cite` on
    a response hit (retrieved or NeighbourHit)."""
    raw = item.get("citation") if "citation" in item else item.get("cite")
    return raw if isinstance(raw, str) else None


def packed_citation_hits(citation: str, packed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every packed entry behind a validated citation, for rows whose response
    shows neighbour expansion ran (#1314; `answers.ResponseJoin`). Same-page
    neighbours repeat their parent's cite line, so a bare citation maps to
    every entry carrying it, as the agent and the Pipe map it
    (`capture.RowCitations.cites`). A `[n]` prefix whose prompt slot (an
    explicit `index`, else the 1-based position) carries the cite selects
    that slot alone. Never used on the sibling-pool join, which keeps
    `citation_to_hit`'s first match."""
    match = CITE_PREFIX_RE.match(citation)
    cite = citation[match.end():] if match is not None else citation
    if match is not None:
        slot = int(citation[: match.end()].strip().strip("[]"))
        if any(type(item.get("index")) is int for item in packed):
            exact = [item for item in packed
                     if type(item.get("index")) is int and item["index"] == slot
                     and _packed_cite(item) == cite]
            if exact:
                return exact
        elif 1 <= slot <= len(packed) and _packed_cite(packed[slot - 1]) == cite:
            return [packed[slot - 1]]
    return [item for item in packed if _packed_cite(item) == cite]


def packed_cited_doc_ids(
    citations: list[str], packed: list[dict[str, Any]]
) -> tuple[set[str], list[str]]:
    """`cited_doc_ids` over the packed join of an expanded response (#1314):
    the doc ids of every entry `packed_citation_hits` maps, plus the
    citations that map to none."""
    docs: set[str] = set()
    unmatched: list[str] = []
    for c in citations:
        matched = packed_citation_hits(c, packed)
        if not matched:
            unmatched.append(c)
        docs.update(str(item.get("doc_id") or "") for item in matched)
    return docs, unmatched


def _split_citation(citation: str) -> tuple[str, int | None]:
    """(cite line, `[n]` prompt slot or None) of a validated citation."""
    match = CITE_PREFIX_RE.match(citation)
    if match is None:
        return citation, None
    return citation[match.end():], int(citation[: match.end()].strip().strip("[]"))


def _expansion_added(
    cite: str, hits: list[dict[str, Any]], supplied: list[dict[str, Any]],
    pool: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Supplied excerpts under `cite` that neighbour expansion added, or
    none when the response does not account for the whole line (the rule of
    `expansion_citation_excerpts`)."""
    pooled = {h["chunk_id"] for h in pool if isinstance(h.get("chunk_id"), str)}
    retrieved = {h["chunk_id"]: h.get("doc_id") for h in hits
                 if isinstance(h.get("chunk_id"), str) and "neighbour_of" not in h}

    def added_by_expansion(hit: dict[str, Any]) -> bool:
        parent, doc_id = hit.get("neighbour_of"), hit.get("doc_id")
        return (isinstance(parent, str) and isinstance(doc_id, str) and bool(doc_id)
                and retrieved.get(parent) == doc_id)

    # Fail closed on the line (#1389 review B1): a response hit under it that
    # is neither an added neighbour nor a chunk the pool holds (a retrieved
    # hit beyond the pool's depth) could be what the answer cited.
    if any(h.get("cite") == cite and not added_by_expansion(h)
           and not (isinstance(h.get("chunk_id"), str) and h["chunk_id"] in pooled)
           for h in hits):
        return []
    added: list[dict[str, Any]] = []
    for item in supplied:
        if item.get("citation") != cite:
            continue
        index = item.get("index")
        if type(index) is not int or not 1 <= index <= len(hits):
            return []
        hit = hits[index - 1]
        if hit.get("cite") != cite or hit.get("doc_id") != item.get("doc_id"):
            return []
        if added_by_expansion(hit):
            added.append(item)
    return added


def expansion_citation_excerpts(
    citation: str, hits: list[dict[str, Any]], supplied: list[dict[str, Any]],
    pool: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The supplied excerpts behind a validated citation that neighbour
    expansion added (#1377), for harness L2/L4 on a streamed final. An
    excerpt counts only with its provenance recorded in that final: its
    prompt slot `index` is response hit `index` (packing labels the offered
    hits 1..n), that hit carries the cite line, the excerpt's `doc_id` and a
    string `neighbour_of`, and `neighbour_of` is the `chunk_id` of a
    retrieved hit (one without the key) of the same `doc_id` in the same
    response.

    The line is decided as a whole and fails closed (#1389 review B1): every
    response hit and every supplied excerpt carrying it must be such a
    neighbour or a chunk the sibling `pool` holds (by `chunk_id`). A
    retrieved hit the pool lacks under the same line (two chunks of one
    page, one beyond the pool's depth) leaves the line ungrounded, as does
    a supplied entry under it that does not map to its response hit.

    A bare citation maps to every added excerpt under the line; a `[n]`
    prefix to the added excerpt in slot `n` and to nothing else. Empty for
    anything else: a retrieved hit is never grounded here, so the
    sibling-pool join keeps deciding it."""
    cite, slot = _split_citation(citation)
    added = _expansion_added(cite, hits, supplied, pool)
    return added if slot is None else [item for item in added if item["index"] == slot]


def precision_recall(cited: set[str], gold: set[str]) -> tuple[float | None, float | None]:
    """Doc-level citation precision/recall. None denominators (no citations
    / no gold) stay None so abstain rows and zero-hit paths never dilute
    the averages."""
    if not gold:
        return None, None
    if not cited:
        return None, 0.0
    inter = len(cited & gold)
    return inter / len(cited), inter / len(gold)


def judge_messages(answer: str, evidence: str) -> list[ChatMessage]:
    """NLI-style claim-vs-excerpt prompt. The judge sees evidence text and
    the answer body — never citation markers (the validator owns those).
    Returns ChatMessage rows so the production HttpxLLMClient can send them
    unmodified (one client shape, no second serializer)."""
    return [
        ChatMessage(
            role="system",
            content=(
                "You are a strict grounding judge for a retrieval-augmented "
                "answer system. You are given manual EXCERPTS and an ANSWER "
                'produced from them. Reply with exactly one JSON object: '
                '{"label": "entailed"} when every factual claim in the ANSWER '
                'is supported by the EXCERPTS, {"label": "contradiction"} '
                'when the EXCERPTS contradict a claim, {"label": "neutral"} '
                "when the ANSWER makes claims the EXCERPTS neither support "
                "nor contradict. No other text."
            ),
        ),
        ChatMessage(role="user", content=f"EXCERPTS:\n{evidence}\n\nANSWER:\n{answer}"),
    ]


def relevance_messages(question: str, answer: str) -> list[ChatMessage]:
    """Answer-relevance prompt: does the ANSWER address the QUESTION.

    Separate from faithfulness by design (one label per prompt): relevance
    needs no excerpts, so the judge never sees manual text or citation
    markers here. Returns ChatMessage rows for the production client.
    """
    return [
        ChatMessage(
            role="system",
            content=(
                "You are a strict answer-relevance judge for a "
                "retrieval-augmented question answering system. Given a "
                "QUESTION and an ANSWER, reply with exactly one JSON object: "
                '{"label": "relevant"} when the ANSWER directly addresses the '
                'QUESTION, {"label": "partial"} when it addresses only part '
                'of the QUESTION or drifts off-topic, {"label": "irrelevant"} '
                "when it does not address the QUESTION. No other text."
            ),
        ),
        ChatMessage(role="user", content=f"QUESTION:\n{question}\n\nANSWER:\n{answer}"),
    ]


def _parse_label(text: str, labels: tuple[str, ...]) -> str:
    """Extract one label from the judge's JSON reply (tolerates fences/prose
    around the object; anything else fails closed)."""
    m = JSON_BLOCK_RE.search(text)
    if m:
        try:
            label = json.loads(m.group(0)).get("label")
        except json.JSONDecodeError:
            label = None
        if label in labels:
            return label
    raise JudgeError(f"unparseable judge reply: {text[:120]!r}")


def parse_judge_label(text: str) -> str:
    """Faithfulness label (entailed/neutral/contradiction)."""
    return _parse_label(text, JUDGE_LABELS)


def parse_relevance_label(text: str) -> str:
    """Relevance label (relevant/partial/irrelevant)."""
    return _parse_label(text, RELEVANCE_LABELS)


def evidence_for_citations(citations: list[str], hits: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Excerpt text behind the cited hits (deterministic retrieval makes the
    /v1/search rows the same pool the agent answered from), bounded for the
    judge prompt. Returns (evidence, unmapped citations)."""
    texts: list[str] = []
    unmapped: list[str] = []
    for c in citations:
        hit = citation_to_hit(c, hits)
        if hit is None:
            unmapped.append(c)
        else:
            texts.append(str(hit.get("text") or ""))
    return _bounded_evidence(texts), unmapped


def _bounded_evidence(texts: list[str]) -> str:
    evidence = "\n\n---\n\n".join(texts)
    if len(evidence) > JUDGE_MAX_EVIDENCE_CHARS:
        evidence = evidence[:JUDGE_MAX_EVIDENCE_CHARS] + "\n…[truncated for the judge]"
    return evidence


def evidence_for_expanded_citations(
    citations: list[str], pool: list[dict[str, Any]],
    hits: list[dict[str, Any]], supplied: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """`evidence_for_citations` for a row whose streamed final shows
    neighbour expansion ran (#1377): the judge reads the evidence that was
    supplied. Per citation: a `[n]` prefix naming an excerpt expansion
    added (`expansion_citation_excerpts`' rule) reads that excerpt's
    supplied text alone. Otherwise a citation in the sibling pool reads the
    pool hit's text, exactly as there, and a bare one also every added
    excerpt carrying its line (a same-page neighbour repeats its parent's
    line, and the agent returns bare lines), except the excerpt of that
    pool hit's own chunk: a pooled chunk expansion added again is read once
    (#1389 review N1). A citation outside the pool reads its added
    excerpts, and is unmapped when there are none."""
    texts: list[str] = []
    unmapped: list[str] = []
    for c in citations:
        cite, slot = _split_citation(c)
        added = _expansion_added(cite, hits, supplied, pool)
        exact = [item for item in added if item["index"] == slot]
        hit = citation_to_hit(c, pool)
        if exact:
            chosen = exact
        elif hit is not None:
            texts.append(str(hit.get("text") or ""))
            pooled_id = hit.get("chunk_id")
            chosen = [] if slot is not None else [
                item for item in added
                if not isinstance(pooled_id, str) or hits[item["index"] - 1].get("chunk_id") != pooled_id
            ]
        else:
            chosen = added if slot is None else []
            if not chosen:
                unmapped.append(c)
        texts.extend(str(item.get("text") or "") for item in chosen)
    return _bounded_evidence(texts), unmapped
