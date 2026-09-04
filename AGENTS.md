# AGENTS.md

Working agreement for coding agents on this repository.
Revise this file in the same PR that learns a new rule.
If a review comment conflicts with this file, follow this file and note the conflict on the PR.

## Start here (new agents read this first)

1. Read, in order: the issue → `ROADMAP.md` → this file → `docs/architecture.md` → `docs/adr/0001-baseline-decisions.md`.
2. Place the change using the Module/Owns layer table below, then read only that module's file. Do not wade through unrelated layers.
3. Look up the change class and run exactly its rungs — no more, no less:

| Change class | Required rungs (`docs/live-stack.md`) |
|---|---|
| Docs only | Existence-check cited paths; no GPU |
| Tests / make / CI only | `make check` |
| Agent HTTP / validation | `make check` + live probes (rung 6) |
| Ingest / chunk / classify | `make check` + gate-l1 + fresh paraphrase |
| Retrieve / embed / RRF / rerank / screen | Full ladder + A/B numbers in the PR body |
| Defaults, UUID, `chunk_type`, production constants | Split the PR; the split-off pays eval + A/B |

4. Lethal mistakes (any one of these fails review outright, however green the gates):
   no new `chunk_type` vocabulary; no UUID5 point-id change; no default flips
   (`RERANK_ENABLED`, `llm_stream`, any `Settings` default) inside a feature PR;
   no committed PDFs/snapshots/manuals; never delete or weaken tests to go green;
   never hash-eval against a vLLM-dim collection (a mismatch skip is not a pass);
   no baseline rewrite except via `make eval-baseline` in a dedicated PR;
   no quoting vendor-manual text into the tree.
5. Prove the environment before writing code: bring up the stack and run the ladder rungs your class requires. A red rung stops all work.
6. Testing rules live in `docs/testing.md`. This file states what to run; that file states how to write it.

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

- `make check` is clean locally (ruff, mypy, unit suite).
- The ladder rungs your change class requires (Start-here table, `docs/live-stack.md`) are green in full. Skipping a required rung — or inventing its numbers — fails review outright.
- Branched fresh from `origin/main`, single concern, not an already-merged branch.
- Every new behavior has a test that fires the **claimed path**, not only the exception/fallback path. See `docs/testing.md`.
- Every new handler, branch, and error shape has a reachable test. A handler no test can fire is dead code.
- Input-handling / parsers were probed adversarially: empty, multi-digit, wrapped (`> `, backticks, quotes, parens, `**bold**`, `[links](url)`, `<angle>`), inline/non-anchored, top-placed, missing blank lines, case folding.
- `git status` shows no untracked toolchain artifacts (`node_modules/`, lockfiles from experiments, venvs, caches). Experiments run outside the repo tree.
- Every claim in the PR body is true of the code in **this** push. Grep for the counterexample before writing “defaults unchanged”, “no runtime change”, “CLI overrides work”, or “prevents env leaks”.
- Every change to a default, constant, timeout, retry count, limit, or chunk size is called out in the PR body.
- Retrieval changes (embedder, chunking, RRF, filters, query shape) include `make eval` vs the mode-keyed baseline (`evals/baseline.json` hash, `evals/baseline-vllm.json` live) in the PR body (identifier recall@1 strict 1.0; overall recall@1 ×0.9, recall@5 ×0.95, MRR ×0.95; 0 query errors). Tooling PRs that also touch those paths still owe the numbers. Re-baseline is a dedicated PR (`make eval-baseline`).
- Local vLLM / Makefile / script work must not change production chunking, retrieval constants, or ingest-worker semantics in the same PR. If they must, that is two concerns: split, or pay the eval rule above.

## Testing (checklist; rules live in `docs/testing.md`)

- `pytest` is the gate; tests generate original PDFs at runtime, no binary fixtures; CI fails on committed `.pdf` / `.pdx` / `.idx`.
- Unit tests are hermetic: no live Qdrant / vLLM / internet; fake the client; patch `httpx2`; never mutate module-global state; pin contracts, not internals.
- Tests lock the claimed path: force the success path with mocks; never assert what the fallback would also produce. Adversarial matrices and the parser/citation/fence case lists are in `docs/testing.md` — apply the ones your change touches.
- Tier commands and their green conditions are the ladder in `docs/live-stack.md`; tier mechanics (golden/holdout discipline, sim/load/bench/harness invariants) are in `docs/testing.md`.

## CLI, Makefile, and local vLLM

User-supplied `--embed-model`, `--model`, `--embed-url`, `--vllm-url`, `--embed-mode`, `--dense-dim`, and matching Makefile/`ENV` values must be applied or fail nonzero with a message. Never silently keep `load_settings()` values after a `/models` probe. Ambiguous auto-detect (`len(avail) != 1` and no match) fails closed.

- Quote every shell expansion. Never stash JSON flags in an unquoted `${MODEL_ARGS}` string.
- Never `export` a mode variable globally in the Makefile: make exports reach every recipe, and the airgap scripts refuse `EMBED_MODE=hash` fail-closed (a global export broke the CI airgap-dryrun). Scope it: `eval eval-baseline …: export EMBED_MODE := $(EMBED_MODE)` (immediate expansion — a recursive `=` self-references under a target-specific directive).
- `case` globs are case-sensitive: `*embed*` does not match `Embedding`. Match `*embed*` and `*Embed*` (or use a case-insensitive test).
- Pin vLLM image tags that actually implement the flags you pass (`gemma4` parsers, `--runner pooling --convert embed`). `:latest` and stale minors are production bugs. vLLM v0.28.0 removed `--task`.
- Local 8GB launch flags are resolved, not hardcoded: `scripts/run_local_vllm.sh` evals `mainframe_rag.serve resolve --profile LOCAL_RT_8GB --role <reasoning|embed>` (reasoning `GPU_MEM=0.64`; embed `GPU_MEM=0.33` with `--runner pooling --convert embed --enforce-eager`; both `MAX_LEN=4096`) and fails closed when resolve does. `make local-vllm*` passes `ROLE` + venv `BUDGET_PYTHON` per-recipe and carries `| .venv`; explicit `GPU_MEM=`/`MAX_LEN=`/`SEQS=`/`ROLE=` always win. `--enable-prefix-caching` resolves from Budget `prefix_cache` (on for LOCAL reasoning — vLLM v0.28 already caches by default, the pin guards flips; embed off, unmeasured). Solo reasoning `GPU_MEM=0.85` is an explicit override. A 2048 embed window was rejected by the #99 tokenizer sweep (worst case 2043 tokens, ~2.0 chars/token on syntax-dense text). The embed budget is pinned hermetically by `tests/test_embed_budget.py` — re-run the sweep before changing chunk constants or the embed-text header.
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
| `ibm_pdf` (parse) | Open, metadata, optional IBM signals, generic fallbacks; extract-time text sanitization (`sanitize_page_text`: CSI/C0/bidi/zero-width dropped, printable bytes identical) |
| `chrome` | Repeated headers/footers; never threshold=1; skip docs under 8 pages |
| `chunk` | Outline → else whole doc; UUID5 ids; heading path; `SECTION_MAX_CHARS = 3500`; code regions (JCL/REXX/console, detected in `chunk.py`) split at statement boundaries only — per-statement atomic items, overlap backs off to whole statements, one oversize statement emits whole |
| `classify` | `message` / `syntax` / `table` / `narrative` |
| `embed` | Dense from internal vLLM; sparse local (no Cloud inference) |
| `qdrant_io` | Collection + payload indexes **before** load; dim fail-fast |
| `retrieve` | Filters in prefetch; hybrid dense+BM25; cross-encoder rerank dispatch (`rerank.py`, default off); query-class screen (`screen.py`: trap checked before identifiers, sibling must_nots stay answerable; trap queries bypass rerank in both search twins, RRF order stands) |
| `agent` | HTTP API; citation validation; request-size guardrails (`query_max_chars` 422s closed, `splunk_context_max_chars` truncates with suffix) |

Standing #20 rules: embed / Qdrant points / LLM are `Protocol`s in `ports.py`; upserts are batched (`Settings.batch_size`); payload indexes exist before load; every outbound call has a Settings timeout.

Ingest workers (`_parse_one`) trap exceptions and return plain `InventoryRecord(status="error")`. Unpicklable `httpx2.HTTPStatusError` objects crash `ProcessPoolExecutor` across spawn IPC.

## GitHub vs GitLab CI (read only when touching CI files)

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

## Image refs (connected factory — read only for image/pack work)

- Connected `main` is the only image factory. The air-gap never builds Containerfiles until an issue says so.
- One string, everywhere: `ghcr.io/<owner-lowercase>/qdrant-pdf-rag-{ingest,agent}:<full-git-sha>`. Full SHA is `git rev-parse HEAD` / `$GITHUB_SHA`, **never** `${GITHUB_SHA::7}`. That exact string is used for `docker tag`, `docker push`, kustomize sed, `airgap-pack`, and `airgap-load`.
- Makefile local names are `mainframe-rag/{ingest,agent}` — retag to the GHCR ref **before** push.
- Third-party pins live in `images.txt` (Qdrant unprivileged, UBI). A `requirements.lock.txt` bump requires `make wheelhouse bm25-weights` on CPython 3.14 and a connected image rebuild. Dedicated PR, not drive-by. `qdrant-client` pin tracks the 1.19 server/chart.
- Do not add unpublished extras (`types-httpx2`). `httpx2` ships types. A dependency no module imports is a phantom (`test_no_litellm_anywhere`). Audit `pyproject.toml` before adding.
- Images: UBI, non-root, `--no-index` from `/wheelhouse`. Bake BM25 weights (`make bm25-weights`).

## Overlays (never mix CI and prod — read only for deploy work)

- **CI (lab, connected only):** `overlays/ci/values.yaml` + `deploy/kustomize/overlays/ci` — 1 replica / 1Gi, `EMBED_MODE=hash`, synthetic PDFs generated in-cluster, GHCR pulls. Never copy the CI ingest Job into prod.
- **Prod (air-gap):** `overlays/openshift/values.yaml` + `deploy/kustomize/overlays/openshift` (agent) + `overlays/openshift-ingest` (one-shot Job). 3 replicas / 500Gi / unprivileged / RWO. No `EMBED_MODE` key. Corpus is a caller-supplied PVC. Do not shrink prod values to CI sizes.
- Placeholders in git (`__TOKEN__`, `ghcr.io/OWNER`). Render must fail closed on leftover `__[A-Z][A-Z0-9_]*__`. No real registries, namespaces, or URLs in git. Helm values stay placeholders (`INTERNAL_REGISTRY` / `REGISTRY_INTERNAL`, `PULL_SECRET`, `STORAGE_CLASS`).
- When `PULL_SECRET` is set, it must reach Helm Qdrant **and** agent/ingest pods. Confirm rendered YAML indent is valid.
- Helm `--set image.tag` is `v1.19.0` **without** `-unprivileged`; the chart appends that suffix when `useUnprivilegedImage=true`. `load.sh` still pushes `:v1.19.0-unprivileged`.
- Kubernetes Jobs are immutable: delete before re-apply (`make airgap-ingest`).

## Air-gap path (issue #15 — read only for air-gap work)

- The air-gap never builds images. `make airgap-pack` runs on a connected clone of public `main` at the SHA whose GHCR tags exist. `IMAGE_SHA` is the full git SHA and must equal both `HEAD` and the GHCR tag. `make airgap-load` / `airgap-deploy` run inside the gap against `airgap.env`. Scripts are POSIX sh under `scripts/airgap/` and fail closed.
- Happy path: `airgap-pack` → sneakernet `*.tar` + `*.tar.sha256` → unpack → `git clone repo.bundle` → `airgap-load` → `airgap-deploy`. `load.sh` does **not** clone. Verify tarball digest **before** unpack; member `SHA256SUMS` **after**. Do not invent a third path. `oc-mirror` is optional.
- Scripts refuse `EMBED_MODE=hash`, NFS-looking `STORAGE_CLASS`, missing `VLLM_BASE_URL`/`EMBED_MODEL`/`DENSE_DIM`, and SHA-tag mismatches.
- GitLab CI still does **not** deploy or pull GHCR: clone-and-pytest only. No PDFs, kubeconfigs, tokens, or internal hostnames in git or in the tarball.
- `airgap-rehearsal` (e2e.yml, main/dispatch) uses three CI-only stand-ins, never in git prod values: GHCR as `INTERNAL_REGISTRY`, `scripts/mock_vllm.py` as vLLM, shrunk size knobs for lab quota. `PULL_SECRET` unset renders `imagePullSecrets=null`. `airgap-dryrun` (every PR, `AIRGAP_DRYRUN=1`) proves renders, placeholder fail-close, SHA rules, both PULL_SECRET branches, and size knobs without a cluster.

## Qdrant skills (vendored — read before touching Qdrant)

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
- Rerank ships default-off (`rerank_enabled=False`): fused top-`rerank_candidates` (50) go through the cross-encoder only when explicitly enabled. `search()` and `async_search()` must return identical hits for identical fakes — the drift-guard test is the contract (query.py carries a near-verbatim twin by design).
- Async handlers never run sync I/O on the event loop: the embed (`dense_query`/`sparse`) and cross-encoder (`rerank_candidates`) legs execute via `asyncio.to_thread`; the pooled sync retrieval-leg client is built and closed in lifespan.
- Runtime paths never sniff monkeypatched module attributes to pick clients (no `__name__`/`<lambda>` checks). Construct the production class explicitly; test doubles ride the `isawaitable` shims.
- SSE contract on `/v1/answer?stream=true`: token deltas → exactly one terminal `final` whose schema is identical on every path (the empty-hits path carries `finish_reason`/`ttft_ms`/`usage` too); a mid-stream failure emits `event: error` and ends WITHOUT `final` — no final = failed.
- SSE streams must end with `[DONE]`: a stream that ends without it is `TruncatedStreamError`, never `finish_reason` stop. `chat_stream` raises (app emits `event: error` + `answer_alert` `stream_truncated`); `achat` / `_chat_sync` fall back to the non-streaming POST and discard the prefix.
- Prompt user-message blocks are named (`context`/`question`/`excerpt`/`tail`) and ordered by policy (`Settings.prompt_order`, default `retrieval` = historical order, byte-identical). Policies reorder and frame excerpts (delimiters carry no attributes); dropping the tail or duplicating excerpts fails closed in `order_prompt_blocks`; the static instruction block is identical across queries by construction (prefix-cache premise, pinned).
- Citation normalization: normalize_citation_line peels bracketed index markers ([1], [1]:) from model citation lines.
- Tokenizer: vLLM `/tokenize` is at the server **origin** (strip `/v1` from `LLM_BASE_URL`; LiteLLM may not expose it). First failure logs one warning and pins the estimator fallback — never a silent per-call fallback. Budgeting plans with the in-process estimator and verifies the packed prompt **once** via `/tokenize` `messages`; never per-chunk tokenize RPCs.
- Run manifests: unreachable Qdrant records `qdrant_version="unknown"` — never the pinned server version.
- After a non-obvious bug, add a regression test **and** a one-line note here if it is a standing rule.

## When you change this file

Same PR as the work that taught the rule. Keep it short. Delete advice that is no longer true.
Do not turn this file into a changelog of merged PRs — record the invariant, not the round number.
