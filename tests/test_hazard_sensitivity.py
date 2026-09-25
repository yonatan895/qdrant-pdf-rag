"""A red mutation run counts only when its intended existing assertion fails."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from scripts.check_hazard_sensitivity import HazardError, apply_mutation, assess

HAZARD = {'test': 'tests/test_behavior.py::test_original', 'assertion': 'assert received == expected'}


def report(tmp_path, *, name='test_original', classname='tests.test_behavior', result=None, text='', count=1):
    suite = ET.Element('testsuite')
    for _ in range(count):
        case = ET.SubElement(suite, 'testcase', name=name, classname=classname)
        if result:
            child = ET.SubElement(case, result, message='original failure')
            child.text = text
    path = tmp_path / 'result.xml'
    ET.ElementTree(suite).write(path)
    return path


def test_only_intended_behavioral_failure_counts(tmp_path):
    path = report(tmp_path, result='failure', text='tests/test_behavior.py:10: in test_original\n    assert received == expected\nE AssertionError: wrong received value')
    assert assess(path, 1, HAZARD, baseline=False)['status'] == 'killed_by_behavior'
    assert assess(path, 2, HAZARD, baseline=False)['status'] == 'invalid'
    assert assess(path, 1, HAZARD, baseline=True)['status'] == 'baseline_failed'
    path = report(tmp_path)
    assert assess(path, 0, HAZARD, baseline=True)['status'] == 'baseline_pass'
    assert assess(path, 0, HAZARD, baseline=False)['status'] == 'survived'


@pytest.mark.parametrize('kwargs', [
    {'count': 0}, {'count': 2}, {'name': 'test_other'}, {'classname': 'tests.test_other'},
    {'result': 'skipped'}, {'result': 'error', 'text': 'ImportError'},
    {'result': 'failure', 'text': 'assert received == expected\nImportError'},
    {'result': 'failure', 'text': 'assert unrelated_tool_exists\nAssertionError'},
])
def test_missing_skipped_setup_or_wrong_assertion_never_counts(tmp_path, kwargs):
    assert assess(report(tmp_path, **kwargs), 1, HAZARD, baseline=False)['status'] == 'invalid'


def test_corrupt_result_never_counts(tmp_path):
    path = tmp_path / 'bad.xml'
    path.write_text('<truncated')
    assert assess(path, 1, HAZARD, baseline=False)['status'] == 'invalid'


def test_mutation_requires_exact_preimage_and_keeps_other_bytes(tmp_path):
    source = tmp_path / 'src/example.py'
    source.parent.mkdir()
    source.write_text('def result():\n    return 2\n')
    hazard = {'target': 'src/example.py', 'before': 'return 2', 'after': 'return 3', 'occurrences': 1}
    apply_mutation(tmp_path, hazard)
    assert source.read_text() == 'def result():\n    return 3\n'
    with pytest.raises(HazardError, match='preimage'):
        apply_mutation(tmp_path, hazard)
    for target in ('../outside.py', '/tmp/outside.py', 'docs/unrelated.md'):
        with pytest.raises(HazardError, match='boundaries'):
            apply_mutation(tmp_path, {**hazard, 'target': target})


@pytest.mark.parametrize('error', ['E   assert False', 'E   AssertionError: wrong value'])
def test_pytest_rewritten_assertion_format_counts(tmp_path, error):
    text = 'tests/test_behavior.py:10: in test_original\n    assert received == expected\n' + error
    assert assess(report(tmp_path, result='failure', text=text), 1, HAZARD, baseline=False)['status'] == 'killed_by_behavior'


@pytest.mark.parametrize('tail', [
    'helper.py:20: in helper\n    assert unrelated\nE AssertionError',
    'tests/test_behavior.py:20: in test_other\n    assert received == expected\nE AssertionError',
    'tests/test_behavior.py:20: in test_original\n    assert unrelated\nE AssertionError',
    'tests/test_behavior.py:20: in test_original\n    assert received == expected\nE ImportError: AssertionError',
])
def test_expected_assertion_in_earlier_frame_is_not_a_kill(tmp_path, tail):
    text = 'tests/test_behavior.py:10: in test_original\n    assert received == expected\n' + tail
    assert assess(report(tmp_path, result='failure', text=text), 1, HAZARD, baseline=False)['status'] == 'invalid'


def synthetic_tree(tmp_path, body):
    root = tmp_path / 'candidate'
    (root / 'tests').mkdir(parents=True)
    (root / 'src').mkdir()
    (root / 'tests/__init__.py').write_text('')
    (root / 'src/value.py').write_text('VALUE = 1\n')
    (root / 'tests/test_behavior.py').write_text(body)
    return root


def test_pristine_pair_does_not_credit_baseline_marker_for_irrelevant_mutation(tmp_path):
    import sys
    import tarfile
    from pathlib import Path

    from scripts.check_hazard_sensitivity import run_pair

    root = synthetic_tree(tmp_path, '''import os
from pathlib import Path
from value import VALUE

def test_original():
    marker = Path("first-run")
    locations = [marker, Path(os.environ['TMPDIR']) / 'marker',
                 Path(os.environ['XDG_CACHE_HOME']) / 'marker']
    received = all(not location.exists() for location in locations)
    for location in locations:
        location.write_text(str(VALUE))
    expected = True
    assert received == expected
''')
    archive = tmp_path / 'candidate.tar'
    with tarfile.open(archive, 'w') as tar:
        for path in root.iterdir():
            tar.add(path, arcname=path.name)
    output = tmp_path / 'output'
    output.mkdir()
    hazard = {**HAZARD, 'id': 'irrelevant', 'target': 'src/value.py',
              'before': 'VALUE = 1', 'after': 'VALUE = 2', 'occurrences': 1}
    baseline, mutant = run_pair(archive, tmp_path, Path(sys.executable), hazard, output, root)
    assert baseline['status'] == 'baseline_pass'
    assert mutant['status'] == 'survived'
    assert (tmp_path / 'irrelevant-baseline/first-run').read_text() == '1'
    assert (tmp_path / 'irrelevant-mutation/first-run').read_text() == '2'


def test_passing_test_cannot_mutate_snapshot_source(tmp_path):
    import sys
    from pathlib import Path

    from scripts.check_hazard_sensitivity import run_test

    root = synthetic_tree(tmp_path, '''from pathlib import Path

def test_original():
    Path("src/value.py").write_text("VALUE = 3\\n")
''')
    result = run_test(root, Path(sys.executable), HAZARD, tmp_path / 'drift',
                      baseline=True, tools_root=root)
    assert result['status'] == 'invalid'
    assert result['source_drift'] == ['src/value.py']


@pytest.mark.parametrize('parent_wait', [True, False])
def test_timeout_reaps_started_descendant_and_next_test_is_clean(tmp_path, parent_wait):
    import os
    import sys
    import time
    from pathlib import Path

    from scripts.check_hazard_sensitivity import run_test

    root = synthetic_tree(tmp_path, '''import subprocess
import sys
import time
from pathlib import Path

def test_original():
    child = subprocess.Popen([sys.executable, "-c", """
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('child.pid').write_text(str(os.getpid()))
while True:
    with Path('writes').open('a') as out:
        out.write('x')
    time.sleep(0.02)
"""])
    while not Path('child.pid').exists():
        time.sleep(0.01)
    print('owned child started', flush=True)
    time.sleep(30)
'''.replace('    time.sleep(30)', '    time.sleep(30)' if parent_wait else '    pass'))
    result = run_test(root, Path(sys.executable), HAZARD, tmp_path / 'timeout',
                      baseline=True, tools_root=root, timeout=10)
    assert result['status'] == 'invalid'
    assert result['cause'] == ('test deadline exceeded' if parent_wait else 'test left descendant processes')
    assert result['process_group_drained']
    assert 'owned child started' in (tmp_path / 'timeout.log').read_text()
    pid = int((root / 'child.pid').read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    written = (root / 'writes').read_bytes()
    time.sleep(0.1)
    assert (root / 'writes').read_bytes() == written
    (root / 'tests/test_behavior.py').write_text('def test_original():\n    assert True\n')
    result = run_test(root, Path(sys.executable), HAZARD, tmp_path / 'next',
                      baseline=True, tools_root=root)
    assert result['status'] == 'baseline_pass' and result['process_group_drained']
