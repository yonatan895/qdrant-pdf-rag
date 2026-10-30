#!/usr/bin/env python3
"""Dev kit key (#1118, ADR-0009 §8): one SHA-256 over the dev workspace image's inputs.

The key names the dev workspace image (`qdrant-pdf-rag-devkit:<key>`) and its kit
(`mfr-devkit-<key>`). It covers a fixed, ordered list of upstream-owned pin files plus
every file under images/devkit/ in sorted order, because the image copies them. Each
file contributes its repository path and its bytes, both length-prefixed, after a
versioned domain prefix, so no two different input sets frame to the same stream.

  python3 scripts/devkit_key.py           print the key
  python3 scripts/devkit_key.py --check   compare it with scripts/tools/devkit-pin.txt

A stale pin is refused only by kit packing and the release gate (D-8), never by
ordinary unit CI. Standard library only: agent_doctor will import it.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = 'scripts/tools/devkit-pin.txt'
DOMAIN = b'mfr-devkit-key/v1\n'
INPUTS = (
    'requirements.dev.lock.txt',
    'locks/cp314-linux-x86_64.json',
    'scripts/tools/task-pin.txt',
    'scripts/tools/helm-pin.txt',
    'scripts/tools/oc-pin.txt',
    'images/devkit-packages.txt',
    'images/Containerfile.devkit',
)
IMAGE_TREE = 'images/devkit'
KEY = re.compile(r'[0-9a-f]{64}')
DIGEST = re.compile(r'sha256:(?:PENDING|[0-9a-f]{64})')
PENDING = 'sha256:PENDING'


class DevkitKeyError(Exception):
    """A fixed, operator-facing refusal."""


def inputs(root: Path) -> list[str]:
    """The fixed inputs, then every file under images/devkit/ by sorted path."""
    tree = root / IMAGE_TREE
    if tree.is_symlink():
        raise DevkitKeyError(f'{IMAGE_TREE} must be a directory, not a symlink')
    extra: list[str] = []
    if tree.exists():
        for path in tree.rglob('*'):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise DevkitKeyError(f'{IMAGE_TREE} may hold only regular files and directories')
            if path.is_file():
                extra.append(path.relative_to(root).as_posix())
    return [*INPUTS, *sorted(extra)]


def key(root: Path = ROOT) -> str:
    digest = hashlib.sha256(DOMAIN)
    for relative in inputs(root):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise DevkitKeyError(f'kit key input is missing or not a regular file: {relative}')
        name = relative.encode('utf-8')
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, 'big'))
        digest.update(name)
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    return digest.hexdigest()


def read_pin(root: Path = ROOT) -> tuple[str, str]:
    """Return (key, digest) from devkit-pin.txt; refuse any other shape."""
    malformed = DevkitKeyError(
        f'{PIN} is missing or malformed: it needs exactly one "key: <64 hex>" line and one '
        f'"digest: {PENDING}" or "digest: sha256:<64 hex>" line')
    try:
        lines = (root / PIN).read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        raise malformed from None
    fields: dict[str, list[str]] = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, sep, value = line.partition(':')
        if not sep:
            raise malformed
        fields.setdefault(name.strip(), []).append(value.strip())
    keys, digests = fields.get('key', []), fields.get('digest', [])
    if (len(keys) != 1 or len(digests) != 1 or not KEY.fullmatch(keys[0])
            or not DIGEST.fullmatch(digests[0])):
        raise malformed
    return keys[0], digests[0]


def check(root: Path = ROOT) -> str:
    current = key(root)
    pinned, digest = read_pin(root)
    if pinned != current:
        raise DevkitKeyError(
            f'{PIN} is stale: the kit inputs changed. Record key: {current} with '
            f'digest: {PENDING} in a pin PR, then record the digest the devkit '
            'factory (.github/workflows/devkit.yml) pushes for that key')
    if digest == PENDING:
        return f'devkit-key: {PIN} matches key {current}; digest pending (the factory has not pushed it)'
    return f'devkit-key: {PIN} matches key {current} at {digest}'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--check', action='store_true', help=f'compare the key with {PIN}')
    args = parser.parse_args(argv)
    try:
        print(check(args.root) if args.check else key(args.root))
        return 0
    except DevkitKeyError as exc:
        print(f'devkit-key: {exc}', file=sys.stderr)
    except (OSError, UnicodeDecodeError):
        print('devkit-key: a kit key input could not be read', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
