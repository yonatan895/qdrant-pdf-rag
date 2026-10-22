"""CI-only runner-side judgements for the rerank rehearsal (issue #815).

Inputs are small JSON observations written by rerank_contracts.py and the
stand-in's request counters: chunk ids, scores and counts, never passage text
or credentials. Output is a fixed one-line verdict per check.

    rerank_checks.py delta BEFORE AFTER route.kind=N ...
    rerank_checks.py compare ON OFF
    rerank_checks.py same-order A B
"""
import json
import sys

BATCH_SIZE = 32  # RERANK_BATCH_SIZE default; one request carries every candidate only up to here


class CheckFailed(Exception):
    pass


def load(path):
    with open(path) as handle:
        return json.load(handle)


def delta(before, after, expectations):
    """Each expectation is route.kind=N: requests the stand-in saw between the snapshots."""
    for item in expectations:
        name, _, wanted = item.partition('=')
        route, _, kind = name.partition('.')
        got = after[route][kind] - before[route][kind]
        if got != int(wanted):
            raise CheckFailed(f'{name}: expected {wanted} request(s), stand-in saw {got}')


def _hits(observation, label):
    hits = observation['hits']
    if not 2 <= len(hits) <= BATCH_SIZE:
        raise CheckFailed(f'{label}: need 2..{BATCH_SIZE} scoped candidates in one batch, got {len(hits)}')
    return hits


def compare(on, off):
    """Rerank on versus off for the same scoped query. The stand-in scores candidate i of n
    as (i+1)/n, so the only explanation for the 'on' order is the scores it returned."""
    if on['scope'] != off['scope'] or on['query'] != off['query']:
        raise CheckFailed('on/off observations are not the same scoped query')
    if on['query_kind'] != 'nl' or off['query_kind'] != 'nl':
        raise CheckFailed('query bypasses rerank (identifier or trap); not a rerank rehearsal')
    on_hits, off_hits = _hits(on, 'rerank on'), _hits(off, 'rerank off')
    if any(hit['rerank_score'] is not None for hit in off_hits):
        raise CheckFailed('rerank off but hits carry rerank_score')
    if any(hit['rerank_score'] is None for hit in on_hits):
        raise CheckFailed('rerank on but a hit has no rerank_score')
    on_ids, off_ids = [h['chunk_id'] for h in on_hits], [h['chunk_id'] for h in off_hits]
    if sorted(on_ids) != sorted(off_ids):
        raise CheckFailed('rerank on/off returned different candidate sets')
    off_score = {h['chunk_id']: h['score'] for h in off_hits}
    if any(abs(h['score'] - off_score[h['chunk_id']]) > 1e-9 for h in on_hits):
        raise CheckFailed('fusion scores differ between on and off')
    count = len(on_hits)
    got = sorted(h['rerank_score'] for h in on_hits)
    if any(abs(g - (i + 1) / count) > 1e-5 for i, g in enumerate(got)):
        raise CheckFailed("rerank_score values are not the stand-in's positional scores")
    for a in on_hits:
        for b in on_hits:
            if a['score'] > b['score'] and not a['rerank_score'] < b['rerank_score']:
                raise CheckFailed('rerank scores do not invert the fusion order the stand-in was handed')
    if on_ids == off_ids:
        raise CheckFailed('rerank on and off returned the same order')


def same_order(a, b):
    ids = [[h['chunk_id'] for h in obs['hits']] for obs in (a, b)]
    scores = [[h['rerank_score'] for h in obs['hits']] for obs in (a, b)]
    if ids[0] != ids[1] or scores[0] != scores[1]:
        raise CheckFailed('fallback leg returned a different order or scores than the primary leg')


def main(argv):
    try:
        if len(argv) >= 3 and argv[0] == 'delta':
            delta(load(argv[1]), load(argv[2]), argv[3:])
        elif len(argv) == 3 and argv[0] == 'compare':
            compare(load(argv[1]), load(argv[2]))
        elif len(argv) == 3 and argv[0] == 'same-order':
            same_order(load(argv[1]), load(argv[2]))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (CheckFailed, KeyError, TypeError, ValueError, OSError) as exc:
        print(f'RERANK CHECK FAILED: {argv[0]}: {exc if isinstance(exc, CheckFailed) else type(exc).__name__}')
        return 1
    print(f'RERANK CHECK PASSED: {argv[0]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
