"""Code-entry detection tests (issue #591).

Tests for the ingest-side code-entry splitting: a line that is exactly
a 3-hex code, followed within two lines by description text rather than
another bare code. This excludes index runs.
"""

from mainframe_rag.ingest.chunk import _code_entries, _is_code_entry_start


def test_single_code_entry() -> None:
    text = "0C4\nData exception\n\nThe system detected a data exception."
    result = _code_entries(text)
    assert result is not None
    assert len(result) == 1
    assert result[0][1] is True  # atomic
    assert result[0][0].startswith("0C4")


def test_multiple_code_entries() -> None:
    text = "0C4\nData exception\n\n0C5\nProtection exception\n\n0C6\nAddressing exception"
    result = _code_entries(text)
    assert result is not None
    assert len(result) == 3
    assert all(item[1] for item in result)  # all atomic
    assert result[0][0].startswith("0C4")
    assert result[1][0].startswith("0C5")
    assert result[2][0].startswith("0C6")


def test_index_run_excluded() -> None:
    text = "0C4\n0C5\n0C6\n0C7"
    result = _code_entries(text)
    assert result is None


def test_index_run_with_description_after() -> None:
    text = "0C4\n0C5\n0C6\nData exception"
    result = _code_entries(text)
    assert result is not None
    assert len(result) == 2
    assert result[0][1] is False
    assert result[1][1] is True
    assert result[1][0].startswith("0C6")


def test_code_with_blank_line_before_description() -> None:
    text = "0C4\n\nData exception"
    result = _code_entries(text)
    assert result is not None
    assert len(result) == 1
    assert result[0][1] is True
    assert result[0][0].startswith("0C4")


def test_code_with_description_on_second_line() -> None:
    text = "0C4\n\nData exception"
    lines = text.splitlines()
    assert _is_code_entry_start(lines, 0) is True


def test_code_with_description_on_third_line() -> None:
    text = "0C4\n\n\nData exception"
    lines = text.splitlines()
    assert _is_code_entry_start(lines, 0) is False


def test_prose_before_code_entries() -> None:
    text = "The following codes are documented:\n0C4\nData exception\n\n0C5\nProtection exception"
    result = _code_entries(text)
    assert result is not None
    assert len(result) == 3
    assert result[0][1] is False  # prose prefix
    assert result[1][1] is True  # code entry
    assert result[2][1] is True  # code entry


def test_no_code_entries() -> None:
    text = "This is a regular paragraph with no codes."
    result = _code_entries(text)
    assert result is None


def test_code_like_line_not_entry() -> None:
    text = "The value is 0C4 in hex."
    result = _code_entries(text)
    assert result is None


def test_mixed_prose_and_codes() -> None:
    text = "System completion codes:\n\n0C4\nData exception\n\n0C5\nProtection exception\n\nSee also the system codes manual."
    result = _code_entries(text)
    assert result is not None
    assert len(result) == 3
    assert result[0][1] is False  # prose prefix
    assert result[1][1] is True  # 0C4
    assert result[2][1] is True  # 0C5 (includes trailing prose)