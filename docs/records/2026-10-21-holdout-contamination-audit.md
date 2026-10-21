# Holdout contamination audit (record)

Status: dated record for issue #844, 2026-10-21. Base: `main` `423f7ba`. Owner of the rule it audits:
[golden corpus discipline](../eval.md#7-golden-corpus-discipline) ("tune against dev only; never iterate the holdout").
Read-only: no eval was run, and the holdout, its pin and every baseline are unchanged. It holds row ids, metrics and
references only (no query or manual text).

## Question

Which shipped defaults, constants, algorithms or prompts were chosen, kept or rejected on frozen-holdout numbers,
and which holdout rows can still serve as an independent ruler?

Using the holdout to gate a release, record a baseline or size an operational limit is documented design and is not
counted here. Choosing between candidates because of holdout results is.

## Method

- Searched configuration and code comments (`src/`), `docs/` (ADRs, owner docs, the dated table in `docs/eval.md`
  §10, records), commit messages (`git log --grep`, `-S`) and PR/issue bodies and comments (`gh search`).
- Rebuilt the holdout id set at every committed version of `evals/holdout.jsonl`. Checked which version was in force
  at each decision with `git merge-base --is-ancestor`.
- Recomputed the answer-tier samples. `select_sample` (`eval/answers.py`) is a deterministic class round-robin over
  dev plus holdout under `VENUE=rc`: the L2/L4/`eval-answers` default since #268 (`resolve_golden_paths`), and the
  unconditional harness default before it.
- Three parallel read-only research passes. Their material claims were re-checked against the primary source (PR
  body, commit, file version) before inclusion.

## Holdout versions

| Version | Date | Rows | Change |
|---|---|---|---|
| `2443308` | 2026-09-06 | 70 | Last retrieval-gold edit of the original rows (after the 09-02 re-split and the V2R2 re-bind) |
| `dff83f8` | 2026-09-12 | 72 | + TBL-03, TBL-04 (#292) |
| `080385c` | 2026-09-16 | 72 | `expected_verification_state` on every row; DOC-03 answer gold re-adjudicated (#406) |
| `3bf7c48` | 2026-10-03 | 76 | + SYS-01, SYS-05, SYS-06, SYS-10 (#591/#637); existing rows byte-identical |

The pin matches: `evals/holdout.jsonl.sha256` = `sha256sum evals/holdout.jsonl` (`58fab339…`).

## Decisions taken on holdout numbers

| ID | Decision (where it lives now) | Decided | Holdout evidence | Kind | Dev evidence |
|---|---|---|---|---|---|
| H1 | `comparative_split_enabled = True` (`config.py`) | PR #287, 2026-09-11 (#270) | 2x2 on the 70-row holdout, pre-registered adoption rule; comparative r@1 0.286→0.429, MRR 0.429→0.536; movers CMP-03, CMP-08, CMP-09 | Selection (4 cells, one rule, no iteration) | None usable: the synthetic dev venue was saturated (#217) |
| H2 | `diagnostic_dualpath_enabled = False` (`config.py`) | PR #287 (same 2x2) | Class MRR 0.655→0.643, overall r@5 0.873→0.857; DIA-03, DIA-24 | Selection (kept off) | As H1 |
| H3 | `rerank_fusion_alpha = 1.0`, rerank default off (`config.py`) | PR #213, 2026-09-08 (#215) | Live cross-encoder on the holdout: alpha 1.0/0.75/0.5/0.25/0.0 compared; "no fusion weight beats legacy" | Selection by iteration (5 values) | **Re-measured on dev only** in PR #290 (2026-09-11, n=104): every alpha at or below RRF on r@1, the same conclusion |
| H4 | `rrf_k = 2` kept; query-prefix retune and `acronym_expansion_enabled = True` rejected | PR #213 text: "Earlier sweeps on same venue" | `rrf_k` 60 MRR −0.006; prefix +1/−1; acronyms +2/−1 | Selection by iteration (aggregates only; the runs have no record of their own) | `rrf_k = 2` predates the holdout (initial implementation, 2026-08-28). The dev replay of 2026-10-19 (#853) now favours `rrf_k` 60: dev r@1 0.667→0.702, r@5 0.921→0.956 |
| H5 | Comparative paths fuse by maximum, not sum (`retrieve/query.py`, `max_split_hits`) | PR #217 / `77e9b07`, 2026-09-08 (#214) | One holdout row: CMP-14 (noted in the code comment) | Algorithm chosen from a holdout row | Unknown |
| H6 | Slash pairs (`A/B`) never split (`retrieve/split.py`) | PR #217 (#214) | "Measured negative": VER-03 | Rule chosen from a holdout row | Unknown |
| H7 | Doc-family filter: suffix-less document numbers expand to their edition family (`retrieve/filters.py`, `_find_doc_filter_ids`) | PR #289, 2026-09-11 (#270) | Motivated by holdout misses DOC-03/DOC-04 (DOC-03 debugged at fused rank 15 in #213), confirmed with `eval-holdout`: doc_number r@1 0.667→1.0 | Fix designed on holdout failures, confirmed on the holdout | The dev doc_number gap was visible (r@1 0.786, #270), but no live dev A/B was recorded for #289 |
| H8 | Complex thinking reserve 0→1000 (`llm_thinking_reserve_tokens_complex`) | PR #304, 2026-09-12 (#298) | `VENUE=rc` `harness-l4` N=24x3; reserve 500 rejected on an entailment band fail | Selection on a sample with 7 holdout rows | No. **Superseded:** the default is now 8192, provisionally priced by #777 and not taken from this evidence |
| H9 | Answer prompt rules 4 and 6 (`agent/answer.py`) | PR #308, 2026-09-12 (#305) | L2 N=24 + L4 N=24x3; "two stronger wordings were probed and rejected by measurement"; DOC-03 named among the refusers | Selection on a sample with 7 holdout rows | No |

Answer-tier samples (H8, H9): the N=24 round-robin over dev plus holdout draws CMP-03, DIA-03, DOC-03, MSG-04,
MSG-07, TBL-03 and VER-03 at `dff83f8` and every later version (recomputed on `main`; both runs descend from
`dff83f8`). N=12 draws no holdout row. The per-run reports are gitignored, so membership is computed, not read
from a report; DOC-03 is named in the runs' own text.

Gold adjusted after holdout results were seen: VER-09/VER-10 sibling `must_not` removed (#178), and DOC-03's answer
gold re-adjudicated (#406, retrieval fields unchanged). Both edits were justified against the corpus. They still mean
those rows were shaped with knowledge of the system's output.

## Uses that are not contamination

| Use | Why it is allowed |
|---|---|
| `evals/holdout-baseline.json`, `benchmarks/harness-vllm.json`, `eval:holdout` | Gating against a recorded baseline (documented RC design) |
| `evals/harness-l4-thresholds.json` (reference recorded on the mixed N=24 sample) | Gating reference; its 0.15 tolerance is not tuned to holdout results |
| Reranker window 1024→2048 (`serve/profiles.py`, 23/70 holdout queries returned 400) | Operational sizing from input lengths, not a quality choice |
| Judge `reasoning_effort = "low"` (`eval/judging.py`) | Evaluation instrument, fixed for unparseable output |
| #847 BM25 query encoder (holdout r@5 0.870→0.884, "measurement only") | Correctness fix kept on dev grounds |
| #290, #852, #853 sweeps; #212 retry; `CHAT_CONDENSE_ENABLED` stays off | Dev only (the holdout was not read, or was a check only) |

No holdout-based decision was found after 2026-10-03. #853 plans one confirmatory holdout run for `rrf_k` 60. That
value was already measured on the holdout once (H4), so the run would not be independent.

## Which rows are still independent

| Class | Rows | Exposed to a selection | Exposed to measurement only | Independent |
|---|---|---|---|---|
| comparative | 7 | 7 | 0 | 0 |
| diagnostic | 14 | 14 | 0 | 0 |
| doc_number | 6 | 6 | 0 | 0 |
| message_id | 19 | 15 | 0 | 4 (SYS-01, SYS-05, SYS-06, SYS-10) |
| negative | 6 | 6 | 0 | 0 |
| syntax | 16 | 16 | 0 | 0 |
| table | 2 | 1 (TBL-03, via H8/H9) | 1 (TBL-04) | 0 |
| version | 6 | 6 | 0 | 0 |
| **Total** | **76** | **71** | **1** | **4** |

All 70 rows of version `2443308` were in force for H1–H7. Aggregate decisions (H1–H4) read every row. SYS-01 and
SYS-05 had earlier dev versions with different queries (#591), so SYS-06 and SYS-10 are the cleanest.

**Conclusion:** the frozen holdout is still a valid *regression* ruler, because its gate compares each candidate
with its own recorded baseline. It is no longer an *independent* estimate of quality: 71 of 76 rows informed shipped
choices, so holdout numbers overstate generalisation for H1–H9.

## Proposed remedy (maintainer decision)

1. **Re-measure the live decisions on dev only,** at the target profile, through a dedicated concern per change.
   The current-rules dev set discriminates (dev r@1 0.667), unlike the saturated synthetic venue of September.

   | ID | Re-measurement | Status |
   |---|---|---|
   | H3 | — | Done: dev-only #290 reached the same value |
   | H4 | `rrf_k`: the pending default-change concern from #853 decides on dev | Open. Prefix and acronyms need a live dev run (they change query text or embeddings) |
   | H1, H2, H5, H6, H7 | One live dev A/B in a G4 session (split on/off, diagnostic on/off, doc-family on/off). Fusion and slash rules only if the split A/B moves comparative rows | Open |
   | H8 | Nothing to re-measure: superseded by #777. Its eventual measurement must be dev only | — |
   | H9 | Prompt wording re-checked on a dev-only answer sample (`--golden evals/golden.jsonl` under `VENUE=rc`) when #830 runs | Open |

2. **Replace the independent ruler.** Either:
   - promote the independent release set (#367) as the confirmation ruler; or
   - re-freeze through §7 with newly authored rows. The current 76 rows stay as the regression set and keep their
     baselines.

   Until then, RC reports should label holdout numbers as exposed (71/76). No new decision, including #853's
   `rrf_k` 60, should be confirmed on the holdout.
3. **Close the path that let H8 and H9 happen (optional).** Under `VENUE=rc`, the L2/L4/`eval-answers` default
   silently mixes holdout rows into an A/B sample. A selection experiment should pass `--golden evals/golden.jsonl`
   explicitly, or the harness should refuse holdout rows for comparison runs. This is a process change, so it needs
   its own approved concern.

Not fixed here (recorded in #588): the `llm_thinking_reserve_tokens_complex` comment in `config.py` still describes
the 1000 value, but the default is 8192 (#777).
