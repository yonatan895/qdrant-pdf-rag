# Reasoning-effort arms (record)

Status: dated record for issue #831 criterion 2 (with the #843 thinking-budget arm), runs 2026-10-22 to 2026-10-23 on `main`
`308f70b`. xhigh arm: [baseline record](2026-10-23-xhigh-answer-baseline.md). Read-only: no default is changed here.
Identifiers, counts, classes, states and latencies only; raw reports stay outside git.

## Setup

- G4 BF16 `Qwen/Qwen3.8-27B`, `--max-num-seqs 2`; alias `colab_manuals_qwen3_8b_4096_ced400a7` (rules `ced400a7`); rerank off.
- 127 dev rows, frozen holdout excluded (#859). `REQUEST_DEADLINE_S` 570, client timeout 600 in both arms. Only
  `LLM_REASONING_EFFORT_{SIMPLE,COMPLEX}` differs: xhigh (S2 to S4) versus low (S5, 2026-10-23; 64 chunks, 0 errors, 0 timeouts).
- Budget arm (#843): xhigh with thinking reserve 16384 (`max_tokens` 18,944, was 10,752), deadline 900 s, client 950 s, on the 32
  rows the xhigh baseline ended `generation_incomplete`.

## Results by class

| Class | n | xhigh pass | low pass | xhigh p50 / p95 s | low p50 / p95 s |
|---|---:|---:|---:|---:|---:|
| comparative | 12 | 4 | 12 | 493.2 / 495.4 | 113.7 / 147.4 |
| diagnostic | 27 | 16 | 27 | 474.4 / 494.5 | 87.9 / 154.1 |
| doc_number | 14 | 14 | 14 | 93.8 / 210.1 | 38.8 / 53.0 |
| message_id | 29 | 25 | 27 | 143.0 / 483.5 | 61.8 / 114.1 |
| negative | 8 | 6 | 5 | 124.7 / 437.7 | 30.3 / 99.7 |
| syntax | 25 | 17 | 23 | 287.3 / 495.6 | 73.2 / 121.3 |
| table | 4 | 4 | 3 | 75.7 / 148.6 | 35.5 / 37.2 |
| version | 8 | 2 | 5 | 294.3 / 494.1 | 63.5 / 110.4 |
| all | 127 | 88 | 116 | 221.0 / 494.6 | 70.4 / 136.0 |

Rows over 300 s: xhigh 55, low 0 (low max 254.4 s). Median ratio xhigh/low 3.26.

Paired by row id (127 of 127): both pass 84, low only 32, xhigh only 4, both fail 7; exact McNemar p < 0.001.

- On the 32 rows xhigh ends `generation_incomplete`, low passes 31 (NEG-03, an abstain row, fails). On the 95 rows xhigh
  completes, xhigh passes 88 and low 85 (discordant 4 versus 1; not significant). Low produced 0 `generation_incomplete`.
- xhigh only: NEG-02, SYN-01, SYS-07, TBL-02. Low-arm failures (11): MSG-13, NEG-02, NEG-03, NEG-04, SYN-01, SYN-07, SYS-07,
  TBL-02, VER-07, VER-08, VER-11.
- Abstention: 7 of 13 correct at both efforts.

## Thinking-budget arm (#843)

19 rows ran before the session closed; 16 are credited (DIA-06 `deadline_exceeded` at 901 s, SYN-06 and SYN-16 `model_timeout`
are reported separately).

| Same 16 rows | xhigh + 16384 reserve | low |
|---|---:|---:|
| pass | 11 | 15 |
| still `generation_incomplete` | 4 (CMP-01, LEG-03, MSG-18, VER-06) | 0 |
| p50 latency s | 658 | 110 |

No row passes only at xhigh with the larger reserve; NEG-03 fails in both arms.

## Recommendation (not a decision)

Propose, as its own approved default-change concern, `low` reasoning effort for both routes on the target profile. It passes
more rows (no truncation) at about a third of the median latency (p95 136 s, max 254 s against a tail at the deadline for
xhigh). xhigh's advantage is confined to four rows and is not significant, and raising the thinking budget does not close the
gap. Routing (low simple, xhigh complex) was not run as a third arm; the complex classes need no xhigh (comparative 12 of 12,
diagnostic 27 of 27 at low). The shipped deadline value for #843 depends on that decision. Defaults are unchanged here.

## Limits

- The dev set is the tuning set; no holdout confirmation (#859 exposure).
- One run per row at temperature 0; automated rubric, not human grading.
- Budget arm: 16 credited rows, not the 32. SYN-06's `model_timeout` at 302 s on a streamed call is unexplained.
