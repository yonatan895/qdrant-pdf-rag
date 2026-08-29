"""Classification and chrome-stripping tests (synthetic strings only)."""

from mainframe_rag.ingest.chrome import chrome_lines, strip_page
from mainframe_rag.ingest.classify import classify


def test_message():
    text = "IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy\n\nExplanation: fixture text."
    assert classify(text) == "message"


def test_syntax():
    assert classify(">>-IOSCMDS--+-APPLY-+--parm---><\n            +-LIST--+") == "syntax"
    assert classify("expr ::= term | factor") == "syntax"
    assert classify("┌─────────┐\n│ operand │\n└─────────┘") == "syntax"


def test_table_and_narrative():
    table = "Parameter   Meaning\nLFAREA      Size\nPROGxx      Auth"
    assert classify(table) == "table"
    assert classify("The lookaside facility stores invented entries for tests.") == "narrative"


def test_chrome_strip():
    pages = [
        "IEA500I Manual\n" + f"body line {i}\n(c) Synthetic Corp 2026"
        for i in range(10)
    ]
    chrome = chrome_lines(pages)
    assert "iea500i manual" in chrome
    assert "(c) synthetic corp 2026" in chrome
    stripped = strip_page(pages[0], chrome)
    assert "IEA500I Manual" not in stripped
    assert "body line 0" in stripped


def test_strip_page_keeps_roman_letter_words():
    """Regression: the old [ivxlcdm]+ IGNORECASE charset matched any word made
    of roman-numeral letters, so standalone "XML", "civil", "dim" lines were
    silently deleted as page numbers. Only strict numerals may be dropped."""
    chrome: set[str] = set()
    page = "XML samples\nxml\ncivil\ndim\nmid\nlid\nxiv\nXII\n12\n12-34"
    out = strip_page(page, chrome)
    for kept in ("XML samples", "xml", "civil", "dim", "mid", "lid"):
        assert kept in out, kept
    for dropped in ("xiv", "XII", "12", "12-34"):
        assert dropped not in out, dropped


def test_strip_page_drops_bare_page_numbers_everywhere():
    """Page-number stripping is per-line and does not depend on chrome detection."""
    page = "body\n7\niv.\n1234"
    out = strip_page(page, set())
    assert out == "body\niv."


def test_strip_page_keeps_inner_dot_numbers():
    """A standalone "1.2" is a section number, not a page footer: the old
    r"\\d+[-.]?\\d*" form deleted it. Dash-between stays a page range."""
    out = strip_page("intro\n1.2\n2.10\n12-34", set())
    assert "1.2" in out and "2.10" in out
    assert "12-34" not in out
