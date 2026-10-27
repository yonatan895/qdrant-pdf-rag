#!/usr/bin/env python3
"""Refuse a Qdrant volume size the existing StatefulSet cannot take.

A StatefulSet's volumeClaimTemplates are immutable, so `helm upgrade` with a
different size fails after earlier steps already changed the namespace. This
read-only check compares the sizes the release would render with the existing
StatefulSet and refuses a mismatch before any change (#1070).

Usage: check_qdrant_volumes.py --name < helm-template-output   (print the StatefulSet's name)
       check_qdrant_volumes.py LIVE_STATEFULSET_JSON < helm-template-output
Exit 0: accepted (no existing StatefulSet, or equal sizes). Fixed diagnostics
only; sizes and manifest content are never echoed.
"""
from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

import yaml

# volumeClaimTemplate name -> the airgap.env key that sets it.
KEYS = {"qdrant-storage": "QDRANT_DATA_SIZE", "qdrant-snapshots": "QDRANT_SNAPSHOT_SIZE"}
UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50}


def _bytes(raw: object) -> Fraction | None:
    text = str(raw)
    for suffix, factor in UNITS.items():
        if text.endswith(suffix):
            try:
                return Fraction(text[: -len(suffix)]) * factor
            except ValueError:
                return None
    try:
        return Fraction(text)
    except ValueError:
        return None


def _sizes(templates: list[dict]) -> dict[str, Fraction | None]:
    return {
        t["metadata"]["name"]: _bytes(t["spec"]["resources"]["requests"]["storage"])
        for t in templates
        if t.get("metadata", {}).get("name") in KEYS
    }


def main() -> int:
    if sys.argv[1:] == ["--name"]:
        # The chart's own fullname logic decides the name (the release alone when
        # it already contains "qdrant"), so read it from the render, never guess.
        names = [d["metadata"]["name"] for d in yaml.safe_load_all(sys.stdin.read())
                 if d and d.get("kind") == "StatefulSet"]
        if len(names) != 1:
            return 1
        print(names[0])
        return 0
    live_text = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
    if not live_text:
        return 0  # nothing exists yet: any size is a first install
    live = _sizes(json.loads(live_text).get("spec", {}).get("volumeClaimTemplates") or [])
    wanted: dict[str, Fraction | None] = {}
    for doc in yaml.safe_load_all(sys.stdin.read()):
        if doc and doc.get("kind") == "StatefulSet":
            wanted = _sizes(doc["spec"].get("volumeClaimTemplates") or [])
    changed = [KEYS[name] for name in KEYS if name in live and name in wanted and live[name] != wanted[name]]
    if changed:
        print(
            "FAIL: the existing Qdrant StatefulSet has different volume sizes and its volume claim "
            "templates are immutable; set " + ", ".join(changed) + " to the sizes it was created with "
            "(grow a volume with `oc patch pvc`, never by changing the key)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
