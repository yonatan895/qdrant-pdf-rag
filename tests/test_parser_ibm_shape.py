"""Parser tests on the synthetic IBM-shaped fixture (architecture.md 5.3)."""

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
    assert detect_vendor(dest) == "IBM"
    assert detect_vendor(tmp_path / "Broadcom" / "x.pdf") == "Broadcom"
