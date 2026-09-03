#!/usr/bin/env python3
"""Generate original test PDFs (NOT vendor manuals). Never commit the output."""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf

FOOTER = "(c) Synthetic Corp 2026 - Fixture for testing only"

PAGES = [
    ("Synthetic Operating System Reference\nz/OS V9R9\nSA22-0000-00\n\n"
    "This fixture was generated for tests. It contains no real vendor content."),
    ("Contents\n\nChapter 1 System parameters ........ 3\n"
    "Chapter 2 Operator messages ....... 5\nAppendix A Notices .............. 7"),
    "Figures\n\nFigure 1. Layout ................ 5",
    ("Chapter 1 System parameters\n\n"
    "IEASYSxx contains system initialization parameters. The synthetic LFAREA "
    "parameter defines the size of the invented lookaside facility.\n\n"
    "PROGxx controls the synthetic program authorization list. APF entries are "
    "described here for testing only."),
    ("Chapter 1 System parameters\n\n"
    "Table 1. Synthetic parameters\n\n"
    "Parameter   Meaning\n"
    "LFAREA      Lookaside facility size\n"
    "PROGxx      Program authorization"),
    ("Chapter 2 Operator messages\n\n"
    "IEA500I\n\n"
    "IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy\n\n"
    "Explanation: A synthetic IOSCMDS command was rejected by the fixture "
    "before IOS initialization completed. yy is a two digit reason code.\n\n"
    "System action: The system ignores the synthetic command.\n\n"
    "Operator response: Reissue the command after initialization completes."),
    ("Chapter 2 Operator messages\n\n"
    "Syntax diagrams for the synthetic IOSCMDS command:\n\n"
    ">>-IOSCMDS--+-APPLY-+--parameter-name-------------------------><\n"
    "            +-LIST--+\n\n"
    "::= describes the invented command grammar for testing."),
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


def build(
    out_path: Path,
    doc_id: str = "SA22-0000-00",
    title: str = "Synthetic Operating System Reference",
    message_id: str = "IEA500I",
) -> Path:
    """Build the IBM-shaped fixture. doc_id/title/message_id are parameterized
    so the simulation tier can build genuinely distinct documents — identical
    bodies would tie in RRF and flip top-1 between runs. The defaults keep the
    classic fixture unchanged."""
    header = f"{doc_id} {title}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for i, text in enumerate(PAGES):
        if i == 0:
            body = text.split("\n\n", 1)[1]
            text = f"{title}\nz/OS V9R9\n{doc_id}\n\n{body}"
        text = text.replace("IEA500I", message_id)
        page = doc.new_page()
        w, h = page.rect.width, page.rect.height
        page.insert_textbox(pymupdf.Rect(72, 72, w - 72, h - 90), text, fontsize=11)
        page.insert_textbox(pymupdf.Rect(72, 30, w - 72, 55), header, fontsize=8)
        page.insert_textbox(pymupdf.Rect(72, h - 60, w - 72, h - 40), FOOTER, fontsize=8)
    doc.set_toc(
        [[level, message_id if t == "IEA500I" else t, page_no] for level, t, page_no in TOC]
    )
    doc.set_page_labels(
        [{"startpage": 0, "prefix": "1-", "style": "D", "firstpagenumber": 1}]
    )
    doc.set_metadata({"title": title, "author": "Synthetic Corp"})
    doc.save(out_path)
    doc.close()
    return out_path


def build_plain(out_path: Path) -> Path:
    """Generic PDF: no form number, no outline. Original test prose."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    pages = [
        "Acme Widget Controller Guide\nThis guide describes the Acme Widget Controller.\nWidgets store torque in a buffer.",
        "Installation\nMount the widget on a 19-inch rack.\nTorque the screws to 5 Nm.",
        "Messages\nWDG001I BUFFER FULL\nThe torque buffer reached capacity. Drain it before retry.",
    ]
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    doc.set_metadata({"title": "Acme Widget Controller Guide", "author": "pdf-rag test fixture"})
    doc.save(out_path)
    doc.close()
    return out_path


# JCL sample job (issue #79): cards, //* comments, in-stream SYSIN data,
# IF/ENDIF logic, and col-72-style continuations (// + whitespace continue
# the open statement). Placed line by line with insert_text: textbox flow
# would rewrap the cards and destroy the line structure under test.
JCL_BASE_LINES = [
    "//WIDGETB JOB (ACCT),'WIDGET BUILD',CLASS=A,MSGCLASS=X",
    "//*--------------------------------------------------",
    "//* Build the widget inventory file - nightly batch",
    "//*--------------------------------------------------",
    "//STEP1 EXEC PGM=IEBGENER",
    "//SYSUT1 DD DSN=WIDGET.INPUT.DATA,DISP=SHR",
    "//SYSUT2 DD DSN=WIDGET.OUTPUT.INV,DISP=(NEW,CATLG,DELETE),",
    "//            UNIT=SYSDA,SPACE=(CYL,(10,5),RLSE),",
    "//            DCB=(RECFM=FB,LRECL=80,BLKSIZE=8000)",
    "//SYSPRINT DD SYSOUT=*",
    "//SYSIN DD DUMMY",
    "//STEP2 EXEC PGM=SORT,COND=(0,NE)",
    "//SORTIN DD DSN=WIDGET.OUTPUT.INV,DISP=SHR",
    "//SORTOUT DD DSN=WIDGET.OUTPUT.SRT,DISP=(NEW,CATLG,DELETE),",
    "//            UNIT=SYSDA,SPACE=(CYL,(5,2),RLSE)",
    "//SYSOUT DD SYSOUT=*",
    "//SYSIN DD *",
    "  SORT FIELDS=(1,10,CH,A)",
    "/*",
    "//IFSTEP1 IF (STEP1.RC = 0) THEN",
    "//NOTIFY EXEC PGM=IKJEFT01,DYNAMNBR=20",
    "//SYSTSPRT DD SYSOUT=*",
    "//SYSTSIN DD *",
    "  SEND 'WIDGET BUILD COMPLETE' USER(OPERATOR)",
    "/*",
    "//ENDIF",
]


def _write_code_lines(doc: pymupdf.Document, lines: list[str], fontsize: int = 10, step: int = 12) -> None:
    """One page per ~55 lines; insert_text per line so cards keep their
    line breaks (no textbox rewrap, no clipping of short lines). Leading
    spaces are preserved: real manuals indent examples, and the detector
    must see them that way."""
    page = doc.new_page()
    y = 72
    for line in lines:
        if y > page.rect.height - 40:
            page = doc.new_page()
            y = 72
        page.insert_text((72, y), line, fontsize=fontsize)
        y += step


def build_jcl(out_path: Path, doc_id: str = "SA22-8004-00") -> Path:
    """JCL fixture: a small documented job plus a multi-page run of
    generated steps (~6000 chars, no blank lines) that forces accumulation
    flushes mid-region — every flush and overlap seed must still land on
    statement boundaries. Single-paragraph oversize slicing is pinned at
    unit level (a > SECTION_MAX_CHARS para cannot fit one PDF page)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    title = "Synthetic JCL Samples"
    page = doc.new_page()
    page.insert_text((72, 72), f"{title}\nz/OS V9R9\n{doc_id}", fontsize=11)
    _write_code_lines(doc, JCL_BASE_LINES)
    # Indented example block, as IBM manuals print JCL inside running text:
    # the detector must see lstripped cards, and continuations must stay
    # glued to their opener across the indent. The prose lead shares the
    # paragraph (no blank emitted) and must survive as its own unit.
    _write_code_lines(
        doc,
        [
            "Example job as printed in the manual:",
            "    //EXJOB JOB (ACCT),'EXAMPLE',CLASS=A",
            "    //EXSTEP EXEC PGM=IEFBR14",
            "    //INDATA DD DSN=EXAMPLE.INPUT,DISP=SHR",
            "    //OUTDATA DD DSN=EXAMPLE.OUTPUT,DISP=(NEW,CATLG,DELETE),",
            "    //            UNIT=SYSDA,SPACE=(CYL,(2,1),RLSE)",
            "    // EXEC PGM=IKJEFT01",
        ],
    )
    generated = []
    for n in range(1, 61):
        generated.append(f"//S{n:02d} EXEC PGM=IEFBR14,PARM='BLD{n:02d}'")
        generated.append(f"//* step {n:02d} of the nightly widget build")
        if n % 5 == 0:
            generated.append(f"//DD{n:02d} DD DSN=WIDGET.GEN{n:02d}.DATA,DISP=(NEW,CATLG,DELETE),")
            generated.append("//            UNIT=SYSDA,SPACE=(CYL,(1,1),RLSE)")
    _write_code_lines(doc, generated)
    doc.set_metadata({"title": title, "author": "pdf-rag test fixture"})
    doc.save(out_path)
    doc.close()
    return out_path


# REXX sample program (issue #79): /* REXX */ header, a multi-line block
# comment, ;-terminated statements, quoted semicolons (must not split),
# and a comma line-continuation.
REXX_LINES = [
    "/* REXX */",
    "/* Widget inventory reporter.",
    "   Reads the nightly file and mails",
    "   the summary to operations. */",
    'PARSE ARG infile outfile',
    'IF infile = "" THEN DO',
    '  SAY "Usage: reporter <infile> <outfile>";',
    "  EXIT 8;",
    "END",
    "/* Open and validate; RC must be 0",
    "   before the summary step runs. */",
    "CALL SysOpen",
    "rc = RESULT",
    "IF rc \\= 0 THEN DO",
    '  SAY "Open failed, RC="rc"; aborting.";',
    "  EXIT rc;",
    "END",
    "total = records + errors + ,",
    "  warnings + infos;",
    "DO i = 1 TO 10",
    "  line = LINEIN(infile);",
    '  IF line = "" THEN LEAVE;',
    "  CALL ProcessLine line;",
    "END",
    "CALL SysClose",
    "EXIT 0;",
]


def build_rexx(out_path: Path, doc_id: str = "SA22-8005-00") -> Path:
    """REXX fixture: one program exercising header, block comments,
    semicolons inside string literals, and comma continuations."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    title = "Synthetic REXX Samples"
    page = doc.new_page()
    page.insert_text((72, 72), f"{title}\nz/OS V9R9\n{doc_id}", fontsize=11)
    _write_code_lines(doc, REXX_LINES)
    doc.set_metadata({"title": title, "author": "pdf-rag test fixture"})
    doc.save(out_path)
    doc.close()
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--plain", action="store_true")
    args = parser.parse_args()
    print(build_plain(args.out) if args.plain else build(args.out))


if __name__ == "__main__":
    main()
