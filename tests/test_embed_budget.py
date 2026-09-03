"""Worst-case embedded-string budget for the local embed server (issue #99).

The local vLLM embed server keeps --max-model-len 4096 and launches with
--runner pooling --convert embed --enforce-eager at GPU_MEM=0.33
(scripts/run_local_vllm.sh embed branch). These hermetic tests pin the worst
case the ingest pipeline can produce, so the window cannot silently rot when
constants change:

  - the real chunker (parse_pdf -> strip_chrome -> make_chunks) on a
    runtime-generated worst-case PDF must emit a body bounded by
    SECTION_MAX_CHARS + SPLIT_OVERLAP_CHARS (+ separator), and the test
    asserts the constructed case actually reaches that bound;
  - build_embed_text over that worst body with a maximal realistic header
    must stay inside a conservative char budget.

Budget derivation (issue #99 review + Task 1 sweep on PR #100): the sweep
tokenized the real pipeline's worst case (a 3902-char seeded syntax-dense
body + maximal header) with the REAL Qwen3-Embedding-0.6B tokenizer and
measured 2043 tokens — 2.03 chars/token, worse than the review's pessimistic
2.5 floor. That rejected the proposed 2048 window (2043 > 1800 acceptance),
so the local embed server keeps 4096 (the review's documented fallback; the
review also requires never shrinking the reasoning window). The char budget
below uses a 1.9 chars/token floor — worse than the measured worst ratio
(2.005) — against the 4096 window minus 512 tokens of headroom:
(4096 - 512) x 1.9 = 6809 chars. The current worst case measures 4147
chars / 2043 tokens.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import (
    SECTION_MAX_CHARS,
    SPLIT_OVERLAP_CHARS,
    make_chunks,
)
from mainframe_rag.ingest.embed import build_embed_text
from mainframe_rag.ingest.ibm_pdf import parse_pdf

# 4096-token local embed window minus 512 tokens of headroom, at a 1.9
# chars/token floor — worse than the measured worst ratio (2.005) on
# syntax-dense text (see the Task 1 sweep artifact on PR #100).
EMBED_WINDOW_TOKENS = 4096
TOKEN_HEADROOM = 512
MIN_CHARS_PER_TOKEN = 1.9
TOKEN_BUDGET = EMBED_WINDOW_TOKENS - TOKEN_HEADROOM  # 3584
CHAR_BUDGET = int(TOKEN_BUDGET * MIN_CHARS_PER_TOKEN)  # 6809

# The worst-case chunk body shape produced by _split_blocks: a block seeded
# with the previous block's SPLIT_OVERLAP_CHARS tail, then filled up to
# SECTION_MAX_CHARS, joined by the paragraph separator.
SEPARATOR_LEN = 2  # "\n\n"
MAX_BODY_CHARS = SECTION_MAX_CHARS + SPLIT_OVERLAP_CHARS + SEPARATOR_LEN

# Maximal REALISTIC header fields (real manual shapes, not adversarial):
# product + version + doc_id line, a long title, a deep heading path.
PRODUCT = "z/OS"
VERSION = "3.2"
DOC_ID = "SA23-1380-70"
TITLE = "z/OS MVS Initialization and Tuning Reference for Sysplex and Parallel Sysplex Environments"
HEADING_PATH = (
    "Chapter 1 System parameters > IEASYSxx (system initialization parameters) "
    "> LFAREA (large frame area) > Syntax and parameter values"
)

_UNIT = (
    "IEASYSxx LFAREA=(NNN,K,M),XCFAS=NO,PROGxx=APF ADD DSNAME=SYS1.USER.LINKLST,"
    "VOLUME=VOL123,CSVSYSINFO=YES,ALLOC=(xx),SVC=nnn,GRSCNFxx=CONFLICT,OPI=1,EXIT=01 "
)


def _syntax_dense(length: int) -> str:
    """Single-paragraph syntax-dense filler: JCL/parmlib-like tokens with
    punctuation — the tokenizer-entropy worst case, not prose. No blank
    lines, so _BLANK_SPLIT_RE keeps it as one paragraph."""
    repeated = _UNIT * (length // len(_UNIT) + 1)
    return repeated[:length].rstrip()


def _write_worst_case_pdf(path: Path) -> Path:
    """Two-page PDF, one syntax-dense paragraph per page, sized to force
    _split_blocks into its worst shape: a block seeded with the full
    SPLIT_OVERLAP_CHARS tail and filled to SECTION_MAX_CHARS. (Textboxes
    wrap within the page; single-point insert_text would clip long lines.)"""
    # para1 (3000) accumulates alone; para2 at exactly SECTION_MAX_CHARS
    # forces a flush of para1 and a seeded block of tail(400) + "\n\n" + para2.
    paras = [_syntax_dense(3000), _syntax_dense(SECTION_MAX_CHARS)]

    doc = pymupdf.open()
    for para in paras:
        page = doc.new_page()
        leftover = page.insert_textbox(
            pymupdf.Rect(50, 50, page.rect.width - 50, page.rect.height - 50),
            para,
            fontsize=8,
        )
        assert leftover >= 0, "textbox overflow would silently drop worst-case text"
    doc.set_metadata({"title": TITLE, "author": "pdf-rag test fixture"})
    doc.save(path)
    doc.close()
    return path


def _worst_case_embed_text(tmp_path: Path) -> tuple[str, str]:
    """Drive the real pipeline (parse -> chrome -> chunks) and return the
    worst-case embedded string together with its raw body."""
    pdf = _write_worst_case_pdf(tmp_path / "SA23-1380-70.pdf")
    parsed = parse_pdf(pdf)
    doc = pymupdf.open(pdf)
    try:
        page_texts = [doc[i].get_text() for i in range(doc.page_count)]
    finally:
        doc.close()
    stripped = strip_chrome(page_texts)
    chunks = make_chunks(parsed, stripped, [None] * parsed.page_count)
    assert chunks, "worst-case PDF must produce chunks"

    longest = max(chunks, key=lambda c: len(c.text))
    embed_text = build_embed_text(PRODUCT, VERSION, DOC_ID, TITLE, HEADING_PATH, longest.text)
    return embed_text, longest.text


def test_chunker_emits_the_worst_case_body_shape(tmp_path: Path):
    """The constructed PDF must actually push make_chunks to its worst body
    size — if the chunker ever changes shape, this fails before the budget
    assertions below can pass vacuously."""
    _, body = _worst_case_embed_text(tmp_path)
    assert len(body) >= SECTION_MAX_CHARS + SPLIT_OVERLAP_CHARS
    assert len(body) <= MAX_BODY_CHARS


def test_worst_case_embed_text_under_char_budget(tmp_path: Path):
    """Worst-case embedded string (header + syntax-dense body with overlap
    seed) must stay inside the conservative char budget. Fails if
    SECTION_MAX_CHARS, SPLIT_OVERLAP_CHARS, or the build_embed_text header
    grows the worst case past the embed window's headroom budget."""
    embed_text, _ = _worst_case_embed_text(tmp_path)
    assert len(embed_text) <= CHAR_BUDGET, (
        f"worst-case embed text is {len(embed_text)} chars; budget is "
        f"{CHAR_BUDGET} ({TOKEN_BUDGET} tokens at {MIN_CHARS_PER_TOKEN} chars/token). "
        "The local embed server window would no longer be safe; re-run the "
        "issue #99 tokenizer sweep before changing anything."
    )


def test_worst_case_with_max_context_prefix_under_budget(tmp_path: Path):
    """Issue #78: the contextual prefix is deterministically capped at
    context_max_chars, so the worst case stays provable — header + max
    prefix + worst body must still fit the window's headroom budget."""
    from mainframe_rag.config import Settings

    max_prefix = Settings(_env_file=None).context_max_chars
    _, body = _worst_case_embed_text(tmp_path)
    with_prefix = build_embed_text(
        PRODUCT, VERSION, DOC_ID, TITLE, HEADING_PATH, body,
        context="x" * max_prefix,
    )
    assert len(with_prefix) <= CHAR_BUDGET + 1 + max_prefix
