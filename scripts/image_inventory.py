#!/usr/bin/env python3
"""Read installed Python metadata from Docker archive layers without executing them."""
from __future__ import annotations

import argparse
import email.parser
import gzip
import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath

try:
    from scripts import dependency_lock as locks
except ModuleNotFoundError:
    import dependency_lock as _local_locks

    locks = _local_locks

RECEIPT = 'opt/rag-locks/installed-inventory.json'
LOCK = 'opt/rag-locks/locks/cp314-linux-x86_64.json'
REQUIREMENTS = 'opt/rag-locks/requirements.lock.txt'


def path_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise locks.LockError('unsafe image member path')
    return str(path)


def read_member(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.getmember(name)
    if not member.isfile() or member.size > 8_000_000:
        raise locks.LockError('invalid or oversized image metadata')
    stream = archive.extractfile(member)
    if stream is None:
        raise locks.LockError('missing image metadata bytes')
    return stream.read()


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
    with tarfile.open(image, 'r:*') as outer:
        index = json.loads(read_member(outer, 'manifest.json'))
        if not isinstance(index, list) or len(index) != 1:
            raise locks.LockError('image archive must contain exactly one image')
        config_bytes = read_member(outer, index[0]['Config'])
        config = json.loads(config_bytes)
        if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
            raise locks.LockError('image target differs from the locked target')
        layers = index[0]['Layers']
        diff_ids = config['rootfs']['diff_ids']
        if not layers or len(layers) != len(diff_ids):
            raise locks.LockError('image layer identity is incomplete')
        for name, diff_id in zip(layers, diff_ids, strict=True):
            member = outer.getmember(name)
            if not member.isfile():
                raise locks.LockError('image layer is not a regular member')
            stream = outer.extractfile(member)
            if stream is None:
                raise locks.LockError('missing image layer')
            magic = stream.read(2)
            stream.seek(0)
            # Docker's containerd exporter uses compressed OCI blobs even in
            # Docker archives; skopeo may produce plain tar layers. diff_ids
            # always bind the uncompressed tar bytes.
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
            with tarfile.open(fileobj=decoded, mode='r|') as layer:
                for item in layer:
                    path = path_name(item.name)
                    parts = PurePosixPath(path)
                    if parts.name.startswith('.wh.'):
                        removed_path = parts.parent if parts.name == '.wh..wh..opq' else parts.with_name(parts.name[4:])
                        removals.add(str(removed_path))
                        continue
                    if not item.isdir():
                        replacements.add(path)
                    if item.issym() or item.islnk():
                        new_links.add(path)
                    wanted = path in (RECEIPT, LOCK, REQUIREMENTS) or (package_metadata(path) and not item.isdir())
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
    parser.add_argument('--archive', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(inventory(args.root, args.archive), indent=2, sort_keys=True))
        return 0
    except locks.LockError as exc:
        print(f'image-inventory: {exc}')
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        print('image-inventory: invalid image archive or inventory')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
