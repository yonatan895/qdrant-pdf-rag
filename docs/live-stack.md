# Live-stack runbook and verification ladder

How to bring up the full local stack, prove it is healthy, and run the
mandatory pre-push battery on it. Normative: `AGENTS.md` requires this
ladder before every non-docs push; this file is the procedure.

## 1. Bring-up order

Start Qdrant first, then embed, then reasoning. Each step has a health
proof — do not proceed past a failed proof.

```sh
# Qdrant (docker, port 6333). The sim container is ephemeral (--rm):
# see §4 before rebooting or doing GPU work.
curl -s -m 5 http://127.0.0.1:6333/collections | head -c 200

# Embed server (docker via Budget launcher, port 8001)
make local-vllm-embed
curl -s -m 10 http://127.0.0.1:8001/v1/models | head -c 200

# Reasoning server (docker via Budget launcher, port 8000)
make local-vllm
curl -s -m 10 http://127.0.0.1:8000/v1/models | head -c 200
```

## 2. Environment block

`make eval` and the eval scripts read `Settings` from the environment —
the Makefile does **not** set embed coordinates, so export them in the
same shell (never globally, never committed):

```sh
export EMBED_MODE=vllm
export EMBED_BASE_URL=http://127.0.0.1:8001/v1
export EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B   # fully qualified: the short id 404s
export DENSE_DIM=1024
export QDRANT_URL=http://127.0.0.1:6333
export QDRANT_COLLECTION=mainframe_manuals
export RERANK_BASE_URL=http://127.0.0.1:8002/v1   # only when a reranker is served
```

Model ids must be fully qualified (`Qwen/Qwen3-Embedding-0.6B`, not
`Qwen3-Embedding-0.6B`): vLLM answers the short id with 404 and the eval
fails every query.

## 3. Verification ladder (mandatory before every non-docs push)

Run top to bottom. Each rung states its green condition — a red rung
stops the push, no exceptions.

1. `make check` — ruff, mypy, unit suite. Green: all clean.
2. `make gate-l1` — L1 retrieval gate on the ephemeral simulator. Green: exit 0, 0 regressions.
3. Fresh-ingest `make eval-paraphrase` — re-ingest the paraphrase corpus into a scratch collection, then evaluate. Green: exit 0 reproducing `evals/baseline-paraphrase.json` exactly (proves the change moves nothing it should not).
4. `make sim` — integration tier. Green: all pass, **0 skipped** (a skip fails the job; a skip on missing local weights means symlink or rebuild them, never ignore it).
5. `make eval EMBED_MODE=vllm` (with the §2 block exported) — Green: 0 query failures; numbers at or above the mode-keyed baseline.
6. Live agent probes — `make run-agent` (or equivalent uvicorn) against the real stack, then: `/healthz` ok; one legit `/v1/answer` (grounded, ≥1 citation); one trap/injection `/v1/answer` (must refuse); one overlong query (422 `invalid_request`). Green: all four behave.
7. Feature A/B numbers in the PR body — any retrieval/ranking change ships measured deltas (2×2 where applicable: off/on × base/context), per-query attribution for every moved query, must_not hard-zero.

## 4. Qdrant persistence (read before rebooting or juggling GPUs)

The local Qdrant container is ephemeral (`--rm`, no volume): a reboot,
daemon restart, or container crash **destroys every collection**. Before
any of those, snapshot to persistent disk and restore-test one collection:

```sh
mkdir -p /home/yonti/qdrant-snapshots
# per collection:
curl -s -X POST http://127.0.0.1:6333/collections/<name>/snapshots
curl -s -o /home/yonti/qdrant-snapshots/<name>.snapshot \
  http://127.0.0.1:6333/collections/<name>/snapshots/<snapshot-file>
```

Restore-test (fresh container, throwaway port, verify point count, remove it):

```sh
docker run -d --name qdrant-restore-test -p 6334:6333 docker.io/qdrant/qdrant:v1.19.0-unprivileged
curl -s -X POST "http://127.0.0.1:6334/collections/<name>/snapshots/upload?priority=snapshot" \
  -F "snapshot=@/home/yonti/qdrant-snapshots/<name>.snapshot"
curl -s http://127.0.0.1:6334/collections/<name>   # expect status green + full points_count
docker stop qdrant-restore-test && docker rm qdrant-restore-test
```

An untested backup is not a backup. Re-snapshot after any ingest that must survive.

## 5. GPU rules (8 GB box)

- Co-residency budget is tight by design (reasoning 0.64 + embed 0.33). Never launch a third server alongside both: stop `:8000` before serving anything else (e.g. the reranker on `:8002`), restore afterwards with `make local-vllm`, verify `/v1/models`.
- Reranker recipe (vLLM pooling, offline weights): mirror the embed container flags with `--runner pooling` and **no** `--convert` (v0.28 auto-detects sequence-classification); serve `/v1/score`; smoke-test discrimination (relevant vs irrelevant score gap, correct direction) before any A/B.
- Crashed vLLM inits can leak VRAM across container restarts; repeated launch failures with shrinking headroom mean stop retrying — a host reboot is the reset. Do §4 first.
- `nvidia-smi` is the source of truth for free VRAM, not arithmetic.

## 6. Shell safety

- Never switch branches while an ingest runs: parse workers spawn fresh processes that re-import the working tree — a mid-run switch mixes code versions across documents or crashes workers. Finish or kill the ingest first.
- Never `pkill -f` with a pattern matching your own command line (the shell kills itself); use the `[u]` trick (`pkill -f "[u]vicorn.*8087"`) or port-based kill.
- Agent stdout goes to a file, never a pipe, under load (an unread pipe wedges every request).

## 7. Real-corpus etiquette

Vendor corpora (e.g. `/home/yonti/*_pdfs`) are read in place — never copied into the repo, never committed, never quoted at length outside the local machine. Ingest progress/inventory files go to `/tmp/opencode/` (persistent local disk), never the repo. Resume is the norm: re-running ingest skips completed docs (inventory + Qdrant sha check); transient embed timeouts under batch pile-up are retried, not debugged as parse bugs.
