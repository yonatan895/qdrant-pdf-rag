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

### 3.4 Retrieval Accuracy & Benchmarking

Mainframe RAG includes golden-dataset regression gates and performance benchmarking:

```bash
# Evaluate retrieval accuracy against golden set (checks against evals/baseline.json)
EMBED_MODE=hash QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=local-corpus make eval

# Re-record committed accuracy baseline (dedicated PR only)
make eval-baseline

# Run performance benchmarking (ingest throughput, Qdrant RAM/disk, agent latency)
make bench

# Re-record committed benchmark baseline (dedicated PR only)
make bench-baseline
```

### 3.5 Developer Reporting & Interactive Query Demo

```bash
# Render terminal evaluation summary
make eval-report

# Generate self-contained offline HTML evaluation dashboard
make eval-html
# -> outputs bundles/eval-report.html

# Compare current evaluation against baseline with regression checks
make eval-compare

# Render benchmark performance report and HTML dashboard
make bench-report
make bench-html
# -> outputs bundles/bench-report.html

# Launch interactive query debugger (REPL)
EMBED_MODE=hash QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=local-corpus make query-demo

# Or inspect a single query with rank, scores, and text preview
EMBED_MODE=hash QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=local-corpus make query-demo QUERY="SC23-6883-70"
```

### 3.6 Testing with Local vLLM & GPU Acceleration (e.g. RTX 5060 8GB)

To test the entire reasoning and citation-generation pipeline locally with a real LLM on a consumer GPU:

```bash
# 1. Start local vLLM serving google/gemma-4-E4B-it-qat-mobile-ct on port 8000
# (Pass your Hugging Face token if downloading gated weights)
HF_TOKEN="<your-token>" make local-vllm

# 2. In another terminal, run the automated local end-to-end test suite
make test-vllm-e2e
```

The test script connects to `http://localhost:8000/v1`, starts a local Qdrant container, ingests synthetic IBM-shaped manuals, queries `/v1/answer`, and verifies that the model generates operational responses with valid, grounded citations.

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

### 5.3 Common Troubleshooting Scenarios

| Issue | Symptom | Remediation |
|---|---|---|
| **Dimension Mismatch** | Ingest/Search fails with 400 dimension mismatch | Verify `DENSE_DIM` in `airgap.env` matches `EMBED_MODEL` on vLLM. |
| **Qdrant Unready** | `/healthz` returns `503 qdrant_unready` | Check Qdrant pod logs (`oc logs qdrant-0`); check block PVC mount. |
| **NFS Storage Refusal** | `make airgap-deploy` fails validation | Set `STORAGE_CLASS` to an RWO block driver (Ceph RBD / SAN / EBS). |
| **Hash Mode in Prod** | Scripts fail closed with `EMBED_MODE=hash forbidden` | Remove `EMBED_MODE` from production environment; provide valid vLLM endpoint. |
