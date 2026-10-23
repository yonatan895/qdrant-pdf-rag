# Medium reasoning-effort arm (record)

Status: dated record for issue #917 criterion 1, run 2026-10-23 on `main` `308f70b` (session S9). Pairs with the
[effort arms](2026-10-23-effort-arms.md) (low, xhigh) and the [xhigh baseline](2026-10-23-xhigh-answer-baseline.md).
Read-only: no default is changed here; recommendation pending maintainer decision (#917).
Identifiers, counts, classes, states and latencies only; raw reports stay outside git.

## Setup

- Identical to the S5 low and S2 to S4 xhigh arms: G4 BF16 `Qwen/Qwen3.8-27B`, `--max-num-seqs 2`; alias
  `colab_manuals_qwen3_8b_4096_ced400a7` (rules `ced400a7`); rerank off; `REQUEST_DEADLINE_S` 570, client timeout 600; same
  row order. Only `LLM_REASONING_EFFORT_{SIMPLE,COMPLEX}` differs: `medium`, verified from the live agent environment before
  the first row (the driver refuses otherwise).
- 126 of the 127 dev rows ran (63 of 64 chunks; frozen holdout excluded, #859). SYS-09, in the last chunk, was not run:
  the session ran out of time. All tables below use the same 126 rows for all three arms, so low and xhigh figures differ
  slightly from the 127-row [effort arms](2026-10-23-effort-arms.md) record (SYS-09 dropped from both).
- Session elapsed 6598.6 s, exit 0; 126 rows, 0 request errors, 0 timeouts.
- Sources: medium S9 `answers-medium/chunk-*.json`, low S5 `answers-low/`, xhigh S2 `queue/reports/`; pairs recomputed
  from the per-row JSONs (judged `verdict`, `elapsed_ms`, `verification_state`).

## Results by class

Pass counts by `query_class`, same 126 rows:

| Class | n | low pass | medium pass | xhigh pass | low p50 / p95 s | medium p50 / p95 s | xhigh p50 / p95 s |
|---|---:|---:|---:|---:|---|---|---|
| comparative | 12 | 12 | 12 | 4 | 113.7 / 147.4 | 134.7 / 218.1 | 493.2 / 495.4 |
| diagnostic | 27 | 27 | 27 | 16 | 87.9 / 154.1 | 100.6 / 185.3 | 474.4 / 494.5 |
| doc_number | 14 | 14 | 14 | 14 | 38.8 / 53.0 | 49.0 / 66.9 | 93.8 / 210.1 |
| message_id | 28 | 26 | 26 | 25 | 61.6 / 114.2 | 76.5 / 139.2 | 142.7 / 473.5 |
| negative | 8 | 5 | 6 | 6 | 30.3 / 99.7 | 37.7 / 107.0 | 124.7 / 437.7 |
| syntax | 25 | 23 | 24 | 17 | 73.2 / 121.3 | 91.6 / 175.8 | 287.3 / 495.6 |
| table | 4 | 3 | 4 | 4 | 35.5 / 37.2 | 53.9 / 87.0 | 75.7 / 148.6 |
| version | 8 | 5 | 6 | 2 | 63.5 / 110.4 | 83.9 / 145.4 | 294.3 / 494.1 |
| all | 126 | 115 | 119 | 88 | 69.7 / 136.1 | 87.5 / 163.3 | 212.5 / 494.6 |

| Same 126 rows | low | medium | xhigh |
|---|---:|---:|---:|
| pass | 115 | 119 | 88 |
| abstain rows declined (`insufficient_evidence`) | 7/13 | 9/13 | 7/13 |
| `generation_incomplete` | 0 | 0 | 31 |
| latency p50 / p95 / max s | 69.7 / 136.1 / 254.4 | 87.5 / 163.3 / 232.3 | 212.5 / 494.6 / 506.4 |
| rows over 300 s | 0 | 0 | 54 |

Median per-row latency ratio: medium/low 1.22, xhigh/medium 2.48.

## Paired results

Paired by row id (126 of 126), exact two-sided McNemar:

- medium vs low: both pass 115, medium only 4 (NEG-02, SYN-01, TBL-02, VER-07), low only 0, both fail 7; p = 0.125.
- medium vs xhigh: both pass 87, medium only 32, xhigh only 1 (SYS-07), both fail 6; p < 0.001 (7.9e-9).
- The 32 medium-only rows are CMP-01, -02, -05, -06, -07, -11, -12, -16; DIA-01, -05, -06, -18, -22, -23, -32; LEG-01, -03,
  -10, -11; MSG-18, -22; SYN-06, -16, -17, -22, -25, -26, -32; VER-01, -06, -07, -12. 30 of them are xhigh
  `generation_incomplete`; VER-07 and SYN-32 are abstain-tier rows xhigh answered (`accepted`).

## Abstention

Medium declines 9 of the 13 abstain-tier rows, low and xhigh 7. NEG-02 and VER-07 are abstain-tier rows that
medium declines and low does not (both listed under medium only above). The four abstain rows medium still answers are NEG-03, NEG-04,
VER-08 and VER-11.

## Latency

Medium p50 87.5 s, p95 163.3 s, max 232.3 s: about 18 s above low at the median, a third of xhigh's median, and well inside
the 570 s deadline. No row exceeded 300 s; no `generation_incomplete`.

## Medium failures (7) and routing

| Row | Class | Served state | Routing |
|---|---|---|---|
| MSG-13 | message_id | `insufficient_evidence` (answer-tier refusal) | #916; retrieval fix in #922 / #931 |
| SYN-07 | syntax | `insufficient_evidence` (answer-tier refusal) | #916 |
| SYS-07 | message_id | `insufficient_evidence` (answer-tier refusal) | unrouted (not yet diagnosed); the only row xhigh passes and medium does not |
| NEG-04 | negative | `accepted`, trap answered | #915 (prompt concern) |
| VER-08 | version | `accepted` on an abstain-tier row | #915; maintainer approved relabeling to answer-tier, pending that PR |
| VER-11 | version | `accepted` on an abstain-tier row | #915; same relabel |
| NEG-03 | negative | `accepted`, trap answered (forbidden-substring and citation checks) | unrouted (not yet diagnosed); fails at every effort |

Low fails the same seven plus NEG-02, SYN-01, TBL-02 and VER-07.

## Recommendation

Recommendation pending maintainer decision (#917). No default is changed here. Medium matches or beats low on every row (no
low-only row) for about 18 s more at the median; its gain over low is not significant on its own (4 to 0, p = 0.125). Both
beat xhigh by about 30 rows at a third of the latency or less.

## Limits

- SYS-09 not run (session time): 126 of 127 rows.
- One run per row at temperature 0; automated rubric, not human grading.
- The dev set is the tuning set; no holdout confirmation (#859 exposure).
- The xhigh column is the S2 to S4 baseline, run on separate sessions from low and medium; the arms differ in session, not
  only in effort.
- SYS-07 and NEG-03 are unrouted: neither has been diagnosed.
