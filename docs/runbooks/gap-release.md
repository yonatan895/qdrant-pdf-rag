# Gap release runbook (ADR-0009 §5, F6 increment 1)

How a commit on the fork's protected `main` becomes a signed gap release
that the installed release verifies before anyone deploys it. Owner:
[ADR-0009 §5](../adr/0009-gap-fork.md#5-gap-releases); design and decisions
D1–D12: [#1207](https://github.com/yonatan895/qdrant-pdf-rag/issues/1207).
The record each release fills in is the
[gap release record template](../gap-release-record.md).

## What this increment can release

- The cumulative delta from the base upstream release (`git diff
  --no-renames gap_base <tag>`) may touch only paths on the allow-list in
  [`scripts/gap/allowlist.py`](../../scripts/gap/allowlist.py): the site
  layer (`gap/`, `.gitlab/`), `charts/mainframe-rag/`, `deploy/`,
  `Taskfile.yml`, `taskfiles/`, `.gitlab-ci.yml`, `airgap.env.example`,
  docs, tests and evals, and `.agents/`/`.claude/` except what feeds a
  copied member. The image archives are the base release's bytes under the
  new commit's member names; no step is built or executed (D1).
- A change under `src/`, or under `scripts/` where it enters the image,
  needs layer replacement (increment 2) and is refused. A change to a lock,
  pin, Containerfile, licence input or any other refused path is never
  released from the gap: report it upstream (D2). Inside
  `charts/mainframe-rag/`, a nested `charts/` subtree (vendored subchart)
  and any `Chart.lock` are refused too: third-party chart code would reach
  the deployment with no licence or provenance record. The allow-list sees
  paths only, so it cannot see a `dependencies:` edit in `Chart.yaml`.
- `release.sh classify` lists every changed path with its verdict.

## Before the first release

- The installed release (the workspace the guard verifies) contains
  `scripts/gap/release.sh`. Every command below runs from that workspace,
  so a change to these tools takes effect one release later.
- Both anchors are plain files in `/work/anchors/` and are **exported in the
  process environment**: `release.sh` ignores `airgap.env`, except that
  `rehearse-check` (step 7) reads the rehearsal and installed workspaces'
  deploy settings the way `deploy.sh` does. The gap release stage variables
  of steps 7 and 8 (`GAP_REHEARSAL_*`, `GAP_PROMOTE_IDENTITY`) are read from
  the process environment only, too (`airgap:config check` reports them as
  such when `airgap.env` sets one).

  ```sh
  export SNEAKERNET_TRUSTED_PUB=/work/anchors/upstream.pub
  export SNEAKERNET_GAP_TRUSTED_PUB=/work/anchors/gap.pub
  export TMPDIR=/work/tmp   # bundles unpack to GBs; the pod's /tmp is small
  ```

- The base upstream bundle (tarball and `.sha256`) of the release the fork's
  last import came from is retained; its sha is the newest
  `Upstream-Release:` trailer on `main`.
- A read-only GitLab credential reaches the bastion through git's own
  credential mechanism in the memory-only session directory, never in the
  URL (site fact: not yet issued).

## Key ceremony (once; again on rotation)

The gap maintainer runs it. The bastion never holds a private key, a
passphrase or a credential that can sign (D3).

- **Vault transit (preferred, when the site Vault supports it).** Create a
  non-exportable `rsa-3072` transit key; bind a policy that allows only
  `<mount>/sign/<key>` to the named key holders. On the maintainer's
  workstation, read the public key and normalise it:

  ```sh
  VAULT_ADDR=https://vault.site.example VAULT_TOKEN=... \
    python3 -I sign_vault.py vault-pubkey --key-name <key> --out gap.pub
  ```

  It must print an RSA-3072 key and its fingerprint (`PUBKEY v<n> <sha256>`).
  Add the Vault address to the isolation proof's site values (`VAULT_CIDRS`,
  `VAULT_PORT`): the proof then requires every workspace role, the bastion
  included, to be blocked from it. Leave the `VAULT_CIDRS` line out only when
  the site has no Vault; the record then lists `vault` under
  `optional_classes_unset`. A line that is present but blank or `[]` is
  refused.
- **Fallback, without Vault.** Generate a passphrase-encrypted RSA-3072 key
  in the zero-egress signing pod
  ([`deploy/workspace/sign.template.yaml`](../../deploy/workspace/sign.template.yaml):
  no `/work`, no Secret, no token, no egress, a memory-only
  `/run/mfr-session`; render it with
  `sh scripts/gap/release.sh render-role --template sign --param-file <file> | oc apply -f -`
  and delete the pod afterwards), store only the encrypted
  file in the site secret store, keep the passphrase with the key holder,
  and delete the pod:

  ```sh
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -aes-256-cbc -out gap-release.key.enc
  openssl pkey -in gap-release.key.enc -pubout -out gap.pub
  ```

  Anyone who can exec into that pod while it runs can take the key and the
  passphrase: every namespace admin. That is why Vault is preferred.
- Either way: install `gap.pub` as `/work/anchors/gap.pub` (refuse to
  overwrite an existing one; rotation is a new record and a replaced file),
  record its fingerprint in `gap/records/` through an ordinary MR, and carry
  the fingerprint outside the bastion too: an upstream installed release has
  no `gap/` to read it from.

## Release steps

Working state lives in `/work/gap-release/<tag>/`, one directory per tag.
Every command either writes a member once or stops with one fixed `STOP:`
line; rerun the same command after fixing the cause.

1. **Authorise.** The approver approves a GitLab issue that names the full
   commit sha. The gap maintainer creates the protected annotated tag
   `gap-release-<n>` on that commit, which must be a merge commit on the
   first-parent chain of `main`, with exactly one `Gap-Release: GL#<issue>`
   line in the tag message. A wrong tag is never moved: abandon it and
   increment `<n>`. Pipeline status is recorded, not trusted (D4).
2. **Pack** (bastion, no cluster or registry credential: run
   `mfr-session-logout` first).

   ```sh
   sh scripts/gap/release.sh pack --tag gap-release-<n> --remote <read-only GitLab URL> \
     --base-bundle /work/<base>/qdrant-pdf-rag-<base sha>.tar \
     --work-dir /work/gap-release/gap-release-<n>
   ```

   It refuses a session holding a kubeconfig, registry auth or
   `SNEAKERNET_SIGNING_KEY`, verifies the base bundle against the upstream
   anchor, fetches only the tag and `main` into a bare repository, checks
   the tag, the allow-list and the ledger (outside `gap/` and `.gitlab/`,
   the tree equals the base except the paths `gap/patches.md` lists), and
   writes `out/`, `out/SHA256SUMS` and `request.txt` (it unpacks the base
   into `<work-dir>/scratch/`, which it removes on entry and exit). It prints both anchor
   fingerprints, the release identity (the sha256 of `SHA256SUMS`) and
   `PACKED <sha> <identity>`. Compare the fingerprints with the recorded ones.
3. **Sign** (outside the bastion). Copy `SHA256SUMS` out, recompute its
   sha256 and compare it with the identity pack printed and with the sha in
   the issue: that reading is the authorisation (D4). Copy
   `scripts/gap/sign_vault.py` from the installed workspace (standard
   library only).
   - Vault, on the workstation:

     ```sh
     VAULT_ADDR=https://vault.site.example VAULT_TOKEN=... \
       python3 -I sign_vault.py vault-sign --key-name <key> --sums SHA256SUMS \
       --expected-identity <identity> --verify-pub gap.pub --out SHA256SUMS.sig
     ```

     It signs the prehashed digest with PKCS#1 v1.5 and SHA-256 and converts
     `vault:v<n>:<base64>` to the raw signature bootstrap verifies; plain
     `http://` is accepted only to a loopback address.
   - Vault from a pod, only when the workstation cannot reach Vault:
     [`sign-vault.template.yaml`](../../deploy/workspace/sign-vault.template.yaml)
     (egress to Vault and DNS only), rendered with
     `release.sh render-role --template sign-vault --param-file <file> | oc apply -f -`;
     pipe in the maintainer's Vault token for the one call and delete the pod.
     `render-role` runs `oc process --local` and prints nothing when any
     NetworkPolicy egress rule has no peers (`VAULT_CIDRS=[]` renders
     `to: []`, which allows every destination).
   - Fallback, in the zero-egress signing pod: pipe in `SHA256SUMS`, the
     encrypted key and `sign_vault.py`, then run
     `python3 -I sign_vault.py key-sign --sums SHA256SUMS --key gap-release.key.enc
     --expected-identity <identity> --verify-pub gap.pub --out SHA256SUMS.sig`
     (openssl prompts for the passphrase, so use `oc exec -it`), pipe the
     signature out and delete the pod.

   Copy `SHA256SUMS.sig` into `/work/gap-release/<tag>/`.
4. **Assemble** (bastion).

   ```sh
   sh scripts/gap/release.sh assemble --work-dir /work/gap-release/gap-release-<n> \
     --signature /work/gap-release/gap-release-<n>/SHA256SUMS.sig
   ```

   It verifies the signature with the gap anchor, records the identity in
   `signed.sha256` (a different `SHA256SUMS` for the tag is refused from then
   on) and writes `qdrant-pdf-rag-<sha>.tar` and its `.sha256`.
5. **Verify** (any bastion session; reads only).

   ```sh
   sh scripts/gap/release.sh verify --bundle /work/gap-release/gap-release-<n>/qdrant-pdf-rag-<sha>.tar \
     --base-bundle /work/<base>/qdrant-pdf-rag-<base sha>.tar --scratch-dir /work/tmp \
     [--remote <read-only GitLab URL>]
   ```

   In order: the release guard on the installed workspace; the member
   inventory; the signature with the gap anchor; `repo.bundle` by content
   (one `HEAD` ref equal to `sha:`, `gap_tree`, no object outside that
   history, `fsck --strict`); the base bundle against the upstream anchor; the
   allow-list and the ledger; then every member by its rule (copied members
   equal the base's signed checksums, `bootstrap.sh` equals the commit's
   blob, the app archives equal the base's, and `MANIFEST.txt`,
   `PACKING_RECORD.txt` and `sbom.json` equal their regeneration). It prints
   the upstream-owned paths the release changes and `VERIFIED <sha>
   <identity>`. With `--remote` it also requires the tag to still name `sha:`.
6. **Record.** Copy the [template](../gap-release-record.md) to
   `gap/records/YYYY-MM-DD-gap-release-<n>.md`, fill it from `request.txt`
   and the pack, sign and verify output, and merge it through an ordinary
   MR. It starts `Promotion decision: BLOCKED`.
7. **Rehearse** in the dedicated rehearsal namespace, never the production
   namespace or a second release in it (D6): a fresh workspace with both
   anchors and `NAMESPACE` set to the rehearsal namespace, the ordinary
   `airgap:` pipeline, then the go-live checks. Apply
   [`rehearsal-egress.template.yaml`](../../deploy/workspace/rehearsal-egress.template.yaml)
   to the rehearsal namespace first
   (`release.sh render-role --template rehearsal-egress --param-file <file> | oc apply -f -`;
   an empty `GATEWAY_CIDRS` is refused). Before the pipeline, from the
   installed workspace and in the shell and kube login the pipeline will use, run

   ```sh
   sh scripts/gap/release.sh rehearse-check --rehearsal-workspace <rehearsal workspace> \
     --rehearsal-namespace <ns> --prod-namespace <live ns> \
     --rehearsal-registry <rehearsal INTERNAL_REGISTRY> --prod-registry <live INTERNAL_REGISTRY> \
     --kube-context <the rehearsal cluster's context name> \
     --candidate-manifest <bundle>/MANIFEST.txt --installed-manifest dist/MANIFEST.txt
   ```

   It resolves `NAMESPACE`, `INTERNAL_REGISTRY`, `QDRANT_RELEASE` and `KC`
   for each workspace with that workspace's own `scripts/airgap/common.sh`,
   as `deploy.sh` does (this environment over `AIRGAP_ENV` or `airgap.env`,
   then the deploy defaults), and the kube context from `KC config
   current-context`. It refuses: a workspace it cannot resolve; the installed
   workspace itself; a rehearsal `NAMESPACE` that is unset (deploy would
   default it) or equal to the installed workspace's, ignoring case and
   surrounding spaces; an installed workspace whose `NAMESPACE` is unset (its
   target is unknown); an unset `INTERNAL_REGISTRY`; no current context; a
   `KC` with arguments; a `HELM_KUBECONTEXT` other than that context; any
   typed value that differs from its resolved one; a changed third-party
   `*_digest` while the two registries are the same path ignoring case, port
   and a trailing `/` (load pushes third-party images under fixed tags); and
   a rehearsal namespace in which the deploy's `KC` lists any Deployment,
   StatefulSet, Job, Pod, PVC or Helm release Secret, or answers with
   anything but a list of objects. `HELM_KUBECONTEXT` and `KUBECONFIG` count
   as the rehearsal workspace exports them, `airgap.env` included, and the
   listing uses that `KUBECONFIG`.

   Then run the pipeline from the rehearsal workspace with the stage declared
   in the process environment (never in `airgap.env`: an assignment or
   `export` there is ignored for these, a `readonly` one is refused). Keep the rehearsal settings in that workspace's own `airgap.env`:
   inside a stage an exported value cannot be told from a file value, so the
   stages resolve both workspaces from their own `airgap.env` (or the deploy
   defaults) and refuse a stage whose `NAMESPACE`, `INTERNAL_REGISTRY` or
   `KC` differs from that, for example one exported in the shell or chosen
   with `AIRGAP_ENV`.

   ```sh
   export GAP_REHEARSAL_INSTALLED_WORKSPACE=<installed workspace>
   export GAP_REHEARSAL_KUBE_CONTEXT=<the rehearsal cluster's context name>
   sh scripts/tools/run-task.sh airgap:pipeline
   ```

   Every stage that writes (`airgap:load`, `airgap:deploy`, `airgap:ingest`,
   `airgap:retire-legacy`) refuses a gap bundle (`origin: gap`) unless a
   rehearsal or a promotion is declared, and for a rehearsal repeats the
   resolution and refusals above in its own process, before any registry or
   cluster write (#1338): the installed workspace itself, an unset or live
   `NAMESPACE`, an installed workspace that does not record its `NAMESPACE`, an unset `INTERNAL_REGISTRY`, a `KC` with arguments, no
   current context, a `HELM_KUBECONTEXT` other than that context, a changed
   fixed-tag image on the installed registry path, a stage value that differs
   from the workspace's resolution, and a context other than
   `GAP_REHEARSAL_KUBE_CONTEXT`. A later `NAMESPACE=…` override, login or
   context switch is therefore refused too. The stages do not repeat the
   emptiness check: a repeat deploy or ingest into the same rehearsal is
   allowed, so `rehearse-check` stays the step that proves the namespace
   started empty. Afterwards uninstall both
   releases, delete the namespace's PVCs and Jobs and confirm it is empty.
8. **Promote** only when every required row of the record is `PASS` or
   `INHERITED` and the approver signed off: bootstrap the same signed
   members (the sha256 of `SHA256SUMS` equals the record's identity) into a
   fresh workspace and deploy as usual, declaring the promotion in the
   process environment with that identity:
   `GAP_PROMOTE_IDENTITY=<record's release identity> sh scripts/tools/run-task.sh airgap:pipeline`.
   A stage refuses an identity that is not the bundle's, and a promotion
   declared together with a rehearsal. Promotion runs no rehearsal guard:
   that the bundle was rehearsed is still the record's to show (D9). When
   the declared `airgap:deploy` completes its rollout, it records the
   promotion in this workspace's `dist/gap-promotion.txt` (#1344). A failed
   or abandoned promotion, a declared load, ingest or retire-legacy on its
   own, and a dry-run record nothing and never refresh an existing record.
   Every later `airgap:load`, `airgap:deploy`, `airgap:ingest` or
   `airgap:retire-legacy` run from this live workspace then needs no export,
   as long as the file still names this bundle's signed identity, this
   workspace's path and directory inode, and the `NAMESPACE` and
   `INTERNAL_REGISTRY` the stage resolves. A copy of the workspace at
   another path, an edited file, another bundle bootstrapped into it, or
   another target is refused (the message names the first field that
   differs). The inode is a best-effort check only: a workspace removed and
   restored at the same path may get the same inode back, so only the bundle
   and target recorded at that path can pass there. To record it again, run
   the promotion deploy with `GAP_PROMOTE_IDENTITY` exported; its completed
   rollout rewrites the file. Never copy a promoted workspace to make a rehearsal
   workspace, and never write the file by hand. The file is an **explicit
   exception** to "no file declares a promotion" (maintainer decision on
   #1361, #1344 amendment): it stands in for the export and is not an
   attestation. Its basis is that the people who can write `/work` are
   exactly the people with live-namespace rights, who can export the
   identity anyway. Its residual is persistence: it covers every later
   undeclared run by anyone in this workspace until it is removed (step 10).
   If `/work` ever becomes writable by anyone without live-namespace rights
   (backups or restores by another team, another pod mounting the volume),
   the exception lapses and the design must be revisited before the next
   gap release. The identity names the bundle, not the workspace:
   the rehearsal's checks come from the `GAP_REHEARSAL_*` declaration, never
   from this one, and a declared rehearsal never reads the file. Keep the
   previous release's
   workspace and bundle, and the base bundle of each kept gap release,
   until the go-live checks pass.
9. **Roll back** to the previous signed release, upstream or gap, from its
   own retained workspace and bundle, as ADR-0009 §5 step 7 says. Rolling
   back to a gap release runs as the promotion its retained workspace
   recorded (step 8), when its `dist/gap-promotion.txt` still holds there;
   otherwise declare it with its own identity (`GAP_PROMOTE_IDENTITY`).
10. **Retire a gap workspace** that is abandoned (its release was given up
    or rolled back) or superseded, and will not be used for rollback: from
    that workspace run `rm -f dist/gap-promotion.txt` and confirm
    `ls dist/gap-promotion.txt` reports no such file. No later undeclared
    stage from it can then run as its promotion; a stage from it needs
    `GAP_PROMOTE_IDENTITY` again (step 9 if it is ever used for a rollback
    after all). Keep the retained rollback workspace's file until that
    workspace is retired too.

Bootstrap refuses a bundle that declares `origin: gap` unless
`SNEAKERNET_GAP_TRUSTED_PUB` is set (never trust on first use), and every
guard refuses gap fields on an `origin: upstream` MANIFEST.

## Not in this increment

- Layer replacement for `src/` and image-bound `scripts/` changes, with the
  base-layer self-check at `diff_id` level (increment 2). Prerequisite: the
  ten licence-tracked paths under `src/mainframe_rag/webui/static/vendor/`
  (htmx, IBM Plex and their licence and SHA256SUMS files) are refused, or
  run through the licence check on the tagged tree, before replacement is
  enabled. Today `src/` is wholly refused, so they cannot ship unchecked.
- The generated gate table, the upstream-after-gap check (D8: record-only
  until the first upstream import after a gap release), the promotion
  attestation (D9) and a Kind lane that rehearses a gap bundle.
