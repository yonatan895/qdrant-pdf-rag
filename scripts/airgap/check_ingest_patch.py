#!/usr/bin/env python3
"""Refuse an INGEST_EXTRA_PATCH that changes anything but container resources.

The CI-rehearsal patch exists to shrink the Job to a lab quota. Applied after
the render checks, it must not be able to change what the Job runs: images,
env (hash embeddings), security context, volumes, extra containers, identity.
Usage: check_ingest_patch.py BEFORE.yaml AFTER.yaml  (exit 0 = accepted).
Fixed diagnostics only; manifest content is never echoed.
"""
from __future__ import annotations

import copy
import sys

import yaml

FIXED = "INGEST_EXTRA_PATCH may only change container resources (the patched Job differs elsewhere)"
HASH_KEYS = {"EMBED_MODE", "ALLOW_HASH_MODE"}


def _strip_resources(doc: object) -> object:
    stripped = copy.deepcopy(doc)
    try:
        spec = stripped["spec"]["template"]["spec"]
        for key in ("containers", "initContainers"):
            for container in spec.get(key) or []:
                container.pop("resources", None)
    except (KeyError, TypeError, AttributeError):
        pass
    return stripped


def _hash_env(doc: object) -> bool:
    try:
        spec = doc["spec"]["template"]["spec"]
        containers = (spec.get("containers") or []) + (spec.get("initContainers") or [])
        return any(
            entry.get("name") in HASH_KEYS and str(entry.get("value", "")).lower() in {"hash", "true", "1", "yes"}
            for container in containers for entry in container.get("env") or []
        )
    except (KeyError, TypeError, AttributeError):
        return True


def main(argv: list[str]) -> int:
    try:
        with open(argv[1], encoding="utf-8") as before, open(argv[2], encoding="utf-8") as after:
            old, new = yaml.safe_load(before), yaml.safe_load(after)
    except (IndexError, OSError, yaml.YAMLError):
        print("FAIL: cannot read the rendered or patched ingest Job", file=sys.stderr)
        return 2
    if _hash_env(new) or _strip_resources(old) != _strip_resources(new):
        print(f"FAIL: {FIXED}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
