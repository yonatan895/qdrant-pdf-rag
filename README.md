# Mainframe RAG

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/yonatan895/qdrant-pdf-rag)

Citation-first expert mainframe agent: hybrid retrieval over ~100 GB of IBM-style manuals (IBM, Broadcom/CA, BMC, Precisely) on **air-gapped OpenShift**, answering operational questions with exact citations — document number, title, heading path, printed page label — plus optional JCL/REXX/operator steps from a reasoning model.

Models (reasoning, dense embed, reranker) are served by the **platform team's internal vLLM / LiteLLM gateway** on a separate cluster — this repo never installs model servers. See [Model Gateway](#model-gateway-platform-team) below.

- **Design & Architecture:** [docs/architecture.md](docs/architecture.md) (source of truth)
- **Installation & Operations Guide:** [docs/install_and_ops.md](docs/install_and_ops.md) (step-by-step local & air-gap runbook)

---

## What This Repo Contains

| Path | What |
|---|---|
| `charts/qdrant-*.tgz` | Vendored Qdrant Helm chart (Apache-2.0); never `helm repo add` in the air-gap |
| `overlays/openshift/values.yaml` | Qdrant OpenShift values: 3-replica StatefulSet, unprivileged, RWO block, ClusterIP only |
| `deploy/` | 2-replica agent Deployment, one-shot ingest Job, opt-in Jaeger; `AGENT_ROUTE=true` for an edge Route |
| `oc-mirror/` | `ImageSetConfiguration` for disconnected mirroring |
| `src/mainframe_rag/ingest/` | PDF walk, IBM-style parse, chrome strip, chunk, classify, embed, Qdrant IO |
| `src/mainframe_rag/retrieve/` | Hybrid search (dense + BM25 prefetch, batched query, weighted RRF), query-class screen, optional cross-encoder rerank (`RERANK_ENDPOINT_ORDER`), diversification, filters |
| `src/mainframe_rag/agent/` | Async FastAPI `/healthz`, `/v1/search`, `/v1/answer` (reasoning model, optional SSE streaming), opt-in `GET /metrics` |
| `src/mainframe_rag/mcp/` | Read-only Zowe live-state bridge (default off; mock backend for sim) |
| `src/mainframe_rag/serve/` | Local vLLM VRAM budget profiles (`LOCAL_RT_8GB`, …) + `resolve` CLI |
| `scripts/` | Benchmark suite, golden set eval, report renderer, query demo, gateway readiness probe (`probe_gateway.py`), air-gap ops |
| `images/` | UBI Containerfiles (non-root, wheelhouse + BM25 weights baked in) |
| `tests/` | Unit, hygiene, regression gates, and Docker simulation integration tests |

---

## Model Gateway (Platform Team)

Reasoning, dense embed, and rerank models run on the platform team's cluster behind a LiteLLM gateway; Qdrant + the agent run in this namespace. Wire all three legs in `airgap.env` (see [docs/install_and_ops.md](docs/install_and_ops.md) §4.3):

| Role | URL key | Auth |
|---|---|---|
| Reasoning (`/v1/answer`) | `LLM_BASE_URL` + `LLM_MODEL_REASONING` | `llm-api-key` |
| Embed (`/v1/search`, ingest) | `EMBED_BASE_URL` (+ `EMBED_MODEL` + `DENSE_DIM`) | `embed-api-key` |
| Rerank (default off) | `RERANK_BASE_URL` + `RERANK_MODEL` + `RERANK_ENDPOINT_ORDER` | `rerank-api-key` |

Keys live only in one operator-created Secret (`GATEWAY_API_KEY_SECRET`, rendered via `secretKeyRef` — never plaintext in `airgap.env`; unset = keyless). Before ingesting, prove every leg from inside the cluster and take the recommended leg order:

```bash
kubectl -n mainframe-rag exec deploy/rag-agent -- python3 /app/scripts/probe_gateway.py
# recommendation: RERANK_ENDPOINT_ORDER=rerank_first  (gateways; score_first for raw vLLM)
```

## Live State (Optional, Default Off)

- **Splunk (system of record):** caller-supplied context — pass `splunk_context` with `/v1/answer`; the agent never crawls Splunk itself.
- **Zowe MCP (agent-fetched):** read-only datasets / JES spool / USS / job status via a sidecar bridge (`zowe_mcp_enabled=false` default; mock backend in sim). See [docs/architecture.md](docs/architecture.md).

---

## Legal & Repository Rules (Hard)

- **No manuals in this repository.** No PDFs, HTML dumps, Redbooks, or "cleaned" markdown of IBM / Broadcom / BMC / Precisely content. The corpus stays on enterprise storage.
- **Ingest inside the enterprise only.** Never ingest real manuals from a public runner or connected cluster; ingest runs after Qdrant is healthy in the air-gap.
- **Never commit:** Qdrant snapshots/embeddings/chunk dumps, production JCL, credentials, TLS material, real storage class names. Placeholders only.
- **Tests use synthetic content only;** product names appear nominatively.

---

## Makefile Targets Reference

| Target Category | Command | Description |
|---|---|---|
| **Setup & Quality** | `make venv` | Create `.venv` and install locked Python 3.14 dependencies |
| | `make bm25-weights` | Download and cache FastEmbed BM25 model weights |
| | `make check` | Run `ruff check`, `mypy src`, and unit test suite |
| | `make test` | Run unit test suite (`pytest tests -v`) |
| | `make lint` | Run Ruff linter |
| | `make typecheck` | Run Mypy static type checker |
| **Simulation** | `make sim` | Run full integration simulation tier (ephemeral Docker Qdrant + mock LLM) |
| | `make sim-qdrant` | Start a local Docker Qdrant container on port 6333 |
| | `make sim-clean` | Stop and remove the local Docker Qdrant container |
| | `make loadtest-mock` | Load tier: same composition under concurrency, absolute contracts (fail-closed, no skips) |
| **Accuracy & Eval** | `make eval` | Score golden set queries (`evals/golden.jsonl`) against the mode-keyed baseline (`evals/baseline.json` in hash mode, `evals/baseline-vllm.json` in vllm mode) |
| | `make gate-l1` | L1 retrieval gate on an ephemeral Qdrant simulator (automated CI check) |
| | `make eval-answers` | Answer-tier grounding eval (`/v1/answer` must cite, abstain entries must not answer) — live GPU stack |
| | `make eval-paraphrase` | Paraphrase retrieval instrument (semantic queries without near-verbatim echo) — dedicated collection |
| | `make eval-holdout` | Score the frozen holdout (`evals/holdout.jsonl`, sha-pinned) — release candidates only |
| | `make eval-baseline` | Re-record committed retrieval accuracy baseline (dedicated PR) |
| | `make eval-draft` | Helper to draft golden-set candidate queries from collection payload |
| | `make verify-golden` | Mechanically verify golden expectations against the live collection (gates the corpus) |
| | `make eval-report` | Print terminal retrieval evaluation report |
| | `make eval-html` | Generate self-contained offline HTML evaluation dashboard (`bundles/eval-report.html`) |
| | `make eval-compare` | Compare evaluation runs with classification shifts and regression checks |
| | `make harness-gate` / `make harness-l2` / `make harness-l3` | Layered harness tiers L1/L2/L3 — RC-only, live GPU stack |
| | `make harness-baseline` / `make harness-l3-baseline` | Re-record harness baselines (dedicated PR) |
| **Benchmarks** | `make bench` | Benchmark ingest rate, peak RSS, Qdrant RAM/disk, and latency vs baseline |
| | `make bench-baseline` | Re-record committed performance baseline (dedicated PR) |
| | `make bench-report` | Print terminal benchmark performance report |
| | `make bench-html` | Generate self-contained offline HTML benchmark dashboard (`bundles/bench-report.html`) |
| | `make bench-compare` | Compare benchmark performance against baseline |
| | `make loadtest` | Run concurrent load test against agent search endpoint |
| **Interactive Demo** | `make query-demo` | Launch interactive terminal REPL (`rag-search> `) for inspecting queries |
| | `QUERY="..." make query-demo` | Inspect a single query with classification, latency, rank, citations & text |
| | `make ask` | Launch interactive reasoning Q&A assistant (`rag-answer> `) with LLM & citations |
| | `QUERY="..." make ask` | Ask a single question and get grounded reasoning answer with citations |
| **Local vLLM & GPU** | `make local-vllm` | Run local vLLM reasoning server on GPU (port 8000, Gemma-4, Budget `GPU_MEM=0.64`) |
| | `make local-vllm-embed` | Run local vLLM dense embedding server on GPU (port 8001, Qwen3-Embedding-0.6B, `GPU_MEM=0.33`, `--runner pooling --convert embed --enforce-eager`) |
| | `make local-vllm-rerank` | Run local vLLM reranker server on GPU (port 8002, `BAAI/bge-reranker-v2-m3`) |
| | `make run-agent` | Start the agent with `LLM_STREAM=true` (reasoning SSE streaming for TTFT) on port 8080 |
| | `make test-vllm-e2e` | Run automated end-to-end suite against local vLLM & Qdrant with grounding validation |
| **Cluster recipe** | `make pull-chart` / `make helm-template` / `make helm-lint` | Fetch / render / lint the vendored Qdrant chart against OpenShift values |
| | `make wheelhouse` / `make bm25-weights` / `make build-images` | Build offline wheelhouse, cache BM25 weights, build UBI images (connected host) |
| | `make e2e-demo-pdfs` | Generate synthetic demo PDFs into `output/demo-pdfs` |
| | `make clean` | Remove `.venv`, caches, `bundles/`, and `output/` |
| **Packaging & Standard Deployment** | `make airgap-pack` | Build sneakernet package (`dist/qdrant-pdf-rag-<sha>.tar`) on connected host |
| | `make airgap-validate` | Pre-flight validation of tools, env, storage class, and OpenShift SCC |
| | `make airgap-load` | Load image archives and push to `${INTERNAL_REGISTRY}` (in air-gap or local test registry) |
| | `make airgap-deploy` | Deploy Qdrant cluster, Jaeger v2 tracing, and Agent via standard Helm + Kustomize |
| | `make airgap-ingest` | Launch one-shot ingest Job against `CORPUS_PVC=<pvc>` |
| | `make airgap-smoke` | Smoke test in-cluster search endpoint with fail-closed `/healthz` probe |
| | `make airgap-pipeline` | Single master orchestrator running validate -> load -> deploy -> ingest -> smoke |
| | `make airgap-dryrun` | Pre-flight dry-run proving manifest rendering and fail-closed rules without cluster |

---

## Quickstart (Connected Host)

```bash
# 1. Bootstrap environment
make venv
make bm25-weights

# 2. Run quality checks
make check

# 3. Run integration simulation
make sim

# 4. Ingest a sample corpus in development mode (hash embedder, scratch collection)
make sim-qdrant   # local Qdrant on 127.0.0.1:6333 (or reuse QDRANT_SIM_URL)
QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=dev-corpus \
  EMBED_MODE=hash ALLOW_HASH_MODE=true \
  .venv/bin/python -m mainframe_rag.ingest.run_ingest \
  --src <dir-with-pdfs> --progress /tmp/dev-corpus/inventory.jsonl --workers 2
```

---

## Standard Deployment Architecture (Air-Gap & Local Cluster)

The hardened 5-stage deployment pipeline (`airgap-pack` -> `airgap-load` -> `airgap-deploy` -> `airgap-ingest` -> `airgap-smoke`) is the **canonical deployment standard across the entire project**. Both production air-gapped OpenShift and local testing environments adhere to this pipeline (using the same scripts, Helm chart, and Kustomize overlays, with adapted sizing and SCC for local test clusters).

**The air-gap never builds images.** Connected `main` is the only image factory.

### 1. Production Air-Gapped OpenShift Workflow

1. **Connected Host (or CI Release Download):**
   ```bash
   git checkout <main-sha>  # Full 40-character Git SHA (or download sneakernet-bundle from GitHub Actions)
   make airgap-pack         # -> dist/qdrant-pdf-rag-<sha>.tar + .sha256 + PACKING_RECORD.txt
   ```

2. **Sneakernet Transfer & Automated Bootstrap:**
   ```bash
   sha256sum -c qdrant-pdf-rag-<sha>.tar.sha256
   tar -xf qdrant-pdf-rag-<sha>.tar
   sh bootstrap.sh          # Verifies checksums, clones repo, populates dist/, sets up airgap.env
   cd qdrant-pdf-rag
   ```

3. **Air-Gapped Bastion:**
   ```bash
   # Configure environment & pre-flight validation:
   cp airgap.env.example airgap.env   # registry, namespace, storage class, model endpoints + gateway keys
   make airgap-validate               # tools, storage class (refusing NFS), OpenShift SCC, key Secret

   # Option A: Run complete automated pipeline:
   CORPUS_PVC=<pvc> make airgap-pipeline

   # Option B: Or execute step-by-step:
   make airgap-load                   # Push 4 image archives to internal registry
   make airgap-deploy                 # Deploy Qdrant StatefulSet + Agent (opt-in Jaeger with tracing)
   # Prove the gateway from inside the cluster, apply its leg-order recommendation:
   kubectl -n mainframe-rag exec deploy/rag-agent -- python3 /app/scripts/probe_gateway.py
   make airgap-ingest CORPUS_PVC=<pvc># Ingest corpus from storage PVC
   make airgap-smoke                  # Verify search endpoint (/healthz pre-flight check)
   ```

### 2. Local Cluster Testing Standard (Kind + Local Registry)

To test the deployment scripts and Kubernetes manifests locally with adapted sizing (1-replica Kind with mock/local vLLM instead of 3-replica OpenShift `restricted-v2`):

1. **Bootstrap Kind & Local Registry:**
   ```bash
   docker run -d --restart=always -p 5000:5000 --name airgap-registry registry:2
   kind create cluster --name airgap
   docker network connect "kind" airgap-registry || true
   ```

2. **Configure Environment:**
   Copy `airgap.env.example` to `airgap.env`, pointing `INTERNAL_REGISTRY=airgap-registry:5000`, `STORAGE_CLASS=standard`, `QDRANT_EXTRA_VALUES=scratch/qdrant-local.yaml` (1 replica override), and your vLLM/mock endpoint.

3. **Execute 5-Stage Pipeline:**
   ```bash
   make airgap-pack
   INTERNAL_REGISTRY=localhost:5000 INSECURE_REGISTRY=true make airgap-load
   make airgap-deploy
   make airgap-ingest CORPUS_PVC=<pvc>
   make airgap-smoke
   ```

See **[docs/install_and_ops.md](docs/install_and_ops.md#47-local-cluster-testing-standard-kind--local-registry)** for the complete Kind setup, registry configuration, and mock vLLM setup.

---

## Endpoints

The agent listens on port 8080 (ClusterIP `rag-agent:8080` in-cluster; edge Route only with `AGENT_ROUTE=true`). Local ports: Qdrant 6333, reasoning 8000, embed 8001, rerank 8002.

```bash
# Liveness: {"status":"ok","qdrant":true,"embed":true} (degraded/503 shapes documented)
curl -s http://localhost:8080/healthz

# Ranked manual chunks with citations, no LLM:
curl -s -X POST http://localhost:8080/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"IEA500I IOSCMDS COMMAND REJECTED","limit":5}'

# Grounded answer from the reasoning model (+ caller Splunk context):
curl -s -X POST http://localhost:8080/v1/answer \
  -H 'Content-Type: application/json' \
  -d '{"query":"How do I resolve IEA500I command rejected?","product":"z/OS"}'

# Streamed answer: token deltas, then exactly one terminal final event
curl -N -X POST 'http://localhost:8080/v1/answer?stream=true' \
  -H 'Content-Type: application/json' \
  -d '{"query":"What should LFAREA be set to in IEASYSxx?"}'

# Prometheus exposition (opt-in via METRICS_ENABLED, else 404):
curl -s http://localhost:8080/metrics
```

Errors use a stable `{"code","message"}` envelope (never stack traces or upstream bodies); overlong queries fail closed with `422 invalid_request`. Full contracts live in [docs/agent.md](docs/agent.md) and [docs/install_and_ops.md](docs/install_and_ops.md) §5.

---

## Library Scope

- Core libraries: `pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx2`, `fastapi`, `pydantic-settings`.
- No LangChain, LlamaIndex, or external vector databases.
