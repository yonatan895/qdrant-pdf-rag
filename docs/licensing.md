# Third-party licensing and release obligations

Contract / scope: issue #376 (parent #358; inventory mechanism shared with #371).
This file records what the repository can establish from its own pinned artifacts
and what a release must carry. It is **not legal advice and not an approval**:
a declared license, an SBOM or a passing check never settles whether a component
may be distributed or used. Decisions marked **owner decision** belong to the
maintainer and the organization's qualified legal/open-source owner.

Status: partially implemented. The record, the check and the bundled notices
exist; no owner disposition is recorded yet, so `release_approval` is absent and
the unresolved list below is open.
Source of authority: #376 and its triage comments (19 and 24 September, 1 October
2026): close on a documented owner disposition, never on CI alone.
Decision owners: `licenses/inventory.json` (the record),
`scripts/license_inventory.py` (check and notices), `scripts/airgap/pack.sh`
(bundle member), and this file (obligations, options).

## Responsibility boundaries

| Category | What it is | Who is responsible |
|---|---|---|
| Public code | This repository, Apache-2.0 per `pyproject.toml` and `LICENSE`. Its license does not cover dependencies. | Maintainer |
| Transferred artifacts | Both app images (wheels, base layers, htmx, BM25 weights), Qdrant/Jaeger/oauth-proxy images, vendored chart, Task binary, repo bundle. Only these are what a release gives to the enterprise. | Release owner; obligations below |
| Local model simulation | Weights a developer downloads for local gateway/embed runs. Not in any image, chart or bundle. | Each developer or CI owner, per upstream model license |
| Platform model service | Production reasoning/embed/rerank models behind the gateway. This repo consumes HTTP endpoints; it does not redistribute weights. | Platform team and its consumption agreement |
| Protected corpus | Vendor manuals/runbooks. Never in git or a bundle. Rights to hold and process them are a separate corpus-governance decision. | Corpus owner; protected records |

## What is recorded and how it is derived

`licenses/inventory.json` holds one record per locked runtime wheel (60) and per
non-Python component (15, including the three non-distributed boundaries above).

- **Python wheels** are derived offline from the exact locked wheel bytes (59,
  METADATA `License-Expression`, `License` field or classifier, plus license-file
  names and SHA-256) or, where the locked wheel was not on the host, from a
  version-matching installed dist-info (1: `urllib3` 2.8.0). `evidence` says which.
  `license_spdx` is the normalized value and `spdx_basis` says how: 39 declared
  expressions, 17 normalized from the `License` field, 3 from classifiers, 1 from
  license-file text (`py-rust-stemmers` declares nothing). A reviewer must treat
  normalized values as claims to confirm, not as findings.
- **Other components** record the license the upstream project declares or the
  vendored license text (`spdx_basis`), bound to the exact bytes: `images.txt`
  pin and digest, chart archive and its inner `LICENSE`, htmx files, Task pin and
  license digest, BM25 manifest, skills license/notice. Image labels recorded in
  this repository: none. The only archives observed locally (an older pack) carry
  no license label on Qdrant or Jaeger, so image licenses are the upstream
  project's, with layer contents not enumerated.

License classes (conservative, from `license_inventory.py`): `permissive`,
`weak-copyleft` (MPL, LGPL), `strong-copyleft` (GPL), `network-copyleft` (AGPL),
`election-required` (dual license without a recorded election), `proprietary`
(`LicenseRef-*`), `unknown`. AND takes the most restrictive operand; an OR whose
alternatives differ needs a recorded `elected` value. Runtime wheel result: 57
permissive, 2 weak-copyleft (`certifi`, `tqdm`: MPL-2.0), 1 election-required
(`pymupdf`). 14 wheels are binary; their declared license may not describe native
libraries vendored inside them (numpy lists 17 license files; most others one).
The dev/build profiles are not shipped and are not inventoried.

## Unresolved items (owner decisions open)

| Component | Why unresolved |
|---|---|
| `pymupdf` 1.28.2 | Dual AGPL/commercial; see the decision record. |
| `flatbuffers`, `loguru`, `onnxruntime`, `tokenizers` | The wheel ships no license text under dist-info, so the image does not carry the text this class of license expects to accompany copies. Supply upstream text on a connected host or record why it is not needed. |
| UBI 9 base image | UBI EULA and the license of every RPM (including GPL/LGPL packages and any source-offer duty) are not enumerated; RPM inventory has not been extracted from the actual archive (external to this host: needs the shipped archive and `rpm` data, #371). |
| Qdrant, Jaeger images | Upstream project license recorded; base-OS and bundled-module layers not enumerated. |
| oauth-proxy image | Red Hat registry/subscription terms for transferring the image are an owner decision. |
| BM25 weights (`Qdrant/bm25`, rev `22b8d2af71a7`) | No license file in the snapshot and the model-card license was never captured. Confirm on a connected host. |
| Corpus rights | Out of scope here by design; recorded as an excluded boundary. |

`python3 scripts/license_inventory.py check` prints these as `UNRESOLVED` on every
run so they stay visible; `check --release` fails while any remain.

<a id="pymupdf"></a>
## PyMuPDF decision record

Status: **open, owner decision.** The repository does not choose, purchase,
replace or relicense anything. This section lays out facts and options.

Observed facts (source inspection and package metadata, 2 October 2026):

- `pymupdf==1.28.2` is a runtime dependency (`pyproject.toml`, `requirements.lock.txt`),
  a native wheel installed in both application images.
- Its METADATA states "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial
  License". The wheel's only license file is that one line; it contains no AGPL or
  commercial text and does not say AGPL "only" or "or later". Artifex's own terms
  were not fetched (no network) and are the authority to read.
- Imported only under `mainframe_rag/ingest/` (`ibm_pdf.py`, `run_ingest.py`) and
  in `scripts/gate_l1.py` and `scripts/make_synthetic_pdf.py`. The serving agent is
  designed not to import it (comment in `ingest/chunk.py`); no test enforces this.
- The project itself is Apache-2.0; the repository records no election and no
  commercial grant.

Questions only the owner can answer: does transferring the images or bundle to the
enterprise count as distribution; is the ingest job, the agent, or both a
network-facing use that the chosen license reaches; does the chosen license permit
combining PyMuPDF with Apache-2.0 code as shipped; and does an existing Artifex
agreement already cover this use.

| Option | What it involves | Cost to this repository |
|---|---|---|
| A. Rely on AGPL-3.0 | Owner confirms obligations (notices, corresponding-source availability, network-use clause, combined-work terms) for the real deployment and records the election. | Notices in the bundle (done for the declared text, see below); any source-offer mechanism the owner requires. |
| B. Artifex commercial license | Procurement and coverage check by the authorized owner. Contract and keys stay in protected records. | Record only an approval reference; no keys or terms in git. |
| C. Replace the parser | A different PDF library or a separately authorized rewrite. | Large: ingest parsing is extraction-rule code (`extraction_rules_version` change, forced re-ingest, golden/holdout re-baselining); needs its own approved issue. |
| D. Exclude or scope | Do not ship the ingest image/path to this enterprise, or ship only where the owner decides no distribution occurs. | Release scoping change; `status: excluded` with the reason. |

**Maintainer's decision:** choose among A-D (or another), then record it. Until
then `pymupdf` stays `pending-owner-decision` and `check --release` fails.

## What a release must satisfy

1. `python3 scripts/license_inventory.py check` passes (CI and `pack.sh` run it; a
   changed dependency, wheel hash, image digest, vendored asset, chart or notice
   file fails with fixed messages before any image is pulled). Optionally add
   `--wheelhouse DIR` / `--site-packages DIR` to re-derive license metadata from
   the actual wheelhouse and flag a license or license-file change.
2. `check --release` passes: no `pending-owner-decision` entry and a
   `release_approval` whose `approved_digest` equals the current inventory scope
   digest. Any later dependency, license or asset change makes the approval stale.
   `pack.sh` does not enforce `--release`: whether packing itself should block until
   decisions are recorded is the maintainer's decision (one flag in `pack.sh`).
3. The bundle carries `THIRD-PARTY-NOTICES.txt` (signed member in `SHA256SUMS`,
   required by `bootstrap.sh`, copied to `dist/`): the license/class/status of every
   component and wheel, the open owner decisions, the approval state, and the full
   texts of the project LICENSE, the Qdrant chart LICENSE, htmx, the Qdrant and
   Matt Pocock skills licenses and notices, and Task (`task-LICENSE`). It is
   deterministic and readable without any tool beyond a text viewer.
4. Python license texts travel inside each image (`<dist>.dist-info`), not in the
   notices file. Pack-time verification of license files inside the actual image
   layers is not implemented (follow-up with the #371 image reader).
5. Source-offer duties apply only where the owner finds them for a component
   (candidates: GPL/LGPL packages in base layers, AGPL if elected). The repository
   has no mechanism that delivers third-party source; if the owner requires one,
   that is a separately approved packaging change.
6. Keep the approval reference, contracts, license keys and corpus agreements in
   protected release records. Publish only the sanitized disposition.

Recording a decision: set the entry's `status` to `approved` (or `excluded`) with
an opaque `approval_ref`, and for a dual license set `elected`; then set
`release_approval.approval_ref` and `approved_digest` (the digest `check` reports
through `notices`). Do not write a decision on the owner's behalf.

## Refresh

In a dependency or pin change, run
`python3 scripts/license_inventory.py generate --wheelhouse DIR [--site-packages DIR]`
on a host that has the new wheels, review the diff (a changed declared license is re-seeded with a warning and a
new package is `UNRECORDED` until reviewed; decision fields are kept, so re-do the
owner approval for any changed component), then re-run `check`. `generate` never
marks anything approved.
