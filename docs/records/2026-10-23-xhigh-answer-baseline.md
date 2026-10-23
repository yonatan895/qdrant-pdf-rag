# xhigh answer baseline (record)

Status: dated record for issue #830 criteria 1 and 2, runs 2026-10-22 to 2026-10-23. Tool:
[`scripts/answer_taxonomy.py`](../../scripts/answer_taxonomy.py) ([taxonomy](../eval.md#failure-taxonomy-830)). Candidate: `main` `308f70b`.
Identifiers, counts, classes, states and latencies only: no query, answer, reasoning or manual text. Raw reports stay outside git.

## Setup

| Item | Value |
|---|---|
| Model | G4 BF16 `Qwen/Qwen3.8-27B`, `--max-num-seqs 2`, max-model-len 32768; effort xhigh/xhigh; rerank off |
| Embedding | `Qwen/Qwen3-Embedding-8B`, dim 4096 |
| Corpus | alias `colab_manuals_qwen3_8b_4096_ced400a7`, rules `ced400a7`, 234,610 points |
| Budgets | `REQUEST_DEADLINE_S` 570, client timeout 600; `max_tokens` 10,752 (`LLM_RESERVED_OUTPUT_TOKENS` 2560 + thinking reserve 8192) |
| Rows | 127 dev rows (`evals/golden.jsonl`); frozen holdout excluded (#859) |
| Sessions | S2 2026-10-22 (46 started; SYN-06 got a 502 at session end, discarded by a ledger correction and re-run in S3), S3 (45), S4 2026-10-23 (37) |

127 of 127 rows ran exactly once (final accepted: S2 45, S3 45, S4 37), 0 timeouts, 0 request errors.

## Result

88 of 127 pass. Primary class per row (first match wins):

| Row class | pass | generation_incomplete | retrieval_miss | unsafe_answer | refusal_gold_present | citation_format |
|---|---:|---:|---:|---:|---:|---:|
| CMP | 3 | 6 | 2 | 0 | 0 | 0 |
| DIA | 14 | 5 | 2 | 0 | 0 | 0 |
| DOC | 11 | 0 | 0 | 0 | 0 | 0 |
| LEG | 8 | 4 | 0 | 0 | 0 | 0 |
| MSG | 21 | 2 | 0 | 0 | 1 | 0 |
| NEG | 4 | 1 | 0 | 0 | 0 | 1 |
| SYN | 16 | 6 | 0 | 1 | 1 | 0 |
| SYS | 5 | 0 | 1 | 0 | 0 | 0 |
| TBL | 4 | 0 | 0 | 0 | 0 | 0 |
| VER | 2 | 3 | 0 | 2 | 0 | 1 |
| all | 88 | 27 | 5 | 3 | 2 | 2 |

## Per-row attribution (non-pass rows)

| Primary | n | Rows | Attribution |
|---|---:|---|---|
| generation_incomplete | 27 | CMP-02, CMP-05, CMP-07, CMP-11, CMP-12, CMP-16, DIA-01, DIA-05, DIA-06, DIA-22, DIA-32, LEG-01, LEG-03, LEG-10, LEG-11, MSG-18, MSG-22, NEG-03, SYN-06, SYN-16, SYN-17, SYN-22, SYN-25, SYN-26, VER-01, VER-06, VER-12 | `max_tokens` exhausted by thinking, 492.1 to 507.6 s (median 495.4 s; every other row <= 496.7 s, median 156.0 s). Packing `ok` for the 26 answer rows. NEG-03 is an abstain row |
| retrieval_miss | 5 | CMP-01, CMP-06, DIA-18, DIA-23, SYS-09 | Gold not retrieved: CMP-01, DIA-18, SYS-09 `not_retrieved`; CMP-06, DIA-23 `partial_retrieval`. All five also ended `generation_incomplete` (470.5 to 497.9 s), so a retrieval fix alone does not pass them |
| unsafe_answer | 3 | SYN-32, VER-08, VER-11 | Abstain rows answered (state `accepted`) |
| citation_format | 2 | NEG-04, VER-07 | Abstain rows answered (state `accepted`); classed by the failure text, same behavior as `unsafe_answer` |
| refusal_gold_present | 2 | MSG-13, SYN-07 | Served `insufficient_evidence` with the gold packed (packing `ok`) |

Passes with a gold gap (judged pass, gold unit not retrieved): DIA-17, DIA-21, DIA-26, DIA-28, SYS-08. Pool disagreements: none.
By final state: accepted 86, generation_incomplete 32 (27 primary + 5 retrieval_miss), insufficient_evidence 9.

## Abstention (#798)

13 abstain rows: 7 correct declines (DIA-08, MSG-01, MSG-24, NEG-02, NEG-07, NEG-08, NEG-09); NEG-04, SYN-32, VER-07, VER-08, VER-11
answered; NEG-03 `generation_incomplete`. Prior baselines: main 4/13, original #764 sentence 8/13.

## Routing

- `generation_incomplete` (27, plus the 5 truncated retrieval rows) is owned by #843 (thinking budget, arm in progress) and #831 (effort).
  The #831 low-effort arm passes 31 of the 32 rows xhigh truncates
  ([comment](https://github.com/yonatan895/qdrant-pdf-rag/issues/831#issuecomment-6355914926)). No separate Task.
- `retrieval_miss`: CMP-01 is already in #825; CMP-06 extends it. DIA-18, DIA-23, SYS-09 have no owner.
- Abstention answered (5 rows) and `refusal_gold_present` (2 rows) have no owner beyond the measurement in #798.
- Draft Tasks (criterion 3) are filed separately by the maintainer.

Limits: one run per row at temperature 0; the dev set is the tuning set, not an acceptance set.
