"""Original synthetic Docker archives with faithful layer/whiteout semantics."""
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path


def tar_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


def image_files(root: Path) -> dict[str, bytes]:
    manifest = json.loads((root / 'locks/cp314-linux-x86_64.json').read_bytes())
    packages = {name: manifest['packages'][name]['version'] for name in manifest['profiles']['runtime']['packages']}
    packages.update({'pip': '24.2'})
    lock_bytes = (root / 'locks/cp314-linux-x86_64.json').read_bytes()
    requirements = (root / 'requirements.lock.txt').read_bytes()
    receipt = {'schema_version': 1, 'profile': 'runtime', 'packages': packages,
               'project_installed': False, 'base_image': manifest['base_image']['reference'],
               'lock_sha256': hashlib.sha256(lock_bytes).hexdigest(),
               'requirements_sha256': hashlib.sha256(requirements).hexdigest()}
    files = {f'opt/app-root/lib/python3.14/site-packages/{name}-{version}.dist-info/METADATA':
             f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n'.encode()
             for name, version in packages.items()}
    files.update({'opt/rag-locks/installed-inventory.json': json.dumps(receipt).encode(),
                  'opt/rag-locks/locks/cp314-linux-x86_64.json': lock_bytes,
                  'opt/rag-locks/requirements.lock.txt': requirements})
    return files


def write_image(path: Path, layers: list[dict[str, bytes]], *, compressed: bool = False) -> None:
    members = {f'{index}/layer.tar': tar_bytes(files) for index, files in enumerate(layers)}
    config = json.dumps({'os': 'linux', 'architecture': 'amd64',
                         'rootfs': {'type': 'layers', 'diff_ids': [
                             'sha256:' + hashlib.sha256(data).hexdigest() for data in members.values()]}}).encode()
    config_name = hashlib.sha256(config).hexdigest() + '.json'
    index = [{'Config': config_name, 'Layers': list(members)}]
    if compressed:
        members = {name: gzip.compress(data, mtime=0) for name, data in members.items()}
    members[config_name] = config
    members['manifest.json'] = json.dumps(index).encode()
    path.write_bytes(tar_bytes(members))
