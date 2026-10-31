"""GitLab quality-gate parity with GitHub CI, and proof that failures propagate (#370).

Hermetic: both CI definitions are parsed, the GitLab job scripts are executed
under `sh -e` with stub tools, and every static rule has negative controls
(mutated copies of the real definition must be rejected). Pipeline shape
(GH#1118 F11) is evaluated from the real workflow and job rules: ready merge
request and tag pipelines run every lane on `gate`, draft merge request
pipelines run every lane on `wip`, branch pipelines run the lane selector on
`wip` and its child pipeline. The select job runs for real against
synthetic git histories, and the unit shards are checked against a real
collection. A real GitLab pipeline run is still required to prove runner,
image, service, child-pipeline and auto-cancel behavior.
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
from scripts.gitlab_lanes import LANE_JOBS, child_config, selected_jobs

ROOT = Path(__file__).resolve().parents[1]
GITLAB = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
GITHUB = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
REQUIRED_JOBS = {'hygiene', 'lint', 'test', 'hazards', 'sim', 'gate-l1'}
SELECTION_JOBS = {'select', 'lanes'}
UNIT_SHARDS = 4
# The only selection options the unit lane may pass: the shard plugin with
# GitLab's parallel index/total and the GitHub durations file.
UNIT_SHARD_ARGS = ['-p', 'tests.ci_shard', '--unit-shard=$CI_NODE_INDEX', '--unit-shards=$CI_NODE_TOTAL',
                   '--unit-durations=tests/unit-durations.json']
SELECT_SCRIPT = [
    'git fetch --no-tags origin "+refs/heads/${CI_DEFAULT_BRANCH}:refs/remotes/origin/${CI_DEFAULT_BRANCH}"',
    'BASE_SHA="$(git merge-base "refs/remotes/origin/${CI_DEFAULT_BRANCH}" "$CI_COMMIT_SHA")"',
    'python scripts/review_tooling.py profile --base "$BASE_SHA" --head "$CI_COMMIT_SHA" --out lanes-manifest.json',
    'python scripts/gitlab_lanes.py --manifest lanes-manifest.json --out lanes.yml',
]
LANES_TRIGGER = {'include': [{'artifact': 'lanes.yml', 'job': 'select'}], 'strategy': 'depend',
                 'forward': {'yaml_variables': False}}
# Integration-marked files that drive a local Docker daemon; the offline GitLab
# runner only has the mirrored Qdrant service, so the sim lane omits them.
DOCKER_ONLY_SIM_FILES = {'tests/test_qdrant_auth.py', 'tests/test_openwebui_proxy_nginx.py'}
# Selection-narrowing or evidence-changing pytest options: the unit lane must
# run the complete eligible suite exactly as pyproject defines it (sharding
# aside, see UNIT_SHARD_ARGS).
NARROWING_FLAGS = ('-k', '-m', '-o', '-p', '--ignore', '--ignore-glob', '--deselect', '--lf',
                   '--ff', '--co', '--collect-only', '--unit-shard', '--unit-shards', '--unit-durations',
                   '--pyargs', '--continue-on-collection-errors')
MASKS = (r'\|\|\s*(true|:|exit\s+0)\b', r';\s*(true|:)\s*$', r'\bset\s+\+e\b', r'\bexit\s+0\b')
# Jobs run on the dev workspace image (ADR-0009 section 8) by the digest its pin
# records, under the documented Quay role path (docs/deploy.md), and use its
# installed environment (prepare_python.py --kit) and its Task and Helm
# (scripts/devkit/link-tools.sh) instead of wheels and archives (GH#1118 F5).
KIT_PIN = ROOT / 'scripts/tools/devkit-pin.txt'
KIT_REPOSITORY = '$CI_APPROVED_REGISTRY/qdrant-pdf-rag-devkit'
KIT_PYTHON = 'python3 scripts/prepare_python.py --kit --venv "$CI_PROJECT_DIR/.venv"'
KIT_TOOLS = 'sh scripts/devkit/link-tools.sh --task-archive "$CI_TASK_ARCHIVE"'
TOOL_JOBS = ('test', 'hazards', 'sim')
# Retired inputs (docs/release-notes.md): no job may read them again.
RETIRED_INPUTS = ('install-task.sh', 'install-helm.sh', 'CI_HELM_ARCHIVE', 'PIP_FIND_LINKS', 'PIP_INDEX_URL',
                  '--wheelhouse', '--internal-index')


def jobs(doc):
    return {k: v for k, v in doc.items()
            if isinstance(v, dict) and not k.startswith('.') and k not in ('workflow', 'default', 'variables')}


def merge(base, over):
    """GitLab's merge for `extends` and `include`: hashes deep-merge, the later side wins."""
    out = dict(base)
    for key, value in over.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def resolve(doc, name):
    job = doc[name]
    parents = job.get('extends', [])
    out = {}
    for parent in [parents] if isinstance(parents, str) else parents:
        out = merge(out, resolve(doc, parent))
    return merge(out, {k: v for k, v in job.items() if k != 'extends'})


def lines(job, key='script'):
    return [step for step in job.get(key, []) if isinstance(step, str)]


def pytest_line(job):
    found = [s for s in lines(job) if s.startswith(('pytest ', 'python -m pytest '))]
    return found[0] if len(found) == 1 else None


def tool_lines(text, tools=('ruff', 'mypy')):
    return [l.strip() for l in text.splitlines() if l.strip().split(' ')[0] in tools]


def github_step(job, needle):
    return next(s for s in GITHUB['jobs'][job]['steps'] if needle in s.get('run', ''))


def ignores(command):
    return {t.split('=', 1)[1] for t in shlex.split(command) if t.startswith('--ignore=')}


def expression_matches(expression, variables):
    """The rule expression forms this file may use: `$V`, `$V == "x"`, `$V != "x"`, joined by &&."""
    for clause in (c.strip() for c in expression.split('&&')):
        compare = re.fullmatch(r'\$(\w+) (==|!=) "([^"]*)"', clause)
        bare = re.fullmatch(r'\$(\w+)', clause)
        assert compare or bare, f'unsupported rule expression {expression!r}'
        if compare:
            matched = (variables.get(compare.group(1)) == compare.group(3)) == (compare.group(2) == '==')
        else:
            matched = bool(variables.get(bare.group(1)))
        if not matched:
            return False
    return True


def first_rule(rules, variables):
    return next((r for r in rules if 'if' not in r or expression_matches(r['if'], variables)), None)


def pipeline(doc, variables):
    """The pipeline GitLab creates for these predefined variables, or None when none is."""
    rule = first_rule(doc['workflow']['rules'], variables)
    if rule is None or rule.get('when') == 'never':
        return None
    scope = {**variables, **rule.get('variables', {})}
    names = set()
    for name in jobs(doc):
        job_rules = resolve(doc, name).get('rules')
        matched = {} if job_rules is None else first_rule(job_rules, scope)
        if matched is not None and matched.get('when', 'on_success') != 'never':
            names.add(name)
    return {'variables': rule.get('variables', {}), 'jobs': names,
            'auto_cancel': {**doc['workflow'].get('auto_cancel', {}), **rule.get('auto_cancel', {})}}


def child_doc(doc, config):
    """The child pipeline configuration: the included file, then the generated keys over it."""
    assert config['include'] == [{'local': '.gitlab-ci.yml'}]
    return merge(doc, {k: v for k, v in config.items() if k != 'include'})


MR = {'CI_PIPELINE_SOURCE': 'merge_request_event', 'CI_MERGE_REQUEST_IID': '3',
      'CI_MERGE_REQUEST_DRAFT': 'false'}
DRAFT_MR = {**MR, 'CI_MERGE_REQUEST_DRAFT': 'true'}
TAG = {'CI_PIPELINE_SOURCE': 'push', 'CI_COMMIT_TAG': 'v1.2.3'}
BRANCH = {'CI_PIPELINE_SOURCE': 'push', 'CI_COMMIT_BRANCH': 'feature'}
CHILD = {'CI_PIPELINE_SOURCE': 'parent_pipeline', 'CI_COMMIT_BRANCH': 'feature'}
# A GitLab before 17.10 does not set CI_MERGE_REQUEST_DRAFT: drafts fall back to the gate.
UNSET_DRAFT_MR = {k: v for k, v in DRAFT_MR.items() if k != 'CI_MERGE_REQUEST_DRAFT'}
GATE_SCENARIOS = {'merge request': MR, 'tag': TAG,
                  'merge request without CI_MERGE_REQUEST_DRAFT': UNSET_DRAFT_MR}
# Draft merge requests: every lane, no selection (#808), but on wip (ADR-0009 section 2).
DRAFT_SCENARIOS = {'draft merge request': DRAFT_MR}
WIP_SCENARIOS = {
    'branch': BRANCH,
    'default branch': {'CI_PIPELINE_SOURCE': 'push', 'CI_COMMIT_BRANCH': 'main'},
    'scheduled': {'CI_PIPELINE_SOURCE': 'schedule', 'CI_COMMIT_BRANCH': 'main'},
}
NOT_ADMITTED = {
    # The merge request pipeline is the only one for a branch with an open MR.
    'branch with an open merge request': {**BRANCH, 'CI_OPEN_MERGE_REQUESTS': 'group/project!3'},
    'api call without a ref': {'CI_PIPELINE_SOURCE': 'api'},
}


def image_references(doc):
    default = doc.get('default', {}).get('image')
    refs = {'default image': default, 'CI_PYTHON_IMAGE': doc['variables']['CI_PYTHON_IMAGE']}
    for name, job in jobs(doc).items():
        if 'image' in job:
            refs[f'{name} image'] = job['image']
        for index, service in enumerate(job.get('services', [])):
            refs[f'{name} service {index}'] = service if isinstance(service, str) else service['name']
    return refs


def kit_image(pin_text):
    """The job image a devkit-pin.txt text names; None while it records no digest."""
    digest = re.search(r'(?m)^digest: (sha256:[0-9a-f]{64})$', pin_text)
    return f'{KIT_REPOSITORY}@{digest.group(1)}' if digest else None


def kit_image_problem(doc, pin_text):
    if doc['variables']['CI_PYTHON_IMAGE'] != kit_image(pin_text):
        return 'CI_PYTHON_IMAGE default is not the dev kit image by the digest in scripts/tools/devkit-pin.txt'
    return None


def kit_violations(doc):
    """Python jobs use the kit's environment and tools, never a wheelhouse or an archive install."""
    found = []
    base = '\n'.join(lines(doc['.py-base'], 'before_script'))
    if KIT_PYTHON not in base or '/opt/devkit/locks' not in base \
            or 'export PATH="$CI_PROJECT_DIR/.venv/bin:$PATH"' not in base:
        found.append('.py-base: Python does not come from the kit (prepare_python.py --kit)')
    all_jobs = jobs(doc)
    for name in ('lint', 'test', 'hazards', 'sim', 'gate-l1'):
        parents = all_jobs[name].get('extends', [])
        if '.py-base' not in ([parents] if isinstance(parents, str) else parents):
            found.append(f'{name}: does not run on the kit environment (.py-base)')
    for name in TOOL_JOBS:
        body = lines(all_jobs[name])
        if KIT_TOOLS not in body:
            found.append(f'{name}: Task and Helm do not come from the kit (link-tools.sh)')
            continue
        users = [i for i, s in enumerate(body) if 'agent_doctor' in s or 'run-task.sh' in s
                 or s.startswith(('pytest ', 'python -m pytest '))]
        if not users or body.index(KIT_TOOLS) > min(users):
            found.append(f'{name}: tools used before link-tools.sh verified them')
    refusal = [s for s in lines(all_jobs['hygiene']) if 'CI_HELM_ARCHIVE' in s]
    for name, node in doc.items():
        if not isinstance(node, dict):
            continue
        for key in ('script', 'before_script', 'after_script'):
            for step in lines(node, key):
                if step in refusal:
                    continue
                for retired in RETIRED_INPUTS:
                    if retired in step:
                        found.append(f'{name}: reads the retired {retired}')
    if len(refusal) != 1 or 'exit 1' not in refusal[0]:
        found.append('hygiene: the retired CI_HELM_ARCHIVE is not refused')
    return found


def keys_anywhere(node, key):
    if isinstance(node, dict):
        return (key in node) or any(keys_anywhere(v, key) for v in node.values())
    return isinstance(node, list) and any(keys_anywhere(v, key) for v in node)


# Rule forms the evaluator models. Anything else (changes:, exists:, manual or
# delayed `when`) could gate or defer a lane outside this test's model.
WORKFLOW_RULE_KEYS = {'if', 'when', 'variables', 'auto_cancel'}
JOB_RULE_KEYS = {'if', 'when'}


def rule_shape_violations(doc):
    found = []
    for rule in doc['workflow']['rules']:
        if set(rule) - WORKFLOW_RULE_KEYS or rule.get('when', 'always') not in ('always', 'never'):
            found.append(f'workflow rule {rule} is outside the modelled forms')
    for name, node in doc.items():
        for rule in (node.get('rules') or []) if isinstance(node, dict) and name != 'workflow' else []:
            if set(rule) - JOB_RULE_KEYS or rule.get('when', 'on_success') not in ('on_success', 'never'):
                found.append(f'{name}: rule {rule} can defer, gate or skip a lane silently')
    return found


def pipeline_violations(doc):
    """Pipeline shape (GH#1118 F11): which jobs run where, on which runner tag."""
    found = rule_shape_violations(doc)
    for label, variables in GATE_SCENARIOS.items():
        run = pipeline(doc, variables)
        if run is None:
            found.append(f'{label} pipelines are not admitted')
            continue
        if run['jobs'] != REQUIRED_JOBS:
            found.append(f'{label} pipelines must run every lane directly, ran {sorted(run["jobs"])}')
        if run['variables'].get('CI_RUNNER_TAG') != 'gate':
            found.append(f'{label} pipelines must run on the gate runner tag')
    for label, variables in DRAFT_SCENARIOS.items():
        run = pipeline(doc, variables)
        if run is None:
            found.append(f'{label} pipelines are not admitted')
            continue
        if run['jobs'] != REQUIRED_JOBS:
            found.append(f'{label} pipelines must run every lane directly, ran {sorted(run["jobs"])}')
        if run['variables'].get('CI_RUNNER_TAG') != 'wip':
            found.append(f'{label} pipelines must run on the wip runner tag')
    for label, variables in WIP_SCENARIOS.items():
        run = pipeline(doc, variables)
        if run is None:
            found.append(f'{label} pipelines are not admitted')
            continue
        if run['jobs'] != SELECTION_JOBS:
            found.append(f'{label} pipelines must run only the lane selector, ran {sorted(run["jobs"])}')
        if run['variables'].get('CI_RUNNER_TAG') != 'wip':
            found.append(f'{label} pipelines must run on the wip runner tag')
    for label, variables in NOT_ADMITTED.items():
        if pipeline(doc, variables) is not None:
            found.append(f'{label}: a pipeline is created')
    tag = pipeline(doc, TAG)
    if tag and tag['auto_cancel'].get('on_new_commit') != 'none':
        found.append('tag pipelines can be auto-cancelled')
    for label, variables in {'merge request': MR, 'draft merge request': DRAFT_MR,
                             'branch': BRANCH, 'child': CHILD}.items():
        run = pipeline(doc, variables)
        if run and run['auto_cancel'].get('on_new_commit') != 'interruptible':
            found.append(f'{label} pipelines are not auto-cancelled by a newer commit')
    if set(LANE_JOBS.values()) != REQUIRED_JOBS:
        found.append('the lane generator does not map every lane job')
    unselected = pipeline(doc, CHILD)
    if unselected is None or unselected['jobs']:
        found.append('a child pipeline must run exactly the generated selection')
    # A merge request opened while the parent ran must not strand its trigger job.
    if pipeline(doc, {**CHILD, 'CI_OPEN_MERGE_REQUESTS': 'group/project!3'}) is None:
        found.append('a child pipeline is refused once a merge request is open')
    full = pipeline(child_doc(doc, child_config(sorted(REQUIRED_JOBS))), CHILD)
    if full is None or full['jobs'] != REQUIRED_JOBS or full['variables'].get('CI_RUNNER_TAG') != 'wip':
        found.append('a full selection does not run every lane on wip in the child pipeline')
    if doc['default'].get('tags') != ['$CI_RUNNER_TAG']:
        found.append('default runner tag is not the workflow-selected CI_RUNNER_TAG')
    if doc['default'].get('interruptible') is not True:
        found.append('jobs are not interruptible by default')
    for name, job in jobs(doc).items():
        resolved = resolve(doc, name)
        if 'tags' in resolved:
            found.append(f'{name}: overrides the runner tag')
        if 'rules' in job:
            found.append(f'{name}: rules must come from .gate-pipeline or .wip-pipeline')
        if 'trigger' in resolved and resolved.get('interruptible') is not True:
            found.append(f'{name}: trigger job is not interruptible')
        if resolved.get('interruptible', True) is not True:
            found.append(f'{name}: not interruptible')
    if lines(doc['select']) != SELECT_SCRIPT or doc['select'].get('variables', {}).get('GIT_DEPTH') != '0':
        found.append('select: must classify the full branch diff with review_tooling.py and the generator')
    if 'lanes.yml' not in doc['select'].get('artifacts', {}).get('paths', []):
        found.append('select: child configuration not retained')
    if doc['lanes'].get('trigger') != LANES_TRIGGER or doc['lanes'].get('needs') != ['select']:
        found.append('lanes: child pipeline must come from select, mirror its status and forward no YAML variables')
    return found


def violations(doc):
    """Every rule the GitLab definition must satisfy; empty means conforming."""
    found = []
    all_jobs = jobs(doc)
    for label, ref in image_references(doc).items():
        if 'CI_REGISTRY' in ref:
            found.append(f'{label}: derived from the GitLab registry, not the approved registry')
    if 'CI_APPROVED_REGISTRY' not in doc['variables']['CI_PYTHON_IMAGE']:
        found.append('CI_PYTHON_IMAGE default is not derived from CI_APPROVED_REGISTRY')
    if 'CI_APPROVED_REGISTRY' in doc['variables']:
        found.append('CI_APPROVED_REGISTRY must be operator-set, with no default')
    problem = kit_image_problem(doc, KIT_PIN.read_text())
    if problem:
        found.append(problem)
    for missing in sorted((REQUIRED_JOBS | SELECTION_JOBS) - set(all_jobs)):
        found.append(f'missing job {missing}')
    if keys_anywhere(doc, 'allow_failure'):
        found.append('allow_failure can turn a failure green')
    for name in all_jobs:
        job = resolve(doc, name)
        if job.get('when', 'on_success') != 'on_success' or 'only' in job or 'except' in job:
            found.append(f'{name}: conditional job can be skipped silently')
        for key in ('script', 'before_script', 'after_script'):
            for step in lines(job, key):
                if 'NOTE_BODY=' in step:
                    continue  # advisory MR note; runs after the gate command
                if any(re.search(p, step, re.MULTILINE) for p in MASKS):
                    found.append(f'{name}: masked exit status in {key}')
    if found:
        return found
    found += pipeline_violations(doc)
    found += kit_violations(doc)
    if not doc['stages'].index('hygiene') < doc['stages'].index('test'):
        found.append('stage order')
    if all_jobs['lint'].get('stage') != 'hygiene':
        found.append('lint must be a hygiene-stage gate')
    expected = tool_lines(github_step('lint', 'ruff check')['run'])
    if lines(all_jobs['lint']) != expected:
        found.append('lint commands differ from the GitHub lint job')
    hygiene = '\n'.join(lines(all_jobs['hygiene']))
    if not tag_guard(doc) or 'CI_COMMIT_REF_PROTECTED' not in tag_guard(doc):
        found.append('hygiene: tag pipelines are not restricted to protected tags')
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
    unit = shlex.split(pytest_line(all_jobs['test']) or '')
    unit = unit[3:] if unit[:3] == ['python', '-m', 'pytest'] else unit[1:]
    shard_args = [t for t in unit if t in ('-p', 'tests.ci_shard') or t.startswith('--unit-')]
    if shard_args != UNIT_SHARD_ARGS or all_jobs['test'].get('parallel') != UNIT_SHARDS:
        found.append(f'test: must run shard $CI_NODE_INDEX of parallel: {UNIT_SHARDS} with the GitHub layout')
    github_unit = github_step('unit', '--unit-shards=')['run']
    if '--unit-durations tests/unit-durations.json' not in github_unit:
        found.append('test: GitHub no longer balances shards with tests/unit-durations.json')
    rest = [t for t in unit if t not in shard_args]
    test_flags = [t for t in rest if any(t == f or t.startswith(f + '=') for f in NARROWING_FLAGS)]
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


def tag_guard(doc):
    return next((s for s in lines(jobs(doc)['hygiene']) if 'CI_COMMIT_TAG' in s), None)


def mutated(mutator):
    doc = copy.deepcopy(GITLAB)
    mutator(doc)
    return doc

def test_current_gitlab_definition_conforms():
    assert violations(GITLAB) == []


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


UNIT_LINE = pytest_line(GITLAB['test'])


def workflow_rule(doc, needle):
    return next(r for r in doc['workflow']['rules'] if r['if'] == needle)


DRAFT_RULE = '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_DRAFT == "true"'


def set_unit_line(doc, line):
    doc['test']['script'][doc['test']['script'].index(UNIT_LINE)] = line


NEGATIVE_CONTROLS = {
    'allow_failure on unit lane': lambda d: d['test'].update(allow_failure=True),
    'allow_failure on the child trigger': lambda d: d['lanes'].update(allow_failure=True),
    'allow_failure in a template': lambda d: d['.py-base'].update(allow_failure=True),
    'manual unit lane': lambda d: d['test'].update(when='manual'),
    'pytest status masked': lambda d: set_unit_line(d, UNIT_LINE + ' || true'),
    'skip gate removed from unit': lambda d: d['test']['script'].remove(
        'python scripts/check_junit_clean.py unit-junit.xml'),
    'skip gate before pytest': lambda d: d['test']['script'].insert(
        0, d['test']['script'].pop()),
    'report not retained': lambda d: d['test'].pop('artifacts'),
    'unit selection narrowed by -k': lambda d: set_unit_line(d, UNIT_LINE + ' -k "not slow"'),
    'unit not sharded': lambda d: d['test'].pop('parallel'),
    'unit sharded into 8 jobs': lambda d: d['test'].update(parallel=8),
    'every shard runs shard 1': lambda d: set_unit_line(
        d, UNIT_LINE.replace('--unit-shard="$CI_NODE_INDEX"', '--unit-shard=1')),
    'shard plugin not loaded': lambda d: set_unit_line(d, UNIT_LINE.replace('-p tests.ci_shard ', '')),
    'round-robin instead of the durations layout': lambda d: set_unit_line(
        d, UNIT_LINE.replace(' --unit-durations=tests/unit-durations.json', '')),
    'extra plugin in the unit lane': lambda d: set_unit_line(d, UNIT_LINE + ' -p no:randomly'),
    'doctor removed': lambda d: d['test']['script'].remove(
        'python scripts/agent_doctor.py --python "$(command -v python)"'),
    'runner contracts allowed to skip': lambda d: d['test']['variables'].clear(),
    'ruff scope shrunk': lambda d: d['lint']['script'].__setitem__(0, 'ruff check src tests'),
    'sim narrowed beyond docker-only': lambda d: extend(d['sim'], 'python -m pytest ',
                                                         ' --ignore=tests/test_mcp_sim.py'),
    'sim service not approved digest': lambda d: d['sim']['services'][0].update(
        name='${CI_APPROVED_REGISTRY}/qdrant/qdrant:latest'),
    'sim service from the GitLab registry': lambda d: d['sim']['services'][0].update(
        name=d['sim']['services'][0]['name'].replace('CI_APPROVED_REGISTRY', 'CI_REGISTRY')),
    'job image from the GitLab registry': lambda d: d['variables'].update(
        CI_PYTHON_IMAGE='$CI_REGISTRY/ci/python:3.14'),
    'hardcoded registry host for the job image': lambda d: d['variables'].update(
        CI_PYTHON_IMAGE='registry.example.invalid/ci/python:3.14'),
    'approved registry given a default': lambda d: d['variables'].update(
        CI_APPROVED_REGISTRY='$CI_REGISTRY/ci'),
    'job image is the pre-kit Python image': lambda d: d['variables'].update(
        CI_PYTHON_IMAGE='$CI_APPROVED_REGISTRY/ci/python:3.14'),
    'job image is the kit by a mutable tag': lambda d: d['variables'].update(
        CI_PYTHON_IMAGE=KIT_REPOSITORY + ':latest'),
    'Python from a wheelhouse': lambda d: d['.py-base'].update(before_script=[
        'python scripts/prepare_python.py --python "$(command -v python)" --wheelhouse "$PIP_FIND_LINKS"']),
    'Python job outside the kit environment': lambda d: d['gate-l1'].update(extends='.gate-pipeline'),
    'Helm from an archive': lambda d: d['sim']['script'].insert(
        1, 'sh scripts/tools/install-helm.sh --archive "$CI_HELM_ARCHIVE"'),
    'unit tools not from the kit': lambda d: d['test']['script'].remove(KIT_TOOLS),
    'hazards tools not from the kit': lambda d: replace(
        d['hazards'], 'sh scripts/devkit/link-tools.sh', 'sh scripts/tools/install-task.sh --archive x'),
    'tools linked after the doctor': lambda d: d['sim']['script'].append(
        d['sim']['script'].pop(d['sim']['script'].index(KIT_TOOLS))),
    'retired Helm archive accepted': lambda d: d['hygiene']['script'].remove(
        next(s for s in d['hygiene']['script'] if 'CI_HELM_ARCHIVE' in s)),
    'tag pipelines not admitted': lambda d: d['workflow']['rules'].remove(
        workflow_rule(d, '$CI_COMMIT_TAG')),
    'branch pipelines not admitted': lambda d: d['workflow']['rules'].pop(),
    'child pipeline not admitted': lambda d: d['workflow']['rules'].pop(0),
    'open merge request runs a duplicate branch pipeline': lambda d: d['workflow']['rules'].remove(
        workflow_rule(d, '$CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS')),
    'merge request pipelines on wip': lambda d: workflow_rule(
        d, '$CI_PIPELINE_SOURCE == "merge_request_event"')['variables'].update(CI_RUNNER_TAG='wip'),
    'draft merge requests on gate': lambda d: workflow_rule(d, DRAFT_RULE)['variables'].update(
        CI_RUNNER_TAG='gate'),
    'draft merge request rule removed': lambda d: d['workflow']['rules'].remove(workflow_rule(d, DRAFT_RULE)),
    'every merge request on wip': lambda d: workflow_rule(d, DRAFT_RULE).update(
        {'if': '$CI_PIPELINE_SOURCE == "merge_request_event"'}),
    'draft rule matches a ready merge request': lambda d: workflow_rule(d, DRAFT_RULE).update(
        {'if': '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_DRAFT'}),
    'draft merge requests run no lanes': lambda d: d['.gate-pipeline']['rules'].insert(
        0, {'if': '$CI_MERGE_REQUEST_DRAFT == "true"', 'when': 'never'}),
    'draft merge requests run the selector': lambda d: d['.wip-pipeline']['rules'].append(
        {'if': '$CI_MERGE_REQUEST_DRAFT == "true"'}),
    'draft merge request pipelines never auto-cancelled': lambda d: workflow_rule(d, DRAFT_RULE).update(
        auto_cancel={'on_new_commit': 'none'}),
    'tag pipelines on wip': lambda d: workflow_rule(d, '$CI_COMMIT_TAG')['variables'].update(
        CI_RUNNER_TAG='wip'),
    'branch pipelines on gate': lambda d: d['workflow']['rules'][-1]['variables'].update(CI_RUNNER_TAG='gate'),
    'child pipelines on gate': lambda d: d['workflow']['rules'][0]['variables'].update(CI_RUNNER_TAG='gate'),
    'a job pins its own runner tag': lambda d: d['sim'].update(tags=['wip']),
    'no auto-cancel on a newer commit': lambda d: d['workflow'].pop('auto_cancel'),
    'tag pipelines can be auto-cancelled': lambda d: workflow_rule(d, '$CI_COMMIT_TAG').pop('auto_cancel'),
    'jobs not interruptible by default': lambda d: d['default'].pop('interruptible'),
    'one job not interruptible': lambda d: d['sim'].update(interruptible=False),
    'child trigger not interruptible': lambda d: d['lanes'].pop('interruptible'),
    'lane jobs run in branch pipelines': lambda d: d['.gate-pipeline']['rules'].append(
        {'if': '$CI_COMMIT_BRANCH'}),
    'tag pipelines lose the lanes': lambda d: d['.gate-pipeline']['rules'].pop(),
    'lane job with its own rules': lambda d: d['sim'].update(rules=[{'if': '$CI_COMMIT_TAG'}]),
    'manual lanes in tag pipelines': lambda d: d['.gate-pipeline']['rules'][-1].update(when='manual'),
    'merge request lanes gated on changed paths': lambda d: d['.gate-pipeline']['rules'][0].update(
        changes=['src/**/*']),
    'delayed merge request lanes': lambda d: d['.gate-pipeline']['rules'][0].update(when='delayed'),
    'manual selector': lambda d: d['.wip-pipeline']['rules'][-1].update(when='manual'),
    'workflow rule gated on changed paths': lambda d: d['workflow']['rules'][-1].update(changes=['src/**/*']),
    'selector runs inside the child pipeline': lambda d: d['.wip-pipeline']['rules'].pop(0),
    'selector diff is only the newest push': lambda d: d['select']['script'].__setitem__(
        1, 'BASE_SHA="$CI_COMMIT_BEFORE_SHA"'),
    'selector bypasses the classifier': lambda d: d['select']['script'].__setitem__(
        2, 'python scripts/review_tooling.py profile --files README.md --out lanes-manifest.json'),
    'selector on a shallow clone': lambda d: d['select'].pop('variables'),
    'child status not mirrored': lambda d: d['lanes']['trigger'].pop('strategy'),
    'YAML variables forwarded to the child': lambda d: d['lanes']['trigger'].update(
        forward={'yaml_variables': True}),
    'child configuration not from select': lambda d: d['lanes']['trigger'].update(
        include=[{'local': '.gitlab-ci.yml'}]),
    'tag guard removed': lambda d: d['hygiene']['script'].remove(tag_guard(d)),
    'tag guard ignores protection': lambda d: d['hygiene']['script'].__setitem__(
        d['hygiene']['script'].index(tag_guard(d)), tag_guard(d).replace('CI_COMMIT_REF_PROTECTED', 'X')),
    'gate-l1 args changed': lambda d: d['gate-l1']['script'].__setitem__(
        0, d['gate-l1']['script'][0].replace('--delta eval-delta.md', '')),
    'hazards removed': lambda d: d.pop('hazards'),
}


@pytest.mark.parametrize('name', sorted(NEGATIVE_CONTROLS))
def test_static_rules_reject_each_weakened_definition(name):
    doc = mutated(lambda d: NEGATIVE_CONTROLS[name](d))
    assert violations(doc), f'{name} was not detected'


# ---- behavior: execute the real job scripts with stub tools -----------------

STUB_PYTHON = '''#!/bin/sh
if [ "$1" = -m ] && [ "$2" = pytest ]; then shift 2; exec pytest "$@"; fi
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
    (tmp_path / 'scripts/devkit').mkdir(exist_ok=True)
    (tmp_path / 'scripts/devkit/link-tools.sh').write_text(
        'printf \'link-tools %s\\n\' "$*" >> "$STUB_LOG"\nexit "${LINK_STATUS:-0}"\n')
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
    for key in ('CI_TASK_ARCHIVE', 'CI_HELM_ARCHIVE', 'CI_BM25_CACHE_DIR', 'FAIL_ON', 'LINK_STATUS',
                'GITLAB_TOKEN', 'CI_MERGE_REQUEST_IID'):
        base.pop(key, None)
    proc = subprocess.run(['sh', '-e', '-c', '\n'.join(steps)], cwd=tmp_path, check=False,
                          env={**base, **env}, capture_output=True, text=True)
    return proc, log.read_text()


@pytest.mark.parametrize('failing, expected_after', [
    ('', 3), ('ruff check', 0), ('mypy src', 1)])
def test_lint_job_fails_when_any_tool_fails(tmp_path, failing, expected_after):
    proc, log = run_job(tmp_path, 'lint', FAIL_ON=failing)
    assert (proc.returncode == 0) == (failing == ''), proc.stderr
    executed = log.splitlines()
    # Fail fast: nothing after the failing command runs, and it is the last one.
    assert len(executed) == (3 if not failing else expected_after + 1)
    if failing:
        assert executed[-1].startswith(failing)


@pytest.mark.parametrize('job, report, tail, kind, pytest_status, passes', [
    *[('test', 'unit-junit.xml', 'rm -f unit-junit.xml', kind, status, passes) for kind, status, passes in (
        ('clean', 0, True),
        ('clean', 1, False),        # pytest itself failed
        ('failure', 0, False),      # report records a failure although status was 0
        ('skipped', 0, False),      # skipped tests are not a pass
        ('xfail', 0, False),        # xfail-laden shard is not a pass
        ('empty', 0, False),        # nothing executed
        ('garbage', 0, False),
        ('none', 0, False))],       # pytest produced no report at all
    *[('sim', 'sim-junit.xml', 'rm -f sim-junit.xml', kind, status, passes) for kind, status, passes in (
        ('clean', 0, True), ('clean', 1, False), ('skipped', 0, False))]])
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
    ({'CI_TASK_ARCHIVE': None}, False), ({'LINK_STATUS': '2'}, False)])
def test_sim_job_prerequisites_fail_before_pytest(tmp_path, failing_env, passes):
    env = {'CI_TASK_ARCHIVE': str(tmp_path / 'task.tgz'),
           'CI_BM25_CACHE_DIR': str(tmp_path / 'bm25'), 'JUNIT_BODY': JUNIT['clean']}
    for key, value in failing_env.items():
        if value is None:
            env.pop(key)
        else:
            env[key] = value
    proc, log = run_job(tmp_path, 'sim', **env)
    assert (proc.returncode == 0) is passes, proc.stdout + proc.stderr
    assert ('pytest ' in log) is passes
    if 'CI_TASK_ARCHIVE' in env:
        assert f'link-tools --task-archive {env["CI_TASK_ARCHIVE"]}' in log.splitlines()
    if passes:
        selection = next(l for l in log.splitlines() if l.startswith('pytest '))
        assert '-m integration' in selection and '--junitxml=sim-junit.xml' in selection


@pytest.mark.parametrize('status', [0, 1])
def test_gate_l1_regression_fails_the_job_and_posts_no_note(tmp_path, status):
    (tmp_path / 'eval-delta.md').write_text('delta')
    proc, log = run_job(tmp_path, 'gate-l1', GATE_STATUS=str(status),
                        GITLAB_TOKEN='synthetic' if status else '',
                        CI_MERGE_REQUEST_IID='3' if status else '')
    assert proc.returncode == status, proc.stderr
    assert 'gate_l1.py' in log
    assert 'curl' not in log  # a failed gate never reaches the advisory MR note


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


# ---- pipeline shape: gate, wip and lane selection (#808, GH#1118 F11) -------

@pytest.mark.parametrize('label', sorted(GATE_SCENARIOS))
def test_merge_request_and_tag_pipelines_run_every_lane_directly_on_gate(label):
    run = pipeline(GITLAB, GATE_SCENARIOS[label])
    assert run is not None
    assert run['jobs'] == REQUIRED_JOBS  # no selector, no child: #808 holds for the gate
    assert run['variables'] == {'CI_RUNNER_TAG': 'gate'}
    assert GITLAB['default']['tags'] == ['$CI_RUNNER_TAG']
    assert resolve(GITLAB, 'test')['parallel'] == UNIT_SHARDS


def test_a_draft_merge_request_runs_every_lane_directly_on_wip():
    # Marking a merge request ready starts no new pipeline, so a draft pipeline
    # may be the one a merge rests on: no selection (#808), but off the gate slot.
    run = pipeline(GITLAB, DRAFT_MR)
    assert run is not None
    assert run['jobs'] == REQUIRED_JOBS and not run['jobs'] & SELECTION_JOBS
    assert run['variables'] == {'CI_RUNNER_TAG': 'wip'}
    assert run['auto_cancel'] == {'on_new_commit': 'interruptible'}
    assert resolve(GITLAB, 'test')['parallel'] == UNIT_SHARDS


def test_only_a_draft_merge_request_leaves_the_gate():
    assert pipeline(GITLAB, {**DRAFT_MR, 'CI_MERGE_REQUEST_DRAFT': 'false'})['variables'] == {
        'CI_RUNNER_TAG': 'gate'}
    assert pipeline(GITLAB, UNSET_DRAFT_MR)['variables'] == {'CI_RUNNER_TAG': 'gate'}
    # A tag never matches the draft rule, even if a draft variable leaked into its scope.
    tag = pipeline(GITLAB, {**TAG, 'CI_MERGE_REQUEST_DRAFT': 'true'})
    assert tag['variables'] == {'CI_RUNNER_TAG': 'gate'} and tag['jobs'] == REQUIRED_JOBS
    assert tag['auto_cancel'] == {'on_new_commit': 'none'}


@pytest.mark.parametrize('label', sorted(WIP_SCENARIOS))
def test_branch_pipelines_run_the_selector_and_its_child_on_wip(label):
    run = pipeline(GITLAB, WIP_SCENARIOS[label])
    assert run is not None
    assert run['jobs'] == SELECTION_JOBS
    assert run['variables'] == {'CI_RUNNER_TAG': 'wip'}
    lanes = resolve(GITLAB, 'lanes')
    assert lanes['trigger']['include'] == [{'artifact': 'lanes.yml', 'job': 'select'}]
    assert lanes['trigger']['strategy'] == 'depend' and lanes['needs'] == ['select']
    child = pipeline(GITLAB, {**WIP_SCENARIOS[label], 'CI_PIPELINE_SOURCE': 'parent_pipeline'})
    assert child is not None and child['variables'] == {'CI_RUNNER_TAG': 'wip'}
    assert child['jobs'] == set()  # nothing runs in a child unless the generator selects it


@pytest.mark.parametrize('label', sorted(NOT_ADMITTED))
def test_no_duplicate_or_refless_pipeline_is_created(label):
    assert pipeline(GITLAB, NOT_ADMITTED[label]) is None


def test_every_job_is_interruptible_and_nothing_cancels_a_tag_pipeline():
    assert GITLAB['default']['interruptible'] is True
    for name in jobs(GITLAB):
        job = resolve(GITLAB, name)
        assert job.get('interruptible', GITLAB['default']['interruptible']) is True, name
        if 'trigger' in job:  # default keywords are not relied on for a trigger job
            assert GITLAB[name]['interruptible'] is True
    assert pipeline(GITLAB, TAG)['auto_cancel'] == {'on_new_commit': 'none'}
    assert not keys_anywhere(GITLAB, 'allow_failure')


@pytest.mark.parametrize('variables', [MR, DRAFT_MR, BRANCH, CHILD],
                         ids=['merge request', 'draft merge request', 'branch', 'child'])
def test_a_newer_commit_cancels_older_merge_request_and_branch_pipelines(variables):
    assert pipeline(GITLAB, variables)['auto_cancel'] == {'on_new_commit': 'interruptible'}


# ---- the select job, run for real on synthetic histories ---------------------

SELECT_INPUTS = ('scripts/__init__.py', 'scripts/review_tooling.py', 'scripts/gitlab_lanes.py',
                 'src/mainframe_rag/ingest/rules_version.py')
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'ci',
           'GIT_AUTHOR_EMAIL': 'ci@example.invalid', 'GIT_COMMITTER_NAME': 'ci',
           'GIT_COMMITTER_EMAIL': 'ci@example.invalid'}


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def python_on_path(tmp_path):
    """A `python` that is this interpreter, as the job image's python is."""
    bin_dir = tmp_path / 'pybin'
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / 'python').write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    (bin_dir / 'python').chmod(0o755)
    return bin_dir


def run_select(tmp_path, commits):
    """Push `main`, branch off it, commit `commits`, then run the real select job in a fresh clone."""
    seed = tmp_path / 'seed'
    seed.mkdir()
    git(seed, 'init', '-q', '-b', 'main')
    for name in (*SELECT_INPUTS, '.gitlab-ci.yml'):
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, seed / name)
    (seed / 'README.md').write_text('seed\n')
    git(seed, 'add', '.')
    git(seed, 'commit', '-q', '-m', 'seed')
    git(tmp_path, 'clone', '-q', '--bare', str(seed), 'origin.git')
    work = tmp_path / 'work'
    git(tmp_path, 'clone', '-q', '--branch', 'main', str(tmp_path / 'origin.git'), 'work')
    git(work, 'checkout', '-q', '-b', 'feature')
    for files in commits:
        for name, text in files.items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(text)
        git(work, 'add', '.')
        git(work, 'commit', '-q', '-m', 'change')
    # GitLab checks out the pipeline SHA detached, with only that ref fetched.
    git(work, 'checkout', '-q', '--detach')
    git(work, 'update-ref', '-d', 'refs/remotes/origin/main')
    git(work, 'branch', '-q', '-D', 'main')
    env = {**os.environ, **GIT_ENV, 'PATH': f'{python_on_path(tmp_path)}:{os.environ["PATH"]}',
           'CI_DEFAULT_BRANCH': 'main', 'CI_COMMIT_SHA': git(work, 'rev-parse', 'HEAD'),
           'PYTHONPATH': ''}
    env.pop('GITHUB_OUTPUT', None)
    proc = subprocess.run(['sh', '-e', '-c', '\n'.join(lines(GITLAB['select']))], cwd=work, env=env,
                          check=False, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    config = yaml.safe_load((work / 'lanes.yml').read_text())
    return pipeline(child_doc(GITLAB, config), CHILD), proc.stdout


@pytest.mark.parametrize('label, commits, expected, not_run', [
    ('prose only', [{'docs/notes.md': 'x\n'}], {'hygiene'}, set()),
    ('tests only', [{'tests/test_config.py': 'x = 1\n'}], {'hygiene', 'lint', 'test', 'hazards'}, set()),
    # The base is the merge base, not the previous push: a docs push after a
    # storage change (whose pipeline was auto-cancelled) still selects sim.
    ('storage then prose', [{'src/mainframe_rag/ingest/chunker.py': 'x = 1\n'}, {'README.md': 'y\n'}],
     REQUIRED_JOBS, {'ha', 'load'}),
    ('unmapped path fails closed', [{'unmapped/thing.bin': 'x\n'}], REQUIRED_JOBS, {'ha', 'load'}),
    ('GitLab pipeline definition', [{'.gitlab-ci.yml': '# changed\n', 'pyproject.toml': '\n'}],
     REQUIRED_JOBS, {'packaging'}),
    ('no change against the default branch', [], REQUIRED_JOBS, {'ha', 'load'}),
])
def test_select_job_child_pipeline_runs_exactly_the_classified_lanes(tmp_path, label, commits, expected,
                                                                     not_run):
    child, output = run_select(tmp_path, commits)
    assert child is not None and child['variables'] == {'CI_RUNNER_TAG': 'wip'}
    assert child['jobs'] == expected, output
    if not_run:  # lanes with no GitLab job are named, never counted as run
        reported = next(l for l in output.splitlines() if l.startswith('NOT RUN (no GitLab job): '))
        assert not_run <= set(reported.split(': ', 1)[1].split()), output


@pytest.mark.parametrize('manifest, expected', [
    ({'required_lanes': ['context_check', 'packaging'], 'changed_paths': ['charts/x.yaml']}, {'hygiene'}),
    ({'required_lanes': ['context_check', 'new_lane'], 'changed_paths': ['a.py']}, REQUIRED_JOBS),
    ({'required_lanes': [], 'changed_paths': ['scripts/check_junit_clean.py']}, REQUIRED_JOBS),
])
def test_generator_fails_closed_on_unknown_lanes_and_pipeline_inputs(manifest, expected):
    assert set(selected_jobs(manifest)[0]) == expected


@pytest.mark.parametrize('raw', ['not json', '[]', '{"required_lanes": "unit_tests", "changed_paths": []}',
                                 '{"changed_paths": []}'])
def test_generator_refuses_an_unreadable_manifest(tmp_path, raw):
    (tmp_path / 'manifest.json').write_text(raw)
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts/gitlab_lanes.py'), '--manifest',
                           str(tmp_path / 'manifest.json'), '--out', str(tmp_path / 'lanes.yml')],
                          check=False, capture_output=True, text=True)
    assert proc.returncode == 2 and 'cannot select lanes' in proc.stderr
    assert not (tmp_path / 'lanes.yml').exists()


# ---- unit shards: GitLab's parallel index reaches the shard plugin -----------

@pytest.mark.parametrize('index', range(1, UNIT_SHARDS + 1))
def test_each_parallel_job_runs_its_own_shard_of_four(tmp_path, index):
    proc, log = run_job(tmp_path, 'test', start='rm -f unit-junit.xml', JUNIT_BODY=JUNIT['clean'],
                        CI_NODE_INDEX=str(index), CI_NODE_TOTAL=str(UNIT_SHARDS))
    assert proc.returncode == 0, proc.stderr
    argv = shlex.split(next(l for l in log.splitlines() if l.startswith('pytest ')))
    assert [a for a in argv if a.startswith('--unit-')] == [
        f'--unit-shard={index}', '--unit-shards=4', '--unit-durations=tests/unit-durations.json']


def test_a_job_without_parallel_variables_cannot_pass_as_a_shard(tmp_path):
    # Without GitLab's parallel variables the shard options are empty and the
    # real plugin refuses them, so a job that lost `parallel` cannot pass as a shard.
    unset = UNIT_LINE.replace('$CI_NODE_INDEX', '').replace('$CI_NODE_TOTAL', '')
    proc = subprocess.run(['sh', '-e', '-c', unset.replace('--junitxml=unit-junit.xml', '--collect-only')],
                          cwd=ROOT, check=False, capture_output=True, text=True,
                          env={**os.environ, 'PATH': f'{python_on_path(tmp_path)}:{os.environ["PATH"]}'})
    assert proc.returncode == 4 and 'unit-shard' in proc.stderr, proc.stdout + proc.stderr


COLLECTOR = """import os


def pytest_collection_finish(session):
    with open(os.environ['COLLECTED_OUT'], 'w', encoding='utf-8') as handle:
        handle.write('\\n'.join(item.nodeid for item in session.items))
"""


def collect(tmp_path, command, label, **env):
    out = tmp_path / f'{label}.txt'
    clean = {k: v for k, v in os.environ.items()
             if k not in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'CI_NODE_INDEX',
                          'CI_NODE_TOTAL')}
    proc = subprocess.run(['sh', '-e', '-c', command], cwd=ROOT, check=False, capture_output=True, text=True,
                          env={**clean, 'PATH': f'{python_on_path(tmp_path)}:{os.environ["PATH"]}',
                               'PYTHONPATH': str(tmp_path), 'COLLECTED_OUT': str(out), **env})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return out.read_text(encoding='utf-8').splitlines()


def test_four_shards_are_a_disjoint_complete_partition_of_the_real_collection(tmp_path):
    from scripts.unit_evidence import select_nodes
    (tmp_path / 'unit_collector.py').write_text(COLLECTOR)
    collect_only = UNIT_LINE.replace('--junitxml=unit-junit.xml',
                                     '--collect-only -p no:cacheprovider -p unit_collector')
    assert collect_only != UNIT_LINE
    unsharded = re.sub(r' --unit-[a-z-]+=\S+', '', collect_only)
    full = collect(tmp_path, unsharded, 'full')
    assert len(full) == len(set(full)) > 100
    assert 'tests/test_ci_gitlab_parity.py::' + sys._getframe().f_code.co_name in full
    shards = [collect(tmp_path, collect_only, f'shard-{index}', CI_NODE_INDEX=str(index),
                      CI_NODE_TOTAL=str(UNIT_SHARDS)) for index in range(1, UNIT_SHARDS + 1)]
    assert all(shards)
    assert sum(len(shard) for shard in shards) == len(full)  # disjoint ...
    assert set().union(*map(set, shards)) == set(full)       # ... and complete
    durations = (ROOT / 'tests/unit-durations.json').read_text(encoding='utf-8')
    for index, shard in enumerate(shards, start=1):  # the layout GitHub's producer computes for 4
        assert sorted(shard) == select_nodes(full, index, UNIT_SHARDS, durations)


# ---- release pipelines from immutable tags and approved-registry images (#808) --

@pytest.mark.parametrize('env, passes', [
    ({}, True),                                                   # branch / MR pipeline
    ({'CI_COMMIT_TAG': 'v1.2.3', 'CI_COMMIT_REF_PROTECTED': 'true'}, True),
    ({'CI_COMMIT_TAG': 'v1.2.3', 'CI_COMMIT_REF_PROTECTED': 'false'}, False),
    ({'CI_COMMIT_TAG': 'v1.2.3'}, False),                         # protection unknown
])
def test_hygiene_refuses_a_tag_pipeline_unless_the_tag_is_protected(tmp_path, env, passes):
    base = {k: v for k, v in os.environ.items() if not k.startswith('CI_')}
    proc = subprocess.run(['sh', '-e', '-c', tag_guard(GITLAB)], cwd=tmp_path, check=False,
                          env={**base, **env}, capture_output=True, text=True)
    assert (proc.returncode == 0) is passes, proc.stderr
    if not passes:
        assert 'v1.2.3' in proc.stderr and 'protected' in proc.stderr


def test_job_and_service_images_come_only_from_the_operator_set_registry():
    references = image_references(GITLAB)
    assert references['default image'] == '$CI_PYTHON_IMAGE'
    assert references['CI_PYTHON_IMAGE'].startswith(KIT_REPOSITORY + '@sha256:')
    services = {k: v for k, v in references.items() if 'service' in k}
    assert set(services) == {'sim service 0', 'gate-l1 service 0'}
    for ref in services.values():
        assert ref.startswith('${CI_APPROVED_REGISTRY}/qdrant/qdrant@sha256:')
    text = (ROOT / '.gitlab-ci.yml').read_text()
    code = '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith('#'))
    assert 'CI_REGISTRY' not in code
    # The registry is an operator variable: no host, URL or credential in the file.
    assert not re.search(r'https?://(?!qdrant:6333)|\.(internal|local|corp)\b|glpat-', text)


def test_documented_quay_roles_match_the_image_paths_load_sh_pushes():
    load = (ROOT / 'scripts/airgap/load.sh').read_text()
    paths = set(re.findall(r'\$INTERNAL_REGISTRY/([\w./-]+)(?::|\b)', load))
    assert {'qdrant/qdrant', 'openobserve/openobserve', 'qdrant-pdf-rag-ingest',
            'qdrant-pdf-rag-agent', 'openshift4/ose-oauth-proxy'} <= paths
    deploy = (ROOT / 'docs/deploy.md').read_text()
    example = (ROOT / 'airgap.env.example').read_text()
    for path in sorted(paths):
        assert f'`{path}' in deploy, path
    assert 'CI_APPROVED_REGISTRY' in deploy and 'CI_APPROVED_REGISTRY' in example
    assert 'INTERNAL_REGISTRY' in deploy


# ---- the dev kit job image (ADR-0009 section 8, GH#1118 F5) ------------------

def test_the_default_job_image_is_exactly_the_kit_pin():
    pin = KIT_PIN.read_text()
    digest = re.search(r'(?m)^digest: (sha256:\S+)$', pin).group(1)
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', digest), 'the kit pin records no pushed digest yet'
    assert GITLAB['variables']['CI_PYTHON_IMAGE'] == f'$CI_APPROVED_REGISTRY/qdrant-pdf-rag-devkit@{digest}'
    assert kit_image_problem(GITLAB, pin) is None
    # A pin bump without the default, and a default bump without the pin, both fail;
    # so does a pin that records no pushed digest.
    assert kit_image_problem(GITLAB, pin.replace(digest, 'sha256:' + 'f' * 64))
    assert kit_image_problem(GITLAB, pin.replace(digest, 'sha256:PENDING'))
    other = mutated(lambda d: d['variables'].update(CI_PYTHON_IMAGE=f'{KIT_REPOSITORY}@sha256:{"e" * 64}'))
    assert kit_image_problem(other, pin)
    # The repository is the documented Quay role path that scripts/devkit/install.sh writes.
    assert '| `qdrant-pdf-rag-devkit:<kit key>` |' in (ROOT / 'docs/deploy.md').read_text()


def py_base_script(tmp_path, kit_present):
    """.py-base's kit step, with the image's lock directory relocated under tmp_path."""
    locks = tmp_path / 'opt/devkit/locks'
    if kit_present:
        locks.mkdir(parents=True)
    step = next(s for s in lines(GITLAB['.py-base'], 'before_script') if KIT_PYTHON in s)
    return step.replace('/opt/devkit/locks', str(locks))


@pytest.mark.parametrize('kit_present, prepare_status, passes', [
    (True, 0, True), (True, 2, False), (False, 0, False)])
def test_python_jobs_run_only_on_the_kit_environment(tmp_path, kit_present, prepare_status, passes):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'python3').write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$STUB_LOG"\nexit "$PREPARE_STATUS"\n')
    (bin_dir / 'python3').chmod(0o755)
    log = tmp_path / 'log'
    log.write_text('')
    env = {**{k: v for k, v in os.environ.items() if not k.startswith(('CI_', 'PIP_'))},
           'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'STUB_LOG': str(log), 'CI_PROJECT_DIR': str(tmp_path),
           'PREPARE_STATUS': str(prepare_status)}
    proc = subprocess.run(['sh', '-e', '-c', py_base_script(tmp_path, kit_present)], cwd=tmp_path, check=False,
                          env=env, capture_output=True, text=True)
    assert (proc.returncode == 0) is passes, proc.stderr
    if kit_present:
        assert log.read_text() == f'scripts/prepare_python.py --kit --venv {tmp_path}/.venv\n'
    else:
        # Any other image fails before Python runs, with a fixed message naming the fix.
        assert log.read_text() == ''
        assert 'not the dev kit image' in proc.stderr and 'CI_PYTHON_IMAGE unset' in proc.stderr


@pytest.mark.parametrize('value, passes', [(None, True), ('', True), ('/runner/helm archive.tgz', False)])
def test_hygiene_refuses_the_retired_helm_archive_variable(tmp_path, value, passes):
    step = next(s for s in lines(GITLAB['hygiene']) if 'CI_HELM_ARCHIVE' in s)
    env = {k: v for k, v in os.environ.items() if not k.startswith('CI_')}
    if value is not None:
        env['CI_HELM_ARCHIVE'] = value
    proc = subprocess.run(['sh', '-e', '-c', step], cwd=tmp_path, check=False, env=env,
                          capture_output=True, text=True)
    assert (proc.returncode == 0) is passes, proc.stderr
    if not passes:
        # A fixed message naming the replacement; the value itself is never echoed.
        assert 'CI_HELM_ARCHIVE is retired' in proc.stderr and 'link-tools.sh' in proc.stderr
        assert value not in proc.stderr
