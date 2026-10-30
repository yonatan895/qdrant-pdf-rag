#!/usr/bin/env python3
"""Read installed Python metadata from OCI archive layers without executing them."""
from __future__ import annotations

import argparse
import email.parser
import gzip
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

try:
    from scripts import dependency_lock as locks
except ModuleNotFoundError:
    import dependency_lock as _local_locks

    locks = _local_locks
try:
    from scripts.airgap import image_manifest as oci
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent / 'airgap'))
    import image_manifest as _local_oci

    oci = _local_oci

RECEIPT = 'opt/rag-locks/installed-inventory.json'
LOCK = 'opt/rag-locks/locks/cp314-linux-x86_64.json'
REQUIREMENTS = 'opt/rag-locks/requirements.lock.txt'


def path_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise locks.LockError('unsafe image member path')
    return str(path)


def package_metadata(path: str) -> bool:
    search_path = '/site-packages/' in path or '/dist-packages/' in path or path.startswith('app/src/')
    return search_path and path.endswith(('.dist-info/METADATA', '.egg-info/PKG-INFO', '.egg-info'))


def inventory(root: Path, image: Path) -> dict:
    manifest, packages = locks.load(root, 'runtime')
    expected = {name: entry['version'] for name, entry in packages.items()}
    if manifest['base_image'] != {'reference': locks.BASE_IMAGE, 'inherited_python_packages': {'pip': '24.2'}}:
        raise locks.LockError('unrecognized image base inventory')
    expected.update(manifest['base_image']['inherited_python_packages'])
    retained: dict[str, bytes] = {}
    links: set[str] = set()
    with tarfile.open(image, 'r:') as outer:
        # The bundle's OCI reader (#1066) verifies the single image's manifest,
        # config and every layer blob against its descriptor first.
        try:
            image_layout = oci.read_archive(outer)
        except oci.BlobMismatch:
            raise locks.LockError('image archive blob does not match its digest') from None
        except (oci.ArchiveError, KeyError, TypeError) as exc:
            raise locks.LockError('image archive must contain exactly one OCI image') from exc
        config_bytes = image_layout.config_raw
        config = json.loads(config_bytes)
        if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
            raise locks.LockError('image target differs from the locked target')
        layers = image_layout.layers
        diff_ids = config['rootfs']['diff_ids']
        if not layers or len(layers) != len(diff_ids):
            raise locks.LockError('image layer identity is incomplete')
        for name, diff_id in zip(layers, diff_ids, strict=True):
            stream = oci.open_blob(outer, image_layout, name)
            magic = stream.read(2)
            stream.seek(0)
            # Registry layers are gzip (the factory's push) or plain tar;
            # diff_ids always bind the uncompressed tar bytes.
            decoded = gzip.GzipFile(fileobj=stream) if magic == b'\x1f\x8b' else stream
            layer_hash = hashlib.sha256()
            while block := decoded.read(1024 * 1024):
                layer_hash.update(block)
            if 'sha256:' + layer_hash.hexdigest() != diff_id:
                raise locks.LockError('image layer digest mismatch')
            decoded.seek(0)
            additions: dict[str, bytes] = {}
            new_links: set[str] = set()
            replacements: set[str] = set()
            removals: set[str] = set()
            directories: set[str] = set()
            with tarfile.open(fileobj=decoded, mode='r|') as layer:
                for item in layer:
                    path = path_name(item.name)
                    parts = PurePosixPath(path)
                    if parts.name.startswith('.wh.'):
                        removed_path = parts.parent if parts.name == '.wh..wh..opq' else parts.with_name(parts.name[4:])
                        removals.add(str(removed_path))
                        continue
                    if item.isdir():
                        directories.add(path)
                    else:
                        replacements.add(path)
                    if item.issym() or item.islnk():
                        new_links.add(path)
                    wanted = path in (RECEIPT, LOCK, REQUIREMENTS) or (
                        package_metadata(path) and not (item.isdir() and path.endswith('.egg-info')))
                    if wanted:
                        if not item.isfile() or item.size > 8_000_000:
                            raise locks.LockError('installed metadata is not a bounded regular file')
                        content = layer.extractfile(item)
                        if content is None:
                            raise locks.LockError('installed metadata is missing')
                        additions[path] = content.read()
            for target in removals | replacements:
                prefix = '' if target == '.' else target + '/'
                retained = {p: v for p, v in retained.items() if p != target and not p.startswith(prefix)}
                links = {p for p in links if p != target and not p.startswith(prefix)}
            for directory in directories:
                retained.pop(directory, None)
                links.discard(directory)
            retained.update(additions)
            links.update(new_links)
    for path in retained:
        if any(path == link or path.startswith(link + '/') for link in links):
            raise locks.LockError('installed metadata traverses a link')
    if (retained.get(LOCK) != (root / locks.MANIFEST).read_bytes()
            or retained.get(REQUIREMENTS) != (root / locks.PROFILE_FILES['runtime']).read_bytes()):
        raise locks.LockError('shipped image lock differs from release source')
    actual: dict[str, str] = {}
    for path, data in retained.items():
        if not package_metadata(path):
            continue
        metadata = email.parser.BytesParser().parsebytes(data, headersonly=True)
        name = locks.canonical(metadata['Name'])
        if name in actual or not metadata['Version']:
            raise locks.LockError('duplicate or incomplete installed image metadata')
        actual[name] = metadata['Version']
    if actual != expected:
        raise locks.LockError('actual installed image packages differ from the lock/base profile')
    receipt = json.loads(retained.get(RECEIPT, b'{}'))
    if (receipt.get('packages') != actual or receipt.get('profile') != 'runtime'
            or receipt.get('lock_sha256') != locks.digest(root / locks.MANIFEST)
            or receipt.get('requirements_sha256') != locks.digest(root / locks.PROFILE_FILES['runtime'])
            or receipt.get('base_image') != locks.BASE_IMAGE or receipt.get('project_installed') is not False):
        raise locks.LockError('image receipt omits or misattributes installed packages')
    return {'schema_version': 1, 'archive_sha256': locks.digest(image),
            'config_sha256': hashlib.sha256(config_bytes).hexdigest(),
            'lock_sha256': locks.digest(root / locks.MANIFEST),
            'packages': dict(sorted(actual.items()))}


FROM_LINE = re.compile(r'^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$', re.IGNORECASE)


# Named build contexts the release build supplies (docker build --build-context);
# they are directories, not registry images. devkit-tools holds the pinned Task,
# Helm and oc archives of the dev workspace image (#1118, .github/workflows/devkit.yml).
BUILD_CONTEXTS = frozenset({'wheelhouse', 'bm25', 'devkit-tools'})

# `COPY --from=X`, `ADD --from=X` and `RUN --mount=...,from=X`: X must be an earlier
# stage alias; anything else is an unpinned external image (#1030).
FROM_SOURCE = re.compile(r'(?:--from=|--mount=[^\s]*?\bfrom=|(?<=,)from=)([^\s,]+)')


def check_containerfile_bases(root: Path) -> None:
    """Every images/Containerfile.* FROM must be the approved base pin (#1030).

    The pin is locks.BASE_IMAGE; images.txt (what pack.sh labels the SBOM with)
    and the lock's base_image reference must agree with it, and each Containerfile
    FROM (a later stage may start FROM an earlier stage alias) must be exactly it.
    Fixed diagnostics only.
    """
    digest = locks.BASE_IMAGE.rpartition('@')[2]
    repository = locks.BASE_IMAGE.rpartition('@')[0]
    pins = [line.split() for line in (root / 'images.txt').read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    recorded = [p[1] for p in pins if len(p) >= 2 and 'python-314-minimal' in p[0]]
    if recorded != [digest]:
        raise locks.LockError('images.txt base digest differs from the approved base pin')
    manifest = locks.read_json(root / locks.MANIFEST)
    if manifest.get('base_image', {}).get('reference') != locks.BASE_IMAGE:
        raise locks.LockError('lock base image differs from the approved base pin')
    files = sorted((root / 'images').glob('Containerfile.*'))
    if not files:
        raise locks.LockError('no images/Containerfile.* to verify')
    for path in files:
        stages: set[str] = set()
        bases = 0
        for line in path.read_text().replace('\\\n', ' ').splitlines():
            for source in FROM_SOURCE.findall(line):
                if source not in stages and source not in BUILD_CONTEXTS:
                    raise locks.LockError('Containerfile copies from or mounts a non-stage image')
            match = FROM_LINE.match(line)
            if not match:
                if re.match(r'^\s*FROM\b', line, re.IGNORECASE):
                    raise locks.LockError('unparseable Containerfile FROM line')
                continue
            reference, alias = match.groups()
            if reference not in stages:
                if reference != locks.BASE_IMAGE and reference.partition('@')[0] == repository:
                    raise locks.LockError('Containerfile base digest differs from the approved base pin')
                if reference != locks.BASE_IMAGE:
                    raise locks.LockError('Containerfile base is not the approved digest-pinned base')
                bases += 1
            if alias:
                stages.add(alias)
        if bases == 0:
            raise locks.LockError('Containerfile has no approved base')


def verify_sbom(root: Path, directory: Path, commit: str) -> None:
    """Reconcile the serialized SBOM, independently of its producer's objects."""
    sbom = locks.read_json(directory / 'sbom.json')
    _, packages = locks.load(root, 'runtime')
    expected_wheels = [{'name': name, **entry} for name, entry in sorted(packages.items())]
    observed = {role: inventory(root, directory / f'app-{role}-{commit}.tar')
                for role in ('agent', 'ingest')}
    if (sbom.get('image_sha') != commit or sbom.get('wheels') != expected_wheels
            or sbom.get('installed_python') != observed):
        raise locks.LockError('serialized SBOM omits or misattributes shipped Python components')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=locks.ROOT)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--check-containerfiles', action='store_true',
                        help='verify images/Containerfile.* FROM digests against images.txt and the lock')
    args = parser.parse_args()
    try:
        if args.check_containerfiles:
            check_containerfile_bases(args.root)
            print('image-inventory: Containerfile bases match the approved pin')
            return 0
        if args.archive is None:
            parser.error('--archive is required')
        print(json.dumps(inventory(args.root, args.archive), indent=2, sort_keys=True))
        return 0
    except locks.LockError as exc:
        print(f'image-inventory: {exc}')
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        print('image-inventory: invalid image archive or inventory')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
