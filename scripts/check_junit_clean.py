#!/usr/bin/env python3
"""Fail unless a pytest JUnit report shows executed tests and no skip/xfail/failure.

Offline GitLab gate (issue #370): the exit status of pytest alone lets skipped
and xfail-laden runs pass silently. This reuses the GitHub evidence producer's
counting rule (`ci_evidence.junit_bytes`: pytest writes xfail as <skipped>), so
both CIs reject the same reports. It never grants acceptance by itself.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ci_evidence import junit_bytes


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print('usage: check_junit_clean.py JUNIT_XML', file=sys.stderr)
        return 2
    try:
        counts = junit_bytes(Path(argv[0]).read_bytes())
    except (OSError, ValueError, ET.ParseError):
        print('FAIL: test report missing, unreadable or empty', file=sys.stderr)
        return 1
    bad = {key: counts[key] for key in ('failed', 'errors', 'skipped') if counts[key]}
    if bad:
        print(f'FAIL: executed={counts["executed"]} but {bad}; skips and xfails are not '
              'passes in this lane', file=sys.stderr)
        return 1
    print(f'ok: {counts["executed"]} tests executed, none skipped, xfailed or failing')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
