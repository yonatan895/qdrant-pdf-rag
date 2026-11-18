# ADR-0012: Read-only z/OS dataset reads over FTP for the Docs chat (pilot)

- **Status:** proposed; decisions above accepted by the maintainer 2026-11-18
  in session, open points pending; record pending merge. Enables nothing.
  Build issue [#1294](https://github.com/yonatan895/qdrant-pdf-rag/issues/1294).
  This record **amends** [ADR-0003](0003-zowe-mcp-read.md) (decisions 2, 3, 6
  and 8, the "Required of #91" list and the reactivation gate's wording),
  the z/OS row of [ADR-0011](0011-gateway-caller-classes.md)'s policy matrix
  and its shared-credentials table, and the decision of
  [ADR-0005](0005-answer-feedback-data-boundary.md), as "Amendments" lists.
  Accepting it turns nothing on: the bridge is not deployed, `fetch_live`
  has no caller, and the pilot still needs the reactivation gate (D8).
- **Context:**
  - The maintainer ranks read-only access to the site's z/OS as the most
    useful next capability (2026-11-18), mainly **dataset reads** of shared
    libraries (for example a PARMLIB, a PROCLIB, team JCL, REXX and source
    libraries), so an answer can explain the site's actual members with the
    manuals.
  - The site has no z/OSMF; it has FTP. The maintainer expects the site to
    offer **plain FTP only** (no FTPS or AT-TLS). Syslog and job completion go
    through Splunk (#1077, #1293), not FTP.
  - ADR-0003 approves only `job_status` and leaves `dataset_read` unapproved
    until an amendment sets its scope, screening and byte cap. ADR-0011 says the
    bridge's shared credential "does not qualify" for users, because mainframe
    entitlements differ per user.
  - State at d2bdcba8 (audit in #1294): the bridge (`mcp/bridge.py`), client
    (`agent/zowe_mcp.py`) and planner (`agent/live_state.py`) exist,
    default-off, never connected to a real system. Gaps: FTPS builds an
    unverified TLS context; no dataset allowlist; a PDS name without a member
    returns its member list; logon failures are retried; no content screening;
    no answer-path wiring; no deployment; all four tools register; the Docs
    path carries no user identity.

## Decision

1. **D1, one operation, one route.** `dataset_read` is approved for this
   pilot only, on the Docs path (the docs Pipe to the agent's chat and answer
   routes), planned by the existing deterministic planner
   (`agent/live_state.py`). The model never chooses a read, and manual,
   retrieved or model text never names one: the target comes from the user's
   question and must pass D2. The Docs model stays tool-free (ADR-0010 TD1).
2. **D2, target scope: an operator allowlist of exact names.**
   - The operator maintains a list of datasets (setting named by #1294).
     Anything not on it is `denied` with no FTP command sent.
   - Exact names only: each qualifier and the member are validated before any
     FTP command. Wildcards, relative GDG references, quotes and names outside
     the list are refused, including by case, quoting or member tricks.
   - Whether a member list of an allowlisted PDS may be read is open point (3).
3. **D3, one dedicated READ-only ID (named exception E3 to ADR-0011).** The
   bridge logs on with one dedicated RACF ID, mounted from a Secret, never in
   git, flags or logs. *Premise* (maintainer, 2026-11-18): every user who can
   sign in to Open WebUI is on the mainframe team and can already read every
   allowlisted dataset with their own ID. So the pilot returns nothing a caller
   could not read directly, which is ADR-0011 D1's rule. Guards:
   - (a) the premise is re-checked, and recorded at the site, whenever the
     sign-in groups or the allowlist change;
   - (b) mechanical: render and deploy refuse enabling reads unless every
     `OPENWEBUI_OIDC_ALLOWED_ROLES` and `OPENWEBUI_OIDC_ADMIN_ROLES` value is
     in an approved-roles setting for dataset reads (named by #1294). Admin
     roles are included because an admin role alone signs a user in.
   - Other callers of the agent's chat and answer routes (the console Route,
     pods allowed by the agent NetworkPolicy) are open point (6).
4. **D4, read-only is enforced by z/OS, not by the client.** The site owns,
   and records before the pilot: the ID has READ access to the allowlisted
   datasets and nothing wider; it cannot submit jobs or write over
   FTP (site FTP and RACF controls). The
   repository does not specify RACF syntax. The bridge's pinned verb set
   (ADR-0003) and the allowlist are defence in depth: a captured password
   bypasses both and is bounded only by the site's controls. An egress
   NetworkPolicy lets the bridge reach DNS and the one z/OS host on its FTP
   ports only.
5. **D5, transport.**
   - **Plain FTP is the expected pilot transport.** It needs the site
     security owner's written acceptance, recorded at the site before the
     first live read. Residual risk, stated plainly: the dedicated ID's
     password and the returned dataset contents cross the network
     unencrypted between the cluster and z/OS. Mitigations: the ID is
     READ-only on the allowlist only, so a captured password reaches only data
     every team member can already read (D3, D4); job submission and writes are
     blocked by site controls; egress goes to one host and its FTP ports; the
     password is rotated by the site and held only in a Secret. Team members
     already use plain FTP with their own, broader IDs; that is context, not
     justification.
   - **FTPS, when the site enables TLS:** the bridge verifies the server
     against the site CA and checks the host name, fails closed, and never
     presents an unverified session as FTPS (gap 1 in #1294). Not a pilot
     blocker.
   - **Mode pinned by configuration** (recommended rule, maintainer to
     confirm, open point (5)): the operator sets FTPS (verified, fail closed)
     or plain FTP (owner-accepted). No automatic FTPS-to-FTP fallback: an
     on-path attacker can make `AUTH TLS` fail, so a fallback gives plain-FTP
     exposure while it looks like FTPS. The mode comes from a one-time site
     probe (is `AUTH TLS` offered; does the chain verify with the site CA)
     that never logs on.
6. **D6, content handling.**
   - Text only (`TYPE A`), under the byte cap (`zowe_mcp_max_bytes`; hitting
     it is `truncated`), with a configurable code page.
   - Screened before it reaches the prompt: control and bidi characters
     stripped, password-shaped strings redacted.
   - Framed as an untrusted, delimited observation block, cited distinctly
     from manuals; it never grounds a manual citation.
   - ADR-0003's `acquired_at` and outcome vocabulary apply; every outcome
     other than `complete` is stated in the answer. No caching across
     requests; dry-run plans without calling.
   - Where content may live: only the user's Open WebUI chat history, as user
     session state under ADR-0007 S1 to S4 (30-day purge). Never Qdrant, logs,
     telemetry, traces or the feedback store. How the feedback path keeps it
     out is open point (7).
7. **D7, the bridge registers only approved tools.** The registered set is a
   configured subset of the closed four, default `dataset_read` only. The
   setting can only narrow; a tool outside the four is still a new ADR.
   `job_status`: open point (4).
8. **D8, reactivation gate for this pilot.** ADR-0003's gate applies to
   `dataset_read` as follows:
   - (a) the named investigation is "explain the site's actual PARMLIB,
     PROCLIB and JCL members with the manuals";
   - (b) the source owner approves the read capability, the allowlist and the
     transport in writing before any live read;
   - a dry run comes first, then one live read of an allowlisted member on a
     test LPAR, recorded with ids, outcome and bytes only.
9. **D9, logon-failure breaker.** A logon failure (FTP 530) gets its own
   code (named by #1294) and stops further logons until an operator resets it
   or a cool-down passes, so repeated failures cannot revoke the ID.

## Shared credentials (replaces ADR-0011's z/OS row for the pilot)

| Credential | Class | What it reaches, and the premise |
|---|---|---|
| E3. z/OS bridge's dedicated READ-only RACF ID (this ADR, D3) | Named exception, pilot only | The allowlisted datasets, read-only, through `dataset_read` only. Premise: every user who can reach the reads through the product (every Open WebUI sign-in and admin role, and any other caller open point (6) admits) is on the mainframe team and can already read every allowlisted dataset. If a wider group signs in, or a dataset some team member cannot read joins the list, this needs a new decision first |

## Guarantees and what establishes them

| Guarantee | State that establishes it | Who can change that state | Valid for | What forbids the counterexample |
|---|---|---|---|---|
| G1. Only allowlisted exact names are read | The operator allowlist and the name validation in the bridge, before any FTP command | The operator, through a deployed release | Until the list changes | **Required of #1294:** bypass tests (case, quotes, member, GDG, wildcard) and a fake FTP showing no command was sent |
| G2. No sign-in holder reads data they could not read themselves | The premise (D3), the approved-roles guard and the site's RACF scope of the ID (D4) | The identity team (who holds a role); the operator (role lists); the site (the ID's access, team membership) | Until a role list, group or the allowlist changes | **Required:** render refusal tests; the premise record at the site. Group membership in the IdP is not checked by this repository |
| G3. Nothing is written or submitted | Site controls on the ID (D4); the bridge's pinned verbs | The site security owner; bridge code, through a release | Standing | Pinned verb test (`tests/test_mcp_ftp_bridge.py`); the site's record. The verb pin alone is not the control |
| G4. Content stays out of logs, telemetry, Qdrant and feedback | D6; the bridge and agent log ids, counts and codes only | Product code, through a release | Standing | **Required of #1294:** tests that logs and the answer record of an observation-bearing turn carry no content, per open point (7) |

## Amendments and unchanged clauses

| Record | Was | Now |
|---|---|---|
| ADR-0003 decision 2 | Only `job_status` approved; `dataset_read` needs its own amendment | `dataset_read` approved for this pilot only, under D1 to D9 |
| ADR-0003 decision 3 | Per-caller target authorization by the agent (the `denied` check), one shared SAF credential | For `dataset_read` in the pilot: the allowlist (D2) plus the premise (D3) replace per-caller authorization; off-list targets are `denied` with no call |
| ADR-0003 decision 6 | `unavailable` covers `timeout`, `jes_unavailable`, `upstream_error`, `not_configured` | Adds a logon-failure code (D9) |
| ADR-0003 decision 8 | Plain FTP a recorded risk; FTPS where AT-TLS exists | D5: plain FTP is the expected pilot transport with written owner acceptance; FTPS must verify the server; the mode is pinned, no fallback |
| ADR-0003 "Required of #91" | Includes the per-caller `denied` check | For the pilot, replaced by D2 and D3; screening, delimiting, distinct citation and fixed errors stay required (D6), built under #1294 |
| ADR-0003 reactivation gate | One implementation (#91) wires `job_status` | For `dataset_read`, D8 states the investigation and approvals; #1294 builds it |
| ADR-0011 policy matrix, z/OS, user class | ADR-0003; shared credential does not qualify | `dataset_read` through E3, pilot only. Agent class: still none |
| ADR-0011 shared credentials, z/OS row | Does not qualify | Named exception E3 (table above), pilot only. Per-user access stays the target (follow-up 1) |
| ADR-0005 decision scope (as amended by ADR-0007 and ADR-0010) | Chat history may hold question, answer, excerpt and Team-model tool output | It may also hold dataset observations from Docs-model chats, under the same rules. The feedback store never holds them (D6, open point (7)) |
| ADR-0007 R3, the agent anonymous to users | Safe because every user may read every manual | Unchanged; for dataset reads it is safe only while E3's premise holds |
| ADR-0010 TD1, the Docs model stays tool-free | | Unchanged: reads are planned by the agent, not offered as a tool |
| ADR-0003 decisions 1, 4, 5, 7, 9 and the pinned verb set | | Unchanged |

## Open points (maintainer decides; recommendation each)

1. **Audit attribution on the Docs path.** The Pipe sends no user identity,
   so the pilot as built attributes a read to a request id only (plus the
   Open WebUI chat that holds the answer). Site security decides whether
   per-person attribution is required before the pilot.
   *Recommendation:* the Pipe sends a keyed hash (HMAC with a site Secret, as
   ADR-0010 TD5 does for Splunk) of the Open WebUI user id as a
   non-authorizing audit field; the agent logs it with the request id,
   dataset name, outcome and bytes. It never authorizes anything: any caller
   that reaches the agent's port could forge it, and the NetworkPolicy limits
   that to the namespace. It leaves "the Pipe declares no `__oauth_token__`"
   intact. *Alternative:* forward the verified user token, stronger but an
   ADR-0010 amendment.
2. **Bridge caller authentication.** *Recommendation:* a bearer secret, as
   the feedback service has (ADR-0005), in addition to an ingress
   NetworkPolicy admitting agent pods only, because the bridge holds a
   mainframe credential. *Alternative:* NetworkPolicy only.
3. **Member listing of an allowlisted PDS.** *Recommendation:* not in the
   pilot: a PDS name without a member is refused, because a listing is a
   second read shape with no screening need tested yet. Revisit after the dry
   run.
4. **`job_status`.** *Recommendation:* stays approved (ADR-0003) but is not
   registered in the pilot (D7); Splunk covers jobs.
5. **Transport mode rule** (D5): pinned mode, no fallback, probe without
   logon. *Recommendation:* confirm as written.
6. **Callers other than Open WebUI users.** The console Route (when
   `AGENT_ROUTE=true`) and pods admitted by the agent NetworkPolicy also
   reach the chat and answer routes, and the guard in D3 does not cover them.
   *Recommendation:* render and deploy refuse enabling reads with
   `AGENT_ROUTE=true`, with `AGENT_NETWORK_POLICY=false`, or with extra
   allowed namespaces, until each such caller group is added to the premise.
7. **Feedback and export of observation-bearing turns.** With
   `FEEDBACK_ENABLED`, each finalized answer sends an answer record with the
   question and answer text (ADR-0005), and the rated-case export (ADR-0011)
   carries the chat history. An answer that explains a member quotes it, and a
   later turn's history can carry it. *Recommendation:* for the pilot, render
   and deploy refuse enabling reads together with `FEEDBACK_ENABLED=true`.
   *Alternative:* the answer record carries no question or answer text for a
   turn whose answer or history holds an observation (detected by its fixed
   citation marker), and the export excludes such records.

## Follow-up

1. **Per-user access** in a z/OS Team model with RACF PassTickets: needs
   #1088 (identity), site RACF setup, and an ADR-0003 amendment for
   model-chosen calls. PassTickets would also shorten the useful life of a
   captured credential (**VERIFY** the site's replay and expiry behaviour
   before relying on it).
2. **AT-TLS or FTPS at the site**, after which the pilot moves to verified
   FTPS (D5).
3. The build: #1294 (bridge, agent wiring, chart and operator inputs, site
   pilot record).

## Consequences

- A team member gets answers that cite the site's own members next to the
  manuals, with every non-`complete` outcome stated.
- The pilot rests on a premise the repository cannot check (team membership
  and the ID's RACF scope); the role guard catches only a widened role list.
- Plain FTP exposes the ID's password and the read text on the network path,
  accepted by the site owner in writing and bounded by D3 and D4.
- New site inputs: the ID's Secret, the allowlist, the approved roles, the
  z/OS host and ports for egress, the code page, the transport mode and its
  owner acceptance, and the CA bundle when FTPS is used.
- Verification: the prose row of
  [live-stack](../live-stack.md#verification-minimums) for this record; #1294
  carries the HTTP/lifecycle and packaging/deploy rows.
