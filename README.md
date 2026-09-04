# Mainframe RAG

Citation-first expert mainframe agent: hybrid retrieval over ~100 GB of IBM-style manuals (IBM, Broadcom/CA, BMC, Precisely) on **air-gapped OpenShift**, answering operational questions with exact citations — document number, title, heading path, printed page label — plus optional JCL/REXX/operator steps from a reasoning model.

- **Design & Architecture:** [docs/architecture.md](docs/architecture.md) (source of truth)
- **Installation & Operations Guide:** [docs/install_and_ops.md](docs/install_and_ops.md) (step-by-step local & air-gap runbook)

---

## What This Repo Contains

| Path | What |
|---|---|
| `charts/qdrant-*.tgz` | Vendored Qdrant Helm chart (Apache-2.0); never `helm repo add` in the air-gap |
| `overlays/openshift/values.yaml` | OpenShift values: 3 replicas, unprivileged, RWO block, ClusterIP only |
| `deploy/` | NetworkPolicies, ingest CronJob/Job, agent Deployment |
| `oc-mirror/` | `ImageSetConfiguration` for disconnected mirroring |
| `src/mainframe_rag/ingest/` | PDF walk, IBM-style parse, chrome strip, chunk, classify, embed, Qdrant IO |
| `src/mainframe_rag/retrieve/` | Hybrid search (dense + BM25 prefetch, batched query, weighted RRF), optional cross-encoder rerank, payload projection, filters |
| `src/mainframe_rag/agent/` | Async FastAPI `/healthz`, `/v1/search`, `/v1/answer` (reasoning model, optional SSE streaming) |
| `scripts/` | Benchmark suite, golden set eval, report renderer, query demo, air-gap ops |
| `images/` | UBI Containerfiles (non-root, wheelhouse + BM25 weights baked in) |
| `tests/` | Unit, hygiene, regression gates, and Docker simulation integration tests |

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
| | `make test` | Run fast unit tests (`pytest -m "not integration"`) |
| | `make lint` | Run Ruff linter |
| | `make typecheck` | Run Mypy static type checker |
| **Simulation** | `make sim` | Run full integration simulation tier (ephemeral Docker Qdrant + mock LLM) |
| | `make sim-qdrant` | Start a local Docker Qdrant container on port 6333 |
| | `make sim-clean` | Stop and remove the local Docker Qdrant container |
| **Accuracy & Eval** | `make eval` | Score golden set queries (`evals/golden.jsonl`) against the mode-keyed baseline (`evals/baseline.json` in hash mode, `evals/baseline-vllm.json` in vllm mode) |
| | `make gate-l1` | L1 retrieval gate on an ephemeral Qdrant simulator (automated CI check) |
| | `make eval-answers` | Answer-tier grounding eval (`/v1/answer` must cite, abstain entries must not answer) — live GPU stack |
| | `make eval-holdout` | Score the frozen holdout (`evals/holdout.jsonl`, sha-pinned) — release candidates only |
| | `make eval-baseline` | Re-record committed retrieval accuracy baseline (dedicated PR) |
| | `make eval-draft` | Helper to draft golden-set candidate queries from collection payload |
| | `make verify-golden` | Mechanically verify golden expectations against the live collection (gates the corpus) |
| | `make eval-report` | Print terminal retrieval evaluation report |
| | `make eval-html` | Generate self-contained offline HTML evaluation dashboard (`bundles/eval-report.html`) |
| | `make eval-compare` | Compare evaluation runs with classification shifts and regression checks |
| | `make harness-gate` / `harness-l2` / `harness-l3` | Layered harness tiers L1/L2/L3 — RC-only, live GPU stack |
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
| | `make run-agent` | Start the agent with `LLM_STREAM=true` (reasoning SSE streaming for TTFT) on port 8080 |
| | `make test-vllm-e2e` | Run automated end-to-end suite against local vLLM & Qdrant with grounding validation |
| **Packaging & Standard Deployment** | `make airgap-pack` | Build sneakernet package (`dist/qdrant-pdf-rag-<sha>.tar`) on connected host |
| | `make airgap-load` | Load image archives and push to `${INTERNAL_REGISTRY}` (in air-gap or local test registry) |
| | `make airgap-deploy` | Deploy Qdrant cluster, Jaeger v2 tracing, and Agent via standard Helm + Kustomize |
| | `make airgap-ingest` | Launch one-shot ingest Job against `CORPUS_PVC=<pvc>` |
| | `make airgap-smoke` | Smoke test in-cluster search endpoint with fail-closed `/healthz` probe |
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

# 4. Try interactive query inspection
EMBED_MODE=hash QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=local-corpus make query-demo
```

---

## Standard Deployment Architecture (Air-Gap & Local Cluster)

The hardened 5-stage deployment pipeline (`airgap-pack` -> `airgap-load` -> `airgap-deploy` -> `airgap-ingest` -> `airgap-smoke`) is the **canonical deployment standard across the entire project**. Both production air-gapped OpenShift and local testing environments adhere to this pipeline (using the same scripts, Helm chart, and Kustomize overlays, with adapted sizing and SCC for local test clusters).

**The air-gap never builds images.** Connected `main` is the only image factory.

### 1. Production Air-Gapped OpenShift Workflow

1. **Connected Host:**
   ```bash
   git checkout <main-sha>  # Full 40-character Git SHA
   make airgap-pack         # -> dist/qdrant-pdf-rag-<sha>.tar + .sha256
   ```

2. **Sneakernet Transfer & Extract:**
   ```bash
   sha256sum -c qdrant-pdf-rag-<sha>.tar.sha256
   tar -xf qdrant-pdf-rag-<sha>.tar
   git clone repo.bundle qdrant-pdf-rag && cd qdrant-pdf-rag
   ```

3. **Air-Gapped Bastion:**
   ```bash
   cp airgap.env.example airgap.env   # Configure registry, namespace, storage class, vLLM
   make airgap-dryrun                 # Pre-flight verification (proves manifests without cluster)
   make airgap-load                   # Push 4 image archives to internal registry
   make airgap-deploy                 # Deploy Qdrant StatefulSet, Jaeger v2 tracing, and Agent
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

## Library Scope

- Core libraries: `pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx2`, `fastapi`, `pydantic-settings`.
- No LangChain, LlamaIndex, or external vector databases.
