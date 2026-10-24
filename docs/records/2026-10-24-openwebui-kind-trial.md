# Open WebUI offline Kind trial (record, interim)

Status: **interim, run 1 of the #1073 qualification** (tracker #1070; ADR-0007 #1080 not accepted). Run
2026-10-24 19:53 to 20:24 IDT on base `fcc843e`. Trial-only: no chart, `images.txt`, Taskfile or production
manifest change. Ids, counts and states only; evidence stays outside git in
`~/.local/share/mainframe-rag/owui-trial-1073/` (secrets and kubeconfig deleted after teardown).

## Verdict so far

No no-go condition was hit. Two findings need a decision before go: API file upload is not closed by
configuration (V1), and the default Ollama probe made egress attempts until an explicit setting removed it (R2).
Not yet run: (h), the live-admin-edit residual of (i), the retention purge (S2), console alongside (g), a
browser render of the chips, and the licence inventory record. Next commands are at the end.

## Identity

| Item | Value |
|---|---|
| Open WebUI base | `ghcr.io/open-webui/open-webui:v0.12.3-slim`, index `sha256:5adfb0355bbe4a5860c0ba9482cc0b40f573de3749a21a64cf4789391a14fffc`, amd64 `sha256:912c133d…52f5f`, revision `91ea4e34` (tag v0.12.3), created 2026-10-16 |
| Derived image | `scripts/ci/openwebui-trial/Containerfile.openwebui`: one ownership/permission layer (126 MB), image id `sha256:bf4e04373ceab05bfff212a51f195d8afd3c0e8ecdb55b1a7d9ce528df0c002f` (local build, not pushed). Kept out of `images/` so the production Containerfile base-pin check never sees a trial image |
| Agent image | `images/Containerfile.agent` at `fcc843e`, fresh runtime wheelhouse; id `sha256:f8e3b0ff8a1d53600ef8b6d2fa271baf80ca2a37c2701cf228ede3c3b64f5ea9` |
| Docs Pipe | `integrations/openwebui/docs_pipe.py` at `feat/1072-docs-pipe` `8afdd40` (draft #1081), sha256 `f9af2e9e…66e6` |
| Test IdP | Dex v2.46.0-distroless (index `sha256:8ba378a1…3589`, amd64 `sha256:a9492ee2…3912`, Apache-2.0, 17 days old). Chosen over Keycloak: about 30 MB of memory against roughly 700 MB on a shared 16 GB host, a single static config, and static users with a `groups` claim since v2.45. Test-only; never shipped |
| Other | Qdrant pin (amd64 `sha256:390ca163…e633` of the `images.txt` index); `scripts/mock_vllm.py` from a ConfigMap (embeddings and reasoning, **no hash mode**); Kind v0.33.0, `kindest/node:v1.37.0@sha256:a1ed56cf…`, cluster `owui-trial` (deleted) |

## Checks

| Check | Observed | Result |
|---|---|---|
| (a) random UID | Pod ran as uid 1000700172, gid 0, under PSA `restricted`; `runAsNonRoot`, drop ALL, RuntimeDefault, no privilege escalation; **readOnlyRootFilesystem true works** (writable: PVC + emptyDir static, `/root`, `/tmp`). The derived layer leaves 0 files outside group 0 and 0 directories without setgid; DATA_DIR, static, HOME and `/tmp` were writable as uid 1000770123:0 | PASS |
| (b) egress | nftables prerouting set (pre-NAT, pre-policy) of every new flow. First start: DNS, Dex 5556, agent 8080, **plus `host.docker.internal` 11434 and 12434** (the Ollama port probe in `config.py`, which runs even with `ENABLE_OLLAMA_API=false`). With `OLLAMA_BASE_URL=http://127.0.0.1:9` the restarted pod opened only DNS and agent 8080 | PASS with the extra setting (R2 finding) |
| (c) vector store | DATA_DIR held `webui.db` (+wal/shm) and one upload; no vector directory or file; no vector, chunk or collection table; knowledge, memory, note and chat_file rows 0 | PASS for vectors; see V1 finding |
| (d) Docs only | OIDC via Dex: admin (first user) admin, alice `docs-readers`, bob `other-team`; groups JIT-created from the claim. Models: admin `docs`, `hidden_control`; alice `docs`; bob none. bob→docs and alice→hidden: HTTP 400. Login form off; password sign-in 403; signup 403 | PASS |
| (e) source chips | Each chat turn stored one `source` (1 excerpt of 1259 to 1515 chars, 1 metadata entry) and the status events ("Searching…", "Source-matched · 1 citation · Human review required…"). A browser render was NOT RUN (no browser on the host) | PASS at the stored-event level |
| (f) restart | After a Recreate rollout on the RWO PVC: alice's 3 chats, model grants, both Functions and the Pipe hash were all intact, and the next chat succeeded | PASS |
| (i) tasks | Title, tags, follow-up and autocomplete endpoints are refused by the env toggles. `emoji`, `queries` (untyped) and `image_prompt` are **not gated** and reached the Docs Pipe, which answered `Docs question`. Agent `rag_requests_total` = 2 after 2 user turns plus 8 task calls; stored titles stayed "New Chat", follow-ups 0 | PASS (Pipe backstop needed) |
| Pipe asks | `httpx` imports in the image (the Pipe loaded and ran). `__task__` = `emoji_generation`/`query_generation` with `__metadata__.task`; chat calls get `__metadata__` (chat, message, session, user ids…) and an event emitter. Install by API, toggle, valves and `GET` hash re-check: the stored content hash equals the repository file | PASS |
| Refusals | knowledge create 401, memory add 404, retrieval query 403, web search 403, community sharing false. **`POST /api/v1/files/` returned 200** despite `USER_PERMISSIONS_CHAT_FILE_UPLOAD=false`: one 17-byte file stored in `uploads/` and a `file` row (text extracted, no collection) | **FINDING (V1)** |
| (g), (h), S2 purge | NOT RUN in run 1 (session ended). `qualify.py config-set` and `purge.py` are ready | NOT RUN |

## Findings for decision

1. **V1 uploads.** v0.12.3 checks the upload permission in the UI only; the route stores the file. The options are
   to block `/api/v1/files` in front of the pod, to accept it with S2 purging files, or to stop the pilot. A
   maintainer decision is needed.
2. **R2 Ollama probe.** Set an explicit `OLLAMA_BASE_URL` (loopback) in any manifest; the offline flag does not stop it.
3. NetworkPolicy must admit the IdP and DNS as well as the agent: the OIDC code exchange is server-side.
4. `CORS_ALLOW_ORIGIN` defaults to `*` (startup warning); set it to the Route host.

## Resume

```sh
cd dist/worktrees/1073-frontend-trial   # branch fix/1073-frontend-trial
export EVIDENCE=$HOME/.local/share/mainframe-rag/owui-trial-1073 KIND_BIN=$HOME/.config/mainframe-rag/kind-live/bin/kind \
  KUBECTL=$HOME/.config/mainframe-rag/kind-live/bin/kubectl PIPE_FILE=$EVIDENCE/docs_pipe.py
sh scripts/ci/openwebui-trial/run_trial.sh cluster   # needs the images phase or the local images
sh scripts/ci/openwebui-trial/run_trial.sh deploy && sh scripts/ci/openwebui-trial/run_trial.sh audit
```
