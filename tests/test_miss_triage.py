"""Retrieval-miss triage (issue #1383): buckets, the final_hits re-check,
repeat handling, refusals, read-only Qdrant access, and the export allowlist.

Synthetic data only: pools come from capture_pool's own record builder and
the stored corpus is an in-memory Qdrant collection."""

import json
import uuid

import pytest
from qdrant_client import QdrantClient, models
from scripts.answer_taxonomy import RETRIEVAL_MISS as TAXONOMY_RETRIEVAL_MISS
from scripts.answer_taxonomy import _gold_gaps
from scripts.capture_pool import legs_to_record

from mainframe_rag.eval import miss_triage as mt
from tests.helpers_eval_local import eval_settings
from tests.helpers_eval_local import point as scored_point

COLL = "triage_c"
CANARIES = {
    "query": "CANARY-QUERY-91aa",
    "effective": "CANARY-EFFECTIVE-22bb",
    "title": "CANARY-TITLE-33cc",
    "heading": "CANARY-HEADING-44dd",
    "text": "CANARY-TEXT-55ee",
    "reason": "CANARY-REASON-66ff",
    "doc": "CANARYDOC-77",
}


def cid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


# Stored corpus: (name, doc, heading, page, chunk_type, text)
STORED = [
    ("a-gold", "DOCA", "Part > Gold Section", "5", "table", "alpha body"),
    ("a-other", "DOCA", "Part > Other", "6", "narrative", "alpha other"),
    ("b-gold", "DOCB", "Intro", "1", "syntax", "beta body"),
    ("b2-gold", "DOCB2", "Intro", "1", "narrative", "beta two"),
    ("c-gold", "DOCC", "Intro", "1", "message", "gamma"),
    ("e-gold", "DOCE", "Intro", "1", "narrative", "epsilon"),
    ("u-gold", "DOCU", "Intro", "1", "narrative", "upsilon"),
    ("k-gold", "DOCK", "Intro", "1", "narrative", "kappa"),
    ("k-other", "DOCK", "Intro", "1", "narrative", "kappa other"),
    ("m-hit", "DOCM", "Intro", "1", "narrative", "has the needle word"),
    ("m-miss", "DOCM", "Intro", "1", "narrative", "nothing here"),
    ("cn-gold", CANARIES["doc"], CANARIES["heading"], "9", "table", CANARIES["text"]),
]


def corpus() -> QdrantClient:
    client = QdrantClient(":memory:")
    client.create_collection(COLL, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    client.upsert(COLL, [
        models.PointStruct(id=cid(name), vector=[1.0, 0.0], payload={
            "doc_id": doc, "title": CANARIES["title"] if doc == CANARIES["doc"] else "Guide",
            "heading_path": heading, "page_label": page, "chunk_type": ctype, "text": text,
            "product": "z/OS", "version": "3.1"})
        for name, doc, heading, page, ctype, text in STORED])
    return client


def golden(row_id, query, docs, **extra):
    return {"id": row_id, "query": query, "expected_doc_ids": docs, "expected_behavior": "answer",
            "query_class": "syntax", **extra}


GOLDEN = [
    golden("SYN-1", "q one", ["DOCA"], expected_heading="Gold Section"),  # A
    golden("SYN-2", "q two", ["DOCB"]),                                   # B, rank 3
    golden("SYN-3", "q three", ["DOCB2"]),                                # B, rank 45
    golden("SYN-4", "q four", ["DOCC"]),                                  # C (gold in final_hits)
    golden("SYN-5", "q five", ["DOCA"]),                                  # D (coverage none)
    golden("SYN-6", "q six", ["DOC-NOT-STORED"]),                         # D (nothing stored)
    golden("SYN-7", "q seven", ["DOCE"]),                                 # E (identifier)
    golden("SYN-8", "q eight", ["DOCU"]),                                 # unknown_no_final_hits
    golden("SYN-9", "q nine", ["DOCU"]),                                  # unknown_no_pool
    golden("SYN-10", "q ten", ["DOCK"], gold_chunk_ids=[cid("k-gold")]),  # B via gold_chunk_ids
    golden("SYN-11", "q eleven", ["DOCM"], gold_must_contain=["Needle"]), # A, must_contain
    golden("SYN-12", CANARIES["query"], [CANARIES["doc"]]),               # A, canary chunk
    golden("SYN-13", "q thirteen", ["DOCA"]),                             # not selected (pass)
]


def fillers(prefix, n):
    return [scored_point(cid(f"{prefix}-{i}"), "DOCX") for i in range(n)]


def pool(query, dense, sparse=(), kind="nl", depth=50, collection=COLL):
    legs = [{"effective_text": CANARIES["effective"], "filter_fallback": False,
             "dense": list(dense), "sparse": list(sparse)}]
    return legs_to_record(query, kind, legs, {}, {"collection": collection, "depth": depth})


def sp(name, doc="DOCX"):
    return scored_point(cid(name), doc)


POOLS = [
    pool("q one", [sp("a-other", "DOCA")] + fillers("f1", 5)),
    pool("q two", fillers("f2", 2) + [sp("b-gold", "DOCB")]),
    pool("q three", fillers("f3", 44) + [sp("b2-gold", "DOCB2")]),
    pool("q four", [sp("c-gold", "DOCC")]),
    pool("q five", [sp("a-gold", "DOCA")]),
    pool("q six", fillers("f6", 3)),
    pool("q seven", [sp("e-gold", "DOCE")], kind="identifier"),
    pool("q eight", [sp("u-gold", "DOCU")]),
    {"query": "q nine", "error": "capture failed"},
    pool("q ten", [sp("k-other", "DOCK")], sparse=fillers("f10", 6) + [sp("k-gold", "DOCK")]),
    pool("q eleven", [sp("m-miss", "DOCM")]),
    pool(CANARIES["query"], fillers("f12", 3)),
    pool("q thirteen", [sp("a-gold", "DOCA")]),
]


def hit(name):
    return {"chunk_id": cid(name), "doc_id": "DOCX", "chunk_type": "narrative", "page_label": "1",
            "pdf_page_start": 1, "pdf_page_end": 1, "score": 0.5, "rerank_score": None}


def result(row_id, final=("f-any",), repeat=1, coverage="full", **extra):
    return {"id": row_id, "repeat": repeat, "expected_behavior": "answer", "verdict": "fail",
            "coverage": coverage, "final_hits": None if final is None else [hit(n) for n in final],
            **extra}


RESULTS = [
    result("SYN-1"), result("SYN-2"), result("SYN-3"),
    result("SYN-4", final=("f-any", "c-gold")),
    result("SYN-5", coverage="none"), result("SYN-6"), result("SYN-7"),
    result("SYN-8", final=None), result("SYN-9"), result("SYN-10"), result("SYN-11"),
    result("SYN-12"), result("SYN-13"),
]


def tax_row(row_id, primary=TAXONOMY_RETRIEVAL_MISS, verdict="fail", gaps=()):
    return {"id": row_id, "verdict": verdict, "expected_behavior": "answer", "primary": primary,
            "gold_gaps": list(gaps)}


TAXONOMY = {"rows": [
    *(tax_row(f"SYN-{i}") for i in range(1, 11) if i != 4),
    # selected through a partial gold gap, whatever its primary class
    tax_row("SYN-4", primary="content_miss",
            gaps=_gold_gaps({"targets": [{"kind": "doc", "key": "DOCC", "outcome": "not_retrieved"}]})),
    tax_row("SYN-11"), tax_row("SYN-12"),
    tax_row("SYN-13", primary="pass", verdict="pass"),
    tax_row("SYN-14", primary="timeout", verdict="error"),
]}

EXPECTED_BUCKETS = {
    "SYN-1": "A", "SYN-2": "B", "SYN-3": "B", "SYN-4": "C", "SYN-5": "D", "SYN-6": "D",
    "SYN-7": "E", "SYN-8": "unknown_no_final_hits", "SYN-9": "unknown_no_pool", "SYN-10": "B",
    "SYN-11": "A", "SYN-12": "A",
}


def run_triage(client=None, results=RESULTS, reports=None, pools=POOLS, prefetch=40):
    return mt.triage(client or corpus(), COLL, prefetch, GOLDEN,
                     reports if reports is not None else [{"results": results}], TAXONOMY, pools)


def test_taxonomy_vocabulary_is_answer_taxonomys():
    assert mt.RETRIEVAL_MISS == TAXONOMY_RETRIEVAL_MISS


def test_each_selected_row_gets_its_bucket():
    rows = {r["id"]: r for r in run_triage()}
    assert {i: r["bucket"] for i, r in rows.items()} == EXPECTED_BUCKETS
    assert rows["SYN-2"]["dense_rank"] == 3 and rows["SYN-2"]["beyond_prefetch"] is False
    assert rows["SYN-3"]["dense_rank"] == 45 and rows["SYN-3"]["beyond_prefetch"] is True
    assert (rows["SYN-10"]["sparse_rank"], rows["SYN-10"]["dense_rank"]) == (7, None)
    assert rows["SYN-10"]["gold_basis"] == "chunk_ids" and rows["SYN-10"]["gold_n"] == 1
    # heading narrows a manual to one chunk; the other chunk in the pool is not gold
    assert rows["SYN-1"]["gold_ids"] == [cid("a-gold")] and rows["SYN-1"]["gold_basis"] == "doc+heading"
    assert rows["SYN-11"]["gold_ids"] == [cid("m-hit")] and rows["SYN-11"]["gold_basis"] == "doc+must_contain"
    assert rows["SYN-6"]["gold_n"] == 0


def test_final_hits_recheck_turns_a_pool_hit_into_c_and_overrides_a_shallow_label():
    # SYN-4: the taxonomy (depth-8 diagnostic) said not retrieved; the served
    # final_hits hold the gold chunk, so the answer stage lost it.
    rows = {r["id"]: r for r in run_triage()}
    assert rows["SYN-4"]["bucket"] == "C"
    # The same row without gold in final_hits but in the pool is ordering.
    results = [r if r["id"] != "SYN-4" else result("SYN-4") for r in RESULTS]
    rows = {r["id"]: r for r in run_triage(results=results)}
    assert rows["SYN-4"]["bucket"] == "B"


def test_repeat_one_only_and_split_reports():
    later = [result("SYN-1", repeat=2, final=("a-gold",)), result("SYN-2", repeat=3, coverage="none")]
    rows = {r["id"]: r for r in run_triage(reports=[{"results": RESULTS[:6] + later},
                                                    {"results": RESULTS[6:]}])}
    assert {i: r["bucket"] for i, r in rows.items()} == EXPECTED_BUCKETS


def test_a_row_twice_in_repeat_one_refuses():
    with pytest.raises(mt.TriageError, match="appears twice"):
        run_triage(reports=[{"results": RESULTS}, {"results": [result("SYN-1")]}])


def test_selected_row_missing_from_the_report_refuses():
    with pytest.raises(mt.TriageError, match="not in the answer reports: SYN-2"):
        run_triage(results=[r for r in RESULTS if r["id"] != "SYN-2"])


def test_old_report_without_final_hits_refuses_with_a_fixed_message():
    old = [{k: v for k, v in r.items() if k != "final_hits"} for r in RESULTS]
    with pytest.raises(mt.TriageError) as exc:
        run_triage(results=old)
    assert str(exc.value) == mt.NO_FINAL_HITS
    # the buffered transport (every final_hits null) is refused the same way
    with pytest.raises(mt.TriageError, match="predates #1191"):
        run_triage(results=[{**r, "final_hits": None} for r in RESULTS])


@pytest.mark.parametrize(("record", "message"), [
    (pool("q one", [], depth=None), mt.POOL_NO_DEPTH),
    (pool("q one", [], depth=20), mt.POOL_SHALLOW),
    (pool("q one", [], collection="other"), mt.POOL_COLLECTION),
])
def test_pools_that_would_fake_bucket_a_refuse(record, message):
    if record["_meta"]["depth"] is None:
        del record["_meta"]["depth"]
    with pytest.raises(mt.TriageError) as exc:
        run_triage(pools=[record, *POOLS[1:]])
    assert str(exc.value) == message


def test_served_prefetch_follows_rerank():
    assert mt.served_prefetch_depth(eval_settings()) == 40
    assert mt.served_prefetch_depth(eval_settings(rerank_enabled=True, rerank_candidates=50)) == 50
    rows = {r["id"]: r for r in run_triage(prefetch=50)}
    assert rows["SYN-3"]["beyond_prefetch"] is False


class ReadOnlyGuard:
    """Lets only scroll and retrieve through; records every call."""

    def __init__(self, client):
        self._client, self.calls = client, []

    def __getattr__(self, name):
        if name not in ("scroll", "retrieve"):
            raise AssertionError(f"non-read Qdrant call: {name}")
        self.calls.append(name)
        return getattr(self._client, name)


def test_classify_and_show_only_read():
    client = corpus()
    before = client.count(COLL).count
    guard = ReadOnlyGuard(client)
    run_triage(client=guard)
    list(mt.show_blocks(guard, COLL, [cid("a-gold"), "DOCA@6"]))
    assert set(guard.calls) == {"scroll", "retrieve"}
    assert client.count(COLL).count == before


def _cli(monkeypatch, tmp_path, capsys, results=RESULTS):
    client = corpus()
    monkeypatch.setattr(mt, "_client_and_settings",
                        lambda: (client, eval_settings(qdrant_collection=COLL)))
    files = {}
    for name, data in (("golden.jsonl", GOLDEN), ("pools.jsonl", POOLS)):
        files[name] = tmp_path / name
        files[name].write_text("".join(json.dumps(r) + "\n" for r in data), encoding="utf-8")
    for name, data in (("report.json", {"results": results}), ("taxonomy.json", TAXONOMY)):
        files[name] = tmp_path / name
        files[name].write_text(json.dumps(data), encoding="utf-8")
    code = mt.main(["classify", "--golden", str(files["golden.jsonl"]), "--pools", str(files["pools.jsonl"]),
                    "--report", str(files["report.json"]), "--taxonomy", str(files["taxonomy.json"]),
                    "--out", str(tmp_path / "triage.jsonl")])
    return code, capsys.readouterr()


READING = (
    "# id\tverdict\ttype\treason\n"
    f"SYN-1\tyes\ttable\t{CANARIES['reason']}\n"
    f"SYN-11\twrong_gold\t-\t{CANARIES['reason']}\n"
    f"SYN-12\tno\tnarrative\t{CANARIES['reason']}\n"
)


def test_cli_end_to_end_export_is_text_free(monkeypatch, tmp_path, capsys):
    code, out = _cli(monkeypatch, tmp_path, capsys)
    assert code == 1  # unknown rows present: no decision on this run
    triage_text = (tmp_path / "triage.jsonl").read_text(encoding="utf-8")
    for name, canary in CANARIES.items():
        assert canary.lower() not in (out.out + out.err).lower(), name
        # triage rows stay in the gap; even so they carry chunk ids and ranks, never text
        assert canary.lower() not in triage_text.lower(), name

    (tmp_path / "reading.tsv").write_text(READING, encoding="utf-8")
    code = mt.main(["export", "--triage", str(tmp_path / "triage.jsonl"),
                    "--reading", str(tmp_path / "reading.tsv"), "--out", str(tmp_path / "x.json")])
    out = capsys.readouterr()
    assert code == 0
    text = (tmp_path / "x.json").read_text(encoding="utf-8")
    for name, canary in CANARIES.items():
        assert canary.lower() not in (text + out.out + out.err).lower(), name
    for name, *_ in STORED:
        assert cid(name) not in text
    for doc in {s[1] for s in STORED}:
        assert doc not in text
    export = json.loads(text)
    assert export == {
        "schema": "miss_triage_export/1",
        "rows": 12,
        "served_prefetch": 40,
        "pool_depth_min": 50,
        "counts": {"A": 2, "B": 3, "C": 1, "D": 3, "E": 1, "unknown_no_final_hits": 1,
                   "unknown_no_pool": 1},
        "row_ids": {"A": ["SYN-1", "SYN-12"], "B": ["SYN-10", "SYN-2", "SYN-3"], "C": ["SYN-4"],
                    "D": ["SYN-11", "SYN-5", "SYN-6"], "E": ["SYN-7"],
                    "unknown_no_final_hits": ["SYN-8"], "unknown_no_pool": ["SYN-9"]},
        "b_beyond_prefetch": ["SYN-3"],
        "a_rows": [{"id": "SYN-1", "context_starved": "yes", "chunk_type": "table"},
                   {"id": "SYN-11", "context_starved": "wrong_gold", "chunk_type": "-"},
                   {"id": "SYN-12", "context_starved": "no", "chunk_type": "narrative"}],
        "context_starved": 1,
        "unidentifiable": 0,
        "misses_excl_d": 9,
    }


def test_cli_refuses_an_old_report(monkeypatch, tmp_path, capsys):
    old = [{k: v for k, v in r.items() if k != "final_hits"} for r in RESULTS]
    code, out = _cli(monkeypatch, tmp_path, capsys, results=old)
    assert code == 2 and out.err.strip() == f"REFUSED: {mt.NO_FINAL_HITS}"
    assert not (tmp_path / "triage.jsonl").exists()


def test_show_prints_the_embedded_text_to_the_console_only(monkeypatch, capsys):
    client = corpus()
    monkeypatch.setattr(mt, "_client_and_settings",
                        lambda: (client, eval_settings(qdrant_collection=COLL)))
    assert mt.main(["show", cid("cn-gold"), f"{CANARIES['doc']}@9", "DOCA@404"]) == 0
    out = capsys.readouterr().out
    for name in ("title", "heading", "text", "doc"):
        assert CANARIES[name] in out, name
    # build_embed_text order, unclipped: header, title, heading path, body
    assert f"z/OS 3.1 {CANARIES['doc']}\n{CANARIES['title']}\n{CANARIES['heading']}\n{CANARIES['text']}" in out
    assert out.count(f"=== {cid('cn-gold')} table p.9") == 2
    assert "=== DOCA@404: no stored chunk" in out


def test_real_client_entry_keeps_the_venue_rule_and_constructs_the_client(monkeypatch, capsys):
    # limits= is pinned src-wide by test_timeouts (#1131); this runs the real entry once.
    from mainframe_rag import config

    monkeypatch.delenv("VENUE", raising=False)
    monkeypatch.setattr(config, "load_settings", lambda: eval_settings(qdrant_collection="real_manuals"))
    assert mt.main(["show", cid("a-gold")]) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("REFUSED: collection")
    monkeypatch.setattr(config, "load_settings", lambda: eval_settings(qdrant_collection=COLL))
    client, settings = mt._client_and_settings()  # constructs only; no request is sent
    assert isinstance(client, QdrantClient) and settings.qdrant_collection == COLL


def _rows():
    return [{"id": i, "bucket": b, "served_prefetch": 40, "pool_depth": 50} for i, b in
            (("SYN-1", "A"), ("SYN-2", "B"))]


@pytest.mark.parametrize(("reading", "message"), [
    (f"SYN-1\t{CANARIES['reason']}\tyes\n", "reading line 1: verdict"),
    (f"SYN-1\tyes\t{CANARIES['reason']}\n", "reading line 1: chunk type"),
    (f"SYN-1 yes {CANARIES['reason']}\n", "reading line 1: needs"),
    ("SYN-1\tyes\ttable\n\nSYN-1\tno\ttable\n", "reading line 3: a row read twice"),
])
def test_reading_errors_name_the_line_never_its_content(reading, message):
    with pytest.raises(mt.TriageError) as exc:
        mt.parse_reading(reading.splitlines(keepends=True))
    assert str(exc.value).startswith(message) and CANARIES["reason"] not in str(exc.value)


def test_export_refuses_unread_extra_and_odd_rows():
    with pytest.raises(mt.TriageError, match="without a reading: SYN-1"):
        mt.build_export(_rows(), {})
    with pytest.raises(mt.TriageError, match="1 reading line"):
        mt.build_export(_rows(), {"SYN-1": ("yes", "table"), f"{CANARIES['query']}": ("no", "-")})
    with pytest.raises(mt.TriageError) as exc:
        mt.build_export([{"id": f"bad id {CANARIES['query']}", "bucket": "A"}], {})
    assert CANARIES["query"] not in str(exc.value)
    with pytest.raises(mt.TriageError, match="unknown bucket"):
        mt.build_export([{"id": "SYN-1", "bucket": CANARIES["text"]}], {})


def test_export_never_copies_unlisted_row_fields():
    rows = [{**_rows()[0], "gold_ids": [cid("a-gold")], "query": CANARIES["query"],
             "heading": CANARIES["heading"], "coverage": CANARIES["text"]}]
    text = json.dumps(mt.build_export(rows, {"SYN-1": ("unidentifiable", "message")}))
    for canary in (cid("a-gold"), CANARIES["query"], CANARIES["heading"], CANARIES["text"]):
        assert canary not in text
