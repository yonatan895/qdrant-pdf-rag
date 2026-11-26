# ADR-0015: Splunk gateway over the site's Splunk MCP Server, templates only

- **Status:** approved by the maintainer 2026-11-25 (in session); accepted by
  merging. The maintainer set the direction on 2026-11-25 (in session): the
  site runs Splunk's own MCP Server app, it accepts raw SPL, and its token
  user reads exactly the indexes every team member can read. The same day
  the maintainer approved bearer mode as named exception B2 (D7), allowlisted
  macros with a start-up check (D9) and gateway-wide caps (D10). Tracker #1075; build
  issue #1077; identity #1088. This record **amends**
  [ADR-0010](0010-team-tools-on-open-webui.md) (TD5's form, TD7's credential
  and go-live input 3) and [ADR-0011](0011-gateway-caller-classes.md) (the
  E1 row of the shared-credentials table, which also gains B2), as
  "Amendments and unchanged clauses" lists. It also supersedes the "Splunk
  REST only, no Splunk-side install" requirement and the escaping wording in
  #1077's issue body. Accepting it deploys nothing: the gateway ships
  default-off, and its chart and Open WebUI registration are a later pull
  request, as for GitLab ([ADR-0014](0014-gitlab-team-token-gateway.md) D5).
  Status note 2026-11-25: the deployment pull request (#1077 part 2) adds
  the default-off chart objects, NetworkPolicy and Open WebUI registration
  for this gateway ([deploy](../deploy.md#splunk-gateway-deploy)). The
  decisions above are unchanged.
- **Context:**
  - #1077 planned a gateway that calls Splunk REST (`/services/search/jobs`)
    with a read-only service token and no Splunk-side install.
  - The site's platform team installed Splunk's MCP Server app (Splunkbase
    7931, version 1.0.5 at the site) on the Splunk management port at
    `/services/mcp`. Clients authenticate with
    `Authorization: Bearer <encrypted token>`. Splunk's documentation for
    the 1.x line says encrypted tokens are for MCP only and cannot call the
    Splunk REST API. The credential the site can issue therefore reaches
    Splunk only through the MCP Server: a REST backend is impossible with it.
  - The MCP Server's search tool is `splunk_run_query` (an older listing
    called it `run_splunk_query`). Splunk documents that it fails, rather
    than truncates, a search that runs past one minute or returns more than
    1000 events, and that it may refuse commands it considers unsafe. Running
    a tool needs the capability `mcp_tool_execute` on the token user's role.
    Other tools (`splunk_get_*`, `saia_*`) exist. Besides the run-query tool,
    this gateway calls only the knowledge-object tool, once at start-up and
    in the site probe, to read macro definitions (D9).
  - The documentation does not publish the run-query tool's arguments other
    than the query, its result shape, or whether the server uses sessions or
    event streams. These are site facts (D5).
  - ADR-0010 TD7 already allows one Splunk service token on the
    equal-entitlement premise (named exception E1 in ADR-0011). The
    maintainer confirmed the premise for the MCP token user on 2026-11-25.
  - The team's searches use Splunk macros extensively (maintainer,
    2026-11-25). The shared token is one Splunk user, so Splunk's per-user
    search quota covers every caller together.

## What the protocol and the vendor documents establish

Checked on 2026-11-25 against the MCP specification and Splunk's MCP Server
documentation.

| Item | Status | Source |
|---|---|---|
| Streamable HTTP: every message is a POST with `Accept: application/json, text/event-stream`; a request is answered with one JSON object or an event stream, and the client must support both; a notification is answered 202 | Confirmed | MCP 2025-06-18, transports |
| In an event stream the server may send requests and notifications before the response, and should close the stream after it | Confirmed | MCP 2025-06-18, transports |
| `initialize` negotiates the version; a client that does not support the server's answer should disconnect; `notifications/initialized` follows | Confirmed | MCP 2025-06-18, lifecycle |
| `MCP-Protocol-Version` on every request after initialize; a server answers 400 to an invalid or unsupported value | Confirmed | MCP 2025-06-18, transports |
| `Mcp-Session-Id`: optional, visible ASCII, echoed on every later request; 404 on a request carrying it means the session ended and the client must initialize again | Confirmed | MCP 2025-06-18, transports |
| `tools/list` pages with `nextCursor`; a `tools/call` result has `content`, optional `structuredContent` and `isError` | Confirmed | MCP 2025-06-18, tools |
| 2025-11-25 lets a server close an event stream before its response and expects the client to resume with GET and `Last-Event-ID` | Confirmed; not implemented | MCP 2025-11-25 changelog (SEP-1699) |
| The current revision, 2026-07-28, is stateless: no `initialize`, no `Mcp-Session-Id`, a `server/discover` method and `Mcp-Method`/`Mcp-Name` request headers | Confirmed; not implemented | MCP 2026-07-28 changelog |
| The site's server negotiates 2025-06-18 | Maintainer report | Site |
| Encrypted tokens are MCP-only; `mcp_tool_execute` runs tools; Splunk recommends per-user tokens | Confirmed | Splunk MCP Server 1.x documentation |
| `splunk_run_query` fails past one minute or 1000 events and may refuse unsafe commands | Confirmed | Splunk MCP Server 1.0 tools page |
| The run-query tool's arguments besides the query (1.3 and later add a workload pool) | Unconfirmed | Site probe |
| The result shape (`structuredContent` or JSON text, and the key holding rows) | Unconfirmed | Site probe |
| Whether the server issues a session or answers with event streams (a community reading of a later 2.x release says neither) | Unconfirmed | Site probe |
| Whether the tool adds its own leading `search` keyword, and whether its guardrail accepts an SPL comment | Unconfirmed | Site probe |
| A macro with arguments is defined under `name(<count>)` and substitutes `$arg$` in its definition; `srchJobsQuota` limits one user's concurrent searches and is set on the role (default 3) | Confirmed | Splunk Knowledge Manager and `authorize.conf` documentation |
| The knowledge-object tool (`splunk_get_knowledge_objects`, type `macros`): its argument names and row shape | Tool name: maintainer report. Arguments and rows: unconfirmed | Site probe (`splunk.macros`, the tool's input schema) |

## Decision

1. **D1, the backend is the Splunk MCP Server's run-query tool.** The gateway
   is a small MCP client of the site's server (`SplunkMcpClient` over the
   shared `UpstreamClient`): POST only, no redirects, no proxy environment,
   the configured https origin and path, a byte cap and the site CA. It sends
   `initialize`, `notifications/initialized`, `tools/list` and
   `tools/call` for the configured run-query tool, plus, at start-up and in
   the site probe only, one `tools/call` of the knowledge-object tool (D9).
   Nothing else. It speaks MCP
   2025-06-18 and accepts a server that answers 2025-06-18 or 2025-03-26.
   Any other answer is refused (D4). It reads JSON or event-stream answers,
   stops reading an event stream at the response to its own request (so a
   stream the server keeps open cannot hold a call), echoes a session id
   when one is issued, and renews an expired session once per call. One
   session serves the process.
2. **D2, templates only.** The model never writes SPL (#1077 and ADR-0010
   unchanged). Each site template becomes one tool with typed parameters.
   The gateway composes the search:
   `search index="…" earliest=… latest=… (<template terms>) <template pipeline> | head <rows>`,
   then the TD5 comment (D6). The template's search terms are
   parenthesised because Splunk evaluates `OR` before the implicit `AND`: an
   `OR` in a template cannot bind to the gateway's own terms.
   The index clause comes from the template's fixed indexes or one
   parameter whose values are the configured `allowed_indexes`. For a
   template flagged `index_from_macro`, the index comes from an allowlisted
   macro the start-up check proved (D9), and the gateway writes no index
   clause of its own. The time
   bounds and the row cap are written into the SPL itself, so they do not
   depend on any unconfirmed tool argument. Rows are at most the configured
   cap, which is at most 1000 and at most the runtime's `max_rows`. The
   window is at most the template's and the global maximum; a snapped start
   counts its whole snap unit.
3. **D3, parameters are rejected, never escaped, and templates are linted.**
   Each type admits a small character set with no quote, backslash, pipe,
   bracket, backtick, equals sign, space or control character: identifiers
   (`[A-Za-z0-9_.:-]{1,64}`), enums, integers in a range, and index names
   from the allowlist. Time values follow a small grammar (`now`, `-15m`,
   `-24h`, `-7d@d`, `@d`). A string value goes into SPL only inside the
   double quotes the template wrote. When the configuration loads, each
   template is checked: every placeholder is declared and every parameter
   used; the text is printable ASCII with balanced quotes and parentheses; it names no index
   or time bound; it has no subsearch and no backtick span other than a call
   of an allowlisted macro (D9), so no comment; and it uses none of
   a list of commands that write, run other searches or read outside the
   composed index clause. This check is a lint over trusted, operator-written
   text. The token user's Splunk role remains the boundary.
4. **D4, a start-up check that fails closed.** Before serving, the gateway
   initializes a session, requires a supported protocol version (a version
   in the answer outside the allowlist, or a JSON-RPC error that refuses the
   requested version, is `protocol_unsupported`), lists the
   tools, and requires the run-query tool with the query argument and every
   configured constant argument in its input schema, and no required
   argument it does not send. When a template calls a macro, it runs the
   macro check (D9). Otherwise it exits 3 with a fixed reason. A
   server that cannot answer is a refusal too. The check runs at every pod
   start.
5. **D5, a site probe answers the unconfirmed items.**
   `python -m mainframe_rag.gateways.splunk --site-probe` runs in the gateway
   pod with the gateway's configuration. It prints a text-free receipt (the
   shared probe receipt rules) and one site-private block of schemas and
   key/type shapes. The block also names each allowlisted macro (operator
   configuration) with its D9 verdict. The probe never prints the token, a
   row, a field value, an index name, a query or a macro definition; its
   only numbers from site data are 0-or-1 event counts.
6. **D6, TD5 attribution as an SPL comment, with an off switch.** Each
   search ends with an SPL inline comment (Splunk's triple-backtick syntax)
   holding `mfr sub_hash=<16 hex> req=<request id>`. Whether the run-query guardrail accepts a comment
   is a site check. If it refuses one, `attribution_comment: false` drops
   the comment; attribution then exists only in the gateway's decision log
   (request id to `sub`), and Splunk's own records show only the token
   user.
7. **D7, caller authentication by switch: `SPLUNK_AUTH_MODE=oidc|bearer`.**
   `oidc` (default) is ADR-0010's caller verification, with
   `require_single_audience=True` fixed by the entry point. `bearer` is
   named exception **B2**, approved by the maintainer on 2026-11-25 (in
   session), the Splunk twin of ADR-0014 B1. One shared secret
   (`SPLUNK_GATEWAY_BEARER_SECRET`) is held only by Open WebUI, with the same
   deployment guards as ADR-0014 D4 (a) and (b). In bearer mode the caller is
   `service:openwebui`: the per-caller limits cover the whole instance, and
   the TD5 hash is the same for every search, so per-person attribution is
   only Open WebUI's chat history. The bearer-mode per-caller defaults are 3
   searches in flight and 60 a minute, unless the configuration sets them.
   `oidc` mode keeps the runtime's per-user defaults. Both modes also apply
   D10's gateway-wide caps. The tool timeout defaults to 60 seconds for this
   gateway, and the backend timeout must be below it.
8. **D8, results are untrusted data.** `isError` results, including Splunk's
   one-minute and 1000-event failures, become the fixed code
   `search_refused`; a result whose rows cannot be found is
   `upstream_protocol`. Rows go through the shared runtime: redacted, capped
   and framed as untrusted. Logs carry ids, codes, counts and timings only.
9. **D9, allowlisted macros, checked at start-up (maintainer decision,
   2026-11-25).** The team's searches use Splunk macros extensively, so a
   template may call the macros the operator lists in `allowed_macros` (exact
   names): `` `name` `` or `` `name(arg,…)` ``, where an argument is a
   parameter placeholder, a literal token or a double-quoted literal with no
   placeholder. Parameters still never carry a backtick (D3). Any other
   backtick span, a comment included, is refused; the gateway's own TD5
   comment is the only exception, and the gateway appends it after the
   checks. When the configuration loads, each call is parsed and checked
   against the allowlist. Before serving, the gateway reads the definitions
   once with the knowledge-object tool (`macro_source`: by default
   `splunk_get_knowledge_objects` with `type` = `macros`; the tool and
   argument names and the row shape are configurable because the site has not
   confirmed them). It expands every call recursively: arguments are
   substituted, a nested macro must be allowlisted too, the depth is bounded
   (5), and a cycle is refused. The template rules (D3) then run on each
   expansion and on each template's whole expanded search, one more call
   expanded at a time. That way a macro after the template's own pipe, whose
   definition carries no pipe, is still checked. In an expansion:
   - refused commands, subsearches and time modifiers are refused;
   - an `index` term is allowed only with `=`, `::` or `IN`, and only when
     every index it names is in `allowed_indexes`, so wildcards and indexes
     that come from a parameter are refused;
   - a parameter's value must sit inside double quotes, so a caller's value
     cannot become an operator, a field name, an index term or a command
     (an integer may stand bare but may not build an index name);
   - a backslash outside a string, and a nested call whose arguments hold a
     quoted comma, are refused: Splunk reads both differently from the
     check, which fails closed where the readings could differ;
   - a macro called in the search terms may not contain a pipe;
   - an eval-based macro, one whose eval flag is not found, an arity
     mismatch, or one name defined twice with different definitions counts
     as unreadable.

   A failure refuses start-up (exit 3) with a fixed reason naming the macro,
   never its text. When the definitions cannot be read, the gateway refuses
   to start (`macros_unverifiable`) unless the operator sets
   `allow_unverified_macros: true`. Then the token user's role is the only
   bound on what a macro reads or runs, and the start log records
   `"macros": "unverified"`.

   A template may set `index_from_macro` instead of `indexes` or
   `index_param` when a macro in its search terms defines the index. The
   gateway then writes no index clause of its own, but still writes the time
   bounds and the row cap. The macro check must prove that the expanded
   terms restrict the search to allowlisted indexes: no top-level `OR`, and a
   top-level index term or a group of index terms joined by `OR`, not
   preceded by `NOT`, not inside a string and spelled `index` in lower case. Otherwise the template needs
   the unverified flag. The site probe reports, per allowlisted macro,
   whether its definitions were readable, the verdict code and the nesting
   depth, and never the definition. Definitions are checked only at pod
   start: a macro the Splunk owner changes afterwards runs unchecked until
   the next start, so the role remains the boundary.
10. **D10, gateway-wide caps (maintainer decision, 2026-11-25).** Every
    search runs as the one token user, so Splunk's per-user concurrent-search
    quota (`srchJobsQuota`, set on the role, 3 by default) covers all
    callers together. The shared runtime's `Limits` gains two optional
    gateway-wide caps, `max_concurrent_total` and `rate_per_minute_total`.
    They are unset by default, so the GitLab gateway is unchanged. The Splunk
    gateway defaults them to 3 calls in flight and 60 a minute in both modes.
    A call over a cap gets the existing `busy` or `rate_limited` code, and a
    refused call counts against no rate. The caps are per process, which
    means per pod: run one replica, or divide the caps by the replica count.

## Shared credentials (amends ADR-0011's table)

| Credential | Class | What it reaches, and the premise |
|---|---|---|
| E1. Splunk encrypted MCP token (ADR-0010 TD7, this record D1) | Named exception | MCP calls as one Splunk user whose role holds `mcp_tool_execute` and the team's index role, read-only. The token cannot call Splunk REST. Premise: every team-group member can search the same indexes with their own account (confirmed by the maintainer, 2026-11-25). Splunk recommends per-user tokens; the exception holds only while the premise does. If it fails, per-user tokens are required, and that needs a new decision first |
| B2. Splunk gateway bearer secret (D7) | Service-to-service caller credential, named exception approved by the maintainer 2026-11-25 (in session) | Calls the gateway's template tools as `service:openwebui`. Premise: one caller service, Open WebUI, holds it; the NetworkPolicy admits only that service (ADR-0014 D4 guards (a) and (b) applied to this gateway); attribution is shared, one hash for every search. If the premise fails, the gateway runs in `oidc` mode |

## Guarantees and what establishes them

| Guarantee | State that establishes it | Who can change that state | Valid for | What forbids the counterexample |
|---|---|---|---|---|
| G1. The gateway never writes to Splunk | One run-query tool for searches (the knowledge-object tool is read once at start); templates only; the lint's refused commands (D3) on templates and on macro expansions (D9); the token user's read-only role; Splunk's guardrail | Gateway code, through a release; the operator (templates, `allowed_macros`); the Splunk owner (role, macro definitions) | Standing; the template lint and the macro check run at every start | `tests/test_gateway_splunk.py`: the fake records every request and the exact SPL; write commands in a template refuse start-up. A role without write capabilities is a site input |
| G2. Searches read only allowlisted indexes | The composed index clause, or for `index_from_macro` the macro check's proof (D9); index parameters limited to `allowed_indexes`; the lint (no index terms, subsearches or data-source commands), also over macro expansions | The operator, through a deployed release; the Splunk owner (role) | Until the configuration or the role changes | Tests: index values outside the allowlist refused before any call; each lint rule; the exact composed SPL. The lint is not a proof: the role is the boundary |
| G3. Every search is bounded in time and rows | Time bounds and `\| head` written by the gateway; template text cannot name time bounds | Gateway code | Standing | Tests: the exact SPL the fake received; window and row caps; snapped starts |
| G4. A parameter cannot change the search's structure | Typed rejection (D3); values only inside the template's quotes | Gateway code | Standing | Tests: injection attempts per type (quotes, pipe, backticks, brackets, comment markers, control characters, Unicode lookalikes, NUL); a property test over arbitrary text |
| G5. No secret or content in logs | The decision log (ids, codes, counts, timings); fixed error codes | Gateway code | Standing | Tests: the token, upstream text, event content and parameter values never appear in captured logs or replies |
| G7. A macro does not widen a search past the template rules | The start-up macro check (D9): allowlisted names, recursive expansion, the D3 rules on each expansion and on the whole search, allowlisted index terms only, parameters quoted | The operator (`allowed_macros`, `allow_unverified_macros`); the Splunk owner (definitions) | From one pod start to the next: a definition changed afterwards runs unchecked until restart. With `allow_unverified_macros`, not at all; the role is the only bound | `tests/test_gateway_splunk.py`: unlisted and nested-unlisted macros, cycles, depth, refused commands (also after the template's pipe), subsearches, foreign, wildcard, negated and parameter-supplied indexes, time modifiers, unquoted parameters, unreadable definitions with and without the flag, `index_from_macro` proofs |
| G8. Splunk's per-user search quota is not exceeded by the gateway | D10's gateway-wide caps | The operator (`limits`); the Splunk owner (`srchJobsQuota`) | Per pod; several replicas multiply the caps | Tests: two callers against a total cap of one; the total rate; GitLab's defaults unset |
| G6. Protocol drift fails closed | The version allowlist; the start-up tool check | Gateway code; the platform (a Splunk MCP upgrade) | Every start | Tests: unsupported versions answered (including 2025-11-25 and 2026-07-28) or refused with a version error, a missing tool or argument, and endless pagination refuse start-up |

## Amendments and unchanged clauses

| Record | Was | Now |
|---|---|---|
| ADR-0010 TD7 | One read-only Splunk service token | One encrypted Splunk MCP token (E1), MCP-only, for a user with `mcp_tool_execute` and the team's index role. Premise unchanged |
| ADR-0010 TD5 | The hash and request id in an SPL comment | Unchanged by default. The site may turn the comment off when the guardrail refuses it (D6); in bearer mode (B2) the hash names the instance |
| ADR-0010 go-live input 3 | Search-head URL and CA, a read-only role and service token, egress, templates | The MCP Server's management-port URL and CA, the token user (`mcp_tool_execute` plus the team's index role, no write capabilities), the encrypted token as a Secret, egress to the management port, templates and `allowed_indexes`, and the site-probe receipt |
| ADR-0010 amendments table and ADR-0014's V2 row (`auth_type`) | A static bearer key for the GitLab gateway in bearer mode (B1) | Also for the Splunk gateway in bearer mode (B2), approved 2026-11-25 |
| ADR-0011 shared credentials | E1 a read-only Splunk service token | E1 as above; adds B2 |
| ADR-0011 D1 and G1 | Every user-class call carries one verified user | Unchanged in `oidc` mode. In `bearer` mode, B2 is the stated exception, as B1 is for GitLab |
| #1077 issue body | Splunk REST only, no Splunk-side install; values "validated and escaped"; a fake Splunk REST server | The Splunk MCP Server app the platform installed (REST is impossible with an MCP-only token); values validated and rejected, never escaped; a fake Splunk MCP Server |
| ADR-0010 TD1 to TD4, TD6, TD8; ADR-0011 D2 to D4, G2 to G6; ADR-0014 | | Unchanged |

## Open site checks (before go-live)

The site probe answers items 1 to 3, run in the gateway pod with the
gateway's configuration:

```sh
python -m mainframe_rag.gateways.splunk --site-probe [--receipt <writable path>]
```

Receipt checks: `splunk.initialize` (negotiated version, session issued,
JSON or event stream), `splunk.startup_check` (D4's verdict),
`splunk.result_shape` (`| makeresults count=1` returns rows where
`result_source` and `result_path` look), `splunk.comment` (the same search
with the TD5 comment, against that control), `splunk.search_prefix` (one
count over the first allowed index with and without a leading `search`;
counts only), `splunk.composed_search` (a search composed as a tool call
composes one), and `splunk.macros` (D9's verdict, with counts). Exit codes are the shared probe codes. The site-private block
holds the server's name and version, the tool names, the run-query tool's
input and output schema, and the makeresults result's key and type shape.
The schema is the server's own text and may carry site names in enums or
defaults: keep it at the site.

1. The run-query tool's input schema, and the constant arguments the site
   needs (`extra_arguments`, for example a workload pool).
2. The result shape, which sets `result_source` and `result_path`.
3. Whether the guardrail accepts the comment (D6) and whether the tool adds
   its own `search` keyword (`search_prefix`).
4. The token user and role (`mcp_tool_execute`, the team's index role, no
   write capabilities), the token's expiry and rotation, the CA bundle and
   the egress rule to the management port.
5. The E1 premise, recorded at the site and re-checked when the team or the
   indexes change.
6. The templates, `allowed_indexes` and `allowed_macros`, written at the
   site. None ship in the repository.
7. The knowledge-object tool's arguments and row shape (`macro_source`), and
   whether the token user can read the macro definitions. If it cannot, the
   gateway needs `allow_unverified_macros`, and the role is then the only
   bound on macros.
8. The replica count: D10's caps are per pod.

## Consequences

- Team members get templated Splunk answers through the platform's own MCP
  Server, with no REST credential and no search-job lifecycle in the gateway.
- Splunk's audit shows the token user. Per-person attribution is the TD5
  comment plus the gateway's decision log, or only Open WebUI's chat history
  in bearer mode.
- A Splunk MCP upgrade that drops 2025-06-18 and 2025-03-26 (for example a
  stateless 2026-07-28 server) stops the gateway at start-up until the
  client is extended: `protocol_unsupported` when the server answers
  initialize with another version or a version error, `upstream_protocol`
  or a status code when it refuses initialize some other way. That is deliberate:
  the client does not guess at a protocol it does not implement.
- The template lint refuses subsearches and some commands a site query
  might use. Templates are rewritten without them, or a later decision
  widens the lint. Allowlisted macros are allowed (D9), but a macro whose
  definition breaks a rule stops the gateway at start-up, naming the
  macro. The macro is then rewritten, left out of the allowlist, or
  accepted unverified.
- `allow_unverified_macros` trades the macro check for availability: with
  it, a macro can read other indexes or run a refused command, within the
  token user's role.
- At most 3 searches run at once and 60 a minute across all callers (D10),
  per pod, unless the site raises them to match the token user's quota.
- Verification: the prose row for this record; the HTTP/lifecycle row for the
  gateway ([live-stack](../live-stack.md#verification-minimums)). Real
  Splunk MCP compatibility is the site probe.
