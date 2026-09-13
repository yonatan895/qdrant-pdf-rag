"""CI-only confirmation of the computation state behind the gateway's Service."""
import argparse
import json
import time
import urllib.error
import urllib.request

STATE_TIMEOUT_S = 60
REQUEST_TIMEOUT_S = 5
POLL_S = 1


def wait_for_state(expected):
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('mock Service did not converge to the requested state')
        try:
            with urllib.request.urlopen('http://vllm-mock:8000/healthz',
                                        timeout=min(REQUEST_TIMEOUT_S, remaining)) as response:
                state = json.load(response)
            if (isinstance(state, dict) and state.get('status') == 'ok'
                    and all(state.get(key) == value for key, value in expected.items())):
                return
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason, (ConnectionRefusedError, TimeoutError)):
                raise
        except TimeoutError:
            pass
        time.sleep(min(POLL_S, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('chat_fault', choices=['healthy', 'upstream', 'malformed', 'truncated'])
    parser.add_argument('embed_fault', choices=['healthy', 'upstream', 'malformed', 'dimension'])
    parser.add_argument('ttft_ms', type=float)
    args = parser.parse_args()
    if not 0 <= args.ttft_ms < float('inf'):
        parser.error('ttft_ms must be finite and nonnegative')
    expected = vars(args)
    wait_for_state(expected)
    print('MOCK STATE READY ' + json.dumps(expected, sort_keys=True))


if __name__ == '__main__':
    main()
