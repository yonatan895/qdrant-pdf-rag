"""Exact-candidate verifier decisions recorded by the human-only main workflow.

Only API data is read here. Candidate code is never imported or executed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.acceptance_evidence import LIMIT, artifact_members, object_json, paginate, require

WORKFLOW = 'verifier-update.yml'
MAINTAINER = 'yonatan895'
FIXED_POLICY = {'scripts/review_tooling.py', 'tests/hazards/critical.json'}


class VerifierApprovalRequired(ValueError):
    def __init__(self, paths: list[str]):
        self.paths = sorted(paths)
        self.approval_allowed = not bool(set(paths) & FIXED_POLICY)
        super().__init__('Changed verifier inputs require a current maintainer decision')


def latest_decision(api: Any, candidate: dict) -> dict | None:
    runs = paginate(api.get, api.prefix + 'actions/workflows/' + WORKFLOW + '/runs?event=workflow_dispatch',
                    'workflow_runs')
    titles = {f"Verifier PR {candidate['number']}: {decision}" for decision in ('approve', 'revoke')}
    matches = [r for r in runs if r.get('display_title') in titles]
    if not matches:
        return None
    # A newer pending, failed, cancelled or revoked decision never falls back.
    listed = max(matches, key=lambda r: r['id'])
    run = api.get(api.prefix + f"actions/runs/{listed['id']}")
    require(run['id'] == listed['id'] and run['display_title'] in titles)
    return run


def validated_decision(api: Any, candidate: dict, inputs: dict[str, str]) -> dict:
    run = latest_decision(api, candidate)
    require(run is not None)
    assert run is not None
    require(run['event'] == 'workflow_dispatch' and run['path'] == '.github/workflows/' + WORKFLOW)
    require(run['head_sha'] == candidate['base_sha'] and run['head_branch'] == 'main')
    require(run['repository']['id'] == candidate['repository_id']
            and run['repository']['full_name'] == candidate['repository'])
    require(run['head_repository']['id'] == candidate['repository_id'])
    require(candidate['head_repository_id'] == candidate['repository_id'])
    require(run['status'] == 'completed' and run['conclusion'] == 'success')
    for key in ('actor', 'triggering_actor'):
        require(run[key]['login'] == MAINTAINER and run[key]['type'] == 'User')
    jobs = paginate(api.get, api.prefix + f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", 'jobs')
    require(len(jobs) == 1 and jobs[0]['name'] == 'record-decision'
            and jobs[0]['conclusion'] == 'success' and jobs[0]['status'] == 'completed'
            and jobs[0]['run_id'] == run['id'] and jobs[0]['run_attempt'] == run['run_attempt']
            and jobs[0]['head_sha'] == candidate['base_sha'])
    artifacts = paginate(api.get, api.prefix + f"actions/runs/{run['id']}/artifacts", 'artifacts')
    selected = [a for a in artifacts if a['name'] == f"verifier-decision-{run['run_attempt']}"]
    require(len(selected) == 1)
    artifact = selected[0]
    require(artifact['expired'] is False and type(artifact['id']) is int
            and 0 < artifact['size_in_bytes'] <= LIMIT)
    binding = artifact['workflow_run']
    require(binding['id'] == run['id'] and binding['head_sha'] == candidate['base_sha']
            and binding['repository_id'] == candidate['repository_id']
            and binding['head_repository_id'] == candidate['repository_id'])
    files = artifact_members(api.raw(api.prefix + f"actions/artifacts/{artifact['id']}/zip"), artifact['digest'])
    require(set(files) == {'evidence.json'})
    record = object_json(files['evidence.json'])
    # Timestamp provenance comes from approved-main snapshot code and the API's
    # dispatch record. A queued run cannot grant trust to a subsequently changed
    # PR. Strict ordering rejects ambiguous same-second updates as well.
    updated = datetime.fromisoformat(record['pr_updated_at'])
    dispatched = datetime.fromisoformat(run['created_at'])
    require(updated.tzinfo is not None and dispatched.tzinfo is not None and updated < dispatched)
    expected = {'schema_version': 1, 'candidate': candidate, 'inputs': inputs,
                'pr_updated_at': record['pr_updated_at'],
                'decision': 'approve', 'run_id': run['id'], 'run_attempt': run['run_attempt']}
    require(record == expected and type(record.get('schema_version')) is int)
    require(run['display_title'] == f"Verifier PR {candidate['number']}: approve")
    return {'run_id': run['id'], 'run_attempt': run['run_attempt'], 'inputs': inputs}


def approved_inputs(api: Any, candidate: dict, expected: dict[str, str]) -> tuple[dict[str, str], dict | None]:
    observed = {path: hashlib.sha256(api.blob(candidate['execution_sha'], path)).hexdigest()
                for path in expected}
    changed = [path for path in expected if observed[path] != expected[path]]
    if not changed:
        return expected, None
    # Approval grants trust to implementation bytes, not fewer obligations.
    if set(changed) & FIXED_POLICY:
        raise VerifierApprovalRequired(changed)
    try:
        approval = validated_decision(api, candidate, observed)
    except (KeyError, TypeError, ValueError, OSError):
        raise VerifierApprovalRequired(changed) from None
    return observed, approval


def record_decision(api: Any, number: int, decision: str, root: Path, run_id: int, attempt: int) -> dict:
    import subprocess

    from scripts.acceptance import candidate_identity, require_current_base, verification_inputs

    require(type(number) is int and number > 0 and decision in {'approve', 'revoke'})
    require(run_id > 0 and attempt > 0)
    pr = api.get(api.prefix + f'pulls/{number}')
    candidate = candidate_identity(pr, api.repository)
    require(candidate['head_repository_id'] == candidate['repository_id'])
    require_current_base(api, pr, candidate)
    require(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            == candidate['base_sha'])
    require(not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'],
                                        cwd=root, text=True).strip())
    commit = api.get(api.prefix + 'commits/' + candidate['execution_sha'])
    require(commit['sha'] == candidate['execution_sha']
            and [p['sha'] for p in commit['parents']] == [candidate['base_sha'], candidate['head_sha']])
    inputs = {path: hashlib.sha256(api.blob(candidate['execution_sha'], path)).hexdigest()
              for path in verification_inputs(root)}
    require(candidate_identity(api.get(api.prefix + f'pulls/{number}'), api.repository) == candidate)
    require_current_base(api, pr, candidate)
    return {'schema_version': 1, 'candidate': candidate, 'inputs': inputs, 'decision': decision,
            'pr_updated_at': pr['updated_at'],
            'run_id': run_id, 'run_attempt': attempt}


def main() -> None:
    import argparse
    import os

    from scripts.acceptance import GitHub

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pr', required=True)
    parser.add_argument('--decision', choices=('approve', 'revoke'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(args.pr.isascii() and args.pr.isdecimal() and str(int(args.pr)) == args.pr and int(args.pr) > 0)
    require(os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch'
            and os.environ['GITHUB_REF'] == 'refs/heads/main'
            and os.environ['GITHUB_ACTOR'] == MAINTAINER
            and os.environ['GITHUB_TRIGGERING_ACTOR'] == MAINTAINER)
    record = record_decision(GitHub(os.environ['GITHUB_REPOSITORY']), int(args.pr), args.decision,
                             Path.cwd(), int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT']))
    require(not args.output.exists())
    args.output.write_text(json.dumps(record, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
