# ADR-0012: Read-only z/OS dataset reads (and opt-in JES reads) over FTP for the Docs chat (pilot)

- **Status:** proposed; baseline decisions accepted by the maintainer
  2026-11-18 and all open points plus the stable-egress topology locked by the
  maintainer 2026-11-21 (each folded into its decision below and summarised
  under "Decided open points"); record pending merge of PR #1295. Enables
  nothing. Build issue
  [#1294](https://github.com/yonatan895/qdrant-pdf-rag/issues/1294).
  This record **amends** [ADR-0003](0003-zowe-mcp-read.md) (decisions 2, 3, 6
  and 8, the audit-record clause, the "Required of #91" list, the registry pin
  and the reactivation gate's wording), the z/OS row of
  [ADR-0011](0011-gateway-caller-classes.md)'s policy matrix and its
  shared-credentials table,
  [ADR-0005](0005-answer-feedback-data-boundary.md) (chat-history scope and
  observation-turn suppression), and
  [ADR-0007](0007-open-webui-docs-front-end.md) (V2 Pipe parameter and
  answer-reference rules, and R3), as "Amendments and unchanged clauses" lists.
  Accepting it turns nothing on: the bridge is not deployed, `fetch_live` has
  no caller, and the pilot still needs the reactivation gate (D8).
- **Context:**
  - The maintainer ranks read-only access to the site's z/OS as the most
    useful next capability (2026-11-18), starting with **dataset reads** of
    shared libraries (for example a PARMLIB, a PROCLIB, team JCL, REXX and
    source libraries), so an answer can explain the site's actual members with
    the manuals.
  - The site has no z/OSMF; it has FTP. The maintainer expects the site to
    offer **plain FTP only** (no FTPS or AT-TLS). SMF and syslog provide job
    completion codes and step summaries, not actual job logs, and IronStream
    job-log forwarding to Splunk (#1077, #1293) is not yet working; the pilot
    therefore also approves opt-in `job_status` and DD-scoped `jes_spool_read`
    behind an operator tool allowlist (default: `dataset_read` only).
  - ADR-0003 approves only `job_status` and leaves `dataset_read`, `uss_read`
    and `jes_spool_read` unapproved until an amendment sets their scope,
    screening and byte cap. ADR-0011 says the bridge's shared credential "does
    not qualify" for users, because mainframe entitlements differ per user.
  - State at d2bdcba8 (audit in #1294): the bridge (`src/mainframe_rag/mcp/bridge.py`),
    client (`src/mainframe_rag/agent/zowe_mcp.py`) and planner
    (`src/mainframe_rag/agent/live_state.py`) exist, default-off, never
    connected to a real system. Gaps to close in #1294: FTPS builds an
    unverified TLS context; passive mode (`ftp.set_pasv(True)`) is not set
    explicitly; no dataset allowlist; a PDS name without a member falls back to
    `NLST` member listing; `jes_spool_read` lacks `TYPE A`, `LIST` prefix
    verification and `JESMSGLG`/`JESYSMSG` DD scoping; logon failures are
    retried; no content screening; no answer-path wiring or feedback
    suppression; no deployment; all four tools register unconditionally; the
    Docs path carries no pseudonymous audit attribution.

## Decision

1. **D1, approved pilot operations, one route.** Three read-only operations are
   approved for this pilot only, all gated behind the bridge's
   `MCP_ENABLED_TOOLS` allowlist (D7; **default: `dataset_read` only**; opt-in
   `dataset_read,job_status,jes_spool_read` once z/OS `JESSPOOL` rules and
   submission blocks are verified). `uss_read` remains unapproved. All pilot
   reads run on the Docs path (the docs Pipe to the agent's chat and answer
   routes), planned by the existing deterministic planner
   (`src/mainframe_rag/agent/live_state.py`). The model never chooses a read,
   and manual, retrieved or model text never names one: the target comes from
   the user's question and must pass D2. The Docs model stays tool-free
   (ADR-0010 TD1).
2. **D2, target scope: operator allowlists, exact targets and DD scoping.**
   - **`dataset_read` (default):** the operator maintains an allowlist of exact
     dataset or PDS-member names (setting named by #1294). Anything not on it
     is `denied` with no FTP command sent. Each qualifier and the member are
     validated before any FTP command: wildcards, relative GDG references,
     quotes and names outside the list are refused, including by case, quoting
     or member tricks.
   - **PDS member listing excluded (decided 2026-11-21, point 3):** a PDS name
     without an explicit member is refused (no `NLST` fallback); revisit after
     the dry run.
   - **Opt-in `job_status` and `jes_spool_read` (decided 2026-11-21, Option
     4A):** `job_status` uses `SITE FILETYPE=JES` + `LIST` and `jes_spool_read`
     uses `SITE FILETYPE=JES` + `RETR <JOBID>.<spool_id>` (with explicit
     `TYPE A`, windowed to the byte cap as D6 states). Both require an exact target (`JOBnnnnn`, or exact
     `job_name`/`owner` where supported) checked against a bridge-side
     jobname/owner prefix allowlist verified via `LIST` before any `RETR` (no
     arbitrary wildcards such as `PAY*`). `jes_spool_read` is DD-scoped to
     `JESMSGLG` and `JESYSMSG` only — `JESJCL`, `SYSPRINT` and `SYSOUT` are
     excluded to preserve E3 sensitivity and avoid hitting the 256 KiB cap.
     Any off-allowlist job, prefix or DD is `denied` with no `RETR`.
3. **D3, one dedicated READ-only ID (named exception E3 to ADR-0011), role and
   caller guards, and pseudonymous audit attribution.** The bridge logs on with
   one dedicated RACF ID, mounted from a Secret, never in git, flags or logs.
   *Premise* (maintainer, 2026-11-18; extended to opt-in JES scope 2026-11-21):
   every user who can sign in to Open WebUI is on the mainframe team and can
   already read every allowlisted dataset — and, when JES tools are opted in,
   `JESMSGLG`/`JESYSMSG` for the approved team jobname/owner prefixes — with
   their own ID. So the pilot returns nothing a caller could not read directly,
   which is ADR-0011 D1's rule. Guards:
   - (a) **Premise review:** the premise is re-checked, and recorded at the
     site, whenever the sign-in groups, the dataset or JES prefix allowlists,
     or team membership change (a departing person is also demoted or deleted
     in Open WebUI, see G2).
   - (b) **Role guard:** render and deploy refuse enabling reads unless every
     value in `OPENWEBUI_OIDC_ALLOWED_ROLES` and `OPENWEBUI_OIDC_ADMIN_ROLES`
     is in an approved-roles setting for z/OS reads (named by #1294), because
     an admin role alone signs a user in.
   - (c) **Readers outside signed-in team members (decided 2026-11-21, point
     6):** render and deploy refuse enabling reads when `AGENT_ROUTE=true`,
     `AGENT_NETWORK_POLICY=false`, or extra `allowedNamespaces` are set. Open
     WebUI admins are bound by E3's premise and a pilot runbook rule against
     sharing observation-bearing chats (bounded by the admin role limit,
     ADR-0007 S3).
   - (d) **Audit attribution on the Docs path (decided 2026-11-21, point 1):**
     because a shared RACF ID behind a single `EgressIP` cannot distinguish
     individual users in z/OS SMF logs, the docs Pipe sends a keyed HMAC (with
     a site Secret, as ADR-0010 TD5 does for Splunk) of the Open WebUI user ID
     as a non-authorizing audit field. The agent logs it with the request ID,
     tool/target name, outcome and byte count. It never authorizes anything
     (any caller reaching the agent's port could forge it; NetworkPolicy limits
     that to the namespace) and leaves "the Pipe declares no
     `__oauth_token__`" intact. This amends ADR-0003's audit-record clause,
     ADR-0007 V2's Pipe parameter rule (`__user__`) and ADR-0007 R3.
4. **D4, z/OS-enforced read-only, standalone bridge topology, stable egress
   identity and bridge caller auth (decided 2026-11-21, Option A, point 2 and
   Option 4A).**
   - **z/OS controls own read-only:** the site owns, and records before the
     pilot, that the dedicated RACF ID has READ access to the allowlisted
     datasets and nothing wider, and cannot submit jobs or write over FTP (site
     FTP and RACF controls). The bridge's pinned verb set (ADR-0003) and
     allowlists are defence in depth.
   - **Mandatory z/OS guardrails before opting in JES tools (Option 4A):**
     (a) hard-block JCL submission on z/OS (`STOR`/`PUT` under `FILETYPE=JES`
     blocked via RACF `JESJOBS`/`FTCHKCMD`, in addition to the bridge's pinned
     verb allowlist); (b) `JESINTERFACELEVEL=2` plus scoped RACF `JESSPOOL`
     READ profiles restricted to the approved team jobname/owner prefixes
     (never `JESSPOOL *`); (c) DD-scoped `JESMSGLG` and `JESYSMSG` only (D2);
     (d) bridge-side prefix allowlist verified via `LIST` before `RETR`,
     explicit `TYPE A`, and D6 screening.
   - **Standalone in-cluster Deployment (Option A):** the FTP bridge runs as a
     standalone in-cluster Deployment (`ClusterIP`-only, `restricted-v2`, label
     `app: rag-mcp-ftp`).
   - **Stable egress IP & RACF terminal-alias binding (Option A):** as a
     one-time cluster-admin / platform prerequisite, the site allocates **1
     OpenShift `EgressIP`** (`k8s.ovn.org/v1`) scoped via `podSelector`
     strictly to the FTP bridge Deployment (`app: rag-mcp-ftp`) — **not**
     `rag-agent` and **not** namespace-wide. The Helm chart does **not** render
     the cluster-scoped `EgressIP` CR; the namespace admin and chart own the
     bridge Deployment, RACF Secret and `NetworkPolicy` day-to-day. z/OS RACF
     binds the dedicated READ-only pilot RACF ID exclusively to the registered
     Rosetta terminal alias mapped to that single `EgressIP`, strengthening E3
     as defence in depth.
   - **Explicit passive mode and egress firewall (Option A):** the bridge sets
     explicit passive mode (`ftp.set_pasv(True)`), mandatory behind `EgressIP`
     SNAT. The bridge egress `NetworkPolicy` and site firewall allow egress
     only to cluster DNS and the single z/OS LPAR IP on TCP 21 and the site's
     known `FTP.DATA` `PASSIVEPORTRANGE`.
   - **Bridge caller authentication (point 2):** callers authenticate to the
     bridge with a bearer secret (matching the feedback service pattern,
     ADR-0005 / #804), in addition to an ingress `NetworkPolicy` admitting
     `app: rag-agent` pods only. Render/deploy refuses enabling the bridge
     without both.
5. **D5, transport (decided 2026-11-21, point 5).**
   - **Plain FTP is the expected pilot transport.** It needs the site security
     owner's written acceptance, recorded at the site before the first live
     read. Residual risk, stated plainly: the dedicated ID's password and the
     returned dataset or spool text cross the network unencrypted between the
     cluster and z/OS. Mitigations: the ID is READ-only on the allowlisted
     scope only and RACF-bound to the bridge's single `EgressIP` terminal alias
     (D3, D4), so a captured password cannot log on from outside that
     `EgressIP` and reaches only data every team member can already read; job
     submission and writes are blocked by site controls (`JESJOBS`/`FTCHKCMD`
     and RACF); egress goes only to one LPAR IP on TCP 21 and
     `PASSIVEPORTRANGE`; the password is rotated by the site and held only in a
     Secret. Team members already use plain FTP with their own, broader IDs;
     that is context, not justification.
   - **FTPS, when the site enables TLS:** the bridge verifies the server
     against the site CA and checks the host name, fails closed, and never
     presents an unverified session as FTPS (gap 1 in #1294). Not a pilot
     blocker.
   - **Mode pinned by configuration (locked):** the operator pins either FTPS
     (verified, fail closed) or plain FTP (owner-accepted). No automatic
     FTPS-to-FTP downgrade fallback: an on-path attacker can make `AUTH TLS`
     fail, so a fallback gives plain-FTP exposure while looking like FTPS. The
     mode is chosen from a one-time, logon-free connectivity probe (whether
     `AUTH TLS` is offered and verifies against the site CA).
6. **D6, content handling and feedback suppression (decided 2026-11-21, Option
   7A).**
   - Text only (explicit `TYPE A` for both `dataset_read` and `jes_spool_read`),
     under the byte cap (`zowe_mcp_max_bytes`, 256 KiB default; hitting it is
     `truncated`), with a configurable code page (default `latin-1`, decided 2026-11-21: text-mode
     FTP output is a single-byte ASCII page, usually ISO8859-1 per the server's
     `SBDATACONN`; bytes above 0x7F such as `¬` are invalid UTF-8 and would decode
     to U+FFFD; a site with UTF-8 output sets `MCP_FTP_ENCODING=utf-8`).
   - Trailing blanks stripped (decided 2026-11-21): every text read sends
     `SITE NOTRAILINGBLANKS` and `SITE SBSENDEOL=CRLF` before `TYPE A`. An
     unconfirmed reply (202, a 200 carrying an unrecognized-parameter line, or
     a 5xx) is a capacity loss, not a security control: the read proceeds and
     the reply code alone is logged and set on the span.
   - Job logs windowed at both ends (decided 2026-11-21): `jes_spool_read`
     returns a spool file whole when its screened text fits the cap less 1 KiB
     for the frame; a longer one keeps its first quarter and last three
     quarters (module constants, no setting) around a gap marker, with the
     window facts repeated in the bridge-authored header and in
     `structuredContent`. The framed result fits `MCP_FTP_MAX_BYTES`, so the
     agent's head-only live cap keeps the tail and the END delimiter as long as
     `ZOWE_MCP_MAX_BYTES` is at least `MCP_FTP_MAX_BYTES` (both 256 KiB by
     default; nothing enforces the pairing across the two processes). A
     contiguous read is screened once as a whole and both parts are cut from
     it. With a gap, each part is screened on its own, the tail over its whole
     retained ring, and the tail's start is dropped through the line where any
     key and separator a match from before the ring could carry end, so no
     value whose key fell in the gap survives. Whole lines are trimmed last.
     `MCP_FTP_SPOOL_STREAM_MAX_BYTES`
     (16 MiB) and `MCP_FTP_SPOOL_STREAM_MAX_S` (8 s, below the agent's default
     15 s call timeout) stop a longer stream, which returns the head with a
     stop marker; a `LIST` BYTE-COUNT above the byte ceiling reads the head
     only. `dataset_read` and `job_status` stay head-only.
   - Screened before it reaches the prompt: control and bidi characters
     stripped, password-shaped strings redacted.
   - Framed as an untrusted, delimited observation block, cited distinctly from
     manuals with a fixed z/OS observation citation marker; it never grounds a
     manual citation.
   - ADR-0003's `acquired_at` and outcome vocabulary apply; every outcome other
     than `complete` is stated in the answer. No caching across requests;
     dry-run plans without calling.
   - **Where content may live:** in transit, in the prompt to the designated
     reasoning model; at rest, only the user's Open WebUI chat history, as user
     session state under ADR-0007 S1 to S4 (30-day purge). Never Qdrant, logs,
     telemetry, traces or the feedback store.
   - **Feedback and export boundary (Option 7A):** `FEEDBACK_ENABLED=true`
     stays enabled and working for all normal manual-only Docs turns; render
     and deploy do **not** refuse `FEEDBACK_ENABLED=true`. Instead,
     `_record_feedback` in `src/mainframe_rag/agent/app.py` is suppressed
     whenever the current turn invoked `fetch_live` **or** any prior message in
     `messages` carries the fixed z/OS observation citation marker, and the
     `Answer reference:` footer is omitted on observation-bearing turns. Such
     turns therefore never enter the 180-day `rag-feedback` SQLite DB, cannot
     collect free-text rating comments quoting z/OS data, and cannot reach
     `src/mainframe_rag/feedback/exchange.py` or the coding-agents VM
     (preserving ADR-0011). The pilot improvement loop relies on content-free
     structured telemetry plus human-distilled synthetic repro fixtures from
     30-day Open WebUI chats.
7. **D7, the bridge registers only approved tools (decided 2026-11-21, Option
   4A).** The registered tool set is configured by `MCP_ENABLED_TOOLS`, a
   subset of the closed four: **default `dataset_read` only**; opt-in
   `dataset_read,job_status,jes_spool_read` once z/OS `JESSPOOL` rules and the
   submission hard-block are verified (D4, D8). `uss_read` remains unapproved
   and is refused even if named. The setting can only select within the
   approved pilot subset; a tool outside the closed four is still a new ADR.
   Calls to unregistered tools fail closed.
8. **D8, reactivation gate and phased rollout for this pilot.** ADR-0003's gate
   applies as follows:
   - (a) **Named investigation:** "explain the site's actual PARMLIB, PROCLIB
     and JCL members (and, once opted in, team job `JESMSGLG`/`JESYSMSG`
     diagnostics where Splunk lacks job logs) with the manuals";
   - (b) **Site prerequisites and written approvals:** before any live read,
     the source owner approves the read capability, the allowlist(s) and the
     transport in writing; platform/cluster-admin allocates the single
     `EgressIP` scoped to `app: rag-mcp-ftp`, registers its Rosetta terminal
     alias, binds the dedicated READ-only RACF ID to that alias, and opens the
     firewall to the LPAR IP on TCP 21 and `FTP.DATA` `PASSIVEPORTRANGE`;
   - (c) **Phased validation:** first the logon-free transport probe, then a
     `dry_run` plan without FTP calls (recording the FTP server's `SBDATACONN`
     code page, to confirm or override `MCP_FTP_ENCODING`), then one live `dataset_read` of an
     allowlisted member on a test LPAR (recorded with ids, outcome and bytes
     only). Opting in `job_status,jes_spool_read` in `MCP_ENABLED_TOOLS` waits
     until z/OS `JESINTERFACELEVEL=2`, scoped `JESSPOOL` READ profiles (never
     `JESSPOOL *`) and `JESJOBS`/`FTCHKCMD` submission blocks are verified and
     recorded.
   - (d) **Dry-run checklist (unverified until the test LPAR records them):**
     - `SITE NOTRAILINGBLANKS` and `SITE SBSENDEOL=CRLF` are the server's
       spellings, and the reply wording for an accepted and an ignored operand
       (the bridge treats 200 without "unrecognized" as confirmed);
     - whether the strip applies under `FILETYPE=JES`; it covers
       fixed-format records only, and sequence numbers in columns 73 to 80
       defeat it;
     - the unit of the JES `LIST` BYTE-COUNT column (the bridge reads it as
       bytes for the stream-ceiling hint);
     - that a long `JESMSGLG`/`JESYSMSG` read returns its first and last
       lines within `MCP_FTP_SPOOL_STREAM_MAX_S`, recorded with ids, window
       and byte counts only.
9. **D9, logon-failure breaker.** A logon failure (FTP 530) gets its own code
   (named by #1294) and stops further logons until an operator resets it or a
   cool-down passes, so repeated failures cannot revoke the ID.

## Shared credentials (replaces ADR-0011's z/OS row for the pilot)

| Credential | Class | What it reaches, and the premise |
|---|---|---|
| E3. z/OS bridge's dedicated READ-only RACF ID (this ADR, D3, D4) | Named exception, pilot only | The allowlisted datasets via `dataset_read` (default), and — when opted in via `MCP_ENABLED_TOOLS` — `job_status` and `JESMSGLG`/`JESYSMSG` spool reads for the approved team jobname/owner prefixes only; read-only, RACF-bound exclusively to the Rosetta terminal alias of the bridge's single `EgressIP` (D4). Premise: every user who can reach reads through the product (every Open WebUI sign-in and admin role; `AGENT_ROUTE=true`, `AGENT_NETWORK_POLICY=false` and extra `allowedNamespaces` are refused, and admins are bound by this premise and the pilot no-share runbook rule) is on the mainframe team and can already read every allowlisted dataset and opted-in JES prefix. If a wider group signs in, or a dataset or job prefix some team member cannot read joins the list, this needs a new decision first |

## Guarantees and what establishes them

| Guarantee | State that establishes it | Who can change that state | Valid for | What forbids the counterexample |
|---|---|---|---|---|
| G1. Only allowlisted exact targets (and allowed JES prefixes/DDs) are read | The operator dataset allowlist and exact-name validation (no PDS-without-member `NLST`), plus for opt-in JES tools the jobname/owner prefix allowlist verified via `LIST` and `JESMSGLG`/`JESYSMSG` DD filter before `RETR` (D2) | The operator, through a deployed release | Until an allowlist changes | **Required of #1294:** bypass tests (case, quotes, PDS-without-member, GDG, wildcard, `PAY*` prefix, `JESJCL`/`SYSPRINT`/`SYSOUT` DD rejection) against a fake FTP proving no forbidden command or `RETR` was sent |
| G2. No sign-in holder or unapproved caller reads data they could not read themselves | The premise (D3), the approved-roles guard (`OPENWEBUI_OIDC_ALLOWED_ROLES` and `OPENWEBUI_OIDC_ADMIN_ROLES`), render refusals (`AGENT_ROUTE=true`, `AGENT_NETWORK_POLICY=false`, extra `allowedNamespaces`), bridge bearer auth + `app: rag-agent`-only ingress NetworkPolicy (D4), and site RACF scope + `EgressIP` terminal-alias binding (D4) | The identity team (who holds a role); the operator (role lists, chart values, Secrets); cluster-admin (`EgressIP`); the site (RACF scope, terminal binding, team membership) | Until a role list, group, allowlist or binding changes. A person removed from the group keeps an open session up to its expiry (8 hours) and their API keys keep the old role until the account is demoted or deleted in Open WebUI ("Revocation window" in [deploy](../deploy.md#docs-front-end)) | **Required of #1294:** render refusal tests for roles, `AGENT_ROUTE`, `AGENT_NETWORK_POLICY`, extra `allowedNamespaces`, and missing bridge auth/NetworkPolicy; bridge 401/403 tests without bearer token; the site premise and RACF binding record. Group membership in the IdP is not checked by this repository |
| G3. Nothing is written or submitted | Site controls on the ID (D4: dataset READ-only, plus `STOR`/`PUT` under `FILETYPE=JES` hard-blocked via RACF `JESJOBS`/`FTCHKCMD` before JES opt-in); the bridge's pinned verbs and `MCP_ENABLED_TOOLS` default | The site security owner; bridge code and config, through a release | Standing | Pinned verb test (`tests/test_mcp_ftp_bridge.py`); `MCP_ENABLED_TOOLS` refusal tests; the site's RACF/`FTCHKCMD` verification record. The verb pin alone is not the control |
| G4. Content stays out of logs, telemetry, Qdrant, feedback DB and agent-VM exports | D6: the bridge and agent log ids, HMAC audit tag, counts and codes only; `_record_feedback` in `src/mainframe_rag/agent/app.py` is suppressed and `Answer reference:` is omitted when the turn called `fetch_live` or any prior message in `messages` carries the fixed z/OS observation citation marker (Option 7A) | Product code, through a release | Standing | **Required of #1294:** tests proving logs/telemetry carry no observation text, `_record_feedback` and `Answer reference:` are suppressed on both live-fetch turns and observation-history follow-ups (never reaching `rag-feedback` SQLite or `src/mainframe_rag/feedback/exchange.py`), while manual-only turns still record feedback normally when `FEEDBACK_ENABLED=true` |
| G5. Bridge egress uses one stable IP and explicit passive mode | Cluster-admin `EgressIP` (`k8s.ovn.org/v1`) scoped via `podSelector` to `app: rag-mcp-ftp` only; `ftp.set_pasv(True)` in bridge `connect()`; bridge egress `NetworkPolicy` and site firewall restricted to DNS + LPAR IP on TCP 21 and `PASSIVEPORTRANGE`; RACF terminal-alias binding (D4) | Cluster-admin (`EgressIP`); namespace admin (Deployment label, `NetworkPolicy`); site network/security owners (firewall, RACF binding) | Standing, until `EgressIP`, label or firewall changes | **Required of #1294:** unit test pinning `set_pasv(True)` in `mcp/bridge.py`; chart render test proving no `EgressIP` CR is emitted and egress `NetworkPolicy` targets `app: rag-mcp-ftp` on TCP 21 + `PASSIVEPORTRANGE` only; site test-LPAR verification of Rosetta alias binding |

## Amendments and unchanged clauses

| Record | Was | Now |
|---|---|---|
| ADR-0003 decision 2 | Only `job_status` approved; `dataset_read` and `jes_spool_read` unapproved without amendments | Approves `dataset_read` (default) plus opt-in `job_status` and DD-scoped `jes_spool_read` (`JESMSGLG`/`JESYSMSG` only) for this pilot under D1 to D9 (`MCP_ENABLED_TOOLS`); `uss_read` stays unapproved |
| ADR-0003 decision 3 | Per-caller target authorization by the agent (the `denied` check), one shared SAF credential | For pilot reads: the allowlists/DD scope (D2) plus E3's premise and guards (D3, D4) replace per-caller authorization; off-list targets are `denied` with no call or `RETR` |
| ADR-0003 decision 6 | `unavailable` covers `timeout`, `jes_unavailable`, `upstream_error`, `not_configured` | Adds a logon-failure code (D9) |
| ADR-0003 decision 8 | Plain FTP a recorded risk; FTPS where AT-TLS exists | D5: plain FTP is the expected pilot transport with written owner acceptance (bounded by `EgressIP` terminal binding); FTPS must verify the server; mode pinned by config, no downgrade fallback |
| ADR-0003 security boundary, audit-record clause | Request id, route, tool names, byte counts and a fixed degradation code only | Adds a non-authorizing keyed HMAC of the Open WebUI user ID on the Docs path (D3(d), point 1) plus target name/outcome |
| ADR-0003 security boundary, "the client allowlist equals the bridge registry" | Client allowlist and bridge registry are the same four | The bridge registers `MCP_ENABLED_TOOLS` (default `dataset_read` only; opt-in adds `job_status,jes_spool_read`; D7); the client allowlist and closed four stay as pinned; calls to unregistered tools fail closed |
| ADR-0003 "Required of #91" | Includes the per-caller `denied` check | For the pilot, replaced by D2, D3 and D4; screening, delimiting, distinct citation and fixed errors stay required (D6), built under #1294 |
| ADR-0003 reactivation gate | One implementation (#91) wires `job_status` | D8 states the pilot investigation, site prereqs (`EgressIP`, RACF terminal binding, `JESSPOOL`/`FTCHKCMD` for JES opt-in) and approvals; #1294 builds it |
| ADR-0011 policy matrix, z/OS, user class | ADR-0003; shared credential does not qualify | `dataset_read` (default) and opt-in `job_status` / DD-scoped `jes_spool_read` through E3, pilot only. Agent class: still none; rated-case export boundary preserved by Option 7A (D6) |
| ADR-0011 shared credentials, z/OS row | Does not qualify | Named exception E3 (table above), pilot only, strengthened by `EgressIP` RACF terminal-alias binding (D4). Per-user access stays the target (follow-up 1) |
| ADR-0005 decision scope & answer-record rule (as amended by ADR-0007 and ADR-0010) | Chat history may hold question, answer, excerpt and Team-model tool output; every finalized answer sends an answer record when `FEEDBACK_ENABLED` | Chat history may also hold z/OS pilot observations from Docs-model chats under ADR-0007 S1 to S4. Observation-bearing turns (current `fetch_live` or prior observation citation marker in `messages`) are suppressed from `_record_feedback` and omit `Answer reference:`, so the feedback store and `feedback/exchange.py` never hold them while manual-only turns keep recording normally (D6, Option 7A) |
| ADR-0007 V2 (Pipe parameters and answer reference) | Pipe declares no `__user__` without a recorded decision; ends every completed answer with `Answer reference:` when ratings are on | Records `__user__` on the docs Pipe solely to compute the keyed-HMAC audit field (`__oauth_token__` and `__request__` stay forbidden, D3(d), point 1); omits `Answer reference:` on observation-bearing turns (D6, Option 7A). Status pointer in ADR-0007 updates with #1294 |
| ADR-0007 R3, the agent anonymous to users | Safe because every user may read every manual | Amended to pseudonymous audit attribution (keyed HMAC of Open WebUI user ID, non-authorizing, D3(d), point 1); safe for z/OS reads only while E3's premise holds |
| ADR-0010 TD1, the Docs model stays tool-free | | Unchanged: reads are planned by the agent, not offered as a tool |
| ADR-0003 decisions 1, 4, 5, 7, 9 and the pinned verb set | | Unchanged |

## Decided open points

All 7 open points from the initial draft plus the stable egress IP requirement
were decided by the maintainer on 2026-11-21. Each is folded into its decision
above with no open point remaining. This record stays proposed until the
maintainer merges PR #1295.

| Point | Decision (2026-11-21, maintainer) | Alternative considered | Where it lives |
|---|---|---|---|
| Egress / terminal alias | **Option A:** standalone in-cluster bridge (`ClusterIP`-only, `restricted-v2`, `app: rag-mcp-ftp`), 1 cluster-admin `EgressIP` (`k8s.ovn.org/v1`) scoped via `podSelector` strictly to the bridge (chart does not render the CR), RACF ID bound to that `EgressIP`'s Rosetta terminal alias, explicit `ftp.set_pasv(True)`, egress `NetworkPolicy` + firewall limited to DNS + LPAR IP TCP 21 and `FTP.DATA` `PASSIVEPORTRANGE` | Outside-cluster bridge or namespace-wide `EgressIP` | D4, D5, D8, E3, G5 |
| 1. Audit attribution | **As recommended:** docs Pipe sends a keyed HMAC of the Open WebUI user ID as a non-authorizing audit field (amends ADR-0003 audit clause, ADR-0007 V2 and R3) — essential because shared RACF ID + single `EgressIP` cannot distinguish users in SMF | Forward verified user token (amends ADR-0010); or request-id only | D3(d), Amendments |
| 2. Bridge caller auth | **As recommended:** bearer secret (matching ADR-0005 feedback service) + ingress `NetworkPolicy` admitting `app: rag-agent` pods only | NetworkPolicy only | D4, G2 |
| 3. PDS member listing | **As recommended:** excluded from pilot (refuse PDS name without explicit member; no `NLST` fallback); revisit after dry run | Allow `NLST` member listing of allowlisted PDS | D2, G1, Follow-up |
| 4. `job_status` & FTP job logs | **Option 4A:** approve `dataset_read` (default), `job_status` (`SITE FILETYPE=JES` + `LIST`) and DD-scoped `jes_spool_read` (`JESMSGLG`/`JESYSMSG` only) behind `MCP_ENABLED_TOOLS` (default `dataset_read` only; opt-in after z/OS `JESSPOOL` + `JESJOBS`/`FTCHKCMD` verification, `JESINTERFACELEVEL=2`, `LIST` prefix allowlist, explicit `TYPE A` and D6 screening) | Keep `job_status` unregistered and `jes_spool_read` unapproved, relying on Splunk alone | Context, D1, D2, D3, D4, D7, D8, E3, G1, G3, Amendments |
| 5. Transport mode | **As recommended:** mode pinned by config (verified FTPS fail-closed or owner-accepted plain FTP), no automatic downgrade fallback, logon-free connectivity probe | Automatic FTPS-to-FTP fallback | D5 |
| 6. Readers outside team | **As recommended:** render/deploy refuses enabling reads with `AGENT_ROUTE=true`, `AGENT_NETWORK_POLICY=false` or extra `allowedNamespaces`; admins bound by E3 premise + pilot runbook no-share rule | Allow console Route or extra namespaces during pilot | D3(c), E3, G2 |
| 7. Feedback & export | **Option 7A:** keep `FEEDBACK_ENABLED=true` working for manual-only Docs turns; suppress `_record_feedback` in `src/mainframe_rag/agent/app.py` when the turn called `fetch_live` or any prior message in `messages` carries the fixed z/OS observation citation marker, and omit `Answer reference:` on observation-bearing turns (preserving ADR-0011) | Refuse `FEEDBACK_ENABLED=true` during pilot; or 7C (in-cluster only) / 7B (ADR-0011 amendment) | D6, G4, Amendments, Follow-up |

## Follow-up

1. **Per-user access** in a z/OS Team model with RACF PassTickets: needs #1088
   (identity), site RACF setup, and an ADR-0003 amendment for model-chosen
   calls. PassTickets would also shorten the useful life of a captured
   credential (**VERIFY** the site's replay and expiry behaviour before relying
   on it).
2. **AT-TLS or FTPS at the site**, after which the pilot moves to verified FTPS
   (D5).
3. **PDS member listing (`NLST`)**, revisited after the test-LPAR dry run
   (point 3).
4. **Observation-turn feedback loop:** the pilot uses content-free structured
   telemetry plus human-distilled synthetic repro fixtures from 30-day Open
   WebUI chats; revisit in-cluster-only feedback retention (7C) or an ADR-0011
   export amendment (7B) only after #1199 lands if needed.
5. **The build:** #1294 (bridge hardening, `MCP_ENABLED_TOOLS`, `set_pasv(True)`,
   JES prefix/DD guardrails, agent wiring, `_record_feedback` suppression and
   `Answer reference:` omission, Pipe HMAC audit header, chart Deployment /
   NetworkPolicies / render refusals, ADR-0007 status pointer, and site pilot
   record).

## Consequences

- A team member gets answers that cite the site's own allowlisted members (and,
  once opted in, `JESMSGLG`/`JESYSMSG` job logs) next to the manuals, with
  every non-`complete` outcome stated.
- The pilot rests on a premise the repository cannot check (team membership and
  the ID's RACF scope and terminal binding); the role and render guards catch
  only widened role lists or unapproved caller paths.
- Plain FTP exposes the ID's password and the read text on the network path,
  accepted by the site owner in writing and bounded by D3, D4 (`EgressIP`
  Rosetta terminal binding, `JESJOBS`/`FTCHKCMD`, `JESSPOOL` scope) and D5.
- Normal manual-only Docs turns keep `FEEDBACK_ENABLED=true` and ratings
  working unchanged; observation-bearing turns stay completely out of
  `rag-feedback` SQLite and `feedback/exchange.py`.
- New site inputs: the one-time cluster-admin `EgressIP` CR (scoped to
  `app: rag-mcp-ftp`) and its Rosetta terminal alias binding, the RACF ID
  Secret, the bridge bearer Secret, the Pipe HMAC Secret, the dataset allowlist
  (and optional JES jobname/owner prefix allowlist + `MCP_ENABLED_TOOLS`), the
  approved roles, the z/OS LPAR IP and `FTP.DATA` `PASSIVEPORTRANGE`, the code
  page, the pinned transport mode and its owner acceptance, and the CA bundle
  when FTPS is used.
- Verification: the prose row of
  [live-stack](../live-stack.md#verification-minimums) for this record; #1294
  carries the HTTP/lifecycle, Open WebUI helper and packaging/deploy rows.
