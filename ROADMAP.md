# qdrant-pdf-rag — SOTA Roadmap (Agent-Ready, v2)

> Each section below maps 1:1 to a GitHub issue (#75–#94). An agent picking up a task
> MUST read: the issue, this document, `AGENTS.md`, `docs/architecture.md`, and
> `docs/adr/0001-baseline-decisions.md` before writing code.

## Verified repo facts (audit of 2026-09-02)

These were verified against code and docs, not assumed. Agent tasks below reference them.

- **Parsing:** PyMuPDF is the baseline parser (ADR 0001). `.pdx`/`.idx` ignored.
- **Chunking:** Section-outline aware. `chunk.py` builds chunks per section with
  `heading_path`, threaded through `embed.py` → `qdrant_io.py` (payload) →
  `retrieve/query.py` → `scripts/eval_retrieval.py` (`expected_heading` matching).
  Point id = UUID5 of `{doc_id}|{heading_path}|{page_start}|{ordinal}`.
  NO code-atomic protection: nothing in `chunk.py` handles JCL/REXX blocks.
- **Retrieval:** Hybrid dense + BM25 (FastEmbed, baked weights, `bm25-weights.sha256`),
  local weighted RRF ([1,3] with identifiers, else [1,1], k=2) in `retrieve/query.py`.
  No reranker, no SPLADE, no ColBERT, no query rewriting.
- **Serving:** FastAPI. Routes `/healthz`, `/v1/search`, `/v1/answer` are sync `def`.
  Only lifespan + request-id middleware are async. No SSE/streaming anywhere.
  Embeddings via HTTP: `http.post(f"{settings.embed_base_url}/embeddings")`.
  LLM via `HttpxLLMClient` (sync httpx) in `agent/answer.py`.
- **Splunk:** Already in scope as *caller-supplied* context: `/v1/answer` accepts
  `splunk_context` (see `app.py`, `answer.py`, `tests/test_agent_api.py`). ADR 0001:
  "Splunk stays system of record (context in, not crawl)."
- **Eval gate (exists, not CI-wired):** `scripts/harness.py` gates on bootstrap CIs of
  paired per-query deltas vs mode-keyed baselines (hash/vllm). Stages:
  `harness_l1` (retrieval; must_not violations gated to zero in top-5),
  `harness_l2` (answers), `harness_l3` (perf: p95 regression + VRAM, Server-Timing).
  Support: `eval_retrieval.py`, `eval_answers.py`, `verify_golden.py`,
  `render_report.py`, `bootstrap_ci.py`, `qdrant_sim.py`, `loadtest.py`.
  Golden sets: `evals/golden.jsonl`, `evals/holdout.jsonl` (sha-pinned),
  `evals/expert_golden_seed.jsonl`; baselines in `evals/baseline*.json`.
  Re-freeze process documented in `scripts/build_golden_corpus.py`.
- **CI:** `.github/workflows/ci.yml` + mirrored `.gitlab-ci.yml` = binary-hygiene +
  pytest only. `bench.yml` benchmarks on push to main. `e2e.yml` = connected-path
  smoke test on lab OpenShift with synthetic PDFs. The eval harness runs in NEITHER.
- **Constraints (AGENTS.md / ADR 0001):** Qdrant 1.19.0 `*-unprivileged`, vendored Helm
  chart, no `helm repo add` on air-gap host. This repo does NOT install vLLM/LiteLLM/
  Splunk/GPU operators. Dense embed = other team's in-cluster vLLM (`VLLM_BASE_URL`).
  `EMBED_MODE=hash` is CI/dev only; prod refuses hash without `ALLOW_HASH_MODE=true`.
  Corpus never leaves the enterprise. GitHub repo is a public mirror imported AS-IS
  into air-gapped GitLab — **any CI change must be made in both** `ci.yml` **and**
  `.gitlab-ci.yml`. Superseding an ADR 0001 decision requires a new ADR +
  `architecture.md` update in the same PR.

## Global rules for every PR

1. **Eval gate is law.** Every PR touching ingest, retrieval, ranking, or prompting MUST
   pass `scripts/harness.py` (L1–L3 as applicable) and include the rendered delta table
   (`scripts/render_report.py`) in the PR description. No regression beyond harness
   epsilon without explicit sign-off.
2. **Feature flags, not rewrites.** New capabilities land behind a `Settings` flag
   (`src/mainframe_rag/config.py`), default-off where behavior changes.
3. **Air-gap vendoring.** Any new model/binary: pinned version + sha256 + offline load
   path, following the `images.txt` / `bm25-weights.sha256` pattern. No runtime downloads.
4. **One PR = one capability.** Dependencies listed per PR. Check the linked issue for
   status before starting.

---

## P0 — Foundation & highest ROI

### PR-01 (issue #75): Wire the eval gate into CI + close metric gaps
- **Why:** The harness exists and is statistically sound, but nothing enforces it on PRs.
  Every PR below depends on automatic enforcement.
- **Scope:** `.github/workflows/ci.yml`, `.gitlab-ci.yml`, `Makefile`,
  `scripts/render_report.py`
- **Implementation:**
  1. Add a `gate-l1` job running the retrieval harness in hash mode against
     `scripts/qdrant_sim.py` (fully CPU, no GPU runner needed). Command shape already
     documented in `scripts/eval_retrieval.py` docstring.
  2. Mirror the job in `.gitlab-ci.yml` (mirror parity is mandatory).
  3. Decide the GPU story for L2/L3 (self-hosted runner or nightly schedule); document
     in `docs/install_and_ops.md`.
  4. Post the rendered delta table as a PR comment (GitHub) / MR note (GitLab).
  5. Audit metric coverage: if recall@k / MRR / nDCG are missing from
     `eval_retrieval.py` per-query metrics, add them there — do NOT build a parallel harness.
- **Tests:** extend `tests/test_harness_l1.py` for any new metric.
- **Acceptance:** A PR that degrades retrieval fails a required check automatically;
  delta table renders without manual steps.

### PR-02 (issue #76): Cross-encoder reranking
- **Why:** RRF is fusion, not scoring. Largest single retrieval-quality win.
- **Scope:** new `src/mainframe_rag/retrieve/rerank.py`, `retrieve/query.py`, `config.py`
- **Implementation:**
  1. Vendor `bge-reranker-v2-m3` (pinned + sha256 + offline load).
  2. After existing local RRF (keep weights/contract — see `query.py` docstring), take
     fused top-50, score with the cross-encoder, return top-k (config, default 8).
  3. Batch scoring; keep added latency <300ms p95 on target GPU (L3 gate watches this).
  4. Add reranker score to the response payload (PR-12 consumes it for confidence).
  5. Flag: `RERANK_ENABLED` (default off).
- **Tests:** new `tests/test_rerank.py` (ordering, flag off = byte-identical old path);
  extend `tests/test_query_filters.py` if filters interact.
- **Gate:** L1 must not regress; expect nDCG/recall improvement on golden.
- **Depends on:** #75.

### PR-03 (issue #77): Async stack + SSE streaming
- **Why:** Routes are sync `def`; LLM/embed clients are sync httpx. No streaming → TTFT
  equals full generation time.
- **Scope:** `agent/app.py`, `agent/answer.py`, `ingest/qdrant_io.py`, `retrieve/query.py`
- **Implementation:**
  1. Swap `QdrantClient` → `AsyncQdrantClient`; `httpx.Client` → `httpx.AsyncClient`
     (including `HttpxLLMClient` and the `/embeddings` call in `app.py`).
  2. Make route handlers `async def`.
  3. `/v1/answer`: `stream=true` → `StreamingResponse` (`text/event-stream`): token
     delta events, then one final event with citations + retrieval metadata.
     Default (`stream=false`) unchanged — response contract is load-bearing
     (see `scripts/eval_answers.py` "deliberate non-features").
  4. Emit TTFT via the existing Server-Timing mechanism (L3 reads it).
- **Tests:** extend `tests/test_agent_api.py` (both modes, identical citations);
  `tests/test_integration_sim.py` still green.
- **Gate:** L3 perf must not regress; add TTFT assertion.
- **Depends on:** none. Land early — #80 and #83 build on it.

### PR-04 (issue #78): Contextual retrieval (chunk context prefixes)
- **Why:** Anthropic-style contextual prefixes cut retrieval failures; today bare chunks
  are embedded.
- **Scope:** `ingest/chunk.py`, `ingest/embed.py`, `ingest/run_ingest.py`
- **Implementation:**
  1. At ingest, generate a 1–2 sentence situating context per chunk via the reasoning
     LLM: manual title + `heading_path` (already available!) + chunk gist.
  2. Cache contexts keyed by (doc sha, chunk id) — chunk ids are deterministic UUID5,
     so unchanged docs must produce zero LLM calls on re-ingest (manifest.py tracks docs).
  3. Embed `context + chunk_text`; keep raw chunk text in payload for display/citation.
  4. Version the context-generation prompt template in-repo.
  5. Flag: `CONTEXTUAL_EMBED_ENABLED`; re-ingest required (see manifest versioning).
- **Tests:** extend `tests/test_chunk_ibm_shape.py` / `test_qdrant_io.py` for the new
  payload field; cache-hit test with unchanged fixture docs.
- **Gate:** L1 recall must improve on golden; no regression on holdout.
- **Depends on:** #75.

### PR-05 (issue #79): Code-atomic chunking for JCL/REXX (rescoped)
- **Why:** Section-outline chunking with `heading_path` ALREADY exists — do not rebuild it.
  The real gap: `_split_blocks` works on paragraphs and can split positional code
  (JCL statements, col-72 continuations, REXX blocks) across chunks.
- **Scope:** `ingest/chunk.py`, `ingest/classify.py`, `tests/fixtures/`
- **Implementation:**
  1. Add a code-region detector: JCL (`//` cards, `//*` comments, continuation col 72),
     REXX (`/* */` comments, `/* REXX */` header), and monospaced/console blocks.
  2. Treat detected regions as atomic units inside `_split_blocks`: never split;
     on overflow, split at statement boundaries only (continuation-aware).
  3. Verify `heading_path` is filterable/returned in the Qdrant payload (not only baked
     into the point UUID); if missing from payload schema in `qdrant_io.py`, add it.
  4. Keep the UUID5 point-id contract unchanged (id stability = incremental ingest).
- **Tests:** NEW fixtures — JCL with continuations/inline comments, REXX with block
  comments. Assert: no statement ever split. Extend `tests/test_chunk_ibm_shape.py`.
- **Gate:** L1 must not regress on golden (esp. exact-code query class).
- **Depends on:** none.

### PR-06 (issue #80): vLLM prefix caching + prompt ordering + injection hardening
- **Why:** Free GPU via KV-cache reuse; current prompt assembly (`answer.py`) must be
  ordered for it.
- **Scope:** `agent/answer.py`, deploy manifests (vLLM launch args are NOT in this repo —
  coordinate with the embeddings/serving team; document required flag)
- **Implementation:**
  1. Reorder prompt assembly: [system + instructions] → [retrieved context, stable
     ordering] → [splunk_context if present] → [user question]. Remove anything volatile
     (timestamps, request ids) from prompt text. (`answer.py` already builds parts:
     sysplex context, splunk_context, chunks — reorder and stabilize.)
  2. Document `--enable-prefix-caching` as a requirement for the vLLM deployment in
     `docs/install_and_ops.md` (this repo must not install vLLM — AGENTS.md).
  3. Injection hardening: wrap retrieved chunks and `splunk_context` in delimited,
     instruction-isolated blocks; system prompt demotes retrieved content to data.
- **Tests:** prompt-fixture test asserting block order; injection fixture
  ("ignore previous instructions" inside a chunk / splunk_context) does not alter output.
- **Gate:** L2 answer eval must not regress; measure cache-hit rate in bench.
- **Depends on:** #77.

---

## P1 — Quality & operability

### PR-07 (issue #81): Parent-child (small-to-big) retrieval
- **Scope:** `ingest/chunk.py`, `ingest/qdrant_io.py`, `retrieve/query.py`
- **Implementation:** Embed child chunks (~128–256 tokens); retrieve children, return
  parent (~512–1024 tokens) to the LLM; group via payload `parent_id`; dedupe multiple
  children → same parent. Keep UUID5 id scheme (extend the f-string, don't replace it).
- **Tests:** dedupe test; payload round-trip in `test_qdrant_io.py`.
- **Gate:** L1 + L2 improvement vs PR-04 baseline.
- **Depends on:** #78, #79.

### PR-08 (issue #82): Query understanding — acronym expansion + HyDE (gated)
- **Scope:** new `src/mainframe_rag/retrieve/rewrite.py`, `agent/answer.py`
- **Implementation:** Curated versioned acronym glossary (JSON) for deterministic
  expansion; HyDE / step-back behind independent flags; heuristic bypass when the query
  is mostly identifiers/error codes (reuse `regexes.py` — the [1,3] identifier weighting
  in `query.py` shows these patterns already exist). Eval each technique separately.
- **Gate:** per-technique L1 deltas; identifier-heavy query class must not regress.
- **Depends on:** #75.

### PR-09 (issue #83): OpenTelemetry tracing
- **Scope:** `agent/`, `retrieve/`, `deploy/` (collector config)
- **Implementation:** Spans: tokenize → embed (HTTP) → qdrant prefetch → RRF → rerank →
  LLM (TTFT, tokens/s). Attrs: scores, rerank rank deltas, doc ids, cache hits. OTLP
  export; vendored self-hosted Jaeger or Arize Phoenix overlay. Reuse the existing
  request-id middleware as trace-id source.
- **Acceptance:** one request = one trace with all stages; injected 5s spike attributable
  to a stage from the trace alone.
- **Depends on:** #77.

### PR-10 (issue #84): LLM-as-a-judge eval stage
- **Scope:** `scripts/` (new `judge_answers.py`), `evals/`, both CI files
- **Implementation:** Local reasoning model judges faithfulness (claims supported by cited
  chunks), relevance, citation precision. Deterministic harness checks still gate; judge
  scores reported with thresholds + borderline human-review queue. Fully offline.
  Integrate as harness L4, don't fork the harness.
- **Tests:** seeded hallucinated answer fails faithfulness; `tests/test_harness_*` pattern.
- **Depends on:** #75.

### PR-11 (issue #85): Layout-aware parsing pilot (Marker)
- **Why:** PyMuPDF text extraction mangles IBM manual tables/diagrams (real concern) —
  but "fatal" is unproven. Measure, then decide.
- **Scope:** new `ingest/layout.py` (alternative front-end feeding `chunk.py`), vendored
  Marker models, `deploy/` GPU notes
- **Implementation:** Marker → structured Markdown (tables preserved) → existing
  section-outline chunking. Build a golden subset of table/diagram-heavy questions
  (process in `scripts/build_golden_corpus.py`). A/B: PyMuPDF vs Marker. Flag-gated;
  adopt only if the delta justifies ingest cost.
- **Acceptance:** A/B report in PR; adopt/reject ADR recorded in `docs/adr/`.
- **Depends on:** #75, #79.

### PR-12 (issue #86): Corrective retrieval + abstention (CRAG-style)
- **Scope:** `agent/answer.py`, `retrieve/query.py`
- **Implementation:** Confidence from reranker score distribution (#76). Low → one
  rewrite + re-search (max 1 retry, hard latency budget — L3 watches p95). Still low →
  abstain: "insufficient context in indexed manuals" + closest citations. Add
  unanswerable cases to the golden set (follow re-freeze process).
  NOTE: `eval_answers.py` documents "single-shot by contract (issue #20)" — this PR
  changes that contract; coordinate with issue #20 and update the eval's non-features.
- **Gate:** seeded unanswerable → abstention, not hallucination.
- **Depends on:** #76, #82.

### PR-13 (issue #87): Prompt-injection & retrieved-content hygiene
- **Scope:** `agent/answer.py`, `ingest/` sanitization, `tests/security/`
- **Implementation:** Anything not covered by #80: strip/neutralize control sequences in
  extracted PDF text at ingest; size-cap assembled context (respect
  `max_context_chars`); injection fixture battery (instruction overrides, fake system
  messages, delimiter escapes — including via `splunk_context`).
- **Acceptance:** security fixture suite green in both CIs.
- **Depends on:** #80.

---

## P2 — Expansion

### PR-14 (issue #88): SPLADE sparse leg (measured)
- **Scope:** `ingest/embed.py`, `retrieve/query.py`, vendored SPLADE model
- **Implementation:** Add SPLADE sparse vectors as a THIRD prefetch leg alongside
  FastEmbed BM25; compare BM25 vs SPLADE vs both under the existing weighted RRF.
  Keep the winner — existing identifier-weighted BM25 is strong for exact codes.
- **Gate:** three-way L1 comparison; decision recorded in `docs/adr/`.
- **Depends on:** #75, #76.

### PR-15 (issue #89): Embedding improvement track
- **Scope:** `evals/expert_golden_seed.jsonl` → training pairs; `ingest/embed.py`,
  `manifest.py`
- **Implementation:** Hard-negative mining from failed harness runs; fine-tune or evaluate
  alternates. Embedding model version recorded in `manifest.py` with re-embed detection.
  NOTE: dense embeddings are served by the other team's vLLM — a model change is a
  cross-team deliverable; this repo's part is versioning, detection, and eval.
  If an MRL-capable model is adopted, Matryoshka truncation becomes available;
  otherwise skip Matryoshka, use #92.
- **Depends on:** #75, #84.

### PR-16 (issue #90): ADR 0002 — agent-initiated live-state retrieval (rescoped)
- **Why:** ADR 0001 decided "Splunk: context in, not crawl" and `/v1/answer` already
  accepts caller-supplied `splunk_context`. Letting the AGENT fetch live state
  supersedes ADR 0001 → per repo policy this REQUIRES a new ADR + `architecture.md`
  update in the same PR. No code in this PR.
- **Scope:** `docs/adr/0002-*.md`, `docs/architecture.md`
- **Implementation:** ADR covering: routing taxonomy (static-manual vs live-state vs
  hybrid questions); read-only Splunk REST/SPL connector interface; auth; audit logging;
  dry-run mode; fallback when telemetry is unreachable; explicit "no job control, no
  commands" safety statement; migration from caller-supplied `splunk_context`.
- **Acceptance:** ADR approved before #91 is scheduled.
- **Depends on:** none (document only).

### PR-17 (issue #91): Read-only ops tool-calling
- **Scope:** new `agent/tools/`, feature-flagged, allowlisted tools only
- **Implementation:** Function-calling against the #90 connector interface; every call
  audit-logged with request id; disabled by default; tool results wrapped as untrusted
  data per #87. Must satisfy ADR 0002 exactly.
- **Gate:** tool calls visible in OTel traces (#83) and audit log; non-allowlisted calls
  rejected; flag off = zero tool surface.
- **Depends on:** #90, #83, #87.

---

## P3 — Optional / research (only on measured gaps)

### PR-18 (issue #92): Qdrant RAM reduction
- Scalar/product quantization + on-disk payload on the dense collection; measure recall
  delta + RAM/latency improvement via harness. Prefer over Matryoshka unless #89 adopted
  an MRL model. Qdrant 1.19.0 — verify quantization API against that version.
- **Depends on:** #75.

### PR-19 (issue #93): ColBERT late-interaction pilot
- ONLY if exact-code retrieval still fails after #76 + #88. Qdrant multivector + MaxSim
  (verify 1.19.0 support); expect ~10x vector RAM — quantify against #92 first.
- **Depends on:** #76, #88, #92.

### PR-20 (issue #94): Cross-reference graph ("GraphRAG-lite")
- ONLY if multi-hop questions still fail after #82 + #86. Extract entity/see-also/syntax
  cross-references at ingest (deterministic, no LLM community summaries); 1-hop expansion
  at retrieval. Full GraphRAG out of scope unless this proves the direction.
- **Depends on:** measured failure after #82, #86.

---

## Explicitly rejected from the original review

- **Full GraphRAG at ingest:** worst cost/benefit for a static-manual corpus; the
  motivating example (JCL→REXX→VSAM→RACF) describes the *live environment* — that's
  #90/#91's problem, not a KG's.
- **ColBERT as default:** multi-vector RAM contradicts the same review's RAM concern;
  gated behind measured failure (#93).
- **Matryoshka standalone:** meaningless without an MRL embedding model; folded into
  #89/#92.
- **"Text extraction is a fatal flaw":** overstated; measured pilot (#85).
- **"No hierarchical chunking":** wrong — section-outline chunking with `heading_path`
  exists; the gap is code-atomic blocks (#79) and parent-child sizing (#81).
- **"No eval gate":** wrong — `scripts/harness*.py` already implements bootstrap-gated
  regression checks; #75 is CI enforcement, not greenfield.

---

Tracking: issues #75–#94 (PR-01 → #75 … PR-20 → #94).
