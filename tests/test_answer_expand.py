"""Neighbour expansion on the answer path (issue #1314): after retrieval (and
candidate selection), a hit's reading-order neighbours within two pages are
appended as separate hits. Stored points are synthetic; every expected list
is written out by label ("heading#ordinal"), never recomputed."""

from __future__ import annotations

import asyncio
import logging

import pytest

from mainframe_rag.agent.answer_core import (
    AnswerCoreDeps,
    AnswerCoreInput,
    SearchPlan,
    execute_answer_core,
    retrieve_for_answer,
)
from mainframe_rag.agent.core_ports import RetrievalResult
from mainframe_rag.agent.model_adapter import ModelAdapter
from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import make_chunk_id
from mainframe_rag.ports import ChatResult, TokenUsage
from mainframe_rag.retrieve.query import NeighbourHit, SearchHit, hit_from_payload
from tests.fakes import StoredChunksFake

REV = "rev-a"
PLAIN_Q = "How do I restart the widget service?"
IDENTIFIER_Q = "IEA500I rejected"
CANARY = "storage-secret-detail"
TIMINGS = {"embed_ms": 1, "qdrant_ms": 2}

# The generated manual of the real-chunker tests below, as the chunker cuts
# it: (section, page_start, ordinal) in output order.
_LP = "Long procedure"
_MANUAL_SHAPE = [
    ("Widget guide", 0, 0), (_LP, 1, 0), (_LP, 1, 1), (_LP, 2, 2), (_LP, 3, 3), (_LP, 3, 4),
    ("WID101E", 4, 0), ("WID102E", 4, 0), ("WID103E", 4, 0), ("WID104E", 5, 0),
]
_MANUAL_CASES = [
    pytest.param((_LP, 2, 2), [(_LP, 1, 0), (_LP, 1, 1), (_LP, 3, 3), (_LP, 3, 4)], 1,
                 id="ordinals-past-zero-across-pages-then-stops-at-the-shared-page"),
    pytest.param(("Widget guide", 0, 0), [(_LP, 1, 0), (_LP, 1, 1), (_LP, 2, 2)], 0,
                 id="into-the-next-section-at-its-first-chunk"),
    pytest.param((_LP, 3, 4), [(_LP, 1, 0), (_LP, 1, 1), (_LP, 2, 2), (_LP, 3, 3)], 1,
                 id="backwards-through-the-section"),
    pytest.param(("WID102E", 4, 0), [], 2, id="three-sections-on-one-page-are-refused"),
    pytest.param(("WID104E", 5, 0), [], 1, id="no-walk-back-into-the-shared-page"),
]
# Summed over every chunk as the hit at a per-hit cap of 8: 3 + 5 + 5 + 5 + 4 + 4.
_MANUAL_NEIGHBOURS_FOUND = 26


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None, llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model", embed_mode="hash", allow_hash_mode=True,
        **{"answer_expand_enabled": True, **overrides},
    )


class Corpus:
    """Stored points of synthetic manuals. A section is (heading, chunks) and
    a chunk (page_start, page_end) or (page_start, page_end, ordinal); the
    ordinal defaults to the chunk's position, as the chunker assigns it. A
    chunk's label and stored text are "heading#ordinal" ("@page" is added
    when a section repeats an ordinal)."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict] = {}
        self.ids: dict[str, str] = {}

    def add(self, sections, *, rev=REV, product="z/OS", version="9.9", scope=None) -> Corpus:
        for heading, chunks in sections:
            for position, chunk in enumerate(chunks):
                start, end, ordinal = (*chunk, position)[:3]
                label = f"{heading}#{ordinal}"
                if label in self.ids:
                    label += f"@{start}"
                chunk_id = make_chunk_id(rev, heading, start, ordinal)
                self.ids[label] = chunk_id
                self.payloads[chunk_id] = {
                    "doc_id": "SYN-0001-00", "title": "Synthetic Reference", "source_rev": rev,
                    "heading_path": heading, "page_start": start, "page_end": end,
                    "page_label": str(start + 1), "chunk_type": "narrative", "text": label,
                    "product": product, "version": version, "message_ids": [],
                    **(scope or {}).get(label, {}),
                }
        return self

    def hit(self, label: str, score: float = 0.5) -> SearchHit:
        return hit_from_payload(self.ids[label], self.payloads[self.ids[label]], score)


class _Search:
    def __init__(self, hits: list[SearchHit]):
        self.hits = hits

    async def __call__(self, query, *, product, version, settings, limit=None):
        return RetrievalResult(list(self.hits), "nl", dict(TIMINGS))


class _AnswerLLM:
    def __init__(self) -> None:
        self.calls: list[list] = []

    def chat(self, messages, reasoning_effort=None, temperature=None):
        self.calls.append(messages)
        return ChatResult(content="An answer [1].", finish_reason="stop", usage=TokenUsage())


def _root():
    from opentelemetry.sdk.trace import TracerProvider

    return TracerProvider().get_tracer("test").start_span("root")


def _expand_attributes(root) -> dict:
    return {k: v for k, v in dict(root.attributes).items() if k.startswith("rag.expand")}


async def _expand(corpus: Corpus, labels: list[str], reader=None, *, query=PLAIN_Q, anchor=None,
                  product=None, version=None, **overrides):
    """Run the answer path's retrieval stages over hits 'labels' (scores
    descend with rank); returns (retrieved, result hits, root attributes, reader)."""
    retrieved = [corpus.hit(label, 0.9 - 0.1 * rank) for rank, label in enumerate(labels)]
    reader = StoredChunksFake(corpus.payloads) if reader is None else reader
    deps = AnswerCoreDeps(settings=_settings(**overrides), llm=ModelAdapter(_AnswerLLM()),
                          retrieve=_Search(retrieved), neighbours=reader)
    root = _root()
    plan = SearchPlan("literal", query) if anchor is None else SearchPlan("anchored", query, anchor)
    found = await retrieve_for_answer(
        plan, AnswerCoreInput(query=query, product=product, version=version), deps, root)
    return retrieved, found, _expand_attributes(root), reader


def _texts(hits) -> list[str]:
    return [hit.text for hit in hits]


_ONE_PAGE = [("A", [(3, 3)] * 5)]
_SPANNING = [("A", [(3, 3), (3, 4), (4, 4), (5, 5), (6, 6)])]
_CONTINUING_THEN_NEW = [("A", [(2, 2), (3, 3)]), ("B", [(3, 3), (4, 4)])]
_CONFINED_THEN_EXTENDING = [("A", [(2, 2)]), ("B", [(3, 3)]), ("C", [(3, 3), (4, 4)])]
_TWO_CONFINED = [("A", [(2, 2), (2, 2)]), ("B", [(3, 3)]), ("C", [(3, 3)]), ("D", [(4, 4)])]
_TWO_STARTERS_AT_THE_EDGE = [("A", [(1, 1), (1, 1)]), ("B", [(3, 3)]), ("C", [(3, 3), (4, 4)])]
_CONTINUING_AT_THE_EDGE = [("A", [(1, 1), (2, 2), (3, 3)]), ("B", [(3, 3)])]
_REPEATED = [("A", [(3, 3, 0), (3, 3, 1), (4, 4, 1)])]
_GAP = [("A", [(3, 3, 0), (3, 3, 1), (3, 3, 3)])]
_NOT_FROM_ZERO = [("A", [(2, 2), (2, 2)]), ("B", [(3, 3, 1), (3, 3, 2)])]


@pytest.mark.anyio
@pytest.mark.parametrize("sections,hit,overrides,neighbours,stopped", [
    pytest.param(_ONE_PAGE, "A#2", {}, ["A#0", "A#1", "A#3", "A#4"], 0,
                 id="several-chunks-sharing-a-page-in-ordinal-order"),
    pytest.param(_SPANNING, "A#0", {}, ["A#1", "A#2", "A#3"], 0,
                 id="a-section-spanning-pages-stays-inside-two-pages"),
    pytest.param(_SPANNING, "A#4", {}, ["A#2", "A#3"], 0, id="backwards-across-pages"),
    pytest.param(_CONTINUING_THEN_NEW, "A#1", {}, ["A#0", "B#0", "B#1"], 0,
                 id="a-continuing-section-precedes-the-one-starting-on-its-page"),
    pytest.param(_CONTINUING_THEN_NEW, "B#0", {}, ["A#0", "A#1", "B#1"], 0,
                 id="back-into-the-continuing-section"),
    pytest.param(_CONFINED_THEN_EXTENDING, "B#0", {}, ["A#0", "C#0", "C#1"], 0,
                 id="a-one-page-section-precedes-the-one-that-runs-on"),
    pytest.param(_TWO_CONFINED, "B#0", {}, [], 2, id="two-one-page-sections-are-unorderable"),
    pytest.param(_TWO_CONFINED, "A#0", {}, ["A#1"], 1,
                 id="expansion-stops-before-an-unorderable-page"),
    pytest.param(_TWO_CONFINED, "D#0", {}, [], 1, id="and-does-not-walk-back-into-one"),
    pytest.param(_TWO_STARTERS_AT_THE_EDGE, "A#0", {}, ["A#1"], 1,
                 id="a-section-on-the-windows-last-page-is-not-provably-confined"),
    pytest.param(_CONTINUING_AT_THE_EDGE, "A#0", {}, ["A#1", "A#2", "B#0"], 0,
                 id="one-section-starting-after-a-continuing-one-needs-no-proof-of-its-end"),
    pytest.param(_REPEATED, "A#0", {}, [], 2, id="a-repeated-ordinal-orders-nothing"),
    pytest.param(_GAP, "A#1", {}, ["A#0"], 1, id="a-missing-ordinal-stops-that-direction"),
    pytest.param(_NOT_FROM_ZERO, "A#1", {}, ["A#0"], 1,
                 id="a-next-section-that-does-not-start-at-zero-is-not-entered"),
    pytest.param([("A", [(3, 3)] * 9)], "A#4", {"answer_expand_per_hit": 2}, ["A#3", "A#5"], 0,
                 id="per-hit-cap-keeps-the-nearest-forward-first"),
    pytest.param([("A", [(3, 3)] * 9)], "A#4", {"answer_expand_per_hit": 3},
                 ["A#3", "A#5", "A#6"], 0, id="an-odd-cap-takes-the-extra-one-forward"),
    pytest.param([("A", [(3, 3)] * 70)], "A#4", {}, [], 2,
                 id="a-truncated-scroll-expands-nothing"),
    pytest.param([("A", [(3, 3)])], "A#0", {}, [], 0, id="a-hit-with-no-neighbour"),
])
async def test_a_hits_reading_run_is_appended_in_reading_order(
    sections, hit, overrides, neighbours, stopped
):
    corpus = Corpus().add(sections)
    retrieved, found, attributes, _ = await _expand(corpus, [hit], **overrides)
    assert found.hits[:1] == retrieved
    assert _texts(found.hits[1:]) == neighbours
    assert attributes == {
        "rag.expand_mode": "expanded", "rag.expand_added": len(neighbours),
        "rag.expand_hits": 1 if neighbours else 0, "rag.expand_stopped": stopped,
        "rag.expand_budget_cut": 0,
    }


@pytest.mark.anyio
async def test_a_neighbour_is_its_own_hit_built_from_the_stored_payload():
    """Own chunk id, cite, text and page span from the stored point, through
    retrieval's payload conversion; the parent's score and the parent's id."""
    corpus = Corpus().add(_SPANNING)
    retrieved, found, _, _ = await _expand(corpus, ["A#0"], answer_expand_per_hit=1)
    [parent], [neighbour] = retrieved, found.hits[1:]
    assert isinstance(neighbour, NeighbourHit)
    assert neighbour.model_dump() == {
        **corpus.hit("A#1", parent.score).model_dump(), "neighbour_of": parent.chunk_id,
    }
    assert (neighbour.chunk_id, neighbour.text, neighbour.score) == (corpus.ids["A#1"], "A#1", 0.9)
    assert neighbour.cite == "SYN-0001-00 Synthetic Reference, A, p. 4"
    assert (neighbour.page_start, neighbour.page_end) == (3, 4)
    assert "neighbour_of" not in parent.model_dump()


@pytest.mark.anyio
async def test_the_lookup_is_one_identity_read_and_one_window_scroll_per_hit():
    """One retrieve of the hit ids for identity fields only (no text), then
    one scroll per hit on source_rev and page_start in [p-2, p+2] with a
    bounded limit, all on the bound collection."""
    corpus = Corpus().add([("A", [(3, 3), (3, 3)])]).add([("B", [(0, 0), (1, 1)])], rev="rev-b")
    retrieved, _, _, reader = await _expand(corpus, ["A#0", "B#1"])
    lookup, *scrolls = reader.calls
    assert lookup == {"op": "retrieve", "collection": _settings().qdrant_collection,
                      "ids": [hit.chunk_id for hit in retrieved],
                      "with_payload": ["source_rev", "heading_path", "page_start", "page_end"]}
    assert [(c["op"], c["collection"], c["limit"], c["with_payload"]) for c in scrolls] == [
        ("scroll", _settings().qdrant_collection, 65, True)] * 2
    assert sorted(
        [(cond.key, cond.match.value if cond.match else (cond.range.gte, cond.range.lte))
         for cond in call["filter"].must]
        for call in scrolls
    ) == [[("source_rev", "rev-a"), ("page_start", (1, 5))],
          [("source_rev", "rev-b"), ("page_start", (0, 3))]]


@pytest.mark.anyio
@pytest.mark.parametrize("budget,per_hit,neighbours,cut", [
    pytest.param(16, 4, ["X#1", "X#2", "X#3", "Y#1", "Y#2", "Y#3"], 0, id="budget-not-binding"),
    pytest.param(3, 4, ["X#1", "X#2", "Y#1"], 3, id="farthest-of-the-lowest-ranked-go-first"),
    pytest.param(1, 4, ["X#1"], 5, id="one-slot-goes-to-the-top-hit"),
    pytest.param(4, 2, ["X#1", "X#2", "Y#1", "Y#2"], 0, id="per-hit-cap-under-the-budget"),
])
async def test_the_budget_drops_the_farthest_neighbours_of_the_lowest_ranked_hits_first(
    budget, per_hit, neighbours, cut
):
    """Slots go by distance from the hit first and by the hit's rank second
    (nearest neighbour of every hit, then the second nearest, ...), which is
    not "fill the top hit first" (that would pack X#1 X#2 X#3 at 3). The
    packed order is still parent rank, then reading order."""
    corpus = Corpus().add([("X", [(3, 3)] * 4)]).add([("Y", [(3, 3)] * 4)], rev="rev-b")
    retrieved, found, attributes, _ = await _expand(
        corpus, ["X#0", "Y#0"], answer_expand_budget=budget, answer_expand_per_hit=per_hit)
    assert found.hits[:2] == retrieved
    assert _texts(found.hits[2:]) == neighbours
    assert [hit.neighbour_of for hit in found.hits[2:]] == [
        corpus.ids[f"{text[0]}#0"] for text in neighbours]
    assert [hit.score for hit in found.hits[2:]] == [
        retrieved["XY".index(text[0])].score for text in neighbours]
    assert attributes["rag.expand_budget_cut"] == cut
    assert attributes["rag.expand_hits"] == len({text[0] for text in neighbours})


@pytest.mark.anyio
@pytest.mark.parametrize("hits,neighbours", [
    pytest.param(["A#1", "A#2"], [("A#0", "A#1"), ("A#3", "A#2")],
                 id="adjacent-hits-share-their-run"),
    pytest.param(["A#0", "A#3"], [("A#1", "A#0"), ("A#2", "A#3")],
                 id="a-chunk-between-two-hits-goes-to-whoever-is-nearer-then-higher-ranked"),
])
async def test_a_chunk_is_packed_once(hits, neighbours):
    """A neighbour that is already a retrieved hit, or already another hit's
    neighbour, is not added again; it still counts as a step of the walk."""
    corpus = Corpus().add([("A", [(3, 3)] * 4)])
    retrieved, found, attributes, reader = await _expand(corpus, hits)
    assert found.hits[:2] == retrieved
    assert [(hit.text, hit.neighbour_of) for hit in found.hits[2:]] == [
        (text, corpus.ids[parent]) for text, parent in neighbours]
    assert len({hit.chunk_id for hit in found.hits}) == len(found.hits) == 4
    assert [call["op"] for call in reader.calls] == ["retrieve", "scroll"]  # one shared window
    assert attributes["rag.expand_budget_cut"] == 0


@pytest.mark.anyio
async def test_a_chunk_already_packed_still_counts_toward_the_per_hit_cap():
    """At a per-hit cap of 1, A#1's one neighbour is the chunk after it, A#2,
    which is already a retrieved hit: A#1 gains nothing, and the chunk before
    it (A#0) is not taken in its place."""
    corpus = Corpus().add([("A", [(3, 3)] * 4)])
    retrieved, found, attributes, _ = await _expand(
        corpus, ["A#1", "A#2"], answer_expand_per_hit=1)
    assert found.hits[:2] == retrieved
    assert [(hit.text, hit.neighbour_of) for hit in found.hits[2:]] == [("A#3", corpus.ids["A#2"])]
    assert attributes["rag.expand_added"] == 1
    assert attributes["rag.expand_hits"] == 1
    assert attributes["rag.expand_budget_cut"] == 0


def _carrying(**ids_by_label) -> dict:
    return {label: {"message_ids": ids} for label, ids in ids_by_label.items()}


# One message section on one page: the entry before, the hit's entry in two
# chunks (M#1 and its continuation M#2), the entry after, and a later chunk
# that names the hit's id again.
_ENTRIES = _carrying(**{"M#0": ["IEA499I"], "M#1": ["IEA500I"], "M#2": ["IEA500I"],
                        "M#3": ["IEA501I"], "M#4": ["IEA501I", "IEA500I"]})


@pytest.mark.anyio
@pytest.mark.parametrize("query,anchor,per_hit,neighbours,skipped", [
    pytest.param(IDENTIFIER_Q, None, 2, ["M#2"], 1,
                 id="the-continuation-is-packed-and-the-adjacent-entry-is-not"),
    pytest.param(IDENTIFIER_Q, None, 4, ["M#2", "M#4"], 2,
                 id="another-entry-is-skipped-not-a-stop"),
    pytest.param("iea500i rejected", None, 2, ["M#2"], 1, id="lower-case-as-retrieval-folds-it"),
    pytest.param("What should I do next?", IDENTIFIER_Q, 2, ["M#2"], 1,
                 id="an-anchored-follow-up-whose-antecedent-names-it"),
    pytest.param("IEA501I rejected", None, 4, ["M#3", "M#4"], 2,
                 id="the-rule-follows-the-named-identifier-not-the-hit"),
    pytest.param("IEA500I or IEA499I?", None, 4, ["M#0", "M#2", "M#4"], 1,
                 id="any-one-named-identifier-is-enough"),
    pytest.param(PLAIN_Q, None, 4, ["M#0", "M#2", "M#3", "M#4"], None,
                 id="a-turn-naming-no-identifier-is-unchanged"),
    pytest.param("What should I do next?", PLAIN_Q, 4, ["M#0", "M#2", "M#3", "M#4"], None,
                 id="an-anchored-follow-up-naming-no-identifier-is-unchanged"),
])
async def test_an_identifier_turn_packs_only_neighbours_carrying_a_named_identifier(
    query, anchor, per_hit, neighbours, skipped
):
    """Review decision 8 (#1324): a hit for one message id must not pack the
    adjacent entries for other ids, and must pack a chunk carrying the same
    id. A skipped neighbour does not stop the walk, takes no budget slot and
    is counted, text-free, only when the rule was in force."""
    corpus = Corpus().add([("M", [(3, 3)] * 5)], scope=_ENTRIES)
    retrieved, found, attributes, _ = await _expand(
        corpus, ["M#1"], query=query, anchor=anchor, answer_expand_per_hit=per_hit)
    assert found.hits[:1] == retrieved
    assert _texts(found.hits[1:]) == neighbours
    assert attributes.get("rag.expand_identifier_skipped") == skipped
    assert attributes["rag.expand_added"] == len(neighbours)
    assert attributes["rag.expand_stopped"] == 0
    assert attributes["rag.expand_budget_cut"] == 0


@pytest.mark.anyio
async def test_a_skipped_neighbour_takes_no_budget_slot_but_counts_toward_the_per_hit_cap():
    """Budget 1: the chunk after the hit (M#3, another entry) is skipped and
    its slot goes to the chunk before it (M#1); M#4 is then cut by the
    budget and M#0 skipped. Per-hit cap 1: the one neighbour is M#3, and
    nothing farther is taken in its place (as for a de-duplicated chunk)."""
    corpus = Corpus().add([("M", [(3, 3)] * 5)], scope=_ENTRIES)
    _, found, attributes, _ = await _expand(
        corpus, ["M#2"], query=IDENTIFIER_Q, answer_expand_budget=1)
    assert _texts(found.hits[1:]) == ["M#1"]
    assert (attributes["rag.expand_identifier_skipped"], attributes["rag.expand_budget_cut"]) == (2, 1)
    _, found, attributes, _ = await _expand(
        corpus, ["M#2"], query=IDENTIFIER_Q, answer_expand_per_hit=1)
    assert found.hits[1:] == []
    assert (attributes["rag.expand_identifier_skipped"], attributes["rag.expand_added"]) == (1, 0)


@pytest.mark.anyio
@pytest.mark.parametrize("query,stored,neighbours,skipped", [
    pytest.param("ieasysxx syntax", {"A#0": {"members": ["IEASYSxx"]}}, ["A#0"], 1,
                 id="a-member-in-retrievals-folded-case"),
    pytest.param("abend S0C4 in the step", {"A#2": {"system_codes": ["0C4"]}}, ["A#2"], 1,
                 id="a-system-code-in-retrievals-canonical-form"),
    pytest.param("IEA500I rejected", {"A#0": {"members": ["IEA500I"]}}, [], 2,
                 id="an-identifier-is-matched-in-its-own-field"),
    pytest.param("IEA500I rejected", {"A#0": {"message_ids": None}, "A#2": {"message_ids": "IEA500I"}},
                 [], 2, id="a-malformed-stored-field-carries-nothing"),
    pytest.param("SA23-1380", {"A#0": {"message_ids": ["IEA500I"]}}, [], 2,
                 id="a-document-number-alone-names-nothing-a-chunk-can-carry"),
])
async def test_the_identifier_rule_reads_the_three_stored_fields_with_retrievals_parsing(
    query, stored, neighbours, skipped
):
    """The named identifiers are retrieval's own parse of the searched text
    (parse_query), compared field by field with a neighbour's stored
    message_ids, members and system_codes. A turn that names only a document
    number is an identifier turn that names nothing in those fields: no
    neighbour is packed."""
    corpus = Corpus().add([("A", [(3, 3)] * 3)], scope=stored)
    _, found, attributes, _ = await _expand(corpus, ["A#1"], query=query)
    assert _texts(found.hits[1:]) == neighbours
    assert attributes["rag.expand_identifier_skipped"] == skipped


def test_the_identifier_rule_is_one_function_over_the_searched_texts():
    from mainframe_rag.agent.answer_expand import named_identifiers

    assert named_identifiers(PLAIN_Q, None) is None
    assert named_identifiers(PLAIN_Q, PLAIN_Q) is None
    named = named_identifiers("What next?", "IEA500I with abend S0C4 in ieasysxx, see SA23-1380-09")
    assert named is not None
    assert (named.message_ids, named.members, named.system_codes, named.doc_ids) == (
        ["IEA500I"], ["IEASYSxx"], ["0C4"], [])


@pytest.mark.anyio
@pytest.mark.parametrize("product,version,scope,neighbours,stopped", [
    pytest.param("z/OS", "9.9", {}, ["A#0", "A#2"], 0, id="same-scope"),
    pytest.param(None, None, {"A#2": {"product": "other"}}, ["A#0"], 1,
                 id="a-neighbour-must-share-its-hits-scope-even-unscoped"),
    pytest.param("z/OS", None, {"A#2": {"product": "other"}}, ["A#0"], 1,
                 id="another-product-stops-that-direction"),
    pytest.param("z/OS", "9.9", {"A#0": {"version": "1.1"}}, ["A#2"], 1,
                 id="another-version-stops-that-direction"),
])
async def test_a_neighbour_outside_the_request_scope_stops_its_direction(
    product, version, scope, neighbours, stopped
):
    corpus = Corpus().add([("A", [(3, 3)] * 3)], scope=scope)
    _, found, attributes, _ = await _expand(corpus, ["A#1"], product=product, version=version)
    assert _texts(found.hits[1:]) == neighbours
    assert attributes["rag.expand_stopped"] == stopped


@pytest.mark.anyio
async def test_a_neighbour_of_another_source_revision_is_never_packed():
    """The scroll is filtered on the hit's source_rev: the same heading and
    page in another revision of the manual is not a neighbour."""
    corpus = Corpus().add([("A", [(3, 3)] * 2)]).add([("A", [(3, 3)] * 3)], rev="rev-b")
    _, found, _, _ = await _expand(corpus, ["A#0"])
    assert [hit.chunk_id for hit in found.hits[1:]] == [make_chunk_id(REV, "A", 3, 1)]


@pytest.mark.anyio
async def test_a_hit_whose_stored_point_is_gone_is_not_expanded():
    corpus = Corpus().add([("A", [(3, 3)] * 3)]).add([("B", [(3, 3)] * 2)], rev="rev-b")
    retrieved = [corpus.hit("A#1", 0.9), corpus.hit("B#0", 0.8)]
    del corpus.payloads[corpus.ids["A#1"]]
    reader = StoredChunksFake(corpus.payloads)
    deps = AnswerCoreDeps(settings=_settings(), llm=ModelAdapter(_AnswerLLM()),
                          retrieve=_Search(retrieved), neighbours=reader)
    root = _root()
    found = await retrieve_for_answer(
        SearchPlan("literal", PLAIN_Q), AnswerCoreInput(query=PLAIN_Q), deps, root)
    assert found.hits[:2] == retrieved and _texts(found.hits[2:]) == ["B#1"]
    assert [call["op"] for call in reader.calls] == ["retrieve", "scroll"]
    assert _expand_attributes(root)["rag.expand_stopped"] == 2


class _Failing:
    def __init__(self, op: str, action):
        self.op, self.action = op, action

    async def __call__(self, op: str) -> None:
        if op == self.op:
            await self.action()


async def _raise_storage_error() -> None:
    raise ConnectionError(CANARY)


async def _never_answer() -> None:
    await asyncio.sleep(3600)


@pytest.mark.anyio
@pytest.mark.parametrize("op,action,reason", [
    ("retrieve", _raise_storage_error, "lookup_error"),
    ("scroll", _raise_storage_error, "lookup_error"),
    ("retrieve", _never_answer, "timeout"),
    ("scroll", _never_answer, "timeout"),
])
async def test_a_lookup_failure_leaves_the_hits_exactly_as_they_were(op, action, reason, caplog):
    """Any lookup error, or the one stage deadline, is never a retrieval
    failure: the retrieved hits pass on unchanged under a fixed label, and
    no log carries the upstream text."""
    corpus = Corpus().add([("A", [(3, 3)] * 3)]).add([("B", [(3, 3)] * 3)], rev="rev-b")
    reader = StoredChunksFake(corpus.payloads, before=_Failing(op, action))
    with caplog.at_level(logging.INFO):
        retrieved, found, attributes, _ = await _expand(
            corpus, ["A#1", "B#1"], reader, evidence_timeout_s=0.05)
    assert found.hits == retrieved and found.timings == TIMINGS
    assert attributes == {
        "rag.expand_mode": "fallback", "rag.expand_reason": reason, "rag.expand_added": 0,
        "rag.expand_hits": 0, "rag.expand_stopped": 0, "rag.expand_budget_cut": 0,
    }
    assert CANARY not in caplog.text


@pytest.mark.anyio
async def test_cancellation_during_the_lookup_is_not_a_fallback():
    async def cancel() -> None:
        raise asyncio.CancelledError

    corpus = Corpus().add([("A", [(3, 3)] * 3)])
    with pytest.raises(asyncio.CancelledError):
        await _expand(corpus, ["A#1"], StoredChunksFake(corpus.payloads, before=_Failing("scroll", cancel)))


@pytest.mark.anyio
@pytest.mark.parametrize("hits,overrides,reader,attributes", [
    pytest.param([], {}, True, {"rag.expand_mode": "skipped", "rag.expand_reason": "no_hits"},
                 id="no-hits"),
    pytest.param(["A#1"], {"answer_expand_budget": 0}, True,
                 {"rag.expand_mode": "skipped", "rag.expand_reason": "no_budget"}, id="no-budget"),
    pytest.param(["A#1"], {}, False,
                 {"rag.expand_mode": "fallback", "rag.expand_reason": "no_reader"},
                 id="no-stored-chunk-reader-wired"),
])
async def test_expansion_with_nothing_to_do_reads_nothing(hits, overrides, reader, attributes):
    corpus = Corpus().add([("A", [(3, 3)] * 3)])
    stored = StoredChunksFake(corpus.payloads)
    retrieved = [corpus.hit(label) for label in hits]
    deps = AnswerCoreDeps(settings=_settings(**overrides), llm=ModelAdapter(_AnswerLLM()),
                          retrieve=_Search(retrieved), neighbours=stored if reader else None)
    root = _root()
    found = await retrieve_for_answer(
        SearchPlan("literal", PLAIN_Q), AnswerCoreInput(query=PLAIN_Q), deps, root)
    assert found.hits == retrieved and stored.calls == []
    assert _expand_attributes(root) == {
        "rag.expand_added": 0, "rag.expand_hits": 0, "rag.expand_stopped": 0,
        "rag.expand_budget_cut": 0, **attributes,
    }


class _SelectingLLM(_AnswerLLM):
    """Answers the candidate-selection call (told apart by its candidate
    list) with 'reply'; every other call is the answer generation."""

    def __init__(self, reply: str):
        super().__init__()
        self.reply, self.select_calls = reply, 0

    def chat(self, messages, reasoning_effort=None, temperature=None):
        if "Candidate excerpts:" not in messages[-1].content:
            return super().chat(messages, reasoning_effort, temperature)
        self.select_calls += 1
        return ChatResult(content=self.reply, finish_reason="stop", usage=TokenUsage())


@pytest.mark.anyio
@pytest.mark.parametrize("query,select_calls,packed", [
    pytest.param(PLAIN_Q, 1, ["C#0", "A#0", "C#1", "A#1"], id="after-the-models-selection"),
    pytest.param(IDENTIFIER_Q, 0, ["A#0", "B#0", "A#1", "B#1"],
                 id="an-identifier-turn-bypasses-selection-and-is-still-expanded"),
])
async def test_expansion_runs_after_selection_on_the_final_list(query, select_calls, packed):
    """Stage order: retrieve, select (when on), expand. The neighbours are
    those of the hits selection kept, in that list's order; an identifier
    turn (selection bypassed) is expanded like any other."""
    corpus = Corpus()
    for name, rev in (("A", "rev-a"), ("B", "rev-b"), ("C", "rev-c")):
        # Every chunk carries the identifier the identifier turn names, so
        # the identifier rule (below) leaves the stage order visible.
        corpus.add([(name, [(3, 3)] * 2)], rev=rev,
                   scope={f"{name}#{n}": {"message_ids": ["IEA500I"]} for n in range(2)})
    llm = _SelectingLLM("[3, 1]")
    deps = AnswerCoreDeps(
        settings=_settings(answer_select_enabled=True, answer_hit_limit=2,
                           answer_select_candidates=3, answer_expand_per_hit=1),
        llm=ModelAdapter(llm), retrieve=_LimitedSearch([corpus.hit(f"{n}#0") for n in "ABC"]),
        neighbours=StoredChunksFake(corpus.payloads))
    root = _root()
    out = await execute_answer_core(AnswerCoreInput(query=query), deps, parent_span=root)
    assert llm.select_calls == select_calls
    assert _texts(out.hits) == packed
    assert [e.chunk_id for e in out.evidence.entries] == [corpus.ids[label] for label in packed]
    assert _expand_attributes(root)["rag.expand_added"] == 2


class _LimitedSearch(_Search):
    async def __call__(self, query, *, product, version, settings, limit=None):
        depth = settings.answer_hit_limit if limit is None else limit
        return RetrievalResult(list(self.hits)[:depth], "nl", dict(TIMINGS))


class _Untouchable:
    """A stored-chunk reader the stage must never call."""

    async def retrieve(self, *args, **kwargs):
        raise AssertionError("stored chunks read with the stage off")

    scroll = retrieve


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_the_packed_prompt_labels_retrieved_hits_first_then_neighbours(stream):
    """The neighbours are ordinary entries after the retrieved hits: the
    prompt labels, the evidence manifest and the supplied projection carry
    them with their own ids, on the buffered and the streamed path."""
    from mainframe_rag.agent.answer_core import execute_answer_core_stream

    corpus = Corpus().add([("A", [(3, 3)] * 3)])
    retrieved = [corpus.hit("A#1")]
    llm = _AnswerLLM()
    deps = AnswerCoreDeps(settings=_settings(), llm=ModelAdapter(llm), retrieve=_Search(retrieved),
                          neighbours=StoredChunksFake(corpus.payloads))
    if stream:
        events = [event async for event in execute_answer_core_stream(AnswerCoreInput(query=PLAIN_Q), deps)]
        out = events[-1]["output"]
    else:
        out = await execute_answer_core(AnswerCoreInput(query=PLAIN_Q), deps)
    assert _texts(out.hits) == ["A#1", "A#0", "A#2"]
    assert [(e.prompt_index, e.chunk_id) for e in out.evidence.entries] == [
        (1, corpus.ids["A#1"]), (2, corpus.ids["A#0"]), (3, corpus.ids["A#2"])]
    # The projection's handle is the prompt label; same-page neighbours of one
    # section repeat their hit's cite string, which the flag declares.
    assert [(s.index, s.text, s.citation, s.duplicate_citation) for s in out.supplied] == [
        (n, text, "SYN-0001-00 Synthetic Reference, A, p. 4", True)
        for n, text in enumerate(["A#1", "A#0", "A#2"], 1)]
    prompt = llm.calls[-1][-1].content
    assert prompt.index("[1]") < prompt.index("A#1") < prompt.index("[2]") < prompt.index("A#0")
    assert out.timings == TIMINGS


def _single(query: str) -> AnswerCoreInput:
    return AnswerCoreInput(query=query)


def _follow_up(query: str) -> AnswerCoreInput:
    """An anchored follow-up: both legs are searched and fused."""
    from mainframe_rag.ports import ChatMessage

    return AnswerCoreInput(query=query, is_chat=True, messages=[
        ChatMessage(role="user", content="What does IEA500I mean?"),
        ChatMessage(role="assistant", content="An earlier answer"),
        ChatMessage(role="user", content=query)])


@pytest.mark.anyio
@pytest.mark.parametrize("select", [False, True])
@pytest.mark.parametrize("query", [PLAIN_Q, IDENTIFIER_Q])
@pytest.mark.parametrize("source", [_single, _follow_up])
async def test_expansion_off_never_reads_stored_chunks_and_changes_nothing(
    select, query, source, monkeypatch
):
    """ANSWER_EXPAND_ENABLED off (the default): the stage's code is never
    entered and no stored chunk is read, on a first turn and an anchored
    follow-up, with candidate selection on or off. The hits, timings, prompt
    and span attributes equal those of a turn with no reader at all."""
    from mainframe_rag.agent import answer_expand

    async def never(*args, **kwargs):
        raise AssertionError("neighbour expansion entered with the stage off")

    monkeypatch.setattr(answer_expand, "expand_hits", never)
    corpus = Corpus()
    for name, rev in (("A", "rev-a"), ("B", "rev-b"), ("C", "rev-c")):
        corpus.add([(name, [(3, 3)] * 2)], rev=rev)
    pool = [corpus.hit(f"{n}#0") for n in "ABC"]
    overrides = {"answer_expand_enabled": False, "answer_hit_limit": 2,
                 "answer_select_enabled": select, "answer_select_candidates": 3,
                 "chat_followup_anchor_enabled": True}
    runs = []
    for reader in (None, _Untouchable()):
        llm, root = _SelectingLLM("[3, 1]"), _root()
        out = await execute_answer_core(
            source(query),
            AnswerCoreDeps(settings=_settings(**overrides), llm=ModelAdapter(llm),
                           retrieve=_LimitedSearch(pool), neighbours=reader),
            parent_span=root)
        runs.append((out.hits, out.timings, out.supplied, llm.calls, dict(root.attributes)))
    assert runs[0] == runs[1]
    hits, timings, _, _, attributes = runs[1]
    assert len(hits) == 2 and all(type(hit) is SearchHit for hit in hits)
    assert sorted(timings) == ["embed_ms", "qdrant_ms"]
    assert not any(key.startswith("rag.expand") for key in attributes)



# --- The real chunker and a real Qdrant store --------------------------------
#
# The cases above mint point ids with the chunk-key function the stage also
# uses. Here the chunker itself assigns sections, pages and ordinals, the
# ingest writer stores the points, and the stage reads them back through the
# sync-to-async adapter over qdrant-client's local store: the filter (exact
# match and integer range), limit and next-page offset are the client's own.

_STEP = "Step {n}: set the widget dial to {n} and wait for the lamp. "
_MESSAGES_PAGE = (
    "WID101E\nWIDGET TABLE FULL\nExplanation: The widget table has no free slot.\n"
    "WID102E\nWIDGET NAME INVALID\nExplanation: The name has a character outside A-Z.\n"
    "WID103E\nWIDGET LIMIT REACHED\nExplanation: The configured widget limit was reached.\n"
)


def _procedure_page(first: int) -> str:
    return "\n\n".join((_STEP.format(n=n) * 12).strip() for n in range(first, first + 6))


@pytest.fixture(scope="module")
def stored_manual():
    """(chunks in the chunker's order, sync client, collection) for one
    generated manual: an overview page, a procedure whose chunks run over
    three pages, a page of three bookmarked messages, a last message."""
    from pathlib import Path

    from qdrant_client import QdrantClient, models

    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.qdrant_io import upsert_chunks

    toc = [[1, "Widget guide", 1], [2, "Long procedure", 2], [2, "Messages", 5],
           [3, "WID101E", 5], [3, "WID102E", 5], [3, "WID103E", 5], [3, "WID104E", 6]]
    pages = ["Overview of the widget guide.", "Long procedure\n\n" + _procedure_page(1),
             _procedure_page(7), _procedure_page(13), "Messages\n" + _MESSAGES_PAGE,
             "WID104E\nWIDGET OFFLINE\nExplanation: The widget is offline.\n"]
    parsed = ParsedDoc(path=Path("widget-guide.pdf"), sha256="7" * 64, doc_id="widget-guide",
                       title="Widget Guide", product="Widget", version="1.0", vendor="Example",
                       toc=toc, page_count=len(pages))
    chunks = make_chunks(parsed, pages, [str(n) for n in range(1, len(pages) + 1)])
    settings = Settings(qdrant_collection="expand", dense_dim=2, _env_file=None)
    client = QdrantClient(":memory:")
    client.create_collection(
        "expand", vectors_config={"dense": models.VectorParams(size=2, distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams()})
    upsert_chunks(client, settings, parsed, chunks, [([1.0, 0.0], ([1], [1.0])) for _ in chunks])
    yield chunks, client
    client.close()


async def _expand_stored(stored_manual, position: int, **overrides):
    from mainframe_rag.ports import AsyncReaderAdapter

    chunks, client = stored_manual
    [record] = client.retrieve("expand", ids=[chunks[position].chunk_id], with_payload=True)
    hit = hit_from_payload(str(record.id), record.payload, 0.5)
    deps = AnswerCoreDeps(
        settings=_settings(qdrant_collection="expand", **overrides),
        llm=ModelAdapter(_AnswerLLM()), retrieve=_Search([hit]),
        neighbours=AsyncReaderAdapter(client))
    root = _root()
    found = await retrieve_for_answer(
        SearchPlan("literal", PLAIN_Q), AnswerCoreInput(query=PLAIN_Q), deps, root)
    return found.hits[1:], _expand_attributes(root)


def _shape(chunks) -> list[tuple[str, int, int]]:
    return [(c.heading_path.rsplit(" > ", 1)[-1], c.page_start, c.ordinal) for c in chunks]


def test_the_generated_manual_has_the_shapes_the_order_rules_meet(stored_manual):
    """Pins the chunker's own output for the manual: a section whose chunks
    span pages with ordinals past 0, and one page three sections share."""
    assert _shape(stored_manual[0]) == _MANUAL_SHAPE


@pytest.mark.anyio
@pytest.mark.parametrize("hit,neighbours,stopped", _MANUAL_CASES)
async def test_stored_points_of_the_real_chunker_expand_in_the_chunkers_order(
    stored_manual, hit, neighbours, stopped
):
    """The neighbours of a hit, by (section, page, ordinal) as the chunker
    assigned them, read back from a real store."""
    chunks = stored_manual[0]
    found, attributes = await _expand_stored(stored_manual, _MANUAL_SHAPE.index(hit))
    by_id = dict(zip([c.chunk_id for c in chunks], _MANUAL_SHAPE, strict=True))
    assert [by_id[h.chunk_id] for h in found] == neighbours
    assert attributes["rag.expand_stopped"] == stopped


@pytest.mark.anyio
async def test_recovered_order_never_disagrees_with_the_chunkers_own_order(stored_manual):
    """One property over the whole manual: whichever chunk is the hit, the
    hit and the neighbours the stage returns are one unbroken run of the
    chunker's output order, listed in that order."""
    chunks = stored_manual[0]
    order = [chunk.chunk_id for chunk in chunks]
    wrong, found = [], 0
    for position in range(len(order)):
        neighbours, _ = await _expand_stored(stored_manual, position, answer_expand_per_hit=8)
        placed = [order.index(h.chunk_id) for h in neighbours]
        found += len(placed)
        run = sorted([position, *placed])
        if placed != sorted(placed) or run != list(range(run[0], run[0] + len(run))):
            wrong.append(position)
    assert wrong == []
    assert found == _MANUAL_NEIGHBOURS_FOUND


@pytest.mark.anyio
async def test_on_the_real_chunker_only_chunks_whose_own_text_names_the_id_are_packed():
    """Ingest fills message_ids from each chunk's own text. A long message
    entry is cut into three chunks; only the first and the last name the id.
    On an identifier turn the hit (the entry's first chunk) packs the last
    one and neither the continuation that does not name it nor the next
    entry; on a plain turn all three neighbours are packed."""
    from pathlib import Path

    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.payload import point_payload

    toc = [[1, "Messages", 1], [2, "WID200E", 1], [2, "WID201E", 3]]
    pages = ["Messages\nWID200E\nWIDGET VALVE STUCK\nExplanation: The valve is stuck.\n\n"
             + _procedure_page(1),
             _procedure_page(7) + "\n\nSee also WID200E in the log.",
             "WID201E\nWIDGET OFFLINE\nExplanation: The widget is offline.\n"]
    parsed = ParsedDoc(path=Path("widget-messages.pdf"), sha256="8" * 64, doc_id="widget-messages",
                       title="Widget Messages", product="Widget", version="1.0", vendor="Example",
                       toc=toc, page_count=len(pages))
    chunks = make_chunks(parsed, pages, ["1", "2", "3"])
    assert [(c.heading_path.rsplit(" > ", 1)[-1], c.ordinal, c.message_ids) for c in chunks] == [
        ("WID200E", 0, ["WID200E"]), ("WID200E", 1, []), ("WID200E", 2, ["WID200E"]),
        ("WID201E", 0, ["WID201E"])]
    payloads = {c.chunk_id: point_payload(parsed, c, "test-rules") for c in chunks}
    order = [c.chunk_id for c in chunks]

    async def neighbours_of_the_first_chunk(query: str) -> tuple[list[int], dict]:
        hit = hit_from_payload(order[0], payloads[order[0]], 0.5)
        deps = AnswerCoreDeps(settings=_settings(), llm=ModelAdapter(_AnswerLLM()),
                              retrieve=_Search([hit]), neighbours=StoredChunksFake(payloads))
        root = _root()
        found = await retrieve_for_answer(
            SearchPlan("literal", query), AnswerCoreInput(query=query), deps, root)
        return [order.index(h.chunk_id) for h in found.hits[1:]], _expand_attributes(root)

    packed, attributes = await neighbours_of_the_first_chunk("What does WID200E mean?")
    assert packed == [2]
    assert attributes["rag.expand_identifier_skipped"] == 2
    packed, attributes = await neighbours_of_the_first_chunk(PLAIN_Q)
    assert packed == [1, 2, 3]
    assert "rag.expand_identifier_skipped" not in attributes
