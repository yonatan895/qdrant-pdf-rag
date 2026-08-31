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
| `qdrant` | StatefulSet (vendored chart) | 3 | Cluster mode, P2P 6335, HTTP 6333, gRPC 6334, `restricted-v2` SCC |
| `rag-agent` | Deployment | 2 | FastAPI, unprivileged, no GPU |
| `rag-ingest` | One-Shot Job | 1 | High CPU, worker pool, RWO scratch |
| `bm25-weights` | Baked in images | — | FastEmbed `Qdrant/bm25`; no runtime download |

- **Storage Constraint:** Qdrant persistent data volume **must** be RWO Block storage (NFS and object storage are refused). Ingest work volume is also RWO Block. The corpus volume may be mounted as read-only NFS or PVC.
- **Networking:** ClusterIP services only. No public Route to Qdrant. Agent Route is optional (`AGENT_ROUTE=true`).

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

- **Dense Embeddings:** Ingest calls internal vLLM via `POST ${VLLM_BASE_URL}/embeddings` (supporting arbitrary embedding dimensions, e.g. 1024-dim `Qwen3-Embedding-0.6B` or 768-dim models). Dimension is dynamically auto-probed and validated.
- **Sparse BM25 Embeddings:** Computed in-process via FastEmbed using pre-baked `Qdrant/bm25` weights.
- **Collection Configuration (`mainframe_manuals`):**
  - Dense: `${DENSE_DIM}` dimensions, Cosine distance, on-disk HNSW ($M=16, ef\_construct=128$), int8 scalar quantization in RAM. Test runners automatically recreate collections if vector dimensions differ between test runs.
  - Sparse: `modifier=idf`, on-disk storage.
  - Payload indexes: `doc_id`, `product`, `version`, `vendor`, `chunk_type`, `message_ids`, `members`, `sha256` (keyword indexes).

### 4.3 Parallel Prefetch & Retrieval Fusion

Retrieval executes filtered dense and BM25 searches **concurrently** using thread workers to minimize retrieval latency:

```
User / Splunk Query
   ├── Query Classifier: Identifier (message ID / doc ID) vs Natural Language
   │
   ├── Thread Pool (max_workers=2)
   │     ├── Worker 1: Dense ANN Query (Prefetch limit 40)
   │     └── Worker 2: Sparse BM25 Query (Prefetch limit 40)
   │
   ├── Payload Projection: Fetch only RETRIEVE_PAYLOAD_FIELDS
   │
   ├── Local Reciprocal Rank Fusion (RRF):
   │     k = 2
   │     Weights: [1.0, 3.0] (Dense, BM25) for Identifiers
   │     Weights: [1.0, 1.0] for Natural Language
   │
   └── Top-K Ranked Hits with Strict Citation Formatting
```

### 4.4 Reasoning LLM Prompt Construction & Citation Grounding Contract

The agent enforces strict grounding guarantees before returning reasoning answers to clients:

1. **Few-Shot Citation Injection:**
   The prompt dynamically includes a concrete few-shot example using `hits[0].cite` in the instructions to enforce uniform formatting from both large reasoning models and quantized edge models (e.g. Gemma 4 INT4 QAT).

2. **Two-Pass Citation Resolution:**
   - **Primary Pass (Explicit Block):** Looks for a terminal `Citations:` section. Each listed citation is normalized and matched against the allowed search hit citations (`allowed_citations = {h.cite for h in hits}`).
   - **Fallback Pass (Bracketed Index Resolution):** If no explicit `Citations:` block is present, the parser scans for bracketed number references `\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]` (matching prompt tokens like `[1]`, `[2]`, `[1, 2]`) and resolves them to `ordered_cites[index - 1]`.
   - **Parenthesis Immunity:** Parentheses `(...)` are deliberately excluded from inference to avoid false positives on standard mainframe technical notation such as `z/OS (3.1)`, `SYS1.PARMLIB(IEASYS00)`, `(2)`, or `APARs (1, 2)`.

3. **Body Stripping & Verification:**
   Any hallucinated citation lines that match the citation regex but are not in `allowed_citations` are stripped from the response text before transmission. Mid-sentence narrative text mentioning document IDs is preserved under the standalone-line rule.

### 4.5 Outbound HTTP & Agent Lifespan

Outbound HTTP communication is managed via `httpx2` with connection pools created and closed during application lifespan:
- **Reasoning Answer Calls (`/v1/answer`):** Single-shot with a 300s timeout; connection-level retries are explicitly disabled (`retries=0`).
- **Embed & Health Probes:** Managed with bounded connection retries (`http_connect_retries: 2`).
- **Error Contract:** Standard JSON error envelopes (`{"code": "...", "message": "..."}`). Internal exceptions and upstream response bodies are never leaked to clients.

---

## 5. Evaluation, Benchmarking & Tooling

### 5.1 Retrieval Accuracy Gates (`evals/`)
- **Golden Dataset:** `evals/golden.jsonl` contains labeled identifier and natural language queries.
- **Regression Gate:** `make eval` evaluates retrieval recall and MRR against `evals/baseline.json`.
- **Measured Accuracy (Qwen3-Embedding-0.6B Dense + BM25 Sparse):**
  - **Recall@1:** `0.667` *(Identifier: 1.000, NL: 0.556, Baseline: 0.500)*
  - **Recall@3:** `0.833` *(Baseline: 0.750)*
  - **Recall@5:** `0.917` *(Identifier: 1.000, NL: 0.889, Baseline: 0.750)*
  - **MRR:** `0.781` *(Identifier: 1.000, NL: 0.708, Baseline: 0.625)*
  - **Failures:** `0 / 12`
- **Regression Bounds:**
  - Overall $Recall@1 \ge 0.9\times$ baseline
  - Overall $Recall@5 \ge 0.95\times$ baseline
  - Overall $MRR \ge 0.95\times$ baseline
  - Identifier $Recall@1 = 1.0$ (strict)
  - Zero query errors

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
  - Runs reasoning models (Gemma-4 on port 8000, `GPU_MEM=0.55`) and embedding models (Qwen3-Embedding-0.6B on port 8001, `GPU_MEM=0.43 MAX_LEN=2048`) concurrently on consumer 8GB VRAM cards.
- **Automated Local End-to-End Suite (`scripts/test_local_e2e_vllm.py` / `make test-vllm-e2e`):**
  - Validates full pipeline from PDF build and dense/sparse ingestion to FastAPI HTTP `/v1/search` and `/v1/answer` endpoints against local vLLM, with dimension auto-probing and strict grounding validation.

---

## 6. Software Architecture & Package Layout

```text
src/mainframe_rag/
  ingest/
    walk.py           # *.pdf discovery
    ibm_pdf.py        # PDF parser & metadata extraction
    chrome.py         # Running header/footer stripping
    chunk.py          # Outline-based chunking & UUID5 generation
    classify.py       # Message, syntax, table, narrative classification
    embed.py          # vLLM dense & FastEmbed BM25 embedder
    qdrant_io.py      # Collection creation & upsert batching
    run_ingest.py     # Ingest CLI worker orchestration
  retrieve/
    query.py          # Parallel prefetch & weighted RRF search
    filters.py        # Query classification & Qdrant filter building
  agent/
    app.py            # FastAPI service & lifespan client management
    answer.py         # Reasoning LLM prompt construction & citation grounding
    cites.py          # Citation shape validation & extraction
  config.py           # Pydantic Settings & environment validation
```

**Allowed Dependencies:** Python 3.14 GIL, `pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx2`, `fastapi`, `pydantic-settings`.
