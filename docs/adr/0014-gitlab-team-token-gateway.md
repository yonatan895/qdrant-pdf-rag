# ADR-0014: GitLab gateway with one read-only team token, and a bearer-mode caller exception

- **Status:** proposed. The maintainer approved the design on 2026-11-21 (in
  session; recorded on [#1078](https://github.com/yonatan895/qdrant-pdf-rag/issues/1078)).
  The maintainer accepts this record by merging it. Tracker #1075; build
  issue #1078; identity #1088. This record **amends**
  [ADR-0010](0010-team-tools-on-open-webui.md) (TD4, TD4b's scope, the trust
  model's first bullet and its V2 tool-server row, for this gateway only) and
  [ADR-0011](0011-gateway-caller-classes.md) (the GitLab user-class cell of
  the policy matrix and the shared-credentials table, which gains E4 and B1),
  as "Amendments and unchanged clauses" lists. It also supersedes the
  per-user requirement in #1078's issue body. Accepting it deploys nothing:
  the gateway ships default-off, and its chart and Open WebUI registration
  are a later pull request. The maintainer's review-round decisions of
  2026-11-21 (PR #1333) are part of this record:
  - D3: a long job log is streamed and its end kept, not refused;
  - D3: a read-only tool for issue comments;
  - D4: bearer-mode limit defaults.
- **Context:**
  - ADR-0010 TD4 gives each user their own `read_api` personal access token,
    stored in Vault and read through Vault JWT auth with the user's own
    IdP token. That needs a Vault JWT mount trusting AD FS, per-user
    enrollment, and #1088's per-gateway token audiences (TD6). The identity
    team has not answered #1088.
  - The site's GitLab has no built-in MCP server (its version is recorded
    off GitHub), so the gateway stays REST v4, as ADR-0010 planned.
  - The maintainer (2026-11-21): the GitLab tools serve the mainframe team,
    whose members are in one organisation and read the same repositories. One
    read-only team token is simpler than per-user tokens. Write actions are
    not wanted.
  - ADR-0011 D1 allows a shared read-only credential only as a named
    exception whose premise is equal entitlement. ADR-0010 TD7 (Splunk, E1)
    and ADR-0012 E3 (z/OS) are the precedents. ADR-0012 D4 is the precedent
    for a bearer secret plus an ingress NetworkPolicy as caller
    authentication.

## Decision

1. **D1, one team token (named exception E4).** The gateway reads GitLab
   with one **group access token**: Reporter role, scope `read_api` (plus
   `read_repository` only if a later tool needs it), mounted from a Secret
   as `GITLAB_TOKEN`, never in git, flags or logs. The gateway looks the
   token up through a one-method `GitLabTokenProvider`.
   `StaticGroupTokenProvider` returns the team token for every caller. A
   per-user provider (TD4's Vault design) can replace it later without
   changing the tools, the REST client or the caller check.
   *Premise:* every person who can reach the gateway's tools can already read
   every allowlisted project with their own GitLab account. The allowlists
   (D3) are cut to make this true. If a project some team member cannot read
   joins the allowlist, or a wider group can reach the tools, this needs a
   new decision first (per-user provider).
2. **D2, a start-up scope check that fails closed.** Before serving, the
   gateway reads `GET /api/v4/personal_access_tokens/self` with the team
   token. It refuses to start unless the token is active and not revoked,
   holds `read_api`, and holds **no scope outside** `read_api` and
   `read_repository`. This is an allowlist: `api`, `write_repository`,
   `sudo`, `admin_mode` and any scope GitLab adds later are refused. A
   GitLab that does not answer this endpoint, or answers it in an unexpected
   shape, is a refusal too (TD4b kept). The check runs at start-up only: a
   token's scopes cannot change in place, and a rotated Secret takes effect
   at the next pod start, which runs the check again.
3. **D3, a full read-only tool set.** The tools read projects, repository
   trees and files, code search in one project, commits, merge requests and
   their diffs, issues and their comments (notes; the team documents its work
   in issues), pipelines, pipeline jobs, and the end of a job log. Merge-request
   comments are not read. No write tool exists. The REST client:
   - issues GET only, under `/api/v4` on the configured https origin, with
     no redirects and no proxy environment, verifying the site CA;
   - refuses any project outside `allowed_groups` (at a path-segment
     boundary, case-insensitively) and `allowed_projects` before any call;
     it then checks the `path_with_namespace` GitLab returns, because a
     moved project still answers under its old path, and uses the numeric id
     after that;
   - uses project-scoped endpoints only, never instance-wide search;
   - caps every response. A file and a JSON answer each have a byte cap, and a
     larger body is refused (a list page is first asked for again with fewer
     rows). The page size and the depth a list reads (`start`) are capped too;
   - streams a job log, of any length, and keeps only its end in memory. It
     stops reading at a byte ceiling or a time ceiling below the tool timeout.
     It returns the last whole lines within the result cap, behind a fixed
     marker line saying how much came before, and says when the end was not
     read. Partial lines at a cut are dropped, so a cut cannot leave a secret
     fragment that redaction would miss, and private keys are tracked across
     the whole stream, so a tail that starts or stops inside a key shows none
     of it;
   - returns selected fields only (usernames, no e-mail addresses), redacted,
     capped and framed as untrusted data by the shared gateway runtime, with
     fixed `{code, message}` errors.
4. **D4, caller authentication by switch: `GITLAB_AUTH_MODE=oidc|bearer`.**
   - **`oidc` (default):** ADR-0010's caller verification, unchanged. The
     entry point fixes `require_single_audience=True` (TD6), whatever the
     configuration file says.
   - **`bearer` (named exception B1, pilot):** one caller service, Open
     WebUI, presents one shared secret (`GITLAB_GATEWAY_BEARER_SECRET`, at
     least 32 characters, from a Secret), compared in constant time. The
     gateway then knows no user: the caller is `service:openwebui`, so the
     rate and concurrency limits and the decision log cover the whole
     instance, and per-user attribution exists only in Open WebUI's chat
     history. In this mode the defaults are 4 calls in flight and 120 calls a
     minute for the instance, unless the configuration sets them. `oidc` mode
     keeps the runtime's per-user defaults. Required guards, which the deployment pull request enforces at
     render and deploy:
     - (a) an ingress NetworkPolicy admitting only `app: rag-openwebui` pods;
     - (b) one of these holds: every value in `OPENWEBUI_OIDC_ALLOWED_ROLES`
       and `OPENWEBUI_OIDC_ADMIN_ROLES` is an approved team role (as ADR-0012
       D3(b)), so only team members can sign in; or, when other users sign in
       for Docs, the GitLab Team model and its tool-server entry are granted
       to the team group only. The second is weaker, because Open WebUI is then
       the only control in front of GitLab: an admin who makes the model
       public exposes it. It is accepted for the pilot only.
     *Exit:* when #1088 delivers single-audience gateway tokens, the gateway
     switches to `oidc` with no code change.
5. **D5, deployment (later pull request).** A default-off chart Deployment
   on the agent image (TD2): `restricted-v2`, ClusterIP only, an ingress
   NetworkPolicy from Open WebUI only, and egress to cluster DNS and the
   internal GitLab on TCP 443 only. In `oidc` mode egress also reaches the
   IdP's JWKS. Vault egress is not needed while E4 holds.
6. **D6, the Team model's tool loop (TD3, unchanged).** The maintainer
   confirmed that the served reasoning model supports native tool calling.
   The registration pull request wires Open WebUI's own per-leg LiteLLM key
   Secret and adds a test that the key reaches the model connection.

## Shared credentials (adds to ADR-0011's table)

| Credential | Class | What it reaches, and the premise |
|---|---|---|
| E4. GitLab group access token (D1, D2) | Named exception | The allowlisted projects, read-only (`read_api`, Reporter). Premise: every person who can reach the tools can already read every allowlisted project with their own account. If not, per-user tokens are required, and that needs a new decision first |
| B1. GitLab gateway bearer secret (D4) | Service-to-service caller credential, pilot only | Calls the gateway's read-only tools as `service:openwebui`. Held only by Open WebUI. Safe only with D4's guards (a) and (b) |

## Guarantees and what establishes them

| Guarantee | State that establishes it | Who can change that state | Valid for | What forbids the counterexample |
|---|---|---|---|---|
| G1. The gateway never writes to GitLab | GET-only client; no write tool; the team token's scopes (D2) | Gateway code, through a release; the GitLab group owner, who issues the token | Standing; the scope check re-runs at every start | `tests/test_gateway_gitlab.py`: every tool against a fake GitLab that records any non-GET; the scope-check table (write, unknown and missing scopes refused). GitLab itself refuses a write with a `read_api` token (site check) |
| G2. Only allowlisted projects are read | `allowed_groups` and `allowed_projects`; the resolved-path check (D3) | The operator, through a deployed release | Until an allowlist changes | Tests: near-miss paths (`team-other`, `teamx`, deeper or longer names) refused before any call; a moved project resolving elsewhere refused |
| G3. Only team members reach the tools | `oidc`: the verified token and group (ADR-0010). `bearer`: the NetworkPolicy, the shared secret, and guard (b) | The IdP; the platform (NetworkPolicy); Open WebUI admins (role lists, model grants) | `oidc`: the token's lifetime. `bearer`: until a role list, grant or policy changes | `oidc`: the tool_auth attack tests plus the pinned single-audience test. `bearer`: wrong, truncated and missing secrets refused (tests); guards (a) and (b) are enforced by the deployment pull request and the site premise review |
| G4. No secret or content in logs | Decision log fields: ids, codes, counts, timings | Gateway code, through a release | Standing | Tests: the team token, the bearer secret and upstream error text never appear in captured logs |

## Amendments and unchanged clauses

| Record | Was | Now |
|---|---|---|
| ADR-0010 TD4 | Each user's own `read_api` PAT, read from Vault at request time | For the GitLab gateway: one group access token (E4), behind `GitLabTokenProvider`. The Vault per-user design stays the documented upgrade path (a provider) |
| ADR-0010 TD4b | A PAT whose scopes cannot be checked is refused | Unchanged in effect, applied to the team token at start-up (D2), with an explicit scope allowlist |
| ADR-0010 trust model, "the gateway is the authority … `X-OpenWebUI-*` headers never authorize" | The gateway verifies the IdP token and group on every call | Unchanged in `oidc` mode. In `bearer` mode (B1), the gateway authenticates Open WebUI as a service, and team membership rests on D4's guards. Headers still never authorize |
| ADR-0010 TD6 | Single-audience tokens at every gateway entry point; the reason given is Vault replay | Unchanged in `oidc` mode, fixed by the entry point. Not applicable in `bearer` mode, which verifies no IdP token |
| ADR-0010 amendments table, ADR-0007 V2 tool servers (`auth_type: system_oauth`) | `system_oauth` only | Also a static bearer key (Open WebUI's `bearer` auth type) for the GitLab gateway in bearer mode (B1); the registration pull request verifies the type against the pinned Open WebUI |
| ADR-0011 policy matrix, GitLab, user class | ADR-0010 TD4, the user's own token | E4 through this gateway; agent class unchanged (no access through this gateway) |
| ADR-0011 shared credentials | No GitLab row | E4 and B1 (table above) |
| ADR-0011 D1 ("the product acts only as the signed-in user") and G1 ("a user-class call runs as one verified user") | Every user-class gateway call carries one verified user | Unchanged in `oidc` mode. In `bearer` mode, B1 is the stated exception: calls run as `service:openwebui`, no user is verified by the gateway, and team membership rests on D4's guards (G3 above) |
| #1078 issue body, "act as the end user, never as a shared write-capable account" | Per-user tokens | One shared **read-only** token (E4); write actions would still need per-user tokens |
| ADR-0010 TD1–TD3, TD5, TD7, TD8; ADR-0011 D2–D4, G2–G6 | | Unchanged |

## Open site checks (before go-live)

Items 1 and 5 and the GitLab version are answered by the read-only site
probe (#1343), run in the gateway pod with the gateway's configuration:

```sh
python -m mainframe_rag.gateways.gitlab --site-probe \
  --job <allowlisted project>:<job id with a long log> \
  --issue <allowlisted project>:<iid with label or state changes> [--receipt <writable path>]
```

Its receipt holds one JSON line per check with counts, version integers and
fixed reason codes (never paths, ids, text or the token); the exit code is 1
when a check failed. `gitlab.version` passes on 17.x (another major is
`inconclusive`: a human decides); `gitlab.token_scopes` passes when the
start-up check passes and the token holds `read_api` alone (D1);
`gitlab.job_trace_stream` when the named log streams to its end within
`max_job_log_read_bytes`/`max_job_log_read_s`; `gitlab.issue_notes_filter`
when the filter removes the system notes the unfiltered first page holds
(`filter_ignored` and `no_system_notes` are `inconclusive`; `filter_refused`
fails).

1. A group access token with Reporter and `read_api` can be created for the
   team's group, and `personal_access_tokens/self` answers for it (the
   gateway refuses to start otherwise; `gitlab.token_scopes`).
2. The E4 premise holds for the allowlists, recorded at the site, and is
   re-checked when the allowlists or the team change.
3. Whether people outside the team sign in to Open WebUI, which selects
   D4(b)'s form in `bearer` mode.
4. The site CA bundle, the GitLab egress rule, and the token's expiry and
   rotation procedure.
5. The site GitLab accepts `activity_filter=only_comments` on issue notes, or
   ignores it (`gitlab.issue_notes_filter`). If it refuses the parameter
   (`filter_refused`), drop it: the gateway filters system notes itself as
   well. The job-log endpoint streams a long log within the read ceilings
   (`gitlab.job_trace_stream`).

## Consequences

- Team members get read-only GitLab answers without Vault enrollment or
  #1088, under one standing credential whose reach is the allowlist.
- GitLab's own audit log shows the token's bot user, not the person. In
  `bearer` mode the gateway's log shows only the instance; per-person
  attribution is the Open WebUI chat history.
- Rate limits in `bearer` mode apply to the whole instance. The defaults (4 in
  flight, 120 a minute) suit a small team, and the operator may set others.
- Descriptions and note bodies are cut at 32768 characters, not the runtime's 4096
  (maintainer, 2026-11-21). The GitLab result cap is 128 KiB, not the runtime's
  64 KiB (maintainer, 2026-11-21, round 3): it holds one full field of up to
  three-byte characters; a field of four-byte or bracket characters is clipped
  to fit. A list result that leaves rows out names the `start` of the first
  row not shown, so following it reads every row once.
- Issue comments and job logs are untrusted text from many authors. They
  reach the model only redacted and framed as data, like every other result.
- Verification: the prose row for this record; the HTTP/lifecycle row for
  the gateway ([live-stack](../live-stack.md#verification-minimums)).
