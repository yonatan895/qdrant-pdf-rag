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
  released from the gap: report it upstream (D2).
- `release.sh classify` lists every changed path with its verdict.

## Before the first release

- The installed release (the workspace the guard verifies) contains
  `scripts/gap/release.sh`. Every command below runs from that workspace,
  so a change to these tools takes effect one release later.
- Both anchors are plain files in `/work/anchors/` and are **exported in the
  process environment**: `release.sh` ignores `airgap.env`.

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
  included, to be blocked from it.
- **Fallback, without Vault.** Generate a passphrase-encrypted RSA-3072 key
  in the zero-egress signing pod
  ([`deploy/workspace/sign.template.yaml`](../../deploy/workspace/sign.template.yaml):
  no `/work`, no Secret, no token, no egress, a memory-only
  `/run/mfr-session`; render it with `oc process --local -f … | oc apply -f -`
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
     (egress to Vault and DNS only); pipe in the maintainer's Vault token for
     the one call and delete the pod.
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
   to the rehearsal namespace first. Before deploying, run

   ```sh
   sh scripts/gap/release.sh rehearse-check --rehearsal-namespace <ns> --prod-namespace <live ns> \
     --rehearsal-registry <rehearsal INTERNAL_REGISTRY> --prod-registry <live INTERNAL_REGISTRY> \
     --candidate-manifest <bundle>/MANIFEST.txt --installed-manifest <installed>/dist/MANIFEST.txt --kubectl oc
   ```

   It refuses the installed release's namespace, a non-empty namespace, and
   a changed third-party `*_digest` on a shared registry path (load pushes
   third-party images under fixed tags). Afterwards uninstall both
   releases, delete the namespace's PVCs and Jobs and confirm it is empty.
8. **Promote** only when every required row of the record is `PASS` or
   `INHERITED` and the approver signed off: bootstrap the same signed
   members (the sha256 of `SHA256SUMS` equals the record's identity) into a
   fresh workspace and deploy as usual. Keep the previous release's
   workspace and bundle, and the base bundle of each kept gap release,
   until the go-live checks pass.
9. **Roll back** to the previous signed release, upstream or gap, from its
   own retained workspace and bundle, as ADR-0009 §5 step 7 says.

Bootstrap refuses a bundle that declares `origin: gap` unless
`SNEAKERNET_GAP_TRUSTED_PUB` is set (never trust on first use), and every
guard refuses gap fields on an `origin: upstream` MANIFEST.

## Not in this increment

- Layer replacement for `src/` and image-bound `scripts/` changes, with the
  base-layer self-check at `diff_id` level (increment 2).
- The generated gate table, the upstream-after-gap check (D8: record-only
  until the first upstream import after a gap release), the promotion
  attestation (D9) and a Kind lane that rehearses a gap bundle.
