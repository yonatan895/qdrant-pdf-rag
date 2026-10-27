# Mainframe RAG

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/yonatan895/qdrant-pdf-rag)

Citation-first expert mainframe agent: hybrid retrieval over ~100 GB of IBM-style manuals (IBM, Broadcom/CA, BMC, Precisely) on **air-gapped OpenShift**, answering operational questions with exact citations — document number, title, heading path, printed page label — plus optional JCL/REXX/operator steps from a reasoning model.

Models (reasoning, dense embed, reranker) are served by the **platform team's internal vLLM / LiteLLM gateway** on a separate cluster — in production this repo only consumes that tier over HTTP; locally `sh scripts/tools/run-task.sh local:stack` simulates the full topology (vLLM behind the real LiteLLM gateway + Qdrant + OpenObserve + agent, with tracing verified end to end). See [Model Gateway & Ownership Contract](#model-gateway--ownership-contract) below.

- **Design & Architecture:** [docs/architecture.md](docs/architecture.md) (source of truth)
- **Installation & Operations Guide:** [docs/install_and_ops.md](docs/install_and_ops.md) (step-by-step local & air-gap runbook)

---

## What This Repo Contains

| Path | What |
|---|---|
| `charts/qdrant-*.tgz` | Vendored Qdrant Helm chart (Apache-2.0); never `helm repo add` in the air-gap |
| `charts/qdrant-openshift.values.yaml` | Qdrant OpenShift values: 3-replica StatefulSet, unprivileged, RWO block, ClusterIP only |
| `charts/mainframe-rag/` | 2-replica agent Deployment, one-shot ingest Job, OpenObserve (tracing on by default); `AGENT_ROUTE=true` renders the OAuth sidecar and a reencrypt Route — fails closed while the oauth-proxy pin is `sha256:PENDING` |
| `src/mainframe_rag/ingest/` | PDF walk, IBM-style parse, chrome strip, chunk, classify, embed, Qdrant IO |
| `src/mainframe_rag/retrieve/` | Hybrid search (dense + BM25 prefetch, batched query, weighted RRF), query-class screen, optional cross-encoder rerank (`RERANK_ENDPOINT_ORDER`), diversification, filters |
| `src/mainframe_rag/agent/` | Async FastAPI `/healthz` + `/livez`, `/v1/search`, `/v1/answer`, multi-turn `/v1/chat` + `/v1/chat/completions` (shared `answer_core`, optional SSE streaming), exact-evidence `GET /v1/evidence/{reference}` and `GET /v1/sources` (contract: `docs/evidence-contract.md`), opt-in `GET /metrics` |
| `src/mainframe_rag/webui/` | Operator console served at `/ui` (ADR-0004): Jinja2 + vendored HTMX/SSE, browser-only state, strict CSP, `UI_ENABLED` fail-closed |
| `src/mainframe_rag/feedback/` | Answer-feedback store, single-writer service and agent-side client (ADR-0005, #780, #804); default off; the service is an optional chart StatefulSet (`FEEDBACK_ENABLED`) |
| `src/mainframe_rag/mcp/` | Read-only Zowe live-state bridge (default off; mock backend for sim; serve via `python -m mainframe_rag.mcp`) |
| `src/mainframe_rag/ops/` | Read-only operations CLI (`mainframe-rag-ops health, search, answer`; HTTP consumer of `/healthz`, `/v1/search`, `/v1/answer` only; contract: `docs/agent.md` Operations CLI) |
| `src/mainframe_rag/serve/` | Local vLLM VRAM budget profiles (`LOCAL_RT_8GB`, …) + `resolve` CLI (`python -m mainframe_rag.serve resolve …`) |
| `scripts/` | Benchmark suite, golden set eval, report renderer, query demo, gateway readiness probe (`probe_gateway.py`), air-gap ops |
| `images/` | UBI Containerfiles (non-root, wheelhouse + BM25 weights baked in) |
| `tests/` | Unit, hygiene, regression gates, and Docker simulation integration tests |

---

## Model Gateway & Ownership Contract

**Production (air-gap):** the platform team owns the model tier — reasoning,
dense embed and rerank behind its LiteLLM gateway on a separate cluster. This
repo owns Qdrant + ingest + retrieval + the agent and consumes that tier **over
HTTP only**: never vLLM / LiteLLM / GPU operators on a product path, and no
direct-to-vLLM leg. Tracing is **on by default** for this repo's components;
`OTEL_EXPORTER_OTLP_ENDPOINT` in `airgap.env` selects a custom collector or
`off`. See the [ownership contract](docs/architecture.md#ownership-contract).

**Local dev/test:** `local:stack` simulates the complete topology on one machine
— this repo's Qdrant + agent + OpenObserve plus local vLLM backends behind the
**real** (digest-pinned) LiteLLM gateway. Agent and ingest still go through the
gateway, so the production wire contract (single origin, model-id routing,
per-leg virtual keys) is what gets tested, and a `v1.search` span must land in
the local OpenObserve before the stack reports up. The console is on by default
(`UI_ENABLED=true`; `UI_ENABLED=false` exercises the fail-closed route set). The
local model/gateway scripts are local-only — CI's `airgap-rehearsal` uses
`scripts/mock_vllm.py`; see
[install and operations §3.6](docs/install_and_ops.md#36-local-vllm-inference--gpu-acceleration-rtx-5060--8gb-vram).

Every leg and its per-leg key is set in `airgap.env` and one operator-created
Secret (`GATEWAY_API_KEY_SECRET` via `secretKeyRef`, never plaintext; unset =
keyless); the
[wiring table](docs/install_and_ops.md#43-configure-environment--pre-flight-validation)
owns the names. Prove every leg from inside the cluster and apply the order the
probe recommends:

```bash
kubectl -n mainframe-rag exec deploy/rag-agent -- python3 /app/scripts/probe_gateway.py
```

Local full-stack simulation (model commands in separate terminals; the default
8 GiB pack has no reranker):

```bash
sh scripts/tools/run-task.sh local:llm
sh scripts/tools/run-task.sh local:embed
RERANK_ENABLED=false sh scripts/tools/run-task.sh local:stack
# Optional ingest: CORPUS_DIR=<dir> RERANK_ENABLED=false sh scripts/tools/run-task.sh local:stack
```

Fresh Kind deployment: [clean Kind guide](docs/local-kind.md). Helm and the
preserved corpus: [local real-corpus operations](docs/local-real-corpus.md).
Reranking needs the separately selected compatible three-model pack; never add
it beside the default reasoning/embedding pair.


## Live State (Optional, Default Off)

- **Splunk (system of record):** caller-supplied context — pass `splunk_context` with `/v1/answer`, `/v1/chat`, or a console request; the agent never crawls Splunk itself.
- **Zowe MCP (agent-fetched):** bridge/client code for read-only observations (`zowe_mcp_enabled=false` default, unwired from endpoints; only exact-target job status is approved by ADR-0003; mock backend in sim). Answer-context and Helm sidecar integration remain incomplete. See [docs/architecture.md](docs/architecture.md).

---

## Legal & Repository Rules (Hard)

- **No manuals in this repository.** No PDFs, HTML dumps, Redbooks, or "cleaned" markdown of IBM / Broadcom / BMC / Precisely content. The corpus stays on enterprise storage.
- **Ingest inside the enterprise only.** Never ingest real manuals from a public runner or connected cluster; ingest runs after Qdrant is healthy in the air-gap.
- **Never commit:** Qdrant snapshots/embeddings/chunk dumps, production JCL, credentials, TLS material, real storage class names. Placeholders only.
- **Tests use synthetic content only;** product names appear nominatively.

---

## Task command reference

Use the repository-pinned entry from the checkout root. On a connected host,
install once with `sh scripts/tools/install-task.sh`; offline bootstrap supplies
the same tool without network access. Task `desc`/`summary` is the input and
default catalog:

```bash
sh scripts/tools/run-task.sh --list                 # names and one-line goals
sh scripts/tools/run-task.sh <name> --summary        # inputs, defaults, side effects
```

Setup, inputs, cwd, exit codes and older command names are owned by the
[runner contract](docs/task-runner.md). Required verification tiers are owned by
[docs/live-stack.md](docs/live-stack.md#verification-minimums).

---

## Quickstart (Connected Host)

```bash
# 1. Install the pinned host runner, then prepare the environment
sh scripts/tools/install-task.sh
sh scripts/tools/run-task.sh dev:setup PY=python3.14
sh scripts/tools/run-task.sh artifacts:bm25

# 2. Run quality checks
sh scripts/tools/run-task.sh qa:check

# 3. Run integration simulation
sh scripts/tools/run-task.sh qa:sim

# 4. Ingest a sample corpus in development mode (hash embedder, scratch collection)
sh scripts/tools/run-task.sh local:qdrant:up   # local Qdrant on 127.0.0.1:6333 (or reuse QDRANT_SIM_URL)
QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=dev-corpus \
  EMBED_MODE=hash ALLOW_HASH_MODE=true \
  .venv/bin/python -m mainframe_rag.ingest.run_ingest \
  --src <dir-with-pdfs> --progress /tmp/dev-corpus/inventory.jsonl --workers 2
```

---

## Standard Deployment Architecture (Air-Gap & Local Cluster)

The hardened pipeline is `airgap:pack` (connected-host pre-step) then
`airgap:validate` -> `airgap:load` -> `airgap:deploy` -> `airgap:ingest` ->
`airgap:smoke` (or `airgap:pipeline`). The same scripts and Helm charts serve
production air-gapped OpenShift and local testing, with adapted sizing and SCC
locally. The air gap never builds images; connected `main` is the only image
factory.

- Production: [install and operations §4](docs/install_and_ops.md#4-standard-deployment-architecture-air-gap-production--local-cluster-testing)
  and the [release gate](docs/release-verification.md).
- Local Kind and registry: [docs/local-kind.md](docs/local-kind.md) and
  [install and operations §4.7](docs/install_and_ops.md#47-local-cluster-testing-standard-kind--local-registry).
- Packaging, signing, pins, sizing and security: [docs/deploy.md](docs/deploy.md).

---

## Endpoints

The agent listens on port 8080 (ClusterIP `rag-agent:8080` in-cluster; the external console Route is `AGENT_ROUTE=true`). Local ports: Qdrant 6333, reasoning 8000, embed 8001, rerank 8002, LiteLLM gateway 4000 (`sh scripts/tools/run-task.sh local:stack`), OpenObserve (port per `local:stack`).

```bash
# Readiness: {"status":"ok","qdrant":true,"embed":true,"representation":"compatible","rerank":null} (degraded = HTTP 503; rerank is true/false when RERANK_ENABLED=true)
curl -s http://localhost:8080/healthz
# Liveness (process only): {"status":"alive"}
curl -s http://localhost:8080/livez

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

# Multi-turn chat — client-managed history (OpenAI-compatible alias at /v1/chat/completions):
curl -s -X POST http://localhost:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"How do I resolve IEA500I command rejected?"}]}'

# Operator console (ADR-0004) — served when UI_ENABLED=true (sh scripts/tools/run-task.sh local:stack does):
#   http://localhost:8080/ui

# Prometheus exposition (opt-in via METRICS_ENABLED, else 404):
curl -s http://localhost:8080/metrics
```

Errors use a stable `{"code","message"}` envelope (never stack traces or upstream bodies); overlong queries and oversized chat bodies (`chat_max_body_chars`) fail closed with `422 invalid_request`. Full contracts live in [docs/agent.md](docs/agent.md) and [docs/install_and_ops.md](docs/install_and_ops.md) §5.

---

## Library Scope

- Core libraries: `pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx2`, `fastapi`, `jinja2` (console templates), `python-multipart` (console forms), `pydantic-settings`.
- No LangChain, LlamaIndex, or external vector databases.
