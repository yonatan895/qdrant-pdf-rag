# Comparative merge live evaluation (record)

Status: dated record for issue #825's last criterion, live run 2026-10-22 on `main` `308f70b`. Owner of the behavior:
[retrieval contracts](../retrieval.md) (per-document comparative merge, #852, `3329275`). Read-only: no collection, index or
data change. The frozen holdout was not read (#859: its comparative rows are exposed, so there is no holdout confirmation).
Ids, counts and metrics only; the raw run files stay outside git.

## Setup

- G4 runtime, Qwen3-Embedding-8B (dim 4096) through the gateway embed leg; alias `colab_manuals_qwen3_8b_4096_ced400a7`
  (rules `ced400a7`, 234,610 points); shipped retrieval defaults, rerank off.
- Dev golden (127 rows, 114 scored) and `evals/sections.jsonl` (17). Two replicates, rows identical between them. The same
  runs are the ON arm of the #842 search-params A/B ([record](2026-10-22-search-params.md)); its OFF arm is the control.

## Results

| Set | n | r@1 | r@5 | r@8 | MRR |
|---|---|---|---|---|---|
| Dev, all scored | 114 | 0.711 | 0.956 | 0.965 | 0.802 |
| Dev, comparative | 12 | 0.333 | 0.917 | n/a | 0.583 |
| Sections | 17 | 0.235 | 0.471 | 0.529 | 0.333 |

0 failures, 0 `must_not` violations. Comparative has the lowest r@1 of any dev class; its r@5 is 11 of 12 rows.

Rows named in #825 (first expected document, dev run, both replicates):

| Row | r@1 | r@5 | r@8 | MRR | Status |
|---|---|---|---|---|---|
| CMP-05 | 0 | 1 | 1 | 0.333 (rank 3) | retrieved; claim refuted on current rules |
| CMP-17 | 0 | 1 | 1 | 0.333 (rank 3) | retrieved; claim refuted on current rules |
| CMP-06 | 0 | 1 | 1 | 0.500 (rank 2) | recovered by #852 |
| CMP-01 | 0 | 0 | 0 | 0 | not retrieved: golden-label question, labels kept as written |

CMP-01 expects two JES2 manuals (`SA32-0992-02`, `SA32-0991-02`) for a JES2-versus-JES3 question; neither is within the
top 8 and the retrieved top 3 are three other documents. The single miss in the class is CMP-01; no other comparative row
misses at depth 5.

## No regression

- Within the run: the #842 OFF arm gives identical dev r@1, r@5, MRR and identical comparative numbers; across all 127 dev
  rows only SYN-12 changes rank (better with ON), and in sections only SEC-06 (better), none worse.
- Offline replay from #852 (corpus `c47352b2`, pools at `912a23f`): dev comparative r@5 0.833 to 0.917, MRR 0.514 to 0.542;
  overall dev r@5 0.912 to 0.921; r@1 unchanged; section probes unchanged. The live comparative r@5 0.917 matches it.
- Previous live record ([eval.md](../eval.md), 2026-10-21, `2a723c6`, `c47352b2`, `rrf_k` 60): dev r@1 0.702, r@5 0.956,
  MRR 0.796; sections r@5 0.471, MRR 0.325. Now r@1 0.711, r@5 0.956, MRR 0.802; sections r@5 0.471, MRR 0.333.

Comparability. Run to run these differ in corpus rules (`c47352b2` versus `ced400a7`, 234,723 versus 234,610 points) and
in the pinned dense search parameters (#842), so the 2026-10-21 and 2026-10-22 live numbers are consistent with no
regression but are not a controlled comparison. The offline replay is controlled but ran on the older corpus and replayed
pools. No live pre-#852 comparative number exists on this corpus, so the live run shows no regression and the recovered
rows, not a measured live gain.

## Not covered

- Holdout (comparative or otherwise): not read (#859).
- Answer quality: out of scope here. The #831 low-effort arm passed 12/12 comparative answers
  ([comment](https://github.com/yonatan895/qdrant-pdf-rag/issues/831#issuecomment-6355914926)).
- Rerank path (R1 on #588): not measured.
