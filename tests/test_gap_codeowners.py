"""scripts/gap_codeowners.py: the gap fork's GitLab CODEOWNERS (GH#1118 F4, ADR-0009 section 6).

The protected list must equal the Verifier's list plus the gap gates the ADR
names, written here independently. The group section must own every tracked
path except gap/ using only GitLab 17.0 syntax (sections and plain path
entries; no `!` exclusion, which needs 17.10+). Generated files are checked
line by line, owners round-trip exactly, and every weakened or hand-edited
file, missing file and shadowing CODEOWNERS fails the real hygiene step.

Under the 17.0 enumeration a new top-level name (a root CODEOWNERS included)
matches no entry, so the hygiene step is the guard against a shadowing file:
it must run first in its job, isolated (`python3 -I`), and execute nothing a
group-only change can alter (#1176 review B1, run here against a simulated
fork commit).
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from scripts import gap_codeowners as codeowners
from scripts.acceptance import VERIFICATION_INPUTS, verification_inputs
from scripts.acceptance_evidence import PRODUCERS
from scripts.unit_evidence import INPUTS as UNIT_INPUTS

from tests import helpers_signing_closure as closure

ROOT = Path(__file__).resolve().parents[1]
GROUP = '@site/gap-maintainers'
APPROVERS = ['@first.approver', '@second_approver-2']
# ADR-0009 section 6, written out: the gap's own gates. The release pipeline
# and signing files are today's bundle and kit signers (F6 extends this), plus
# the generator the hygiene job runs (#1176 review).
GAP_GATES = {'.gitlab-ci.yml', '.gitlab/CODEOWNERS', 'scripts/check_junit_clean.py', 'scripts/gate_l1.py',
             'scripts/tools/devkit-pin.txt', 'scripts/airgap/pack.sh', 'scripts/devkit/pack.sh',
             'scripts/gap_codeowners.py',
             # Every code-owner file GitLab reads first: adding one needs the
             # approver even if the hygiene step were neutralised (#1176 B1).
             'CODEOWNERS', 'docs/CODEOWNERS'}
# ADR-0009 section 6, written out: the entry points that pack, sign or verify a
# release or kit (#588 N1). Every file they run or read by name is protected.
SIGNING_ENTRY_POINTS = {'scripts/airgap/pack.sh', 'scripts/airgap/bootstrap.sh', 'scripts/airgap/load.sh',
                        'scripts/devkit/pack.sh', 'scripts/devkit/install.sh', 'scripts/gap/import_release.sh'}
# Isolated: no script directory or cwd on sys.path and no PYTHON* variables,
# so no group-owned file can shadow a module the check imports.
CHECK_STEP = 'python3 -I scripts/gap_codeowners.py check'


def gitlab_glob(pattern: str, path: str) -> bool:
    """A root-anchored GitLab CODEOWNERS file pattern: `*` never crosses `/`."""
    parts, names = pattern.lstrip('/').split('/'), path.split('/')
    return len(parts) == len(names) and all(fnmatch.fnmatchcase(n, p) for p, n in zip(parts, names, strict=True))


def entries(text: str, section: str) -> list[str]:
    lines = text.split('\n')
    body = lines[lines.index(section) + 1:]
    return body[:body.index('')] if '' in body else body


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / 'scripts/gap_codeowners.py'), '--root', str(root), *args],
                          capture_output=True, text=True, check=False)


# A small fork tree: hidden and plain top-level files and directories, and gap/.
TREE = ('.gitlab-ci.yml', '.github/workflows/ci.yml', 'AGENTS.md', 'docs/adr/x.md', 'gap/README.md',
        'gap/config/airgap.env')
GROUP_OWNED = ['.github/', '.gitlab-ci.yml', '.gitlab/', 'AGENTS.md', 'docs/']


def git(root: Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def tracked(root: Path, *paths: str) -> None:
    if not (root / '.git').exists():
        git(root, 'init', '-q')
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text('x\n')
    if paths:
        git(root, 'add', '--', *paths)


def written(tmp_path: Path) -> Path:
    tracked(tmp_path, *TREE)
    assert codeowners.main(['--root', str(tmp_path), 'write', '--group', GROUP,
                            *(a for name in APPROVERS for a in ('--approver', name))]) == 0
    return tmp_path / '.gitlab/CODEOWNERS'


def test_protected_set_is_the_verifier_list_plus_the_named_gap_gates_and_the_signing_closure():
    workflows = {'.github/workflows/' + p.workflow for p in PRODUCERS}
    signing = closure.derive(ROOT, SIGNING_ENTRY_POINTS)
    assert set(codeowners.protected_paths()) == {*VERIFICATION_INPUTS, 'taskfiles/*.yml', *workflows, *GAP_GATES,
                                                 *signing}
    assert set(UNIT_INPUTS) <= set(codeowners.protected_paths())
    assert not any(p.startswith('gap/') for p in codeowners.protected_paths())


def test_every_file_the_signing_entry_points_run_is_protected_and_the_list_tracks_the_scripts():
    # #588 N1: a group-only change to code a signer or verifier runs (common.sh
    # holds the anchor checks and check_manifest_chain) could change what is
    # packed, signed or accepted. The closure is derived from the scripts, and
    # the generator's copy must equal it: a new dependency fails here until it
    # is protected, and a dropped one leaves the list.
    signing = closure.derive(ROOT, SIGNING_ENTRY_POINTS)
    assert {'scripts/airgap/common.sh', 'scripts/airgap/image_identity.sh', 'scripts/airgap/image_manifest.py',
            'scripts/license_inventory.py', 'scripts/tools/task-artifact.sh', 'licenses/inventory.json',
            'scripts/airgap/reference_docs.py', 'images.txt', *SIGNING_ENTRY_POINTS} <= signing
    assert sorted(signing - set(codeowners.protected_paths())) == []
    assert set(codeowners.SIGNING_ENTRY_POINTS) == SIGNING_ENTRY_POINTS
    assert set(codeowners.SIGNING_CLOSURE) == signing
    assert not any(p.startswith('gap/') for p in signing)


def test_every_path_the_verifier_binds_is_protected_and_every_named_path_exists():
    bound = verification_inputs(ROOT)  # the real function acceptance hashes
    assert bound and all(any(gitlab_glob(p, path) for p in codeowners.protected_paths()) for path in bound)
    absent = {codeowners.TARGET, *codeowners.SHADOWING}
    named = [p for p in codeowners.protected_paths() if '*' not in p and p not in absent]
    assert [p for p in named if not (ROOT / p).is_file()] == []
    # Upstream tracks no code-owner file at all (F3 enforces .gitlab/).
    assert [p for p in codeowners.SHADOWING if (ROOT / p).exists() or (ROOT / p).is_symlink()] == []
    # In the fork, which tracks the file, it must already be current.
    if (ROOT / codeowners.TARGET).exists():
        assert codeowners.main(['--root', str(ROOT), 'check']) == 0


def test_generated_file_owns_everything_but_gap_and_names_approvers_on_the_protected_list(tmp_path):
    text = written(tmp_path).read_text()
    assert text.endswith('\n') and '\r' not in text and not any(l != l.rstrip() for l in text.split('\n'))
    # GitLab parses owners even inside comments, so no comment names one.
    assert not any('@' in line for line in text.split('\n') if line.startswith('#'))
    assert entries(text, '[Gap maintainers]') == [f'/{entry} {GROUP}' for entry in GROUP_OWNED]
    protected = entries(text, '[Protected list]')
    assert protected == [f'/{p} {" ".join(APPROVERS)}' for p in sorted(codeowners.protected_paths())]
    sections = [line for line in text.split('\n') if line.startswith('[')]
    assert sections == ['[Gap maintainers]', '[Protected list]']


def test_only_gitlab_17_0_syntax_is_used(tmp_path):
    # GitLab 17.0 CODEOWNERS: `#` comments, `[Section]` headers and `path owner...`
    # entries. Exclusions (`!`, 17.10+), optional sections (`^`) and approval
    # counts (`[Section][N]`) are not used; `*` appears only inside taskfiles/*.yml.
    for line in written(tmp_path).read_text().split('\n'):
        if not line or line.startswith('#'):
            continue
        if line.startswith('['):
            assert line in ('[Gap maintainers]', '[Protected list]')
            continue
        path, *owners = line.split(' ')
        assert path.startswith('/') and not path.startswith('/!') and owners
        assert '*' not in path.replace('/taskfiles/*.yml', '')


def test_the_group_owns_every_tracked_path_of_this_checkout_except_gap():
    listed = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True,
                            check=True).stdout.decode().split('\0')
    entries_ = codeowners.group_owned(ROOT)
    def owned(path: str) -> bool:
        return any(path == e or (e.endswith('/') and path.startswith(e)) for e in entries_)
    assert [p for p in filter(None, listed) if not p.startswith('gap/') and not owned(p)] == []
    assert not any(e.startswith('gap') for e in entries_) and '.gitlab/' in entries_


def test_a_new_top_level_path_fails_check_until_refresh(tmp_path):
    # The next ordinary operation after an import adds a top-level directory.
    target = written(tmp_path)
    tracked(tmp_path, 'newdir/file.py')
    assert run(tmp_path, 'check').returncode == 1
    assert run(tmp_path, 'refresh').returncode == 0
    assert f'/newdir/ {GROUP}' in entries(target.read_text(), '[Gap maintainers]')
    assert run(tmp_path, 'check').returncode == 0
    # Untracked files and anything under gap/ never change the output.
    (tmp_path / 'untracked').mkdir()
    (tmp_path / 'untracked/x').write_text('x\n')
    tracked(tmp_path, 'gap/runbooks/new.md')
    assert run(tmp_path, 'check').returncode == 0


@pytest.mark.parametrize('name', ['#comment', '!negated', '[section]', '^optional', 'two words', 'star*'])
def test_a_top_level_name_gitlab_would_misread_is_refused(tmp_path, name):
    tracked(tmp_path, 'AGENTS.md', name)
    result = run(tmp_path, 'write', '--group', GROUP, '--approver', APPROVERS[0])
    assert (result.returncode, result.stderr) == (1, codeowners.NAME_ERROR)
    assert not (tmp_path / '.gitlab').exists()


def test_write_outside_a_git_checkout_is_refused(tmp_path):
    result = run(tmp_path, 'write', '--group', GROUP, '--approver', APPROVERS[0])
    assert (result.returncode, result.stderr) == (1, codeowners.GIT_ERROR)


def test_owners_round_trip_through_check_and_refresh(tmp_path):
    target = written(tmp_path)
    before = target.read_bytes()
    assert codeowners.owners_of(before.decode()) == (GROUP, APPROVERS)
    assert run(tmp_path, 'check').returncode == 0
    assert run(tmp_path, 'refresh').returncode == 0
    assert target.read_bytes() == before
    # Owners are read back from the file, so a consistent change of owners is
    # current; that change is guarded because the file is on the protected list.
    target.write_text(before.decode().replace(GROUP, '@other/group'))
    assert run(tmp_path, 'check').returncode == 0
    assert codeowners.TARGET in codeowners.protected_paths()


def _drop_protected(text: str) -> str:
    return text.replace('/scripts/gate_l1.py ' + ' '.join(APPROVERS) + '\n', '')


MUTATIONS = {
    'a protected line dropped': _drop_protected,
    'one approver dropped from a line': lambda t: t.replace(
        '/Taskfile.yml ' + ' '.join(APPROVERS), '/Taskfile.yml ' + APPROVERS[0]),
    'an approver added to a line': lambda t: t.replace('/Taskfile.yml ', '/Taskfile.yml @extra '),
    'gap/ owned by the group': lambda t: t.replace(f'/docs/ {GROUP}\n', f'/docs/ {GROUP}\n/gap/ {GROUP}\n'),
    'a top-level entry dropped': lambda t: t.replace(f'/docs/ {GROUP}\n', ''),
    'a path excluded from the group': lambda t: t.replace(f'/docs/ {GROUP}\n', f'/docs/ {GROUP}\n!/docs/adr/\n'),
    'a catch-all added': lambda t: t.replace('[Gap maintainers]\n', f'[Gap maintainers]\n* {GROUP}\n'),
    'a second owner on a group line': lambda t: t.replace(f'/docs/ {GROUP}', f'/docs/ {GROUP} @other'),
    'hand-added unowned entry': lambda t: t + '/gap/extra.md ' + APPROVERS[0] + '\n',
    'lines reordered': lambda t: t.replace('/Taskfile.yml', '/Zz').replace('/.gitlab-ci.yml', '/Taskfile.yml')
    .replace('/Zz', '/.gitlab-ci.yml'),
    'CRLF line endings': lambda t: t.replace('\n', '\r\n'),
    'trailing whitespace': lambda t: t.replace(f'/docs/ {GROUP}\n', f'/docs/ {GROUP} \n'),
    'role owner instead of a named approver': lambda t: t.replace(' '.join(APPROVERS), '@@maintainer'),
    'header edited': lambda t: t.replace('Do not edit by hand', 'Edit freely'),
    'empty file': lambda t: '',
}


@pytest.mark.parametrize('mutation', MUTATIONS)
def test_check_refuses_every_hand_edited_file(tmp_path, mutation):
    target = written(tmp_path)
    target.write_bytes(MUTATIONS[mutation](target.read_text()).encode())
    result = run(tmp_path, 'check')
    assert (result.returncode, result.stdout) == (1, '')
    assert result.stderr == codeowners.DRIFT


@pytest.mark.parametrize('shadow', codeowners.SHADOWING)
def test_check_refuses_a_file_gitlab_would_read_first(tmp_path, shadow):
    written(tmp_path)
    (tmp_path / shadow).parent.mkdir(exist_ok=True)
    (tmp_path / shadow).write_text('* @anyone\n')
    result = run(tmp_path, 'check')
    assert (result.returncode, result.stderr) == (1, codeowners.SHADOWED.format(path=shadow))


def _symlinked(tmp_path: Path) -> Path:
    (tmp_path / 'real').mkdir()
    real = written(tmp_path / 'real')
    (tmp_path / '.gitlab').mkdir()
    (tmp_path / '.gitlab/CODEOWNERS').symlink_to(real)
    return real


@pytest.mark.parametrize('command, expected', [('check', 'DRIFT'), ('refresh', 'UNREADABLE')])
def test_a_missing_file_is_refused(tmp_path, command, expected):
    result = run(tmp_path, command)
    assert (result.returncode, result.stderr) == (1, getattr(codeowners, expected))
    assert not (tmp_path / '.gitlab').exists()


@pytest.mark.parametrize('command, expected', [('check', 'DRIFT'), ('refresh', 'UNREADABLE')])
def test_a_symlinked_file_is_refused_and_its_target_untouched(tmp_path, command, expected):
    real = _symlinked(tmp_path)
    before = real.read_bytes()
    result = run(tmp_path, command)
    assert (result.returncode, result.stderr) == (1, getattr(codeowners, expected))
    assert real.read_bytes() == before


def test_an_unwritable_target_fails_with_the_fixed_message(tmp_path):
    tracked(tmp_path, *TREE)
    (tmp_path / '.gitlab').write_text('not a directory\n')
    result = run(tmp_path, 'write', '--group', GROUP, '--approver', APPROVERS[0])
    assert (result.returncode, result.stdout, result.stderr) == (1, '', codeowners.IO_ERROR)


def test_refresh_after_an_import_regenerates_and_the_next_check_passes(tmp_path):
    # The operator path (ADR-0009 section 4): upstream adds a protected path,
    # the committed file lacks it, hygiene fails, refresh, hygiene passes.
    target = written(tmp_path)
    expected = target.read_bytes()
    target.write_text(_drop_protected(target.read_text()))
    assert run(tmp_path, 'check').returncode == 1
    assert run(tmp_path, 'refresh').returncode == 0
    assert target.read_bytes() == expected
    assert run(tmp_path, 'check').returncode == 0


def test_refresh_never_spreads_a_hand_weakened_line(tmp_path):
    target = written(tmp_path)
    weakened = MUTATIONS['one approver dropped from a line'](target.read_text())
    target.write_text(weakened)
    result = run(tmp_path, 'refresh')
    assert (result.returncode, result.stderr) == (1, codeowners.UNREADABLE)
    assert target.read_text() == weakened
    assert run(tmp_path, 'check').returncode == 1


SYNTAX = 'FAIL: give --group @group[/subgroup] and at least one --approver @user.\n'
REPEATED = 'FAIL: the group and the approvers must all be different.\n'


@pytest.mark.parametrize('group, approvers, expected', [
    ('@@maintainer', APPROVERS, SYNTAX),
    ('gap-maintainers', APPROVERS, SYNTAX),
    (GROUP, ['@a b'], SYNTAX),
    (GROUP, ['@user@example.com'], SYNTAX),
    (GROUP, ['@-dash-first'], SYNTAX),
    (GROUP, ['@site/gap-maintainers/sub'], SYNTAX),
    ('@approver', ['@approver'], REPEATED),
    (GROUP, ['@same', '@SAME'], REPEATED),
])
def test_write_refuses_unnamed_or_repeated_owners_and_writes_nothing(tmp_path, group, approvers, expected):
    result = run(tmp_path, 'write', '--group', group, *(a for n in approvers for a in ('--approver', n)))
    assert (result.returncode, result.stdout, result.stderr) == (1, '', expected)
    assert not (tmp_path / '.gitlab').exists()


def test_write_without_an_approver_is_a_usage_error(tmp_path):
    result = run(tmp_path, 'write', '--group', GROUP)
    assert result.returncode == 2 and '--approver' in result.stderr and 'Traceback' not in result.stderr
    assert not (tmp_path / '.gitlab').exists()


def test_the_gitlab_hygiene_job_runs_the_check_and_fails_on_drift(tmp_path):
    gitlab = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
    steps = gitlab['hygiene']['script']
    assert steps.count(CHECK_STEP) == 1 and 'allow_failure' not in gitlab['hygiene']
    # The real step, run from a checkout root as the job does.
    (tmp_path / 'scripts').symlink_to(ROOT / 'scripts')
    env = {**os.environ, 'PATH': os.pathsep.join([str(Path(sys.executable).parent), os.environ['PATH']])}

    def step() -> subprocess.CompletedProcess[str]:
        return subprocess.run(['sh', '-ec', CHECK_STEP], cwd=tmp_path, env=env, capture_output=True,
                              text=True, check=False)

    assert (step().returncode, step().stderr) == (1, codeowners.DRIFT)
    target = written(tmp_path)
    assert step().returncode == 0
    target.write_text(_drop_protected(target.read_text()))
    assert step().returncode == 1


def test_every_code_owner_file_gitlab_reads_is_protected():
    # GitLab reads the first of these; a merge request adding an earlier one
    # matches the protected line in the target branch's file.
    assert {'CODEOWNERS', 'docs/CODEOWNERS', '.gitlab/CODEOWNERS'} <= set(codeowners.protected_paths())
    assert set(codeowners.SHADOWING) <= set(codeowners.protected_paths())


def test_the_check_is_the_first_code_the_hygiene_job_runs():
    # A group-owned script run earlier in the job could change the workspace
    # the check reads (#1176 B1). Nothing runs before it: no before_script on
    # the job, its templates or the defaults.
    gitlab = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
    job = gitlab['hygiene']
    assert job['script'][0] == CHECK_STEP
    templates = job.get('extends', [])
    for name in [templates] if isinstance(templates, str) else templates:
        assert 'before_script' not in gitlab[name]
    assert 'before_script' not in job and 'before_script' not in gitlab.get('default', {})
    assert 'before_script' not in gitlab


def test_every_documented_invocation_runs_isolated():
    # Operators run write and refresh as the hygiene job runs check, so a
    # planted module cannot alter the generated file (maintainer, 2026-11-02).
    # Found by scanning the tracked tree for a subcommand after the script
    # name; tests drive the script through sys.executable, not as operators.
    names = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True).stdout
    found = []
    for name in names.decode().split('\0'):
        path = ROOT / name
        if not name or name.startswith('tests/') or not path.is_file():
            continue
        data = path.read_bytes()
        if b'gap_codeowners.py' not in data:
            continue
        for line in data.decode('utf-8', 'replace').splitlines():
            for match in re.finditer(r'gap_codeowners\.py\s+(write|refresh|check)\b', line):
                found.append((name, match[1], line.strip()))
                assert re.search(r"(?:^|[\s`'\"])python3 -I \S*$", line[:match.start()]), (name, line)
    assert {command for _, command, _ in found} == {'write', 'refresh', 'check'}, found


# The attack payload: when the code-owner check runs, report success and exit.
SHIM = ("import os, sys\n"
        "if sys.argv and sys.argv[0].endswith('gap_codeowners.py'):\n"
        "    sys.stdout.write('OK: .gitlab/CODEOWNERS equals the generator output.\\n')\n"
        "    sys.stdout.flush()\n"
        "    os._exit(0)\n")
# Standard-library names the check or its former imports load at run time.
STDLIB_NAMES = ('json', 'hashlib', 'tempfile', 'shutil', 're', 'argparse', 'subprocess', 'pathlib', 'zipfile')


def fork(tmp_path: Path) -> Path:
    """A simulated fork commit: this checkout's scripts/, gap/, and the generated file."""
    root = tmp_path / 'fork'
    root.mkdir()
    shutil.copytree(ROOT / 'scripts', root / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
    tracked(root, *TREE)
    git(root, 'add', '--', 'scripts')
    result = subprocess.run([sys.executable, '-I', str(root / 'scripts/gap_codeowners.py'), 'write', '--group',
                             GROUP, '--approver', APPROVERS[0]], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    commit(root)
    return root


def commit(root: Path) -> None:
    git(root, 'add', '-A')
    git(root, '-c', 'user.name=fork', '-c', 'user.email=fork@example.invalid', 'commit', '-q', '-m', 'fork')


def hygiene_step(root: Path) -> subprocess.CompletedProcess[str]:
    """The job's code-owner step, as written in .gitlab-ci.yml, run from the fork root."""
    steps = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())['hygiene']['script']
    step = next(s for s in steps if 'gap_codeowners.py check' in s)
    env = {**os.environ, 'PATH': os.pathsep.join([str(Path(sys.executable).parent), os.environ['PATH']])}
    return subprocess.run(['sh', '-ec', step], cwd=root, env=env, capture_output=True, text=True, check=False)


def test_a_root_codeowners_with_a_root_module_shadowing_the_stdlib_fails_the_step(tmp_path):
    # Reviewer variant 1: an MR adds only new top-level names, which no entry
    # of the 17.0 file owns, and a stdlib-shadowing module at the root and in
    # scripts/ (both group-owned) that reports success.
    root = fork(tmp_path)
    assert hygiene_step(root).returncode == 0
    (root / 'CODEOWNERS').write_text('* @attacker\n')
    for name in STDLIB_NAMES:
        (root / f'{name}.py').write_text(SHIM)
        (root / f'scripts/{name}.py').write_text(SHIM)
    commit(root)
    result = hygiene_step(root)
    assert (result.returncode, result.stdout, result.stderr) == (1, '', codeowners.SHADOWED.format(path='CODEOWNERS'))


def test_docs_codeowners_with_edited_group_owned_scripts_fails_the_step(tmp_path):
    # Reviewer variant 2: docs/CODEOWNERS plus a short edit to scripts the
    # check used to import (acceptance_evidence.py, scripts/__init__.py and the
    # rest): every unprotected script under scripts/ gets the payload.
    root = fork(tmp_path)
    protected = set(codeowners.protected_paths())
    edited = [p for p in sorted((root / 'scripts').rglob('*.py'))
              if str(p.relative_to(root)) not in protected]
    # scripts/__init__.py is now protected (#588 N1: license_inventory.py may
    # import it), so a group-only change can no longer edit it.
    assert root / 'scripts/acceptance_evidence.py' in edited and root / 'scripts/__init__.py' not in edited
    for path in edited:
        path.write_text(path.read_text() + '\n' + SHIM)
    (root / 'docs/CODEOWNERS').write_text('* @attacker\n')
    commit(root)
    result = hygiene_step(root)
    assert (result.returncode, result.stdout, result.stderr) == (1, '', codeowners.SHADOWED.format(path='docs/CODEOWNERS'))


def test_the_isolated_check_loads_no_module_from_the_checkout(tmp_path):
    # The check's whole import closure is the protected script plus the
    # standard library: nothing a group-only change can edit is executed.
    root = fork(tmp_path)
    script = root / 'scripts/gap_codeowners.py'
    probe = ('import runpy, sys\n'
             f'sys.argv = [{str(script)!r}, "check"]\n'
             'try:\n'
             f'    runpy.run_path({str(script)!r}, run_name="__main__")\n'
             'except SystemExit as stop:\n'
             '    assert stop.code == 0, stop.code\n'
             'for module in list(sys.modules.values()):\n'
             '    print(getattr(module, "__file__", None) or "")\n')
    result = subprocess.run([sys.executable, '-I', '-c', probe], cwd=root, capture_output=True, text=True,
                            check=False)
    assert result.returncode == 0, result.stderr
    loaded = [line for line in result.stdout.splitlines() if line.startswith(str(root))]
    assert loaded == []


# -- the signing closure derivation (tests/helpers_signing_closure.py) on fixture repositories --

def repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    if not (tmp_path / '.git').exists():
        git(tmp_path, 'init', '-q')
    git(tmp_path, 'add', '-A')
    return tmp_path


def derived(root: Path, *entries: str, reviewed: dict[tuple[str, str], tuple[str | None, str]] | None = None
            ) -> set[str]:
    # The fixtures review nothing and declare no root variable unless a test says so.
    return set(closure.derive(root, entries or ('scripts/pack.sh',), reviewed=reviewed or {}, root_variables={}))


PACK = ('#!/bin/sh\n'
        'set -eu\n'
        '. "$(dirname -- "$0")/common.sh"\n'
        'python3 "$REPO_ROOT/scripts/sign.py" --pin "$REPO_ROOT/pins/key.txt"\n'
        'echo "done: see docs/guide.md"\n')
COMMON = ('REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
          'cd "$REPO_ROOT"\n'
          'case "$x" in */*) die "a pattern, not a command" ;; esac\n')
SIGN = 'import json\nimport helper\n'
BASE = {'scripts/pack.sh': PACK, 'scripts/common.sh': COMMON, 'scripts/sign.py': SIGN,
        'scripts/helper.py': 'X = 1\n', 'pins/key.txt': 'k\n', 'docs/guide.md': 'g\n',
        'scripts/unrelated.sh': 'echo\n'}
BASE_CLOSURE = {'scripts/pack.sh', 'scripts/common.sh', 'scripts/sign.py', 'scripts/helper.py', 'pins/key.txt'}


def test_derivation_follows_sourcing_interpreters_imports_and_pins_only(tmp_path):
    # Not the message's docs/guide.md, not an unrelated script, not a case pattern.
    assert derived(repo(tmp_path, BASE)) == BASE_CLOSURE


def test_a_new_sourced_file_is_flagged(tmp_path):
    root = repo(tmp_path, BASE)
    pinned = derived(root)
    repo(root, {'scripts/common.sh': COMMON + '. "$REPO_ROOT/scripts/anchors.sh"\n',
                'scripts/anchors.sh': '. scripts/more.env\n', 'scripts/more.env': 'A=1\n'})
    assert derived(root) - pinned == {'scripts/anchors.sh', 'scripts/more.env'}


def test_a_new_python_import_is_flagged_and_a_stdlib_shadow_is_a_dependency(tmp_path):
    root = repo(tmp_path, BASE)
    pinned = derived(root)
    repo(root, {'scripts/sign.py': SIGN + 'from scripts.lib import verify\n', 'scripts/__init__.py': '',
                'scripts/lib/__init__.py': '', 'scripts/lib/verify.py': '',
                # Python puts the script's directory first: this json.py would run.
                'scripts/json.py': ''})
    assert derived(root) - pinned == {'scripts/__init__.py', 'scripts/lib/__init__.py', 'scripts/lib/verify.py',
                                      'scripts/json.py'}


def test_a_removed_dependency_is_dropped(tmp_path):
    root = repo(tmp_path, BASE)
    repo(root, {'scripts/sign.py': 'import json\n'})
    assert derived(root) == BASE_CLOSURE - {'scripts/helper.py'}


def test_sh_c_positionals_and_python_heredocs_are_followed(tmp_path):
    root = repo(tmp_path, {**BASE, 'scripts/pack.sh': (
        'ROOT=$(CDPATH=\'\' cd -- "$(dirname -- "$0")/.." && pwd)\n'
        'LIB="$ROOT/scripts/common.sh"\n'
        "sh -c '. \"$1\" && run_it' \"$0\" \"$LIB\"\n"
        'python3 -I - "$ROOT" <<\'PYEOF\' || exit 1\n'
        'import helper\n'
        'open("pins/key.txt")\n'
        'PYEOF\n')})
    assert derived(root) == {'scripts/pack.sh', 'scripts/common.sh', 'scripts/helper.py', 'pins/key.txt'}


@pytest.mark.parametrize('line, problem', [
    ('. "$SOMEWHERE/lib.sh"', "cannot resolve sourced file '$SOMEWHERE/lib.sh'"),
    ('. "$REPO_ROOT/scripts/missing.sh"', "cannot resolve sourced file '$REPO_ROOT/scripts/missing.sh'"),
    ('sh "$REPO_ROOT/scripts/missing.sh"', "cannot resolve shell script '$REPO_ROOT/scripts/missing.sh'"),
    ('python3 "$TOOLS/x.py"', "cannot resolve Python script '$TOOLS/x.py'"),
    ('"$TOOL" --sign', "cannot resolve command '$TOOL'"),
    ('eval "$CMD"', "cannot resolve command '$CMD'"),
    ('python3 -c "import yaml_like_thirdparty"', "cannot resolve import 'yaml_like_thirdparty'"),
    ('python3 -c "import importlib; importlib.import_module(name)"', 'dynamic code (import_module)'),
    ('sh -c', 'sh -c without a command string'),
])
def test_an_unresolvable_reference_fails_closed(tmp_path, line, problem):
    root = repo(tmp_path, {**BASE, 'scripts/pack.sh': PACK + line + '\n'})
    with pytest.raises(closure.Unresolved) as raised:
        derived(root)
    assert any(problem in p for p in raised.value.problems), raised.value.problems


def test_a_reviewed_reference_is_accepted_and_an_unused_one_fails(tmp_path):
    root = repo(tmp_path, {**BASE, 'scripts/pack.sh': PACK + '. "$OPERATOR_ENV"\nsh "$KIT/x.sh"\n',
                           'tools/x.sh': 'echo\n'})
    reviewed = {('scripts/pack.sh', '$OPERATOR_ENV'): (None, 'outside the repository'),
                ('scripts/pack.sh', '$KIT/x.sh'): ('tools/x.sh', 'the kit copy of tools/x.sh')}
    assert derived(root, reviewed=reviewed) == BASE_CLOSURE | {'tools/x.sh'}
    with pytest.raises(closure.Unresolved, match='no longer used'):
        derived(root, reviewed={**reviewed, ('scripts/pack.sh', '$GONE'): (None, 'stale')})


def test_an_unparseable_or_untracked_entry_point_fails_closed(tmp_path):
    root = repo(tmp_path, {**BASE, 'scripts/pack.sh': PACK + "echo 'unterminated\n"})
    with pytest.raises(closure.Unresolved, match='cannot parse shell'):
        derived(root)
    with pytest.raises(closure.Unresolved, match='not a tracked file'):
        derived(root, 'scripts/absent.sh')


def test_the_generated_file_names_the_approver_on_every_signing_dependency(tmp_path):
    # The simulated fork commit: the real generator writes the closure into
    # the protected section, and the hygiene step passes on it.
    root = fork(tmp_path)
    protected = entries((root / codeowners.TARGET).read_text(), '[Protected list]')
    for path in closure.derive(ROOT, SIGNING_ENTRY_POINTS):
        assert f'/{path} {APPROVERS[0]}' in protected
    assert hygiene_step(root).returncode == 0
