# Expert Mainframe Agent — Design and Architecture Guide

**Status:** implementation-ready (source of truth)  
**Audience:** coding agents, platform architects, and operators  
**Constraint:** public GitHub for *code and cluster recipes*; air-gapped OpenShift for *runtime, corpus, embeddings*  
**Operations Guide:** see **[docs/install_and_ops.md](install_and_ops.md)** for step-by-step setup and operational runbooks.  
**Non-goal:** republishing IBM / Broadcom / BMC / Precisely manuals  

---

## 1. Mission

Build a **citation-first expert mainframe agent** that answers operational questions from a ~100 GB born-digital PDF corpus (mostly IBM, plus Broadcom/CA, BMC, Precisely) running on an **air-gapped OpenShift** cluster owned by another team.

| Layer | System | Job |
|---|---|---|
| Live state | Splunk (existing) | Events, jobs, messages *now* |
| Knowledge | Qdrant (self-hosted) | Manuals, precedent, "what does this mean / how is this supposed to work" |
| Reasoning | Internal vLLM / LiteLLM (platform team) | Thinking model for citation + solution / script generation |
| Embeddings | Internal vLLM stack | Dense vectors only; OpenAI-compatible endpoint |

The agent returns **answers grounded strictly in citations** (doc number, title, heading path, printed page label) and, when asked, JCL/REXX/operator steps verified against those citations.

---

## 2. System Context & Boundaries

```
                    ┌─────────────────────────────────────────┐
  Connected LAN     │  Public GitHub  (this repo)             │
                    │  chart, Makefile, ingest, retrieval     │
                    └───────────────┬─────────────────────────┘
                                    │ sneaker-net bundle
                                    ▼
┌──────────────────────────────────────────────────────────────────┐
│ Air-gapped OpenShift Cluster                                     │
│                                                                  │
│  ┌────────────┐  gRPC/HTTP   ┌─────────────┐  OpenAI-compat     │
│  │ Ingest Job │─────────────▶│   Qdrant    │◀──── Agent API     │
│  │ (One-Shot) │  upsert      │  3 replicas │                    │
│  └─────▲──────┘              └──────▲──────┘                    │
│        │ PDF on PVC / NFS RO        │ query (gRPC/HTTP)         │
│  ┌─────┴──────┐              ┌──────┴──────┐                    │
│  │ Corpus PVC │              │ Agent svc   │                    │
│  │  ~100 GB   │              │ FastAPI     │                    │
│  └────────────┘              └──────┬──────┘                    │
│                                     │                           │
│                    ┌────────────────┼─────────────┐             │
│                    ▼                ▼             ▼             │
│              vLLM embeddings   LiteLLM/vLLM    Splunk           │
│              (dense only)      reasoning LLM   (REST/SPL)       │
└──────────────────────────────────────────────────────────────────┘
```

### 2.1 Trust and Legal Boundaries
- **Zero Internet Access:** Runtime is completely disconnected. All container images, wheelhouses, and BM25 weights are mirrored into the enterprise.
- **Corpus Protection:** Real PDFs, manual text, Qdrant snapshots, and customer JCL are **never** committed to Git. Public GitHub hosts code, tests against synthetic PDFs, and deployment recipes only.

---

## 3. Target Infrastructure & Packaging

### 3.1 Workloads & Security Context

| Workload | Kind | Replicas | Spec |
|---|---|---|---|
| `qdrant` | StatefulSet (vendored chart) | 3 | Cluster mode, P2P 6335 (TLS off), HTTP 6333, gRPC 6334, `restricted-v2` SCC |
| `rag-agent` | Deployment | 2 | FastAPI, unprivileged, no GPU |
| `rag-ingest` | One-Shot Job | 1 | High CPU, worker pool, RWO scratch |
| `jaeger` | Deployment (optional) | 1 | Jaeger v2 all-in-one, Badger RWO block PVC, opt-in tracing backend |
| `bm25-weights` | Baked in images | — | FastEmbed `Qdrant/bm25`; no runtime download |

- **Storage Constraint:** Qdrant persistent data volume **must** be RWO Block storage (NFS and object storage are refused). Ingest work volume is also RWO Block. The corpus volume may be mounted as read-only NFS or PVC.
- **Networking:** ClusterIP services only. No public Route to Qdrant. Agent Route is optional (`AGENT_ROUTE=true`). Inter-node Qdrant gossip is plaintext on the CNI (`config.cluster.p2p.enable_tls: false` in `values.yaml`) because cluster certificates are not mounted.

### 3.2 Canonical Deployment Standard Across Environments

The 5-stage pipeline (`airgap-pack` -> `airgap-load` -> `airgap-deploy` -> `airgap-ingest` -> `airgap-smoke`, orchestratable via `make airgap-pipeline` with pre-flight safety via `make airgap-validate`) is the **canonical deployment standard across the entire project**:
1. **Production (Air-Gapped OpenShift):** Full 3-replica Qdrant cluster, internal enterprise registry, `restricted-v2` SCC, cluster vLLM endpoints, sneakernet tarball verification.
2. **Local Cluster Testing (Kind + Local Registry):** Local single-node Kind cluster using a local container registry (`localhost:5000` / `airgap-registry:5000`) and 1-replica overrides (`QDRANT_EXTRA_VALUES`). Runs the exact same packaging scripts, image archives, Helm chart, and Kustomize overlays, with adapted local sizing and SCC.
3. **Continuous Integration (CI):** Mandatory `make airgap-dryrun` on every PR validating manifest rendering, variable quoting, and fail-closed placeholder rules without a cluster; `airgap-rehearsal` executes the end-to-end pipeline on `main`.

This architectural standard ensures local cluster testing exercises the real production packaging and deployment artifacts, avoiding custom or divergent test manifests.

---

## 4. Data Processing & Retrieval Architecture

### 4.1 Document Ingest & Chunking

1. **PDF Discovery & Parsing:** PyMuPDF extracts metadata, table of contents (bookmarks), printed page labels (`page.get_label()`), and message IDs (`XXXnnnY`).
2. **Chrome Stripping:** Repeated header/footer lines appearing across $\ge 35\%$ of sampled pages in documents $\ge 8$ pages are stripped.
3. **Chunk Construction:** Sections partitioned by outline hierarchy. Sections $> 3500$ characters are split on blank lines with a 400-character overlap (`SECTION_MAX_CHARS = 3500`). This ensures dense tables, character code matrices (e.g. AFP fonts), and message documentation never exceed the 4,096-token context limit of dense embedding models.
4. **Multiprocessing Worker IPC Isolation:** Ingest worker processes trap exceptions locally inside `_parse_one` and serialize plain-data `InventoryRecord(status="error")` payloads, preventing unpicklable exception instances (such as `httpx2.HTTPStatusError` with attached response/request references) from crashing the `ProcessPoolExecutor`.
5. **Point ID Generation:** UUID5 derived from document and chunk keys (guaranteeing deterministic, idempotency-safe IDs without invalid hex strings).
6. **Payload Slimming:** Points store only essential query and citation attributes (`doc_id`, `title`, `heading_path`, `page_label`, `chunk_type`, `product`, `version`, `message_ids`, `text`). Redundant `embed_text` is omitted from storage.

### 4.2 Hybrid Embeddings & Collection Configuration

- **Dense Embeddings:** Ingest and query embed via internal vLLM at `POST ${EMBED_BASE_URL}/embeddings` (supporting arbitrary embedding dimensions, e.g. 1024-dim `Qwen3-Embedding-0.6B` or 768-dim models). `DENSE_DIM` is a fail-fast setting in vLLM mode: the agent and ingest validate it before any collection or embed call, and collection creation verifies the stored dimension matches. Query embeddings prepend the asymmetric query prefix (`Settings.dense_query_prefix`) on the dense query vector only; document chunks and the CI/dev hash embedder stay raw text.
- **Sparse BM25 Embeddings:** Computed in-process via FastEmbed using pre-baked `Qdrant/bm25` weights.
- **Collection Configuration (`mainframe_manuals`):**
  - Dense: `${DENSE_DIM}` dimensions, Cosine distance, on-disk HNSW ($M=16, ef\_construct=128$), int8 scalar quantization in RAM. Test runners automatically recreate collections if vector dimensions differ between test runs.
  - Sparse: `modifier=idf`, on-disk storage.
  - Payload indexes: `doc_id`, `product`, `version`, `vendor`, `chunk_type`, `message_ids`, `members`, `sha256` (keyword indexes).
- **Embed window budget:** the worst-case embedded string (chunk header + a `SECTION_MAX_CHARS = 3500` body carrying the `SPLIT_OVERLAP_CHARS = 400` split seed) is pinned by `tests/test_embed_budget.py`; local embed servers keep `--max-model-len 4096` (a 2048 window was rejected by tokenizer sweep — the worst case measures ~2,043 tokens at ~2.0 chars/token on syntax-dense text).

### 4.3 Hybrid Retrieval: Batched Prefetch, RRF Fusion, Rerank & Diversification

Retrieval executes the filtered dense and BM25 prefetches **concurrently in a single batched HTTP call** (`query_batch_points`, falling back to sequential `query_points` for clients without batch support). Async handlers run the same core through `async_search`, with the sync embed (`dense_query`/`sparse`) and cross-encoder (`rerank_candidates`) legs offloaded via `asyncio.to_thread` so a slow embed or rerank call never blocks the event loop:

```
User / Splunk Query
   ├── Query Classifier: Identifier (message ID / doc ID) vs Natural Language
   │
   ├── Embed leg (asyncio.to_thread): dense query vector + FastEmbed BM25 indices
   │
   ├── Batched prefetch — one HTTP round trip (limit 40 each; 50 when rerank is enabled):
   │     ├── Dense ANN query (filtered, "dense" vector)
   │     └── Sparse BM25 query (filtered, "bm25" vector)
   │
   ├── Payload Projection: Fetch only RETRIEVE_PAYLOAD_FIELDS
   │
   ├── Local Reciprocal Rank Fusion (RRF):
   │     k = 2
   │     Weights: [1.0, 3.0] (Dense, BM25) for Identifiers
   │     Weights: [1.0, 1.0] for Natural Language
   │
   ├── Optional cross-encoder rerank (default OFF — `rerank_enabled=False`):
   │     fused top-`rerank_candidates` (50) scored by `bge-reranker-v2-m3`
   │     via `HttpReranker` (vLLM /v1/score with /rerank fallback), batched
   │     by `rerank_batch_size` (32) under `rerank_timeout_s`
   │
   └── Hit diversification → Top-K Ranked Hits with Strict Citation Formatting:
         max 1 chunk per page, max 3 per doc, 3-phase backfill
```

`search()` (sync, used by tooling/eval) and `async_search()` (agent hot path) are contractually identical on identical fakes — a drift-guard test pins the invariant.

### 4.4 Reasoning LLM Prompt Construction, Complexity Modulation & Citation Grounding Contract

The agent enforces strict grounding guarantees and adaptive reasoning depth before returning answers to clients:

1. **Query Complexity Classification (`classify_query_complexity`):**
   Incoming inquiries are classified into two operational tiers:
   - **Simple Lookups:** Single message code queries (e.g. `IEA500I`), return code lookups, or short factual definitions.
   - **Complex Operational Inquiries:** Multi-step diagnostics, failure/abend troubleshooting (e.g. journal overflow, abend S0C4), configuration procedures (e.g. LFAREA 1M/2G page frames), and comparative memory tuning (e.g. DFSORT HIPRMAX vs MOSIZE).
   - *Design Rationale:* Factoid questions need fast, accurate answers (~4–7s) without wasting compute. Diagnostic and configuration inquiries demand deep internal thinking (~14–20s, >1,000 reasoning tokens) to analyze interacting subsystems, verify syntax, and structure recovery procedures.

2. **Adaptive Context Length Budgeting (`prompt_max_context_chars_complex`):**
   - **Context Truncation Vulnerability:** Reasoning models running on a 4,096-token maximum context window (`max_model_len=4096`) are vulnerable to context exhaustion. A default 8,000-character prompt context consumes ~2,400 prompt tokens, leaving only ~1,600 tokens total for *both* reasoning thinking tokens and generated response content. When the model deliberated deeply (>1,000 reasoning tokens), generation hit `Finish: length`, resulting in answers truncated mid-sentence and omitted `Citations:` sections.
   - **Solution:** For complex queries, prompt manual excerpts are capped at 4,500 characters (`Settings.prompt_max_context_chars_complex = 4500`). This preserves ~1,200 tokens for the prompt, reserving **~2,600 tokens of headroom** exclusively for thinking tokens and comprehensive answer text, completely eliminating truncation faults (`Finish: stop` guaranteed).
   - **Tokenizer discipline:** budget planning uses the in-process estimator (zero RPCs); the packed prompt is verified **once** per answer via the vLLM `/tokenize` endpoint at the server *origin* (`/v1` stripped). First `/tokenize` failure logs one warning and pins the in-process estimator for the life of the instance — never a silent per-call fallback, never per-chunk tokenize RPCs.

3. **Reasoning Protocol & Engine Control:**
   - **System Prompt Extension (`SYSTEM_PROMPT_COMPLEX_EXTENSION`):** Injected dynamically on complex queries. Instructs the reasoning model to conduct multi-phase internal deliberation: problem decomposition, cross-examining manual excerpts for parameters and return codes, constructing verified JCL/operator commands in fenced blocks, and auditing claims against cited manuals.
   - **Engine Controls:** Dispatches `reasoning_effort="high"` for complex queries and `reasoning_effort="low"` for simple queries. Pins `temperature=0.2` for grounded, deterministic reasoning.

4. **Few-Shot Citation Injection:**
   The prompt dynamically includes a concrete few-shot example using `hits[0].cite` in the instructions to enforce uniform formatting from both large reasoning models and quantized edge models (e.g. Gemma 4 INT4 QAT).

5. **Two-Pass Citation Resolution:**
   - **Primary Pass (Explicit Block):** Looks for a terminal `Citations:` section. Each listed citation is normalized and matched against the allowed search hit citations (`allowed_citations = {h.cite for h in hits}`).
   - **Fallback Pass (Bracketed Index Resolution):** If no explicit `Citations:` block is present, the parser scans for bracketed number references `\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]` (matching prompt tokens like `[1]`, `[2]`, `[1, 2]`) and resolves them to `ordered_cites[index - 1]`.
   - **Parenthesis Immunity:** Parentheses `(...)` are deliberately excluded from inference to avoid false positives on standard mainframe technical notation such as `z/OS (3.1)`, `SYS1.PARMLIB(IEASYS00)`, `(2)`, or `APARs (1, 2)`.

6. **Body Stripping & Verification:**
   Any hallucinated citation lines that match the citation regex but are not in `allowed_citations` are stripped from the response text before transmission. Mid-sentence narrative text mentioning document IDs is preserved under the standalone-line rule.

### 4.5 Outbound HTTP, Agent Lifespan & API Contracts

The agent is async end to end: all routes are `async def` on `AsyncQdrantClient` + `httpx2.AsyncClient`, so slow LLM/Qdrant legs never exhaust a threadpool. Lifespan owns every client and closes what it opens:
- **Async pool:** `httpx2.AsyncClient` with bounded keepalive/connection limits and connect retries — used by `/healthz` probes (pooled client only; no blocking sync fallback on the event loop).
- **Sync retrieval-leg pool:** a bounded `httpx2.Client` passed to the embedder, tokenizer, and reranker builders; their sync protocol calls execute inside `asyncio.to_thread`. Closed at shutdown.
- **Reasoning answer calls (`/v1/answer`):** single-shot with a 300s timeout on the LLM client's own pool; connection-level retries are explicitly disabled (`retries=0`) — answers are not idempotent and a retry would re-ask a model that may already be thinking.
- **Streaming:** `/v1/answer?stream=true` (or body `stream: true`) returns `text/event-stream`: `event: token` deltas, then exactly one terminal `event: final` carrying the verified answer, citations, script, hits, query kind, `ttft_ms`, and token usage. The final schema is identical on the empty-hits path. A mid-stream failure emits `event: error` and ends **without** `final` — clients must treat stream-end-without-final as failure. Non-streaming JSON remains the default. Server-side reasoning SSE is toggled by `LLM_STREAM` (default off; `make run-agent` enables it); TTFT is measured on the first content token and surfaced both in the `final` event and as `Server-Timing: ttft;dur=...` on the JSON path.
- **Error Contract:** Standard JSON error envelopes (`{"code": "...", "message": "..."}`). Internal exceptions and upstream response bodies are never leaked to clients; registered handlers pin 404/405/422/500 shapes. Retrieval failures read `502 upstream_error / "retrieval failed"`, LLM failures `502 upstream_error / "answer failed"`; prompt-construction failures are local faults and map to 500 `internal` — never mislabeled as upstream.
- **Distributed tracing (issue #83):** OTel spans, default OFF — the agent only exports when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (bare collector origin; OTLP/HTTP, the exporter appends `/v1/traces`; Jaeger v2 all-in-one is the reference backend, Phoenix-compatible since both speak OTLP). One request renders as one trace: `v1.search`/`v1.answer` root → `retrieve.search` → `embed` → `prefetch` → `rrf` → `rerank` (or `rerank_bypass_reason=trap|identifier` on the parent) → `prompt.build` → `llm.chat` (TTFT, token usage, finish_reason). The SSE stream holds the root span open until the terminal `final`/`error` event so the LLM leg stays a child of the same trace. Export is bounded (queue + timeout) and fail-open: collector outages log and drop, never fail a request. Span attributes mirror the log contract (ids, counts, scores, timings) with one deliberate exception: the bounded query text is carried on request spans for debugging — PDF/manual text and secrets never enter spans, and the never-log-query-text rule for JSON logs is unchanged.

---

## 5. Evaluation, Benchmarking & Tooling

### 5.1 Retrieval Accuracy Gates (`evals/`)
- **Golden Dataset:** `evals/golden.jsonl` (dev set, 117 entries) and the frozen `evals/holdout.jsonl` (70 entries, sha256-pinned at `evals/holdout.jsonl.sha256`). Entries carry `query_class` (message_id / doc_number / syntax / diagnostic / comparative / version / negative), `expected_behavior` (answer / abstain), and `must_not_retrieve` trap guards. The holdout is never iterated against: it runs on release candidates only (`make eval-holdout`). Corpus entries are mechanically verified against the live collection (`make verify-golden`).
- **Regression Gate:** `make eval` evaluates retrieval recall and MRR against the **mode-keyed baseline**: `evals/baseline.json` in hash mode (CI/dev), `evals/baseline-vllm.json` in vllm mode (release candidates, live embedder). Re-baselining is a dedicated PR (`make eval-baseline`).
- **Recorded vllm baseline (evals/baseline-vllm.json, 2026-09-02):** Recall@1 `0.45` · Recall@3 `0.752` · Recall@5 `0.826` · MRR `0.609` over 117 dev queries.
- **Regression Bounds:**
  - Overall $Recall@1 \ge 0.9\times$ baseline
  - Overall $Recall@5 \ge 0.95\times$ baseline
  - Overall $MRR \ge 0.95\times$ baseline
  - Identifier $Recall@1 = 1.0$ (strict)
  - Zero query errors
- **CI Wiring:** `make gate-l1` (ephemeral Qdrant simulator, hash-mode synthetic corpus) is an automated PR check in GitHub CI; the GitLab mirror runs hygiene + pytest only. `make loadtest-mock` (same composition plus a real uvicorn agent: zero errors, SSE integrity, citation parity, fixed error shapes under concurrency) gates PRs touching agent/retrieve/ingest via `.github/workflows/load.yml`.
- **Tier Map:** retrieval eval (`make eval`) → answer-tier grounding eval (`make eval-answers`, live GPU stack: answer entries must produce ≥1 validated citation, abstain/trap entries must not be answered) → layered harness (`make harness-gate` / `harness-l2` / `harness-l3`: snapshot-pinned L1 retrieval gate, citation precision/recall + NLI faithfulness judge, per-stage p50/p95 latency + TTFT + VRAM). Harness tiers are release-candidate-only, never PR gates.

### 5.2 Performance Benchmarking (`benchmarks/`)
- **Benchmark Suite:** `make bench` runs concurrent load tests against Qdrant and a deterministic mock LLM, measuring peak RSS, Qdrant container RAM/disk, and p50/p90/p95/p99 search and answer latencies against `benchmarks/baseline.json`.

### 5.3 Developer Tooling & Reporting (`scripts/`)
- **Interactive Conversational Assistant & REPL (`scripts/query_demo.py` / `make ask`):**
  - Interactive REPL (`rag-answer> `) and CLI tool supporting pure retrieval inspection, LLM reasoning answers, live mode toggling (`:mode`), and export to JSON/HTML.
  - Surfaces citation extraction source (`[explicit Citations: section]` vs `[inferred from excerpt [1, 2]]`).
- **Report Renderer & Comparator (`scripts/render_report.py`):**
  - Formats eval and benchmark reports into terminal text, Markdown, and 100% self-contained offline HTML dashboards.
  - Subcommands: `eval`, `bench`, `compare-eval`, `compare-bench`.
- **Local GPU Dual-Model vLLM Server (`scripts/run_local_vllm.sh` / `make local-vllm` / `make local-vllm-embed`):**
  - Runs reasoning models (Gemma-4 on port 8000, `GPU_MEM=0.64`) and embedding models (Qwen3-Embedding-0.6B on port 8001, `GPU_MEM=0.33`, `--runner pooling --convert embed --enforce-eager` — vLLM v0.28.0 removed `--task`) concurrently on consumer 8GB VRAM cards. Launch flags resolve from the `mainframe_rag.serve` Budget `LOCAL_RT_8GB` profile (explicit env wins). Both servers keep `--max-model-len 4096`; the embed window budget is pinned by `tests/test_embed_budget.py` (see §4.2).
- **Agent Dev Server (`make run-agent`):** starts uvicorn with `LLM_STREAM=true` so `/v1/answer?stream=true` streams reasoning tokens (default off in production config).
- **Automated Local End-to-End Suite (`scripts/test_local_e2e_vllm.py` / `make test-vllm-e2e`):**
  - Validates full pipeline from PDF build and dense/sparse ingestion to FastAPI HTTP `/v1/search` and `/v1/answer` endpoints against local vLLM, with served-model resolution and strict grounding validation.

---

## 6. Software Architecture & Package Layout

```text
src/mainframe_rag/
  ports.py            # Layer-boundary protocols (Embedder, QdrantPoints, Reranker, LLMClient, Tokenizer)
  ingest/
    walk.py           # *.pdf discovery
    ibm_pdf.py        # PDF parser & metadata extraction
    chrome.py         # Running header/footer stripping
    chunk.py          # Outline-based chunking & UUID5 generation
    classify.py       # Message, syntax, table, narrative classification
    embed.py          # vLLM dense & FastEmbed BM25 embedder; embed-text builder
    qdrant_io.py      # Collection creation & upsert batching
    run_ingest.py     # Ingest CLI worker orchestration
  retrieve/
    query.py          # Batched prefetch, weighted RRF fusion, diversification (sync + async)
    filters.py        # Query classification & Qdrant filter building
    rerank.py         # Cross-encoder rerank: HashReranker / HttpReranker + dispatch
  agent/
    app.py            # FastAPI service (async routes, SSE) & lifespan client management
    answer.py         # Reasoning LLM client (sync/async/SSE), prompt construction & citation grounding
    tokenizer.py      # vLLM /tokenize counting with estimator fallback
    cites.py          # Citation shape validation & extraction
  config.py           # Pydantic Settings & environment validation
  logs.py             # One-JSON-object-per-line logging
  manifest.py         # Run manifests (git sha, model ids, settings hash)
  regexes.py          # Shared identifier regexes (MSG_RE, DOCNO_RE)
```

**Allowed Dependencies:** Python 3.14 GIL, `pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx2`, `fastapi`, `pydantic-settings`.
