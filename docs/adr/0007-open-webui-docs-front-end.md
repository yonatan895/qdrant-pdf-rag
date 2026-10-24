# ADR-0007: Open WebUI as an optional, single-replica docs front end

- **Status:** accepted (maintainer, 2026-10-24; #1071, tracker #1070).
  Accepting this record does not enable anything: the chart component stays
  off by default, and a site enables it only after every condition below
  passes. Amends [ADR-0004](0004-operator-console-htmx.md) and
  [ADR-0005](0005-answer-feedback-data-boundary.md) as stated below.
- **Context:** the maintainer wants one team-owned front end with SSO and
  group-scoped capabilities (request, 2026-10-24). This record covers only the
  first step, a docs-only pilot for at most 100 users. Open WebUI fits the
  docs half only as a thin client of the agent. As shipped, it conflicts with
  several rules. It embeds its own RAG and vector store, and its official image
  runs as root. It phones home unless configured not to, and an offline flag
  alone does not block egress. It keeps chats server-side with no retention
  setting and no encryption at rest, and admins can read every chat. Its
  licence is BSD-3 plus a branding clause, which is not OSI-approved. Security
  fixes land only on its latest release, and High advisories arrive every few
  weeks. Connected to the agent directly, its requests can fail the strict
  request schema (`extra="forbid"`), its background task calls reach the agent
  as full answers, and the verification state is lost.
- **Decision:** with a default-off chart flag, deploy one pinned Open WebUI
  replica that serves the documentation corpus to signed-in employees. It
  serves it only through a repository-owned Pipe over the agent's streaming
  `/v1/chat`. The pilot must first pass the conditions below in an offline
  Kind trial (#1073), recorded in `docs/records/`. The console (ADR-0004) stays
  the supported fallback and the HA path, frozen to fixes. Retiring the
  console needs its own later ADR. Team tools (Splunk, mail, GitLab) are a
  separate track with their own ADR, likely on a separate instance. This
  instance holds no tool, tool server or model connection.

## Conditions (each is a check; one failure blocks enablement)

The trial and every pin import (P1) record evidence for each condition. A
condition is met only when Open WebUI behaves as stated. Proof that a setting
exists does not count. The environment wins at every start
(`ENABLE_PERSISTENT_CONFIG=False`). Two residuals remain. A live admin edit
applies until the next restart. Admin-created Functions, models and valves are
not environment configuration. The mitigation is that only the hash-checked
Pipe is provisioned and the admin role is limited to the team.

- **C1 Admin edits do not survive a restart.** The trial changes a setting in
  the admin UI, restarts the pod and shows the environment value back in force.
- **V1 No second vector-store content.** The image is the upstream slim
  variant, with no vector-store URL configured. Built-in RAG, Knowledge, chat
  uploads, memories, notes, web search, the built-in knowledge tools, the code
  interpreter and code execution are all off. Open WebUI is never pointed at
  this repository's Qdrant. Proof: a battery of refused upload, knowledge and
  memory attempts, then an inspection of the data directory. It must contain
  no vector-store directory or file, and no uploaded files. A non-empty store
  is an acceptance failure.
- **V2 Answers only through the agent.** Open WebUI holds no gateway or LiteLLM
  key. It has no OpenAI or Ollama connections, so the default connections are
  off. Direct connections, user-created models and user Tools/Functions are
  off, and the Pipelines server is not deployed. The only model is the docs
  Pipe. Its Functions are repository files installed by the deploy step, with
  their hash checked against the repository. Community Functions and
  frontmatter pip installs are refused. Task features (titles, tags,
  follow-ups, autocomplete, query generation) are off. The Pipe also answers
  any Open WebUI `__task__` call with fixed text and never forwards it to the
  agent.
  Proof: over a multi-turn trial, agent requests equal user turns.
- **V3 Contract fidelity.** The Pipe sends exactly the agent's `ChatRequest`
  shape. It renders validated citations with their supplied excerpts as source
  events, and it shows `verification_state` and the script-review warning.
  The Pipe maps every stable agent error code to a fixed user-visible text
  defined in the Pipe, with no agent or upstream text, and that table is
  tested. An error frame, or a stream with no final frame, shows a fixed
  "incomplete" banner. The Pipe never
  yields exception or upstream text. The fixed-error rule binds this
  repository's surfaces. Open WebUI's own error texts are accepted as
  third-party behaviour, and the trial shows that no agent or upstream text
  passes through them. The Pipe is stateless. It does no retrieval, prompt
  assembly, tool planning or LLM calls of its own.
- **R1 Non-root.** The pod is admitted under `restricted-v2` with an arbitrary
  UID: non-root, ALL capabilities dropped, RuntimeDefault seccomp and no
  privilege escalation. There is no `anyuid` and no root. Writable paths are
  explicit mounts: the data PVC plus `emptyDir` volumes. The secret key and
  OIDC client secret come from operator-created Secrets. The image is the
  upstream slim release plus one derived layer that changes only ownership
  and permissions (maintainer, 2026-10-24). The layer is built in the
  connected factory, with no source change and no Node build. Both digests are
  pinned and recorded: the deployed derived digest and its upstream base.
- **R2 Offline, zero egress.** Offline mode is on. The version check,
  community sharing, model downloads, STT/TTS and the Pyodide CDN path are off.
  An egress-deny NetworkPolicy allows only the agent Service, cluster DNS and
  the IdP. The control is the policy, not the offline flag. Proof: 0 egress
  attempts behind the Kind egress boundary, and no external fetch from the
  browser.
- **E1 Unenforced endpoints are blocked before the pod.** (Maintainer
  decision 2026-10-24, #1070/#1071.) A feature that v0.12.3 disables only in
  its UI, not in the route handler, stays reachable by API. The router must
  refuse those API prefixes (`/api/v1/files`, `/api/v1/retrieval`,
  `/api/v1/knowledge`, `/api/v1/audio`) for every spelling that Open WebUI's
  server decodes to them, including percent-encoded ones. The control must
  match the decoded, normalised path; an OpenShift path Route matches the raw
  path and cannot (see the #1083 analysis), so it is not sufficient alone.
  Re-checked on every import (P1): list each router's handlers and sort them
  into server-enforced and UI-only. Proof: site curl checks in
  `docs/deploy.md` return a refusal for each path and variant.
- **R3 Identity.** Login is OIDC against the identity team's IdP (AD FS, with
  Keycloak also in use). Every employee signs in with a personal account.
  Groups and the admin role come from IdP claims. The local login form,
  signup, password auth and trusted-header auth are off. Sessions expire in
  hours, not the four-week default, because without Redis a sign-out does not
  revoke a session. Open WebUI is exposed only through its own Route, with
  re-encrypt TLS and a Service CA certificate. The Route-exposure preflight
  refuses any other Route in front of its Service. The agent stays anonymous
  to Open WebUI users (#373), which is safe because the docs corpus is read by
  everyone.
- **S1 Storage.** The pod runs one replica with a `Recreate` rollout, a single
  worker and SQLite. Its data lives on a kept RWO block PVC, never NFS:
  validate refuses an NFS-named class, as for the other writable volumes,
  and site acceptance confirms block semantics.
  Multiple replicas (Postgres plus Redis) need a new ADR.
- **S2 Bounded history.** Chat retention is **30 days** (maintainer,
  2026-10-24). A repository-owned scheduled purge enforces it. It deletes the
  chats, messages, shared snapshots and files whose last update is older than
  30 days. It then reclaims the freed database pages, so the text is gone
  from the file. The pattern is ADR-0005/#804: failures are retried and
  counted and are never reported as success. Proof: the stored rows are gone,
  then the next ordinary chat succeeds. Users may delete their own chats. Chat
  sharing and admin export are off.
- **S3 Protected copies and admin access.** PVC backups hold manual excerpts
  and pasted incident text. They are protected records that never leave the
  enterprise. They follow the same 30-day window: the purge job also removes
  backups older than 30 days. Admin chat access is off, but that toggle only
  hides chats in the UI. Admins, in-process Functions and anyone with access
  to the PVC or the DB can still read them. This is an accepted residual, kept
  small by limiting the admin role to the named owners through an IdP group.
  Encryption at rest, if required, comes from the storage class, because Open
  WebUI provides none. The maintainer, as corpus owner, accepted chat
  content on the PVC and in backups under these rules (2026-10-24). If that
  acceptance is withdrawn, the no-go item below applies.
- **S4 No content in telemetry.** The log level is INFO or higher, and the
  audit level never records request or response bodies. A canary scan of the
  container logs finds 0 hits.
- **L1 Licence.** The branding stays unmodified: no custom name, logo, theme
  or injected CSS. The licence permits that at any user count. Users are
  capped at 100. The maintainer signed off as repository owner on 2026-10-24.
  The sign-off covers internal use with the Open WebUI branding unmodified, at
  an expected 100 users or fewer. Like the other
  [owner decisions](../licensing.md#owner-decisions), it is a maintainer
  disposition, not a legal approval. It is recorded in the inventory, bound to
  the pinned digest, when the image is pinned. Any rebrand needs a new decision
  and either written permission from the copyright holder or an enterprise
  licence.
- **P1 Patch policy.** The owner is the maintainer (yonatan895, 2026-10-24).
  About monthly, an agent prepares the import of the latest upstream release:
  the import PR plus the qualification run. The maintainer reviews it, merges
  it and loads the bundle. Security fixes land only on the latest release, so
  staying on an old pin means accepting published vulnerabilities. Each import
  is a dedicated pin-bump PR. It re-runs every condition above, captures Open
  WebUI's request body against the Pipe, and upgrades a copy of the PVC. Open
  WebUI migrates its schema at startup, so the pre-upgrade snapshot is kept
  for rollback. Switch-off trigger: the rollback flag (K1) disables Open WebUI
  if no qualified import exists 14 days after a published High or Critical
  advisory affecting the pinned version (maintainer, 2026-10-24).
- **K1 Rollback by one flag.** Turning the chart flag off removes Open WebUI
  and keeps its PVC. With the flag off, the render is byte-identical to the
  render without Open WebUI. The agent and the console are untouched. The
  limits: Open WebUI chats cannot move into the console, and a migrated DB
  rolls back only through the pre-upgrade snapshot.

## No-go conditions

Any one of these, from the trial, site acceptance or an owner's refusal, stops
the pilot. The console remains the only supported front end.

- It cannot run under `restricted-v2` with an arbitrary UID without `anyuid`
  or root.
- An egress attempt or runtime download remains that configuration cannot
  remove.
- The vector store cannot be kept provably empty.
- A model other than the agent becomes reachable, or a background LLM call
  cannot be switched off.
- The Pipe cannot preserve citations, abstention and the verification state.
- Retention cannot be enforced, or the security or corpus owner refuses chat
  content on a PVC and in backups.
- The Route cannot meet the documented pin and Secret checks.
- The IdP cannot supply OIDC group claims.
- The licence disposition is withdrawn, or a rebrand becomes necessary
  without written permission or an enterprise licence.
- The console fallback is not kept.

## What changes in ADR-0004 and ADR-0005

- **ADR-0004 non-goal 2 (no secondary backend service): amended.** The
  optional Open WebUI deployment is a second front end and a client of the
  agent. It runs in its own pod in the release namespace, not alongside the
  agent.
- **ADR-0004 non-goal 3 (no server-side session DB): amended for Open WebUI
  only.** While the flag is on, Open WebUI stores its users' chats under S1 to
  S3. The console still keeps its history in browser `localStorage`, and the
  agent pod stays stateless.
- **ADR-0004 non-goal 4 and §3 (OAuth ingress): amended for Open WebUI only.**
  Open WebUI authenticates users itself through OIDC (R3). The console Route
  keeps its oauth-proxy sidecar.
- **ADR-0004 §4 CSP: does not extend to Open WebUI.** Its HTML/artifact
  preview and code execution stay off. Whether security headers can be added
  is a trial item.
- **Unchanged:** non-goal 1 (no Node in our images or packaging). The upstream
  image is a third-party artifact, as OpenObserve is under ADR-0006, and a fork
  stays excluded. Also unchanged: non-goal 5 (no
  remote assets, R2), the reasoning-model-only rule (V2) and §2 for the
  console.
- **ADR-0005 decision ("only in the feedback store"): amended.** Open WebUI
  chat history may hold question, answer and excerpt text as user session
  state under S1 to S4. It is not a quality-record store. Open WebUI's
  built-in ratings and evaluations are off. Answer records still reach the
  feedback service through `/v1/chat` as before. Open WebUI ratings wait for a
  UI-agnostic rating route, which would amend ADR-0003's POST set. Telemetry
  still holds no content.
- **Reading of "no LangChain / second vector DB / new orchestration
  framework":** these rules bind this repository's code, dependencies and
  answer path. A pinned, unmodified third-party image may contain such
  libraries only while their features are off and outside every answer path
  (V1, V2), as with OpenObserve (ADR-0006).

## Where a customization goes

Work down the list and stop at the first home that fits:

1. **The agent**, which is mandatory for anything that decides what is true,
   allowed or recorded: grounding, refusals, the verification state, limits,
   error codes, efforts, storage and ratings. It is preferred whenever a front
   end only displays API data.
2. **A repository-owned Pipe or Filter**, pinned and hash-checked. It only
   adapts request shapes or renders data the agent returns, uses documented
   extension points, is stateless and is small enough to unit-test.
3. **A fork or patch, avoided.** It is considered only for a security or
   data-handling property that neither 1 nor 2 can deliver. It needs its own
   ADR, because it requires a Node build (ADR-0004 non-goal 1), voids the
   licence decision and adds a rebase on every pin import.
4. **Console only, or dropped.** The console keeps the no-JavaScript path,
   browser-only history, the themes, the accessibility contract, the
   reasoning-effort selector, the Markdown export with its handling banner,
   and ratings.

## Considered options

- **Console only.** It gives no SSO or group-scoped front end.
- **The agent as a plain OpenAI connection.** This breaks on the strict
  request schema, sends task calls to the agent and loses the verification
  state.
- **The knowledge MCP tool behind a general model.** This bypasses citation
  validation and abstention.
- **A rebranded fork.** It needs a Node build and enterprise licensing.
- **High-availability Open WebUI.** It adds new stateful dependencies.

## Consequences

- The front end becomes a single point of failure. The console stays the HA
  path.
- A second content store and a monthly re-qualification are recurring costs
  that P1 owns.
- Enabling the pilot takes the packaging/deploy, HTTP/lifecycle and release
  rows of [live-stack](../live-stack.md#verification-minimums), plus a Kind
  front-end lane (#1073).
