# Ingest pipeline reference

Owner: this file. Design overview: `docs/architecture.md` §4.1. Runbook:
`docs/install_and_ops.md` §3.10 (collections) and §4.5 (air-gap ingest Job).
Tests: `docs/testing.md` (hermetic, claimed-path, IPC rules).

> One fact, one owner — this file owns ingest internals. Do not duplicate
> `architecture.md` (design), `install_and_ops.md` (procedure), or code
> comments (rationale lives here only when no other doc owns it).
> All `file:line` refs are against `src/mainframe_rag/ingest/` unless noted.

Pipeline shape: `walk` → `parse_pdf` (+ `sanitize`) → `strip_chrome` →
`make_chunks` (+ `classify`, message/member extraction) → `embed_batch`
(+ optional contexts) → `upsert_chunks`. Orchestrated by `run_ingest.py` in
two stages: spawn-pool parse+embed, then thread-pool check-delete-upsert,
with an inventory file for resume.

---

## 1. Discovery and vendor inference (`walk.py`)

`walk_pdfs(root)` returns a deterministically **sorted** list of PDFs
(`walk.py:45`); `--limit` truncates that order (`run_ingest.py:350-352`), so
limited runs are a stable prefix, not a sample.

- Only `suffix.lower() == ".pdf"` is kept; `.pdx`/`.idx` catalogs are ignored
  by omission (`walk.py:50-51`).
- Any path component starting with `.`, plus `__MACOSX` and `lost+found` at
  any depth, is skipped (`walk.py:20,48-49`).
- `VENDOR_MARKERS` is a substring table checked **first-match in dict order**:
  `broadcom`, `ca-`, `/ca/` → Broadcom; `bmc` → BMC; `precisely`; `ibm`;
  `red-hat`, `redhat` (`walk.py:7-16`). Order matters: a path containing both
  `ca-` and `ibm` reports Broadcom. `bmc`/`ca-` can false-positive on
  unrelated names — known limitation, reason unknown.
- `detect_vendor` lowercases and normalizes `\` → `/`, default `"unknown"`
  (`walk.py:36-40`).
- `infer_from_path` requires the layout `root/vendor/product/version/*.pdf`
  (`len(parts) >= 4` after `resolve().relative_to()`); anything else —
  including unresolvable paths (`ValueError`) — yields
  `("unknown", "unknown", "")` (`walk.py:23-32`). Note the empty-string (not
  `None`) version.

Contract tests: `tests/test_generic_pdf.py` (`detect_vendor`,
`infer_from_path`).

## 2. Parsing and metadata (`ibm_pdf.py`)

PyMuPDF is the only parser (`ibm_pdf.py:13`). `parse_pdf` returns a frozen,
slots `ParsedDoc` (`ibm_pdf.py:50-60`).

`doc_id` precedence (`ibm_pdf.py:82-87,127`):
1. `FILENAME_DOCNO_RE` on `stem.upper()` — the filename wins.
2. Most-frequent `DOCNO_RE` match in the first `min(4, page_count)` pages.
3. Fallback: `path.stem`.

- `FILENAME_DOCNO_RE` (`^([A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?)(?![\d-])`) is
  anchored with a trailing guard and is deliberately **not**
  `regexes.DOCNO_RE`: sharing the pattern would churn UUID5 point ids
  (`ibm_pdf.py:26,83`).
- `_doc_id_from_text` breaks equal-count ties with
  `max(sorted(set(matches)), key=count)` (`ibm_pdf.py:75-79`). The `sorted`
  is load-bearing: `PYTHONHASHSEED` differs per spawn worker, so an unsorted
  tie-break flips `doc_id` between runs (found on a real z/OS corpus — DCF
  books carry several form numbers with equal counts). Never "clean up" the
  `sorted`.
- Product/version and title scan the first pages only (4 for product/version
  via `PRODUCT_VERSION_RE` / `GENERIC_VR_RE`, first 10 lines for title);
  `z/OS` normalizes via `lower().replace("/","")`; generic version returns
  `(None, "X.Y")`; title falls back to `doc_id` then `"Untitled"`
  (`ibm_pdf.py:18-22,90-111`).
- Final precedence for vendor/product/version/title:
  CLI > path layout (when not `unknown`/`""`) > text detection >
  `unknown`/`None`; `version_f or None`, `vendor_f or "unknown"`, title
  sanitized, `toc=tuple(get_toc(simple=True))`, `close()` in `finally`
  (`ibm_pdf.py:129-146`).
- `sanitize_page_text` drops CSI, C0 controls, bidi marks, and zero-width
  chars **in that order**, preserving `\t` and `\n`; all other printable
  bytes (including JCL padding) are byte-identical (`ibm_pdf.py:28-47`).
  Sanitizing happens **before** chrome stripping — fractured control bytes
  would otherwise break line matching (`run_ingest.py:68-83`).
- `sha256_file` reads in 1 MiB (`1<<20`) chunks (`ibm_pdf.py:63-68`). The
  parent hashes each PDF once for the inventory skip-check and passes the
  digest through `parse_pdf(sha256=...)` so workers never re-read the file
  (`run_ingest.py:358-364`, `ibm_pdf.py:122-123`).

Contract tests: `tests/test_parser_ibm_shape.py`,
`tests/test_generic_pdf.py`, `tests/test_sanitize.py`.

## 3. Chrome stripping (`chrome.py`)

Frequency-based running header/footer removal, computed **once per document**
(`strip_chrome`, `chrome.py:82-85`).

- Constants: `FREQUENCY_THRESHOLD=0.35`, `SAMPLE_TARGET=64`,
  `MIN_PAGES_FOR_CHROME=8`, `MIN_HITS=3`; the effective threshold is
  `max(3, int(0.35*n))` (`chrome.py:12-15,64`). Documents under 8 pages skip
  chrome removal entirely — a `max(1, …)`-style threshold would wipe short
  PDFs.
- Sampling: ≤64 pages → all pages; else uniform `step=n/64` indices, deduped
  (`chrome.py:48-52`).
- Each page votes once per line (per-page deduped set before
  `Counter.update`), so a line repeated 50× on one page counts once
  (`chrome.py:62`).
- Matching is case- and whitespace-insensitive (`_normalize`: collapse
  `\s+`, strip, lower) (`chrome.py:35-36`).
- Page numbers are stripped even when infrequent: ASCII decimals
  (`[0-9]+(-[0-9]+)?[-.]?`, no inner dot so `1.2` survives) or strict roman
  numerals. A loose `[ivxlcdm]+` match once deleted words like `XML`,
  `civil`, `dim` — the strict form is a regression fix; `mix=1009`/`di=501`
  stay numerals by design (`chrome.py:18-45`).
- `strip_page` keeps blank lines, drops chrome or page-number lines, and
  strips `\n` only (not spaces) from the joined result (`chrome.py:68-79`).

Contract tests: `tests/test_classify_messages.py`; `testing.md` mandates a
≥8-page chrome fixture.

## 4. Chunking (`chunk.py`)

Section-outline chunking with per-statement code protection. Point id =
`UUID5(NAMESPACE_URL, f"{doc_id}|{heading_path}|{page_start}|{ordinal}")`
(`chunk.py:111-113`).

- Budgets: `SECTION_MAX_CHARS=3500`, `SPLIT_OVERLAP_CHARS=400`
  (`chunk.py:23-26`). 3500 (not 6000) keeps table/code pages inside 4096-token
  embedders; the worst-case embedded string budgets are pinned by
  `tests/test_embed_budget.py`.
- **Id-stability warning:** `ordinal` resets per section
  (`enumerate(_split_blocks)` inside the section loop) and `page_start` is a
  0-based index, not the printed label (`chunk.py:418-419,96-105`). Renaming a
  heading, reordering the TOC, or inserting a section re-IDs every later
  chunk in that section (full re-embed; stale points are removed only via the
  sha-mismatch delete in §9).
- No TOC → one whole-document section (`chunk.py:121-122`).
- Front matter: `front_matter_limit = max(2, int(0.15*n))`
  (`FRONT_MATTER_FRACTION=0.15`, `FRONT_MATTER_MIN_PAGES=2`).
  `SKIP_ALWAYS_RE` (notices/trademarks/reader comments/bibliography/
  copyright/index) skips at any position; `FRONT_MATTER_RE`
  (`contents|figures|tables|summary of changes`) skips only at or before the
  limit — a mid-book same-named section is kept by design
  (`chunk.py:124-137`, `regexes.py:28-34`).
- Outline build: entries sorted by `(page, level)`, stack pops `>=level`,
  `heading_path` joined with `" > "`, section `start=max(0,page_1based-1)`,
  `end` at the next same-or-higher level (else end of doc); empty ranges
  dropped (`chunk.py:127-154`).
- Code regions (`detect_code_region`, `chunk.py:31-84`): JCL-dominant at 0.6
  on left-stripped lines (PDFs left-pad code), `DD DATA/*` single-card
  override, REXX via `/* rexx` header or unbalanced `/*` vs `*/`, console via
  indent; precedence JCL > REXX > console; empty input yields `None`.
- JCL splitting (`chunk.py:159-211`): `^//\S` and `^//\s\S` open a statement,
  `^//  ` (2+ spaces) continues; a bare `//` rejoins the next line when that
  line contains `=` and no `/`. Non-card lines stay single-line units (a 20k-
  line SYSIN block is never glued).
- REXX splitting (`chunk.py:214-280`): `;` and line splits that skip nested
  `/* */` (counter, TSO/E-safe), honor `"…"`/`'…'` with `''` escapes, and
  follow `,` continuations; unterminated constructs swallow to end (fail-safe
  toward larger, never split).
- Console blocks stay per-line atomic (`chunk.py:296`).
- `_join_items`: adjacent atomic items share one `\n`, else `\n\n`; offsets
  tracked; prose-only joins stay byte-identical to the legacy path
  (`chunk.py:300-318`).
- Overlap (`_overlap_seed`, `chunk.py:321-334`): the next chunk restarts from
  the blind 400-char tail **unless** that cut lands inside an atomic code
  statement — then it backs off to whole trailing items, possibly empty.
  Oversize atomics are emitted whole (no slicing; overlap restarts after
  them); oversize prose is char-sliced every 3500.
- Paragraph split is `_BLANK_SPLIT_RE = \n\s*\n` (`chunk.py:28`); empties
  dropped with `(page_idx, para)` tracking (`chunk.py:403-434`). The chunk
  label comes from a **single-page lookup** (`labels[page_idx]`) — a block
  spanning pages does not span labels. `page_label_range` formats `0→""`,
  single, or `first–last` with an en-dash (`chunk.py:390-397`).
- Per chunk, `classify` (§5) plus `find_message_ids` / `find_members` run and
  land in the payload (§8).

Contract tests: `tests/test_chunk_ibm_shape.py` (outline, detectors,
splitters, `SECTION_MAX`), `tests/test_embed_budget.py`.

## 5. Classification (`classify.py`)

Fixed vocabulary — `message` / `syntax` / `table` / `narrative`. Adding a
value is a lethal mistake (payload contract + filters depend on it).
Precedence is message > syntax > table > narrative (`classify.py:24-40`).

- `message`: `MESSAGE_LINE_RE=^\s*[A-Z]{3}\d{2,5}[A-Z]` matched against the
  first 4 non-blank lines (`classify.py:16-29`). Deliberately narrower and
  line-anchored vs the broad extractor `regexes.MSG_RE`: widening it changes
  `chunk_type` distribution — needs an eval, not a cleanup.
- `syntax`: `"::="` anywhere, or ≥2 box-drawing lines (18-char set), or ≥2
  syntax-marker lines (`::=|>>-|>>+|<--|--\+|-\+-|--\-`), or a `<parm>` token
  anywhere (`classify.py:11-14,31-34`).
- `table`: fraction of columnish lines (2+-space-separated columns,
  `_COLUMN_RE`) ≥ 0.6 (`classify.py:14,36-38`).
- Empty text → `narrative` (`classify.py:24-25`).

Contract tests: `tests/test_classify_messages.py`.

## 6. Contextual prefixes (`context.py`)

Opt-in via `CONTEXTUAL_EMBED_ENABLED` (default `False`, `config.py:177`).
**Enabling changes every dense vector: recreate the collection on first use.**

- Per-chunk 1–2 sentence gist from a cheap chat model (never the reasoning
  model): `CONTEXT_LLM_BASE_URL` / `CONTEXT_LLM_MODEL`, timeout
  `context_llm_timeout_s=30.0` (`config.py:177-188`).
- Cache key `v2:sha:chunk_id`; `CONTEXT_PROMPT_VERSION="v2"` (v1 duplicated
  the header and echoed instructions) (`context.py:46,96`).
- Model budget `MAX_COMPLETION_TOKENS=256`; deterministic
  `context_max_chars=500` cap with collapse + rstrip normalization; empty
  gists raise (never stored silent-empty) (`context.py:56-57,88-96,99-150`).
- Cache file: explicit `context_cache_path` wins, else a sibling
  `<stem>.contexts.jsonl`; last-wins load, corrupt lines warn and regenerate;
  the parent is the single-writer appender (no lock); workers take a snapshot
  (sibling docs invisible by design); chunks scored sequentially
  (`context.py:153-236`, `run_ingest.py:130-136`).
- Fail-fast twice (parent pre-pool + worker defense-in-depth): hash mode with
  the flag on raises, missing context LLM raises, missing cache path raises
  (`run_ingest.py:320-330`).
- Stored in payload `context` (unindexed, observability only) and embedded
  **dense-only** — sparse stays raw terms (§7).

Contract tests: `tests/test_contextual.py`.

## 7. Embeddings (`embed.py`)

One `Embedder` (`dense`, `dense_query`, `sparse`) built once by
`build_embedder` — callers never branch on `embed_mode`
(`embed.py:179-185`, `ports.py:30-39`).

- Embed text: `header = " ".join(product, version, doc_id if present)` then
  `"\n".join(header, title, heading_path, context, body if present)`;
  falsy parts dropped. The contextual prefix sits between heading and body so
  chunk terms keep the tail position (`embed.py:32-45`).
- **Asymmetry contract:** `embed_batch` feeds contexts to the **dense** leg
  only; sparse embeds the raw header/title/heading/body — BM25 must match
  the terms operators type, never LLM prose (`embed.py:196-208`).
- Hash leg (CI/dev): tokens `[A-Za-z0-9]{2,}` lowercased (1-char tokens
  dropped); `blake2b(digest 8, person=b"dense"/b"spars")`; dense
  `1.0+log(count)` L2-normalized (`norm>0` guard), sparse raw counts with
  sorted indices, buckets mod `1<<31` (Qdrant applies IDF)
  (`embed.py:27-29,60-99`). `HashEmbedder.dense_query == dense` — no prefix.
  `HASH_EMBED_DIM=256` fixed so CI never depends on a vLLM dim
  (`config.py:17-19`).
- vLLM leg: lazy `require_embed()`; client timeout `embed_timeout_s=60.0`
  with connect retries only (no POST retry); `POST {base}/embeddings`
  `{model, input}`, results resorted by `index`
  (`embed.py:137-163`, `config.py:45`).
- `dense_query` prepends `Settings.dense_query_prefix` when truthy — query
  vectors only, never document chunks (`embed.py:165-170`).
- Sparse BM25 model is process-wide single (`lru_cache(maxsize=1)`,
  `embed.py:116-120`).
- Timeouts: embed calls `embed_timeout_s=60.0` (`config.py:45`); ingest-side
  Qdrant calls use `qdrant_ingest_timeout_s=120` vs query-side
  `qdrant_timeout_s=30` — split because the call shapes differ
  (`config.py:36-37`).

Contract tests: `tests/test_hash_embed.py`, `tests/test_contextual.py`
(dense-only), `tests/test_embed_budget.py`, `tests/test_failfast.py`.

## 8. Qdrant load (`qdrant_io.py`)

Collection + indexes-before-load + batched idempotent upsert, behind the
`ports.QdrantPoints` protocol (`query_points` takes `query_filter`, not
`filter`; `ports.py:52-129`).

- Dense: cosine, on-disk, HNSW `m=16 ef_construct=128`, INT8 scalar
  quantization (`quantile=0.99`, `always_ram=True`); sparse BM25 leg IDF +
  on-disk index; `on_disk_payload=True`
  (`qdrant_io.py:54-72,108-113`).
- Payload indexes are created **before** load — including on pre-existing
  collections: keywords `vendor, product, version, doc_id, chunk_type,
  message_ids, members, sha256` + integer `page_start`
  (`qdrant_io.py:26,75-82,103-106`). An unindexed filter becomes a scan.
- `ensure_collection` verifies the stored dim against settings on both the
  named-vector (`dict`) and single-vector schemas, raising `DimMismatchError`
  (`qdrant_io.py:90-102`).
- Bulk indexing (`set_bulk_indexing`): `1<<30` vs default `20000`; never
  `m=0` (drops existing HNSW). Default **off**, load-bearing on single-node:
  measured 371-doc/246k-point bulk load ran 3× slower with unindexed
  segments (`qdrant_io.py:20-51`). Do not enable for initial loads on small
  nodes.
- Point payload (13 fields + optional `context`): `vendor, product, version,
  doc_id, title, heading_path, page_label, page_start, chunk_type,
  message_ids, members, sha256, text`; `context` only when present — never
  indexed, observability only. Point id = `chunk_id` (UUID5); vectors
  `{dense, bm25 SparseVector}`; upserts loop `batch_size` (default 128,
  bounds 16–256) with `wait=True`; idempotent by UUID5, no app-level retry —
  client timeouts bound the calls (`qdrant_io.py:152-195`,
  `config.py:145-151`).
- Downstream consumers: `doc_id/product/version/vendor/chunk_type/
  message_ids/members/sha256/page_start` → filtered prefetch + keyword
  indexes (`retrieve/`); `title/heading_path/page_label/text` →
  citations/prompt (`agent/`); `context` → observability only.
- `doc_sha256` reads one payload (`scroll limit=1`, filter `doc_id`) for the
  upsert skip-check (`qdrant_io.py:117-129`); `delete_by_doc` uses
  `FilterSelector(doc_id)` with `wait=True` (`qdrant_io.py:132-141`).

Contract tests: `tests/test_qdrant_io.py`,
`tests/test_ingest_robustness.py`.

## 9. Orchestration (`run_ingest.py`, `inventory.py`)

Two stages with different parallelism for a physical reason: parse workers
embed (hash embedding is GIL-bound — a thread pool would serialize it), so
parsing runs in a `spawn` process pool while check-delete-upsert runs in a
thread pool (`run_ingest.py:91-94,149-159,396-436`).

- **Two-level skip** (independent — understand both): parent
  `inventory.should_skip(path, sha)` (zero-parse) **and** upsert-stream
  `doc_sha256(doc_id) == sha` (zero-write). A sha mismatch deletes by doc
  and re-upserts (`run_ingest.py:286-292,359-362`).
- **Stale-inventory hazard:** the inventory skip never consults Qdrant.
  Re-running with an old `inventory.jsonl` against an empty or recreated
  collection silently does nothing. Delete or re-point `--progress` when the
  collection was wiped.
- `should_skip`: exact-sha plus (`upserted` always, or `dry` only when the
  current run is also dry — a real run never skips prior `dry`)
  (`inventory.py:50-55`).
- `load_inventory`: latest record per path; torn lines
  (`ValidationError`/`ValueError`) ignored; appends open/close per record
  (crash-safe — a mid-run crash keeps every completed record)
  (`inventory.py:28-47`).
- `mp.get_context("spawn")` (`run_ingest.py:396`): workers re-import the
  tree — never switch branches mid-run. Per-worker cached
  `Settings/Embedder/ContextClient/ContextCache/Qdrant` (ingest timeout);
  env inherited under spawn (`run_ingest.py:204-250`).
- `resolve_workers`: `max(1, min(requested_or_Settings, 2*CPU))`;
  `Settings.ingest_workers` defaults to `CPU-1`; `--workers 0` means default
  (`run_ingest.py:195-201`, `config.py:148-150`).
- In-flight window `max(2, workers*2)` caps
  `parse_pending + upsert_pending` so slow upserts never let the parent hold
  unbounded vectors in RAM; `FIRST_COMPLETED` pump; upsert streams
  `ingest_upsert_streams` default 4 (bounds 1–8)
  (`run_ingest.py:398-436`, `config.py:154-155`).
- `_DocLocks`: per-`doc_id` threading locks (retained, bounded by unique
  doc ids) serialize colliding form-number check-delete-upsert sequences —
  two files may legitimately share one `doc_id`
  (`run_ingest.py:253-270,286`).
- `_parse_one` traps everything into
  `InventoryRecord(status="error", error=str[:500], error_type=class,
  doc_id=parsed-or-stem, pages/chunks 0)` plus a dummy `ParsedDoc`; the
  future-exception path uses `sha=""`. One bad PDF never kills the run.
  Records must stay picklable across spawn IPC (no exception objects —
  `httpx2.HTTPStatusError` is unpicklable) (`run_ingest.py:169-192,442-466`;
  `testing.md` IPC rule).
- Result accounting: `skipped` + `upserted` both count `files_ok`; only
  `upserted` adds `chunks_upserted`. Bulk mode applies to real runs only
  (`ingest_bulk_load and not dry_run`), restored in `finally`. Summary logs
  `files_ok/failed/chunks/parse_s/upsert_s/pages_per_s/bulk/elapsed_ms` and
  warns on failures; exit `1` iff any failure
  (`run_ingest.py:339-348,539-597`).
- Dry-run embeds nothing (parse + chunk only); used by ingest unit tests
  (`run_ingest.py:122`, `testing.md`).
- Logs are one JSON object per line — ids and counts only, never PDF text or
  secrets; parse workers return records for the parent to log
  (`run_ingest.py:293-303,332`; `AGENTS.md` logging rule).

Contract tests: `tests/test_run_ingest.py` (`main`, `resolve_workers`,
`_DocLocks`, `_parse_one`), `tests/test_ingest_robustness.py`,
`testing.md` pickle round-trip.
