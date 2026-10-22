# Packing diagnostic (record)

Status: dated record for issue #811 phase 0, 2026-10-17. Tool: [`scripts/packing_diagnostic.py`](../../scripts/packing_diagnostic.py)
(tests: `tests/test_packing_diagnostic.py`). Base: `main` `a3b3b20`. Owner of the packing rules it measures:
[answer contract](../agent.md#answer-contract). This is a measurement, not an acceptance run: no LLM, no answers, estimator-only
token counting. It holds counts, ranks and identifiers only (no manual or chunk text).

## Question

Are gold units lost because the answer-path excerpt planner (`_plan_packed_excerpts`, the cross-hit budget `break`) drops
retrieved hits, or are they never retrieved? A packing loss justifies phase 1 (coverage-first, skip-and-continue packing);
a retrieval miss belongs to retrieval.

## Corpus and embedding identity (read this first)

**This is not the corpus the plan named.** The plan assumed `manuals_current` (physical
`manuals_current__gen272b3c2e8474ca3b1fd5bc562d42`, 234,723 points, ingested at `ac4e4b3`). The Kind cluster `rag-kind` Qdrant, the only
corpus reachable in the lab, holds a different and older one, and has no `manuals_current` collection:

| Item | Value |
|---|---|
| Qdrant | StatefulSet `qdrant`, namespace `rag-kind` (scaled to 1 for the run, back to 0 after), through `scripts/kube_forward.py`, read-only key |
| Collection | alias `mainframe_manuals` -> `mainframe_manuals__gendbb64cc17c6e610274510d4bed06`, 431,233 points |
| Vectors | dense 1024 (cosine, int8 scalar quantization), sparse `bm25` |
| Stored extraction rules | payload `rules_v` `c1862af03b39cfca` (September 2026): **older than the current rules** |
| Persisted unit spans | absent on 348,029 of 431,233 points; the planner redetects units with the shared detectors for those |
| Code-entry chunks | none: the SYSCODE family and code-entry extraction (#603 to #629) postdate this corpus |
| Embedding | `Qwen/Qwen3-Embedding-0.6B`, revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, dense dim 1024, vLLM v0.28.0 pooling mode (`--enforce-eager`, GPU utilization 0.33, container `--memory=4g --memory-swap=4g`) |
| Sparse | local FastEmbed `Qdrant/bm25`, weights `bundles/bm25-weights` |
| Retrieval | `retrieve.query.search`, limit 8 (the answer path's depth), reranking off; `evals/golden.jsonl` rows CMP 11, TBL 4, SYS 6, plus MSG 24 as a control; `VENUE=rc` for the real-corpus collection |
| Serving gate | **not servable**: outcome `reembed_required`, detail `extraction_rules`. Recorded, not bypassed: the tool calls retrieval and the planner directly against the configured alias |

Consequences. The stored representation is stale for chunk content, so chunk boundaries, unit spans and the presence of code-entry chunks
differ from what current rules produce. Packing behavior (budget arithmetic, the per-chunk cap, the cross-hit `break`) does not depend on
those details, so the packing result is valid for the planner. Per-row *retrieval* outcomes, and every code row, are specific to this
corpus and must not be read as the current corpus. The raw JSON (chunk ids and offsets, no text) is kept outside git at
`~/.local/state/mainframe-rag/packing-diagnostic-2026-10-17/run3.json`.

## Method

Per row: real retrieval, then `answer_core._build_prepared_prompt` (the one prompt-build owner of the JSON, stream and chat surfaces) with
an estimator-only tokenizer, spying on `_plan_packed_excerpts`. Packed ranges come from the final prompt manifest. A hit absent from the
prompt gets a reason by re-running the same planner on that hit alone with an unbounded budget: no result means no unit fits the per-chunk
cap (`no_unit_fits_cap`); a result means the cross-hit budget `break` cut it (`budget_break`). A verification trim after planning would be
`trimmed_after_plan` (none observed). Production verification counts tokens with the model server's tokenizer and could trim more than the
estimator; this run cannot see that.

Gold targets, at the level the row can name:

- **heading** (TBL, rows with `expected_heading`): a gold-document hit whose heading matches; packed means such a hit is in the prompt.
- **doc** (CMP, and rows without identifiers): one per expected document; packed means any hit of that document is in the prompt.
- **entry-heading / entry-mention / entry-none** (a requested message id, member or system code): a gold-document hit with a unit headed by
  the identifier (`_entry_anchors`), else one that names it anywhere (older rules), else none. Packed means the packed range covers that text.

A target is `retrieved_dropped` (packing loss) when carriers were retrieved but none is in the prompt, `not_retrieved` when no carrier was
retrieved, else `retrieved_packed`. A dropped target lists which mechanism would have put it in the prompt: `skip_and_continue` (a
budget-dropped carrier fits the budget left after the real plan), `coverage_pass` (an identifier carrier fits an empty budget, so packing it
first works), or `prioritize_doc_only` (the same for a document or heading target, which the #811 design does not cover). Row category
priority: `packing_loss`, `partial_retrieval` (some target retrieved and packed, another never retrieved), `not_retrieved`, `ok`.

## Planner profiles

The same retrieved hits are packed under three planner settings: the `main` defaults (`target`), a 16384-token window with the default
reserves (`w16384`), and the local lane from `scripts/local_lane_budget.sh` (`local-lane`). Budget is what the planner receives for excerpt
bodies after the fixed preamble.

| Profile | Window | Reserved output | Thinking reserve (simple / complex) | Chunk char cap | Budget (tokens) |
|---|---:|---:|---|---|---:|
| `target` | 32768 | 2560 | 8192 / 8192 | 4000 | 21,092 to 21,399 |
| `w16384` | 16384 | 2560 | 8192 / 8192 | 4000 | 4,708 to 5,015 |
| `local-lane` | 4096 | 1536 | 0 / 1000 | 3000 (1100 narrative, complex) | 636 to 1,943 |

## Class summary

Hits packed / hits retrieved, over the 45 selected rows:

| Profile | CMP (11 rows) | TBL (4) | SYS (6) | MSG control (24) |
|---|---|---|---|---|
| `target` | 88 / 88 | 32 / 32 | 48 / 48 | 182 / 182 |
| `w16384` | 87 / 88 | 32 / 32 | 47 / 48 | 179 / 182 |
| `local-lane` | 31 / 88 | 18 / 32 | 18 / 48 | 86 / 182 |

At `target` the largest planned prompt uses 29% of the budget (CMP 25%, TBL 26%, SYS 26%, MSG 29%): eight hits capped at 4000 characters
stay far below a 21k-token budget, so the cross-hit `break` never fires on these rows. (Not a hard bound: only token-dense text with a long
history or Splunk block could approach it.) At `w16384` the planned prompt reaches 98% to 100% of the budget and five
hits are cut, and no gold target is lost.

Row categories and gold-target outcomes (targets: retrieved and packed / retrieved but dropped / never retrieved):

| Class | Profile | Rows | ok | packing_loss | partial_retrieval | not_retrieved | Gold targets |
|---|---|---:|---:|---:|---:|---:|---|
| CMP | `target` | 11 | 8 | 0 | 3 | 0 | 17 / 0 / 3 |
| CMP | `w16384` | 11 | 8 | 0 | 3 | 0 | 17 / 0 / 3 |
| CMP | `local-lane` | 11 | 5 | 6 | 0 | 0 | 9 / 8 / 3 |
| TBL | `target` | 4 | 4 | 0 | 0 | 0 | 4 / 0 / 0 |
| TBL | `w16384` | 4 | 4 | 0 | 0 | 0 | 4 / 0 / 0 |
| TBL | `local-lane` | 4 | 3 | 1 | 0 | 0 | 3 / 1 / 0 |
| SYS (code) | all three | 6 | 0 | 0 | 0 | 6 | 0 / 0 / 6 |
| MSG (control) | `target` | 24 | 22 | 0 | 0 | 2 | 22 / 0 / 2 |
| MSG (control) | `w16384` | 24 | 22 | 0 | 0 | 2 | 22 / 0 / 2 |
| MSG (control) | `local-lane` | 24 | 19 | 3 | 0 | 2 | 19 / 3 / 2 |

Reading the tables:

- **Production-sized budget (`target`, `w16384`): no gold target is retrieved but dropped in any CMP, TBL, SYS or MSG row.** The CMP rows
  that do not reach `ok` there are retrieval gaps: CMP-01 (`SA32-0992-02`) and CMP-05 (`SC34-2662-05`) never retrieve one of their two
  expected documents, and CMP-17 never retrieves a hit naming `MPFLSTxx` in the expected document. Whenever both comparison sides were
  retrieved, both are in the prompt.
- **Local lane (4096 window): packing loss is real.** Complex queries leave about 640 tokens, so 2 to 3 of 8 hits are packed and the rest
  are cut by the `break` (`budget_break` is the only drop reason; `no_unit_fits_cap` and `trimmed_after_plan` never occur). Rows with a
  lost target: CMP-01, 02, 05, 06, 12, 17; TBL-05; MSG-06, 12, 37. The dropped gold carriers sit at ranks 4 to 8 (MSG-37 also has a rank 3 carrier that is packed but cut before the mention). Of the 12 dropped targets,
  `coverage_pass` would recover 4 (CMP-17 `MSGFLDxx`; MSG-06, MSG-12, MSG-37) and `skip_and_continue` recovers none (the leftover is 36 to
  249 tokens against 142 to 741 for the carriers). The other 8 (7 CMP documents and TBL-05) fit only if packed first, which the #811
  design does not do for documents or headings.
- **Code rows (SYS-02, 03, 04, 07, 08, 09) cannot be diagnosed on this corpus.** The expected document `SA38-0665-03` is retrieved in 3 of 6
  rows (SYS-02 rank 5, SYS-08 rank 4, SYS-09 ranks 3, 7, 8), and no retrieved gold-document hit names the requested code. The corpus has no
  code-entry chunks, so this is retrieval or representation (stale rules), not packing. The #591 question (did 806 and AFB lose their entry
  unit to packing) stays open until a rerun on a current-rules corpus.
  *Resolved 2026-10-19:* on the current-rules corpus all six code rows are retrieved and packed with 0 drops
  ([code-row diagnostic](2026-10-19-code-row-diagnostic.md), #826).

## Per-row tables (CMP, TBL, SYS)

`Cx` is the query complexity the planner uses (`s` simple, `c` complex). `Gold carrier ranks` are the ranks of all hits in an expected
document (document level). Targets read `kind:key=outcome`; `[...]` after a dropped target names the mechanism that would recover it. The
MSG control rows are summarized above and kept in the raw JSON; every `w16384` row has the same category as `target`.

#### Profile `target`

| Row | Cx | Packed / dropped hits | Dropped ranks | Gold carrier ranks | Targets (kind:key=outcome) | Row category |
|---|---|---|---|---|---|---|
| CMP-01 | c | 8 / 0 | - | 5 | doc:SA32-0991-02=retrieved_packed; doc:SA32-0992-02=not_retrieved | partial_retrieval |
| CMP-02 | c | 8 / 0 | - | 2,4-8 | doc:SA23-1380-09=retrieved_packed; doc:SA38-0667-09=retrieved_packed; entry-mention:SMFPRMxx=retrieved_packed | ok |
| CMP-05 | c | 8 / 0 | - | 7 | doc:SC34-2662-05=not_retrieved; doc:SC34-2663-04=retrieved_packed | partial_retrieval |
| CMP-06 | c | 8 / 0 | - | 5,7-8 | doc:SA23-2287-03=retrieved_packed; doc:SA23-2288-03=retrieved_packed | ok |
| CMP-07 | c | 8 / 0 | - | 2,6-7 | doc:SC23-6871-04=retrieved_packed | ok |
| CMP-10 | c | 8 / 0 | - | 3 | doc:SA23-2292-03=retrieved_packed | ok |
| CMP-11 | c | 8 / 0 | - | 2,6-7 | doc:SC34-2662-05=retrieved_packed | ok |
| CMP-12 | c | 8 / 0 | - | 1,3,5-7 | doc:SA23-1380-09=retrieved_packed; doc:SA38-0667-09=retrieved_packed; entry-mention:SMFPRMxx=retrieved_packed | ok |
| CMP-15 | c | 8 / 0 | - | 1-2,5 | doc:SA32-0975-01=retrieved_packed | ok |
| CMP-16 | c | 8 / 0 | - | 1-8 | doc:GC35-0033-41=retrieved_packed | ok |
| CMP-17 | c | 8 / 0 | - | 5 | doc:SA23-1380-09=retrieved_packed; entry-none:MPFLSTxx=not_retrieved; entry-mention:MSGFLDxx=retrieved_packed | partial_retrieval |
| TBL-01 | s | 8 / 0 | - | 1-8 | heading:heading=retrieved_packed | ok |
| TBL-02 | s | 8 / 0 | - | 1-3,5-8 | heading:heading=retrieved_packed | ok |
| TBL-05 | c | 8 / 0 | - | 1-8 | heading:heading=retrieved_packed | ok |
| TBL-06 | s | 8 / 0 | - | 1-8 | heading:heading=retrieved_packed | ok |
| SYS-02 | s | 8 / 0 | - | 5 | entry-none:0C7=not_retrieved | not_retrieved |
| SYS-03 | c | 8 / 0 | - | - | entry-none:806=not_retrieved | not_retrieved |
| SYS-04 | c | 8 / 0 | - | - | entry-none:B37=not_retrieved | not_retrieved |
| SYS-07 | c | 8 / 0 | - | - | entry-none:AFB=not_retrieved | not_retrieved |
| SYS-08 | s | 8 / 0 | - | 4 | entry-none:W064=not_retrieved | not_retrieved |
| SYS-09 | s | 8 / 0 | - | 3,7-8 | entry-none:W0A2=not_retrieved | not_retrieved |

#### Profile `local-lane`

| Row | Cx | Packed / dropped hits | Dropped ranks | Gold carrier ranks | Targets (kind:key=outcome) | Row category |
|---|---|---|---|---|---|---|
| CMP-01 | c | 3 / 5 | 4-8 | 5 | doc:SA32-0991-02=retrieved_dropped [prioritize_doc_only]; doc:SA32-0992-02=not_retrieved | packing_loss |
| CMP-02 | c | 3 / 5 | 4-8 | 2,4-8 | doc:SA23-1380-09=retrieved_dropped [prioritize_doc_only]; doc:SA38-0667-09=retrieved_packed; entry-mention:SMFPRMxx=retrieved_packed | packing_loss |
| CMP-05 | c | 2 / 6 | 3-8 | 7 | doc:SC34-2662-05=not_retrieved; doc:SC34-2663-04=retrieved_dropped [prioritize_doc_only] | packing_loss |
| CMP-06 | c | 3 / 5 | 4-8 | 5,7-8 | doc:SA23-2287-03=retrieved_dropped [prioritize_doc_only]; doc:SA23-2288-03=retrieved_dropped [prioritize_doc_only] | packing_loss |
| CMP-07 | c | 3 / 5 | 4-8 | 2,6-7 | doc:SC23-6871-04=retrieved_packed | ok |
| CMP-10 | c | 3 / 5 | 4-8 | 3 | doc:SA23-2292-03=retrieved_packed | ok |
| CMP-11 | c | 3 / 5 | 4-8 | 2,6-7 | doc:SC34-2662-05=retrieved_packed | ok |
| CMP-12 | c | 2 / 6 | 3-8 | 1,3,5-7 | doc:SA23-1380-09=retrieved_packed; doc:SA38-0667-09=retrieved_dropped [prioritize_doc_only]; entry-mention:SMFPRMxx=retrieved_packed | packing_loss |
| CMP-15 | c | 3 / 5 | 4-8 | 1-2,5 | doc:SA32-0975-01=retrieved_packed | ok |
| CMP-16 | c | 3 / 5 | 4-8 | 1-8 | doc:GC35-0033-41=retrieved_packed | ok |
| CMP-17 | c | 3 / 5 | 4-8 | 5 | doc:SA23-1380-09=retrieved_dropped [prioritize_doc_only]; entry-none:MPFLSTxx=not_retrieved; entry-mention:MSGFLDxx=retrieved_dropped [coverage_pass] | packing_loss |
| TBL-01 | s | 3 / 5 | 4-8 | 1-8 | heading:heading=retrieved_packed | ok |
| TBL-02 | s | 8 / 0 | - | 1-3,5-8 | heading:heading=retrieved_packed | ok |
| TBL-05 | c | 3 / 5 | 4-8 | 1-8 | heading:heading=retrieved_dropped [prioritize_doc_only] | packing_loss |
| TBL-06 | s | 4 / 4 | 5-8 | 1-8 | heading:heading=retrieved_packed | ok |
| SYS-02 | s | 4 / 4 | 5-8 | 5 | entry-none:0C7=not_retrieved | not_retrieved |
| SYS-03 | c | 3 / 5 | 4-8 | - | entry-none:806=not_retrieved | not_retrieved |
| SYS-04 | c | 2 / 6 | 3-8 | - | entry-none:B37=not_retrieved | not_retrieved |
| SYS-07 | c | 3 / 5 | 4-8 | - | entry-none:AFB=not_retrieved | not_retrieved |
| SYS-08 | s | 3 / 5 | 4-8 | 4 | entry-none:W064=not_retrieved | not_retrieved |
| SYS-09 | s | 3 / 5 | 4-8 | 3,7-8 | entry-none:W0A2=not_retrieved | not_retrieved |


## Decision for #811

**Phase 1 is not justified by this evidence for the production profile.** At the 32768-token window (and at 16384) packing dropped none of
the gold targets in 45 rows, with at most 29% of a 21k-token budget used by eight hits at the 4000-character cap. The
measured losses (CMP-02 on 2026-10-03, the 806 and AFB refusals in #591) come from the 4096-token local lane, a relative-only dev stand-in,
where the planner keeps only the first 2 or 3 hits. The three CMP rows that fail at `target` are retrieval gaps (a document or an identifier
never retrieved), and TBL-05 has its gold hit in the prompt, so the 2026-10-17 refusal with the gold document retrieved is not a packing loss.

Open items and routing:

1. **Code rows (806, AFB) are undetermined, not cleared.** Rerun `scripts/packing_diagnostic.py --classes SYS` on a corpus ingested under
   the current rules (code-entry chunks present) before attributing the #591 refusals. By the budget headroom they are very unlikely to be
   packing losses at the production window; the expected owner is retrieval or representation, or the model, and the rerun confirms it.
2. **If the local lane stays a decision lane**, the data supports something narrower than phase 1 as designed: the identifier coverage pass
   recovers 4 of the 12 lost targets and skip-and-continue none. Document-level coverage (the CMP sides) is outside the approved design and
   needs its own decision.
3. The comparison retrieval gaps at `target` (CMP-01, 05, 17) belong to retrieval (split and expansion), measured on this stale corpus only.

Limits: stale corpus and rules (above); one retrieval pass per row on one corpus; estimator-only token counting (production verification
can trim more); `entry-mention` matches any whole-token mention in a gold-document hit, which can overstate an identifier's retrieval; no
answers or judging were produced.

## Reproduce

```sh
# Qdrant through the supervised forward and the embed leg through the repository launcher (docs/live-stack.md);
# the API key is read from the environment only.
VENUE=rc QDRANT_URL=http://127.0.0.1:<port> QDRANT_COLLECTION=<alias> EMBED_MODE=vllm \
EMBED_BASE_URL=http://127.0.0.1:8001/v1 EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B \
EMBED_MODEL_REVISION=<revision> DENSE_DIM=1024 BM25_CACHE_DIR=<repo>/bundles/bm25-weights \
  python scripts/packing_diagnostic.py --classes CMP,TBL,SYS,MSG \
  --profile target --profile w16384:llm_max_model_len=16384 \
  --profile 'local-lane:llm_max_model_len=4096,llm_reserved_output_tokens=1536,llm_thinking_reserve_tokens_simple=0,llm_thinking_reserve_tokens_complex=1000,llm_max_chunk_tokens_narrative=350,prompt_max_chunk_chars=3000,prompt_max_chunk_chars_complex=1100' \
  --out report.json --markdown report.md
```
