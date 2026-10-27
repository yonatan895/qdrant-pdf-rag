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
| Live state (agent-fetched) | Zowe MCP server, read-only (ADR-0003) | Authorized typed read-only observations; only exact-target `job_status` is approved, other tools unapproved; bounded, audited, default-off, unwired (see [agent.md](agent.md#source-observations)) |
| Knowledge | Qdrant (self-hosted) | Manuals, precedent, "what does this mean / how is this supposed to work" |
| Reasoning | Internal vLLM / LiteLLM (platform team) | Thinking model for citation + solution / script generation |
| Embeddings | Internal vLLM stack | Dense vectors only; OpenAI-compatible endpoint |

The intended acceptance is **answers supported by supplied evidence and citations** (doc number, title, heading path, printed page label) and, when asked, JCL/REXX/operator steps verified against those citations. Claim support is not established by citation eligibility alone (see the answer contract below). Agent-fetched live-state prompt wiring under ADR-0003 remains incomplete; the desired behavior is distinct live citations and honest manuals-only degradation.

### 1.1 Ownership contract (prod vs local simulation)
<a id="ownership-contract"></a>

| Environment | Model tier (reasoning / embed / rerank) | Qdrant + ingest + retrieval + agent |
|---|---|---|
| **Production (air-gap OpenShift)** | **Platform team owns it**: vLLM servers behind the LiteLLM gateway. This repo consumes it over HTTP only (`*_BASE_URL` + per-leg virtual keys) — it never installs, deploys, or Helm-charts vLLM / LiteLLM / GPU operators on a product path. | **This repo owns it.** |
| **Local dev/test** | **Simulated by this repo** (`sh scripts/tools/run-task.sh local:stack`): local vLLM backends behind the real, digest-pinned LiteLLM gateway (`scripts/run_local_gateway.sh`) — a stand-in for the platform team's tier. | **Same code as prod**, same wire contract, plus OpenObserve tracing of our components verified end to end. |

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
| `zowe-mcp` | Proposed sidecar (ADR-0003); absent from the current Helm chart | Not deployed | Bridge/client code exists; answer-context integration and deployment wiring remain incomplete. Default-off; enabling requires the ADR-0003 reactivation gate, separate site credentials and written site acceptance |
| `openobserve` | Deployment (default on; `off` sentinel disables; disabling only the backend needs an explicit collector endpoint or `off`) | 1 | OpenObserve, local mode, RWO block PVC, tracing backend |
| `feedback` | Single-replica StatefulSet `rag-feedback` (ADR-0005, #804), rendered only with `FEEDBACK_ENABLED` (default off; [deploy.md](deploy.md#feedback-service)) | Deployed when enabled | Single SQLite writer for answer-quality records on its own kept RWO block PVC `rag-feedback-data` (never NFS); bearer token from an operator-created Secret; ClusterIP only, ingress limited to agent pods by `rag-feedback-ingress`; purges its own retention window in-process and exports purge outcomes on a bearer-protected `/metrics`; agents send to it best-effort when `FEEDBACK_ENABLED` (default off). Not console session state, which stays in browser `localStorage` (ADR-0004) |
| `open-webui` | Accepted (ADR-0007, #1070); absent from the current Helm chart | Not deployed | Optional docs front end behind a default-off flag: one replica, OIDC login, chats in SQLite on a kept RWO block PVC (never NFS) with a 30-day retention purge. It reaches the corpus only through a repository-owned Pipe over the agent's `/v1/chat`, with no model connection or gateway key. Its built-in RAG and vector store are off and proven empty, and egress is limited to the agent, DNS and the IdP. The console stays the fallback and HA path |
| `bm25-weights` | Baked in images | — | FastEmbed `Qdrant/bm25`; no runtime download |

- **Storage Constraint:** Qdrant persistent data volume **must** be RWO Block storage (NFS and object storage are refused). Ingest work volume is also RWO Block. The corpus volume may be mounted as read-only NFS or PVC.
- **Networking:** ClusterIP services only. No public Route to Qdrant. The agent console Route is optional (`AGENT_ROUTE=true`): when enabled, a Red Hat `oauth-proxy` sidecar terminates external ingress on 8443 (Service CA serving cert, `reencrypt` Route, `/healthz` probe bypass) while the ClusterIP 8080 API port stays available for in-cluster tools. A default-on ingress NetworkPolicy (`AGENT_NETWORK_POLICY`, agent pods only) limits 8080 to the release namespace, monitoring and operator-listed namespaces, and 8443 to the router ([deploy.md](deploy.md#agent-networkpolicy)). Inter-node Qdrant gossip is plaintext on the CNI (`config.cluster.p2p.enable_tls: false` in `values.yaml`) because cluster certificates are not mounted.

### 3.2 Canonical Deployment Standard Across Environments

The 5-stage pipeline (`airgap:pack` -> `airgap:load` -> `airgap:deploy` -> `airgap:ingest` -> `airgap:smoke`, orchestratable via `sh scripts/tools/run-task.sh airgap:pipeline` with pre-flight safety via `sh scripts/tools/run-task.sh airgap:validate`) is the **canonical deployment standard across the entire project**:
1. **Production (Air-Gapped OpenShift):** Full 3-replica Qdrant cluster, internal enterprise registry, `restricted-v2` SCC, cluster vLLM endpoints, sneakernet tarball verification.
2. **Local Cluster Testing (Kind + Local Registry):** Local single-node Kind cluster using a local container registry (`localhost:5000` / `airgap-registry:5000`) and 1-replica overrides (`QDRANT_EXTRA_VALUES`). Runs the exact same packaging scripts, image archives, Helm charts, with explicit local sizing and identity overrides. Kind does not implement OpenShift SCC admission.
3. **Continuous Integration (CI):** `sh scripts/tools/run-task.sh airgap:dryrun` validates applicable deployment changes without a cluster; product workflow path filters exclude markdown-only changes. The context workflow checks relevant docs separately. Published-bundle rehearsal runs on `main`/dispatch; enforced jobs and policy obligations are distinguished in [deploy](deploy.md#ci-policy).
4. **Release verification (Kind with both real models):** Published-main bundles must pass [the manual release gate](release-verification.md) before production transfer: a disposable Kind cluster runs the production Helm charts against both real models behind the authenticated TLS gateway, alongside the required CI jobs on the same signed bundle. Transfer the identical tested bundle. SCC admission, OAuth/Route, Service CA and runtime isolation are first verified on the site ([site OpenShift acceptance](install_and_ops.md#site-openshift-acceptance)); production version, identity, storage, and multi-node acceptance remain separate.

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
clients. The application lifespan retains client ownership and closes only what
it created (`AsyncExitStack`: startup failure, shutdown, cancellation); requests
use an `AgentResources` view that never closes anything.

Retrieval SDK dispatch is unchanged. Beneath it, serving holds only the async
read protocol `ports.AsyncQdrantReader` (#369): retrieval, the serving gate,
the evidence service and `AgentResources` await it and cannot write through
their handle. Sync tooling enters through `ports.AsyncReaderAdapter` (the
`retrieve.query.search` wrapper), and only ingest/admin code holds the writer
protocol `QdrantPoints`.
Within ingest, publication resolution, verification and every other helper that
only reads also take `QdrantReader`; only helpers that mutate storage require
`QdrantPoints`. `tests/test_serving_gate.py` and `tests/test_ingest_publish.py`
run the `qa:typecheck` checker to accept reads and reject writes; read-only
credentials remain the runtime boundary. The core's `Retriever` operation is the read boundary: a storage/admin client cannot
stand in for that operation, and the core has no storage handle on which to write.
The three core modules carry their strict mypy options inline; ordinary
`qa:typecheck` enforces them without changing shared pytest/verifier configuration.
`tests/test_answer_core.py` checks forbidden imports, rejects storage injection and
write use through the actual core operation, and exercises model parity and
stream cleanup followed by the next operation. Low-level SDK protocol assignment
pins are retired with the removed SDK-interface rewrite; existing retrieval
client behavior tests remain unchanged.

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
browser-only console state, strict CSP, local assets and external OAuth ingress;
[ADR-0007](adr/0007-open-webui-docs-front-end.md) and
[ADR-0008](adr/0008-open-webui-main-docs-platform.md) amend it only for the
Open WebUI docs platform (default on; the console stays the fallback).
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
artifact/topology acceptance stays in [the release gate](release-verification.md).

---

## 6. Software Architecture & Package Layout

```text
src/mainframe_rag/
  ports.py            # Layer-boundary protocols (Embedder, QdrantReader/QdrantPoints, Reranker, LLMClient, Tokenizer)
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
    catalog.py        # Publish-time source catalog, one entry per published revision (issue #827)
    bounds.py         # Opt-in embed-input and per-document size bounds; refuse, never truncate (issue #374)
    inventory.py      # Ingest progress tracking
    embed.py          # vLLM dense & FastEmbed BM25 embedder; embed-text builder
    qdrant_io.py      # Collection creation & upsert batching
    payload.py        # The stored point payload (an extraction-rules module)
    run_ingest.py     # Ingest CLI worker orchestration
    build.py          # Versioned immutable build binding
    placement.py      # Replica-placement evaluation for HA acceptance
    rules_version.py  # Extraction-rules version (issue #124)
    seal.py           # Stored-content certificate for immutable builds
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
    evidence.py       # Exact-evidence service: references, exact reads, source listing (issues #405, #828)
    admission.py      # Bounded request admission slots and FIFO wait queue (issue #374)
    sse.py            # SSE payloads: /v1/answer events + OpenAI chat chunks/errors/[DONE]
    tokenizer.py      # vLLM /tokenize counting with estimator fallback
    cites.py          # Citation shape validation & extraction
    metrics.py        # Prometheus counters/gauges (opt-in /metrics)
    live_state.py     # Zowe live-state routing/fetch layer (ADR-0003, default-off)
    zowe_mcp.py       # Zowe MCP client (read-only tools; mock backend for sim)
    chat_turn.py      # One active-user boundary for chat routes, retrieval and prompts
    core_ports.py     # Typed operations supplied to the answer use case
    model_adapter.py  # Normalizes legacy chat/stream seams; never owns the shared client
    resources.py      # Per-request read-only view of lifespan-owned clients; retrieval wiring + core deps
  feedback/           # Answer feedback (issue #780, ADR-0005)
    store.py          # SQLite store and vocabulary owner: answers, one rating per answer, purge
    service.py        # Single-writer HTTP service over the store (bearer token, fixed error codes)
    exchange.py       # Operator CLI: canonical JSONL export (read-only snapshot) and import (#805)
    report.py         # Offline report from an export file: counts and bounded labels, no text (#805)
    client.py         # Agent-side best-effort client: bounded queue, counted drops, console ratings
  webui/
    routes.py         # Operator console routes (/ui): fail-closed gate, HTMX form/SSE, CSP
    templates/        # Jinja2 shell + message pair (server-rendered, no external assets)
    static/           # Local CSS/JS + vendored htmx/sse with SHA256SUMS pin
  serve/              # Local vLLM VRAM budget profiles (LOCAL_RT_8GB) + resolve CLI
  eval/               # Evaluation measurement core: datasets, retrieval scoring, answers/answer-tier, judging, chat A/B, performance/quality gates, promotion verdicts, reports, statistics, load
  mcp/
    server.py         # MCP framing: JSON-RPC dispatch, tool schemas, transports
    knowledge.py      # Read-only knowledge MCP adapter over the agent's search, evidence and sources routes (issue #405)
    bridge.py         # FTP transport for the read-only Zowe MCP bridge (ADR-0003)
    mock.py           # Filesystem-backed mock z/OS backend (test-only)
  config.py           # Pydantic Settings & environment validation
  logs.py             # One-JSON-object-per-line logging
  manifest.py         # Run manifests (unreachable Qdrant version is unknown, never the pin)
  regexes.py          # Shared identifier regexes (MSG_RE, DOCNO_RE)
  tracing.py          # OTel export: agent + ingest; library default off, deploy default on
```

**Allowed Dependencies:** Python 3.14 GIL, `pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx2`, `fastapi`, `jinja2` (operator console templates), `python-multipart` (console form parsing), `uvicorn`, `pydantic`, `pydantic-settings`, `opentelemetry-api`/`-sdk` plus the OTLP-HTTP and Prometheus exporters, `urllib3`, `pyjwt[crypto]` (team-tool caller verification, [team tools](team-tools.md#caller-verification)).

### Per-area reference docs

This file states the design; the deep contracts, rationale and constants live
with their owners. The machine-checked
[context map](agent-workflow.md#context-map) is the current contract-to-owner
index (one fact, one owner) and is verified by `qa:context`; it replaces the
hand-maintained list that used to sit here.
