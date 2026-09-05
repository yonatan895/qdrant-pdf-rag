# Air-gap deployment internals reference

Owner: this file. Operator runbook: `docs/install_and_ops.md` §4. CI job
reference: this file §7. Design overview: `docs/architecture.md` §3.

> Skeleton for the docs epic (PR-A). Section headings below mark the full
> content that lands in PR-F. Each section documents script contracts,
> precedence, exact triggers, and sizing rationale with `file:line` evidence.
> One fact, one owner — do not duplicate `install_and_ops.md` runbook steps.

## 1. Pipeline stages

Scope: `pack → load → deploy → ingest → smoke` data flow; `pipeline.sh`
flags (`--skip-load`, `--skip-ingest`, `--dry-run`); ingest-only-if-`CORPUS_PVC`;
banner vocabulary; no-rollback rule.

## 2. Environment precedence

Scope: `OPERATOR_ENV_KEYS` allowlist (explicit non-empty env wins; empty stays
unset); `AIRGAP_ENV` vs `./airgap.env` selection; alias resolution
(`REGISTRY_INTERNAL`, `OPENSHIFT_NAMESPACE`, `IMAGE_SHA←HEAD`,
`EMBED_BASE_URL` derivation); maintenance warning (new `.example` key must
join the list or it regresses to file-wins); `require_env` all-missing
reporting; `EMBED_MODE=hash` refusal and its case-sensitivity limit.

## 3. Render pipeline

Scope: vendored chart + kustomize overlays; placeholder substitution and the
integer/bool unquote-then-quote rule; `fail_on_placeholders` regex;
`wire_pull_secret` indent preservation + DNS-name constraint;
`imagePullSecrets=null` fail-closed; tag-suffix strip (`-unprivileged`);
`QDRANT_RELEASE`-prefixed secret names; Jaeger opt-in gating; rollout
timeouts and failure diagnostics; `AGENT_ROUTE` `oc`-only rule.

## 4. Signing and provenance

Scope: bundle member list; `SHA256SUMS` coverage (and what the `.sig` does not
cover); tar-manifest vs list digests (arch-resolved, by construction);
`signed: true` vs `ephemeral` honesty label; trust roots in order
(`SNEAKERNET_TRUSTED_PUB`, fingerprint compare, HTTPS); load-side re-verify
(4 digests + SHA equality); `bootstrap.sh` standalone twin rationale.

## 5. Sizing and security

Scope: prod values (3×500Gi RWO, CPU/mem, PDB, topology spread) and why;
`restricted-v2` UIDs + `anyuid` escape hatch; plaintext p2p rationale;
ClusterIP-only rule; agent stays 2 tiny replicas; ingest Job
(backoff/deadline/resources, `ingest-work` auto-create, corpus never created);
Kind/CI shrink knobs that must never reach prod.

## 6. Images and pins

Scope: `images.txt` digest contract (tag-form invalid); UBI non-root build;
`--no-index` wheelhouse + baked BM25; mock exclusion via `.dockerignore`;
`qdrant-client` tracks server/chart; `pull-chart` unpinned-drift risk;
`oc-mirror` tag-vs-digest staleness.

## 7. CI inventory

Scope: per-workflow job tables (`ci`, `load`, `bench`, `e2e`, `opencode`)
with triggers, secrets, timeouts, and skips (md-only, PR-vs-main,
fork/secret gating); air-gap GitLab scope (hygiene + pytest + gate-l1, no
e2e/load/deploys); pinned third-party versions + the unpinned local-path
exception; teardown guarantees.
