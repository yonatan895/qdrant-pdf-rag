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


# "Synthetic Corp" demo manuals (issue #890): a small invented product family
# with original prose, used by the scripted console recording
# (scripts/record_demo.py). Every fact below is made up for this fixture; there
# is no vendor text. Each page is (heading, body); `build_corp_manual` lays one
# out per PDF page and raises when a page would overflow, so a wording edit can
# never silently clip a sentence.
CORP_FOOTER = "(c) Synthetic Corp 2026 - Invented demo manual, not a real product"

CORP_MANUALS: dict[str, dict] = {
    "SC-1001-00_Meridian_Scheduler_Operations_Guide": {
        "title": "Meridian Scheduler Operations Guide",
        "doc_id": "SC-1001-00",
        "pages": [
            ("Meridian Scheduler Operations Guide",
             "Synthetic Corp Meridian Scheduler 4.2\nDocument number SC-1001-00\n\n"
             "Meridian is an invented batch job scheduler used only to demonstrate "
             "document search. This guide explains how operators start, tune and "
             "monitor the scheduler. Message explanations are in the Meridian "
             "Messages Reference, SC-1002-00."),
            ("Chapter 1 Starting the scheduler",
             "The scheduler runs as a dispatcher process and one or more worker "
             "nodes. Start the dispatcher first with the command mrdctl dispatcher "
             "start, then start each worker with mrdctl worker start.\n\n"
             "The dispatcher refuses to start when the state directory is locked by "
             "another dispatcher. Remove the lock only after confirming that the "
             "other dispatcher is stopped.\n\n"
             "A normal start takes about twelve seconds. The dispatcher writes the "
             "message MRD1001I when it is ready to accept jobs."),
            ("Chapter 2 Queue leases",
             "Every worker holds a lease on the queue it serves. The lease proves "
             "that the worker is alive. If a worker cannot renew its lease before "
             "the lease timeout elapses, the dispatcher takes the queue back and "
             "hands its pending jobs to another worker.\n\n"
             "The lease timeout is set by the LEASE_TIMEOUT_SEC parameter. The "
             "default is 90 seconds. Valid values are 30 through 600 seconds. A "
             "value below 30 is rejected when the dispatcher starts.\n\n"
             "Raise the value on slow networks. Lower it when failover must be "
             "faster, but keep it at least three times the renewal interval."),
            ("Chapter 2 Queue leases",
             "Table 1. Lease parameters\n\n"
             "Parameter            Default   Meaning\n"
             "LEASE_TIMEOUT_SEC    90        Seconds before an unrenewed lease expires\n"
             "LEASE_RENEW_SEC      30        Seconds between lease renewals\n"
             "MAX_CLOCK_SKEW_SEC   5         Largest tolerated clock difference\n\n"
             "Clock differences between the dispatcher and a worker shorten the "
             "effective lease. When the difference exceeds MAX_CLOCK_SKEW_SEC the "
             "worker is marked suspect."),
            ("Chapter 3 Monitoring",
             "Use mrdctl status to list workers, leases and queue depth. The "
             "command mrdctl clock-check compares the clock of every worker with "
             "the dispatcher and prints the difference in seconds.\n\n"
             "Queue depth above 500 pending jobs for ten minutes should be "
             "treated as a capacity problem, not a fault. Add a worker or raise "
             "the per-worker slot count with the SLOTS parameter."),
            ("Appendix A Notices",
             "This is an invented manual written for a demonstration. Meridian "
             "and Synthetic Corp do not exist as products or companies."),
        ],
    },
    "SC-1002-00_Meridian_Messages_Reference": {
        "title": "Meridian Messages Reference",
        "doc_id": "SC-1002-00",
        "pages": [
            ("Meridian Messages Reference",
             "Synthetic Corp Meridian Scheduler 4.2\nDocument number SC-1002-00\n\n"
             "This reference lists the messages written by the invented Meridian "
             "scheduler. Each entry gives an explanation, the system action and "
             "the operator response."),
            ("Chapter 1 Dispatcher messages",
             "MRD1001I\n\nMRD1001I DISPATCHER READY, QUEUES=nn\n\n"
             "Explanation: The dispatcher finished starting and is accepting "
             "jobs. nn is the number of queues it opened.\n\n"
             "System action: Processing continues.\n\n"
             "Operator response: None."),
            ("Chapter 1 Dispatcher messages",
             "MRD2041E\n\nMRD2041E LEASE EXPIRED FOR QUEUE qname, WORKER=wname\n\n"
             "Explanation: The worker wname did not renew its lease on queue "
             "qname before LEASE_TIMEOUT_SEC elapsed. The usual causes are a "
             "stalled worker, a congested network, or clock skew larger than "
             "MAX_CLOCK_SKEW_SEC.\n\n"
             "System action: The dispatcher takes the queue back and reassigns "
             "its pending jobs to another worker.\n\n"
             "Operator response: Run mrdctl clock-check. If the skew is more "
             "than 5 seconds, resynchronize the worker clock. Then run mrdctl "
             "worker restart wname to return the worker to service."),
            ("Chapter 1 Dispatcher messages",
             "MRD2042W\n\nMRD2042W WORKER wname MARKED SUSPECT, SKEW=ss\n\n"
             "Explanation: The clock of worker wname differs from the "
             "dispatcher clock by ss seconds, which is more than "
             "MAX_CLOCK_SKEW_SEC.\n\n"
             "System action: The worker keeps its queues but receives no new "
             "jobs until its clock is within the limit.\n\n"
             "Operator response: Resynchronize the worker clock."),
            ("Chapter 2 Worker messages",
             "MRD3007E\n\nMRD3007E JOB jobid ENDED WITH CODE cc\n\n"
             "Explanation: Job jobid ended with the non-zero return code cc.\n\n"
             "System action: The job is marked failed and its successors wait.\n\n"
             "Operator response: Read the job log, correct the cause, and "
             "resubmit the job with mrdctl job retry jobid."),
        ],
    },
    "SC-2001-00_Harbor_Archive_Administration_Guide": {
        "title": "Harbor Archive Administration Guide",
        "doc_id": "SC-2001-00",
        "pages": [
            ("Harbor Archive Administration Guide",
             "Synthetic Corp Harbor Archive 2.0\nDocument number SC-2001-00\n\n"
             "Harbor is an invented long-term archive for job output. This guide "
             "explains retention, restore and capacity planning."),
            ("Chapter 1 Retention",
             "Harbor keeps job output for the retention period of its class. The "
             "class standard keeps output for 30 days. The class audit keeps "
             "output for 400 days and cannot be shortened once a record is "
             "written.\n\n"
             "Expired records are removed by the nightly sweep, which starts at "
             "02:00 local time."),
            ("Chapter 2 Restoring output",
             "Restore a record with harborctl restore recordid. A record younger "
             "than seven days restores immediately. An older record is read from "
             "cold storage and takes up to one hour.\n\n"
             "A restore never overwrites existing output. It writes a copy with "
             "the suffix .restored."),
        ],
    },
}


def build_corp_manual(out_path: Path, spec: dict) -> Path:
    """One Synthetic Corp demo manual: heading plus body per page, an outline
    from the page headings, and the fixed fixture footer."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    header = f"{spec['doc_id']} {spec['title']}"
    toc: list[list] = []
    for number, (heading, body) in enumerate(spec["pages"], start=1):
        page = doc.new_page()
        w, h = page.rect.width, page.rect.height
        text = f"{heading}\n\n{body}"
        leftover = page.insert_textbox(pymupdf.Rect(72, 72, w - 72, h - 90), text, fontsize=11)
        if leftover < 0:
            raise ValueError(f"{out_path.name} page {number}: text overflows the page")
        page.insert_textbox(pymupdf.Rect(72, 30, w - 72, 55), header, fontsize=8)
        page.insert_textbox(pymupdf.Rect(72, h - 60, w - 72, h - 40), CORP_FOOTER, fontsize=8)
        if heading.startswith(("Chapter", "Appendix")) and (not toc or toc[-1][1] != heading):
            toc.append([1, heading, number])
    if toc:
        doc.set_toc(toc)
    doc.set_metadata({"title": spec["title"], "author": "Synthetic Corp"})
    doc.save(out_path)
    doc.close()
    return out_path


def build_corp_manuals(out_dir: Path) -> list[Path]:
    """Write every Synthetic Corp demo manual into out_dir (filename-stem
    identity, no vendor signals); returns the paths in name order."""
    return [
        build_corp_manual(out_dir / f"{stem}.pdf", spec)
        for stem, spec in sorted(CORP_MANUALS.items())
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--plain", action="store_true")
    parser.add_argument(
        "--synthetic-corp",
        action="store_true",
        help="write the Synthetic Corp demo manuals (issue #890) into the directory --out",
    )
    args = parser.parse_args()
    if args.synthetic_corp:
        for path in build_corp_manuals(args.out):
            print(path)
        return
    print(build_plain(args.out) if args.plain else build(args.out))


if __name__ == "__main__":
    main()
