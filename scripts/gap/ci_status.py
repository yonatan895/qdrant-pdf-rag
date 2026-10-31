#!/usr/bin/env python3
"""Report, or wait on, a commit's GitLab pipeline (GH#1118 F5, ADR-0009 section 2).

The gap agents' `gh run watch`: find the commit's merge request pipeline, else
its branch or tag pipeline, follow a branch pipeline's `lanes` bridge into the
child pipeline, print every job's name, stage, status and duration, and for a
failed test job the failing test ids from its JUnit report. Usage:
docs/task-runner.md#gap-ci-status.

Fail closed, like the pipeline itself: the verdict is PASSED only when every
job and every pipeline succeeded, no job sets allow_failure, no pipeline is
empty and every lane the pipeline must run ran. Lanes the branch selector
chose that have no GitLab job are reported as NOT RUN, never as passed: when
everything that ran passed, the verdict is PASSED-UNVERIFIED (exit 4). While a
merge request is open on the commit, only its merge request pipeline answers.

Standard library only; the token is read from a file (never argv) and never
printed. Errors print fixed messages and HTTP status codes, never exception or
response text, and no job log is read. Exit codes: 0 passed (every required
lane ran and passed), 1 failed, 2 pending (at --timeout with --wait, or no
pipeline yet), 3 usage, network or auth error, 4 passed-unverified (every job
that ran passed; selected lanes have no GitLab job).
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))  # direct script entry imports the lane-to-job owner

from scripts.gitlab_lanes import ALWAYS, LANE_JOBS, selected_jobs

EXIT_PASSED, EXIT_FAILED, EXIT_PENDING, EXIT_ERROR, EXIT_UNVERIFIED = 0, 1, 2, 3, 4
# The dev pod mounts Secret mfr-dev-gitlab here (deploy/workspace/ci-namespace.template.yaml).
TOKEN_FILE = '/etc/mfr/dev-gitlab/token'
URL_ENV = ('CI_SERVER_URL', 'MFR_GITLAB_URL')
PROJECT_ENV = ('CI_PROJECT_PATH', 'MFR_GITLAB_PROJECT')
# Job and bridge names in .gitlab-ci.yml.
SELECT_JOB = 'select'
LANES_BRIDGE = 'lanes'
GATE_JOBS = frozenset(LANE_JOBS.values()) | frozenset(ALWAYS)
TERMINAL = frozenset({'success', 'failed', 'canceled', 'skipped', 'manual'})
LOOPBACK = frozenset({'localhost', '127.0.0.1', '::1'})
SHA_RE = re.compile(r'[0-9a-f]{40}')
TOKEN_RE = re.compile(r'[\x21-\x7e]{8,1024}')
SHARD_RE = re.compile(r'(.+) (\d+)/(\d+)')
REQUEST_TIMEOUT = 30
MAX_PAGES = 20
MAX_DEPTH = 3
JSON_LIMIT = 16 << 20
ARCHIVE_LIMIT = 64 << 20
REPORT_LIMIT = 32 << 20


class GitLabError(Exception):
    """A fixed operator message; exit 3."""


class HttpStatus(GitLabError):
    def __init__(self, code: int) -> None:
        self.code = code
        known = {
            401: 'GitLab refused the token (HTTP 401): check the token file and its expiry',
            403: 'GitLab denied access (HTTP 403): the token needs read_api on this project',
            404: 'GitLab returned HTTP 404: the project, commit or pipeline is not visible '
                 'to this token',
            429: 'GitLab rate limit (HTTP 429): retry later',
        }
        super().__init__(known.get(code, f'GitLab API returned HTTP {code}'))


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print('ci_status: invalid arguments (see --help)', file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip('/') + '/api/v4'
        self.token = token

    def read(self, path: str, params: dict[str, str] | None = None,
             limit: int = JSON_LIMIT) -> tuple[bytes, str]:
        url = self.base + path + ('?' + urllib.parse.urlencode(params) if params else '')
        request = urllib.request.Request(url)
        # Unredirected: an artifact redirect to object storage never carries the token.
        request.add_unredirected_header('PRIVATE-TOKEN', self.token)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                data = response.read(limit + 1)
                next_page = response.headers.get('X-Next-Page') or ''
        except urllib.error.HTTPError as error:
            error.close()
            raise HttpStatus(error.code) from None
        except (OSError, http.client.HTTPException, ValueError):
            raise GitLabError('network error: GitLab did not answer') from None
        if len(data) > limit:
            raise GitLabError('GitLab response exceeds the size limit')
        return data, next_page

    def json(self, path: str, params: dict[str, str] | None = None) -> Any:
        data, _ = self.read(path, params)
        try:
            return json.loads(data)
        except ValueError:
            raise GitLabError('unexpected response from GitLab (not JSON)') from None

    def pages(self, path: str, params: dict[str, str] | None = None) -> list[dict]:
        items: list[dict] = []
        page = '1'
        for _ in range(MAX_PAGES):
            data, page = self.read(path, {**(params or {}), 'per_page': '100', 'page': page})
            try:
                chunk = json.loads(data)
            except ValueError:
                raise GitLabError('unexpected response from GitLab (not JSON)') from None
            if not isinstance(chunk, list) or not all(isinstance(i, dict) for i in chunk):
                raise GitLabError('unexpected response from GitLab (not a list)')
            items.extend(chunk)
            if not page:
                return items
        raise GitLabError('GitLab listing exceeds the page limit')


@dataclass
class Row:
    via: str
    pipeline: int
    project: int
    job_id: int
    name: str
    stage: str
    status: str
    duration: float | None
    allow_failure: bool
    bridge: bool
    junit: bool
    tests: list[str] = field(default_factory=list)
    more: int = 0
    report: str = ''

    @property
    def label(self) -> str:
        return f'{self.via} > {self.name}' if self.via else self.name


@dataclass
class Snapshot:
    rows: list[Row] = field(default_factory=list)
    # (label, pipeline id, status, job and bridge count)
    pipelines: list[tuple[str, int, str, int]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    pending: bool = False


def clean(text: object, limit: int = 200) -> str:
    """Printable, bounded text from a GitLab field (names, ids; never logs)."""
    return ''.join(c if c.isprintable() else '?' for c in str(text))[:limit]


def kind_of(pipeline: dict) -> str:
    if pipeline.get('source') == 'merge_request_event':
        return 'merge request'
    if pipeline.get('tag'):
        return 'tag'
    return 'branch'


def mr_pipeline_for(api: Api, pipeline: dict, owner: int, sha: str) -> bool:
    """True when an MR pipeline ran on `sha`, or on a merged results commit whose parent it is."""
    if pipeline.get('source') != 'merge_request_event':
        return False
    if pipeline.get('sha') == sha:
        return True
    commit = api.json(f'/projects/{owner}/repository/commits/{clean(pipeline.get("sha"), 40)}')
    parents = commit.get('parent_ids') if isinstance(commit, dict) else None
    return isinstance(parents, list) and sha in parents


def find_pipeline(api: Api, project: int, sha: str) -> tuple[int, dict, str] | str | None:
    """(project id, pipeline, how it was chosen); a str is why it must still wait.

    While a merge request containing this commit is open, only a merge request
    pipeline answers: if GitLab has not created one for this commit yet, wait,
    never fall back to the branch pipeline (it ran only the selected lanes;
    the merge request pipeline runs every lane, GH#808). With no open merge
    request, the commit's newest pipeline answers, so a closed merge request's
    older pipeline never hides a newer branch pipeline.
    """
    found = [p for p in api.pages(f'/projects/{project}/pipelines',
                                  {'sha': sha, 'order_by': 'id', 'sort': 'desc'})
             if p.get('source') != 'parent_pipeline']
    try:
        requests = api.pages(f'/projects/{project}/repository/commits/{sha}/merge_requests')
    except HttpStatus as error:
        if error.code != 404:  # 404: GitLab has not received the commit yet
            raise
        requests = []
    open_requests = sorted((m for m in requests if m.get('state') == 'opened'),
                           key=lambda m: -int(m.get('iid', 0)))
    for request in open_requests:
        iid = int(request['iid'])
        mine = [p for p in found if p.get('source') == 'merge_request_event'
                and str(p.get('ref', '')).startswith(f'refs/merge-requests/{iid}/')]
        if mine:
            newest = max(mine, key=lambda p: int(p['id']))
            return project, api.json(f'/projects/{project}/pipelines/{int(newest["id"])}'), \
                f'the newest pipeline of open merge request !{iid}'
        if request.get('sha') != sha:
            continue
        # A merged results pipeline runs on a merge commit, so the sha filter misses it.
        target = int(request.get('project_id', project))
        detail = api.json(f'/projects/{target}/merge_requests/{iid}')
        head = detail.get('head_pipeline') if isinstance(detail, dict) else None
        if isinstance(head, dict) and head.get('id'):
            owner = int(head.get('project_id', target))
            pipeline = api.json(f'/projects/{owner}/pipelines/{int(head["id"])}')
            if mr_pipeline_for(api, pipeline, owner, sha):
                return owner, pipeline, (f'the head pipeline of open merge request !{iid}'
                                         f' (on commit {clean(pipeline.get("sha"), 40)})')
    if open_requests:
        listed = ', '.join(f'!{int(m["iid"])}' for m in open_requests)
        return (f'MR pipeline not created yet: open merge request {listed} has no merge '
                'request pipeline for this commit; the branch pipeline is not the merge gate')
    if found:
        newest = max(found, key=lambda p: int(p['id']))
        pipeline = api.json(f'/projects/{project}/pipelines/{int(newest["id"])}')
        return project, pipeline, f'the newest {kind_of(pipeline)} pipeline of this commit'
    return None


def inspect(api: Api, project: int, pipeline: dict, via: str, depth: int,
            snap: Snapshot) -> None:
    pid = int(pipeline['id'])
    status = str(pipeline.get('status'))
    if status not in TERMINAL:
        snap.pending = True
    jobs = api.pages(f'/projects/{project}/pipelines/{pid}/jobs')
    bridges = api.pages(f'/projects/{project}/pipelines/{pid}/bridges')
    label = f'child pipeline {pid} (via {via})' if via else f'pipeline {pid}'
    snap.pipelines.append((label, pid, status, len(jobs) + len(bridges)))
    for job, bridge in [(j, False) for j in jobs] + [(b, True) for b in bridges]:
        row = Row(
            via=via, pipeline=pid, project=project, job_id=int(job['id']),
            name=clean(job.get('name')), stage=clean(job.get('stage')),
            status=clean(job.get('status'), 40),
            duration=job['duration'] if isinstance(job.get('duration'), (int, float)) else None,
            allow_failure=bool(job.get('allow_failure')), bridge=bridge,
            junit=any(isinstance(a, dict) and a.get('file_type') == 'junit'
                      for a in job.get('artifacts') or []))
        snap.rows.append(row)
        if row.status not in TERMINAL:
            snap.pending = True
        if not bridge:
            continue
        down = job.get('downstream_pipeline')
        if isinstance(down, dict) and down.get('id'):
            if depth >= MAX_DEPTH:
                snap.problems.append(f'{row.label}: child pipelines nest deeper than {MAX_DEPTH}')
                continue
            child_project = int(down.get('project_id', project))
            child = api.json(f'/projects/{child_project}/pipelines/{int(down["id"])}')
            inspect(api, child_project, child, row.label, depth + 1, snap)
        elif row.status in TERMINAL:
            snap.problems.append(f'{row.label}: the bridge started no child pipeline')
        else:
            snap.pending = True


def missing_jobs(expected: set[str], names: list[str], where: str) -> list[str]:
    """Expected jobs absent from `names`; a parallel job needs every shard k/N."""
    plain: set[str] = set()
    shards: dict[str, dict[int, set[int]]] = {}
    for name in names:
        match = SHARD_RE.fullmatch(name)
        if match:
            shards.setdefault(match[1], {}).setdefault(int(match[3]), set()).add(int(match[2]))
        else:
            plain.add(name)
    problems = []
    for job in sorted(expected):
        totals = shards.get(job, {})
        if job in plain and not totals:
            continue
        if not totals:
            problems.append(f'{where}: expected job {job} did not run')
        elif len(totals) != 1 or job in plain:
            problems.append(f'{where}: job {job} has inconsistent shards')
        else:
            [(total, seen)] = totals.items()
            absent = sorted(set(range(1, total + 1)) - seen)
            if absent:
                problems.append(f'{where}: job {job} shard(s) '
                                f'{", ".join(map(str, absent))} of {total} did not run')
    return problems


def artifact_json(api: Api, project: int, job_id: int, name: str) -> dict:
    data, _ = api.read(f'/projects/{project}/jobs/{job_id}/artifacts/{name}', limit=1 << 20)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise TypeError(name)
    return value


def lane_selection(api: Api, snap: Snapshot) -> tuple[list[str], list[str]]:
    """(problems, NOT RUN lanes) for a branch pipeline, from the select job's artifacts.

    The child must hold every job of lanes.yml and every job the manifest's lanes
    map to, so a lane dropped between the two is missing, never silently absent.
    """
    select = [r for r in snap.rows if not r.via and r.name == SELECT_JOB and not r.bridge]
    if not select:
        return [f'expected job {SELECT_JOB} did not run'], []
    if not any(not r.via and r.name == LANES_BRIDGE and r.bridge for r in snap.rows):
        return [f'expected bridge {LANES_BRIDGE} did not run'], []
    job = select[0]
    try:
        lanes = artifact_json(api, job.project, job.job_id, 'lanes.yml')
        manifest = artifact_json(api, job.project, job.job_id, 'lanes-manifest.json')
        jobs, not_run, _ = selected_jobs(manifest)
    except HttpStatus as error:
        if error.code == 401:
            raise
        return [f'cannot read the lane selection of job {SELECT_JOB} (HTTP {error.code})'], []
    except Exception:  # noqa: BLE001 - any malformed artifact: fixed message, fail closed
        return [f'cannot read the lane selection of job {SELECT_JOB}'], []
    expected = {clean(k) for k in lanes if k != 'include'} | set(jobs)
    child = [r.name for r in snap.rows if r.via == LANES_BRIDGE]
    return missing_jobs(expected, child, f'child pipeline (via {LANES_BRIDGE})'), not_run


def test_id(case: ET.Element) -> str:
    """file::Class::test from a pytest JUnit testcase (file attribute, else classname)."""
    parts = [p for p in case.get('classname', '').split('.') if p]
    cut = len(parts)
    while cut > 0 and parts[cut - 1][:1].isupper():
        cut -= 1
    path = case.get('file') or ('/'.join(parts[:cut]) + '.py' if cut else '')
    return '::'.join(p for p in (path, *parts[cut:], case.get('name', '?')) if p)


def failing_tests(api: Api, row: Row, bound: int) -> None:
    """Fill row.tests from the job's artifacts archive: test ids only, never messages."""
    try:
        data, _ = api.read(f'/projects/{row.project}/jobs/{row.job_id}/artifacts',
                           limit=ARCHIVE_LIMIT)
    except HttpStatus as error:
        if error.code == 401:
            raise
        row.report = f'JUnit report unavailable (HTTP {error.code})'
        return
    except GitLabError:
        row.report = 'JUnit report unavailable'
        return
    found: list[str] = []
    reports = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.endswith('junit.xml'):
                    continue
                if info.file_size > REPORT_LIMIT:
                    raise ValueError(info.filename)
                reports += 1
                for case in ET.fromstring(archive.read(info)).iter('testcase'):
                    if case.find('failure') is not None or case.find('error') is not None:
                        found.append('FAILED  ' + clean(test_id(case), 400))
                    elif case.find('skipped') is not None:
                        found.append('SKIPPED ' + clean(test_id(case), 400))
    except Exception:  # noqa: BLE001 - corrupt, encrypted or unsupported archive
        row.report = 'JUnit report unreadable'
        return
    if not reports:
        row.report = 'no JUnit report in the artifacts'
    elif not found:
        row.report = 'JUnit report lists no failing test'
    row.tests, row.more = found[:bound], max(0, len(found) - bound)


def verdict(api: Api, pipeline: dict, snap: Snapshot, bound: int) -> tuple[list[str], list[str]]:
    """(problems, NOT RUN lanes) for a finished pipeline.

    Problems mean FAILED; else NOT RUN lanes mean PASSED-UNVERIFIED; else PASSED.
    """
    problems = list(snap.problems)
    for label, _, status, count in snap.pipelines:
        if count == 0:
            problems.append(f'{label} has no jobs: nothing ran, which is never a pass')
        if status != 'success':
            problems.append(f'{label}: status {clean(status, 40)}')
    for row in snap.rows:
        if row.status != 'success':
            problems.append(f'{row.label}: {row.status}')
        if row.allow_failure:
            problems.append(f'{row.label}: allow_failure is set, which this repository forbids')
        if row.status == 'failed' and row.junit and not row.bridge:
            failing_tests(api, row, bound)
    not_run: list[str] = []
    if kind_of(pipeline) == 'branch':
        found, not_run = lane_selection(api, snap)
        problems += found
    else:
        problems += missing_jobs(set(GATE_JOBS), [r.name for r in snap.rows if not r.via],
                                 f'pipeline {int(pipeline["id"])}')
    return problems, not_run


def duration(value: float | None) -> str:
    return '-' if value is None else f'{round(value)}s'


def render_text(report: dict) -> str:
    lines = [f'commit {report["sha"]}']
    pipe = report.get('pipeline')
    if pipe:
        lines.append(f'using {pipe["kind"]} pipeline {pipe["id"]}: {pipe["how"]}; '
                     f'ref {pipe["ref"]}, status {pipe["status"]}')
        if pipe['web_url']:
            lines.append(pipe['web_url'])
    else:
        lines.append('no pipeline found for this commit')
    if report['jobs']:
        width = max(len(j['label']) for j in report['jobs']) + 2
        lines.append(f'{"JOB":<{width}}{"STAGE":<10}{"STATUS":<10}DURATION')
        for job in report['jobs']:
            name = job['label'] + (' [bridge]' if job['bridge'] else '')
            flag = '  allow_failure' if job['allow_failure'] else ''
            lines.append(f'{name:<{width}}{job["stage"]:<10}{job["status"]:<10}'
                         f'{duration(job["duration"])}{flag}')
            lines.extend(f'    {test}' for test in job['tests'])
            if job['more']:
                lines.append(f'    ... {job["more"]} more (raise --max-tests to list them)')
            if job['report']:
                lines.append(f'    ({job["report"]})')
    if report['not_run']:
        lines.append('NOT RUN (no GitLab job): ' + ' '.join(report['not_run'])
                     + '. This pipeline does not verify these lanes (exit 4).')
    if report['problems']:
        lines.append('PROBLEMS:')
        lines.extend(f'  - {problem}' for problem in report['problems'])
    lines.append(f'VERDICT: {report["verdict"].upper()}')
    return '\n'.join(lines)


def build_report(sha: str, chosen: tuple[int, dict, str] | None, snap: Snapshot,
                 problems: list[str], not_run: list[str], result: str) -> dict:
    pipe = None
    if chosen:
        _, pipeline, how = chosen
        pipe = {'id': int(pipeline['id']), 'kind': kind_of(pipeline), 'how': how,
                'ref': clean(pipeline.get('ref')), 'status': clean(pipeline.get('status'), 40),
                'web_url': clean(pipeline.get('web_url') or '', 400)}
    jobs = [{'label': r.label, 'pipeline': r.pipeline, 'name': r.name, 'stage': r.stage,
             'status': r.status, 'duration': r.duration, 'allow_failure': r.allow_failure,
             'bridge': r.bridge, 'tests': r.tests, 'more': r.more, 'report': r.report}
            for r in snap.rows]
    code = {'passed': EXIT_PASSED, 'failed': EXIT_FAILED, 'pending': EXIT_PENDING,
            'passed-unverified': EXIT_UNVERIFIED}[result]
    return {'sha': sha, 'pipeline': pipe, 'jobs': jobs, 'not_run': not_run,
            'problems': problems, 'verdict': result, 'exit_code': code}


def base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.username is not None or parsed.password is not None or '@' in parsed.netloc:
        raise GitLabError('the GitLab URL must not carry credentials')
    if parsed.query or parsed.fragment or not parsed.hostname:
        raise GitLabError('the GitLab URL must be https://<host>[/<path>]')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http'
                                         and parsed.hostname in LOOPBACK):
        raise GitLabError('the GitLab URL must use https (plain http only on loopback)')
    return value.rstrip('/')


def read_token(path: str) -> str:
    try:
        token = Path(path).read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError):
        raise GitLabError('cannot read the token file (see --token-file)') from None
    if not TOKEN_RE.fullmatch(token):
        raise GitLabError('the token file is empty or malformed')
    return token


def head_sha() -> str:
    try:
        done = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD^{commit}'],
                              capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        done = None
    if done is None or done.returncode != 0:
        raise GitLabError('no --sha given and no git checkout here')
    return done.stdout.strip()


def bounded(low: float, high: float, kind: type = float) -> Any:
    def parse(text: str) -> Any:
        value = kind(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError('out of range')
        return value
    return parse


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = Parser(prog='ci_status.py', description=__doc__,
                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', default=next(
        (os.environ[n] for n in PROJECT_ENV if os.environ.get(n)), None),
        help='project path (group/name) or numeric id; default $CI_PROJECT_PATH, '
             'then $MFR_GITLAB_PROJECT')
    parser.add_argument('--sha', help='full commit sha; default HEAD of the current checkout')
    parser.add_argument('--gitlab-url', default=next(
        (os.environ[n] for n in URL_ENV if os.environ.get(n)), None),
        help='GitLab base URL; default $CI_SERVER_URL, then $MFR_GITLAB_URL')
    parser.add_argument('--token-file', default=TOKEN_FILE,
                        help=f'file holding a read_api token (default {TOKEN_FILE})')
    parser.add_argument('--wait', action='store_true',
                        help='poll until the pipeline finishes or --timeout passes')
    parser.add_argument('--timeout', type=bounded(1, 21600), default=3600.0,
                        help='seconds --wait polls before exit 2 (1..21600, default 3600)')
    parser.add_argument('--interval', type=bounded(1, 600), default=30.0,
                        help='seconds between polls (1..600, default 30)')
    parser.add_argument('--max-tests', type=bounded(1, 500, int), default=20,
                        help='failing test ids listed per job (1..500, default 20)')
    parser.add_argument('--json', action='store_true', help='print one JSON object')
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict:
    if not args.gitlab_url:
        raise GitLabError('no GitLab URL: pass --gitlab-url or set MFR_GITLAB_URL')
    if not args.project or any(c.isspace() for c in args.project) or len(args.project) > 255:
        raise GitLabError('no valid project: pass --project or set MFR_GITLAB_PROJECT')
    sha = (args.sha or head_sha()).lower()
    if not SHA_RE.fullmatch(sha):
        raise GitLabError('the commit must be a full 40-hex sha')
    api = Api(base_url(args.gitlab_url), read_token(args.token_file))
    project = api.json('/projects/' + urllib.parse.quote(args.project, safe=''))
    if not isinstance(project, dict) or not isinstance(project.get('id'), int):
        raise GitLabError('unexpected response from GitLab (project)')
    deadline = time.monotonic() + (args.timeout if args.wait else 0)
    last = ''
    while True:
        found = find_pipeline(api, project['id'], sha)
        waiting = found if isinstance(found, str) else 'no pipeline for this commit yet'
        chosen = found if isinstance(found, tuple) else None
        snap = Snapshot()
        if chosen:
            inspect(api, chosen[0], chosen[1], '', 0, snap)
        if chosen and not snap.pending:
            problems, not_run = verdict(api, chosen[1], snap, args.max_tests)
            result = 'failed' if problems else 'passed-unverified' if not_run else 'passed'
            return build_report(sha, chosen, snap, problems, not_run, result)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            note = [waiting if not chosen
                    else 'still running at the end of --timeout' if args.wait
                    else 'still running (pass --wait to wait for it)']
            return build_report(sha, chosen, snap, note, [], 'pending')
        done = sum(r.status in TERMINAL for r in snap.rows)
        state = (f'waiting: pipeline {int(chosen[1]["id"])} '
                 f'{clean(chosen[1].get("status"), 40)}, {done}/{len(snap.rows)} jobs finished'
                 if chosen else f'waiting: {waiting}')
        if state != last:
            print(state, file=sys.stderr, flush=True)
            last = state
        time.sleep(min(args.interval, remaining))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run(args)
    except GitLabError as error:
        message = str(error)
    except (KeyError, TypeError, ValueError, AttributeError):
        message = 'unexpected response from GitLab'
    except KeyboardInterrupt:
        message = 'interrupted'
    except Exception:  # noqa: BLE001 - never exception text
        message = 'internal error (details withheld)'
    else:
        print(json.dumps(report, indent=2) if args.json else render_text(report))
        return int(report['exit_code'])
    if args.json:
        print(json.dumps({'verdict': 'error', 'message': message, 'exit_code': EXIT_ERROR}))
    else:
        print(f'ci_status: {message}', file=sys.stderr)
    return EXIT_ERROR


if __name__ == '__main__':
    raise SystemExit(main())
