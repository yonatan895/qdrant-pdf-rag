# ADR-0009: The air-gapped GitLab is a downstream fork; one pinned dev workspace image

- **Status:** proposed (tracker [GH#1118](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118),
  items C1 to C3; maintainer decisions D2 in
  [comment 6442866734](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6442866734)
  and D3/D4 in
  [comment 6443165098](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6443165098),
  2026-10-27). Replaces the AGENTS.md rule "Keep one public GitHub /
  air-gapped GitLab history". Accepting it changes no product default,
  identity or baseline; the tooling below lands in later issues.
- **Context:** releases reach the gap as signed bundles, and practically
  nothing leaves it. A single shared history cannot exist: commits made in the
  gap never return. Development continues in the gap with less capable models
  that have first-hand production context, no internet and no GitHub. The gap
  cannot build a unit-test environment from what is transferred today (the
  bundle has no dev wheels, Helm or Python 3.14), and review, acceptance and
  the Verifier exist only on GitHub. The gap's GitLab supports approval rules
  and CODEOWNERS.
- **Decision:** the air-gapped GitLab is a **downstream fork**. Its `main` is
  upstream release merges plus gap-local commits. Gap work lives in a
  reserved **site layer** so every release merges without conflicts. GitLab
  CODEOWNERS and approvals stand in for the Verifier. A separate signed **dev
  kit** carries one pinned dev workspace image that is both the GitLab runner
  image and the in-namespace dev pod.

## 1. One-way flow

- Upstream (GitHub `main`) is the only source of release commits. A release
  enters the gap only as the signed bundle the
  [release gate](../release-verification.md) accepted, and its `repo.bundle`
  HEAD is the release commit.
- Production runs only a pristine bootstrap workspace of a signed release.
  The existing checkout guard (`check_checkout_sha` in
  `scripts/airgap/common.sh`) refuses a HEAD other than the packed MANIFEST
  sha and any tracked change, so the fork's `main` is never an operator
  workspace. Operators copy site inputs into the release workspace as
  untracked files, as today ([bootstrap](../install_and_ops.md)).
- Nothing in this ADR moves code out of the gap. An upstream defect found in
  the gap leaves only as the organisation permits, at most as a description
  without site data. Until a release fixes it, it stays a gap patch (§2).

## 2. Site layer

`site/` is taken: it is the public static website. The site layer is:

| Path | Holds |
|---|---|
| `gap/README.md` | The gap entry point, read after AGENTS.md: the gap maintainer group, these rules, where site inputs live. Never named `AGENTS.md` (the context check would require it in the upstream chain table) |
| `gap/patches.md` | The ledger of gap patches: one `- path: <path>` line per upstream-owned path a gap patch changes, under a heading naming its `GL#` issue and retire condition |
| `gap/records/` | Dated site records, `YYYY-MM-DD-<slug>.md`: imports, site acceptance, incidents. Gap records never go in `docs/records/` |
| `gap/runbooks/` | Site procedures that upstream cannot know |
| `gap/config/` | Non-secret site inputs approved for git, such as an extra Helm values file named by `QDRANT_EXTRA_VALUES` (see open question 2) |
| `gap/dev/` | The dev pod manifest and site dev notes |
| `.gitlab/CODEOWNERS` | Generated (§4). GitHub ignores `.gitlab/` |
| `.gitlab/issue_templates/`, `.gitlab/merge_request_templates/` | Gap issue and MR forms |

Everything else is **upstream-owned**, including `AGENTS.md`, `docs/`
(ADRs and `docs/records/` too), `.agents/`, `.claude/`, `.github/`,
`.gitlab-ci.yml`, `airgap.env.example`, `charts/`, `scripts/`, `src/`,
`tests/`, `evals/`, `Taskfile.yml`, `taskfiles/`, the locks and pins, and
`site/`.

- Upstream never adds a path under `gap/` or `.gitlab/`. A GitHub-only
  hygiene step asserts `git ls-files gap .gitlab` is empty. It is not a
  pytest, because the fork has those paths.
- The site layer obeys every AGENTS.md hard boundary: no secrets, tokens,
  kubeconfigs, manual text, corpora, vectors or snapshots.
- A **gap patch** is a gap commit that changes an upstream-owned path. It
  needs an approved GitLab issue, is its own commit with a
  `Gap-Patch: GL#NNN` trailer, and is listed in `gap/patches.md` in the same
  MR. A Verifier-owned path in a gap patch takes its own commit, as on GitHub
  (D2). A gap patch never reaches production: production runs only release
  commits (§1), and builds in the gap still need an approved issue.

## 3. Release import procedure

Run it in the dev pod (§6), one release at a time, oldest first. Every
command must succeed. **Stop** means: if a merge is in progress run
`git merge --abort`, push nothing, and open a GitLab issue titled
`Release import <SHA> stopped at step N` with the failing command and its
first error line. Never retry with other flags. Never use `git rebase`,
`--force`, `-X ours`/`-X theirs` or `--squash`, and never edit an
upstream-owned path to make a check pass.

1. **Set the inputs.** `R` must not exist yet.
   ```sh
   export BUNDLE=/abs/path/qdrant-pdf-rag-<SHA>.tar
   export SNEAKERNET_TRUSTED_PUB=/abs/path/trusted-release-signing.pub
   export FORK=/abs/path/gitlab-clone
   export R=/abs/path/releases/<SHA>
   ```
2. **Verify the bundle.** `bootstrap.sh` checks the signature against the
   trust anchor, every member checksum, and that `repo.bundle` HEAD equals the
   MANIFEST sha. Any `FAIL` line: stop.
   ```sh
   cd "$(dirname "$BUNDLE")" && sha256sum -c "$(basename "$BUNDLE").sha256"
   mkdir "$R" && cd "$R" && tar -xf "$BUNDLE"
   AIRGAP_WORKSPACE=release sh bootstrap.sh
   SHA=$(sed -n 's/^sha: *//p' "$R/MANIFEST.txt")
   ```
3. **Prepare the fork.** `git status --porcelain` must print nothing.
   ```sh
   cd "$FORK" && git status --porcelain
   git fetch origin && git switch -c "import/$SHA" origin/main
   PREV=$(git log -1 --format=%B --grep='^Upstream-Release: ' origin/main | sed -n 's/^Upstream-Release: //p')
   ```
4. **Fetch and check ancestry.** The checks prove, in order: the fetched
   commit is the release; it is not imported yet; it descends from the last
   import. An empty `PREV` means the fork was never created (see below).
   ```sh
   git fetch "$R/repo.bundle" HEAD && test "$(git rev-parse FETCH_HEAD)" = "$SHA"
   ! git merge-base --is-ancestor "$SHA" HEAD
   test -n "$PREV" && git merge-base --is-ancestor "$PREV" "$SHA"
   ```
5. **Merge.**
   ```sh
   git merge --no-ff --no-commit FETCH_HEAD
   git diff --name-only --diff-filter=U
   ```
   `git merge` exits non-zero on a conflict; that is the one expected
   failure. No output from the second command: go on. A listed path under `gap/` or `.gitlab/`: keep the gap
   side with `git checkout --ours -- <path> && git add <path>`. Any other
   listed path: stop. Then:
   ```sh
   git commit -m "Import upstream release $SHA" -m "Upstream-Release: $SHA"
   ```
6. **Prove the upstream tree.** Outside the site layer the fork must equal
   the release except the ledger paths. `cmp` must print nothing.
   ```sh
   git diff --name-only "$SHA" HEAD -- . ':(exclude)gap' ':(exclude).gitlab' | sort > "$R/drift.txt"
   sed -n 's/^- path: //p' gap/patches.md | sort > "$R/patches.txt"
   cmp "$R/drift.txt" "$R/patches.txt"
   ```
7. **Site follow-ups,** each its own commit, site layer only. A dev kit
   refusal from `agent_doctor` names the kit to request: stop until it is
   installed. A CODEOWNERS drift: run the generator (§4) and commit its
   output. `git diff "$PREV" "$SHA" -- airgap.env.example` lists the key
   changes the operator applies to the untracked `airgap.env`; record them in
   `gap/records/`.
   ```sh
   python3 scripts/agent_doctor.py --profile unit
   git diff "$PREV" "$SHA" -- airgap.env.example
   ```
8. **Push and open a draft MR** named `Import upstream release <SHA>`, with
   `SHA`, `PREV`, the step 6 result and the step 7 commits in its body.
   ```sh
   git push origin "import/$SHA"
   ```
   The gap maintainer merges it with a merge commit once §4 holds. A squashed
   or rebased import loses upstream ancestry and makes every later import
   conflict.

The first import creates the fork: push the bootstrap clone's history to the
empty GitLab project, then commit `--allow-empty` with the
`Upstream-Release: <SHA>` trailer. When the implementing PR moves this
procedure into its owner document, this section becomes a link.

## 4. Governance in the gap

- **Settings** (the gap maintainer records them once in `gap/records/`):
  `main` is protected with no direct push; merge requires a successful
  pipeline and the approval rules below; the merge method is a merge commit
  and squashing is not allowed.
- **Protected paths.** Upstream adds a generator (proposed
  `scripts/gap_codeowners.py`) that writes `.gitlab/CODEOWNERS` from the
  protected-input list that `scripts/acceptance.py` and
  `scripts/unit_evidence.py` already define (`VERIFICATION_INPUTS`,
  `taskfiles/*.yml` and the producer workflows), plus `.gitlab-ci.yml`,
  `.gitlab/CODEOWNERS` and `gap/patches.md`. The owner group is a generator
  argument, so no site name enters upstream. Code-owner approval is required
  on `main`. That list stays the one source of truth: an upstream unit test
  asserts the generator's path set equals it, and the GitLab hygiene job
  fails when the committed file differs from the generator output. A release
  that changes the list therefore fails the import pipeline until step 7
  regenerates the file.
- **Required verification met** (gap) means all three: the latest pipeline on
  the MR head is green, with every job run and the clean-JUnit gate passing;
  the required approvals are present, including code-owner approval when a
  protected path changed; and the MR body lists each
  [live-stack](../live-stack.md#verification-minimums) row the diff selects,
  naming the GitLab job that produced it (`hygiene`, `lint`, `test`,
  `hazards`, `sim`, `gate-l1`), a dev pod command and exit code, or NOT RUN.
  A NOT RUN required row is not a pass, as in AGENTS.md.
- **acceptance.py and the Verifier** stay in the tree byte-identical and
  inert. They need `gh` and GitHub check-runs, and no gap job runs them.
  Their protected list is reused as data by the generator. The code-owner
  approval is the gap's Verifier decision, and a human in the gap maintainer
  group gives it. In the gap, read AGENTS.md "PR" as MR and "yonatan895" as
  the gap maintainer group. Agents open draft MRs, are not code owners, and
  never approve, mark ready or merge.

## 5. Issue references

GitHub issues are public. The dev kit carries an offline snapshot of them
(§6). Text written after this ADR, upstream and in the site layer, writes
`GH#NNN` for a GitHub issue and `GL#NNN` for a gap GitLab issue. A bare
`#NNN` in a committed file predates this ADR and means GitHub; it is not
rewritten, which would conflict on import. In GitLab issue and MR text, a
bare `#NNN` is GitLab's own.

## 6. Dev workspace image and dev kit (D3)

- **One image** serves as both the GitLab runner image and the dev pod in
  the project namespace. It is UBI-based and holds the CPython 3.14 GIL
  build, the pinned Task, Helm 4, the hash-locked dev wheels from
  `requirements.dev.lock.txt`, and the tools the procedure in §3 runs (git,
  openssl, sha256sum, tar). It runs under `restricted-v2` with
  an arbitrary non-root UID. The checkout's own source is used in place, so
  the gap installs and builds nothing.
- **Keyed by the dev-lock digest:** one SHA-256 over a fixed, ordered list
  of pin files that upstream owns and protects (proposed:
  `requirements.dev.lock.txt`, `locks/cp314-linux-x86_64.json`,
  `scripts/tools/task-pin.txt`, `scripts/tools/helm-pin.txt` and the image
  recipe). The image is pinned by manifest digest. `.gitlab-ci.yml`'s default
  `CI_PYTHON_IMAGE` names that digest, a unit test asserts it matches the
  kit pin, and the gap leaves the project-level variable unset, so each
  branch's pipeline uses the kit for its own lock.
- **Built connected-side only.** The connected factory builds the image, as
  it builds product images. The gap only verifies the kit and copies the
  OCI archive into the site registry. This fits the AGENTS.md rule "No
  builds in the gap without an approved issue": no build happens in the gap.
- **The kit** is a signed tarball, separate from the release bundle:
  `mfr-devkit-<key>.tar` holding the image as an `oci-archive`,
  `MANIFEST.txt` (key, image digest, the pin-file digests, the upstream
  commit built from, the snapshot time), `sbom.json`, third-party notices and
  the [licence](../licensing.md) check, `SHA256SUMS` with its signature, a
  verify/install script and the GitHub issue snapshot. A release ships a new
  kit only when its key changes, and its MANIFEST names the key it needs.
- **`agent_doctor` refuses a kit/lock mismatch** with a fixed message that
  names the kit to request (`mfr-devkit-<key>`). Outside a kit image,
  connected development keeps today's checks.

## Considered options

- **Keep one shared history.** Impossible while nothing leaves the gap.
- **Rebase gap commits onto each release.** This rewrites the published
  `main` and needs force-pushes.
- **Import releases as unrelated snapshot commits.** Each import becomes a
  whole-tree diff, and ancestry and blame are lost.
- **Dev kit as new release-bundle members** (D3 option A). Every release
  would carry the toolchain over the slow transfer path, even when the lock
  is unchanged.
- **Build the dev environment in the gap from wheels.** This needs an
  interpreter the gap lacks and conflicts with the no-builds rule.

## Consequences

- **AGENTS.md.** The rule becomes:

  ```text
  - Air-gapped GitLab is a [downstream fork](docs/adr/0009-gap-fork.md); never
    push/force-push `main` or change repository access. Draft PRs only. Only
    yonatan895 marks ready, requests changes or merges; agents never act for him.
    [Branches](docs/agent-workflow.md#git-workflow).
  ```

  It is byte-negative and lands with this record. The MR and gap-maintainer
  reading lives in §4, because the root has no bytes for it.
- **Trade-off.** Gap development gets first-hand production context, but
  weaker models do it, and its learning does not flow back upstream. Gap
  patches cost a stop on every import that touches them, until a release
  retires them.
- **Upstream keeps the extension points stable:** the reserved `gap/` and
  `.gitlab/` paths; the `airgap.env` keys (a rename or retirement keeps the
  fixed refusal naming its replacement); the protected-input list and the
  generator's command line; the kit key inputs and the `agent_doctor`
  message; the GitLab CI/CD variable names; the `Upstream-Release:` trailer
  and the import procedure. A release that changes any of these says so in
  its release record, with the site-layer action.
- **Owner documents** change in the implementing PRs:
  [deployment policy](../deploy.md#deployment-policy) and
  [CI policy](../deploy.md#ci-policy) (the CI job image becomes this
  repository's dev kit image), the live-stack GitLab job map, the
  [task-runner](../task-runner.md#installation) gap workstation recipe and
  the bootstrap guide (§3 moves there).
- **Prerequisites:** "pipeline green" means nothing until the GitLab
  pipeline runs on a real runner
  ([GH#446](https://github.com/yonatan895/qdrant-pdf-rag/issues/446)) and
  GH#1118 A5 (a unit test that skips in GitLab) and C2 are fixed.
- **Verifier-owned touches** in the implementation: `scripts/agent_doctor.py`
  and any workflow or taskfile the kit build adds.

## Open questions for the maintainer

1. **What gap code changes are for.** This draft keeps gap patches dev-side:
   production runs only release commits, and the checkout guard refuses the
   fork's HEAD. Should a product fix ever ship from the gap? That would need
   an approved in-gap build and its own ADR.
2. **Site configuration in git.** May `gap/config/` hold the site's
   non-secret `airgap.env` and internal names? AGENTS.md forbids committing
   private configuration, so this draft says no: the file stays untracked
   and operator-owned.
3. **Kit key inputs.** Only the dev lock, as decided, or also the Task and
   Helm pins and the image recipe, as proposed, so that a tool pin bump also
   yields a new kit?
4. **Kit signing.** The release-signing key, or a separate dev-kit key?
5. **Approval scope.** Only the protected list (as decided), or also a
   catch-all owner for every upstream-owned path, so that every gap patch
   needs the gap maintainer's approval? And which group is the gap
   maintainer?
6. **`GL#NNN`** for GitLab issues in committed files: wanted?
7. **Rows with no GitLab producer** (HTTP probes, browser, HA, load,
   packaging, semantic eval): which, if any, the dev pod may run, given that
   it has no container runtime.
8. **Directory name** `gap/`.
9. **Snapshot refresh:** with every kit, or also on a schedule?
