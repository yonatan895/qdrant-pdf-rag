"""Generic PDFs (no IBM form number, no outline) must still ingest."""

from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import make_chunks
from mainframe_rag.ingest.ibm_pdf import parse_pdf
from mainframe_rag.ingest.walk import detect_vendor, infer_from_path


def test_plain_pdf_uses_filename_stem_as_doc_id(plain_pdf):
    parsed = parse_pdf(plain_pdf)
    assert parsed.doc_id == "widget-guide"
    assert parsed.vendor == "unknown"
    assert parsed.title


def test_plain_pdf_chunks_without_outline(plain_pdf):
    import pymupdf

    parsed = parse_pdf(plain_pdf)
    assert parsed.toc == ()
    doc = pymupdf.open(plain_pdf)
    try:
        texts = [p.get_text() for p in doc]
        labels = [p.get_label() for p in doc]
    finally:
        doc.close()
    chunks = make_chunks(parsed, strip_chrome(texts), labels)
    assert chunks
    bodies = " ".join(c.text for c in chunks).lower()
    assert "widget" in bodies or "torque" in bodies
    assert all(len(c.chunk_id) == 36 for c in chunks)


def test_path_layout_vendor_product_version(tmp_path, plain_pdf):
    import shutil

    dest = tmp_path / "acme" / "widget" / "1.0" / "guide.pdf"
    dest.parent.mkdir(parents=True)
    shutil.copy(plain_pdf, dest)
    v, p, ver = infer_from_path(dest, tmp_path)
    assert (v, p, ver) == ("acme", "widget", "1.0")
    parsed = parse_pdf(dest, corpus_root=tmp_path)
    assert parsed.vendor == "acme"
    assert parsed.product == "widget"
    assert parsed.version == "1.0"


def test_detect_vendor_default_unknown(tmp_path):
    p = tmp_path / "notes.pdf"
    p.write_bytes(b"%PDF-1.4")
    assert detect_vendor(p) == "unknown"
    assert detect_vendor(tmp_path / "Broadcom" / "x.pdf") == "Broadcom"
