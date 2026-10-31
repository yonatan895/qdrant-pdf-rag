"""scripts/gap/import_release.sh: the ADR-0009 §4 release import (GH#1118 F7).

Every case runs the real script, from the fork checkout as an operator does,
on throwaway repositories: an upstream repository, signed release bundles that
the real bootstrap.sh accepts, a bare repository standing in for GitLab, and a
fork clone with a gap/ site layer and ledgered gap patches. Expected results
are computed independently of the script: trees are compared blob by blob, and
the ledger and the commit messages are compared byte for byte.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from functools import cache
from pathlib import Path

import pytest

from tests.helpers_airgap import REPO, gen_sign_keypair, sha256_bytes, sign_sums
from tests.helpers_task_artifact import copy_task_tools, task_manifest, task_members

SCRIPT = "scripts/gap/import_release.sh"
SHIPPED = (SCRIPT, "scripts/airgap/common.sh", "scripts/airgap/collection-policy.env")
TOOLS = ("git", "openssl", "sha256sum", "tar", "awk", "sed", "sh")
PATH = os.pathsep.join(dict.fromkeys([*(str(Path(shutil.which(t) or "/usr/bin/x").parent) for t in TOOLS),
                                      "/usr/bin", "/bin"]))
RUN_DATE = "1800000000 +0000"
TITLE = "# Gap patches\n\nFree text before the first section stays.\n"


@cache
def _task_members() -> dict[str, bytes]:
    return task_members()


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    root = tmp_path_factory.mktemp("keys")
    key = gen_sign_keypair(root)
    anchor = root / "anchor.pub"
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(anchor)],
                   check=True, capture_output=True)
    return key, anchor


def lines(prefix: str, count: int) -> str:
    return "".join(f"{prefix} {n}\n" for n in range(1, count + 1))


def with_line(text: str, number: int, new: str) -> str:
    rows = text.splitlines(keepends=True)
    rows[number - 1] = new + "\n"
    return "".join(rows)


class World:
    """Upstream, signed bundles, the GitLab stand-in and the fork clone."""

    def __init__(self, root: Path, keys: tuple[Path, Path]):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.key, anchor = keys
        self.anchor = root / "trust" / "anchor.pub"
        self.anchor.parent.mkdir(parents=True)
        shutil.copy(anchor, self.anchor)
        self.clock = 1700000000
        (root / "home").mkdir()
        (root / "home" / ".gitconfig").write_text("")
        (root / "tmp").mkdir()
        self.up = root / "upstream"
        self.bare = root / "gitlab.git"
        self.fork = root / "fork clone"
        self.bundles = root / "bundles dir"
        self.releases = root / "releases dir"
        self.bundles.mkdir()
        self.up.mkdir()
        self.git(self.up, "init", "-q", "-b", "main")
        for relative in SHIPPED:
            (self.up / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / relative, self.up / relative)
        copy_task_tools(self.up)
        self.r1 = self.release({
            "README.md": lines("readme", 10), "src/app.py": lines("app", 20),
            "lib/data.txt": lines("data", 5), "airgap.env.example": "INTERNAL_REGISTRY=example\n",
            "docs/release-notes.md": "# Release notes\n",
        }, "Release 1")

    # -- plumbing --------------------------------------------------------
    def env(self, date: str | None = None) -> dict[str, str]:
        if date is None:
            self.clock += 60
            date = f"{self.clock} +0000"
        return {"PATH": PATH, "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp"),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(self.root / "home" / ".gitconfig"),
                "GIT_AUTHOR_NAME": "Gap Maintainer", "GIT_AUTHOR_EMAIL": "gap@example.invalid",
                "GIT_COMMITTER_NAME": "Gap Maintainer", "GIT_COMMITTER_EMAIL": "gap@example.invalid",
                "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env(), check=True,
                              capture_output=True, text=True).stdout

    def raw(self, cwd: Path, *args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env(), check=True, capture_output=True).stdout

    def commit(self, repo: Path, edits: dict[str, str | None], message: str) -> str:
        for relative, content in edits.items():
            path = repo / relative
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        self.git(repo, "add", "-A")
        subprocess.run(["git", "commit", "-q", "--cleanup=verbatim", "-F", "-"], cwd=repo, env=self.env(),
                       input=message.encode(), check=True, capture_output=True)
        return self.git(repo, "rev-parse", "HEAD").strip()

    # -- upstream releases -------------------------------------------------
    def release(self, edits: dict[str, str | None], message: str, **bundle) -> str:
        sha = self.commit(self.up, edits, message)
        self.bundle(sha, **bundle)
        return sha

    def bundle(self, sha: str, *, name_sha: str | None = None, key: Path | None = None, tamper=None,
               append=None) -> Path:
        """A signed tarball whose repo.bundle HEAD is upstream HEAD and whose
        MANIFEST and file name say name_sha (default: sha)."""
        name_sha = name_sha or sha
        stage = self.root / "stage" / f"{name_sha}-{len(list(self.bundles.iterdir()))}"
        stage.mkdir(parents=True)
        self.git(self.up, "bundle", "create", "-q", str(stage / "repo.bundle"), "HEAD", "main")
        key = key or self.key
        subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(stage / "sneakernet-signing.pub")],
                       check=True, capture_output=True)
        files = {
            "bootstrap.sh": (REPO / "scripts/airgap/bootstrap.sh").read_bytes(),
            "repo.bundle": (stage / "repo.bundle").read_bytes(),
            "qdrant-image.tar": b"qdrant", "openobserve-image.tar": b"openobserve", "otelcol-image.tar": b"otelcol",
            f"app-ingest-{name_sha}.tar": b"ingest", f"app-agent-{name_sha}.tar": b"agent",
            "MANIFEST.txt": (f"sha: {name_sha}\n" + task_manifest()).encode(),
            **_task_members(),
            "PACKING_RECORD.txt": b"record\n", "sbom.json": b'{"images": []}\n',
            "THIRD-PARTY-NOTICES.txt": b"notices\n",
            "sneakernet-signing.pub": (stage / "sneakernet-signing.pub").read_bytes(),
        }
        for name, content in files.items():
            (stage / name).write_bytes(content)
        (stage / "SHA256SUMS").write_text("".join(f"{sha256_bytes(c)}  {n}\n" for n, c in files.items()))
        sign_sums(stage, key)
        if tamper:
            tamper(stage)
        tarball = self.bundles / f"qdrant-pdf-rag-{name_sha}.tar"
        subprocess.run(["tar", "-cf", str(tarball), "-C", str(stage), *files, "SHA256SUMS", "SHA256SUMS.sig"],
                       check=True, capture_output=True)
        if append:  # Members the signature does not cover.
            with tarfile.open(tarball, "a") as archive:
                for info, data in append(self):
                    archive.addfile(info, data)
        Path(f"{tarball}.sha256").write_text(f"{sha256_bytes(tarball.read_bytes())}  {tarball.name}\n")
        return tarball

    # -- the fork --------------------------------------------------------
    def create_fork(self, release: str) -> None:
        """ADR-0009 §4 fork creation: push the release history, then commit
        the site layer with the Upstream-Release trailer."""
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.git(self.up, "push", "-q", str(self.bare), f"{release}:refs/heads/main")
        self.git(self.root, "clone", "-q", str(self.bare), str(self.fork))
        self.commit(self.fork, {"gap/README.md": "Gap entry point\n", "gap/patches.md": TITLE},
                    f"Create the gap fork\n\nUpstream-Release: {release}\n")
        self.git(self.fork, "push", "-q", "origin", "main")

    def patch(self, issue: int, edits: dict[str, str | None], message: str, free_text: str = "") -> str:
        """One gap patch commit, then its ledger section in a second commit."""
        sha = self.commit(self.fork, edits, message)
        ledger = self.fork / "gap/patches.md"
        section = f"## GL#{issue} {sha}\n" + "".join(f"- path: {p}\n" for p in edits) + free_text
        ledger.write_text(ledger.read_text(encoding="utf-8") + section, encoding="utf-8")
        self.commit(self.fork, {}, f"Ledger GL#{issue}")
        self.git(self.fork, "push", "-q", "origin", "main")
        return sha

    def stray(self, edits: dict[str, str | None]) -> str:
        sha = self.commit(self.fork, edits, "An unledgered change to an upstream-owned path")
        self.git(self.fork, "push", "-q", "origin", "main")
        return sha

    def read(self, relative: str) -> str:
        return (self.fork / relative).read_text(encoding="utf-8")

    def up_text(self, relative: str) -> str:
        return (self.up / relative).read_text(encoding="utf-8")

    # -- running the script ----------------------------------------------
    def run(self, sha: str, *, path_prefix: str = "", extra: dict[str, str | None] | None = None):
        env = self.env(RUN_DATE)
        env.update(SHA=sha, BUNDLE=str(self.bundles / f"qdrant-pdf-rag-{sha}.tar"),
                   SNEAKERNET_TRUSTED_PUB=str(self.anchor), FORK=str(self.fork), R=str(self.releases / sha))
        if path_prefix:
            env["PATH"] = path_prefix + os.pathsep + env["PATH"]
        for name, value in (extra or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return subprocess.run(["sh", str(self.fork / SCRIPT)], cwd=self.root, env=env, capture_output=True,
                              text=True, check=False, start_new_session=True, timeout=600)

    def refs(self, repo: Path, *, stash: bool = True) -> str:
        listed = self.git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        return listed if stash else "".join(r for r in listed.splitlines(keepends=True)
                                            if not r.startswith("refs/stash "))

    def tree(self, rev: str) -> dict[bytes, bytes]:
        """Blob ids by path outside the site layer."""
        entries = {}
        for entry in self.raw(self.fork, "ls-tree", "-r", "-z", rev).split(b"\0"):
            if entry:
                meta, path = entry.split(b"\t", 1)
                if not (path.startswith((b"gap/", b".gitlab/"))):
                    entries[path] = meta.split()[2]
        return entries

    def drift(self, release: str, rev: str) -> set[str]:
        before, after = self.tree(release), self.tree(rev)
        return {p.decode() for p in before.keys() | after.keys() if before.get(p) != after.get(p)}

    def ledger_paths(self, rev: str) -> set[str]:
        text = self.git(self.fork, "show", f"{rev}:gap/patches.md")
        return {line[len("- path: "):] for line in text.splitlines() if line.startswith("- path: ")}

    def message(self, rev: str) -> bytes:
        return self.raw(self.fork, "cat-file", "commit", rev).split(b"\n\n", 1)[1]

    def first_parents(self, rev: str, count: int) -> list[str]:
        return self.git(self.fork, "log", "--first-parent", f"-{count}", "--format=%s", rev).splitlines()


@pytest.fixture
def world(tmp_path, keys):
    return World(tmp_path, keys)


def last(result) -> str:
    return result.stdout.rstrip("\n").splitlines()[-1]


def assert_one_stop(result, expected: str) -> None:
    assert result.returncode != 0, result.stdout[-3000:]
    assert last(result) == expected, result.stdout[-3000:]
    assert result.stdout.count("STOP:") == 1, result.stdout[-3000:]


def assert_ready(world: World, result, sha: str) -> str:
    assert result.returncode == 0, result.stdout[-4000:]
    assert last(result) == f"IMPORT READY {sha}"
    assert "STOP:" not in result.stdout
    tip = world.git(world.fork, "rev-parse", f"refs/heads/import/{sha}").strip()
    # Step 7, computed independently: outside the site layer the fork equals
    # the release except exactly the paths the ledger lists.
    assert world.drift(sha, tip) == world.ledger_paths(tip)
    return tip


def reapplied(world: World, sha: str) -> list[str]:
    return (world.releases / sha / "reapplied.txt").read_text().splitlines()


# -- imports -----------------------------------------------------------------

def test_clean_import_without_gap_patches(world):
    world.create_fork(world.r1)
    main = world.git(world.fork, "rev-parse", "origin/main").strip()
    bare = world.refs(world.bare)
    r2 = world.release({"README.md": with_line(world.up_text("README.md"), 1, "readme one"),
                        "docs/new.md": "new\n",
                        "airgap.env.example": "INTERNAL_REGISTRY=example\nNEW_SITE_KEY=\n",
                        "docs/release-notes.md": "# Release notes\n\n- added NEW_SITE_KEY\n"}, "Release 2")

    result = world.run(r2)

    tip = assert_ready(world, result, r2)
    assert world.drift(r2, tip) == set()
    assert world.git(world.fork, "symbolic-ref", "HEAD").strip() == f"refs/heads/import/{r2}"
    assert world.message(tip) == f"Import upstream release {r2}\n\nUpstream-Release: {r2}\n".encode()
    assert world.git(world.fork, "rev-parse", f"{tip}^1", f"{tip}^2").split() == [main, r2]
    assert reapplied(world, r2) == []
    out = world.releases / r2
    assert "+NEW_SITE_KEY=" in (out / "env-keys.diff").read_text()
    assert "+- added NEW_SITE_KEY" in (out / "release-notes.diff").read_text()
    assert world.refs(world.bare) == bare  # The script never pushes.


def test_ledgered_patches_reapply_in_ledger_order_and_again_on_the_next_release(world):
    world.create_fork(world.r1)
    app = world.read("src/app.py")
    p11 = world.patch(11, {"src/app.py": with_line(app, 3, "gap 11")}, "GL#11 change\n\nGap-Patch: GL#11\n",
                      "Title: first\n")
    p12 = world.patch(12, {"lib/data.txt": with_line(world.read("lib/data.txt"), 2, "gap 12")},
                      "GL#12 change\n\nGap-Patch: GL#12\n")
    # GL#13 builds on GL#11: re-applied in any other order, it conflicts.
    p13 = world.patch(13, {"src/app.py": with_line(world.read("src/app.py"), 3, "gap 13")},
                      "GL#13 change\n\nGap-Patch: GL#13\n", "Title: third\n")
    r2 = world.release({"src/app.py": with_line(world.up_text("src/app.py"), 18, "upstream 18"),
                        "README.md": world.up_text("README.md") + "more\n"}, "Release 2")

    result = world.run(r2)

    tip = assert_ready(world, result, r2)
    rows = [row.split() for row in reapplied(world, r2)]
    assert [row[:3] for row in rows] == [["reapplied", "GL#11", p11], ["reapplied", "GL#12", p12],
                                         ["reapplied", "GL#13", p13]]
    new = [row[3] for row in rows]
    assert world.first_parents(tip, 9) == [
        f"Re-apply gap patches after importing {r2}", "GL#13 change", "GL#12 change", "GL#11 change",
        f"Import upstream release {r2}", f"Set aside gap patches before importing {r2}",
        'Revert "GL#11 change"', 'Revert "GL#12 change"', 'Revert "GL#13 change"']
    for old, copy in zip((p11, p12, p13), new, strict=True):
        assert world.message(copy) == world.message(old)
    assert world.git(world.fork, "rev-list", "--reverse", f"{tip}~4..{tip}~1").split() == new
    assert world.read("gap/patches.md") == (
        TITLE + f"## GL#11 {new[0]}\n- path: src/app.py\nTitle: first\n"
        f"## GL#12 {new[1]}\n- path: lib/data.txt\n"
        f"## GL#13 {new[2]}\n- path: src/app.py\nTitle: third\n")
    app = world.read("src/app.py").splitlines()
    assert (app[2], app[17]) == ("gap 13", "upstream 18")

    # The next ordinary operation: the maintainer merges the MR, and the next
    # release imports on top of it, re-applying the re-applied copies.
    world.git(world.fork, "push", "-q", "origin", f"import/{r2}")
    world.git(world.fork, "switch", "-q", "-C", "main", "origin/main")
    world.git(world.fork, "merge", "-q", "--no-ff", "-m", f"Merge branch 'import/{r2}' into 'main'", f"import/{r2}")
    world.git(world.fork, "push", "-q", "origin", "main", f":import/{r2}")
    r3 = world.release({"lib/data.txt": with_line(world.up_text("lib/data.txt"), 5, "upstream 5")}, "Release 3")

    result = world.run(r3)

    tip3 = assert_ready(world, result, r3)
    assert [row.split()[:3] for row in reapplied(world, r3)] == [
        ["reapplied", "GL#11", new[0]], ["reapplied", "GL#12", new[1]], ["reapplied", "GL#13", new[2]]]
    assert world.git(world.fork, "rev-parse", f"{tip3}~4^2").strip() == r3
    assert world.read("lib/data.txt").splitlines()[1:] == ["gap 12", "data 3", "data 4", "upstream 5"]


def test_conflicting_or_empty_patch_stays_set_aside_and_the_rest_continue(world):
    world.create_fork(world.r1)
    p21 = world.patch(21, {"README.md": with_line(world.read("README.md"), 2, "gap 2")},
                      "GL#21 conflicts\n\nGap-Patch: GL#21\n")
    p22 = world.patch(22, {"lib/data.txt": with_line(world.read("lib/data.txt"), 1, "same fix")},
                      "GL#22 upstream makes it too\n\nGap-Patch: GL#22\n")
    p23 = world.patch(23, {"src/app.py": with_line(world.read("src/app.py"), 5, "gap 23")},
                      "GL#23 survives\n\nGap-Patch: GL#23\n", "Title: survives\n")
    r2 = world.release({"README.md": with_line(world.up_text("README.md"), 2, "upstream 2"),
                        "lib/data.txt": with_line(world.up_text("lib/data.txt"), 1, "same fix")}, "Release 2")

    result = world.run(r2)

    tip = assert_ready(world, result, r2)
    rows = reapplied(world, r2)
    assert rows[:2] == [f"set-aside GL#21 {p21}", f"set-aside GL#22 {p22}"]
    assert rows[2].split()[:3] == ["reapplied", "GL#23", p23]
    notes = (f"GL#21 {p21}: not re-applied on upstream release {r2}; rework or retire\n"
             f"GL#22 {p22}: not re-applied on upstream release {r2}; rework or retire\n")
    assert (world.releases / r2 / "issue-notes.txt").read_text() == notes
    for note in notes.splitlines():
        assert f"  {note}\n" in result.stdout
    assert world.read("gap/patches.md") == TITLE + f"## GL#23 {rows[2].split()[3]}\n- path: src/app.py\nTitle: survives\n"
    assert world.read("README.md") == world.up_text("README.md")
    assert world.drift(r2, tip) == {"src/app.py"}


# -- stops ---------------------------------------------------------------------

def test_unexpected_merge_conflict_stops_and_a_rerun_resets_to_the_same_stop(world):
    world.create_fork(world.r1)
    world.stray({"README.md": with_line(world.read("README.md"), 4, "gap 4")})
    r2 = world.release({"README.md": with_line(world.up_text("README.md"), 4, "upstream 4")}, "Release 2")
    expected = (f"STOP: conflict in: README.md; an unledgered change on main overlaps the release: rerun resets, "
                f'then open GitLab issue "Release import {r2} stopped" with the last 20 lines of this output')

    first = world.run(r2)
    assert_one_stop(first, expected)
    assert (world.fork / ".git" / "MERGE_HEAD").is_file()

    again = world.run(r2)
    assert_one_stop(again, expected)
    assert "Aborting the unfinished import's merge, revert or cherry-pick" in again.stdout


def test_an_unledgered_change_on_top_of_a_patch_stops_the_set_aside(world):
    world.create_fork(world.r1)
    p71 = world.patch(71, {"README.md": with_line(world.read("README.md"), 6, "gap 71")},
                      "GL#71\n\nGap-Patch: GL#71\n")
    world.stray({"README.md": with_line(world.read("README.md"), 6, "unledgered on top")})
    r2 = world.release({"src/app.py": world.up_text("src/app.py") + "more\n"}, "Release 2")

    result = world.run(r2)

    assert_one_stop(result, f"STOP: cannot set aside gap patch {p71}; an unledgered change overlaps it: rerun resets, "
                            f'then open GitLab issue "Release import {r2} stopped" with the last 20 lines of this output')


def test_a_stray_change_outside_the_site_layer_is_caught_by_step_7(world):
    world.create_fork(world.r1)
    world.patch(31, {"src/app.py": with_line(world.read("src/app.py"), 7, "gap 31")}, "GL#31\n\nGap-Patch: GL#31\n")
    world.stray({"src/stray.py": "unledgered\n"})
    r2 = world.release({"README.md": world.up_text("README.md") + "more\n"}, "Release 2")

    result = world.run(r2)

    assert_one_stop(result, "STOP: upstream-owned paths differ from the ledger (above: < ledgered only, "
                             "> changed only); ledger the change or revert it on main through an MR, then rerun")
    assert "> src/stray.py\n" in result.stdout
    assert "\n< " not in result.stdout


REFUSALS = {
    "bad-signature": "STOP: the bundle is not signed by SNEAKERNET_TRUSTED_PUB (FAIL line above); do not import it: "
                     "ask for a correctly signed bundle",
    "other-key": "STOP: the bundle is not signed by SNEAKERNET_TRUSTED_PUB (FAIL line above); do not import it: "
                 "ask for a correctly signed bundle",
    "corrupt-member": "STOP: a bundle member does not match the signed SHA256SUMS; do not import it: copy the "
                      "tarball again or ask for a new bundle",
    "no-anchor": "STOP: no trust anchor: SNEAKERNET_TRUSTED_PUB must name the release public key received out of "
                 "band (there is no trust on first use); ask the gap maintainer for it, then rerun",
    "missing-anchor-file": "STOP: no trust anchor: SNEAKERNET_TRUSTED_PUB must name the release public key "
                           "received out of band (there is no trust on first use); ask the gap maintainer for it, "
                           "then rerun",
    "head-differs": "STOP: repo.bundle HEAD is not the MANIFEST sha {sha}; do not import it: ask for a new bundle",
    "equal": "STOP: {sha} is already imported (origin/main contains it: it is the last import or older); "
             "there is nothing to import",
    "older": "STOP: {sha} is already imported (origin/main contains it: it is the last import or older); "
             "there is nothing to import",
    "not-descendant": "STOP: {sha} does not descend from the last import {prev}; import the releases in order, "
                      "or ask the gap maintainer",
    "tracks-gap": "STOP: the release tracks gap/ or .gitlab/; do not import it: report it upstream",
    "ledger-duplicate": "STOP: gap/patches.md lists gap patch {dup} twice; fix the ledger on main through an MR, "
                        "then rerun",
    "ledger-unknown": "STOP: gap/patches.md names {dup}, which is not a commit on origin/main; fix the ledger on main "
                      "through an MR, then rerun",
    "in-progress": "STOP: a merge, revert, cherry-pick or rebase is in progress in FORK outside an unfinished import; "
                   "finish or abort it (git status shows which), then rerun",
    "backslash": "STOP: R must not contain a backslash (the release bootstrap cannot handle one); use another path "
                 "and rerun",
    "r-inside-fork": "STOP: R must be outside FORK; choose R outside the clone and rerun",
    "ledger-heading": 'STOP: gap/patches.md line 4 is not a ledger heading "## GL#NNN <full commit sha>"; '
                      "fix the ledger on main through an MR, then rerun",
    "dirty": "STOP: {fork} has local changes (listed above); commit or remove them, or ask the gap maintainer, "
             "then rerun",
    "r-not-sha": "STOP: R must end in /{sha} (R=/abs/path/releases/{sha}), because the reset deletes R; "
                 "fix R and rerun",
    "r-holds-fork": "STOP: FORK, BUNDLE and SNEAKERNET_TRUSTED_PUB must be outside R, because the reset deletes R; "
                    "move them or choose another R, then rerun",
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_refusals_change_nothing(world, case, tmp_path):
    extra: dict[str, str | None] = {}
    prev, dup = world.r1, ""
    if case == "older":
        r2 = world.release({"README.md": "two\n"}, "Release 2")
        world.create_fork(r2)
        prev, sha = r2, world.r1
    else:
        world.create_fork(world.r1)
        sha = world.r1
    if case == "bad-signature":
        sha = world.release({"README.md": "two\n"}, "Release 2", tamper=lambda stage: (stage / "SHA256SUMS").write_text(
            (stage / "SHA256SUMS").read_text() + "0" * 64 + "  injected\n"))
    elif case == "other-key":
        sha = world.release({"README.md": "two\n"}, "Release 2", key=gen_sign_keypair(tmp_path / "home", "other.key"))
    elif case == "corrupt-member":
        sha = world.release({"README.md": "two\n"}, "Release 2",
                            tamper=lambda stage: (stage / "sbom.json").write_bytes(b"{}\n"))
    elif case in ("no-anchor", "missing-anchor-file"):
        sha = world.release({"README.md": "two\n"}, "Release 2")
        extra["SNEAKERNET_TRUSTED_PUB"] = None if case == "no-anchor" else str(world.root / "trust" / "absent.pub")
    elif case == "head-differs":
        world.release({"README.md": "two\n"}, "Release 2")
        sha = world.commit(world.up, {"README.md": "three\n"}, "Release 3")
        world.git(world.up, "reset", "-q", "--hard", "HEAD~1")  # repo.bundle HEAD: Release 2.
        world.bundle(world.git(world.up, "rev-parse", "HEAD").strip(), name_sha=sha)
    elif case == "not-descendant":
        world.git(world.up, "switch", "-q", "--orphan", "side")
        shipped = {relative: (REPO / relative).read_text() for relative in SHIPPED}
        copy_task_tools(world.up)
        sha = world.release({**shipped, "README.md": "side\n"}, "Unrelated release")
    elif case == "tracks-gap":
        sha = world.release({"gap/README.md": "upstream must not\n"}, "Release 2")
    elif case == "ledger-heading":
        world.commit(world.fork, {"gap/patches.md": TITLE + "## Notes\n"}, "A section the ledger format lacks")
        world.git(world.fork, "push", "-q", "origin", "main")
        sha = world.release({"README.md": "two\n"}, "Release 2")
    elif case in ("ledger-duplicate", "ledger-unknown"):
        dup = world.patch(61, {"src/app.py": "gap\n"}, "GL#61\n\nGap-Patch: GL#61\n")
        if case == "ledger-unknown":
            dup = "1" * 40
        world.commit(world.fork, {"gap/patches.md": world.read("gap/patches.md") + f"## GL#62 {dup}\n"}, "Ledger")
        world.git(world.fork, "push", "-q", "origin", "main")
        sha = world.release({"README.md": "two\n"}, "Release 2")
    elif case == "in-progress":
        sha = world.release({"README.md": "two\n"}, "Release 2")
        world.git(world.fork, "switch", "-q", "-c", "topic")
        world.commit(world.fork, {"README.md": "topic\n"}, "Topic")
        world.git(world.fork, "switch", "-q", "main")
        world.commit(world.fork, {"README.md": "main\n"}, "Main")
        subprocess.run(["git", "merge", "topic"], cwd=world.fork, env=world.env(), capture_output=True, check=False)
        assert (world.fork / ".git" / "MERGE_HEAD").is_file()
    elif case == "backslash":
        sha = world.release({"README.md": "two\n"}, "Release 2")
        extra["R"] = str(world.root / "back\\slash" / sha)
    elif case == "r-inside-fork":
        sha = world.release({"README.md": "two\n"}, "Release 2")
        extra["R"] = str(world.fork / "releases" / sha)
    elif case == "dirty":
        sha = world.release({"README.md": "two\n"}, "Release 2")
        (world.fork / "README.md").write_text("operator edit\n")
    elif case == "r-not-sha":
        sha = world.release({"README.md": "two\n"}, "Release 2")
        extra["R"] = str(world.releases / "current")
    elif case == "r-holds-fork":
        sha = world.release({"README.md": "two\n"}, "Release 2")
        world.git(world.root, "clone", "-q", str(world.bare), str(world.releases / sha / "fork"))
        extra["FORK"] = str(world.releases / sha / "fork")
    fork_refs, bare_refs = world.refs(world.fork), world.refs(world.bare)
    status = world.git(world.fork, "status", "--porcelain")

    result = world.run(sha, extra=extra)

    assert_one_stop(result, REFUSALS[case].format(sha=sha, prev=prev, fork=world.fork, dup=dup))
    if case != "r-holds-fork":  # That case runs on a second clone.
        assert world.refs(world.fork) == fork_refs
        assert world.git(world.fork, "status", "--porcelain") == status
    assert world.refs(world.bare) == bare_refs


# -- members the signature does not cover ---------------------------------------

def _member(name: str, kind: bytes = tarfile.REGTYPE, link: str = "", data: bytes = b"planted\n"):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    if kind == tarfile.REGTYPE:
        info.size = len(data)
        return info, io.BytesIO(data)
    return info, None


NOT_REGULAR = ("STOP: the tarball holds a member that is not a regular file (a link, directory or device); "
               "do not import it: ask for a new bundle")
NOT_PLAIN = ("STOP: the tarball holds a member that is not a plain file name (a path, .. or an unusual character) "
             "or holds one name twice; do not import it: ask for a new bundle")
UNLISTED = ("STOP: the tarball holds a member that the signed SHA256SUMS does not list; do not import it: "
            "ask for a new bundle")
HOSTILE = {
    # The review's T1: a wrongly signed bundle aims a scratch name at a file outside R.
    "symlink-wrong-key": (True, lambda w: [_member("empty.env", tarfile.SYMTYPE, str(w.root / "victim.txt"))],
                          NOT_REGULAR),
    # The review's T1b: a correctly signed bundle aims another scratch name at it.
    "symlink-signed": (False, lambda w: [_member("patches.before", tarfile.SYMTYPE, str(w.root / "victim.txt"))],
                       NOT_REGULAR),
    "hardlink": (False, lambda w: [_member("reapplied.txt", tarfile.LNKTYPE, str(w.root / "victim.txt"))],
                 NOT_REGULAR),
    "directory": (False, lambda w: [_member("release", tarfile.DIRTYPE)], NOT_REGULAR),
    "dotdot": (False, lambda w: [_member("../../escaped.txt")], NOT_PLAIN),
    "absolute": (False, lambda w: [_member(str(w.root / "escaped.txt"))], NOT_PLAIN),
    "repeated-name": (False, lambda w: [_member("sbom.json", data=b"{}\n")], NOT_PLAIN),
    "unlisted": (False, lambda w: [_member("notes.txt")], UNLISTED),
}


def _outside(world: World, sha: str) -> dict[str, tuple[bytes, int]]:
    """Every file under the world root except the run's own: R, the fork's
    git directory (step 0 fetches) and TMPDIR."""
    skip = (world.releases / sha, world.fork / ".git", world.root / "tmp")
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(world.root.rglob("*"))
            if p.is_file() and not p.is_symlink() and not any(p.is_relative_to(s) for s in skip)}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_unsigned_members_are_refused_before_use_and_the_next_signed_import_passes(world, case):
    world.create_fork(world.r1)
    world.patch(81, {"src/app.py": with_line(world.read("src/app.py"), 4, "gap 81")}, "GL#81\n\nGap-Patch: GL#81\n")
    (world.root / "victim.txt").write_text("keep\n")
    wrong_key, members, expected = HOSTILE[case]
    key = gen_sign_keypair(world.root / "home", "other.key") if wrong_key else None
    r2 = world.release({"README.md": world.up_text("README.md") + "more\n"}, "Release 2", key=key, append=members)
    before = _outside(world, r2)
    fork_refs = world.refs(world.fork)

    result = world.run(r2)

    assert_one_stop(result, expected)
    assert (world.root / "victim.txt").read_text() == "keep\n"
    assert not (world.releases / "escaped.txt").exists() and not (world.root / "escaped.txt").exists()
    assert _outside(world, r2) == before
    assert world.refs(world.fork) == fork_refs

    world.bundle(r2)  # The same release, packed and signed as pack.sh does.
    assert_ready(world, world.run(r2), r2)
    assert (world.root / "victim.txt").read_text() == "keep\n"


# -- idempotence -----------------------------------------------------------------

def _snapshot(world: World, sha: str) -> dict[str, object]:
    out = world.releases / sha
    files = {str(p.relative_to(out)): (p.read_bytes(), p.stat().st_mtime_ns)
             for p in sorted(out.rglob("*")) if p.is_file() and not p.is_symlink()}
    return {"fork": world.refs(world.fork), "bare": world.refs(world.bare),
            "head": world.git(world.fork, "symbolic-ref", "HEAD"),
            "status": world.git(world.fork, "status", "--porcelain", "--ignored"), "files": files}


def _patched_world(world: World) -> str:
    world.create_fork(world.r1)
    world.patch(41, {"README.md": with_line(world.read("README.md"), 2, "gap 41")}, "GL#41\n\nGap-Patch: GL#41\n")
    world.patch(42, {"src/app.py": with_line(world.read("src/app.py"), 9, "gap 42")}, "GL#42\n\nGap-Patch: GL#42\n")
    world.patch(43, {"lib/data.txt": with_line(world.read("lib/data.txt"), 4, "gap 43")}, "GL#43\n\nGap-Patch: GL#43\n")
    return world.release({"README.md": with_line(world.up_text("README.md"), 2, "upstream 2"),
                          "src/app.py": world.up_text("src/app.py") + "app 21\n"}, "Release 2")


def test_a_rerun_after_success_changes_nothing(world):
    r2 = _patched_world(world)
    assert_ready(world, world.run(r2), r2)
    before = _snapshot(world, r2)

    again = world.run(r2)

    assert_ready(world, again, r2)
    assert f"import/{r2} is already imported and unchanged since IMPORT READY; nothing changed" in again.stdout
    assert _snapshot(world, r2) == before

    # origin/main moved after IMPORT READY: the untouched branch is rebuilt on it.
    other = world.root / "maintainer"
    world.git(world.root, "clone", "-q", str(world.bare), str(other))
    moved = world.commit(other, {"gap/records/2026-10-30-note.md": "note\n"}, "Record")
    world.git(other, "push", "-q", "origin", "main")
    rebuilt = world.run(r2)
    tip = assert_ready(world, rebuilt, r2)
    assert world.git(world.fork, "merge-base", tip, moved).strip() == moved

    # A follow-up commit after IMPORT READY (§4: keys in gap/config/) is never
    # reset away: a rerun stops and keeps it.
    keys = world.commit(world.fork, {"gap/config/airgap.env": "NEW_SITE_KEY=x\n"}, "Apply key changes")
    after_follow_up = world.run(r2)
    assert_one_stop(after_follow_up, (
        f"STOP: import/{r2} changed after IMPORT READY; push it as it is with: "
        f"git -C '{world.fork}' push origin 'import/{r2}', and fix forward on its MR, or ask the gap maintainer"))
    assert world.git(world.fork, "rev-parse", f"import/{r2}").strip() == keys


CRASH = """#!/bin/sh
sub=$1
[ "$sub" != -C ] || sub=$3
if [ "$sub" = "$CRASH_SUB" ]; then
    n=$(cat "$CRASH_COUNT" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "$CRASH_COUNT"
    if [ "$n" = "$CRASH_AT" ]; then
        case "$CRASH_MODE" in
            before) kill -KILL 0 ;;
            fail) exit 1 ;;
            after) "$REAL_GIT" "$@"; kill -KILL 0 ;;
        esac
    fi
fi
exec "$REAL_GIT" "$@"
"""


def _crash(world: World, sha: str, sub: str, at: int, mode: str):
    """Run the import with git interrupted at the at-th call of sub."""
    wrap = world.root / "wrap"
    wrap.mkdir(exist_ok=True)
    (wrap / "git").write_text(CRASH)
    (wrap / "git").chmod(0o755)
    (wrap / "count").unlink(missing_ok=True)
    crashed = world.run(sha, path_prefix=str(wrap), extra={
        "CRASH_SUB": sub, "CRASH_AT": str(at), "CRASH_MODE": mode, "CRASH_COUNT": str(wrap / "count"),
        "REAL_GIT": shutil.which("git")})
    assert crashed.returncode != 0
    assert "IMPORT READY" not in crashed.stdout
    return crashed


def _straight(tmp_path: Path, keys) -> tuple[World, str, str]:
    """The uninterrupted reference run of _patched_world: world, release, tip."""
    straight = World(tmp_path / "straight", keys)
    r2 = _patched_world(straight)
    return straight, r2, assert_ready(straight, straight.run(r2), r2)


STASHED = re.compile(r'^Stashed the unfinished import\'s uncommitted changes as "import_release ([0-9a-f]{40}) '
                     r'(\d{8}T\d{6}Z)"; restore them with: git -C (.+) stash apply ([0-9a-f]{40})$', re.MULTILINE)


@pytest.mark.parametrize(("sub", "at", "mode"), [
    ("clone", 1, "after"),        # step 1: the bootstrap's workspace clone
    ("revert", 2, "after"),       # step 4: between two reverts
    ("commit", 1, "before"),      # step 4: the emptied ledger is not committed
    ("merge", 1, "after"),        # step 5: merged, not committed
    ("cherry-pick", 2, "after"),  # step 6: between two re-applies
    ("commit", 3, "before"),      # step 6: the re-applied ledger is not committed
    ("commit", 3, "fail"),        # step 6: a failing command, which STOPs
], ids=lambda value: str(value))
def test_resume_after_an_interruption_converges_to_the_uninterrupted_result(tmp_path, keys, sub, at, mode):
    straight, r2, _ = _straight(tmp_path, keys)

    world = World(tmp_path / "interrupted", keys)
    assert _patched_world(world) == r2  # Same content and dates: same commits.
    crashed = _crash(world, r2, sub, at, mode)
    if mode == "fail":
        assert_one_stop(crashed, f"STOP: step 6 (re-apply the gap patches) failed (exit 1); rerun the same command "
                                 f"to reset and resume; if it stops here again, open GitLab issue "
                                 f'"Release import {r2} stopped" with the last 20 lines of this output')

    left = world.git(world.fork, "status", "--porcelain")

    resumed = world.run(r2)

    tip = assert_ready(world, resumed, r2)
    assert tip == straight.git(straight.fork, "rev-parse", f"import/{r2}").strip()
    # The script's own commits are rebuilt, never kept under a second name.
    assert world.refs(world.fork, stash=False) == straight.refs(straight.fork)
    assert reapplied(world, r2) == reapplied(straight, r2)
    assert world.git(world.fork, "status", "--porcelain") == ""
    # Uncommitted state left on the import branch is stashed, never discarded.
    stashed = STASHED.findall(resumed.stdout)
    assert len(stashed) == (1 if left else 0), resumed.stdout[-3000:]
    if stashed:
        assert world.git(world.fork, "stash", "list", "--format=%gs") == (
            f"On import/{r2}: import_release {r2} {stashed[0][1]}\n")


# -- step 0 never loses work -----------------------------------------------------

DIRTY = "STOP: {fork} has local changes (listed above); commit or remove them, or ask the gap maintainer, then rerun"


@pytest.mark.parametrize("where", ["import-branch", "other-branch"])
def test_step_0_stops_before_setting_anything_aside_then_stashes_and_converges(tmp_path, keys, where):
    """The review's T2: an untracked file next to a tracked edit stops step 0
    before the edit is touched. Once the untracked file is gone, the edit is
    stashed under a printed name (recoverable), never discarded, and the
    import ends as the uninterrupted run did. On another branch any change
    stops the run; nothing there is ever set aside."""
    straight, r2, straight_tip = _straight(tmp_path, keys)
    world = World(tmp_path / "interrupted", keys)
    assert _patched_world(world) == r2
    _crash(world, r2, "cherry-pick", 2, "after")  # An unfinished import/<SHA> with the script's commits.
    if where == "other-branch":
        world.git(world.fork, "switch", "-q", "main")
    edit = "operator edit\n"
    (world.fork / "README.md").write_text(edit)
    (world.fork / "notes.txt").write_text("untracked\n")
    refs, status = world.refs(world.fork), world.git(world.fork, "status", "--porcelain")

    stopped = world.run(r2)

    assert_one_stop(stopped, DIRTY.format(fork=world.fork))
    assert world.refs(world.fork) == refs  # No stash, no deleted or renamed branch.
    assert world.git(world.fork, "status", "--porcelain") == status
    assert world.read("README.md") == edit
    assert "Stashed" not in stopped.stdout and "Discarding" not in stopped.stdout

    (world.fork / "notes.txt").unlink()
    if where == "other-branch":
        again = world.run(r2)
        assert_one_stop(again, DIRTY.format(fork=world.fork))
        assert world.read("README.md") == edit and world.refs(world.fork) == refs
        world.git(world.fork, "checkout", "--", "README.md")  # The operator's own call.

    resumed = world.run(r2)

    tip = assert_ready(world, resumed, r2)
    assert tip == straight_tip
    assert world.refs(world.fork, stash=False) == straight.refs(straight.fork)
    stashed = STASHED.findall(resumed.stdout)
    if where == "other-branch":
        assert stashed == [] and world.git(world.fork, "stash", "list") == ""
        return
    [(name_sha, stamp, fork, commit)] = stashed
    assert name_sha == r2 and shlex.split(fork) == [str(world.fork)]
    assert world.git(world.fork, "stash", "list", "--format=%gs") == f"On import/{r2}: import_release {r2} {stamp}\n"
    assert world.git(world.fork, "rev-parse", "refs/stash").strip() == commit
    world.git(world.fork, "stash", "apply", "-q", commit)
    assert world.read("README.md") == edit


@pytest.mark.parametrize("where", ["import-branch", "other-branch"])
def test_commits_added_to_an_unfinished_import_are_kept_under_a_printed_name(tmp_path, keys, where):
    """The review's T3: a commit someone added to an unfinished import/<SHA>
    is kept as import/<SHA>-<UTC timestamp>, and the import still ends as the
    uninterrupted run did."""
    straight, r2, straight_tip = _straight(tmp_path, keys)
    world = World(tmp_path / "interrupted", keys)
    assert _patched_world(world) == r2
    _crash(world, r2, "cherry-pick", 2, "after")
    added = world.commit(world.fork, {"src/app.py": world.read("src/app.py") + "operator fix\n"}, "Operator fix")
    if where == "other-branch":
        world.git(world.fork, "switch", "-q", "main")

    resumed = world.run(r2)

    tip = assert_ready(world, resumed, r2)
    assert tip == straight_tip
    kept = re.findall(rf"^Kept the unfinished import's commits as branch (import/{r2}-\d{{8}}T\d{{6}}Z)$",
                      resumed.stdout, re.MULTILINE)
    assert len(kept) == 1, resumed.stdout[-3000:]
    assert world.git(world.fork, "rev-parse", f"refs/heads/{kept[0]}").strip() == added
    refs = world.refs(world.fork).replace(f"refs/heads/{kept[0]} {added}\n", "")
    assert refs == straight.refs(straight.fork)


# The documented heading is "## GL#NNN <full commit sha>"; each of these only
# looks like one. CRLF and a lowercase gl# were refused before.
NEAR_MISS = {
    "no-space": "##GL#41 {sha}",
    "leading-space": " ## GL#41 {sha}",
    "tab": "##\tGL#41 {sha}",
    "leading-tab": "\t## GL#41 {sha}",
    "three-hashes": "### GL#41 {sha}",
    "lowercase": "## gl#41 {sha}",
    "crlf": "## GL#41 {sha}\r",
}


@pytest.mark.parametrize("case", sorted(NEAR_MISS))
def test_a_near_miss_ledger_heading_stops_and_the_fixed_ledger_converges(tmp_path, keys, case):
    """The review's T4: a near-miss heading would hide its gap patch from the
    set-aside. It stops the import before the branch exists; after the ledger
    is fixed on main, the import matches the uninterrupted run."""
    straight, r2, straight_tip = _straight(tmp_path, keys)
    world = World(tmp_path / "near miss", keys)
    assert _patched_world(world) == r2
    good = world.read("gap/patches.md")
    p41 = good.splitlines()[3].split()[2]
    world.commit(world.fork, {"gap/patches.md": with_line(good, 4, NEAR_MISS[case].format(sha=p41))}, "Near miss")
    world.git(world.fork, "push", "-q", "origin", "main")
    refs = world.refs(world.fork)

    stopped = world.run(r2)

    assert_one_stop(stopped, 'STOP: gap/patches.md line 4 is not a ledger heading "## GL#NNN <full commit sha>"; '
                             "fix the ledger on main through an MR, then rerun")
    assert world.refs(world.fork) == refs
    assert world.git(world.fork, "status", "--porcelain") == ""

    world.git(world.fork, "switch", "-q", "main")  # Step 0 left FORK detached at origin/main.
    world.commit(world.fork, {"gap/patches.md": good}, "Fix the ledger heading")
    world.git(world.fork, "push", "-q", "origin", "main")
    tip = assert_ready(world, world.run(r2), r2)
    # main gained two commits, so the commit ids differ; the content does not.
    assert world.tree(tip) == straight.tree(straight_tip)
    sha = re.compile(r"[0-9a-f]{40}")
    assert sha.sub("<sha>", world.git(world.fork, "show", f"{tip}:gap/patches.md")) == sha.sub(
        "<sha>", straight.git(straight.fork, "show", f"{straight_tip}:gap/patches.md"))
    assert [row.split()[:2] for row in reapplied(world, r2)] == [row.split()[:2] for row in reapplied(straight, r2)]


# -- round trips -----------------------------------------------------------------

def test_names_ledger_text_and_messages_round_trip_exactly(tmp_path, keys):
    world = World(tmp_path / "it's $HOME & more", keys)
    world.create_fork(world.r1)
    odd = "Fix \"quoted\" & $HOME \\ back\\\\slash 'single' — ünïcödé  two  spaces"
    message = f"{odd}\n\n#not a comment\ntrailing spaces   \n\tindented\n\nGap-Patch: GL#51\n"
    free = f"Title: {odd}  \nNotes: *star* [x](y) ~tilde $(echo never) `run`\n\n"
    p51 = world.patch(51, {"docs/with space.md": "gap\n", "src/ünï.py": "gap\n"}, message, free)
    p52 = world.patch(52, {"README.md": with_line(world.read("README.md"), 3, "gap 52")},
                      "GL#52 set aside\n\nGap-Patch: GL#52\n")
    r2 = world.release({"README.md": with_line(world.up_text("README.md"), 3, "upstream 3")}, "Release 2")

    result = world.run(r2)

    tip = assert_ready(world, result, r2)
    [row, aside] = reapplied(world, r2)
    assert row.split()[:3] == ["reapplied", "GL#51", p51] and aside == f"set-aside GL#52 {p52}"
    copy = row.split()[3]
    assert world.message(copy) == world.message(p51) == message.encode()
    assert world.read("gap/patches.md") == (
        TITLE + f"## GL#51 {copy}\n- path: docs/with space.md\n- path: src/ünï.py\n{free}")
    assert world.drift(r2, tip) == {"docs/with space.md", "src/ünï.py"}

    push = result.stdout.splitlines()[-2].strip()
    assert shlex.split(push) == ["git", "-C", str(world.fork), "push", "origin", f"import/{r2}"]
    subprocess.run(["sh", "-c", push], env=world.env(), check=True, capture_output=True)
    assert world.git(world.bare, "rev-parse", f"refs/heads/import/{r2}").strip() == tip

    after_push = world.run(r2)
    assert_one_stop(after_push, f"STOP: import/{r2} is already pushed to origin; never rerun after the push: "
                                "fix forward with new commits on its MR branch")
    assert world.git(world.fork, "rev-parse", f"import/{r2}").strip() == tip
