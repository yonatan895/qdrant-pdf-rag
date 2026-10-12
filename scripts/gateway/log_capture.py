"""Byte-only sink for the native local gateway's private bounded host log.

The shell still owns Docker, its status and cleanup. This sink starts no process
or service and keeps draining after saturation or a file error. No line or
upstream exception text is interpreted or copied into operational diagnostics.
"""
from __future__ import annotations

import os
import stat
import sys


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        maximum = int(sys.argv[2])
        if not 1 <= maximum <= 64 * 1024 * 1024:
            return 2
    except ValueError:
        return 2
    output = None
    try:
        descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            os.close(descriptor)
            raise OSError
        output = os.fdopen(descriptor, "wb", buffering=0)
    except OSError:
        # Even initial file failure must drain the pipe before returning the
        # failure to its owner, rather than detaching a still-running daemon.
        pass

    marker = b"\n[gateway log truncated]\n"
    retained = 0
    truncated, failed = False, output is None
    try:
        while chunk := os.read(sys.stdin.fileno(), 65536):
            if output is None or failed:
                continue
            try:
                selected = chunk[:max(0, maximum - retained)]
                if selected:
                    if output.write(selected) != len(selected):
                        raise OSError
                    retained += len(selected)
                if len(selected) != len(chunk) and not truncated:
                    truncated = True
                    suffix = marker[-maximum:]
                    output.seek(maximum - len(suffix))
                    if output.write(suffix) != len(suffix):
                        raise OSError
            except OSError:
                # Continue draining: a full/broken log cannot hold the Docker
                # client or conceal its exit. The owner receives nonzero status.
                failed = True
    except OSError:
        failed = True
    finally:
        if output is not None:
            try:
                output.close()
            except OSError:
                failed = True
    if truncated:
        print("Gateway host log reached its byte limit; excess output was discarded.", file=sys.stderr)
    if failed:
        print("Gateway host log capture failed.", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
