"""Gap pipeline reader scripts/gap/ci_status.py (GH#1118 F5, ADR-0009 section 2).

The script runs as a real subprocess against a loopback stub of the GitLab
API v4 endpoints it reads (projects, pipelines, jobs, bridges, merge requests,
job artifacts), serving canned JSON and real zip archives. Expected verdicts
come from the pipeline shapes in .gitlab-ci.yml, not from the script.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from scripts.gap import ci_status

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'scripts' / 'gap' / 'ci_status.py'
SHA = '0123456789abcdef0123456789abcdef01234567'
CANARY = 'glpat-CANARY-1118-never-print-me'
DATA_CANARY = 'MANUAL-TEXT-CANARY-from-a-failure-message'
API = '/api/v4/projects/7'


class Stub:
    """Canned GitLab API: path -> responses; the last response repeats."""

    def __init__(self) -> None:
        self.routes: dict[str, list[tuple[int, Any]]] = {}
        self.requests: list[tuple[str, dict[str, str], dict[str, str]]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path, _, query = self.path.partition('?')
                stub.requests.append((path, dict(urllib.parse.parse_qsl(query)),
                                      {k.lower(): v for k, v in self.headers.items()}))
                queue = stub.routes.get(path)
                if not queue:
                    status, body = 404, {'message': '404 Not Found'}
                else:
                    status, body = queue.pop(0) if len(queue) > 1 else queue[0]
                if status in (301, 302):
                    self.send_response(status)
                    self.send_header('Location', body)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def add(self, path: str, *responses: tuple[int, Any]) -> None:
        self.routes[path] = list(responses)

    def ok(self, path: str, *bodies: Any) -> None:
        self.add(path, *[(200, body) for body in bodies])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    server = Stub()
    server.ok('/api/v4/projects/group%2Fproj', {'id': 7, 'path_with_namespace': 'group/proj'})
    server.ok(f'{API}/repository/commits/{SHA}/merge_requests', [])
    yield server
    server.close()


@pytest.fixture
def token(tmp_path: Path) -> Path:
    path = tmp_path / 'token'
    path.write_text(CANARY + '\n')
    return path


def run(stub: Stub, token: Path, *args: str, cwd: Path | None = None,
        sha: bool = True) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items()
           if not k.lower().endswith('_proxy') and k not in (
               'CI_SERVER_URL', 'MFR_GITLAB_URL', 'CI_PROJECT_PATH', 'MFR_GITLAB_PROJECT')}
    command = [sys.executable, str(SCRIPT), '--gitlab-url', stub.url, '--project', 'group/proj',
               '--token-file', str(token), *(['--sha', SHA] if sha else []), *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False,
                            env=env, cwd=cwd or REPO)
    output = result.stdout + result.stderr
    assert CANARY not in output
    assert DATA_CANARY not in output
    assert 'Traceback' not in output
    return result


def job(job_id: int, name: str, stage: str = 'test', status: str = 'success', *,
        allow_failure: bool = False, junit: bool = False) -> dict:
    artifacts = [{'file_type': 'archive'}] + ([{'file_type': 'junit'}] if junit else [])
    return {'id': job_id, 'name': name, 'stage': stage, 'status': status, 'duration': 42.4,
            'allow_failure': allow_failure, 'artifacts': artifacts}


def gate_jobs(**status: str) -> list[dict]:
    names = [('hygiene', 'hygiene'), ('lint', 'hygiene'), ('test 1/4', 'test'),
             ('test 2/4', 'test'), ('test 3/4', 'test'), ('test 4/4', 'test'),
             ('hazards', 'test'), ('sim', 'test'), ('gate-l1', 'test')]
    return [job(1000 + i, name, stage, status.get(name, 'success'), junit=name.startswith('test'))
            for i, (name, stage) in enumerate(names)]


def pipeline(pid: int, source: str, status: str = 'success', **extra: Any) -> dict:
    ref = 'refs/merge-requests/4/head' if source == 'merge_request_event' else 'feat/x'
    return {'id': pid, 'sha': SHA, 'ref': ref, 'source': source, 'status': status,
            'web_url': f'https://gitlab.invalid/group/proj/-/pipelines/{pid}', **extra}


def junit_zip(*cases: str) -> bytes:
    xml = ('<?xml version="1.0"?><testsuites><testsuite name="pytest">'
           + ''.join(cases) + '</testsuite></testsuites>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('unit-junit.xml', xml)
    return buffer.getvalue()


def mr_pipeline(stub: Stub, jobs: list[dict], status: str = 'success') -> None:
    # An older branch pipeline of the same commit must lose to the MR pipeline.
    stub.ok(f'{API}/pipelines', [pipeline(100, 'merge_request_event', status),
                                 pipeline(99, 'push', 'failed')])
    stub.ok(f'{API}/pipelines/100', pipeline(100, 'merge_request_event', status))
    stub.ok(f'{API}/pipelines/100/jobs', jobs)
    stub.ok(f'{API}/pipelines/100/bridges', [])
    stub.ok(f'{API}/repository/commits/{SHA}/merge_requests',
            [{'iid': 4, 'state': 'opened', 'sha': SHA, 'project_id': 7}])


def branch_pipeline(stub: Stub, child_jobs: list[dict], *, status: str = 'success',
                    lanes: tuple[str, ...] = ('hygiene', 'lint', 'test'),
                    required: tuple[str, ...] = ('context_check', 'lint_and_types',
                                                 'unit_tests', 'packaging')) -> None:
    stub.ok(f'{API}/pipelines', [pipeline(200, 'push', status)])
    stub.ok(f'{API}/pipelines/200', pipeline(200, 'push', status))
    stub.ok(f'{API}/pipelines/200/jobs', [job(2001, 'select', 'hygiene')])
    stub.ok(f'{API}/pipelines/200/bridges', [{
        'id': 2002, 'name': 'lanes', 'stage': 'test', 'status': status, 'duration': 600.0,
        'allow_failure': False, 'downstream_pipeline': {'id': 201, 'project_id': 7,
                                                        'status': status}}])
    stub.ok(f'{API}/pipelines/201', pipeline(201, 'parent_pipeline', status))
    stub.ok(f'{API}/pipelines/201/jobs', child_jobs)
    stub.ok(f'{API}/pipelines/201/bridges', [])
    config: dict[str, Any] = {'include': [{'local': '.gitlab-ci.yml'}]}
    config.update({name: {'rules': [{'if': '$CI_PIPELINE_SOURCE == "parent_pipeline"'}]}
                   for name in lanes})
    stub.ok(f'{API}/jobs/2001/artifacts/lanes.yml', config)
    stub.ok(f'{API}/jobs/2001/artifacts/lanes-manifest.json',
            {'required_lanes': list(required), 'changed_paths': ['src/x.py'], 'profile': 'code'})


def child_unit_jobs(**status: str) -> list[dict]:
    return [job(2010, 'hygiene', 'hygiene', status.get('hygiene', 'success')),
            job(2011, 'lint', 'hygiene', status.get('lint', 'success'))] + [
        job(2012 + k, f'test {k + 1}/4', 'test', status.get(f'test {k + 1}/4', 'success'),
            junit=True) for k in range(4)]


def test_merge_request_pipeline_passes(stub: Stub, token: Path) -> None:
    mr_pipeline(stub, gate_jobs())
    result = run(stub, token)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'using merge request pipeline 100: the newest pipeline of open merge request !4' \
        in result.stdout
    assert 'VERDICT: PASSED' in result.stdout
    for name in ('hygiene', 'lint', 'test 4/4', 'sim', 'gate-l1'):
        assert name in result.stdout
    assert '42s' in result.stdout
    # The token went to GitLab in the header and nowhere else.
    assert {h.get('private-token') for _, _, h in stub.requests} == {CANARY}
    assert stub.requests[1][1]['sha'] == SHA


def test_branch_pipeline_follows_bridge_and_lists_failing_tests(stub: Stub, token: Path) -> None:
    branch_pipeline(stub, child_unit_jobs(**{'test 2/4': 'failed'}), status='failed')
    message = f'<failure message="{DATA_CANARY}">{DATA_CANARY}</failure>'
    stub.add(f'{API}/jobs/2013/artifacts', (302, '/storage/artifacts-2013.zip'))
    stub.ok('/storage/artifacts-2013.zip', junit_zip(
        '<testcase classname="tests.test_alpha.TestBeta" name="test_gamma[x-1]">'
        f'{message}</testcase>',
        f'<testcase classname="tests.test_delta" name="test_eps"><error>{DATA_CANARY}</error>'
        '</testcase>',
        '<testcase classname="tests.test_delta" name="test_skip"><skipped/></testcase>',
        '<testcase classname="tests.test_delta" name="test_ok"/>'))
    result = run(stub, token)
    assert result.returncode == 1, result.stdout + result.stderr
    out = result.stdout
    assert 'using branch pipeline 200' in out
    assert 'lanes > test 2/4' in out and 'lanes > test 1/4' in out
    assert 'FAILED  tests/test_alpha.py::TestBeta::test_gamma[x-1]' in out
    assert 'FAILED  tests/test_delta.py::test_eps' in out
    assert 'SKIPPED tests/test_delta.py::test_skip' in out
    assert 'test_ok' not in out
    assert 'NOT RUN (no GitLab job): packaging' in out
    assert 'VERDICT: FAILED' in out
    storage = [h for p, _, h in stub.requests if p.startswith('/storage/')]
    assert storage and all('private-token' not in h for h in storage)


def test_unreadable_junit_archive_is_a_fixed_note(stub: Stub, token: Path) -> None:
    branch_pipeline(stub, child_unit_jobs(**{'test 2/4': 'failed'}), status='failed')
    data = bytearray(junit_zip('<testcase classname="tests.test_a" name="t"><failure/></testcase>'))
    central = data.index(b'PK\x01\x02')
    data[central + 10:central + 12] = (99).to_bytes(2, 'little')  # unsupported compression
    stub.ok(f'{API}/jobs/2013/artifacts', bytes(data))
    result = run(stub, token)
    assert result.returncode == 1, result.stdout + result.stderr
    assert '(JUnit report unreadable)' in result.stdout
    assert 'unit-junit.xml' not in result.stdout + result.stderr


def test_failing_ids_are_bounded(stub: Stub, token: Path) -> None:
    branch_pipeline(stub, child_unit_jobs(**{'test 1/4': 'failed'}), status='failed')
    stub.ok(f'{API}/jobs/2012/artifacts', junit_zip(*[
        f'<testcase classname="tests.test_many" name="test_{i}"><failure/></testcase>'
        for i in range(30)]))
    result = run(stub, token, '--max-tests', '5')
    assert result.returncode == 1
    assert result.stdout.count('FAILED  tests/test_many.py::') == 5
    assert '... 25 more' in result.stdout


def test_branch_pipeline_passes_and_reports_not_run_lanes(stub: Stub, token: Path) -> None:
    branch_pipeline(stub, child_unit_jobs())
    result = run(stub, token, '--json')
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['verdict'] == 'passed' and report['exit_code'] == 0
    assert report['not_run'] == ['packaging']
    labels = {j['label']: j['status'] for j in report['jobs']}
    assert labels['lanes > test 3/4'] == 'success'
    assert 'packaging' not in json.dumps(report['jobs'])


def test_selected_lane_missing_from_child_fails(stub: Stub, token: Path) -> None:
    jobs = [j for j in child_unit_jobs() if j['name'] != 'test 3/4']
    branch_pipeline(stub, jobs)
    result = run(stub, token)
    assert result.returncode == 1
    assert 'job test shard(s) 3 of 4 did not run' in result.stdout


def test_manifest_lane_missing_from_lanes_yml_fails(stub: Stub, token: Path) -> None:
    # The select job's lanes.yml and manifest disagree: hazards was required.
    branch_pipeline(stub, child_unit_jobs(), required=('context_check', 'lint_and_types',
                                                       'unit_tests', 'hazards'))
    result = run(stub, token)
    assert result.returncode == 1, result.stdout
    assert 'expected job hazards did not run' in result.stdout


def test_unreadable_lane_selection_fails_closed(stub: Stub, token: Path) -> None:
    branch_pipeline(stub, child_unit_jobs())
    stub.add(f'{API}/jobs/2001/artifacts/lanes.yml', (404, {'message': 'expired'}))
    result = run(stub, token)
    assert result.returncode == 1
    assert 'cannot read the lane selection of job select (HTTP 404)' in result.stdout


def test_empty_child_pipeline_is_never_success(stub: Stub, token: Path) -> None:
    branch_pipeline(stub, [], lanes=())
    result = run(stub, token)
    assert result.returncode == 1, result.stdout
    assert 'child pipeline 201 (via lanes) has no jobs' in result.stdout
    assert 'VERDICT: FAILED' in result.stdout


def test_bridge_without_child_pipeline_fails(stub: Stub, token: Path) -> None:
    branch_pipeline(stub, child_unit_jobs())
    stub.ok(f'{API}/pipelines/200/bridges', [{
        'id': 2002, 'name': 'lanes', 'stage': 'test', 'status': 'success',
        'downstream_pipeline': None}])
    result = run(stub, token)
    assert result.returncode == 1
    assert 'the bridge started no child pipeline' in result.stdout


@pytest.mark.parametrize('status', ['failed', 'success'])
def test_allow_failure_is_a_failure(stub: Stub, token: Path, status: str) -> None:
    jobs = gate_jobs(sim=status)
    jobs[7]['allow_failure'] = True
    # GitLab marks a pipeline whose only failure is allowed as success.
    mr_pipeline(stub, jobs, 'success')
    result = run(stub, token)
    assert result.returncode == 1
    assert 'sim: allow_failure is set' in result.stdout


def test_merge_request_pipeline_missing_lane_fails(stub: Stub, token: Path) -> None:
    mr_pipeline(stub, [j for j in gate_jobs() if j['name'] != 'gate-l1'])
    result = run(stub, token)
    assert result.returncode == 1
    assert 'expected job gate-l1 did not run' in result.stdout


def test_closed_merge_request_pipeline_does_not_hide_newer_branch_pipeline(
        stub: Stub, token: Path) -> None:
    stub.ok(f'{API}/pipelines', [pipeline(300, 'push', 'failed'),
                                 pipeline(100, 'merge_request_event')])
    stub.ok(f'{API}/repository/commits/{SHA}/merge_requests',
            [{'iid': 4, 'state': 'closed', 'sha': SHA, 'project_id': 7}])
    stub.ok(f'{API}/pipelines/300', pipeline(300, 'push', 'failed'))
    stub.ok(f'{API}/pipelines/300/jobs', [job(3001, 'select', 'hygiene', 'failed')])
    stub.ok(f'{API}/pipelines/300/bridges', [])
    result = run(stub, token)
    assert result.returncode == 1, result.stdout
    assert 'using branch pipeline 300' in result.stdout


def test_tag_pipeline_needs_every_lane(stub: Stub, token: Path) -> None:
    tag = pipeline(400, 'push', tag=True, ref='v1.0.0')
    stub.ok(f'{API}/pipelines', [tag])
    stub.ok(f'{API}/pipelines/400', tag)
    stub.ok(f'{API}/pipelines/400/jobs', gate_jobs())
    stub.ok(f'{API}/pipelines/400/bridges', [])
    result = run(stub, token)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'using tag pipeline 400' in result.stdout
    stub.ok(f'{API}/pipelines/400/jobs', [j for j in gate_jobs() if j['name'] != 'hazards'])
    assert run(stub, token).returncode == 1


def test_merged_results_pipeline_found_through_merge_request(stub: Stub, token: Path) -> None:
    stub.ok(f'{API}/pipelines', [])
    stub.ok(f'{API}/repository/commits/{SHA}/merge_requests',
            [{'iid': 4, 'state': 'opened', 'sha': SHA, 'project_id': 7}])
    stub.ok(f'{API}/merge_requests/4', {'iid': 4, 'head_pipeline': {'id': 300, 'project_id': 7}})
    stub.ok(f'{API}/pipelines/300', {**pipeline(300, 'merge_request_event'), 'sha': 'f' * 40})
    stub.ok(f'{API}/pipelines/300/jobs', gate_jobs())
    stub.ok(f'{API}/pipelines/300/bridges', [])
    result = run(stub, token)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'head pipeline of open merge request !4' in result.stdout


def test_pending_then_success_with_wait(stub: Stub, token: Path) -> None:
    mr_pipeline(stub, gate_jobs())
    stub.ok(f'{API}/pipelines/100', pipeline(100, 'merge_request_event', 'running'),
            pipeline(100, 'merge_request_event', 'success'))
    stub.ok(f'{API}/pipelines/100/jobs', gate_jobs(sim='running'), gate_jobs())
    result = run(stub, token, '--wait', '--interval', '1', '--timeout', '30')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'waiting: pipeline 100 running, 8/9 jobs finished' in result.stderr
    assert 'VERDICT: PASSED' in result.stdout


def test_timeout_exits_pending(stub: Stub, token: Path) -> None:
    mr_pipeline(stub, gate_jobs(sim='running'), 'running')
    waited = run(stub, token, '--wait', '--interval', '1', '--timeout', '1')
    assert waited.returncode == 2
    assert 'still running at the end of --timeout' in waited.stdout
    assert 'VERDICT: PENDING' in waited.stdout
    once = run(stub, token)
    assert once.returncode == 2
    assert 'pass --wait' in once.stdout


def test_no_pipeline_yet_is_pending(stub: Stub, token: Path) -> None:
    stub.ok(f'{API}/pipelines', [])
    stub.add(f'{API}/repository/commits/{SHA}/merge_requests', (404, {'message': 'no commit'}))
    result = run(stub, token, '--json')
    assert result.returncode == 2
    assert json.loads(result.stdout)['problems'] == ['no pipeline for this commit yet']


@pytest.mark.parametrize(('status', 'message'), [
    (401, 'GitLab refused the token (HTTP 401): check the token file and its expiry'),
    (403, 'GitLab denied access (HTTP 403): the token needs read_api on this project'),
    (404, 'GitLab returned HTTP 404: the project, commit or pipeline is not visible'),
    (500, 'GitLab API returned HTTP 500'),
])
def test_http_errors_have_fixed_messages(stub: Stub, token: Path, status: int,
                                         message: str) -> None:
    stub.add('/api/v4/projects/group%2Fproj', (status, {'message': f'{CANARY} {DATA_CANARY}'}))
    result = run(stub, token)
    assert result.returncode == 3
    assert result.stdout == ''
    assert result.stderr.startswith('ci_status: ' + message)


def test_artifact_auth_failure_is_an_error(stub: Stub, token: Path) -> None:
    branch_pipeline(stub, child_unit_jobs(**{'test 2/4': 'failed'}), status='failed')
    stub.add(f'{API}/jobs/2013/artifacts', (401, {'message': CANARY}))
    result = run(stub, token, '--json')
    assert result.returncode == 3
    assert json.loads(result.stdout) == {
        'verdict': 'error', 'exit_code': 3,
        'message': 'GitLab refused the token (HTTP 401): check the token file and its expiry'}


def test_network_error_is_fixed(token: Path, tmp_path: Path) -> None:
    server = Stub()
    url = server.url
    server.close()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--gitlab-url', url, '--project', 'g/p',
         '--token-file', str(token), '--sha', SHA], capture_output=True, text=True,
        timeout=60, check=False, env={'PATH': os.environ['PATH']})
    assert result.returncode == 3
    assert result.stderr == 'ci_status: network error: GitLab did not answer\n'


@pytest.mark.parametrize(('args', 'message'), [
    (['--gitlab-url', 'http://gitlab.invalid'], 'must use https'),
    (['--gitlab-url', 'https://user:pw@gitlab.invalid'], 'must not carry credentials'),
    (['--sha', 'abc123'], 'full 40-hex sha'),
    (['--timeout', '0'], 'invalid arguments'),
    (['--token-file', '/nonexistent/token'], 'cannot read the token file'),
])
def test_usage_errors_exit_3_without_contacting_gitlab(stub: Stub, token: Path,
                                                       args: list[str], message: str) -> None:
    result = run(stub, token, *args)
    assert result.returncode == 3
    assert message in result.stderr
    assert stub.requests == []


def test_malformed_token_file_is_refused(stub: Stub, tmp_path: Path) -> None:
    empty = tmp_path / 'empty'
    empty.write_text('\n')
    result = run(stub, empty)
    assert result.returncode == 3
    assert 'the token file is empty or malformed' in result.stderr
    assert stub.requests == []


def test_default_sha_is_checkout_head(stub: Stub, token: Path, tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@invalid',
           '-c', 'commit.gpgsign=false']
    subprocess.run([*git, 'init', '-q'], check=True)
    subprocess.run([*git, 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)
    head = subprocess.run([*git, 'rev-parse', 'HEAD'], check=True, capture_output=True,
                          text=True).stdout.strip()
    stub.ok(f'{API}/pipelines', [])
    stub.ok(f'{API}/repository/commits/{head}/merge_requests', [])
    result = run(stub, token, cwd=repo, sha=False)
    assert result.returncode == 2
    assert f'commit {head}' in result.stdout
    assert [q['sha'] for p, q, _ in stub.requests if p == f'{API}/pipelines'] == [head]


@pytest.mark.parametrize(('attrs', 'expected'), [
    ({'classname': 'tests.test_a.TestB', 'name': 't[1]'}, 'tests/test_a.py::TestB::t[1]'),
    ({'classname': 'tests.test_a.TestB.TestC', 'name': 't'}, 'tests/test_a.py::TestB::TestC::t'),
    ({'classname': 'tests.test_a', 'name': 't'}, 'tests/test_a.py::t'),
    ({'classname': 'x.TestB', 'name': 't', 'file': 'tests/x.py'}, 'tests/x.py::TestB::t'),
])
def test_test_id_from_junit_case(attrs: dict[str, str], expected: str) -> None:
    assert ci_status.test_id(ET.Element('testcase', attrs)) == expected
