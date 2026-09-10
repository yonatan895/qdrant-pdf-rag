# ADR-0002: agent-fetched live z/OS state via read-only Zowe MCP (issue #90)

- **Status:** proposed
- **Context:** ADR-0001 decided "Splunk stays system of record (context
  in, not crawl)" and `/v1/answer` accepts caller-supplied
  `splunk_context`. Operators now ask live-state questions the manuals
  cannot answer ("why did JOB123 fail last night", "what is in
  `SYS1.PARMLIB(IEASYS00)` on SYSA"). Letting the AGENT fetch live
  state supersedes ADR-0001 for this scope, so this ADR exists.
  Reframing from #90: the interface is Zowe MCP (datasets, JES spool,
  USS, job status), not Splunk REST/SPL — Splunk stays caller-supplied
  context exactly as ADR-0001 says.
- **Backend (amended pre-merge):** a minimal in-repo FTP bridge
  (`mcp/` package, Python stdlib `ftplib` only), NOT the official
  `zowe-mcp` server tarball — verified from its README that its only
  live backend is SSH (via `zowex-sdk`), and the target z/OS 2.2 has no
  sshd and none may be started. The MCP interface (initialize /
  tools-list / tools-call over stdio + Streamable HTTP, the four
  allowlisted tools) is unchanged, so agent code stays
  transport-agnostic and a future SSH backend swaps without agent
  changes. z/OS FTP covers all four tools natively (dataset RETR,
  `SITE JESINTERFACELEVEL=2` spool/status, USS CWD+RETR).
- **Accepted risk:** plain FTP on the wire inside the isolated network
  (no probe path to verify FTPS; sshd unavailable). Bounded by the
  read-only SAF profile (a sniffed credential can still only read),
  secret-mounted credentials, and per-call byte caps. Revisit if the
  network posture changes. z/OS 2.2 EOS (2020, unpatched FTP daemon)
  is a flagged environmental risk with the same mitigations; periodic
  review.
- **Decision:** the agent may fetch bounded read-only live context
  through the vendored Zowe MCP server and inject it as delimited
  untrusted blocks (the `splunk_context` precedent); manual citations
  stay the only manual grounding, live sources cite distinctly.
- **Routing taxonomy:** `manual` (meanings, syntax, procedures — manuals
  only, never touch MCP),   `live` (job failures, dataset/USS contents,
  spool output — manuals provide background only), `hybrid` (validate-
  then-verify, e.g. JCL valid per manual AND ran clean per spool).
  Deterministic classifiers route; trap queries never reach MCP.
- **Allowlist (phase 1, closed):** dataset read, JES spool read, USS
  read, job status. No submit/write/console/TSO — adding one is a new
  ADR, never a flag flip.
- **Auth:** read-only SAF profile; credentials via mounted secret/env,
  never git. Only z/OS access controls enforce real boundaries.
- **Audit + dry-run:** every fetch logs request id, tool, args, bytes,
  elapsed_ms (ids/counts, never dataset/spool text); dry-run mode plans
  without calling.
- **Fallback:** unreachable/slow MCP degrades to manuals-only with an
  honest marker — never fail the answer, never silently omit.
- **Safety:** tool results are untrusted data (screened as strictly as
  retrieved chunks; the server marks them untrusted too — defense in
  depth). Bounded calls (max 2), byte caps with truncation suffix,
  dedicated short timeout distinct from the answer timeout. Rollout
  default-off (`zowe_mcp_enabled=false`).
- **Migration:** caller-supplied `splunk_context` unchanged; live MCP
  context is a sibling block, never a replacement.
- **Consequences:** new `mcp/` bridge package (stdlib-only: no wheelhouse
  pin, no Node tarball, no new image — sidecar runs the agent image with
  an `--mcp-serve` entrypoint), fake-`ftplib` hermetic tests plus mock
  mode for sim/CI, golden entries with live-state expectations.
  Per-tool degradation when the site's FTP JES interface is limited.
  Supersedes ADR-0001 only for agent-fetched Zowe state; everything
  else in ADR-0001 stands.
