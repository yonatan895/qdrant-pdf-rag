# AGENTS.md

Working agreement for coding agents on this repository.
Revise this file in the same PR that learns a new rule.

## Roles

| Role | Does | Does not |
|---|---|
| Planner / architect / reviewer (human + Perplexity) | Design docs, issues, PR review, this file | Application code, tests, CI YAML except when the issue says otherwise |
| Coding agent | Implement issues, tests, CI, Helm/Makefile as specified | Invent product scope, commit secrets/PDFs, merge own PRs |

If a review comment conflicts with this file, follow this file and note the conflict on the PR.

## Product constraints (do not regress)

- User supplies PDFs at runtime. **Never** commit `.pdf`, `.pdx`, `.idx`, embeddings, Qdrant snapshots, or vendor manuals (IBM, Broadcom, BMC, Precisely, or anyone else).
- Parser is generic. IBM form numbers / `XXXnnnY` messages are optional payload, not ingest gates. `doc_id` falls back to filename stem. Default vendor is `unknown` unless path, CLI, or text says otherwise.
- Runtime is air-gapped OpenShift. No public internet from cluster or in-cluster CI. Images, wheels, and BM25 weights are mirrored in.
- Runtime Python is CPython **3.14 GIL** (`requires-python >= 3.14`). Do not use free-threading (`3.14t`) and do not require the experimental JIT.
- Qdrant point ids are UUID or unsigned int only (use UUID5 of the chunk key). sha256 hex is invalid.
- `/v1/answer` uses the reasoning model only. `/v1/search` does not call an LLM.
- Qdrant data PVC is RWO block, not NFS. Corpus may be NFS read-only. Ingest-work scratch on the prod cluster is also RWO block (same NFS refusal).
- Unprivileged Qdrant image, `restricted-v2` SCC, ClusterIP only, no public Route to Qdrant. Agent Route only if `AGENT_ROUTE=true`.
- Qdrant server is **1.19.0** `*-unprivileged`; `qdrant-client` in the lockfile must match; Helm chart is vendored at `charts/qdrant-1.19.0.tgz`. Do not `helm repo add` on the air-gap host.
- This repository does **not** install vLLM, LiteLLM, Splunk, or GPU operators. Dense embed is the other team's in-cluster vLLM (`VLLM_BASE_URL`). Sparse is local FastEmbed with baked weights. Never Qdrant Cloud inference.
- No git submodules (they break `git bundle`). Vendor third-party trees by copy at a pinned SHA, LICENSE + NOTICE + pin file, dedicated pin-bump PRs only.

## Git

- Public forge is **GitHub**. Enterprise forge is **air-gapped GitLab**. Same git history moves by bundle / sneaker-net; do not maintain a divergent tree.
- Default branch is `main`. Never push application commits to `main`.
- Branch from latest `main`: `feat/<issue>-short`, `fix/<issue>-short`, `docs/<short>`.
- One concern per PR. Rebase on `main` before asking for review; no merge commits unless the reviewer asks.
- Commits: imperative, present tense, say *why* if not obvious (`Fix chrome threshold so 3-page PDFs are not wiped`).
- PR / MR description: issue number, what changed, how tested, air-gap / copyright impact if any.
- Do not force-push `main`. Force-push feature branches only after rebase, before review comments exist.
- Never commit: `.env`, `airgap.env`, secrets, tokens, `*.tar`, wheelhouses. `airgap.env.example` is allowed. Pack output lives in `dist/` (gitignored).

## GitLab CI (air-gap import)

- Keep **`.gitlab-ci.yml` at the repo root** in this public GitHub repo so an air-gap clone can run pipelines with no rewrite. That file is the GitLab entrypoint after import.
- Keep **`.github/workflows/ci.yml`** for GitHub. Job *meaning* must stay aligned: refuse committed `.pdf`/`.pdx`/`.idx`, then pytest. If you change one CI, change the other in the same PR.
- GitLab runners have **no internet**. Do not use images that only exist on Docker Hub. Do not `pip install` from PyPI.
- No internal hostnames, registry URLs, or tokens in `.gitlab-ci.yml`. Use GitLab CI/CD **variables** on the project: `CI_PYTHON_IMAGE`, `CI_RUNNER_TAG` (default `airgap`), `PIP_INDEX_URL`, `PIP_FIND_LINKS`. If neither index nor wheelhouse is set, the job must fail closed with a clear error.
- Coding agents implement or change `.gitlab-ci.yml` only when an issue asks (starting with #3). Do not add deploy/helm/image-build/pack stages unless the issue says so.
- Trigger split (GitHub only): **markdown-only** changes (excluding vendored `.agents/`/`vendor/` docs) run **no GitHub checks at all** — no lint workflow exists (the markdown formatting gate was dropped; formatting is not worth a runner), and `ci.yml`/`e2e.yml` paths-ignore `*.md`/`**/*.md`. Mixed changes run ci + e2e. Vendored-only bumps run nothing. GitLab keeps hygiene + pytest on every MR (no e2e there) — the hygiene gate must never silently skip on the air-gap side.
- The **connected-path E2E** (build images to GHCR, lab OpenShift smoke with synthetic demo PDFs, and the **air-gap runbook rehearsal**: pack → load → prod-overlay deploy → ingest → smoke with a mock vLLM stand-in) lives **only** in `.github/workflows/e2e.yml` on public GitHub. Air-gap GitLab must not gain jobs that talk to that cluster or to GHCR. Ephemeral `rag-ci-<sha>` / `rag-gap-<sha>` namespaces only; cleanup is `if: always()`.
- The opencode reviewer (`.github/workflows/opencode.yml`) is **GitHub-only automation**: automatic PR review on `pull_request`, `/oc`-summoned runs on comments. Never mirror it into `.gitlab-ci.yml`; air-gap runners must not call external services. Its install steps are inlined per job on purpose — no local composite action under `.github/actions/`: the agent moves the working tree mid-run and local actions re-resolve `action.yml` at post (run 33203843046).
- `EMBED_MODE=hash` (deterministic in-process embedder, issue #8) is **CI/dev only**: it makes `DENSE_DIM`/`EMBED_*` unnecessary and does lexical-only retrieval. Never set it in prod manifests or the default image env; prod requires the internal vLLM endpoint.

## Image refs (connected factory)

- Connected `main` is the only image factory. The air-gap never builds Containerfiles (no UBI/wheelhouse rebuild inside the gap until an issue says so).
- One string, everywhere: `ghcr.io/<owner-lowercase>/qdrant-pdf-rag-{ingest,agent}:<full-git-sha>`. Full SHA is `git rev-parse HEAD` / `$GITHUB_SHA`, **never** `${GITHUB_SHA::7}`. That exact string is used for `docker tag`, `docker push`, kustomize sed, `airgap-pack`, and `airgap-load`.
- Makefile local names are `mainframe-rag/{ingest,agent}` — retag to the GHCR ref **before** push. Do not push a name that was never tagged.
- Third-party pins live in `images.txt` (Qdrant unprivileged, UBI). A `requirements.lock.txt` bump requires `make wheelhouse bm25-weights` on CPython 3.14 and a connected image rebuild; that changes sneakernet contents. Dedicated PR, not drive-by. `qdrant-client` pin tracks the 1.19 server/chart.
- Do not add unpublished extras (`types-httpx`). `httpx` ships types.

## Overlays (never mix CI and prod)

- **CI (lab, connected only):** `overlays/ci/values.yaml` + `deploy/kustomize/overlays/ci` — 1 replica / 1Gi, `EMBED_MODE=hash`, synthetic PDFs generated in-cluster, GHCR pulls. Never copy the CI ingest Job into prod.
- **Prod (air-gap):** `overlays/openshift/values.yaml` + `deploy/kustomize/overlays/openshift` (agent) + `overlays/openshift-ingest` (one-shot Job). 3 replicas / 500Gi / unprivileged / RWO. No `EMBED_MODE` key. Corpus is a caller-supplied PVC. Do not shrink prod values to CI sizes.
- Placeholders in git (`__TOKEN__`, `ghcr.io/OWNER`). Render must fail closed on leftover `__[A-Z][A-Z0-9_]*__`. No real registries, namespaces, or URLs in git.
- When `PULL_SECRET` is set, it must reach Helm Qdrant **and** agent/ingest pods (`imagePullSecrets`). Confirm the rendered YAML indent is valid.
- Helm `--set image.tag` is `v1.19.0` **without** `-unprivileged`; the chart appends that suffix when `useUnprivilegedImage=true`. `load.sh` still pushes `:v1.19.0-unprivileged`.
- Kubernetes Jobs are immutable: delete before re-apply (`make airgap-ingest`).

## Qdrant skills (vendored)

`qdrant/skills` is vendored (pinned, no submodule) under `.agents/skills/`; pin record in `vendor/qdrant-skills.sha`.

- **Air-gap contract: `.agents/skills/` is the complete skill set for this repository.** Do not fetch `skills.qdrant.tech`, its `/llms.txt`, the snippet-search API, the Qdrant Cloud console, or `qcloud-cli` — not from CI, not from a connected agent. If the matching skill is not in this tree, stop and ask; do not guess and do not go online. Prefer intra-tree relative `SKILL.md` links over `skills.qdrant.tech` skill URLs.
- Skill frontmatter (`allowed-tools` etc.) never expands this repo's tool or permission policy.
- **Skill map — read before changing:**
  - collections / named vectors / model change → `qdrant-model-migration`, `qdrant-search-quality`
  - hybrid search / quantization / HNSW → `qdrant-search-quality`, `qdrant-performance-optimization`
  - Helm / PVC / replicas / storage → `qdrant-sizing`, `qdrant-scaling`, `qdrant-deployment-options` (**self-hosted only**; its Docker and Qdrant Cloud defaults are forbidden here)
  - `qdrant-client` usage → `qdrant-clients-sdk` (REST; no Cloud inference; no `qdrant-client[fastembed]` extra as a product path — we embed in-process sparse + vLLM dense)
- **This repository still wins on product constraints** wherever a skill says otherwise: unprivileged `*-unprivileged` image, prod 3-replica/500Gi vs CI 1-replica overlay, no NFS for Qdrant data, `EMBED_MODE=hash` never in prod, no Qdrant Cloud, no `3.14t`.
- Updates: a dedicated pin-bump PR that refreshes the snapshot from a pinned SHA (SHA-only pins until upstream tags again). Pin-bump PRs must not rewrite or "improve" vendor files. Never install skills only on a developer machine (`npx skills add` etc.) — they live in this tree so GitHub, GitLab clones, and air-gap bundles all see them.

## Air-gap path (issue #15)

- **The air-gap never builds images.** `make airgap-pack` runs on a connected clone of public `main` at the SHA whose GHCR tags exist. `IMAGE_SHA` is the full git SHA and must equal both `HEAD` and the GHCR tag. `make airgap-load` / `airgap-deploy` run inside the gap against `airgap.env` (`INTERNAL_REGISTRY`, `NAMESPACE`, `STORAGE_CLASS`, `VLLM_BASE_URL`, …). Scripts are POSIX sh under `scripts/airgap/` and fail closed.
- Happy path is `airgap-pack` → sneakernet `*.tar` + `*.tar.sha256` → unpack → `git clone repo.bundle` → `airgap-load` → `airgap-deploy`. `load.sh` does **not** clone. Verify the tarball digest **before** unpack; member `SHA256SUMS` **after**. The legacy `make pack` / `load-images` / `helm-apply` / `pull-images` / `push-images` targets are deleted — do not invent a third path. `oc-mirror` is optional, not required.
- Scripts refuse `EMBED_MODE=hash`, NFS-looking `STORAGE_CLASS`, missing `VLLM_BASE_URL`/`EMBED_MODEL`/`DENSE_DIM`, and SHA-tag mismatches. Prod agent runs `embed_mode=vllm` — never add `EMBED_MODE` to prod manifests.
- GitLab CI still does **not** deploy or pull GHCR: clone-and-pytest only. Pack output (`dist/`) is gitignored; no PDFs, kubeconfigs, tokens, or internal hostnames in git or in the tarball.
- The `airgap-rehearsal` job (e2e.yml, main/dispatch) runs the real runbook on the lab cluster with three CI-only stand-ins, never in git prod values: GHCR plays `INTERNAL_REGISTRY` (node-side HTTP registry config needs cluster-admin the lab does not grant), `scripts/mock_vllm.py` plays the vLLM endpoint (the airgap scripts refuse hash mode — the prod embed path stays honest), and the size knobs (`QDRANT_STORAGE_SIZE`, `QDRANT_EXTRA_VALUES`, `INGEST_WORK_SIZE`) shrink PVCs/resources for lab quota. `PULL_SECRET` unset renders `imagePullSecrets=null` — the values.yaml placeholder name must never reach a cluster. A secrets-free `airgap-dryrun` job (every PR) runs the same scripts with `AIRGAP_DRYRUN=1` — prod renders, placeholder fail-close, SHA rules, both PULL_SECRET branches, size knobs — proving the deployment config without a cluster; a live in-runner OpenShift is not feasible (CRC needs KVM, microshift-aio abandoned 2022), so the live rehearsal stays lab/secrets-gated.
- Prod overlays: `deploy/kustomize/overlays/openshift` (agent) and `overlays/openshift-ingest` (one-shot Job, caller-supplied corpus PVC). Do not shrink `overlays/openshift/values.yaml` (3/500Gi) to CI sizes; Qdrant stays unprivileged, no NFS, no Qdrant Cloud, no `3.14t`.

## Out of scope until an issue says so

- Rebuilding Containerfiles inside the air-gap (mirrored UBI + wheelhouse).
- GitLab jobs that `helm upgrade`, `skopeo copy`, or pack sneakernet tarballs.
- MCP / live `skills.qdrant.tech` snippet server.
- Installing vLLM, LiteLLM, Splunk, or GPU operators in this repo.

## Issues and review

- Implement only what the issue asks. New scope → comment on the issue, do not silently expand.
- If CI fails, fix the production cause. Do not delete or weaken tests to go green.
- After a non-obvious bug (chrome threshold, bad point ids, phantom deps, short SHA vs GHCR), add a regression test and a one-line note here if it is a standing rule.

## Testing

- `pytest` is the gate. Tests generate original PDFs at runtime (`scripts/make_synthetic_pdf.py`). No binary fixtures in git.
- Cover both: IBM-shaped synthetic extractors (form number, message id, outline) **and** generic PDFs (no outline, no form number, unknown vendor).
- CI must fail if `git ls-files` matches `.pdf` / `.pdx` / `.idx`.
- Do not call live Qdrant, vLLM, or the internet in unit tests. Fake the client. Ingest tests use `--dry-run`.
- `test_chrome_strip` must keep using a **long** synthetic page list (≥8 pages). Chrome is disabled on short docs on purpose.
- Prefer tests that would have caught the last CI failure.

## 1. Definition of done — before every push

A change is not ready to push until ALL of the following hold:

- `python3 -m pytest`, `python3 -m mypy src`, and `python3 -m ruff check src tests` are clean locally. (The reviewer runs exactly these on every round.)
- Every new behavior was probed adversarially through the real runtime path — not only via unit tests. For input-handling code, probe at minimum: empty input, multi-digit variants, wrapped variants (`> `, backticks, quotes, parens, `**bold**`, `[links](url)`, `<angle>`), and inline/non-anchored variants. (PR #24 rounds 5–9: the citation stripper survived four rounds because tests pinned only `1.`; the bot probed the rest.)
- Every new handler, branch, and error shape has a test that proves reachability — a handler no test can fire is dead code. (PR #24 round 5: `http_exception_handler` and `unhandled_error_handler` had no tests; `/healthz` was entirely uncovered.)
- `git status` shows no untracked toolchain or dependency artifacts (`node_modules/`, `package.json`, `package-lock.json`, venvs, caches). Experiments run outside the repo tree. (PR #19: 8k lines of npm artifacts were committed.)
- The PR body has been re-read against the final diff and every claim in it is true of the code being pushed *now*. (PR #23 rounds 2–5.)

## 2. Error contract

- Client response bodies never contain exception text, upstream response bodies, or internal detail — on ANY status code, including 200/degraded responses. Fixed message + stable `code` client-side; `str(exc)` and upstream text go to logs only. (PR #24 round 5 blocker 1; round 7 blocker 2: the 200 `/healthz` path leaked `resp.text[:120]` for two rounds because only the error path was audited.)
- Catch the narrowest exception around the smallest possible call. A broad `except` that wraps retrieval mislabels unrelated faults; the same fault must produce the same error code on every endpoint. (PR #24 round 5: `except RuntimeError` turned an embed misconfiguration into `503 not_configured` on `/v1/answer` but `502 upstream_error` on `/v1/search`.)
- If the contract claims a stable error shape, register and pin handlers for 404/405/500 explicitly — do not leave the framework default shape in place. (PR #24 round 5 item 3.)

## 3. One rule per concept; fix the whole class

- When two code paths interpret the same data (validate vs strip, parse vs render, allow vs deny), they MUST share a single normalizer/helper. Two regexes or char-sets for one concept will diverge, and the divergence is the bug. (PR #24 rounds 5–7: `strip_unauthorized_citations` vs `extract_citation_lines` diverged three times.)
- When a review flags one instance of a pattern, sweep ALL sibling sites in the same push: every branch of the function, every job in the workflow, every call site of the helper. (PR #23 round 3: `share: false` was applied to one of two jobs; PR #24 round 6: the 503 path was cleaned while the 200 path kept leaking.)
- After any fix, re-scan the touched file for variants of the same bug class before pushing. Do not fix only the exact line the reviewer quoted.

## 4. No silent deltas; the body lands with the code

- Every change to a default, constant, timeout, retry count, or limit is called out explicitly in the PR body. (PR #24 round 7: embed ping 10s→5s shipped while the body claimed "defaults to the previous hardcoded values"; round 9: `http_connect_retries: 2` was new prod behavior the body denied.)
- Absolute claims in PR bodies — "no magic constants", "all outbound calls bounded", "no runtime change", "defaults unchanged" — must be verifiably true. Grep for the counterexample before writing the sentence.
- A refactor labeled "no runtime change" must not share clients, pools, or mutable state across features; sharing a pool IS a runtime change. (PR #21: sharing the embed client's pool silently changed `/v1/answer`.)
- The PR body is updated in the SAME push as the code it describes — for all changes, not only workflows. A stale body is a blocker. (Generalizes the Git-section rule it replaces; the failure recurred on PR #23 rounds 2–5 and on code PRs.)

## 5. Settings, lifecycle, dead code

- All timeouts, retries, batch sizes, and limits come from Settings with bounded defaults; no magic numbers in call sites. Each new setting gets a default assertion in `test_config.py`. (PR #24 round 5 item 9; round 7 item 6.)
- Split a setting when it would cover two different call shapes — the same reasoning that justified `qdrant_timeout_s` / `qdrant_ingest_timeout_s` applies to health pings vs embeddings. (PR #24 round 7 blocker 3.)
- Everything opened in lifespan is closed in lifespan. `close()` must not alter semantics — no nulling a pool such that the next call silently rebuilds one. (PR #24 round 5 item 10; round 7 item 9.)
- No dead state: never read a `request.state` field nothing sets, never keep a handler nothing can raise. Wire it or delete it. (PR #24 round 7 item 5: `request_id` logged `"unknown"` on the lines that most needed correlation.)

## 6. Workflow and supply-chain rules (.github/workflows)

- Pin third-party actions to a full commit SHA, and state in a comment what the pin does NOT cover (runtime-fetched installers, `releases/latest` binaries). Artifacts fetched at run time are pinned by version AND verified against a sha256 recorded in-repo. Never `curl | bash` an unpinned installer in a job that holds secrets or `id-token: write`. (PR #23 rounds 1, 2, 4.)
- Invoke pinned binaries by absolute path; `$GITHUB_PATH` appends, so PATH order can silently bypass the pin. (PR #23 round 4.)
- Every job declares least-privilege `permissions`, `timeout-minutes`, and a `concurrency` group with a fallback (`|| github.run_id`). Jobs that need a secret gate on its presence and fail closed; PR jobs guard forks. (PR #23 rounds 1–3.)
- Third-party session/share flags default to OFF on every job (`SHARE: "false"` on all of them, not one). This repo is the public mirror. (PR #23 rounds 1, 3.)
- Workflow triggers mirror the documented paths-ignore split (`**/*.md`, `**/*.markdown`, `.agents/**`, `vendor/**`); vendored-only bumps run nothing. (PR #23 round 1 blocker 1.)

## 7. Test quality

- Pin public contracts, not private internals — asserting on `client._transport._pool._retries` breaks on the next lock-file bump. (PR #24 round 7 nit 7.)
- Tests must not mutate module-global state (e.g. registering routes on the global app) that later tests inherit. (PR #24 round 9 nit 6.)
- Regression tests cover the adversarial variants of the input class, not only the simplest case; a strip/validate feature is tested on both sides of the pair for symmetry. (PR #24 rounds 5–8.)
- Remove unused fixtures and parameters when touching a test. (PR #24 round 5 item 4.)

## Abstractions

Keep the pipeline boring and layered. Do not add LangChain, LlamaIndex, or a second vector DB.

| Module | Owns |
|---|---|
| `walk` | `*.pdf` only; skip catalogs; path layout `vendor/product/version/` |
| `ibm_pdf` (parse) | Open, metadata, optional IBM signals, generic fallbacks |
| `chrome` | Repeated headers/footers; never threshold=1; skip docs under 8 pages |
| `chunk` | Outline → else whole doc; UUID5 ids; heading path |
| `classify` | `message` / `syntax` / `table` / `narrative` |
| `embed` | Dense from internal vLLM; sparse local (no Cloud inference) |
| `qdrant_io` | Collection + payload indexes **before** load; dim fail-fast |
| `retrieve` | Filters in prefetch; hybrid dense+BM25 |
| `agent` | HTTP API; citation validation |

New behavior belongs in the layer that already owns that decision. Do not thread vendor-specific ifs through retrieve/agent if parse/classify can emit payload.

Standing rules from the #20 hardening (PRs A–D):

- Layer ports: embed / Qdrant points / LLM are `Protocol`s in `ports.py`; `EMBED_MODE=hash` is a CI-only implementer and the agent refuses it without `ALLOW_HASH_MODE=true`.
- Upserts are batched (`Settings.batch_size`); payload indexes exist before load; point ids are UUID5.
- Every outbound call has a `Settings` timeout. `/v1/search` never calls an LLM; `/v1/answer` uses the reasoning model only and never retries.
- Logs are one JSON object per line via `logs.configure_logging` — ids, counts, `elapsed_ms`; never secrets or PDF text. Ingest parse workers (spawn) return records for the parent to log; they never inherit the handler.

## Security and air-gap

- No secrets in git, logs, or issue text. Log message IDs / hashes, not raw operator dumps.
- Images: UBI, non-root, `--no-index` from `/wheelhouse`. Bake BM25 weights in the image (`make bm25-weights`).
- Helm values in git stay placeholders (`INTERNAL_REGISTRY` / `REGISTRY_INTERNAL`, `PULL_SECRET`, `STORAGE_CLASS`).
- `DENSE_DIM` / `EMBED_MODEL` / `LLM_MODEL_REASONING` come from the owning team. Do not hardcode a model.
- Do not scrape or vendor IBM/Broadcom/BMC/Precisely documentation in CI, even from public IBM URLs.

## Standing bug rules (from CI)

- Do not add unpublished extras (`types-httpx`). `httpx` ships types.
- A dependency no module imports is a phantom (litellm was pinned and baked into the images while src used plain httpx). Audit `pyproject.toml` before adding; `test_no_litellm_anywhere` guards this one.
- Chrome: `max(1, 0.35*n)` wipes short PDFs. Use min 8 pages and min 3 hits.
- Classify `message` if `XXXnnnY` appears in the first few lines, not only line 1 (headings precede IDs).
- Qdrant ids: UUID5, not sha256 hex.
- `query_points` takes `query_filter`, not `filter` (the unit fake masked this; do not regress).
- Image tags are the **full** git SHA. Short SHA (`::7`) will 404 on GHCR after #16.

## When you change this file

Same PR as the work that taught the rule. Keep it short. Delete advice that is no longer true.
