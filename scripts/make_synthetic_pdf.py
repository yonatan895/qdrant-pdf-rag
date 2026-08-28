#!/usr/bin/env python3
"""Generate the synthetic IBM-shaped test PDF (NOT a real manual).

Fixture contract (architecture.md section 5.3):
- doc number SA22-0000-00 on page 1
- outline (bookmarks) with front matter + mid-book sections
- a fake IEA500I message section (invented text, no IBM content)
- printed page labels "1-1", "1-2", ...
Output is committed under tests/fixtures/synthetic/ and is regenerated if missing.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf

HEADER = "SA22-0000-00 Synthetic Operating System Reference"
FOOTER = "(c) Synthetic Corp 2026 - Fixture for testing only"

PAGES = [
    # (chapter/section bookmark structure is expressed via TOC below)
    "Synthetic Operating System Reference\nz/OS V9R9\nSA22-0000-00\n\n"
    "This fixture was generated for tests. It contains no real vendor content.",
    "Contents\n\nChapter 1 System parameters ........ 3\n"
    "Chapter 2 Operator messages ....... 5\nAppendix A Notices .............. 7",
    "Figures\n\nFigure 1. Layout ................ 5",
    "Chapter 1 System parameters\n\n"
    "IEASYSxx contains system initialization parameters. The synthetic LFAREA "
    "parameter defines the size of the invented lookaside facility.\n\n"
    "PROGxx controls the synthetic program authorization list. APF entries are "
    "described here for testing only.",
    "Chapter 1 System parameters\n\n"
    "Table 1. Synthetic parameters\n\n"
    "Parameter   Meaning\n"
    "LFAREA      Lookaside facility size\n"
    "PROGxx      Program authorization",
    "Chapter 2 Operator messages\n\n"
    "IEA500I\n\n"
    "IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy\n\n"
    "Explanation: A synthetic IOSCMDS command was rejected by the fixture "
    "before IOS initialization completed. yy is a two digit reason code.\n\n"
    "System action: The system ignores the synthetic command.\n\n"
    "Operator response: Reissue the command after initialization completes.",
    "Chapter 2 Operator messages\n\n"
    "Syntax diagrams for the synthetic IOSCMDS command:\n\n"
    ">>-IOSCMDS--+-APPLY-+--parameter-name-------------------------><\n"
    "            +-LIST--+\n\n"
    "::= describes the invented command grammar for testing.",
    "Appendix A Notices\n\nThis is synthetic back matter used to test skip rules.",
]

TOC = [
    [1, "Contents", 2],
    [1, "Figures", 3],
    [1, "Chapter 1 System parameters", 4],
    [2, "IEASYSxx parameters", 4],
    [2, "PROGxx parameters", 5],
    [1, "Chapter 2 Operator messages", 6],
    [2, "IEA500I", 6],
    [2, "IOSCMDS syntax", 7],
    [1, "Appendix A Notices", 8],
]


def build(out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for i, text in enumerate(PAGES):
        page = doc.new_page()
        w, h = page.rect.width, page.rect.height
        page.insert_textbox(pymupdf.Rect(72, 72, w - 72, h - 90), text, fontsize=11)
        page.insert_textbox(pymupdf.Rect(72, 30, w - 72, 55), HEADER, fontsize=8)
        page.insert_textbox(pymupdf.Rect(72, h - 60, w - 72, h - 40), FOOTER, fontsize=8)
    doc.set_toc(TOC)
    doc.set_page_labels(
        [{"startpage": 0, "prefix": "1-", "style": "D", "firstpagenumber": 1}]
    )
    doc.set_metadata({"title": "Synthetic Operating System Reference", "author": "Synthetic Corp"})
    doc.save(out_path)
    doc.close()
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser()
    default = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "synthetic" / "SA22-0000-00_outline.pdf"
    parser.add_argument("--out", type=Path, default=default)
    args = parser.parse_args()
    print(build(args.out))


if __name__ == "__main__":
    main()
