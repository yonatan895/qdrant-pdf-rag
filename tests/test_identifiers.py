"""Identifier-shape tests (issues #120, #591).

The classic 3-letter form (IEA500I) missed whole vendor families on real
corpora: CICS DFH cards without trailing severity (DFHAC2006), bare IMS
DFS codes (DFS058), and 4-letter-prefix codes (DSNA670I, TSSC001E).
One shared regex serves ingest payloads and query parsing, so these pins
hold on both sides by construction.

Issue #591 adds system/user/wait-state completion codes: S0C4, 0C4,
X'0C4', U4038, wait state 064 — with context gating to avoid false
identifier routing for ordinary hex-looking words in prose.
"""

import re

from mainframe_rag.regexes import find_message_ids, find_system_codes
from mainframe_rag.retrieve.filters import parse_query
from tests.fakes import iter_golden_queries

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
    """All 215 golden queries (193 golden+holdout after the #270 re-freeze,
    plus 22 paraphrase): the only queries gaining message_ids vs the classic
    shape are the 7 reviewed real codes below. Any other flip is a precision
    regression."""
    expected = {
        "A CICS TS 3.1 transaction abended and DFHAC2006 is in the message log. What does the message te": ["DFHAC2006"],
        "DFHAP0001 was issued on CICSP1. What does the message indicate and where are the dump/take-acti": ["DFHAP0001"],
        "What does DFHAC2006 indicate for a CICS TS 3.1 transaction?": ["DFHAC2006"],
        "Queue manager CSQ1 is in restart-recovery after CSQW100I-class log messages. What documented re": ["CSQW100I"],
        "DSNT500I came back from a DB2 10 BIND with a resource-unavailable reason code. What structure d": ["DSNT500I"],
        "Look up CSQJ001I for IBM MQ for z/OS. What startup or log-manager condition does it report?": ["CSQJ001I"],
        "What does message HASP310I report after a JES2 checkpoint reconfiguration?": ["HASP310I"],
    }
    total = 0
    for name, query in iter_golden_queries():
        total += 1
        old = sorted(set(CLASSIC.findall(query)))
        new = find_message_ids(query)
        if new != old:
            assert expected.get(query[:95]) == new, f"{name}: {query[:95]} -> {new}"
    assert total == 220
    assert len(expected) == 7


# Issue #591: system/user/wait-state completion codes


def test_syscode_s_prefix_self_contexting() -> None:
    assert find_system_codes("What does abend S0C4 mean?") == ["0C4"]
    assert find_system_codes("S0C4") == ["0C4"]
    assert find_system_codes("s0c4") == ["0C4"]


def test_syscode_hex_literal_self_contexting() -> None:
    assert find_system_codes("X'0C4'") == ["0C4"]
    assert find_system_codes("x'0c4'") == ["0C4"]
    # 5-hex-digit literal is not a 3-hex code
    assert find_system_codes("X'0C4AB'") == []


def test_syscode_bare_needs_context() -> None:
    assert find_system_codes("abend 0C4") == ["0C4"]
    assert find_system_codes("completion code 0C4") == ["0C4"]
    assert find_system_codes("system code 0C4") == ["0C4"]
    # No context — bare 3-hex is not an identifier
    assert find_system_codes("0C4") == []
    assert find_system_codes("The value is 0C4 in hex") == []


def test_usercode_needs_abend_context() -> None:
    assert find_system_codes("abend U4038") == ["U4038"]
    assert find_system_codes("U4038") == []


def test_waitstate_self_contexting() -> None:
    assert find_system_codes("wait state 064") == ["W064"]
    assert find_system_codes("W064") == []


def test_query_kind_flips_for_system_codes() -> None:
    assert parse_query("What does abend S0C4 mean?").has_identifiers
    assert parse_query("abend 0C4").has_identifiers
    assert parse_query("wait state 064").has_identifiers
    assert parse_query("The value is 0C4 in hex").has_identifiers is False


def test_system_codes_normalized() -> None:
    # S-prefix stripped to canonical 0C4
    assert find_system_codes("S0C4") == ["0C4"]
    # X'...' stripped to canonical 0C4
    assert find_system_codes("X'0C4'") == ["0C4"]
    # Wait state gets W prefix
    assert find_system_codes("wait state 064") == ["W064"]
    # User code keeps U prefix
    assert find_system_codes("abend U4038") == ["U4038"]
