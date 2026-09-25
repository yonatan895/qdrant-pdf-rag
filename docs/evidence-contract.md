# Evidence, publication and access contract

<a id="evidence-contract"></a>
## Scope and status

Decision proposal for #482 M1/A0 and #405 E0, based on main
`b36053da1c2ca62d2c9db8547a6cbe7d13634ae1` and #405's G1–G5 design record.
Maintainer review of this contract is required before its new persistent format
ships. This document owns the exact-evidence design; existing behavior remains
owned by [ingest](ingest.md#publication-contract),
[serving](agent.md#serving-contract), [HTTP/model policy](agent.md#http-model-contract)
and [deployment](deploy.md#deployment-policy). No runtime behavior changes here.

Current main has shared answer preparation/finalization, scoped retrieval,
revision-separated chunk identity, alias publication/repair and a TTL-cached
physical serving binding. It does **not** implement a public exact-evidence
service, durable reference/build schema, current caller-entitlement service,
admitted-reader leases or automatic safe GC. Required mechanisms below are
implementation obligations for #405/#391/#373/#360, not claims of shipped safety.
M1/A1 implements only the existing core's typed dependency boundaries.

## Identity and retained evidence

| Fact | Decision and owner | Failure / independent proof |
|---|---|---|
| Source family and revision | Preserve `identity.source_rev_key`, original source SHA-256 and normalized labels; printed `doc_id` and mount path are not revision identity. Existing UUID5 chunk keys and four chunk types remain unchanged. | Two editions sharing a printed number remain distinct; mount relocation preserves identity. Existing revision/identity suites own this proof. |
| Representation | `representation` continues to own recipe compatibility; its digest is not a build ID. | Same recipe with different corpus or a fresh repair must not recreate an old reference. |
| Build | Add a full UUID allocated once under the publication lock, persisted in the durable publish sidecar before any candidate collection write, then identically in paired control metadata. Resume reuses that UUID only after exact input/pair checks. Distinct repair/rollback-by-rebuild gets a fresh UUID. | Sidecar absent/corrupt/mismatched: refuse, never infer from a name or recipe. A crash after allocation resumes the recorded build; an unrecorded candidate is quarantined from publication. |
| Canonical evidence unit | Retain exact normalized UTF-8 text plus source revision/hash, physical page, printed label when known, unit ID and atomic type in the generation. Preserve original source bytes or an authorized immutable source version in protected artifact storage. | A digest validates identity, not extraction quality. Missing/corrupt retained content cannot be replaced by re-extraction or current text. Tables/code are whole units or explicit refusal, never silent partial success. |
| Exact reference v1 | Opaque `e1.` plus unpadded URL-safe base64 of build UUID (16 bytes), existing unit UUID (16), and SHA-256 of the canonical evidence envelope (32): 89 ASCII characters total. Reject noncanonical encoding, wrong length and unknown versions. The envelope uses UTF-8 JSON, sorted keys, compact separators, no NaN, and includes text, revision/hash, representation binding, location and atomic type. | Full build + unit + envelope digest bind the exact excerpt and provenance. No path, URL, collection name, user identity or authorization grant is accepted from a reference. Test exact producer/consumer round-trips and every changed envelope field independently. |

The proposed v1 envelope has exactly these keys: `schema` (integer 1),
`build_id` and `unit_id` (lowercase hyphenated UUID strings), `source_revision`
(the existing revision key), `source_sha256` and `representation_sha256`
(lowercase 64-character hexadecimal digests), `text` (string), `page`
(positive integer physical page), `printed_label` (string or null), and
`chunk_type` (the existing four-type vocabulary). No optional absent keys or
additional keys are accepted in v1. Encode JSON with `ensure_ascii=False`,
`sort_keys=True`, separators `(',', ':')`, and `allow_nan=False`, then strict
UTF-8 without BOM or trailing newline. Reject unpaired surrogates and booleans
where integers are required. The digest covers the entire encoded envelope,
including the build and unit IDs repeated in the reference. E1 must freeze this
schema with literal expected bytes, not an oracle calling the implementation. Missing printed labels remain null, never guessed
from the physical page. Unicode text is retained exactly as normalized at ingest;
reads do not perform a second whitespace/Unicode normalization. The public token
is a locator, not a secret or bearer capability. Consumers treat it as opaque.

Build lookup reuses Qdrant's existing alias/control boundary: the trusted storage
adapter derives private per-build data and control aliases from the configured
logical corpus and canonical build UUID. The writer creates both immutable build
aliases in the same alias operation that switches the ordinary serving alias,
after verification. Their names and target pairing are writer-owned adapter
metadata, never accepted from a public request.
Consumers receive neither storage aliases nor collection names. A read resolves
and pins the target, then verifies the paired control's complete build UUID and
schema; redirected/wrong controls fail closed. Publication retries inspect all three
aliases: all committed means read-only finalization; inconsistent state refuses.
Never rebind an existing build alias to a new build or use a current alias to
repair an old reference. This extends the existing alias publication operation;
it adds no second database or general catalogue service.

The retained control alias is also the retirement lookup path. A tombstone is
not evidence content and cannot authorize a caller by itself. Its schema records
the full build UUID, retired state and per-unit provenance required by the current
access policy. If that policy cannot establish access, return the same unavailable
outcome as an unknown reference. Crash after tombstoning but before physical
deletion remains retired; cleanup retries never republish it. Do not delete the
control lookup before the approved tombstone horizon. No such horizon is approved
at this baseline, so automated tombstone purging remains unsupported.

Retained normalized evidence is the read source; originals and verified backups
are the recovery source. Initial exact-read service does not reconstruct missing
content on a request. Restore is an explicit verified administrative operation;
unavailable/retired references remain explicit outcomes until a matching retained
build is restored. Keeping every historical ANN index forever is not promised.

## Publication and reader lifetime

The selected normal mode is immutable alias publication under one authorized
publisher per logical corpus. Existing in-place mode remains isolated legacy
maintenance requiring operator quiescence/drain; it cannot issue v1 references.
No launcher default is changed by this decision. Multi-host concurrent writers
remain unsupported; a local file lock and RWO volume are not distributed locks.

| Transition | Preconditions and durable ordering | Failure outcome / owner |
|---|---|---|
| absent → allocated | Acquire target coordination; validate explicit intended-set diff; persist UUID and inputs before candidate writes. | `publish` / `run_ingest`: refuse conflicting live writer or ambiguous sidecar, preserve existing data. |
| allocated → building → sealed | Prepare non-live corpus/control pair; write content, completion/coverage and immutable evidence; verify full intended membership, representation and required placement before sealing. | Unknown residue, missing control, incomplete source observation or placement refuses cutover. #391/#360 own real storage/crash tests. |
| sealed → published | Verify immutable controls and observed prior target; atomically create both build aliases and switch serving alias; finalize inventory/sidecar. | Pre-swap failure preserves old serving target. Post-swap interruption finalizes the same build read-only; never mutates live data on retry. |
| published → retained | Successor publication changes the ordinary alias, not old content/control/build aliases. | Admitted old readers and old refs remain bound to the old build, subject to current access policy. |
| retained → retiring → retired | Explicit policy-authorized retirement blocks new admissions first; drain all admitted readers; verify retention/recovery obligations; persist a retired tombstone in the paired control collection before deleting data and its build alias. Retain the control alias and minimal per-unit revision/digest/location metadata needed to authorize and distinguish retired references. Purging these records requires a separately approved tombstone horizon. | No drain/retention proof: refuse deletion. Tombstones must not disclose existence to unauthorized callers. #391 owns enforcement, #373 disclosure and #360 restore. |
| retained → serving rollback | Verify old data/control/schema, compatible executable/model/config and policy; swap ordinary alias under the same writer boundary. | Incompatible/missing artifacts refuse; no fallback to other vectors or regenerated text. |

No automatic GC is introduced. Retain current, previous and any generation
needed by an admitted reader or explicit evidence obligation. Capacity preflight
must refuse a new build before destructive work when retention cannot be met.
A retention duration, tombstone horizon, recovery objective or physical-worker
claim requires its platform/domain owner; there is no invented default here.

Initial safe retirement uses the supported operator maintenance boundary:
stop admissions on **all** serving replicas, cancel/drain operations, confirm
all readers have ended, then retire under the writer lock. No multi-host lease
service is introduced. Rolling online GC is unsupported until a tested shared
admission/lease protocol exists. TTL expiry, a pending manifest, process-local
cache invalidation or one replica's active count is never proof of global drain.
Every operation carries a configured total deadline and bounded work/response
budget. Cancellation ends response work and releases its admission in `finally`;
non-cancellable thread work must finish or remain accounted for before drain can
succeed. Do not claim cancelling an await kills an already running worker thread.

## Trusted caller, scope and failure disclosure

Transports establish a trusted caller context from the approved ingress/service
identity mechanism. Model/user request fields, product/version filters, session
IDs and forwarded headers do not establish entitlement. A shared-corpus mode
requires an explicitly approved cohort and direct-Service reachability policy;
per-source restrictions require a policy mapping before activation. No implicit
allow-all or invented identity provider is part of this contract.

The shared use case intersects caller authorization with requested product/release
scope before search, source listing or exact lookup. Explicit scope cannot widen
on fallback; unknown applicability remains distinct candidates or clarification.
An exact old reference is authorized on **every new read** before consulting a
content cache. Content cache keys include full build/unit/digest; cached text is
not cached permission. Positive authorization is request-scoped initially.
Unavailable policy fails closed. Recheck the authority's policy version before
the first response byte, and reauthorize if it differs from admission; previously emitted bytes cannot be
revoked. In-flight work uses a bounded admission, and emergency revocation needs
the same stop-admission/drain mechanism. No instantaneous cross-system revocation
promise is made. Platform owners must approve this timing model for their data.

| Internal outcome | Public additive exact-read mapping | Required behavior |
|---|---|---|
| malformed / unsupported reference | 400 `invalid_evidence_reference` | Fixed message; no lookup or model call. |
| unauthenticated caller | 401 `authentication_required` | No source existence disclosure. |
| denied or unknown reference | 404 `evidence_unavailable` | Same fixed envelope; do not reveal whether another principal can read it. |
| authorized retired reference | 410 `evidence_retired` | No current-generation substitution. |
| corrupt/missing retained bytes or controls | 503 `evidence_unavailable` | Internal reason remains distinct, public fixed message. |
| unavailable access policy | 503 `access_unavailable` | No cache-based authorization bypass. |
| atomic unit exceeds explicit budget | 413 `evidence_budget_exceeded` | No truncated table/code presented as complete. |
| complete unit | 200 typed evidence result | Exact canonical bytes/provenance/digest; no LLM, embedding or approximate search. |

These are proposed **additive** service outcomes, not changes to current endpoint
status codes or messages. Existing source absence, retrieval no-hits and answer
verification states keep their owners and established behavior. Failure logs use
fixed codes and opaque IDs/counts, never manual text, tokens or upstream bodies.

## Public use-case boundary and compatibility

A small async service exposes `search_knowledge`, `read_evidence`, `list_sources`
with trusted caller context supplied separately from user arguments. Typed
outcomes preserve complete/partial/stale/failed/denied distinctions internally.
HTTP/console/chat and a future MCP consumer validate wire input and map results;
they never implement another entitlement, reference or citation rule. Source
observations remain a separate bounded port, not a generic query/command proxy.

Example of the proposed additive exact-read contract (not an implemented route):

```text
read_evidence(caller=trusted_context, reference=opaque_ref, budget=caller_budget)
  -> Evidence(text=exact_text, source_revision=revision, location=location,
              build_id=build_uuid, digest=canonical_digest, completeness="complete")
  -> EvidenceFailure(code="evidence_unavailable")
```

| Stored/API version | Existing consumers | New exact-evidence service | Write/migration rule |
|---|---|---|---|
| Current representation/completion formats, no full build/evidence record | Preserve existing supported search/answer behavior. | Cannot mint v1 refs; explicit capability unavailable. | Explicit verified new-generation publication; never invent missing provenance in place. |
| v1 build/evidence controls and `e1.` ref | Existing routes remain additive-compatible only after their schema checks pass. | Exact read only after full controls, digest and current-access checks. | One writer emits v1 after reviewed schema introduction; chunk UUID5 and vocabulary remain unchanged. |
| Unknown mandatory control/ref version | No interpretation by field resemblance. | Refuse before serving. | Explicit migration/qualified executable pair, never silently downgrade controls. |
| Retained previous release/build | Serve only its tested executable/config/schema combination. | Old references resolve only if that reader supports their version and retained bytes. | Restore matching artifacts/config together; record the actual supported previous pair at release qualification. |

An older executable is not declared compatible merely because its parser ignores
extra fields. E1's schema rollout must make unknown mandatory versions fail closed
at **all** serving/publication readers before v1 can be enabled. No indefinite
historical compatibility, archive import, schema mutation or default flip is
approved by this document.

<a id="four-design-walks-and-implementation-witnesses"></a>
## Four design walks and implementation witnesses

| Scenario | Required result and counterexample | Owning code/test boundary |
|---|---|---|
| Old ref after alias move, forced repair, then retirement | Return identical old text/revision while retained and authorized; after explicit retirement return retired/unavailable, never successor text. Identical recipe is not identical build. | `publish.resolve_publish_staging`, paired controls, future exact-read use case. Existing `test_forced_repair_never_touches_live_during_build`, `test_interrupted_same_contract_repair_resumes_recorded_build`, `test_subsequent_ordinary_run_recognizes_repair_steady_state` establish current alias repair; #405 adds independent literal reference/content tests and #391 retirement/drain tests. |
| Scope revoked with warm cache | New request is refused even with warm bytes/generation cache; forged scope cannot broaden access. In-flight disclosure follows the approved admission timing above. | Future trusted-context/access port plus `ServingGate`; #373/#405 tests must revoke between two reads and between admission/first emission, without clearing the cache as a shortcut. Current serving TTL does not establish authorization. |
| Crash between data/control/publication writes | Resume the recorded UUID; no mixed build/control acceptance or premature reference minting; post-cutover retry is read-only. | `write_publish_state`, coverage/manifest verification, alias operation, finalization. Existing `test_swap_failure_preserves_live_then_recovers`, `test_forced_repair_swap_then_crash_finalizes_read_only`; #391/#360 add durable interruption and real-server pair/placement tests for the new schema. |
| New consumer using only public service | Thin consumer receives the same outcome/scope/evidence as HTTP without importing `agent.app`, Qdrant/admin or deployment modules. Supplying a write-capable storage dependency must fail the boundary checks. | `answer_core`, `ports`, future knowledge service; #369 A1 adds import/type/capability checks and sync/async/stream parity cases in existing answer/transport suites. No implemented MCP claim. |

## Platform and domain input record

On 25 September 2026 the maintainer confirmed that no approved access,
retention or revocation rules are available yet. They remain explicit platform
inputs; none are inferred from a successful login, issue status or local test. Record approved values and their owner before
activating the corresponding capability; private addresses, keys and corpus text
remain outside git.

| Owner/input | Known contract | Still required for activation |
|---|---|---|
| #373 platform identity/access | OAuth Route and internal Service are separate exposure paths; filters are not entitlement. | Approved cohort/principal mapping, source grants, trusted headers/audience, direct-Service reachability and revocation timing. |
| #405/#391 domain retention | No automatic GC; preserve retained evidence and explicit retirement. | Retention/tombstone obligations, legal holds and capacity admission policy. |
| #360/#446 platform recovery | Production 6/3/2 on distinct workers; development 1/1/1 is non-HA. | Actual topology, backup/restore pair, RPO/RTO and retained previous-release qualification. |
| Model/platform owner | Designated HTTP model endpoints, per-leg Secret refs, existing attested model/dimension/window owners. | Approved revisions/windows/CA and deadline/budget values for deployment; no guessed site capacity. |

Unknown site inputs do not prevent A1's behavior-preserving typed refactor or
synthetic tests, but do block claiming deployed access/retention guarantees.
