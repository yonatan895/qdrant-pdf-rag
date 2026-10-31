# Release verification record

Copy outside Git for a candidate; follow [the procedure](release-verification.md).
Keep credentials, snapshots, PDFs, private infrastructure coordinates, and raw
browser captures out of Git. Link protected evidence using local artifact names.

**Transfer decision: BLOCKED**

## Candidate identity

| Field | Value |
|---|---|
| Operator / UTC start and finish | NOT RECORDED |
| Full published-main commit SHA / CI evidence | NOT RECORDED |
| Original tarball filename / SHA256 | NOT RECORDED |
| Trusted signing-key fingerprint / out-of-band provenance | NOT RECORDED |
| Signature / member-checksum logs | NOT RECORDED |
| Manifest and packing-record copies | NOT RECORDED |
| Requested pins / bundled platform digests / running imageIDs (all images) | NOT RECORDED |
| Model revisions / actual launch flags / host RAM and VRAM | NOT RECORDED |
| Registry image digest / auth and trust evidence | NOT RECORDED |
| Kubernetes context / namespace / storage provisioner | NOT RECORDED |
| Sanitized local env / override files and hashes | NOT RECORDED |
| Synthetic corpus generator / PDF hashes / collection and vector config | NOT RECORDED |

## Lane decisions

| Lane | Status | Evidence / reason |
|---|---|---|
| Disposable Kind with both real models | NOT RUN | |
| CI bundle integrity and fresh bootstrap | NOT RUN | |
| CI full pipeline | NOT RUN | |
| CI gateway/fault contracts | NOT RUN | |
| CI Open WebUI operator lane (`kind-openwebui-operator`, stand-in models, including the slow-model `paced` phase) | NOT RUN | |
| CI three-worker Kind lifecycle | NOT RUN | |

Both lanes run on the same bundle. A skip is not a pass. OpenShift-specific
checks are not part of this record; they belong to
[site OpenShift acceptance](install_and_ops.md#site-openshift-acceptance).

## Required results

Only `PASS` permits promotion. Attach the command or browser procedure, UTC
time, exit status/observation, and evidence path for each row. Keep separate
before/after results where required. Split a row for multiple workloads so a
successful agent check cannot conceal an ingest failure.

| Check | Status | Evidence / failure reason |
|---|---|---|
| Required repository checks for candidate changes | NOT RUN | |
| Fresh trusted-signature bootstrap, SHA and member checksums | NOT RUN | |
| Authenticated TLS registry load and uncached node pull | NOT RUN | |
| Gateway probe from application-configured pod before deployment | NOT RUN | |
| Full pipeline, no skipped ingest/search/tracing | NOT RUN | |
| Running image digest reconciliation | NOT RUN | |
| PVC writes including ingest scratch, Qdrant, OpenObserve | NOT RUN | |
| Qdrant / agent pod replacement and persistent trace recovery | NOT RUN | |
| Repeat deployment and ingest without data/key regression | NOT RUN | |
| Isolated synthetic snapshot restore and query equivalence | NOT RUN | |
| Explicitly cited synthetic answer and follow-up | NOT RUN | |
| Answer / both chat aliases / UI stream completion | NOT RUN | |
| Open WebUI with the real models: first pass without a Pipe, second deploy installs it, the 20 checks, K1 off and on | NOT RUN | |
| Open WebUI real answer times inside the CI envelope (no answer verdict `end_s` / `done_s` above 240 s) | NOT RUN | |
| Search and answer/chat traces | NOT RUN | |
| Missing/wrong keys, bad CA, hostname mismatch, upstream errors and timeouts | NOT RUN | |
| Wrong embedding dimension, malformed replies, truncated streams fail closed | NOT RUN | |
| Long embedding input / representative RAG prompt under selected profile | NOT RUN | |
| Final tarball checksum unchanged | NOT RUN | |

## Production differences and remaining acceptance

| Property | Local observation | Production requirement / owner / disposition |
|---|---|---|
| OpenShift version, SCC/RBAC, assigned identities | Not exercised (Kind) | SITE OPENSHIFT ACCEPTANCE |
| OAuth Route, Service CA, reencrypt certificates | Not exercised (Kind) | SITE OPENSHIFT ACCEPTANCE |
| Egress policy and node image-pull isolation | Not exercised (Kind) | SITE OPENSHIFT ACCEPTANCE |
| Storage driver, topology, RWO semantics | NOT RECORDED | UNKNOWN |
| Identity provider and authorization policy | NOT RECORDED | UNKNOWN |
| Network restrictions, DNS and model gateway | NOT RECORDED | UNKNOWN |
| Sizing and multi-node resilience | Single-node rehearsal only | SEPARATE ACCEPTANCE |
| Answer quality | Synthetic deployment checks only | SEPARATE ACCEPTANCE |

Local checks must all pass for transfer. Unknown production requirements stay
explicitly unresolved and prevent claims of production compatibility.

## Sign-off and handoff

- Transfer decision: BLOCKED (change only after all required evidence passes).
- Operator / UTC sign-off: NOT RECORDED.
- Approved tarball SHA256: NOT RECORDED.
- Licensing gate: `python3 scripts/license_inventory.py check --release` result
  (exit code, unresolved count, `release_approval` state) NOT RECORDED; it must pass
  on the packed commit before transfer ([licensing](licensing.md#what-a-release-must-satisfy)).
- Dev kit, when this release ships one: NOT RECORDED. Copy from the output
  of `scripts/devkit/pack.sh` the kit name, the fingerprint (the hash in the
  kit's `SHA256SUMS.sha256`), the 16-hex trust anchor beside it, and the
  anchor bootstrap text exactly as printed. The authoritative bootstrap is
  the copy in [ADR-0009 §8](adr/0009-gap-fork.md#the-anchor-bootstrap-verbatim)
  at the commit the kit was packed from (its MANIFEST `commit:`); if the
  printed text differs from it, do not publish. Publish all four with
  this record on GitHub. The operator carries the fingerprint (with its
  anchor) and the bootstrap by a route separate from the kit transfer, and
  runs step 1 of [the Windows runbook](runbooks/devkit-windows.md) before any
  push.
- Open blockers and their owners: NOT RECORDED.
- Transfer receipt and destination checksum: NOT RECORDED.
- Air-gap bootstrap / pipeline / site OpenShift acceptance evidence: NOT RUN.

Approval applies only to the recorded bytes. A new bundle or a configuration
fix invalidates prior acceptance and requires another run.
