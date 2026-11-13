# ADR-0011: Two caller classes for the data gateways (the product as the user, improvement agents as a workload identity)

- **Status:** decisions accepted by the maintainer on 2026-11-13 (in session;
  summary in [#1200](https://github.com/yonatan895/qdrant-pdf-rag/issues/1200));
  record pending merge. Tracker #1191; related #1075, #1199. This record
  **extends** [ADR-0010](0010-team-tools-on-open-webui.md) with the agent
  class. It binds TD6's single-audience and verification parts to
  agent-class entry points whenever their workload token is IdP-issued; the
  token-source clause needs the amendment listed under (a) if the grant is
  client-credentials. It names every ADR-0010 amendment that open point (a)
  may require ("Amendments").
  Accepting it enables nothing: no gateway, entry point, Route, credential
  or fence rule is added by it. Each agent-class gateway still needs its own
  decision record ("Required follow-up").
- **Context:**
  - ADR-0010 defines the gateways for one caller, a signed-in user in a
    Team-model chat. The improvement agents of the gap (herdr-core plus
    OpenCode, #1172, #1196) now need production signals (#1199), and no user
    is present when they run.
  - Without a rule, each later gateway (telemetry #1199, Splunk #1077, GitLab
    #1078, mail, z/OS) would decide alone whether a service identity may
    call it and what that identity may read.
  - Site topology, as the maintainer states it (2026-11-13): one product
    namespace; a separate CI namespace that holds the GitLab runners and a
    dev pod (ADR-0009 §2); and the agents VM, a Linux VM outside OpenShift
    ([agents-vm](../agents-vm.md#accounts)).
  - The maintainer accepted ADR-0009 on 2026-11-13; its status change is a
    separate pull request, so this record cites it as accepted, pending
    merge. Its follow-up F6 (gap release build and signing) is not started
    and is now raised in priority.
  - State at c5259460: only the caller-verification library exists
    (`src/mainframe_rag/mcp/tool_auth.py`); no gateway is built. The agents
    VM holds a dev LiteLLM key and, optionally, a GitLab project token, no
    cluster credential, and its egress fence allows DNS, GitLab and the
    model gateway only.
- **Decision:**
  1. **D1, two caller classes, by whose authority the caller acts.**
     - **User class: the product acts only as the signed-in user.** It holds
       no standing access of its own to a source where users' entitlements
       differ: each gateway call to such a source carries that user's
       verified identity and entitlement. A shared read-only credential is
       allowed only where every user who can reach that credential's data
       through the product has the same entitlement, and each such case is a
       named exception with its premise written down, in the form ADR-0010
       TD7 uses for Splunk. A credential that reaches no stored data (the
       per-leg model keys) is not a shared data credential and needs no
       exception. "Shared credentials" classifies the known cases.
     - **Agent class: the improvement agents act as a narrow workload
       identity for the project team, with no user present.**
       *In scope:* the repository; the CI namespace's dev pod and CI job
       pods (no gateway entry point); content-free telemetry
       ([tier 1](../observability.md#data-tiers)); rated-case exports from
       the feedback store; and their own dev LiteLLM key.
       *Out of scope:* production user data, any production write, and
       deploy authority. Their work reaches production only as a reviewed
       merge request and a signed release.
     - **Production user data** is content that users or their systems
       produced and that a production source holds: Splunk indexes, mail,
       z/OS data, other people's GitLab content, and the question, answer,
       comment and chat-history text in the feedback store or in Open WebUI
       chats. Tier 1 telemetry is not user data, because it carries no such
       text.
     - **The single exception is the rated-case export** (#1199): rated
       cases only, with the full user message and the chat history the model
       saw, under ADR-0005's export rules, never in git, merge requests,
       issues or logs. Its onward leg is part of the exception: a replayed
       case sends that text to the model gateway under the agents' dev key.
       *(The maintainer decided the export and its content on 2026-11-13
       and confirmed it as the single content exception. The definition
       above extends the earlier "production source system or live gateway"
       wording to feedback-store and Open WebUI chat text; that extension
       awaits confirmation.)*
  2. **D2, one gateway layer serves both classes: the ADR-0010 gateways.**
     - *Shared:* the gateway code base (caller verification, output capping,
       redaction and framing, fixed errors, limits, the decision log), the
       MCP protocol, and one policy model of caller class x source x
       operation.
     - *Not shared:* credentials, token audiences and policy.
     - A user token is never accepted at an agents' entry point, and an
       agents' credential is never accepted at a user entry point.
     - Whenever the workload identity is an IdP-issued token, ADR-0010 TD6's
       single-audience and verification parts bind the agent-class entry
       point as they bind a user one: the token carries exactly one audience,
       that entry point's own, and `require_single_audience=True` is fixed
       there, not an operator setting. The token-source clause binds only for
       token exchange or on-behalf-of; a client-credentials grant needs the
       amendment in "Amendments". The agent token carries the team group and
       the agent group, and the entry point's `allowed_groups` is the agent
       group: the trust model's team-group check is satisfied, the agent
       group is the class boundary, and a user whom the IdP issues both
       groups and the agent audience can still cross because the library
       cannot tell the classes apart (G2).
  3. **D3, one gateway per source, each with its own decision record.** The
     first agent-class gateway is the read-only telemetry gateway of #1199:
     live read access to production metrics and traces. The rule it must
     meet: it forwards only to the backend's search endpoints; it bounds time
     range, rows, timeout and concurrency; it has one authenticated entry
     point, for the agents VM only; and the trace backend itself gains no Route. It is not deployable until
     #1199 amends ADR-0006. The dev pod and CI job pods get no entry point
     under this ADR; extending it to the dev pod would be a separate
     decision. Its detailed design is follow-up.
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
gateway. Every grant is a read, except three: the product's feedback writes
(ADR-0005), the product's telemetry export into the trace backend, and the
agents' GitLab project token, which can push branches to the project's own
repository (`write_repository`, Developer role).

| Source | User class (the product as the signed-in user) | Agent class (improvement agents) |
|---|---|---|
| Manuals (Qdrant) | Existing: read-only serving credential (AGENTS.md); the agent is anonymous to users (ADR-0007 R3). **This ADR:** named exception E2 | Existing: none in production (a negative control of the fence proof). Dev or CI `sim` instances only |
| z/OS | Existing: ADR-0003. Off and unwired. **This ADR:** its shared credential does not qualify; see "Shared credentials" | **This ADR:** none |
| Splunk | Existing: ADR-0010 TD5 and TD7 (#1077), named exception E1 | **This ADR:** none for production indexes. A scoped view of the product's own logs: **open (b)** |
| Trace and metrics backend | Existing: none for users. Operators reach the console by port-forward with cluster RBAC (ADR-0006) | **This ADR:** read-only, through the telemetry gateway (D3), after its record and the ADR-0006 amendment. Their own dev stack's backend: direct |
| GitLab | Existing: ADR-0010 TD4, the user's own `read_api` token (#1078) | Existing: the project's own repository with a Developer-role project token when the optional token is present ([agents-vm](../agents-vm.md#accounts)). **This ADR:** no other project |
| Mail | Existing: ADR-0010 TD1, a separate Team model; not approved, needs its own decision | **This ADR:** none |
| Feedback store | Existing: ADR-0005, the agent's answer-record and rating writes; no read path for users | **This ADR:** rated-case export files only, no live query: the single exception of D1. Needs the ADR-0005 amendment of #1199 first |
| Model gateway | Existing: per-leg keys, the agent's (AGENTS.md) and Open WebUI's own (ADR-0010 TD3). Not a data source | Existing: their own dev key ([agents-vm](../agents-vm.md#required-settings)). It carries replayed case text (D1) |

How the agent class's identity is issued is **open (a)**.

## Shared credentials

Classification of every standing credential the product holds, under D1.
Each named exception's premise must be an instance of D1's rule (every user
who can reach that credential's data through the product has the same
entitlement); a row that is not is a defect in this record.

| Credential | Class | What it reaches, and the premise |
|---|---|---|
| E1. Splunk read-only service token (ADR-0010 TD7) | Named exception | The indexes of its read-only role. Premise: every team-group member has the same index entitlement. Unchanged |
| E2. Qdrant read-only serving credential (AGENTS.md, ADR-0007 R3) | Named exception | The manual corpus, read-only. Premise: every user may read every manual. If manuals ever become restricted per user or group, this needs a new decision before the change |
| Per-leg model gateway keys | Not a data source | Model inference only; the key reads no stored data |
| Feedback-service bearer token (ADR-0005, #804) | Service-to-service, not user data access | The feedback service's write routes (answer records, ratings), its purge action and its metrics. The service has no route that returns stored text |
| Telemetry ingest credential (ADR-0006) | Service-to-service, not user data access | The trace backend, which holds tier 1 only. At c5259460 it is derived from the backend's one operator credential, which can also query and delete there |
| z/OS bridge's single read-only SAF credential (ADR-0003 decision 3) | **Does not qualify** | Mainframe entitlements differ per user, so a shared credential would return data a caller may not be entitled to. The bridge is off and unwired today. It cannot be enabled for users until it authorizes per caller (the `denied` check) or a separate decision is recorded |
| Qdrant ingest/admin writer credential (AGENTS.md) | Service-to-service write, not user data access | Corpus ingest and admin actions; serving stays read-only. It writes the corpus, not user data |

## Guarantees and what establishes them

Review each row with the four questions of the AGENTS.md review rules.
No gateway exists at c5259460, so G1, G2 and G6 are **required of each
gateway** and enforced by nothing today. Facts marked "executed" are from the
[first review](https://github.com/yonatan895/qdrant-pdf-rag/pull/1202#issuecomment-6854398439)'s
probe of `verify_token`.

| Guarantee | State that establishes it | Who can change that state | Valid for | What forbids the counterexample |
|---|---|---|---|---|
| G1. A user-class call runs as one verified user (**required**) | The forwarded IdP token: signature, issuer, exactly this gateway's audience, expiry, and a group in `allowed_groups` ([caller verification](../team-tools.md#caller-verification)); and the entry point fixing `require_single_audience=True` | The IdP (keys, registrations, group membership); the gateway's configuration, through a deployed release only | The token's `exp` plus `leeway_s` (at most 300 s); cached keys for `jwks_ttl_s` (default 300 s, no upper bound in the library); stale keys at most `jwks_max_stale_s` after a failed fetch | `verify_token` refuses each failing check and `principal_from_scope` raises without a verified principal (`tests/test_mcp_tool_auth.py`). Executed: the library default (`require_single_audience=False`) accepts a two-audience token, so the guarantee holds only where the entry point fixes it on. **Required:** each gateway pins that setting in a test. E1 is the stated exception to per-user entitlement |
| G2. No credential crosses classes (**required**) | Each entry point serves one class and names only its own audience. With an IdP-issued workload token: TD6's single-audience rule, its token source (token exchange or on-behalf-of, or the source-clause amendment for a client-credentials grant), and a group in `allowed_groups`. The agent token carries the team group and the agent group; the agent entry point's `allowed_groups` is the agent group, so the trust model's team-group check passes and the agent group is the class boundary. With a per-workload secret (open (a)): a second verifier that accepts no IdP token | The identity team: which client or user may be issued which audience and group. The gateway's configuration, through a deployed release | As G1 for tokens; for a per-workload secret, until rotation (open (a)) | Executed: with distinct audience and group per entry point, each class's token is refused at the other's entry point. Executed: a user who is issued the agent audience and the agent group is accepted. So separation rests on what the IdP issues plus each gateway fixing the single-audience requirement; the library cannot tell the classes apart. **Required:** each agent-class gateway's acceptance shows a valid user token refused at its entry point and its own credential refused at a user entry point; the site records the IdP's issuing rule |
| G3. The agents reach no production user data, except the rated-case export (D1) | No matrix cell for a live production source; no credential for one on the agents VM; the egress fence ([agents-vm](../agents-vm.md)). For the dev pod, and for agent-authored code that runs in CI job pods after a push: the CI namespace's isolation from the product namespace (ADR-0009 §2 isolation proof) | The maintainer (this matrix and the exception); the platform and source owners (credentials); the VM administrator (fence); the platform team (CI namespace policy) | Until the fence or the namespace policy changes, a credential is issued, or the exception is widened by a new decision | `agents_vm.py start` refuses an unfenced account; the fence proof's negative controls; the isolation proof's probes from the CI namespace. Both proofs are site steps, required and **not yet run**. Nothing technical limits what an export file contains: that rests on the #1199 export tool and the ADR-0005 amendment |
| G4. Agent work reaches production only as a reviewed merge request and a signed release | *Implemented:* outside dry-run, load, deploy, ingest and validate refuse a checkout that is not a verified signed release (ADR-0009 F1). *Site record, not verifiable from this repository:* protected `main`, code-owner approvals, and a Developer-role bot token that cannot approve or merge (ADR-0009 §6). *Not built:* the gap release build and signing (ADR-0009 follow-up F6, not started) | The gap maintainer group (GitLab settings, approvers); holders of a release key; an operator, through the dev-only opt-out, which the scripts refuse without `CI_REHEARSAL=1` (that nobody in the gap sets it is a rule, not a control) | Standing; the bot token expires and is rotated | The F1 refusal and its air-gap shell suites. Because F6 does not exist, gap-developed code has no route to production yet: an upstream signed release is the only route today. ADR-0009 is accepted, pending merge of its status change; F6 is raised in priority |
| G5. Agent-class telemetry reads return no question, answer or excerpt text the product emits | The product emits none into tier 1 (ADR-0005, ADR-0006, [observability](../observability.md#data-tiers)) | Product code, through a reviewed release; any other writer into the backend, since the metrics scraping or forwarding path is a site decision (ADR-0006) | Standing | The one-time ADR-0006 qualification scan (its record notes the `_meta` stream was not scanned). The gateway adds no content filter: a free-form read returns whatever any writer stored |
| G6. The telemetry gateway cannot mutate or starve the backend (**required**) | Forward-only to search endpoints; fixed bounds on time range, rows, timeout and concurrency (D3) | The gateway's code and configuration, through a deployed release | Standing | **Required** of the gateway's record: pinning tests for the forwarded path set and for each bound |

## Amendments and unchanged clauses

| Record | Effect |
|---|---|
| ADR-0010 TD6, single-audience rule and `require_single_audience=True` at "every gateway entry point" | **Bound to agent-class entry points** whenever the workload identity is an IdP-issued token (D2): exactly one audience, `require_single_audience=True`, and the agent token carrying the team group and the agent group with `allowed_groups` set to the agent group. No change of wording |
| ADR-0010 TD6, token source ("obtained by … token exchange, or by AD FS on-behalf-of") | Both listed grants start from a user's token, and the agent class has none. A client-credentials style grant (open (a)-1, when only that grant is possible) is not in that list, so choosing it **amends this clause**; the amendment is decided and recorded with (a) |
| ADR-0010 TD6 and trust model ("verifies the forwarded IdP token … and the team group on every call") | A per-workload secret (open (a), second option) verifies no IdP token. Choosing it **amends TD6 and the trust model**; the amendment is decided and recorded with (a), before any agent-class entry point is built |
| ADR-0010 TD1 to TD5, TD7, TD8, amendments table | Unchanged. "The gateway is the authority", "read-only by construction" and "tool output is untrusted data" bind the agent class too |
| ADR-0007 R3, the agent anonymous to users | Unchanged; now the named exception E2 |
| ADR-0003 | Unchanged in text. D1 adds a condition before the bridge is enabled for users ("Shared credentials"); ADR-0003's own reactivation gate still applies |
| ADR-0006, "ClusterIP only … no Route and no extra auth layer" | Unchanged here, and the order stands: the telemetry gateway is not deployable until #1199 amends it. The amendment must cover a reader other than the operator; the external exposure of the agents' entry point (the VM is outside the cluster), constrained by the AGENTS.md boundaries; and the trace backend's NetworkPolicy admitting the gateway as a new client |
| ADR-0005, content only in the feedback store | Unchanged here. Exports on the agents VM, the full message and history, and the onward leg to the model gateway under the dev key need the amendment in #1199 (its item 6) |
| ADR-0009 (accepted 2026-11-13, pending merge of its status change) | Unchanged. G3 relies on its §2 and G4 on its §1 and §6. The dev pod and CI job pods of its CI namespace get no gateway entry point under this ADR, so its §2 isolation proof is unchanged |
| [agents-vm](../agents-vm.md) fence allow-list | Unchanged here; one host is added only with the telemetry gateway's record |

## Open points

- **(a) How the agents' workload identity is issued at a site.** Options:
  - **(a)-1, an IdP service token** obtained by token exchange or
    on-behalf-of (TD6's sources), carrying exactly one audience (the
    agent-class entry point's), a non-empty `sub`, the team group and the
    agent group. The library then needs no change and TD6 binds unchanged.
    If the identity team can only issue a client-credentials grant, that
    grant carries the same claims but needs the TD6 source-clause
    amendment.
  - **(a)-2, a per-workload secret** held on the agents VM, with a second
    verifier in the gateway code base, its own storage, rotation and expiry
    rules, and the TD6 and trust-model amendment.
  *Question for the identity team:* can the site IdP issue a service client
  an access token by token exchange or on-behalf-of that carries exactly one
  audience, a non-empty `sub`, the team group, and a group claim no user can
  be issued, signed with a key in the JWKS the library already reads? If
  yes, (a)-1 needs no library change. If only a client-credentials grant is
  possible, say so and it is recorded as the source-clause amendment. If
  neither, (a)-2 applies. The maintainer chooses after that answer, and the
  choice records any amendment.
- **(b) A scoped Splunk view of the product's own logs for the agents.**
  *Question for the platform's Splunk owner:* can a token be issued to a
  non-human identity that searches only the product's own log streams and
  nothing else? The maintainer decides whether to grant the cell once such a
  token exists. Until then the agent class has no Splunk access.

## Required follow-up

1. The telemetry gateway's decision record and design (#1199), meeting D3,
   G2 and G6, with the ADR-0006 amendment (reader, external exposure,
   backend NetworkPolicy) and the [observability](../observability.md) and
   fence updates.
2. The ADR-0005 amendment for rated-case exports (#1199), including the
   onward leg to the model gateway.
3. Open point (a) and its ADR-0010 amendment, before any agent-class entry
   point is built.
4. ADR-0009 follow-up F6 (raised in priority), before any gap-developed
   change can reach production.

## Consequences

- Every later gateway states which caller classes it serves, per operation,
  and proves G2, and G1 where it serves the user class, in its acceptance.
- A new shared read-only credential needs a named exception with its
  premise; a source with per-user entitlements needs per-user
  authentication.
- The agents see production through content-free telemetry and rated-case
  exports only. A defect visible only in other production user data is
  diagnosed by a person.
- Two credential sets and two audiences per source served to both classes,
  each with its own rotation.
- Verification: the prose row of
  [live-stack](../live-stack.md#verification-minimums) for this record.
