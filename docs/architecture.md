# Expert Mainframe Agent — Design and Architecture Guide

**Status:** implementation-ready
**Audience:** coding agents and humans building this system
**Constraint:** public GitHub for *code and cluster recipes*; air-gapped OpenShift for *runtime, corpus, embeddings*
**Non-goal:** republishing IBM / Broadcom / BMC / Precisely manuals

This document is the source of truth for the first implementation. If a later PR disagrees with it, update this file in the same PR.

---

## 1. Mission

Build a **citation-first expert mainframe agent** that answers operational questions from a ~100 GB born-digital PDF corpus (mostly IBM, plus Broadcom/CA, BMC, Precisely) running on an **air-gapped OpenShift** cluster owned by another team.

| Layer | System | Job |
|---|---|---|
| Live state | Splunk (existing) | Events, jobs, messages *now* |
| Knowledge | Qdrant | Manuals, precedent, "what does this mean / how is this supposed to work" |
| Reasoning | Internal vLLM / LiteLLM (owned by other team) | Thinking model for citation + solution / script generation |
| Embeddings | Same vLLM stack | Dense vectors only; model TBD at wiring time |

The agent must return **answers with citations** (doc number, title, heading path, printed page label) and, when asked, JCL/REXX/operator steps grounded in those citations. Citation-plus-generation traffic goes through a reasoning model, not a cheap chat model.

### 1.1 In scope

- Disconnected Qdrant on OpenShift (Helm, unprivileged image, RWO block storage)
- PDF ingest that understands IBM-style manuals (doc number, outline, page labels, message IDs)
- Hybrid retrieval: dense + BM25, payload filters, RRF
- Agent retrieval API used by the reasoning model
- Air-gap packaging: build on a connected host, import as a bundle
- Public GitHub for infrastructure + ingest + retrieval **code**

### 1.2 Out of scope (v1)

- Qdrant Private Cloud / commercial operator
- Milvus, Elasticsearch, or a second vector DB
- Ingesting the PDF corpus in the public/open cluster
- OCR of the whole corpus
- Adobe `.pdx` / `.idx` catalog import
- CDC from Db2/IMS (later; Splunk + manuals first)
- Public Routes to Qdrant
- Fine-tuning an LLM on the manuals

### 1.3 Hard legal / repo rules

Public GitHub is **outside the enterprise**. IBM documentation terms allow in-enterprise copies; they do **not** allow distributing those publications or derivatives outside the enterprise.

**Never commit:**

- PDFs, HTML dumps, Redbooks, "cleaned" markdown of manuals
- Qdrant snapshots, embeddings, chunk JSON that can reconstruct manuals
- Production JCL, customer configs, internal runbooks
- Registry credentials, API keys, TLS material, real storage class names

**Allowed:** Helm values with placeholders, ingest/query code, tests with *synthetic* IBM-shaped fixtures (`SA22-0000-00`, fake `IEA500I` text), nominative use of product names in READMEs.

Corpus stays on enterprise disk. Ingest runs only after Qdrant is healthy in the air-gap.

---

## 2. System context

```
                    ┌─────────────────────────────────────────┐
  Connected LAN     │  Public GitHub  (this repo)             │
                    │  chart, Makefile, ingest, retrieval     │
                    └───────────────┬─────────────────────────┘
                                    │ sneaker-net / diode
                                    ▼
┌──────────────────────────────────────────────────────────────────┐
│ Air-gapped OpenShift  (other team owns cluster + vLLM/LiteLLM)   │
│                                                                  │
│  ┌────────────┐  gRPC/HTTP   ┌─────────────┐  OpenAI-compat     │
│  │ Ingest Job │─────────────▶│   Qdrant    │◀──── Agent API     │
│  │ (CronJob)  │  upsert      │  3 replicas │                    │
│  └─────▲──────┘              └──────▲──────┘                    │
│        │ PDF on PVC / NFS RO        │ query                     │
│  ┌─────┴──────┐              ┌──────┴──────┐                    │
│  │ Corpus PVC │              │ Agent svc   │                    │
│  │  ~100 GB   │              │ FastAPI     │                    │
│  └────────────┘              └──────┬──────┘                    │
│                                     │                           │
│                    ┌────────────────┼─────────────┐             │
│                    ▼                ▼             ▼             │
│              vLLM embeddings   LiteLLM/vLLM    Splunk           │
│              (dense only)      reasoning LLM   (REST/SPL)       │
└──────────────────────────────────────────────────────────────────┘
```

**Trust boundary:** nothing in the cluster may reach the public internet. All images, wheels, and the BM25 model weights are in the internal registry / ingest image.

**Ownership boundary:**

| You own | Other team owns | You request |
|---|---|---|
| This repo, Helm values, ingest, agent API, collection schema | Cluster, SCC, ingress, vLLM, LiteLLM, maybe GitLab runners | Namespace, RWO StorageClass, pull secret, registry path, embedding model name + dim, reasoning model name |

Do not block on registry hostname. Use `REGISTRY_INTERNAL`, `PULL_SECRET`, `STORAGE_CLASS` placeholders until they exist.

---

## 3. Target architecture (runtime)

### 3.1 Namespaces and workloads

Recommended namespace: `mainframe-rag` (request; do not assume you can create it).

| Workload | Kind | Replicas | Notes |
|---|---|---|---|
| `qdrant` | Helm release `qdrant/qdrant` | 3 | Cluster mode, P2P 6335, HTTP 6333, gRPC 6334 |
| `rag-agent` | Deployment | 2 | FastAPI; no GPU |
| `ingest` | CronJob / Job | 1 | CPU-heavy; local SSD / RWO work volume |
| `bm25-weights` | baked in ingest + agent images | — | FastEmbed `Qdrant/bm25`; no runtime download |

Network: ClusterIP only, no Route to Qdrant. NetworkPolicy intent: agent + ingest may reach Qdrant 6333/6334; only agent may reach vLLM and Splunk; no ingress from other namespaces unless the cluster team requires a mesh.
Enforcement today is ClusterIP + namespace isolation. Dedicated NetworkPolicy manifests were removed from the repo (no deploy path ever applied them) and remain a platform-team follow-up.

### 3.2 Qdrant on OpenShift (pin)

| Artifact | Pin |
|---|---|
| App image | `docker.io/qdrant/qdrant:v1.19.0-unprivileged` (digest-pin after first pull) |
| Chart | vendored `helm pull qdrant/qdrant` `.tgz` in repo; never `helm repo add` in the air-gap |
| SCC | `restricted-v2`; `useUnprivilegedImage: true` |
| Storage | RWO **block** ≥ 500Gi data + 500Gi snapshots; **not NFS, not S3** |
| Service | ClusterIP; no Route |
| Auth | API key + read-only API key from SealedSecret / cluster secret (not git) |

OpenShift values (placeholders only in git): see `overlays/openshift/values.yaml`.

**Do not** ingest the 100 GB corpus on a connected cluster and ship the PVC. Indexes are storage-class and UID/fsGroup specific. Rebuild collections in the air-gap.

### 3.3 Air-gap bundle (connected host → cluster)

Connected build (RHEL/UBI host):

1. `helm pull qdrant/qdrant --destination charts/`
2. `skopeo copy docker://docker.io/qdrant/qdrant:v1.19.0-unprivileged docker-archive:qdrant-v1.19.0-unprivileged.tar`
3. Record digest in `images.txt`
4. Prefer `oc mirror --v2` with `additionalImages` so the cluster gets IDMS/ITMS YAML it already understands
5. Pack: chart tgz + values + image archive or `mirror_seq*.tar` + git bundle
6. Also vendor: ingest/agent images (UBI), Python wheelhouse, FastEmbed BM25 weights

Air-gap import: `oc mirror --from file://… docker://${REGISTRY_INTERNAL}` **or** `skopeo copy docker-archive:…`, apply IDMS, `helm upgrade -i qdrant charts/qdrant-*.tgz -n mainframe-rag -f overlays/openshift/values.yaml`.

Accept: pods `restricted-v2`, PVCs Bound, `/readyz` on 6333, P2P 6335 among 3 replicas.

---

## 4. Data and retrieval design

### 4.1 What a manual is

Born-digital FrameMaker/DITA PDFs. **Ignore `.pdx` / `.idx`** (Adobe Reader catalogs; not embeddings, not a public format we parse).

Extract instead:

| Signal | Where | Payload / use |
|---|---|---|
| Document number | Filename, title page, header: `SA22-7592-05`, `GC28-1910-13`, `SC34-xxxx-nn` | `doc_id`; filter; citation |
| Product / version | Title + first 4 pages (`z/OS V2R5` → `2.5`, `z/OS 3.1` → `3.1`) | `product`, `version` filters |
| Bookmark tree | `doc.get_toc()` | `heading_path`; chunk boundaries |
| Printed page | `page.get_label()` | citation `p. 3-17`, not PDF index 42 |
| Message IDs | Body: `IEA500I`, `CSV003I`, `IEC333I` | keyword payload; Splunk join |
| Members | `IEASYSxx`, `PROGxx` | keyword payload |
| Vendor | path/title: IBM default; Broadcom/CA, BMC, Precisely | `vendor` |

Skip by bookmark title: Notices, Trademarks, Reader's Comments, Bibliography, copyright. Skip early Contents/Figures/Tables as *front matter* (do not skip a chapter named "Contents" in the middle of a book).

Strip running headers/footers by frequency (line appears on ≥35% of sampled pages) so embeddings are not "© IBM Corp."

### 4.2 Chunk contract

One Qdrant point = one chunk.

```
chunk_id = sha256(f"{doc_id}|{heading_path}|{page_start}|{ordinal}")
```

Idempotent upserts. New edition (`SA22-7592-04` vs `-05`) is a **new** `doc_id`. If `sha256` of the PDF changed for the same `doc_id`, delete by `doc_id` then re-upsert.

Chunking:

1. Map each bookmark to `[start, next_same_or_higher)`
2. Concatenate stripped page text in that range
3. If section > 6000 characters (~800 tokens), split on blank lines with 400-char overlap
4. Classify: `message` (starts with `XXXnnnY`), `syntax` (box-drawing / `::=` / `<parm`), `table`, else `narrative`

**Embed this string, not the body alone:**

```
{product} {version} {doc_id}
{title}
{heading_path}
{body}
```

### 4.3 Collection `mainframe_manuals`

Create payload indexes **before** load. Unindexed filters become scans.

- `vectors.dense`: size=`${DENSE_DIM}`, Cosine, on_disk, HNSW m=16 ef_construct=128, int8 scalar quant (always_ram)
- `sparse.bm25`: modifier=idf, on_disk
- payload indexes keyword: vendor, product, version, doc_id, chunk_type, message_ids, members, sha256; integer: page_start
- `on_disk_payload`: true

`${DENSE_DIM}` **must** equal the vLLM embedding model. Wrong size fails create/upsert. Until the other team names the model, keep dim in config (`DENSE_DIM`, `EMBED_MODEL`, `EMBED_BASE_URL`). Do not hardcode a model name in collection create.

### 4.4 Ingest pipeline (air-gap Job)

```
walk *.pdf only  ──► skip .pdx/.idx
                 ──► sha256  ──► skip if same doc_id+hash already in Qdrant
                 ──► PyMuPDF: metadata, toc, page labels, text
                 ──► chrome strip, outline sections, classify
                 ──► dense = POST ${EMBED_BASE_URL}/embeddings  (vLLM, OpenAI-compat)
                 ──► sparse = FastEmbed Qdrant/bm25  (local weights)
                 ──► upsert batch 64
```

Implementation modules (do not invent a framework): see `src/mainframe_rag/ingest/`.

Concurrency: process pool, **one PDF per worker**, `workers = CPU-1`, files on local SSD. OCR **off** unless `len(page.get_text()) < 40` and page is not blank.

Do not use Qdrant Cloud `Document(model=...)` inference. Embed in-process against internal vLLM.

### 4.5 Query path

Identifier-aware hybrid search. Filters applied **in prefetch**, not after ANN.

```
user / Splunk text
  → extract doc_id, message_ids, members, optional product/version from agent context
  → Filter must: product, version, doc_id, message_ids (when present)
  → prefetch dense limit 40 + prefetch bm25 limit 40
  → RRF  (weights [1,3] if identifiers present else [1,1]; k=2)
  → return top 8 with citation fields
```

Citation format the agent **must** emit:

```
SA22-7592-05 z/OS MVS Initialization and Tuning Reference, IEASYSxx > LFAREA, p. 1-17
```

Always pass `product` / `version` from sysplex context when known. Identifier-heavy queries (message IDs, form numbers, members) weight BM25 up. Natural language weights equal until an eval set exists; then tune RRF weights on a train/val split. Do not hand-tune without measurement.

### 4.6 Agent API (v1)

FastAPI service, in-cluster only.

| Method | Path | Contract |
|---|---|---|
| GET | `/healthz` | Qdrant `/readyz` + embed endpoint probe |
| POST | `/v1/search` | `{query, product?, version?, limit?}` → hits with `cite`, `heading`, `text`, `score` |
| POST | `/v1/answer` | `{query, product?, version?, splunk_context?}` → reasoning model with retrieved chunks; returns `answer`, `citations[]`, optional `script` |

`/v1/answer` **must** use the reasoning/thinking model. `/v1/search` does not call an LLM.

Prompt contract for `/v1/answer`:

- System: you are a mainframe operations expert; only assert what citations support; if manuals disagree by version, say so; scripts are examples, not production-ready without review.
- User: question + optional Splunk snippet + numbered retrieved chunks with cite headers.
- Model output: answer, then `Citations:` list in the format above. If it proposes JCL/REXX, keep it in a fenced block tied to a citation.

Splunk: the agent does **not** replace Splunk. Optional `splunk_context` is a short error/job snippet the caller already fetched. v1 does not query Splunk itself unless a later ADR says otherwise. Join key is `message_ids`.

---

## 5. Software architecture (code)

### 5.1 Package layout

```text
src/mainframe_rag/
  ingest/ ...
  retrieve/
    query.py          # parse_query, search()
    filters.py
  agent/
    app.py            # FastAPI
    answer.py         # LiteLLM/vLLM chat
    cites.py          # enforce citation shape
  config.py           # env: QDRANT_URL, API keys, EMBED_*, DENSE_DIM, MODEL_REASONING
tests/
  fixtures/synthetic/SA22-0000-00_outline.pdf   # tiny generated PDF, not a real manual
```

Language: Python 3.14 (GIL build; no free-threading). Pins in `requirements.lock.txt`. Ingest image installs from `/wheelhouse --no-index`.

Libraries (allowed): `pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx2`, `fastapi`, `pydantic-settings`. Do not pull LangChain / LlamaIndex for v1.

### 5.2 Config (env)

See `airgap.env.example`. Fail fast at startup if `DENSE_DIM` is unset when talking to Qdrant.

### 5.3 Tests agents must land before "ingest real PDFs"

- Synthetic PDF with outline, `SA22-0000-00` on page 1, a fake `IEA500I` section, printed labels `1-1`
- Parser extracts doc_id, heading_path, page_label, message_ids
- Walker ignores `.pdx`
- `chunk_id` stable across two runs
- Collection create includes all payload indexes
- Search with `IEA500I` in query applies `message_ids` filter (unit test with mocked client)
- `/v1/answer` refuses to call a non-reasoning model (config assertion)

No real IBM PDF in CI.

### 5.4 Implementation order (agents)

Work in this order. Later steps assume earlier acceptance checks.

1. **Repo skeleton** — Makefile, `airgap.env.example`, `images.txt`, this doc, license Apache-2.0.
2. **OpenShift package** — vendored chart, values with placeholders, `ImageSetConfiguration`, unprivileged image pin. Dry-run `helm template`. No cluster required.
3. **Ingest core** — parse/chunk/classify on synthetic PDF; inventory JSONL; no Qdrant.
4. **Qdrant IO** — `ensure_collection`, upsert, delete-by-doc; Qdrant container in CI **only** with synthetic data.
5. **Retrieve** — hybrid query helper, identifier parse, citation formatter.
6. **Agent API** — `/healthz`, `/v1/search`, `/v1/answer` stub (search + fake LLM in tests).
7. **Containerfiles** — UBI, non-root, wheelhouse, BM25 weights copied in.
8. **Air-gap Makefile targets** — `make airgap-pack` on connected; `make airgap-load airgap-deploy airgap-ingest airgap-smoke` inside the gap (`scripts/airgap/`, issue #15).
9. **Cluster bring-up** — with the other team: StorageClass, pull secret, registry, embedding dim.
10. **Pilot ingest** — 5–10 real manuals **inside the enterprise**, eval 30 questions (message ID, parm name, conceptual), then full 100 GB.

Do not start step 10 from a public runner.

### 5.5 Observability

- Ingest: JSON logs per file (doc_id, pages, chunks, seconds, skip/upsert)
- Qdrant: scrape existing metrics if the cluster has Prometheus; otherwise request a scrape policy from the platform team (no NetworkPolicy manifests ship in this repo — see section 3.1)
- Agent: request_id, query_kind (`identifier`|`nl`), hit count, embed_ms, qdrant_ms, llm_ms
- No query text in logs if it might contain production dump content; log hashes / message IDs only

### 5.6 Risks and decisions (locked unless an ADR says otherwise)

| Decision | Choice | Why |
|---|---|---|
| Vector DB | Qdrant OSS Helm | Single image, air-gap, filtered ANN, hybrid in one process |
| Image | `*-unprivileged` | OpenShift SCC |
| Storage | RWO block 500Gi+ | Qdrant needs POSIX; NFS is not acceptable |
| Parser | PyMuPDF | Speed on 100 GB born-digital PDFs |
| Acrobat catalogs | Ignore | `.pdx` is not a RAG index |
| Embeddings | Internal vLLM | Air-gap; dim from owning team |
| Sparse | Local BM25 FastEmbed | Message IDs / form numbers |
| Fusion | RRF | Default without eval set |
| LLM for answers | Reasoning model only | User requirement: citations + scripts |
| Corpus in git | Never | Copyright / enterprise boundary |
| Splunk | Context in, not crawl in v1 | Splunk remains system of record for events |

### 5.7 Open questions (do not block 1–8)

1. Embedding model name and `DENSE_DIM`
2. Reasoning model name on LiteLLM
3. Namespace, StorageClass, pull secret, registry hostname
4. Whether ingest may mount the corpus as NFS **read-only** (Qdrant data still block RWO)
5. GitLab vs GitHub Actions for the connected build; air-gap runners are internal-registry only

When an open question is answered, put it in `airgap.env.example` comments and a one-line ADR under `docs/adr/`.

---

## Appendix A — Regex (shared)

Keep these in one module imported by ingest and retrieve: `src/mainframe_rag/regexes.py`.

## Appendix B — Acceptance for "v1 done"

- `helm template` renders 3-replica unprivileged StatefulSet
- Synthetic ingest → search for `IEA500I` returns the synthetic chunk with cite `SA22-0000-00 … p. 1-1`
- Agent `/v1/answer` includes at least one citation in the required format
- `make airgap-pack` produces `dist/qdrant-pdf-rag-<sha>.tar`: git bundle (chart, values, code) + image archives + MANIFEST with member checksums
- README states: no manuals in this repository; ingest inside the enterprise only
