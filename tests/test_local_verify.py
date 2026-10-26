"""Hermetic local verification runner (#796): each guarantee refuses, then the next run proceeds."""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from scripts import colab as controller
from scripts import local_verify as lv

ROOT = Path(__file__).resolve().parents[1]


def test_child_env_is_allowlisted_and_records_explicit_passes(tmp_path):
    inherited = {'PATH': '/usr/bin', 'HOME': '/home/u', 'LC_ALL': 'C.UTF-8', 'PYTHONPATH': '/other/src',
                 'EMBED_MODE': 'dense', 'LLM_BASE_URL': 'http://x', 'ALLOW_HASH_MODE': '1',
                 'TASK_EMBED_MODE': 'hash', 'PYTEST_ADDOPTS': '-x', 'CONSOLE_BROWSER_CHROME': '/c'}
    env = lv.child_env(inherited, tmp_path, tmp_path / 'tmp', ['CONSOLE_BROWSER_CHROME'])
    assert set(env) == {'PATH', 'HOME', 'LC_ALL', 'CONSOLE_BROWSER_CHROME', 'TMPDIR',
                        'TASK_CONTRACTS_REQUIRE_RUNNER', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD'}
    assert env['PATH'] == f'{tmp_path / ".tools/bin"}{os.pathsep}/usr/bin'
    assert env['TMPDIR'] == str(tmp_path / 'tmp')
    with pytest.raises(lv.Refusal, match='--pass-env MISSING is not set'):
        lv.child_env(inherited, tmp_path, tmp_path / 'tmp', ['MISSING'])


def nested_python(tmp_path: Path) -> str:
    """The current interpreter prepared for this worktree without weakening G1.

    A shared main venv is an editable install of main's src; the documented
    worktree recipe puts this checkout's src first with PYTHONPATH. Wrap that
    recipe so local_verify's clean-environment probe and child see the same
    interpreter contract they would under a per-worktree venv.
    """
    # Match local_verify's allowlisted probe: ambient PYTHONPATH is not evidence.
    probe = subprocess.run([sys.executable, '-c', 'import mainframe_rag; print(mainframe_rag.__file__)'],
                           env={'PATH': os.defpath}, capture_output=True, text=True, check=False)
    expected = (ROOT / 'src/mainframe_rag/__init__.py').resolve()
    if probe.returncode == 0 and probe.stdout.strip() and Path(probe.stdout.strip()).resolve() == expected:
        return sys.executable
    launcher = tmp_path / 'python-with-worktree-src'
    launcher.write_text(
        f'#!/bin/sh\nPYTHONPATH={shlex.quote(str(ROOT / "src"))}${{PYTHONPATH:+:$PYTHONPATH}} '
        f'exec {shlex.quote(sys.executable)} "$@"\n')
    launcher.chmod(0o755)
    return str(launcher)


def _fake_tree(root: Path, reported: Path) -> Path:
    (root / 'src/mainframe_rag').mkdir(parents=True)
    (root / 'src/mainframe_rag/__init__.py').write_text('')
    python = root / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\necho {reported}\n')
    python.chmod(0o755)
    return python


def test_interpreter_importing_another_tree_refuses_then_own_tree_proceeds(tmp_path):
    root = tmp_path / 'wt'
    python = _fake_tree(root, tmp_path / 'main/src/mainframe_rag/__init__.py')
    env = {'PATH': os.defpath}
    with pytest.raises(lv.Refusal, match="does not import this worktree's src"):
        lv.check_interpreter(root, python, env)
    python.write_text(f'#!/bin/sh\necho {root / "src/mainframe_rag/__init__.py"}\n')
    with pytest.raises(lv.Refusal, match='pinned Task runner missing'):
        lv.check_interpreter(root, python, env)
    (root / '.tools/bin').mkdir(parents=True)
    (root / '.tools/bin/task').write_text('')
    lv.check_interpreter(root, python, env)


def test_ram_backed_out_dir_refuses_by_longest_mount_prefix(tmp_path):
    mounts = ('/dev/sda1 / ext4 rw 0 0\n'
              'tmpfs /tmp tmpfs rw 0 0\n'
              '/dev/sdb1 /tmp/disk ext4 rw 0 0\n')
    assert lv.filesystem_type(Path('/tmp/run'), mounts) == 'tmpfs'
    assert lv.filesystem_type(Path('/tmp/disk/run'), mounts) == 'ext4'
    assert lv.filesystem_type(Path('/tmpx/run'), mounts) == 'ext4'
    with pytest.raises(lv.Refusal, match='on tmpfs'):
        lv.check_disk_backed(Path('/tmp/run'), mounts)
    lv.check_disk_backed(Path('/tmp/disk/run'), mounts)


def test_busy_assignment_lock_names_holder_then_released_lock_proceeds(tmp_path):
    lock = tmp_path / 'assignment.lock'
    assert lv.assignment_lock_holder(lock) is None  # never created: nobody holds it
    with open(lock, 'w+b') as holder:
        holder.write(f'{os.getpid()}\n'.encode())
        holder.flush()
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert lv.assignment_lock_holder(lock) == f'PID {os.getpid()}'
        holder.truncate(0)
        assert lv.assignment_lock_holder(lock) == 'an unrecorded PID'
    assert lv.assignment_lock_holder(lock) is None


def test_probe_sees_the_lock_the_colab_controller_holds():
    with controller.assignment_lock():
        assert lv.assignment_lock_holder(lv.colab_assignment_lock()) == f'PID {os.getpid()}'
    assert lv.assignment_lock_holder(lv.colab_assignment_lock()) is None


def test_pytest_running_from_this_tree_refuses_then_clears(tmp_path):
    inside, outside = tmp_path / 'wt', tmp_path / 'elsewhere'
    (inside / 'tests').mkdir(parents=True)
    outside.mkdir()
    sleeper = [sys.executable, '-c', 'import time; time.sleep(60)', 'pytest']
    stray = subprocess.Popen(sleeper, cwd=inside / 'tests')
    other = subprocess.Popen(sleeper, cwd=outside)
    try:
        assert lv.stray_pytest(inside) == [stray.pid]
    finally:
        for proc in (stray, other):
            proc.kill()
            proc.wait()
    assert lv.stray_pytest(inside) == []


@pytest.mark.parametrize('selector, reason', [
    ('-k', 'is an option'),
    ('src/mainframe_rag/config.py', 'not a test path'),
    ('tests/test_absent_module.py', 'not a test path'),
    ('../outside/tests/test_x.py', 'not a test path'),
])
def test_selection_must_name_test_paths(selector, reason):
    with pytest.raises(lv.Refusal, match=reason):
        lv.validate_selectors(ROOT, [selector])


def test_selection_accepts_files_and_node_ids_and_refuses_empty():
    lv.validate_selectors(ROOT, ['tests/test_local_selection.py', 'tests/test_local_selection.py::test_x'])
    with pytest.raises(lv.Refusal, match='no selection'):
        lv.validate_selectors(ROOT, [])


def test_runner_self_test_is_refused_explicitly_with_a_fixed_message():
    for selector in ('tests/test_local_verify.py', 'tests/test_local_verify.py::test_x'):
        with pytest.raises(lv.Refusal, match='exercises this runner; run it with pytest or CI'):
            lv.validate_selectors(ROOT, [selector])


def _receipt(state: dict, code: int, counts: dict | None) -> dict:
    return lv.build_receipt(state=state, tier='explicit', selection={'mode': 'explicit', 'selectors': ['tests/t.py']},
                            command=['pytest'],
                            env={'TMPDIR': '/t', 'TASK_CONTRACTS_REQUIRE_RUNNER': '1',
                                 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
                            dropped=['PYTHONPATH'], passed=[], memory_max=3072, code=code,
                            counts=counts, started=0.0, finished=1.0)


CLEAN = {'head': 'a' * 40, 'base': {'ref': 'origin/main', 'sha': 'b' * 40}, 'dirty': []}
PASS = {'executed': 3, 'failed': 0, 'errors': 0, 'skipped': 0}


@pytest.mark.parametrize('state, code, counts, result, evidence, label', [
    (CLEAN, 0, PASS, 'passed', True, 'local verification of the selection only'),
    ({**CLEAN, 'dirty': ['src/x.py']}, 0, PASS, 'passed', False, 'uncommitted, not evidence'),
    (CLEAN, 0, {**PASS, 'skipped': 1}, 'incomplete: 1 skipped (a skip is not a pass)', False, 'not evidence'),
    (CLEAN, 1, {**PASS, 'failed': 1}, 'failed', False, 'not evidence'),
    (CLEAN, 2, None, 'failed: no readable test report', False, 'not evidence'),
])
def test_receipt_is_evidence_only_for_a_clean_complete_pass(state, code, counts, result, evidence, label):
    receipt = _receipt(state, code, counts)
    assert (receipt['result'], receipt['evidence'], receipt['label']) == (result, evidence, label)
    assert receipt['tier'] == 'explicit' and receipt['not_verified'] == list(lv.NOT_VERIFIED)
    assert receipt['not_verified'][0].startswith('full unit suite')


def test_out_dir_inside_the_repository_refuses_and_default_is_outside(tmp_path):
    with pytest.raises(lv.Refusal, match='inside the repository'):
        lv.check_outside_tree(ROOT / '.local-verify' / 'run', ROOT)
    with pytest.raises(lv.Refusal, match='inside the repository'):
        lv.check_outside_tree(ROOT, ROOT)
    lv.check_outside_tree(tmp_path / 'run', ROOT)
    assert lv.default_out_dir({'XDG_STATE_HOME': '/s', 'HOME': '/h'}) == Path('/s/mainframe-rag/local-verify')
    assert lv.default_out_dir({'HOME': '/h'}) == Path('/h/.local/state/mainframe-rag/local-verify')


def test_run_writes_a_receipt_for_exactly_the_selection_and_drops_ambient_env(monkeypatch, tmp_path):
    out = tmp_path / f'out-{uuid.uuid4().hex}'
    node = 'tests/test_local_selection.py::test_selection_is_deterministic_and_sorted'
    monkeypatch.setenv('EMBED_MODE', 'dense')
    monkeypatch.setenv('PYTHONPATH', '/other/src')
    try:
        code = lv.main(['--no-memory-cap', '--python', nested_python(tmp_path), '--out-dir', str(out), node],
                       lock=out / 'no-lock')
        (run,) = [p for p in out.iterdir() if p.is_dir()]
        receipt = json.loads((run / 'receipt.json').read_text())
    finally:
        shutil.rmtree(out, ignore_errors=True)
    assert code == (0 if receipt['result'] == 'passed' else 1)
    assert receipt['result'] == 'passed', receipt
    assert receipt['selection'] == {'mode': 'explicit', 'selectors': [node]}
    assert receipt['counts']['executed'] == 1
    assert {'EMBED_MODE', 'PYTHONPATH'} <= set(receipt['environment']['dropped'])
    assert receipt['memory_max_mib'] is None
    assert receipt['evidence'] == (not receipt['tree']['dirty'])


def test_t0_targets_follow_the_ci_lint_scope():
    changed = ['scripts/colab.py', 'scripts/review_tooling.py', 'src/mainframe_rag/config.py',
               'tests/test_local_verify.py', 'docs/testing.md', 'src/mainframe_rag/deleted.py']
    assert lv.t0_targets(ROOT, changed) == {
        'ruff': ['scripts/review_tooling.py', 'src/mainframe_rag/config.py', 'tests/test_local_verify.py'],
        'mypy_src': ['src'], 'mypy_scripts': ['scripts/review_tooling.py']}
    assert lv.t0_targets(ROOT, ['scripts/colab.py']) == {'ruff': [], 'mypy_src': [], 'mypy_scripts': []}


def test_t0_script_scope_matches_the_ci_lint_step():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    step = next(s['run'] for s in workflow['jobs']['lint']['steps'] if 'ruff check src tests' in s.get('run', ''))
    ruff_line = next(line for line in step.splitlines() if 'ruff check src tests' in line)
    assert tuple(word for word in ruff_line.split() if word.startswith('scripts/')) == lv.CI_LINT_SCRIPTS


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_changed_paths_are_committed_plus_dirty_including_both_sides_of_a_rename(tmp_path):
    _git(tmp_path, 'init', '-q', '-b', 'main')
    for name in ('kept.txt', 'old.txt'):
        (tmp_path / name).write_text('x\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base')
    base = _git(tmp_path, 'rev-parse', 'HEAD')
    (tmp_path / 'committed.txt').write_text('y\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'change')
    _git(tmp_path, 'mv', 'old.txt', 'new.txt')
    (tmp_path / 'untracked.txt').write_text('z\n')
    state = lv.tree_state(tmp_path)
    assert state['dirty'] == ['new.txt', 'old.txt', 'untracked.txt']
    assert lv.changed_paths(tmp_path, {**state, 'base': {'ref': 'origin/main', 'sha': base}}) == [
        'committed.txt', 'new.txt', 'old.txt', 'untracked.txt']
    with pytest.raises(lv.Refusal, match='no merge-base'):
        lv.changed_paths(tmp_path, {**state, 'base': {'ref': 'origin/main', 'sha': None}})


def test_tree_state_keeps_unstaged_status_paths_and_dirty_rename_sides(tmp_path):
    _git(tmp_path, 'init', '-q', '-b', 'main')
    for name in ('unstaged.txt', 'unstaged space.txt', 'deleted.txt', 'rename-old.txt'):
        (tmp_path / name).write_text('x\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base')
    base = _git(tmp_path, 'rev-parse', 'HEAD')
    (tmp_path / 'unstaged.txt').write_text('changed\n')
    (tmp_path / 'unstaged space.txt').write_text('changed\n')
    (tmp_path / 'deleted.txt').unlink()
    _git(tmp_path, 'mv', 'rename-old.txt', 'rename-new.txt')
    (tmp_path / 'untracked.txt').write_text('new\n')
    expected = ['deleted.txt', 'rename-new.txt', 'rename-old.txt', 'unstaged space.txt',
                'unstaged.txt', 'untracked.txt']
    state = lv.tree_state(tmp_path)
    assert state['dirty'] == expected
    assert lv.changed_paths(tmp_path, {**state, 'base': {'ref': 'origin/main', 'sha': base}}) == expected


def test_unmapped_change_refuses_then_a_select_override_runs_and_is_recorded(monkeypatch, tmp_path, capsys):
    from scripts import local_selection
    orphan = 'src/mainframe_rag/not_a_real_module.py'
    monkeypatch.setattr(lv, 'changed_paths', lambda root, state: [orphan])
    monkeypatch.setattr(local_selection, 'select', lambda changed, force_t2=False: local_selection.Selection(
        'T1', (), {}, (), (orphan,)))
    common = ['--no-memory-cap', '--python', nested_python(tmp_path), '--out-dir', str(tmp_path / 'out')]
    assert lv.main(common, lock=tmp_path / 'no-lock') == 2
    assert f'unmapped change(s) with no test: {orphan}' in capsys.readouterr().err
    node = 'tests/test_local_selection.py::test_selection_is_deterministic_and_sorted'
    assert lv.main([*common, '--select', node], lock=tmp_path / 'no-lock') == 0
    (run,) = [p for p in (tmp_path / 'out').iterdir() if p.is_dir() and (p / 'receipt.json').exists()]
    receipt = json.loads((run / 'receipt.json').read_text())
    assert (receipt['tier'], receipt['selection']['mode'], receipt['selection']['tests']) == ('T1', 'auto', [node])
    assert receipt['selection']['override'] == {'select': [node], 'unmapped': [orphan]}
    assert receipt['t0'] == {'files': [], 'ruff': None, 'mypy_src': None, 'mypy_scripts': None}
    assert receipt['counts']['executed'] == 1 and receipt['result'] == 'passed'


def test_auto_selection_of_only_the_runner_self_test_refuses_with_a_fixed_message(monkeypatch, tmp_path, capsys):
    from scripts import local_selection
    changed = 'scripts/local_verify.py'
    monkeypatch.setattr(lv, 'changed_paths', lambda root, state: [changed])
    monkeypatch.setattr(local_selection, 'select', lambda changed, force_t2=False: local_selection.Selection(
        'T1', (), {}, (), (), tuple(changed)))
    assert lv.main(['--no-memory-cap', '--out-dir', str(tmp_path / 'out')], lock=tmp_path / 'no-lock') == 2
    err = capsys.readouterr().err
    assert f'harness self-test change(s) are owned by CI or isolated pytest: {changed}' in err
    assert 'exercises this runner; run it with pytest or CI' in err


def test_explicit_mode_rejects_auto_mode_options(tmp_path, capsys):
    args = ['--no-memory-cap', '--out-dir', str(tmp_path), '--t2', 'tests/test_local_verify.py']
    assert lv.main(args, lock=tmp_path / 'no-lock') == 2
    assert 'apply to auto mode' in capsys.readouterr().err


def test_colab_lock_matters_only_for_selections_that_exercise_the_controller():
    assert lv.colab_lock_sensitive(['tests/test_config.py', 'tests/test_local_selection.py']) == []
    assert lv.colab_lock_sensitive(['tests/test_colab.py::test_x', 'tests/test_config.py']) == ['tests/test_colab.py']
    assert 'tests/test_local_verify.py' in lv.colab_lock_sensitive(['tests/test_local_verify.py'])


def test_held_lock_refuses_a_controller_selection_but_not_an_unrelated_one(tmp_path, capsys):
    lock = tmp_path / 'assignment.lock'
    common = ['--no-memory-cap', '--python', nested_python(tmp_path), '--out-dir', str(tmp_path / 'out')]
    unrelated = 'tests/test_local_selection.py::test_selection_is_deterministic_and_sorted'
    with open(lock, 'w+b') as holder:
        holder.write(b'4242\n')
        holder.flush()
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert lv.main([*common, 'tests/test_colab.py::test_x'], lock=lock) == 2
        assert 'held by PID 4242 and the selection runs tests/test_colab.py' in capsys.readouterr().err
        assert lv.main([*common, unrelated], lock=lock) == 0
    (run,) = [p for p in (tmp_path / 'out').iterdir() if (p / 'receipt.json').exists()]
    assert json.loads((run / 'receipt.json').read_text())['colab_lock_checked'] is False
