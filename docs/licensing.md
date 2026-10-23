# Third-party licensing and release obligations

Contract / scope: issue #376 (parent #358; inventory mechanism shared with #371).
This file records what the repository can establish from its own pinned artifacts
and what a release must carry. It is **not legal advice and not an approval**:
a declared license, an SBOM or a passing check never settles whether a component
may be distributed or used. Decisions marked **owner decision** belong to the
maintainer and the organization's qualified legal/open-source owner.

Status: partially implemented. The record, the check and the bundled notices
exist. The maintainer recorded dispositions on 2026-10-23 (issue #376 comment);
only `pymupdf` stays unresolved (legal confirmation pending) and `release_approval`
is absent, so `check --release` still fails.
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

`licenses/inventory.json` holds one record per locked runtime wheel (62) and per
non-Python component (17, including the three non-distributed boundaries above).

- **Python wheels** are derived offline from the exact locked wheel bytes (61,
  METADATA `License-Expression`, `License` field or classifier, plus license-file
  names and SHA-256) or, where the locked wheel was not on the host, from a
  version-matching installed dist-info (1: `urllib3` 2.8.0). `evidence` says which.
  `license_spdx` is the normalized value and `spdx_basis` says how: 41 declared
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
alternatives differ needs a recorded `elected` value. Runtime wheel result: 59
permissive, 2 weak-copyleft (`certifi`: MPL-2.0; `tqdm`: MPL-2.0 AND MIT), 1 network-copyleft
(`pymupdf`, AGPL elected; legal confirmation pending). 14 wheels are binary; their declared license may not describe native
libraries vendored inside them (numpy lists 17 license files; most others one).
The dev/build profiles are not shipped and are not inventoried;
`check` fails if a locked runtime wheel has no record or a record has no locked wheel,
so the 62 are exactly the runtime profile of `locks/cp314-linux-x86_64.json`.
Four wheels (`flatbuffers`, `loguru`, `onnxruntime`, `tokenizers`) ship no license
file; their upstream texts are bound in `licenses/texts/` and rendered into the notices.

## Docs-only tooling (not in the inventory)

The Sphinx API reference (#889) is built from `site-src/reference/requirements.docs.lock.txt`
(28 wheels, `--require-hashes`) by the Pages workflow only. It is not in any
image, wheelhouse, bundle or `locks/`, so `licenses/inventory.json` does not
list it. Declared licenses of the tools: Sphinx BSD-2-Clause, Furo MIT,
docutils (public domain/BSD; its metadata also lists GPL for optional parts, not
used by the build), Jinja2/Pygments-family BSD, `certifi` MPL-2.0, `requests`
Apache-2.0, `typing_extensions` PSF-2.0. The published HTML embeds Sphinx and
Furo static assets (BSD-2-Clause, MIT) and credits both in its footer.

## Owner decisions

Recorded by the maintainer (owner of record: yonatan895) on 2026-10-23, issue #376.
Each entry in `licenses/inventory.json` carries `status`, an opaque `approval_ref`
and a `decision` object (`owner`, `date`, `disposition`, plus item-specific fields);
`decision` does not move the scope digest. The maintainer is not the qualified legal
owner: these are maintainer dispositions, not legal approvals.

| Component | Recorded state |
|---|---|
| `pymupdf` 1.28.2 | `elected: AGPL-3.0-only`, scoped to ingest. **Legal confirmation pending**: status stays `pending-owner-decision` (the one remaining unresolved item) until the qualified legal owner confirms. |
| `flatbuffers`, `loguru`, `onnxruntime`, `tokenizers` | `approved`. Upstream license texts (locked version's tag, source URLs in the inventory) are in `licenses/texts/` and rendered into `THIRD-PARTY-NOTICES.txt`. |
| Qdrant, Jaeger images | `approved` on the upstream Apache-2.0 license. Layer inventory is follow-up #371. |
| UBI 9 base image | `approved` on the UBI EULA after RPM-list extraction: 116 packages (name, version, license tag) from the rpmdb of the pinned digest in `licenses/ubi9-python-314-minimal.rpms.txt` (hash-bound, rendered into the notices). License tags are declared RPM metadata; any source offer would be a separately approved packaging change. |
| oauth-proxy image | `approved` to transfer as an **external dependency**: the Red Hat image is shipped unmodified, not built here (`distributed_by_repo` stays true because the bundle carries it). Escalation, if transfer becomes a problem: maintainer, then platform team. |
| IBM Plex fonts | `approved`; `OFL-1.1` is a permissive class in the checker. |
| BM25 weights (`Qdrant/bm25`, rev `22b8d2af71a7`) | `approved`: the Hugging Face model card at that revision declares `license: apache-2.0` (`https://huggingface.co/api/models/Qdrant/bm25/revision/22b8d2af71a76161e18dd432d2cee0eefa66e412`, checked 2026-10-23). Re-check if the revision changes. |
| Corpus rights | Out of scope here by design; recorded as an excluded boundary. |

`python3 scripts/license_inventory.py check` prints open items as `UNRESOLVED` on every
run so they stay visible; `check --release` fails while any remain.

## Obligations by license class

What the license texts themselves require when a copy is passed on. Section numbers
are those of the standard texts (SPDX identifiers in the inventory). This is a reading
of the texts for planning, **not legal advice or a settled conclusion**; whether an
obligation is triggered by a given delivery is an owner decision (next section).
"Transfer" means putting the images or bundle in the enterprise's hands.

| Class (inventory count) | Conditions the text states | What the bundle carries today |
|---|---|---|
| Permissive: MIT, BSD-2/3, 0BSD, Zlib, CC0, MIT-CMU, PSF-2.0, OFL-1.1 (59 wheels; htmx, Task, skills, Plex, BM25 declared) | Keep the copyright notice and permission/license text with copies (MIT, MIT-CMU, PSF-2.0; BSD-2/3 for binary form in the documentation or other materials supplied). BSD-3 adds no use of the author's name to endorse. Zlib: mark altered versions. OFL-1.1: keep copyright and license with each copy; the font is not sold on its own; Reserved Font Names unusable for modified versions. 0BSD and CC0 state none. | Wheel license files stay in each image under `<dist>.dist-info`; the four wheels without one have bound texts. Vendored asset licenses are in the notices file. |
| Apache-2.0 (21 wheels, counting expressions that include it; Qdrant and Jaeger images, chart, project) | Section 4: give recipients a copy of the License; keep copyright, patent, trademark and attribution notices; if the Work has a NOTICE file, carry its attribution text in a NOTICE file, in documentation or in a display; mark changed files. | License files in `dist-info`; chart `LICENSE`, project `LICENSE` in the notices. Three wheels (`fastembed`, `prometheus-client`, `requests`) carry a `licenses/NOTICE` file, which stays in their `dist-info`; NOTICE content of image layers is not enumerated (#371). |
| Weak copyleft: MPL-2.0 (`certifi`; MPL part of `tqdm`) | Section 3.1: Covered Software stays under MPL in Source Code Form. Section 3.2: for an Executable Form, the recipient must be told how to obtain the Source Code Form by reasonable means, in a timely manner, at no more than the cost of distribution. Section 3.3: a larger work may carry other terms if the MPL rights in the covered files are not restricted. File-level scope. | The wheels are unmodified upstream bytes; their license files are in `dist-info`. No source pointer or source copy is carried. |
| LGPL (no wheel; 43 of 116 UBI RPMs carry an LGPL tag) | LGPL-2.1 sections 4 to 6 and LGPL-3.0 sections 4 to 6: notice, license text, source for the library (or an offer), and the recipient's ability to relink or replace the library. | RPM names, versions and declared tags in `licenses/ubi9-python-314-minimal.rpms.txt` (rendered into the notices); not the license texts or sources. |
| Strong copyleft: GPL (no wheel; 23 UBI RPMs tagged GPL without LGPL, such as `bash`, `coreutils-single`, `grep`, `sed`) | GPL-2.0 section 3 and GPL-3.0 section 6: pass object code only with the Corresponding Source, or with a written offer for it (GPL-3.0: valid for at least three years), or the other means the section lists. | Same RPM list. No source and no offer. Whether these base-layer packages trigger the duty for this delivery is the open source-offer question below. |
| Network copyleft: AGPL-3.0 (`pymupdf`, if elected) | Sections 4 and 5: keep notices and the license text; a modified work is licensed as a whole under the AGPL. Section 6: object code is conveyed with the Corresponding Source by one of the listed means. Section 13: if the Program is modified, users who interact with it remotely must be offered its Corresponding Source. The "combined work" and "interacting remotely" readings are the owner's. | The wheel's one license file is a single line naming both licenses; it holds no AGPL text. The notices file does not carry AGPL-3.0 text or any source. |
| Proprietary or terms-bound: Red Hat UBI EULA, Red Hat registry terms (oauth-proxy), Artifex Commercial | Defined by the agreement, not by a standard text. None of those texts is in the repository; none was fetched. | Recorded as a license reference and owner decision only. Contracts stay in protected records. |
| Not transferred: platform models, local simulation weights, vendor corpus | Governed by their own agreements; excluded boundaries in the inventory. | Nothing. |

Facts the table rests on: counts come from `licenses/inventory.json` (declared metadata,
not findings); the UBI counts come from the RPM header tags, which are declared metadata
too and use free-form strings. `numpy` and other binary wheels may vendor native
libraries under licenses their top-level metadata does not name (numpy's metadata lists
17 license files); pack-time inspection of those is not implemented.

## What the owners must decide

These are legal or maintainer calls. Nothing here is decided by the repository.

| # | Decision | Who | Options | What each implies for the bundle |
|---|---|---|---|---|
| 1 | PyMuPDF: AGPL-3.0 or Artifex commercial (see the record below) | Qualified legal owner; maintainer has recorded A, scoped to ingest, legal confirmation pending | A: AGPL. B: commercial. C: replace the parser. D: exclude the ingest path. | A: add the AGPL-3.0 text to the notices, and a source mechanism that works offline (a hash-pinned source archive of the exact PyMuPDF and MuPDF version in the bundle, or a written offer the enterprise accepts); the owner decides which means and whether the project's own code is covered. B: no AGPL text or source; record an approval reference, change `elected`, no contract or key in git. C and D: separate approved issues. |
| 2 | Does transferring the images or bundle to the enterprise count as distribution or conveying, and does the enterprise's own operation count as network use? | Qualified legal owner | Internal use only, conveyed to a separate legal entity, or something else | If conveying: source duties of the table apply to PyMuPDF, MPL wheels and the GPL/LGPL base-layer RPMs. If not: notices only. |
| 3 | Source offer for base-layer GPL/LGPL RPMs and MPL-2.0 wheels | Qualified legal owner; packaging is a separate approved change | Carry sources or an offer; rely on the base image provider's offer; conclude no duty | Carrying sources makes the bundle larger and adds hashed members to `SHA256SUMS`; relying on a provider needs that provider's terms recorded. |
| 4 | Combination with Apache-2.0 project code | Qualified legal owner | Accept the combination under the elected license; keep ingest as a separate program boundary | No code change; the answer only sets what the notices and any offer say. |
| 5 | Final release approval for this exact inventory | Maintainer with the legal owner | Record `release_approval` with `approved_digest` | `check --release` stays failing until decisions 1 to 3 are recorded; any later change to a dependency, pin or notice voids the approval. |
| 6 | oauth-proxy transfer terms (Red Hat registry terms) | Maintainer, escalating to the platform team | Ship unmodified as an external dependency (recorded), or exclude | Excluding drops the image from the bundle. |
| 7 | Corpus rights | Corpus owner | Out of scope of this inventory | None; the corpus never enters a bundle. |

<a id="pymupdf"></a>
## PyMuPDF decision record

Status: **election recorded, legal confirmation pending.** The repository does not choose, purchase,
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

**Maintainer's decision (2026-10-23): A, scoped to ingest.** The election is
recorded (`elected: AGPL-3.0-only`). **Legal confirmation is pending** and recorded
as such: `pymupdf` stays `pending-owner-decision`, and `check --release` also refuses
any entry whose `decision.legal_confirmation` is `pending`, even after its status is
flipped, until the qualified legal owner confirms that handing the images to the enterprise plus a
source offer is acceptable (otherwise option B). Not a legal approval.

## What a release must satisfy

1. `python3 scripts/license_inventory.py check` passes (CI and `pack.sh` run it; a
   changed dependency, wheel hash, image digest, vendored asset, chart or notice
   file fails with fixed messages before any image is pulled). Optionally add
   `--wheelhouse DIR` / `--site-packages DIR` to re-derive license metadata from
   the actual wheelhouse and flag a license or license-file change.
2. `check --release` passes: no `pending-owner-decision` entry and a
   `release_approval` whose `approved_digest` equals the current inventory scope
   digest. Any later dependency, license or asset change makes the approval stale.
   `pack.sh` deliberately does not run `--release` (maintainer decision 2026-10-23),
   so a draft bundle can still be built. Running `check --release` on the packed
   commit is an explicit handoff step, recorded in the sign-off list of
   [release-record](release-record.md#sign-off-and-handoff).
3. The bundle carries `THIRD-PARTY-NOTICES.txt` (signed member in `SHA256SUMS`,
   required by `bootstrap.sh`, copied to `dist/`): the license/class/status of every
   component and wheel, the open owner decisions, the approval state, and the full
   texts of the project LICENSE, the Qdrant chart LICENSE, htmx, the Qdrant,
   Matt Pocock and pstack skills licenses and notices, and Task (`task-LICENSE`). It is
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
