# Ingest pipeline reference

Owner: this file. Design overview: `docs/architecture.md` §4.1. Runbook: `docs/install_and_ops.md` §3.10 (collections) and §4.5 (air-gap ingest Job).

> Skeleton for the docs epic (PR-A). Section headings below mark the full
> content that lands in PR-B. Each section documents inputs, outputs,
> invariants, and the reason behind non-obvious choices, with `file:line`
> evidence. One fact, one owner — do not duplicate `architecture.md`.

## 1. Discovery and vendor inference (`walk.py`)

Scope: `*.pdf` discovery order, skip rules, `VENDOR_MARKERS` first-match order, `vendor/product/version/` layout inference, `unknown` defaults.

## 2. Parsing and metadata (`ibm_pdf.py`)

Scope: `doc_id` precedence (filename → 4-page window → stem), `FILENAME_DOCNO_RE` vs `DOCNO_RE` split and UUID5-stability rationale, `PYTHONHASHSEED`-stable tie-break, product/version/title fallbacks, `sanitize_page_text` order, `ParsedDoc` shape.

## 3. Chrome stripping (`chrome.py`)

Scope: frequency threshold formula, 8-page / 3-hit minimums, 64-page sampling, page-number shapes (kept vs dropped), strip semantics.

## 4. Chunking (`chunk.py`)

Scope: `SECTION_MAX_CHARS` / overlap and the 4096-token budget, front-matter skipping, outline→section algorithm, code-atomic regions (JCL/REXX/console) and statement-boundary splitting, overlap backoff, UUID5 id contract and what churns it, payload fields (`chunk_id`, `heading_path`, `page_label`, `chunk_type`, `message_ids`, `members`, `ordinal`).

## 5. Classification (`classify.py`)

Scope: fixed `message` / `syntax` / `table` / `narrative` vocabulary (no new values), precedence and thresholds, first-lines rule.

## 6. Contextual prefixes (`context.py`)

Scope: versioned cache (`v2:sha:chunk_id`), per-chunk gist budgets, dense-only embedding, collection-recreate requirement when enabling.

## 7. Embeddings (`embed.py`)

Scope: embed-text layout (header/title/heading/context/body positions), hash vs vLLM legs, dense-query prefix (queries only), sparse-from-raw-terms rule, batching, timeouts, fail-fast.

## 8. Qdrant load (`qdrant_io.py`)

Scope: collection schema (HNSW, quantization, on-disk), payload indexes before load, dim fail-fast (both vector schemas), batched idempotent upsert, bulk-indexing default-off rationale.

## 9. Orchestration (`run_ingest.py`, `inventory.py`)

Scope: spawn-pool parse vs thread-pool upsert split (GIL rationale), two-level skip (inventory + Qdrant sha), delete-on-mismatch, `_DocLocks`, in-flight window, per-worker caches, error records, exit codes, log contract.
