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
from mainframe_rag.retrieve.filters import parse_query, query_kind
from tests.fakes import iter_golden_queries

CLASSIC = re.compile(r"\b([A-Z]{3}\d{2,5}[A-Z])\b")


def test_message_id_families() -> None:
    # Classic shape.
    assert find_message_ids("What does IEA500I mean?") == ["IEA500I"]
    assert find_message_ids("CAS2180I and CAS2181I") == ["CAS2180I", "CAS2181I"]
    # CICS DFH cards without severity; x-placeholder family bookmarks are not codes.
    assert find_message_ids("DFHAC2006 transaction abend") == ["DFHAC2006"]
    assert find_message_ids("reply to DFH0690") == ["DFH0690"]
    assert find_message_ids("DFHACxxxx messages") == []
    # IMS DFS bare forms; module names (letters after DFS) are not codes.
    assert find_message_ids("DFS058 alongside DFS058I") == ["DFS058", "DFS058I"]
    assert find_message_ids("DFS554 and DFS555A") == ["DFS554", "DFS555A"]
    assert find_message_ids("DFSCLMR0 called the generator") == []
    # 4-letter prefixes.
    assert find_message_ids("What does DSNA670I mean?") == ["DSNA670I"]
    assert find_message_ids("TSSC001E security violation") == ["TSSC001E"]
    assert find_message_ids("HASP310I checkpoint") == ["HASP310I"]
    # Documented limitation: 5-letter prefixes stay out until measured.
    assert find_message_ids("ABCDE1234F happened") == []


def test_query_kind_flips_only_for_real_identifiers() -> None:
    assert parse_query("What does DFHAC2006 indicate?").has_identifiers
    assert parse_query("What does DSNT500I mean?").has_identifiers
    assert parse_query("How do I issue DISPLAY THREAD with LUWID options?").has_identifiers is False
    assert parse_query("What does abend S0C4 mean?").has_identifiers
    assert parse_query("abend 0C4").has_identifiers
    assert parse_query("wait state 064").has_identifiers
    assert parse_query("The value is 0C4 in hex").has_identifiers is False


def test_golden_sweep_flips_are_real_codes() -> None:
    """Every golden/holdout/paraphrase query: the only queries gaining
    message_ids vs the classic shape are the reviewed real codes below. Any
    other flip is a precision regression."""
    expected = {
        "A CICS TS 3.1 transaction abended and DFHAC2006 is in the message log. What does the message te": ["DFHAC2006"],
        "DFHAP0001 was issued on CICSP1. What does the message indicate and where are the dump/take-acti": ["DFHAP0001"],
        "What does DFHAC2006 indicate for a CICS TS 3.1 transaction?": ["DFHAC2006"],
        "Queue manager CSQ1 is in restart-recovery after CSQW100I-class log messages. What documented re": ["CSQW100I"],
        "DSNT500I came back from a DB2 10 BIND with a resource-unavailable reason code. What structure d": ["DSNT500I"],
        "Look up CSQJ001I for IBM MQ for z/OS. What startup or log-manager condition does it report?": ["CSQJ001I"],
        "What does message HASP310I report after a JES2 checkpoint reconfiguration?": ["HASP310I"],
    }
    for name, query in iter_golden_queries():
        old = sorted(set(CLASSIC.findall(query)))
        new = find_message_ids(query)
        if new != old:
            assert expected.get(query[:95]) == new, f"{name}: {query[:95]} -> {new}"


# Issue #591: system/user/wait-state completion codes


def test_syscode_self_contexting_forms_are_normalized() -> None:
    # S-prefix and X'..' stripped to canonical hex; case-insensitive.
    assert find_system_codes("What does abend S0C4 mean?") == ["0C4"]
    assert find_system_codes("s0c4") == ["0C4"]
    assert find_system_codes("X'0c4'") == ["0C4"]
    assert find_system_codes("X'0C4AB'") == []  # 5-hex-digit literal is not a 3-hex code
    # U + 4 digits is unambiguous on its own (review #603); wait state gets a W prefix.
    assert find_system_codes("U4038") == ["U4038"]
    assert find_system_codes("user completion code U4038") == ["U4038"]
    assert find_system_codes("wait state 064") == ["W064"]
    assert find_system_codes("W064") == []
    # Common letter-prefixed abends are recognised.
    assert find_system_codes("What causes abend SB37?") == ["B37"]
    assert find_system_codes("abend SD37") == ["D37"]
    assert find_system_codes("abend S80A") == ["80A"]


def test_bare_code_needs_an_adjacent_code_phrase() -> None:
    """A code phrase elsewhere in the sentence does not license every hex
    token in it (review #603); the window is one code-ish connector."""
    assert find_system_codes("abend 0C4") == ["0C4"]
    assert find_system_codes("system code 0C4") == ["0C4"]
    assert find_system_codes("abend 806") == ["806"]
    assert find_system_codes("completion code 222") == ["222"]
    assert find_system_codes("abend code 0C4") == ["0C4"]
    assert find_system_codes("abend error 0C4") == ["0C4"]
    for query in (
        "0C4",
        "The value is 0C4 in hex",
        "The job abended; read 100 records from the ADD file",
        "abend in the FEE calculation step",
        "abend after 300 seconds",
        "abend for 100 records",
        "abend with a BAD return?",
        "reason code 004 from DYNALLOC",  # reason codes are a different family
    ):
        assert find_system_codes(query) == [], query


def test_codes_are_not_confused_with_words_models_or_doc_numbers() -> None:
    # No English word carries a digit; without the rule S+3hex swallows "safe"/"seed".
    for query in (
        "Is it safe to delete the dataset?",
        "Is it SAFE to IPL now?",
        "How do I seed the random generator in REXX?",
        "Explain S390 channel subsystem architecture",
        "How do S370 and S390 addressing differ?",
    ):
        assert find_system_codes(query) == [], query
        assert parse_query(query).has_identifiers is False
    # Form numbers share the S-family shape; the exclusion is load-bearing.
    for query in ("Identify SC23-6862", "SA23-1380", "What manual is SC23-6846-02", "GC35-0033-41"):
        assert find_system_codes(query) == [], query
        assert parse_query(query).has_identifiers  # doc_ids, not codes


def test_all_letter_codes_need_uppercase_and_adjacency() -> None:
    """AFB/BFB/CFB/DFB/EFB are real completion codes with no digit. They are
    reachable only in uppercase right after the code phrase, so the digit
    rule keeps excluding "safe"/"seed" and lowercase prose (review #603)."""
    assert find_system_codes("abend AFB") == ["AFB"]
    assert find_system_codes("abend SAFB") == ["AFB"]
    assert find_system_codes("What does system completion code EFB mean?") == ["EFB"]
    assert query_kind(parse_query("abend SCFB")) == "identifier"
    for query in (
        "Is it SAFE to IPL now?",
        "my job abended bad record",
        "abend afb",  # lowercase: the uppercase rule is the precision guard
        "AFB abend",  # before the phrase, not after it
        "abend after FEE",  # non-code connector
        "reason code AFB",
    ):
        assert find_system_codes(query) == [], query
