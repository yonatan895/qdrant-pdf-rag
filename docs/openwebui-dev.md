# Open WebUI dev environment and acceptance lane

Owner of the supported way to run the main docs platform (Open WebUI over the
agent, ADR-0007 and the 2026-10-26 decision in #1070) on Kind, and of the
suite that proves its conditions by execution. The chart (`charts/mainframe-rag`,
Open WebUI templates) owns what is deployed, the [Docs Pipe](../integrations/openwebui/docs_pipe.py)
owns the answer path, [deploy.md](deploy.md) owns the site procedure. This file
owns only the Kind tooling around them. Scripts: `scripts/openwebui/`.
Design records (dated, unchanged): [kind trial](records/2026-10-24-openwebui-kind-trial.md),
[hands-on setup](records/2026-10-24-openwebui-hands-on.md).

Both entry points render the chart and the Pipe from the **working tree**, never
from a git ref: `helm template` runs on a copy of `charts/mainframe-rag`, and
`integrations/openwebui/docs_pipe.py` replaces the chart's copy in that render
(its sha256 is checked against the rendered ConfigMap). The values file
(`scripts/openwebui/values.yaml`) supplies every site input the chart needs
(Secret name, host, OIDC provider and roles, IdP egress CIDR, Route destination
CA), so the render still works once the chart fails closed without them. Kind
stand-ins: `kind-support.yaml` (test IdP Dex, router stand-in, kubelet-probe
policy), restricted-v2's UID patch, `dev-app.yaml` (dev only: namespace and
agent relay). Production has none of them.

<a id="dev-environment"></a>
## Dev environment

`sh scripts/openwebui/dev.sh [--dry-run] up|ready|status|urls|down|agent-down`
(also `images|agent-up|cluster|deploy|relay|provision|smoke`). A long-lived Kind
cluster (`owui-hands-on`), the chart's Open WebUI objects, a test IdP with three
users, and the local agent lane (vLLM models, dedicated Qdrant, `local:stack`)
behind a host relay, reachable from the Windows browser at `https://owui.localhost/`.
`--dry-run` validates tools, pins, the chart render, both manifest sets, ports,
memory and the lock without docker or a cluster. Run it with an isolated
`OWUI_STATE`.

| Guarantee | Mechanism |
|---|---|
| RAM admission | `up` sums the whole plan (both models, stack, node cap, headroom) against MemAvailable and exits 75 before starting anything; the heavy lock held exits 75 with a message |
| Only its own cluster | `down` deletes `KIND_NAME` only when `cluster.created` records the node container id; a kind or docker failure keeps the record and retries |
| Private state | `$OWUI_STATE` (default `~/.local/share/mainframe-rag/openwebui-dev`; the old `openwebui-trial` directory is reused while it is the only one) is mode 700; passwords, keys and the kubeconfig mode 600 |
| Secrets | files and stdin only, never argv, never logs; output carries ids, counts, states |
| Local CA | name-constrained to `localhost`, `.localhost`, `.svc`, `.svc.cluster.local`, so importing it into a browser store cannot vouch for other sites; an older unconstrained CA is replaced on `deploy` |

The agent lane runs from this working tree (`AGENT_SRC`); after any rule-module
change run `INGEST=always dev.sh up` (a rule-module change forces a re-embed).
The IdP CA reaches the pod only through the chart input `openwebui.oidc.caBundle` (`--set-file` of the run's CA), so the dev environment and the lane also exercise that input against a private-CA IdP. Pins for the dev-only images live in `scripts/openwebui/pins.txt`; the proxy
image digest comes from `images.txt`.

<a id="acceptance-lane"></a>
## Acceptance lane

`e2e.yml` job `kind-live-rehearsal`, lane `openwebui` (`workflow_dispatch` and
nightly on main, like every Kind lane; never on `pull_request`). It runs the
lane's normal pipeline first, so the agent is the one the other Kind lanes
deploy from the bundle: test gateway with mock reasoning and embedding models,
the synthetic corpus, no GPU, no hash mode. Before the egress boundary closes it
pulls the derived Open WebUI image **by the digest the `image-openwebui` job pushed** (`pins.txt`, `OWUI_PUSHED`), plus the pinned Dex and nginx images, and loads all three into the node (`acceptance.sh images`; `OWUI_BUILD=1` builds from `images/openwebui/Containerfile` instead, dev only). Behind the
boundary it deploys the chart objects and stand-ins into the lane's namespace
(`acceptance.sh deploy`) and runs `acceptance.py` through port-forwards
(`acceptance.sh run`). The JSON result is kept as lane evidence
(`diagnostics/openwebui/acceptance.json`, ids and states only).

`scripts/openwebui/acceptance.py` collects (kubectl, HTTP as the signed-in
users through the router stand-in); `scripts/openwebui/checks.py` judges.
Every rule in `checks.py` has a counterexample in
[tests/test_openwebui_acceptance.py](../tests/test_openwebui_acceptance.py).

| Check | Executed as |
|---|---|
| arbitrary non-root UID | `id` in the container equals the namespace UID, group 0, not the image `USER`; every container has the restricted shape |
| zero egress | the pod's CoreDNS queries name only the IdP and the agent; real sockets from the pod reach the agent and the IdP and fail to reach 1.1.1.1, 8.8.8.8, the API server, Qdrant, the gateway and a pod in another namespace; the lane's shared boundary policy excludes the Open WebUI pod, and the suite fails if any policy but the chart's selects it for egress |
| no vector store or uploads after use | `inspect_data.py` in the pod after answers: no vector paths or tables, no `uploads/`, no retrieval rows |
| Docs model only | both users list exactly `mainframe_rag_docs`; IdP group claim mapped; signup and password login refused |
| citations | one saved turn has finished, text, and source events with documents (the Pipe's source chips) |
| C1 admin edits | a live admin config edit takes effect, never reaches the `config` table and is gone after a restart |
| restart persistence (RWO) | chats, the Docs Function bytes (sha256 equal to the repository Pipe) and the grant survive a Recreate rollout; the next chat answers |
| retention purge (S2) | an aged chat and an orphan upload are removed by the chart's own `purge_chats.py`, the canary is gone from every file on the claim, a fresh chat survives, and the next ordinary chat answers |
| log canary (S4) | no question canary, password or key occurs in the Open WebUI, agent, router or Dex logs |
| blocked paths | the allow-list paths answer the proxy's 403 `denied`; an allowed path still answers 200 |
| console alongside | `/ui` and `/healthz` of the agent answer before and after the Open WebUI restart |

Not proven here: the real IdP, Route and SCC (stand-ins, see the hands-on
record), model quality (mock models), the node-level pre-NAT flow audit of the
2026-10-24 trial (the DNS and socket checks replace it in CI), and anything
about the site's CNI beyond Kind's NetworkPolicy engine. A failed lane is a
product defect or a lane defect; do not weaken a rule in `checks.py` to pass.

The same workflow's `image-openwebui` job (dispatch and nightly only) builds
that Containerfile and pushes `ghcr.io/<owner>/qdrant-pdf-rag-openwebui` with the commit-SHA tag,
sets the `v0.12.3-slim` tag only if it does not exist yet (never moves it), and
writes the manifest digest, the tag outcome and a rebuild-reproducibility probe to the job summary; the `images.txt` pin is a separate PR. Every other e2e job that runs
the operator flow sets `OPENWEBUI_ENABLED=false` (the front end is default-on
and fails closed without site inputs); only the `openwebui` lane deploys it.

<a id="gate-map"></a>
### ADR-0007 gate to evidence map

A green lane is not "all gates passed". Each condition and where it is proven:

| Gate | Proven by |
|---|---|
| C1 admin edits revert on restart | lane `admin-edit-reverts` (`scripts/openwebui/checks.py`) |
| V1 no second vector store | lane `data-dir`, `docs-model-only` (no upload/knowledge path: `blocked-paths`); chart env pins in `tests/test_helm_chart_contracts.py`; memory/notes refusal: site acceptance only |
| V2 answers only through the agent | `tests/test_openwebui_docs_pipe.py` (`test_pipe_never_declares_token_or_request_parameters`, `test_task_calls_never_reach_the_agent`); lane `docs-model-only` and installer refusal of other Functions (`tests/test_openwebui_frontend_scripts.py`); "agent requests equal user turns": site acceptance only |
| V3 contract fidelity | `tests/test_openwebui_docs_pipe.py` (error-code table, stream, citations); lane `answer-citations` |
| R1 non-root, restricted | lane `uid`, `pod-security`; pinned digests: `tests/test_helm_chart_contracts.py`, `images.txt` |
| R2 offline, zero egress | lane `dns-egress`, `egress-probes`, `egress-policy-owner`; policy render: `tests/test_helm_chart_contracts.py`; browser external fetch: site acceptance only |
| R3 identity | lane `group-claim`, `role-revocation` (a Dex user in neither role list gets no session), `sso-only`, private-CA IdP sign-in (`openwebui.oidc.caBundle`; a corrupt bundle is refused at render and by the ca_trust init step: `tests/test_helm_chart_contracts.py`); session expiry and Route-only ingress: `tests/test_helm_chart_contracts.py`, site acceptance |
| E1 allow-list proxy | lane `blocked-paths` (403 from the sidecar; NUL path asked on the sidecar itself); `tests/test_openwebui_proxy_nginx.py` runs the real image against every variant |
| S1 RWO block PVC, one replica | lane `restart-persistence`; `tests/test_helm_chart_contracts.py` (claim), `tests/test_airgap_validate_sh.py` (NFS refused); block semantics: site acceptance only |
| S2 30-day purge | lane `retention-purge` (chart's `purge_chats.py`, next chat); `tests/test_openwebui_frontend_scripts.py` |
| S3 backups and admin access | site acceptance only (backup window, storage-class encryption) |
| S4 no content in logs | lane `log-canary` |
| L1 licence | `tests/test_license_inventory.py`, `docs/licensing.md`; branding unmodified: derived layer changes ownership only (`images/openwebui/Containerfile`) |
| P1 patch policy | process (pin-bump PR re-runs this lane on the new digest) |
| K1 rollback by one flag | `tests/test_helm_chart_contracts.py` (`test_openwebui_opt_out_is_byte_identical_to_a_chart_without_the_component`) |
