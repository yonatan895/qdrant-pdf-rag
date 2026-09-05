# Ingest pipeline reference

Owner: this file. Design overview: `docs/architecture.md` §4.1. Runbook:
`docs/install_and_ops.md` §3.10 (collections) and §4.5 (air-gap ingest Job).
Tests: `docs/testing.md` (hermetic, claimed-path, IPC rules).

> One fact, one owner — this file owns ingest internals. Do not duplicate
> `architecture.md` (design), `install_and_ops.md` (procedure), or code
> comments (rationale lives here only when no other doc owns it).
> Code is named by module and function, never by line number.

Pipeline shape: `walk` → `parse_pdf` (+ `sanitize`) → `strip_chrome` →
`make_chunks` (+ `classify`, message/member extraction) → `embed_batch`
(+ optional contexts) → `upsert_chunks`. Orchestrated by `run_ingest.py` in
two stages: spawn-pool parse+embed, then thread-pool check-delete-upsert,
with an inventory file for resume.

---

## 1. Discovery and vendor inference (`walk.py`)

`walk_pdfs(root)` returns a deterministically **sorted** list of PDFs;
`--limit` truncates that order, so limited runs are a stable prefix, not a
sample.

- Only `suffix.lower() == ".pdf"` is kept; `.pdx`/`.idx` catalogs are ignored
  by omission.
- Any path component starting with `.`, plus `__MACOSX` and `lost+found` at
  any depth, is skipped.
- `VENDOR_MARKERS` is a substring table checked **first-match in dict order**:
  `broadcom`, `ca-`, `/ca/` → Broadcom; `bmc` → BMC; `precisely`; `ibm`;
  `red-hat`, `redhat`. Order matters: a path containing both `ca-` and `ibm`
  reports Broadcom. `bmc`/`ca-` can false-positive on unrelated names —
  known limitation, reason unknown.
- `detect_vendor` lowercases and normalizes `\` → `/`, default `"unknown"`.
- `infer_from_path` requires the layout `root/vendor/product/version/*.pdf`
  (at least 4 path parts after resolving against the root); anything else —
  including unresolvable paths — yields `("unknown", "unknown", "")`. Note
  the empty-string (not `None`) version.

Contract tests: `tests/test_generic_pdf.py` (`detect_vendor`,
`infer_from_path`).

## 2. Parsing and metadata (`ibm_pdf.py`)

PyMuPDF is the only parser. `parse_pdf` returns a frozen, slots `ParsedDoc`.

`doc_id` precedence:
1. `FILENAME_DOCNO_RE` on `stem.upper()` — the filename wins.
2. Most-frequent `DOCNO_RE` match in the first 4 pages (or fewer for short
   docs).
3. Fallback: `path.stem`.

- `FILENAME_DOCNO_RE` is anchored with a trailing guard and is deliberately
  **not** `regexes.DOCNO_RE`: sharing the pattern would churn UUID5 point
  ids.
- `_doc_id_from_text` breaks equal-count ties with `max` over the
  **sorted** set of matches. The sort is load-bearing: `PYTHONHASHSEED`
  differs per spawn worker, so an unsorted tie-break flips `doc_id` between
  runs (found on a real z/OS corpus — DCF books carry several form numbers
  with equal counts). Never "clean up" the sort.
- Product/version and title scan the first pages only (4 for product/version,
  first 10 lines for title); `z/OS` normalizes via `lower().replace("/","")`;
  a generic version returns `(None, "X.Y")`; title falls back to `doc_id`
  then `"Untitled"`.
- Final precedence for vendor/product/version/title:
  CLI > path layout (when not `unknown`/`""`) > text detection >
  `unknown`/`None`; empty version becomes `None`, empty vendor becomes
  `"unknown"`; title is sanitized; the TOC comes from `get_toc(simple=True)`;
  the document is closed in a `finally`.
- `sanitize_page_text` drops CSI sequences, C0 controls, bidi marks, and
  zero-width chars **in that order**, preserving `\t` and `\n`; all other
  printable bytes (including JCL padding) are byte-identical. Sanitizing
  happens **before** chrome stripping — fractured control bytes would
  otherwise break line matching.
- `sha256_file` reads in 1 MiB chunks. The parent hashes each PDF once for
  the inventory skip-check and passes the digest through
  `parse_pdf(sha256=...)` so workers never re-read the file.

Contract tests: `tests/test_parser_ibm_shape.py`,
`tests/test_generic_pdf.py`, `tests/test_sanitize.py`.

## 3. Chrome stripping (`chrome.py`)

Frequency-based running header/footer removal, computed **once per document**
(`strip_chrome`).

- A line becomes chrome when it appears on at least
  `max(3, int(0.35 * n_pages))` sampled pages. Documents under 8 pages skip
  chrome removal entirely — a minimum-1 threshold would wipe short PDFs.
- Sampling: up to 64 pages → all pages; larger docs → uniform sampled
  indices, deduped.
- Each page votes once per line (per-page deduped set before counting), so a
  line repeated 50× on one page counts once.
- Matching is case- and whitespace-insensitive (whitespace collapsed,
  stripped, lowercased).
- Page numbers are stripped even when infrequent: ASCII decimals (no inner
  dot, so `1.2` survives) or strict roman numerals. A loose roman match once
  deleted words like `XML`, `civil`, `dim` — the strict form is a regression
  fix; `mix=1009`/`di=501` stay numerals by design.
- `strip_page` keeps blank lines, drops chrome or page-number lines, and
  strips newlines only (not spaces) from the joined result.

Contract tests: `tests/test_classify_messages.py`; `testing.md` mandates a
≥8-page chrome fixture.

## 4. Chunking (`chunk.py`)

Section-outline chunking with per-statement code protection. Point id =
`UUID5(NAMESPACE_URL, "doc_id|heading_path|page_start|ordinal")`.

- Budgets: `SECTION_MAX_CHARS = 3500` with a 400-char overlap. 3500 (not
  6000) keeps table/code pages inside 4096-token embedders; the worst-case
  embedded-string budgets are pinned by `tests/test_embed_budget.py`.
- **Id-stability warning:** the ordinal resets per section and `page_start`
  is a 0-based index, not the printed label. Renaming a heading, reordering
  the TOC, or inserting a section re-IDs every later chunk in that section
  (full re-embed; stale points are removed only via the sha-mismatch delete
  in §9).
- No TOC → one whole-document section.
- Front matter: the limit is `max(2, int(0.15 * page_count))`. Always-skipped
  sections (notices, trademarks, reader comments, bibliography, copyright,
  index) skip at any position; contents/figures/tables/summary-of-changes
  skip only at or before the limit — a mid-book same-named section is kept
  by design.
- Outline build: entries sorted by `(page, level)`, a stack popping
  deeper-or-equal levels, `heading_path` joined with `" > "`, each section
  running to the next same-or-higher-level entry (else end of doc); empty
  ranges dropped.
- Code regions (`detect_code_region`): JCL-dominant at 0.6 on left-stripped
  lines (PDFs left-pad code), a `DD DATA/*` single-card override, REXX via a
  `/* rexx` header or unbalanced `/*` vs `*/`, console via indent;
  precedence JCL > REXX > console; empty input yields no region.
- JCL splitting: `//`-cards open a statement, deeply-indented `//` lines
  continue it; a bare `//` rejoins the next line when that line contains `=`
  and no `/`. Non-card lines stay single-line units (a 20k-line SYSIN block
  is never glued).
- REXX splitting: `;` and line splits that skip nested `/* */` (TSO/E-safe),
  honor quoted strings with `''` escapes, and follow `,` continuations;
  unterminated constructs swallow to end (fail-safe toward larger, never
  split).
- Console blocks stay per-line atomic.
- Joining: adjacent atomic items share one newline, else two; offsets
  tracked; prose-only joins stay byte-identical to the legacy path.
- Overlap: the next chunk restarts from the blind 400-char tail **unless**
  that cut lands inside an atomic code statement — then it backs off to
  whole trailing items, possibly empty. Oversize atomics are emitted whole
  (no slicing; overlap restarts after them); oversize prose is char-sliced
  every 3500.
- Paragraphs split on blank lines; empties dropped with page tracking. The
  chunk label comes from a **single-page lookup** — a block spanning pages
  does not span labels. Label ranges format as empty, single, or
  `first–last` with an en-dash.
- Per chunk, `classify` (§5) plus message/member extraction run and land in
  the payload (§8).

Contract tests: `tests/test_chunk_ibm_shape.py` (outline, detectors,
splitters, section max), `tests/test_embed_budget.py`.

## 5. Classification (`classify.py`)

Fixed vocabulary — `message` / `syntax` / `table` / `narrative`. Adding a
value breaks the payload contract and the retrieval filters, so it is
forbidden. Precedence is message > syntax > table > narrative.

- `message`: a line-anchored classic `XXXnnnY` pattern matched against the
  first 4 non-blank lines. Deliberately narrower than the broad extractor
  `regexes.MSG_RE`: widening it changes `chunk_type` distribution — needs an
  eval, not a cleanup.
- `syntax`: `"::="` anywhere, or ≥2 box-drawing lines, or ≥2 syntax-marker
  lines, or a `<parm>` token anywhere.
- `table`: fraction of columnish lines (2+-space-separated columns) ≥ 0.6.
- Empty text → `narrative`.

Contract tests: `tests/test_classify_messages.py`.

## 6. Contextual prefixes (`context.py`)

Opt-in via `CONTEXTUAL_EMBED_ENABLED` (default off).
**Enabling changes every dense vector: recreate the collection on first use.**

- Per-chunk 1–2 sentence gist from a cheap chat model (never the reasoning
  model): `CONTEXT_LLM_BASE_URL` / `CONTEXT_LLM_MODEL` with a short,
  dedicated timeout distinct from the 300s answer timeout.
- Cache key `v2:sha:chunk_id` under `CONTEXT_PROMPT_VERSION = "v2"` (v1
  duplicated the header and echoed instructions).
- Model budget 256 completion tokens; deterministic 500-char cap with
  collapse-and-rstrip normalization; empty gists raise (never stored
  silent-empty).
- Cache file: explicit `context_cache_path` wins, else a sibling
  `<stem>.contexts.jsonl`; last-wins load, corrupt lines warn and
  regenerate; the parent is the single-writer appender (no lock); workers
  take a snapshot (sibling docs invisible by design); chunks scored
  sequentially.
- Fail-fast twice (parent pre-pool + worker defense-in-depth): hash mode
  with the flag on raises, missing context LLM raises, missing cache path
  raises.
- Stored in payload `context` (unindexed, observability only) and embedded
  **dense-only** — sparse stays raw terms (§7).

Contract tests: `tests/test_contextual.py`.

## 7. Embeddings (`embed.py`)

One `Embedder` (`dense`, `dense_query`, `sparse`) built once by
`build_embedder` — callers never branch on `embed_mode`.

- Embed text: a header of product/version/doc_id, then title, heading path,
  optional context, and body, joined by newlines with falsy parts dropped.
  The contextual prefix sits between heading and body so chunk terms keep
  the tail position.
- **Asymmetry contract:** `embed_batch` feeds contexts to the **dense** leg
  only; sparse embeds the raw header/title/heading/body — BM25 must match
  the terms operators type, never LLM prose.
- Hash leg (CI/dev): `[A-Za-z0-9]{2,}` tokens lowercased (1-char tokens
  dropped); `blake2b` buckets with `dense`/`spars` domain separators; dense
  `1.0+log(count)` L2-normalized, sparse raw counts with sorted indices and
  buckets mod 2³¹ (Qdrant applies IDF). `dense_query` is identical to
  `dense` — no prefix. The hash dim is fixed at 256 so CI never depends on a
  vLLM dim.
- vLLM leg: lazy endpoint validation; 60s client timeout with connect
  retries only (no POST retry); `POST {base}/embeddings` with `{model,
  input}`, results resorted by `index`.
- `dense_query` prepends `Settings.dense_query_prefix` when set — query
  vectors only, never document chunks.
- The sparse BM25 model is a process-wide single (one-entry cache).
- Timeouts: embed calls 60s (`embed_timeout_s`); ingest-side Qdrant calls
  use a 120s timeout vs 30s on the query side — split because the call
  shapes differ.

Contract tests: `tests/test_hash_embed.py`, `tests/test_contextual.py`
(dense-only), `tests/test_embed_budget.py`, `tests/test_failfast.py`.

## 8. Qdrant load (`qdrant_io.py`)

Collection + indexes-before-load + batched idempotent upsert, behind the
`ports.QdrantPoints` protocol (`query_points` takes `query_filter`, not
`filter`).

- Dense: cosine, on-disk, HNSW `m=16 ef_construct=128`, INT8 scalar
  quantization (`quantile=0.99`, `always_ram`); sparse BM25 leg with IDF and
  an on-disk index; on-disk payloads.
- Payload indexes are created **before** load — including on pre-existing
  collections: keywords `vendor, product, version, doc_id, chunk_type,
  message_ids, members, sha256` plus integer `page_start`. An unindexed
  filter becomes a scan.
- `ensure_collection` verifies the stored dim against settings on both the
  named-vector and single-vector schemas, raising `DimMismatchError`.
- Bulk indexing raises the threshold to 2³⁰ KB vs the 20000 default — and
  never to `m=0` (which drops existing HNSW). Default **off**, load-bearing
  on single-node: a measured 371-doc/246k-point bulk load ran 3× slower
  with unindexed segments. Do not enable for initial loads on small nodes.
- Point payload (13 fields + optional `context`): `vendor, product, version,
  doc_id, title, heading_path, page_label, page_start, chunk_type,
  message_ids, members, sha256, text`; `context` only when present — never
  indexed, observability only. Point id = `chunk_id` (UUID5); vectors
  `{dense, bm25}`; upserts loop `batch_size` (default 128, bounds 16–256)
  with `wait=True`; idempotent by UUID5, no app-level retry — client
  timeouts bound the calls.
- Downstream consumers: `doc_id/product/version/vendor/chunk_type/
  message_ids/members/sha256/page_start` → filtered prefetch + keyword
  indexes (`retrieve/`); `title/heading_path/page_label/text` →
  citations/prompt (`agent/`); `context` → observability only.
- `doc_sha256` reads one payload (scroll limit 1, filter `doc_id`) for the
  upsert skip-check; `delete_by_doc` deletes by `doc_id` selector with
  `wait=True`.

Contract tests: `tests/test_qdrant_io.py`,
`tests/test_ingest_robustness.py`.

## 9. Orchestration (`run_ingest.py`, `inventory.py`)

Two stages with different parallelism for a physical reason: parse workers
embed (hash embedding is GIL-bound — a thread pool would serialize it), so
parsing runs in a `spawn` process pool while check-delete-upsert runs in a
thread pool.

- **Two-level skip** (independent — understand both): parent
  `inventory.should_skip(path, sha)` (zero-parse) **and** upsert-stream
  `doc_sha256(doc_id) == sha` (zero-write). A sha mismatch deletes by doc
  and re-upserts.
- **Stale-inventory hazard:** the inventory skip never consults Qdrant.
  Re-running with an old `inventory.jsonl` against an empty or recreated
  collection silently does nothing. Delete or re-point `--progress` when the
  collection was wiped.
- `should_skip`: exact-sha plus (`upserted` always, or `dry` only when the
  current run is also dry — a real run never skips prior `dry`).
- `load_inventory`: latest record per path; torn lines ignored; appends
  open/close per record (crash-safe — a mid-run crash keeps every completed
  record).
- `spawn` context: workers re-import the tree — never switch branches
  mid-run. Per-worker cached settings/embedder/context-client/cache/Qdrant
  (ingest timeout); env inherited under spawn.
- `resolve_workers`: capped to `[1, 2*CPU]`; `Settings.ingest_workers`
  defaults to `CPU-1`; `--workers 0` means default.
- In-flight window `max(2, workers*2)` caps pending parse+upsert work so
  slow upserts never let the parent hold unbounded vectors in RAM;
  first-completed pump; upsert streams default 4 (bounds 1–8).
- `_DocLocks`: per-`doc_id` threading locks (retained, bounded by unique
  doc ids) serialize colliding form-number check-delete-upsert sequences —
  two files may legitimately share one `doc_id`.
- `_parse_one` traps everything into an error inventory record (message
  capped at 500 chars, exception class name, doc id or filename stem, zero
  pages/chunks) plus a dummy parsed doc; the future-exception path uses an
  empty sha. One bad PDF never kills the run. Records must stay picklable
  across spawn IPC (no exception objects — `httpx2.HTTPStatusError` is
  unpicklable).
- Result accounting: `skipped` + `upserted` both count as files-ok; only
  `upserted` adds upserted chunks. Bulk mode applies to real runs only,
  restored in a `finally`. The summary logs files-ok/failed/chunks/parse and
  upsert seconds/pages-per-second/bulk/elapsed-ms and warns on failures;
  exit `1` iff any failure.
- Dry-run embeds nothing (parse + chunk only); used by ingest unit tests.
- Logs are one JSON object per line — ids and counts only, never PDF text or
  secrets; parse workers return records for the parent to log.

Contract tests: `tests/test_run_ingest.py` (`main`, `resolve_workers`,
`_DocLocks`, `_parse_one`), `tests/test_ingest_robustness.py`,
`testing.md` pickle round-trip.
