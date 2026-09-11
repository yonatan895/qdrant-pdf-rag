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

# 3. Download and cache BM25 model weights locally (first run downloads
# Qdrant/bm25 via FastEmbed — needs HuggingFace reachability; afterwards the
# weights live in bundles/bm25-weights and are baked into images)
make bm25-weights
```

Fresh-machine heavylift to budget for: the venv install, the BM25 download above, the Qdrant image pull on first `make sim`, and the `vllm-openai` pull on first `make local-vllm*` (the pinned `v0.28.0` image is ≈29 GB). The `HF_TOKEN` path in §3.6 additionally needs gated-repo approval (e.g. Gemma) before the token works — request access first.

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

Tier map, thresholds, and gate semantics live in `eval.md` (§1–§4); the
rungs your change class owes live in `live-stack.md` (§0, §3). This
section keeps only the runnable commands.

#### Automated L1 Retrieval Gate (`make gate-l1`)

The L1 gate runs automatically on every PR in GitHub Actions and GitLab CI:

```bash
# Run L1 retrieval evaluation gate locally (starts ephemeral Qdrant simulator if needed)
make gate-l1

# Or run the script directly:
python scripts/gate_l1.py --out bundles/eval-report.json --delta bundles/eval-delta.md
```

- **Execution Invariant:** Zero committed PDFs. An original synthetic PDF corpus covering the golden dataset expectations is generated at runtime in a temporary directory and ingested in hash mode.
- **Fail-Closed Verification:** Fails nonzero on any query failure or metric regression — ratios in `eval.md` §2 (strict `identifier`/`message_id` recall@1, `must_not.violations == 0` absolute).
- **PR Delta Reporting:** Automatically posts or updates a markdown delta table comment on the PR (GitHub) or merge request note (GitLab).

#### Paraphrase Retrieval Instrument (`make eval-paraphrase`)

The main golden set echoes query text into target pages, so semantic
improvements cannot register — `evals/paraphrase.jsonl` (22 entries) is the
complementary instrument (rationale, no-echo contract, and baselines in
`eval.md` §6; never tune against the frozen holdout).

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

Baselines (`evals/baseline-paraphrase.json` hash, `evals/baseline-paraphrase-vllm.json` vllm) gate the same tolerances as the main set (`eval.md` §2). Uses: contextual-prefix A/B, reranker on/off A/B, dense-prefix tuning. Not wired into CI (no cluster, no embed server there).

#### GPU Story for L2 and L3 Tiers (Operational Strategy)

Standard CI runners are CPU-only; L2/L3 need the live GPU stack. Three
options, cheapest first:

1. **RC gate (primary):** run `make harness-l2` / `make harness-l3` on lab
   GPU workstations during the release-candidate window. Standing-red
   product debts are tracked as explicit RC debt.
2. **Dedicated GPU runner (optional):** enterprise runner with an NVIDIA
   GPU tagged `gpu`; workflows target `runs-on: [self-hosted, gpu]`
   (GitHub) or `tags: [gpu]` (GitLab CI).
3. **Nightly schedule:** L2/L3 against `main` on the GPU runner instead of
   per-PR gating. Baselines (`benchmarks/harness-l3-vllm.json`) are
   captured in the runner's own hardware environment.

#### Manual Evaluation & Baseline Updates

Eval/baseline/bench mechanics (mode-keyed baselines, mismatch skip
semantics, dedicated-PR re-recording) live in `eval.md` §2; the rungs you
owe live in `live-stack.md` §3:

```bash
# Evaluate retrieval accuracy against the golden set (mode-keyed baselines
# per eval.md §2; mismatch skip semantics per eval.md §2).
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
  - `--enable-prefix-caching`: on for the LOCAL reasoning server (Budget `prefix_cache`; vLLM already caches by default — the pin guards flips, hit rate measured for issue #80); off for embed.
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

#### Serving configurations (pick one pack per 8GB card)

One server per `make` invocation (each blocks its shell — run each in its own terminal or background it). `BUDGET_PROFILE` selects the pack; the launcher preflights the full pack (`--check-pack`) and fails closed on deficits. Explicit `GPU_MEM=`/`MAX_LEN=`/`SEQS=` always win over resolved values.

| Goal | Profile (default `LOCAL_RT_8GB`) | Commands | Notes |
|---|---|---|---|
| Answer quality (big reasoning + embedding) | `LOCAL_RT_8GB` | `make local-vllm` (:8000, E4B) + `make local-vllm-embed` (:8001) | Default pair. No room for a third leg (measured 7.0 GB resident). |
| Full topology (reasoning + embedding + ranking) | `TRIPLE_8GB` | Above with `MODEL=Qwen/Qwen2.5-0.5B-Instruct GPU_MEM=0.20 MAX_LEN=4096 SEQS=1` on :8000, plus `make local-vllm-rerank` (:8002) | 0.5B answers are weak — plumbing/rerank coverage only. E4B triple demonstrably does not fit; resolve refuses it. |
| Retrieval + ranking, no LLM | `RANK_EMBED_8GB` | `make local-vllm-embed` (:8001) + `make local-vllm-rerank` (:8002) | Rerank A/B and `--rerank` evals without spending VRAM on reasoning. |
| Reasoning + ranking (no vLLM embed) | — | Unsupported | Hash embed mode pins `HashReranker` by design (determinism), so a GPU reranker is unreachable there — see issue #193. |

Reranked search also needs `RERANK_ENABLED=true RERANK_BASE_URL=http://127.0.0.1:8002 RERANK_MODEL=BAAI/bge-reranker-v2-m3` on the consumer side (`query-demo`, eval `--rerank`, agent env). Launch order on a cold card: reasoning → embed → rerank (a 4k-context server fails KV init against leftovers; the profiles declare this allocation order).

#### Local Production Simulation (`make local-stack`)

The ownership contract is explicit: in production the platform team owns the model tier (vLLM + LiteLLM) and this repo owns Qdrant + ingest + retrieval + the agent, reaching every model leg only over the gateway HTTP contract. `make local-stack` reproduces that **complete topology on one machine** — pinned Qdrant + the real LiteLLM gateway (digest-pinned) in front of the three local vLLM backends + the FastAPI agent — and probes every leg through the gateway before declaring the stack up. Agent and ingest never call vLLM directly, so the wire contract under test is the production one.

Prerequisites: Docker, the three backends already running (`make local-vllm` :8000, `make local-vllm-embed` :8001, `make local-vllm-rerank` :8002), and `.venv`. Qdrant is started via `make sim-qdrant` (the pinned-image owner) when unreachable; `make sim-clean` stops it.

```bash
# Full stack: Qdrant -> gateway -> probe -> (optional ingest) -> agent -> smoke
make local-stack
CORPUS_DIR=output/demo-pdfs make local-stack     # also ingest through the gateway
LOCAL_STACK_DRYRUN=1 make local-stack            # ordered plan only; no docker/network
```

On exit (Ctrl-C) the agent and the gateway stop; Qdrant stays for `make sim-clean`. Ports: agent 8080 (`LOCAL_AGENT_PORT`), gateway 4000 (`GATEWAY_PORT`), Qdrant `QDRANT_URL` (default `http://127.0.0.1:6333`). Ephemeral keys are written mode 600 to a temp env file (`GATEWAY_ENV_FILE`, default `/tmp/local-stack-gateway-<port>.env`) — never inside the repo, never committed.

The gateway contract the stack exercises:

* **One origin per leg, model-id routing**: `EMBED_BASE_URL`, `LLM_BASE_URL`, and `RERANK_BASE_URL` all point at `http://localhost:4000/v1`; the requested `model` id selects the backend.
* **Per-leg Bearer virtual keys**: the agent/ingest `*_API_KEY` values are LiteLLM virtual keys; missing/unknown keys get 401 on every route (including the rerank legs), so key wiring is exercised for real.
* **Both rerank legs**: native `/v1/rerank` via the `hosted_vllm/` provider, plus a `/v1/score` pass-through to the vLLM backend (LiteLLM has no native score route). `scripts/probe_gateway.py` prints the recommended `RERANK_ENDPOINT_ORDER` after probing both.
* **Tokenizer**: `/tokenize` stays 404 behind the gateway — the agent pins its in-process estimator after one warning (expected, not a fault).

For single-leg debugging, `make local-gateway` keeps the gateway in the foreground (`make local-gateway-stop` stops it and its key store); then export the printed keys and run `scripts/probe_gateway.py --stream` and `make run-agent` yourself. The key store is a throwaway Postgres container + named volume (LiteLLM `/key/generate` needs a database): env-passed keys survive restarts, minted keys rotate per start (`GATEWAY_RESET_KEYS=1` wipes the store). `scripts/run_local_gateway.sh` owns config rendering; the LiteLLM image is pinned by digest there. These simulation scripts are local-dev only — never a product path, never in CI or the air gap.

---

### 3.7 Reasoning Performance, Query Complexity & Context Budgeting

Complexity classification, adaptive budgets, and the truncation analysis
live in `architecture.md` §4.4 (design) and `agent.md` §4 (knobs) — one
rule, one owner. Operational upshot: simple lookups stay fast and cheap;
complex diagnostics/procedures think longer and cost more GPU time. Tune
via the `LLM_REASONING_EFFORT_*` / `PROMPT_MAX_CONTEXT_CHARS*` Settings
(`agent.md` §7), never by editing prompts.

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

Dispatch, bypass rules, and failure mapping live in `retrieval.md` §6;
knob defaults live in `agent.md` §7. Ships default-off — retrieval is
identical to the hybrid+RRF baseline until explicitly enabled:

```bash
# Enable against a scorer endpoint (default order tries vLLM /v1/score,
# then falls back to /v1/rerank; set RERANK_ENDPOINT_ORDER=rerank_first
# for gateways — see probe_gateway.py below):
RERANK_ENABLED=true \
RERANK_BASE_URL=http://localhost:8001/v1 \
RERANK_MODEL=BAAI/bge-reranker-v2-m3 \
make run-agent
```

---

## 4. Standard Deployment Architecture (Air-Gap Production & Local Cluster Testing)

The hardened 5-stage deployment pipeline (`airgap-pack` -> `airgap-load` -> `airgap-deploy` -> `airgap-ingest` -> `airgap-smoke`) is the **canonical deployment standard across the entire project**. Both production air-gapped OpenShift and local testing environments adhere to this pipeline (using the same scripts, Helm chart, and Kustomize overlays, with adapted sizing and SCC for local test clusters).

### 4.1 Packaging on the Connected Host (Image Factory)

The sneakernet archive is packaged on the connected host. Connected `main` is the only image factory.

> [!TIP]
> **Automated CI Packaging:** Every commit merged to `main` on public GitHub automatically triggers the `airgap-package` workflow in `.github/workflows/e2e.yml` once container images have been built and pushed to GHCR. Any authenticated GitHub user with read access to the repository can download the pre-packaged, verified `sneakernet-bundle-<sha>` artifact (containing `qdrant-pdf-rag-<sha>.tar`, digest, `PACKING_RECORD.txt`, and `MANIFEST.txt`, retained for 90 days) directly from GitHub Actions runs. Note that pull request runs intentionally skip this packaging job; the first real sneakernet bundle for a given commit is generated upon its first merge to `main`.

To build the package manually on a connected Linux workstation:

```bash
git clone https://github.com/yonatan895/qdrant-pdf-rag.git
cd qdrant-pdf-rag
git checkout <main-sha>  # Full 40-character SHA matching built GHCR images

# Signing key: CI uses the SNEAKERNET_SIGNING_KEY secret (PEM private key),
# with SNEAKERNET_KEY_TRUSTED=true so MANIFEST records signed: true.
# Without the secret, CI packs with an ephemeral throwaway key and records
# signed: ephemeral. A maintainer creates the production key once with
# `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048` and stores
# the PEM as the secret.
# Trust model (read carefully): the offline signature binds the bundle
# members together — any member swap invalidates it — but verification
# against the in-bundle pubkey alone is TOFU and cannot prove which key
# signed. Authenticity roots, strongest first: (1) set SNEAKERNET_TRUSTED_PUB
# to a pubkey file obtained out of band and bootstrap.sh / load.sh refuse
# any bundle whose pub differs; (2) compare the bundle pubkey fingerprint
# against the org-published value (PACKING_RECORD.txt records the signing
# key fingerprint: `openssl pkey -in <key> -pubout | openssl sha256`);
# (3) download the tarball over HTTPS from GitHub Actions (TLS plus access
# control). Local rehearsal generates a throwaway:
#   openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out /tmp/signing.key
# Build air-gap sneakernet package:
SNEAKERNET_SIGNING_KEY=/tmp/signing.key make airgap-pack
```

This generates `dist/qdrant-pdf-rag-<sha>.tar` and its digest `dist/qdrant-pdf-rag-<sha>.tar.sha256`, containing:
1. Complete Git repository bundle (`repo.bundle`).
2. App container images (`qdrant-pdf-rag-agent`, `qdrant-pdf-rag-ingest`), with per-image digests bound in `MANIFEST.txt` and re-verified on load.
3. Vendored third-party Qdrant unprivileged image and Jaeger v2 image (tag + digest pinned in `images.txt`).
4. Vendored Helm chart (`charts/qdrant-1.19.0.tgz`).
5. Self-contained extraction bootstrap script (`bootstrap.sh`).
6. Manifest (`MANIFEST.txt`), Packing Record (`PACKING_RECORD.txt`), digest enumeration (`sbom.json`), offline signature (`SHA256SUMS.sig` + `sneakernet-signing.pub`), and member `SHA256SUMS`.

### 4.2 Transfer & Automated Bootstrap

Transfer the tarball and checksum file via approved sneakernet media to the air-gapped bastion host:

```bash
# 1. Verify tarball integrity BEFORE unpacking
sha256sum -c qdrant-pdf-rag-<sha>.tar.sha256

# 2. Extract tarball and run the automated bootstrap script:
tar -xf qdrant-pdf-rag-<sha>.tar
sh bootstrap.sh

# 3. Enter the initialized workspace:
cd qdrant-pdf-rag
```

The `bootstrap.sh` script automatically:
- Verifies all member checksums in `SHA256SUMS`.
- Clones the Git repository from `repo.bundle`.
- Populates `./dist` with image archives and manifests.
- Initializes `airgap.env` from `airgap.env.example` if not already present.

### 4.3 Configure Environment & Pre-Flight Validation

Edit `airgap.env` to configure your cluster environment:

> [!NOTE]
> Environment precedence (explicit env beats file, `AIRGAP_ENV` path beats
> local `./airgap.env`) lives in `deploy.md` §2 — one rule, one owner. Two
> re-run notes: `VAR=x make airgap-*` overrides a stale key, but a bare
> `make airgap-*` reuses whatever the file still holds — re-run
> `make airgap-validate` after editing `airgap.env` before touching the cluster.

```ini
# Internal image registry accessible to cluster nodes
INTERNAL_REGISTRY=registry.internal.enterprise:5000/mainframe-rag

# Target namespace
NAMESPACE=mainframe-rag

# Persistent storage class (Must be RWO Block, e.g. ocs-storagecluster-ceph-rbd)
STORAGE_CLASS=gp3-csi

# In-cluster inference endpoints (provided by LLM platform team).
# VLLM_BASE_URL takes the bare server origin (a trailing /v1 is tolerated:
# the deploy scripts strip it before deriving EMBED_BASE_URL).
VLLM_BASE_URL=http://vllm.inference.svc.cluster.local:8000
EMBED_MODEL=ibm-granite/granite-embedding-278m-multilingual
DENSE_DIM=768
LLM_MODEL_REASONING=ibm-granite/granite-20b-code-instruct

# Gateway virtual keys: name of ONE operator-created Secret holding the
# platform team's LiteLLM keys (unset = keyless). Never put key values here.
#GATEWAY_API_KEY_SECRET=gateway-api-keys

# Optional pull secret name (if registry requires credentials)
PULL_SECRET=internal-registry-pull-secret
```

#### Production model trio (reasoning / embed / rerank)

The platform team serves three model endpoints; this repo never hardcodes model names — wire all three in `airgap.env`:

| Role | URL key | Model key | Notes |
|---|---|---|---|
| Reasoning (`/v1/answer` only) | `LLM_BASE_URL` | `LLM_MODEL_REASONING` | Empty model = answers stay disabled. Raise `LLM_MAX_MODEL_LEN` past the 4096 default to the served context (tokenizer uses the server `/tokenize`, estimator fallback otherwise). Auth: `llm-api-key` from the `GATEWAY_API_KEY_SECRET` Secret (unset = keyless). |
| Embed (`/v1/search`, ingest) | `EMBED_BASE_URL` (defaults to `VLLM_BASE_URL`) | `EMBED_MODEL` + `DENSE_DIM` | `DENSE_DIM` is required and fail-closed: it must equal the served native dim (4096 for Qwen3-Embedding-8B). Collections are created at that width; a mismatch against an existing collection refuses with `DimMismatchError`. Auth: `embed-api-key` from the same Secret; the ingest Job reads it too. |
| Rerank (optional, default off) | `RERANK_BASE_URL` (defaults to `EMBED_BASE_URL`) | `RERANK_MODEL` | Served via a vLLM pooling server (`--runner pooling`, `/v1/score`; TEI `/v1/rerank` fallback). Point it at the reranker server — the embed default only fits single-server deployments. Lifespan logs a loud warning (never a refusal) when the endpoint is unreachable at startup. Auth: `rerank-api-key` from the same Secret. Leg order: `RERANK_ENDPOINT_ORDER=rerank_first` for gateways (run `probe_gateway.py` below to decide). |

Create the key Secret **before** `make airgap-deploy` (one Secret, four data keys; the contextual-gist key rides the ingest Job):

```bash
kubectl -n mainframe-rag create secret generic gateway-api-keys \
  --from-literal=llm-api-key='<platform-key>' \
  --from-literal=embed-api-key='<platform-key>' \
  --from-literal=rerank-api-key='<platform-key>' \
  --from-literal=context-llm-api-key='<platform-key>'
```

`make airgap-validate` verifies the Secret exists (when the namespace does) and refuses plaintext `*_API_KEY` values in `airgap.env`. Rotate by updating the Secret, then rollout-restart the agent (or re-run the ingest Job).

#### Pre-Flight Validation (`make airgap-validate`)

Before modifying any cluster state, run the pre-flight validation check to verify tools, required variables, storage class compliance (refusing NFS), and OpenShift SCC permissions:

```bash
make airgap-validate
```

To preview rendered templates and substitution rules without cluster credentials:
```bash
make airgap-dryrun
```

### 4.4 Load Images & Deploy Stack

Operators can either run the complete automated pipeline in one command or execute each stage individually:

#### Option A: Unified Pipeline Orchestrator (Recommended)

```bash
# Execute validate -> load -> deploy -> (optional ingest) -> smoke in sequence:
make airgap-pipeline

# Or with ingest:
CORPUS_PVC=my-corpus-pvc make airgap-pipeline
```

#### Option B: Step-by-Step Modular Execution

```bash
# 1. Verify internal member checksums and push images to internal registry
make airgap-load

# 2. Deploy Qdrant 3-replica cluster and Agent deployment
make airgap-deploy
```

#### OpenShift Security Context Constraints (SCC) Note
Qdrant's unprivileged image specifies `runAsUser: 1000` and `fsGroup: 3000` in `overlays/openshift/values.yaml`. On OpenShift clusters enforcing `restricted-v2` with `MustRunAsRange` (where project UIDs are dynamically allocated, e.g. `1000670000/10000`), admission controllers will reject static UID 1000 unless:
1. The `qdrant` ServiceAccount is granted `anyuid` SCC by a cluster admin:
   ```bash
   oc adm policy add-scc-to-user anyuid -z qdrant -n mainframe-rag
   ```
2. Or a project-specific override file is provided via `QDRANT_EXTRA_VALUES`:
   ```bash
   cat > qdrant-scc.yaml <<'EOF'
   containerSecurityContext:
     runAsUser: null
     runAsGroup: null
   podSecurityContext:
     fsGroup: null
   EOF
   QDRANT_EXTRA_VALUES=qdrant-scc.yaml make airgap-deploy
   ```

#### Qdrant Inter-Node Gossip (p2p TLS) Note
In `overlays/openshift/values.yaml`, `config.cluster.p2p.enable_tls: false` is set because cluster gossip is plaintext on the CNI; we do not mount `./tls/cert.pem` (avoiding crashloops on startup).

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

### 4.4.1 Tracing (optional, issue #83)

Tracing is opt-in: set `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` in
`airgap.env` **before** `make airgap-deploy`. deploy.sh then also renders and
applies `deploy/kustomize/jaeger` (Jaeger v2 all-in-one, badger storage on a
10Gi RWO block PVC, 14-day span TTL, ClusterIP only) and wires the endpoint
into the agent. Without the variable the agent keeps tracing off and no
Jaeger is deployed.

The sneakernet bundle always carries the Jaeger image (`images.txt` is a
pack-wide contract — every pinned image is mirrored on every pack), even
when tracing stays off; only the deployment is opt-in. The endpoint may be
given with or without the `/v1/traces` path — the agent accepts both.

Every exported span carries deploy identity as resource attributes:
`service.version` is the packed `IMAGE_SHA` automatically, and
`deployment.environment` comes from the optional
`OTEL_DEPLOYMENT_ENVIRONMENT` in `airgap.env` (e.g. `prod`; omitted when
unset) — so traces from lab and prod sharing one backend stay
unambiguous. The agent also honors the standard `OTEL_RESOURCE_ATTRIBUTES`
mapping underneath these explicit keys, and joins an upstream W3C
`traceparent` when the caller sends one.

View traces (port-forward only — Jaeger has no public Route, like Qdrant):

```bash
oc -n mainframe-rag port-forward svc/jaeger 16686:16686
# open http://localhost:16686, service "mainframe-rag-agent" (rename via OTEL_SERVICE_NAME
# in airgap.env — deploy.sh renders it, and strips the entry when unset so the
# agent default stands)
```

Backend posture (single-replica debug-grade, sample-all retention math,
exemplar deferral) is recorded in `docs/adr/0002-otel-backend-posture.md`.

### 4.5 Corpus Ingestion

#### Gateway readiness probe

After `make airgap-deploy` and before ingesting, prove the platform
gateway answers every configured leg from inside the cluster (same network
as the agent — the bastion itself may not reach it):

```bash
# Embed + reasoning + rerank reachability, dim match, auth diagnosis:
kubectl -n mainframe-rag exec deploy/rag-agent -- \
  python3 /app/scripts/probe_gateway.py
# Add --stream to also verify SSE [DONE] (needed only for ?stream=true TTFT).
```

The probe exits nonzero when a required leg fails (a 401 names the missing
`*_API_KEY`; a dim mismatch fails — ingest would refuse it too) and prints
the recommended `RERANK_ENDPOINT_ORDER` when reranking is enabled
(`score_first` for raw vLLM, `rerank_first` for gateways). Set the
recommendation in `airgap.env` and re-run `make airgap-deploy` before
ingesting. A missing `/tokenize` is informational only — the agent pins
its in-process estimator (expected behind LiteLLM).

Once the manual PDF corpus PVC is provisioned and populated:

```bash
# Launch one-shot ingest Job against corpus PVC
make airgap-ingest CORPUS_PVC=mainframe-manuals-pvc
```

Monitor ingest progress:
```bash
oc -n mainframe-rag logs -f job/ingest
```

### 4.6 Verification & Smoke Testing

```bash
# Run in-cluster smoke search
make airgap-smoke

# Or query specific message IDs:
QUERY="IEA500I operator message" make airgap-smoke
```

### 4.7 Local Cluster Testing Standard (Kind + Local Registry)

To test the deployment scripts and Kubernetes manifests locally without access to an OpenShift cluster, the project standardizes on **Kind** (Kubernetes-in-Docker) paired with a local registry container on port 5000.

> [!NOTE]
> Local cluster testing exercises the identical packaging scripts, container archives, Helm chart, and Kustomize overlays as production, but with adapted sizing and security contexts (1-replica Kind + mock/local vLLM rather than 3-replica OpenShift `restricted-v2`).

#### Step 1: Provision Local Registry & Kind Cluster

Kind nodes pull images inside Docker; pulling from `airgap-registry:5000` over HTTP requires configuring containerd mirrors in Kind:

```bash
# 1. Start local container registry container
docker run -d --restart=always -p 5000:5000 --name airgap-registry registry:2

# 2. Create Kind cluster with containerd mirrors for the local registry.
# Two mirror keys, one registry: host-side pushes address it as
# localhost:5000 (only localhost resolves on the host), while in-cluster
# image refs use airgap-registry:5000 (deploy.sh renders app images under
# INTERNAL_REGISTRY) AND localhost:5000 (the mock/corpus-gen manifests
# below). Both keys point at the same registry container over plain HTTP —
# drop either one and that naming family ErrImagePulls (proven 2026-09-06).
mkdir -p scratch
cat <<'EOF' > scratch/kind-config.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    [plugins."io.containerd.grpc.v1.cri".registry.mirrors]
      [plugins."io.containerd.grpc.v1.cri".registry.mirrors."localhost:5000"]
        endpoint = ["http://airgap-registry:5000"]
      [plugins."io.containerd.grpc.v1.cri".registry.mirrors."airgap-registry:5000"]
        endpoint = ["http://airgap-registry:5000"]
EOF
kind create cluster --name airgap --config scratch/kind-config.yaml

# 3. Connect local registry to Kind network (already connected on re-runs;
# the redirect keeps the re-run output clean)
docker network connect "kind" airgap-registry 2>/dev/null || true

# 4. Point kubectl at the new cluster and verify access (a fresh shell may
# have no current-context, in which case every kubectl call fails against
# localhost:8080)
kubectl config use-context kind-airgap
kubectl get nodes
```

#### Step 2: Pack Sneakernet Tarball

Packing pulls the app images from the registry tags for the checked-out SHA — it never builds locally. That means this step only works on a **green `main` SHA whose CI images already exist** (check out `main` first; an unmerged branch fails closed with `manifest unknown`). It also requires a signing key (§4.1); for rehearsal generate a throwaway:

```bash
# Checkout a green main SHA first (pack bundles HEAD and pulls its GHCR tags).
# Confirm the SHA's main workflows are green — e2e green means its images
# were pushed; a missing tag still fails closed at pack time with a 404.
# A stale local airgap.env IMAGE_SHA also fails closed here (explicit env
# beats the file, or update the file):
git checkout <green-main-sha>
gh run list --branch main --limit 5   # ci + e2e green for the SHA

# Rehearsal-only signing key (production uses the custodied key, §4.1):
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out /tmp/signing.key

# Build the complete image and bundle archive in dist/
SNEAKERNET_SIGNING_KEY=/tmp/signing.key make airgap-pack
```

#### Step 3: Load & Push to Local Registry

```bash
# Load archives from dist/ and push to localhost:5000 (airgap-registry)
INTERNAL_REGISTRY=localhost:5000 INSECURE_REGISTRY=true make airgap-load
```

#### Step 4: Configure `airgap.env` & Deploy Stack to Kind

Create a local single-replica override for Qdrant, copy `airgap.env.example` to `airgap.env`, and populate required values:

```bash
# Create local sizing override (1 replica for Kind test node; the 3x16Gi
# prod values cannot schedule on one node)
cat > scratch/qdrant-local.yaml <<'EOF'
replicaCount: 1
resources:
  requests:
    cpu: 200m
    memory: 512Mi
  limits:
    cpu: 2000m
    memory: 2Gi
EOF

# Copy example and configure environment
cp airgap.env.example airgap.env
```

Ensure `airgap.env` contains:
```sh
INTERNAL_REGISTRY=airgap-registry:5000
NAMESPACE=mainframe-rag
STORAGE_CLASS=standard
QDRANT_STORAGE_SIZE=1Gi
QDRANT_EXTRA_VALUES=scratch/qdrant-local.yaml
IMAGE_SHA=$(awk '/^sha: /{print $2}' dist/MANIFEST.txt)
VLLM_BASE_URL=http://vllm-mock:8000
EMBED_MODEL=mock-embed
DENSE_DIM=64
```
`DENSE_DIM` must equal the mock's `MOCK_DIM` below (both 64 here); any pair works as long as they match — ingest fails closed on a mismatch. `LLM_BASE_URL` / `LLM_MODEL_REASONING` are intentionally left unset: the Kind path proves ingest + `/v1/search`; `/v1/answer` stays disabled without a reasoning endpoint (the mock does serve `/v1/chat/completions` deterministically, but wiring answers in-cluster is unproven — see the e2e rehearsal, which asserts search only).

Deploy the in-cluster mock vLLM **before** `make airgap-deploy` (pods resolve `vllm-mock` over cluster DNS — no host networking needed; the "point at the host" alternative does not work from Kind pods without extra setup):

```bash
NS=mainframe-rag
SHA=$(awk '/^sha: /{print $2}' dist/MANIFEST.txt)
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create configmap mock-vllm --from-file=mock_vllm.py=scripts/mock_vllm.py
kubectl apply -n "$NS" -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-mock
  labels: {app: vllm-mock}
spec:
  replicas: 1
  selector: {matchLabels: {app: vllm-mock}}
  template:
    metadata: {labels: {app: vllm-mock}}
    spec:
      containers:
        - name: mock
          # Tarball-faithful: loaded from the bundle via airgap-load, never pulled.
          image: localhost:5000/qdrant-pdf-rag-ingest:${SHA}
          command: ["python3", "/cm/mock_vllm.py"]
          env:
            - {name: MOCK_DIM, value: "64"}
            - {name: PORT, value: "8000"}
          ports: [{containerPort: 8000}]
          readinessProbe:
            httpGet: {path: /healthz, port: 8000}
            initialDelaySeconds: 2
          volumeMounts: [{name: mock, mountPath: /cm}]
      volumes:
        - name: mock
          configMap: {name: mock-vllm}
---
apiVersion: v1
kind: Service
metadata:
  name: vllm-mock
spec:
  selector: {app: vllm-mock}
  ports: [{port: 8000, targetPort: 8000}]
EOF
kubectl -n "$NS" rollout status deploy/vllm-mock --timeout=180s
# Probe the mock before deploying: rollout only proves the pod is up, while a
# dim mismatch (MOCK_DIM vs airgap.env DENSE_DIM) surfaces much later at
# ingest, which fails closed. The lengths must agree (no wget in the image;
# python3 + stdlib urllib instead):
kubectl -n "$NS" exec deploy/vllm-mock -- python3 -c \
  "import json,urllib.request;print(len(json.load(urllib.request.urlopen(urllib.request.Request('http://localhost:8000/v1/embeddings',data=json.dumps({'model':'mock-embed','input':'probe'}).encode(),headers={'Content-Type':'application/json'}),timeout=10))['data'][0]['embedding']))"
# expect: 64 (== DENSE_DIM)
```
(This mirrors the `airgap-rehearsal` job in `.github/workflows/e2e.yml`, which is the proven reference when this section and CI disagree.)

Deploy the stack (Qdrant StatefulSet, Jaeger v2, Agent Deployment):
```bash
make airgap-deploy
```

#### Step 5: Ingest Corpus & Run Smoke Test

The ingest Job mounts a caller-supplied corpus PVC read-only — the scripts never create it. For rehearsal, create a `corpus` PVC and fill it with synthetic PDFs via a generator Job (same tarball-faithful ingest image as the mock above):

```bash
NS=mainframe-rag
SHA=$(awk '/^sha: /{print $2}' dist/MANIFEST.txt)
kubectl -n "$NS" apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: corpus}
spec:
  accessModes: ["ReadWriteOnce"]
  resources: {requests: {storage: 1Gi}}
  storageClassName: standard
---
apiVersion: batch/v1
kind: Job
metadata: {name: corpus-gen}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: gen
          image: localhost:5000/qdrant-pdf-rag-ingest:${SHA}
          command: ["/bin/sh", "-ec"]
          args:
            - |
              python3 /app/scripts/make_synthetic_pdf.py --out /corpus/SA22-0000-00_outline.pdf
              python3 /app/scripts/make_synthetic_pdf.py --plain --out /corpus/plain-widget-notes.pdf
              ls -la /corpus
          volumeMounts: [{name: corpus, mountPath: /corpus}]
      volumes:
        - name: corpus
          persistentVolumeClaim: {claimName: corpus}
EOF
kubectl -n "$NS" wait --for=condition=complete job/corpus-gen --timeout=300s
```

Then launch ingest and smoke-test (shrink the prod-sized ingest scratch for the single Kind node):

```bash
# Launch one-shot ingest Job against corpus PVC:
CORPUS_PVC=corpus INGEST_WORK_SIZE=2Gi make airgap-ingest

# Run in-cluster smoke search:
make airgap-smoke
```

#### Teardown Local Test Cluster

```bash
kind delete cluster --name airgap
docker rm -f airgap-registry
```

---

## 5. Day-2 Operations & Maintenance

### 5.1 Health Check API

The agent exposes `/healthz` for OpenShift liveness/readiness probes
(contract: `agent.md` §1 — `ok`/`degraded`/`503 qdrant_unready`, upstream
bodies stay server-side):

```bash
curl -s http://rag-agent.mainframe-rag.svc:8080/healthz
```

```json
{
  "status": "ok",
  "qdrant": true,
  "embed": true
}
```

### 5.2 Agent Endpoints

Endpoint shapes, error envelopes, SSE framing, and timing headers live in
`agent.md` (§1–§3) — one rule, one owner. Copy-paste probes:

#### `POST /v1/search`
Retrieves ranked manual chunks with normalized citations without invoking an LLM.

```bash
curl -X POST http://rag-agent:8080/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "IEA500I IOSCMDS COMMAND REJECTED", "limit": 5}'
```

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

#### Streaming (`?stream=true`)
Both the query parameter and the body field (`"stream": true`) enable
server-sent events; the query parameter wins when both are set. Frame
contract (`token*` → exactly one `final`; `error` ends without `final`):
`agent.md` §3.

Citation validation runs on the accumulated text exactly as in JSON mode: the citations in the `final` event are byte-identical to the non-streaming response for the same request.

### 5.3 Common Troubleshooting Scenarios

| Issue | Symptom | Remediation |
|---|---|---|
| **Dimension Mismatch** | Ingest/Search fails with 400 dimension mismatch | Verify `DENSE_DIM` in `airgap.env` matches `EMBED_MODEL` on vLLM. |
| **Qdrant Unready** | `/healthz` returns `503 qdrant_unready` | Check Qdrant pod logs (`oc logs qdrant-0`); check block PVC mount. |
| **NFS Storage Refusal** | `make airgap-deploy` fails validation | Set `STORAGE_CLASS` to an RWO block driver (Ceph RBD / SAN / EBS). |
| **Hash Mode in Prod** | Scripts fail closed with `EMBED_MODE=hash forbidden` | Remove `EMBED_MODE` from production environment; provide valid vLLM endpoint. |
| **Registry Certificate Error** | `skopeo copy` fails with `x509: certificate signed by unknown authority` | Set `SKOPEO_ARGS=--dest-tls-verify=false` or `INSECURE_REGISTRY=true` in `airgap.env`. |
| **OpenShift SCC Rejection** | Pod `qdrant-0` fails with `unable to validate against any security context constraint` | Grant `anyuid` SCC (`oc adm policy add-scc-to-user anyuid -z qdrant -n <ns>`) or supply `QDRANT_EXTRA_VALUES` to clear static `runAsUser`. |
| **PVC Multi-Attach Error** | `job/ingest` fails with `Multi-Attach error for volume` on corpus PVC | Ensure any previous writer pod has released the PVC, or use a ReadOnlyMany volume. |
| **Qdrant P2P CrashLoop** | Pod `qdrant-0` fails with `No such file or directory` looking for `cert.pem` | Ensure `config.cluster.p2p.enable_tls: false` in `values.yaml` (gossip is plaintext on CNI without `./tls/cert.pem`). |
| **K8s Manifest Integer/Boolean Error** | `Invalid value: "string", expected integer/boolean` | Ensure numeric/boolean env vars (`DENSE_DIM`, `INGEST_WORKERS`, `RERANK_ENABLED`) are explicitly quoted in rendered manifests. |
| **Degraded `/healthz` Smoke Failure** | `make airgap-smoke` exits 1 with `FAIL: /healthz probe did not report ok` | Pre-flight probe failed closed; check Qdrant and vLLM connectivity inside the cluster. |
| **Qdrant 401 After Reinstall** | `/v1/search` fails with `401 Invalid API key or JWT` after Qdrant was reinstalled or re-`helm upgrade`d | The chart regenerates the `<release>-apikey` secret on reinstall while running agent pods keep the old key in env. Roll the agent: `kubectl -n <ns> rollout restart deploy/rag-agent` and wait for rollout before smoking again. |
| **Stale dist/ MANIFEST** | `make airgap-validate` / `-deploy` / `-ingest` / `-dryrun` fail with `IMAGE_SHA=<sha> does not match packed MANIFEST sha` | `dist/` is gitignored build output that persists across checkouts — the MANIFEST inside is from an older pack (only `pack` regenerates it; the other steps just read it). Repack at the current HEAD, or clear the stale `dist/` before re-running. |
| **Stale airgap.env IMAGE_SHA** | `make airgap-pack` / `-load` fail with `IMAGE_SHA=<sha> is not the checked-out commit` right after checking out a new SHA | `airgap.env` is gitignored local state from a previous rehearsal — its `IMAGE_SHA` no longer matches HEAD. Explicit env beats the file (`IMAGE_SHA=$(git rev-parse HEAD) make airgap-pack`), or update the file. |
| **Stale dist/ tarballs fill disk** | `pack`/`load` fail with no-space errors after several rehearsals | Every pack leaves a ~1.5 GB `qdrant-pdf-rag-<sha>.tar` in gitignored `dist/`; only the MANIFEST-pinned one is live. Delete superseded tarballs (keep the `.tar.sha256` of the live one) — pack never prunes. |
| **Kind ErrImagePull on localhost:5000** | mock/corpus-gen pods fail with `dial tcp [::1]:5000: connect: connection refused` | The Kind `containerdConfigPatches` in §4.7 must mirror **both** `localhost:5000` and `airgap-registry:5000` to the registry container — one key per naming family used by the manifests. Recreate the cluster with the documented config (containerd mirrors are set at creation). |
