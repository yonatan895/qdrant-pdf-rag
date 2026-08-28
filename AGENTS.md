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
- Qdrant point ids are UUID or unsigned int only (use UUID5 of the chunk key). sha256 hex is invalid.
- `/v1/answer` uses the reasoning model only. `/v1/search` does not call an LLM.
- Qdrant data PVC is RWO block, not NFS. Corpus may be NFS read-only.
- Unprivileged Qdrant image, `restricted-v2` SCC, ClusterIP only, no public Route.

## Git

- Default branch is `main`. Never push application commits to `main`.
- Branch from latest `main`: `feat/<issue>-short`, `fix/<issue>-short`, `docs/<short>`.
- One concern per PR. Rebase on `main` before asking for review; no merge commits unless the reviewer asks.
- Commits: imperative, present tense, say *why* if not obvious (`Fix chrome threshold so 3-page PDFs are not wiped`).
- PR description: issue number, what changed, how tested, air-gap / copyright impact if any.
- Do not force-push `main`. Force-push feature branches only after rebase, before review comments exist.
- Never commit: `.env`, `airgap.env`, secrets, pull-secret names with tokens, `*.tar`, wheelhouses, `airgap.env` (example file is allowed).

## Issues and review

- Implement only what the issue asks. New scope → comment on the issue, do not silently expand.
- If CI fails, fix the production cause. Do not delete or weaken tests to go green.
- After a non-obvious bug (chrome threshold, bad point ids, phantom deps), add a regression test and a one-line note here if it is a standing rule.

## Testing

- `pytest` is the gate. Tests generate original PDFs at runtime (`scripts/make_synthetic_pdf.py`). No binary fixtures in git.
- Cover both: IBM-*shaped* synthetic extractors (form number, message id, outline) **and** generic PDFs (no outline, no form number, unknown vendor).
- CI must fail if `git ls-files` matches `\\.(pdf|pdx|idx)$`.
- Do not call live Qdrant, vLLM, or the internet in unit tests. Fake the client. Ingest tests use `--dry-run`.
- `test_chrome_strip` must keep using a **long** synthetic page list (≥8 pages). Chrome is disabled on short docs on purpose.
- Prefer tests that would have caught the last CI failure.

## Abstractions

Keep the pipeline boring and layered. Do not add LangChain, LlamaIndex, or a second vector DB.

| Module | Owns |
|---|---|
| `walk` | `*.pdf` only; skip catalogs; path layout `vendor/product/version/` |
| `ibm_pdf` (parse) | Open, metadata, optional IBM signals, generic fallbacks |
| `chrome` | Repeated headers/footers; never threshold=1; skip docs &lt; 8 pages |
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

## When you change this file

Same PR as the work that taught the rule. Keep it short. Delete advice that is no longer true.
