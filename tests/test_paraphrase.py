"""Paraphrase retrieval instrument (issue #78 sequence): generator behavior and
file validity. All hermetic — PDFs are generated at runtime, never committed."""

import json
from pathlib import Path

import pymupdf
from scripts.eval_retrieval import load_golden
from scripts.gate_l1 import generate_synthetic_golden_corpus

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


def test_abstain_entry_generates_no_doc(tmp_path):
    entry = {
        "id": "NEG-T",
        "query": "What PTFs are applied?",
        "expected_behavior": "abstain",
        "must_not_retrieve": ["SA22-8001-00"],
    }
    out = tmp_path / "corpus"
    info = generate_synthetic_golden_corpus([entry], out)
    assert info["docs_generated"] == 1  # distractor only
    assert list(out.glob("*.pdf")) == [out / "generic-distractor.pdf"]


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


def test_paraphrase_file_loads_and_has_unique_ids():
    entries = load_golden(PARAPHRASE_PATH)
    assert len(entries) >= 10
    ids = [e.id for e in entries]
    assert len(set(ids)) == len(ids)


def test_paraphrase_answer_entries_carry_answer_text():
    for raw in _paraphrase_entries():
        if raw.get("expected_behavior", "answer") == "answer":
            assert raw.get("expected_doc_ids"), raw.get("id")
            assert raw.get("answer_text"), f"{raw.get('id')}: answer entry needs answer_text"
        else:
            assert not raw.get("expected_doc_ids"), raw.get("id")
            assert not raw.get("answer_text"), f"{raw.get('id')}: abstain entry must not set answer_text"


def test_paraphrase_doc_ids_disjoint_from_golden():
    """Separate corpora today; disjoint IDs keep a future merge safe and
    prove the paraphrase docs are genuinely new fixtures."""
    def doc_ids(path: Path) -> set[str]:
        ids: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.update(json.loads(line).get("expected_doc_ids", []))
        return ids

    overlap = doc_ids(PARAPHRASE_PATH) & doc_ids(GOLDEN_PATH)
    assert overlap == set(), f"paraphrase doc ids collide with golden: {overlap}"


def test_paraphrase_traps_reference_existing_docs():
    """A must_not trap against a doc that is never ingested can never fire —
    every trapped doc must exist in the paraphrase corpus."""
    corpus_docs: set[str] = set()
    for raw in _paraphrase_entries():
        corpus_docs.update(raw.get("expected_doc_ids", []))
    for raw in _paraphrase_entries():
        for trapped in raw.get("must_not_retrieve", []):
            assert trapped in corpus_docs, f"{raw.get('id')}: must_not {trapped} not in corpus"


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


def test_paraphrase_baselines_parse():
    for name, mode in (
        ("evals/baseline-paraphrase.json", "hash"),
        ("evals/baseline-paraphrase-vllm.json", "vllm"),
    ):
        data = json.loads(Path(name).read_text(encoding="utf-8"))
        meta = data["_meta"]
        assert meta["collection"] == "paraphrase-manuals"
        assert meta["embed_mode"] == mode
        assert meta["n"] == 22
        for key in ("recall@1", "recall@5", "mrr"):
            assert key in data, f"{name} missing {key}"
