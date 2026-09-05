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
  recall@5/8 ≥ 0.95, MRR ≥ 0.95, nDCG@8 ≥ 0.95, identifier recall@1 and
  `message_id`-class recall@1 exactly 1.0 — plus the absolute invariant:
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
on for A/B runs. The paraphrase branch builds pages from `answer_text`
without echoing the query (see §6).

## 4. Layered harness (`harness.py`, `harness_l1/l2/l3.py`)

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
  + holdout over the pinned real corpus) and `benchmarks/harness.json`
  (hash venue: **dev golden set only**, `evals/golden.jsonl`, over the
  snapshot-pinned synthetic hash corpus — the Makefile harness targets pin
  `--golden` per mode, issue #158). The holdout is vllm-venue-only: its
  sibling-book traps (VER-09/10) are structurally unpassable in the
  synthetic hash corpus, whose sibling pages carry near-identical query
  text by corpus design ("deliberate lexical competitors") — recording
  them would bake a permanent P0 into the venue. Both venues: collection
  + hash-vs-vllm pairing is operator env (the baseline pins the snapshot,
  not the collection name).
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
  bench file. Gates fail closed on env mismatch (4 keys), demand zero
  errors and zero missing timings, and cap stage p95 at 3× baseline.
- Harness clients use a hardcoded 60s Qdrant timeout (distinct from the
  eval's settings timeout). Bootstrap CIs use linear-interpolated
  percentiles — note the load-tier percentile below is nearest-rank, so
  cross-tier "p95" values are incomparable by construction.

## 5. Answer eval (`eval_answers.py`)

In-process `/v1/answer` grounding honesty: deterministic stratified
round-robin sampling (sorted classes and ids, no RNG, small classes
revisited first; default 24 queries, `--all` for full runs), then judge:

- Answer behavior fails on empty bodies, explicit refusals (7 case-fold
  markers), or zero validated citations; abstain behavior fails only when
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
