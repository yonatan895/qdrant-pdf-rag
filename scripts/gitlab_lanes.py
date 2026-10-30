#!/usr/bin/env python3
"""Write the GitLab child pipeline for a branch pipeline's selected lanes (GH#1118 F11).

The `select` job in .gitlab-ci.yml runs `scripts/review_tooling.py profile`,
the fail-closed classifier GitHub's `select` job runs, and passes its manifest
here. The child pipeline includes .gitlab-ci.yml and replaces the rules of
hygiene and of each selected lane job, so those jobs, and only those, run in
it, with their definitions unchanged. Standard library only: it runs before
any install.

Fail closed: an unreadable manifest is an error (the job fails); an unknown
lane name, or a change to a GitLab-only pipeline input below, selects every
lane. Lanes with no GitLab job are reported, never silently counted as run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))  # direct script entry imports the classifier's lane list

from scripts.review_tooling import ALL_KNOWN_LANES

# review_tooling lane -> the .gitlab-ci.yml job that produces it.
LANE_JOBS = {
    'context_check': 'hygiene',
    'lint_and_types': 'lint',
    'unit_tests': 'test',
    'hazards': 'hazards',
    'simulation': 'sim',
    'gate_l1': 'gate-l1',
}
# Hygiene also refuses committed binaries: every child pipeline runs it.
ALWAYS = ('hygiene',)
# GitLab-only inputs the classifier counts as tooling. They change how the
# GitLab lanes themselves run, so a change selects every lane.
PIPELINE_INPUTS = frozenset({'.gitlab-ci.yml', 'scripts/gitlab_lanes.py', 'scripts/check_junit_clean.py'})
CHILD_RULE = {'if': '$CI_PIPELINE_SOURCE == "parent_pipeline"'}


def selected_jobs(manifest: dict) -> tuple[list[str], list[str], str]:
    """(jobs to run, lanes with no GitLab job, reason) for one classifier manifest."""
    lanes = manifest.get('required_lanes')
    paths = manifest.get('changed_paths')
    if not (isinstance(lanes, list) and all(isinstance(lane, str) for lane in lanes)
            and isinstance(paths, list) and all(isinstance(path, str) for path in paths)):
        raise ValueError('manifest has no lane or path list')
    every = sorted(set(LANE_JOBS.values()))
    no_job = sorted(lane for lane in lanes if lane not in LANE_JOBS)
    unknown = sorted(set(lanes) - set(ALL_KNOWN_LANES))
    if unknown:
        return every, no_job, 'unknown lane ' + ', '.join(unknown)
    touched = sorted(PIPELINE_INPUTS.intersection(paths))
    if touched:
        return every, no_job, 'pipeline input changed: ' + ', '.join(touched)
    jobs = sorted(set(ALWAYS) | {LANE_JOBS[lane] for lane in lanes if lane in LANE_JOBS})
    return jobs, no_job, 'classifier profile ' + str(manifest.get('profile', 'unknown'))


def child_config(jobs: list[str]) -> dict:
    config: dict = {'include': [{'local': '.gitlab-ci.yml'}]}
    for job in jobs:
        config[job] = {'rules': [dict(CHILD_RULE)]}
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        if not isinstance(manifest, dict):
            raise TypeError('manifest is not an object')
        jobs, no_job, reason = selected_jobs(manifest)
    except (OSError, ValueError, TypeError) as error:
        print(f'FAIL: cannot select lanes: {error}', file=sys.stderr)
        return 2
    # JSON is YAML: GitLab reads the child configuration from this artifact.
    args.out.write_text(json.dumps(child_config(jobs), indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'lanes ({reason}): ' + ' '.join(jobs))
    if no_job:
        print('NOT RUN (no GitLab job): ' + ' '.join(no_job))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
