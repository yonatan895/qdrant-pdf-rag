"""CI-only egress probe: attempt real TCP connections and judge the outcome.

usage: egress_probe.py MODE HOST:PORT [HOST:PORT ...]

MODE blocked        every target must FAIL to connect (name resolution
                    failure, refusal, unreachable or timeout all count)
     reachable      every target must connect
     reachable-any  at least one target must connect

The probe opens genuine sockets; it never mocks the network. The same file is
run on the runner and, through `kubectl exec -i ... python3 - MODE TARGET`,
inside pods, so a blocked result from a pod proves the pod's own egress path.
Output names only the target and an error class, never payloads or secrets.
Exit status: 0 expectation met, 1 expectation violated, 2 usage error.
"""
import errno
import socket
import sys

TIMEOUT_S = 5.0
MODES = ('blocked', 'reachable', 'reachable-any')


def parse_target(text):
    host, sep, port = text.rpartition(':')
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f'invalid target (want HOST:PORT): {text!r}')
    return host, int(port)


def attempt(host, port, timeout=TIMEOUT_S):
    """Return (connected, detail); detail is an error class when not connected."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, 'connected'
    except socket.gaierror:
        return False, 'name-resolution-failed'
    except TimeoutError:
        return False, 'timeout'
    except OSError as exc:
        return False, errno.errorcode.get(exc.errno, 'oserror')


def main(argv):
    if len(argv) < 2 or argv[0] not in MODES:
        print('usage: egress_probe.py {blocked|reachable|reachable-any} HOST:PORT ...', file=sys.stderr)
        return 2
    mode = argv[0]
    try:
        targets = [parse_target(text) for text in argv[1:]]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    results = []
    for host, port in targets:
        connected, detail = attempt(host, port)
        results.append(connected)
        print(f'{"REACHABLE" if connected else "unreachable"} {host}:{port} ({detail})')
    if mode == 'blocked':
        ok = not any(results)
    elif mode == 'reachable':
        ok = all(results)
    else:
        ok = any(results)
    print(f'egress-probe {mode}: {"ok" if ok else "VIOLATED"}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
