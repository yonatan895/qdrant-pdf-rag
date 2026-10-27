# Helm live-deployment audit (record, 20 September 2026)

Status: dated record, superseded. It is not current acceptance and none of its
observations prove the present release; inline time stamps, host paths and
private evidence locations are not repeated here.

Scope: Helm teardown and redeployment of the complete local stack with the
preserved real corpus and real reasoning/embedding models, plus the gaps found
on the way. Baseline: `cbcc74b0aa0c21a611f8542fb4135b3df32a7434` (merged Helm
migration). The audit treated the preserved corpus, snapshots, private
configuration and volumes as data that must survive.

## What it never ran

| Environment | Not run | Where the requirement now lives |
|---|---|---|
| CRC / OpenShift | Current candidate SCC, Route/OAuth, Service CA, NetworkPolicy and storage acceptance (no authorized CRC startup headroom) | [release gate](release-verification.md), [deploy policy](deploy.md#deployment-policy) |
| Internal site | GitLab import/checkout, Quay push/pull and target-namespace identity (no authorized endpoints) | [install and operations](install_and_ops.md#site-openshift-acceptance) |
| Production topology | Independent-worker 6/3/2 placement and failure tolerance | [Qdrant topology operations](install_and_ops.md#54-qdrant-632-topology-operations-issue-360), [required minimums](live-stack.md#verification-minimums) |

At the audit's end, publication of the real-corpus migration was also
outstanding: completion records and stored points disagreed by 951 legacy
chunks, the alias stayed on the preserved old collection, and the staged
residue removal waited on operator approval. The supported recovery path is
now the full-source migration in
[real-corpus recovery](local-real-corpus.md); never invent completion evidence
or relax the serving gate to finish a run.

Host sizing: six ingest workers were not accepted on the audit host (swap
exhausted, memory stalls, repeated read timeouts) and four workers completed
documents only by retaining verified checkpoints. Neither a historical
one-worker figure nor a brief healthy sample establishes full-run fit.

The audit's durable lesson: a completed document count is not coverage, a
green outer checksum is not a deployed release, and a local Kind run cannot
close OpenShift or site controls.
