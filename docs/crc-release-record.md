# CRC release verification record

Copy outside Git for a candidate; follow [the procedure](crc-release-verification.md).
Keep credentials, snapshots, PDFs, private infrastructure coordinates, and raw
browser captures out of Git. Link protected evidence using local artifact names.

**Transfer decision: BLOCKED**

## Candidate identity

| Field | Value |
|---|---|
| Verification mode: fully live CRC / complementary fallback | NOT SELECTED |
| Fit attempts: 12288 / 10752 MiB, two cold starts, 30-minute workload | NOT RECORDED |
| Model revisions / actual launch flags / WSL settings and hashes | NOT RECORDED |
| Per-lane original tarball checksum and image digests | NOT RECORDED |
| Combined OpenShift + real-model coverage gap (fallback only) | NOT RECORDED |
| Operator / UTC start and finish | NOT RECORDED |
| Full published-main commit SHA / CI evidence | NOT RECORDED |
| Original tarball filename / SHA256 | NOT RECORDED |
| Trusted signing-key fingerprint / out-of-band provenance | NOT RECORDED |
| Signature / member-checksum logs | NOT RECORDED |
| Manifest and packing-record copies | NOT RECORDED |
| Requested pins / bundled platform digests / running imageIDs (all images) | NOT RECORDED |
| CRC version / bundled and running OpenShift version | NOT RECORDED |
| CRC installer URL / published and computed SHA256 | NOT RECORDED |
| Windows edition / CPU / available RAM / disk / CRC allocation | NOT RECORDED |
| WSL networking mode / gateway forwarding and trust | NOT RECORDED |
| Registry image digest / auth and trust evidence | NOT RECORDED |
| Kubernetes context / namespace / storage provisioner | NOT RECORDED |
| Sanitized local env / override files and hashes | NOT RECORDED |
| Synthetic corpus generator / PDF hashes / collection and vector config | NOT RECORDED |
| Original Kind containers / backup and restore evidence | NOT RECORDED |

## Lane decisions

| Lane | Status | Evidence / reason |
|---|---|---|
| Simultaneous-fit experiment and headroom/time series | NOT RUN | |
| CRC: real model or explicitly declared mock computation | NOT RUN | |
| Disposable Kind with both real models (required in fallback) | NOT RUN | |
| CI bundle integrity and fresh bootstrap | NOT RUN | |
| CI full pipeline | NOT RUN | |
| CI gateway/fault contracts | NOT RUN | |
| CI three-worker Kind lifecycle | NOT RUN | |

Fallback requires CRC, real-model Kind, and all required CI jobs on the same
bundle. A skip is not a pass. Attach the reason neither simultaneous allocation
passed and explicitly retain the missing combined test. Synthetic mock citations
prove interface behavior only.

## Required results

Only `PASS` permits promotion. Attach the command or browser procedure, UTC
time, exit status/observation, and evidence path for each row. Keep separate
before/after results where required. Split a row for multiple workloads so a
successful agent check cannot conceal an ingest or OAuth failure.

| Check | Status | Evidence / failure reason |
|---|---|---|
| Credentials and OAuth image access | NOT RUN | |
| Host capacity and preserved Kind backups | NOT RUN | |
| Pinned CRC installation and cluster health | NOT RUN | |
| Required repository checks for candidate changes | NOT RUN | |
| Fresh trusted-signature bootstrap, SHA and member checksums | NOT RUN | |
| Authenticated TLS registry load and uncached node pull | NOT RUN | |
| Gateway probe from application-configured pod before deployment | NOT RUN | |
| Full pipeline, no skipped ingest/search/tracing | NOT RUN | |
| Every workload admitted under restricted-v2 and assigned ranges | NOT RUN | |
| Running image digest reconciliation including OAuth | NOT RUN | |
| PVC writes including ingest scratch, Qdrant, Jaeger | NOT RUN | |
| Qdrant / agent pod replacement and persistent trace recovery | NOT RUN | |
| Repeat deployment and ingest without data/key regression | NOT RUN | |
| Isolated synthetic snapshot restore and query equivalence | NOT RUN | |
| Unauthenticated redirect and authenticated console | NOT RUN | |
| Ingress and Service CA certificate validation / reencrypt Route | NOT RUN | |
| Explicitly cited synthetic answer and follow-up | NOT RUN | |
| Answer / both chat aliases / UI stream completion | NOT RUN | |
| Search and answer/chat traces | NOT RUN | |
| Egress policy / before-and-after public direct-IP negative controls | NOT RUN | |
| Runtime checks repeated under egress restriction | NOT RUN | |
| Separate uncached public-node-pull denial and local-pull success | NOT RUN | |
| Missing/wrong keys, bad CA, hostname mismatch, upstream errors and timeouts | NOT RUN | |
| Wrong embedding dimension, malformed replies, truncated streams fail closed | NOT RUN | |
| Long embedding input / representative RAG prompt under selected profile | NOT RUN | |
| Final tarball checksum unchanged | NOT RUN | |

## Production differences and remaining acceptance

| Property | CRC observation | Production requirement / owner / disposition |
|---|---|---|
| OpenShift version | NOT RECORDED | UNKNOWN |
| Storage driver, topology, RWO semantics | NOT RECORDED | UNKNOWN |
| SCC/RBAC and assigned identities | NOT RECORDED | UNKNOWN |
| Identity provider and authorization policy | NOT RECORDED | UNKNOWN |
| Registry CA, authentication and node pulls | NOT RECORDED | UNKNOWN |
| Network restrictions, DNS and model gateway | NOT RECORDED | UNKNOWN |
| Sizing and multi-node resilience | Single-node rehearsal only | SEPARATE ACCEPTANCE |
| Answer quality | Synthetic deployment checks only | SEPARATE ACCEPTANCE |

Local checks must all pass for transfer. Unknown production requirements stay
explicitly unresolved and prevent claims of production compatibility.

## Sign-off and handoff

- Transfer decision: BLOCKED (change only after all required evidence passes).
- Operator / UTC sign-off: NOT RECORDED.
- Approved tarball SHA256: NOT RECORDED.
- Open blockers and their owners: NOT RECORDED.
- Transfer receipt and destination checksum: NOT RECORDED.
- Air-gap bootstrap / pipeline / acceptance evidence: NOT RUN.

Approval applies only to the recorded bytes. A new bundle or a configuration
fix invalidates prior acceptance and requires another run.
