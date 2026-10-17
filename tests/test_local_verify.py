"""Hermetic local verification runner (#796): each guarantee refuses, then the next run proceeds."""
from __future__ import annotations

import fcntl
import json
import os
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
    lv.validate_selectors(ROOT, ['tests/test_local_verify.py', 'tests/test_local_verify.py::test_x'])
    with pytest.raises(lv.Refusal, match='no selection'):
        lv.validate_selectors(ROOT, [])


def _receipt(state: dict, code: int, counts: dict | None) -> dict:
    return lv.build_receipt(state=state, selectors=['tests/t.py'], command=['pytest'],
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


def test_run_writes_a_receipt_for_exactly_the_selection_and_drops_ambient_env(monkeypatch):
    out = ROOT / '.local-verify' / f'test-{uuid.uuid4().hex}'
    node = 'tests/test_local_verify.py::test_selection_accepts_files_and_node_ids_and_refuses_empty'
    monkeypatch.setenv('EMBED_MODE', 'dense')
    monkeypatch.setenv('PYTHONPATH', '/other/src')
    try:
        code = lv.main(['--no-memory-cap', '--python', sys.executable, '--out-dir', str(out), node],
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
