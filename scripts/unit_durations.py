"""Build tests/unit-durations.json from the raw JUnit of a green ci run's unit shards.

Usage: python scripts/unit_durations.py DIR_OR_TESTS_XML... > tests/unit-durations.json

Each argument is a tests.xml from an `evidence-unit-N-attempt-M` artifact or a
directory searched for them. Tests faster than MIN_MS are left out and weigh
`default_ms` in the balanced shard rule (docs/testing.md).
"""
from __future__ import annotations

import base64
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MIN_MS = 20
DEFAULT_MS = 5


def durations(paths: list[Path]) -> dict:
    files = [f for p in paths for f in (sorted(p.rglob('tests.xml')) if p.is_dir() else [p])]
    if not files:
        raise SystemExit('no tests.xml found')
    ms: dict[str, int] = {}
    for file in files:
        for case in ET.parse(file).iter('testcase'):
            props = [p for p in case.findall('./properties/property') if p.get('name') == 'native_nodeid']
            if len(props) != 1:
                raise SystemExit(f'{file}: testcase without exactly one native_nodeid')
            node = base64.b64decode(props[0].get('value', ''), validate=True).decode('utf-8')
            value = round(float(case.get('time', '0')) * 1000)
            if node in ms:
                raise SystemExit(f'{node}: appears in more than one shard JUnit')
            if value >= MIN_MS:
                ms[node] = value
            else:
                ms.setdefault(node, 0)
    kept = {node: value for node, value in sorted(ms.items()) if value >= MIN_MS}
    return {'schema_version': 1, 'default_ms': DEFAULT_MS, 'ms': kept}


if __name__ == '__main__':
    print(json.dumps(durations([Path(a) for a in sys.argv[1:]]), indent=0, sort_keys=True, ensure_ascii=True))
