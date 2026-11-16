# Evaluation, harness, and benchmark reference

Retrieval scoring and measured execution are owned by
`mainframe_rag.eval.retrieval`: `evaluate` constructs the configured clients,
measures each selected query and reports query failures; `collect_rows` uses
the caller's clients for L1 and propagates query failures. The retrieval CLI, simulator gate and
promotion harness consume the canonical operations directly. Client imports occur when an
operation is called. CLI handling, label drafting, simulator lifecycle and
snapshot/restore orchestration remain in their operational scripts.

Answer-tier measurement is owned by `mainframe_rag.eval.answers`: deterministic
sampling, request-attributed `AnswerCapture`, structural judging, aggregation,
and report writing share that import identity with L2. The supported
`python -m mainframe_rag.eval.answers` command calls `main(argv)` in a prepared
installation. Supply explicit `--golden`, `--out`, and `--summary` paths outside
a workspace; default datasets remain relative to the working directory and
missing files fail without acquisition. Venue guards apply before live work.

L2 judge prompts, bounded evidence assembly, citation mapping, label parsing and
judge-call parameters live in `mainframe_rag.eval.judging`. L2 orchestration and
L4 repeated quality gates remain distinct; both consume the canonical labels.
L2 row enrichment, request-attributed alert capture, aggregation, structural
verdicts and measured execution live in `mainframe_rag.eval.answer_tier`.
L4 imports this runner directly. Importing the measurement package does not
start the app or connect to model/storage services. L2's CLI retains dataset/venue resolution and
output/manifest composition. The synthetic HTTP runner test covers per-run search
caching, judge-leg order/eligibility, request attribution, cleanup and the next run.

L4 threshold validation, repeat aggregation, quality verdicts and recorded
reports are owned by `mainframe_rag.eval.quality`, with explicit inputs and
paths. `scripts/harness_l4.py` retains live repeat execution, CLI defaults,
venue checks and the explicit record-versus-check operation. It consumes the
quality owner directly; moving ownership does not change
rate thresholds, structural failure gates, sampling or repeat counts.

Seeded bootstrap intervals are owned by `mainframe_rag.eval.statistics`.
The promotion policy imports that owner directly.

Pure promotion verdicts and their metric/floor constants are owned by
`mainframe_rag.eval.promotion`. The harness calls that owner; baseline persistence,
snapshots and CLI execution stay in `scripts/harness.py`.

Multi-turn condensation A/B measurement is owned by `mainframe_rag.eval.chat`,
using the shared dataset and retrieval scorers. `python -m mainframe_rag.eval.chat`
runs its `main` in a prepared installation.  Outside a workspace, supply explicit golden/output paths; the default
`evals/golden.jsonl` stays relative to the working directory. Importing the module does not
execute a session.

Reusable concurrent HTTP load measurement, Server-Timing parsing, percentiles
and optional GPU observations are owned by `mainframe_rag.eval.load`. The L3
harness, ingestion benchmark and load integration tier consume that owner.
`scripts/loadtest.py` retains CLI dispatch and baseline persistence, including
reserved CI-baseline protection.
The `eval:load` Task delegates endpoint, worker-count, duration and absent-URL
defaults to this CLI: search, eight workers, 30 seconds and
`http://127.0.0.1:8080`. Explicit `AGENT_URL` values (including empty) are
transported literally; CLI Task inputs override ambient values. `PY` still
selects the base interpreter and no model-mode or venue defaults are exported.
Relative URLs retain the existing diagnostic behavior: request errors
appear in the report; an ordinary run without baseline export still exits zero.
Query load and ingestion benchmark workloads remain distinct. L3 verdicts and
report formatting live in `mainframe_rag.eval.performance`.
Its `default_baseline_path(workspace_root, embed_mode)` takes explicit inputs;
the CLI supplies its existing workspace root and reads `EMBED_MODE` on each
invocation. This replaces the script's import-time `EMBED_MODE` and
`DEFAULT_L3_BASELINE` snapshots. Missing, empty and non-vllm modes keep the hash
filename, case-insensitive vllm keeps its dedicated filename, and explicit
`--baseline` still takes precedence. Environment capture, baseline persistence
and live load orchestration remain operational script concerns.
Both L3 Task commands delegate baseline defaults to this Python operation.
`HARNESS_L3_BASELINE` supplies an explicit override only when present (CLI
Task input wins over ambient input); an explicitly empty override remains an
empty `--baseline` argument and does not fall back to the mode default.
Case-insensitive mode selection therefore agrees between Task and direct
Python. Gate runs never record baselines; recording remains explicit.

Rendering and comparison of recorded retrieval/benchmark JSON are owned by
`mainframe_rag.eval.reports`. The L1 gate imports this renderer directly;
`python -m mainframe_rag.eval.reports` runs its `main` in a prepared installation.
Text, Markdown, HTML, escaping, comparison rules and exit codes are
unchanged. The renderer consumes supplied files and never reruns evaluation.
Explicit report/baseline/output paths remain authoritative. The opt-in
`--bundle-dir DIR` selects the existing Task defaults: `DIR/eval-report.json`
and `evals/baseline.json`, or `DIR/bench-report.json` and
`benchmarks/baseline.json`. These report baselines are fixed, never selected by
embedding mode. Render subcommands with HTML format also default output to
`DIR/<family>-report.html`; comparison subcommands keep stdout unless `--out`
is supplied. Python prepares the HTML bundle and output parent and prints the
written path once (the duplicate Task echo is retired).

Without `--bundle-dir`, the renderer still requires `--report`, or both `--base`
and `--current` for comparison; a render baseline remains optional and HTML
still goes to stdout unless `--out` is given. Task transports only nonempty
path overrides: explicit empty overrides restore defaults even over ambient
values. Direct empty Path arguments still refuse rather than defaulting.
For compatibility, an empty `BUNDLE_DIR` retains root-relative default reads
for text/compare and is refused for HTML directory preparation. With a distinct output path, input JSON bytes remain unchanged; explicit
output aliases retain the existing writer behavior. Missing/corrupt inputs
fail before an existing output is replaced.

The #508 compatibility delegates are retired: `scripts/venue`,
`scripts/bootstrap_ci`, `scripts/harness_l1`, `scripts/eval_answers`,
`scripts/eval_chat` and `scripts/render_report`. Known callers and Task
dispatch use the package owners above; old script commands/imports, including
`_AnswerCapture`, are deliberately removed. Direct module commands require a
prepared editable or wheel installation. The eight module Tasks (answers, chat,
and six report operations) bind `PYTHONPATH=src` at the checkout root, selecting
this checkout's canonical modules even with a foreign editable installation or
inherited `PYTHONPATH`. Dependencies still require preparation; help names
follow the module.

Retained retrieval/harness/load scripts own operational composition and no
longer provide compatibility-only helper exports. Import measurement and
policy helpers from their package owners above.

Owner: this file. Test-writing rules: `docs/testing.md`. Live ladder:
`docs/live-stack.md`. Design overview: `docs/architecture.md` §5.

> One fact, one owner — this file owns eval internals. Code is named by
> module and script, never by line number.

<a id="tier-map"></a>
## 1. Tier map

Each tier answers a different question; baselines, venues, and gates never
mix (see `testing.md` harness invariants):

| Tier | Script | Question | Venue |
|---|---|---|---|
| L1 gate | `gate_l1.py` | Does retrieval regress this PR? | CPU hash mode, ephemeral simulator, runtime synthetic corpus; required PR check |
| Retrieval eval | `eval_retrieval.py` | How accurate is retrieval on real data? | Live Qdrant, golden or holdout |
| Paraphrase | `eval_retrieval.py --golden evals/paraphrase.jsonl` | Do semantic changes move non-verbatim queries? | Dedicated collection, manual runbook |
| Section probes | `eval_retrieval.py --golden evals/sections.jsonl`; record-replay §6.1 | Does retrieval reach the right section of the right manual, not only the right manual? | Real-corpus collections only (no synthetic pages exist for it); dev-only, never part of golden/holdout |
| Answer eval | `python -m mainframe_rag.eval.answers` | Are answers grounded and abstentions honest? | Live GPU stack, in-process client |
| Layered harness | `harness.py` + `harness_l2/l3/l4.py` (`eval.retrieval` owns L1, `eval.quality` owns L4) | Promote to release candidacy? | Snapshot-pinned live index (RC only) |
| Bench | `benchmark.py` | Do resources/latencies regress? | CI runner env, mock LLM |
| Load | `loadtest.py` / `test_load_tier.py` | Do absolute contracts hold under concurrency? | Sim composition + real uvicorn agent |
| Release acceptance (#367) | `eval_acceptance.py` | Does the production profile meet the pre-registered absolute criteria on the independent SME set? | `VENUE=rc`, sha-pinned `release_set.jsonl`, adjudicated outcomes; stand-in or proposed criteria never certify (§11) |
| Corpus hygiene | `verify_golden.py` / `build_golden_corpus.py` | Is the golden set sound? | Live collection facts |

Supporting cast: `eval.reports` (text/md/HTML renders and comparators),
`eval.statistics` (paired-bootstrap CIs), `qdrant_sim.py` + `qdrant_pin.py`
(the only docker-lifecycle and pin-parse owners), `query_demo.py`
(inspection, never eval), `mock_vllm.py` (deterministic stand-in, §9),
`make_synthetic_pdf.py` (runtime-only fixture factory),
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
`paraphrase.jsonl` venue, same rebuild. Point counts and class mixes follow
the current entries and chunking rules; do not copy them between sessions.
`real_manuals` (the preserved real-corpus collection) is NOT managed here:
never delete, re-ingest, or gate against it from dev workflows. Its sampled
mix is mostly narrative, with little message/syntax/table — and the dev
venues hold no syntax/table chunks at all, so the #216 per-type BM25 boost
(default 1.0) was unmeasurable outside the real corpus; its ON decision has
since been measured OFF on real-corpus record-replay pools (see `retrieval.md`
§4 for the verdict and reopen gate).

<a id="retrieval-eval"></a>
## 2. Retrieval eval (`eval_retrieval.py`)

Runs real `retrieve_search(limit=8)` — the 8 gives recall@5 headroom —
over a live collection and scores doc-level against the golden entries.
Retrieval runs with `comparative_split_enabled=True` by default, activating
multi-path retrieval for comparative queries with the per-side document merge
(`interleave_split_hits`, `retrieval.md` §3b),
while `diagnostic_dualpath_enabled` remains default-off.

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
- **Gate coverage (issue #367):** an applied gate requires positive total and
  eligible scored counts, finite required metrics, and the zero-violation
  invariant. Every class with a positive baseline `n` must be observed; a
  positive baseline `scored` requires scored observations. Non-null baseline
  class metrics require finite current values, without introducing new
  per-class quality thresholds. Intentionally unscored abstention classes keep
  null recall/MRR and remain outside those denominators; their presence and
  trap checks still count. Missing required scores, NaN/Infinity, and empty or
  abstention-only scoring fail with exit **1**. Existing tolerances are unchanged.
- **Skip semantics:** missing `--check` files and collection or embedding-mode
  mismatches exit **2**. A supplied baseline must select at least one finite
  relative or identifier comparison metric; empty, metadata-only, all-null,
  and report-only policies cannot certify a requested gate (exit **2** with
  an explicit unavailable-rule reason, unless scoring already failed).
  Sparse policies and zero-valued comparison metrics remain valid; the pure
  `check_baseline` helper retains its absolute-only use with `{}`.
  A skipped gate is not a pass. Direct retrieval CLI JSON reports and run
  manifests record `gate.status` (`passed`, `failed`, `skipped`, or
  `not_requested`); the text summary labels the verdict. Exit 0 means a green
  applied gate or explicitly non-certifying diagnostics (`--no-check`, or no
  mode-keyed baseline recorded yet). Baselines lacking a metric do not invent
  a threshold for it. The existing baseline identity covers collection/mode;
  it does not attest exact platform weights or semantic model acceptance.
- **Retrieval input selection (#508):** `eval.datasets.default_retrieval_paths`
  owns the workspace-relative dataset/baseline pair for `--suite dev` (default)
  or `--suite paraphrase`; its unused baseline-only compatibility delegate is retired.
  It uses the current invocation's normalized Settings
  mode: vllm selects the `-vllm` baseline; other modes select the hash-family
  filename. An explicitly empty mode remains empty, without promising a usable
  model backend. Explicit `--golden`, `--check PATH`, and
  `--update-baseline PATH` remain authoritative.
  A bare `--check` requires the selected default baseline (missing means exit 2);
  a bare `--update-baseline` explicitly records that default. A run with query
  failures is never recorded: the script prints
  `FAIL: refusing to record a baseline from a run with query failures (N failed)`
  and leaves the baseline untouched, while the report, summary and run manifest
  still record the failed run and its failures decide the exit code (1).
  Every report carries `representation`: the serving gate's own verdict on the
  configured collection (`agent.serving.resolve_published_generation`:
  representation contract and immutable publication). A servable run is
  measured through the validated physical, exactly as the agent binds requests.
  A non-servable run (for example a different `EMBED_MODEL_REVISION` than the
  collection was built with) still prints trend numbers with a warning, but a
  requested gate is skipped with exit 2 and `--update-baseline` refuses (exit 2);
  `scripts/gate_l1.py` fails the same way, and the chat eval
  (`mainframe_rag.eval.chat`) records and binds to the same verdict. Omitting both
  retains the legacy implicit check only when the default exists.
  Task `eval:retrieval`, `eval:paraphrase`, and `eval:baseline` pass suite and
  operation intent to this owner. This fixes raw-mode YAML selection disagreeing
  with Settings for mixed-case/space-padded vllm, including recording into the
  wrong baseline. Existing Task `EVAL_BASELINE`/`PARAPHRASE_BASELINE` inputs were
  ignored by their unconditional definitions; explicit paths use the Python CLI.
  Approved baseline contents, scoring and holdout access rules are unchanged.
- **`--check` cannot be combined with `--update-baseline`, `--no-check`,
  or `--label-draft`.** Conflicting modes fail before accessing the venue. Baselines
  record `_meta` (size, collection, mode, timestamp) plus the gated metrics;
  `must_not`/failures/per-query are intentionally omitted (the zero-gate is
  absolute).

<a id="l1-gate"></a>
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

Fails on regressions **or** any query failure. `--update-baseline` records
the run as the baseline only when it had no query failures; otherwise it
prints `ERROR: --update-baseline refused: N query failures occurred; baseline
file left unchanged: PATH`, leaves the baseline untouched, still writes
`--out`/`--summary`/`--delta` and the report on stdout, and exits 1 (the
same contract as `eval_retrieval.py` above, #1211/#1213). `--rerank`
flips reranking on for A/B runs (leg order stays `RERANK_ENDPOINT_ORDER`, default
`score_first` — there is no gate flag for the order, by design (#252):
the gate forces hash mode, so leg-order numbers there would compare stub
legs; gateway-order A/Bs run the same gate with the env var set, after
`probe_gateway.py` recommends it). The paraphrase branch builds pages from
`answer_text` without echoing the query (see §6).

<a id="layered-harness"></a>
## 4. Layered harness (`harness.py`, `harness_l2/l3/l4.py`)

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
- **Mode-keyed venues:** `benchmarks/harness-vllm.json` (vllm venue: **dev
  golden set only**, 127 entries, over the snapshot-pinned G4 corpus
  `colab_manuals_qwen3_8b_4096_ced400a7` — 234,610 pts, Qwen3-Embedding-8B dim
  4096; re-recorded 2026-10-23, see the §10 row; a gate run that adds the
  holdout under `VENUE=rc` does not match its entry set, #859) and `benchmarks/harness.json`
  (hash venue: **dev golden set only**, `evals/golden.jsonl`, over the
  snapshot-pinned synthetic hash corpus; re-recorded 2026-10-22, see the §10 row). Python now selects both the
  mode baseline and this dataset default for Task and direct invocations
  (#508 C3, preserving the #158 venue contract). The harness-specific
  `resolve_harness_golden_paths` uses the normalized Settings mode: hash
  defaults to dev golden only even under RC, while vllm uses the ordinary
  venue-controlled set. This corrects direct hash+RC including holdout and
  Task mixed-case vllm selecting the hash inputs. Explicit Python `--golden`
  and `--baseline` remain authoritative; holdout still requires RC and its
  approved pin. The Task commands continue to ignore `HARNESS_BASELINE`
  inputs; they never exposed an effective override. L2/L4/answer defaults,
  snapshot policy and promotion criteria are unchanged. Venue architecture:
  harness ↔ real
  (`real_manuals` promotion signal with discriminating power) alongside
  holdout ↔ real (`real_manuals` via `sh scripts/tools/run-task.sh eval:holdout` — the honest
  real-corpus semantic gate); the synthetic dev venue (`mainframe_manuals`,
  214 pts) currently has no vLLM harness pin, so a synthetic `harness-gate`
  fails closed until one is re-recorded. The holdout runs only under vllm
  mode — inside the vllm harness (real venue) and as `sh scripts/tools/run-task.sh eval:holdout`
  against `real_manuals` (the semantic gate). The hash harness stays dev-only per
  #158: the synthetic hash venue's sibling pages carry near-identical
  query text by corpus design ("deliberate lexical competitors"). Both venues: collection + hash-vs-vllm pairing is
  operator env (the baseline pins the snapshot, not the collection name).
- **Verdict:** zero trap failures AND no per-class regression beyond the
  baseline floor (default 0.05) in recall@1, recall@5 or MRR AND at least one
  primary metric (recall@5, MRR) improving with a paired-bootstrap 95% CI
  excluding zero (2000 resamples, seeded). Pure parity refactors hold by
  design. recall@1 (#837) takes part only in the per-class regression check,
  and only where the baseline class block records it: a baseline without the
  key (or with a null) reads as missing, never as 0. It is deliberately not a
  paired-CI primary metric: that would let a first-rank gain promote while
  recall@5 and MRR are flat, and would hold every baseline recorded before
  the key ("no paired values"), so primary metrics and thresholds are unchanged.
- **L1 metrics:** `L1_LIMIT=8` (the answer path's retrieval depth before
  #1274, which made it 16; the gate's own cutoff, unchanged);
  recall@1/@5/@8, MRR@8, shared nDCG@8 helper (no @3 here, unlike the
  eval); abstain rows contribute traps only; trap precision divides by all
  rows. recall@1 (#837) is reported per class, overall and per query and is
  compared per class where the baseline records it (see Verdict).
  `benchmarks/harness.json` (re-recorded 2026-10-22) and
  `benchmarks/harness-vllm.json` (re-recorded 2026-10-23) both record it, so
  both are gated on it per class.
- **L2 (answer tier):** reuses the answer-eval runner and adds citation
  precision/recall over exact cite-string joins (precision averages cited
  rows, recall counts zero-cite rows at 0; unmapped citations fail the
  row), a temp-0 NLI faithfulness judge over capped evidence
  (entailed/neutral/contradiction; unparseable output is a structural
  fail), truncation joined from `answer_alert` log lines by request id, and
  syntax-pattern checks. The judge runs at an explicit low reasoning effort
  (#305): with the server-default effort the model put its chain-of-thought
  in the content channel on long answers and the JSON label never appeared,
  failing rows as judge infra errors. Only structural fails gate; rates are
  trend data. The L1-pinned-collection rule is operator discipline
  (unenforced in code). The summary also carries the issue #365 acceptance
  shape: `by_verification_state` (served state per judged row; `unknown`
  means a pre-state response, never a fabricated state), `state_mismatches`
  (rows whose entry opted into `expected_verification_state`), and
  `faithfulness_by_class` (entailment per query class, so procedure and
  version-sensitive support read apart from the aggregate) — all
  report-only.
- **L3 (perf tier):** per-stage p50/p95 from `Server-Timing` plus TTFT
  (requires `LLM_STREAM=true` on the agent) and `nvidia-smi` VRAM under
  concurrent load, against dedicated mode-keyed baselines — never the CI
  bench file. Gates fail closed on env mismatch (5 keys: `cpu_count`,
  `embed_mode`, `qdrant_image`, `gpu_name`, `concurrency`), demand zero
  errors and zero missing timings, and cap stage p95 at 3× baseline.
  `CONCURRENCY`/`DURATION`/`REQUEST_TIMEOUT` Task inputs shape the load
  (recorded in `_meta.env`); a slow reasoning model under concurrency needs
  `REQUEST_TIMEOUT` above the 30s default or every request is a client-side
  error, not a latency sample.   `benchmarks/harness-l3-vllm.json` is
  recorded (2026-09-11, RC stand-in host, `_meta.env` pins the tier) and
  `benchmarks/harness-l3.json` (hash) was recorded 2026-09-12 against
  `real_manuals_hash` — a hash-vector mirror of `real_manuals` (same
  435057 ids and payload, vectors rebuilt with the production hash
  builders; the 256-vs-1024 dim gap means hash can never query the vLLM
  collection). The hash leg isolates agent/LLM/Qdrant perf from the embed
  server (search p95 75ms vs 163ms vllm at record time); the CI bench
  still owns CPU-mode perf trends.
- **L4 (answer-quality gate):** `harness_l4.py` runs the L2 runner
  (`run_l2(..., relevance_enabled=True)`, one judging path) K times
  (`--repeats`, default 3) over the same deterministic sample and adds the
  answer-relevance judge (relevant/partial/irrelevant, no excerpts, no
  citation markers; unparseable output is a structural fail). Structural
  fails, request errors, and judge-infra errors fail in **any** repeat;
  per-metric means are compared against `evals/harness-l4-thresholds.json`
  with `_meta.tolerance`: at/better than the reference passes, inside the
  band holds and writes a human-review queue (`--queue`), outside fails.
  Gating strictly enforces 9 metrics defined in `GATED_METRICS`: `grounded_rate`,
  `citation_precision`, `citation_recall`, `truncation_rate`, `syntax_compliance`,
  `faithfulness.entailed`, `faithfulness.contradiction`, `relevance.relevant`,
  and `relevance.irrelevant`. Note that `answer_completeness` is excluded
  from gating (it is reported only as observational trend data in run summaries),
  as are the issue #365 state aggregates (`by_verification_state` summed across
  repeats and `state_mismatches`) — the threshold file's key set stays exact,
  so recording a reference can never silently adopt them.
  The default tolerance is 0.15 — ~2.3σ of the 3-repeat mean at N=24
  (measured: a 0.05 band flagged run-to-run sampling noise as rate
  regressions); raise N to tighten it, and treat sub-band movement as
  trend data, not a verdict.
  An uncomputed metric fails rather than vanishing. The reference records
  the venue/embed mode/reasoning model and the gate refuses (exit 2) when
  the live tier differs; record it with `sh scripts/tools/run-task.sh eval:harness:l4-record`
  (dedicated PR). RC-only, never a PR gate.
- Harness clients use a hardcoded 60s Qdrant timeout (distinct from the
  eval's settings timeout). Bootstrap CIs use linear-interpolated
  percentiles — note the load-tier percentile below is nearest-rank, so
  cross-tier "p95" values are incomparable by construction.

<a id="answer-eval"></a>
## 5. Answer eval (`python -m mainframe_rag.eval.answers`)

Sample/repeat defaults belong to the Python parsers: answers and L2/L4 use
24 queries, chat uses 12 sessions, and L4 uses 3 repeats. Task transports
nonempty `N`/`REPEATS` values literally and omits the option for missing or
empty values; an explicit empty Task input overrides a nonempty ambient value
and restores the parser default. Zero stays zero, and malformed values reach
the parser and fail. Direct CLI empty integer arguments remain invalid.
`tests/test_eval_task_sampling.py` exercises the actual parsers, sampling and
repeat loops, reports and quality verdicts with synthetic runtime boundaries.
Existing zero-sample exit differences remain: answers/L2 emit empty results;
chat refuses an empty sample and L4 cannot gate/record uncomputed metrics.
None of these empty runs establishes semantic acceptance.

`/v1/answer` grounding honesty: deterministic stratified
round-robin sampling (sorted classes and ids, no RNG, small classes
revisited first; default 24 queries, `--all` for full runs), then judge.
The default executes the local app through FastAPI TestClient and its live
lifespan-built clients. For an already deployed agent, pass `--agent-url`:

```bash
VENUE=rc .venv/bin/python -m mainframe_rag.eval.answers \
  --agent-url http://127.0.0.1:8087 \
  --golden "$PRODUCTION_GOLDEN_PATH" --all \
  --out "$LAB_STATE/answer-report.json" \
  --summary "$LAB_STATE/answer-summary.md"
```

The HTTP mode sends the same sibling `/v1/search` and scored `/v1/answer`
requests to that target. It uses no local app lifespan, Settings, Qdrant or
model clients. Task `eval:answers AGENT_URL=http://127.0.0.1:8087` exposes the
same optional target with its existing dataset/sample inputs; an unset or
explicitly empty `AGENT_URL` selects TestClient. Protected dataset/holdout
venue and pin checks still apply;
use the declared venue and explicit private golden path for production-corpus
measurement. The agent URL accepts an HTTP(S) base with an optional Route
prefix; URL credentials, query strings and fragments are rejected. Use the
authorized local forward or trusted TLS endpoint. Redirects and environment
proxy routing are disabled. `--request-timeout` sets the HTTP client timeout
for search/answer calls (1–1800 seconds, default 300); the pre-scoring health
call uses 10 seconds. The owning run controller supplies the overall deadline.
Requests are single-shot, without retries. For long generations (for example
`xhigh`), set the agent's `REQUEST_DEADLINE_S` below `--request-timeout`: a
client timeout alone leaves the agent generating, while expiry cancels its model
leg and frees the slot ([§6a](agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374)).

Before scoring, HTTP mode reads the target's actual `/healthz` response and
requires HTTP 200, `status=ok`, Qdrant ready, no failed embedding/rerank leg,
and the serving owner's `compatible` or `record_only_drift` representation.
The ready-but-`empty` bootstrap state cannot certify an evaluation. An
unhealthy, non-servable, malformed or unreachable target exits 2, sends no
scored requests, and retains every selected case as an error row in the
report. Missing/null required answer or citation fields are malformed, including
on abstention cases. Per-case HTTP, transport or malformed-response errors remain rows,
later cases still run, and any error makes the final exit nonzero. Existing
rates retain their judged-row meanings; `metrics.queries` counts all selected
cases and `metrics.errors` exposes unsuccessful ones. The report also carries
the coverage fields added under issue #1216, each counted over every selected
case:

- `total`: every case the run selected, the same count as `metrics.queries`;
  judged, error and unjudged rows alike.
- `unjudged`: rows with no pass, fail or error verdict, so
  `judged + errors + unjudged == total`.
- `error_rate`: `metrics.errors` divided by `total`; null on an empty run.
- `judged_rate`: `metrics.judged` divided by `total`, the share of selected
  cases that actually received a pass or fail verdict; null on an empty run.

Every pass rate is over judged rows only: `unjudged` and `error` rows are in no
rate denominator, so read the rates together with these fields rather than
inferring coverage from the rate alone. Metrics from a run built before these
fields exist — an older report file, for example — stay readable: without them
the text summary prints the answer pass rate line in its previous shape, with
the pass rate and its judged-row count only.

HTTP reports and trend manifests record the target URL, observed fixed health
fields and pre-scoring representation verdict. `/healthz` exposes no immutable
build SHA, image digest, model/configuration or collection identity: absent
deployment fields remain unknown. Trend manifests separately label the
evaluator checkout SHA as `evaluator_git_sha`; it never attributes the deployment.
Retain the deployment lane's actual image/bundle/configuration/corpus receipt
beside these private results before comparing builds. HTTP mode cannot join
the remote process's answer logs. Its default JSON request therefore leaves
the finish reason and token counts unknown; `--stream` reads them from the SSE
`final` instead ([measurement protocol](#measurement-protocol)). The
packing-attribution fields (`budget_verified`, `units_omitted`, query
complexity) stay log-only and unknown over HTTP. Answer and
golden content in reports remains private, outside git and public CI.

Sampling temperature (issue #596): the answer eval sends `temperature=0` by
default and records it per row, in the report `params`, and in the run
manifest — production `llm_temperature` (0.2) is unchanged. L2/L4 stay on
the server default (`temperature=None`): their thresholds were recorded
under it, so moving them to 0 needs its own threshold re-record PR.
Comparison rule: compare single runs at temperature 0, or repeat the
changed rows N≥3 times at the production temperature (`--temperature 0.2`)
and compare the distributions; vLLM at 0 is steadier, not
bit-deterministic, so expect few verdict flips across repeats, not zero:

- Answer behavior fails on empty or citation-scaffolding-only bodies (the shared
  body-presence predicate, #576, including empty `Answer` headings and citation
  labels followed only by indices or a citation, even when the server labels the
  row `accepted`; substantive text after labels still counts as prose),
  true abstentions (the agent's shared `is_abstention`, which also zeroes
  citations, #135/#630/#764: the structured decline (the fixed whole-question
  prefix plus the missing subject) leading the response, or a whole-response refusal; a grounded partial answer whose scope
  caveat carries a refusal phrase is not an abstention), zero validated
  citations, or **only inferred citations** (the agent mapped bare `[n]`
  markers with no explicit citation line; issue #269). The run report
  counts `inferred_citations` so fabrication is visible, never silently
  grounded. `inferred_index_off_gold` maps an inferred index to the sibling
  `/v1/search` pool rank; since issue #364 only supplied excerpts can be
  inferred and prompt labels are the retrieval ranks of the packed prefix,
  that mapping stays exact — an omitted hit can no longer surface at all.
  Abstain behavior keeps the marker test: fails only when
  grounded *and* unrefusing; a grounded decline (hedged) or a silent
  abstention warns. Gold substring/identifier checks are case-fold
  literals, suppressed on the canned zero-hits path (judging fixed strings
  teaches nothing about the model).
- `answer_completeness` (issue #305): over judged answer rows whose
  expected docs were in the fetched pool, the share whose validated
  citations cover every expected doc (doc-level recall 1.0). A refusal or
  partial answer over retrieved gold is incomplete; rows without the pool
  join stay out of the denominator. Reported by the answer eval and both
  harness summaries for observational tracking of the reader-side
  refusal/partial-answer track (excluded from `harness_l4` threshold gating).
  The answer eval fetches the same sibling `/v1/search` pool as L2 and joins
  doc-level citation recall per row (`apply_pool_join`, observational: an
  unmapped citation never fails the row here), so it reports a number instead
  of `None` (#307; a caller or failed search without a pool stays out of the
  denominator). Non-200 rows record the envelope's top-level `code`
  (`prompt_budget_exceeded`, `invalid_request`, ...; the eval previously read a
  nested key that never exists and printed `?`) - code only, never the message.
- Acceptance states (issue #365): every dev golden row opts into
  `expected_verification_state` via `build_golden_corpus.expected_state_for`
  — answer-tier rows expect `accepted` (substantive parsed prose + eligible
  non-inferred supplied citations + `stop`),
  abstain-tier rows expect `insufficient_evidence`; adjudicated exceptions go
  in `EXPECTED_STATE_OVERRIDES` with a note. A mismatch is a row failure: the
  acceptance set states the product requirement, it is not a description of
  current behavior. The frozen holdout opts in only through the adjudicated
  re-freeze process (§7). `by_verification_state` reports the served
  histogram and `state_mismatches` the opt-in failures.
- Refusal/unsafe split (issue #365): `false_refusal_rate` is the share of
  judged answer-tier rows failing as explicit refusals (a correct answer was
  expected); `unsafe_answer_rate` is the share of judged abstain-tier rows
  failing at all — currently broader than "answered a trap" (it includes
  gold-substring/identifier misses on abstain rows), deliberately kept
  separate so neither rate hides behind the other's pass rate.
- Non-200 responses record the error code only (no bodies); transport
  exceptions record errors. Exit 0 iff zero failures and zero errors.
  Deliberate non-features: no retries, no `finish_reason` verdict (it is
  measured, never judged), and the judge never re-parses citations (the agent
  validator is the single source of truth).

<a id="measurement-protocol"></a>
### Measurement protocol for answer changes (#1195)

Confirmed by the maintainer on 2026-11-13. The site serves a different
reasoning model on different hardware behind a platform gateway, so a result
measured here carries over only through quantities that do not depend on the
host.

- **Two groups, reported apart.** The report's `metrics.measurement` block and
  the summary's two tables keep them separate; the
  [metric contract](metric-contract.md#answer-measurement-groups) defines each
  value, its source and its limits.
  - Hardware-independent: pass rate per class, truncation rate, and prompt,
    completion and reasoning tokens per answer (p50/p95).
  - Hardware-dependent: time to first token, decode tokens per second and
    end-to-end time (p50/p95).
- **Decide on the first group only.** Quality and budget decisions never read
  a timing. Time at a site is predicted as tokens divided by that site's own
  measured tokens per second (its in-pod gateway probe) and then checked there.
- **Venue.** A validated Colab G4 lane for quality. The site-shaped gateway is
  a candidate venue for plumbing only: on the pinned adapter it likely rejects
  `reasoning_effort`, so `run_local_stack.sh` stops a quality lane whose
  startup probe fails. A quality run requires a probe that reaches `/v1/answer`
  with a judged row, and #1193's argument probe must record the actual site's
  parameter behavior before any site-shape arm is used as a baseline
  ([inputs](colab-playground.md#measurement-inputs)). The 8 GB local pair is
  for plumbing and relative checks only.
- **Method.** Sweeps run a fixed class-interleaved subset three times; a
  winner is confirmed by one full pass. The holdout is never used for tuning
  (§7).

Flags (defaults are unchanged without them):

| Flag | Effect |
|---|---|
| `--stream` | HTTP mode only. Sends the same request with `stream: true` and judges the SSE `final`, which already carries `finish_reason`, `ttft_ms` and `usage` ([agent contract](agent.md), §3 Streaming). Rows gain finish reason, token counts, `ttft_ms`, the client-side `client_first_token_ms` and `decode_tps`. Token deltas and the `final`'s retrieval hits are not kept. An `error` event, a stream that ends without `final`, or a `final` without valid counts is an error row with a fixed label. The in-process client buffers the whole response, so the flag is refused without `--agent-url`; in-process rows keep their answer-log join |
| `--subset N` | The default sampler's round-robin (sorted classes, sorted ids, one row per class per round), so every cutoff of its order keeps every class. Refused (exit 2, no request) when `N` is below the class count, and refused before any read when the resolved dataset contains the frozen holdout: a sweep needs an explicit non-holdout `--golden`. Exclusive with `--all`. `params.sample` records the mode, row count, class count, a SHA-256 over the ordered ids, and the resolved datasets' basenames + SHA-256, so two arms can prove they ran the same rows over the same splits |
| `--repeats R` | 1–10 whole passes over the sample (pass 1 over every row, then pass 2, ...), so drift within a session lands on every row equally. Rows carry `repeat`; `metrics` pools all passes; `metrics.repeats` gives, per metric, the per-pass values with min, max, mean and range, plus `verdict_flips` (entry ids judged differently across passes). More than one pass is a sweep, so the same non-holdout `--golden` rule applies |
| `--endpoint chat` | Issue #1191. Each question goes as a single-turn streaming `POST /v1/chat` (`messages: [{role: user, content}]`, `stream: true`, `temperature`), the route the Open WebUI Pipe calls. Needs `--stream`. `params.endpoint` records `answer` (default) or `chat`; `params.transport` still means `json`/`sse`. Differences are listed under [chat rows](#chat-rows) |
| `--coverage` | Issue #1191. Before the rows, pages `GET /v1/sources` (limit 200) once and records its `build_id`; after the rows, reads the first page again as `build_id_end`. Each row gets `coverage` (`all`, `some` or `none` of its `expected_doc_ids` served; `not_applicable` when it names none; `unknown`) and `expected_served`; `report.coverage` holds the counts per state over distinct rows, `pages` and `sources_n`. The match is on the exact doc id, edition included. A 503 (a build published without a catalog), a 404, a transport error, a malformed page, a build change between pages or 1,000 pages give `status: unknown` with a fixed `reason`. Coverage never changes a verdict or the exit code |
| `--hit-limit N` | Issue #1191. The deployed `ANSWER_HIT_LIMIT` as the operator declares it (the agent does not report it; 1–32, the agent's bounds): the sibling `/v1/search` pool behind `gold_retrieved`, `hit_doc_ids` and citation precision/recall asks for `N` hits, so those fields see as deep as the answer did. Default the agent default (16 since #1274, 8 before); recorded as `params.hit_limit` only when passed. Never pass a value that differs from the deployed one |
| `--export PATH` | Issue #1191. A JSON file built by whitelist for leaving an air-gapped site: `params` (without `agent_url`), `metrics`, `healthz`, `representation`, `coverage` and one row per result with id, repeat, class, behaviour, verdict, failure and warn labels, verification states, finish reason, path, citation-WHY mode, citation and inferred-index counts, token counts, timings, `gold_retrieved`, expected/hit/cited doc ids, citation precision/recall, coverage, and `supplied_n` / `supplied_doc_n` (#1191: how many excerpts the response's `supplied_evidence` lists and from how many distinct manuals, on both endpoints; 0 on the zero-hits path, None when the field is missing or an entry has no doc id). Failure labels come from a closed set (anything else is `other`; an error code outside the agent's stable codes is `?`), finish reasons outside the OpenAI set are `other`, and a `build_id` of an unexpected shape is null. No question, answer, citation, excerpt, script or failure text. `tests/test_eval_site.py` seeds canaries into every text field and checks the export bytes |

Without `--stream`, the JSON request is unchanged: the row still records the
model-leg `llm_ms` (and `ttft_ms` when the agent runs with `LLM_STREAM`) from
the existing `Server-Timing` header, and the `generation_incomplete` rate is
reported for every transport as an upper bound on truncation. Token counts
stay unknown there, never zero.

A difference between two arms that is smaller than the range either arm shows
across its own repeats is noise at this sample size; confirm it with the full
pass or do not claim it.

```bash
# One arm of a sweep: 24 interleaved rows, three passes, counts from the stream.
# The explicit non-holdout --golden is mandatory: --subset/--repeats refuse a
# resolved set that contains the frozen holdout (exit 2, before any request).
VENUE=rc .venv/bin/python -m mainframe_rag.eval.answers \
  --agent-url http://127.0.0.1:8087 --golden "$PRODUCTION_GOLDEN_PATH" \
  --stream --subset 24 --repeats 3 \
  --out "$LAB_STATE/arm-a.json" --summary "$LAB_STATE/arm-a.md"
# Confirm the winner: one full pass.
VENUE=rc .venv/bin/python -m mainframe_rag.eval.answers \
  --agent-url http://127.0.0.1:8087 --golden "$PRODUCTION_GOLDEN_PATH" \
  --stream --all --out "$LAB_STATE/confirm.json" --summary "$LAB_STATE/confirm.md"
```

Sweep order, one variable at a time, each arm against the same subset
identity:

1. Site-shaped candidate: today's defaults behind `--gateway-shape site`, only
   after #1193's argument probe records the site's actual parameter behavior.
   It is not a quality baseline before then (see **Venue**).
2. Effort and chat-template arguments.
3. Window 32k, 64k and 128k, each with and without more evidence.
4. Concurrency, raised until the cache or latency breaks.

Rows run sequentially, so the eval itself measures single-request behaviour;
the concurrency step varies the served `--max-num-seqs` and drives load with
the load tools (§8), reading this eval's hardware-independent group for the
quality side. A live Colab session has not run this protocol yet; one is
required before it gates a change.

<a id="chat-rows"></a>
**Chat rows (`--endpoint chat`).** The chat stream ([agent contract](agent.md),
§3) ends with a chunk whose `choices[0]` carries `finish_reason`, `citations`,
`citations_inferred`, `inferred_indices`, `verification_state`, the script
flag and `supplied_evidence`, then `[DONE]`. The row is judged by the same
`judge()` on those fields. Differences from `/v1/answer` rows:

- **Body.** The stream has no answer field: its deltas are the model's raw
  output followed by the validated `**Citations:**` list. The eval drops that
  list and runs the agent's own `parse_answer` over the rest with the
  terminal chunk's `supplied_evidence`, which is how the agent derives the
  `/v1/answer` body. `tests/test_eval_site.py` checks through the real app
  that both endpoints give the same body, citations, state and verdict for the
  same model output. Against an older agent this is the RC's parser over that
  agent's output.
- **Unavailable, recorded as null (never a failure or a zero):** token counts,
  `ttft_ms` and `decode_tps` (the chat chunks carry no `usage` and no
  `ttft_ms`). Truncation still comes from `finish_reason`;
  `client_first_token_ms` is the first content delta.
- **Completion.** A terminal chunk and `[DONE]` are both required, as in the
  Pipe; an error frame is an error row (`stream error (<code>)`) although
  `[DONE]` follows it.
- **Not measured.** The eval sends `temperature`; the Pipe sends none, so the
  agent's configured default applies to users. The Pipe's own rendering
  (hiding the model's citation section, banners) is not scored.

<a id="at-a-site"></a>
### At a site (before/after an upgrade)

Issue #1191; maintainer decisions of 2026-11-16. Measures answer quality on
the site's own model before and after an in-place upgrade, with the
candidate's eval against both builds. Code reaches the site only inside the
release bundle, so the instrument is the candidate's checkout.

Since #1274 the answer-quality features are on by default: follow-up
anchoring (`CHAT_FOLLOWUP_ANCHOR_ENABLED`), a 4000-character prior-turn cut
(`CHAT_MAX_PRIOR_TURN_CHARS`), the `auto` prompt count
(`LLM_TOKEN_COUNT_SOURCE`) and the 262144-token window default
(`LLM_MAX_MODEL_LEN`). No pre-merge evidence exists for them on the site
model; this visit is the deciding measurement. The release's defaults are
the baseline. Each [ablation arm](#site-ablation-arms) switches one feature
off in `airgap.env`. A feature that hurts stays off in `airgap.env`, with no
new release; flipping its code default back is a later decision.

**Step 0, before any session or arm.** Record the deployed `LLM_*` values
(the receipt below), then run the two probes in the agent pod:

```sh
oc -n "$NS" get deploy rag-agent \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | grep -E '^(LLM_|CHAT_|REQUEST_)' | grep -v -E 'KEY|SECRET'   # unset = the default
oc -n "$NS" exec deploy/rag-agent -c agent -- python3 /app/scripts/probe_gateway.py --probe-args
oc -n "$NS" exec deploy/rag-agent -c agent -- python3 /app/scripts/probe_gateway.py --probe-count
```

`--probe-args` shows, per argument set, whether the served model honours the
reasoning effort: compare `reasoning_tokens` across the effort candidates. If
answers come back with near-zero reasoning tokens at every effort, the effort
may not be reaching the model. When an argument set moves the reasoning
tokens, an effort arm follows the ablations (`LLM_EXTRA_BODY` /
`LLM_EFFORT_TRANSPORT`, [model request arguments](agent.md#model-request-arguments)).
`--probe-count` says whether `/tokenize` or the completion-usage count serves
this gateway, which decides what the `count-tokenize` arm can change. It
exists from #1266 on: on f117160c run `--probe-args` only, and run
`--probe-count` after the candidate is deployed, before the first B
session.

**Where.** The bastion pod (devkit image, Python 3.14 with the dev lock),
through `oc port-forward` with the operator's namespace session. The Route is
not usable (OAuth port; the eval refuses credentials and redirects). No
cluster object changes.

1. Bootstrap the candidate's workspace before deploying it
   ([upgrade step 3](install_and_ops.md#upgrade-and-recovery-runbook-issue-272): fresh workspace, `bootstrap.sh`),
   so the same eval runs against the old build first.
2. Forward the agent and run the module directly, from a scratch working
   directory (the run manifest goes to `<cwd>/evals/runs/`):

```sh
oc -n "$NS" port-forward svc/rag-agent 8087:8080 &   # keep it running
WS=/work/<candidate>/qdrant-pdf-rag                  # the candidate workspace
mkdir -p /work/site-eval && cd /work/site-eval
run() {  # $1 = session label, $2 = endpoint, then extra flags
  label=$1 endpoint=$2; shift 2
  PYTHONPATH="$WS/src" python3 -m mainframe_rag.eval.answers \
    --agent-url http://127.0.0.1:8087 --golden "$WS/evals/golden.jsonl" \
    --stream --temperature 0 --request-timeout 600 --coverage \
    --endpoint "$endpoint" \
    --out "$label-$endpoint.json" --summary "$label-$endpoint.md" \
    --export "$label-$endpoint.export.json" "$@" 2>"$label-$endpoint.log"
}
run T0 answer --subset 24 --hit-limit 8   # timed 24-row run first (on f117160c)
run A1 answer --all --repeats 2 --hit-limit 8   # the full session, both endpoints
run A1 chat --all --repeats 2 --hit-limit 8
```

- Always pass `--golden evals/golden.jsonl` explicitly. Never the holdout:
  `--repeats` refuses it anyway.
- `--request-timeout` must exceed the deployed `REQUEST_DEADLINE_S` (the
  chart ships 270 s), so the agent's own deadline, not the client, ends a
  slow answer.
- Run off-hours: a 503 from user traffic is an error row.
- Time the 24-row run first. A session is 127 rows × 2 repeats × 2 endpoints,
  so the two endpoints double its cost. If the full session does not fit, the
  fallback is `--subset 24 --repeats 3`, which only catches gross changes.

**Session plan.** All at temperature 0, both endpoints, same flags:

| Session | Build | When |
|---|---|---|
| A1 | old (f117160c) | the day before the upgrade |
| A2 | old | just before deploying |
| B1 | candidate | after the post-deploy smoke |
| B2 | candidate | the next day |
| C | candidate | after B2: one pass at `--temperature 0.2 --repeats 1`, both endpoints; it confirms, it never decides |

A is the release before #1274 (f117160c, every feature off); B is this
release with its defaults. A1 against A2 is the noise between sessions on an
unchanged build. Every A run passes `--hit-limit 8`: f117160c retrieves
8 hits per answer, and the eval's own default is the candidate agent's
16, so without it the A sibling pool (`gold_retrieved`, citation recall)
would be deeper than the answer it scores. The B runs pass neither flag.
A follow-up run (`--turns 2`) against f117160c also passes
`--prior-cap 1000`, its deployed cut (the candidate's default is 4000).
Check that `params.hit_limit` is 8 in every A export and absent in every
B export, that
`coverage.build_id` equals `build_id_end` in every export, that both A
sessions name the same build, and that `params.sample.ids_sha256` and the
dataset sha256 match across all sessions.

**What may leave the gap.** Only the `*.export.json` files and a receipt: the
old and candidate commit SHAs, the agent image digests, the model name and the
non-secret `LLM_*` and `REQUEST_*` values. Review each export before it
leaves; this prints 0 when no golden question and no follow-up question
appears in it (a doc_number question that is itself part of a doc id is
skipped, since doc ids may leave):

```sh
PYTHONPATH="$WS/src" python3 - "$WS/evals/golden.jsonl" A1-answer.export.json <<'PY'
import json, sys
from mainframe_rag.eval.follow_up import every_follow_up_text
rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
ids = " ".join(d for row in rows for d in row.get("expected_doc_ids") or [])
text = open(sys.argv[2]).read()
asked = [row["query"] for row in rows] + every_follow_up_text()
print(sum(query in text and query not in ids for query in asked))
PY
```

`--out`, `--summary`, the stderr log and `evals/runs/` stay in the gap: they
hold questions, answers, citations and failure text.

**Follow-up answers (issue #1268).** `--turns 2` (needs `--endpoint chat
--stream` and `--arm`) adds a second turn after every answer row that has
expected docs; trap and gold-less rows end after turn 1. Turn 2 is the class
follow-up from `mainframe_rag/eval/follow_up.py` (one owner, shared with
`eval.chat`; none names a message id, member or abend code, so
`condense_query` never bypasses the rewrite), sent with the history the Open
WebUI Pipe would send. The evaluator runs its own checkout's
`integrations/openwebui/docs_pipe.py` on the recorded turn-1 frames: the kept
answer is the model prose without its own citations section, followed by the
agent's validated `**Citations:**` list (source events are not part of the
message). The turn-2 body is the Pipe's `build_request`/`fit_request` plus
`temperature`, as on every chat row. A missing Pipe file, or one without the
seams the evaluator drives (`Pipe._transport`, `build_request`, `fit_request`,
`DEFAULT_MAX_BODY_CHARS`), refuses the run (exit 2) before any request.

- Turn-2 retrieval is read from the terminal chunk's `supplied_evidence`
  (what the agent packed), never from a search of the follow-up text.
  `evidence_gold_hit` is that set against the opener's `expected_doc_ids`; an
  empty set (zero hits) is a known miss. Turn-2 citations map to doc ids
  through the same entries (`evidence_citation_precision`/`_recall`).
- Turn-2 pass/fail is structural only (a body, no refusal, explicit validated
  citations). The opener's gold substrings, identifier and expected state are
  written for the opener's question and are not applied: read the evidence
  hit, `vs_opener`, the path and the verification states first.
- The existing metric blocks cover turn 1 only, so they read like a
  single-turn chat run over the same sample. `metrics.by_turn` compares the
  openers that got a follow-up (`"1"`) with their follow-ups (`"2"`):
  evidence hit and rate, zero hits, refusals, verification states, citation
  precision/recall, the structural pass rate, `vs_opener` (kept, lost,
  gained, neither per session), `prior_answer_chars` (how many kept answers
  exceed the application's default prior-turn cut,
  `chat_max_prior_turn_chars`, 4000 since #1274 and 1000 before; without
  `--follow-up` the count always uses that default, so an arm
  that sets the operator input `CHAT_MAX_PRIOR_TURN_CHARS` (#1268) must read it
  against its own cut) and, with
  `--repeats`, the spread per repeat. An opener that errored gives a turn-2
  error row (`opener not answered`), and a Pipe that cannot build the history
  gives `follow-up history failed`; both are counted, not dropped. The exit
  code covers both turns: 1 when any row of either turn fails or errors, even
  when the turn-1 blocks report no failure.
- Export rows gain `turn`, `prior_answer_chars` and the `evidence_*` fields;
  params gain `turns`, `arm`, `follow_ups_sha256` and `pipe_sha256`. Without
  `--turns 2` neither the export nor the report changes.
- The agent does not report whether it condenses, so `--arm` is the
  operator's declaration (1-32 of `a-z 0-9 . _ -`). Check the deployed value
  before each arm, as below. `answer_taxonomy.py` takes single-turn reports
  only (it refuses a repeated row id).

**Follow-up kinds.** Follow-ups fail in three ways: a follow-up that needs
new evidence (A), one about the previous answer itself (B), and a change of
topic that the history should not disturb (C). `--follow-up` (needs
`--turns 2`) takes `all` or a comma list of the kinds below. Every kind
branches off the same turn-1 opener and the same kept answer, so each
eligible row costs one opener plus one call per kind. Without `--follow-up`
the run is the anaphoric run above and its output is unchanged.

| Kind | Class | Turn 2 asks | Who gets it | Turn-2 pass means |
|---|---|---|---|---|
| `anaphoric` | A | the class follow-up | every answer row with expected docs | structural pass, as above |
| `switch` | C | another sampled row's own question | the same rows, when a partner exists | that row's full gold verdict |
| `restate` | B | to repeat the exact command, message or name from the last answer | openers that passed and have `gold_must_contain` | every opener gold string in the answer |
| `summary` | B | to sum up the last answer in three bullets | the same as `restate` | the same as `restate` |

- **Pairing (switch).** The partner is the next eligible row in sample order,
  wrapping around, that shares no expected doc id with the opener. No seed is
  used, so the same sample always pairs the same way. The partner is also
  asked as an opener in the same pass. `vs_single` compares each switched
  question with that same question asked alone in the same repeat: pass,
  evidence gold hit and verification state are reported as kept, lost,
  gained, neither or unknown (either side errored), and state changes as
  `from>to` counts. A switch row's `coverage` is that of the question it
  asked. This measures
  harm from history, condensation or anchoring. A switched question may
  name an identifier, so condensation is bypassed for it, exactly as for a
  user's new standalone question.
- **Class B.** `restate` and `summary` pass when the answer holds every
  opener gold substring (case-folded, the judge's rule) and is not a
  decline. The agent answers from fresh excerpts only, so an uncited
  restatement is the product rule and is not a failure. A decline (the
  zero-hits path, the agent's structured decline or a whole-response
  refusal) fails as `follow-up declined`, even when it names a gold string,
  and is counted in `restate.declines` (over `restate.n` judged rows); its
  gold strings count in `n` but never as `found`. Each gold
  string is placed against the prior-turn cut in the history answer as sent
  (stripped, pasted excerpts dropped, as the agent does):
  `before_cut` (wholly inside the kept head), `after_cut`, or
  `not_in_history`. It is crossed with whether the follow-up's packed
  evidence holds an opener gold doc. That evidence side is a doc-level
  proxy: `supplied_evidence` carries doc ids, not excerpt text.
- **Prior-turn cut.** The agent does not report `chat_max_prior_turn_chars`,
  so `--prior-cap` declares it (200-5000, default the agent default: 4000
  since #1274, 1000 before;
  recorded as `params.prior_cap`). Exposure per turn-2 row:
  `history_over_cap`, and `citation_list_cut` (`kept`, `partial`, `cut` or
  `absent`): where the Pipe's appended `**Citations:**` list lies against
  the cut. A test pins this model to the agent's own prompt builder.
- **Skips.** Turn-2 rows exist only for follow-ups that were asked. Skips are
  counted per kind in `skipped`: `no_partner`, `no_gold_strings` and
  `opener_not_passed`. Class-B denominators depend on turn-1 outcomes, so
  read `n` beside every rate. An errored opener still gives `opener not
  answered` rows for `anaphoric` and `switch`.
- **Output.** `metrics.by_turn` becomes turn x kind: `by_turn["1"][kind]`
  covers the openers that got that kind and `by_turn["2"][kind]` covers its
  follow-ups. Kinds are never pooled. Each block also carries `timing`
  (`elapsed_ms` and `client_first_token_ms` distributions; `ttft_ms` and
  `total_tokens` are None, because the chat stream carries neither). Turn-2
  blocks carry `exposure` and `skipped`, plus `vs_single` (switch) or
  `restate` (class B). Every export row gains `follow_up_kind` (None on turn
  1), `switch_id`, `history_over_cap`, `citation_list_cut`,
  `restate_declined`, `gold_n`, `gold_found_n` and `gold_split` (counts
  only; None where they do not apply). Params gain
  `follow_up`, `prior_cap`, `restate_follow_ups_sha256`,
  `switch_pairs_sha256` and `switch_pairs_n`.
- **Exit code.** The rule is unchanged: 1 when any row of any turn or kind
  fails or errors. Class-B rows fail whenever a gold string is not restated,
  so with `--follow-up` an exit of 1 is expected. It is not the arm signal.

<a id="site-ablation-arms"></a>
**Ablation arms (one visit).** The arms run on the candidate build, after
the B sessions or on a quiet day. The baseline is the release's defaults
(nothing below set in `airgap.env`). Each arm switches one feature off with
one `airgap.env` line and a redeploy; the previous arm's line is removed
first. Every arm uses the run function above (temperature 0, `--coverage`)
with the same sample flags:

```sh
show() {  # the deployed value of one agent setting (empty = unset, the default)
  oc -n "$NS" get deploy rag-agent -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name==\"$1\")].value}"; echo
}
vals() { for v in CHAT_CONDENSE_ENABLED CHAT_FOLLOWUP_ANCHOR_ENABLED CHAT_MAX_PRIOR_TURN_CHARS LLM_TOKEN_COUNT_SOURCE; do show $v; done; }
vals   # all empty: the defaults (anchoring on, 4000, auto)
run H-baseline chat --all --repeats 2 --turns 2 --follow-up all --arm baseline
# airgap.env: CHAT_FOLLOWUP_ANCHOR_ENABLED=false; `sh scripts/tools/run-task.sh airgap:deploy`;
# wait for the rollout; vals, then:
run H-anchor-off chat --all --repeats 2 --turns 2 --follow-up all --arm anchor-off
# remove it; CHAT_MAX_PRIOR_TURN_CHARS=1000; deploy; vals:
run H-history1000 chat --all --repeats 2 --turns 2 --follow-up all --arm history1000 \
  --prior-cap 1000
# remove it; LLM_TOKEN_COUNT_SOURCE=tokenize; deploy; vals:
run K-count-tokenize answer --all --repeats 2
run K-count-tokenize chat --all --repeats 2
# remove it; ANSWER_HIT_LIMIT=8 and RETRIEVE_MAX_CHUNKS_PER_DOC=3; deploy; vals and
# show ANSWER_HIT_LIMIT RETRIEVE_MAX_CHUNKS_PER_DOC:
run D-depth-8-3 answer --all --repeats 2 --hit-limit 8
run D-depth-8-3 chat --all --repeats 2 --hit-limit 8
# remove both and deploy: the site ends on the release defaults.
```

| Arm | `airgap.env` line | Compare with |
|---|---|---|
| `anchor-off` | `CHAT_FOLLOWUP_ANCHOR_ENABLED=false` | `H-baseline` |
| `history1000` | `CHAT_MAX_PRIOR_TURN_CHARS=1000`, run with `--prior-cap 1000` | `H-baseline` |
| `count-tokenize` | `LLM_TOKEN_COUNT_SOURCE=tokenize` | B2 (same flags, same build) |
| `depth-8-3` | `ANSWER_HIT_LIMIT=8` and `RETRIEVE_MAX_CHUNKS_PER_DOC=3`, run with `--hit-limit 8` | B2 |

- `--prior-cap` must equal the deployed cut: omit it on every arm but
  `history1000` (the default is the agent default, 4000), and never pass a
  value that differs from the deployed one.
- `count-tokenize` changes only where `/tokenize` is not served (step 0's
  `--probe-count`); where it is served, `auto` already counts with it and the
  arm can be skipped. It acts on single-turn answers, so it runs the session
  flags, not the follow-up flags. Compare `verification_state`,
  `finish_reason` and the budget fields with B2.
- `depth-8-3` switches the retrieval depth (#1191) back to 8 hits and 3
  chunks per manual; it runs the session flags, single-turn
  ([depth arm](#site-depth-arm)).
- Condensation is no longer an arm. `CHAT_CONDENSE_ENABLED=true` still works
  and turns the anchoring default off; setting both true is refused at deploy
  and at startup.
- Before the window default rose to 262144 (#1274), `LLM_MAX_MODEL_LEN` was
  32768. Record the deployed value in step 0; it must not exceed the served
  window.

Cost: per answer row with expected docs, an arm asks one opener and up to
four follow-ups. Class-B follow-ups go only to passed openers with gold
strings. Time the baseline arm on `--subset 24` first. If `--all
--repeats 2` does not fit every arm, run every arm with the same
`--subset 24 --repeats 3`.

Reading the arms. First check that `params.sample.ids_sha256`,
`follow_ups_sha256`, `restate_follow_ups_sha256`, `switch_pairs_sha256` and
`pipe_sha256` are equal across the follow-up exports, and that `prior_cap`
differs only in `history1000`. Then compare each kind on its own signal:

- `anaphoric`: `by_turn["2"]["anaphoric"]`: `evidence_gold_hit_rate` and
  `vs_opener`.
- `switch`: `vs_single`: `lost` against `gained` for pass and evidence.
  Gains on anaphoric that come with more switch losses are a trade.
- `restate`/`summary`: `pass_rate`, `restate.declines` and
  `restate.gold_split` (`after_cut` found should move only with a smaller
  cap).
- `exposure.over_cap_share` and `citation_list_cut_share` per arm.
- `timing` p50 per turn and kind is the arm's cost on this host
  (hardware-dependent; never the deciding signal).

A difference inside either arm's own repeat range is noise. The export's
`repeats` blocks give that range for the evidence hit, pass rate and
citation recall only; for `vs_single`, `restate.declines` and `gold_split`,
recount each repeat from the export rows (`repeat`, `follow_up_kind`,
`switch_id`, `verdict`, `evidence_gold_hit`, `gold_split`). Only the four
`*.export.json` files leave the gap (review each as above); the stderr log,
`--out` and `--summary` hold questions, follow-ups and answers and stay. Any
default change after the comparison is a separate maintainer decision.

<a id="site-depth-arm"></a>
**Retrieval depth arm (issue #1191, `depth-8-3`).** Not the Pipe's answer
depths (#1255, reasoning effort). Since #1274 the release retrieves 16 hits
per answer and keeps up to 5 chunks per manual; the arm switches both back
to 8 and 3 together, single-turn on both endpoints, against B2 (the same
build at its defaults, no `--hit-limit`: the eval's default is the agent
default, 16):

- `ANSWER_HIT_LIMIT=16` (default): twice the earlier depth. At the 262144
  default window the packed evidence does not bind; on a site whose served
  window is smaller and set in `LLM_MAX_MODEL_LEN`, a 32768 window leaves a
  packing budget of about 21,100 tokens after the fixed preamble, so a
  denser server count trims the tail excerpt (recorded as omitted).
  Scaling the stand-in's 8-hit p95 of about 7,600 prompt tokens gives about
  15,000 at 16 (static estimate; read `prompt_tokens` p95 from B2).
- `RETRIEVE_MAX_CHUNKS_PER_DOC=5` (default; `/v1/search` included): the
  section gap is within the gold manual, and the per-manual cap is what held
  its further sections out. In a replay of the recorded stand-in pools (17
  section rows, RRF only), a gold section reached the top 8 in 8 rows at 3,
  9 at 4 and 10 at 5 or 6, and depth alone (16 at 3) added one row, at rank
  13. On the dev set, document-level recall@5 moved from 113 to 111 of 117
  at 5; recall@8 stayed at 114.
- `--hit-limit 8` on the arm keeps the sibling pool as deep as the arm's
  answer; B2 runs without it.
- Above 12 excerpts the supplied-evidence text total (48000 characters) can
  leave a cited source with empty text in the Open WebUI Pipe
  ([bounds](agent.md#supplied-excerpts)); count such sources in B2.

Compare `D-depth-8-3` with B2 per endpoint: `gold_retrieved` rate, citation
recall, pass rate by class, refusals (abstentions and
`insufficient_evidence` on answer rows), `prompt_tokens` and `elapsed_ms`
p50/p95, and `supplied_n`/`supplied_doc_n`. A gain inside either side's
repeat range is noise. If depth hurts, the site keeps the two lines in
`airgap.env`; changing the code default back is a separate maintainer
decision.

The earlier two-arm condense comparison (`--arm condense-off`/`condense-on`
without `--follow-up`) remains valid. Its exports keep the anaphoric-only
shape.

**Decision rule.** The [protocol above](#measurement-protocol)
applies: decide on the hardware-independent group only, and a difference
smaller than the range either arm shows across its own repeats is noise. At a
site, a change is also claimed only when B differs from A by more than A1
differs from A2. Classes with 10 rows or fewer (table, negative, the abstain
rows) are read in aggregate only. The paired per-row comparison runs on the
connected side from the exports and is not part of this tool.

### Failure taxonomy (#830)

`scripts/answer_taxonomy.py` gives every golden row one primary failure class.
It joins one or more answer reports (`--out` above; a run split across
sessions passes several) with one
[packing diagnostic](records/2026-10-17-packing-diagnostic.md) report from the
same corpus. The diagnostic must cover every row class of the run, and
`--profile` must name the served planner profile. It runs offline: no Qdrant,
model or agent. A row that appears in two answer reports, or that is outside
the expected golden set (`--golden`, default dev golden), refuses the join.
A golden row with no answer is `not_run`, never a pass, and makes the exit
code 1. Pass the same `--golden` set to the answer run, the packing diagnostic
and the taxonomy: under `VENUE=rc` an answer run without `--golden` adds
`evals/holdout.jsonl`, and the join refuses with exit 2 (for the dev baseline,
use `--golden evals/golden.jsonl` on the answer run).

The first matching class is the primary one: `pass`; `timeout` (client
`ReadTimeout` or the agent's 504 `deadline_exceeded`); `packing_loss` for a
served 422 `prompt_budget_exceeded` (the estimator-only diagnostic under-reports
these rows); `error` (any other request or HTTP error);
`unsafe_answer` (abstain row, trap answered); `retrieval_miss` (served
zero-hits path, or a gold target never retrieved); `packing_loss` (retrieved
but not packed); `unattributed` (a failing answer row without a diagnostic
row); `refusal_gold_present`; `generation_incomplete`; `citation_format`
(empty body, no validated or only inferred citations, or another state
mismatch); `content_miss` (a gold substring or identifier check failed). Retrieval and packing come before
model behavior, because the model cannot be judged on evidence it never saw;
`also` keeps the model classes a row shows as well. Abstain rows have no gold
to retrieve, so the diagnostic is not read for them. `pool_disagrees` marks rows where
the answer run's own `/v1/search` pool and the diagnostic disagree about gold
presence (corpus or settings drift). The report holds identifiers, classes and
redacted failure buckets only. For a row whose expected docs are alternatives
(any one is relevant), the diagnostic still makes one target per doc, so a
partially retrieved row is `retrieval_miss` and `gold_gaps` shows which doc is
missing (currently DIA-17 and DIA-23).

### Claim-level support (#365)

Claim support is evaluated offline, never asserted at runtime. A verification
state describes provenance and completion only; neither `accepted` nor
citation membership claims that a claim follows from the evidence.

- The offline instrument is the L2 NLI judge: for each judged answer with
  eligible citations it receives the cited hits' text (capped at
  `JUDGE_MAX_EVIDENCE_CHARS`; unmapped citations fail the row) and returns
  one temp-0, low-effort label — `entailed`, `neutral`, or `contradiction`.
  Citations are validated against the final supplied-evidence manifest
  (#364), so a cited hit is a retained excerpt; prose excerpts may still be
  char-trimmed during packing, so the judge can see marginally more text
  than the prompt carried — a documented approximation, not a silent
  widening of the claim.
- `faithfulness_by_class` slices the labels per query class so procedure
  (`syntax`, `diagnostic`) and version-sensitive (`version`) support reads
  apart from the aggregate. Judge labels are advisory trend data:
  unparseable output is a structural fail, and no gate or API state depends
  on a judge verdict.
- High-impact and version-sensitive acceptance cases are adjudicated by the
  domain expert on the RC venue under #367's release-set policy; judge
  disagreements, parse failures, and unscored rows are reported in the run
  summary rather than averaged away. Runtime claim verification — a
  universal truth validator or a mandatory second judge call — stays a #365
  non-goal requiring a separate measured decision.

<a id="paraphrase"></a>
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
  corpus, ingest into the dedicated collection, then evaluate — `sh scripts/tools/run-task.sh eval:paraphrase` runs only the check, not the ingest. Re-record a mode's baseline in a dedicated PR with `scripts/eval_retrieval.py --suite paraphrase --update-baseline`; `eval:baseline` owns the dev set only. Not wired into CI
   (no cluster, no embed server there); never tune against the frozen
    holdout.
- Hash anchor re-recorded 2026-10-26: the reviewed #860 `rrf_k` 2→60
  default moved `PAR-04` and `PAR-19` from rank 1 to 2 (r@1 0.90→0.80, MRR
  0.925→0.875, r@5 held at 0.95); all other rows are unchanged. Details and
  the one-variable A/B are in the dated record below.
- Measured verdict (issue #300, paraphrase vLLM A/B 2026-09-12, header-only
  vs `CONTEXTUAL_EMBED_ENABLED=true` over gateway-routed E4B gists, same
  model/dim/collection-shape): 22/22 queries byte-identical (r@1/mrr, both
  arms saturated at 1.0 under current defaults) — no headroom for a gain
  and none observed, so no holdout/RC escalation. Contextual stays
  default-off. Note: both arms beat the committed `baseline-paraphrase-
  vllm.json` (r@1 0.90), which no longer reproduces under the post-#270
  defaults — re-recording it is a separate dedicated PR, not this one.

### 6.1 Record-replay A/B (tune prod ranking from local)

Paraphrase measures semantic movement on synthetic pools; record-replay
measures ranking movement on real pools without needing prod models
locally. Capture runs where the models live (RC/gap), replay runs
anywhere (`tests/test_replay_sweep.py`, `scripts/capture_pool.py`). A single live A/B
pair is noise (dense retrieval is nondeterministic, #836): capture once, then
compare configs on those pools with `replay_sweep`.

Retained runs live under the run root documented in the
[sandbox lane](colab-playground.md#sandbox-lane-daily-iteration); the current
layout uses `data/pools-*.jsonl` for replay pools, `data/census-stored-*.jsonl`
for stored-content censuses and `reports/l1-*.json` for gate/probe reports.
These are private run artifacts: never commit them; shared reports carry ids,
ranks, scores, hashes/counts and metrics only, never chunk or manual text, and
private paths stay private.

```bash
# 1. Capture (RC/gap, live Qdrant + platform embed/rerank endpoints):
VENUE=rc sh scripts/tools/run-task.sh eval:capture-pool          # records into bundles/pools-YYYYMMDD.jsonl
#    overrides: GOLDEN=evals/golden.jsonl OUT=/path/pools.jsonl DEPTH=200
#    (the raw script also takes --no-ce / --max-queries; see its --help)
# 2. Carry pools.jsonl back (ids/ranks/scores only, never chunk text —
#    safe to move; still never tune against the frozen holdout).
# 3. Replay locally (record_to_rows → replay_pool → rank the replayed pool): sweep
#    type-boosts, fusion alpha, and diversity caps hermetically, no GPU.
#    The sweep CLI runs the production chain per config and scores
#    doc-level recall/MRR against the tune golden:
#      python scripts/replay_sweep.py --pools bundles/pools.jsonl \
#        --golden evals/golden.jsonl --json bundles/sweep.json
#    Section level: join the captured ids to a local collection ingested
#    from the same sources under the same extraction rules:
#      python scripts/replay_sweep.py --pools bundles/pools.jsonl \
#        --golden evals/sections.jsonl --headings-from manuals_current
#    Fusion grid (#845): `--grid rrf` replays rrf_k x sparse weight, NL and
#    identifier separately (the other kind at production), and reports both
#    kinds per config. Dev pools only; adoption is a separate default change.
```

**Exit status.** `replay_sweep.py` exits 1 when any pool row records a capture
error (the rows that already made `capture_pool.py` exit 1, shown in the table's
`fail` column); the table and `--json` are still written. It exits 2 when it
refuses (frozen holdout outside RC, failed heading join) and 0 otherwise.
Unmatched queries and skipped configs do not change the exit status.

**Depth.** `DEPTH` (`--depth`, at most 200) records each leg deeper than
production; the default records exactly the production prefetch, and a
capture is never shallower. Cross-encoder scores stop at 100 per leg (the
`rerank_candidates` ceiling, the deepest pool replay can rerank). Records
carry `_meta.depth`/`_meta.ce_depth`; replay refuses a config deeper than
the recorded depth, and the sweep reports such configs as skipped. A pool
without `_meta.depth` predates depth recording and refuses outright: recapture it. `rerank100` in the grid needs
`DEPTH>=100`.

**Page bucket.** Pools record each chunk's physical first page
(`page_start`) and replay diversifies on it exactly like live search
(`_page_key`, #271). Earlier pools carried only the printed label, which
is empty for unlabelled manuals (the JCL reference among them), so their
replays bucketed whole documents as one page; re-capture rather than
compare against them.

**Section level.** Pools carry no headings, but chunk ids are UUID5 over
`source_rev|heading_path|page|ordinal`, so an id that resolves in a local
collection is the captured chunk with the captured heading.
`--headings-from` joins every captured id (doc id and page must agree too)
and then scores `expected_heading` with the shared `is_relevant_hit`. Any
missing or mismatched id refuses the whole join: a different ingest is not
a partial answer. `evals/sections.jsonl` holds the section probes:
parameter-value questions against the JCL reference whose
`expected_heading` targets the correctly labelled "Subparameter
definition" sections. The doc-level golden cannot see right-manual,
wrong-section misses.

Capture path defaults live in `capture_pool.main`, selected explicitly with
`--bundle-dir DIR`: the default input is `evals/golden.jsonl` and the output is
`DIR/pools-YYYYMMDD.jsonl`, using the local calendar date at each invocation.
Task passes its `BUNDLE_DIR` and only nonempty `GOLDEN`/`OUT` overrides; explicit
empty Task overrides restore these defaults even over nonempty ambient values.
Direct `--golden`/`--out` paths remain authoritative, including empty-path refusal.
Without `--bundle-dir`, both paths are still required. An empty bundle directory
is rejected. Python creates the selected bundle after dataset/venue checks;
parents of arbitrary explicit output paths must already exist. Checked holdout
access precedes client startup and output replacement on either entry point.

Rules: CE-less pools (bypassed queries, `--no-ce`) replay RRF-only —
rerank refuses them fail-closed, never fabricate scores. Split
recordings replay per-leg (`record_to_rows(record, leg=i)`); merging
legs corrupts ranks. The sweep's ruler is doc-level unless headings are
joined (pools carry no message ids either), so a swept gain is a
candidate: adoption requires the live eval/holdout. Deltas ship in the PR body like any retrieval
change (`live-stack.md` rung 7); re-capture after any re-ingest (pools
pin rank order, not content). Pools stay in `bundles/`/scratch — never
committed (the real-corpus venue guard refuses `real_manuals` without
`VENUE=rc`; §10).

<a id="golden-corpus"></a>
## 7. Golden corpus discipline

`golden.jsonl` (127 dev) and `holdout.jsonl` (76, sha256-pinned, verified
by the Python dataset owner before protected parsing/scoring) are built from
`expert_golden_seed.jsonl` plus payload mining by `build_golden_corpus.py`:
manual bindings for out-of-pattern families, authored corrections,
forced-abstain ids, absent-trap ids, then a deterministic ~60/40 per-class
split (LEG entries always dev). The builder also rewrites `evals/holdout.jsonl`: run it in a
scratch checkout and compare only the holdout sha with its pin.

- `verify_golden` scrolls live payload and FAILs on missing
  expected/`must_not` docs, absent headings/pages, unbound message-id
  queries, code lookups (`message_id` class) whose parsed system code is not
  in the expected docs' `system_codes` or whose page carries no chunk with
  that code (#591), `must_not`-inside-expected without the query id, and duplicate
  queries (case-folded); rarity, stratification, and hygiene WARN unless
  `--strict` (which also demands size and class coverage — and the default
  `sh scripts/tools/run-task.sh eval:verify-golden` does not pass `--strict`, so weak traps ship on 0
  FAIL alone).
- Re-freeze process: rebuild, `verify-golden` 0 FAIL, new holdout sha,
  re-record baselines — one dedicated commit. Never iterate the holdout to
  tune. Exposure: 71 of the 76 current rows informed shipped choices, so the
  holdout is a regression ruler, not an independent one
  ([contamination audit](records/2026-10-21-holdout-contamination-audit.md), #844).
- Acceptance states (issue #365): the builder stamps
  `expected_verification_state` on every generated row (answer-tier
  `accepted`, abstain-tier `insufficient_evidence`; adjudicated exceptions in
  `EXPECTED_STATE_OVERRIDES`). The dev golden sha pin moves with the field;
  the frozen holdout receives it only in an adjudicated re-freeze, so dev
  acceptance and holdout acceptance stay separate rulers.

<a id="benchmarks"></a>
## 8. Benchmarks (`benchmark.py`, `loadtest.py`)

`sh scripts/tools/run-task.sh eval:bench` ingests a generated corpus (distinct bodies with unique
message ids, plus one plain doc; leftovers wiped; stale inventory deleted)
into the pinned Qdrant image, then loads a real uvicorn agent backed by the
mock LLM, measuring peak RSS, Qdrant container RAM/disk, and search/answer
latencies.

- An explicit `--check` reference must be a readable JSON object containing
  all five gated metrics as finite, nonnegative numbers (booleans are invalid).
  Optional `_meta` and `_meta.env` must be objects when present. Invalid
  references fail with exit 2 before CLI workload or output effects. The gate
  uses the parsed reference throughout the run, including concurrent replacement.
- Requested checking and recording validate each measurement pass before
  aggregation: all gated metrics must be available and valid, and both endpoint
  error counts must be integer zero with positive integer request counts. Empty
  workloads cannot qualify as zero-latency successes. Failure returns exit 2,
  stops the simulator, and preserves existing baseline/result/summary files without appending a
  manifest. The baseline writer also refuses invalid or failed measurements.
  This deliberately replaces partial-baseline and unmeasured-metric success:
  external Qdrant reuse without measurable RAM/disk cannot qualify a requested
  gate or baseline recording. Run without `--check` for diagnostic measurements;
  diagnostic output is not gate acceptance. Valid-input aggregation, thresholds
  and optional legacy environment identity checks are unchanged.
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

<a id="mock-fidelity"></a>
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

<a id="real-corpus-cadence"></a>
## 10. Real-corpus cadence (RC)

The per-PR gate runs hash mode on a synthetic corpus that saturates; the
real-corpus holdout is the only discriminating semantic instrument, and
the answer tiers are the only grounding instruments. They run on a
schedule, never as a PR gate (`live-stack.md` §0/§5.2).

**Venue declaration.** Every eval/harness entry point defaults to the dev
venue: `evals/golden.jsonl` only, synthetic collections only. The frozen
holdout (`evals/holdout.jsonl`) and the real-corpus collection
(`real_manuals`) require `VENUE=rc` — set by the `sh scripts/tools/run-task.sh eval:holdout`
recipe itself, and by operators for the harness tiers
(`VENUE=rc sh scripts/tools/run-task.sh eval:harness:l2`). Without the declaration the scripts exit 2
("frozen holdout … requires VENUE=rc"), so a truncated venue is never
scored. `mainframe_rag.eval.datasets` owns the rule.

**Triggers** — run the battery when any of these fires:

- an RC cut (the promotion decision);
- a re-ingest or golden/holdout re-freeze on the real corpus;
- a model or gateway change (embed, rerank, reasoning);
- a ranking-constant change (capture → replay A/B before merge).

**Battery** (retrieval before answer quality):

| Step | Command | Artifact / verdict |
|---|---|---|
| 1. holdout semantics | `sh scripts/tools/run-task.sh eval:holdout` | `<BUNDLE_DIR>/eval-holdout-{report.json,summary.md}` + manifest |
| 2. L1 promotion | `VENUE=rc sh scripts/tools/run-task.sh eval:harness:gate` | report + manifest; `merge`/`hold` |
| 3. answer tier | `VENUE=rc sh scripts/tools/run-task.sh eval:harness:l2` | report + summary + manifest; structural fails gate |
| 4. judge gate | `VENUE=rc sh scripts/tools/run-task.sh eval:harness:l4` | report + summary + review queue; `pass`/`hold`/`fail` |
| 5. perf tier | `VENUE=rc sh scripts/tools/run-task.sh eval:harness:l3` | report + manifest vs the L3 baseline |
| 6. pool capture | `VENUE=rc sh scripts/tools/run-task.sh eval:capture-pool` | dated `pools-YYYYMMDD.jsonl` (never committed) |
| 7. hermetic replay | `pytest tests/test_replay_sweep.py` + a replay sweep | A/B deltas in the PR body |

L4's reference (`evals/harness-l4-thresholds.json`) is recorded with
`VENUE=rc sh scripts/tools/run-task.sh eval:harness:l4-record` on the RC tier and ships in a dedicated
PR. L2/L4 standing red on known product debt is an RC debt signal, not a
broken target.

Historical command spellings below are preserved as executed; new runs use the
current `--list` / `<task> --summary` catalogue
([task policy](task-runner.md#scope)).

**Dated record.** Each battery run appends a row here in the same PR or
RC checklist that runs it (`$BUNDLE_DIR` reports are local). Run
manifests (`evals/runs/*.jsonl`, gitignored) carry the timestamp, git
SHA, settings hash, model ids, Qdrant version, and snapshot id — the
committed row is the pointer, the manifest is the detail.

Rows before 2026-10-19 were pruned as superseded history; their detail
remains in git (`git show 7c4bb00:docs/eval.md`) and, where one exists, in
the dated record under `docs/records/`.

| Date | Git SHA | Venue | Steps | Result |
|---|---|---|---|---|
| 2026-10-23 | 308f70b | `colab_manuals_qwen3_8b_4096_ced400a7` (rules ced400a7, 234,610 pts) + G4 Qwen3.8-27B xhigh, Qwen3-Embedding-8B dim 4096, rerank off, deadline 570 s / client 600 s | `eval-answers` 127 dev rows over 3 sessions (VENUE=rc, frozen holdout excluded) - #830 xhigh answer baseline | 88/127 pass; 127/127 once, 0 timeouts; generation_incomplete 27, retrieval_miss 5, unsafe_answer 3, refusal_gold_present 2, citation_format 2; abstention 7/13 (#798). Per-class counts and per-row attribution (ids only): [record](records/2026-10-23-xhigh-answer-baseline.md) |
| 2026-10-19 | 3329275 + #845 branch | `colab_manuals_qwen3_8b_4096_c47352b2` (rules `c47352b2`, Qwen3-Embedding-8B dim 4096; pools captured at `912a23f`, depth 40, no CE) | `replay_sweep --grid rrf` over dev golden and section probes, headings joined, rerank off; `rrf_k` ∈ {2,5,10,30,60} × sparse weight ∈ {1,2,3,5}, NL and identifier separately | production (k 2, weights NL 1/1, identifier 1/3) dev r@1 0.667 / r@5 0.921 / MRR 0.766, sections r@5 0.353; 0 `must_not` in every config. **k 60, weights unchanged**: dev r@1 0.702, r@5 0.956, MRR 0.796 (r@1 +9/−5 rows, r@5 +5/−1: SYN-07 rank 4→6; identifier rows gain most; VER-06, CMP-12, CMP-15, SYN-27, SYN-31 lose rank 1), sections r@5 0.471, MRR 0.325. Identifier sparse weight 1: best r@1 (k 5: 0.719, MRR 0.806) but `message_id` r@5 1.00→0.96 (an exact-code row leaves the top 5) — rejected. NL sparse weight 2 at k 60: dev r@5 0.965 but the section gain is lost (0.353) — rejected. **Decision:** no adoption here (dev selection only). `rrf_k` 2→60 with weights unchanged is the candidate for a dedicated default-change concern, confirmed live (dev + frozen holdout once) before any flip |
| 2026-10-21 | 2a723c6 (G4 sandbox) | `colab_manuals_qwen3_8b_4096_c47352b2` (rules `c47352b2`, Qwen3-Embedding-8B dim 4096, rerank off) | #860 live dev confirmation: `eval_retrieval --no-check` dev (127) and section probes (17) at `RRF_K` 2 and 60, same session and stack; holdout not read (#859) | k2 → k60: dev r@1 0.658→0.702, r@5 0.930→0.956, MRR 0.759→0.796, nDCG@8 0.785→0.813; `message_id` r@1 0.786→0.857, r@5 1.0→1.0; sections r@5 0.353→0.471, MRR 0.279→0.325; `must_not` 0 in both. Rank-1 +9 (CMP-02, CMP-10, DIA-17, DIA-21, MSG-02, MSG-22, SYN-11, SYN-16, SYN-21) / −4 (CMP-12, SYN-27, SYN-31, VER-06), all predicted by the 2026-10-19 replay; top-5 +DIA-17 +DIA-21 +SYN-06 +TBL-05, −SYN-07. Pre-registered #860 rule met → `rrf_k` default 2 → 60 |
| 2026-10-22 | 80607ec | synthetic hash L1 venue: `mainframe_manuals` alias over a fresh throwaway Qdrant (`v1.19.0-unprivileged`, digest-pinned), original PDFs generated from `evals/golden.jsonl` by `gate_l1.generate_synthetic_golden_corpus` (61 docs, 135 pts, hash embeddings, `ALLOW_HASH_MODE`) | `eval:harness:baseline` equivalent (`scripts/harness.py --update-baseline --restore drift`, `EMBED_MODE=hash`, dev golden 127 entries; Qdrant started and corpus ingested by a scratch driver because the Task entry assumes an already-prepared `mainframe_manuals`) - #837 second criterion; **the default `rrf_k` is now 60 (#867, was 2), so the re-record reflects that change as well as recall@1** | `benchmarks/harness.json` re-recorded: 114 scored + 13 abstain rows (127 checked), overall r@1 1.0, r@5 1.0, r@8 1.0, MRR 1.0, nDCG@8 0.9737, 0 trap failures; every class r@1 1.0 (`negative` has no scored rows: null); recorded `golden_entries` 127. Previous record (2026-09-06, 208 pts, 117 checked, 104 scored, r@5/MRR 0.9712, no recall@1, no `table` class, no `golden_entries`) was stale against the current golden and rules. The synthetic pages echo the query text, so the venue is saturated: it detects drops in class recall@1 (floor 0.05), not gains. New pin `mainframe_manuals__gen94e05d25f7c9c1344b5aa9137e9e-279217298361320-2026-10-22-07-24-35.snapshot` (135 pts), snapshot file kept at `~/qdrant-snapshots/harness-hash-837/`. `harness-vllm.json` not re-recorded: needs the RC venue on a G4 session |
| 2026-10-22 | 308f70b (G4 sandbox) | `colab_manuals_qwen3_8b_4096_ced400a7` (rules `ced400a7`, 234,610 pts, Qwen3-Embedding-8B dim 4096, shipped defaults, rerank off) | #825 live dev and section evaluation of the per-document comparative merge (#852); two identical replicates; holdout not read (#859) | dev (114 scored) r@1 0.711, r@5 0.956, MRR 0.802; comparative (12) r@1 0.333, r@5 0.917, MRR 0.583; sections (17) r@1 0.235, r@5 0.471, MRR 0.333; 0 `must_not`. CMP-05, CMP-17 retrieved at rank 3, CMP-06 rank 2, CMP-01 not in top 8 (label question). No regression versus the 2026-10-21 live row; not a controlled comparison (corpus rules and search params differ). Detail: [record](records/2026-10-22-comparative-live-eval.md) |
| 2026-10-23 | 308f70b (G4 sandbox) | `colab_manuals_qwen3_8b_4096_ced400a7` (rules `ced400a7`, 234,610 pts, Qwen3.8-27B, Qwen3-Embedding-8B dim 4096, rerank off, deadline 570 s / client 600 s) | `eval-answers` 127 dev rows, xhigh versus low `LLM_REASONING_EFFORT_{SIMPLE,COMPLEX}` (only difference), plus the #843 thinking-reserve 16384 arm on the capped rows; holdout excluded (#859) - #831 | xhigh 88/127, low 116/127; p50 221.0 s vs 70.4 s; over 300 s 55 vs 0. Paired: both 84, low only 32, xhigh only 4 (NEG-02, SYN-01, SYS-07, TBL-02), both fail 7; McNemar p < 0.001. Low passes 31 of the 32 xhigh-truncated rows; on the 95 completed rows 88 vs 85 (not significant). Reserve arm: 11/16 vs low 15/16, p50 658 s vs 110 s. **Recommendation only (no default changed):** `low` for both routes as its own concern. Per-class table: [record](records/2026-10-23-effort-arms.md) |
| 2026-10-23 | 308f70b (G4 sandbox) | `colab_manuals_qwen3_8b_4096_ced400a7` (rules `ced400a7`, 234,610 pts, Qwen3.8-27B, Qwen3-Embedding-8B dim 4096, rerank off, deadline 570 s / client 600 s) | `eval-answers` 126 of 127 dev rows (SYS-09 not run, session time), `LLM_REASONING_EFFORT_{SIMPLE,COMPLEX}` medium (only difference), paired with low and xhigh on the same 126 rows; holdout excluded (#859) - #917 | medium 119/126, low 115, xhigh 88; abstain declined 9/13 vs 7/13 vs 7/13; p50 / p95 / max 87.5 / 163.3 / 232.3 s; 0 timeouts, 0 `generation_incomplete`, 0 over 300 s. Paired: medium vs low 115 both, medium only 4 (NEG-02, SYN-01, TBL-02, VER-07), low only 0, p = 0.125; medium vs xhigh 87 both, medium only 32, xhigh only 1 (SYS-07), p < 0.001. Failures: MSG-13, SYN-07, SYS-07 (#916, #922 / #931), NEG-04, VER-08, VER-11 (#915), NEG-03. **No default changed; recommendation pending maintainer decision (#917).** Per-class table: [record](records/2026-10-23-medium-effort-arm.md) |
| 2026-10-23 | 170ac64 (G4 session S10) | `colab_manuals_qwen3_8b_4096_ced400a7` (rules `ced400a7`, 234,610 pts, Qwen3-Embedding-8B dim 4096, rerank off); pin `colab_manuals_qwen3_8b_4096_ced400a7__gen9acd068bf9b914d12d10f9f0f592-2773425469235116-2026-10-23-14-14-36.snapshot` | `harness.py --update-baseline` equivalent (`EMBED_MODE=vllm`, dev golden 127 entries, golden sha `0f298256`, frozen holdout not used, #859) - #837 | `benchmarks/harness-vllm.json` re-recorded and pinned (`snapshot_name` set, `points_count` 234610), replacing the 203-entry dev-plus-holdout record of 2026-10-03: 117 scored, overall r@1 0.7265, r@5 0.9573, r@8 0.9658, MRR 0.815, nDCG@8 0.8344, 0 trap failures; recall@1 recorded overall and per class, so the promotion gate compares it per class. Snapshot count 1 before and after, pin still listed. Supersedes the unpinned 2026-10-22 S6 record (114 scored), which predated the VER-08, VER-11 and NEG-03 relabels |
| 2026-10-23 | 170ac64 (G4 session S10) | 2-slot engine at medium effort, deadline 270 s, `colab_manuals_qwen3_8b_4096_ced400a7` (rules `ced400a7`) | D4 closed-loop stepped load (in flight 1, 2, 4, 6, 8, plus 8 with admission 4/4/30) - #843 | OK 5/5, 7/7, 13/13, 9/14, 7/18; 504 share 0, 0, 0, 36%, 61%; OK per hour 35, 53, 89 (peak at 4), 52, 39. Admission 4/4/30 at 8 in flight: 6/21 OK, 0 504, the rest immediate 503. Basis for shipping admission 4/4/30 in the chart and air-gap defaults (`Settings` stays 0); sizing rule `max_concurrent` = 2 x engine slots ([agent.md](agent.md)). Query-free: counts only |
| 2026-10-23 | `170ac64` (A) vs `bed6897` (B, #953) (G4 sandbox, session S11) | `colab_manuals_qwen3_8b_4096_ced400a7` (rules `ced400a7`, 234,610 pts, Qwen3.8-27B, Qwen3-Embedding-8B dim 4096, rerank off, `REQUEST_DEADLINE_S` 270) | #944 paired A/B of the #953 resolved-system-code note: the six SYS rows plus the 13 abstain-class rows, `LLM_REASONING_EFFORT_{SIMPLE,COMPLEX}` low and medium, A and B interleaved per row, one scorer; holdout excluded (#859) | SYS-07 fail (`insufficient_evidence`) to pass (`accepted`) at both efforts; SYS rows passing 5 to 6 of 6 at both. Abstain-class rows passing: low 11 to 11, medium 12 to 11; the only difference is SYN-32 at medium (A declined, B answered), whose A and B prompts were identical because the note did not fire, so it is sampling variance (known variance row, #915). The note fired on exactly 3 rows (`S806`, `SB37`, `SAFB` codes) and on no A prompt. Errors 0 in all four arms. Maintainer accepted criterion 2 of #944 on 2026-10-23. Ids and verdicts only. |
| 2026-10-24 | `5e8ca4c` (G4 session S7); capture/replay `170ac64` (same retrieval path) | `colab_manuals_qwen3_8b_4096_ced400a7` (rules `ced400a7`, Qwen3-Embedding-8B dim 4096); replay cells `k60d50a1` (rerank on) vs `k60-off` within one pool per arm, section-level scoring | #862 cross-encoder A/B, one arm per session (Qwen3-Reranker 8B, 4B): dev 127 records (51 reranked, 76 identifier rows bypassed; 117 scored, 46 natural-language), sections 17 (all reranked); pre-registered rule plus 2026-10-23 addendum; holdout not read (#859) | **No-adopt; rerank default stays off.** 8B: dev r@5 net -4 rows, CI [-8, -1] (lost CMP-06, CMP-11, TBL-05, VER-02; gained none); sections r@5 net +5, CI [+2, +9] (SEC-01/03/04/06/07); dev r@1 net -3, CI [-10, +4]; class floor 0.05 breached on dev: comparative r@1 0.333->0.25, r@5 0.917->0.75, MRR 0.583->0.451, table r@1 0.75->0.25, r@5 1.0->0.75, MRR 0.8->0.417, version r@5 1.0->0.857; latency p50/p95 off->on c1 305/635 -> 2,091/2,465 ms, c4 295/709 -> 4,723/5,649 ms, 0 errors. 4B: dev r@5 net -3, CI [-7, 0] (lost DIA-02, TBL-05, VER-02; gained none); sections r@5 net +3, CI [0, +6] (SEC-01/04/06); dev r@1 net -3, CI [-10, +4]; class floor breached on dev: comparative MRR 0.583->0.506, table r@1 0.75->0.5, r@5 1.0->0.75, MRR 0.8->0.625, version r@1 0.571->0.429, r@5 1.0->0.857, MRR 0.738->0.607; latency p50/p95 off->on c1 312/616 -> 1,558/1,941 ms, c4 293/698 -> 2,686/3,365 ms, 0 errors. Both arms: criteria 1 and 3 fail, criterion 2 passes, 0 must_not violations dev and sections; `replay.py --choose`: neither arm passes. r@1 movers on dev, 8B gained DIA-23, LEG-09, SYN-26, SYN-27, VER-12, lost CMP-10, DIA-17, LEG-02, LEG-03, SYN-31, TBL-01, TBL-02, VER-08; 4B gained CMP-07, DIA-23, LEG-09, SYN-27, VER-12, lost CMP-10, DIA-17, DIA-32, LEG-02, SYN-31, TBL-01, VER-08, VER-13. **Decision: do not ask the platform to serve a reranker on this evidence** (cross-encoders help section-level questions but cost natural-language dev rows and small classes at 5-16x p50 latency; latency was report-only). **Replay caveat:** replay fuses without the #922 entry ids, so replay order can differ from live capture for message-id queries; both arms report `identifier_rows_unchanged: false` (identifier rows bypass the cross-encoder and should not move), most likely this caveat and not confirmed; it applies to the on and off cells alike, so each comparison stays internal to its pool and on-off deltas are never subtracted across pools. Verdicts: `verdict-8b.json`, `verdict-4b.json` (local run folders `2026-10-24-5e8ca4c-s7-{8b,4b}`). |
| 2026-10-26 | 97e4fd8 | synthetic paraphrase hash venue (`paraphrase-manuals`, 22 entries, 15 docs, `EMBED_MODE=hash`, `ALLOW_HASH_MODE`) | `eval:paraphrase` baseline re-record (`eval_retrieval.py --suite paraphrase --update-baseline`) after the reviewed #860 `rrf_k` 2→60 default — #1093 | Committed 2026-09-03 hash baseline was stale: r@1 0.90→0.80, MRR 0.925→0.875, nDCG@8 0.932→0.895, r@5 held 0.95, 0 `must_not` violations. Only `PAR-04` (`doc_number`) and `PAR-19` (`diagnostic`) moved rank 1→2; no other row's recall/MRR changed. One-variable A/B on identical code and corpus: at 97e4fd8 `RRF_K` 60 gives 0.80/0.875 and `RRF_K` 2 gives 0.90/0.925; at baseline-era 32e8054 `RRF_K` 2 gives 0.90/0.925 and `RRF_K` 60 gives 0.80/0.875. #860 was pre-registered and live-confirmed for dev (r@1 0.658→0.702); gate ratios are unchanged, and the recorded baseline is now the post-#860 anchor. Two literal check runs after the re-record are identical and green |

**2026-10-23 default decision (#917).** The maintainer adopted `medium` reasoning effort on both routes as the shipped default after the S9 medium arm (126 dev rows: 119 pass against 115 low and 88 xhigh; 0 `generation_incomplete`; latency p50 87.5 s, p95 162 s, max 232 s). The measured record is #932; this note carries no new run. The xhigh and low rows above stay as the history they measured. Live confirmation of the changed default on G4 has not been run.

### Protected holdout acquisition (#508 C3)

`mainframe_rag.eval.datasets.read_golden_text` owns protected input access.
The retrieval, answer, chat, L1/L2/L4 harness, L1 gate, capture and replay
entry points use it before model/storage work. The Task holdout command
retains its explicit RC venue and argument transport; its duplicate shell
checksum check is removed. Direct Python entry points enforce the same pin.

A path named `holdout.jsonl`, or resolving to that name, requires `VENUE=rc`.
Resolve the target once and load its adjacent `<target-name>.sha256` pin.
The pin must contain one SHA256 record naming the target basename, either
alone or with the historical `evals/` prefix; the record never selects a
different file. Copies used outside the checkout must carry their approved
pin. Symlink aliases use the resolved target's pin. This authenticates bytes
against the supplied approved dataset/pin pair, not the provenance of an
arbitrarily replaced pair; dataset re-freezing remains a dedicated concern.

The reader hashes and decodes the same byte buffer, with no reopen between
verification and parsing. Missing, malformed or mismatched pins refuse with
CLI exit 2 before scoring or output publication. Every invocation verifies
again; repairing the dataset/pin permits the next ordinary run. Dev inputs
remain unpinned, and each evaluator retains its own row schema. L1's existing
dev-golden pin check remains separate. No release baseline or scoring rule
changes. Tests live in `test_venue.py` and `test_eval_gate.py`; the latter
executes actual Task and the actual retrieval CLI with runtime effects
instrumented, including corruption, recovery, precedence and literal paths.

<a id="release-acceptance"></a>
## 11. Release acceptance: independent set and pre-registered criteria (#367)

**Status: PROPOSED in the machine file; accepted by the maintainer (see the
2026-10-23 registration record below).** The criteria in
`evals/acceptance-criteria.json` are policy choices, not
measured production performance; the pruned pre-2026-10-19 stand-in rows in §10
history are not evidence for or against them. Until `status` is `registered` (with
`registered_by`/`registered_at`, in a dedicated PR before any scored run),
every result is non-certifying. A later change needs a new `criteria_id` and a
new set binding; never edit criteria after seeing scores.

**What this repository provides.** The schema, validator, venue protection and
scorer in `mainframe_rag.eval.acceptance` (`python scripts/eval_acceptance.py`,
`python -m mainframe_rag.eval.acceptance`). **What it cannot provide:** SME
authoring, independent adjudication, and the production-profile run (see
"External work"). The code refuses to certify without them.

### Independent set format (`release_set.jsonl`)

Typed JSONL records, first a `manifest`, then `case` records. The set lives
outside git (ignored like the corpora) with an adjacent `release_set.jsonl.sha256`
pin (one record naming the basename). Passages are referenced, never quoted:
`evidence[].excerpt_sha256` may hash the adjudicated passage.

- `manifest`: `set_id`, `version`, `stage` (`pilot` or `release`, selects the
  criteria stage), `criteria_sha256` (the criteria file the SMEs authored
  against), `corpus_revision`, declared `authors` and `adjudicators`, and an
  `independence` attestation (`authored_outside_tuning_loop: true`,
  `saw_system_outputs: false`, statement).
- `case`: `id`, `query`, `query_class` (the golden vocabulary), `expected_behavior`
  (`answer`/`abstain`), `provenance` (`author`, `authored_at`, `source`,
  `release`), `adjudication` (`adjudicator`, `adjudicated_at`, `verdict:
  accepted`), optional `critical_probe` (`scope`, `release`, `protocol`,
  `access`, `false_completion`, `provenance`), paired with a predeclared
  `critical_expected_outcome` (`answer`, `refusal`, `interrupted`).
  Answer cases add `evidence[]` (`doc_id`, `release`, physical `physical_page`
  >= 1, `locator`), `expected_doc_ids` equal to the evidence docs,
  `required_facts`, `required_conditions`. Abstain cases add `abstain_reason`
  (`insufficient_evidence`/`wrong_premise`) and `must_not_assert`, and carry no
  docs/evidence.
- The validator rejects unknown fields, an `adjudicated_at` that is not a later day
  than `authored_at` (D2 amendment: adjudication is a separate pass), authors or
  adjudicators absent from the manifest, duplicate ids or case/whitespace-folded
  queries, malformed dates/hashes, and any unadjudicated case. The CLI also
  refuses a set whose queries overlap the required `golden.jsonl`,
  `paraphrase.jsonl` or pinned `holdout.jsonl`. The script supplies its repository
  root independently of caller cwd; installed/module callers select the workspace
  or an explicit `--comparison-root` artifact directory. Missing, empty or invalid
  comparison sets refuse. Exact normalized overlap checks do not establish that
  unseen paraphrases or development exposure are absent.
- **Never a tuning set.** `datasets.read_release_set_text` requires `VENUE=rc`
  and the adjacent pin (hash and decode one buffer; mismatch, malformed or
  missing pin refuse with exit 2; the next ordinary run succeeds after repair).
  Every golden reader (`read_golden_text`, hence retrieval, answers, chat, L1/L2/L4,
  capture, replay) refuses a file named or resolving to `release_set.jsonl` even
  under RC, and `parse_golden_text` refuses release-set records in a renamed
  copy. Never tune against it; a defect found with it is fixed against the dev
  golden and the set is re-adjudicated or retired, not retuned.

### Outcomes and scoring

The reviewer records one adjudicated `outcome` per case and repeat after running
the exact candidate through the approved gateway, behind a `run` record that
carries the `set_sha256`, `criteria_sha256`, `repeats` and the profile identity
(`kind` `production`/`standin`; production must name reasoning, embed and rerank
model (`none` when disabled), corpus collection/revision, image, git SHA and
temperature). Outcome fields: `status` (`scored`/`skipped`/`error`),
`completed`, `refused`, SME `useful`/`supported`/`traceable`,
`evidence_supplied`, abstain `appropriate_abstention`/`fabricated_instruction`,
`critical_failures`, explicit `critical_safe` assessment for critical probes,
and `failure_stage` (extraction, retrieval,
evidence_omitted, context_exhaustion, protocol, unsupported_answer,
infrastructure). Outcome adjudicators must be declared in the set manifest,
and a production run's corpus revision must exactly match the set's revision.
These checks establish record consistency, not human identity authentication.
A usefulness pass requires `status=scored`, `completed`, no critical
failure and, for answers, not refused and useful, supported and traceable.
Everything is over **all requests**: `length`, transport failures, skips,
missing outcomes and false refusals are failures, never exclusions. The report's
diagnostics give all-request pass rate, completion rate and quality given
completion with separate denominators, plus failure attribution and per-class
rates. Duplicate/unknown outcomes, hash or criteria binding mismatch, wrong
repeat count, missing adjudication fields and NaN refuse with exit 2; missing
outcomes make `outcomes_complete` insufficient (verdict `incomplete`). Every
critical case/repeat also needs scored explicit safety adjudication matching
its declared state: completed non-refused answer, completed appropriate refusal
without fabricated instructions, or interrupted generation without completion
or refusal. Missing/skipped/error/unassessed critical slots are insufficient;
unsafe or state-mismatched assessments reject. A safe interruption can pass
its safety check while still counting as a usefulness/completion miss.

### Proposed criteria

Profile: only `production` (the actual platform reasoning/embed/rerank models and
the selected frozen corpus) can certify. A local stand-in run (for example the
8 GB gemma/Qwen3-Embedding profile) exercises the mechanics and reports
per-criterion results but is `non-certifying`.

| Stage | Statistic | Criteria (all must pass) |
|---|---|---|
| `pilot` (30 cases: 24 answerable, 6 abstain; 1 repeat; **no statistical claim**, a small screen per #447 G2) | point estimate | P1 useful+supported+traceable >= 20/24; P2 abstain pass 6/6; P3 zero fabricated instructions; P4 zero critical failures; P5 false refusals <= 10%; coverage >= 3 answer cases in each of `message_id`, `version`, `syntax`, `table`, `diagnostic` |
| `release` (n >= 100 answerable, 40 abstain per D4, below; the criteria file still reads 30 until #367 lands; 3 repeats at production temperature) | descriptive Wilson quantities; inferential unit awaits owner decision | R1 answer pass lower quantity >= 0.80; R2 abstain pass lower quantity >= 0.85; R3 zero fabrications and R4 zero critical failures in any repeat; R5 completion >= 0.95; R6 false-refusal upper quantity <= 0.10; R7 evidence supplied intact >= 0.90; R8 repeat flip rate <= 0.10; per-class answer floors with >= 10 cases (`message_id`, `doc_number` 0.90; `syntax`, `diagnostic`, `version`, `table`, `comparative` 0.75) |

Repeats do not add independent cases: the Wilson bound uses the case count as
`n` and the mean pass fraction across repeats as the rate. Fractional successes
are descriptive quantities, not established binomial confidence bounds for
repeated-case mean performance. Unless the stage registers `case_reduction: "majority"` (see the 2026-10-23
statistical-method entry below), a requested statistical claim for these means
keeps the result non-certifying (`statistical_claim=false`, original request
reported separately). The maintainer must select and register a statistical
unit/method in the dedicated criteria decision; the scorer does not invent a
binary reduction or change thresholds. A single-repeat binary case remains
eligible for the existing Wilson calculation.

### Registration record, 2026-10-23 (#367 D1-D6)

Maintainer decisions of yonatan895 on the 2026-10-22 proposal; they bind this
section. This record precedes any scored run, and scoring may only start after
the PR carrying it merges. Dated; amend by a new dated record, not in place.

- **Criteria registered as proposed (D3).** The pilot and release criteria in
  the table above, with the thresholds in `evals/acceptance-criteria.json`,
  are accepted unchanged, with two amendments: the abstain count (D4) and the
  profile binding (D5). Nothing else is invented or tuned. Open numbers: the
  statistical unit/method for repeated-case means (see "Proposed criteria")
  remains unselected, so a release-stage statistical claim stays
  non-certifying until the maintainer registers it.
- **Build path B (D1).** Run the 30-case pilot first as the go/no-go for the
  authoring spend, then grow to a release set of about 130 rows (100 answer
  plus 40 abstain). Fresh-manual split (option C) is deferred unless the pilot
  shows the corpus is too narrow. Reusing the exposed holdout is rejected.
- **Roles (D2).** Adjudicator: yonatan895. Author: **not yet named**; must be
  a different person (the validator rejects author equal to adjudicator). The
  set cannot be built until the author is named; #367 stays blocked on that.
- **D2 amended, 2026-10-23 (yonatan895).** Supersedes the Roles (D2) line above
  (kept as the dated original). Author: **yonatan895**, also the adjudicator;
  the "different person" condition is dropped and #367 is no longer blocked on
  an author. Independence now means: the release set is separate from
  development, and **agents never author, read, score against or tune on it**
  (the rule of the frozen holdout, #859); only its denominators and ids are
  published. The second-person check is replaced by the written authoring
  checklist and by adjudication in a separate pass after authoring, not while
  writing (enforced as `adjudicated_at` later than `authored_at`). The
  "adjudicated by a second person" clause in the #859 paragraph below is
  superseded accordingly.
- **Statistical method, 2026-10-23 (yonatan895; option B).** For the registered
  absolute bars R1, R2 and R6 (the Wilson-bounded rates), each case is reduced to
  pass/fail by majority over its repeats (at least 2 of 3; the stage field
  `case_reduction: "majority"` requires an odd repeat count, so there are no ties),
  and the existing Wilson bound then runs with n = number of cases. This
  supersedes the "unit unselected" and "descriptive" wording above for those
  criteria; the other release criteria (R3-R5, R7, R8, per-class floors) keep
  their stated statistic over all repeat slots. Option A (a deterministic
  percentile cluster bootstrap over per-case means, seed 367, 10000 resamples)
  is reported in the report's `sensitivity` list and **never gates**. Option C
  (paired comparison) is reserved for model-change comparisons and is not
  implemented. The criteria file carries `case_reduction` for the release stage
  but stays `proposed` with abstain `min_n` 30 until registration.
- **Abstain composition (D4).** 40 abstain rows in the release set (pilot
  keeps 6), so one miss is tolerable: R2 (Wilson lower >= 0.85) at n=30 needs
  30/30, at n=40 it tolerates one miss. Mix of `insufficient_evidence` and
  `wrong_premise` is the author's, declared in the manifest before scoring.
  The release answer count stays at least 100.
- **Model-profile binding (D5).** The criteria certify one named `production`
  profile: reasoning model and effort, embed and rerank model, corpus
  collection/revision, image and git SHA (the run record fields above). A change
  to any of them, notably the reasoning model, invalidates the verdict and
  requires a re-run of the release stage against the new profile.
- **Feedback rows excluded (D6).** Rows derived from user feedback (#806) never
  enter the release set or the pilot; they have seen system output.
- **Independence from the frozen holdout (#859).** The #859 audit found that
  71 of the 76 frozen holdout rows informed shipped choices, so the holdout is
  a regression ruler only and cannot be the independent instrument. This set is
  independent of it because it is authored outside the tuning loop by a person
  who has not seen system outputs, adjudicated by a second person, held outside
  git behind the `VENUE=rc` pin, refused by every golden reader, and checked
  for query overlap against the dev golden, paraphrase and holdout sets.

- **Machine file not yet flipped.** `evals/acceptance-criteria.json` keeps
  `status: proposed` (and release abstain `min_n` 30) in this PR; every result
  stays non-certifying until the maintainer sets `registered`,
  `registered_by`/`registered_at` and the 40-row abstain `min_n` with the set
  binding, before the first scored run.

### Pilot authoring kit (#367, 2026-10-23)

Tooling and rules for the maintainer; agents never write, read or run it on
real rows. It reuses the registered `release_set.jsonl` schema and loaders in
`mainframe_rag.eval.acceptance` (no second format); `scripts/release_kit.py`
(`mainframe_rag.eval.release_kit`) adds authoring-time checks. Tests:
`tests/test_release_kit.py`, `tests/test_acceptance.py`, synthetic only.

- **Private storage.** `~/.config/mainframe-rag/release-set/` (mode 700, outside
  the working tree, never committed, never under the agent scratch cache):
  `release_set.jsonl` plus its adjacent `.sha256` pin. The kit refuses a path
  inside the repository and flags a group/other-accessible directory. Back it up
  with the other durable records in `~/.config/mainframe-rag/`.
- **Row template.** `python scripts/release_kit.py template` prints a placeholder
  manifest, one answer row and one abstain row generated from the schema fields.
  Ids use a `REL-` prefix; never reuse a dev golden id.
- **Check while authoring.** `python scripts/release_kit.py check` (default
  `--set` is the private path) validates the schema, ids, the stage counts
  (pilot 24 answer + 6 abstain, covering the criteria's per-class
  `case_count` minimums; release at least 100 answer and exactly 40 abstain),
  the manifest `criteria_sha256`, one `required_facts` entry per answer row,
  `excerpt_sha256` on every evidence item, and no id or normalized-query overlap
  with `golden.jsonl` and `paraphrase.jsonl`. It prints ids and counts only,
  never query text. The pinned holdout overlap is checked by the final
  `--validate-only` run, not here.
- **Freeze.** `python scripts/release_kit.py pin` writes the `.sha256` pin; then
  `VENUE=rc python scripts/eval_acceptance.py --set <path> --validate-only`
  (adds holdout overlap). Run scoring only after the criteria are registered.
- **Authoring checklist (replaces the second-person check).**
  1. Write from the manual page, never from system output or the dev golden set;
     do not paraphrase a dev row.
  2. One fact per answer row (`required_facts` has one entry); side conditions
     go in `required_conditions`.
  3. Record the source passage: `doc_id`, `release`, physical page, locator and
     `excerpt_sha256` of the passage (hash only, never the text).
  4. The question must have one answer supportable by the cited passage alone,
     and must not name the expected fact.
  5. Abstain rows: choose `insufficient_evidence` (the corpus plausibly lacks
     the answer; confirm absence by reading the manuals, not by querying the
     system) or `wrong_premise` (a false assumption the manuals contradict);
     `must_not_assert` lists the specific fabrication to catch. Abstain rows
     carry no docs or evidence. Declare the mix in the manifest `independence.statement` before
     scoring.
  6. Cooling-off: finish authoring, wait at least one full day, then adjudicate
     each row cold against the cited passage in a separate sitting. Record the
     adjudication date; same-day adjudication is rejected.
  7. Reject, do not repair, a row that needed changing after adjudication
     reading; rewrite and re-adjudicate it on a later day.
  8. No feedback-derived rows (D6); no row written after seeing a system answer
     to it (set `saw_system_outputs` false only if true).
  9. After the first scored run, a defect found through the set is fixed against
     the dev golden and the row is re-adjudicated or retired, never tuned on.

### Decision procedure

1. Maintainer/SME review and register the criteria (before authoring or scoring).
2. SMEs author and independently adjudicate the set against the registered
   criteria' sha; `eval_acceptance.py --validate-only` checks it.
3. Freeze corpus/revision and effective model/context/tokenizer configuration;
   run the exact candidate image through the approved gateway, `VENUE=rc`, at the
   registered repeat count; the reviewer adjudicates each served answer.
4. Score. `accepted` (exit 0) needs registered criteria, a `production` profile,
   a matching manifest binding and every criterion passing. Any failing criterion
   is `rejected` and any insufficient criterion `incomplete` (exit 1). Proposed
   criteria or a stand-in profile exit 2: reported, never a pass.
5. Record the dated result row in §10 and the failed cases with their
   attributed stage. Failures are fixed on the dev golden, never against the set.

### External work (not done by this repository)

SME authoring and independent adjudication of the set; maintainer/SME
registration of the criteria; the production-profile run and per-answer
adjudication; the corpus/model freeze record. Tests use synthetic fixtures only
(`tests/test_acceptance.py`).
