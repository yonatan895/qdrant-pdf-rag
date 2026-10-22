"""Ingest text sanitization tests (issue #87).

Extracted PDF text must not carry control sequences, terminal escapes,
bidi overrides, or zero-width characters into chunks, embeddings, or
prompts — while printable content (especially JCL/REXX alignment
whitespace) stays byte-identical.
"""

import pymupdf

from mainframe_rag.ingest import run_ingest
from mainframe_rag.ingest.ibm_pdf import parse_pdf, sanitize_page_text


def test_sanitize_page_text_drops_controls_and_keeps_content() -> None:
    assert sanitize_page_text("IEA500I\x00 BEFORE\x07 IOS\x7f END") == "IEA500I BEFORE IOS END"

    # Dropping lone ESC would leave "[31m" behind; the whole sequence goes.
    assert sanitize_page_text("\x1b[31mREJECTED\x1b[0m, REASON=12") == "REJECTED, REASON=12"

    assert sanitize_page_text("LFAREA\u202e\u2066 512M\u2069\u202c ok") == "LFAREA 512M ok"

    assert sanitize_page_text("\ufeffIEA\u200b500\u200cI\u200d!") == "IEA500I!"

    text = "    //OUTDATA DD DSN=X,\n\t//            UNIT=SYSDA\nGröße: 512M 日本語 🙂"
    assert sanitize_page_text(text) == text

    assert sanitize_page_text("") == ""
    assert sanitize_page_text("plain narrative prose.") == "plain narrative prose."


def test_sanitize_page_text_spells_out_typographic_ligatures() -> None:
    """Issue #870: fonts emit U+FB00-FB06 ligatures inside words
    ("con\ufb01guration", "certi\ufb01cate"; 525 in e0z2n161), which a typed
    query can never match in the BM25 leg. Exactly those seven code points
    become their letters; the characters next to them, full-width and
    superscript forms (what blanket NFKC would rewrite) and private-use
    glyphs stay as extracted."""
    assert sanitize_page_text("con\ufb01guration certi\ufb01cate tra\ufb00ic \ufb02ow") == (
        "configuration certificate traffic flow")
    pairs = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
             "\ufb05": "st", "\ufb06": "st"}
    for lig, letters in pairs.items():
        assert sanitize_page_text(f"a{lig}b") == f"a{letters}b"
    kept = "\uff21\uff22 x\u00b2 \uf6d9 \ufb07 \ufaff \ufb13"
    assert sanitize_page_text(kept) == kept


class _StubPage:
    def __init__(self, text: str, label: str | None = None) -> None:
        self._text = text
        self._label = label

    def get_text(self, option: str = "text") -> str | list:
        # Plain text only; the geometry options see an empty page, so the
        # table-row reassembly (#85) falls through to plain extraction.
        return self._text if option == "text" else []

    def get_label(self) -> str | None:
        return self._label


class _StubDoc:
    def __init__(self, pages: list[_StubPage]) -> None:
        self._pages = pages

    @property
    def page_count(self) -> int:
        return len(self._pages)

    def __getitem__(self, i: int) -> _StubPage:
        return self._pages[i]


def test_extract_page_texts_sanitizes_and_preserves_labels() -> None:
    doc = _StubDoc([
        _StubPage("clean page one", "1"),
        _StubPage("IEA500I\x00 BEFORE\x1b[1m IOS\u202e\u200b", "2"),
        _StubPage("", None),
        _StubPage("Con\ufb01gure the \ufb01le", "3"),
    ])
    texts, labels = run_ingest._extract_page_texts(doc)  # type: ignore[arg-type]
    assert texts == ["clean page one", "IEA500I BEFORE IOS", "", "Configure the file"]
    assert labels == ["1", "2", None, "3"]


def test_parse_pdf_sanitizes_metadata_title(monkeypatch, tmp_path) -> None:
    """The title flows into payloads, embed headers, and prompts, so the
    parse wiring — not just the helper — must sanitize it."""

    class FakeDoc:
        page_count = 1

        @property
        def metadata(self) -> dict[str, str]:
            return {"title": "Manual\x00 Title\u202e"}

        def __getitem__(self, i: int) -> _StubPage:
            return _StubPage("body text without a form number")

        def get_toc(self, simple: bool = True) -> list:
            return []

        def close(self) -> None:
            pass

    monkeypatch.setattr(pymupdf, "open", lambda path: FakeDoc())
    pdf = tmp_path / "SA22-0001-00.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    parsed = parse_pdf(pdf)
    assert parsed.title == "Manual Title"
    assert parsed.doc_id == "SA22-0001-00"
