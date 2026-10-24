#!/usr/bin/env python3
"""Shared single-image config/layer validation for archive and registry stages.

Stage callers own registry access and fixed diagnostics. Exit 3 means archive
manifest checksum mismatch, 4 invalid single-image structure, 5 image mismatch,
6 an archive blob that differs from its descriptor (#1066).

Bundle image members are `oci-archive` tars (#1066). `read_archive` is the one
reader of that layout for pack, load and the SBOM inventory: `oci-layout`
version, an `index.json` naming exactly one single-image manifest, and every
manifest, config and layer blob present once with the size and sha256 its
descriptor names. The manifest bytes are the registry's (pack copies with
`--preserve-digests`), so the archive digest is the platform manifest digest.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# Single-image manifest types an OCI layout may hold: OCI, and Docker schema 2
# kept byte-for-byte by `skopeo copy --preserve-digests`.
IMAGE_MANIFEST_TYPES = frozenset({
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
})
METADATA_LIMIT = 8_000_000


class ArchiveError(ValueError):
    """The member is not a single-image OCI archive."""


class BlobMismatch(ArchiveError):
    """A blob is missing or differs from the size/digest its descriptor names."""


@dataclass(frozen=True)
class OciImage:
    manifest_raw: bytes
    config_raw: bytes
    layers: tuple[str, ...]
    blobs: dict[str, tarfile.TarInfo]

    @property
    def digest(self) -> str:
        return manifest_digest(self.manifest_raw)


def manifest_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def image_config(raw: bytes) -> tuple[str, int]:
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or "manifests" in manifest:
        raise ValueError("not a single image")
    config = manifest["config"]["digest"]
    layers = manifest["layers"]
    if not isinstance(config, str) or not DIGEST.fullmatch(config) or not isinstance(layers, list):
        raise ValueError("invalid image config/layers")
    for layer in layers:
        if (not isinstance(layer, dict) or not isinstance(layer.get("digest"), str)
                or not DIGEST.fullmatch(layer["digest"])):
            raise ValueError("invalid image layer")
    return config, len(layers)


def _members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for item in archive.getmembers():
        path = PurePosixPath(item.name)
        if path.is_absolute() or ".." in path.parts:
            raise ArchiveError("unsafe archive member path")
        name = str(path)
        if item.isdir():
            continue
        # A later duplicate would shadow the verified bytes for some readers.
        if name in members or not item.isfile():
            raise ArchiveError("duplicate or non-regular archive member")
        members[name] = item
    return members


def _descriptor(value: object) -> tuple[str, int]:
    if not isinstance(value, dict):
        raise ArchiveError("invalid descriptor")
    digest, size = value.get("digest"), value.get("size")
    if (not isinstance(digest, str) or not DIGEST.fullmatch(digest)
            or not isinstance(size, int) or isinstance(size, bool) or size < 0):
        raise ArchiveError("invalid descriptor")
    return digest, size


def _blob(archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo],
          digest: str, size: int, keep: bool) -> bytes:
    item = members.get("blobs/sha256/" + digest[7:])
    if item is None or item.size != size or (keep and size > METADATA_LIMIT):
        raise BlobMismatch("archive blob is missing or has the wrong size")
    stream = archive.extractfile(item)
    if stream is None:
        raise BlobMismatch("archive blob is unreadable")
    sha = hashlib.sha256()
    kept = bytearray()
    while block := stream.read(1024 * 1024):
        sha.update(block)
        if keep:
            kept += block
    if "sha256:" + sha.hexdigest() != digest:
        raise BlobMismatch("archive blob does not match its digest")
    return bytes(kept)


def _small(archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo], name: str) -> object:
    item = members.get(name)
    if item is None or item.size > METADATA_LIMIT:
        raise ArchiveError(f"archive has no usable {name}")
    stream = archive.extractfile(item)
    if stream is None:
        raise ArchiveError(f"archive has no usable {name}")
    return json.loads(stream.read())


def read_archive(archive: tarfile.TarFile) -> OciImage:
    """Verify one oci-archive member and return its manifest, config and layers."""
    members = _members(archive)
    layout = _small(archive, members, "oci-layout")
    if not isinstance(layout, dict) or layout.get("imageLayoutVersion") != "1.0.0":
        raise ArchiveError("unsupported OCI layout version")
    index = _small(archive, members, "index.json")
    if not isinstance(index, dict) or not isinstance(index.get("manifests"), list) or len(index["manifests"]) != 1:
        raise ArchiveError("OCI index must name exactly one image")
    entry = index["manifests"][0]
    digest, size = _descriptor(entry)
    if entry.get("mediaType") not in IMAGE_MANIFEST_TYPES:
        raise ArchiveError("OCI index entry is not a single-image manifest")
    manifest_raw = _blob(archive, members, digest, size, keep=True)
    image_config(manifest_raw)
    manifest = json.loads(manifest_raw)
    config_digest, config_size = _descriptor(manifest["config"])
    config_raw = _blob(archive, members, config_digest, config_size, keep=True)
    layers = []
    for layer in manifest["layers"]:
        layer_digest, layer_size = _descriptor(layer)
        _blob(archive, members, layer_digest, layer_size, keep=False)
        layers.append(layer_digest)
    return OciImage(manifest_raw, config_raw, tuple(layers), members)


def open_blob(archive: tarfile.TarFile, image: OciImage, digest: str) -> IO[bytes]:
    stream = archive.extractfile(image.blobs["blobs/sha256/" + digest[7:]])
    if stream is None:
        raise BlobMismatch("archive blob is unreadable")
    return stream


def main(argv: list[str]) -> int:
    try:
        if argv == ["config"]:
            print(image_config(sys.stdin.buffer.read())[0])
        elif len(argv) == 3 and argv[0] == "archive":
            # Verified manifest bytes go to argv[2]; stdout is their digest.
            with tarfile.open(argv[1], "r:") as archive:
                image = read_archive(archive)
            Path(argv[2]).write_bytes(image.manifest_raw)
            print(image.digest)
        elif len(argv) == 3 and argv[0] == "registry":
            raw = Path(argv[1]).read_bytes()
            if image_config(raw)[0] != argv[2]:
                return 5
            print(manifest_digest(raw))
        elif len(argv) == 4 and argv[0] == "load":
            archive_raw = Path(argv[1]).read_bytes()
            if manifest_digest(archive_raw) != argv[3]:
                return 3
            registry_raw = Path(argv[2]).read_bytes()
            if image_config(archive_raw) != image_config(registry_raw):
                return 5
            print(manifest_digest(registry_raw))
        else:
            return 2
    except BlobMismatch:
        return 6
    except (ValueError, KeyError, TypeError, OSError, tarfile.TarError):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
