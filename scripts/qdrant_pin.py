#!/usr/bin/env python3
"""Print the pinned Qdrant image from images.txt (single parser for the
simulation tier: pytest fixture and `sh scripts/tools/run-task.sh local:qdrant:up` both read this)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def qdrant_image_pin(images_txt: Path) -> str:
    """First qdrant line's name column — a pin bump is picked up automatically."""
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        fields = line.split()
        if fields and "qdrant" in fields[0]:
            return fields[0]
    raise ValueError(f"no qdrant image pin found in {images_txt}")


def qdrant_digest_pin(images_txt: Path) -> str:
    """Resolve the approved content pin without changing its human version tag."""
    rows = [line.split() for line in images_txt.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    rows = [row for row in rows if 'qdrant' in row[0]]
    if len(rows) != 1 or len(rows[0]) != 2 or not re.fullmatch(r'sha256:[a-f0-9]{64}', rows[0][1]):
        raise ValueError('one complete Qdrant image digest pin is required')
    return rows[0][0].rsplit(':', 1)[0] + '@' + rows[0][1]


def prepared_image(reference: str) -> str:
    """Inspect the selected daemon read-only; return immutable bytes for launch.

    A tag or image presence alone is not attestation. Inspect by the approved
    repository digest and require its daemon record, then launch the immutable
    image ID with --pull=never so later tag movement cannot substitute bytes.
    """
    if not re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}', reference):
        raise ValueError('prepared image inspection requires a repository digest')
    try:
        result = subprocess.run(['docker', 'image', 'inspect', reference],
                                capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            raise ValueError('approved Qdrant image is missing; run explicit artifacts:qdrant preparation')
        records = json.loads(result.stdout)
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError('invalid image inspection')
        record = records[0]
        image_id = record.get('Id', '')
        digests = record.get('RepoDigests') or []
        expected = reference.removeprefix('docker.io/')
        if (not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id)
                or not any(isinstance(d, str) and d.removeprefix('docker.io/') == expected for d in digests)):
            raise ValueError('local Qdrant image identity does not match the approved digest')
        return image_id
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise ValueError('approved Qdrant image inspection unavailable') from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--digest', action='store_true', help='print the approved repository digest')
    choice.add_argument('--prepared', action='store_true', help='verify the local image and print its immutable ID')
    choice.add_argument('--prepare', action='store_true', help='explicit connected pull of the approved digest')
    args = parser.parse_args()
    images_txt = Path(__file__).resolve().parents[1] / 'images.txt'
    try:
        if args.prepare or args.prepared or args.digest:
            reference = qdrant_digest_pin(images_txt)
            if args.prepare:
                result = subprocess.run(['docker', 'pull', reference], check=False, timeout=900,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode:
                    raise ValueError('explicit approved Qdrant image preparation failed')
            print(reference if args.digest else prepared_image(reference))
        else:
            print(qdrant_image_pin(images_txt))
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc) if isinstance(exc, ValueError) else 'Qdrant preparation unavailable', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
