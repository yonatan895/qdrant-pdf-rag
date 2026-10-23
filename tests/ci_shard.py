"""Opt-in, dependency-free partition of pytest's selected items."""

import base64
import importlib.metadata
import inspect
import json
import os
import re
import shlex
import tomllib
from pathlib import Path

import pytest
from scripts.unit_evidence import POLICY, input_hashes, rule_record, select_nodes


def pytest_addoption(parser):
    parser.addoption('--unit-evidence')
    parser.addoption('--unit-phase', choices=('collect', 'execute'))
    parser.addoption('--unit-shards', type=int, choices=(2, 4, 8), default=2)
    parser.addoption('--unit-durations', default=None,
                     help='Repo-relative durations file: balance shards by recorded test time.')
    parser.addoption(
        "--unit-shard", type=int, choices=tuple(range(1, 9)), default=None,
        help="Run one shard of the selected tests (omit to run all tests).",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    # Run after pytest's -m/-k filtering, so both runners partition the same
    # eligible collection. Parametrized cases are indivisible node IDs.
    shard = config.getoption("--unit-shard")
    if shard is None or config.getoption("--unit-evidence"):
        return
    ordered = sorted(items, key=lambda item: item.nodeid)
    shards = config.getoption('--unit-shards')
    if shard > shards:
        raise pytest.UsageError('unit shard exceeds the configured shard count')
    selected_ids = set(select_nodes([item.nodeid for item in ordered], shard, shards,
                                    durations_text(config)))
    selected = [item for item in ordered if item.nodeid in selected_ids]
    assert len(selected) == len(selected_ids)
    deselected = [item for item in ordered if item.nodeid not in selected_ids]
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected


def durations_text(config):
    """The durations file's text, or None for the round-robin rule. Must stay inside the rootdir."""
    option = config.getoption('--unit-durations')
    if option is None:
        return None
    root = Path(config.rootpath).resolve()
    path = (root / option).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise pytest.UsageError('unit durations file must be a file inside the repository')
    return path.read_text(encoding='utf-8')


def pytest_configure(config):
    if config.getoption('--unit-evidence'):
        config.pluginmanager.register(UnitCoverage(config), 'native-unit-coverage')


class UnitCoverage:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.rootpath)
        self.output = Path(config.getoption('--unit-evidence'))
        self.phase = config.getoption('--unit-phase')
        self.shard = config.getoption('--unit-shard')
        self.shards = config.getoption('--unit-shards')
        self.executed = []
        self.record = None
        self.durations = durations_text(config)
        if (self.output.exists() or self.phase is None or self.shard is None
                or not 1 <= self.shard <= self.shards):
            raise pytest.UsageError('native unit coverage needs fresh output and explicit phase/shard')
        if os.environ.get('PYTEST_ADDOPTS') or os.environ.get('PYTEST_PLUGINS'):
            raise pytest.UsageError('ambient pytest selection/plugins are not native evidence')
        if (config.inipath != self.root / 'pyproject.toml' or config.args != ['tests']
                or config.getoption('keyword') or config.getoption('markexpr')
                or bool(config.getoption('collectonly')) != (self.phase == 'collect')):
            raise pytest.UsageError('native unit invocation differs from approved full collection')
        self.policy = {key: config.getini(key) for key in POLICY if key != 'addopts'}
        self.policy['addopts'] = shlex.split(tomllib.loads(
            (self.root / 'pyproject.toml').read_text())['tool']['pytest']['ini_options']['addopts'])
        if self.policy != POLICY:
            raise pytest.UsageError('native pytest selection policy changed')
        self.inputs = input_hashes(self.root)
        self.versions = {name: importlib.metadata.version(name) for name in ('pytest', 'pluggy', 'anyio')}
        lock = (self.root / 'requirements.dev.lock.txt').read_text()
        for name, version in self.versions.items():
            matches = re.findall(r'^' + name + r'==([^ \n]+) ', lock, re.MULTILINE)
            if matches != [version]:
                raise pytest.UsageError('pytest collection package differs from prepared lock')

    def plugins(self):
        seen = set()
        for plugin_name, plugin in self.config.pluginmanager.list_name_plugin():
            if plugin is None:
                continue
            module = plugin if inspect.ismodule(plugin) else inspect.getmodule(
                plugin if inspect.isclass(plugin) else type(plugin))
            name = getattr(module, '__name__', '')
            path = getattr(module, '__file__', None)
            if name == '_pytest' or name.startswith('_pytest.'):
                seen.add('_pytest')
            elif name == 'anyio.pytest_plugin':
                seen.add(name)
            elif path and Path(path).resolve() in {
                self.root / 'tests/ci_shard.py', self.root / 'tests/conftest.py'
            }:
                seen.add(str(Path(path).resolve().relative_to(self.root)))
            else:
                raise pytest.UsageError('unapproved plugin in native unit collection: ' + plugin_name + ':' + name + ':' + type(plugin).__name__)
        return sorted(seen)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, items):
        plugins = self.plugins()
        before = sorted((item.nodeid, item.get_closest_marker('integration') is not None) for item in items)
        yield
        after = sorted((item.nodeid, item.get_closest_marker('integration') is not None) for item in items)
        if before != after or len({node for node, _ in before}) != len(before) or plugins != self.plugins():
            raise pytest.UsageError('collection hook removed, duplicated or changed required items')
        eligible = [node for node, integration in before if not integration]
        selected = eligible if self.phase == 'collect' else select_nodes(
            eligible, self.shard, self.shards, self.durations)
        self.record = {'phase': self.phase, 'inputs': self.inputs, 'policy': self.policy, 'versions': self.versions,
                       'plugins': plugins, 'all': [node for node, _ in before], 'eligible': eligible,
                       'integration': [node for node, integration in before if integration], 'selected': selected}
        if self.durations is not None:
            self.record['rule'] = rule_record(self.durations)
        by_id = {item.nodeid: item for item in items}
        excluded = [item for item in items if item.nodeid not in set(selected)]
        self.config.hook.pytest_deselected(items=excluded)
        items[:] = [by_id[node] for node in selected]
        for item in items:
            item.user_properties.append(('native_nodeid', base64.b64encode(item.nodeid.encode()).decode()))

    def pytest_collection_finish(self, session):
        if self.record is None:
            return  # Preserve the original collection error.
        if [item.nodeid for item in session.items] != self.record['selected']:
            raise pytest.UsageError('final unit collection differs from required selection')

    def pytest_runtest_logreport(self, report):
        # Passing subtests contribute to their parent's one JUnit testcase.
        if report.when == 'call' and type(report) is pytest.TestReport:
            self.executed.append(report.nodeid)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        record = self.record or {}
        record.update(executed=self.executed, passed=exitstatus == 0)
        if self.inputs != input_hashes(self.root):
            record['passed'] = False
            session.exitstatus = 1
        self.output.write_text(json.dumps(record, sort_keys=True) + '\n')
