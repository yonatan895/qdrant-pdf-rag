# Controlled Python dependency profiles

Contract / scope: issue #371, #482 V0. The supported wheel target is **Linux
x86_64, CPython 3.14 GIL, experimental JIT disabled, glibc >= 2.34**. Other
architectures, free-threading, newer Python minors, musl and source builds are
unqualified and fail closed. This is dependency-inventory reproducibility,
not a claim of byte-identical images or legal/security approval.

The owning inputs are `locks/cp314-linux-x86_64.json` and its three complete,
hash-pinned requirements files. The manifest records each selected wheel,
version, SHA-256 and public origin. Existing direct versions remain unchanged;
transitive versions were taken from the qualified development environment.

| Profile | File | Consumers |
|---|---|---|
| runtime | `requirements.lock.txt` | Runtime wheelhouse and both application images |
| dev | `requirements.dev.lock.txt` | Local development, GitHub and GitLab verification |
| build | `requirements.build.lock.txt` | Controlled project build tools, included in dev |

The dev closure contains the identical runtime and build closures plus test and
lint/type tools. It pins pip 26.2.1, setuptools and wheel. Runtime images keep
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

GitHub uses the same helper and manifest. GitLab accepts `PIP_FIND_LINKS` as a
local complete dev wheelhouse, or explicitly selects `--internal-index` using
an internal HTTPS `PIP_INDEX_URL`. The latter requires pip 26.2.1 already in
the prepared CI image, downloads only hash-approved wheels, ignores inherited
extra indexes/configuration, and suppresses download diagnostics that could
contain mirror credentials. Neither mode has a public-index fallback. Internal
mirror egress policy and trust/CA configuration remain platform responsibilities.
Private mirror addresses and credentials never belong in git.

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

`pack.sh` reads each actual shipped Docker archive through
`scripts/image_inventory.py`; it never executes an image to inspect it. The
reader verifies uncompressed layer identities, applies overwrites/whiteouts,
reads installed distribution metadata, and checks the image's lock and receipt
against the release source. Missing/extra packages and receipt omissions fail
before signing. `sbom.json` retains wheel identities and adds the independently
observed `installed_python` inventories with archive/config/lock hashes. Signed
bundle member hashes and existing image digest checks remain authoritative for
transport; an inventory receipt does not replace those checks.

This Python inventory distinguishes the inherited base, chart and host tools.
It is not a complete OS vulnerability report or a rights decision for vendored
JS, charts, skills or model/knowledge assets. Their inventory/notices and
qualified distribution decisions retain their existing owners under #376.

## Reproduce, refresh and roll back

Generation uses the existing pip 26.2.1 report format, not a new package manager.
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
