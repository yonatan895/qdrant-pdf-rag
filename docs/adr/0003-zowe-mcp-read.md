# ADR-0003: authorized, typed, read-only operational observations (issue #90)

- **Status:** accepted on maintainer merge (decision recorded 2026-10-03;
  replaces the 2026-09 proposed text). Accepting the *contract* enables
  nothing: live fetching stays off and unwired until the
  [reactivation gate](#reactivation-gate) is met. Supersedes ADR-0001 only for
  agent-fetched source observations; everything else in ADR-0001 stands.
- **Context:** ADR-0001 keeps Splunk as system of record (context in, not
  crawl) and `/v1/answer` accepts caller-supplied `splunk_context`. Operators
  also ask live-state questions manuals cannot answer ("why did JOB00123 fail
  last night"). A Zowe-shaped, read-only, stdlib-FTP bridge
  (`src/mainframe_rag/mcp/`), an agent client (`agent/zowe_mcp.py`) and a
  deterministic router/planner (`agent/live_state.py`) exist and are
  default-off (`zowe_mcp_enabled=false`); no endpoint calls `fetch_live`. Code
  existing is not approval to read a real system. The official `zowe-mcp`
  server is not used: its only live backend is SSH and the target z/OS 2.2 has
  no sshd. The interface stays MCP-shaped so a later SSH backend swaps without
  agent changes.
- **Decision:**
  1. **Separate bounded port.** Source observations are a distinct port from
     manual evidence ([evidence contract](../evidence-contract.md)). They are
     never a generic query/command proxy or tool framework, and never chosen
     by the model or by manual text: a deterministic router plans, and only a
     closed allowlist of read operations can run. Caller-supplied
     `splunk_context` is unchanged; observations are a sibling block, never a
     replacement.
  2. **First supported operation: `job_status`.** One exact `JOBnnnnn` id per
     request (`job_name` + `owner` only when both are exact). A wildcard or
     unfiltered listing is forbidden. It is the only operation approved to
     reach an answer prompt. `dataset_read`, `uss_read` and `jes_spool_read`
     stay in the code allowlist (closed set of four, mirrored by client,
     bridge and tests) but are **not approved for use**: they return
     arbitrary site text that may hold secrets or personal data, so each needs
     its own ADR amendment (target scope, screening/redaction, byte cap)
     before enablement. A fifth tool is a new ADR, never a flag flip.
  3. **Target and caller scope.** An observation names its exact target: the
     source system (the one configured bridge instance, one LPAR) and the job
     id. The caller is the authenticated service caller
     ([#373](https://github.com/yonatan895/qdrant-pdf-rag/issues/373) owns
     identity). The bridge uses one site read-only SAF credential, so z/OS
     cannot tell callers apart: per-caller target authorization is the agent's
     job, before any source call, and a refusal is `denied` with no call.
     Manual, retrieved or model text never grants or widens scope.
  4. **Authority.** The source owner (site security) owns the read capability
     and the SAF profile; this repository owns the contract and its limits.
     Credentials come from a mounted Secret/env, never git, flags or logs.
     Only z/OS access controls enforce real source boundaries.
  5. **Time.** Every observation carries `acquired_at` (agent clock when the
     bridge replied) and `observed_at` (source-reported time, `null` when the
     source gives none). The JES FTP listing carries no timestamp, so
     `job_status` has `observed_at=null` and is presented "as of
     `acquired_at`". Observations are never cached across requests, so
     staleness is only ever the age shown by `acquired_at`.
  6. **Outcomes.** `complete`, `truncated` (byte cap hit), `partial` (part of
     the plan or listing missing), `not_found`, `unavailable`
     (`timeout` / `jes_unavailable` / `upstream_error` / `not_configured`),
     `no_target` (no exact target extracted, no call made), `dry_run`,
     `denied`. z/OS FTP answers 550 for both "absent" and "not permitted";
     both surface as `not_found`, so existence is not disclosed. Every outcome
     other than `complete` is stated in the answer, which falls back to
     manuals-only with an explicit marker: never failed, never silently
     shortened.
  7. **Limits.** At most 2 source calls per request (`MAX_TOOL_CALLS`), a byte
     cap per call (`zowe_mcp_max_bytes`, default 262144), a dedicated per-call
     timeout (`zowe_mcp_timeout_s`, default 15 s, distinct from the answer
     timeout), no retry beyond connect-phase, no polling or background
     acquisition. Dry-run plans and audits without calling.
  8. **Transport posture.** Plain FTP inside the isolated network is a
     recorded risk, not site acceptance. It is bounded by the read-only SAF
     profile, secret-mounted credentials and the caps; FTPS (`use_tls`) is
     allowed where AT-TLS exists. The site security owner must accept the
     posture in writing (#373) before a real system is contacted. z/OS 2.2 is
     out of service (2020): a flagged environmental risk, reviewed periodically.
  9. **Pilot versus production.** The manual-only POC runs with live fetching
     disabled and makes zero source calls. An opt-in live pilot needs the
     reactivation gate. Nothing here is production-approved.
- **Security boundary:**
  - *Enforced today, with pinning tests:* the bridge sends only `RETR`, `LIST`,
    `NLST`, `TYPE A` and JES query `SITE` filters
    (`tests/test_mcp_ftp_bridge.py`); the client allowlist equals the bridge
    registry and no tool name is a mutating verb; the agent HTTP surface has no
    PUT/PATCH/DELETE, its POST set is exactly the query routes, and no
    endpoint module references `fetch_live` (`tests/test_mcp_agent.py`);
    `job_status` is never planned without an exact job id; defaults are off
    (`tests/test_config.py`). Audit records carry request id, route, tool
    names, byte counts and a fixed degradation code only. The bridge's
    `tool_error` text stays inside the agent and never reaches a client.
    Search never calls an LLM; routing is deterministic and trap queries
    always route `manual`.
  - *Required of #91 (not yet built):* observation text is untrusted data,
    screened like retrieved chunks, delimited in the prompt and cited
    distinctly from manuals (it never grounds a manual citation); client
    errors use fixed messages and stable codes; the per-caller `denied` check;
    read credentials only, never ingest/admin writer credentials.
  - No submit, write, console, TSO, SPL, job control or arbitrary command.
    Untrusted content cannot enable the feature or widen the allowlist: both
    are operator configuration.
- **Reactivation gate:** (a) the POC names a specific investigation that
  manuals and caller-supplied Splunk context cannot answer, and (b) the source
  owner approves the read capability and the FTP posture in writing. Only then
  does one bounded implementation (#91) wire `job_status` into the answer
  path. A proposed ADR, transport code or an "accepted risk" sentence is not
  that approval.
- **Consequences:**
  - Locks in the closed read-only allowlist, exact-target `job_status` as the
    only approved operation, `acquired_at`/`observed_at`, the outcome
    vocabulary and the caps. Default-off stays the shipped state.
  - Code seam shipped with this ADR: `live_state` no longer plans an
    unscoped `job_status` (a bare "JES" mention used to list every job), and a
    plan with no exact target degrades as `no_target` instead of an empty
    success.
  - Not built (handoff to #91): the typed observation record (`target`,
    `acquired_at`, `observed_at`, `outcome`, `truncated`), the per-caller
    `denied` check, `acquired_at` stamping, answer-path prompt wiring with
    screening and distinct citation, and sidecar Helm wiring (the chart does
    not deploy it today).
  - Per-tool degradation when the site's FTP JES interface is limited
    (`jes_unavailable`); JES spool numbering varies by site and is verified on
    first contact, never hardcoded.
  - The operator HTTP client (#172) is a separate consumer of
    health/search/answer; it never exposes live tools.
- **Rejected alternatives:**
  - *Agent-side Splunk REST/SPL connector:* arbitrary SPL is an unbounded read
    surface; Splunk stays caller-supplied context (ADR-0001).
  - *Official `zowe-mcp` server:* SSH-only backend, unavailable on the target.
  - *Generic tool/connector framework or LLM-chosen tool calls:* the model
    would pick sources and untrusted text could steer reads.
  - *Crawling or indexing live state into Qdrant:* stale by construction and
    mixes observations into manual evidence.
  - *Enabling all four read tools now:* content tools can return secrets; one
    operation is approved at a time.
  - *Flag-gated write/submit/console tools:* a flag flip must never widen
    capability; a new ADR must.
  - *Requiring FTPS before any pilot:* the site cannot probe FTPS today; the
    posture is recorded and owner-accepted instead of assumed.
