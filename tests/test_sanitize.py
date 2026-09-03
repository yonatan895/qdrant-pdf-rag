"""Ingest text sanitization tests (issue #87).

Extracted PDF text must not carry control sequences, terminal escapes,
bidi overrides, or zero-width characters into chunks, embeddings, or
prompts — while printable content (especially JCL/REXX alignment
whitespace) stays byte-identical.
"""

import pymupdf

from mainframe_rag.ingest import run_ingest
from mainframe_rag.ingest.ibm_pdf import parse_pdf, sanitize_page_text


def test_drops_c0_controls_and_del() -> None:
    assert sanitize_page_text("IEA500I\x00 BEFORE\x07 IOS\x7f END") == "IEA500I BEFORE IOS END"


def test_strips_full_csi_sequences_not_just_esc() -> None:
    # Dropping lone ESC would leave "[31m" behind; the whole sequence goes.
    assert sanitize_page_text("\x1b[31mREJECTED\x1b[0m, REASON=12") == "REJECTED, REASON=12"


def test_drops_bidi_overrides() -> None:
    assert sanitize_page_text("LFAREA\u202e\u2066 512M\u2069\u202c ok") == "LFAREA 512M ok"


def test_drops_zero_width_and_bom() -> None:
    assert sanitize_page_text("\ufeffIEA\u200b500\u200cI\u200d!") == "IEA500I!"


def test_preserves_whitespace_newlines_and_unicode() -> None:
    text = "    //OUTDATA DD DSN=X,\n\t//            UNIT=SYSDA\nGröße: 512M 日本語 🙂"
    assert sanitize_page_text(text) == text


def test_empty_and_plain_passthrough() -> None:
    assert sanitize_page_text("") == ""
    assert sanitize_page_text("plain narrative prose.") == "plain narrative prose."


class _StubPage:
    def __init__(self, text: str, label: str | None = None) -> None:
        self._text = text
        self._label = label

    def get_text(self) -> str:
        return self._text

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
    ])
    texts, labels = run_ingest._extract_page_texts(doc)  # type: ignore[arg-type]
    assert texts == ["clean page one", "IEA500I BEFORE IOS", ""]
    assert labels == ["1", "2", None]


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
