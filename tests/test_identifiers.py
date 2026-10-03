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
    """All 225 golden queries (203 golden+holdout after the #591 re-freeze,
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
    assert total == 225
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


def test_usercode_is_self_contexting() -> None:
    # U + 4 digits is unambiguous on its own (review #603): unlike a bare
    # 3-hex token it needs no code phrase beside it.
    assert find_system_codes("U4038") == ["U4038"]
    assert find_system_codes("abend U4038") == ["U4038"]
    assert find_system_codes("user completion code U4038") == ["U4038"]


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


def test_model_numbers_are_not_codes() -> None:
    """S390/S370 share the S+3-hex shape but are never completion codes
    (review #603). A model number must not flip query_kind either."""
    assert find_system_codes("Explain S390 channel subsystem architecture") == []
    assert find_system_codes("How do S370 and S390 addressing differ?") == []
    assert parse_query("Explain S390 channel subsystem architecture").has_identifiers is False


def test_bare_code_needs_adjacency_not_just_context() -> None:
    """A code phrase somewhere in the sentence does not license every hex
    token in it (review #603): only codes adjacent to the phrase count."""
    assert find_system_codes("The job abended; read 100 records from the ADD file") == []
    assert find_system_codes("abend in the FEE calculation step") == []
    assert find_system_codes("abend with a BAD return?") == []
    # Adjacent codes still resolve, including all-digit ones.
    assert find_system_codes("abend 806") == ["806"]
    assert find_system_codes("completion code 222") == ["222"]
    assert find_system_codes("abend code 0C4") == ["0C4"]


def test_adjacency_window_is_one_code_ish_connector() -> None:
    """One arbitrary word of slack re-admitted the false positives the window
    exists to exclude (review #603): "abend after 300 seconds" is
    structurally identical to "abend code 0C4" unless the connector is
    constrained to a code-ish word."""
    assert find_system_codes("My job abended after it read 100 records from the ADD file") == []
    assert find_system_codes("abend after 300 seconds") == []
    assert find_system_codes("abend for 100 records") == []
    # The same shape with a code connector still resolves.
    assert find_system_codes("abend code 0C4") == ["0C4"]
    assert find_system_codes("abend error 0C4") == ["0C4"]


def test_s_prefixed_words_need_a_digit() -> None:
    """Every real completion code carries a digit; no English word does.
    Without the rule the S-prefix admits S + three hex letters and swallows
    ordinary operator vocabulary, flipping those queries onto identifier
    ranking (review #603)."""
    assert find_system_codes("Is it safe to delete the dataset?") == []
    assert find_system_codes("Is it SAFE to IPL now?") == []
    assert find_system_codes("How do I seed the random generator in REXX?") == []
    assert parse_query("Is it safe to delete the dataset?").has_identifiers is False
    assert parse_query("How do I seed the random generator in REXX?").has_identifiers is False
    # Codes with a digit in any position still resolve.
    assert find_system_codes("abend S80A") == ["80A"]
    assert find_system_codes("abend S806") == ["806"]
    assert find_system_codes("abend SB37") == ["B37"]


def test_reason_codes_are_a_different_family() -> None:
    """"reason code" is not a completion-code phrase: admitting it routed
    DYNALLOC's reason codes into the system-codes filter (review #603)."""
    assert find_system_codes("reason code 004 from DYNALLOC") == []
    assert find_system_codes("reason code 878") == []


def test_subsystem_abends_with_letter_prefix_are_recognised() -> None:
    """SB37/SD37/SE37 are among the most common abends; requiring a digit
    after S to spare doc numbers dropped them (review #603)."""
    assert find_system_codes("What causes abend SB37?") == ["B37"]
    assert find_system_codes("abend SD37") == ["D37"]
    assert find_system_codes("abend SE37") == ["E37"]
    assert find_system_codes("abend S80A") == ["80A"]
    assert find_system_codes("abend S0C4") == ["0C4"]


def test_doc_numbers_never_match_the_s_prefix_family() -> None:
    """The S-family is admitted by shape now, so the doc-number exclusion is
    load-bearing: a form number is letter + 2 digits + `-` + 4 digits."""
    for query in (
        "Identify SC23-6862",
        "SA23-1380",
        "What manual is SC23-6846-02",
        "SC34-2662-05",
        "GC35-0033-41",
        "SA38-0665-03",
    ):
        assert find_system_codes(query) == [], query
        assert parse_query(query).has_identifiers  # doc_ids, not codes


def test_all_letter_codes_need_uppercase_and_adjacency() -> None:
    """AFB/BFB/CFB/DFB/EFB are real completion codes with no digit (about 1%
    of a real system-codes manual). They are reachable only in uppercase
    right after the code phrase, so the digit rule keeps excluding
    "safe"/"seed" and lowercase prose (follow-up to review #603)."""
    assert find_system_codes("abend AFB") == ["AFB"]
    assert find_system_codes("abend SAFB") == ["AFB"]
    assert find_system_codes("abend code DFB") == ["DFB"]
    assert find_system_codes("What does system completion code EFB mean?") == ["EFB"]
    assert query_kind(parse_query("abend SCFB")) == "identifier"
    for query in (
        "Is it SAFE to IPL now?",
        "my job abended bad record",
        "abended add",
        "abend afb",  # lowercase: the uppercase rule is the precision guard
        "AFB abend",  # before the phrase, not after it
        "abend after FEE",  # non-code connector
        "reason code AFB",
    ):
        assert find_system_codes(query) == [], query
