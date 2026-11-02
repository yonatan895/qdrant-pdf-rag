# Release notes

Owner: this file announces changes to the upstream/gap extension surface
([ADR-0009 §9](adr/0009-gap-fork.md#9-extension-surface-open-question-2-decided)).
A gap import diffs it between two releases. Append one section per change,
newest last; never rewrite a published entry.

```text
## YYYY-MM-DD GH#NNN <title>

<What changed and what the operator or gap maintainer does.>

- removed `<category>:<item>`; replaced by `<new>`; migration: <kind> in `<path>`
```

- One `- removed` line for each item that leaves the surface, including a
  task or flag kept only as a refusing stub. Omit `; replaced by ...` only
  for a retired key that nothing replaces.
- `<category>` is a key of `tests/fixtures/extension-surface.json`, and
  `<item>` is exactly as it appears there.
- `<kind>` and `<path>` name the migration in the category's owner file:
  - `retired-key` in `scripts/airgap/model_config.py`: a `RETIRED_KEYS`
    entry whose message names the new key;
  - `refusal` in the taskfile or script: the stub that refuses the old task
    or flag with a fixed message naming the new one;
  - `refusal` in another owner: a fixed refusal line naming both names
    (see ADR-0009 §9).
- `tests/test_extension_surface.py` checks every line against the code. An
  added item needs no entry.

## Entries

## 2026-10-30 GH#1118 Deploy refuses an unverified checkout

Outside dry-run, `airgap:load`, `airgap:deploy`, `airgap:ingest` and
`airgap:validate` (and so `airgap:pipeline`) now run only from a verified
signed release: the checkout `bootstrap.sh` made from the signed bundle, with
`SNEAKERNET_TRUSTED_PUB` set to the release signing public key obtained out of
band. A plain clone, a missing anchor, a signature the anchor does not verify,
HEAD other than the MANIFEST sha, or a tracked or hidden change is refused
with a fixed message. Dry-run is unchanged. Operators export
`SNEAKERNET_TRUSTED_PUB` for every stage, not only `bootstrap.sh`. Connected
developers and the connected CI and Kind lanes that run these stages from a
plain checkout set `AIRGAP_UNVERIFIED_CHECKOUT=1` together with
`CI_REHEARSAL=1` in the process environment (never in `airgap.env`, never in
the air gap). See [deploy §4](deploy.md#4-signing-and-provenance).

## 2026-10-31 GH#1118 Retire-legacy refuses an unverified checkout

Outside dry-run, `airgap:retire-legacy` (`scripts/airgap/retire_legacy.sh`,
which uses the Qdrant writer key) now runs the same verified-release guard as
load, deploy, ingest and validate, before any cluster or Qdrant contact. Run
it from the checkout `bootstrap.sh` made from the signed bundle, with
`SNEAKERNET_TRUSTED_PUB` exported. The connected opt-out is unchanged:
`AIRGAP_UNVERIFIED_CHECKOUT=1` only together with `CI_REHEARSAL=1` in the
process environment, never in the air gap.

## 2026-10-31 GH#1118 GitLab CI runs on the dev kit image

The default `CI_PYTHON_IMAGE` in `.gitlab-ci.yml` is now the dev workspace
image, `$CI_APPROVED_REGISTRY/qdrant-pdf-rag-devkit` by the digest in
`scripts/tools/devkit-pin.txt` (ADR-0009 §8). Install the kit image there
(`scripts/devkit/install.sh --registry <CI_APPROVED_REGISTRY>/qdrant-pdf-rag-devkit`)
and leave the project variable `CI_PYTHON_IMAGE` unset; a Python job (lint,
test, hazards, sim, gate-l1) on any other image fails with a fixed message. Jobs use the image's installed dev
environment and its Task and Helm, so these GitLab CI/CD variables are retired:

- `CI_HELM_ARCHIVE`: the `hygiene` job refuses it with a fixed message naming
  `scripts/devkit/link-tools.sh`. Delete the project variable. To rerun a
  pipeline for a ref older than this change, pass it as a variable of that run
  only.
- `PIP_FIND_LINKS` and `PIP_INDEX_URL`: no longer read, and not refused, since
  pip itself honours them and a platform may set them for other projects. No
  GitLab job installs a package any more.

`CI_TASK_ARCHIVE` is unchanged and still required for `test`, `sim` and
`hazards`: the image holds the Task binary only, and the artifact tests and the
doctor read the archive. These variables are not items of the derived
extension surface, so this entry has no `- removed` line.

## 2026-10-31 GH#1174 Offline reference docs in the release bundle

The bundle gains the required signed member `reference-docs.tar.gz` and three
MANIFEST fields (`reference_docs`, `reference_docs_sha256`,
`reference_docs_sets`). `bootstrap.sh` verifies it and unpacks it to
`dist/reference-docs/` (start at `INDEX.md`): this repository's docs at the
release commit and 38 version-matched upstream doc sets. Nothing to do for the
gap maintainer; `dist/reference-docs/` is untracked, like the other members.
