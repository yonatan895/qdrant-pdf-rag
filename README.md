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

The air-gap never builds images (issue #15): connected `main` is the only
image factory, and e2e tags app images with the full git SHA.

Connected host:

```bash
make airgap-pack   # dist/qdrant-pdf-rag-<full-sha>.tar + .tar.sha256
                   # tarball = git bundle + image archives + MANIFEST + member checksums
```

Sneakernet the `.tar` and its `.tar.sha256` into the gap. On the disconnected
host, verify the digest **before** unpacking, then clone the bundled repo:

```bash
sha256sum -c qdrant-pdf-rag-<full-sha>.tar.sha256
# verify member SHA256SUMS inside the tarball after unpack (load.sh does this)
git clone repo.bundle repo && cd repo
```

From inside that clone:

```bash
make airgap-load     # verify member checksums, load images, push to ${INTERNAL_REGISTRY}
make airgap-deploy   # Qdrant (vendored chart, prod sizing) + agent prod overlay; waits for Ready
make airgap-ingest   # one-shot ingest Job against the caller-supplied corpus PVC
make airgap-smoke    # /v1/search non-empty hit check (skips when nothing ingested yet); --expect variant printed at the end
```

The scripts are POSIX sh under `scripts/airgap/` and fail closed (missing
env, `EMBED_MODE=hash`, NFS-looking StorageClass, SHA mismatches). Acceptance
after bring-up: pods run under `restricted-v2`, PVCs Bound (RWO block),
`/readyz` OK on 6333, P2P 6335 among 3 replicas.

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

## Air-gap

Three commands, one env file. **The air-gap never builds** — connected `main` is the only image factory.

1. **Connected host** (after `main` is green):

   ```bash
   git clone https://github.com/yonatan895/qdrant-pdf-rag && cd qdrant-pdf-rag
   git checkout <main-sha>     # the SHA whose GHCR tags exist
   make airgap-pack            # -> dist/qdrant-pdf-rag-<sha>.tar + SHA256SUMS
   ```

2. **Transfer** `dist/qdrant-pdf-rag-<sha>.tar` + `dist/qdrant-pdf-rag-<sha>.tar.sha256` to the bastion (USB / approved drop), verify the tarball digest, then unpack and clone from the bundle (the bundle's HEAD is the packed SHA):

   ```bash
   sha256sum -c qdrant-pdf-rag-<sha>.tar.sha256
   tar xf qdrant-pdf-rag-<sha>.tar
   git clone repo.bundle qdrant-pdf-rag && cd qdrant-pdf-rag
   ```

3. **Air-gapped bastion** (`oc`, `helm`, `skopeo`; no internet):

   ```bash
   cp airgap.env.example airgap.env   # edit locally; never commit
   make airgap-load                   # push the 3 images to $INTERNAL_REGISTRY (same names, SHA tags)
   make airgap-deploy                 # vendored chart with PROD values + agent; waits Ready
   ```

   Required in `airgap.env`: `INTERNAL_REGISTRY`, `NAMESPACE`, `STORAGE_CLASS` (RWO block — NFS is refused), `EMBED_MODEL`, `DENSE_DIM`, `VLLM_BASE_URL` (in-cluster inference; this repo does not install vLLM).

4. Optional, once a corpus PVC exists (no PDFs in git): `make airgap-ingest CORPUS_PVC=<pvc>`, then `make airgap-smoke`.

Details: prod Qdrant stays 3 replicas / 500Gi / unprivileged (never shrunk); refs stay `qdrant-pdf-rag-{ingest,agent}:<full-git-sha>` across pack/load/apply — `IMAGE_SHA` must be exactly the GHCR tag e2e.yml pushed; scripts refuse `EMBED_MODE=hash` and NFS storage classes; `AGENT_ROUTE=true` opts into an edge Route to the agent. `oc-mirror/imageset-config.yaml` remains the optional way to refresh base/Qdrant pins — not the happy path.

## For agents

Qdrant-specific guidance (hybrid fusion, HNSW, quantization, deployment, client usage) is vendored under `.agents/skills/` (pinned; see `vendor/qdrant-skills.sha`). Read the matching skill before touching collections, retrieval, or Qdrant config — but this repo's AGENTS.md product rules win over any skill.

## Developer tools & reporting

- **Interactive query inspection:** `make query-demo` (or `QUERY="..." make query-demo`).
- **Evaluation reports & comparison:** `make eval-report`, `make eval-html`, `make eval-compare`.
- **Benchmark reports & comparison:** `make bench-report`, `make bench-html`, `make bench-compare`.
- HTML artifacts generated by `*-html` targets are 100% self-contained for disconnected environments.

## Library scope

`pymupdf`, `qdrant-client`, `fastembed` (sparse only), `httpx2`, `fastapi`,
`pydantic-settings`. No LangChain / LlamaIndex in v1.
