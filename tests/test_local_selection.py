"""Deterministic test selection for local verification (#796 G4/G5)."""
from __future__ import annotations

from pathlib import Path

import pytest
from scripts import local_selection as ls

ROOT = Path(__file__).resolve().parents[1]

TREE = {
    'src/mainframe_rag/__init__.py': '',
    'src/mainframe_rag/core.py': 'VALUE = 1\n',
    'src/mainframe_rag/api.py': 'from mainframe_rag import core\n',
    'src/mainframe_rag/rel/__init__.py': '',
    'src/mainframe_rag/rel/mod.py': 'from .. import core\n',
    'src/mainframe_rag/contracted.py': 'X = 1\n',
    'src/mainframe_rag/orphan.py': 'Y = 1\n',
    'src/mainframe_rag/webui/static/app.js': '// asset\n',
    'scripts/runner.py': 'import mainframe_rag.api\n',
    'scripts/tool.sh': 'true\n',
    'tests/__init__.py': '',
    'tests/conftest.py': '',
    'tests/helpers_run.py': 'RUNNER = "scripts/runner.py"\n',
    'tests/test_api.py': 'from mainframe_rag.api import core\n',
    'tests/test_rel.py': 'import mainframe_rag.rel.mod\n',
    'tests/test_runner.py': 'from tests import helpers_run\n',
    'tests/test_tool.py': 'SCRIPT = ("scripts", "tool.sh")\n',
    'tests/test_toolbox.py': 'NAME = "my_tool.sh"\n',
    'tests/test_static.py': 'ASSET = "app.js"\n',
    'tests/test_other.py': '',
    'tests/test_colab.py': '',
    'tests/test_local_verify.py': '',
    'docs/agent-workflow.md': (
        '<!-- context-map:start -->\n'
        '| Contract | Canonical owner | Boundaries to inspect | Evidence home |\n'
        '|---|---|---|---|\n'
        '| demo | [Owner](demo.md) | [Contracted](../src/mainframe_rag/contracted.py) '
        '| [Other tests](../tests/test_other.py) |\n'
        '<!-- context-map:end -->\n'),
}


@pytest.fixture
def tree(tmp_path):
    for rel, text in TREE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


@pytest.mark.parametrize('changed, expected', [
    # Import closure through an importing module and a relative import, plus a
    # script that imports it and is run by path from a helper the test imports.
    (['src/mainframe_rag/core.py'], ['tests/test_api.py', 'tests/test_rel.py', 'tests/test_runner.py']),
    (['scripts/tool.sh'], ['tests/test_tool.py']),  # basename; my_tool.sh is a different file
    (['src/mainframe_rag/webui/static/app.js'], ['tests/test_static.py']),
    (['src/mainframe_rag/contracted.py'], ['tests/test_other.py']),  # context-map evidence
    (['tests/test_other.py'], ['tests/test_other.py']),
])
def test_each_rule_selects_exactly_the_exercising_tests(tree, changed, expected):
    selection = ls.select(changed, root=tree)
    assert (selection.tier, list(selection.tests), selection.unmapped) == ('T1', expected, ())
    assert all(selection.reasons[t] == tuple(changed) for t in expected)


def test_unmapped_guarded_change_is_reported_but_prose_and_deletions_are_not(tree):
    selection = ls.select(['src/mainframe_rag/orphan.py', 'src/mainframe_rag/gone.py', 'docs/notes.md'],
                          root=tree)
    assert selection.unmapped == ('src/mainframe_rag/orphan.py',)
    assert selection.tests == ()


@pytest.mark.parametrize('broad', ['tests/conftest.py', 'pyproject.toml', 'requirements.dev.lock.txt',
                                   'taskfiles/quality.yml', 'scripts/tools/run-task.sh'])
def test_broad_change_escalates_to_t2_without_slow_modules(tree, broad):
    selection = ls.select([broad, 'scripts/tool.sh'], root=tree)
    assert selection.tier == 'T2' and selection.broad == (broad,)
    unit = {p for p in ls.all_unit_modules(tree)}
    assert set(selection.tests) == unit - {'tests/test_colab.py', 'tests/test_local_verify.py'}


def test_slow_module_runs_in_t2_when_the_change_selects_it(tree):
    selection = ls.select(['tests/conftest.py', 'tests/test_colab.py'], root=tree)
    assert 'tests/test_colab.py' in selection.tests


def test_selection_is_deterministic_and_sorted(tree):
    changed = ['src/mainframe_rag/core.py', 'scripts/tool.sh', 'src/mainframe_rag/contracted.py']
    first, second = ls.select(changed, root=tree), ls.select(list(reversed(changed)), root=tree)
    assert first == second
    assert list(first.tests) == sorted(first.tests)
    assert first.as_record()['reasons']['tests/test_api.py'] == ['src/mainframe_rag/core.py']


@pytest.fixture(scope='module')
def repo_graph():
    return ls.build_graph(ROOT)


@pytest.mark.parametrize('changed, required', [
    # #793: Colab lock PID plus the Taskfile contract test fix.
    (['scripts/colab.py', 'tests/test_colab.py', 'tests/test_taskfile_contracts.py', 'docs/colab-playground.md'],
     {'tests/test_colab.py', 'tests/test_taskfile_contracts.py'}),
    # #784: structured decline in the answer parser.
    (['src/mainframe_rag/agent/answer.py'], {'tests/test_agent_api.py', 'tests/test_eval_answers.py'}),
])
def test_real_session_diffs_select_their_owning_suites(repo_graph, changed, required):
    selection = ls.select(changed, graph=repo_graph)
    assert selection.tier == 'T1' and not selection.unmapped
    assert required <= set(selection.tests)


@pytest.mark.parametrize('changed', ['scripts/local_verify.py', 'tests/test_local_verify.py', 'docs/testing.md'])
def test_runner_self_test_is_never_auto_selected(repo_graph, changed):
    selection = ls.select([changed], graph=repo_graph)
    assert 'tests/test_local_verify.py' not in selection.tests
    assert 'tests/test_local_verify.py' not in selection.reasons


def test_runner_self_test_is_never_added_by_t2(repo_graph):
    selection = ls.select(['pyproject.toml'], graph=repo_graph)
    assert selection.tier == 'T2'
    assert 'tests/test_local_verify.py' not in selection.tests


def test_harness_only_change_is_recorded_instead_of_selected(tree):
    selection = ls.select(['tests/test_local_verify.py'], root=tree)
    assert (selection.tests, selection.harness) == ((), ('tests/test_local_verify.py',))
    assert selection.as_record()['harness'] == ['tests/test_local_verify.py']
