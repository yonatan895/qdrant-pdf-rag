"""Chunk contract tests: outline sections, chunk_id stability, classification."""

import re

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
    for _, text in blocks:
        assert len(text) <= SECTION_MAX_CHARS + 100
    assert blocks[0][1].startswith("//SYSIN DD *")
    joined = "\n".join(text for _, text in blocks)
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
    assert _split_blocks([(page, text)]) == [(0, "//A X\n//B Y")]
    prose = _split_blocks([(0, "para one"), (0, "para two")])
    assert prose == [(0, "para one\n\npara two")]


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
    joined = "\n".join(text for _, text in blocks)
    for statement in statements:
        assert statement in joined
    # Every piece opens with a statement start (overlap seeds may carry a
    # leading separator; strip it before checking — a sliced card would
    # still fail this assertion).
    for _, text in blocks:
        opening = text.lstrip()
        assert opening.startswith(("//S", "//*"))


def test_oversize_single_statement_emitted_whole():
    from mainframe_rag.ingest.chunk import SECTION_MAX_CHARS, _split_blocks

    giant = "//LONG EXEC PGM=X,PARM='" + "Y" * (SECTION_MAX_CHARS + 500) + "'"
    blocks = _split_blocks([(0, giant)])
    assert len(blocks) == 1
    assert blocks[0][1] == giant


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
    assert blocks[0][1].endswith(stmts[3])
    assert blocks[1][1].startswith(stmts[3] + "\n" + stmts[4])
    for statement in stmts:
        assert statement in blocks[0][1] or statement in blocks[1][1]
