# Expert Mainframe Agent — Design and Architecture Guide

**Status:** architecture and ownership map; implementation status belongs to the linked contracts
**Audience:** coding agents, platform architects, and operators  
**Constraint:** public GitHub for *code and cluster recipes*; air-gapped OpenShift for *runtime, corpus, embeddings*  
**Operations Guide:** see **[docs/install_and_ops.md](install_and_ops.md)** for step-by-step setup and operational runbooks.  
**Non-goal:** republishing IBM / Broadcom / BMC / Precisely manuals  

---

## 1. Mission

Build a **citation-first expert mainframe agent** that answers operational questions from a ~100 GB born-digital PDF corpus (mostly IBM, plus Broadcom/CA, BMC, Precisely) running on an **air-gapped OpenShift** cluster owned by another team.

| Layer | System | Job |
|---|---|---|
| Live state (caller-supplied) | Splunk (existing) | Events, jobs, messages *now* (context in, not crawl — ADR-0001) |
| Live state (agent-fetched) | Zowe MCP server, read-only (ADR-0003) | Datasets, JES spool, USS, job status — bounded, audited, default-off |
| Knowledge | Qdrant (self-hosted) | Manuals, precedent, "what does this mean / how is this supposed to work" |
| Reasoning | Internal vLLM / LiteLLM (platform team) | Thinking model for citation + solution / script generation |
| Embeddings | Internal vLLM stack | Dense vectors only; OpenAI-compatible endpoint |

The intended acceptance is **answers supported by supplied evidence and citations** (doc number, title, heading path, printed page label) and, when asked, JCL/REXX/operator steps verified against those citations. Claim support is not established by citation eligibility alone (see the answer contract below). Agent-fetched live-state prompt wiring under ADR-0003 remains incomplete; the desired behavior is distinct live citations and honest manuals-only degradation.

### 1.1 Ownership contract (prod vs local simulation)

| Environment | Model tier (reasoning / embed / rerank) | Qdrant + ingest + retrieval + agent |
|---|---|---|
| **Production (air-gap OpenShift)** | **Platform team owns it**: vLLM servers behind the LiteLLM gateway. This repo consumes it over HTTP only (`*_BASE_URL` + per-leg virtual keys) — it never installs, deploys, or Helm-charts vLLM / LiteLLM / GPU operators on a product path. | **This repo owns it.** |
| **Local dev/test** | **Simulated by this repo** (`sh scripts/tools/run-task.sh local:stack`): local vLLM backends behind the real, digest-pinned LiteLLM gateway (`scripts/run_local_gateway.sh`) — a stand-in for the platform team's tier. | **Same code as prod**, same wire contract, plus Jaeger tracing of our components verified end to end. |

Local simulation exists so agent/ingest always exercise the production gateway wire shape (single origin, model-id routing, per-leg Bearer virtual keys, native `/rerank` + `/v1/score` pass-through) — never a straight-to-vLLM shortcut. The local model/gateway simulation scripts (`scripts/run_local_gateway.sh`, `scripts/run_local_stack.sh`, `scripts/run_local_vllm.sh`) are local-only: never in the air gap or Helm. CI's `airgap-rehearsal` is a deployment-config rehearsal and uses the documented `scripts/mock_vllm.py` stand-in instead.

---

## 2. System Context & Boundaries

The diagram includes the intended ADR-0003 live-state tier. Its bridge/client
code exists, but the current Helm deployment does not include the sidecar or
complete its answer-context integration; see the workload status table below.

```
                    ┌─────────────────────────────────────────┐
  Connected LAN     │  Public GitHub  (this repo)             │
                    │  chart, Taskfile, ingest, retrieval     │
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
│              vLLM embeddings   LiteLLM/vLLM    Splunk context   │
│              (dense only)      reasoning LLM   (caller-supplied)│
│                                                                    │
│  ┌────────────────┐  MCP/HTTP   ┌────────────────┐                │
│  │ FTP MCP bridge │◀────────────│ Agent svc      │                │
│  │ (agent-image   │ read-only   │ fetches live   │                │
│  │ sidecar, stdio │ tools only  │ (ADR-0003)     │                │
│  │ or localhost)  │             │                │                │
│  └───────▲────────┘             └────────────────┘                │
│          │ FTP port 21 (read-only SAF profile)                   │
│          ▼                                                       │
│   z/OS live state (datasets, JES spool, USS, job status)         │
└──────────────────────────────────────────────────────────────────┘
```

### 2.1 Trust and Legal Boundaries
- **Zero Internet Access:** Runtime is completely disconnected. All container images, wheelhouses, and BM25 weights are mirrored into the enterprise.
- **Corpus Protection:** Real PDFs, manual text, Qdrant snapshots, and customer JCL are **never** committed to Git. Public GitHub hosts code, tests against synthetic PDFs, and deployment recipes only.
- **Live-state reads (ADR-0003):** the agent fetches only the allowlisted read tools over ClusterIP; credentials never enter git; tool results are untrusted data screened like retrieved chunks; every fetch is audit-logged (ids/counts, never dataset/spool text).

---

## 3. Target Infrastructure & Packaging

### 3.1 Workloads & Security Context

| Workload | Kind | Replicas | Spec |
|---|---|---|---|
| `qdrant` | StatefulSet (vendored chart) | 3 | Cluster mode, P2P 6335 (TLS off), HTTP 6333, gRPC 6334, `restricted-v2` SCC |
| `rag-agent` | Deployment | 2 | FastAPI, unprivileged, no GPU |
| `ingest` | One-Shot Job | 1 | High CPU, worker pool, RWO scratch |
| `zowe-mcp` | Proposed sidecar (ADR-0003); absent from the current Helm chart | Not deployed | Bridge/client code exists; answer-context integration and deployment wiring remain incomplete. Default-off; requires separate site credentials and acceptance |
| `jaeger` | Deployment (default on; `off` sentinel disables) | 1 | Jaeger v2 all-in-one, Badger RWO block PVC, tracing backend |
| `bm25-weights` | Baked in images | — | FastEmbed `Qdrant/bm25`; no runtime download |

- **Storage Constraint:** Qdrant persistent data volume **must** be RWO Block storage (NFS and object storage are refused). Ingest work volume is also RWO Block. The corpus volume may be mounted as read-only NFS or PVC.
- **Networking:** ClusterIP services only. No public Route to Qdrant. The agent console Route is optional (`AGENT_ROUTE=true`): when enabled, a Red Hat `oauth-proxy` sidecar terminates external ingress on 8443 (Service CA serving cert, `reencrypt` Route, `/healthz` probe bypass) while the ClusterIP 8080 API port stays available for in-cluster tools. Inter-node Qdrant gossip is plaintext on the CNI (`config.cluster.p2p.enable_tls: false` in `values.yaml`) because cluster certificates are not mounted.

### 3.2 Canonical Deployment Standard Across Environments

The 5-stage pipeline (`airgap:pack` -> `airgap:load` -> `airgap:deploy` -> `airgap:ingest` -> `airgap:smoke`, orchestratable via `sh scripts/tools/run-task.sh airgap:pipeline` with pre-flight safety via `sh scripts/tools/run-task.sh airgap:validate`) is the **canonical deployment standard across the entire project**:
1. **Production (Air-Gapped OpenShift):** Full 3-replica Qdrant cluster, internal enterprise registry, `restricted-v2` SCC, cluster vLLM endpoints, sneakernet tarball verification.
2. **Local Cluster Testing (Kind + Local Registry):** Local single-node Kind cluster using a local container registry (`localhost:5000` / `airgap-registry:5000`) and 1-replica overrides (`QDRANT_EXTRA_VALUES`). Runs the exact same packaging scripts, image archives, Helm charts, with explicit local sizing and identity overrides. Kind does not implement OpenShift SCC admission.
3. **Continuous Integration (CI):** `sh scripts/tools/run-task.sh airgap:dryrun` validates applicable deployment changes without a cluster; product workflow path filters exclude markdown-only changes. The context workflow checks relevant docs separately. Published-bundle rehearsal runs on `main`/dispatch; enforced jobs and policy obligations are distinguished in [deploy](deploy.md#ci-policy).
4. **Release verification (Windows OpenShift Local / CRC):** Published-main bundles must pass [the manual CRC gate](crc-release-verification.md) before production transfer. WSL retains the authenticated model gateway and GPU servers; CRC exercises the production Helm charts, real SCC admission, OAuth/Route, persistence, and runtime isolation. Transfer the identical tested bundle. Production version, identity, storage, and multi-node acceptance remain separate.

Application and Qdrant resources use the same release charts. Local/CI platform stand-ins, synthetic corpus generators and sizing overrides remain separate test fixtures; their success does not establish production platform acceptance.

---

## 4. Data Processing & Retrieval Architecture

<a id="boundary-map"></a>
### Producer, persistence and consumer boundaries

Start with the decision owner, then inspect affected unchanged callers and
consumers. A filename list cannot establish that a change is isolated.

| Producer / decision owner | State crossing the boundary | Consumers to inspect | Canonical contract |
|---|---|---|---|
| Discovery, generic parser, chrome, chunk, classify | Parsed metadata, sanitized text, chunk IDs and fixed vocabulary | Embed-text construction, inventory, Qdrant payload/filter/citation readers | [Ingest](ingest.md) |
| Source/representation identity | Revision keys, representation manifest and generation fingerprints | Completion/skip/refresh, publication, serving/cache, eval | [Identity](ingest.md#identity-contract) |
| Ingest orchestration/admin writer | Data points, completion store, pending/committed manifest, inventory, alias | Active readers, readiness, recovery, rollback and GC | [Publication](ingest.md#publication-contract), [metadata](ingest.md#metadata-contract) |
| Serving generation gate | Validated physical target, process-local TTL cache | Search, answer, both chat aliases, console and unchanged query entry points | [Reader lifetime](agent.md#serving-contract) |
| Dense/BM25 query, filters, screen, fusion, optional rerank | Ranked candidates and timings | Prompt packing, returned hits, eval/capture/replay | [Retrieval](retrieval.md) |
| Prompt packing and reasoning | Supplied-evidence manifest, provisional tokens, parsed citations | API JSON/SSE, chat, browser render/export, answer eval/L2 | [Answer states](agent.md#answer-contract) |
| Operator settings and packaging | Example/override → preflight → rendered agent and ingest env → Settings | Gateway auth/TLS, both model consumers, health/readiness and migration | [Configuration](deploy.md#configuration-contract) |
| Lifespan, logs and tracing | Pools, structured logs, bounded export queues | Health, request paths, shutdown, collector and run manifests | [HTTP/lifecycle](agent.md#http-model-contract), agent log/trace contract |

At each row ask: **What false implementation could satisfy our current local
assertion?** A complete inventory does not prove complete searchable coverage;
a physical name does not prove immutability; a local progress lock does not
prove publication serialization. These are outstanding #391 proof boundaries.

The [exact-evidence design](evidence-contract.md#evidence-contract) owns M1/A0's
proposed build/reference identity, immutable publication, access, retention and
compatibility decisions. Its state tables explicitly separate existing behavior
from later #405/#391/#373/#360 implementation and platform acceptance.

M1/A1 narrows the existing core boundaries: transports call shared use cases;
core consumes typed retrieval/model ports; adapters own concrete storage and
sync/async compatibility. Serving read capabilities remain separate from
publication/admin writes. Core cannot depend on `agent.app`, console routes,
deployment or MCP transport modules. Dependency checks must exercise actual
imports and the operations available to a consumer, not infer read-only authority
from a protocol annotation alone. Runtime credentials remain independently
read-only. Stream token/terminal/error types must preserve buffered recovery and
post-emission no-replay behavior, along with cancellation and client ownership.
`AnswerCoreDeps` now receives `Retriever` and `AnswerModel` operations and typed
prompt builders; it has no raw Qdrant/embedder/admin handle. `ModelAdapter`
adapts sync/async calls with structured `ChatResult` completions and token/done
mappings. Bare strings are rejected; finish/usage metadata is never invented. Errors
propagate as exceptions, including the existing typed `TruncatedStreamError`.
`CoreToken`/`CoreFinal` discriminate the internal stream; transport wire schemas
remain unchanged. The adapter closes per-operation generators, never shared
clients. The application lifespan retains client ownership.

`QdrantSearch` and optional `QdrantBatchSearch` expose retrieval reads, while
`QdrantPoints`/`AsyncQdrantPoints` retain publication/admin operations. Structural
protocols restrict checked consumer operations; they are not a Python sandbox
and do not prove a supplied credential is read-only. Existing serving credential
requirements remain mandatory. `tests/test_answer_core.py` checks the transitive
core import graph, rejects raw storage injection/write use with the real type
checker, and exercises adapter parity plus stream cleanup/next operation.
Targeted strict type options apply to `answer_core`, `core_ports` and
`model_adapter`; legacy modules gain no new ignore rules.

### 4.1 Document Ingest & Chunking

[Ingest](ingest.md) owns generic discovery, parsing/sanitization, chrome removal,
outline/fallback sections, atomic code/table splits, fixed chunk types, UUID5
identity, spawn-worker IPC and completion/publication. Vendor signals remain
payload, never ingestion gates. Do not thread vendor-specific conditions through
retrieval or HTTP when parse/classify can emit the necessary payload.

### 4.2 Hybrid Embeddings & Collection Configuration

[Ingest embedding/load](ingest.md) owns named dense/BM25 vectors, dimensions,
indexes-before-load, batched upserts, model attestation and stored representation.
[Deployment](deploy.md#configuration-contract) owns endpoint/key propagation.
Dense vectors are platform HTTP calls; sparse weights are baked/local. Read the
[identity](ingest.md#identity-contract) and [metadata](ingest.md#metadata-contract)
contracts before model migration. No copy of the representation hash-field policy
is maintained here.

### 4.3 Hybrid Retrieval: Batched Prefetch, RRF Fusion, Rerank & Diversification

[Retrieval](retrieval.md) owns filtered dense/BM25 prefetch, local weighted RRF,
optional cross-encoder order/fallback, trap/identifier bypass, rewrite/split and
three-phase diversification. `async_search` is the shared implementation and
`search` a fail-closed synchronous wrapper. Limits/defaults live in Settings;
changes require the applicable evaluation and A/B evidence.

### 4.4 Reasoning, Prompt Assembly & Citation Enforcement

[Agent](agent.md#answer-contract) owns prompt packing, final supplied evidence,
citation eligibility, abstention, provisional/completed output and limitations.
Answer/chat/console share `answer_core`. Search never invokes an LLM. Answering
uses the configured reasoning model; citation eligibility alone is not semantic
support or script validation. ADR-0003 live-state routing/fetch helpers exist
behind a default-off flag, but endpoint prompt wiring is still required under
ROADMAP #90/#91. Splunk remains caller-supplied context.

### 4.5 Outbound HTTP, Agent Lifespan & API Contracts

[Agent HTTP/model](agent.md#http-model-contract) owns per-operation retries and
fallbacks, Settings-bounded pools/timeouts, stable error bodies, endpoint-specific
SSE completion and shutdown. [ADR-0004](adr/0004-operator-console-htmx.md) owns
browser-only console state, strict CSP, local assets and external OAuth ingress.
`tracing.py` owns bounded fail-open OTel export for agent and ingest, with no
manual text/secrets; bounded query span text is the explicit exception to the
JSON-log prohibition. A collector outage is not required-metadata permission.

## 5. Evaluation, Benchmarking & Tooling

[Live-stack](live-stack.md#verification-minimums) is the single required-tier
map. [Testing](testing.md#evidence-design) owns independent expected results,
client fidelity and test consolidation. [Eval](eval.md) owns venue, baseline,
threshold and measured-outcome detail. No baseline changes to make a patch pass.

Local launch order, gateway handoff, GPU profiles and mode limits live in
[live-stack](live-stack.md#operating-modes); installation, component-debugging,
reports and REPL procedures live in [install and operations](install_and_ops.md).
A local mocked run cannot certify release behavior or distributed HA. Published
artifact/topology acceptance stays in [the CRC gate](crc-release-verification.md).

---

## 6. Software Architecture & Package Layout

```text
src/mainframe_rag/
  ports.py            # Layer-boundary protocols (Embedder, QdrantPoints, Reranker, LLMClient, Tokenizer)
  ingest/
    walk.py           # *.pdf discovery
    ibm_pdf.py        # PDF parser & metadata extraction
    chrome.py         # Running header/footer stripping
    chunk.py          # Outline-based chunking, code-atomic regions & UUID5 generation
    classify.py       # Message, syntax, table, narrative classification
    context.py        # Contextual retrieval prefixes (versioned cache)
    identity.py       # Source-revision identity and collision/dedup planning
    representation.py # Stored contract, migration state, reader compatibility
    completion.py     # Per-document verification and progress lock
    publish.py        # Staging and final publication checks
    inventory.py      # Ingest progress tracking
    embed.py          # vLLM dense & FastEmbed BM25 embedder; embed-text builder
    qdrant_io.py      # Collection creation & upsert batching
    run_ingest.py     # Ingest CLI worker orchestration
  retrieve/
    query.py          # Batched prefetch, weighted RRF fusion, diversification (sync + async)
    filters.py        # Query classification & Qdrant filter building
    rerank.py         # Cross-encoder rerank: HashReranker / HttpReranker + dispatch
    screen.py         # Injection trap screen (trap before identifiers)
    rewrite.py        # Deterministic acronym expansion (default off)
    split.py          # Comparative/multipath query splitting (comparative ON, diagnostic off)
    acronyms_v1.json  # Versioned acronym glossary
  agent/
    app.py            # FastAPI service (async routes, SSE) & lifespan client management
    answer.py         # Reasoning LLM client (sync/async/SSE), prompt construction, condensation & citation grounding
    answer_core.py    # Shared engine: retrieval(optional), prompt budget, LLM, citation parse
    serving.py        # Read-only physical-generation gate and TTL cache
    sse.py            # SSE payloads: /v1/answer events + OpenAI chat chunks/errors/[DONE]
    tokenizer.py      # vLLM /tokenize counting with estimator fallback
    cites.py          # Citation shape validation & extraction
    metrics.py        # Prometheus counters/gauges (opt-in /metrics)
    live_state.py     # Zowe live-state routing/fetch layer (ADR-0003, default-off)
    zowe_mcp.py       # Zowe MCP client (read-only tools; mock backend for sim)
  webui/
    routes.py         # Operator console routes (/ui): fail-closed gate, HTMX form/SSE, CSP
    templates/        # Jinja2 shell + message pair (server-rendered, no external assets)
    static/           # Local CSS/JS + vendored htmx/sse with SHA256SUMS pin
  serve/              # Local vLLM VRAM budget profiles (LOCAL_RT_8GB) + resolve CLI
  config.py           # Pydantic Settings & environment validation
  logs.py             # One-JSON-object-per-line logging
  manifest.py         # Run manifests (unreachable Qdrant version is unknown, never the pin)
  regexes.py          # Shared identifier regexes (MSG_RE, DOCNO_RE)
  tracing.py          # OTel export: agent + ingest; library default off, deploy default on
```

**Allowed Dependencies:** Python 3.14 GIL, `pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx2`, `fastapi`, `jinja2` (operator console templates), `python-multipart` (console form parsing), `uvicorn`, `pydantic`, `pydantic-settings`, `opentelemetry-api`/`-sdk` plus the OTLP-HTTP and Prometheus exporters.

### Per-area reference docs

Deep contracts, rationale, and constants live in the per-area references (one fact, one owner — this file states the design, those files state the details):
- `docs/ingest.md` — parse/chunk/classify/embed/Qdrant pipeline, payload contract, id-stability rules
- `docs/retrieval.md` — query flow, RRF, filters, screen, rerank, rewrite, diversification (incl. Appendix A identifier patterns)
- `docs/agent.md` — endpoint/error/SSE contracts, prompt assembly and budgets, Settings catalog
- `docs/eval.md` — tiers, gates, thresholds, baselines, mock fidelity limits
- `docs/deploy.md` — air-gap script contracts, render pipeline, signing model, CI inventory
