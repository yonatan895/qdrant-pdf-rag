"""Retrieval-miss triage (issue #1383): where does each retrieval miss lose
its gold chunk, and does the gold chunk name its own subject?

Read-only. Three steps, run where the inputs live:

  classify  inside the agent pod (served settings, read-only Qdrant key):
            joins the answer report(s), the failure taxonomy, a depth-50
            capture pool (scripts/capture_pool.py) and the golden set, and
            puts every selected row in one bucket. Writes triage rows (chunk
            ids and ranks; they stay in the gap).
  show      inside the agent pod: prints stored chunks (the text that is
            embedded) to the console for the reader. Never to a file.
  export    anywhere: the triage rows plus the reader's verdicts reduced to
            an allowlist (row ids, bucket letters, counts, closed-set
            verdicts and chunk types). No question, chunk text, heading,
            doc id or chunk id leaves.

Selected rows: answer rows the taxonomy (scripts/answer_taxonomy.py) judged
failing with primary class retrieval_miss or a "=not_retrieved" gold gap,
repeat 1 only. Row-level buckets come first:

  D  corpus/label: the row's coverage is "none", or no stored chunk is gold
  E  identifier: the captured query_kind is "identifier"
  unknown_no_final_hits: the row has no final_hits (buffered transport or a
     malformed hit list); fix the input and rerun
  unknown_no_pool: no pool record for the row's query (or a capture error)

Otherwise each missing target (a not_retrieved gold gap; the whole row when
there is none) is bucketed on its own gold chunks (a doc:X target on doc X
only), and the row takes the most upstream target bucket:

  A  recall: no target gold chunk in any leg of the pool
  B  ordering: in a dense or sparse leg of the pool but not in final_hits
     (beyond_prefetch: its best rank is past the served prefetch)
  C  answer stage: a target gold chunk is in the served final_hits
  D  only when no missing target has a stored gold chunk

Gold is the row's gold_chunk_ids when set; otherwise every stored chunk of
expected_doc_ids that is_relevant_hit accepts (heading substring), on
expected_page when set, narrowed by gold_must_contain when set (any string in
the chunk text: a heuristic, recorded in gold_basis).

    python -m mainframe_rag.eval.miss_triage classify --golden G \
        --pools pools-50.jsonl --report report.json --taxonomy taxonomy.json \
        --out triage.jsonl
    python -m mainframe_rag.eval.miss_triage show CHUNK_ID DOC_ID@PAGE_LABEL
    python -m mainframe_rag.eval.miss_triage export --triage triage.jsonl \
        --reading reading.tsv --out triage-export.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from mainframe_rag.ingest.classify import ChunkType

if TYPE_CHECKING:  # the export step stays importable without qdrant_client
    from mainframe_rag.ports import QdrantReader

EXPORT_SCHEMA = "miss_triage_export/1"
BUCKETS = ("A", "B", "C", "D", "E", "unknown_no_final_hits", "unknown_no_pool")
UNKNOWN_BUCKETS = frozenset(b for b in BUCKETS if b.startswith("unknown_"))
VERDICTS = ("yes", "no", "unidentifiable", "wrong_gold")
CHUNK_TYPES = (*get_args(ChunkType), "-")
# answer_taxonomy.py's primary class for a retrieval miss (pinned by a test).
RETRIEVAL_MISS = "retrieval_miss"
GOLD_IDS_KEPT = 20  # gold candidates a triage row lists for the reader
NOT_RETRIEVED = "not_retrieved"  # packing_diagnostic's target outcome (pinned by a test)
ROW_TARGET = "row"  # the whole row's gold, for a miss with no not_retrieved gap
# Target buckets, most upstream first; a row takes the first one any of its
# missing targets has. "D" here is a target with no stored gold chunk.
TARGET_ORDER = ("A", "B", "C", "D")
SCROLL_PAGE = 256
# Payload fields the tool reads: the embedded text's parts plus locators.
PAYLOAD_KEYS = ["product", "version", "doc_id", "title", "heading_path", "context", "text",
                "page_label", "page_start", "page_end", "chunk_type"]
_ROW_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# Fixed refusal messages: they never echo report, pool or golden content.
NO_FINAL_HITS = ("no selected answer row carries final_hits: the report predates #1191 or was "
                 "run without --stream; rerun eval.answers with --stream")
REPEAT_DUPLICATE = "a row id appears twice in repeat 1 across the answer reports"
POOL_NO_DEPTH = "a pool record has no _meta.depth: recapture with capture_pool.py --depth 50"
POOL_SHALLOW = "a pool record is shallower than the served prefetch: recapture with a deeper --depth"
POOL_COLLECTION = "a pool record was captured from another collection than the served one"


class TriageError(RuntimeError):
    """A refused input. The message is fixed text plus row ids at most."""


# ---------------------------------------------------------------- inputs
def load_json(path: Path | str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TriageError(f"{Path(path).name}: not a JSON object")
    return data


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def missing_targets(gold_gaps: Iterable[Any]) -> list[str]:
    """The diagnostic targets a row's gold_gaps report as never retrieved
    (`kind:key`, e.g. `doc:SC27-1234-05`), sorted. answer_taxonomy writes a
    gap as `kind:key=outcome`; retrieved_dropped gaps are packing, not misses."""
    return sorted({str(g).rsplit("=", 1)[0] for g in gold_gaps
                   if str(g).endswith("=" + NOT_RETRIEVED)})


def select_rows(taxonomy: dict[str, Any]) -> dict[str, list[str]]:
    """Failing answer rows the taxonomy calls a retrieval miss, or with a gold
    target never retrieved (a partial miss): row id -> missing targets. A row
    with no not_retrieved gap (the served zero-hits path) gets the whole-row
    target ROW_TARGET."""
    rows = taxonomy.get("rows")
    if not isinstance(rows, list):
        raise TriageError("taxonomy report has no rows list")
    picked: dict[str, list[str]] = {}
    for r in rows:
        if r.get("verdict") != "fail" or r.get("expected_behavior") != "answer":
            continue
        targets = missing_targets(r.get("gold_gaps") or [])
        if r.get("primary") == RETRIEVAL_MISS or targets:
            picked[str(r["id"])] = targets or [ROW_TARGET]
    return dict(sorted(picked.items()))


def repeat_one_rows(reports: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Row id -> result row, repeat 1 only, across reports split by session.
    A row id twice in repeat 1 refuses: each row is measured once."""
    out: dict[str, dict[str, Any]] = {}
    for report in reports:
        results = report.get("results")
        if not isinstance(results, list):
            raise TriageError("an answer report has no results list")
        for row in results:
            repeat = row.get("repeat")
            if repeat is not None and repeat != 1:
                continue
            row_id = str(row.get("id") or "")
            if row_id in out:
                raise TriageError(REPEAT_DUPLICATE)
            out[row_id] = row
    return out


def pool_index(records: Iterable[dict[str, Any]], collection: str,
               served_prefetch: int) -> dict[str, dict[str, Any]]:
    """Query text -> pool record. Error records (no legs) are skipped, so
    their rows land in unknown_no_pool. A record without a depth, shallower
    than the served prefetch, or from another collection refuses: it would
    turn every miss into bucket A."""
    out: dict[str, dict[str, Any]] = {}
    for rec in records:
        if "legs" not in rec:
            continue
        meta = rec.get("_meta") or {}
        depth = meta.get("depth")
        if type(depth) is not int:
            raise TriageError(POOL_NO_DEPTH)
        if depth < served_prefetch:
            raise TriageError(POOL_SHALLOW)
        if meta.get("collection") != collection:
            raise TriageError(POOL_COLLECTION)
        out.setdefault(str(rec["query"]), rec)
    return out


# ---------------------------------------------------------------- gold
def doc_points(client: QdrantReader, collection: str, doc_ids: Sequence[str]) -> Iterator[Any]:
    """Every stored point of these doc ids (doc_id is a keyword-indexed field)."""
    from qdrant_client import models

    flt = models.Filter(must=[models.FieldCondition(
        key="doc_id", match=models.MatchAny(any=list(doc_ids)))])
    offset = None
    while True:
        points, offset = client.scroll(
            collection, scroll_filter=flt, limit=SCROLL_PAGE, offset=offset,
            with_vectors=False, with_payload=PAYLOAD_KEYS)
        yield from points
        if offset is None:
            return


def gold_set(client: QdrantReader, collection: str,
             record: dict[str, Any]) -> tuple[dict[str, str], str]:
    """(gold chunk id -> its doc id, basis) for one golden record."""
    from pydantic import ValidationError

    from mainframe_rag.eval.datasets import GoldenEntry
    from mainframe_rag.eval.retrieval import is_relevant_hit
    from mainframe_rag.retrieve.query import hit_from_payload

    try:
        entry = GoldenEntry.model_validate(record)
    except ValidationError:
        # Fixed text: a validation message would echo the row's question.
        row_id = record.get("id")
        shown = row_id if isinstance(row_id, str) and _ROW_ID_RE.fullmatch(row_id) else "?"
        raise TriageError(f"golden row {shown} is not a valid golden entry") from None
    if entry.gold_chunk_ids:
        points = client.retrieve(collection, ids=list(entry.gold_chunk_ids),
                                 with_payload=["doc_id"], with_vectors=False)
        return ({str(p.id): str((p.payload or {}).get("doc_id") or "") for p in points},
                "chunk_ids")
    page = entry.expected_page or ""
    must = [str(s).lower() for s in record.get("gold_must_contain") or []]
    basis = "+".join(["doc"] + [name for name, on in (
        ("heading", bool(entry.expected_heading)), ("page", bool(page)),
        ("must_contain", bool(must))) if on])
    gold: dict[str, str] = {}
    for point in doc_points(client, collection, entry.expected_doc_ids):
        hit = hit_from_payload(str(point.id), point.payload or {}, 0.0)
        if not is_relevant_hit(hit.doc_id, hit.heading, entry):
            continue
        if page and hit.page_label != page:
            continue
        if must and not any(m in hit.text.lower() for m in must):
            continue
        gold[hit.chunk_id] = hit.doc_id
    return gold, basis


# ---------------------------------------------------------------- buckets
def leg_ranks(record: dict[str, Any], chunk_ids: Iterable[str]) -> dict[str, int]:
    """Best 1-based rank of any of these chunks per side, across every leg."""
    wanted = set(chunk_ids)
    ranks: dict[str, int] = {}
    for leg in record["legs"]:
        for side in ("dense", "sparse"):
            for rank, cid in enumerate(leg[side], 1):
                if cid in wanted and rank < ranks.get(side, rank + 1):
                    ranks[side] = rank
    return ranks


def target_gold(target: str, gold: dict[str, str]) -> set[str]:
    """The gold chunks one missing target stands for: a `doc:X` target is the
    row's gold chunks in doc X; any other target (the whole row, a heading or
    an identifier entry) is the row's whole gold set."""
    if target.startswith("doc:"):
        doc = target.removeprefix("doc:")
        return {cid for cid, d in gold.items() if d == doc}
    return set(gold)


def classify_row(row_id: str, row: dict[str, Any], record: dict[str, Any],
                 gold: dict[str, str], basis: str, served_prefetch: int,
                 targets: Sequence[str] = (ROW_TARGET,)) -> dict[str, Any]:
    """One selected row's bucket (pure). Row level first: D (coverage none or
    no stored gold), E (identifier query), unknown_no_final_hits. Otherwise
    each missing target is bucketed on its own gold (C in final_hits, B in a
    pool leg, A in neither, D nothing stored) and the row takes the most
    upstream target bucket: A before B before C before D."""
    final = row.get("final_hits")
    out: dict[str, Any] = {
        "id": row_id, "query_kind": record.get("query_kind"),
        "pool_depth": record["_meta"]["depth"], "served_prefetch": served_prefetch,
        "gold_basis": basis, "gold_n": len(gold), "coverage": row.get("coverage"),
        "missing_n": len(targets),
    }
    if row.get("coverage") == "none" or not gold:
        out["bucket"] = "D"
        return out
    if record.get("query_kind") == "identifier":
        out["bucket"] = "E"
        return out
    if not isinstance(final, list):
        out["bucket"] = "unknown_no_final_hits"
        return out
    served = {h.get("chunk_id") for h in final if isinstance(h, dict)}
    per: list[dict[str, Any]] = []
    for target in targets:
        chunks = target_gold(target, gold)
        ranks = leg_ranks(record, chunks)
        bucket = ("D" if not chunks else "C" if chunks & served else "B" if ranks else "A")
        per.append({"target": target, "bucket": bucket, "gold_n": len(chunks),
                    "dense_rank": ranks.get("dense"), "sparse_rank": ranks.get("sparse"),
                    "_chunks": chunks})
    bucket = min((p["bucket"] for p in per), key=TARGET_ORDER.index)
    deciding = [p for p in per if p["bucket"] == bucket]
    for side in ("dense_rank", "sparse_rank"):
        found = [p[side] for p in deciding if p[side] is not None]
        out[side] = min(found) if found else None
    if bucket == "B":
        # Past the served prefetch when some missing target's best rank is.
        out["beyond_prefetch"] = any(
            min(r for r in (p["dense_rank"], p["sparse_rank"]) if r is not None) > served_prefetch
            for p in deciding)
    if bucket == "A":
        out["gold_ids"] = sorted(set().union(*(p["_chunks"] for p in deciding)))[:GOLD_IDS_KEPT]
    # A partial row: an expected doc that is not a missing target was served
    # (comparative rows, and rows whose expected docs are alternatives).
    missing_docs = {t.removeprefix("doc:") for t in targets if t.startswith("doc:")}
    out["partial"] = bool(missing_docs) and any(
        cid in served for cid, d in gold.items() if d not in missing_docs)
    out["targets"] = [{k: v for k, v in p.items() if k != "_chunks"} for p in per]
    out["bucket"] = bucket
    return out


def triage(client: QdrantReader, collection: str, served_prefetch: int,
           golden: Sequence[dict[str, Any]], reports: Sequence[dict[str, Any]],
           taxonomy: dict[str, Any], pools: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every selected row exactly once, in id order."""
    targets = select_rows(taxonomy)
    selected = list(targets)
    rows = repeat_one_rows(reports)
    entries = {str(e.get("id")): e for e in golden}
    missing = [i for i in selected if i not in rows]
    if missing:
        raise TriageError(f"rows selected by the taxonomy are not in the answer reports: {', '.join(missing)}")
    unknown = [i for i in selected if i not in entries]
    if unknown:
        raise TriageError(f"rows selected by the taxonomy are not in the golden set: {', '.join(unknown)}")
    if selected and not any(isinstance(rows[i].get("final_hits"), list) for i in selected):
        raise TriageError(NO_FINAL_HITS)
    index = pool_index(pools, collection, served_prefetch)
    out = []
    for row_id in selected:
        record = index.get(str(entries[row_id].get("query")))
        if record is None:
            out.append({"id": row_id, "bucket": "unknown_no_pool"})
            continue
        gold, basis = gold_set(client, collection, entries[row_id])
        out.append(classify_row(row_id, rows[row_id], record, gold, basis, served_prefetch,
                                targets[row_id]))
    return out


def served_prefetch_depth(settings: Any) -> int:
    """The served per-leg prefetch for a natural-language query: the rerank
    pool when rerank is on, else the fixed prefetch (retrieve/query.py)."""
    from mainframe_rag.retrieve.query import _prefetch_limit_for

    return _prefetch_limit_for(settings, bool(settings.rerank_enabled))


# ---------------------------------------------------------------- reader
def show_blocks(client: QdrantReader, collection: str, args: Sequence[str]) -> Iterator[str]:
    """Stored chunks as the reader judges them: the embedded string
    (build_embed_text over the stored payload, unclipped). A plain argument
    is a chunk id; DOC_ID@PAGE_LABEL is every chunk on that printed page."""
    from mainframe_rag.ingest.embed import build_embed_text

    for arg in args:
        if "@" in arg:
            doc, page = arg.split("@", 1)
            points = [p for p in doc_points(client, collection, [doc])
                      if str((p.payload or {}).get("page_label") or "") == page]
        else:
            points = client.retrieve(collection, ids=[arg], with_payload=PAYLOAD_KEYS,
                                     with_vectors=False)
        if not points:
            yield f"=== {arg}: no stored chunk\n"
        for p in points:
            pl = p.payload or {}
            embedded = build_embed_text(
                pl.get("product"), pl.get("version"), str(pl.get("doc_id") or ""),
                str(pl.get("title") or ""), str(pl.get("heading_path") or ""),
                str(pl.get("text") or ""), context=pl.get("context"))
            yield (f"=== {p.id} {pl.get('chunk_type') or 'narrative'} "
                   f"p.{pl.get('page_label') or '-'}\n{embedded}\n")


# ---------------------------------------------------------------- export
def parse_reading(lines: Iterable[str]) -> dict[str, tuple[str, str]]:
    """reading.tsv: row id, verdict, chunk type, reason (the reason stays in
    the gap). Errors name the line number, never its content."""
    out: dict[str, tuple[str, str]] = {}
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 3:
            raise TriageError(f"reading line {n}: needs id, verdict and chunk type (tab-separated)")
        row_id, verdict, ctype = fields[0].strip(), fields[1].strip(), fields[2].strip()
        if verdict not in VERDICTS:
            raise TriageError(f"reading line {n}: verdict must be one of {', '.join(VERDICTS)}")
        if ctype not in CHUNK_TYPES:
            raise TriageError(f"reading line {n}: chunk type must be one of {', '.join(CHUNK_TYPES)}")
        if row_id in out:
            raise TriageError(f"reading line {n}: a row read twice")
        out[row_id] = (verdict, ctype)
    return out


def build_export(rows: Sequence[dict[str, Any]], reading: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """The text-free export. Every value is copied by an explicit rule:
    row ids only in the golden id shape, buckets, verdicts and chunk types
    from closed sets, integers. A reader's wrong_gold moves an A row to D."""
    ids: dict[str, list[str]] = {b: [] for b in BUCKETS}
    beyond: list[str] = []
    partial: list[str] = []
    missing_n: dict[str, int] = {}
    prefetch: set[int] = set()
    depths: list[int] = []
    seen: set[str] = set()
    for n, row in enumerate(rows, 1):
        row_id, bucket = row.get("id"), row.get("bucket")
        if not isinstance(row_id, str) or not _ROW_ID_RE.fullmatch(row_id):
            raise TriageError(f"triage row {n}: row id has an unexpected shape")
        if bucket not in ids:
            raise TriageError(f"triage row {n}: unknown bucket")
        if row_id in seen:
            raise TriageError(f"triage row {n}: row {row_id} appears twice (one classify run per export)")
        seen.add(row_id)
        if type(row.get("missing_n")) is int and row["missing_n"] > 0:
            missing_n[row_id] = row["missing_n"]
        if row.get("partial") is True:
            partial.append(row_id)
        if bucket == "A" and reading.get(row_id, ("",))[0] == "wrong_gold":
            bucket = "D"
        ids[bucket].append(row_id)
        if bucket == "B" and row.get("beyond_prefetch") is True:
            beyond.append(row_id)
        for key, into in (("served_prefetch", prefetch), ("pool_depth", None)):
            value = row.get(key)
            if type(value) is int and value > 0:
                if into is None:
                    depths.append(value)
                else:
                    into.add(value)
    if len(prefetch) > 1:
        raise TriageError("triage rows disagree on the served prefetch: classify once per run")
    a_rows = sorted(i for i in (r.get("id") for r in rows if r.get("bucket") == "A")
                    if isinstance(i, str))
    unread = [i for i in a_rows if i not in reading]
    if unread:
        raise TriageError(f"bucket A rows without a reading: {', '.join(unread)}")
    extra = len(set(reading) - set(a_rows))
    if extra:
        raise TriageError(f"{extra} reading line(s) name a row that is not in bucket A")
    kept_a = [i for i in a_rows if reading[i][0] != "wrong_gold"]
    return {
        "schema": EXPORT_SCHEMA,
        "rows": sum(len(v) for v in ids.values()),
        "served_prefetch": next(iter(prefetch), None),
        "pool_depth_min": min(depths) if depths else None,
        "counts": {b: len(v) for b, v in ids.items()},
        "row_ids": {b: sorted(v) for b, v in ids.items()},
        "b_beyond_prefetch": sorted(beyond),
        "missing_targets_n": dict(sorted(missing_n.items())),
        "partial_rows": sorted(partial),
        "a_rows": [{"id": i, "context_starved": reading[i][0], "chunk_type": reading[i][1]}
                   for i in a_rows],
        "context_starved": sum(reading[i][0] == "yes" for i in kept_a),
        "unidentifiable": sum(reading[i][0] == "unidentifiable" for i in kept_a),
        "misses_excl_d": sum(len(v) for b, v in ids.items() if b != "D"),
    }


# ---------------------------------------------------------------- CLI
def _client_and_settings() -> tuple[Any, Any]:
    from qdrant_client import QdrantClient

    from mainframe_rag.config import load_settings, qdrant_pool_limits
    from mainframe_rag.eval.datasets import require_rc_for_collection

    settings = load_settings()
    # Venue rule (issue #268): the real-corpus collection is RC-only. In the
    # agent pod the configured key is the read-only serving key.
    require_rc_for_collection(settings.qdrant_collection)
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key,
                          timeout=settings.qdrant_timeout_s,
                          limits=qdrant_pool_limits(settings.qdrant_url))
    return client, settings


def _classify(args: argparse.Namespace) -> int:
    from mainframe_rag.eval.datasets import load_golden_records

    golden = load_golden_records([args.golden])
    reports = [load_json(p) for p in args.report]
    taxonomy = load_json(args.taxonomy)
    pools = read_jsonl(args.pools)
    client, settings = _client_and_settings()
    rows = triage(client, settings.qdrant_collection, served_prefetch_depth(settings),
                  golden, reports, taxonomy, pools)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row) + "\n" for row in rows)
    counts = {b: sum(r["bucket"] == b for r in rows) for b in BUCKETS}
    print(json.dumps({"rows": len(rows), "counts": counts}))
    return 1 if any(counts[b] for b in UNKNOWN_BUCKETS) else 0


def _show(args: argparse.Namespace) -> int:
    client, settings = _client_and_settings()
    for block in show_blocks(client, settings.qdrant_collection, args.chunk):
        print(block)
    return 0


def _export(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.triage)
    with open(args.reading, encoding="utf-8") as fh:
        reading = parse_reading(fh)
    export = build_export(rows, reading)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(export, indent=1, sort_keys=True, ensure_ascii=True) + "\n",
                        encoding="utf-8")
    print(json.dumps({"counts": export["counts"], "context_starved": export["context_starved"],
                      "misses_excl_d": export["misses_excl_d"]}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mainframe_rag.eval.miss_triage",
        description="Read-only retrieval-miss triage (issue #1383); see docs/eval.md.")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("classify", help="bucket every selected row (agent pod; read-only Qdrant)")
    c.add_argument("--golden", required=True, help="the golden jsonl the answer run used")
    c.add_argument("--pools", required=True, type=Path, help="capture_pool.py output (depth 50)")
    c.add_argument("--report", required=True, type=Path, action="append",
                   help="full eval.answers --out report; repeat for a run split across sessions")
    c.add_argument("--taxonomy", required=True, type=Path, help="answer_taxonomy.py --out report")
    c.add_argument("--out", required=True, type=Path, help="triage rows (jsonl; stays in the gap)")
    s = sub.add_parser("show", help="print stored chunks to this console (stays in the gap)")
    s.add_argument("chunk", nargs="+", help="chunk id, or DOC_ID@PAGE_LABEL")
    e = sub.add_parser("export", help="text-free export of the triage and the reading")
    e.add_argument("--triage", required=True, type=Path)
    e.add_argument("--reading", required=True, type=Path)
    e.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    from mainframe_rag.eval.datasets import DatasetError

    run = {"classify": _classify, "show": _show, "export": _export}[args.command]
    try:
        return run(args)
    except (TriageError, DatasetError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
