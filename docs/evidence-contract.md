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
revision-separated chunk identity, alias publication/repair, a TTL-cached
physical serving binding, and (#405 E1/MCP1) the
[stored-payload exact-evidence profile](#stored-payload-profile) below with its
HTTP route and read-only knowledge MCP adapter. It does **not** implement the
full `e1.` profile (byte-to-page location map), retirement tombstones,
a per-source caller-entitlement service, admitted-reader leases or automatic
safe GC. The remaining mechanisms are implementation obligations for
#405/#391/#373/#360, not claims of shipped safety.
M1/A1 implements only the existing core's typed dependency boundaries.

## Identity and retained evidence

| Fact | Decision and owner | Failure / independent proof |
|---|---|---|
| Source family and revision | Preserve `identity.source_rev_key`, original source SHA-256 and normalized labels; printed `doc_id` and mount path are not revision identity. Existing UUID5 chunk keys and four chunk types remain unchanged. | Two editions sharing a printed number remain distinct; mount relocation preserves identity. Existing revision/identity suites own this proof. |
| Representation | `representation` continues to own recipe compatibility; its digest is not a build ID. | Same recipe with different corpus or a fresh repair must not recreate an old reference. |
| Build | Add a full UUID allocated once under the publication lock, persisted in the durable publish sidecar before any candidate collection write, then identically in paired control metadata. Resume reuses that UUID only after exact input/pair checks. Distinct repair/rollback-by-rebuild gets a fresh UUID. | Sidecar absent/corrupt/mismatched: refuse, never infer from a name or recipe. A crash after allocation resumes the recorded build; an unrecorded candidate is quarantined from publication. |
| Canonical evidence unit | A v1 reference returns one complete retained `Chunk.text`, keyed by its existing `Chunk.chunk_id`. Retain its exact normalized UTF-8 bytes, all known atomic ranges and complete byte-to-source locations with the revision/hash. `UnitSpan` is a boundary annotation, not an independently addressable unit. Preserve original source bytes or an authorized immutable source version in protected artifact storage. | A digest validates identity, not extraction quality. Missing/corrupt retained content cannot be replaced by re-extraction or current text. Tables/code are whole units or explicit refusal, never silent partial success. |
| Exact reference v1 | Opaque `e1.` plus unpadded URL-safe base64 of build UUID (16 bytes), existing chunk UUID (16), and SHA-256 of the canonical evidence envelope (32): 89 ASCII characters total. Reject noncanonical encoding, wrong length and unknown versions. The envelope uses UTF-8 JSON, sorted keys, compact separators, no NaN, and includes text, revision/hash, representation binding, location and atomic type. | Full build + chunk + envelope digest bind the exact excerpt and provenance. No path, URL, collection name, user identity or authorization grant is accepted from a reference. Test exact producer/consumer round-trips and every changed envelope field independently. |

### Complete-chunk granularity and location

V1 identifies **the whole retained chunk**, not a `UnitSpan`, a prompt prefix,
a page, a table row selected from a chunk, or a separately minted sub-chunk ID.
`chunk_id` is exactly the existing UUID5 Qdrant point identity produced by
`make_chunk_id`; source revision, heading, start page and ordinal inputs do not
change. Lookup is `(build_id, chunk_id)` in that build's immutable data target,
followed by full envelope-digest verification. There is no second sub-unit index
or UUID namespace. Overlapping chunks remain distinct existing points; a ref
never searches for a matching span in another chunk. No parent ID is needed:
the envelope's `chunk_id` is the parent binding for every range it contains.

`UnitSpan(start, end, kind)` currently uses character offsets over stripped
chunk text, has no UUID, and may contain multiple atomic items in one chunk.
V1's `atomic_spans` retains every complete `kind == "atomic"` range as UTF-8 byte
intervals. Convert offsets once against the exact retained text using the byte
length of each character prefix, never by treating character counts as bytes.
Spans are ordered, non-overlapping, nonempty and UTF-8-boundary aligned. They
need not cover intervening prose or separators. They are annotations only:
exact read returns **all** chunk bytes, including all atomic spans. A budget
smaller than the complete chunk yields 413, never a prefix or just one span.
This does not promise that a chunk contains a whole source manual/example/table;
it promises that none of the producer's declared atomic items is partial.

The current `_build_blocks` knows a page range but `Chunk` retains only
`page_start` and a compressed display label. Those are insufficient to mint v1
with complete location. E1 must retain a byte-to-page map before that lossy
projection, without changing chunk identity or existing payload meaning. Every
byte belongs to exactly one ordered `locations` segment: no gaps or overlaps.
A `source` segment has its actual **one-based physical page** and printed label
or null. Conversion from today's zero-based parser page indexes is explicit.
An inserted join separator has origin `separator` and both page/printed label
null; it is never attributed to a guessed page. Adjacent segments with identical
origin/page/label are coalesced for canonical encoding. Unknown printed labels
stay null even when neighboring labels are known. Neither `page_start` nor a
compressed range string is presented as a complete location map.

A declared atomic item may cross physical pages: preserve one atomic interval
with multiple location segments, including any recorded separator, and return
it whole. If ingestion split that item across chunks, lost its cross-page
boundary, capped its unit metadata to unknown (`units=None`), or cannot recover
the complete location map from retained ingest observations, v1 issuance is
unavailable for that chunk. Do not reconstruct mappings from current text,
page-label arithmetic or another generation. E1 must add these producer proofs
before enabling issuance; this document does not assert today's chunker already
retains them. Known prose with no atomic items uses `atomic_spans: []`; unknown
atomic boundaries are not silently converted to an empty list.

The proposed v1 envelope has exactly these keys: `schema` (integer 1),
`build_id` and `chunk_id` (lowercase hyphenated UUID strings), `source_revision`
(the existing revision key), `source_sha256` and `representation_sha256`
(lowercase 64-character hexadecimal digests), `text` (string), `chunk_type`
(the existing four-type vocabulary), `atomic_spans`, and `locations`.
Each atomic span has exactly integer `start`/`end` byte offsets `[start, end)`.
Each location has exactly `start`, `end`, `origin` (`source` or `separator`),
`page` and `printed_label`, with the constraints above. There is no ambiguous
top-level `page`, `printed_label` or `unit_id`. No optional absent or additional
keys are accepted. Empty text, invalid ranges or incomplete locations refuse
issuance. Range boundaries cannot split a UTF-8 code point.

Encode JSON with `ensure_ascii=False`, `sort_keys=True`, compact separators
`(',', ':')`, and `allow_nan=False`, then strict UTF-8 without BOM or trailing
newline. Reject unpaired surrogates and booleans where integers are required.
The digest covers the entire encoded envelope, including the build/chunk UUIDs
repeated in the token. E1 must use independent literal expected bytes such as
[the three design witnesses](evidence-v1-examples.md), not an oracle calling its
own encoder. Reads perform no second whitespace/Unicode normalization. The
public token is a locator, not a secret or bearer capability; consumers treat
it as opaque.

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

The L1 writer introduces publication sidecar version 2 and build binding schema 2
independently of E1's future per-chunk evidence envelope. The full UUID is recorded
under the target writer lock before collection writes, then stored with logical
corpus, physical data/control pairing and input fingerprints in the verified
publication receipt. Existing completed generations receive no retroactive build
identity. This binding does not by itself enable exact references or exact reads.
The maintainer's rollout decision is to refuse unfinished old-format builds:
finish them with the matching old release or explicitly abandon them before a new
verified build. An older executable ignoring these controls is not a qualified
reader/writer for newly published builds.

The retained control alias is also the retirement lookup path. A tombstone is
not evidence content and cannot authorize a caller by itself. Its schema records
the full build UUID, retired state and per-chunk provenance required by the current
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
| retained → retiring → retired | Explicit policy-authorized retirement blocks new admissions first; drain all admitted readers; verify retention/recovery obligations; persist a retired tombstone in the paired control collection before deleting data and its build alias. Retain the control alias and minimal per-chunk revision/digest/location metadata needed to authorize and distinguish retired references. Purging these records requires a separately approved tombstone horizon. | No drain/retention proof: refuse deletion. Tombstones must not disclose existence to unauthorized callers. #391 owns enforcement, #373 disclosure and #360 restore. |
| retained → serving rollback | Verify old data/control/schema, compatible executable/model/config and policy; swap ordinary alias under the same writer boundary. | Incompatible/missing artifacts refuse; no fallback to other vectors or regenerated text. |

New build schema-2 receipts retain a versioned content seal over the complete
verified stored data and non-receipt controls, including vectors. Publication
and retry enforcement lives in [ingest publication](ingest.md#publication-contract).
This preserves the expected member set independently of surviving completion
records; deleting a revision and its completion cannot erase its membership
from the seal. Completed older builds remain readable without this capability;
there is no retroactive certification. The read-only seal verifier distinguishes
absence from a match and raises on mismatch. A future administrative rollback
must require the match **and** the schema/executable/config/placement/policy
checks above. The seal alone is not a rollback or disposal operation, an access
grant, or proof of backup/recovery obligations.

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
content cache. Content cache keys include full build/chunk/digest; cached text is
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
| complete chunk exceeds explicit budget | 413 `evidence_budget_exceeded` | No truncated table/code presented as complete. |
| complete chunk | 200 typed evidence result | Exact canonical bytes/provenance/digest; no LLM, embedding or approximate search. |

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
observations remain a separate bounded port, not a generic query/command proxy
([ADR-0003](adr/0003-zowe-mcp-read.md)).

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
| Future v1 evidence controls and `e1.` ref | Existing routes remain additive-compatible only after their schema checks pass. | Exact read only after full controls, digest and current-access checks. | One writer emits v1 after reviewed schema introduction; chunk UUID5 and vocabulary remain unchanged. |
| Completed build binding schema 1, no content seal | Preserve validated published/retained reads. | Build identity alone does not supply complete immutable evidence. | Never backfill a seal; publish a newly verified successor for that capability. Unpublished sealed candidates require the matching old release or explicit abandonment. |
| Build binding schema 2 with content-seal schema 1 | Supporting readers require a well-formed mandatory seal and valid immutable aliases; they do not rescan the corpus on each request. | No exact-read API is introduced by this storage certificate. | Publication/steady verification compare the full stored seal; future administrative rollback must additionally validate compatibility and policy. |
| Unfinished v1 publication sidecar from the old writer | Completed legacy generations remain readable; unfinished candidates are not adopted. | No reference capability is inferred. | The new writer refuses without mutation. Finish with the matching old release, or explicitly abandon the candidate and allocate a newly verified build. |
| Unknown mandatory control/ref version | No interpretation by field resemblance. | Refuse before serving. | Explicit migration/qualified executable pair, never silently downgrade controls. |
| Retained previous release/build | Serve only its tested executable/config/schema combination. | Old references resolve only if that reader supports their version and retained bytes. | Restore matching artifacts/config together; record the actual supported previous pair at release qualification. |

An older executable is not declared compatible merely because its parser ignores
extra fields. E1's schema rollout must make unknown mandatory versions fail closed
at **all** serving/publication readers before v1 can be enabled. No indefinite
historical compatibility, archive import, schema mutation or default flip is
approved by this document.

<a id="stored-payload-profile"></a>
## Stored-payload profile (implemented, #405 E1/MCP1)

**Status:** implemented with evidence for builds that carry a build binding
(#515/#516); `e1.` stays reserved and unimplemented. **Decision owner:**
`agent.evidence` (`EvidenceService`, `build_evidence`, `PUBLIC_FAILURES`).
**Why a profile:** the `e1.` envelope needs a per-byte page map that the ingest
path does not retain, and adding one means changing stored payloads and the
extraction-rule modules (forced re-ingest, protected format). This profile
therefore uses only what a point already stores, with a distinct `ep1.` prefix,
so no `ep1.` reference can be mistaken for an `e1.` one and no payload,
UUID5 chunk key or `chunk_type` changes. Decoders reject every other prefix.

| Aspect | Decision |
|---|---|
| Reference | `ep1.` + unpadded URL-safe base64 of build UUID (16 bytes), existing chunk UUID (16), SHA-256 of the canonical envelope (32): 90 ASCII characters. Canonical-only decode; wrong length/prefix/padding/non-zero trailing bits/zero build UUID are `invalid_evidence_reference` before any storage contact. A reference is a locator, never a grant. |
| Envelope | UTF-8 JSON, sorted keys, compact separators, `ensure_ascii=False`, no NaN. Keys: `schema` (1), `profile` (`stored-payload`), `build_id`, `chunk_id`, `generation_fingerprint` (the verified control's `gen_fp`; not an identity), `source_revision` (`source_rev`), `source_sha256` (`sha256`), `doc_id`, `title`, `product`, `version`, `heading_path`, `chunk_type` (the four-type vocabulary only), `text`, `atomic_spans`, `physical_page_start`/`physical_page_end` (one-based; stored indexes are zero-based), `printed_label`. |
| Atomic spans | Stored `units` are character offsets; the envelope carries the `atomic` ones as UTF-8 **byte** ranges `[start,end)`. The key is `null` when `units` is absent: the writer omits it both for known prose and for capped span lists, so "not recorded" is never presented as "no atomic items". Malformed spans refuse. |
| Location | The stored chunk page span only (`physical_page_end` is null for points lacking it; `printed_label` is the stored display string, possibly a range, null if empty). There is **no** byte-to-page map and the response does not imply one. |
| Issuance | Search mints a reference only for hits served from a generation whose control record decodes to a build binding for the configured logical corpus and whose phase is published/retained. Legacy/in-place/unbound generations, sealed-unpublished builds and payloads that cannot form a complete envelope get `reference: null`; nothing is invented and search never fails because minting failed. |
| Read | Parse; resolve the build only through its immutable per-build data/control aliases (never the ordinary serving alias, never a name from the request); require the paired control alias, the control record's build UUID/physical/logical corpus to match; retrieve the one point; derive scope; **authorize**; apply optional narrowing `product`/`version`; rebuild the envelope and compare digests; enforce the budget; re-ask the authority's policy version before returning. No content or authorization cache exists. Only `get_aliases`, `collection_exists` and `retrieve` are called, so the read-only serving credential suffices; no model, embed, rerank or approximate search runs. |
| Bounds | `max_bytes` (query, 1..1 MiB) is clamped by `EVIDENCE_MAX_BYTES` (default 65536); a whole chunk over the budget is `413`, never a prefix or single span. `EVIDENCE_TIMEOUT_S` (default 10) bounds the read; every wait is a real async storage await, so cancellation propagates to the in-flight call. |
| Access | `EvidenceAccess` is the port (`authorize`, `current_version`). The deployed object is `SharedCorpusAccess`, the explicit shared-corpus mode that matches today's search exposure; #373 replaces it. The trusted caller is built by the transport, never from request fields. A denied caller always gets the unavailable outcome, decided before digest/size/corruption outcomes can be observed. |

Public outcomes (the mapping table above, as implemented). Implemented:
400 `invalid_evidence_reference`, 401 `authentication_required` and 503
`access_unavailable` (reachable only through a non-default access object), 404
`evidence_unavailable` (denied, unknown build, product/version mismatch), 503
`evidence_unavailable` (missing/redirected controls, missing or changed stored
point, malformed stored fields), 413 `evidence_budget_exceeded`. Added:
504 `evidence_timeout` and 502 `upstream_error` / `evidence read failed` for a
storage fault (fixed text; the exception type is logged only).
**Not implemented:** 410 `evidence_retired`. No tombstone writer exists (#391),
so a retired or removed build is indistinguishable from an unknown one and reads
as 404; it is never answered with successor text.

Example (synthetic; the literals are asserted independently in
`tests/test_evidence_service.py`). A build `00000000-0000-4000-8000-00000000000a`
holds chunk `ab6cacd2-9c9e-5072-a508-16896576a84d` with the 32-byte text
`//A EXEC PGM=ONE\n\n//B µ PGM=TWO`:

```text
POST /v1/search {"query":"step"}
  -> hits[0].reference = ep1.AAAAAAAAQACAAAAAAAAACqtsrNKcnlBypQgWiWV2qE01x8NlDrvJUGFAamE2sviehLFI2sHOEVJsCJ9y1jl4Vw
GET /v1/evidence/<reference>[?max_bytes=..&product=..&version=..]
  -> 200 {"completeness":"complete","text":"...","text_bytes":32,
          "digest":"35c7c3650ebbc95061406a6136b2f89e84b148dac1ce11526c089f72d6397857",
          "atomic_spans":[{"start":0,"end":16},{"start":18,"end":32}],
          "location":{"physical_page_start":3,"physical_page_end":4,"printed_label":"iii-iv"},
          "build_id":..., "chunk_id":..., "source_revision":..., "doc_id":..., ...}
  -> 404 {"code":"evidence_unavailable","message":"the requested evidence is not available"}
```

The downstream consumer is `mcp/knowledge.py` (tools `knowledge_search`,
`evidence_read`): an adapter over this HTTP surface with no Qdrant, model,
entitlement or reference code, no forwarded incoming credentials and no SDK
dependency (hand-rolled JSON-RPC like the FTP bridge). It speaks as its own
deployment identity to `KNOWLEDGE_API_BASE_URL`; approving that trusted-caller
boundary and hosting it (Helm/sidecar) are rollout decisions, not made here.

**Evidence:** `tests/test_evidence_service.py` (literal envelope/reference
bytes, every-field sensitivity, malformed references, alias swap/repair with
same recipe and changed corpus, removed build, redirected/missing controls,
changed stored data, malformed payloads, budget, revocation between reads and
between admission and response, policy outage, deadline, cancellation, HTTP
envelopes) and `tests/test_mcp_knowledge.py` (HTTP/MCP parity for success,
unknown, malformed, budget, scope, denied, policy-down, unauthenticated and
corrupt outcomes against the real app; cancellation; no-bypass import check).
A disposable-Qdrant exercise of the same read path ran on 6333 with a `wt405_`
prefix and was deleted (see the PR record). Known gaps: `e1.` location map,
tombstones/410 and retention (#391), per-source entitlement and revocation
timing approval (#373), restore/rollback (#360), hosting of the MCP adapter,
and `list_sources` (not delivered: no bounded source index exists yet).

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
