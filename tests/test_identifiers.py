"""Identifier-shape tests (issue #120).

The classic 3-letter form (IEA500I) missed whole vendor families on real
corpora: CICS DFH cards without trailing severity (DFHAC2006), bare IMS
DFS codes (DFS058), and 4-letter-prefix codes (DSNA670I, TSSC001E).
One shared regex serves ingest payloads and query parsing, so these pins
hold on both sides by construction.
"""

import json
import re
from pathlib import Path

from mainframe_rag.regexes import find_message_ids
from mainframe_rag.retrieve.filters import parse_query

CLASSIC = re.compile(r"\b([A-Z]{3}\d{2,5}[A-Z])\b")


def test_classic_shape_unchanged() -> None:
    assert find_message_ids("What does IEA500I mean?") == ["IEA500I"]
    assert find_message_ids("CAS2180I and CAS2181I") == ["CAS2180I", "CAS2181I"]


def test_cics_dfh_family() -> None:
    assert find_message_ids("DFHAC2006 transaction abend") == ["DFHAC2006"]
    assert find_message_ids("DFHSI1579 issued") == ["DFHSI1579"]
    assert find_message_ids("DFHME0116 dump") == ["DFHME0116"]
    assert find_message_ids("reply to DFH0690") == ["DFH0690"]
    assert find_message_ids("DFHAP0001 was issued") == ["DFHAP0001"]
    # Family bookmarks with x-placeholders are not codes.
    assert find_message_ids("DFHACxxxx messages") == []


def test_ims_dfs_bare_forms() -> None:
    assert find_message_ids("DFS058 alongside DFS058I") == ["DFS058", "DFS058I"]
    assert find_message_ids("DFS554 and DFS555A") == ["DFS554", "DFS555A"]
    # Module names (letters after DFS) are not codes.
    assert find_message_ids("DFSCLMR0 called the generator") == []


def test_four_letter_prefix_codes() -> None:
    assert find_message_ids("What does DSNA670I mean?") == ["DSNA670I"]
    assert find_message_ids("TSSC001E security violation") == ["TSSC001E"]
    assert find_message_ids("BPXI040I fork failure") == ["BPXI040I"]
    assert find_message_ids("CSQJ001I startup") == ["CSQJ001I"]
    assert find_message_ids("HASP310I checkpoint") == ["HASP310I"]


def test_five_letter_prefixes_still_missed_by_design() -> None:
    # Documented limitation: 5-letter prefixes stay out until measured.
    assert find_message_ids("ABCDE1234F happened") == []


def test_query_kind_flips_only_for_real_codes() -> None:
    assert parse_query("What does DFHAC2006 indicate?").has_identifiers
    assert parse_query("What does DSNT500I mean?").has_identifiers
    assert parse_query("How do I issue DISPLAY THREAD with LUWID options?").has_identifiers is False


def test_golden_sweep_flips_are_real_codes() -> None:
    """All 209 golden queries: the only queries gaining message_ids vs
    the classic shape are the 7 reviewed real codes below. Any other flip
    is a precision regression."""
    expected = {
        "A CICS TS 3.1 transaction abended and DFHAC2006 is in the message log. What does the message te": ["DFHAC2006"],
        "DFHAP0001 was issued on CICSP1. What does the message indicate and where are the dump/take-acti": ["DFHAP0001"],
        "What does DFHAC2006 indicate for a CICS TS 3.1 transaction?": ["DFHAC2006"],
        "Queue manager CSQ1 is in restart-recovery after CSQW100I-class log messages. What documented re": ["CSQW100I"],
        "DSNT500I came back from a DB2 10 BIND with a resource-unavailable reason code. What structure d": ["DSNT500I"],
        "Look up CSQJ001I for IBM MQ for z/OS. What startup or log-manager condition does it report?": ["CSQJ001I"],
        "What does message HASP310I report after a JES2 checkpoint reconfiguration?": ["HASP310I"],
    }
    root = Path(__file__).resolve().parent.parent
    total = 0
    for name in ("evals/golden.jsonl", "evals/paraphrase.jsonl", "evals/holdout.jsonl"):
        with open(root / name) as f:
            for line in f:
                query = json.loads(line).get("query") or ""
                total += 1
                old = sorted(set(CLASSIC.findall(query)))
                new = find_message_ids(query)
                if new != old:
                    assert expected.get(query[:95]) == new, f"{name}: {query[:95]} -> {new}"
    assert total == 209
    assert len(expected) == 7
