"""Parser tests on the synthetic IBM-shaped fixture (generated at runtime)."""

import pytest

from mainframe_rag.ingest.ibm_pdf import parse_pdf
from mainframe_rag.ingest.walk import walk_pdfs


def test_extracts_ibm_metadata_title_and_toc(synthetic_pdf):
    parsed = parse_pdf(synthetic_pdf)
    assert parsed.doc_id == "SA22-0000-00"
    assert parsed.product == "z/OS"
    assert parsed.version == "9.9"
    assert parsed.title == "Synthetic Operating System Reference"
    assert any(t[1] == "Chapter 1 System parameters" for t in parsed.toc)
    assert parsed.page_count == 8


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
    assert parse_pdf(synthetic_pdf).sha256 == parse_pdf(synthetic_pdf).sha256
    assert len(sha256_file(synthetic_pdf)) == 64


def test_walker_finds_pdfs_under_dot_parent_dir(tmp_path, synthetic_pdf):
    """Issue #594: only parts below root are filtered — a corpus root under
    a dot-directory (or ignored dir) must still be walked, while dot-dirs
    and ignored dirs *inside* the corpus are still skipped."""
    import shutil

    for parent_name in (".parent", "__MACOSX", "lost+found"):
        root = tmp_path / parent_name / "corpus"
        root.mkdir(parents=True)
        dest = root / synthetic_pdf.name
        shutil.copy(synthetic_pdf, dest)
        # Inside-corpus filters still apply.
        (root / ".hidden").mkdir(exist_ok=True)
        shutil.copy(synthetic_pdf, root / ".hidden" / "hidden.pdf")
        (root / "__MACOSX").mkdir(exist_ok=True)
        shutil.copy(synthetic_pdf, root / "__MACOSX" / "mac.pdf")

        found = walk_pdfs(root)
        assert [p.name for p in found] == [dest.name]


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


def test_extract_page_texts_label_tree_starting_after_page_zero(tmp_path):
    """Issue #271: with a /PageLabels tree whose first rule starts at page 3,
    PyMuPDF get_label() raises IndexError on pages 0-2. Real PyMuPDF, not a
    stub: those labels are absent (None), later pages keep theirs, and the
    document is not lost."""
    import pymupdf

    from mainframe_rag.ingest.run_ingest import _extract_page_texts

    path = tmp_path / "WX10-0002-00.pdf"
    doc = pymupdf.open()
    for i in range(5):
        doc.new_page().insert_text((72, 72), f"Widget page {i + 1}")
    doc.set_page_labels([{"startpage": 3, "prefix": "", "style": "D", "firstpagenum": 1}])
    doc.save(path)
    doc.close()

    with pymupdf.open(path) as reopened:
        with pytest.raises(IndexError):
            reopened[0].get_label()  # the library behavior this guards against
        texts, labels = _extract_page_texts(reopened)
    assert labels == [None, None, None, "1", "2"]
    assert [t.strip() for t in texts] == [f"Widget page {i + 1}" for i in range(5)]


def test_extract_page_texts_decodes_hex_string_label_prefixes(tmp_path):
    """Issue #863: a /PageLabels prefix written as a PDF hex string comes back
    from PyMuPDF's get_label() verbatim ("<FEFF0033002D>1"). Extraction stores
    the printed label: UTF-16BE after a byte-order mark, PDFDocEncoding
    otherwise, an empty <FEFF> prefix as nothing. A literal prefix is
    untouched, and an undecodable one is absent, never guessed."""
    import pymupdf

    from mainframe_rag.ingest.run_ingest import _extract_page_texts

    path = tmp_path / "WX10-0003-00.pdf"
    doc = pymupdf.open()
    for i in range(7):
        doc.new_page().insert_text((72, 72), f"Widget page {i + 1}")
    doc.xref_set_key(
        doc.pdf_catalog(),
        "PageLabels",
        "<</Nums [0 <</S /r /P <FEFF>>> 2 <</S /D /P <FEFF0033002D>>> "
        "4 <</S /D /P <412D>>> 5 <</S /D /P (<B>)>> 6 <</S /D /P <FEFF00>>>]>>",
    )
    doc.save(path)
    doc.close()

    with pymupdf.open(path) as reopened:
        assert reopened[2].get_label() == "<FEFF0033002D>1"  # the library behavior
        _, labels = _extract_page_texts(reopened)
    assert labels == ["i", "ii", "3-1", "3-2", "A-1", "<B>1", None]


def test_extract_page_texts_lives_in_a_hashed_rule_module():
    """Payload-changing extraction (#85 table rows) must sit in a module that
    extraction_rules_version hashes; run_ingest only re-exports it."""
    from pathlib import Path

    from mainframe_rag.ingest import ibm_pdf, run_ingest
    from mainframe_rag.ingest.rules_version import rule_module_paths

    assert run_ingest._extract_page_texts is ibm_pdf._extract_page_texts
    assert Path(ibm_pdf.__file__).resolve() in {p.resolve() for p in rule_module_paths()}
