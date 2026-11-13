# ADR-0011: Two caller classes for the data gateways (the product as the user, improvement agents as a workload identity)

- **Status:** decisions accepted by the maintainer on 2026-11-13 (in session;
  summary in [#1200](https://github.com/yonatan895/qdrant-pdf-rag/issues/1200));
  record pending merge. Tracker #1191; related #1075, #1199. This record
  **extends** [ADR-0010](0010-team-tools-on-open-webui.md): it adds the agent
  class and leaves every ADR-0010 clause unchanged for the user class. The
  agent class needs the TD6/trust-model amendment listed below once open
  point (a) chooses its credential. Accepting it enables nothing: no gateway,
  entry point, Route, credential or fence rule is added by it. Each
  agent-class gateway still needs its own decision record ("Required
  follow-up").
- **Context:**
  - ADR-0010 defines the gateways for one caller, a signed-in user in a
    Team-model chat. The improvement agents of the gap (herdr-core plus
    OpenCode, #1172, #1196) now need production signals (#1199), and no user
    is present when they run.
  - Without a rule, each later gateway (telemetry #1199, Splunk #1077, GitLab
    #1078, mail, z/OS) would decide alone whether a service identity may
    call it and what that identity may read.
  - State at c5259460: only the caller-verification library exists
    (`src/mainframe_rag/mcp/tool_auth.py`); no gateway is built. The agents
    machine holds a dev LiteLLM key and, optionally, a GitLab project token,
    no cluster credential, and its egress fence allows DNS, GitLab and the
    model gateway only ([agents-vm](../agents-vm.md#accounts)).
- **Decision:**
  1. **D1, two caller classes, by whose authority the caller acts.**
     - **User class: the product acts only as the signed-in user.** It holds
       no standing access of its own to any source, apart from the named
       shared-credential paths below. Each gateway call carries that user's
       verified identity and entitlement. ADR-0010 TD7 (one read-only Splunk
       service token, on the premise of equal entitlement) stays as a
       stated-premise exception, unchanged.
     - **Agent class: the improvement agents act as a narrow workload
       identity for the project team, with no user present.**
       *In scope:* the repository; the dev/CI namespace; content-free
       telemetry ([tier 1](../observability.md#data-tiers)); rated-case
       exports from the feedback store (#1199: rated cases only, with the
       full message and history, never in git, merge requests, issues or
       logs); and their own dev LiteLLM key.
       *Out of scope:* production user data (Splunk indexes, mail, z/OS,
       other people's GitLab), any production write, and deploy authority.
       Their work reaches production only as a reviewed merge request and a
       signed release.
       The rated-case export files are user-authored content and the one
       admitted content exception to "no production user data": they are
       admitted only under the ADR-0005 amendment of #1199. Here,
       "production user data" means data held in a production source system
       or read live through a production gateway; it does not mean those
       export files.
  2. **D2, one gateway layer serves both classes: the ADR-0010 gateways.**
     - *Shared:* the gateway code base (caller verification, output capping,
       redaction and framing, fixed errors, limits, the decision log), the
       MCP protocol, and one policy model of caller class x source x
       operation.
     - *Not shared:* credentials, token audiences and policy.
     - A user token is never accepted at an agents' entry point, and an
       agents' credential is never accepted at a user entry point.
  3. **D3, one gateway per source, each with its own decision record.** The
     first agent-class gateway is the read-only telemetry gateway of #1199:
     live read access to production metrics and traces. The rule it must
     meet: it forwards only to the backend's search endpoints; it bounds time
     range, rows, timeout and concurrency; the agents VM — not the CI
     namespace or the dev pod — gets one authenticated entry point; and the
     trace backend itself gains no Route. That entry point needs an external
     exposure decision, and the backend's NetworkPolicy must admit the new
     client; both belong to its record and the ADR-0006 amendment. Its
     detailed design is follow-up.
  4. **D4, not adopted.**
     - A new orchestration layer. Open WebUI's native tool loop serves users
       (ADR-0010 TD3); herdr-core plus OpenCode serves the agents.
     - A single model with every tool attached. ADR-0010 TD1's separate Team
       models stay. The reason is containment: tool output is untrusted, and
       text injected through one tool's output can only steer the tools
       attached to that model.
     - Agents testing product tools with a real user's token. They use mock
       or dev backends.

## Policy matrix

A cell is the whole grant. A caller class, source or operation with no cell
is refused, and a new cell needs the decision record of that source's
gateway. Every granted operation is a read, except (a) the product's
existing feedback writes, (b) the agent GitLab cell, whose Developer token
can push branches (`write_repository`, ADR-0009 §2), and (c) the product's
trace and metric ingest into the backend.

| Source | User class (the product as the signed-in user) | Agent class (improvement agents) |
|---|---|---|
| Manuals (Qdrant) | Existing: read-only serving credential (AGENTS.md); the agent is anonymous to users on the premise that everyone reads the corpus (ADR-0007 R3). Not a gateway. See "Shared-credential paths" | Existing: none in production (ADR-0009 §2 test data; a negative control of the fence proof). Dev sidecar or CI `sim` only |
| z/OS | Existing: ADR-0003. Off; `job_status` only, after its reactivation gate. See "Shared-credential paths" | **This ADR:** none |
| Splunk | Existing: ADR-0010 TD5 and TD7 (#1077) | **This ADR:** none for production indexes. A scoped view of the product's own logs: **open (b)** |
| Trace and metrics backend | Existing: none for users. Operators reach the console by port-forward with cluster RBAC (ADR-0006) | **This ADR:** read-only, through the telemetry gateway (D3). Not deployable before its record and the ADR-0006 amendment. Their own dev stack's backend: direct |
| GitLab | Existing: ADR-0010 TD4, the user's own `read_api` token (#1078) | Existing: the project's own repository with a Developer-role project token (ADR-0009 §2). **This ADR:** no other project |
| Mail | Existing: ADR-0010 TD1, a separate Team model; not approved, needs its own decision | **This ADR:** none |
| Feedback store | Existing: ADR-0005, the agent's answer-record and rating writes; no read path for users | **This ADR:** rated-case export files only, no live query. The files carry user-authored content and are the one named exception to G3, admitted under the ADR-0005 amendment of #1199 |
| Model gateway | Existing: per-leg keys, the agent's (AGENTS.md) and Open WebUI's own (ADR-0010 TD3) | Existing: their own dev key (ADR-0009 §2) |

How the agent class's identity is issued is **open (a)**.

## Guarantees and what establishes them

Review each row with the four questions of the AGENTS.md review rules.
"Required" marks a control this record demands and nothing yet enforces.

| Guarantee | State that establishes it | Who can change that state | Valid for | What forbids the counterexample |
|---|---|---|---|---|
| G1. A user-class call runs as one verified user | The forwarded IdP token: signature, issuer, exactly this gateway's audience with `require_single_audience=True`, expiry, and a group in `allowed_groups` ([caller verification](../team-tools.md#caller-verification)). No gateway mounts the library yet | The IdP (keys, registrations, group membership); the gateway's configuration, through a deployed release only | The token's `exp` plus at most 300 s leeway; cached keys 300 s, stale keys at most `jwks_max_stale_s` after a failed fetch | `verify_token` refuses each failing check; `principal_from_scope` raises without a verified principal; `tests/test_mcp_tool_auth.py`. The library default is `require_single_audience=False`, which accepts a two-audience token, so only an entry point that fixes it on is inside this guarantee. TD7 is the one stated exception to per-user entitlement |
| G2. No token crosses classes | Each entry point serves one class and names only its own audience. Under open point (a)-1 (IdP token), `require_single_audience=True` and the group check apply to an agent entry point exactly as to a user entry point; the boundary against a user who carries the agent group and audience is the IdP's issuing rule, not the gateway. Under (a)-2 (per-workload secret), the entry point verifies no IdP token; a second verifier checks the secret's scope and rotation instead, and TD6 and the trust-model clause are amended for it. The agent credential is one no user can obtain | The identity team (registrations, which client may get which audience); the gateway's configuration, through a deployed release | As G1 for tokens; for a per-workload secret, until rotation (open (a)) | The audience and group checks under (a)-1. **Required:** each agent-class gateway's acceptance shows a valid user token refused at its entry point and its own credential refused at a user entry point, and no entry point accepts the other class's credential |
| G3. The agents reach no production user data, with rated-case export files as the one admitted content exception | No matrix cell for a production source; no credential for those sources on the agents machine; the egress fence ([agents-vm](../agents-vm.md)); the export path admits rated cases only (ADR-0005 amendment, #1199) | The maintainer (this matrix and the exception); the platform and source owners (credentials); the VM administrator (fence) | Until the fence is re-rendered, a credential is issued, or the exception is widened | `agents_vm.py start` refuses an unfenced account; the fence proof's negative controls. The site fence proof is required and **not yet run**. Agent-authored code also runs in CI job pods after a push; that path's state is the ADR-0009 §2 isolation proof, also a site step |
| G4. Agent work reaches production only as a reviewed merge request and a signed release | Protected `main` with code-owner approvals; a Developer-role bot token that cannot approve or merge; deploy refuses a checkout that is not a verified signed release (ADR-0009 §1, §2, §6). ADR-0009 F1 and F2 are done; F6, the gap release build and sign pipeline, is not started, so gap-developed code has no route to production yet | The gap maintainer group (GitLab settings, approvers); holders of the release key; the operator can set the dev-only opt-out only with `CI_REHEARSAL=1`, refused otherwise | Standing; the bot token expires and is rotated | ADR-0009 F1 and the hygiene and CODEOWNERS checks. F6 is outstanding; ADR-0009's own status is still "proposed" |
| G5. Agent-class telemetry reads return no question, answer or excerpt text | The product emits none into tier 1 (ADR-0005, ADR-0006, [observability](../observability.md#data-tiers)) | Product code, through a reviewed release; the site's metrics scraping or forwarding path can add writers (ADR-0006) | Standing | The ADR-0006 data-boundary gate (its record notes the `_meta` stream was not scanned). The gateway adds no content filter of its own; G5 rests on the producer, and a free-form read returns whatever any writer stored |
| G6. The telemetry gateway cannot mutate or starve the backend | Forward-only to search endpoints; fixed bounds on time range, rows, timeout and concurrency (D3) | The gateway's code and configuration, through a deployed release | Standing | **Required** of the gateway's record: pinning tests for the forwarded path set and for each bound |

## Amendments and unchanged clauses

This record amends no accepted clause for the user class. It adds the agent
class; the agent-class amendments are listed below.

| Record | Effect |
|---|---|
| ADR-0010 TD1 to TD8, trust model, amendments table | Unchanged for the user class. TD6 (`require_single_audience=True`, exactly this gateway's audience) and the trust-model clause "verifies the forwarded IdP token … on every call" bind an agent-class entry point only if open point (a) chooses the IdP token. Under the per-workload secret they verify nothing, so the decision on (a) must name the TD6 and trust-model amendment before any such entry point is built. "The gateway is the authority" and "tool output is untrusted data" bind the agent class too |
| ADR-0010 TD7 | Unchanged; the one named Splunk exception under the shared-credential rule |
| ADR-0006, "ClusterIP only … no Route and no extra auth layer", and the credential rule | Unchanged here. ADR-0006 says credentials come from a Secret and the console is reached by port-forward; the telemetry gateway adds a reader other than the operator and an entry point outside the cluster, so #1199 must amend it before that gateway is deployed. That includes the entry point's external exposure and the backend's NetworkPolicy admitting the new client |
| ADR-0005, content only in the feedback store | Unchanged here. Exports on the agents machine and the full message and history need the amendment in #1199 (its item 6). That amendment must also cover the replay leg: replayed case text goes to the model gateway under the agents' dev key |
| ADR-0009 §1, §2, §5, §6 | Unchanged; G4 relies on them. F1 and F2 are done; F6, the gap release build and sign pipeline, is not started, so gap-developed code has no route to production yet. The CI namespace and the dev pod gain no path to the production namespace: the telemetry entry point is for the agents VM only |
| ADR-0003 | Unchanged; z/OS is not an ADR-0010 gateway and its shared SAF credential does not qualify under "Shared-credential paths" |
| [agents-vm](../agents-vm.md) fence allow-list | Unchanged here; one host is added only with the telemetry gateway's record |

## Shared-credential paths

A shared **read-only** credential is allowed only where every user the
product serves has equal entitlement to the data it reaches, and each is a
named exception. Three qualify today: ADR-0010 TD7 (Splunk), the read-only
Qdrant serving credential with an agent anonymous to users on the premise
that everyone reads the corpus (ADR-0007 R3), and the per-leg model keys
(AGENTS.md, ADR-0010 TD3).

The single read-only SAF credential of the z/OS bridge (ADR-0003 decision 3,
off and unwired) does **not** qualify: z/OS access controls do not map to the
product's users, and the per-caller `denied` check is not built. It needs
per-user authorization or its own decision before activation; this record
leaves it as it is.

Two further shared credentials are writes by the product itself, not user
reads: the operator-created feedback-service bearer (ADR-0005, #804) and the
telemetry ingest credential. They are not exceptions to the read rule; each
stays bounded by its own ADR and the gateway policy.

## Open points

- **(a) How the agents' workload identity is issued at a site.** Either an
  IdP service token from a client-credentials style grant, so the existing
  verification library treats the agents as one more group, or a
  per-workload secret held on the agents machine.
  *Question for the identity team:* can the site IdP issue a service client
  an access token that carries exactly one audience (the agent-class entry
  point's), a non-empty `sub`, and a group claim no user token can carry,
  signed with a key in the JWKS the library already reads? If yes, the
  library needs no change. If no, the per-workload secret needs a second
  verifier in the gateway code base, with its own storage, rotation and
  expiry rules. The maintainer chooses after that answer.
- **(b) A scoped Splunk view of the product's own logs for the agents.**
  *Question for the platform's Splunk owner:* can a token be issued to a
  non-human identity that searches only the product's own log streams and
  nothing else? The maintainer decides whether to grant the cell once such a
  token exists. Until then the agent class has no Splunk access.

## Required follow-up

1. The telemetry gateway's decision record and design (#1199), meeting D3,
   G2 and G6, with the ADR-0006 amendment (including external exposure and
   the backend NetworkPolicy), and the
   [observability](../observability.md) and fence updates.
2. The ADR-0005 amendment for rated-case exports (#1199), including the
   replay leg to the model gateway under the dev key.
3. Open point (a), before any agent-class entry point is built; if it
   chooses the per-workload secret, the decision names the ADR-0010 TD6 and
   trust-model amendment.
4. ADR-0009 F6 if gap-developed code is to have a route to production: the
   gap release build and sign pipeline does not exist yet.

## Consequences

- Every later gateway states which caller classes it serves, per operation,
  and proves G2 in its acceptance.
- The agents see production through content-free telemetry and rated-case
  export files only. The files carry user-authored content and are covered
  by the ADR-0005 amendment; a defect visible only in live production user
  data is diagnosed by a person.
- Two credential sets and two audiences per source served to both classes,
  each with its own rotation.
- Verification: the prose row of
  [live-stack](../live-stack.md#verification-minimums) for this record.
