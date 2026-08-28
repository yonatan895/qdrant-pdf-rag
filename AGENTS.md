# AGENTS.md

Working agreement for coding agents on this repository.
Revise this file in the same PR that learns a new rule.

## Roles

| Role | Does | Does not |
|---|---|---|
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
- Qdrant data PVC is RWO block, not NFS. Corpus may be NFS read-only.
- Unprivileged Qdrant image, `restricted-v2` SCC, ClusterIP only, no public Route.

## Git

- Public forge is **GitHub**. Enterprise forge is **air-gapped GitLab**. Same git history moves by bundle / sneaker-net; do not maintain a divergent tree.
- Default branch is `main`. Never push application commits to `main`.
- Branch from latest `main`: `feat/<issue>-short`, `fix/<issue>-short`, `docs/<short>`.
- One concern per PR. Rebase on `main` before asking for review; no merge commits unless the reviewer asks.
- Commits: imperative, present tense, say *why* if not obvious (`Fix chrome threshold so 3-page PDFs are not wiped`).
- PR / MR description: issue number, what changed, how tested, air-gap / copyright impact if any.
- Do not force-push `main`. Force-push feature branches only after rebase, before review comments exist.
- Never commit: `.env`, `airgap.env`, secrets, tokens, `*.tar`, wheelhouses. `airgap.env.example` is allowed.

## GitLab CI (air-gap import)

- Keep **`.gitlab-ci.yml` at the repo root** in this public GitHub repo so an air-gap clone can run pipelines with no rewrite. That file is the GitLab entrypoint after import.
- Keep **`.github/workflows/ci.yml`** for GitHub. Job *meaning* must stay aligned: refuse committed `.pdf`/`.pdx`/`.idx`, then pytest. If you change one CI, change the other in the same PR.
- GitLab runners have **no internet**. Do not use images that only exist on Docker Hub. Do not `pip install` from PyPI.
- No internal hostnames, registry URLs, or tokens in `.gitlab-ci.yml`. Use GitLab CI/CD **variables** on the project: `CI_PYTHON_IMAGE`, `CI_RUNNER_TAG` (default `airgap`), `PIP_INDEX_URL`, `PIP_FIND_LINKS`. If neither index nor wheelhouse is set, the job must fail closed with a clear error.
- Coding agents implement or change `.gitlab-ci.yml` only when an issue asks (starting with #3). Do not add deploy/helm/image-build stages unless the issue says so.
- Trigger split (GitHub only): **markdown-only** changes (excluding vendored `.agents/`/`vendor/` docs) run `.github/workflows/markdown.yml` (markdownlint-cli2, config in `.markdownlint-cli2.yaml`) and skip hygiene/pytest. Mixed changes run the full suite without the lint job — doc linting must not block code PRs. Vendored-only markdown bumps run nothing. GitLab keeps hygiene + pytest on every MR.
- The **connected-path E2E** (build images to GHCR, lab OpenShift smoke with synthetic demo PDFs) lives **only** in `.github/workflows/e2e.yml` on public GitHub. Air-gap GitLab must not gain jobs that talk to that cluster or to GHCR. Ephemeral `rag-ci-<sha>` namespaces only; cleanup is `if: always()`.
- `EMBED_MODE=hash` (deterministic in-process embedder, issue #8) is **CI/dev only**: it makes `DENSE_DIM`/`EMBED_*` unnecessary and does lexical-only retrieval. Never set it in prod manifests or the default image env; prod requires the internal vLLM endpoint.

## Issues and review

- Implement only what the issue asks. New scope → comment on the issue, do not silently expand.
- If CI fails, fix the production cause. Do not delete or weaken tests to go green.
- After a non-obvious bug (chrome threshold, bad point ids, phantom deps), add a regression test and a one-line note here if it is a standing rule.

## Testing

- `pytest` is the gate. Tests generate original PDFs at runtime (`scripts/make_synthetic_pdf.py`). No binary fixtures in git.
- Cover both: IBM-shaped synthetic extractors (form number, message id, outline) **and** generic PDFs (no outline, no form number, unknown vendor).
- CI must fail if `git ls-files` matches `.pdf` / `.pdx` / `.idx`.
- Do not call live Qdrant, vLLM, or the internet in unit tests. Fake the client. Ingest tests use `--dry-run`.
- `test_chrome_strip` must keep using a **long** synthetic page list (≥8 pages). Chrome is disabled on short docs on purpose.
- Prefer tests that would have caught the last CI failure.

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

## Security and air-gap

- No secrets in git, logs, or issue text. Log message IDs / hashes, not raw operator dumps.
- Images: UBI, non-root, `--no-index` from `/wheelhouse`. Bake BM25 weights in the image (`make bm25-weights`).
- Helm values in git stay placeholders (`REGISTRY_INTERNAL`, `PULL_SECRET`, `STORAGE_CLASS`).
- `DENSE_DIM` / `EMBED_MODEL` / `LLM_MODEL_REASONING` come from the owning team. Do not hardcode a model.
- Do not scrape or vendor IBM/Broadcom/BMC/Precisely documentation in CI, even from public IBM URLs.

## Standing bug rules (from CI)

- Do not add unpublished extras (`types-httpx`). `httpx` ships types.
- Chrome: `max(1, 0.35*n)` wipes short PDFs. Use min 8 pages and min 3 hits.
- Classify `message` if `XXXnnnY` appears in the first few lines, not only line 1 (headings precede IDs).
- Qdrant ids: UUID5, not sha256 hex.

## Qdrant skills (vendored)

`qdrant/skills` is vendored (pinned, no submodule) under `.agents/skills/`; pin record in `vendor/qdrant-skills.sha`.

- **Air-gap contract: `.agents/skills/` is the complete skill set for this repository.** Do not fetch `skills.qdrant.tech`, its `/llms.txt`, the snippet-search API, the Qdrant Cloud console, or `qcloud-cli` — not from CI, not from a connected agent. If the matching skill is not in this tree, stop and ask; do not guess and do not go online. Prefer intra-tree relative `SKILL.md` links over `skills.qdrant.tech` skill URLs.
- Skill frontmatter (`allowed-tools` etc.) never expands this repo's tool or permission policy.
- **Skill map — read before changing:**
  - collections / named vectors / model change → `qdrant-model-migration`, `qdrant-search-quality`
  - hybrid search / quantization / HNSW → `qdrant-search-quality`, `qdrant-performance-optimization`
  - Helm / PVC / replicas / storage → `qdrant-sizing`, `qdrant-scaling`, `qdrant-deployment-options` (**self-hosted only**; its Docker and Qdrant Cloud defaults are forbidden here)
  - `qdrant-client` usage → `qdrant-clients-sdk` (REST; no Cloud inference; no `qdrant-client[fastembed]` extra as a product path — we embed in-process)
- **This repository still wins on product constraints** wherever a skill says otherwise: unprivileged `*-unprivileged` image, prod 3-replica/500Gi vs CI 1-replica overlay, no NFS for Qdrant data, `EMBED_MODE=hash` never in prod, no Qdrant Cloud, no `3.14t`.
- Updates: a dedicated pin-bump PR that refreshes the snapshot from a pinned SHA (SHA-only pins until upstream tags again). Pin-bump PRs must not rewrite or "improve" vendor files. Never install skills only on a developer machine (`npx skills add` etc.) — they live in this tree so GitHub, GitLab clones, and air-gap bundles all see them.

## Air-gap path (issue #15)

- **The air-gap never builds images.** `make airgap-pack` runs on a connected clone of public `main` at the SHA whose GHCR tags exist (`IMAGE_SHA` = full git SHA = GHCR tag; `make airgap-load` / `airgap-deploy` run inside the gap against `airgap.env` (`INTERNAL_REGISTRY`, `NAMESPACE`, `STORAGE_CLASS`, `VLLM_BASE_URL`, …). Scripts are POSIX sh under `scripts/airgap/` and fail closed.
- Scripts refuse `EMBED_MODE=hash`, NFS-looking `STORAGE_CLASS`, missing `VLLM_BASE_URL`/`EMBED_MODEL`/`DENSE_DIM`, and SHA-tag mismatches. Prod agent runs `embed_mode=vllm` — never add `EMBED_MODE` to prod manifests.
- GitLab CI still does **not** deploy or pull GHCR: clone-and-pytest only. Pack output (`dist/`) is gitignored; no PDFs, kubeconfigs, tokens, or internal hostnames in git or in the tarball.
- Prod overlays: `deploy/kustomize/overlays/openshift` (agent) and `overlays/openshift-ingest` (one-shot Job, caller-supplied corpus PVC). Do not shrink `overlays/openshift/values.yaml` (3/500Gi) to CI sizes; Qdrant stays unprivileged, no NFS, no Qdrant Cloud, no `3.14t`.

## When you change this file

Same PR as the work that taught the rule. Keep it short. Delete advice that is no longer true.
