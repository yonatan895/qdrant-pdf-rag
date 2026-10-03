"""GitLab quality-gate parity with GitHub CI, and proof that failures propagate (#370).

Hermetic: both CI definitions are parsed, the GitLab job scripts are executed
under `sh -e` with stub tools, and every static rule has negative controls
(mutated copies of the real definition must be rejected). A real GitLab
pipeline run is still required to prove runner, image and service behavior.
"""
import copy
import os
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GITLAB = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
GITHUB = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
REQUIRED_JOBS = {'hygiene', 'lint', 'test', 'hazards', 'sim', 'gate-l1'}
# Integration-marked files that drive a local Docker daemon; the offline GitLab
# runner only has the mirrored Qdrant service, so the sim lane omits them.
DOCKER_ONLY_SIM_FILES = {'tests/test_qdrant_auth.py'}
# Selection-narrowing or evidence-changing pytest options: the unit lane must
# run the complete eligible suite exactly as pyproject defines it.
NARROWING_FLAGS = ('-k', '-m', '-o', '-p', '--ignore', '--ignore-glob', '--deselect', '--lf',
                   '--ff', '--co', '--collect-only', '--unit-shard', '--unit-shards',
                   '--pyargs', '--continue-on-collection-errors')
MASKS = (r'\|\|\s*(true|:|exit\s+0)\b', r';\s*(true|:)\s*$', r'\bset\s+\+e\b', r'\bexit\s+0\b')


def jobs(doc):
    return {k: v for k, v in doc.items()
            if isinstance(v, dict) and k not in ('workflow', 'default', 'variables')}


def lines(job, key='script'):
    return [step for step in job.get(key, []) if isinstance(step, str)]


def pytest_line(job):
    found = [s for s in lines(job) if s.startswith('pytest ')]
    return found[0] if len(found) == 1 else None


def tool_lines(text, tools=('ruff', 'mypy')):
    return [l.strip() for l in text.splitlines() if l.strip().split(' ')[0] in tools]


def github_step(job, needle):
    return next(s for s in GITHUB['jobs'][job]['steps'] if needle in s.get('run', ''))


def ignores(command):
    return {t.split('=', 1)[1] for t in shlex.split(command) if t.startswith('--ignore=')}


def violations(doc):
    """Every rule the GitLab definition must satisfy; empty means conforming."""
    found = []
    all_jobs = jobs(doc)
    for missing in sorted(REQUIRED_JOBS - set(all_jobs)):
        found.append(f'missing job {missing}')
    for name, job in all_jobs.items():
        if 'allow_failure' in job:
            found.append(f'{name}: allow_failure can turn a failure green')
        if job.get('when', 'on_success') != 'on_success' or 'rules' in job or 'only' in job \
                or 'except' in job:
            found.append(f'{name}: conditional job can be skipped silently')
        for key in ('script', 'before_script', 'after_script'):
            for step in lines(job, key):
                if 'NOTE_BODY=' in step:
                    continue  # advisory MR note; runs after the gate command
                if any(re.search(p, step, re.MULTILINE) for p in MASKS):
                    found.append(f'{name}: masked exit status in {key}')
    if found:
        return found
    if not doc['stages'].index('hygiene') < doc['stages'].index('test'):
        found.append('stage order')
    if all_jobs['lint'].get('stage') != 'hygiene' or 'allow_failure' in all_jobs['lint']:
        found.append('lint must be a hygiene-stage gate')
    expected = tool_lines(github_step('lint', 'ruff check')['run'])
    if lines(all_jobs['lint']) != expected:
        found.append('lint commands differ from the GitHub lint job')
    hygiene = '\n'.join(lines(all_jobs['hygiene']))
    pdf = re.search(r"grep -Ei '([^']+)'", github_step('unit', 'git ls-files')['run'])
    if 'scripts/check_agent_context.py' not in hygiene or not pdf or pdf.group(1) not in hygiene:
        found.append('hygiene lost the context check or the binary refusal')
    for name in ('test', 'sim'):
        job = all_jobs[name]
        body = lines(job)
        command = pytest_line(job)
        if command is None:
            found.append(f'{name}: exactly one pytest command required')
            continue
        report = re.search(r'--junitxml=(\S+)', command)
        gate = [i for i, s in enumerate(body) if s.startswith('python scripts/check_junit_clean.py ')]
        if not report or len(gate) != 1 or body[gate[0]].split()[-1] != report.group(1) \
                or gate[0] <= body.index(command):
            found.append(f'{name}: skip/xfail gate must follow pytest on the same report')
        if report and report.group(1) not in job.get('artifacts', {}).get('paths', []):
            found.append(f'{name}: report not retained')
        if job.get('artifacts', {}).get('when') != 'always':
            found.append(f'{name}: report must be retained on failure')
        if '-rs' not in command.split() and name == 'sim':
            found.append('sim: skip reasons must be reported')
    test_flags = [t for t in shlex.split(pytest_line(all_jobs['test']) or '')
                  if any(t == f or t.startswith(f + '=') for f in NARROWING_FLAGS)]
    if test_flags:
        found.append(f'test: selection narrowed by {test_flags}')
    if not any(s.startswith('python scripts/agent_doctor.py --python') for s in lines(all_jobs['test'])):
        found.append('test: prerequisite doctor missing')
    if 'TASK_CONTRACTS_REQUIRE_RUNNER' not in all_jobs['test'].get('variables', {}):
        found.append('test: runner contracts may skip')
    sim = pytest_line(all_jobs['sim']) or ''
    github_sim = github_step('sim', 'pytest -m integration')['run']
    github_sim = re.search(r'python -m pytest (.*?) -v', github_sim).group(1)
    if '-m integration' not in sim or not ignores('x ' + github_sim) <= ignores(sim):
        found.append('sim: selection must cover the GitHub sim selection')
    if not ignores(sim) - ignores('x ' + github_sim) <= DOCKER_ONLY_SIM_FILES:
        found.append('sim: unapproved extra exclusions')
    if all_jobs['sim'].get('variables', {}).get('QDRANT_SIM_URL') is None:
        found.append('sim: no Qdrant service URL')
    if not any(s.startswith('python scripts/fetch_bm25_weights.py') and '--verify-only' in s
               for s in lines(all_jobs['sim'])):
        found.append('sim: BM25 cache not verified')
    first = lines(all_jobs['gate-l1'])[:1]
    github_gate = re.sub(r'\s*\\\n\s*', ' ', github_step('gate-l1', 'gate_l1.py')['run'])
    github_gate = re.search(r'-- (python scripts/gate_l1.py[^\n]*)', github_gate).group(1)
    if first != [github_gate.strip()]:
        found.append('gate-l1: first command must be the GitHub gate command')
    haz = '\n'.join(lines(all_jobs['hazards']))
    if 'qa:hazards' not in haz or 'qa:hazards' not in github_step('hazards', 'qa:hazards')['run']:
        found.append('hazards: catalogue not run')
    digest = re.search(r'sha256:[0-9a-f]{64}', (ROOT / 'images.txt').read_text()).group(0)
    for name in ('sim', 'gate-l1'):
        if not any(digest in s.get('name', '') for s in all_jobs[name].get('services', [])):
            found.append(f'{name}: Qdrant service is not the approved digest')
    return found


def mutated(mutator):
    doc = copy.deepcopy(GITLAB)
    mutator(doc)
    return doc


def test_current_gitlab_definition_conforms():
    assert violations(GITLAB) == []


def test_github_counterparts_exist_with_expected_shape():
    # A parity rule is meaningless if its GitHub reference silently changes.
    assert GITHUB['jobs']['unit']['strategy']['matrix']['shard'] == [1, 2, 3, 4]
    assert '--unit-shards=4' in github_step('unit', '--unit-shards')['run']
    aggregate = GITHUB['jobs']['test']
    assert aggregate['if'] == 'always()' and set(aggregate['needs']) == {'select', 'unit'}
    assert 'sim' in GITHUB['jobs'] and 'hazards' in GITHUB['jobs'] and 'gate-l1' in GITHUB['jobs']
    assert not any('continue-on-error' in j for j in GITHUB['jobs'].values())


def test_gitlab_runs_the_complete_unit_suite_that_the_github_shards_partition():
    command = shlex.split(pytest_line(GITLAB['test']))
    assert command == ['pytest', '-q', '--junitxml=unit-junit.xml']
    # pyproject selection is the single source for both CIs.
    from scripts.unit_evidence import POLICY
    assert POLICY['addopts'] == ['-q', '-m', 'not integration']


def test_sim_exclusions_are_exactly_the_docker_driving_integration_files():
    for name in DOCKER_ONLY_SIM_FILES:
        assert '"docker"' in (ROOT / name).read_text() or "'docker'" in (ROOT / name).read_text()
    # Every other integration-marked file must reach Qdrant through QDRANT_SIM_URL.
    marked = re.compile(r'^(@pytest\.mark\.integration|pytestmark\s*=.*integration)', re.MULTILINE)
    skipped = DOCKER_ONLY_SIM_FILES | {'tests/test_load_tier.py', 'tests/test_ha_cluster.py',
                                       'tests/test_review_tooling.py'}
    checked = []
    for path in sorted((ROOT / 'tests').glob('test_*.py')):
        rel = f'tests/{path.name}'
        if rel not in skipped and marked.search(path.read_text()):
            checked.append(rel)
            assert '["docker"' not in path.read_text(), rel
    assert 'tests/test_integration_sim.py' in checked and 'tests/test_mcp_sim.py' in checked


def replace(job, prefix, new):
    index = next(i for i, s in enumerate(job['script']) if s.startswith(prefix))
    job['script'][index] = new


def extend(job, prefix, suffix):
    index = next(i for i, s in enumerate(job['script']) if s.startswith(prefix))
    job['script'][index] += suffix


NEGATIVE_CONTROLS = {
    'allow_failure on unit lane': lambda d: d['test'].update(allow_failure=True),
    'allow_failure exit codes': lambda d: d['gate-l1'].update(allow_failure={'exit_codes': 1}),
    'manual unit lane': lambda d: d['test'].update(when='manual'),
    'rules-gated lint': lambda d: d['lint'].update(rules=[{'if': '$NEVER'}]),
    'pytest status masked': lambda d: d['test']['script'].__setitem__(
        d['test']['script'].index('pytest -q --junitxml=unit-junit.xml'),
        'pytest -q --junitxml=unit-junit.xml || true'),
    'ruff status masked': lambda d: d['lint']['script'].__setitem__(
        0, d['lint']['script'][0] + ' || true'),
    'skip gate removed from unit': lambda d: d['test']['script'].remove(
        'python scripts/check_junit_clean.py unit-junit.xml'),
    'skip gate removed from sim': lambda d: d['sim']['script'].remove(
        'python scripts/check_junit_clean.py sim-junit.xml'),
    'skip gate reads another report': lambda d: d['test']['script'].__setitem__(
        d['test']['script'].index('python scripts/check_junit_clean.py unit-junit.xml'),
        'python scripts/check_junit_clean.py other.xml'),
    'skip gate before pytest': lambda d: d['test']['script'].insert(
        0, d['test']['script'].pop()),
    'report not retained': lambda d: d['test'].pop('artifacts'),
    'unit selection narrowed by -k': lambda d: d['test']['script'].__setitem__(
        d['test']['script'].index('pytest -q --junitxml=unit-junit.xml'),
        'pytest -q --junitxml=unit-junit.xml -k "not slow"'),
    'unit selection narrowed by ignore': lambda d: d['test']['script'].__setitem__(
        d['test']['script'].index('pytest -q --junitxml=unit-junit.xml'),
        'pytest -q --junitxml=unit-junit.xml --ignore=tests/test_agent_api.py'),
    'unit shard flag': lambda d: d['test']['script'].__setitem__(
        d['test']['script'].index('pytest -q --junitxml=unit-junit.xml'),
        'pytest -q --junitxml=unit-junit.xml -p tests.ci_shard --unit-shard=1'),
    'doctor removed': lambda d: d['test']['script'].remove(
        'python scripts/agent_doctor.py --python "$(command -v python)"'),
    'runner contracts allowed to skip': lambda d: d['test']['variables'].clear(),
    'lint job removed': lambda d: d.pop('lint'),
    'mypy src dropped': lambda d: d['lint']['script'].remove('mypy src'),
    'script lint scope dropped': lambda d: d['lint']['script'].pop(),
    'ruff scope shrunk': lambda d: d['lint']['script'].__setitem__(0, 'ruff check src tests'),
    'sim job removed': lambda d: d.pop('sim'),
    'sim narrowed beyond docker-only': lambda d: extend(d['sim'], 'pytest ',
                                                         ' --ignore=tests/test_mcp_sim.py'),
    'sim BM25 verification removed': lambda d: replace(d['sim'], 'python scripts/fetch_bm25', 'true'),
    'sim service not approved digest': lambda d: d['sim']['services'][0].update(
        name='${CI_REGISTRY}/qdrant/qdrant:latest'),
    'gate-l1 removed': lambda d: d.pop('gate-l1'),
    'gate-l1 status masked': lambda d: d['gate-l1']['script'].__setitem__(
        0, d['gate-l1']['script'][0] + ' || true'),
    'gate-l1 args changed': lambda d: d['gate-l1']['script'].__setitem__(
        0, d['gate-l1']['script'][0].replace('--delta eval-delta.md', '')),
    'hazards removed': lambda d: d.pop('hazards'),
    'hygiene binary refusal removed': lambda d: d['hygiene'].update(
        script=['python3 scripts/check_agent_context.py']),
    'stage order reversed': lambda d: d.update(stages=['test', 'hygiene']),
}


@pytest.mark.parametrize('name', sorted(NEGATIVE_CONTROLS))
def test_static_rules_reject_each_weakened_definition(name):
    doc = mutated(lambda d: NEGATIVE_CONTROLS[name](d))
    assert violations(doc), f'{name} was not detected'


# ---- behavior: execute the real job scripts with stub tools -----------------

STUB_PYTHON = '''#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$*" in
  *check_junit_clean*) exec "$REAL_PYTHON" "$@" ;;
  *fetch_bm25*) exit "${BM25_STATUS:-0}" ;;
  *agent_doctor*) exit "${DOCTOR_STATUS:-0}" ;;
  *gate_l1*) exit "${GATE_STATUS:-0}" ;;
esac
exit 0
'''

JUNIT = {
    'clean': '<testsuite><testcase name="a"/><testcase name="b"/></testsuite>',
    'skipped': '<testsuite><testcase name="a"/><testcase name="b"><skipped type="pytest.skip"/>'
               '</testcase></testsuite>',
    'xfail': '<testsuite><testcase name="a"/><testcase name="b"><skipped type="pytest.xfail"/>'
             '</testcase></testsuite>',
    'failure': '<testsuite><testcase name="a"><failure message="x"/></testcase></testsuite>',
    'error': '<testsuite><testcase name="a"><error message="x"/></testcase></testsuite>',
    'empty': '<testsuite/>',
    'garbage': 'not xml',
    'doctype': '<!DOCTYPE x><testsuite><testcase name="a"/></testsuite>',
}


def stage(tmp_path):
    for name in ('scripts/__init__.py', 'scripts/check_junit_clean.py', 'scripts/ci_evidence.py'):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    tools = tmp_path / 'scripts/tools'
    tools.mkdir(exist_ok=True)
    for name in ('install-task.sh', 'install-helm.sh'):
        (tools / name).write_text('exit 0\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / 'python').write_text(STUB_PYTHON)
    (bin_dir / 'curl').write_text('#!/bin/sh\necho curl >> "$STUB_LOG"\n')
    for name in ('ruff', 'mypy'):
        (bin_dir / name).write_text(
            '#!/bin/sh\nprintf \'%s %s\\n\' "$(basename "$0")" "$*" >> "$STUB_LOG"\n'
            'if [ -n "$FAIL_ON" ]; then case "$(basename "$0") $*" in "$FAIL_ON"*) exit 1;; esac; fi\n')
    (bin_dir / 'pytest').write_text(
        '#!/bin/sh\nprintf \'pytest %s\\n\' "$*" >> "$STUB_LOG"\n'
        'for a in "$@"; do case "$a" in --junitxml=*) out="${a#--junitxml=}";; esac; done\n'
        'if [ "${JUNIT_KIND:-clean}" != none ]; then printf \'%s\' "$JUNIT_BODY" > "$out"; fi\n'
        'exit "${PYTEST_STATUS:-0}"\n')
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    return bin_dir


def run_job(tmp_path, job, *, start=None, doc=None, **env):
    """Run (a tail of) a job's script lines as GitLab would: fail fast, no -u noise."""
    bin_dir = stage(tmp_path)
    steps = lines((doc or GITLAB)[job])
    if start:
        steps = steps[next(i for i, s in enumerate(steps) if s.startswith(start)):]
    log = tmp_path / 'log'
    log.write_text('')
    base = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'STUB_LOG': str(log),
            'REAL_PYTHON': sys.executable, 'CI_PROJECT_DIR': str(tmp_path)}
    for key in ('CI_TASK_ARCHIVE', 'CI_HELM_ARCHIVE', 'CI_BM25_CACHE_DIR', 'FAIL_ON',
                'GITLAB_TOKEN', 'CI_MERGE_REQUEST_IID'):
        base.pop(key, None)
    proc = subprocess.run(['sh', '-e', '-c', '\n'.join(steps)], cwd=tmp_path, check=False,
                          env={**base, **env}, capture_output=True, text=True)
    return proc, log.read_text()


@pytest.mark.parametrize('failing, expected_after', [
    ('', 3), ('ruff check', 0), ('mypy src', 1), ('mypy --follow-imports=silent', 2)])
def test_lint_job_fails_when_any_tool_fails(tmp_path, failing, expected_after):
    proc, log = run_job(tmp_path, 'lint', FAIL_ON=failing)
    assert (proc.returncode == 0) == (failing == ''), proc.stderr
    executed = log.splitlines()
    # Fail fast: nothing after the failing command runs, and it is the last one.
    assert len(executed) == (3 if not failing else expected_after + 1)
    if failing:
        assert executed[-1].startswith(failing)


def test_lint_scripts_are_the_same_commands_github_runs():
    assert lines(GITLAB['lint']) == tool_lines(github_step('lint', 'ruff check')['run'])


@pytest.mark.parametrize('job, report, tail', [
    ('test', 'unit-junit.xml', 'rm -f unit-junit.xml'),
    ('sim', 'sim-junit.xml', 'rm -f sim-junit.xml')])
@pytest.mark.parametrize('kind, pytest_status, passes', [
    ('clean', 0, True),
    ('clean', 1, False),        # pytest itself failed
    ('failure', 0, False),      # report records a failure although status was 0
    ('failure', 1, False),
    ('error', 0, False),
    ('skipped', 0, False),      # skipped tests are not a pass
    ('xfail', 0, False),        # xfail-laden shard is not a pass
    ('empty', 0, False),        # nothing executed
    ('garbage', 0, False),
    ('doctype', 0, False),
    ('none', 0, False)])        # pytest produced no report at all
def test_unit_and_sim_jobs_reject_every_non_clean_outcome(
        tmp_path, job, report, tail, kind, pytest_status, passes):
    body = JUNIT.get(kind, '')
    proc, log = run_job(tmp_path, job, start=tail, JUNIT_KIND=kind, JUNIT_BODY=body,
                        PYTEST_STATUS=str(pytest_status))
    assert (proc.returncode == 0) is passes, proc.stdout + proc.stderr
    if pytest_status:
        assert 'check_junit_clean' not in log  # a failing run stops before the gate


def test_stale_report_cannot_satisfy_the_gate(tmp_path):
    # A clean report left by an earlier run must not survive a run that writes none.
    (tmp_path / 'unit-junit.xml').write_text(JUNIT['clean'])
    proc, _ = run_job(tmp_path, 'test', start='rm -f unit-junit.xml', JUNIT_KIND='none')
    assert proc.returncode != 0


@pytest.mark.parametrize('failing_env, passes', [
    ({}, True), ({'BM25_STATUS': '3'}, False), ({'DOCTOR_STATUS': '2'}, False),
    ({'CI_BM25_CACHE_DIR': None}, False), ({'CI_TASK_ARCHIVE': None}, False),
    ({'CI_HELM_ARCHIVE': None}, False)])
def test_sim_job_prerequisites_fail_before_pytest(tmp_path, failing_env, passes):
    env = {'CI_TASK_ARCHIVE': str(tmp_path / 'task.tgz'), 'CI_HELM_ARCHIVE': str(tmp_path / 'helm.tgz'),
           'CI_BM25_CACHE_DIR': str(tmp_path / 'bm25'), 'JUNIT_BODY': JUNIT['clean']}
    for key, value in failing_env.items():
        if value is None:
            env.pop(key)
        else:
            env[key] = value
    proc, log = run_job(tmp_path, 'sim', **env)
    assert (proc.returncode == 0) is passes, proc.stdout + proc.stderr
    assert ('pytest ' in log) is passes
    if passes:
        selection = next(l for l in log.splitlines() if l.startswith('pytest '))
        assert '-m integration' in selection and '--junitxml=sim-junit.xml' in selection


@pytest.mark.parametrize('status', [0, 1, 2])
def test_gate_l1_regression_fails_the_job_and_posts_no_note(tmp_path, status):
    (tmp_path / 'eval-delta.md').write_text('delta')
    proc, log = run_job(tmp_path, 'gate-l1', GATE_STATUS=str(status),
                        GITLAB_TOKEN='synthetic' if status else '',
                        CI_MERGE_REQUEST_IID='3' if status else '')
    assert proc.returncode == status, proc.stderr
    assert 'gate_l1.py' in log
    assert 'curl' not in log  # a failed gate never reaches the advisory MR note


def test_masking_a_command_would_make_the_scripts_pass_which_the_static_rule_forbids(tmp_path):
    # Negative control for the harness itself: with `|| true`, a failing ruff
    # passes the shell, and the static rule is what rejects that definition.
    doc = copy.deepcopy(GITLAB)
    doc['lint']['script'][0] += ' || true'
    proc, _ = run_job(tmp_path, 'lint', doc=doc, FAIL_ON='ruff check')
    assert proc.returncode == 0
    assert violations(doc)
    assert violations(GITLAB) == []


# ---- the real junit writer, not a hand-written report -----------------------

def real_pytest_report(tmp_path, body, *args):
    case = tmp_path / 'case'
    case.mkdir()
    (case / 'test_case.py').write_text('import pytest\n' + body)
    report = tmp_path / 'report.xml'
    subprocess.run([sys.executable, '-m', 'pytest', '-o', 'addopts=', '-p', 'no:cacheprovider',
                    '-q', f'--junitxml={report}', '--rootdir', str(case), str(case), *args],
                   cwd=case, check=False, capture_output=True, text=True,
                   env={**os.environ, 'PYTEST_ADDOPTS': ''})
    return subprocess.run([sys.executable, str(ROOT / 'scripts/check_junit_clean.py'), str(report)],
                          check=False, capture_output=True, text=True)


@pytest.mark.parametrize('body, passes', [
    ('def test_ok():\n    assert True\n', True),
    ('def test_ok():\n    assert True\n\n@pytest.mark.skip\ndef test_s():\n    pass\n', False),
    ('def test_ok():\n    assert True\n\n@pytest.mark.xfail\ndef test_x():\n    assert False\n', False),
    ('def test_ok():\n    assert True\n\ndef test_skip():\n    pytest.skip("env")\n', False),
    ('def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n', False),
    ('import missing_module_for_collection_error\n', False),
])
def test_real_pytest_reports_classify_as_the_gate_requires(tmp_path, body, passes):
    result = real_pytest_report(tmp_path, body)
    assert (result.returncode == 0) is passes, result.stdout + result.stderr


def test_junit_gate_uses_the_github_evidence_counting_rule():
    from scripts.ci_evidence import junit_bytes
    for kind, raw in JUNIT.items():
        try:
            counts = junit_bytes(raw.encode())
        except (ValueError, ET.ParseError):
            counts = None
        clean = counts is not None and not any(counts[k] for k in ('failed', 'errors', 'skipped'))
        assert clean is (kind == 'clean'), kind
