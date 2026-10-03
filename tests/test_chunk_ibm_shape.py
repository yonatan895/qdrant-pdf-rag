"""Chunk contract tests: outline sections, chunk_id stability, classification."""

import re

import pytest

from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import make_chunks, outline_sections
from mainframe_rag.ingest.ibm_pdf import parse_pdf


def _chunks_for(synthetic_pdf):
    import pymupdf

    parsed = parse_pdf(synthetic_pdf)
    doc = pymupdf.open(synthetic_pdf)
    try:
        page_texts = [p.get_text() for p in doc]
        labels = [p.get_label() for p in doc]
    finally:
        doc.close()
    stripped = strip_chrome(page_texts)
    return parsed, make_chunks(parsed, stripped, labels)


def test_outline_maps_sections(synthetic_pdf):
    parsed = parse_pdf(synthetic_pdf)
    sections = outline_sections(parsed)
    paths = [s.heading_path for s in sections]
    # Front matter (Contents, Figures on early pages) is skipped.
    assert not any(p.startswith("Contents") for p in paths)
    assert any("Chapter 1 System parameters" in p for p in paths)
    # Nested bookmark builds a > separated path.
    assert any("Chapter 1 System parameters > IEASYSxx parameters" in p for p in paths)


def test_notice_section_skipped(synthetic_pdf):
    parsed = parse_pdf(synthetic_pdf)
    sections = outline_sections(parsed)
    assert not any("Notices" in s.heading_path for s in sections)


def test_chunk_id_stable_across_runs(synthetic_pdf):
    _, first = _chunks_for(synthetic_pdf)
    _, second = _chunks_for(synthetic_pdf)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


def test_message_chunk_extracts_ids_and_members(synthetic_pdf):
    _, chunks = _chunks_for(synthetic_pdf)
    msg_chunks = [c for c in chunks if "IEA500I" in c.text]
    assert msg_chunks, "synthetic IEA500I section must be chunked"
    assert any("IEA500I" in c.message_ids for c in msg_chunks)
    assert any(c.chunk_type == "message" for c in chunks)
    assert any("IEASYSxx" in c.members for c in chunks if "IEASYSxx" in c.text)


def test_page_label_and_start(synthetic_pdf):
    _, chunks = _chunks_for(synthetic_pdf)
    msg = next(c for c in chunks if "IEA500I" in c.text)
    assert msg.page_start == 5
    # Issue #577: deepest-section rule — the IEA500I page is chunked once
    # under the deepest entry, not duplicated under its Chapter 2 ancestor.
    # The section covers index page 5 only (printed 1-6; next section starts
    # at 1-based page 7), so the label is single, not a range.
    assert msg.heading_path == "Chapter 2 Operator messages > IEA500I"
    assert msg.page_label == "1-6"


def test_long_section_split_with_overlap():
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    parsed = ParsedDoc(
        path=__import__("pathlib").Path("synthetic.pdf"),
        sha256="0" * 64,
        doc_id="SA22-0000-00",
        title="Synthetic",
        product="z/OS",
        version="9.9",
        vendor="IBM",
        toc=[[1, "Long section", 1]],
        page_count=1,
    )
    long_text = "\n\n".join(f"Paragraph {i} " + "x" * 120 for i in range(120))
    chunks = make_chunks(parsed, [long_text], ["1-1"])
    assert len(chunks) > 1
    # 400-char overlap: the head of chunk N-1's tail appears in chunk N.
    body0, body1 = chunks[0].text, chunks[1].text
    assert body1[:50] == body0[-450:-400] or body1[:400] in body0
    # ordinals are sequential
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


# Code-atomic chunking (issue #79): JCL cards, REXX programs, and
# monospaced console blocks split at statement boundaries only, never
# mid-statement. Detector lives in chunk.py (a splitting decision, not a
# chunk_type); the message/syntax/table/narrative vocabulary is unchanged.


def test_detect_code_region_matrix():
    from mainframe_rag.ingest.chunk import detect_code_region

    assert detect_code_region("//STEP1 EXEC PGM=IEFBR14\n//DD1 DD DSN=X,DISP=SHR") == "jcl"
    assert detect_code_region("//* comment\n//A B\n//  continuation") == "jcl"
    assert detect_code_region("/* REXX */\nSAY hello;") == "rexx"
    assert detect_code_region("/* opens here\nstill comment\n*/ done\nX = 1;") == "rexx"
    assert detect_code_region("  READY\n  IKJ56250I JOB DONE\n  SHOW DSN") == "console"
    assert detect_code_region("") is None
    assert detect_code_region("Plain narrative prose about system parameters.") is None
    # Adversarial negatives: URL-heavy prose and complete /* */ mentions
    # must not trip the detector (misses fall back to paragraph behavior).
    assert detect_code_region("See https://example.com/docs for details\non the layout.") is None
    assert detect_code_region("Use /*comment*/ style sparingly in prose.") is None


def test_jcl_statement_grouping():
    from mainframe_rag.ingest.chunk import _split_jcl_statements

    text = (
        "//STEP1 EXEC PGM=IEFBR14\n"
        "//DD1 DD DSN=X,DISP=(NEW,CATLG,DELETE),\n"
        "//            UNIT=SYSDA,SPACE=(CYL,(1,1),RLSE)\n"
        "//* a comment\n"
        "//STEP2 EXEC PGM=SORT"
    )
    assert _split_jcl_statements(text) == [
        "//STEP1 EXEC PGM=IEFBR14",
        "//DD1 DD DSN=X,DISP=(NEW,CATLG,DELETE),\n//            UNIT=SYSDA,SPACE=(CYL,(1,1),RLSE)",
        "//* a comment",
        "//STEP2 EXEC PGM=SORT",
    ]


def test_rexx_comment_quote_and_continuation():
    from mainframe_rag.ingest.chunk import _split_rexx_statements

    text = (
        "/* REXX */\n"
        "/* multi-line\n"
        "   block comment; with semicolon */\n"
        'SAY "Open failed, RC="rc"; aborting.";\n'
        "total = a + b + ,\n"
        "  c + d;\n"
        "EXIT 0;"
    )
    statements = _split_rexx_statements(text)
    assert statements[0] == "/* REXX */"
    assert "/* multi-line\n   block comment; with semicolon */" in statements
    assert 'SAY "Open failed, RC="rc"; aborting.";' in statements
    assert "total = a + b + ,\n  c + d;" in statements
    assert statements[-1] == "EXIT 0;"


def test_rexx_unterminated_comment_swallows_to_end():
    from mainframe_rag.ingest.chunk import _split_rexx_statements

    statements = _split_rexx_statements("X = 1;\n/* never closed\nY = 2;\nZ = 3;")
    assert statements == ["X = 1;", "/* never closed\nY = 2;\nZ = 3;"]


def _jcl_source_statements():
    from scripts.make_synthetic_pdf import JCL_BASE_LINES

    from mainframe_rag.ingest.chunk import _split_jcl_statements

    return _split_jcl_statements("\n".join(JCL_BASE_LINES))


def test_jcl_fixture_statements_never_split(jcl_pdf):
    from mainframe_rag.ingest.chunk import detect_code_region

    _, chunks = _chunks_for(jcl_pdf)
    assert len(chunks) > 1, "the generated region must force splits or the test is vacuous"
    texts = [c.text for c in chunks]
    full = "\n".join(texts)
    for statement in _jcl_source_statements():
        assert statement in full, f"statement split across chunks: {statement[:60]!r}"
    # Every chunk starts at a statement boundary: the first JCL-looking
    # line of a chunk (after stripping the manual's left pad) is always a
    # new statement or unnamed op, never a col-16 continuation.
    cont_re = re.compile(r"^//\s{2,}")
    for chunk in chunks:
        first_jcl = next(
            (ln.lstrip() for ln in chunk.text.splitlines() if ln.lstrip().startswith("//")),
            None,
        )
        if first_jcl is not None:
            assert not cont_re.match(first_jcl), f"chunk opens mid-statement: {first_jcl[:40]!r}"
    # Detector fires on the real extraction (line starts survived the PDF round-trip).
    assert any(
        detect_code_region("\n".join(ln for ln in c.text.splitlines() if ln.startswith("//")))
        == "jcl"
        for c in chunks
        if any(ln.startswith("//") for ln in c.text.splitlines())
    )


def test_jcl_fixture_ids_stable(jcl_pdf):
    _, first = _chunks_for(jcl_pdf)
    _, second = _chunks_for(jcl_pdf)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


def test_indented_jcl_detected_grouped_and_intact(jcl_pdf):
    """Blocker 1 (review): manuals indent examples; the detector and the
    splitter work on lstripped cards, continuations stay glued across the
    indent, and the unnamed op stands alone."""
    from mainframe_rag.ingest.chunk import _split_jcl_statements, detect_code_region

    indented = (
        "    //EXJOB JOB (ACCT),'EXAMPLE',CLASS=A\n"
        "    //OUTDATA DD DSN=EXAMPLE.OUTPUT,DISP=(NEW,CATLG,DELETE),\n"
        "    //            UNIT=SYSDA,SPACE=(CYL,(2,1),RLSE)\n"
        "    // EXEC PGM=IKJEFT01"
    )
    assert detect_code_region(indented) == "jcl"
    assert _split_jcl_statements(indented) == [
        "    //EXJOB JOB (ACCT),'EXAMPLE',CLASS=A",
        (
            "    //OUTDATA DD DSN=EXAMPLE.OUTPUT,DISP=(NEW,CATLG,DELETE),\n"
            "    //            UNIT=SYSDA,SPACE=(CYL,(2,1),RLSE)"
        ),
        "    // EXEC PGM=IKJEFT01",
    ]
    _, chunks = _chunks_for(jcl_pdf)
    full = "\n".join(c.text for c in chunks)
    assert "    //OUTDATA DD DSN=EXAMPLE.OUTPUT,DISP=(NEW,CATLG,DELETE),\n    //            UNIT=SYSDA" in full
    assert "    // EXEC PGM=IKJEFT01" in full


def test_unnamed_ops_are_statements_not_continuations():
    from mainframe_rag.ingest.chunk import _split_jcl_statements

    assert _split_jcl_statements("// EXEC PGM=X\n// DD DSN=Y") == ["// EXEC PGM=X", "// DD DSN=Y"]


def test_wrapped_card_rejoins_across_lines():
    """Real IBM manuals wrap `//LABEL=params` as `//` + newline + params
    (dfha3b08 Figure 12): the pair rejoins into the true card instead of a
    dangling `//` plus a phantom prose unit."""
    from mainframe_rag.ingest.chunk import _split_jcl_statements

    assert _split_jcl_statements("//\nASMBLR=ASMA90,") == ["//ASMBLR=ASMA90,"]
    assert _split_jcl_statements("//JOB JOB (A),\n//\nINDEX=X,") == ["//JOB JOB (A),", "//INDEX=X,"]


def test_bare_slash_slash_does_not_glue_without_parameter():
    """The `=` guard: null statements, delimiters, and SYSIN data after a
    bare `//` stay split — only wrapped parameter cards rejoin."""
    from mainframe_rag.ingest.chunk import _split_jcl_statements

    assert _split_jcl_statements("//\nSome prose here") == ["//", "Some prose here"]
    assert _split_jcl_statements("//\n//NEXT JOB") == ["//", "//NEXT JOB"]
    assert _split_jcl_statements("//\n/*") == ["//", "/*"]
    assert _split_jcl_statements("//X DD *\nENTRY prog") == ["//X DD *", "ENTRY prog"]
    assert _split_jcl_statements("trailing\n//") == ["trailing", "//"]


def test_instream_data_splits_between_lines_not_as_atom():
    """Blocker 2 (review): a 4000-char SYSIN block must split between data
    lines; only // cards stay continuation-atomic."""
    from mainframe_rag.ingest.chunk import SECTION_MAX_CHARS, _split_blocks

    data = [f"RECORD-{i:04d} PAYLOAD-DATA-LINE" for i in range(200)]
    para = "//SYSIN DD *\n" + "\n".join(data)
    assert len(para) > SECTION_MAX_CHARS
    blocks = _split_blocks([(0, para)])
    assert len(blocks) > 1
    for _, _, text in blocks:
        assert len(text) <= SECTION_MAX_CHARS + 100
    assert blocks[0][2].startswith("//SYSIN DD *")
    joined = "\n".join(text for _, _, text in blocks)
    for line in data:
        assert line in joined


def test_rexx_nested_comments_dont_split():
    from mainframe_rag.ingest.chunk import _split_rexx_statements

    statements = _split_rexx_statements("/* outer /* inner ; */ still comment ; */\nX = 1;")
    assert statements == ["/* outer /* inner ; */ still comment ; */", "X = 1;"]


def test_code_runs_share_single_newlines():
    """Review: adjacent atomic items join with one newline (exact-card
    sparse fidelity); prose boundaries keep the double newline."""
    from mainframe_rag.ingest.chunk import _split_blocks

    (page, text), = [(0, "//A X\n//B Y")]
    assert _split_blocks([(page, text)]) == [(0, 0, "//A X\n//B Y")]
    prose = _split_blocks([(0, "para one"), (0, "para two")])
    assert prose == [(0, 0, "para one\n\npara two")]


def test_rexx_fixture_statements_never_split(rexx_pdf):
    from scripts.make_synthetic_pdf import REXX_LINES

    from mainframe_rag.ingest.chunk import _split_rexx_statements

    _, chunks = _chunks_for(rexx_pdf)
    assert chunks, "REXX fixture must produce chunks"
    full = "\n".join(c.text for c in chunks)
    for statement in _split_rexx_statements("\n".join(REXX_LINES)):
        assert statement in full, f"statement split across chunks: {statement[:60]!r}"
    # The multi-line block comment lives whole in exactly the chunks the
    # overlap duplicates — never sliced: every occurrence is complete.
    comment = "/* Open and validate; RC must be 0\n   before the summary step runs. */"
    assert comment in full
    _, again = _chunks_for(rexx_pdf)
    assert [c.chunk_id for c in chunks] == [c.chunk_id for c in again]


def test_oversize_code_para_splits_at_statement_starts():
    from mainframe_rag.ingest.chunk import SECTION_MAX_CHARS, _split_blocks

    statements = [f"//S{i:02d} EXEC PGM=IEFBR14,PARM='PHASE-{i:02d}'" for i in range(120)]
    para = "\n".join(statements)
    assert len(para) > SECTION_MAX_CHARS
    blocks = _split_blocks([(0, para)])
    assert len(blocks) > 1
    joined = "\n".join(text for _, _, text in blocks)
    for statement in statements:
        assert statement in joined
    # Every piece opens with a statement start (overlap seeds may carry a
    # leading separator; strip it before checking — a sliced card would
    # still fail this assertion).
    for _, _, text in blocks:
        opening = text.lstrip()
        assert opening.startswith(("//S", "//*"))


def test_oversize_single_statement_emitted_whole():
    from mainframe_rag.ingest.chunk import SECTION_MAX_CHARS, _split_blocks

    giant = "//LONG EXEC PGM=X,PARM='" + "Y" * (SECTION_MAX_CHARS + 500) + "'"
    blocks = _split_blocks([(0, giant)])
    assert len(blocks) == 1
    assert blocks[0][2] == giant


def test_overlap_backoff_keeps_statements_whole():
    """Constructed arithmetic: prose 2000 + six 300-char JCL statements.
    The 400-char overlap tail starts inside S3, so the seed backs off to
    whole trailing items and the next block opens with all of S4
    (stmts[3]) followed by S5, joined with single newlines (code runs
    share one newline, never a blank line)."""
    from mainframe_rag.ingest.chunk import _split_blocks

    prose = "y" * 2000
    stmts = [f"//S{i:02d}  " + "X" * (300 - 7) for i in range(1, 7)]
    assert all(len(s) == 300 for s in stmts)
    code_para = "\n".join(stmts)
    blocks = _split_blocks([(0, prose), (0, code_para)])
    assert len(blocks) == 2
    assert blocks[0][2].endswith(stmts[3])
    assert blocks[1][2].startswith(stmts[3] + "\n" + stmts[4])
    for statement in stmts:
        assert statement in blocks[0][2] or statement in blocks[1][2]


# Issue #216: table/syntax fidelity — atomic blocks, message detector,
# page labels. Table rows are atomic like code statements (row-boundary
# splits, whole-row overlap backoff); the 0.6 column rule is shared with
# classify.is_table_block, never redefined here.


def _table_para(n_rows: int = 120) -> str:
    header = "Parameter   Meaning              Default"
    rows = [f"PARM{i:03d}      Description text {i:03d}      VALUE{i:03d}" for i in range(n_rows)]
    return "\n".join([header, *rows])


def test_detect_table_region_matrix():
    from mainframe_rag.ingest.chunk import detect_table_region

    assert detect_table_region(_table_para(4)) is True
    # Code wins over columns: JCL continuations carry wide indents that
    # read as columns, but the splitter must treat them as statements.
    jcl = "//DD1 DD DSN=X,DISP=(NEW,CATLG,DELETE),\n//            UNIT=SYSDA,SPACE=(CYL,(1,1))"
    assert detect_table_region(jcl) is False
    assert detect_table_region("Plain narrative prose about system parameters.") is False
    assert detect_table_region("") is False


def test_oversize_table_splits_at_row_boundaries():
    """Table-atomic: a 120-row column block splits into several blocks and
    every row survives whole — never a mid-row char-slice."""
    from mainframe_rag.ingest.chunk import SECTION_MAX_CHARS, _split_blocks

    para = _table_para(120)
    assert len(para) > SECTION_MAX_CHARS
    blocks = _split_blocks([(0, para)])
    assert len(blocks) > 1
    for _, _, text in blocks:
        assert len(text) <= SECTION_MAX_CHARS + 100
    joined = "\n".join(text for _, _, text in blocks)
    for line in para.splitlines():
        assert line in joined, f"row split across blocks: {line!r}"


def test_table_overlap_backs_off_to_whole_rows():
    """Mirror of the JCL backoff test: 2000-char prose + six 300-char table
    rows. The 400-char tail starts inside R3, so the next block opens with
    all of R4 — never a row fragment."""
    from mainframe_rag.ingest.chunk import _split_blocks

    prose = "y" * 2000
    rows = [(f"PARM{i:02d}      ") + "X" * 288 for i in range(1, 7)]
    assert all(len(r) == 300 for r in rows)
    table_para = "Parameter   Meaning\n" + "\n".join(rows)
    blocks = _split_blocks([(0, prose), (0, table_para)])
    assert len(blocks) == 2
    assert blocks[0][2].endswith(rows[3])
    assert blocks[1][2].startswith(rows[3] + "\n" + rows[4])


def test_mixed_prose_jcl_extracts_cards():
    """Mixed paragraph: prose stays a blob, JCL cards become atomic
    statements; a single `//see`-style line stays prose (byte-identical)."""
    from mainframe_rag.ingest.chunk import _mixed_jcl_items, _split_blocks

    para = (
        "To allocate the dataset use this job.\n"
        "Submit it after IPL completes.\n"
        "//STEP1 EXEC PGM=IEFBR14\n"
        "//DD1 DD DSN=X,DISP=SHR\n"
        "The job ends with condition code zero."
    )
    items = _mixed_jcl_items(para)
    assert items[0] == (
        "To allocate the dataset use this job.\nSubmit it after IPL completes.",
        False,
    )
    assert ("//STEP1 EXEC PGM=IEFBR14", True) in items
    assert ("//DD1 DD DSN=X,DISP=SHR", True) in items
    assert items[-1] == ("The job ends with condition code zero.", False)
    # Single mention passes through untouched.
    lone = "See //see the manual for details.\nMore prose here."
    assert _mixed_jcl_items(lone) == [(lone, False)]
    # End to end: statements stay whole; prose/code boundaries keep the
    # historical double newline, code runs share one.
    blocks = _split_blocks([(0, para)])
    assert blocks == [
        (
            0,
            0,
            (
                "To allocate the dataset use this job.\n"
                "Submit it after IPL completes.\n\n"
                "//STEP1 EXEC PGM=IEFBR14\n"
                "//DD1 DD DSN=X,DISP=SHR\n\n"
                "The job ends with condition code zero."
            ),
        )
    ]


def test_sysin_adjacency_splits_between_records():
    """SYSIN data in the paragraph AFTER the DD * card paragraph splits
    between records, never mid-record — even across the para boundary."""
    from mainframe_rag.ingest.chunk import SECTION_MAX_CHARS, _split_blocks

    card = "//SYSIN DD *"
    data = [f"RECORD-{i:04d} PAYLOAD-DATA-LINE" for i in range(200)]
    assert len("\n".join(data)) > SECTION_MAX_CHARS
    blocks = _split_blocks([(0, card), (1, "\n".join(data))])
    assert len(blocks) > 1
    joined = "\n".join(text for _, _, text in blocks)
    for line in data:
        assert line in joined, f"record split: {line!r}"
    # The chain ends at prose: an explanation stays one prose blob item.
    blocks2 = _split_blocks([(0, card), (1, "REC1 DATA\nREC2 DATA"), (2, "See the manual.")])
    assert blocks2[-1][2].endswith("See the manual.")


def test_rexx_keyword_fallback_matrix():
    """Balanced samples with no header still detect via keyword + code
    signal; manual prose with lone lead words stays prose."""
    from mainframe_rag.ingest.chunk import detect_code_region

    assert detect_code_region("X = 1\nSAY X\nDO I = 1 TO 5\nSAY I\nEND") == "rexx"
    assert detect_code_region("count = count + 1;\nSAY count;\nSAY done;") == "rexx"
    # Negatives: single lead words and prose without code signals.
    assert detect_code_region("Do not restart the system now.\nContact support.") is None
    assert detect_code_region("If the job fails, check the log.\nThen resubmit.") is None
    assert detect_code_region("Use /*comment*/ style sparingly in prose.") is None


def test_console_indent_pins():
    """Console rule pins: 2/3 indented lines are console; 1/3 is prose."""
    from mainframe_rag.ingest.chunk import detect_code_region

    assert detect_code_region("  READY\n  SHOW DSN\nHeader line") == "console"
    assert detect_code_region("Header line\n  one indented\nAnother header") is None


def test_block_page_spans_and_chunk_labels():
    """_split_blocks returns (start, end, text): a cross-page accumulation
    spans both pages in one block."""
    from mainframe_rag.ingest.chunk import _split_blocks

    assert _split_blocks([(3, "para one"), (5, "para two")]) == [(3, 5, "para one\n\npara two")]
    assert _split_blocks([(2, "solo")]) == [(2, 2, "solo")]


def test_make_chunks_span_label_range():
    """Two-page section, one accumulated block: label cites both printed
    pages, page_start is the first index page."""
    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    parsed = ParsedDoc(
        path=__import__("pathlib").Path("span.pdf"),
        sha256="1" * 64,
        doc_id="SA22-0000-01",
        title="Span",
        product="z/OS",
        version="9.9",
        vendor="IBM",
        toc=[[1, "Only chapter", 1]],
        page_count=2,
    )
    chunks = make_chunks(parsed, ["Alpha paragraph here.", "Beta paragraph here."], ["1-6", "1-7"])
    assert len(chunks) == 1
    assert chunks[0].page_start == 0
    assert chunks[0].page_label == "1-6–1-7"
    # UUID pins the span start: the key carries the source revision (issue
    # #361), so same-form-number revisions never share point ids.
    from mainframe_rag.ingest.chunk import make_chunk_id
    from mainframe_rag.ingest.identity import source_rev_key

    rev = source_rev_key("IBM", "z/OS", "9.9", "1" * 64)
    assert chunks[0].chunk_id == make_chunk_id(rev, "Only chapter", 0, 0)


def test_chunk_id_pins_uuid5_namespace_and_key():
    """The point-id contract is UUID5(NAMESPACE_URL, "rev|heading|page|ordinal").
    A literal expected value catches a namespace or key-format change that the
    self-referential comparison above cannot (AGENTS.md lethal-mistake rule).
    The first segment is the source revision (issue #361), not the printed
    doc_id — the old doc-keyed id must never be minted again."""
    import uuid

    from mainframe_rag.ingest.chunk import make_chunk_id

    rev = "ibm|z/os|9.9|" + "1" * 64
    key = rev + "|Only chapter|0|0"
    assert make_chunk_id(rev, "Only chapter", 0, 0) == str(
        uuid.uuid5(uuid.NAMESPACE_URL, key)
    )
    assert make_chunk_id(rev, "Only chapter", 0, 0) == (
        "a1b5c7d0-d365-53d3-a4c2-34fbb3df953e"
    )
    assert make_chunk_id(rev, "Only chapter", 0, 0) != (
        "88bf0502-bc81-5dc1-8165-99b55e6a7835"
    ), "doc-keyed ids are retired by the 361B migration"


def test_fallback_sections_split_no_toc_book():
    """No TOC: heading leads open sections instead of one whole-doc blob;
    deterministic across runs; long headingless runs window every 10 pages."""
    from mainframe_rag.ingest.chunk import FALLBACK_MAX_PAGES, fallback_sections

    pages = ["Cover page words."] + [f"1.{i} Subsection topic words here." for i in range(1, 4)]
    first = fallback_sections(pages, "Guide")
    assert [s.heading_path for s in first] == [
        "Guide",
        "Guide > 1.1 Subsection topic words here.",
        "Guide > 1.2 Subsection topic words here.",
        "Guide > 1.3 Subsection topic words here.",
    ]
    assert fallback_sections(pages, "Guide") == first
    plain = [f"Body prose page {i} with enough words." for i in range(25)]
    windowed = fallback_sections(plain, "Guide")
    assert len(windowed) == 3
    assert [(s.page_start, s.page_end) for s in windowed] == [
        (0, FALLBACK_MAX_PAGES),
        (FALLBACK_MAX_PAGES, 2 * FALLBACK_MAX_PAGES),
        (2 * FALLBACK_MAX_PAGES, 25),
    ]
    assert fallback_sections(["Only one page."], "Guide")[0].heading_path == "Guide"


def test_nested_outline_chunks_deepest_once_no_duplicates():
    """Issue #577: nested outline entries must not re-chunk descendant pages.

    L1 > L2 > L3 across pages: each page's text appears under exactly one
    section (the deepest covering entry) with the full heading path, and no
    two chunks share identical text outside the 400-char overlap.
    """
    from mainframe_rag.ingest.chunk import make_chunks, outline_sections
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    parsed = ParsedDoc(
        path=__import__("pathlib").Path("nested.pdf"),
        sha256="3" * 64,
        doc_id="SA22-0000-00",
        title="Nested",
        product="z/OS",
        version="9.9",
        vendor="IBM",
        toc=[
            [1, "Chapter 1", 1],
            [2, "Section 1.1", 2],
            [3, "Sub 1.1.1", 3],
            [2, "Section 1.2", 4],
            [1, "Chapter 2", 5],
        ],
        page_count=5,
    )
    sections = outline_sections(parsed)
    assert [(s.heading_path, s.page_start, s.page_end) for s in sections] == [
        ("Chapter 1", 0, 1),
        ("Chapter 1 > Section 1.1", 1, 2),
        ("Chapter 1 > Section 1.1 > Sub 1.1.1", 2, 3),
        ("Chapter 1 > Section 1.2", 3, 4),
        ("Chapter 2", 4, 5),
    ]
    # Every page covered exactly once (union unchanged, no overlap).
    covered = [i for s in sections for i in range(s.page_start, s.page_end)]
    assert sorted(covered) == [0, 1, 2, 3, 4]

    pages = [f"Page {i} unique body text for nesting check." for i in range(5)]
    chunks = make_chunks(parsed, pages, [str(i) for i in range(5)])
    texts = [c.text for c in chunks]
    assert len(texts) == len(set(texts))
    assert any(c.heading_path.endswith("Sub 1.1.1") for c in chunks)


def test_same_page_parent_child_loses_no_text():
    """Issue #577 counterexample: parent and first child starting on the same
    page — the parent's intro moves into the child's section, which is
    acceptable, but no text may be dropped."""
    from mainframe_rag.ingest.chunk import make_chunks, outline_sections
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    parsed = ParsedDoc(
        path=__import__("pathlib").Path("samepage.pdf"),
        sha256="4" * 64,
        doc_id="SA22-0000-00",
        title="SamePage",
        product="z/OS",
        version="9.9",
        vendor="IBM",
        toc=[
            [1, "Chapter 3", 3],
            [2, "Section 1", 3],
            [3, "Functions", 3],
            [1, "Chapter 4", 6],
        ],
        page_count=6,
    )
    sections = outline_sections(parsed)
    # Same-page ancestors are empty and dropped; the deepest entry carries
    # the shared pages. Union of pre-change ranges [2,5) is unchanged.
    assert [(s.heading_path, s.page_start, s.page_end) for s in sections] == [
        ("Chapter 3 > Section 1 > Functions", 2, 5),
        ("Chapter 4", 5, 6),
    ]
    pages = [f"Page {i} shared intro text." for i in range(6)]
    chunks = make_chunks(parsed, pages, [str(i) for i in range(6)])
    joined = "\n".join(c.text for c in chunks)
    for i in (2, 3, 4):
        assert f"Page {i} shared intro text." in joined
    assert len({c.text for c in chunks}) == len(chunks)


def test_skipped_child_heading_does_not_cut_parent():
    """Issue #577 review: skipped headings produce no section and must not
    bound a kept section — otherwise the parent is cut and pages are lost."""
    from mainframe_rag.ingest.chunk import make_chunks, outline_sections
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    def _parsed(toc, page_count):
        return ParsedDoc(
            path=__import__("pathlib").Path("skipped.pdf"),
            sha256="5" * 64,
            doc_id="SA22-0000-00",
            title="Skipped",
            product="z/OS",
            version="9.9",
            vendor="IBM",
            toc=toc,
            page_count=page_count,
        )

    # 1. Titles ending in "index" match SKIP_ALWAYS_RE: the two skipped
    # children must not end Ch 6; pages 82–87 stay covered under Ch 6.
    p1 = _parsed(
        [
            [1, "Ch 6 Alternate indexes", 80],
            [2, "Defining an Alternate Index", 82],
            [2, "Building an Alternate Index", 85],
            [2, "Maintaining data", 88],
            [1, "Ch 7", 95],
        ],
        100,
    )
    s1 = outline_sections(p1)
    assert [(s.heading_path, s.page_start, s.page_end) for s in s1] == [
        ("Ch 6 Alternate indexes", 79, 87),
        ("Ch 6 Alternate indexes > Maintaining data", 87, 94),
        ("Ch 7", 94, 100),
    ]
    # Union matches the pre-#577 base ([79,94) + [94,100)): no pages lost.
    assert sorted({i for s in s1 for i in range(s.page_start, s.page_end)}) == list(range(79, 100))
    pages1 = [f"Marker page {i} unique." for i in range(100)]
    joined1 = "\n".join(c.text for c in make_chunks(p1, pages1, [str(i) for i in range(100)]))
    for i in (81, 82, 83, 84, 85, 86):
        assert f"Marker page {i} unique." in joined1

    # 2. Empty title after cleaning is skipped: Chapter 1 runs to Real child.
    p2 = _parsed(
        [[1, "Chapter 1", 10], [2, "   ", 12], [2, "Real child", 15], [1, "Chapter 2", 20]],
        25,
    )
    s2 = outline_sections(p2)
    assert [(s.heading_path, s.page_start, s.page_end) for s in s2] == [
        ("Chapter 1", 9, 14),
        ("Chapter 1 > Real child", 14, 19),
        ("Chapter 2", 19, 25),
    ]
    assert sorted({i for s in s2 for i in range(s.page_start, s.page_end)}) == list(range(9, 25))

    # 3. Front-matter child (Contents@2 within limit) is skipped: Preface
    # runs to Chapter 1, not to the skipped Contents.
    p3 = _parsed([[1, "Preface", 1], [1, "Contents", 2], [1, "Chapter 1", 6]], 20)
    s3 = outline_sections(p3)
    assert [(s.heading_path, s.page_start, s.page_end) for s in s3] == [
        ("Preface", 0, 5),
        ("Chapter 1", 5, 20),
    ]
    assert sorted({i for s in s3 for i in range(s.page_start, s.page_end)}) == list(range(20))


def test_no_toc_pdf_chunks_without_collapse():
    """End to end: a TOC-less document yields several sections and stable
    ids, not one giant whole-doc chunk."""
    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    parsed = ParsedDoc(
        path=__import__("pathlib").Path("notoc.pdf"),
        sha256="2" * 64,
        doc_id="NODOC-1",
        title="NoToc",
        product=None,
        version=None,
        vendor="unknown",
        toc=[],
        page_count=3,
    )
    pages = [
        "Cover words here.",
        "Chapter 2 Operator messages\n\nIEA500I BEFORE IOS REJECTED",
        "Chapter 3 Tuning notes\n\nLFAREA sizing words here.",
    ]
    chunks = make_chunks(parsed, pages, ["1", "2", "3"])
    assert {c.heading_path for c in chunks} >= {"NoToc", "NoToc > Chapter 2 Operator messages"}
    assert len(chunks) > 1
    again = make_chunks(parsed, pages, ["1", "2", "3"])
    assert [c.chunk_id for c in chunks] == [c.chunk_id for c in again]


@pytest.mark.parametrize(
    ("labels", "expected_label"),
    [
        (["1-6", "1-7"], "1-6–1-7"),  # fully labeled: unchanged printed range
        (["", ""], ""),  # no /PageLabels: PyMuPDF returns ''
        ([None, "1"], ""),  # partial: the surviving label must not understate the span
        (["7", None], ""),
        (["A-", "A-"], ""),  # repeated folio across a 2-page span locates nothing
        (["1-6"], ""),  # label list shorter than the span: missing page is unlabeled
        (None, ""),  # no labels supplied at all
    ],
)
def test_make_chunks_stores_physical_span_and_honest_label(labels, expected_label):
    """Issue #271: every chunk carries its physical span (page_start/page_end,
    0-based inclusive); page_label is the printed range only when printed
    labels locate the whole span. Identity still pins page_start."""
    from mainframe_rag.ingest.chunk import make_chunk_id, make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.identity import source_rev_key

    parsed = ParsedDoc(
        path=__import__("pathlib").Path("span.pdf"),
        sha256="1" * 64,
        doc_id="SA22-0000-01",
        title="Span",
        product="z/OS",
        version="9.9",
        vendor="IBM",
        toc=[[1, "Only chapter", 1]],
        page_count=2,
    )
    chunks = make_chunks(parsed, ["Alpha paragraph here.", "Beta paragraph here."], labels)
    assert len(chunks) == 1
    (chunk,) = chunks
    assert (chunk.page_start, chunk.page_end) == (0, 1)
    assert chunk.page_label == expected_label
    rev = source_rev_key("IBM", "z/OS", "9.9", "1" * 64)
    assert chunk.chunk_id == make_chunk_id(rev, "Only chapter", 0, 0)


def test_make_chunks_single_labeled_page_keeps_label():
    """A single-page chunk keeps even a repeated folio: it locates that page."""
    from mainframe_rag.ingest.chunk import _page_label_range

    assert _page_label_range(["A-"]) == "A-"
    assert _page_label_range([""]) == ""
    assert _page_label_range([]) == ""


# Issue #597: realistic-shape generated PDFs through the real parse path
# (parse_pdf -> _extract_page_texts -> strip_chrome -> make_chunks). Original
# text only, built with PyMuPDF at test time; nothing is committed.


def _pipeline_chunks(path):
    import pymupdf

    from mainframe_rag.ingest.run_ingest import _extract_page_texts

    parsed = parse_pdf(path)
    with pymupdf.open(path) as doc:
        texts, labels = _extract_page_texts(doc)
    return make_chunks(parsed, strip_chrome(texts), labels)


# 1-based outline page -> (level, title). "Contents" is front matter inside the
# limit; "Step Index" / "Section Index" match SKIP_ALWAYS_RE; "" is empty.
_DEEP_TOC = [
    (1, "Contents", 1),
    (1, "Chapter 1 Overview", 2),
    (2, "Section 1.1 Setup", 3),
    (3, "Part 1.1.1 Install", 4),
    (4, "Step 1.1.1.1 Unpack", 5),
    (4, "Step Index", 6),
    (4, "", 7),
    (3, "Part 1.1.2 Verify", 8),
    (2, "Section Index", 9),
    (2, "Section 1.2 Tuning", 10),
    (1, "Chapter 2 Reference", 11),
    (1, "Chapter 3 Limits", 12),
]


def _deep_outline_pdf(path, label_rules=None):
    import pymupdf

    doc = pymupdf.open()
    for i in range(1, 13):
        doc.new_page().insert_text((72, 72), f"Widget marker PG{i:02d} original fixture text.")
    doc.set_toc([[level, title, page] for level, title, page in _DEEP_TOC])
    if label_rules:
        doc.set_page_labels(label_rules)
    doc.save(path)
    doc.close()
    return path


@pytest.mark.parametrize(
    "label_rules",
    [None, [{"startpage": 4, "prefix": "", "style": "D", "firstpagenum": 1}]],
    ids=["no-pagelabels", "labels-start-after-page-0"],
)
def test_deep_outline_pdf_chunks_each_page_once_under_deepest_kept_section(tmp_path, label_rules):
    """Issue #577 shape: 4 levels deep with skipped headings at depths 2, 3
    and 4 (SKIP_ALWAYS match, empty title) plus front matter. Every body page
    is chunked exactly once, skipped entries neither cut a parent nor
    duplicate pages, and unlabeled/partially labeled pages never fail the
    document or invent a label (issue #271 shapes)."""
    chunks = _pipeline_chunks(_deep_outline_pdf(tmp_path / "WX10-0010-00_deep.pdf", label_rules))
    paths = {c.heading_path for c in chunks}
    deepest = "Chapter 1 Overview > Section 1.1 Setup > Part 1.1.1 Install > Step 1.1.1.1 Unpack"
    assert deepest in paths
    owner = {}
    for page in range(2, 13):  # outline pages 2..12 carry body text; page 1 is front matter
        marker = f"PG{page:02d}"
        holders = [c.heading_path for c in chunks if marker in c.text]
        assert len(holders) == 1, f"{marker} chunked {len(holders)} times"
        owner[page] = holders[0]
    assert "PG01" not in "".join(c.text for c in chunks)
    # Skipped entries' pages stay with the kept section that precedes them.
    assert owner[6] == owner[7] == deepest
    assert owner[9] == "Chapter 1 Overview > Section 1.1 Setup > Part 1.1.2 Verify"
    assert owner[10] == "Chapter 1 Overview > Section 1.2 Tuning"
    assert owner[12] == "Chapter 3 Limits"
    by_start = {c.page_start: c for c in chunks}
    if label_rules is None:
        assert {c.page_label for c in chunks} == {""}
    else:
        # Pages 0-3 precede the first label rule: no label, never a guess.
        assert all(c.page_label == "" for c in chunks if c.page_start < 4)
        assert by_start[10].page_label == "7" and by_start[11].page_label == "8"


def _table_pdf(path):
    """Column-major table (#85 shape): each column is drawn whole, so plain
    extraction yields column lists, not rows."""
    import pymupdf

    columns = [
        ("Parameter", ["MAXJOBS", "MAXUSERS", "MAXWAIT", "MAXQ"]),
        ("Default", ["200", "50", "30", "10"]),
        ("Meaning", ["Maximum queued jobs", "Maximum signed on users",
                     "Seconds before timeout", "Queue depth"]),
    ]
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Chapter 1 Limits", fontsize=12)
    x = 72
    for head, cells in columns:
        page.insert_text((x, 100), head, fontsize=10)
        for row, cell in enumerate(cells, start=1):
            page.insert_text((x, 100 + 14 * row), cell, fontsize=10)
        x += 140
    doc.set_toc([[1, "Chapter 1 Limits", 1]])
    doc.save(path)
    doc.close()
    return path


_TABLE_ROWS = [
    ("MAXJOBS", "200", "Maximum queued jobs"),
    ("MAXUSERS", "50", "Maximum signed on users"),
    ("MAXWAIT", "30", "Seconds before timeout"),
    ("MAXQ", "10", "Queue depth"),
]


def test_column_major_table_pdf_keeps_text_cells_and_location(tmp_path):
    chunks = _pipeline_chunks(_table_pdf(tmp_path / "WX10-0011-00_table.pdf"))
    assert [(c.heading_path, c.page_start) for c in chunks] == [("Chapter 1 Limits", 0)]
    text = chunks[0].text
    for head in ("Parameter", "Default", "Meaning"):
        assert head in text
    for name, _default, meaning in _TABLE_ROWS:
        assert name in text and meaning in text


def test_column_major_table_keeps_row_value_associations(tmp_path):
    """#85: every independently specified (name, default, meaning) triple is
    on one line, in row order, and no column list survives."""
    (chunk,) = _pipeline_chunks(_table_pdf(tmp_path / "WX10-0011-00_table.pdf"))
    lines = chunk.text.splitlines()
    for name, default, meaning in _TABLE_ROWS:
        assert f"{name} {default} {meaning}" in lines
    assert "Parameter Default Meaning" in lines
    order = [lines.index(f"{n} {d} {m}") for n, d, m in _TABLE_ROWS]
    assert order == sorted(order)


def _change_bar_pdf(path):
    """IBM revision bars: a margin run of bare `|` glyphs beside prose and
    after it. Prose lines are drawn in the body column, bars at x=50."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Chapter 1 Limits", fontsize=12)
    y = 100
    for i in range(6):
        page.insert_text((50, y), "|", fontsize=10)
        page.insert_text((72, y), f"Revised sentence {i} about the widget limit.", fontsize=10)
        y += 14
    for _ in range(8):
        page.insert_text((50, y), "|", fontsize=10)
        y += 12
    doc.set_toc([[1, "Chapter 1 Limits", 1]])
    doc.save(path)
    doc.close()
    return path


def test_change_bar_pdf_keeps_prose_in_order(tmp_path):
    (chunk,) = _pipeline_chunks(_change_bar_pdf(tmp_path / "WX10-0012-00_bars.pdf"))
    assert chunk.heading_path == "Chapter 1 Limits"
    prose = [ln for ln in chunk.text.splitlines() if ln.startswith("Revised sentence")]
    assert prose == [f"Revised sentence {i} about the widget limit." for i in range(6)]


def test_change_bar_glyph_lines_are_dropped_and_prose_kept(tmp_path):
    """#85: bare '|' margin lines are not content; prose lines are untouched."""
    (chunk,) = _pipeline_chunks(_change_bar_pdf(tmp_path / "WX10-0012-00_bars.pdf"))
    assert not [ln for ln in chunk.text.splitlines() if ln.strip() == "|"]
    assert sum(ln.startswith("Revised sentence") for ln in chunk.text.splitlines()) == 6


def _draw_columns(page, columns, x0=72, y0=100, step=140, leading=14, wrapped=()):
    """Column-major draw: each column whole, one text object per line. A '|'
    inside a cell starts a continuation line; rows listed in `wrapped`
    (0-based) reserve one extra line of height in every column."""
    for c, (head, cells) in enumerate(columns):
        x = x0 + step * c
        page.insert_text((x, y0), head, fontsize=10)
        for row, cell in enumerate(cells):
            top = y0 + leading * (row + 1 + sum(1 for w in wrapped if w < row))
            for k, piece in enumerate(cell.split("|")):
                page.insert_text((x, top + leading * k), piece, fontsize=10)


def _one_page_pdf(path, draw):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Chapter 1 Limits", fontsize=12)
    draw(page)
    doc.set_toc([[1, "Chapter 1 Limits", 1]])
    doc.save(path)
    doc.close()
    return path


def test_column_major_wrapped_rows_and_repeated_headers(tmp_path):
    """Row 2's meaning wraps onto a second line ('|' splits the cell); the
    header row repeats mid-table. Triples are specified independently of the
    drawing code."""
    cols = [
        ("Name", ["ALPHA", "BETA", "Name", "GAMMA", "DELTA"]),
        ("Value", ["11", "22", "Value", "33", "44"]),
        ("Note", ["first note", "second note|continues here", "Note", "third note", "fourth note"]),
    ]
    path = _one_page_pdf(
        tmp_path / "WX10-0013-00_wrap.pdf",
        lambda page: _draw_columns(page, cols, wrapped=(1,)),
    )
    (chunk,) = _pipeline_chunks(path)
    lines = chunk.text.splitlines()
    for row in ("ALPHA 11 first note", "BETA 22 second note", "GAMMA 33 third note",
                "DELTA 44 fourth note"):
        assert row in lines, row
    assert lines.count("Name Value Note") == 2  # header + repeat, both whole rows
    assert "continues here" in lines  # wrapped tail kept, directly after its row
    assert lines.index("continues here") == lines.index("BETA 22 second note") + 1


def test_prose_beside_table_is_not_merged_into_rows(tmp_path):
    """A long-line prose paragraph to the right of the table, vertically
    overlapping it, stays prose; the table still becomes rows."""
    cols = [
        ("Key", ["K1", "K2", "K3"]),
        ("Val", ["7", "8", "9"]),
    ]

    def draw(page):
        import pymupdf

        _draw_columns(page, cols, step=60)
        page.insert_textbox(
            pymupdf.Rect(260, 90, 540, 200),
            "This sidebar paragraph explains in ordinary running prose how the "
            "listed keys are chosen and why their values matter to operators.",
            fontsize=10,
        )

    (chunk,) = _pipeline_chunks(_one_page_pdf(tmp_path / "WX10-0014-00_side.pdf", draw))
    lines = chunk.text.splitlines()
    for row in ("K1 7", "K2 8", "K3 9"):
        assert row in lines
    prose = " ".join(ln for ln in lines if ln not in {"Key Val", "K1 7", "K2 8", "K3 9"})
    assert "This sidebar paragraph explains in ordinary running prose how the" in prose


def _plain_vs_extracted(path):
    import pymupdf

    from mainframe_rag.ingest.ibm_pdf import extract_page_text

    with pymupdf.open(path) as doc:
        return [(p.get_text(), extract_page_text(p)) for p in doc]


def test_multicolumn_prose_extraction_is_byte_identical_to_plain(tmp_path):
    """#85 A/B: the table correction must not touch multi-column prose, drawn
    as paragraph boxes or line by line, nor single-column prose."""
    import pymupdf

    doc = pymupdf.open()
    boxed = doc.new_page()
    for rect, text in (
        (pymupdf.Rect(50, 50, 280, 200), "Left column paragraph one. " * 12),
        (pymupdf.Rect(310, 50, 540, 200), "Right column paragraph one. " * 12),
        (pymupdf.Rect(50, 230, 280, 400), "Left column paragraph two. " * 12),
        (pymupdf.Rect(310, 230, 540, 400), "Right column paragraph two. " * 12),
    ):
        boxed.insert_textbox(rect, text, fontsize=10)
    lined = doc.new_page()
    for col, x in (("Left", 50), ("Right", 310)):
        for i in range(10):
            lined.insert_text((x, 100 + 12 * i), f"{col} line {i} of the {col} column text here", fontsize=10)
    single = doc.new_page()
    for i in range(10):
        single.insert_text((72, 100 + 14 * i), f"Single column sentence number {i} runs the full width.", fontsize=10)
    path = tmp_path / "WX10-0015-00_prose.pdf"
    doc.save(path)
    doc.close()
    pairs = _plain_vs_extracted(path)
    assert len(pairs) == 3
    for plain, extracted in pairs:
        assert extracted == plain


def test_row_major_drawn_table_cells_share_a_line(tmp_path):
    """Row-major draw order: plain extraction puts every cell on its own
    line; the rows must come out whole too."""
    rows = [("Name", "Value", "Note"), ("A1", "1", "one"), ("B2", "2", "two"), ("C3", "3", "three")]

    def draw(page):
        for r, cells in enumerate(rows):
            for c, cell in enumerate(cells):
                page.insert_text((72 + 140 * c, 100 + 14 * r), cell, fontsize=10)

    (chunk,) = _pipeline_chunks(_one_page_pdf(tmp_path / "WX10-0016-00_rowmajor.pdf", draw))
    lines = chunk.text.splitlines()
    for r in rows:
        assert " ".join(r) in lines


def test_table_reassembly_only_reorders_words(tmp_path):
    """No word is added or lost by the row rebuild (change bars aside)."""
    import collections

    cols = [
        ("Name", ["ALPHA", "BETA", "Name", "GAMMA"]),
        ("Value", ["11", "22", "Value", "33"]),
        ("Note", ["first note", "second note|continues here", "Note", "third note"]),
    ]
    path = _one_page_pdf(
        tmp_path / "WX10-0017-00_words.pdf",
        lambda page: _draw_columns(page, cols, wrapped=(1,)),
    )
    ((plain, extracted),) = _plain_vs_extracted(path)
    assert extracted != plain
    assert collections.Counter(extracted.split()) == collections.Counter(plain.split())


def test_lone_header_pair_on_one_baseline_is_not_merged(tmp_path):
    """Running header/footer pairs (one baseline) are not tables."""

    def draw(page):
        page.insert_text((72, 780), "Widget Guide", fontsize=9)
        page.insert_text((480, 780), "Page 3", fontsize=9)
        page.insert_text((72, 100), "Plain body sentence one.", fontsize=10)

    ((plain, extracted),) = _plain_vs_extracted(_one_page_pdf(tmp_path / "WX10-0018-00_hdr.pdf", draw))
    assert extracted == plain


@pytest.mark.parametrize("columns", [
    (["If the system fails,", "do not restart it.", "First preserve the dump.", "Then call the owner."],
     ["During normal scheduled work,", "restart is usually permitted.", "Check the approved window.", "Record the completed action."]),
    (["Emergency procedure", "1. Preserve the dump.", "2. Call the owner.", "3. Await approval."],
     ["Routine procedure", "1. Check the window.", "2. Restart the system.", "3. Record the result."]),
])
def test_short_independent_column_procedures_keep_original_sequence(tmp_path, columns):
    """F1: identical baselines and short lines do not establish table cells."""
    import pymupdf

    from mainframe_rag.ingest.ibm_pdf import extract_page_text

    with pymupdf.open() as doc:
        page = doc.new_page(width=612, height=792)
        for x, lines in zip((55, 325), columns, strict=True):
            page.insert_text((x, 90), "\n".join(lines), fontsize=12, lineheight=1.5)
        assert extract_page_text(page) == page.get_text()
        path = tmp_path / "independent-procedures.pdf"
        doc.save(path)
    joined = "\n".join(c.text for c in _pipeline_chunks(path))
    for lines in columns:
        assert "\n".join(lines) in joined


def test_short_prose_beside_captioned_table_keeps_associations(tmp_path):
    """A short sidebar must stay outside an admitted table's row set."""
    cols = [("Key", ["K1", "K2", "K3"]), ("Value", ["7", "8", "9"])]
    sidebar = ["If the system fails,", "do not restart it.", "First preserve the dump.", "Then call the owner."]

    def draw(page):
        page.insert_text((72, 82), "Table 1. Approved limits", fontsize=10)
        _draw_columns(page, cols, step=60)
        page.insert_text((325, 100), "\n".join(sidebar), fontsize=10, lineheight=1.4)

    (chunk,) = _pipeline_chunks(_one_page_pdf(tmp_path / "caption-sidebar.pdf", draw))
    lines = chunk.text.splitlines()
    assert "Table 1. Approved limits" in lines
    assert all(row in lines for row in ("Key Value", "K1 7", "K2 8", "K3 9"))
    assert "\n".join(sidebar) in chunk.text
    assert lines.index("Table 1. Approved limits") < lines.index("Key Value")


def test_centered_multiword_headings_keep_wrapped_row_associations(tmp_path):
    """Real PDF shape: headings and their data have different horizontal starts."""
    columns = [
        (84, 72, "Parameter name", ["MAXJOBS", "RESVBUF", "DYNALLOC"]),
        (234, 250, "Default value", ["200", "64", "32"]),
        (370, 350, "Description", ["Maximum queued jobs.", "Reserved buffers|continues here.", "Allocation slots."]),
    ]

    def draw(page):
        page.insert_text((72, 70), "Table 1. Original example", fontsize=10)
        for heading_x, data_x, heading, values in columns:
            page.insert_text((heading_x, 100), heading, fontsize=10)
            for row, value in enumerate(values):
                y = 114 + 14 * (row + int(row > 1))
                for continuation, line in enumerate(value.split("|")):
                    page.insert_text((data_x, y + 14 * continuation), line, fontsize=10)
        for row, line in enumerate(["Keep this note.", "Read before use.", "Then proceed."]):
            page.insert_text((510, 114 + 14 * row), line, fontsize=10)

    (chunk,) = _pipeline_chunks(_one_page_pdf(tmp_path / "centered-heading-table.pdf", draw))
    lines = chunk.text.splitlines()
    for expected in ["MAXJOBS 200 Maximum queued jobs.", "RESVBUF 64 Reserved buffers", "DYNALLOC 32 Allocation slots."]:
        assert expected in lines
    assert lines.index("continues here.") == lines.index("RESVBUF 64 Reserved buffers") + 1
    assert chunk.text.index("Table 1. Original example") < chunk.text.index("MAXJOBS 200")
    assert "Keep this note.\nRead before use.\nThen proceed." in chunk.text


def test_overlapping_headings_cannot_share_one_inferred_data_column(tmp_path):
    def draw(page):
        for x, heading in [(84, "Parameter"), (110, "Value"), (350, "Description")]:
            page.insert_text((x, 100), heading, fontsize=10)
        for x, values in [(116, ["K1", "K2", "K3"]), (250, ["200", "64", "32"]),
                          (350, ["First limit", "Second limit", "Third limit"])]:
            for row, value in enumerate(values):
                page.insert_text((x, 114 + 14 * row), value, fontsize=10)

    ((plain, extracted),) = _plain_vs_extracted(_one_page_pdf(tmp_path / "ambiguous-headings.pdf", draw))
    assert extracted == plain


def test_standalone_diagram_bar_in_body_is_preserved(tmp_path):
    def draw(page):
        for y, text in ((100, "Input"), (114, "|"), (128, "Output")):
            page.insert_text((220, y), text, fontsize=10)
        page.insert_text((50, 114), "|", fontsize=10)  # one glyph alone is not a change-bar run

    ((plain, extracted),) = _plain_vs_extracted(_one_page_pdf(tmp_path / "diagram.pdf", draw))
    assert extracted == plain
    (chunk,) = _pipeline_chunks(tmp_path / "diagram.pdf")
    assert "Input\n|\nOutput" in chunk.text and chunk.text.splitlines().count("|") == 2


@pytest.mark.parametrize(("font", "labels"), [
    ("cour", ["branch alpha", "branch beta", "branch gamma"]),
    ("helv", ["branch alpha", "branch beta", "branch gamma"]),
    ("cour", ["Branch alpha remains active.", "Branch beta remains idle.", "Branch gamma remains closed."]),
])
def test_margin_positioned_diagram_wall_keeps_its_bars(tmp_path, font, labels):
    def draw(page):
        for y, label in zip((90, 106, 122), labels, strict=True):
            page.insert_text((40, y), "|", fontsize=12, fontname=font)
            page.insert_text((80, y), label, fontsize=12, fontname=font)

    ((plain, extracted),) = _plain_vs_extracted(_one_page_pdf(tmp_path / "margin-wall.pdf", draw))
    assert extracted == plain and extracted.splitlines().count("|") == 3


def test_table_associations_round_trip_through_stored_and_queried_payload(tmp_path):
    from contextlib import closing

    from qdrant_client import QdrantClient, models

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.qdrant_io import upsert_chunks

    path = _table_pdf(tmp_path / "original-table.pdf")
    chunks = _pipeline_chunks(path)
    settings = Settings(qdrant_collection="fidelity", dense_dim=2, _env_file=None)
    with closing(QdrantClient(":memory:")) as client:
        client.create_collection("fidelity", vectors_config={"dense": models.VectorParams(size=2, distance=models.Distance.COSINE)},
                                 sparse_vectors_config={"bm25": models.SparseVectorParams()})
        upsert_chunks(client, settings, parse_pdf(path), chunks,
                      [([1.0, 0.0], ([1], [1.0])) for _ in chunks])
        stored = client.retrieve("fidelity", ids=[chunks[0].chunk_id], with_payload=True)[0]
        hit = client.query_points("fidelity", using="dense", query=[1.0, 0.0], with_payload=True).points[0]
        assert stored.id == hit.id == chunks[0].chunk_id
        assert stored.payload == hit.payload
        lines = hit.payload["text"].splitlines()
        assert "Parameter Default Meaning" in lines
        for name, default, meaning in _TABLE_ROWS:
            assert f"{name} {default} {meaning}" in lines
        assert hit.payload["page_start"] == hit.payload["page_end"] == 0


# --- Same-page bookmarks: one section per bookmark --------------------------
#
# Message manuals bookmark every message, several per page. Page-granular
# sections gave all but the last same-page bookmark an empty range, so a
# page of messages was filed and cited under the last message's heading
# (83% of real message chunks defined more than one message).


def _parsed_doc(toc, page_count, name="widget-messages"):
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    return ParsedDoc(
        path=__import__("pathlib").Path(f"{name}.pdf"),
        sha256="7" * 64,
        doc_id=name,
        title="Widget Messages",
        product="Widget",
        version="1.0",
        vendor="Example",
        toc=toc,
        page_count=page_count,
    )


_MSG_PAGE = (
    "WID101E\nWIDGET TABLE FULL\nExplanation: The widget table has no free slot.\n"
    "See message WID103E for the related limit.\n"
    "WID102E\nWIDGET NAME INVALID\nExplanation: The name has a character outside A-Z.\n"
    "WID103E\nWIDGET LIMIT REACHED\nExplanation: The configured widget limit was reached.\n"
)


def test_same_page_bookmarks_each_get_their_own_section():
    toc = [
        [1, "Widget messages", 1],
        [2, "WID100E to WID199E", 2],
        [3, "WID101E", 2],
        [3, "WID102E", 2],
        [3, "WID103E", 2],
        [3, "WID104E", 3],
    ]
    pages = ["Overview of widget messages.", "WID100E to WID199E\n" + _MSG_PAGE, "WID104E\nWIDGET OFFLINE\n"]
    sections = outline_sections(_parsed_doc(toc, 3), pages)
    prefix = "Widget messages > WID100E to WID199E > "
    assert [(s.heading_path, s.page_start, s.page_end) for s in sections] == [
        ("Widget messages", 0, 1),
        # The range header shares the page with its first child: it folds
        # into WID101E as before (#577), it is not split off on its own.
        (prefix + "WID101E", 1, 2),
        (prefix + "WID102E", 1, 2),
        (prefix + "WID103E", 1, 2),
        (prefix + "WID104E", 2, 3),
    ]
    # The shared page is cut exactly once per bookmark: no byte lost or repeated.
    shared = [s for s in sections if s.page_start == 1]
    assert "".join(pages[1][s.start_char : s.end_char] for s in shared) == pages[1]

    chunks = make_chunks(_parsed_doc(toc, 3), pages, ["1", "2", "3"])
    by_leaf = {c.heading_path.rsplit(" > ", 1)[-1]: c for c in chunks}
    for msg in ("WID101E", "WID102E", "WID103E"):
        chunk = by_leaf[msg]
        assert chunk.text.splitlines()[0 if msg != "WID101E" else 1] == msg
        assert chunk.page_label == "2"
    # Each chunk defines exactly its own message; a cross-reference is not a
    # definition and does not move the cut.
    assert "WID102E" not in by_leaf["WID101E"].text
    assert "See message WID103E" in by_leaf["WID101E"].text
    assert by_leaf["WID103E"].text.startswith("WID103E\nWIDGET LIMIT REACHED")
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    assert [c.chunk_id for c in chunks] == [c.chunk_id for c in make_chunks(_parsed_doc(toc, 3), pages, ["1", "2", "3"])]


@pytest.mark.parametrize(
    "page, why",
    [
        ("WID101E\nA.\nWID103E\nC.\n", "a later bookmark's title line is missing"),
        ("WID101E\nA.\nWID103E\nC.\nWID102E\nB.\n", "title lines out of outline order"),
    ],
)
def test_same_page_bookmarks_fall_back_to_the_whole_page(page, why):
    toc = [[1, "WID101E", 1], [1, "WID102E", 1], [1, "WID103E", 1]]
    sections = outline_sections(_parsed_doc(toc, 1), [page])
    assert [(s.heading_path, s.start_char, s.end_char) for s in sections] == [("WID103E", 0, None)], why


def test_same_page_split_needs_page_text_and_distinct_headings():
    toc = [[1, "WID101E", 1], [1, "WID102E", 1]]
    page = "WID101E\nA.\nWID102E\nB.\n"
    # Callers without page text keep the page-granular behavior.
    assert [s.heading_path for s in outline_sections(_parsed_doc(toc, 1))] == ["WID102E"]
    # Two identical headings on one page would mint one chunk key twice.
    dup = [[1, "Notes", 1], [1, "Notes", 1]]
    assert [s.heading_path for s in outline_sections(_parsed_doc(dup, 1), ["Notes\nA.\nNotes\nB.\n"])] == ["Notes"]
    # A piece holding only its own title line folds into the next bookmark.
    bare = outline_sections(_parsed_doc(toc, 1), ["WID101E\nWID102E\nB.\n"])
    assert [(s.heading_path, s.start_char) for s in bare] == [("WID102E", 0)]
    split = outline_sections(_parsed_doc(toc, 1), [page])
    assert [(s.heading_path, page[s.start_char : s.end_char]) for s in split] == [
        ("WID101E", "WID101E\nA.\n"),
        ("WID102E", "WID102E\nB.\n"),
    ]


def test_same_page_message_bookmarks_end_to_end(tmp_path):
    """Producer path on a generated PDF: real outline, real text extraction
    and chrome stripping. Each message bookmark becomes its own cited chunk."""
    import pymupdf

    from mainframe_rag.ingest.ibm_pdf import _extract_page_texts

    path = tmp_path / "widget-messages.pdf"
    doc = pymupdf.open()
    bodies = [
        ["Widget messages", "This part lists the widget messages."],
        ["WID101E", "WIDGET TABLE FULL", "Explanation: The widget table has no free slot.",
         "WID102E", "WIDGET NAME INVALID", "Explanation: The name has a character outside A-Z.",
         "WID103E", "WIDGET LIMIT REACHED", "Explanation: The configured widget limit was reached."],
        ["WID104E", "WIDGET OFFLINE", "Explanation: The widget server stopped responding."],
    ]
    for lines in bodies:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 90 + 18 * i), line, fontsize=11)
    doc.set_toc([[1, "Widget messages", 1], [2, "WID101E", 2], [2, "WID102E", 2], [2, "WID103E", 2], [2, "WID104E", 3]])
    doc.save(path)
    doc.close()

    parsed = parse_pdf(path)
    doc = pymupdf.open(path)
    try:
        texts, labels = _extract_page_texts(doc)
    finally:
        doc.close()
    chunks = make_chunks(parsed, strip_chrome(texts), labels)
    cited = {c.heading_path: c for c in chunks}
    for msg in ("WID101E", "WID102E", "WID103E", "WID104E"):
        chunk = cited[f"Widget messages > {msg}"]
        assert chunk.chunk_type == "message"
        assert chunk.text.startswith(msg)
        assert [m for m in chunk.message_ids if m.startswith("WID")] == [msg]
    assert sum(c.text.count("Explanation:") for c in chunks) == 4
