"""Classification and chrome-stripping tests (synthetic strings only)."""

import pytest

from mainframe_rag.ingest.chrome import chrome_lines, strip_page
from mainframe_rag.ingest.classify import classify


def test_message_families_beyond_classic():
    """Issue #216: the anchored detector covers the MSG_RE families CICS
    DFH cards, IMS DFS codes (severity optional), and 4-letter prefixes —
    still line-anchored, still the fixed `message` value (no new vocab)."""
    text = "IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy\n\nExplanation: fixture text."
    assert classify(text) == "message"
    assert classify("DFHAC2006 CICS TRANSACTION FAILED") == "message"
    assert classify("DFHSI1579 CICS INITIALIZATION") == "message"
    assert classify("DFS058 IMS DATABASE ERROR") == "message"
    assert classify("DFS058I IMS DATABASE ERROR") == "message"
    assert classify("DSNA670I DB2 OBJECT UNAVAILABLE") == "message"
    assert classify("TSSC001E SECURITY VIOLATION") == "message"


def test_message_buried_after_explanation():
    """Issue #216: 1-2 explanation lines above the anchored id card still
    classify as message; a bare mid-line mention never does."""
    buried = (
        "This message appears during initialization.\n"
        "Check the operator console for details.\n"
        "DFHAC2006 CICS TRANSACTION FAILED"
    )
    assert classify(buried) == "message"
    assert classify("The manual mentions IEA500I in passing among words.") == "narrative"
    # Anchored code past the scan window stays narrative (precision over
    # recall: the window is MESSAGE_SCAN_LINES, not the whole chunk).
    from mainframe_rag.ingest.classify import MESSAGE_SCAN_LINES

    deep = "\n".join(f"Filler prose line {i}." for i in range(MESSAGE_SCAN_LINES + 1))
    deep += "\nIEA500I BURIED TOO DEEP"
    assert classify(deep) == "narrative"


def test_syntax_table_and_narrative():
    assert classify(">>-IOSCMDS--+-APPLY-+--parm---><\n            +-LIST--+") == "syntax"
    assert classify("expr ::= term | factor") == "syntax"
    assert classify("┌─────────┐\n│ operand │\n└─────────┘") == "syntax"
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


@pytest.mark.parametrize("page_count", [128, 384, 200])
def test_chrome_lines_find_alternating_footers_at_any_page_count(page_count):
    """Issue #864: book layouts put the running footer on alternate pages. A
    fixed-step sample of 64 pages took only even pages whenever the step was
    an even integer (128, 384 or 640 pages), so a footer on odd pages was
    never counted and survived on half the manual (GA32-0983-01: 190 of 384
    pages)."""
    pages = [
        f"Entry {i} heading\nbody line {i}\n" + ("Synthetic Guide: Data Areas" if i % 2 else f"Chapter {i // 40 + 1}. Part")
        for i in range(page_count)
    ]
    chrome = chrome_lines(pages)
    assert "synthetic guide: data areas" in chrome
    assert strip_page(pages[1], chrome) == "Entry 1 heading\nbody line 1"


def test_chrome_lines_skip_labels_that_also_repeat_in_page_bodies():
    """Issue #864 (#604 guard): counting every page found real footers, but
    also made structural labels chrome whenever they sat at a page edge on
    35% of pages ("Explanation:", "Return code", "Offset"). A running footer
    never appears in a page body; a label does (bpx1mc10: "explanation:" on
    360 edge pages and 519 body pages), so it stays content."""
    def page(i: int) -> str:
        filler = [f"Detail line {i}.{j} of the entry text." for j in range(5)]
        top = ["Explanation:"] if i % 2 else []  # a continued entry starts the page
        return "\n".join([*top, f"Entry {i}", *filler, "Explanation:", *filler, "Synthetic Messages Guide"])

    pages = [page(i) for i in range(40)]
    chrome = chrome_lines(pages)
    assert "synthetic messages guide" in chrome
    assert "explanation:" not in chrome
    assert strip_page(pages[1], chrome).startswith("Explanation:\nEntry 1")


def test_strip_page_drops_copyright_notices_at_page_edges():
    """Issue #864: a copyright notice prints on chapter-opening pages only
    (18-35% of pages in small manuals), under the frequency threshold. At a
    page edge, a line shaped like a notice (an optional copyright sign,
    "copyright" and a year) is chrome; in the body, and without a year, it
    stays."""
    body = "\n".join(f"Generated body line {i}." for i in range(6))
    for notice in ("© Copyright IBM Corp. 1990, 2017", "(C) Copyright Synthetic Corp 2026",
                   " Copyright Example Corporation 1988, 2015", "Copyright Synthetic Corp. 2001"):
        assert strip_page(f"{body}\n{notice}", set()) == body
        assert strip_page(f"{notice}\n{body}", set()) == body
    middle = "a.\nb.\nc.\nd.\n© Copyright IBM Corp. 1990, 2017\ne.\nf.\ng.\nh."
    assert "© Copyright IBM Corp. 1990, 2017" in strip_page(middle, set())
    for kept in ("Copyright notices are listed in Appendix B", "The COPYRIGHT keyword sets the owner"):
        assert strip_page(f"{body}\n{kept}", set()) == f"{body}\n{kept}"


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


def test_strip_page_drops_bare_page_numbers_at_page_edges():
    """Page-number stripping is per-line and does not depend on chrome detection.
    Roman footers take the same trailing punctuation as decimal ones — front
    matter renders as "iv." as often as "iv" (one concept, one rule). Folios
    are recognized only in the page's edge lines (issue #604): the same
    numbers in the page body are content, and a header-edge number next to a
    footer folio is content too (a page has one folio)."""
    page = "body\n1234\niv.\nxii.\nXIV-"
    assert strip_page(page, set()) == "body"
    interior = "intro\n7\ntext a\ntext b\ntext c\ntext d\ntext e"
    assert strip_page(interior + "\nxii.", set()) == interior
    body = "body a\nbody b\nbody c\nbody d"
    assert strip_page(f"7\n{body}", set()) == body  # header folio, no footer folio
    assert strip_page(f"7\n{body}\n8", set()) == f"7\n{body}"  # the footer folio wins


def test_strip_page_drops_entry_range_headers_above_a_footer_folio():
    """Message manuals print the page's first and last entry as a header
    ("ICH408I • ICH409I") above a footer folio. The one-folio rule kept it as
    content on 723 pages of two local message manuals; a range is never body
    text, so it goes at either edge."""
    body = "\n".join(f"Generated entry text line {i}." for i in range(8))
    for header in ("ICH408I \u2022 ICH409I", "IRRA001I \u2022 IRRA009I", "U901 \u2022 U902"):
        assert strip_page(f"{header}\n{body}\n17", set()) == body
    # Code families with a lowercase placeholder and all-letter hex codes.
    for footer in ("0BB \u2022 0Cx", "EC7 \u2022 FFx", "AC8 \u2022 AFB", "805 \u2022 806"):
        assert strip_page(f"{body}\n{footer}\n17", set()) == body


    # Bullet pairs that are not entry ranges stay content.
    for line in ("Yes \u2022 No", "TSO \u2022 ISPF", "Return Code=4 \u2022 Return Code=8"):
        assert strip_page(f"{line}\n{body}\n17", set()) == f"{line}\n{body}"
    # An entry range in the page body is left alone, like a bare number.
    page = "a.\nb.\nc.\nd.\nICH408I \u2022 ICH409I\ne.\nf.\ng.\nh."
    assert "ICH408I \u2022 ICH409I" in strip_page(page, set())


def test_strip_page_keeps_blank_lines_and_inner_dot_numbers():
    """Blank lines are never page numbers. A standalone "1.2" is a section
    number, not a page footer; dash-between stays a page range."""
    assert strip_page("body\n\n   \n\t\n7", set()) == "body\n\n   \n\t"
    out = strip_page("intro\n1.2\n2.10\n12-34", set())
    assert "1.2" in out and "2.10" in out
    assert "12-34" not in out


def test_chunk_type_vocabulary_is_frozen():
    """AGENTS.md lethal rule: no new chunk_type values. The Literal annotation
    makes mypy reject a new return; this pins the exact vocabulary and fires
    classify() on each member."""
    from typing import get_args

    from mainframe_rag.ingest.classify import ChunkType, classify

    assert set(get_args(ChunkType)) == {"message", "syntax", "table", "narrative"}
    samples = [
        ("IEA500I Message text here.", "message"),
        ("Syntax: ::= with a parm <name>", "syntax"),
        ("NAME     TYPE    LEN\nALPHA    CHAR    10\nBETA     NUM     4", "table"),
        ("This is a plain narrative paragraph about system concepts.", "narrative"),
    ]
    for text, expected in samples:
        assert classify(text) == expected
        assert classify(text) in get_args(ChunkType)
