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
- **Data Storage:** Qdrant persistent volumes **must** use RWO block storage (NFS-looking `STORAGE_CLASS` values are refused; the snapshot storage class falls back to it but is refused separately when NFS-looking).
- **Inference Separation:** The platform team owns the model servers and gateway. Agent and ingest consume its authenticated HTTP endpoints; this product does not deploy that tier.

---

## 2. Prerequisites

### Local Development
- **Operating System:** Linux / WSL2 (linux-amd64 pinned Task artifact)
- **Python:** CPython **3.14 GIL** (`python3.14 --version`). Do not use free-threading (`3.14t`).
- **Container Runtime:** Docker or Podman (for running integration simulation tests).
- **Tools:** `git`, `curl`, POSIX shell, `tar`, `sha256sum`; the pinned Task runner is installed explicitly below.

### Disconnected / Air-Gapped Bastion
- **Bastion pod:** from the fresh install on (ADR-0009 §8) the bastion is the
  `mfr-bastion` pod on the dev workspace image, which carries the tools below
  at their pins. Create it from the signed dev kit with
  [`docs/runbooks/devkit-windows.md`](runbooks/devkit-windows.md); see
  [air-gapped GitLab fork](#gap-fork).
- **Host runner:** signed bundle bootstrap installs verified Task into `.tools/bin/task`; no Make, preinstalled Task, Go, application Python or internet is needed for bootstrap. Keep `git`, POSIX shell, `tar`, `sha256sum` and `openssl` available for its verification steps.
- **OpenShift Client:** `oc` (v4.12+) or `kubectl`.
- **Helm:** checksum-pinned **4.3.0**, as used by [the deployment workflow](../.github/workflows/e2e.yml); the launcher requires Helm 4. Transfer the verified client with the operator tools. Both `charts/mainframe-rag` and `charts/qdrant-1.19.2.tgz` are local; do **not** run `helm repo add` in the air gap.
- **Rendering:** host `python3` with its standard library for release-value serialization and ownership checks. Bootstrap itself does not require Python; deployment does. No application packages or runtime dependency downloads are needed on the bastion.
- **Image Tooling:** `skopeo` (for loading the bundle's `oci-archive` image members into the internal registry). skopeo ignores `TMPDIR` for `oci-archive` and uses `/var/tmp` unless `--tmpdir` is given: add `--tmpdir /work/tmp` to `SKOPEO_ARGS` when `/var/tmp` is not writable. `load.sh` checks that directory (exists, writable, room for the largest member) before the first push. Let a restrictive `--policy` file admit the `oci-archive` transport ([image format](deploy.md#bundle-image-format)).
- **Cluster Permissions:** Access to create/manage workloads in the target namespace with `restricted-v2` SCC.

---

## 3. Local Development & Simulation Workflow

For the release rehearsal with both real models, use
[release-verification.md](release-verification.md). It owns the pinned model
views, gateway, TLS registry and Kind cluster setup and the local sizing.

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

`sh scripts/tools/run-task.sh qa:check` runs Ruff, mypy and the unit suite in
that order and stops at the first failure. Run one step alone with `qa:lint`,
`qa:typecheck` or `qa:unit`; pass focused paths after `--` (for example
`qa:unit -- tests/test_agent_context.py -q`). CI alone runs the full unit
suite; test design is owned by [testing.md](testing.md#evidence-design).

### 3.3 Integration Simulation Tier

`sh scripts/tools/run-task.sh qa:sim` spins up the pinned unprivileged Qdrant
container, parses runtime-generated synthetic PDFs, ingests them and exercises
the agent API against the deterministic mock LLM. `sh scripts/tools/run-task.sh qa:load`
runs the same composition under concurrency and asserts absolute contracts (no
errors, SSE integrity, citation parity, fixed envelopes, determinism, abort
survival, TTFT floor); its obligations and verification weight are owned by
[eval.md §8](eval.md#benchmarks) and
[live-stack.md §0](live-stack.md#verification-minimums), and its `LOAD_*` knobs
are defined in `tests/test_load_tier.py`. A persistent local
Qdrant on 6333 is `local:qdrant:up` / `local:qdrant:down`.

### 3.4 Retrieval Evaluation Gates & Quality Tiers

Tier map, thresholds, gate semantics, venue rules and the paraphrase no-echo
contract are owned by [eval.md](eval.md) (§1–§6); the rungs a change class owes
are owned by [live-stack.md](live-stack.md#verification-minimums) (§0, §3).
Runnable entries (prefix `sh scripts/tools/run-task.sh`):

| Task | Purpose |
|---|---|
| `eval:gate-l1` | Required PR check: runtime synthetic hash corpus into an ephemeral Qdrant simulator; fails closed on query failure or metric regression and posts the PR delta. |
| `eval:paraphrase` | Scores `evals/paraphrase.jsonl` against the mode-keyed paraphrase baselines; generate the runtime corpus and dedicated collection first (recipe in [eval.md §6](eval.md#paraphrase)); not wired into CI. |
| `eval:retrieval` / `eval:baseline` | Score the golden set, or re-record the mode-keyed baseline (dedicated PR). |
| `eval:bench` / `eval:bench-baseline` | Benchmark resources/latency, or re-record the baseline (dedicated PR). |
| `eval:harness:l2` / `eval:harness:l3` | Live-GPU tiers, run in the RC window (or a dedicated GPU runner / nightly); standing-red debt is RC debt. |

Mode selection, mismatch/skip semantics and baseline rules: eval.md §2/§8.

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

Local query/answer tooling sets its own defaults only when inputs are unset:
`QDRANT_URL=http://localhost:6333`, collection `mainframe_manuals`, and hash
embedding with `ALLOW_HASH_MODE=true` unless a model endpoint is configured.
Explicit environment and CLI values always win. Production settings and their
defaults are owned by the [agent Settings catalog](agent.md#7-settings-catalog);
operator keys by [`airgap.env.example`](../airgap.env.example); local 8 GB lane
pins by `scripts/local_lane_budget.sh` (§3.6).

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
  - `--limit-mm-per-prompt '{"image":0,"audio":0}'` rejects those modalities; it does not establish the memory saving.
  - `--max-num-seqs 1`: Bounds concurrent sequence allocation to prevent out-of-memory spikes.
  - `--enable-prefix-caching`: on for the LOCAL reasoning server (Budget `prefix_cache`; vLLM already caches by default — the pin guards flips, hit rate measured for issue #80); off for embed.
  - `--max-num-batched-tokens` (embed server): capped at the Budget window so the memory-profiling peak stays bounded; it does not follow a `MAX_LEN` operator override (erring small is the safe side).
  - `MAX_LEN=4096` for **both** servers: the reasoning prompt budget requires it, and a 2048 embed window was rejected by tokenizer sweep — the worst-case embedded string (chunk header + a `SECTION_MAX_CHARS=3500` body with the 400-char split seed) measures ~2,043 tokens at ~2.0 chars/token on syntax-dense text. The budget is pinned hermetically by `tests/test_embed_budget.py`.
* **Gemma-4 Support**: Automatically configures `--tool-call-parser gemma4`, `--reasoning-parser gemma4`, and `--chat-template /vllm-workspace/examples/tool_chat_template_gemma4.jinja`.
* **Task launch contract**: `local:llm`, `local:embed` and `local:rerank` pass the role and repository venv `BUDGET_PYTHON` per invocation; a missing `.venv` fails with an explicit setup instruction. Re-run the tokenizer sweep before changing chunk constants or the embed-text header.
* **Embedding Model Detection**: Model names matching `*embed*`/`*Embed*` (e.g. `Qwen/Qwen3-Embedding-0.6B`) derive `ROLE=embed` (overridable; `sh scripts/tools/run-task.sh local:llm` (or `local:embed` / `local:rerank`) passes `ROLE` explicitly) and get the Budget pooling-runner serving shape automatically.
* **Host-RAM cap and admission check** (issue #580): every container runs with `--memory=<N>m --memory-swap=<N>m` (equal values: no swap), so a runaway is OOM-killed inside its own cgroup instead of thrashing the host. `<N>` is the Budget per-role `host_mem_mb` (`BUDGET_HOST_MEM_MB`: embed 2600, reasoning 6400 for the 4B model (an unmeasured estimate) or 4500 for the 0.5B stand-in, rerank 4700); `HOST_MEM_MB=<MiB>` overrides it and is required for a profile that declares none. Before `docker run` the launcher refuses (exit 75, fixed message, nothing started) when `MemAvailable` < cap + `HOST_MEM_HEADROOM_MB` (default 2048) or when memory or IO PSI `some avg10` is >= `HOST_PSI_MAX` (default 10). `FORCE_START=1` skips only that admission check (the cap still applies). The `/proc/meminfo` and `/proc/pressure` paths are `HOST_MEMINFO` and `HOST_PSI_DIR` (tests stub them); a missing PSI file skips that check with a notice. The GPU `GPU_MEM` fractions are unchanged and are a ceiling on total device use including co-tenant servers. Local profiles still take the compiled reasoning path unless the profile sets eager; changing that alters GPU sizing and needs the budget owner's measurements.
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
| Full topology (reasoning + embedding + ranking) | `TRIPLE_8GB` | Above with `MODEL=Qwen/Qwen2.5-0.5B-Instruct GPU_MEM=0.20 MAX_LEN=4096 SEQS=1` on :8000, plus `sh scripts/tools/run-task.sh local:rerank` (:8002) | 0.5B answers are weak — plumbing/rerank coverage only. E4B triple demonstrably does not fit; resolve refuses it. |
| Retrieval + ranking, no LLM | `RANK_EMBED_8GB` | `sh scripts/tools/run-task.sh local:embed` (:8001) + `sh scripts/tools/run-task.sh local:rerank` (:8002) | Rerank A/B and `--rerank` evals without spending VRAM on reasoning. |
| Reasoning + ranking (no vLLM embed) | — | Unsupported | Hash embed mode pins `HashReranker` by design (determinism), so a GPU reranker is unreachable there — see issue #193. |

Reranked search also needs `RERANK_ENABLED=true RERANK_BASE_URL=http://127.0.0.1:4000/v1 RERANK_MODEL=BAAI/bge-reranker-v2-m3` on the consumer side (`query-demo`, eval `--rerank`, agent env). Launch order on a cold card: reasoning → embed → rerank (a 4k-context server fails KV init against leftovers; the profiles declare this allocation order).

#### Local Production Simulation (`sh scripts/tools/run-task.sh local:stack`)

The ownership contract is explicit: in production the platform team owns the model tier (vLLM + LiteLLM) and this repo owns Qdrant + ingest + retrieval + the agent, reaching every model leg only over the gateway HTTP contract. `sh scripts/tools/run-task.sh local:stack` reproduces that **complete topology on one machine** — pinned Qdrant + OpenObserve + the real LiteLLM gateway (digest-pinned) in front of the enabled local vLLM backends + the FastAPI agent — and probes every leg through the gateway **and verifies a trace landed in OpenObserve** before declaring the stack up. Agent and ingest never call vLLM directly, so the wire contract under test is the production one.

Prerequisites: Docker, `.venv`, reasoning on :8000 and embeddings on :8001. Start reranking on :8002 only when enabled. The current two-model rehearsal explicitly uses `RERANK_ENABLED=false`. Qdrant is started via `sh scripts/tools/run-task.sh local:qdrant:up` (the pinned-image owner) when unreachable; `sh scripts/tools/run-task.sh local:qdrant:down` stops it.

```bash
# Full stack: Qdrant -> OpenObserve -> gateway -> probe -> (optional ingest) -> agent -> smoke -> trace check
RERANK_ENABLED=false sh scripts/tools/run-task.sh local:stack
RERANK_ENABLED=false CORPUS_DIR=output/demo-pdfs sh scripts/tools/run-task.sh local:stack     # also ingest through the gateway
LOCAL_STACK_DRYRUN=1 sh scripts/tools/run-task.sh local:stack            # ordered plan only; no docker/network
```

Tracing is part of the stack, not a flag: the agent and (when `CORPUS_DIR` is set) the ingest run export OTLP to OpenObserve, and a `v1.search` span must land before the stack reports up. OpenObserve reuses an instance already answering on its port (an operator-managed one is left alone) or starts the digest-pinned owner (`scripts/run_local_openobserve.sh`); the real local LiteLLM gateway also exports its spans there so the gateway hop appears in the waterfall. Gateway spans leave the gateway only through an attribute allowlist (`scripts/gateway/trace_privacy.py`, #636): operation, model, token usage, HTTP/DB status and error type are kept; request/response content, API-key-derived metadata, `hidden_params`, key-management records, exception text, events and status descriptions are dropped, and `turn_off_message_logging` / `redact_user_api_key_info` are set for every logging callback. An owned OpenObserve runs in a container capped at `O2_MEMORY` (default `1g`, no swap) with its data in the named volume `O2_VOLUME`; a reused one keeps whatever bounds it was started with. The OpenObserve **browser UI** is at `http://127.0.0.1:5080`.

The operator console (ADR-0004) is part of the stack by default: the agent starts with `UI_ENABLED=true`, the up sequence smoke-checks `GET /ui` (HTTP 200), and the banner prints the console URL (`http://127.0.0.1:8080/ui`). Set `UI_ENABLED=false` to exercise the fail-closed 404 route set; `sh scripts/tools/run-task.sh local:agent` alone honors `UI_ENABLED` without a default.

On exit (Ctrl-C) the agent, the gateway, and an owned OpenObserve stop; Qdrant stays for `sh scripts/tools/run-task.sh local:qdrant:down`. Ports: agent 8080 (`LOCAL_AGENT_PORT`), gateway 4000 (`GATEWAY_PORT`), OpenObserve UI and OTLP 5080 (`O2_PORT`), Qdrant `QDRANT_URL` (default `http://127.0.0.1:6333`). Ephemeral keys are written mode 600 to a temp env file (`GATEWAY_ENV_FILE`, default `/tmp/local-stack-gateway-<port>.env`) — never inside the repo, never committed.

The gateway contract the stack exercises:

* **One origin per leg, model-id routing**: `EMBED_BASE_URL`, `LLM_BASE_URL`, and `RERANK_BASE_URL` all point at `http://localhost:4000/v1`; the requested `model` id selects the backend.
* **Per-leg Bearer virtual keys**: the agent/ingest `*_API_KEY` values are LiteLLM virtual keys; missing/unknown keys get 401 on every route (including the rerank legs), so key wiring is exercised for real.
* **Both rerank legs**: native `/v1/rerank` via the `hosted_vllm/` provider, plus a `/v1/score` pass-through to the vLLM backend (LiteLLM has no native score route). `scripts/probe_gateway.py` prints the recommended `RERANK_ENDPOINT_ORDER` after probing both.
* **Tokenizer**: `/tokenize` stays 404 behind the gateway — the agent pins its in-process estimator after one warning (expected, not a fault).

For single-component debugging, `sh scripts/tools/run-task.sh local:openobserve:up` / `sh scripts/tools/run-task.sh local:openobserve:down` manage just the trace backend, and `sh scripts/tools/run-task.sh local:gateway:up` keeps the gateway in the foreground (`sh scripts/tools/run-task.sh local:gateway:down` stops it and its key store); then export the printed keys and run `scripts/probe_gateway.py --require-reasoning --stream` and `sh scripts/tools/run-task.sh local:agent` yourself. The key store is a throwaway Postgres container + named volume (LiteLLM `/key/generate` needs a database): env-passed keys survive restarts, minted keys rotate per start (`GATEWAY_RESET_KEYS=1` wipes the store). `scripts/run_local_gateway.sh` owns gateway config rendering; the LiteLLM image is pinned by digest there. The model/gateway launcher is local-only. CI supplies its own gateway deployment and deterministic model computation; neither deployment belongs on a product path. The shared strict-finish module has a gateway-only LiteLLM import exception and is excluded from application images.

---

### 3.7 Reasoning Performance, Query Complexity & Context Budgeting

Complexity classification, adaptive budgets, and the truncation analysis
live in `architecture.md` §4.4 (design) and `agent.md` §4 (knobs) — one
rule, one owner. Operational upshot: simple lookups stay fast and cheap;
complex diagnostics/procedures think longer and cost more GPU time. Tune
via the `LLM_REASONING_EFFORT_*` / `PROMPT_MAX_CONTEXT_CHARS*` Settings
(`agent.md` §7), never by editing prompts.

---

### 3.8 Local End-to-End Verification

The secondary `qa:vllm-e2e` command and its script are retired. Use the
[canonical supervisor](live-stack.md#full-local-simulation) with its private
gateway handoff, explicit model revision and selected dimension (commands in
[§3.6](#36-local-vllm-inference--gpu-acceleration-rtx-5060--8gb-vram)).

Check an existing stack with `sh scripts/tools/run-task.sh local:check --
--agent URL --trace-url URL --query TEXT --followup TEXT --report PATH`; the
[retained-stack runbook](local-real-corpus.md#4-verify-and-retain-the-complete-live-stack)
owns endpoint and private-report selection. It requires grounded first and
follow-up answers, complete SSE and a fresh search trace (`--trace-backend` per
[observability](observability.md#backend-status)); OpenObserve credentials come
from `TRACE_QUERY_USER` and `TRACE_QUERY_PASSWORD`. Representation changes use
the [snapshot-gated ingestion migration](ingest.md#metadata-contract). For
semantic answer quality on the configured live GPU stack, run
`sh scripts/tools/run-task.sh eval:answers` under the
[answer-tier contract](testing.md#answer-tier).

#### Streaming Reasoning on the Local Stack

`local:agent` starts the agent with `LLM_STREAM=true` for TTFT measurement
(production default `false`); it honors `UI_ENABLED` and serves `/ui` when true.
The frame contract (`token*` → exactly one `final`; `error` ends without
`final`), the console/chat contracts and a streamed probe are owned by
[agent.md](agent.md) §1/§3 and [§5.2](#52-agent-endpoints).

### 3.9 Exporting Standalone Model Weights for Offline Bastions

This is a separate platform-team model handoff, outside the application bundle.
The platform team chooses and verifies model revisions, mirrors weights, and owns
serving and gateway acceptance. The product consumes the resulting URLs, model
IDs, dimensions and per-leg keys. For the exact pinned local model views used in
the rehearsal, follow [the release guide](release-verification.md#pinned-models).

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
and drain readers for any maintenance that writes outside publication. These commands are operator actions,
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
* **Corrupted / Partial File Safety**: An interrupted or failed build never publishes a partial corpus. Its staging contract stays `pending`, so an ordinary re-run refuses before any parse; resume it with `--reingest`, which reuses that build's verified document checkpoints and reprocesses the rest (`docs/ingest.md` §9). The local stack and Colab lanes expose this as an explicit per-run choice (`LOCAL_INGEST_REINGEST=1`, `COLAB_INGEST_REINGEST=1`).

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

The hardened 5-stage deployment pipeline (`airgap:validate` -> `airgap:load` -> `airgap:deploy` -> `airgap:ingest` -> `airgap:smoke`; `airgap:pack` is the connected-host pre-step that builds the sneakernet bundle) is the **canonical deployment standard across the entire project**. Both production air-gapped OpenShift and local testing environments adhere to this pipeline (using the same scripts and Helm charts, with adapted sizing and SCC for local test clusters).

### 4.1 Packaging on the Connected Host (Image Factory)

The sneakernet archive is packaged on the connected host. Connected `main` is the only image factory.

> [!TIP]
> **Automated CI Packaging:** A nightly `e2e.yml` run (02:17 UTC, current `main` HEAD) or a manual dispatch triggers the `airgap-package` job once container images have been built and pushed to GHCR; merges to `main` alone no longer package a bundle. Any authenticated GitHub user with read access to the repository can download the pre-packaged `sneakernet-bundle-<sha>` artifact (containing `qdrant-pdf-rag-<sha>.tar`, digest, `PACKING_RECORD.txt`, and `MANIFEST.txt`, retained for 90 days) directly from GitHub Actions runs. The artifact is stored uncompressed (`compression-level: 0`; the download is byte-identical). Pull request runs intentionally skip this packaging job. Only a manual `workflow_dispatch` on `main` signs with the custodied key (`release-signing` environment, required reviewer; [custody](deploy.md)). Nightly and branch bundles are `signed: ephemeral` (plumbing only, no trust) and not promotable. To get a bundle for a specific commit, dispatch `e2e.yml` on it. A failed nightly run shows on the Actions page and in GitHub's scheduled-workflow failure email.

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

`airgap:pack` refuses to sign unless the working tree is the clean checked-out
commit (no tracked staged or unstaged change, no assume-unchanged/skip-worktree
flag; untracked `airgap.env` and `dist/` are fine) and every
`images/Containerfile.*` `FROM` digest equals the base pin in `images.txt` and
the dependency lock (`python3 scripts/image_inventory.py --check-containerfiles`);
every `COPY/ADD --from` and `RUN --mount ...from=` must name an earlier build stage or
the `wheelhouse`/`bm25` build contexts (#1030).

A maintainer creates the signing key once in a protected directory, for example
with `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out signing.pem`,
and exports its public half with `openssl pkey -in signing.pem -pubout -out signing.pub`.
Store the private PEM as the GitHub `SNEAKERNET_SIGNING_KEY` secret through the
approved credential process; distribute the exact public PEM independently to
operators. Back up the private key securely. A throwaway key tests signing
mechanics only and does not authorize release transfer. Without a configured
secret CI may label a package `signed: ephemeral`; reject it for promotion.

`REDHAT_REGISTRY_USER` / `REDHAT_REGISTRY_PASSWORD` are the approved Red Hat
registry service-account credentials. Do not paste them into issue/PR text.

This generates `dist/qdrant-pdf-rag-<sha>.tar` and its digest
`dist/qdrant-pdf-rag-<sha>.tar.sha256`. The bundle holds the git bundle, the app
images, the vendored Qdrant/OpenObserve/otelcol images (and the oauth-proxy
sidecar when its pin is recorded), the Helm chart, `bootstrap.sh`, the signed
manifests/SBOM/notices, the pinned host runner and the offline reference docs.
Pack reuses the installer's `.tools/cache`, or an explicit
`AIRGAP_TASK_ARCHIVE=<absolute path>`, for that runner archive. The reference
docs step downloads about 1.4 GB of pinned upstream doc archives (deleted after
use) unless `AIRGAP_REFDOCS_CACHE=<directory>` holds them named by sha256, in
which case it never downloads. [Deploy §4](deploy.md#4-signing-and-provenance)
owns the member contract and provenance rules; do not restate them here.

### 4.2 Transfer & Automated Bootstrap

Before production transfer, complete [the release gate](release-verification.md)
and [verification record](release-record.md) against the published-main
bundle. Missing or failed checks block transfer. Send the identical tested
tarball; do not rebuild or repack after verification. The steps below are also
used for the fresh local Kind bootstrap rehearsal.

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

Keep `SNEAKERNET_TRUSTED_PUB` exported for every later stage: outside dry-run,
load, deploy, ingest, validate and retire-legacy refuse to run without it,
and refuse any checkout that is not this verified signed release
([deploy §4](deploy.md#4-signing-and-provenance)).

The `bootstrap.sh` script verifies the bundle signature and every member
checksum, clones `repo.bundle`, verifies HEAD against the manifest, installs the
bundled pinned Task without network access or preinstalled Make/Task, seeds
`airgap.env` only when absent, stages `dist/` before promotion, and unpacks the
verified reference docs to `qdrant-pdf-rag/dist/reference-docs/`: open
`INDEX.md` there for this repository's docs and the version-matched upstream
docs, readable with any text viewer or browser ([reference docs](deploy.md#reference-docs)). The trust
chain, staging/interruption recovery, refusal messages and the executing-checkout
guard are owned by [deploy §4](deploy.md#4-signing-and-provenance); read them
before a recovery attempt. A rerun never advances an existing workspace to a new
release: check out the approved bundle SHA deliberately (or use a fresh
`AIRGAP_WORKSPACE`) first.

After bootstrap use `sh scripts/tools/run-task.sh --list` for discovery.
Rollback uses the previous approved bundle and its own bootstrap/command
contract in a separate workspace; an older bundle may still use Make. Never
combine its assets with a newer checkout ([format change](deploy.md#bundle-image-format)).

### 4.3 Configure Environment & Pre-Flight Validation

Edit `airgap.env` to configure your cluster environment:

> [!NOTE]
> Environment precedence (explicit env beats file, `AIRGAP_ENV` path beats
> local `./airgap.env`) lives in [deploy.md §2](deploy.md#2-environment-precedence).
> Two re-run notes: `VAR=x sh scripts/tools/run-task.sh airgap:validate` overrides a stale key, but a bare
> `sh scripts/tools/run-task.sh airgap:validate` reuses whatever the file still holds — re-run
> `sh scripts/tools/run-task.sh airgap:validate` after editing `airgap.env` before touching the cluster.

Every operator key, with its placeholder and constraints, is catalogued in
[`airgap.env.example`](../airgap.env.example): registry, namespace, storage
classes, sizing, image roles, model endpoints and budgets, request bounds,
tracing, console, ingress NetworkPolicy, feedback, Open WebUI, ingest and
collection policy. Bootstrap seeds `airgap.env` from it; fill the site values
there and never commit that file. Propagation and validation are owned by the
[configuration contract](deploy.md#configuration-contract).

#### Platform model endpoints (reasoning / embed / optional rerank)

The platform team supplies embedding and reasoning endpoints and, when enabled,
a reranker. Set the actual model IDs and dimensions in `airgap.env`; the example
placeholders above are not deployment values. Keep reranking disabled unless
the site explicitly enables it:

All model IDs below may be platform aliases. `GATEWAY_BASE_URL` supplies missing
per-operation API bases. With `GATEWAY_API_KEY_SECRET_KEY` set, that shared data
key replaces every per-leg key named in the table; leaving it unset keeps the
default per-leg keys.

| Role | URL key | Model key | Notes |
|---|---|---|---|
| Reasoning (answer, chat and console) | `LLM_BASE_URL` | `LLM_MODEL_REASONING` | Empty model = answers stay disabled. `LLM_MAX_MODEL_LEN` defaults to 32768 and must not exceed the served context: lower it when the platform serves a smaller window, because nothing in the request path can read the served window through the gateway (tokenizer uses the server `/tokenize`, estimator fallback otherwise). Auth: `llm-api-key` from the `GATEWAY_API_KEY_SECRET` Secret (unset = keyless). |
| Embed (`/v1/search`, ingest) | `EMBED_BASE_URL` (defaults to `GATEWAY_BASE_URL`) | `EMBED_MODEL` + `DENSE_DIM` + `EMBED_MODEL_REVISION` | `DENSE_DIM` is required and fail-closed: it must equal the served native dim (4096 for Qwen3-Embedding-8B). Collections are created at that width; a mismatch against an existing collection refuses with `DimMismatchError`. `EMBED_MODEL_REVISION` is the operator-declared immutable model/config revision (a gateway alias is mutable and a dimension is not an identity): blank/whitespace-only values fail pre-flight and refuse agent/ingest startup, and a revision change requires a deliberate `--reingest` migration. Auth: `embed-api-key` from the same Secret; the ingest Job reads it too. |
| Rerank (optional, default off) | `RERANK_BASE_URL` (defaults to `GATEWAY_BASE_URL`, no embed fallback) | `RERANK_MODEL` | Served via a vLLM pooling server (`--runner pooling`, `/v1/score`; TEI `/v1/rerank` fallback). Use the platform gateway rerank leg and its model ID; do not configure a direct-backend consumer shortcut. Lifespan logs a loud warning (never a refusal) when the endpoint is unreachable at startup. Auth: `rerank-api-key` from the same Secret. Leg order: run `probe_gateway.py` below to decide — `score_first` whenever the score leg answers (even on a gateway exposing both legs), `rerank_first` only when the score leg is unavailable. |

Set the namespace to the same value chosen in `airgap.env` and create it if the
site has not already provisioned it. Do not source a local-development handoff in
this operator shell. Environment exports override `airgap.env`.

```sh
export NAMESPACE=mainframe-rag
oc create namespace "$NAMESPACE" --dry-run=client -o yaml | oc apply -f -
```

Create the key Secret **before** `sh scripts/tools/run-task.sh airgap:deploy`
(one shared data key as selected above, used by agent and ingest):

```bash
# Files are mode 600, supplied through the platform credential process.
oc -n "$NAMESPACE" create secret generic gateway-api-keys \
  --from-file=api-key=/secure/gateway/api-key
oc -n "$NAMESPACE" create configmap gateway-ca \
  --from-file=ca-bundle.crt=/secure/gateway/ca-bundle.crt
```

Supply a complete CA bundle containing every required root: `SSL_CERT_FILE`
replaces the client's default bundle. Mounting this ConfigMap makes agent and
ingest trust the gateway; node registry trust and browser ingress trust remain
separate. Test correct CA, wrong CA and hostname mismatch from actual pods.

`sh scripts/tools/run-task.sh airgap:validate` verifies the Secret exists (when the namespace does) and refuses plaintext `*_API_KEY` values in `airgap.env`. Rotate by updating the Secret, then rollout-restart the agent (or re-run the ingest Job).

#### Pre-Flight Validation (`sh scripts/tools/run-task.sh airgap:validate`)

Before modifying any cluster state, run the pre-flight validation check to verify tools, required variables, storage class compliance (refusing NFS for the data and snapshot classes), strict operator booleans, and required keys. A denied (Forbidden) cluster read is reported as unverified, never as absent or as a non-OpenShift cluster. It prints OpenShift SCC guidance but does not verify SCC permissions:

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
`QDRANT_EXTRA_VALUES`, `INGEST_EXTRA_PATCH` and the tiny rehearsal claims are local
rehearsal overrides. [The release guide](release-verification.md#deploy-candidate) owns those values.

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
site's SCC, identity, storage, registry and network acceptance evidence
([site OpenShift acceptance](#site-openshift-acceptance)); a local Kind pass does
not replace it.

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
volume group. OpenObserve also leaves IDs to admission. Application images make
only their cache and work directories group-0 writable.

Require actual `restricted-v2` admission and successful data, snapshot,
ingest-work and trace volume writes under
[site OpenShift acceptance](#site-openshift-acceptance).
An admission or storage failure blocks promotion and requires a fix in the
owning production configuration followed by a new bundle and verification.
Do not grant `anyuid`, substitute another project's IDs, or hide security
changes in `QDRANT_EXTRA_VALUES`.

#### Qdrant Inter-Node Gossip (p2p TLS) Note
In `charts/qdrant-openshift.values.yaml`, `config.cluster.p2p.enable_tls: false` is set because cluster gossip is plaintext on the CNI; we do not mount `./tls/cert.pem` (avoiding crashloops on startup).

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
selects the bundled OpenObserve OSS backend (ADR-0006, #956; the only trace
backend, Jaeger is retired), and `sh scripts/tools/run-task.sh airgap:deploy`
renders and installs it with its otelcol collector from `charts/mainframe-rag`,
wires the export into the agent and the ingest Job, and checks both images
against the packed digests (registry read-back before the release, running pods
after rollout). To disable tracing and skip both workloads, set
`OTEL_EXPORTER_OTLP_ENDPOINT=off` (also `none`, `false`, or `0`) in
`airgap.env`. A custom `http(s)` origin is passed through unchanged; anything
else fails closed before a manifest is rendered. `JAEGER_ENABLED` and
`JAEGER_QUERY_URL` no longer exist and are refused when set (#1070). `sh scripts/tools/run-task.sh airgap:validate` prints the
resolved mode.

This traces **this repo's components only** (agent, retrieval, ingest); the
platform team's model tier keeps its own monitoring. Outbound model calls
carry W3C `traceparent` so their gateway can correlate its spans with ours
when it is tracing-enabled — nothing from this repo configures or deploys
their monitoring.

The sneakernet bundle always carries the OpenObserve and otelcol images (`images.txt` is a
pack-wide contract — every pin with a recorded digest is mirrored on every
pack; the oauth-proxy pin is skipped while `sha256:PENDING`), so the
default-on path works in a disconnected install. The endpoint may be given
with or without the `/v1/traces` path — the agent accepts both.
`sh scripts/tools/run-task.sh airgap:smoke` proves a `v1.search` span landed before reporting
acceptance (empty-collection runs report tracing as skipped, trace arrival NOT
VERIFIED). With no manual env, smoke queries `http://openobserve:5080` and reads
the credentials from the `openobserve-credentials` Secret at run time (never
printed); `TRACE_QUERY_URL`, `TRACE_QUERY_USER` and `TRACE_QUERY_PASSWORD` override.

**Bundle size (#956).** Jaeger v2.20.0 leaves the bundle and OpenObserve v1.1.2
plus otelcol-contrib 0.160.0 enter it. By registry layer sizes read with
`skopeo inspect --raw` for linux/amd64 (compressed): Jaeger -50.2 MB,
OpenObserve +161.8 MB, otelcol +110.8 MB, net +222.4 MB. Since #1066 the
bundle's `oci-archive` members keep those compressed registry blobs, so the
bundle grows by about the same amount.

Every exported span carries deploy identity as resource attributes:
`service.version` is the packed `IMAGE_SHA` for the agent Deployment, and
`deployment.environment` comes from the optional
`OTEL_DEPLOYMENT_ENVIRONMENT` in `airgap.env` (e.g. `prod`; omitted when
unset) — so traces from lab and prod sharing one backend stay
unambiguous. The ingest Job does not carry `IMAGE_SHA` yet, so its spans have
no `service.version` (issue #315). The agent also honors the standard
`OTEL_RESOURCE_ATTRIBUTES` mapping underneath these explicit keys, and joins
an upstream W3C `traceparent` when the caller sends one.

View traces (port-forward only — OpenObserve has no public Route, like Qdrant):

```bash
oc -n mainframe-rag port-forward svc/openobserve 5080:5080
# open http://localhost:5080, service "mainframe-rag-agent" (rename via OTEL_SERVICE_NAME
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
   fails closed when credentials are absent or rejected. Keep personal
   pull secrets on the workstation. The OCI archive omits unsupported
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
   probes. In-cluster tools of the release namespace (and of
   `AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES`) keep using the ClusterIP 8080 port
   (unauthenticated by design, no Route); other namespaces are denied by the
   default-on `rag-agent-ingress` NetworkPolicy (set `AGENT_NETWORK_POLICY=false`
   to opt out; [rules](deploy.md#agent-networkpolicy)).

`sh scripts/tools/run-task.sh airgap:validate` with `AGENT_ROUTE=true` checks the
recorded and chart/bundle-consistent oauth-proxy pin (static, also in dry-run),
the cookie Secret key, the namespace service CA and that no other Route exposes
the agent or Qdrant Services (live). `deploy.sh` repeats these checks, verifies
the live Route after the release and deletes it if it is not the OAuth reencrypt
contract (details: `docs/deploy.md`).

> **Connected-host follow-up before an air-gap cut:** a `requirements.lock.txt`
> bump (e.g. the `jinja2` + `python-multipart` pins the console needs) requires
> `sh scripts/tools/run-task.sh artifacts:wheelhouse artifacts:bm25` plus a connected image rebuild/push and a
> fresh pack — air-gap images install only from the baked wheelhouse.

### 4.4.3 Answer-feedback service (optional, default off, ADR-0005)

The console can rate answers and keep them as quality records. The records hold
question and answer text, so they live only in the feedback service's own claim.
Rules, lifecycle and limits: [deploy.md](deploy.md#feedback-service).

1. Create the token Secret once, in the release namespace. The value never goes
   into `airgap.env`, values or git:

   ```sh
   oc -n "$NAMESPACE" create secret generic rag-feedback-token \
     --from-literal=service-token="$(openssl rand -hex 32)"
   ```

2. Set `FEEDBACK_ENABLED=true` and `FEEDBACK_SECRET=rag-feedback-token` (and, if
   wanted, `FEEDBACK_STORAGE_SIZE` and `FEEDBACK_RETENTION_DAYS`), then run
   `airgap:validate` and `airgap:deploy`. Validation refuses a missing Secret
   name or key, a malformed size or retention, and `AGENT_NETWORK_POLICY=false`
   before anything changes. The deploy waits for `statefulset/rag-feedback`.
3. Check: `oc -n "$NAMESPACE" get statefulset,pvc,networkpolicy rag-feedback
   rag-feedback-data rag-feedback-ingress`; the pod is Ready; rate an answer in
   the console and confirm the next ordinary answer is recorded (the rating
   control works for it). With `METRICS_ENABLED=true` the service's
   `rag_feedback_purge_total{outcome="truncated"}` rises after the first purge
   (about 30 s after the pod starts); `deferred` or `error` series that keep
   growing mean expired text may still be on the volume.
4. Disabling (`FEEDBACK_ENABLED=false`) removes the workload but **keeps the
   claim and its content, and nothing purges while it is off**. To retire the
   feature for good, delete the claim yourself: `oc -n "$NAMESPACE" delete pvc
   rag-feedback-data`. Any backup or snapshot of the claim also holds question
   and answer text and must expire within the retention window.
5. Rotating the token: replace the Secret value, then restart both sides:
   `oc -n "$NAMESPACE" rollout restart statefulset/rag-feedback deployment/rag-agent`.

#### Exporting and importing feedback records (#805, ADR-0005)

An export holds question, answer and comment text. It is created inside the
pod, so the content travels only over the `oc exec` stream into a file you
create with `umask 077`, outside any git work tree. It escapes the 180-day
purge, so **you delete it** when done (`shred -u FILE` or equivalent); the
header records `exported_at` and `retention_days`. Never paste, attach or
upload it.

- **Export** (service running; read-only, takes no write lock):

  ```sh
  (umask 077; oc -n "$NAMESPACE" exec rag-feedback-0 -- \
    python -m mainframe_rag.feedback.exchange export --db /data/feedback.sqlite3 --out - \
    > feedback-export.jsonl)
  ```

  It reads one snapshot and closes the store before writing. Exporting at the
  instant the scheduled purge fires can add one `deferred` purge count; the
  retry ladder converges.
- **Import into an empty store** (a new claim or a Postgres-move rehearsal): the
  target must not be served. Copy the file into a pod that mounts the claim
  with the service stopped, then run `... exchange import --in FILE --db
  /data/feedback.sqlite3`. A non-empty target is refused.
- **`--merge` into a populated store:** first stop the writer. Stopping it is
  the control against a second writer on the live store. The import's
  exclusive-lock probe (refusal `store_in_use`) is only a backstop: it sees a
  connection held at that moment, not one started afterwards, and is unreliable
  across hosts or NFS: `oc -n "$NAMESPACE"
  scale statefulset/rag-feedback --replicas=0`, wait for the pod to end, run
  the import from a one-off pod mounting `rag-feedback-data` with the
  application image and the same restricted security context, delete that pod,
  then `--replicas=1`. Conflicts refuse the import (exit 2); rerun with
  `--keep-existing` only if keeping the target's rows is intended. Rows past the
  retention window (`expired` in the counts) are removed by the first purge
  after restart (about 30 s).
- Stdout of import is counts only. Delete the file and any copy afterwards.

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

<a id="site-openshift-acceptance"></a>
#### Site OpenShift acceptance (before go-live)

The [release gate](release-verification.md) has no local OpenShift lane, so these
checks are **not run before transfer**. Run them on the site after the pipeline,
against the transferred bundle, before users get access. Any failure blocks
go-live and requires a fix in the owning production configuration and a new
candidate; never grant `anyuid`, disable SCC, or hide a security fix in a
site-only values file. Record evidence before replacing pods, so their original
admission and image IDs stay attributable. Checks that need cluster-scoped access
beyond the namespace record the platform team's evidence and owner instead.

| Check | Required evidence |
|---|---|
| SCC and identities | Every product pod, completed ingest Job and console OAuth container has `openshift.io/scc` exactly `restricted-v2`; namespace UID/group ranges, admitted security contexts and actual process IDs match; no `anyuid` grant. A denied pod or unwritable volume blocks go-live. |
| Images | Each running container's `imageID`, including OAuth, matches the loaded release artifacts under [the image-identity procedure](deploy.md#image-identity-across-archive-and-registry-formats). |
| OAuth Route | A fresh unauthenticated `GET /ui` may show the provider chooser (403); `/oauth/start` redirects to OpenShift OAuth; login returns to `/ui`; a fresh session still requires authentication. No direct public API, Jaeger or Qdrant Route. |
| Certificates | Browser and CLI verify the Route hostname and ingress chain without bypass. The Route is `reencrypt`, targets OAuth 8443 and trusts the Service CA; the serving Secret matches its service identity. |
| Agent ingress | `oc -n "$NAMESPACE" get networkpolicy rag-agent-ingress` exists (or `AGENT_NETWORK_POLICY=false` is a recorded site decision). Pods stay Ready after it is applied (probes). From a pod in a **different, unlisted** namespace, `rag-agent:8080` and the pod IP `:8080` fail while the same call from this namespace succeeds; the Route login still works (router to 8443) and a UWM scrape of `/metrics` succeeds when metrics are on. Record that the router and monitoring namespaces carry the `network.openshift.io/policy-group` labels (cluster-scoped read: platform evidence) and that no site allow-all policy selects the agent pods (policies are additive and would void the restriction). Repository tests prove the rendered rules only, not enforcement. |
| Feedback ingress (only with `FEEDBACK_ENABLED`) | `oc -n "$NAMESPACE" get networkpolicy rag-feedback-ingress` exists. From an agent pod, `rag-feedback:8080/healthz` answers; from the ingest Job pod, a debug pod and a pod in another namespace, `rag-feedback:8080` and the pod IP fail; an unauthenticated request to an agent-reachable data route is 401. The claim `rag-feedback-data` is Bound on the intended block class and the pod writes it under `restricted-v2`. Record that no site allow-all policy selects the feedback pod (policies are additive) and, for a default-deny egress site, that agent to feedback is allowed. Repository tests prove the rendered rules only, not enforcement. |
| Egress | Prove a public TCP destination is reachable from each application policy context first; after the namespace default-deny egress policy with narrow DNS, Qdrant, OpenObserve, OAuth/API and gateway allows, a bounded direct-IP connection to that address fails while DNS and gateway calls succeed. Repeat ingest, gateway streaming probe, retrieval, tracing, OAuth login, cited answers and stream endings under the policy. An unresolved name or invalid certificate is not evidence of blocking. |
| Node pulls | An uncached public image pull fails and an uncached internal-registry pull succeeds, recorded with digests and events. A cached image proves neither; NetworkPolicy success does not imply node isolation. |

```sh
oc -n "$NAMESPACE" get pods -o custom-columns='NAME:.metadata.name,SCC:.metadata.annotations.openshift\.io/scc,UID:.spec.securityContext.runAsUser,GROUP:.spec.securityContext.fsGroup'
oc get namespace "$NAMESPACE" -o jsonpath='{.metadata.annotations.openshift\.io/sa\.scc\.uid-range}{"\n"}{.metadata.annotations.openshift\.io/sa\.scc\.supplemental-groups}{"\n"}'
```

Pod-level fields may be empty when a container has its own context; inspect both
and run `id` where the image provides it. Save only sanitized resource metadata;
never archive Secret YAML, token-bearing login commands or browser cookies.

##### Open WebUI go-live checks

Open WebUI is the default-on main docs platform ([ADR-0008](adr/0008-open-webui-main-docs-platform.md)),
and every [ADR-0007](adr/0007-open-webui-docs-front-end.md) condition is a release
gate, so each check below blocks go-live on failure. A failure also blocks a site
that keeps it on. The same rules apply as above: no `anyuid`, no site-only fix, and
evidence holds ids, states and counts, never chat text, tokens or Secret content.
The Kind acceptance lane stands in for the IdP, Route and SCC, so none of these
checks is proven before the site run. Inputs and the AD FS client are described in
[the Open WebUI section of deploy.md](deploy.md#docs-front-end): the paragraphs
"Enabling it at a site (default)" and "Register Open WebUI with AD FS".

| Check (gate) | Required evidence |
|---|---|
| Admission with an arbitrary UID (R1) | The `rag-openwebui` pod and its sidecars carry `openshift.io/scc` exactly `restricted-v2`. `id` in the container shows a UID from the namespace range with group 0, not an image-fixed UID. No `anyuid` grant. |
| Own Route only (R3) | `oc -n "$NAMESPACE" get route` lists `rag-openwebui` as `reencrypt`, with the `OPENWEBUI_HOST` host. No other Route points at the `rag-openwebui` Service. Browser and CLI verify the certificate without bypass. |
| AD FS sign-in, user and admin (R3) | A test account in the allowed-role group signs in and appears in the admin user list with role `user` (not `pending`). A test account in the admin group signs in and has role `admin`. A test account in no role group is refused (a redirect to `/auth?error=...`, no session). With the [LDAP-attribute-only rule form](deploy.md#adfs-ldap-only-rule-form) the role lists are domain-qualified group names, single-quoted in `airgap.env`. Record the claims check in role-list mode before the deploy: exit 0 for a docs-group account, and exit 0 with `--expect-refused` for an account in neither docs group. Record the ID token size of the test account with the most groups. Above 8000 bytes, show that sign-out of that account returns to the IdP without an error. |
| Role revocation (R3) | Two tests. **User:** remove a test user from the role group and sign in again: expect a refusal (`/auth?error=...`, no session) when no role group is left, or demotion to `user` for an admin who stays in the user group. **Admin holding a Functions-API key:** (1) the test admin holds a key allowed on `/api/v1/functions`; show it works (for example a listing of Functions answers 200). (2) Remove that admin from the admins group and every role group. (3) Sign in again: expect a refusal (`/auth?error=...`, no session), while the stored role stays admin. (4) Show the key still works. This is the documented residual: the IdP change applies only at the next successful sign-in, and the chart restricts keys to admins and to `/api/v1/functions`, so a removed admin keeps Functions-API admin until demoted. (5) Demote or delete the account in the Open WebUI admin UI. (6) Show the key is now refused. (7) If that admin owned the installer's `admin-api-key`, rotate it (deploy.md, "Admin API key (installer)"). Record ids and status codes only. |
| Docs model only (V2) | A signed-in user and an admin see only the Docs model. No other model, connection, tool or tool server is listed. Signup and password login are refused. |
| Answer with citations (V3) | An ordinary user asks one corpus question. The answer streams to the end and shows source chips with documents. The console `/ui` still works afterwards. |
| Egress probe from the pod (R2) | From inside the pod, bounded direct-IP TCP attempts, with the tool the image provides as under the Egress row above. Reachable: the agent Service, cluster DNS and the IdP addresses. Blocked: a public address, the API server, Qdrant, the gateway and a pod in another namespace. Record that no policy grants this pod egress beyond the chart's allow-list (agent, DNS, IdP): list the policies that select the pod (`oc -n "$NAMESPACE" get networkpolicy -o yaml`, names and egress rules only); a namespace default-deny that also selects it is expected and adds nothing. Namespace-admin can run the pod-side attempts and read the namespace policies. The API-server and other-namespace targets, and node-level flows, need platform evidence from the network team. |
| Blocked endpoints at the router (E1) | With `$C` a signed-in user's session cookie and `$H` the Route host, run the POST loop from the "Allow-list proxy" paragraph of [deploy.md](deploy.md#docs-front-end) through the real router and Route (the paragraph's "Site acceptance" block, not a port-forward). Every path must return `403` (`400` for the null byte), and nothing may be stored. An ordinary chat must still answer through the Docs model. |
| Storage class (S1) | `oc -n "$NAMESPACE" get pvc rag-openwebui-data -o jsonpath='{.status.phase} {.spec.accessModes} {.spec.storageClassName}{"\n"}'` shows `Bound`, `ReadWriteOnce` and the intended class. The class is block storage, never NFS: `oc get storageclass` and its provisioner need cluster-read, otherwise platform evidence from the storage team. |
| Backup retention (S3, site only) | Platform evidence from the backup owner: backups of `rag-openwebui-data` are kept at most 30 days (plus one backup cycle), match `OPENWEBUI_RETENTION_DAYS`, and stay inside the enterprise. Namespace-admin cannot see the backup system. |
| Session lifetime (R3) | After sign-in, the session token expires in about 8 hours (`JWT_EXPIRES_IN`): decode the session token's `exp` against `iat` (claims only, never the token itself), or confirm the setting in the pod's rendered environment (`oc -n "$NAMESPACE" get deploy rag-openwebui -o yaml`, name and value of `JWT_EXPIRES_IN` only). |
| No vector store or uploads (V1) | After the answer above, the data directory on the claim has no vector store directory, no vector tables and no `uploads/` content. List names and counts only. |
| Retention purge scheduled (S2) | The chat-purge sidecar is running and its state file on the claim reports `consecutive_failures` 0 and a recent `last_success`. `OPENWEBUI_RETENTION_DAYS` matches the site decision (default 30). |
| Console fallback (ADR-0008 item 5) | The OAuth console Route and `/ui` still work with Open WebUI up. |
| Rollback leaves the PVC (K1) | Set `OPENWEBUI_ENABLED=false` and re-run `airgap:deploy`. The Deployment, Service, Route, NetworkPolicy and ConfigMap are gone, `rag-openwebui-data` is still Bound, and the agent and console are unaffected. Then set it back to `true` and re-deploy; the next ordinary chat succeeds. |

Record the platform team's evidence and owner for any step that needs cluster-scoped
access.

##### Upgrading an existing site

A site that moves from a release without Open WebUI **must** choose before the next
`airgap:validate` or `airgap:deploy`: set the `OPENWEBUI_*` inputs and create their
Secret, or set `OPENWEBUI_ENABLED=false`. With neither, validation refuses before any
cluster change and the running release is untouched (fail closed, ADR-0008 items 3
and 4). Follow the "Upgrade consequence for existing sites" paragraph of
[deploy.md](deploy.md#docs-front-end), and review the new keys in `airgap.env.example`.

If the new release changes the extraction rules version, the stored corpus is stale:
a non-empty collection written by a different rules version refuses an ordinary ingest.
Re-ingest it with `--reingest` (see "Extraction-rules changes" in
[section 3.10](#310-managing-collections--real-world-ingest) and
[ingest.md section 9](ingest.md#9-orchestration-run_ingestpy-inventorypy)) before go-live,
and confirm `/healthz` reports the representation ready.

### 4.7 Local Cluster Testing Standard (Kind + Local Registry)

For a clean local deployment with real models and an optional full corpus
checkpoint, start with [the fresh Kind guide](local-kind.md).

To test the deployment scripts and Kubernetes manifests locally without access to an OpenShift cluster, the project standardizes on **Kind** (Kubernetes-in-Docker) paired with a local registry container on port 5000.

Kind is also the release rehearsal: production release transfer requires
[the release gate](release-verification.md). OpenShift SCC, OAuth, Route, Service
CA and isolation checks are not exercised in Kind; they run on the site under
[site OpenShift acceptance](#site-openshift-acceptance).

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
2. `EMBED_BASE_URL`/`LLM_BASE_URL` point through the real TLS test gateway. `scripts/ci/deploy_test_gateway.sh` owns the test
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
The release rehearsal uses [the release guide's generator](release-verification.md#synthetic-corpus).
No mock or test gateway belongs in production manifests or application images.

Access Kind privately with `kubectl -n "$NAMESPACE" port-forward svc/rag-agent
8080:8080` and open `http://localhost:8080/ui`. Kind supplies no OpenShift OAuth,
Route, Service CA or SCC evidence. Only remove the newly created disposable
cluster after retaining diagnostics and synthetic recovery evidence.

The release gate's real-model Kind lane uses the same original bundle, the two
current GPU models through the authenticated TLS gateway, and a separate
real-vector collection; it must pass alongside CI.

<a id="gap-fork"></a>
### 4.8 Air-gapped GitLab fork (ADR-0009)

The air-gapped GitLab is a downstream fork of this repository.
[ADR-0009](adr/0009-gap-fork.md) owns the decisions; this section lists what
exists on `main` and points at it.

- **Dev kit.** The dev workspace image reaches the site in a signed dev kit,
  separate from the release bundle. The connected side packs it with
  `scripts/devkit/pack.sh` and publishes its anchor with the release record
  ([release record](release-record.md#sign-off-and-handoff)). On site, follow
  [`docs/runbooks/devkit-windows.md`](runbooks/devkit-windows.md): it checks
  the kit against the anchor on Windows, pushes the image, creates the bastion
  and runs the kit's verifier (`scripts/devkit/install.sh`) there.
- **Workspace pods and isolation.** The bastion, dev pod and runner-job
  manifests are in [`deploy/workspace/`](../deploy/workspace/contract.json).
  The [isolation proof](deploy.md#workspace-isolation-proof) checks them, and
  its record goes in `gap/records/`.
- **Release import** ([ADR-0009 §4](adr/0009-gap-fork.md#4-release-import-procedure)).
  Run `scripts/gap/import_release.sh` from the fork clone. Its `--help` lists
  the inputs, and ADR §4 gives the stop and rerun rules and the steps after
  `IMPORT READY`.
- **Gap releases** ([ADR-0009 §5](adr/0009-gap-fork.md#5-gap-releases)). Not
  implemented (F6). Decided so far
  ([GH#1118 comment 6525955227](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6525955227)):
  an approved GitLab issue names the commit and the gap maintainer creates a
  protected tag on it. The release is built, packed and signed in the bastion,
  never on a GitLab runner. That session holds only the gap signing key and a
  read-only GitLab token, with no `oc` token, and runs the scripts of the
  installed signed release. Rehearsal and promotion run in a separate deploy
  session, without the signing key. The unprivileged image-build mechanism is
  still open question 4. Until F6 lands, production deploys only upstream
  signed releases.

---

## 5. Day-2 Operations & Maintenance

### 5.1 Health Check API

The agent exposes `/healthz` for the OpenShift readiness probe and
`/livez` for liveness (contract: `agent.md` §1 — `ok`/`degraded`/
`503 qdrant_unready`, upstream bodies stay server-side). `/healthz` is a
real readiness failure (HTTP 503) whenever the served generation is not
validated compatible — including `reembed_required`, `legacy`, a `pending`
migration, or unreadable metadata, or when `RERANK_ENABLED` is on and the
rerank endpoint(s) fail the cached probe (`rerank: false`); `empty` stays ready so the
deploy -> ingest bootstrap can complete. `/livez` is process-only.

```bash
curl -s http://rag-agent.mainframe-rag.svc:8080/healthz
```

```json
{
  "status": "ok",
  "qdrant": true,
  "embed": true,
  "representation": "compatible",
  "rerank": null
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

#### Operations CLI (`mainframe-rag-ops`)
A read-only client of `/healthz`, `/v1/search` and `/v1/answer` for operators
and scripts; contract, exit codes and secret handling live in
`agent.md` ([Operations CLI](agent.md#ops-cli)). Credentials come only from the
environment or a file, never an argument:

```bash
export MAINFRAME_RAG_URL=https://rag-agent.example.com
export MAINFRAME_RAG_API_KEY="$(cat /run/secrets/rag-key)"   # optional
mainframe-rag-ops health --ca-file /etc/gateway-ca/ca-bundle.crt
mainframe-rag-ops search "IEA500I" --product z/OS --version 3.1 --format json
mainframe-rag-ops answer "How do I resolve IEA500I?" --stream --format json
echo $?   # 0 ok, 3 answer not verified, 4 unauthorized, 5 unavailable, 6 bad response
```

Without an installed console script use `python -m mainframe_rag.ops ...`
(`PYTHONPATH=src`).

### 5.3 Common Troubleshooting Scenarios

| Issue | Symptom | Remediation |
|---|---|---|
| **Dimension Mismatch** | Ingest/Search fails with 400 dimension mismatch | Verify `DENSE_DIM` in `airgap.env` matches `EMBED_MODEL` on vLLM. |
| **Qdrant Unready** | `/healthz` returns `503 qdrant_unready` | Check Qdrant pod logs (`oc logs qdrant-0`); check block PVC mount. |
| **NFS Storage Refusal** | `sh scripts/tools/run-task.sh airgap:deploy` fails validation | Set `STORAGE_CLASS` to an RWO block driver (Ceph RBD / SAN / EBS). |
| **Hash Mode in Prod** | Scripts fail closed with `EMBED_MODE=hash forbidden` | Remove `EMBED_MODE` from production environment; provide valid vLLM endpoint. |
| **Registry Certificate Error** | `skopeo copy` fails with `x509: certificate signed by unknown authority` | Install the registry CA in the loader's trust store and configure node trust separately. Verify the certificate hostname; do not disable TLS verification for release or production acceptance. The Kind HTTP registry is a development-only exception. |
| **OpenShift SCC Rejection** | Pod `qdrant-0` fails with `unable to validate against any security context constraint` | Block promotion, capture admission evidence, and fix project-range compatibility in the owning production configuration. Verify PVC writes under `restricted-v2`; never grant `anyuid`. See [site OpenShift acceptance](#site-openshift-acceptance). |
| **PVC Multi-Attach Error** | `job/ingest` fails with `Multi-Attach error for volume` on corpus PVC | Ensure any previous writer pod has released the PVC, or use a ReadOnlyMany volume. |
| **Qdrant P2P CrashLoop** | Pod `qdrant-0` fails with `No such file or directory` looking for `cert.pem` | Ensure `config.cluster.p2p.enable_tls: false` in `values.yaml` (gossip is plaintext on CNI without `./tls/cert.pem`). |
| **K8s Manifest Integer/Boolean Error** | `Invalid value: "string", expected integer/boolean` | Ensure numeric/boolean env vars (`DENSE_DIM`, `INGEST_WORKERS`, `RERANK_ENABLED`) are explicitly quoted in rendered manifests. |
| **Degraded `/healthz` Smoke Failure** | `sh scripts/tools/run-task.sh airgap:smoke` exits 1 with `FAIL: /healthz probe did not report ok` | Pre-flight probe failed closed (non-`ok` body or non-200). Check Qdrant and vLLM connectivity, then the `representation` field: `reembed_required`/`pending` needs `--reingest`, `legacy` needs a new collection (unsupported stored data); `unknown` means the metadata store is unreachable. Requests refuse with `503 representation_unavailable` while this holds. |
| **Qdrant 401 After Reinstall** | `/v1/search` fails with `401 Invalid API key or JWT` after Qdrant was reinstalled or re-`helm upgrade`d | The chart regenerates the `<release>-apikey` secret on reinstall while running agent pods keep the old key in env. Roll the agent: `kubectl -n <ns> rollout restart deploy/rag-agent` and wait for rollout before smoking again. |
| **Stale dist/ MANIFEST** | `airgap:validate` / `airgap:deploy` / `airgap:ingest` / `airgap:dryrun` fail with `IMAGE_SHA=<sha> does not match packed MANIFEST sha` | `dist/` is gitignored build output that persists across checkouts — the MANIFEST inside is from an older pack (only `pack` regenerates it; the other steps just read it). Repack at the current HEAD, or clear the stale `dist/` before re-running. |
| **Checkout/manifest mismatch** | `airgap:validate` / `airgap:deploy` / `airgap:ingest` / `airgap:load` fail with `executing checkout HEAD does not match the packed MANIFEST sha` | The scripts run from a different checkout than the packed bundle (e.g. old code with a new `dist/`, or `IMAGE_SHA` overridden to the new SHA while old code executes). Run from the approved bundle checkout — overriding `IMAGE_SHA` alone never changes which code executes. Skipped only in `AIRGAP_DRYRUN=1` previews. |
| **Unverified checkout** | `airgap:validate` / `airgap:load` / `airgap:deploy` / `airgap:ingest` / `airgap:retire-legacy` fail with `checkout is not a verified signed release (no packed MANIFEST)` or `no release trust anchor is configured` | Outside dry-run these run only from the checkout `bootstrap.sh` made from the signed bundle, with `SNEAKERNET_TRUSTED_PUB` set to the release key obtained out of band ([deploy §4](deploy.md#4-signing-and-provenance), #1118 F1). Rerun `bootstrap.sh` from the signed bundle and export the anchor. A connected developer or CI lane may instead set `AIRGAP_UNVERIFIED_CHECKOUT=1` with `CI_REHEARSAL=1` in the process environment; never in the air gap. |
| **Stale airgap.env IMAGE_SHA** | `airgap:pack` / `airgap:load` fail with `IMAGE_SHA=<sha> is not the checked-out commit` right after checking out a new SHA | `airgap.env` is gitignored local state from a previous rehearsal — its `IMAGE_SHA` no longer matches HEAD. Explicit env beats the file (`IMAGE_SHA=$(git rev-parse HEAD) sh scripts/tools/run-task.sh airgap:pack`), or update the file. |
| **Stale dist/ tarballs fill disk** | `pack`/`load` fail with no-space errors after several rehearsals | Every pack leaves a ~1.5 GB `qdrant-pdf-rag-<sha>.tar` in gitignored `dist/`; only the MANIFEST-pinned one is live. Delete superseded tarballs (keep the `.tar.sha256` of the live one) — pack never prunes. |
| **Kind ErrImagePull on localhost:5000** | mock/corpus-gen pods fail with `dial tcp [::1]:5000: connect: connection refused` | The Kind `containerdConfigPatches` in §4.7 must mirror **both** `localhost:5000` and `airgap-registry:5000` to the registry container — one key per naming family used by the manifests. Recreate the cluster with the documented config (containerd mirrors are set at creation). |
| **Console Route deploy fail-close** | `sh scripts/tools/run-task.sh airgap:deploy` dies on the oauth-proxy `sha256:PENDING` pin or a missing `rag-agent-oauth-cookie` Secret | Record the digest in `images.txt` + repack and create the cookie Secret (§4.4.2); or deploy with `AGENT_ROUTE=false` (ClusterIP-only, `/ui` still served in-cluster). |

### 5.4 Qdrant 6/3/2 topology operations (issue #360)

Production topology is 3 peers on distinct workers, 6 shards / RF3 / W2 on
both corpus and control collections (decision and failure contract:
`deploy.md` §collection-policy). One unavailable peer at a time is the
supported failure; two surviving copies are degraded, not healthy. The
procedures below are the authorized operator paths — automation beyond
them (replica-repair tooling, node-addressed restore) remains open under
#360 and must not be improvised here.

#### Safe inspection (read-only, any time)

Run the placement verifier from the cluster network (agent/ingest pod or
bastion with peer access); it mutates nothing:

```bash
# Strict production qualification: 6/3/2 across all three direct peers.
QDRANT_URL=http://qdrant:6333 QDRANT_SHARD_NUMBER=6 \
QDRANT_REPLICATION_FACTOR=3 QDRANT_WRITE_CONSISTENCY_FACTOR=2 \
  python3 scripts/verify_placement.py --production \
    --peer-url http://qdrant-0.qdrant-headless:6333 \
    --peer-url http://qdrant-1.qdrant-headless:6333 \
    --peer-url http://qdrant-2.qdrant-headless:6333

# Pre-publication staging check: point QDRANT_COLLECTION at the staging
# physical name (no alias carries that name, so the verifier checks the
# staging corpus/control pair instead of live). Refuse cutover unless this
# reports healthy; the ingest cutover gate additionally refuses unknown or
# mismatched configured policy with the alias untouched.
```

`--allow-degraded` (never with `--production`) is for operational
continuation during a positively established single-member loss only.
Pod counts, `replication_factor` in config, or one passing search are
never qualification. A degraded-but-readable generation may keep serving
verified immutable reads; never use strict qualification as application
liveness and never publish a new generation until every shard is ACTIVE
on three distinct peers again.

#### Mutation ownership

- **Publication** is the one authorized writer path (`ingest.md`
  publication contract, single target-held lock, alias swap only after
  full verification). Retired generations and safety snapshots are kept;
  rollback is alias re-pointing by the operator — never automatic
  deletion, retirement, or garbage collection (#391 owns publication
  integrity).
- **Existing-collection topology changes** are snapshot-gated and explicit:
  same-shard-count replication repair establishes actual copies and waits
  for ACTIVE state; a shard-count change rebuilds a distinct 6/3/2
  generation with preserved source identities and cuts over by alias.
  Never PATCH shard count in place, never delete/recreate live, never
  lower RF/W to hide a mismatch.
- **Rolling maintenance** evicts/restarts one peer at a time
  (`maxUnavailable: 1` limits voluntary disruption only) and **waits for
  full shard recovery before the next eviction**. A PDB does not see
  replica catch-up and does not cover involuntary failures. ACTIVE
  placement alone is not read convergence — observed 2026-09-21: a rejoined
  peer reports ACTIVE replicas before finishing data sync — so verify exact
  reads (not only the verifier's placement verdict) before the next
  disruption or any cutover.

#### Abort, retry, and recovery

- An interrupted clone, transfer, or build is resumed or retried, never
  repaired by deleting live data. A superseded physical plus its safety
  snapshot survive every swap until the operator removes them after
  acceptance.
- **Peer replacement is not stop/wipe/start.** Observed 2026-09-21 against
  the pinned `v1.19.0-unprivileged` image: a fresh container restarted at
  the same URI while the dead peer still holds shards is refused
  (`peer URI ... already used by peer ... which still has shards, remove
  its shards first or use a different URI`) and crash-loops. Replacement
  is an explicit multi-step procedure — inspect, remove the dead peer's
  shards/membership through the supported API, join, then verify placement
  plus exact corpus/control data — whose acceptance is still open under
  #360. Until then, the supported recovery for a lost peer with
  unrecoverable storage is a tested fresh rebuild from protected originals
  with explicitly accepted downtime (POC path, #447); no untested
  distributed-restore or uninterrupted-HA promise is implied.
- Update publications over a distributed (multi-shard or replicated) live
  generation prepare staging by an exact points-API copy, never a snapshot,
  and a pre-publication layout (a physical collection squatting on the alias
  name) is refused, never migrated (`deploy.md#distributed-recovery`). The site qualification steps and the
  sizing/HA-scope decision are in `deploy.md#site-qualification` and
  `deploy.md#sizing-note`.
- Loss of publisher scratch state is not repaired by Qdrant replication:
  preserve progress/authorization/build state for deterministic resume
  (`ingest.md`), and treat a missing sidecar as explicit operator recovery,
  never as permission to publish unknown staging.

<a id="upgrade-and-recovery-runbook-issue-272"></a>
#### Upgrade and recovery runbook (issue #272)

Scope: the selected POC/production envelope of #447 — repeat a candidate,
refuse wrong/missing artifacts without mutation, keep a compatible prior
software/configuration set, and either restore data or deliberately rebuild
from protected originals. Live migration/retirement and uninterrupted-HA
restore are not promised here (#360, #391). `$KC` is `oc`/`kubectl`,
`$NS` the owned namespace; run from the checkout of the bundle being operated.
Qdrant snapshots are **node-local**: a collection snapshot taken on one peer
holds only the shards that peer stores. Admin calls use the writer key from the
`<release>-apikey` Secret (`api-key`), never the serving key; do not print it.

**0. Preconditions (read-only).**

```sh
sh scripts/tools/run-task.sh airgap:validate          # access, Secrets keys, storage class
python3 scripts/verify_placement.py --production \
  --peer-url http://qdrant-0.qdrant-headless:6333 \
  --peer-url http://qdrant-1.qdrant-headless:6333 \
  --peer-url http://qdrant-2.qdrant-headless:6333   # run from the cluster network
```

Stop on any not-ACTIVE shard. Check snapshot headroom first
(`deploy.md` capacity arithmetic: `retained * S` must fit the 500Gi claim) and
that no ingest Job is running (`$KC -n "$NS" get job ingest`): the writer is
quiescent before any snapshot.

**1. Snapshot every peer and make a verified off-cluster copy.** Per physical
data collection and its `<physical>__completions` pair (alias mapping recorded
first: `GET /aliases`), on each peer `qdrant-N`:

```sh
KEY=$($KC -n "$NS" get secret qdrant-apikey -o jsonpath='{.data.api-key}' | base64 -d)
for peer in 0 1 2; do
  $KC -n "$NS" port-forward "pod/qdrant-$peer" 6333:6333 >/dev/null & pf=$!
  sleep 3
  name=$(curl -fsS -X POST -H "api-key: $KEY" http://127.0.0.1:6333/collections/<physical>/snapshots \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')
  curl -fsS -H "api-key: $KEY" -o "<physical>-peer$peer.snapshot" \
    "http://127.0.0.1:6333/collections/<physical>/snapshots/$name"
  kill "$pf"
done
sha256sum <physical>-peer*.snapshot > SNAPSHOTS.sha256   # keep with the bundle SHA and aliases
```

The off-cluster copy is the point of the exercise: snapshot PVCs share the
cluster's failure domain. The artifacts contain manual text: store them like the
corpus, never in git. Unverified copies (`sha256sum -c SNAPSHOTS.sha256` fails)
are not backups.

**2. Isolated restore proof (never over the source).** Before relying on a
snapshot, restore one peer's data and `<physical>__completions` files into *new*
collection names on a scratch Qdrant, compare `points_count`, exact-query results,
sampled payloads and vectors, and the whole completions collection (every
completion record, the representation manifest and the publication record) with
the source, then delete only the scratch collections. A data-only restore is not
a recovery: without its completions pair the check must fail.
`scripts/ci/check_snapshot.py` is this check for the synthetic lifecycle lane
(`scripts/ci/check_lifecycle.sh`): it restores both snapshots as
`ci-restore-<uuid>` and `ci-restore-<uuid>__completions`, asserts equal stored
content, proves on the live service that a data-only restore is refused, and
after cleanup confirms the source pair and aliases are unchanged and that a
further query and snapshot on the source still succeed. It prints fixed messages
only, never stored text. It does not cover, and the site must still rehearse:
the off-cluster copy and its checksums, restore on a different cluster or Qdrant
peer, distributed multi-peer restore and peer replacement (#360), the filesystem
behavior of the real snapshot storage class, and the application serving the
restored pair (the lane exercises the live pair, not the scratch copy).
A source collection is never deleted or reset to "reingest" (the one exception
is the operator-confirmed retire step for the d53595e flat layout below, after
a verified copy): completion markers,
representation and inventory move with publication (`ingest.md`), so recovery is
by alias re-pointing or by the publication contract, not by deleting live data.

**3. Application upgrade (candidate bundle B over A).** In a fresh workspace for
B (`bootstrap.sh`, never advance A's workspace), then:

```sh
sh scripts/tools/run-task.sh airgap:validate
sh scripts/tools/run-task.sh airgap:pipeline          # or: sh scripts/airgap/pipeline.sh --skip-load
```

Deploy refuses before any cluster change when the chart or any registry image is
not the packed one (`deploy.md` image identity), then checks the running
`imageID`s. The agent's startup refuses an incompatible or pending existing
representation by design; that is a staged recovery (step 5), not a rerun.
Repeating the same candidate is supported and must leave identical digests.

**4. Rollback (compatible software/config only).** Run the previous approved
bundle A from its own workspace and its own `airgap.env`
(`airgap:deploy`, same namespace). Application rollback never rolls data back
and never edits PVCs (`deploy.md` lifecycle notes). Rollback is valid only if
the data representation A expects is the one the target holds; otherwise use
step 5. Never combine A's assets with B's checkout.

**5. Data recovery or rebuild (explicit, downtime recorded).**

- Alias-published generation intact: re-point the serving alias to the
  retained previous generation (`ingest.md` publication contract); verify with
  exact queries and the agent `/healthz`. Retired generations and safety
  snapshots stay until the operator accepts.
- Lost/corrupt storage on one peer: no improvised stop/wipe/start (see above).
  The supported path today is a fresh rebuild from protected originals with
  accepted downtime: provision clean claims, `airgap:pipeline` with
  `CORPUS_PVC`, then step 0 verifier and step 6 acceptance. Peer replacement and
  distributed snapshot restore stay open under #360.

**6. Acceptance after any of the above.**

```sh
python3 scripts/verify_placement.py --production --peer-url ...   # 6/3/2, three peers
sh scripts/tools/run-task.sh airgap:smoke
```

Then exact reads on every peer (not only the verifier verdict), the documented
user-experience checks, and record downtime, the bundle SHA, registry
digests (`==> registry image verified` lines) and snapshot checksums in the
approved venue.

**Qdrant version upgrade.** Follow
[qdrant-version-upgrade](../.agents/skills/qdrant-version-upgrade/SKILL.md):
storage compatibility holds for one minor version, so step one minor at a time
(latest patch of each), SDK first, snapshot first (step 1), at most one peer
unavailable at a time (RF3 permits a rolling upgrade; wait for full shard
recovery between peers), and confirm every peer reports the target version.
There is no supported downgrade of data written by a newer minor: rollback of
the Qdrant image alone is not a recovery plan; restore the pre-upgrade snapshot
into a clean cluster instead. The vendored chart/image pin changes only in a
dedicated pin-bump PR.

**PVC expansion.** Preconditions: the StorageClass sets
`allowVolumeExpansion: true` and the backing storage has the capacity. Patch
each claim (`$KC -n "$NS" patch pvc qdrant-storage-qdrant-N -p '{"spec":{"resources":{"requests":{"storage":"<new>"}}}}'`
and `qdrant-snapshots-qdrant-N` likewise), wait for `FileSystemResizePending` to clear
(a pod restart may be needed), then align `persistence.size` /
`snapshotPersistence.size` for the next upgrade. The StatefulSet
`volumeClaimTemplates` are immutable, so the template change needs
`$KC -n "$NS" delete statefulset qdrant --cascade=orphan` before the next
`helm upgrade` (pods and claims are kept). Claims are never shrunk.

**Not exercised by this repository.** Everything above is documented procedure
plus the hermetic checks named in it; no OpenShift or site run of the
snapshot, off-cluster copy, restore, rollback, Qdrant minor upgrade or PVC
expansion has been recorded for it. Each needs a live rehearsal in the
approved venue (site) before the corresponding operation is enabled;
this is tracked on #272.

#### Upgrade from a legacy flat collection (issue #1070)

Release d53595e stored the corpus as a plain collection `mainframe_manuals`
(plus `mainframe_manuals__completions`) with no alias. This release serves and
ingests only an alias-published, build-bound corpus, so that layout cannot be
upgraded in place (maintainer decision R1: a maintenance window with a full
re-embed). Rule (`scripts/airgap/legacy_layout.py check`, aliases read first;
Qdrant 1.19 reports an alias name as an existing collection):

| Stored state | Preflight |
|---|---|
| No Qdrant StatefulSet and no Qdrant Helm release record (fresh install) | pass |
| A Qdrant Helm release record without its StatefulSet | refuse `E_LAYOUT_UNVERIFIED` |
| No alias and no collection `mainframe_manuals` | pass (`empty`) |
| Alias `mainframe_manuals` -> T, and `T__completions` holds a build-bound publication record | pass (`ok`) |
| No alias, but a collection `mainframe_manuals` | refuse `E_LEGACY_COLLECTION` |
| Alias -> T without a build-bound publication record | refuse `E_UNBOUND_PUBLICATION` |
| Alias -> `mainframe_manuals_d53595e_keep` (after a rollback) | refuse `E_ROLLED_BACK` |
| StatefulSet present but no running `rag-agent` pod can read the store, or any read fails | refuse `E_LAYOUT_UNVERIFIED` |

`airgap:validate` (with cluster access) and `airgap:deploy` apply it before
any change (the namespace, Route deletes and both Helm upgrades come after
it), so `airgap:pipeline` stops at stage 1. The check runs inside the running
agent pod (the Qdrant image has no shell client or Python) with that pod's
read-only key, and prints only the state code.

**Two stages, never one command.** `retire_legacy.sh` refuses without
`--stage copy` or `--stage retire`, so nothing is deleted by accident.
`sh scripts/tools/run-task.sh airgap:retire-legacy -- <flags>` runs the same script.
It uses the Qdrant writer key, so outside dry-run it runs only from the
verified signed release, like deploy
([deploy §4](deploy.md#4-signing-and-provenance)).

| Stage | When | What it changes | Search |
|---|---|---|---|
| `--stage copy` | before the window | creates `mainframe_manuals_d53595e_keep(__completions)`: points-API copies, exact id sets verified, the d53595e manifest re-keyed so the d53595e agent serves the copy | keeps working (d53595e still serves `mainframe_manuals`) |
| `--stage retire` | starts the window | takes **this run's** snapshot of both collections on every peer, binds each to the collection's point count and id digest (read before and after the snapshot), records the names in `mainframe_manuals_d53595e_retire_record`, then deletes both collections | down from the delete until go-live |

A snapshot already listed for the collection (an older backup, or a file left
by an earlier collection of the same name, which Qdrant lists again) is never
used. A re-run reuses only a snapshot the record names whose binding and
listed checksum and size still match. Limit: the binding proves which id set
was in the collection while its snapshot was taken, not the snapshot's bytes;
restoring into a scratch Qdrant (recovery step 2 above) is the content proof.
The kept copy, which is verified point by point, is the rollback data. The
snapshots are secondary.

**Outage.** Search is down from the delete in `--stage retire` until
go-live. With `N` documents, `G` the site gateway embedding speed relative to
our local GPU endpoint (measure a 20-document slice first) and 2 ingest workers:

```text
outage ≈ retire stage (snapshots: minutes) + 0.5 h (deploy, smoke, checks)
         + 19 s × N × 1.5 × G (full ingest)
         e.g. N = 452: ≈ 0.5 h + 3.6 h × G;  N = 1,000: ≈ 0.5 h + 8 h × G
copy stage (before the window, not part of the outage): P = 520 × N points;
         locally ~1.2k points/s at 1024 dims on one node; measure on site
```

Plan a one-day window (`INGEST_TIMEOUT=86400`); the estimates and the
per-pod disk/RAM arithmetic (two generations at most) are in the site
re-ingest plan.

**Procedure** (single operator; ingest frozen from step 3 on; run from the
new bundle's workspace; d53595e stays deployed until step 6):

1. **Announce** the window and the expected outage above.
2. **Preflight:** `sh scripts/tools/run-task.sh airgap:validate`. It must stop
   with `E_LEGACY_COLLECTION`; any other refusal is fixed first.
3. **Copy (before the window, search keeps working):**
   `sh scripts/airgap/retire_legacy.sh --stage copy --confirm mainframe_manuals`.
   It checks each Qdrant peer's volumes first (`df` in the pod: data volume
   free ≥ its used size). If the usage cannot be read it refuses with
   `E_RETIRE_SPACE_UNKNOWN`: check the claims yourself and add
   `--space-verified`. Record the `KEPT` lines. Re-run after any failure; it
   resumes. The preflight still reports `E_LEGACY_COLLECTION`.
4. **Retire (the window starts):**
   `sh scripts/airgap/retire_legacy.sh --stage retire --confirm mainframe_manuals`.
   Space gate: data free ≥ used and snapshot free ≥ half the used size, since
   the copy is now counted in used. It refuses without the verified copy
   (`E_RETIRE_NOT_COPIED`) and never deletes an alias name. Snapshots go to
   `QDRANT_PEER_URLS`, else the StatefulSet's headless names. Re-run the same
   command after any failure or crash. When everything is in place it prints
   `STATE retired` and the `RECORDED snapshot` lines, and changes nothing.
   With nothing to retire and no kept copy it refuses with
   `E_RETIRE_NOTHING_KEPT`; `--no-keep-ok` accepts that no rollback data
   exists. The writer key travels on stdin only and is never printed.
5. **Deploy:** `sh scripts/tools/run-task.sh airgap:deploy` (the preflight now
   reports `empty`).
6. **Full ingest** under the current rules: `sh scripts/tools/run-task.sh
   airgap:ingest` with the site values (section 4.5). It builds a new
   generation and publishes it by the alias `mainframe_manuals`. If the wait
   times out, the Job keeps running: watch it, do not start another.
7. **Verify:** the Job log shows `files_failed 0` and the publish line; then
   the placement verifier (section 5.4), `airgap:smoke`, the site's
   cited-answer questions, and `/healthz` `representation` ready. The
   preflight now reports `ok`.
8. **Go-live:** announce the end of the window. Keep the kept copy, the run
   record and the snapshots until the go-live checks pass and at least one
   working week of use. Then remove them with the writer key.

**Rollback to d53595e** (before the kept copy is removed). It does not
depend on any agent: the new one may be crashlooping, and the d53595e agent
crashloops on the new generation (`reembed_required`) until the alias moves.
`<qdrant>` is the chart's resource name: the release name when it contains
`qdrant` (the default `qdrant`), else `<release>-qdrant`.

1. Scale the new agent down, or keep it down:
   `$KC -n "$NS" scale deploy/rag-agent --replicas=0`.
2. Port-forward the Qdrant Service in a second terminal:
   `$KC -n "$NS" port-forward svc/<qdrant> 16333:6333`.
3. Repoint the alias:
   `sh scripts/airgap/retire_legacy.sh --rollback --confirm mainframe_manuals --qdrant-url http://127.0.0.1:16333`.
   This is one atomic alias call, `mainframe_manuals ->
   mainframe_manuals_d53595e_keep`. The new generation stays. Only a loopback
   URL is accepted, and the writer key goes on stdin. Re-running it is a
   no-op. Without `--qdrant-url` it runs through a running agent pod.
4. Redeploy d53595e from its own workspace and bundle (`airgap:deploy`);
   never mix it with this checkout. **Unqualified: d53595e's chart pins
   Qdrant 1.19.0, and this release ran 1.19.2 on the same volumes. That is a
   server downgrade on data a newer version wrote, and the release rehearsal
   must test it before the site relies on it.** Freeze ingest: d53595e
   ingest on an alias-published layout is unverified.
5. Verify: d53595e `airgap:smoke` and check-live, and the point count of
   `mainframe_manuals_d53595e_keep` equal to the recorded `KEPT` line.
6. Secondary path (node-local, not qualified for 6/3/2): delete the alias
   `mainframe_manuals`. Then, on the peer the run record names,
   `PUT /collections/mainframe_manuals/snapshots/recover` with
   `{"location": "file:///qdrant/snapshots/mainframe_manuals/<RECORDED SNAPSHOT>", "priority": "snapshot"}`
   and the writer key. Do the same for `__completions`, then verify the counts.
7. To upgrade again later, start at step 2 of the procedure. The preflight
   refuses with `E_ROLLED_BACK`, and `--stage retire` removes the alias
   (the kept copy stays) before the deploy.

Hermetic proof is in `tests/test_airgap_retire_legacy_sh.py`:
- both stages and a crash at each point;
- stale, unlisted, unbound and recorded snapshots;
- rollback through a port-forward with no agent pod;
- no key in any output.

The preflight cases are in `tests/test_airgap_deploy_sh.py`,
`tests/test_airgap_validate_sh.py` and `tests/test_airgap_pipeline_sh.py`.
Both stages, the rollback and the snapshot recovery ran against a throwaway
single-node Qdrant. No OpenShift or multi-peer run exists.

#### Ownership and linkage

Qdrant topology/placement acceptance stays under #360. Publication and
data integrity belong to #391, operational recovery to #272, measured
capacity to #374, POC scope to #447, cross-environment delivery to #446.
Site qualification (peer→pod→worker layout, failure domains, storage
headroom, measured failover/resync/restore) is recorded in the approved
venue with private details kept out of git; three loopback containers
prove distributed software behavior only.
| **`/ui` returns 404** | Console request returns the stable `404 not_found` envelope | `UI_ENABLED` is unset/false for the agent process. The production chart sets it true; for local runs use `UI_ENABLED=true sh scripts/tools/run-task.sh local:agent` or `sh scripts/tools/run-task.sh local:stack`. |
