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
