"""Collect current native PR evidence for the approved-base acceptance consumer."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.acceptance_evidence import (
    LIMIT,
    PRODUCERS,
    NativeProducer,
    artifact_members,
    normalize_native,
    object_json,
    paginate,
    require,
    unit_producers,
)
from scripts.unit_evidence import INPUTS as UNIT_INPUTS
from scripts.unit_evidence import validate_union
from scripts.verifier_approval import (
    CATALOGUE,
    VerifierApprovalRequired,
    approved_catalogue,
    approved_inputs,
    validated_decision,
)

VERIFICATION_INPUTS = (
    *UNIT_INPUTS,
    'scripts/review_tooling.py', 'scripts/ci_evidence.py', 'Taskfile.yml',
    'scripts/tools/run-task.sh', 'scripts/tools/task-pin.txt',
    'scripts/check_hazard_sensitivity.py', 'tests/hazards/critical.json',
)


class GitHub:
    """Use the runner's existing gh authentication without exposing credentials."""

    def __init__(self, repository: str):
        require(bool(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)))
        self.repository = repository
        self.prefix = 'repos/' + repository + '/'

    def raw(self, endpoint: str) -> bytes:
        require(endpoint.startswith(self.prefix))
        result = subprocess.run(['gh', 'api', endpoint], capture_output=True, check=False, timeout=60)
        require(result.returncode == 0 and len(result.stdout) <= LIMIT)
        return result.stdout

    def get(self, endpoint: str) -> Any:
        return json.loads(self.raw(endpoint))

    def write(self, endpoint: str, payload: dict[str, Any], *, method: str) -> dict[str, Any]:
        check_endpoint = endpoint == self.prefix + 'check-runs' or bool(re.fullmatch(
            re.escape(self.prefix) + r'check-runs/[0-9]+', endpoint))
        comment_endpoint = bool(re.fullmatch(re.escape(self.prefix) + r'issues/[1-9][0-9]*/comments', endpoint))
        require((check_endpoint and method in {'POST', 'PATCH'}) or (comment_endpoint and method == 'POST'))
        result = subprocess.run(['gh', 'api', '--method', method, endpoint, '--input', '-'],
                                input=json.dumps(payload), capture_output=True, text=True,
                                check=False, timeout=60)
        require(result.returncode == 0 and len(result.stdout) <= LIMIT)
        value = json.loads(result.stdout)
        require(isinstance(value, dict))
        return value

    def blob(self, sha: str, path: str) -> bytes:
        require(bool(re.fullmatch('[a-f0-9]{40}', sha)))
        require(path in VERIFICATION_INPUTS or bool(re.fullmatch(r'taskfiles/[A-Za-z0-9_-]+\.yml', path)) or
                path in {'.github/workflows/' + p.workflow for p in PRODUCERS})
        data = self.get(self.prefix + f'contents/{path}?ref={sha}')
        require(data['type'] == 'file' and data['encoding'] == 'base64')
        raw = base64.b64decode(data['content'].replace('\n', ''), validate=True)
        require(len(raw) <= LIMIT)
        return raw


def candidate_identity(pr: dict[str, Any], repository: str) -> dict[str, Any]:
    require(pr['base']['repo']['full_name'] == repository)
    require(pr['base']['ref'] == pr['base']['repo']['default_branch'])
    result = {'repository': repository, 'repository_id': pr['base']['repo']['id'],
              'head_repository_id': pr['head']['repo']['id'], 'number': pr['number'],
              'head_sha': pr['head']['sha'], 'base_sha': pr['base']['sha'],
              'execution_sha': pr['merge_commit_sha']}
    require(all(isinstance(result[key], str) and re.fullmatch('[a-f0-9]{40}', result[key]) is not None
                for key in ('head_sha', 'base_sha', 'execution_sha')))
    require(pr['state'] == 'open' and pr['mergeable'] is True)
    return result


class StaleBase(ValueError):
    """The candidate's recorded base is behind the live default-branch tip.

    It is still a ValueError, so every existing refusal path keeps refusing;
    the publisher only names the case and its fix instead of a generic one.
    """

    def __init__(self, branch: str, base_sha: str, live_sha: str):
        super().__init__('candidate base is not the live default-branch tip')
        self.branch, self.base_sha, self.live_sha = branch, base_sha, live_sha

    def report(self) -> str:
        return ('## Candidate Technical Verification\n\n'
                f'- **Base SHA**: `{self.base_sha}`\n'
                f'- **Live `{self.branch}` tip**: `{self.live_sha}`\n'
                '- **Technical Verification**: `incomplete`\n\n'
                f'The candidate was built on a base that is no longer the tip of `{self.branch}`, '
                'so its evidence cannot be current. Fix: update the branch (merge or rebase onto '
                f'the live `{self.branch}` tip) and let the native workflows run on the new merge.')


def require_current_base(api: GitHub, pr: dict[str, Any], candidate: dict[str, Any]) -> None:
    """PR merge metadata may lag a default-branch push; read the live ref too."""
    branch = pr['base']['ref']
    require(isinstance(branch, str) and bool(branch))
    ref = api.get(api.prefix + 'git/ref/heads/' + quote(branch, safe='/'))
    require(ref['ref'] == 'refs/heads/' + branch and ref['object']['type'] == 'commit')
    live = ref['object']['sha']
    require(isinstance(live, str) and re.fullmatch('[a-f0-9]{40}', live) is not None)
    if live != candidate['base_sha']:
        raise StaleBase(branch, candidate['base_sha'], live)


# GitHub's nonterminal run states. A queued workflow has not produced its jobs
# or artifacts yet, so its required lanes cannot be called missing failures.
ACTIVE_RUN_STATUSES = frozenset({'queued', 'in_progress', 'requested', 'waiting', 'pending'})
RUN_STATUSES = ACTIVE_RUN_STATUSES | {'completed'}


def timestamp(value: Any) -> datetime:
    """Parse a GitHub ISO timestamp and require a timezone."""
    require(isinstance(value, str) and bool(value))
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None)
    return parsed


def current_native_run(candidate: dict[str, Any], workflow: str, run: dict[str, Any],
                       execution_time: datetime) -> bool:
    """Bind a run to the current candidate head, PR and test merge.

    The run API exposes the pull-request head, not the test-merge SHA. Requiring
    the run to have been created after the current execution commit was
    generated keeps an older merge's missing lanes from reading as pending. A content-twin
    merge can still supply successful receipts; only its *missing* lanes are not
    current.

    When GitHub supplies a non-empty ``pull_requests`` list, the run must name
    this PR. Empty or missing metadata falls back to the head/repository/time
    binding above; that limitation is explicit rather than inferred.
    """
    try:
        pull_requests = run.get('pull_requests')
        return (run['event'] == 'pull_request'
                and run['path'] == '.github/workflows/' + workflow
                and run['head_sha'] == candidate['head_sha']
                and run['repository']['id'] == candidate['repository_id']
                and run['repository']['full_name'] == candidate['repository']
                and run['head_repository']['id'] == candidate['head_repository_id']
                and run['status'] in RUN_STATUSES
                and (not pull_requests or any(
                    isinstance(item, dict) and item.get('number') == candidate['number']
                    for item in pull_requests))
                # The run's ref is fixed when the run is created; a rerun keeps
                # that ref, so the original creation time binds its test merge.
                and timestamp(run.get('created_at') or run.get('run_started_at')) >= execution_time)
    except (KeyError, TypeError, ValueError):
        return False


def acceptance_pending(summary: Any, pending_lanes: set[str]) -> bool:
    """Pending publication is allowed only when every blocker is a pending miss.

    Completed missing, skipped, cancelled, failed, unverified and stale lanes
    stay failures even while another native workflow is still running.
    """
    from scripts.review_tooling import LaneState

    blockers = [lane for lane in summary.lanes if lane.blocks_readiness]
    return bool(blockers) and all(
        lane.state is LaneState.SELECTED_MISSING and lane.name in pending_lanes
        for lane in blockers)


def review_template(api: GitHub, number: int) -> dict[str, Any]:
    """Generate identity fields, never a verdict or a submitted review."""
    from scripts.review_tooling import SCHEMA_VERSION

    require(type(number) is int and number > 0)
    pr = api.get(api.prefix + f'pulls/{number}')
    candidate = candidate_identity(pr, api.repository)
    require_current_base(api, pr, candidate)
    commit = api.get(api.prefix + 'commits/' + candidate['execution_sha'])
    require(commit['sha'] == candidate['execution_sha'])
    require([p['sha'] for p in commit['parents']] == [candidate['base_sha'], candidate['head_sha']])
    current = api.get(api.prefix + f'pulls/{number}')
    require(candidate_identity(current, api.repository) == candidate and current['draft'] == pr['draft'])
    require_current_base(api, current, candidate)
    return {'schema_version': SCHEMA_VERSION,
            **{key: candidate[key] for key in ('head_sha', 'base_sha', 'execution_sha')},
            'candidate_currentness': 'current',
            'code_assessment': '<acceptable|changes_required|incomplete>',
            'verification': '<complete|incomplete|failed>',
            'merge_readiness': '<ready_for_maintainer|not_ready>',
            'material_findings': [],
            'evidence': {'review': '<reviewed scope and verification evidence>'}}


def review_template_comment(template: dict[str, Any]) -> str:
    """A copyable skeleton, explicitly not an independent review."""
    return ('<!-- generated-human-review-template -->\n'
            '### Your review template\n\n'
            'Copy the JSON below into a new comment or Comment review and fill the human judgment fields. '
            'The commit IDs are already filled in. This generated template is **not a review or approval**. '
            'Use the newest template if the PR changes; carry forward prior material findings. '
            'If there are no findings, leave `material_findings` as the empty array `[]`; '
            'do not put the string "[]" inside finding fields. Otherwise add finding objects with '
            '`id`, `disposition`, and `description`. Replace the evidence placeholder with what you reviewed.\n\n'
            '```json\n' + json.dumps(template, indent=2) + '\n```\n')


def post_review_template(api: GitHub, number: int, template: dict[str, Any]) -> None:
    """Append an exact template once; never edit comments or trust a marker alone."""
    require(type(number) is int and number > 0)
    body = review_template_comment(template)
    endpoint = api.prefix + f'issues/{number}/comments'
    comments = paginate(api.get, endpoint)
    if not any(comment.get('body') == body for comment in comments):
        api.write(endpoint, {'body': body}, method='POST')


def verification_inputs(root: Path) -> dict[str, str]:
    paths = (*VERIFICATION_INPUTS, *(str(p.relative_to(root)) for p in (root / 'taskfiles').glob('*.yml')),
             *sorted({'.github/workflows/' + p.workflow for p in PRODUCERS}))
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths}


# A job copied unchanged into a later attempt keeps these fields. "Re-run failed
# jobs" lists every job in the new attempt, under a new job id and that attempt
# number, but only the rerun jobs execute and upload evidence there.
CARRIED_JOB_FIELDS = ('name', 'head_sha', 'status', 'conclusion', 'started_at', 'completed_at')


class RunAttempts:
    """Read one run's attempts lazily; every attempt must belong to that run."""

    def __init__(self, api: GitHub, run: dict[str, Any]):
        require(type(run['id']) is int and type(run['run_attempt']) is int and run['run_attempt'] >= 1)
        self.api, self.run = api, run
        self._jobs: dict[int, list[dict[str, Any]]] = {}
        self._runs: dict[int, dict[str, Any]] = {run['run_attempt']: run}

    def jobs(self, attempt: int) -> list[dict[str, Any]]:
        if attempt not in self._jobs:
            self._jobs[attempt] = paginate(
                self.api.get, self.api.prefix + f"actions/runs/{self.run['id']}/attempts/{attempt}/jobs", 'jobs')
        return self._jobs[attempt]

    def attempt_run(self, attempt: int) -> dict[str, Any]:
        """An earlier attempt of the same run, for the same head and workflow."""
        if attempt not in self._runs:
            value = self.api.get(self.api.prefix + f"actions/runs/{self.run['id']}/attempts/{attempt}")
            require(value['id'] == self.run['id'] and value['run_attempt'] == attempt)
            require(all(value[key] == self.run[key] for key in ('event', 'path', 'head_sha')))
            require(value['repository']['id'] == self.run['repository']['id'])
            require(value['repository']['full_name'] == self.run['repository']['full_name'])
            require(value['head_repository']['id'] == self.run['head_repository']['id'])
            require(value['status'] == 'completed')
            self._runs[attempt] = value
        return self._runs[attempt]

    def named(self, attempt: int, name: str) -> dict[str, Any] | None:
        selected = [job for job in self.jobs(attempt) if job['name'] == name]
        if not selected:
            return None
        require(len(selected) == 1)
        job = selected[0]
        require(job['run_id'] == self.run['id'] and job['run_attempt'] == attempt)
        return job

    def execution(self, name: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Return (attempt run, job) for the attempt that executed the latest record.

        The latest attempt's record decides the job's state. When that record is
        a carried copy (it started before its attempt did), the attempt that
        actually ran it supplies the job, the run identity and the evidence
        artifact. The copy must match that record exactly; anything else refuses.
        Missing timestamps never move evidence to an earlier attempt.
        """
        attempt = self.run['run_attempt']
        job = self.named(attempt, name)
        owner = self.run
        while job is not None and attempt > 1 and job['status'] == 'completed':
            started, began = job.get('started_at'), owner.get('run_started_at')
            if not started or not began or timestamp(started) >= timestamp(began):
                break
            previous = self.named(attempt - 1, name)
            require(previous is not None)
            assert previous is not None
            require(all(previous.get(key) == job.get(key) for key in CARRIED_JOB_FIELDS))
            attempt -= 1
            job, owner = previous, self.attempt_run(attempt)
        return owner, job


def collect_native(api: GitHub, pr: dict[str, Any], approved_root: Path) -> dict[str, Any]:
    """Collect the latest run/attempt, never fall back to an older green result.

    approved_root must be the trusted base checkout. Reading candidate content
    through the API does not execute it. The publisher must repeat the candidate
    and run checks before publication; this snapshot alone grants no acceptance.
    A lane without evidence is pending only while a run bound to the current
    head and test merge is still active; completed misses stay missing.
    """
    candidate = candidate_identity(pr, api.repository)
    # The live ref first: a base behind the tip is named as such (StaleBase)
    # rather than reported as an approved-root mismatch.
    require_current_base(api, pr, candidate)
    root_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=approved_root, text=True).strip()
    require(root_sha == candidate['base_sha'])
    require(not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'],
                                        cwd=approved_root, text=True).strip())
    policy_digest = hashlib.sha256((approved_root / 'scripts/review_tooling.py').read_bytes()).hexdigest()
    # A distinct decision can change anchors, never the approved obligations.
    # Only after that proof may the ordinary implementation comparison treat
    # these catalogue bytes as approved. Mixed changes require both decisions.
    approved_catalogue_bytes = (approved_root / CATALOGUE).read_bytes()
    catalogue_bytes, catalogue_approval = approved_catalogue(api, candidate, approved_catalogue_bytes)
    expected_inputs = verification_inputs(approved_root)
    expected_inputs[CATALOGUE] = hashlib.sha256(catalogue_bytes).hexdigest()
    # Receipt hashes are claims, not proof of the actual executed source.
    policy_inputs, verifier_approval = approved_inputs(api, candidate, expected_inputs)
    # The receipt describes the selector that ran in the candidate job. Its
    # approved bytes may differ from main, but cannot select acceptance lanes:
    # collect_acceptance continues to use this approved-base process's policy.
    execution_policy_digest = policy_inputs['scripts/review_tooling.py']
    producer_digest = policy_inputs['scripts/ci_evidence.py']
    hazard_policy = {'catalogue': catalogue_bytes, 'approved_catalogue': approved_catalogue_bytes,
                     'runner_sha256': policy_inputs['scripts/check_hazard_sensitivity.py']}
    execution_commit = api.get(api.prefix + 'commits/' + candidate['execution_sha'])
    require(execution_commit['sha'] == candidate['execution_sha'])
    require([parent['sha'] for parent in execution_commit['parents']]
            == [candidate['base_sha'], candidate['head_sha']])
    execution_time = timestamp(execution_commit['commit']['committer']['date'])
    runs = paginate(api.get, api.prefix + f"actions/runs?event=pull_request&head_sha={candidate['head_sha']}", 'workflow_runs')
    workflows = {p.workflow for p in PRODUCERS}
    latest = {}
    for workflow in workflows:
        matches = [r for r in runs if r['path'] == '.github/workflows/' + workflow]
        if matches:
            latest[workflow] = max(matches, key=lambda r: r['id'])
    native = []
    snapshots = {}
    stale_runs = []
    active = False
    producers: tuple[NativeProducer, ...] = PRODUCERS
    for workflow, run in latest.items():
        run = api.get(api.prefix + f"actions/runs/{run['id']}")
        attempts = RunAttempts(api, run)
        attempts.jobs(run['run_attempt'])
        artifacts = paginate(api.get, api.prefix + f"actions/runs/{run['id']}/artifacts", 'artifacts')
        snapshots[workflow] = {'run_id': run['id'], 'run_attempt': run['run_attempt'], 'status': run['status']}
        current = current_native_run(candidate, workflow, run, execution_time)
        pending = current and run['status'] in ACTIVE_RUN_STATUSES
        active = active or pending
        if not current:
            stale_runs.append({'workflow': workflow, 'run_id': run['id'], 'run_attempt': run['run_attempt']})
        source = api.blob(candidate['execution_sha'], '.github/workflows/' + workflow)
        workflow_digest = policy_inputs['.github/workflows/' + workflow]
        if workflow == 'ci.yml':
            producers = tuple(producer for producer in producers if producer.lane != 'unit_tests') + unit_producers(source, workflow_digest)
        for producer in (definition for definition in producers if definition.workflow == workflow):
            record: dict[str, Any] = {'lane': producer.lane, 'job': producer.job, 'status': 'missing'}
            try:
                # The job's latest record decides; its evidence comes from the
                # attempt that executed it, which a failed-jobs rerun may not be.
                owner, selected = attempts.execution(producer.job)
                if selected is None:
                    record['status'] = 'pending' if pending else ('missing' if current else 'stale')
                    native.append(record)
                    continue
                job = selected
                if job['status'] != 'completed':
                    record['status'] = 'pending' if pending else ('missing' if current else 'stale')
                    native.append(record)
                    continue
                if job['conclusion'] in {'failure', 'cancelled', 'skipped', 'timed_out'}:
                    record['status'] = job['conclusion']
                    native.append(record)
                    continue
                if job['conclusion'] != 'success':
                    # neutral/action_required and any future non-success
                    # completion are final evidence failures, never pending.
                    record['status'] = 'unverified' if current else 'stale'
                    native.append(record)
                    continue
                selected_artifacts = [a for a in artifacts if a['name'] ==
                                      f"evidence-{producer.artifact}-attempt-{owner['run_attempt']}"]
                if not selected_artifacts:
                    # The job is complete: a missing artifact is final, even if
                    # sibling jobs keep the parent run active.
                    record['status'] = 'missing' if current else 'stale'
                    native.append(record)
                    continue
                require(len(selected_artifacts) == 1)
                artifact = selected_artifacts[0]
                require(type(artifact['id']) is int and 0 < artifact['size_in_bytes'] <= LIMIT)
                archive = api.raw(api.prefix + f"actions/artifacts/{artifact['id']}/zip")
                report = object_json(artifact_members(archive, artifact['digest'])['evidence.json'])
                tested_sha = report['execution_sha']
                require(isinstance(tested_sha, str) and re.fullmatch('[a-f0-9]{40}', tested_sha) is not None)
                if not current and tested_sha == candidate['execution_sha']:
                    # A run created before the current test merge cannot claim
                    # the current execution SHA. Only a receipt naming another
                    # merge with identical parents/tree may use the content-twin
                    # exception below.
                    record['status'] = 'stale'
                    native.append(record)
                    continue
                tested_commit = (execution_commit if tested_sha == candidate['execution_sha'] else
                                 api.get(api.prefix + 'commits/' + tested_sha))
                # normalize_native refuses an active run. Validate the completed
                # job against a completed-status copy so a corrupt/forged
                # artifact fails now instead of hiding as pending.
                active_run = run['status'] in ACTIVE_RUN_STATUSES
                normalization_run = ({**owner, 'status': 'completed'}
                                     if owner['status'] in ACTIVE_RUN_STATUSES else owner)
                result = normalize_native(candidate=candidate, producer=producer, run=normalization_run, job=job,
                                          artifact=artifact, execution_commit=tested_commit,
                                          candidate_commit=execution_commit, archive=archive,
                                          policy_digest=execution_policy_digest, producer_digest=producer_digest,
                                          workflow_digest=workflow_digest, workflow_source=source,
                                          hazard_policy=hazard_policy,
                                          unit_policy={path: policy_inputs[path] for path in UNIT_INPUTS})
                if pending:
                    record['status'] = 'pending'
                elif active_run:
                    record['status'] = 'stale'
                else:
                    record.update(result)
            except (KeyError, TypeError, ValueError, OSError):
                # A valid active run is pending only through the explicit path
                # above; any normalization refusal is unverified or stale.
                record['status'] = 'unverified' if current else 'stale'
            native.append(record)
    statuses = {}
    pending_lanes: set[str] = set()
    lane_names = {producer.lane for producer in producers}
    for lane in lane_names:
        required_jobs = [producer for producer in producers if producer.lane == lane]
        results = [r for r in native if r['lane'] == lane]
        values = {r['status'] for r in results}
        if values & {'failure', 'timed_out', 'unverified'}:
            statuses[lane] = 'unverified' if 'unverified' in values else 'failure'
        elif values & {'cancelled', 'skipped'}:
            statuses[lane] = 'cancelled' if 'cancelled' in values else 'skipped'
        elif len(results) == len(required_jobs) and values == {'success'}:
            statuses[lane] = 'success'
            if lane == 'unit_tests':
                try:
                    validate_union([record['unit_coverage'] for record in results], shards=len(required_jobs))
                except (ValueError, KeyError, TypeError):
                    statuses[lane] = 'unverified'
        elif (len(results) == len(required_jobs) and 'pending' in values
              and values <= {'success', 'pending'}):
            pending_lanes.add(lane)
        # An absent workflow, shard, or artifact stays missing in the existing
        # taxonomy: do not turn it into a reported execution failure.
    # A workflow that has not been created yet has no run to inspect. While a
    # current candidate run is active, its lanes are pending rather than a
    # completed missing result. Once every current run completes, this falls
    # back to the ordinary missing/failure taxonomy.
    if active:
        evaluated = {record['lane'] for record in native}
        pending_lanes.update(lane_names - evaluated)
    return {'candidate': candidate, 'native': native, 'lane_statuses': statuses, 'runs': snapshots,
            'pending_lanes': pending_lanes, 'stale_runs': stale_runs,
            'policy_sha256': policy_digest, 'producer_sha256': producer_digest, 'policy_inputs': policy_inputs,
            'verifier_approval': verifier_approval, 'catalogue_approval': catalogue_approval}


def changed_pr_paths(api: GitHub, pr: dict[str, Any]) -> list[str]:
    """Retain both rename names verbatim and reject truncated PR file lists."""
    files = paginate(api.get, api.prefix + f"pulls/{pr['number']}/files", identity_key='filename')
    require(type(pr['changed_files']) is int and len(files) == pr['changed_files'])
    paths = []
    for item in files:
        paths.append(item['filename'])
        if item['status'] == 'renamed':
            require(isinstance(item.get('previous_filename'), str) and bool(item['previous_filename']))
            paths.append(item['previous_filename'])
    return paths


def collect_acceptance(api: GitHub, number: int, approved_root: Path) -> dict[str, Any]:
    """Assemble the existing acceptance summary; this function has no writes."""
    from scripts.review_tooling import (
        build_acceptance_summary,
        classify_paths,
    )

    require(type(number) is int and number > 0)
    pr = api.get(api.prefix + f'pulls/{number}')
    candidate = candidate_identity(pr, api.repository)
    paths = changed_pr_paths(api, pr)
    decision = classify_paths(paths)
    manifest = {**candidate, 'profile': decision.profile.value,
                'matched_categories': decision.matched_categories, 'changed_paths': paths}
    native = collect_native(api, pr, approved_root)
    require(native['candidate'] == candidate)
    require(type(pr['draft']) is bool)
    summary = build_acceptance_summary(manifest, native['lane_statuses'])
    pending_lanes = set(native.get('pending_lanes', ()))
    pending = acceptance_pending(summary, pending_lanes)
    result = summary.to_dict()
    result['native'] = [{key: value for key, value in record.items() if key != 'results'}
                        for record in native['native']]
    result['runs'] = native['runs']
    result['policy_sha256'] = native['policy_sha256']
    result['policy_inputs'] = native['policy_inputs']
    result['verifier_approval'] = native.get('verifier_approval')
    result['catalogue_approval'] = native.get('catalogue_approval')
    result['pending'] = pending
    result['pending_lanes'] = sorted(pending_lanes)
    result['stale_runs'] = native.get('stale_runs', [])
    result['markdown_report'] = summary.markdown_report
    if pending:
        required_pending = [lane.name for lane in summary.lanes
                            if lane.required and lane.name in pending_lanes]
        lanes = ', '.join(f'`{lane}`' for lane in required_pending)
        result['markdown_report'] = (
            '## Candidate Technical Verification\n\n'
            f"- **Head SHA**: `{candidate['head_sha']}`\n"
            f"- **Base SHA**: `{candidate['base_sha']}`\n"
            f"- **Execution SHA**: `{candidate['execution_sha']}`\n"
            '- **Prerequisite Obligations**: `PENDING_NATIVE_RUNS`\n'
            '- **Technical Verification**: `pending`\n\n'
            'Current native runs for this execution are still queued or in progress. '
            f'Missing required lanes are pending, not failed: {lanes}. '
            'The next native workflow event or the periodic reconciliation re-evaluates '
            'this candidate. A completed run with a missing lane still fails.')
    elif result['stale_runs']:
        runs = ', '.join(f"`{run['workflow']}` run `{run['run_id']}`" for run in result['stale_runs'])
        result['markdown_report'] += (
            f'\n\nNative run(s) {runs} could not be attributed to the current head and '
            'test merge, so their missing lanes are not pending.')
    tested_shas = sorted({record['execution_sha'] for record in native['native']
                          if record.get('status') == 'success' and record.get('execution_sha')})
    if any(sha != candidate['execution_sha'] for sha in tested_shas):
        result['markdown_report'] += ('\n\nNative evidence tested execution SHA(s): '
                                      + ', '.join(f'`{sha}`' for sha in tested_shas)
                                      + '. GitHub commit records verify identical file trees and exact '
                                      'current base/head parents against the current test merge.')
    if result['verifier_approval'] is not None:
        run_id = result['verifier_approval']['run_id']
        result['markdown_report'] += ('\n\nVerifier implementation trust: [maintainer decision]('
                                      f'https://github.com/{api.repository}/actions/runs/{run_id}). '
                                      'This binds the exact candidate bytes; it does not waive technical checks '
                                      'or authorize merging.')
    if result['catalogue_approval'] is not None:
        run_id = result['catalogue_approval']['run_id']
        result['markdown_report'] += ('\n\nCatalogue anchor trust: [maintainer decision]('
                                      f'https://github.com/{api.repository}/actions/runs/{run_id}). '
                                      'Only the exact before/after strings are adopted; all approved-main '
                                      'challenge obligations and technical checks still apply.')
    result['candidate'] = candidate
    result['draft'] = pr['draft']
    return result


def recheck_current(api: GitHub, result: dict[str, Any]) -> None:
    """Refuse publication when a head/base or latest native attempt moved.

    This is a bounded observation, not atomic merge authorization. Maintainer
    enforcement must also require an up-to-date branch and the trusted status
    source; a workflow result alone cannot establish those repository rules.
    """
    candidate = result['candidate']
    pr = api.get(api.prefix + f"pulls/{candidate['number']}")
    require(candidate_identity(pr, api.repository) == candidate)
    runs = paginate(api.get, api.prefix + f"actions/runs?event=pull_request&head_sha={candidate['head_sha']}", 'workflow_runs')
    latest = {}
    for producer in PRODUCERS:
        matches = [run for run in runs if run['path'] == '.github/workflows/' + producer.workflow]
        if matches:
            run = max(matches, key=lambda run: run['id'])
            latest[producer.workflow] = {'run_id': run['id'], 'run_attempt': run['run_attempt'], 'status': run['status']}
    require(latest == result['runs'])
    if result.get('verifier_approval') is not None:
        require(validated_decision(api, candidate, result['verifier_approval']['inputs'])
                == result['verifier_approval'])
    if result.get('catalogue_approval') is not None:
        _, current = approved_catalogue(api, candidate, api.blob(candidate['base_sha'], CATALOGUE))
        require(current == result['catalogue_approval'])
    require_current_base(api, pr, candidate)


def publish_acceptance(api: GitHub, number: int, approved_root: Path, *,
                       template_directory: Path | None = None, publisher_run_id: int | None = None,
                       post_templates: bool = False) -> dict[str, Any]:
    """Publish a fresh pending check before collecting; only a rechecked pass goes green.

    Missing lanes backed by an active current run leave the check in progress
    and non-passing; completed misses, failures and stale runs complete as
    failure. Authentication determines the check's App source. Ordinary Actions tokens
    provide an advisory result; required enforcement needs the maintainer's
    dedicated App and repository rules, as documented in docs/acceptance.md.
    """
    import zipfile

    pr = api.get(api.prefix + f'pulls/{number}')
    head = pr['head']['sha']
    require(isinstance(head, str) and re.fullmatch('[a-f0-9]{40}', head) is not None)
    check = api.write(api.prefix + 'check-runs', {
        'name': 'current-candidate-acceptance', 'head_sha': head, 'status': 'in_progress',
        'output': {'title': 'Collecting current candidate evidence',
                   'summary': 'Acceptance is pending while current native execution evidence is verified.'}},
        method='POST')
    require(type(check['id']) is int and check['id'] > 0)
    template_note = ''
    if template_directory is not None:
        try:
            require(type(publisher_run_id) is int and publisher_run_id > 0)
            template = review_template(api, number)
            require(template['head_sha'] == head)
            template_directory.mkdir(parents=True, exist_ok=True)
            filename = f'pr-{number}-{head}.json'
            (template_directory / filename).write_text(json.dumps(template, indent=2) + '\n')
            template_note = (f'\n\n### Human review template\nDownload `{filename}` from the '
                             '`review-templates-<attempt>` artifact in '
                             f'[this publisher run](https://github.com/{api.repository}/actions/runs/{publisher_run_id}). '
                             'The artifact is available after the upload step completes. '
                             'Fill the human judgment fields and submit your review; regenerate if the candidate changes.')
            if post_templates:
                try:
                    post_review_template(api, number, template)
                    template_note = '\n\nA copyable review template is posted in the PR conversation.' + template_note
                except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
                    template_note = '\n\nTemplate comment unavailable; use the artifact below.' + template_note
        except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
            template_note = '\n\nReview template unavailable: current candidate identity could not be verified.'
    try:
        result = collect_acceptance(api, number, approved_root)
        require(result['candidate']['head_sha'] == head)
        recheck_current(api, result)
    except VerifierApprovalRequired as exc:
        guidance = ('The maintainer must review these exact changes, then run the '
                    '[Verifier update decision](https://github.com/' + api.repository
                    + '/actions/workflows/verifier-update.yml) workflow on main with this PR number. '
                    'Ready-for-review is not a verifier trust decision. All selected technical checks still apply.')
        if exc.kind == 'catalogue-anchors':
            guidance = ('The maintainer must review the exact before/after anchor changes, then run the '
                        '[Verifier update decision](https://github.com/' + api.repository
                        + '/actions/workflows/verifier-update.yml) workflow on main with this PR number '
                        'and kind catalogue-anchors. An implementation decision cannot approve anchors; '
                        'all approved-main challenge obligations and technical checks still apply.')
        elif not exc.approval_allowed:
            guidance = ('Hazard-catalogue changes cannot use a verifier implementation '
                        'decision. They require a separately reviewed catalogue change; no obligations are waived.')
        result = {'all_prerequisites_met': False, 'verification_status': 'incomplete',
                  'error': ('hazard_catalogue_requires_maintainer_decision' if exc.kind == 'catalogue-anchors'
                            else 'verifier_update_requires_maintainer_decision' if exc.approval_allowed
                            else 'verifier_policy_change_not_supported'),
                  'markdown_report': ('Verifier files differ from approved main: ' + ', '.join(exc.paths)
                                      + '.\n\n' + guidance)}
    except StaleBase as exc:
        result = {'all_prerequisites_met': False, 'verification_status': 'incomplete',
                  'error': 'candidate_base_not_current', 'markdown_report': exc.report()}
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile):
        result = {'all_prerequisites_met': False, 'verification_status': 'incomplete',
                  'error': 'acceptance_evidence_unavailable_or_changed'}
    ready = result['all_prerequisites_met']
    pending = bool(result.get('pending')) and not ready
    # The report contains generated lane/status text; no artifact commands run.
    summary = result.get('markdown_report', 'Current evidence is unavailable or changed; acceptance is not ready.')
    summary = summary[:55000] + template_note
    if pending:
        # A completed neutral check satisfies required checks on this
        # repository. Leave pending in progress and non-passing; a later native
        # event or reconciliation publishes success or failure.
        payload = {'status': 'in_progress',
                   'output': {'title': 'Candidate CI is still running',
                              'summary': summary[:60000]}}
    else:
        payload = {'status': 'completed',
                   'conclusion': 'success' if ready else 'failure',
                   'output': {'title': ('Technical verification passed' if ready else
                                        'Technical verification incomplete'),
                              'summary': summary[:60000]}}
    api.write(api.prefix + f"check-runs/{check['id']}", payload, method='PATCH')
    return {**result, 'check_run_id': check['id']}


def main(argv: list[str] | None = None) -> int:
    """Unmet or unavailable evidence exits nonzero; writes require --publish."""
    import argparse
    import zipfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--pr', type=int)
    selection.add_argument('--all-open', action='store_true')
    selection.add_argument('--head-sha', help='evaluate only open PRs whose current head is this commit')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--review-template', action='store_true',
                        help='print current identity and unset human review fields; never submit')
    parser.add_argument('--review-templates-dir', type=Path)
    parser.add_argument('--post-review-templates', action='store_true')
    parser.add_argument('--publisher-run-id', type=int)
    parser.add_argument('--approved-root', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if args.head_sha is not None and re.fullmatch('[a-f0-9]{40}', args.head_sha) is None:
        parser.error('--head-sha requires a full lowercase commit SHA')
    bulk = args.all_open or args.head_sha is not None
    if args.review_template and (args.publish or bulk):
        parser.error('--review-template requires --pr and cannot be combined with --publish')
    if ((args.review_templates_dir is not None or args.publisher_run_id is not None)
            and (not args.publish or args.review_templates_dir is None
                 or args.publisher_run_id is None or args.publisher_run_id <= 0)):
        parser.error('--review-templates-dir and positive --publisher-run-id require --publish')
    if args.post_review_templates and (not args.publish or args.review_templates_dir is None):
        parser.error('--post-review-templates requires --publish and --review-templates-dir')
    results = []
    try:
        api = GitHub(args.repository)
        if args.review_template:
            print(json.dumps(review_template(api, args.pr), indent=2))
            return 0
        if args.pr is not None:
            numbers = [args.pr]
        else:
            # An event for a superseded head selects nothing: the current head
            # has its own events and the periodic all-open reconciliation.
            numbers = [pr['number'] for pr in paginate(api.get, api.prefix + 'pulls?state=open')
                       if args.head_sha is None or pr['head']['sha'] == args.head_sha]
        for number in numbers:
            require(type(number) is int and number > 0)
            if args.publish:
                if args.review_templates_dir is not None:
                    result = publish_acceptance(api, number, args.approved_root.resolve(),
                                                template_directory=args.review_templates_dir,
                                                publisher_run_id=args.publisher_run_id,
                                                post_templates=args.post_review_templates)
                else:
                    result = publish_acceptance(api, number, args.approved_root.resolve())
            else:
                result = collect_acceptance(api, number, args.approved_root.resolve())
                recheck_current(api, result)
            results.append(result)
    except StaleBase as exc:
        # Commit IDs and the fixed fix are not diagnostics; nothing remote is echoed.
        print(json.dumps({'schema_version': 2, 'all_prerequisites_met': False,
                          'verification_status': 'incomplete', 'error': 'candidate_base_not_current',
                          'base_sha': exc.base_sha, 'live_base_sha': exc.live_sha,
                          'fix': 'update the branch onto the live default-branch tip'}))
        return 1
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile):
        # Never print API errors, remote bodies or authentication diagnostics.
        print(json.dumps({'schema_version': 2, 'all_prerequisites_met': False,
                          'verification_status': 'incomplete',
                          'error': 'acceptance_evidence_unavailable_or_changed'}))
        return 1
    print(json.dumps(results if bulk else results[0], sort_keys=True))
    # Bulk publisher health is separate from each PR's technical result.
    # API/publication errors above still exit nonzero; red candidate checks
    # remain red without failing an unrelated PR's aggregate workflow job.
    if bulk and args.publish:
        return 0
    return 0 if all(result['all_prerequisites_met'] for result in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
