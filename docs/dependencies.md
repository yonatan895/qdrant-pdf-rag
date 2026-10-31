# Controlled Python dependency profiles

Contract / scope: issue #371, #482 V0. The supported wheel target is **Linux
x86_64, CPython 3.14 GIL, experimental JIT disabled, glibc >= 2.34**. Other
architectures, free-threading, newer Python minors, musl and source builds are
unqualified and fail closed. This is dependency-inventory reproducibility,
not a claim of byte-identical images or legal/security approval.

The owning inputs are `locks/cp314-linux-x86_64.json` and its three complete,
hash-pinned requirements files. The manifest records each selected wheel,
version, SHA-256 and public origin. Versions change only through a dedicated
reviewed refresh; every other pin stays fixed.
The `urllib3` security pin and the directly imported `pyjwt` are also explicit in
`pyproject.toml`; their runtime/dev lock entries must agree with those project
dependencies, and urllib3's wheel identity too.
`hypothesis` (property tests, #817) and its `sortedcontainers` dependency are dev-profile
only; they are in neither the runtime nor the build closure, nor any image.

| Profile | File | Consumers |
|---|---|---|
| runtime | `requirements.lock.txt` | Runtime wheelhouse and both application images |
| dev | `requirements.dev.lock.txt` | Local development, GitHub and GitLab verification |
| build | `requirements.build.lock.txt` | Controlled project build tools, included in dev |

The dev closure contains the identical runtime and build closures plus test and
lint/type tools. It pins pip 26.2.2, setuptools and wheel. Runtime images keep
the existing digest-pinned UBI base's pip 24.2; that inherited difference is
explicit in the manifest and inventory, rather than upgrading production pip
implicitly. Application source still runs through `/app/src` and `PYTHONPATH`;
it is not installed as a distribution in either runtime image. Dev installs the
editable project separately with `--no-deps --no-build-isolation` after checking
all dependency bytes. The doctor checks the editable source tree as well as
missing, wrong-version, duplicate-location and unexpected distributions.

## Explicit preparation and verification

Preparation is never a verification prerequisite that repairs itself:

```sh
# Connected, explicit local preparation. No source build or dependency resolution.
sh scripts/tools/run-task.sh dev:setup
# Offline, explicit preparation into a directory owned by this checkout.
python3.14 scripts/prepare_python.py --venv .venv --wheelhouse /approved/dev-wheels
# Read-only verification; no package index, service call or installation.
.venv/bin/python scripts/dependency_lock.py installed --profile dev --project
# Connected runtime wheel acquisition, then offline byte verification.
sh scripts/tools/run-task.sh artifacts:wheelhouse
.venv/bin/python scripts/dependency_lock.py wheelhouse --profile runtime --directory bundles/wheelhouse
```

`prepare_python.py` verifies every wheel before creating or modifying the target
environment, then runs the verified pip wheel under the selected interpreter.
It ignores inherited pip settings and disables indexes and build isolation for
installation. It refuses local symlink environment destinations; use an owned
venv, or explicitly select an owned CI interpreter with `--python`.
The wheelhouse must contain exactly the selected profile's wheels; maintain
separate runtime and dev directories. Missing/corrupt/incompatible/unselected
wheels fail verification before installation. Connected acquisition is an
explicit `--connected` operation and verifies downloads before replacement.

In the air gap the dev wheels ship once, as the dev kit image's installed
environment (ADR-0009 §8, D-7); the image holds no wheelhouse.
`prepare_python.py --kit --venv .venv` first requires the image's two lock files
(`/opt/devkit/locks/`) to equal the checkout's `requirements.dev.lock.txt` and
`locks/cp314-linux-x86_64.json`; otherwise it refuses and names the kit to
request (`mfr-devkit-<key>`). It then creates a small venv layered over the
image's environment. The image's `python3` is itself a venv, so a `.pth` file
adds its site directories (`--system-site-packages` would add the empty base
ones) and its native tools are linked, so `python -m ruff` works. That `.pth`
sorts before the checkout's editable one, so locked distributions precede
`src/`. Only the checkout is installed, editable, with `--no-deps` and no index.
The result must equal the dev lock plus the checkout exactly (`dependency_lock.py
installed --profile dev --project`), and a top-level `src/` name that a locked
distribution provides is refused. It runs as the pod's arbitrary UID, with the
image's `python3`, never from inside a kit venv. `--kit` never acquires, so it
refuses `--connected`, `--internal-index` and `--python`.
`agent_doctor.py` makes the same lock comparison in any image that has
`/opt/devkit/locks/` and reports a mismatch as a `dev kit` finding naming the
same kit. Outside a kit image it adds no finding.

GitHub uses the same helper and manifest. GitLab jobs run on the dev kit image
and use `--kit`, so they acquire no package; they no longer read
`PIP_FIND_LINKS` or `PIP_INDEX_URL` (GH#1118 F5). `--internal-index` remains for
an explicit preparation with an internal HTTPS `PIP_INDEX_URL`: it requires pip
26.2.2 already in the selected interpreter, downloads only hash-approved wheels,
ignores inherited extra indexes/configuration, and suppresses download
diagnostics that could contain mirror credentials. No mode has a public-index
fallback. Internal mirror egress policy and trust/CA configuration remain
platform responsibilities. Private mirror addresses and credentials never
belong in git.

Connected GitHub preparation explicitly selects `--download-workers 8`.
`dependency_lock.py acquire/prepare` and `prepare_python.py --connected` support
1–16 workers; the ordinary default remains one, and offline/internal-index dev
preparation cannot opt into this public-origin acquisition path. Each worker
owns a distinct temporary wheel and verifies its approved hash before atomic
member replacement. Duplicate destinations, unapproved origins and symlinks are
refused before downloads start. The complete inventory is verified before any
installation or preparation completion record. On failure, queued downloads
are cancelled and active workers finish before staging cleanup; staged
preparation preserves the previous cache. Parallel acquisition prints verified
wheel filenames to stderr, never URLs or credentials. Cache reuse still hashes
the actual bytes and does not authorize concurrent readers during publication.

Status stamps alone do not qualify caches. Local setup rechecks installed
inventory/source identity; wheelhouse status rechecks every member against the
approved manifest. Separate preparation processes must own separate directories.
Interrupted preparation is unqualified until the same command succeeds and
verification passes. Existing unselected packages are reported, not silently
removed; select a fresh owned environment when changing profiles.

## Installed image and release inventory

Both Containerfiles verify runtime wheel bytes before offline hash-mode
installation, run `pip check`, and compare actual installed metadata with the
runtime profile plus inherited pip. They record that observed inventory under
`/opt/rag-locks/installed-inventory.json`. Dev/build tools are not installed in
runtime images. The final process still runs unprivileged.

`pack.sh` reads each actual shipped OCI archive (#1066) through
`scripts/image_inventory.py`; it never executes an image to inspect it. The
reader first verifies the archive's blobs against their descriptors
(`scripts/airgap/image_manifest.py`), then the uncompressed layer identities
(diff_ids), applies overwrites/whiteouts,
reads installed distribution metadata, and checks the image's lock and receipt
against the release source. Missing/extra packages and receipt omissions fail
before signing. `sbom.json` retains wheel identities and adds the independently
observed `installed_python` inventories with archive/config/lock hashes. Signed
bundle member hashes and existing image digest checks remain authoritative for
transport; an inventory receipt does not replace those checks.

This Python inventory distinguishes the inherited base, chart and host tools.
It is not a complete OS vulnerability report or a rights decision for vendored
JS, charts, skills or model/knowledge assets. Their license inventory, bundled
notices and open owner decisions are owned by [licensing](licensing.md) (#376);
`scripts/license_inventory.py check` reconciles that record with these locks.

## Trust-store inputs (#894)

Two locked wheels carry a CA bundle: `certifi` and `pip` (`pip/_vendor/certifi`).
Both pins move together with the base image CA store, proven by bundle content,
not version. Production gateway TLS uses `SSL_CERT_FILE` from the site CA
ConfigMap when `gateway.caConfigMapName` is set (chart); certifi is the fallback
otherwise. certifi 2026.10.19 and pip 26.2.2 drop only "emSign ECC Root CA - G3";
the G1, C1 and C3 roots remain. The pinned UBI digest and the current `9.8` tag
ship the same `ca-certificates` 2025.2.80_v9.0.305-91.el9 bundle with all four
emSign roots; no workaround is applied. Re-inspect on each base-image refresh.

## FastEmbed and Pillow (#1064, #895)

`fastembed` and `pillow` move together: FastEmbed's Python 3.14 requirement
declared `pillow<13.0` through 0.9.0, so Pillow 13 could not be locked without
overriding that constraint. `fastembed` 0.9.1 (released 2026-10-24) declares
`pillow<14.0,>=12.0.0` on Python 3.14; the refresh pins `fastembed==0.9.1` and
`pillow==13.0.0` in the runtime and dev profiles. No other pin changed: the 3.14
floors (`numpy>=2.3.0`, `onnxruntime>=1.24.2`) were already satisfied by numpy
2.5.3 and onnxruntime 1.30.0, and no transitive package was added. Pillow 13.0.0
is the first patched release for GHSA-5x8w-8jj5-q52h (critical) and
GHSA-654x-cwwv-5j9c (medium); advisory status is evidence, not an approval.
For a fixed synthetic query and document set, `SparseTextEmbedding` produced
byte-identical BM25 document and query vectors under fastembed 0.8.0/Pillow
12.3.0 and 0.9.1/Pillow 13.0.0 (#895).

## Reproduce, refresh and roll back

Generation uses the existing pip 26.2.2 report format, not a new package manager.
In a qualified target environment, reproduce the current profiles without
installing or changing the environment:

```sh
mkdir -p /tmp/lock-reports
for profile in runtime dev build; do
  case "$profile" in
    runtime) requirement=requirements.lock.txt ;;
    dev) requirement=requirements.dev.lock.txt ;;
    build) requirement=requirements.build.lock.txt ;;
  esac
  .venv/bin/python -m pip --isolated install --dry-run --ignore-installed \
    --only-binary=:all: --require-hashes -r "$requirement" \
    --report "/tmp/lock-reports/$profile.json"
done
.venv/bin/python scripts/dependency_lock.py generate --reports /tmp/lock-reports
```

Resolution is a connected maintenance operation. Reports must identify the
qualified resolver/target and public wheel origins. Review all generated pins,
profile differences and wheel hashes in a dedicated dependency concern.
For an approved refresh, supply reviewed candidate constraints to that same
resolver, keep unchanged pins fixed, generate all profiles together, and record
the input constraints and reports in the PR evidence. Do not update requirements
alone or repair a failed check by regenerating its expected inventory.

### Offline candidate-lock check (required before approval)

Every dependency-refresh PR records the bounded on-demand check
(`scripts/verify_dependency_refresh.py`, reached through `qa:deps`) against
the complete candidate wheelhouse: the command, exit code and receipt.

```sh
sh scripts/tools/run-task.sh qa:deps \
  WHEELHOUSE=/approved/dev-wheels PY=.venv/bin/python
```

`qa:deps` verifies the selected profile's wheelhouse bytes against the lock
manifest, creates a temporary CPython 3.14 venv from the prepared interpreter,
installs the complete dev profile with `--no-index --find-links` (no index or
network), runs `pip check`, then the dependency-lock, licence-inventory,
BM25-weights, embed-budget and hash-embed suites in that clean environment. It
never installs into `.venv`, never edits a lock, and removes the temporary
environment on exit. The temporary environment lives under `TMPDIR`; for a full
dev install set `TMPDIR` to disk-backed scratch such as
`$HOME/.cache/agent-tmp/` (the [live-stack](live-stack.md) `$SCRATCH_DIR`
convention), not a small tmpfs. Missing, incomplete, extra, symlinked or
tampered wheels and a missing interpreter fail closed before effects.
`WHEELHOUSE` defaults to the prepared `.tools/python-wheels/dev`; point it at
the candidate wheelhouse for a refresh. In a fresh worktree without `.venv`,
pass `PY=<main>/.venv/bin/python`; it is only the base interpreter, and the
disposable venv is created from it. `PROFILE` defaults to `dev`; the focused
suites require it because only the dev profile contains their test runner, and
it is a strict superset of runtime/build at identical pins. This check is
on-demand maintenance, not an every-lane CI job; the two clean same-target
environments below remain the approval evidence.

Before approval, install into two clean same-target environments and compare
observed dependency inventories; exercise missing, modified and incompatible
transitive artifacts; build both images without networking; reconcile actual
archives and the signed packaging output. Run the applicable verification from
[live-stack](live-stack.md#verification-minimums). Record advisory database,
version/date and qualified licensing decisions separately; hashes alone confer
neither publisher trust nor legal approval.

Retain the previous approved source/locks, complete wheel profiles, pinned
Python/tool/image bytes, BM25 cache and signed release bundle outside git. Test
the retained previous inputs offline in a separate workspace/environment before
promoting the refresh. Roll back by selecting that complete approved release;
never combine an old image/wheelhouse with a new lock or silently resolve a
replacement inside the air gap. No deployment promotion is implied by local
lock verification.
