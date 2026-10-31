"""scripts/gap_codeowners.py: the gap fork's GitLab CODEOWNERS (GH#1118 F4, ADR-0009 section 6).

The protected list must equal the Verifier's list plus the gap gates the ADR
names, written here independently. The group section must own every tracked
path except gap/ using only GitLab 17.0 syntax (sections and plain path
entries; no `!` exclusion, which needs 17.10+). Generated files are checked
line by line, owners round-trip exactly, and every weakened or hand-edited
file, missing file and shadowing CODEOWNERS fails the real hygiene step.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from scripts import gap_codeowners as codeowners
from scripts.acceptance import VERIFICATION_INPUTS, verification_inputs
from scripts.acceptance_evidence import PRODUCERS
from scripts.unit_evidence import INPUTS as UNIT_INPUTS

ROOT = Path(__file__).resolve().parents[1]
GROUP = '@site/gap-maintainers'
APPROVERS = ['@first.approver', '@second_approver-2']
# ADR-0009 section 6, written out: the gap's own gates. The release pipeline
# and signing files are today's bundle and kit signers (F6 extends this), plus
# the generator the hygiene job runs (#1176 review).
GAP_GATES = {'.gitlab-ci.yml', '.gitlab/CODEOWNERS', 'scripts/check_junit_clean.py', 'scripts/gate_l1.py',
             'scripts/tools/devkit-pin.txt', 'scripts/airgap/pack.sh', 'scripts/devkit/pack.sh',
             'scripts/gap_codeowners.py'}
CHECK_STEP = 'python3 scripts/gap_codeowners.py check'


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


def test_protected_set_is_the_verifier_list_plus_the_named_gap_gates():
    workflows = {'.github/workflows/' + p.workflow for p in PRODUCERS}
    assert set(codeowners.protected_paths()) == {*VERIFICATION_INPUTS, 'taskfiles/*.yml', *workflows, *GAP_GATES}
    assert set(UNIT_INPUTS) <= set(codeowners.protected_paths())
    assert not any(p.startswith('gap/') for p in codeowners.protected_paths())


def test_every_path_the_verifier_binds_is_protected_and_every_named_path_exists():
    bound = verification_inputs(ROOT)  # the real function acceptance hashes
    assert bound and all(any(gitlab_glob(p, path) for p in codeowners.protected_paths()) for path in bound)
    named = [p for p in codeowners.protected_paths() if '*' not in p and p != codeowners.TARGET]
    assert [p for p in named if not (ROOT / p).is_file()] == []
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
