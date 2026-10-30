# ADR-0009: The air-gapped GitLab is a downstream fork; one pinned dev workspace image

- **Status:** proposed (tracker [GH#1118](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118),
  items C1 to C3). Maintainer decisions:
  - D2 in [comment 6442866734](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6442866734);
  - D3 and D4 in [comment 6443165098](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6443165098);
  - Q1, Q2 and Q5, plus coordinator defaults Q3, Q4 and Q6 to Q9, in [GH#1120 comment 6443697962](https://github.com/yonatan895/qdrant-pdf-rag/pull/1120#issuecomment-6443697962);
  - "the air-gapped environment is production" (maintainer clarification, 2026-10-27);
  - the namespace answer (open question 1) in [comment 6451566378](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6451566378);
  - the extension-surface decision (open question 2) in [comment 6451605866](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6451605866);
  - the F9 + F5 amendment (§2, §3, §8), decided 2026-10-28 and 29: the bastion
    toolchain ([6479973901](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6479973901)),
    the namespace teardown ([6480141704](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6480141704)),
    the operator workstation facts ([6480253256](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6480253256)),
    the [plan](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6480613459)
    and its decisions D-1 to D-10 ([6480982316](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6480982316),
    [6481285738](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6481285738), [6481464776](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6481464776)),
    and the review blockers B1 to B3 ([6481952257](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6481952257)).
    This record writes them D-1 to D-10, apart from the earlier D2 to D4;
  - the CI namespace, agent iteration and coding-model key decisions of
    2026-10-30 ([6524485455](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6524485455)),
    which revise D-1, D-4 and D-5 and replace B1.

  This record replaces the AGENTS.md rule "Keep one public GitHub /
  air-gapped GitLab history". Accepting it changes no product default,
  identity or baseline. The enforcement and tooling it commits to are
  follow-up items for GH#1118 (listed at the end) and do not exist yet.
- **Context:**
  - Releases reach the gap as signed bundles, and practically nothing leaves
    it. A single shared history cannot exist, because commits made in the gap
    never return.
  - The air-gapped environment **is** production. The live service runs in
    a non-production namespace on the site cluster. Dev pods and GitLab
    runners run on the same cluster in a separate CI namespace (§2).
  - Development continues there with less capable models, which have
    first-hand production context but no internet and no GitHub.
  - The gap cannot build a unit-test environment from what is transferred
    today: the bundle has no dev wheels, no Helm and no Python 3.14.
  - Review, acceptance and the Verifier exist only on GitHub.
  - The gap's GitLab supports approval rules, CODEOWNERS and project access
    tokens.
  - The site operator workstation is Windows with no WSL and only `oc`. The
    site registry holds only images that earlier releases brought.
- **Decision:**
  - The air-gapped GitLab is a **downstream fork**. Its `main` is upstream
    release merges plus gap-local commits.
  - Gap work lives in a reserved **site layer** so that every release merges
    cleanly.
  - Gap code reaches production only as a **signed gap release**. Production
    deploys only signed releases, never an unsigned checkout.
  - Development, CI and rehearsal are **isolated from the live release**.
  - GitLab CODEOWNERS and approvals stand in for the Verifier.
  - A separate signed **dev kit** carries one pinned dev workspace image. The
    bastion, the dev pod and the runner jobs run it, each under its own
    identity. The kit is a prerequisite of the fresh install.

## 1. One-way flow and release trust

- **Release sources.** Upstream (GitHub `main`) is the only source of
  upstream release commits. They enter the gap only as the signed bundle that
  the [release gate](../release-verification.md) accepted, and the bundle's
  `repo.bundle` HEAD is the release commit. A **gap release** (§5) is the
  only other source of production code.
- **Current behavior** (F1 is done) of `check_checkout_sha` in
  `scripts/airgap/common.sh`, which load, deploy, ingest and validate call
  (the pipeline runs all four):
  - Outside dry-run it refuses, with a fixed message, any checkout that is
    not a verified signed release as the decision below defines it. No
    MANIFEST, no configured trust anchor, a signature that does not verify
    against the anchor, HEAD other than the MANIFEST sha, and a tracked or
    hidden change are each refused.
  - The trust anchor `SNEAKERNET_TRUSTED_PUB` is required. The guard reads
    its anchors from a list of variable names (`RELEASE_TRUST_ANCHOR_VARS`),
    so F2 adds the gap release key as a second name.
  - Under `AIRGAP_DRYRUN=1` it still prints `Notice: dry-run — checkout not
    release-verified` and returns 0.
  - So a plain clone of the fork runs nothing live in the gap.
- **Decision (follow-up F1):** outside dry-run, load, deploy, ingest and
  validate refuse, with a fixed message, any checkout that is not a verified
  signed release. A verified signed release means three things:
  - the MANIFEST's `SHA256SUMS` signature verifies against a configured
    trust anchor, either the upstream release key or the gap release key;
  - HEAD equals the MANIFEST sha;
  - there is no tracked or hidden change.
- **The dev-only opt-out** is one explicit variable,
  `AIRGAP_UNVERIFIED_CHECKOUT=1`, read from the process environment only
  (never `airgap.env`). It keeps the pre-F1 rules: no MANIFEST runs with a
  notice, and without an anchor the signature check is trust on first use.
  It is accepted only together with the existing `CI_REHEARSAL=1` marker,
  which already means "only in a disposable lab, never in the air gap". It
  is refused with a fixed message otherwise. Only connected-side developers and
  the connected CI and Kind lanes may use it. Nobody in the gap uses it, and
  gap rehearsals use signed gap releases instead (§5).
- **Credentials.** The dev pod and the runner jobs run in the CI namespace
  with no RBAC, and job code never sees the runner manager's credentials
  (§2). The dev pod's LiteLLM key is its own, never a production serving
  key. The bastion holds the operator's session token and the registry
  robot auth, memory-only and for one session (§8). So even an unverified
  checkout in the gap cannot change the live release.
- **Outbound.** Nothing in this ADR moves code out of the gap. An upstream
  defect found there leaves only as the organisation permits, at most as a
  description without site data.

## 2. Isolation of development from the live release

- **Namespaces (open question 1, answered; CI namespace decided
  2026-10-30).** Two namespaces share the site cluster:
  - The **production namespace** holds the live release and the bastion.
    Today it is non-production. Once it has been stable for a period, the
    service moves to the production cluster with a production IdP client.
    The move is a planned fresh install, not an in-place upgrade: the
    identical signed release is promoted there (§5 step 6) with a new IdP
    client. After the move, the old namespace stays as the gap-release
    rehearsal namespace.
  - The **CI namespace** holds the GitLab runner manager, its job pods and
    the dev pod. The platform team creates it. Nothing in it holds
    production deploy authority, and the gap release signing key and any
    image push credential never enter it: the runner manager can read every
    Secret there and exec into every pod. The F6 build and sign jobs
    therefore run outside the CI namespace (open question 4).
  - The namespace in use on 2026-10-28 is torn down, not remediated. The
    fresh install starts with every rule below in place, so F9 and F5 land
    before it.
- **Identities (F9).** Each pod role has its own ServiceAccount. The
  **F9-defined pods** (bastion, dev and runner job) have no RBAC and no
  token automount, and their egress is limited to the listed destinations:

  | Role | Namespace | ServiceAccount | Egress |
  |---|---|---|---|
  | bastion | production | its own; acts with the operator's session credentials (§8) | `kubernetes.default.svc` at the API server addresses in site config (D-3), site registry, DNS |
  | dev | CI | its own | GitLab, the LiteLLM endpoint (with the dev key, below), DNS |
  | runner job | CI | `mfr-runner-job` | GitLab, DNS |
  | runner manager | CI | created by the site's GitLab Runner Operator | `kubernetes.default.svc` at the API server addresses in site config, GitLab, DNS |

  The F6 build and sign jobs use their own identity, never `mfr-runner-job`.
- **Labels.** F9-defined pods carry only the
  `workspace.mainframe-rag.io/role` key (`bastion`, `dev`, `runner`), never
  `app` or `app.kubernetes.io/*`. Chart NetworkPolicies, Services and the
  vendored Qdrant chart select on those keys, so such a label could admit a
  workspace pod to production peers or make it a Service backend.
- **Egress-side enforcement.** Each F9-defined pod in the production
  namespace is selected by a default-deny policy plus its egress
  allow-list. In the CI namespace, one default-deny egress policy selects
  every pod (`podSelector: {}`), the runner manager included, and each role
  gets its allow-list from the table. Production Qdrant has no
  NetworkPolicy, so this egress policy is what keeps CI pods off it (its
  API keys still guard the data). The agent is also guarded on the ingress
  side: the chart's agent policy admits only pods of its own namespace,
  unless `networkPolicy.allowedNamespaces` lists another. The site config
  never lists the CI namespace there.
- **Production authority.** Of the F9-defined pods, only the bastion
  reaches the API server and the registry. Operator scripts reach the
  service through `$KC exec`, so the bastion needs nothing else. Dev and
  runner-job pods have no route to either, so the network enforces "only the
  bastion holds production authority". No F9-defined pod can read a
  production Secret (the Qdrant writer and admin keys, the gateway keys,
  the OIDC client secret, the gap signing key) or write any production
  workload object.
- **Runner manager (D-1, revised 2026-10-30).** CI runs in the CI namespace
  through the site's GitLab Runner Operator, with the Kubernetes executor.
  Each job runs in a fresh pod under `mfr-runner-job`; GitLab `services:`,
  such as the `sim` Qdrant, run as containers inside it. Job code never sees
  the manager's credentials.
  - The manager's operator-created Role lets it read every Secret in the CI
    namespace, create and delete pods there, and exec into or attach to
    them. A Role cannot narrow these to names generated at run time.
  - The namespace boundary confines it. The manager can read the CI
    namespace's Secrets (the runner token, `mfr-dev-gitlab`,
    `mfr-dev-litellm` and the pull-only registry robot), never a production
    Secret. This replaces the
    [B1](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6481952257)
    accepted trust in production Secrets, and the request for a platform
    ValidatingAdmissionPolicy is withdrawn.
  - The runner configuration allows no job overrides. It has no
    `*_overwrite_allowed` setting (ServiceAccount, namespace, annotations,
    helper image), restricts `allowed_images` and `allowed_services` to the
    site registry, runs no privileged container, and sets requests and
    limits on every container (build, helper and services).
- **CI capacity and agent iteration (2026-10-30).** Gap agents reach the CI
  namespace only through GitLab: they push a branch and read the result.
  The dev pod has no RBAC there, because creating a pod would let it mount
  any Secret in the namespace.
  - There are four job slots of about 1 CPU each, since pytest runs on one
    core. They form two runner registrations: `gate`, one slot for merge
    request and tag pipelines, and `wip`, three slots for branch pipelines.
    Agent work never queues ahead of a merge gate. Tag pipelines on `gate`
    run only the verification lanes; the F6 build and sign jobs do not run
    on these runners.
  - The unit job is sharded (`parallel: 4`). Jobs are `interruptible`, and
    redundant pipelines are cancelled automatically, so only a branch's
    newest commit holds slots.
  - Branch pipelines select lanes with the fail-closed classifier in
    `scripts/review_tooling.py`, as GitHub does. Merge request and tag
    pipelines still run every lane
    ([GH#808](https://github.com/yonatan895/qdrant-pdf-rag/issues/808)).
  - `scripts/gap/ci_status.py` (standard library, `read_api`) waits on a
    commit's pipeline and prints each job's status and the failing test ids
    from its JUnit report.
- **Secrets.** No F9-defined pod in the production namespace mounts a
  Secret; the bastion's credentials are session-piped (§8). In the CI
  namespace the dev pod mounts two:
  - `mfr-dev-gitlab` (D-4, amended 2026-10-30), a GitLab project access
    token. It is a bot identity with the Developer role, so it cannot
    approve, merge to protected `main` or change approval rules. Its scopes
    are `read_repository`, `write_repository` and `read_api`, never `api`.
    `read_api` reads pipelines, job logs and test reports. The token expires
    after about 90 days and is rotated.
  - `mfr-dev-litellm`, the coding agents' own LiteLLM key (below).

  The runner manager holds the operator's runner token Secret. Job pods
  pull with a pull-only registry robot in the CI namespace, never the
  production push robot. The platform team is asked whether etcd encryption
  at rest is enabled.
- **The coding model's key (maintainer decision 2026-10-30).** The gap's
  coding agents call the product's LiteLLM endpoint with their **own key**,
  which the platform team issues separately from the product's serving keys.
  It lives in `mfr-dev-litellm` in the CI namespace, and the dev pod's egress
  allows the LiteLLM endpoint. The dev key has its own rate limit, budget,
  attribution in LiteLLM logs and spend, and rotation, so a runaway agent
  loop or a leak from the dev pod never touches production serving. The
  runner manager can read it, like every CI namespace Secret, and runner
  jobs never receive it.
- **Resources (D-5, revised 2026-10-30).** Both namespaces enforce quota on
  requests and limits, so every workspace container sets both, with small
  caps.
  - The production request covers production at site sizing (set as R2
    `QDRANT_*`/`INGEST_*` values at the fresh install), the bastion and
    rollout surge.
  - The CI request covers the manager, the four job slots and the dev pod
    with its PVC (§8). Its figures are a draft estimate, still to be
    measured on the real jobs.
  - Both sets of figures are recorded privately. CI no longer shares a
    quota with production, so the rule that runner jobs never run alongside
    an ingest Job is retired.
- **The isolation proof** (`scripts/devkit/isolation_proof.py`, recorded in
  `gap/records/`) covers both namespaces. It holds:
  - each ServiceAccount's `can-i` list, as verbs and resources only,
    including the manager Role's verbs and resources;
  - a negative matrix of what each identity is refused;
  - every pod classified by its labels, with mounted Secrets checked
    against the allowed list above: none on F9-defined pods in the
    production namespace; in the CI namespace, the dev pod's two, the
    manager's runner token and the pull robot;
  - the rendered runner configuration, showing that it allows no override;
  - live network probes from each F9-defined role and from a probe pod
    carrying the runner manager's labels, including the API server addresses
    (D-3), probes from the CI namespace to production Qdrant and the agent,
    and a negative control that shows the probe reaches a target once the
    policy is absent;
  - the production release's `networkPolicy.allowedNamespaces`, showing it
    does not list the CI namespace.
- **Test data.** Tests and development never write to the production
  collection or alias. The dev pod's Qdrant is a localhost sidecar, and a CI
  job's is the `sim` service inside its job pod. Rehearsals use a
  non-production release with its own PVCs (§5). The chart fixes the logical
  collection name, so a separate collection in the production Qdrant is not
  an option without its own approved concern.
- **Storage.** The import procedure (§4) needs room for about three times
  the release tarball, on the dev PVC (§8). Remove `$R` after the import MR
  merges.

## 3. Site layer

`site/` is taken: it is the public static website. The site layer is:

| Path | Holds |
|---|---|
| `gap/README.md` | The gap entry point, read after AGENTS.md. It names the gap maintainer group and the named approvers, restates these rules and says where site inputs live. Never name it `AGENTS.md`: the context check would require it in the upstream chain table |
| `gap/patches.md` | The gap-patch ledger. A `# Gap patches` title line, then one section per patch: a heading `## GL#NNN <full commit sha>`, then one `- path: <path>` line per upstream-owned path the patch changes. Other lines are free text. Sections are in application order: a new patch is appended at the end |
| `gap/config/` | The site's non-secret configuration (Q2), such as its `airgap.env` without secret values. Secrets stay in Kubernetes Secrets or the site's secret store and are referenced by name only. Site values for the `deploy/workspace/` manifests go in `gap/config/workspace.env`. Rehearsal-only overrides (`CI_REHEARSAL` keys) never go here |
| `gap/records/` | Dated site records, `YYYY-MM-DD-<slug>.md`: imports, gap releases, go-live checks, isolation proofs, incidents. Gap records never go in `docs/records/` |
| `gap/runbooks/` | Site procedures that upstream cannot know |
| `gap/dev/` | Site dev notes. The workspace manifests are upstream-owned (below) |
| `.gitlab/CODEOWNERS` | Generated (§6). GitHub ignores `.gitlab/` |
| `.gitlab/issue_templates/`, `.gitlab/merge_request_templates/` | Gap issue and MR forms |

Everything else is **upstream-owned**: `AGENTS.md`, `docs/` (including the
ADRs and `docs/records/`), `.agents/`, `.claude/`, `.github/`,
`.gitlab-ci.yml`, `airgap.env.example`, `charts/`, `deploy/`, `images/`,
`scripts/`, `src/`, `tests/`, `evals/`, `Taskfile.yml`, `taskfiles/`, the
locks and pins, and `site/`. The workspace manifests live in
`deploy/workspace/`, not `gap/`: upstream never tracks `gap/`, and the fresh
install needs them on day one.

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
   - the hash-locked runtime and build wheels (carried by a later dev kit;
     kit v1 has none, §8);
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
   either to a rehearsal namespace or to a non-production release name in
   the production namespace, with its own Qdrant instance and PVCs and
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

- **One image** (`images/Containerfile.devkit`, under the existing base-pin
  check) serves the bastion, the dev pod and the runner jobs. It is
  UBI-based and holds:
  - the CPython 3.14 GIL build, the pinned Task and Helm 4;
  - the hash-locked dev wheels from `requirements.dev.lock.txt`;
  - `oc` and `skopeo`, plus git-core, openssl, sha256sum, tar and gawk, from
    an exact RPM NEVRA list, so drift fails the build. GPL-licensed RPMs are
    accepted;
  - the session login helpers (`mfr-session-login` and its logout).

  It runs under `restricted-v2` with an arbitrary non-root UID. The
  checkout's own source is used in place, so development installs and
  builds nothing.
- **The bastion** runs the Linux-only operator path. It is a pod on this
  image under its own ServiceAccount, with no RBAC (D-2) and no Secret
  mount. At session start the operator's own `oc` on Windows pipes in two
  credentials
  ([B3](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6481952257)):
  - the short-lived `oc login` token (D-3), `oc whoami -t | oc exec -i
    mfr-bastion -- mfr-session-login`, into a kubeconfig that targets
    `kubernetes.default.svc` with the CA from the `kube-root-ca.crt`
    ConfigMap;
  - the existing registry robot auth, `oc get secret <robot> -o jsonpath=… |
    oc exec -i mfr-bastion -- mfr-session-login --registry`, into
    `/run/mfr-session/auth.json`.

  Both live only in the memory-only `/run/mfr-session/`. `oc logout`
  revokes the token; logout or pod deletion removes both.
- **The Windows operator path** pushes the first image, since no site image
  carries `oc` and `skopeo`. The literal runbook
  (`docs/runbooks/devkit-windows.md`) runs the
  [plan's](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6480613459)
  five steps, pushing with `oras.exe copy --from-oci-layout` and creating
  the bastion with `oc process --local | oc apply`.
  - **Trust anchor** ([B2](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6481952257)).
    The kit and the release bundle each ship a `.sha256` file that the
    runbook compares with `Get-FileHash`, which catches corruption and the
    wrong version. Against tampering, the connected side publishes both
    SHA-256 fingerprints on GitHub (the release comment or record). The
    operator carries the first 16 hex characters of each by a route separate
    from the transfer and checks them by eye on Windows. Both checks pass
    before any push and before any token reaches a pod. A verifier inside
    the kit cannot vouch for itself, so the bastion's signature checks come
    after the anchor and are trusted only because of it.
  - **Fallback:** if Windows cannot push, `oc cp` a static Linux `oras` and
    the archive into a pod from the previous release's agent image, already
    in the site registry, and push from inside.
  - **Proof** splits as the plan lists: Linux CI, a GitHub-hosted
    `windows-2025` job (D-6) and recorded site steps.
- **Kit key (Q3).** `scripts/devkit_key.py` computes one SHA-256 over a
  fixed, ordered list of pin files that upstream owns and protects:
  `requirements.dev.lock.txt`, `locks/cp314-linux-x86_64.json`,
  `scripts/tools/task-pin.txt`, `scripts/tools/helm-pin.txt`,
  `scripts/tools/oc-pin.txt` (the version the platform team names, D-6b),
  `images/devkit-packages.txt` and
  `images/Containerfile.devkit`. `scripts/tools/devkit-pin.txt` records the
  key and the image digest.
  - The image is pinned by manifest digest.
  - The default `CI_PYTHON_IMAGE` in `.gitlab-ci.yml` names that digest, and
    a unit test asserts it matches the kit pin.
  - The gap leaves the project-level variable unset, so each branch's
    pipeline uses the kit for its own lock.
  - **A stale pin** (D-8) is refused at kit pack time and by the release
    gate. A dependency PR does not have to re-pin.
- **Built connected-side only.** The connected factory builds the image, as
  it builds product images. The gap only verifies the kit and copies the
  image into the site registry. Gap releases (§5) are the only builds
  in the gap, and each one needs an approved issue.
- **The kit** is a signed tarball, separate from the release bundle,
  `mfr-devkit-<key>.tar`. It holds:
  - the image as an OCI layout, with the dev wheels inside it only: a
    `prepare_python.py --kit` mode (D-7) keeps them from shipping twice;
  - a pinned `oras.exe`, the bastion template and the Windows runbook;
  - `MANIFEST.txt`, recording the key, the image digest, the pin-file
    digests, the upstream commit it was built from and the snapshot time;
  - `sbom.json`, the third-party notices and the licence check;
  - `SHA256SUMS`, signed with the connected release key (Q4);
  - a verify/install script, run in the bastion after the trust anchor:
    signature, inventory, and the digest read back from the registry and
    from the pod's `imageID`;
  - the GitHub issue snapshot.
- **Cadence (D-9).** A release ships a new kit only when the key changes, and
  the release MANIFEST names the key in a `devkit_key:` field. Kit v1 has no
  gap-release build inputs; F6 adds them under a new key. The first kit
  rides with the signed release candidate, before the fresh install. A
  re-sign under the same key is
  allowed when only the issue snapshot changes: the MANIFEST records the new
  snapshot time, and the image digest is unchanged.
- **Storage (D-10).** The bastion `/work` PVC is 30 GiB, in the
  production namespace. The dev PVC is 20 GiB, in the CI namespace.
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
- **CI in the production namespace, with the runner manager as accepted
  trust** (D-1 as decided on 2026-10-28, review B1). Replaced on 2026-10-30
  by the CI namespace (§2). RBAC cannot narrow the manager's Secret reads
  and admission never sees them, so the manager could have read every
  production Secret.
- **A platform-run runner manager** (plan D-1). Not pursued: the CI
  namespace confines the manager without a platform-run service.
- **The product's LiteLLM key for the coding agents.** Rejected on
  2026-10-30. A copy in the CI namespace would share the product's rate
  limit and budget, hide coding traffic in its logs and spend, and make a
  dev pod leak a production rotation.
- **An in-memory-only GitLab credential** (plan D-4). Rejected for a scoped
  project access token Secret (§2).

## Consequences

- **AGENTS.md** changes in this PR:
  - The "one history" bullet becomes "Air-gapped GitLab is a downstream
    fork" with a link here. The bullet goes from 286 to 283 bytes.
  - The hard boundary "private configuration" gains "outside the fork's
    `gap/config/`" (Q2). On GitHub, the F3 check keeps `gap/` absent.

  The file stays under 8192 bytes. The MR and gap-maintainer reading is in
  §6, because the root file has no room for it.
- **Trade-off.** Gap development has first-hand production context, but
  weaker models do it, on the live service's cluster. Its learning does not flow
  back upstream. Every import reverts and re-applies every gap patch, so each
  patch adds two commits per import, and a patch the release breaks stays
  set aside until its issue is reworked.
- **Stable extension points** are the public API that §9 defines.
- **Owner documents** change in the follow-up PRs:
  - [deployment policy](../deploy.md#deployment-policy) and
    [CI policy](../deploy.md#ci-policy): the CI job image becomes this
    repository's dev kit image, CI runs in its own namespace, and
    verified-release enforcement is added;
  - the live-stack GitLab job map;
  - the [task-runner](../task-runner.md#installation) gap workstation
    recipe;
  - the bootstrap guide, which takes over §4 and §5;
  - [licensing](../licensing.md): GPL-licensed RPMs in the workspace image
    (§8).
- **Prerequisites.** "Pipeline green" means nothing until the GitLab
  pipeline runs on a real runner
  ([GH#446](https://github.com/yonatan895/qdrant-pdf-rag/issues/446)), and
  GH#1118 A5 (a unit test that skips in GitLab) and C2 are fixed.

## Follow-up items for GH#1118

| Item | Scope | Verifier-owned |
|---|---|---|
| F1 | Done: outside dry-run, load, deploy, ingest and validate refuse a checkout that is not a verified signed release. The trust anchor is required. The opt-out needs `CI_REHEARSAL=1`. Tests go in the air-gap shell suites. The live bundle lanes export the packing job's key as the anchor; hermetic fixtures from plain trees set the opt-out | Lanes and taskfiles: yes |
| F2 | Two trust anchors (upstream and gap) and the MANIFEST `origin:` field | No |
| F3 | Done in this PR: `tests/test_hygiene.py` fails on GitHub Actions when `gap/` or `.gitlab/` exists | No |
| F4 | The CODEOWNERS generator, its equality test and the GitLab hygiene check | No |
| F5 | The dev kit (§8), as [plan](https://github.com/yonatan895/qdrant-pdf-rag/issues/1118#issuecomment-6480613459) PR-2, PR-4 (with `prepare_python.py --kit`) and PR-5 (with `scripts/gap/ci_status.py`) | `agent_doctor.py` and `prepare_python.py`: yes. `devkit_key.py`: recommended for `UNIT_INPUTS`. The factory workflow is separate from `e2e.yml` and not an acceptance producer: no |
| F6 | The gap release pipeline: protected-tag build and sign jobs, the unprivileged in-namespace build, the gap pack (`origin: gap`), the key-generation runbook and the release record template | Pack and taskfiles: yes |
| F7 | `scripts/gap/import_release.sh` from §4, with its throwaway-repo tests. §4 then becomes a link | No |
| F8 | The owner-document updates listed under Consequences | No |
| F9 | Isolation of the production and CI namespaces (§2), as plan PR-3, before the fresh install, including the runner registrations and their configuration | Lane taskfile: yes. Manifests and proof: no |
| F10 | Done with §9: the surface snapshot, its test, the release notes file and the release-procedure step | No |
| F11 | GitLab CI iteration (§2): the sharded unit job, `interruptible` jobs, the `gate` and `wip` runner tags, and lane selection on branch pipelines, with the GitHub/GitLab parity test | No |

## Open questions for the maintainer

1. **Namespaces.** Answered in §2.
2. **Extension points.** Decided in §9.
3. **The gap release gate.** Which [release-gate](../release-verification.md)
   checks must a gap release pass? It has no connected lanes.
4. **The build mechanism** for the unprivileged in-namespace image build
   (F6), and where its build and sign jobs run. They cannot run in the CI
   namespace, because the signing key and the push credential never enter
   it (§2).
5. **F9 + F5 decisions D-1 to D-10.** Closed (status block).
