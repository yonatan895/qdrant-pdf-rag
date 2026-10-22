"""CI-only view of the rerank stand-in behind the gateway's Service (issue #815).

Run in the test-gateway pod via stdin: that pod already reaches the stand-in
Service, and the application pod is not asked to. `stats` prints the request
counters; `wait <fault>` returns once the stand-in reports that fault state.
Counters hold request counts and the fault name only, never request content.
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = 'http://vllm-mock:8000'
FAULTS = ('healthy', 'score_unavailable', 'unavailable')
STATE_TIMEOUT_S = 60
REQUEST_TIMEOUT_S = 5
POLL_S = 1


def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


def wait_for_fault(fault):
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while True:
        try:
            if fetch('/healthz').get('rerank_fault') == fault:
                return
        except (urllib.error.URLError, TimeoutError):
            pass  # the Recreate rollout may still be replacing the pod
        if time.monotonic() >= deadline:
            raise TimeoutError('rerank stand-in did not converge to the requested fault state')
        time.sleep(POLL_S)


def main(argv):
    if argv == ['stats']:
        print(json.dumps(fetch('/stats'), sort_keys=True))
    elif len(argv) == 2 and argv[0] == 'wait' and argv[1] in FAULTS:
        wait_for_fault(argv[1])
        print('RERANK MOCK STATE READY ' + argv[1])
    else:
        print('usage: stats | wait <healthy|score_unavailable|unavailable>', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
