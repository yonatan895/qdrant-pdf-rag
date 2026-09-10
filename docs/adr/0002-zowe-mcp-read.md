# ADR-0002: agent-fetched live z/OS state via read-only Zowe MCP (issue #90)

- **Status:** proposed
- **Context:** ADR-0001 decided "Splunk stays system of record (context
  in, not crawl)" and `/v1/answer` accepts caller-supplied
  `splunk_context`. Operators now ask live-state questions the manuals
  cannot answer ("why did JOB123 fail last night", "what is in
  `SYS1.PARMLIB(IEASYS00)` on SYSA"). Letting the AGENT fetch live
  state supersedes ADR-0001 for this scope, so this ADR exists.
  Reframing from #90: the interface is Zowe MCP (official `zowe-mcp`
  server: datasets, JES spool, USS, job status), not Splunk REST/SPL —
  Splunk stays caller-supplied context exactly as ADR-0001 says.
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
- **Consequences:** new vendored server pin (SHA + tarball sha256 +
  LICENSE/NOTICE, air-gap sneakernet like BM25 weights), new MCP-server
  Deployment + ClusterIP, golden entries with live-state expectations,
  mock-mode backend for hermetic CI. Supersedes ADR-0001 only for
  agent-fetched Zowe state; everything else in ADR-0001 stands.
