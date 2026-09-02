# AGENTS.md

Working agreement for coding agents on this repository.
Revise this file in the same PR that learns a new rule.
If a review comment conflicts with this file, follow this file and note the conflict on the PR.

## Roles

| Role | Does | Does not |
|---|---|
| Planner / architect / reviewer (human + Perplexity) | Design docs, issues, PR review, this file | Application code, tests, CI YAML except when the issue says otherwise |
| Coding agent | Implement issues, tests, CI, Helm/Makefile as specified | Invent product scope, commit secrets/PDFs, merge own PRs |

Implement only what the issue asks. New scope → comment on the issue, do not silently expand.
If CI fails, fix the production cause. Do not delete or weaken tests to go green.

## Start of work

1. `git status` and `git branch`. Never start on a dirty tree or an already-merged branch.
2. Fresh branch from latest main: `git fetch origin main && git checkout -b <type>/<issue>-<short> origin/main`.
   Types: `feat/`, `fix/`, `docs/`. One concern per branch.
3. Do not bundle application code into an open docs-only PR, or docs-only work into a code PR, unless the docs are the standing-rule note for that code.
4. Read the layer table below and change only the module that owns the decision.

## Product constraints (do not regress)

- User supplies PDFs at runtime. **Never** commit `.pdf`, `.pdx`, `.idx`, embeddings, Qdrant snapshots, or vendor manuals (IBM, Broadcom, BMC, Precisely, or anyone else).
- Parser is generic. IBM form numbers / `XXXnnnY` messages are optional payload, not ingest gates. `doc_id` falls back to filename stem. Default vendor is `unknown` unless path, CLI, or text says otherwise.
- Runtime is air-gapped OpenShift. No public internet from cluster or in-cluster CI. Images, wheels, and BM25 weights are mirrored in.
- Runtime Python is CPython **3.14 GIL** (`requires-python >= 3.14`). No free-threading (`3.14t`), no experimental JIT.
- Qdrant point ids are UUID or unsigned int only (UUID5 of the chunk key). sha256 hex is invalid. `query_points` takes `query_filter`, not `filter`.
- `/v1/answer` uses the reasoning model only and never retries. `/v1/search` does not call an LLM.
- Qdrant data PVC is RWO block, not NFS. Corpus may be NFS read-only. Ingest-work scratch on prod is also RWO block.
- Unprivileged Qdrant image, `restricted-v2` SCC, ClusterIP only, no public Route to Qdrant. Agent Route only if `AGENT_ROUTE=true`.
- Qdrant server is **1.19.0** `*-unprivileged`; `qdrant-client` in the lockfile must match; Helm chart is vendored at `charts/qdrant-1.19.0.tgz`. Do not `helm repo add` on the air-gap host.
- This repository does **not** install vLLM, LiteLLM, Splunk, or GPU operators. Dense embed is the other team's in-cluster vLLM (`VLLM_BASE_URL`). Sparse is local FastEmbed with baked weights. Never Qdrant Cloud inference.
- `EMBED_MODE=hash` is **CI/dev only**. Prod requires internal vLLM. Never set `EMBED_MODE` in prod manifests or the default image env. Agent refuses hash without `ALLOW_HASH_MODE=true`.
- `DENSE_DIM` / `EMBED_MODEL` / `LLM_MODEL_REASONING` come from the owning team. Do not hardcode a model.
- No git submodules (they break `git bundle`). Vendor third-party trees by copy at a pinned SHA, LICENSE + NOTICE + pin file, dedicated pin-bump PRs only.
- Keep the pipeline boring. Do not add LangChain, LlamaIndex, or a second vector DB.

## Git

- Public forge is **GitHub**. Enterprise forge is **air-gapped GitLab**. Same history moves by bundle / sneakernet; do not maintain a divergent tree.
- Default branch is `main`. Never push application commits to `main`.
- One concern per PR. Rebase on `main` before asking for review; no merge commits unless the reviewer asks.
- Commits: imperative, present tense, say *why* if not obvious (`Fix chrome threshold so 3-page PDFs are not wiped`).
- PR / MR body: issue number, what changed, how tested, air-gap / copyright impact if any. Update the body in the **same push** as the code. A stale body is a blocker.
- Do not force-push `main`. Force-push feature branches only after rebase, before review comments exist.
- Never commit: `.env`, `airgap.env`, secrets, tokens, `*.tar`, wheelhouses. `airgap.env.example` is allowed. Pack output lives in `dist/` (gitignored).

## Definition of done — before every push

All of these must hold. Self-review the **diff**, not the PR body.

- `python3 -m pytest`, `python3 -m mypy src`, and `python3 -m ruff check src tests scripts` are clean locally.
- Branched fresh from `origin/main`, single concern, not an already-merged branch.
- Every new behavior has a test that fires the **claimed path**, not only the exception/fallback path. See Testing.
- Every new handler, branch, and error shape has a reachable test. A handler no test can fire is dead code.
- Input-handling / parsers were probed adversarially: empty, multi-digit, wrapped (`> `, backticks, quotes, parens, `**bold**`, `[links](url)`, `<angle>`), inline/non-anchored, top-placed, missing blank lines, case folding.
- `git status` shows no untracked toolchain artifacts (`node_modules/`, lockfiles from experiments, venvs, caches). Experiments run outside the repo tree.
- Every claim in the PR body is true of the code in **this** push. Grep for the counterexample before writing “defaults unchanged”, “no runtime change”, “CLI overrides work”, or “prevents env leaks”.
- Every change to a default, constant, timeout, retry count, limit, or chunk size is called out in the PR body.
- Retrieval changes (embedder, chunking, RRF, filters, query shape) include `make eval` vs the mode-keyed baseline (`evals/baseline.json` hash, `evals/baseline-vllm.json` live) in the PR body (identifier recall@1 strict 1.0; overall recall@1 ×0.9, recall@5 ×0.95, MRR ×0.95; 0 query errors). Tooling PRs that also touch those paths still owe the numbers. Re-baseline is a dedicated PR (`make eval-baseline`).
- Local vLLM / Makefile / script work must not change production chunking, retrieval constants, or ingest-worker semantics in the same PR. If they must, that is two concerns: split, or pay the eval rule above.

## Testing

`pytest` is the gate. Tests generate original PDFs at runtime (`scripts/make_synthetic_pdf.py`). No binary fixtures in git.
CI must fail if `git ls-files` matches `.pdf` / `.pdx` / `.idx`.
Cover IBM-shaped synthetics (form number, message id, outline) **and** generic PDFs (no outline, no form number, unknown vendor).
`test_chrome_strip` must keep a **long** page list (≥8 pages). Chrome is disabled on short docs on purpose.

### Unit tests are hermetic

Do not call live Qdrant, vLLM, or the internet. Fake the client. Ingest tests use `--dry-run`.

- Patch `httpx2.get` / `httpx2.post` in every unit test that can reach them. Hostnames like `embed-host:9000` are live network. A test that “works because connect failed” is invalid.
- Requesting the `monkeypatch` fixture does nothing by itself. Register every mutated env key with `monkeypatch.setenv` / `monkeypatch.delenv` **before** the code under test runs, or snapshot with `monkeypatch.setattr(os, "environ", dict(os.environ))`. Autouse fixtures must call `monkeypatch`.
- Do not mutate module-global state (routes on the global app, leftover `os.environ`) that later tests inherit.
- Pin public contracts, not private internals (`client._transport._pool._retries` dies on the next lockfile bump).
- Remove unused fixtures and parameters when touching a test.

### Tests must lock the claimed path

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

### Simulation, eval, bench

- Golden corpus (dev/holdout): `evals/golden.jsonl` is the dev set; `evals/holdout.jsonl` is **frozen** (sha256-pinned at `evals/holdout.jsonl.sha256`). Tune against dev only; `make eval-holdout` runs on release candidates only. Both files are mechanically verified by `make verify-golden` (0 FAIL required; rebuild via `scripts/build_golden_corpus.py` from `evals/expert_golden_seed.jsonl` + payload mining). Entries carry `query_class` (message_id/doc_number/syntax/diagnostic/comparative/version/negative), `expected_behavior` (answer/abstain), `must_not_retrieve`/`must_not_message_ids` (gated hard-zero within top-5; a chunk co-carrying the query's own message id is the same documented page, not a violation — same sibling-allowance as the builder's trap assertion), `expected_page` (diagnostic), and answer-tier gold fields for the answer eval. Abstain entries carry no `expected_doc_ids` and stay out of recall/MRR denominators. Loading new vendor books (new domains/editions) triggers a re-bind: re-run `scripts/build_golden_corpus.py` (domain entries flip abstain→answer automatically when their identifiers bind), re-author for the new domains, `make verify-golden` 0 FAIL, then re-freeze the holdout (new sha) and re-record baselines in one dedicated commit.

- Simulation tier (marker `integration`, `make sim`): real PDFs → real ingest into a docker Qdrant (`images.txt` pin, or `QDRANT_SIM_URL`) → agent endpoints over the real app. `scripts/mock_vllm.py` is the only stand-in. No retrieval/LLM code is monkeypatched. Docker-only, loopback-only, corpus generated at runtime. Plain `pytest` deselects it (`-m 'not integration'`). CI `sim` job is **fail-closed**: missing docker, any skipped test, or zero passes fails the job. Fetched BM25 weights verify against `bm25-weights.sha256`. Synthetic documents must differ in **body text**, not just metadata. Never pin top-1 across potentially equal-text chunks; assert scoping + presence + within-run determinism.
- Eval: `make eval`, dev golden set vs the mode-keyed baseline (auto-selected by `EMBED_MODE`; collection mismatch skips the gate with a loud warning). Golden hits are doc-level. Sim runs `test_eval_retrieval_on_synthetic_corpus`.
- Answer tier (`make eval-answers`, live GPU stack, in-process TestClient like `scripts/test_local_e2e_vllm.py`): `/v1/answer` grounding honesty — answer entries must produce ≥1 validated citation and must not refuse; abstain/trap entries must not be answered (zero validated citations). Gold substrings judge model phrasing and are suppressed on the canned zero-hits path. The judge never re-parses citations (the agent's validator is the single source of truth). No retries, no finish_reason checks (not in the response contract; the app alerts non-stop per request). Deterministic stratified round-robin sample (`N=24` default, `N=all` full run); reasoning sampling is not run-deterministic — structural FAILs gate, rates are trend data in the manifest.
- Bench (`make bench`; `.github/workflows/bench.yml`; GitHub-only; never a PR gate): ingest wall/docs/s/RSS, Qdrant RAM/CPU/disk, agent latency against the pinned image. `/v1/answer` uses the mock LLM — say so in every report. `--check benchmarks/baseline.json` (RSS/disk ×1.5, latency p95 ×3; improvements never fail). Re-baseline is a dedicated PR. GitLab has no bench.
- Bench baselines must be captured in the gate's own environment (CI runners) via the `update_baseline` dispatch with repeats ≥3 (noise floor: `--repeats N` aggregates min latency/footprint, max errors/throughput) — never a dev machine: a 24-core-local baseline gating 4-vCPU CI runs at ×3 left near-zero p95 headroom and failed on runner contention (2026-09-02). `scripts/qdrant_sim.py` and `scripts/qdrant_pin.py` have exactly one owner each — do not fork them; `make sim-qdrant` is a fixed-port wrapper.
- Harness invariants (all harness tiers L1/L2/L3):
  1. *One gate, one baseline file, one environment*: Bench ≠ L1 ≠ L2 ≠ L3. Never merge GPU numbers into the GitHub-runner bench JSON. Capture in the gate's own environment; env mismatch (`cpu_count`, `qdrant_image`, `embed_mode`, `gpu_name`) fails closed with a distinct error, not `p95 > ×3`.
  2. *Harness PRs touching src/ are production PRs*: Justify runtime changes. Default-off for new transport/settings (`llm_stream: bool = False`). Fail-loud on fallback. Hermetic tests of the new production path, not only script helpers.
  3. *Do not change response body contracts to measure*: Headers (`Server-Timing`) and logs are the probe surface; JSON schemas stay frozen.
  4. *Fail-closed is the default*: Missing baseline, missing pin, missing header, unmatched join, unparseable judge, empty stream — refuse to score. Never `except: pass` then continue as success. Gate helpers must not drop earlier reasons when baseline is missing.
  5. *Make recipes are part of the contract*: Recipes must pass the declared Make variables (`--gate`, `--baseline $(FILE)`, `--url`).
  6. *Standing-red live runs are labeled as RC debt, not broken targets*.
  7. *Read the last harness review before writing the next layer*.

- Layered harness (`make harness-gate` / `harness-baseline`; RC-only, GPU stack; never a PR gate, never GitLab): L1 retrieval per class — recall@5/@8, MRR@8, doc-level nDCG@8 (per-doc deduped, gains: doc +1/heading +1/page +1; never aggregate-only), trap precision absolute (one must_not violation fails; sibling allowance shared with the retrieval eval via `must_not_violations`). Gate verdict: zero P0 trap failures AND no per-class regression beyond the baseline's `class_regression_floor` (0.05 default) AND ≥1 primary metric (recall@5, MRR) improving with a paired-bootstrap 95% CI excluding zero. Determinism is pinned: mode-keyed baselines store PER-QUERY values (retrieval is deterministic against the snapshot-pinned index — proven byte-stable), bootstrap is seeded (stdlib `random.Random`), Qdrant pin = exactly one snapshot whose server-assigned name + points count live in the baseline `_meta` (recover ≈30s per 840k points; drift-only restore by default, always by the RECORDED name; gate runs fail closed when the baseline or pin is missing — never pin live state; a drifted record run creates a NEW snapshot of the current state and prunes the old pin (re-adoption happens only under the skip path's exact fingerprint match). L2 (`make harness-l2`; answer tier, live GPU stack, same venue rules): reuses the answer-tier eval's runner (one judging path) and adds citation precision/recall vs `expected_doc_ids` (doc-level, citations mapped to hits by exact cite-string match — `/v1/answer` retrieves with the same limit-8 call `/v1/search` makes, so no regex guessing; L2 must run on the L1-pinned collection and any citation that does not map back to the fetched pool FAILS the row — P/R over a silently-wrong join is worse than no measurement; precision averages rows with ≥1 citation while recall includes zero-cite rows at 0), a temp-0 NLI faithfulness judge over the cited excerpts (unparseable judge output is a structural FAIL; label distribution is trend data; the judge never sees citation markers), truncation rate joined from the app's `answer_alert` log lines by request_id (a missing request_id fails the row — a silent false would undercount), and `syntax_pattern` gold (authored per syntax entry in `build_golden_corpus.py`; keyword-presence by design — echo-able, so a pass means the construct was NAMED, not that valid syntax was produced; a miss is a structural FAIL). Structural fails gate the exit code; all rates are trend data (sampling is not run-deterministic). The L2 gate is standing red on known product debt (suffix-less doc-number gap, 4096-window truncation, the MQ trap) until those are fixed — an RC debt signal, not a broken target. L3 (`make harness-l3` / `harness-l3-baseline`; performance & latency tier, live GPU stack, same venue rules): per-stage p50/p95 latency (`embed_ms`, `qdrant_ms`, `llm_ms`, `ttft_ms`) captured from `Server-Timing` headers, Time To First Token (TTFT) via reasoning-model SSE chunk streaming on first content token (agent server must be started with `LLM_STREAM=true`, e.g. via `make run-agent`; default production `llm_stream=False`), and VRAM footprint via `nvidia-smi` under concurrent load (`loadtest.py`) reported as trend data. Uses dedicated mode-keyed baselines (`benchmarks/harness-l3-vllm.json` / `benchmarks/harness-l3.json`), NEVER the CI bench file `benchmarks/baseline.json`. Gating fails closed on env mismatch (`cpu_count`, `embed_mode`, `qdrant_image`, `gpu_name`), requires 0 request errors, 0 missing `Server-Timing` headers, all baseline stages present, and p95 stage latencies within baseline limit (×3). Baselines refuse to record from broken runs (errors > 0 or missing_timings > 0).
- Reports: `scripts/render_report.py` (`make eval-report` / `eval-html` / `eval-compare` / `bench-report` / `bench-html` / `bench-compare`). `scripts/query_demo.py` (`make query-demo`, `make ask`) is inspection, not a substitute for eval.

## CLI, Makefile, and local vLLM

User-supplied `--embed-model`, `--model`, `--embed-url`, `--vllm-url`, `--embed-mode`, `--dense-dim`, and matching Makefile/`ENV` values must be applied or fail nonzero with a message. Never silently keep `load_settings()` values after a `/models` probe. Ambiguous auto-detect (`len(avail) != 1` and no match) fails closed.

- Quote every shell expansion. Never stash JSON flags in an unquoted `${MODEL_ARGS}` string.
- Never `export` a mode variable globally in the Makefile: make exports reach every recipe, and the airgap scripts refuse `EMBED_MODE=hash` fail-closed (a global export broke the CI airgap-dryrun). Scope it: `eval eval-baseline …: export EMBED_MODE := $(EMBED_MODE)` (immediate expansion — a recursive `=` self-references under a target-specific directive).
- `case` globs are case-sensitive: `*embed*` does not match `Embedding`. Match `*embed*` and `*Embed*` (or use a case-insensitive test).
- Pin vLLM image tags that actually implement the flags you pass (`gemma4` parsers, `--task embedding`). `:latest` and stale minors are production bugs.
- Local 8GB co-residency: reasoning port 8000 `GPU_MEM=0.65`; embedding port 8001 `GPU_MEM=0.30` with `--task embedding`; solo reasoning `GPU_MEM=0.85`.
- `scripts/qdrant_sim.py` / `scripts/qdrant_pin.py` remain the only docker-lifecycle and pin-parse owners.

## Error contract

- Client response bodies never contain exception text, upstream bodies, or internal detail — on any status, including 200/degraded. Fixed message + stable `code` client-side; `str(exc)` and upstream text go to logs only.
- Catch the narrowest exception around the smallest call. The same fault produces the same error code on every endpoint.
- If the contract claims a stable error shape, register and pin handlers for 404/405/500. Do not leave the framework default.
- Logs are one JSON object per line via `logs.configure_logging` — ids, counts, `elapsed_ms`; never secrets or PDF text. Ingest parse workers (spawn) return records for the parent to log; they never inherit the handler.

## One rule per concept

- When two paths interpret the same data (validate vs strip, parse vs render, allow vs deny), they share one helper. Two regexes for one concept will diverge, and the divergence is the bug.
- When review flags one instance, sweep every sibling site in the same push: every branch of the function, every job in the workflow, every call site.
- After any fix, re-scan the touched file for variants of the same bug class. Do not fix only the quoted line.
- A refactor labeled “no runtime change” must not share clients, pools, or mutable state across features. Sharing a pool **is** a runtime change.

## Settings and lifecycle

- All timeouts, retries, batch sizes, and limits come from Settings with bounded defaults; no magic numbers in call sites. Each new setting gets a default assertion in `test_config.py`.
- Split a setting when it would cover two different call shapes (`qdrant_timeout_s` vs `qdrant_ingest_timeout_s`; health ping vs embeddings).
- Everything opened in lifespan is closed in lifespan. `close()` must not null a pool such that the next call silently rebuilds one.
- No dead state: never read a `request.state` field nothing sets; never keep a handler nothing can raise.

## Pipeline layers

New behavior belongs in the layer that already owns that decision. Do not thread vendor-specific ifs through retrieve/agent if parse/classify can emit payload.

| Module | Owns |
|---|---|
| `walk` | `*.pdf` only; skip catalogs; path layout `vendor/product/version/` |
| `ibm_pdf` (parse) | Open, metadata, optional IBM signals, generic fallbacks |
| `chrome` | Repeated headers/footers; never threshold=1; skip docs under 8 pages |
| `chunk` | Outline → else whole doc; UUID5 ids; heading path; `SECTION_MAX_CHARS = 3500` |
| `classify` | `message` / `syntax` / `table` / `narrative` |
| `embed` | Dense from internal vLLM; sparse local (no Cloud inference) |
| `qdrant_io` | Collection + payload indexes **before** load; dim fail-fast |
| `retrieve` | Filters in prefetch; hybrid dense+BM25 |
| `agent` | HTTP API; citation validation |

Standing #20 rules: embed / Qdrant points / LLM are `Protocol`s in `ports.py`; upserts are batched (`Settings.batch_size`); payload indexes exist before load; every outbound call has a Settings timeout.

Ingest workers (`_parse_one`) trap exceptions and return plain `InventoryRecord(status="error")`. Unpicklable `httpx2.HTTPStatusError` objects crash `ProcessPoolExecutor` across spawn IPC.

## GitHub vs GitLab CI

- Keep **`.gitlab-ci.yml` at the repo root** so an air-gap clone runs pipelines with no rewrite.
- Keep **`.github/workflows/ci.yml`** for GitHub. Job *meaning* stays aligned: refuse committed `.pdf`/`.pdx`/`.idx`, then pytest. Change both in the same PR.
- GitLab runners have **no internet**. No Docker Hub-only images. No `pip install` from PyPI.
- No internal hostnames, registry URLs, or tokens in `.gitlab-ci.yml`. Use project variables: `CI_PYTHON_IMAGE`, `CI_RUNNER_TAG` (default `airgap`), `PIP_INDEX_URL`, `PIP_FIND_LINKS`. If neither index nor wheelhouse is set, fail closed.
- Coding agents change `.gitlab-ci.yml` only when an issue asks. Do not add deploy/helm/image-build/pack stages unless the issue says so.
- GitHub trigger split: markdown-only changes (excluding vendored `.agents/`/`vendor/` docs) run **no** GitHub checks (`ci.yml`/`e2e.yml` paths-ignore `*.md`). Mixed changes run ci + e2e. Vendored-only bumps run nothing. GitLab keeps hygiene + pytest on every MR (no e2e, no sim). The hygiene gate must never silently skip on the air-gap side.
- Connected-path E2E (GHCR images, lab OpenShift smoke, air-gap runbook rehearsal) lives **only** in `.github/workflows/e2e.yml`. Air-gap GitLab must not talk to that cluster or GHCR. Ephemeral `rag-ci-<sha>` / `rag-gap-<sha>` namespaces; cleanup is `if: always()`.
- opencode reviewer is GitHub-only. Never mirror it into `.gitlab-ci.yml`. Install steps are inlined per job — no local composite action under `.github/actions/` (local actions re-resolve `action.yml` at post after the agent moves the tree).

### Workflow supply-chain

- Pin third-party actions to a full commit SHA. Comment what the pin does **not** cover (runtime-fetched installers, `releases/latest` binaries). Runtime artifacts are version-pinned **and** sha256-verified in-repo. Never `curl | bash` an unpinned installer in a job that holds secrets or `id-token: write`.
- Invoke pinned binaries by absolute path; `$GITHUB_PATH` appends, so PATH order can bypass the pin.
- Every job declares least-privilege `permissions`, `timeout-minutes`, and a `concurrency` group with a fallback (`|| github.run_id`). Secret-gated jobs fail closed; PR jobs guard forks.
- Third-party session/share flags default OFF on every job (`SHARE: "false"` on all of them). This repo is the public mirror.

## Image refs (connected factory)

- Connected `main` is the only image factory. The air-gap never builds Containerfiles until an issue says so.
- One string, everywhere: `ghcr.io/<owner-lowercase>/qdrant-pdf-rag-{ingest,agent}:<full-git-sha>`. Full SHA is `git rev-parse HEAD` / `$GITHUB_SHA`, **never** `${GITHUB_SHA::7}`. That exact string is used for `docker tag`, `docker push`, kustomize sed, `airgap-pack`, and `airgap-load`.
- Makefile local names are `mainframe-rag/{ingest,agent}` — retag to the GHCR ref **before** push.
- Third-party pins live in `images.txt` (Qdrant unprivileged, UBI). A `requirements.lock.txt` bump requires `make wheelhouse bm25-weights` on CPython 3.14 and a connected image rebuild. Dedicated PR, not drive-by. `qdrant-client` pin tracks the 1.19 server/chart.
- Do not add unpublished extras (`types-httpx2`). `httpx2` ships types. A dependency no module imports is a phantom (`test_no_litellm_anywhere`). Audit `pyproject.toml` before adding.
- Images: UBI, non-root, `--no-index` from `/wheelhouse`. Bake BM25 weights (`make bm25-weights`).

## Overlays (never mix CI and prod)

- **CI (lab, connected only):** `overlays/ci/values.yaml` + `deploy/kustomize/overlays/ci` — 1 replica / 1Gi, `EMBED_MODE=hash`, synthetic PDFs generated in-cluster, GHCR pulls. Never copy the CI ingest Job into prod.
- **Prod (air-gap):** `overlays/openshift/values.yaml` + `deploy/kustomize/overlays/openshift` (agent) + `overlays/openshift-ingest` (one-shot Job). 3 replicas / 500Gi / unprivileged / RWO. No `EMBED_MODE` key. Corpus is a caller-supplied PVC. Do not shrink prod values to CI sizes.
- Placeholders in git (`__TOKEN__`, `ghcr.io/OWNER`). Render must fail closed on leftover `__[A-Z][A-Z0-9_]*__`. No real registries, namespaces, or URLs in git. Helm values stay placeholders (`INTERNAL_REGISTRY` / `REGISTRY_INTERNAL`, `PULL_SECRET`, `STORAGE_CLASS`).
- When `PULL_SECRET` is set, it must reach Helm Qdrant **and** agent/ingest pods. Confirm rendered YAML indent is valid.
- Helm `--set image.tag` is `v1.19.0` **without** `-unprivileged`; the chart appends that suffix when `useUnprivilegedImage=true`. `load.sh` still pushes `:v1.19.0-unprivileged`.
- Kubernetes Jobs are immutable: delete before re-apply (`make airgap-ingest`).

## Air-gap path (issue #15)

- The air-gap never builds images. `make airgap-pack` runs on a connected clone of public `main` at the SHA whose GHCR tags exist. `IMAGE_SHA` is the full git SHA and must equal both `HEAD` and the GHCR tag. `make airgap-load` / `airgap-deploy` run inside the gap against `airgap.env`. Scripts are POSIX sh under `scripts/airgap/` and fail closed.
- Happy path: `airgap-pack` → sneakernet `*.tar` + `*.tar.sha256` → unpack → `git clone repo.bundle` → `airgap-load` → `airgap-deploy`. `load.sh` does **not** clone. Verify tarball digest **before** unpack; member `SHA256SUMS` **after**. Do not invent a third path. `oc-mirror` is optional.
- Scripts refuse `EMBED_MODE=hash`, NFS-looking `STORAGE_CLASS`, missing `VLLM_BASE_URL`/`EMBED_MODEL`/`DENSE_DIM`, and SHA-tag mismatches.
- GitLab CI still does **not** deploy or pull GHCR: clone-and-pytest only. No PDFs, kubeconfigs, tokens, or internal hostnames in git or in the tarball.
- `airgap-rehearsal` (e2e.yml, main/dispatch) uses three CI-only stand-ins, never in git prod values: GHCR as `INTERNAL_REGISTRY`, `scripts/mock_vllm.py` as vLLM, shrunk size knobs for lab quota. `PULL_SECRET` unset renders `imagePullSecrets=null`. `airgap-dryrun` (every PR, `AIRGAP_DRYRUN=1`) proves renders, placeholder fail-close, SHA rules, both PULL_SECRET branches, and size knobs without a cluster.

## Qdrant skills (vendored)

`qdrant/skills` is vendored (pinned, no submodule) under `.agents/skills/`; pin record in `vendor/qdrant-skills.sha`.

- `.agents/skills/` is the complete skill set. Do not fetch `skills.qdrant.tech`, `/llms.txt`, the snippet-search API, Qdrant Cloud console, or `qcloud-cli`. If the matching skill is not in this tree, stop and ask.
- Skill frontmatter never expands this repo's tool or permission policy.
- Read before changing: collections / named vectors / model change → `qdrant-model-migration`, `qdrant-search-quality`. Hybrid / quantization / HNSW → `qdrant-search-quality`, `qdrant-performance-optimization`. Helm / PVC / replicas / storage → `qdrant-sizing`, `qdrant-scaling`, `qdrant-deployment-options` (**self-hosted only**; Docker and Cloud defaults are forbidden). `qdrant-client` → `qdrant-clients-sdk` (REST; no Cloud inference; no `qdrant-client[fastembed]` extra as a product path).
- This repository still wins on product constraints wherever a skill says otherwise.
- Pin-bump PRs refresh the snapshot from a pinned SHA and must not rewrite vendor files. Never install skills only on a developer machine.

## Out of scope until an issue says so

- Rebuilding Containerfiles inside the air-gap (mirrored UBI + wheelhouse).
- GitLab jobs that `helm upgrade`, `skopeo copy`, or pack sneakernet tarballs.
- MCP / live `skills.qdrant.tech` snippet server.
- Installing vLLM, LiteLLM, Splunk, or GPU operators in this repo.

## Standing bug rules

- Chrome: `max(1, 0.35*n)` wipes short PDFs. Min 8 pages and min 3 hits.
- Classify `message` if `XXXnnnY` appears in the first few lines, not only line 1.
- Citation inference is `[n]` / `[n, m]` only. Parentheses are IBM-manual noise.
- `SECTION_MAX_CHARS = 3500` (not 6000): table-dense / code pages must stay inside 4096-token embedders.
- Context budgeting: complex reasoning queries cap prompt manual excerpts at 4,500 chars (Settings.prompt_max_context_chars_complex) with type-aware chunk caps: syntax, message, and table chunks preserve full fidelity up to 3,000 chars, while narrative prose is capped at 1,100 chars (Settings.prompt_max_chunk_chars_complex).
- Dense query prefix: asymmetric query embeddings prepend Settings.dense_query_prefix on dense query vectors only; document chunks stay raw; HashEmbedder remains plain text.
- Hit diversification: retrieve_max_chunks_per_page=1 and retrieve_max_chunks_per_doc=3 with 3-phase backfill prevent near-duplicate consecutive chunks from monopolizing prompt context slots.
- Citation normalization: normalize_citation_line peels bracketed index markers ([1], [1]:) from model citation lines.
- Tokenizer: vLLM `/tokenize` is at the server **origin** (strip `/v1` from `LLM_BASE_URL`; LiteLLM may not expose it). First failure logs one warning and pins the estimator fallback — never a silent per-call fallback. Budgeting plans with the in-process estimator and verifies the packed prompt **once** via `/tokenize` `messages`; never per-chunk tokenize RPCs.
- Run manifests: unreachable Qdrant records `qdrant_version="unknown"` — never the pinned server version.
- After a non-obvious bug, add a regression test **and** a one-line note here if it is a standing rule.

## When you change this file

Same PR as the work that taught the rule. Keep it short. Delete advice that is no longer true.
Do not turn this file into a changelog of merged PRs — record the invariant, not the round number.
