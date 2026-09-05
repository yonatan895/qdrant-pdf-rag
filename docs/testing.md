# Testing reference

Normative test rules for this repo. `AGENTS.md` states what to run before
every push; this file states how to write the tests. The two files agree —
if they ever conflict, `AGENTS.md` wins and this file gets fixed in the
same PR.

Jump list: [hermetic](#unit-tests-are-hermetic) ·
[claimed-path](#tests-must-lock-the-claimed-path) ·
[airgap-tier](#air-gap-deployment-tier-make-airgap-dryrun-tests-test_airgap_py-local-kind) ·
[golden](#golden-corpus-devholdout) · [sim](#simulation-tier-marker-integration-make-sim) ·
[load](#load-tier-marker-integration-make-loadtest-mock-pr-gated-by-githubworkflowsloadyml-on-agentretrieveingestmock-paths) ·
[eval](#eval-make-eval) · [paraphrase](#paraphrase-instrument-evalsparaphrasejsonl-make-eval-paraphrase) ·
[answers](#answer-tier-make-eval-answers-live-gpu-stack-in-process-testclient-like-scriptstest_local_e2e_vllmpy) ·
[bench](#bench-make-bench-githubworkflowsbenchyml-github-only-never-a-pr-gate) ·
[harness](#harness-invariants-all-harness-tiers-l1l2l3) · [reports](#reports)

Read only the sections your change touches — L2/L3 have no business in
a splitter-change review.

`pytest` is the gate. `make check` runs `ruff check`, `mypy src`, and the
unit suite. Tests generate original PDFs at runtime
(`scripts/make_synthetic_pdf.py`). No binary fixtures in git.
CI must fail if `git ls-files` matches `.pdf` / `.pdx` / `.idx`.
Cover IBM-shaped synthetics (form number, message id, outline) **and**
generic PDFs (no outline, no form number, unknown vendor).
`test_chrome_strip` must keep a **long** page list (≥8 pages). Chrome is
disabled on short docs on purpose.

## Unit tests are hermetic

Do not call live Qdrant, vLLM, or the internet. Fake the client. Ingest
tests use `--dry-run`.

- Patch `httpx2.get` / `httpx2.post` in every unit test that can reach them. Hostnames like `embed-host:9000` are live network. A test that “works because connect failed” is invalid.
- Requesting the `monkeypatch` fixture does nothing by itself. Register every mutated env key with `monkeypatch.setenv` / `monkeypatch.delenv` **before** the code under test runs, or snapshot with `monkeypatch.setattr(os, "environ", dict(os.environ))`. Autouse fixtures must call `monkeypatch`.
- Do not mutate module-global state (routes on the global app, leftover `os.environ`) that later tests inherit.
- Pin public contracts, not private internals (`client._transport._pool._retries` dies on the next lockfile bump).
- Remove unused fixtures and parameters when touching a test.

## Tests must lock the claimed path

If the PR claims “CLI override”, “auto-detect”, “unwrap fence”, “sandbox env”, “IPC isolation”, or “dimension recreate”, the test must still pass when the **success** path is forced with mocks.

Do not assert an outcome the fallback would also produce.

Minimum matrix for any auto-detect / resolve helper:

1. Explicit value matches a served id or basename.
2. Explicit value + multiple nonmatching ids → keep the explicit value **or** fail closed with a message. Never silently keep `load_settings()` leftovers.
3. No explicit value + exactly one served id → auto-select.
4. Connection refused / timeout → documented fallback only; hash mode requires `allow_hash_mode=True`.
5. HTTP 200 with non-JSON or missing `data` → must not raise out of the helper.

Parser / citation / fence / grounding changes need the cases that broke last time:

- Top-placed `Citations:` with no blank line after the last cite.
- `CITATIONS:` case folding.
- Parentheses noise must not become excerpt indexes (`z/OS (3.1)`).
- Unlabeled vs language-tagged fences; do not use `len > N` as a script signal.
- Out-of-bounds `[99]`.
- e2e `/v1/answer` fails on zero citations or “no supporting excerpts”.
- Queried identifiers (`IEA500I`, `LFAREA`, …) must exist in the synthetic `build()` fixture, otherwise the gate cannot fail for the right reason.

IPC / worker changes: round-trip the error record through `pickle.dumps`.
Collection-dimension logic: missing, matching, and mismatched (named `dense` dict **and** single-vector schemas), including `--skip-ingest`.

## Tiers

### Air-gap deployment tier (`make airgap-dryrun`, `tests/test_airgap_*.py`, local Kind)

The canonical 5-stage deployment pipeline (`airgap-pack` -> `airgap-load` -> `airgap-deploy` -> `airgap-ingest` -> `airgap-smoke`) is verified across three complementary tiers:
1. **Hermetic Test Suite (`pytest tests/test_airgap_*.py`):** Fast unit tests running without a cluster or Docker daemon. Exercises `scripts/airgap/*.sh` via stubs for `helm`, `kubectl`, `oc`, `kustomize`, and `skopeo`. Verifies pre-flight environment validation (`validate.sh`), sneakernet extraction and bootstrap (`bootstrap.sh`), pipeline orchestration (`pipeline.sh`), manifest rendering, string quoting of integers and booleans (`DENSE_DIM`, `INGEST_WORKERS`, `RERANK_ENABLED`), storage class checks (refusing NFS), Jaeger v2 wiring, and fail-closed behavior on `/healthz` probe failures.
2. **CI Pre-Flight Dry-Run (`make airgap-dryrun`):** Automated PR gate in GitHub Actions. Renders production Helm templates and Kustomize overlays using test parameters, verifying that all placeholders are substituted and zero leftover `__[A-Z0-9_]+__` patterns remain.
3. **Local Cluster & E2E Rehearsal:** In local development, operators test the complete pipeline against a single-node Kind cluster and local registry container on port 5000 (`localhost:5000`). In CI, `airgap-rehearsal` runs on `main` against an ephemeral namespace in the lab OpenShift cluster, validating the real sneakernet tarball unpack, image push, StatefulSet rollout, and smoke queries.

### Golden corpus (dev/holdout)

`evals/golden.jsonl` is the dev set; `evals/holdout.jsonl` is **frozen** (sha256-pinned at `evals/holdout.jsonl.sha256`). Tune against dev only; `make eval-holdout` runs on release candidates only. Both files are mechanically verified by `make verify-golden` (0 FAIL required; rebuild via `scripts/build_golden_corpus.py` from `evals/expert_golden_seed.jsonl` + payload mining). Entries carry `query_class` (message_id/doc_number/syntax/diagnostic/comparative/version/negative), `expected_behavior` (answer/abstain), `must_not_retrieve`/`must_not_message_ids` (gated hard-zero within top-5; a chunk co-carrying the query's own message id is the same documented page, not a violation — same sibling-allowance as the builder's trap assertion), `expected_page` (diagnostic), and answer-tier gold fields for the answer eval. Abstain entries carry no `expected_doc_ids` and stay out of recall/MRR denominators. Loading new vendor books (new domains/editions) triggers a re-bind: re-run `scripts/build_golden_corpus.py` (domain entries flip abstain→answer automatically when their identifiers bind), re-author for the new domains, `make verify-golden` 0 FAIL, then re-freeze the holdout (new sha) and re-record baselines in one dedicated commit.

### Simulation tier (marker `integration`, `make sim`)

Real PDFs → real ingest into a docker Qdrant (`images.txt` pin, or `QDRANT_SIM_URL`) → agent endpoints over the real app. `scripts/mock_vllm.py` is the only stand-in. No retrieval/LLM code is monkeypatched. Docker-only, loopback-only, corpus generated at runtime. Plain `pytest` deselects it (`-m 'not integration'`). CI `sim` job is **fail-closed**: missing docker, any skipped test, or zero passes fails the job. Fetched BM25 weights verify against `bm25-weights.sha256`. Synthetic documents must differ in **body text**, not just metadata. Never pin top-1 across potentially equal-text chunks; assert scoping + presence + within-run determinism.

### Load tier (marker `integration`, `make loadtest-mock`, PR-gated by `.github/workflows/load.yml` on agent/retrieve/ingest/mock paths)

The sim composition plus a real uvicorn agent (`LLM_STREAM=true`) asserting **absolute** contracts under threaded load — zero errors, zero missing `Server-Timing`, per-stream SSE integrity (tokens → exactly one `final`, no `error`), citation parity stream/search/JSON, fixed error envelopes with no leaked internals, determinism after load, abort-storm survival (complete XOR aborted per stream, one `stream_truncated` alert per abort), and a TTFT floor under a paced mock. Never cross-environment comparisons. Fail-closed like sim (no skips; agent stdout goes to a file, never a pipe — an unread pipe wedges every request under load).

### Eval (`make eval`)

Dev golden set vs the mode-keyed baseline (auto-selected by `EMBED_MODE`; collection mismatch skips the gate with a loud warning). Golden hits are doc-level. Sim runs `test_eval_retrieval_on_synthetic_corpus`.

### Paraphrase instrument (`evals/paraphrase.jsonl`, `make eval-paraphrase`)

Operator-phrased queries whose answers exist in the synthetic corpus WITHOUT the query text near-verbatim (no-echo contract pinned hermetically in `tests/test_paraphrase.py`), over lexical-competitor docs. Separate golden set + mode-keyed baselines (`evals/baseline-paraphrase[-vllm].json`) so the main gate is untouched; corpus generated at runtime via `gate_l1.generate_synthetic_golden_corpus`, ingested into a dedicated collection. For semantic A/B work (contextual prefixes, reranker on/off, dense-prefix tuning) where the main set saturates. Not wired into CI.

### Answer tier (`make eval-answers`, live GPU stack, in-process TestClient like `scripts/test_local_e2e_vllm.py`)

/v1/answer` grounding honesty — answer entries must produce ≥1 validated citation and must not refuse; abstain/trap entries must not be answered (zero validated citations). Gold substrings judge model phrasing and are suppressed on the canned zero-hits path. The judge never re-parses citations (the agent's validator is the single source of truth). No retries, no finish_reason checks (not in the response contract; the app alerts non-stop per request). Deterministic stratified round-robin sample (`N=24` default, `N=all` full run); reasoning sampling is not run-deterministic — structural FAILs gate, rates are trend data in the manifest.

### Bench (`make bench`; `.github/workflows/bench.yml`; GitHub-only; never a PR gate)

Ingest wall/docs/s/RSS, Qdrant RAM/CPU/disk, agent latency against the pinned image. `/v1/answer` uses the mock LLM — say so in every report. `--check benchmarks/baseline.json` (RSS/disk ×1.5, latency p95 ×3; improvements never fail). Re-baseline is a dedicated PR. GitLab has no bench.

Bench baselines must be captured in the gate's own environment (CI runners) via the `update_baseline` dispatch with repeats ≥3 (noise floor: `--repeats N` aggregates min latency/footprint, max errors/throughput) — never a dev machine: a 24-core-local baseline gating 4-vCPU CI runs at ×3 left near-zero p95 headroom and failed on runner contention (2026-09-02). `scripts/qdrant_sim.py` and `scripts/qdrant_pin.py` have exactly one owner each — do not fork them; `make sim-qdrant` is a fixed-port wrapper.

### Harness invariants (all harness tiers L1/L2/L3)

1. *One gate, one baseline file, one environment*: Bench ≠ L1 ≠ L2 ≠ L3. Never merge GPU numbers into the GitHub-runner bench JSON. Capture in the gate's own environment; env mismatch fails closed with a distinct error, not `p95 > ×3` — the CI bench gate checks (`cpu_count`, `qdrant_image`); the L3 harness gate checks (`cpu_count`, `embed_mode`, `qdrant_image`, `gpu_name`).
2. *Harness PRs touching src/ are production PRs*: Justify runtime changes. Default-off for new transport/settings (`llm_stream: bool = False`). Fail-loud on fallback. Hermetic tests of the new production path, not only script helpers.
3. *Do not change response body contracts to measure*: Headers (`Server-Timing`) and logs are the probe surface; JSON schemas stay frozen.
4. *Fail-closed is the default*: Missing baseline, missing pin, missing header, unmatched join, unparseable judge, empty stream — refuse to score. Never `except: pass` then continue as success. Gate helpers must not drop earlier reasons when baseline is missing.
5. *Make recipes are part of the contract*: Recipes must pass the declared Make variables (`--gate`, `--baseline $(FILE)`, `--url`).
6. *Standing-red live runs are labeled as RC debt, not broken targets*.
7. *Read the last harness review before writing the next layer*.

### Layered harness (`make harness-gate` / `harness-baseline`; RC-only, GPU stack; never a PR gate, never GitLab)

L1 retrieval per class — recall@5/@8, MRR@8, doc-level nDCG@8 (per-doc deduped, gains: doc +1/heading +1/page +1; never aggregate-only), trap precision absolute (one must_not violation fails; sibling allowance shared with the retrieval eval via `must_not_violations`). Gate verdict: zero P0 trap failures AND no per-class regression beyond the baseline's `class_regression_floor` (0.05 default) AND ≥1 primary metric (recall@5, MRR) improving with a paired-bootstrap 95% CI excluding zero. Determinism is pinned: mode-keyed baselines store PER-QUERY values (retrieval is deterministic against the snapshot-pinned index — proven byte-stable), bootstrap is seeded (stdlib `random.Random`), Qdrant pin = exactly one snapshot whose server-assigned name + points count live in the baseline `_meta` (recover ≈30s per 840k points; drift-only restore by default, always by the RECORDED name; gate runs fail closed when the baseline or pin is missing — never pin live state; a drifted record run creates a NEW snapshot of the current state and prunes the old pin (re-adoption happens only under the skip path's exact fingerprint match). L2 (`make harness-l2`; answer tier, live GPU stack, same venue rules): reuses the answer-tier eval's runner (one judging path) and adds citation precision/recall vs `expected_doc_ids` (doc-level, citations mapped to hits by exact cite-string match — `/v1/answer` retrieves with the same limit-8 call `/v1/search` makes, so no regex guessing; L2 must run on the L1-pinned collection — unenforced in code, operator discipline, and any citation that does not map back to the fetched pool FAILS the row — P/R over a silently-wrong join is worse than no measurement; precision averages rows with ≥1 citation while recall includes zero-cite rows at 0), a temp-0 NLI faithfulness judge over the cited excerpts (unparseable judge output is a structural FAIL; label distribution is trend data; the judge never sees citation markers), truncation rate joined from the app's `answer_alert` log lines by request_id (a missing request_id fails the row — a silent false would undercount), and `syntax_pattern` gold (authored per syntax entry in `build_golden_corpus.py`; keyword-presence by design — echo-able, so a pass means the construct was NAMED, not that valid syntax was produced; a miss is a structural FAIL). Structural fails gate the exit code; all rates are trend data (sampling is not run-deterministic). The L2 gate is standing red on known product debt (suffix-less doc-number gap, 4096-window truncation, the MQ trap) until those are fixed — an RC debt signal, not a broken target. L3 (`make harness-l3` / `harness-l3-baseline`; performance & latency tier, live GPU stack, same venue rules): per-stage p50/p95 latency (`embed_ms`, `qdrant_ms`, `llm_ms`, `ttft_ms`) captured from `Server-Timing` headers, Time To First Token (TTFT) via reasoning-model SSE chunk streaming on first content token (agent server must be started with `LLM_STREAM=true`, e.g. via `make run-agent`; default production `llm_stream=False`), and VRAM footprint via `nvidia-smi` under concurrent load (`loadtest.py`) reported as trend data. Uses dedicated mode-keyed baselines (`benchmarks/harness-l3-vllm.json` / `benchmarks/harness-l3.json`), NEVER the CI bench file `benchmarks/baseline.json`. Standing gap: those two L3 files do not exist in the tree yet — do not create them in a random PR; recording them is a dedicated baseline PR. Gating fails closed on env mismatch (`cpu_count`, `embed_mode`, `qdrant_image`, `gpu_name`), requires 0 request errors, 0 missing `Server-Timing` headers, all baseline stages present, and p95 stage latencies within baseline limit (×3). Baselines refuse to record from broken runs (errors > 0 or missing_timings > 0).

### Reports

`scripts/render_report.py` (`make eval-report` / `eval-html` / `eval-compare` / `bench-report` / `bench-html` / `bench-compare`). `scripts/query_demo.py` (`make query-demo`, `make ask`) is inspection, not a substitute for eval.
