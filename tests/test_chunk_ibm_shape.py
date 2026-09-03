"""Chunk contract tests: outline sections, chunk_id stability, classification."""

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
    # Every chunk starts at a statement boundary: a JCL line opening a
    # chunk is always a new statement, never a continuation.
    for chunk in chunks:
        first_jcl = next((ln for ln in chunk.text.splitlines() if ln.startswith("//")), None)
        if first_jcl is not None:
            assert not first_jcl.startswith("// ") and not first_jcl.startswith("//\t"), (
                f"chunk opens mid-statement: {first_jcl[:40]!r}"
            )
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
    The flush tail (400 chars) would cut inside S3, so the seed must back
    off to whole items and the next block must open with all of S4."""
    from mainframe_rag.ingest.chunk import _split_blocks

    prose = "y" * 2000
    stmts = [f"//S{i:02d}  " + "X" * (300 - 7) for i in range(1, 7)]
    assert all(len(s) == 300 for s in stmts)
    code_para = "\n".join(stmts)
    blocks = _split_blocks([(0, prose), (0, code_para)])
    assert len(blocks) == 2
    assert blocks[0][1].endswith(stmts[3])
    assert blocks[1][1].startswith(stmts[3] + "\n\n" + stmts[4])
    for statement in stmts:
        assert statement in blocks[0][1] or statement in blocks[1][1]
