# Testing reference

Normative test-design rules. [Live-stack](live-stack.md#verification-minimums)
owns required verification; [agent workflow](agent-workflow.md#conflicts) owns
conflict handling. Distinguish policy, implementation, acceptance and history.

Jump list: [hermetic](#unit-tests-are-hermetic) ·
[claimed-path](#tests-must-lock-the-claimed-path) ·
[shared-doubles](#shared-doubles-share-builders-pin-behavior) ·
[airgap-tier](#airgap-tier) ·
[golden](#golden-corpus-devholdout) · [sim](#simulation-tier) ·
[load](#load-tier) ·
[eval](#evaluation) · [paraphrase](#paraphrase-instrument) ·
[answers](#answer-tier) ·
[bench](#benchmarks) ·
[harness](#harness-invariants-all-harness-tiers-l1l2l3l4) · [reports](#reports)

Read sections relevant to the actual impact, including affected unchanged
consumers and interaction tests; unrelated expensive tiers are not useful.

`pytest` is the gate. `sh scripts/tools/run-task.sh qa:check` runs `ruff check`, `mypy src`, and the
unit suite. Tests generate original PDFs at runtime
(`scripts/make_synthetic_pdf.py`). No binary fixtures in git.
CI must fail if `git ls-files` matches `.pdf` / `.pdx` / `.idx`.
Cover IBM-shaped synthetics (form number, message id, outline) **and**
generic PDFs (no outline, no form number, unknown vendor).
`test_chrome_strip` must keep a **long** page list (≥8 pages). Chrome is
disabled on short docs on purpose.

## Unit tests are hermetic

Do not call live Qdrant, vLLM, or the internet. Fake the client. Parse-only ingest
tests can use `--dry-run`; persistence/publication regressions must exercise the
real non-dry control path against faithful fakes so the claimed behavior runs.

- Patch `httpx2.get` / `httpx2.post` in every unit test that can reach them. Hostnames like `embed-host:9000` are live network. A test that “works because connect failed” is invalid. For scripts that also stream, patch `httpx2.stream` too — or swap the whole `httpx2` module attribute for a URL-keyed fake (the `FakeGateway` pattern in `tests/test_probe_gateway.py`: first-match-wins routes, a `(method, url, headers)` call log, canned-server failures only).
- Requesting the `monkeypatch` fixture does nothing by itself. Register every mutated env key with `monkeypatch.setenv` / `monkeypatch.delenv` **before** the code under test runs, or snapshot with `monkeypatch.setattr(os, "environ", dict(os.environ))`. Autouse fixtures must call `monkeypatch`.
- Do not mutate module-global state (routes on the global app, leftover `os.environ`) that later tests inherit.
- Pin public contracts, not private internals (`client._transport._pool._retries` dies on the next lockfile bump).
- Remove unused fixtures and parameters when touching a test.

## Shared doubles: share builders, pin behavior

Pure builders live in `tests/fakes.py` (`make_hit`, `make_point`,
`TokenizerPostFake`, `HttpxStreamFake` + `PostResp`/`StreamResp`,
`QdrantFake`, `LegacyQdrantFake`, `EmbedderFake`, `RerankerFake`,
`PromotingRerankerFake`, `settings_kw`, `iter_golden_queries`,
`vllm_models_mock`, `embedding_mock`) and `tests/helpers_airgap.py` (`make_bin_tree`,
`run_sh`, `sign_sums`, `skopeo_stub`, `assert_pull_secret_wired`).
`tests/conftest.py` re-exports the retrieval doubles as
backwards-compatible aliases — import from either, define in neither.

Behavior pins stay explicit via arguments, never subclasses: the
sequential-fallback pin needs a double with NO `query_batch_points`
method (`LegacyQdrantFake` — retrieve dispatches on `hasattr`, so a
raising stub would error instead of falling back); `str` vs `ChatResult`
LLM returns lock different coercion paths (do not normalize to one);
limit-slicing, dim-16 recording, and per-file cite shapes stay local
with a comment saying why. A shared-helper change must never silently
flip a fallback pin into a success pin.

Double fidelity includes client-side defaults the production call relies
on: a Qdrant `retrieve` double returns vectors only when `with_vectors=True`
(issue #391 F5 — a double that always returned them hid a missing vector
projection against every real server, and only the disposable-Qdrant lane
caught it). A double for a store that upserts must overwrite same-id points
(issue #391 F2 — a duplicated manifest point reads back as the stale first,
so a pending contract looked committed in unit tests but not against a
server). Alias resolution is part of that fidelity (issue #391 F4):
`AliasQdrant` scripts `get_aliases` plus per-physical
`<physical>__completions` payloads so the gate's resolution and its metadata
read are pinned to the same generation, and `ServingGateFake` is installed
autouse in `tests/conftest.py` — endpoint tests that monkeypatch the
retrieval/LLM seams get a servable generation while gate/refusal tests
install the real `ServingGate` with a scripted Qdrant double, and
integration-marked tests keep the real gate against the real server.

## Tests must lock the claimed path

If the PR claims “CLI override”, “auto-detect”, “unwrap fence”, “sandbox env”, “IPC isolation”, or “dimension recreate”, the test must still pass when the **success** path is forced with mocks.

Do not assert an outcome the fallback would also produce.

Auth-header changes pin both directions on the success leg: header sent when the key is set, header absent (not empty) when unset — the fakes capture `headers` (`TokenizerPostFake`, `HttpxStreamFake`, `FakeGateway` call log) for exactly this.

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
- Retrieved-but-omitted cites are rejected (issue #364): assert the actual outgoing prompt so the fixture cannot accidentally pack every hit; the tail's example cite is not evidence, and `[n]` inside a dropped thinking/extracted script fence is not promoted.
- e2e `/v1/answer` fails on zero citations or “no supporting excerpts”.
- Queried identifiers (`IEA500I`, `LFAREA`, …) must exist in the synthetic `build()` fixture, otherwise the gate cannot fail for the right reason.

IPC / worker changes: round-trip the error record through `pickle.dumps`.
Collection-dimension logic: missing, matching, and mismatched (named `dense` dict **and** single-vector schemas), including `--skip-ingest`.

## Tiers

Required tier selection and commands are maintained only in
[live-stack](live-stack.md#verification-minimums) across the 7 risk categories
(`prose-only`, `test/tool-only`, `publication/retirement lifecycle`, `extraction/ranking`,
`HTTP/lifecycle`, `packaging/deploy`, `release promotion`). The sections below describe
how those instruments work and what their results can establish.

<a id="airgap-tier"></a>
### Air-gap deployment tier (`sh scripts/tools/run-task.sh airgap:dryrun`, `tests/test_airgap_*.py`, local Kind)

The canonical 5-stage deployment pipeline (`airgap:pack` -> `airgap:load` -> `airgap:deploy` -> `airgap:ingest` -> `airgap:smoke`) is verified across three complementary tiers:
1. **Hermetic Test Suite (`pytest tests/test_airgap_*.py`):** Fast unit tests running without a cluster or Docker daemon. Exercises `scripts/airgap/*.sh` via stubs for `helm`, `kubectl`, `oc`, `kustomize`, and `skopeo`. Verifies pre-flight environment validation (`validate.sh`), sneakernet extraction and bootstrap (`bootstrap.sh`), pipeline orchestration (`pipeline.sh`), manifest rendering, string quoting of integers and booleans (`DENSE_DIM`, `INGEST_WORKERS`, `RERANK_ENABLED`), storage class checks (refusing NFS), Jaeger v2 wiring, gateway key strip-or-substitute rendering plus plaintext-key refusal and Secret verification, and fail-closed behavior on `/healthz` probe failures.
2. **CI Pre-Flight Dry-Run (`sh scripts/tools/run-task.sh airgap:dryrun`):** Automated PR gate in GitHub Actions. Renders production Helm templates and Kustomize overlays using test parameters, verifying that all placeholders are substituted and zero leftover `__[A-Z0-9_]+__` patterns remain.
3. **Local Cluster & E2E Rehearsal:** In local development, operators test the complete pipeline against a single-node Kind cluster and local registry container on port 5000 (`localhost:5000`). In CI, `airgap-rehearsal` runs on `main` against an ephemeral namespace in the lab OpenShift cluster, validating the real sneakernet tarball unpack, image push, StatefulSet rollout, and smoke queries.

### Golden corpus (dev/holdout)

`evals/golden.jsonl` is the dev set; `evals/holdout.jsonl` is **frozen** (sha256-pinned at `evals/holdout.jsonl.sha256`). Tune against dev only; `sh scripts/tools/run-task.sh eval:holdout` runs on release candidates only. Every eval/harness script defaults to the dev venue (`evals/golden.jsonl` only); the frozen holdout and the `real_manuals` collection fail closed with exit code 2 across all 8 eval and harness scripts (`scripts/eval_retrieval.py`, `scripts/eval_answers.py`, `scripts/eval_chat.py`, `scripts/harness.py`, `scripts/harness_l2.py`, `scripts/harness_l3.py`, `scripts/harness_l4.py`, `scripts/capture_pool.py`) unless `VENUE=rc` is declared in the environment (`scripts/venue.py`, issue #268). Missing `VENUE=rc` prints `FAIL: ...` to stderr and terminates with exit 2 (`sh scripts/tools/run-task.sh eval:holdout` declares itself, harness tiers require `VENUE=rc` from the operator). Both files are mechanically verified by `sh scripts/tools/run-task.sh eval:verify-golden` (0 FAIL required; rebuild via `scripts/build_golden_corpus.py` from `evals/expert_golden_seed.jsonl` + payload mining). Entries carry `query_class` (message_id/doc_number/syntax/diagnostic/comparative/version/negative/table), `expected_behavior` (answer/abstain), `must_not_retrieve`/`must_not_message_ids` (gated hard-zero within top-5; a chunk co-carrying the query's own message id is the same documented page, not a violation — same sibling-allowance as the builder's trap assertion), `expected_page` (diagnostic), and answer-tier gold fields for the answer eval. Abstain entries carry no `expected_doc_ids` and stay out of recall/MRR denominators. Loading new vendor books (new domains/editions) triggers a re-bind: re-run `scripts/build_golden_corpus.py` (domain entries flip abstain→answer automatically when their identifiers bind), re-author for the new domains, `sh scripts/tools/run-task.sh eval:verify-golden` 0 FAIL, then re-freeze the holdout (new sha) and re-record baselines in one dedicated commit.

<a id="simulation-tier"></a>
### Simulation tier (marker `integration`, `sh scripts/tools/run-task.sh qa:sim`)

Real PDFs → real ingest into a docker Qdrant (`images.txt` pin, or `QDRANT_SIM_URL`) → agent endpoints over the real app. `scripts/mock_vllm.py` is the only stand-in. No retrieval/LLM code is monkeypatched. Docker-only, loopback-only, corpus generated at runtime. Plain `pytest` deselects it (`-m 'not integration'`). The sim lane excludes `tests/test_load_tier.py` (`--ignore` in the Task command and the CI `sim` job); that file runs only in the load lane below, so the two lanes cover every `integration`-marked node with no overlap. CI `sim` job is **fail-closed**: missing docker, any skipped test, or zero passes fails the job. Fetched BM25 weights verify against `bm25-weights.sha256`. Synthetic documents must differ in **body text**, not just metadata. Never pin top-1 across potentially equal-text chunks; assert scoping + presence + within-run determinism.

<a id="load-tier"></a>
### Load tier (marker `integration`, `sh scripts/tools/run-task.sh qa:load`, PR-gated by `.github/workflows/load.yml` on agent/retrieve/ingest/mock paths)

The sim composition plus a real uvicorn agent (`LLM_STREAM=true`) asserting **absolute** contracts under threaded load — zero errors, zero missing `Server-Timing`, per-stream SSE integrity (tokens → exactly one `final`, no `error`), citation parity stream/search/JSON, fixed error envelopes with no leaked internals, determinism after load, abort-storm survival (complete XOR aborted per stream, one `stream_truncated` alert per abort), and a TTFT floor under a paced mock. Never cross-environment comparisons. Fail-closed like sim (no skips; agent stdout goes to a file, never a pipe — an unread pipe wedges every request under load). This lane selects only `tests/test_load_tier.py`; it is the single owner of those nodes.

<a id="evaluation"></a>
### Eval (`sh scripts/tools/run-task.sh eval:retrieval`)

Dev golden set vs the mode-keyed baseline (auto-selected by `EMBED_MODE`; collection mismatch skips the gate with a loud warning). Golden hits are doc-level. Sim runs `test_eval_retrieval_on_synthetic_corpus`.

<a id="paraphrase-instrument"></a>
### Paraphrase instrument (`evals/paraphrase.jsonl`, `sh scripts/tools/run-task.sh eval:paraphrase`)

Operator-phrased queries whose answers exist in the synthetic corpus WITHOUT the query text near-verbatim (no-echo contract pinned hermetically in `tests/test_paraphrase.py`), over lexical-competitor docs. Separate golden set + mode-keyed baselines (`evals/baseline-paraphrase[-vllm].json`) so the main gate is untouched; corpus generated at runtime via `gate_l1.generate_synthetic_golden_corpus`, ingested into a dedicated collection. For semantic A/B work (contextual prefixes, reranker on/off, dense-prefix tuning) where the main set saturates. Not wired into CI.

<a id="answer-tier"></a>
### Answer tier (`sh scripts/tools/run-task.sh eval:answers`, live GPU stack, in-process TestClient like `scripts/test_local_e2e_vllm.py`)

/v1/answer` grounding honesty — answer entries must produce ≥1 explicit (non-inferred) validated citation and must not abstain (the agent's shared marker + shape predicate, #135/#305); abstain/trap entries must not be answered (zero validated citations). Gold substrings judge model phrasing and are suppressed on the canned zero-hits path. The judge never re-parses citations (the agent's validator is the single source of truth). No retries, no finish_reason checks (not in the response contract; the app alerts non-stop per request). Deterministic stratified round-robin sample (`N=24` default, `N=all` full run); reasoning sampling is not run-deterministic — structural FAILs gate, rates are trend data in the manifest.

<a id="benchmarks"></a>
### Bench (`sh scripts/tools/run-task.sh eval:bench`; `.github/workflows/bench.yml`; GitHub-only; never a PR gate)

Ingest wall/docs/s/RSS, Qdrant RAM/CPU/disk, agent latency against the pinned image. `/v1/answer` uses the mock LLM — say so in every report. `--check benchmarks/baseline.json` (RSS/disk ×1.5, latency p95 ×3; improvements never fail). Re-baseline is a dedicated PR. GitLab has no bench.

Bench baselines must be captured in the gate's own environment (CI runners) via the `update_baseline` dispatch with repeats ≥3 (noise floor: `--repeats N` aggregates min latency/footprint, max errors/throughput) — never a dev machine: a 24-core-local baseline gating 4-vCPU CI runs at ×3 left near-zero p95 headroom and failed on runner contention (2026-09-02). `scripts/qdrant_sim.py` and `scripts/qdrant_pin.py` have exactly one owner each — do not fork them; `sh scripts/tools/run-task.sh local:qdrant:up` is a fixed-port wrapper.

### Harness invariants (all harness tiers L1/L2/L3/L4)

1. *One gate, one baseline file, one environment*: Bench ≠ L1 ≠ L2 ≠ L3 ≠ L4. Never merge GPU numbers into the GitHub-runner bench JSON. Capture in the gate's own environment; env mismatch fails closed with a distinct error, not `p95 > ×3` — the CI bench gate checks (`cpu_count`, `qdrant_image`); the L3 harness gate checks (`cpu_count`, `embed_mode`, `qdrant_image`, `gpu_name`, `concurrency`); the L4 gate checks the reference's (`venue`, `embed_mode`, `llm_model_reasoning`).
2. *Harness PRs touching src/ are production PRs*: Justify runtime changes. Default-off for new transport/settings (`llm_stream: bool = False`). Fail-loud on fallback. Hermetic tests of the new production path, not only script helpers.
3. *Do not change response body contracts to measure*: Headers (`Server-Timing`) and logs are the probe surface; JSON schemas stay frozen.
4. *Fail-closed is the default*: Missing baseline, missing pin, missing header, unmatched join, unparseable judge, empty stream — refuse to score. Never `except: pass` then continue as success. Gate helpers must not drop earlier reasons when baseline is missing.
5. *Task dispatch is part of the contract*: Tasks must pass declared inputs to their script owner as exact argv/environment values (`--gate`, `--baseline`, `--url`); use runner-boundary evidence.
6. *Standing-red live runs are labeled as RC debt, not broken targets*.
7. *Read the last harness review before writing the next layer*.

### Layered harness (`sh scripts/tools/run-task.sh eval:harness:gate` / `eval:harness:baseline`; RC-only, GPU stack; never a PR gate, never GitLab)

L1 retrieval per class — recall@5/@8, MRR@8, doc-level nDCG@8 (per-doc deduped, gains: doc +1/heading +1/page +1; never aggregate-only), trap precision absolute (one must_not violation fails; sibling allowance shared with the retrieval eval via `must_not_violations`). Gate verdict: zero P0 trap failures AND no per-class regression beyond the baseline's `class_regression_floor` (0.05 default) AND ≥1 primary metric (recall@5, MRR) improving with a paired-bootstrap 95% CI excluding zero. Determinism is pinned: mode-keyed baselines store PER-QUERY values (retrieval is deterministic against the snapshot-pinned index — proven byte-stable), bootstrap is seeded (stdlib `random.Random`), Qdrant pin = exactly one snapshot whose server-assigned name + points count live in the baseline `_meta` (recover ≈30s per 840k points; drift-only restore by default, always by the RECORDED name; gate runs fail closed when the baseline or pin is missing — never pin live state; a drifted record run creates a NEW snapshot of the current state and prunes the old pin (re-adoption happens only under the skip path's exact fingerprint match). L2 (`sh scripts/tools/run-task.sh eval:harness:l2`; answer tier, live GPU stack, same venue rules): reuses the answer-tier eval's runner (one judging path) and adds citation precision/recall vs `expected_doc_ids` (doc-level, citations mapped to hits by exact cite-string match — `/v1/answer` retrieves with the same limit-8 call `/v1/search` makes, so no regex guessing; L2 must run on the L1-pinned collection — unenforced in code, operator discipline, and any citation that does not map back to the fetched pool FAILS the row — P/R over a silently-wrong join is worse than no measurement; precision averages rows with ≥1 citation while recall includes zero-cite rows at 0), a temp-0 NLI faithfulness judge over the cited excerpts (unparseable judge output is a structural FAIL; label distribution is trend data; the judge never sees citation markers), truncation rate joined from the app's `answer_alert` log lines by request_id (a missing request_id fails the row — a silent false would undercount), and `syntax_pattern` gold (authored per syntax entry in `build_golden_corpus.py`; keyword-presence by design — echo-able, so a pass means the construct was NAMED, not that valid syntax was produced; a miss is a structural FAIL). Structural fails gate the exit code; all rates are trend data (sampling is not run-deterministic). The L2 gate is standing red on known product debt (4096-window truncation, the MQ trap) until those are fixed — an RC debt signal, not a broken target; the suffix-less doc-number filter gap closed at retrieval in #270 (L2 re-run pending). L3 (`sh scripts/tools/run-task.sh eval:harness:l3` / `eval:harness:l3-baseline`; performance & latency tier, live GPU stack, same venue rules): per-stage p50/p95 latency (`embed_ms`, `qdrant_ms`, `llm_ms`, `ttft_ms`) captured from `Server-Timing` headers, Time To First Token (TTFT) via reasoning-model SSE chunk streaming on first content token (agent server must be started with `LLM_STREAM=true`, e.g. via `sh scripts/tools/run-task.sh local:agent`; default production `llm_stream=False`), and VRAM footprint via `nvidia-smi` under concurrent load (`loadtest.py`) reported as trend data. Uses dedicated mode-keyed baselines (`benchmarks/harness-l3-vllm.json` / `benchmarks/harness-l3.json`), NEVER the CI bench file `benchmarks/baseline.json`. `benchmarks/harness-l3-vllm.json` was recorded 2026-09-11 on the RC stand-in host (RTX 5060 Laptop, 24 vCPU, 8 GB); `_meta.env` pins the exact tier. The hash file is intentionally not recorded — L3 is a GPU tier and the CI bench owns CPU-mode perf; do not create `harness-l3.json` in a random PR, recording it is a dedicated baseline PR. Gating fails closed on env mismatch (`cpu_count`, `embed_mode`, `qdrant_image`, `gpu_name`, `concurrency`), requires 0 request errors, 0 missing `Server-Timing` headers, all baseline stages present, and p95 stage latencies within baseline limit (×3). `CONCURRENCY`/`DURATION`/`REQUEST_TIMEOUT` are Task inputs (recorded in `_meta.env`; concurrency is gated) — a slow reasoning model under load needs `REQUEST_TIMEOUT` above the 30s client default. Baselines refuse to record from broken runs (errors > 0 or missing_timings > 0).

L4 (`sh scripts/tools/run-task.sh eval:harness:l4`; same venue rules, `VENUE=rc` for the holdout/corpus)
repeats the L2 sample K times and adds the answer-relevance judge.
Structural fails, request errors, and judge-infra errors gate
unconditionally; per-metric means are gated against
`evals/harness-l4-thresholds.json` with `_meta.tolerance` (default 0.15,
calibrated to the 3-repeat sampling noise at N=24) — at/better than
the reference passes, inside the band holds and writes a human-review
queue, outside fails; the reference refuses cross-tier use (exit 2) and
records venue/embed mode/reasoning model. Recording refuses broken
measurement (request/judge errors) but records through product structural
debt — that count is stored and still gates every run. The deterministic
gate-l1/L1 checks stay the PR gate.

### Reports

`scripts/render_report.py` (`sh scripts/tools/run-task.sh eval:report` / `eval:html` / `eval:compare` / `eval:bench-report` / `eval:bench-html` / `eval:bench-compare`). `scripts/query_demo.py` (`sh scripts/tools/run-task.sh local:query`, `sh scripts/tools/run-task.sh local:ask`) is inspection, not a substitute for eval.

<a id="evidence-design"></a>
## Counterexamples, evidence and consolidation

**Policy authority:** #411 (Increment A), #397 (15 September 2026); broader
consolidation is #388, and general CI enforcement is #370. Required commands and
merge evidence remain in [live-stack](live-stack.md#verification-minimums)
structured across the 7 risk categories (`prose-only`, `test/tool-only`,
`publication/retirement lifecycle`, `extraction/ranking`, `HTTP/lifecycle`,
`packaging/deploy`, `release promotion`).

**CI claims discipline:** A documented merge obligation is not automatically
enforced CI: do not claim checks are automated in CI unless implemented. Under Increment B
(#411 / #370), PR CI enforces deterministic lint and type checking (`ci.yml` `lint` job running
`ruff check src tests` and `mypy src`), safe PR concurrency cancellation, and profile-selected
review environments with runtime manifests (`candidate-manifest.json`). The reviewer job gates its
machine-readable result with `scripts/review_tooling.py validate-review --require-payload
--check-git`: a missing, malformed, or mis-attributed payload fails the job, while a valid
`changes_required` verdict remains a successful review execution. Candidate acceptance summaries
(`scripts/review_tooling.py summarize-acceptance`) are implemented and unit-tested but are not yet
wired as a required check or an always-scheduled PR workflow; that gating is the tracked follow-up
to this increment and must not be claimed as enforcement until the maintainer configures the check.

**Review profile selection:** `scripts/review_tooling.py profile` is the executable projection of
[verification minimums](live-stack.md#verification-minimums), not a second policy language. It maps
changed paths to the categories `prose`, `tooling`, `tests`, `deploy`, `storage`, `http`, and
`tracing`; cross-layer unions select `full`. `deploy` covers the packaging/deployment/defaults
surfaces (Containerfiles, `deploy/` kustomize overlays, `pyproject.toml`, lockfiles,
`scripts/airgap/`, air-gap shell tests) without heavy services. Unmapped paths and unreadable or
empty diffs fail
closed to `full`, so an ambiguous executable change is never reviewed as docs-only. The profile
selects the reviewer environment and the acceptance lanes; it does not waive any required evidence.

**Resource boundaries & invariant protection:** Prose/docs and test/tooling PRs
must respect their resource boundary and avoid starting heavy services (GPU, Qdrant,
model gateways, Jaeger). Documentation or tooling changes cannot silently waive,
alter, or bypass core data invariants (UUID5 chunk keys, 4-type vocabulary, residue
audit, fail-closed contracts).

For a claimed invariant, retain an independent expected outcome. Ask what wrong
implementation could pass the local assertion, then test that boundary. Metadata
counts/digests can prove only the membership and semantics their assertions
actually cover. Apply missing/corrupt data, interruption, retry, concurrent
readers/writers, warm caches, rollback and configuration cases where relevant;
record why exclusions do not affect the contract.

### A. Producer-to-consumer round trips

For a transported identifier or configuration value, use the actual producer and inspect the actual consumer artifact:

```text
source_rev_key(... multi-word product ...)
  -> documented operator input
  -> actual launcher/render path
  -> parsed YAML args
  -> backend selector against synthetic approved inventory
```

Assert exact value and argument count, not substring presence or YAML parseability alone. Keep an independent expected result; do not call the changed serializer on both sides of the assertion. Include a domain boundary that differs from the comfortable happy-path fixture, such as internal whitespace or an actual delimiter. No real private source is needed.

A reviewer cannot narrow the supported domain to the current sample corpus merely because the producer's broader inputs expose a defect. Either preserve the contract or obtain an explicit authorized compatibility decision with truthful documentation.

### B. One action after successful cleanup

For a lifecycle change, add or identify a retained sequence test such as:

```text
publish -> force repair -> successful swap -> sidecar cleanup
        -> ordinary identical run -> another identical run
```

Assert stable logical identity, exact retained membership, and the absence of unnecessary mutations/embedding/allocation. Do not turn this into a timing benchmark when counters/read-only assertions answer the question. A crash-before-cleanup test is separate evidence, not a substitute. Preserve meaningful existing crash/retry/rollback tests without multiplying them across unrelated transports/models.

### C. Equivalent validation; explicit emission/commit boundaries

Use a compact table for buffered/streaming or sync/async paths that implement the same validation rule. Include an error that coexists with a success-shaped field, wrong field types, and a healthy control. Expected verdicts must be independently specified.

Track distinctions that drive recovery or publication:

```text
received != validated != emitted
built != verified != published
source identity != stored-content verification
```

For streaming recovery, assert yielded events and fallback-call counts through the actual iterator. For storage, inspect actual records and protected operations. A flag saying `complete` or a test named `parity` is not itself proof of the corresponding behavior.

### D. Curate exploratory tests before committing

Challengers may explore freely in an isolated workspace, but retain only clear cases protecting distinct behavior. Add them to the owning suite; use parameterization when the rule is genuinely shared. Do not preserve separate large files named after each agent or review round. Map replacements to their retained coverage; removing a forbidden-behavior pin requires explicit contract authority, not merely the desire for a green run.

Existing homes: completion/publication/representation/serving suites cover
those boundaries; API/chat/webui/stream suites cover adapters and completion;
air-gap shell suites cover configuration precedence and both render paths.
Prefer adding a regression to these behavior-focused suites. Every new handler,
branch and error shape needs a reachable case. Input handling should cover empty,
multi-digit, wrapped (quotes, blockquotes, backticks, parentheses, bold, links,
angle brackets), inline, top-placed, missing-blank-line and case-folded forms when
those inputs are meaningful to the parser.

Before consolidating tests, map `old case → retained behavior and owner` or give
a reason to retire an implementation-only pin. Never delete/weaken a failing
product test to get green. A fake must preserve the real operation's projection,
capability, overwrite, filtering and alias semantics; do not normalize every
return shape or invent a universal fake framework. Examples: omitted
`with_vectors` must not return vectors; absent batch capability is different from
a method that raises; upsert replaces an existing same-ID point. Read-only
contracts must assert no writes, rather than merely successful return values.

Evidence records separate observed execution (SHA, command, exit, counts and
location), static reasoning, proposed tests and unavailable checks. A skipped
required check is not a pass. Historical green runs are not current release
evidence. The PR table can group related tests; do not fabricate a run or require
a permanent artifact for every small assertion. New context-tool tests use
temporary trees/mocked subprocesses, not live Docker, GPU or a private corpus.
