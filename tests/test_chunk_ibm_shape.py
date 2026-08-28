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
