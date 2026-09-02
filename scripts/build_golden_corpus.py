#!/usr/bin/env python3
"""Build the golden corpus (dev + holdout) from the expert seed + payload mining.

Inputs (read-only):
  - evals/expert_golden_seed.jsonl  (sanitized operator-history seed, answer-tier gold)
  - the live collection at QDRANT_URL (message-id -> docs map, doc titles)

Outputs:
  - evals/golden.jsonl              (dev set)
  - evals/holdout.jsonl             (frozen holdout; sha256 pinned alongside)
  - evals/holdout.jsonl.sha256

Every message-id expectation is asserted against the live payload map before
anything is written; the build fails loudly on broken bindings, missing docs,
or duplicate queries. Split is deterministic (~60/40 dev/holdout per class).

Re-bind runbook (when vendor books are loaded — e.g. the shop's CICS, MQ,
IMS, JES3, and z/OS 2.2-era manuals):
  1. Load the books into the collection (user-supplied PDFs; never in git).
  2. Re-run this script. Seed entries whose domain was absent abstain only
     while their identifiers cannot be bound (ABSENT_DOMAIN_FALLBACK); once
     the books carry them they flip back to answer automatically with the
     abstention notes dropped — but only when the id shape is parseable by
     MSG_RE (MVS-style XXXnnnY). Vendor shapes outside it (DFH*, CSQ*,
     HASP*, IAT*, DSNT*) need manual SEED_OVERRIDES bindings even when the
     books are present. FORCE_ABSTAIN entries stay abstain and print a loud
     warning if they become bindable. Unbindable no-identifier entries
     (VER-02/03/04, SYN-06, DIA-04, ...) need manual SEED_OVERRIDES bindings.
  3. Author new entries for the loaded domains (mine real DFH*/CSQ*/DFS*/
     HASP* message IDs) to restore class balance.
  4. make verify-golden must be 0 FAIL, then re-freeze: the holdout sha
     changes, so re-record evals/holdout-baseline.json and commit the new
     pin + baselines as one dedicated re-freeze commit. Never iterate
     against the holdout to tune.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
SEED = REPO / "evals" / "expert_golden_seed.jsonl"
FACTS = Path("/tmp/opencode/corpus_facts.json")

SEED_CLASS_MAP = {
    "message_id_lookup": "message_id",
    "doc_number_identifier": "doc_number",
    "syntax_construction": "syntax",
    "diagnostic_recovery": "diagnostic",
    "comparative_tuning": "comparative",
    "version_sensitive": "version",
    "negative_trap": "negative",
}
SEED_BEHAVIOR_MAP = {
    "cite_lookup": "answer",
    "identifier_resolve": "answer",
    "synthesize_syntax": "answer",
    "diagnose_recover": "answer",
    "compare_tune": "answer",
    "version_disambiguate": "answer",
    "abstain": "abstain",
}

OPS_DOC = "ca-ops-mvs-event-management-and-automation-14-0"
INIT_REF = "SA23-1380-70"  # z/OS MVS Initialization and Tuning Reference (parmlib owner)

# ---------------------------------------------------------------- entry table
# (id, query, query_class, expected_doc_ids, expected_heading, expected_page,
#  must_not_retrieve, must_not_message_ids, note)
# expected_behavior is "answer" unless listed in ABSTAIN set below.
E = []


def e(id_, query, cls, docs, *, heading=None, page=None, must_not=None, must_not_msgs=None, note=None, trap_type=None):
    if trap_type and note:
        note = f"{note} (trap_type={trap_type})"
    elif trap_type:
        note = f"trap_type={trap_type}"
    E.append({
        "id": id_, "query": query, "query_class": cls, "expected_doc_ids": docs,
        "expected_heading": heading, "expected_page": str(page) if page is not None else None,
        "must_not_retrieve": must_not or [], "must_not_message_ids": must_not_msgs or [],
        "note": note,
    })


# --- legacy 12 (migrated; user-verified z/OS 3.2 books; stay dev) -------------
LEG = [
    ("NFS mount error return codes", "diagnostic", ["SC23-6883-70"]),
    ("DFSORT sysin squeeze parameter", "syntax", ["SC23-6878-70"]),
    ("hsm journal filling up troubleshooting", "diagnostic", ["SC23-6871-70"]),
    ("SC23-6883-70", "doc_number", ["SC23-6883-70"]),
    ("SC23-6878-70", "doc_number", ["SC23-6878-70"]),
    ("SC23-6871-70", "doc_number", ["SC23-6871-70"]),
    ("Network File System mount", "diagnostic", ["SC23-6883-70"]),
    ("DFSORT application programming", "diagnostic", ["SC23-6878-70"]),
    ("DFSMShsm storage administration", "diagnostic", ["SC23-6871-70"]),
    ("DFSMShsm implementation customization", "diagnostic", ["SC23-6869-70"]),
    ("DFSORT messages codes diagnosis", "diagnostic", ["SC23-6879-70"]),
    ("DFSORT tuning", "comparative", ["SC23-6882-70"]),
]
for i, (q, cls, docs) in enumerate(LEG, 1):
    e(f"LEG-{i:02d}", q, cls, docs,
      note="user query 1-12 from the original golden set, expectations verified against the local z/OS 3.2 corpus")

# --- new: message_id lookups (canonical System Messages volumes) ---------------

e("MSG-09", "ARC0500I appeared in DFSMShsm output. What does the message book say it reports and what action follows?", "message_id", ["SA38-0669-70"])
e("MSG-10", "CBR3300I was issued by the tape library. What does the manual say it reports?", "message_id", ["SA38-0671-70"])
e("MSG-11", "ATR218I came from RRS. What does the message document and what should the operator do?", "message_id", ["SA38-0670-70"])
e("MSG-12", "What does AHL124I report during a GTF trace?", "message_id", ["SA38-0668-70"])
e("MSG-13", "IEE400I showed up at IPL. What condition does the message book describe?", "message_id", ["SA38-0674-70"], heading="IEE400I")
e("MSG-14", "RMM issued EDG4004I during a tape mount. What is documented?", "message_id", ["SA38-0672-70"])
e("MSG-15", "IGD002I appeared during allocation. What does the message indicate per the manual?", "message_id", ["SA38-0675-70"])
e("MSG-16", "The console is showing IEE012A. What reply does the manual document?", "message_id", ["SA38-0674-70"], heading="IEE012A")
e("MSG-17", "What does ATB002I report about APPC/MVS scheduling?", "message_id", ["SA38-0670-70"])
e("MSG-18", "IXG312E during log stream processing: what does the message book document?", "message_id", ["SA38-0677-70"])
e("MSG-19", "GRS issued ISG009D. Which replies does the manual document?", "message_id", ["SA38-0676-70"])
e("MSG-20", "EDG0215D came from RMM. What reply options are documented?", "message_id", ["SA38-0672-70"])
e("MSG-21", "What does CRU113I report about the tape library drive?", "message_id", ["SA38-0671-70"])
e("MSG-22", "DADSM produced ADR918I. What is the documented meaning?", "message_id", ["SA38-0668-70"])
e("MSG-23", "ADR497E during volume processing: what does the message book say?", "message_id", ["SA38-0668-70"])
e("MSG-24", "IEE196I appeared with display output. What does the manual say it is?", "message_id", ["SA38-0674-70"], heading="IEE196I")
e("MSG-25", "IOS207I for a device: what condition and operator action are documented?", "message_id", ["SA38-0676-70"], must_not_msgs=["IOS208I"],
     note="sibling trap: IOS208I is a real adjacent ID in the same volume and must not supply the context")
e("MSG-26", "IOS208I was issued for an I/O device. What does the manual document?", "message_id", ["SA38-0676-70"], must_not_msgs=["IOS207I"],
     note="sibling trap: IOS207I is a real adjacent ID in the same volume")
e("MSG-27", "IEF238D is waiting for a reply about a volume. What reply texts are documented?", "message_id", ["SA38-0675-70", "SA38-0666-70"])
e("MSG-28", "ARC0184I from DFHSM: what does the message book say it reports?", "message_id", ["SA38-0669-70", "SC23-6871-70"])
e("MSG-29", "What does ARC0835I report in DFSMShsm processing?", "message_id", ["SA38-0669-70"])
e("MSG-30", "GTF is asking AHL101A for options. Which replies does the manual document?", "message_id", ["SA38-0668-70"])
e("MSG-31", "AMD001A appeared during SMS processing. What does the message document?", "message_id", ["SA38-0668-70"])
e("MSG-32", "ICU010I was issued. What does the message book say about it?", "message_id", ["SA38-0673-70"])
e("MSG-33", "ARC0734I from DFSMShsm: what is the documented meaning?", "message_id", ["SA38-0669-70"])
e("MSG-34", "IGD17002I during SMS allocation: what does it report?", "message_id", ["SA38-0675-70"])
e("MSG-35", "IEE892I was displayed at the console. What does the message book say about it?", "message_id", ["SA38-0674-70"], heading="IEE892I")
e("MSG-36", "IXC207A from XCF: what does the message document?", "message_id", ["SA38-0677-70"])

# --- new: doc_number identifiers (full suffix form; suffix-less numbers hit the
# exact-match doc_id filter gap and are documented as a follow-up) --------------

e("DOC-07", "What manual is SC23-6846-70 and which commands does it define?", "doc_number", ["SC23-6846-70"])
e("DOC-08", "GC35-0033-70", "doc_number", ["GC35-0033-70"])
e("DOC-09", "Which publication carries number SA23-1385-70 and what is its scope?", "doc_number", ["SA23-1385-70"])
e("DOC-10", "SA32-0992-70", "doc_number", ["SA32-0992-70"])
e("DOC-11", "Identify document SC34-2662-70 and its subject area.", "doc_number", ["SC34-2662-70"])
e("DOC-12", "What book is SA23-1382-70 and what is it used for?", "doc_number", ["SA23-1382-70"])
e("DOC-13", "SA23-2274-70", "doc_number", ["SA23-2274-70"])
e("DOC-14", "Which manual is SC23-6855-70?", "doc_number", ["SC23-6855-70"])
e("DOC-15", "SA38-0666-70", "doc_number", ["SA38-0666-70"])
e("DOC-16", "Identify SC23-6879-70 and say what kind of content it holds.", "doc_number", ["SC23-6879-70"])

# --- new: syntax construction (parmlib members live in SA23-1380-70) -----------
PARMLIB = "Part 2. Members of SYS1.PARMLIB"

e("SYN-10", "Code a VATLSTxx entry that marks a user volume permanently resident, using only documented VATLSTxx syntax.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 86. VATLSTxx")
e("SYN-11", "Write a SCHEDxx PPT entry that gives a program non-swappable status, per documented SCHEDxx syntax.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 82. SCHEDxx")
e("SYN-12", "Define a new MCS console in CONSOLxx with a documented name and auth level.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 23. CONSOLxx")
e("SYN-13", "Write an MPFLSTxx entry that suppresses a noisy message ID on the operator console.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 76. MPFLSTxx")
e("SYN-14", "Code an LNKLSTxx statement that adds a user library to the LNKLST set after SYS1.LINKLIB.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 72. LNKLSTxx")
e("SYN-15", "Draft an LPALSTxx entry adding a library to LPA, following the documented syntax.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 74. LPALSTxx")
e("SYN-16", "Add a new SVC number with IEASVCxx using only documented parameters.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 54. IEASVCxx")
e("SYN-17", "Define a GRS ring configuration in GRSCNFxx per the documented syntax.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 34. GRSCNFxx")
e("SYN-18", "Use DIAGxx to enable common storage tracking as documented.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 30. DIAGxx")
e("SYN-19", "Set unit affinity and allocation system defaults in ALLOCxx using documented statements.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 4. ALLOCxx")
e("SYN-20", "Write CLOCKxx statements setting the time zone for UTC with the documented keywords.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 16. CLOCKxx")
e("SYN-21", "Define XCF signalling paths in COUPLExx with documented syntax.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 24. COUPLExx")
e("SYN-22", "Tune real storage management via IEAOPTxx with only documented parameters.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 51. IEAOPTxx")
e("SYN-23", "Write an IEASLPxx SLIP SET statement to capture a dump for an abend in a named module.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 53. IEASLPxx")
e("SYN-24", "Define a subsystem named BATCHA in IEFSSNxx keyword form per the documented syntax.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 59. IEFSSNxx")
e("SYN-25", "Configure a System Logger structure in IXGCNFxx with documented keywords.", "syntax", [INIT_REF], heading=f"{PARMLIB} > Chapter 70. IXGCNFxx")
e("SYN-26", "Write IDCAMS DEFINE CLUSTER syntax for a KSDS with the documented parameters.", "syntax", ["SC23-6846-70"])
e("SYN-27", "Use documented IDCAMS ALTER syntax to change a data set's share options.", "syntax", ["SC23-6846-70"])
e("SYN-28", "Code a RACF RDEFINE command for a general resource profile with UACC NONE, per documented syntax.", "syntax", ["SA23-2292-70"])
e("SYN-29", "Write the documented RACF CONNECT command syntax to add a user to a group.", "syntax", ["SA23-2292-70"])
e("SYN-30", "Write a REXX exec that uses ARG and SAY per the documented TSO/E REXX instructions.", "syntax", ["SA32-0982-70"])
e("SYN-31", "Show documented TSO/E ALLOCATE syntax allocating a new data set with a block size.", "syntax", ["SA32-0975-70"])
e("SYN-32", "Code an OPS/MVS AOF )REQ rule that responds to a request, using only documented AOF grammar.", "syntax", [OPS_DOC])
e("SYN-33", "Write an OPS/MVS AOF )CMD rule that issues a response command, per documented syntax.", "syntax", [OPS_DOC])
e("SYN-34", "Write DFSORT INCLUDE and SORT FIELDS statements for filtering and sorting fixed records, using documented syntax.", "syntax", ["SC23-6878-70"])
e("SYN-35", "Write JCL with a DD statement concatenating three data sets, following documented DD syntax.", "syntax", ["SA23-1385-70"])
e("SYN-36", "Show documented ICKDSF INIT command syntax to initialize a DASD volume.", "syntax", ["GC35-0033-70"])

# --- new: diagnostic / recovery ------------------------------------------------

e("DIA-09", "IEC161I during VSAM open: how should the reason codes be read and what recovery is documented?", "diagnostic",
     ["SA38-0674-70", "SA38-0674-06", "SC23-6852-70"],
     note="IEC range lives in System Messages Vol 7 in both the z/OS 2.2 kit and the 2.5-era -70 editions; both are honest answers")
e("DIA-10", "IOS071I rejected a command against a device. What do the reason codes mean per the manual?", "diagnostic", ["SA38-0676-70", "SA38-0666-70"])
e("DIA-11", "What does IEE699I tell the operator about the display output that follows it?", "diagnostic", ["SA38-0674-70"], heading="IEE699I")
e("DIA-12", "System Logger issued IXG601I. What condition is documented and what action follows?", "diagnostic", ["SA38-0677-70", "SC23-6843-70"])
e("DIA-13", "DFHSM issued ARC1001I during recall. What do the message and its reason code document?", "diagnostic", ["SA38-0669-70", "SC23-6871-70"])
e("DIA-14", "IGW467I from SMB serving: what does the manual say to check?", "diagnostic", ["SA38-0676-70"])
e("DIA-15", "An SVC dump was taken: what is the documented IPCS path to analyze the failing module?", "diagnostic", ["SA23-1384-70", "SA23-1382-70"])
e("DIA-16", "IBM Health Checker reported an exception: what does the check output tell the operator to do?", "diagnostic", ["SC23-6843-70"])
e("DIA-17", "Catalog DEFINE failures are cascading: which book documents catalog diagnosis and what steps does it specify?", "diagnostic", ["SC23-6863-70", "SC23-6861-70"])
e("DIA-18", "A tape mount is stuck pending: what does the tape documentation say to check on the drive and the library?", "diagnostic", ["SC23-6854-70"])
e("DIA-19", "GRS shows enqueue contention between two jobs: what does the documentation recommend to diagnose it with DISPLAY GRS?", "diagnostic", ["SA38-0666-70", INIT_REF])
e("DIA-20", "SLIP was set for an S0C4: how do SLIP and IEASLPxx document the trap and dump flow?", "diagnostic", [INIT_REF])
e("DIA-21", "A Language Environment CEExxxx abend occurred: what is the documented debugging path?", "diagnostic", ["GA32-0908-70"])
e("DIA-22", "SMF ran out of space: what does the SMF documentation say about managing SYS1.MAN data sets and switching?", "diagnostic", ["SA38-0667-70"])
e("DIA-23", "XCF signalling between two systems failed: what diagnosis does the sysplex documentation specify?", "diagnostic", ["SA23-1399-70", "SA38-0658-70"])
e("DIA-24", "IEA995I says a dump was produced: what documented next steps lead to the dump data set?", "diagnostic", ["SA38-0673-70", "SA23-1374-70"])
e("DIA-25", "A batch step is looping on CPU: what do the RMF and SMF books document to confirm the address space and its consumption?", "diagnostic", ["SC34-2665-70", "SA38-0667-70"])
e("DIA-26", "ICH408I denied a data set access: which RACF documentation explains how to read the profile and access fields?", "diagnostic", ["SA23-2287-70"])
e("DIA-27", "A DFSMShsm-managed volume is at capacity: what migration actions does the storage administration book document?", "diagnostic", ["SC23-6871-70"])
e("DIA-28", "A REXX exec failed with an IRX message: what do the IRX explanations say to check?", "diagnostic", ["SA32-0982-70"], heading="IRX")
e("DIA-29", "A user cannot enter TSO commands after logon: what does the TSO/E documentation say about command availability and IKJTSOxx?", "diagnostic", ["SA32-0975-70", INIT_REF])
e("DIA-30", "Print output is stuck in the JES spool: what do the SDSF and JES2 books document for holding and releasing output?", "diagnostic", ["SA23-2274-70", "SA32-0991-70"])
e("DIA-31", "A data space hit a storage constraint: what does the Extended Addressability book document for diagnosis?", "diagnostic", ["SA23-1394-70"])
e("DIA-32", "ICKDSF reported an error during volume initialization: where are the ICK message explanations and recovery?", "diagnostic", ["GC35-0033-70"])

# --- new: comparative / tuning --------------------------------------------------

e("CMP-07", "Compare documented DFSMShsm migration level 1 versus level 2: when is each used?", "comparative", ["SC23-6871-70"])
e("CMP-08", "Compare IDCAMS DEFINE versus ALTER for changing SMS-managed data set attributes, per the AMS command book.", "comparative", ["SC23-6846-70"])
e("CMP-09", "Compare IEASYSxx versus the SET command for changing system parameters: which is documented as IPL-only?", "comparative", [INIT_REF, "SA38-0666-70"])
e("CMP-10", "Compare RACF PERMIT versus CONNECT: which applies to data set profiles and which to user-group membership?", "comparative", ["SA23-2292-70"])
e("CMP-11", "Compare WLM service classes versus report classes when tuning batch goals.", "comparative", ["SC34-2662-70"])
e("CMP-12", "Compare SMFPRMxx SYS versus SUBSYS recording options for reducing SMF volume.", "comparative", ["SA38-0667-70", INIT_REF])
e("CMP-13", "Compare SLIP TRACE versus GTF for capturing diagnostic data: what does the documentation recommend for each?", "comparative", [INIT_REF, "SA23-1378-70"])
e("CMP-14", "Compare binder REPLACE versus ALIAS options in program management.", "comparative", ["SA23-1393-70"])
e("CMP-15", "Compare TSO/E TRANSMIT and RECEIVE for data set exchange per the command reference.", "comparative", ["SA32-0975-70"])
e("CMP-16", "Compare ICKDSF INIT versus REVAL for DASD volume preparation.", "comparative", ["GC35-0033-70"])
e("CMP-17", "Compare MPFLSTxx versus MSGFLDxx settings for controlling message flooding at the console.", "comparative", [INIT_REF])

# --- new: version-sensitive ------------------------------------------------------

e("VER-07", "BatchPipes BatchPipeWorks pipelining syntax: quote the documented syntax and label which operating system edition the book covers.", "version", ["SA22-7456-01"],
     note="OS/390-era book (SA22-7456-01) is the only BatchPipes edition in the corpus; answer tier must label the edition")
e("VER-08", "Is BatchPipes documented for current z/OS releases or only for OS/390, per the excerpts?", "version", ["SA22-7456-01"],
     note="correct retrieval lands on the OS/390-era book; the version statement must come from the excerpts")
e("VER-09", "Encryption Facility for z/OS OpenPGP usage: which book covers it and what version does its title carry?", "version", ["SA23-2230-60"], must_not=["SA23-2229-60"],
     note="sibling-book trap: SA23-2229-60 is the planning book of the same product, not the OpenPGP user guide")
e("VER-10", "Encryption Facility planning and customizing: which book covers key configuration and what edition suffix does it carry?", "version", ["SA23-2229-60"], must_not=["SA23-2230-60"],
     note="sibling-book trap, inverse of VER-09")
e("VER-11", "SCRT sub-capacity reporting: which tool version does the documentation cover and what SMF data does it use?", "version", ["SC23-6845-22"],
     note="SCRT 30.1.0 documentation; version is part of the title")
e("VER-12", "JES2 initialization statements: state which z/OS edition the excerpts cover before quoting IAZ/HASJ syntax.", "version", ["SA32-0992-70", "SA32-0992-02"],
     note="single-edition corpus: the answer must name the covered edition rather than merging releases")
e("VER-13", "Infoprint Server Print Transforms: which product version do the excerpts document and how is it labeled?", "version", ["aokfa00_v2r3"],
     note="v2r3-era book; the answer must label the version from the excerpts")

# --- new: negative / trap ---------------------------------------------------------

e("NEG-08", "What does message IEC072I report after a VSAM open failure?", "negative", [],
     must_not_msgs=["IEC070I"],
     note="sibling near-miss: IEC072I does not exist in the corpus while IEC070I does; the wrong sibling must never supply the answer",
     trap_type="sibling_near_miss")
e("NEG-09", "Look up document SA23-9999-99 and summarize the manual.", "negative", [],
     note="invented doc number; no such publication exists in the corpus",
     trap_type="invented_identifier")

# --- re-bind 2026-09: operator books loaded --------------------------------------
# CICS TS 3.1, WebSphere MQ 7.1 (programming guide only), IMS 11, z/OS 2.2 kit
# (incl. JES2/JES3), DB2 10, CA Top Secret 16, PDSMAN 7.7. Bindings verified
# against the live payload after ingest; entry ids continue each class series
# (the 170-entry corpus ran through MSG-36/DIA-32/SYN-36/CMP-17/VER-13/
# DOC-16/NEG-09). DFH*/CSQ*/HASP*/IAT* shapes are outside MSG_RE, so these
# message-id entries carry authored bindings (the auto-assert only covers
# parseable ids).

e("MSG-40", "DSN9022I came back for a DISPLAY DATABASE command. What does the message report?", "message_id",
     ["SC19-2972-13", "SC19-2968-15"])
e("MSG-37", "DFS0535I was issued during IMS startup. What does the IMS messages book say it reports?", "message_id", ["GC19-4233-01"])
e("MSG-38", "TSS9134A appeared after a CA Top Secret signon. What does the message document?", "message_id",
     ["tss-messages", "messages-for-ca-top-secret-for-z-os"])
e("MSG-39", "What does DFHAC2006 indicate for a CICS TS 3.1 transaction?", "message_id", ["GC34-6442-07"])

e("DIA-33", "A CICS TS 3.1 transaction abended and DFHAC2006 is in the message log. What does the message tell the operator to collect before calling support?", "diagnostic",
     ["GC34-6442-07"], page=66)
e("DIA-34", "DSNT500I came back from a DB2 10 BIND with a resource-unavailable reason code. What structure does the message document for decoding the reason?", "diagnostic",
     ["GC19-2971-12"])
e("DIA-35", "JES3 printed IAT8707 on the global during cold start. What condition is documented and what recovery is specified?", "diagnostic",
     ["SA32-1007-02"])

e("SYN-37", "Construct a JES2 SPOOLDEF initialization statement that defines a spool volume for a JES2 2.2 complex.", "syntax", ["SA32-0992-02"])
e("SYN-38", "Draft CEDA RDO definitions for a CICS TS 3.1 transaction and the program it runs.", "syntax", ["SC34-6430-09"])
e("SYN-39", "Show the documented DB2 10 BIND PACKAGE subcommand syntax, including the required keywords.", "syntax", ["SC19-2972-13"])
e("SYN-40", "Which PDSMAN control statement activates journaling of PDS directory updates, and how is it coded?", "syntax", ["ca-pdsman-pds-library-management-7-7"])

e("CMP-18", "Compare the documented JES2 and JES3 operator commands for draining spool volumes.", "comparative", ["SA32-0990-02", "SA32-1008-01"])

e("VER-14", "State which z/OS release the HASP050 excerpts document before quoting JES2 spool guidance.", "version",
     ["SA32-0989-03", "SA32-0989-70"],
     note="both the 2.2 and 2.5-era JES2 Messages editions are honest gold; the answer must name the covered edition")
e("VER-15", "Which IMS release do the abend-code excerpts document?", "version", ["GC19-4234-01"])

e("DOC-17", "What does SA32-0989-03 cover and which JES2 release does it document?", "doc_number", ["SA32-0989-03"])
e("DOC-18", "Summarize GC34-6442-07 and state the CICS release it covers.", "doc_number", ["GC34-6442-07"])

e("NEG-10", "What does message HASP310I report after a JES2 checkpoint reconfiguration?", "negative", [],
     note="sibling near-miss: HASP310I does not exist in the corpus while checkpoint sibling HASP309 does (unparseable by MSG_RE, so it cannot gate must_not); the wrong sibling must never supply the answer",
     trap_type="sibling_near_miss")

# ---------------------------------------------------------------- seed absorption
# Binding corrections discovered from the live payload (doc-number assumptions
# in the seed that did not match the actual books are corrected and the
# original assumption is preserved in the note). Always applied.
DOC_CORRECTIONS = {
    "DOC-02": "seed note assumed 'MVS System Commands'; the corpus book SA23-1383-70 is z/OS MVS IPCS Customization - expectation bound to the real book",
    "DOC-03": "seed note assumed 'JES2 Initialization and Tuning Reference'; SC23-6858-70 is z/OS DFSMS Using Magnetic Tapes - expectation bound to the real book",
    "DOC-04": "seed asked JES3 init/tuning vs commands; SC23-6862-70 is z/OS DFSMSdfp Checkpoint/Restart - neither; the number resolves to the real book",
}

# Applied only when the entry actually ends up abstain because its domain is
# absent from the corpus at build time. After the 2026-09 book load most seed
# entries bound again; these four remain honestly abstain (IEA500I was the
# synthetic fixture's invention; only the MQ 7.1 programming guide was loaded,
# so the MQ messages/log-manager books and their IDs are still absent).
ABSENT_CORRECTIONS = {
    "MSG-01": "IEA500I is not present in the real corpus (only ever the synthetic fixture, now removed); correct outcome is abstention",
    "MSG-07": "only the MQ 7.1 programming guide is loaded (no CSQ* messages/log books); CSQJ001I cannot be answered; correct outcome is abstention",
    "DIA-08": "only the MQ 7.1 programming guide is loaded (no CSQ* messages/log books); CSQW100I cannot be answered; correct outcome is abstention",
    "VER-04": "only the MQ 7.1 programming guide is loaded; MQ log/system-parameter defaults cannot be compared against another edition; correct outcome is abstention",
}


def map_seed(entry: dict) -> dict:
    cls = SEED_CLASS_MAP[entry["class"]]
    behavior = SEED_BEHAVIOR_MAP[entry["expected_behavior"]]
    note_parts = [entry.get("notes") or ""]
    if entry.get("trap_type"):
        note_parts.append(f"trap_type={entry['trap_type']}")
    if entry["id"] in DOC_CORRECTIONS:
        note_parts.append(DOC_CORRECTIONS[entry["id"]])
    out = {
        "id": entry["id"],
        "query": entry["query"],
        "query_class": cls,
        "expected_behavior": behavior,
        # candidate_doc_ids from the seed carry unsuffixed numbers that do not
        # match corpus doc_ids; binding happens from the query text in bind_seed
        "expected_doc_ids": [],
        "source": "operator-history",
        "note": "; ".join(p for p in note_parts if p),
    }
    # answer-tier gold carried through for scripts/eval_answers.py (superset schema)
    for k in ("gold_must_contain", "gold_must_not_contain", "must_cite_identifier", "domain", "trap_type"):
        if entry.get(k) not in (None, []):
            out[k] = entry[k]
    return out


def bind_seed(entry: dict, msg_docs: dict, titles: dict) -> dict:
    """Bind expected_doc_ids for seed entries that expect an answer, from the
    identifiers parsed out of the query."""
    from mainframe_rag.regexes import find_docnos, find_message_ids
    if entry["expected_behavior"] == "abstain" or entry["expected_doc_ids"]:
        return entry
    msgs = find_message_ids(entry["query"])
    docnos = find_docnos(entry["query"])
    bound: list[str] = []
    for m in msgs:
        docs = [d for d in msg_docs.get(m, {}) if d]
        canon = [d for d in docs if "System Messages" in titles.get(d, "")]
        bound += canon[:1] or docs[:1]
    for d in docnos:
        matches = [doc for doc in titles if doc.startswith(d)]
        bound += matches[:1]
    seen: list[str] = []
    for d in bound:
        if d not in seen:
            seen.append(d)
    entry["expected_doc_ids"] = seen
    return entry


SEED_OVERRIDES = {
    # manual canonical bindings where automatic binding is ambiguous or the
    # cross-referencing books would outrank the owner
    "MSG-02": ["SA38-0675-70"],          # IEF450I -> Vol 8 (IEF-IGD)
    "MSG-03": ["SA38-0673-70", "SC23-6844-70"],  # IEA794I -> Vol 6 (GOS-IEA) + Problem Management
    "MSG-04": ["SA38-0676-70", "SA23-1373-70", "SA23-2284-70"],  # IRA200E
    "MSG-05": ["SA23-2291-70"],          # ICH408I -> RACF Messages and Codes
    "DIA-01": ["SA23-1379-70", "SA38-0676-70"],  # IRA100E CSA/SQA -> Init&Tuning Guide + Vol 9
    "DIA-03": ["SA38-0674-70", "SC23-6854-70"],  # IOS000I
    "DIA-05": ["SA38-0674-70", "SC23-6885-70"],  # IEC070I
    "DIA-06": ["SA38-0673-70", "SC23-6844-70", "SA23-1374-70"],  # IEA611I
    "CMP-01": ["SA32-0992-70", "SA32-0991-70"],  # JES2 side answerable; no JES3 book (note covers)
    "CMP-02": ["SA38-0667-70", INIT_REF],        # SMFPRMxx
    "CMP-03": [INIT_REF, "SA23-1379-70"],        # CSA/ECSA/SQA
    "CMP-05": ["SC34-2662-70", "SC34-2663-70", "SC34-2662-05"],  # WLM (2.5 + 2.2 editions)
    "CMP-06": ["SA23-2287-70", "SA23-2288-70"],  # RACF SETROPTS
    "VER-01": [INIT_REF, "SA23-1380-09"],         # LFAREA: I&T Ref 2.5 + 2.2 editions
    "VER-05": ["SA23-2287-70"],                   # RACF version rules: single edition
    "VER-06": [INIT_REF, "SA23-1380-09", "GA32-0884-70"],  # BPXPRMxx (both I&T editions)
    "SYN-01": [INIT_REF],                         # LFAREA -> IEASYSxx chapter
    "SYN-02": [INIT_REF, "SA23-1390-70"],         # COMMNDxx
    "SYN-03": ["SA23-1385-70"],                   # JCL
    "SYN-04": [OPS_DOC],                          # OPS/MVS AOF )MSG
    "SYN-05": [INIT_REF],                         # PROGxx APF
    "SYN-08": ["SA23-2292-70"],                   # RACF PERMIT
    # --- re-bind 2026-09: operator books loaded; bindings verified against the
    # live payload (CICS TS 3.1, MQ 7.1 programming, IMS 11, z/OS 2.2 kit incl.
    # JES2/JES3). DFH*/CSQ*/HASP* shapes are outside MSG_RE, so these need
    # manual bindings even though the books are present.
    "MSG-06": ["GC34-6442-07"],                    # DFHAP0001 -> CICS Messages&Codes V3R1
    "MSG-08": ["GC19-4233-01", "GC19-4234-01"],    # DFS554A -> IMS Messages&Codes vols
    "DIA-02": ["SA32-0989-03"],                    # HASP050 -> z/OS 2.2 JES2 Messages
    "DIA-04": ["GC34-6442-07", "SC34-6428-08", "SC34-6441-05"],  # MXT/CEMT diagnostics -> msgs+SysDef+CEMT
    "SYN-06": ["SC34-6428-08"],                    # CICS SIT overrides -> Sys Def Guide
    "SYN-07": ["wmq71.pdf"],                       # CSQ6SYSP checkpoint -> MQ 7.1 Prog
    "SYN-09": ["SA32-1005-01"],                    # JES3 inish fragment -> JES3 I&T Ref
    "CMP-04": ["SC34-6428-08", "SC34-6430-09", "GC34-6442-07"],  # MXT vs EDSALIMIT
    "VER-02": ["SA32-1004-00"],                    # JES3 support/migration -> Introduction
    "VER-03": ["SC34-6428-08", "SC34-6430-09"],    # SIT DSA params per CICS release
    # DFS629I is carried by several books (incl. incidental mentions); pin the
    # IMS 11 messages volume explicitly instead of relying on bind_seed order.
    "DIA-07": ["GC19-4234-01"],
}

# MSG-01 abstains regardless of what the corpus carries: IEA500I was the
# synthetic fixture's invented ID; if a real book ever carries it, flip
# deliberately after human review (the build prints a loud warning when that
# happens). DIA-07 left this set at the 2026-09 re-bind: DFS629I became a real
# IMS 11 messages entry and auto-binds from the payload.
FORCE_ABSTAIN = {"MSG-01"}

# Seed entries whose domain books are not loaded yet abstain while their
# identifiers cannot be bound. After the 2026-09 load the unbound remainder is
# only the MQ-adjacent trio (MSG-07/DIA-08/VER-04: no MQ messages books) — they
# have no parseable id and stay abstain via ABSENT_CORRECTIONS notes. Any
# answer entry that lands here with an id NOT covered by a manual override or
# note fails the build loudly.
ABSENT_DOMAIN_FALLBACK = {
    "MSG-06", "MSG-07", "MSG-08",
    "SYN-06", "SYN-07", "SYN-09",
    "DIA-02", "DIA-04", "DIA-08",
    "CMP-04",
    "VER-02", "VER-03", "VER-04",
}

# trap IDs that must be ABSENT from the corpus for the trap to be valid
ABSENT_TRAP_IDS = {"NEG-01": "IEA9999Z", "NEG-08": "IEC072I", "NEG-10": "HASP310I"}
ABSENT_TRAP_DOCS = {"NEG-09": "SA23-9999-99"}

# seed entries dropped from the corpus (duplicates of the migrated legacy set;
# the seed header itself flagged DOC-06 as intentionally overlapping)
DROP_SEED_IDS = {"DOC-06"}

# ---------------------------------------------------------------- build
def main() -> int:
    from qdrant_client import QdrantClient

    from mainframe_rag.config import load_settings
    from mainframe_rag.regexes import find_message_ids

    settings = load_settings()
    client = QdrantClient(url=settings.qdrant_url, timeout=30)

    print("[*] building message-id -> docs map ...", file=sys.stderr)
    msg_docs: dict[str, Counter] = defaultdict(Counter)
    titles: dict[str, str] = {}
    offset = None
    while True:
        pts, offset = client.scroll(
            settings.qdrant_collection, limit=1000, offset=offset,
            with_payload=["doc_id", "title", "message_ids"], with_vectors=False,
        )
        for p in pts:
            pl = p.payload or {}
            d = str(pl.get("doc_id") or "")
            if d and d not in titles:
                titles[d] = str(pl.get("title") or "")
            for m in pl.get("message_ids") or []:
                msg_docs[str(m)][d] += 1
        if offset is None:
            break
    print(f"[*] {len(titles)} docs, {len(msg_docs)} distinct message ids", file=sys.stderr)

    # seed entries
    seed_entries: list[dict] = []
    seed_errors: list[str] = []
    for line in SEED.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        if raw["id"] in DROP_SEED_IDS:
            continue
        mapped = map_seed(raw)
        if mapped["id"] in SEED_OVERRIDES:
            mapped["expected_doc_ids"] = SEED_OVERRIDES[mapped["id"]]
        else:
            mapped = bind_seed(mapped, msg_docs, titles)
        if mapped["id"] in FORCE_ABSTAIN:
            if mapped["expected_doc_ids"]:
                print(
                    f"warn: {mapped['id']} is forced to abstain but its identifiers now bind to "
                    f"{mapped['expected_doc_ids']}; review a deliberate flip",
                    file=sys.stderr,
                )
            mapped["query_class"] = "negative"
            mapped["expected_behavior"] = "abstain"
            mapped["expected_doc_ids"] = []
            if ABSENT_CORRECTIONS.get(mapped["id"]):
                mapped["note"] = f"{mapped['note']}; {ABSENT_CORRECTIONS[mapped['id']]}"
        elif mapped["expected_behavior"] == "answer" and not mapped["expected_doc_ids"]:
            if mapped["id"] in ABSENT_DOMAIN_FALLBACK:
                # domain books not loaded yet: honest abstain until they bind
                mapped["query_class"] = "negative"
                mapped["expected_behavior"] = "abstain"
                if ABSENT_CORRECTIONS.get(mapped["id"]):
                    mapped["note"] = f"{mapped['note']}; {ABSENT_CORRECTIONS[mapped['id']]}"
            else:
                seed_errors.append(
                    f"{mapped['id']}: answer seed entry could not be bound to any corpus doc "
                    "(add a SEED_OVERRIDES binding or reclassify it deliberately)"
                )
        seed_entries.append(mapped)

    # new entries
    new_entries: list[dict] = []
    for row in E:
        entry = dict(row)
        entry["expected_behavior"] = "abstain" if entry["query_class"] == "negative" else "answer"
        entry["source"] = "payload-draft"
        new_entries.append(entry)

    entries = seed_entries + new_entries
    for entry in entries:
        entry.setdefault("must_not_retrieve", [])
        entry.setdefault("must_not_message_ids", [])
        entry.setdefault("expected_doc_ids", [])

    # ---------------------------------------------------------- assertions
    errors: list[str] = list(seed_errors)
    seen_queries: set[str] = set()
    seen_ids: set[str] = set()
    for entry in entries:
        q = entry["query"].strip().lower()
        if q in seen_queries:
            errors.append(f"duplicate query: {entry['id']} {entry['query'][:60]}")
        seen_queries.add(q)
        if entry["id"] in seen_ids:
            errors.append(f"duplicate entry id: {entry['id']} (class series already used it)")
        seen_ids.add(entry["id"])
        for d in entry["expected_doc_ids"]:
            if d not in titles:
                errors.append(f"{entry['id']}: expected doc {d} not in corpus")
        for d in entry["must_not_retrieve"]:
            if d not in titles:
                errors.append(f"{entry['id']}: must_not doc {d} not in corpus")
            if d in entry["expected_doc_ids"]:
                errors.append(f"{entry['id']}: must_not doc {d} is also expected")
        for m in entry["must_not_message_ids"]:
            if m not in msg_docs:
                errors.append(f"{entry['id']}: must_not message id {m} absent from corpus")
                continue
            # A must_not ID inside an expected doc is only broken when the doc
            # does not also carry the query's own message ID: same-volume
            # sibling precision assertions (IOS207I vs IOS208I) are allowed.
            query_ids = set(find_message_ids(entry["query"]))
            for d in entry["expected_doc_ids"]:
                if m in msg_docs[d] and not (query_ids & set(msg_docs[d])):
                    errors.append(
                        f"{entry['id']}: must_not id {m} present inside expected doc {d} "
                        "which does not carry the query's own message id; trap is broken"
                    )
        if entry["query_class"] == "message_id" and entry["expected_behavior"] == "answer":
            for m in find_message_ids(entry["query"]):
                if m not in msg_docs:
                    errors.append(f"{entry['id']}: message id {m} absent from corpus")
                    continue
                missing = [d for d in entry["expected_doc_ids"] if d not in msg_docs[m]]
                if missing:
                    errors.append(f"{entry['id']}: message id {m} not carried by expected doc(s) {missing}")
        if entry["query_class"] == "negative" and entry["expected_behavior"] == "answer":
            errors.append(f"{entry['id']}: negative class must be abstain")
        if entry["expected_behavior"] == "abstain" and entry["expected_doc_ids"]:
            errors.append(f"{entry['id']}: abstain entry carries expected_doc_ids")
        if entry["expected_behavior"] == "answer" and not entry["expected_doc_ids"]:
            errors.append(f"{entry['id']}: answer entry has no expected_doc_ids")
    # trap candidates that must be ABSENT for the trap to be valid
    for entry in entries:
        trap_id = ABSENT_TRAP_IDS.get(entry["id"])
        if trap_id and trap_id in msg_docs:
            errors.append(f"{entry['id']}: trap id {trap_id} unexpectedly EXISTS in corpus")
        trap_doc = ABSENT_TRAP_DOCS.get(entry["id"])
        if trap_doc and trap_doc in titles:
            errors.append(f"{entry['id']}: trap doc {trap_doc} unexpectedly EXISTS in corpus")

    if errors:
        print(f"BUILD FAILED with {len(errors)} assertion errors:", file=sys.stderr)
        for err in errors:
            print(f"  ERROR {err}", file=sys.stderr)
        return 1

    # ---------------------------------------------------------- split (~60/40)
    by_class: dict[str, list[dict]] = defaultdict(list)
    for entry in sorted(entries, key=lambda x: x["id"]):
        by_class[entry["query_class"]].append(entry)
    dev: list[dict] = []
    holdout: list[dict] = []
    for cls, rows in sorted(by_class.items()):
        # legacy entries (LEG-*) always dev; every 5th/4th position of the rest holdout
        legacy = [r for r in rows if r["id"].startswith("LEG-")]
        rest = [r for r in rows if not r["id"].startswith("LEG-")]
        for i, r in enumerate(rest):
            (holdout if (i + 1) % 5 in (3, 4) else dev).append(r)
        dev.extend(legacy)

    def write(path: Path, rows: list[dict]) -> None:
        rows = sorted(rows, key=lambda x: (x["query_class"], x["id"]))
        lines = [json.dumps(r, ensure_ascii=False) for r in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    write(REPO / "evals" / "golden.jsonl", dev)
    write(REPO / "evals" / "holdout.jsonl", holdout)
    digest = hashlib.sha256((REPO / "evals" / "holdout.jsonl").read_bytes()).hexdigest()
    (REPO / "evals" / "holdout.jsonl.sha256").write_text(f"{digest}  evals/holdout.jsonl\n")

    tally = Counter(e["query_class"] for e in dev)
    tally_h = Counter(e["query_class"] for e in holdout)
    print(f"[*] dev={len(dev)} holdout={len(holdout)} total={len(dev) + len(holdout)}", file=sys.stderr)
    for cls in sorted(set(tally) | set(tally_h)):
        print(f"    {cls:12s} dev={tally[cls]:3d} holdout={tally_h[cls]:3d}", file=sys.stderr)
    print(f"[*] holdout sha256: {digest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
