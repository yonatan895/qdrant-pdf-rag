"""Page-image A/B tool (issue #1318, PR-0): hermetic, over original synthetic
PDFs drawn in the test, a fake agent (search + evidence routes) and a fake
reasoning model. No corpus, network, GPU or real model."""

from __future__ import annotations

import base64
import json
import logging
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pymupdf
import pytest

from mainframe_rag.agent.answer import build_messages
from mainframe_rag.agent.app import EvidenceSearchHit, SearchResponse
from mainframe_rag.agent.evidence import encode_reference
from mainframe_rag.agent.images import wire_messages
from mainframe_rag.config import Settings
from mainframe_rag.eval import page_image_arm as pia
from mainframe_rag.eval.answers import judge
from mainframe_rag.ingest.ibm_pdf import sha256_file as ingest_sha256_file
from mainframe_rag.ports import ChatResult, TokenUsage
from mainframe_rag.retrieve.query import SearchHit
from tests.fakes import settings_kw

ROOT = Path(__file__).resolve().parents[1]
# Sentinels: a leak into the export, stdout, stderr or logs is detectable.
QUESTION_MARK = "quibblefrost"
ANSWER_MARK = "snargleplume"
HEADING_MARK = "wobblethatch"
CHUNK_MARK = "zorblatt"
PAGE_MARK = "PLINTHWICK"
GOLD = "GRONK42"
FORBIDDEN = "excerpts do not answer"
TABLE_DOC, DIAGRAM_DOC = "TB00-0001-00", "DG00-0002-00"


def _table_pdf(path: Path) -> None:
    """Three pages; the ruled table is on the third (0-based index 2)."""
    doc = pymupdf.open()
    doc.new_page(width=612, height=792).insert_text((72, 90), f"Synthetic table guide {PAGE_MARK} cover")
    doc.new_page(width=612, height=792).insert_text((72, 90), f"Narrative about gadgets {PAGE_MARK}")
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 70), f"Table 1. Synthetic lever settings {PAGE_MARK}")
    for row in range(5):
        y = 100 + 24 * row
        page.draw_line((72, y), (432, y))
        page.insert_text((78, y + 16), f"LEVER{row}")
        page.insert_text((258, y + 16), f"{GOLD if row == 3 else 'VALUE' + str(row)}")
    for x in (72, 252, 432):
        page.draw_line((x, 100), (x, 196))
    doc.save(path)
    doc.close()


def _diagram_pdf(path: Path) -> None:
    """Two pages; the second is diagram-like (boxes and a rail, little text)."""
    doc = pymupdf.open()
    doc.new_page(width=612, height=792).insert_text((72, 90), f"Synthetic diagram guide {PAGE_MARK}")
    page = doc.new_page(width=612, height=792)
    page.draw_line((72, 200), (500, 200))
    for x in (120, 260, 400):
        page.draw_rect(pymupdf.Rect(x, 170, x + 80, 230))
        page.insert_text((x + 8, 204), "OPTION")
    doc.save(path)
    doc.close()


@pytest.fixture(autouse=True)
def one_gradeable_row_is_enough(monkeypatch):
    """Most cases run one or two rows; the real floor has its own test."""
    monkeypatch.setattr(pia, "MIN_GRADEABLE_ROWS", 1)


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    (root / "sub").mkdir(parents=True)
    _table_pdf(root / "table-guide.pdf")
    _diagram_pdf(root / "sub" / "diagram-guide.PDF")
    return root


def _hit(chunk_id: str, doc_id: str, *, chunk_type: str = "narrative", page: int = 0) -> dict:
    heading = f"Lever settings {HEADING_MARK} {chunk_id}"
    return {
        "chunk_id": chunk_id, "score": 0.9, "cite": f"{doc_id} Guide, {heading}, p. {page + 1}",
        "heading": heading, "text": f"The {CHUNK_MARK} lever {chunk_id} is set in the table.",
        "doc_id": doc_id, "title": "Guide", "page_label": str(page + 1),
        "chunk_type": chunk_type, "message_ids": [], "page_start": page, "page_end": page,
        "reference": f"ref/{chunk_id}",
    }


class FakeResponse:
    def __init__(self, status_code: int, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeAgent:
    """The two agent routes the tool reads. `spans` maps a chunk id to
    (sha256, one-based first page, one-based last page or None)."""

    def __init__(self, hits_by_query: dict, spans: dict, search_status: int = 200) -> None:
        self.hits_by_query = hits_by_query
        self.spans = spans
        self.search_status = search_status
        self.calls: list[tuple[str, object]] = []

    def post(self, path, json):
        self.calls.append((path, json))
        assert path == "/v1/search"
        if self.search_status != 200:
            return FakeResponse(self.search_status, {"error": {"message": QUESTION_MARK}})
        hits = self.hits_by_query[json["query"]][: json["limit"]]
        return FakeResponse(200, {"request_id": "r", "query_kind": "prose", "hits": hits})

    def get(self, path):
        self.calls.append((path, None))
        prefix = "/v1/evidence/ref%2F"
        assert path.startswith(prefix)
        span = self.spans.get(path[len(prefix):])
        if span is None:
            return FakeResponse(404, {"error": {"code": "evidence_not_found"}})
        sha, first, last = span
        return FakeResponse(200, {
            "source_sha256": sha, "text": f"stored {CHUNK_MARK} text",
            "location": {"physical_page_start": first, "physical_page_end": last,
                         "printed_label": None}})


class FakeModel:
    """Records each call's wire messages; `answer(query_text, images)` decides
    the reply, or raises."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list[list[dict]] = []

    async def chat(self, messages, reasoning_effort=None, temperature=None, max_tokens=None):
        wire = wire_messages(messages)
        self.calls.append(wire)
        last = wire[-1]["content"]
        images = [] if isinstance(last, str) else [p for p in last if p["type"] == "image_url"]
        text = last if isinstance(last, str) else last[0]["text"]
        body = self.answer(text, images)
        return ChatResult(
            content=f"{body} {ANSWER_MARK} [1]\n\nCitations:\n[1]",
            usage=TokenUsage(prompt_tokens=100 + 2700 * len(images), completion_tokens=5))


def _text_of(wire: list[dict]) -> list[tuple[str, str]]:
    return [(m["role"], m["content"] if isinstance(m["content"], str) else m["content"][0]["text"])
            for m in wire]


def _pngs(wire: list[dict]) -> list[bytes]:
    last = wire[-1]["content"]
    if isinstance(last, str):
        return []
    urls = [p["image_url"]["url"] for p in last if p["type"] == "image_url"]
    assert all(u.startswith("data:image/png;base64,") for u in urls)
    return [base64.b64decode(u.split(",", 1)[1]) for u in urls]


def _ihdr(png: bytes) -> tuple[int, int, int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR"
    width, height, depth, colour = struct.unpack(">IIBB", png[16:26])
    assert zlib.crc32(png[12:29]) == struct.unpack(">I", png[29:33])[0]
    return width, height, depth, colour


def _row(row_id: str, n: int, query_class: str = "table", **over) -> dict:
    row = {
        "id": row_id, "query": f"Which value does lever {n} take, {QUESTION_MARK} {n}?",
        "query_class": query_class, "expected_behavior": "answer",
        "expected_doc_ids": [TABLE_DOC], "gold_must_contain": [GOLD],
        "gold_must_not_contain": [FORBIDDEN],
    }
    row.update(over)
    return row


def _settings(**over) -> Settings:
    return Settings(**settings_kw(answer_select_enabled=False, answer_expand_enabled=False, **over))


class Run:
    """One tool run over `rows` with the fakes; outputs under tmp_path."""

    def __init__(self, tmp_path, corpus, rows, *, hits=None, spans=None, answer=None,
                 probe=lambda: True, argv=(), agent=None, golden_name="golden.jsonl"):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.golden = tmp_path / golden_name
        self.golden.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.out = tmp_path / "private" / "report.jsonl"
        self.export = tmp_path / "private" / "export.json"
        self.out.parent.mkdir(exist_ok=True)
        table_sha = pia.sha256_file(corpus / "table-guide.pdf")
        diagram_sha = pia.sha256_file(corpus / "sub" / "diagram-guide.PDF")
        default_hits = [_hit("d1", DIAGRAM_DOC, chunk_type="syntax", page=1),
                        _hit("t1", TABLE_DOC, chunk_type="table", page=2)]
        self.agent = agent or FakeAgent(
            {r["query"]: (default_hits if hits is None else hits) for r in rows},
            {"t1": (table_sha, 3, 3), "d1": (diagram_sha, 2, None)} if spans is None else spans)
        self.model = FakeModel(answer or (lambda text, images: GOLD if images else "unsure"))
        self.argv = ["--golden", str(self.golden), "--corpus-dir", str(corpus),
                     "--out", str(self.out), "--export", str(self.export), *argv]
        self.probe = probe
        self.rc: int | None = None

    def go(self, settings: Settings | None = None, tokenizer=None) -> Run:
        self.rc = pia.main(self.argv, settings=settings or _settings(), agent=self.agent,
                           model=self.model, tokenizer=tokenizer, probe=self.probe)
        return self

    @property
    def exported(self) -> dict:
        return json.loads(self.export.read_text(encoding="utf-8"))

    @property
    def private(self) -> list[dict]:
        return [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines()]

    def outcomes(self) -> dict[str, str]:
        return {r["id"]: r["outcome"] for r in self.exported["rows"]}


# --- the two arms -------------------------------------------------------------

def test_both_arms_send_identical_text_and_only_the_image_arm_carries_the_gold_page(
        tmp_path, corpus):
    run = Run(tmp_path, corpus, [_row("T-1", 1)]).go()
    assert run.rc == 0 and run.outcomes() == {"T-1": "fixed"}
    text_call, image_call = run.model.calls
    assert isinstance(text_call[-1]["content"], str) and _pngs(text_call) == []
    # Both hits are packed, the gold hit in slot [2].
    prompt = text_call[-1]["content"]
    assert prompt.index("lever d1") < prompt.rfind("[2]", 0, prompt.index("lever t1"))
    # The text arm is the library's prompt, byte for byte: no page-image wording.
    assert "Page image" not in json.dumps(text_call)
    library = build_messages(
        _row("T-1", 1)["query"], [SearchHit.model_validate({**h, "reference": None}) for h in (
            _hit("d1", DIAGRAM_DOC, chunk_type="syntax", page=1),
            _hit("t1", TABLE_DOC, chunk_type="table", page=2))],
        max_context_chars=_settings().prompt_max_context_chars,
        max_chunk_chars=_settings().prompt_max_chunk_chars, settings=_settings(),
        order=_settings().prompt_order)
    assert _text_of(text_call) == [(m.role, m.content) for m in library.messages]
    # The image arm: the same messages, and on the tail of the final user
    # message one protocol sentence plus one label for the one image, naming
    # the PDF page (1-based) and the packed slot of the hit that got it.
    assert _text_of(image_call)[:-1] == _text_of(text_call)[:-1]
    assert _text_of(image_call)[-1] == ("user", prompt + "\n\n" + pia.PAGE_IMAGE_PROTOCOL
                                        + "\nPage image 1: PDF page 3 of excerpt [2]")
    # One image: the packed gold hit's page (0-based index 2), never the
    # packed distractor's page, at the design profile (150 dpi, 8-bit grey).
    (png,) = _pngs(image_call)
    with pymupdf.open(corpus / "table-guide.pdf") as doc:
        pages = [doc[i].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")
                 for i in range(doc.page_count)]
    assert png == pages[2] and png not in (pages[0], pages[1])
    assert _ihdr(png) == (1275, 1650, 8, 0)
    assert [m["role"] for m in image_call] == [m["role"] for m in text_call]
    assert [p["type"] for p in image_call[-1]["content"]] == ["text", "image_url"]


def test_no_image_label_sends_byte_identical_text_in_both_arms(tmp_path, corpus):
    run = Run(tmp_path, corpus, [_row("T-1", 1)], argv=["--no-image-label"]).go()
    text_call, image_call = run.model.calls
    assert _text_of(text_call) == _text_of(image_call) and len(_pngs(image_call)) == 1
    assert run.exported["params"]["image_label"] is False
    assert Run(tmp_path / "b", corpus, [_row("T-1", 1)]).go().exported["params"]["image_label"] is True


def test_a_two_page_gold_hit_gets_both_pages_and_the_count_flag_caps_them(tmp_path, corpus):
    sha = pia.sha256_file(corpus / "table-guide.pdf")
    spans = {"t1": (sha, 2, 3), "t2": (sha, 1, 3)}
    hits = [_hit("t1", TABLE_DOC, page=1), _hit("t2", TABLE_DOC, page=0)]
    run = Run(tmp_path, corpus, [_row("T-1", 1)], hits=hits, spans=spans).go()
    with pymupdf.open(corpus / "table-guide.pdf") as doc:
        pages = [doc[i].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")
                 for i in range(3)]
    # Default 2: the first hit's two pages; the second hit adds nothing.
    assert _pngs(run.model.calls[1]) == [pages[1], pages[2]]
    one = Run(tmp_path / "one", corpus, [_row("T-1", 1)], hits=hits, spans=spans,
              argv=["--max-images", "1"])
    three = Run(tmp_path / "three", corpus, [_row("T-1", 1)], hits=hits, spans=spans,
                argv=["--max-images", "3"])
    assert _pngs(one.go().model.calls[1]) == [pages[1]]
    # Pages are attached once: t2 spans pages 0-2 but only page 0 is new, and
    # a hit contributes at most two of its pages.
    assert _pngs(three.go().model.calls[1]) == [pages[1], pages[2], pages[0]]


def test_with_the_default_stages_on_only_the_answer_call_of_the_image_arm_gets_the_image(
        tmp_path, corpus):
    """Candidate selection (default on) makes its own model call in the text
    arm; the image arm reuses the text arm's hits and makes the answer call
    only. Expansion has no stored-point reader here and falls back."""
    settings = Settings(**settings_kw())
    assert settings.answer_select_enabled and settings.answer_expand_enabled
    hits = [_hit("t1", TABLE_DOC, chunk_type="table", page=2),
            *(_hit(f"d{n}", DIAGRAM_DOC, page=1) for n in range(1, 20))]
    run = Run(tmp_path, corpus, [_row("T-1", 1)], hits=hits, argv=["--no-image-label"]).go(settings)
    assert run.outcomes() == {"T-1": "fixed"}
    # Selection asked the model once (20 candidates for 16 places), then the two answer calls.
    assert len(run.model.calls) == 3
    with_images = [call for call in run.model.calls if _pngs(call)]
    assert with_images == [run.model.calls[-1]] and len(_pngs(with_images[0])) == 1
    assert _text_of(run.model.calls[-2]) == _text_of(run.model.calls[-1])
    searches = [body["limit"] for path, body in run.agent.calls if path == "/v1/search"]
    # One search at the candidate width; the image arm does not search again.
    assert searches == [settings.answer_select_candidates]
    assert run.exported["params"]["settings"]["answer_select_enabled"] is True


def test_the_image_budget_goes_to_the_qualifying_gold_hit_first(tmp_path, corpus):
    """A manual can pack several chunks; the table chunk is packed last here
    and still gets the first image."""
    sha = pia.sha256_file(corpus / "table-guide.pdf")
    spans = {"n1": (sha, 1, 1), "n2": (sha, 2, 2), "t1": (sha, 3, 3)}
    hits = [_hit("n1", TABLE_DOC, page=0), _hit("n2", TABLE_DOC, page=1),
            _hit("t1", TABLE_DOC, chunk_type="table", page=2)]
    with pymupdf.open(corpus / "table-guide.pdf") as doc:
        pages = [doc[i].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")
                 for i in range(3)]
    run = Run(tmp_path / "a", corpus, [_row("T-1", 1)], hits=hits, spans=spans).go()
    assert _pngs(run.model.calls[1]) == [pages[2], pages[0]]
    assert run.exported["images"]["rows_hit_not_qualifying"] == 0
    # Labels follow the images: slot [3] page 3, then slot [1] page 1; the
    # packed gold hit in slot [2] got no image and gets no label.
    tail = _text_of(run.model.calls[1])[-1][1].split(pia.PAGE_IMAGE_PROTOCOL)[1]
    assert tail == ("\nPage image 1: PDF page 3 of excerpt [3]"
                    "\nPage image 2: PDF page 1 of excerpt [1]")
    one = Run(tmp_path / "b", corpus, [_row("T-1", 1)], hits=hits, spans=spans,
              argv=["--max-images", "1"]).go()
    assert _pngs(one.model.calls[1]) == [pages[2]]
    # No qualifying hit: packed order, and the export counts the row.
    plain = Run(tmp_path / "c", corpus, [_row("T-1", 1)], hits=hits[:2], spans=spans).go()
    assert _pngs(plain.model.calls[1]) == [pages[0], pages[1]]
    assert plain.exported["images"]["rows_hit_not_qualifying"] == 1
    # The row's expected heading qualifies a narrative hit (the eval's relevance rule).
    headed = Run(tmp_path / "d", corpus, [_row("T-1", 1, expected_heading="settings wobblethatch n2")],
                 hits=hits[:2], spans=spans, argv=["--max-images", "1"]).go()
    assert _pngs(headed.model.calls[1]) == [pages[1]]


def test_a_hit_qualifies_by_type_atomic_unit_or_expected_heading():
    def hit(**over):
        return SearchHit.model_validate({**_hit("c", TABLE_DOC), **over})

    assert not pia.qualifies(hit(), None)
    assert pia.qualifies(hit(chunk_type="table"), None) and pia.qualifies(hit(chunk_type="syntax"), None)
    assert pia.qualifies(hit(units=[[0, 5, "prose"], [5, 9, "atomic"]]), None)
    assert not pia.qualifies(hit(units=[[0, 5, "prose"]]), None) and not pia.qualifies(hit(units=[]), None)
    assert pia.qualifies(hit(), "LEVER SETTINGS") and not pia.qualifies(hit(), "valve")


def test_a_diagram_page_is_attached_for_a_syntax_row_whose_span_has_no_end_page(tmp_path, corpus):
    row = _row("S-1", 1, query_class="syntax", expected_doc_ids=[DIAGRAM_DOC])
    run = Run(tmp_path, corpus, [row]).go()
    assert run.outcomes() == {"S-1": "fixed"}
    with pymupdf.open(corpus / "sub" / "diagram-guide.PDF") as doc:
        expected = doc[1].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")
        cover = doc[0].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")
    assert _pngs(run.model.calls[1]) == [expected] and expected != cover


def test_search_hits_and_references_round_trip_from_the_agents_response_model():
    build, chunk = "12345678-1234-4234-8234-123456789abc", "87654321-4321-4234-8234-cba987654321"
    reference = encode_reference(build, chunk, bytes(range(32)))
    served = EvidenceSearchHit(
        chunk_id=chunk, score=0.5, cite="c", heading="h", text="t", doc_id=TABLE_DOC, title="T",
        page_label="iv", chunk_type="table", message_ids=("IEA500I",), product="p", version="v",
        rerank_score=0.25, units=((0, 1, "atomic"),), page_start=3, page_end=4, reference=reference)
    payload = SearchResponse(request_id="r", query_kind="prose", hits=[served]).model_dump(mode="json")

    class Agent:
        def __init__(self):
            self.paths = []

        def post(self, path, json):
            return FakeResponse(200, payload)

        def get(self, path):
            self.paths.append(path)
            return FakeResponse(200, {"source_sha256": "AB" * 32, "location": {
                "physical_page_start": 4, "physical_page_end": 5, "printed_label": "iv"}})

    agent = Agent()
    search = pia.AgentSearch(agent)
    found = search._search("q", None, None, 16)
    assert found.hits == [SearchHit(**served.model_dump(exclude={"reference"}))]
    assert found.kind == "prose" and search.references == {chunk: reference}
    # The reference reaches the evidence route byte for byte; pages become 0-based.
    span = pia.read_source_span(agent, search.references[chunk])
    assert agent.paths == [f"/v1/evidence/{reference}"]
    assert span == pia.SourceSpan("ab" * 32, (3, 4))


def test_dpi_flag_changes_the_rendered_size(tmp_path, corpus):
    run = Run(tmp_path, corpus, [_row("T-1", 1)], argv=["--dpi", "100"]).go()
    assert _ihdr(_pngs(run.model.calls[1])[0]) == (850, 1100, 8, 0)
    assert run.exported["params"]["dpi"] == 100


def test_gold_not_packed_runs_the_text_arm_only_and_is_not_scored(tmp_path, corpus):
    hits = [_hit("d1", DIAGRAM_DOC, chunk_type="syntax", page=1)]
    rows = [_row("T-1", 1), _row("T-2", 2)]
    run = Run(tmp_path, corpus, rows, hits=hits,
              answer=lambda text, images: GOLD if "lever 2 take" in text else "unsure").go()
    assert run.outcomes() == {"T-1": "gold_not_packed_text_fail", "T-2": "gold_not_packed_text_pass"}
    assert len(run.model.calls) == 2 and all(_pngs(c) == [] for c in run.model.calls)
    assert not any(path.startswith("/v1/evidence") for path, _ in run.agent.calls)
    assert run.rc == 1 and run.exported["stop_rule"]["scored"] == 0


@pytest.mark.parametrize(("case", "code"), [
    ("no_file", "unresolved_sha_missing"),
    ("two_files", "unresolved_sha_ambiguous"),
    ("no_reference", "unresolved_reference"),
    ("evidence_404", "unresolved_reference"),
    ("page_out_of_range", "unresolved_page"),
])
def test_an_unresolved_source_is_counted_and_never_guessed(tmp_path, corpus, case, code):
    sha = pia.sha256_file(corpus / "table-guide.pdf")
    spans = {"t1": (sha, 3, 3)}
    hits = [_hit("t1", TABLE_DOC, chunk_type="table", page=2)]
    if case == "no_file":
        # The doc id still names a file in the corpus; only the bytes differ.
        (corpus / "table-guide.pdf").write_bytes(
            (corpus / "table-guide.pdf").read_bytes() + b"\n% changed\n")
        (corpus / f"{TABLE_DOC}.pdf").write_bytes((corpus / "sub" / "diagram-guide.PDF").read_bytes())
    elif case == "two_files":
        (corpus / "copy.pdf").write_bytes((corpus / "table-guide.pdf").read_bytes())
    elif case == "no_reference":
        hits = [{**hits[0], "reference": None}]
    elif case == "evidence_404":
        spans = {}
    else:
        spans = {"t1": (sha, 4, 4)}
    run = Run(tmp_path, corpus, [_row("T-1", 1)], hits=hits, spans=spans).go()
    assert run.outcomes() == {"T-1": code}
    assert run.exported["stop_rule"]["unresolved"] == 1 and run.exported["stop_rule"]["scored"] == 0
    assert len(run.model.calls) == 1 and _pngs(run.model.calls[0]) == []


def test_corpus_hash_is_the_ingest_source_sha256(corpus):
    path = corpus / "table-guide.pdf"
    assert pia.sha256_file(path) == ingest_sha256_file(path)
    index = pia.index_corpus(corpus)
    # Nested directories and an upper-case suffix are walked, like ingest.
    assert sorted(p.name for paths in index.values() for p in paths) == [
        "diagram-guide.PDF", "table-guide.pdf"]


def test_outcomes_follow_the_gold_strings_of_each_arm(tmp_path, corpus):
    replies = {
        "lever 1 take": ("unsure", GOLD),                       # fixed
        "lever 2 take": (GOLD, GOLD),                           # both pass
        "lever 3 take": (GOLD, "unsure"),                       # broken
        "lever 4 take": (GOLD, f"{GOLD}; the {FORBIDDEN} it"),  # broken on a must-not row
        "lever 5 take": ("unsure", "unsure"),                   # both fail
    }

    def answer(text, images):
        return next(v for k, v in replies.items() if k in text)[1 if images else 0]

    rows = [_row(f"T-{n}", n) for n in range(1, 6)]
    rows[2]["gold_must_not_contain"] = []  # T-3 carries no must-not strings
    rows += [
        _row("N-1", 6, expected_behavior="abstain", expected_doc_ids=[], gold_must_contain=[]),
        _row("G-1", 7, gold_must_contain=[]),
        _row("X-1", 8, query_class="diagnostic"),
    ]
    run = Run(tmp_path, corpus, rows, answer=answer).go()
    assert run.outcomes() == {
        "T-1": "fixed", "T-2": "both_pass", "T-3": "broken", "T-4": "broken_must_not",
        "T-5": "both_fail", "N-1": "abstain_row", "G-1": "no_gold_strings"}
    # The abstain row, the row without gold strings and the other class cost no call.
    assert len(run.model.calls) == 10
    rule = run.exported["stop_rule"]
    assert (rule["scored"], rule["text_failures"], rule["fixed"], rule["breaks"],
            rule["breaks_must_not"], rule["excluded"]) == (5, 2, 1, 2, 1, 2)
    assert rule["verdict"] == "stop" and rule["reasons"] == ["few_text_failures", "breaks"]
    # Independent of the tool's own check: the answer eval's judge agrees on
    # which arm misses a gold string or carries a forbidden one.
    for row, entry in zip(run.private[:5], rows[:5], strict=True):
        for arm in ("text", "image"):
            _verdict, failures, _warns = judge(
                {**entry, "expected_behavior": "answer"}, row[f"{arm}_answer"], ["cite"])
            assert row[f"{arm}_missing_gold"] == any("missing required" in f for f in failures)
            assert row[f"{arm}_forbidden"] == any("forbidden substring" in f for f in failures)


def test_a_model_error_in_either_arm_is_its_own_code_and_never_a_broken_answer(tmp_path, corpus):
    def answer(text, images):
        if ("lever 1 take" in text and images) or ("lever 2 take" in text and not images):
            raise RuntimeError(f"upstream said {QUESTION_MARK}")
        return GOLD

    run = Run(tmp_path, corpus, [_row("T-1", 1), _row("T-2", 2), _row("T-3", 3)], answer=answer).go()
    assert run.outcomes() == {"T-1": "image_error", "T-2": "text_error", "T-3": "both_pass"}
    rule = run.exported["stop_rule"]
    assert (rule["errors"], rule["breaks"], rule["text_failures"], rule["scored"]) == (2, 0, 0, 1)


def test_a_failure_outside_the_model_call_keeps_the_run_and_prints_no_exception_text(
        tmp_path, corpus, capsys):
    class BrokenCounter:
        def count_tokens(self, text):
            raise OSError(f"count failed {QUESTION_MARK}")

        def count_messages(self, messages, *, reasoning_effort=None):
            raise OSError(f"count failed {QUESTION_MARK}")

    run = Run(tmp_path, corpus, [_row("T-1", 1), _row("T-2", 2)]).go(tokenizer=BrokenCounter())
    assert run.rc == 1 and run.outcomes() == {"T-1": "text_error", "T-2": "text_error"}
    captured = capsys.readouterr()
    assert QUESTION_MARK not in captured.err + captured.out and "Traceback" not in captured.err


def test_a_broken_row_is_must_not_only_when_the_row_carries_must_not_strings():
    lost = ((False, False), (True, False))
    assert pia.scored_outcome(*lost, must_not_row=True) == "broken_must_not"
    assert pia.scored_outcome(*lost, must_not_row=False) == "broken"
    assert pia.scored_outcome((False, False), (False, True), must_not_row=False) == "broken"


def test_search_failure_and_empty_search_are_their_own_codes(tmp_path, corpus):
    rows = [_row("T-1", 1)]
    failed = Run(tmp_path / "a", corpus, rows)
    failed.agent.search_status = 503
    assert failed.go().outcomes() == {"T-1": "search_failed"}
    assert Run(tmp_path / "b", corpus, rows, hits=[]).go().outcomes() == {"T-1": "no_hits"}
    assert failed.model.calls == []


def test_a_prompt_that_differs_between_the_arms_is_not_scored(tmp_path, corpus, monkeypatch):
    run = Run(tmp_path, corpus, [_row("T-1", 1)])
    core = pia.Tool._core

    async def drifting(self, query, model, search, hits=None, kind=None):
        # The second run packs a different hit list: its prompt text differs.
        return await core(self, query, model, search,
                          hits=None if hits is None else hits[:1], kind=kind)

    monkeypatch.setattr(pia.Tool, "_core", drifting)
    assert run.go().outcomes() == {"T-1": "prompt_mismatch"}
    assert len(run.model.calls) == 1  # the image call never reached the model


# --- the gate and the refusals ------------------------------------------------

def test_a_failing_image_probe_refuses_before_any_call(tmp_path, corpus, capsys):
    run = Run(tmp_path, corpus, [_row("T-1", 1)], probe=lambda: False).go()
    assert run.rc == 3
    assert capsys.readouterr().err.strip() == pia.REFUSALS["probe_failed"]
    assert run.agent.calls == [] and run.model.calls == []
    assert not run.out.exists() and not run.export.exists()


def test_the_probe_is_the_gateway_probe_script_run_for_its_exit_code(tmp_path, monkeypatch):
    assert pia.default_probe_script() == ROOT / "scripts" / "probe_gateway.py"
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1 if len(seen) == 1 else 0)

    monkeypatch.setattr(pia.subprocess, "run", fake_run)
    script = pia.default_probe_script()
    assert pia.run_image_probe(script) is False and pia.run_image_probe(script) is True
    assert seen[0] == [sys.executable, str(script), "--probe-image"]
    with pytest.raises(pia.Refused) as missing:
        pia.run_image_probe(tmp_path / "absent.py")
    assert missing.value.code == "probe_unavailable" and pia.EXIT["probe_unavailable"] == 4


def test_private_report_and_export_paths_are_refused_before_any_call(tmp_path, corpus, capsys):
    for target in ("inside_git", "exists", "holdout_name", "same_file"):
        run = Run(tmp_path / target, corpus, [_row("T-1", 1)])
        out = {"inside_git": ROOT / "1318-private-report.jsonl",
               "exists": run.golden, "holdout_name": run.out.with_name("holdout.jsonl"),
               "same_file": run.export}[target]
        run.argv[run.argv.index("--out") + 1] = str(out)
        assert run.go().rc == 2, target
        assert capsys.readouterr().err.strip() == pia.REFUSALS["output"]
        assert run.agent.calls == [] and run.model.calls == []
        assert not run.export.exists() and not (ROOT / "1318-private-report.jsonl").exists()
    run = Run(tmp_path / "export_in_git", corpus, [_row("T-1", 1)])
    run.argv[run.argv.index("--export") + 1] = str(ROOT / "1318-export.json")
    assert run.go().rc == 2 and run.model.calls == []


def test_the_private_report_is_new_and_owner_only(tmp_path, corpus):
    run = Run(tmp_path, corpus, [_row("T-1", 1)]).go()
    for path in (run.out, run.export):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    (row,) = run.private
    assert ANSWER_MARK in row["text_answer"] and ANSWER_MARK in row["image_answer"]
    assert row["image_pages"] == [
        {"page": 2, "prompt_index": 2, "sha256": pia.sha256_file(corpus / "table-guide.pdf")}]


def test_the_golden_readers_refusals_hold(tmp_path, corpus, capsys):
    holdout = Run(tmp_path / "h", corpus, [_row("T-1", 1)], golden_name="holdout.jsonl")
    candidate = Run(tmp_path / "c", corpus, [_row("T-1", 1, status="candidate")])
    generated = Run(tmp_path / "g", corpus, [_row("T-1", 1, source="generated")])
    release = Run(tmp_path / "r", corpus, [_row("T-1", 1)], golden_name="release_set.jsonl")
    for run in (holdout, candidate, generated, release):
        assert run.go().rc == 2
        assert capsys.readouterr().err.strip() == pia.REFUSALS["dataset"]
        assert run.agent.calls == [] and run.model.calls == [] and not run.out.exists()


def test_other_refusals_are_fixed_messages(tmp_path, corpus, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    run = Run(tmp_path, corpus, [_row("T-1", 1)])
    run.argv[run.argv.index("--corpus-dir") + 1] = str(empty)
    assert run.go().rc == 2 and capsys.readouterr().err.strip() == pia.REFUSALS["corpus"]
    duplicate = Run(tmp_path / "d", corpus, [_row("T-1", 1), _row("T-1", 2)]).go()
    assert duplicate.rc == 2 and capsys.readouterr().err.strip() == pia.REFUSALS["rows"]
    unconfigured = Run(tmp_path / "u", corpus, [_row("T-1", 1)])
    rc = pia.main(unconfigured.argv, settings=Settings(_env_file=None), agent=unconfigured.agent,
                  model=unconfigured.model, probe=lambda: True)
    assert rc == 2 and capsys.readouterr().err.strip() == pia.REFUSALS["model"]
    rc = pia.main(unconfigured.argv, settings=_settings(), probe=lambda: True)
    assert rc == 2 and capsys.readouterr().err.strip() == pia.REFUSALS["agent"]
    assert set(pia.EXIT) == set(pia.REFUSALS)


# --- the export ---------------------------------------------------------------

def test_export_stdout_stderr_and_logs_are_text_free(tmp_path, corpus, capsys, caplog):
    rows = [_row("T-1", 1), _row("T-2", 2, expected_doc_ids=["ZZ00-0000-00"]),
            _row("S-1", 3, query_class="syntax")]
    with caplog.at_level(logging.DEBUG):
        run = Run(tmp_path, corpus, rows).go()
    assert run.rc == 0
    captured = capsys.readouterr()
    export_text = run.export.read_text(encoding="utf-8")
    table_sha = pia.sha256_file(corpus / "table-guide.pdf")
    png_b64 = run.model.calls[1][-1]["content"][1]["image_url"]["url"].split(",", 1)[1]
    for surface in (export_text, captured.out, captured.err, caplog.text):
        for secret in (QUESTION_MARK, ANSWER_MARK, HEADING_MARK, CHUNK_MARK, PAGE_MARK, GOLD,
                       TABLE_DOC, DIAGRAM_DOC, table_sha, "ref/t1", png_b64[:60], "iVBORw0KGgo",
                       str(corpus)):
            assert secret not in surface
    exported = run.exported
    assert set(exported) == {"schema", "params", "validity", "n", "counts", "images", "rows",
                             "stop_rule"}
    assert pia.PAGE_IMAGE_PROTOCOL not in export_text and "Page image" not in export_text
    assert exported["validity"] == {"rows": 3, "gradeable": 3, "with_must_not": 3,
                                    "min_gradeable": 1, "verdict_possible": True}
    assert all(set(r) == {"id", "outcome"} for r in exported["rows"])
    assert {r["outcome"] for r in exported["rows"]} <= set(pia.OUTCOMES)
    assert set(exported["counts"]) == set(pia.OUTCOMES)
    assert sum(exported["counts"].values()) == exported["n"] == 3
    assert exported["images"] == {"rows": 2, "n": 2, "bytes": 2 * len(base64.b64decode(png_b64)),
                                  "rows_hit_not_qualifying": 0, "prompt_token_delta_mean": 2700}
    assert exported["params"]["classes"] == ["table", "syntax"]
    assert "model" not in exported["params"]
    used = exported["params"]["settings"]
    assert used == json.loads(json.dumps({n: getattr(_settings(), n) for n in pia.PACKING_SETTINGS}))
    assert used["answer_hit_limit"] == 16 and used["prompt_order"] == "retrieval"
    assert not any(part in name for name in used for part in ("key", "url", "model_reasoning",
                                                              "secret", "extra_body"))
    assert exported["params"]["expansion"] == "not_reproduced"
    assert set(exported["stop_rule"]["reasons"]) <= set(pia.STOP_REASONS)
    assert json.loads(captured.out)["stop_rule"] == exported["stop_rule"]


# --- row validity ---------------------------------------------------------------

@pytest.mark.parametrize(("gradeable", "rc", "verdict"), [(5, 5, "no_verdict"), (6, 0, "continue")])
def test_no_verdict_is_printed_when_fewer_than_six_rows_could_ever_fail(
        tmp_path, corpus, capsys, monkeypatch, gradeable, rc, verdict):
    monkeypatch.setattr(pia, "MIN_GRADEABLE_ROWS", 6)
    rows = [_row(f"T-{n}", n) for n in range(1, gradeable + 1)]
    rows[0]["gold_must_not_contain"] = []
    rows += [_row("G-1", 20, gold_must_contain=[]),
             _row("N-1", 21, expected_behavior="abstain", expected_doc_ids=[], gold_must_contain=[])]
    run = Run(tmp_path, corpus, rows).go()
    err = capsys.readouterr().err
    assert run.rc == rc and run.exported["stop_rule"]["verdict"] == verdict
    assert run.exported["validity"] == {
        "rows": gradeable + 2, "gradeable": gradeable, "with_must_not": gradeable - 1,
        "min_gradeable": 6, "verdict_possible": gradeable >= 6}
    assert f"{gradeable + 2} selected, {gradeable} gradeable" in err
    assert ("NO VERDICT" in err) == (gradeable < 6) and ("VERDICT continue" in err) == (gradeable >= 6)
    if gradeable < 6:
        assert run.exported["stop_rule"]["reasons"] == ["too_few_gradeable_rows"]
        # The counts are still exported: the rows ran.
        assert run.exported["stop_rule"]["fixed"] == gradeable
    assert run.exported["stop_rule"]["verdict"] in pia.VERDICTS


# --- the stop rule ------------------------------------------------------------

@pytest.mark.parametrize(("counts", "verdict", "reasons"), [
    # 6 text failures, a third fixed, no break: the rule does not stop.
    ({"fixed": 2, "both_fail": 4}, "continue", []),
    ({"fixed": 2, "both_fail": 3}, "stop", ["few_text_failures"]),          # 5 failures
    ({"fixed": 1, "both_fail": 5}, "stop", ["few_fixed"]),                   # 1/6 < 1/3
    ({"fixed": 2, "both_fail": 5}, "stop", ["few_fixed"]),                   # 2/7 < 1/3
    ({"fixed": 3, "both_fail": 6}, "continue", []),                          # exactly 1/3
    ({"fixed": 6, "broken": 1}, "continue", []),                             # one break allowed
    ({"fixed": 6, "broken": 2}, "stop", ["breaks"]),
    ({"fixed": 6, "broken_must_not": 1}, "stop", ["breaks"]),                # any must-not break
    ({"fixed": 6, "gold_not_packed_text_fail": 6}, "continue", []),          # half, not most
    ({"fixed": 6, "gold_not_packed_text_fail": 7}, "stop", ["mostly_not_packed"]),
    ({"fixed": 6, "gold_not_packed_text_pass": 30, "both_pass": 9}, "continue", []),
    ({}, "stop", ["few_text_failures"]),
    ({"both_fail": 6, "broken": 2, "gold_not_packed_text_fail": 7}, "stop",
     ["few_fixed", "breaks", "mostly_not_packed"]),
])
def test_stop_rule_boundaries(counts, verdict, reasons):
    rule = pia.stop_rule(counts)
    assert (rule["verdict"], rule["reasons"]) == (verdict, reasons)


# --- the licence boundary -----------------------------------------------------

def test_importing_and_scoring_never_load_pymupdf_only_rendering_does(tmp_path):
    code = (
        "import sys\n"
        "from mainframe_rag.eval import page_image_arm as pia\n"
        "pia.stop_rule({'fixed': 6}); pia.select_rows(b'', ['table'])\n"
        "assert pia.scored_outcome((True, False), (False, False)) == 'fixed'\n"
        "assert 'pymupdf' not in sys.modules and 'fitz' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"}, check=False)
    assert done.returncode == 0, done.stderr[-400:]
    source = (ROOT / "src/mainframe_rag/eval/page_image_arm.py").read_text(encoding="utf-8")
    assert source.count("import pymupdf") == 1
    assert source.index("def render_page_png") < source.index("import pymupdf") < source.index(
        "def sha256_file")
