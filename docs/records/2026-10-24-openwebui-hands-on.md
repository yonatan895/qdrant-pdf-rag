# Open WebUI hands-on Kind setup (procedure and record)

Status: **phase 2 blocked on host RAM (2026-10-25)**. Nothing from this setup is running; see Results. The maintainer asked on 2026-10-24 to use Open WebUI
hands-on, in the full trial setup, from the Windows browser, and to leave it running. Trial-only
(#1073, tracker #1070; ADR-0007 #1080 is not accepted). It changes no chart, `images.txt`,
Taskfile or production manifest. State and evidence stay outside git in
`~/.local/share/mainframe-rag/openwebui-trial/` (mode 700).

Entry point: `scripts/ci/openwebui-trial/hands_on.sh` (sh), with `hands_on.py` (the host-side
browser stand-in, stdlib only), `agent_relay.py` (the host relay), `hands_on.yaml` (Kind
stand-ins) and `hands_on_values.yaml` (chart values).

## What runs

| Piece | Source | Kind detail |
|---|---|---|
| Open WebUI pod: `openwebui`, the nginx allow-list `proxy` sidecar, `chat-purge` | #1083 chart at `CHART_REF` (head `4b36e0b`), rendered with `helm template --show-only` for the Deployment, NetworkPolicy, both ConfigMaps, Service and PVC | derived image `mainframe-rag/openwebui-trial:0.12.3-slim` (#1085 Containerfile), proxy `nginx-unprivileged` by the `images.txt` digest |
| Docs Pipe | `integrations/openwebui/docs_pipe.py` at `PIPE_REF` (main `13befa2`, with the #1091 trimming), swapped into the rendered chart copy | installed by the chart's own `provision_pipe.py` inside the pod, admin key on stdin |
| Identity | Dex v2.46.0 (pinned), HTTPS from a local CA, static users with a `groups` claim | namespace `owui-idp`; issuer `https://dex.localhost:5556/dex` |
| Route | none (Kind has no Route API) | router stand-in in `owui-router` (labelled `network.openshift.io/policy-group: ingress`), reencrypt, the Route timeout (300 s) |
| Agent | the maintainer's local lane (below) | relay pod labelled `app: rag-agent` plus Service `rag-agent`, so the chart's egress rule and the Pipe's default `AGENT_URL` apply unchanged |

Policy: namespace `owui` has Pod Security `restricted` and a default deny in both directions. The
chart's `rag-openwebui` policy is the allow-list: ingress from the router namespace to 8443,
egress to `app: rag-agent` on 8080, cluster DNS, and the IdP CIDR on 5556.

## How the agent is reached

The real local lane, as the maintainer runs it (`docs/local-kind.md` section 3,
`scripts/run_local_stack.sh`, memory profile "local lane"). `agent-up` starts each part as a
`systemd --user` unit named `owui-hands-on-*`:
- `local:llm` with `google/gemma-4-E4B-it-qat-mobile-ct` on 8000, then `local:embed` with
  `Qwen/Qwen3-Embedding-0.6B` on 8001 (`BUDGET_PROFILE=LOCAL_RT_8GB`, the documented vLLM image).
  A port that already serves the right model is reused. A port that serves another model, such
  as the G4 tunnels, is refused.
- A dedicated Qdrant: `sim_qdrant.sh` with container `owui-hands-on-qdrant`, port 6342 and the
  persistent volume `owui-hands-on-qdrant`. It is not `qdrant-sim` on 6333, which the sim lanes
  reset.
- Corpus: there is no live 0.6B corpus under the current rules (memory "corpus state"). The first
  `up` hard-links 12 of the 452 real manuals in `~/.config/mainframe-rag/kind-live/corpus`. It
  takes every 37th PDF, keeps the relative layout, and ingests them through the stack. Later runs
  skip ingest while the `mainframe_manuals` alias exists (`INGEST=auto|always|never`,
  `CORPUS_COUNT`).
- `local:stack` runs from a detached worktree at `PIPE_REF`, so the agent and the Pipe come from
  the same main commit. It uses the main venv with `PYTHONPATH=<worktree>/src`,
  `RERANK_ENABLED=false`, gateway 4000 and the agent on `127.0.0.1:8080`.

Path: Open WebUI pod → `rag-agent:8080` (relay pod, nginx, 3600 s read timeout) → host
`agent_relay.py` on the Kind network gateway, port 18080. That relay accepts only the Kind node's
address and has no idle timeout (the Windows console relay lesson). It forwards to `127.0.0.1:8080`.

## Browser access (Windows)

The Kind node publishes two loopback ports with `extraPortMappings` (docker-proxy, no idle
timeout and no relay process). WSL forwards them to Windows `localhost`. On 2026-10-24, Windows
showed no listener on 443, 5556, 3000 or 8443.
- Open WebUI: `https://owui.localhost/` (host 443 → router). The chart schema needs a dotted
  host with no port, so the redirect URI is `https://owui.localhost/oauth/oidc/callback`.
  `OWUI_PORT` moves it and patches `WEBUI_URL`/CORS/redirect to match.
- Dex: `https://dex.localhost:5556/dex`. Chrome, Edge and Firefox resolve `*.localhost` to
  loopback. Pods resolve the same name through a CoreDNS `rewrite`, so the issuer is identical on
  both paths.
- Certificates come from a per-run local CA (`pki/ca.crt`, 90 days). Accept the two warnings on
  the first visit, or import the CA into the Windows user store and remove it afterwards.

Sign-in: use the single sign-on button, then the Dex form.

| User | Group | Password file (under the state dir) |
|---|---|---|
| `team-user@trial.test` | `internal-team` | `team-user.password` |
| `docs-user@trial.test` | `docs-readers` | `docs-user.password` |
| `owui-admin@trial.test` (Pipe install, grants) | `owui-admins` | `owui-admin.password` |

Roles come from the same `groups` claim (`rolesClaim: groups`), because Dex static users carry no
roles claim. The allowed roles are the three groups, and the admin role is `owui-admins`. The chart
turns off OAuth group creation, so `provision` creates both groups first. Then it grants
`mainframe_rag_docs` read to them, and both users see only the Docs model.

## Commands

```sh
cd dist/worktrees/1073-owui-hands-on
S=scripts/ci/openwebui-trial/hands_on.sh
sh $S --dry-run up        # validate everything; no docker, no cluster
sh $S up                  # images, agent-up, cluster, deploy, relay, provision (heavy lock, flock -n)
sh $S ready               # health + smoke, writes READY.md (mode 600) in the state dir
sh $S status              # cluster, pods, units, agent, browser paths (WSL and Windows), memory
sh $S down                # deletes only the cluster whose node id this script recorded; stops the relay
sh $S agent-down          # stops only the owui-hands-on-* units and the dedicated Qdrant (volume kept)
```

`smoke` signs in both users and checks role `user`, the group and Docs-only models. Then it asks
one question as `docs-user` and requires `done` and at least one source chip. The question is read
from `smoke-question.txt` in the state dir and is never in git. Every path in the #1083 site
acceptance list must answer the proxy's own `403 denied`, and `GET /api/models` must answer 200.

## Resources

- Kind node capped at `KIND_NODE_MEM_MB=4096` (`docker update`). Admission needs MemAvailable of
  at least 5120 MB at cluster creation. The Open WebUI container limit is the chart default
  (2 GiB).
- Local lane, by the budget caps (`LOCAL_RT_8GB`): reasoning 6400 MB and embed 2600 MB of host RAM,
  each checked by `run_local_vllm.sh` itself. Add the gateway, OpenObserve, the agent and Qdrant.
  The order is models, then ingest, then Kind, so ingest never overlaps the cluster start.
- Whole plan: `up` checks the sum before it starts anything. The estimates are reasoning 5500 MB
  (observed 5.35 GiB host RSS), embed 2000, stack 2048, the Kind cap and 1024 MB headroom, about
  13.6 GB with `KIND_NODE_MEM_MB=3072`. They are set by `REASONING_EST_MB`, `EMBED_EST_MB` and
  `STACK_EST_MB`. Each vLLM guard checks only its own server, so before this check the reasoning
  server could start and leave too little for the rest. On any failure, `up` removes what that
  run started, recorded in `run-started.txt`, and writes `READY.md` with the step and exit code.
- Sized for a host with nothing else heavy running, after G1. `up` refuses while ports 4000, 8000,
  8001 or 8080 are held by something it did not start (`AGENT_REUSE=1` accepts a running agent).

## Kind stand-ins (differences from the site)

| Site | Here | Why |
|---|---|---|
| restricted-v2 assigns a UID | pod `runAsUser` from 1000700000–1000799999, group 0 (kustomize patch) | no SCC on Kind |
| IdP certificate from an enterprise CA | `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` and a CA ConfigMap, patched in | **finding: the chart has no input for IdP CA trust**; Open WebUI's server-side OIDC calls fail against a private CA without it |
| Route, reencrypt, destinationCA | router stand-in pod (nginx), same timeout | no Route API |
| kubelet probes pass the SDN | policy `kind-kubelet-probes`: the node address to 8443 only | the proxy uses an `httpGet` probe |
| IdP CIDRs | pod network `10.244.0.0/16`, TCP 5556 | Dex runs as a pod |
| agent pods | relay pod `app: rag-agent` → host relay → local agent | the agent runs on the host |
| `offline_access` refresh | kept (chart scopes); Dex v2.46.0 wedged on refresh in the trial | if sign-in hangs, restart Dex (`kubectl -n owui-idp rollout restart deploy/dex`) |

## Results

Phase 1, 2026-10-24: `sh $S --dry-run up` rc=0, along with `--dry-run down`, `agent-down` and
`ready`. The chart render gave 6 objects. The ConfigMap Pipe sha256 matched main's file
(`6fdf65ca…`). The NetworkPolicy has 3 egress rules. Dex has 3 users with an HTTPS issuer. The
support manifest has 14 objects.

Phase 2, 2026-10-25 00:59–01:06 IDT, after G1 ended:
- **Attempt 1: `up` rc=1 in `agent-up`.** The images were built. The derived image was rebuilt
  from the #1085 Containerfile as `de278f9e…`, user `1000:0`. gemma-4-E4B served on 8000 at
  5.35 GiB host RSS. The embed server then refused under its own guard: 2600 + 2048 MiB needed,
  1663 MiB available. `agent-down` removed the reasoning unit. No cluster, Qdrant, stack or relay
  had started.
- **Attempt 2, with the whole-plan check: `up` rc=75.** The check refused before anything
  started: 6959 MB available, 13644 MB needed. `READY.md` records the failure.
- **Held outside this setup:**
  - the rag-kind node, at 4.63 GiB (its qdrant-0 was OOM-looping at the 4 GiB limit; not
    touched);
  - three `dist/deploy-bb53388` multiprocessing workers, about 0.85 GB each (not touched).
- **No startup check ran:** the sign-ins, the docs question with chips and the blocked path.
- **To fit,** about 7 GB must be freed (rag-kind and those workers). That is the maintainer's
  call. A smaller corpus does not help: the model servers dominate.
