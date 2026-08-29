"""Parser tests on the synthetic IBM-shaped fixture (generated at runtime)."""

from mainframe_rag.ingest.ibm_pdf import parse_pdf
from mainframe_rag.ingest.walk import detect_vendor, walk_pdfs


def test_extracts_doc_id(synthetic_pdf):
    parsed = parse_pdf(synthetic_pdf)
    assert parsed.doc_id == "SA22-0000-00"


def test_extracts_product_version(synthetic_pdf):
    parsed = parse_pdf(synthetic_pdf)
    assert parsed.product == "z/OS"
    assert parsed.version == "9.9"


def test_extracts_title_and_toc(synthetic_pdf):
    parsed = parse_pdf(synthetic_pdf)
    assert parsed.title == "Synthetic Operating System Reference"
    assert any(t[1] == "Chapter 1 System parameters" for t in parsed.toc)
    assert parsed.page_count == 8


def test_page_label_is_printed_label(synthetic_pdf):
    import pymupdf

    doc = pymupdf.open(synthetic_pdf)
    try:
        assert doc[0].get_label() == "1-1"
        assert doc[5].get_label() == "1-6"
    finally:
        doc.close()


def test_sha256_is_stable(synthetic_pdf):
    a = parse_pdf(synthetic_pdf)
    b = parse_pdf(synthetic_pdf)
    assert a.sha256 == b.sha256 and len(a.sha256) == 64


def test_doc_id_tie_break_is_lexicographic():
    """Equal-count doc numbers must break ties deterministically: the old
    max(set(...)) depended on PYTHONHASHSEED (spawn workers flip it), so the
    doc_id changed between runs and churned the resume path (found on a real
    z/OS corpus whose front matter carries several form numbers)."""
    from mainframe_rag.ingest.ibm_pdf import _doc_id_from_text

    text = "Cover: GC20-0001-00  Manual: SX26-3723-06\nGC20-0001-00 again\nSX26-3723-06 again"
    assert _doc_id_from_text(text) == "GC20-0001-00"  # lexicographically first of the tied pair
    # A strictly-more-frequent doc number still wins outright.
    text2 = "ZZ99-9999-00\nAA11-1111-00\nAA11-1111-00"
    assert _doc_id_from_text(text2) == "AA11-1111-00"


def test_parse_pdf_sha256_override_and_fallback(synthetic_pdf):
    """The caller-supplied digest must land in ParsedDoc.sha256 verbatim —
    resume (should_skip / doc_sha256) keys on this field, so a dropped or
    altered override would silently corrupt the skip logic."""
    from mainframe_rag.ingest.ibm_pdf import sha256_file

    assert parse_pdf(synthetic_pdf, sha256="ab" * 32).sha256 == "ab" * 32
    assert parse_pdf(synthetic_pdf).sha256 == sha256_file(synthetic_pdf)


def test_walker_ignores_pdx_and_idx(tmp_path, synthetic_pdf):
    import shutil

    dest = tmp_path / synthetic_pdf.name
    shutil.copy(synthetic_pdf, dest)
    (tmp_path / "SA22-0000-00.pdx").write_bytes(b"adobe catalog")
    (tmp_path / "SA22-0000-00.idx").write_bytes(b"adobe index")
    (tmp_path / "notes.txt").write_text("not a pdf")
    (tmp_path / ".hidden").mkdir()
    shutil.copy(synthetic_pdf, tmp_path / ".hidden" / "hidden.pdf")

    found = walk_pdfs(tmp_path)
    names = [p.name for p in found]
    assert names == [dest.name]
    assert detect_vendor(dest) == "unknown"
    assert detect_vendor(tmp_path / "Broadcom" / "x.pdf") == "Broadcom"
