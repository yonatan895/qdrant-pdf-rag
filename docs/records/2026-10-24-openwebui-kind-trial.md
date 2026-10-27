# Open WebUI offline Kind trial (record, interim)

> Code note (#1070): the trial scripts this record names (`scripts/ci/openwebui-trial/`) no longer exist; the supported dev environment and acceptance lane live in `scripts/openwebui/` (owner doc: docs/openwebui-dev.md). The record below is unchanged.

Status: **interim, runs 1 and 2 of the #1073 qualification** (tracker #1070; ADR-0007 #1080 was accepted on 2026-10-24, #1089, after run 1 and during run 2; this change stays trial-only).
Run 1: 2026-10-24 19:53 to 20:24 IDT on base `fcc843e`. Run 2: 2026-10-24 22:28 to 22:58 IDT on branch head
`382ffa9` plus this change (tool-server identity probe for #1088, checks g, h, the (i) residual, S2 purge).
Trial-only: no chart, `images.txt`, Taskfile or production manifest change. Ids, counts, states, and claim names and
values from the test IdP only; evidence stays outside git in `~/.local/share/mainframe-rag/owui-trial-1073/`
(secrets, IdP configs and kubeconfig deleted after each teardown).

## Verdict so far

No production no-go condition was hit. The trial is not a clean pass: uploads are not closed by configuration (V1) and
the S2 purge prototype removes the upload rows but not the upload bytes (V1 follow-on); the task-model requirement
is not met as worded (check i, finding 7); source chips are stored-event evidence only (check e); and run 2's model
list and egress report are partial (checks d and b). The run-2 trial-only `tool_probe` Function also contradicts the
only-Docs check (d) and is recorded as a trial contradiction, not a production deployment. Every listed check is now
accounted for, but a browser render of the chips, the ingress-block positive probe and a per-check command/exit-code
log were NOT RUN. For the team tools
(#1075, #1088):
Open WebUI forwards the user's **access token**; the extra-audience and broker token-exchange paths both work with
the test IdP; the refresh path could not be observed because the test IdP wedged.

## Tool-server identity (#1088, run 2)

Setup (`run_trial.sh toolprobe`, `toolprobe.yaml`, `toolprobe_echo.py`): a dummy tool server registered twice in
`TOOL_SERVER_CONNECTIONS`, as MCP (`server:mcp:echo`, streamable HTTP) and as OpenAPI (`server:echo-openapi`), both
`auth_type: system_oauth`, public read grant; `ENABLE_FORWARD_USER_INFO_HEADERS=true`. The echo server logs only
header names, `X-OpenWebUI-*` values, the decoded JWT header and claims and a 12-hex sha256 fingerprint; never the
token. A trial control Pipe (`tool_probe`) calls every tool Open WebUI resolved for the turn (the same callables a
native tool call uses) and reports the key names and fingerprints of the token set Open WebUI holds. alice
(`docs-readers`) signed in through Dex and invoked both tools.

| Item | Observed |
|---|---|
| Which token | The **access token**: the forwarded fingerprint equalled `access_token` in the session's token set (`c9c523477f05`), not `id_token` (`65aa9ae026d0`). Same for MCP and OpenAPI. Source: `utils/tools.py` `build_tool_server_headers` sends `oauth_token['access_token']`. Dex access tokens are ID-token-shaped (they carry `nonce` and `at_hash`), so shape alone does not tell them apart |
| `iss` | `http://dex.owui-trial.svc.cluster.local:5556/dex` |
| `aud` | `"open-webui"` (the Open WebUI client id, a string) |
| `azp` / `client_id` | absent / absent (Dex sets `azp` only with more than one audience) |
| `scope` | absent (Dex puts no scope claim in tokens) |
| `exp` | `iat` + 86400 s (Dex default); Open WebUI's own session is 8 h |
| groups | **present**: `groups: ["docs-readers"]` |
| Other claims | `sub` (Dex-encoded user id + connector), `email`, `email_verified`, `name`, `auth_time`, `jti`, `nonce`, `at_hash`; header `alg: RS256`, `kid` |
| Forwarded headers | `X-OpenWebUI-User-Name`, `-User-Id` (Open WebUI user UUID, not `sub`), `-User-Email`, `-User-Role`, `X-OpenWebUI-Chat-Id`, `X-OpenWebUI-Message-Id`; no groups header. MCP adds `mcp-protocol-version`. The OpenAPI spec fetch carried no `Authorization` and no user headers |
| Extra audience | Dex client `team-gateway` with `trustedPeers: [open-webui]`, Open WebUI `OAUTH_SCOPES` plus `audience:server:client_id:team-gateway`: the forwarded access token had `aud: ["team-gateway", "open-webui"]`, `azp: "open-webui"`; sign-in still worked. One token then carries every listed audience |
| Exchange at the IdP itself | RFC 8693 grant at the trial Dex, client `team-gateway`, `connector_id=local`: HTTP 400 `invalid_request` ("Requested connector does not exist."; Dex log: "connector doesn't implement token exchange"). Dex exchanges only upstream-connector tokens, never its own |
| Exchange at a broker | A second Dex (`dex-broker`) with an OIDC connector to the trial IdP (`getUserInfo: true`): the forwarded access token exchanged by client `splunk-gateway` gave HTTP 200, `iss` = broker, `aud: "splunk-gateway"`, `groups` kept, new `sub`, 24 h. With scope `audience:server:client_id:gitlab-gateway` (trusting `splunk-gateway`): `aud: ["gitlab-gateway", "splunk-gateway"]`, `azp: "splunk-gateway"`. Dex v2.46.0's connector checks the subject token's signature (or calls userinfo) but **not its audience** (`SkipClientIDCheck`), so it accepts any IdP token for any client; a token signed by a rotated key was refused |
| Functions see tokens | Every Function, the Docs Pipe included, may declare `__oauth_token__` and receives the token set with keys `access_token`, `id_token`, `expires_at`, `expires_in`, `issued_at`, `token_type`, `userinfo`. The browser also gets an `oauth_id_token` cookie |
| Expiry, no refresh token | Dex `expiry.idTokens: 4m` (Open WebUI refreshes 5 min before expiry), default scopes: the first chat found "No refresh token available", deleted the session, and the MCP tool was silently dropped (`tools: []`, "No OAuth access token available"). Before alice's older sessions were cleared, Open WebUI fell back to her most recent other OAuth session and forwarded **an earlier sign-in's token** (other `aud`, rotated key) |
| Expiry, `offline_access` | NOT OBSERVED: the refresh grant wedged Dex v2.46.0 (memory and sqlite3 storage; every endpoint stopped answering, process asleep). Open WebUI held the chat for about 300 s, then deleted the session. A test-IdP limitation; the real IdP must be tried |

## Identity

| Item | Value |
|---|---|
| Open WebUI base | `ghcr.io/open-webui/open-webui:v0.12.3-slim`, index `sha256:5adfb0355bbe4a5860c0ba9482cc0b40f573de3749a21a64cf4789391a14fffc`, amd64 `sha256:912c133d…52f5f`, revision `91ea4e34` (tag v0.12.3), created 2026-10-16 |
| Derived image | `scripts/ci/openwebui-trial/Containerfile.openwebui`: one ownership/permission layer (126 MB), image id `sha256:bf4e04373ceab05bfff212a51f195d8afd3c0e8ecdb55b1a7d9ce528df0c002f` (local build on the connected host, not pushed; deployed from the local tag `mainframe-rag/openwebui-trial:0.12.3-slim` with `imagePullPolicy: Never`; the image ID is a local content address, **not a registry-pinned digest**; no build runs in the gap). Kept out of `images/` so the production Containerfile base-pin check never sees a trial image |
| Agent image | `images/Containerfile.agent` at `fcc843e`, fresh runtime wheelhouse; id `sha256:f8e3b0ff8a1d53600ef8b6d2fa271baf80ca2a37c2701cf228ede3c3b64f5ea9` |
| Docs Pipe | Trial source: `integrations/openwebui/docs_pipe.py` at `feat/1072-docs-pipe` `8afdd40` (draft #1081), sha256 `f9af2e9e…66e6`; current `origin/main` is `99ce3e8`, newer than `8afdd40`. The source-chip evidence was produced with `8afdd40` and was not re-run on the merged Pipe |
| Test IdP | Dex v2.46.0-distroless (index `sha256:8ba378a1…3589`, amd64 `sha256:a9492ee2…3912`, Apache-2.0, 17 days old). Chosen over Keycloak: about 30 MB of memory against roughly 700 MB on a shared 16 GB host, a single static config, and static users with a `groups` claim since v2.45. Test-only; never shipped. Run 2 adds a second instance of the same image as the token-exchange broker |
| Licence | The production inventory entries (`image:openwebui`, `image:openwebui-base`) are owned by #1083; this trial carries no duplicate. Upstream LICENSE at tag v0.12.3 sha256 `5f1bd74c…c232` (2819 bytes): BSD-3-Clause terms plus clause 4 (branding unaltered above 50 end users per rolling 30 days). The image carries no LICENSE file. Branding was not altered in the trial. ADR-0007 L1 (maintainer, 2026-10-24) records the owner sign-off for internal use at 100 users or fewer with the branding unmodified, as a maintainer disposition, not a legal approval. The trial image is not in `images.txt`, the chart or a bundle |
| Other | Qdrant pin (amd64 `sha256:390ca163…e633` of the `images.txt` index); `scripts/mock_vllm.py` from a ConfigMap (embeddings and reasoning, **no hash mode**); Kind v0.33.0, `kindest/node:v1.37.0@sha256:a1ed56cf…`, cluster `owui-trial` (deleted after each run) |

## Configuration

`run_trial.sh deploy` renders and applies `scripts/ci/openwebui-trial/stack.yaml`; that file is the settings source
(no admin-UI state survives a restart, check h). It sets `OFFLINE_MODE=true`, the update/version and pip-install
checks off, `ENABLE_COMMUNITY_SHARING=false`, both `*_TRUST_REMOTE_CODE=false`, `ENABLE_SIGNUP=false`,
`ENABLE_LOGIN_FORM=false`, `ENABLE_PASSWORD_AUTH=false`, `JWT_EXPIRES_IN=8h`, `WEBUI_SECRET_KEY` from the
`openwebui-secret` Secret and `ENABLE_PERSISTENT_CONFIG=false`. Built-in RAG, knowledge, memories, notes, web
search, code interpreter/execution and image generation are configured off; code interpreter/execution and both
trust-remote-code flags have no separate behavior test. Title, tags, follow-up and autocomplete generation are
configured off and `TASK_MODEL`/`TASK_MODEL_EXTERNAL` are empty, with check (i)'s residual. The upload
permission is off, but the route still stores an upload: that is the V1 finding, not a settings pass. Runtime
effects and refusals are in (b)-(i) and Refusals; the full env list is the rendered stack.

## Checks

| Check | Observed | Result |
|---|---|---|
| (a) random UID | Pod ran as the run's random UID `1000741351` (`trial-uid.txt`), gid 0, under PSA `restricted` (the Kind stand-in for OpenShift `restricted-v2`; the real SCC is site acceptance); `runAsNonRoot`, drop ALL, RuntimeDefault, no privilege escalation; **readOnlyRootFilesystem true works** (writable: PVC + emptyDir static, `/root`, `/tmp`). The derived layer leaves 0 files outside group 0 and 0 directories without setgid; the separate write test ran as uid `1000770123`:0 (`derived-layer-check.json`) and found DATA_DIR, static, HOME and `/tmp` writable | PASS |
| (b) egress | Run 1, saved artifacts only (`egress-report-1.txt`, `egress-report-2.txt`, `flows-before-fix.txt`, `flows-after-fix.txt`): nftables prerouting set (pre-NAT, pre-policy) of every new flow. First start: DNS, Dex 5556, agent 8080, **plus `host.docker.internal` 11434 and 12434** (the Ollama port probe in `config.py`, which runs even with `ENABLE_OLLAMA_API=false`). With `OLLAMA_BASE_URL=http://127.0.0.1:9` the restarted pod opened only DNS and agent 8080. Run 2's report was **not saved**: `egress_report` reads `$EVIDENCE/owui-pod-ips.txt`, which no committed script writes, and the documented `deploy && audit` order installs the audit after startup; the record's "DNS, Dex 5556, toolprobe 8000, nothing else" line has no run-2 artifact | Run-1 PASS with the extra setting (R2 finding); run-2 **PARTIAL / UNSAVED / NOT REPRODUCIBLE** |
| (c) vector store | DATA_DIR held `webui.db` (+wal/shm) and one upload; no vector directory or file; no vector, chunk or collection table; knowledge, memory, note and chat_file rows 0 | PASS for vectors; see V1 finding |
| (d) Docs only | Run 1, before the #1088 tool probe: OIDC via Dex; admin (first user) admin, alice `docs-readers`, bob `other-team`; groups JIT-created from the claim. Models: admin `docs`, `hidden_control`; alice `docs`; bob none. bob→docs and bob→hidden: HTTP 400 (`step-chat2-refusals.jsonl`). Login form off; password sign-in 403; signup 403. Run 2: after `setup-toolprobe`, alice's model list was `["docs","tool_probe"]` (`step-i-live-edit-3.jsonl`) because the probe Function was granted public read (`qualify.py` `setup-toolprobe`). The "only Docs" result applies to alice in run 1, not every signed-in user or the run-2 probe config | Run-1 PASS for alice; run-2 **CONTRADICTED** (trial `tool_probe` public read) |
| (d2) auth-only reachability | No Ingress or Route in the trial; the `openwebui-ingress` NetworkPolicy admits only the browser pod on 8080. Login form off, password sign-in 403 and signup 403 are recorded in (d). The ingress-block positive probe was **NOT RUN**, and no unauthenticated API probe exists. | **NOT RUN** |
| (e) source chips | Run 1, Pipe `8afdd40`: each chat turn stored one `source` (1 excerpt of 1259 to 1515 chars, 1 metadata entry) and the status events ("Searching…", "Source-matched · 1 citation · Human review required…"). A browser render was **NOT RUN** (no browser on the host), so the stored `source`/`statusHistory` data is not a visual chip proof. Run 2 did not re-check the chips, and the merged Pipe `99ce3e8` was not used. | **PARTIAL / NOT RUN (visual)** — stored-event evidence only |
| (f) restart | After a Recreate rollout on the RWO PVC: alice's 3 chats, model grants, both Functions and the Pipe hash were all intact, and the next chat succeeded | PASS |
| (g) console alongside | Same agent behind both: `/ui` 200 (5098 B), `/ui/healthz` Online, `/ui/chat` 200 with 3 citation markers; then an Open WebUI Docs turn (1 source); then the console again 200. Agent `rag_requests_total`: console 2, chat 1, all `ok`/`accepted` | PASS |
| (h) admin edit vs restart | `ENABLE_PERSISTENT_CONFIG=False`: admin set `JWT_EXPIRES_IN` 8h → 4w (HTTP 200, read back 4w, live); after a restart it read 8h. The `config` table held 0 rows: the edit never reached the database | PASS |
| (i) tasks | Title, tags, follow-up and autocomplete endpoints are refused by the env toggles. `emoji`, `queries` (untyped) and `image_prompt` are **not gated** and reached the Docs Pipe, which answered `Docs question`. Agent `rag_requests_total` = 2 after 2 user turns plus 8 task calls; stored titles stayed "New Chat", follow-ups 0. Run 2 residual: an admin live-enabled title generation; the title call reached the Docs Pipe and got the fixed `Docs question`; pointing `TASK_MODEL_EXTERNAL` at the hidden model gave alice HTTP 400; a restart restored "disabled". (Right after a restart, task calls return 404 until a model list loads the model cache.) | **PARTIAL** — Pipe backstop only; no separate task model |
| Pipe asks | `httpx` imports in the image (the Pipe loaded and ran). `__task__` = `emoji_generation`/`query_generation` with `__metadata__.task`; chat calls get `__metadata__` (chat, message, session, user ids…) and an event emitter. Install by API, toggle, valves and `GET` hash re-check: the stored content hash equals the repository file | PASS |
| Refusals | knowledge create 401, memory add 404, retrieval query 403, web search 403, community sharing false. **`POST /api/v1/files/` returned 200** despite `USER_PERMISSIONS_CHAT_FILE_UPLOAD=false`: one 17-byte file stored in `uploads/` and a `file` row (text extracted, no collection). Reproduced in run 2 | **FINDING (V1)** |
| S2 purge (prototype) | `purge.py --window-seconds 60` against the live pod after a canary turn and an upload, with one chat created inside the window. Before: chat 4, chat_message 8, file 1, canary in `webui.db-wal`. Purge 1: deleted chats 4, messages 8, files 1, busy retries 0. Purge 2: all 0 (converges). After: chat 1 (the in-window chat), file 0, canary in 0 DATA_DIR files; the next Docs turn succeeded (1 source). **The 17-byte upload stayed in `uploads/`**: the prototype deletes rows, not storage objects. `oauth_session` rows (Fernet-encrypted tokens) are outside its scope | PASS for chats; **FINDING (V1 follow-on)** |

## Evidence status

Not a clean qualification. The retained evidence is partial in these places:

- **Source chips:** NOT RUN (visual); only the stored `source` and `statusHistory` events exist (check e).
- **Run-2 model list:** CONTRADICTED by the public-read `tool_probe`; the run-1 alice-only result stands (check d).
- **Run-2 egress:** PARTIAL / UNSAVED; only run 1 has saved reports. A future re-run must install the audit before
  `deploy`, write the pod IP list and then run `egress-report` (Resume).
- **Ingress-block positive probe:** NOT RUN (check d2).
- **Per-check commands/exit codes:** PARTIAL/absent; the Resume block has reproduction commands, not a per-check
  transcript. The exact run-2 repository test commands and exit codes are in the PR body, not this record.
- **Code interpreter/execution and trust-remote-code:** configured only; no behavior test.
- **Derived image:** local tag plus image ID; not a registry-pinned digest (Identity).
- **Source-chip Pipe version:** trial Pipe `8afdd40` is older than main `99ce3e8`; no re-run on the merged Pipe
  (Identity, check e).

## Findings for decision

1. **V1 uploads.** v0.12.3 checks the upload permission in the UI only; the route stores the file. The options are
   to block `/api/v1/files` in front of the pod, to accept it with S2 purging files (the prototype does not yet
   delete the bytes in `uploads/`), or to stop the pilot. A maintainer decision is needed; nothing was built for it.
2. **R2 Ollama probe.** Set an explicit `OLLAMA_BASE_URL` (loopback) in any manifest; the offline flag does not stop it.
3. NetworkPolicy must admit the IdP and DNS as well as the agent: the OIDC code exchange is server-side.
4. `CORS_ALLOW_ORIGIN` defaults to `*` (startup warning); set it to the Route host.
5. **Tokens in Functions.** Any admin-installed Function can read the user's access and ID tokens
   (`__oauth_token__`). The Docs Pipe must not declare it; a review rule for every installed Function is needed.
6. **Stale-session fallback.** When the current OAuth session is gone, `system_oauth` forwards the user's most
   recent other session's token. Gateways must check `exp`, `aud` and signature themselves (#1076).
7. **Task model.** The issue allows tasks off or a task model that is not the docs model. The env toggles close
   title, tags, follow-up and autocomplete, but `emoji`, `queries` and `image_prompt` still call the docs Pipe,
   which answers fixed text. The Pipe backstop keeps agent requests at user turns but the docs Pipe is still the
   task model. A separate task model or an explicit owner decision is needed.

## Resume

```sh
cd dist/worktrees/1073-frontend-trial   # branch fix/1073-frontend-trial
export EVIDENCE=$HOME/.local/share/mainframe-rag/owui-trial-1073 KIND_BIN=$HOME/.config/mainframe-rag/kind-live/bin/kind \
  KUBECTL=$HOME/.config/mainframe-rag/kind-live/bin/kubectl PIPE_FILE=$EVIDENCE/docs_pipe.py
sh scripts/ci/openwebui-trial/run_trial.sh cluster   # needs the images phase or the local images
sh scripts/ci/openwebui-trial/run_trial.sh audit     # before deploy: capture startup flows
sh scripts/ci/openwebui-trial/run_trial.sh deploy
# qualify.py: login admin|alice|bob; setup; then the probe:
sh scripts/ci/openwebui-trial/run_trial.sh toolprobe   # TOOLPROBE_OAUTH_SCOPES=... for the extra-audience run
# qualify.py: setup-toolprobe; tool-chat alice server:mcp:echo server:echo-openapi; kubectl logs deploy/toolprobe
# After the run (repeat after any pod restart): write the IP list, which no committed script does, then report.
"$KUBECTL" --context kind-owui-trial -n owui-trial get pod -l app=openwebui \
  -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}' > "$EVIDENCE/owui-pod-ips.txt"
sh scripts/ci/openwebui-trial/run_trial.sh egress-report > "$EVIDENCE/egress-report-run2.txt"
# DEX_TOKEN_EXPIRY=4m [DEX_STORAGE=sqlite3] run_trial.sh idp-config   # lifetime probe; then teardown
```
