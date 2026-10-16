"""Paraphrase retrieval instrument (issue #78 sequence): generator behavior and
file validity. All hermetic — PDFs are generated at runtime, never committed."""

import json
from pathlib import Path

import pymupdf
from scripts.gate_l1 import generate_synthetic_golden_corpus

from mainframe_rag.eval.datasets import load_golden

PARAPHRASE_PATH = Path("evals/paraphrase.jsonl")
GOLDEN_PATH = Path("evals/golden.jsonl")

# A verbatim run of this many consecutive query words on the target page
# breaks the instrument's contract (the query must not appear near-verbatim).
MAX_VERBATIM_RUN = 8


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _page_texts(out_dir: Path, doc_id: str) -> str:
    doc = pymupdf.open(out_dir / f"{doc_id}.pdf")
    try:
        return "\n".join(doc[i].get_text() for i in range(doc.page_count))
    finally:
        doc.close()


def _verbatim_runs(query_words: list[str], page: str, n: int = MAX_VERBATIM_RUN) -> list[str]:
    found = []
    for i in range(len(query_words) - n + 1):
        run = " ".join(query_words[i : i + n])
        if run in page:
            found.append(run)
    return found


def test_paraphrase_entry_page_has_answer_without_query_echo(tmp_path):
    entry = {
        "id": "PAR-T",
        "query": "Which manual documents the IEASYSxx members used at IPL?",
        "expected_doc_ids": ["SA22-8001-00"],
        "expected_heading": "IEASYSxx members",
        "must_cite_identifier": "IEASYSxx",
        "answer_text": "The z/OS MVS Initialization and Tuning Reference describes every IEASYSxx member.",
    }
    out = tmp_path / "corpus"
    generate_synthetic_golden_corpus([entry], out)
    page = _norm(_page_texts(out, "SA22-8001-00"))
    assert "the z/os mvs initialization and tuning reference describes" in page
    assert "identifier: ieasysxx" in page
    assert _norm(entry["query"]) not in page
    assert _verbatim_runs(_norm(entry["query"]).split(), page) == []


def test_standard_entry_still_echoes_query(tmp_path):
    """The paraphrase branch must not change legacy behavior: standard
    entries keep writing the query text into the page."""
    entry = {
        "id": "MSG-T",
        "query": "What does IEA500I mean?",
        "expected_doc_ids": ["SA22-0000-00"],
    }
    out = tmp_path / "corpus"
    generate_synthetic_golden_corpus([entry], out)
    assert _norm(entry["query"]) in _norm(_page_texts(out, "SA22-0000-00"))


def test_generator_is_deterministic(tmp_path):
    entries = [
        {
            "id": "PAR-T",
            "query": "Which manual documents the IEASYSxx members used at IPL?",
            "expected_doc_ids": ["SA22-8001-00"],
            "expected_heading": "IEASYSxx members",
            "answer_text": "The z/OS MVS Initialization and Tuning Reference describes every IEASYSxx member.",
        }
    ]
    out1, out2 = tmp_path / "a", tmp_path / "b"
    generate_synthetic_golden_corpus(entries, out1)
    generate_synthetic_golden_corpus(entries, out2)
    assert _page_texts(out1, "SA22-8001-00") == _page_texts(out2, "SA22-8001-00")


def _paraphrase_entries() -> list[dict]:
    return [
        json.loads(line)
        for line in PARAPHRASE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_paraphrase_file_is_valid_instrument_input():
    """Unique ids; answer entries carry doc ids and answer_text, abstain
    entries neither; paraphrase docs are disjoint from golden's; every
    must_not trap names a doc that exists in the paraphrase corpus (a trap
    against a never-ingested doc can never fire)."""
    entries = load_golden(PARAPHRASE_PATH)
    ids = [e.id for e in entries]
    assert len(ids) >= 10 and len(set(ids)) == len(ids)

    raws = _paraphrase_entries()
    corpus_docs: set[str] = set()
    for raw in raws:
        if raw.get("expected_behavior", "answer") == "answer":
            assert raw.get("expected_doc_ids"), raw.get("id")
            assert raw.get("answer_text"), f"{raw.get('id')}: answer entry needs answer_text"
        else:
            assert not raw.get("expected_doc_ids"), raw.get("id")
            assert not raw.get("answer_text"), f"{raw.get('id')}: abstain entry must not set answer_text"
        corpus_docs.update(raw.get("expected_doc_ids", []))
    for raw in raws:
        for trapped in raw.get("must_not_retrieve", []):
            assert trapped in corpus_docs, f"{raw.get('id')}: must_not {trapped} not in corpus"

    golden_docs: set[str] = set()
    for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            golden_docs.update(json.loads(line).get("expected_doc_ids", []))
    assert corpus_docs & golden_docs == set()


def test_paraphrase_no_query_echo_in_generated_pages(tmp_path):
    """The instrument's core contract, enforced on the committed file: for
    every answer entry, the normalized query is not a substring of its
    target pages and no long verbatim word run leaks through."""
    entries = _paraphrase_entries()
    out = tmp_path / "corpus"
    generate_synthetic_golden_corpus(entries, out)
    for raw in entries:
        if raw.get("expected_behavior", "answer") != "answer":
            continue
        query = _norm(raw["query"])
        for doc_id in raw["expected_doc_ids"]:
            page = _norm(_page_texts(out, doc_id))
            assert query not in page, f"{raw['id']}: query echoed in {doc_id}"
            runs = _verbatim_runs(query.split(), page)
            assert runs == [], f"{raw['id']}: verbatim run in {doc_id}: {runs[0][:60]}"

