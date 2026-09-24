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
