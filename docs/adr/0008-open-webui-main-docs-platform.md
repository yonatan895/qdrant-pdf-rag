# ADR-0008: Open WebUI as the main docs platform

- **Status:** accepted (maintainer, 2026-10-26: items 1, 2, 5 to 8); the
  fail-closed rule (items 3 and 4) is pending maintainer confirmation.
  Tracker #1070. Supersedes
  [ADR-0007](0007-open-webui-docs-front-end.md) in part: its "optional",
  "default-off" and "pilot" framing. Every ADR-0007 condition and no-go item
  stays in force. [ADR-0004](0004-operator-console-htmx.md) and
  [ADR-0005](0005-answer-feedback-data-boundary.md) keep the amendments that
  ADR-0007 recorded.
- **Context:** Open WebUI is no longer an experiment. The maintainer chose it
  as the main docs platform (2026-10-26). ADR-0007 made it an opt-in pilot
  behind a default-off flag. Its conditions were written as pilot checks. A
  default-on component needs them as gates that every release and every site
  must pass. Open WebUI also needs site inputs that no default can supply: an
  OIDC client Secret, a Route host and the IdP issuer and client.
- **Decision:**
  1. **Default on.** The chart deploys Open WebUI unless `openwebui.enabled`
     is `false`. The flag stays as the one-flag rollback (ADR-0007 K1): off
     removes the component, keeps its PVC, and renders byte-identical to a
     release that never had it.
  2. **Conditions become release gates.** Every condition in ADR-0007 as
     amended is a gate, currently C1, V1 to V3, R1 to R3, S1 to S4, L1, P1,
     K1 and E1 (the Route-layer block of unenforced endpoints, added with
     #1083), plus the no-go list. A condition added to ADR-0007 later is a
     gate without a change here. A failed gate blocks the release or the
     site, not only a pilot. Evidence is recorded per pin import (P1) and per
     site.
  3. **Fail closed on missing site inputs.** With Open WebUI enabled (the
     default), the render fails with a fixed message when any required input
     is empty. The inputs are `openwebui.secretName`, `openwebui.host`,
     `openwebui.oidc.providerUrl`, `openwebui.oidc.clientId`,
     `openwebui.oidc.allowedRoles`, `openwebui.oidc.adminRoles`,
     `openwebui.idpEgress.cidrs` and `route.destinationCA`. One template
     check covers every required key, because Helm validates the values
     schema before templates run and a schema error would hide the fixed
     message. The message names the missing keys and the opt-out. It
     contains no value and no Secret content. There is never a
     half-configured Open WebUI: no placeholder host, no local login
     fallback, no partial render. The existing refusals (retention window,
     repository-owned Pipe) stay.
  4. **Operator layer and opt-out.** Sites set the operator env
     (`airgap.env`), not chart values. `OPENWEBUI_ENABLED` defaults to true.
     Setting it to false makes `scripts/airgap/map_values.py` emit an explicit
     `openwebui.enabled: false`. That is the K1 rollback and the opt-out for
     `deploy.sh` and `ingest.sh`. The chart-level opt-out is
     `openwebui.enabled=false`. With `OPENWEBUI_ENABLED` on and a site input
     missing (including `ROUTE_DESTINATION_CA_FILE`), the operator path also
     refuses, before any cluster change. Upgrade consequence: a site that
     upgrades from a release without Open WebUI and sets neither the inputs
     nor the opt-out is refused. Nothing is applied and the running release
     is untouched. Release notes and the upgrade runbook state this before
     the first default-on release.
  5. **The console stays the supported fallback and HA path**, frozen to
     fixes ([ADR-0007](0007-open-webui-docs-front-end.md) decision;
     ADR-0004 still owns the console). Retiring it needs its own later ADR.
  6. **Scope is docs Q&A.** Team tools (Splunk, mail, GitLab) are the next
     track on the same platform and need their own ADR. Until then this
     instance holds no tool, tool server or model connection (ADR-0007 V2).
  7. **Scale is one replica, SQLite, at most 100 users**, under the existing
     licence sign-off (L1). High availability (Postgres plus Redis) needs its
     own ADR.
  8. **Trial tooling becomes supported tooling.** The Kind trial scripts stop
     being CI-only trial scripts. The dev environment and the qualification
     checks are supported tooling and an acceptance lane. They run the
     conditions as gates (item 2). The owning task and CI wiring land in
     their own PRs under the live-stack minimums.

## What does not change

- No condition is weakened. Passing a gate means Open WebUI behaves as the
  condition states, not that a setting exists.
- The Pipe is the only model. Open WebUI never reaches Qdrant or a gateway.
- The agent stays the single owner of grounding, refusals, verification state
  and error codes (ADR-0007, "Where a customization goes").
- No-go conditions now stop a release or a site. The fallback is the console
  or `openwebui.enabled=false`.

## Considered options

- **Default on with placeholder values.** It renders a front end that cannot
  authenticate, or one that tempts a fallback login. Rejected: fail closed.
- **Default off, enable per site.** It keeps the pilot framing the
  maintainer retired.
- **Auto-disable when inputs are empty.** It would silently drop the main
  platform on an incomplete upgrade. Rejected: the opt-out must be explicit.
- **Retire the console now.** It removes the HA path. Deferred to a later ADR.

## Consequences

- Every release carries Open WebUI's recurring costs: monthly re-qualification
  (P1), the second content store under S1 to S3, and a single point of
  failure with the console as the HA path.
- Existing sites must act once on upgrade (item 4).
- Checks that render the chart with no site values, such as `helm lint` and
  `helm template`, must now pass a test-inputs file under
  `charts/mainframe-rag/ci/`, which includes `route.destinationCA`.
- Verification follows the packaging/deploy, HTTP/lifecycle and release rows
  of [live-stack](../live-stack.md#verification-minimums), plus the
  front-end acceptance lane (item 8).
