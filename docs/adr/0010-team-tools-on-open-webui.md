# ADR-0010: Team tools on the same Open WebUI instance, through separate Team models

- **Status:** accepted (maintainer, 2026-10-31). The decisions TD1 to TD5 are
  recorded in [#1075 comment 6548196723](https://github.com/yonatan895/qdrant-pdf-rag/issues/1075#issuecomment-6548196723),
  which accepted every recommendation of the team-tools plan. Tracker #1075;
  children #1077 (Splunk), #1078 (GitLab) and #1088 (identity). This record
  **amends** [ADR-0007](0007-open-webui-docs-front-end.md) (its decision
  paragraph, V2, R2 and one no-go item) and item 6 of
  [ADR-0008](0008-open-webui-main-docs-platform.md), as listed under
  "Amendments" below. Accepting it enables nothing: until the registration PR
  (T4) merges, the chart keeps tool servers, tools and model connections off,
  and that PR stays default-off.
- **Context:**
  - ADR-0007 deferred team tools (Splunk, GitLab, mail) to their own ADR,
    "likely on a separate instance", and fixed the rule "this instance holds
    no tool, tool server or model connection". ADR-0008 item 6 kept that rule
    for the default-on platform until this ADR.
  - The caller-verification library (#1076, `src/mainframe_rag/mcp/tool_auth.py`)
    makes each gateway verify the IdP token and the team group itself. Open
    WebUI's own access control is therefore no longer the only control in
    front of Splunk or GitLab.
  - A second instance would double the OIDC client, Route, PVC, re-qualification
    (P1) and site inputs for at most the same 100 users.
  - The Kind trial (#1073) showed that Open WebUI v0.12.3 registers MCP tool
    servers from the environment (`TOOL_SERVER_CONNECTIONS`, `auth_type:
    system_oauth`) and forwards the signed-in user's access token, never the ID
    token.
- **Decision:**
  1. **TD1, one instance, separate Team models.** Team tools run on the same
     Open WebUI instance as the docs platform. Each tool set is offered only
     through its own "Team" model, visible to the team group. The **Docs
     model stays tool-free**: it is still only the hash-checked docs Pipe over
     the agent, with no tool, tool server or Function attached. Mail, when it
     is approved, is a separate Team model.
  2. **TD2, the agent image.** Each gateway is its own Deployment running the
     existing agent image with its own module entry point
     (`python -m mainframe_rag.gateways.<name>`). No new image, dependency,
     pin or licence scope.
  3. **TD3, Open WebUI's native tool loop.** A Team model is Open WebUI's
     built-in tool-calling loop over the designated reasoning model, reached
     through LiteLLM with Open WebUI's own per-leg key. The loop runs outside
     this repository's runtime: the repository adds no orchestration layer.
  4. **TD4, GitLab per-user tokens in Vault.** Each user's own `read_api`
     personal access token lives in the site Vault. The GitLab gateway reads
     it at request time through Vault JWT auth with the user's own verified
     IdP token, under an identity-templated policy. The gateway keeps no token
     store. A token whose scopes cannot be checked (no
     `personal_access_tokens/self`, older GitLab) is **refused** with a fixed
     code (TD4b).
  5. **TD5, Splunk attribution by hash.** Each Splunk search carries a stable
     hash of the caller's `sub` and the request id in an SPL comment. The
     hash-to-user map exists only in the gateway's decision log. Raw identity
     never enters SPL or Splunk's `_audit`.

## Trust model

- **The gateway is the authority.** It verifies the forwarded IdP token
  (signature, issuer, audience, expiry) and the team group on every call
  ([caller verification](../team-tools.md#caller-verification)). Open WebUI
  model and tool access grants are user experience and defence in depth only.
  `X-OpenWebUI-*` headers never authorize.
- **Read-only by construction.** No gateway defines a write tool. Each backend
  credential is read-only (Splunk role, `read_api` PAT); the gateways never
  accept a credential through chat.
- **Tool output is untrusted data.** Every result is capped, stripped of
  control and bidi characters, redacted for token- and password-shaped strings,
  and framed as data, as manual excerpts are for the agent. Errors are fixed
  `{code, message}` pairs; upstream text is never returned. Logs carry ids,
  decisions, counts and timings only.
- **No LLM in a gateway.** Search still never calls an LLM; the only model a
  Team model uses is the designated reasoning model.

## Amendments

ADR-0007 and ADR-0008 conditions remain release gates (ADR-0008 item 2).
Only the clauses below change, and only while `teamTools` is enabled.

| Clause | Was | Now |
|---|---|---|
| ADR-0007 decision, last sentences | Team tools "likely on a separate instance"; "this instance holds no tool, tool server or model connection" | Team tools are on this instance (TD1). The **Docs model** holds no tool, tool server or Function. Tool servers exist only for Team models. |
| ADR-0007 V2, model connections | no gateway or LiteLLM key, no OpenAI or Ollama connection; the only model is the docs Pipe | One OpenAI-compatible connection, to LiteLLM only, with Open WebUI's own per-leg key from a Secret, limited to the designated reasoning model. Its base model is selectable by no user directly; only Team models restricted to the team group use it. |
| ADR-0007 V2, tool servers | none | Tool servers come only from the environment (`TOOL_SERVER_CONNECTIONS`): repository-owned gateways, `type: mcp`, `auth_type: system_oauth`, granted to the team group. |
| ADR-0007 R2, egress | agent Service, DNS, IdP | also the gateway Services and LiteLLM |
| ADR-0007 no-go, "a model other than the agent becomes reachable" | any such model stops the platform | stops it unless it is the designated reasoning model, reached only through a Team model by team-group members |
| ADR-0008 item 6 | no tool, tool server or model connection until this ADR | as this table |
| ADR-0008, "What does not change" | "The Pipe is the only model. Open WebUI never reaches Qdrant or a gateway." | The Pipe is the only **Docs** model. Open WebUI never reaches Qdrant; it reaches LiteLLM and the team-tool gateways only for Team models. |

Unchanged, and checked by the T4 acceptance checks:

- the docs Pipe is the only Function; the installer still refuses any other,
  and the Pipe still declares no `__oauth_token__`;
- user, workspace and direct tools, user-created models, Ollama, the
  Pipelines server and community Functions stay off;
- the proxy allow-map stays closed to tool-server and connection config APIs,
  because registration comes from the environment;
- task features stay off, and background tasks never run on a Team model or
  with tools (a tool-less task model);
- the Docs model lists zero tools, and a user outside the team group sees no
  Team model.

## Considered options

- **A separate Open WebUI instance** (the research recommendation of
  2026-10-24). Rejected: it doubles the OIDC client, Route, PVC, P1
  re-qualification and site inputs, while the gateway, not the instance, is
  the boundary.
- **Trusting Open WebUI's identity headers or its own audience.** Rejected
  (#1088): Open WebUI would become the only control in front of Splunk and
  GitLab.
- **A repository-owned tool loop.** Rejected: it would be a new orchestration
  layer in this repository.
- **Separate gateway images.** Rejected for now: extra pins, factory jobs and
  licence scope with no isolation gain over separate Deployments.
- **A gateway-side token store for GitLab, or raw UPNs in SPL.** Rejected in
  favour of Vault per-user reads (TD4) and hashed attribution (TD5).

## Consequences

- One instance carries both tracks. A Team-model defect is rolled back by
  disabling `teamTools` (the Docs model is unaffected); the console stays the
  docs HA path.
- **Tool output lands in chat storage.** Splunk events and GitLab content
  returned by a tool are kept in Open WebUI chats, under the 30-day purge (S2)
  and the PVC and backup rules (S3). The maintainer's S3 acceptance
  (2026-10-24) named manual excerpts and pasted incident text; the security
  or data owner must confirm it covers tool output before T4 is enabled at a
  site (go-live item 6 below).
- New site inputs before go-live:
  1. identity (#1088): access-token claims (the site probe), the audience
     path (RFC 8693, AD FS on-behalf-of or extra audiences), gateway client
     registrations, JWKS URL, lifetimes and refresh tokens, and the team
     group's domain-qualified name;
  2. platform: a LiteLLM per-leg key for Open WebUI's Team models, and
     tool-call parsing on the reasoning model;
  3. Splunk: search-head URL and CA, a read-only role and service token,
     egress, and the site's templates;
  4. GitLab and Vault: base URL and CA, a JWT auth mount trusting AD FS, the
     per-user policy and KV path, and egress;
  5. namespace quota and NetworkPolicy CIDRs for two small pods;
  6. owner confirmation that S3 covers tool output in chats.
- Verification: the prose row for this record; the HTTP/lifecycle row for the
  gateway runtime and gateways; the packaging/deploy row and the Open WebUI
  Kind lane for registration (T4), per
  [live-stack](../live-stack.md#verification-minimums).
