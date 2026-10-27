# ADR-0009: The air-gapped GitLab is a downstream fork; one pinned dev workspace image

- **Status:** proposed (tracker [GH#1118](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118),
  items C1 to C3). Maintainer decisions:
  - D2 in [comment 6442866734](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6442866734);
  - D3 and D4 in [comment 6443165098](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6443165098);
  - Q1, Q2 and Q5, plus coordinator defaults Q3, Q4 and Q6 to Q9, in [GH#1120 comment 6443697962](https://github.com/yonatan895/qdrant-pdf-rag/pull/1120#issuecomment-6443697962);
  - "the air-gapped environment is production" (maintainer clarification, 2026-10-27);
  - the namespace answer (open question 1) in [comment 6451566378](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6451566378);
  - the extension-surface decision (open question 2) in [comment 6451605866](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6451605866).

  This record replaces the AGENTS.md rule "Keep one public GitHub /
  air-gapped GitLab history". Accepting it changes no product default,
  identity or baseline. The enforcement and tooling it commits to are
  follow-up items for GH#1118 (listed at the end) and do not exist yet.
- **Context:**
  - Releases reach the gap as signed bundles, and practically nothing leaves
    it. A single shared history cannot exist, because commits made in the gap
    never return.
  - The air-gapped environment **is** production. Dev pods, GitLab runners
    and gap builds run alongside the live service, today in the same
    non-production namespace (§2).
  - Development continues there with less capable models, which have
    first-hand production context but no internet and no GitHub.
  - The gap cannot build a unit-test environment from what is transferred
    today: the bundle has no dev wheels, no Helm and no Python 3.14.
  - Review, acceptance and the Verifier exist only on GitHub.
  - The gap's GitLab supports approval rules and CODEOWNERS.
- **Decision:**
  - The air-gapped GitLab is a **downstream fork**. Its `main` is upstream
    release merges plus gap-local commits.
  - Gap work lives in a reserved **site layer** so that every release merges
    cleanly.
  - Gap code reaches production only as a **signed gap release**. Production
    deploys only signed releases, never an unsigned checkout.
  - Development, CI and rehearsal are **isolated from the live release**.
  - GitLab CODEOWNERS and approvals stand in for the Verifier.
  - A separate signed **dev kit** carries one pinned dev workspace image. It
    serves as both the GitLab runner image and the dev pod.

## 1. One-way flow and release trust

- **Release sources.** Upstream (GitHub `main`) is the only source of
  upstream release commits. They enter the gap only as the signed bundle that
  the [release gate](../release-verification.md) accepted, and the bundle's
  `repo.bundle` HEAD is the release commit. A **gap release** (§5) is the
  only other source of production code.
- **Current behavior** of `check_checkout_sha` in `scripts/airgap/common.sh`,
  which load, deploy, ingest and validate call:
  - With a packed MANIFEST, it verifies the MANIFEST chain and requires HEAD
    to equal the MANIFEST sha with no tracked or hidden changes.
  - Under `AIRGAP_DRYRUN=1`, or when there is no MANIFEST and no bundle
    evidence, it prints `Notice: ... not release-verified` and returns 0.
  - The trust anchor `SNEAKERNET_TRUSTED_PUB` is optional; without it the
    signature check is trust on first use.
  - So today a plain clone of the fork passes with a Notice, and only
    convention keeps gap code out of production.
- **Decision (follow-up F1):** outside dry-run, load, deploy, ingest and
  validate refuse, with a fixed message, any checkout that is not a verified
  signed release. A verified signed release means three things:
  - the MANIFEST's `SHA256SUMS` signature verifies against a configured
    trust anchor, either the upstream release key or the gap release key;
  - HEAD equals the MANIFEST sha;
  - there is no tracked or hidden change.
- **The dev-only opt-out** is one explicit variable (proposed
  `AIRGAP_UNVERIFIED_CHECKOUT=1`). It is accepted only together with the
  existing `CI_REHEARSAL=1` marker, which already means "only in a
  disposable lab, never in the air gap". Only connected-side developers and
  the connected CI and Kind lanes may use it. Nobody in the gap uses it, and
  gap rehearsals use signed gap releases instead (§5).
- **Credentials.** The dev pod and the GitLab runner hold no production
  writer credentials and no write RBAC (§2). So even an unverified checkout
  in the gap cannot change the live release.
- **Outbound.** Nothing in this ADR moves code out of the gap. An upstream
  defect found there leaves only as the organisation permits, at most as a
  description without site data.

## 2. Isolation of development from the live release

- **Namespaces (open question 1, answered).** Today everything, the live
  service included, runs in one non-production namespace. Once it has been
  stable for a period, the service moves to the production cluster with a
  production IdP client.
  - The move is a planned fresh install, not an in-place upgrade: the
    identical signed release is promoted there (§5 step 6) with a new IdP
    client.
  - After the move, the current namespace stays as the dev, CI and
    gap-release rehearsal namespace, the preferred isolation.
  - Until then, the shared-namespace rules below apply.
- **In a shared namespace** (follow-up F9 records the proof in
  `gap/records/`):
  - The dev pod, the MR runner and the release-build job each use their own
    ServiceAccount.
  - None of them can read the production Secrets: the Qdrant writer and
    admin keys, the gateway keys, the OIDC client secret and the gap signing
    key. Only the protected-tag signing job (§5) reads the signing key.
  - None of them has write RBAC on production Deployments, StatefulSets,
    PVCs or any other production workload object.
  - A NetworkPolicy keeps dev and CI pods off production Qdrant and the
    agent. Their labels never match a production selector.
  - The proof is the ServiceAccounts' `can-i` lists (verbs and resources
    only) and the policies.
- **Test data.** Tests and development never write to the production
  collection or alias. Dev and CI use a separate Qdrant instance, either the
  GitLab `sim` service or a non-production release with its own PVCs. The
  chart fixes the logical collection name, so a separate collection in the
  production Qdrant is not an option without its own approved concern.
- **Storage.** The import procedure (§4) needs room for about three times
  the release tarball, on a dev PVC. Remove `$R` after the import MR
  merges.

## 3. Site layer

`site/` is taken: it is the public static website. The site layer is:

| Path | Holds |
|---|---|
| `gap/README.md` | The gap entry point, read after AGENTS.md. It names the gap maintainer group and the named approvers, restates these rules and says where site inputs live. Never name it `AGENTS.md`: the context check would require it in the upstream chain table |
| `gap/patches.md` | The gap-patch ledger. A `# Gap patches` title line, then one section per patch: a heading `## GL#NNN <full commit sha>`, then one `- path: <path>` line per upstream-owned path the patch changes. Other lines are free text. Sections are in application order: a new patch is appended at the end |
| `gap/config/` | The site's non-secret configuration (Q2), such as its `airgap.env` without secret values. Secrets stay in Kubernetes Secrets or the site's secret store and are referenced by name only. Rehearsal-only overrides (`CI_REHEARSAL` keys) never go here |
| `gap/records/` | Dated site records, `YYYY-MM-DD-<slug>.md`: imports, gap releases, go-live checks, isolation proofs, incidents. Gap records never go in `docs/records/` |
| `gap/runbooks/` | Site procedures that upstream cannot know |
| `gap/dev/` | The dev pod manifest and site dev notes |
| `.gitlab/CODEOWNERS` | Generated (§6). GitHub ignores `.gitlab/` |
| `.gitlab/issue_templates/`, `.gitlab/merge_request_templates/` | Gap issue and MR forms |

Everything else is **upstream-owned**: `AGENTS.md`, `docs/` (including the
ADRs and `docs/records/`), `.agents/`, `.claude/`, `.github/`,
`.gitlab-ci.yml`, `airgap.env.example`, `charts/`, `scripts/`, `src/`,
`tests/`, `evals/`, `Taskfile.yml`, `taskfiles/`, the locks and pins, and
`site/`.

- **Upstream never tracks `gap/` or `.gitlab/`.** F3 lands with this
  record: a check in `tests/test_hygiene.py` fails on GitHub Actions
  (`GITHUB_ACTIONS=true`) when either path exists. It asserts nothing
  elsewhere, because the fork does track those paths and never runs GitHub
  Actions. Step 3 of the import also refuses a release that tracks them.
- **Hard boundaries.** Apart from Q2, the site layer obeys every AGENTS.md
  hard boundary: no secrets, tokens, kubeconfigs, manual text, corpora,
  vectors or snapshots.
- **A gap patch** is one gap commit that changes upstream-owned paths. It
  needs an approved GitLab issue and carries a `Gap-Patch: GL#NNN` trailer.
  Its ledger section lands in the same MR. A Verifier-owned path takes its
  own commit, as on GitHub (D2). A gap patch reaches production only inside
  a signed gap release (§5).

## 4. Release import procedure

The gap maintainer creates the fork once:
1. Bootstrap the first upstream release as in step 1 of the script below.
2. Push its history to the empty GitLab project.
3. Commit `gap/README.md` and a `gap/patches.md` that holds only its title
   line, with the trailer `Upstream-Release: <SHA>`.
4. Then protect `main` (§6).

Agents never push `main`.

**Every later import.** Export the five inputs. Save the script below as
`$HOME/import-release.sh` and run `sh -eu "$HOME/import-release.sh"`. Until
follow-up F7 ships it as `scripts/gap/import_release.sh`, this block is the
script.

- **Success:** it ends with `IMPORT READY <SHA>`. Then push the branch and
  open the MR (below).
- **Failure:** the script stops at a `STOP:` line or at a failing command.
  Open or update a GitLab issue titled `Release import <SHA> stopped`, with
  the last 20 lines of output.
- **Rerun:** after any stop, rerun the same command. The script resets its
  own state first: it aborts an unfinished merge, revert or cherry-pick,
  deletes the local `import/<SHA>` branch and removes `$R`.
- **Never** rerun after the branch has been pushed. Fix forward with new
  commits on the MR branch instead. Never use `git rebase`, `--force`,
  `-X ours`, `-X theirs` or `--squash`, and never edit an upstream-owned path
  to make a check pass.

```sh
export SHA=<40-hex sha from the tarball name qdrant-pdf-rag-<SHA>.tar>
export BUNDLE=/abs/path/qdrant-pdf-rag-$SHA.tar
export SNEAKERNET_TRUSTED_PUB=/abs/path/trusted-release-signing.pub
export FORK=/abs/path/gitlab-clone
export R=/abs/path/releases/$SHA
```

<!-- import-script:start -->
```sh
# Any failure, including one with no STOP line of its own, ends with a STOP line.
trap 'rc=$?; [ "$rc" -eq 0 ] || echo "STOP: the import failed (exit $rc); rerun to reset, then report if it stops again"' EXIT

# 0. Reset: safe on every run, and the way to resume after any stop.
cd "$FORK"
if git rev-parse -q --verify MERGE_HEAD >/dev/null; then git merge --abort; fi
if git rev-parse -q --verify REVERT_HEAD >/dev/null; then git revert --abort; fi
if git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null; then git cherry-pick --abort; fi
if [ -n "$(git status --porcelain)" ]; then echo "STOP: $FORK has local changes; ask the gap maintainer"; false; fi
git fetch origin
git switch --detach origin/main
if git rev-parse -q --verify "refs/heads/import/$SHA" >/dev/null; then git branch -D "import/$SHA"; fi
rm -rf "$R"

# 1. Verify the bundle: signature against the trust anchor, every member
#    checksum, and repo.bundle HEAD equal to the MANIFEST sha.
cd "$(dirname "$BUNDLE")"
sha256sum -c "$(basename "$BUNDLE").sha256"
mkdir -p "$R"
cd "$R"
tar -xf "$BUNDLE"
AIRGAP_WORKSPACE=release sh bootstrap.sh
if [ "$(sed -n 's/^sha: *//p' "$R/MANIFEST.txt")" != "$SHA" ]; then echo "STOP: MANIFEST sha is not $SHA"; false; fi

# 2. Branch from the fork's main and find the last imported release.
cd "$FORK"
git switch -c "import/$SHA" origin/main
PREV=$(git log -1 --format=%B --grep='^Upstream-Release: ' HEAD | sed -n 's/^Upstream-Release: //p')
if [ -z "$PREV" ]; then echo "STOP: no Upstream-Release trailer; the fork was never created"; false; fi

# 3. Fetch the release and refuse what the procedure cannot handle.
git fetch "$R/repo.bundle" HEAD
if [ "$(git rev-parse FETCH_HEAD)" != "$SHA" ]; then echo "STOP: bundle HEAD is not $SHA"; false; fi
if git merge-base --is-ancestor "$SHA" HEAD; then echo "STOP: $SHA is already imported"; false; fi
if ! git merge-base --is-ancestor "$PREV" "$SHA"; then echo "STOP: $SHA does not descend from the last import $PREV"; false; fi
if [ -n "$(git ls-tree -r --name-only "$SHA" -- gap .gitlab)" ]; then echo "STOP: the release tracks gap/ or .gitlab/"; false; fi

# 4. Set aside every ledgered gap patch: revert them newest first (the
#    ledger lists them in application order), then empty the ledger.
: > "$R/all.txt"
if [ -f gap/patches.md ]; then
  cp gap/patches.md "$R/patches.before"
  awk '/^## GL#/ {print $2, $3}' gap/patches.md > "$R/all.txt"
fi
for c in $(tac "$R/all.txt" | awk '{print $2}'); do
  if ! git revert --no-edit "$c"; then echo "STOP: cannot set aside gap patch $c; an unledgered change overlaps it"; false; fi
done
if [ -s "$R/all.txt" ]; then
  awk '/^## / {exit} {print}' "$R/patches.before" > gap/patches.md
  git commit -am "Set aside gap patches before importing $SHA"
fi

# 5. Merge. With every gap patch set aside, any conflict is unexpected.
if ! git merge --no-ff --no-commit "$SHA"; then
  echo "STOP: conflict in: $(git diff --name-only --diff-filter=U | tr '\n' ' ')"; false
fi
git commit -m "Import upstream release $SHA" -m "Upstream-Release: $SHA"

# 6. Re-apply every patch in ledger order. One that conflicts or is now
#    empty stays set aside; its GL# issue gets a note (see below).
: > "$R/reapplied.txt"
for c in $(awk '{print $2}' "$R/all.txt"); do
  i=$(awk -v c="$c" '$2 == c {print $1}' "$R/all.txt")
  if git cherry-pick "$c"; then
    echo "reapplied $i $c $(git rev-parse HEAD)" >> "$R/reapplied.txt"
  else
    git cherry-pick --abort
    echo "set-aside $i $c" >> "$R/reapplied.txt"
  fi
done
if grep -q '^reapplied ' "$R/reapplied.txt"; then
  awk 'FILENAME==ARGV[1] {if ($1 == "reapplied") new[$3]=$4; next}
       /^## / {on=($3 in new); if (on) print $1, $2, new[$3]; next}
       on' "$R/reapplied.txt" "$R/patches.before" >> gap/patches.md
  git commit -am "Re-apply gap patches after importing $SHA"
fi

# 7. Prove the upstream tree: outside the site layer the fork equals the
#    release except the paths the ledger lists.
git diff --name-only "$SHA" HEAD -- . ':(exclude)gap' ':(exclude).gitlab' | sort -u > "$R/drift.txt"
if [ -f gap/patches.md ]; then sed -n 's/^- path: //p' gap/patches.md; fi | sort -u > "$R/patches.txt"
if ! cmp -s "$R/drift.txt" "$R/patches.txt"; then
  echo "STOP: upstream-owned paths differ from the ledger:"; diff "$R/patches.txt" "$R/drift.txt" || true; false
fi
git diff "$PREV" "$SHA" -- airgap.env.example > "$R/env-keys.diff"
git diff "$PREV" "$SHA" -- docs/release-notes.md > "$R/release-notes.diff"
echo "IMPORT READY $SHA"
```
<!-- import-script:end -->

After `IMPORT READY`:

1. Until follow-ups F4 and F5 land, run these by hand.
   - **Dev kit.** If `agent_doctor` refuses the dev kit, it names the kit to
     request. Stop until that kit is installed.
   - **CODEOWNERS.** If the file has drifted, run the generator and commit
     its output.
   - **Keys.** `$R/env-keys.diff` lists the key changes. Apply them to
     `gap/config/` in their own commit.
   - **Extension surface.** `$R/release-notes.diff` lists each removed or
     renamed name and its migration (§9). Update every gap-owned use of it.
   - **Set-aside patches.** For each `set-aside GL#NNN <sha>` line in
     `$R/reapplied.txt`, reopen or comment on that GitLab issue: "not
     re-applied on upstream release <SHA>; rework or retire". A rework is a
     new gap patch.
2. Push the branch with `git push origin "import/$SHA"`.
3. Open a draft MR named `Import upstream release <SHA>`. Its body gives
   `SHA`, `PREV`, `$R/reapplied.txt` and the follow-up commits.
4. Ordinary approvals and the second approver (§6) apply. The gap maintainer
   merges it with a merge commit. A squashed or rebased import loses
   upstream ancestry and makes every later import conflict.

## 5. Gap releases

Gap-developed code reaches production only as a signed gap release
(follow-up F6). AGENTS.md "No builds in the gap without an approved issue"
stays, and it is the gate.

1. **Authorise.** An approved GitLab issue names the fork commit to
   release. The gap maintainer creates a protected tag on it. Only
   protected-tag pipelines can build or sign.
2. **Build.** The release-build job builds the agent and ingest images
   unprivileged (`restricted-v2`, no `anyuid`), isolated as in §2. Every
   input is already in the gap:
   - base images by digest;
   - the hash-locked runtime and build wheels (carried by the dev kit, §8);
   - the BM25 weights from the upstream bundle.

   Nothing is downloaded.
3. **Pack and sign.** The bundle has the upstream layout, so the same
   bootstrap, load and deploy path runs it:
   - `repo.bundle` of the fork;
   - the image archives;
   - `MANIFEST.txt`, with `origin: gap`, the upstream release it is based on
     and the `GL#` issue;
   - `PACKING_RECORD.txt`, `sbom.json`, the third-party notices and the
     [licence](../licensing.md) check;
   - `SHA256SUMS` and its signature.

   The **gap release key** signs it:
   - The gap maintainer generates the key in the gap and holds it in the
     site's secret store.
   - It never leaves the gap and is a separate trust anchor.
   - Its public key reaches operators inside the site, and its fingerprint
     is recorded in `gap/records/`.
4. **Record.** A release record goes in `gap/records/`, shaped like
   [the release record](../release-record.md). Each release-gate check is
   marked run with its evidence, or NOT RUN. A NOT RUN required check blocks
   promotion, as AGENTS.md requires.
5. **Rehearse.** Bootstrap the tarball into a fresh workspace. Deploy it
   either to the non-production namespace or to a non-production release
   name in the shared namespace, with its own Qdrant instance and PVCs and
   never the production collection or alias. Pass the
   [go-live checks](../install_and_ops.md#site-openshift-acceptance) there
   and record them.
6. **Promote.** Bootstrap the identical rehearsed tarball (no repack) into a
   fresh operator workspace and deploy it to the production release. It is
   verified against the gap anchor.
7. **Roll back** to the previous signed release, upstream or gap. Deploy it
   from its own retained bootstrap workspace and bundle, as the
   [bootstrap guide](../install_and_ops.md) requires. Data follows the
   [upgrade and recovery runbook](../install_and_ops.md#upgrade-and-recovery-runbook-issue-272).
   Keep the previous release's tarball and workspace until the new one has
   passed its go-live checks.

An upstream release may take the same rehearse-then-promote path. Later
upstream imports merge into a fork `main` that contains gap releases. Their
gap patches stay ledgered.

## 6. Governance in the gap

- **Settings.** The gap maintainer records these once in `gap/records/`:
  - `main` is protected, with no direct push;
  - a merge needs a successful pipeline and the approvals below;
  - the merge method is a merge commit, and squashing is not allowed.
- **CODEOWNERS (Q5).** A generator (follow-up F4, proposed
  `scripts/gap_codeowners.py`) writes `.gitlab/CODEOWNERS`. It takes the
  owner group and the named approvers as arguments, so no site name enters
  upstream.
  - **Every path except `gap/`** is owned by the gap maintainer group, and
    code-owner approval is required on `main`. F4 confirms which
    CODEOWNERS form leaves `gap/` without an owner on the site's GitLab.
  - **The protected list** also needs a **second named approver**. It is the
    list that `scripts/acceptance.py` and `scripts/unit_evidence.py` already
    define (`VERIFICATION_INPUTS`, `taskfiles/*.yml` and the producer
    workflows), plus the gap's own gates: `.gitlab-ci.yml`,
    `.gitlab/CODEOWNERS`, `scripts/check_junit_clean.py`,
    `scripts/gate_l1.py`, the dev kit pin, and the gap release pipeline and
    signing files.
  - **One source of truth.** An upstream unit test asserts that the
    generator's protected set equals those lists plus the named gap
    additions. The GitLab hygiene job fails when the committed file differs
    from the generator output.
- **Required verification met** (in the gap) means all of the following:
  - The latest pipeline on the MR head is green, every job ran, and the
    clean-JUnit gate passes.
  - The required approvals are present.
  - The MR body lists each [live-stack](../live-stack.md#verification-minimums)
    row the diff selects, with the GitLab job that produced it (`hygiene`,
    `lint`, `test`, `hazards`, `sim` or `gate-l1`) or NOT RUN.
  - Rows with no GitLab producer (HTTP probes, browser, HA, load, packaging,
    semantic eval) are NOT RUN in development for now (Q7). A change that
    selects them is verified only through a gap-release rehearsal (§5). A
    NOT RUN required row is not a pass.
- **acceptance.py and the Verifier** stay in the tree byte-identical and
  inert. They need `gh` and GitHub check-runs, and no gap job runs them.
  Their protected list is reused as data. The second named approval is the
  gap's Verifier decision.
- **Reading AGENTS.md in the gap:** "PR" means MR, and "yonatan895" means
  the gap maintainer group. Agents open draft MRs, are not code owners or
  named approvers, and never approve, mark ready or merge.

## 7. Issue references

GitHub issues are public. The dev kit carries an offline snapshot of them,
refreshed with each kit (Q9).

- Text written after this ADR, upstream and in the site layer, writes
  `GH#NNN` for a GitHub issue and `GL#NNN` for a gap GitLab issue (Q6).
- A bare `#NNN` in a committed file predates this ADR and means GitHub. It
  is not rewritten, because a rewrite would conflict on import.
- In GitLab issue and MR text, a bare `#NNN` is GitLab's own.

## 8. Dev workspace image and dev kit (D3)

- **One image** serves as both the GitLab runner image and the dev pod. It
  is UBI-based and holds:
  - the CPython 3.14 GIL build, the pinned Task and Helm 4;
  - the hash-locked dev wheels from `requirements.dev.lock.txt`;
  - the tools §4 runs: git, openssl, sha256sum, tar and awk.

  It runs under `restricted-v2` with an arbitrary non-root UID. The
  checkout's own source is used in place, so development installs and
  builds nothing.
- **Kit key (Q3).** One SHA-256 over a fixed, ordered list of pin files that
  upstream owns and protects: `requirements.dev.lock.txt`,
  `locks/cp314-linux-x86_64.json`, `scripts/tools/task-pin.txt`,
  `scripts/tools/helm-pin.txt` and the image recipe.
  - The image is pinned by manifest digest.
  - The default `CI_PYTHON_IMAGE` in `.gitlab-ci.yml` names that digest, and
    a unit test asserts it matches the kit pin.
  - The gap leaves the project-level variable unset, so each branch's
    pipeline uses the kit for its own lock.
- **Built connected-side only.** The connected factory builds the image, as
  it builds product images. The gap only verifies the kit and copies the
  OCI archive into the site registry. Gap releases (§5) are the only builds
  in the gap, and each one needs an approved issue.
- **The kit** is a signed tarball, separate from the release bundle,
  `mfr-devkit-<key>.tar`. It holds:
  - the image as an `oci-archive`;
  - `MANIFEST.txt`, recording the key, the image digest, the pin-file
    digests, the upstream commit it was built from and the snapshot time;
  - `sbom.json`, the third-party notices and the licence check;
  - `SHA256SUMS`, signed with the connected release key (Q4);
  - a verify/install script;
  - the GitHub issue snapshot;
  - the gap-release build inputs from §5.

  A release ships a new kit only when its key changes, and the release
  MANIFEST names the key it needs.
- **`agent_doctor` refuses a kit/lock mismatch** with a fixed message that
  names the kit to request (`mfr-devkit-<key>`). Outside a kit image,
  connected development keeps today's checks.

## 9. Extension surface (open question 2, decided)

The names the gap builds on are a **public API**, limited to this list:

| Category | Derived from |
|---|---|
| `airgap.env` keys | Every key `airgap.env.example` assigns or shows commented out, plus `RETIRED_KEYS` |
| Chart values that site config sets | The value paths `map_values.py` writes when every optional block is selected, plus the Qdrant paths `deploy.sh` sets |
| Task names and operator script flags | Every task `task --list` shows (all tasks are in scope); the flags each `airgap:` task's script parses; the §4 import script's inputs |
| `gap/` paths | The §3 table |
| Release bundle layout | The tarball, member and MANIFEST field names `pack.sh` writes, which include everything the §4 import script reads |

- **Change rule.** Removing or renaming an item ships only with both:
  - a migration path: the old name keeps working, or it is refused with a
    fixed message naming the new one (the `RETIRED_KEYS` pattern of GH#1111);
  - a `- removed` line in [the release notes](../release-notes.md).

  A task or flag left under its old name only to refuse is a rename too. An
  added item needs only a snapshot refresh.
- **Migration evidence** is in the category's owner file, never in a note:
  - a key: its `RETIRED_KEYS` entry;
  - a task or flag: its refusing stub in the taskfile or script;
  - anything else: a fixed refusal line in the owner (`map_values.py` or
    `deploy.sh`, the packer or a bundle reader, or the import script).

  Only an executable refusal counts, not a comment, docstring or bare
  string. A refusing stub may use a `{ ...; }` group but no condition. The
  old and new names match as whole tokens. A `RETIRED_KEYS` entry is itself
  a migration and stays.
- **Enforcement.** `scripts/extension_surface.py` derives the list from the
  code. `tests/test_extension_surface.py` fails with a fixed message when the
  list differs from the snapshot `tests/fixtures/extension-surface.json`, or
  when a release note names a migration the code does not provide.
  - The refresh, `.venv/bin/python scripts/extension_surface.py --write`,
    refuses a removal without both.
  - A removed item stays in the snapshot, so its migration stays checked.
  - The values derivation selects every optional block through one fixed
    input in that script. A new optional block extends that input.
- **In the gap.** The import writes `$R/release-notes.diff` (§4). A gap
  patch that changes the surface also changes the snapshot.
- **Out of scope.** The protected list and generator command line (F4), the
  kit key inputs and `agent_doctor` message (F5) and the `Upstream-Release:`
  trailer stay with their owners. They are not part of this API.

## Considered options

- **Keep one shared history.** This is impossible while nothing leaves the
  gap.
- **Rebase gap commits onto each release.** This rewrites the published
  `main` and needs force-pushes.
- **Import releases as unrelated snapshot commits.** Every import becomes a
  whole-tree diff, and ancestry and blame are lost.
- **Deploy gap code from a checkout.** This is unsigned and unrehearsed, and
  it runs next to the live service. Rejected (Q1).
- **Dev kit as new release-bundle members** (D3 option A). Every release
  would carry the toolchain over the slow transfer path, even when the lock
  is unchanged.
- **Build the dev environment in the gap from wheels.** It needs an
  interpreter the gap lacks, and it is a build without an issue.
- **No stability promise for extension points** (open question 2). Imports
  would find breaking renames only when a gap patch or site config fails.

## Consequences

- **AGENTS.md** changes in this PR:
  - The "one history" bullet becomes "Air-gapped GitLab is a downstream
    fork" with a link here. The bullet goes from 286 to 283 bytes.
  - The hard boundary "private configuration" gains "outside the fork's
    `gap/config/`" (Q2). On GitHub, the F3 check keeps `gap/` absent.

  The file stays under 8192 bytes. The MR and gap-maintainer reading is in
  §6, because the root file has no room for it.
- **Trade-off.** Gap development has first-hand production context, but
  weaker models do it, next to the live service. Its learning does not flow
  back upstream. Every import reverts and re-applies every gap patch, so each
  patch adds two commits per import, and a patch the release breaks stays
  set aside until its issue is reworked.
- **Stable extension points** are the public API that §9 defines.
- **Owner documents** change in the follow-up PRs:
  - [deployment policy](../deploy.md#deployment-policy) and
    [CI policy](../deploy.md#ci-policy): the CI job image becomes this
    repository's dev kit image, and verified-release enforcement is added;
  - the live-stack GitLab job map;
  - the [task-runner](../task-runner.md#installation) gap workstation
    recipe;
  - the bootstrap guide, which takes over §4 and §5.
- **Prerequisites.** "Pipeline green" means nothing until the GitLab
  pipeline runs on a real runner
  ([GH#446](https://github.com/yonatan895/qdrant-pdf-rag/issues/446)), and
  GH#1118 A5 (a unit test that skips in GitLab) and C2 are fixed.

## Follow-up items for GH#1118

| Item | Scope | Verifier-owned |
|---|---|---|
| F1 | Outside dry-run, load, deploy, ingest and validate refuse a checkout that is not a verified signed release. The trust anchor is required. The opt-out needs `CI_REHEARSAL=1`. Tests go in the air-gap shell suites, and the connected lanes set the opt-out | Lanes and taskfiles: yes |
| F2 | Two trust anchors (upstream and gap) and the MANIFEST `origin:` field | No |
| F3 | Done in this PR: `tests/test_hygiene.py` fails on GitHub Actions when `gap/` or `.gitlab/` exists | No |
| F4 | The CODEOWNERS generator, its equality test and the GitLab hygiene check | No |
| F5 | The dev kit: image recipe, connected factory job, kit pack, verify/install script, issue snapshot, kit key, `.gitlab-ci.yml` default image and the `agent_doctor` refusal | Yes (`agent_doctor.py`, workflow) |
| F6 | The gap release pipeline: protected-tag build and sign jobs, the unprivileged in-namespace build, the gap pack (`origin: gap`), the key-generation runbook and the release record template | Pack and taskfiles: yes |
| F7 | `scripts/gap/import_release.sh` from §4, with its throwaway-repo tests. §4 then becomes a link | No |
| F8 | The owner-document updates listed under Consequences | No |
| F9 | Isolation in the shared namespace: ServiceAccounts, RBAC, NetworkPolicy, and the recorded `can-i` proof | No |
| F10 | Done with §9: the surface snapshot, its test, the release notes file and the release-procedure step | No |

## Open questions for the maintainer

1. **Namespaces.** Answered in §2.
2. **Extension points.** Decided in §9.
3. **The gap release gate.** Which [release-gate](../release-verification.md)
   checks must a gap release pass? It has no connected lanes.
4. **The build mechanism** for the unprivileged in-namespace image build
   (F6).
