# Evaluation, harness, and benchmark reference

Owner: this file. Test-writing rules: `docs/testing.md`. Live ladder:
`docs/live-stack.md`. Design overview: `docs/architecture.md` §5.

> One fact, one owner — this file owns eval internals. Code is named by
> module and script, never by line number.

## 1. Tier map

Each tier answers a different question; baselines, venues, and gates never
mix (see `testing.md` harness invariants):

| Tier | Script | Question | Venue |
|---|---|---|---|
| L1 gate | `gate_l1.py` | Does retrieval regress this PR? | CPU hash mode, ephemeral simulator, runtime synthetic corpus; required PR check |
| Retrieval eval | `eval_retrieval.py` | How accurate is retrieval on real data? | Live Qdrant, golden or holdout |
| Paraphrase | `eval_retrieval.py --golden evals/paraphrase.jsonl` | Do semantic changes move non-verbatim queries? | Dedicated collection, manual runbook |
| Answer eval | `eval_answers.py` | Are answers grounded and abstentions honest? | Live GPU stack, in-process client |
| Layered harness | `harness.py` + `harness_l1/l2/l3.py` | Promote to release candidacy? | Snapshot-pinned live index (RC only) |
| Bench | `benchmark.py` | Do resources/latencies regress? | CI runner env, mock LLM |
| Load | `loadtest.py` / `test_load_tier.py` | Do absolute contracts hold under concurrency? | Sim composition + real uvicorn agent |
| Corpus hygiene | `verify_golden.py` / `build_golden_corpus.py` | Is the golden set sound? | Live collection facts |

Supporting cast: `render_report.py` (text/md/HTML renders and comparators),
`bootstrap_ci.py` (paired-bootstrap CIs), `qdrant_sim.py` + `qdrant_pin.py`
(the only docker-lifecycle and pin-parse owners), `query_demo.py`
(inspection, never eval), `mock_vllm.py` (deterministic stand-in, §9),
`make_synthetic_pdf.py` (runtime-only fixture factory),
`test_local_e2e_vllm.py` (live-GPU manual precedent),
`smoke_search.py` (in-cluster smoke: limit 8, `--min-hits 1`, substring
`--expect` over lowercased cite/heading/text).

**Dev venue provenance** — the shared dev collections are synthetic and
rebuildable; never delete one without regenerating it in the same
session. `mainframe_manuals` is the combined golden + holdout venue:
regenerate in ONE pass (`gate_l1.generate_synthetic_golden_corpus` over
`golden.jsonl` + `holdout.jsonl` concatenated — 28 doc_ids are shared, so
separate passes would clobber each other's pages), then `run_ingest
--reingest` with a fresh `--progress` file (`--reingest` bypasses
inventory and Qdrant sha skips; doc_ids are stable across chunking
changes, so delete-first is unnecessary). `paraphrase-manuals` is the
`paraphrase.jsonl` venue (14 docs + generic-distractor), same rebuild.
Both re-ingested 2026-09-10 under current rules (post-#216): 208 pts
(160 narrative / 48 message) and 21 pts (16 narrative / 5 message).
`real_manuals` (435k pts, 452 real books) is NOT managed here: never
delete, re-ingest, or gate against it from dev workflows. Its sampled
mix is ~95% narrative / ~2% message / ~3% syntax / ~0% table — and the
dev venues hold no syntax/table chunks at all, so the #216 per-type BM25
boost (default 1.0) was unmeasurable outside the real corpus; its ON
decision has since been measured OFF on real-corpus record-replay pools
(see `retrieval.md` §4 for the verdict and reopen gate).

## 2. Retrieval eval (`eval_retrieval.py`)

Runs real `retrieve_search(limit=8)` — the 8 gives recall@5 headroom —
over a live collection and scores doc-level against the golden entries.

- **Relevance:** doc id in `expected_doc_ids`, plus the heading substring
  (case-folded) when the entry sets one. Answer entries must set expected
  docs; abstain entries must not, and stay out of recall/MRR denominators
  (their top-5 scores are recorded for calibration instead).
- **Graded gain:** +1 doc hit, +1 heading match, +1 page match. nDCG@8 is
  per-doc deduped (best chunk per doc wins — otherwise N chunks of one doc
  push DCG past IDCG, which is meaningless) against the entry's own ideal.
- **Per-query:** recall@1/3/5/8, MRR (`1/rank`), `page_hit@5`
  (doc-restricted, diagnostic only — never a gate). Per-query exceptions
  (`HTTPError`, `RuntimeError`, `OSError`, `ValueError`) count as failures
  and the run continues.
- **Traps:** `must_not` violations are collected inside the top-5 window
  (`MUST_NOT_WINDOW=5`) with the sibling allowance (a chunk co-carrying the
  query's own message id is the same documented page, not a violation).
- **Aggregation:** means (3 decimals), identifier-vs-NL kind split,
  per-class blocks, abstain top-score stats, `must_not` checked/violations,
  page stats.
- **Gates (ratios vs the mode-keyed baseline):** recall@1 ≥ 0.90,
  recall@5/8 ≥ 0.95, MRR ≥ 0.95, nDCG@8 ≥ 0.95. Identifier recall@1 and
  `message_id`-class recall@1 are no-drop gates: exactly 1.0 on the
  synthetic venue (where the recorded baseline is saturated), and "not
  below the recorded baseline" on a real-corpus instrument
  (`baseline._meta.collection` in `venue.RC_ONLY_COLLECTIONS`, issue #286)
  where the baseline itself is below 1.0 — plus the absolute invariant:
  `must_not.violations == 0` regardless of baseline.
- **Skip semantics:** missing metric or baseline warns and never gates; a
  **collection** mismatch skips the gate (baseline dropped); an
  **embed-mode** mismatch only warns and still gates. A requested gate
  that cannot be applied — missing `--check` file or collection mismatch
  — exits **2**, never 0 (issue #159): a skip is distinguishable from
  green, so `make eval` / `make eval-paraphrase` fail the job. Exit 0
  remains only for a genuinely green gated run or a run with no gate
  requested (`--no-check`, or no mode-keyed baseline recorded yet).
- **`--check` vs `--update-baseline` are mutually exclusive.** Baselines
  record `_meta` (size, collection, mode, timestamp) plus the gated metrics;
  `must_not`/failures/per-query are intentionally omitted (the zero-gate is
  absolute).

## 3. L1 gate (`gate_l1.py`)

The required PR check: starts an ephemeral Qdrant simulator, generates one
synthetic PDF per unique expected doc id (cover page + one page per entry,
verbatim `Identifier:`/`Term:`/`Syntax construct:` lines, plus a
generic distractor for abstains), ingests with `--workers 1` in
`EMBED_MODE=hash` + `ALLOW_HASH_MODE=true`, evaluates, renders the delta
table, and cleans up (drops the `gate-l1-<pid>` collection, removes the
temp dir, stops sims it started unless `--keep-sim`). An existing server
via `QDRANT_SIM_URL`/`QDRANT_URL` is reused; the environment is restored
afterward.

Fails on regressions **or** any query failure. `--rerank` flips reranking
on for A/B runs (leg order stays `RERANK_ENDPOINT_ORDER`, default
`score_first` — there is no gate flag for the order, by design (#252):
the gate forces hash mode, so leg-order numbers there would compare stub
legs; gateway-order A/Bs run the same gate with the env var set, after
`probe_gateway.py` recommends it). The paraphrase branch builds pages from
`answer_text` without echoing the query (see §6).

## 4. Layered harness (`harness.py`, `harness_l1/l2/l3/l4.py`)

Release-candidate promotion gate, never a PR gate. Fingerprint, restore, or
pin a Qdrant snapshot; run L1; deliver a `merge` / `hold` / `baseline`
verdict.

- **Snapshot semantics:** the pin is count + server-assigned name — a cheap
  drift guard, **not** a content pin. Same count with different vectors
  passes silently (known limit). `never` skips unconditionally (bypassing
  even the drift check); `always` restores the recorded snapshot or fails
  (a gate never pins live state); `drift` restores only on exact
  name-and-count match. Re-adoption creates a new snapshot and prunes
  strays. Restore verifies the post-restore point count.
- **Mode-keyed venues:** `benchmarks/harness-vllm.json` (vllm venue: golden
  + holdout over the snapshot-pinned **synthetic** corpus — the regenerated
  dev venue, not the real books) and `benchmarks/harness.json`
  (hash venue: **dev golden set only**, `evals/golden.jsonl`, over the
  snapshot-pinned synthetic hash corpus — the Makefile harness targets pin
  `--golden` per mode, issue #158). Venue architecture: dev ↔ synthetic
  (determinism and promotion signal), holdout ↔ real (`real_manuals` via
  `make eval-holdout` — the honest real-corpus semantic gate). The vLLM
  harness over 211 synthetic points saturates most classes at 1.0 and has
  accordingly little discriminating power; it guards determinism, not
  real-corpus quality. The holdout runs only under vllm mode — inside the
  vllm harness (synthetic venue) and as `make eval-holdout` against
  `real_manuals` (the semantic gate). The hash harness stays dev-only per
  #158: the synthetic hash venue's sibling pages carry near-identical
  query text by corpus design ("deliberate lexical competitors"). Both venues: collection + hash-vs-vllm pairing is
  operator env (the baseline pins the snapshot, not the collection name).
- **Verdict:** zero trap failures AND no per-class regression beyond the
  baseline floor (default 0.05) AND at least one primary metric (recall@5,
  MRR) improving with a paired-bootstrap 95% CI excluding zero (2000
  resamples, seeded). Pure parity refactors hold by design.
- **L1 metrics:** `L1_LIMIT=8` (matches the answer path's retrieval depth);
  recall@5/@8, MRR@8, shared nDCG@8 helper (no @1/@3 here, unlike the
  eval); abstain rows contribute traps only; trap precision divides by all
  rows.
- **L2 (answer tier):** reuses the answer-eval runner and adds citation
  precision/recall over exact cite-string joins (precision averages cited
  rows, recall counts zero-cite rows at 0; unmapped citations fail the
  row), a temp-0 NLI faithfulness judge over capped evidence
  (entailed/neutral/contradiction; unparseable output is a structural
  fail), truncation joined from `answer_alert` log lines by request id, and
  syntax-pattern checks. Only structural fails gate; rates are trend data.
  The L1-pinned-collection rule is operator discipline (unenforced in
  code).
- **L3 (perf tier):** per-stage p50/p95 from `Server-Timing` plus TTFT
  (requires `LLM_STREAM=true` on the agent) and `nvidia-smi` VRAM under
  concurrent load, against dedicated mode-keyed baselines — never the CI
  bench file. Gates fail closed on env mismatch (5 keys: `cpu_count`,
  `embed_mode`, `qdrant_image`, `gpu_name`, `concurrency`), demand zero
  errors and zero missing timings, and cap stage p95 at 3× baseline.
  `CONCURRENCY`/`DURATION`/`REQUEST_TIMEOUT` Make variables shape the load
  (recorded in `_meta.env`); a slow reasoning model under concurrency needs
  `REQUEST_TIMEOUT` above the 30s default or every request is a client-side
  error, not a latency sample. `benchmarks/harness-l3-vllm.json` is
  recorded (2026-09-11, RC stand-in host, `_meta.env` pins the tier); the
  hash file is intentionally not recorded — L3 is a GPU tier and the CI
  bench owns CPU-mode perf.
- **L4 (answer-quality gate):** `harness_l4.py` runs the L2 runner
  (`run_l2(..., relevance_enabled=True)`, one judging path) K times
  (`--repeats`, default 3) over the same deterministic sample and adds the
  answer-relevance judge (relevant/partial/irrelevant, no excerpts, no
  citation markers; unparseable output is a structural fail). Structural
  fails, request errors, and judge-infra errors fail in **any** repeat;
  per-metric means are compared against `evals/harness-l4-thresholds.json`
  with `_meta.tolerance`: at/better than the reference passes, inside the
  band holds and writes a human-review queue (`--queue`), outside fails.
  The default tolerance is 0.15 — ~2.3σ of the 3-repeat mean at N=24
  (measured: a 0.05 band flagged run-to-run sampling noise as rate
  regressions); raise N to tighten it, and treat sub-band movement as
  trend data, not a verdict.
  An uncomputed metric fails rather than vanishing. The reference records
  the venue/embed mode/reasoning model and the gate refuses (exit 2) when
  the live tier differs; record it with `make harness-l4-record`
  (dedicated PR). RC-only, never a PR gate.
- Harness clients use a hardcoded 60s Qdrant timeout (distinct from the
  eval's settings timeout). Bootstrap CIs use linear-interpolated
  percentiles — note the load-tier percentile below is nearest-rank, so
  cross-tier "p95" values are incomparable by construction.

## 5. Answer eval (`eval_answers.py`)

In-process `/v1/answer` grounding honesty: deterministic stratified
round-robin sampling (sorted classes and ids, no RNG, small classes
revisited first; default 24 queries, `--all` for full runs), then judge:

- Answer behavior fails on empty bodies, explicit refusals (7 case-fold
  markers), zero validated citations, or **only inferred citations** (the
  agent mapped bare `[n]` markers with no explicit citation line; issue
  #269). The run report counts `inferred_citations` so fabrication is
  visible, never silently grounded. Abstain behavior fails only when
  grounded *and* unrefusing (hedged or silent answers warn). Gold
  substring/identifier checks are case-fold literals, suppressed on the
  canned zero-hits path (judging fixed strings teaches nothing about the
  model).
- Non-200 responses record the error code only (no bodies); transport
  exceptions record errors. Exit 0 iff zero failures and zero errors.
  Deliberate non-features: no retries, no `finish_reason` checks, and the
  judge never re-parses citations (the agent validator is the single source
  of truth).

## 6. Paraphrase instrument

The main golden set echoes query text into target pages, so header-only
retrieval saturates and semantic improvements cannot register. The 22-entry
paraphrase set is the complementary instrument: operator-phrased queries
whose answers live in the corpus without near-verbatim echo, over lexical
competitors (sibling docs sharing vocabulary, intra-doc section pairs).

- No-echo is pinned hermetically: normalized query must not appear on the
  page, and no 8-word verbatim run (`MAX_VERBATIM_RUN=8`) may occur.
  Abstain entries generate distractor-only pages.
- Mode-keyed baselines (`baseline-paraphrase.json` hash,
  `-vllm.json` vllm, dedicated collection): the hash numbers are a plumbing
  anchor, the vllm numbers the semantic instrument — headroom below 1.0 is
  intentional. Same ratio tolerances as the main set. Runbook: generate the
  corpus, ingest into the dedicated collection, then evaluate — `make
  eval-paraphrase` runs only the check, not the ingest. Not wired into CI
  (no cluster, no embed server there); never tune against the frozen
   holdout.

### 6.1 Record-replay A/B (tune prod ranking from local)

Paraphrase measures semantic movement on synthetic pools; record-replay
measures ranking movement on real pools without needing prod models
locally. Capture runs where the models live (RC/gap), replay runs
anywhere (`tests/test_replay.py`, `scripts/capture_pool.py`):

```bash
# 1. Capture (RC/gap, live Qdrant + platform embed/rerank endpoints):
VENUE=rc make capture-pool          # records into bundles/pools-YYYYMMDD.jsonl
#    overrides: GOLDEN=evals/golden.jsonl OUT=/path/pools.jsonl
#    (the raw script also takes --no-ce / --max-queries; see its --help)
# 2. Carry pools.jsonl back (ids/ranks/scores only, never chunk text —
#    safe to move; still never tune against the frozen holdout).
# 3. Replay locally (record_to_rows → replay_pool → replay_rank): sweep
#    type-boosts, fusion alpha, and diversity caps hermetically, no GPU.
#    The sweep CLI runs the production chain per config and scores
#    doc-level recall/MRR against the tune golden:
#      python scripts/replay_sweep.py --pools bundles/pools.jsonl \
#        --golden evals/golden.jsonl --json bundles/sweep.json
```

Rules: CE-less pools (bypassed queries, `--no-ce`) replay RRF-only —
rerank refuses them fail-closed, never fabricate scores. Split
recordings replay per-leg (`record_to_rows(record, leg=i)`); merging
legs corrupts ranks. The sweep's ruler is doc-level only (pools carry no
headings/message ids), so a swept gain is a candidate: adoption requires
the live eval/holdout. Deltas ship in the PR body like any retrieval
change (`live-stack.md` rung 7); re-capture after any re-ingest (pools
pin rank order, not content). Pools stay in `bundles/`/scratch — never
committed (the real-corpus venue guard refuses `real_manuals` without
`VENUE=rc`; §10).

## 7. Golden corpus discipline

`golden.jsonl` (117 dev) and `holdout.jsonl` (70, sha256-pinned, verified
with `sha256sum -c` on RC-only `make eval-holdout`) are built from
`expert_golden_seed.jsonl` plus payload mining by `build_golden_corpus.py`:
manual bindings for out-of-pattern families, authored corrections,
forced-abstain ids, absent-trap ids, then a deterministic ~60/40 per-class
split (LEG entries always dev).

- `verify_golden` scrolls live payload and FAILs on missing
  expected/`must_not` docs, absent headings/pages, unbound message-id
  queries, `must_not`-inside-expected without the query id, and duplicate
  queries (case-folded); rarity, stratification, and hygiene WARN unless
  `--strict` (which also demands size and class coverage — and the default
  `make verify-golden` does not pass `--strict`, so weak traps ship on 0
  FAIL alone).
- Re-freeze process: rebuild, `verify-golden` 0 FAIL, new holdout sha,
  re-record baselines — one dedicated commit. Never iterate the holdout to
  tune.

## 8. Benchmarks (`benchmark.py`, `loadtest.py`)

`make bench` ingests a generated corpus (distinct bodies with unique
message ids, plus one plain doc; leftovers wiped; stale inventory deleted)
into the pinned Qdrant image, then loads a real uvicorn agent backed by the
mock LLM, measuring peak RSS, Qdrant container RAM/disk, and search/answer
latencies.

- Gated metrics with tolerances: RSS, Qdrant mem, Qdrant disk ×1.5;
  search/answer p95 ×3.0. Improvements never fail. Baselines refuse to
  record from broken runs.
- Env gating checks **two** keys (`cpu_count`, `qdrant_image`) — not four;
  cross-env runs become p95 hunts, so capture baselines in the gate's own
  environment (CI runners, repeats ≥3, min-latency/min-footprint and
  max-error aggregation).
- Measurement caveats (floors, not ceilings): RSS is max-single-process
  (underreports the tree); disk is real blocks (sparse mmap reads ~400MB
  empty); `peak_rss` degrades to the first pass. Load percentiles are
  nearest-rank (empty → 0.0); stages report p50/p90/p95/p99/max with
  per-request `Server-Timing` parsing, round-robin deterministic queries,
  and thread-local HTTP clients.
- The load tier (`test_load_tier.py`, PR-gated on agent/retrieve/ingest
  paths) asserts absolute contracts instead of correctness: zero errors,
  zero missing `Server-Timing`, per-stream SSE integrity (tokens → exactly
  one `final`, no `error`), citation parity across shapes, fixed error
  envelopes, determinism after load, abort-storm survival (complete XOR
  aborted per stream, one `stream_truncated` alert each), and a TTFT floor
  under a paced mock. Fail-closed: no skips, agent stdout to a file.

## 9. Mock fidelity limits

`mock_vllm.py` is the only stand-in, and green-with-mock is plumbing
evidence, not quality evidence:

- Embedding is a bag-of-hashes lexical vector (same spirit as the hash
  embedder, not the same bytes; no query prefix, no model semantics) —
  proves URL/dim/plumbing and fail-fast only. `MOCK_DIM` must equal
  `DENSE_DIM`.
- Chat derives its body from hit-1 text and echoes hit-1's cite so parsing
  passes by construction — always cites hit-1, proving nothing about
  grounding honesty, rerank quality, or semantics.
- `/tokenize` ids are stable across processes (blake2b, issue #160) — but
  they are still mock ids, not real vLLM BPE ids, so budget assertions must
  treat them as shape-only evidence, never as ground-truth tokenization.
- Knobs (`MOCK_TTFT_MS`, interval/jitter/seed, `MOCK_ERROR_RATE`,
  `mock_finish_reason` backdoor) default to byte-identical zeros; scope is
  chat-only, with embeddings/tokenize/health/models instant and infallible.

## 10. Real-corpus cadence (RC)

The per-PR gate runs hash mode on a synthetic corpus that saturates; the
real-corpus holdout is the only discriminating semantic instrument, and
the answer tiers are the only grounding instruments. They run on a
schedule, never as a PR gate (`live-stack.md` §0/§5.2).

**Venue declaration.** Every eval/harness entry point defaults to the dev
venue: `evals/golden.jsonl` only, synthetic collections only. The frozen
holdout (`evals/holdout.jsonl`) and the real-corpus collection
(`real_manuals`) require `VENUE=rc` — set by the `make eval-holdout`
recipe itself, and by operators for the harness tiers
(`VENUE=rc make harness-l2`). Without the declaration the scripts exit 2
("frozen holdout … requires VENUE=rc"), so a truncated venue is never
scored. `scripts/venue.py` owns the rule.

**Triggers** — run the battery when any of these fires:

- an RC cut (the promotion decision);
- a re-ingest or golden/holdout re-freeze on the real corpus;
- a model or gateway change (embed, rerank, reasoning);
- a ranking-constant change (capture → replay A/B before merge).

**Battery** (retrieval before answer quality):

| Step | Command | Artifact / verdict |
|---|---|---|
| 1. holdout semantics | `make eval-holdout` | `$(BUNDLE_DIR)/eval-holdout-{report.json,summary.md}` + manifest |
| 2. L1 promotion | `VENUE=rc make harness-gate` | report + manifest; `merge`/`hold` |
| 3. answer tier | `VENUE=rc make harness-l2` | report + summary + manifest; structural fails gate |
| 4. judge gate | `VENUE=rc make harness-l4` | report + summary + review queue; `pass`/`hold`/`fail` |
| 5. perf tier | `VENUE=rc make harness-l3` | report + manifest vs the L3 baseline |
| 6. pool capture | `VENUE=rc make capture-pool` | dated `pools-YYYYMMDD.jsonl` (never committed) |
| 7. hermetic replay | `pytest tests/test_replay.py` + a replay sweep | A/B deltas in the PR body |

L4's reference (`evals/harness-l4-thresholds.json`) is recorded with
`VENUE=rc make harness-l4-record` on the RC tier and ships in a dedicated
PR. L2/L4 standing red on known product debt is an RC debt signal, not a
broken target.

**Dated record.** Each battery run appends a row here in the same PR or
RC checklist that runs it (`$BUNDLE_DIR` reports are local). Run
manifests (`evals/runs/*.jsonl`, gitignored) carry the timestamp, git
SHA, settings hash, model ids, Qdrant version, and snapshot id — the
committed row is the pointer, the manifest is the detail.

| Date | Git SHA | Venue | Steps | Result |
|---|---|---|---|---|
| 2026-09-02 | pre-#268 | `real_manuals` | `harness-l2` N=24 | 10 structural fails; grounded 0.76, citation precision 0.16, truncation 0.59 — standing red debt (`testing.md` harness section) |
| 2026-09-11 | 9469d4f | `real_manuals` | `harness-l4-record` N=24×3, then `harness-l4` gate | reference recorded (grounded 0.70, citation P/R 0.60/0.40, truncation 0.43, syntax 0.33, entailment 0.35, relevance 0.97); gate fail on 32 structural fails with no rate outside the 0.15 band, 20-row review queue — standing RC debt |
| 2026-09-11 | 4db737a | `real_manuals` | `harness-l3-baseline` + `harness-l3` gate (C=8, request timeout 300s) | vllm baseline recorded (search p95 151 ms; answer p95 110 s, ttft p95 108 s under 8-way concurrency on the 8 GB stand-in); gate pass |
| 2026-09-11 | 632ad3e | `real_manuals` | `harness-l4-record` N=24×3 under strict grounding (#269) | reference re-recorded: grounded 0.48 (was 0.70 — inferred cites no longer count), citation P/R 0.63/0.39, truncation 0.49, entailment 0.29, relevance 0.96; 37 structural fails — standing RC debt |
| 2026-09-12 | d15fbca | `real_manuals` | `eval_retrieval --no-check` split 2×2, 70-query frozen holdout (vllm, rerank off) | comparative split ON: class r@1 0.286→0.429, class MRR 0.429→0.536, overall r@1 0.603→0.619, overall MRR 0.700→0.712, 0 must_not violations → flipped default-ON; diagnostic neutral-negative (class r@1 flat 0.571, class MRR 0.655→0.643, overall r@5 0.873→0.857) → stays off |
| 2026-09-12 | 5a952b7 | `real_manuals` | `make eval-holdout` (70-query frozen holdout, vllm, rerank off) | doc-family filter ON: `doc_number` r@1 0.667→1.0 (DOC-03 0→1.0, DOC-04 0→1.0); overall r@1 0.619→0.651, r@5 0.873→0.889, MRR 0.712→0.739; only those two queries moved; 0 violations; gate green |
| 2026-09-12 | 96f013d | `real_manuals` | `replay_sweep` over dev golden, pools captured with CE at depth 80 | tune baseline (n=104) r@1 0.683 / r@5 0.923 / MRR 0.792; fuse 24→32→40 identical, `page2` r@5 0.904/MRR 0.769, `doc4` neutral, `doc2` +1/104, rerank alphas r@1 ≤0.673 — no adoption, production config stands |
