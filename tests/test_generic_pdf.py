"""Generic PDFs (no IBM form number, no outline) must still ingest."""

from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import make_chunks
from mainframe_rag.ingest.ibm_pdf import parse_pdf
from mainframe_rag.ingest.walk import detect_vendor, infer_from_path


def test_plain_pdf_uses_filename_stem_and_chunks_without_outline(plain_pdf):
    import pymupdf

    parsed = parse_pdf(plain_pdf)
    assert parsed.doc_id == "widget-guide"
    assert parsed.vendor == "unknown"
    assert parsed.title
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
    # Issue #839: the layout supplies product and version; its vendor
    # segment does not (no marker below the root, so unknown).
    assert parsed.vendor == "unknown"
    assert parsed.product == "widget"
    assert parsed.version == "1.0"



def test_path_layout_applies_only_at_exactly_four_levels(tmp_path, plain_pdf):
    """Issue #849: an IBM kit tree (kit/form/doc/pdf/x.pdf) is not the
    vendor/product/version layout. Before the fix any depth >= 4 was read as
    one, storing the kit's folder names as product and version; now only
    root/vendor/product/version/file.pdf is, and other depths fall back to
    text detection (none in this synthetic PDF)."""
    import shutil

    kit = ("zos_v22_pdfs", "zosv2r2kitpdfs", "c27843006", "SC27-8430-06", "pdf")
    for depth in (6, 5, 3, 2, 1):
        root = tmp_path / f"depth{depth}"
        dest = root.joinpath(*kit[: depth - 1], "guide.pdf")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(plain_pdf, dest)
        assert infer_from_path(dest, root) == ("unknown", "unknown", ""), depth
        parsed = parse_pdf(dest, corpus_root=root)
        assert (parsed.product, parsed.version) == (None, None), depth

def test_detect_vendor_reads_only_the_path_below_the_corpus_root(tmp_path):
    """Issue #839: markers in the mount never decide the vendor; markers
    below the root keep their first-match order, and "/ca/" still matches
    a top-level directory."""
    for mount in ("precisely", "bmc-share", "ca", "plain"):
        root = tmp_path / mount
        assert detect_vendor(root / "manual.pdf", root) == "unknown"
        assert detect_vendor(root / "IBM" / "zOS" / "2.5" / "x.pdf", root) == "IBM"
        assert detect_vendor(root / "ca" / "x.pdf", root) == "Broadcom"
        assert detect_vendor(root / "ca-7" / "ibm" / "x.pdf", root) == "Broadcom"
    assert detect_vendor(tmp_path / "elsewhere" / "ibm.pdf", tmp_path / "plain") == "unknown"
    assert detect_vendor(tmp_path / "precisely" / "x.pdf", None) == "unknown"
    assert detect_vendor(tmp_path / "plain" / "redhat-guide.pdf", None) == "Red Hat"
