# Evaluation, harness, and benchmark reference

Owner: this file. Test-writing rules: `docs/testing.md`. Live ladder:
`docs/live-stack.md`. Design overview: `docs/architecture.md` §5.

> Skeleton for the docs epic (PR-A). Section headings below mark the full
> content that lands in PR-E. Each section documents metric definitions,
> gate thresholds, baseline shapes, and discipline rules with `file:line`
> evidence. One fact, one owner — do not duplicate `testing.md`.

## 1. Tier map

Scope: script → tier table (`gate_l1`, `eval_retrieval`, `eval_answers`,
`harness*`, `benchmark`, `loadtest`, `verify_golden`, `build_golden_corpus`,
`render_report`, `bootstrap_ci`, `qdrant_sim`/`qdrant_pin`, `query_demo`,
`mock_vllm`, `make_synthetic_pdf`, `test_local_e2e_vllm`, `smoke_search`).

## 2. Retrieval eval (`eval_retrieval.py`)

Scope: `SEARCH_LIMIT=8`, doc-level relevance (+heading rule), graded gains and
per-doc nDCG@8, recall/MRR/`page_hit@5` definitions, aggregation and rounding,
gated ratios, warn-skip rules (missing metric/baseline, mode vs collection
mismatch — and the exit-0 skip semantics), `--check` vs `--update-baseline`.

## 3. L1 gate (`gate_l1.py`)

Scope: synthetic corpus generation layout, ephemeral collection naming,
`--workers 1`, env restore, fail conditions (regressions or failures),
cleanup, `--rerank` flip, `--keep-sim`.

## 4. Layered harness (`harness.py`, `harness_l1/l2/l3.py`)

Scope: snapshot fingerprint/restore/pin semantics (count+name, not content);
`never`/`always`/`drift` actions; paired bootstrap (`resamples`, seed, CI
rule); merge/hold/baseline verdict vocabulary; L1 keys and rounding; L2
citation P/R, NLI judge, alert join, structural fails; L3 latencies, TTFT,
VRAM, env-key gating (bench 2-key vs L3 4-key); RC-only discipline.

## 5. Answer eval (`eval_answers.py`)

Scope: zero-hits/refusal/markers, judge rules per behavior, gold suppression,
stratified sampling, structural FAILs vs trend rates, deliberate non-features
(no retry, no `finish_reason` checks, judge never re-parses citations).

## 6. Paraphrase instrument

Scope: no-echo contract (`MAX_VERBATIM_RUN=8`), corpus competitors,
mode-keyed baselines and what each measures (plumbing anchor vs semantic
headroom), manual runbook (generate → ingest → check), not-in-CI rule.

## 7. Golden corpus discipline

Scope: dev vs frozen holdout, `verify_golden` FAIL/WARN taxonomy, `--strict`
vs default, re-freeze + re-record in one commit, never-tune-holdout,
`build_golden_corpus` split rule and binding tables.

## 8. Benchmarks (`benchmark.py`, `loadtest.py`)

Scope: gated metrics and tolerances, env snapshot, corpus generation,
RSS/disk floors and aggregation semantics (min/max, `peak_rss` caveat),
refuse-to-record rules, percentile definitions per tool (nearest-rank vs
linear).

## 9. Mock fidelity limits

Scope: what `mock_vllm.py` proves (URL/dim/plumbing, SSE shape, chaos/TTFT
knobs) and does NOT prove (lexical-hash embed is not semantic; chat echoes
hit-1; `/tokenize` cross-process variance; `mock_finish_reason` backdoor).
Green-with-mock is not prod-quality evidence.
