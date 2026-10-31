#!/bin/sh
# Release import for the air-gapped GitLab fork (ADR-0009 §4, follow-up F7, GH#1118).
# Run `sh scripts/gap/import_release.sh --help` for the inputs and outcomes.
#
# The whole script is one function that is parsed before it runs: step 0
# switches the fork checkout, so the file on disk may change under a run
# started as `sh "$FORK/scripts/gap/import_release.sh"`.

usage() {
    cat <<'EOF'
Import one signed upstream release into the air-gapped GitLab fork (ADR-0009 §4).

Export the five inputs, then run the script from the fork clone:

  export SHA=<40-hex sha from the tarball name qdrant-pdf-rag-<SHA>.tar>
  export BUNDLE=/abs/path/qdrant-pdf-rag-$SHA.tar   # its .sha256 file sits next to it
  export SNEAKERNET_TRUSTED_PUB=/abs/path/trusted-release-signing.pub
  export FORK=/abs/path/gitlab-clone
  export R=/abs/path/releases/$SHA                  # outside FORK; ends in /$SHA
  sh "$FORK/scripts/gap/import_release.sh"

Paths are absolute and hold no backslash.

Steps: 0 reset, 1 verify the bundle, 2 find the last import, 3 fetch and
check the release, then branch import/<SHA> from origin/main, 4 set aside the
ledgered gap patches, 5 merge, 6 re-apply the gap patches in ledger order,
7 prove the upstream tree.

Success ends with `IMPORT READY <SHA>` and prints the push command; the
script never pushes. Any failure ends with one `STOP:` line that names the
cause and the next action. Rerun the same command after any stop: step 0
aborts the unfinished import and starts again. It never discards work: it
stashes the unfinished import's uncommitted changes, keeps commits someone
added to import/<SHA> as import/<SHA>-<UTC timestamp>, and prints both
names. A rerun after success changes nothing. The script refuses to run
once import/<SHA> is pushed.
EOF
}

main() {
    set -eu
    exec 2>&1
    STEP="input check"
    STOPPED=0
    trap 'on_exit' EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY
    export GIT_PAGER=cat GIT_EDITOR=false LC_ALL=C

    case "${1:-}" in
        -h|--help) usage; exit 0 ;;
        "") ;;
        *) stop "the script takes no arguments; export the five inputs (--help lists them) and rerun" ;;
    esac

    # The inputs. This list is the extension surface (ADR-0009 §9).
    INPUTS="SHA BUNDLE SNEAKERNET_TRUSTED_PUB FORK R"
    for name in $INPUTS; do
        eval "value=\${$name:-}"
        if [ "$name" = SNEAKERNET_TRUSTED_PUB ]; then
            if [ -z "$value" ] || [ ! -f "$value" ]; then
                stop "no trust anchor: SNEAKERNET_TRUSTED_PUB must name the release public key received out of band (there is no trust on first use); ask the gap maintainer for it, then rerun"
            fi
        elif [ -z "$value" ]; then
            stop "$name is not set; export the five inputs (--help lists them) and rerun"
        fi
        case "$name:$value" in
            SHA:*) ;;
            *\\*) stop "$name must not contain a backslash (the release bootstrap cannot handle one); use another path and rerun" ;;
            *:/*) ;;
            *) stop "$name must be an absolute path; fix it and rerun" ;;
        esac
    done
    if [ "${#SHA}" -ne 40 ] || [ -n "$(printf '%s' "$SHA" | tr -d '0-9a-f')" ]; then
        stop "SHA must be the 40-hex release sha from the tarball name qdrant-pdf-rag-<SHA>.tar; fix SHA and rerun"
    fi
    if [ "$(basename -- "$BUNDLE")" != "qdrant-pdf-rag-$SHA.tar" ] || [ ! -f "$BUNDLE" ]; then
        stop "BUNDLE must be the file qdrant-pdf-rag-$SHA.tar; fix BUNDLE or SHA and rerun"
    fi
    if [ "$(basename -- "$R")" != "$SHA" ]; then
        stop "R must end in /$SHA (R=/abs/path/releases/$SHA), because the reset deletes R; fix R and rerun"
    fi
    if ! FORK_P=$(cd -P -- "$FORK" 2>/dev/null && pwd) \
            || [ "$(git -C "$FORK_P" rev-parse --show-toplevel 2>/dev/null)" != "$FORK_P" ]; then
        stop "FORK is not the top of a git clone; set FORK to the GitLab fork clone and rerun"
    fi
    R_P=$R
    if R_PARENT=$(cd -P -- "$(dirname -- "$R")" 2>/dev/null && pwd); then
        R_P="${R_PARENT%/}/$SHA"
    fi
    BUNDLE_P="$(cd -P -- "$(dirname -- "$BUNDLE")" && pwd)/$(basename -- "$BUNDLE")"
    ANCHOR_P="$(cd -P -- "$(dirname -- "$SNEAKERNET_TRUSTED_PUB")" && pwd)/$(basename -- "$SNEAKERNET_TRUSTED_PUB")"
    for kept in "$FORK_P" "$BUNDLE_P" "$ANCHOR_P"; do
        case "$kept/" in "$R_P"/*)
            stop "FORK, BUNDLE and SNEAKERNET_TRUSTED_PUB must be outside R, because the reset deletes R; move them or choose another R, then rerun" ;;
        esac
    done
    case "$R_P/" in "$FORK_P"/*)
        stop "R must be outside FORK; choose R outside the clone and rerun" ;;
    esac
    SCRIPT_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
    SELF="$SCRIPT_ROOT/scripts/gap/import_release.sh"
    COMMON="$SCRIPT_ROOT/scripts/airgap/common.sh"
    if [ ! -f "$COMMON" ] || [ ! -f "$SELF" ]; then
        stop "run the script from a fork checkout (sh \"\$FORK/scripts/gap/import_release.sh\"), not from a copy"
    fi
    export SNEAKERNET_TRUSTED_PUB
    REPORT="open GitLab issue \"Release import $SHA stopped\" with the last 20 lines of this output"
    BRANCH="import/$SHA"
    PUSH="git -C $(q "$FORK") push origin $(q "$BRANCH")"

    STEP="0 (reset)"
    cd "$FORK_P"
    MARK="$(git rev-parse --absolute-git-dir)/import-release-$SHA"
    git fetch -q --prune origin || stop "cannot fetch origin in FORK; check the GitLab remote, then rerun"
    BASE=$(git rev-parse -q --verify 'refs/remotes/origin/main^{commit}') \
        || stop "FORK has no origin/main; set FORK to a clone of the GitLab fork and rerun"
    if git merge-base --is-ancestor "$SHA" "$BASE" 2>/dev/null; then
        stop "$SHA is already imported (origin/main contains it: it is the last import or older); there is nothing to import"
    fi
    if git rev-parse -q --verify "refs/remotes/origin/$BRANCH" >/dev/null; then
        stop "$BRANCH is already pushed to origin; never rerun after the push: fix forward with new commits on its MR branch"
    fi
    on_branch=0
    [ "$(git symbolic-ref -q HEAD || true)" != "refs/heads/$BRANCH" ] || on_branch=1
    tip=$(git rev-parse -q --verify "refs/heads/$BRANCH^{commit}" || true)
    finished=0
    if [ -f "$MARK" ]; then
        mark_base=$(sed -n 's/^base //p' "$MARK")
        mark_tip=$(sed -n 's/^tip //p' "$MARK")
        if [ "${#mark_base}" -ne 40 ] || [ "${#mark_tip}" -ne 40 ]; then
            stop "the import marker $MARK is damaged; ask the gap maintainer"
        fi
        if [ -n "$tip" ] && [ "$tip" != "$mark_tip" ]; then
            stop "$BRANCH changed after IMPORT READY; push it as it is with: $PUSH, and fix forward on its MR, or ask the gap maintainer"
        fi
        [ -z "$tip" ] || finished=1
    fi
    # Every stop check runs before anything is set aside, so a stop changes
    # nothing. Only the unfinished import's own checkout is ever set aside.
    unfinished=0
    if [ "$on_branch" = 1 ] && [ "$finished" = 0 ]; then unfinished=1; fi
    if in_progress && [ "$unfinished" = 0 ]; then
        stop "a merge, revert, cherry-pick or rebase is in progress in FORK outside an unfinished import; finish or abort it (git status shows which), then rerun"
    fi
    if [ -n "$(git status --porcelain --untracked-files=all | grep '^??' || true)" ] \
            || { [ "$unfinished" = 0 ] && [ -n "$(git status --porcelain)" ]; }; then
        git status --porcelain
        stop "$FORK has local changes (listed above); commit or remove them, or ask the gap maintainer, then rerun"
    fi
    # The script never starts a rebase or git am, so it never sets one aside.
    if rebase_in_progress; then
        stop "a rebase or git am is in progress in FORK; finish or abort it (git status shows which), then rerun"
    fi
    # One timestamp names whatever this reset keeps (UTC, to the second).
    KEPT=$(date -u +%Y%m%dT%H%M%SZ)
    keep=0
    if [ -n "$tip" ] && ! script_made "$tip"; then
        keep=1
        if git rev-parse -q --verify "refs/heads/$BRANCH-$KEPT" >/dev/null; then
            stop "cannot keep $BRANCH as $BRANCH-$KEPT, which exists; rerun in a second, or ask the gap maintainer"
        fi
    fi
    if in_progress; then
        printf '%s\n' "Aborting the unfinished import's merge, revert or cherry-pick"
    fi
    if [ "$unfinished" = 1 ] && [ -n "$(git status --porcelain)" ]; then
        # git stash refuses unmerged paths: record them as they are first.
        # The stash also ends a merge in progress. Changes that match HEAD
        # make no stash: then nothing is printed, never an older stash.
        before=$(git rev-parse -q --verify refs/stash || true)
        git add -u
        git stash push -q -m "import_release $SHA $KEPT"
        after=$(git rev-parse -q --verify refs/stash || true)
        if [ -n "$after" ] && [ "$after" != "$before" ]; then
            printf '%s\n' "Stashed the unfinished import's uncommitted changes as \"import_release $SHA $KEPT\"; restore them with: git -C $(q "$FORK") stash apply $after"
        fi
    fi
    if in_progress; then
        if git rev-parse -q --verify MERGE_HEAD >/dev/null; then git merge --abort; fi
        if git rev-parse -q --verify REVERT_HEAD >/dev/null; then git revert --abort; fi
        if git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null; then git cherry-pick --abort; fi
        if in_progress; then
            stop "a merge, revert, cherry-pick or rebase is in progress in FORK outside an unfinished import; finish or abort it (git status shows which), then rerun"
        fi
    fi
    if [ "$finished" = 1 ] && [ "$mark_base" = "$BASE" ] && [ -f "$R/reapplied.txt" ] \
            && [ -f "$R/issue-notes.txt" ] && [ -f "$R/env-keys.diff" ] && [ -f "$R/release-notes.diff" ]; then
        printf '%s\n' "$BRANCH is already imported and unchanged since IMPORT READY; nothing changed"
        summary
        exit 0
    fi
    git switch -q --detach "$BASE"
    if [ -n "$tip" ]; then
        if [ "$keep" = 0 ]; then
            git branch -q -D "$BRANCH"
        else
            # Commits that someone else added: keep them under a new name.
            git branch -m "$BRANCH" "$BRANCH-$KEPT" \
                || stop "cannot keep $BRANCH as $BRANCH-$KEPT; rerun in a second, or ask the gap maintainer"
            printf '%s\n' "Kept the unfinished import's commits as branch $BRANCH-$KEPT"
        fi
    fi
    rm -f "$MARK"
    rm -rf "$R"

    STEP="1 (verify the bundle)"
    # The signature covers only what SHA256SUMS lists, so nothing else in the
    # tarball is trusted: members are listed before anything is unpacked, the
    # signed files are verified before the rest is unpacked, and the bundle
    # goes to its own fresh directory. Everything the script writes lives in
    # R itself or in a mktemp directory, never at a path the bundle supplied.
    [ -f "$BUNDLE.sha256" ] || stop "$BUNDLE.sha256 is missing; copy it next to the tarball and rerun"
    if ! awk -v name="qdrant-pdf-rag-$SHA.tar" 'NF != 2 || $2 != name { bad=1 } END { exit bad || NR != 1 }' "$BUNDLE.sha256" \
            || ! (cd "$(dirname -- "$BUNDLE")" && sha256sum --quiet -c "qdrant-pdf-rag-$SHA.tar.sha256"); then
        stop "the tarball does not match $BUNDLE.sha256; copy the tarball again and rerun"
    fi
    mkdir -p "$R"
    WORK=$(mktemp -d "$R/work.XXXXXX")
    UNPACKED="$R/bundle"
    mkdir "$UNPACKED"
    if ! tar -tvf "$BUNDLE" > "$WORK/types" || ! tar -tf "$BUNDLE" > "$WORK/members"; then
        stop "cannot list $BUNDLE; copy the tarball again and rerun"
    fi
    if [ -n "$(awk 'substr($0, 1, 1) != "-"' "$WORK/types")" ]; then
        stop "the tarball holds a member that is not a regular file (a link, directory or device); do not import it: ask for a new bundle"
    fi
    if [ -n "$(awk '$0 !~ /^[A-Za-z0-9._-]+$/ || $0 == "." || $0 == ".." || seen[$0]++' "$WORK/members")" ]; then
        stop "the tarball holds a member that is not a plain file name (a path, .. or an unusual character) or holds one name twice; do not import it: ask for a new bundle"
    fi
    for signed in SHA256SUMS SHA256SUMS.sig sneakernet-signing.pub MANIFEST.txt; do
        grep -qxF "$signed" "$WORK/members" || stop "the tarball has no $signed; do not import it: ask for a new bundle"
    done
    # cd, not tar -C: GNU tar unquotes backslashes in the -C argument.
    (cd "$UNPACKED" && tar -xf "$BUNDLE" SHA256SUMS SHA256SUMS.sig sneakernet-signing.pub MANIFEST.txt) \
        || stop "cannot unpack $BUNDLE; copy the tarball again and rerun"
    : > "$WORK/empty.env"
    # common.sh cds to this checkout; the chain is checked from the bundle
    # directory with a relative name (sha256sum escapes some path characters).
    if ! env AIRGAP_ENV="$WORK/empty.env" AIRGAP_DRYRUN=0 sh -c '. "$1" && cd "$2" && check_manifest_chain MANIFEST.txt' \
            "$SELF" "$COMMON" "$UNPACKED"; then
        stop "the bundle is not signed by SNEAKERNET_TRUSTED_PUB (FAIL line above); do not import it: ask for a correctly signed bundle"
    fi
    if [ -n "$(awk 'NR == FNR { listed[$2] = 1; next }
                    !($0 in listed) && $0 != "SHA256SUMS" && $0 != "SHA256SUMS.sig"' \
                    "$UNPACKED/SHA256SUMS" "$WORK/members")" ]; then
        stop "the tarball holds a member that the signed SHA256SUMS does not list; do not import it: ask for a new bundle"
    fi
    (cd "$UNPACKED" && tar -xf "$BUNDLE") || stop "cannot unpack $BUNDLE; copy the tarball again and rerun"
    (cd "$UNPACKED" && sha256sum --quiet -c SHA256SUMS) \
        || stop "a bundle member does not match the signed SHA256SUMS; do not import it: copy the tarball again or ask for a new bundle"
    if [ "$(sed -n 's/^sha: *//p' "$UNPACKED/MANIFEST.txt")" != "$SHA" ]; then
        stop "MANIFEST sha is not $SHA; check SHA against the tarball name and rerun"
    fi
    if [ "$(git bundle list-heads "$UNPACKED/repo.bundle" HEAD | awk '{print $1}')" != "$SHA" ]; then
        stop "repo.bundle HEAD is not the MANIFEST sha $SHA; do not import it: ask for a new bundle"
    fi
    (cd "$UNPACKED" && AIRGAP_WORKSPACE=release sh bootstrap.sh) \
        || stop "the release bootstrap refused the bundle (FAIL line above); do not import it: ask for a new bundle"

    STEP="2 (find the last import)"
    cd "$FORK_P"
    PREV=$(git log -1 --format=%B --grep='^Upstream-Release: ' "$BASE" | sed -n 's/^Upstream-Release: //p' | tail -n 1)
    if [ -z "$PREV" ]; then
        stop "no Upstream-Release trailer on origin/main; the fork was never created: create it as ADR-0009 §4 says"
    fi
    git rev-parse -q --verify "$PREV^{commit}" >/dev/null \
        || stop "the last Upstream-Release trailer names $PREV, which is not a commit in FORK; ask the gap maintainer"

    STEP="3 (fetch and check the release, then branch)"
    # Step 0 already refused a release that origin/main contains (the same
    # release or an older one). The branch comes last, so a refusal leaves none.
    git fetch -q "$UNPACKED/repo.bundle" HEAD
    if [ "$(git rev-parse FETCH_HEAD)" != "$SHA" ]; then
        stop "bundle HEAD is not $SHA; do not import it: ask for a new bundle"
    fi
    if ! git merge-base --is-ancestor "$PREV" "$SHA"; then
        stop "$SHA does not descend from the last import $PREV; import the releases in order, or ask the gap maintainer"
    fi
    if [ -n "$(git ls-tree -r --name-only "$SHA" -- gap .gitlab)" ]; then
        stop "the release tracks gap/ or .gitlab/; do not import it: report it upstream"
    fi
    # The ledger as origin/main has it, checked before the branch exists.
    LEDGER=gap/patches.md
    : > "$WORK/all.txt"
    if git cat-file -e "$BASE:$LEDGER" 2>/dev/null; then
        git show "$BASE:$LEDGER" > "$WORK/patches.before"
        # Any line that starts like a heading must be an exact one: a near
        # miss (##GL#, a leading blank, a tab, ###, CRLF) would otherwise
        # hide a gap patch from the set-aside.
        bad=$(awk '/^[[:space:]]*##/ {
                if (!(NF == 3 && $2 ~ /^GL#[0-9]+$/ && length($3) == 40 && $3 !~ /[^0-9a-f]/ \
                      && $0 == "## " $2 " " $3)) { print NR; exit }
            }' "$WORK/patches.before")
        [ -z "$bad" ] || stop "$LEDGER line $bad is not a ledger heading \"## GL#NNN <full commit sha>\"; fix the ledger on main through an MR, then rerun"
        awk '/^## / { print $2, $3 }' "$WORK/patches.before" > "$WORK/all.txt"
        twice=$(awk 'seen[$2]++ { print $2; exit }' "$WORK/all.txt")
        [ -z "$twice" ] || stop "$LEDGER lists gap patch $twice twice; fix the ledger on main through an MR, then rerun"
        for c in $(awk '{ print $2 }' "$WORK/all.txt"); do
            git merge-base --is-ancestor "$c" "$BASE" 2>/dev/null \
                || stop "$LEDGER names $c, which is not a commit on origin/main; fix the ledger on main through an MR, then rerun"
        done
    fi
    # Every later ref update on the branch carries this reflog action, so
    # step 0 of a rerun can tell the script's own commits from anyone else's.
    GIT_REFLOG_ACTION="import_release $SHA"
    export GIT_REFLOG_ACTION
    git switch -q --no-track -c "$BRANCH" "$BASE"

    STEP="4 (set aside the gap patches)"
    for c in $(awk '{ line[NR] = $2 } END { for (i = NR; i > 0; i--) print line[i] }' "$WORK/all.txt"); do
        if ! git revert --no-edit "$c"; then
            stop "cannot set aside gap patch $c; an unledgered change overlaps it: rerun resets, then $REPORT"
        fi
    done
    if [ -s "$WORK/all.txt" ]; then
        awk '/^## / { exit } { print }' "$WORK/patches.before" > "$LEDGER"
        git add -- "$LEDGER"
        git commit -q -m "Set aside gap patches before importing $SHA"
    fi

    STEP="5 (merge)"
    if ! git merge -q --no-ff --no-commit "$SHA"; then
        stop "conflict in: $(git diff --name-only --diff-filter=U | tr '\n' ' ' | sed 's/ $//'); an unledgered change on main overlaps the release: rerun resets, then $REPORT"
    fi
    git commit -q -m "Import upstream release $SHA" -m "Upstream-Release: $SHA"

    STEP="6 (re-apply the gap patches)"
    : > "$R/reapplied.txt"
    : > "$R/issue-notes.txt"
    for c in $(awk '{ print $2 }' "$WORK/all.txt"); do
        i=$(awk -v c="$c" '$2 == c { print $1 }' "$WORK/all.txt")
        if git cherry-pick "$c"; then
            echo "reapplied $i $c $(git rev-parse HEAD)" >> "$R/reapplied.txt"
        elif git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null; then
            git cherry-pick --abort
            echo "set-aside $i $c" >> "$R/reapplied.txt"
            echo "$i $c: not re-applied on upstream release $SHA; rework or retire" >> "$R/issue-notes.txt"
        else
            stop "cannot re-apply gap patch $c (output above): rerun resets, then $REPORT"
        fi
    done
    if grep -q '^reapplied ' "$R/reapplied.txt"; then
        awk 'FILENAME == ARGV[1] { if ($1 == "reapplied") new[$3] = $4; next }
             /^## / { on = ($3 in new); if (on) print $1, $2, new[$3]; next }
             on' "$R/reapplied.txt" "$WORK/patches.before" >> "$LEDGER"
        git add -- "$LEDGER"
        git commit -q -m "Re-apply gap patches after importing $SHA"
    fi

    STEP="7 (prove the upstream tree)"
    # Outside the site layer the fork equals the release except the paths the
    # ledger lists. --no-renames lists both sides of a rename; -z keeps paths raw.
    git diff --no-renames -z --name-only "$SHA" HEAD -- . ':(exclude)gap' ':(exclude).gitlab' > "$WORK/drift.z"
    tr '\0' '\n' < "$WORK/drift.z" | sort -u > "$WORK/drift.txt"
    if [ -f "$LEDGER" ]; then sed -n 's/^- path: //p' "$LEDGER"; fi | sort -u > "$WORK/patches.txt"
    if ! cmp -s "$WORK/drift.txt" "$WORK/patches.txt"; then
        diff "$WORK/patches.txt" "$WORK/drift.txt" || true
        stop "upstream-owned paths differ from the ledger (above: < ledgered only, > changed only); ledger the change or revert it on main through an MR, then rerun"
    fi
    git diff "$PREV" "$SHA" -- airgap.env.example > "$R/env-keys.diff"
    git diff "$PREV" "$SHA" -- docs/release-notes.md > "$R/release-notes.diff"
    printf 'base %s\ntip %s\n' "$BASE" "$(git rev-parse HEAD)" > "$MARK.tmp"
    mv -f "$MARK.tmp" "$MARK"
    summary
}

# Quote one word for a copy-paste shell command.
q() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# printf, not echo: dash's echo expands backslashes in paths.
stop() {
    STOPPED=1
    printf '%s\n' "STOP: $*"
    exit 1
}

on_exit() {
    rc=$?
    if [ "$rc" -ne 0 ] && [ "$STOPPED" = 0 ]; then
        printf '%s\n' "STOP: step $STEP failed (exit $rc); rerun the same command to reset and resume; if it stops here again, open GitLab issue \"Release import ${SHA:-<SHA>} stopped\" with the last 20 lines of this output"
    fi
}

# True when the import branch tip holds nothing but this script's own work:
# it is on origin/main already, or the branch's last reflog entry is a step
# of this script that set exactly that tip. Without a reflog it is false.
script_made() {
    if git merge-base --is-ancestor "$1" "$BASE"; then return 0; fi
    last=$(git reflog show -n 1 --format='%H %gs' "refs/heads/$BRANCH" 2>/dev/null || true)
    [ "${last%% *}" = "$1" ] || return 1
    case "${last#* }" in "import_release $SHA: "*) return 0 ;; esac
    return 1
}

rebase_in_progress() {
    for dir in rebase-merge rebase-apply; do
        if [ -d "$(git rev-parse --git-path "$dir")" ]; then return 0; fi
    done
    return 1
}

in_progress() {
    for ref in MERGE_HEAD REVERT_HEAD CHERRY_PICK_HEAD; do
        if git rev-parse -q --verify "$ref" >/dev/null; then return 0; fi
    done
    for dir in rebase-merge rebase-apply; do
        if [ -d "$(git rev-parse --git-path "$dir")" ]; then return 0; fi
    done
    return 1
}

summary() {
    printf '%s\n' "Gap patches ($R/reapplied.txt):"
    sed 's/^/  /' "$R/reapplied.txt"
    if [ -s "$R/issue-notes.txt" ]; then
        printf '%s\n' "Comment on or reopen each GitLab issue below with its note ($R/issue-notes.txt):"
        sed 's/^/  /' "$R/issue-notes.txt"
    fi
    printf '%s\n' "Key changes for gap/config/: $R/env-keys.diff"
    printf '%s\n' "Release notes for the extension surface: $R/release-notes.diff"
    printf '%s\n' "Next: finish the follow-ups in ADR-0009 §4, then push with:"
    printf '%s\n' "  $PUSH"
    printf '%s\n' "IMPORT READY $SHA"
}

main "$@"; exit "$?"
