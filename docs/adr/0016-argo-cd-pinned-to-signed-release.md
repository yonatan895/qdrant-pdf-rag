# ADR-0016: Deployment through the platform's Argo CD, pinned to the signed release commit

- **Status:** proposed. The maintainer accepts this record by merging it.
  Issue [#1197](https://github.com/yonatan895/qdrant-pdf-rag/issues/1197);
  tracker #1191; prerequisite pull requests tracked in #1381 (D11). The
  coordinator's recommendation (central controller, one set of Applications
  per namespace, manual sync, prune off, pilot on a new namespace) was
  accepted in principle on 2026-11-25. Maintainer decisions 2026-11-25 (in
  session):
  1. **Where deployed settings live (D6).** Each Application's
     `targetRevision` is pinned to the signed release commit already in the
     fork, and the per-namespace settings live in the Application spec. The
     Application manifests themselves are kept as non-secret site
     configuration in the fork's site layer, `gap/config/<cluster>/<namespace>/`
     (ADR-0009 §3, Q2), so each namespace's settings stay versioned. The
     bastion applies them.
  2. **The weaker gate is accepted for the dev pilot (D6, G1),** with two
     measures and one open question: namespace RBAC limits who may edit
     Applications; the post-sync verify step checks that `targetRevision`
     equals the recorded signed sha; and whether the platform project can
     enforce commit-signature verification stays an open site question
     (input 10). Production
     adoption needs that question answered (D10).
  3. **The vendored Qdrant chart** is also kept as an unpacked directory
     beside the archive, in its own prerequisite pull request, with a check
     that the directory is byte-identical to the archive's contents (D11
     item 1).
  4. **The Qdrant key Secret is operator-created** by a bastion task;
     rotation is that task plus a restart (D4). D4 notes the fit with the
     site Vault (#1209).
  5. **The pilot restores its corpus by upload through a port-forward**
     (D10). The pilot doubles as the first cross-cluster restore rehearsal
     (#360, #272).
  6. **The Kind lane with a disposable Argo CD** is added, non-gating until
     the pilot passes (D11).
  7. **The own-instance fallback (D1)** is recorded, not approved.

  Accepting this record deploys nothing and changes no default:
  `airgap:deploy` stays the only deploy path outside the pilot namespace
  until the pilot's exit criteria (D10) are recorded. It **amends** [ADR-0009](0009-gap-fork.md) and two
  passages of [`docs/deploy.md`](../deploy.md), as "Amendments" lists. Site
  names stay off GitHub: this record writes `<cluster>` and `<namespace>`.
- **Context:**
  - Deployment today is `scripts/airgap/deploy.sh`: two Helm releases per
    namespace (the vendored Qdrant chart, release `$QDRANT_RELEASE`, default
    `qdrant`; and `charts/mainframe-rag`, release `mainframe-rag`), run on the
    bastion from a verified signed release with the operator's session token.
  - A second dev cluster now exists and production will be a third place. One
    untracked `airgap.env` per bastion does not say which release and which
    configuration each namespace runs.
  - #1197 named four obstacles: no controller of our own, the Qdrant chart's
    `lookup`, imperative day-2 steps, and the signed bundle as trust gate.
    The site facts below remove the first; this record decides the other three.

## Facts, assumptions and site inputs

**Site facts (maintainer, 2026-11-25).**

- S1. Red Hat OpenShift GitOps operator 1.19.0 is installed on both dev
  clusters. A central shared Argo CD instance manages tenant namespaces by
  sync impersonation of a ServiceAccount the platform pre-seeds in each
  namespace (the *client SA*). The client SA can create every kind our charts
  render, including NetworkPolicy and Route.
- S2. In our namespaces we can create Applications, AppProjects and ArgoCDs.
- S3. Resource tracking is the instance default, LABEL. We cannot change
  `argocd-cm` (no instance-level ignore rules, tracking method or health
  customisation). Everything we set goes in the Application spec, including
  Helm options.
- S4. The second dev cluster has two new namespaces with the current
  namespace's quotas: one is the pilot namespace, the other is free.
- S5. Each cluster has its own Quay (same image digests, different host).
  Each bastion reaches only its own cluster, with `oc` and the operator's
  key, and can push a verified-release commit or tag to the air-gapped GitLab
  fork. D6 decides not to use the push.
- S6. Transfers between clusters use the manual transfer folder that carries
  release bundles.

**Observed in this tree (origin/main 6bca43f0, Helm v4.3.0 pin).**

- O1. `helm template` of the Qdrant chart with the repository values
  (`apiKey: true`, `readOnlyApiKey: true`) gives different keys and a
  different `checksum/secret` pod annotation on every render: `lookup` is
  empty without a cluster and the chart falls back to `randAlphaNum 32`.
  `docs/deploy.md` already warns against applying such a render.
- O2. The chart's `valueFrom.secretKeyRef` form also reads the Secret through
  `lookup`: the render holds a `<release>-apikey` Secret with **empty data**,
  still mounted by the StatefulSet.
- O3. With `apiKey: false`, `readOnlyApiKey: false` and the chart's `env`
  list (`QDRANT__SERVICE__API_KEY`, `QDRANT__SERVICE__READ_ONLY_API_KEY`,
  each `valueFrom.secretKeyRef`), the render holds no Secret, no
  `checksum/secret`, and is byte-identical across renders.
- O4. The Qdrant chart puts `app.kubernetes.io/instance: <release name>` in
  its selector labels: the StatefulSet selector (immutable), the pod template,
  both Services' selectors and the PodDisruptionBudget. Its
  `volumeClaimTemplates` carry only `app:`. The chart's ServiceMonitor (off in our
  values) selects Services on the full label set, instance included.
- O5. `charts/mainframe-rag` uses only `app: <component>` labels and
  selectors, no `app.kubernetes.io/*` label, no `lookup`, no Helm hook, no
  `.Capabilities` and no Secret (no template holds `kind: Secret`). Three PVCs carry
  `helm.sh/resource-policy: keep`. The Qdrant chart's only hooks are two
  `helm.sh/hook: test` objects.
- O6. `deploy.sh` does more than apply. Before: bundle, chart and checkout
  checks, registry digests, Secret presence, ownership, Route exposure (it
  deletes a disabled or incompatible Route), stored layout and volume sizes.
  After: rollout waits, running-image digests, Route verification, and
  provisioning of the Open WebUI Pipe and prompts.
- O7. The F9 workspace contract forbids `app` and `app.kubernetes.io/*`
  labels on bastion, dev and runner pods (`deploy/workspace/contract.json`).
- O8. The vendored chart is a packed archive, `charts/qdrant-1.19.2.tgz`,
  bound to the bundle by `chart_sha256` in the signed MANIFEST.

**Assumptions. None has been seen at the site; each is a pilot check.**
"Documented" means the Argo CD *stable* documentation read on 2026-11-25,
which may not be the site's version. "Recalled" means not confirmed in any
source read for this record.

- A1. *Documented:* an Application outside the control-plane namespace is
  named by namespace and name together, and label values are truncated to 63
  characters. *Recalled:* the value is exactly
  `<application namespace>_<application name>`, and Argo CD writes the
  label on each object's own metadata only, never into selectors or pod
  templates.
- A2. *Documented:* sync impersonation is switched on for the whole instance,
  and the ServiceAccount is bound in the AppProject
  (`destinationServiceAccounts`); the examples keep AppProjects in the
  control-plane namespace. *Recalled:* an AppProject we create in our own
  namespace is read only by an instance of our own.
- A3. *Documented:* Argo CD renders with `helm template` only (so O1 and O2
  apply), skips unsupported hooks such as `test`, and reports a chart that
  uses `randAlphaNum` as always out of sync. *Recalled:* it applies
  client-side with a three-way diff by default.
- A4. *Recalled:* verifying commit signatures needs key configuration we
  cannot set (S3). The stable documentation deprecates GnuPG verification for
  a "source integrity verification" feature; whether the site's version has
  either, and whether the platform project can enforce it, is open site
  input 10.
- A5. *Documented:* the health check for the Application kind was removed and
  comes back only through `argocd-cm`, so ordering between Applications by
  sync wave is not available to us (S3).
- A6. The Helm that the site's Argo CD bundles renders both charts to the
  same objects as the pinned Helm v4.3.0. *Documented:* Argo CD bundles its
  own Helm.
- A7. *Recalled:* Qdrant reads both keys from the two environment variables
  of O3.
- A8. *Recalled:* Kubernetes copies a StatefulSet's selector labels onto the
  PVCs it creates from `volumeClaimTemplates`, and those PVCs have no owner
  reference. With O4, the Qdrant PVCs then carry
  `app.kubernetes.io/instance: <release name>`.
- A9. *Documented:* inside one sync, objects are ordered by phase, wave, kind
  and name. *Recalled:* the kind order puts NetworkPolicy before workloads,
  as Helm's does.
- A10. Nulls in an Application's inline Helm values may be dropped. Not
  confirmed either way; D6 avoids depending on it.

**Open site inputs** are listed at the end.

## Decision

1. **D1, the central controller.** Deployments use the platform's central Argo
   CD (S1). We run no Argo CD of our own.
   - *Why not our own instance:* its controller would hold a standing
     ServiceAccount token with admin rights in the deployment namespace, in a
     namespace we administer. ADR-0009 §2 and §8 keep deploy authority in a
     memory-only operator session and give no pod of ours RBAC. Self-granting
     that admin right reverses both, and adds a controller, repo server and
     cache to our quota and to our upgrades.
   - *Fallback:* if the central instance cannot serve us (it does not watch
     our namespaces, or cannot read the fork), an instance of our own in the
     tools namespace (D9), with annotation tracking and no Route. It is
     recorded, not approved (maintainer decision 7): it needs a new
     maintainer decision first, because of the standing credential.
2. **D2, two Applications per namespace, no app-of-apps.** Each deployment
   namespace holds Applications `mfr-qdrant` and `mfr-app`, created in that
   namespace.
   - `spec.source.helm.releaseName` is today's release name
     (`$QDRANT_RELEASE`, and `mainframe-rag`), so object names, selectors,
     the StatefulSet identity and the PVC names are the ones Helm produced. An
     existing namespace can migrate without recreating anything.
   - Application names never equal a release name (D3).
   - **Order** is a runbook step, not a wave (A5): sync `mfr-qdrant`, wait
     for the StatefulSet rollout, then sync `mfr-app`. This is the order of
     `deploy.sh`.
   - **NetworkPolicies** need no annotation if A9 holds: inside one sync Argo
     CD then applies NetworkPolicy before workloads, as Helm does today. The
     Kind lane asserts it (D11). If A9 fails, the chart gains a sync-wave
     annotation on its NetworkPolicies. Qdrant has no NetworkPolicy today; that is
     unchanged.
   - *App-of-apps rejected for the pilot:* Argo CD would read the children,
     and so the pin (D6), from a fork branch, so a merge in the fork, not
     the bastion's checks against the verified bundle, would move the
     deployment. It also adds a tracked layer for two children. Under D6 the
     manifests are versioned in the fork too, but only the bastion applies
     them, after those checks.
3. **D3, living with LABEL tracking.**
   - Argo CD overwrites `app.kubernetes.io/instance` on the top-level
     metadata of each Qdrant object. Selectors, pod templates and pods keep
     the release name (A1), so nothing immutable changes. Our own chart is
     unaffected (O5).
   - **Rule:** the tracking value must never equal a Helm release name.
     Otherwise Argo CD would count the Qdrant data and snapshot PVCs (A8) as
     its own objects that are missing from git, and offer them for pruning.
     Under A1 the value contains `_`, which no release name can. The names
     `mfr-qdrant` and `mfr-app` keep the rule under the D1 fallback too.
     `len(<namespace>) + 1 + len(<application name>)` must be at most 63
     (open site input 5).
   - The Qdrant ServiceMonitor stays off under Argo CD: its selector would
     stop matching the relabelled Service (O4).
   - **Objects created outside Argo CD never carry the tracking label:**
     operator-created Secrets, the ingest Job and its work PVC, maintenance
     Jobs and pods, Open WebUI copy pods, and the bastion (O7). Day-2 tasks
     render from the chart or from fixed manifests; they never copy the
     metadata of a live Argo-managed object. Staging collections, aliases and
     snapshots are Qdrant state, not Kubernetes objects, and are invisible to
     Argo CD.
   - **Prune and delete.** Pilot: automated sync off, self-heal off, prune
     off, and no `resources-finalizer` on either Application, so deleting an
     Application deletes nothing it deployed. Prune off is not neutral: Helm
     removes objects a release drops, and `deploy.sh` removes a disabled or
     incompatible Route before the release. Under Argo CD the bastion's
     pre-sync step keeps the Route removal (D6), and its post-sync step lists
     every object Argo CD reports as needing pruning; the operator deletes
     each by name. Later: prune may be chosen per sync, never automated, and
     only after the three kept PVCs carry
     `argocd.argoproj.io/sync-options: Prune=false,Delete=false` (Argo CD
     ignores Helm's `resource-policy`).
4. **D4, no `lookup` in the Argo render; the Qdrant key Secret is
   pre-created.** Pre-creating the Secret alone is not enough (O1, O2): the
   chart would still render a Secret of that name. The Argo render therefore
   uses the O3 form, in which the chart renders no Secret.
   - The Secret keeps today's name and keys, `<QDRANT_RELEASE>-apikey` with
     `api-key` and `read-only-api-key`, so the application chart, ingest
     and the operator scripts read it unchanged. A migrating namespace keeps
     its existing Secret and keys.
   - A bastion task creates it with the operator's session before the first
     sync: two random 32-character keys, never printed, no
     `app.kubernetes.io/*` label, and it refuses to overwrite.
   - **Rotation** is that task with an explicit rotate flag in a maintenance
     window, then a restart of Qdrant, the agent and any ingest consumer, then
     the existing revoked-key probe (old key 401). No pod annotation triggers
     the restart any more. Maintainer decision 4 accepts this explicit
     compatibility change.
   - **Fit with the site Vault (#1209).** Nothing here depends on who writes
     the Secret. If Vault comes to deliver credentials, it delivers this
     Secret under the same name and keys, the task's create step gives way to
     it, and rotation becomes a Vault rotation followed by the same restart
     and probe. The charts and the Applications do not change.
   - `deploy.sh` keeps the chart-managed form by default. The O3 form is an
     explicit opt-in there, so one input can produce both renders (D10).
   - Nothing else depends on `lookup` or a hook (O5). The Applications set
     `skipTests: true`.
5. **D5, ignore rules live in each Application.** Each Application sets
   `ignoreDifferences` and `RespectIgnoreDifferences=true`, and keeps
   client-side apply (`deploy.sh` already passes `--server-side=false` for the
     application release; the Qdrant release passes no apply flag, so the
     migration in D10 checks the Qdrant objects' field managers).
   - The rule list is fixed by the pilot's observed diff, not guessed. Each
     entry names the object, the path and who mutates it. A diff nobody can
     attribute blocks the exit criteria.
   - Candidates to confirm or strike:
     - ServiceAccount `imagePullSecrets` and `secrets` (OpenShift);
     - Route `rag-agent` `spec.host` and operator-chosen certificate
       fields (the host is deliberately not in git), and Route `status`;
     - StatefulSet `volumeClaimTemplates[]` `status` and
       `spec.volumeMode`;
     - PVC `spec.volumeName` and defaulted storage fields;
     - service-CA annotations on the two Services that request a serving
       certificate.
   - With a three-way diff most server defaults produce no difference (A3),
     so the expected list is short.
6. **D6, the signed bundle stays the gate; Argo CD is pinned to the signed
   commit; the Application manifests are site configuration.**
   `targetRevision` of both Applications is the full 40-hex **MANIFEST sha**
   of the verified bundle, never a branch or a tag.
   - That commit is in the fork's history after the release import (ADR-0009
     §4), or is the fork commit itself for a gap release. Git names a commit
     by its content, so Argo CD renders exactly the tree the signature covers.
   - Values come from two places. The repository's own values files (the
     Qdrant OpenShift overlay, the chart defaults) are read from the signed
     tree as `valueFiles`; the overlay's nulls, which remove the chart's
     default user and group IDs for `restricted-v2`, therefore never pass
     through the Application (A10). The per-namespace overrides (image
     repositories on that cluster's Quay with the verified digests, and the
     namespace's site configuration) are written into the Application spec
     as inline Helm values. The generator emits no null, and a test pins
     that.
   - **The Application manifests are versioned site configuration**
     (maintainer decision 1). The prepare step generates the two manifests.
     They are committed under `gap/config/<cluster>/<namespace>/`, beside
     that namespace's `airgap.env` (non-secret keys only), through the
     fork's ordinary reviewed merge request for the site layer (ADR-0009 §3
     and §6). They hold Secret names but no Secret value (D7), image
     references by digest, and the namespace's non-secret settings; they hold
     no rendered chart object. This record reads them as
     non-secret site configuration under ADR-0009 Q2 (decision 1; confirmed
     by merging), not as the generated deployment artifacts AGENTS.md keeps
     out of git; rendered chart output,
     bundles and images stay out.
   - **Argo CD never reads `gap/config/`.** It reads the charts and values
     files at the MANIFEST sha. The bastion keeps read-only access to GitLab:
     it reads the manifests and `airgap.env` at a fork commit it records and
     applies the manifests with the operator's session. It commits and
     pushes nothing (S5).
   - **The committed manifest never vouches for itself.** Before applying,
     the bastion regenerates both manifests from the verified bundle and the
     site configuration at the same fork commit, and refuses unless the committed files
     equal its output byte for byte and each `targetRevision` equals the
     verified bundle's MANIFEST sha. The verify step compares the live
     Application to the MANIFEST sha recorded from the verified bundle, never
     to the committed file alone.
   - Three costs. The signed commit must already be in the fork: the
     ADR-0009 §4 import of that release is merged first, and the prepare step
     refuses a sha the fork does not hold. Argo CD reads the charts at that
     commit, so the pilot release must already contain D11 items 1 and 5.
     And every release, and every change to a namespace's settings, is a
     site-layer merge request per namespace before the apply.
   - **Flow, per namespace:**
     0. The release is imported into the fork (ADR-0009 §4), once for all
        clusters.
     1. Bootstrap the bundle on that cluster's bastion against the out-of-band
        anchor (ADR-0009 §1 and §8, unchanged).
     2. `airgap:load` into that cluster's Quay.
     3. `airgap:config -- check` and `airgap:validate`.
     4. **Prepare** (new task): every pre-apply check of O6, the renders, the
        Qdrant render check, then the two Application manifests into
        `dist/`.
     5. The operator commits the two manifests to
        `gap/config/<cluster>/<namespace>/` through the fork's reviewed merge
        request (a change that leaves them equal needs none). The bastion
        pushes nothing. How the files reach the merge request follows what
        ADR-0009 §2 allows to leave the bastion; the route is a pilot
        procedure. Integrity does not depend on it, because step 6 derives
        the manifests again.
     6. On the bastion, run the prepare task again: regenerate both
        manifests from the verified bundle and the `airgap.env` at the merged
        fork commit, require the committed
        manifests at that commit to equal them byte for byte and to pin the
        MANIFEST sha, then apply the two Applications with the operator's
        session. Step 4's output is a proposal; this check decides.
     7. Sync by hand in D2's order.
     8. **Verify** (new task): each Application reports the MANIFEST sha as its
        `targetRevision` and its synced revision, and its live spec equals the
        manifest applied in step 6; it lists who may edit Applications in the
        namespace; then every post-apply step of O6 and the smoke.
     9. Record the MANIFEST sha, the Application manifests' SHA-256 and the
        fork commit that holds them in `gap/records/`.
   - **What stops an unverified commit:** the pin is a content hash that only
     the prepare step writes, from a verified bundle; the bastion applies only
     a committed manifest that equals the manifests regenerated at the merged
     commit; sync is manual; the
     verify step refuses any other revision or spec. Namespace RBAC limits
     who may edit Applications in a deployment namespace to the operators who
     run this flow. Whether the cluster's built-in namespace roles already
     grant Application rights is open site input 11; if they do, the limit
     needs the platform. We ask the platform to limit the project's
     `sourceRepos` to the fork and its destinations to our namespaces (A2);
     that narrows the reach of a mistake but is not our control.
   - **This is weaker than today in one respect, stated plainly.** Today the
     executing guard (ADR-0009 F1) refuses to run unverified code. Under Argo
     CD the controller applies, and anyone who can edit the Application in
     the namespace can point it elsewhere. That is the same group that can run
     `helm` there today, but the refusal moves from before the act to the
     verify step after it. The maintainer accepts this for the
     dev pilot with the measures above (decision 2). Commit-signature
     verification would close it; whether the platform project can enforce
     it is open site input 10 (A4), and production adoption waits for that
     answer (D10).
   - **Two writers are refused.** `deploy.sh` refuses a namespace that holds
     these Applications, and the prepare step refuses a namespace that still
     holds Helm release records, outside the one migration step (D10).
7. **D7, Secrets stay out of git and out of Argo CD.** Operators create every
   Secret with the bastion (or the site Vault, #1209) and the charts reference
   it by name. After D4 neither render contains a Secret; a test pins that. We
   ask the platform to deny the Secret kind in the project. The prepare step
   checks presence and key names only, as `deploy.sh` does.
8. **D8, day-2 operations stay imperative.** Ingest, alias publish, retire,
   rollback of data, snapshot and restore, Open WebUI copy-out and restore,
   Pipe and prompt provisioning, and the site probes run as bastion tasks with
   the operator's session, outside the Applications.
   - They coexist with sync because of three facts: their objects carry no
     tracking label (D3), nothing prunes (D3), and nothing self-heals, so Argo
     CD writes only when an operator syncs.
   - A day-2 task never edits an Argo-managed object. A change to one goes
     through site configuration and a new prepare.
   - **Release rollback** is the same flow from the previous release's retained
     workspace (ADR-0009 §5 step 7): its MANIFEST sha and its values, with
     its manifests committed back to `gap/config/` by D6's step 5. Argo
     CD's own history rollback is not used, because it skips the prepare and
     verify steps.
9. **D9, one bastion per cluster, in a tools namespace.** A bastion reaches
   only its own cluster (S5), so each cluster has its own, in a tools
   namespace that holds no deployment. On the second cluster this is the free
   namespace (S4). The first cluster's bastion moves out of its deployment
   namespace after the pilot.
   - **Rights:** push to that cluster's Quay; read on the GitLab fork, no push (D6);
     through the operator's session, namespace admin in the deployment
     namespaces for Secrets, the two Applications and day-2 tasks. Its own
     ServiceAccount keeps no RBAC and no token (ADR-0009 F9). It no longer applies
     chart objects.
   - **Independent verification:** each bastion receives the same tarball by
     the transfer folder, and its anchor by the separate out-of-band route,
     into `/work/anchors/` outside any checkout. An anchor is never copied
     from the other bastion or fetched from GitLab. Each runs the same
     bootstrap. The two records must show the same MANIFEST sha and the same
     image digests; a difference stops both.
10. **D10, pilot and exit criteria.** The pilot is a fresh replica in the new
    namespace on the second dev cluster.
    - **Corpus by snapshot restore, a day-2 step outside Argo CD.**
      - Source: the current dev namespace. Snapshot the published data
        collection and its `<physical>__completions` pair on every peer,
        after recording the aliases, as step 1 of the
        [upgrade and recovery runbook](../install_and_ops.md#upgrade-and-recovery-runbook-issue-272)
        does. Which peers' files the restore uses is part of the rehearsal.
      - Transport: the manual transfer folder (S6).
      - Integrity does not depend on the transport. Before: a SHA-256 manifest
        of every snapshot file travels with them and the receiving bastion
        checks it before any upload. After: collection and point counts equal
        the source's record on every peer, with the collection policy's replica
        placement; the publication receipt and the content seal
        verify read-only; the serving gate passes; smoke answers.
      - Restore: the receiving bastion uploads through a port-forward to
        Qdrant's snapshot upload endpoint with the writer key, under the
        original physical names. The serving alias and both immutable build
        aliases are then created together, so data, completions and
        publication controls arrive as one generation. A data-only restore
        must be refused, as the lifecycle lane already proves.
      - **Risk:** restore on a different cluster and across three peers has
        never been rehearsed. The #272 runbook (step 2) leaves it to the site,
        and #360 closed with distributed restore still unproven. The pilot is
        the first cross-cluster restore rehearsal (maintainer decision 5). If
        the upload through a port-forward cannot carry the size, the fallback
        is the maintenance Job that deployment policy already names.
      - Snapshots hold manual text: they are stored like the corpus and never
        enter git.
    - **Exit criteria, each recorded with evidence:**
      1. A1 to A10 observed at the site, or the decision that depends on one
         reopened.
      2. Render equality: for the same inputs, the objects Argo CD renders
         equal the `deploy.sh` render with the O3 opt-in. The only allowed
         difference is the tracking label. The record names both Helm
         versions.
      3. First sync to a healthy stack; a second sync with no change is a
         no-op; the D5 list is final and every entry attributed.
      4. The restore checks above pass.
      5. An ordinary ingest and publish after the first sync leave both
         Applications in sync, and the next sync touches nothing of theirs.
      6. A full release cycle under Argo CD: upgrade to the next signed
         release, then roll back (D8), with running-image digests verified
         both times.
      7. A Qdrant key rotation (D4).
      8. No object outside the renders carries the tracking value; the Qdrant
         PVCs are not offered for pruning.
    - **Then the existing dev namespace** migrates in a window, with its own
      approved issue: snapshot; switch Qdrant to the O3 form, keeping the
      Secret; delete the Helm release records (never `helm uninstall`);
      record the Qdrant objects' field managers (D5); prepare, commit,
      apply, sync. The expected changes are the tracking label and one
      rolling restart of Qdrant. Any other difference stops the migration.
    - **Production** is a fresh install on new namespaces (ADR-0009 §2) by the
      same flow, after both dev namespaces have run one release cycle, and
      only after open site input 10 is answered and recorded (maintainer
      decision 2). If the platform project cannot enforce commit-signature
      verification, production adoption needs a new maintainer decision on
      the post-sync gate.
11. **D11, repository work and verification.** Each item is its own pull
    request under the packaging/deploy row of
    [live-stack](../live-stack.md#verification-minimums) unless noted.
    1. The Qdrant chart as a directory Argo CD can read (O8): a pinned
       unpacked copy beside the archive, in its own pull request, with a
       check that the directory is byte-identical to the archive's contents
       (the same files, the same bytes, nothing extra). Vendoring policy
       applies (pin, licence, notice). Maintainer decision 3.
    2. The O3 opt-in in `deploy.sh` and `check_qdrant_render.py`, the key
       task of D4, and `tests/test_qdrant_auth.py` run against the O3 form
       (A7). The default is unchanged.
    3. Per-namespace site configuration: `gap/config/<cluster>/<namespace>/`
       as the location of that namespace's `airgap.env` and its two
       Application manifests (ADR-0009 §3 and §9).
    4. The prepare and verify tasks (D6): `deploy.sh` split so both paths
       share every check of O6, the Application generator, and the two-writer
       refusals. The stored-layout and volume-size checks stop reading Helm
       release records to recognise a fresh install. The prepare step refuses
       an Application name equal to `$QDRANT_RELEASE` or `mainframe-rag`,
       and, when run again at the merged commit (D6 step 6), committed
       manifests that differ from its output or pin another sha; the verify step compares against the verified bundle's MANIFEST
       sha and lists who may edit Applications in the namespace.
       A test bounds the size of each rendered object: Argo CD's client-side
       apply stores a last-applied copy that Helm does not, and the Open
       WebUI Pipe ConfigMap is the large one.
    5. Kept-PVC annotations (D3) in `charts/mainframe-rag`.
    6. The bastion in a tools namespace (D9): the workspace templates, the
       contract and the isolation proof.
    7. A snapshot seed and restore task with the D10 checks (publication/retirement
       lifecycle row as well).
    8. Owner documents: `docs/deploy.md` and `docs/install_and_ops.md`
       (prose-only row; may ride with the item whose behaviour it
       documents).
    - **Hermetic tier:** render and generator tests in `qa:check`;
      `airgap:dryrun` prints both Applications with zero placeholders.
    - **Kind lane:** a disposable Argo CD in Kind syncing the charts from a
      git server inside the cluster is acceptable and cheap enough: a core
      install (controller, repo server, cache; no UI), configured like the
      site (LABEL tracking, an Application outside the control-plane
      namespace, sync impersonation). It adds a few minutes and its pinned
      images to connected CI only; nothing enters the release bundle. It
      proves A1, A3, the NetworkPolicy order, the no-op second sync and
      criterion 8 before the site does. It runs when charts or the Argo tasks
      change, and is non-gating, not an acceptance producer, until the
      pilot passes (maintainer decision 6). Its
      Argo CD version follows the site's (open input 1).
    - This record itself takes the prose row.

## Guarantees and what establishes them

| Guarantee | State that establishes it | Who can change that state | Valid for | What forbids the counterexample |
|---|---|---|---|---|
| G1. Argo CD applies only a signed release's tree | `targetRevision` is the verified MANIFEST sha | Users with Application edit rights in the namespace (limited by namespace RBAC, input 11); fork committers to `gap/config/` (the committed manifest); the fork cannot change what a sha names | Until the Application is edited | The bastion applies only a committed manifest equal to the manifests regenerated at the merged commit (D6 step 6); the verify step compares against the verified bundle's sha and refuses another revision or spec. Not prevented before the sync (D6); accepted for the dev pilot only, production waits for input 10 |
| G2. Argo CD never deletes data | Prune off, no finalizer, tracking value unequal to any release name | Namespace admins; the platform (instance settings) | Until prune is chosen for a sync | Exit criterion 8; the Kind lane; kept-PVC annotations before any prune |
| G3. Qdrant keys never change on a sync | The render holds no Secret (O3) | A release, through the chart or values | Standing | The no-Secret render test; `check_qdrant_render.py` |
| G4. No Secret value is in git or in an Application | Secrets are operator-created and referenced by name | Operators | Standing | The no-Secret render test; the prepare step reads names only; the project denies the kind (requested) |
| G5. Day-2 objects are neither adopted nor pruned | They carry no tracking label; nothing prunes or self-heals | Day-2 task code, through a release | Standing | Exit criteria 5 and 8; the F9 label contract |
| G6. Both clusters run the same release | Each bastion verifies the same bundle against its own anchor copy | The connected side (the bundle); the anchor's custodian | Per release | The two records' MANIFEST sha and digests are compared |

## Amendments

Scope of each row: the Decision, §1 and §5 rows apply per namespace, in a
namespace deployed under Argo CD (the pilot namespace first, any other only
after the pilot's exit criteria in D10 are recorded); the §2 and §8 rows
apply per cluster once its bastion is in a tools namespace (D9); the §3 and
§9 rows change the site layout when D11 item 3 lands, for `deploy.sh`
namespaces too; the `docs/deploy.md` rows apply under Argo CD only.
Elsewhere the amended text stands.

| Record | Was | Now |
|---|---|---|
| ADR-0009 Decision and §1, "production deploys only signed releases" | The executing guard covers the apply | Unchanged for every script. For the apply itself, the MANIFEST-sha pin, the committed-manifest check and the verify step (D6, G1) |
| ADR-0009 §2, bastion row and "only the bastion holds production authority" | The bastion is in the production namespace; no other identity writes workloads | One bastion per cluster in a tools namespace (D9). The platform's controller, as the client SA, also writes workload objects; it is platform-owned |
| ADR-0009 §2 and §8, D-10 | `/work` is in the production namespace | In the tools namespace |
| ADR-0009 §3, `gap/config/` | One site `airgap.env` | One directory per cluster and namespace, holding its `airgap.env` and its two Application manifests (non-secret site configuration, Q2; maintainer decision 1) |
| ADR-0009 §5 steps 5 to 7 | Rehearse, promote and roll back with `airgap:deploy` | The D6 flow in a namespace under Argo CD; `airgap:deploy` elsewhere |
| ADR-0009 §9 | The current surface | Adds the new tasks, keys and the per-namespace path |
| `docs/deploy.md`, Qdrant key rotation | Chart-native: delete the Secret and redeploy | Under Argo CD: the key task and an explicit restart (D4) |
| `docs/deploy.md`, snapshot administration in a maintenance Job | Snapshot administration runs in a temporary maintenance Job | For the pilot restore (D10), upload from the bastion through a port-forward; the maintenance Job stays the fallback and the path everywhere else |

Hard boundaries are unchanged: no Qdrant Route, no secrets or snapshots in
git, no build in the gap, no runtime download. The Application manifests in
`gap/config/` are read as site configuration under ADR-0009 Q2 (maintainer
decision 1); rendered chart output stays out of git.

## Considered options

- **Stay with `deploy.sh` only.** No shared view of what runs where across
  clusters, and each bastion keeps apply rights. Kept as the path until D10.
- **Our own Argo CD instance.** Rejected in D1; it is the recorded fallback,
  not approved (maintainer decision 7).
- **One Application with an umbrella chart, or app-of-apps.** Rejected in D2.
  An umbrella chart would also change the release names.
- **Application manifests only in the bastion's `dist/` and in the cluster.**
  Nothing in git would say which settings a namespace runs. Rejected by
  maintainer decision 1 in favour of committing them under `gap/config/`.
- **A deploy commit or tag in the fork that carries rendered values**, with
  the Application targeting it. Rejected by maintainer decision 1: it gives
  the bastion a write credential to GitLab, and makes the deployed commit
  differ from the signed one, so a tree-difference proof and tag protection
  would become part of the gate. Decision 1 keeps `targetRevision` on the
  signed sha and versions the settings beside it instead.
- **Keep the chart-managed key Secret and ignore its data.** Argo CD would
  manage a Secret (against D7), and one missed rule rotates the keys.
- **The Qdrant chart from Quay as an OCI artifact.** Needs registry
  credentials inside the central instance and pins a tag, not signed bytes.
- **Automated sync or prune in the pilot.** Rejected: a sync must follow a
  verified prepare, and prune can reach data.
- **One bastion for both clusters.** Rejected: impractical with per-cluster
  access (S5), and it would hold two clusters' credentials in one pod.
- **Snapshot transport, later and not now:** GitLab's generic package
  registry on the fork, since both bastions reach GitLab anyway. It needs the
  registry enabled, a file-size limit above the snapshot size, and a project
  token with write for the source bastion and read for the other. It would
  hold manual text in GitLab, which needs its own decision.
- **Rejected for snapshot transport:** any Qdrant reachability between
  clusters (no Qdrant Route is a hard boundary), and Qdrant recovering from a
  URL (new egress from Qdrant).
- **Ingest instead of restore for the pilot.** Rejected by the maintainer:
  the pilot should be a replica, and the restore rehearsal is owed anyway.

## Consequences

- The operator gains one place per cluster that shows the release and values
  each namespace runs, and a diff before each sync.
- Each namespace's settings, including its Application manifests, are
  versioned and reviewed in the fork's site layer. Every release adds one
  site-layer merge request per namespace before the apply.
- A deploy becomes prepare, commit, apply, sync, verify where there was one
  step.
  The checks are the same; their order around the apply is now a procedure.
- The trust gate depends on a step after the sync (G1). Until the platform
  enforces signature verification, a skipped verify step is the residual
  risk; it is accepted for the dev pilot only.
- Argo CD's bundled Helm becomes a second renderer to keep equal with the
  pin (A6); a platform upgrade of the operator can change it without us.
- Two bastions mean two anchors to install and two workspaces to keep per
  release.

## Open site inputs

1. The Argo CD and Helm versions inside OpenShift GitOps 1.19.0 at the site.
2. Whether the central instance watches Applications in our namespaces, and
   the name and settings of the platform project they must use: source
   repositories, destinations, the client SA binding, denied kinds (A2).
3. Who registers the fork with the central instance, and with which read-only
   credential.
4. Who may press sync: our access to the Argo CD UI or CLI, or only the
   Application's `operation` field.
5. Namespace name lengths against the 63-character tracking label (D3).
6. The second cluster's values: Quay host, storage classes, API server
   addresses, Route host, IdP client, model endpoints and their Secrets.
7. That the second cluster's embedding model, revision and dimension equal
   the source's; otherwise the restored corpus cannot serve.
8. The snapshot size against the transfer folder's limits.
9. Whether the operator has namespace admin in the tools namespace and both
   deployment namespaces of each cluster, and whether the tools namespace's
   quota fits `/work` plus a staged snapshot.
10. Whether the platform project can enforce commit-signature or
    source-integrity verification for our Applications (A4). If it can, G1's
    gap closes. Production adoption needs this answered (maintainer
    decision 2).
11. Whether the cluster's built-in namespace roles grant rights on
    Applications, which decides whether our own namespace RBAC can limit who
    edits them (D6).
