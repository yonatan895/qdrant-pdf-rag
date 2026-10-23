"""Exact-candidate verifier decisions recorded by the human-only main workflow.

Only API data is read here. Candidate code is never imported or executed.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.acceptance_evidence import (
    LIMIT,
    artifact_members,
    object_json,
    paginate,
    require,
    require_same_merge,
)

WORKFLOW = 'verifier-update.yml'
MAINTAINER = 'yonatan895'
# Selector bytes may be trusted as receipt provenance after exact human
# approval. The consumer still selects obligations with approved-main code.
# Implementation decisions cannot change the challenge oracle. Anchor-only
# decisions have a separate namespace and retain every approved obligation.
CATALOGUE = 'tests/hazards/critical.json'
FIXED_POLICY = {CATALOGUE}
EVENTS = ('workflow_dispatch', 'issue_comment')
# Grammar of the PR comment decision. Approvals must name the head the maintainer
# reviewed; revocations need no SHA. Anything else is refused, never guessed.
COMMAND = re.compile(r'/verifier (?:(?P<approve>approve) (?P<sha>[0-9a-f]{7,40})|(?P<revoke>revoke))'
                     r'(?: (?P<kind>catalogue-anchors|implementation))?')


class VerifierApprovalRequired(ValueError):
    def __init__(self, paths: list[str], *, kind: str = 'implementation'):
        self.paths = sorted(paths)
        self.approval_allowed = not bool(set(paths) & FIXED_POLICY)
        self.kind = kind
        super().__init__('Changed verifier inputs require a current maintainer decision')


def _canonical(value: Any) -> str:
    # Typed JSON comparison distinguishes bool/int/float while preserving list
    # order. Object key formatting/order is not a catalogue obligation.
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def allowed_anchor_change(approved_bytes: bytes, candidate_bytes: bytes) -> dict:
    """Bind a real anchor edit without changing any other typed catalogue field."""
    documents = []
    for raw in (approved_bytes, candidate_bytes):
        require(type(raw) is bytes)
        document = object_json(raw)
        require(type(document.get('schema_version')) is int and document['schema_version'] == 1)
        hazards = document.get('hazards')
        require(isinstance(hazards, list) and bool(hazards))
        assert isinstance(hazards, list)
        identities = set()
        for hazard in hazards:
            require(isinstance(hazard, dict))
            identity = hazard.get('id')
            require(isinstance(identity, str) and bool(identity) and identity not in identities)
            identities.add(identity)
            before, after = hazard.get('before'), hazard.get('after')
            require(isinstance(before, str) and bool(before) and isinstance(after, str) and bool(after)
                    and before != after)
        documents.append(document)
    approved, candidate = documents
    require(len(approved['hazards']) == len(candidate['hazards']))
    changed = []
    for old, new in zip(approved['hazards'], candidate['hazards'], strict=True):
        if any(old[key] != new[key] for key in ('before', 'after')):
            changed.append(old['id'])
        for hazard in (old, new):
            hazard['before'] = hazard['after'] = None
    require(bool(changed) and _canonical(approved) == _canonical(candidate))
    return {'base_sha256': hashlib.sha256(approved_bytes).hexdigest(),
            'candidate_sha256': hashlib.sha256(candidate_bytes).hexdigest(), 'changed_ids': changed}


def _namespace(kind: str) -> tuple[str, str]:
    require(kind in {'implementation', 'catalogue-anchors'})
    return (('Verifier', 'verifier-decision') if kind == 'implementation'
            else ('Catalogue anchors', 'catalogue-anchor-decision'))


def latest_decision(api: Any, candidate: dict, *, kind: str = 'implementation') -> dict | None:
    title, _ = _namespace(kind)
    runs: dict[int, dict] = {}
    for event in EVENTS:
        for item in paginate(api.get, api.prefix + 'actions/workflows/' + WORKFLOW + f'/runs?event={event}',
                             'workflow_runs'):
            runs[item['id']] = item
    titles = {f"{title} PR {candidate['number']}: {decision}" for decision in ('approve', 'revoke')}
    # A comment run by anyone else is a skipped no-op and must not shadow the
    # maintainer's decision; the decision itself is validated again below.
    matches = [r for r in runs.values() if r.get('display_title') in titles
               and (r.get('event') != 'issue_comment' or (r.get('actor') or {}).get('login') == MAINTAINER)]
    if not matches:
        return None
    # A newer pending, failed, cancelled or revoked decision never falls back.
    listed = max(matches, key=lambda r: r['id'])
    run = api.get(api.prefix + f"actions/runs/{listed['id']}")
    require(run['id'] == listed['id'] and run['display_title'] in titles)
    return run


def _bound_candidate(api: Any, candidate: dict, recorded: str) -> dict:
    """The candidate a decision may name: the current merge or its content twin.

    GitHub can regenerate an unchanged PR's test merge with a new SHA (#753).
    The receipt rule applies: exact current base/head parents and an identical
    tree, read from independently fetched commit records. A new base or head
    changes the parents, so it still needs a new decision.
    """
    if recorded == candidate['execution_sha']:
        return candidate
    require(isinstance(recorded, str) and re.fullmatch('[a-f0-9]{40}', recorded) is not None)
    current = api.get(api.prefix + 'commits/' + candidate['execution_sha'])
    other = api.get(api.prefix + 'commits/' + recorded)
    require(other['sha'] == recorded)
    require_same_merge(candidate, current, other)
    return {**candidate, 'execution_sha': recorded}


def parse_command(body: Any) -> tuple[str, str, str | None]:
    """Strictly parse a decision comment into (decision, kind, head sha prefix)."""
    require(isinstance(body, str))
    match = COMMAND.fullmatch(body.strip())
    require(match is not None)
    assert match is not None
    decision = 'approve' if match['approve'] else 'revoke'
    return decision, match['kind'] or 'implementation', match['sha']


def _require_maintainer_comment(api: Any, number: int, comment_id: int) -> dict:
    """Re-read the comment: maintainer-authored, OWNER, on this PR, and unedited."""
    comment = api.get(api.prefix + f'issues/comments/{comment_id}')
    require(comment['id'] == comment_id and comment['user']['login'] == MAINTAINER
            and comment['user']['type'] == 'User' and comment['author_association'] == 'OWNER'
            and comment['issue_url'].endswith('/' + api.prefix + f'issues/{number}')
            and comment['created_at'] == comment['updated_at'])
    return comment


def _comment_binding(api: Any, candidate: dict, binding: Any, run: dict, kind: str) -> dict:
    """Validate the recorded comment against the live comment and current head."""
    require(isinstance(binding, dict) and set(binding) == {'id', 'created_at', 'head_sha'}
            and type(binding['id']) is int and binding['id'] > 0)
    live = _require_maintainer_comment(api, candidate['number'], binding['id'])
    decision, command_kind, prefix = parse_command(live['body'])
    require(decision == 'approve' and command_kind == kind and prefix is not None
            and binding['head_sha'].startswith(prefix) and binding['head_sha'] == candidate['head_sha']
            and live['created_at'] == binding['created_at'])
    require(datetime.fromisoformat(binding['created_at']) <= datetime.fromisoformat(run['created_at']))
    return binding


def validated_decision(api: Any, candidate: dict, inputs: dict[str, str], *,
                       kind: str = 'implementation', catalogue: dict | None = None) -> dict:
    title, artifact_name = _namespace(kind)
    require((kind == 'implementation' and catalogue is None)
            or (kind == 'catalogue-anchors' and isinstance(catalogue, dict)))
    if kind == 'catalogue-anchors':
        assert catalogue is not None
        require(inputs == {CATALOGUE: catalogue['candidate_sha256']})
    run = latest_decision(api, candidate, kind=kind)
    require(run is not None)
    assert run is not None
    require(run['event'] in EVENTS and run['path'] == '.github/workflows/' + WORKFLOW)
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
    selected = [a for a in artifacts if a['name'] == f"{artifact_name}-{run['run_attempt']}"]
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
    require(updated.tzinfo is not None and dispatched.tzinfo is not None)
    comment = None
    if run['event'] == 'workflow_dispatch':
        require(updated < dispatched)
    else:
        # The comment itself bumps the PR's updated_at, so the exact head named in
        # the maintainer's comment (checked against the live comment) is the binding.
        comment = _comment_binding(api, candidate, record.get('comment'), run, kind)
    bound = _bound_candidate(api, candidate, record['candidate']['execution_sha'])
    expected = {'schema_version': 1, 'candidate': bound, 'inputs': inputs,
                'pr_updated_at': record['pr_updated_at'],
                'decision': 'approve', 'run_id': run['id'], 'run_attempt': run['run_attempt']}
    if comment is not None:
        expected['comment'] = comment
    identity = {'run_id': run['id'], 'run_attempt': run['run_attempt'], 'inputs': inputs}
    if kind == 'catalogue-anchors':
        expected.update(schema_version=2, kind=kind, catalogue=catalogue)
        identity.update(kind=kind, catalogue=catalogue)
    require(_canonical(record) == _canonical(expected) and type(record.get('schema_version')) is int)
    require(run['display_title'] == f"{title} PR {candidate['number']}: approve")
    return identity


def approved_catalogue(api: Any, candidate: dict, approved_bytes: bytes) -> tuple[bytes, dict | None]:
    """Read candidate bytes as data, then require independent exact anchor trust."""
    observed = api.blob(candidate['execution_sha'], CATALOGUE)
    if observed == approved_bytes:
        return approved_bytes, None
    try:
        catalogue = allowed_anchor_change(approved_bytes, observed)
    except (KeyError, TypeError, ValueError):
        # Structural catalogue changes remain outside either approval path.
        raise VerifierApprovalRequired([CATALOGUE]) from None
    try:
        approval = validated_decision(api, candidate, {CATALOGUE: catalogue['candidate_sha256']},
                                      kind='catalogue-anchors', catalogue=catalogue)
    except (KeyError, TypeError, ValueError, OSError):
        raise VerifierApprovalRequired([CATALOGUE], kind='catalogue-anchors') from None
    return observed, approval


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


def record_decision(api: Any, number: int, decision: str, root: Path, run_id: int, attempt: int, *,
                    kind: str = 'implementation', comment: dict | None = None,
                    head_prefix: str | None = None) -> dict:
    import subprocess

    from scripts.acceptance import candidate_identity, require_current_base, verification_inputs

    _namespace(kind)
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
    if comment is not None:
        # Comment path: the approved head is the one the maintainer named, never
        # whatever the PR happens to be at job time.
        require(decision == 'revoke' or (head_prefix is not None
                                         and candidate['head_sha'].startswith(head_prefix)))
        comment = {**comment, 'head_sha': candidate['head_sha']}
    commit = api.get(api.prefix + 'commits/' + candidate['execution_sha'])
    require(commit['sha'] == candidate['execution_sha']
            and [p['sha'] for p in commit['parents']] == [candidate['base_sha'], candidate['head_sha']])
    catalogue = None
    if kind == 'catalogue-anchors':
        catalogue = allowed_anchor_change((root / CATALOGUE).read_bytes(),
                                         api.blob(candidate['execution_sha'], CATALOGUE))
        inputs = {CATALOGUE: catalogue['candidate_sha256']}
    else:
        inputs = {path: hashlib.sha256(api.blob(candidate['execution_sha'], path)).hexdigest()
                  for path in verification_inputs(root)}
    require(candidate_identity(api.get(api.prefix + f'pulls/{number}'), api.repository) == candidate)
    require_current_base(api, pr, candidate)
    record = {'schema_version': 1, 'candidate': candidate, 'inputs': inputs, 'decision': decision,
              'pr_updated_at': pr['updated_at'], 'run_id': run_id, 'run_attempt': attempt}
    if kind == 'catalogue-anchors':
        record.update(schema_version=2, kind=kind, catalogue=catalogue)
    if comment is not None:
        record['comment'] = comment
    return record


def comment_request(api: Any, event: dict) -> tuple[int, str, str, str | None, dict]:
    """Authorize a created PR comment event: (pr, decision, kind, sha prefix, comment)."""
    comment, issue = event['comment'], event['issue']
    require(event['action'] == 'created' and 'pull_request' in issue)
    require(comment['user']['login'] == MAINTAINER and comment['user']['type'] == 'User'
            and comment['author_association'] == 'OWNER' and event['sender']['login'] == MAINTAINER)
    require(type(issue['number']) is int and issue['number'] > 0 and type(comment['id']) is int)
    decision, kind, prefix = parse_command(comment['body'])
    live = _require_maintainer_comment(api, issue['number'], comment['id'])
    require(live['body'] == comment['body'])
    return (issue['number'], decision, kind, prefix,
            {'id': comment['id'], 'created_at': live['created_at']})


def _emit(**outputs: str) -> None:
    import os

    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a') as handle:
            for key, value in outputs.items():
                require(re.fullmatch('[a-z0-9-]*', value) is not None)
                handle.write(f'{key}={value}\n')


def main() -> None:
    import argparse
    import os

    from scripts.acceptance import GitHub

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pr')
    parser.add_argument('--decision', choices=('approve', 'revoke'))
    parser.add_argument('--kind', choices=('implementation', 'catalogue-anchors'), default='implementation')
    parser.add_argument('--from-comment-event', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(os.environ['GITHUB_REF'] == 'refs/heads/main'
            and os.environ['GITHUB_ACTOR'] == MAINTAINER
            and os.environ['GITHUB_TRIGGERING_ACTOR'] == MAINTAINER)
    api = GitHub(os.environ['GITHUB_REPOSITORY'])
    run_id, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    extra: dict[str, Any] = {}
    if args.from_comment_event:
        require(os.environ['GITHUB_EVENT_NAME'] == 'issue_comment' and args.pr is None and args.decision is None)
        # The comment text is read from the event file by this script only; it is
        # never expanded into a shell or an environment variable.
        number, decision, kind, prefix, comment = comment_request(
            api, json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()))
        extra = {'comment': comment, 'head_prefix': prefix}
    else:
        require(os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch')
        require(args.pr is not None and args.decision is not None
                and args.pr.isascii() and args.pr.isdecimal() and str(int(args.pr)) == args.pr and int(args.pr) > 0)
        number, decision, kind = int(args.pr), args.decision, args.kind
    record = record_decision(api, number, decision, Path.cwd(), run_id, attempt, kind=kind, **extra)
    require(not args.output.exists())
    args.output.write_text(json.dumps(record, sort_keys=True) + '\n')
    if args.from_comment_event:
        _emit(kind=kind, decision=decision, head_sha=record['candidate']['head_sha'])
    else:
        _emit(kind=kind)


if __name__ == '__main__':
    main()
