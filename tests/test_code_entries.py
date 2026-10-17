"""Code-entry detection tests (issue #591).

Tests for the ingest-side code-entry splitting: a line that is exactly
a 3-hex code, followed within two lines by description text rather than
another bare code. This excludes index runs.
"""

from mainframe_rag.ingest.chunk import (
    _canonical_code,
    _code_entries,
    _extract_system_codes,
    _is_code_entry_start,
)


def test_code_entries_split_into_atomic_entries() -> None:
    result = _code_entries("0C4\nData exception\n\n0C5\nProtection exception\n\n0C6\nAddressing exception")
    assert result is not None
    assert len(result) == 3
    assert all(item[1] for item in result)  # all atomic
    assert [item[0][:3] for item in result] == ["0C4", "0C5", "0C6"]
    # A blank line before the description still opens an entry.
    blank = _code_entries("0C4\n\nData exception")
    assert blank is not None
    assert [(t[:3], atomic) for t, atomic in blank] == [("0C4", True)]
    # Prose before and after is kept as non-atomic text; trailing prose rides with the last entry.
    mixed = _code_entries(
        "System completion codes:\n\n0C4\nData exception\n\n0C5\nProtection exception\n\nSee also the manual."
    )
    assert mixed is not None
    assert [atomic for _, atomic in mixed] == [False, True, True]
    assert _code_entries("The following codes:\n0C4\nData exception") is not None


def test_index_runs_and_code_like_words_are_not_entries() -> None:
    assert _code_entries("0C4\n0C5\n0C6\n0C7") is None
    assert _code_entries("This is a regular paragraph with no codes.") is None
    assert _code_entries("The value is 0C4 in hex.") is None
    # An index run followed by a description: only the last code opens an entry.
    run = _code_entries("0C4\n0C5\n0C6\nData exception")
    assert run is not None
    assert [(t[:3], atomic) for t, atomic in run][-1] == ("0C6", True)
    assert run[0][1] is False
    # Description must follow within two lines.
    assert _is_code_entry_start("0C4\n\nData exception".splitlines(), 0) is True
    assert _is_code_entry_start("0C4\n\n\nData exception".splitlines(), 0) is False


def test_every_entry_code_is_recorded_not_just_the_first() -> None:
    """A block packs several entries; recording only line 0 captured 16% of
    the codes in a real system-codes manual and missed 0C4 entirely
    (review #603)."""
    text = "0C4\nProtection exception.\n\n0C5\nOperator intervention.\n\n0C7\nData exception."
    assert _extract_system_codes(text) == ["0C4", "0C5", "0C7"]
    # The chunk need not open with an entry for later entries to be recorded.
    text = "Chapter overview paragraph.\n\n0C4\nProtection exception.\n\n0C5\nOperator intervention."
    assert _extract_system_codes(text) == ["0C4", "0C5"]
    assert _extract_system_codes("0C4\n0C5\n0C6\n0C7") == []  # index run
    # Ingest stores the form the query parser emits (review #603).
    assert _extract_system_codes("U4038\nUser abend raised by the Language Environment.") == ["U4038"]
    # Bare 3-hex is a completion code except in a wait-state section.
    wait = "064\nAn address that is being waited on."
    assert _extract_system_codes(wait, wait_state=True) == ["W064"]
    assert _extract_system_codes(wait, wait_state=False) == ["064"]


def _make_chunks(text: str, heading: str):
    from pathlib import Path

    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    parsed = ParsedDoc(
        path=Path("synthetic.pdf"),
        sha256="deadbeef",
        doc_id="SA99-0000-00",
        title=heading,
        toc=((1, heading, 1),),
        page_count=1,
    )
    return make_chunks(parsed, [text])


# Real code sections label every entry (#621: 812 of 812 in a system-codes
# manual); the label is what marks the section as a code section.
CODE_MANUAL = (
    "System completion codes\n\n"
    "0C4\nExplanation:\nA protection exception occurred during the operation.\n\n"
    "0C5\nExplanation:\nOperator intervention is required before retrying.\n\n"
    "0C7\nExplanation:\nA data exception occurred.\n"
)


def test_make_chunks_records_every_entry_without_touching_text():
    """End-to-end: the payload lists every entry the chunk holds, so the
    system_codes filter can select an entry (review #603). The operator
    alias is never injected into the stored text, and unit spans stay
    aligned with it."""
    chunks = _make_chunks(CODE_MANUAL, "System completion codes")
    recorded = {code for c in chunks for code in c.system_codes}
    assert {"0C4", "0C5", "0C7"} <= recorded
    assert all("0C4" in c.system_codes for c in chunks if "0C4" in c.text)
    for chunk in chunks:
        for line in chunk.text.splitlines():
            # An injected alias would stand alone as S<code> on its own line.
            assert not (len(line) == 4 and line[0] == "S" and line[1:] in recorded)
        for span in chunk.units or ():
            assert 0 <= span.start < span.end <= len(chunk.text)
            assert chunk.text[span.start : span.end].strip()
    wait = "Wait states\n\n064\nExplanation:\nThe system is waiting on an address that is protected.\n"
    assert [c.system_codes for c in _make_chunks(wait, "Wait states") if c.system_codes] == [["W064"]]


def test_query_and_payload_agree_on_every_code_family():
    """Producer-to-consumer round-trip (review #603): the string the query
    parser emits must equal the string ingest stores, or the MatchAny filter
    can never match and silently falls back for that whole family. The
    filter must key on the field ingest actually writes."""
    from mainframe_rag.retrieve.filters import build_filter, parse_query, query_kind

    cases = {
        "What does abend S0C4 mean and what are common causes?": "0C4",
        "abend 0C7": "0C7",
        "abend S80A what should I check": "80A",
        "user completion code U4038": "U4038",
        "abend 806": "806",
        "abend SAFB": "AFB",
    }
    for query, code in cases.items():
        assert parse_query(query).system_codes == [code]
        # Ingest stores the identical canonical form for a completion-code entry.
        assert _canonical_code(code, wait_state=False) == code
    assert _canonical_code("u4038", False) == "U4038"
    # Wait states only round-trip inside a wait-state section: a completion
    # code section stores the bare form, so W064 must not be promised there.
    assert parse_query("wait state 064").system_codes == ["W064"]
    assert _canonical_code("064", wait_state=True) == "W064"
    ids = parse_query("What does abend S0C4 mean?")
    assert query_kind(ids) == "identifier"
    flt = build_filter(ids)
    assert flt is not None
    clause = next(c for c in flt.must if c.key == "system_codes")
    assert clause.match.any == ["0C4"]


# --- chrome stripping must not eat code-entry lines (issue #604) ------------

_ENTRY_CODES = ["001", "806", "064", "0C4"]


def _entry_pages(n_pages: int = 12) -> list[str]:
    """Generated multi-page code manual. Each page ends in a range footer, a
    running chapter footer and a folio; entry code lines land at the page top
    and the page middle, and the labels land at varying line offsets (a label
    at the same edge position on most pages would be a real running header)."""
    leads = [6, 0, 8, 3, 10, 5, 9, 7, 1, 12, 4, 11]
    pages = []
    for i in range(n_pages):
        code = _ENTRY_CODES[i % len(_ENTRY_CODES)]
        lead = [
            f"Continued text of the previous entry, line {k}." for k in range(leads[i % len(leads)])
        ]
        body = [
            code,
            "Explanation:",
            f"Generated explanation for entry {code} on page {i}.",
            "System action:",
            "The generated job step ends.",
            "System programmer response:",
            "Correct the generated input and run the job again.",
        ]
        footer = [
            f"{code} \u2022 {_ENTRY_CODES[(i + 1) % 4]}",
            "Chapter 2. Generated codes",
            str(i + 1),
        ]
        pages.append("\n".join(lead + body + footer))
    return pages


def test_strip_chrome_keeps_entry_lines_and_strips_only_chrome():
    from mainframe_rag.ingest.chrome import strip_chrome

    pages = _entry_pages()
    for page, code in zip(strip_chrome(pages), _ENTRY_CODES * 3, strict=True):
        lines = page.splitlines()
        assert code in lines
        assert lines[lines.index(code) + 1] == "Explanation:"
        assert "System action:" in lines
        assert "System programmer response:" in lines
        assert "Chapter 2. Generated codes" not in page
        assert "\u2022" not in page  # the range footer
        assert not any(ln.isdigit() and len(ln) <= 2 for ln in lines)  # folios
    # A running header is stripped while the entry labels stay.
    for page in strip_chrome([f"Generated Codes Manual\n{p}" for p in pages]):
        assert "Generated Codes Manual" not in page
        assert "Explanation:" in page


def test_make_chunks_after_strip_chrome_records_every_entry_code():
    """Round trip through the ingest path (strip_chrome -> make_chunks): the
    stored system_codes field contains 806 and 064, whose bare lines used to
    be deleted as folios (issue #604)."""
    from pathlib import Path

    from mainframe_rag.ingest.chrome import strip_chrome
    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    pages = _entry_pages()
    parsed = ParsedDoc(
        path=Path("synthetic.pdf"),
        sha256="deadbeef",
        doc_id="SA99-0000-00",
        title="System completion codes",
        toc=((1, "System completion codes", 1),),
        page_count=len(pages),
    )
    chunks = make_chunks(parsed, strip_chrome(pages))
    recorded = {code for c in chunks for code in c.system_codes}
    assert {"001", "806", "0C4"} <= recorded
    assert all("806" in c.system_codes for c in chunks if "\n806\n" in f"\n{c.text}\n")
    assert any("\n806\n" in f"\n{c.text}\n" for c in chunks)


# --- system_codes only from code sections (issue #621) ----------------------


def _parsed(toc: tuple, n_pages: int):
    from pathlib import Path

    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    return ParsedDoc(
        path=Path("synthetic.pdf"),
        sha256="deadbeef",
        doc_id="SA99-0000-00",
        title="Generated Codes Manual",
        toc=toc,
        page_count=n_pages,
    )


# Generated three-chapter code manual: labelled entries (one of them a family
# entry whose sub-codes carry a description but no label of their own), a
# code-to-module table, and back-of-book index pages.
_CODES_CHAPTER = (
    "0C1\nExplanation:\nA program interruption occurred. The code identifies its kind:\n"
    "0C4\nA protection exception occurred.\n"
    "0C7\nA data exception occurred.\n\n"
    "806\nExplanation:\nThe requested load module was not found.\n"
)
_MODULE_TABLE = "101\nGENMOD01\n\n122\nGENMOD02\n\n806\nGENMOD03\n"
_INDEX = "abend codes\n101\nsee completion codes\n\n122\nsystem action\n"


def _three_chapter_chunks():
    from mainframe_rag.ingest.chunk import make_chunks

    toc = (
        (1, "Chapter 2. Completion codes", 1),
        (1, "Chapter 3. Completion code to module table", 2),
        (1, "Chapter 4. Code lookup", 3),
    )
    return make_chunks(_parsed(toc, 3), [_CODES_CHAPTER, _MODULE_TABLE, _INDEX])


def test_code_section_gate_records_labelled_sections_only():
    """0C4 and 0C7 sit inside the 0C1 family entry with no label of their
    own (the real manual's shape); a per-entry label rule dropped them, which
    is why the gate is per section. A module table and index pages carry
    code-shaped lines followed by text but no labelled entry, so their chunks
    record no codes (issue #621) and are not split into atomic code entries."""
    chunks = _three_chapter_chunks()
    codes = {c for ch in chunks if ch.heading_path.startswith("Chapter 2") for c in ch.system_codes}
    assert codes == {"0C1", "0C4", "0C7", "806"}
    others = [ch for ch in chunks if not ch.heading_path.startswith("Chapter 2")]
    assert {ch.heading_path for ch in others} == {
        "Chapter 3. Completion code to module table",
        "Chapter 4. Code lookup",
    }
    assert all(ch.system_codes == [] for ch in others)
    # The same text the gate rejects is still code-shaped to the extractor:
    # the empty payload comes from the section gate, not from the text.
    assert any(_extract_system_codes(ch.text) for ch in others)
    for chunk in others:
        for span in chunk.units:
            unit = chunk.text[span.start : span.end]
            assert not (span.kind == "atomic" and unit.splitlines()[0] in {"101", "122", "806"})


def test_label_opening_the_next_page_confirms_the_section():
    from mainframe_rag.ingest.chunk import make_chunks

    pages = ["Overview of the generated codes.\n\n0C4", "Explanation:\nA protection exception."]
    chunks = make_chunks(_parsed(((1, "Completion codes", 1),), 2), pages)
    assert "0C4" in {c for ch in chunks for c in ch.system_codes}


# A dump listing shaped like an LE/C dump report: a bare decimal value line,
# a field path, a type. Before #612 the bare numbers were stripped as folios;
# after it they opened one code "entry" that ran to the end of the paragraph
# (5,813 chars, 4,081 tokens on a real manual) and overflowed the embed window.
# One 3-digit value opens the "entry"; the values after it are 1-2 digits,
# which are not code-shaped, so nothing closes it until the paragraph ends.
_DUMP_LINES = ["255"]
for _i in range(240):
    _DUMP_LINES += [f"*.*.C(SAMPLE{_i:03d}):>field_{_i}", "signed int", str(_i % 90)]
_DUMP = "\n".join(_DUMP_LINES)


def test_dump_listing_outside_a_code_section_stays_within_the_cap():
    from mainframe_rag.ingest.chunk import SECTION_MAX_CHARS, SPLIT_OVERLAP_CHARS, make_chunks

    chunks = make_chunks(_parsed(((1, "Diagnosing dump output", 1),), 1), [_DUMP])
    assert len(_DUMP) > 2 * SECTION_MAX_CHARS
    assert max(len(c.text) for c in chunks) <= SECTION_MAX_CHARS + SPLIT_OVERLAP_CHARS
    assert all(c.system_codes == [] for c in chunks)


def test_oversize_entry_in_a_code_section_is_cut_at_line_boundaries():
    """A real code section can hold a very long entry: it is cut at line
    boundaries so no chunk overflows the embed window, the code stays
    recorded, and no line is lost or sliced."""
    from mainframe_rag.ingest.chunk import (
        SECTION_MAX_CHARS,
        SPLIT_OVERLAP_CHARS,
        _cap_entry,
        make_chunks,
    )

    body = "\n".join(
        f"Generated reason-code explanation line {i} for this entry." for i in range(200)
    )
    entry = f"0C4\nExplanation:\n{body}"
    pieces = _cap_entry(entry)
    assert len(pieces) > 1
    assert pieces[0].startswith("0C4\nExplanation:")
    assert all(len(p) <= SECTION_MAX_CHARS for p in pieces)
    assert "\n".join(pieces).splitlines() == entry.splitlines()

    chunks = make_chunks(_parsed(((1, "Completion codes", 1),), 1), [entry])
    assert max(len(c.text) for c in chunks) <= SECTION_MAX_CHARS + SPLIT_OVERLAP_CHARS
    assert "0C4" in {code for c in chunks for code in c.system_codes}
