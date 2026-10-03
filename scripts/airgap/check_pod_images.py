#!/usr/bin/env python3
"""Compare running container imageIDs with verified registry digests (#272).

stdin: `kubectl get pods -o json`. argv: REPOSITORY=sha256:DIGEST pairs
(repository without tag/digest). Every non-terminating container whose
spec image is in REPOSITORY must report an imageID ending in @DIGEST, and each
REPOSITORY must have at least one such container. Diagnostics name pods,
containers and public digests only.
"""

from __future__ import annotations

import json
import re
import sys


def repository(image: str) -> str:
    image = image.split("@", 1)[0]
    head, sep, leaf = image.rpartition("/")
    return head + sep + leaf.split(":", 1)[0]


def check(pods: dict, expected: dict[str, str]) -> list[str]:
    problems: list[str] = []
    seen: dict[str, int] = dict.fromkeys(expected, 0)
    for pod in pods.get("items", []):
        meta = pod.get("metadata", {})
        if meta.get("deletionTimestamp"):
            continue
        spec = pod.get("spec", {})
        images = {
            c.get("name"): c.get("image", "")
            for c in spec.get("containers", []) + spec.get("initContainers", [])
        }
        status = pod.get("status", {})
        for st in status.get("containerStatuses", []) + status.get("initContainerStatuses", []):
            repo = repository(images.get(st.get("name"), ""))
            if repo not in expected:
                continue
            seen[repo] += 1
            actual = (st.get("imageID") or "").rpartition("@")[2]
            if actual != expected[repo]:
                problems.append(
                    f"pod {meta.get('name')} container {st.get('name')} runs {actual or 'an unknown digest'}, "
                    f"expected {expected[repo]}"
                )
    problems.extend(f"no running container found for {repo}" for repo, n in seen.items() if n == 0)
    return problems


def main(argv: list[str]) -> int:
    expected: dict[str, str] = {}
    for arg in argv:
        repo, _, digest = arg.partition("=")
        if not repo or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            print("FAIL: arguments must be REPOSITORY=sha256:DIGEST", file=sys.stderr)
            return 2
        expected[repo] = digest
    try:
        pods = json.load(sys.stdin)
    except ValueError:
        print("FAIL: pod list is not valid JSON", file=sys.stderr)
        return 1
    problems = check(pods, expected)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"==> running images match the verified registry digests ({len(expected)} images)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
