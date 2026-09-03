# Mainframe RAG: Installation, Deployment & Operations Guide

This guide provides end-to-end instructions for installing, configuring, deploying, and operating the **Mainframe RAG** system in both local development environments and air-gapped OpenShift clusters.

---

## 1. System Overview & Boundaries

Mainframe RAG is a citation-first retrieval-augmented generation engine designed for ~100 GB of born-digital mainframe PDF manuals.

```
┌──────────────────────────────────────────────────────────────────┐
│ Air-Gapped OpenShift Cluster                                     │
│                                                                  │
│  ┌──────────────┐   Upsert Batch    ┌─────────────────────────┐  │
│  │ Ingest Job   │──────────────────▶│ Qdrant Cluster          │  │
│  │ (One-Shot)   │                   │ 3 Replicas / RWO Block  │  │
│  └──────┬───────┘                   └────────────▲────────────┘  │
│         │ Read PDFs                              │ Query (gRPC)  │
│  ┌──────┴───────┐                   ┌────────────┴────────────┐  │
│  │ Corpus PVC   │                   │ Agent Service           │  │
│  │ (Read-Only)  │                   │ (FastAPI / No GPU)      │  │
│  └──────────────┘                   └────────────┬────────────┘  │
│                                                  │ Outbound HTTP │
│                                     ┌────────────┼────────────┐  │
│                                     ▼            ▼            ▼  │
│                                vLLM Embed     vLLM Chat    Splunk│
│                                (Dense)        (Reasoning)  (REST)│
└──────────────────────────────────────────────────────────────────┘
```

### Key Operational Rules
- **Air-Gap Image Factory:** The air-gap environment never builds images. Connected `main` builds container images with baked wheelhouses and BM25 weights, tagging them with the full 40-character Git SHA.
- **Data Storage:** Qdrant persistent volumes **must** use RWO block storage (NFS and object storage are refused).
- **Inference Separation:** Inference (embeddings and reasoning models) is provided by the cluster's internal vLLM endpoints (`VLLM_BASE_URL`).

---

## 2. Prerequisites

### Local Development
- **Operating System:** Linux / macOS / WSL2
- **Python:** CPython **3.14 GIL** (`python3.14 --version`). Do not use free-threading (`3.14t`).
- **Container Runtime:** Docker or Podman (for running integration simulation tests).
- **Tools:** `git`, `make`, `curl`.

### Disconnected / Air-Gapped Bastion
- **OpenShift Client:** `oc` (v4.12+) or `kubectl`.
- **Helm:** `helm` v3.12+ (do **not** run `helm repo add` in the air-gap; chart is vendored at `charts/qdrant-1.19.0.tgz`).
- **Image Tooling:** `skopeo` (for loading archives into the internal registry).
- **Cluster Permissions:** Access to create/manage workloads in the target namespace with `restricted-v2` SCC.

---

## 3. Local Development & Simulation Workflow

### 3.1 Setup Environment

```bash
# 1. Clone repository
git clone https://github.com/yonatan895/qdrant-pdf-rag.git
cd qdrant-pdf-rag

# 2. Bootstrap virtual environment and install locked dependencies
make venv

# 3. Download and cache BM25 model weights locally
make bm25-weights
```

### 3.2 Code Quality & Unit Tests

Run static analysis, type checking, and unit test suites:

```bash
# Run linters (ruff), type checking (mypy), and unit tests (pytest)
make check

# Or run individual verification steps:
make test        # Fast unit tests (mocked clients, synthetic data)
make lint        # Ruff linting
make typecheck   # Mypy static typing
```

### 3.3 Integration Simulation Tier

The simulation tier spins up an ephemeral Docker container running the pinned unprivileged Qdrant image (`docker.io/qdrant/qdrant:v1.19.0-unprivileged`), parses runtime-generated synthetic PDFs, ingests them, and exercises the agent API against a deterministic mock LLM server:

```bash
# Run simulation suite
make sim

# Optional: Run a persistent local Qdrant container on port 6333
make sim-qdrant

# Teardown local Qdrant container
make sim-clean
```

**Load tier** (`make loadtest-mock`, `tests/test_load_tier.py`) runs the same composition — runtime PDFs, hash-mode ingest into the pinned Qdrant image, a real uvicorn agent (`LLM_STREAM=true`) plus the deterministic mock LLM — and asserts absolute contracts under concurrency instead of correctness: zero request errors and zero missing `Server-Timing` headers on `/v1/search` and `/v1/answer`, per-stream SSE integrity on `/v1/answer?stream=true` (token deltas, exactly one `final` with citations, no `error` event), citation parity across stream/search/JSON shapes, fixed error envelopes with no leaked internals, and determinism after load. The chaos leg runs the same streams against an abort-storm mock (`MOCK_ERROR_RATE`): every stream must classify as complete XOR aborted, aborted streams carry `event: error` with no `final`, and each leaves exactly one `stream_truncated` alert. The TTFT leg runs against a paced mock (`MOCK_TTFT_MS`): agent `ttft_ms` never precedes the model's first byte. Never cross-environment latency comparisons. Knobs (CI-sane defaults): `LOAD_SEARCH_CONCURRENCY` / `LOAD_SEARCH_DURATION_S` / `LOAD_ANSWER_CONCURRENCY` / `LOAD_ANSWER_DURATION_S` / `LOAD_STREAMS` / `LOAD_STREAM_WORKERS`; `QDRANT_SIM_URL` reuses a running server.

```bash
# Run load tier (fail-closed: no skips; docker/startup/zero-request failures raise)
make loadtest-mock
```

### 3.4 Retrieval Evaluation Gates & Quality Tiers

Mainframe RAG employs a layered testing and evaluation hierarchy across CPU and GPU environments:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ EVALUATION TIERS                                                            │
│                                                                             │
│ [L1] Retrieval Gate (Every PR / MR — CPU Only)                             │
│      • Mode: EMBED_MODE=hash (CPU) against scripts/qdrant_sim.py            │
│      • Data: Runtime-generated synthetic PDFs matching evals/golden.jsonl   │
│      • Metrics: recall@1, recall@5, recall@8, MRR, nDCG@8, zero traps       │
│      • CI: gate-l1 job in GitHub Actions (.github) and GitLab CI (.gitlab)  │
│      • PR Feedback: Rendered markdown delta table posted directly to PR/MR  │
│                                                                             │
│ [L2] Answer Grounding & Faithfulness (Live GPU Stack)                       │
│      • Models: Internal vLLM reasoning model + dense embedding              │
│      • Metrics: Citation precision/recall, temp-0 NLI judge, truncation     │
│      • Execution: make harness-l2 (RC gate / dedicated GPU runner)          │
│                                                                             │
│ [L3] Latency & TTFT Performance Tier (Live GPU Stack)                       │
│      • Metrics: Per-stage p50/p95 (embed, qdrant, llm), TTFT, VRAM          │
│      • Execution: make harness-l3 (RC gate / dedicated GPU runner)          │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### Automated L1 Retrieval Gate (`make gate-l1`)

The L1 gate runs automatically on every PR in GitHub Actions and GitLab CI:

```bash
# Run L1 retrieval evaluation gate locally (starts ephemeral Qdrant simulator if needed)
make gate-l1

# Or run the script directly:
python scripts/gate_l1.py --out bundles/eval-report.json --delta bundles/eval-delta.md
```

- **Execution Invariant:** Zero committed PDFs. An original synthetic PDF corpus covering the golden dataset expectations is generated at runtime in a temporary directory and ingested in hash mode.
- **Fail-Closed Verification:** Fails nonzero if any query fails or if metrics regress beyond tolerance (strict no-drop `identifier.recall@1` ratio >= 1.0 vs baseline, `classes.message_id.recall@1` ratio >= 1.0, `recall@1 >= 0.90 * baseline`, `recall@5/8 >= 0.95 * baseline`, `mrr >= 0.95 * baseline`, `ndcg@8 >= 0.95 * baseline`, `must_not.violations == 0`).
- **PR Delta Reporting:** Automatically posts or updates a markdown delta table comment on the PR (GitHub) or merge request note (GitLab).

#### Paraphrase Retrieval Instrument (`make eval-paraphrase`)

The main golden set writes each query's text nearly verbatim into its target pages, so header-only retrieval saturates and semantic improvements (dense prefixes, reranking, contextual chunks) cannot register. `evals/paraphrase.jsonl` (22 entries) is the complementary instrument: operator-phrased queries whose answers live in the synthetic corpus **without** the query text appearing near-verbatim, over a corpus with deliberate lexical competitors (sibling docs sharing vocabulary, intra-doc section pairs). A hermetic test pins the no-echo contract on every entry.

```bash
# 1. Generate the paraphrase corpus (runtime PDFs, never committed):
.venv/bin/python -c "
import json, sys; sys.path.insert(0, 'scripts')
from pathlib import Path
from gate_l1 import generate_synthetic_golden_corpus
entries = [json.loads(l) for l in open('evals/paraphrase.jsonl') if l.strip()]
generate_synthetic_golden_corpus(entries, Path('/tmp/para-corpus'))"

# 2. Ingest into a dedicated collection (mode decides the embedder):
QDRANT_URL=http://localhost:6333 QDRANT_COLLECTION=paraphrase-manuals \
  EMBED_MODE=hash ALLOW_HASH_MODE=true \
  .venv/bin/python -m mainframe_rag.ingest.run_ingest \
  --src /tmp/para-corpus --progress /tmp/para-corpus/inventory.jsonl --workers 2

# 3. Score against the mode-keyed paraphrase baselines:
QDRANT_URL=http://localhost:6333 QDRANT_COLLECTION=paraphrase-manuals make eval-paraphrase
```

Baselines (`evals/baseline-paraphrase.json` hash, `evals/baseline-paraphrase-vllm.json` vllm) gate the same tolerances as the main set. The hash numbers are a plumbing anchor (lexical matching goes far on a small corpus); the vllm numbers are the semantic instrument — headroom below 1.0 with per-query variance is intentional. Uses: contextual-prefix A/B, reranker on/off A/B, dense-prefix tuning. Not wired into CI (no cluster, no embed server there); never iterate the frozen holdout to tune.

#### GPU Story for L2 and L3 Tiers (Operational Strategy)

Standard CI runners (`ubuntu-latest` on GitHub and air-gapped corporate GitLab runners) are CPU-only without GPU accelerators. Because L2 (NLI faithfulness judge) and L3 (Time To First Token and per-stage latency with concurrent VRAM tracking) require live GPU inference stacks:

1. **Pre-Release Release Candidate (RC) Gate (Primary):**
   - L2 and L3 are executed on lab GPU workstations during the release candidate stabilization window (`make harness-l2`, `make harness-l3`).
   - Standing-red known product debts (e.g. suffix-less doc-number gap) serve as explicit release-candidate debt tracking.
2. **Dedicated GPU Self-Hosted Runner (Optional CI Integration):**
   - For teams desiring continuous GPU evaluation, register an enterprise runner equipped with an NVIDIA GPU and tag it `gpu`.
   - Workflows target `runs-on: [self-hosted, gpu]` in GitHub Actions or `tags: [gpu]` in GitLab CI.
3. **Scheduled Nightly Benchmarks:**
   - Run L2 and L3 on a nightly schedule against `main` on the dedicated GPU runner rather than gating every PR. This avoids runner queue bottlenecks while continuously tracking latency and grounding trends.
   - Baselines (`benchmarks/harness-l3-vllm.json`) are captured in the runner's own hardware environment to eliminate environment-mismatch false alarms.

#### Manual Evaluation & Baseline Updates

```bash
# Evaluate retrieval accuracy against golden set on a running Qdrant instance.
# Baselines are mode-keyed: EMBED_MODE=hash scores against evals/baseline.json
# (CI/dev); EMBED_MODE=vllm scores against evals/baseline-vllm.json (release
# candidates, live embedder). Collection/embed-mode mismatch skips the gate
# with a loud warning.
EMBED_MODE=hash QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=local-corpus make eval

# Re-record committed accuracy baseline (dedicated PR only, per AGENTS.md)
make eval-baseline

# Run performance benchmarking
make bench

# Re-record committed benchmark baseline (dedicated PR only)
make bench-baseline
```

### 3.5 Developer Reporting & Interactive Query Assistant (`make ask` / `make query-demo`)

Mainframe RAG provides interactive terminal REPLs and single-command CLI utilities for inspecting retrieval results and testing LLM reasoning. Local developer defaults (`EMBED_MODE=hash`, `ALLOW_HASH_MODE=true`, `QDRANT_URL=http://localhost:6333`, and auto-detection of local vLLM models) are applied automatically:

```bash
# 1. Interactive conversational Q&A assistant (Reasoning LLM + Qdrant retrieval)
make ask

# 2. Ask a single question directly on the command line
make ask QUERY="What is message ICH408I?"

# 3. Query a specific collection
make ask QUERY="What is message IEA500I?" COLLECTION=local_vllm_test_corpus

# 4. Launch pure retrieval debugger REPL (inspect rank scores and chunk payloads without calling LLM)
make query-demo

# 5. Inspect a single query in pure search mode
make query-demo QUERY="IEA500I" COLLECTION=local_vllm_test_corpus

# 6. Export query results to self-contained HTML or JSON
PYTHONPATH=. .venv/bin/python scripts/query_demo.py --answer --query "IEA500I" --format html --out bundles/answer-IEA500I.html
PYTHONPATH=. .venv/bin/python scripts/query_demo.py --answer --query "IEA500I" --format json --out bundles/answer-IEA500I.json
```

#### Local Development Environment Defaults

When running local tooling (`make ask`, `make query-demo`, `test_local_e2e_vllm.py`), the following defaults are automatically applied if unset in the environment:

| Variable | Local Dev Default | Air-Gap / Prod Rule |
|---|---|---|
| **`QDRANT_URL`** | `http://localhost:6333` | Internal OpenShift service DNS (e.g. `http://qdrant:6333`) |
| **`QDRANT_COLLECTION`** | `mainframe_manuals` (or CLI `COLLECTION=...`) | `mainframe_manuals` |
| **`EMBED_MODE`** | `"hash"` (auto-set if `EMBED_BASE_URL` is unset) | `"vllm"` (mandatory; prod fails closed on hash mode) |
| **`ALLOW_HASH_MODE`** | `"true"` (auto-set for local test utilities) | `false` (fails closed to prevent hash retrieval in prod) |
| **`LLM_BASE_URL`** | `http://localhost:8000/v1` (auto-detected if listening) | Internal vLLM platform endpoint from `airgap.env` |
| **`LLM_MODEL_REASONING`** | Auto-resolved from `/v1/models` on local server | Dedicated reasoning model specified in `airgap.env` |
| **`LLM_REASONING_EFFORT_SIMPLE`** | `"low"` | `"low"` (preserves latency on factoid lookups) |
| **`LLM_REASONING_EFFORT_COMPLEX`** | `"high"` | `"high"` (enforces deep multi-step deliberation) |
| **`LLM_TEMPERATURE`** | `0.2` | `0.2` (deterministic, grounded technical reasoning) |
| **`PROMPT_MAX_CONTEXT_CHARS`** | `8000` | `8000` (for simple queries) |
| **`PROMPT_MAX_CONTEXT_CHARS_COMPLEX`** | `4500` | `4500` (reserves ~2.6k token headroom for reasoning) |
| **`RERANK_ENABLED`** | `false` | `false` (cross-encoder rerank ships default-off; see §3.11) |
| **`RERANK_BASE_URL`** | — | vLLM/TEI scoring endpoint (required when `RERANK_ENABLED=true`) |
| **`RERANK_MODEL`** | `BAAI/bge-reranker-v2-m3` | Must match the served reranker model |
| **`LLM_STREAM`** | `true` via `make run-agent` | `false` (production default; enable only where TTFT metrics are wanted) |

#### REPL Controls & Options
* **Interactive Mode Switch (`:mode`)**: Type `:mode` inside the REPL to toggle dynamically between `search` (pure vector/BM25 retrieval preview) and `answer` (retrieval + LLM reasoning generation).
* **Citation Status Indicators**: The output clearly indicates whether citations were parsed from a formal `Citations:` section (`[explicit Citations: section]`) or resolved from inline bracketed references (`[inferred from excerpt [1, 2]]`).
* **Formatted Scripts**: JCL and REXX scripts produced by the reasoning model are automatically extracted and syntax-highlighted in terminal output and rendered inside copy-friendly code blocks in HTML exports.

---

### 3.6 Local vLLM Inference & GPU Acceleration (RTX 5060 / 8GB VRAM)

The repository provides a hardened launcher script ([`scripts/run_local_vllm.sh`](../scripts/run_local_vllm.sh)) and Makefile targets for serving local reasoning and dense embedding models via Docker with NVIDIA GPU pass-through:

#### Key Launcher Features
* **Pinned Container Image**: Defaults to `vllm/vllm-openai:v0.28.0` (built with CUDA 12.8+, supporting NVIDIA Blackwell architectures like the RTX 5060 Laptop GPU and Gemma-4). The pinned tag implements every flag the script passes — v0.28.0 removed `--task`, so the embed branch passes `--runner pooling --convert embed`.
* **Dual-Model 8GB VRAM Co-Residency** (defaults resolved from the `mainframe_rag.serve` Budget `LOCAL_RT_8GB` profile — single source of truth, not script constants; the launcher preflights the full co-resident pack with `--check-pack` before starting either server):
  - **Reasoning Model (Port 8000)**: `GPU_MEM=0.64` (~5.2 GB VRAM allocation).
  - **Embedding Model (Port 8001)**: `GPU_MEM=0.33` with `--enforce-eager` (~2.7 GB VRAM budget; measured 1.29 GiB spare KV at startup). Explicit `GPU_MEM=`/`MAX_LEN=`/`SEQS=`/`ROLE=` always win.
  - Fits comfortably within 8GB VRAM cards. With torch.compile enabled the embed server's profiled peak (compile + CUDA-graph workspace) went over budget — eager mode removes it, and embeddings are single-shot prefill so eager costs little.
  - *Solo Runs*: For dedicated reasoning benchmarks, `GPU_MEM=0.85 make local-vllm` restores maximum KV cache capacity.
* **8GB VRAM Optimizations**:
  - `--limit-mm-per-prompt '{"image":0,"audio":0}'`: Disables multimodal vision/audio buffers in Gemma 4 to reclaim substantial VRAM.
  - `--max-num-seqs 1`: Bounds concurrent sequence allocation to prevent out-of-memory spikes.
  - `--enable-prefix-caching`: off unless the Budget profile enables it (`prefix_cache`, issue #80 measures the hit rate first).
  - `--max-num-batched-tokens` (embed server): capped at the Budget window so the memory-profiling peak stays bounded; it does not follow a `MAX_LEN` operator override (erring small is the safe side).
  - `MAX_LEN=4096` for **both** servers: the reasoning prompt budget requires it, and a 2048 embed window was rejected by tokenizer sweep — the worst-case embedded string (chunk header + a `SECTION_MAX_CHARS=3500` body with the 400-char split seed) measures ~2,043 tokens at ~2.0 chars/token on syntax-dense text. The budget is pinned hermetically by `tests/test_embed_budget.py`.
* **Gemma-4 Support**: Automatically configures `--tool-call-parser gemma4`, `--reasoning-parser gemma4`, and `--chat-template /vllm-workspace/examples/tool_chat_template_gemma4.jinja`.
* **Embedding Model Detection**: Model names matching `*embed*`/`*Embed*` (e.g. `Qwen/Qwen3-Embedding-0.6B`) derive `ROLE=embed` (overridable; `make local-vllm*` passes `ROLE` explicitly) and get the Budget pooling-runner serving shape automatically.
* **WSL2 Compatibility**: Exports `VLLM_WSL2_ENABLE_PIN_MEMORY=1` for host memory stability.
* **Safe Secrets**: Passes `HF_TOKEN` via `-e HF_TOKEN` without exposing secret tokens on command-line argument lists.

#### Starting Local vLLM Servers

**1. Start the Reasoning Model Server (Port 8000):**
```bash
# Offline weights directory (recommended):
MODEL=/path/to/models/gemma-4-E4B-it-qat-mobile-ct make local-vllm

# Or via HuggingFace Hub:
HF_TOKEN="<your-token>" MODEL=google/gemma-4-E4B-it-qat-mobile-ct make local-vllm
```

**2. Start the Dense Embedding Server (Port 8001):**
```bash
# Offline weights directory (recommended):
MODEL=/path/to/models/Qwen3-Embedding-0.6B make local-vllm-embed

# Or via HuggingFace Hub:
MODEL=Qwen/Qwen3-Embedding-0.6B make local-vllm-embed
```

---

### 3.7 Reasoning Performance, Query Complexity & Context Budgeting

The agent dynamically adapts its reasoning protocol and prompt allocation based on the technical nature of incoming inquiries.

#### Query Classification Matrix
The pipeline automatically classifies queries into two categories:
* **Simple Inquiries (Factoids & Message Codes)**:
  - Examples: `What does operator message IEA500I indicate?`, `What parameter in IEASYSxx defines 1MB large page frames?`, `What return code does NFS mount fail with?`
  - Latency: ~4–8 seconds
  - Reasoning Tokens: ~200–500 tokens
  - Engine Settings: `LLM_REASONING_EFFORT_SIMPLE=low`, `PROMPT_MAX_CONTEXT_CHARS=8000`
  - Operational Goal: Fast, concise, low-latency extraction without wasteful compute overhead.
* **Complex Inquiries (Diagnostics, Procedures, Comparative Tuning)**:
  - Examples: `How do I diagnose and recover when the DFSMShsm journal fills up during active migration?`, `Explain how to configure 1MB and 2GB large page frames with LFAREA in IEASYSxx...`, `Compare DFSORT memory options (HIPRMAX, MOSIZE, DSPSIZE)...`
  - Latency: ~14–20 seconds
  - Reasoning Tokens: ~800–1,250 tokens
  - Engine Settings: `LLM_REASONING_EFFORT_COMPLEX=high`, `PROMPT_MAX_CONTEXT_CHARS_COMPLEX=4500`
  - Operational Goal: Exhaustive technical analysis, cross-referencing parameters/messages across excerpts, and synthesizing actionable recovery steps and fenced JCL/operator command blocks.

#### Context Length Budgeting: Root Cause & Solution
* **The 4,096-Token Ceiling**: Local reasoning instances (e.g. Gemma-4) run with `--max-model-len 4096` to fit within 8GB VRAM cards alongside embedding models.
* **The Truncation Bug**: If prompt context is allowed to reach 8,000 characters (~2,400 tokens), only ~1,600 tokens remain for both internal thinking and output generation. When the model thinks deeply (>1,000 reasoning tokens), it runs out of token budget before finishing the answer. This caused answers to be truncated mid-sentence (`Finish: length`) and dropped the `Citations:` section.
* **The Solution**: Setting `PROMPT_MAX_CONTEXT_CHARS_COMPLEX=4500` caps complex retrieved passages at ~1,200 tokens. This guarantees **~2,600 tokens of headroom** exclusively for thinking tokens and comprehensive answer text, achieving a 100% completion rate (`Finish: stop`).

---

### 3.8 Automated Local End-to-End Suite (`make test-vllm-e2e`)

To verify the entire RAG pipeline from PDF generation and dense/sparse ingestion to HTTP retrieval and grounded LLM reasoning:

```bash
# Run automated end-to-end test against both local servers:
make test-vllm-e2e

# Or pass custom model parameters:
make test-vllm-e2e \
  MODEL=gemma-4-E4B-it-qat-mobile-ct \
  VLLM_URL=http://localhost:8000/v1 \
  EMBED_MODEL=Qwen3-Embedding-0.6B \
  EMBED_URL=http://localhost:8001/v1 \
  DENSE_DIM=1024
```

#### Test Execution Flow
1. **Model Connectivity & Dimension Probing**: Queries `/v1/models` and `/v1/embeddings` to auto-resolve served model names and probe the dense embedding dimension (`dense_dim=1024` for Qwen3-0.6B).
2. **Collection Dimension Validation**: If `--skip-ingest` is passed, validates that the collection exists and its dense vector dimension matches `dense_dim` (failing fast if mismatched). If ingesting, automatically recreates the collection if dimensions changed.
3. **Corpus Generation & Ingest**: Builds synthetic IBM-shaped manual PDFs with specific message IDs (`IEA500I`, `LFAREA`) and ingests them into a local Qdrant collection using real dense + BM25 sparse vectors.
4. **HTTP `/v1/search` Verification**: Queries the FastAPI endpoint and validates parallel prefetch fusion and hit ranking.
5. **HTTP `/v1/answer` Verification**: Executes reasoning queries against the local vLLM server via FastAPI HTTP endpoints.
6. **Strict Grounding Gate**: Fails closed if the model response returns zero validated citations or indicates ungrounded hallucination.

#### Streaming Reasoning on the Local Stack (`make run-agent`)
```bash
# Start the agent with LLM_STREAM=true so reasoning SSE reaches the client:
make run-agent                    # uvicorn on http://localhost:8080

# Stream a grounded answer (SSE):
curl -N -X POST "http://localhost:8080/v1/answer?stream=true" \
  -H "Content-Type: application/json" \
  -d '{"query": "What parameter controls LFAREA in IEASYSxx?"}'
```
The SSE response yields `event: token` deltas as the reasoning model generates, then exactly one terminal `event: final` carrying the full verified answer, citations, optional script, retrieval metadata, `ttft_ms`, and token usage. A mid-stream failure emits `event: error` and ends **without** a `final` event — treat stream-end-without-final as a failed request. `LLM_STREAM` (server-side reasoning SSE) defaults to `false` in production config; `make run-agent` enables it for TTFT measurement (also consumed by the L3 harness).

---

### 3.9 Exporting Standalone Model Weights for Offline Bastions

To archive model weights and configurations for use in completely disconnected or air-gapped environments:

```bash
# 1. Download Gemma-4 Reasoning Model:
.venv/bin/python -c '
from pathlib import Path
from huggingface_hub import snapshot_download

target_dir = Path.home() / "models" / "gemma-4-E4B-it-qat-mobile-ct"
target_dir.mkdir(parents=True, exist_ok=True)
snapshot_download(
    repo_id="google/gemma-4-E4B-it-qat-mobile-ct",
    local_dir=str(target_dir),
    local_dir_use_symlinks=False,
)
'

# 2. Download Qwen3-Embedding-0.6B Dense Embedder:
.venv/bin/python -c '
from pathlib import Path
from huggingface_hub import snapshot_download

target_dir = Path.home() / "models" / "Qwen3-Embedding-0.6B"
target_dir.mkdir(parents=True, exist_ok=True)
snapshot_download(
    repo_id="Qwen/Qwen3-Embedding-0.6B",
    local_dir=str(target_dir),
    local_dir_use_symlinks=False,
)
'
```

---

### 3.10 Managing Collections & Real-World Ingest

When working with real PDF corpora (e.g. `z/OS 3.2` manuals, vendor books):

#### 1. Initial Ingestion with Dense Embeddings
```bash
EMBED_MODE=vllm \
EMBED_BASE_URL=http://localhost:8001/v1 \
EMBED_MODEL=Qwen3-Embedding-0.6B \
DENSE_DIM=1024 \
QDRANT_URL=http://localhost:6333 \
QDRANT_COLLECTION=mainframe_manuals \
.venv/bin/python -m mainframe_rag.ingest.run_ingest \
  --src /path/to/manuals \
  --progress /path/to/manuals/inventory.jsonl \
  --workers 4
```

#### 2. Incremental Ingestion: Adding New PDFs Without Re-ingesting
Mainframe RAG supports native **idempotent incremental ingestion** via the inventory tracking file (`--progress inventory.jsonl`):

* **SHA-256 Change Detection**: On every run, `run_ingest` computes the SHA-256 digest of each discovered PDF.
* **Instant Skipping**: Any PDF whose SHA-256 digest is already marked as `upserted` in `inventory.jsonl` is skipped immediately (zero PDF parsing, zero embedding overhead).
* **Deterministic UUID5 Point IDs**: New chunks are assigned deterministic UUID5 keys and inserted directly into the existing Qdrant collection without deleting or modifying previously indexed vectors.
* **Corrupted / Partial File Safety**: If ingestion was interrupted midway or a PDF failed earlier with an error, re-running `run_ingest` will pick up right where it left off, only processing un-ingested files.

**How to add new manuals:**
Simply drop the new PDFs into your manuals directory (or specify a new `--src` directory) and re-run with the same `QDRANT_COLLECTION` and `--progress` path:

```bash
# Ingest only newly added or modified PDFs:
EMBED_MODE=vllm \
EMBED_BASE_URL=http://localhost:8001/v1 \
EMBED_MODEL=Qwen3-Embedding-0.6B \
DENSE_DIM=1024 \
QDRANT_URL=http://localhost:6333 \
QDRANT_COLLECTION=mainframe_manuals \
.venv/bin/python -m mainframe_rag.ingest.run_ingest \
  --src /path/to/new_manuals \
  --progress /path/to/manuals/inventory.jsonl \
  --workers 4
```

#### 3. Querying with Live Models
```bash
# Interactive reasoning assistant:
EMBED_MODE=vllm \
EMBED_BASE_URL=http://localhost:8001/v1 \
EMBED_MODEL=Qwen3-Embedding-0.6B \
DENSE_DIM=1024 \
LLM_BASE_URL=http://localhost:8000/v1 \
LLM_MODEL_REASONING=gemma-4-E4B-it-qat-mobile-ct \
QDRANT_URL=http://localhost:6333 \
.venv/bin/python scripts/query_demo.py --answer --query "Your question here"
```

---

### 3.11 Cross-Encoder Reranker (Optional, Default Off)

Retrieval quality upgrade: fused RRF candidates (top-`RERANK_CANDIDATES`, default 50) are rescored by a cross-encoder before diversification. **Ships default-off** (`rerank_enabled=false`) — retrieval results are identical to the hybrid+RRF baseline until explicitly enabled.

```bash
# Enable against a vLLM /v1/score endpoint (falls back to /v1/rerank):
RERANK_ENABLED=true \
RERANK_BASE_URL=http://localhost:8001/v1 \
RERANK_MODEL=BAAI/bge-reranker-v2-m3 \
make run-agent
```

| Variable | Default | Notes |
|---|---|---|
| `RERANK_ENABLED` | `false` | Master switch; nothing calls the cross-encoder when off |
| `RERANK_BASE_URL` | falls back to `EMBED_BASE_URL` | vLLM or TEI scoring endpoint |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | Must match the served reranker |
| `RERANK_CANDIDATES` | `50` | Fused candidates rescored per query (10–100) |
| `RERANK_BATCH_SIZE` | `32` | Texts scored per HTTP call |
| `RERANK_TIMEOUT_S` | `5.0` | Per scoring call |

Failures surface as `rerank_ms` in `/v1/search` `Server-Timing` and the agent log; a reranker outage fails the request like any other retrieval fault (`502 upstream_error`). CI/dev runs without a reranker endpoint can use `HashReranker` (lexical scoring) via `ALLOW_HASH_MODE=true` — dev only, never production.

---

## 4. Air-Gapped OpenShift Deployment

### 4.1 Packaging on the Connected Host (Image Factory)

On a connected Linux host with Docker/Podman:

```bash
git clone https://github.com/yonatan895/qdrant-pdf-rag.git
cd qdrant-pdf-rag
git checkout <main-sha>  # Full 40-character SHA matching built GHCR images

# Build air-gap sneakernet package:
make airgap-pack
```

This generates `dist/qdrant-pdf-rag-<sha>.tar` and its digest `dist/qdrant-pdf-rag-<sha>.tar.sha256`, containing:
1. Complete Git repository bundle (`repo.bundle`).
2. App container images (`qdrant-pdf-rag-agent`, `qdrant-pdf-rag-ingest`).
3. Vendored third-party Qdrant unprivileged image.
4. Vendored Helm chart (`charts/qdrant-1.19.0.tgz`).
5. Manifest and member `SHA256SUMS`.

### 4.2 Transfer & Verification

Transfer the tarball and checksum file via approved sneakernet media to the air-gapped bastion host:

```bash
# 1. Verify tarball integrity BEFORE unpacking
sha256sum -c qdrant-pdf-rag-<sha>.tar.sha256

# 2. Extract tarball
tar -xf qdrant-pdf-rag-<sha>.tar

# 3. Clone repository from bundle
git clone repo.bundle qdrant-pdf-rag
cd qdrant-pdf-rag
```

### 4.3 Configure Environment

Copy `airgap.env.example` to `airgap.env` and populate cluster values:

```bash
cp airgap.env.example airgap.env
chmod 600 airgap.env
```

Edit `airgap.env`:
```ini
# Internal image registry accessible to cluster nodes
INTERNAL_REGISTRY=registry.internal.enterprise:5000/mainframe-rag

# Target namespace
NAMESPACE=mainframe-rag

# Persistent storage class (Must be RWO Block, e.g. ocs-storagecluster-ceph-rbd)
STORAGE_CLASS=gp3-csi

# In-cluster inference endpoints (provided by LLM platform team)
VLLM_BASE_URL=http://vllm.inference.svc.cluster.local:8000/v1
EMBED_MODEL=ibm-granite/granite-embedding-278m-multilingual
DENSE_DIM=768
LLM_MODEL_REASONING=ibm-granite/granite-20b-code-instruct

# Optional pull secret name (if registry requires credentials)
PULL_SECRET=internal-registry-pull-secret
```

### 4.4 Load Images & Deploy

```bash
# 1. Verify internal member checksums and push images to internal registry
make airgap-load

# 2. Deploy Qdrant 3-replica cluster and Agent deployment
make airgap-deploy
```

Verify pod statuses and readiness:
```bash
oc -n mainframe-rag get pods -w
oc -n mainframe-rag get pvc
```

Acceptance Criteria:
- 3 Qdrant StatefulSet pods Running (`qdrant-0`, `qdrant-1`, `qdrant-2`).
- 2 Agent pods Running (`rag-agent-...`).
- All PVCs `Bound` with block storage class.
- Security Context: running unprivileged under `restricted-v2` SCC.

### 4.5 Corpus Ingestion

Once the manual PDF corpus PVC is provisioned and populated:

```bash
# Launch one-shot ingest Job against corpus PVC
make airgap-ingest CORPUS_PVC=mainframe-manuals-pvc
```

Monitor ingest progress:
```bash
oc -n mainframe-rag logs -f job/rag-ingest
```

### 4.6 Verification & Smoke Testing

```bash
# Run in-cluster smoke search
make airgap-smoke

# Or query specific message IDs:
QUERY="IEA500I operator message" make airgap-smoke
```

---

## 5. Day-2 Operations & Maintenance

### 5.1 Health Check API

The agent exposes `/healthz` for OpenShift liveness/readiness probes:

```bash
curl -s http://rag-agent.mainframe-rag.svc:8080/healthz
```

Response format:
```json
{
  "status": "ok",
  "qdrant": true,
  "embed": true
}
```

If Qdrant shards are unready or degraded, `/healthz` returns `503` with JSON error `{"code": "qdrant_unready", "message": "qdrant is not ready"}` (internal errors and raw upstream payloads remain in server logs only).

### 5.2 Agent Endpoints

#### `POST /v1/search`
Retrieves ranked manual chunks with normalized citations without invoking an LLM.

```bash
curl -X POST http://rag-agent:8080/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "IEA500I IOSCMDS COMMAND REJECTED", "limit": 5}'
```

Stage timings are returned as `Server-Timing: embed;dur=..., qdrant;dur=..., rerank;dur=...` (the rerank stage appears only when the cross-encoder is enabled).

#### `POST /v1/answer`
Executes hybrid retrieval, constructs a citation-grounded prompt, and queries the reasoning LLM.

```bash
curl -X POST http://rag-agent:8080/v1/answer \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How do I resolve IEA500I command rejected?",
    "product": "z/OS",
    "version": "3.1"
  }'
```

JSON-mode responses carry `Server-Timing: embed, qdrant, llm;dur=..., ttft;dur=...` (TTFT = time to first generated token; requires `LLM_STREAM=true` on the agent server for server-side reasoning SSE).

#### Streaming (`?stream=true`)
Both the query parameter and the body field (`"stream": true`) enable server-sent events; the query parameter wins when both are set. The response is `text/event-stream`:

1. Zero or more `event: token` frames — incremental answer deltas.
2. Exactly one terminal `event: final` — the full verified answer, validated citations, optional script, retrieval hits, query kind, `ttft_ms`, and token `usage`. The `final` schema is identical on the empty-hits path (zero citations, `ttft_ms: null`, zeroed usage).
3. A mid-stream failure emits `event: error` and the stream ends **without** a `final` event — clients must treat stream-end-without-final as a failed request.

Citation validation runs on the accumulated text exactly as in JSON mode: the citations in the `final` event are byte-identical to the non-streaming response for the same request.

### 5.3 Common Troubleshooting Scenarios

| Issue | Symptom | Remediation |
|---|---|---|
| **Dimension Mismatch** | Ingest/Search fails with 400 dimension mismatch | Verify `DENSE_DIM` in `airgap.env` matches `EMBED_MODEL` on vLLM. |
| **Qdrant Unready** | `/healthz` returns `503 qdrant_unready` | Check Qdrant pod logs (`oc logs qdrant-0`); check block PVC mount. |
| **NFS Storage Refusal** | `make airgap-deploy` fails validation | Set `STORAGE_CLASS` to an RWO block driver (Ceph RBD / SAN / EBS). |
| **Hash Mode in Prod** | Scripts fail closed with `EMBED_MODE=hash forbidden` | Remove `EMBED_MODE` from production environment; provide valid vLLM endpoint. |
