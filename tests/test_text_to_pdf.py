"""Text-to-PDF converter (#1277): plain-text sources through the unchanged ingest.

Every input here is original text written for these tests (no IBM source).
The round trips run the worker's own steps (run_ingest: parse_pdf,
_extract_page_texts, strip_chrome, make_chunks) on the converter's output
and format citations with retrieve.query.format_citation.
"""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path

import pymupdf
import pytest

from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import SECTION_MAX_CHARS, make_chunks
from mainframe_rag.ingest.ibm_pdf import _extract_page_texts, parse_pdf
from mainframe_rag.retrieve.query import format_citation
from mainframe_rag.tools import text_to_pdf as t2p

TITLE = "Site export - TEST.MACLIB macro sources (not IBM documentation)"
STEM = "SITE-MACLIB-TEST-20261117"


def macro(name: str, body_lines: int = 0) -> str:
    """An original assembler macro with a unique marker comment."""
    lines = [
        "         MACRO",
        f"&LABEL   {name:<6} &AREA,&LEN=80,&OPT=NONE",
        f".*  {name} - TEST MACRO WRITTEN FOR THE CONVERTER TESTS ONLY.",
        f".*  MARKER-{name}",
        "&LABEL   LA    1,&AREA                 POINT AT THE AREA",
        "         LHI   0,&LEN                  RECORD LENGTH",
        "         AIF   ('&OPT' EQ 'NONE').DONE",
        "         MVI   0(1),C' '               CLEAR THE FIRST BYTE",
    ]
    lines += [f"         AHI   {i % 15},{i}                  STEP {i:04d} OF {name}" for i in range(body_lines)]
    lines += [".DONE    ANOP", "         MEND"]
    return "\n".join(lines) + "\n"


JOB = """//ZZJOB1   JOB (ACCT),'TEST JOB',CLASS=A,MSGCLASS=X
//*  MARKER-ZZJOB1 ORIGINAL TEST JOB FOR THE CONVERTER
//STEP1    EXEC PGM=ZZPROG1,
//             PARM='MODE=TEST'
//INPUT    DD DSN=TEST.ZZ.INPUT,DISP=SHR
//SYSIN    DD *
  OPTION ONE
  OPTION TWO
/*
//SYSPRINT DD SYSOUT=*
"""


def write_library(root: Path, members: dict[str, str]) -> Path:
    src = root / "export"
    src.mkdir()
    for name, text in members.items():
        (src / name).write_text(text, encoding="utf-8")
    return src


def standard_members() -> dict[str, str]:
    members = {f"ZZM{i:02d}": macro(f"ZZM{i:02d}") for i in range(1, 11)}
    # A form-number-shaped token in the first member's comment: the cover
    # keeps it out of ingest's first-pages doc id scan.
    members["ZZM01"] = members["ZZM01"].replace("ONLY.", "ONLY. SEE ZZ99-1234.")
    members["ZZJOB1"] = JOB
    members["ZZTINY"] = "* MARKER-ZZTINY\n         BR    14\n"
    members["ZZDATA"] = "MARKER-ZZDATA RECORD ONE\n\n00042\n"
    members["ZZLONG"] = macro("ZZLONG", body_lines=600)
    return members


def ingest(path: Path, corpus_root: Path | None = None):
    """The ingest worker's parse and chunk steps (run_ingest.py)."""
    parsed = parse_pdf(path, corpus_root=corpus_root)
    doc = pymupdf.open(path)
    try:
        page_texts, page_labels = _extract_page_texts(doc)
    finally:
        doc.close()
    return parsed, make_chunks(parsed, strip_chrome(page_texts), page_labels)


def convert_library(tmp_path: Path, members: dict[str, str], **kwargs) -> t2p.Result:
    src = write_library(tmp_path, members)
    plan = t2p.plan_library(src, kwargs.pop("stem", STEM), kwargs.pop("title", TITLE), **kwargs)
    return t2p.write_pdf(plan, tmp_path / "corpus" / "site" / "macros" / "test")


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    """One converted standard library per module (read-only in the tests)."""
    root = tmp_path_factory.mktemp("library")
    result = convert_library(root, standard_members())
    return result, *ingest(result.path, corpus_root=root / "corpus")


MEMBERS = sorted(standard_members())
MACROS = [f"ZZM{i:02d}" for i in range(1, 11)]


# --- Library round trip -----------------------------------------------------
def test_library_identity_title_and_layout_labels(library):
    result, parsed, _ = library
    assert result.path.name == f"{STEM}.pdf"
    assert parsed.doc_id == STEM
    assert parsed.title == TITLE
    assert (parsed.product, parsed.version, parsed.vendor) == ("macros", "test", "unknown")


def test_library_heading_paths_are_member_names(library):
    _, parsed, chunks = library
    names = sorted(standard_members())
    assert [title for _, title, _ in parsed.toc] == names
    assert {level for level, _, _ in parsed.toc} == {1}
    assert {c.heading_path for c in chunks} == set(names)


@pytest.mark.parametrize("name", MEMBERS)
def test_library_citation_names_doc_title_member_and_pdf_page(library, name):
    _, parsed, chunks = library
    chunk = next(c for c in chunks if c.heading_path == name and c.ordinal == 0)
    page = {title: page for _, title, page in parsed.toc}[name]
    cite = format_citation(chunk.doc_id, parsed.title, chunk.heading_path, chunk.page_label,
                           chunk.page_start, chunk.page_end)
    assert cite == f"{STEM} {TITLE}, {name}, p. PDF {page}"


@pytest.mark.parametrize("name", MEMBERS)
def test_library_member_text_is_only_under_its_own_heading(library, name):
    _, _, chunks = library
    holders = {c.heading_path for c in chunks if f"MARKER-{name}" in c.text}
    assert holders == {name}


def test_library_columns_survive_in_chunk_text(library):
    _, _, chunks = library
    text = next(c.text for c in chunks if c.heading_path == "ZZM03")
    expected = macro("ZZM03").splitlines()
    # Every line keeps its columns except the first line of a paragraph,
    # whose leading blanks make_chunks strips (para.strip()).
    assert text.splitlines() == [expected[0].strip(), *expected[1:]]
    job = next(c.text for c in chunks if c.heading_path == "ZZJOB1")
    assert "//             PARM='MODE=TEST'" in job.splitlines()
    assert "  OPTION ONE" in job.splitlines()


def test_blank_records_stay_paragraph_breaks(tmp_path):
    """A blank record renders as one space, so it stays a paragraph break
    (prose-like members split at paragraphs, not mid-line). Today's
    make_chunks strips each paragraph, so the line after a blank record loses
    its leading blanks: the documented exception to column preservation."""
    text = "         MACRO\n         LA    1,0\n\n         LA    2,0\n         MEND\n"
    result = convert_library(tmp_path, {"ZZB": text})
    _, chunks = ingest(result.path)
    assert [c.text for c in chunks] == ["MACRO\n         LA    1,0\n\nLA    2,0\n         MEND"]


@pytest.mark.parametrize("name", MACROS)
def test_library_macro_and_mend_survive_chrome_stripping(library, name):
    """MACRO and MEND open and close every member page: the chrome rules
    delete such repeated edge lines unless the header and footer hold the
    page edges."""
    _, _, chunks = library
    text = "\n".join(c.text for c in chunks if c.heading_path == name)
    assert text.startswith("MACRO\n") and text.endswith("\n         MEND")


def test_library_numeric_last_record_survives_chrome_stripping(library):
    _, _, chunks = library
    data = "\n".join(c.text for c in chunks if c.heading_path == "ZZDATA")
    assert data.endswith("00042")


def test_library_cover_and_chrome_produce_no_chunks(library):
    result, parsed, chunks = library
    first_member_page = min(page for _, _, page in parsed.toc)
    assert first_member_page == t2p.COVER_PAGES + 1
    assert min(c.page_start for c in chunks) == t2p.COVER_PAGES
    assert not any(TITLE in c.text or STEM in c.text for c in chunks)
    assert result.chunks == len(chunks)


def test_long_member_splits_within_budget_and_tiny_member_stays_whole(library):
    _, _, chunks = library
    long = [c for c in chunks if c.heading_path == "ZZLONG"]
    assert len(long) >= 5
    assert all(len(c.text) <= SECTION_MAX_CHARS for c in long)
    # Statements are never cut: every chunk line is a whole source line.
    source = set(macro("ZZLONG", body_lines=600).splitlines()) | {"MACRO"}
    assert all(line in source for c in long for line in c.text.splitlines())
    tiny = [c for c in chunks if c.heading_path == "ZZTINY"]
    assert [c.text for c in tiny] == ["* MARKER-ZZTINY\n         BR    14"]


def test_output_is_byte_deterministic(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = convert_library(tmp_path / "a", standard_members())
    b = convert_library(tmp_path / "b", standard_members())
    assert a.sha256 == b.sha256 == hashlib.sha256(a.path.read_bytes()).hexdigest()
    assert a.path.read_bytes() == b.path.read_bytes()


def test_creation_date_is_fixed_by_flag_only(tmp_path):
    src = write_library(tmp_path, {"ZZA": macro("ZZA")})
    plan = t2p.plan_library(src, STEM, TITLE)
    plain, _ = t2p.render(plan)
    dated, _ = t2p.render(plan, "2026-11-17")
    assert dated == t2p.render(plan, "2026-11-17")[0] and dated != plain
    assert pymupdf.open(stream=plain).metadata["creationDate"] == ""
    assert pymupdf.open(stream=dated).metadata["creationDate"] == "D:20261117000000Z"
    with pytest.raises(t2p.ConversionError, match="YYYY-MM-DD"):
        t2p.render(plan, "17/11/2026")


def test_sequence_numbers_are_stripped_on_request(tmp_path):
    text = "".join(f"{line:<72}{n * 10:08d}\n" for n, line in enumerate(macro("ZZSEQ").splitlines(), 1))
    result = convert_library(tmp_path, {"ZZSEQ": text}, strip_sequence=True)
    _, chunks = ingest(result.path)
    body = "\n".join(c.text for c in chunks)
    assert "00000010" not in body and "MARKER-ZZSEQ" in body
    assert "&LABEL   LA    1,&AREA                 POINT AT THE AREA" in body.splitlines()


def test_wide_records_keep_their_columns(tmp_path):
    wide = "* WIDE RECORD" + " " * 100 + "COLUMN-120-MARK\n"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(t2p.ConversionError, match="longer than 80 columns"):
        convert_library(tmp_path / "a", {"ZZWIDE": wide})
    result = convert_library(tmp_path / "b", {"ZZWIDE": "* FIRST\n" + wide}, columns=133)
    _, chunks = ingest(result.path)
    assert wide.rstrip("\n") in chunks[0].text.splitlines()


def test_skipped_names_are_bookmarked_with_a_suffix(tmp_path):
    members = {"ZZINDEX": macro("ZZINDEX"), "TABLES": macro("TABLES"), "ZZA": macro("ZZA")}
    result = convert_library(tmp_path, members)
    _, chunks = ingest(result.path)
    assert {c.heading_path for c in chunks} == {"ZZA", "ZZINDEX (member)", "TABLES (member)"}
    assert all("MARKER-ZZA" not in c.text for c in chunks if c.heading_path != "ZZA")
    assert any("ZZINDEX" in n for n in result.notices)


@pytest.mark.parametrize("name, expected", [
    ("IEFBR14", "IEFBR14"), ("iefbr14.jcl", "IEFBR14"), ("@ABC#$", "@ABC#$"), ("A", "A"),
    ("ZZ99.txt", "ZZ99"),
])
def test_pds_member_names(name, expected):
    assert t2p.member_name(name, shop_names=False) == expected


@pytest.mark.parametrize("name", ["1ABC", "TOOLONGNAME", "AB-C", "A B", "AB_C", "\u00c4BC"])
def test_invalid_pds_member_names_are_refused(name):
    with pytest.raises(t2p.ConversionError, match="not a PDS member name"):
        t2p.member_name(name, shop_names=False)


def test_shop_names_keep_the_file_name_and_stay_citation_safe(tmp_path):
    result = convert_library(tmp_path, {"build-step.rexx": "/* REXX */\nsay 'MARKER'\n"}, shop_names=True)
    _, chunks = ingest(result.path)
    assert [c.heading_path for c in chunks] == ["build-step.rexx"]
    (tmp_path / "x").mkdir()
    with pytest.raises(t2p.ConversionError, match="comma"):
        convert_library(tmp_path / "x", {"a,b.rexx": "say 1\n"}, shop_names=True)


@pytest.mark.parametrize("title, reason", [
    ("Site export, SYS1.MACLIB", "comma"),
    ("Site export p. 5 macros", "page mark"),
    ("Site export SC28-1234 macros", "form number"),
    ("Site  export", "would change"),
    ("Site export \u2014 macros", "U\\+2014"),
    ("x" * 201, "longer than"),
    ("---", "no letter"),
])
def test_unsafe_titles_are_refused(tmp_path, title, reason):
    src = write_library(tmp_path, {"ZZA": macro("ZZA")})
    with pytest.raises(t2p.ConversionError, match=reason):
        t2p.plan_library(src, STEM, title)


@pytest.mark.parametrize("stem", ["SINGLEWORD", "SC28-1234-SITE", "bad name-x", "SITE-SC28-1234"])
def test_unsafe_output_names_are_refused(tmp_path, stem):
    src = write_library(tmp_path, {"ZZA": macro("ZZA")})
    with pytest.raises(t2p.ConversionError, match="output name"):
        t2p.plan_library(src, stem, TITLE)


@pytest.mark.parametrize("text, reason", [
    ("* CAF\u00c9 OK\n* \u20ac NOT OK\n", "line 2: character U\\+20AC"),
    ("* BELL\x07\n", "U\\+0007"),
])
def test_unrenderable_characters_are_refused(tmp_path, text, reason):
    with pytest.raises(t2p.ConversionError, match=reason):
        convert_library(tmp_path, {"ZZA": text})


def test_printable_latin1_round_trips(tmp_path):
    result = convert_library(tmp_path, {"ZZA": "* CAF\u00c9 \u00ac\u00a2 \xff\n"})
    assert ingest(result.path)[1][0].text == "* CAF\u00c9 \u00ac\u00a2 \xff"


def test_undecodable_bytes_name_the_member(tmp_path):
    src = tmp_path / "export"
    src.mkdir()
    (src / "ZZA").write_bytes(b"\xc1\xc2\xff\n")
    with pytest.raises(t2p.ConversionError, match="member ZZA: cannot decode as utf-8"):
        t2p.plan_library(src, STEM, TITLE)
    assert t2p.plan_library(src, STEM, TITLE, encoding="cp1252").pages


def test_duplicate_symlinked_and_nested_members_are_refused(tmp_path):
    src = write_library(tmp_path, {"ZZA": "x\n", "zza.txt": "y\n"})
    with pytest.raises(t2p.ConversionError, match="same member name"):
        t2p.plan_library(src, STEM, TITLE)
    (src / "zza.txt").unlink()
    (src / "ZZB").symlink_to(src / "ZZA")
    with pytest.raises(t2p.ConversionError, match="not a regular file"):
        t2p.plan_library(src, STEM, TITLE)
    (src / "ZZB").unlink()
    (src / "SUB").mkdir()
    with pytest.raises(t2p.ConversionError, match="not a regular file"):
        t2p.plan_library(src, STEM, TITLE)


def test_literal_credentials_are_refused_unless_reviewed(tmp_path):
    members = {
        "ZZOK": "         ZZLOGON PASSWORD=&PW,TOKEN=*\n",
        "ZZBAD": "//ZZJOB   JOB (A),'X',PASSWORD=TOPS3CR3T\n",
    }
    src = write_library(tmp_path, members)
    with pytest.raises(t2p.ConversionError, match=r"ZZBAD:1;") as err:
        t2p.plan_library(src, STEM, TITLE)
    assert "TOPS3CR3T" not in str(err.value) and "ZZOK" not in str(err.value)
    assert t2p.plan_library(src, STEM, TITLE, allow_secret_like=True).pages


def test_size_caps_refuse(tmp_path, monkeypatch):
    src = write_library(tmp_path, {"ZZA": macro("ZZA")})
    monkeypatch.setattr(t2p, "MAX_MEMBER_BYTES", 10)
    with pytest.raises(t2p.ConversionError, match="size cap"):
        t2p.plan_library(src, STEM, TITLE)
    monkeypatch.setattr(t2p, "MAX_MEMBER_BYTES", 1 << 20)
    monkeypatch.setattr(t2p, "MAX_PAGES", 5)
    plan = t2p.plan_library(src, STEM, TITLE)
    with pytest.raises(t2p.ConversionError, match="more than 5 pages"):
        t2p.render(plan)


def test_existing_output_is_kept_unless_identical_or_replaced(tmp_path):
    first = convert_library(tmp_path, {"ZZA": macro("ZZA")})
    src = tmp_path / "export"
    again = t2p.write_pdf(t2p.plan_library(src, STEM, TITLE), first.path.parent)
    assert again.sha256 == first.sha256
    (src / "ZZB").write_text(macro("ZZB"))
    changed = t2p.plan_library(src, STEM, TITLE)
    with pytest.raises(t2p.ConversionError, match="exists with different content"):
        t2p.write_pdf(changed, first.path.parent)
    assert hashlib.sha256(first.path.read_bytes()).hexdigest() == first.sha256
    assert t2p.write_pdf(changed, first.path.parent, replace=True).sha256 != first.sha256
    assert [p.name for p in first.path.parent.iterdir()] == [first.path.name]


def test_corpus_root_layout_is_checked(tmp_path):
    src = write_library(tmp_path, {"ZZA": macro("ZZA")})
    plan = t2p.plan_library(src, STEM, TITLE)
    root = tmp_path / "corpus"
    with pytest.raises(t2p.ConversionError, match="<vendor>/<product>/<version>"):
        t2p.write_pdf(plan, root / "site" / "macros", corpus_root=root)
    result = t2p.write_pdf(plan, root / "ibm-site" / "macros" / "sysa", corpus_root=root)
    assert any("vendor 'IBM'" in n for n in result.notices)


def test_self_check_refuses_output_ingest_would_misread(tmp_path, monkeypatch):
    """A plan whose rendering ingest reads differently never reaches the
    output folder (here: a forged outline title)."""
    src = write_library(tmp_path, {"ZZA": macro("ZZA")})
    plan = t2p.plan_library(src, STEM, TITLE)
    real_render = t2p.render

    def forged(p, creation_date=None):
        data, expected = real_render(p, creation_date)
        doc = pymupdf.open(stream=data)
        doc.set_toc([[1, "OTHER", 5]])
        return doc.tobytes(), expected

    monkeypatch.setattr(t2p, "render", forged)
    out = tmp_path / "out"
    with pytest.raises(t2p.ConversionError, match="self-check: ingest reads a different outline"):
        t2p.write_pdf(plan, out)
    assert list(out.iterdir()) == []


def test_conversion_opens_no_network_connection(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network use")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert convert_library(tmp_path, {"ZZA": macro("ZZA")}).chunks


def test_cli_library_prints_ids_only(tmp_path, capsys):
    src = write_library(tmp_path, {"ZZA": macro("ZZA")})
    out = tmp_path / "out"
    rc = t2p.main(["library", "--src", str(src), "--out-dir", str(out), "--name", STEM, "--title", TITLE])
    printed = capsys.readouterr()
    assert rc == 0
    assert "MARKER" not in printed.out + printed.err
    assert f'"doc_id": "{STEM}"' in printed.out
    rc = t2p.main(["library", "--src", str(src), "--out-dir", str(out), "--name", STEM, "--title", "a, b"])
    assert rc == 2 and "refused" in capsys.readouterr().err


# --- Markdown ---------------------------------------------------------------
GUIDE = """# House guide - JCL coding standards (AI draft)

This guide is an original draft written for the converter tests. MARKER-INTRO

## Job statements

Every job names an owner and a class. MARKER-JOBS \u201cquoted\u201d \u2014 dashed.

### Examples

```jcl
//ZZJOB    JOB (ACCT),'OWNER',CLASS=A
# not a heading inside a fence
//STEP1    EXEC PGM=ZZPROG
```

### Checklist

- Name the owner. MARKER-CHECK
- Set the class
  on every job.

## DD statements

### Examples

| Keyword | Rule |
|---------|------|
| DISP    | SHR for input. MARKER-DD |

#### Note in bold

A wrapped paragraph that is long enough to need more than one line when it is set in the prose font across the page. MARKER-WRAP
"""


def convert_guide(tmp_path: Path, text: str = GUIDE, name: str = "HOUSE-AI-DRAFT-JCL-STANDARDS"):
    src = tmp_path / f"{name}.md"
    src.write_text(text, encoding="utf-8")
    return t2p.write_pdf(t2p.plan_markdown(src), tmp_path / "corpus" / "house" / "guidance" / "2026-11-17")


def test_markdown_title_outline_and_citations(tmp_path):
    result = convert_guide(tmp_path)
    parsed, chunks = ingest(result.path)
    assert parsed.doc_id == "HOUSE-AI-DRAFT-JCL-STANDARDS"
    assert parsed.title == "House guide - JCL coding standards (AI draft)"
    assert [(level, title) for level, title, _ in parsed.toc] == [
        (1, "Job statements"), (2, "Examples"), (2, "Checklist"), (1, "DD statements"), (2, "Examples"),
    ]
    owner = {m: c.heading_path for c in chunks for m in
             ("INTRO", "JOBS", "CHECK", "DD", "WRAP", "ZZPROG") if f"MARKER-{m}" in c.text or m in c.text}
    assert owner == {
        "INTRO": parsed.title,
        "JOBS": "Job statements",
        "ZZPROG": "Job statements > Examples",
        "CHECK": "Job statements > Checklist",
        "DD": "DD statements > Examples",
        "WRAP": "DD statements > Examples",
    }
    check = next(c for c in chunks if "MARKER-CHECK" in c.text)
    cite = format_citation(check.doc_id, parsed.title, check.heading_path, check.page_label,
                           check.page_start, check.page_end)
    assert cite == (f"HOUSE-AI-DRAFT-JCL-STANDARDS {parsed.title}, Job statements > Checklist, "
                    f"p. PDF {check.page_start + 1}")


def test_markdown_code_is_verbatim_and_prose_is_wrapped(tmp_path):
    result = convert_guide(tmp_path)
    _, chunks = ingest(result.path)
    code = next(c.text for c in chunks if "ZZPROG" in c.text).splitlines()
    assert "# not a heading inside a fence" in code
    assert "//STEP1    EXEC PGM=ZZPROG" in code
    table = next(c.text for c in chunks if "MARKER-DD" in c.text).splitlines()
    assert table[table.index("| Keyword | Rule |"):][:3] == [
        "| Keyword | Rule |", "|---------|------|", "| DISP    | SHR for input. MARKER-DD |"]
    jobs = next(c.text for c in chunks if "MARKER-JOBS" in c.text)
    assert "\"quoted\" -- dashed." in jobs
    wrap = next(c.text for c in chunks if "MARKER-WRAP" in c.text)
    assert "Note in bold" in wrap
    paragraph = wrap[wrap.index("A wrapped"):]
    assert paragraph.count("\n") >= 1 and " ".join(paragraph.split()).endswith("page. MARKER-WRAP")
    # A list item keeps its marker; its continuation line joins the item.
    items = next(c.text for c in chunks if "MARKER-CHECK" in c.text).splitlines()
    assert items[-3:] == ["- Name the owner. MARKER-CHECK", "", "- Set the class on every job."]


@pytest.mark.parametrize("text, reason", [
    ("Intro first\n# Title\n", "must start with one H1"),
    ("# Title\n\n# Again\n", "second H1"),
    ("# Title\n\n### Early\n", "H3 before any H2"),
    ("# Title\n\n## Notices\n", "ingest skips"),
    ("# Title\n\n## One, two\n", "comma"),
    ("# Title, with comma\n", "comma"),
    ("# Title\n\n```\nopen fence\n", "never closed"),
    ("# Title\n\nArrow \u21d2 here\n", "line 3: character U\\+21D2"),
    ("\n\n", "no H1"),
])
def test_markdown_refusals(tmp_path, text, reason):
    src = tmp_path / "HOUSE-AI-DRAFT-X.md"
    src.write_text(text, encoding="utf-8")
    with pytest.raises(t2p.ConversionError, match=reason):
        t2p.plan_markdown(src)


def test_cli_markdown(tmp_path, capsys):
    src = tmp_path / "HOUSE-AI-DRAFT-JCL-STANDARDS.md"
    src.write_text(GUIDE, encoding="utf-8")
    assert t2p.main(["markdown", "--src", str(src), "--out-dir", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "HOUSE-AI-DRAFT-JCL-STANDARDS.pdf").is_file()
    assert "MARKER" not in capsys.readouterr().out


# --- Word-exported PDFs (docx_to_pdf.ps1) -----------------------------------
def word_style_pdf(path: Path) -> None:
    """What Word's ExportAsFixedFormat with heading bookmarks produces: a
    /Title, nested heading bookmarks, headings that share a page (Word does
    not break pages at headings) and a repeated heading title."""
    doc = pymupdf.open()
    pages = [
        ["Operations handbook", "Purpose of this handbook. MARKER-PURPOSE"],
        ["1 Batch window", "The batch window opens nightly. MARKER-BATCH",
         "1.1 Restart rules", "Restart from the failed step. MARKER-RESTART"],
        ["Restart rules continued. MARKER-RESTART2", "Examples", "Example one. MARKER-EX1"],
        ["2 Online window", "Online hours. MARKER-ONLINE", "Examples", "Example two. MARKER-EX2"],
    ]
    for lines in pages:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontname="helv", fontsize=11)
            y += 20
    doc.set_toc([
        [1, "1 Batch window", 2], [2, "1.1 Restart rules", 2], [2, "Examples", 3],
        [1, "2 Online window", 4], [2, "Examples", 4],
    ])
    doc.set_metadata({"title": "Site operations handbook"})
    doc.save(path)


def test_word_style_pdf_headings_and_citations(tmp_path):
    """Today's outline rules on a Word export (pinned, see the operator
    section): a page belongs to the bookmarks that start on it, so text
    continued from the previous page is filed under the next heading, a
    parent's intro on its child's page folds into the child, and a repeated
    child title ("Examples") does not split its page."""
    path = tmp_path / "corpus" / "site" / "docs" / "2026" / "OPS-HANDBOOK-2026.pdf"
    path.parent.mkdir(parents=True)
    word_style_pdf(path)
    parsed, chunks = ingest(path, corpus_root=tmp_path / "corpus")
    assert parsed.doc_id == "OPS-HANDBOOK-2026"
    assert parsed.title == "Site operations handbook"
    assert (parsed.product, parsed.version) == ("docs", "2026")
    markers = ("PURPOSE", "BATCH", "RESTART", "RESTART2", "EX1", "ONLINE", "EX2")
    owner = {m: (c.heading_path, c.page_start + 1) for c in chunks for m in markers
             if f"MARKER-{m}" in c.text.split()}
    assert owner == {
        "PURPOSE": ("Site operations handbook", 1),
        "BATCH": ("1 Batch window > 1.1 Restart rules", 2),
        "RESTART": ("1 Batch window > 1.1 Restart rules", 2),
        "RESTART2": ("1 Batch window > Examples", 3),
        "EX1": ("1 Batch window > Examples", 3),
        "ONLINE": ("2 Online window > Examples", 4),
        "EX2": ("2 Online window > Examples", 4),
    }
    restart = next(c for c in chunks if "MARKER-RESTART" in c.text.split())
    cite = format_citation(restart.doc_id, parsed.title, restart.heading_path, restart.page_label,
                           restart.page_start, restart.page_end)
    assert cite == ("OPS-HANDBOOK-2026 Site operations handbook, 1 Batch window > 1.1 Restart rules, "
                    "p. PDF 2")


def test_long_member_page_split_keeps_columns(tmp_path, monkeypatch):
    """A member longer than a page is split before a column-1 line where one
    is near the limit, so no continuation page starts with an indented
    statement whose blanks ingest would strip."""
    monkeypatch.setattr(t2p, "MAX_LINES_PER_PAGE", 50)
    monkeypatch.setattr(t2p, "SPLIT_SEARCH_LINES", 20)
    lines = [f"         LA    1,{i}" if i % 10 else f"* SECTION {i}" for i in range(1, 121)]
    result = convert_library(tmp_path, {"ZZBIG": "\n".join(lines) + "\n"})
    parsed, chunks = ingest(result.path)
    assert parsed.page_count == t2p.COVER_PAGES + 3
    chunk_lines = [ln for c in chunks for ln in c.text.split("\n")]
    assert set(chunk_lines) <= set(lines)


def test_page_split_without_column1_line_falls_back_to_the_limit():
    lines = ["         LA    1,0"] * 30
    assert t2p._page_split(lines, 0) == len(lines)
    assert t2p._page_split(["         X"] * 3000, 0) == t2p.MAX_LINES_PER_PAGE
    near = ["         X"] * 3000
    near[990] = "* LABEL"
    assert t2p._page_split(near, 0) == 990


def test_empty_member_between_members_gets_a_bookmark_and_no_chunks(tmp_path):
    result = convert_library(tmp_path, {"ZZA": macro("ZZA"), "ZZE": "\n\n", "ZZZ": macro("ZZZ")})
    parsed, chunks = ingest(result.path)
    assert [title for _, title, _ in parsed.toc] == ["ZZA", "ZZE", "ZZZ"]
    assert {c.heading_path for c in chunks} == {"ZZA", "ZZZ"}
    assert any("ZZE is empty" in n for n in result.notices)


# --- Markdown edge cases ------------------------------------------------------
def test_markdown_closing_hashes_need_a_space_before_them(tmp_path):
    text = "# Guide\n\n## Programming in C#\n\nBody A.\n\n## Closed heading ##\n\nBody B.\n"
    result = convert_guide(tmp_path, text, "HOUSE-AI-DRAFT-C")
    parsed, _ = ingest(result.path)
    assert [title for _, title, _ in parsed.toc] == ["Programming in C#", "Closed heading"]


def test_markdown_indented_code_is_verbatim(tmp_path):
    text = ("# Guide\n\n## Jobs\n\nRun this:\n\n    //STEP1   EXEC PGM=X\n"
            "    //DD1     DD *\n\nAfter.\n")
    result = convert_guide(tmp_path, text, "HOUSE-AI-DRAFT-IND")
    _, chunks = ingest(result.path)
    # Line by line, columns kept, the 4-space Markdown indent removed.
    assert chunks[0].text == "Jobs\n\nRun this:\n\n//STEP1   EXEC PGM=X\n//DD1     DD *\n\nAfter."


def test_markdown_thematic_break_is_dropped(tmp_path):
    result = convert_guide(tmp_path, "# Guide\n\n## A\n\nOne.\n\n---\n\nTwo.\n", "HOUSE-AI-DRAFT-HR")
    _, chunks = ingest(result.path)
    assert chunks[0].text == "A\n\nOne.\n\nTwo."


@pytest.mark.parametrize("text, reason", [
    ("# Title\n\nSection\n-------\n", "setext"),
    ("```\ncode\n```\n# Title\n", "must start with one H1"),
])
def test_markdown_unsupported_structure_is_refused(tmp_path, text, reason):
    src = tmp_path / "HOUSE-AI-DRAFT-X.md"
    src.write_text(text, encoding="utf-8")
    with pytest.raises(t2p.ConversionError, match=reason):
        t2p.plan_markdown(src)


# --- check: existing PDFs (Word exports) ---------------------------------------
def word_pdf_at(tmp_path: Path, name: str, page_one: str, bookmark: str = "1 Batch window") -> Path:
    path = tmp_path / f"{name}.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), page_one, fontname="helv", fontsize=11)
    doc.set_toc([[1, bookmark, 1]])
    doc.set_metadata({"title": "Site operations handbook"})
    doc.save(path)
    return path


def test_check_accepts_a_clean_word_export(tmp_path):
    path = word_pdf_at(tmp_path, "OPS-HANDBOOK-2026", "Batch window rules.")
    assert t2p.check_pdf(path) == ("OPS-HANDBOOK-2026", 1, [], [])


@pytest.mark.parametrize("name, page_one, bookmark, problem", [
    ("OPS-HANDBOOK-2026", "See the site copy of ZZ23-1385-07.", "1 Batch window", "IBM form number"),
    ("OPS-HANDBOOK-2026", "Batch window rules.", "Batch, nightly", "bookmark 1 contains a comma"),
    ("HANDBOOK", "Batch window rules.", "1 Batch window", "output name must be"),
])
def test_check_reports_what_ingest_would_misread(tmp_path, name, page_one, bookmark, problem):
    path = word_pdf_at(tmp_path, name, page_one, bookmark)
    _, _, problems, _ = t2p.check_pdf(path)
    assert any(problem in p for p in problems), problems


def test_cli_check_exit_codes(tmp_path, capsys):
    good = word_pdf_at(tmp_path, "OPS-HANDBOOK-2026", "Batch window rules.")
    assert t2p.main(["check", str(good)]) == 0
    bad = word_pdf_at(tmp_path, "OPS-OTHER-2026", "See ZZ23-1385-07.")
    assert t2p.main(["check", str(good), str(bad)]) == 2
    out = capsys.readouterr().out
    assert '"doc_id": "ZZ23-1385-07"' in out and "See ZZ23" not in out
