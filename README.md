# mainframe-rag

Citation-first expert mainframe agent: hybrid retrieval over ~100 GB of
IBM-style manuals (IBM, Broadcom/CA, BMC, Precisely) on **air-gapped OpenShift**,
answering operational questions with citations — doc number, title, heading
path, printed page label — plus optional JCL/REXX/operator steps from a
reasoning model.

Design and architecture: **[docs/architecture.md](docs/architecture.md)** (source of truth).

## What this repo contains

| Path | What |
|---|---|
| `charts/qdrant-*.tgz` | Vendored Qdrant Helm chart (Apache-2.0); never `helm repo add` in the air-gap |
| `overlays/openshift/values.yaml` | OpenShift values: 3 replicas, unprivileged, RWO block, ClusterIP only |
| `deploy/` | NetworkPolicies, ingest CronJob, agent Deployment |
| `oc-mirror/` | `ImageSetConfiguration` for disconnected mirroring |
| `src/mainframe_rag/ingest/` | PDF walk, IBM-style parse, chrome strip, chunk, classify, embed, Qdrant IO |
| `src/mainframe_rag/retrieve/` | Identifier-aware hybrid search (dense + BM25, RRF), filters |
| `src/mainframe_rag/agent/` | FastAPI `/healthz`, `/v1/search`, `/v1/answer` |
| `images/` | UBI Containerfiles (non-root, wheelhouse + BM25 weights baked in) |
| `tests/` | Synthetic IBM-shaped fixtures only (`SA22-0000-00`, fake `IEA500I`) |

## Legal / repo rules (hard)

- **No manuals in this repository.** No PDFs, HTML dumps, Redbooks, or
  "cleaned" markdown of IBM / Broadcom / BMC / Precisely content. The corpus
  stays on enterprise disk.
- **Ingest inside the enterprise only.** Never ingest real manuals from a
  public runner or connected cluster; ingest runs after Qdrant is healthy in
  the air-gap.
- Never commit: Qdrant snapshots/embeddings/chunk dumps, production JCL,
  credentials, TLS material, real storage class names. Placeholders only.
- Tests use synthetic content we generated; product names appear nominatively.

## Quickstart (connected host)

```bash
make venv            # .venv + deps
make check           # ruff + mypy + pytest
make helm-lint       # vendored chart + OpenShift values
make helm-template   # renders 3-replica unprivileged StatefulSet
```

## Air-gap workflow

Connected host (see Makefile):

```bash
make pull-chart pull-images wheelhouse bm25-weights   # gather artifacts
make pack                                             # bundles/: chart, images, git bundle, wheelhouse, BM25 weights
```

Disconnected host:

```bash
make load-images   # skopeo docker-archive -> ${REGISTRY_INTERNAL}
make helm-apply    # helm upgrade -i qdrant -f overlays/openshift/values.yaml
```

Acceptance after bring-up: pods run under `restricted-v2`, PVCs Bound (RWO
block), `/readyz` OK on 6333, P2P 6335 among 3 replicas.

## Configuration

Copy `airgap.env.example` → `airgap.env`. `DENSE_DIM`, `EMBED_MODEL`,
`LLM_MODEL_REASONING`, registry, and StorageClass come from the owning teams
(open questions in `docs/architecture.md` §5.7 — none block steps 1–8).
The agent fails fast if `DENSE_DIM` is unset or disagrees with the collection.

`EMBED_MODE=hash` selects a deterministic in-process embedder (no network, no
weights, lexical-only retrieval). **CI/dev only** — connected E2E uses it;
never set it in production.

## Connected E2E (GitHub Actions)

`.github/workflows/e2e.yml` builds both Containerfiles, pushes `ghcr.io/<owner>/qdrant-pdf-rag-{ingest,agent}:<sha>` on `main`/dispatch, deploys a 1-replica Qdrant (`overlays/ci/values.yaml`) plus agent and one-shot ingest Job into an ephemeral `rag-ci-<sha>` lab-OpenShift namespace, ingests **generated demo PDFs only** (`EMBED_MODE=hash`), runs two search smokes, and deletes the namespace `if: always()`.

Secrets (create in GitHub repo settings; values never in git):

| Secret | Purpose |
|---|---|
| `OPENSHIFT_SERVER` | Lab OpenShift API URL |
| `OPENSHIFT_TOKEN` | Namespace-scoped, short-lived service-account token |

Unset secrets → the e2e job is skipped (fork PRs only build images, no push).

Notes:

- **GHCR packages must be public** (repo → Packages → package settings →
  change visibility) so the lab cluster can pull anonymously. Alternative:
  create an `imagePullSecret` from a fine-grained PAT via `oc create secret`
  in the workflow (token stays in the cluster, never in git).
- The workflow logs in with `--insecure-skip-tls-verify=true` — **lab only**;
  a production cluster would use a trusted CA.
- The readyz probe pod uses `curlimages/curl` from Docker Hub — allowed on the
  connected lab cluster only, never in the air-gap.
- Image refs are exactly `ghcr.io/<owner>/qdrant-pdf-rag-{ingest,agent}:<sha>`
  across `docker tag`, `docker push`, and the kustomize overlay sed.

## For agents

Qdrant-specific guidance (hybrid fusion, HNSW, quantization, deployment, client usage) is vendored under `.agents/skills/` (pinned; see `vendor/qdrant-skills.sha`). Read the matching skill before touching collections, retrieval, or Qdrant config — but this repo's AGENTS.md product rules win over any skill.

## Library scope

`pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx`, `fastapi`,
`pydantic-settings`. No LangChain / LlamaIndex in v1.
