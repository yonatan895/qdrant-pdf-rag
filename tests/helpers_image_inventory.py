"""Original synthetic OCI (and legacy Docker) image archives with faithful layer/whiteout semantics."""
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path


def tar_bytes(files: dict[str, bytes | None]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            if data is None:
                item.type = tarfile.DIRTYPE
                archive.addfile(item)
            else:
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


def image_files(root: Path) -> dict[str, bytes | None]:
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


OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
DOCKER_MANIFEST = 'application/vnd.docker.distribution.manifest.v2+json'
_CONFIG_TYPE = {OCI_MANIFEST: 'application/vnd.oci.image.config.v1+json',
                DOCKER_MANIFEST: 'application/vnd.docker.container.image.v1+json'}
_LAYER_TYPE = {(OCI_MANIFEST, False): 'application/vnd.oci.image.layer.v1.tar',
               (OCI_MANIFEST, True): 'application/vnd.oci.image.layer.v1.tar+gzip',
               (DOCKER_MANIFEST, False): 'application/vnd.docker.image.rootfs.diff.tar',
               (DOCKER_MANIFEST, True): 'application/vnd.docker.image.rootfs.diff.tar.gzip'}


def oci_archive(path: Path, config: bytes, layers: list[bytes], *, manifest_type: str = OCI_MANIFEST,
                compressed: bool = False) -> bytes:
    """Write an `oci-archive` layout as `skopeo copy --preserve-digests` does; return the manifest bytes."""
    blobs: dict[str, bytes | None] = {'blobs/': None, 'blobs/sha256/': None}

    def put(data: bytes) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        blobs[f'blobs/sha256/{digest}'] = data
        return {'digest': f'sha256:{digest}', 'size': len(data)}

    manifest = json.dumps({'schemaVersion': 2, 'mediaType': manifest_type,
                           'config': {'mediaType': _CONFIG_TYPE[manifest_type], **put(config)},
                           'layers': [{'mediaType': _LAYER_TYPE[manifest_type, compressed], **put(layer)}
                                      for layer in layers]}, separators=(',', ':')).encode()
    index = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
             'manifests': [{'mediaType': manifest_type, **put(manifest)}]}
    path.write_bytes(tar_bytes({**blobs, 'index.json': json.dumps(index).encode(),
                                'oci-layout': b'{"imageLayoutVersion":"1.0.0"}'}))
    return manifest


def _image_config(layers: list[bytes]) -> bytes:
    return json.dumps({'os': 'linux', 'architecture': 'amd64',
                       'rootfs': {'type': 'layers', 'diff_ids': [
                           'sha256:' + hashlib.sha256(data).hexdigest() for data in layers]}}).encode()


def write_image(path: Path, layers: list[dict[str, bytes | None]], *, compressed: bool = False) -> bytes:
    """An OCI archive of these filesystem layers (gzip blobs when compressed); returns its manifest."""
    tars = [tar_bytes(files) for files in layers]
    blobs = [gzip.compress(data, mtime=0) for data in tars] if compressed else tars
    return oci_archive(path, _image_config(tars), blobs, manifest_type=DOCKER_MANIFEST, compressed=compressed)


def write_docker_archive(path: Path, layers: list[dict[str, bytes | None]]) -> None:
    """A legacy `docker-archive` (pre-#1066 bundle member) of these layers."""
    members: dict[str, bytes | None] = {f'{index}/layer.tar': tar_bytes(files) for index, files in enumerate(layers)}
    config = _image_config([data for data in members.values() if data is not None])
    config_name = hashlib.sha256(config).hexdigest() + '.json'
    index = [{'Config': config_name, 'Layers': list(members)}]
    members[config_name] = config
    members['manifest.json'] = json.dumps(index).encode()
    path.write_bytes(tar_bytes(members))
