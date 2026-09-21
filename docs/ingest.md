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
`UUID5(NAMESPACE_URL, "source_rev|heading_path|page_start|ordinal")`
where `source_rev` is `normalize(vendor)|normalize(product)|normalize(version)|sha256`
(issue #361): same-form-number revisions mint disjoint point ids, so a
different revisions do not collide; same-revision writers still need coordination. The UUID5-of-key
scheme never changes; the payload `doc_id` stays the printed family key
for citations and filters.

- Budgets: `SECTION_MAX_CHARS = 3500` with a 400-char overlap. 3500 (not
  6000) keeps table/code pages inside 4096-token embedders; the worst-case
  embedded-string budgets are pinned by `tests/test_embed_budget.py`.
- **Id-stability warning:** the ordinal resets per section and `page_start`
  is a 0-based index, not the printed label. Renaming a heading, reordering
  the TOC, or inserting a section re-IDs every later chunk in that section
  (full re-embed; stale points are removed only via the revision delete in
  §9). Relabeling a file (vendor/product/version) or changing its bytes
  likewise re-IDs the whole document — that is the 361B migration churn,
  a one-time event, not ongoing instability.
- No TOC → windowed fallback sections (`fallback_sections`, issue #216),
  not one whole-document blob: a new section opens at a heading-like page
  lead (numbered headings, Chapter/Appendix/Section leads) or every
  `FALLBACK_MAX_PAGES = 10` pages, whichever comes first. Deterministic in
  the input (same pages → same sections and ordinals).
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
  `/* rexx` header, unbalanced `/*` vs `*/`, or (issue #216) a keyword
  fallback — ≥2 line-initial REXX keywords plus an assignment or `;`, so
  balanced samples without headers still detect while "Do not"/"If" prose
  without code signals stays prose; console via indent;
  precedence JCL > REXX > console; empty input yields no region.
- JCL splitting: `//`-cards open a statement, deeply-indented `//` lines
  continue it; a bare `//` rejoins the next line when that line contains `=`
  and no `/`. Non-card lines stay single-line units (a 20k-line SYSIN block
  is never glued).
- REXX splitting: `;` and line splits that skip nested `/* */` (TSO/E-safe),
  honor quoted strings with `''` escapes, and follow `,` continuations;
  unterminated constructs swallow to end (fail-safe toward larger, never
  split).
- Table regions (`detect_table_region`, issue #216): column blocks per the
  shared `classify.is_table_block` rule (one rule, one helper) that are NOT
  code (JCL continuations carry wide indents that read as columns, so code
  wins). Rows are atomic like code statements: overflow splits at row
  boundaries (one line is one row), the overlap backs off to whole rows,
  and an oversize single row emits whole.
- Mixed prose+JCL paragraphs (`_mixed_jcl_items`, issue #216): runs of `//`
  cards expand to atomic statements while prose runs stay blobs — but only
  with ≥2 statement-starts, so one `//see`-style mention passes through
  byte-identical.
- SYSIN adjacency (issue #216): data paragraphs following a `DD *`/`DD DATA`
  card keep splitting between records across page/paragraph boundaries
  (line-atomic units). The chain ends at sentence punctuation (data records
  do not end lines with `.`/`?`/`!`/`:`; prose explanations do) or at a
  code/table paragraph — a missed break only makes prose line-atomic (text
  preserved), a   false break restores the status-quo slice.
- Console blocks stay per-line atomic.
- Joining: adjacent atomic items share one newline, else two; offsets
  tracked; prose-only joins stay byte-identical to the legacy path.
- Overlap: the next chunk restarts from the blind 400-char tail **unless**
  that cut lands inside an atomic (code/table) item — then it backs off to
  whole trailing items, possibly empty. Oversize atomics are emitted whole
  (no slicing; overlap restarts after them); oversize prose is char-sliced
  every 3500.
- Paragraphs split on blank lines; empties dropped with page tracking. Each
  block tracks its page span (min/max over composing items): the chunk
  label cites the full span (`_page_label_range` over every touched page),
  while the UUID pins the span start — the `doc|heading|page|ordinal` key
  contract is unchanged. Label ranges format as empty, single, or
  `first–last` with an en-dash.
- Per chunk, `classify` (§5) plus message/member extraction run and land in
  the payload (§8).

Contract tests: `tests/test_chunk_ibm_shape.py` (outline, detectors,
splitters, section max), `tests/test_embed_budget.py`.

## 5. Classification (`classify.py`)

Fixed vocabulary — `message` / `syntax` / `table` / `narrative`. Adding a
value breaks the payload contract and the retrieval filters, so it is
forbidden. Precedence is message > syntax > table > narrative.

- `message`: a line-anchored id pattern matched against the first
  `MESSAGE_SCAN_LINES = 6` non-blank lines (issue #216: 1-2 explanation
  lines may sit above the id card). The pattern covers the `MSG_RE`
  families — classic `XXXnnnY`, CICS `DFH` cards, IMS `DFS` codes with
  optional severity, 4-letter prefixes — but stays line-anchored, so a bare
  mid-line mention never flips a chunk. Widening it further changes
  `chunk_type` distribution — needs an eval, not a cleanup.
- `syntax`: `"::="` anywhere, or ≥2 box-drawing lines, or ≥2 syntax-marker
  lines, or a `<parm>` token anywhere.
- `table`: fraction of columnish lines (2+-space-separated columns) ≥ 0.6
  via `is_table_block` — the same helper `chunk.detect_table_region` uses
  for atomic row splitting (one rule per concept).
- Empty text → `narrative`.

Contract tests: `tests/test_classify_messages.py`.

## 6. Contextual prefixes (`context.py`)

Opt-in via `CONTEXTUAL_EMBED_ENABLED` (default off).
**Enabling changes every dense vector: recreate the collection on first use.**

- Per-chunk 1–2 sentence gist from a cheap chat model (never the reasoning
  model): `CONTEXT_LLM_BASE_URL` / `CONTEXT_LLM_MODEL` with a short,
  dedicated timeout (`CONTEXT_LLM_TIMEOUT_S`, 30.0s) distinct from the 300s
  answer timeout. `CONTEXT_LLM_API_KEY` (unset = keyless) rides the gist
  calls as a Bearer virtual key behind a gateway.
- Cache key `v2:sha:chunk_id` under `CONTEXT_PROMPT_VERSION = "v2"` (v1
  duplicated the header and echoed instructions).
- Model budget 256 completion tokens; deterministic `CONTEXT_MAX_CHARS`
  (500) cap with collapse-and-rstrip normalization; empty gists raise
  (never stored silent-empty).
- Cache file: explicit `CONTEXT_CACHE_PATH` (unset = sibling
  `<stem>.contexts.jsonl`); last-wins load, corrupt lines warn and
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
- The sparse BM25 model is a process-wide single (one-entry cache) loaded
  from `BM25_MODEL` (`Qdrant/bm25`); `BM25_CACHE_DIR` overrides the weight
  location (images set it to the baked `/opt/bm25`, unset = library
  default cache).
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
- Point payload (15 fields + optional `context`): `vendor, product, version,
  doc_id, source_rev, title, heading_path, page_label, page_start, chunk_type,
  message_ids, members, sha256, rules_v, text`; `context` only when present — never
  indexed, observability only. Structured chunks (code/table/SYSIN) add an
  optional `units` list of `[start, end, kind]` atomic/prose spans over the
  stripped text for prompt packing (issue #368) — additive and unindexed;
  prose chunks omit the key, so their payloads are byte-identical to before. Point id = `chunk_id` (UUID5 over the
  revision-keyed chunk key); vectors
  `{dense, bm25}`; upserts loop `batch_size` (default 128, bounds 16–256)
  with `wait=True`; idempotent by UUID5, no app-level retry — client
  timeouts bound the calls.
- Pair-length contract (issue #359): `chunks` and `vectors` must align
  exactly — a mismatch raises instead of zip-truncating.
- Completion records (issues #359 + #361, `ingest/completion.py`): one point
  per published document generation in `<collection>__completions`
  (same dim, dummy vectors, keyword indexes on
  `doc_id/sha256/rules_v/generation_id/target_collection`). Markers are
  per-revision: the point id scopes to
  `(target, doc_id, source_rev, generation_id)` and the payload carries
  `source_rev`, so coexisting revisions under one `doc_id` verify
  independently and a refresh retires exactly its lineage's markers (plus
  sole-history legacy markers). The
  generation binds source hash + the `representation_fingerprint` projection
  (field policy owned by `representation.REEMBED_FIELDS`) + CLI source
  triple (`vendor|product|version` overrides, `source_labels()`) + target
  collection, with expected chunk count and chunk-ID/content digests.
  Written only after every batch is acknowledged and the stored points
  verify (count + per-point sha/rules/revision + recomputed digests);
  refreshes invalidate the revision's markers before deleting points, so a
  failed refresh leaves no valid completion and retries safely. Zero-chunk
  docs are an explicit `empty` outcome — never published, never skippable.
- Downstream consumers: `doc_id/product/version/vendor/chunk_type/
  message_ids/members/sha256/rules_v/page_start` → filtered prefetch + keyword
  indexes (`retrieve/` — untouched by the identity migration: citations and
  filters still key on the printed family); `title/heading_path/page_label/text` →
  citations/prompt (`agent/`); `source_rev` → revision tooling (refresh
  targeting, coexistence) + keyword index; `context` → observability only.
- `stored_doc_revisions` lists the distinct source revisions under a
  `doc_id` (paginated to exhaustion via the shared `scroll_all_points`
  helper, page size `Settings.ingest_scan_page_size`, `None` for pre-361B
  points) for the refresh plan; `delete_by_revision` deletes exactly one
  revision with `wait=True`;
  `delete_by_doc` remains for sole-history legacy residue only (never over
  coexisting revisions).

Contract tests: `tests/test_qdrant_io.py`,
`tests/test_ingest_robustness.py`.

## 9. Orchestration (`run_ingest.py`, `inventory.py`)

Two stages with different parallelism for a physical reason: parse workers
embed (hash embedding is GIL-bound — a thread pool would serialize it), so
parsing runs in a `spawn` process pool while check-delete-upsert runs in a
thread pool.

- **Bound two-level skip with verified completion** (issues #124 + #359,
  independent — understand all three): parent
  `inventory.should_skip(rec, sha, rules_version=extraction_rules_version())`
  (zero-parse) **plus** a Qdrant completion check
  (`completion.is_doc_complete`: valid marker for this target generation
  **and** verified points) before skipping; the upsert stream skips only on
   the same verified completion. A single sampled point proves nothing —
   `stored_doc_revisions` is a revision-scoped observer (the distinct
   `source_rev` values under a `doc_id`, `None` for pre-361B points), not a
   delete probe. Partial residue, wiped or
  restored collections, and legacy marker-less state all re-ingest, never
  skip. Inventory `upserted`/`skipped` lines carry the generation binding
  (`generation_id`, `chunk_ids_digest`, `content_digest`); pre-#359 lines
  without it re-verify in Qdrant.
  Extraction rules version (`rules_v`) is a 16-hex SHA-256 over the 5 payload-producing
  modules (`regexes.py`, `ingest/ibm_pdf.py`, `ingest/chrome.py`, `ingest/chunk.py`,
  `ingest/classify.py`). If either the PDF SHA, `rules_v`, or the representation
  fingerprint mismatches, skip is refused: the document is re-parsed and existing
  points in Qdrant are deleted and re-upserted.
  Additionally, `run_ingest` checks `stored_rules_version(client, settings)` at startup;
  if a non-empty collection has mismatched `rules_v`, it fails closed unless `--reingest`
  is passed.
- **Stale-inventory hazard (closed):** the planner re-verifies every
  inventory skip against Qdrant, so an old `inventory.jsonl` against a
  wiped collection re-ingests instead of silently doing nothing.
- **Single writer:** one ingest run per progress directory (fcntl
   `LOCK_EX|LOCK_NB` on `<progress>.lock`, fail-closed); disjoint Jobs
   against one collection must still run serially. `_DocLocks` remains the
   in-process per-revision guard only.
- **Representation manifest, enforced** (issue #362,
  `ingest/representation.py`): every non-dry run ensures one fixed-ID
  manifest point in `<collection>__completions` (get-by-id, no index;
  written only when the stored contract differs, so steady-state reruns
  stay zero-write) plus an `action: representation` run-log line, and
  every completion + inventory record carries the 16-hex `manifest_digest`. The manifest is
  the stored-representation contract — extraction rules, identity schema
  (`source_rev`), dense mode/model/operator-revision/
  dim, contextual block (enabled, LLM id, prompt version, max chars),
  sparse model/weights revision — plus the record-only query prefix
  (query-side drift is an evaluation event, never a re-embed trigger).
  The gateway exposes only mutable aliases, so the dense revision is an
  operator-declared fingerprint, never an inferred weight id; vllm mode
  with a blank `EMBED_MODEL_REVISION` fails closed (hash mode exempt),
  including under `--reingest` — the force flag bypasses stored-data
  rejection, never the requirement to name the representation being
  written.
- **Migration lifecycle (issue #391 F2; scope proof from the #391 current
  packet):** the stored contract carries an envelope-level `state`. A run
  whose wanted contract differs from the stored committed one — or that
  finds a fresh/legacy target — writes it `pending` before any
  delete/upsert and flips it `committed` only on the success path: zero
  document failures AND a read-only scope proof that (a) no completion
  marker under another `manifest_digest` remains and (b) every searchable
  point is attributable to a verified walked document of this run. The
  commit proof is strict: approved pre-361B legacy membership is not
  accepted there, because a new contract may only be declared over vectors
  this run re-embedded. Unmarked old data therefore blocks the commit and
  leaves the contract pending; points an explicit, lock-validated
  `--retire-doc` plan removes before the swap audit are excused at commit
  and enforced gone downstream. Unknown data is preserved, never deleted;
  `--limit` is refused for any migration (`refuse_limited_migration`)
  except a fresh empty-target bootstrap. A pending contract is never
  ordinarily skippable (`check_ingest_compatible`), servable
  (`serving_outcome`/lifespan; `/healthz` degrades), or swappable
  (`verify_all_complete`); resumption is `--reingest`. For alias publication,
  retrying the same sidecar-bound build may reuse its completed document
  checkpoints: the staging contract must exactly match the requested contract,
  and each skip verifies a revision-scoped completion targeting that staging,
  its exact manifest digest, and the actual stored point count/ID/content
  digests. Inventory alone and inherited live completions never qualify.
  Incomplete, missing or corrupt checkpoints are reprocessed. Recovery is at
  document granularity; a document interrupted before its durable checkpoint
  may be replayed. Full-corpus commit and alias-swap verification still run.
- **Generation identity (issue #391 F2):** completion ids and staging
  names derive from the versioned fingerprint `rp2:` — a digest of the
  manifest's `REEMBED_FIELDS` projection, the same field policy
  `compare_manifests` uses (one tuple, no second field list). A
  revision-only change therefore changes completion ids, staging names,
  and skip eligibility together; record-only fields (dense query prefix)
  stay out. A future fingerprint-format bump invalidates old markers
  explicitly — one fail-closed re-ingest cycle — documented in the
  changelog note below; chunk UUID5s never change.
- **Representation preflight (issue #362):** before any parse worker
  spawns, the run proves the target accepts its contract
  (`check_ingest_compatible`, one rule with the serving check). Explicit
  outcomes: empty target proceeds (the run opens its contract pending);
  compatible or record-only drift proceeds (drift logs
  `action: representation_drift` — re-evaluation owed, never a re-ingest);
  re-embed drift, legacy unversioned state, or a pending contract raises
  with the `--reingest` remediation. `--reingest` is the one deliberate
  migration step (same override idiom as the #124 rules gate, which runs
  first so its error precedence is unchanged). Skip paths share the
  contract structurally: the preflight proves run-level compatibility
  before any ordinary skip is evaluated. A forced alias-build retry uses the
  stricter checkpoint proof above, so a stale completion can never cause a
  skip under a drifted representation. For ordinary skips, marker
  `manifest_digest` values are audit (the generation identity gate is the
  fingerprint); forced-build retries require an exact digest match. The
  digest also participates in the commit-time scope proof.
- **Source-revision identity** (issue #361, `ingest/identity.py` +
  revision-keyed pipeline): three identities — printed `doc_id`
  (family/citation key), `source_rev`
  (`normalize(vendor)|normalize(product)|normalize(version)|sha256`, the
  destructive key for locks, deletes, completions, and chunk ids),
  committed generation. Planning (in-place and alias-publish prewalk
  alike) gates the walked corpus before any parse worker spawns:
  byte-identical copies under several paths ingest exactly once (the
  lexicographically smallest corpus-relpath wins; losers log
  `action: duplicate` and take no inventory record, so reruns re-elect the
  same winner with no stored state), and distinct revisions claiming one
  `doc_id` abort fail-closed (`RevisionCollisionError`: doc id +
  corpus-relative paths + sha16s, never manual text or absolute paths).
  Unreadable files map to no key — prescan validates identity, never file
  health (the worker still error-records them per file), so a corrupt twin
  neither causes nor hides a collision. Prescan resolves through the same
  `ibm_pdf.resolve_doc_id` helper the workers use (filename-form without
  opening, else first-four-pages text), so prescan keys and worker doc ids
  cannot diverge; cost is one serial open + short text scan per file on top
  of the hashing the parent already does. Mount relocation does not change
  source-revision keys. Publication currently hashes walked path strings in
  `corpus_fingerprint`, so physical build names can change with a mount path;
  this is a distinct identity and remains a #361/#391 design question.
- **Refresh lineage rule:** a refresh replaces precisely the revision named
  by inventory lineage (the path's previous `source_rev`, threaded from
  the parent plan into the upsert stage). Committed coexisting revisions
  are left alone — replacing one needs its lineage, otherwise the write
  would be the overwrite bug. Completion-less residue is swept (crash-safe
  retry); sole-history legacy residue migrates via `doc_id` delete; a
  lineage-less run therefore ADDS revisions — keep the `--progress` file
  (prod `/work` persists it) so refreshes replace. Unattributable
  sourceless residue beside named revisions raises `AmbiguousRevisionError`
  (doc id + revision/content ids, remediation included) before any delete.
- **361B migration runbook (snapshot-gated, operator-driven, never
  automatic):** (1) snapshot the collection (`create_snapshot` — the same
  mechanism publish uses for safety snapshots); (2) re-ingest in place —
  under the #391 F2 fingerprint (`rp2:`) every pre-rp2 marker is
  unreachable, so this first pass re-ingests the whole walked corpus
  (one fail-closed cycle, unchanged docs included); changed docs also
  migrate their history precisely, and the representation manifest flips
  to `identity_schema: source_rev`; (3) verify counts plus
   `verify_all_complete` semantics (every walked doc revision-verified);
   (4) roll back by restoring the snapshot — exercised in sim by
   `tests/test_integration_sim.py::test_361_inplace_snapshot_restore_keeps_coexisting_revisions`
   (snapshot → delete → upload-restore → counts/revisions/server-side
   filters/resume-skip re-verified; alias rollback stays the primary
   production path). Docs whose history predates
  revision stamps AND share a `doc_id` with a named revision need one
  explicit cleanup first (delete the stale revision's points by
  `doc_id` + content-sha filter, then re-ingest) — the refresh error names
  exactly that. Pre-rp2 completion markers read as absent, and the first
  refresh of each lineage rewrites them scoped. Evaluation needs no
  re-baseline: citations, filters, and goldens key on `doc_id`/text (see
  the eval numbers in each 361B PR).
- **Refresh visibility — alias publication** (`INGEST_ALIAS_PUBLISH=true`,
  default off; enabling by default is a dedicated follow-up PR; issue #359
  req 4/5, `ingest/publish.py` + `qdrant_io` alias/snapshot helpers):
  readers resolve `<collection>` through a Qdrant alias. The ordinary
  distinct-staging cutover keeps the old target during preparation; one
  target-held lock serializes publishers for the same alias from staging
  resolution through cutover, and a pre-swap live recheck refuses a
  candidate overtaken by a lock-bypass writer. Each publish run derives
  a deterministic staging generation
  `<collection>__gen<genfp><corpusfp>` (representation fingerprint + CLI
   source triple + walked-corpus content); an identical rerun resumes the
   same recorded unfinished build — including a suffixed allocation from a
   rollback-by-republish collision — via the `publish-<alias>.json` sidecar
   (crash-safe resume; a record naming the now-serving generation means the
   crash landed between swap and cleanup, so the rerun finalizes through
   the read-only steady-state path instead of rebuilding). Changed inputs
   address a new build, and a record for different inputs fails closed
   (remove it explicitly to abandon the recorded build). A derived name
   equal to the live physical means "already published"
  (read-only re-verify, no clone, no swap — plus the representation
  read-only check for record-only drift and for a pending same-contract
  resume). Because the fingerprint embeds every re-embed-required field
  (issue #391 F2), a revision-only change derives a **distinct** staging
  generation instead of reconverging live. Staging starts as a
  server-side snapshot-clone of live (points AND completion markers); a
  marker certifies its own `target_collection`, so the walked corpus
  re-embeds into the new generation rather than skipping across physicals
  — the clone preserves the old physical during ordinary distinct-staging
  preparation; it does not prove full coverage or incremental re-embedding. The manifest is
  re-keyed onto the staging id verbatim (`rekey_manifest` — the fixed
  point id embeds the collection name, so a byte copy is unreadable; the
  contract AND its pending/committed state are carried, never recomputed,
  or a drifted run would see its own wanted contract and sail through its
  preflight; the transfer is verified and repaired on reuse, issue #391
  F5); the inner run then enforces the same preflight, so a cloned staging
  under a changed representation fails closed until `--reingest`. With
  `--reingest` a new build re-embeds every walked document; a changed contract
  opens `pending` on staging. A retry of that same bound build retains only
  its verified completed documents. The residue proof commits a pending
  contract, and only then does the
  alias swap. `verify_all_complete` rejects a present pending record and a
  missing/unreadable/drifted final manifest on a walked corpus, plus any
  searchable point no walked document accounts for (read-only residue
  audit — the refusal deletes nothing). Publish-mode `--reingest` never
  mutates the serving physical: an unchanged contract allocates a distinct
  repair generation (suffixed, sidecar-recorded, resumable, cloned from
  live) and a representation change derives a distinct staging via the
  fingerprint; both re-embed the walked corpus, verify, and swap, and the
  superseded generation is retained for rollback. A resolved generation
  that already serves re-verifies read-only (`already_live`) — including a
  forced rerun after a swap-before-cleanup crash, which finalizes instead
  of building again. Swap failure leaves the previous generation serving (job
  fails, staging retained for retry). `--limit` subsets and empty corpora
   are refused fail-closed. Corpus deletions are NOT swept: a file missing
   from the walk is not a removal instruction — unmarked residue (including
   a previously published document that simply was not walked) refuses
   cutover until the operator either restores the file or names an explicit
   approved removal (`--retire-doc DOCID[@SOURCEREV]`, repeatable, planned
   under the target lock against the freshest approved inventory and applied
   to staging with revision-scoped deletes before verification). Whole-doc
   retirement covers approved sourceless (pre-361B) history alongside named
   revisions — the apply-time sole-history check (no named revision left in
   staging) still guards the delete; a named-only retirement never takes
   resolution — never a dead end). Retiring revision A (`--retire-doc DOCID@SOURCEREV`)
   preserves sibling revision B across planning, walked-document conflict
   checks, migration commit exclusions, deletion, and residue audits.
   Retired document revisions that reappear in the walk (or whole documents
   reappearing when retired wholesale), and unknown retirement names or
   unapproved revisions, fail closed before any mutation.
   Committed retirements persist into the inventory progress log (`status: retired`)
   upon publication cutover (and recover from the in-flight publish state sidecar
   if interrupted between swap and cleanup), ensuring subsequent ordinary runs
   recognize deliberate retirements without demanding missing chunks or allocating
   redundant generations.
   The audit's legacy allowance is a compatibility bridge with digest-level
   verification (issue #391 Q417-L1): a sourceless point is covered only when
   it belongs to an approved legacy inventory record with verified chunk count,
   chunk IDs, extraction rules, and excerpt text digests (`chunk_ids_digest`,
   `content_digest`, `rules_version`) — matching `(doc_id, sha256)` alone is
   not attribution. Unverifiable legacy data fails closed requiring `--reingest`.
   Unexplained residue under walked, unwalked or retired docs refuses and is
   preserved. The prod Job reaches these modes through
   `scripts/airgap/ingest.sh` (issue #391 current packet):
   `INGEST_ALIAS_PUBLISH=true` selects publication, `INGEST_REINGEST=true`
    renders `--reingest` (forced repair), and `INGEST_RETIRE_DOCS` takes a
    **comma- or newline-separated** `DOCID[@SOURCEREV]` list rendered as
    repeated `--retire-doc` (requires alias publication; labels may carry
    `|`, `/` and interior spaces — the launcher validates structure, not an
    ASCII subset). Leading and trailing whitespace per entry is trimmed, but
    interior spaces are preserved verbatim, so a value like
    `DOC_A DOC_B` is **one entry** with an embedded space, not two entries.
    To separate two documents use a comma (`DOC_A,DOC_B`) or a newline.
    Shell/env-file example for a multi-word revision:
    `INGEST_RETIRE_DOCS="z/OS Comm Svr@v|IBM|z/OS communications server|2.5"`.
    Previous documentation described comma/space separation; operators using
    bare-space separation between entries must migrate to comma or newline
    delimiters.
   The launcher keeps one Job name and the shared `/work` progress path,
   and never flips a mode implicitly: one authorized publisher at a time,
   no distributed lock.
  During a migration retained markers block the commit until
  the operator re-ingests the complete corpus or cleans the stale
  generation. First-publish cutover from a legacy physical layout snapshots the
  squatter, deletes it (brief documented maintenance window), then creates
  the alias; stale-rules legacy content needs `--reingest` like any other
  rules migration.
- **Retained generations are never workspace (issue #405 R2):** a derived
  staging name that collides with a committed retained (non-live)
  generation allocates a suffixed candidate instead of reusing it, so
  rollback-by-republish preserves the retained points and manifest
  byte-identically. A forced rebuild whose derived name IS live takes the
  same suffixed-allocation route (issue #391 current packet), so the
  serving generation is never workspace either. At cutover, publication
  fingerprints `(gen_fp, corpus_fp)` are committed to the generation's metadata
  (`<collection>__completions`), allowing subsequent ordinary runs to recognize
  successful repair generations as steady state without allocating further staging
  generations (issue #391 Q418-R1). An existing generation lacking a publication receipt
  performs a one-time metadata write when verified as `already_live`; once the receipt
  exists, subsequent ordinary runs perform zero writes (corpus, markers, and metadata remain
  completely untouched). The resume path deliberately checks no
  manifest state:
  staging is cloned from live, so an interrupted clone carries a COMMITTED
  manifest indistinguishable from a finished build — resume safety comes
  from the sidecar's fingerprint binding, converge re-verification before
  any swap, and never building into the serving generation. An existing
  staging with no matching build record fails closed (remove it explicitly
  or resume the run that recorded it). Same-alias publishers must share the
  progress directory (already required for refresh lineage) so the target
  lock serializes them; different directories or hosts are operator error,
  guarded only by the pre-swap live recheck — there is no distributed lock.
  Lock filenames sanitize the alias charset: aliases differing only in
  sanitised-away characters share a lock (over-serialization, never
  concurrent publication).
- **Fingerprint-format upgrade (one-time, fail-closed):** the pre-#391
  fingerprint omitted the operator revision and other re-embed-required
  fields, so it cannot be trusted for skip eligibility. Existing
  collections therefore see old markers as unreachable once (`rp2:` ids
  change) and re-ingest on the first run — unchanged docs included; fail
  closed, never a silent pass. Alias-publish deployments publish the
  re-ingest as a new staging generation and swap when it verifies; the old
  physical retains its old data **and its own contract
  metadata** (`<old>__completions`), so an alias rollback selects matching
  metadata, not the new contract.
- **Rollback / GC (operator actions, never automatic):** the superseded
  physical and a safety snapshot are kept on every swap. Roll back by
  re-pointing the alias (any Qdrant client):
  `update_collection_aliases([DeleteAlias(alias), CreateAlias(prev, alias)])`.
  When confident, delete the old physical, its `<old>__completions`, and
  the safety snapshot (`delete_collection`, `delete_snapshot`). Verify
  counts/dim against the run log (`action: publish` carries
  alias/physical/previous/safety_snapshot/docs) before and after.
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
- `_DocLocks`: per-revision threading locks (retained, bounded by unique
  revisions) serialize same-revision check-delete-upsert sequences across
  the parallel streams — coexisting revisions under one `doc_id` take
  different locks and proceed concurrently.
- `_parse_one` traps everything and returns a plain `InventoryRecord(status="error")`
  (message capped at 500 chars, exception class name in `error_type`, doc id or filename
  stem, zero pages/chunks) plus a dummy parsed doc; the future-exception path uses an
  empty sha. One bad PDF never kills the run. Records must stay picklable across spawn
  IPC (no exception objects — unpicklable exceptions like `httpx2.HTTPStatusError` from
  contextual LLM calls would crash `ProcessPoolExecutor` across process boundaries).
- Result accounting: `skipped` + `upserted` both count as files-ok; only
  `upserted` adds upserted chunks. `empty` (zero-chunk doc, issue #359) and
  `error` count as failed and exit nonzero. Bulk mode applies to real runs only,
  restored in a `finally`. The summary logs files-ok/failed/chunks/parse and
  upsert seconds/pages-per-second/bulk/elapsed-ms and warns on failures;
  exit `1` iff any failure.
- Dry-run embeds nothing (parse + chunk only); used by ingest unit tests.
- Logs are one JSON object per line — ids and counts only, never PDF text or
  secrets; parse workers return records for the parent to log.

Contract tests: `tests/test_run_ingest.py` (`main`, `resolve_workers`,
`_DocLocks`, `_parse_one`), `tests/test_ingest_robustness.py`,
`tests/test_ingest_completion.py` (failure boundaries),
`tests/test_ingest_publish.py` (alias publication),
`tests/test_ingest_identity.py` (dedup/collision planning gate),
`tests/test_ingest_revisions.py` (revision coexistence/refresh/migration),
`testing.md` pickle round-trip.

<a id="identity-contract"></a>
## Identity contract and proof boundaries

**Status:** implemented with unit evidence for the named identity functions;
complete publication safety remains partial. **Authority:** #361, #362 and #391;
context/status clarification authorized by #397 (15 September 2026).
**Decision owners:** `identity.source_rev_key`, `ibm_pdf.resolve_doc_id`,
`chunk.make_chunks`, `completion.representation_fingerprint` and
`publish.staging_name_for`.

| Identity / input | Producer → persisted state → consumers | Lifetime / distinction |
|---|---|---|
| Printed document identity | Parser → payload `doc_id` → citation formatting, family filters, eval | A printed family key is not a destructive revision selector |
| Source revision | `source_rev_key` → chunk payload, inventory, completion → locks/deletes/refresh planning | Normalized labels and source content; mount location is not identity |
| Representation | `representation.build_manifest` and `compare_manifests` → fixed-ID contract and completion digest → ingest preflight, serving | `REEMBED_FIELDS` in code is the authoritative field policy; no second hash-field list here |
| Physical build/publication | `doc_generation_id`, publication fingerprints → physical collection, its completion collection, alias → reader gate | Deterministic naming binds inputs; it neither makes storage immutable nor serializes publishers |
| Query-only configuration | `compare_manifests` record-only classification → manifest/log evidence → query embedding/evaluation | Query-only drift can require evaluation without re-embedding stored documents |

**Preconditions/failures:** identity attestation is supplied by the model owner;
blank vLLM revision must fail even with force. Deduplication and collision checks
precede destructive work; ambiguous legacy lineage fails rather than deleting a
sibling. Revision-separated UUIDs prevent cross-revision collisions, but two
writers of the same revision can still target the same points.
**Evidence:** `tests/test_ingest_identity.py`, `tests/test_ingest_revisions.py`
(including the closure pins: exact product/version filter partition plus
family-citation round-trip over coexisting revisions, and an event-ordered
threaded lock test proving same-revision exclusion with unblocked coexisting
revisions), `tests/test_representation.py::test_fingerprint_revision_only_change_alters_identity`
and `test_fingerprint_rules_and_prefix_policy` pin identity behavior, and
`tests/test_integration_sim.py::test_361_inplace_snapshot_restore_keeps_coexisting_revisions`
proves coexistence, server-side revision scoping, and snapshot-restore
rollback against a real server. They do not prove corpus-wide coverage or
concurrent publication safety. #361 retains its
acceptance ownership; publication/lifetime gaps belong to #391.

<a id="publication-contract"></a>
## Completeness, publication and writer coordination

**Status: partially implemented.** **Authority:** #359/#391 and #405 R1/R2;
#397 documents the remaining gap, not a runtime fix. **Decision owner:**
`completion` verification, `run_ingest._commit_migration_representation`,
`publish.verify_searchable_coverage`, `publish.verify_all_complete`,
`run_ingest._run_publish`. Static inspection below was at
`d8d9ecb7a9a6f426529c715368b513926f6c5a96` (merge #408); the #391 current packet
re-audited `ff5ccba` and its commit-scope/legacy-attribution findings are
implemented with the tests named under Existing evidence.

**Required invariant:** all searchable points in a published generation are
attributable to verified generation coverage. A scan finding no stale completion
markers does **not** establish coverage of unmarked points. Per-walked-document
success does not establish coverage of retained, removed or unwalked data.

**Inputs and transitions:** source walk + wanted representation + prior physical
and inventory → resolve → prepare staging/metadata → ingest and verify documents
→ verify complete target and contract → publish alias → retain old target for
rollback. Producers are ingest/admin writers; persistent boundaries are data,
completion records, manifest, inventory and alias; consumers include serving,
readiness, retrieval, answer/chat/console, recovery tools and evaluation.

**Current behavior and limits:**

- `is_doc_complete` checks marker binding plus stored point counts/digests;
  vector/chunk length mismatches fail before load; zero chunks are explicit
  `empty`, never a successful document publication.
- Collection distribution (issue #360): the corpus and completion
  constructors carry the explicitly selected policy
  (`QDRANT_SHARD_NUMBER` / `QDRANT_REPLICATION_FACTOR` /
  `QDRANT_WRITE_CONSISTENCY_FACTOR`) to creation verbatim. The air-gap
  production default is the checked-in 6/3/2 tuple, and the loader validates
  the complete effective tuple after explicit caller > operator file >
  `overlays/openshift/collection-policy.env` precedence; a partial override
  inherits the remaining preset values and local/one-node lanes select 1/1/1
  explicitly. An existing collection
  whose configured values differ from the selected policy refuses before
  load — examined read-only, never recreated or mutated. Unreadable live
  values are unknown, not mismatches, at this compatibility check; the
  strict production verifier (`scripts/verify_placement.py`,
  [deploy policy](deploy.md#collection-policy)) refuses unknown placement
  and checks actual per-shard active copies for the corpus plus control
  collection. Publication additionally re-checks the exact staging
  candidate's configured policy for both collections immediately before
  cutover (`publish.verify_staging_distribution`, also applied to the
  already-live re-verify): a missing, unreadable, or mismatched value
  refuses with the alias untouched and names the snapshot-gated migration,
  never a policy downgrade. Per-shard ACTIVE-copy qualification of the
  candidate via direct-peer verification remains the operator step before
  relying on HA.
- Representation migration writes `pending`, then commits after no document
  failures, `stale_completion_markers` finds no differently stamped markers,
  and the read-only scope proof attributes every searchable point to a verified
  walked document of this run (`verify_searchable_coverage`; strict at commit —
  no legacy allowance). This detects marked residue AND unmarked data. An
  explicitly approved removal planned under the target lock is excused at
  commit and enforced gone before the swap.
- `verify_all_complete` checks the walked inventory, rejects a present pending
  contract, a missing/unreadable/drifted final manifest on a walked corpus,
  and any searchable point no verified walked generation accounts for —
  sourceless points only through verified approved legacy document digests
  (read-only residue audit; explicit `--retire-doc` removals are the only
  deletions and are applied before verification). In-place mode still never
  removes unwalked data.
- Distinct staging plus the alias update isolates ordinary cutover and forced
  repair under the coverage and writer assumptions below; the serving
  generation is never mutated in publish mode, and only first legacy-name
  conversion has a brief documented maintenance window.
- Corpus deletion is not automatic garbage collection. On a disposable synthetic
  regenerated corpus, use an isolated fresh target; the legacy delete-and-rebuild
  recipe is destructive and is not an instruction to delete a live collection.
  Real-corpus recovery follows [its runbook](local-real-corpus.md).

**Coordination/lifetime assumptions:** serialize the **entire** resolve/prepare/
ingest/verify/publish interval across every writer/admin actor to the same target.
Publish mode holds one target-identified advisory file lock
(`publish-<alias>.lock` beside the progress file) from staging resolution
through cutover; a second live publisher for the same target fails closed
(fcntl releases on process death, so a held lock means a live holder — locks
are never stolen). Same-alias runs must share the progress directory; a
pre-swap live recheck refuses a candidate overtaken by a lock-bypass writer
(different directory or host), without which the alias could swing back to
an older generation. The inner build keeps its own progress lock; `_DocLocks`
only serializes revisions inside one process. Supported operation requires
operator-serialized jobs, not a claim of distributed lock enforcement. No HA
claim follows from a single-node run.

**Maintenance and rollback:** in-place mode (`INGEST_ALIAS_PUBLISH=false`)
repairs the live collection directly, so operators must quiesce writers and
drain affected readers first; setting a manifest pending or waiting a TTL
alone does not drain in-flight requests. Alias-mode repair is a normal
distinct-generation publish: the old physical keeps serving until the
verified atomic swap, and is retained afterward. Preserve a restorable backup
and the old physical **and its own metadata**. Alias rollback still needs
compatible settings and reader revalidation. Retain old state until readers
have drained; GC is an explicit operator action, never automatic. See
[serving lifetime](agent.md#serving-contract)
and [release recovery](crc-release-verification.md).

**Existing evidence:** `tests/test_ingest_completion.py` checks per-document failure
boundaries; `tests/test_qdrant_io.py` checks corpus-constructor forwarding and
read-only distribution examination; completion-constructor policy checks live
beside the generation-scoping tests in `tests/test_ingest_completion.py`; `tests/test_ingest_publish.py` includes staging visibility, failed
swap recovery, pending/missing/drifted-manifest refusal, forced same-contract
repair as a distinct generation (no live mutation, resumable, reader-compatible
mid-build: `test_publish_force_same_contract_repairs_distinct_generation`,
`test_forced_repair_never_touches_live_during_build`,
`test_interrupted_same_contract_repair_resumes_recorded_build`,
`test_forced_repair_partial_corpus_refuses_and_preserves_live`) and rollback,
plus the R1/R2 lifecycle: partial-walk refusal with
preservation, explicit retirement with rollback history, fail-closed unknown
and contradictory retirements, read-only residue audit, same-target writer
serialization, overtaken-publisher refusal, same-staging resume, retained
generation immutability, and in-place lock scoping. The #391 current-packet
scope proof adds `tests/test_run_ingest.py::test_unmarked_searchable_point_blocks_migration_commit`
(empty walk over an unmarked old-rules point refuses and preserves it) and
`test_ingest_publish.py::test_walked_doc_unapproved_legacy_stray_refuses` /
`test_walked_doc_approved_legacy_stray_publishes` (content attribution, not a
printed-doc_id match), plus the migration-with-retirement pair
(`test_publish_migration_with_explicit_retire_single_run`,
`test_publish_migration_retire_unapproved_stray_refuses`). Warm-cache mutation and
serving-reader draining remain #391 acceptance counterexamples; retain
independent expected membership and real client projection semantics.

<a id="metadata-contract"></a>
## Representation metadata outcomes

**Status: partially implemented.** **Authority:** #362/#391; #397 status audit.
**Decision owner:** `representation.read_manifest_record`, its async counterpart,
`check_ingest_compatible`, `serving_outcome`, and the final publication check.
**Inputs/producers:** operator-attested wanted contract and ingest-written
manifest envelope → `<physical>__completions` → ingest, publication, reader gate.

| State / observation | Required decision and currently visible distinction |
|---|---|
| Explicitly empty/bootstrap | Establish no searchable data; readiness may allow bootstrap, requests still refuse |
| Compatible committed | Eligible under the coverage/lifetime preconditions, not proof of those preconditions |
| Record-only drift | Existing policy permits reads; record and evaluate query behavior |
| Pending | Refuse serving/ordinary skips/publication; a forced retry of the same bound alias build can verify completed document checkpoints, never promote merely on startup |
| Missing on a populated target | Must refuse certification; serving classifies legacy; final publication guard has the gap above |
| Corrupt | Must not authorize a populated target; decoder folds malformed records into absent/legacy |
| Unsupported schema/state | No dedicated unsupported outcome: a schema-version mismatch compares as re-embed drift; an unfamiliar envelope state is noncommitted/pending |
| Unreadable store | Must not authorize a populated target; sync manifest retrieval folds fetch errors into absent (existence-check errors can propagate), async serving reports unknown |
| Representation drift | Refuse until approved re-embedding migration; force never supplies missing model attestation |

The desired distinctions above are not new implemented enum values. Missing,
corrupt, unsupported and unreadable must stay separate in diagnosis even where
current helpers collapse them. No new schema or defaults are introduced here.
`begin_manifest` can retain an already committed same-contract record during
forced repair; pending is not a universal mutation barrier. Cached validation
has the [reader contract's limits](agent.md#serving-contract).

**Evidence:** `tests/test_representation_gate.py` covers empty, compatible, drift,
corrupt-as-legacy, pending and unreadable-serving paths; `tests/test_serving_gate.py`
checks physical metadata binding. These do not close the final-verifier or
whole-data coverage gaps under #391. Observability has a separate bounded fail-open
policy ([agent logging/tracing](agent.md)); do not copy it into required metadata.


### Narrow legacy staging recovery

For an unfinished snapshot-cloned migration with duplicate sourceless points,
the explicit `mainframe_rag.ingest.repair` plan/apply command can export and
remove only exact duplicates retained in the rollback collection after their
replacement completion and stored content verify. It refuses served staging,
changed plan/backups/inputs, ambiguous replacement attribution and new residue.
It takes the existing publisher/run locks on the actual shared progress path;
other writers must be stopped. It never edits controls, retires sources or
publishes. See the [local repair procedure](local-real-corpus.md#explicit-recovery-of-duplicate-legacy-staging-points)
for writer credentials, private backups, interrupted retries and the required
ordinary ingest afterward. This is an explicit maintenance operation, never an
automatic exception to the publication coverage gate.
