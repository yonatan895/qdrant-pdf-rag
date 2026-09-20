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
- **Data Storage:** Qdrant persistent volumes **must** use RWO block storage (NFS-looking `STORAGE_CLASS` values are refused; the snapshot storage class falls back to it and is not checked separately).
- **Inference Separation:** The platform team owns the model servers and gateway. Agent and ingest consume its authenticated HTTP endpoints; this product does not deploy that tier.

---

## 2. Prerequisites

### Local Development
- **Operating System:** Linux / WSL2 (linux-amd64 pinned Task artifact)
- **Python:** CPython **3.14 GIL** (`python3.14 --version`). Do not use free-threading (`3.14t`).
- **Container Runtime:** Docker or Podman (for running integration simulation tests).
- **Tools:** `git`, `curl`, POSIX shell, `tar`, `sha256sum`; the pinned Task runner is installed explicitly below.

### Disconnected / Air-Gapped Bastion
- **Host runner:** signed bundle bootstrap installs verified Task into `.tools/bin/task`; no Make, preinstalled Task, Go, application Python or internet is needed for bootstrap. Keep `git`, POSIX shell, `tar`, `sha256sum` and `openssl` available for its verification steps.
- **OpenShift Client:** `oc` (v4.12+) or `kubectl`.
- **Helm:** `helm` v3.12+ (do **not** run `helm repo add` in the air-gap; chart is vendored at `charts/qdrant-1.19.0.tgz`).
- **Image Tooling:** `skopeo` (for loading archives into the internal registry).
- **Cluster Permissions:** Access to create/manage workloads in the target namespace with `restricted-v2` SCC.

---

## 3. Local Development & Simulation Workflow

For the measured Windows/WSL + real-model OpenShift topology, use
[local-crc-environment.md](local-crc-environment.md). It includes the host memory
profile, registry, TLS, Windows clients and local sizing. The selected operating
mode is the [complementary CRC/Kind rehearsal](local-release-fallback.md).

### 3.1 Setup Environment

```bash
# 1. Clone repository
git clone https://github.com/yonatan895/qdrant-pdf-rag.git
cd qdrant-pdf-rag

# 2. Install the pinned host runner (connected host only)
sh scripts/tools/install-task.sh

# 3. Bootstrap virtual environment and install locked dependencies
sh scripts/tools/run-task.sh dev:setup PY=python3.14

# 4. Download and cache BM25 model weights locally (first run downloads
# Qdrant/bm25 via FastEmbed — needs HuggingFace reachability; afterwards the
# weights live in bundles/bm25-weights and are baked into images)
sh scripts/tools/run-task.sh artifacts:bm25
```

Fresh-machine heavylift to budget for: the venv install, the BM25 download above, the Qdrant image pull on first `sh scripts/tools/run-task.sh qa:sim`, and the `vllm-openai` pull on first `sh scripts/tools/run-task.sh local:llm` (or `local:embed` / `local:rerank`) (the pinned `v0.28.0` image is ≈29 GB). The `HF_TOKEN` path in §3.6 additionally needs gated-repo approval (e.g. Gemma) before the token works — request access first.

### 3.2 Code Quality & Unit Tests

Run static analysis, type checking, and unit test suites:

```bash
# Run linters (ruff), type checking (mypy), and unit tests (pytest)
sh scripts/tools/run-task.sh qa:check

# Or run individual verification steps:
sh scripts/tools/run-task.sh qa:unit        # Fast unit tests (mocked clients, synthetic data)
sh scripts/tools/run-task.sh qa:lint        # Ruff linting
sh scripts/tools/run-task.sh qa:typecheck   # Mypy static typing
```

### 3.3 Integration Simulation Tier

The simulation tier spins up an ephemeral Docker container running the pinned unprivileged Qdrant image (`docker.io/qdrant/qdrant:v1.19.0-unprivileged`), parses runtime-generated synthetic PDFs, ingests them, and exercises the agent API against a deterministic mock LLM server:

```bash
# Run simulation suite
sh scripts/tools/run-task.sh qa:sim

# Optional: Run a persistent local Qdrant container on port 6333
sh scripts/tools/run-task.sh local:qdrant:up

# Teardown local Qdrant container
sh scripts/tools/run-task.sh local:qdrant:down
```

**Load tier** (`sh scripts/tools/run-task.sh qa:load`, `tests/test_load_tier.py`) runs the same composition — runtime PDFs, hash-mode ingest into the pinned Qdrant image, a real uvicorn agent (`LLM_STREAM=true`) plus the deterministic mock LLM — and asserts absolute contracts under concurrency instead of correctness: zero request errors and zero missing `Server-Timing` headers on `/v1/search` and `/v1/answer`, per-stream SSE integrity on `/v1/answer?stream=true` (token deltas, exactly one `final` with citations, no `error` event), citation parity across stream/search/JSON shapes, fixed error envelopes with no leaked internals, and determinism after load. The chaos leg runs the same streams against an abort-storm mock (`MOCK_ERROR_RATE`): every stream must classify as complete XOR aborted, aborted streams carry `event: error` with no `final`, and each leaves exactly one `stream_truncated` alert. The TTFT leg runs against a paced mock (`MOCK_TTFT_MS`): agent `ttft_ms` never precedes the model's first byte. Never cross-environment latency comparisons. Knobs (CI-sane defaults): `LOAD_SEARCH_CONCURRENCY` / `LOAD_SEARCH_DURATION_S` / `LOAD_ANSWER_CONCURRENCY` / `LOAD_ANSWER_DURATION_S` / `LOAD_STREAMS` / `LOAD_STREAM_WORKERS`; `QDRANT_SIM_URL` reuses a running server.

```bash
# Run load tier (fail-closed: no skips; docker/startup/zero-request failures raise)
sh scripts/tools/run-task.sh qa:load
```

### 3.4 Retrieval Evaluation Gates & Quality Tiers

Tier map, thresholds, and gate semantics live in `eval.md` (§1–§4); the
rungs your change class owes live in `live-stack.md` (§0, §3). This
section keeps only the runnable commands.

#### Automated L1 Retrieval Gate (`sh scripts/tools/run-task.sh eval:gate-l1`)

The L1 gate runs automatically on every PR in GitHub Actions and GitLab CI:

```bash
# Run L1 retrieval evaluation gate locally (starts ephemeral Qdrant simulator if needed)
sh scripts/tools/run-task.sh eval:gate-l1

# Or run the script directly:
python scripts/gate_l1.py --out bundles/eval-report.json --delta bundles/eval-delta.md
```

- **Execution Invariant:** Zero committed PDFs. An original synthetic PDF corpus covering the golden dataset expectations is generated at runtime in a temporary directory and ingested in hash mode.
- **Fail-Closed Verification:** Fails nonzero on any query failure or metric regression — ratios in `eval.md` §2 (strict `identifier`/`message_id` recall@1, `must_not.violations == 0` absolute).
- **PR Delta Reporting:** Automatically posts or updates a markdown delta table comment on the PR (GitHub) or merge request note (GitLab).

#### Paraphrase Retrieval Instrument (`sh scripts/tools/run-task.sh eval:paraphrase`)

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
QDRANT_URL=http://localhost:6333 QDRANT_COLLECTION=paraphrase-manuals sh scripts/tools/run-task.sh eval:paraphrase
```

Baselines (`evals/baseline-paraphrase.json` hash, `evals/baseline-paraphrase-vllm.json` vllm) gate the same tolerances as the main set (`eval.md` §2). Uses: contextual-prefix A/B, reranker on/off A/B, dense-prefix tuning. Not wired into CI (no cluster, no embed server there).

#### GPU Story for L2 and L3 Tiers (Operational Strategy)

Standard CI runners are CPU-only; L2/L3 need the live GPU stack. Three
options, cheapest first:

1. **RC gate (primary):** run `sh scripts/tools/run-task.sh eval:harness:l2` / `sh scripts/tools/run-task.sh eval:harness:l3` on lab
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
EMBED_MODE=hash QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=local-corpus sh scripts/tools/run-task.sh eval:retrieval

# Re-record committed accuracy baseline (dedicated PR only, per AGENTS.md)
sh scripts/tools/run-task.sh eval:baseline

# Run performance benchmarking
sh scripts/tools/run-task.sh eval:bench

# Re-record committed benchmark baseline (dedicated PR only)
sh scripts/tools/run-task.sh eval:bench-baseline
```

### 3.5 Developer Reporting & Interactive Query Assistant (`sh scripts/tools/run-task.sh local:ask` / `sh scripts/tools/run-task.sh local:query`)

Mainframe RAG provides interactive terminal REPLs and single-command CLI utilities for inspecting retrieval results and testing LLM reasoning. Local developer defaults (`EMBED_MODE=hash`, `ALLOW_HASH_MODE=true`, `QDRANT_URL=http://localhost:6333`, and auto-detection of local vLLM models) are applied automatically:

```bash
# 1. Interactive conversational Q&A assistant (Reasoning LLM + Qdrant retrieval)
sh scripts/tools/run-task.sh local:ask

# 2. Ask a single question directly on the command line
sh scripts/tools/run-task.sh local:ask QUERY="What is message ICH408I?"

# 3. Query a specific collection
sh scripts/tools/run-task.sh local:ask QUERY="What is message IEA500I?" COLLECTION=local_vllm_test_corpus

# 4. Launch pure retrieval debugger REPL (inspect rank scores and chunk payloads without calling LLM)
sh scripts/tools/run-task.sh local:query

# 5. Inspect a single query in pure search mode
sh scripts/tools/run-task.sh local:query QUERY="IEA500I" COLLECTION=local_vllm_test_corpus

# 6. Export query results to self-contained HTML or JSON
PYTHONPATH=. .venv/bin/python scripts/query_demo.py --answer --query "IEA500I" --format html --out bundles/answer-IEA500I.html
PYTHONPATH=. .venv/bin/python scripts/query_demo.py --answer --query "IEA500I" --format json --out bundles/answer-IEA500I.json
```

#### Local Development Environment Defaults

When running local tooling (`sh scripts/tools/run-task.sh local:ask`, `sh scripts/tools/run-task.sh local:query`, `test_local_e2e_vllm.py`), the following defaults are automatically applied if unset in the environment:

| Variable | Local Dev Default | Air-Gap / Prod Rule |
|---|---|---|
| **`QDRANT_URL`** | `http://localhost:6333` | Internal OpenShift service DNS (e.g. `http://qdrant:6333`) |
| **`QDRANT_COLLECTION`** | `mainframe_manuals` (or CLI `COLLECTION=...`) | `mainframe_manuals` |
| **`EMBED_MODE`** | `"hash"` (auto-set if `EMBED_BASE_URL` is unset) | `"vllm"` (mandatory; prod fails closed on hash mode) |
| **`ALLOW_HASH_MODE`** | `"true"` (auto-set for local test utilities) | `false` (fails closed to prevent hash retrieval in prod) |
| **`LLM_BASE_URL`** | Source the real gateway's `GATEWAY_ENV_FILE` (`http://localhost:4000/v1`) | Platform gateway endpoint from `airgap.env` |
| **`LLM_MODEL_REASONING`** | Auto-resolved from `/v1/models` on local server | Dedicated reasoning model specified in `airgap.env` |
| **`LLM_REASONING_EFFORT_SIMPLE`** | `"low"` | `"low"` (preserves latency on factoid lookups) |
| **`LLM_REASONING_EFFORT_COMPLEX`** | `"high"` | `"high"` (enforces deep multi-step deliberation) |
| **`LLM_TEMPERATURE`** | `0.2` | `0.2` (deterministic, grounded technical reasoning) |
| **`PROMPT_MAX_CONTEXT_CHARS`** | `8000` | `8000` (for simple queries) |
| **`PROMPT_MAX_CONTEXT_CHARS_COMPLEX`** | `4500` | `4500` (reserves ~2.6k token headroom for reasoning) |
| **`RERANK_ENABLED`** | `false` | `false` (cross-encoder rerank ships default-off; see §3.11) |
| **`RERANK_BASE_URL`** | Gateway handoff URL | Platform gateway scoring/rerank leg (required when `RERANK_ENABLED=true`) |
| **`RERANK_MODEL`** | `BAAI/bge-reranker-v2-m3` | Must match the served reranker model |
| **`LLM_STREAM`** | `true` via `sh scripts/tools/run-task.sh local:agent` | `false` (production default; enable only where TTFT metrics are wanted) |
| **`UI_ENABLED`** | `"true"` via `sh scripts/tools/run-task.sh local:stack`; unset via `sh scripts/tools/run-task.sh local:agent` (fail-closed 404) | `"true"` from the production chart (console served; only the external Route is OAuth-protected) |
| **`CHAT_CONDENSE_ENABLED`** | `false` (default) | `false` (enabling is a dedicated default-flip PR; `sh scripts/tools/run-task.sh eval:chat` is the evidence) |

#### REPL Controls & Options
* **Interactive Mode Switch (`:mode`)**: Type `:mode` inside the REPL to toggle dynamically between `search` (pure vector/BM25 retrieval preview) and `answer` (retrieval + LLM reasoning generation).
* **Citation Status Indicators**: The output clearly indicates whether citations were parsed from a formal `Citations:` section (`[explicit Citations: section]`) or resolved from inline bracketed references (`[inferred from excerpt [1, 2]]`).
* **Formatted Scripts**: JCL and REXX scripts produced by the reasoning model are automatically extracted and syntax-highlighted in terminal output and rendered inside copy-friendly code blocks in HTML exports.

---

### 3.6 Local vLLM Inference & GPU Acceleration (RTX 5060 / 8GB VRAM)

The repository provides a hardened launcher script ([`scripts/run_local_vllm.sh`](../scripts/run_local_vllm.sh)) and Task launch commands for serving local reasoning and dense embedding models via Docker with NVIDIA GPU pass-through:

#### Key Launcher Features
* **Pinned Container Image**: Defaults to `vllm/vllm-openai:v0.28.0` (built with CUDA 12.8+, supporting NVIDIA Blackwell architectures like the RTX 5060 Laptop GPU and Gemma-4). The pinned tag implements every flag the script passes — v0.28.0 removed `--task`, so the embed branch passes `--runner pooling --convert embed`.
* **Dual-Model 8GB VRAM Co-Residency** (defaults resolved from the `mainframe_rag.serve` Budget `LOCAL_RT_8GB` profile — single source of truth, not script constants; the launcher preflights the full co-resident pack with `--check-pack` before starting either server):
  - **Reasoning Model (Port 8000)**: `GPU_MEM=0.64` (~5.2 GB VRAM allocation).
  - **Embedding Model (Port 8001)**: `GPU_MEM=0.33` with `--enforce-eager` (~2.7 GB VRAM budget; measured 1.29 GiB spare KV at startup). Explicit `GPU_MEM=`/`MAX_LEN=`/`SEQS=`/`ROLE=` always win.
  - Verify actual residency and long inputs on the selected GPU. Eager execution removes compilation/CUDA-graph workspace, but allocations alone do not establish fit.
  - *Solo Runs*: For dedicated reasoning benchmarks, `GPU_MEM=0.85 sh scripts/tools/run-task.sh local:llm` restores maximum KV cache capacity.
* **8GB VRAM Optimizations**:
  - `--limit-mm-per-prompt '{"image":0,"audio":0}'` rejects those modalities; it does not establish the memory saving. The opt-in `LOCAL_CRC_32GB` profile additionally uses `--language-model-only` and a zero multimodal processor cache.
  - `--max-num-seqs 1`: Bounds concurrent sequence allocation to prevent out-of-memory spikes.
  - `--enable-prefix-caching`: on for the LOCAL reasoning server (Budget `prefix_cache`; vLLM already caches by default — the pin guards flips, hit rate measured for issue #80); off for embed.
  - `--max-num-batched-tokens` (embed server): capped at the Budget window so the memory-profiling peak stays bounded; it does not follow a `MAX_LEN` operator override (erring small is the safe side).
  - `MAX_LEN=4096` for **both** servers: the reasoning prompt budget requires it, and a 2048 embed window was rejected by tokenizer sweep — the worst-case embedded string (chunk header + a `SECTION_MAX_CHARS=3500` body with the 400-char split seed) measures ~2,043 tokens at ~2.0 chars/token on syntax-dense text. The budget is pinned hermetically by `tests/test_embed_budget.py`.
* **Gemma-4 Support**: Automatically configures `--tool-call-parser gemma4`, `--reasoning-parser gemma4`, and `--chat-template /vllm-workspace/examples/tool_chat_template_gemma4.jinja`.
* **Task launch contract**: `local:llm`, `local:embed` and `local:rerank` pass the role and repository venv `BUDGET_PYTHON` per invocation; a missing `.venv` fails with an explicit setup instruction. Re-run the tokenizer sweep before changing chunk constants or the embed-text header.
* **Embedding Model Detection**: Model names matching `*embed*`/`*Embed*` (e.g. `Qwen/Qwen3-Embedding-0.6B`) derive `ROLE=embed` (overridable; `sh scripts/tools/run-task.sh local:llm` (or `local:embed` / `local:rerank`) passes `ROLE` explicitly) and get the Budget pooling-runner serving shape automatically.
* **WSL2 Compatibility**: Exports `VLLM_WSL2_ENABLE_PIN_MEMORY=1` for host memory stability.
* **Safe Secrets**: Passes `HF_TOKEN` via `-e HF_TOKEN` without exposing secret tokens on command-line argument lists.

#### Starting Local vLLM Servers

**1. Start the Reasoning Model Server (Port 8000):**
```bash
# Offline weights directory (recommended):
MODEL=/path/to/models/gemma-4-E4B-it-qat-mobile-ct sh scripts/tools/run-task.sh local:llm

# Or via HuggingFace Hub:
HF_TOKEN="<your-token>" MODEL=google/gemma-4-E4B-it-qat-mobile-ct sh scripts/tools/run-task.sh local:llm
```

**2. Start the Dense Embedding Server (Port 8001):**
```bash
# Offline weights directory (recommended):
MODEL=/path/to/models/Qwen3-Embedding-0.6B sh scripts/tools/run-task.sh local:embed

# Or via HuggingFace Hub:
MODEL=Qwen/Qwen3-Embedding-0.6B sh scripts/tools/run-task.sh local:embed
```

#### Serving configurations (pick one pack per 8GB card)

One server per Task invocation (each blocks its shell — run each in its own terminal or background it). `BUDGET_PROFILE` selects the pack; the launcher preflights the full pack (`--check-pack`) and fails closed on deficits. Explicit `GPU_MEM=`/`MAX_LEN=`/`SEQS=` always win over resolved values.

| Goal | Profile (default `LOCAL_RT_8GB`) | Commands | Notes |
|---|---|---|---|
| Answer quality (big reasoning + embedding) | `LOCAL_RT_8GB` | `sh scripts/tools/run-task.sh local:llm` (:8000, E4B) + `sh scripts/tools/run-task.sh local:embed` (:8001) | Default pair. No room for a third leg (measured 7.0 GB resident). |
| Windows CRC plus the current two models | `LOCAL_CRC_32GB` | Both launch targets with `BUDGET_PROFILE=LOCAL_CRC_32GB` | 0.54/0.43, eager, one sequence, 4096 tokens; [host setup and measurements](local-crc-environment.md). |
| Full topology (reasoning + embedding + ranking) | `TRIPLE_8GB` | Above with `MODEL=Qwen/Qwen2.5-0.5B-Instruct GPU_MEM=0.20 MAX_LEN=4096 SEQS=1` on :8000, plus `sh scripts/tools/run-task.sh local:rerank` (:8002) | 0.5B answers are weak — plumbing/rerank coverage only. E4B triple demonstrably does not fit; resolve refuses it. |
| Retrieval + ranking, no LLM | `RANK_EMBED_8GB` | `sh scripts/tools/run-task.sh local:embed` (:8001) + `sh scripts/tools/run-task.sh local:rerank` (:8002) | Rerank A/B and `--rerank` evals without spending VRAM on reasoning. |
| Reasoning + ranking (no vLLM embed) | — | Unsupported | Hash embed mode pins `HashReranker` by design (determinism), so a GPU reranker is unreachable there — see issue #193. |

Reranked search also needs `RERANK_ENABLED=true RERANK_BASE_URL=http://127.0.0.1:4000/v1 RERANK_MODEL=BAAI/bge-reranker-v2-m3` on the consumer side (`query-demo`, eval `--rerank`, agent env). Launch order on a cold card: reasoning → embed → rerank (a 4k-context server fails KV init against leftovers; the profiles declare this allocation order).

#### Local Production Simulation (`sh scripts/tools/run-task.sh local:stack`)

The ownership contract is explicit: in production the platform team owns the model tier (vLLM + LiteLLM) and this repo owns Qdrant + ingest + retrieval + the agent, reaching every model leg only over the gateway HTTP contract. `sh scripts/tools/run-task.sh local:stack` reproduces that **complete topology on one machine** — pinned Qdrant + Jaeger + the real LiteLLM gateway (digest-pinned) in front of the enabled local vLLM backends + the FastAPI agent — and probes every leg through the gateway **and verifies a trace landed in Jaeger** before declaring the stack up. Agent and ingest never call vLLM directly, so the wire contract under test is the production one.

Prerequisites: Docker, `.venv`, reasoning on :8000 and embeddings on :8001. Start reranking on :8002 only when enabled. The current two-model rehearsal explicitly uses `RERANK_ENABLED=false`. Qdrant is started via `sh scripts/tools/run-task.sh local:qdrant:up` (the pinned-image owner) when unreachable; `sh scripts/tools/run-task.sh local:qdrant:down` stops it.

```bash
# Full stack: Qdrant -> Jaeger -> gateway -> probe -> (optional ingest) -> agent -> smoke -> trace check
RERANK_ENABLED=false sh scripts/tools/run-task.sh local:stack
RERANK_ENABLED=false CORPUS_DIR=output/demo-pdfs sh scripts/tools/run-task.sh local:stack     # also ingest through the gateway
LOCAL_STACK_DRYRUN=1 sh scripts/tools/run-task.sh local:stack            # ordered plan only; no docker/network
```

Tracing is part of the stack, not a flag: the agent and (when `CORPUS_DIR` is set) the ingest run export OTLP to Jaeger, and a `v1.search` span must land before the stack reports up. Jaeger reuses an instance already answering on the UI port (an operator-managed one is left alone) or starts the digest-pinned owner (`scripts/run_local_jaeger.sh`); the real local LiteLLM gateway also exports its spans there so the gateway hop appears in the waterfall. The Jaeger **browser UI** is at `http://127.0.0.1:16686`.

The operator console (ADR-0004) is part of the stack by default: the agent starts with `UI_ENABLED=true`, the up sequence smoke-checks `GET /ui` (HTTP 200), and the banner prints the console URL (`http://127.0.0.1:8080/ui`). Set `UI_ENABLED=false` to exercise the fail-closed 404 route set; `sh scripts/tools/run-task.sh local:agent` alone honors `UI_ENABLED` without a default.

On exit (Ctrl-C) the agent, the gateway, and an owned Jaeger stop; Qdrant stays for `sh scripts/tools/run-task.sh local:qdrant:down`. Ports: agent 8080 (`LOCAL_AGENT_PORT`), gateway 4000 (`GATEWAY_PORT`), Jaeger UI 16686 / OTLP 4318 (`JAEGER_PORT`, `JAEGER_OTLP_PORT`), Qdrant `QDRANT_URL` (default `http://127.0.0.1:6333`). Ephemeral keys are written mode 600 to a temp env file (`GATEWAY_ENV_FILE`, default `/tmp/local-stack-gateway-<port>.env`) — never inside the repo, never committed.

The gateway contract the stack exercises:

* **One origin per leg, model-id routing**: `EMBED_BASE_URL`, `LLM_BASE_URL`, and `RERANK_BASE_URL` all point at `http://localhost:4000/v1`; the requested `model` id selects the backend.
* **Per-leg Bearer virtual keys**: the agent/ingest `*_API_KEY` values are LiteLLM virtual keys; missing/unknown keys get 401 on every route (including the rerank legs), so key wiring is exercised for real.
* **Both rerank legs**: native `/v1/rerank` via the `hosted_vllm/` provider, plus a `/v1/score` pass-through to the vLLM backend (LiteLLM has no native score route). `scripts/probe_gateway.py` prints the recommended `RERANK_ENDPOINT_ORDER` after probing both.
* **Tokenizer**: `/tokenize` stays 404 behind the gateway — the agent pins its in-process estimator after one warning (expected, not a fault).

For single-component debugging, `sh scripts/tools/run-task.sh local:jaeger:up` / `sh scripts/tools/run-task.sh local:jaeger:down` manage just the trace backend, and `sh scripts/tools/run-task.sh local:gateway:up` keeps the gateway in the foreground (`sh scripts/tools/run-task.sh local:gateway:down` stops it and its key store); then export the printed keys and run `scripts/probe_gateway.py --require-reasoning --stream` and `sh scripts/tools/run-task.sh local:agent` yourself. The key store is a throwaway Postgres container + named volume (LiteLLM `/key/generate` needs a database): env-passed keys survive restarts, minted keys rotate per start (`GATEWAY_RESET_KEYS=1` wipes the store). `scripts/run_local_gateway.sh` owns gateway config rendering; the LiteLLM image is pinned by digest there. The model/gateway launcher is local-only. CI supplies its own gateway deployment and deterministic model computation; neither deployment belongs on a product path. The shared strict-finish module has a gateway-only LiteLLM import exception and is excluded from application images.

---

### 3.7 Reasoning Performance, Query Complexity & Context Budgeting

Complexity classification, adaptive budgets, and the truncation analysis
live in `architecture.md` §4.4 (design) and `agent.md` §4 (knobs) — one
rule, one owner. Operational upshot: simple lookups stay fast and cheap;
complex diagnostics/procedures think longer and cost more GPU time. Tune
via the `LLM_REASONING_EFFORT_*` / `PROMPT_MAX_CONTEXT_CHARS*` Settings
(`agent.md` §7), never by editing prompts.

---

### 3.8 Automated Local End-to-End Suite (`sh scripts/tools/run-task.sh qa:vllm-e2e`)

To verify the entire RAG pipeline from PDF generation and dense/sparse ingestion to HTTP retrieval and grounded LLM reasoning:

```bash
# Use the trusted local handoff and the checks in live-stack.md first.
. "$GATEWAY_ENV_FILE"
: "${EMBED_MODEL_REVISION:?gateway handoff must include the local revision label}"
: "${DENSE_DIM:?export the selected embedding dimension}"
export DENSE_DIM
export RERANK_ENABLED=false
sh scripts/tools/run-task.sh qa:vllm-e2e \
  MODEL="$LLM_MODEL_REASONING" VLLM_URL="$LLM_BASE_URL" \
  EMBED_MODEL="$EMBED_MODEL" EMBED_URL="$EMBED_BASE_URL" DENSE_DIM="$DENSE_DIM"
```

#### Test Execution Flow
1. **Model Connectivity & Dimension Probing**: Queries `/v1/models` and `/v1/embeddings` to auto-resolve served model names and probe the dense embedding dimension (`dense_dim=1024` for Qwen3-0.6B).
2. **Collection Dimension Validation**: If `--skip-ingest` is passed, validates that the collection exists and its dense vector dimension matches `dense_dim` (failing fast if mismatched). If ingesting, automatically recreates the collection if dimensions changed.
3. **Corpus Generation & Ingest**: Builds synthetic IBM-shaped manual PDFs with specific message IDs (`IEA500I`, `LFAREA`) and ingests them into a local Qdrant collection using real dense + BM25 sparse vectors.
4. **HTTP `/v1/search` Verification**: Queries the FastAPI endpoint and validates parallel prefetch fusion and hit ranking.
5. **HTTP `/v1/answer` Verification**: Executes reasoning queries through the real gateway via FastAPI HTTP endpoints.
6. **Strict Grounding Gate**: Fails closed if the model response returns zero validated citations or indicates ungrounded hallucination.

#### Streaming Reasoning on the Local Stack (`sh scripts/tools/run-task.sh local:agent`)
```bash
# Start the agent with LLM_STREAM=true so reasoning SSE reaches the client:
sh scripts/tools/run-task.sh local:agent                    # uvicorn on http://localhost:8080

# Stream a grounded answer (SSE):
curl -N -X POST "http://localhost:8080/v1/answer?stream=true" \
  -H "Content-Type: application/json" \
  -d '{"query": "What parameter controls LFAREA in IEASYSxx?"}'
```
The SSE response yields `event: token` deltas as the reasoning model generates, then exactly one terminal `event: final` carrying the full verified answer, citations, optional script, retrieval metadata, `ttft_ms`, and token usage. A mid-stream failure emits `event: error` and ends **without** a `final` event — treat stream-end-without-final as a failed request. `LLM_STREAM` (server-side reasoning SSE) defaults to `false` in production config; `sh scripts/tools/run-task.sh local:agent` enables it for TTFT measurement (also consumed by the L3 harness).

This component runner honors `UI_ENABLED`: `UI_ENABLED=true sh scripts/tools/run-task.sh local:agent` serves the operator console at `http://localhost:8080/ui` (unset keeps `/ui` at the fail-closed 404). `sh scripts/tools/run-task.sh local:stack` sets it true by default; the multi-turn chat and console contracts live in `docs/agent.md` §1/§3.

---

### 3.9 Exporting Standalone Model Weights for Offline Bastions

This is a separate platform-team model handoff, outside the application bundle.
The platform team chooses and verifies model revisions, mirrors weights, and owns
serving and gateway acceptance. The product consumes the resulting URLs, model
IDs, dimensions and per-leg keys. For the exact pinned local model views used in
the rehearsal, follow [the local guide](local-crc-environment.md#3-pin-and-start-the-two-model-servers-sequentially).

The following export uses the measured local revisions as an example. Production
model selection remains with the platform team:

```bash
# 1. Download Gemma-4 Reasoning Model:
.venv/bin/python -c '
from pathlib import Path
from huggingface_hub import snapshot_download

target_dir = Path.home() / "models" / "gemma-4-E4B-it-qat-mobile-ct"
target_dir.mkdir(parents=True, exist_ok=True)
snapshot_download(
    repo_id="google/gemma-4-E4B-it-qat-mobile-ct",
    revision="3624117cf04528e099519f93839f0f0b7a18913d",
    local_dir=str(target_dir),
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
    revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
    local_dir=str(target_dir),
)
'
```

---

### 3.10 Managing Collections & Real-World Ingest

When working with runtime-supplied PDF corpora, start the real gateway, source
its private `GATEWAY_ENV_FILE`, and export `RERANK_ENABLED=false` for the two-model
configuration. The examples inherit the gateway URLs, served model IDs and
per-leg keys and `EMBED_MODEL_REVISION` from that handoff; keep model computation
behind the gateway. Follow [the environment handoff checks](live-stack.md#local-environment)
first. The generated local revision label is a simulation label, never production
weight attestation. Before modifying existing data, read the
[publication/reader limitations](ingest.md#publication-contract); serialize writers
and drain readers for in-place maintenance. These commands are operator actions,
not prerequisites for a documentation change.

#### 1. Initial Ingestion with Dense Embeddings
```bash
EMBED_MODE=vllm \
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

* **SHA-256 + rules-version detection**: On every run, `run_ingest` computes the SHA-256 digest of each discovered PDF and re-verifies the inventory `(sha256, rules_v, manifest_digest)` binding against Qdrant: a skip needs a valid per-revision completion (`completion.is_revision_committed` — marker for this target generation plus verified points), never a sampled point.
* **Verified skipping**: Inventory matches avoid parsing/embedding only after the target generation's completion and stored points verify; inventory status alone is insufficient.
* **Extraction-rules changes**: a stored point whose `rules_v` differs is deleted and re-ingested; a non-empty collection written by a different rules version fails closed unless `--reingest` is passed (details in `docs/ingest.md` §9).
* **Deterministic UUID5 Point IDs**: New chunks are assigned deterministic UUID5 keys and inserted directly into the existing Qdrant collection without deleting or modifying previously indexed vectors.
* **Corrupted / Partial File Safety**: If ingestion was interrupted midway or a PDF failed earlier with an error, re-running `run_ingest` will pick up right where it left off, only processing un-ingested files.

**How to add new manuals:**
Simply drop the new PDFs into your manuals directory (or specify a new `--src` directory) and re-run with the same `QDRANT_COLLECTION` and `--progress` path:

```bash
# Ingest only newly added or modified PDFs:
EMBED_MODE=vllm \
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
DENSE_DIM=1024 \
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
RERANK_BASE_URL=http://localhost:4000/v1 \
RERANK_MODEL=BAAI/bge-reranker-v2-m3 \
sh scripts/tools/run-task.sh local:agent
```

---

## 4. Standard Deployment Architecture (Air-Gap Production & Local Cluster Testing)

The hardened 5-stage deployment pipeline (`airgap:pack` -> `airgap:load` -> `airgap:deploy` -> `airgap:ingest` -> `airgap:smoke`) is the **canonical deployment standard across the entire project**. Both production air-gapped OpenShift and local testing environments adhere to this pipeline (using the same scripts and Helm charts, with adapted sizing and SCC for local test clusters).

### 4.1 Packaging on the Connected Host (Image Factory)

The sneakernet archive is packaged on the connected host. Connected `main` is the only image factory.

> [!TIP]
> **Automated CI Packaging:** Every commit merged to `main` on public GitHub automatically triggers the `airgap-package` workflow in `.github/workflows/e2e.yml` once container images have been built and pushed to GHCR. Any authenticated GitHub user with read access to the repository can download the pre-packaged, verified `sneakernet-bundle-<sha>` artifact (containing `qdrant-pdf-rag-<sha>.tar`, digest, `PACKING_RECORD.txt`, and `MANIFEST.txt`, retained for 90 days) directly from GitHub Actions runs. Note that pull request runs intentionally skip this packaging job; the first real sneakernet bundle for a given commit is generated upon its first merge to `main`.

To build the package manually on a connected Linux workstation:

```bash
git clone https://github.com/yonatan895/qdrant-pdf-rag.git
cd qdrant-pdf-rag
git checkout "$IMAGE_SHA"  # Set to the full published-main SHA matching built GHCR images
sh scripts/tools/install-task.sh  # Explicit connected runner provisioning

# Custodied release key supplied through the approved credential process.
# CI reads PEM bytes from SNEAKERNET_SIGNING_KEY; the local pack command
# takes the PATH to a protected PEM file.
SNEAKERNET_SIGNING_KEY=/secure/release-signing.pem \
SNEAKERNET_KEY_TRUSTED=true sh scripts/tools/run-task.sh airgap:pack
```

A maintainer creates the signing key once in a protected directory, for example
with `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out signing.pem`,
and exports its public half with `openssl pkey -in signing.pem -pubout -out signing.pub`.
Store the private PEM as the GitHub `SNEAKERNET_SIGNING_KEY` secret through the
approved credential process; distribute the exact public PEM independently to
operators. Back up the private key securely. A throwaway key tests signing
mechanics only and does not authorize release transfer. Without a configured
secret CI may label a package `signed: ephemeral`; reject it for promotion.

`REDHAT_REGISTRY_USER` / `REDHAT_REGISTRY_PASSWORD` are the approved Red Hat
registry service-account credentials. They are distinct from CRC's developer or
kubeadmin credentials printed by `crc console --credentials`, and from the CRC
pull-secret JSON. Do not paste any of these credentials into issue/PR text.

This generates `dist/qdrant-pdf-rag-<sha>.tar` and its digest `dist/qdrant-pdf-rag-<sha>.tar.sha256`, containing:
1. Complete Git repository bundle (`repo.bundle`).
2. App container images (`qdrant-pdf-rag-agent`, `qdrant-pdf-rag-ingest`), with per-image digests bound in `MANIFEST.txt` and re-verified on load.
3. Vendored third-party Qdrant unprivileged image and Jaeger v2 image (tag + digest pinned in `images.txt`).
4. Vendored Helm chart (`charts/qdrant-1.19.0.tgz`).
5. Self-contained extraction bootstrap script (`bootstrap.sh`).
6. Manifest (`MANIFEST.txt`), Packing Record (`PACKING_RECORD.txt`), digest enumeration (`sbom.json`), offline signature (`SHA256SUMS.sig` + `sneakernet-signing.pub`), and member `SHA256SUMS`.
7. The console oauth-proxy sidecar image (`oauth-proxy-image.tar`) — pinned in `images.txt`; connected packaging needs Red Hat registry authentication (see §4.4.2).
8. The pinned host runner (`task_linux_amd64.tar.gz`), `task-pin.txt` and `task-LICENSE`, all covered by member checksums. Packaging reuses `.tools/cache` from the installer or `AIRGAP_TASK_ARCHIVE=/absolute/path/task_linux_amd64.tar.gz`; otherwise the connected pack fetches the exact pinned archive.

### 4.2 Transfer & Automated Bootstrap

Before production transfer, complete [the Windows CRC release gate](crc-release-verification.md)
and [verification record](crc-release-record.md) against the published-main
bundle. Missing or failed checks block transfer. Send the identical tested
tarball; do not rebuild or repack after verification. The steps below are also
used for the fresh local CRC bootstrap rehearsal.

Transfer the tarball and checksum file via approved sneakernet media to the air-gapped bastion host:

```bash
# 1. Verify tarball integrity BEFORE unpacking
export BUNDLE=/absolute/path/to/qdrant-pdf-rag-FULL_SHA.tar
export SNEAKERNET_TRUSTED_PUB=/secure/trusted-release-signing.pub
cd "$(dirname "$BUNDLE")"
sha256sum -c "$(basename "$BUNDLE").sha256"

# 2. Extract tarball and run the automated bootstrap script:
mkdir /absolute/path/to/new-candidate
cd /absolute/path/to/new-candidate
tar -xf "$BUNDLE"
sh bootstrap.sh

# 3. Enter the initialized workspace:
cd qdrant-pdf-rag
```

The `bootstrap.sh` script automatically:
- Verifies the bundle signature (`SNEAKERNET_TRUSTED_PUB` when provided) and then all member checksums in `SHA256SUMS`.
- Clones the Git repository from `repo.bundle` and checks its full HEAD against the manifest. An existing workspace must already match that SHA; otherwise use a fresh `AIRGAP_WORKSPACE` or deliberately check out the approved bundle SHA before retrying.
- Populates `./dist` with image archives and manifests, including `oauth-proxy-image.tar` when the bundle contains it.
- Verifies the bundled Task pin against the checkout, verifies installer integrity, then installs `.tools/bin/task` from the signed bundle archive without network access or preinstalled Make/Task/Go/application Python.
- Initializes `airgap.env` from `airgap.env.example` only when absent; reruns preserve operator configuration and retained artifacts.

After bootstrap use `sh scripts/tools/run-task.sh --list` for discovery.
For rollback, use the previous approved bundle and its own bootstrap/command
contract in a separate workspace; an older bundle may still use Make. Never
combine its assets with a newer checkout.

### 4.3 Configure Environment & Pre-Flight Validation

Edit `airgap.env` to configure your cluster environment:

> [!NOTE]
> Environment precedence (explicit env beats file, `AIRGAP_ENV` path beats
> local `./airgap.env`) lives in `deploy.md` §2 — one rule, one owner. Two
> re-run notes: `VAR=x sh scripts/tools/run-task.sh airgap:validate` overrides a stale key, but a bare
> `sh scripts/tools/run-task.sh airgap:validate` reuses whatever the file still holds — re-run
> `sh scripts/tools/run-task.sh airgap:validate` after editing `airgap.env` before touching the cluster.

```ini
# Internal image registry accessible to cluster nodes
INTERNAL_REGISTRY=registry.internal.enterprise:5000/mainframe-rag

# Target namespace
NAMESPACE=mainframe-rag

# Persistent storage class (Must be RWO Block, e.g. ocs-storagecluster-ceph-rbd)
STORAGE_CLASS=gp3-csi

# Caller-provisioned, populated corpus claim mounted read-only by ingest.
CORPUS_PVC=manuals-corpus
RERANK_ENABLED=false
INSECURE_REGISTRY=false

# Collection distribution policy (issue #360): the checked-in production
# preset overlays/openshift/collection-policy.env supplies 6 shards / RF 3 /
# W 2 when these are unset. A one-node rehearsal must select 1/1/1
# explicitly (never infer a downgrade). Values here win over the preset.
# QDRANT_SHARD_NUMBER=6
# QDRANT_REPLICATION_FACTOR=3
# QDRANT_WRITE_CONSISTENCY_FACTOR=2

# Authenticated TLS gateway endpoints supplied by the platform team.
# VLLM_BASE_URL takes the bare server origin (a trailing /v1 is tolerated:
# the deploy scripts strip it before deriving EMBED_BASE_URL).
VLLM_BASE_URL=https://gateway.example.test
EMBED_BASE_URL=https://gateway.example.test/v1
LLM_BASE_URL=https://gateway.example.test/v1
EMBED_MODEL=REPLACE_WITH_PLATFORM_EMBED_MODEL
DENSE_DIM=REPLACE_WITH_PLATFORM_DIMENSION
EMBED_MODEL_REVISION=REPLACE_WITH_PLATFORM_EMBED_REVISION
LLM_MODEL_REASONING=REPLACE_WITH_PLATFORM_REASONING_MODEL

# Gateway virtual keys: name of ONE operator-created Secret holding the
# platform team's LiteLLM keys (unset = keyless). Never put key values here.
GATEWAY_API_KEY_SECRET=gateway-api-keys
GATEWAY_CA_CONFIGMAP=gateway-ca

# Optional pull secret name (if registry requires credentials)
PULL_SECRET=internal-registry-pull-secret

# Optional: external OAuth-proxied console Route (ADR-0004). Requires the
# oauth-proxy digest recorded in images.txt, a repack, and Secret
# rag-agent-oauth-cookie (see §4.4.2). Without it the console is still served
# in-cluster at /ui on the ClusterIP 8080 port.
#AGENT_ROUTE=true
```

#### Platform model endpoints (reasoning / embed / optional rerank)

The platform team supplies embedding and reasoning endpoints and, when enabled,
a reranker. Set the actual model IDs and dimensions in `airgap.env`; the example
placeholders above are not deployment values. Keep reranking disabled unless
the site explicitly enables it:

| Role | URL key | Model key | Notes |
|---|---|---|---|
| Reasoning (answer, chat and console) | `LLM_BASE_URL` | `LLM_MODEL_REASONING` | Empty model = answers stay disabled. Raise `LLM_MAX_MODEL_LEN` past the 4096 default to the served context (tokenizer uses the server `/tokenize`, estimator fallback otherwise). Auth: `llm-api-key` from the `GATEWAY_API_KEY_SECRET` Secret (unset = keyless). |
| Embed (`/v1/search`, ingest) | `EMBED_BASE_URL` (defaults to `VLLM_BASE_URL`) | `EMBED_MODEL` + `DENSE_DIM` + `EMBED_MODEL_REVISION` | `DENSE_DIM` is required and fail-closed: it must equal the served native dim (4096 for Qwen3-Embedding-8B). Collections are created at that width; a mismatch against an existing collection refuses with `DimMismatchError`. `EMBED_MODEL_REVISION` is the operator-declared immutable model/config revision (a gateway alias is mutable and a dimension is not an identity): blank/whitespace-only values fail pre-flight and refuse agent/ingest startup, and a revision change requires a deliberate `--reingest` migration. Auth: `embed-api-key` from the same Secret; the ingest Job reads it too. |
| Rerank (optional, default off) | `RERANK_BASE_URL` (defaults to `EMBED_BASE_URL`) | `RERANK_MODEL` | Served via a vLLM pooling server (`--runner pooling`, `/v1/score`; TEI `/v1/rerank` fallback). Use the platform gateway rerank leg and its model ID; do not configure a direct-backend consumer shortcut. Lifespan logs a loud warning (never a refusal) when the endpoint is unreachable at startup. Auth: `rerank-api-key` from the same Secret. Leg order: `RERANK_ENDPOINT_ORDER=rerank_first` for gateways (run `probe_gateway.py` below to decide). |

Set the namespace to the same value chosen in `airgap.env` and create it if the
site has not already provisioned it. Do not source a local-development handoff in
this operator shell. Environment exports override `airgap.env`.

```sh
export NAMESPACE=mainframe-rag
oc create namespace "$NAMESPACE" --dry-run=client -o yaml | oc apply -f -
```

Create the key Secret **before** `sh scripts/tools/run-task.sh airgap:deploy` (one Secret, four data keys; the contextual-gist key rides the ingest Job):

```bash
# Files are mode 600, supplied through the platform credential process.
# Include all referenced keys, even when an optional leg is disabled.
oc -n "$NAMESPACE" create secret generic gateway-api-keys \
  --from-file=llm-api-key=/secure/gateway/llm-api-key \
  --from-file=embed-api-key=/secure/gateway/embed-api-key \
  --from-file=rerank-api-key=/secure/gateway/rerank-api-key \
  --from-file=context-llm-api-key=/secure/gateway/context-llm-api-key
oc -n "$NAMESPACE" create configmap gateway-ca \
  --from-file=ca-bundle.crt=/secure/gateway/ca-bundle.crt
```

Supply a complete CA bundle containing every required root: `SSL_CERT_FILE`
replaces the client's default bundle. Mounting this ConfigMap makes agent and
ingest trust the gateway; node registry trust and browser ingress trust remain
separate. Test correct CA, wrong CA and hostname mismatch from actual pods.

`sh scripts/tools/run-task.sh airgap:validate` verifies the Secret exists (when the namespace does) and refuses plaintext `*_API_KEY` values in `airgap.env`. Rotate by updating the Secret, then rollout-restart the agent (or re-run the ingest Job).

#### Pre-Flight Validation (`sh scripts/tools/run-task.sh airgap:validate`)

Before modifying any cluster state, run the pre-flight validation check to verify tools, required variables, storage class compliance (refusing NFS), and required keys — it prints OpenShift SCC guidance but does not verify SCC permissions:

```bash
sh scripts/tools/run-task.sh airgap:validate
```

To preview rendered templates and substitution rules without cluster credentials:
```bash
sh scripts/tools/run-task.sh airgap:dryrun
```

Before loading, the registry administrator must configure authenticated TLS,
install its CA for the loader and cluster nodes, and prove an uncached node pull.
Authenticate the loader into a protected `REGISTRY_AUTH_FILE` using the approved
registry credentials and its installed CA. For example, set the real registry
authority (without a repository suffix) and let Skopeo prompt:

```sh
umask 077
export REGISTRY_AUTH_FILE=/secure/registry-auth.json
skopeo login --authfile "$REGISTRY_AUTH_FILE" registry.example.test:5000
```

Then create the namespace pull Secret without putting passwords in argv:

```sh
oc -n "$NAMESPACE" create secret generic internal-registry-pull-secret \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$REGISTRY_AUTH_FILE"
```

Set `INSECURE_REGISTRY=false`. Provision the operator-supplied corpus PVC and
OAuth cookie Secret before deployment. Use production storage and sizing;
`QDRANT_EXTRA_VALUES`, `INGEST_EXTRA_PATCH` and the tiny CRC claims are local
rehearsal overrides. [The local guide](local-crc-environment.md) owns those values.

### 4.4 Load Images & Deploy Stack

Operators can either run the complete automated pipeline in one command or execute each stage individually:

#### Option A: Unified Pipeline Orchestrator (Recommended)

```bash
# Execute validate -> load -> deploy -> (optional ingest) -> smoke in sequence:
sh scripts/tools/run-task.sh airgap:pipeline

# Or with ingest:
CORPUS_PVC=my-corpus-pvc sh scripts/tools/run-task.sh airgap:pipeline
```

The pipeline probes configured legs before ingest, but its built-in probe does
not request the stricter streaming check. For first production acceptance use
the modular sequence below so `--require-reasoning --stream` passes before ingest.
Later repeat the full pipeline with identical configuration. Require a nonempty
search, explicit cited answer, contextual follow-up and every streaming interface
(including browser busy-state recovery), verified OAuth/Route certificates and
persisted traces before declaring the installation operational. Preserve the
site's SCC, identity, storage, registry and network acceptance evidence; a local
CRC pass does not replace it.

#### Option B: Step-by-Step Modular Execution

```bash
# 1. Verify internal member checksums and push images to internal registry
sh scripts/tools/run-task.sh airgap:load

# 2. Deploy Qdrant 3-replica cluster and Agent deployment
sh scripts/tools/run-task.sh airgap:deploy

# 3. Require both real platform legs and a successful streaming finish
# from the application's network, trust store and Secret-backed identity.
oc -n "$NAMESPACE" exec deploy/rag-agent -c agent -- \
  python3 /app/scripts/probe_gateway.py --require-reasoning --stream

# 4. Only after that probe passes:
sh scripts/tools/run-task.sh airgap:ingest
sh scripts/tools/run-task.sh airgap:smoke
```

#### OpenShift Security Context Constraints (SCC) Note

The production and CI OpenShift values explicitly remove the chart's fixed UID,
GID and fsGroup defaults. `restricted-v2` assigns the namespace identity and
volume group. Jaeger also leaves IDs to admission. Application images make
only their cache and work directories group-0 writable.

Require actual `restricted-v2` admission and successful data, snapshot,
ingest-work and trace volume writes under
[the CRC procedure](crc-release-verification.md#5-release-prerequisites-and-local-sizing).
An admission or storage failure blocks promotion and requires a fix in the
owning production configuration followed by a new bundle and verification.
Do not grant `anyuid`, substitute another project's IDs, or hide security
changes in `QDRANT_EXTRA_VALUES`.

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

### 4.4.1 Tracing (active by default, issue #83)

Tracing is **on by default**: leaving `OTEL_EXPORTER_OTLP_ENDPOINT` unset
resolves to the in-cluster `http://jaeger:4318`, and `sh scripts/tools/run-task.sh airgap:deploy`
renders and installs the Jaeger templates in `charts/mainframe-rag` (Jaeger v2 all-in-one, badger
storage on a 10Gi RWO block PVC, 14-day span TTL, ClusterIP only) and wires
the endpoint into the agent and the ingest Job. To disable tracing — and skip
the Jaeger deployment entirely — set `OTEL_EXPORTER_OTLP_ENDPOINT=off` (also
`none`, `false`, or `0`) in `airgap.env`. A custom `http(s)` OTLP/HTTP
collector origin is accepted in place of the in-cluster Jaeger; anything else
fails closed before a manifest is rendered. `sh scripts/tools/run-task.sh airgap:validate` prints the
resolved mode.

This traces **this repo's components only** (agent, retrieval, ingest); the
platform team's model tier keeps its own monitoring. Outbound model calls
carry W3C `traceparent` so their gateway can correlate its spans with ours
when it is tracing-enabled — nothing from this repo configures or deploys
their monitoring.

The sneakernet bundle always carries the Jaeger image (`images.txt` is a
pack-wide contract — every pin with a recorded digest is mirrored on every
pack; the oauth-proxy pin is skipped while `sha256:PENDING`), so the
default-on path works in a disconnected install. The endpoint may be given
with or without the `/v1/traces` path — the agent accepts both.
`sh scripts/tools/run-task.sh airgap:smoke` proves a `v1.search` span landed before reporting
acceptance (empty-collection runs report tracing as skipped); it polls the
Jaeger query API at `JAEGER_QUERY_URL` (default `http://jaeger:16686`) —
change that only when a custom collector exposes a Jaeger-compatible query
API.

Every exported span carries deploy identity as resource attributes:
`service.version` is the packed `IMAGE_SHA` for the agent Deployment, and
`deployment.environment` comes from the optional
`OTEL_DEPLOYMENT_ENVIRONMENT` in `airgap.env` (e.g. `prod`; omitted when
unset) — so traces from lab and prod sharing one backend stay
unambiguous. The ingest Job does not carry `IMAGE_SHA` yet, so its spans have
no `service.version` (issue #315). The agent also honors the standard
`OTEL_RESOURCE_ATTRIBUTES` mapping underneath these explicit keys, and joins
an upstream W3C `traceparent` when the caller sends one.

View traces (port-forward only — Jaeger has no public Route, like Qdrant):

```bash
oc -n mainframe-rag port-forward svc/jaeger 16686:16686
# open http://localhost:16686, service "mainframe-rag-agent" (rename via OTEL_SERVICE_NAME
# in airgap.env — deploy.sh renders it, and strips the entry when unset so the
# agent default stands)
```

Backend posture (single-replica debug-grade, sample-all retention math,
exemplar deferral) is recorded in `docs/adr/0002-otel-backend-posture.md`.

### 4.4.2 Operator Console Route (optional, ADR-0004)

The agent serves the operator console at `/ui`, and the production chart sets
`UI_ENABLED=true` — so the console is reachable in-cluster at
`http://rag-agent:8080/ui` even without a Route. Only the **external** Route is
OAuth-protected; enable it with `AGENT_ROUTE=true`:

1. Authenticate the connected packer to Red Hat's registry. The image digest
   is already recorded in `images.txt`; inspect that exact digest when checking
   access. Future pin updates require a dedicated pin PR and a new main bundle:
   ```bash
   skopeo login registry.redhat.io
   OAUTH_IMAGE="$(awk '$1 ~ /^registry.redhat.io\/openshift4\/ose-oauth-proxy:/ {print $1 "@" $2}' images.txt)"
   skopeo inspect --no-tags "docker://$OAUTH_IMAGE" | jq -r .Digest
   # Package only a green published-main SHA, following section 4.1.
   ```
   While the pin is `sha256:PENDING`, `pack.sh` skips the sidecar and
   `AGENT_ROUTE=true` deploy fails closed.
   The GitHub pack job requires repository secrets `REDHAT_REGISTRY_USER` and
   `REDHAT_REGISTRY_PASSWORD` for a registry service account authorized to pull
   this image. Provision those before merging a recorded-pin change; the job
   fails closed when credentials are absent or rejected. Keep personal CRC
   pull secrets on the workstation. The Docker archive omits unsupported
   upstream signature attachments; image digests and the offline bundle
   signature remain enforced.
2. Create the cookie-encryption Secret (operator-owned; deploy fails closed
   without it):
   ```bash
   # First provisioning only; reuse the same protected file on restart.
   umask 077
   openssl rand -base64 24 | tr -d '\n' > /secure/oauth-cookie-secret
   oc -n "$NAMESPACE" create secret generic rag-agent-oauth-cookie \
     --from-file=cookie-secret=/secure/oauth-cookie-secret
   ```
3. Set `AGENT_ROUTE=true` in `airgap.env` and deploy. `deploy.sh` renders
   the chart OAuth resources and reconciles the `reencrypt` Route, including
   an existing Route's CA and timeout, using
   the namespace `openshift-service-ca.crt` bundle as the
   `destinationCACertificate`.
4. Reach it: `oc -n mainframe-rag get route rag-agent` → unauthenticated
   browser requests may show a provider chooser (403); `/oauth/start` redirects
   to OpenShift OAuth; `/healthz` bypasses OAuth for
   probes. In-cluster tools keep using the ClusterIP 8080 port (unauthenticated
   by design, no Route).

`sh scripts/tools/run-task.sh airgap:validate` checks none of these prerequisites — a missing digest or
Secret fails at deploy time.

> **Connected-host follow-up before an air-gap cut:** a `requirements.lock.txt`
> bump (e.g. the `jinja2` + `python-multipart` pins the console needs) requires
> `sh scripts/tools/run-task.sh artifacts:wheelhouse artifacts:bm25` plus a connected image rebuild/push and a
> fresh pack — air-gap images install only from the baked wheelhouse.

### 4.5 Corpus Ingestion

#### Gateway readiness probe

After `sh scripts/tools/run-task.sh airgap:deploy` and before ingesting, prove the platform
gateway answers every configured leg from inside the cluster (same network
as the agent — the bastion itself may not reach it). `sh scripts/tools/run-task.sh airgap:pipeline`
runs this probe automatically after deployment; the following command is for
modular deployment and diagnosis:

```bash
# Embed + reasoning + rerank reachability, dim match, auth diagnosis:
kubectl -n mainframe-rag exec deploy/rag-agent -- \
  python3 /app/scripts/probe_gateway.py --require-reasoning --stream
# Requires a successful finish and [DONE]; stream errors fail the probe.
```

The probe exits nonzero when a required leg fails (a 401 names the missing
`*_API_KEY`; a dim mismatch fails — ingest would refuse it too) and prints
the recommended `RERANK_ENDPOINT_ORDER` (`score_first` whenever the score leg
answers — a gateway exposing both legs still gets `score_first`;
`rerank_first` only when the score leg is unavailable). Set the
recommendation in `airgap.env` and re-run `sh scripts/tools/run-task.sh airgap:deploy` before
ingesting. A missing `/tokenize` is informational only — the agent pins
its in-process estimator (expected behind LiteLLM).

Once the manual PDF corpus PVC is provisioned and populated:

```bash
# Launch one-shot ingest Job against corpus PVC
sh scripts/tools/run-task.sh airgap:ingest CORPUS_PVC=mainframe-manuals-pvc
```

Monitor ingest progress:
```bash
oc -n mainframe-rag logs -f job/ingest
```

### 4.6 Verification & Smoke Testing

```bash
# Run in-cluster smoke search
sh scripts/tools/run-task.sh airgap:smoke

# Or query specific message IDs:
QUERY="IEA500I operator message" sh scripts/tools/run-task.sh airgap:smoke
```

**Collection placement (issue #360):** before accepting a generation, run
the read-only verifier from a pod or the bastion against every expected Qdrant
peer (see [the collection policy contract](deploy.md#collection-policy)).
`QDRANT_URL` is the entry/Service endpoint used for inventory and the alias
binding; the `--peer-url` endpoints are the authoritative direct peers:

```bash
QDRANT_URL=http://qdrant:6333 QDRANT_SHARD_NUMBER=6 \
QDRANT_REPLICATION_FACTOR=3 QDRANT_WRITE_CONSISTENCY_FACTOR=2 \
python3 scripts/verify_placement.py --production \
    --peer-url http://qdrant-0.qdrant-headless:6333 \
    --peer-url http://qdrant-1.qdrant-headless:6333 \
    --peer-url http://qdrant-2.qdrant-headless:6333
```

In the ingest image the script is at `/app/scripts/verify_placement.py` and
the entrypoint runs ingestion; run it as an explicit read-only command
override (never as an ingest Job): point the container command at
`python3 /app/scripts/verify_placement.py --production --peer-url ...` with
the same `QDRANT_*` environment.

`VERDICT: healthy` with exit 0 is the production qualification outcome and
is placement evidence only (no read/write probe). `degraded` (exit 0 only
with `--allow-degraded`, never with `--production`) means one known member
is lost and is operational continuation, not qualification. A one-node
rehearsal labels itself `VERDICT: non-ha` with `--expect-single-node` and
never counts as distributed acceptance.

### 4.7 Local Cluster Testing Standard (Kind + Local Registry)

To test the deployment scripts and Kubernetes manifests locally without access to an OpenShift cluster, the project standardizes on **Kind** (Kubernetes-in-Docker) paired with a local registry container on port 5000.

Kind remains the fast development rehearsal. Production release transfer
requires [Windows CRC verification](crc-release-verification.md), including
real OpenShift SCC, OAuth, Route, TLS, and isolation checks. Preserve Kind
volumes and tested backups; stop its nodes while CRC runs.

> [!NOTE]
> Local cluster testing exercises the identical packaging scripts, container archives, Helm charts as production, but with adapted sizing and security contexts (1-replica Kind + mock/local vLLM rather than 3-replica OpenShift `restricted-v2`).

The executable recipe for the current test environment is the
[`kind-live-rehearsal` matrix](../.github/workflows/e2e.yml), with lane details in
[deploy.md](deploy.md#release-rehearsal-lanes-and-fixes). To reproduce the complete
CI environment, dispatch the existing workflow on published `main` and inspect
all three lanes plus bundle acceptance:

```sh
gh workflow run e2e.yml --ref main
gh run list --workflow e2e.yml --branch main --limit 5
# Set RUN_ID to the dispatched run, then wait and retain its diagnostics.
gh run watch "$RUN_ID" --exit-status
gh run download "$RUN_ID" --dir /path/outside/git/rehearsal-evidence
```

This runs the connected main factory; it does not promote a bundle. All lanes
use that run's published bundle instead of repacking it independently. For local
Kind debugging, follow those same steps from a fresh bootstrap, using a **new
uniquely named** disposable cluster and registry. If port 5000 or a cluster name
belongs to the preserved development environment, choose a separate port/name
and update the loader, node registry trust and image authority consistently.
Do not delete, overwrite or reuse the preserved cluster's volumes.

The current recipe differs from older HTTP/direct-mock examples:

1. A digest-pinned registry uses TLS, bcrypt authentication and explicit node
   trust. The loader's protected auth file supplies the Kubernetes pull Secret.
2. Both `VLLM_BASE_URL` and `EMBED_BASE_URL`/`LLM_BASE_URL` point through the
   real TLS test gateway. `scripts/ci/deploy_test_gateway.sh` owns the test
   LiteLLM/PostgreSQL deployment, CA and virtual-key Secrets.
3. Only model computation uses `scripts/mock_vllm.py`, mounted from the fresh
   bundle's checkout. Set `MOCK_DIM=1024`, `EMBED_MODEL=mock-embed`,
   `LLM_MODEL_REASONING=mock-reasoning`, `DENSE_DIM=1024`,
   `EMBED_MODEL_REVISION=mock-embed@ci` and
   `RERANK_ENABLED=false`; consumer URLs are `https://test-gateway:4000/v1`.
   Set `GATEWAY_API_KEY_SECRET=test-gateway-keys` and
   `GATEWAY_CA_CONFIGMAP=test-gateway-ca`.
4. Use the production deployment pipeline with small test resource overrides,
   `AGENT_ROUTE=false`, and the loaded candidate ingest image to generate the
   corpus. Kind needs its own volume-group override because it has no SCC.
   Never copy that fixed group into the OpenShift production values.
5. `application_contracts.py` checks real application responses and stream
   endings. `gateway_faults.sh` verifies injected failures through LiteLLM and
   recovery. `check_lifecycle.sh` verifies snapshot restore, replacement/PVCs
   and persistence of an existing trace; repeat the pipeline afterward.

The exact setup YAML, image/tool pins, resource patches, authenticated registry
configuration, corpus generator and cleanup are kept together in the workflow.
The CRC variant uses [the local runbook's generator](local-crc-environment.md#72-configure-and-deploy-the-same-candidate)
with `restricted-v2` and project-assigned IDs. No mock or test gateway belongs
in production manifests or application images.

Access Kind privately with `kubectl -n "$NAMESPACE" port-forward svc/rag-agent
8080:8080` and open `http://localhost:8080/ui`. Kind supplies no OpenShift OAuth,
Route, Service CA or SCC evidence. Only remove the newly created disposable
cluster after retaining diagnostics and synthetic recovery evidence.

If the CRC fit fails, the separate real-model Kind lane uses the same original
bundle, the two current GPU models through the authenticated TLS gateway, and a
separate real-vector collection. It must pass alongside the CRC mock lane and CI;
[the fallback gate](crc-release-verification.md#11-bounded-simultaneous-fit-attempt-and-complementary-verification)
keeps the missing combined OpenShift/live-model coverage explicit.

---

## 5. Day-2 Operations & Maintenance

### 5.1 Health Check API

The agent exposes `/healthz` for the OpenShift readiness probe and
`/livez` for liveness (contract: `agent.md` §1 — `ok`/`degraded`/
`503 qdrant_unready`, upstream bodies stay server-side). `/healthz` is a
real readiness failure (HTTP 503) whenever the served generation is not
validated compatible — including `reembed_required`, `legacy`, a `pending`
migration, or unreadable metadata; `empty` stays ready so the
deploy -> ingest bootstrap can complete. `/livez` is process-only.

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

#### `POST /v1/chat` and `POST /v1/chat/completions`
Multi-turn chat over the same shared core as `/v1/answer`; the client owns the
history. Request/response fields and the OpenAI-compatible SSE contract live in
`agent.md` §1/§3.

```bash
curl -X POST http://rag-agent:8080/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "How do I resolve IEA500I command rejected?"}]}'
```

#### Operator console (`GET /ui`)
Server-rendered console (ADR-0004: Jinja2 + HTMX + SSE, browser-only session
state). `UI_ENABLED` unset/false returns the stable 404 envelope; the
production chart sets it true, and external access is the OAuth-proxied
Route from §4.4.2.

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
| **NFS Storage Refusal** | `sh scripts/tools/run-task.sh airgap:deploy` fails validation | Set `STORAGE_CLASS` to an RWO block driver (Ceph RBD / SAN / EBS). |
| **Hash Mode in Prod** | Scripts fail closed with `EMBED_MODE=hash forbidden` | Remove `EMBED_MODE` from production environment; provide valid vLLM endpoint. |
| **Registry Certificate Error** | `skopeo copy` fails with `x509: certificate signed by unknown authority` | Install the registry CA in the loader's trust store and configure node trust separately. Verify the certificate hostname; do not disable TLS verification for CRC release or production acceptance. The Kind HTTP registry is a development-only exception. |
| **OpenShift SCC Rejection** | Pod `qdrant-0` fails with `unable to validate against any security context constraint` | Block promotion, capture admission evidence, and fix project-range compatibility in the owning production configuration. Verify PVC writes under `restricted-v2`; never grant `anyuid`. See [CRC verification](crc-release-verification.md). |
| **PVC Multi-Attach Error** | `job/ingest` fails with `Multi-Attach error for volume` on corpus PVC | Ensure any previous writer pod has released the PVC, or use a ReadOnlyMany volume. |
| **Qdrant P2P CrashLoop** | Pod `qdrant-0` fails with `No such file or directory` looking for `cert.pem` | Ensure `config.cluster.p2p.enable_tls: false` in `values.yaml` (gossip is plaintext on CNI without `./tls/cert.pem`). |
| **K8s Manifest Integer/Boolean Error** | `Invalid value: "string", expected integer/boolean` | Ensure numeric/boolean env vars (`DENSE_DIM`, `INGEST_WORKERS`, `RERANK_ENABLED`) are explicitly quoted in rendered manifests. |
| **Degraded `/healthz` Smoke Failure** | `sh scripts/tools/run-task.sh airgap:smoke` exits 1 with `FAIL: /healthz probe did not report ok` | Pre-flight probe failed closed (non-`ok` body or non-200). Check Qdrant and vLLM connectivity, then the `representation` field: `reembed_required`/`legacy`/`pending` needs `--reingest`; `unknown` means the metadata store is unreachable. Requests refuse with `503 representation_unavailable` while this holds. |
| **Qdrant 401 After Reinstall** | `/v1/search` fails with `401 Invalid API key or JWT` after Qdrant was reinstalled or re-`helm upgrade`d | The chart regenerates the `<release>-apikey` secret on reinstall while running agent pods keep the old key in env. Roll the agent: `kubectl -n <ns> rollout restart deploy/rag-agent` and wait for rollout before smoking again. |
| **Stale dist/ MANIFEST** | `airgap:validate` / `airgap:deploy` / `airgap:ingest` / `airgap:dryrun` fail with `IMAGE_SHA=<sha> does not match packed MANIFEST sha` | `dist/` is gitignored build output that persists across checkouts — the MANIFEST inside is from an older pack (only `pack` regenerates it; the other steps just read it). Repack at the current HEAD, or clear the stale `dist/` before re-running. |
| **Stale airgap.env IMAGE_SHA** | `airgap:pack` / `airgap:load` fail with `IMAGE_SHA=<sha> is not the checked-out commit` right after checking out a new SHA | `airgap.env` is gitignored local state from a previous rehearsal — its `IMAGE_SHA` no longer matches HEAD. Explicit env beats the file (`IMAGE_SHA=$(git rev-parse HEAD) sh scripts/tools/run-task.sh airgap:pack`), or update the file. |
| **Stale dist/ tarballs fill disk** | `pack`/`load` fail with no-space errors after several rehearsals | Every pack leaves a ~1.5 GB `qdrant-pdf-rag-<sha>.tar` in gitignored `dist/`; only the MANIFEST-pinned one is live. Delete superseded tarballs (keep the `.tar.sha256` of the live one) — pack never prunes. |
| **Kind ErrImagePull on localhost:5000** | mock/corpus-gen pods fail with `dial tcp [::1]:5000: connect: connection refused` | The Kind `containerdConfigPatches` in §4.7 must mirror **both** `localhost:5000` and `airgap-registry:5000` to the registry container — one key per naming family used by the manifests. Recreate the cluster with the documented config (containerd mirrors are set at creation). |
| **Console Route deploy fail-close** | `sh scripts/tools/run-task.sh airgap:deploy` dies on the oauth-proxy `sha256:PENDING` pin or a missing `rag-agent-oauth-cookie` Secret | Record the digest in `images.txt` + repack and create the cookie Secret (§4.4.2); or deploy with `AGENT_ROUTE=false` (ClusterIP-only, `/ui` still served in-cluster). |
| **`/ui` returns 404** | Console request returns the stable `404 not_found` envelope | `UI_ENABLED` is unset/false for the agent process. The production chart sets it true; for local runs use `UI_ENABLED=true sh scripts/tools/run-task.sh local:agent` or `sh scripts/tools/run-task.sh local:stack`. |
