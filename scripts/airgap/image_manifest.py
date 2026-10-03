#!/usr/bin/env python3
"""Shared single-image config/layer validation for archive and registry stages.

Stage callers own registry access and fixed diagnostics. Exit 3 means archive
manifest checksum mismatch, 4 invalid single-image structure, 5 image mismatch.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


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


def main(argv: list[str]) -> int:
    try:
        if argv == ["config"]:
            print(image_config(sys.stdin.buffer.read())[0])
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
    except (ValueError, KeyError, TypeError, OSError):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
