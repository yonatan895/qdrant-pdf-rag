"""scripts/gap_codeowners.py: the gap fork's GitLab CODEOWNERS (GH#1118 F4, ADR-0009 section 6).

The protected list must equal the Verifier's list plus the gap gates the ADR
names, written here independently. Generated files are checked line by line,
owners round-trip exactly, and every weakened or hand-edited file, missing
file and shadowing CODEOWNERS fails the real hygiene step from .gitlab-ci.yml.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from scripts import gap_codeowners as gc
from scripts.acceptance import VERIFICATION_INPUTS, verification_inputs
from scripts.acceptance_evidence import PRODUCERS
from scripts.unit_evidence import INPUTS as UNIT_INPUTS

ROOT = Path(__file__).resolve().parents[1]
GROUP = '@site/gap-maintainers'
APPROVERS = ['@first.approver', '@second_approver-2']
# ADR-0009 section 6, written out: the gap's own gates. The release pipeline
# and signing files are today's bundle and kit signers (F6 extends this).
GAP_GATES = {'.gitlab-ci.yml', '.gitlab/CODEOWNERS', 'scripts/check_junit_clean.py', 'scripts/gate_l1.py',
             'scripts/tools/devkit-pin.txt', 'scripts/airgap/pack.sh', 'scripts/devkit/pack.sh'}
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


def written(tmp_path: Path) -> Path:
    assert gc.main(['--root', str(tmp_path), 'write', '--group', GROUP,
                    *(a for name in APPROVERS for a in ('--approver', name))]) == 0
    return tmp_path / '.gitlab/CODEOWNERS'


def test_protected_set_is_the_verifier_list_plus_the_named_gap_gates():
    workflows = {'.github/workflows/' + p.workflow for p in PRODUCERS}
    assert set(gc.protected_paths()) == {*VERIFICATION_INPUTS, 'taskfiles/*.yml', *workflows, *GAP_GATES}
    assert set(UNIT_INPUTS) <= set(gc.protected_paths())
    assert not any(p.startswith('gap/') for p in gc.protected_paths())


def test_every_path_the_verifier_binds_is_protected_and_every_named_path_exists():
    bound = verification_inputs(ROOT)  # the real function acceptance hashes
    assert bound and all(any(gitlab_glob(p, path) for p in gc.protected_paths()) for path in bound)
    named = [p for p in gc.protected_paths() if '*' not in p and p != gc.TARGET]
    assert [p for p in named if not (ROOT / p).is_file()] == []
    # In the fork, which tracks the file, it must already be current.
    if (ROOT / gc.TARGET).exists():
        assert gc.main(['--root', str(ROOT), 'check']) == 0


def test_generated_file_owns_everything_but_gap_and_names_approvers_on_the_protected_list(tmp_path):
    text = written(tmp_path).read_text()
    assert text.endswith('\n') and '\r' not in text and not any(l != l.rstrip() for l in text.split('\n'))
    # GitLab parses owners even inside comments, so no comment names one.
    assert not any('@' in line for line in text.split('\n') if line.startswith('#'))
    assert entries(text, '[Gap maintainers]') == [f'* {GROUP}', '!/gap/']
    protected = entries(text, '[Protected list]')
    assert protected == [f'/{p} {" ".join(APPROVERS)}' for p in sorted(gc.protected_paths())]
    sections = [line for line in text.split('\n') if line.startswith('[')]
    assert sections == ['[Gap maintainers]', '[Protected list]']


def test_owners_round_trip_through_check_and_refresh(tmp_path):
    target = written(tmp_path)
    before = target.read_bytes()
    assert gc.owners_of(before.decode()) == (GROUP, APPROVERS)
    assert run(tmp_path, 'check').returncode == 0
    assert run(tmp_path, 'refresh').returncode == 0
    assert target.read_bytes() == before
    # Owners are read back from the file, so a consistent change of owners is
    # current; that change is guarded because the file is on the protected list.
    target.write_text(before.decode().replace(GROUP, '@other/group'))
    assert run(tmp_path, 'check').returncode == 0
    assert gc.TARGET in gc.protected_paths()


def _drop_protected(text: str) -> str:
    return text.replace('/scripts/gate_l1.py ' + ' '.join(APPROVERS) + '\n', '')


MUTATIONS = {
    'a protected line dropped': _drop_protected,
    'one approver dropped from a line': lambda t: t.replace(
        '/Taskfile.yml ' + ' '.join(APPROVERS), '/Taskfile.yml ' + APPROVERS[0]),
    'an approver added to a line': lambda t: t.replace('/Taskfile.yml ', '/Taskfile.yml @extra '),
    'gap/ exclusion removed': lambda t: t.replace('!/gap/\n', ''),
    'a path excluded from the group': lambda t: t.replace('!/gap/\n', '!/gap/\n!/scripts/\n'),
    'a second owner on the wildcard': lambda t: t.replace(f'* {GROUP}', f'* {GROUP} @other'),
    'hand-added unowned entry': lambda t: t + '/gap/extra.md ' + APPROVERS[0] + '\n',
    'lines reordered': lambda t: t.replace('/Taskfile.yml', '/Zz').replace('/.gitlab-ci.yml', '/Taskfile.yml')
    .replace('/Zz', '/.gitlab-ci.yml'),
    'CRLF line endings': lambda t: t.replace('\n', '\r\n'),
    'trailing whitespace': lambda t: t.replace('!/gap/\n', '!/gap/ \n'),
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
    assert result.stderr == gc.DRIFT


@pytest.mark.parametrize('shadow', gc.SHADOWING)
def test_check_refuses_a_file_gitlab_would_read_first(tmp_path, shadow):
    written(tmp_path)
    (tmp_path / shadow).parent.mkdir(exist_ok=True)
    (tmp_path / shadow).write_text('* @anyone\n')
    result = run(tmp_path, 'check')
    assert (result.returncode, result.stderr) == (1, gc.SHADOWED.format(path=shadow))


def test_check_and_refresh_refuse_a_missing_or_symlinked_file(tmp_path):
    for command in ('check', 'refresh'):
        assert (run(tmp_path, command).returncode, run(tmp_path, command).stderr) == (1, gc.DRIFT)
    (tmp_path / 'real').mkdir()
    real = written(tmp_path / 'real')
    (tmp_path / '.gitlab').mkdir()
    (tmp_path / '.gitlab/CODEOWNERS').symlink_to(real)
    assert run(tmp_path, 'check').stderr == gc.DRIFT
    assert not (tmp_path / 'CODEOWNERS').exists()


@pytest.mark.parametrize('group, approvers', [
    ('@@maintainer', APPROVERS),
    ('gap-maintainers', APPROVERS),
    (GROUP, []),
    (GROUP, ['@a b']),
    (GROUP, ['@user@example.com']),
    (GROUP, [GROUP]),
    (GROUP, ['@same', '@SAME']),
    (GROUP, ['@-dash-first']),
])
def test_write_refuses_unnamed_or_repeated_owners_and_writes_nothing(tmp_path, group, approvers):
    result = run(tmp_path, 'write', '--group', group, *(a for n in approvers for a in ('--approver', n)))
    assert result.returncode in (1, 2) and result.stdout == ''
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

    assert (step().returncode, step().stderr) == (1, gc.DRIFT)
    target = written(tmp_path)
    assert step().returncode == 0
    target.write_text(_drop_protected(target.read_text()))
    assert step().returncode == 1
