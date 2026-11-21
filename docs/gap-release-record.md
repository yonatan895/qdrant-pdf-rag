# Gap release record

Template for one gap release ([runbook](runbooks/gap-release.md),
[ADR-0009 §5](adr/0009-gap-fork.md#5-gap-releases)). Copy it to
`gap/records/YYYY-MM-DD-gap-release-<n>.md` in the fork and merge it through
an ordinary MR after the release; evidence rows are appended, never
rewritten. Keep credentials, manual text, PDFs and private infrastructure
coordinates out of it. The upstream gate keeps its own
[release record](release-record.md).

**Promotion decision: BLOCKED**

## Release identity

| Field | Value |
|---|---|
| Tag / full commit sha / tree id (`gap_tag`, `sha`, `gap_tree`) | NOT RECORDED |
| `GL#` issue / approver / sign-off UTC | NOT RECORDED |
| Release identity (sha256 of `SHA256SUMS`) / tarball sha256 | NOT RECORDED |
| Signer / signing path (Vault key and version, or fallback pod) / UTC | NOT RECORDED |
| Gap anchor fingerprint, compared with the carried value | NOT RECORDED |
| Base upstream sha (`gap_base`) / base tarball sha256 / upstream anchor fingerprint | NOT RECORDED |
| Installed release sha and origin whose scripts packed and verified | NOT RECORDED |
| Tag pipeline id and per-job status for the sha (recorded, not a control) | NOT RECORDED |
| Ledger: each `GL#`, patch sha and paths; the upstream-owned paths verify printed | NOT RECORDED |
| Replaced layers (0 in increment 1); per image: manifest and config digests | NOT RECORDED |
| Doc paths changed but not reflected in `reference-docs.tar.gz` (base copy, D7) | NOT RECORDED |
| Data-compatible with the previous release (yes/no; no needs the data steps rehearsed) | NOT RECORDED |
| Previous release kept for rollback (sha, origin, workspace path) | NOT RECORDED |

## Gate

States: `PASS` (run in the gap on this bundle or sha, with evidence);
`INHERITED` (not selected by the installed release's classifier on the
upstream-owned delta, and `PASS` in the base release's record: cite it);
`WAIVED` (the second approver, a reason and the lane owner, listed first
in this record, never counted as `PASS`); `NOT RUN`; `FAIL`. Promotion
needs every required row `PASS` or `INHERITED`. A `NOT RUN` required row
blocks it.

| Check | Required | Status | Evidence / reason |
|---|---|---|---|
| Tag pipeline on `gate`, every lane | always | NOT RUN | |
| Licence check on the tagged tree | always | NOT RUN | |
| `release.sh verify` from the installed release (`VERIFIED` line) | always | NOT RUN | |
| Rehearsal bootstrap with both anchors, sha and member checksums | always | NOT RUN | |
| Authenticated TLS registry load (same digests as the base: record "identical digest") | always | NOT RUN | |
| Gateway probe from the application pod | always | NOT RUN | |
| Full pipeline on a synthetic corpus, no skipped ingest, search or tracing | always | NOT RUN | |
| Running image digest reconciliation (rehearsal, and again at promotion) | always | NOT RUN | |
| Cited answer, follow-up, chat aliases, stream completion, traces | always | NOT RUN | |
| Site OpenShift acceptance (go-live checks) | always | NOT RUN | |
| PVC writes, pod replacement and trace recovery, repeat deploy and ingest, snapshot restore | when the delta selects `packaging` | NOT RUN | |
| Open WebUI checks (needs a rehearsal host and IdP client) | when the delta touches the docs front end | NOT RUN | |
| `load`, `agent_probes`, `ha`, `eval_retrieval` lanes | when selected | NOT RUN | |
| Rehearsal teardown: namespace empty | always | NOT RUN | |
| Promotion: sha256 of `SHA256SUMS` equals the release identity above | always | NOT RUN | |
