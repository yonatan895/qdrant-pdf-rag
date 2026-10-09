"""Collect current native PR evidence for the approved-base acceptance consumer."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
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
from scripts.verifier_approval import VerifierApprovalRequired, approved_inputs, validated_decision

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


def require_current_base(api: GitHub, pr: dict[str, Any], candidate: dict[str, Any]) -> None:
    """PR merge metadata may lag a default-branch push; read the live ref too."""
    branch = pr['base']['ref']
    require(isinstance(branch, str) and bool(branch))
    ref = api.get(api.prefix + 'git/ref/heads/' + quote(branch, safe='/'))
    require(ref['ref'] == 'refs/heads/' + branch and ref['object']['type'] == 'commit')
    require(ref['object']['sha'] == candidate['base_sha'])


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


def collect_native(api: GitHub, pr: dict[str, Any], approved_root: Path) -> dict[str, Any]:
    """Collect the latest run/attempt, never fall back to an older green result.

    approved_root must be the trusted base checkout. Reading candidate content
    through the API does not execute it. The publisher must repeat the candidate
    and run checks before publication; this snapshot alone grants no acceptance.
    """
    candidate = candidate_identity(pr, api.repository)
    root_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=approved_root, text=True).strip()
    require(root_sha == candidate['base_sha'])
    require_current_base(api, pr, candidate)
    require(not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'],
                                        cwd=approved_root, text=True).strip())
    policy_digest = hashlib.sha256((approved_root / 'scripts/review_tooling.py').read_bytes()).hexdigest()
    # Receipt hashes are claims, not proof of the actual executed source.
    policy_inputs, verifier_approval = approved_inputs(api, candidate, verification_inputs(approved_root))
    # The receipt describes the selector that ran in the candidate job. Its
    # approved bytes may differ from main, but cannot select acceptance lanes:
    # collect_acceptance continues to use this approved-base process's policy.
    execution_policy_digest = policy_inputs['scripts/review_tooling.py']
    producer_digest = policy_inputs['scripts/ci_evidence.py']
    hazard_policy = {'catalogue': (approved_root / 'tests/hazards/critical.json').read_bytes(),
                     'runner_sha256': policy_inputs['scripts/check_hazard_sensitivity.py']}
    runs = paginate(api.get, api.prefix + f"actions/runs?event=pull_request&head_sha={candidate['head_sha']}", 'workflow_runs')
    workflows = {p.workflow for p in PRODUCERS}
    latest = {}
    for workflow in workflows:
        matches = [r for r in runs if r['path'] == '.github/workflows/' + workflow]
        if matches:
            latest[workflow] = max(matches, key=lambda r: r['id'])
    native = []
    snapshots = {}
    producers: tuple[NativeProducer, ...] = PRODUCERS
    for workflow, run in latest.items():
        run = api.get(api.prefix + f"actions/runs/{run['id']}")
        jobs = paginate(api.get, api.prefix + f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", 'jobs')
        artifacts = paginate(api.get, api.prefix + f"actions/runs/{run['id']}/artifacts", 'artifacts')
        snapshots[workflow] = {'run_id': run['id'], 'run_attempt': run['run_attempt'], 'status': run['status']}
        source = api.blob(candidate['execution_sha'], '.github/workflows/' + workflow)
        workflow_digest = policy_inputs['.github/workflows/' + workflow]
        if workflow == 'ci.yml':
            producers = tuple(producer for producer in producers if producer.lane != 'unit_tests') + unit_producers(source, workflow_digest)
        commit = api.get(api.prefix + 'commits/' + candidate['execution_sha'])
        for producer in (definition for definition in producers if definition.workflow == workflow):
            record: dict[str, Any] = {'lane': producer.lane, 'job': producer.job, 'status': 'missing'}
            try:
                selected_jobs = [j for j in jobs if j['name'] == producer.job]
                if not selected_jobs:
                    native.append(record)
                    continue
                require(len(selected_jobs) == 1)
                job = selected_jobs[0]
                if job['status'] == 'completed' and job['conclusion'] in {'failure', 'cancelled', 'skipped', 'timed_out'}:
                    record['status'] = job['conclusion']
                    native.append(record)
                    continue
                selected_artifacts = [a for a in artifacts if a['name'] ==
                                      f"evidence-{producer.artifact}-attempt-{run['run_attempt']}"]
                if not selected_artifacts:
                    native.append(record)
                    continue
                require(len(selected_artifacts) == 1)
                artifact = selected_artifacts[0]
                require(type(artifact['id']) is int and 0 < artifact['size_in_bytes'] <= LIMIT)
                archive = api.raw(api.prefix + f"actions/artifacts/{artifact['id']}/zip")
                report = object_json(artifact_members(archive, artifact['digest'])['evidence.json'])
                tested_sha = report['execution_sha']
                require(isinstance(tested_sha, str) and re.fullmatch('[a-f0-9]{40}', tested_sha) is not None)
                tested_commit = (commit if tested_sha == candidate['execution_sha'] else
                                 api.get(api.prefix + 'commits/' + tested_sha))
                result = normalize_native(candidate=candidate, producer=producer, run=run, job=job,
                                          artifact=artifact, execution_commit=tested_commit,
                                          candidate_commit=commit, archive=archive,
                                          policy_digest=execution_policy_digest, producer_digest=producer_digest,
                                          workflow_digest=workflow_digest, workflow_source=source,
                                          hazard_policy=hazard_policy,
                                          unit_policy={path: policy_inputs[path] for path in UNIT_INPUTS})
                record.update(result)
            except (KeyError, TypeError, ValueError, OSError):
                record['status'] = 'unverified'
            native.append(record)
    statuses = {}
    for lane in {producer.lane for producer in producers}:
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
        # An absent workflow, shard, or artifact stays missing in the existing
        # taxonomy: do not turn it into a reported execution failure.
    return {'candidate': candidate, 'native': native, 'lane_statuses': statuses, 'runs': snapshots,
            'policy_sha256': policy_digest, 'producer_sha256': producer_digest, 'policy_inputs': policy_inputs,
            'verifier_approval': verifier_approval}


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
    result = summary.to_dict()
    result['native'] = [{key: value for key, value in record.items() if key != 'results'}
                        for record in native['native']]
    result['runs'] = native['runs']
    result['policy_sha256'] = native['policy_sha256']
    result['policy_inputs'] = native['policy_inputs']
    result['verifier_approval'] = native.get('verifier_approval')
    result['markdown_report'] = summary.markdown_report
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
    require_current_base(api, pr, candidate)


def publish_acceptance(api: GitHub, number: int, approved_root: Path, *,
                       template_directory: Path | None = None, publisher_run_id: int | None = None,
                       post_templates: bool = False) -> dict[str, Any]:
    """Publish a fresh pending check before collecting; only a rechecked pass goes green.

    Authentication determines the check's App source. Ordinary Actions tokens
    provide an advisory result; required enforcement needs the maintainer's
    dedicated App and repository rules, as documented in agent-workflow.md.
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
        if not exc.approval_allowed:
            guidance = ('Hazard-catalogue changes cannot use a verifier implementation '
                        'decision. They require a separately reviewed catalogue change; no obligations are waived.')
        result = {'all_prerequisites_met': False, 'verification_status': 'incomplete',
                  'error': ('verifier_update_requires_maintainer_decision' if exc.approval_allowed
                            else 'verifier_policy_change_not_supported'),
                  'markdown_report': ('Verifier files differ from approved main: ' + ', '.join(exc.paths)
                                      + '.\n\n' + guidance)}
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile):
        result = {'all_prerequisites_met': False, 'verification_status': 'incomplete',
                  'error': 'acceptance_evidence_unavailable_or_changed'}
    ready = result['all_prerequisites_met']
    # The report contains generated lane/status text; no artifact commands run.
    summary = result.get('markdown_report', 'Current evidence is unavailable or changed; acceptance is not ready.')
    summary = summary[:55000] + template_note
    api.write(api.prefix + f"check-runs/{check['id']}", {
        'status': 'completed', 'conclusion': 'success' if ready else 'failure',
        'output': {'title': 'Technical verification passed' if ready else 'Technical verification incomplete',
                   'summary': summary[:60000]}}, method='PATCH')
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
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--review-template', action='store_true',
                        help='print current identity and unset human review fields; never submit')
    parser.add_argument('--review-templates-dir', type=Path)
    parser.add_argument('--post-review-templates', action='store_true')
    parser.add_argument('--publisher-run-id', type=int)
    parser.add_argument('--approved-root', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if args.review_template and (args.publish or args.all_open):
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
        numbers = ([args.pr] if args.pr is not None else
                   [pr['number'] for pr in paginate(api.get, api.prefix + 'pulls?state=open')])
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
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile):
        # Never print API errors, remote bodies or authentication diagnostics.
        print(json.dumps({'schema_version': 2, 'all_prerequisites_met': False,
                          'verification_status': 'incomplete',
                          'error': 'acceptance_evidence_unavailable_or_changed'}))
        return 1
    print(json.dumps(results if args.all_open else results[0], sort_keys=True))
    # Bulk publisher health is separate from each PR's technical result.
    # API/publication errors above still exit nonzero; red candidate checks
    # remain red without failing an unrelated PR's aggregate workflow job.
    if args.all_open and args.publish:
        return 0
    return 0 if all(result['all_prerequisites_met'] for result in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
