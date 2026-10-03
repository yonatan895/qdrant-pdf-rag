#!/usr/bin/env python3
"""Verify current release workloads, including each replica's imageIDs (#272).

stdin: kubectl get deployments,statefulsets,replicasets,pods -o json.
argv: NAMESPACE QDRANT_RELEASE ROLE=REPOSITORY@sha256:DIGEST ...
Roles bind to workload/container names independently of observed image values.
Controller UIDs, selectors and current revisions exclude diagnostic Jobs and
old rollout Pods. Successful init containers on current Pods remain relevant.
"""
from __future__ import annotations

import json
import re
import sys

ROLE_TARGETS = {
    "qdrant": ("StatefulSet", None, "qdrant"),
    "agent": ("Deployment", "rag-agent", "agent"),
    "jaeger": ("Deployment", "jaeger", "jaeger"),
    "oauth_proxy": ("Deployment", "rag-agent", "oauth-proxy"),
}
REVISION = "deployment.kubernetes.io/revision"


def repository(image: str) -> str:
    image = image.split("@", 1)[0]
    head, sep, leaf = image.rpartition("/")
    return head + sep + leaf.split(":", 1)[0]


def controlled_by(item: dict, kind: str, uid: str) -> bool:
    return any(ref.get("controller") is True and ref.get("kind") == kind and ref.get("uid") == uid
               for ref in item.get("metadata", {}).get("ownerReferences", []))


def check(document: dict, expected: dict[str, tuple[str, str]], namespace: str,
          qdrant_release: str) -> list[str]:
    items = document["items"]
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("invalid workload inventory")
    items = [item for item in items if item.get("metadata", {}).get("namespace") == namespace]
    problems: list[str] = []
    targets: dict[tuple[str, str], dict[tuple[str, str], tuple[str, str]]] = {}
    for role, image in expected.items():
        kind, name, container = ROLE_TARGETS[role]
        targets.setdefault((kind, name or qdrant_release), {})[("containers", container)] = image
    for (kind, name), required in targets.items():
        workloads = [item for item in items if item.get("kind") == kind
                     and item.get("metadata", {}).get("name") == name]
        if len(workloads) != 1:
            problems.append(f"missing or ambiguous {kind} {name}")
            continue
        workload = workloads[0]
        meta, spec = workload["metadata"], workload["spec"]
        uid = meta.get("uid")
        replicas = spec.get("replicas", 1)
        selector = spec["selector"]
        labels = selector.get("matchLabels")
        if (not uid or meta.get("deletionTimestamp") or type(replicas) is not int or replicas < 1
                or not isinstance(labels, dict) or not labels or selector.get("matchExpressions")):
            problems.append(f"cannot establish current replicas of {kind} {name}")
            continue
        template = spec["template"]["spec"]
        # The vendored chart's optional Qdrant init role is identified by
        # name even if its declared repository has drifted.
        if (kind == "StatefulSet" and name == qdrant_release
                and any(c.get("name") == "ensure-dir-ownership" for c in template.get("initContainers", []))):
            required[("initContainers", "ensure-dir-ownership")] = expected["qdrant"]
        # Bind additional verified containers (notably init containers) by the
        # workload template's name, then check the Pod's actual repository too.
        by_repo = {repo: (repo, digest) for repo, digest in expected.values()}
        for field in ("containers", "initContainers"):
            for container in template.get(field, []):
                template_image = by_repo.get(repository(container["image"]))
                if template_image:
                    required.setdefault((field, container["name"]), template_image)
        template_containers = {(field, c["name"]): c for field in ("containers", "initContainers")
                               for c in template.get(field, [])}
        for key, (repo, _) in required.items():
            if repository(template_containers.get(key, {}).get("image", "")) != repo:
                problems.append(f"{kind} {name} container {key[1]} has an unexpected repository")
        if kind == "Deployment":
            revision = meta.get("annotations", {}).get(REVISION)
            controllers = [item for item in items if item.get("kind") == "ReplicaSet"
                           and controlled_by(item, kind, uid)
                           and not item["metadata"].get("deletionTimestamp")
                           and revision and item["metadata"].get("annotations", {}).get(REVISION) == revision]
            if len(controllers) != 1 or not controllers[0]["metadata"].get("uid"):
                problems.append(f"cannot establish current ReplicaSet of Deployment {name}")
                continue
            pod_kind, pod_uid = "ReplicaSet", controllers[0]["metadata"]["uid"]
            revision = None
        else:
            pod_kind, pod_uid = kind, uid
            revision = workload.get("status", {}).get("updateRevision")
            if not revision:
                problems.append(f"cannot establish current revision of StatefulSet {name}")
                continue
        pods = [item for item in items if item.get("kind") == "Pod"
                and controlled_by(item, pod_kind, pod_uid)
                and not item["metadata"].get("deletionTimestamp")]
        current = []
        for pod in pods:
            pod_labels = pod["metadata"].get("labels", {})
            if (any(pod_labels.get(key) != value for key, value in labels.items())
                    or revision and pod_labels.get("controller-revision-hash") != revision):
                problems.append(f"pod {pod['metadata'].get('name')} is not the current {kind} {name} revision/selector")
                continue
            current.append(pod)
        if len(current) != replicas:
            problems.append(f"{kind} {name} has {len(current)} current pods, expected {replicas}")
        for pod in current:
            pod_name = pod["metadata"].get("name")
            status = pod.get("status", {})
            if status.get("phase") != "Running":
                problems.append(f"pod {pod_name} is not Running")
            containers = {(field, c["name"]): c for field in ("containers", "initContainers")
                          for c in pod["spec"].get(field, [])}
            statuses = {(field, c["name"]): c for field, status_field in (
                ("containers", "containerStatuses"), ("initContainers", "initContainerStatuses"))
                        for c in status.get(status_field, [])}
            for key, (repo, digest) in required.items():
                container = key[1]
                if repository(containers.get(key, {}).get("image", "")) != repo:
                    problems.append(f"pod {pod_name} container {container} has an unexpected repository")
                actual = (statuses.get(key, {}).get("imageID") or "").rpartition("@")[2]
                if actual != digest:
                    problems.append(f"pod {pod_name} container {container} runs {actual or 'an unknown digest'}, expected {digest}")
    return problems


def main(argv: list[str]) -> int:
    expected: dict[str, tuple[str, str]] = {}
    if len(argv) < 3:
        print("FAIL: arguments must be NAMESPACE QDRANT_RELEASE ROLE=REPOSITORY@sha256:DIGEST", file=sys.stderr)
        return 2
    for arg in argv[2:]:
        role, _, image = arg.partition("=")
        repo, _, digest = image.partition("@")
        if role not in ROLE_TARGETS or role in expected or not repo or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            print("FAIL: arguments must be unique ROLE=REPOSITORY@sha256:DIGEST", file=sys.stderr)
            return 2
        expected[role] = (repo, digest)
    try:
        problems = check(json.load(sys.stdin), expected, argv[0], argv[1])
    except (ValueError, KeyError, TypeError, AttributeError):
        print("FAIL: workload inventory is invalid; running image identity is not verified", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"==> running images match the verified registry digests ({len(expected)} roles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
